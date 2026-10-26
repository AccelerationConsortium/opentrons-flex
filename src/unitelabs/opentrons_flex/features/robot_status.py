"""SiLA 2 provider that reports the complete Flex connector state in one stream."""

import asyncio
import enum
from dataclasses import dataclass

from unitelabs.cdk import sila

from ..io.robot_status import HttpServerState, RobotState, RobotStatusSnapshot, RobotStatusSource


class OverallState(enum.Enum):
    """Whether the Flex can accept a new command, and if not, why."""

    READY = "READY"
    BUSY = "BUSY"
    PROTOCOL_RUN_ACTIVE = "PROTOCOL_RUN_ACTIVE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    HARDWARE_ERROR = "HARDWARE_ERROR"
    INITIALIZING = "INITIALIZING"


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
    run_status: str
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


_STATE = {state: OverallState[state.name] for state in RobotState}
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
            run_status=snapshot.run.status,
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
    )


class RobotStatusProvider(sila.Feature):
    """
    Reports whether the Flex is ready, busy, owned by a protocol run, or needs recovery.

    Status is read from cached state and never waits for a running hardware
    operation, so it can be observed continuously while the robot moves.
    """

    def __init__(self, source: RobotStatusSource, *, interval: float = 0.25) -> None:
        super().__init__(
            originator="ca.accelerationconsortium",
            category="robots",
            version="1.0",
        )
        self._source = source
        self._interval = interval

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


__all__ = [
    "EmergencyStopState",
    "ModuleConnection",
    "OverallState",
    "ProtocolRunOwnership",
    "RobotStatus",
    "RobotStatusProvider",
    "WebServerState",
    "to_robot_status",
]
