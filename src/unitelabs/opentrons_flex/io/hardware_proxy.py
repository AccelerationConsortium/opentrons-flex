"""
Locking proxy for opentrons HardwareControlAPI.

Wraps a HardwareControlAPI instance with an asyncio.Lock so that concurrent
callers (SiLA2 gRPC server and opentrons HTTP server) cannot interleave hardware
commands on the Flex CAN bus.
"""

import asyncio
import functools
import inspect
import typing

from opentrons.hardware_control import HardwareControlAPI

from ._errors import NotHomedError
from .recovery_state import HardwareRecoveryState, recovery_state_for

_RECOVERY_SAFE_CALLS = frozenset(
    {
        "add_tip",
        "cache_instruments",
        "clean_up",
        "current_position",
        "gantry_position",
        "get_tip_presence_status",
        "pause",
        "remove_tip",
    }
)


class _TimedLock:
    """asyncio.Lock with an optional acquire timeout and a descriptive error on expiry."""

    def __init__(self, lock: asyncio.Lock, timeout_s: float | None = None) -> None:
        self._lock = lock
        self._timeout_s = timeout_s

    async def __aenter__(self) -> None:
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            msg = f"Hardware lock not acquired within {self._timeout_s}s — robot_server may be holding the hardware API"
            raise TimeoutError(msg) from None

    async def __aexit__(self, *args: object) -> None:
        self._lock.release()


class HardwareProxy:
    """
    Serialises concurrent async callers against a shared HardwareControlAPI.

    Uses __getattr__ to delegate every attribute access to the wrapped API.
    Async methods are transparently wrapped with an asyncio.Lock so that only
    one hardware call is in-flight at a time. Sync attributes are passed through;
    sync methods are guarded by the shared post-halt recovery gate.

    pause() and resume() are sync methods that schedule internal coroutines via
    run_coroutine_threadsafe; they bypass the lock but do not send bus commands
    directly.
    """

    _api: HardwareControlAPI
    _lock: _TimedLock
    _recovery_state: HardwareRecoveryState

    def __init__(
        self,
        api: HardwareControlAPI,
        lock: asyncio.Lock | None = None,
        lock_timeout_s: float | None = None,
    ) -> None:
        object.__setattr__(self, "_api", api)
        raw_lock = lock if lock is not None else asyncio.Lock()
        object.__setattr__(self, "_lock", _TimedLock(raw_lock, lock_timeout_s))
        object.__setattr__(self, "_recovery_state", recovery_state_for(api))

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._api, name, value)

    def __getattr__(self, name: str) -> object:
        attr = getattr(self._api, name)

        if inspect.isasyncgenfunction(attr):

            @functools.wraps(attr)
            async def locked_gen(*args: object, **kwargs: object) -> typing.AsyncGenerator[object, None]:
                async with self._lock:
                    async for item in attr(*args, **kwargs):
                        yield item

            return locked_gen

        if inspect.iscoroutinefunction(attr):
            if name == "halt":

                @functools.wraps(attr)
                async def preemptive_halt(*args: object, **kwargs: object) -> object:
                    self._recovery_state.mark_halted()
                    # A halt must not wait behind the operation it is stopping.
                    halt = asyncio.create_task(attr(*args, **kwargs))
                    cancelled = False
                    while not halt.done():
                        try:
                            await asyncio.shield(halt)
                        except asyncio.CancelledError:
                            cancelled = True
                    result = await halt
                    if cancelled:
                        raise asyncio.CancelledError
                    return result

                return preemptive_halt

            @functools.wraps(attr)
            async def locked(*args: object, **kwargs: object) -> object:
                async with self._lock:
                    if not _allowed_during_recovery(name, args, kwargs, self._recovery_state):
                        raise _recovery_error(self._recovery_state)
                    generation = self._recovery_state.generation
                    is_actuation = not _is_status_call(name)
                    task = self._recovery_state.register_current_operation() if is_actuation else None
                    try:
                        result = await attr(*args, **kwargs)
                        if generation != self._recovery_state.generation and is_actuation:
                            raise asyncio.CancelledError
                        if name == "home" and _is_full_home(args, kwargs):
                            self._recovery_state.mark_fully_homed(generation)
                        elif name == "home_gripper_jaw":
                            self._recovery_state.mark_gripper_homed(generation)
                        return result
                    except asyncio.CancelledError:
                        if task is not None:
                            self._recovery_state.unregister_operation(task)
                        if name in {"grip", "ungrip", "home_gripper_jaw"}:
                            self._recovery_state.require_gripper_home()
                        if is_actuation:
                            await _recover_cancelled_proxy_action(self._api, self._recovery_state)
                        raise
                    finally:
                        if task is not None:
                            self._recovery_state.unregister_operation(task)

            return locked

        if callable(attr):

            @functools.wraps(attr)
            def guarded_sync(*args: object, **kwargs: object) -> object:
                if not _allowed_during_recovery(name, args, kwargs, self._recovery_state):
                    raise _recovery_error(self._recovery_state)
                return attr(*args, **kwargs)

            return guarded_sync

        return attr

    def wrapped(self) -> "HardwareProxy":
        """Return self — satisfies robot-server's ThreadManager.wrapped() call."""
        return self

    def wraps_instance(self, cls: type) -> bool:
        """
        Return True if the underlying API is an instance of cls.

        Satisfies ThreadManager.wraps_instance() used by robot-server hardware
        routes to distinguish OT-2 (API) from OT-3 (OT3API).
        """
        return isinstance(self._api, cls)


def _is_full_home(args: tuple[object, ...], kwargs: dict[str, object]) -> bool:
    """Whether a HardwareControlAPI.home call requests every robot axis."""
    if args:
        return args[0] is None
    return kwargs.get("axes") is None


def _is_status_call(name: str) -> bool:
    return name in _RECOVERY_SAFE_CALLS or name.startswith(("get_", "has_", "is_", "read_"))


def _allowed_during_recovery(
    name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    state: HardwareRecoveryState,
) -> bool:
    if state.operation_ready or _is_status_call(name):
        return True
    if name == "home" and _is_full_home(args, kwargs):
        return True
    return name == "home_gripper_jaw" and state.gantry_recovered


def _recovery_error(state: HardwareRecoveryState) -> NotHomedError:
    msg = f"The robot is recovery-gated. {state.operator_guidance} before another hardware actuation."
    return NotHomedError(msg)


async def _recover_cancelled_proxy_action(api: HardwareControlAPI, state: HardwareRecoveryState) -> None:
    """Halt an HTTP-side action before propagating its cancellation."""
    state.mark_halted()
    halt = asyncio.create_task(api.halt())
    while not halt.done():
        try:
            await asyncio.shield(halt)
        except asyncio.CancelledError:
            continue
    await halt
