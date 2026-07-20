"""SiLA 2 feature for the Flex Stacker module."""

import asyncio
import enum
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    DeviceInfo,
    FlexStackerController,
    FlexStackerAxis as StackerAxisName,
    FlexStackerDirection as StackerDirection,
    FlexStackerLedColor as StackerLedColor,
    FlexStackerLedPattern as StackerLedPattern,
    InvalidStackerConfigurationError,
    StackerMovementOutOfRangeError,
    StackerNotReadyError,
)
from ._progress import OperationProgress, run_observable
from ._subscriptions import stream_changes

_MILLIMETRE = constraints.Unit(
    "mm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=0.001,
)
_SECOND = constraints.Unit(
    "s",
    [constraints.Unit.Component(constraints.Unit.SI.SECOND)],
)
_DIMENSIONLESS = constraints.Unit(
    "1",
    [constraints.Unit.Component(constraints.Unit.SI.DIMENSIONLESS)],
)
_LabwareHeight = typing.Annotated[
    float,
    constraints.MinimalInclusive(4.0),
    constraints.MaximalInclusive(102.5),
    _MILLIMETRE,
]
_Distance = typing.Annotated[
    float,
    constraints.MinimalInclusive(0.0),
    constraints.MaximalInclusive(194.0),
    _MILLIMETRE,
]
_LedPower = typing.Annotated[
    float,
    constraints.MinimalInclusive(0.0),
    constraints.MaximalInclusive(1.0),
    _DIMENSIONLESS,
]
_Duration = typing.Annotated[
    float,
    constraints.MinimalInclusive(0.0),
    constraints.MaximalInclusive(10.0),
    _SECOND,
]
_LedRepetitions = typing.Annotated[
    int,
    constraints.MinimalInclusive(-1),
    constraints.MaximalInclusive(10),
]
_STACKER_ERRORS = (
    *COMMON_MODULE_ERRORS,
    InvalidStackerConfigurationError,
    StackerMovementOutOfRangeError,
    StackerNotReadyError,
)


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
    """Flex Stacker shuttle platform state."""

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
    """Flex Stacker limit-switch state."""

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
    recovery_required: bool
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
    recovery_required: bool
    error_details: str


class _RawFlexStackerLimitSwitches(typing.Protocol):
    x: str
    z: str
    latch: str


def _status(raw: object) -> FlexStackerStatus:
    state = typing.cast(_RawFlexStackerState, raw)
    return FlexStackerStatus(
        status=StackerStatus(state.status),
        latch_state=LatchState(state.latch_state),
        platform_state=PlatformState(state.platform_state),
        hopper_door_state=HopperDoorState(state.hopper_door_state),
        install_detected=state.install_detected,
        initialized=state.initialized,
        recovery_required=state.recovery_required,
        error_details=state.error_details,
    )


def _limit_switches(raw: object) -> FlexStackerLimitSwitchStatus:
    switches = typing.cast(_RawFlexStackerLimitSwitches, raw)
    return FlexStackerLimitSwitchStatus(
        x=AxisState(switches.x),
        z=AxisState(switches.z),
        latch=AxisState(switches.latch),
    )


class _FlexStackerCommands:
    """Shared observable-command execution for both Stacker concerns."""

    _controller: FlexStackerController

    async def _run_and_status(
        self,
        action: typing.Awaitable[None],
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
        starting: str,
        completed: str,
        cancelled: str,
    ) -> FlexStackerStatus:
        try:
            await run_observable(status, intermediate, starting, completed, cancelled, action)
        except asyncio.CancelledError:
            # Cancelling a gRPC await does not guarantee that module firmware
            # stops its current motor sequence, so issue an explicit motor stop.
            await self._controller.halt_for_cancellation()
            raise
        return _status(self._controller.state)


class FlexStackerFeature(_FlexStackerCommands, sila.Feature):
    """Retrieve and store labware with a Flex Stacker through SiLA 2."""

    def __init__(self, controller: FlexStackerController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="modules",
            identifier="FlexStackerController",
            name="Flex Stacker Controller",
            version="2.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def retrieve_labware(
        self,
        labware_height: _LabwareHeight,
        enforce_hopper_labware_sensing: bool,
        enforce_shuttle_labware_sensing: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Retrieve the bottom labware item from the Stacker onto its shuttle."""
        return await self._run_and_status(
            self._controller.retrieve_labware(
                labware_height=labware_height,
                enforce_hopper_labware_sensing=enforce_hopper_labware_sensing,
                enforce_shuttle_labware_sensing=enforce_shuttle_labware_sensing,
            ),
            status=status,
            intermediate=intermediate,
            starting="Starting Flex Stacker labware retrieval.",
            completed="Flex Stacker labware retrieval completed.",
            cancelled="Flex Stacker labware retrieval cancelled.",
        )

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def store_labware(
        self,
        labware_height: _LabwareHeight,
        enforce_shuttle_labware_sensing: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Store the shuttle labware at the bottom of the Stacker."""
        return await self._run_and_status(
            self._controller.store_labware(
                labware_height=labware_height,
                enforce_shuttle_labware_sensing=enforce_shuttle_labware_sensing,
            ),
            status=status,
            intermediate=intermediate,
            starting="Starting Flex Stacker labware storage.",
            completed="Flex Stacker labware storage completed.",
            cancelled="Flex Stacker labware storage cancelled.",
        )

    @sila.ObservableProperty(errors=_STACKER_ERRORS)
    async def subscribe_status(self) -> sila.Stream[FlexStackerStatus]:
        """Subscribe to operational and recovery state changes."""
        async for value in stream_changes(lambda: _status(self._controller.state)):
            yield value

    @sila.UnobservableProperty(errors=COMMON_MODULE_ERRORS)
    def device_info(self) -> DeviceInfo:
        """Return the attached Stacker serial number, model, and firmware version."""
        return self._controller.device_info


class FlexStackerMaintenanceFeature(_FlexStackerCommands, sila.Feature):
    """Perform service, recovery, latch, axis, and LED operations on a Flex Stacker."""

    def __init__(self, controller: FlexStackerController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="modules",
            identifier="FlexStackerMaintenanceController",
            name="Flex Stacker Maintenance Controller",
            version="1.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def home_all(
        self,
        ignore_latch: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Home all Stacker axes; ignore_latch is intended only for recovery."""
        return await self._run_and_status(
            self._controller.home_all(ignore_latch=ignore_latch),
            status=status,
            intermediate=intermediate,
            starting="Starting Flex Stacker homing.",
            completed="Flex Stacker homing completed.",
            cancelled="Flex Stacker homing cancelled.",
        )

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def home_axis(
        self,
        axis: StackerAxisName,
        direction: StackerDirection,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Home one maintenance axis in the named direction."""
        return await self._run_and_status(
            self._controller.home_axis(axis, direction),
            status=status,
            intermediate=intermediate,
            starting=f"Starting Flex Stacker {axis.value} axis home.",
            completed=f"Flex Stacker {axis.value} axis home completed.",
            cancelled=f"Flex Stacker {axis.value} axis home cancelled.",
        )

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def move_axis(
        self,
        axis: StackerAxisName,
        direction: StackerDirection,
        distance: _Distance,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Move one maintenance axis by a constrained distance."""
        return await self._run_and_status(
            self._controller.move_axis(axis, direction, distance),
            status=status,
            intermediate=intermediate,
            starting=f"Starting Flex Stacker {axis.value} axis move.",
            completed=f"Flex Stacker {axis.value} axis move completed.",
            cancelled=f"Flex Stacker {axis.value} axis move cancelled.",
        )

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def open_latch(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Open the Stacker shuttle latch."""
        return await self._run_and_status(
            self._controller.open_latch(),
            status=status,
            intermediate=intermediate,
            starting="Opening the Flex Stacker latch.",
            completed="Flex Stacker latch opened.",
            cancelled="Flex Stacker latch opening cancelled.",
        )

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def close_latch(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Close the Stacker shuttle latch."""
        return await self._run_and_status(
            self._controller.close_latch(),
            status=status,
            intermediate=intermediate,
            starting="Closing the Flex Stacker latch.",
            completed="Flex Stacker latch closed.",
            cancelled="Flex Stacker latch closing cancelled.",
        )

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def set_led(
        self,
        power: _LedPower,
        color: StackerLedColor,
        pattern: StackerLedPattern,
        duration: _Duration,
        repetitions: _LedRepetitions,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Set the Stacker status LED; duration is expressed in seconds."""
        return await self._run_and_status(
            self._controller.set_led(
                power=power,
                color=color,
                pattern=pattern,
                duration=duration,
                repetitions=repetitions,
            ),
            status=status,
            intermediate=intermediate,
            starting="Setting the Flex Stacker LED state.",
            completed="Flex Stacker LED state set.",
            cancelled="Flex Stacker LED command cancelled.",
        )

    @sila.ObservableCommand(errors=_STACKER_ERRORS)
    async def deactivate(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> FlexStackerStatus:
        """Stop Stacker motors."""
        await run_observable(
            status,
            intermediate,
            "Deactivating the Flex Stacker.",
            "Flex Stacker deactivated.",
            "Flex Stacker deactivation cancelled.",
            self._controller.deactivate(),
        )
        return _status(self._controller.state)

    @sila.ObservableProperty(errors=_STACKER_ERRORS)
    async def subscribe_status(self) -> sila.Stream[FlexStackerStatus]:
        """Subscribe to Stacker state changes during maintenance and recovery."""
        async for value in stream_changes(lambda: _status(self._controller.state)):
            yield value

    @sila.ObservableProperty(errors=_STACKER_ERRORS)
    async def subscribe_limit_switch_status(self) -> sila.Stream[FlexStackerLimitSwitchStatus]:
        """Subscribe to X, Z, and latch limit-switch changes."""
        async for value in stream_changes(lambda: _limit_switches(self._controller.limit_switches)):
            yield value

    @sila.UnobservableProperty(errors=COMMON_MODULE_ERRORS)
    def device_info(self) -> DeviceInfo:
        """Return the attached Stacker serial number, model, and firmware version."""
        return self._controller.device_info
