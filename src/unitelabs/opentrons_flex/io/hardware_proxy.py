"""
Locking proxy for opentrons HardwareControlAPI.

Wraps a HardwareControlAPI instance with an asyncio.Lock so that concurrent
callers (SiLA2 gRPC server and opentrons HTTP server) cannot interleave hardware
commands on the Flex CAN bus.
"""

import asyncio
import enum
import functools
import inspect
import logging
import typing
from collections.abc import Mapping

from opentrons.hardware_control import HardwareControlAPI
from opentrons.hardware_control.types import Axis, OT3Mount

from ._errors import NotHomedError
from ._module_base import complete_before_cancellation
from .recovery_state import (
    FlexStackerRecoveryState,
    HardwareRecoveryState,
    recovery_state_for,
    stacker_recovery_state_for,
)

if typing.TYPE_CHECKING:
    from .labware_state import LabwareMovementState
    from .simulator_compat import OT3SimulatorCompatibilityAdapter

log = logging.getLogger(__name__)

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

    def __init__(
        self,
        lock: asyncio.Lock,
        timeout_s: float | None = None,
        *,
        protocol_engine: bool = False,
        observation: bool = False,
    ) -> None:
        self._lock = lock
        self._timeout_s = timeout_s
        self._protocol_engine = protocol_engine
        self._observation = observation

    async def __aenter__(self) -> None:
        try:
            if self._protocol_engine and hasattr(self._lock, "acquire_protocol_engine"):
                acquire = self._lock.acquire_protocol_engine
            elif self._observation and hasattr(self._lock, "acquire_observation"):
                acquire = self._lock.acquire_observation
            else:
                acquire = self._lock.acquire
            await asyncio.wait_for(acquire(), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            msg = f"Hardware lock not acquired within {self._timeout_s}s — robot_server may be holding the hardware API"
            raise TimeoutError(msg) from None

    async def __aexit__(self, *args: object) -> None:
        self._lock.release()

    def assert_direct_control_allowed(self) -> None:
        """Apply a RunAwareLock ownership check to synchronous direct calls."""
        authority = getattr(self._lock, "authority", None)
        if authority is not None:
            authority.assert_direct_control_allowed()

    def observation(self) -> "_TimedLock":
        """Return a view that serializes read-only calls without actuation gating."""
        return _TimedLock(self._lock, self._timeout_s, observation=True)


_STACKER_ACTUATION_METHODS = frozenset(
    {
        "close_latch",
        "deactivate",
        "dispense_labware",
        "home_all",
        "home_axis",
        "move_axis",
        "open_latch",
        "store_labware",
    }
)
_STACKER_LABWARE_METHODS = frozenset({"dispense_labware", "store_labware"})


class _PostRunRecoveryMode(enum.Enum):
    """Protocol Engine cleanup sequences recognized by the shared proxy."""

    STAY_ENGAGED_IN_PLACE = enum.auto()
    HOME_AFTER_DISENGAGE = enum.auto()


class _LockedModuleProxy:
    """Serialize one robot-server module view through the connector lock."""

    def __init__(self, module: object, lock: _TimedLock) -> None:
        self._module = module
        self._lock = lock

    @property
    def __class__(self) -> type:
        """Preserve runtime type checks used by Protocol Engine safety code."""
        return self._module.__class__

    async def _invoke_coroutine(
        self,
        _name: str,
        attr: typing.Callable[..., typing.Awaitable[object]],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        return await attr(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        attr = getattr(self._module, name)

        if inspect.isasyncgenfunction(attr):

            @functools.wraps(attr)
            async def locked_gen(*args: object, **kwargs: object) -> typing.AsyncGenerator[object, None]:
                async with self._lock:
                    async for item in attr(*args, **kwargs):
                        yield item

            return locked_gen

        if inspect.iscoroutinefunction(attr):

            @functools.wraps(attr)
            async def locked(*args: object, **kwargs: object) -> object:
                async with self._lock:
                    return await self._invoke_coroutine(name, attr, args, kwargs)

            return locked

        return attr


class _AbsorbanceReaderModuleProxy(_LockedModuleProxy):
    """Keep HTTP Reader ownership until executor-backed module work settles."""

    async def _invoke_coroutine(
        self,
        name: str,
        attr: typing.Callable[..., typing.Awaitable[object]],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        return await complete_before_cancellation(
            attr(*args, **kwargs),
            f"HTTP Absorbance Reader {name}",
        )


async def _finish_stacker_deactivation(module: object) -> None:
    """Shield Stacker motor deactivation from repeated client cancellation."""
    deactivate = module.deactivate
    stop = asyncio.create_task(deactivate())
    while not stop.done():
        try:
            await asyncio.shield(stop)
        except asyncio.CancelledError:
            continue
    await stop


class _FlexStackerModuleProxy(_LockedModuleProxy):
    """Layer Stacker recovery authority onto the generic module lock."""

    def __init__(self, module: object, lock: _TimedLock) -> None:
        super().__init__(module, lock)
        self._recovery_state: FlexStackerRecoveryState = stacker_recovery_state_for(module)

    async def _invoke_coroutine(
        self,
        name: str,
        attr: typing.Callable[..., typing.Awaitable[object]],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        # This check deliberately runs after the generic proxy acquires the
        # shared lock, so a queued request cannot race a failing SiLA action.
        if name in _STACKER_LABWARE_METHODS and self._recovery_state.recovery_required(self._module):
            msg = "The Flex Stacker requires a complete HomeAll recovery before moving labware."
            raise NotHomedError(msg)

        try:
            result = await attr(*args, **kwargs)
        except asyncio.CancelledError:
            if name in _STACKER_ACTUATION_METHODS:
                self._recovery_state.require_full_home()
                try:
                    await _finish_stacker_deactivation(self._module)
                except Exception:
                    log.exception("Flex Stacker failed to deactivate after a cancelled HTTP operation")
            raise
        except Exception:
            if name in _STACKER_ACTUATION_METHODS:
                self._recovery_state.require_full_home()
            raise

        if name in _STACKER_ACTUATION_METHODS and result is False:
            self._recovery_state.require_full_home()
        elif name == "home_all":
            ignore_latch = bool(args[0]) if args else bool(kwargs.get("ignore_latch", False))
            if not ignore_latch:
                self._recovery_state.mark_fully_homed()
        return result


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

    _api: "HardwareControlAPI | OT3SimulatorCompatibilityAdapter"
    _lock: _TimedLock
    _recovery_state: HardwareRecoveryState
    _labware_state: "LabwareMovementState | None"
    _module_proxies: dict[int, tuple[object, object]]
    _post_run_recovery_task: asyncio.Task[object] | None
    _post_run_recovery_mode: _PostRunRecoveryMode | None
    _post_run_stopped: bool
    _post_run_gripper_z_homed: bool

    def __init__(
        self,
        api: "HardwareControlAPI | OT3SimulatorCompatibilityAdapter",
        lock: asyncio.Lock | None = None,
        lock_timeout_s: float | None = None,
        labware_state: "LabwareMovementState | None" = None,
    ) -> None:
        object.__setattr__(self, "_api", api)
        raw_lock = lock if lock is not None else asyncio.Lock()
        object.__setattr__(self, "_lock", _TimedLock(raw_lock, lock_timeout_s, protocol_engine=True))
        object.__setattr__(self, "_recovery_state", recovery_state_for(api))
        object.__setattr__(self, "_labware_state", labware_state)
        object.__setattr__(self, "_module_proxies", {})
        object.__setattr__(self, "_post_run_recovery_task", None)
        object.__setattr__(self, "_post_run_recovery_mode", None)
        object.__setattr__(self, "_post_run_stopped", False)
        object.__setattr__(self, "_post_run_gripper_z_homed", False)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._api, name, value)

    def __getattr__(self, name: str) -> object:
        attr = getattr(self._api, name)

        if name == "attached_modules":
            live_proxies: dict[int, tuple[object, object]] = {}
            result: list[object] = []
            for module in attr:
                key = id(module)
                cached = self._module_proxies.get(key)
                if cached is not None and cached[0] is module:
                    module_proxy = cached[1]
                elif _is_flex_stacker_module(module):
                    module_proxy = _FlexStackerModuleProxy(module, self._lock)
                elif _is_absorbance_reader_module(module):
                    module_proxy = _AbsorbanceReaderModuleProxy(module, self._lock)
                else:
                    module_proxy = _LockedModuleProxy(module, self._lock)
                live_proxies[key] = (module, module_proxy)
                result.append(module_proxy)
            object.__setattr__(self, "_module_proxies", live_proxies)
            return result

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
                    # Protocol Engine always passes this argument explicitly.
                    # Connector emergency halts call halt() with no argument and
                    # must remain fail-closed instead of gaining cleanup authority.
                    protocol_engine_cleanup = bool(args) or "disengage_before_stopping" in kwargs
                    if protocol_engine_cleanup:
                        disengage = bool(_argument(args, kwargs, 0, "disengage_before_stopping"))
                        mode = (
                            _PostRunRecoveryMode.HOME_AFTER_DISENGAGE
                            if disengage
                            else _PostRunRecoveryMode.STAY_ENGAGED_IN_PLACE
                        )
                        object.__setattr__(self, "_post_run_recovery_task", asyncio.current_task())
                        object.__setattr__(self, "_post_run_recovery_mode", mode)
                        object.__setattr__(self, "_post_run_stopped", False)
                        object.__setattr__(self, "_post_run_gripper_z_homed", False)
                    else:
                        self._clear_post_run_recovery()
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
                        self._clear_post_run_recovery()
                        raise asyncio.CancelledError
                    return result

                return preemptive_halt

            @functools.wraps(attr)
            async def locked(*args: object, **kwargs: object) -> object:
                async with self._lock:
                    is_post_run_recovery_call = self._is_post_run_recovery_call(name, args, kwargs)
                    if (
                        self._labware_state is not None
                        and _is_direct_gripper_actuation(name, args, kwargs)
                        and not is_post_run_recovery_call
                    ):
                        self._labware_state.assert_direct_gripper_control_allowed(f"HTTP {name}")
                    if not (
                        _allowed_during_recovery(name, args, kwargs, self._recovery_state) or is_post_run_recovery_call
                    ):
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
                        if is_post_run_recovery_call:
                            self._advance_post_run_recovery(name, args, kwargs, generation)
                        return result
                    except asyncio.CancelledError:
                        if is_post_run_recovery_call:
                            self._clear_post_run_recovery()
                        if task is not None:
                            self._recovery_state.unregister_operation(task)
                        if name in {"grip", "ungrip", "home_gripper_jaw"}:
                            self._recovery_state.require_gripper_home()
                        if is_actuation:
                            await _recover_cancelled_proxy_action(self._api, self._recovery_state)
                        raise
                    except Exception:
                        if is_post_run_recovery_call:
                            self._clear_post_run_recovery()
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

    def _is_post_run_recovery_call(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> bool:
        """Authorize only Protocol Engine's ordered post-run recovery calls."""
        if self._post_run_recovery_task is not asyncio.current_task():
            return False
        if name == "stop":
            home_after = _argument(args, kwargs, 0, "home_after")
            return not self._post_run_stopped and home_after is False
        if name == "home_z":
            mount = _argument(args, kwargs, 0, "mount")
            return self._post_run_stopped and not self._post_run_gripper_z_homed and _is_gripper_mount(mount)
        if name == "home":
            axes = _argument(args, kwargs, 0, "axes")
            return (
                self._post_run_stopped
                and (self._post_run_gripper_z_homed or not self._api.has_gripper())
                and _homes_post_run_gantry_axes(axes)
            )
        return False

    def _advance_post_run_recovery(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
        generation: int,
    ) -> None:
        """Advance a successful Protocol Engine halt recovery sequence."""
        if name == "stop":
            object.__setattr__(self, "_post_run_stopped", True)
            if self._post_run_recovery_mode is _PostRunRecoveryMode.STAY_ENGAGED_IN_PLACE:
                self._recovery_state.mark_motion_preserved_after_stop(generation)
                self._clear_post_run_recovery()
        elif name == "home_z":
            object.__setattr__(self, "_post_run_gripper_z_homed", True)
        elif name == "home" and _homes_post_run_gantry_axes(_argument(args, kwargs, 0, "axes")):
            self._recovery_state.mark_fully_homed(generation)
            self._clear_post_run_recovery()

    def _clear_post_run_recovery(self) -> None:
        object.__setattr__(self, "_post_run_recovery_task", None)
        object.__setattr__(self, "_post_run_recovery_mode", None)
        object.__setattr__(self, "_post_run_stopped", False)
        object.__setattr__(self, "_post_run_gripper_z_homed", False)

    def wrapped(self) -> "HardwareProxy":
        """Return self — satisfies robot-server's ThreadManager.wrapped() call."""
        return self

    def wraps_instance(self, cls: type) -> bool:
        """
        Return True if the underlying API is an instance of cls.

        Satisfies ThreadManager.wraps_instance() used by robot-server hardware
        routes to distinguish OT-2 (API) from OT-3 (OT3API).
        """
        wrapped_api = getattr(self._api, "wrapped_api", self._api)
        return isinstance(wrapped_api, cls)

    def clean_up(self) -> None:
        """
        Leave shared hardware cleanup to the connector lifecycle owner.

        Robot-server treats its hardware singleton as a synchronous
        ``ThreadManagedHardware`` and calls ``clean_up()`` without awaiting it.
        The connector owns the underlying async ``OT3API`` and awaits its cleanup
        after both servers stop, so this compatibility method is intentionally a
        no-op.
        """


def _is_flex_stacker_module(module: object) -> bool:
    module_type = getattr(module, "MODULE_TYPE", None)
    return getattr(module_type, "name", "") == "FLEX_STACKER"


def _is_absorbance_reader_module(module: object) -> bool:
    module_type = getattr(module, "MODULE_TYPE", None)
    return getattr(module_type, "name", "") == "ABSORBANCE_READER"


def _is_full_home(args: tuple[object, ...], kwargs: dict[str, object]) -> bool:
    """Whether a HardwareControlAPI.home call requests every robot axis."""
    if args:
        return args[0] is None
    return kwargs.get("axes") is None


def _is_direct_gripper_actuation(name: str, args: tuple[object, ...], kwargs: dict[str, object]) -> bool:
    """Whether a robot-server call would bypass the durable labware ledger."""
    if name in {"grip", "ungrip", "idle_gripper"}:
        return True
    if name in {"move_to", "move_rel", "prepare_for_mount_movement", "retract"}:
        return _is_gripper_mount(_argument(args, kwargs, 0, "mount"))
    if name == "home_z":
        mount = _argument(args, kwargs, 0, "mount")
        return mount is None or _is_gripper_mount(mount)
    if name == "move_axes":
        return _contains_gripper_axis(_argument(args, kwargs, 0, "position"))
    if name == "retract_axis":
        return _contains_gripper_axis(_argument(args, kwargs, 0, "axis"))
    if name == "disengage_axes":
        return _contains_gripper_axis(_argument(args, kwargs, 0, "which"))
    if name == "home":
        axes = _argument(args, kwargs, 0, "axes")
        # Full homing remains an explicit recovery path, but a targeted raw
        # gripper-axis home must not bypass the managed labware surface.
        return axes is not None and _contains_gripper_axis(axes)
    return False


def _argument(
    args: tuple[object, ...],
    kwargs: dict[str, object],
    index: int,
    name: str,
) -> object | None:
    return args[index] if len(args) > index else kwargs.get(name)


def _is_gripper_mount(value: object) -> bool:
    try:
        return OT3Mount.from_mount(value) is OT3Mount.GRIPPER  # type: ignore[arg-type]
    except (AttributeError, KeyError, TypeError, ValueError):
        mount_name = str(getattr(value, "name", value)).upper()
        return mount_name in {"EXTENSION", "GRIPPER"}


def _contains_gripper_axis(value: object) -> bool:
    if isinstance(value, Mapping):
        axes = value.keys()
    elif isinstance(value, (list, tuple, set, frozenset)):
        axes = value
    else:
        axes = (value,)
    for axis in axes:
        if axis is Axis.Z_G or axis is Axis.G:
            return True
        axis_name = str(getattr(axis, "name", axis)).upper()
        if axis_name in {"Z_G", "G", "EXTENSION_Z", "GRIPPER_JAW"}:
            return True
    return False


def _homes_post_run_gantry_axes(value: object) -> bool:
    """Whether Protocol Engine requested its complete post-run gantry home."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return False
    requested = set(value)
    return {Axis.X, Axis.Y, Axis.Z_L, Axis.Z_R}.issubset(requested)


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


async def _recover_cancelled_proxy_action(
    api: "HardwareControlAPI | OT3SimulatorCompatibilityAdapter",
    state: HardwareRecoveryState,
) -> None:
    """Halt an HTTP-side action before propagating its cancellation."""
    state.mark_halted()
    halt = asyncio.create_task(api.halt())
    while not halt.done():
        try:
            await asyncio.shield(halt)
        except asyncio.CancelledError:
            continue
    await halt
