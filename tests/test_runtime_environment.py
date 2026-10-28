"""Process-level behaviour for months of unattended operation."""

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path

import pytest

from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app, runtime_environment
from unitelabs.opentrons_flex.runtime_environment import (
    PERSISTENCE_ENV,
    LifecycleRecord,
    RobotEnvironmentError,
    apply_robot_server_persistence,
    quiet_noisy_loggers,
    remove_stale_robot_server_tempdirs,
    require_robot_environment,
    sd_notify,
    watchdog_heartbeat,
    watchdog_interval_s,
)


def test_live_hardware_requires_the_robot_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_environment, "robot_environment_detected", lambda: False)

    with pytest.raises(RobotEnvironmentError, match="RUNNING_ON_VERDIN"):
        require_robot_environment(use_simulator=False, required=True)
    require_robot_environment(use_simulator=True, required=True)
    require_robot_environment(use_simulator=False, required=False)


async def test_create_app_refuses_live_hardware_without_the_robot_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_environment, "robot_environment_detected", lambda: False)
    generator = create_app(OpentronsFlexConfig(use_simulator=False))
    with pytest.raises(RobotEnvironmentError):
        await generator.__anext__()


def test_persistence_directory_comes_from_config_or_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PERSISTENCE_ENV, raising=False)
    assert apply_robot_server_persistence(None).temporary

    monkeypatch.setenv(PERSISTENCE_ENV, "/data/opentrons_robot_server")
    info = apply_robot_server_persistence(None)
    assert not info.temporary
    assert info.directory == "/data/opentrons_robot_server"

    info = apply_robot_server_persistence("/data/custom")
    assert os.environ[PERSISTENCE_ENV] == "/data/custom"
    assert not info.temporary


def test_only_old_robot_server_temp_dirs_are_removed(tmp_path: Path) -> None:
    old = tmp_path / "opentrons-robot-server-old"
    new = tmp_path / "opentrons-robot-server-new"
    other = tmp_path / "unrelated-old"
    for path in (old, new, other):
        path.mkdir()
    past = time.time() - 3600
    os.utime(old, (past, past))
    os.utime(other, (past, past))

    removed = remove_stale_robot_server_tempdirs(root=str(tmp_path))

    assert removed == [str(old)]
    assert new.exists()
    assert other.exists()


def test_noisy_loggers_are_lowered() -> None:
    for name in ("sqlalchemy.engine", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.INFO)
    quiet_noisy_loggers(access_log=False)
    assert logging.getLogger("sqlalchemy.engine").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("uvicorn.access").getEffectiveLevel() == logging.WARNING

    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    quiet_noisy_loggers(access_log=True)
    assert logging.getLogger("uvicorn.access").getEffectiveLevel() == logging.INFO


def test_lifecycle_explains_an_unclean_previous_exit(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"

    first = LifecycleRecord(path)
    assert first.start().clean
    assert json.loads(path.read_text())["state"] == "running"

    crashed_restart = LifecycleRecord(path)
    previous = crashed_restart.start()
    assert not previous.clean
    assert "without an orderly shutdown" in previous.summary

    crashed_restart.stop("restarted to register THERMOCYCLER")
    after_clean_stop = LifecycleRecord(path).start()
    assert after_clean_stop.clean
    assert "register THERMOCYCLER" in after_clean_stop.summary


def test_lifecycle_without_a_path_is_memory_only() -> None:
    record = LifecycleRecord(None)
    assert record.start().clean
    record.stop("done")


def test_sd_notify_sends_datagrams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(address)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", address)
        assert sd_notify("READY=1")
        assert server.recv(64) == b"READY=1"
    finally:
        server.close()
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert not sd_notify("READY=1")


def test_watchdog_interval_is_half_of_watchdog_sec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog_interval_s() is None
    monkeypatch.setenv("WATCHDOG_USEC", "120000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert watchdog_interval_s() == 60.0
    monkeypatch.setenv("WATCHDOG_PID", "1")
    assert watchdog_interval_s() is None, "the watchdog belongs to another process"


async def test_heartbeat_pings_until_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(runtime_environment, "sd_notify", lambda message: sent.append(message) or True)
    task = asyncio.create_task(watchdog_heartbeat(0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sent.count("WATCHDOG=1") >= 2
