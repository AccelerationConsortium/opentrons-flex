"""End-to-end gRPC coverage for advanced liquid and labware movement features."""

import asyncio
import base64
from pathlib import Path

import grpc
import grpc.aio
import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount
from opentrons.types import Point
from unitelabs.cdk import Connector, SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig
from unitelabs.opentrons_flex.features import (
    LabwareMovementController,
    LabwareDeckState,
    LabwareMovementResult,
    LabwarePlanSummary,
    LiquidHandlingController,
    LiquidLevel,
    LiquidPosition,
    PipetteMount,
    TransferProfile,
    VerifiedLiquidClass,
    VerifiedTransferResult,
    WellGeometry,
)
from unitelabs.opentrons_flex.io import (
    FlexGripperController,
    FlexLabwareMovementController,
    FlexLiquidHandlingController,
    FlexMotionController,
    LabwareGripGeometry,
    LabwareMovementPlan,
    LabwareMovementState,
)

from .observable import call_observable

_LIQUID_PKG = "sila2.ca.accelerationconsortium.robots.liquidhandlingcontroller.v1"
_LIQUID_SERVICE = f"{_LIQUID_PKG}.LiquidHandlingController"
_LABWARE_PKG = "sila2.ca.accelerationconsortium.robots.labwaremovementcontroller.v1"
_LABWARE_SERVICE = f"{_LABWARE_PKG}.LabwareMovementController"


class _Client:
    def __init__(self, channel: grpc.aio.Channel, protobuf: object, package: str, service: str) -> None:
        self._channel = channel
        self._protobuf = protobuf
        self._package = package
        self._service = service

    async def observable(self, method: str, params: dict) -> dict:
        return await call_observable(
            self._channel,
            self._protobuf,
            self._service,
            self._package,
            method,
            params,
        )

    async def property(self, method: str) -> dict:
        stub = self._channel.unary_unary(f"/{self._service}/{method}")
        response = await stub(b"")
        return await self._protobuf.decode(f"{self._package}.{method}_Responses", response)


@pytest_asyncio.fixture
async def liquid_client(monkeypatch: pytest.MonkeyPatch) -> tuple[_Client, LiquidPosition]:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"}}
    )
    await api.home()
    api.add_tip(OT3Mount.LEFT, tip_length=95.6)
    home = await api.gantry_position(OT3Mount.LEFT, refresh=True)

    async def detected(*args: object, **kwargs: object) -> float:
        return 42.5

    monkeypatch.setattr(api, "liquid_probe", detected)
    motion = FlexMotionController.from_api(api, lock=asyncio.Lock())
    connector = Connector(
        OpentronsFlexConfig(
            use_simulator=True,
            sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
            cloud_server_endpoint=None,
            discovery=None,
        )
    )
    connector.register(LiquidHandlingController(FlexLiquidHandlingController(motion)))
    await connector.start()
    channel = grpc.aio.insecure_channel(connector.sila_server._address)
    try:
        yield (
            _Client(channel, connector.sila_server.protobuf, _LIQUID_PKG, _LIQUID_SERVICE),
            LiquidPosition(home.x, home.y, home.z),
        )
    finally:
        await channel.close()
        await connector.stop()
        await api.clean_up()


@pytest.mark.simulator_only
async def test_all_advanced_liquid_endpoints_over_grpc(
    liquid_client: tuple[_Client, LiquidPosition],
) -> None:
    client, home = liquid_client
    await client.observable(
        "Mix",
        {"mount": PipetteMount.LEFT, "cycles": 2, "volume": 20.0, "aspirate_rate": 1.0, "dispense_rate": 1.0},
    )
    touch_well = WellGeometry(home.x - 20, home.y - 20, home.z - 30, home.z - 20, 8.0, 8.0)
    touch = await client.observable(
        "TouchTip",
        {
            "mount": PipetteMount.LEFT,
            "well": touch_well,
            "z_offset": -1.0,
            "distance_from_edge": 1.0,
            "speed": 20.0,
        },
    )
    assert isinstance(next(iter(touch.values())), LiquidPosition)

    level = await client.observable("ProbeLiquidLevel", {"mount": PipetteMount.LEFT, "maximum_distance": 10.0})
    assert next(iter(level.values())) == LiquidLevel(detected_height=42.5)

    tracking_end = LiquidPosition(home.x - 20, home.y - 20, home.z - 35)
    tracked = await client.observable(
        "AspirateWhileTracking",
        {
            "mount": PipetteMount.LEFT,
            "end_position": tracking_end,
            "volume": 20.0,
            "rate": 1.0,
            "movement_delay": 0.0,
        },
    )
    assert next(iter(tracked.values())) == tracking_end
    tracked = await client.observable(
        "DispenseWhileTracking",
        {
            "mount": PipetteMount.LEFT,
            "end_position": LiquidPosition(tracking_end.x, tracking_end.y, tracking_end.z + 5),
            "volume": 20.0,
            "rate": 1.0,
            "push_out": 0.0,
            "movement_delay": 0.0,
        },
    )
    assert isinstance(next(iter(tracked.values())), LiquidPosition)

    source = LiquidPosition(home.x - 30, home.y - 30, home.z - 40)
    source_retract = LiquidPosition(source.x, source.y, source.z + 10)
    destination = LiquidPosition(home.x - 60, home.y - 30, home.z - 40)
    destination_retract = LiquidPosition(destination.x, destination.y, destination.z + 10)
    await client.observable(
        "Transfer",
        {
            "mount": PipetteMount.LEFT,
            "source": source,
            "source_retract": source_retract,
            "destination": destination,
            "destination_retract": destination_retract,
            "volume": 50.0,
            "profile": TransferProfile(1.0, 1.0, 0.0, 0.0, 0, 0.0, 0, 0.0, True),
        },
    )

    source_well = WellGeometry(home.x - 30, home.y - 30, home.z - 45, home.z - 25, 8.0, 8.0)
    destination_well = WellGeometry(home.x - 60, home.y - 30, home.z - 45, home.z - 25, 8.0, 8.0)
    verified = await client.observable(
        "TransferWithVerifiedLiquidClass",
        {
            "mount": PipetteMount.LEFT,
            "source_well": source_well,
            "destination_well": destination_well,
            "volume": 50.0,
            "liquid_class": VerifiedLiquidClass.WATER,
            "tiprack_uri": "opentrons/opentrons_flex_96_tiprack_1000ul/1",
        },
    )
    result = next(iter(verified.values()))
    assert isinstance(result, VerifiedTransferResult)
    assert result.liquid_class is VerifiedLiquidClass.WATER


@pytest.mark.simulator_only
async def test_verified_liquid_class_error_is_defined_over_grpc(
    liquid_client: tuple[_Client, LiquidPosition],
) -> None:
    client, home = liquid_client
    well = WellGeometry(home.x - 30, home.y - 30, home.z - 45, home.z - 25, 8.0, 8.0)
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await client.observable(
            "TransferWithVerifiedLiquidClass",
            {
                "mount": PipetteMount.LEFT,
                "source_well": well,
                "destination_well": well,
                "volume": 50.0,
                "liquid_class": VerifiedLiquidClass.WATER,
                "tiprack_uri": "opentrons/not_a_real_tiprack/1",
            },
        )
    assert b"LiquidClassNotSupportedError" in base64.b64decode(excinfo.value.details() or "")


@pytest_asyncio.fixture
async def labware_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Client:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.GRIPPER: {"model": "gripperV1.3", "id": "sim-gripper"}}
    )
    await api.home()
    monkeypatch.setattr(api, "raise_error_if_gripper_pickup_failed", lambda **kwargs: None)
    home = await api.gantry_position(OT3Mount.GRIPPER, refresh=True)
    lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=lock)
    gripper = FlexGripperController.from_api(api, lock=lock)
    geometry = LabwareGripGeometry(15.0, 74.0, 2.0, 2.0)
    outbound = LabwareMovementPlan(
        "plate-out",
        "plate-1",
        False,
        "D1",
        "DECK_SLOT",
        Point(home.x - 80, home.y - 80, 80),
        "D2",
        "DECK_SLOT",
        Point(home.x - 180, home.y - 80, 80),
        geometry,
        Point(0, 0, 0),
    )
    lid = LabwareMovementPlan(
        "lid-out",
        "lid-1",
        True,
        "D3",
        "DECK_SLOT",
        Point(home.x - 80, home.y - 140, 80),
        "D4",
        "DECK_SLOT",
        Point(home.x - 180, home.y - 140, 80),
        geometry,
        Point(0, 0, 0),
    )
    occupied_target = LabwareMovementPlan(
        "occupied-target",
        "plate-2",
        False,
        "D5",
        "DECK_SLOT",
        lid.source_grip_point,
        "D1",
        "DECK_SLOT",
        outbound.source_grip_point,
        geometry,
        Point(0, 0, 0),
    )
    labware_state = LabwareMovementState(
        tmp_path / "grpc-labware-state.json",
        {"D1": "plate-1", "D3": "lid-1", "D5": "plate-2"},
    )
    connector = Connector(
        OpentronsFlexConfig(
            use_simulator=True,
            sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
            cloud_server_endpoint=None,
            discovery=None,
        )
    )
    connector.register(
        LabwareMovementController(
            FlexLabwareMovementController(
                motion,
                gripper,
                plans=[outbound, lid, occupied_target],
                state=labware_state,
            )
        )
    )
    await connector.start()
    channel = grpc.aio.insecure_channel(connector.sila_server._address)
    try:
        yield _Client(channel, connector.sila_server.protobuf, _LABWARE_PKG, _LABWARE_SERVICE)
    finally:
        await channel.close()
        await connector.stop()
        labware_state.close()
        await api.clean_up()


@pytest.mark.simulator_only
async def test_labware_and_lid_movement_endpoints_over_grpc(
    labware_client: _Client,
) -> None:
    plans = next(iter((await labware_client.property("Get_AvailablePlans")).values()))
    assert all(isinstance(plan, LabwarePlanSummary) for plan in plans)
    assert {plan.plan_identifier for plan in plans} == {"plate-out", "lid-out", "occupied-target"}

    moved = await labware_client.observable("MoveLabware", {"plan_identifier": "plate-out"})
    assert isinstance(next(iter(moved.values())), LabwareMovementResult)

    moved_lid = await labware_client.observable("MoveLid", {"plan_identifier": "lid-out"})
    assert isinstance(next(iter(moved_lid.values())), LabwareMovementResult)
    state = next(iter((await labware_client.property("Get_DeckState")).values()))
    assert isinstance(state, LabwareDeckState)
    assert state.valid is True
    assert {item.location_identifier for item in state.occupied_locations} == {"D2", "D4", "D5"}


@pytest.mark.simulator_only
async def test_occupied_labware_destination_is_defined_over_grpc(
    labware_client: _Client,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await labware_client.observable("MoveLabware", {"plan_identifier": "occupied-target"})
    assert b"DestinationOccupiedError" in base64.b64decode(excinfo.value.details() or "")
