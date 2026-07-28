from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from unitelabs.opentrons_flex.robot_server_compat import (
    RobotServerBindings,
    current_run_store,
    include_robot_server_router,
    install_robot_server_app,
    protocol_run_state,
    restore_robot_server_app,
)

_CHECKPOINT_PREFIX = "UNITELABS_MUTATION_CHECKPOINT:"


def test_startup_without_store_retains_protocol_engine_ownership() -> None:
    state = protocol_run_state(
        None,
        robot_server_ready=False,
        checkpoint_prefix=_CHECKPOINT_PREFIX,
    )

    assert state.status == "initializing"
    assert state.owns_hardware is True


def test_ready_server_without_store_is_idle() -> None:
    state = protocol_run_state(
        None,
        robot_server_ready=True,
        checkpoint_prefix=_CHECKPOINT_PREFIX,
    )

    assert state.status == "idle"
    assert state.owns_hardware is False


def test_missing_run_store_after_startup_fails_closed() -> None:
    bindings = RobotServerBindings(
        app=SimpleNamespace(state=SimpleNamespace()),
        hardware_accessor=MagicMock(),
        initialization_task_accessor=MagicMock(),
        get_deck_type=MagicMock(),
        get_robot_type=MagicMock(),
        get_robot_type_enum=MagicMock(),
        mark_light_control_startup_finished=MagicMock(),
        start_light_control_task=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="run_orchestrator_store"):
        current_run_store(bindings, robot_server_ready=True)


def test_active_checkpoint_is_read_from_validated_store_shape() -> None:
    command = SimpleNamespace(
        id="command-1",
        commandType="waitForResume",
        status=SimpleNamespace(value="running"),
        params=SimpleNamespace(message=f"{_CHECKPOINT_PREFIX}before-transfer"),
    )
    store = SimpleNamespace(
        current_run_id="run-1",
        run_orchestrator=SimpleNamespace(_protocol_runner=object()),
        get_status=lambda: SimpleNamespace(value="paused"),
        get_current_command=lambda: SimpleNamespace(command_id="command-1"),
        get_command=lambda command_id: command if command_id == "command-1" else None,
        run_was_started=lambda: True,
    )

    state = protocol_run_state(
        store,
        robot_server_ready=True,
        checkpoint_prefix=_CHECKPOINT_PREFIX,
    )

    assert state.run_id == "run-1"
    assert state.status == "paused"
    assert state.mutation_checkpoint_id == "command-1"
    assert state.protocol_less is False
    assert state.owns_hardware is True


def test_private_run_store_drift_fails_closed() -> None:
    store = SimpleNamespace(
        current_run_id="run-1",
        run_orchestrator=SimpleNamespace(),
        get_status=lambda: SimpleNamespace(value="running"),
    )

    with pytest.raises(RuntimeError, match=r"_protocol_runner.*unavailable"):
        protocol_run_state(
            store,
            robot_server_ready=True,
            checkpoint_prefix=_CHECKPOINT_PREFIX,
        )


def test_app_installation_and_router_registration_are_reversible() -> None:
    original_lifespan = MagicMock()
    original_dependency = object()
    original_override = object()
    app_routes = [object()]
    app = SimpleNamespace(
        state=SimpleNamespace(),
        dependency_overrides={original_dependency: original_override},
        router=SimpleNamespace(routes=app_routes, lifespan_context=original_lifespan),
        openapi_schema=object(),
    )

    def include_router(router) -> None:
        app_routes.extend(object() for _ in router.routes)

    app.include_router = include_router
    original_hardware = object()
    original_initialization_task = object()
    hardware_accessor = SimpleNamespace(
        get_from=MagicMock(return_value=original_hardware),
        set_on=MagicMock(),
    )
    initialization_task_accessor = SimpleNamespace(
        get_from=MagicMock(return_value=original_initialization_task),
        set_on=MagicMock(),
    )
    installed_hardware = object()
    installed_initialization_task = object()
    bindings = RobotServerBindings(
        app=app,
        hardware_accessor=hardware_accessor,
        initialization_task_accessor=initialization_task_accessor,
        get_deck_type=MagicMock(),
        get_robot_type=MagicMock(),
        get_robot_type_enum=MagicMock(),
        mark_light_control_startup_finished=MagicMock(),
        start_light_control_task=MagicMock(),
    )
    added_dependency = object()
    added_override = object()
    replacement_lifespan = MagicMock()

    installation = install_robot_server_app(
        bindings,
        initialization_task=installed_initialization_task,
        hardware_proxy=installed_hardware,
        identity_overrides={
            original_dependency: object(),
            added_dependency: added_override,
        },
        lifespan_factory=lambda _: replacement_lifespan,
    )
    baseline_route = app_routes[0]
    added_routes = include_robot_server_router(
        installation,
        SimpleNamespace(routes=[object(), object()]),
    )

    assert len(added_routes) == 2
    assert app.router.lifespan_context is replacement_lifespan
    assert app.dependency_overrides[added_dependency] is added_override

    restore_robot_server_app(installation, added_routes=added_routes)

    assert app.router.routes == [baseline_route]
    assert app.router.lifespan_context is original_lifespan
    assert app.dependency_overrides == {original_dependency: original_override}
    assert hardware_accessor.set_on.call_args_list == [
        call(app.state, installed_hardware),
        call(app.state, original_hardware),
    ]
    assert initialization_task_accessor.set_on.call_args_list == [
        call(app.state, installed_initialization_task),
        call(app.state, original_initialization_task),
    ]


def test_router_registration_that_adds_no_routes_fails_closed() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(),
        dependency_overrides={},
        router=SimpleNamespace(routes=[], lifespan_context=MagicMock()),
        openapi_schema=None,
        include_router=lambda _: None,
    )
    bindings = RobotServerBindings(
        app=app,
        hardware_accessor=SimpleNamespace(
            get_from=MagicMock(return_value=None),
            set_on=MagicMock(),
        ),
        initialization_task_accessor=SimpleNamespace(
            get_from=MagicMock(return_value=None),
            set_on=MagicMock(),
        ),
        get_deck_type=MagicMock(),
        get_robot_type=MagicMock(),
        get_robot_type_enum=MagicMock(),
        mark_light_control_startup_finished=MagicMock(),
        start_light_control_task=MagicMock(),
    )
    installation = install_robot_server_app(
        bindings,
        initialization_task=object(),
        hardware_proxy=object(),
        identity_overrides={},
        lifespan_factory=lambda original: original,
    )

    with pytest.raises(RuntimeError, match="did not register"):
        include_robot_server_router(
            installation,
            SimpleNamespace(routes=[object()]),
        )


def test_partial_app_installation_is_rolled_back_transactionally() -> None:
    original_hardware = object()
    original_initialization_task = object()
    installed_hardware = object()
    installed_initialization_task = object()
    state = SimpleNamespace(
        hardware=original_hardware,
        initialization_task=original_initialization_task,
    )

    class _Accessor:
        def __init__(self, attribute: str, fail_value: object | None = None) -> None:
            self._attribute = attribute
            self._fail_value = fail_value

        def get_from(self, app_state) -> object:
            return getattr(app_state, self._attribute)

        def set_on(self, app_state, value: object) -> None:
            setattr(app_state, self._attribute, value)
            if value is self._fail_value:
                raise RuntimeError("setter failed")

    app = SimpleNamespace(
        state=state,
        dependency_overrides={},
        router=SimpleNamespace(routes=[], lifespan_context=MagicMock()),
        openapi_schema=None,
    )
    app.include_router = MagicMock()
    bindings = RobotServerBindings(
        app=app,
        hardware_accessor=_Accessor("hardware", fail_value=installed_hardware),
        initialization_task_accessor=_Accessor("initialization_task"),
        get_deck_type=MagicMock(),
        get_robot_type=MagicMock(),
        get_robot_type_enum=MagicMock(),
        mark_light_control_startup_finished=MagicMock(),
        start_light_control_task=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="setter failed"):
        install_robot_server_app(
            bindings,
            initialization_task=installed_initialization_task,
            hardware_proxy=installed_hardware,
            identity_overrides={},
            lifespan_factory=lambda original: original,
        )

    assert state.hardware is original_hardware
    assert state.initialization_task is original_initialization_task
