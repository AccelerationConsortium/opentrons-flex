"""Shared fixtures and options for integration tests.

Pass --robot HOST:PORT to run against a live SiLA2 server instead of the
built-in simulator.  The gRPC channel is redirected; the local simulator is
still started to obtain the protobuf codec object (pb).

Pass --robot-http HOST:PORT (or just --robot HOST:PORT, port is ignored) to run
HTTP API tests against the robot's built-in HTTP server on port 31950.  These
tests exercise the opentrons robot-server we start in-process with our injected
HardwareProxy.

Pass --with-http-server to start our connector in simulator mode with the
robot-server on a free TCP port and run the HTTP API tests against it locally.
This is used in CI to exercise the full HTTP+gRPC stack without real hardware.

Markers:
  simulator_only  — skipped when --robot is set
  robot_http_only — skipped unless --robot-http, --robot, or --with-http-server is set
  smoketest_http_only — skipped unless --with-http-server starts the local simulator HTTP stack
"""

import asyncio
import contextlib
import socket
import sys
import logging
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from unittest.mock import MagicMock

import grpc.aio
import httpx
import pytest
import pytest_asyncio

from unitelabs.cdk import SiLAServerConfig
from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app

log = logging.getLogger("opentrons_flex.tests")

_HTTP_API_PORT = 31950
_HTTP_API_VERSION_HEADER = "Opentrons-Version"

_SIMULATOR_DEVICE_ID = "ot3-simulator"


@dataclass(frozen=True)
class SimulatorStack:
    """Addresses for a local connector stack running in smoketest/simulator mode."""

    http_url: str
    grpc_address: str
    protobuf: object


@dataclass(frozen=True)
class RunContext:
    """
    Explicit record of *where* and *how* a test is running (Pitfall #1 guard).

    Generated code can silently run "hardware" tests against the simulator (or
    vice-versa). Every integration test records this context to its junit output
    and the log so each result is unambiguous about ``mode`` (smoketest vs
    hardware), the ``target`` it hit, and the ``device_id`` / config in use.
    """

    mode: str  # "smoketest" | "hardware"
    sila_target: str  # gRPC address actually used
    http_target: str  # HTTP base URL actually used, or "n/a"
    device_id: str  # stable device identity: robot host, or "ot3-simulator"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--robot",
        metavar="HOST:PORT",
        default=None,
        help="Run integration tests against a live SiLA2 server (e.g. 100.108.249.112:50051)",
    )
    parser.addoption(
        "--robot-http",
        metavar="HOST:PORT",
        default=None,
        help=(
            "Run HTTP API integration tests against a live robot "
            "(e.g. 100.108.249.112:31950). If omitted but --robot is set, the host "
            "is taken from --robot and port defaults to 31950."
        ),
    )
    parser.addoption(
        "--with-http-server",
        action="store_true",
        default=False,
        help=(
            "Start the connector in simulator mode with the opentrons robot-server "
            "on a free TCP port and run HTTP API tests against it. "
            "Requires the robot_server package to be installed."
        ),
    )
    parser.addoption(
        "--heater-shaker-actuation",
        action="store_true",
        default=False,
        help=(
            "Enable opt-in Heater-Shaker hardware actuation tests. Before using this flag, "
            "install a compatible thermal adapter and labware, close the robot door, and keep the E-stop ready."
        ),
    )


def _is_hardware_run(config: pytest.Config) -> bool:
    """Whether this session targets a real robot (a --robot / --robot-http host is set)."""
    return bool(config.getoption("--robot") or config.getoption("--robot-http"))


def _compute_run_context(config: pytest.Config) -> RunContext:
    """Derive the explicit mode/target/device record for this session (Pitfall #1)."""
    robot = config.getoption("--robot")
    robot_http = config.getoption("--robot-http")
    with_http = bool(config.getoption("--with-http-server"))

    if _is_hardware_run(config):
        host = (robot or robot_http).split(":")[0]
        sila_target = robot or f"{host}:50051"
        if robot_http:
            http_target = robot_http if ":" in robot_http else f"{robot_http}:{_HTTP_API_PORT}"
        else:
            http_target = f"{host}:{_HTTP_API_PORT}"
        return RunContext(mode="hardware", sila_target=sila_target, http_target=http_target, device_id=host)

    return RunContext(
        mode="smoketest",
        sila_target="in-process OT3API simulator",
        http_target="in-process robot-server (simulator)" if with_http else "n/a",
        device_id=_SIMULATOR_DEVICE_ID,
    )


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Print the run mode/target/device at the top of the session (Pitfall #1)."""
    ctx = _compute_run_context(config)
    return [
        f"opentrons-flex integration mode={ctx.mode} device_id={ctx.device_id} "
        f"sila={ctx.sila_target} http={ctx.http_target}"
    ]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_robot = bool(config.getoption("--robot"))
    has_robot_http = bool(config.getoption("--robot-http")) or has_robot or bool(config.getoption("--with-http-server"))
    has_smoketest_http = bool(config.getoption("--with-http-server"))
    has_hardware = _is_hardware_run(config)
    has_heater_shaker_actuation = bool(config.getoption("--heater-shaker-actuation"))

    skip_sim = pytest.mark.skip(reason="simulator-only test, skipped when --robot is set")
    skip_http = pytest.mark.skip(reason="robot_http_only test, requires --robot-http, --robot, or --with-http-server")
    skip_smoketest_http = pytest.mark.skip(reason="smoketest_http_only test, requires --with-http-server")
    skip_hardware = pytest.mark.skip(reason="hardware_only test, requires --robot or --robot-http (a real Flex)")
    skip_heater_shaker_actuation = pytest.mark.skip(
        reason="Heater-Shaker actuation requires the explicit --heater-shaker-actuation safety gate"
    )

    for item in items:
        if has_robot and item.get_closest_marker("simulator_only"):
            item.add_marker(skip_sim)
        if not has_robot_http and item.get_closest_marker("robot_http_only"):
            item.add_marker(skip_http)
        if not has_smoketest_http and item.get_closest_marker("smoketest_http_only"):
            item.add_marker(skip_smoketest_http)
        if not has_hardware and item.get_closest_marker("hardware_only"):
            item.add_marker(skip_hardware)
        if not has_heater_shaker_actuation and item.get_closest_marker("heater_shaker_actuation"):
            item.add_marker(skip_heater_shaker_actuation)


@pytest.fixture(scope="session")
def run_context(request: pytest.FixtureRequest) -> RunContext:
    """Session-wide record of the mode/target/device this run is exercising."""
    return _compute_run_context(request.config)


@pytest.fixture(autouse=True)
def _record_run_context(request: pytest.FixtureRequest, run_context: RunContext, record_property) -> None:
    """
    Attach mode/target/device_id to every integration test's result (Pitfall #1).

    Guards against the "silently ran against the wrong target" trap: the mode,
    target and device_id are written to the junit ``<properties>`` for each test
    and logged, so no result is ambiguous about whether it hit the simulator or a
    real device. A ``hardware_only`` test is also asserted to actually be running
    in hardware mode so it can never pass by accident against the simulator.
    """
    record_property("mode", run_context.mode)
    record_property("sila_target", run_context.sila_target)
    record_property("http_target", run_context.http_target)
    record_property("device_id", run_context.device_id)

    if request.node.get_closest_marker("hardware_only") and run_context.mode != "hardware":
        pytest.fail(
            "hardware_only test reached execution in smoketest mode — "
            "movement results would be against the simulator, not a real device."
        )

    log.info(
        "RUN mode=%s device_id=%s sila=%s http=%s :: %s",
        run_context.mode,
        run_context.device_id,
        run_context.sila_target,
        run_context.http_target,
        request.node.nodeid,
    )


@pytest.fixture(scope="session")
def robot_address(request: pytest.FixtureRequest) -> str | None:
    return request.config.getoption("--robot")


@pytest.fixture(scope="session")
def is_smoketest_http(request: pytest.FixtureRequest) -> bool:
    """Whether tests are using the local simulator HTTP stack."""
    return bool(request.config.getoption("--with-http-server"))


@pytest.fixture(scope="session")
def simulator_stack(request: pytest.FixtureRequest) -> Generator[SimulatorStack | None, None, None]:
    """Start the connector with simulator + robot-server on free local ports.

    Runs the asyncio event loop in a background thread so the server stays up
    for the full test session while sync test fixtures can still access the HTTP
    URL. Yields None when --with-http-server is not set.
    """
    if not request.config.getoption("--with-http-server"):
        yield None
        return
    rs_app = sys.modules.get("robot_server.app")
    if rs_app is not None and isinstance(getattr(rs_app, "app", None), MagicMock):
        pytest.skip("--with-http-server requires the real opentrons robot_server package")

    def _free_ports(count: int) -> list[int]:
        sockets: list[socket.socket] = []
        try:
            for _ in range(count):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", 0))
                sockets.append(s)
            return [s.getsockname()[1] for s in sockets]
        finally:
            for s in sockets:
                s.close()

    http_port, grpc_port = _free_ports(2)
    base_url = f"http://127.0.0.1:{http_port}"

    ready: threading.Event = threading.Event()
    stop: threading.Event = threading.Event()
    exc: list[BaseException] = []
    stack: list[SimulatorStack] = []

    async def _serve() -> None:
        config = OpentronsFlexConfig(
            use_simulator=True,
            simulated_heater_shaker=True,
            with_robot_server=True,
            robot_server_tcp_port=http_port,
            sila_server=SiLAServerConfig(hostname="127.0.0.1", port=grpc_port, tls=False),
            cloud_server_endpoint=None,
            discovery=None,
        )
        gen = create_app(config)
        connector = await gen.__anext__()
        await connector.start()
        stack.append(
            SimulatorStack(
                http_url=base_url,
                grpc_address=connector.sila_server._address,
                protobuf=connector.sila_server.protobuf,
            )
        )
        ready.set()
        await asyncio.to_thread(stop.wait)
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()

    def _run() -> None:
        try:
            asyncio.run(_serve())
        except BaseException as e:
            exc.append(e)
            ready.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    if not ready.wait(timeout=60):
        raise TimeoutError("Simulator HTTP server did not start within 60 s")
    if exc:
        raise exc[0]

    # Poll until uvicorn is actually accepting connections.
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", http_port), timeout=1):
                break
        except OSError:
            time.sleep(1)

    # Poll until robot_server hardware init is complete (/health returns non-503).
    import httpx as _httpx

    for _ in range(30):
        try:
            r = _httpx.get(f"{base_url}/health", timeout=2)
            if r.status_code != 503:
                break
        except _httpx.TransportError:
            pass
        time.sleep(1)

    yield stack[0]

    stop.set()
    thread.join(timeout=15)


@pytest.fixture(scope="session")
def _simulator_http_url(simulator_stack: SimulatorStack | None) -> str | None:
    """HTTP base URL for the local simulator stack."""
    return simulator_stack.http_url if simulator_stack else None


@pytest.fixture(scope="session")
def robot_http_url(
    request: pytest.FixtureRequest,
    _simulator_http_url: str | None,
) -> str:
    """Base URL for the robot's opentrons HTTP API (port 31950).

    Derived from --robot-http HOST:PORT if given, otherwise from the host in
    --robot HOST:PORT with port fixed to 31950, or from the local simulator
    when --with-http-server is set.
    """
    explicit = request.config.getoption("--robot-http")
    if explicit:
        host_port = explicit
        host = host_port.split(":")[0]
        port = host_port.split(":")[1] if ":" in host_port else str(_HTTP_API_PORT)
        return f"http://{host}:{port}"

    robot = request.config.getoption("--robot")
    if robot:
        host = robot.split(":")[0]
        return f"http://{host}:{_HTTP_API_PORT}"

    if _simulator_http_url is not None:
        return _simulator_http_url

    pytest.skip("--robot-http, --robot, or --with-http-server required for HTTP API tests")


@pytest.fixture(scope="session")
def http_client(robot_http_url: str) -> httpx.Client:
    """Synchronous httpx client pre-configured for the robot's HTTP API.

    Session-scoped: one connection is shared across all HTTP API tests.
    """
    with httpx.Client(
        base_url=robot_http_url,
        headers={_HTTP_API_VERSION_HEADER: "*"},
        timeout=30.0,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def sila_channel(robot_address: str | None):
    """Yield (channel, pb).

    channel connects to the live robot when --robot is given, or to a local
    simulator otherwise.  pb (the protobuf codec) always comes from a local
    simulator because it is derived from the feature definitions, not the wire.
    """
    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_heater_shaker=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    gen = create_app(config)
    connector = await gen.__anext__()
    await connector.start()
    pb = connector.sila_server.protobuf
    address = robot_address or connector.sila_server._address

    channel = grpc.aio.insecure_channel(address)
    try:
        yield channel, pb
    finally:
        await channel.close()
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()
