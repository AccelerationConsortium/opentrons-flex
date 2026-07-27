"""Thermocycler module IO wrapper."""

import asyncio
import logging
import math

from opentrons.drivers.thermocycler.driver import ThermocyclerDriverV2
from opentrons.hardware_control.modules.types import ThermocyclerStep

from ._errors import InvalidThermocyclerProfileError, ModuleOperationError
from ._module_base import ModuleControllerBase
from ._types import DeviceInfo, Temperature

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

    _SHARED_LOCK_EXEMPT_METHODS = frozenset(
        {
            "deactivate_all",
            "deactivate_block",
            "deactivate_lid",
            "execute_profile",
            "set_lid_temperature",
            "set_plate_temperature",
            "wait_for_lid_temperature",
            "wait_for_plate_temperature",
        }
    )

    def __init__(self, driver: object = None, module: object = None, lock: asyncio.Lock | None = None) -> None:
        super().__init__(driver=driver, module=module, lock=lock)
        self._lid_control_lock = asyncio.Lock()
        self._block_control_lock = asyncio.Lock()
        self._driver_device_info = DeviceInfo(serial_number="", model="", firmware_version="")
        self._profile_completed_steps = 0
        self._profile_total_steps = 0
        self._profile_repetition = 0
        self._profile_step = 0
        self._profile_active = False

    @property
    def device_info(self) -> DeviceInfo:
        """Return the attached module identity without an additional device call."""
        if self._module is not None:
            return DeviceInfo.from_dict(dict(self._module.device_info))
        return self._driver_device_info

    @property
    def profile_progress(self) -> tuple[int, int, int, int, bool]:
        """Return completed/total steps, current repetition/step, and active state."""
        return (
            self._profile_completed_steps,
            self._profile_total_steps,
            self._profile_repetition,
            self._profile_step,
            self._profile_active,
        )

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
        controller = cls(driver=driver)
        controller._driver_device_info = await controller.get_device_info()
        return controller

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
        self._validate_range(temperature, "Lid temperature", 37.0, 110.0)
        async with self._lid_control_lock, self._operation_lock():
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
        async with self._block_control_lock:
            await self._set_plate_temperature(temperature, hold_time, volume, ramp_rate)

    async def _set_plate_temperature(
        self,
        temperature: float,
        hold_time: float | None,
        volume: float | None,
        ramp_rate: float | None,
    ) -> None:
        """Validate and send one block target while briefly owning the shared lock."""
        self._validate_range(temperature, "Block temperature", 4.0, 99.0)
        if hold_time is not None:
            self._validate_range(hold_time, "Hold time", 0.0, None)
        if volume is not None:
            self._validate_range(volume, "Block maximum volume", 1.0, 200.0)
        if ramp_rate is not None:
            self._validate_range(ramp_rate, "Ramp rate", 0.01, 4.25)

        async with self._operation_lock():
            current_temperature = (await self._read_plate_temperature())[0].current
            if ramp_rate is not None:
                maximum_ramp_rate = 4.25 if temperature >= current_temperature else 2.0
                if ramp_rate > maximum_ramp_rate:
                    direction = "heating" if temperature >= current_temperature else "cooling"
                    message = (
                        f"Ramp rate for {direction} from {current_temperature} to {temperature} °C "
                        f"must be between 0.01 and {maximum_ramp_rate} °C/s; received {ramp_rate!r}."
                    )
                    raise InvalidThermocyclerProfileError(message)
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
        """Wait for the active lid target without monopolizing the global lock."""
        async with self._lid_control_lock:
            reading = await self.get_lid_temperature()
            if reading.target is None:
                message = "The Thermocycler lid has no active target. Set a lid temperature before waiting."
                raise ModuleOperationError(message)
            await self._wait_for_lid_temperature(float(reading.target))

    async def wait_for_plate_temperature(self) -> None:
        """Wait for the active block target without monopolizing the global lock."""
        async with self._block_control_lock:
            reading = await self.get_plate_temperature()
            if reading.target is None:
                message = "The Thermocycler block has no active target. Set a block temperature before waiting."
                raise ModuleOperationError(message)
            await self._wait_for_plate_temperature(float(reading.target))

    async def _wait_for_plate_temperature(self, expected_target: float) -> None:
        """Wait for a specific block target and fail if another interface changes it."""
        while True:
            async with self._operation_lock(observation=True):
                reading, hold_remaining = await self._read_plate_temperature()
            self._require_unchanged_target("block", expected_target, reading.target)
            at_temperature = abs(reading.current - expected_target) <= 0.5
            hold_complete = hold_remaining is None or hold_remaining <= 0
            if at_temperature and hold_complete:
                return
            await asyncio.sleep(0.25 if self._module is not None else 1.0)

    async def _wait_for_lid_temperature(self, expected_target: float) -> None:
        """Wait for a specific lid target and fail if another interface changes it."""
        while True:
            reading = await self.get_lid_temperature()
            self._require_unchanged_target("lid", expected_target, reading.target)
            if abs(reading.current - expected_target) <= 0.5:
                return
            await asyncio.sleep(0.25 if self._module is not None else 1.0)

    @staticmethod
    def _require_unchanged_target(name: str, expected: float, observed: float | None) -> None:
        """Raise a recoverable error when another interface replaces a wait target."""
        if observed is None or abs(float(observed) - expected) > 0.01:
            message = (
                f"Thermocycler {name} target changed while waiting: expected {expected} °C, "
                f"observed {observed!r}. Another client may have changed or deactivated the module."
            )
            raise ModuleOperationError(message)

    @staticmethod
    def _validate_range(value: float, name: str, minimum: float, maximum: float | None) -> None:
        """Validate a finite Thermocycler value before sending it to hardware."""
        try:
            in_range = math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
        except (TypeError, ValueError):
            in_range = False
        if in_range:
            return
        maximum_text = f" and {maximum}" if maximum is not None else ""
        message = f"{name} must be a finite value between {minimum}{maximum_text}; received {value!r}."
        raise InvalidThermocyclerProfileError(message)

    @classmethod
    def _validate_profile(
        cls,
        steps: list[ThermocyclerStep],
        repetitions: int,
        volume: float | None,
    ) -> None:
        """Validate profile shape and values before starting its first step."""
        if not 1 <= len(steps) <= 20:
            message = f"Thermocycler profile must contain between 1 and 20 steps; received {len(steps)}."
            raise InvalidThermocyclerProfileError(message)
        if repetitions < 1:
            message = f"Thermocycler repetitions must be at least 1; received {repetitions!r}."
            raise InvalidThermocyclerProfileError(message)
        if volume is not None:
            cls._validate_range(volume, "Block maximum volume", 1.0, 200.0)
        for index, step in enumerate(steps, start=1):
            try:
                temperature = float(step["temperature"])
                hold_time = step.get("hold_time_seconds")
                ramp_rate = step.get("ramp_rate")
            except (KeyError, TypeError, ValueError) as exc:
                message = f"Thermocycler profile step {index} is missing a numeric temperature."
                raise InvalidThermocyclerProfileError(message) from exc
            cls._validate_range(temperature, f"Profile step {index} temperature", 4.0, 99.0)
            if hold_time is not None:
                cls._validate_range(float(hold_time), f"Profile step {index} hold time", 0.0, None)
            if ramp_rate is not None:
                cls._validate_range(float(ramp_rate), f"Profile step {index} ramp rate", 0.01, 4.25)

    async def execute_profile(
        self,
        steps: list[ThermocyclerStep],
        repetitions: int,
        volume: float | None = None,
    ) -> None:
        """
        Execute a validated profile while releasing the shared lock between device calls.

        The local block-control lock prevents another SiLA block command from
        replacing the profile target. A parallel HTTP command is detected by the
        target check and fails the profile instead of allowing false success.
        """
        self._validate_profile(steps, repetitions, volume)
        async with self._block_control_lock:
            self._profile_completed_steps = 0
            self._profile_total_steps = len(steps) * repetitions
            self._profile_repetition = 0
            self._profile_step = 0
            self._profile_active = True
            try:
                for repetition in range(1, repetitions + 1):
                    self._profile_repetition = repetition
                    for step_index, step in enumerate(steps, start=1):
                        self._profile_step = step_index
                        target = float(step["temperature"])
                        await self._set_plate_temperature(
                            temperature=target,
                            hold_time=step.get("hold_time_seconds"),
                            volume=volume,
                            ramp_rate=step.get("ramp_rate"),
                        )
                        await self._wait_for_plate_temperature(target)
                        self._profile_completed_steps += 1
            finally:
                self._profile_active = False

    async def get_lid_temperature(self) -> Temperature:
        """Get lid temperature."""
        if self._module is not None:
            return Temperature(current=self._module.lid_temp, target=self._module.lid_target)
        t = await self._driver.get_lid_temperature()
        return Temperature(current=t.current, target=t.target)

    async def get_plate_temperature(self) -> Temperature:
        """Get plate (block) temperature."""
        return (await self._read_plate_temperature())[0]

    async def _read_plate_temperature(self) -> tuple[Temperature, float | None]:
        """Read block temperature and the firmware's remaining hold time."""
        if self._module is not None:
            return (
                Temperature(current=self._module.temperature, target=self._module.target),
                self._module.hold_time,
            )
        t = await self._driver.get_plate_temperature()
        return Temperature(current=t.current, target=t.target), t.hold

    async def deactivate_lid(self) -> None:
        """Turn off lid heater."""
        async with self._lid_control_lock, self._operation_lock():
            if self._module is not None:
                await self._module.deactivate_lid()
            else:
                await self._driver.deactivate_lid()

    async def deactivate_block(self) -> None:
        """Turn off block heater/cooler."""
        async with self._block_control_lock, self._operation_lock():
            if self._module is not None:
                await self._module.deactivate_block()
            else:
                await self._driver.deactivate_block()

    async def deactivate_all(self) -> None:
        """Turn off all heating/cooling."""
        async with self._lid_control_lock, self._block_control_lock, self._operation_lock():
            if self._module is not None:
                await self._module.deactivate()
            else:
                await self._driver.deactivate_all()
