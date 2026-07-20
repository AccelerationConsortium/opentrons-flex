"""
Defined hardware errors for module controllers, and translation from opentrons.

The module features surface these as SiLA Defined Execution Errors. Translation
lives at the controller (io) layer because the underlying opentrons exception
type depends on the backend in use (low-level driver vs high-level module
object) — catching them here means the features see one stable set of errors
regardless of path.

Coverage (source-verified):
- ModuleNotRespondingError  <- opentrons comm-layer SerialException / NoResponse
- ModuleOperationError      <- FailedCommand/ErrorResponse, ThermocyclerError, TempDeckError

Known gap: the heater-shaker has no dedicated opentrons exception class. Its
firmware rejections surface as FailedCommand and map to ModuleOperationError,
while any failure outside the translated classes remains an undefined error.
Revisit this once dedicated heater-shaker exceptions are observed on hardware.
"""

import functools
import inspect
import typing

from opentrons.drivers.asyncio.communication.errors import FailedCommand, SerialException
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


class InvalidTemperatureTargetError(Exception):
    """
    The requested target is not a finite temperature supported by the module.

    Provide a finite value between 4 and 95 degrees Celsius and retry. Values
    such as NaN and positive or negative infinity are never sent to hardware.
    """


class InvalidWavelengthError(Exception):
    """
    The requested wavelength configuration is not supported by the attached reader.

    Read the SupportedWavelengths field from the AbsorbanceReaderController Status
    property, choose only values reported by that module, and retry.
    """


class PlateReaderNotReadyError(Exception):
    """
    The Absorbance Plate Reader is not ready for the requested operation.

    For initialization, remove the plate and place the lid on the reader. For a
    measurement, initialize the reader, place the plate, and place the lid on the
    reader. Use an allowlisted LabwareMovementController lid plan with the Flex
    Gripper; do not move the reader lid manually.
    """


class StackerNotReadyError(Exception):
    """
    The Flex Stacker is not installed, initialized, or safely closed for this operation.

    Confirm the Stacker is mechanically installed, close its hopper door, clear any
    obstruction, home it if necessary, and retry.
    """


class StackerMovementOutOfRangeError(Exception):
    """
    The requested maintenance move exceeds the travel of the selected Stacker axis.

    Use a non-negative distance no greater than the axis-specific limit reported in
    the error message. Prefer RetrieveLabware and StoreLabware for routine workflows.
    """


class InvalidStackerConfigurationError(Exception):
    """
    A Stacker labware or LED setting is outside the supported hardware range.

    Correct the value using the range in the error message and retry. For labware,
    use the exact assembled height including any lid or adapter.
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
        except FailedCommand as e:
            # The device did respond, but rejected the operation. Preserve the
            # firmware response (including its error code) for operator recovery.
            raise ModuleOperationError(str(e)) from e
        except SerialException as e:
            raise ModuleNotRespondingError(str(e)) from e
        except AbsorbanceReaderDisconnectedError as e:
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

    Home the robot (or the affected mount) before requesting a move. If a halt
    interrupted gripper motion, fully home the robot and then run HomeJaw before
    resuming other hardware actuation.
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


class TipNotAttachedError(Exception):
    """
    The requested drop was refused because no tip is attached.

    Confirm the physical tip state and perform a verified pickup before retrying
    the drop operation.
    """


class TipStateError(Exception):
    """
    The measured tip state did not match the requested tip operation.

    Inspect the pipette and tip sensor, reconcile the physical tip state, and
    re-home before retrying if a collision or unexpected attachment occurred.
    """


class LiquidVolumeOutOfRangeError(Exception):
    """
    The requested liquid volume is outside the attached pipette's current operating range.

    Check the attached pipette model, tip capacity, and liquid already held in
    the tip. Use a volume within the limits reported by PipetteController and retry.
    """


class LiquidHandlingError(Exception):
    """
    The pipette failed while preparing, aspirating, dispensing, or blowing out liquid.

    Inspect the tip and liquid path, clear any obstruction or overpressure
    condition, reconcile the physical liquid state, and re-home before retrying.
    The underlying Opentrons error message is preserved for diagnosis.
    """


class LiquidNotFoundError(Exception):
    """
    The Flex pipette sensors did not detect liquid within the requested probing distance.

    Confirm that the tip is immersed over the intended well, that liquid is
    present, and that the maximum probing distance reaches the expected level.
    """


class LiquidClassNotSupportedError(Exception):
    """
    The selected verified liquid class has no definition for the attached pipette and tip rack.

    Confirm the attached Flex pipette model and provide the full Opentrons tip-rack
    URI used by the tip. Choose one of the supported verified liquid classes and retry.
    """


class NozzleConfigurationError(Exception):
    """
    The requested partial-tip nozzle layout is incompatible with the attached pipette.

    Remove any attached tips, check nozzle identifiers against the attached
    1-, 8-, or 96-channel head, provide the tip-rack diameter, and retry.
    """


def translate_liquid_errors(
    fn: typing.Callable[..., typing.Awaitable[object]],
) -> typing.Callable[..., typing.Awaitable[object]]:
    """Wrap an async liquid method, translating expected OT3API failures."""

    @functools.wraps(fn)
    async def wrapper(*args: object, **kwargs: object) -> object:
        from opentrons.types import PipetteNotAttachedError as OpentronsPipetteNotAttachedError
        from opentrons_shared_data.errors.exceptions import PipetteLiquidNotFoundError, PipetteOverpressureError

        try:
            return await fn(*args, **kwargs)
        except OpentronsPipetteNotAttachedError as exc:
            msg = f"{exc} Attach a Flex pipette, re-scan instruments, and retry."
            raise PipetteNotAttachedError(msg) from exc
        except PipetteLiquidNotFoundError as exc:
            raise LiquidNotFoundError(str(exc)) from exc
        except PipetteOverpressureError as exc:
            msg = f"{exc} Clear the obstruction or overpressure condition, then reconcile the liquid state."
            raise LiquidHandlingError(msg) from exc

    return wrapper


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
            msg = f"{e} Attach a Flex pipette, re-scan instruments, and retry."
            raise PipetteNotAttachedError(msg) from e
        except (TipPickupFailedError, UnexpectedTipAttachError) as e:
            msg = f"{e} Check alignment, tip compatibility, and tip-rack seating before retrying."
            raise TipPickupError(msg) from e
        except (TipDropFailedError, UnexpectedTipRemovalError) as e:
            msg = f"{e} Check the drop location and clear any jam before retrying from a safe position."
            raise TipDropError(msg) from e
        except (UnmatchedTipPresenceStates, FailedTipStateCheck) as e:
            msg = f"{e} Inspect the pipette and reconcile the physical tip state before retrying."
            raise TipStateError(msg) from e

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


class DirectGripperControlDisabledError(Exception):
    """
    Direct gripper actuation is disabled while the durable labware plan registry is active.

    Use LabwareMovementController for allowlisted moves. To perform maintenance,
    stop the connector and explicitly disable the local labware movement configuration.
    """


class LabwareMovementNotAllowedError(Exception):
    """
    The requested labware move is unsafe for the declared deck or module state.

    Reconcile deck occupancy, open any required module lid or latch, clear the
    gripper path, and submit a new movement plan before retrying.
    """


class DestinationOccupiedError(Exception):
    """
    The requested destination is occupied in the supplied deck snapshot.

    Remove or relocate the occupying item, refresh the deck occupancy snapshot,
    and retry. The connector will not overwrite an occupied location.
    """


class LabwareNotPickedError(Exception):
    """
    Gripper jaw-width verification indicates that the labware was not picked up.

    Inspect the source alignment and labware geometry, clear obstructions, then
    fully home the robot and gripper jaw before retrying.
    """


class LabwareNotPlacedError(Exception):
    """
    The gripper did not complete a verified placement at the destination.

    Inspect both the gripper and destination, reconcile the physical deck state,
    then fully home the robot and gripper jaw before any further movement.
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
