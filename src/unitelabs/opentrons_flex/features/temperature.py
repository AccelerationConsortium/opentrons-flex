"""SiLA2 feature for Temperature Module control."""

import typing

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    DeviceInfo,
    TemperatureModuleController,
    Temperature,
)
from ._progress import OperationProgress, run_observable

# Sourced from opentrons: tempdeck QA-tested range 4-95 C
# (opentrons/hardware_control/modules/tempdeck.py, protocol_api/module_contexts.py).
_TempCelsius = typing.Annotated[float, constraints.MinimalInclusive(4.0), constraints.MaximalInclusive(95.0)]


class TemperatureModuleFeature(sila.Feature):
    """
    SiLA2 feature for Temperature Module.

    Provides commands for temperature control of samples on the deck.
    Temperature range is typically 4-95°C.
    """

    def __init__(self, controller: TemperatureModuleController):
        """
        Initialize the temperature module feature.

        Args:
            controller: The TemperatureModuleController instance.
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
            temperature_celsius: Target temperature in Celsius (valid range 4-95 C).

        Returns:
            Current and target temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Setting temperature module target to {temperature_celsius} C.",
            "Temperature module target set.",
            "Temperature module target command cancelled.",
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
        Wait until the module reaches a target temperature.

        Args:
            temperature_celsius: Temperature in Celsius to wait for.

        Yields:
            Update: Current temperature wait progress update.

        Returns:
            Current and target temperature.
        """
        await run_observable(
            status,
            intermediate,
            f"Waiting for temperature module to reach {temperature_celsius} C.",
            "Temperature module reached target temperature.",
            "Temperature module temperature wait cancelled.",
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
            "Reading temperature module temperature.",
            "Temperature module temperature read.",
            "Temperature module temperature read cancelled.",
            self._controller.get_temperature(),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Temperature:
        """
        Turn off temperature control.

        Returns:
            Current temperature after deactivation.
        """
        await run_observable(
            status,
            intermediate,
            "Deactivating temperature module.",
            "Temperature module deactivated.",
            "Temperature module deactivation cancelled.",
            self._controller.deactivate(),
        )
        return await self._controller.get_temperature()

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
            "Reading temperature module device information.",
            "Temperature module device information read.",
            "Temperature module device information read cancelled.",
            self._controller.get_device_info(),
        )
