import asyncio
import collections.abc
import dataclasses
import logging
from importlib.metadata import version

from unitelabs.cdk import Connector, ConnectorBaseConfig, SiLAServerConfig

from .features import (
    CalibrationFeature,
    GripperFeature,
    HeaterShakerFeature,
    MotionControlFeature,
    PipetteFeature,
    TemperatureModuleFeature,
    ThermocyclerFeature,
)
from .io import (
    FlexCalibrationController,
    FlexGripperController,
    FlexMotionController,
    HardwareProxy,
    HeaterShakerController,
    TemperatureModuleController,
    ThermocyclerController,
)

log = logging.getLogger(__name__)

__version__ = version("unitelabs-opentrons-flex")


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
            description="SiLA2 connector for Opentrons Flex motion, pipettes, gripper and calibration",
            version=str(__version__),
            vendor_url="https://opentrons.com/",
        )
    )


# Module type -> (IO controller class, SiLA feature class). The Magnetic Module is
# intentionally absent — the Flex does not support it.
def _module_factories() -> dict:
    from opentrons.hardware_control.modules.types import ModuleType

    return {
        ModuleType.HEATER_SHAKER: (HeaterShakerController, HeaterShakerFeature),
        ModuleType.THERMOCYCLER: (ThermocyclerController, ThermocyclerFeature),
        ModuleType.TEMPERATURE: (TemperatureModuleController, TemperatureModuleFeature),
    }


def _register_core_features(
    connector: Connector,
    motion: FlexMotionController,
    gripper: FlexGripperController,
    calibration: FlexCalibrationController,
) -> None:
    connector.register(MotionControlFeature(motion))
    connector.register(PipetteFeature(motion))
    connector.register(GripperFeature(gripper))
    connector.register(CalibrationFeature(calibration))


def _register_modules(connector: Connector, attached_modules: collections.abc.Iterable) -> None:
    factories = _module_factories()
    for module in attached_modules:
        factory = factories.get(module.MODULE_TYPE)
        if factory is None:
            log.info("Skipping unsupported module type %s", module.MODULE_TYPE.name)
            continue
        controller_cls, feature_cls = factory
        connector.register(feature_cls(controller_cls.from_module(module)))
        log.info("Registered SiLA feature for module %s", module.MODULE_TYPE.name)


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

    if config.with_robot_server:
        async for connector in _create_app_with_robot_server(config):
            yield connector
        return

    from opentrons.hardware_control.ot3api import OT3API

    if config.use_simulator:
        log.info("Building OT3API simulator backend")
        api = await OT3API.build_hardware_simulator()
    else:
        log.info("Building OT3API for real Flex hardware (CAN)")
        api = await OT3API.build_hardware_controller()

    shared_lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
    gripper = FlexGripperController.from_api(api, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
    calibration = FlexCalibrationController.from_api(api, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)

    app = Connector(config)
    _register_core_features(app, motion, gripper, calibration)
    _register_modules(app, api.attached_modules)

    log.info("SiLA server listening on %s:%d", config.sila_server.hostname, config.sila_server.port)

    yield app

    await api.clean_up()


async def _create_app_with_robot_server(
    config: OpentronsFlexConfig,
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

    from robot_server.hardware import _hw_api_accessor, _init_task_accessor  # type: ignore[import]
    from robot_server.app import app as robot_server_app  # type: ignore[import]

    if config.use_simulator:
        log.info("Building shared OT3API (simulator)")
        shared_hardware = await OT3API.build_hardware_simulator()
    else:
        log.info("Building shared OT3API on CAN bus")
        shared_hardware = await OT3API.build_hardware_controller()

    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(shared_hardware, lock=shared_lock, lock_timeout_s=config.lock_timeout_s)
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

    if config.robot_server_tcp_port is not None:
        uv_config = uvicorn.Config(
            robot_server_app,
            host="127.0.0.1",
            port=config.robot_server_tcp_port,
            ws="wsproto",
            loop="none",
            log_level="info",
        )
        log.info("robot-server starting on 127.0.0.1:%d", config.robot_server_tcp_port)
    else:
        uv_config = uvicorn.Config(
            robot_server_app,
            uds=config.robot_server_uds,
            ws="wsproto",
            loop="none",
            log_level="info",
        )
        log.info("robot-server starting on %s", config.robot_server_uds)

    uv_server = uvicorn.Server(uv_config)
    robot_server_task = asyncio.create_task(uv_server.serve())

    connector = Connector(config)
    _register_core_features(connector, motion, gripper, calibration)
    _register_modules(connector, shared_hardware.attached_modules)

    log.info("SiLA server listening on %s:%d", config.sila_server.hostname, config.sila_server.port)

    yield connector

    uv_server.should_exit = True
    await asyncio.gather(robot_server_task, return_exceptions=True)
    await shared_hardware.clean_up()
