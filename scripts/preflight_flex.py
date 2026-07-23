#!/usr/bin/env python3
"""Run bounded, read-only readiness checks before a Flex workflow test."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Check:
    """One no-motion readiness result."""

    name: str
    ok: bool
    detail: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Flex hostname or IP address.")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--http-port", type=int, default=31950)
    parser.add_argument("--grpc-port", type=int, default=50051)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--runtime-only", action="store_true", help="Skip live HTTP and gRPC checks.")
    parser.add_argument(
        "--allow-base-only",
        action="store_true",
        help="Do not require controlled mutation readiness or routes.",
    )
    return parser


def _remote_runtime(args: argparse.Namespace) -> Check:
    mutation_flag = "" if args.allow_base_only else "--require-mutation"
    remote_command = (
        "set -eu; "
        "test -x /var/sila2_flex/bin/python; "
        "if test -f /var/lib/unitelabs-opentrons-flex/run-mutation.env; then "
        "set -a; . /var/lib/unitelabs-opentrons-flex/run-mutation.env; set +a; fi; "
        "exec /var/sila2_flex/bin/python -m unitelabs.opentrons_flex.runtime_preflight "
        "--config /var/sila2_flex/config.json --require-robot-server "
        f"{mutation_flag}"
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
        return Check("remote-runtime", False, str(exc))
    output = completed.stdout.strip() or completed.stderr.strip()
    if completed.returncode != 0:
        return Check("remote-runtime", False, output[-2000:])
    try:
        report = json.loads(completed.stdout)
        detail = (
            f"connector={report['connector_version']}, opentrons={report['opentrons_version']}, "
            f"python={report['python_version']}, mutation_ready={report['mutation_ready']}"
        )
    except (json.JSONDecodeError, KeyError):
        detail = output[-2000:]
    return Check("remote-runtime", True, detail)


def _tcp_check(host: str, port: int, timeout: float) -> Check:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return Check(f"tcp:{port}", False, str(exc))
    return Check(f"tcp:{port}", True, "connection accepted")


def _http_json(host: str, port: int, path: str, timeout: float) -> tuple[Check, Any | None]:
    url = f"http://{host}:{port}{path}"
    request = urllib.request.Request(url, headers={"User-Agent": "unitelabs-flex-preflight/0.9.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return Check(f"http:{path}", False, str(exc)), None
    return Check(f"http:{path}", True, f"HTTP {status}"), payload


def main(argv: list[str] | None = None) -> int:
    """Run all requested no-motion checks and print a machine-readable report."""
    args = _parser().parse_args(argv)
    checks = [_remote_runtime(args)]
    if not args.runtime_only:
        checks.append(_tcp_check(args.host, args.grpc_port, args.timeout))
        for path in ("/health", "/deck_configuration", "/pipettes", "/modules", "/openapi.json"):
            check, payload = _http_json(args.host, args.http_port, path, args.timeout)
            checks.append(check)
            if path == "/openapi.json" and check.ok and not args.allow_base_only:
                paths = payload.get("paths", {}) if isinstance(payload, dict) else {}
                mutation_path = "/unitelabs/runs/{run_id}/mutations"
                checks.append(
                    Check(
                        "mutation-route",
                        mutation_path in paths,
                        f"{mutation_path} {'present' if mutation_path in paths else 'missing'}",
                    )
                )
    report = {"host": args.host, "checks": [asdict(check) for check in checks]}
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
