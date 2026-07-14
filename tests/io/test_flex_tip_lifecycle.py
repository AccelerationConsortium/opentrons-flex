"""Controller-level tip lifecycle tests against the OT3 simulator."""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount, TipStateType

from unitelabs.opentrons_flex.io import FlexMotionController, PipetteNotAttachedError, TipPickupError


@pytest_asyncio.fixture
async def controller() -> FlexMotionController:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"},
        }
    )
    await api.home()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    yield controller
    await api.clean_up()


async def test_pick_up_and_drop_tip_round_trip(controller: FlexMotionController) -> None:
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.ABSENT

    picked_up = await controller.pick_up_tip(
        OT3Mount.LEFT,
        tip_length=95.6,
        presses=1,
        increment=None,
        prep_after=False,
    )
    assert picked_up is TipStateType.PRESENT
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.PRESENT

    dropped = await controller.drop_tip(OT3Mount.LEFT, home_after=False)
    assert dropped is TipStateType.ABSENT
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.ABSENT


async def test_pick_up_tip_without_pipette_is_defined_error() -> None:
    api = await OT3API.build_hardware_simulator()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    try:
        with pytest.raises(PipetteNotAttachedError):
            await controller.pick_up_tip(OT3Mount.LEFT, tip_length=95.6, prep_after=False)
    finally:
        await api.clean_up()


async def test_pick_up_tip_when_tip_is_already_attached_is_defined_error(
    controller: FlexMotionController,
) -> None:
    await controller.pick_up_tip(OT3Mount.LEFT, tip_length=95.6, presses=1, prep_after=False)

    with pytest.raises(TipPickupError):
        await controller.pick_up_tip(OT3Mount.LEFT, tip_length=95.6, presses=1, prep_after=False)
