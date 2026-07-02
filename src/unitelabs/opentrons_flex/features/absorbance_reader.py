"""SiLA2 feature for the Flex Absorbance Reader module."""

import enum
import typing
from dataclasses import dataclass

from opentrons.drivers.types import ABSMeasurementMode
from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    AbsorbanceMeasurement,
    AbsorbanceReaderController,
    DeviceInfo,
)
from ._progress import OperationProgress, run_observable

_WavelengthNm = typing.Annotated[int, constraints.MinimalInclusive(1)]


class MeasurementMode(enum.Enum):
    """Absorbance Reader measurement mode."""

    SINGLE = "single"
    MULTI = "multi"


class ReaderStatus(enum.Enum):
    """Absorbance Reader status."""

    IDLE = "idle"
    MEASURING = "measuring"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "ReaderStatus":
        return cls.UNKNOWN


class ReaderLidStatus(enum.Enum):
    """Absorbance Reader lid status."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "ReaderLidStatus":
        return cls.UNKNOWN


class PlatePresence(enum.Enum):
    """Absorbance Reader plate-presence status."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "PlatePresence":
        return cls.UNKNOWN


@dataclass
class AbsorbanceReaderStatus:
    """Current Absorbance Reader status."""

    status: ReaderStatus
    lid_status: ReaderLidStatus
    plate_presence: PlatePresence
    supported_wavelengths: list[int]
    measurement_mode: MeasurementMode
    sample_wavelengths: list[int]
    reference_wavelength_nm: int


class _RawAbsorbanceReaderState(typing.Protocol):
    status: str
    lid_status: str
    plate_presence: str
    supported_wavelengths: list[int]
    measurement_mode: str
    sample_wavelengths: list[int]
    reference_wavelength: int | None


def _measurement_mode(mode: MeasurementMode) -> ABSMeasurementMode:
    return ABSMeasurementMode.SINGLE if mode is MeasurementMode.SINGLE else ABSMeasurementMode.MULTI


def _status(raw: object) -> AbsorbanceReaderStatus:
    state = typing.cast(_RawAbsorbanceReaderState, raw)
    mode = MeasurementMode(state.measurement_mode) if state.measurement_mode else MeasurementMode.SINGLE
    return AbsorbanceReaderStatus(
        status=ReaderStatus(state.status),
        lid_status=ReaderLidStatus(state.lid_status),
        plate_presence=PlatePresence(state.plate_presence),
        supported_wavelengths=list(state.supported_wavelengths),
        measurement_mode=mode,
        sample_wavelengths=list(state.sample_wavelengths),
        reference_wavelength_nm=int(state.reference_wavelength or 0),
    )


class AbsorbanceReaderFeature(sila.Feature):
    """SiLA2 feature for configuring and reading the Flex Absorbance Reader."""

    def __init__(self, controller: AbsorbanceReaderController):
        super().__init__(originator="ca.accelerationconsortium", category="modules")
        self._controller = controller

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def configure_measurement(
        self,
        mode: MeasurementMode,
        wavelengths_nm: list[_WavelengthNm],
        reference_wavelength_nm: typing.Annotated[int, constraints.MinimalInclusive(0)],
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceReaderStatus:
        """
        Configure the measurement mode and sample wavelengths.

        Args:
            mode: Single-wavelength or multi-wavelength measurement mode.
            wavelengths_nm: Sample wavelength or wavelengths in nm.
            reference_wavelength_nm: Reference wavelength in nm, or 0 for none.

        Yields:
            Update: Current configuration progress update.

        Returns:
            Reader state after configuration.
        """
        await run_observable(
            status,
            intermediate,
            "Configuring Absorbance Reader measurement.",
            "Absorbance Reader measurement configured.",
            "Absorbance Reader measurement configuration cancelled.",
            self._controller.configure_measurement(
                mode=_measurement_mode(mode),
                wavelengths=wavelengths_nm,
                reference_wavelength=reference_wavelength_nm if reference_wavelength_nm > 0 else None,
            ),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def start_measure(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceMeasurement:
        """
        Start a measurement and return the absorbance matrix.

        Yields:
            Update: Current measurement progress update.

        Returns:
            Absorbance values as rows returned by the module.
        """
        return await run_observable(
            status,
            intermediate,
            "Starting Absorbance Reader measurement.",
            "Absorbance Reader measurement completed.",
            "Absorbance Reader measurement cancelled.",
            self._controller.start_measure(),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceReaderStatus:
        """
        Deactivate the Absorbance Reader.

        Yields:
            Update: Current deactivation progress update.

        Returns:
            Reader state after deactivation.
        """
        await run_observable(
            status,
            intermediate,
            "Deactivating Absorbance Reader.",
            "Absorbance Reader deactivated.",
            "Absorbance Reader deactivation cancelled.",
            self._controller.deactivate(),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_status(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> AbsorbanceReaderStatus:
        """
        Get Absorbance Reader state.

        Yields:
            Update: Current status-read progress update.

        Returns:
            Reader state.
        """
        return _status(
            await run_observable(
                status,
                intermediate,
                "Reading Absorbance Reader status.",
                "Absorbance Reader status read.",
                "Absorbance Reader status read cancelled.",
                self._controller.get_state(),
            )
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_device_info(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> DeviceInfo:
        """
        Get Absorbance Reader device information.

        Yields:
            Update: Current device-info read progress update.

        Returns:
            Serial number, model, and firmware version.
        """
        return await run_observable(
            status,
            intermediate,
            "Reading Absorbance Reader device information.",
            "Absorbance Reader device information read.",
            "Absorbance Reader device information read cancelled.",
            self._controller.get_device_info(),
        )
