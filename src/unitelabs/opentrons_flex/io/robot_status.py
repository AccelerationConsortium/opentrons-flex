"""
Lock-free aggregation of every piece of connector state an operator needs at a glance.

Each value is read from cached hardware state or in-memory gates, so a status
snapshot never waits for, or interferes with, a running hardware operation.
"""

from __future__ import annotations

import datetime
import enum
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .events import EVENTS, EventKind, EventLog, describe_error
from .flex_motion import FlexMotionController

log = logging.getLogger(__name__)


class RobotState(enum.Enum):
    """
    Single summary of whether the Flex can accept a new command.

    Precedence (first match wins): INITIALIZING (restart pending), EMERGENCY_STOP,
    HARDWARE_ERROR, INITIALIZING (web server or run starting), RECOVERING,
    RECOVERY_REQUIRED, PROTOCOL_RUN_ACTIVE, BUSY, READY.
    """

    READY = "READY"
    BUSY = "BUSY"
    PROTOCOL_RUN_ACTIVE = "PROTOCOL_RUN_ACTIVE"
    RECOVERING = "RECOVERING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    HARDWARE_ERROR = "HARDWARE_ERROR"
    INITIALIZING = "INITIALIZING"


# Transitions between these states are routine and are not added to the event journal.
_ROUTINE_STATES = frozenset({RobotState.READY, RobotState.BUSY})


class HttpServerState(enum.Enum):
    """State of the embedded Opentrons HTTP robot-server."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class ModuleStatusSnapshot:
    """Connection state of one module registered at connector startup."""

    module_type: str
    serial_number: str
    connected: bool


@dataclass(frozen=True)
class RunOwnershipSnapshot:
    """Protocol Engine ownership as seen by the connector."""

    run_id: str
    status: str
    direct_control_allowed: bool


@dataclass(frozen=True)
class RobotStatusSnapshot:
    """Complete, comparable connector status."""

    state: RobotState
    simulating: bool
    estop: str
    door_open: bool
    hardware_error_message: str
    hardware_busy: bool
    rehome_required: bool
    gripper_home_required: bool
    tip_reconciliation_required: tuple[str, ...]
    recovery_guidance: str
    run: RunOwnershipSnapshot
    deck_ledger_configured: bool
    deck_ledger_valid: bool
    modules: tuple[ModuleStatusSnapshot, ...]
    unregistered_modules: tuple[str, ...]
    http_server: HttpServerState
    restart_pending: str = ""
    state_reason: str = ""
    state_since: datetime.datetime | None = None
    last_error: str = ""
    warnings: tuple[str, ...] = ()
    previous_exit: str = ""
    started_at: datetime.datetime | None = None


class RobotStatusSource:
    """Build ``RobotStatusSnapshot`` values from the shared connector components."""

    def __init__(
        self,
        motion: FlexMotionController,
        *,
        run_state: Callable[[], tuple[str | None, str, bool]] | None = None,
        deck_state: Callable[[], bool] | None = None,
        registered_modules: Iterable[tuple[str, str, Callable[[], bool]]] = (),
        attached_module_ids: Callable[[], Iterable[tuple[str, str]]] | None = None,
        http_server_state: Callable[[], HttpServerState] | None = None,
        restart_pending: Callable[[], str | None] | None = None,
        recovering: Callable[[], bool] | None = None,
        warnings: Callable[[], Iterable[str]] | None = None,
        previous_exit: str = "",
        events: EventLog = EVENTS,
    ) -> None:
        """
        Collect the status providers.

        Args:
            motion: Motion controller sharing the robot API and recovery gate.
            run_state: Returns ``(run_id, status, owns_hardware)`` for the current Protocol Engine run.
            deck_state: Returns whether the durable deck ledger is valid, when one is configured.
            registered_modules: ``(type, serial, connected)`` for modules registered at startup.
            attached_module_ids: Returns ``(type, serial)`` for every module currently attached.
            http_server_state: Returns the embedded robot-server state, when one is configured.
            restart_pending: Returns why the connector is about to restart, if it is.
            recovering: Returns whether a RecoveryController.Recover command is running.
            warnings: Returns standing configuration or persistence problems an operator should fix.
            previous_exit: How the previous connector process ended, when that is worth reporting.
            events: Journal that receives state transitions and provides the last error.
        """
        self._motion = motion
        self._run_state = run_state
        self._deck_state = deck_state
        self._registered_modules = tuple(registered_modules)
        self._attached_module_ids = attached_module_ids
        self._http_server_state = http_server_state
        self._restart_pending = restart_pending
        self._recovering = recovering
        self._warnings = warnings
        self._previous_exit = previous_exit
        self._events = events
        self._started_at = datetime.datetime.now(datetime.UTC)
        self._state: RobotState | None = None
        self._since = self._started_at

    def snapshot(self) -> RobotStatusSnapshot:
        """Return the current status without touching the hardware lock or bus."""
        machine = self._motion.machine_status()
        recovery = self._motion.recovery_state
        run = self._read_run()
        deck_valid = self._read_deck()
        modules = tuple(
            ModuleStatusSnapshot(module_type=module_type, serial_number=serial, connected=_safe_bool(connected))
            for module_type, serial, connected in self._registered_modules
        )
        registered = {(module.module_type, module.serial_number) for module in modules}
        unregistered = tuple(
            sorted(f"{module_type} {serial}" for module_type, serial in self._read_attached() - registered)
        )
        http = self._http_server_state() if self._http_server_state is not None else HttpServerState.NOT_CONFIGURED
        busy = self._motion.hardware_busy
        guidance = recovery.operator_guidance if not recovery.operation_ready else ""

        restart_reason = (self._restart_pending() if self._restart_pending is not None else None) or ""
        recovering = _safe_bool(self._recovering) if self._recovering is not None else False
        disconnected = [f"{module.module_type} {module.serial_number}" for module in modules if not module.connected]
        deck_invalid = self._deck_state is not None and not deck_valid
        if restart_reason:
            state, reason = RobotState.INITIALIZING, f"Restarting: {restart_reason}"
        elif machine.is_error_state:
            state, reason = RobotState.EMERGENCY_STOP, machine.message
        elif disconnected or http is HttpServerState.STOPPED:
            causes = [f"module {name} is disconnected; reconnect its USB cable and power" for name in disconnected]
            if http is HttpServerState.STOPPED:
                causes.append("the embedded Opentrons web server stopped; the connector restarts itself")
            state, reason = RobotState.HARDWARE_ERROR, "; ".join(causes)
        elif http is HttpServerState.STARTING or run.status == "initializing":
            state, reason = RobotState.INITIALIZING, "The Opentrons web server or a protocol run is starting"
        elif recovering:
            state, reason = RobotState.RECOVERING, "RecoveryController.Recover is clearing the recovery gates"
        elif not recovery.operation_ready or deck_invalid:
            steps = [guidance] if guidance else []
            if deck_invalid:
                steps.append("reconcile the deck ledger (flexctl deck <robot> reconcile)")
            cause = f"{recovery.reason}. " if recovery.reason else ""
            automatic = "" if deck_invalid else " RecoveryController.Recover performs these steps."
            state, reason = RobotState.RECOVERY_REQUIRED, f"{cause}Required: {', then '.join(steps)}.{automatic}"
        elif not run.direct_control_allowed:
            state, reason = (
                RobotState.PROTOCOL_RUN_ACTIVE,
                f"Protocol run {run.run_id or '(unknown)'} is {run.status}; direct SiLA control is blocked",
            )
        elif busy:
            state, reason = RobotState.BUSY, "A hardware operation is running"
        else:
            state, reason = RobotState.READY, ""
        since = self._observe(state, reason)

        return RobotStatusSnapshot(
            state=state,
            simulating=self._motion.is_simulating,
            estop=machine.estop,
            door_open=machine.door_open,
            hardware_error_message=machine.message,
            hardware_busy=busy,
            rehome_required=recovery.rehome_required,
            gripper_home_required=recovery.gripper_home_required,
            tip_reconciliation_required=tuple(sorted(recovery.tip_reconciliation_required)),
            recovery_guidance=guidance,
            run=run,
            deck_ledger_configured=self._deck_state is not None,
            deck_ledger_valid=deck_valid,
            modules=modules,
            unregistered_modules=unregistered,
            http_server=http,
            restart_pending=restart_reason,
            state_reason=reason,
            state_since=since,
            last_error=describe_error(self._events.last_error),
            warnings=self._read_warnings(recovery.persistence_error),
            previous_exit=self._previous_exit,
            started_at=self._started_at,
        )

    def _observe(self, state: RobotState, reason: str) -> datetime.datetime:
        """Track when the state last changed and journal significant transitions."""
        previous = self._state
        if state is not previous:
            self._state = state
            self._since = datetime.datetime.now(datetime.UTC)
            if previous is not None and not (state in _ROUTINE_STATES and previous in _ROUTINE_STATES):
                detail = f": {reason}" if reason else ""
                self._events.record(EventKind.STATE, "RobotStatus", f"{previous.value} -> {state.value}{detail}")
        return self._since

    def _read_warnings(self, persistence_error: str) -> tuple[str, ...]:
        warnings: list[str] = []
        if persistence_error:
            warnings.append(f"Recovery gates could not be saved and would be lost on restart: {persistence_error}")
        if self._warnings is not None:
            try:
                warnings.extend(str(item) for item in self._warnings())
            except Exception as exc:  # noqa: BLE001 - status reporting must never raise
                warnings.append(f"Could not read configuration warnings: {exc}")
        return tuple(warnings)

    def _read_run(self) -> RunOwnershipSnapshot:
        if self._run_state is None:
            return RunOwnershipSnapshot(run_id="", status="idle", direct_control_allowed=True)
        try:
            run_id, status, owns_hardware = self._run_state()
        except Exception as exc:  # noqa: BLE001 - an unreadable owner is reported fail-closed
            log.debug("Protocol Engine run state unavailable: %s", exc)
            return RunOwnershipSnapshot(run_id="", status="unknown", direct_control_allowed=False)
        return RunOwnershipSnapshot(run_id=run_id or "", status=status, direct_control_allowed=not owns_hardware)

    def _read_deck(self) -> bool:
        if self._deck_state is None:
            return True
        return _safe_bool(self._deck_state)

    def _read_attached(self) -> set[tuple[str, str]]:
        if self._attached_module_ids is None:
            return set()
        try:
            return {(str(module_type), str(serial)) for module_type, serial in self._attached_module_ids()}
        except Exception:  # noqa: BLE001
            return set()


def _safe_bool(read: Callable[[], bool]) -> bool:
    try:
        return bool(read())
    except Exception:  # noqa: BLE001 - status reporting must never raise
        return False


__all__ = [
    "HttpServerState",
    "ModuleStatusSnapshot",
    "RobotState",
    "RobotStatusSnapshot",
    "RobotStatusSource",
    "RunOwnershipSnapshot",
]
