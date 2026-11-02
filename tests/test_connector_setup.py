"""Tests for configuration defaults, startup supervision, and the eager run-store initialization."""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unitelabs.opentrons_flex import (
    SILA_SERVER_VERSION,
    OpentronsFlexConfig,
    _RobotServerWatchdog,
    _simulator_attached_instruments,
    _simulator_attached_modules,
    runtime_preflight,
)
from unitelabs.opentrons_flex.io.robot_status import HttpServerState

_ROOT = Path(__file__).parents[1]

# ------------------------------------------------------------------ configuration


@pytest.mark.parametrize(
    "path",
    ["config/flex_config.json", "config/flex_unit_operations.json", "config/smoketest_config.json"],
)
def test_shipped_configs_take_their_version_from_the_installed_package(path: str) -> None:
    raw = json.loads((_ROOT / path).read_text())
    assert "version" not in raw["sila_server"]

    config = OpentronsFlexConfig.from_dict(raw)

    assert config.sila_server.version == SILA_SERVER_VERSION


def test_configs_without_server_identity_get_flex_defaults() -> None:
    config = OpentronsFlexConfig.validate({"sila_server": {"hostname": "127.0.0.1", "port": 0}})

    assert config.sila_server.name == "Opentrons Flex"
    assert config.sila_server.type == "LiquidHandler"
    assert config.sila_server.version == SILA_SERVER_VERSION


def test_explicit_server_identity_is_kept() -> None:
    config = OpentronsFlexConfig.validate({"sila_server": {"name": "Bench 3 Flex", "version": "0.9.1"}})

    assert config.sila_server.name == "Bench 3 Flex"
    assert config.sila_server.version == "0.9.1"


def test_simulated_modules_list_and_legacy_flags_are_equivalent() -> None:
    from_list = OpentronsFlexConfig(use_simulator=True, simulated_modules=["heater_shaker", "gripper"])
    from_flags = OpentronsFlexConfig(use_simulator=True, simulated_heater_shaker=True, simulated_gripper=True)

    assert _simulator_attached_modules(from_list).keys() == _simulator_attached_modules(from_flags).keys()
    assert _simulator_attached_instruments(from_list).keys() == _simulator_attached_instruments(from_flags).keys()


def test_unknown_simulated_module_is_rejected_with_the_valid_choices() -> None:
    with pytest.raises(ValueError, match="heater_shaker"):
        OpentronsFlexConfig(use_simulator=True, simulated_modules=["magnetic_module"])


async def test_simulated_module_list_is_rejected_in_live_mode() -> None:
    from unitelabs.opentrons_flex import create_app

    generator = create_app(OpentronsFlexConfig(use_simulator=False, simulated_modules=["thermocycler"]))
    with pytest.raises(ValueError, match=r"simulated_modules\[thermocycler\]"):
        await generator.__anext__()


def test_preflight_accepts_an_omitted_version_and_rejects_a_stale_one(tmp_path: Path) -> None:
    issues = runtime_preflight._configuration_issues(
        {"sila_server": {}},
        connector_version="1.2.3",
        require_robot_server=False,
        require_sila_only=False,
        require_live_hardware=False,
    )
    assert issues == ()

    stale = runtime_preflight._configuration_issues(
        {"sila_server": {"version": "1.2.2"}},
        connector_version="1.2.3",
        require_robot_server=False,
        require_sila_only=False,
        require_live_hardware=False,
    )
    assert len(stale) == 1
    assert "Remove sila_server.version" in stale[0]


# ------------------------------------------------------------------- watchdog


async def test_watchdog_restarts_the_connector_when_robot_server_dies() -> None:
    server = MagicMock(should_exit=False)
    terminated = asyncio.Event()
    watchdog = _RobotServerWatchdog(server, grace_s=0.01, terminate=terminated.set)
    assert watchdog.state() is HttpServerState.STARTING

    async def crash() -> None:
        raise OSError("socket closed")

    task = asyncio.create_task(crash())
    watchdog.watch(task)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(terminated.wait(), timeout=1)

    assert watchdog.failed
    assert watchdog.terminated
    assert watchdog.state() is HttpServerState.STOPPED


async def test_watchdog_ignores_an_orderly_shutdown() -> None:
    server = MagicMock(should_exit=False)
    terminate = MagicMock()
    watchdog = _RobotServerWatchdog(server, grace_s=0.01, terminate=terminate)

    task = asyncio.create_task(asyncio.sleep(0))
    watchdog.watch(task)
    assert watchdog.state() is HttpServerState.RUNNING
    watchdog.shutting_down = True
    server.should_exit = True
    await task
    await asyncio.sleep(0.05)

    assert not watchdog.failed
    terminate.assert_not_called()


async def test_watchdog_cancels_a_pending_restart_when_shutdown_starts() -> None:
    server = MagicMock(should_exit=False)
    terminate = MagicMock()
    watchdog = _RobotServerWatchdog(server, grace_s=0.05, terminate=terminate)

    task = asyncio.create_task(asyncio.sleep(0))
    watchdog.watch(task)
    await task
    await asyncio.sleep(0)
    assert watchdog.failed
    watchdog.shutting_down = True
    await asyncio.sleep(0.1)

    terminate.assert_not_called()
    assert not watchdog.terminated
