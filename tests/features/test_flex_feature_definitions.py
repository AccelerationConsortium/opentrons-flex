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
from unitelabs.opentrons_flex import OpentronsFlexConfig
from unitelabs.opentrons_flex.features import (
    CalibrationFeature,
    GripperFeature,
    MotionControlFeature,
    PipetteFeature,
)
from unitelabs.opentrons_flex.io import (
    FlexCalibrationController,
    FlexGripperController,
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
    connector.register(MotionControlFeature(motion))
    connector.register(PipetteFeature(motion))
    connector.register(GripperFeature(gripper))
    connector.register(CalibrationFeature(calibration))

    await connector.start()
    try:
        # If any feature's commands/properties were not SiLA-mappable, start() raises.
        assert connector.sila_server.protobuf is not None
    finally:
        await connector.stop()
        await api.clean_up()
