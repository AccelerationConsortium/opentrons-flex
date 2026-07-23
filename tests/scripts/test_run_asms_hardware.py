from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from scripts import run_asms_hardware


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.is_success = 200 <= status_code < 300

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, *args, **kwargs) -> None:
        self.posts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def get(self, path: str, *, params: dict | None = None) -> _Response:
        if path == "/health":
            return _Response(200, {"robot_model": "OT-3 Standard"})
        if path == "/pipettes":
            return _Response(200, {"left": {}, "right": {"name": "flex_8channel_1000"}})
        if path == "/modules":
            return _Response(200, {"data": [{"moduleModel": "temperatureModuleV2", "serialNumber": "TM-123"}]})
        if path == "/deck_configuration":
            return _Response(200, _ready_deck())
        if path == "/runs/run-1":
            return _Response(200, {"data": {"id": "run-1", "status": "succeeded", "current": None}})
        if path == "/runs/run-1/commands":
            commands = []
            for command_type, count in run_asms_hardware._EXPECTED_TWO_COLUMN_COMMANDS.items():
                commands.extend({"commandType": command_type} for _ in range(count))
            commands.extend({"commandType": "other"} for _ in range(462 - len(commands)))
            return _Response(200, {"data": commands})
        raise AssertionError(f"Unexpected GET {path} params={params}")

    def post(
        self,
        path: str,
        *,
        files: list | None = None,
        data: dict | None = None,
        json: dict | None = None,
    ) -> _Response:
        self.posts.append(path)
        if path == "/protocols":
            assert files is not None
            assert data is not None
            return _Response(
                201,
                {
                    "data": {
                        "id": "protocol-1",
                        "analysisSummaries": [{"id": "analysis-1", "status": "completed"}],
                    }
                },
            )
        if path == "/runs":
            assert json is not None
            return _Response(201, {"data": {"id": "run-1", "status": "idle"}})
        if path == "/runs/run-1/actions":
            assert json == {"data": {"actionType": "play"}}
            return _Response(201, {"data": {"id": "action-1"}})
        raise AssertionError(f"Unexpected POST {path}")


def _ready_deck() -> dict:
    return {
        "data": {
            "cutoutFixtures": [
                {"cutoutId": "cutoutA3", "cutoutFixtureId": "singleRightSlot"},
                {"cutoutId": "cutoutB2", "cutoutFixtureId": "magneticBlockV1"},
                {
                    "cutoutId": "cutoutC1",
                    "cutoutFixtureId": "temperatureModuleV2",
                    "opentronsModuleSerialNumber": "TM-123",
                },
                {"cutoutId": "cutoutD1", "cutoutFixtureId": "trashBinAdapter"},
            ]
        }
    }


def test_runtime_parameters_default_to_short_non_mutating_mechanics_run() -> None:
    assert run_asms_hardware._runtime_parameters(columns=1, scientific=False) == {
        "connector_test_mode": True,
        "number_of_columns": 1,
        "enable_mutation_checkpoints": False,
    }


def test_runtime_parameters_preserve_full_scientific_mode() -> None:
    assert run_asms_hardware._runtime_parameters(columns=2, scientific=True) == {
        "connector_test_mode": False,
        "number_of_columns": 2,
        "enable_mutation_checkpoints": False,
    }


def test_exact_upload_bundle_contains_protocol_and_two_labware_definitions() -> None:
    files = run_asms_hardware._exact_protocol_files()
    names = [file_info[1][0] for file_info in files]
    assert names == [
        "asms_single_point_wash_and_elute.py",
        "azenta_96_wellplate_200ul_pcr.json",
        "thermokingfisherdeepwell_96_wellplate_2000ul.json",
    ]


def test_ready_deck_has_no_errors() -> None:
    assert run_asms_hardware._deck_configuration_errors(_ready_deck()) == []


def test_deck_validation_reports_wrong_fixture_and_missing_module_serial() -> None:
    deck = _ready_deck()
    fixtures = deck["data"]["cutoutFixtures"]
    fixtures[0]["cutoutFixtureId"] = "trashBinAdapter"
    fixtures[2].pop("opentronsModuleSerialNumber")

    assert run_asms_hardware._deck_configuration_errors(deck) == [
        "cutoutA3: expected singleRightSlot, found trashBinAdapter",
        "cutoutC1: Temperature Module serial number is missing",
    ]


def test_hardware_inventory_requires_right_flex_pipette_and_temperature_module() -> None:
    assert (
        run_asms_hardware._hardware_inventory_errors(
            {"right": {"name": "flex_8channel_1000"}},
            {"data": [{"moduleModel": "temperatureModuleV2"}]},
        )
        == []
    )
    assert run_asms_hardware._hardware_inventory_errors(
        {"right": {"name": "flex_1channel_1000"}},
        {"data": []},
    ) == [
        "right pipette: expected flex_8channel_1000, found flex_1channel_1000",
        "Temperature Module GEN2 is not connected",
    ]


def test_two_column_evidence_requires_the_offline_pinned_counts() -> None:
    counts = Counter(run_asms_hardware._EXPECTED_TWO_COLUMN_COMMANDS)
    run_asms_hardware._verify_two_column_evidence(462, counts)

    with pytest.raises(RuntimeError, match="aspirate: expected 82, found 81"):
        counts["aspirate"] = 81
        run_asms_hardware._verify_two_column_evidence(462, counts)


def test_execute_requires_explicit_deck_confirmation(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_asms_hardware, "_validate_exact_bundle", lambda: None)

    result = run_asms_hardware.main(["--execute"])

    assert result == 2
    assert "ASMS-DECK-READY" in capsys.readouterr().err


def test_default_mode_analyzes_without_creating_or_playing_a_run(monkeypatch, capsys) -> None:
    client = _Client()
    monkeypatch.setattr(run_asms_hardware, "_validate_exact_bundle", lambda: None)
    monkeypatch.setattr(run_asms_hardware.httpx, "Client", lambda *args, **kwargs: client)

    result = run_asms_hardware.main([])

    assert result == 0
    assert client.posts == ["/protocols"]
    assert "ANALYSIS ONLY" in capsys.readouterr().out


def test_confirmed_two_column_execution_requires_pinned_command_evidence(monkeypatch, capsys) -> None:
    client = _Client()
    monkeypatch.setattr(run_asms_hardware, "_validate_exact_bundle", lambda: None)
    monkeypatch.setattr(run_asms_hardware.httpx, "Client", lambda *args, **kwargs: client)

    result = run_asms_hardware.main(
        [
            "--columns",
            "2",
            "--execute",
            "--confirm-deck-ready",
            "ASMS-DECK-READY",
        ]
    )

    assert result == 0
    assert client.posts == ["/protocols", "/runs", "/runs/run-1/actions"]
    assert "AS-MS hardware workflow PASS" in capsys.readouterr().out
