"""Shared data types for module IO wrappers."""

from dataclasses import dataclass


@dataclass
class Temperature:
    """Temperature reading."""

    current: float
    target: float | None = None


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
    """One row of Absorbance Reader measurement values."""

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
    error_details: str


@dataclass
class FlexStackerLimitSwitches:
    """Flex Stacker limit-switch states."""

    x: str
    z: str
    latch: str
