from __future__ import annotations

import json

import pytest

from scripts import preflight_flex
from tests.test_acceptance_manifest import _manifest
from unitelabs.opentrons_flex.hitl_evidence import (
    HITL_BLOCKED_STATUS,
    HITL_READINESS_STATUS,
    load_hitl_readiness_evidence,
)
from unitelabs.opentrons_flex.runtime_compat import RUNTIME_CONTRACT_ID

_RELEASE_IDENTITY = {
    "release_id": "flex-0.9.1-ot9.0.0-py3.12-aarch64-bbbbbbbbbbbb",
    "bundle_sha256": "b" * 64,
}


def _runtime() -> dict:
    return {
        "connector_version": "0.9.1",
        "opentrons_version": "9.0.0",
        "python_version": "3.12.11",
        "runtime_contract_id": RUNTIME_CONTRACT_ID,
        "mutation_ready": True,
        "release_identity": _RELEASE_IDENTITY,
    }


def _http_payload(path: str) -> object:
    if path == "/pipettes":
        return {"left": {"serialNumber": "PIP-001", "name": "flex_8channel_1000"}}
    if path == "/modules":
        return {
            "data": [
                {"serialNumber": serial, "moduleModel": model}
                for serial, model in (
                    ("HS-001", "heaterShakerModuleV1"),
                    ("TC-001", "thermocyclerModuleV2"),
                    ("TM-001", "temperatureModuleV2"),
                    ("AR-001", "absorbanceReaderV1"),
                    ("FS-001", "flexStackerModuleV1"),
                )
            ]
        }
    if path == "/openapi.json":
        return {"paths": {"/unitelabs/runs/{run_id}/mutations": {}}}
    return {"data": {}}


def _runtime_manifest() -> dict:
    return {
        "schemaVersion": 1,
        "releaseId": _RELEASE_IDENTITY["release_id"],
        "connectorVersion": "0.9.1",
        "opentronsVersion": "9.0.0",
        "robotServerVersion": "9.0.0",
        "opentronsSourceCommit": "44b37a2f91520bf2e7245c70bf799d46c8c2d9a5",
        "pythonVersion": "3.12",
        "architecture": "aarch64",
        "bundleSha256": _RELEASE_IDENTITY["bundle_sha256"],
    }


def _patch_live_checks(monkeypatch) -> None:
    monkeypatch.setattr(
        preflight_flex,
        "_remote_runtime",
        lambda _: (
            preflight_flex.Check("remote-runtime", True, "ready"),
            _runtime(),
            {
                path: _http_payload(path)
                for path in ("/health", "/deck_configuration", "/pipettes", "/modules", "/openapi.json")
            },
        ),
    )
    monkeypatch.setattr(
        preflight_flex,
        "_tcp_check",
        lambda *_: preflight_flex.Check("tcp:50051", True, "ready"),
    )
    monkeypatch.setattr(
        preflight_flex,
        "_http_json",
        lambda _host, _port, path, _timeout: (
            preflight_flex.Check(f"http:{path}", True, "ready"),
            _http_payload(path),
        ),
    )


def test_acceptance_preflight_writes_cross_platform_ready_evidence(tmp_path, monkeypatch, capsys) -> None:
    _patch_live_checks(monkeypatch)
    manifest_payload = _manifest()
    manifest_path = tmp_path / "acceptance.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    output_path = tmp_path / "readiness.json"
    runtime_manifest_path = tmp_path / "runtime-manifest.json"
    runtime_manifest_path.write_text(json.dumps(_runtime_manifest()), encoding="utf-8")

    result = preflight_flex.main(
        [
            manifest_payload["expected_robot_host"],
            "--acceptance-manifest",
            str(manifest_path),
            "--runtime-manifest",
            str(runtime_manifest_path),
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["evidenceStatus"] == HITL_READINESS_STATUS
    assert report["hardwarePassed"] is False
    evidence = load_hitl_readiness_evidence(
        output_path,
        expected_host=manifest_payload["expected_robot_host"],
        expected_manifest_sha256=report["manifestSha256"],
        expected_runtime_contract_id=RUNTIME_CONTRACT_ID,
    )
    assert evidence.connector_version == "0.9.1"


def test_acceptance_preflight_blocks_manifest_target_mismatch(tmp_path, monkeypatch, capsys) -> None:
    _patch_live_checks(monkeypatch)
    manifest_payload = _manifest()
    manifest_path = tmp_path / "acceptance.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    runtime_manifest_path = tmp_path / "runtime-manifest.json"
    runtime_manifest_path.write_text(json.dumps(_runtime_manifest()), encoding="utf-8")

    result = preflight_flex.main(
        [
            "192.0.2.99",
            "--acceptance-manifest",
            str(manifest_path),
            "--runtime-manifest",
            str(runtime_manifest_path),
        ]
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["evidenceStatus"] == HITL_BLOCKED_STATUS
    assert next(check for check in report["checks"] if check["name"] == "acceptance-host")["ok"] is False


def test_general_runtime_preflight_keeps_legacy_success_without_manifest(monkeypatch, capsys) -> None:
    _patch_live_checks(monkeypatch)

    assert preflight_flex.main(["192.0.2.10"]) == 0
    assert json.loads(capsys.readouterr().out)["evidenceStatus"] == "RUNTIME_READY"


def test_ssh_option_injection_is_rejected_by_cli_parser() -> None:
    with pytest.raises(SystemExit):
        preflight_flex._parser().parse_args(["192.0.2.10", "--ssh-user=-oProxyCommand=bad"])


def test_acceptance_preflight_rejects_runtime_manifest_from_another_bundle(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _patch_live_checks(monkeypatch)
    manifest_payload = _manifest()
    manifest_path = tmp_path / "acceptance.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    runtime_manifest = _runtime_manifest()
    runtime_manifest["releaseId"] = "flex-0.9.1-unrelated"
    runtime_manifest_path = tmp_path / "runtime-manifest.json"
    runtime_manifest_path.write_text(json.dumps(runtime_manifest), encoding="utf-8")

    result = preflight_flex.main(
        [
            manifest_payload["expected_robot_host"],
            "--acceptance-manifest",
            str(manifest_path),
            "--runtime-manifest",
            str(runtime_manifest_path),
        ]
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["evidenceStatus"] == HITL_BLOCKED_STATUS
    assert next(check for check in report["checks"] if check["name"] == "acceptance-manifest")["ok"] is False
