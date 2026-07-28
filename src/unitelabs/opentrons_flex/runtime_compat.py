"""Runtime compatibility contract for the deployed Flex connector."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import MutableMapping, MutableSequence
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

SUPPORTED_OPENTRONS_VERSION = "9.0.0"
SUPPORTED_OPENTRONS_SOURCE_COMMIT = "44b37a2f91520bf2e7245c70bf799d46c8c2d9a5"
SUPPORTED_PYTHON_VERSION = (3, 10)
SUPPORTED_RUNTIME_PACKAGES = {
    "aiohttp": "3.12.14",
    "anyio": "4.9.0",
    "fastapi": "0.100.0",
    "idna": "3.3",
    "paho-mqtt": "1.6.1",
    "pydantic": "2.11.7",
    "pydantic-settings": "2.4.0",
    "Pyro5": "5.17",
    "python-can": "4.2.2",
    "python-dotenv": "1.0.1",
    "python-multipart": "0.0.18",
    "SQLAlchemy": "1.4.51",
    "unitelabs-flex-acceptance-contract": "0.1.0",
    "uvicorn": "0.27.0.post1",
    "wsproto": "1.2.0",
}

_ROBOT_SERVER_SYMBOLS = (
    ("robot_server.app", "app"),
    ("robot_server.hardware", "_hw_api_accessor"),
    ("robot_server.hardware", "_init_task_accessor"),
    ("robot_server.hardware", "get_deck_type"),
    ("robot_server.hardware", "get_robot_type"),
    ("robot_server.hardware", "get_robot_type_enum"),
    ("robot_server.runs.dependencies", "mark_light_control_startup_finished"),
    ("robot_server.runs.dependencies", "start_light_control_task"),
)

_ROBOT_SERVER_SHAPE_REQUIREMENTS = (
    ("robot_server.hardware", "_hw_api_accessor.set_on", "callable"),
    ("robot_server.hardware", "_init_task_accessor.set_on", "callable"),
    ("robot_server.hardware", "get_deck_type", "callable"),
    ("robot_server.hardware", "get_robot_type", "callable"),
    ("robot_server.hardware", "get_robot_type_enum", "callable"),
    ("robot_server.runs.dependencies", "mark_light_control_startup_finished", "callable"),
    ("robot_server.runs.dependencies", "start_light_control_task", "callable"),
    ("robot_server.app", "app.state", "attribute"),
    ("robot_server.app", "app.dependency_overrides", "mutable-mapping"),
    ("robot_server.app", "app.router.lifespan_context", "callable"),
    ("robot_server.app", "app.router.routes", "mutable-sequence"),
    ("robot_server.app", "app.include_router", "callable"),
    ("robot_server.app", "app.openapi_schema", "attribute"),
)

_ROBOT_SERVER_INSTANCE_CONTRACTS = (
    "app.state.run_orchestrator_store.current_run_id",
    "app.state.run_orchestrator_store.get_status()",
    "app.state.run_orchestrator_store.run_was_started()",
    "app.state.run_orchestrator_store.get_current_command()",
    "app.state.run_orchestrator_store.get_command(command_id)",
    "app.state.run_orchestrator_store.run_orchestrator._protocol_runner",
    "app.state.run_orchestrator_store.run_orchestrator._protocol_engine.state_view",
)

_OPENTRONS_BASE_SYMBOLS = (
    ("opentrons.hardware_control.ot3api", "OT3API"),
    ("opentrons.hardware_control.types", "OT3Mount"),
    ("opentrons.protocol_engine", "DeckType"),
    ("opentrons_shared_data.robot.types", "RobotTypeEnum"),
)

_BASE_RELEASE_MODULES = (
    "opentrons",
    "opentrons_shared_data",
)

_ROBOT_SERVER_RELEASE_MODULES = (
    "opentrons_hardware",
    "server_utils",
)

_MUTATION_SYMBOLS = (
    ("opentrons.protocol_engine.state.tips", "TipView"),
    ("opentrons.protocol_engine.types", "MotorAxis"),
    ("opentrons.protocol_engine.types", "TipRackWellState"),
)


@dataclass(frozen=True)
class RuntimeContractCheck:
    """One machine-readable private runtime contract check."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class RuntimeCompatibilityReport:
    """Read-only compatibility evidence gathered before hardware initialization."""

    connector_version: str
    python_version: str
    opentrons_version: str
    robot_server_version: str | None
    robot_server_source: str | None
    runtime_package_versions: dict[str, str]
    runtime_contract_id: str
    private_api_checks: tuple[RuntimeContractCheck, ...]
    base_compatible: bool
    mutation_compatible: bool
    issues: tuple[str, ...]
    mutation_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report."""
        return asdict(self)


def inspect_runtime_compatibility(*, require_robot_server: bool) -> RuntimeCompatibilityReport:
    """Inspect the effective Python, Opentrons, and robot-server runtime."""
    connector_version = _package_version("unitelabs-opentrons-flex")
    opentrons_version = _package_version("opentrons")
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    issues: list[str] = []
    mutation_issues: list[str] = []

    if sys.version_info[:2] != SUPPORTED_PYTHON_VERSION:
        expected = ".".join(str(part) for part in SUPPORTED_PYTHON_VERSION)
        issues.append(f"Python {python_version} is unsupported; expected Python {expected}.x.")
    if opentrons_version != SUPPORTED_OPENTRONS_VERSION:
        issues.append(
            f"Opentrons {opentrons_version} is unsupported; expected Opentrons {SUPPORTED_OPENTRONS_VERSION}."
        )
    runtime_package_versions = {
        package_name: _package_version(package_name) for package_name in SUPPORTED_RUNTIME_PACKAGES
    }
    if require_robot_server:
        for package_name, expected_version in SUPPORTED_RUNTIME_PACKAGES.items():
            actual_version = runtime_package_versions[package_name]
            if actual_version != expected_version:
                issues.append(
                    f"{package_name} {actual_version} is unsupported; expected {package_name} {expected_version}."
                )
    for module_name, attribute_name in _OPENTRONS_BASE_SYMBOLS:
        issue = _missing_symbol_issue(module_name, attribute_name)
        if issue is not None:
            issues.append(issue)
    release_modules = _BASE_RELEASE_MODULES
    if require_robot_server:
        release_modules = (*release_modules, *_ROBOT_SERVER_RELEASE_MODULES)
    for module_name in release_modules:
        issue = _module_outside_release_issue(module_name)
        if issue is not None:
            issues.append(issue)

    robot_server_version: str | None = None
    robot_server_source: str | None = None
    private_api_checks: tuple[RuntimeContractCheck, ...] = ()
    if require_robot_server:
        robot_server_version, robot_server_source, robot_server_issues, private_api_checks = _inspect_robot_server()
        issues.extend(robot_server_issues)

    for module_name, attribute_name in _MUTATION_SYMBOLS:
        issue = _missing_symbol_issue(module_name, attribute_name)
        if issue is not None:
            mutation_issues.append(issue)

    base_compatible = not issues
    if not base_compatible:
        mutation_issues.insert(0, "The base connector runtime is incompatible.")
    mutation_compatible = base_compatible and not mutation_issues
    return RuntimeCompatibilityReport(
        connector_version=connector_version,
        python_version=python_version,
        opentrons_version=opentrons_version,
        robot_server_version=robot_server_version,
        robot_server_source=robot_server_source,
        runtime_package_versions=runtime_package_versions,
        runtime_contract_id=RUNTIME_CONTRACT_ID,
        private_api_checks=private_api_checks,
        base_compatible=base_compatible,
        mutation_compatible=mutation_compatible,
        issues=tuple(issues),
        mutation_issues=tuple(mutation_issues),
    )


def require_compatible_runtime(*, require_robot_server: bool) -> RuntimeCompatibilityReport:
    """Return compatibility evidence or fail before any hardware is initialized."""
    report = inspect_runtime_compatibility(require_robot_server=require_robot_server)
    if not report.base_compatible:
        detail = " ".join(report.issues)
        message = f"Flex connector runtime preflight failed: {detail}"
        raise RuntimeError(message)
    return report


def mutation_configuration_issues(
    report: RuntimeCompatibilityReport,
    *,
    ledger_path: str | None,
    token: str | None,
    actor: str | None,
) -> tuple[str, ...]:
    """Return reasons controlled mutation cannot be enabled."""
    issues = list(report.mutation_issues)
    if ledger_path is None:
        issues.append("run_mutation_ledger_path is not configured.")
    if token is None or len(token) < 32:
        issues.append("The configured mutation token is missing or shorter than 32 characters.")
    if actor is None or not actor.strip() or len(actor.strip()) > 200:
        issues.append("The configured mutation actor must contain 1 to 200 non-whitespace characters.")
    return tuple(issues)


def _inspect_robot_server() -> tuple[
    str | None,
    str | None,
    tuple[str, ...],
    tuple[RuntimeContractCheck, ...],
]:
    issues: list[str] = []
    try:
        package = importlib.import_module("robot_server")
    except (ImportError, OSError) as exc:
        return None, None, (f"robot_server could not be imported: {exc}",), ()

    source_value = getattr(package, "__file__", None)
    source = str(Path(source_value).resolve()) if isinstance(source_value, str) else None
    robot_server_version = _robot_server_version()
    is_test_stub = getattr(package, "__spec__", None) is None
    if not is_test_stub and robot_server_version != SUPPORTED_OPENTRONS_VERSION:
        issues.append(f"robot_server {robot_server_version} does not match Opentrons {SUPPORTED_OPENTRONS_VERSION}.")
    if not is_test_stub:
        if source is None:
            issues.append("robot_server source location is unavailable.")
        else:
            source_path = Path(source)
            runtime_prefix = Path(sys.prefix).resolve()
            if not source_path.is_relative_to(runtime_prefix):
                issues.append(
                    f"robot_server is loaded from {source_path}, outside the active release {runtime_prefix}."
                )
    for module_name, attribute_name in _ROBOT_SERVER_SYMBOLS:
        issue = _missing_symbol_issue(module_name, attribute_name)
        if issue is not None:
            issues.append(issue)
    private_api_checks = tuple(
        _shape_check(module_name, attribute_path, expected_kind)
        for module_name, attribute_path, expected_kind in _ROBOT_SERVER_SHAPE_REQUIREMENTS
    )
    issues.extend(check.detail for check in private_api_checks if not check.ok)
    return robot_server_version, source, tuple(issues), private_api_checks


def _robot_server_version() -> str | None:
    for package_name in ("robot-server", "robot_server"):
        try:
            return version(package_name)
        except PackageNotFoundError:
            continue
    try:
        version_module = importlib.import_module("robot_server._version")
    except (ImportError, OSError):
        return None
    value = getattr(version_module, "__version__", None)
    return value if isinstance(value, str) else None


def _missing_symbol_issue(module_name: str, attribute_name: str) -> str | None:
    try:
        module = importlib.import_module(module_name)
    except (ImportError, OSError) as exc:
        return f"{module_name} could not be imported: {exc}"
    if not hasattr(module, attribute_name):
        return f"{module_name}.{attribute_name} is unavailable."
    return None


def _shape_check(module_name: str, attribute_path: str, expected_kind: str) -> RuntimeContractCheck:
    name = f"{module_name}.{attribute_path}"
    try:
        value: object = importlib.import_module(module_name)
        for part in attribute_path.split("."):
            value = getattr(value, part)
    except (ImportError, OSError, AttributeError) as exc:
        return RuntimeContractCheck(name=name, ok=False, detail=f"{name} is unavailable: {exc}")

    kind_ok = {
        "attribute": True,
        "callable": callable(value),
        "mutable-mapping": isinstance(value, MutableMapping),
        "mutable-sequence": isinstance(value, MutableSequence),
    }[expected_kind]
    if not kind_ok:
        return RuntimeContractCheck(
            name=name,
            ok=False,
            detail=f"{name} does not satisfy the required {expected_kind} contract.",
        )
    return RuntimeContractCheck(name=name, ok=True, detail=f"{name} satisfies {expected_kind}.")


def _module_outside_release_issue(module_name: str) -> str | None:
    try:
        module = importlib.import_module(module_name)
    except (ImportError, OSError) as exc:
        return f"{module_name} could not be imported: {exc}"
    if getattr(module, "__spec__", None) is None:
        return None
    source_value = getattr(module, "__file__", None)
    if not isinstance(source_value, str):
        return f"{module_name} source location is unavailable."
    source_path = Path(source_value).resolve()
    runtime_prefix = Path(sys.prefix).resolve()
    if not source_path.is_relative_to(runtime_prefix):
        return f"{module_name} is loaded from {source_path}, outside the active release {runtime_prefix}."
    return None


def _package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "not-installed"


def _runtime_contract_id() -> str:
    payload = {
        "python": SUPPORTED_PYTHON_VERSION,
        "opentrons": SUPPORTED_OPENTRONS_VERSION,
        "opentronsSourceCommit": SUPPORTED_OPENTRONS_SOURCE_COMMIT,
        "packages": SUPPORTED_RUNTIME_PACKAGES,
        "robotServerShapes": _ROBOT_SERVER_SHAPE_REQUIREMENTS,
        "robotServerInstances": _ROBOT_SERVER_INSTANCE_CONTRACTS,
        "mutationSymbols": _MUTATION_SYMBOLS,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"flex-runtime-{hashlib.sha256(encoded).hexdigest()[:16]}"


RUNTIME_CONTRACT_ID = _runtime_contract_id()


__all__ = [
    "RUNTIME_CONTRACT_ID",
    "SUPPORTED_OPENTRONS_SOURCE_COMMIT",
    "SUPPORTED_OPENTRONS_VERSION",
    "SUPPORTED_PYTHON_VERSION",
    "SUPPORTED_RUNTIME_PACKAGES",
    "RuntimeCompatibilityReport",
    "RuntimeContractCheck",
    "inspect_runtime_compatibility",
    "mutation_configuration_issues",
    "require_compatible_runtime",
]
