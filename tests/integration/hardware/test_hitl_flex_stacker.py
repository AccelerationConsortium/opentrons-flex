"""Guarded hardware acceptance tests for an attached Flex Stacker."""

import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.flex_stacker import FlexStackerStatus, HopperDoorState, StackerStatus

from ..flex_stacker_client import FlexStackerClient

pytestmark = pytest.mark.hardware_only


@pytest_asyncio.fixture
async def stacker(sila_channel) -> FlexStackerClient:
    channel, protobuf = sila_channel
    return FlexStackerClient(channel, protobuf)


@pytest.mark.asyncio
async def test_flex_stacker_identity_and_status_are_readable(stacker: FlexStackerClient) -> None:
    """Confirm the attached Stacker is registered without moving it."""
    info = await stacker.get_device_info()
    status = await stacker.get_status()

    assert info.serial_number, "Stacker serial number is empty; check USB power and restart the connector"
    assert info.model, "Stacker model is empty; check module discovery and firmware compatibility"
    assert isinstance(status, FlexStackerStatus)
    assert status.install_detected, "Stacker installation sensor is inactive; re-seat the module before actuation"


@pytest.mark.asyncio
@pytest.mark.stacker_actuation
async def test_flex_stacker_guarded_retrieve_store_round_trip(
    stacker: FlexStackerClient,
    request: pytest.FixtureRequest,
) -> None:
    """Retrieve and restore one item after explicit operator preparation and height input."""
    height = request.config.getoption("--stacker-labware-height")
    if height is None:
        pytest.fail("--stacker-actuation also requires --stacker-labware-height with the exact assembled height")

    before = await stacker.get_status()
    assert before.hopper_door_state is HopperDoorState.CLOSED, "Close the Stacker hopper door before actuation"
    assert before.install_detected, "Re-seat the Stacker until its installation sensor is active"

    retrieved = False
    stored: FlexStackerStatus | None = None
    try:
        homed = await stacker.home_all(ignore_latch=False)
        assert homed.status is StackerStatus.IDLE
        await stacker.retrieve_labware(height, True, True)
        retrieved = True
        stored = await stacker.store_labware(height, True)
        retrieved = False
    finally:
        if retrieved:
            await stacker.store_labware(height, True)
        await stacker.deactivate()

    assert stored is not None
    assert stored.status is StackerStatus.IDLE
