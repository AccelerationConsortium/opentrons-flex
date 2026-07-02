"""SiLA-definition smoke test for the module features (no hardware).

The module features are only registered at runtime when a module is attached, so
nothing else exercises their SiLA feature-definition generation. Registering each
with a dummy controller and starting the connector builds the SiLA definitions,
catching invalid command/return types (e.g. the DeviceInfo structure, status
enums) without needing an attached module.
"""

import pytest

from unitelabs.cdk import Connector, SiLAServerConfig
from unitelabs.opentrons_flex import OpentronsFlexConfig
from unitelabs.opentrons_flex.features import (
    AbsorbanceReaderFeature,
    FlexStackerFeature,
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
    TemperatureModuleFeature,
    HeaterShakerFeature,
    ThermocyclerFeature,
]


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
