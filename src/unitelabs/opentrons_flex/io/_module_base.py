"""
Shared base for module IO controllers.

Each module controller wraps one of two backends:

- a low-level driver that owns the serial port (via the subclass ``build``), or
- a high-level opentrons module object already attached to a shared
  ``HardwareControlAPI`` (via ``from_module``), whose own poller serialises
  concurrent callers.

The backend-agnostic plumbing — construction, connection state, and device
info — lives here so the concrete controllers only implement the
device-specific commands.
"""

import asyncio
import functools
import inspect
import logging
import typing
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ._errors import (
    ModuleNotRespondingError,
    ModuleOperationError,
    translate_module_errors,
    translate_public_async_methods,
)
from ._types import DeviceInfo

log = logging.getLogger(__name__)


async def complete_before_cancellation[T](operation: typing.Awaitable[T], description: str) -> T:
    """Wait for native work to settle before propagating client cancellation."""
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("%s failed while completing after client cancellation", description)
        raise


_LIVE_CONTROLLERS: "weakref.WeakSet[ModuleControllerBase]" = weakref.WeakSet()


def module_activity(lock: object) -> bool:
    """Whether any module controller sharing ``lock`` has an operation in progress."""
    return any(
        controller._shared_lock is lock and controller.has_active_operation for controller in list(_LIVE_CONTROLLERS)
    )


def _serial(module: object) -> str | None:
    device_info = getattr(module, "device_info", None)
    if isinstance(device_info, typing.Mapping):
        serial = device_info.get("serial")
        return str(serial) if serial else None
    return None


def _serialize_module_method(
    fn: typing.Callable[..., typing.Awaitable[object]],
) -> typing.Callable[..., typing.Awaitable[object]]:
    """Serialize one module call through its shared hardware-operation lock."""

    @functools.wraps(fn)
    async def wrapper(self: "ModuleControllerBase", *args: object, **kwargs: object) -> object:
        observation = fn.__name__.startswith("get_") or fn.__name__ == "is_connected"
        async with self._operation_lock(observation=observation):
            return await fn(self, *args, **kwargs)

    return wrapper


def _serialize_public_async_methods(cls: type) -> None:
    """Apply shared-lock serialization to public async methods declared by ``cls``."""
    exempt_methods = getattr(cls, "_SHARED_LOCK_EXEMPT_METHODS", frozenset())
    for name, attr in list(vars(cls).items()):
        if name not in exempt_methods and not name.startswith("_") and inspect.iscoroutinefunction(attr):
            setattr(cls, name, _serialize_module_method(attr))


class ModuleControllerBase:
    """
    Common backend dispatch shared by all module controllers.

    Exactly one of ``driver`` (a low-level serial driver) or ``module`` (a
    high-level opentrons module object) is set; both are intentionally untyped
    here since each subclass wraps a different concrete type.

    Every public async method on a subclass is wrapped (via ``__init_subclass__``)
    to translate opentrons driver/comm exceptions into the defined module errors,
    so the features see one stable error set regardless of backend.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        translate_public_async_methods(cls)
        _serialize_public_async_methods(cls)

    def __init__(self, driver: object = None, module: object = None, lock: asyncio.Lock | None = None) -> None:
        self._driver = driver
        self._attached_modules: typing.Callable[[], typing.Iterable[object]] | None = None
        self._bound_module = module
        self._shared_lock = lock if lock is not None else asyncio.Lock()
        self._lock_owner: asyncio.Task[object] | None = None
        self._lock_depth = 0
        _LIVE_CONTROLLERS.add(self)

    # ------------------------------------------------------------ live binding

    @property
    def _module(self) -> object:
        """
        The attached Opentrons module object, rebound automatically after a reconnect.

        Opentrons creates a new module object when a USB module is unplugged and
        plugged back in. With a live inventory, the controller follows the module by
        its serial number, so the registered SiLA feature keeps working without a
        connector restart. A module with a different serial is never adopted
        (another unit may hold a different sample, temperature or labware), except
        when the original module reported no serial at all and exactly one unclaimed
        module of that type is attached. The binding only changes while this
        controller holds its operation lock; status reads never rebind. A command
        against a module that is currently absent raises ``ModuleNotRespondingError``.
        """
        if self._bound_module is None or self._attached_modules is None:
            return self._bound_module
        holds_lock = self._lock_owner is not None and self._lock_owner is asyncio.current_task()
        return self._resolve_live_module(required=True, rebind=holds_lock)

    @_module.setter
    def _module(self, module: object) -> None:
        self._bound_module = module

    def _claimed_by_others(self, candidate: object) -> bool:
        return any(other is not self and other._bound_module is candidate for other in list(_LIVE_CONTROLLERS))

    def _find_replacement(self, attached: list[object]) -> tuple[object | None, list[object]]:
        """Return (replacement, same-type candidates) for the bound module, without side effects."""
        bound = self._bound_module
        module_type = getattr(bound, "MODULE_TYPE", None)
        same_type = [
            candidate
            for candidate in attached
            if getattr(candidate, "MODULE_TYPE", None) == module_type and not self._claimed_by_others(candidate)
        ]
        serial = _serial(bound)
        if serial is not None:
            matches = [candidate for candidate in same_type if _serial(candidate) == serial]
            return (matches[0] if matches else None), same_type
        return (same_type[0] if len(same_type) == 1 else None), same_type

    def _resolve_live_module(self, *, required: bool, rebind: bool = True) -> object | None:
        bound = self._bound_module
        attached = list(self._attached_modules()) if self._attached_modules is not None else [bound]
        if any(candidate is bound for candidate in attached):
            return bound
        replacement, same_type = self._find_replacement(attached)
        module_type = getattr(bound, "MODULE_TYPE", None)
        serial = _serial(bound)
        if replacement is not None:
            if not rebind:
                return replacement
            log.info(
                "Rebinding %s controller (serial %s) to the reattached module object",
                getattr(module_type, "name", module_type),
                serial,
            )
            self._bound_module = replacement
            self._on_module_rebound(replacement)
            return replacement
        if not required:
            return None
        name = getattr(module_type, "name", "module")
        if serial is None and len(same_type) > 1:
            serials = ", ".join(str(_serial(candidate)) for candidate in same_type)
            msg = (
                f"Several {name} modules are attached ({serials}) and the original one reported no serial. "
                "Disconnect the extra module so exactly one remains, then retry."
            )
            raise ModuleOperationError(msg)
        if same_type:
            serials = ", ".join(str(_serial(candidate)) for candidate in same_type)
            msg = (
                f"The {name} module with serial {serial} is not attached; a different unit ({serials}) is. "
                "The connector does not switch to another unit automatically. Reconnect the original module, "
                "or restart the connector to register the new one."
            )
            raise ModuleNotRespondingError(msg)
        msg = (
            f"The {name} module (serial {serial}) is not attached. Reconnect its USB cable and power; "
            "the connector resumes using it automatically, without a restart."
        )
        raise ModuleNotRespondingError(msg)

    def _on_module_rebound(self, module: object) -> None:
        """Refresh state derived from the module object after a reconnect."""

    @asynccontextmanager
    async def _operation_lock(self, *, observation: bool = False) -> AsyncIterator[None]:
        """Acquire the shared lock reentrantly for nested module-controller calls."""
        task = asyncio.current_task()
        if task is not None and self._lock_owner is task:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return

        lock_context = (
            self._shared_lock.observation()
            if observation and hasattr(self._shared_lock, "observation")
            else self._shared_lock
        )
        async with lock_context:
            self._lock_owner = task
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                self._lock_owner = None

    @classmethod
    def from_module(
        cls,
        module: object,
        lock: asyncio.Lock | None = None,
        attached_modules: typing.Callable[[], typing.Iterable[object]] | None = None,
    ) -> "ModuleControllerBase":
        """
        Build a controller backed by a module already attached to a shared HardwareControlAPI.

        ``attached_modules`` returns the API's live module list. When supplied,
        ``is_connected`` reports False once Opentrons drops the module after a USB
        disconnect instead of assuming the startup inventory is still present.
        """
        controller = cls(module=module, lock=lock)
        controller._attached_modules = attached_modules
        return controller

    @property
    def has_active_operation(self) -> bool:
        """
        Whether this module is doing anything a connector restart would interrupt.

        Covers calls holding the shared lock, long waits that release it (each
        controller's ``*_control_lock``), and thermocycler profiles.
        """
        if self._lock_owner is not None or getattr(self, "_profile_active", False):
            return True
        return any(
            isinstance(value, asyncio.Lock) and value.locked() and name.endswith("_control_lock")
            for name, value in vars(self).items()
        )

    @property
    def module_connected(self) -> bool:
        """Lock-free, read-only connection check used by status reporting (never rebinds)."""
        if self._bound_module is None or self._attached_modules is None:
            return True
        try:
            return self._resolve_live_module(required=False, rebind=False) is not None
        except Exception:  # noqa: BLE001 - an unreadable inventory is reported as disconnected
            return False

    @translate_module_errors
    @_serialize_module_method
    async def disconnect(self) -> None:
        """Disconnect from the module. No-op when backed by a shared module (the API owns it)."""
        if self._bound_module is None:
            await self._driver.disconnect()

    @translate_module_errors
    @_serialize_module_method
    async def is_connected(self) -> bool:
        """Check connection status."""
        if self._bound_module is not None:
            if self._attached_modules is None:
                return True
            try:
                return self._resolve_live_module(required=False, rebind=True) is not None
            except Exception:  # noqa: BLE001 - an unreadable inventory is reported as disconnected
                return False
        return await self._driver.is_connected()

    @translate_module_errors
    @_serialize_module_method
    async def get_device_info(self) -> DeviceInfo:
        """Get device serial number, model, and firmware version."""
        if self._module is not None:
            return DeviceInfo.from_dict(dict(self._module.device_info))
        return DeviceInfo.from_dict(await self._driver.get_device_info())
