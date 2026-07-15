"""Controller-level liquid-handling tests against the OT3 simulator."""

import asyncio
import math

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount

from unitelabs.opentrons_flex.io import (
    FlexMotionController,
    LiquidVolumeOutOfRangeError,
    NotHomedError,
    PipetteNotAttachedError,
    TipNotAttachedError,
)


@pytest_asyncio.fixture
async def liquid_controller() -> FlexMotionController:
    """Return a homed single-channel simulator with a known 1000 µL tip."""
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"},
        }
    )
    await api.home()
    api.add_tip(OT3Mount.LEFT, tip_length=95.6)
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    yield controller
    await api.clean_up()


async def test_prepare_aspirate_dispense_and_blow_out_round_trip(
    liquid_controller: FlexMotionController,
) -> None:
    instrument = liquid_controller._api.hardware_instruments[OT3Mount.LEFT.to_mount()]

    await liquid_controller.prepare_for_aspirate(OT3Mount.LEFT)
    await liquid_controller.aspirate(OT3Mount.LEFT, volume=50.0, rate=1.0)
    assert instrument.current_volume == pytest.approx(50.0)

    await liquid_controller.dispense(OT3Mount.LEFT, volume=20.0, rate=1.0, push_out=0.0)
    assert instrument.current_volume == pytest.approx(30.0)

    await liquid_controller.blow_out(OT3Mount.LEFT)
    assert instrument.current_volume == pytest.approx(0.0)


async def test_liquid_action_without_pipette_is_defined_error() -> None:
    api = await OT3API.build_hardware_simulator()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    try:
        with pytest.raises(PipetteNotAttachedError, match="No pipette"):
            await controller.aspirate(OT3Mount.LEFT, volume=10.0)
    finally:
        await api.clean_up()


async def test_liquid_action_without_tip_is_defined_error() -> None:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"},
        }
    )
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    try:
        with pytest.raises(TipNotAttachedError, match="No tip"):
            await controller.prepare_for_aspirate(OT3Mount.LEFT)
    finally:
        await api.clean_up()


@pytest.mark.parametrize("volume", [0.0, -1.0, 4.9, math.nan, math.inf, -math.inf, 1000.1])
async def test_aspirate_rejects_invalid_dynamic_volume(
    liquid_controller: FlexMotionController,
    volume: float,
) -> None:
    with pytest.raises(LiquidVolumeOutOfRangeError):
        await liquid_controller.aspirate(OT3Mount.LEFT, volume=volume)


async def test_aspirate_respects_remaining_tip_capacity(liquid_controller: FlexMotionController) -> None:
    await liquid_controller.prepare_for_aspirate(OT3Mount.LEFT)
    await liquid_controller.aspirate(OT3Mount.LEFT, volume=950.0)

    with pytest.raises(LiquidVolumeOutOfRangeError, match="dynamic limit is 50"):
        await liquid_controller.aspirate(OT3Mount.LEFT, volume=55.0)


async def test_dispense_rejects_more_than_current_volume(liquid_controller: FlexMotionController) -> None:
    await liquid_controller.prepare_for_aspirate(OT3Mount.LEFT)
    await liquid_controller.aspirate(OT3Mount.LEFT, volume=50.0)

    with pytest.raises(LiquidVolumeOutOfRangeError, match="dynamic limit is 50"):
        await liquid_controller.dispense(OT3Mount.LEFT, volume=55.0)


async def test_cancelled_aspiration_halts_and_requires_rehome(
    liquid_controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aspiration_started = asyncio.Event()
    halted = asyncio.Event()

    async def blocked_aspirate(*args: object, **kwargs: object) -> None:
        aspiration_started.set()
        await asyncio.Event().wait()

    async def halt_while_locked() -> None:
        assert liquid_controller._lock._lock.locked()
        halted.set()

    monkeypatch.setattr(liquid_controller._api, "aspirate", blocked_aspirate)
    monkeypatch.setattr(liquid_controller._api, "halt", halt_while_locked)

    task = asyncio.create_task(liquid_controller.aspirate(OT3Mount.LEFT, volume=50.0))
    await aspiration_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert halted.is_set()
    with pytest.raises(NotHomedError, match="Fully re-home"):
        await liquid_controller.prepare_for_aspirate(OT3Mount.LEFT)


@pytest.mark.parametrize(
    "model, expected_channels",
    [
        ("p1000_single_v3.0", 1),
        ("p1000_multi_v3.0", 8),
        ("p1000_96_v3.0", 96),
    ],
)
async def test_liquid_primitives_cover_flex_channel_families(model: str, expected_channels: int) -> None:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": model, "id": f"sim-{expected_channels}"}}
    )
    try:
        await api.home()
        api.add_tip(OT3Mount.LEFT, tip_length=95.6)
        controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
        assert controller.attached_instruments[OT3Mount.LEFT.to_mount()]["channels"] == expected_channels
        await controller.prepare_for_aspirate(OT3Mount.LEFT)
        await controller.aspirate(OT3Mount.LEFT, volume=50.0)
        await controller.dispense(OT3Mount.LEFT, volume=50.0, push_out=0.0)
    finally:
        await api.clean_up()
