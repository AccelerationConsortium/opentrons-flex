"""Absorbance Reader module IO wrapper."""

import logging

from opentrons.drivers.types import ABSMeasurementMode

from ._errors import ModuleOperationError
from ._module_base import ModuleControllerBase
from ._types import AbsorbanceMeasurement, AbsorbanceMeasurementRow, AbsorbanceReaderState

log = logging.getLogger(__name__)


class AbsorbanceReaderController(ModuleControllerBase):
    """Controller for the Flex Absorbance Reader module."""

    @staticmethod
    def _module_only() -> ModuleOperationError:
        return ModuleOperationError(
            "Absorbance Reader control requires an attached module from OT3API.attached_modules. "
            "Start the connector through the Flex OT3API path and attach the module before retrying."
        )

    async def configure_measurement(
        self,
        mode: ABSMeasurementMode,
        wavelengths: list[int],
        reference_wavelength: int | None = None,
    ) -> None:
        """Configure measurement mode and wavelengths."""
        if self._module is None:
            raise self._module_only()
        await self._module.set_sample_wavelength(mode, wavelengths, reference_wavelength)

    async def start_measure(self) -> AbsorbanceMeasurement:
        """Run a plate measurement and return the absorbance matrix."""
        if self._module is None:
            raise self._module_only()
        rows = await self._module.start_measure()
        return AbsorbanceMeasurement(rows=[AbsorbanceMeasurementRow(values=list(row)) for row in rows])

    async def deactivate(self) -> None:
        """Deactivate the reader."""
        if self._module is None:
            raise self._module_only()
        await self._module.deactivate()

    async def get_state(self) -> AbsorbanceReaderState:
        """Get the current reader state."""
        if self._module is None:
            raise self._module_only()
        config = self._module.measurement_config
        return AbsorbanceReaderState(
            status=self._module.status.value,
            lid_status=self._module.lid_status.value,
            plate_presence=self._module.plate_presence.value,
            supported_wavelengths=list(self._module.supported_wavelengths),
            measurement_mode=config.measure_mode.value if config else "",
            sample_wavelengths=list(config.sample_wavelengths) if config else [],
            reference_wavelength=config.reference_wavelength if config else None,
        )
