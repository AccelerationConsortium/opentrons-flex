"""Flex Stacker module IO wrapper."""

import asyncio
import math
import typing

from opentrons.drivers.flex_stacker.types import (
    Direction as OpentronsDirection,
    LEDColor as OpentronsLedColor,
    LEDPattern as OpentronsLedPattern,
    StackerAxis as OpentronsStackerAxis,
)

from ._errors import (
    InvalidStackerConfigurationError,
    ModuleOperationError,
    StackerMovementOutOfRangeError,
    StackerNotReadyError,
)
from ._module_base import ModuleControllerBase
from ._types import (
    DeviceInfo,
    FlexStackerAxis,
    FlexStackerDirection,
    FlexStackerLedColor,
    FlexStackerLedPattern,
    FlexStackerLimitSwitches,
    FlexStackerState,
)
from .recovery_state import FlexStackerRecoveryState, stacker_recovery_state_for

_AXIS_TRAVEL_MILLIMETRES = {
    FlexStackerAxis.X: 194.0,
    FlexStackerAxis.Z: 139.5,
    FlexStackerAxis.LATCH: 22.0,
}
_MIN_LABWARE_HEIGHT_MILLIMETRES = 4.0
_MAX_LABWARE_HEIGHT_MILLIMETRES = 102.5
_MIN_LED_DURATION_SECONDS = 0.025
_MAX_LED_DURATION_SECONDS = 10.0
_MAX_LED_REPETITIONS = 10


def _driver_axis(axis: FlexStackerAxis) -> OpentronsStackerAxis:
    return OpentronsStackerAxis.L if axis is FlexStackerAxis.LATCH else OpentronsStackerAxis(axis.value)


def _driver_direction(direction: FlexStackerDirection) -> OpentronsDirection:
    return OpentronsDirection.RETRACT if direction is FlexStackerDirection.RETRACT else OpentronsDirection.EXTEND


class FlexStackerController(ModuleControllerBase):
    """Control an attached Flex Stacker through the shared OT3API module."""

    def __init__(
        self,
        driver: object = None,
        module: object = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        super().__init__(driver=driver, module=module, lock=lock)
        self._recovery_state: FlexStackerRecoveryState | None = (
            stacker_recovery_state_for(module) if module is not None else None
        )

    @staticmethod
    def _module_only() -> ModuleOperationError:
        return ModuleOperationError(
            "Flex Stacker control requires an attached module from OT3API.attached_modules. "
            "Start the connector through the Flex OT3API path and attach the module before retrying."
        )

    def _require_module(self) -> object:
        if self._module is None:
            raise self._module_only()
        return self._module

    def _require_recovery_state(self) -> FlexStackerRecoveryState:
        module = self._require_module()
        if self._recovery_state is None:
            self._recovery_state = stacker_recovery_state_for(module)
        return self._recovery_state

    @property
    def limit_switches(self) -> FlexStackerLimitSwitches:
        """Return the latest limit-switch states maintained by the module poller."""
        module = self._require_module()
        states = module.limit_switch_status
        return FlexStackerLimitSwitches(
            x=states[OpentronsStackerAxis.X].value,
            z=states[OpentronsStackerAxis.Z].value,
            latch=states[OpentronsStackerAxis.L].value,
        )

    @property
    def state(self) -> FlexStackerState:
        """Return the latest operational state maintained by the module poller."""
        module = self._require_module()
        error = ""
        live_data = module.live_data.get("data")
        if isinstance(live_data, dict):
            error = str(live_data.get("errorDetails") or "")
        return FlexStackerState(
            status=module.status.value,
            latch_state=module.latch_state.value,
            platform_state=module.platform_state.value,
            hopper_door_state=module.hopper_door_state.value,
            install_detected=bool(module.install_detected),
            initialized=bool(module.initialized),
            recovery_required=self._require_recovery_state().recovery_required(module),
            error_details=error,
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the attached Stacker identity."""
        module = self._require_module()
        return DeviceInfo.from_dict(dict(module.device_info))

    def _assert_ready_for_labware(self) -> None:
        state = self.state
        if state.recovery_required:
            message = (
                "The previous Stacker movement was interrupted or failed. "
                "Clear any obstruction, run HomeAll, and retry."
            )
            raise StackerNotReadyError(message)
        if not state.install_detected:
            message = "The Stacker installation sensor is not active. Re-seat the module and retry."
            raise StackerNotReadyError(message)
        if not state.initialized:
            message = "The Stacker is not initialized. Home the Stacker and retry."
            raise StackerNotReadyError(message)
        if state.hopper_door_state != "closed":
            message = "Close the Stacker hopper door before moving labware."
            raise StackerNotReadyError(message)

    @staticmethod
    def _validate_labware_height(labware_height: float) -> None:
        if not math.isfinite(labware_height) or not (
            _MIN_LABWARE_HEIGHT_MILLIMETRES <= labware_height <= _MAX_LABWARE_HEIGHT_MILLIMETRES
        ):
            message = (
                "Labware height must be between "
                f"{_MIN_LABWARE_HEIGHT_MILLIMETRES} and {_MAX_LABWARE_HEIGHT_MILLIMETRES} mm; "
                f"received {labware_height!r}."
            )
            raise InvalidStackerConfigurationError(message)

    @staticmethod
    def _validate_led(power: float, duration: float, repetitions: int) -> None:
        if not math.isfinite(power) or not 0.0 <= power <= 1.0:
            message = f"LED power must be between 0.0 and 1.0; received {power!r}."
            raise InvalidStackerConfigurationError(message)
        duration_is_valid = duration == 0.0 or (
            math.isfinite(duration) and _MIN_LED_DURATION_SECONDS <= duration <= _MAX_LED_DURATION_SECONDS
        )
        if not duration_is_valid:
            message = (
                "LED duration must be 0 for the module default or between "
                f"{_MIN_LED_DURATION_SECONDS} and {_MAX_LED_DURATION_SECONDS} seconds; "
                f"received {duration!r}."
            )
            raise InvalidStackerConfigurationError(message)
        if not -1 <= repetitions <= _MAX_LED_REPETITIONS:
            message = (
                f"LED repetitions must be -1 (continuous) or 0 through {_MAX_LED_REPETITIONS}; received {repetitions}."
            )
            raise InvalidStackerConfigurationError(message)

    @staticmethod
    def _require_success(result: bool, action: str) -> None:
        if not result:
            message = (
                f"The Flex Stacker did not complete {action}. Clear obstructions, home the affected axis, and retry."
            )
            raise ModuleOperationError(message)

    async def _run_motion(self, action: typing.Awaitable[bool], description: str) -> None:
        try:
            result = await action
        except (Exception, asyncio.CancelledError):
            self._require_recovery_state().require_full_home()
            raise
        if not result:
            self._require_recovery_state().require_full_home()
        self._require_success(result, description)

    async def home_all(self, ignore_latch: bool) -> None:
        """Home all Stacker axes."""
        module = self._require_module()
        try:
            await module.home_all(ignore_latch=ignore_latch)
        except (Exception, asyncio.CancelledError):
            self._require_recovery_state().require_full_home()
            raise
        if not ignore_latch:
            self._require_recovery_state().mark_fully_homed()

    async def home_axis(self, axis: FlexStackerAxis, direction: FlexStackerDirection) -> None:
        """Home one Stacker axis and convert a false driver result into a defined error."""
        module = self._require_module()
        await self._run_motion(
            module.home_axis(_driver_axis(axis), _driver_direction(direction)),
            f"the {axis.value} axis home",
        )

    async def move_axis(self, axis: FlexStackerAxis, direction: FlexStackerDirection, distance: float) -> None:
        """Move one Stacker axis by distance in millimetres."""
        module = self._require_module()
        maximum = _AXIS_TRAVEL_MILLIMETRES[axis]
        if not math.isfinite(distance) or not 0.0 <= distance <= maximum:
            message = f"The {axis.value} axis accepts distances from 0 to {maximum} mm; received {distance!r} mm."
            raise StackerMovementOutOfRangeError(message)
        await self._run_motion(
            module.move_axis(_driver_axis(axis), _driver_direction(direction), distance),
            f"the {axis.value} axis move",
        )

    async def open_latch(self) -> None:
        """Open the Stacker latch."""
        module = self._require_module()
        await self._run_motion(module.open_latch(), "the latch opening")

    async def close_latch(self) -> None:
        """Close the Stacker latch."""
        module = self._require_module()
        await self._run_motion(module.close_latch(), "the latch closing")

    async def retrieve_labware(
        self,
        labware_height: float,
        enforce_hopper_labware_sensing: bool,
        enforce_shuttle_labware_sensing: bool,
    ) -> None:
        """Retrieve one labware item from the Stacker onto its shuttle."""
        module = self._require_module()
        self._validate_labware_height(labware_height)
        self._assert_ready_for_labware()
        try:
            await module.dispense_labware(
                labware_height=labware_height,
                enforce_hopper_lw_sensing=enforce_hopper_labware_sensing,
                enforce_shuttle_lw_sensing=enforce_shuttle_labware_sensing,
            )
        except (Exception, asyncio.CancelledError):
            self._require_recovery_state().require_full_home()
            raise

    async def store_labware(self, labware_height: float, enforce_shuttle_labware_sensing: bool) -> None:
        """Store one labware item from the shuttle in the Stacker."""
        module = self._require_module()
        self._validate_labware_height(labware_height)
        self._assert_ready_for_labware()
        try:
            await module.store_labware(
                labware_height=labware_height,
                enforce_shuttle_lw_sensing=enforce_shuttle_labware_sensing,
            )
        except (Exception, asyncio.CancelledError):
            self._require_recovery_state().require_full_home()
            raise

    async def set_led(
        self,
        power: float,
        color: FlexStackerLedColor,
        pattern: FlexStackerLedPattern,
        duration: float,
        repetitions: int,
    ) -> None:
        """Set the Stacker LED state using a duration expressed in seconds."""
        module = self._require_module()
        self._validate_led(power, duration, repetitions)
        duration_milliseconds = round(duration * 1000) if duration > 0 else None
        await module.set_led_state(
            power=power,
            color=OpentronsLedColor[color.name],
            pattern=OpentronsLedPattern[pattern.name],
            duration=duration_milliseconds,
            reps=repetitions,
        )

    async def deactivate(self) -> None:
        """Stop Stacker motors."""
        module = self._require_module()
        await module.deactivate()

    async def halt_for_cancellation(self) -> None:
        """Stop motors after cancellation and require a full home before labware motion."""
        self._require_recovery_state().require_full_home()
        module = self._require_module()
        await module.deactivate()
