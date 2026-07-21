"""Raw gRPC client shared by Thermocycler acceptance tests."""

import grpc.aio

from unitelabs.opentrons_flex.features.thermocycler import (
    LidStatus,
    ThermocyclerProfileStep,
    ThermocyclerStatus,
    ThermocyclerTemperature,
)
from unitelabs.opentrons_flex.io import DeviceInfo

from .observable import call_observable

_PACKAGE = "sila2.ca.accelerationconsortium.modules.thermocyclercontroller.v2"
_SERVICE = f"{_PACKAGE}.ThermocyclerController"


class ThermocyclerClient:
    """Call the Thermocycler Controller through its generated SiLA gRPC surface."""

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
            timeout_s=180.0,
        )
        return next(iter(decoded.values()))

    async def open_lid(self) -> LidStatus:
        """Open the Thermocycler lid."""
        return await self._observable("OpenLid")

    async def close_lid(self) -> LidStatus:
        """Close the Thermocycler lid."""
        return await self._observable("CloseLid")

    async def get_lid_status(self) -> LidStatus:
        """Read the Thermocycler lid state."""
        return await self._observable("GetLidStatus")

    async def set_lid_temperature(self, temperature: float) -> ThermocyclerTemperature:
        """Set the lid-heater target."""
        return await self._observable("SetLidTemperature", {"temperature": temperature})

    async def wait_for_lid_temperature(self) -> ThermocyclerTemperature:
        """Wait for the configured lid-heater target."""
        return await self._observable("WaitForLidTemperature")

    async def get_lid_temperature(self) -> ThermocyclerTemperature:
        """Read current and target lid temperatures."""
        return await self._observable("GetLidTemperature")

    async def set_plate_temperature(
        self,
        temperature: float,
        hold_time: float,
        volume: float,
        ramp_rate: float,
    ) -> ThermocyclerTemperature:
        """Set the block-temperature target with explicit SiLA parameters."""
        return await self._observable(
            "SetPlateTemperature",
            {
                "temperature": temperature,
                "hold_time": hold_time,
                "volume": volume,
                "ramp_rate": ramp_rate,
            },
        )

    async def wait_for_plate_temperature(self) -> ThermocyclerTemperature:
        """Wait for the configured block-temperature target."""
        return await self._observable("WaitForPlateTemperature")

    async def get_plate_temperature(self) -> ThermocyclerTemperature:
        """Read current and target block temperatures."""
        return await self._observable("GetPlateTemperature")

    async def execute_profile(
        self,
        steps: list[ThermocyclerProfileStep],
        repetitions: int,
        volume: float,
    ) -> ThermocyclerStatus:
        """Execute a typed block-temperature profile."""
        return await self._observable(
            "ExecuteProfile",
            {"steps": steps, "repetitions": repetitions, "volume": volume},
        )

    async def deactivate_lid(self) -> ThermocyclerTemperature:
        """Deactivate the lid heater."""
        return await self._observable("DeactivateLid")

    async def deactivate_block(self) -> ThermocyclerTemperature:
        """Deactivate block heating and cooling."""
        return await self._observable("DeactivateBlock")

    async def deactivate_all(self) -> ThermocyclerStatus:
        """Deactivate both thermal controllers."""
        return await self._observable("DeactivateAll")

    async def get_status(self) -> ThermocyclerStatus:
        """Read the combined Thermocycler state."""
        return await self._observable("GetStatus")

    async def get_device_info(self) -> DeviceInfo:
        """Read Thermocycler identity information."""
        return await self._observable("GetDeviceInfo")
