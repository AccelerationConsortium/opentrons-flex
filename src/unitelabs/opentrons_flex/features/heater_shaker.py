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

# Sourced from opentrons: heater-shaker temperature validated 0-95 C
# (opentrons/protocol_api/module_validation_and_errors.py: HEATER_SHAKER_TEMPERATURE_MAX=95),
# shaking speed 0-3000 RPM (opentrons/hardware_control/modules/heater_shaker.py).
_TempCelsius = typing.Annotated[float, constraints.MinimalInclusive(0.0), constraints.MaximalInclusive(95.0)]
_Rpm = typing.Annotated[int, constraints.MinimalInclusive(0), constraints.MaximalInclusive(3000)]


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
class HeaterShakerStatus:
    """Current status of heater-shaker module."""

    temperature_current: float
    temperature_target: float | None
    rpm_current: int
    rpm_target: int | None
    latch_status: LatchStatus


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
        super().__init__(originator="ca.accelerationconsortium", category="modules")
        self._controller = controller

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def set_temperature(
        self,
        temperature_celsius: _TempCelsius,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Set the target temperature.

        Args:
            temperature_celsius: Target temperature in Celsius (valid range 0-95 C;
                the module heats only, so the effective minimum is ambient).

        Returns:
            Current and target temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Setting heater-shaker temperature target to {temperature_celsius} C.",
            "Heater-shaker temperature target set.",
            "Heater-shaker temperature command cancelled.",
            self._controller.set_temperature(temperature_celsius),
        )
        return await self._controller.get_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def wait_for_temperature(
        self,
        temperature_celsius: _TempCelsius,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Wait until the heater reaches a target temperature.

        Args:
            temperature_celsius: Temperature in Celsius to wait for.

        Yields:
            Update: Current heater wait progress update.

        Returns:
            Current and target temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Waiting for heater-shaker to reach {temperature_celsius} C.",
            "Heater-shaker reached target temperature.",
            "Heater-shaker temperature wait cancelled.",
            self._controller.wait_for_temperature(temperature_celsius),
        )
        return await self._controller.get_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_temperature(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Get the current temperature.

        Returns:
            Current and target temperature.
        """
        return await run_observable(
            status,
            intermediate,
            "Reading heater-shaker temperature.",
            "Heater-shaker temperature read.",
            "Heater-shaker temperature read cancelled.",
            self._controller.get_temperature(),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate_heater(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
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
        return await self._controller.get_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def set_rpm(
        self,
        rpm: _Rpm,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> RPM:
        """
        Set the shaking speed.

        Args:
            rpm: Target shaking speed in revolutions per minute (valid range 0-3000;
                0 stops shaking).

        Returns:
            Current and target RPM.
        """
        await run_observable(
            status,
            intermediate,
            f"Setting heater-shaker speed to {rpm} RPM.",
            "Heater-shaker speed set.",
            "Heater-shaker speed command cancelled.",
            self._controller.set_rpm(rpm),
        )
        return await self._controller.get_rpm()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_rpm(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> RPM:
        """
        Get the current shaking speed.

        Returns:
            Current and target RPM.
        """
        return await run_observable(
            status,
            intermediate,
            "Reading heater-shaker speed.",
            "Heater-shaker speed read.",
            "Heater-shaker speed read cancelled.",
            self._controller.get_rpm(),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def stop_shaking(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> RPM:
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
        return await self._controller.get_rpm()

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
            Temperature, RPM, and latch status.
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
        rpm = await self._controller.get_rpm()
        latch = await self._controller.get_latch_status()

        return HeaterShakerStatus(
            temperature_current=temp.current,
            temperature_target=temp.target,
            rpm_current=rpm.current,
            rpm_target=rpm.target,
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
