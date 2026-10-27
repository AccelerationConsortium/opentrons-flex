"""SiLA 2 provider that reports the complete Flex connector state in one stream."""

import asyncio
import datetime
import enum
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io.events import EVENTS, ConnectorEvent, EventKind, EventLog
from ..io.robot_status import HttpServerState, RobotState, RobotStatusSnapshot, RobotStatusSource

# Protocol Engine run statuses (robot-server 9.0) plus the connector's own values.
PROTOCOL_RUN_STATUSES = (
    "idle",
    "running",
    "paused",
    "stop-requested",
    "stopped",
    "finishing",
    "failed",
    "succeeded",
    "blocked-by-open-door",
    "awaiting-recovery",
    "awaiting-recovery-blocked-by-open-door",
    "awaiting-recovery-paused",
    "initializing",
    "unknown",
)
ProtocolRunStatus = typing.Annotated[str, constraints.Set(list(PROTOCOL_RUN_STATUSES))]


class OverallState(enum.Enum):
    """Whether the Flex can accept a new command, and if not, why."""

    READY = "READY"
    BUSY = "BUSY"
    PROTOCOL_RUN_ACTIVE = "PROTOCOL_RUN_ACTIVE"
    RECOVERING = "RECOVERING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    HARDWARE_ERROR = "HARDWARE_ERROR"
    INITIALIZING = "INITIALIZING"


class EventCategory(enum.Enum):
    """Category of a connector event."""

    STATE = "STATE"
    ERROR = "ERROR"
    HALT = "HALT"
    RECOVERY = "RECOVERY"
    MODULE = "MODULE"
    LIFECYCLE = "LIFECYCLE"


@dataclass
class ConnectorEventEntry:
    """
    One entry of the connector event journal.

    Attributes:
        Sequence: Increasing event number since the connector started.
        Time: When the event happened (UTC).
        Category: What kind of event it is.
        Source: The command, component, or controller that reported it.
        Message: What happened, including the error type for errors.
    """

    sequence: int
    time: datetime.datetime
    category: EventCategory
    source: str
    message: str


class EmergencyStopState(enum.Enum):
    """State of the Flex emergency-stop pendant."""

    DISENGAGED = "DISENGAGED"
    PHYSICALLY_ENGAGED = "PHYSICALLY_ENGAGED"
    LOGICALLY_ENGAGED = "LOGICALLY_ENGAGED"
    NOT_PRESENT = "NOT_PRESENT"


class WebServerState(enum.Enum):
    """State of the embedded Opentrons HTTP robot-server."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


@dataclass
class ModuleConnection:
    """
    Connection state of one module registered at connector startup.

    Attributes:
        ModuleType: Opentrons module type.
        SerialNumber: Module serial number.
        Connected: True while the robot still reports the module as attached.
    """

    module_type: str
    serial_number: str
    connected: bool


@dataclass
class ProtocolRunOwnership:
    """
    Protocol Engine run that currently owns the Flex, if any.

    Attributes:
        RunIdentifier: Run identifier, or empty when no run exists.
        RunStatus: Protocol Engine run status (for example idle, running, paused, succeeded).
        DirectControlAllowed: Whether independent SiLA hardware commands are currently accepted.
    """

    run_identifier: str
    run_status: ProtocolRunStatus
    direct_control_allowed: bool


@dataclass
class RobotStatus:
    """
    Complete connector state for operators and orchestration software.

    Attributes:
        State: Summary of whether the Flex can accept a new command.
        Simulating: True when the connector drives the Opentrons simulator.
        EmergencyStop: Emergency-stop pendant state.
        DoorOpen: True when the front door is open.
        HardwareErrorMessage: Operator guidance for an active hardware error state, empty when healthy.
        HardwareBusy: True while an operation holds the shared hardware.
        RehomeRequired: True until a full robot home completes after a halt or restart.
        GripperHomeRequired: True until GripperController.HomeJaw completes after an interrupted grip.
        TipReconciliationRequired: Pipette mounts whose tip state must be reconciled.
        RecoveryGuidance: Ordered recovery actions, empty when no recovery is required.
        ProtocolRun: Protocol Engine ownership of the Flex.
        DeckLedgerConfigured: True when allowlisted labware movement tracks deck occupancy.
        DeckLedgerValid: False after an interrupted move until an operator reconciles the deck.
        Modules: Modules registered at connector startup and whether they are still attached.
        UnregisteredModules: Attached modules that appeared after startup; restart the connector to use them.
        WebServer: State of the embedded Opentrons HTTP robot-server.
        RestartPending: Why the connector is restarting itself (for example to register a new module), else empty.
        StateReason: Why the connector is in its current state and what resolves it; empty when ready.
        StateSince: When the current state was entered (UTC).
        LastError: The most recent failed command, halt, or hardware error with its time; empty if none.
        Warnings: Standing configuration or persistence problems an operator should fix.
        PreviousExit: How the previous connector process ended, when it did not shut down cleanly.
        StartedAt: When this connector process started (UTC).
    """

    state: OverallState
    simulating: bool
    emergency_stop: EmergencyStopState
    door_open: bool
    hardware_error_message: str
    hardware_busy: bool
    rehome_required: bool
    gripper_home_required: bool
    tip_reconciliation_required: list[str]
    recovery_guidance: str
    protocol_run: ProtocolRunOwnership
    deck_ledger_configured: bool
    deck_ledger_valid: bool
    modules: list[ModuleConnection]
    unregistered_modules: list[str]
    web_server: WebServerState
    restart_pending: str
    state_reason: str
    state_since: datetime.datetime
    last_error: str
    warnings: list[str]
    previous_exit: str
    started_at: datetime.datetime


_STATE = {state: OverallState[state.name] for state in RobotState}
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
_CATEGORIES = {kind: EventCategory[kind.name] for kind in EventKind}
_WEB = {state: WebServerState[state.name] for state in HttpServerState}


def to_robot_status(snapshot: RobotStatusSnapshot) -> RobotStatus:
    """Convert the IO snapshot into the SiLA structure."""
    try:
        estop = EmergencyStopState[snapshot.estop]
    except KeyError:
        estop = EmergencyStopState.NOT_PRESENT
    return RobotStatus(
        state=_STATE[snapshot.state],
        simulating=snapshot.simulating,
        emergency_stop=estop,
        door_open=snapshot.door_open,
        hardware_error_message=snapshot.hardware_error_message,
        hardware_busy=snapshot.hardware_busy,
        rehome_required=snapshot.rehome_required,
        gripper_home_required=snapshot.gripper_home_required,
        tip_reconciliation_required=list(snapshot.tip_reconciliation_required),
        recovery_guidance=snapshot.recovery_guidance,
        protocol_run=ProtocolRunOwnership(
            run_identifier=snapshot.run.run_id,
            run_status=snapshot.run.status if snapshot.run.status in PROTOCOL_RUN_STATUSES else "unknown",
            direct_control_allowed=snapshot.run.direct_control_allowed,
        ),
        deck_ledger_configured=snapshot.deck_ledger_configured,
        deck_ledger_valid=snapshot.deck_ledger_valid,
        modules=[
            ModuleConnection(
                module_type=module.module_type,
                serial_number=module.serial_number,
                connected=module.connected,
            )
            for module in snapshot.modules
        ],
        unregistered_modules=list(snapshot.unregistered_modules),
        web_server=_WEB[snapshot.http_server],
        restart_pending=snapshot.restart_pending,
        state_reason=snapshot.state_reason,
        state_since=snapshot.state_since or _EPOCH,
        last_error=snapshot.last_error,
        warnings=list(snapshot.warnings),
        previous_exit=snapshot.previous_exit,
        started_at=snapshot.started_at or _EPOCH,
    )


def to_event_entry(event: ConnectorEvent) -> ConnectorEventEntry:
    """Convert a journal event into the SiLA structure."""
    return ConnectorEventEntry(
        sequence=event.sequence,
        time=event.time,
        category=_CATEGORIES[event.kind],
        source=event.source,
        message=event.message,
    )


class RobotStatusProvider(sila.Feature):
    """
    Reports whether the Flex is ready, busy, owned by a protocol run, or needs recovery.

    Status is read from cached state and never waits for a running hardware
    operation, so it can be observed continuously while the robot moves.
    """

    def __init__(self, source: RobotStatusSource, *, interval: float = 0.25, events: EventLog = EVENTS) -> None:
        super().__init__(
            originator="ca.accelerationconsortium",
            category="robots",
            version="1.1",
        )
        self._source = source
        self._interval = interval
        self._events = events

    @sila.ObservableProperty()
    async def subscribe_status(self) -> sila.Stream[RobotStatus]:
        """Subscribe to the complete connector status; a new value is sent whenever anything changes."""
        previous: object = object()
        while True:
            current = to_robot_status(self._source.snapshot())
            if current != previous:
                yield current
                previous = current
            await asyncio.sleep(self._interval)

    @sila.ObservableProperty()
    async def subscribe_recent_events(self) -> sila.Stream[list[ConnectorEventEntry]]:
        """
        Subscribe to the most recent connector events, oldest first.

        Includes significant state changes, failed commands, halts, recovery steps,
        and restarts, so a client can see why the connector changed state.
        """
        previous = -1
        while True:
            sequence = self._events.sequence
            if sequence != previous:
                yield [to_event_entry(event) for event in self._events.recent()]
                previous = sequence
            await asyncio.sleep(self._interval)


__all__ = [
    "PROTOCOL_RUN_STATUSES",
    "ConnectorEventEntry",
    "EmergencyStopState",
    "EventCategory",
    "ModuleConnection",
    "OverallState",
    "ProtocolRunOwnership",
    "RobotStatus",
    "RobotStatusProvider",
    "WebServerState",
    "to_robot_status",
]
