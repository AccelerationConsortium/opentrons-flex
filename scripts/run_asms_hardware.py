#!/usr/bin/env python3
"""Upload, analyze, and explicitly execute the prepared AS-MS protocol on a Flex."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import httpx

from unitelabs.opentrons_flex.protocol_preflight import inspect_protocol

_ROOT = Path(__file__).resolve().parents[1]
_PROTOCOL = _ROOT / "protocols" / "asms" / "asms_single_point_wash_and_elute.py"
_LABWARE_DIR = _PROTOCOL.parent / "labware"
_LABWARE_DEFINITIONS = tuple(sorted(_LABWARE_DIR.glob("*.json")))
_HTTP_API_VERSION_HEADER = "Opentrons-Version"
_EXECUTION_CONFIRMATION = "ASMS-DECK-READY"
_TERMINAL_RUN_STATES = {"succeeded", "failed", "stopped"}
_TERMINAL_ANALYSIS_STATES = {"completed", "failed"}
_EXPECTED_LABWARE_HASHES = {
    "azenta_96_wellplate_200ul_pcr": "43506d5482e3dfebff377e56b709150c81415473efc9cf2c6362dc4b68a1e20f",
    "thermokingfisherdeepwell_96_wellplate_2000ul": (
        "2ea9c15468816ace3970fe497cef7e1dc22d5f9ab033656bf9472a62396dfb47"
    ),
}
_EXPECTED_DECK_FIXTURES = {
    "cutoutA3": "singleRightSlot",
    "cutoutB2": "magneticBlockV1",
    "cutoutC1": "temperatureModuleV2",
    "cutoutD1": "trashBinAdapter",
}
_EXPECTED_TWO_COLUMN_COMMANDS = {
    "moveLabware": 9,
    "pickUpTip": 26,
    "aspirate": 82,
    "dispense": 66,
    "temperatureModule/deactivate": 1,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the exact AS-MS bundle through the connector's embedded robot-server. "
            "The default uploads and analyzes without moving hardware. Execution requires "
            f"--execute and --confirm-deck-ready {_EXECUTION_CONFIRMATION}."
        )
    )
    parser.add_argument("--host", default="169.254.105.239", help="Flex IP or hostname.")
    parser.add_argument("--port", type=int, default=31950, help="Connector robot-server HTTP port.")
    parser.add_argument("--columns", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--scientific",
        action="store_true",
        help="Use full scientific delays. The default connector test mode shortens delays.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create and play the analyzed run. Omit this flag for analysis-only mode.",
    )
    parser.add_argument(
        "--confirm-deck-ready",
        default=None,
        metavar="PHRASE",
        help=f"Required execution confirmation phrase: {_EXECUTION_CONFIRMATION}",
    )
    parser.add_argument("--analysis-timeout", type=float, default=120.0)
    parser.add_argument("--run-timeout", type=float, default=3600.0)
    return parser


def _runtime_parameters(*, columns: int, scientific: bool) -> dict[str, bool | int]:
    return {
        "connector_test_mode": not scientific,
        "number_of_columns": columns,
        "enable_mutation_checkpoints": False,
    }


def _exact_protocol_files() -> list[tuple[str, tuple[str, bytes, str]]]:
    files = [("files", (_PROTOCOL.name, _PROTOCOL.read_bytes(), "text/x-python"))]
    files.extend(
        ("files", (definition.name, definition.read_bytes(), "application/json")) for definition in _LABWARE_DEFINITIONS
    )
    return files


def _latest_analysis(protocol: dict) -> dict | None:
    analyses = protocol.get("analysisSummaries") or []
    return analyses[-1] if analyses else None


def _deck_configuration_errors(deck_response: dict) -> list[str]:
    fixtures = deck_response.get("data", {}).get("cutoutFixtures", [])
    fixture_by_cutout = {fixture.get("cutoutId"): fixture for fixture in fixtures}
    errors = []
    for cutout, expected_fixture in _EXPECTED_DECK_FIXTURES.items():
        actual = fixture_by_cutout.get(cutout, {}).get("cutoutFixtureId")
        if actual != expected_fixture:
            errors.append(f"{cutout}: expected {expected_fixture}, found {actual or 'missing'}")

    temperature_fixture = fixture_by_cutout.get("cutoutC1", {})
    if not temperature_fixture.get("opentronsModuleSerialNumber"):
        errors.append("cutoutC1: Temperature Module serial number is missing")
    return errors


def _hardware_inventory_errors(pipettes: dict, modules: dict) -> list[str]:
    errors = []
    right = pipettes.get("right")
    if not isinstance(right, dict):
        errors.append("right pipette mount is empty")
    elif right.get("name") != "flex_8channel_1000":
        errors.append(f"right pipette: expected flex_8channel_1000, found {right.get('name') or 'unknown'}")

    module_items = modules.get("data")
    if not isinstance(module_items, list):
        errors.append("module inventory response is invalid")
    elif not any(
        module.get("moduleModel") == "temperatureModuleV2" or module.get("model") == "temperatureModuleV2"
        for module in module_items
    ):
        errors.append("Temperature Module GEN2 is not connected")
    return errors


def _validate_exact_bundle() -> None:
    report = inspect_protocol(
        _PROTOCOL,
        custom_labware_paths=[_LABWARE_DIR],
        expected_custom_labware_hashes=_EXPECTED_LABWARE_HASHES,
    )
    if not report.simulation_completed or not report.exact_bundle_ready:
        raise RuntimeError(f"Exact AS-MS preflight failed: {report.to_dict()}")
    print(
        "Exact preflight READY: "
        f"{report.command_count} records, {report.tip_pickups} tip pickups, "
        f"{report.gripper_moves} gripper moves"
    )


def _response_data(response: httpx.Response, *, action: str) -> dict:
    if not response.is_success:
        raise RuntimeError(f"{action} failed with HTTP {response.status_code}: {response.text}")
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"{action} returned an unexpected response: {payload}")
    return data


def _wait_for_analysis(
    client: httpx.Client,
    protocol: dict,
    *,
    timeout: float,
) -> dict:
    protocol_id = protocol["id"]
    deadline = time.monotonic() + timeout
    analysis = _latest_analysis(protocol)
    while (analysis is None or analysis.get("status") not in _TERMINAL_ANALYSIS_STATES) and time.monotonic() < deadline:
        time.sleep(0.5)
        protocol = _response_data(client.get(f"/protocols/{protocol_id}"), action="protocol analysis poll")
        analysis = _latest_analysis(protocol)
    if analysis is None or analysis.get("status") not in _TERMINAL_ANALYSIS_STATES:
        raise TimeoutError(f"Protocol analysis did not finish within {timeout:g} seconds")
    return analysis


def _wait_for_run(client: httpx.Client, run_id: str, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        run = _response_data(client.get(f"/runs/{run_id}"), action="run status poll")
        status = run.get("status")
        current = run.get("current")
        marker = (status, json.dumps(current, sort_keys=True))
        if marker != previous:
            print(f"Run {run_id}: status={status}, current={current}")
            previous = marker
        if status in _TERMINAL_RUN_STATES:
            return run
        time.sleep(1)
    raise TimeoutError(f"Run {run_id} did not finish within {timeout:g} seconds")


def _read_command_counts(client: httpx.Client, run_id: str) -> tuple[int, Counter]:
    response = client.get(f"/runs/{run_id}/commands", params={"pageLength": 1000})
    if not response.is_success:
        raise RuntimeError(f"Command log failed with HTTP {response.status_code}: {response.text}")
    commands = response.json().get("data")
    if not isinstance(commands, list):
        raise RuntimeError(f"Command log returned an unexpected response: {response.text}")
    return len(commands), Counter(command.get("commandType") for command in commands)


def _verify_two_column_evidence(total: int, counts: Counter) -> None:
    errors = []
    if total != 462:
        errors.append(f"total commands: expected 462, found {total}")
    for command_type, expected in _EXPECTED_TWO_COLUMN_COMMANDS.items():
        actual = counts[command_type]
        if actual != expected:
            errors.append(f"{command_type}: expected {expected}, found {actual}")
    if errors:
        raise RuntimeError("Two-column command evidence mismatch: " + "; ".join(errors))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.execute and args.confirm_deck_ready != _EXECUTION_CONFIRMATION:
        print(
            f"BLOCKED: execution requires --confirm-deck-ready {_EXECUTION_CONFIRMATION}",
            file=sys.stderr,
        )
        return 2

    _validate_exact_bundle()
    runtime_parameters = _runtime_parameters(columns=args.columns, scientific=args.scientific)
    base_url = f"http://{args.host}:{args.port}"
    print(f"Target: {base_url}")
    print(f"Runtime parameters: {json.dumps(runtime_parameters, sort_keys=True)}")

    with httpx.Client(
        base_url=base_url,
        headers={_HTTP_API_VERSION_HEADER: "*"},
        timeout=30.0,
    ) as client:
        health = client.get("/health")
        if not health.is_success:
            raise RuntimeError(f"Connector health check failed with HTTP {health.status_code}: {health.text}")
        print("Connector health: PASS")

        pipettes_response = client.get("/pipettes")
        if not pipettes_response.is_success:
            raise RuntimeError(
                f"Pipette inventory failed with HTTP {pipettes_response.status_code}: {pipettes_response.text}"
            )
        modules_response = client.get("/modules")
        if not modules_response.is_success:
            raise RuntimeError(
                f"Module inventory failed with HTTP {modules_response.status_code}: {modules_response.text}"
            )
        inventory_errors = _hardware_inventory_errors(pipettes_response.json(), modules_response.json())
        if inventory_errors:
            raise RuntimeError("Hardware inventory is not AS-MS ready: " + "; ".join(inventory_errors))
        print("Hardware inventory: PASS")

        deck = _response_data(client.get("/deck_configuration"), action="deck configuration")
        deck_errors = _deck_configuration_errors({"data": deck})
        if deck_errors:
            raise RuntimeError("Deck configuration is not AS-MS ready: " + "; ".join(deck_errors))
        print("Deck configuration: PASS")

        response = client.post(
            "/protocols",
            files=_exact_protocol_files(),
            data={"run_time_parameter_values": json.dumps(runtime_parameters)},
        )
        protocol = _response_data(response, action="protocol upload")
        analysis = _wait_for_analysis(client, protocol, timeout=args.analysis_timeout)
        if analysis.get("status") != "completed":
            raise RuntimeError(f"Protocol analysis failed: {analysis}")
        print(f"Protocol analysis: PASS (protocol_id={protocol['id']})")

        if not args.execute:
            print("ANALYSIS ONLY: no run was created and no hardware moved.")
            return 0

        response = client.post(
            "/runs",
            json={"data": {"protocolId": protocol["id"], "runTimeParameterValues": runtime_parameters}},
        )
        run = _response_data(response, action="run creation")
        run_id = run["id"]
        print(f"Created run {run_id}")
        response = client.post(
            f"/runs/{run_id}/actions",
            json={"data": {"actionType": "play"}},
        )
        if response.status_code != 201:
            raise RuntimeError(f"Run start failed with HTTP {response.status_code}: {response.text}")

        try:
            run = _wait_for_run(client, run_id, timeout=args.run_timeout)
        except KeyboardInterrupt:
            print(f"\nInterrupt received; stopping run {run_id}...")
            stop = client.post(
                f"/runs/{run_id}/actions",
                json={"data": {"actionType": "stop"}},
            )
            if stop.status_code != 201:
                print(f"WARNING: stop returned HTTP {stop.status_code}: {stop.text}", file=sys.stderr)
            return 130

        total, counts = _read_command_counts(client, run_id)
        print(
            "Command evidence: "
            f"total={total}, tips={counts['pickUpTip']}, aspirates={counts['aspirate']}, "
            f"dispenses={counts['dispense']}, gripper_moves={counts['moveLabware']}"
        )
        if run.get("status") != "succeeded":
            raise RuntimeError(f"Run {run_id} ended in {run.get('status')}: {run.get('errors', [])}")
        if args.columns == 2:
            _verify_two_column_evidence(total, counts)
        print(f"AS-MS hardware workflow PASS (run_id={run_id})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
