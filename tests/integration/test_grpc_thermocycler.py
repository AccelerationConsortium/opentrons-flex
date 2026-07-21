"""End-to-end SiLA gRPC workflow tests for the simulated Flex Thermocycler."""

import pytest
import pytest_asyncio
from sila.framework.protobuf import ConversionError

from unitelabs.opentrons_flex.features.thermocycler import (
    LidStatus,
    ThermocyclerProfileStep,
    ThermocyclerStatus,
    ThermocyclerTemperature,
)

from .thermocycler_client import ThermocyclerClient


@pytest_asyncio.fixture
async def thermocycler(sila_channel) -> ThermocyclerClient:
    channel, protobuf = sila_channel
    return ThermocyclerClient(channel, protobuf)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_thermocycler_workflow_round_trip(thermocycler: ThermocyclerClient) -> None:
    """Exercise identity, lid, thermal profile, status, and cleanup over gRPC."""
    info = await thermocycler.get_device_info()
    assert info.serial_number == "TC-SIM-1"
    assert info.model

    assert await thermocycler.open_lid() is LidStatus.OPEN
    assert await thermocycler.get_lid_status() is LidStatus.OPEN
    assert await thermocycler.close_lid() is LidStatus.CLOSED

    lid = await thermocycler.set_lid_temperature(45.0)
    assert isinstance(lid, ThermocyclerTemperature)
    assert lid.target_temperature == pytest.approx(45.0)
    assert lid.target_active is True
    reached_lid = await thermocycler.wait_for_lid_temperature()
    assert reached_lid.current_temperature == pytest.approx(45.0)
    assert (await thermocycler.get_lid_temperature()).target_active is True

    plate = await thermocycler.set_plate_temperature(30.0, 0.0, 50.0, 0.0)
    assert plate.target_temperature == pytest.approx(30.0)
    reached_plate = await thermocycler.wait_for_plate_temperature()
    assert reached_plate.current_temperature == pytest.approx(30.0)
    assert (await thermocycler.get_plate_temperature()).target_active is True

    profile = await thermocycler.execute_profile(
        [
            ThermocyclerProfileStep(temperature=35.0, hold_time=0.0, ramp_rate=0.0),
            ThermocyclerProfileStep(temperature=25.0, hold_time=0.0, ramp_rate=0.0),
        ],
        repetitions=1,
        volume=50.0,
    )
    assert isinstance(profile, ThermocyclerStatus)
    assert profile.current_plate_temperature == pytest.approx(25.0)

    assert (await thermocycler.deactivate_lid()).target_active is False
    assert (await thermocycler.deactivate_block()).target_active is False
    deactivated = await thermocycler.deactivate_all()
    assert deactivated.lid_target_active is False
    assert deactivated.plate_target_active is False
    assert isinstance(await thermocycler.get_status(), ThermocyclerStatus)


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("temperature", [3.9, 99.1])
async def test_thermocycler_rejects_invalid_block_temperature(
    thermocycler: ThermocyclerClient,
    temperature: float,
) -> None:
    """The FDL blocks invalid temperatures before hardware execution."""
    with pytest.raises(ConversionError):
        await thermocycler.set_plate_temperature(temperature, 0.0, 50.0, 0.0)


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("temperature", [36.9, 110.1])
async def test_thermocycler_rejects_invalid_lid_temperature(
    thermocycler: ThermocyclerClient,
    temperature: float,
) -> None:
    """The FDL blocks invalid lid temperatures before hardware execution."""
    with pytest.raises(ConversionError):
        await thermocycler.set_lid_temperature(temperature)
