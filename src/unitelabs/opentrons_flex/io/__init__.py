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
    GripActionError,
    GripperNotAttachedError,
    MachineErrorStateError,
    ModuleNotRespondingError,
    ModuleOperationError,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    StallDetectedError,
    TipDropError,
    TipPickupError,
    TipStateError,
)
from ._types import (
    AbsorbanceMeasurement,
    AbsorbanceMeasurementRow,
    AbsorbanceReaderState,
    DeviceInfo,
    FlexStackerLimitSwitches,
    FlexStackerState,
    RPM,
    Temperature,
)
from .absorbance_reader import AbsorbanceReaderController
from .calibration import FlexCalibrationController
from .flex_motion import Axis, FlexMotionController, MachineState, OT3Mount, Point, TipStateType
from .flex_stacker import FlexStackerController
from .gripper import FlexGripperController
from .hardware_proxy import HardwareProxy
from .heater_shaker import HeaterShakerController
from .modules import scan_module_ports
from .temperature_module import TemperatureModuleController
from .thermocycler import ThermocyclerController

__all__ = [
    "COMMON_MODULE_ERRORS",
    "RPM",
    "AbsorbanceMeasurement",
    "AbsorbanceMeasurementRow",
    "AbsorbanceReaderController",
    "AbsorbanceReaderState",
    "Axis",
    "CalibrationFailedError",
    "CalibrationProbeNotAttachedError",
    "DeviceInfo",
    "FlexCalibrationController",
    "FlexGripperController",
    "FlexMotionController",
    "FlexStackerController",
    "FlexStackerLimitSwitches",
    "FlexStackerState",
    "GripActionError",
    "GripperNotAttachedError",
    "HardwareProxy",
    "HeaterShakerController",
    "MachineErrorStateError",
    "MachineState",
    "ModuleNotRespondingError",
    "ModuleOperationError",
    "MovementOutOfBoundsError",
    "NotHomedError",
    "OT3Mount",
    "PipetteNotAttachedError",
    "Point",
    "StallDetectedError",
    "Temperature",
    "TemperatureModuleController",
    "ThermocyclerController",
    "TipDropError",
    "TipPickupError",
    "TipStateError",
    "TipStateType",
    "scan_module_ports",
]
