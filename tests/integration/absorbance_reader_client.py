"""Raw gRPC client shared by Absorbance Plate Reader acceptance tests."""

import grpc.aio

from unitelabs.opentrons_flex.features.absorbance_reader import AbsorbanceReaderStatus, PlateMeasurement
from unitelabs.opentrons_flex.io import DeviceInfo

from .observable import call_observable

_PACKAGE = "sila2.ca.accelerationconsortium.modules.absorbancereadercontroller.v2"
_SERVICE = f"{_PACKAGE}.AbsorbanceReaderController"


class AbsorbanceReaderClient:
    """Call the Absorbance Reader SiLA feature through its generated gRPC surface."""

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
            timeout_s=60.0,
        )
        return next(iter(decoded.values()))

    async def _property(self, method: str) -> object:
        stub = self._channel.unary_unary(f"/{_SERVICE}/{method}")
        response = await stub(b"")
        decoded = await self._protobuf.decode(f"{_PACKAGE}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def _subscription(self, method: str) -> object:
        stub = self._channel.unary_stream(f"/{_SERVICE}/{method}")
        call = stub(b"")
        response = await call.read()
        call.cancel()
        decoded = await self._protobuf.decode(f"{_PACKAGE}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def initialize_single(self, wavelength: int) -> AbsorbanceReaderStatus:
        return await self._observable("InitializeSingle", {"wavelength": wavelength})

    async def initialize_single_with_reference(
        self,
        wavelength: int,
        reference_wavelength: int,
    ) -> AbsorbanceReaderStatus:
        return await self._observable(
            "InitializeSingleWithReference",
            {"wavelength": wavelength, "reference_wavelength": reference_wavelength},
        )

    async def initialize_multiple(self, wavelengths: list[int]) -> AbsorbanceReaderStatus:
        return await self._observable("InitializeMultiple", {"wavelengths": wavelengths})

    async def read_plate(self) -> PlateMeasurement:
        return await self._observable("ReadPlate")

    async def deactivate(self) -> AbsorbanceReaderStatus:
        return await self._observable("Deactivate")

    async def get_status(self) -> AbsorbanceReaderStatus:
        return await self._subscription("Subscribe_Status")

    async def get_device_info(self) -> DeviceInfo:
        return await self._property("Get_DeviceInfo")
