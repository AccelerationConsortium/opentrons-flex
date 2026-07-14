"""
IO wrapper for Opentrons Flex (OT-3) motion control.

Unlike the OT-2 connector, which drives the Smoothie board over a serial port,
the Flex talks to its motor-controller boards over CAN bus. There is no
``SmoothieDriver`` to wrap. Instead this controller wraps the high-level
``HardwareControlAPI`` (the ``OT3API`` implementation), which already exposes
mount/axis motion, instrument caching, lights and gripper control on top of the
CAN layer. We work at that API seam — we do not reimplement the CAN protocol or
modify ot3-firmware.

Concurrency: the SiLA2 gRPC server and the in-process opentrons robot-server
share a single ``HardwareControlAPI``. ``HardwareProxy`` serialises every call
from the robot-server through an ``asyncio.Lock``. This controller is handed the
*same* lock so SiLA motion commands cannot interleave with HTTP-driven motion.
"""

import asyncio
import logging
from dataclasses import dataclass

from opentrons.hardware_control import HardwareControlAPI
from opentrons.hardware_control.types import Axis, DoorState, EstopState, OT3Mount, TipStateType
from opentrons.types import Point

from ._errors import MachineErrorStateError, TipStateError, translate_motion_errors, translate_tip_errors
from .hardware_proxy import _TimedLock

log = logging.getLogger(__name__)

# Axes that belong to each mount, used when homing "just this mount".
_MOUNT_AXES: dict[OT3Mount, list[Axis]] = {
    OT3Mount.LEFT: [Axis.Z_L, Axis.P_L],
    OT3Mount.RIGHT: [Axis.Z_R, Axis.P_R],
    OT3Mount.GRIPPER: [Axis.Z_G, Axis.G],
}

# E-stop states that mean the robot is in a hardware error state. DISENGAGED is
# healthy; NOT_PRESENT is treated as healthy too (the simulator and some rigs
# report no E-stop hardware) so it is surfaced for information but never fails a
# move. Motor/encoder "status ok" is deliberately NOT used here: it reads False
# simply because an axis is not homed yet, which is a normal pre-home condition,
# not an error.
_ESTOP_ERROR_STATES: frozenset[EstopState] = frozenset({EstopState.PHYSICALLY_ENGAGED, EstopState.LOGICALLY_ENGAGED})


@dataclass(frozen=True)
class MachineState:
    """
    Snapshot of the Flex safety/error state, independent of any single move.

    ``is_error_state`` is the single question a caller asks after a movement
    command: did the robot silently enter an error state even though the command
    returned? ``estop`` and ``door_open`` provide the underlying detail.
    """

    estop: str  # EstopState name: DISENGAGED | PHYSICALLY_ENGAGED | LOGICALLY_ENGAGED | NOT_PRESENT
    door_open: bool
    is_error_state: bool
    message: str  # operator-facing description + resolution hint; empty when healthy


class FlexMotionController:
    """
    High-level motion controller for the Opentrons Flex, wrapping ``OT3API``.

    This is a thin, SiLA-agnostic wrapper that:
    1. Reuses the battle-tested ``OT3API`` implementation (CAN, calibration, etc.).
    2. Provides a focused surface for the SiLA2 features (motion, pipette, lights).
    3. Serialises hardware access through a lock shared with ``HardwareProxy``.

    Methods take Opentrons types (``OT3Mount``, ``Axis``, ``Point``); the SiLA
    features own the conversion to and from user-facing structures.
    """

    def __init__(
        self,
        api: HardwareControlAPI,
        lock: asyncio.Lock | None = None,
        lock_timeout_s: float | None = None,
    ) -> None:
        """
        Initialize with an existing ``HardwareControlAPI``.

        Use :meth:`build` for normal construction, or :meth:`from_api` when sharing
        the API and lock with the in-process robot-server.
        """
        self._api = api
        raw_lock = lock if lock is not None else asyncio.Lock()
        self._lock: _TimedLock = _TimedLock(raw_lock, lock_timeout_s)

    @classmethod
    async def build(
        cls,
        simulate: bool = True,
        lock_timeout_s: float | None = None,
    ) -> "FlexMotionController":
        """
        Build a standalone ``FlexMotionController`` (own ``OT3API``, own lock).

        Args:
            simulate: If True, build the OT3 hardware *simulator* backend rather than
                connecting to real CAN hardware. AGENTS.md mandates a simulation mode.
            lock_timeout_s: Seconds to wait for the hardware lock before raising
                ``TimeoutError``.
        """
        from opentrons.hardware_control.ot3api import OT3API

        if simulate:
            log.info("Building FlexMotionController in simulation mode")
            api = await OT3API.build_hardware_simulator()
        else:
            log.info("Building FlexMotionController for real Flex hardware (CAN)")
            api = await OT3API.build_hardware_controller()
        return cls(api=api, lock_timeout_s=lock_timeout_s)

    @classmethod
    def from_api(
        cls,
        api: HardwareControlAPI,
        lock: asyncio.Lock,
        lock_timeout_s: float | None = None,
    ) -> "FlexMotionController":
        """
        Wrap an already-built ``HardwareControlAPI``, sharing a lock with ``HardwareProxy``.

        Used in the in-process server mode where both the SiLA2 gRPC server and the
        opentrons HTTP server drive one ``OT3API``. The caller creates one
        ``asyncio.Lock`` and passes it to both this method and ``HardwareProxy`` so
        callers from both servers are serialised through the same lock.
        """
        return cls(api=api, lock=lock, lock_timeout_s=lock_timeout_s)

    # ------------------------------------------------------------------ status

    @property
    def is_simulating(self) -> bool:
        """Whether the underlying API is a simulator backend."""
        return bool(self._api.is_simulator)

    # ------------------------------------------------------- error / safety state

    def machine_status(self) -> MachineState:
        """
        Return the robot's current safety/error state (E-stop, door).

        Reads cached hardware state only (no CAN I/O and no lock), so it is safe
        to call while a move is holding the lock. This is the query a caller runs
        after a movement command to confirm the robot did not silently enter an
        error state even though the command returned successfully.
        """
        estop = self._api.get_estop_state()
        door_open = self._api.door_state == DoorState.OPEN
        is_error = estop in _ESTOP_ERROR_STATES
        message = f"E-stop is {estop.name}. Release the E-stop and re-home before continuing." if is_error else ""
        return MachineState(estop=estop.name, door_open=door_open, is_error_state=is_error, message=message)

    def _assert_machine_ok(self) -> None:
        """
        Raise ``MachineErrorStateError`` if the robot has entered a hardware error state.

        Called at the end of each motion command so a move that "succeeded" at the
        OT3API level but left the machine E-stopped is reported as a failure rather
        than a silent success.
        """
        state = self.machine_status()
        if state.is_error_state:
            raise MachineErrorStateError(state.message)

    # ------------------------------------------------------------------ motion

    @translate_motion_errors
    async def home(self, axes: list[Axis] | None = None) -> None:
        """Home the given axes (all axes when ``None``)."""
        async with self._lock:
            await self._api.home(axes=axes)
            self._assert_machine_ok()

    async def home_mount(self, mount: OT3Mount) -> None:
        """Home only the axes belonging to one mount."""
        await self.home(_MOUNT_AXES.get(mount))

    @translate_motion_errors
    async def move_to(self, mount: OT3Mount, point: Point, speed: float | None = None) -> Point:
        """Move ``mount`` to an absolute deck ``point`` and return the resulting position."""
        async with self._lock:
            await self._api.move_to(mount=mount, abs_position=point, speed=speed)
            result = await self._api.gantry_position(mount, refresh=True)
            self._assert_machine_ok()
            return result

    @translate_motion_errors
    async def move_rel(self, mount: OT3Mount, delta: Point, speed: float | None = None) -> Point:
        """Move ``mount`` by ``delta`` and return the resulting position."""
        async with self._lock:
            await self._api.move_rel(mount=mount, delta=delta, speed=speed)
            result = await self._api.gantry_position(mount, refresh=True)
            self._assert_machine_ok()
            return result

    async def gantry_position(self, mount: OT3Mount) -> Point:
        """Return the current position of ``mount``."""
        async with self._lock:
            return await self._api.gantry_position(mount, refresh=True)

    async def stop(self) -> None:
        """Halt all motion. A re-home is required before further motion."""
        async with self._lock:
            await self._api.halt()

    def pause(self) -> None:
        """Pause execution (does not touch the bus directly; no lock needed)."""
        from opentrons.hardware_control.types import PauseType

        self._api.pause(PauseType.PAUSE)

    def resume(self) -> None:
        """Resume execution after a pause."""
        from opentrons.hardware_control.types import PauseType

        self._api.resume(PauseType.PAUSE)

    # --------------------------------------------------------------- pipettes

    async def cache_instruments(self) -> None:
        """Re-scan the mounts for attached pipettes."""
        async with self._lock:
            await self._api.cache_instruments()

    @property
    def attached_instruments(self) -> dict:
        """Mapping of mount -> attached pipette dict (empty dict when no pipette)."""
        return dict(self._api.attached_instruments)

    async def prepare_for_aspirate(self, mount: OT3Mount, rate: float = 1.0) -> None:
        """Position the plunger at bottom, ready to aspirate."""
        async with self._lock:
            await self._api.prepare_for_aspirate(mount=mount, rate=rate)

    async def aspirate(self, mount: OT3Mount, volume: float | None = None, rate: float = 1.0) -> None:
        """Aspirate ``volume`` µL on ``mount`` (full available volume when ``None``)."""
        async with self._lock:
            await self._api.aspirate(mount=mount, volume=volume, rate=rate)

    async def dispense(
        self, mount: OT3Mount, volume: float | None = None, rate: float = 1.0, push_out: float | None = None
    ) -> None:
        """Dispense ``volume`` µL on ``mount`` (current volume when ``None``)."""
        async with self._lock:
            await self._api.dispense(mount=mount, volume=volume, rate=rate, push_out=push_out)

    async def blow_out(self, mount: OT3Mount, volume: float | None = None) -> None:
        """Blow out any residual liquid on ``mount``."""
        async with self._lock:
            await self._api.blow_out(mount=mount, volume=volume)

    @translate_tip_errors
    @translate_motion_errors
    async def pick_up_tip(
        self,
        mount: OT3Mount,
        tip_length: float,
        presses: int | None = None,
        increment: float | None = None,
        prep_after: bool = True,
    ) -> TipStateType:
        """Pick up a tip at the current position and verify that it is present."""
        async with self._lock:
            await self._api.pick_up_tip(
                mount=mount,
                tip_length=tip_length,
                presses=presses,
                increment=increment,
                prep_after=prep_after,
            )
            state = await self._api.get_tip_presence_status(mount)
            self._assert_machine_ok()
            if state is not TipStateType.PRESENT:
                msg = f"Tip pickup completed but {mount.name} sensor reported {state.name}."
                raise TipStateError(msg)
            return state

    @translate_tip_errors
    @translate_motion_errors
    async def drop_tip(self, mount: OT3Mount, home_after: bool = False) -> TipStateType:
        """Drop the attached tip at the current position and verify that it is absent."""
        async with self._lock:
            await self._api.drop_tip(mount=mount, home_after=home_after)
            state = await self._api.get_tip_presence_status(mount)
            self._assert_machine_ok()
            if state is not TipStateType.ABSENT:
                msg = f"Tip drop completed but {mount.name} sensor reported {state.name}."
                raise TipStateError(msg)
            return state

    @translate_tip_errors
    async def get_tip_presence(self, mount: OT3Mount) -> TipStateType:
        """Return the tip-presence sensor state for a pipette mount."""
        async with self._lock:
            return await self._api.get_tip_presence_status(mount)

    # ------------------------------------------------------------------ lights

    async def set_lights(self, button: bool | None = None, rails: bool | None = None) -> None:
        """Set the status-bar (``button``) and/or deck (``rails``) lights."""
        async with self._lock:
            await self._api.set_lights(button=button, rails=rails)

    async def get_lights(self) -> dict[str, bool]:
        """Return the current light state, e.g. ``{"button": bool, "rails": bool}``."""
        async with self._lock:
            return await self._api.get_lights()

    # ----------------------------------------------------------------- cleanup

    async def disconnect(self) -> None:
        """Release hardware resources owned by a standalone controller."""
        async with self._lock:
            await self._api.clean_up()


# Re-export for callers that map SiLA enums to Opentrons types.
__all__ = ["Axis", "FlexMotionController", "MachineState", "OT3Mount", "Point", "TipStateType"]
