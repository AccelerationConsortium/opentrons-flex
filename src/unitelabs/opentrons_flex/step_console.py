"""
Step console: a local web page that sends single Flex operations to the connector over SiLA gRPC.

Every button and every step of a built sequence becomes one or more SiLA commands
(MotionController, TipController, PipetteController, GripperController,
RecoveryController). The robot stays where each step leaves it; nothing homes
unless the operator adds a Home step. XY travel is always lift, move level, descend:
every mount is first raised to the travel height, so no move is diagonal.

    uv run python -m unitelabs.opentrons_flex.step_console --sila 127.0.0.1:50052 --robot <robot-ip>

With ``--robot``, a step that needs the other connector mode switches it first (direct jaw
control needs connector-pe; preset plate moves need connector-unit), then homes and continues.

In connector-unit mode the SiLA server listens on the robot's loopback only, so
open an SSH tunnel first: ``ssh -N -L 50052:127.0.0.1:50051 root@<robot-ip>``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import enum
import json
import string
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .asms_sila_workflow import AsmsSilaClient, analyze, compile_plan, connect
from .console_store import DEFAULT_PATH, ConsoleStore, labware_definition
from .features import PipetteMount, WellGeometry
from .features.motion_control import Mount
from .features.tip_controller import TipLocation

if TYPE_CHECKING:
    from aiohttp import web

_MOTION_PACKAGE = "sila2.ca.accelerationconsortium.robots.motioncontroller.v2"
_MOTION_SERVICE = f"{_MOTION_PACKAGE}.MotionController"
_GRIPPER_PACKAGE = "sila2.ca.accelerationconsortium.robots.grippercontroller.v1"
_GRIPPER_SERVICE = f"{_GRIPPER_PACKAGE}.GripperController"
_RECOVERY_PACKAGE = "sila2.ca.accelerationconsortium.robots.recoverycontroller.v1"
_RECOVERY_SERVICE = f"{_RECOVERY_PACKAGE}.RecoveryController"

# Every XY move happens at or above this height. It clears a Flex tip rack (top at
# 99 mm) with margin, and stays below the top of travel of a p1000 carrying a 1000 µL
# tip (about 164 mm) and of the gripper (about 164 mm), so every mount can reach it.
TRAVEL_Z = 150.0
_POSITION_TOLERANCE = 0.05

# Flex deck cutout origins (ot3_standard deck definition, schema 5); a slot is 128 x 86 mm.
_SLOT_ORIGINS = {
    f"{row}{column}": (x, y)
    for row, y in (("A", 321.0), ("B", 214.0), ("C", 107.0), ("D", 0.0))
    for column, x in ((1, 0.0), (2, 164.0), (3, 328.0))
}
_SLOT_SIZE = (128.0, 86.0)


@dataclasses.dataclass(frozen=True)
class TipRack:
    """A Flex tip rack, with the effective tip length Protocol Engine reports for it."""

    tip_volume: float
    diameter: float
    tip_length: float


# Well A1 of both racks sits at (14.38, 74.38) in the slot; the rack top is 99 mm.
# Tip lengths are tipLength minus the pipette's tip overlap, as Protocol Engine passes them
# for the AS-MS protocols (p50 multi + 50 µL tips, p1000 multi + 1000 µL tips).
TIP_RACKS = {
    "opentrons_flex_96_tiprack_50ul": TipRack(tip_volume=50.0, diameter=5.58, tip_length=48.62),
    "opentrons_flex_96_tiprack_1000ul": TipRack(tip_volume=1000.0, diameter=5.47, tip_length=85.93),
}
_RACK_A1 = (14.38, 74.38)
_RACK_TOP_Z = 99.0
_WELL_PITCH = 9.0

# Trash bin in D1, A1-tip positions per mount, as Protocol Engine places the drop for the AS-MS protocols.
TRASH_D1 = {"LEFT": (-52.75, 74.5, 40.0), "RIGHT": (97.25, 74.5, 40.0)}

_GRIP_FORCE_RANGE = (5.0, 25.0)

# MoveTo positions a pipette's A1 nozzle, the back-most nozzle of an 8-channel. The centre of
# the eight nozzles is this far in front of it (7 gaps of 9 mm / 2), the same shift the AS-MS
# compile applies between Protocol Engine's instrument-centre and SiLA's A1 positions.
_EIGHT_CHANNEL_CENTRE_Y = 31.5

# Gripper jaw-centre height for an Axygen 96 plate standing directly in a deck slot, from the
# pinned AS-MS gripper plans (config/asms_unit_labware_movement.json, slot A1 grip point).
DECK_PLATE_GRIP_Z = 7.175


class StepError(ValueError):
    """A step the operator built cannot be sent; the message says which field to fix."""


def slot_centre(slot: str) -> tuple[float, float]:
    """Centre of a deck slot in deck coordinates."""
    try:
        x, y = _SLOT_ORIGINS[slot.upper()]
    except KeyError:
        msg = f"Unknown slot {slot!r}; use A1-D3."
        raise StepError(msg) from None
    return x + _SLOT_SIZE[0] / 2, y + _SLOT_SIZE[1] / 2


_ROWS = "ABCDEFGH"


def _well(name: str) -> tuple[int, int]:
    """Row index (A = 0) and column (1-12) of a 96-well name such as ``H1``."""
    text = str(name).strip().upper()
    if len(text) >= 2 and text[0] in _ROWS and text[1:].isdigit() and 1 <= int(text[1:]) <= 12:
        return _ROWS.index(text[0]), int(text[1:])
    msg = f"Tip well must be A1-H12; got {name!r}."
    raise StepError(msg)


def tip_well_location(slot: str, well: str) -> TipLocation:
    """Location of the top of tip ``well`` in a Flex tip rack sitting in ``slot``."""
    row, column = _well(well)
    try:
        x, y = _SLOT_ORIGINS[slot.upper()]
    except KeyError:
        msg = f"Unknown slot {slot!r}; use A1-D3."
        raise StepError(msg) from None
    return TipLocation(
        round(x + _RACK_A1[0] + (column - 1) * _WELL_PITCH, 2),
        round(y + _RACK_A1[1] - row * _WELL_PITCH, 2),
        _RACK_TOP_Z,
    )


def well_location(load_name: str, slot: str, well: str, origin: str = "top", offset: float = 0.0) -> TipLocation:
    """Deck position of a well's top (or bottom) plus ``offset`` mm, for labware standing directly in ``slot``."""
    definition = labware_definition(load_name)
    geometry = definition["wells"].get(str(well).upper())
    if geometry is None:
        msg = f"{definition['metadata']['displayName']} has no well {well!r}."
        raise StepError(msg)
    x, y = slot_origin(slot)
    bottom = float(geometry["z"])
    z = bottom + float(geometry["depth"]) if origin == "top" else bottom
    return TipLocation(round(x + float(geometry["x"]), 3), round(y + float(geometry["y"]), 3), round(z + offset, 3))


def slot_origin(slot: str) -> tuple[float, float]:
    """Front-left corner of a deck slot in deck coordinates."""
    try:
        return _SLOT_ORIGINS[slot.upper()]
    except KeyError:
        msg = f"Unknown slot {slot!r}; use A1-D3."
        raise StepError(msg) from None


class TipRackTracker:
    """
    Which tips the console has taken from each rack since the operator last marked it full.

    An eight-channel pipette configured to use only its A1 nozzle still carries seven more
    nozzles in front of it (towards row H). Picking well ``R`` of a column presses those
    nozzles onto rows after ``R``, so a single pickup needs every later row of that column
    already empty: single tips are taken from H towards A, as the AS-MS prep protocol does.
    """

    def __init__(self, store: ConsoleStore | None = None) -> None:
        self._store = store or ConsoleStore(":memory:")

    def _taken(self, slot: str, _rack: str) -> set[str]:
        return self._store.used_tips(slot)

    def check_single(self, slot: str, rack: str, well: str) -> None:
        """Refuse a single-nozzle pickup of a taken well or one whose later rows still hold tips."""
        row, column = _well(well)
        taken = self._taken(slot, rack)
        name = f"{_ROWS[row]}{column}"
        if name in taken:
            msg = f"{slot} {name} was already taken. Pick another well, or mark the rack full after refilling it."
            raise StepError(msg)
        blocking = [f"{_ROWS[later]}{column}" for later in range(row + 1, 8) if f"{_ROWS[later]}{column}" not in taken]
        if blocking:
            msg = (
                f"Cannot take {name} alone: the other seven nozzles would press onto {', '.join(blocking)}. "
                f"Take single tips from row H upwards (first {blocking[-1]})."
            )
            raise StepError(msg)

    def check_column(self, slot: str, rack: str, column: int) -> None:
        """Refuse a full-column pickup from a column that is no longer full."""
        taken = self._taken(slot, rack)
        missing = [f"{row}{column}" for row in _ROWS if f"{row}{column}" in taken]
        if missing:
            msg = (
                f"Column {column} in {slot} is not full ({', '.join(missing)} already taken); "
                "a full pickup needs 8 tips."
            )
            raise StepError(msg)

    def take(self, slot: str, _rack: str, wells: list[str]) -> None:
        """Record wells as taken."""
        self._store.take_tips(slot, wells)

    def refill(self, slot: str) -> None:
        """Forget what was taken from ``slot``: the operator put a full rack there."""
        self._store.refill_tips(slot)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return the taken wells per slot that has any."""
        rows = self._store._rows("SELECT DISTINCT slot FROM used_tips ORDER BY slot")
        return {
            row["slot"]: {
                "rack": self._store.labware_in(row["slot"]) or "",
                "taken": sorted(self._store.used_tips(row["slot"])),
            }
            for row in rows
        }


def _mount(value: object, allowed: tuple[str, ...] = ("LEFT", "RIGHT", "GRIPPER")) -> str:
    name = str(value).upper()
    if name not in allowed:
        msg = f"Mount must be one of {', '.join(allowed)}; got {value!r}."
        raise StepError(msg)
    return name


def _number(params: Mapping[str, Any], key: str, default: float | None = None) -> float:
    value = params.get(key, default)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        msg = f"{key} must be a number; got {value!r}."
        raise StepError(msg) from None


def _force(params: Mapping[str, Any]) -> float:
    force = _number(params, "force", 15.0)
    low, high = _GRIP_FORCE_RANGE
    if not low <= force <= high:
        msg = f"Grip force must be {low:g}-{high:g} N; got {force:g}."
        raise StepError(msg)
    return force


# Steps that need one connector mode: unit mode refuses direct jaw control, pe mode has no preset plans.
_STEP_MODES = {
    "gripper_close": "pe",
    "gripper_open": "pe",
    "pick_plate": "pe",
    "place_plate": "pe",
    "move_labware": "unit",
}
_MODE_NAMES = {"pe": "connector-pe", "unit": "connector-unit"}
_RECONNECT_SECONDS = 180.0


class ModeSwitcher:
    """Switch the robot's connector mode with ``flexctl mode`` (over SSH), as an operator would."""

    def __init__(self, robot_host: str) -> None:
        self._robot_host = robot_host
        self._repo = Path(__file__).resolve().parents[3]

    async def switch(self, mode: str) -> None:
        """Run ``flexctl mode <robot> <mode>``; raise StepError with its output when it fails."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "unitelabs.opentrons_flex.flexctl",
            "mode",
            self._robot_host,
            mode,
            cwd=self._repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            tail = " ".join(output.decode("utf-8", "replace").split()[-60:])
            msg = f"Switching to {_MODE_NAMES[mode]} failed (exit {process.returncode}): {tail}"
            raise StepError(msg)


class StepRunner:
    """Turns one console step into SiLA commands. Each step leaves the robot where it ends."""

    def __init__(
        self, client: AsmsSilaClient, switcher: ModeSwitcher | None = None, store: ConsoleStore | None = None
    ) -> None:
        self._client = client
        self._switcher = switcher
        self.store = store or ConsoleStore(":memory:")
        # Liquid bookkeeping: the well each pipette last moved to, what its tip holds, its nozzle layout.
        self._last_well: dict[str, tuple[str, str]] = {}
        self._tip_contents: dict[str, tuple[str, float]] = {}
        self._layout: dict[str, str] = {}
        self._ready: set[str] = set()
        #: What the running step is doing right now, shown on the page while it runs.
        self.activity = ""
        # The jaw state cannot tell a held plate apart: Opentrons also closes an idle jaw when a pipette
        # moves. The console tracks it instead: closed by a grip step, open after a release or jaw home.
        self.racks = TipRackTracker(self.store)
        # The connector marks the gripper Z axis unknown once a pipette moves; reading it then
        # only logs a POSITION_UNKNOWN error on the robot, so status polling skips it until it is homed.
        self._gripper_z_known = True
        self._channels: dict[str, int] = {}

    @property
    def _jaw_may_hold(self) -> bool:
        # Stored, so a console restart cannot forget a plate in the jaw and allow a Home that drops it.
        return self.store.get_flag("jaw_may_hold")

    @_jaw_may_hold.setter
    def _jaw_may_hold(self, value: bool) -> None:
        self.store.set_flag("jaw_may_hold", value)

    async def run(self, kind: str, params: Mapping[str, Any]) -> str:
        """Send one step; raise StepError for a step that cannot be sent."""
        handler: Callable[[Mapping[str, Any]], Awaitable[str]] | None = getattr(self, f"_step_{kind}", None)
        if handler is None:
            msg = f"Unknown step {kind!r}."
            raise StepError(msg)
        self.activity = ""
        try:
            note = await self._ensure_mode(_STEP_MODES.get(kind))
            return note + await handler(params)
        finally:
            self.activity = ""

    async def mode(self) -> str:
        """Return the connector mode: preset gripper plans exist only in connector-unit mode."""
        return "unit" if await self._client.available_plans() else "pe"

    async def _ensure_mode(self, required: str | None) -> str:
        """Switch the connector to ``required`` when it is in the other mode; return a note for the result."""
        if required is None:
            return ""
        current = await self.mode()
        if current == required:
            return ""
        if self._switcher is None:
            msg = (
                f"This step needs {_MODE_NAMES[required]} mode, but the connector is in {_MODE_NAMES[current]} mode. "
                "Start the console with --robot <robot-ip> to switch automatically."
            )
            raise StepError(msg)
        await self._check_safe_to_restart()
        self.activity = (
            f"Switching the connector from {_MODE_NAMES[current]} to {_MODE_NAMES[required]} mode "
            "(restarts the connector, about 1-2 minutes)..."
        )
        # A half-finished switch can leave neither the connector nor the stock server owning the robot,
        # so Stop waits for the switch to finish instead of abandoning it.
        switching = asyncio.ensure_future(self._switch_and_reconnect(required))
        try:
            await asyncio.shield(switching)
        except asyncio.CancelledError:
            self.activity = "Stop requested: finishing the mode switch first, it cannot be interrupted safely..."
            with contextlib.suppress(Exception):
                await switching
            raise
        self.activity = "Homing all axes after the mode switch..."
        await self._client.home()
        self._gripper_z_known = True
        self._channels.clear()
        return f"Switched to {_MODE_NAMES[required]} mode and homed. "

    async def _check_safe_to_restart(self) -> None:
        """Refuse a mode switch while a restart could drop a plate or lose a tip's length."""
        if self._jaw_may_hold:
            msg = (
                "The gripper is closed and may be holding a plate; a mode switch restarts the connector. "
                "Place the plate (or open the jaw) first."
            )
            raise StepError(msg)
        for mount in ("LEFT", "RIGHT"):
            presence = ""
            with contextlib.suppress(Exception):
                presence = _text(await self._client.tip_presence(PipetteMount[mount]))
            if presence == "PRESENT":
                msg = f"{mount} carries a tip; a mode switch restarts the connector. Drop the tip first."
                raise StepError(msg)

    async def _switch_and_reconnect(self, mode: str) -> None:
        assert self._switcher is not None  # noqa: S101 - checked by the caller
        await self._switcher.switch(mode)
        self.activity = f"Waiting for the {_MODE_NAMES[mode]} connector to answer..."
        await self._wait_for_connector()

    async def _wait_for_connector(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RECONNECT_SECONDS
        while True:
            try:
                await self._client.machine_status()
            except Exception:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(2.0)
            else:
                return

    # -- motion --------------------------------------------------------------------

    async def _positions(self) -> dict[str, dict[str, float]]:
        positions: dict[str, dict[str, float]] = {}
        for mount in Mount:
            if mount is Mount.GRIPPER and not self._gripper_z_known:
                continue
            try:
                positions[mount.value] = await self._client.position(mount)
            except Exception as exc:  # noqa: BLE001 - an unreadable mount is reported as missing
                if mount is Mount.GRIPPER and "POSITION_UNKNOWN" in _error_text(exc):
                    self._gripper_z_known = False
        return positions

    async def _home_gripper_z_if_unknown(self) -> None:
        """Home only the gripper mount (straight up) when the connector no longer knows its height."""
        if self._gripper_z_known:
            try:
                await self._client.position(Mount.GRIPPER)
            except Exception as exc:
                if "POSITION_UNKNOWN" not in _error_text(exc):
                    raise
                self._gripper_z_known = False
        if not self._gripper_z_known:
            await self._client._observable(_MOTION_SERVICE, _MOTION_PACKAGE, "HomeMount", {"mount": Mount.GRIPPER})
            self._gripper_z_known = True

    async def _travel(self, mount: str, x: float, y: float, z: float) -> dict[str, float]:
        """Lift every mount to the travel height, move level, then descend straight down."""
        if mount == "GRIPPER":
            await self._home_gripper_z_if_unknown()
        positions = await self._positions()
        here = positions.get(mount)
        if here is None:
            msg = f"Cannot read the {mount} position; is it attached and homed?"
            raise StepError(msg)
        if abs(here["x"] - x) > _POSITION_TOLERANCE or abs(here["y"] - y) > _POSITION_TOLERANCE:
            for name, position in positions.items():
                if position["z"] < TRAVEL_Z:
                    await self._client.move_to(Mount[name], {**position, "z": TRAVEL_Z})
            # Moving to the exact current point is skipped: at a homed axis it can round below the limit.
            level = max(here["z"], TRAVEL_Z)
            await self._client.move_to(Mount[mount], {"x": x, "y": y, "z": level})
        return await self._client.move_to(Mount[mount], {"x": x, "y": y, "z": z})

    def _refuse_home_while_holding(self) -> None:
        # A full home lifts the gripper first and opens the jaw last, at the top: a held plate would fall.
        if self._jaw_may_hold:
            msg = (
                "The gripper may still be holding a plate. Home and Recover lift the gripper to the top and only "
                "then open the jaw, so the plate would fall. Take the plate out first (switch the robot off if the "
                "jaw will not let go), then add the step 'Gripper is empty' and run Recover."
            )
            raise StepError(msg)

    async def _step_lift_gripper_in_place(self, _params: Mapping[str, Any]) -> str:
        # Homes only the gripper Z (straight up); the jaw keeps the plate. Recover is still needed.
        await self._client._observable(
            _RECOVERY_SERVICE, _RECOVERY_PACKAGE, "LiftGripperInPlace", timeout=90.0, emergency_on_failure=False
        )
        self._gripper_z_known = True
        return "Gripper lifted straight up; the jaw still holds. Support the plate, then Release jaw in place."

    async def _step_release_gripper_in_place(self, _params: Mapping[str, Any]) -> str:
        # Opens only the jaw, where it stopped; Recover is still needed afterwards.
        await self._client._observable(
            _RECOVERY_SERVICE, _RECOVERY_PACKAGE, "ReleaseGripperInPlace", timeout=60.0, emergency_on_failure=False
        )
        return "Jaw opened in place. Take the plate out, add 'Gripper is empty', then run Recover."

    async def _step_gripper_empty(self, _params: Mapping[str, Any]) -> str:
        self._jaw_may_hold = False
        return "Gripper marked empty; Home and Recover are allowed again."

    async def _step_home(self, _params: Mapping[str, Any]) -> str:
        self._refuse_home_while_holding()
        await self._client.home()
        self._gripper_z_known = True
        return "All axes homed."

    async def _step_recover(self, _params: Mapping[str, Any]) -> str:
        self._refuse_home_while_holding()
        steps = await self._client._observable(_RECOVERY_SERVICE, _RECOVERY_PACKAGE, "Recover", timeout=300.0)
        self._gripper_z_known = True
        return "Recovered: " + (", ".join(steps) if steps else "nothing was required")  # type: ignore[arg-type]

    async def _channel_count(self, mount: str) -> int:
        if mount not in self._channels:
            for pipette in await self._client.attached_pipettes():
                self._channels[_text(pipette.mount)] = int(pipette.channels)
        return self._channels.get(mount, 1)

    async def _step_move_to_slot(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"))
        slot = str(params.get("slot", ""))
        x, y = slot_centre(slot)
        z = _number(params, "z", TRAVEL_Z)
        # Centre the nozzles (not the A1 nozzle) over the slot, so a multichannel sits over the labware.
        if mount != "GRIPPER" and await self._channel_count(mount) == 8:
            y += _EIGHT_CHANNEL_CENTRE_Y
        end = await self._travel(mount, x, y, z)
        self._last_well.pop(mount, None)
        return f"{mount} centred over {slot.upper()}, A1 nozzle at ({end['x']:.1f}, {end['y']:.1f}, {end['z']:.1f})."

    async def _step_move_to_tip_well(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        slot = str(params.get("slot", "")).upper()
        well = str(params.get("well", "A1")).upper()
        clearance = _number(params, "clearance", 10.0)
        if clearance < 0:
            msg = "Height above the tip rack must be 0 mm or more; use Move relative to go lower."
            raise StepError(msg)
        location = tip_well_location(slot, well)
        end = await self._travel(mount, location.x, location.y, location.z + clearance)
        return f"{mount} A1 nozzle {clearance:g} mm above {slot} {well} (z {end['z']:.1f})."

    async def _step_move_to(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"))
        end = await self._travel(mount, _number(params, "x"), _number(params, "y"), _number(params, "z"))
        return f"{mount} at ({end['x']:.1f}, {end['y']:.1f}, {end['z']:.1f})."

    async def _step_move_relative(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"))
        if mount == "GRIPPER":
            await self._home_gripper_z_if_unknown()
        here = await self._client.position(Mount[mount])
        dx, dy, dz = (_number(params, key, 0.0) for key in ("dx", "dy", "dz"))
        target = {"x": here["x"] + dx, "y": here["y"] + dy, "z": here["z"] + dz}
        # A sideways jog travels like any other move (lift, level, descend): the tip may be in a well.
        if dx or dy:
            end = await self._travel(mount, target["x"], target["y"], target["z"])
        else:
            end = await self._client.move_to(Mount[mount], target)
        return f"{mount} at ({end['x']:.1f}, {end['y']:.1f}, {end['z']:.1f})."

    # -- tips ----------------------------------------------------------------------

    async def _step_pick_up_tip(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        if "location" in params:
            return await self._pick_up_compiled_tip(mount, params)
        slot = str(params.get("slot", "")).upper()
        slot_centre(slot)
        recorded = self.store.labware_in(slot)
        rack_name = str(params.get("tiprack", "")) or recorded or ""
        rack = TIP_RACKS.get(rack_name)
        if rack is None:
            msg = (
                f"Choose the tip rack in {slot} ({', '.join(TIP_RACKS)}), or record it in Inventory; "
                f"got {rack_name or 'nothing'}."
            )
            raise StepError(msg)
        if recorded is None:
            # A pickup from an empty inventory slot records the rack, so its used tips are tracked from now on.
            self.store.set_labware(slot, rack_name)
        elif recorded != rack_name:
            msg = f"Inventory records {recorded} in {slot}, not {rack_name}. Fix the step or the inventory."
            raise StepError(msg)
        mode = str(params.get("mode", "single"))
        if mode == "single":
            row, column = _well(str(params.get("well", "H1")))
            well = f"{_ROWS[row]}{column}"
            self.racks.check_single(slot, rack_name, well)
            wells = [well]
        elif mode == "column":
            column = int(_number(params, "column", 1))
            if not 1 <= column <= 12:
                msg = f"Tip column {column} is outside 1-12."
                raise StepError(msg)
            self.racks.check_column(slot, rack_name, column)
            well = f"A{column}"
            wells = [f"{row}{column}" for row in _ROWS]
        else:
            msg = f"Pickup mode must be single or column; got {mode!r}."
            raise StepError(msg)
        await self._client.configure_tip_type(PipetteMount[mount], rack.tip_volume)
        if mode == "single":
            await self._client.configure_single_nozzle(PipetteMount[mount], "A1", rack.diameter)
        else:
            await self._client.configure_full_nozzle(PipetteMount[mount], rack.diameter)
        presence = await self._client.pick_up_tip(PipetteMount[mount], tip_well_location(slot, well), rack.tip_length)
        self.racks.take(slot, rack_name, wells)
        self._layout[mount] = "single_A1" if mode == "single" else "full"
        self._tip_contents.pop(mount, None)
        self._last_well.pop(mount, None)
        taken = well if mode == "single" else f"column {wells[0][1:]}"
        return f"{mount} picked up {taken} from {slot}: tip {_text(presence)}."

    async def _step_mark_rack_full(self, params: Mapping[str, Any]) -> str:
        slot = str(params.get("slot", "")).upper()
        slot_centre(slot)
        self.racks.refill(slot)
        return f"Tip rack in {slot} marked full."

    async def _step_drop_tip_trash(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        presence = await self._client.drop_tip(PipetteMount[mount], TipLocation(*TRASH_D1[mount]))
        self._tip_contents.pop(mount, None)
        self._last_well.pop(mount, None)
        return f"{mount} dropped its tips in the D1 trash: tip {_text(presence)}."

    async def _step_tip_presence(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        return f"{mount} tip sensor: {_text(await self._client.tip_presence(PipetteMount[mount]))}."

    # -- gripper -------------------------------------------------------------------

    async def _gripper(self, method: str, parameters: dict[str, Any] | None = None) -> None:
        await self._client._observable(_GRIPPER_SERVICE, _GRIPPER_PACKAGE, method, parameters, timeout=60.0)

    async def _step_move_labware(self, params: Mapping[str, Any]) -> str:
        plan = str(params.get("plan", ""))
        if not plan and params.get("source") and params.get("destination"):
            route = (str(params["source"]), str(params["destination"]))
            matches = [
                p.plan_identifier
                for p in await self._client.available_plans()
                if (p.source_location, p.destination_location) == route and not p.is_lid
            ]
            if len(matches) != 1:
                msg = (
                    f"No single preset gripper plan moves labware {route[0]} -> {route[1]} on this connector "
                    f"(found {matches or 'none'}). Use Pick up plate / Place plate instead."
                )
                raise StepError(msg)
            plan = matches[0]
        if not plan:
            msg = "Choose a labware movement plan."
            raise StepError(msg)
        await self._home_gripper_z_if_unknown()
        try:
            result = await self._client.move_labware(plan)
        except Exception:
            # A failed plate move can stop with the plate still in the jaw.
            self._jaw_may_hold = True
            raise
        return (
            f"{result.labware_identifier} moved to {result.final_location} ({_text(result.state)}, "
            f"jaw {result.jaw_width:.1f} mm)."
        )

    async def _step_pick_plate(self, params: Mapping[str, Any]) -> str:
        """Open, travel above the slot, descend, grip, and lift back to the travel height."""
        slot = str(params.get("slot", "")).upper()
        x, y = slot_centre(slot)
        grip_z = _number(params, "grip_z", DECK_PLATE_GRIP_Z)
        force = _force(params)
        # Opening first fails fast (before any motion) where direct jaw control is disabled.
        await self._gripper("Ungrip", {"force": force})
        await self._travel("GRIPPER", x, y, TRAVEL_Z)
        await self._client.move_to(Mount.GRIPPER, {"x": x, "y": y, "z": grip_z})
        await self._gripper("Grip", {"force": force})
        self._jaw_may_hold = True
        await self._client.move_to(Mount.GRIPPER, {"x": x, "y": y, "z": TRAVEL_Z})
        return f"Gripped the plate in {slot} at z {grip_z:g} mm with {force:g} N and lifted it to {TRAVEL_Z:g} mm."

    async def _step_place_plate(self, params: Mapping[str, Any]) -> str:
        """Travel above the slot holding the plate, descend, release, and lift back to the travel height."""
        slot = str(params.get("slot", "")).upper()
        x, y = slot_centre(slot)
        release_z = _number(params, "grip_z", DECK_PLATE_GRIP_Z)
        force = _force(params)
        await self._travel("GRIPPER", x, y, TRAVEL_Z)
        await self._client.move_to(Mount.GRIPPER, {"x": x, "y": y, "z": release_z})
        await self._gripper("Ungrip", {"force": force})
        self._jaw_may_hold = False
        await self._client.move_to(Mount.GRIPPER, {"x": x, "y": y, "z": TRAVEL_Z})
        return f"Placed the plate in {slot} at z {release_z:g} mm and lifted the gripper to {TRAVEL_Z:g} mm."

    async def _step_gripper_close(self, params: Mapping[str, Any]) -> str:
        force = _force(params)
        await self._gripper("Grip", {"force": force})
        self._jaw_may_hold = True
        return f"Gripper closed at {force:g} N."

    async def _step_gripper_open(self, params: Mapping[str, Any]) -> str:
        force = _force(params)
        await self._gripper("Ungrip", {"force": force})
        self._jaw_may_hold = False
        return "Gripper opened."

    async def _step_gripper_home_jaw(self, _params: Mapping[str, Any]) -> str:
        await self._gripper("HomeJaw")
        self._jaw_may_hold = False
        return "Gripper jaw homed (fully open)."

    async def _step_gripper_home_z(self, _params: Mapping[str, Any]) -> str:
        await self._client._observable(_MOTION_SERVICE, _MOTION_PACKAGE, "HomeMount", {"mount": Mount.GRIPPER})
        self._gripper_z_known = True
        return "Gripper mount homed (straight up)."

    # -- wells, liquids, and compiled protocol steps --------------------------------

    def _target_wells(self, mount: str, slot: str, well: str) -> tuple[list[str], int]:
        """Wells under the active nozzles, and how many nozzles share each (8 over one reservoir trough)."""
        load_name = self.store.labware_in(slot)
        if load_name is None or self._layout.get(mount) != "full":
            return [well], 1
        wells = labware_definition(load_name)["wells"]
        row, column = _well(well) if well[1:].isdigit() else (0, 1)
        under = [f"{_ROWS[r]}{column}" for r in range(row, min(row + 8, 8)) if f"{_ROWS[r]}{column}" in wells]
        return (under, 1) if len(under) > 1 else ([well], 8)

    def _liquid_out(self, mount: str, volume: float, at: Mapping[str, str] | None) -> str:
        where = (at["slot"], at["well"]) if at else self._last_well.get(mount)
        name, held = self._tip_contents.get(mount, ("", 0.0))
        if where is None:
            self._tip_contents[mount] = (name or "untracked", held + volume)
            return ""
        wells, share = self._target_wells(mount, *where)
        names = {self.store.remove_volume(where[0], well, volume * share) for well in wells} - {None}
        self._tip_contents[mount] = (", ".join(sorted(names)) or name or "untracked", held + volume)
        return f" from {where[0]} {where[1]}" + (f" ({len(wells)} wells)" if len(wells) > 1 else "")

    def _liquid_in(self, mount: str, volume: float, at: Mapping[str, str] | None) -> str:
        where = (at["slot"], at["well"]) if at else self._last_well.get(mount)
        name, held = self._tip_contents.get(mount, ("untracked", 0.0))
        self._tip_contents[mount] = (name, max(0.0, held - volume))
        if where is None:
            return ""
        wells, share = self._target_wells(mount, *where)
        for well in wells:
            self.store.add_volume(where[0], well, name, volume * share)
        return f" into {where[0]} {where[1]}" + (f" ({len(wells)} wells)" if len(wells) > 1 else "")

    async def _step_move_to_well(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        slot = str(params.get("slot", "")).upper()
        well = str(params.get("well", "A1")).upper()
        load_name = self.store.labware_in(slot)
        if load_name is None:
            msg = f"No labware is recorded in {slot}. Add it in Inventory first, so the well position is known."
            raise StepError(msg)
        origin = str(params.get("origin", "top"))
        offset = _number(params, "offset", 5.0)
        location = well_location(load_name, slot, well, origin, offset)
        end = await self._travel(mount, location.x, location.y, location.z)
        self._last_well[mount] = (slot, well)
        return f"{mount} at {slot} {well}, {offset:+g} mm from the {origin} (z {end['z']:.1f})."

    async def _step_move(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        target = params.get("target") or {}
        end = await self._travel(mount, _number(target, "x"), _number(target, "y"), _number(target, "z"))
        at = params.get("at")
        if at:
            self._last_well[mount] = (str(at["slot"]), str(at["well"]))
        else:
            self._last_well.pop(mount, None)
        place = f" ({at['slot']} {at['well']})" if at else ""
        return f"{mount} at ({end['x']:.1f}, {end['y']:.1f}, {end['z']:.1f}){place}."

    async def _pick_up_compiled_tip(self, mount: str, params: Mapping[str, Any]) -> str:
        location = params["location"]
        await self._client.pick_up_tip_prepared(
            PipetteMount[mount], TipLocation(location["x"], location["y"], location["z"]), _number(params, "tip_length")
        )
        if params.get("slot") and params.get("wells"):
            self.store.take_tips(str(params["slot"]), [str(well) for well in params["wells"]])
        self._tip_contents.pop(mount, None)
        self._ready.add(mount)
        wells = ", ".join(params.get("wells") or [])
        return f"{mount} picked up {wells or 'tips'} from {params.get('slot', 'the rack')}."

    async def _step_drop_tip(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        location = params.get("location") or {}
        await self._client.drop_tip_and_home_plunger(
            PipetteMount[mount], TipLocation(_number(location, "x"), _number(location, "y"), _number(location, "z"))
        )
        self._tip_contents.pop(mount, None)
        self._last_well.pop(mount, None)
        self._ready.discard(mount)
        return f"{mount} dropped its tips ({params.get('destination', 'drop location')})."

    async def _step_configure_tip_type(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        await self._client.configure_tip_type(PipetteMount[mount], _number(params, "tip_volume"))
        return f"{mount} set for {_number(params, 'tip_volume'):g} uL tips."

    async def _step_configure_nozzles(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        layout = str(params.get("layout", "full"))
        diameter = _number(params, "tiprack_diameter")
        if layout == "full":
            await self._client.configure_full_nozzle(PipetteMount[mount], diameter)
        else:
            await self._client.configure_single_nozzle(PipetteMount[mount], "A1", diameter)
        self._layout[mount] = layout
        return f"{mount} nozzles: {'all 8' if layout == 'full' else 'A1 only'}."

    async def _step_set_flow_rates(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        rates = [_number(params, key) for key in ("aspirate", "dispense", "blow_out")]
        await self._client.set_flow_rates(PipetteMount[mount], *rates)
        return f"{mount} flow rates {rates[0]:g} / {rates[1]:g} / {rates[2]:g} uL/s (aspirate / dispense / blow out)."

    async def _step_prepare_for_aspirate(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        await self._client.prepare_for_aspirate(Mount[mount])
        self._ready.add(mount)
        return f"{mount} plunger ready to aspirate."

    async def _step_aspirate(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        volume = _number(params, "volume")
        if mount not in self._ready:
            msg = (
                f"{mount} plunger is not prepared. Add 'Prepare to aspirate' while the tip is above the liquid "
                "(preparing in the liquid would blow a bubble), then aspirate."
            )
            raise StepError(msg)
        await self._client.aspirate(Mount[mount], volume)
        source = self._liquid_out(mount, volume, params.get("at"))
        return f"{mount} aspirated {volume:g} uL{source}."

    async def _step_dispense(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        volume = _number(params, "volume")
        push_out = params.get("push_out", "default")
        await self._client.dispense(Mount[mount], volume, push_out)
        if push_out != "none":
            self._ready.discard(mount)
        target = self._liquid_in(mount, volume, params.get("at"))
        return f"{mount} dispensed {volume:g} uL{target}."

    async def _step_blow_out(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        await self._client.blow_out(Mount[mount])
        self._ready.discard(mount)
        name, _held = self._tip_contents.get(mount, ("", 0.0))
        self._tip_contents[mount] = (name, 0.0)
        return f"{mount} blew out."

    async def _step_touch_tip(self, params: Mapping[str, Any]) -> str:
        mount = _mount(params.get("mount"), ("LEFT", "RIGHT"))
        await self._client.touch_tip(
            PipetteMount[mount], WellGeometry(**params["well"]), _number(params, "z_offset"), _number(params, "speed")
        )
        return f"{mount} touched the well wall."

    async def _step_set_temperature_and_wait(self, params: Mapping[str, Any]) -> str:
        celsius = _number(params, "celsius")
        self.activity = f"Waiting for the temperature module to reach {celsius:g} C..."
        await self._client.set_temperature_and_wait(celsius)
        return f"Temperature module at {celsius:g} C."

    async def _step_deactivate_temperature(self, _params: Mapping[str, Any]) -> str:
        await self._client.deactivate_temperature()
        return "Temperature module off."

    async def _step_delay(self, params: Mapping[str, Any]) -> str:
        seconds = _number(params, "seconds")
        self.activity = f"Waiting {seconds:g} s..."
        await asyncio.sleep(seconds)
        return f"Waited {seconds:g} s."

    async def _step_comment(self, params: Mapping[str, Any]) -> str:
        return str(params.get("message", ""))

    async def _step_switch_mode(self, params: Mapping[str, Any]) -> str:
        mode = str(params.get("mode", ""))
        if mode not in _MODE_NAMES:
            msg = "Mode must be pe or unit."
            raise StepError(msg)
        note = await self._ensure_mode(mode)
        return note.strip() or f"Already in {_MODE_NAMES[mode]} mode."

    # -- status --------------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """Read machine status, mount positions, tip sensors, and gripper state."""
        status = await self._client.machine_status()
        snapshot: dict[str, Any] = {
            "machine": {
                "estop": status.estop,
                "door_open": status.door_open,
                "error": status.is_error_state,
                "message": status.message,
            },
            "positions": await self._positions(),
            "tips": {},
        }
        for mount in ("LEFT", "RIGHT"):
            with contextlib.suppress(Exception):
                snapshot["tips"][mount] = _text(await self._client.tip_presence(PipetteMount[mount]))
        with contextlib.suppress(Exception):
            gripper = await self._client.gripper_status()
            snapshot["gripper"] = {"attached": gripper.attached, "state": gripper.state}
        with contextlib.suppress(Exception):
            snapshot["simulating"] = await self._client.is_simulating()
        with contextlib.suppress(Exception):
            snapshot["mode"] = await self.mode()
        snapshot["tip_contents"] = {
            mount: {"liquid": name, "volume": round(volume, 3)} for mount, (name, volume) in self._tip_contents.items()
        }
        snapshot["gripper_z_known"] = self._gripper_z_known
        snapshot["holding_plate"] = self._jaw_may_hold
        snapshot["racks"] = self.racks.snapshot()
        return snapshot

    async def info(self) -> dict[str, Any]:
        """Return the movement plans and deck ledger the connector reports, for the page's choices."""
        info: dict[str, Any] = {"plans": [], "deck": None}
        with contextlib.suppress(Exception):
            info["plans"] = [
                {
                    "id": plan.plan_identifier,
                    "labware": plan.labware_identifier,
                    "source": plan.source_location,
                    "destination": plan.destination_location,
                }
                for plan in await self._client.available_plans()
            ]
        if not info["plans"]:
            # connector-pe mode has no preset plans; offer the connector-unit ones, which switch modes on use.
            with contextlib.suppress(Exception):
                info["plans"] = _unit_mode_plans()
        with contextlib.suppress(Exception):
            deck = await self._client.deck_state()
            info["deck"] = {
                "valid": deck.valid,
                "occupied": {item.location_identifier: item.labware_identifier for item in deck.occupied_locations},
            }
        return info


def _unit_mode_plans() -> list[dict[str, str]]:
    """Read the connector-unit preset plans from the repository's movement profile."""
    profile = Path(__file__).resolve().parents[3] / "config" / "asms_unit_labware_movement.json"
    return [
        {
            "id": plan["identifier"],
            "labware": plan["labware_identifier"],
            "source": plan["source"]["identifier"],
            "destination": plan["destination"]["identifier"],
        }
        for plan in json.loads(profile.read_text(encoding="utf-8"))["plans"]
    ]


# Labware the inventory view offers; any other Opentrons load name can be typed in.
LABWARE_CATALOGUE = (
    "opentrons_flex_96_tiprack_50ul",
    "opentrons_flex_96_tiprack_200ul",
    "opentrons_flex_96_tiprack_1000ul",
    "axygen_96_wellplate_500ul",
    "corning_96_wellplate_360ul_flat",
    "nest_96_wellplate_200ul_flat",
    "nest_96_wellplate_2ml_deep",
    "nest_12_reservoir_15ml",
    "nest_1_reservoir_195ml",
    "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
    "opentrons_96_wellplate_200ul_pcr_full_skirt",
)


async def import_protocol(source: bytes, filename: str, workdir: Path) -> dict[str, Any]:
    """
    Analyse an Opentrons Flex Python protocol and compile it into editable console steps.

    Opentrons' own analysis (``opentrons analyze``) supplies every coordinate, flow rate, and
    tip length. The compiler refuses commands it has no SiLA mapping for instead of dropping
    them, so an imported list is either complete or not produced at all.
    """
    await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
    name = Path(filename).name or "protocol.py"
    if not name.endswith(".py"):
        msg = "Upload an Opentrons Python protocol (.py)."
        raise StepError(msg)
    protocol = workdir / name
    await asyncio.to_thread(protocol.write_bytes, source)
    analysis = await asyncio.to_thread(analyze, protocol, [], workdir / f"{protocol.stem}.analysis.json")
    commands = analysis["commands"]
    labware_slots: dict[str, str] = {}
    module_slots: dict[str, str] = {}
    deck: dict[str, str] = {}
    for command in commands:
        params, result = command.get("params", {}), command.get("result") or {}
        if command["commandType"] == "loadModule":
            module_slots[result["moduleId"]] = params["location"]["slotName"]
        elif command["commandType"] == "loadLabware":
            location = params["location"]
            slot = location.get("slotName") or module_slots.get(location.get("moduleId", "")) or ""
            if slot:
                labware_slots[result["labwareId"]] = slot
                deck[slot] = params["loadName"]
    liquid_names = {item["id"]: item.get("displayName") or item["id"] for item in analysis.get("liquids", [])}
    liquids = [
        {
            "slot": labware_slots[params["labwareId"]],
            "well": well,
            "liquid": liquid_names.get(params["liquidId"], params["liquidId"]),
            "volume": volume,
        }
        for command in commands
        if command["commandType"] == "loadLiquid"
        and (params := command.get("params", {}))["labwareId"] in labware_slots
        for well, volume in params["volumeByWell"].items()
    ]
    plan = await compile_plan(analysis, dict(deck))
    return {
        "protocol": name,
        "pipettes": plan["pipettes"],
        "steps": [{"kind": step.pop("kind"), "params": step} for step in plan["steps"]],
        "inventory": {"labware": deck, "liquids": liquids},
    }


def _text(value: object) -> str:
    return value.value if isinstance(value, enum.Enum) else str(value)


def _error_text(exc: BaseException) -> str:
    """Return the SiLA error message, decoded when the connector sent a defined execution error."""
    details = getattr(exc, "details", None)
    if callable(details):
        import base64

        with contextlib.suppress(Exception):
            raw = base64.b64decode(details() or "")
            printable = "".join(ch if ch in string.printable else " " for ch in raw.decode("utf-8", "replace"))
            return " ".join(printable.split())[-600:]
    return f"{type(exc).__name__}: {exc}"


def _explain(error: str) -> str:
    """Add the operator action for connector errors whose fix is not in the message itself."""
    if "DirectGripperControlDisabledError" in error:
        return (
            error + " — connector-unit mode only moves the gripper along the preset plans (use 'Move labware'). "
            "To open and close the jaw directly, switch to connector-pe mode: flexctl mode <robot-ip> pe "
            "(the console does this itself when started with --robot <robot-ip>)."
        )
    if "RecoveryRequiredError" in error or "recovery-gated" in error:
        return error + " — run Recover (or Home) first."
    return error


class Console:
    """One SiLA client, one running step at a time, and a Stop that cancels it."""

    def __init__(self, runner: StepRunner) -> None:
        self._runner = runner
        self._task: asyncio.Task[str] | None = None
        self._lock = asyncio.Lock()

    async def run_step(self, kind: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Run one step unless another is running, and report its result for the page."""
        if self._lock.locked():
            return {"ok": False, "error": "Another step is still running."}
        async with self._lock:
            self._task = asyncio.create_task(self._runner.run(kind, params))
            try:
                message = await self._task
            except asyncio.CancelledError:
                return {
                    "ok": False,
                    "stopped": True,
                    "error": "Stopped. Any running motion was halted (EmergencyStop); run Recover or Home. "
                    "A mode switch in progress was allowed to finish.",
                }
            except StepError as exc:
                return self._record(kind, params, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - every connector error is shown to the operator
                return self._record(kind, params, {"ok": False, "error": _explain(_error_text(exc))})
            finally:
                self._task = None
            return self._record(kind, params, {"ok": True, "message": message})

    def _record(self, kind: str, params: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            self._runner.store.log(kind, params, bool(result["ok"]), result.get("message") or result.get("error", ""))
        return result

    @property
    def store(self) -> ConsoleStore:
        """The database this console records inventory, tips, workflows, and history in."""
        return self._runner.store

    def stop(self) -> bool:
        """Cancel the running step; the client then sends EmergencyStop to the connector."""
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        return True

    async def info(self) -> dict[str, Any]:
        """Report the movement plans and deck ledger, unless a step is running."""
        if self._lock.locked():
            return {"busy": True}
        return await self._runner.info()

    async def state(self) -> dict[str, Any]:
        """Report the robot state, or that a step is running."""
        if self._lock.locked():
            return {"busy": True, "activity": self._runner.activity}
        try:
            return {"busy": False, **(await self._runner.snapshot())}
        except Exception as exc:  # noqa: BLE001 - shown as a connection problem on the page
            return {"busy": False, "error": _error_text(exc)}


def build_web_app(console: Console) -> web.Application:
    from aiohttp import web

    page = (Path(__file__).with_name("step_console.html")).read_text(encoding="utf-8")

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

    async def state(_request: web.Request) -> web.Response:
        return web.json_response(await console.state())

    async def step(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(await console.run_step(str(body.get("kind", "")), body.get("params") or {}))

    async def stop(_request: web.Request) -> web.Response:
        return web.json_response({"stopped": console.stop()})

    async def info(_request: web.Request) -> web.Response:
        return web.json_response(await console.info())

    async def inventory(_request: web.Request) -> web.Response:
        store = console.store
        return web.json_response(
            {**store.snapshot(), "catalogue": list(LABWARE_CATALOGUE), "history": store.history(30)}
        )

    async def inventory_update(request: web.Request) -> web.Response:
        body = await request.json()
        store = console.store
        action = body.get("action")
        try:
            if action == "set_labware":
                store.set_labware(str(body["slot"]).upper(), str(body["load_name"]), str(body.get("label", "")))
            elif action == "remove_labware":
                store.remove_labware(str(body["slot"]).upper())
            elif action == "refill_tips":
                store.refill_tips(str(body["slot"]).upper())
            elif action == "set_liquid":
                store.set_liquid(
                    str(body["slot"]).upper(), str(body["well"]).upper(), str(body["liquid"]), float(body["volume"])
                )
            elif action == "apply_protocol":
                for slot, load_name in (body.get("labware") or {}).items():
                    store.set_labware(slot, load_name)
                for item in body.get("liquids") or []:
                    store.set_liquid(item["slot"], item["well"], item["liquid"], float(item["volume"]))
            else:
                return web.json_response({"ok": False, "error": f"Unknown inventory action {action!r}."})
        except Exception as exc:  # noqa: BLE001 - a bad entry is reported, not raised
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return web.json_response({"ok": True, **store.snapshot()})

    async def workflows(request: web.Request) -> web.Response:
        store = console.store
        if request.method == "POST":
            body = await request.json()
            name = str(body.get("name", "")).strip()
            if not name:
                return web.json_response({"ok": False, "error": "Give the workflow a name."})
            store.save_workflow(name, body.get("steps") or [], str(body.get("source", "")))
        elif request.method == "DELETE":
            store.delete_workflow(request.query.get("name", ""))
        elif request.query.get("name"):
            steps = store.workflow(request.query["name"])
            return web.json_response({"ok": steps is not None, "steps": steps or []})
        return web.json_response({"ok": True, "workflows": store.workflows()})

    async def import_(request: web.Request) -> web.Response:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "protocol":
            return web.json_response({"ok": False, "error": "Attach the protocol file as 'protocol'."})
        source = await field.read()
        try:
            result = await import_protocol(bytes(source), field.filename or "protocol.py", workdir)
        except Exception as exc:  # noqa: BLE001 - analysis and compile errors are shown to the operator
            return web.json_response({"ok": False, "error": str(exc)})
        return web.json_response({"ok": True, **result})

    workdir = (
        console.store.path.parent / "imports"
        if str(console.store.path) != ":memory:"
        else Path(tempfile.mkdtemp(prefix="flex-console-"))
    )
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/state", state),
            web.get("/api/info", info),
            web.post("/api/step", step),
            web.post("/api/stop", stop),
            web.get("/api/inventory", inventory),
            web.post("/api/inventory", inventory_update),
            web.get("/api/workflows", workflows),
            web.post("/api/workflows", workflows),
            web.delete("/api/workflows", workflows),
            web.post("/api/import", import_),
        ]
    )
    return app


async def serve(sila_address: str, host: str, port: int, robot_host: str | None, db: Path) -> None:
    from aiohttp import web

    async with connect(sila_address) as client:
        switcher = ModeSwitcher(robot_host) if robot_host else None
        step_runner = StepRunner(client, switcher, ConsoleStore(db))
        app = build_web_app(Console(step_runner))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        sys.stdout.write(f"Step console on http://{host}:{port}  (SiLA {sila_address})\n")
        sys.stdout.flush()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[1], formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--sila", default="127.0.0.1:50052", help="connector SiLA gRPC address (host:port)")
    parser.add_argument("--host", default="127.0.0.1", help="address the web page listens on")
    parser.add_argument("--port", type=int, default=8765, help="port the web page listens on")
    parser.add_argument(
        "--robot",
        help="robot IP; lets the console switch between connector-pe and connector-unit mode when a step needs it",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_PATH, help="console database (inventory, tips, workflows)")
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.sila, args.host, args.port, args.robot, args.db))


if __name__ == "__main__":
    main()


__all__ = [
    "TIP_RACKS",
    "TRASH_D1",
    "TRAVEL_Z",
    "Console",
    "StepError",
    "StepRunner",
    "TipRackTracker",
    "slot_centre",
    "tip_well_location",
]
