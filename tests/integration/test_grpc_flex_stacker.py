"""End-to-end SiLA gRPC workflow tests for the simulated Flex Stacker."""

import pytest
import pytest_asyncio
from sila.framework.protobuf import ConversionError

from unitelabs.opentrons_flex.features.flex_stacker import (
    FlexStackerStatus,
    StackerLedColor,
    StackerLedPattern,
    StackerStatus,
)

from .flex_stacker_client import FlexStackerClient


@pytest_asyncio.fixture
async def stacker(sila_channel) -> FlexStackerClient:
    channel, protobuf = sila_channel
    return FlexStackerClient(channel, protobuf)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_flex_stacker_retrieve_store_workflow(stacker: FlexStackerClient) -> None:
    """Exercise identity, home, retrieve, store, status, LED, and stop over gRPC."""
    info = await stacker.get_device_info()
    assert info.serial_number == "FS-SIM-1"

    homed = await stacker.home_all(ignore_latch=False)
    assert isinstance(homed, FlexStackerStatus)
    assert homed.status is StackerStatus.IDLE
    retrieved = await stacker.retrieve_labware(14.4, False, False)
    assert retrieved.initialized is True
    assert retrieved.recovery_required is False
    stored = await stacker.store_labware(14.4, False)
    assert stored.status is StackerStatus.IDLE
    led = await stacker.set_led(0.5, StackerLedColor.GREEN, StackerLedPattern.FLASH, 0.1, 2)
    assert led.install_detected is True

    switches = await stacker.get_limit_switch_status()
    assert switches.x.value in {"extended", "retracted", "unknown"}
    assert (await stacker.get_status()).initialized is True
    assert (await stacker.deactivate()).status is StackerStatus.IDLE


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("height", [3.9, 102.6])
async def test_flex_stacker_rejects_unsupported_labware_height(
    stacker: FlexStackerClient,
    height: float,
) -> None:
    """FDL height constraints reject incompatible labware before motion."""
    with pytest.raises(ConversionError):
        await stacker.retrieve_labware(height, False, False)
