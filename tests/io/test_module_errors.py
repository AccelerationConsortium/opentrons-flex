"""Tests for module error translation: opentrons exceptions -> defined errors.

The controllers wrap driver/module calls so the features see one stable set of
defined errors regardless of which opentrons exception (comm vs module-specific)
was actually raised.
"""

import pytest

from opentrons.drivers.asyncio.communication.errors import ErrorResponse, NoResponse
from opentrons.drivers.temp_deck.driver import TempDeckError

from unitelabs.opentrons_flex.io import (
    ModuleNotRespondingError,
    ModuleOperationError,
    TemperatureModuleController,
)


class _RaisingTempDriver:
    """Fake temp-deck driver whose calls raise a preset exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def set_temperature(self, celsius: float) -> None:
        raise self._exc


@pytest.mark.asyncio
async def test_comm_error_becomes_not_responding() -> None:
    ctrl = TemperatureModuleController(driver=_RaisingTempDriver(NoResponse(port="/dev/x", command="M104")))
    with pytest.raises(ModuleNotRespondingError):
        await ctrl.set_temperature(50.0)


@pytest.mark.asyncio
async def test_firmware_error_becomes_operation_error_and_preserves_response() -> None:
    """A responding module's firmware rejection is not mislabeled as a disconnect."""
    firmware_error = ErrorResponse(port="/dev/x", response="ERR001: latch open", command="M3")
    ctrl = TemperatureModuleController(driver=_RaisingTempDriver(firmware_error))

    with pytest.raises(ModuleOperationError, match="ERR001: latch open"):
        await ctrl.set_temperature(50.0)


@pytest.mark.asyncio
async def test_module_error_becomes_operation_error() -> None:
    ctrl = TemperatureModuleController(driver=_RaisingTempDriver(TempDeckError("over temperature")))
    with pytest.raises(ModuleOperationError):
        await ctrl.set_temperature(50.0)
