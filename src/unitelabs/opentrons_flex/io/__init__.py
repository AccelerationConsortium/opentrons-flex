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

The Magnetic Module is not supported on the Flex (it is replaced by the passive
Magnetic Block), so there is no magnetic controller here.
"""

from ._errors import (
    COMMON_MODULE_ERRORS,
    CalibrationFailedError,
    CalibrationProbeNotAttachedError,
    EngageHeightOutOfRangeError,
    GripActionError,
    GripperNotAttachedError,
    ModuleNotRespondingError,
    ModuleOperationError,
    MovementOutOfBoundsError,
    NotHomedError,
    StallDetectedError,
)
from ._types import RPM, DeviceInfo, Temperature
from .calibration import FlexCalibrationController
from .flex_motion import Axis, FlexMotionController, OT3Mount, Point
from .gripper import FlexGripperController
from .hardware_proxy import HardwareProxy
from .heater_shaker import HeaterShakerController
from .modules import scan_module_ports
from .temperature_module import TemperatureModuleController
from .thermocycler import ThermocyclerController

__all__ = [
    "COMMON_MODULE_ERRORS",
    "RPM",
    "Axis",
    "CalibrationFailedError",
    "CalibrationProbeNotAttachedError",
    "DeviceInfo",
    "EngageHeightOutOfRangeError",
    "FlexCalibrationController",
    "FlexGripperController",
    "FlexMotionController",
    "GripActionError",
    "GripperNotAttachedError",
    "HardwareProxy",
    "HeaterShakerController",
    "ModuleNotRespondingError",
    "ModuleOperationError",
    "MovementOutOfBoundsError",
    "NotHomedError",
    "OT3Mount",
    "Point",
    "StallDetectedError",
    "Temperature",
    "TemperatureModuleController",
    "ThermocyclerController",
    "scan_module_ports",
]
