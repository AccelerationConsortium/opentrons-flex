"""Static checks for workflow packages published to UniteLabs cloud."""

import importlib
import importlib.metadata
import inspect
import sys
import types

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"

WORKFLOW_PACKAGES = (
    "ot2-home",
    "ot2-transfer",
    "ot2-jingle",
)

WORKFLOW_MODULES = {
    "ot2-home": ("ot2_home.workflow", "ot2_home_flow"),
    "ot2-transfer": ("ot2_transfer.workflow", "ot2_transfer_flow"),
    "ot2-jingle": ("ot2_jingle.workflow", "ot2_jingle_flow"),
}


def _pyproject(package: str) -> dict:
    with (WORKFLOWS / package / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _install_workflow_runtime_stubs(monkeypatch) -> None:
    """Install tiny Prefect/SDK stubs so workflow packages can be imported offline."""

    real_version = importlib.metadata.version

    def version_with_workflow_fallback(name: str) -> str:
        if name in {"ot2_home", "ot2_transfer", "ot2_jingle", "shared"}:
            return "0.0.0+test"
        return real_version(name)

    def _decorator(kind: str):
        def factory(**decorator_kwargs):
            def wrap(fn):
                fn.__prefect_kind__ = kind
                fn.__prefect_name__ = decorator_kwargs.get("name")
                return fn

            return wrap

        return factory

    prefect = types.ModuleType("prefect")
    prefect.flow = _decorator("flow")
    prefect.task = _decorator("task")

    cache_policies = types.ModuleType("prefect.cache_policies")
    cache_policies.NONE = object()

    sdk = types.ModuleType("unitelabs.sdk")

    class _Logger:
        def info(self, *_args, **_kwargs) -> None:
            pass

    class _Client:
        pass

    def get_logger() -> _Logger:
        return _Logger()

    sdk.Client = _Client
    sdk.get_logger = get_logger

    monkeypatch.setitem(sys.modules, "prefect", prefect)
    monkeypatch.setitem(sys.modules, "prefect.cache_policies", cache_policies)
    monkeypatch.setitem(sys.modules, "unitelabs.sdk", sdk)
    monkeypatch.setattr(importlib.metadata, "version", version_with_workflow_fallback)


def test_workflow_packages_have_cloud_entrypoints() -> None:
    """Every workflow package declares a UniteLabs workflow entrypoint."""
    for package in WORKFLOW_PACKAGES:
        data = _pyproject(package)
        workflow = data["tool"]["unitelabs"]["workflow"]
        module_name, flow_name = workflow["entrypoint"].split(":")

        assert module_name == "workflow.py"
        assert flow_name.endswith("_flow")
        assert data["project"]["scripts"]["workflow"].endswith(":main")


def test_liquid_handler_workflows_are_tagged() -> None:
    """Cloud workflow metadata should preserve liquid-handler discoverability."""
    for package in WORKFLOW_PACKAGES:
        tags = set(_pyproject(package)["tool"]["unitelabs"]["workflow"]["tags"])
        assert "ot2" in tags


def test_transfer_workflow_uses_shared_labware_helpers() -> None:
    """The plate-transfer E2E workflow should keep its shared labware helper dependency."""
    data = _pyproject("ot2-transfer")

    assert "shared" in data["project"]["dependencies"]
    assert data["tool"]["uv"]["sources"]["shared"]["path"] == "../shared"


def test_workflow_entrypoints_import_and_build_as_flows(monkeypatch) -> None:
    """Each cloud workflow entrypoint should import and resolve to an async flow offline."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "shared" / "src"))
    for package in WORKFLOW_PACKAGES:
        monkeypatch.syspath_prepend(str(WORKFLOWS / package / "src"))

    for package, (module_name, function_name) in WORKFLOW_MODULES.items():
        for loaded in list(sys.modules):
            if loaded == module_name or loaded.startswith(module_name.split(".")[0] + "."):
                monkeypatch.delitem(sys.modules, loaded, raising=False)

        module = importlib.import_module(module_name)
        flow_fn = getattr(module, function_name)
        configured_entrypoint = _pyproject(package)["tool"]["unitelabs"]["workflow"]["entrypoint"]

        assert configured_entrypoint == f"workflow.py:{function_name}"
        assert inspect.iscoroutinefunction(flow_fn)
        assert getattr(flow_fn, "__prefect_kind__") == "flow"
        assert getattr(flow_fn, "__prefect_name__").startswith("Workflow:")
