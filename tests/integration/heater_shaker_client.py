"""Shared raw-gRPC client for Heater-Shaker integration acceptance tests."""

import grpc.aio

from unitelabs.opentrons_flex.features.heater_shaker import (
    HeaterShakerSpeed,
    HeaterShakerStatus,
    HeaterShakerTemperature,
    LatchStatus,
)
from unitelabs.opentrons_flex.io import DeviceInfo

from .observable import call_observable

_PACKAGE = "sila2.ca.accelerationconsortium.modules.heatershakercontroller.v3"
_SERVICE = f"{_PACKAGE}.HeaterShakerController"


class HeaterShakerClient:
    """Call the Heater-Shaker SiLA feature through its generated gRPC surface."""

    def __init__(self, channel: grpc.aio.Channel, protobuf: object) -> None:
        self._channel = channel
        self._protobuf = protobuf

    async def _observable(self, method: str, parameters: dict | None = None) -> object:
        decoded = await call_observable(
            self._channel,
            self._protobuf,
            _SERVICE,
            _PACKAGE,
            method,
            parameters,
            timeout_s=30.0,
        )
        return next(iter(decoded.values()))

    async def close_latch(self) -> LatchStatus:
        """Close the labware latch."""
        return await self._observable("CloseLatch")

    async def open_latch(self) -> LatchStatus:
        """Open the labware latch."""
        return await self._observable("OpenLatch")

    async def set_temperature(self, temperature: float) -> HeaterShakerTemperature:
        """Set the temperature target."""
        return await self._observable("SetTemperature", {"temperature": temperature})

    async def wait_for_temperature(self, temperature: float) -> HeaterShakerTemperature:
        """Wait for the temperature target."""
        return await self._observable("WaitForTemperature", {"temperature": temperature})

    async def get_temperature(self) -> HeaterShakerTemperature:
        """Read the current and target temperature."""
        return await self._observable("GetTemperature")

    async def deactivate_heater(self) -> HeaterShakerTemperature:
        """Deactivate the heater."""
        return await self._observable("DeactivateHeater")

    async def set_speed(self, rotation_speed: int) -> HeaterShakerSpeed:
        """Set the active shaking speed."""
        return await self._observable("SetSpeed", {"speed": rotation_speed})

    async def get_speed(self) -> HeaterShakerSpeed:
        """Read the current and target shaking speed."""
        return await self._observable("GetSpeed")

    async def stop_shaking(self) -> HeaterShakerSpeed:
        """Stop and home the shaker."""
        return await self._observable("StopShaking")

    async def get_status(self) -> HeaterShakerStatus:
        """Read combined Heater-Shaker status."""
        return await self._observable("GetStatus")

    async def get_latch_status(self) -> LatchStatus:
        """Read the labware latch state."""
        return await self._observable("GetLatchStatus")

    async def get_device_info(self) -> DeviceInfo:
        """Read module identity information."""
        return await self._observable("GetDeviceInfo")
