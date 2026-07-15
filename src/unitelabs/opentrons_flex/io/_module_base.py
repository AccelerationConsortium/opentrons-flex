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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ._errors import translate_module_errors, translate_public_async_methods
from ._types import DeviceInfo

log = logging.getLogger(__name__)


def _serialize_module_method(
    fn: typing.Callable[..., typing.Awaitable[object]],
) -> typing.Callable[..., typing.Awaitable[object]]:
    """Serialize one module call through its shared hardware-operation lock."""

    @functools.wraps(fn)
    async def wrapper(self: "ModuleControllerBase", *args: object, **kwargs: object) -> object:
        async with self._operation_lock():
            return await fn(self, *args, **kwargs)

    return wrapper


def _serialize_public_async_methods(cls: type) -> None:
    """Apply shared-lock serialization to public async methods declared by ``cls``."""
    for name, attr in list(vars(cls).items()):
        if not name.startswith("_") and inspect.iscoroutinefunction(attr):
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
        self._module = module
        self._shared_lock = lock if lock is not None else asyncio.Lock()
        self._lock_owner: asyncio.Task[object] | None = None
        self._lock_depth = 0

    @asynccontextmanager
    async def _operation_lock(self) -> AsyncIterator[None]:
        """Acquire the shared lock reentrantly for nested module-controller calls."""
        task = asyncio.current_task()
        if task is not None and self._lock_owner is task:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return

        async with self._shared_lock:
            self._lock_owner = task
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                self._lock_owner = None

    @classmethod
    def from_module(cls, module: object, lock: asyncio.Lock | None = None) -> "ModuleControllerBase":
        """Build a controller backed by a module already attached to a shared HardwareControlAPI."""
        return cls(module=module, lock=lock)

    @translate_module_errors
    @_serialize_module_method
    async def disconnect(self) -> None:
        """Disconnect from the module. No-op when backed by a shared module (the API owns it)."""
        if self._module is None:
            await self._driver.disconnect()

    @translate_module_errors
    @_serialize_module_method
    async def is_connected(self) -> bool:
        """Check connection status."""
        if self._module is not None:
            return True
        return await self._driver.is_connected()

    @translate_module_errors
    @_serialize_module_method
    async def get_device_info(self) -> DeviceInfo:
        """Get device serial number, model, and firmware version."""
        if self._module is not None:
            return DeviceInfo.from_dict(dict(self._module.device_info))
        return DeviceInfo.from_dict(await self._driver.get_device_info())
