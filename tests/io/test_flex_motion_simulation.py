"""Simulation-mode tests for FlexMotionController.

Drives a real ``OT3API`` hardware *simulator* (no mocks) through the controller,
confirming the feature -> controller -> OT3API chain is wired correctly. The
simulator maintains internal position state, so motion is observable.

Runs fully offline: depends only on ``opentrons`` (the OT3 simulator), not on the
unitelabs CDK.
"""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import DoorState, EstopState, OT3Mount
from opentrons.types import Point

from unitelabs.opentrons_flex.io import FlexMotionController, MachineErrorStateError, MachineState


@pytest_asyncio.fixture
async def controller() -> FlexMotionController:
    api = await OT3API.build_hardware_simulator()
    ctrl = FlexMotionController.from_api(api, lock=asyncio.Lock())
    yield ctrl
    await api.clean_up()


async def test_is_simulating_true(controller: FlexMotionController):
    assert controller.is_simulating is True


async def test_home_then_position_is_a_point(controller: FlexMotionController):
    await controller.home()
    pos = await controller.gantry_position(OT3Mount.LEFT)
    assert isinstance(pos, Point)


async def test_move_rel_offsets_position(controller: FlexMotionController):
    await controller.home()
    start = await controller.gantry_position(OT3Mount.LEFT)
    after = await controller.move_rel(OT3Mount.LEFT, Point(x=-10, y=-5, z=-3))
    assert after.x == pytest.approx(start.x - 10, abs=1e-3)
    assert after.y == pytest.approx(start.y - 5, abs=1e-3)
    assert after.z == pytest.approx(start.z - 3, abs=1e-3)


async def test_move_to_sets_absolute_position(controller: FlexMotionController):
    await controller.home()
    start = await controller.gantry_position(OT3Mount.LEFT)
    target = Point(x=start.x - 20, y=start.y - 20, z=start.z - 20)
    after = await controller.move_to(OT3Mount.LEFT, target)
    assert after.x == pytest.approx(target.x, abs=1e-3)
    assert after.y == pytest.approx(target.y, abs=1e-3)
    assert after.z == pytest.approx(target.z, abs=1e-3)


async def test_lights_roundtrip_returns_dict(controller: FlexMotionController):
    await controller.set_lights(button=True, rails=True)
    state = await controller.get_lights()
    assert set(state) >= {"button", "rails"}


async def test_attached_instruments_keyed_by_mount(controller: FlexMotionController):
    await controller.cache_instruments()
    instruments = controller.attached_instruments
    # No pipettes configured on the bare simulator -> mounts absent or empty.
    assert all(not v for v in instruments.values())


async def test_pause_resume_stop_do_not_raise(controller: FlexMotionController):
    await controller.home()
    controller.pause()
    controller.resume()
    await controller.stop()


# ------------------------------------------------------ machine error state (Pitfall #2)


async def test_machine_status_healthy_on_simulator(controller: FlexMotionController):
    """A fresh simulator reports a healthy, non-error machine state."""
    state = controller.machine_status()
    assert isinstance(state, MachineState)
    assert state.estop == "DISENGAGED"
    assert state.door_open is False
    assert state.is_error_state is False
    assert state.message == ""


async def test_machine_status_reports_estop_engaged(controller: FlexMotionController, monkeypatch: pytest.MonkeyPatch):
    """When the E-stop is engaged, machine_status flags an error state with a hint."""
    monkeypatch.setattr(controller._api, "get_estop_state", lambda: EstopState.PHYSICALLY_ENGAGED)

    state = controller.machine_status()
    assert state.estop == "PHYSICALLY_ENGAGED"
    assert state.is_error_state is True
    assert "re-home" in state.message.lower()


async def test_machine_status_reports_open_door(controller: FlexMotionController, monkeypatch: pytest.MonkeyPatch):
    """An open door is surfaced for information but is not on its own an error state."""
    monkeypatch.setattr(type(controller._api), "door_state", property(lambda self: DoorState.OPEN))

    state = controller.machine_status()
    assert state.door_open is True
    assert state.is_error_state is False


async def test_not_present_estop_is_not_an_error(controller: FlexMotionController, monkeypatch: pytest.MonkeyPatch):
    """NOT_PRESENT (no E-stop hardware / simulator) must not be treated as an error."""
    monkeypatch.setattr(controller._api, "get_estop_state", lambda: EstopState.NOT_PRESENT)

    state = controller.machine_status()
    assert state.estop == "NOT_PRESENT"
    assert state.is_error_state is False


async def test_move_to_raises_when_machine_enters_error_state(
    controller: FlexMotionController, monkeypatch: pytest.MonkeyPatch
):
    """A move that "succeeds" but leaves the robot E-stopped must not return silently."""
    await controller.home()
    start = await controller.gantry_position(OT3Mount.LEFT)

    # Simulate the E-stop becoming engaged during/after the move.
    monkeypatch.setattr(controller._api, "get_estop_state", lambda: EstopState.LOGICALLY_ENGAGED)

    with pytest.raises(MachineErrorStateError):
        await controller.move_to(OT3Mount.LEFT, Point(x=start.x - 5, y=start.y - 5, z=start.z - 5))


async def test_move_rel_raises_when_machine_enters_error_state(
    controller: FlexMotionController, monkeypatch: pytest.MonkeyPatch
):
    """MoveRelative surfaces a hidden error state the same way MoveTo does."""
    await controller.home()
    monkeypatch.setattr(controller._api, "get_estop_state", lambda: EstopState.PHYSICALLY_ENGAGED)

    with pytest.raises(MachineErrorStateError):
        await controller.move_rel(OT3Mount.LEFT, Point(x=-1, y=-1, z=-1))


async def test_home_raises_when_machine_enters_error_state(
    controller: FlexMotionController, monkeypatch: pytest.MonkeyPatch
):
    """Home surfaces a hidden error state rather than reporting a false success."""
    monkeypatch.setattr(controller._api, "get_estop_state", lambda: EstopState.PHYSICALLY_ENGAGED)

    with pytest.raises(MachineErrorStateError):
        await controller.home()
