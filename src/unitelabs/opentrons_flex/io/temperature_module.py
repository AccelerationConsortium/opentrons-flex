"""Temperature Module GEN2 IO wrapper."""

import asyncio
import logging
import math

from opentrons.drivers.temp_deck.driver import TempDeckDriver

from ._module_base import ModuleControllerBase
from ._errors import InvalidTemperatureTargetError, ModuleOperationError
from ._types import DeviceInfo, Temperature, TemperatureModuleState

log = logging.getLogger(__name__)


class TemperatureModuleController(ModuleControllerBase):
    """
    Control a Temperature Module GEN2 through the shared hardware API.

    Two backends are supported (see ``ModuleControllerBase``):

    - ``build(port=...)`` wraps a low-level ``TempDeckDriver`` that owns the serial
      port directly (standalone connector mode).
    - ``from_module(module)`` wraps the high-level ``TempDeck`` object already
      attached to a shared ``HardwareControlAPI`` (in-process robot-server mode).
    """

    # The module controls temperature autonomously after receiving a target.
    # Holding the connector-wide hardware lock for the entire thermal wait would
    # block unrelated robot-server pipetting and gripper work for minutes. These
    # methods take the global lock only around actual device I/O and use the
    # controller-local lock to serialize target ownership.
    _SHARED_LOCK_EXEMPT_METHODS = frozenset(
        {
            "deactivate",
            "set_temperature",
            "set_temperature_and_wait",
            "wait_for_target_temperature",
        }
    )

    def __init__(self, driver: object = None, module: object = None, lock: asyncio.Lock | None = None) -> None:
        super().__init__(driver=driver, module=module, lock=lock)
        self._temperature_control_lock = asyncio.Lock()
        self._driver_state = TemperatureModuleState(
            status="unknown",
            current_temperature=0.0,
            target_temperature=None,
        )
        self._driver_device_info = DeviceInfo(serial_number="", model="", firmware_version="")

    def _update_driver_state(self, current: float, target: float | None) -> None:
        if target is None:
            status = "idle"
        elif abs(current - target) <= 0.5:
            status = "holding at target"
        elif current < target:
            status = "heating"
        else:
            status = "cooling"
        self._driver_state = TemperatureModuleState(
            status=status,
            current_temperature=float(current),
            target_temperature=float(target) if target is not None else None,
        )

    @property
    def state(self) -> TemperatureModuleState:
        """Return the latest state maintained by the Opentrons module poller."""
        if self._module is not None:
            status = self._module.status
            return TemperatureModuleState(
                status=getattr(status, "value", str(status)),
                current_temperature=float(self._module.temperature),
                target_temperature=float(self._module.target) if self._module.target is not None else None,
            )
        return self._driver_state

    @property
    def device_info(self) -> DeviceInfo:
        """Return the attached GEN2 module identity."""
        if self._module is not None:
            return DeviceInfo.from_dict(dict(self._module.device_info))
        return self._driver_device_info

    @classmethod
    async def build(cls, port: str) -> "TemperatureModuleController":
        """
        Build a controller that owns the serial port via a low-level driver.

        Args:
            port: Serial port path.

        Returns:
            Configured TemperatureModuleController.
        """
        driver = await TempDeckDriver.create(port=port, loop=None)
        await driver.connect()
        controller = cls(driver=driver)
        controller._driver_device_info = await controller.get_device_info()
        await controller.get_temperature()
        return controller

    async def set_temperature(self, temperature: float) -> None:
        """Set target temperature in Celsius (does not wait for the target to be reached)."""
        async with self._temperature_control_lock:
            await self._set_temperature(temperature)

    async def _set_temperature(self, temperature: float) -> None:
        """Send one target while briefly owning the connector-wide hardware lock."""
        if not math.isfinite(temperature) or not 4.0 <= temperature <= 95.0:
            message = f"Target temperature must be a finite value from 4 to 95 °C; received {temperature!r}."
            raise InvalidTemperatureTargetError(message)
        async with self._operation_lock():
            if self._module is not None:
                await self._module.start_set_temperature(temperature)
            else:
                await self._driver.set_temperature(celsius=temperature)
                self._update_driver_state(self._driver_state.current_temperature, temperature)

    async def set_temperature_and_wait(self, temperature: float) -> None:
        """Set a target and wait until the module reports that it is holding it."""
        async with self._temperature_control_lock:
            await self._set_temperature(temperature)
            await self._wait_for_target_temperature(temperature)

    async def get_temperature(self) -> Temperature:
        """Get current and target temperature."""
        if self._module is not None:
            return Temperature(current=self._module.temperature, target=self._module.target)
        t = await self._driver.get_temperature()
        self._update_driver_state(t.current, t.target)
        return Temperature(current=t.current, target=t.target)

    async def wait_for_target_temperature(self) -> None:
        """Wait for the active target without accepting a second, conflicting value."""
        async with self._temperature_control_lock:
            target = self._module.target if self._module is not None else (await self.get_temperature()).target
            if target is None:
                message = "The Temperature Module has no active target. Set a target temperature before waiting."
                raise ModuleOperationError(message)
            await self._wait_for_target_temperature(float(target))

    async def _wait_for_target_temperature(self, expected_target: float) -> None:
        """Wait without monopolizing the global lock, failing if another interface changes the target."""
        if self._module is not None:
            while True:
                active_target = self._module.target
                if active_target is None or abs(float(active_target) - expected_target) > 0.01:
                    message = (
                        f"Temperature Module target changed while waiting: expected {expected_target} °C, "
                        f"observed {active_target!r}. Another client may have changed or deactivated the module."
                    )
                    raise ModuleOperationError(message)
                status = getattr(self._module.status, "value", str(self._module.status))
                if status == "holding at target":
                    return
                if status == "error":
                    message = "Temperature Module entered an error state while waiting for its target."
                    raise ModuleOperationError(message)
                await asyncio.sleep(0.25)
        else:
            while True:
                reading = await self.get_temperature()
                if reading.target is None or abs(reading.target - expected_target) > 0.01:
                    message = (
                        f"Temperature Module target changed while waiting: expected {expected_target} °C, "
                        f"observed {reading.target!r}. Another client may have changed or deactivated the module."
                    )
                    raise ModuleOperationError(message)
                if abs(reading.current - expected_target) <= 0.5:
                    return
                await asyncio.sleep(1.0)

    async def deactivate(self) -> None:
        """Turn off temperature control."""
        async with self._temperature_control_lock, self._operation_lock():
            if self._module is not None:
                await self._module.deactivate()
            else:
                await self._driver.deactivate()
                self._update_driver_state(self._driver_state.current_temperature, None)
