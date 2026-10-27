"""One-call recovery and automatic recovery against the OT3 simulator."""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import DoorState, EstopState, OT3Mount, TipStateType

from unitelabs.opentrons_flex.io import FlexGripperController, FlexMotionController
from unitelabs.opentrons_flex.io.events import EventKind, EventLog
from unitelabs.opentrons_flex.io.recovery import (
    AutoRecovery,
    DoorOpenError,
    EmergencyStopEngagedError,
    ManualStepsRequiredError,
    RecoveryCoordinator,
    RecoveryStep,
    StackerRecoveryTarget,
)


@pytest_asyncio.fixture
async def robot() -> tuple[OT3API, FlexMotionController, FlexGripperController]:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": "p1000_single_v3.5", "id": "P1K-SIM"}},
    )
    await api.home()
    lock = asyncio.Lock()
    yield api, FlexMotionController.from_api(api, lock=lock), FlexGripperController.from_api(api, lock=lock)
    await api.clean_up()


def _estop(api: OT3API, monkeypatch: pytest.MonkeyPatch, initial: EstopState) -> dict[str, EstopState]:
    state = {"value": initial}
    monkeypatch.setattr(api, "get_estop_state", lambda: state["value"])

    def acknowledge() -> None:
        if state["value"] is EstopState.LOGICALLY_ENGAGED:
            state["value"] = EstopState.DISENGAGED

    monkeypatch.setattr(api, "estop_acknowledge_and_clear", acknowledge)
    return state


async def test_nothing_to_do_returns_no_steps(robot: tuple) -> None:
    _, motion, gripper = robot
    coordinator = RecoveryCoordinator(motion, gripper, events=EventLog())
    assert coordinator.plan() == []
    assert await coordinator.recover() == []


async def test_recover_after_a_halt_homes_and_reports_progress(robot: tuple) -> None:
    _, motion, gripper = robot
    await motion.stop()
    journal = EventLog()
    coordinator = RecoveryCoordinator(motion, gripper, events=journal)
    assert [step.step for step in coordinator.plan()] == [RecoveryStep.HOME_ROBOT]
    progress: list[str] = []

    done = await coordinator.recover(lambda _fraction, message: progress.append(message))

    assert done == ["fully home the robot"]
    assert motion.recovery_state.operation_ready
    assert progress[0].startswith("Step 1 of 1")
    assert [event.kind for event in journal.recent()][-1] is EventKind.RECOVERY


async def test_released_estop_is_acknowledged_then_robot_homed(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    api, motion, gripper = robot
    estop = _estop(api, monkeypatch, EstopState.LOGICALLY_ENGAGED)
    motion.recovery_state.require_rehome("E-stop pressed during a move")
    coordinator = RecoveryCoordinator(motion, gripper, events=EventLog())

    done = await coordinator.recover()

    assert done == ["acknowledge the released E-stop", "fully home the robot"]
    assert estop["value"] is EstopState.DISENGAGED


async def test_pressed_estop_refuses_recovery(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    api, motion, gripper = robot
    _estop(api, monkeypatch, EstopState.PHYSICALLY_ENGAGED)
    motion.recovery_state.require_rehome("test")
    with pytest.raises(EmergencyStopEngagedError, match="release"):
        await RecoveryCoordinator(motion, gripper, events=EventLog()).recover()
    assert motion.recovery_state.rehome_required


async def test_open_door_refuses_homing(robot: tuple) -> None:
    api, motion, gripper = robot
    api.door_state = DoorState.OPEN
    motion.recovery_state.require_rehome("test")
    with pytest.raises(DoorOpenError):
        await RecoveryCoordinator(motion, gripper, events=EventLog()).recover()


async def test_tip_quarantine_is_settled_by_the_sensor(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    api, motion, gripper = robot

    async def no_tip(_mount: OT3Mount) -> TipStateType:
        return TipStateType.ABSENT

    monkeypatch.setattr(api, "get_tip_presence_status", no_tip)
    motion.recovery_state.require_tip_reconciliation("LEFT", "A tip pickup was cancelled")

    done = await RecoveryCoordinator(motion, gripper, events=EventLog()).recover()

    assert done == ["reconcile the LEFT tip state from its sensor"]
    assert not motion.recovery_state.tip_reconciliation_required


async def test_unknown_tip_needs_a_person_but_other_steps_still_run(
    robot: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, motion, gripper = robot

    async def tip(_mount: OT3Mount) -> TipStateType:
        return TipStateType.PRESENT

    monkeypatch.setattr(api, "get_tip_presence_status", tip)
    await motion.stop()
    motion.recovery_state.require_tip_reconciliation("LEFT")

    with pytest.raises(ManualStepsRequiredError, match="length is unknown"):
        await RecoveryCoordinator(motion, gripper, events=EventLog()).recover()

    assert not motion.recovery_state.rehome_required, "the home step still ran"
    assert motion.recovery_state.tip_reconciliation_required == {"LEFT"}


async def test_jaw_gate_without_a_gripper_is_cleared(robot: tuple) -> None:
    _, motion, gripper = robot
    motion.recovery_state.require_gripper_home("A labware move was cancelled")
    done = await RecoveryCoordinator(motion, gripper, events=EventLog()).recover()
    assert done == ["home the gripper jaw"]
    assert motion.recovery_state.operation_ready


async def test_invalid_deck_and_detached_stacker_are_manual(robot: tuple) -> None:
    _, motion, gripper = robot
    homed: list[str] = []

    async def home() -> None:
        homed.append("ST-1")

    coordinator = RecoveryCoordinator(
        motion,
        gripper,
        stackers=[
            StackerRecoveryTarget("ST-1", lambda: True, lambda: True, home),
            StackerRecoveryTarget("ST-2", lambda: True, lambda: False, home),
        ],
        deck_valid=lambda: False,
        events=EventLog(),
    )

    with pytest.raises(ManualStepsRequiredError) as excinfo:
        await coordinator.recover()

    assert homed == ["ST-1"]
    assert "deck ledger" in str(excinfo.value)
    assert "reconnect Flex Stacker ST-2" in str(excinfo.value)


async def test_concurrent_recover_is_refused(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _, motion, gripper = robot
    await motion.stop()
    release = asyncio.Event()
    original = motion.home

    async def slow_home(*args: object, **kwargs: object) -> None:
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(motion, "home", slow_home)
    coordinator = RecoveryCoordinator(motion, gripper, events=EventLog())
    first = asyncio.create_task(coordinator.recover())
    await asyncio.sleep(0)
    assert coordinator.running
    with pytest.raises(ManualStepsRequiredError, match="already running"):
        await coordinator.recover()
    release.set()
    await first
    assert not coordinator.running


# --------------------------------------------------------------- automatic


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_safe_auto_recovery_never_moves(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    api, motion, gripper = robot

    async def no_tip(_mount: OT3Mount) -> TipStateType:
        return TipStateType.ABSENT

    monkeypatch.setattr(api, "get_tip_presence_status", no_tip)
    await motion.stop()
    motion.recovery_state.require_tip_reconciliation("LEFT")
    motion.recovery_state.require_gripper_home()
    clock = _Clock()
    auto = AutoRecovery(
        RecoveryCoordinator(motion, gripper, events=EventLog()),
        mode="safe",
        gated_and_idle=lambda: True,
        clock=clock,
        events=EventLog(),
    )

    done = await auto.check()
    clock.now = 3600.0
    done += await auto.check()

    assert "reconciled the LEFT tip state from its sensor" in done
    assert motion.recovery_state.rehome_required, "safe mode must not home the robot"


async def test_full_auto_recovery_homes_after_the_delay(robot: tuple) -> None:
    _, motion, gripper = robot
    await motion.stop()
    clock = _Clock()
    auto = AutoRecovery(
        RecoveryCoordinator(motion, gripper, events=EventLog()),
        mode="full",
        gated_and_idle=lambda: True,
        full_delay_s=30.0,
        clock=clock,
        events=EventLog(),
    )

    assert await auto.check() == []
    clock.now = 31.0
    assert await auto.check() == ["fully home the robot"]
    assert motion.recovery_state.operation_ready


async def test_full_auto_recovery_waits_for_a_released_estop(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    api, motion, gripper = robot
    _estop(api, monkeypatch, EstopState.LOGICALLY_ENGAGED)
    motion.recovery_state.require_rehome("test")
    clock = _Clock()
    auto = AutoRecovery(
        RecoveryCoordinator(motion, gripper, events=EventLog()),
        mode="full",
        gated_and_idle=lambda: True,
        clock=clock,
        events=EventLog(),
    )
    await auto.check()
    clock.now = 100.0
    assert await auto.check() == [], "a person acknowledges the E-stop; it is never auto-acknowledged"
    assert motion.recovery_state.rehome_required


async def test_auto_recovery_is_idle_while_busy(robot: tuple) -> None:
    _, motion, gripper = robot
    motion.recovery_state.require_gripper_home()
    auto = AutoRecovery(
        RecoveryCoordinator(motion, gripper, events=EventLog()),
        mode="safe",
        gated_and_idle=lambda: False,
        events=EventLog(),
    )
    assert await auto.check() == []
    assert motion.recovery_state.gripper_home_required


def test_auto_recovery_mode_is_validated(robot: tuple) -> None:
    with pytest.raises(ValueError, match="auto_recovery"):
        AutoRecovery(object(), mode="always", gated_and_idle=lambda: True)  # type: ignore[arg-type]


# ------------------------------------------------- review regression tests


async def test_auto_recovery_never_homes_while_a_client_command_runs(
    robot: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from unitelabs.opentrons_flex import _auto_recovery_may_run
    from unitelabs.opentrons_flex.io.robot_status import RobotStatusSource

    api, motion, gripper = robot
    await motion.stop()
    homes: list[str] = []
    original = api.home

    async def slow_home(*args: object, **kwargs: object) -> None:
        homes.append(asyncio.current_task().get_name())
        await asyncio.sleep(0.3)
        await original(*args, **kwargs)

    monkeypatch.setattr(api, "home", slow_home)
    source = RobotStatusSource(motion, events=EventLog())
    hardware = SimpleNamespace(lock=motion._lock, motion=motion)
    auto = AutoRecovery(
        RecoveryCoordinator(motion, gripper, events=EventLog()),
        mode="full",
        full_delay_s=0.0,
        gated_and_idle=lambda: _auto_recovery_may_run(source, hardware),
        events=EventLog(),
    )

    client = asyncio.create_task(motion.home(), name="client-home")
    await asyncio.sleep(0.05)
    assert await auto.check() == []
    await client

    assert homes == ["client-home"]


async def test_failed_automatic_home_is_never_retried(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    from opentrons_shared_data.errors.exceptions import StallOrCollisionDetectedError

    api, motion, gripper = robot
    await motion.stop()
    attempts: list[int] = []

    async def stall(*_args: object, **_kwargs: object) -> None:
        attempts.append(1)
        raise StallOrCollisionDetectedError(message="collision")

    monkeypatch.setattr(api, "home", stall)
    clock = _Clock()
    journal = EventLog()
    auto = AutoRecovery(
        RecoveryCoordinator(motion, gripper, events=EventLog()),
        mode="full",
        gated_and_idle=lambda: True,
        clock=clock,
        events=journal,
    )
    for minute in range(10):
        clock.now = minute * 61.0 + 31.0
        await auto.check()

    assert len(attempts) == 1
    assert any("stopped after a failure" in event.message for event in journal.recent())


async def test_a_halt_during_recover_stops_the_remaining_steps(robot: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    from unitelabs.opentrons_flex.io import OperationHaltedError

    api, motion, gripper = robot
    motion.recovery_state.require_tip_reconciliation("LEFT", "test")

    async def slow_sensor(_mount: OT3Mount) -> TipStateType:
        await asyncio.sleep(0.2)
        return TipStateType.ABSENT

    monkeypatch.setattr(api, "get_tip_presence_status", slow_sensor)
    homed: list[str] = []

    async def stacker_home() -> None:
        homed.append("S1")

    coordinator = RecoveryCoordinator(
        motion,
        gripper,
        stackers=[StackerRecoveryTarget("S1", lambda: True, lambda: True, stacker_home)],
        events=EventLog(),
    )
    task = asyncio.create_task(coordinator.recover())
    await asyncio.sleep(0.05)
    await motion.stop()

    with pytest.raises(OperationHaltedError):
        await task
    assert homed == []


async def test_door_opened_while_waiting_for_the_lock_blocks_the_home(robot: tuple) -> None:
    api, motion, gripper = robot
    await motion.stop()
    coordinator = RecoveryCoordinator(motion, gripper, events=EventLog())
    async with motion._lock:
        task = asyncio.create_task(coordinator.recover())
        await asyncio.sleep(0.05)
        api.door_state = DoorState.OPEN
    with pytest.raises(DoorOpenError):
        await task
    assert motion.recovery_state.rehome_required
