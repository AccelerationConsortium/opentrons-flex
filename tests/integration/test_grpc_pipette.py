"""End-to-end gRPC integration tests for Flex pipette discovery and nozzle layouts."""

import asyncio
import grpc
import grpc.aio
import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount
from unitelabs.cdk import Connector, SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig
from unitelabs.opentrons_flex.features.pipette import NozzleConfiguration, PipetteFeature, PipetteInfo
from unitelabs.opentrons_flex.features.tip_controller import PipetteMount
from unitelabs.opentrons_flex.io import FlexMotionController
from .observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.pipettecontroller.v1"
_SERVICE = f"{_PKG}.PipetteController"


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

    async def configure_full(self, mount: PipetteMount) -> NozzleConfiguration:
        decoded = await self._call("ConfigureFullNozzleLayout", {"mount": mount, "tiprack_diameter": 5.2})
        return next(iter(decoded.values()))

    async def configure_single(self, mount: PipetteMount, nozzle: str) -> NozzleConfiguration:
        decoded = await self._call(
            "ConfigureSingleNozzleLayout",
            {"mount": mount, "nozzle": nozzle, "tiprack_diameter": 5.2},
        )
        return next(iter(decoded.values()))

    async def configure_rectangle(self, mount: PipetteMount) -> NozzleConfiguration:
        decoded = await self._call(
            "ConfigureRectangularNozzleLayout",
            {
                "mount": mount,
                "back_left_nozzle": "A1",
                "front_right_nozzle": "H1",
                "starting_nozzle": "A1",
                "tiprack_diameter": 5.2,
            },
        )
        return next(iter(decoded.values()))

    async def get_nozzle_configuration(self, mount: PipetteMount) -> NozzleConfiguration:
        decoded = await self._call("GetNozzleConfiguration", {"mount": mount})
        return next(iter(decoded.values()))


@pytest_asyncio.fixture
async def client(sila_channel) -> _PipetteClient:
    channel, pb = sila_channel
    return _PipetteClient(channel, pb)


@pytest_asyncio.fixture
async def attached_96_client() -> _PipetteClient:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": "p1000_96_v3.0", "id": "sim-96"}}
    )
    connector = Connector(
        OpentronsFlexConfig(
            use_simulator=True,
            sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
            cloud_server_endpoint=None,
            discovery=None,
        )
    )
    connector.register(PipetteFeature(FlexMotionController.from_api(api, lock=asyncio.Lock())))
    await connector.start()
    channel = grpc.aio.insecure_channel(connector.sila_server._address)
    try:
        yield _PipetteClient(channel, connector.sila_server.protobuf)
    finally:
        await channel.close()
        await connector.stop()
        await api.clean_up()


@pytest.mark.asyncio
async def test_get_attached_pipettes_returns_both_mounts(client: _PipetteClient) -> None:
    """GetAttachedPipettes returns one entry per mount (LEFT, RIGHT) over the wire."""
    result = await client.get_attached_pipettes()
    assert {p.mount for p in result} == {PipetteMount.LEFT, PipetteMount.RIGHT}


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


@pytest.mark.simulator_only
async def test_all_nozzle_configuration_endpoints_over_wire(attached_96_client: _PipetteClient) -> None:
    full = await attached_96_client.configure_full(PipetteMount.LEFT)
    assert full.active_nozzles == 96

    column = await attached_96_client.configure_rectangle(PipetteMount.LEFT)
    assert column.active_nozzles == 8
    assert column.front_right_nozzle == "H1"

    single = await attached_96_client.configure_single(PipetteMount.LEFT, "A1")
    assert single.active_nozzles == 1
    assert await attached_96_client.get_nozzle_configuration(PipetteMount.LEFT) == single
