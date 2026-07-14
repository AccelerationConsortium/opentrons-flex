"""
Defined hardware errors for module controllers, and translation from opentrons.

The module features surface these as SiLA Defined Execution Errors. Translation
lives at the controller (io) layer because the underlying opentrons exception
type depends on the backend in use (low-level driver vs high-level module
object) — catching them here means the features see one stable set of errors
regardless of path.

Coverage (source-verified):
- ModuleNotRespondingError  <- opentrons comm-layer SerialException / NoResponse
- ModuleOperationError      <- ThermocyclerError, TempDeckError

Known gap: the heater-shaker has no dedicated opentrons exception class, so its
operational failures surface as comm errors (ModuleNotRespondingError) or, if
neither, as undefined errors. This should be revisited once the real
heater-shaker failure exceptions are observed on hardware.
"""

import functools
import inspect
import typing

from opentrons.drivers.asyncio.communication.errors import SerialException
from opentrons.drivers.temp_deck.driver import TempDeckError
from opentrons.hardware_control.modules.errors import AbsorbanceReaderDisconnectedError
from opentrons.hardware_control.modules.thermocycler import ThermocyclerError
from opentrons_shared_data.errors.exceptions import (
    FlexStackerHopperLabwareError,
    FlexStackerShuttleLabwareError,
    FlexStackerShuttleMissingError,
    FlexStackerShuttleNotEmptyError,
    FlexStackerStallError,
)

_OPERATION_ERRORS = (
    ThermocyclerError,
    TempDeckError,
    AbsorbanceReaderDisconnectedError,
    FlexStackerHopperLabwareError,
    FlexStackerShuttleLabwareError,
    FlexStackerShuttleMissingError,
    FlexStackerShuttleNotEmptyError,
    FlexStackerStallError,
)


class ModuleNotRespondingError(Exception):
    """
    The module did not respond — it may be disconnected or powered off.

    Check that the module is connected to the Flex and powered on, then retry.
    """


class ModuleOperationError(Exception):
    """
    The module reported an error while carrying out the requested operation.

    The underlying driver/module message is preserved to aid recovery (e.g.
    re-seat labware, check the module's status indicators, or power-cycle it).
    """


# Defined errors every module command can raise; the features pass this to their
# SiLA command declarations (plus any command-specific errors).
COMMON_MODULE_ERRORS = (ModuleNotRespondingError, ModuleOperationError)


def translate_module_errors(
    fn: typing.Callable[..., typing.Awaitable[object]],
) -> typing.Callable[..., typing.Awaitable[object]]:
    """Wrap an async controller method, translating opentrons exceptions to defined errors."""

    @functools.wraps(fn)
    async def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return await fn(*args, **kwargs)
        except SerialException as e:
            raise ModuleNotRespondingError(str(e)) from e
        except _OPERATION_ERRORS as e:
            raise ModuleOperationError(str(e)) from e

    return wrapper


def translate_public_async_methods(cls: type) -> None:
    """Apply ``translate_module_errors`` to every public async instance method of cls."""
    for name, attr in list(vars(cls).items()):
        if not name.startswith("_") and inspect.iscoroutinefunction(attr):
            setattr(cls, name, translate_module_errors(attr))


# ---------------------------------------------------------------------------
# Motion defined errors
#
# Translation lives here (io layer) for the same reason as the module errors:
# the features see one stable set of SiLA errors regardless of the opentrons
# exception type the OT3API happens to raise. The docstrings become the SiLA
# DefinedExecutionError descriptions on the wire, so they are written for an
# operator and include a resolution hint.
# ---------------------------------------------------------------------------


class NotHomedError(Exception):
    """
    The robot's position is unknown, so the move was refused.

    Home the robot (or the affected mount) before requesting a move.
    """


class MovementOutOfBoundsError(Exception):
    """
    The requested move would take the mount outside the deck working volume.

    Check the target coordinates against the Flex deck envelope and retry.
    """


class StallDetectedError(Exception):
    """
    A stall or collision was detected during the move; motion was halted.

    Clear any obstruction, then re-home before continuing. The underlying
    hardware message is preserved to aid diagnosis.
    """


class MachineErrorStateError(Exception):
    """
    The move was accepted but the robot has silently entered a hardware error state.

    A movement command can return without raising while the machine has already
    entered an error state (for example the E-stop became engaged mid-move). This
    error surfaces that hidden condition so callers never treat such a move as
    successful. Clear the fault (release the E-stop), then re-home before
    continuing. The underlying hardware state is preserved to aid diagnosis.
    """


# ---------------------------------------------------------------------------
# Pipette tip lifecycle defined errors
# ---------------------------------------------------------------------------


class PipetteNotAttachedError(Exception):
    """
    No pipette is attached to the requested mount.

    Attach a Flex pipette, re-scan instruments, and retry the tip operation.
    """


class TipPickupError(Exception):
    """
    The pipette failed to pick up a tip at its current position.

    Check pipette alignment, tip compatibility, and tip-rack seating, then move
    to the intended tip well and retry.
    """


class TipDropError(Exception):
    """
    The pipette failed to release its attached tip.

    Check the drop location and for a jammed tip, then clear any obstruction and
    retry from a safe position.
    """


class TipStateError(Exception):
    """
    The measured tip state did not match the requested tip operation.

    Inspect the pipette and tip sensor, reconcile the physical tip state, and
    re-home before retrying if a collision or unexpected attachment occurred.
    """


def translate_tip_errors(
    fn: typing.Callable[..., typing.Awaitable[object]],
) -> typing.Callable[..., typing.Awaitable[object]]:
    """Wrap an async tip method, translating OT3API exceptions to defined errors."""

    @functools.wraps(fn)
    async def wrapper(*args: object, **kwargs: object) -> object:
        from opentrons.hardware_control.types import FailedTipStateCheck
        from opentrons.types import PipetteNotAttachedError as OpentronsPipetteNotAttachedError
        from opentrons_shared_data.errors.exceptions import (
            TipDropFailedError,
            TipPickupFailedError,
            UnexpectedTipAttachError,
            UnexpectedTipRemovalError,
            UnmatchedTipPresenceStates,
        )

        try:
            return await fn(*args, **kwargs)
        except OpentronsPipetteNotAttachedError as e:
            raise PipetteNotAttachedError(str(e)) from e
        except (TipPickupFailedError, UnexpectedTipAttachError) as e:
            raise TipPickupError(str(e)) from e
        except (TipDropFailedError, UnexpectedTipRemovalError) as e:
            raise TipDropError(str(e)) from e
        except (UnmatchedTipPresenceStates, FailedTipStateCheck) as e:
            raise TipStateError(str(e)) from e

    return wrapper


def translate_motion_errors(
    fn: typing.Callable[..., typing.Awaitable[object]],
) -> typing.Callable[..., typing.Awaitable[object]]:
    """Wrap an async motion method, translating OT3API exceptions to defined errors."""

    @functools.wraps(fn)
    async def wrapper(*args: object, **kwargs: object) -> object:
        # Imported lazily: these live in opentrons/opentrons_shared_data and are
        # only needed when a motion call actually fails.
        from opentrons.hardware_control.errors import OutOfBoundsMove
        from opentrons_shared_data.errors.exceptions import (
            PositionEstimationInvalidError,
            PositionUnknownError,
            StallOrCollisionDetectedError,
        )

        try:
            return await fn(*args, **kwargs)
        except (PositionUnknownError, PositionEstimationInvalidError) as e:
            raise NotHomedError(str(e)) from e
        except OutOfBoundsMove as e:
            raise MovementOutOfBoundsError(str(e)) from e
        except StallOrCollisionDetectedError as e:
            raise StallDetectedError(str(e)) from e

    return wrapper


# ---------------------------------------------------------------------------
# Gripper defined errors (Flex-only instrument)
# ---------------------------------------------------------------------------


class GripperNotAttachedError(Exception):
    """
    No gripper is attached, so the requested gripper action cannot run.

    Attach the Flex gripper to the rear mount and re-scan instruments before retrying.
    """


class GripActionError(Exception):
    """
    The gripper failed to complete a grip, ungrip, or home-jaw action.

    The underlying hardware message is preserved so the failure can be diagnosed.
    """


# ---------------------------------------------------------------------------
# Calibration defined errors
# ---------------------------------------------------------------------------


class CalibrationProbeNotAttachedError(Exception):
    """
    The calibration probe is required for this routine but is not attached.

    Attach the conductive calibration probe to the pipette nozzle (or the named
    gripper jaw) and retry.
    """


class CalibrationFailedError(Exception):
    """
    An automatic calibration routine failed to find or verify a position.

    The underlying hardware/geometry message is preserved so the measured
    deviation and probe coordinates are available for troubleshooting.
    """
