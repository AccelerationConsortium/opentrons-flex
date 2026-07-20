"""SiLA 2 feature for the Flex Absorbance Plate Reader."""

import enum
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    AbsorbanceMeasurement,
    AbsorbanceMeasurementMode,
    AbsorbanceReaderController,
    DeviceInfo,
    InvalidWavelengthError,
    PlateReaderNotReadyError,
)
from ._progress import OperationProgress, run_observable
from ._subscriptions import stream_changes

_NANOMETRE = constraints.Unit(
    "nm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=1e-9,
)
_ABSORBANCE_UNIT = constraints.Unit(
    "AU",
    [constraints.Unit.Component(constraints.Unit.SI.DIMENSIONLESS)],
)
_WavelengthReading = typing.Annotated[int, _NANOMETRE]
_Absorbance = typing.Annotated[float, _ABSORBANCE_UNIT]
_Wavelength = typing.Annotated[
    int,
    constraints.MinimalInclusive(350),
    constraints.MaximalInclusive(1000),
    _NANOMETRE,
]
_MultipleWavelengths = typing.Annotated[
    list[_Wavelength],
    constraints.MinimalElementCount(1),
    constraints.MaximalElementCount(6),
]
_WellIdentifier = typing.Annotated[str, constraints.Pattern(r"[A-H](?:[1-9]|1[0-2])")]
_READER_ERRORS = (*COMMON_MODULE_ERRORS, InvalidWavelengthError, PlateReaderNotReadyError)


class MeasurementMode(enum.Enum):
    """Absorbance measurement mode reported in status."""

    UNCONFIGURED = "unconfigured"
    SINGLE = "single"
    MULTI = "multi"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "MeasurementMode":
        return cls.UNKNOWN


class ReaderStatus(enum.Enum):
    """Absorbance Plate Reader status."""

    IDLE = "idle"
    MEASURING = "measuring"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "ReaderStatus":
        return cls.UNKNOWN


class ReaderLidStatus(enum.Enum):
    """Absorbance Plate Reader lid location."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "ReaderLidStatus":
        return cls.UNKNOWN


class PlatePresence(enum.Enum):
    """Absorbance Plate Reader plate-presence state."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "PlatePresence":
        return cls.UNKNOWN


@dataclass
class AbsorbanceReaderStatus:
    """Current configuration and physical state of the reader."""

    status: ReaderStatus
    lid_status: ReaderLidStatus
    plate_presence: PlatePresence
    supported_wavelengths: list[_Wavelength]
    measurement_mode: MeasurementMode
    sample_wavelengths: list[_Wavelength]
    reference_wavelength: _WavelengthReading
    reference_active: bool


@dataclass
class WellAbsorbance:
    """One absorbance result associated with a 96-well plate position."""

    well_identifier: _WellIdentifier
    absorbance: _Absorbance


_WellMeasurements = typing.Annotated[
    list[WellAbsorbance],
    constraints.MinimalElementCount(96),
    constraints.MaximalElementCount(96),
]


@dataclass
class WavelengthMeasurement:
    """Absorbance results for all wells at one wavelength."""

    wavelength: _Wavelength
    wells: _WellMeasurements


_WavelengthMeasurements = typing.Annotated[
    list[WavelengthMeasurement],
    constraints.MinimalElementCount(1),
    constraints.MaximalElementCount(6),
]


@dataclass
class PlateMeasurement:
    """Structured Absorbance Plate Reader result grouped by wavelength."""

    measurements: _WavelengthMeasurements


class _RawAbsorbanceReaderState(typing.Protocol):
    status: str
    lid_status: str
    plate_presence: str
    supported_wavelengths: list[int]
    measurement_mode: str
    sample_wavelengths: list[int]
    reference_wavelength: int | None


def _status(raw: object) -> AbsorbanceReaderStatus:
    state = typing.cast(_RawAbsorbanceReaderState, raw)
    mode = MeasurementMode(state.measurement_mode) if state.measurement_mode else MeasurementMode.UNCONFIGURED
    return AbsorbanceReaderStatus(
        status=ReaderStatus(state.status),
        lid_status=ReaderLidStatus(state.lid_status),
        plate_presence=PlatePresence(state.plate_presence),
        supported_wavelengths=list(state.supported_wavelengths),
        measurement_mode=mode,
        sample_wavelengths=list(state.sample_wavelengths),
        reference_wavelength=state.reference_wavelength or 0,
        reference_active=state.reference_wavelength is not None,
    )


def _measurement(raw: AbsorbanceMeasurement) -> PlateMeasurement:
    measurements: list[WavelengthMeasurement] = []
    for row in raw.rows:
        # The reader is installed rotated by 180 degrees. Opentrons' Protocol
        # Engine applies the same reversal before assigning A1 through H12.
        values = list(reversed(row.values))
        wells = [
            WellAbsorbance(
                well_identifier=f"{chr(ord('A') + index // 12)}{index % 12 + 1}",
                absorbance=value,
            )
            for index, value in enumerate(values)
        ]
        measurements.append(WavelengthMeasurement(wavelength=row.wavelength, wells=wells))
    return PlateMeasurement(measurements=measurements)


class AbsorbanceReaderFeature(sila.Feature):
    """Initialize and read a Flex Absorbance Plate Reader through SiLA 2."""

    def __init__(self, controller: AbsorbanceReaderController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="modules",
            identifier="AbsorbanceReaderController",
            name="Absorbance Reader Controller",
            version="2.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=_READER_ERRORS)
    async def initialize_single(
        self,
        wavelength: _Wavelength,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceReaderStatus:
        """Initialize an empty, covered reader for one sample wavelength."""
        await run_observable(
            status,
            intermediate,
            f"Initializing the reader at {wavelength} nm.",
            "Reader initialization completed.",
            "Reader initialization cancelled.",
            self._controller.initialize_measurement(AbsorbanceMeasurementMode.SINGLE, [wavelength], None),
        )
        return _status(self._controller.state)

    @sila.ObservableCommand(errors=_READER_ERRORS)
    async def initialize_single_with_reference(
        self,
        wavelength: _Wavelength,
        reference_wavelength: _Wavelength,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceReaderStatus:
        """Initialize one sample wavelength with a reference subtraction wavelength."""
        await run_observable(
            status,
            intermediate,
            f"Initializing the reader at {wavelength} nm with {reference_wavelength} nm reference.",
            "Referenced reader initialization completed.",
            "Referenced reader initialization cancelled.",
            self._controller.initialize_measurement(
                AbsorbanceMeasurementMode.SINGLE,
                [wavelength],
                reference_wavelength,
            ),
        )
        return _status(self._controller.state)

    @sila.ObservableCommand(errors=_READER_ERRORS)
    async def initialize_multiple(
        self,
        wavelengths: _MultipleWavelengths,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceReaderStatus:
        """Initialize an empty, covered reader for one to six sample wavelengths."""
        await run_observable(
            status,
            intermediate,
            "Initializing the reader for multiple wavelengths.",
            "Multi-wavelength reader initialization completed.",
            "Multi-wavelength reader initialization cancelled.",
            self._controller.initialize_measurement(AbsorbanceMeasurementMode.MULTI, list(wavelengths), None),
        )
        return _status(self._controller.state)

    @sila.ObservableCommand(errors=_READER_ERRORS)
    async def read_plate(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> PlateMeasurement:
        """Measure a covered 96-well plate using the active initialization."""
        raw = await run_observable(
            status,
            intermediate,
            "Starting the plate measurement.",
            "Plate measurement completed.",
            "Plate measurement cancelled.",
            self._controller.read_plate(),
        )
        return _measurement(raw)

    @sila.ObservableCommand(errors=_READER_ERRORS)
    async def deactivate(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceReaderStatus:
        """Deactivate the reader and return its latest state."""
        await run_observable(
            status,
            intermediate,
            "Deactivating the reader.",
            "Reader deactivated.",
            "Reader deactivation cancelled.",
            self._controller.deactivate(),
        )
        return _status(self._controller.state)

    @sila.ObservableProperty(errors=_READER_ERRORS)
    async def subscribe_status(self) -> sila.Stream[AbsorbanceReaderStatus]:
        """Subscribe to physical, operational, and configuration state changes."""
        async for value in stream_changes(lambda: _status(self._controller.state)):
            yield value

    @sila.UnobservableProperty(errors=COMMON_MODULE_ERRORS)
    def device_info(self) -> DeviceInfo:
        """Return the attached reader serial number, model, and firmware version."""
        return self._controller.device_info
