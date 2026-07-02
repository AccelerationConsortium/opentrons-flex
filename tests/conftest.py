import importlib.metadata
import sys
import types
from unittest.mock import MagicMock

# The connector reads its version via importlib.metadata at import time. When the
# package is run from source without being pip-installed (offline dev), that
# lookup raises PackageNotFoundError; fall back to a placeholder so imports work.
_real_version = importlib.metadata.version


def _version_with_fallback(name: str) -> str:
    try:
        return _real_version(name)
    except importlib.metadata.PackageNotFoundError:
        if name == "unitelabs-opentrons-flex":
            return "0.0.0+dev"
        raise


importlib.metadata.version = _version_with_fallback

# gpiod is a Linux-only kernel library; stub it so tests run on any platform.
if "gpiod" not in sys.modules:
    sys.modules["gpiod"] = MagicMock()

# robot_server is an Opentrons-internal package not published to PyPI.
# Stub it when it is not installed so the with_robot_server=True wiring tests
# (test_create_app_with_robot_server.py) can run in CI without the real package.
# When the real robot_server IS installed (e.g. the HTTP integration step), the
# real package is used and these stubs are not inserted.
try:
    import robot_server as _  # noqa: F401
except ImportError:
    _rs = types.ModuleType("robot_server")
    _rs_hw = types.ModuleType("robot_server.hardware")
    _rs_hw._hw_api_accessor = MagicMock(name="_hw_api_accessor")
    _rs_hw._init_task_accessor = MagicMock(name="_init_task_accessor")
    _rs_app = types.ModuleType("robot_server.app")
    _rs_app.app = MagicMock(name="robot_server_app")
    sys.modules["robot_server"] = _rs
    sys.modules["robot_server.hardware"] = _rs_hw
    sys.modules["robot_server.app"] = _rs_app

try:
    import unitelabs.bus.testing.fixtures

    pytest_plugins = ["unitelabs.bus.testing.fixtures"]
except ImportError:
    pytest_plugins = []


# The unitelabs CDK lives on a private package index and may be absent in offline
# / open-source CI. When it is missing, install a minimal stub so the connector
# package imports and the *simulation* tests (feature -> controller -> OT3 hardware
# simulator) can run without it. The SiLA decorators become identity functions, so
# these tests exercise real controller/feature behaviour against the real opentrons
# simulator — they do NOT cover SiLA wire-format / feature-definition generation,
# which is verified separately in CI where the real CDK is installed.
try:
    import unitelabs.cdk  # noqa: F401

    _CDK_STUBBED = False
except ImportError:
    _cdk = types.ModuleType("unitelabs.cdk")

    class _Connector:
        def __init__(self, config=None):
            self.config = config
            self.features = []

        def register(self, feature):
            self.features.append(feature)

    class _ConnectorBaseConfig:
        pass

    class _SiLAServerConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.hostname = kw.get("hostname", "127.0.0.1")
            self.port = kw.get("port", 50051)

    _cdk.Connector = _Connector
    _cdk.ConnectorBaseConfig = _ConnectorBaseConfig
    _cdk.SiLAServerConfig = _SiLAServerConfig

    _sila = types.ModuleType("unitelabs.cdk.sila")

    class _Feature:
        def __init__(self, **kw):
            self._meta = kw

    def _passthrough(*_a, **_k):
        def wrap(fn):
            return fn

        return wrap

    class _Status:
        def update(self, **_kwargs):
            pass

    class _Intermediate:
        def send(self, *_responses):
            pass

    _sila.Feature = _Feature
    _sila.UnobservableCommand = _passthrough
    _sila.ObservableCommand = _passthrough
    _sila.UnobservableProperty = _passthrough
    _sila.ObservableProperty = _passthrough
    _sila.Status = _Status
    _sila.Intermediate = _Intermediate

    _constraints = types.ModuleType("unitelabs.cdk.sila.constraints")
    for _name in ("MinimalInclusive", "MaximalInclusive", "MinimalExclusive", "Pattern"):
        setattr(_constraints, _name, lambda *_a, **_k: object())
    _sila.constraints = _constraints

    sys.modules["unitelabs.cdk"] = _cdk
    sys.modules["unitelabs.cdk.sila"] = _sila
    sys.modules["unitelabs.cdk.sila.constraints"] = _constraints
    _CDK_STUBBED = True
