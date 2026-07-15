"""SiLA 2 controller for the sensor-verified Flex tip lifecycle."""

import asyncio
import enum
import math
import typing
from dataclasses import dataclass

from opentrons.hardware_control.types import TipStateType
from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    FlexMotionController,
    MachineErrorStateError,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    Point,
    StallDetectedError,
    TipDropError,
    TipNotAttachedError,
    TipPickupError,
    TipStateError,
)
from ._pipette_types import PIPETTE_MOUNTS, PipetteMount
from ._progress import OperationPhase

MAX_SUPPORTED_TIP_LENGTH_MM = 100.0
_MILLIMETRE = constraints.Unit(
    "mm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=0.001,
)
Millimetres = typing.Annotated[float, _MILLIMETRE]
TipLength = typing.Annotated[
    float,
    constraints.MinimalExclusive(0.0),
    constraints.MaximalInclusive(MAX_SUPPORTED_TIP_LENGTH_MM),
    _MILLIMETRE,
]

_TIP_MOTION_ERRORS = [
    NotHomedError,
    MovementOutOfBoundsError,
    StallDetectedError,
    MachineErrorStateError,
]
_TIP_STATE_ERRORS = [PipetteNotAttachedError, TipStateError]
_FEATURE_FQI = "ca.accelerationconsortium/robots/TipController/v1"
_T = typing.TypeVar("_T")


class TipPresence(enum.Enum):
    """Physical tip-presence state reported by the pipette sensor."""

    ABSENT = "ABSENT"
    PRESENT = "PRESENT"


@dataclass
class TipOperationProgress:
    """
    Progress update for a tip attachment or release operation.

    Attributes:
        Phase: Current lifecycle phase of the operation.
        Message: Operator-facing progress or recovery message.
    """

    phase: OperationPhase
    message: str


@dataclass
class TipLocation:
    """
    Absolute deck position for the pipette tip operation.

    Attributes:
        X: Target X coordinate in millimetres.
        Y: Target Y coordinate in millimetres.
        Z: Target Z coordinate in millimetres.
    """

    x: Millimetres
    y: Millimetres
    z: Millimetres


def _tip_presence(state: TipStateType) -> TipPresence:
    return TipPresence[state.name]


def _parameter_fqi(command: str, parameter: str) -> str:
    return f"{_FEATURE_FQI}/Command/{command}/Parameter/{parameter}"


def _validate_tip_length(tip_length: float) -> None:
    """Reject non-finite values not caught by numeric FDL constraints."""
    if not math.isfinite(tip_length) or not 0.0 < tip_length <= MAX_SUPPORTED_TIP_LENGTH_MM:
        raise sila.errors.ValidationError(
            f"Tip length must be finite and greater than 0 mm and at most {MAX_SUPPORTED_TIP_LENGTH_MM:g} mm.",
            _parameter_fqi("PickUpTip", "TipLength"),
        )


def _validate_location(command: str, location: TipLocation) -> None:
    if not all(math.isfinite(value) for value in (location.x, location.y, location.z)):
        raise sila.errors.ValidationError(
            "Tip location coordinates must all be finite. Check the deck position and retry.",
            _parameter_fqi(command, "Location"),
        )


def _report_progress(
    status: sila.Status,
    intermediate: sila.Intermediate[TipOperationProgress],
    progress: float,
    phase: OperationPhase,
    message: str,
) -> None:
    status.update(progress=progress)
    intermediate.send(TipOperationProgress(phase=phase, message=message))


async def _run_observable(
    status: sila.Status,
    intermediate: sila.Intermediate[TipOperationProgress],
    start_message: str,
    complete_message: str,
    cancelled_message: str,
    action: typing.Awaitable[_T],
) -> _T:
    _report_progress(status, intermediate, 0.0, OperationPhase.STARTING, start_message)
    try:
        result = await action
    except asyncio.CancelledError:
        _report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, cancelled_message)
        raise
    _report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, complete_message)
    return result


class TipController(sila.Feature):
    """Controls and verifies tip attachment on Opentrons Flex pipettes."""

    def __init__(self, controller: FlexMotionController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="robots",
            version="1.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=[*_TIP_MOTION_ERRORS, TipPickupError, *_TIP_STATE_ERRORS])
    async def pick_up_tip(
        self,
        mount: PipetteMount,
        location: TipLocation,
        tip_length: TipLength,
        prep_after: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[TipOperationProgress],
    ) -> TipPresence:
        """
        Move to an absolute deck position, pick up a tip, and verify it.

        Movement and pickup execute under one hardware lock, so another client
        cannot move the robot between positioning and pickup. Presses and depth
        increments use the hardware model's validated defaults.

        Args:
            Mount: Pipette mount to operate (LEFT or RIGHT).
            Location: Absolute deck position where pickup must occur.
            TipLength: Physical tip length in millimetres.
            PrepAfter: Prepare the plunger for aspiration after pickup.

        Yields:
            Update: Current tip-pickup progress update.

        Returns:
            TipPresence: PRESENT after the hardware sensor verifies the pickup.
        """
        _validate_location("PickUpTip", location)
        _validate_tip_length(tip_length)
        state = await _run_observable(
            status,
            intermediate,
            f"Moving {mount.value} to the pickup location.",
            f"Tip pickup on {mount.value} verified.",
            f"Tip pickup on {mount.value} cancelled.",
            self._controller.pick_up_tip(
                PIPETTE_MOUNTS[mount],
                location=Point(x=location.x, y=location.y, z=location.z),
                tip_length=tip_length,
                prep_after=prep_after,
            ),
        )
        return _tip_presence(state)

    @sila.ObservableCommand(errors=[*_TIP_MOTION_ERRORS, TipDropError, TipNotAttachedError, *_TIP_STATE_ERRORS])
    async def drop_tip(
        self,
        mount: PipetteMount,
        location: TipLocation,
        home_after: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[TipOperationProgress],
    ) -> TipPresence:
        """
        Move to an absolute deck position, drop the attached tip, and verify it.

        Movement and release execute under one hardware lock. The supplied
        location must be a safe trash or return-tip position.

        Args:
            Mount: Pipette mount to operate (LEFT or RIGHT).
            Location: Absolute deck position where release must occur.
            HomeAfter: Home the pipette plunger after releasing the tip.

        Yields:
            Update: Current tip-drop progress update.

        Returns:
            TipPresence: ABSENT after the hardware sensor verifies the drop.
        """
        _validate_location("DropTip", location)
        state = await _run_observable(
            status,
            intermediate,
            f"Moving {mount.value} to the drop location.",
            f"Tip drop on {mount.value} verified.",
            f"Tip drop on {mount.value} cancelled.",
            self._controller.drop_tip(
                PIPETTE_MOUNTS[mount],
                location=Point(x=location.x, y=location.y, z=location.z),
                home_after=home_after,
            ),
        )
        return _tip_presence(state)

    @sila.UnobservableCommand(errors=_TIP_STATE_ERRORS)
    async def get_tip_presence(self, mount: PipetteMount) -> TipPresence:
        """
        Read the tip-presence sensor on one attached pipette.

        Args:
            Mount: Pipette mount to read (LEFT or RIGHT).

        Returns:
            TipPresence: PRESENT when a tip is attached; otherwise ABSENT.
        """
        state = await self._controller.get_tip_presence(PIPETTE_MOUNTS[mount])
        return _tip_presence(state)
