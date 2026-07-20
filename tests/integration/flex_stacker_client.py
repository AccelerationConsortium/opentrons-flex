"""Raw gRPC client shared by Flex Stacker acceptance tests."""

import grpc.aio

from unitelabs.opentrons_flex.features.flex_stacker import (
    FlexStackerLimitSwitchStatus,
    FlexStackerStatus,
    StackerLedColor,
    StackerLedPattern,
)
from unitelabs.opentrons_flex.io import DeviceInfo

from .observable import call_observable

_PACKAGE = "sila2.ca.accelerationconsortium.modules.flexstackercontroller.v2"
_SERVICE = f"{_PACKAGE}.FlexStackerController"
_MAINTENANCE_PACKAGE = "sila2.ca.accelerationconsortium.modules.flexstackermaintenancecontroller.v1"
_MAINTENANCE_SERVICE = f"{_MAINTENANCE_PACKAGE}.FlexStackerMaintenanceController"


class FlexStackerClient:
    """Call the Flex Stacker SiLA feature through its generated gRPC surface."""

    def __init__(self, channel: grpc.aio.Channel, protobuf: object) -> None:
        self._channel = channel
        self._protobuf = protobuf

    async def _observable(
        self,
        method: str,
        parameters: dict | None = None,
        *,
        maintenance: bool = False,
    ) -> object:
        package = _MAINTENANCE_PACKAGE if maintenance else _PACKAGE
        service = _MAINTENANCE_SERVICE if maintenance else _SERVICE
        decoded = await call_observable(
            self._channel,
            self._protobuf,
            service,
            package,
            method,
            parameters,
            timeout_s=60.0,
        )
        return next(iter(decoded.values()))

    async def _property(self, method: str) -> object:
        stub = self._channel.unary_unary(f"/{_SERVICE}/{method}")
        response = await stub(b"")
        decoded = await self._protobuf.decode(f"{_PACKAGE}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def _subscription(self, method: str, *, maintenance: bool = False) -> object:
        package = _MAINTENANCE_PACKAGE if maintenance else _PACKAGE
        service = _MAINTENANCE_SERVICE if maintenance else _SERVICE
        stub = self._channel.unary_stream(f"/{service}/{method}")
        call = stub(b"")
        response = await call.read()
        call.cancel()
        decoded = await self._protobuf.decode(f"{package}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def home_all(self, ignore_latch: bool) -> FlexStackerStatus:
        return await self._observable("HomeAll", {"ignore_latch": ignore_latch}, maintenance=True)

    async def retrieve_labware(
        self,
        labware_height: float,
        enforce_hopper_labware_sensing: bool,
        enforce_shuttle_labware_sensing: bool,
    ) -> FlexStackerStatus:
        return await self._observable(
            "RetrieveLabware",
            {
                "labware_height": labware_height,
                "enforce_hopper_labware_sensing": enforce_hopper_labware_sensing,
                "enforce_shuttle_labware_sensing": enforce_shuttle_labware_sensing,
            },
        )

    async def store_labware(
        self,
        labware_height: float,
        enforce_shuttle_labware_sensing: bool,
    ) -> FlexStackerStatus:
        return await self._observable(
            "StoreLabware",
            {
                "labware_height": labware_height,
                "enforce_shuttle_labware_sensing": enforce_shuttle_labware_sensing,
            },
        )

    async def set_led(
        self,
        power: float,
        color: StackerLedColor,
        pattern: StackerLedPattern,
        duration: float,
        repetitions: int,
    ) -> FlexStackerStatus:
        return await self._observable(
            "SetLed",
            {
                "power": power,
                "color": color,
                "pattern": pattern,
                "duration": duration,
                "repetitions": repetitions,
            },
            maintenance=True,
        )

    async def deactivate(self) -> FlexStackerStatus:
        return await self._observable("Deactivate", maintenance=True)

    async def get_status(self) -> FlexStackerStatus:
        return await self._subscription("Subscribe_Status")

    async def get_limit_switch_status(self) -> FlexStackerLimitSwitchStatus:
        return await self._subscription("Subscribe_LimitSwitchStatus", maintenance=True)

    async def get_device_info(self) -> DeviceInfo:
        return await self._property("Get_DeviceInfo")
