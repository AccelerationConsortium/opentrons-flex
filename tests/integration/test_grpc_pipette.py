"""End-to-end gRPC integration tests for Flex pipette discovery."""

import grpc
import grpc.aio
import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.motion_control import Mount
from unitelabs.opentrons_flex.features.pipette import PipetteInfo
from .observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.pipettefeature.v1"
_SERVICE = f"{_PKG}.PipetteFeature"


class _PipetteClient:
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

    async def get_attached_pipettes(self) -> list[PipetteInfo]:
        decoded = await self._observable("GetAttachedPipettes")
        value = next(iter(decoded.values()))
        assert isinstance(value, list)
        return value


@pytest_asyncio.fixture
async def client(sila_channel) -> _PipetteClient:
    channel, pb = sila_channel
    return _PipetteClient(channel, pb)


@pytest.mark.asyncio
async def test_get_attached_pipettes_returns_both_mounts(client: _PipetteClient) -> None:
    """GetAttachedPipettes returns one entry per mount (LEFT, RIGHT) over the wire."""
    result = await client.get_attached_pipettes()
    assert {p.mount for p in result} == {Mount.LEFT, Mount.RIGHT}


@pytest.mark.asyncio
async def test_get_attached_pipettes_returns_pipette_info(client: _PipetteClient) -> None:
    """Each decoded entry is a PipetteInfo dataclass."""
    result = await client.get_attached_pipettes()
    assert all(isinstance(p, PipetteInfo) for p in result)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_bare_simulator_reports_no_pipettes(client: _PipetteClient) -> None:
    """On the bare simulator both mounts report not-attached with empty model (no sentinel)."""
    result = await client.get_attached_pipettes()
    assert all(p.attached is False for p in result)
    assert all(p.model == "" for p in result)
