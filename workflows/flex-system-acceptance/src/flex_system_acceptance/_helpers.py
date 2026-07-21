"""Unitelabs SDK compatibility helpers for the Flex acceptance workflow."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Mapping

from unitelabs.sdk import Client

INSTRUMENT_NAME = "Opentrons Flex"


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


async def get_flex_service(device_name: str):
    """Connect to a named Flex service and keep its client alive."""
    client = Client()
    service = await client.get_service_by_name(device_name)
    if service is None:
        msg = f"Service {device_name!r} was not found. Check the published connector name and cloud connection."
        raise ValueError(msg)
    return service, client


def feature(service: object, identifier: str) -> object:
    """Resolve a SiLA feature across supported Unitelabs SDK naming styles."""
    snake = _snake(identifier)
    candidates = (snake, f"{snake}_feature")
    for attribute in candidates:
        if hasattr(service, attribute):
            return getattr(service, attribute)

    modules = getattr(service, "modules", {})
    if isinstance(modules, Mapping):
        wanted = _normalized(identifier)
        for name, module in modules.items():
            if _normalized(str(name)).endswith(wanted):
                return module
    msg = f"Connected service does not expose the required SiLA feature {identifier!r}."
    raise AttributeError(msg)


async def invoke(target: object, method_names: str | Iterable[str], **parameters: object) -> object:
    """Invoke one SDK command/property while tolerating property naming differences."""
    names = (method_names,) if isinstance(method_names, str) else tuple(method_names)
    for name in names:
        candidate = getattr(target, name, None)
        if candidate is None:
            continue
        if not callable(candidate):
            if parameters:
                msg = f"Feature attribute {name!r} is not callable but parameters were supplied."
                raise TypeError(msg)
            return candidate
        result = candidate(**parameters)
        return await result if inspect.isawaitable(result) else result
    msg = f"Feature {type(target).__name__} does not expose any of {names!r}."
    raise AttributeError(msg)


def field(value: object, name: str) -> object:
    """Read a dataclass or SDK-decoded PascalCase/snake_case response field."""
    if hasattr(value, name):
        return getattr(value, name)
    if isinstance(value, Mapping):
        pascal = "".join(part.capitalize() for part in name.split("_"))
        for candidate in (name, pascal):
            if candidate in value:
                return value[candidate]
    msg = f"Response {value!r} is missing field {name!r}."
    raise KeyError(msg)
