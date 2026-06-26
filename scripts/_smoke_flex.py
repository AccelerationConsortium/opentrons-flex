"""Standalone smoke test: exercise the full Flex connector against the OT3 simulator.

Stubs ``unitelabs.cdk`` (private-index dependency) so the connector package imports
without the real CDK, then drives create_app() in simulator mode end-to-end.
Run with: PYTHONPATH=src python scripts/_smoke_flex.py
"""

import asyncio
import sys
import types
import typing


def _install_cdk_stub() -> None:
    cdk = types.ModuleType("unitelabs.cdk")

    class Connector:
        def __init__(self, config=None):
            self.config = config
            self.features = []

        def register(self, feature):
            self.features.append(feature)

    class ConnectorBaseConfig:
        pass

    class SiLAServerConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.hostname = kw.get("hostname", "127.0.0.1")
            self.port = kw.get("port", 50051)

    cdk.Connector = Connector
    cdk.ConnectorBaseConfig = ConnectorBaseConfig
    cdk.SiLAServerConfig = SiLAServerConfig

    sila = types.ModuleType("unitelabs.cdk.sila")

    class Feature:
        def __init__(self, **kw):
            self._meta = kw

    def _decorator(*_a, **_k):
        def wrap(fn):
            return fn

        return wrap

    sila.Feature = Feature
    sila.UnobservableCommand = _decorator
    sila.ObservableCommand = _decorator
    sila.UnobservableProperty = _decorator
    sila.ObservableProperty = _decorator

    constraints = types.ModuleType("unitelabs.cdk.sila.constraints")
    for name in ["MinimalInclusive", "MaximalInclusive", "MinimalExclusive", "Pattern"]:
        setattr(constraints, name, lambda *_a, **_k: object())
    sila.constraints = constraints

    sys.modules["unitelabs.cdk"] = cdk
    sys.modules["unitelabs.cdk.sila"] = sila
    sys.modules["unitelabs.cdk.sila.constraints"] = constraints


def _install_version_stub() -> None:
    # __init__ calls importlib.metadata.version("unitelabs-opentrons-flex")
    import importlib.metadata as md

    orig = md.version

    def fake(name):
        if name == "unitelabs-opentrons-flex":
            return "0.1.0"
        return orig(name)

    md.version = fake


_install_cdk_stub()
_install_version_stub()


async def main() -> None:
    from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
    from unitelabs.opentrons_flex.features.motion_control import Mount

    config = OpentronsFlexConfig(use_simulator=True)
    agen = create_app(config)
    connector = await agen.__anext__()

    by_type = {type(f).__name__: f for f in connector.features}
    print("registered features:", sorted(by_type))
    assert "MagneticModuleFeature" not in by_type, "magnetic must not be registered on Flex"
    for required in ["MotionControlFeature", "PipetteFeature", "GripperFeature", "CalibrationFeature"]:
        assert required in by_type, f"missing feature {required}"

    motion = by_type["MotionControlFeature"]
    await motion.home()
    pos = await motion.get_position(Mount.LEFT)
    print("LEFT position:", round(pos.x, 1), round(pos.y, 1), round(pos.z, 1))
    moved = await motion.move_relative(Mount.LEFT, delta_x=-5, delta_y=-5, delta_z=-3)
    print("after move_relative:", round(moved.x, 1), round(moved.y, 1), round(moved.z, 1))
    lights = await motion.set_lights(button=True, rails=False)
    print("lights:", lights)
    print("is_simulating:", motion.is_simulating)

    pip = by_type["PipetteFeature"]
    attached = await pip.get_attached_pipettes()
    print("pipettes:", [(p.mount.value, p.attached, p.model) for p in attached])

    grip = by_type["GripperFeature"]
    print("gripper status:", grip.status())

    await agen.aclose()
    print("PASS: full Flex connector stack runs against the OT3 simulator")


if __name__ == "__main__":
    typing.TYPE_CHECKING  # keep import used
    asyncio.run(main())
