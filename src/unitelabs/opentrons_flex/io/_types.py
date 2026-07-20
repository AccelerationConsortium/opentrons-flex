"""Shared, vendor-neutral data types for module IO wrappers."""

import enum
from dataclasses import dataclass


class AbsorbanceMeasurementMode(enum.Enum):
    """Connector-owned Absorbance Reader measurement mode."""

    SINGLE = "single"
    MULTI = "multi"


class FlexStackerAxis(enum.Enum):
    """Connector-owned Flex Stacker maintenance axis."""

    X = "X"
    Z = "Z"
    LATCH = "L"


class FlexStackerDirection(enum.Enum):
    """Connector-owned Flex Stacker movement direction."""

    RETRACT = "RETRACT"
    EXTEND = "EXTEND"


class FlexStackerLedColor(enum.Enum):
    """Connector-owned Flex Stacker LED color."""

    WHITE = "WHITE"
    RED = "RED"
    GREEN = "GREEN"
    BLUE = "BLUE"
    YELLOW = "YELLOW"


class FlexStackerLedPattern(enum.Enum):
    """Connector-owned Flex Stacker LED pattern."""

    STATIC = "STATIC"
    FLASH = "FLASH"
    PULSE = "PULSE"
    CONFIRM = "CONFIRM"


@dataclass
class Temperature:
    """Temperature reading."""

    current: float
    target: float | None = None


@dataclass
class TemperatureModuleState:
    """Current Temperature Module temperature-control state."""

    status: str
    current_temperature: float
    target_temperature: float | None


@dataclass
class RPM:
    """RPM reading."""

    current: int
    target: int | None = None


@dataclass
class DeviceInfo:
    """Identifying information for an attached module."""

    serial_number: str
    model: str
    firmware_version: str

    @classmethod
    def from_dict(cls, info: dict) -> "DeviceInfo":
        """Build from an opentrons device_info mapping (keys: serial, model, version)."""
        return cls(
            serial_number=info.get("serial", ""),
            model=info.get("model", ""),
            firmware_version=info.get("version", ""),
        )


@dataclass
class AbsorbanceReaderState:
    """Current Absorbance Reader state."""

    status: str
    lid_status: str
    plate_presence: str
    supported_wavelengths: list[int]
    measurement_mode: str
    sample_wavelengths: list[int]
    reference_wavelength: int | None


@dataclass
class AbsorbanceMeasurementRow:
    """One wavelength and its row-major 96-well measurement values."""

    wavelength: int
    values: list[float]


@dataclass
class AbsorbanceMeasurement:
    """Absorbance Reader measurement matrix."""

    rows: list[AbsorbanceMeasurementRow]


@dataclass
class FlexStackerState:
    """Current Flex Stacker state."""

    status: str
    latch_state: str
    platform_state: str
    hopper_door_state: str
    install_detected: bool
    initialized: bool
    recovery_required: bool
    error_details: str


@dataclass
class FlexStackerLimitSwitches:
    """Flex Stacker limit-switch states."""

    x: str
    z: str
    latch: str
