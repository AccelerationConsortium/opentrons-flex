"""Flex partial-tip nozzle configuration tests."""

import asyncio

import pytest
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount

from unitelabs.opentrons_flex.io import FlexMotionController, NozzleConfigurationError


@pytest.mark.parametrize(
    "model, expected_full",
    [("p1000_single_v3.0", 1), ("p1000_multi_v3.0", 8), ("p1000_96_v3.0", 96)],
)
async def test_full_nozzle_configuration_covers_every_flex_head(model: str, expected_full: int) -> None:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": model, "id": f"sim-{expected_full}"}}
    )
    try:
        controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
        state = await controller.configure_nozzle_layout(
            OT3Mount.LEFT,
            back_left_nozzle=None,
            front_right_nozzle=None,
            starting_nozzle=None,
            tiprack_diameter=5.2,
        )
        assert state.active_nozzles == expected_full
        assert state.tiprack_diameter == pytest.approx(5.2)
    finally:
        await api.clean_up()


async def test_96_channel_rectangular_and_single_layouts() -> None:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": "p1000_96_v3.0", "id": "sim-96"}}
    )
    try:
        controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
        column = await controller.configure_nozzle_layout(
            OT3Mount.LEFT,
            back_left_nozzle="A1",
            front_right_nozzle="H1",
            starting_nozzle="A1",
            tiprack_diameter=5.2,
        )
        assert column.active_nozzles == 8
        assert column.back_left_nozzle == "A1"
        assert column.front_right_nozzle == "H1"

        single = await controller.configure_nozzle_layout(
            OT3Mount.LEFT,
            back_left_nozzle="A1",
            front_right_nozzle="A1",
            starting_nozzle="A1",
            tiprack_diameter=5.2,
        )
        assert single.active_nozzles == 1
    finally:
        await api.clean_up()


async def test_nozzle_configuration_requires_tip_removal() -> None:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": "p1000_96_v3.0", "id": "sim-96"}}
    )
    try:
        api.add_tip(OT3Mount.LEFT, tip_length=95.6)
        controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
        with pytest.raises(NozzleConfigurationError, match="Remove attached tips"):
            await controller.configure_nozzle_layout(
                OT3Mount.LEFT,
                back_left_nozzle="A1",
                front_right_nozzle="H1",
                starting_nozzle="A1",
                tiprack_diameter=5.2,
            )
    finally:
        await api.clean_up()
