"""Temperature Module GEN2 controller tests independent of physical hardware."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from unitelabs.opentrons_flex.io import (
    InvalidTemperatureTargetError,
    ModuleOperationError,
    TemperatureModuleController,
)


class _TemperatureDriver:
    """Minimal low-level driver double used to verify controller dispatch."""

    def __init__(self, current: float = 25.0, target: float | None = None) -> None:
        self.current = current
        self.target = target
        self.calls: list[tuple] = []

    async def set_temperature(self, celsius: float) -> None:
        self.calls.append(("set_temperature", celsius))
        self.target = celsius

    async def get_temperature(self) -> SimpleNamespace:
        self.calls.append(("get_temperature",))
        return SimpleNamespace(current=self.current, target=self.target)

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))
        self.target = None

    async def connect(self) -> None:
        self.calls.append(("connect",))

    async def get_device_info(self) -> dict[str, str]:
        self.calls.append(("get_device_info",))
        return {"serial": "TM-DRIVER-1", "model": "temperatureModuleV2", "version": "1.0"}


@pytest.mark.asyncio
async def test_build_primes_driver_state_and_identity_properties() -> None:
    """The standalone driver backend satisfies the same v2 property contract."""
    driver = _TemperatureDriver(current=18.0, target=20.0)
    with patch(
        "unitelabs.opentrons_flex.io.temperature_module.TempDeckDriver.create",
        AsyncMock(return_value=driver),
    ):
        controller = await TemperatureModuleController.build("/dev/temperature-module")

    assert controller.state.status == "heating"
    assert controller.state.current_temperature == 18.0
    assert controller.state.target_temperature == 20.0
    assert controller.device_info.serial_number == "TM-DRIVER-1"
    assert driver.calls == [("connect",), ("get_device_info",), ("get_temperature",)]


@pytest.mark.asyncio
async def test_temperature_driver_commands_and_reading() -> None:
    """Set, read, and deactivate map to the low-level GEN2 driver."""
    driver = _TemperatureDriver()
    controller = TemperatureModuleController(driver=driver)

    await controller.set_temperature(42.0)
    reading = await controller.get_temperature()
    await controller.deactivate()

    assert reading.current == 25.0
    assert reading.target == 42.0
    assert driver.calls == [
        ("set_temperature", 42.0),
        ("get_temperature",),
        ("deactivate",),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("temperature", [3.9, 95.1, float("nan"), float("inf"), float("-inf")])
async def test_invalid_temperature_targets_never_reach_the_driver(temperature: float) -> None:
    """Dynamic validation rejects non-finite and out-of-range hardware values."""
    driver = _TemperatureDriver()
    controller = TemperatureModuleController(driver=driver)

    with pytest.raises(InvalidTemperatureTargetError, match="finite value"):
        await controller.set_temperature(temperature)

    assert driver.calls == []


@pytest.mark.asyncio
async def test_set_temperature_and_wait_uses_the_active_target() -> None:
    """The combined operation sets one target and waits for that same target."""
    driver = _TemperatureDriver(current=20.0)
    controller = TemperatureModuleController(driver=driver)

    await controller.set_temperature_and_wait(20.0)

    assert driver.calls == [
        ("set_temperature", 20.0),
        ("get_temperature",),
    ]


@pytest.mark.asyncio
async def test_wait_without_an_active_target_is_a_defined_operation_error() -> None:
    """Waiting without first setting a target gives an operator-recoverable error."""
    controller = TemperatureModuleController(driver=_TemperatureDriver(target=None))

    with pytest.raises(ModuleOperationError, match="no active target"):
        await controller.wait_for_target_temperature()


@pytest.mark.asyncio
async def test_low_level_driver_exposes_the_same_state_and_identity_contract() -> None:
    """The legacy driver backend remains usable by the v2 properties after a read."""
    controller = TemperatureModuleController(driver=_TemperatureDriver(current=30.0, target=20.0))

    await controller.get_temperature()

    assert controller.state.status == "cooling"
    assert controller.state.current_temperature == 30.0
    assert controller.state.target_temperature == 20.0
    assert controller.device_info.serial_number == ""


class _WaitingTemperatureModule:
    """High-level module double whose poller state can be advanced by a test."""

    def __init__(self) -> None:
        self.temperature = 25.0
        self.target: float | None = None
        self.status = SimpleNamespace(value="idle")
        self.device_info = {"serial": "TM1", "model": "temperatureModuleV2", "version": "1.0"}
        self.target_started = asyncio.Event()

    async def start_set_temperature(self, celsius: float) -> None:
        self.target = celsius
        self.status.value = "heating"
        self.target_started.set()

    async def deactivate(self) -> None:
        self.target = None
        self.status.value = "idle"


@pytest.mark.asyncio
async def test_thermal_wait_releases_connector_wide_hardware_lock() -> None:
    """Autonomous heating must not block unrelated robot-server hardware calls."""
    shared_lock = asyncio.Lock()
    module = _WaitingTemperatureModule()
    controller = TemperatureModuleController.from_module(module, lock=shared_lock)

    waiting = asyncio.create_task(controller.set_temperature_and_wait(42.0))
    await module.target_started.wait()

    await asyncio.wait_for(shared_lock.acquire(), timeout=0.2)
    shared_lock.release()
    module.temperature = 42.0
    module.status.value = "holding at target"
    await waiting


@pytest.mark.asyncio
async def test_thermal_wait_detects_target_changed_by_other_interface() -> None:
    """A robot-server target change cannot make the SiLA wait report false success."""
    module = _WaitingTemperatureModule()
    controller = TemperatureModuleController.from_module(module)

    waiting = asyncio.create_task(controller.set_temperature_and_wait(42.0))
    await module.target_started.wait()
    module.target = 30.0

    with pytest.raises(ModuleOperationError, match="target changed while waiting"):
        await waiting
