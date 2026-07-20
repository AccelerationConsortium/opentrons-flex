"""Absorbance Plate Reader workflow controller."""

import asyncio

from opentrons.drivers.types import AbsorbanceReaderLidStatus, AbsorbanceReaderPlatePresence

from ._errors import InvalidWavelengthError, ModuleOperationError, PlateReaderNotReadyError
from ._module_base import ModuleControllerBase, complete_before_cancellation
from ._types import (
    AbsorbanceMeasurement,
    AbsorbanceMeasurementMode,
    AbsorbanceMeasurementRow,
    AbsorbanceReaderState,
    DeviceInfo,
)
from .absorbance_reader_backend import AbsorbanceReaderBackend, build_absorbance_reader_backend

_WELL_COUNT = 96


class AbsorbanceReaderController(ModuleControllerBase):
    """Control a Flex Absorbance Plate Reader through an attached OT3API module."""

    def __init__(
        self,
        driver: object = None,
        module: object = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        super().__init__(driver=driver, module=module, lock=lock)
        self._backend: AbsorbanceReaderBackend | None = (
            build_absorbance_reader_backend(module) if module is not None else None
        )

    @staticmethod
    def _module_only() -> ModuleOperationError:
        return ModuleOperationError(
            "Absorbance Plate Reader control requires an attached module from OT3API.attached_modules. "
            "Start the connector through the Flex OT3API path and attach the module before retrying."
        )

    def _require_module(self) -> object:
        if self._module is None:
            raise self._module_only()
        return self._module

    def _require_backend(self) -> AbsorbanceReaderBackend:
        self._require_module()
        if self._backend is None:
            raise self._module_only()
        return self._backend

    @property
    def state(self) -> AbsorbanceReaderState:
        """Return the latest reader state from the selected transport adapter."""
        return self._require_backend().state

    @property
    def device_info(self) -> DeviceInfo:
        """Return the attached reader identity."""
        return self._require_backend().device_info

    def _validate_wavelengths(
        self,
        mode: AbsorbanceMeasurementMode,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        supported = set(self.state.supported_wavelengths)
        if not supported:
            message = (
                "The reader has not reported its supported wavelengths yet. "
                "Wait for module discovery to finish and retry."
            )
            raise PlateReaderNotReadyError(message)

        requested = set(wavelengths)
        unsupported = sorted(requested - supported)
        if reference_wavelength is not None and reference_wavelength not in supported:
            unsupported.append(reference_wavelength)
        if unsupported:
            values = ", ".join(str(value) for value in sorted(set(unsupported)))
            available = ", ".join(str(value) for value in sorted(supported))
            message = f"Unsupported wavelength(s): {values} nm. This reader reports: {available} nm."
            raise InvalidWavelengthError(message)
        if len(requested) != len(wavelengths):
            message = "Duplicate sample wavelengths are not allowed."
            raise InvalidWavelengthError(message)
        if mode is AbsorbanceMeasurementMode.SINGLE and len(wavelengths) != 1:
            message = "Single-wavelength initialization requires exactly one sample wavelength."
            raise InvalidWavelengthError(message)
        if mode is AbsorbanceMeasurementMode.MULTI and not 1 <= len(wavelengths) <= 6:
            message = "Multi-wavelength initialization requires between one and six sample wavelengths."
            raise InvalidWavelengthError(message)
        if mode is AbsorbanceMeasurementMode.MULTI and reference_wavelength is not None:
            message = "A reference wavelength is only valid for single-wavelength initialization."
            raise InvalidWavelengthError(message)

    async def _refresh_physical_state(self) -> None:
        """Refresh safety interlocks immediately instead of waiting for the 2 s poller."""
        await self._require_backend().refresh_state()

    def _assert_initialization_ready(self) -> None:
        state = self.state
        if state.lid_status != AbsorbanceReaderLidStatus.ON.value:
            message = (
                "Initialization requires the reader lid to be on. "
                "Move it with an allowlisted gripper lid plan and retry."
            )
            raise PlateReaderNotReadyError(message)
        if state.plate_presence != AbsorbanceReaderPlatePresence.ABSENT.value:
            message = (
                "Initialization requires an empty reader. "
                "Remove the plate, replace the lid with the gripper, and retry."
            )
            raise PlateReaderNotReadyError(message)

    async def initialize_measurement(
        self,
        mode: AbsorbanceMeasurementMode,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        """Take the blank reading that initializes a measurement configuration."""
        backend = self._require_backend()
        self._validate_wavelengths(mode, wavelengths, reference_wavelength)
        await self._refresh_physical_state()
        self._assert_initialization_ready()

        try:
            await complete_before_cancellation(
                backend.initialize(mode, wavelengths, reference_wavelength),
                "Absorbance Reader initialization",
            )
        except RuntimeError as error:
            raise ModuleOperationError(str(error)) from error

    async def read_plate(self) -> AbsorbanceMeasurement:
        """Read a 96-well plate and label each result row with its wavelength."""
        backend = self._require_backend()
        await self._refresh_physical_state()
        state = self.state
        if not state.sample_wavelengths:
            message = "Initialize the reader before requesting a plate measurement."
            raise PlateReaderNotReadyError(message)
        if state.lid_status != AbsorbanceReaderLidStatus.ON.value:
            message = "A plate measurement requires the reader lid to be on. Move it with the gripper and retry."
            raise PlateReaderNotReadyError(message)
        if state.plate_presence != AbsorbanceReaderPlatePresence.PRESENT.value:
            message = "No plate is detected. Place a compatible 96-well plate in the reader and retry."
            raise PlateReaderNotReadyError(message)

        try:
            rows = await complete_before_cancellation(backend.measure(), "Absorbance Reader measurement")
        except RuntimeError as error:
            raise ModuleOperationError(str(error)) from error

        wavelengths = state.sample_wavelengths
        if len(rows) != len(wavelengths):
            message = (
                f"Reader returned {len(rows)} result rows for {len(wavelengths)} configured wavelengths. "
                "Power-cycle the module and retry initialization."
            )
            raise ModuleOperationError(message)

        labeled_rows: list[AbsorbanceMeasurementRow] = []
        for wavelength, row in zip(wavelengths, rows, strict=True):
            values = list(row)
            if len(values) != _WELL_COUNT:
                message = (
                    f"Reader returned {len(values)} wells at {wavelength} nm; expected {_WELL_COUNT}. "
                    "Power-cycle the module and repeat the measurement."
                )
                raise ModuleOperationError(message)
            labeled_rows.append(AbsorbanceMeasurementRow(wavelength=wavelength, values=values))
        return AbsorbanceMeasurement(rows=labeled_rows)

    async def deactivate(self) -> None:
        """Deactivate the reader."""
        await self._require_backend().deactivate()
