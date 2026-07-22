"""Offline acceptance contract for the prepared AS-MS Flex protocol."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from unitelabs.opentrons_flex.protocol_preflight import inspect_protocol

_ROOT = Path(__file__).resolve().parents[2]
_PROTOCOL = _ROOT / "protocols" / "asms" / "asms_single_point_wash_and_elute.py"
_LABWARE_DIR = _PROTOCOL.parent / "labware"
_SHADOW_LABWARE = {
    "thermokingfisherdeepwell_96_wellplate_2000ul": "nest_96_wellplate_2ml_deep",
    "azenta_96_wellplate_200ul_pcr": "opentrons_96_wellplate_200ul_pcr_full_skirt",
}
_EXACT_LABWARE_HASHES = {
    "azenta_96_wellplate_200ul_pcr": "43506d5482e3dfebff377e56b709150c81415473efc9cf2c6362dc4b68a1e20f",
    "thermokingfisherdeepwell_96_wellplate_2000ul": (
        "2ea9c15468816ace3970fe497cef7e1dc22d5f9ab033656bf9472a62396dfb47"
    ),
}


def _function_call(tree: ast.Module, name: str) -> ast.Call:
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]
    assert len(calls) == 1, f"Expected one {name} call, found {len(calls)}"
    return calls[0]


def test_exact_preflight_fails_closed_when_bundled_labware_is_omitted() -> None:
    report = inspect_protocol(_PROTOCOL)

    assert report.robot_type == "Flex"
    assert report.api_level == "2.27"
    assert report.missing_labware == (
        "azenta_96_wellplate_200ul_pcr",
        "thermokingfisherdeepwell_96_wellplate_2000ul",
    )
    assert report.simulation_completed is False
    assert report.exact_bundle_ready is False


def test_bundled_exact_preflight_executes_complete_command_and_tip_plan() -> None:
    report = inspect_protocol(
        _PROTOCOL,
        custom_labware_paths=[_LABWARE_DIR],
        expected_custom_labware_hashes=_EXACT_LABWARE_HASHES,
    )

    assert report.exact_bundle_ready is True
    assert report.shadow_passed is False
    assert report.missing_labware == ()
    assert report.shadow_substitutions == ()
    assert report.simulation_error is None
    assert report.command_count == 344
    assert report.tip_pickups == 26
    assert report.returned_tip_pickups == 6
    assert report.discarded_tip_pickups == 20
    assert report.gripper_moves == 9
    assert report.aspirate_commands == 82
    assert report.dispense_commands == 66
    assert report.delay_seconds == 634
    assert report.temperature_deactivations == 1


def test_exact_bundle_is_not_ready_without_approved_labware_identities() -> None:
    report = inspect_protocol(_PROTOCOL, custom_labware_paths=[_LABWARE_DIR])

    assert report.simulation_completed is True
    assert report.custom_labware_identities_verified is False
    assert report.exact_bundle_ready is False


@pytest.mark.parametrize(
    ("file_name", "load_name", "brand_id", "capacity", "semantic_sha256"),
    [
        (
            "azenta_96_wellplate_200ul_pcr.json",
            "azenta_96_wellplate_200ul_pcr",
            "4ti-0740",
            200,
            "43506d5482e3dfebff377e56b709150c81415473efc9cf2c6362dc4b68a1e20f",
        ),
        (
            "thermokingfisherdeepwell_96_wellplate_2000ul.json",
            "thermokingfisherdeepwell_96_wellplate_2000ul",
            "95040450",
            2000,
            "2ea9c15468816ace3970fe497cef7e1dc22d5f9ab033656bf9472a62396dfb47",
        ),
    ],
)
def test_bundled_labware_geometry_is_pinned(
    file_name: str,
    load_name: str,
    brand_id: str,
    capacity: int,
    semantic_sha256: str,
) -> None:
    definition = json.loads((_LABWARE_DIR / file_name).read_text(encoding="utf-8"))
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    assert hashlib.sha256(canonical).hexdigest() == semantic_sha256
    assert definition["schemaVersion"] == 2
    assert definition["version"] == 1
    assert definition["namespace"] == "custom_beta"
    assert definition["parameters"]["loadName"] == load_name
    assert brand_id in definition["brand"]["brandId"]
    assert len(definition["wells"]) == 96
    assert len(definition["ordering"]) == 12
    assert all(len(column) == 8 for column in definition["ordering"])
    assert {well["totalLiquidVolume"] for well in definition["wells"].values()} == {capacity}


def test_exact_preflight_rejects_same_load_name_with_modified_geometry(tmp_path: Path) -> None:
    azenta_name = "azenta_96_wellplate_200ul_pcr.json"
    azenta = json.loads((_LABWARE_DIR / azenta_name).read_text(encoding="utf-8"))
    azenta["dimensions"]["zDimension"] += 1
    (tmp_path / azenta_name).write_text(json.dumps(azenta), encoding="utf-8")
    shutil.copy(
        _LABWARE_DIR / "thermokingfisherdeepwell_96_wellplate_2000ul.json",
        tmp_path,
    )

    with pytest.raises(ValueError, match="identity mismatch"):
        inspect_protocol(
            _PROTOCOL,
            custom_labware_paths=[tmp_path],
            expected_custom_labware_hashes=_EXACT_LABWARE_HASHES,
        )


def test_shadow_preflight_executes_complete_command_and_tip_plan() -> None:
    report = inspect_protocol(_PROTOCOL, shadow_labware=_SHADOW_LABWARE)

    assert report.shadow_passed is True
    assert report.exact_bundle_ready is False
    assert report.simulation_error is None
    assert report.command_count == 344
    assert report.tip_pickups == 26
    assert report.returned_tip_pickups == 6
    assert report.discarded_tip_pickups == 20
    assert report.gripper_moves == 9
    assert report.aspirate_commands == 82
    assert report.dispense_commands == 66
    assert report.delay_seconds == 634
    assert report.temperature_deactivations == 1


def test_preflight_rejects_non_flex_protocol_before_simulation(tmp_path: Path) -> None:
    source = _PROTOCOL.read_text(encoding="utf-8").replace('"robotType": "Flex"', '"robotType": "OT-2"')
    protocol = tmp_path / "wrong_robot.py"
    protocol.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match="robotType='Flex'"):
        inspect_protocol(protocol, shadow_labware=_SHADOW_LABWARE)


def test_methanol_and_final_ms_transfer_follow_elution_plate_lineage() -> None:
    tree = ast.parse(_PROTOCOL.read_text(encoding="utf-8"), filename=str(_PROTOCOL))

    add_methanol = _function_call(tree, "add_methanol_to_elution_plate")
    final_transfer = _function_call(tree, "transfer_methanol_to_ms_plate")

    assert isinstance(add_methanol.args[1], ast.Name)
    assert add_methanol.args[1].id == "elution_columns"
    assert isinstance(final_transfer.args[0], ast.Name)
    assert final_transfer.args[0].id == "elution_columns"
    assert isinstance(final_transfer.args[1], ast.Name)
    assert final_transfer.args[1].id == "ms_columns"


def test_custom_waste_and_ms_labware_are_never_gripper_moved() -> None:
    tree = ast.parse(_PROTOCOL.read_text(encoding="utf-8"), filename=str(_PROTOCOL))
    moved_plates: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"move_to_magnetic_block", "move_to_deck"}:
            continue
        assert node.args and isinstance(node.args[0], ast.Name)
        moved_plates.add(node.args[0].id)

    assert moved_plates == {"asms_plate", "elution_plate"}


def test_connector_test_mode_is_opt_in_and_full_wet_run_remains_default() -> None:
    tree = ast.parse(_PROTOCOL.read_text(encoding="utf-8"), filename=str(_PROTOCOL))
    add_parameters = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "add_parameters"
    )
    calls = [node for node in ast.walk(add_parameters) if isinstance(node, ast.Call)]
    bool_call = next(call for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr == "add_bool")
    int_call = next(call for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr == "add_int")
    bool_keywords = {keyword.arg: keyword.value for keyword in bool_call.keywords}
    int_keywords = {keyword.arg: keyword.value for keyword in int_call.keywords}

    assert ast.literal_eval(bool_keywords["variable_name"]) == "connector_test_mode"
    assert ast.literal_eval(bool_keywords["default"]) is False
    assert ast.literal_eval(int_keywords["variable_name"]) == "number_of_columns"
    assert ast.literal_eval(int_keywords["default"]) == 2
    assert ast.literal_eval(int_keywords["minimum"]) == 1
    assert ast.literal_eval(int_keywords["maximum"]) == 2
