from __future__ import annotations

import json

from unitelabs.opentrons_flex import runtime_identity, runtime_preflight
from unitelabs.opentrons_flex.runtime_compat import RuntimeCompatibilityReport


def _report(*, compatible: bool = True) -> RuntimeCompatibilityReport:
    return RuntimeCompatibilityReport(
        connector_version="0.9.1",
        python_version="3.12.11",
        opentrons_version="9.0.0",
        robot_server_version="9.0.0",
        robot_server_source="/release/robot_server/__init__.py",
        runtime_package_versions={},
        runtime_contract_id="flex-runtime-test",
        private_api_checks=(),
        base_compatible=compatible,
        mutation_compatible=compatible,
        issues=() if compatible else ("base mismatch",),
        mutation_issues=() if compatible else ("base mismatch",),
    )


def test_runtime_preflight_requires_mutation_when_configured(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "with_robot_server": True,
                "run_mutation_required": True,
                "run_mutation_ledger_path": str(tmp_path / "ledger.jsonl"),
                "sila_server": {"version": "0.9.1"},
            }
        )
    )
    monkeypatch.setattr(runtime_preflight, "inspect_runtime_compatibility", lambda **_: _report())
    monkeypatch.delenv("UNITELABS_RUN_MUTATION_TOKEN", raising=False)
    monkeypatch.delenv("UNITELABS_RUN_MUTATION_ACTOR", raising=False)

    assert runtime_preflight.main(["--config", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["base_compatible"] is True
    assert payload["mutation_ready"] is False


def test_runtime_preflight_passes_complete_full_workflow_config(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "with_robot_server": True,
                "run_mutation_required": True,
                "run_mutation_ledger_path": str(tmp_path / "ledger.jsonl"),
                "sila_server": {"version": "0.9.1"},
            }
        )
    )
    monkeypatch.setattr(runtime_preflight, "inspect_runtime_compatibility", lambda **_: _report())
    monkeypatch.setenv("UNITELABS_RUN_MUTATION_TOKEN", "t" * 32)
    monkeypatch.setenv("UNITELABS_RUN_MUTATION_ACTOR", "operator-1")

    assert runtime_preflight.main(["--config", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mutation_ready"] is True


def test_runtime_preflight_fails_incompatible_base_even_when_mutation_optional(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sila_server": {"version": "0.9.1"}}))
    monkeypatch.setattr(runtime_preflight, "inspect_runtime_compatibility", lambda **_: _report(compatible=False))

    assert runtime_preflight.main(["--config", str(config)]) == 1


def test_runtime_preflight_rejects_simulator_for_live_deployment(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "with_robot_server": True,
                "use_simulator": True,
                "sila_server": {"version": "0.9.1"},
            }
        )
    )
    monkeypatch.setattr(runtime_preflight, "inspect_runtime_compatibility", lambda **_: _report())
    monkeypatch.setattr(
        runtime_preflight,
        "release_identity",
        lambda **_: ({"release_id": "test", "bundle_sha256": "a" * 64}, ()),
    )

    result = runtime_preflight.main(
        [
            "--config",
            str(config),
            "--require-robot-server",
            "--require-live-hardware",
        ]
    )

    assert result == 1
    assert "use_simulator must be false" in capsys.readouterr().out


def test_runtime_preflight_passes_connector_unit_config(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "unit-config.json"
    config.write_text(
        json.dumps(
            {
                "with_robot_server": False,
                "use_simulator": False,
                "run_mutation_required": False,
                "run_mutation_ledger_path": None,
                "labware_movement_config": "/var/sila2_flex/asms-unit-labware-movement.json",
                "sila_server": {"version": "0.9.1", "hostname": "127.0.0.1"},
            }
        )
    )
    monkeypatch.setattr(runtime_preflight, "inspect_runtime_compatibility", lambda **_: _report())
    monkeypatch.setattr(
        runtime_preflight,
        "release_identity",
        lambda **_: ({"release_id": "test", "bundle_sha256": "a" * 64}, ()),
    )

    result = runtime_preflight.main(
        [
            "--config",
            str(config),
            "--require-sila-only",
            "--require-live-hardware",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["configuration_issues"] == []


def test_runtime_preflight_rejects_robot_server_in_connector_unit_mode(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "unit-config.json"
    config.write_text(
        json.dumps(
            {
                "with_robot_server": True,
                "use_simulator": False,
                "run_mutation_required": True,
                "run_mutation_ledger_path": "/var/lib/unitelabs-opentrons-flex/mutations.jsonl",
                "labware_movement_config": "relative.json",
                "sila_server": {"version": "0.9.1", "hostname": "0.0.0.0"},
            }
        )
    )
    monkeypatch.setattr(runtime_preflight, "inspect_runtime_compatibility", lambda **_: _report())

    result = runtime_preflight.main(["--config", str(config), "--require-sila-only"])

    assert result == 1
    output = capsys.readouterr().out
    assert "with_robot_server must be false" in output
    assert "run_mutation_required must be false" in output
    assert "run_mutation_ledger_path must be null" in output
    assert "labware_movement_config must be an absolute path" in output
    assert "sila_server.hostname must be 127.0.0.1" in output


def test_release_identity_binds_active_venv_to_verified_bundle(tmp_path, monkeypatch) -> None:
    bundle_sha256 = "a" * 64
    architecture = "aarch64"
    python_version = f"{runtime_preflight.sys.version_info.major}.{runtime_preflight.sys.version_info.minor}"
    release_id = f"flex-0.9.1-ot9.0.0-py{python_version}-{architecture}-{bundle_sha256[:12]}"
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "connectorVersion": "0.9.1",
                "opentronsVersion": "9.0.0",
                "robotServerVersion": "9.0.0",
                "opentronsSourceCommit": "44b37a2f91520bf2e7245c70bf799d46c8c2d9a5",
                "pythonVersion": python_version,
                "architecture": architecture,
                "releaseId": release_id,
                "bundleSha256": bundle_sha256,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_identity.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(runtime_identity, "normalized_architecture", lambda: architecture)

    identity, issues = runtime_identity.release_identity(
        connector_version="0.9.1",
        opentrons_version="9.0.0",
        required=True,
    )

    assert issues == ()
    assert identity == {
        "release_id": release_id,
        "bundle_sha256": bundle_sha256,
    }


def test_release_identity_rejects_manifest_from_different_active_bundle(tmp_path, monkeypatch) -> None:
    manifest = {
        "schemaVersion": 1,
        "connectorVersion": "0.9.1",
        "opentronsVersion": "9.0.0",
        "robotServerVersion": "9.0.0",
        "opentronsSourceCommit": "44b37a2f91520bf2e7245c70bf799d46c8c2d9a5",
        "pythonVersion": f"{runtime_identity.sys.version_info.major}.{runtime_identity.sys.version_info.minor}",
        "architecture": "aarch64",
        "releaseId": "flex-0.9.1-unrelated",
        "bundleSha256": "a" * 64,
    }
    (tmp_path / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(runtime_identity.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(runtime_identity, "normalized_architecture", lambda: "aarch64")

    identity, issues = runtime_identity.release_identity(
        connector_version="0.9.1",
        opentrons_version="9.0.0",
        required=True,
    )

    assert identity is None
    assert any("releaseId" in issue for issue in issues)
