"""Transport-level tests for the Absorbance Plate Reader controller."""

import asyncio

from types import SimpleNamespace

import pytest
from opentrons.drivers.types import ABSMeasurementConfig, ABSMeasurementMode

from unitelabs.opentrons_flex.io import (
    AbsorbanceMeasurementMode,
    AbsorbanceReaderController,
    InvalidWavelengthError,
    ModuleOperationError,
    PlateReaderNotReadyError,
)


class _Driver:
    def __init__(self) -> None:
        self.initializations: list[tuple] = []

    async def initialize_measurement(
        self,
        wavelengths: list[int],
        mode: ABSMeasurementMode,
        reference_wavelength: int | None,
    ) -> None:
        self.initializations.append((wavelengths, mode, reference_wavelength))


class _ReaderModule:
    def __init__(self, *, simulated: bool = False) -> None:
        self.is_simulated = simulated
        self.status = SimpleNamespace(value="idle")
        self.lid_status = SimpleNamespace(value="on")
        self.plate_presence = SimpleNamespace(value="absent")
        self.supported_wavelengths = [450, 570, 600, 650]
        self._measurement_config = None
        self.device_info = {"serial": "AR-1", "model": "ABS96", "version": "1.2.3"}
        self._driver = _Driver()
        self._reader = self
        self.rows: list[list[float]] = [[float(value) for value in range(96)]]
        self.deactivated = False
        self.plate_presence_refreshes = 0

    @property
    def measurement_config(self) -> ABSMeasurementConfig | None:
        return self._measurement_config

    @measurement_config.setter
    def measurement_config(self, value: ABSMeasurementConfig | None) -> None:
        self._measurement_config = value

    async def get_current_lid_status(self) -> object:
        return self.lid_status

    async def get_plate_presence(self) -> None:
        self.plate_presence_refreshes += 1

    async def set_sample_wavelength(
        self,
        mode: ABSMeasurementMode,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        self.measurement_config = ABSMeasurementConfig(mode, wavelengths, reference_wavelength)

    async def start_measure(self) -> list[list[float]]:
        return self.rows

    async def deactivate(self) -> None:
        self.deactivated = True


@pytest.mark.asyncio
async def test_initialize_and_read_labels_each_wavelength() -> None:
    module = _ReaderModule()
    controller = AbsorbanceReaderController.from_module(module)

    await controller.initialize_measurement(AbsorbanceMeasurementMode.MULTI, [450, 600], None)
    module.plate_presence.value = "present"
    module.rows = [[0.1] * 96, [0.2] * 96]
    result = await controller.read_plate()

    assert [row.wavelength for row in result.rows] == [450, 600]
    assert len(result.rows[0].values) == 96
    assert controller.state.measurement_mode == "multi"
    assert module.plate_presence_refreshes == 2


@pytest.mark.asyncio
async def test_reference_initialization_reaches_driver_and_updates_module_state() -> None:
    module = _ReaderModule()
    controller = AbsorbanceReaderController.from_module(module)

    await controller.initialize_measurement(AbsorbanceMeasurementMode.SINGLE, [450], 600)

    assert module._driver.initializations == [([450], ABSMeasurementMode.SINGLE, 600)]
    assert controller.state.reference_wavelength == 600


@pytest.mark.asyncio
async def test_unsupported_wavelength_is_a_defined_error() -> None:
    controller = AbsorbanceReaderController.from_module(_ReaderModule())

    with pytest.raises(InvalidWavelengthError, match="570"):
        await controller.initialize_measurement(AbsorbanceMeasurementMode.MULTI, [450, 562], None)


@pytest.mark.asyncio
async def test_physical_reader_must_be_empty_for_initialization() -> None:
    module = _ReaderModule()
    module.plate_presence.value = "present"
    controller = AbsorbanceReaderController.from_module(module)

    with pytest.raises(PlateReaderNotReadyError, match="empty"):
        await controller.initialize_measurement(AbsorbanceMeasurementMode.SINGLE, [450], None)


@pytest.mark.asyncio
async def test_read_requires_initialization_lid_and_plate() -> None:
    module = _ReaderModule()
    controller = AbsorbanceReaderController.from_module(module)

    with pytest.raises(PlateReaderNotReadyError, match="Initialize"):
        await controller.read_plate()

    module.measurement_config = ABSMeasurementConfig(ABSMeasurementMode.SINGLE, [450], None)
    module.lid_status.value = "off"
    with pytest.raises(PlateReaderNotReadyError, match="lid"):
        await controller.read_plate()

    module.lid_status.value = "on"
    with pytest.raises(PlateReaderNotReadyError, match="No plate"):
        await controller.read_plate()


@pytest.mark.asyncio
async def test_malformed_hardware_result_is_a_defined_error() -> None:
    module = _ReaderModule()
    module.measurement_config = ABSMeasurementConfig(ABSMeasurementMode.SINGLE, [450], None)
    module.plate_presence.value = "present"
    module.rows = [[0.0] * 95]
    controller = AbsorbanceReaderController.from_module(module)

    with pytest.raises(ModuleOperationError, match="95 wells"):
        await controller.read_plate()


@pytest.mark.asyncio
async def test_opentrons_simulator_normalizes_to_96_wells() -> None:
    module = _ReaderModule(simulated=True)
    module.plate_presence.value = "present"
    controller = AbsorbanceReaderController.from_module(module)

    await controller.initialize_measurement(AbsorbanceMeasurementMode.MULTI, [450, 600], None)
    result = await controller.read_plate()

    assert [len(row.values) for row in result.rows] == [96, 96]
    assert all(value == 0.0 for row in result.rows for value in row.values)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["initialize", "measure"])
async def test_cancellation_holds_lock_until_native_reader_action_finishes(operation: str) -> None:
    """Executor-backed reader work must settle before SiLA releases shared ownership."""
    module = _ReaderModule()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*args: object) -> object:
        started.set()
        await release.wait()
        if operation == "measure":
            return [[0.0] * 96]
        module.measurement_config = ABSMeasurementConfig(ABSMeasurementMode.SINGLE, [450], None)
        return None

    if operation == "initialize":
        module.set_sample_wavelength = blocked
    else:
        module.measurement_config = ABSMeasurementConfig(ABSMeasurementMode.SINGLE, [450], None)
        module.plate_presence.value = "present"
        module.start_measure = blocked

    shared_lock = asyncio.Lock()
    controller = AbsorbanceReaderController.from_module(module, lock=shared_lock)
    if operation == "initialize":
        action = asyncio.create_task(controller.initialize_measurement(AbsorbanceMeasurementMode.SINGLE, [450], None))
    else:
        action = asyncio.create_task(controller.read_plate())

    await started.wait()
    action.cancel()
    await asyncio.sleep(0)
    assert shared_lock.locked()
    assert not action.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await action
    assert not shared_lock.locked()
