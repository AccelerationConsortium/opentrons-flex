"""End-to-end gRPC integration tests for the Flex GripperController (simulate mode).

The bare simulator has no gripper attached, so this exercises two things over the
wire: the Status property for an absent gripper, and that a grip/ungrip/home_jaw
command surfaces the ``GripperNotAttachedError`` *Defined Execution Error*
(SiLA maps these to gRPC ABORTED, with the fully-qualified error identifier in
the status details).
"""

import base64

import grpc
import grpc.aio
import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.gripper import GripperStatus
from .observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.grippercontroller.v1"
_SERVICE = f"{_PKG}.GripperController"


class _GripperClient:
    def __init__(self, channel: grpc.aio.Channel, pb: object) -> None:
        self._ch = channel
        self._pb = pb

    async def _call(self, method: str, params: dict | None = None) -> dict:
        req = await self._pb.encode(f"{_PKG}.{method}_Parameters", params or {})
        stub = self._ch.unary_unary(f"/{_SERVICE}/{method}")
        resp_bytes = await stub(req)
        return await self._pb.decode(f"{_PKG}.{method}_Responses", resp_bytes)

    async def _observable(self, method: str, params: dict | None = None) -> dict:
        return await call_observable(self._ch, self._pb, _SERVICE, _PKG, method, params)

    async def get_status(self) -> GripperStatus:
        stub = self._ch.unary_unary(f"/{_SERVICE}/Get_Status")
        resp_bytes = await stub(b"")
        decoded = await self._pb.decode(f"{_PKG}.Get_Status_Responses", resp_bytes)
        return next(iter(decoded.values()))

    async def grip(self, force: float) -> None:
        await self._observable("Grip", {"force": force})

    async def ungrip(self, force: float) -> None:
        await self._observable("Ungrip", {"force": force})

    async def home_jaw(self) -> None:
        await self._observable("HomeJaw")


@pytest_asyncio.fixture
async def client(sila_channel) -> _GripperClient:
    channel, pb = sila_channel
    return _GripperClient(channel, pb)


def _error_identifier(exc: grpc.aio.AioRpcError) -> bytes:
    """The SiLA error payload travels base64-encoded in the gRPC status details."""
    return base64.b64decode(exc.details() or "")


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_status_reports_no_gripper(client: _GripperClient) -> None:
    """Get_Status reports an unattached gripper on the bare simulator (no sentinel garbage)."""
    status = await client.get_status()
    assert isinstance(status, GripperStatus)
    assert status.attached is False
    assert status.model == ""


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_grip_without_gripper_raises_defined_error(client: _GripperClient) -> None:
    """Grip surfaces the GripperNotAttachedError defined execution error over the wire."""
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await client.grip(force=15.0)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"GripperNotAttachedError" in _error_identifier(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_ungrip_without_gripper_raises_defined_error(client: _GripperClient) -> None:
    """Ungrip surfaces the GripperNotAttachedError defined execution error over the wire."""
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await client.ungrip(force=18.0)
    assert b"GripperNotAttachedError" in _error_identifier(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_home_jaw_without_gripper_raises_defined_error(client: _GripperClient) -> None:
    """HomeJaw surfaces the GripperNotAttachedError defined execution error over the wire."""
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await client.home_jaw()
    assert b"GripperNotAttachedError" in _error_identifier(excinfo.value)


# ── Force parameter (simulator with gripper attached) ────────────────────────


@pytest_asyncio.fixture
async def gripper_attached_channel():
    """gRPC channel to a local simulator that has a gripper attached.

    Unlike the session-wide ``sila_channel`` (no gripper), this stack sets
    ``simulated_gripper=True`` so Grip/Ungrip actually exercise the force path
    through the OT3API simulator — offline, no hardware.
    """
    import contextlib

    from unitelabs.cdk import SiLAServerConfig
    from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app

    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_gripper=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    gen = create_app(config)
    connector = await gen.__anext__()
    await connector.start()
    channel = grpc.aio.insecure_channel(connector.sila_server._address)
    try:
        yield channel, connector.sila_server.protobuf
    finally:
        await channel.close()
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_grip_with_force_succeeds(gripper_attached_channel) -> None:
    """Grip at elevated force completes through the SiLA wire on the simulator."""
    channel, pb = gripper_attached_channel
    client = _GripperClient(channel, pb)
    await client.grip(force=20.0)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_ungrip_with_force_succeeds(gripper_attached_channel) -> None:
    """Ungrip at elevated force completes through the SiLA wire on the simulator."""
    channel, pb = gripper_attached_channel
    client = _GripperClient(channel, pb)
    await client.ungrip(force=20.0)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_ungrip_missing_force_rejected_before_wire(gripper_attached_channel) -> None:
    """A bare Ungrip (no force) is rejected at protobuf encode time, not silently defaulted.

    This guards the connector against a stale client built from the old feature
    definition that does not send ``force``: the required field fails encoding
    before any request reaches the robot.
    """
    from sila.framework.protobuf.conversion_error import ConversionError

    _channel, pb = gripper_attached_channel
    with pytest.raises(ConversionError, match="Missing field"):
        await pb.encode(f"{_PKG}.Ungrip_Parameters", {})


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_grip_ungrip_force_cycle(gripper_attached_channel) -> None:
    """Full heavy-plate pick-and-place force cycle: ungrip, grip 18N, ungrip 18N."""
    channel, pb = gripper_attached_channel
    client = _GripperClient(channel, pb)
    await client.home_jaw()
    await client.ungrip(force=18.0)
    await client.grip(force=18.0)
    await client.ungrip(force=18.0)
