from __future__ import annotations

import json
from types import SimpleNamespace

from unitelabs.opentrons_flex import robot_readiness


def test_robot_readiness_combines_runtime_with_loopback_inventory(monkeypatch, capsys, tmp_path) -> None:
    runtime = {
        "connector_version": "0.9.1",
        "opentrons_version": "9.0.0",
        "runtime_contract_id": "flex-runtime-test",
        "release_identity": {
            "release_id": "flex-0.9.1-test",
            "bundle_sha256": "b" * 64,
        },
    }
    monkeypatch.setattr(
        robot_readiness.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(runtime),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        robot_readiness,
        "_loopback_json",
        lambda _port, path, _timeout: (
            {
                "runtimeContractId": runtime["runtime_contract_id"],
                "connectorVersion": runtime["connector_version"],
                "opentronsVersion": "9.0.0",
                "releaseIdentity": runtime["release_identity"],
            }
            if path == "/unitelabs/runtime"
            else {"path": path}
        ),
    )

    result = robot_readiness.main(
        [
            "--config",
            str(tmp_path / "config.json"),
            "--require-mutation",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"] == runtime
    assert set(payload["http"]) == set(robot_readiness._HTTP_PATHS)


def test_robot_readiness_fails_when_loopback_inventory_is_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        robot_readiness.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"connector_version": "0.9.1"}),
            stderr="",
        ),
    )

    def _fail(*_args) -> object:
        raise RuntimeError("loopback unavailable")

    monkeypatch.setattr(robot_readiness, "_loopback_json", _fail)

    assert robot_readiness.main(["--config", str(tmp_path / "config.json")]) == 1


def test_robot_readiness_rejects_running_service_from_another_release(
    monkeypatch,
    tmp_path,
) -> None:
    runtime = {
        "connector_version": "0.9.1",
        "opentrons_version": "9.0.0",
        "runtime_contract_id": "flex-runtime-test",
        "release_identity": {
            "release_id": "release-a",
            "bundle_sha256": "a" * 64,
        },
    }
    monkeypatch.setattr(
        robot_readiness.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(runtime),
            stderr="",
        ),
    )

    def _loopback(_port, path, _timeout) -> object:
        if path == "/unitelabs/runtime":
            return {
                "runtimeContractId": runtime["runtime_contract_id"],
                "connectorVersion": runtime["connector_version"],
                "opentronsVersion": runtime["opentrons_version"],
                "releaseIdentity": {
                    "release_id": "release-b",
                    "bundle_sha256": "b" * 64,
                },
            }
        return {"path": path}

    monkeypatch.setattr(robot_readiness, "_loopback_json", _loopback)

    assert robot_readiness.main(["--config", str(tmp_path / "config.json")]) == 1
