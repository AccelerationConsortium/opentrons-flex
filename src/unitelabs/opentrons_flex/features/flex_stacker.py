"""SiLA2 feature for the Flex Stacker module."""

import enum
import typing
from dataclasses import dataclass

from opentrons.drivers.flex_stacker.types import Direction, LEDColor, LEDPattern, StackerAxis
from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    DeviceInfo,
    FlexStackerController,
)
from ._progress import OperationProgress, run_observable

_LabwareHeightMm = typing.Annotated[float, constraints.MinimalInclusive(4.0), constraints.MaximalInclusive(102.5)]
_DistanceMm = typing.Annotated[float, constraints.MinimalInclusive(0.0)]
_LedPower = typing.Annotated[float, constraints.MinimalInclusive(0.0), constraints.MaximalInclusive(1.0)]
_NonNegativeInteger = typing.Annotated[int, constraints.MinimalInclusive(0)]
_LedRepetitions = typing.Annotated[int, constraints.MinimalInclusive(-1)]


class StackerAxisName(enum.Enum):
    """Flex Stacker axis."""

    X = "X"
    Z = "Z"
    LATCH = "L"


class StackerDirection(enum.Enum):
    """Flex Stacker movement direction."""

    RETRACT = "RETRACT"
    EXTEND = "EXTEND"


class StackerLedColor(enum.Enum):
    """Flex Stacker LED color."""

    WHITE = "WHITE"
    RED = "RED"
    GREEN = "GREEN"
    BLUE = "BLUE"
    YELLOW = "YELLOW"


class StackerLedPattern(enum.Enum):
    """Flex Stacker LED pattern."""

    STATIC = "STATIC"
    FLASH = "FLASH"
    PULSE = "PULSE"
    CONFIRM = "CONFIRM"


class StackerStatus(enum.Enum):
    """Flex Stacker status."""

    IDLE = "idle"
    DISPENSING = "dispensing"
    STORING = "storing"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "StackerStatus":
        return cls.UNKNOWN


class LatchState(enum.Enum):
    """Flex Stacker latch state."""

    CLOSED = "closed"
    OPENED = "opened"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "LatchState":
        return cls.UNKNOWN


class PlatformState(enum.Enum):
    """Flex Stacker platform state."""

    EXTENDED = "extended"
    RETRACTED = "retracted"
    MISSING = "missing"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "PlatformState":
        return cls.UNKNOWN


class HopperDoorState(enum.Enum):
    """Flex Stacker hopper-door state."""

    CLOSED = "closed"
    OPENED = "opened"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "HopperDoorState":
        return cls.UNKNOWN


class AxisState(enum.Enum):
    """Flex Stacker axis state."""

    EXTENDED = "extended"
    RETRACTED = "retracted"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, _value: object) -> "AxisState":
        return cls.UNKNOWN


@dataclass
class FlexStackerStatus:
    """Current Flex Stacker status."""

    status: StackerStatus
    latch_state: LatchState
    platform_state: PlatformState
    hopper_door_state: HopperDoorState
    install_detected: bool
    initialized: bool
    error_details: str


@dataclass
class FlexStackerLimitSwitchStatus:
    """Flex Stacker limit-switch states."""

    x: AxisState
    z: AxisState
    latch: AxisState


class _RawFlexStackerState(typing.Protocol):
    status: str
    latch_state: str
    platform_state: str
    hopper_door_state: str
    install_detected: bool
    initialized: bool
    error_details: str


class _RawFlexStackerLimitSwitches(typing.Protocol):
    x: str
    z: str
    latch: str


def _axis(axis: StackerAxisName) -> StackerAxis:
    return StackerAxis.L if axis is StackerAxisName.LATCH else StackerAxis(axis.value)


def _direction(direction: StackerDirection) -> Direction:
    return Direction.RETRACT if direction is StackerDirection.RETRACT else Direction.EXTEND


def _led_color(color: StackerLedColor) -> LEDColor:
    return LEDColor[color.name]


def _led_pattern(pattern: StackerLedPattern) -> LEDPattern:
    return LEDPattern[pattern.name]


def _status(raw: object) -> FlexStackerStatus:
    state = typing.cast(_RawFlexStackerState, raw)
    return FlexStackerStatus(
        status=StackerStatus(state.status),
        latch_state=LatchState(state.latch_state),
        platform_state=PlatformState(state.platform_state),
        hopper_door_state=HopperDoorState(state.hopper_door_state),
        install_detected=state.install_detected,
        initialized=state.initialized,
        error_details=state.error_details,
    )


def _limit_switches(raw: object) -> FlexStackerLimitSwitchStatus:
    switches = typing.cast(_RawFlexStackerLimitSwitches, raw)
    return FlexStackerLimitSwitchStatus(
        x=AxisState(switches.x),
        z=AxisState(switches.z),
        latch=AxisState(switches.latch),
    )


class FlexStackerFeature(sila.Feature):
    """SiLA2 feature for Flex Stacker labware storage and delivery."""

    def __init__(self, controller: FlexStackerController):
        super().__init__(originator="ca.accelerationconsortium", category="modules")
        self._controller = controller

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def home_all(
        self,
        ignore_latch: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Home all stacker axes.

        Args:
            ignore_latch: Skip latch-closing logic during recovery.

        Yields:
            Update: Current homing progress update.

        Returns:
            Stacker state after homing.
        """
        await run_observable(
            status,
            intermediate,
            "Starting Flex Stacker homing.",
            "Flex Stacker homing completed.",
            "Flex Stacker homing cancelled.",
            self._controller.home_all(ignore_latch=ignore_latch),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def home_axis(
        self,
        axis: StackerAxisName,
        direction: StackerDirection,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> bool:
        """
        Home one stacker axis in the named direction.

        Args:
            axis: Axis to home.
            direction: Homing direction.

        Yields:
            Update: Current homing progress update.

        Returns:
            True when the hardware reports a successful move.
        """
        return await run_observable(
            status,
            intermediate,
            f"Starting Flex Stacker {axis.value} axis home.",
            f"Flex Stacker {axis.value} axis home completed.",
            f"Flex Stacker {axis.value} axis home cancelled.",
            self._controller.home_axis(_axis(axis), _direction(direction)),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def move_axis(
        self,
        axis: StackerAxisName,
        direction: StackerDirection,
        distance: _DistanceMm,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> bool:
        """
        Move one stacker axis by a distance in millimetres.

        Args:
            axis: Axis to move.
            direction: Movement direction.
            distance: Distance in mm.

        Yields:
            Update: Current movement progress update.

        Returns:
            True when the hardware reports a successful move.
        """
        return await run_observable(
            status,
            intermediate,
            f"Starting Flex Stacker {axis.value} axis move.",
            f"Flex Stacker {axis.value} axis move completed.",
            f"Flex Stacker {axis.value} axis move cancelled.",
            self._controller.move_axis(_axis(axis), _direction(direction), distance),
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def open_latch(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Open the stacker latch.

        Yields:
            Update: Current latch movement progress update.

        Returns:
            Stacker state after opening.
        """
        await run_observable(
            status,
            intermediate,
            "Opening Flex Stacker latch.",
            "Flex Stacker latch opened.",
            "Flex Stacker latch open cancelled.",
            self._controller.open_latch(),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def close_latch(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Close the stacker latch.

        Yields:
            Update: Current latch movement progress update.

        Returns:
            Stacker state after closing.
        """
        await run_observable(
            status,
            intermediate,
            "Closing Flex Stacker latch.",
            "Flex Stacker latch closed.",
            "Flex Stacker latch close cancelled.",
            self._controller.close_latch(),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def dispense_labware(
        self,
        labware_height: _LabwareHeightMm,
        enforce_hopper_labware_sensing: bool,
        enforce_shuttle_labware_sensing: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Dispense one labware item from the stacker onto the shuttle.

        Args:
            labware_height: Labware height in mm.
            enforce_hopper_labware_sensing: Require hopper labware detection.
            enforce_shuttle_labware_sensing: Require shuttle labware detection.

        Yields:
            Update: Current dispense progress update.

        Returns:
            Stacker state after dispensing.
        """
        await run_observable(
            status,
            intermediate,
            "Starting Flex Stacker labware dispense.",
            "Flex Stacker labware dispense completed.",
            "Flex Stacker labware dispense cancelled.",
            self._controller.dispense_labware(
                labware_height=labware_height,
                enforce_hopper_labware_sensing=enforce_hopper_labware_sensing,
                enforce_shuttle_labware_sensing=enforce_shuttle_labware_sensing,
            ),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def store_labware(
        self,
        labware_height: _LabwareHeightMm,
        enforce_shuttle_labware_sensing: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Store one labware item from the shuttle into the stacker.

        Args:
            labware_height: Labware height in mm.
            enforce_shuttle_labware_sensing: Require shuttle labware detection.

        Yields:
            Update: Current storage progress update.

        Returns:
            Stacker state after storage.
        """
        await run_observable(
            status,
            intermediate,
            "Starting Flex Stacker labware storage.",
            "Flex Stacker labware storage completed.",
            "Flex Stacker labware storage cancelled.",
            self._controller.store_labware(
                labware_height=labware_height,
                enforce_shuttle_labware_sensing=enforce_shuttle_labware_sensing,
            ),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def set_led(
        self,
        power: _LedPower,
        color: StackerLedColor,
        pattern: StackerLedPattern,
        duration_ms: _NonNegativeInteger,
        repetitions: _LedRepetitions,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Set the stacker LED state.

        Args:
            power: LED power from 0.0 to 1.0.
            color: LED color.
            pattern: LED pattern.
            duration_ms: Pattern duration in ms, or 0 for module default.
            repetitions: Repetition count; -1 means repeat indefinitely.

        Yields:
            Update: Current LED command progress update.

        Returns:
            Stacker state after setting LEDs.
        """
        await run_observable(
            status,
            intermediate,
            "Setting Flex Stacker LED state.",
            "Flex Stacker LED state set.",
            "Flex Stacker LED command cancelled.",
            self._controller.set_led(
                power=power,
                color=_led_color(color),
                pattern=_led_pattern(pattern),
                duration_ms=duration_ms,
                repetitions=repetitions,
            ),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Stop stacker motors.

        Yields:
            Update: Current deactivation progress update.

        Returns:
            Stacker state after deactivation.
        """
        await run_observable(
            status,
            intermediate,
            "Deactivating Flex Stacker.",
            "Flex Stacker deactivated.",
            "Flex Stacker deactivation cancelled.",
            self._controller.deactivate(),
        )
        return _status(await self._controller.get_state())

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_limit_switches(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerLimitSwitchStatus:
        """
        Get stacker limit-switch states.

        Yields:
            Update: Current limit-switch read progress update.

        Returns:
            X, Z, and latch axis states.
        """
        return _limit_switches(
            await run_observable(
                status,
                intermediate,
                "Reading Flex Stacker limit switches.",
                "Flex Stacker limit switches read.",
                "Flex Stacker limit-switch read cancelled.",
                self._controller.get_limit_switches(),
            )
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_status(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """
        Get Flex Stacker state.

        Yields:
            Update: Current status-read progress update.

        Returns:
            Stacker state.
        """
        return _status(
            await run_observable(
                status,
                intermediate,
                "Reading Flex Stacker status.",
                "Flex Stacker status read.",
                "Flex Stacker status read cancelled.",
                self._controller.get_state(),
            )
        )

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def get_device_info(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> DeviceInfo:
        """
        Get Flex Stacker device information.

        Yields:
            Update: Current device-info read progress update.

        Returns:
            Serial number, model, and firmware version.
        """
        return await run_observable(
            status,
            intermediate,
            "Reading Flex Stacker device information.",
            "Flex Stacker device information read.",
            "Flex Stacker device information read cancelled.",
            self._controller.get_device_info(),
        )
