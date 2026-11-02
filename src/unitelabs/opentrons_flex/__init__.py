import asyncio
import collections.abc
import contextlib
import dataclasses
import logging
import os
import signal
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
    RobotStatusProvider,
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
from .features._progress import observable_commands_in_flight
from .io._module_base import module_activity
from .io.recovery_state import recovery_state_for
from .io.events import EVENTS, EventKind
from .io.hot_plug import ModuleHotPlugMonitor
from .io.recovery import AUTO_RECOVERY_MODES, AutoRecovery, RecoveryCoordinator, StackerRecoveryTarget
from .features.recovery_controller import RecoveryController
from .runtime_environment import (
    PERSISTENCE_ENV,
    LifecycleRecord,
    robot_environment_detected,
    apply_robot_server_persistence,
    quiet_noisy_loggers,
    remove_stale_robot_server_tempdirs,
    require_robot_environment,
    systemd_service,
)
from .io.robot_status import HttpServerState, RobotState, RobotStatusSource
from .io.run_authority import (
    ProtocolRunAuthority,
    ProtocolRunState,
    RunAwareLock,
    RunMutationGate,
    RunMutationHttpGuard,
)
from .io.simulator_compat import OT3SimulatorCompatibilityAdapter
from .run_mutation import MUTATION_CHECKPOINT_PREFIX, MutationLedger, MutationLedgerError, RunMutationCoordinator
from .runtime_identity_api import create_runtime_identity_router, process_runtime_identity
from .robot_server_compat import (
    current_run_store,
    initialize_run_store,
    include_robot_server_router,
    install_robot_server_app,
    load_robot_server_bindings,
    protocol_run_state,
    restore_robot_server_app,
)
from .runtime_compat import (
    RuntimeCompatibilityReport,
    mutation_configuration_issues,
    require_compatible_runtime,
)

log = logging.getLogger(__name__)

__version__ = version("unitelabs-opentrons-flex")


def _sila_server_version(package_version: str) -> str:
    """
    Return the package version in SiLA 2 server form, ``major.minor.patch_label``.

    A PEP 440 local version such as ``0.9.1+asms.1`` becomes ``0.9.1_asms_1``.
    """
    public, _, local = package_version.partition("+")
    return f"{public}_{local.replace('.', '_')}" if local else public


SILA_SERVER_VERSION = _sila_server_version(__version__)


class _AppWithState(typing.Protocol):
    state: object


# Defaults applied to any ``sila_server`` field that a configuration file leaves
# at the generic SiLA library default. Configuration files therefore never need
# to repeat the connector version, which always comes from the installed package.
_LIBRARY_SILA_DEFAULTS = SiLAServerConfig()
_SILA_SERVER_DEFAULTS = {
    "name": (_LIBRARY_SILA_DEFAULTS.name, "Opentrons Flex"),
    "type": (_LIBRARY_SILA_DEFAULTS.type, "LiquidHandler"),
    "description": (
        _LIBRARY_SILA_DEFAULTS.description,
        "SiLA2 connector for liquid handling, tip and labware movement, modules, and calibration on an Opentrons Flex",
    ),
    "version": (_LIBRARY_SILA_DEFAULTS.version, SILA_SERVER_VERSION),
    "vendor_url": (_LIBRARY_SILA_DEFAULTS.vendor_url, "https://opentrons.com/"),
}

SIMULATED_MODULE_NAMES = (
    "heater_shaker",
    "flex_stacker",
    "absorbance_reader",
    "temperature_module",
    "thermocycler",
    "gripper",
)
"""Values accepted by ``OpentronsFlexConfig.simulated_modules``."""


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
            version=SILA_SERVER_VERSION,
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

    run_mutation_required: bool = False
    """Fail connector startup when controlled run mutation is not ready.

    The default keeps the base SiLA and HTTP interfaces available if only the
    optional mutation extension is misconfigured. Set this to ``True`` for the
    full workflow so an operator cannot accidentally start without checkpoints.
    """

    simulated_modules: list[str] = dataclasses.field(default_factory=list)
    """Simulated accessories to attach when ``use_simulator`` is enabled.

    Any of ``heater_shaker``, ``flex_stacker``, ``absorbance_reader``,
    ``temperature_module``, ``thermocycler`` and ``gripper``. This list replaces
    the individual ``simulated_*`` flags, which remain accepted for existing files.
    """

    recovery_state_path: str | None = None
    """Persist recovery gates (tip quarantine, jaw home) so a restart cannot clear them.

    Use a durable local path on the robot. When omitted, gates live only in memory.
    """

    robot_server_startup_timeout_s: float = 120.0
    """Seconds to wait for the embedded robot-server to finish starting before failing startup."""

    module_hot_plug: str = "auto"
    """What to do when a module type that had no feature at startup is plugged in.

    ``restart`` restarts the connector once the robot is idle so the module gets
    its SiLA feature; ``report`` only reports it in RobotStatusProvider; ``auto``
    (default) restarts on real hardware and reports in the simulator. Re-plugging a
    module whose type is already registered never needs a restart.
    """

    require_robot_environment: bool = True
    """Refuse to drive real hardware unless the Opentrons robot environment is set.

    Without ``RUNNING_ON_VERDIN`` (copied from the stock robot-server unit by
    ``install_connector_service.sh``) Opentrons reads calibration from
    ``~/.opentrons`` instead of ``/data``. Disable only for a bench test.
    """

    robot_server_persistence_directory: str | None = None
    """Data directory for the embedded robot-server (protocols, runs, offsets, deck configuration).

    When omitted the inherited ``OT_ROBOT_SERVER_persistence_directory`` is used,
    which the service copies from the stock robot-server unit.
    """

    robot_server_access_log: bool = False
    """Log every HTTP request of the embedded robot-server (off: the touchscreen polls continuously)."""

    lifecycle_state_path: str | None = None
    """Small file recording how the previous connector process ended, so a restart is explained in Status."""

    auto_recovery: str = "safe"
    """Automatic recovery while the robot is idle and recovery-gated.

    ``safe`` (default) only performs steps that never move the robot (tip state
    settled by the sensor, jaw gate without a gripper). ``full`` also runs the
    complete Recover sequence, which homes the robot, 30 s after a gate is set
    when the E-stop is released and the door is closed. ``off`` disables it.
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sila_server is not None:
            for field_name, (library_default, flex_default) in _SILA_SERVER_DEFAULTS.items():
                if getattr(self.sila_server, field_name, None) in (None, library_default):
                    setattr(self.sila_server, field_name, flex_default)
        unknown = sorted(set(self.simulated_modules) - set(SIMULATED_MODULE_NAMES))
        if unknown:
            message = f"Unknown simulated_modules entries {unknown}; choose from {', '.join(SIMULATED_MODULE_NAMES)}."
            raise ValueError(message)
        if self.auto_recovery not in AUTO_RECOVERY_MODES:
            message = f"auto_recovery must be one of {', '.join(AUTO_RECOVERY_MODES)}."
            raise ValueError(message)
        if self.module_hot_plug not in {"auto", "restart", "report"}:
            message = "module_hot_plug must be one of auto, restart, report."
            raise ValueError(message)
        if not self.robot_server_startup_timeout_s > 0:
            message = "robot_server_startup_timeout_s must be greater than 0."
            raise ValueError(message)

    def simulated(self, name: str) -> bool:
        """Return whether one simulated accessory is requested (list or legacy flag)."""
        return name in self.simulated_modules or bool(getattr(self, f"simulated_{name}", False))


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
    if config.simulated("heater_shaker"):
        attached_modules["heatershaker"] = [
            SimulatingModule(
                serial_number="HS-SIM-1",
                model="heaterShakerModuleV1",
            )
        ]
    if config.simulated("flex_stacker"):
        attached_modules["flexstacker"] = [
            SimulatingModule(
                serial_number="FS-SIM-1",
                model="flexStackerModuleV1",
            )
        ]
    if config.simulated("absorbance_reader"):
        attached_modules["absorbancereader"] = [
            SimulatingModule(
                serial_number="AR-SIM-1",
                model="absorbanceReaderV1",
            )
        ]
    if config.simulated("temperature_module"):
        attached_modules["tempdeck"] = [
            SimulatingModule(
                serial_number="TM-SIM-1",
                model="temperatureModuleV2",
            )
        ]
    if config.simulated("thermocycler"):
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

    if not config.simulated("gripper"):
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
                profile_sha256=labware_config.profile_sha256,
            )
        )
    )
    connector.register(PipetteFeature(motion))
    connector.register(TipController(motion))
    connector.register(GripperFeature(gripper))
    connector.register(CalibrationFeature(calibration))


class _RegisteredModule(typing.NamedTuple):
    module_type: str
    serial: str
    connected: collections.abc.Callable[[], bool]


def _module_serial(module: object) -> str:
    device_info = getattr(module, "device_info", {})
    serial = device_info.get("serial") if isinstance(device_info, collections.abc.Mapping) else None
    return str(serial or "unknown")


def _register_modules(
    connector: Connector,
    attached_modules: collections.abc.Iterable,
    shared_lock: asyncio.Lock | RunAwareLock,
    *,
    live_modules: collections.abc.Callable[[], collections.abc.Iterable[object]] | None = None,
    controllers_out: list[object] | None = None,
) -> list[_RegisteredModule]:
    """Register one SiLA feature set per supported attached module and return their identities."""
    modules = list(attached_modules)
    factories = _module_factories()
    modules_by_type: dict[object, list[object]] = {}
    for module in modules:
        if module.MODULE_TYPE in factories:
            modules_by_type.setdefault(module.MODULE_TYPE, []).append(module)
    duplicates = {module_type: group for module_type, group in modules_by_type.items() if len(group) > 1}
    if duplicates:
        details = [
            f"{module_type.name}: {', '.join(_module_serial(module) for module in group)}"
            for module_type, group in sorted(duplicates.items(), key=lambda item: item[0].name)
        ]
        duplicate_summary = "; ".join(details)
        message = (
            "Multiple attached modules of the same type cannot be represented by the current "
            "single-instance SiLA feature contract. Connector startup was refused to prevent "
            f"silent feature replacement. Duplicate modules: {duplicate_summary}. "
            "Disconnect the extra module or use a connector release with serial-number routing."
        )
        raise RuntimeError(message)

    registered: list[_RegisteredModule] = []
    for module in modules:
        factory = factories.get(module.MODULE_TYPE)
        if factory is None:
            log.info("Skipping unsupported module type %s", module.MODULE_TYPE.name)
            continue
        controller_cls, feature_classes = factory
        controller = controller_cls.from_module(module, lock=shared_lock, attached_modules=live_modules)
        if controllers_out is not None:
            controllers_out.append(controller)
        for feature_cls in feature_classes:
            connector.register(feature_cls(controller))
            log.info("Registered SiLA feature %s for module %s", feature_cls.__name__, module.MODULE_TYPE.name)
        registered.append(
            _RegisteredModule(
                module_type=module.MODULE_TYPE.name,
                serial=_module_serial(module),
                connected=lambda controller=controller: controller.module_connected,
            )
        )
    return registered


def _attached_module_ids(api: object) -> collections.abc.Callable[[], list[tuple[str, str]]]:
    def read() -> list[tuple[str, str]]:
        factories = _module_factories()
        return [
            (module.MODULE_TYPE.name, _module_serial(module))
            for module in getattr(api, "attached_modules", ())
            if module.MODULE_TYPE in factories
        ]

    return read


def _shared_hardware_robot_server_lifespan(
    original_lifespan: collections.abc.Callable[[_AppWithState], typing.AsyncContextManager[None]],
    proxy: HardwareProxy,
    *,
    access_log: bool = False,
) -> collections.abc.Callable[[_AppWithState], typing.AsyncContextManager[None]]:
    """
    Add robot-server post-init callbacks skipped by shared hardware injection.

    The native lifespan sees the completed hardware initialization task installed
    by the connector, so it deliberately does not run its hardware callbacks.
    Protocol Engine routes still require the light controller created by those
    callbacks. Run only that public robot-server initialization pair after the
    native lifespan has established its task runner and persistence services,
    then create the run-orchestrator store eagerly. Stock robot-server creates
    that store lazily on the first ``/runs`` request; the connector reads it on
    every SiLA command to arbitrate hardware ownership, so it must exist before
    the server is reported ready.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: _AppWithState) -> collections.abc.AsyncGenerator[None, None]:
        bindings = load_robot_server_bindings()
        start_light_control_task = typing.cast(
            collections.abc.Callable[..., collections.abc.Awaitable[object]],
            bindings.start_light_control_task,
        )
        mark_light_control_startup_finished = typing.cast(
            collections.abc.Callable[..., collections.abc.Awaitable[object]],
            bindings.mark_light_control_startup_finished,
        )

        async with original_lifespan(app):
            # robot-server's lifespan re-applies its own logging configuration.
            quiet_noisy_loggers(access_log=access_log)
            await start_light_control_task(app.state, proxy)
            await mark_light_control_startup_finished(app.state, proxy)
            await initialize_run_store(bindings, proxy)
            yield

    return lifespan


@dataclasses.dataclass
class _Hardware:
    """Everything built from one OT3API that both startup modes share."""

    api: typing.Any
    lock: asyncio.Lock | RunAwareLock
    motion: FlexMotionController
    gripper: FlexGripperController
    calibration: FlexCalibrationController
    labware_state: LabwareMovementState | None


async def _build_api(config: OpentronsFlexConfig) -> typing.Any:  # noqa: ANN401 - OT3API is imported lazily
    from opentrons.hardware_control.ot3api import OT3API

    if config.use_simulator:
        log.warning("Building the OT3API SIMULATOR. No physical Flex hardware will move.")
        return await OT3API.build_hardware_simulator(
            attached_instruments=_simulator_attached_instruments(config),
            attached_modules=_simulator_attached_modules(config),
        )
    log.info("Building OT3API for real Flex hardware (CAN)")
    return await OT3API.build_hardware_controller()


def _wire_hardware(
    config: OpentronsFlexConfig,
    api: object,
    lock: asyncio.Lock | RunAwareLock,
    labware_config: LoadedLabwareMovementConfig,
) -> _Hardware:
    """Create the controllers that share ``api`` and ``lock``, and bind durable recovery state."""
    if config.recovery_state_path is not None:
        recovery_state_for(api).bind_persistence(config.recovery_state_path)
    labware_state = (
        LabwareMovementState(labware_config.state_file, labware_config.initial_occupancy)
        if labware_config.state_file is not None
        else None
    )
    return _Hardware(
        api=api,
        lock=lock,
        motion=FlexMotionController.from_api(api, lock=lock, lock_timeout_s=config.lock_timeout_s),
        gripper=FlexGripperController.from_api(api, lock=lock, lock_timeout_s=config.lock_timeout_s),
        calibration=FlexCalibrationController.from_api(api, lock=lock, lock_timeout_s=config.lock_timeout_s),
        labware_state=labware_state,
    )


def _register_all(
    connector: Connector,
    hardware: _Hardware,
    labware_config: LoadedLabwareMovementConfig,
    *,
    run_state: collections.abc.Callable[[], tuple[str | None, str, bool]] | None = None,
    http_server_state: collections.abc.Callable[[], HttpServerState] | None = None,
    restart_pending: collections.abc.Callable[[], str | None] | None = None,
    warnings: collections.abc.Callable[[], collections.abc.Iterable[str]] | None = None,
) -> tuple[RobotStatusSource, set[str], RecoveryCoordinator]:
    """Register core, module, recovery, and status features; return the status source, module types, and recovery."""
    _register_core_features(
        connector,
        hardware.motion,
        hardware.gripper,
        hardware.calibration,
        labware_config,
        hardware.labware_state,
    )
    module_controllers: list[object] = []
    registered_modules = _register_modules(
        connector,
        hardware.api.attached_modules,
        hardware.lock,
        live_modules=lambda: hardware.api.attached_modules,
        controllers_out=module_controllers,
    )
    labware_state = hardware.labware_state
    coordinator = RecoveryCoordinator(
        hardware.motion,
        hardware.gripper,
        stackers=[
            StackerRecoveryTarget(
                serial=_module_serial(controller._bound_module),
                recovery_required=controller.recovery_required_now,
                connected=lambda controller=controller: controller.module_connected,
                home=lambda controller=controller: controller.home_all(ignore_latch=False),
            )
            for controller in module_controllers
            if isinstance(controller, FlexStackerController)
        ],
        deck_valid=(lambda: labware_state.valid) if labware_state is not None else None,
    )
    connector.register(RecoveryController(coordinator))
    source = RobotStatusSource(
        hardware.motion,
        run_state=run_state,
        deck_state=(lambda: labware_state.valid) if labware_state is not None else None,
        registered_modules=registered_modules,
        attached_module_ids=_attached_module_ids(hardware.api),
        http_server_state=http_server_state,
        restart_pending=restart_pending,
        recovering=lambda: coordinator.running,
        warnings=warnings,
        previous_exit=_LIFECYCLE.previous.summary if _LIFECYCLE is not None and not _LIFECYCLE.previous.clean else "",
    )
    connector.register(RobotStatusProvider(source))
    return source, {module.module_type for module in registered_modules}, coordinator


_FINISHED_RUN_STATUSES = frozenset({"idle", "succeeded", "failed", "stopped"})


def _safe_to_restart(source: RobotStatusSource, hardware: _Hardware) -> bool:
    """
    Whether an automatic restart would interrupt nothing.

    Only a fully READY robot qualifies: no Protocol Engine run (in any state), no
    recovery gate that a restart could lose, no observable command in flight (this
    includes temperature waits and thermocycler profiles that release the hardware
    lock while they wait), no locked motion, and no module operation in progress.
    """
    snapshot = source.snapshot()
    if snapshot.state is not RobotState.READY:
        return False
    if snapshot.run.status not in _FINISHED_RUN_STATUSES or not snapshot.run.direct_control_allowed:
        return False
    if observable_commands_in_flight() or module_activity(hardware.lock):
        return False
    locked = getattr(hardware.lock, "locked", None)
    return not (callable(locked) and locked())


def _auto_recovery_may_run(source: RobotStatusSource, hardware: _Hardware) -> bool:
    """
    Whether automatic recovery may act now.

    RECOVERY_REQUIRED outranks BUSY and PROTOCOL_RUN_ACTIVE in the state
    precedence, so idleness is checked separately: no command in flight, no
    locked hardware, and no Protocol Engine run that owns the robot.
    """
    snapshot = source.snapshot()
    if snapshot.state is not RobotState.RECOVERY_REQUIRED:
        return False
    if observable_commands_in_flight() or module_activity(hardware.lock) or hardware.motion.hardware_busy:
        return False
    return snapshot.run.direct_control_allowed and snapshot.run.status in _FINISHED_RUN_STATUSES


def _start_background_tasks(
    config: OpentronsFlexConfig,
    hardware: _Hardware,
    source: RobotStatusSource,
    registered_types: set[str],
    restart: "_RestartRequest",
    coordinator: RecoveryCoordinator,
) -> asyncio.Task[None]:
    """Start module hot-plug handling and automatic recovery as one supervised task."""
    auto_recovery = AutoRecovery(
        coordinator,
        mode=config.auto_recovery,
        gated_and_idle=lambda: _auto_recovery_may_run(source, hardware),
    )
    hot_plug = _start_hot_plug_monitor(config, hardware, source, registered_types, restart)

    async def supervise() -> None:
        recovery = asyncio.create_task(auto_recovery.run(), name="flex-auto-recovery")
        try:
            await asyncio.gather(hot_plug, recovery)
        finally:
            for task in (hot_plug, recovery):
                task.cancel()
            await asyncio.gather(hot_plug, recovery, return_exceptions=True)

    return asyncio.create_task(supervise(), name="flex-background")


def _start_hot_plug_monitor(
    config: OpentronsFlexConfig,
    hardware: _Hardware,
    source: RobotStatusSource,
    registered_types: set[str],
    restart: "_RestartRequest",
) -> asyncio.Task[None]:
    """Watch for newly attached module types and restart when idle (or only report)."""
    mode = config.module_hot_plug
    if mode == "auto":
        mode = "report" if config.use_simulator else "restart"
    monitor = ModuleHotPlugMonitor(
        registered_types=registered_types,
        attached=_attached_module_ids(hardware.api),
        idle=lambda: _safe_to_restart(source, hardware),
        restart=restart.request if mode == "restart" else None,
    )
    return asyncio.create_task(monitor.run(), name="flex-module-hot-plug")


async def _stop_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _load_labware_config(config: OpentronsFlexConfig) -> LoadedLabwareMovementConfig:
    if config.labware_movement_config is not None:
        return load_labware_movement_config(config.labware_movement_config)
    return LoadedLabwareMovementConfig(plans=(), initial_occupancy={}, state_file=None, profile_sha256="0" * 64)


async def create_app(config: OpentronsFlexConfig) -> collections.abc.AsyncGenerator[Connector, None]:
    """
    Create the connector application.

    Builds a single ``OT3API`` (simulator or real Flex hardware), shares it across the
    motion/pipette/gripper/calibration controllers via one lock, and registers the
    corresponding SiLA features plus ``RobotStatusProvider``. When
    ``config.with_robot_server`` is True the opentrons HTTP robot-server is started in
    the same process sharing that one API (see ``_create_app_with_robot_server``).
    """
    log.info(
        "Starting Opentrons Flex connector v%s (simulate=%s, robot_server=%s)",
        __version__,
        config.use_simulator,
        config.with_robot_server,
    )
    compatibility = require_compatible_runtime(require_robot_server=config.with_robot_server)

    legacy_flags = [
        f"simulated_{name}" for name in SIMULATED_MODULE_NAMES if getattr(config, f"simulated_{name}", False)
    ]
    simulated_modules = [*legacy_flags, *(f"simulated_modules[{name}]" for name in config.simulated_modules)]
    if simulated_modules and not config.use_simulator:
        settings = ", ".join(simulated_modules)
        message = (
            f"Simulation settings {settings} require use_simulator=true. "
            "For a real Flex, connect and power the physical modules before starting the connector."
        )
        raise ValueError(message)

    labware_config = _load_labware_config(config)
    require_robot_environment(use_simulator=config.use_simulator, required=config.require_robot_environment)
    quiet_noisy_loggers(access_log=config.robot_server_access_log)

    global _LIFECYCLE  # noqa: PLW0603 - one lifecycle record per connector process
    _LIFECYCLE = LifecycleRecord(config.lifecycle_state_path)
    previous = _LIFECYCLE.start()
    EVENTS.record(
        EventKind.LIFECYCLE,
        "Connector",
        f"started v{__version__}" + (f"; previous run: {previous.summary}" if previous.summary else ""),
    )
    try:
        if config.with_robot_server:
            async for connector in _create_app_with_robot_server(config, labware_config, compatibility):
                async with systemd_service(f"Serving SiLA on port {config.sila_server.port} with the web server"):
                    yield connector
            return

        async for connector in _create_app_sila_only(config, labware_config):
            async with systemd_service(f"Serving SiLA on port {config.sila_server.port}"):
                yield connector
    finally:
        _LIFECYCLE.stop("orderly shutdown")


async def _create_app_sila_only(
    config: OpentronsFlexConfig,
    labware_config: LoadedLabwareMovementConfig,
) -> collections.abc.AsyncGenerator[Connector, None]:
    """Serve SiLA without the embedded robot-server (simulator or connector-unit mode)."""
    api = await _build_api(config)
    hardware: _Hardware | None = None
    restart = _RestartRequest()
    monitor: asyncio.Task[None] | None = None
    try:
        hardware = _wire_hardware(config, api, asyncio.Lock(), labware_config)
        app = Connector(config)
        source, registered_types, coordinator = _register_all(
            app,
            hardware,
            labware_config,
            restart_pending=lambda: restart.reason,
            warnings=_standing_warnings(config),
        )
        monitor = _start_background_tasks(config, hardware, source, registered_types, restart, coordinator)
        log.info("SiLA server listening on %s:%d", config.sila_server.hostname, config.sila_server.port)
        yield app
    finally:
        try:
            await _stop_task(monitor)
            if hardware is not None and hardware.labware_state is not None:
                hardware.labware_state.close()
        finally:
            recovery_state_for(api).wait_for_persistence()
            await api.clean_up()
            restart.exit_if_requested()


def _prepare_mutation_runtime(
    config: OpentronsFlexConfig,
    compatibility: RuntimeCompatibilityReport,
) -> tuple[str | None, str | None, MutationLedger | None]:
    """Validate controlled-mutation state before hardware acquisition."""
    mutation_api_token = os.environ.get(config.run_mutation_token_env)
    mutation_authenticated_actor = os.environ.get(config.run_mutation_actor_env)
    mutation_ledger: MutationLedger | None = None
    mutation_issues = mutation_configuration_issues(
        compatibility,
        ledger_path=config.run_mutation_ledger_path,
        token=mutation_api_token,
        actor=mutation_authenticated_actor,
    )
    if mutation_issues:
        detail = " ".join(mutation_issues)
        if config.run_mutation_required:
            message = f"Controlled run mutation preflight failed: {detail}"
            raise RuntimeError(message)
        log.warning(
            "Controlled run mutation endpoints are disabled: %s "
            "Protocol Engine run ownership protection remains active.",
            detail,
        )
        mutation_api_token = None
        mutation_authenticated_actor = None
    else:
        mutation_authenticated_actor = typing.cast(str, mutation_authenticated_actor).strip()
        try:
            # Verify durable state before acquiring OT3API or mutating the
            # global robot-server app. A corrupt ledger must never be ignored
            # when mutation is required.
            mutation_ledger = MutationLedger(typing.cast(str, config.run_mutation_ledger_path))
        except (MutationLedgerError, OSError) as exc:
            if config.run_mutation_required:
                message = f"Controlled run mutation ledger preflight failed: {exc}"
                raise RuntimeError(message) from exc
            log.error(
                "Controlled run mutation endpoints are disabled because the durable ledger failed verification: %s",
                exc,
            )
            mutation_api_token = None
            mutation_authenticated_actor = None
    return mutation_api_token, mutation_authenticated_actor, mutation_ledger


def _request_restart() -> None:
    """Ask the connector process to shut down cleanly (it then exits non-zero to be restarted)."""
    os.kill(os.getpid(), signal.SIGTERM)


RESTART_EXIT_CODE = 75
"""Exit status that asks the service manager (systemd ``Restart=on-failure``) to start the connector again."""


_LIFECYCLE: LifecycleRecord | None = None


def _standing_warnings(config: OpentronsFlexConfig) -> collections.abc.Callable[[], list[str]]:
    """Return a reader of configuration problems that do not stop the connector but should be fixed."""

    def read() -> list[str]:
        warnings: list[str] = []
        if config.with_robot_server and os.environ.get(PERSISTENCE_ENV, "") in ("", "automatically_make_temporary"):
            warnings.append(
                "The embedded Opentrons web server keeps protocols, runs, labware offsets, and the deck "
                "configuration in a temporary directory, so they are lost on every restart. Reinstall the "
                "service with scripts/install_connector_service.sh or set robot_server_persistence_directory."
            )
        if not config.use_simulator and not robot_environment_detected():
            warnings.append(
                "The Opentrons robot environment is not set, so calibration is not read from /data. "
                "Reinstall the service with scripts/install_connector_service.sh."
            )
        return warnings

    return read


class _RestartRequest:
    """One-shot request to restart the connector process after a clean shutdown."""

    def __init__(self) -> None:
        self.reason: str | None = None

    def request(self, reason: str) -> None:
        """Shut down cleanly now; the process then exits with ``RESTART_EXIT_CODE``."""
        if self.reason is not None:
            return
        self.reason = reason
        EVENTS.record(EventKind.LIFECYCLE, "Connector", f"restarting to {reason}")
        _request_restart()

    def exit_if_requested(self) -> None:
        """Call after hardware cleanup; terminates the process when a restart was requested."""
        if self.reason is not None:
            if _LIFECYCLE is not None:
                _LIFECYCLE.stop(f"restarted to {self.reason}")
            log.critical("Exiting with status %d so the service manager restarts the connector.", RESTART_EXIT_CODE)
            os._exit(RESTART_EXIT_CODE)


class _RobotServerWatchdog:
    """
    Detect an embedded robot-server that stops while the connector keeps serving SiLA.

    The connector cannot arbitrate Protocol Engine ownership without the HTTP
    server, so an unexpected exit is fatal: the watchdog asks the process to shut
    down cleanly after ``grace_s`` and the connector then exits with
    ``EXIT_CODE`` so systemd (``Restart=on-failure``) restarts it.
    """

    EXIT_CODE = RESTART_EXIT_CODE

    def __init__(
        self,
        server: object,
        *,
        grace_s: float = 5.0,
        terminate: collections.abc.Callable[[], None] | None = None,
        restart: _RestartRequest | None = None,
    ) -> None:
        self._server = server
        self._grace_s = grace_s
        self._terminate = terminate
        self._restart = restart
        self.started = False
        self.failed = False
        self.terminated = False
        self.shutting_down = False

    def state(self) -> HttpServerState:
        """Return the embedded robot-server state for status reporting."""
        if self.failed:
            return HttpServerState.STOPPED
        return HttpServerState.RUNNING if self.started else HttpServerState.STARTING

    def watch(self, task: asyncio.Task[object]) -> None:
        """Begin watching the uvicorn serve task once startup has completed."""
        self.started = True
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[object]) -> None:
        if self.shutting_down or bool(getattr(self._server, "should_exit", False)):
            return
        self.failed = True
        failure = None if task.cancelled() else task.exception()
        log.critical(
            "The embedded robot-server stopped unexpectedly (%s). Protocol Engine ownership can no longer be "
            "verified; the connector will restart in %.0f s.",
            failure or "no exception",
            self._grace_s,
        )
        asyncio.get_running_loop().call_later(self._grace_s, self._fire)

    def _fire(self) -> None:
        if self.shutting_down:
            return
        self.terminated = True
        if self._terminate is not None:
            self._terminate()
        elif self._restart is not None:
            self._restart.request("recover from an embedded robot-server failure")
        else:
            _request_restart()


async def _create_app_with_robot_server(
    config: OpentronsFlexConfig,
    labware_config: LoadedLabwareMovementConfig,
    compatibility: RuntimeCompatibilityReport,
) -> collections.abc.AsyncGenerator[Connector, None]:
    """
    Start both the SiLA2 gRPC server and the opentrons HTTP robot-server in one process.

    Hardware cleanup is registered immediately after acquisition. Synchronous
    robot-server and labware cleanup is registered before any subsequent
    fallible startup step.
    """
    mutation_api_token, mutation_authenticated_actor, mutation_ledger = _prepare_mutation_runtime(
        config,
        compatibility,
    )
    persistence = apply_robot_server_persistence(config.robot_server_persistence_directory)
    if not persistence.temporary:
        remove_stale_robot_server_tempdirs()
    shared_hardware = await _build_api(config)
    restart = _RestartRequest()

    try:
        with contextlib.ExitStack() as setup_cleanup:
            async for connector in _serve_with_robot_server(
                config,
                labware_config,
                compatibility,
                shared_hardware=shared_hardware,
                mutation_api_token=mutation_api_token,
                mutation_authenticated_actor=mutation_authenticated_actor,
                mutation_ledger=mutation_ledger,
                setup_cleanup=setup_cleanup,
                restart=restart,
            ):
                yield connector
    finally:
        recovery_state_for(shared_hardware).wait_for_persistence()
        await shared_hardware.clean_up()
        restart.exit_if_requested()


async def _serve_with_robot_server(
    config: OpentronsFlexConfig,
    labware_config: LoadedLabwareMovementConfig,
    compatibility: RuntimeCompatibilityReport,
    *,
    shared_hardware: object,
    mutation_api_token: str | None,
    mutation_authenticated_actor: str | None,
    mutation_ledger: MutationLedger | None,
    setup_cleanup: contextlib.ExitStack,
    restart: _RestartRequest | None = None,
) -> collections.abc.AsyncGenerator[Connector, None]:
    """Wire and serve robot-server using already-acquired shared hardware."""
    import uvicorn
    from opentrons.protocol_engine import DeckType
    from opentrons_shared_data.robot.types import RobotTypeEnum

    robot_server = load_robot_server_bindings()

    def _current_run_store() -> object | None:
        # robot-server stores this singleton under the stable app-state key used
        # by its AppStateAccessor. Reading the state directly keeps the shared
        # hardware wiring testable without importing robot-server's private runs
        # package on development hosts.
        return current_run_store(robot_server, robot_server_ready=robot_server_ready)

    robot_server_ready = False

    def _current_run_state() -> ProtocolRunState:
        return protocol_run_state(
            _current_run_store(),
            robot_server_ready=robot_server_ready,
            checkpoint_prefix=MUTATION_CHECKPOINT_PREFIX,
        )

    run_authority = ProtocolRunAuthority(_current_run_state)
    mutation_gate = RunMutationGate()
    shared_lock = RunAwareLock(run_authority)
    hardware = _wire_hardware(config, shared_hardware, shared_lock, labware_config)
    labware_state = hardware.labware_state
    if labware_state is not None:
        setup_cleanup.callback(labware_state.close)
    robot_server_hardware = (
        OT3SimulatorCompatibilityAdapter(shared_hardware) if config.use_simulator else shared_hardware
    )
    proxy = HardwareProxy(
        robot_server_hardware,
        lock=shared_lock,
        lock_timeout_s=config.lock_timeout_s,
        labware_state=labware_state,
    )

    # Pre-populate robot_server app state so it does not initialise hardware a second time.
    async def _noop() -> None:
        pass

    init_task: asyncio.Task[None] = asyncio.create_task(_noop())
    await init_task

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

    flex_identity_overrides = {
        robot_server.get_robot_type: _get_flex_robot_type,
        robot_server.get_robot_type_enum: _get_flex_robot_type_enum,
        robot_server.get_deck_type: _get_flex_deck_type,
    }
    added_robot_server_routes: list[object] = []
    robot_server_installation = install_robot_server_app(
        robot_server,
        initialization_task=init_task,
        hardware_proxy=proxy,
        identity_overrides=flex_identity_overrides,
        lifespan_factory=lambda original_lifespan: _shared_hardware_robot_server_lifespan(
            typing.cast(typing.Callable[..., object], original_lifespan),
            proxy,
            access_log=config.robot_server_access_log,
        ),
    )

    def _restore_installed_robot_server() -> None:
        restore_robot_server_app(
            robot_server_installation,
            added_routes=tuple(added_robot_server_routes),
        )

    setup_cleanup.callback(_restore_installed_robot_server)
    robot_server_app = typing.cast(typing.Any, robot_server_installation.app)

    runtime_identity = process_runtime_identity(
        compatibility,
        require_release=not config.use_simulator,
    )
    runtime_identity_router = create_runtime_identity_router(runtime_identity)
    added_robot_server_routes.extend(
        include_robot_server_router(
            robot_server_installation,
            runtime_identity_router,
        )
    )
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
        added_robot_server_routes.extend(
            include_robot_server_router(
                robot_server_installation,
                mutation_router,
            )
        )
        log.info("Controlled run mutation enabled with durable ledger %s", mutation_ledger.path)
    else:
        log.info("Controlled run mutation routes were not registered.")

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
            access_log=config.robot_server_access_log,
        )
        log.info("robot-server starting on 127.0.0.1:%d", config.robot_server_tcp_port)
    else:
        uv_config = uvicorn.Config(
            guarded_robot_server_app,
            uds=config.robot_server_uds,
            ws="wsproto",
            loop="none",
            log_level="info",
            access_log=config.robot_server_access_log,
        )
        log.info("robot-server starting on %s", config.robot_server_uds)

    uv_server = uvicorn.Server(uv_config)
    restart = restart or _RestartRequest()
    watchdog = _RobotServerWatchdog(uv_server, restart=restart)
    monitor: asyncio.Task[None] | None = None
    robot_server_task = asyncio.create_task(uv_server.serve())

    def _run_state() -> tuple[str | None, str, bool]:
        state = run_authority.current()
        return state.run_id, state.status, state.owns_hardware

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.robot_server_startup_timeout_s
        while not bool(getattr(uv_server, "started", False)):
            if robot_server_task.done():
                failure = robot_server_task.exception()
                if failure is not None:
                    raise failure
                message = "Embedded robot-server stopped before completing startup."
                raise RuntimeError(message)
            if loop.time() >= deadline:
                message = (
                    f"Embedded robot-server did not finish starting within {config.robot_server_startup_timeout_s:g} "
                    "s. Check the robot-server log above, or raise robot_server_startup_timeout_s."
                )
                raise RuntimeError(message)
            await asyncio.sleep(0.01)
        robot_server_ready = True
        watchdog.watch(robot_server_task)

        connector = Connector(config)
        source, registered_types, coordinator = _register_all(
            connector,
            hardware,
            labware_config,
            run_state=_run_state,
            http_server_state=watchdog.state,
            restart_pending=lambda: restart.reason,
            warnings=_standing_warnings(config),
        )
        monitor = _start_background_tasks(config, hardware, source, registered_types, restart, coordinator)

        log.info("SiLA server listening on %s:%d", config.sila_server.hostname, config.sila_server.port)

        yield connector
    finally:
        watchdog.shutting_down = True
        await _stop_task(monitor)
        uv_server.should_exit = True
        await asyncio.gather(robot_server_task, return_exceptions=True)
