"""Raw gRPC client shared by Temperature Module GEN2 acceptance tests."""

import grpc.aio

from unitelabs.opentrons_flex.features.temperature import TemperatureControllerStatus
from unitelabs.opentrons_flex.io import DeviceInfo

from .observable import call_observable

_PACKAGE = "sila2.ca.accelerationconsortium.modules.temperaturecontroller.v2"
_SERVICE = f"{_PACKAGE}.TemperatureController"


class TemperatureModuleClient:
    """Call the Temperature Controller through its generated SiLA gRPC surface."""

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
            timeout_s=120.0,
        )
        return next(iter(decoded.values()))

    async def _subscription(self, method: str) -> object:
        stub = self._channel.unary_stream(f"/{_SERVICE}/{method}")
        call = stub(b"")
        response = await call.read()
        call.cancel()
        decoded = await self._protobuf.decode(f"{_PACKAGE}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def _property(self, method: str) -> object:
        stub = self._channel.unary_unary(f"/{_SERVICE}/{method}")
        response = await stub(b"")
        decoded = await self._protobuf.decode(f"{_PACKAGE}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def set_temperature(self, temperature: float) -> TemperatureControllerStatus:
        """Start heating or cooling without waiting for the target."""
        return await self._observable("SetTemperature", {"temperature": temperature})

    async def set_temperature_and_wait(self, temperature: float) -> TemperatureControllerStatus:
        """Start heating or cooling and wait until the module holds the target."""
        return await self._observable("SetTemperatureAndWait", {"temperature": temperature})

    async def deactivate(self) -> TemperatureControllerStatus:
        """Deactivate temperature control."""
        return await self._observable("Deactivate")

    async def get_status(self) -> TemperatureControllerStatus:
        """Read current and target temperature state."""
        return await self._subscription("Subscribe_Status")

    async def get_device_info(self) -> DeviceInfo:
        """Read module identity information."""
        return await self._property("Get_DeviceInfo")
