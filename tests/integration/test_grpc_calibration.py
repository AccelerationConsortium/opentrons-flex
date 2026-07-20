"""End-to-end gRPC integration tests for the Flex CalibrationController (simulate mode).

Flex calibration is automatic probe-based calibration, not Smoothie config writes.
On the bare simulator there is no pipette/gripper/probe, so the routines fail — and
that failure is exactly what we assert propagates over the wire as the
``CalibrationFailedError`` Defined Execution Error (gRPC ABORTED). This validates
both the calibration service plumbing and the io-layer error translation.
"""

import base64

import grpc
import grpc.aio
import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.calibration import GripperJaw, PipetteMount
from .observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.calibrationcontroller.v1"
_SERVICE = f"{_PKG}.CalibrationController"


class _CalibrationClient:
    def __init__(self, channel: grpc.aio.Channel, pb: object) -> None:
        self._ch = channel
        self._pb = pb

    async def _call(self, method: str, params: dict) -> dict:
        req = await self._pb.encode(f"{_PKG}.{method}_Parameters", params)
        stub = self._ch.unary_unary(f"/{_SERVICE}/{method}")
        resp_bytes = await stub(req)
        return await self._pb.decode(f"{_PKG}.{method}_Responses", resp_bytes)

    async def _observable(self, method: str, params: dict) -> dict:
        return await call_observable(self._ch, self._pb, _SERVICE, _PKG, method, params)

    async def calibrate_pipette(self, mount: PipetteMount, slot: int) -> dict:
        return await self._observable("CalibratePipette", {"mount": mount, "slot": slot})

    async def calibrate_gripper_jaw(self, jaw: GripperJaw, slot: int) -> dict:
        return await self._observable("CalibrateGripperJaw", {"jaw": jaw, "slot": slot})


@pytest_asyncio.fixture
async def client(sila_channel) -> _CalibrationClient:
    channel, pb = sila_channel
    return _CalibrationClient(channel, pb)


def _details(exc: grpc.aio.AioRpcError) -> bytes:
    return base64.b64decode(exc.details() or "")


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_calibrate_pipette_without_pipette_raises_defined_error(client: _CalibrationClient) -> None:
    """CalibratePipette with no pipette surfaces CalibrationFailedError over the wire."""
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await client.calibrate_pipette(PipetteMount.LEFT, slot=5)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"CalibrationFailedError" in _details(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_calibrate_gripper_jaw_without_gripper_raises_defined_error(client: _CalibrationClient) -> None:
    """CalibrateGripperJaw with no gripper surfaces a calibration defined error over the wire."""
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await client.calibrate_gripper_jaw(GripperJaw.FRONT, slot=5)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    details = _details(excinfo.value)
    assert b"CalibrationFailedError" in details or b"CalibrationProbeNotAttachedError" in details
