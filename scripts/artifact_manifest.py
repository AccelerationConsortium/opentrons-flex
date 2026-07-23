#!/usr/bin/env python3
"""Build and verify a self-describing Flex ARM wheel artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
from pathlib import Path
from typing import Any

MANIFEST_NAME = "runtime-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS"
SCHEMA_VERSION = 1
PINNED_RUNTIME_WHEELS = {
    "aiohttp": "3.12.14",
    "anyio": "4.9.0",
    "fastapi": "0.100.0",
    "idna": "3.3",
    "paho-mqtt": "1.6.1",
    "pydantic": "2.11.7",
    "pydantic-settings": "2.4.0",
    "pyro5": "5.17",
    "python-can": "4.2.2",
    "python-dotenv": "1.0.1",
    "python-multipart": "0.0.18",
    "sqlalchemy": "1.4.51",
    "uvicorn": "0.27.0.post1",
    "wsproto": "1.2.0",
}
_WHEEL_PATTERN = re.compile(
    r"^(?P<name>.+?)-(?P<version>[0-9][^-]*)-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Create the manifest and checksum inventory.")
    build.add_argument("directory", type=Path)
    build.add_argument("--connector-version", required=True)
    build.add_argument("--opentrons-version", required=True)
    build.add_argument("--robot-server-version", required=True)
    build.add_argument("--opentrons-source-commit", required=True)
    build.add_argument("--python-version", required=True)
    build.add_argument("--architecture", required=True)

    verify = subparsers.add_parser("verify", help="Verify every artifact file and expected runtime.")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--connector-version")
    verify.add_argument("--opentrons-version")
    verify.add_argument("--robot-server-version")
    verify.add_argument("--opentrons-source-commit")
    verify.add_argument("--python-version")
    verify.add_argument("--architecture")
    verify.add_argument("--check-host-python", action="store_true")
    verify.add_argument("--check-host-architecture", action="store_true")

    field = subparsers.add_parser("field", help="Print one top-level manifest field.")
    field.add_argument("directory", type=Path)
    field.add_argument("name")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheel_identity(path: Path) -> tuple[str, str]:
    match = _WHEEL_PATTERN.match(path.name)
    if match is None:
        raise RuntimeError(f"Unrecognized wheel filename: {path.name}")
    return _normalize_distribution(match.group("name")), match.group("version")


def _require_wheel_version(wheels: list[dict[str, Any]], distribution: str, expected: str) -> None:
    normalized = _normalize_distribution(distribution)
    versions = sorted({item["version"] for item in wheels if item["distribution"] == normalized})
    if versions != [expected]:
        actual = ", ".join(versions) if versions else "missing"
        raise RuntimeError(f"{distribution} wheel version is {actual}; expected exactly {expected}.")


def build_manifest(
    directory: Path,
    *,
    connector_version: str,
    opentrons_version: str,
    robot_server_version: str,
    opentrons_source_commit: str,
    python_version: str,
    architecture: str,
) -> dict[str, Any]:
    """Write a deterministic manifest and checksum inventory."""
    directory = directory.resolve()
    wheel_paths = sorted(directory.glob("*.whl"))
    if not wheel_paths:
        raise RuntimeError(f"No wheels found in {directory}.")

    wheels: list[dict[str, Any]] = []
    for path in wheel_paths:
        distribution, wheel_version = _wheel_identity(path)
        wheels.append(
            {
                "filename": path.name,
                "distribution": distribution,
                "version": wheel_version,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    _require_wheel_version(wheels, "unitelabs-opentrons-flex", connector_version)
    _require_wheel_version(wheels, "opentrons", opentrons_version)
    _require_wheel_version(wheels, "opentrons-shared-data", opentrons_version)
    _require_wheel_version(wheels, "opentrons-hardware", opentrons_version)
    _require_wheel_version(wheels, "server-utils", opentrons_version)
    _require_wheel_version(wheels, "robot-server", robot_server_version)
    for distribution, expected_version in PINNED_RUNTIME_WHEELS.items():
        _require_wheel_version(wheels, distribution, expected_version)
    if not re.fullmatch(r"[0-9a-f]{40}", opentrons_source_commit):
        raise RuntimeError("Opentrons source commit must be a full lowercase Git commit SHA.")

    bundle_digest = hashlib.sha256(
        "\n".join(f"{item['sha256']}  {item['filename']}" for item in wheels).encode()
    ).hexdigest()
    release_id = (
        f"flex-{connector_version}-ot{opentrons_version}-py{python_version}-{architecture}-{bundle_digest[:12]}"
    )
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "releaseId": release_id,
        "connectorVersion": connector_version,
        "opentronsVersion": opentrons_version,
        "robotServerVersion": robot_server_version,
        "opentronsSourceCommit": opentrons_source_commit,
        "pythonVersion": python_version,
        "architecture": architecture,
        "bundleSha256": bundle_digest,
        "pinnedRuntimeWheels": PINNED_RUNTIME_WHEELS,
        "wheels": wheels,
    }
    manifest_path = directory / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checksum_paths = [*wheel_paths, manifest_path]
    checksums = "\n".join(f"{_sha256(path)}  {path.name}" for path in checksum_paths) + "\n"
    (directory / CHECKSUMS_NAME).write_text(checksums, encoding="utf-8")
    return manifest


def _load_manifest(directory: Path) -> dict[str, Any]:
    manifest_path = directory / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise RuntimeError(f"{manifest_path} does not use schema version {SCHEMA_VERSION}.")
    return manifest


def _safe_artifact_path(directory: Path, filename: object) -> Path:
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise RuntimeError(f"Unsafe artifact filename in manifest: {filename!r}")
    return directory / filename


def _verify_checksum_file(directory: Path) -> set[str]:
    checksum_path = directory / CHECKSUMS_NAME
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Cannot read {checksum_path}: {exc}") from exc
    if not lines:
        raise RuntimeError(f"{checksum_path} is empty.")
    filenames: set[str] = set()
    for line in lines:
        parts = line.split("  ", maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise RuntimeError(f"Malformed checksum line: {line!r}")
        path = _safe_artifact_path(directory, parts[1])
        if path.name in filenames:
            raise RuntimeError(f"Duplicate checksum entry: {path.name}.")
        if not path.is_file() or _sha256(path) != parts[0]:
            raise RuntimeError(f"Checksum verification failed for {parts[1]}.")
        filenames.add(path.name)
    return filenames


def verify_manifest(
    directory: Path,
    *,
    expected: dict[str, str | None],
    check_host_python: bool,
    check_host_architecture: bool,
) -> dict[str, Any]:
    """Verify artifact integrity and its declared deployment contract."""
    directory = directory.resolve()
    manifest = _load_manifest(directory)
    checksum_filenames = _verify_checksum_file(directory)

    wheel_records = manifest.get("wheels")
    if not isinstance(wheel_records, list) or not wheel_records:
        raise RuntimeError("Manifest wheel inventory is missing or empty.")
    expected_filenames: set[str] = set()
    for record in wheel_records:
        if not isinstance(record, dict):
            raise RuntimeError("Manifest wheel inventory contains a non-object entry.")
        path = _safe_artifact_path(directory, record.get("filename"))
        expected_filenames.add(path.name)
        if not path.is_file():
            raise RuntimeError(f"Manifest wheel is missing: {path.name}")
        if path.stat().st_size != record.get("size") or _sha256(path) != record.get("sha256"):
            raise RuntimeError(f"Manifest verification failed for {path.name}.")
        distribution, wheel_version = _wheel_identity(path)
        if distribution != record.get("distribution") or wheel_version != record.get("version"):
            raise RuntimeError(f"Manifest identity does not match wheel filename {path.name}.")
    actual_filenames = {path.name for path in directory.glob("*.whl")}
    if actual_filenames != expected_filenames:
        extras = sorted(actual_filenames - expected_filenames)
        missing = sorted(expected_filenames - actual_filenames)
        raise RuntimeError(f"Wheel inventory mismatch; extra={extras}, missing={missing}.")
    expected_checksum_filenames = expected_filenames | {MANIFEST_NAME}
    if checksum_filenames != expected_checksum_filenames:
        extras = sorted(checksum_filenames - expected_checksum_filenames)
        missing = sorted(expected_checksum_filenames - checksum_filenames)
        raise RuntimeError(f"Checksum inventory mismatch; extra={extras}, missing={missing}.")

    runtime_fields = {
        "connectorVersion": manifest.get("connectorVersion"),
        "opentronsVersion": manifest.get("opentronsVersion"),
        "robotServerVersion": manifest.get("robotServerVersion"),
        "opentronsSourceCommit": manifest.get("opentronsSourceCommit"),
        "pythonVersion": manifest.get("pythonVersion"),
        "architecture": manifest.get("architecture"),
    }
    if not all(isinstance(value, str) and value for value in runtime_fields.values()):
        raise RuntimeError("Manifest runtime identity fields must be non-empty strings.")
    _require_wheel_version(wheel_records, "unitelabs-opentrons-flex", runtime_fields["connectorVersion"])
    _require_wheel_version(wheel_records, "opentrons", runtime_fields["opentronsVersion"])
    _require_wheel_version(wheel_records, "opentrons-shared-data", runtime_fields["opentronsVersion"])
    _require_wheel_version(wheel_records, "opentrons-hardware", runtime_fields["opentronsVersion"])
    _require_wheel_version(wheel_records, "server-utils", runtime_fields["opentronsVersion"])
    _require_wheel_version(wheel_records, "robot-server", runtime_fields["robotServerVersion"])
    if manifest.get("pinnedRuntimeWheels") != PINNED_RUNTIME_WHEELS:
        raise RuntimeError("Manifest pinned runtime wheel contract does not match the verifier.")
    for distribution, expected_version in PINNED_RUNTIME_WHEELS.items():
        _require_wheel_version(wheel_records, distribution, expected_version)

    ordered_wheels = sorted(wheel_records, key=lambda item: item["filename"])
    bundle_digest = hashlib.sha256(
        "\n".join(f"{item['sha256']}  {item['filename']}" for item in ordered_wheels).encode()
    ).hexdigest()
    if manifest.get("bundleSha256") != bundle_digest:
        raise RuntimeError("Manifest bundleSha256 does not match the verified wheel inventory.")
    expected_release_id = (
        f"flex-{runtime_fields['connectorVersion']}-ot{runtime_fields['opentronsVersion']}-"
        f"py{runtime_fields['pythonVersion']}-{runtime_fields['architecture']}-{bundle_digest[:12]}"
    )
    if manifest.get("releaseId") != expected_release_id:
        raise RuntimeError(f"Manifest releaseId does not match the verified artifact: {expected_release_id}.")

    for manifest_field, expected_value in expected.items():
        if expected_value is not None and manifest.get(manifest_field) != expected_value:
            raise RuntimeError(
                f"Manifest {manifest_field} is {manifest.get(manifest_field)!r}; expected {expected_value!r}."
            )
    if check_host_python:
        host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        if manifest.get("pythonVersion") != host_python:
            raise RuntimeError(f"Host Python is {host_python}; artifact requires {manifest.get('pythonVersion')}.")
    if check_host_architecture:
        host_architecture = platform.machine().lower()
        aliases = {"arm64": "aarch64", "amd64": "x86_64"}
        normalized_host = aliases.get(host_architecture, host_architecture)
        if manifest.get("architecture") != normalized_host:
            raise RuntimeError(
                f"Host architecture is {normalized_host}; artifact requires {manifest.get('architecture')}."
            )
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Run the artifact manifest CLI."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_manifest(
                args.directory,
                connector_version=args.connector_version,
                opentrons_version=args.opentrons_version,
                robot_server_version=args.robot_server_version,
                opentrons_source_commit=args.opentrons_source_commit,
                python_version=args.python_version,
                architecture=args.architecture,
            )
            sys.stdout.write(result["releaseId"] + "\n")
        elif args.command == "verify":
            result = verify_manifest(
                args.directory,
                expected={
                    "connectorVersion": args.connector_version,
                    "opentronsVersion": args.opentrons_version,
                    "robotServerVersion": args.robot_server_version,
                    "opentronsSourceCommit": args.opentrons_source_commit,
                    "pythonVersion": args.python_version,
                    "architecture": args.architecture,
                },
                check_host_python=args.check_host_python,
                check_host_architecture=args.check_host_architecture,
            )
            sys.stdout.write(f"verified {result['releaseId']}\n")
        else:
            result = _load_manifest(args.directory.resolve())
            if args.name not in result or isinstance(result[args.name], (dict, list)):
                raise RuntimeError(f"Manifest field {args.name!r} is missing or not scalar.")
            sys.stdout.write(f"{result[args.name]}\n")
        return 0
    except RuntimeError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
