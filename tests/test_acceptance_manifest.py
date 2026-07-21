"""Validation tests for the fail-closed Flex acceptance manifest."""

from copy import deepcopy

import pytest

from unitelabs.opentrons_flex.acceptance import AcceptanceManifest, PlanContract, validate_plan_contract


def _coordinate(x: float, y: float, z: float) -> dict[str, float]:
    return {"x": x, "y": y, "z": z}


def _well(x: float, y: float) -> dict[str, float]:
    return {"x": x, "y": y, "bottom_z": 10.0, "top_z": 25.0, "length": 8.0, "width": 8.0}


def _manifest() -> dict:
    plan_names = {
        "process_deck_to_thermocycler",
        "thermocycler_to_process_deck",
        "process_deck_to_heater_shaker",
        "heater_shaker_to_magnetic_block",
        "magnetic_block_to_process_deck",
        "assay_deck_to_temperature_module",
        "temperature_module_to_assay_deck",
        "reader_lid_open",
        "reader_lid_close",
        "assay_deck_to_reader",
        "reader_to_assay_deck",
    }
    return {
        "schema_version": 1,
        "service_name": "Opentrons Flex Acceptance",
        "expected_robot_host": "192.0.2.10",
        "pipetting": {
            "mount": "LEFT",
            "tip_length": 95.6,
            "tiprack_diameter": 5.2,
            "tip_pickup": _coordinate(10.0, 10.0, 80.0),
            "tip_drop": _coordinate(20.0, 10.0, 80.0),
            "source": _coordinate(30.0, 20.0, 15.0),
            "source_retract": _coordinate(30.0, 20.0, 30.0),
            "destination": _coordinate(50.0, 20.0, 15.0),
            "destination_retract": _coordinate(50.0, 20.0, 30.0),
            "source_well": _well(30.0, 20.0),
            "destination_well": _well(50.0, 20.0),
            "transfer_volume": 50.0,
            "prepared_source_volume": 125.0,
            "probe_maximum_distance": 20.0,
            "mix_volume": 20.0,
            "mix_cycles": 2,
            "tiprack_uri": "opentrons/opentrons_flex_96_tiprack_1000ul/1",
        },
        "modules": {
            "heater_shaker": {"serial": "HS-001", "temperature": 37.0, "speed": 200},
            "thermocycler": {
                "serial": "TC-001",
                "lid_temperature": 45.0,
                "volume": 50.0,
                "profile": [
                    {"temperature": 37.0, "hold_time": 5.0, "ramp_rate": 0.0},
                    {"temperature": 25.0, "hold_time": 5.0, "ramp_rate": 0.0},
                ],
            },
            "temperature_module": {"serial": "TM-001", "target": 20.0},
            "plate_reader": {"serial": "AR-001", "wavelengths": [450, 600], "reference_wavelength": 600},
            "stacker": {"serial": "FS-001", "labware_height": 14.4},
        },
        "plans": {name: f"acceptance_{name}" for name in plan_names},
    }


def _plan_contracts(manifest: AcceptanceManifest) -> tuple[PlanContract, ...]:
    routes = {
        "process_deck_to_thermocycler": ("process-plate", "process-deck", "thermocycler", False),
        "thermocycler_to_process_deck": ("process-plate", "thermocycler", "process-deck", False),
        "process_deck_to_heater_shaker": ("process-plate", "process-deck", "heater-shaker", False),
        "heater_shaker_to_magnetic_block": ("process-plate", "heater-shaker", "magnetic-block", False),
        "magnetic_block_to_process_deck": ("process-plate", "magnetic-block", "process-deck", False),
        "assay_deck_to_temperature_module": ("assay-plate", "assay-deck", "temperature-module", False),
        "temperature_module_to_assay_deck": ("assay-plate", "temperature-module", "assay-deck", False),
        "reader_lid_open": ("reader-lid", "plate-reader", "lid-park", True),
        "reader_lid_close": ("reader-lid", "lid-park", "plate-reader", True),
        "assay_deck_to_reader": ("assay-plate", "assay-deck", "plate-reader", False),
        "reader_to_assay_deck": ("assay-plate", "plate-reader", "assay-deck", False),
    }
    return tuple(
        PlanContract(manifest.plans[name], labware, source, destination, is_lid)
        for name, (labware, source, destination, is_lid) in routes.items()
    )


def test_complete_manifest_parses_to_typed_contract() -> None:
    manifest = AcceptanceManifest.parse(_manifest())

    assert manifest.schema_version == 1
    assert manifest.pipetting.mount == "LEFT"
    assert manifest.pipetting.source.x == 30.0
    assert manifest.pipetting.source_well.top_z == 25.0
    assert manifest.modules.heater_shaker_speed == 200
    assert manifest.modules.plate_reader_wavelengths == (450, 600)
    assert len(manifest.plans) == 11
    assert len(manifest.commissioning_digest()) == 64


def test_commissioning_digest_is_stable_but_changes_with_physical_inputs() -> None:
    first = AcceptanceManifest.parse(_manifest())
    reordered = _manifest()
    reordered["plans"] = dict(reversed(list(reordered["plans"].items())))
    changed = _manifest()
    changed["pipetting"]["source"]["z"] = 16.0

    assert AcceptanceManifest.parse(reordered).commissioning_digest() == first.commissioning_digest()
    assert AcceptanceManifest.parse(changed).commissioning_digest() != first.commissioning_digest()


def test_unknown_fields_are_rejected_before_hardware_calls() -> None:
    data = _manifest()
    data["unsafe_remote_coordinates"] = True

    with pytest.raises(ValueError, match="unknown fields"):
        AcceptanceManifest.parse(data)


def test_deployment_placeholders_are_rejected() -> None:
    data = _manifest()
    data["modules"]["heater_shaker"]["serial"] = "REPLACE_WITH_HEATER_SHAKER_SERIAL"

    with pytest.raises(ValueError, match="placeholder"):
        AcceptanceManifest.parse(data)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("heater_shaker", "speed", 199),
        ("heater_shaker", "temperature", 36.9),
        ("temperature_module", "target", 95.1),
        ("stacker", "labware_height", 102.6),
    ],
)
def test_module_operating_ranges_are_enforced(section: str, field: str, value: float) -> None:
    data = _manifest()
    data["modules"][section][field] = value

    with pytest.raises(ValueError, match="must be between"):
        AcceptanceManifest.parse(data)


def test_reader_reference_must_be_in_measurement_wavelengths() -> None:
    data = _manifest()
    data["modules"]["plate_reader"]["reference_wavelength"] = 650

    with pytest.raises(ValueError, match="must also appear"):
        AcceptanceManifest.parse(data)


def test_reader_requires_a_sample_wavelength_distinct_from_reference() -> None:
    data = _manifest()
    data["modules"]["plate_reader"]["wavelengths"] = [600]

    with pytest.raises(ValueError, match="between 2 and 6"):
        AcceptanceManifest.parse(data)


def test_reader_wavelength_count_and_uniqueness_are_bounded() -> None:
    data = _manifest()
    data["modules"]["plate_reader"]["wavelengths"] = [350, 400, 450, 500, 550, 600, 650]

    with pytest.raises(ValueError, match="between 2 and 6"):
        AcceptanceManifest.parse(data)

    data = _manifest()
    data["modules"]["plate_reader"]["wavelengths"] = [450, 450, 600]
    with pytest.raises(ValueError, match="unique wavelengths"):
        AcceptanceManifest.parse(data)


def test_thermocycler_profile_size_and_total_duration_are_bounded() -> None:
    data = _manifest()
    data["modules"]["thermocycler"]["profile"] = [{"temperature": 25.0, "hold_time": 0.0, "ramp_rate": 0.0}] * 21

    with pytest.raises(ValueError, match="between 1 and 20"):
        AcceptanceManifest.parse(data)

    data = _manifest()
    data["modules"]["thermocycler"]["profile"] = [
        {"temperature": 25.0, "hold_time": 1800.1, "ramp_rate": 0.0},
        {"temperature": 37.0, "hold_time": 1800.0, "ramp_rate": 0.0},
    ]
    with pytest.raises(ValueError, match="total hold time"):
        AcceptanceManifest.parse(data)


def test_prepared_source_volume_covers_all_planned_liquid_actions() -> None:
    data = _manifest()
    data["pipetting"]["prepared_source_volume"] = 119.9

    with pytest.raises(ValueError, match="two transfers plus one mix"):
        AcceptanceManifest.parse(data)


def test_retract_must_be_vertical_and_reachable_by_probe() -> None:
    data = _manifest()
    data["pipetting"]["source_retract"]["x"] = 31.0

    with pytest.raises(ValueError, match="vertically above"):
        AcceptanceManifest.parse(data)

    data = _manifest()
    data["pipetting"]["probe_maximum_distance"] = 14.9
    with pytest.raises(ValueError, match="must reach"):
        AcceptanceManifest.parse(data)


def test_liquid_position_must_be_inside_matching_well() -> None:
    data = _manifest()
    data["pipetting"]["destination"]["z"] = 26.0

    with pytest.raises(ValueError, match="inside the matching well"):
        AcceptanceManifest.parse(data)


def test_duplicate_plan_identifiers_are_rejected() -> None:
    data = deepcopy(_manifest())
    plan_names = list(data["plans"])
    data["plans"][plan_names[1]] = data["plans"][plan_names[0]]

    with pytest.raises(ValueError, match="must be unique"):
        AcceptanceManifest.parse(data)


def test_plan_contract_accepts_the_complete_reconciled_campaign_graph() -> None:
    manifest = AcceptanceManifest.parse(_manifest())

    validate_plan_contract(
        manifest,
        _plan_contracts(manifest),
        deck_valid=True,
        occupancy={
            "process-deck": "process-plate",
            "assay-deck": "assay-plate",
            "plate-reader": "reader-lid",
        },
    )


def test_plan_contract_rejects_wrong_identity_and_initial_occupancy() -> None:
    manifest = AcceptanceManifest.parse(_manifest())
    plans = list(_plan_contracts(manifest))
    plan = next(item for item in plans if item.plan_identifier == manifest.plans["heater_shaker_to_magnetic_block"])
    plans[plans.index(plan)] = PlanContract(
        plan.plan_identifier,
        "wrong-plate",
        plan.source_location,
        plan.destination_location,
        plan.is_lid,
    )

    with pytest.raises(ValueError, match="must use one process plate"):
        validate_plan_contract(
            manifest,
            tuple(plans),
            deck_valid=True,
            occupancy={
                "process-deck": "process-plate",
                "assay-deck": "assay-plate",
                "plate-reader": "reader-lid",
            },
        )

    with pytest.raises(ValueError, match="must start"):
        validate_plan_contract(
            manifest,
            _plan_contracts(manifest),
            deck_valid=True,
            occupancy={"process-deck": "wrong-plate", "assay-deck": "assay-plate", "plate-reader": "reader-lid"},
        )
