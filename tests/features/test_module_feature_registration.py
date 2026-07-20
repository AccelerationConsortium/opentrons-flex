"""SiLA-definition smoke test for the module features (no hardware).

The module features are only registered at runtime when a module is attached, so
nothing else exercises their SiLA feature-definition generation. Registering each
with a dummy controller and starting the connector builds the SiLA definitions,
catching invalid command/return types (e.g. the DeviceInfo structure, status
enums) without needing an attached module.
"""

from importlib.resources import files
from xml.etree import ElementTree

import pytest
import xmlschema
from sila.framework.fdl import Serializer

from unitelabs.cdk import Connector, SiLAServerConfig
from unitelabs.opentrons_flex import OpentronsFlexConfig
from unitelabs.opentrons_flex.features import (
    AbsorbanceReaderFeature,
    FlexStackerFeature,
    FlexStackerMaintenanceFeature,
    HeaterShakerFeature,
    TemperatureModuleFeature,
    ThermocyclerFeature,
)

# Real SiLA feature-definition generation requires the actual unitelabs CDK. When
# it is stubbed (offline dev, see tests/conftest.py), Connector.start() does not
# exist, so this test is skipped — it runs in CI where the real CDK is installed.
_REQUIRES_REAL_CDK = pytest.mark.skipif(
    not hasattr(Connector, "start"),
    reason="real unitelabs CDK not installed (stubbed); SiLA generation runs in CI",
)

_MODULE_FEATURES = [
    AbsorbanceReaderFeature,
    FlexStackerFeature,
    FlexStackerMaintenanceFeature,
    TemperatureModuleFeature,
    HeaterShakerFeature,
    ThermocyclerFeature,
]
_SILA_FEATURE_DEFINITION_SCHEMA = xmlschema.XMLSchema(str(files("sila").joinpath("resources", "FeatureDefinition.xsd")))


def _property_is_observable(fdl: str, identifier: str) -> bool:
    root = ElementTree.fromstring(fdl)
    namespace = {"sila": "http://www.sila-standard.org"}
    return any(
        property_element.findtext("sila:Identifier", namespaces=namespace) == identifier
        and property_element.findtext("sila:Observable", namespaces=namespace) == "Yes"
        for property_element in root.findall("sila:Property", namespace)
    )


@pytest.mark.parametrize(
    "feature_cls",
    [
        AbsorbanceReaderFeature,
        FlexStackerFeature,
        FlexStackerMaintenanceFeature,
        TemperatureModuleFeature,
        HeaterShakerFeature,
        ThermocyclerFeature,
    ],
)
def test_advanced_module_feature_definitions_validate_against_sila_xsd(feature_cls) -> None:
    """Validate every new v2 FDL against the schema shipped by unitelabs-sila."""
    feature = feature_cls(object())
    feature.attach()
    fdl = Serializer.serialize(feature.serialize)

    _SILA_FEATURE_DEFINITION_SCHEMA.validate(fdl)


@_REQUIRES_REAL_CDK
@pytest.mark.parametrize("feature_cls", _MODULE_FEATURES)
@pytest.mark.asyncio
async def test_module_feature_sila_definition_builds(feature_cls) -> None:
    config = OpentronsFlexConfig(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    connector = Connector(config)
    # SiLA generation introspects the method signatures/type hints, not the
    # controller instance, so a placeholder controller is sufficient here.
    connector.register(feature_cls(object()))
    await connector.start()
    try:
        assert connector.sila_server.protobuf is not None
    finally:
        await connector.stop()


@_REQUIRES_REAL_CDK
def test_heater_shaker_definition_has_units_constraints_and_controller_name() -> None:
    """The workflow-facing Heater-Shaker FDL exposes its physical constraints."""
    feature = HeaterShakerFeature(object())
    feature.attach()
    fdl = Serializer.serialize(feature.serialize)

    assert 'FeatureVersion="3.0"' in fdl
    assert "<Identifier>HeaterShakerController</Identifier>" in fdl
    assert "<DisplayName>Heater Shaker Controller</DisplayName>" in fdl
    assert "<Identifier>SetSpeed</Identifier>" in fdl
    assert "<Identifier>GetSpeed</Identifier>" in fdl
    assert "<Identifier>Rpm</Identifier>" not in fdl
    assert "<Identifier>Temperature</Identifier>" in fdl
    assert "<Identifier>TemperatureCelsius</Identifier>" not in fdl
    assert "<MinimalInclusive>200</MinimalInclusive>" in fdl
    assert "<MaximalInclusive>3000</MaximalInclusive>" in fdl
    assert "<Label>rpm</Label>" in fdl
    assert "<SIUnit>Second</SIUnit>" in fdl
    assert "<Exponent>-1</Exponent>" in fdl
    assert "<Label>°C</Label>" in fdl
    assert "<SIUnit>Kelvin</SIUnit>" in fdl
    assert "<Offset>273.15</Offset>" in fdl


@_REQUIRES_REAL_CDK
def test_thermocycler_definition_uses_controller_naming() -> None:
    """The attached Thermocycler service follows SiLA Part A naming."""
    feature = ThermocyclerFeature(object())
    feature.attach()
    fdl = Serializer.serialize(feature.serialize)

    assert "<Identifier>ThermocyclerController</Identifier>" in fdl
    assert "<DisplayName>Thermocycler Controller</DisplayName>" in fdl
    assert 'FeatureVersion="2.0"' in fdl
    assert "<Identifier>TemperatureCelsius</Identifier>" not in fdl
    assert "<Identifier>HoldTimeSeconds</Identifier>" not in fdl
    assert "<Identifier>VolumeUl</Identifier>" not in fdl
    assert "<Label>°C</Label>" in fdl
    assert "<Label>s</Label>" in fdl
    assert "<Label>µL</Label>" in fdl


@_REQUIRES_REAL_CDK
def test_temperature_controller_definition_is_unit_annotated_and_has_no_success_boolean() -> None:
    """The GEN2 temperature surface uses controller naming, units, and structured state."""
    feature = TemperatureModuleFeature(object())
    feature.attach()
    fdl = Serializer.serialize(feature.serialize)

    assert 'FeatureVersion="2.0"' in fdl
    assert "<Identifier>TemperatureController</Identifier>" in fdl
    assert "<DisplayName>Temperature Controller</DisplayName>" in fdl
    assert "<Identifier>SetTemperature</Identifier>" in fdl
    assert "<Identifier>SetTemperatureAndWait</Identifier>" in fdl
    assert "<Identifier>Deactivate</Identifier>" in fdl
    assert "<Identifier>Status</Identifier>" in fdl
    assert _property_is_observable(fdl, "Status")
    assert "<Identifier>DeviceInfo</Identifier>" in fdl
    assert "<Identifier>TemperatureCelsius</Identifier>" not in fdl
    assert "<Identifier>Success</Identifier>" not in fdl
    assert "<Identifier>InvalidTemperatureTargetError</Identifier>" in fdl
    assert "<MinimalInclusive>4</MinimalInclusive>" in fdl
    assert "<MaximalInclusive>95</MaximalInclusive>" in fdl
    assert "<Label>°C</Label>" in fdl
    assert "<SIUnit>Kelvin</SIUnit>" in fdl
    assert "<Offset>273.15</Offset>" in fdl


@_REQUIRES_REAL_CDK
def test_absorbance_reader_definition_is_typed_and_unit_annotated() -> None:
    """The reader FDL exposes workflow commands and wavelength semantics."""
    feature = AbsorbanceReaderFeature(object())
    feature.attach()
    fdl = Serializer.serialize(feature.serialize)

    assert 'FeatureVersion="2.0"' in fdl
    assert "<Identifier>AbsorbanceReaderController</Identifier>" in fdl
    assert "<DisplayName>Absorbance Reader Controller</DisplayName>" in fdl
    assert "<Identifier>InitializeSingle</Identifier>" in fdl
    assert "<Identifier>InitializeSingleWithReference</Identifier>" in fdl
    assert "<Identifier>InitializeMultiple</Identifier>" in fdl
    assert "<Identifier>ReadPlate</Identifier>" in fdl
    assert "<Identifier>WellIdentifier</Identifier>" in fdl
    assert "<Identifier>Absorbance</Identifier>" in fdl
    assert _property_is_observable(fdl, "Status")
    assert "<MinimalElementCount>1</MinimalElementCount>" in fdl
    assert "<MaximalElementCount>6</MaximalElementCount>" in fdl
    assert "<Label>nm</Label>" in fdl
    assert "<Label>AU</Label>" in fdl
    assert "<SIUnit>Meter</SIUnit>" in fdl


@_REQUIRES_REAL_CDK
def test_flex_stacker_definition_uses_units_and_structured_results() -> None:
    """The routine Stacker FDL contains workflows without maintenance controls."""
    feature = FlexStackerFeature(object())
    feature.attach()
    fdl = Serializer.serialize(feature.serialize)

    assert 'FeatureVersion="2.0"' in fdl
    assert "<Identifier>FlexStackerController</Identifier>" in fdl
    assert "<DisplayName>Flex Stacker Controller</DisplayName>" in fdl
    assert "<Identifier>RetrieveLabware</Identifier>" in fdl
    assert "<Identifier>StoreLabware</Identifier>" in fdl
    assert "<Identifier>HomeAll</Identifier>" not in fdl
    assert "<Identifier>MoveAxis</Identifier>" not in fdl
    assert "<Identifier>SetLed</Identifier>" not in fdl
    assert "<Identifier>Success</Identifier>" not in fdl
    assert _property_is_observable(fdl, "Status")
    assert "<Label>mm</Label>" in fdl
    assert "<SIUnit>Meter</SIUnit>" in fdl


@_REQUIRES_REAL_CDK
def test_flex_stacker_maintenance_definition_is_separate_and_constrained() -> None:
    """Maintenance movement and service controls have an independent contract."""
    feature = FlexStackerMaintenanceFeature(object())
    feature.attach()
    fdl = Serializer.serialize(feature.serialize)

    assert 'FeatureVersion="1.0"' in fdl
    assert "<Identifier>FlexStackerMaintenanceController</Identifier>" in fdl
    assert "<Identifier>HomeAll</Identifier>" in fdl
    assert "<Identifier>MoveAxis</Identifier>" in fdl
    assert "<Identifier>SetLed</Identifier>" in fdl
    assert "<Identifier>RetrieveLabware</Identifier>" not in fdl
    assert "<Identifier>Duration</Identifier>" in fdl
    assert "<Identifier>DurationMs</Identifier>" not in fdl
    assert "<Identifier>Success</Identifier>" not in fdl
    assert _property_is_observable(fdl, "Status")
    assert _property_is_observable(fdl, "LimitSwitchStatus")
    assert "<MaximalInclusive>194</MaximalInclusive>" in fdl
    assert "<MaximalInclusive>10</MaximalInclusive>" in fdl
    assert "<Label>mm</Label>" in fdl
    assert "<Label>s</Label>" in fdl
    assert "<SIUnit>Meter</SIUnit>" in fdl
    assert "<SIUnit>Second</SIUnit>" in fdl
