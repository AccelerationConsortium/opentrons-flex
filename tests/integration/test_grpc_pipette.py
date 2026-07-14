"""End-to-end gRPC integration tests for the Flex PipetteFeature (simulate mode)."""

import base64

import grpc
import grpc.aio
import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.motion_control import Mount
from unitelabs.opentrons_flex.features.pipette import PipetteInfo, PipetteMount, TipPresence
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

    async def get_tip_presence(self, mount: PipetteMount) -> TipPresence:
        decoded = await self._observable("GetTipPresence", {"mount": mount})
        return next(iter(decoded.values()))

    async def pick_up_tip(self, mount: PipetteMount) -> TipPresence:
        decoded = await self._observable(
            "PickUpTip",
            {
                "mount": mount,
                "tip_length": 95.6,
                "presses": 0,
                "increment": 0.0,
                "prep_after": False,
            },
        )
        return next(iter(decoded.values()))


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


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_get_tip_presence_reports_absent_over_wire(client: _PipetteClient) -> None:
    assert await client.get_tip_presence(PipetteMount.LEFT) is TipPresence.ABSENT


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_pick_up_tip_without_pipette_raises_defined_error(client: _PipetteClient) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await client.pick_up_tip(PipetteMount.LEFT)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"PipetteNotAttachedError" in base64.b64decode(excinfo.value.details() or "")
