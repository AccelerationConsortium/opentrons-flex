"""Analyze the complete AS-MS protocol through the connector's robot-server."""

from __future__ import annotations

import copy
import json
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_PROTOCOL = _ROOT / "protocols" / "asms" / "asms_single_point_wash_and_elute.py"
_LABWARE_DIR = _PROTOCOL.parent / "labware"
_LABWARE_DEFINITIONS = tuple(sorted(_LABWARE_DIR.glob("*.json")))


def _exact_protocol_files() -> list[tuple[str, tuple[str, bytes, str]]]:
    """Build the same exact protocol bundle that will be uploaded to a Flex."""
    files = [("files", (_PROTOCOL.name, _PROTOCOL.read_bytes(), "text/x-python"))]
    files.extend(
        ("files", (definition.name, definition.read_bytes(), "application/json")) for definition in _LABWARE_DEFINITIONS
    )
    return files


def _latest_analysis(protocol: dict) -> dict:
    analyses = protocol.get("analysisSummaries") or []
    assert analyses, f"Uploaded protocol did not create an analysis: {protocol}"
    return analyses[-1]


def _configure_asms_deck(http_client) -> list[dict]:
    """Mirror the operator-required slots, modules, and trash fixtures."""
    response = http_client.get("/deck_configuration")
    assert response.status_code == 200, response.text
    fixtures = response.json()["data"]["cutoutFixtures"]
    original_fixtures = copy.deepcopy(fixtures)
    fixture_by_cutout = {fixture["cutoutId"]: fixture for fixture in fixtures}
    fixture_by_cutout["cutoutA3"]["cutoutFixtureId"] = "singleRightSlot"
    fixture_by_cutout["cutoutD1"]["cutoutFixtureId"] = "trashBinAdapter"
    fixture_by_cutout["cutoutC1"].update(
        cutoutFixtureId="temperatureModuleV2",
        opentronsModuleSerialNumber="TM-SIM-1",
    )
    fixture_by_cutout["cutoutB2"]["cutoutFixtureId"] = "magneticBlockV1"

    response = http_client.put(
        "/deck_configuration",
        json={"data": {"cutoutFixtures": fixtures}},
    )
    assert response.status_code == 200, response.text
    return original_fixtures


@pytest.fixture
def configured_asms_deck(http_client):
    """Apply the AS-MS deck for one test and restore the session stack afterward."""
    original_fixtures = _configure_asms_deck(http_client)
    yield
    response = http_client.put(
        "/deck_configuration",
        json={"data": {"cutoutFixtures": original_fixtures}},
    )
    assert response.status_code == 200, response.text


@pytest.mark.smoketest_http_only
def test_asms_protocol_executes_through_embedded_protocol_engine(http_client, configured_asms_deck) -> None:
    """The connector HTTP path must analyze and execute the complete command graph."""
    runtime_parameters = {"connector_test_mode": True, "number_of_columns": 2}
    response = http_client.post(
        "/protocols",
        files=_exact_protocol_files(),
        data={"run_time_parameter_values": json.dumps(runtime_parameters)},
    )
    assert response.status_code in {200, 201}, response.text
    protocol = response.json()["data"]
    protocol_id = protocol["id"]

    deadline = time.monotonic() + 45
    analysis = _latest_analysis(protocol)
    while analysis["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.1)
        current = http_client.get(f"/protocols/{protocol_id}")
        assert current.status_code == 200, current.text
        analysis = _latest_analysis(current.json()["data"])

    assert analysis["status"] == "completed", analysis

    response = http_client.post(
        "/runs",
        json={
            "data": {
                "protocolId": protocol_id,
                "runTimeParameterValues": runtime_parameters,
            }
        },
    )
    assert response.status_code in {200, 201}, response.text
    run_id = response.json()["data"]["id"]

    response = http_client.post(
        f"/runs/{run_id}/actions",
        json={"data": {"actionType": "play"}},
    )
    assert response.status_code == 201, response.text

    deadline = time.monotonic() + 90
    run = http_client.get(f"/runs/{run_id}").json()["data"]
    while run["status"] not in {"succeeded", "failed", "stopped"} and time.monotonic() < deadline:
        time.sleep(0.1)
        current = http_client.get(f"/runs/{run_id}")
        assert current.status_code == 200, current.text
        run = current.json()["data"]

    assert run["status"] == "succeeded", {
        "status": run["status"],
        "errors": [
            {key: error.get(key) for key in ("errorType", "errorCode", "detail")} for error in run.get("errors", [])
        ],
        "current": run.get("current"),
    }

    response = http_client.get(f"/runs/{run_id}/commands", params={"pageLength": 500})
    assert response.status_code == 200, response.text
    commands = response.json()["data"]
    command_types = Counter(command["commandType"] for command in commands)
    assert len(commands) == 462
    assert command_types["moveLabware"] == 9
    assert command_types["pickUpTip"] == 26
    assert command_types["aspirate"] == 82
    assert command_types["dispense"] == 66
    assert command_types["temperatureModule/deactivate"] == 1


@pytest.mark.smoketest_http_only
def test_controlled_checkpoint_mutation_is_authenticated_audited_and_ordered(
    http_client,
    configured_asms_deck,
    simulator_stack,
) -> None:
    """A mutation must stay inside the paused PE queue and ahead of the next Python step."""
    runtime_parameters = {
        "connector_test_mode": True,
        "number_of_columns": 1,
        "enable_mutation_checkpoints": True,
    }
    response = http_client.post(
        "/protocols",
        files=_exact_protocol_files(),
        data={"run_time_parameter_values": json.dumps(runtime_parameters)},
    )
    assert response.status_code in {200, 201}, response.text
    protocol = response.json()["data"]
    protocol_id = protocol["id"]

    deadline = time.monotonic() + 45
    analysis = _latest_analysis(protocol)
    while analysis["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.1)
        analysis = _latest_analysis(http_client.get(f"/protocols/{protocol_id}").json()["data"])
    assert analysis["status"] == "completed", analysis

    response = http_client.post(
        "/runs",
        json={"data": {"protocolId": protocol_id, "runTimeParameterValues": runtime_parameters}},
    )
    assert response.status_code in {200, 201}, response.text
    run_id = response.json()["data"]["id"]
    auth = {"Authorization": f"Bearer {simulator_stack.mutation_token}"}
    prestart_bypass = http_client.post(
        f"/runs/{run_id}/commands",
        headers=auth,
        json={"data": {"commandType": "comment", "params": {"message": "unsafe pre-start bypass"}}},
    )
    assert prestart_bypass.status_code == 409
    assert "protocol-less" in prestart_bypass.text
    assert (
        http_client.post(
            f"/runs/{run_id}/actions",
            json={"data": {"actionType": "play"}},
        ).status_code
        == 201
    )

    deadline = time.monotonic() + 30
    run = http_client.get(f"/runs/{run_id}").json()["data"]
    while run["status"] != "paused" and time.monotonic() < deadline:
        time.sleep(0.1)
        run = http_client.get(f"/runs/{run_id}").json()["data"]
    assert run["status"] == "paused", run

    mutation_url = f"/unitelabs/runs/{run_id}"
    assert http_client.get(f"{mutation_url}/mutations").status_code == 401
    assert (
        http_client.get(
            f"{mutation_url}/mutations",
            headers={"Authorization": "Bearer wrong-token"},
        ).status_code
        == 401
    )
    snapshot_response = http_client.get(f"{mutation_url}/mutation-snapshot", headers=auth)
    assert snapshot_response.status_code == 200, snapshot_response.text
    snapshot = snapshot_response.json()
    assert snapshot["status"] == "paused"
    assert snapshot["currentCommand"]["commandType"] == "waitForResume"
    assert snapshot["currentCommand"]["params"]["message"].startswith("UNITELABS_MUTATION_CHECKPOINT:")
    assert snapshot["tipRacks"]
    assert snapshot["pipettes"]
    assert snapshot["labware"]
    assert snapshot["modules"]
    assert snapshot["disposalAreas"]
    assert snapshot["wells"]
    loaded_names = {item["loadName"] for item in snapshot["labware"]}
    assert "azenta_96_wellplate_200ul_pcr" in loaded_names
    assert "thermokingfisherdeepwell_96_wellplate_2000ul" in loaded_names

    raw = http_client.post(
        f"/runs/{run_id}/commands",
        json={"data": {"commandType": "comment", "params": {"message": "unsafe bypass"}}},
    )
    assert raw.status_code == 409

    def labware_id(load_name: str) -> str:
        return next(item["id"] for item in snapshot["labware"] if item["loadName"] == load_name)

    source_id = labware_id("nest_12_reservoir_22ml")
    destination_id = labware_id("thermokingfisherdeepwell_96_wellplate_2000ul")
    disposal_name = next(
        area["addressableAreaName"] for area in snapshot["disposalAreas"] if area["areaType"] == "movableTrash"
    )
    pipette_id = snapshot["pipettes"][0]["id"]
    tip_rack_id = next(iter(snapshot["tipRacks"]))
    inserted_message = "OFFLINE AUDITED MUTATION"
    mutation_id = str(uuid4())
    mutation_body = {
        "mutationId": mutation_id,
        "actor": "offline-integration-test",
        "reason": "prove checkpoint queue ordering",
        "mode": "checkpoint",
        "steps": [
            {
                "stepType": "transfer",
                "pipetteId": pipette_id,
                "tipRackIds": [tip_rack_id],
                "source": {"labwareId": source_id, "wellName": "A2"},
                "destination": {"labwareId": destination_id, "wellName": "A1"},
                "disposal": {
                    "disposalType": "addressableArea",
                    "addressableAreaName": disposal_name,
                },
                "volume": 10,
                "aspirateFlowRate": 10,
                "dispenseFlowRate": 10,
            },
            {"stepType": "comment", "message": inserted_message},
        ],
    }
    wrong_actor_body = {**mutation_body, "actor": "self-declared-impersonation"}
    wrong_actor = http_client.post(
        f"{mutation_url}/mutations",
        headers=auth,
        json=wrong_actor_body,
    )
    assert wrong_actor.status_code == 403, wrong_actor.text

    response = http_client.post(f"{mutation_url}/mutations", headers=auth, json=mutation_body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["resourceSnapshotBefore"] == snapshot["fingerprint"]
    assert len(result["commandIds"]) == 6
    assert result["allocatedTips"][0]["tipCount"] == 8
    assert result["predictedWellVolumes"][f"{source_id}/A2"] == 3920
    for row in "ABCDEFGH":
        assert result["predictedWellVolumes"][f"{destination_id}/{row}1"] == 10

    audit = http_client.get(f"{mutation_url}/mutations", headers=auth)
    assert audit.status_code == 200, audit.text
    audit_records = audit.json()
    audit_events = [record["event"] for record in audit_records]
    assert audit_events[:2] == ["mutation_requested", "mutation_approved"]
    assert audit_events[2:-1] == ["mutation_commands_enqueued"]
    assert audit_records[2]["commandIds"] == result["commandIds"]
    assert audit_events[-1] == "mutation_enqueued"
    assert all(record["actor"] == "offline-integration-test" for record in audit_records)

    response = http_client.post(
        f"/runs/{run_id}/actions",
        headers=auth,
        json={"data": {"actionType": "play"}},
    )
    assert response.status_code == 201, response.text

    deadline = time.monotonic() + 30
    inserted = []
    commands = []
    while time.monotonic() < deadline:
        commands_response = http_client.get(f"/runs/{run_id}/commands", params={"pageLength": 1000})
        assert commands_response.status_code == 200, commands_response.text
        commands = commands_response.json()["data"]
        inserted = [command for command in commands if command["id"] in result["commandIds"]]
        run = http_client.get(f"/runs/{run_id}").json()["data"]
        if (
            len(inserted) == len(result["commandIds"])
            and all(command["status"] == "succeeded" for command in inserted)
            and run["status"] == "paused"
        ):
            break
        time.sleep(0.1)

    assert len(inserted) == 6 and all(command["status"] == "succeeded" for command in inserted), inserted
    checkpoint_index = next(
        index for index, command in enumerate(commands) if command["id"] == result["checkpointCommandId"]
    )
    inserted_indices = [
        next(index for index, command in enumerate(commands) if command["id"] == command_id)
        for command_id in result["commandIds"]
    ]
    next_protocol_index = next(
        index
        for index, command in enumerate(commands)
        if command["commandType"] == "comment"
        and command["params"].get("message") == "Separate initial AS-MS supernatant"
    )
    assert checkpoint_index < min(inserted_indices)
    assert max(inserted_indices) < next_protocol_index

    stop = http_client.post(f"/runs/{run_id}/actions", json={"data": {"actionType": "stop"}})
    assert stop.status_code == 201, stop.text
    deadline = time.monotonic() + 30
    run = http_client.get(f"/runs/{run_id}").json()["data"]
    while run["status"] not in {"succeeded", "failed", "stopped"} and time.monotonic() < deadline:
        time.sleep(0.1)
        run = http_client.get(f"/runs/{run_id}").json()["data"]
    assert run["status"] == "stopped", run
