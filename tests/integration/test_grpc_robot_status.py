"""End-to-end gRPC tests for RobotStatusProvider and the recovery workflow (simulator)."""

import asyncio

import grpc
import grpc.aio
import pytest

from .observable import call_observable

_STATUS_PKG = "sila2.ca.accelerationconsortium.robots.robotstatusprovider.v1"
_STATUS_SERVICE = f"{_STATUS_PKG}.RobotStatusProvider"
_MOTION_PKG = "sila2.ca.accelerationconsortium.robots.motioncontroller.v2"
_MOTION_SERVICE = f"{_MOTION_PKG}.MotionController"


async def _status(channel: grpc.aio.Channel, pb: object) -> dict:
    call = channel.unary_stream(f"/{_STATUS_SERVICE}/Subscribe_Status")(b"")
    response = await call.read()
    call.cancel()
    decoded = await pb.decode(f"{_STATUS_PKG}.Subscribe_Status_Responses", response)
    return next(iter(decoded.values()))


def _field(value: object, name: str) -> object:
    return value[name] if isinstance(value, dict) else getattr(value, name)


def _enum_name(value: object) -> str:
    return str(getattr(value, "value", value))


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_status_reports_ready_simulator_with_registered_modules(sila_channel) -> None:
    channel, pb = sila_channel
    status = await _status(channel, pb)

    assert _enum_name(_field(status, "state")) == "READY"
    assert _field(status, "simulating") is True
    assert _field(status, "hardware_busy") is False
    assert _field(status, "recovery_guidance") == ""
    modules = _field(status, "modules")
    module_types = {_field(module, "module_type") for module in modules}
    assert {"HEATER_SHAKER", "THERMOCYCLER", "TEMPERATURE"} <= module_types
    assert all(_field(module, "connected") for module in modules)
    assert _enum_name(_field(status, "web_server")) == "NOT_CONFIGURED"


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_emergency_stop_is_visible_as_recovery_required_until_rehomed(sila_channel) -> None:
    channel, pb = sila_channel

    await call_observable(channel, pb, _MOTION_SERVICE, _MOTION_PKG, "EmergencyStop", {})
    status = await _status(channel, pb)
    assert _enum_name(_field(status, "state")) == "RECOVERY_REQUIRED"
    assert _field(status, "rehome_required") is True
    assert "re-home" in _field(status, "recovery_guidance")

    await call_observable(channel, pb, _MOTION_SERVICE, _MOTION_PKG, "Home", {}, timeout_s=30.0)
    for _ in range(20):
        status = await _status(channel, pb)
        if _enum_name(_field(status, "state")) == "READY":
            break
        await asyncio.sleep(0.05)
    assert _enum_name(_field(status, "state")) == "READY"
    assert _field(status, "rehome_required") is False
