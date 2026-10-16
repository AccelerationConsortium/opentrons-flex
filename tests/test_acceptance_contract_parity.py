from __future__ import annotations

import dataclasses

from unitelabs_flex_acceptance_contract import wire_types

from unitelabs.opentrons_flex import features
from unitelabs.opentrons_flex.features.thermocycler import ThermocyclerProfileStep


def test_cross_runtime_wire_values_match_connector_feature_contracts() -> None:
    pairs = (
        (wire_types.LiquidPosition, features.LiquidPosition),
        (wire_types.WellGeometry, features.WellGeometry),
        (wire_types.TransferProfile, features.TransferProfile),
        (wire_types.TipLocation, features.TipLocation),
        (wire_types.ThermocyclerProfileStep, ThermocyclerProfileStep),
    )
    for workflow_type, connector_type in pairs:
        assert [field.name for field in dataclasses.fields(workflow_type)] == [
            field.name for field in dataclasses.fields(connector_type)
        ]


def test_cross_runtime_enums_match_connector_feature_contracts() -> None:
    pairs = (
        (wire_types.Mount, features.Mount),
        (wire_types.PipetteMount, features.PipetteMount),
        (wire_types.VerifiedLiquidClass, features.VerifiedLiquidClass),
    )
    for workflow_type, connector_type in pairs:
        assert {member.name: member.value for member in workflow_type} == {
            member.name: member.value for member in connector_type
        }
