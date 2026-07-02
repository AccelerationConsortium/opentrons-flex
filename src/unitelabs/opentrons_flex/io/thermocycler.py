"""Thermocycler module IO wrapper."""

import asyncio
import logging

from opentrons.drivers.thermocycler.driver import ThermocyclerDriverV2
from opentrons.hardware_control.modules.types import ThermocyclerStep

from ._module_base import ModuleControllerBase
from ._types import Temperature

log = logging.getLogger(__name__)


class ThermocyclerController(ModuleControllerBase):
    """
    Controller for Thermocycler module.

    Two backends are supported (see ``ModuleControllerBase``):

    - ``build(port=...)`` wraps a low-level ``ThermocyclerDriverV2`` that owns the
      serial port directly (standalone connector mode).
    - ``from_module(module)`` wraps the high-level ``Thermocycler`` object already
      attached to a shared ``HardwareControlAPI`` (in-process robot-server mode).
    """

    @classmethod
    async def build(cls, port: str) -> "ThermocyclerController":
        """
        Build a controller that owns the serial port via a low-level driver.

        Args:
            port: Serial port path.

        Returns:
            Configured ThermocyclerController.
        """
        driver = await ThermocyclerDriverV2.create(port=port, loop=None)
        await driver.connect()
        return cls(driver=driver)

    async def open_lid(self) -> None:
        """Open the lid."""
        if self._module is not None:
            await self._module.open()
        else:
            await self._driver.open_lid()

    async def close_lid(self) -> None:
        """Close the lid."""
        if self._module is not None:
            await self._module.close()
        else:
            await self._driver.close_lid()

    async def get_lid_status(self) -> str:
        """Get lid status (open/closed/in_between/unknown)."""
        if self._module is not None:
            return self._module.lid_status.name.lower()
        return (await self._driver.get_lid_status()).name.lower()

    async def set_lid_temperature(self, temperature: float) -> None:
        """Set lid temperature in Celsius (does not wait for the target to be reached)."""
        if self._module is not None:
            await self._module.set_target_lid_temperature(temperature)
        else:
            await self._driver.set_lid_temperature(temp=temperature)

    async def set_plate_temperature(
        self,
        temperature: float,
        hold_time: float | None = None,
        volume: float | None = None,
        ramp_rate: float | None = None,
    ) -> None:
        """
        Set plate (block) temperature (does not wait for the target to be reached).

        Args:
            temperature: Target temperature in Celsius.
            hold_time: Optional hold time in seconds.
            volume: Optional sample volume in uL.
            ramp_rate: Optional ramp rate in C/s.
        """
        if self._module is not None:
            kwargs = {"hold_time_seconds": hold_time, "volume": volume}
            if ramp_rate is not None:
                kwargs["ramp_rate"] = ramp_rate
            await self._module.set_target_block_temperature(temperature, **kwargs)
        else:
            await self._driver.set_plate_temperature(
                temp=temperature,
                hold_time=hold_time,
                volume=volume,
                ramp_rate=ramp_rate,
            )

    async def wait_for_lid_temperature(self) -> None:
        """Wait until the lid reaches its target temperature."""
        if self._module is not None:
            await self._module.wait_for_lid_target()
        else:
            while True:
                temp = await self.get_lid_temperature()
                if temp.target is None or abs(temp.current - temp.target) <= 0.5:
                    return
                await asyncio.sleep(1.0)

    async def wait_for_plate_temperature(self) -> None:
        """Wait until the plate reaches its target temperature."""
        if self._module is not None:
            await self._module.wait_for_block_target()
        else:
            while True:
                temp = await self.get_plate_temperature()
                if temp.target is None or abs(temp.current - temp.target) <= 0.5:
                    return
                await asyncio.sleep(1.0)

    async def execute_profile(
        self,
        steps: list[ThermocyclerStep],
        repetitions: int,
        volume: float | None = None,
    ) -> None:
        """
        Execute a simple thermocycler profile.

        Args:
            steps: List of step dictionaries accepted by the opentrons module
                object, or converted to sequential low-level set/wait calls.
            repetitions: Number of times to repeat the steps.
            volume: Optional sample volume in uL.
        """
        if self._module is not None:
            await self._module.cycle_temperatures(steps=steps, repetitions=repetitions, volume=volume)
            return

        for _ in range(repetitions):
            for step in steps:
                await self.set_plate_temperature(
                    temperature=float(step["temperature"]),
                    hold_time=step.get("hold_time_seconds"),
                    volume=volume,
                    ramp_rate=step.get("ramp_rate"),
                )
                await self.wait_for_plate_temperature()

    async def get_lid_temperature(self) -> Temperature:
        """Get lid temperature."""
        if self._module is not None:
            return Temperature(current=self._module.lid_temp, target=self._module.lid_target)
        t = await self._driver.get_lid_temperature()
        return Temperature(current=t.current, target=t.target)

    async def get_plate_temperature(self) -> Temperature:
        """Get plate (block) temperature."""
        if self._module is not None:
            return Temperature(current=self._module.temperature, target=self._module.target)
        t = await self._driver.get_plate_temperature()
        return Temperature(current=t.current, target=t.target)

    async def deactivate_lid(self) -> None:
        """Turn off lid heater."""
        if self._module is not None:
            await self._module.deactivate_lid()
        else:
            await self._driver.deactivate_lid()

    async def deactivate_block(self) -> None:
        """Turn off block heater/cooler."""
        if self._module is not None:
            await self._module.deactivate_block()
        else:
            await self._driver.deactivate_block()

    async def deactivate_all(self) -> None:
        """Turn off all heating/cooling."""
        if self._module is not None:
            await self._module.deactivate()
        else:
            await self._driver.deactivate_all()
