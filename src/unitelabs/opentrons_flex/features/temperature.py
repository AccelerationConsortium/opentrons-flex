"""SiLA 2 feature for Temperature Module GEN2 heating and cooling."""

import asyncio
import enum
import logging
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    COMMON_MODULE_ERRORS,
    DeviceInfo,
    InvalidTemperatureTargetError,
    TemperatureModuleController,
    TemperatureModuleState,
)
from ._progress import OperationProgress, run_observable
from ._subscriptions import stream_changes

_CELSIUS = constraints.Unit(
    "°C",
    [constraints.Unit.Component(constraints.Unit.SI.KELVIN)],
    offset=273.15,
)
_TargetTemperature = typing.Annotated[
    float,
    constraints.MinimalInclusive(4.0),
    constraints.MaximalInclusive(95.0),
    _CELSIUS,
]
_TemperatureReading = typing.Annotated[float, _CELSIUS]

log = logging.getLogger(__name__)
_TEMPERATURE_ERRORS = (*COMMON_MODULE_ERRORS, InvalidTemperatureTargetError)


class TemperatureControlStatus(enum.Enum):
    """Temperature Module operating state."""

    IDLE = "idle"
    HEATING = "heating"
    COOLING = "cooling"
    HOLDING = "holding at target"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "TemperatureControlStatus":
        log.warning("Unrecognized Temperature Module status %r; reporting UNKNOWN", value)
        return cls.UNKNOWN


@dataclass
class TemperatureControllerStatus:
    """Current, target, and operating state of a Temperature Module GEN2."""

    status: TemperatureControlStatus
    current_temperature: _TemperatureReading
    target_temperature: _TemperatureReading
    target_active: bool


def _status(raw: TemperatureModuleState) -> TemperatureControllerStatus:
    return TemperatureControllerStatus(
        status=TemperatureControlStatus(raw.status),
        current_temperature=raw.current_temperature,
        target_temperature=raw.target_temperature if raw.target_temperature is not None else 0.0,
        target_active=raw.target_temperature is not None,
    )


class TemperatureModuleFeature(sila.Feature):
    """Heat, cool, and hold samples with a Temperature Module GEN2."""

    def __init__(self, controller: TemperatureModuleController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="modules",
            identifier="TemperatureController",
            name="Temperature Controller",
            version="2.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=_TEMPERATURE_ERRORS)
    async def set_temperature(
        self,
        temperature: _TargetTemperature,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TemperatureControllerStatus:
        """Start heating or cooling toward a target and return without waiting."""
        await run_observable(
            status,
            intermediate,
            f"Starting Temperature Module control toward {temperature} °C.",
            "Temperature Module target accepted.",
            "Temperature Module target command cancelled.",
            self._controller.set_temperature(temperature),
        )
        return _status(self._controller.state)

    @sila.ObservableCommand(errors=_TEMPERATURE_ERRORS)
    async def set_temperature_and_wait(
        self,
        temperature: _TargetTemperature,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TemperatureControllerStatus:
        """Set a target and wait until the module reports that it is holding it."""
        try:
            await run_observable(
                status,
                intermediate,
                f"Heating or cooling toward {temperature} °C.",
                "Temperature Module reached and is holding the target.",
                "Temperature Module wait cancelled; temperature control is being deactivated.",
                self._controller.set_temperature_and_wait(temperature),
            )
        except asyncio.CancelledError:
            await self._controller.deactivate()
            raise
        return _status(self._controller.state)

    @sila.ObservableCommand(errors=COMMON_MODULE_ERRORS)
    async def deactivate(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TemperatureControllerStatus:
        """Stop heating or cooling and turn off the module fan."""
        await run_observable(
            status,
            intermediate,
            "Deactivating Temperature Module control.",
            "Temperature Module deactivated.",
            "Temperature Module deactivation cancelled.",
            self._controller.deactivate(),
        )
        return _status(self._controller.state)

    @sila.ObservableProperty(errors=COMMON_MODULE_ERRORS)
    async def subscribe_status(self) -> sila.Stream[TemperatureControllerStatus]:
        """Subscribe to temperature, target, and operating-state changes."""
        async for value in stream_changes(lambda: _status(self._controller.state)):
            yield value

    @sila.UnobservableProperty(errors=COMMON_MODULE_ERRORS)
    def device_info(self) -> DeviceInfo:
        """Return the attached module serial number, model, and firmware version."""
        return self._controller.device_info


__all__ = [
    "TemperatureControlStatus",
    "TemperatureControllerStatus",
    "TemperatureModuleFeature",
]
