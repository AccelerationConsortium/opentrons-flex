"""Advanced liquid workflows against the real OT3 simulator backend."""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount
from opentrons.types import Point

from unitelabs.opentrons_flex.io import (
    FlexLiquidHandlingController,
    FlexMotionController,
    LiquidClassNotSupportedError,
    LiquidTransferProfile,
    LiquidWellGeometry,
)


@pytest_asyncio.fixture
async def advanced_controller() -> tuple[OT3API, FlexLiquidHandlingController, Point]:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"},
        }
    )
    await api.home()
    api.add_tip(OT3Mount.LEFT, tip_length=95.6)
    motion = FlexMotionController.from_api(api, lock=asyncio.Lock())
    position = await motion.gantry_position(OT3Mount.LEFT)
    yield api, FlexLiquidHandlingController(motion), position
    await api.clean_up()


async def test_mix_round_trip_leaves_tip_empty(
    advanced_controller: tuple[OT3API, FlexLiquidHandlingController, Point],
) -> None:
    api, controller, _ = advanced_controller
    await controller.mix(OT3Mount.LEFT, cycles=3, volume=50.0, aspirate_rate=1.0, dispense_rate=1.0)

    assert api.hardware_instruments[OT3Mount.LEFT.to_mount()].current_volume == pytest.approx(0.0)


async def test_touch_tip_traces_four_wall_points(
    advanced_controller: tuple[OT3API, FlexLiquidHandlingController, Point],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, home = advanced_controller
    moved: list[Point] = []
    real_move = api.move_to

    async def record_move(*args: object, **kwargs: object) -> None:
        moved.append(kwargs["abs_position"])
        await real_move(*args, **kwargs)

    monkeypatch.setattr(api, "move_to", record_move)
    well = LiquidWellGeometry(home.x - 20, home.y - 20, home.z - 30, home.z - 20, 8.0, 8.0)
    result = await controller.touch_tip(
        OT3Mount.LEFT,
        well,
        z_offset=-1.0,
        distance_from_edge=1.0,
        speed=20.0,
    )

    assert len(moved) == 4
    assert result == moved[-1]
    assert {point.x for point in moved} == {well.center_x - 3.0, well.center_x, well.center_x + 3.0}


async def test_probe_liquid_level_returns_sensor_height(
    advanced_controller: tuple[OT3API, FlexLiquidHandlingController, Point],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, _ = advanced_controller

    async def detected(*args: object, **kwargs: object) -> float:
        return 42.5

    monkeypatch.setattr(api, "liquid_probe", detected)
    assert await controller.probe_liquid_level(OT3Mount.LEFT, maximum_distance=10.0) == pytest.approx(42.5)


async def test_aspirate_and_dispense_while_tracking_move_and_update_volume(
    advanced_controller: tuple[OT3API, FlexLiquidHandlingController, Point],
) -> None:
    api, controller, home = advanced_controller
    await api.prepare_for_aspirate(OT3Mount.LEFT)
    aspiration_end = Point(home.x - 10, home.y - 10, home.z - 20)
    result = await controller.aspirate_while_tracking(
        OT3Mount.LEFT,
        aspiration_end,
        volume=50.0,
        rate=1.0,
        movement_delay=0.0,
    )
    assert result == aspiration_end
    assert api.hardware_instruments[OT3Mount.LEFT.to_mount()].current_volume == pytest.approx(50.0)

    dispense_end = Point(aspiration_end.x, aspiration_end.y, aspiration_end.z + 10)
    result = await controller.dispense_while_tracking(
        OT3Mount.LEFT,
        dispense_end,
        volume=50.0,
        rate=1.0,
        push_out=0.0,
        movement_delay=0.0,
    )
    assert result == dispense_end
    assert api.hardware_instruments[OT3Mount.LEFT.to_mount()].current_volume == pytest.approx(0.0)


async def test_explicit_profile_transfer_is_atomic_and_complete(
    advanced_controller: tuple[OT3API, FlexLiquidHandlingController, Point],
) -> None:
    api, controller, home = advanced_controller
    profile = LiquidTransferProfile(
        aspirate_rate=1.0,
        dispense_rate=1.0,
        push_out=0.0,
        air_gap=5.0,
        mix_before_cycles=2,
        mix_before_volume=20.0,
        mix_after_cycles=1,
        mix_after_volume=20.0,
        blow_out=True,
    )
    source = Point(home.x - 20, home.y - 20, home.z - 30)
    source_retract = Point(source.x, source.y, source.z + 10)
    destination = Point(home.x - 40, home.y - 20, home.z - 30)
    destination_retract = Point(destination.x, destination.y, destination.z + 10)

    await controller.transfer(
        OT3Mount.LEFT,
        source,
        source_retract,
        destination,
        destination_retract,
        volume=50.0,
        profile=profile,
    )

    instrument = api.hardware_instruments[OT3Mount.LEFT.to_mount()]
    assert instrument.current_volume == pytest.approx(0.0)
    final = await api.gantry_position(OT3Mount.LEFT, refresh=True)
    assert final == destination_retract


async def test_verified_water_transfer_uses_installed_opentrons_definition(
    advanced_controller: tuple[OT3API, FlexLiquidHandlingController, Point],
) -> None:
    api, controller, home = advanced_controller
    source = LiquidWellGeometry(home.x - 30, home.y - 30, home.z - 45, home.z - 25, 8.0, 8.0)
    destination = LiquidWellGeometry(home.x - 60, home.y - 30, home.z - 45, home.z - 25, 8.0, 8.0)

    result = await controller.transfer_with_verified_liquid_class(
        OT3Mount.LEFT,
        source,
        destination,
        volume=50.0,
        liquid_class="water",
        tiprack_uri="opentrons/opentrons_flex_96_tiprack_1000ul/1",
    )

    assert result.liquid_class == "water"
    assert result.pipette_model == "flex_1channel_1000"
    assert result.profile.aspirate_rate > 0
    assert api.hardware_instruments[OT3Mount.LEFT.to_mount()].current_volume == pytest.approx(0.0)


async def test_verified_transfer_rejects_unmatched_tiprack_before_motion(
    advanced_controller: tuple[OT3API, FlexLiquidHandlingController, Point],
) -> None:
    _, controller, home = advanced_controller
    well = LiquidWellGeometry(home.x - 30, home.y - 30, home.z - 45, home.z - 25, 8.0, 8.0)

    with pytest.raises(LiquidClassNotSupportedError, match="no verified"):
        await controller.transfer_with_verified_liquid_class(
            OT3Mount.LEFT,
            well,
            well,
            volume=50.0,
            liquid_class="water",
            tiprack_uri="opentrons/not_a_real_tiprack/1",
        )
