"""Stable transport adapters for Opentrons Absorbance Reader modules."""

import typing
from importlib.metadata import PackageNotFoundError, version

from opentrons.drivers.types import ABSMeasurementConfig, ABSMeasurementMode

from ._errors import ModuleOperationError
from ._types import AbsorbanceMeasurementMode, AbsorbanceReaderState, DeviceInfo

_WELL_COUNT = 96
_REFERENCE_WORKAROUND_VERSIONS = frozenset({"8.8.1", "9.0.0"})


def _opentrons_version() -> str:
    try:
        return version("opentrons")
    except PackageNotFoundError:
        return ""


def _driver_mode(mode: AbsorbanceMeasurementMode) -> ABSMeasurementMode:
    return ABSMeasurementMode.SINGLE if mode is AbsorbanceMeasurementMode.SINGLE else ABSMeasurementMode.MULTI


class AbsorbanceReaderBackend(typing.Protocol):
    """Connector-owned contract implemented by real and simulated transports."""

    @property
    def state(self) -> AbsorbanceReaderState:
        """Return current transport state."""
        ...

    @property
    def device_info(self) -> DeviceInfo:
        """Return transport identity."""
        ...

    async def refresh_state(self) -> None:
        """Refresh safety interlocks."""
        ...

    async def initialize(
        self,
        mode: AbsorbanceMeasurementMode,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        """Initialize a blank measurement configuration."""
        ...

    async def measure(self) -> list[list[float]]:
        """Measure one plate."""
        ...

    async def deactivate(self) -> None:
        """Deactivate the reader."""
        ...


class OpentronsAbsorbanceReaderBackend:
    """Adapter around the version-pinned Opentrons module abstraction."""

    def __init__(self, module: object) -> None:
        self._module = module

    @property
    def state(self) -> AbsorbanceReaderState:
        """Return the state exposed by the Opentrons module poller."""
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

    @property
    def device_info(self) -> DeviceInfo:
        """Return the attached reader identity."""
        return DeviceInfo.from_dict(dict(self._module.device_info))

    async def refresh_state(self) -> None:
        """Refresh physical interlocks immediately instead of waiting for the poller."""
        await self._module.get_current_lid_status()
        # Opentrons 8.8.1/9.0.0 do not expose an on-demand plate refresh on
        # AbsorbanceReader itself. Keep the private compatibility seam isolated
        # here so the feature and workflow controller use only this stable API.
        reader = getattr(self._module, "_reader", None)
        refresh_plate_presence = getattr(reader, "get_plate_presence", None)
        if refresh_plate_presence is None:
            message = (
                "The installed Opentrons hardware package cannot refresh plate presence on demand. "
                "Update to a connector-supported Opentrons build before operating the reader."
            )
            raise ModuleOperationError(message)
        await refresh_plate_presence()

    async def initialize(
        self,
        mode: AbsorbanceMeasurementMode,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        """Initialize through the public module surface or pinned workaround."""
        driver_mode = _driver_mode(mode)
        if reference_wavelength is None or _opentrons_version() not in _REFERENCE_WORKAROUND_VERSIONS:
            await self._module.set_sample_wavelength(driver_mode, wavelengths, reference_wavelength)
            return

        # In the pinned supported releases, AbsorbanceReader.set_sample_wavelength()
        # records the reference in memory but drops it from the driver call. Keep
        # this compatibility workaround behind the adapter boundary.
        driver = getattr(self._module, "_driver", None)
        initialize = getattr(driver, "initialize_measurement", None)
        if initialize is None:
            message = (
                "The installed Opentrons hardware package cannot initialize a reference wavelength. "
                "Update to a connector-supported Opentrons build and retry."
            )
            raise ModuleOperationError(message)
        await initialize(wavelengths, driver_mode, reference_wavelength)
        self._module._measurement_config = ABSMeasurementConfig(
            measure_mode=driver_mode,
            sample_wavelengths=wavelengths,
            reference_wavelength=reference_wavelength,
        )

    async def measure(self) -> list[list[float]]:
        """Return raw wavelength rows from the module."""
        return await self._module.start_measure()

    async def deactivate(self) -> None:
        """Deactivate through the module abstraction."""
        await self._module.deactivate()


class SimulatingAbsorbanceReaderBackend(OpentronsAbsorbanceReaderBackend):
    """Explicit simulator transport with the same safety/result contract as hardware."""

    def __init__(self, module: object) -> None:
        super().__init__(module)
        self._blank_completed = False

    @property
    def state(self) -> AbsorbanceReaderState:
        """Return realistic simulated plate-presence state."""
        raw = super().state
        return AbsorbanceReaderState(
            status=raw.status,
            lid_status=raw.lid_status,
            # Opentrons' module simulator reports a permanently present plate.
            # Model the blank as empty, then model a plate after initialization.
            plate_presence="present" if self._blank_completed else "absent",
            supported_wavelengths=raw.supported_wavelengths,
            measurement_mode=raw.measurement_mode,
            sample_wavelengths=raw.sample_wavelengths,
            reference_wavelength=raw.reference_wavelength,
        )

    async def refresh_state(self) -> None:
        """Refresh the simulated lid without using physical plate polling."""
        await self._module.get_current_lid_status()

    async def initialize(
        self,
        mode: AbsorbanceMeasurementMode,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        """Complete the simulated blank and transition to plate-present state."""
        await super().initialize(mode, wavelengths, reference_wavelength)
        self._blank_completed = True

    async def measure(self) -> list[list[float]]:
        """Return a deterministic 96-well matrix per configured wavelength."""
        # Exercise the upstream simulator call, then normalize its one-scalar
        # placeholder into the physical 96-well matrix contract.
        await self._module.start_measure()
        return [[0.0] * _WELL_COUNT for _ in self.state.sample_wavelengths]


def build_absorbance_reader_backend(module: object) -> AbsorbanceReaderBackend:
    """Select a backend only from the module's explicit simulator identity."""
    if bool(getattr(module, "is_simulated", False)):
        return SimulatingAbsorbanceReaderBackend(module)
    return OpentronsAbsorbanceReaderBackend(module)
