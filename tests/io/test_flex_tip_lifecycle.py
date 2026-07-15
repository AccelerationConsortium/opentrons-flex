"""Controller-level tip lifecycle tests against the OT3 simulator."""

import asyncio
import math
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount, TipStateType
from opentrons.types import Point

from unitelabs.opentrons_flex.io import (
    FlexMotionController,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    TipNotAttachedError,
    TipPickupError,
    TipStateError,
)


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
    location = await controller.gantry_position(OT3Mount.LEFT)

    picked_up = await controller.pick_up_tip(
        OT3Mount.LEFT,
        location=location,
        tip_length=95.6,
        prep_after=False,
    )
    assert picked_up is TipStateType.PRESENT
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.PRESENT

    drop_location = await controller.gantry_position(OT3Mount.LEFT)
    dropped = await controller.drop_tip(OT3Mount.LEFT, location=drop_location, home_after=False)
    assert dropped is TipStateType.ABSENT
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.ABSENT


async def test_pick_up_tip_without_pipette_is_defined_error() -> None:
    api = await OT3API.build_hardware_simulator()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    try:
        with pytest.raises(PipetteNotAttachedError):
            await controller.pick_up_tip(
                OT3Mount.LEFT,
                location=Point(0, 0, 0),
                tip_length=95.6,
                prep_after=False,
            )
    finally:
        await api.clean_up()


async def test_pick_up_tip_when_tip_is_already_attached_is_defined_error(
    controller: FlexMotionController,
) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    await controller.pick_up_tip(OT3Mount.LEFT, location=location, tip_length=95.6, prep_after=False)
    location = await controller.gantry_position(OT3Mount.LEFT)

    with pytest.raises(TipPickupError):
        await controller.pick_up_tip(OT3Mount.LEFT, location=location, tip_length=95.6, prep_after=False)


async def test_get_tip_presence_without_pipette_is_defined_error() -> None:
    api = await OT3API.build_hardware_simulator()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    try:
        with pytest.raises(PipetteNotAttachedError):
            await controller.get_tip_presence(OT3Mount.LEFT)
    finally:
        await api.clean_up()


async def test_drop_tip_without_attached_tip_is_defined_error(controller: FlexMotionController) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    with pytest.raises(TipNotAttachedError):
        await controller.drop_tip(OT3Mount.LEFT, location=location, home_after=False)


@pytest.mark.parametrize("tip_length", [0.0, -1.0, 100.1, math.nan, math.inf, -math.inf])
async def test_pick_up_tip_rejects_unsafe_tip_lengths_before_hardware(
    controller: FlexMotionController,
    tip_length: float,
) -> None:
    with pytest.raises(ValueError, match="tip_length must be finite"):
        await controller.pick_up_tip(
            OT3Mount.LEFT,
            location=Point(0, 0, 0),
            tip_length=tip_length,
            prep_after=False,
        )
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.ABSENT


@pytest.mark.parametrize(
    "location",
    [Point(math.nan, 0, 0), Point(0, math.inf, 0), Point(0, 0, -math.inf)],
)
async def test_pick_up_tip_rejects_non_finite_location_before_hardware(
    controller: FlexMotionController,
    location: Point,
) -> None:
    with pytest.raises(ValueError, match="coordinates must all be finite"):
        await controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.ABSENT


async def test_cancelled_tip_move_halts_before_releasing_lock(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    move_started = asyncio.Event()
    halted = asyncio.Event()

    async def blocked_move_to(*args: object, **kwargs: object) -> None:
        move_started.set()
        await asyncio.Event().wait()

    async def halt_while_locked() -> None:
        assert controller._lock._lock.locked()
        halted.set()

    monkeypatch.setattr(controller._api, "move_to", blocked_move_to)
    monkeypatch.setattr(controller._api, "halt", halt_while_locked)

    task = asyncio.create_task(
        controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )
    )
    await move_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert halted.is_set()
    assert not controller._lock._lock.locked()
    with pytest.raises(NotHomedError, match="Fully re-home"):
        await controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )
    with pytest.raises(NotHomedError, match="Fully re-home"):
        await controller.pick_up_tip(
            OT3Mount.RIGHT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )

    with pytest.raises(NotHomedError, match="Fully re-home"):
        await controller.home_mount(OT3Mount.LEFT)
    assert not controller._recovery_state.operation_ready
    await controller.home()
    assert controller._recovery_state.operation_ready


async def test_emergency_stop_bypasses_busy_operation_lock(controller: FlexMotionController) -> None:
    await controller._lock._lock.acquire()
    try:
        await asyncio.wait_for(controller.stop(), timeout=0.1)
    finally:
        controller._lock._lock.release()


async def test_cancelled_emergency_stop_finishes_halt_before_propagating(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    halt_started = asyncio.Event()
    release_halt = asyncio.Event()

    async def blocked_halt() -> None:
        halt_started.set()
        await release_halt.wait()

    monkeypatch.setattr(controller._api, "halt", blocked_halt)
    stop_task = asyncio.create_task(controller.stop())
    await halt_started.wait()
    stop_task.cancel()
    await asyncio.sleep(0)
    assert not stop_task.done()
    release_halt.set()

    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert controller._recovery_state.rehome_required


async def test_emergency_stop_during_full_home_keeps_recovery_gate(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_started = asyncio.Event()
    release_home = asyncio.Event()

    async def blocked_home(*args: object, **kwargs: object) -> None:
        home_started.set()
        await release_home.wait()

    monkeypatch.setattr(controller._api, "home", blocked_home)
    home_task = asyncio.create_task(controller.home())
    await home_started.wait()
    await controller.stop()
    release_home.set()
    with pytest.raises(asyncio.CancelledError):
        await home_task

    assert controller._recovery_state.rehome_required


async def test_emergency_stop_prevents_actuation_after_interleaved_move(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A halt that lands during an await invalidates the next hardware action."""
    location = await controller.gantry_position(OT3Mount.LEFT)
    move_started = asyncio.Event()
    release_move = asyncio.Event()

    async def blocked_move_to(*args: object, **kwargs: object) -> None:
        move_started.set()
        await release_move.wait()

    pickup = AsyncMock()
    monkeypatch.setattr(controller._api, "move_to", blocked_move_to)
    monkeypatch.setattr(controller._api, "pick_up_tip", pickup)

    task = asyncio.create_task(
        controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )
    )
    await move_started.wait()
    await controller.stop()
    release_move.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    pickup.assert_not_awaited()
    assert not controller._recovery_state.tip_reconciliation_required


async def test_emergency_stop_cancels_inside_multistep_pickup(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    pickup_started = asyncio.Event()

    async def blocked_pickup(*args: object, **kwargs: object) -> None:
        pickup_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(controller._api, "pick_up_tip", blocked_pickup)
    pickup_task = asyncio.create_task(
        controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )
    )
    await pickup_started.wait()
    await controller.stop()

    with pytest.raises(asyncio.CancelledError):
        await pickup_task
    instrument = controller._api.hardware_instruments[OT3Mount.LEFT.to_mount()]
    assert not instrument.has_tip
    assert not controller._recovery_state.tip_reconciliation_required


async def test_repeated_cancellation_cannot_interrupt_tip_reconciliation(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    move_started = asyncio.Event()
    halt_started = asyncio.Event()
    release_halt = asyncio.Event()

    async def blocked_move_to(*args: object, **kwargs: object) -> None:
        move_started.set()
        await asyncio.Event().wait()

    async def blocked_halt() -> None:
        halt_started.set()
        await release_halt.wait()

    monkeypatch.setattr(controller._api, "move_to", blocked_move_to)
    monkeypatch.setattr(controller._api, "halt", blocked_halt)

    task = asyncio.create_task(
        controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )
    )
    await move_started.wait()
    task.cancel()
    await halt_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_halt.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not controller._recovery_state.tip_reconciliation_required


async def test_cancelled_pickup_synchronizes_present_sensor_state(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    move_started = asyncio.Event()
    sensor_reads = 0

    async def sensor_state(*args: object, **kwargs: object) -> TipStateType:
        nonlocal sensor_reads
        sensor_reads += 1
        return TipStateType.ABSENT if sensor_reads == 1 else TipStateType.PRESENT

    async def blocked_move_to(*args: object, **kwargs: object) -> None:
        move_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(controller._api, "get_tip_presence_status", sensor_state)
    monkeypatch.setattr(controller._api, "move_to", blocked_move_to)

    task = asyncio.create_task(
        controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )
    )
    await move_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    instrument = controller._api.hardware_instruments[OT3Mount.LEFT.to_mount()]
    assert instrument.has_tip
    assert instrument.current_tip_length == pytest.approx(95.6)
    assert not controller._recovery_state.tip_reconciliation_required


async def test_failed_pickup_verification_removes_phantom_software_tip(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)

    async def absent_sensor(*args: object, **kwargs: object) -> TipStateType:
        return TipStateType.ABSENT

    monkeypatch.setattr(controller._api, "get_tip_presence_status", absent_sensor)
    with pytest.raises(TipStateError, match="sensor reported ABSENT"):
        await controller.pick_up_tip(
            OT3Mount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
        )

    instrument = controller._api.hardware_instruments[OT3Mount.LEFT.to_mount()]
    assert not instrument.has_tip


async def test_failed_drop_verification_restores_software_tip_model(
    controller: FlexMotionController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    await controller.pick_up_tip(OT3Mount.LEFT, location=location, tip_length=95.6, prep_after=False)
    location = await controller.gantry_position(OT3Mount.LEFT)

    async def present_sensor(*args: object, **kwargs: object) -> TipStateType:
        return TipStateType.PRESENT

    monkeypatch.setattr(controller._api, "get_tip_presence_status", present_sensor)
    with pytest.raises(TipStateError, match="sensor reported PRESENT"):
        await controller.drop_tip(OT3Mount.LEFT, location=location, home_after=False)

    instrument = controller._api.hardware_instruments[OT3Mount.LEFT.to_mount()]
    assert instrument.has_tip
    assert instrument.current_tip_length == pytest.approx(95.6)


@pytest.mark.parametrize("coordinate", [-1e9, 1e9])
async def test_tip_location_outside_machine_bounds_is_rejected_before_motion(
    controller: FlexMotionController,
    coordinate: float,
) -> None:
    with pytest.raises(MovementOutOfBoundsError):
        await controller.pick_up_tip(
            OT3Mount.LEFT,
            location=Point(coordinate, 0.0, 0.0),
            tip_length=95.6,
            prep_after=False,
        )
    assert await controller.get_tip_presence(OT3Mount.LEFT) is TipStateType.ABSENT


async def test_halt_gates_non_tip_motion_until_full_home(controller: FlexMotionController) -> None:
    location = await controller.gantry_position(OT3Mount.LEFT)
    await controller.stop()
    with pytest.raises(NotHomedError, match="Fully re-home"):
        await controller.move_to(OT3Mount.LEFT, location)

    await controller.home()
    await controller.move_to(OT3Mount.LEFT, location)
