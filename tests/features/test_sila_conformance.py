"""Cross-feature SiLA Part A and UniteLabs CDK conformance gates."""

import inspect
import re
import typing
from importlib.resources import files
from xml.etree import ElementTree

import pytest
import xmlschema
from sila.framework.fdl import Serializer

from unitelabs.opentrons_flex.features import (
    AbsorbanceReaderFeature,
    CalibrationFeature,
    FlexStackerFeature,
    FlexStackerMaintenanceFeature,
    GripperFeature,
    HeaterShakerFeature,
    LabwareMovementController,
    LiquidHandlingController,
    MotionControlFeature,
    PipetteFeature,
    TemperatureModuleFeature,
    ThermocyclerFeature,
    TipController,
)

_NAMESPACE = {"sila": "http://www.sila-standard.org"}
_FEATURE_CLASSES = (
    AbsorbanceReaderFeature,
    CalibrationFeature,
    FlexStackerFeature,
    FlexStackerMaintenanceFeature,
    GripperFeature,
    HeaterShakerFeature,
    LabwareMovementController,
    LiquidHandlingController,
    MotionControlFeature,
    PipetteFeature,
    TemperatureModuleFeature,
    ThermocyclerFeature,
    TipController,
)
_SCHEMA = xmlschema.XMLSchema(str(files("sila").joinpath("resources", "FeatureDefinition.xsd")))
_UNIT_SUFFIX = re.compile(r"(?:Celsius|Degrees|Milliseconds|Millimetres|Nanometres|Rpm|Seconds|Ul)$")


def _serialize(feature_cls: type) -> str:
    feature = feature_cls(object())
    feature.attach()
    return Serializer.serialize(feature.serialize)


def _owner_identifier(element: ElementTree.Element, parents: dict) -> str:
    owner = parents.get(element)
    while owner is not None:
        identifier = owner.findtext("sila:Identifier", namespaces=_NAMESPACE)
        if identifier is not None:
            return identifier
        owner = parents.get(owner)
    return "unknown"


@pytest.mark.parametrize("feature_cls", _FEATURE_CLASSES)
def test_public_feature_fdl_obeys_cross_feature_conventions(feature_cls: type) -> None:
    """Validate schema, controller naming, units, and response conventions."""
    fdl = _serialize(feature_cls)
    _SCHEMA.validate(fdl)
    root = ElementTree.fromstring(fdl)

    feature_identifier = root.findtext("sila:Identifier", namespaces=_NAMESPACE) or ""
    display_name = root.findtext("sila:DisplayName", namespaces=_NAMESPACE) or ""
    assert feature_identifier.endswith(("Controller", "Provider", "Service"))
    assert "Feature" not in feature_identifier
    assert "Feature" not in display_name

    identifiers = [identifier.text or "" for identifier in root.findall(".//sila:Identifier", _NAMESPACE)]
    assert "Success" not in identifiers
    assert not [identifier for identifier in identifiers if _UNIT_SUFFIX.search(identifier)]

    parents = {child: parent for parent in root.iter() for child in parent}
    for basic in root.findall(".//sila:Basic", _NAMESPACE):
        if basic.text not in {"Integer", "Real"}:
            continue
        data_type = parents[basic]
        constrained = parents.get(data_type)
        assert constrained is not None and constrained.tag.endswith("Constrained"), (
            f"{feature_identifier}.{_owner_identifier(basic, parents)} has an unannotated {basic.text}"
        )
        constraints = constrained.find("sila:Constraints", _NAMESPACE)
        assert constraints is not None and len(constraints), (
            f"{feature_identifier}.{_owner_identifier(basic, parents)} has no numeric constraints"
        )


@pytest.mark.parametrize("feature_cls", _FEATURE_CLASSES)
def test_public_commands_do_not_use_optional_parameters(feature_cls: type) -> None:
    """Keep command parameters explicit because the UniteLabs CDK forbids Optional inputs."""
    for method_name, method in feature_cls.__dict__.items():
        if not inspect.isfunction(method):
            continue
        if method_name.startswith("_"):
            continue
        for parameter, annotation in typing.get_type_hints(method).items():
            if parameter == "return":
                continue
            assert type(None) not in typing.get_args(annotation), (
                f"{feature_cls.__name__}.{method_name} parameter {parameter} is Optional"
            )
