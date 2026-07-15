"""End-to-end gRPC tests for the SiLA-compliant Flex TipController."""

import asyncio
import base64
import math

import grpc
import grpc.aio
import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount
from sila.framework.errors import SiLAError, ValidationError
from sila.framework.protobuf import ConversionError
from unitelabs.cdk import Connector, SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig
from unitelabs.opentrons_flex.features.tip_controller import (
    PipetteMount,
    TipController,
    TipLocation,
    TipPresence,
)
from unitelabs.opentrons_flex.io import FlexMotionController

from .observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.tipcontroller.v1"
_SERVICE = f"{_PKG}.TipController"


class _TipClient:
    def __init__(
        self,
        channel: grpc.aio.Channel,
        pb: object,
        location: TipLocation | None = None,
        drop_location: TipLocation | None = None,
    ) -> None:
        self._ch = channel
        self._pb = pb
        self._location = location or TipLocation(x=0.0, y=0.0, z=0.0)
        self._drop_location = drop_location or self._location

    async def _call(self, method: str, params: dict | None = None) -> dict:
        req = await self._pb.encode(f"{_PKG}.{method}_Parameters", params or {})
        stub = self._ch.unary_unary(f"/{_SERVICE}/{method}")
        resp_bytes = await stub(req)
        return await self._pb.decode(f"{_PKG}.{method}_Responses", resp_bytes)

    async def get_tip_presence(self, mount: PipetteMount) -> TipPresence:
        decoded = await self._call("GetTipPresence", {"mount": mount})
        return next(iter(decoded.values()))

    async def pick_up_tip(
        self,
        mount: PipetteMount,
        tip_length: float = 95.6,
        location: TipLocation | None = None,
    ) -> TipPresence:
        decoded = await call_observable(
            self._ch,
            self._pb,
            _SERVICE,
            _PKG,
            "PickUpTip",
            {
                "mount": mount,
                "location": location or self._location,
                "tip_length": tip_length,
                "prep_after": False,
            },
        )
        return next(iter(decoded.values()))

    async def drop_tip(self, mount: PipetteMount) -> TipPresence:
        decoded = await call_observable(
            self._ch,
            self._pb,
            _SERVICE,
            _PKG,
            "DropTip",
            {"mount": mount, "location": self._drop_location, "home_after": False},
        )
        return next(iter(decoded.values()))


@pytest_asyncio.fixture
async def bare_client(sila_channel) -> _TipClient:
    channel, pb = sila_channel
    return _TipClient(channel, pb)


@pytest_asyncio.fixture
async def attached_client() -> _TipClient:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"},
        }
    )
    await api.home()
    point = await api.gantry_position(OT3Mount.LEFT, refresh=True)
    config = OpentronsFlexConfig(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    connector = Connector(config)
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    connector.register(TipController(controller))
    await connector.start()
    channel = grpc.aio.insecure_channel(connector.sila_server._address)
    try:
        yield _TipClient(
            channel,
            connector.sila_server.protobuf,
            TipLocation(x=point.x, y=point.y, z=point.z),
            TipLocation(x=point.x, y=point.y, z=point.z - 95.6),
        )
    finally:
        await channel.close()
        await connector.stop()
        await api.clean_up()


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_tip_lifecycle_round_trip_over_wire(attached_client: _TipClient) -> None:
    assert await attached_client.get_tip_presence(PipetteMount.LEFT) is TipPresence.ABSENT
    assert await attached_client.pick_up_tip(PipetteMount.LEFT) is TipPresence.PRESENT
    assert await attached_client.get_tip_presence(PipetteMount.LEFT) is TipPresence.PRESENT
    assert await attached_client.drop_tip(PipetteMount.LEFT) is TipPresence.ABSENT


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_tip_state_without_pipette_raises_defined_error(bare_client: _TipClient) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await bare_client.get_tip_presence(PipetteMount.LEFT)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"PipetteNotAttachedError" in base64.b64decode(excinfo.value.details() or "")


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_drop_without_tip_raises_defined_error(attached_client: _TipClient) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await attached_client.drop_tip(PipetteMount.LEFT)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"TipNotAttachedError" in base64.b64decode(excinfo.value.details() or "")


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_nan_tip_length_is_validation_error(attached_client: _TipClient) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await attached_client.pick_up_tip(PipetteMount.LEFT, tip_length=math.nan)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    error = SiLAError.decode(base64.b64decode(excinfo.value.details() or ""))
    assert isinstance(error, ValidationError)
    assert error.parameter == (
        "ca.accelerationconsortium/robots/TipController/v1/Command/PickUpTip/Parameter/TipLength"
    )
    assert "must be finite" in str(error)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_nan_tip_location_is_validation_error(attached_client: _TipClient) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await attached_client.pick_up_tip(
            PipetteMount.LEFT,
            location=TipLocation(x=math.nan, y=0.0, z=0.0),
        )
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    error = SiLAError.decode(base64.b64decode(excinfo.value.details() or ""))
    assert isinstance(error, ValidationError)
    assert error.parameter == ("ca.accelerationconsortium/robots/TipController/v1/Command/PickUpTip/Parameter/Location")


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("tip_length", [0.0, -1.0, 100.1, math.inf, -math.inf])
async def test_tip_length_fdl_constraints_reject_out_of_range_values(
    attached_client: _TipClient,
    tip_length: float,
) -> None:
    with pytest.raises(ConversionError):
        await attached_client.pick_up_tip(PipetteMount.LEFT, tip_length=tip_length)


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("coordinate", [-1e9, 1e9])
async def test_tip_location_outside_machine_bounds_is_defined_error(
    attached_client: _TipClient,
    coordinate: float,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await attached_client.pick_up_tip(
            PipetteMount.LEFT,
            location=TipLocation(x=coordinate, y=0.0, z=0.0),
        )
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"MovementOutOfBoundsError" in base64.b64decode(excinfo.value.details() or "")
