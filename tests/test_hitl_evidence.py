from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from unitelabs.opentrons_flex.hitl_evidence import load_hitl_readiness_evidence, settle_hitl_operations

_NOW = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)


def _report() -> dict:
    return {
        "schemaVersion": 1,
        "evidenceStatus": "READY_FOR_HITL",
        "hardwarePassed": False,
        "generatedAt": (_NOW - timedelta(minutes=5)).isoformat(),
        "host": "flex-lab",
        "manifestSha256": "a" * 64,
        "runtimeContractId": "flex-runtime-test",
        "releaseIdentity": {
            "release_id": "flex-0.9.1-test",
            "bundle_sha256": "b" * 64,
        },
        "runtime": {
            "connector_version": "0.9.1",
            "opentrons_version": "9.0.0",
            "python_version": "3.10.20",
            "runtime_contract_id": "flex-runtime-test",
            "release_identity": {
                "release_id": "flex-0.9.1-test",
                "bundle_sha256": "b" * 64,
            },
        },
        "checks": [{"name": "runtime", "ok": True, "detail": "ready"}],
    }


def test_ready_evidence_is_bound_to_report_bytes(tmp_path) -> None:
    path = tmp_path / "readiness.json"
    path.write_text(json.dumps(_report()), encoding="utf-8")

    evidence = load_hitl_readiness_evidence(
        path,
        expected_host="FLEX-LAB",
        expected_manifest_sha256="a" * 64,
        expected_runtime_contract_id="flex-runtime-test",
        now=_NOW,
    )

    assert len(evidence.report_sha256) == 64
    assert evidence.opentrons_version == "9.0.0"


def test_stale_evidence_is_rejected(tmp_path) -> None:
    report = _report()
    report["generatedAt"] = (_NOW - timedelta(hours=5)).isoformat()
    path = tmp_path / "readiness.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="older than four hours"):
        load_hitl_readiness_evidence(
            path,
            expected_host="flex-lab",
            expected_manifest_sha256="a" * 64,
            expected_runtime_contract_id="flex-runtime-test",
            now=_NOW,
        )


def test_hardware_pass_claim_is_rejected_from_readiness_report(tmp_path) -> None:
    report = _report()
    report["hardwarePassed"] = True
    path = tmp_path / "readiness.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="no-motion READY_FOR_HITL"):
        load_hitl_readiness_evidence(
            path,
            expected_host="flex-lab",
            expected_manifest_sha256="a" * 64,
            expected_runtime_contract_id="flex-runtime-test",
            now=_NOW,
        )


def test_internally_inconsistent_release_identity_is_rejected(tmp_path) -> None:
    report = _report()
    report["runtime"]["release_identity"]["bundle_sha256"] = "c" * 64
    path = tmp_path / "readiness.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="internally inconsistent"):
        load_hitl_readiness_evidence(
            path,
            expected_host="flex-lab",
            expected_manifest_sha256="a" * 64,
            expected_runtime_contract_id="flex-runtime-test",
            now=_NOW,
        )


@pytest.mark.asyncio
async def test_safety_settlement_attempts_every_operation_and_reports_failures() -> None:
    attempted = []

    async def pass_operation() -> object:
        attempted.append("pass")
        return object()

    async def fail_operation() -> object:
        attempted.append("fail")
        raise RuntimeError("not settled")

    errors = await settle_hitl_operations(
        (
            ("first", fail_operation),
            ("second", pass_operation),
        )
    )

    assert attempted == ["fail", "pass"]
    assert errors == ("first: RuntimeError: not settled",)
