"""Read the deployed runtime and robot-server inventory over robot loopback."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from unitelabs.opentrons_flex.http_safety import open_no_redirect

_HTTP_PATHS = (
    "/health",
    "/deck_configuration",
    "/pipettes",
    "/modules",
    "/openapi.json",
    "/unitelabs/runtime",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--http-port", type=int, default=31950)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--require-mutation", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print one authenticated-transport readiness payload without initializing hardware."""
    args = _parser().parse_args(argv)
    runtime_command = [
        sys.executable,
        "-m",
        "unitelabs.opentrons_flex.runtime_preflight",
        "--config",
        str(args.config),
        "--require-robot-server",
        "--require-live-hardware",
    ]
    if args.require_mutation:
        runtime_command.append("--require-mutation")
    try:
        completed = subprocess.run(
            runtime_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"ERROR: runtime preflight failed: {exc}\n")
        return 1
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout)
        return completed.returncode
    try:
        runtime = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"ERROR: runtime preflight returned invalid JSON: {exc}\n")
        return 1

    http_payloads: dict[str, object] = {}
    if not args.runtime_only:
        for path in _HTTP_PATHS:
            try:
                http_payloads[path] = _loopback_json(args.http_port, path, args.timeout)
            except RuntimeError as exc:
                sys.stderr.write(f"ERROR: {exc}\n")
                return 1
        service_identity = http_payloads["/unitelabs/runtime"]
        if not _service_matches_runtime(service_identity, runtime):
            sys.stderr.write(
                "ERROR: the running connector process does not match the active release/runtime preflight.\n"
            )
            return 1
    sys.stdout.write(json.dumps({"runtime": runtime, "http": http_payloads}, sort_keys=True) + "\n")
    return 0


def _loopback_json(port: int, path: str, timeout: float) -> object:
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Opentrons-Version": "*",
            "User-Agent": "unitelabs-flex-robot-readiness",
        },
    )
    try:
        with open_no_redirect(request, timeout=timeout) as response:
            if response.geturl() != url:
                message = f"robot loopback {path} redirected to {response.geturl()}"
                raise RuntimeError(message)
            if response.status != 200:
                message = f"robot loopback {path} returned HTTP {response.status}"
                raise RuntimeError(message)
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        message = f"robot loopback {path} failed: {exc}"
        raise RuntimeError(message) from exc


def _service_matches_runtime(service_identity: object, runtime: object) -> bool:
    if not isinstance(service_identity, dict) or not isinstance(runtime, dict):
        return False
    return (
        service_identity.get("runtimeContractId") == runtime.get("runtime_contract_id")
        and service_identity.get("connectorVersion") == runtime.get("connector_version")
        and service_identity.get("opentronsVersion") == runtime.get("opentrons_version")
        and service_identity.get("releaseIdentity") == runtime.get("release_identity")
    )


if __name__ == "__main__":
    raise SystemExit(main())
