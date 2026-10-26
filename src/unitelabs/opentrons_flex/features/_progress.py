"""Shared observable-command progress structures."""

import asyncio
import contextlib
import enum
import functools
import inspect
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila


class OperationPhase(enum.Enum):
    """Lifecycle phase for an observable robot action."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class OperationProgress:
    """Intermediate progress update for long-running robot actions."""

    phase: OperationPhase
    message: str


def report_progress(
    status: sila.Status,
    intermediate: sila.Intermediate[typing.Any],
    progress: float,
    phase: OperationPhase,
    message: str,
    *,
    progress_type: typing.Callable[..., object] = OperationProgress,
) -> None:
    """Send a status update and matching intermediate response."""
    status.update(progress=progress)
    intermediate.send(progress_type(phase=phase, message=message))


def failure_message(error: BaseException) -> str:
    """Return the operator-facing intermediate message for a failed action."""
    detail = str(error) or type(error).__name__
    return f"Failed ({type(error).__name__}): {detail}"


_IN_FLIGHT = 0


def observable_commands_in_flight() -> int:
    """Return how many observable commands are executing (automatic restarts wait for zero)."""
    return _IN_FLIGHT


@contextlib.contextmanager
def _counted() -> typing.Iterator[None]:
    global _IN_FLIGHT  # noqa: PLW0603 - process-wide activity counter
    _IN_FLIGHT += 1
    try:
        yield
    finally:
        _IN_FLIGHT -= 1


async def run_observable[T](
    status: sila.Status,
    intermediate: sila.Intermediate[typing.Any],
    start_message: str,
    complete_message: str,
    cancelled_message: str,
    action: typing.Awaitable[T],
    *,
    progress_type: typing.Callable[..., object] = OperationProgress,
    running_message: str | None = None,
) -> T:
    """
    Run an awaitable with standard observable-command progress updates.

    Every execution ends with exactly one terminal intermediate response:
    COMPLETED, CANCELLED (client cancellation), or FAILED (any error, including
    an interruption by another client's emergency stop).
    """
    with _counted():
        return await _run_observable(
            status,
            intermediate,
            start_message,
            complete_message,
            cancelled_message,
            action,
            progress_type=progress_type,
            running_message=running_message,
        )


async def _run_observable[T](
    status: sila.Status,
    intermediate: sila.Intermediate[typing.Any],
    start_message: str,
    complete_message: str,
    cancelled_message: str,
    action: typing.Awaitable[T],
    *,
    progress_type: typing.Callable[..., object],
    running_message: str | None,
) -> T:
    report_progress(status, intermediate, 0.0, OperationPhase.STARTING, start_message, progress_type=progress_type)
    if running_message is not None:
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, running_message, progress_type=progress_type)
    try:
        result = await action
    except asyncio.CancelledError:
        report_progress(
            status, intermediate, 1.0, OperationPhase.CANCELLED, cancelled_message, progress_type=progress_type
        )
        raise
    except Exception as error:
        report_progress(
            status, intermediate, 1.0, OperationPhase.FAILED, failure_message(error), progress_type=progress_type
        )
        raise
    report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, complete_message, progress_type=progress_type)
    return result


async def run_observable_with_updates[T](
    status: sila.Status,
    intermediate: sila.Intermediate[OperationProgress],
    start_message: str,
    complete_message: str,
    cancelled_message: str,
    action: typing.Awaitable[T],
    read_update: typing.Callable[[], typing.Awaitable[tuple[float, str]]],
    *,
    interval: float = 0.25,
) -> T:
    """Run an observable action while polling truthful intermediate progress."""
    with _counted():
        return await _run_observable_with_updates(
            status, intermediate, start_message, complete_message, cancelled_message, action, read_update, interval
        )


async def _run_observable_with_updates[T](
    status: sila.Status,
    intermediate: sila.Intermediate[OperationProgress],
    start_message: str,
    complete_message: str,
    cancelled_message: str,
    action: typing.Awaitable[T],
    read_update: typing.Callable[[], typing.Awaitable[tuple[float, str]]],
    interval: float,
) -> T:
    report_progress(status, intermediate, 0.0, OperationPhase.STARTING, start_message)
    task = asyncio.ensure_future(action)
    try:
        while not task.done():
            done, _ = await asyncio.wait((task,), timeout=interval)
            if done:
                break
            progress, message = await read_update()
            report_progress(
                status,
                intermediate,
                max(0.0, min(float(progress), 0.99)),
                OperationPhase.RUNNING,
                message,
            )
        result = task.result()
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, cancelled_message)
        raise
    except Exception as error:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        report_progress(status, intermediate, 1.0, OperationPhase.FAILED, failure_message(error))
        raise
    report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, complete_message)
    return result


def observable_action[T](
    start: str,
    complete: str,
    cancelled: str,
    *,
    running: str | None = None,
    progress_type: typing.Callable[..., object] = OperationProgress,
) -> typing.Callable[[typing.Callable[..., typing.Awaitable[T]]], typing.Callable[..., typing.Awaitable[T]]]:
    """
    Report STARTING / COMPLETED / CANCELLED / FAILED progress around an observable command.

    Apply it directly under ``@sila.ObservableCommand``. The message templates are
    formatted with the command's arguments, so ``"Homing {mount.value}."`` works.
    The decorated method keeps its ``status`` and ``intermediate`` parameters so the
    SiLA definition is unchanged; its body only performs the action.

    Example::

        @sila.ObservableCommand(errors=_MOVE_ERRORS)
        @observable_action(
            "Homing {mount.value}.",
            "{mount.value} homed.",
            "{mount.value} home cancelled.",
        )
        async def home_mount(
            self, mount: Mount, *, status: sila.Status, intermediate: ...
        ) -> None:
            await self._controller.home_mount(_ot3_mount(mount))
    """

    def decorate(
        function: typing.Callable[..., typing.Awaitable[T]],
    ) -> typing.Callable[..., typing.Awaitable[T]]:
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def wrapper(*args: object, **kwargs: object) -> T:
            bound = signature.bind(*args, **kwargs)
            status = typing.cast(sila.Status, bound.arguments["status"])
            intermediate = typing.cast(sila.Intermediate[typing.Any], bound.arguments["intermediate"])
            values = {
                name: value for name, value in bound.arguments.items() if name not in {"self", "status", "intermediate"}
            }
            return await run_observable(
                status,
                intermediate,
                start.format(**values),
                complete.format(**values),
                cancelled.format(**values),
                function(*args, **kwargs),
                progress_type=progress_type,
                running_message=running.format(**values) if running is not None else None,
            )

        return wrapper

    return decorate
