"""End-to-end smoketest for the local simulator connector stack."""

import grpc.aio
import pytest

from unitelabs.opentrons_flex.features.motion_control import Mount, Position
from .observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.motioncontrolfeature.v1"
_SERVICE = f"{_PKG}.MotionControlFeature"


async def _property(channel: grpc.aio.Channel, pb: object, name: str) -> dict:
    stub = channel.unary_unary(f"/{_SERVICE}/{name}")
    resp_bytes = await stub(b"")
    return await pb.decode(f"{_PKG}.{name}_Responses", resp_bytes)


@pytest.mark.robot_http_only
@pytest.mark.asyncio
async def test_smoketest_stack_serves_http_and_sila(simulator_stack: object | None, http_client) -> None:
    """One local simulator process should serve both robot HTTP and SiLA gRPC APIs."""
    if simulator_stack is None:
        pytest.skip("--with-http-server is required for local smoketest stack coverage")

    health = http_client.get("/health")
    assert health.status_code == 200
    assert health.json()["robot_model"] in {"OT-2 Standard", "OT-3 Standard"}

    async with grpc.aio.insecure_channel(simulator_stack.grpc_address) as channel:
        sim = await _property(channel, simulator_stack.protobuf, "Get_IsSimulating")
        assert next(iter(sim.values())) is True

        await call_observable(channel, simulator_stack.protobuf, _SERVICE, _PKG, "Home")
        decoded = await call_observable(
            channel,
            simulator_stack.protobuf,
            _SERVICE,
            _PKG,
            "GetPosition",
            {"mount": Mount.LEFT},
        )
        assert isinstance(next(iter(decoded.values())), Position)
