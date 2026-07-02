"""SiLA2 feature for Thermocycler module control."""

import enum
import logging
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    DeviceInfo,
    ThermocyclerController,
    Temperature,
)
from ._progress import OperationProgress, run_observable

# Sourced from opentrons protocol_api/module_contexts.py: block 4-99 C, lid 37-110 C.
_BlockCelsius = typing.Annotated[float, constraints.MinimalInclusive(4.0), constraints.MaximalInclusive(99.0)]
_LidCelsius = typing.Annotated[float, constraints.MinimalInclusive(37.0), constraints.MaximalInclusive(110.0)]
_NonNegativeFloat = typing.Annotated[float, constraints.MinimalInclusive(0.0)]
_PositiveInteger = typing.Annotated[int, constraints.MinimalInclusive(1)]


log = logging.getLogger(__name__)


class LidStatus(enum.Enum):
    """Thermocycler lid position (mirrors opentrons ThermocyclerLidStatus)."""

    OPEN = "open"
    CLOSED = "closed"
    IN_BETWEEN = "in_between"
    UNKNOWN = "unknown"
    MAX = "max"

    @classmethod
    def _missing_(cls, value: object) -> "LidStatus":
        # A status value outside this set (e.g. from a newer opentrons version)
        # must not crash the command with an undefined SiLA error.
        log.warning("Unrecognized thermocycler lid status %r; reporting UNKNOWN", value)
        return cls.UNKNOWN


@dataclass
class ThermocyclerStatus:
    """Current status of thermocycler module."""

    lid_temperature_current: float
    lid_temperature_target: float | None
    plate_temperature_current: float
    plate_temperature_target: float | None
    lid_status: LidStatus


@dataclass
class ThermocyclerProfileStep:
    """One thermocycler profile step."""

    temperature_celsius: float
    hold_time_seconds: float
    ramp_rate: float


class ThermocyclerFeature(sila.Feature):
    """
    SiLA2 feature for Thermocycler module.

    Provides commands for:
    - Lid control (open/close)
    - Lid temperature control
    - Plate (block) temperature control
    """

    def __init__(self, controller: ThermocyclerController):
        """
        Initialize the thermocycler feature.

        Args:
            controller: The ThermocyclerController instance.
        """
        super().__init__(originator="ca.accelerationconsortium", category="modules")
        self._controller = controller

    # ============ Lid Control ============

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def open_lid(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LidStatus:
        """
        Open the thermocycler lid.

        Returns:
            Lid status after opening.
        """
        await run_observable(
            status,
            intermediate,
            "Opening thermocycler lid.",
            "Thermocycler lid opened.",
            "Thermocycler lid open cancelled.",
            self._controller.open_lid(),
        )
        return LidStatus(await self._controller.get_lid_status())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def close_lid(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LidStatus:
        """
        Close the thermocycler lid.

        Returns:
            Lid status after closing.
        """
        await run_observable(
            status,
            intermediate,
            "Closing thermocycler lid.",
            "Thermocycler lid closed.",
            "Thermocycler lid close cancelled.",
            self._controller.close_lid(),
        )
        return LidStatus(await self._controller.get_lid_status())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_lid_status(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LidStatus:
        """
        Get the current lid status.

        Returns:
            Lid status (open, closed, in_between, unknown).
        """
        lid_status = await run_observable(
            status,
            intermediate,
            "Reading thermocycler lid status.",
            "Thermocycler lid status read.",
            "Thermocycler lid status read cancelled.",
            self._controller.get_lid_status(),
        )
        return LidStatus(lid_status)

    # ============ Temperature Control ============

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def set_lid_temperature(
        self,
        temperature_celsius: _LidCelsius,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Set the lid temperature.

        Args:
            temperature_celsius: Target lid temperature in Celsius (valid range 37-110 C).

        Returns:
            Current and target lid temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Setting thermocycler lid temperature to {temperature_celsius} C.",
            "Thermocycler lid temperature target set.",
            "Thermocycler lid temperature command cancelled.",
            self._controller.set_lid_temperature(temperature_celsius),
        )
        return await self._controller.get_lid_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def wait_for_lid_temperature(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Wait until the thermocycler lid reaches its current target.

        Yields:
            Update: Current lid-temperature wait progress update.

        Returns:
            Current and target lid temperature.
        """
        await run_observable(
            status,
            intermediate,
            "Waiting for thermocycler lid target temperature.",
            "Thermocycler lid reached target temperature.",
            "Thermocycler lid temperature wait cancelled.",
            self._controller.wait_for_lid_temperature(),
        )
        return await self._controller.get_lid_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_lid_temperature(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Get the current lid temperature.

        Returns:
            Current and target lid temperature.
        """
        return await run_observable(
            status,
            intermediate,
            "Reading thermocycler lid temperature.",
            "Thermocycler lid temperature read.",
            "Thermocycler lid temperature read cancelled.",
            self._controller.get_lid_temperature(),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def set_plate_temperature(
        self,
        temperature_celsius: _BlockCelsius,
        hold_time_seconds: _NonNegativeFloat,
        volume_ul: _NonNegativeFloat,
        ramp_rate: _NonNegativeFloat,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Set the plate (block) temperature.

        Args:
            temperature_celsius: Target block temperature in Celsius (valid range 4-99 C).
            hold_time_seconds: Hold time in seconds, or 0 for no hold time.
            volume_ul: Sample volume in uL for thermal control, or 0 for no volume.
            ramp_rate: Ramp rate in C/s, or 0 for module default.

        Returns:
            Current and target plate temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Setting thermocycler plate temperature to {temperature_celsius} C.",
            "Thermocycler plate temperature target set.",
            "Thermocycler plate temperature command cancelled.",
            self._controller.set_plate_temperature(
                temperature=temperature_celsius,
                hold_time=hold_time_seconds if hold_time_seconds > 0 else None,
                volume=volume_ul if volume_ul > 0 else None,
                ramp_rate=ramp_rate if ramp_rate > 0 else None,
            ),
        )
        return await self._controller.get_plate_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def wait_for_plate_temperature(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Wait until the thermocycler plate reaches its current target.

        Yields:
            Update: Current plate-temperature wait progress update.

        Returns:
            Current and target plate temperature.
        """
        await run_observable(
            status,
            intermediate,
            "Waiting for thermocycler plate target temperature.",
            "Thermocycler plate reached target temperature.",
            "Thermocycler plate temperature wait cancelled.",
            self._controller.wait_for_plate_temperature(),
        )
        return await self._controller.get_plate_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_plate_temperature(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Get the current plate (block) temperature.

        Returns:
            Current and target plate temperature.
        """
        return await run_observable(
            status,
            intermediate,
            "Reading thermocycler plate temperature.",
            "Thermocycler plate temperature read.",
            "Thermocycler plate temperature read cancelled.",
            self._controller.get_plate_temperature(),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def execute_profile(
        self,
        steps: list[ThermocyclerProfileStep],
        repetitions: _PositiveInteger,
        volume_ul: _NonNegativeFloat,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> ThermocyclerStatus:
        """
        Execute a thermocycler plate-temperature profile.

        Args:
            steps: Ordered profile steps.
            repetitions: Number of repetitions.
            volume_ul: Sample volume in uL, or 0 for no volume.

        Yields:
            Update: Current profile execution progress update.

        Returns:
            Full thermocycler status after execution.
        """
        profile = [
            {
                "temperature": step.temperature_celsius,
                "hold_time_seconds": step.hold_time_seconds if step.hold_time_seconds > 0 else None,
                "ramp_rate": step.ramp_rate if step.ramp_rate > 0 else None,
            }
            for step in steps
        ]
        await run_observable(
            status,
            intermediate,
            "Starting thermocycler profile.",
            "Thermocycler profile completed.",
            "Thermocycler profile cancelled.",
            self._controller.execute_profile(
                steps=profile,
                repetitions=repetitions,
                volume=volume_ul if volume_ul > 0 else None,
            ),
        )
        return await self._status()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate_lid(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Turn off the lid heater.

        Returns:
            Current lid temperature after deactivation.
        """
        await run_observable(
            status,
            intermediate,
            "Deactivating thermocycler lid heater.",
            "Thermocycler lid heater deactivated.",
            "Thermocycler lid heater deactivation cancelled.",
            self._controller.deactivate_lid(),
        )
        return await self._controller.get_lid_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate_block(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Turn off the block heater/cooler.

        Returns:
            Current plate temperature after deactivation.
        """
        await run_observable(
            status,
            intermediate,
            "Deactivating thermocycler block.",
            "Thermocycler block deactivated.",
            "Thermocycler block deactivation cancelled.",
            self._controller.deactivate_block(),
        )
        return await self._controller.get_plate_temperature()

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate_all(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> ThermocyclerStatus:
        """
        Turn off all heating/cooling.

        Returns:
            Full status after deactivation.
        """
        await run_observable(
            status,
            intermediate,
            "Deactivating thermocycler.",
            "Thermocycler deactivated.",
            "Thermocycler deactivation cancelled.",
            self._controller.deactivate_all(),
        )
        return await self._status()

    # ============ Status ============

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_status(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> ThermocyclerStatus:
        """
        Get complete module status.

        Returns:
            Lid temperature, plate temperature, and lid status.
        """
        await run_observable(
            status,
            intermediate,
            "Reading thermocycler status.",
            "Thermocycler status read.",
            "Thermocycler status read cancelled.",
            self._controller.is_connected(),
        )
        return await self._status()

    async def _status(self) -> ThermocyclerStatus:
        """Read full thermocycler status."""
        lid_temp = await self._controller.get_lid_temperature()
        plate_temp = await self._controller.get_plate_temperature()
        lid_status = LidStatus(await self._controller.get_lid_status())

        return ThermocyclerStatus(
            lid_temperature_current=lid_temp.current,
            lid_temperature_target=lid_temp.target,
            plate_temperature_current=plate_temp.current,
            plate_temperature_target=plate_temp.target,
            lid_status=lid_status,
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
            "Reading thermocycler device information.",
            "Thermocycler device information read.",
            "Thermocycler device information read cancelled.",
            self._controller.get_device_info(),
        )
