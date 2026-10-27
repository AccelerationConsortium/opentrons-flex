"""SiLA 2 controller that returns the Flex to a known, ready state after a halt or fault."""

import asyncio
from dataclasses import dataclass

from unitelabs.cdk import sila

from ..io import (
    SYSTEM_ERRORS,
    GripActionError,
    MachineErrorStateError,
    ModuleNotRespondingError,
    ModuleOperationError,
    PipetteNotAttachedError,
    RunOwnershipError,
    StackerNotReadyError,
    StallDetectedError,
)
from ..io.recovery import (
    DoorOpenError,
    EmergencyStopEngagedError,
    ManualStepsRequiredError,
    RecoveryCoordinator,
)
from ._progress import OperationPhase, OperationProgress, run_observable
from .robot_status import EmergencyStopState

_RECOVER_ERRORS = [
    EmergencyStopEngagedError,
    DoorOpenError,
    ManualStepsRequiredError,
    RunOwnershipError,
    *SYSTEM_ERRORS,
    StallDetectedError,
    MachineErrorStateError,
    GripActionError,
    PipetteNotAttachedError,
    StackerNotReadyError,
    ModuleNotRespondingError,
    ModuleOperationError,
]


@dataclass
class PendingRecovery:
    """
    What a recovery would do now.

    Attributes:
        AutomaticSteps: Steps Recover performs by itself, in order (empty when nothing is required).
        ManualSteps: Steps that need a person before or after Recover.
    """

    automatic_steps: list[str]
    manual_steps: list[str]


class RecoveryController(sila.Feature):
    """
    Returns the Flex to a known, ready state after an emergency stop, halt, or fault.

    Recover performs, in order: acknowledge a released E-stop, fully home the
    robot, reconcile quarantined tip states from the tip sensors, home the gripper
    jaw, and fully home any Flex Stacker that lost its position. Steps that need a
    person (a tip of unknown length, an invalid deck ledger, a disconnected
    module) are reported instead of guessed.
    """

    def __init__(self, coordinator: RecoveryCoordinator, *, interval: float = 0.5) -> None:
        super().__init__(originator="ca.accelerationconsortium", category="robots", version="1.0")
        self._coordinator = coordinator
        self._interval = interval

    @sila.ObservableCommand(errors=_RECOVER_ERRORS)
    async def recover(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> list[str]:
        """
        Clear every recovery gate that does not need a person.

        The front door must be closed and the deck must be safe for the gantry to
        home. Cancelling Recover halts the robot, so a new Recover is required.

        Yields:
            Update: The recovery step being performed.

        Returns:
            PerformedSteps: The steps that were performed, in order (empty when nothing was required).
        """

        def report(fraction: float, message: str) -> None:
            status.update(progress=max(0.0, min(fraction, 1.0)))
            intermediate.send(OperationProgress(phase=OperationPhase.RUNNING, message=message))

        return await run_observable(
            status,
            intermediate,
            "Starting recovery.",
            "Recovery finished; the robot is ready.",
            "Recovery cancelled; the robot was halted.",
            self._coordinator.recover(report),
        )

    @sila.UnobservableCommand(errors=[EmergencyStopEngagedError])
    async def acknowledge_emergency_stop(self) -> EmergencyStopState:
        """
        Acknowledge a released E-stop without moving the robot.

        A Flex keeps a released E-stop latched until it is acknowledged. After
        acknowledging, the robot still needs a full home (Recover does both).

        Returns:
            EmergencyStop: The E-stop state after acknowledging.
        """
        state = self._coordinator.acknowledge_emergency_stop()
        try:
            return EmergencyStopState[state]
        except KeyError:
            return EmergencyStopState.NOT_PRESENT

    @sila.ObservableProperty()
    async def subscribe_pending_recovery(self) -> sila.Stream[PendingRecovery]:
        """Subscribe to the steps Recover would perform now and the steps that need a person."""
        previous: PendingRecovery | None = None
        while True:
            current = PendingRecovery(
                automatic_steps=[step.describe() for step in self._coordinator.plan()],
                manual_steps=self._coordinator.manual_actions(),
            )
            if current != previous:
                yield current
                previous = current
            await asyncio.sleep(self._interval)


__all__ = ["PendingRecovery", "RecoveryController"]
