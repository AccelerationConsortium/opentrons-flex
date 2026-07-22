"""
SiLA2 feature for Opentrons Flex motion control.

Models motion the way the Flex hardware does: per-*mount* deck coordinates
(x, y, z in mm) rather than the OT-2's six raw Smoothie axes. Backed by
``FlexMotionController`` (a wrapper around ``OT3API``).

This feature retains the compatible primitive aspirate/dispense surface. Atomic
mix, touch-tip, liquid tracking, transfer profiles, and verified liquid classes
are separated into ``LiquidHandlingController``.
"""

import asyncio
import enum
import typing
from dataclasses import dataclass

from opentrons.hardware_control.types import OT3Mount
from opentrons.types import Point
from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    DirectGripperControlDisabledError,
    FlexMotionController,
    LiquidHandlingError,
    LiquidVolumeOutOfRangeError,
    MachineErrorStateError,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    RunOwnershipError,
    StallDetectedError,
    TipNotAttachedError,
)
from ._progress import OperationPhase, OperationProgress, report_progress, run_observable

# Expected failure modes for any commanded move; declared on the SiLA commands so
# clients receive Defined Execution Errors (with recovery hints) rather than
# opaque undefined errors. ``MachineErrorStateError`` covers the case where a move
# returns but the robot has silently entered a hardware error state (e.g. E-stop).
_MOVE_ERRORS = [
    RunOwnershipError,
    NotHomedError,
    MovementOutOfBoundsError,
    StallDetectedError,
    MachineErrorStateError,
    DirectGripperControlDisabledError,
]
_LIQUID_ERRORS = [
    *_MOVE_ERRORS,
    PipetteNotAttachedError,
    TipNotAttachedError,
    LiquidVolumeOutOfRangeError,
    LiquidHandlingError,
]

_MILLIMETRE = constraints.Unit(
    "mm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=0.001,
)
_MILLIMETRES_PER_SECOND = constraints.Unit(
    "mm/s",
    [
        constraints.Unit.Component(constraints.Unit.SI.METER),
        constraints.Unit.Component(constraints.Unit.SI.SECOND, exponent=-1),
    ],
    factor=0.001,
)
_MICROLITRE = constraints.Unit(
    "µL",
    [constraints.Unit.Component(constraints.Unit.SI.METER, exponent=3)],
    factor=1e-9,
)
_DIMENSIONLESS_RATE = constraints.Unit(
    "1",
    [constraints.Unit.Component(constraints.Unit.SI.DIMENSIONLESS)],
)
_Millimetres = typing.Annotated[float, _MILLIMETRE]
_MovementSpeed = typing.Annotated[float, constraints.MinimalInclusive(0.0), _MILLIMETRES_PER_SECOND]
_LiquidVolume = typing.Annotated[float, constraints.MinimalExclusive(0.0), _MICROLITRE]
_PushOutVolume = typing.Annotated[float, constraints.MinimalInclusive(0.0), _MICROLITRE]
_FlowRateMultiplier = typing.Annotated[float, constraints.MinimalExclusive(0.0), _DIMENSIONLESS_RATE]

# Possible E-stop states surfaced by MachineStatus. A Set constraint (AGENTS.md:
# model status with strings + Set, not integers) documents the closed vocabulary.
_ESTOP_STATES = ("DISENGAGED", "PHYSICALLY_ENGAGED", "LOGICALLY_ENGAGED", "NOT_PRESENT")


class Mount(enum.Enum):
    """A Flex instrument mount."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"
    GRIPPER = "GRIPPER"


_MOUNT_TO_OT3 = {Mount.LEFT: OT3Mount.LEFT, Mount.RIGHT: OT3Mount.RIGHT, Mount.GRIPPER: OT3Mount.GRIPPER}


def _ot3_mount(mount: Mount) -> OT3Mount:
    return _MOUNT_TO_OT3[mount]


@dataclass
class Position:
    """A deck position for one mount, in millimetres."""

    x: _Millimetres
    y: _Millimetres
    z: _Millimetres


@dataclass
class Lights:
    """State of the Flex status-bar (button) and deck (rails) lights."""

    button: bool
    rails: bool


@dataclass
class MachineStatus:
    """
    The robot's safety/error state, independent of any single command.

    Query this after a movement command to confirm the robot did not silently
    enter an error state even though the command returned successfully.
    """

    estop: typing.Annotated[str, constraints.Set(_ESTOP_STATES)]
    door_open: bool
    is_error_state: bool
    message: str


def _to_position(point: Point) -> Position:
    return Position(x=point.x, y=point.y, z=point.z)


class MotionControlFeature(sila.Feature):
    """
    SiLA2 feature for Opentrons Flex gantry and pipette motion.

    Provides homing, absolute and relative mount moves, position queries,
    primitive aspirate/dispense, emergency stop, and deck/status lights via the
    Opentrons ``OT3API``.
    """

    def __init__(self, controller: FlexMotionController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="robots",
            identifier="MotionController",
            name="Motion Controller",
            version="2.0",
        )
        self._controller = controller

    # ------------------------------------------------------------------ homing

    @sila.ObservableCommand(errors=_MOVE_ERRORS)
    async def home(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Home every axis. Required after an emergency stop before further motion.

        Yields:
            Update: Current homing progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting full robot home.")
        try:
            await self._controller.home()
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Full robot home cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Full robot home completed.")

    @sila.ObservableCommand(errors=_MOVE_ERRORS)
    async def home_mount(
        self,
        mount: Mount,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Home only the axes belonging to one mount.

        Args:
            mount: Mount to home (its Z and plunger/jaw axes).

        Yields:
            Update: Current homing progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Starting {mount.value} mount home.")
        try:
            await self._controller.home_mount(_ot3_mount(mount))
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, f"{mount.value} mount home cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} mount home completed.")

    # ------------------------------------------------------------------ motion

    @sila.ObservableCommand(errors=_MOVE_ERRORS)
    async def move_to(
        self,
        mount: Mount,
        x: _Millimetres,
        y: _Millimetres,
        z: _Millimetres,
        speed: _MovementSpeed,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Position:
        """
        Move a mount to an absolute deck position.

        Args:
            mount: Mount to move.
            x: Target X in mm.
            y: Target Y in mm.
            z: Target Z in mm.
            speed: Movement speed in mm/s (0 = hardware default).

        Yields:
            Update: Current movement progress update.

        Returns:
            The mount position after the move.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Starting {mount.value} absolute move.")
        spd = speed if speed > 0 else None
        try:
            point = await self._controller.move_to(_ot3_mount(mount), Point(x=x, y=y, z=z), speed=spd)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                f"{mount.value} absolute move cancelled.",
            )
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} absolute move completed.")
        return _to_position(point)

    @sila.ObservableCommand(errors=_MOVE_ERRORS)
    async def move_relative(
        self,
        mount: Mount,
        delta_x: _Millimetres,
        delta_y: _Millimetres,
        delta_z: _Millimetres,
        speed: _MovementSpeed,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Position:
        """
        Move a mount relative to its current position.

        Args:
            mount: Mount to move.
            delta_x: X offset in mm.
            delta_y: Y offset in mm.
            delta_z: Z offset in mm.
            speed: Movement speed in mm/s (0 = hardware default).

        Yields:
            Update: Current movement progress update.

        Returns:
            The mount position after the move.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Starting {mount.value} relative move.")
        spd = speed if speed > 0 else None
        try:
            point = await self._controller.move_rel(
                _ot3_mount(mount), Point(x=delta_x, y=delta_y, z=delta_z), speed=spd
            )
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                f"{mount.value} relative move cancelled.",
            )
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} relative move completed.")
        return _to_position(point)

    @sila.ObservableCommand(errors=_MOVE_ERRORS)
    async def get_position(
        self,
        mount: Mount,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Position:
        """
        Return the current deck position of ``mount`` in mm.

        Yields:
            Update: Current position-read progress update.
        """
        point = await run_observable(
            status,
            intermediate,
            f"Reading {mount.value} mount position.",
            f"{mount.value} mount position read.",
            f"{mount.value} mount position read cancelled.",
            self._controller.gantry_position(_ot3_mount(mount)),
        )
        return _to_position(point)

    # ----------------------------------------------------- primitive liquid moves

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def prepare_for_aspirate(
        self,
        mount: Mount,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Move the plunger to its bottom position, ready to aspirate.

        Yields:
            Update: Current pipette preparation progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Preparing {mount.value} to aspirate.")
        try:
            await self._controller.prepare_for_aspirate(_ot3_mount(mount))
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                f"{mount.value} preparation cancelled.",
            )
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} preparation completed.")

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def aspirate(
        self,
        mount: Mount,
        volume: _LiquidVolume,
        rate: _FlowRateMultiplier,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Draw liquid into the attached tip.

        Args:
            mount: Pipette mount (LEFT or RIGHT).
            volume: Volume to aspirate in µL.
            rate: Flow-rate multiplier on the pipette's configured aspirate rate.

        Yields:
            Update: Current aspiration progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Starting {mount.value} aspiration.")
        try:
            await self._controller.aspirate(_ot3_mount(mount), volume=volume, rate=rate)
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, f"{mount.value} aspiration cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} aspiration completed.")

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def dispense(
        self,
        mount: Mount,
        volume: _LiquidVolume,
        rate: _FlowRateMultiplier,
        push_out: _PushOutVolume,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Expel liquid from the attached tip.

        Args:
            mount: Pipette mount (LEFT or RIGHT).
            volume: Volume to dispense in µL.
            rate: Flow-rate multiplier on the pipette's configured dispense rate.
            push_out: Extra plunger push-out volume in µL (0 = pipette default).

        Yields:
            Update: Current dispense progress update.
        """
        po = push_out if push_out > 0 else None
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Starting {mount.value} dispense.")
        try:
            await self._controller.dispense(_ot3_mount(mount), volume=volume, rate=rate, push_out=po)
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, f"{mount.value} dispense cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} dispense completed.")

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def blow_out(
        self,
        mount: Mount,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Blow out any residual liquid from the attached tip.

        Yields:
            Update: Current blow-out progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Starting {mount.value} blow-out.")
        try:
            await self._controller.blow_out(_ot3_mount(mount))
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, f"{mount.value} blow-out cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} blow-out completed.")

    # ------------------------------------------------------------ stop / pause

    @sila.ObservableCommand()
    async def emergency_stop(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> str:
        """
        Immediately halt all motion. A re-home is required before resuming.

        Returns:
            A status message confirming the stop.
        """
        await run_observable(
            status,
            intermediate,
            "Executing emergency stop.",
            "Emergency stop executed.",
            "Emergency stop cancelled.",
            self._controller.stop(),
        )
        return "Emergency stop executed. Re-home required before resuming."

    @sila.ObservableCommand()
    async def pause(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> str:
        """
        Pause motion execution.

        Yields:
            Update: Current pause progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Pausing motion execution.")
        self._controller.pause()
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Motion execution paused.")
        return "Motion paused"

    @sila.ObservableCommand()
    async def resume(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> str:
        """
        Resume motion execution after a pause.

        Yields:
            Update: Current resume progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Resuming motion execution.")
        self._controller.resume()
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Motion execution resumed.")
        return "Motion resumed"

    # ------------------------------------------------------------------ lights

    @sila.ObservableCommand()
    async def set_lights(
        self,
        button: bool,
        rails: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Lights:
        """
        Set the status-bar (button) and deck (rails) lights.

        Args:
            button: Status-bar light on/off.
            rails: Deck rail lights on/off.

        Returns:
            The light state after the change.
        """
        await run_observable(
            status,
            intermediate,
            "Setting Flex lights.",
            "Flex lights set.",
            "Flex lights command cancelled.",
            self._controller.set_lights(button=button, rails=rails),
        )
        return await self._read_lights()

    @sila.UnobservableProperty()
    async def lights(self) -> Lights:
        """Return the current status-bar and deck light state."""
        return await self._read_lights()

    async def _read_lights(self) -> Lights:
        state = await self._controller.get_lights()
        return Lights(button=bool(state.get("button", False)), rails=bool(state.get("rails", False)))

    # ------------------------------------------------------------------ status

    @sila.UnobservableProperty()
    def is_simulating(self) -> bool:
        """Whether the connector is driving the OT3 simulator rather than real hardware."""
        return self._controller.is_simulating

    @sila.UnobservableProperty()
    def machine_status(self) -> MachineStatus:
        """
        Return the robot safety/error state (E-stop, door) independent of any move.

        A movement command can return successfully while the robot has silently
        entered a hardware error state. Read this property after each movement to
        confirm the move genuinely succeeded: ``is_error_state`` is True when the
        robot is in an error state that requires operator intervention.
        """
        state = self._controller.machine_status()
        return MachineStatus(
            estop=state.estop,
            door_open=state.door_open,
            is_error_state=state.is_error_state,
            message=state.message,
        )
