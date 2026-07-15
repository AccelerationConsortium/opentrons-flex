"""Guarded hardware acceptance tests for an attached Flex Heater-Shaker.

Read-only identity and status checks run with the normal ``--robot`` hardware
gate. Mechanical actuation additionally requires ``--heater-shaker-actuation``
and an operator-prepared module with a compatible thermal adapter and secured
labware. The actuation test always attempts to stop shaking and deactivate the
heater during cleanup.
"""

import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.heater_shaker import HeaterShakerStatus, LatchStatus

from ..heater_shaker_client import HeaterShakerClient

pytestmark = pytest.mark.hardware_only


@pytest_asyncio.fixture
async def heater_shaker(sila_channel) -> HeaterShakerClient:
    channel, protobuf = sila_channel
    return HeaterShakerClient(channel, protobuf)


@pytest.mark.asyncio
async def test_heater_shaker_identity_and_status_are_readable(heater_shaker: HeaterShakerClient) -> None:
    """Confirm the connector registered the attached module without actuating it."""
    info = await heater_shaker.get_device_info()
    status = await heater_shaker.get_status()

    assert info.serial_number, "Heater-Shaker serial number is empty; check USB power and restart the connector"
    assert info.model, "Heater-Shaker model is empty; check module discovery and firmware compatibility"
    assert isinstance(status, HeaterShakerStatus)
    assert isinstance(status.latch_status, LatchStatus)


@pytest.mark.asyncio
@pytest.mark.heater_shaker_actuation
async def test_heater_shaker_guarded_actuation_round_trip(heater_shaker: HeaterShakerClient) -> None:
    """Run a minimum-speed shake only after the operator explicitly opens the safety gate."""
    try:
        assert await heater_shaker.close_latch() is LatchStatus.IDLE_CLOSED
        speed = await heater_shaker.set_rpm(200)
        assert speed.target == 200
    finally:
        try:
            stopped = await heater_shaker.stop_shaking()
        finally:
            # Deactivation must still be attempted if stopping reports a fault.
            await heater_shaker.deactivate_heater()

    assert stopped.current == 0
    assert stopped.target == 0
    assert stopped.target_active is False
