"""
Run the AS-MS Opentrons protocols on a Flex through the connector's SiLA gRPC surface only.

Nothing is uploaded to the robot and no Protocol Engine run is created. Instead:

1. ``analyze`` runs Opentrons' own protocol analysis offline, so every absolute
   position, flow rate, tip length, delay, and gripper move comes from the same
   Protocol Engine that would run the protocol natively.
2. ``compile_plan`` turns that command list into a reviewable list of SiLA steps.
   It fails closed on any command it does not understand, mirrors Protocol
   Engine's plunger-readiness rules (prepare in air at the well top), converts
   trash-drop targets from the instrument-centre critical point to the A1 tip,
   and checks every partial-column tip pickup against the tracked tip racks.
3. ``execute`` sends those steps one SiLA command at a time over gRPC. Every move
   between wells lifts to the top of travel, moves level, and descends vertically.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import grpc.aio

from .asms_unit_operations import (
    _LIQUID_PACKAGE,
    _LIQUID_SERVICE,
    _MOTION_PACKAGE,
    _MOTION_SERVICE,
    _PIPETTE_PACKAGE,
    _PIPETTE_SERVICE,
    _TEMPERATURE_PACKAGE,
    _TEMPERATURE_SERVICE,
    _TIP_PACKAGE,
    _TIP_SERVICE,
    GrpcUnitClient,
    UnitOperationError,
)
from .features import PipetteMount, TipLocation, WellGeometry
from .features.motion_control import Mount

PLAN_SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_DIR = ROOT / "protocols" / "asms"
RUNS_FILE = PROTOCOL_DIR / "sila" / "asms_sila_runs.json"

# Commands that only declare things to the analysis; they move nothing.
_DECLARATIONS = frozenset({"loadLabware", "loadModule", "loadPipette", "loadLiquid", "loadLiquidClass", "comment"})
_POSITION_TOLERANCE = 0.01


class AsmsWorkflowError(UnitOperationError):
    """The protocol cannot be compiled or run safely; nothing further is executed."""


# -- analysis --------------------------------------------------------------------------


def analyze(protocol: Path, labware: Sequence[Path], output: Path) -> dict[str, Any]:
    """Run ``opentrons analyze`` in a separate process and return its JSON analysis."""
    command = [
        sys.executable,
        "-m",
        "opentrons.cli",
        "analyze",
        "--json-output",
        str(output),
        str(protocol),
        *(str(path) for path in labware),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    if completed.returncode != 0 or not output.exists():
        msg = f"Opentrons analysis of {protocol.name} failed: {completed.stderr.strip() or completed.stdout.strip()}"
        raise AsmsWorkflowError(msg)
    analysis = json.loads(output.read_text(encoding="utf-8"))
    if analysis.get("errors"):
        details = "; ".join(str(error.get("detail")) for error in analysis["errors"])
        msg = f"Opentrons analysis of {protocol.name} reported errors: {details}"
        raise AsmsWorkflowError(msg)
    return analysis


# -- compilation -----------------------------------------------------------------------


def _hardware_model(pipette_name: str) -> str:
    """Map a Protocol API name such as ``p1000_multi_flex`` to a simulator model name."""
    volume, channels, robot = pipette_name.split("_")
    if robot != "flex":
        msg = f"Pipette {pipette_name!r} is not a Flex pipette."
        raise AsmsWorkflowError(msg)
    return f"{volume}_{channels}_v3.5"


async def instrument_centre_offsets(pipette_names: Mapping[str, str]) -> dict[str, tuple[float, float, float]]:
    """
    Return, per mount, the A1-tip position minus the instrument-centre position.

    Protocol Engine positions trash drops with ``CriticalPoint.INSTRUMENT_XY_CENTER``;
    SiLA DropTip positions the A1 tip, so trash targets are shifted by this offset.
    """
    from opentrons.hardware_control.ot3api import OT3API
    from opentrons.hardware_control.types import CriticalPoint, OT3Mount

    mounts = {"LEFT": OT3Mount.LEFT, "RIGHT": OT3Mount.RIGHT}
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            mounts[mount]: {"model": _hardware_model(name), "id": f"offset-{mount}"}
            for mount, name in pipette_names.items()
        }
    )
    try:
        offsets: dict[str, tuple[float, float, float]] = {}
        for mount in pipette_names:
            tip = api.critical_point_for(mounts[mount], None)
            centre = api.critical_point_for(mounts[mount], CriticalPoint.INSTRUMENT_XY_CENTER)
            offsets[mount] = (round(tip.x - centre.x, 4), round(tip.y - centre.y, 4), round(tip.z - centre.z, 4))
        return offsets
    finally:
        await api.clean_up()


def _point(value: Mapping[str, float], shift: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> dict[str, float]:
    return {axis: round(float(value[axis]) + delta, 4) for axis, delta in zip("xyz", shift, strict=True)}


@dataclass
class _Pipette:
    mount: str
    name: str
    channels: int
    has_tip: bool = False
    ready: bool = False
    volume: float = 0.0
    tip_volume: float | None = None
    layout: tuple[str, float] | None = None
    flow: dict[str, float | None] = field(
        default_factory=lambda: {"aspirate": None, "dispense": None, "blow_out": None}
    )
    tip_rack: str | None = None
    tip_wells: tuple[str, ...] = ()


@dataclass
class _Labware:
    load_name: str
    definition: Mapping[str, Any]
    location: str
    is_tiprack: bool
    tips: set[str] = field(default_factory=set)


class _Compiler:
    def __init__(self, analysis: Mapping[str, Any], labware_identifiers: Mapping[str, str], centre: Mapping[str, Any]):
        self.analysis = analysis
        self.identifiers = dict(labware_identifiers)
        self.centre = centre
        self.steps: list[dict[str, Any]] = []
        self.pipettes: dict[str, _Pipette] = {}
        self.labware: dict[str, _Labware] = {}
        self.module_slots: dict[str, str] = {}
        self.module_models: dict[str, str] = {}
        self.ledger_ids: dict[str, str] = {}
        self.index = -1
        self.target_temperature: float | None = None
        self.pending_trash: tuple[str, dict[str, float]] | None = None

    def emit(self, kind: str, **values: object) -> None:
        self.steps.append({"seq": len(self.steps), "source": self.index, "kind": kind, **values})

    def fail(self, message: str) -> None:
        command = self.analysis["commands"][self.index]
        msg = f"Protocol command {self.index} ({command['commandType']}): {message}"
        raise AsmsWorkflowError(msg)

    # -- helpers ------------------------------------------------------------------------

    def pipette(self, params: Mapping[str, Any]) -> _Pipette:
        return self.pipettes[params["pipetteId"]]

    def location_name(self, location: Mapping[str, Any]) -> str:
        if "slotName" in location:
            return str(location["slotName"])
        if "moduleId" in location:
            return self.module_slots[location["moduleId"]]
        if "addressableAreaName" in location:
            return str(location["addressableAreaName"])
        self.fail(f"unsupported labware location {location}")
        raise AssertionError  # pragma: no cover

    def well_top(self, params: Mapping[str, Any], position: Mapping[str, float]) -> float:
        location = params.get("wellLocation") or {"origin": "top", "offset": {"z": 0.0}}
        offset = float(location.get("offset", {}).get("z", 0.0))
        well = self.labware[params["labwareId"]].definition["wells"][params["wellName"]]
        if location.get("origin") == "top":
            return float(position["z"]) - offset
        if location.get("origin") == "bottom":
            return float(position["z"]) - offset + float(well["depth"])
        self.fail(f"well origin {location.get('origin')!r} is not supported")
        raise AssertionError  # pragma: no cover

    def set_flow(self, pipette: _Pipette, channel: str, rate: float) -> None:
        if pipette.flow[channel] == rate:
            return
        pipette.flow[channel] = rate
        values = {name: value if value is not None else rate for name, value in pipette.flow.items()}
        pipette.flow = dict(values)
        self.emit("set_flow_rates", mount=pipette.mount, **values)

    def move(self, pipette: _Pipette, position: Mapping[str, float]) -> None:
        self.emit("move", mount=pipette.mount, target=_point(position))

    # -- command handlers ---------------------------------------------------------------

    def compile(self) -> list[dict[str, Any]]:
        for index, command in enumerate(self.analysis["commands"]):
            self.index = index
            kind = command["commandType"]
            params = command.get("params", {})
            result = command.get("result") or {}
            if command.get("status") not in (None, "succeeded"):
                self.fail(f"analysis status is {command.get('status')}")
            handler = getattr(self, "on_" + kind.replace("/", "_"), None)
            if handler is None:
                if kind in _DECLARATIONS:
                    self.declare(kind, params, result)
                    continue
                self.fail("this command type has no SiLA mapping; refusing to run a partial protocol")
            handler(params, result)
        for pipette in self.pipettes.values():
            if pipette.has_tip:
                self.fail(f"the protocol ends with a tip on {pipette.mount}")
        return self.steps

    def declare(self, kind: str, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        if kind == "loadPipette":
            name = str(params["pipetteName"])
            channels = 8 if "_multi_" in name else 1 if "_single_" in name else 96
            if channels == 96:
                self.fail("96-channel pipettes are not supported")
            mount = str(params["mount"]).upper()
            self.pipettes[result["pipetteId"]] = _Pipette(mount, name, channels)
        elif kind == "loadModule":
            self.module_slots[result["moduleId"]] = str(params["location"]["slotName"])
            self.module_models[result["moduleId"]] = str(params["model"])
        elif kind == "loadLabware":
            definition = result["definition"]
            location = self.location_name(params["location"])
            is_tiprack = bool(definition["parameters"]["isTiprack"])
            labware = _Labware(str(params["loadName"]), definition, location, is_tiprack)
            if is_tiprack:
                labware.tips = set(definition["wells"])
            self.labware[result["labwareId"]] = labware
            if location in self.identifiers and "slotName" in params["location"]:
                self.ledger_ids[result["labwareId"]] = self.identifiers[location]
        elif kind == "comment":
            self.emit("comment", message=str(params["message"]))

    def on_home(self, _params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        self.emit("home")

    def on_temperatureModule_setTargetTemperature(self, params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        self.target_temperature = float(params["celsius"])

    def on_temperatureModule_waitForTemperature(self, _params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        if self.target_temperature is None:
            self.fail("waitForTemperature without a target temperature")
        self.emit("set_temperature_and_wait", celsius=self.target_temperature)

    def on_temperatureModule_deactivate(self, _params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        self.target_temperature = None
        self.emit("deactivate_temperature")

    def on_waitForDuration(self, params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        self.emit("delay", seconds=float(params["seconds"]))

    def on_pickUpTip(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        if pipette.has_tip:
            self.fail(f"{pipette.mount} already holds a tip")
        rack = self.labware[params["labwareId"]]
        well = str(params["wellName"])
        row, column = well[0], well[1:]
        rows = "ABCDEFGH"
        if pipette.channels == 1 or row != "A":
            # One tip on the A1 nozzle: the other nozzles hang over the rows in front of it,
            # which must already be empty (or off the rack) so they cannot jam a tip.
            layout = "single_A1"
            below = [f"{r}{column}" for r in rows[rows.index(row) + 1 :]] if pipette.channels == 8 else []
            blocked = sorted(name for name in below if name in rack.tips)
            if blocked:
                self.fail(f"picking {well} with the A1 nozzle would press nozzles onto tips {', '.join(blocked)}")
            taken = (well,)
        else:
            layout = "full"
            taken = tuple(f"{r}{column}" for r in rows)
        missing = sorted(name for name in taken if name not in rack.tips)
        if missing:
            self.fail(f"tips {', '.join(missing)} in {rack.load_name} at {rack.location} are already used")
        tip_volume = float(result["tipVolume"])
        diameter = float(result["tipDiameter"])
        if pipette.tip_volume != tip_volume:
            self.emit("configure_tip_type", mount=pipette.mount, tip_volume=tip_volume)
            pipette.tip_volume = tip_volume
            pipette.flow = {"aspirate": None, "dispense": None, "blow_out": None}
        if pipette.layout != (layout, diameter):
            self.emit("configure_nozzles", mount=pipette.mount, layout=layout, tiprack_diameter=diameter)
            pipette.layout = (layout, diameter)
        self.emit(
            "pick_up_tip",
            mount=pipette.mount,
            location=_point(result["position"]),
            tip_length=round(float(result["tipLength"]), 4),
            labware=rack.load_name,
            slot=rack.location,
            wells=list(taken),
        )
        rack.tips -= set(taken)
        pipette.has_tip, pipette.ready, pipette.volume = True, True, 0.0
        pipette.tip_rack, pipette.tip_wells = params["labwareId"], taken

    def on_moveToAddressableAreaForDropTip(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        if not params.get("ignoreTipConfiguration", True):
            self.fail("only instrument-centred trash drops are supported")
        self.pending_trash = (pipette.mount, _point(result["position"], self.centre[pipette.mount]))

    def on_dropTipInPlace(self, params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        pending = self.pending_trash
        if pending is None or pending[0] != pipette.mount:
            self.fail("dropTipInPlace without a preceding trash move")
        self.emit("drop_tip", mount=pipette.mount, location=pending[1], destination="trash")
        self.pending_trash = None
        self.dropped(pipette)

    def on_dropTip(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        rack = self.labware.get(params["labwareId"])
        if rack is None or not rack.is_tiprack:
            self.fail("tips can only be returned to a tip rack")
        well = str(params["wellName"])
        returned = (
            tuple(f"{r}{well[1:]}" for r in "ABCDEFGH") if pipette.layout and pipette.layout[0] == "full" else (well,)
        )
        occupied = sorted(name for name in returned if name in rack.tips)
        if occupied:
            self.fail(f"returning tips onto occupied positions {', '.join(occupied)}")
        self.emit(
            "drop_tip",
            mount=pipette.mount,
            location=_point(result["position"]),
            destination=f"{rack.load_name}@{rack.location}:{well}",
        )
        rack.tips |= set(returned)
        self.dropped(pipette)

    def dropped(self, pipette: _Pipette) -> None:
        if not pipette.has_tip:
            self.fail(f"{pipette.mount} has no tip to drop")
        pipette.has_tip, pipette.ready, pipette.volume = False, False, 0.0

    def on_moveToWell(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        self.move(self.pipette(params), result["position"])

    def on_prepareToAspirate(self, params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        self.emit("prepare_for_aspirate", mount=pipette.mount)
        pipette.ready = True

    def on_aspirate(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        if not pipette.has_tip:
            self.fail("aspirate without a tip")
        position = result["position"]
        if not pipette.ready:
            # Protocol Engine prepares the plunger at the well top, in air, before descending.
            self.move(pipette, {"x": position["x"], "y": position["y"], "z": self.well_top(params, position)})
            self.emit("prepare_for_aspirate", mount=pipette.mount)
            pipette.ready = True
        self.set_flow(pipette, "aspirate", float(params["flowRate"]))
        self.move(pipette, position)
        self.emit("aspirate", mount=pipette.mount, volume=float(params["volume"]))
        pipette.volume += float(params["volume"])

    def on_airGapInPlace(self, params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        if not pipette.ready:
            self.fail("air gap while the plunger is not ready")
        self.set_flow(pipette, "aspirate", float(params["flowRate"]))
        self.emit("aspirate", mount=pipette.mount, volume=float(params["volume"]), air_gap=True)
        pipette.volume += float(params["volume"])

    def on_dispense(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        volume = float(params["volume"])
        if volume > pipette.volume + 1e-6:
            self.fail(f"dispensing {volume:g} uL with only {pipette.volume:g} uL in the tip")
        push_out = params.get("pushOut")
        self.set_flow(pipette, "dispense", float(params["flowRate"]))
        self.move(pipette, result["position"])
        full = abs(volume - pipette.volume) < 1e-6
        if push_out is not None and float(push_out) == 0.0:
            self.emit("dispense", mount=pipette.mount, volume=volume, push_out="none")
        else:
            self.emit(
                "dispense", mount=pipette.mount, volume=volume, push_out="default" if push_out is None else push_out
            )
            if full:
                pipette.ready = False
        pipette.volume = max(0.0, pipette.volume - volume)

    def on_blowout(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        self.set_flow(pipette, "blow_out", float(params["flowRate"]))
        self.move(pipette, result["position"])
        self.emit("blow_out", mount=pipette.mount)
        pipette.ready, pipette.volume = False, 0.0

    def on_blowOutInPlace(self, params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        self.set_flow(pipette, "blow_out", float(params["flowRate"]))
        self.emit("blow_out", mount=pipette.mount)
        pipette.ready, pipette.volume = False, 0.0

    def on_touchTip(self, params: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        pipette = self.pipette(params)
        if params.get("mmFromEdge") is not None:
            self.fail("touchTip mmFromEdge is not supported")
        position = result["position"]
        well = self.labware[params["labwareId"]].definition["wells"][params["wellName"]]
        if well["shape"] == "circular":
            size_x = size_y = float(well["diameter"])
        else:
            size_x, size_y = float(well["xDimension"]), float(well["yDimension"])
        radius = float(params.get("radius", 1.0))
        top = self.well_top(params, position)
        self.move(pipette, position)
        self.emit(
            "touch_tip",
            mount=pipette.mount,
            well={
                "center_x": round(float(position["x"]), 4),
                "center_y": round(float(position["y"]), 4),
                "bottom_z": round(top - float(well["depth"]), 4),
                "top_z": round(top, 4),
                "size_x": round(size_x * radius, 4),
                "size_y": round(size_y * radius, 4),
            },
            z_offset=round(float(position["z"]) - top, 4),
            speed=float(params["speed"]),
        )

    def on_moveLabware(self, params: Mapping[str, Any], _result: Mapping[str, Any]) -> None:
        if params.get("strategy") != "usingGripper":
            self.fail("only gripper labware moves can run without an operator")
        if any(pipette.has_tip for pipette in self.pipettes.values()):
            self.fail("a labware move while a pipette holds a tip")
        labware_id = params["labwareId"]
        if labware_id not in self.ledger_ids:
            labware = self.labware[labware_id]
            self.fail(f"{labware.load_name} at {labware.location} has no deck-ledger identifier in the run spec")
        labware = self.labware[labware_id]
        destination = self.location_name(params["newLocation"])
        self.emit(
            "move_labware",
            labware=self.ledger_ids[labware_id],
            source=labware.location,
            destination=destination,
        )
        labware.location = destination


async def compile_plan(analysis: Mapping[str, Any], labware_identifiers: Mapping[str, str]) -> dict[str, Any]:
    """Compile a Protocol Engine analysis into the SiLA step plan ``execute`` runs."""
    pipettes = {str(p["mount"]).upper(): str(p["pipetteName"]) for p in analysis["pipettes"]}
    centre = await instrument_centre_offsets(pipettes)
    compiler = _Compiler(analysis, labware_identifiers, centre)
    steps = compiler.compile()
    initial_ledger: dict[str, str] = {}
    for step in steps:
        if step["kind"] == "move_labware" and step["labware"] not in initial_ledger.values():
            initial_ledger[step["source"]] = step["labware"]
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "pipettes": pipettes,
        "uses_temperature_module": any(s["kind"] == "set_temperature_and_wait" for s in steps),
        "initial_ledger": initial_ledger,
        "instrument_centre_offsets": {mount: list(offset) for mount, offset in centre.items()},
        "steps": steps,
    }


# -- gRPC client ----------------------------------------------------------------------


class AsmsSilaClient(GrpcUnitClient):
    """The SiLA commands the AS-MS replay needs, on top of the unit-operation client."""

    async def move_to(self, mount: Mount, point: Mapping[str, float]) -> dict[str, float]:
        position = await self._observable(
            _MOTION_SERVICE,
            _MOTION_PACKAGE,
            "MoveTo",
            {"mount": mount, "x": point["x"], "y": point["y"], "z": point["z"], "speed": 0.0},
        )
        return {"x": position.x, "y": position.y, "z": position.z}

    async def position(self, mount: Mount) -> dict[str, float]:
        position = await self._observable(
            _MOTION_SERVICE, _MOTION_PACKAGE, "GetPosition", {"mount": mount}, emergency_on_failure=False
        )
        return {"x": position.x, "y": position.y, "z": position.z}

    async def prepare_for_aspirate(self, mount: Mount) -> None:
        await self._observable(_MOTION_SERVICE, _MOTION_PACKAGE, "PrepareForAspirate", {"mount": mount})

    async def aspirate(self, mount: Mount, volume: float) -> None:
        await self._observable(
            _MOTION_SERVICE, _MOTION_PACKAGE, "Aspirate", {"mount": mount, "volume": volume, "rate": 1.0}
        )

    async def dispense(self, mount: Mount, volume: float, push_out: object) -> None:
        if push_out == "none":
            await self._observable(
                _MOTION_SERVICE,
                _MOTION_PACKAGE,
                "DispenseWithoutPushOut",
                {"mount": mount, "volume": volume, "rate": 1.0},
            )
            return
        value = 0.0 if push_out == "default" else float(push_out)
        await self._observable(
            _MOTION_SERVICE,
            _MOTION_PACKAGE,
            "Dispense",
            {"mount": mount, "volume": volume, "rate": 1.0, "push_out": value},
        )

    async def blow_out(self, mount: Mount) -> None:
        await self._observable(_MOTION_SERVICE, _MOTION_PACKAGE, "BlowOut", {"mount": mount})

    async def configure_single_nozzle(self, mount: PipetteMount, nozzle: str, tiprack_diameter: float) -> object:
        return await self._command(
            _PIPETTE_SERVICE,
            _PIPETTE_PACKAGE,
            "ConfigureSingleNozzleLayout",
            {"mount": mount, "nozzle": nozzle, "tiprack_diameter": tiprack_diameter},
        )

    async def configure_tip_type(self, mount: PipetteMount, tip_volume: float) -> object:
        return await self._command(
            _PIPETTE_SERVICE, _PIPETTE_PACKAGE, "ConfigureTipType", {"mount": mount, "tip_volume": tip_volume}
        )

    async def set_flow_rates(self, mount: PipetteMount, aspirate: float, dispense: float, blow_out: float) -> object:
        return await self._command(
            _PIPETTE_SERVICE,
            _PIPETTE_PACKAGE,
            "SetFlowRates",
            {
                "mount": mount,
                "aspirate_flow_rate": aspirate,
                "dispense_flow_rate": dispense,
                "blow_out_flow_rate": blow_out,
            },
        )

    async def pick_up_tip_prepared(self, mount: PipetteMount, location: TipLocation, tip_length: float) -> object:
        return await self._observable(
            _TIP_SERVICE,
            _TIP_PACKAGE,
            "PickUpTip",
            {"mount": mount, "location": location, "tip_length": tip_length, "prep_after": True},
        )

    async def drop_tip_and_home_plunger(self, mount: PipetteMount, location: TipLocation) -> object:
        return await self._observable(
            _TIP_SERVICE, _TIP_PACKAGE, "DropTip", {"mount": mount, "location": location, "home_after": True}
        )

    async def touch_tip(self, mount: PipetteMount, well: WellGeometry, z_offset: float, speed: float) -> object:
        return await self._observable(
            _LIQUID_SERVICE,
            _LIQUID_PACKAGE,
            "TouchTip",
            {"mount": mount, "well": well, "z_offset": z_offset, "distance_from_edge": 0.0, "speed": speed},
        )

    async def set_temperature_and_wait(self, celsius: float) -> object:
        return await self._observable(
            _TEMPERATURE_SERVICE,
            _TEMPERATURE_PACKAGE,
            "SetTemperatureAndWait",
            {"temperature": celsius},
            timeout=1800.0,
        )

    async def set_lights(self, on: bool) -> None:
        await self._command(_MOTION_SERVICE, _MOTION_PACKAGE, "SetLights", {"button": on, "rails": on})


@contextlib.asynccontextmanager
async def connect(address: str) -> AsyncIterator[AsmsSilaClient]:
    """Connect to a live connector; the protobuf codec is built from a local simulator app."""
    from unitelabs.cdk import SiLAServerConfig

    from . import OpentronsFlexConfig, create_app

    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_gripper=True,
        simulated_temperature_module=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    application = create_app(config)
    codec_connector = await application.__anext__()
    channel = grpc.aio.insecure_channel(address)
    try:
        yield AsmsSilaClient(channel, codec_connector.sila_server.protobuf)
    finally:
        await channel.close()
        with contextlib.suppress(StopAsyncIteration):
            await application.__anext__()


# -- execution -------------------------------------------------------------------------


def _enum(value: object) -> object:
    return getattr(value, "value", value)


async def preflight(client: AsmsSilaClient, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Check robot state against the plan without moving anything; return the plan ids to use."""
    status = await client.machine_status()
    if status.is_error_state or status.estop != "DISENGAGED" or status.door_open:
        msg = "Flex is not ready: clear errors, disengage the E-stop, and close the door."
        raise AsmsWorkflowError(msg)
    attached = {str(_enum(p.mount)): p for p in await client.attached_pipettes()}
    for mount, name in plan["pipettes"].items():
        pipette = attached.get(mount)
        if pipette is None or not pipette.attached:
            msg = f"The plan needs {name} on {mount}, but no pipette is attached there."
            raise AsmsWorkflowError(msg)
        if name not in {pipette.name, pipette.model} and _hardware_model(name).rsplit("_v", 1)[0] not in pipette.model:
            msg = f"The plan needs {name} on {mount}, but {pipette.name or pipette.model} is attached."
            raise AsmsWorkflowError(msg)
        if pipette.has_tip:
            msg = f"{mount} holds a tip; drop it and reconcile the tip state before the run."
            raise AsmsWorkflowError(msg)
    plans: dict[tuple[str, str, str], str] = {}
    moves = [step for step in plan["steps"] if step["kind"] == "move_labware"]
    if moves:
        available = await client.available_plans()
        for step in moves:
            key = (step["labware"], step["source"], step["destination"])
            matches = [
                p.plan_identifier
                for p in available
                if (p.labware_identifier, p.source_location, p.destination_location) == key and not p.is_lid
            ]
            if len(matches) != 1:
                msg = f"The connector must have exactly one gripper plan for {key}; found {matches}."
                raise AsmsWorkflowError(msg)
            plans[key] = matches[0]
        deck = await client.deck_state()
        occupancy = {item.location_identifier: item.labware_identifier for item in deck.occupied_locations}
        if not deck.valid:
            msg = "The deck ledger is invalid; reconcile physical plate locations first."
            raise AsmsWorkflowError(msg)
        for location, labware in plan["initial_ledger"].items():
            if occupancy.get(location) != labware:
                msg = f"The deck ledger shows {occupancy.get(location)!r} at {location}, expected {labware!r}."
                raise AsmsWorkflowError(msg)
    if plan["uses_temperature_module"]:
        await client.temperature_device_info()
    return {"plans": plans, "simulating": await client.is_simulating()}


@dataclass
class _Mount:
    position: dict[str, float] | None = None
    travel_z: float | None = None


async def execute(
    client: AsmsSilaClient,
    plan: Mapping[str, Any],
    *,
    delay_scale: float = 1.0,
    sleep: Callable[[float], Any] = asyncio.sleep,
    log: Callable[[str], None] = lambda message: sys.stdout.write(message + "\n"),
) -> dict[str, Any]:
    """Run every step over gRPC; on any failure stop, deactivate the temperature module, and re-raise."""
    checked = await preflight(client, plan)
    mounts = {name: _Mount() for name in ("LEFT", "RIGHT")}
    record: list[dict[str, Any]] = []
    started = datetime.datetime.now(datetime.UTC)
    temperature_active = False

    async def travel(mount: str, target: Mapping[str, float]) -> None:
        state = mounts[mount]
        here = state.position or await client.position(Mount[mount])
        if state.travel_z is None:
            msg = f"{mount} has no known travel height; a liquid move must follow a tip pickup."
            raise AsmsWorkflowError(msg)
        if abs(here["x"] - target["x"]) > _POSITION_TOLERANCE or abs(here["y"] - target["y"]) > _POSITION_TOLERANCE:
            await client.move_to(Mount[mount], {"x": here["x"], "y": here["y"], "z": state.travel_z})
            await client.move_to(Mount[mount], {"x": target["x"], "y": target["y"], "z": state.travel_z})
        state.position = await client.move_to(Mount[mount], target)

    try:
        for step in plan["steps"]:
            kind = step["kind"]
            mount = step.get("mount")
            if kind == "comment":
                log(f"# {step['message']}")
            elif kind == "home":
                await client.home()
                for state in mounts.values():
                    state.position = None
            elif kind == "set_temperature_and_wait":
                temperature_active = True
                await client.set_temperature_and_wait(step["celsius"])
            elif kind == "deactivate_temperature":
                await client.deactivate_temperature()
                temperature_active = False
            elif kind == "delay":
                await sleep(step["seconds"] * delay_scale)
            elif kind == "configure_tip_type":
                await client.configure_tip_type(PipetteMount[mount], step["tip_volume"])
            elif kind == "configure_nozzles":
                if step["layout"] == "full":
                    await client.configure_full_nozzle(PipetteMount[mount], step["tiprack_diameter"])
                else:
                    await client.configure_single_nozzle(PipetteMount[mount], "A1", step["tiprack_diameter"])
            elif kind == "set_flow_rates":
                await client.set_flow_rates(PipetteMount[mount], step["aspirate"], step["dispense"], step["blow_out"])
            elif kind == "pick_up_tip":
                location = step["location"]
                await client.pick_up_tip_prepared(
                    PipetteMount[mount], TipLocation(location["x"], location["y"], location["z"]), step["tip_length"]
                )
                # The connector lifts every mount to the top of travel after pickup: that height
                # clears the whole deck with this tip, so every later move between wells uses it.
                here = await client.position(Mount[mount])
                mounts[mount].position = here
                mounts[mount].travel_z = here["z"]
            elif kind == "drop_tip":
                location = step["location"]
                await client.drop_tip_and_home_plunger(
                    PipetteMount[mount], TipLocation(location["x"], location["y"], location["z"])
                )
                mounts[mount].position = None
                mounts[mount].travel_z = None
            elif kind == "move":
                await travel(mount, step["target"])
            elif kind == "prepare_for_aspirate":
                await client.prepare_for_aspirate(Mount[mount])
            elif kind == "aspirate":
                await client.aspirate(Mount[mount], step["volume"])
            elif kind == "dispense":
                await client.dispense(Mount[mount], step["volume"], step["push_out"])
            elif kind == "blow_out":
                await client.blow_out(Mount[mount])
            elif kind == "touch_tip":
                await client.touch_tip(
                    PipetteMount[mount], WellGeometry(**step["well"]), step["z_offset"], step["speed"]
                )
                mounts[mount].position = None  # the connector ends at a well wall
            elif kind == "move_labware":
                plan_id = checked["plans"][(step["labware"], step["source"], step["destination"])]
                await client.move_labware(plan_id)
                status = await client.machine_status()
                if status.is_error_state:
                    msg = f"Flex entered an error state after gripper plan {plan_id}."
                    raise AsmsWorkflowError(msg)
            else:
                msg = f"Unknown plan step kind {kind!r}."
                raise AsmsWorkflowError(msg)
            record.append({"seq": step["seq"], "kind": kind})
    except BaseException as exc:
        failed = len(record)
        log(f"Stopped at step {failed} ({plan['steps'][failed]['kind']}): {type(exc).__name__}: {exc}")
        if temperature_active:
            with contextlib.suppress(Exception):
                await client.deactivate_temperature()
        raise
    return {
        "status": "completed",
        "simulating": checked["simulating"],
        "started_at": started.isoformat(),
        "finished_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "steps_executed": len(record),
        "gripper_plans": sorted(set(checked["plans"].values())),
    }


# -- runs and CLI ----------------------------------------------------------------------


def load_runs(path: Path = RUNS_FILE) -> dict[str, dict[str, Any]]:
    """Load the run specs: protocol file, labware files, and deck-ledger identifiers per run."""
    runs = json.loads(path.read_text(encoding="utf-8"))["runs"]
    for name, run in runs.items():
        run["protocol"] = (path.parent / run["protocol"]).resolve()
        run["labware"] = [(path.parent / item).resolve() for item in run["labware"]]
        run.setdefault("labware_identifiers", {})
        run["name"] = name
    return runs


async def build_plan(run: Mapping[str, Any], workdir: Path) -> dict[str, Any]:
    """Analyse one run's protocol in ``workdir`` and compile it into a SiLA step plan."""
    analysis = await asyncio.to_thread(
        analyze, run["protocol"], run["labware"], workdir / f"{run['name']}.analysis.json"
    )
    plan = await compile_plan(analysis, run["labware_identifiers"])
    plan["run"] = run["name"]
    plan["protocol"] = run["protocol"].name
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile or run the AS-MS protocols over the Flex SiLA gRPC surface.")
    sub = parser.add_subparsers(dest="command", required=True)
    compile_ = sub.add_parser("compile", help="Analyse a protocol and write its SiLA step plan for review.")
    compile_.add_argument("run", choices=sorted(load_runs()))
    compile_.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run", help="Run a compiled plan over gRPC.")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=50051)
    run.add_argument("--execute", action="store_true", help="Without this flag only the no-motion preflight runs.")
    run.add_argument("--confirm-deck", default="", help="Must equal the plan's run name followed by -DECK-READY.")
    run.add_argument("--report", type=Path)
    return parser


async def _main_async(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "compile":
        with tempfile.TemporaryDirectory() as workdir:
            plan = await build_plan(load_runs()[args.run], Path(workdir))
        args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        kinds: dict[str, int] = {}
        for step in plan["steps"]:
            kinds[step["kind"]] = kinds.get(step["kind"], 0) + 1
        return {"plan": str(args.output), "steps": len(plan["steps"]), "by_kind": kinds}
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    async with connect(f"{args.host}:{args.port}") as client:
        if not args.execute:
            checked = await preflight(client, plan)
            return {"status": "preflight-passed", "simulating": checked["simulating"], "steps": len(plan["steps"])}
        expected = f"{plan['run'].upper()}-DECK-READY"
        if args.confirm_deck != expected:
            msg = f"Pass --confirm-deck {expected} after checking the deck, tips, and liquids for this run."
            raise AsmsWorkflowError(msg)
        report = await execute(client, plan)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command and print its JSON result; return 2 on a refused or failed run."""
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_main_async(args))
    except AsmsWorkflowError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
