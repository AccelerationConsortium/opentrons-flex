from __future__ import annotations

import json

from unitelabs.opentrons_flex import runtime_preflight
from unitelabs.opentrons_flex.runtime_compat import RuntimeCompatibilityReport


def _report(*, compatible: bool = True) -> RuntimeCompatibilityReport:
    return RuntimeCompatibilityReport(
        connector_version="0.9.1",
        python_version="3.10.20",
        opentrons_version="9.0.0",
        robot_server_version="9.0.0",
        robot_server_source="/release/robot_server/__init__.py",
        runtime_package_versions={},
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
