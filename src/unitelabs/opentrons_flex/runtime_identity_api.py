"""Process-bound release identity exposed by the embedded robot-server."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter

from .runtime_compat import RuntimeCompatibilityReport
from .runtime_identity import release_identity

# Resolve these once when the connector process imports this module. If the
# /var/sila2_flex symlink changes without a service restart, the running process
# continues reporting the immutable release directory it actually launched from.
_PROCESS_RELEASE_PREFIX = Path(sys.prefix).resolve()


def process_runtime_identity(
    compatibility: RuntimeCompatibilityReport,
    *,
    require_release: bool,
) -> dict[str, object]:
    """Build identity evidence bound to this running Python process."""
    active_release_identity, issues = release_identity(
        connector_version=compatibility.connector_version,
        opentrons_version=compatibility.opentrons_version,
        required=require_release,
        prefix=_PROCESS_RELEASE_PREFIX,
    )
    if issues:
        detail = " ".join(issues)
        message = f"Running connector release identity is invalid: {detail}"
        raise RuntimeError(message)
    return {
        "runtimeContractId": compatibility.runtime_contract_id,
        "connectorVersion": compatibility.connector_version,
        "opentronsVersion": compatibility.opentrons_version,
        "releaseIdentity": active_release_identity,
    }


def create_runtime_identity_router(identity: dict[str, object]) -> APIRouter:
    """Create the read-only identity endpoint served by the live process."""
    router = APIRouter()

    @router.get("/unitelabs/runtime", include_in_schema=True)
    async def get_runtime_identity() -> dict[str, object]:
        return dict(identity)

    return router


__all__ = ["create_runtime_identity_router", "process_runtime_identity"]
