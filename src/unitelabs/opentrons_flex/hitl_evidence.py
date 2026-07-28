"""Cross-platform validation for no-motion HITL readiness evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HITL_READINESS_SCHEMA_VERSION = 1
HITL_OFFLINE_VALIDATED_STATUS = "OFFLINE_VALIDATED"
HITL_READINESS_STATUS = "READY_FOR_HITL"
HITL_BLOCKED_STATUS = "BLOCKED"
HITL_HARDWARE_PASSED_STATUS = "HARDWARE_PASSED"
HITL_READINESS_MAX_AGE = timedelta(hours=4)


@dataclass(frozen=True)
class HitlReadinessEvidence:
    """Validated readiness report bound to one robot, manifest, and runtime."""

    host: str
    generated_at: datetime
    manifest_sha256: str
    runtime_contract_id: str
    connector_version: str
    opentrons_version: str
    python_version: str
    release_id: str
    bundle_sha256: str
    report_sha256: str


async def settle_hitl_operations(
    operations: Sequence[tuple[str, Callable[[], Awaitable[object]]]],
) -> tuple[str, ...]:
    """Attempt every safety settlement operation and retain all failures."""
    errors = []
    for label, operation in operations:
        try:
            await operation()
        except Exception as exc:  # noqa: BLE001 - settlement attempts are independent
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    return tuple(errors)


def load_hitl_readiness_evidence(
    path: str | Path,
    *,
    expected_host: str,
    expected_manifest_sha256: str,
    expected_runtime_contract_id: str,
    now: datetime | None = None,
) -> HitlReadinessEvidence:
    """Load a fresh readiness report and reject stale or mismatched evidence."""
    report_path = Path(path).expanduser()
    try:
        raw = report_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        message = f"Cannot load HITL readiness report {report_path}: {exc}"
        raise ValueError(message) from exc
    if not isinstance(payload, Mapping):
        message = "HITL readiness report must be a JSON object."
        raise ValueError(message)

    required_fields = {
        "schemaVersion",
        "evidenceStatus",
        "hardwarePassed",
        "generatedAt",
        "host",
        "manifestSha256",
        "runtimeContractId",
        "releaseIdentity",
        "runtime",
        "checks",
    }
    missing = required_fields - set(payload)
    if missing:
        message = f"HITL readiness report is missing fields: {', '.join(sorted(missing))}."
        raise ValueError(message)
    if payload["schemaVersion"] != HITL_READINESS_SCHEMA_VERSION:
        message = f"HITL readiness report schema must be {HITL_READINESS_SCHEMA_VERSION}."
        raise ValueError(message)
    if payload["evidenceStatus"] != HITL_READINESS_STATUS or payload["hardwarePassed"] is not False:
        message = "HITL readiness report is not a no-motion READY_FOR_HITL result."
        raise ValueError(message)

    host = _text(payload["host"], "host")
    if host.casefold() != expected_host.casefold():
        message = f"HITL readiness report targets {host!r}; pytest targets {expected_host!r}."
        raise ValueError(message)
    manifest_sha256 = _sha256_text(payload["manifestSha256"], "manifestSha256")
    if manifest_sha256 != expected_manifest_sha256:
        message = "HITL readiness report does not match the selected acceptance manifest."
        raise ValueError(message)
    runtime_contract_id = _text(payload["runtimeContractId"], "runtimeContractId")
    if runtime_contract_id != expected_runtime_contract_id:
        message = (
            f"HITL readiness report uses runtime contract {runtime_contract_id!r}; "
            f"this checkout requires {expected_runtime_contract_id!r}."
        )
        raise ValueError(message)
    release_identity = payload["releaseIdentity"]
    if not isinstance(release_identity, Mapping):
        message = "HITL readiness releaseIdentity must be a JSON object."
        raise ValueError(message)
    release_id = _text(release_identity.get("release_id"), "releaseIdentity.release_id")
    bundle_sha256 = _sha256_text(
        release_identity.get("bundle_sha256"),
        "releaseIdentity.bundle_sha256",
    )

    generated_at = _timestamp(payload["generatedAt"])
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        message = "Readiness validation time must be timezone-aware."
        raise ValueError(message)
    age = current_time.astimezone(timezone.utc) - generated_at
    if age < -timedelta(minutes=5):
        message = "HITL readiness report timestamp is in the future."
        raise ValueError(message)
    if age > HITL_READINESS_MAX_AGE:
        message = "HITL readiness report is older than four hours; rerun the no-motion preflight."
        raise ValueError(message)

    checks = payload["checks"]
    if not isinstance(checks, list) or not checks:
        message = "HITL readiness report has no check inventory."
        raise ValueError(message)
    if any(not isinstance(check, Mapping) or check.get("ok") is not True for check in checks):
        message = "HITL readiness report contains a failed or malformed check."
        raise ValueError(message)

    runtime = payload["runtime"]
    if not isinstance(runtime, Mapping):
        message = "HITL readiness runtime evidence must be a JSON object."
        raise ValueError(message)
    if runtime.get("runtime_contract_id") != runtime_contract_id:
        message = "HITL readiness runtime contract fields are internally inconsistent."
        raise ValueError(message)
    if runtime.get("release_identity") != release_identity:
        message = "HITL readiness release identity fields are internally inconsistent."
        raise ValueError(message)
    connector_version = _text(runtime.get("connector_version"), "runtime.connector_version")
    opentrons_version = _text(runtime.get("opentrons_version"), "runtime.opentrons_version")
    python_version = _text(runtime.get("python_version"), "runtime.python_version")

    return HitlReadinessEvidence(
        host=host,
        generated_at=generated_at,
        manifest_sha256=manifest_sha256,
        runtime_contract_id=runtime_contract_id,
        connector_version=connector_version,
        opentrons_version=opentrons_version,
        python_version=python_version,
        release_id=release_id,
        bundle_sha256=bundle_sha256,
        report_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        message = f"HITL readiness {label} must be a non-empty string."
        raise ValueError(message)
    return value.strip()


def _sha256_text(value: object, label: str) -> str:
    normalized = _text(value, label)
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        message = f"HITL readiness {label} must be a lowercase SHA-256 digest."
        raise ValueError(message)
    return normalized


def _timestamp(value: object) -> datetime:
    text = _text(value, "generatedAt")
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        message = "HITL readiness generatedAt must be an ISO-8601 timestamp."
        raise ValueError(message) from exc
    if timestamp.tzinfo is None:
        message = "HITL readiness generatedAt must include a timezone."
        raise ValueError(message)
    return timestamp.astimezone(timezone.utc)


__all__ = [
    "HITL_BLOCKED_STATUS",
    "HITL_HARDWARE_PASSED_STATUS",
    "HITL_OFFLINE_VALIDATED_STATUS",
    "HITL_READINESS_MAX_AGE",
    "HITL_READINESS_SCHEMA_VERSION",
    "HITL_READINESS_STATUS",
    "HitlReadinessEvidence",
    "load_hitl_readiness_evidence",
    "settle_hitl_operations",
]
