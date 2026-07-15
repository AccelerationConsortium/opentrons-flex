"""SiLA feature-definition generation for the core Flex features.

Registering each core feature on a real Connector and starting it forces the CDK
to build the SiLA feature definitions, validating that every command/property
signature, return dataclass, enum, constraint and ``errors=[...]`` declaration is
mappable to SiLA. This is the coverage the mocked-Connector wiring tests cannot
give. Requires the real unitelabs CDK; skipped when it is stubbed (offline).
"""

import asyncio

import pytest
from opentrons.hardware_control.ot3api import OT3API

from unitelabs.cdk import Connector, SiLAServerConfig
from sila.framework.fdl import Serializer
from unitelabs.opentrons_flex import OpentronsFlexConfig
from unitelabs.opentrons_flex.features import (
    CalibrationFeature,
    GripperFeature,
    LabwareMovementController,
    LiquidHandlingController,
    MotionControlFeature,
    PipetteFeature,
    TipController,
)
from unitelabs.opentrons_flex.io import (
    FlexCalibrationController,
    FlexGripperController,
    FlexLabwareMovementController,
    FlexLiquidHandlingController,
    FlexMotionController,
)

pytestmark = pytest.mark.skipif(
    not hasattr(Connector, "start"),
    reason="real unitelabs CDK not installed (stubbed); SiLA generation runs in CI",
)


@pytest.mark.asyncio
async def test_core_features_generate_sila_definitions():
    api = await OT3API.build_hardware_simulator()
    lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=lock)
    gripper = FlexGripperController.from_api(api, lock=lock)
    calibration = FlexCalibrationController.from_api(api, lock=lock)

    # discovery/cloud disabled so start() does not block on zeroconf / cloud connect
    config = OpentronsFlexConfig(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    connector = Connector(config)
    motion_feature = MotionControlFeature(motion)
    connector.register(motion_feature)
    liquid_feature = LiquidHandlingController(FlexLiquidHandlingController(motion))
    connector.register(liquid_feature)
    labware_feature = LabwareMovementController(FlexLabwareMovementController(motion, gripper))
    connector.register(labware_feature)
    pipette = PipetteFeature(motion)
    tip = TipController(motion)
    connector.register(pipette)
    connector.register(tip)
    gripper_feature = GripperFeature(gripper)
    calibration_feature = CalibrationFeature(calibration)
    connector.register(gripper_feature)
    connector.register(calibration_feature)

    pipette_fdl = Serializer.serialize(pipette.serialize)
    tip_fdl = Serializer.serialize(tip.serialize)

    assert '<Feature SiLA2Version="1.1" FeatureVersion="1.0"' in tip_fdl
    assert "<Identifier>TipController</Identifier>" in tip_fdl
    assert "<Identifier>PickUpTip</Identifier>" in tip_fdl
    assert "<Identifier>DropTip</Identifier>" in tip_fdl
    assert "<Identifier>GetTipPresence</Identifier>" in tip_fdl
    assert "<Identifier>Stop</Identifier>" not in tip_fdl
    assert "<Observable>No</Observable>" in tip_fdl
    assert "<Identifier>TipLength</Identifier>" in tip_fdl
    assert "<Identifier>Location</Identifier>" in tip_fdl
    assert "<Label>mm</Label>" in tip_fdl
    assert "<Factor>0.001</Factor>" in tip_fdl
    assert "<Identifier>TipPresence</Identifier>" in tip_fdl
    assert "Current lifecycle phase of the operation." in tip_fdl
    assert "Operator-facing progress or recovery message." in tip_fdl
    assert "PickUpTip" not in pipette_fdl
    assert 'FeatureVersion="1.1"' in pipette_fdl
    assert "<Identifier>ConfigureFullNozzleLayout</Identifier>" in pipette_fdl
    assert "<Identifier>ConfigureSingleNozzleLayout</Identifier>" in pipette_fdl
    assert "<Identifier>ConfigureRectangularNozzleLayout</Identifier>" in pipette_fdl
    assert "<Identifier>GetNozzleConfiguration</Identifier>" in pipette_fdl
    gripper_fdl = Serializer.serialize(gripper_feature.serialize)
    calibration_fdl = Serializer.serialize(calibration_feature.serialize)
    motion_fdl = Serializer.serialize(motion_feature.serialize)
    liquid_fdl = Serializer.serialize(liquid_feature.serialize)
    labware_fdl = Serializer.serialize(labware_feature.serialize)
    assert 'FeatureVersion="2.0"' in motion_fdl
    assert 'FeatureVersion="1.2"' in gripper_fdl
    assert 'FeatureVersion="1.1"' in calibration_fdl
    assert "<Identifier>NotHomedError</Identifier>" in gripper_fdl
    assert "<Identifier>NotHomedError</Identifier>" in calibration_fdl
    assert "<Identifier>Mix</Identifier>" in liquid_fdl
    assert "<Identifier>TransferWithVerifiedLiquidClass</Identifier>" in liquid_fdl
    assert "<Identifier>LiquidClassNotSupportedError</Identifier>" in liquid_fdl
    assert "<Identifier>MoveLabware</Identifier>" in labware_fdl
    assert "<Identifier>MoveLid</Identifier>" in labware_fdl
    assert "<Identifier>AvailablePlans</Identifier>" in labware_fdl
    assert "<Identifier>DeckState</Identifier>" in labware_fdl
    assert "<Identifier>DestinationOccupiedError</Identifier>" in labware_fdl
    assert "<Identifier>PlanIdentifier</Identifier>" in labware_fdl

    await connector.start()
    try:
        # If any feature's commands/properties were not SiLA-mappable, start() raises.
        assert connector.sila_server.protobuf is not None
    finally:
        await connector.stop()
        await api.clean_up()
