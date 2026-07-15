"""End-to-end SiLA gRPC workflow tests for the simulated Flex Heater-Shaker."""

import pytest
import pytest_asyncio
from sila.framework.protobuf import ConversionError

from unitelabs.opentrons_flex.features.heater_shaker import (
    HeaterShakerSpeed,
    HeaterShakerStatus,
    HeaterShakerTemperature,
    LatchStatus,
)

from .heater_shaker_client import HeaterShakerClient


@pytest_asyncio.fixture
async def heater_shaker(sila_channel) -> HeaterShakerClient:
    channel, protobuf = sila_channel
    return HeaterShakerClient(channel, protobuf)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_heater_shaker_workflow_round_trip(heater_shaker: HeaterShakerClient) -> None:
    """Exercise the colleague workflow's full heat, shake, status, stop, and latch chain."""
    info = await heater_shaker.get_device_info()
    assert info.serial_number == "HS-SIM-1"
    # The Opentrons SimulatingDriver currently reports its own non-empty
    # ``dummyModelHS`` identifier rather than echoing the configured model.
    assert info.model

    assert await heater_shaker.close_latch() is LatchStatus.IDLE_CLOSED
    assert await heater_shaker.get_latch_status() is LatchStatus.IDLE_CLOSED

    temperature = await heater_shaker.set_temperature(42.0)
    assert isinstance(temperature, HeaterShakerTemperature)
    assert temperature.target == pytest.approx(42.0)
    assert temperature.target_active is True
    reached = await heater_shaker.wait_for_temperature(42.0)
    assert reached.current == pytest.approx(42.0)
    measured_temperature = await heater_shaker.get_temperature()
    assert measured_temperature.current == pytest.approx(42.0)
    assert measured_temperature.target_active is True

    speed = await heater_shaker.set_rpm(500)
    assert isinstance(speed, HeaterShakerSpeed)
    assert speed.target == 500
    assert speed.target_active is True
    measured_speed = await heater_shaker.get_rpm()
    assert measured_speed.target == 500
    assert measured_speed.target_active is True

    status = await heater_shaker.get_status()
    assert isinstance(status, HeaterShakerStatus)
    assert status.temperature_target == pytest.approx(42.0)
    assert status.temperature_target_active is True
    assert status.rpm_target == 500
    assert status.rpm_target_active is True
    assert status.latch_status is LatchStatus.IDLE_CLOSED

    stopped = await heater_shaker.stop_shaking()
    assert stopped.current == 0
    assert stopped.target == 0
    assert stopped.target_active is False

    deactivated = await heater_shaker.deactivate_heater()
    assert deactivated.target == pytest.approx(0.0)
    assert deactivated.target_active is False
    assert await heater_shaker.open_latch() is LatchStatus.IDLE_OPEN
    assert await heater_shaker.get_latch_status() is LatchStatus.IDLE_OPEN


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("rotation_speed", [0, 199, 3001])
async def test_set_rpm_rejects_values_outside_operating_range(
    heater_shaker: HeaterShakerClient,
    rotation_speed: int,
) -> None:
    """The FDL constraint rejects invalid speeds before a hardware command is sent."""
    with pytest.raises(ConversionError):
        await heater_shaker.set_rpm(rotation_speed)


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("temperature", [-0.1, 95.1])
async def test_set_temperature_rejects_values_outside_operating_range(
    heater_shaker: HeaterShakerClient,
    temperature: float,
) -> None:
    """The FDL constraint rejects invalid temperature targets before execution."""
    with pytest.raises(ConversionError):
        await heater_shaker.set_temperature(temperature)
