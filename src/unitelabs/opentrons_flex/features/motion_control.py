"""
SiLA2 feature for Opentrons Flex motion control.

Models motion the way the Flex hardware does: per-*mount* deck coordinates
(x, y, z in mm) rather than the OT-2's six raw Smoothie axes. Backed by
``FlexMotionController`` (a wrapper around ``OT3API``).

Liquid handling is exposed as the primitive aspirate/dispense moves only — the
connector does not own liquid-class logic (mix, touch-tip, sequence ordering);
that stays on the client, matching the OT-2 connector's contract.
"""

import enum
import typing
from dataclasses import dataclass

from opentrons.hardware_control.types import OT3Mount
from opentrons.types import Point
from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    FlexMotionController,
    MovementOutOfBoundsError,
    NotHomedError,
    StallDetectedError,
)

# Expected failure modes for any commanded move; declared on the SiLA commands so
# clients receive Defined Execution Errors (with recovery hints) rather than
# opaque undefined errors.
_MOVE_ERRORS = [NotHomedError, MovementOutOfBoundsError, StallDetectedError]


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

    x: float
    y: float
    z: float


@dataclass
class Lights:
    """State of the Flex status-bar (button) and deck (rails) lights."""

    button: bool
    rails: bool


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
        super().__init__(originator="ca.accelerationconsortium", category="robots")
        self._controller = controller

    # ------------------------------------------------------------------ homing

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def home(self) -> None:
        """Home every axis. Required after an emergency stop before further motion."""
        await self._controller.home()

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def home_mount(self, mount: Mount) -> None:
        """
        Home only the axes belonging to one mount.

        Args:
            mount: Mount to home (its Z and plunger/jaw axes).
        """
        await self._controller.home_mount(_ot3_mount(mount))

    # ------------------------------------------------------------------ motion

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def move_to(
        self,
        mount: Mount,
        x: float,
        y: float,
        z: float,
        speed: typing.Annotated[float, constraints.MinimalInclusive(0.0)] = 0.0,
    ) -> Position:
        """
        Move a mount to an absolute deck position.

        Args:
            mount: Mount to move.
            x: Target X in mm.
            y: Target Y in mm.
            z: Target Z in mm.
            speed: Movement speed in mm/s (0 = hardware default).

        Returns:
            The mount position after the move.
        """
        spd = speed if speed > 0 else None
        point = await self._controller.move_to(_ot3_mount(mount), Point(x=x, y=y, z=z), speed=spd)
        return _to_position(point)

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def move_relative(
        self,
        mount: Mount,
        delta_x: float,
        delta_y: float,
        delta_z: float,
        speed: typing.Annotated[float, constraints.MinimalInclusive(0.0)] = 0.0,
    ) -> Position:
        """
        Move a mount relative to its current position.

        Args:
            mount: Mount to move.
            delta_x: X offset in mm.
            delta_y: Y offset in mm.
            delta_z: Z offset in mm.
            speed: Movement speed in mm/s (0 = hardware default).

        Returns:
            The mount position after the move.
        """
        spd = speed if speed > 0 else None
        point = await self._controller.move_rel(
            _ot3_mount(mount), Point(x=delta_x, y=delta_y, z=delta_z), speed=spd
        )
        return _to_position(point)

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def get_position(self, mount: Mount) -> Position:
        """Return the current deck position of ``mount`` in mm."""
        point = await self._controller.gantry_position(_ot3_mount(mount))
        return _to_position(point)

    # ----------------------------------------------------- primitive liquid moves

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def prepare_for_aspirate(self, mount: Mount) -> None:
        """Move the plunger to its bottom position, ready to aspirate."""
        await self._controller.prepare_for_aspirate(_ot3_mount(mount))

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def aspirate(
        self,
        mount: Mount,
        volume: typing.Annotated[float, constraints.MinimalInclusive(0.0)],
        rate: typing.Annotated[float, constraints.MinimalExclusive(0.0)] = 1.0,
    ) -> None:
        """
        Draw liquid into the attached tip.

        Args:
            mount: Pipette mount (LEFT or RIGHT).
            volume: Volume to aspirate in µL.
            rate: Flow-rate multiplier on the pipette's configured aspirate rate.
        """
        await self._controller.aspirate(_ot3_mount(mount), volume=volume, rate=rate)

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def dispense(
        self,
        mount: Mount,
        volume: typing.Annotated[float, constraints.MinimalInclusive(0.0)],
        rate: typing.Annotated[float, constraints.MinimalExclusive(0.0)] = 1.0,
        push_out: typing.Annotated[float, constraints.MinimalInclusive(0.0)] = 0.0,
    ) -> None:
        """
        Expel liquid from the attached tip.

        Args:
            mount: Pipette mount (LEFT or RIGHT).
            volume: Volume to dispense in µL.
            rate: Flow-rate multiplier on the pipette's configured dispense rate.
            push_out: Extra plunger push-out volume in µL (0 = pipette default).
        """
        po = push_out if push_out > 0 else None
        await self._controller.dispense(_ot3_mount(mount), volume=volume, rate=rate, push_out=po)

    @sila.UnobservableCommand(errors=_MOVE_ERRORS)
    async def blow_out(self, mount: Mount) -> None:
        """Blow out any residual liquid from the attached tip."""
        await self._controller.blow_out(_ot3_mount(mount))

    # ------------------------------------------------------------ stop / pause

    @sila.UnobservableCommand()
    async def emergency_stop(self) -> str:
        """
        Immediately halt all motion. A re-home is required before resuming.

        Returns:
            A status message confirming the stop.
        """
        await self._controller.stop()
        return "Emergency stop executed. Re-home required before resuming."

    @sila.UnobservableCommand()
    async def pause(self) -> str:
        """Pause motion execution."""
        self._controller.pause()
        return "Motion paused"

    @sila.UnobservableCommand()
    async def resume(self) -> str:
        """Resume motion execution after a pause."""
        self._controller.resume()
        return "Motion resumed"

    # ------------------------------------------------------------------ lights

    @sila.UnobservableCommand()
    async def set_lights(self, button: bool, rails: bool) -> Lights:
        """
        Set the status-bar (button) and deck (rails) lights.

        Args:
            button: Status-bar light on/off.
            rails: Deck rail lights on/off.

        Returns:
            The light state after the change.
        """
        await self._controller.set_lights(button=button, rails=rails)
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
