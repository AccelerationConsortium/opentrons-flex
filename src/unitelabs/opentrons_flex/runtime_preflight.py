"""Command-line runtime preflight that never initializes Flex hardware."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from .runtime_compat import (
    SUPPORTED_OPENTRONS_SOURCE_COMMIT,
    inspect_runtime_compatibility,
    mutation_configuration_issues,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the Flex connector runtime without initializing hardware.")
    parser.add_argument("--config", type=Path, required=True, help="Installed connector JSON configuration.")
    parser.add_argument(
        "--require-robot-server",
        action="store_true",
        help="Require the embedded Opentrons robot-server imports used in connector mode.",
    )
    parser.add_argument(
        "--require-mutation",
        action="store_true",
        help="Fail unless controlled run mutation is ready and authenticated.",
    )
    parser.add_argument(
        "--require-live-hardware",
        action="store_true",
        help="Reject simulator configuration for an on-robot deployment.",
    )
    return parser


def _read_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"Cannot read connector config {path}: {exc}"
        raise RuntimeError(message) from exc
    if not isinstance(payload, dict):
        message = f"Connector config {path} must contain a JSON object."
        raise RuntimeError(message)
    return payload


def main(argv: list[str] | None = None) -> int:
    """Run the no-hardware runtime preflight."""
    args = _parser().parse_args(argv)
    try:
        config = _read_config(args.config)
        require_robot_server = bool(args.require_robot_server or config.get("with_robot_server"))
        report = inspect_runtime_compatibility(require_robot_server=require_robot_server)
        config_issues = _configuration_issues(
            config,
            connector_version=report.connector_version,
            require_robot_server=args.require_robot_server,
            require_live_hardware=args.require_live_hardware,
        )
        release_identity, release_issues = _release_identity(
            connector_version=report.connector_version,
            opentrons_version=report.opentrons_version,
            required=args.require_live_hardware,
        )
        config_issues = (*config_issues, *release_issues)
        token_env = str(config.get("run_mutation_token_env", "UNITELABS_RUN_MUTATION_TOKEN"))
        actor_env = str(config.get("run_mutation_actor_env", "UNITELABS_RUN_MUTATION_ACTOR"))
        mutation_issues = mutation_configuration_issues(
            report,
            ledger_path=_string_or_none(config.get("run_mutation_ledger_path")),
            token=os.environ.get(token_env),
            actor=os.environ.get(actor_env),
        )
        mutation_ready = not mutation_issues
        payload = {
            **report.to_dict(),
            "release_identity": release_identity,
            "configuration_issues": config_issues,
            "mutation_requested": config.get("run_mutation_ledger_path") is not None,
            "mutation_required": bool(config.get("run_mutation_required", False)),
            "mutation_ready": mutation_ready,
            "mutation_configuration_issues": mutation_issues,
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if not report.base_compatible or config_issues:
            return 1
        if (args.require_mutation or payload["mutation_required"]) and not mutation_ready:
            return 2
        return 0
    except RuntimeError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _configuration_issues(
    config: dict[str, Any],
    *,
    connector_version: str,
    require_robot_server: bool,
    require_live_hardware: bool,
) -> tuple[str, ...]:
    issues = []
    if require_robot_server and config.get("with_robot_server") is not True:
        issues.append("with_robot_server must be true for connector mode.")
    if require_live_hardware and config.get("use_simulator") is not False:
        issues.append("use_simulator must be false for a real Flex deployment.")
    sila_config = config.get("sila_server")
    configured_version = sila_config.get("version") if isinstance(sila_config, dict) else None
    if configured_version != connector_version:
        issues.append(
            f"sila_server.version is {configured_version!r}; expected connector version {connector_version!r}."
        )
    return tuple(issues)


def _release_identity(
    *,
    connector_version: str,
    opentrons_version: str,
    required: bool,
    prefix: Path | None = None,
) -> tuple[dict[str, str] | None, tuple[str, ...]]:
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
        "architecture": _normalized_architecture(),
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
            f"{_normalized_architecture()}-{bundle_sha256[:12]}"
        )
        if release_id != expected_release_id:
            issues.append(f"Active release manifest releaseId is {release_id!r}; expected {expected_release_id!r}.")
    if issues:
        return None, tuple(issues)
    return {
        "release_id": release_id,
        "bundle_sha256": bundle_sha256,
    }, ()


def _normalized_architecture() -> str:
    architecture = platform.machine().lower()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(architecture, architecture)


if __name__ == "__main__":
    raise SystemExit(main())
