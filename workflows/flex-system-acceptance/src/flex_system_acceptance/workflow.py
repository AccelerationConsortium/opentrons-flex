"""Publishable Unitelabs workflow for guarded Flex system acceptance."""

import hmac
import os

from prefect import flow
from unitelabs.sdk import get_logger

from ._helpers import INSTRUMENT_NAME
from ._steps import (
    connect_and_preflight_step,
    heater_shaker_step,
    home_and_configure_step,
    liquid_handling_step,
    plate_reader_step,
    safe_shutdown,
    stacker_step,
    temperature_module_step,
    thermocycler_step,
    validate_manifest_step,
)

_COMMISSIONED_DIGEST_ENV = "FLEX_ACCEPTANCE_MANIFEST_SHA256"


def _require_commissioned_manifest(manifest) -> None:
    """Require the exact manifest fingerprint from a prior direct HITL commission."""
    approved = os.environ.get(_COMMISSIONED_DIGEST_ENV, "").strip().lower()
    if len(approved) != 64 or any(character not in "0123456789abcdef" for character in approved):
        msg = f"{_COMMISSIONED_DIGEST_ENV} must contain the 64-character digest from a passed direct HITL run."
        raise ValueError(msg)
    if not hmac.compare_digest(approved, manifest.commissioning_digest()):
        msg = "The runtime manifest differs from the exact manifest commissioned by the direct HITL run."
        raise ValueError(msg)


@flow(name="Workflow: Flex System Acceptance", retries=0)
async def flex_system_acceptance_flow(manifest: dict, device_name: str = INSTRUMENT_NAME) -> None:
    """Run the operator-prepared Flex acceptance campaign through Unitelabs."""
    logger = get_logger()
    logger.info(f"Starting Flex system acceptance | device={device_name!r}")
    validated = await validate_manifest_step(manifest)
    _require_commissioned_manifest(validated)
    if validated.service_name != device_name:
        msg = (
            f"Manifest service_name {validated.service_name!r} does not match the requested "
            f"Unitelabs device {device_name!r}."
        )
        raise ValueError(msg)
    _service, _client, features = await connect_and_preflight_step(device_name, validated)
    try:
        await home_and_configure_step(features, validated)
        await stacker_step(features, validated)
        await thermocycler_step(features, validated)
        await liquid_handling_step(features, validated)
        await heater_shaker_step(features, validated)
        await temperature_module_step(features, validated)
        await plate_reader_step(features, validated)
    finally:
        await safe_shutdown(features)
    logger.info("Flex system acceptance completed")
