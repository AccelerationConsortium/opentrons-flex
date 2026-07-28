#!/usr/bin/env python3
"""Run bounded, read-only readiness checks before a Flex workflow test."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from unitelabs.opentrons_flex.acceptance import AcceptanceManifest
from unitelabs.opentrons_flex.hitl_evidence import (
    HITL_BLOCKED_STATUS,
    HITL_READINESS_SCHEMA_VERSION,
    HITL_READINESS_STATUS,
)
from unitelabs.opentrons_flex.http_safety import open_no_redirect
from unitelabs.opentrons_flex.runtime_compat import (
    RUNTIME_CONTRACT_ID,
    SUPPORTED_OPENTRONS_SOURCE_COMMIT,
    SUPPORTED_OPENTRONS_VERSION,
    SUPPORTED_PYTHON_VERSION,
)


@dataclass(frozen=True)
class Check:
    """One no-motion readiness result."""

    name: str
    ok: bool
    detail: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", type=_host, help="Flex hostname or IP address.")
    parser.add_argument("--ssh-user", type=_ssh_user, default="root")
    parser.add_argument("--http-port", type=_port, default=31950)
    parser.add_argument("--grpc-port", type=_port, default=50051)
    parser.add_argument("--timeout", type=_positive_float, default=8.0)
    parser.add_argument("--runtime-only", action="store_true", help="Skip live HTTP and gRPC checks.")
    parser.add_argument(
        "--allow-base-only",
        action="store_true",
        help="Do not require controlled mutation readiness or routes.",
    )
    parser.add_argument(
        "--acceptance-manifest",
        type=Path,
        help="Strict acceptance manifest used to bind readiness evidence to one live Flex.",
    )
    parser.add_argument(
        "--runtime-manifest",
        type=Path,
        help="Verified runtime-manifest.json from the exact wheel artifact deployed to the Flex.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON report to this cross-platform path as well as stdout.",
    )
    return parser


def _remote_runtime(
    args: argparse.Namespace,
) -> tuple[Check, dict[str, Any] | None, dict[str, Any]]:
    mutation_flag = "" if args.allow_base_only else "--require-mutation"
    runtime_only_flag = "--runtime-only" if args.runtime_only else ""
    remote_command = (
        "set -eu; "
        "test -x /var/sila2_flex/bin/python; "
        "if test -f /var/lib/unitelabs-opentrons-flex/run-mutation.env; then "
        "set -a; . /var/lib/unitelabs-opentrons-flex/run-mutation.env; set +a; fi; "
        "exec /var/sila2_flex/bin/python -m unitelabs.opentrons_flex.robot_readiness "
        f"--config /var/sila2_flex/config.json --http-port {args.http_port} --timeout {args.timeout:g} "
        f"{mutation_flag} {runtime_only_flag}"
    )
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                f"ConnectTimeout={max(1, int(args.timeout))}",
                "-o",
                "BatchMode=yes",
                f"{args.ssh_user}@{args.host}",
                remote_command,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=args.timeout + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("remote-runtime", False, str(exc)), None, {}
    output = completed.stdout.strip() or completed.stderr.strip()
    if completed.returncode != 0:
        return Check("remote-runtime", False, output[-2000:]), None, {}
    try:
        report = json.loads(completed.stdout)
        runtime = report["runtime"]
        http_payloads = report["http"]
        detail = (
            f"connector={runtime['connector_version']}, opentrons={runtime['opentrons_version']}, "
            f"python={runtime['python_version']}, contract={runtime['runtime_contract_id']}, "
            f"mutation_ready={runtime['mutation_ready']}"
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return Check("remote-runtime", False, output[-2000:]), None, {}
    if not isinstance(runtime, dict) or not isinstance(http_payloads, dict):
        return Check("remote-runtime", False, "robot readiness returned an invalid object"), None, {}
    return Check("remote-runtime", True, detail), runtime, http_payloads


def _tcp_check(host: str, port: int, timeout: float) -> Check:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return Check(f"tcp:{port}", False, str(exc))
    return Check(f"tcp:{port}", True, "connection accepted")


def _http_json(host: str, port: int, path: str, timeout: float) -> tuple[Check, Any | None]:
    url = f"http://{_url_host(host)}:{port}{path}"
    request = urllib.request.Request(url, headers={"User-Agent": "unitelabs-flex-preflight/0.9.1"})
    try:
        with open_no_redirect(request, timeout=timeout) as response:
            if response.geturl() != url:
                return Check(f"http:{path}", False, f"unexpected redirect to {response.geturl()}"), None
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return Check(f"http:{path}", False, str(exc)), None
    return Check(f"http:{path}", True, f"HTTP {status}"), payload


def main(argv: list[str] | None = None) -> int:
    """Run all requested no-motion checks and print a machine-readable report."""
    args = _parser().parse_args(argv)
    remote_runtime_check, runtime, authenticated_http_payloads = _remote_runtime(args)
    checks = [remote_runtime_check]
    if not args.runtime_only:
        checks.append(_tcp_check(args.host, args.grpc_port, args.timeout))
        for path in ("/health", "/deck_configuration", "/pipettes", "/modules", "/openapi.json"):
            check, _ = _http_json(args.host, args.http_port, path, args.timeout)
            checks.append(check)
        if not args.allow_base_only:
            openapi = authenticated_http_payloads.get("/openapi.json")
            paths = openapi.get("paths", {}) if isinstance(openapi, dict) else {}
            mutation_path = "/unitelabs/runs/{run_id}/mutations"
            checks.append(
                Check(
                    "mutation-route",
                    mutation_path in paths,
                    f"{mutation_path} {'present' if mutation_path in paths else 'missing'}",
                )
            )
    manifest: AcceptanceManifest | None = None
    manifest_sha256: str | None = None
    expected_release_identity: dict[str, str] | None = None
    if args.acceptance_manifest is not None:
        try:
            manifest = AcceptanceManifest.load(args.acceptance_manifest)
            manifest_sha256 = manifest.commissioning_digest()
            if args.runtime_manifest is None:
                checks.append(
                    Check(
                        "artifact-runtime-manifest",
                        False,
                        "--runtime-manifest is required with --acceptance-manifest",
                    )
                )
            else:
                expected_release_identity = _runtime_manifest_identity(
                    args.runtime_manifest,
                    runtime,
                )
                checks.append(
                    Check(
                        "artifact-runtime-manifest",
                        True,
                        f"release={expected_release_identity['release_id']}",
                    )
                )
            checks.extend(
                _acceptance_checks(
                    args.host,
                    manifest,
                    runtime,
                    authenticated_http_payloads,
                    expected_release_identity,
                )
            )
        except ValueError as exc:
            checks.append(Check("acceptance-manifest", False, str(exc)))

    checks_ok = all(check.ok for check in checks)
    all_ready = bool(manifest is not None and runtime is not None and checks_ok)
    evidence_status = (
        HITL_READINESS_STATUS
        if all_ready
        else "RUNTIME_READY"
        if manifest is None and runtime is not None and checks_ok
        else HITL_BLOCKED_STATUS
    )
    runtime_contract_id = runtime.get("runtime_contract_id") if isinstance(runtime, dict) else None
    release_identity = runtime.get("release_identity") if isinstance(runtime, dict) else None
    report = {
        "schemaVersion": HITL_READINESS_SCHEMA_VERSION,
        "evidenceStatus": evidence_status,
        "hardwarePassed": False,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "host": args.host,
        "manifestSha256": manifest_sha256,
        "runtimeContractId": runtime_contract_id,
        "releaseIdentity": release_identity,
        "runtime": runtime,
        "checks": [asdict(check) for check in checks],
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.expanduser().write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0 if checks_ok and (manifest is None or all_ready) else 1


def _acceptance_checks(
    host: str,
    manifest: AcceptanceManifest,
    runtime: dict[str, Any] | None,
    http_payloads: dict[str, Any],
    expected_release_identity: dict[str, str] | None,
) -> list[Check]:
    checks = [
        Check(
            "acceptance-host",
            manifest.expected_robot_host.casefold() == host.casefold(),
            f"manifest={manifest.expected_robot_host}, target={host}",
        )
    ]
    remote_contract = runtime.get("runtime_contract_id") if isinstance(runtime, dict) else None
    checks.append(
        Check(
            "runtime-contract",
            remote_contract == RUNTIME_CONTRACT_ID,
            f"remote={remote_contract or 'missing'}, checkout={RUNTIME_CONTRACT_ID}",
        )
    )
    remote_release_identity = runtime.get("release_identity") if isinstance(runtime, dict) else None
    checks.append(
        Check(
            "release-identity",
            expected_release_identity is not None and remote_release_identity == expected_release_identity,
            f"remote={remote_release_identity or 'missing'}, expected={expected_release_identity or 'missing'}",
        )
    )
    checks.append(_pipette_inventory_check(manifest, http_payloads.get("/pipettes")))
    checks.extend(_module_inventory_checks(manifest, http_payloads.get("/modules")))
    return checks


def _pipette_inventory_check(manifest: AcceptanceManifest, payload: object) -> Check:
    inventory = payload.get("data", payload) if isinstance(payload, dict) else None
    mount = manifest.pipetting.mount.lower()
    pipette = inventory.get(mount) if isinstance(inventory, dict) else None
    if not isinstance(pipette, dict) or not pipette:
        return Check("acceptance-pipette", False, f"{mount} pipette mount is empty or unreadable")
    identity = pipette.get("serialNumber") or pipette.get("pipetteId") or pipette.get("name") or "attached"
    return Check("acceptance-pipette", True, f"{mount}={identity}")


def _module_inventory_checks(manifest: AcceptanceManifest, payload: object) -> list[Check]:
    module_items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(module_items, list):
        return [Check("acceptance-modules", False, "module inventory is missing or unreadable")]
    expected = {
        "heater-shaker": (manifest.modules.heater_shaker_serial, "heaterShakerModuleV1"),
        "thermocycler": (manifest.modules.thermocycler_serial, "thermocyclerModuleV2"),
        "temperature-module": (manifest.modules.temperature_module_serial, "temperatureModuleV2"),
        "plate-reader": (manifest.modules.plate_reader_serial, "absorbanceReaderV1"),
        "stacker": (manifest.modules.stacker_serial, "flexStackerModuleV1"),
    }
    observed = {}
    for item in module_items:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serialNumber") or item.get("serial") or "")
        model = str(item.get("moduleModel") or item.get("model") or "")
        if serial:
            observed[serial] = model
    return [
        Check(
            f"acceptance-module:{name}",
            observed.get(serial) == model,
            f"expected serial={serial} model={model}; observed model={observed.get(serial) or 'missing'}",
        )
        for name, (serial, model) in expected.items()
    ]


def _host(value: str) -> str:
    normalized = value.strip()
    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass
    if len(normalized) > 253:
        raise argparse.ArgumentTypeError("host must be a valid IP address or DNS name")
    labels = normalized.rstrip(".").split(".")
    if not labels or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise argparse.ArgumentTypeError("host must be a valid IP address or DNS name")
    return normalized


def _ssh_user(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", value):
        raise argparse.ArgumentTypeError("ssh user contains unsupported characters")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive")
    return number


def _url_host(host: str) -> str:
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        return host


def _runtime_manifest_identity(
    path: Path,
    runtime: dict[str, Any] | None,
) -> dict[str, str]:
    manifest_path = path.expanduser()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"Cannot read artifact runtime manifest {manifest_path}: {exc}"
        raise ValueError(message) from exc
    if not isinstance(manifest, dict):
        message = f"Artifact runtime manifest {manifest_path} must be a JSON object."
        raise ValueError(message)
    connector_version = runtime.get("connector_version") if isinstance(runtime, dict) else None
    expected_fields = {
        "schemaVersion": 1,
        "connectorVersion": connector_version,
        "opentronsVersion": SUPPORTED_OPENTRONS_VERSION,
        "robotServerVersion": SUPPORTED_OPENTRONS_VERSION,
        "opentronsSourceCommit": SUPPORTED_OPENTRONS_SOURCE_COMMIT,
        "pythonVersion": ".".join(str(part) for part in SUPPORTED_PYTHON_VERSION),
        "architecture": "aarch64",
    }
    mismatches = [
        f"{name}={manifest.get(name)!r} (expected {expected!r})"
        for name, expected in expected_fields.items()
        if manifest.get(name) != expected
    ]
    if mismatches:
        message = (
            f"Artifact runtime manifest {manifest_path} does not match the active connector contract: "
            + "; ".join(mismatches)
        )
        raise ValueError(message)
    release_id = manifest.get("releaseId")
    bundle_sha256 = manifest.get("bundleSha256")
    if not isinstance(release_id, str) or not release_id:
        message = f"Artifact runtime manifest {manifest_path} has no releaseId."
        raise ValueError(message)
    if (
        not isinstance(bundle_sha256, str)
        or len(bundle_sha256) != 64
        or any(character not in "0123456789abcdef" for character in bundle_sha256)
    ):
        message = f"Artifact runtime manifest {manifest_path} has an invalid bundleSha256."
        raise ValueError(message)
    expected_release_id = (
        f"flex-{connector_version}-ot{SUPPORTED_OPENTRONS_VERSION}-"
        f"py{expected_fields['pythonVersion']}-aarch64-{bundle_sha256[:12]}"
    )
    if release_id != expected_release_id:
        message = (
            f"Artifact runtime manifest {manifest_path} releaseId is {release_id!r}; expected {expected_release_id!r}."
        )
        raise ValueError(message)
    return {"release_id": release_id, "bundle_sha256": bundle_sha256}


if __name__ == "__main__":
    raise SystemExit(main())
