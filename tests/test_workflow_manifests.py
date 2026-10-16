"""Static checks for workflow packages published to UniteLabs cloud."""

import importlib
import importlib.metadata
import inspect
import sys
import types

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - isolated workflow environment
    import tomli as tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"

WORKFLOW_PACKAGES = (
    "ot2-home",
    "ot2-transfer",
    "ot2-jingle",
    "flex-system-acceptance",
)

WORKFLOW_MODULES = {
    "ot2-home": ("ot2_home.workflow", "ot2_home_flow"),
    "ot2-transfer": ("ot2_transfer.workflow", "ot2_transfer_flow"),
    "ot2-jingle": ("ot2_jingle.workflow", "ot2_jingle_flow"),
    "flex-system-acceptance": (
        "flex_system_acceptance.workflow",
        "flex_system_acceptance_flow",
    ),
}


def _pyproject(package: str) -> dict:
    with (WORKFLOWS / package / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _install_workflow_runtime_stubs(monkeypatch) -> None:
    """Install tiny Prefect/SDK stubs so workflow packages can be imported offline."""

    real_version = importlib.metadata.version

    def version_with_workflow_fallback(name: str) -> str:
        if name in {"ot2_home", "ot2_transfer", "ot2_jingle", "flex-system-acceptance", "shared"}:
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

        def error(self, *_args, **_kwargs) -> None:
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
    for package in ("ot2-home", "ot2-transfer", "ot2-jingle"):
        tags = set(_pyproject(package)["tool"]["unitelabs"]["workflow"]["tags"])
        assert "ot2" in tags


@pytest.mark.flex_workflow_offline
def test_flex_acceptance_workflow_is_hardware_tagged() -> None:
    """The Flex workflow must be unmistakably hardware-facing in the catalog."""
    tags = set(_pyproject("flex-system-acceptance")["tool"]["unitelabs"]["workflow"]["tags"])
    assert {"flex", "liquid-handler", "hardware-acceptance"} <= tags


@pytest.mark.flex_workflow_offline
def test_flex_workflow_uses_cross_runtime_contract_instead_of_connector_package() -> None:
    """The workflow must not depend on connector-only robot runtime packages."""
    data = _pyproject("flex-system-acceptance")
    dependencies = set(data["project"]["dependencies"])

    assert data["project"]["requires-python"] == ">=3.12"
    assert "unitelabs-flex-acceptance-contract==0.1.0" in dependencies
    assert all(not dependency.startswith("unitelabs-opentrons-flex") for dependency in dependencies)
    assert data["tool"]["uv"]["sources"]["unitelabs-flex-acceptance-contract"]["path"] == (
        "../../packages/flex-acceptance-contract"
    )


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


@pytest.mark.flex_workflow_offline
def test_flex_workflow_entrypoint_imports_as_a_python_312_safe_flow(monkeypatch) -> None:
    """The Flex entrypoint imports without the connector runtime."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "flex-system-acceptance" / "src"))
    for loaded in list(sys.modules):
        if loaded == "flex_system_acceptance" or loaded.startswith("flex_system_acceptance."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)

    module = importlib.import_module("flex_system_acceptance.workflow")
    flow_fn = module.flex_system_acceptance_flow

    assert inspect.iscoroutinefunction(flow_fn)
    assert getattr(flow_fn, "__prefect_kind__") == "flow"
    assert getattr(flow_fn, "__prefect_name__") == "Workflow: Flex System Acceptance"


@pytest.mark.flex_workflow_offline
@pytest.mark.asyncio
async def test_flex_workflow_phases_execute_the_declared_sdk_sequence(monkeypatch) -> None:
    """Run every hardware phase against a recorder so Python/SDK wiring errors fail offline."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "flex-system-acceptance" / "src"))
    for loaded in list(sys.modules):
        if loaded == "flex_system_acceptance" or loaded.startswith("flex_system_acceptance."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)

    steps = importlib.import_module("flex_system_acceptance._steps")
    from tests.test_acceptance_manifest import _manifest
    from unitelabs_flex_acceptance_contract import AcceptanceManifest

    manifest = AcceptanceManifest.parse(_manifest())
    features = {
        name: object()
        for name in (
            "motion",
            "pipette",
            "tip",
            "liquid",
            "labware",
            "heater_shaker",
            "thermocycler",
            "temperature_module",
            "reader",
            "stacker",
            "stacker_maintenance",
        )
    }
    calls: list[tuple[str, dict]] = []

    async def fake_invoke(_target, method_names, **parameters):
        method = method_names if isinstance(method_names, str) else next(iter(method_names))
        calls.append((method, parameters))
        if method == "read_plate":
            return {"Measurements": [{"Wells": [0.0] * 96}]}
        return None

    async def fake_move(_features, plan_identifier: str, *, lid: bool = False):
        calls.append(("move_lid" if lid else "move_labware", {"plan_identifier": plan_identifier}))

    monkeypatch.setattr(steps, "invoke", fake_invoke)
    monkeypatch.setattr(steps, "_move", fake_move)

    await steps.home_and_configure_step(features, manifest)
    await steps.stacker_step(features, manifest)
    await steps.thermocycler_step(features, manifest)
    await steps.liquid_handling_step(features, manifest)
    await steps.heater_shaker_step(features, manifest)
    await steps.temperature_module_step(features, manifest)
    await steps.plate_reader_step(features, manifest)

    methods = [method for method, _ in calls]
    assert methods[:3] == ["set_lights", "home", "configure_full_nozzle_layout"]
    assert {"retrieve_labware", "execute_profile", "probe_liquid_level", "transfer"} <= set(methods)
    assert {"transfer_with_verified_liquid_class", "set_speed", "set_temperature_and_wait", "read_plate"} <= set(
        methods
    )
    assert methods.count("move_labware") == 9
    assert methods.count("move_lid") == 4


@pytest.mark.flex_workflow_offline
def test_flex_cloud_workflow_requires_the_direct_hitl_manifest_digest(monkeypatch) -> None:
    """Runtime callers cannot substitute uncommissioned coordinates or module identities."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "flex-system-acceptance" / "src"))
    for loaded in list(sys.modules):
        if loaded == "flex_system_acceptance" or loaded.startswith("flex_system_acceptance."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)

    workflow = importlib.import_module("flex_system_acceptance.workflow")
    from tests.test_acceptance_manifest import _manifest
    from unitelabs_flex_acceptance_contract import AcceptanceManifest

    manifest = AcceptanceManifest.parse(_manifest())
    monkeypatch.delenv("FLEX_ACCEPTANCE_MANIFEST_SHA256", raising=False)
    with pytest.raises(ValueError, match="64-character digest"):
        workflow._require_commissioned_manifest(manifest)

    monkeypatch.setenv("FLEX_ACCEPTANCE_MANIFEST_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="differs"):
        workflow._require_commissioned_manifest(manifest)

    monkeypatch.setenv("FLEX_ACCEPTANCE_MANIFEST_SHA256", manifest.commissioning_digest())
    workflow._require_commissioned_manifest(manifest)


@pytest.mark.flex_workflow_offline
@pytest.mark.asyncio
async def test_flex_workflow_shutdown_attempts_every_action_and_reports_all_failures(monkeypatch) -> None:
    """Shutdown must settle every energy-producing feature without hiding failures."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "flex-system-acceptance" / "src"))
    for loaded in list(sys.modules):
        if loaded == "flex_system_acceptance" or loaded.startswith("flex_system_acceptance."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)

    steps = importlib.import_module("flex_system_acceptance._steps")
    feature_names = (
        "heater_shaker",
        "thermocycler",
        "temperature_module",
        "reader",
        "stacker_maintenance",
        "motion",
    )
    features = {name: object() for name in feature_names}
    names_by_feature = {id(feature): name for name, feature in features.items()}
    calls: list[tuple[str, str, dict]] = []

    async def fake_invoke(target, method_names, **parameters):
        feature_name = names_by_feature[id(target)]
        method = method_names if isinstance(method_names, str) else next(iter(method_names))
        calls.append((feature_name, method, parameters))
        if (feature_name, method) in {
            ("heater_shaker", "stop_shaking"),
            ("temperature_module", "deactivate"),
        }:
            raise RuntimeError(f"{method} failed")

    monkeypatch.setattr(steps, "invoke", fake_invoke)

    failures = await steps.safe_shutdown(features)

    assert [(feature, method) for feature, method, _ in calls] == [
        ("heater_shaker", "stop_shaking"),
        ("heater_shaker", "deactivate_heater"),
        ("thermocycler", "deactivate_all"),
        ("temperature_module", "deactivate"),
        ("reader", "deactivate"),
        ("stacker_maintenance", "deactivate"),
        ("motion", "set_lights"),
    ]
    assert calls[-1][2] == {"button": False, "rails": False}
    assert failures == (
        "heater_shaker.stop_shaking: RuntimeError: stop_shaking failed",
        "temperature_module.deactivate: RuntimeError: deactivate failed",
    )


@pytest.mark.flex_workflow_offline
@pytest.mark.asyncio
async def test_flex_workflow_refuses_success_when_shutdown_is_incomplete(monkeypatch) -> None:
    """A successful hardware sequence is not accepted when de-energization fails."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "flex-system-acceptance" / "src"))
    for loaded in list(sys.modules):
        if loaded == "flex_system_acceptance" or loaded.startswith("flex_system_acceptance."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)

    workflow = importlib.import_module("flex_system_acceptance.workflow")
    from tests.test_acceptance_manifest import _manifest
    from unitelabs_flex_acceptance_contract import AcceptanceManifest

    manifest = AcceptanceManifest.parse(_manifest())
    monkeypatch.setenv("FLEX_ACCEPTANCE_MANIFEST_SHA256", manifest.commissioning_digest())
    phase_calls: list[str] = []

    async def validate(value):
        return AcceptanceManifest.parse(value)

    async def connect(_device_name, _manifest):
        return object(), object(), {}

    async def phase(_features, _manifest):
        phase_calls.append("phase")

    async def shutdown(_features):
        phase_calls.append("shutdown")
        return ("heater_shaker.stop_shaking: RuntimeError: timeout",)

    monkeypatch.setattr(workflow, "validate_manifest_step", validate)
    monkeypatch.setattr(workflow, "connect_and_preflight_step", connect)
    for name in (
        "home_and_configure_step",
        "stacker_step",
        "thermocycler_step",
        "liquid_handling_step",
        "heater_shaker_step",
        "temperature_module_step",
        "plate_reader_step",
    ):
        monkeypatch.setattr(workflow, name, phase)
    monkeypatch.setattr(workflow, "safe_shutdown", shutdown)

    with pytest.raises(RuntimeError, match="workflow is not accepted"):
        await workflow.flex_system_acceptance_flow(_manifest(), device_name=manifest.service_name)

    assert phase_calls == ["phase"] * 7 + ["shutdown"]


@pytest.mark.flex_workflow_offline
@pytest.mark.asyncio
async def test_flex_workflow_preserves_run_failure_when_shutdown_succeeds(monkeypatch) -> None:
    """Cleanup must run after a phase failure without masking that original failure."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "flex-system-acceptance" / "src"))
    for loaded in list(sys.modules):
        if loaded == "flex_system_acceptance" or loaded.startswith("flex_system_acceptance."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)

    workflow = importlib.import_module("flex_system_acceptance.workflow")
    from tests.test_acceptance_manifest import _manifest
    from unitelabs_flex_acceptance_contract import AcceptanceManifest

    manifest = AcceptanceManifest.parse(_manifest())
    monkeypatch.setenv("FLEX_ACCEPTANCE_MANIFEST_SHA256", manifest.commissioning_digest())
    shutdown_called = False

    async def validate(value):
        return AcceptanceManifest.parse(value)

    async def connect(_device_name, _manifest):
        return object(), object(), {}

    async def fail_phase(_features, _manifest):
        raise ValueError("phase failed")

    async def shutdown(_features):
        nonlocal shutdown_called
        shutdown_called = True
        return ()

    monkeypatch.setattr(workflow, "validate_manifest_step", validate)
    monkeypatch.setattr(workflow, "connect_and_preflight_step", connect)
    monkeypatch.setattr(workflow, "home_and_configure_step", fail_phase)
    monkeypatch.setattr(workflow, "safe_shutdown", shutdown)

    with pytest.raises(ValueError, match="phase failed"):
        await workflow.flex_system_acceptance_flow(_manifest(), device_name=manifest.service_name)

    assert shutdown_called is True


@pytest.mark.flex_workflow_offline
@pytest.mark.asyncio
async def test_flex_workflow_reports_shutdown_failure_with_phase_failure_as_cause(monkeypatch) -> None:
    """Dual failures must preserve the phase error while making unsafe shutdown visible."""
    _install_workflow_runtime_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(WORKFLOWS / "flex-system-acceptance" / "src"))
    for loaded in list(sys.modules):
        if loaded == "flex_system_acceptance" or loaded.startswith("flex_system_acceptance."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)

    workflow = importlib.import_module("flex_system_acceptance.workflow")
    from tests.test_acceptance_manifest import _manifest
    from unitelabs_flex_acceptance_contract import AcceptanceManifest

    manifest = AcceptanceManifest.parse(_manifest())
    monkeypatch.setenv("FLEX_ACCEPTANCE_MANIFEST_SHA256", manifest.commissioning_digest())

    async def validate(value):
        return AcceptanceManifest.parse(value)

    async def connect(_device_name, _manifest):
        return object(), object(), {}

    async def fail_phase(_features, _manifest):
        raise ValueError("phase failed")

    async def fail_shutdown(_features):
        return ("thermocycler.deactivate_all: RuntimeError: timeout",)

    monkeypatch.setattr(workflow, "validate_manifest_step", validate)
    monkeypatch.setattr(workflow, "connect_and_preflight_step", connect)
    monkeypatch.setattr(workflow, "home_and_configure_step", fail_phase)
    monkeypatch.setattr(workflow, "safe_shutdown", fail_shutdown)

    with pytest.raises(RuntimeError, match="safety shutdown was incomplete") as failure:
        await workflow.flex_system_acceptance_flow(_manifest(), device_name=manifest.service_name)

    assert isinstance(failure.value.__cause__, ValueError)
    assert str(failure.value.__cause__) == "phase failed"
