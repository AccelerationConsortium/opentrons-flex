"""Fail-closed adapter for the connector's version-pinned robot-server APIs."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass

from .io.run_authority import ProtocolRunState


@dataclass(frozen=True)
class RobotServerBindings:
    """Private robot-server objects validated before hardware initialization."""

    app: object
    hardware_accessor: object
    initialization_task_accessor: object
    get_deck_type: object
    get_robot_type: object
    get_robot_type_enum: object
    mark_light_control_startup_finished: object
    start_light_control_task: object


@dataclass(frozen=True)
class RobotServerAppInstallation:
    """Reversible mutation of the global robot-server FastAPI app."""

    app: object
    app_state: object
    hardware_accessor: object
    initialization_task_accessor: object
    original_hardware: object | None
    original_initialization_task: object | None
    router: object
    dependency_overrides: MutableMapping[object, object]
    original_lifespan: object
    original_identity_overrides: tuple[tuple[object, bool, object | None], ...]


def load_robot_server_bindings() -> RobotServerBindings:
    """Load the exact private bindings covered by the runtime contract."""
    try:
        app_module = importlib.import_module("robot_server.app")
        hardware_module = importlib.import_module("robot_server.hardware")
        run_dependencies = importlib.import_module("robot_server.runs.dependencies")
        return RobotServerBindings(
            app=_required_attribute(app_module, "app", "robot_server.app"),
            hardware_accessor=_required_attribute(
                hardware_module,
                "_hw_api_accessor",
                "robot_server.hardware",
            ),
            initialization_task_accessor=_required_attribute(
                hardware_module,
                "_init_task_accessor",
                "robot_server.hardware",
            ),
            get_deck_type=_required_callable(hardware_module, "get_deck_type", "robot_server.hardware"),
            get_robot_type=_required_callable(hardware_module, "get_robot_type", "robot_server.hardware"),
            get_robot_type_enum=_required_callable(
                hardware_module,
                "get_robot_type_enum",
                "robot_server.hardware",
            ),
            mark_light_control_startup_finished=_required_callable(
                run_dependencies,
                "mark_light_control_startup_finished",
                "robot_server.runs.dependencies",
            ),
            start_light_control_task=_required_callable(
                run_dependencies,
                "start_light_control_task",
                "robot_server.runs.dependencies",
            ),
        )
    except (ImportError, OSError) as exc:
        message = f"Validated robot-server bindings could not be loaded: {exc}"
        raise RuntimeError(message) from exc


def current_run_store(
    bindings: RobotServerBindings,
    *,
    robot_server_ready: bool,
) -> object | None:
    """Return the current run store, allowing absence only during startup."""
    app_state = _required_attribute(bindings.app, "state", "robot_server.app.app")
    store = getattr(app_state, "run_orchestrator_store", None)
    if store is None and robot_server_ready:
        message = (
            "robot-server startup completed without the validated run_orchestrator_store. "
            "Direct Flex control remains unavailable."
        )
        raise RuntimeError(message)
    return store


def install_robot_server_app(
    bindings: RobotServerBindings,
    *,
    initialization_task: object,
    hardware_proxy: object,
    identity_overrides: Mapping[object, object],
    lifespan_factory: Callable[[object], object],
) -> RobotServerAppInstallation:
    """Install shared hardware and reversible Flex identity/lifespan wiring."""
    app = bindings.app
    app_state = _required_attribute(app, "state", "robot_server.app.app")
    set_initialization_task = _required_method(
        bindings.initialization_task_accessor,
        "set_on",
        "robot-server initialization task accessor",
    )
    set_hardware = _required_method(
        bindings.hardware_accessor,
        "set_on",
        "robot-server hardware accessor",
    )
    get_initialization_task = _required_method(
        bindings.initialization_task_accessor,
        "get_from",
        "robot-server initialization task accessor",
    )
    get_hardware = _required_method(
        bindings.hardware_accessor,
        "get_from",
        "robot-server hardware accessor",
    )

    dependency_overrides = _required_attribute(
        app,
        "dependency_overrides",
        "robot_server.app.app",
    )
    if not isinstance(dependency_overrides, MutableMapping):
        message = "robot_server.app.app.dependency_overrides must be a mutable mapping."
        raise RuntimeError(message)
    router = _required_attribute(app, "router", "robot_server.app.app")
    original_lifespan = _required_attribute(router, "lifespan_context", "robot-server app router")
    replacement_lifespan = lifespan_factory(original_lifespan)
    if not callable(replacement_lifespan):
        message = "The shared-hardware robot-server lifespan factory must return a callable."
        raise RuntimeError(message)

    original_overrides = tuple(
        (dependency, dependency in dependency_overrides, dependency_overrides.get(dependency))
        for dependency in identity_overrides
    )
    installation = RobotServerAppInstallation(
        app=app,
        app_state=app_state,
        hardware_accessor=bindings.hardware_accessor,
        initialization_task_accessor=bindings.initialization_task_accessor,
        original_hardware=get_hardware(app_state),
        original_initialization_task=get_initialization_task(app_state),
        router=router,
        dependency_overrides=dependency_overrides,
        original_lifespan=original_lifespan,
        original_identity_overrides=original_overrides,
    )

    # All fallible shape validation happens above. Mutate the pinned global app
    # only after the complete installation contract is known to be usable.
    # Roll back internally because the caller cannot register cleanup until
    # this function returns an installation handle.
    try:
        set_initialization_task(app_state, initialization_task)
        set_hardware(app_state, hardware_proxy)
        dependency_overrides.update(identity_overrides)
        router.lifespan_context = replacement_lifespan
    except Exception as exc:
        try:
            restore_robot_server_app(installation, added_routes=())
        except (AttributeError, RuntimeError, TypeError) as rollback_exc:
            message = f"robot-server app installation failed and rollback was incomplete: {rollback_exc}"
            raise RuntimeError(message) from exc
        raise
    return installation


def include_robot_server_router(
    installation: RobotServerAppInstallation,
    router: object,
) -> tuple[object, ...]:
    """Include one temporary router and return the exact route identities added."""
    temporary_routes = _required_attribute(router, "routes", "temporary robot-server router")
    if not isinstance(temporary_routes, MutableSequence):
        message = "Temporary robot-server router routes must be a mutable sequence."
        raise RuntimeError(message)
    app_routes = _required_attribute(installation.router, "routes", "robot-server app router")
    if not isinstance(app_routes, MutableSequence):
        message = "robot-server app router routes must be a mutable sequence."
        raise RuntimeError(message)
    include_router = _required_method(installation.app, "include_router", "robot_server.app.app")
    original_route_ids = {id(route) for route in app_routes}
    try:
        include_router(router)
    except Exception:
        app_routes[:] = [route for route in app_routes if id(route) in original_route_ids]
        installation.app.openapi_schema = None
        raise
    added_routes = tuple(route for route in app_routes if id(route) not in original_route_ids)
    if temporary_routes and not added_routes:
        message = "robot_server.app.app.include_router did not register the requested routes."
        raise RuntimeError(message)
    installation.app.openapi_schema = None
    return added_routes


def restore_robot_server_app(
    installation: RobotServerAppInstallation,
    *,
    added_routes: tuple[object, ...],
) -> None:
    """Restore global robot-server app state after connector shutdown."""
    set_hardware = _required_method(
        installation.hardware_accessor,
        "set_on",
        "robot-server hardware accessor",
    )
    set_initialization_task = _required_method(
        installation.initialization_task_accessor,
        "set_on",
        "robot-server initialization task accessor",
    )
    try:
        try:
            installation.router.lifespan_context = installation.original_lifespan
            if added_routes:
                routes = _required_attribute(installation.router, "routes", "robot-server app router")
                if not isinstance(routes, MutableSequence):
                    message = "robot-server app router routes must remain a mutable sequence."
                    raise RuntimeError(message)
                added_route_ids = {id(route) for route in added_routes}
                routes[:] = [route for route in routes if id(route) not in added_route_ids]
                installation.app.openapi_schema = None
        finally:
            for dependency, existed, original in installation.original_identity_overrides:
                if existed:
                    installation.dependency_overrides[dependency] = original
                else:
                    installation.dependency_overrides.pop(dependency, None)
    finally:
        try:
            set_hardware(installation.app_state, installation.original_hardware)
        finally:
            set_initialization_task(
                installation.app_state,
                installation.original_initialization_task,
            )


def protocol_run_state(
    store: object | None,
    *,
    robot_server_ready: bool,
    checkpoint_prefix: str,
) -> ProtocolRunState:
    """Read the private run-store contract used to arbitrate hardware ownership."""
    if store is None:
        if not robot_server_ready:
            return ProtocolRunState(
                run_id="robot-server-startup",
                status="initializing",
                started=False,
                terminal=False,
            )
        return ProtocolRunState(run_id=None, status="idle", started=False, terminal=True)

    run_id = _required_attribute(store, "current_run_id", "robot-server run store")
    if run_id is None:
        return ProtocolRunState(run_id=None, status="idle", started=False, terminal=True)
    if not isinstance(run_id, str) or not run_id:
        message = "robot-server run store current_run_id must be a non-empty string or None."
        raise RuntimeError(message)

    status = _required_method(store, "get_status", "robot-server run store")()
    status_value = str(getattr(status, "value", status))
    orchestrator = _required_attribute(store, "run_orchestrator", "robot-server run store")
    protocol_runner = _required_attribute(
        orchestrator,
        "_protocol_runner",
        "robot-server run orchestrator",
    )
    checkpoint_id = None
    current_pointer = _required_method(store, "get_current_command", "robot-server run store")()
    if current_pointer is not None:
        command_id = _required_attribute(
            current_pointer,
            "command_id",
            "robot-server current command pointer",
        )
        current_command = _required_method(store, "get_command", "robot-server run store")(command_id)
        params = _required_attribute(current_command, "params", "robot-server current command")
        message = getattr(params, "message", None)
        command_status = _required_attribute(
            current_command,
            "status",
            "robot-server current command",
        )
        command_status_value = str(getattr(command_status, "value", command_status))
        command_type = _required_attribute(
            current_command,
            "commandType",
            "robot-server current command",
        )
        if (
            command_type == "waitForResume"
            and command_status_value == "running"
            and isinstance(message, str)
            and message.startswith(checkpoint_prefix)
        ):
            checkpoint_id = _required_attribute(
                current_command,
                "id",
                "robot-server current command",
            )
    started = _required_method(store, "run_was_started", "robot-server run store")()

    return ProtocolRunState(
        run_id=run_id,
        status=status_value,
        started=bool(started),
        terminal=status_value in {"succeeded", "failed", "stopped"},
        mutation_checkpoint_id=checkpoint_id,
        protocol_less=protocol_runner is None,
    )


def _required_attribute(value: object, name: str, owner: str) -> object:
    try:
        return getattr(value, name)
    except AttributeError as exc:
        message = f"{owner}.{name} is unavailable in the installed robot-server runtime."
        raise RuntimeError(message) from exc


def _required_callable(value: object, name: str, owner: str) -> object:
    candidate = _required_attribute(value, name, owner)
    if not callable(candidate):
        message = f"{owner}.{name} must be callable in the installed robot-server runtime."
        raise RuntimeError(message)
    return candidate


def _required_method(value: object, name: str, owner: str) -> object:
    return _required_callable(value, name, owner)


__all__ = [
    "RobotServerAppInstallation",
    "RobotServerBindings",
    "current_run_store",
    "include_robot_server_router",
    "install_robot_server_app",
    "load_robot_server_bindings",
    "protocol_run_state",
    "restore_robot_server_app",
]
