import asyncio
import collections.abc
import contextlib
import dataclasses
import logging
import os
import typing
from importlib.metadata import version

from unitelabs.cdk import Connector, ConnectorBaseConfig, SiLAServerConfig

from .features import (
    AbsorbanceReaderFeature,
    CalibrationFeature,
    FlexStackerFeature,
    FlexStackerMaintenanceFeature,
    GripperFeature,
    HeaterShakerFeature,
    LabwareMovementController,
    LiquidHandlingController,
    MotionControlFeature,
    PipetteFeature,
    TemperatureModuleFeature,
    ThermocyclerFeature,
    TipController,
)
from .io import (
    AbsorbanceReaderController,
    FlexCalibrationController,
    FlexGripperController,
    FlexLabwareMovementController,
    FlexLiquidHandlingController,
    FlexMotionController,
    FlexStackerController,
    HardwareProxy,
    HeaterShakerController,
    LoadedLabwareMovementConfig,
    LabwareMovementState,
    TemperatureModuleController,
    ThermocyclerController,
    load_labware_movement_config,
)
from .io.run_authority import (
    ProtocolRunAuthority,
    ProtocolRunState,
    RunAwareLock,
    RunMutationGate,
    RunMutationHttpGuard,
)
from .io.simulator_compat import OT3SimulatorCompatibilityAdapter
from .run_mutation import MUTATION_CHECKPOINT_PREFIX, MutationLedger, RunMutationCoordinator

log = logging.getLogger(__name__)

__version__ = version("unitelabs-opentrons-flex")
_MINIMUM_MUTATION_TOKEN_LENGTH = 32
_SUPPORTED_MUTATION_OPENTRONS_VERSIONS = frozenset({"8.8.1"})


class _AppWithState(typing.Protocol):
    state: object


@dataclasses.dataclass
class OpentronsFlexConfig(ConnectorBaseConfig):
    """Configuration for the Opentrons Flex connector."""

    use_simulator: bool = True
    """Whether to use the OT3 simulator backend instead of real Flex (CAN) hardware."""

    with_robot_server: bool = False
    """Run the opentrons HTTP robot-server in the same process, sharing one OT3API.

    When True, the connector builds an ``OT3API`` (real or simulated depending on
    ``use_simulator``), wraps it in ``HardwareProxy``, and starts the robot-server
    FastAPI app alongside the SiLA2 gRPC server. Both share a single ``asyncio.Lock``
    so CAN commands cannot interleave. Requires the opentrons robot_server package.
    """

    lock_timeout_s: float | None = None
    """Seconds to wait to acquire the shared hardware lock before raising a TimeoutError.

    None (the default) means wait indefinitely — the gRPC client deadline governs instead.
    """

    robot_server_uds: str = "/run/aiohttp.sock"
    """Unix domain socket for the opentrons HTTP API when with_robot_server=True.

    On the Flex, nginx proxies external port 31950 to this socket. Ignored when
    robot_server_tcp_port is set.
    """

    robot_server_tcp_port: int | None = None
    """TCP port for the opentrons HTTP API when with_robot_server=True.

    When set, uvicorn binds 127.0.0.1 on this port instead of robot_server_uds.
    Useful for simulator/testing environments.
    """

    sila_server: SiLAServerConfig = dataclasses.field(
        default_factory=lambda: SiLAServerConfig(
            name="Opentrons Flex",
            type="LiquidHandler",
            description=(
                "SiLA2 connector for liquid handling, tip and labware movement, modules, and calibration "
                "on an Opentrons Flex"
            ),
            version=str(__version__),
            vendor_url="https://opentrons.com/",
        )
    )

    simulated_heater_shaker: bool = False
    """Attach one simulated Heater-Shaker when ``use_simulator`` is enabled.

    This is explicit and opt-in so a bare simulator keeps matching an unconfigured
    Flex. It is rejected in live-hardware mode rather than silently substituting a
    simulated module for a missing physical device. Declaring this after the existing
    fields preserves the positional configuration signature for current callers.
    """

    labware_movement_config: str | None = None
    """Local JSON file containing allowlisted gripper plans and initial deck occupancy.

    Raw grip points, geometry-check tolerances, and occupancy are intentionally
    not accepted from remote SiLA calls. Provision them locally and restart the
    connector so the server owns and updates the movement state.
    """

    simulated_flex_stacker: bool = False
    """Attach one simulated Flex Stacker when ``use_simulator`` is enabled."""

    simulated_absorbance_reader: bool = False
    """Attach one simulated Absorbance Plate Reader when ``use_simulator`` is enabled."""

    simulated_temperature_module: bool = False
    """Attach one simulated Temperature Module GEN2 when ``use_simulator`` is enabled."""

    simulated_thermocycler: bool = False
    """Attach one simulated Thermocycler GEN2 when ``use_simulator`` is enabled."""

    simulated_gripper: bool = False
    """Attach a simulated Flex Gripper when ``use_simulator`` is enabled."""

    run_mutation_ledger_path: str | None = None
    """Durable JSONL audit ledger enabling controlled Protocol Engine run mutation.

    This must be a persistent local path when ``with_robot_server`` is enabled.
    When omitted, run ownership protection remains active but mutation endpoints
    are not registered because unaudited mid-run changes are never allowed.
    """

    run_mutation_token_env: str = "UNITELABS_RUN_MUTATION_TOKEN"
    """Environment variable containing the controlled-mutation bearer token.

    The token itself is never accepted in a checked-in JSON configuration.
    """

    run_mutation_actor_env: str = "UNITELABS_RUN_MUTATION_ACTOR"
    """Environment variable binding the mutation credential to one operator identity.

    Mutation requests must claim this exact actor, and durable audit records use
    the environment-bound value rather than trusting caller-supplied identity.
    """


# Module type -> (IO controller class, SiLA feature class). The Magnetic Module is
# intentionally absent — the Flex does not support it.
def _module_factories() -> dict:
    from opentrons.hardware_control.modules.types import ModuleType

    return {
        ModuleType.ABSORBANCE_READER: (AbsorbanceReaderController, (AbsorbanceReaderFeature,)),
        ModuleType.FLEX_STACKER: (
            FlexStackerController,
            (FlexStackerFeature, FlexStackerMaintenanceFeature),
        ),
        ModuleType.HEATER_SHAKER: (HeaterShakerController, (HeaterShakerFeature,)),
        ModuleType.THERMOCYCLER: (ThermocyclerController, (ThermocyclerFeature,)),
        ModuleType.TEMPERATURE: (TemperatureModuleController, (TemperatureModuleFeature,)),
    }


def _simulator_attached_modules(config: OpentronsFlexConfig) -> dict:
    """Build the explicit module inventory passed to the OT3 simulator."""
    from opentrons.hardware_control.modules.types import SimulatingModule

    attached_modules: dict[str, list[SimulatingModule]] = {}
    if config.simulated_heater_shaker:
        attached_modules["heatershaker"] = [
            SimulatingModule(
                serial_number="HS-SIM-1",
                model="heaterShakerModuleV1",
            )
        ]
    if config.simulated_flex_stacker:
        attached_modules["flexstacker"] = [
            SimulatingModule(
                serial_number="FS-SIM-1",
                model="flexStackerModuleV1",
            )
        ]
    if config.simulated_absorbance_reader:
        attached_modules["absorbancereader"] = [
            SimulatingModule(
                serial_number="AR-SIM-1",
                model="absorbanceReaderV1",
            )
        ]
    if config.simulated_temperature_module:
        attached_modules["tempdeck"] = [
            SimulatingModule(
                serial_number="TM-SIM-1",
                model="temperatureModuleV2",
            )
        ]
    if config.simulated_thermocycler:
        attached_modules["thermocycler"] = [
            SimulatingModule(
                serial_number="TC-SIM-1",
                model="thermocyclerModuleV2",
            )
        ]
    return attached_modules


def _simulator_attached_instruments(config: OpentronsFlexConfig) -> dict:
    """Build the explicit instrument inventory passed to the OT3 simulator."""
    from opentrons.hardware_control.types import OT3Mount

    if not config.simulated_gripper:
        return {}
    return {
        OT3Mount.GRIPPER: {
            "model": "gripperV1",
            "id": "GRIPPER-SIM-1",
        }
    }


def _register_core_features(
    connector: Connector,
    motion: FlexMotionController,
    gripper: FlexGripperController,
    calibration: FlexCalibrationController,
    labware_config: LoadedLabwareMovementConfig,
    labware_state: LabwareMovementState | None,
) -> None:
    connector.register(MotionControlFeature(motion))
    connector.register(LiquidHandlingController(FlexLiquidHandlingController(motion)))
    connector.register(
        LabwareMovementController(
            FlexLabwareMovementController(
                motion,
                gripper,
                plans=labware_config.plans,
                state=labware_state,
            )
        )
    )
    connector.register(PipetteFeature(motion))
    connector.register(TipController(motion))
    connector.register(GripperFeature(gripper))
    connector.register(CalibrationFeature(calibration))


def _register_modules(
    connector: Connector,
    attached_modules: collections.abc.Iterable,
    shared_lock: asyncio.Lock | RunAwareLock,
) -> None:
    factories = _module_factories()
    for module in attached_modules:
        factory = factories.get(module.MODULE_TYPE)
        if factory is None:
            log.info("Skipping unsupported module type %s", module.MODULE_TYPE.name)
            continue
        controller_cls, feature_classes = factory
        controller = controller_cls.from_module(module, lock=shared_lock)
        for feature_cls in feature_classes:
            connector.register(feature_cls(controller))
            log.info("Registered SiLA feature %s for module %s", feature_cls.__name__, module.MODULE_TYPE.name)


def _shared_hardware_robot_server_lifespan(
    original_lifespan: collections.abc.Callable[[_AppWithState], typing.AsyncContextManager[None]],
    proxy: HardwareProxy,
) -> collections.abc.Callable[[_AppWithState], typing.AsyncContextManager[None]]:
    """
    Add robot-server post-init callbacks skipped by shared hardware injection.

    The native lifespan sees the completed hardware initialization task installed
    by the connector, so it deliberately does not run its hardware callbacks.
    Protocol Engine routes still require the light controller created by those
    callbacks. Run only that public robot-server initialization pair after the
    native lifespan has established its task runner and persistence services.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: _AppWithState) -> collections.abc.AsyncGenerator[None, None]:
        from robot_server.runs.dependencies import (  # type: ignore[import]
            mark_light_control_startup_finished,
            start_light_control_task,
        )

        async with original_lifespan(app):
            await start_light_control_task(app.state, proxy)
            await mark_light_control_startup_finished(app.state, proxy)
            yield

    return lifespan


async def create_app(config: OpentronsFlexConfig) -> collections.abc.AsyncGenerator[Connector, None]:
    """
    Create the connector application.

    Builds a single ``OT3API`` (simulator or real Flex hardware), shares it across the
    motion/pipette/gripper/calibration controllers via one ``asyncio.Lock``, and registers
    the corresponding SiLA features. When ``config.with_robot_server`` is True the opentrons
    HTTP robot-server is started in the same process sharing that one API (see
    ``_create_app_with_robot_server``).
    """
    log.info(
        "Starting Opentrons Flex connector v%s (simulate=%s, robot_server=%s)",
        __version__,
        config.use_simulator,
        config.with_robot_server,
    )

    simulated_modules = [
        name
        for enabled, name in (
            (config.simulated_heater_shaker, "simulated_heater_shaker"),
            (config.simulated_flex_stacker, "simulated_flex_stacker"),
            (config.simulated_absorbance_reader, "simulated_absorbance_reader"),
            (config.simulated_temperature_module, "simulated_temperature_module"),
            (config.simulated_thermocycler, "simulated_thermocycler"),
            (config.simulated_gripper, "simulated_gripper"),
        )
        if enabled
    ]
    if simulated_modules and not config.use_simulator:
        settings = ", ".join(simulated_modules)
        message = (
            f"Simulation settings {settings} require use_simulator=true. "
            "For a real Flex, connect and power the physical modules before starting the connector."
        )
        raise ValueError(message)

    labware_config = (
        load_labware_movement_config(config.labware_movement_config)
        if config.labware_movement_config is not None
        else LoadedLabwareMovementConfig(plans=(), initial_occupancy={}, state_file=None)
    )

    if config.with_robot_server:
        async for connector in _create_app_with_robot_server(config, labware_config):
            yield connector
        return

    from opentrons.hardware_control.ot3api import OT3API

    if config.use_simulator:
        log.info("Building OT3API simulator backend")
        api = await OT3API.build_hardware_simulator(
            attached_instruments=_simulator_attached_instruments(config),
            attached_modules=_simulator_attached_modules(config),
        )
    else:
        log.info("Building OT3API for real Flex hardware (CAN)")
        api = await OT3API.build_hardware_controller()

    shared_lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
    gripper = FlexGripperController.from_api(api, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
    calibration = FlexCalibrationController.from_api(api, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
    labware_state = (
        LabwareMovementState(labware_config.state_file, labware_config.initial_occupancy)
        if labware_config.state_file is not None
        else None
    )

    try:
        app = Connector(config)
        _register_core_features(app, motion, gripper, calibration, labware_config, labware_state)
        _register_modules(app, api.attached_modules, shared_lock)

        log.info("SiLA server listening on %s:%d", config.sila_server.hostname, config.sila_server.port)

        yield app
    finally:
        try:
            if labware_state is not None:
                labware_state.close()
        finally:
            await api.clean_up()


async def _create_app_with_robot_server(
    config: OpentronsFlexConfig,
    labware_config: LoadedLabwareMovementConfig,
) -> collections.abc.AsyncGenerator[Connector, None]:
    """
    Start both the SiLA2 gRPC server and the opentrons HTTP robot-server in one process.

    Mirrors the OT-2 connector's in-process design, but builds an ``OT3API`` (CAN) instead
    of an OT-2 ``API`` (Smoothie serial):

    1. ``OT3API.build_hardware_controller`` brings up the CAN bus once.
    2. ``HardwareProxy`` wraps it with an ``asyncio.Lock`` — every command from either
       server acquires this lock.
    3. App-state pre-population sets a completed init task + our proxy on
       ``robot_server_app.state`` so robot-server skips its own hardware init.
    4. uvicorn serves ``robot_server_app`` on a Unix domain socket (nginx proxies TCP 31950)
       or a TCP port for testing.

    ``robot_server`` is a system package on the Flex (not on PyPI); its imports are deferred.
    """
    import uvicorn
    from opentrons.hardware_control.ot3api import OT3API

    from opentrons.protocol_engine import DeckType
    from opentrons_shared_data.robot.types import RobotTypeEnum

    from robot_server.hardware import (  # type: ignore[import]
        _hw_api_accessor,
        _init_task_accessor,
        get_deck_type,
        get_robot_type,
        get_robot_type_enum,
    )
    from robot_server.app import app as robot_server_app  # type: ignore[import]

    mutation_api_token: str | None = None
    mutation_authenticated_actor: str | None = None
    mutation_ledger: MutationLedger | None = None
    if config.run_mutation_ledger_path is not None:
        mutation_api_token = os.environ.get(config.run_mutation_token_env)
        if mutation_api_token is None or len(mutation_api_token) < _MINIMUM_MUTATION_TOKEN_LENGTH:
            message = (
                "Controlled run mutation requires environment variable "
                f"{config.run_mutation_token_env!r} to contain at least "
                f"{_MINIMUM_MUTATION_TOKEN_LENGTH} random characters."
            )
            raise RuntimeError(message)
        mutation_authenticated_actor = os.environ.get(config.run_mutation_actor_env)
        if (
            mutation_authenticated_actor is None
            or not mutation_authenticated_actor.strip()
            or len(mutation_authenticated_actor) > 200
        ):
            message = (
                "Controlled run mutation requires environment variable "
                f"{config.run_mutation_actor_env!r} to contain a non-empty operator identity "
                "of at most 200 characters."
            )
            raise RuntimeError(message)
        mutation_authenticated_actor = mutation_authenticated_actor.strip()
        opentrons_version = version("opentrons")
        if opentrons_version not in _SUPPORTED_MUTATION_OPENTRONS_VERSIONS:
            supported = ", ".join(sorted(_SUPPORTED_MUTATION_OPENTRONS_VERSIONS))
            message = (
                f"Controlled run mutation is not validated for Opentrons {opentrons_version}; "
                f"supported runtime version: {supported}."
            )
            raise RuntimeError(message)
        # Verify durable state before acquiring OT3API or mutating the global
        # robot-server app. A corrupt ledger must fail without leaking either.
        mutation_ledger = MutationLedger(config.run_mutation_ledger_path)

    if config.use_simulator:
        log.info("Building shared OT3API (simulator)")
        shared_hardware = await OT3API.build_hardware_simulator(
            attached_instruments=_simulator_attached_instruments(config),
            attached_modules=_simulator_attached_modules(config),
        )
    else:
        log.info("Building shared OT3API on CAN bus")
        shared_hardware = await OT3API.build_hardware_controller()

    def _current_run_store() -> object | None:
        # robot-server stores this singleton under the stable app-state key used
        # by its AppStateAccessor. Reading the state directly keeps the shared
        # hardware wiring testable without importing robot-server's private runs
        # package on development hosts.
        return getattr(robot_server_app.state, "run_orchestrator_store", None)

    robot_server_ready = False

    def _current_run_state() -> ProtocolRunState:
        store = _current_run_store()
        if store is None:
            if not robot_server_ready:
                return ProtocolRunState(
                    run_id="robot-server-startup",
                    status="initializing",
                    started=False,
                    terminal=False,
                )
            return ProtocolRunState(run_id=None, status="idle", started=False, terminal=True)
        run_id = store.current_run_id
        if run_id is None:
            return ProtocolRunState(run_id=None, status="idle", started=False, terminal=True)
        status = store.get_status()
        status_value = str(getattr(status, "value", status))
        missing_protocol_runner = object()
        protocol_runner = getattr(store.run_orchestrator, "_protocol_runner", missing_protocol_runner)
        checkpoint_id = None
        current_pointer = store.get_current_command()
        if current_pointer is not None:
            current_command = store.get_command(current_pointer.command_id)
            message = getattr(current_command.params, "message", None)
            command_status = str(getattr(current_command.status, "value", current_command.status))
            if (
                current_command.commandType == "waitForResume"
                and command_status == "running"
                and isinstance(message, str)
                and message.startswith(MUTATION_CHECKPOINT_PREFIX)
            ):
                checkpoint_id = current_command.id
        return ProtocolRunState(
            run_id=run_id,
            status=status_value,
            started=bool(store.run_was_started()),
            # PE marks its logical run result before halt/reset/home cleanup.
            # Retain exclusive ownership throughout the observable FINISHING or
            # STOP_REQUESTED phase until hardware cleanup has completed.
            terminal=status_value in {"succeeded", "failed", "stopped"},
            mutation_checkpoint_id=checkpoint_id,
            # In the version-pinned RunOrchestrator, only runs created without
            # a protocol have no Python/JSON protocol runner. Unknown layouts
            # fail closed as not protocol-less.
            protocol_less=protocol_runner is None,
        )

    run_authority = ProtocolRunAuthority(_current_run_state)
    mutation_gate = RunMutationGate()
    shared_lock = RunAwareLock(run_authority)
    labware_state = (
        LabwareMovementState(labware_config.state_file, labware_config.initial_occupancy)
        if labware_config.state_file is not None
        else None
    )
    robot_server_hardware = (
        OT3SimulatorCompatibilityAdapter(shared_hardware) if config.use_simulator else shared_hardware
    )
    proxy = HardwareProxy(
        robot_server_hardware,
        lock=shared_lock,
        lock_timeout_s=config.lock_timeout_s,
        labware_state=labware_state,
    )
    motion = FlexMotionController.from_api(shared_hardware, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
    gripper = FlexGripperController.from_api(shared_hardware, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
    calibration = FlexCalibrationController.from_api(
        shared_hardware, lock=shared_lock, lock_timeout_s=config.lock_timeout_s
    )

    # Pre-populate robot_server app state so it does not initialise hardware a second time.
    async def _noop() -> None:
        pass

    init_task: asyncio.Task[None] = asyncio.create_task(_noop())
    await init_task
    _init_task_accessor.set_on(robot_server_app.state, init_task)
    _hw_api_accessor.set_on(robot_server_app.state, proxy)

    # robot-server normally derives these values from process-wide feature flags.
    # Those flags identify an off-robot development host as OT-2 even though this
    # connector has explicitly injected an OT3API. Keep every route aligned with
    # the shared Flex hardware, including protocol upload and run creation.
    async def _get_flex_robot_type() -> str:
        return "OT-3 Standard"

    async def _get_flex_robot_type_enum() -> RobotTypeEnum:
        return RobotTypeEnum.FLEX

    async def _get_flex_deck_type() -> DeckType:
        return DeckType.OT3_STANDARD

    dependency_overrides = robot_server_app.dependency_overrides
    flex_identity_overrides = {
        get_robot_type: _get_flex_robot_type,
        get_robot_type_enum: _get_flex_robot_type_enum,
        get_deck_type: _get_flex_deck_type,
    }
    missing_override = object()
    original_identity_overrides = {
        dependency: dependency_overrides.get(dependency, missing_override) for dependency in flex_identity_overrides
    }
    dependency_overrides.update(flex_identity_overrides)

    original_robot_server_lifespan = robot_server_app.router.lifespan_context
    robot_server_app.router.lifespan_context = _shared_hardware_robot_server_lifespan(
        original_robot_server_lifespan,
        proxy,
    )

    added_mutation_routes: tuple[object, ...] = ()
    mutation_coordinator: RunMutationCoordinator | None = None
    if mutation_ledger is not None:
        from .run_mutation_api import create_run_mutation_router

        mutation_coordinator = RunMutationCoordinator(
            store_provider=_current_run_store,
            ledger=mutation_ledger,
            gate=mutation_gate,
            authenticated_actor=mutation_authenticated_actor,
        )
        mutation_router = create_run_mutation_router(
            mutation_coordinator,
            api_token=typing.cast(str, mutation_api_token),
        )
        added_mutation_routes = tuple(mutation_router.routes)
        robot_server_app.include_router(mutation_router)
        robot_server_app.openapi_schema = None
        log.info("Controlled run mutation enabled with durable ledger %s", mutation_ledger.path)
    else:
        log.warning(
            "Controlled run mutation endpoints are disabled because run_mutation_ledger_path is not configured; "
            "Protocol Engine run ownership protection remains active."
        )

    guarded_robot_server_app = RunMutationHttpGuard(
        robot_server_app,
        run_authority,
        mutation_gate,
        mutation_api_token=mutation_api_token,
        checkpoint_resume_authorizer=(
            mutation_coordinator.authorize_checkpoint_resume if mutation_coordinator is not None else None
        ),
        recovery_resume_authorizer=(
            mutation_coordinator.authorize_recovery_resume if mutation_coordinator is not None else None
        ),
        prestart_setup_authorizer=(
            mutation_coordinator.authorize_prestart_setup if mutation_coordinator is not None else None
        ),
    )

    if config.robot_server_tcp_port is not None:
        uv_config = uvicorn.Config(
            guarded_robot_server_app,
            host="127.0.0.1",
            port=config.robot_server_tcp_port,
            ws="wsproto",
            loop="none",
            log_level="info",
        )
        log.info("robot-server starting on 127.0.0.1:%d", config.robot_server_tcp_port)
    else:
        uv_config = uvicorn.Config(
            guarded_robot_server_app,
            uds=config.robot_server_uds,
            ws="wsproto",
            loop="none",
            log_level="info",
        )
        log.info("robot-server starting on %s", config.robot_server_uds)

    uv_server = uvicorn.Server(uv_config)
    robot_server_task = asyncio.create_task(uv_server.serve())

    try:
        while not bool(getattr(uv_server, "started", False)):
            if robot_server_task.done():
                failure = robot_server_task.exception()
                if failure is not None:
                    raise failure
                message = "Embedded robot-server stopped before completing startup."
                raise RuntimeError(message)
            await asyncio.sleep(0.01)
        robot_server_ready = True

        connector = Connector(config)
        _register_core_features(connector, motion, gripper, calibration, labware_config, labware_state)
        _register_modules(connector, shared_hardware.attached_modules, shared_lock)

        log.info("SiLA server listening on %s:%d", config.sila_server.hostname, config.sila_server.port)

        yield connector
    finally:
        uv_server.should_exit = True
        await asyncio.gather(robot_server_task, return_exceptions=True)
        robot_server_app.router.lifespan_context = original_robot_server_lifespan
        if added_mutation_routes:
            robot_server_app.router.routes[:] = [
                route for route in robot_server_app.router.routes if route not in added_mutation_routes
            ]
            robot_server_app.openapi_schema = None
        for dependency, original_override in original_identity_overrides.items():
            if original_override is missing_override:
                dependency_overrides.pop(dependency, None)
            else:
                dependency_overrides[dependency] = original_override
        try:
            if labware_state is not None:
                labware_state.close()
        finally:
            await shared_hardware.clean_up()
