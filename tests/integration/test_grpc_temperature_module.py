"""End-to-end SiLA gRPC tests for the simulated Temperature Module GEN2."""

import base64

import grpc
import pytest
import pytest_asyncio
from sila.framework.protobuf import ConversionError

from unitelabs.opentrons_flex.features.temperature import (
    TemperatureControllerStatus,
    TemperatureControlStatus,
)

from .temperature_module_client import TemperatureModuleClient


@pytest_asyncio.fixture
async def temperature_module(sila_channel) -> TemperatureModuleClient:
    channel, protobuf = sila_channel
    return TemperatureModuleClient(channel, protobuf)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_temperature_module_heating_and_cooling_workflow(
    temperature_module: TemperatureModuleClient,
) -> None:
    """Exercise identify, heat, cool, status, and deactivate through the real gRPC surface."""
    info = await temperature_module.get_device_info()
    assert info.serial_number == "TM-SIM-1"
    assert info.model

    initial = await temperature_module.get_status()
    assert isinstance(initial, TemperatureControllerStatus)
    assert initial.status is TemperatureControlStatus.IDLE
    assert initial.target_active is False

    heating = await temperature_module.set_temperature(42.0)
    assert heating.target_temperature == pytest.approx(42.0)
    assert heating.target_active is True

    cooling = await temperature_module.set_temperature_and_wait(20.0)
    assert cooling.status is TemperatureControlStatus.HOLDING
    assert cooling.current_temperature == pytest.approx(20.0)
    assert cooling.target_temperature == pytest.approx(20.0)
    assert cooling.target_active is True

    deactivated = await temperature_module.deactivate()
    assert deactivated.status is TemperatureControlStatus.IDLE
    assert deactivated.target_temperature == pytest.approx(0.0)
    assert deactivated.target_active is False


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("temperature", [3.9, 95.1])
async def test_temperature_module_rejects_out_of_range_targets(
    temperature_module: TemperatureModuleClient,
    temperature: float,
) -> None:
    """Static FDL constraints reject invalid targets before hardware execution."""
    with pytest.raises(ConversionError):
        await temperature_module.set_temperature(temperature)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_temperature_module_rejects_nan_as_defined_error(
    temperature_module: TemperatureModuleClient,
) -> None:
    """NaN is rejected by a defined error before it can reach the module driver."""
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await temperature_module.set_temperature(float("nan"))
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"InvalidTemperatureTargetError" in base64.b64decode(excinfo.value.details() or "")


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("temperature", [float("inf"), float("-inf")])
async def test_temperature_module_constraints_reject_infinite_targets(
    temperature_module: TemperatureModuleClient,
    temperature: float,
) -> None:
    """Static range constraints reject infinities before command execution."""
    with pytest.raises(ConversionError):
        await temperature_module.set_temperature(temperature)
