"""
One-call recovery: clear every recovery gate that does not need a person, in a safe order.

After an E-stop, halt, stall, or interrupted tip or gripper action the connector
blocks motion until the robot is back in a known state. ``RecoveryCoordinator``
performs the standard sequence an operator would otherwise run by hand:

1. Acknowledge a released E-stop (refuses while it is still pressed).
2. Fully home the robot (only with the front door closed).
3. Reconcile each quarantined pipette's tip state from its sensor.
4. Home the gripper jaw (or clear the gate when no gripper is attached).
5. Fully home every Flex Stacker that lost its position.

Anything that needs a decision from a person (a tip of unknown length, an
invalid deck ledger, a disconnected Stacker) is left in place and reported with
``ManualStepsRequiredError`` after the automatic steps have run.

Recovery never starts while a Protocol Engine run owns the hardware; the shared
hardware lock enforces that for every step.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from opentrons.hardware_control.types import EstopState, OT3Mount

from ._errors import MachineErrorStateError, OperationHaltedError, TipStateError
from .events import EVENTS, EventKind, EventLog

log = logging.getLogger(__name__)


class EmergencyStopEngagedError(Exception):
    """
    The E-stop is pressed, so recovery cannot start.

    Make the robot safe, release the E-stop by twisting it, and run Recover again.
    """


class DoorOpenError(Exception):
    """
    The front door is open, so the robot will not home automatically.

    Close the front door, make sure nothing is in the gantry's path, and run Recover again.
    """


class ManualStepsRequiredError(Exception):
    """
    Automatic recovery finished, but some steps need a person.

    The message lists each remaining action (for example removing a tip of unknown
    length, reconciling the deck ledger, or reconnecting a module). Complete them
    and run Recover again; the Status property shows what is still required.
    """


class RecoveryStep(enum.Enum):
    """One action in the recovery sequence."""

    ACKNOWLEDGE_EMERGENCY_STOP = "ACKNOWLEDGE_EMERGENCY_STOP"
    HOME_ROBOT = "HOME_ROBOT"
    RECONCILE_TIP_STATE = "RECONCILE_TIP_STATE"
    HOME_GRIPPER_JAW = "HOME_GRIPPER_JAW"
    HOME_STACKER = "HOME_STACKER"


@dataclass(frozen=True)
class PlannedStep:
    """A recovery step and the object it applies to."""

    step: RecoveryStep
    target: str

    def describe(self) -> str:
        """Operator-facing description."""
        return {
            RecoveryStep.ACKNOWLEDGE_EMERGENCY_STOP: "acknowledge the released E-stop",
            RecoveryStep.HOME_ROBOT: "fully home the robot",
            RecoveryStep.RECONCILE_TIP_STATE: f"reconcile the {self.target} tip state from its sensor",
            RecoveryStep.HOME_GRIPPER_JAW: "home the gripper jaw",
            RecoveryStep.HOME_STACKER: f"fully home Flex Stacker {self.target}",
        }[self.step]


@dataclass(frozen=True)
class StackerRecoveryTarget:
    """A Stacker the coordinator may home."""

    serial: str
    recovery_required: Callable[[], bool]
    connected: Callable[[], bool]
    home: Callable[[], Awaitable[None]]


class RecoveryCoordinator:
    """Plan and run the recovery sequence for one robot."""

    def __init__(
        self,
        motion: object,
        gripper: object | None = None,
        *,
        stackers: Iterable[StackerRecoveryTarget] = (),
        deck_valid: Callable[[], bool] | None = None,
        events: EventLog = EVENTS,
    ) -> None:
        """
        Bind the controllers that share one robot API.

        Args:
            motion: ``FlexMotionController`` (gates, homing, E-stop, tips).
            gripper: ``FlexGripperController``, when the robot can have a gripper.
            stackers: Flex Stackers registered at startup.
            deck_valid: Whether the durable deck ledger is valid, when one is configured.
            events: Journal that receives each completed step.
        """
        self._motion = motion
        self._gripper = gripper
        self._stackers = tuple(stackers)
        self._deck_valid = deck_valid
        self._events = events
        self._running = False
        self._lock = asyncio.Lock()
        self._generation = 0

    @property
    def running(self) -> bool:
        """Whether a recovery is in progress."""
        return self._running

    def plan(self) -> list[PlannedStep]:
        """Return the automatic steps the current state requires, in execution order."""
        motion = self._motion
        state = motion.recovery_state
        steps: list[PlannedStep] = []
        estop = motion.machine_status().estop
        if estop == EstopState.LOGICALLY_ENGAGED.name:
            steps.append(PlannedStep(RecoveryStep.ACKNOWLEDGE_EMERGENCY_STOP, "robot"))
        if state.rehome_required:
            steps.append(PlannedStep(RecoveryStep.HOME_ROBOT, "robot"))
        steps.extend(
            PlannedStep(RecoveryStep.RECONCILE_TIP_STATE, mount) for mount in sorted(state.tip_reconciliation_required)
        )
        if state.gripper_home_required:
            steps.append(PlannedStep(RecoveryStep.HOME_GRIPPER_JAW, "gripper"))
        steps.extend(
            PlannedStep(RecoveryStep.HOME_STACKER, stacker.serial)
            for stacker in self._stackers
            if _safe(stacker.recovery_required) and _safe(stacker.connected)
        )
        return steps

    def manual_actions(self) -> list[str]:
        """Return the actions that need a person, given the current state."""
        actions: list[str] = []
        if self._deck_valid is not None and not _safe(self._deck_valid):
            actions.append("reconcile the deck ledger after checking the deck by hand (flexctl deck <robot> reconcile)")
        actions.extend(
            f"reconnect Flex Stacker {stacker.serial} (USB and power) so it can be homed"
            for stacker in self._stackers
            if _safe(stacker.recovery_required) and not _safe(stacker.connected)
        )
        return actions

    def acknowledge_emergency_stop(self) -> str:
        """
        Acknowledge a released E-stop and return the resulting state name.

        Raises:
            EmergencyStopEngagedError: The E-stop is still pressed.
        """
        try:
            state = self._motion.acknowledge_emergency_stop()
        except MachineErrorStateError as exc:
            raise EmergencyStopEngagedError(str(exc)) from exc
        self._events.record(EventKind.RECOVERY, "AcknowledgeEmergencyStop", f"E-stop now {state}")
        return state

    async def recover(self, progress: Callable[[float, str], None] | None = None) -> list[str]:
        """
        Run every automatic step and return what was done, in order.

        Raises:
            EmergencyStopEngagedError: The E-stop is still pressed.
            DoorOpenError: Homing is needed but the front door is open.
            ManualStepsRequiredError: Steps that need a person remain after the automatic steps.
        """
        report = progress or (lambda _fraction, _message: None)
        if self._lock.locked():
            msg = "A recovery is already running; wait for it to finish."
            raise ManualStepsRequiredError(msg)
        async with self._lock:
            self._running = True
            try:
                return await self._recover(report)
            finally:
                self._running = False

    async def _recover(self, report: Callable[[float, str], None]) -> list[str]:
        motion = self._motion
        if motion.machine_status().estop == EstopState.PHYSICALLY_ENGAGED.name:
            message = "The E-stop is pressed. Make the robot safe, release (twist) the E-stop, and run Recover again."
            raise EmergencyStopEngagedError(message)
        plan = self.plan()
        needs_motion = any(step.step is not RecoveryStep.ACKNOWLEDGE_EMERGENCY_STOP for step in plan)
        if needs_motion and motion.machine_status().door_open:
            message = (
                "The front door is open. Close it, check that nothing is in the gantry's path, and run Recover again."
            )
            raise DoorOpenError(message)
        done: list[str] = []
        manual: list[str] = []
        self._generation = motion.recovery_state.generation
        self._events.record(
            EventKind.RECOVERY,
            "Recover",
            "started: " + (", ".join(step.describe() for step in plan) or "nothing to do automatically"),
        )
        for index, step in enumerate(plan):
            self._assert_not_halted()
            report(index / max(len(plan), 1), f"Step {index + 1} of {len(plan)}: {step.describe()}.")
            try:
                await self._run_step(step)
            except TipStateError as exc:
                manual.append(str(exc))
                continue
            done.append(step.describe())
            self._events.record(EventKind.RECOVERY, "Recover", f"done: {step.describe()}")
        self._assert_not_halted()
        manual.extend(self.manual_actions())
        report(1.0, "Recovery finished." if not manual else "Automatic steps finished; manual steps remain.")
        if manual:
            message = "Remaining manual recovery: " + "; ".join(manual)
            self._events.record(EventKind.RECOVERY, "Recover", message)
            raise ManualStepsRequiredError(message)
        self._events.record(EventKind.RECOVERY, "Recover", "finished; the robot is ready")
        return done

    def _assert_not_halted(self) -> None:
        if self._motion.recovery_state.generation != self._generation:
            message = (
                "An emergency stop or halt happened during Recover, so it stopped. Make the robot safe "
                "and run Recover again."
            )
            raise OperationHaltedError(message)

    def _assert_safe_to_move(self) -> None:
        """Re-check, immediately before motion, everything that was checked when Recover started."""
        self._assert_not_halted()
        status = self._motion.machine_status()
        if status.estop not in {EstopState.DISENGAGED.name, EstopState.NOT_PRESENT.name}:
            message = "The E-stop is engaged. Release it and run Recover again."
            raise EmergencyStopEngagedError(message)
        if status.door_open:
            message = "The front door was opened during Recover. Close it and run Recover again."
            raise DoorOpenError(message)

    def _guard_home(self) -> bool:
        self._assert_safe_to_move()
        if not self._motion.recovery_state.rehome_required:
            log.info("Recover skipped the full home: the robot was homed while Recover waited for it")
            return False
        return True

    async def _run_step(self, step: PlannedStep) -> None:
        motion = self._motion
        if step.step is RecoveryStep.ACKNOWLEDGE_EMERGENCY_STOP:
            self.acknowledge_emergency_stop()
        elif step.step is RecoveryStep.HOME_ROBOT:
            await motion.home(guard=self._guard_home)
        elif step.step is RecoveryStep.RECONCILE_TIP_STATE:
            await motion.reconcile_tip_state_from_sensor(OT3Mount[step.target])
        elif step.step is RecoveryStep.HOME_GRIPPER_JAW:
            gripper = self._gripper
            if gripper is None or not gripper.attached:
                if gripper is not None:
                    gripper.clear_jaw_gate_if_detached()
                else:
                    motion.recovery_state.mark_gripper_homed(motion.recovery_state.generation)
            else:
                self._assert_safe_to_move()
                await gripper.home_jaw()
        elif step.step is RecoveryStep.HOME_STACKER:
            stacker = next(item for item in self._stackers if item.serial == step.target)
            self._assert_safe_to_move()
            await stacker.home()


def _safe(read: Callable[[], bool]) -> bool:
    try:
        value = read()
    except Exception:  # noqa: BLE001 - planning reads cached state and must not fail
        return False
    if asyncio.iscoroutine(value):  # a wrongly async reader is treated as unknown
        value.close()
        return False
    return bool(value)


AUTO_RECOVERY_MODES = ("off", "safe", "full")


class AutoRecovery:
    """
    Clear recovery gates automatically while the robot is idle and gated.

    ``safe`` (default) performs only steps that never move the robot: reconcile a
    quarantined tip state when the tip sensor settles it, and clear the jaw gate
    when no gripper is attached. ``full`` additionally runs the complete Recover
    sequence (which homes the robot) once a gate has been set for ``full_delay_s``
    with the E-stop released and the door closed; use it only where an unattended
    home after a fault is acceptable. ``off`` disables automatic recovery.
    """

    def __init__(
        self,
        coordinator: RecoveryCoordinator,
        *,
        mode: str,
        gated_and_idle: Callable[[], bool],
        interval_s: float = 5.0,
        full_delay_s: float = 30.0,
        retry_after_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        events: EventLog = EVENTS,
    ) -> None:
        """Configure; ``gated_and_idle`` is true while recovery is required and nothing else runs."""
        if mode not in AUTO_RECOVERY_MODES:
            msg = f"auto_recovery must be one of {', '.join(AUTO_RECOVERY_MODES)}."
            raise ValueError(msg)
        self._coordinator = coordinator
        self._mode = mode
        self._gated_and_idle = gated_and_idle
        self._interval_s = interval_s
        self._full_delay_s = full_delay_s
        self._retry_after_s = retry_after_s
        self._clock = clock
        self._events = events
        self._gated_since: float | None = None
        self._next_attempt: dict[str, float] = {}
        self._full_recovery_failed = False

    async def run(self) -> None:
        """Check periodically until cancelled."""
        if self._mode == "off":
            return
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Automatic recovery check failed")
            await asyncio.sleep(self._interval_s)

    def _due(self, key: str) -> bool:
        return self._clock() >= self._next_attempt.get(key, 0.0)

    def _defer(self, key: str) -> None:
        self._next_attempt[key] = self._clock() + self._retry_after_s

    async def check(self) -> list[str]:
        """Perform the automatic steps that are due now; return their descriptions."""
        if self._mode == "off" or self._coordinator.running or not _safe(self._gated_and_idle):
            self._gated_since = None
            return []
        now = self._clock()
        if self._gated_since is None:
            self._gated_since = now
        if self._full_recovery_failed and not self._coordinator.plan():
            # A person recovered the robot; automatic full recovery may run again next time.
            self._full_recovery_failed = False
        motion = self._coordinator._motion
        state = motion.recovery_state
        done: list[str] = []
        for mount in sorted(state.tip_reconciliation_required):
            key = f"tip:{mount}"
            if not self._due(key):
                continue
            try:
                await motion.reconcile_tip_state_from_sensor(OT3Mount[mount])
            except TipStateError as exc:
                self._defer(key)
                log.info("Automatic tip reconciliation on %s needs a person: %s", mount, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - retried later, reported in the journal
                self._defer(key)
                self._events.record_error(f"AutoRecovery {mount}", exc)
                continue
            done.append(f"reconciled the {mount} tip state from its sensor")
        gripper = self._coordinator._gripper
        if state.gripper_home_required and gripper is not None and gripper.clear_jaw_gate_if_detached():
            done.append("cleared the jaw gate (no gripper attached)")
        if (
            self._mode == "full"
            and not self._full_recovery_failed
            and self._due("recover")
            and now - self._gated_since >= self._full_delay_s
            and self._coordinator.plan()
        ):
            status = motion.machine_status()
            if status.estop in {EstopState.DISENGAGED.name, EstopState.NOT_PRESENT.name} and not status.door_open:
                self._defer("recover")
                try:
                    done.extend(await self._coordinator.recover())
                except Exception as exc:  # noqa: BLE001 - a person resolves it; Status shows why
                    # Never retry a failed automatic home: whatever stopped it (an obstacle,
                    # a stall) would be hit again. A person checks the robot and runs Recover.
                    self._full_recovery_failed = True
                    self._events.record_error("AutoRecovery", exc)
                    self._events.record(
                        EventKind.RECOVERY,
                        "AutoRecovery",
                        "automatic full recovery stopped after a failure; check the robot and run Recover",
                    )
        for item in done:
            self._events.record(EventKind.RECOVERY, "AutoRecovery", item)
        return done


RECOVERY_ERRORS: Sequence[type[Exception]] = (EmergencyStopEngagedError, DoorOpenError, ManualStepsRequiredError)

__all__ = [
    "AUTO_RECOVERY_MODES",
    "RECOVERY_ERRORS",
    "AutoRecovery",
    "DoorOpenError",
    "EmergencyStopEngagedError",
    "ManualStepsRequiredError",
    "PlannedStep",
    "RecoveryCoordinator",
    "RecoveryStep",
    "StackerRecoveryTarget",
]
