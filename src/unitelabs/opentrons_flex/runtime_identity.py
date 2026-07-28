"""Release identity parsing shared by runtime preflight and live API code."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from .runtime_compat import SUPPORTED_OPENTRONS_SOURCE_COMMIT


def release_identity(
    *,
    connector_version: str,
    opentrons_version: str,
    required: bool,
    prefix: Path | None = None,
) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    """Read and validate the verified release manifest for one Python prefix."""
    manifest_path = (prefix or Path(sys.prefix)).resolve() / "runtime-manifest.json"
    if not manifest_path.is_file():
        issues = (f"Active release manifest is missing: {manifest_path}.",) if required else ()
        return None, issues
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, (f"Cannot read active release manifest {manifest_path}: {exc}",)
    expected = {
        "schemaVersion": 1,
        "connectorVersion": connector_version,
        "opentronsVersion": opentrons_version,
        "robotServerVersion": opentrons_version,
        "opentronsSourceCommit": SUPPORTED_OPENTRONS_SOURCE_COMMIT,
        "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}",
        "architecture": normalized_architecture(),
    }
    issues = [
        f"Active release manifest {name} is {manifest.get(name)!r}; expected {value!r}."
        for name, value in expected.items()
        if manifest.get(name) != value
    ]
    release_id = manifest.get("releaseId")
    bundle_sha256 = manifest.get("bundleSha256")
    if not isinstance(release_id, str) or not release_id:
        issues.append("Active release manifest releaseId is missing.")
    if (
        not isinstance(bundle_sha256, str)
        or len(bundle_sha256) != 64
        or any(character not in "0123456789abcdef" for character in bundle_sha256)
    ):
        issues.append("Active release manifest bundleSha256 is invalid.")
    if isinstance(release_id, str) and release_id and isinstance(bundle_sha256, str):
        expected_release_id = (
            f"flex-{connector_version}-ot{opentrons_version}-"
            f"py{sys.version_info.major}.{sys.version_info.minor}-"
            f"{normalized_architecture()}-{bundle_sha256[:12]}"
        )
        if release_id != expected_release_id:
            issues.append(f"Active release manifest releaseId is {release_id!r}; expected {expected_release_id!r}.")
    if issues:
        return None, tuple(issues)
    return {
        "release_id": release_id,
        "bundle_sha256": bundle_sha256,
    }, ()


def normalized_architecture() -> str:
    """Return the architecture label used by release manifests."""
    architecture = platform.machine().lower()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(architecture, architecture)
