"""
Lock-free aggregation of every piece of connector state an operator needs at a glance.

Each value is read from cached hardware state or in-memory gates, so a status
snapshot never waits for, or interferes with, a running hardware operation.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .flex_motion import FlexMotionController

log = logging.getLogger(__name__)


class RobotState(enum.Enum):
    """Single summary of whether the Flex can accept a new command."""

    READY = "READY"
    BUSY = "BUSY"
    PROTOCOL_RUN_ACTIVE = "PROTOCOL_RUN_ACTIVE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    HARDWARE_ERROR = "HARDWARE_ERROR"
    INITIALIZING = "INITIALIZING"


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
        """
        self._motion = motion
        self._run_state = run_state
        self._deck_state = deck_state
        self._registered_modules = tuple(registered_modules)
        self._attached_module_ids = attached_module_ids
        self._http_server_state = http_server_state
        self._restart_pending = restart_pending

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
        if restart_reason:
            state = RobotState.INITIALIZING
        elif (
            machine.is_error_state or any(not module.connected for module in modules) or http is HttpServerState.STOPPED
        ):
            state = RobotState.HARDWARE_ERROR
        elif http is HttpServerState.STARTING or run.status == "initializing":
            state = RobotState.INITIALIZING
        elif not recovery.operation_ready or (self._deck_state is not None and not deck_valid):
            state = RobotState.RECOVERY_REQUIRED
        elif not run.direct_control_allowed:
            state = RobotState.PROTOCOL_RUN_ACTIVE
        elif busy:
            state = RobotState.BUSY
        else:
            state = RobotState.READY

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
        )

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
