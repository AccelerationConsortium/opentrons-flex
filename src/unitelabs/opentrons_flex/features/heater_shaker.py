"""SiLA2 feature for Heater-Shaker module control."""

import enum
import logging
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    DeviceInfo,
    HeaterShakerController,
    Temperature,
    RPM,
)
from ._progress import OperationProgress, run_observable

# The constraints mirror the public Opentrons Heater-Shaker contract. A target
# below ambient may not be reachable, but API 2.25+ accepts 0-95 degrees Celsius.
# Active shaking is 200-3000 revolutions per minute; stopping is a separate
# command so 0 is not overloaded as a hidden control value.
_CELSIUS = constraints.Unit(
    "°C",
    [constraints.Unit.Component(constraints.Unit.SI.KELVIN)],
    offset=273.15,
)
_REVOLUTIONS_PER_MINUTE = constraints.Unit(
    "rpm",
    [constraints.Unit.Component(constraints.Unit.SI.SECOND, exponent=-1)],
    factor=1 / 60,
)
_TempCelsius = typing.Annotated[
    float,
    constraints.MinimalInclusive(0.0),
    constraints.MaximalInclusive(95.0),
    _CELSIUS,
]
_Speed = typing.Annotated[
    int,
    constraints.MinimalInclusive(200),
    constraints.MaximalInclusive(3000),
    _REVOLUTIONS_PER_MINUTE,
]
_TemperatureReading = typing.Annotated[float, _CELSIUS]
_RotationSpeedReading = typing.Annotated[int, _REVOLUTIONS_PER_MINUTE]


log = logging.getLogger(__name__)


class LatchStatus(enum.Enum):
    """Heater-shaker labware latch position (mirrors opentrons HeaterShakerLabwareLatchStatus)."""

    OPENING = "opening"
    IDLE_OPEN = "idle_open"
    CLOSING = "closing"
    IDLE_CLOSED = "idle_closed"
    IDLE_UNKNOWN = "idle_unknown"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "LatchStatus":
        # A status value outside this set (e.g. from a newer opentrons version)
        # must not crash the command with an undefined SiLA error.
        log.warning("Unrecognized heater-shaker latch status %r; reporting UNKNOWN", value)
        return cls.UNKNOWN


@dataclass
class HeaterShakerTemperature:
    """
    Current and requested Heater-Shaker temperature.

    Attributes:
        current: Measured plate temperature.
        target: Requested target, or zero when TargetActive is false.
        target_active: Whether the heater currently has an active target.
    """

    current: _TemperatureReading
    target: _TemperatureReading
    target_active: bool


@dataclass
class HeaterShakerSpeed:
    """
    Current and requested Heater-Shaker rotation speed.

    Attributes:
        current: Measured rotation speed.
        target: Requested target, or zero when TargetActive is false.
        target_active: Whether the shaker currently has an active target.
    """

    current: _RotationSpeedReading
    target: _RotationSpeedReading
    target_active: bool


@dataclass
class HeaterShakerStatus:
    """
    Current Heater-Shaker temperature, speed, and latch state.

    Attributes:
        current_temperature: Measured plate temperature.
        target_temperature: Requested target, or zero when TemperatureTargetActive is false.
        temperature_target_active: Whether the heater currently has an active target.
        current_speed: Measured rotation speed.
        target_speed: Requested target, or zero when SpeedTargetActive is false.
        speed_target_active: Whether the shaker currently has an active target.
        latch_status: Current labware latch state.
    """

    current_temperature: _TemperatureReading
    target_temperature: _TemperatureReading
    temperature_target_active: bool
    current_speed: _RotationSpeedReading
    target_speed: _RotationSpeedReading
    speed_target_active: bool
    latch_status: LatchStatus


def _temperature_response(reading: Temperature) -> HeaterShakerTemperature:
    """Convert Opentrons' nullable target into an explicit SiLA structure."""
    return HeaterShakerTemperature(
        current=reading.current,
        target=reading.target if reading.target is not None else 0.0,
        target_active=reading.target is not None,
    )


def _speed_response(reading: RPM) -> HeaterShakerSpeed:
    """Convert Opentrons' nullable target into an explicit SiLA structure."""
    return HeaterShakerSpeed(
        current=reading.current,
        target=reading.target if reading.target is not None else 0,
        target_active=reading.target is not None,
    )


class HeaterShakerFeature(sila.Feature):
    """
    SiLA2 feature for Heater-Shaker module.

    Provides commands for:
    - Temperature control (heating)
    - Shaking control (orbital motion)
    - Labware latch control
    """

    def __init__(self, controller: HeaterShakerController):
        """
        Initialize the heater-shaker feature.

        Args:
            controller: The HeaterShakerController instance.
        """
        super().__init__(
            originator="ca.accelerationconsortium",
            category="modules",
            identifier="HeaterShakerController",
            name="Heater Shaker Controller",
            version="3.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def set_temperature(
        self,
        temperature: _TempCelsius,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerTemperature:
        """
        Set the target temperature.

        Args:
            temperature: Target temperature in Celsius (valid range 0-95 C;
                the module heats only, so the effective minimum is ambient).

        Returns:
            Current and target temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Setting heater-shaker temperature target to {temperature} C.",
            "Heater-shaker temperature target set.",
            "Heater-shaker temperature command cancelled.",
            self._controller.set_temperature(temperature),
        )
        return _temperature_response(await self._controller.get_temperature())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def wait_for_temperature(
        self,
        temperature: _TempCelsius,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerTemperature:
        """
        Wait until the heater reaches a target temperature.

        Args:
            temperature: Temperature in Celsius to wait for.

        Yields:
            Update: Current heater wait progress update.

        Returns:
            Current and target temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Waiting for heater-shaker to reach {temperature} C.",
            "Heater-shaker reached target temperature.",
            "Heater-shaker temperature wait cancelled.",
            self._controller.wait_for_temperature(temperature),
        )
        return _temperature_response(await self._controller.get_temperature())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_temperature(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerTemperature:
        """
        Get the current temperature.

        Returns:
            Current and target temperature.
        """
        reading = await run_observable(
            status,
            intermediate,
            "Reading heater-shaker temperature.",
            "Heater-shaker temperature read.",
            "Heater-shaker temperature read cancelled.",
            self._controller.get_temperature(),
        )
        return _temperature_response(reading)

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate_heater(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerTemperature:
        """
        Turn off the heater.

        Returns:
            Current and target temperature after deactivation.
        """
        await run_observable(
            status,
            intermediate,
            "Deactivating heater-shaker heater.",
            "Heater-shaker heater deactivated.",
            "Heater-shaker heater deactivation cancelled.",
            self._controller.deactivate_heater(),
        )
        return _temperature_response(await self._controller.get_temperature())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def set_speed(
        self,
        speed: _Speed,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerSpeed:
        """
        Set the shaking speed.

        Args:
            speed: Target shaking speed (valid range 200-3000 revolutions per minute).
                Use StopShaking to stop and home the shaker.

        Returns:
            Current and target rotation speed.
        """
        await run_observable(
            status,
            intermediate,
            f"Setting heater-shaker speed to {speed} revolutions per minute.",
            "Heater-shaker speed set.",
            "Heater-shaker speed command cancelled.",
            self._controller.set_rpm(speed),
        )
        return _speed_response(await self._controller.get_rpm())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_speed(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerSpeed:
        """
        Get the current shaking speed.

        Returns:
            Current and target rotation speed.
        """
        reading = await run_observable(
            status,
            intermediate,
            "Reading heater-shaker speed.",
            "Heater-shaker speed read.",
            "Heater-shaker speed read cancelled.",
            self._controller.get_rpm(),
        )
        return _speed_response(reading)

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def stop_shaking(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerSpeed:
        """
        Stop shaking and return to home position.

        Returns:
            Current and target RPM after stopping.
        """
        await run_observable(
            status,
            intermediate,
            "Stopping heater-shaker shaking.",
            "Heater-shaker shaking stopped.",
            "Heater-shaker stop shaking cancelled.",
            self._controller.stop_shaking(),
        )
        return _speed_response(await self._controller.get_rpm())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def open_latch(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LatchStatus:
        """
        Open the labware latch.

        Returns:
            Latch status after opening.
        """
        await run_observable(
            status,
            intermediate,
            "Opening heater-shaker labware latch.",
            "Heater-shaker labware latch opened.",
            "Heater-shaker latch open cancelled.",
            self._controller.open_latch(),
        )
        latch_status = await self._controller.get_latch_status()
        return LatchStatus(latch_status.value)

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def close_latch(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LatchStatus:
        """
        Close the labware latch.

        Returns:
            Latch status after closing.
        """
        await run_observable(
            status,
            intermediate,
            "Closing heater-shaker labware latch.",
            "Heater-shaker labware latch closed.",
            "Heater-shaker latch close cancelled.",
            self._controller.close_latch(),
        )
        latch_status = await self._controller.get_latch_status()
        return LatchStatus(latch_status.value)

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_latch_status(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LatchStatus:
        """
        Get the current latch status.

        Returns:
            Latch status (idle_open, idle_closed, opening, closing, etc.).
        """
        latch_status = await run_observable(
            status,
            intermediate,
            "Reading heater-shaker latch status.",
            "Heater-shaker latch status read.",
            "Heater-shaker latch status read cancelled.",
            self._controller.get_latch_status(),
        )
        return LatchStatus(latch_status.value)

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_status(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> HeaterShakerStatus:
        """
        Get complete module status.

        Returns:
            Temperature, rotation speed, and latch status.
        """
        await run_observable(
            status,
            intermediate,
            "Reading heater-shaker status.",
            "Heater-shaker status read.",
            "Heater-shaker status read cancelled.",
            self._controller.is_connected(),
        )
        temp = await self._controller.get_temperature()
        speed = await self._controller.get_rpm()
        latch = await self._controller.get_latch_status()

        return HeaterShakerStatus(
            current_temperature=temp.current,
            target_temperature=temp.target if temp.target is not None else 0.0,
            temperature_target_active=temp.target is not None,
            current_speed=speed.current,
            target_speed=speed.target if speed.target is not None else 0,
            speed_target_active=speed.target is not None,
            latch_status=LatchStatus(latch.value),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_device_info(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> DeviceInfo:
        """
        Get device information.

        Returns:
            Serial number, model, and firmware version.
        """
        return await run_observable(
            status,
            intermediate,
            "Reading heater-shaker device information.",
            "Heater-shaker device information read.",
            "Heater-shaker device information read cancelled.",
            self._controller.get_device_info(),
        )
