"""
Minimal async SiLA client for the Flex connector, used by ``flexctl verify``.

The protobuf codec is generated locally from this package's own feature
definitions (by starting an in-process simulator on loopback), so the client
needs nothing from the robot except its gRPC port.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import typing
from collections.abc import AsyncIterator

import grpc
import grpc.aio

log = logging.getLogger(__name__)

_ALL_SIMULATED = ["heater_shaker", "flex_stacker", "absorbance_reader", "temperature_module", "thermocycler", "gripper"]


class SiLACallError(RuntimeError):
    """A SiLA command or property failed; ``identifier`` names the defined error, if any."""

    def __init__(self, identifier: str, message: str) -> None:
        super().__init__(f"{identifier}: {message}" if identifier else message)
        self.identifier = identifier
        self.message = message


@dataclasses.dataclass(frozen=True)
class _Endpoint:
    package: str
    service: str


_CODEC: tuple[object, dict[str, _Endpoint]] | None = None


async def _build_codec() -> tuple[object, dict[str, _Endpoint]]:
    """Generate (once per process) the protobuf codec from this package's feature definitions."""
    global _CODEC  # noqa: PLW0603 - process-wide cache of an expensive, immutable codec
    if _CODEC is not None:
        return _CODEC
    from unitelabs.cdk import SiLAServerConfig

    from . import OpentronsFlexConfig, create_app

    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_modules=list(_ALL_SIMULATED),
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
        module_hot_plug="report",
    )
    connector_logger = logging.getLogger("unitelabs.opentrons_flex")
    previous_level = connector_logger.level
    connector_logger.setLevel(logging.ERROR)
    generator = create_app(config)
    try:
        connector = await generator.__anext__()
        await connector.start()
        try:
            protobuf = connector.sila_server.protobuf
            endpoints = {
                feature.identifier: _Endpoint(
                    package=feature.fully_qualified_identifier.rpc_package,
                    service=f"{feature.fully_qualified_identifier.rpc_package}.{feature.identifier}",
                )
                for feature in connector.sila_server.features.values()
            }
        finally:
            await connector.stop()
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await generator.__anext__()
        connector_logger.setLevel(previous_level)
    _CODEC = (protobuf, endpoints)
    return _CODEC


class FlexClient:
    """Call commands and read properties on a running Flex connector."""

    def __init__(self, channel: grpc.aio.Channel, protobuf: object, endpoints: dict[str, _Endpoint]) -> None:
        self._channel = channel
        self._pb = protobuf
        self._endpoints = endpoints

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(cls, address: str, *, timeout_s: float = 10.0) -> AsyncIterator[FlexClient]:
        """Build the codec locally, then open an insecure gRPC channel to ``address``."""
        protobuf, endpoints = await _build_codec()
        channel = grpc.aio.insecure_channel(address)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=timeout_s)
        except TimeoutError as exc:
            await channel.close()
            message = f"No SiLA server answered at {address} within {timeout_s:g} s."
            raise ConnectionError(message) from exc
        try:
            yield cls(channel, protobuf, endpoints)
        finally:
            await channel.close()

    def _endpoint(self, feature: str) -> _Endpoint:
        try:
            return self._endpoints[feature]
        except KeyError as exc:
            message = f"Unknown feature {feature!r}."
            raise SiLACallError("", message) from exc

    async def _raise(self, exc: grpc.aio.AioRpcError) -> typing.NoReturn:
        from sila.framework.errors import DefinedExecutionError, SiLAError

        try:
            error = await SiLAError.from_rpc_error(exc)
        except Exception:  # noqa: BLE001 - not a SiLA error payload
            raise SiLACallError("", f"{exc.code().name}: {exc.details()}") from exc
        identifier = ""
        if isinstance(error, DefinedExecutionError):
            identifier = str(getattr(error, "_identifier", "") or "").rsplit("/", 1)[-1]
        raise SiLACallError(identifier, str(error)) from exc

    async def implemented_features(self) -> list[str]:
        """Return the fully qualified identifiers of every feature on the server."""
        return list(await self.read("SiLAService", "ImplementedFeatures"))

    async def read(self, feature: str, name: str) -> object:
        """Read an unobservable property."""
        endpoint = self._endpoint(feature)
        try:
            response = await self._channel.unary_unary(f"/{endpoint.service}/Get_{name}")(b"")
        except grpc.aio.AioRpcError as exc:
            await self._raise(exc)
        decoded = await self._pb.decode(f"{endpoint.package}.Get_{name}_Responses", response)
        return next(iter(decoded.values()))

    async def subscribe_first(self, feature: str, name: str) -> object:
        """Return the current value of an observable property."""
        endpoint = self._endpoint(feature)
        call = self._channel.unary_stream(f"/{endpoint.service}/Subscribe_{name}")(b"")
        try:
            response = await call.read()
        except grpc.aio.AioRpcError as exc:
            await self._raise(exc)
        finally:
            call.cancel()
        decoded = await self._pb.decode(f"{endpoint.package}.Subscribe_{name}_Responses", response)
        return next(iter(decoded.values()))

    async def call(self, feature: str, command: str, params: dict[str, object] | None = None) -> dict[str, object]:
        """Run an unobservable command."""
        endpoint = self._endpoint(feature)
        request = await self._pb.encode(f"{endpoint.package}.{command}_Parameters", params or {})
        try:
            response = await self._channel.unary_unary(f"/{endpoint.service}/{command}")(request)
        except grpc.aio.AioRpcError as exc:
            await self._raise(exc)
        return await self._pb.decode(f"{endpoint.package}.{command}_Responses", response)

    async def run(
        self,
        feature: str,
        command: str,
        params: dict[str, object] | None = None,
        *,
        timeout_s: float = 120.0,
    ) -> dict[str, object]:
        """Run an observable command to completion and return its responses."""
        import base64

        from sila.server import CommandConfirmation, CommandExecutionUUID

        endpoint = self._endpoint(feature)
        request = await self._pb.encode(f"{endpoint.package}.{command}_Parameters", params or {})
        try:
            confirmation = CommandConfirmation.decode(
                await self._channel.unary_unary(f"/{endpoint.service}/{command}")(request)
            )
        except grpc.aio.AioRpcError as exc:
            await self._raise(exc)
        uuid = CommandExecutionUUID(value=confirmation.command_execution_uuid.value).encode()
        result = self._channel.unary_unary(f"/{endpoint.service}/{command}_Result")
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            try:
                response = await result(uuid)
                return await self._pb.decode(f"{endpoint.package}.{command}_Responses", response)
            except grpc.aio.AioRpcError as exc:
                details = base64.b64decode(exc.details() or b"")
                if not (exc.code() is grpc.StatusCode.ABORTED and b"Result is not ready" in details):
                    await self._raise(exc)
                if asyncio.get_running_loop().time() >= deadline:
                    message = f"{command} did not finish within {timeout_s:g} s."
                    raise SiLACallError("", message) from exc
                await asyncio.sleep(0.1)


def field(value: object, name: str) -> object:
    """Read ``name`` from a decoded structure (mapping or object)."""
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def enum_name(value: object) -> str:
    """Return the name of a decoded enum value."""
    return str(getattr(value, "value", value))


__all__ = ["FlexClient", "SiLACallError", "enum_name", "field"]
