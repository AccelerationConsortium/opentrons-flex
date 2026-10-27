"""State machine, event journal, recorded gate causes, and fault-driven re-home gates."""

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import EstopState, OT3Mount
from opentrons.types import Point
from opentrons_shared_data.errors.exceptions import (
    CanbusCommunicationError,
    ModuleCommunicationError,
    PipetteLiquidNotFoundError,
    PositionUnknownError,
    PythonException,
    StallOrCollisionDetectedError,
    TipPickupFailedError,
)

from unitelabs.opentrons_flex.io import FlexMotionController, StallDetectedError
from unitelabs.opentrons_flex.io.events import EventKind, EventLog
from unitelabs.opentrons_flex.io.flex_motion import position_untrusted_after
from unitelabs.opentrons_flex.io.recovery_state import HardwareRecoveryState
from unitelabs.opentrons_flex.io.robot_status import (
    HttpServerState,
    RobotState,
    RobotStatusSource,
)


@pytest_asyncio.fixture
async def motion() -> FlexMotionController:
    api = await OT3API.build_hardware_simulator()
    await api.home()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    yield controller
    await api.clean_up()


# ------------------------------------------------------------------ journal


def test_identical_events_are_folded_and_counted() -> None:
    clock = {"now": 0.0}
    log = EventLog(repeat_window_s=60.0)
    import unitelabs.opentrons_flex.io.events as events_module

    original = events_module.time.monotonic
    events_module.time.monotonic = lambda: clock["now"]
    try:
        for _ in range(5):
            log.record(EventKind.ERROR, "Status", "module unplugged")
        assert len(log.recent()) == 1
        clock["now"] = 61.0
        log.record(EventKind.ERROR, "Status", "module unplugged")
    finally:
        events_module.time.monotonic = original

    messages = [event.message for event in log.recent()]
    assert messages == ["module unplugged", "module unplugged (repeated 4 more times)"]
    assert log.last_error is not None and log.last_error.sequence == 2


def test_journal_is_bounded() -> None:
    log = EventLog(capacity=3)
    for index in range(5):
        log.record(EventKind.STATE, "test", f"event {index}")
    assert [event.message for event in log.recent()] == ["event 2", "event 3", "event 4"]


# ------------------------------------------------------------ recorded causes


def test_gate_cause_is_persisted_and_restored_once(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    first = HardwareRecoveryState()
    first.bind_persistence(path)
    first.require_rehome("StallOrCollisionDetectedError while the robot moved")
    first.wait_for_persistence()
    assert json.loads(path.read_text())["reason"].startswith("StallOrCollision")

    for _ in range(2):
        restarted = HardwareRecoveryState()
        restarted.bind_persistence(path)
        restarted.wait_for_persistence()
    assert restarted.rehome_required
    assert restarted.reason.count("restored after a connector restart") == 1


async def test_clearing_every_gate_clears_the_cause() -> None:
    state = HardwareRecoveryState()
    state.mark_halted("EmergencyStop was requested")
    assert state.reason == "EmergencyStop was requested"
    state.mark_fully_homed(state.generation)
    assert state.reason == ""


def test_persistence_failure_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    state = HardwareRecoveryState()
    state.bind_persistence(blocker / "recovery.json")
    state.require_rehome("test")
    state.wait_for_persistence()
    assert "recovery.json" in state.persistence_error


# --------------------------------------------------- faults invalidate position


@pytest.mark.parametrize(
    ("error", "untrusted"),
    [
        (StallOrCollisionDetectedError(), True),
        (CanbusCommunicationError(), True),
        (PythonException(RuntimeError("driver")), True),
        (TipPickupFailedError(), False),
        (PipetteLiquidNotFoundError(), False),
        (ModuleCommunicationError(), False),
        (PositionUnknownError(), False),
        (ValueError("validation"), False),
    ],
)
def test_which_failures_require_a_rehome(error: Exception, untrusted: bool) -> None:
    assert position_untrusted_after(error) is untrusted


async def test_a_stall_during_a_move_requires_a_full_home(
    motion: FlexMotionController, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stalled(*_args: object, **_kwargs: object) -> None:
        raise StallOrCollisionDetectedError

    monkeypatch.setattr(motion._api, "move_rel", stalled)

    with pytest.raises(StallDetectedError):
        await motion.move_rel(OT3Mount.LEFT, Point(0, 0, -1))

    state = motion.recovery_state
    assert state.rehome_required
    assert "StallOrCollisionDetectedError" in state.reason


# ------------------------------------------------------------- state machine


def _source(motion: FlexMotionController, **kwargs: object) -> RobotStatusSource:
    return RobotStatusSource(motion, events=kwargs.pop("events", EventLog()), **kwargs)


async def test_emergency_stop_has_its_own_state(motion: FlexMotionController, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(motion._api, "get_estop_state", lambda: EstopState.PHYSICALLY_ENGAGED)
    snapshot = _source(motion).snapshot()
    assert snapshot.state is RobotState.EMERGENCY_STOP
    assert "release" in snapshot.state_reason.lower()


async def test_recovery_required_explains_cause_and_next_steps(motion: FlexMotionController) -> None:
    await motion.stop()
    snapshot = _source(motion).snapshot()
    assert snapshot.state is RobotState.RECOVERY_REQUIRED
    assert "EmergencyStop was requested" in snapshot.state_reason
    assert "Recover" in snapshot.state_reason


async def test_recovering_overrides_recovery_required(motion: FlexMotionController) -> None:
    await motion.stop()
    assert _source(motion, recovering=lambda: True).snapshot().state is RobotState.RECOVERING


async def test_transitions_are_journaled_but_busy_ready_toggles_are_not(motion: FlexMotionController) -> None:
    journal = EventLog()
    busy = {"value": False}
    source = _source(motion, events=journal)
    original = type(motion).hardware_busy
    try:
        type(motion).hardware_busy = property(lambda _self: busy["value"])
        first = source.snapshot()
        busy["value"] = True
        assert source.snapshot().state is RobotState.BUSY
        busy["value"] = False
        assert source.snapshot().state_since >= first.state_since
        await motion.stop()
        source.snapshot()
    finally:
        type(motion).hardware_busy = original

    transitions = [event.message for event in journal.recent() if event.kind is EventKind.STATE]
    assert transitions == [
        "READY -> RECOVERY_REQUIRED: EmergencyStop was requested. Required: Fully re-home all robot axes. "
        "RecoveryController.Recover performs these steps."
    ]


async def test_state_since_changes_only_on_transitions(motion: FlexMotionController) -> None:
    source = _source(motion)
    first = source.snapshot()
    await asyncio.sleep(0.01)
    assert source.snapshot().state_since == first.state_since
    await motion.stop()
    assert source.snapshot().state_since > first.state_since


async def test_hardware_error_lists_disconnected_modules(motion: FlexMotionController) -> None:
    source = _source(
        motion,
        registered_modules=[("HEATER_SHAKER", "HS-1", lambda: False)],
        http_server_state=lambda: HttpServerState.RUNNING,
    )
    snapshot = source.snapshot()
    assert snapshot.state is RobotState.HARDWARE_ERROR
    assert "HEATER_SHAKER HS-1 is disconnected" in snapshot.state_reason


async def test_warnings_and_last_error_are_reported(motion: FlexMotionController) -> None:
    journal = EventLog()
    journal.record_error("MotionController.MoveRelative", StallDetectedError("stalled on X"))
    source = _source(motion, events=journal, warnings=lambda: ["robot-server data is temporary"])
    motion.recovery_state.persistence_error = "/var/lib/x: read-only file system"

    snapshot = source.snapshot()

    assert "StallDetectedError: stalled on X" in snapshot.last_error
    assert snapshot.warnings[0].startswith("Recovery gates could not be saved")
    assert "robot-server data is temporary" in snapshot.warnings
