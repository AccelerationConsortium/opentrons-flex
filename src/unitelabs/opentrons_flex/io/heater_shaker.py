"""Heater-Shaker module IO wrapper."""

import asyncio
import logging
import math

from opentrons.drivers.heater_shaker.driver import HeaterShakerDriver
from opentrons.drivers.heater_shaker.abstract import HeaterShakerLabwareLatchStatus

from ._errors import InvalidHeaterShakerTemperatureError, ModuleOperationError
from ._module_base import ModuleControllerBase
from ._types import DeviceInfo, RPM, Temperature

log = logging.getLogger(__name__)


class HeaterShakerController(ModuleControllerBase):
    """
    Controller for Heater-Shaker module.

    Two backends are supported (see ``ModuleControllerBase``):

    - ``build(port=...)`` wraps a low-level ``HeaterShakerDriver`` that owns the
      serial port directly (standalone connector mode).
    - ``from_module(module)`` wraps the high-level ``HeaterShaker`` object already
      attached to a shared ``HardwareControlAPI`` (in-process robot-server mode).
    """

    _SHARED_LOCK_EXEMPT_METHODS = frozenset(
        {
            "deactivate_heater",
            "set_temperature",
            "wait_for_temperature",
        }
    )

    def __init__(self, driver: object = None, module: object = None, lock: asyncio.Lock | None = None) -> None:
        super().__init__(driver=driver, module=module, lock=lock)
        self._temperature_control_lock = asyncio.Lock()
        self._driver_device_info = DeviceInfo(serial_number="", model="", firmware_version="")

    @property
    def device_info(self) -> DeviceInfo:
        """Return the attached module identity without an additional device call."""
        if self._module is not None:
            return DeviceInfo.from_dict(dict(self._module.device_info))
        return self._driver_device_info

    @classmethod
    async def build(cls, port: str) -> "HeaterShakerController":
        """
        Build a controller that owns the serial port via a low-level driver.

        Args:
            port: Serial port path.

        Returns:
            Configured HeaterShakerController.
        """
        driver = await HeaterShakerDriver.create(port=port, loop=None)
        await driver.connect()
        controller = cls(driver=driver)
        controller._driver_device_info = await controller.get_device_info()
        return controller

    async def set_temperature(self, temperature: float) -> None:
        """Set target temperature in Celsius (does not wait for the target to be reached)."""
        self._validate_temperature(temperature)
        async with self._temperature_control_lock, self._operation_lock():
            if self._module is not None:
                await self._module.start_set_temperature(temperature)
            else:
                await self._driver.set_temperature(temperature=temperature)

    async def get_temperature(self) -> Temperature:
        """Get current and target temperature."""
        if self._module is not None:
            return Temperature(current=self._module.temperature, target=self._module.target_temperature)
        t = await self._driver.get_temperature()
        return Temperature(current=t.current, target=t.target)

    async def wait_for_temperature(self, temperature: float) -> None:
        """Wait for one active target without monopolizing the connector-wide lock."""
        self._validate_temperature(temperature)
        async with self._temperature_control_lock:
            while True:
                reading = await self.get_temperature()
                if reading.target is None or abs(float(reading.target) - temperature) > 0.01:
                    message = (
                        f"Heater-Shaker target changed while waiting: expected {temperature} °C, "
                        f"observed {reading.target!r}. Another client may have changed or deactivated the module."
                    )
                    raise ModuleOperationError(message)
                if abs(reading.current - temperature) <= 0.5:
                    return
                await asyncio.sleep(0.25 if self._module is not None else 1.0)

    async def deactivate_heater(self) -> None:
        """Turn off the heater."""
        async with self._temperature_control_lock, self._operation_lock():
            if self._module is not None:
                await self._module.deactivate_heater()
            else:
                await self._driver.deactivate_heater()

    async def set_rpm(self, rpm: int) -> None:
        """Set shaking speed in RPM."""
        if self._module is not None:
            await self._module.set_speed(rpm)
        else:
            await self._driver.set_rpm(rpm=rpm)

    async def get_rpm(self) -> RPM:
        """Get current and target RPM."""
        if self._module is not None:
            return RPM(current=self._module.speed, target=self._module.target_speed)
        r = await self._driver.get_rpm()
        return RPM(current=r.current, target=r.target)

    async def stop_shaking(self) -> None:
        """Stop shaking (home)."""
        if self._module is not None:
            await self._module.deactivate_shaker()
        else:
            await self._driver.home()

    async def open_latch(self) -> None:
        """Open the labware latch."""
        if self._module is not None:
            await self._module.open_labware_latch()
        else:
            await self._driver.open_labware_latch()

    async def close_latch(self) -> None:
        """Close the labware latch."""
        if self._module is not None:
            await self._module.close_labware_latch()
        else:
            await self._driver.close_labware_latch()

    async def get_latch_status(self) -> HeaterShakerLabwareLatchStatus:
        """Get latch status."""
        if self._module is not None:
            return self._module.labware_latch_status
        return await self._driver.get_labware_latch_status()

    @staticmethod
    def _validate_temperature(temperature: float) -> None:
        """Reject values outside the pinned Opentrons Heater-Shaker contract."""
        try:
            is_valid = math.isfinite(temperature) and 0.0 <= temperature <= 95.0
        except (TypeError, ValueError):
            is_valid = False
        if not is_valid:
            message = (
                f"Heater-Shaker target temperature must be a finite value from 0 to 95 °C; received {temperature!r}."
            )
            raise InvalidHeaterShakerTemperatureError(message)
