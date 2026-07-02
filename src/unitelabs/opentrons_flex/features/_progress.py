"""Shared observable-command progress structures."""

import enum
import asyncio
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila

_T = typing.TypeVar("_T")


class OperationPhase(enum.Enum):
    """Lifecycle phase for an observable robot action."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass
class OperationProgress:
    """Intermediate progress update for long-running robot actions."""

    phase: OperationPhase
    message: str


def report_progress(
    status: sila.Status,
    intermediate: sila.Intermediate[OperationProgress],
    progress: float,
    phase: OperationPhase,
    message: str,
) -> None:
    """Send a status update and matching intermediate response."""
    status.update(progress=progress)
    intermediate.send(OperationProgress(phase=phase, message=message))


async def run_observable(
    status: sila.Status,
    intermediate: sila.Intermediate[OperationProgress],
    start_message: str,
    complete_message: str,
    cancelled_message: str,
    action: typing.Awaitable[_T],
) -> _T:
    """Run an awaitable with standard observable-command progress updates."""
    report_progress(status, intermediate, 0.0, OperationPhase.STARTING, start_message)
    try:
        result = await action
    except asyncio.CancelledError:
        report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, cancelled_message)
        raise
    report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, complete_message)
    return result
