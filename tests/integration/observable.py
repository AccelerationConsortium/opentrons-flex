"""Helpers for calling SiLA observable commands over raw gRPC."""

import asyncio
import base64

import grpc
import grpc.aio
from sila.server import CommandConfirmation, CommandExecutionUUID


async def call_observable(
    channel: grpc.aio.Channel,
    pb: object,
    service: str,
    package: str,
    method: str,
    params: dict | None = None,
    timeout_s: float = 10.0,
) -> dict:
    """Start an observable command, poll its result, and decode the response."""
    req = await pb.encode(f"{package}.{method}_Parameters", params or {})
    start = channel.unary_unary(f"/{service}/{method}")
    confirmation = CommandConfirmation.decode(await start(req))
    uuid = confirmation.command_execution_uuid.value

    result = channel.unary_unary(f"/{service}/{method}_Result")
    uuid_bytes = CommandExecutionUUID(value=uuid).encode()
    deadline = asyncio.get_running_loop().time() + timeout_s

    while True:
        try:
            resp_bytes = await result(uuid_bytes)
            return await pb.decode(f"{package}.{method}_Responses", resp_bytes)
        except grpc.aio.AioRpcError as exc:
            details = base64.b64decode(exc.details() or b"")
            result_not_ready = exc.code() is grpc.StatusCode.ABORTED and b"Result is not ready" in details
            if not result_not_ready:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"{method} did not finish within {timeout_s}s") from exc
            await asyncio.sleep(0.05)
