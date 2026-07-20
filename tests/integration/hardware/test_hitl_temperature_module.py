"""Guarded hardware acceptance tests for an attached Temperature Module GEN2."""

import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.temperature import TemperatureControllerStatus, TemperatureControlStatus

from ..temperature_module_client import TemperatureModuleClient

pytestmark = pytest.mark.hardware_only


@pytest_asyncio.fixture
async def temperature_module(sila_channel) -> TemperatureModuleClient:
    channel, protobuf = sila_channel
    return TemperatureModuleClient(channel, protobuf)


@pytest.mark.asyncio
async def test_temperature_module_identity_and_status_are_readable(
    temperature_module: TemperatureModuleClient,
) -> None:
    """Confirm module registration and poller state without changing its target."""
    info = await temperature_module.get_device_info()
    status = await temperature_module.get_status()

    assert info.serial_number, "Temperature Module serial is empty; check module power and restart the connector"
    assert info.model, "Temperature Module model is empty; check module discovery and firmware compatibility"
    assert isinstance(status, TemperatureControllerStatus)
    assert isinstance(status.status, TemperatureControlStatus)


@pytest.mark.asyncio
@pytest.mark.temperature_module_actuation
async def test_temperature_module_guarded_target_and_deactivation(
    temperature_module: TemperatureModuleClient,
    request: pytest.FixtureRequest,
) -> None:
    """Reach an explicit operator-selected target, then always deactivate control."""
    target = request.config.getoption("--temperature-module-target")
    assert target is not None, "Pass an explicit --temperature-module-target between 4 and 95 °C"
    assert 4.0 <= target <= 95.0, "Temperature Module target must be within the official 4-95 °C range"

    before = await temperature_module.get_status()
    assert abs(target - before.current_temperature) >= 1.0, (
        "Choose a target at least 1 °C away from the current reading so heating or cooling is actually tested"
    )

    try:
        reached = await temperature_module.set_temperature_and_wait(target)
        assert reached.status is TemperatureControlStatus.HOLDING
        assert reached.current_temperature == pytest.approx(target, abs=1.0)
        assert reached.target_temperature == pytest.approx(target)
        assert reached.target_active is True
    finally:
        deactivated = await temperature_module.deactivate()

    assert deactivated.status is TemperatureControlStatus.IDLE
    assert deactivated.target_active is False
