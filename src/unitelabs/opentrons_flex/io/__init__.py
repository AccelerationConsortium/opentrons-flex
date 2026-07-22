"""
IO layer for the Opentrons Flex, built on the Opentrons ``OT3API``.

Unlike the OT-2 connector, the Flex has no Smoothie serial board: motion,
pipettes, gripper and calibration are all driven through the high-level
``HardwareControlAPI`` (``OT3API``) over CAN. These controllers are thin,
SiLA-agnostic wrappers that share one API instance and one lock.

Controllers:
- FlexMotionController:        Gantry/pipette motion, lights (OT3API)
- FlexGripperController:       Flex gripper (grip/ungrip/home jaw)
- FlexCalibrationController:   Automatic probe-based calibration (ot3_calibration)
- HeaterShakerController:      Heater-Shaker module
- ThermocyclerController:      Thermocycler module
- TemperatureModuleController: Temperature module
- AbsorbanceReaderController:  Absorbance Plate Reader module
- FlexStackerController:       Flex Stacker module

The Magnetic Module is not supported on the Flex (it is replaced by the passive
Magnetic Block), so there is no magnetic controller here.
"""

from ._errors import (
    COMMON_MODULE_ERRORS,
    CalibrationFailedError,
    CalibrationProbeNotAttachedError,
    DestinationOccupiedError,
    DirectGripperControlDisabledError,
    GripActionError,
    GripperNotAttachedError,
    LiquidClassNotSupportedError,
    LiquidHandlingError,
    LiquidNotFoundError,
    LiquidVolumeOutOfRangeError,
    LabwareMovementNotAllowedError,
    LabwareNotPickedError,
    LabwareNotPlacedError,
    InvalidWavelengthError,
    InvalidTemperatureTargetError,
    InvalidStackerConfigurationError,
    MachineErrorStateError,
    NozzleConfigurationError,
    ModuleNotRespondingError,
    ModuleOperationError,
    MovementOutOfBoundsError,
    NotHomedError,
    PlateReaderNotReadyError,
    PipetteNotAttachedError,
    RunOwnershipError,
    StallDetectedError,
    StackerNotReadyError,
    StackerMovementOutOfRangeError,
    TipDropError,
    TipNotAttachedError,
    TipPickupError,
    TipStateError,
)
from ._types import (
    AbsorbanceMeasurement,
    AbsorbanceMeasurementMode,
    AbsorbanceMeasurementRow,
    AbsorbanceReaderState,
    DeviceInfo,
    FlexStackerLimitSwitches,
    FlexStackerAxis,
    FlexStackerDirection,
    FlexStackerLedColor,
    FlexStackerLedPattern,
    FlexStackerState,
    RPM,
    Temperature,
    TemperatureModuleState,
)
from .absorbance_reader import AbsorbanceReaderController
from .calibration import FlexCalibrationController
from .flex_motion import (
    Axis,
    FlexMotionController,
    MachineState,
    NozzleConfigurationState,
    OT3Mount,
    Point,
    TipStateType,
)
from .flex_stacker import FlexStackerController
from .gripper import FlexGripperController
from .hardware_proxy import HardwareProxy
from .heater_shaker import HeaterShakerController
from .labware_movement import (
    FlexLabwareMovementController,
    LabwareGripGeometry,
    LabwareMovementPlan,
    LabwareMoveResult,
    LoadedLabwareMovementConfig,
    load_labware_movement_config,
)
from .labware_state import LabwareMovementState
from .liquid_handling import (
    FlexLiquidHandlingController,
    LiquidTransferProfile,
    LiquidWellGeometry,
    VerifiedLiquidTransfer,
)
from .modules import scan_module_ports
from .temperature_module import TemperatureModuleController
from .thermocycler import ThermocyclerController

__all__ = [
    "COMMON_MODULE_ERRORS",
    "RPM",
    "AbsorbanceMeasurement",
    "AbsorbanceMeasurementMode",
    "AbsorbanceMeasurementRow",
    "AbsorbanceReaderController",
    "AbsorbanceReaderState",
    "Axis",
    "CalibrationFailedError",
    "CalibrationProbeNotAttachedError",
    "DestinationOccupiedError",
    "DeviceInfo",
    "DirectGripperControlDisabledError",
    "FlexCalibrationController",
    "FlexGripperController",
    "FlexLabwareMovementController",
    "FlexLiquidHandlingController",
    "FlexMotionController",
    "FlexStackerAxis",
    "FlexStackerController",
    "FlexStackerDirection",
    "FlexStackerLedColor",
    "FlexStackerLedPattern",
    "FlexStackerLimitSwitches",
    "FlexStackerState",
    "GripActionError",
    "GripperNotAttachedError",
    "HardwareProxy",
    "HeaterShakerController",
    "InvalidStackerConfigurationError",
    "InvalidTemperatureTargetError",
    "InvalidWavelengthError",
    "LabwareGripGeometry",
    "LabwareMoveResult",
    "LabwareMovementNotAllowedError",
    "LabwareMovementPlan",
    "LabwareMovementState",
    "LabwareNotPickedError",
    "LabwareNotPlacedError",
    "LiquidClassNotSupportedError",
    "LiquidHandlingError",
    "LiquidNotFoundError",
    "LiquidTransferProfile",
    "LiquidVolumeOutOfRangeError",
    "LiquidWellGeometry",
    "LoadedLabwareMovementConfig",
    "MachineErrorStateError",
    "MachineState",
    "ModuleNotRespondingError",
    "ModuleOperationError",
    "MovementOutOfBoundsError",
    "NotHomedError",
    "NozzleConfigurationError",
    "NozzleConfigurationState",
    "OT3Mount",
    "PipetteNotAttachedError",
    "PlateReaderNotReadyError",
    "Point",
    "RunOwnershipError",
    "StackerMovementOutOfRangeError",
    "StackerNotReadyError",
    "StallDetectedError",
    "Temperature",
    "TemperatureModuleController",
    "TemperatureModuleState",
    "ThermocyclerController",
    "TipDropError",
    "TipNotAttachedError",
    "TipPickupError",
    "TipStateError",
    "TipStateType",
    "VerifiedLiquidTransfer",
    "load_labware_movement_config",
    "scan_module_ports",
]
