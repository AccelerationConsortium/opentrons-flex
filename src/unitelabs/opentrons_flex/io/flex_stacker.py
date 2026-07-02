"""Flex Stacker module IO wrapper."""

import logging

from opentrons.drivers.flex_stacker.types import Direction, LEDColor, LEDPattern, StackerAxis

from ._errors import ModuleOperationError
from ._module_base import ModuleControllerBase
from ._types import FlexStackerLimitSwitches, FlexStackerState

log = logging.getLogger(__name__)


class FlexStackerController(ModuleControllerBase):
    """Controller for the Flex Stacker module."""

    @staticmethod
    def _module_only() -> ModuleOperationError:
        return ModuleOperationError(
            "Flex Stacker control requires an attached module from OT3API.attached_modules. "
            "Start the connector through the Flex OT3API path and attach the module before retrying."
        )

    async def home_all(self, ignore_latch: bool) -> None:
        """Home all stacker axes."""
        if self._module is None:
            raise self._module_only()
        await self._module.home_all(ignore_latch=ignore_latch)

    async def home_axis(self, axis: StackerAxis, direction: Direction) -> bool:
        """Home one stacker axis."""
        if self._module is None:
            raise self._module_only()
        return await self._module.home_axis(axis, direction)

    async def move_axis(self, axis: StackerAxis, direction: Direction, distance: float) -> bool:
        """Move one stacker axis by distance in millimetres."""
        if self._module is None:
            raise self._module_only()
        return await self._module.move_axis(axis, direction, distance)

    async def open_latch(self) -> bool:
        """Open the stacker latch."""
        if self._module is None:
            raise self._module_only()
        return await self._module.open_latch()

    async def close_latch(self) -> bool:
        """Close the stacker latch."""
        if self._module is None:
            raise self._module_only()
        return await self._module.close_latch()

    async def dispense_labware(
        self,
        labware_height: float,
        enforce_hopper_labware_sensing: bool,
        enforce_shuttle_labware_sensing: bool,
    ) -> None:
        """Dispense one labware item from the stacker onto the shuttle."""
        if self._module is None:
            raise self._module_only()
        await self._module.dispense_labware(
            labware_height=labware_height,
            enforce_hopper_lw_sensing=enforce_hopper_labware_sensing,
            enforce_shuttle_lw_sensing=enforce_shuttle_labware_sensing,
        )

    async def store_labware(self, labware_height: float, enforce_shuttle_labware_sensing: bool) -> None:
        """Store one labware item from the shuttle into the stacker."""
        if self._module is None:
            raise self._module_only()
        await self._module.store_labware(
            labware_height=labware_height,
            enforce_shuttle_lw_sensing=enforce_shuttle_labware_sensing,
        )

    async def set_led(
        self,
        power: float,
        color: LEDColor,
        pattern: LEDPattern,
        duration_ms: int,
        repetitions: int,
    ) -> None:
        """Set the stacker LED state."""
        if self._module is None:
            raise self._module_only()
        await self._module.set_led_state(
            power=power,
            color=color,
            pattern=pattern,
            duration=duration_ms if duration_ms > 0 else None,
            reps=repetitions,
        )

    async def deactivate(self) -> None:
        """Stop stacker motors."""
        if self._module is None:
            raise self._module_only()
        await self._module.deactivate()

    async def get_limit_switches(self) -> FlexStackerLimitSwitches:
        """Get stacker limit-switch states."""
        if self._module is None:
            raise self._module_only()
        states = self._module.limit_switch_status
        return FlexStackerLimitSwitches(
            x=states[StackerAxis.X].value,
            z=states[StackerAxis.Z].value,
            latch=states[StackerAxis.L].value,
        )

    async def get_state(self) -> FlexStackerState:
        """Get current stacker state."""
        if self._module is None:
            raise self._module_only()
        error = ""
        live_data = self._module.live_data.get("data")
        if isinstance(live_data, dict):
            error = str(live_data.get("errorDetails") or "")
        return FlexStackerState(
            status=self._module.status.value,
            latch_state=self._module.latch_state.value,
            platform_state=self._module.platform_state.value,
            hopper_door_state=self._module.hopper_door_state.value,
            install_detected=bool(self._module.install_detected),
            initialized=bool(self._module.initialized),
            error_details=error,
        )
