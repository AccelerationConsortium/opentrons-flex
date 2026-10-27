"""
Process-level concerns for running the connector unattended for months on a Flex.

* The Opentrons libraries decide whether they run on a robot from environment
  variables set by the stock systemd unit (``RUNNING_ON_VERDIN``). Without them,
  calibration data is read from ``~/.opentrons`` instead of ``/data``, so live
  hardware startup is refused unless the robot environment is detected.
* The embedded robot-server keeps protocols, runs, labware offsets, and the deck
  configuration in its persistence directory. Without one it creates a new
  temporary directory on every start, losing that data and leaking ``/tmp``.
* Long-lived processes must not flood the journal (SQL echo, HTTP access lines).
* ``sd_notify`` readiness and watchdog heartbeats let systemd restart a hung
  connector, and a small lifecycle record explains the previous exit after a restart.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import datetime
import json
import logging
import os
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PERSISTENCE_ENV = "OT_ROBOT_SERVER_persistence_directory"
_TEMPORARY_PERSISTENCE = "automatically_make_temporary"
_ROBOT_SERVER_TEMP_PREFIXES = ("opentrons-robot-server-",)
_NOISY_LOGGERS = ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool")


class RobotEnvironmentError(RuntimeError):
    """The process does not see the Opentrons robot environment needed for live hardware."""


def robot_environment_detected() -> bool:
    """Whether the Opentrons libraries consider this process to be running on a robot."""
    from opentrons.config import IS_ROBOT

    return bool(IS_ROBOT)


def require_robot_environment(*, use_simulator: bool, required: bool) -> None:
    """
    Refuse to drive real hardware without the robot environment.

    Raises:
        RobotEnvironmentError: With the variables to set and the consequence of not setting them.
    """
    if use_simulator or not required or robot_environment_detected():
        return
    from opentrons.config import infer_config_base_dir

    message = (
        "The Opentrons robot environment is not set for this process (RUNNING_ON_VERDIN is missing), so "
        f"calibration data would be read from {infer_config_base_dir()} instead of /data and every absolute "
        "move would ignore the robot's deck, pipette, and gripper calibration. Reinstall the service with "
        "scripts/install_connector_service.sh (it copies the stock robot-server environment), or set "
        "require_robot_environment to false only for a bench test without calibration."
    )
    raise RobotEnvironmentError(message)


@dataclass(frozen=True)
class PersistenceInfo:
    """Where the embedded robot-server keeps its data."""

    directory: str
    temporary: bool


def apply_robot_server_persistence(directory: str | None) -> PersistenceInfo:
    """
    Point the embedded robot-server at a durable data directory.

    An explicit ``directory`` wins; otherwise the inherited environment (normally
    copied from the stock robot-server unit) is used.
    """
    if directory:
        os.environ[PERSISTENCE_ENV] = directory
    current = os.environ.get(PERSISTENCE_ENV, "")
    temporary = current in ("", _TEMPORARY_PERSISTENCE)
    if temporary:
        log.warning(
            "The embedded robot-server has no persistence directory; protocols, run history, labware offsets, "
            "and the deck configuration will be lost on every connector restart. Set %s (normally inherited "
            "from the stock opentrons-robot-server unit) or robot_server_persistence_directory.",
            PERSISTENCE_ENV,
        )
    else:
        log.info("Embedded robot-server data directory: %s", current)
    return PersistenceInfo(directory=current or _TEMPORARY_PERSISTENCE, temporary=temporary)


def remove_stale_robot_server_tempdirs(*, older_than_s: float = 60.0, root: str | None = None) -> list[str]:
    """
    Delete temporary robot-server data directories left by earlier connector runs.

    Only directories with robot-server's temporary prefix that are older than
    ``older_than_s`` are removed, so a directory created by this process is kept.
    """
    base = Path(root or tempfile.gettempdir())
    cutoff = time.time() - older_than_s
    removed: list[str] = []
    for prefix in _ROBOT_SERVER_TEMP_PREFIXES:
        for path in base.glob(f"{prefix}*"):
            try:
                if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path)
                    removed.append(str(path))
            except OSError as exc:
                log.warning("Could not remove stale robot-server directory %s: %s", path, exc)
    if removed:
        log.info("Removed %d stale robot-server temporary directories.", len(removed))
    return removed


def quiet_noisy_loggers(*, access_log: bool) -> None:
    """
    Keep months of unattended operation readable and gentle on the robot's flash.

    robot-server's own logging setup enables SQLAlchemy statement logging at INFO
    (one line per SQL statement on every touchscreen poll), and uvicorn logs one
    access line per HTTP request. Both are lowered to WARNING; errors still appear.
    """
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if not access_log:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# ------------------------------------------------------------------ systemd


def sd_notify(message: str) -> bool:
    """Send one ``sd_notify`` message; a no-op (False) when not started by systemd with notify access."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
    except OSError as exc:
        log.debug("sd_notify failed: %s", exc)
        return False
    return True


def watchdog_interval_s() -> float | None:
    """Half of systemd's WatchdogSec for this process, or None when the watchdog is off."""
    raw = os.environ.get("WATCHDOG_USEC")
    pid = os.environ.get("WATCHDOG_PID")
    if not raw or (pid and pid != str(os.getpid())):
        return None
    try:
        return max(int(raw) / 1_000_000 / 2, 0.5)
    except ValueError:
        return None


async def watchdog_heartbeat(interval_s: float | None = None) -> None:
    """
    Tell systemd the event loop is alive until cancelled.

    The heartbeat runs on the event loop itself, so a blocked or deadlocked loop
    stops it and systemd restarts the connector after WatchdogSec.
    """
    interval = interval_s if interval_s is not None else watchdog_interval_s()
    if interval is None:
        return
    log.info("systemd watchdog heartbeat every %.1f s", interval)
    while True:
        sd_notify("WATCHDOG=1")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def systemd_service(status: str) -> collections.abc.AsyncIterator[None]:
    """Report READY with ``status`` and keep the watchdog fed while the context is open."""
    sd_notify(f"READY=1\nSTATUS={status}")
    heartbeat = asyncio.create_task(watchdog_heartbeat(), name="flex-systemd-watchdog")
    try:
        yield
    finally:
        sd_notify("STOPPING=1")
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


# ---------------------------------------------------------------- lifecycle


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PreviousExit:
    """How the previous connector process ended, as recorded on disk."""

    summary: str
    clean: bool


class LifecycleRecord:
    """
    Remember why the connector last stopped, so a restart is never unexplained.

    On start the previous record is read and a new "running" record is written.
    A record still marked running at the next start means the previous process
    ended without an orderly shutdown (crash, power loss, or a watchdog kill).
    """

    def __init__(self, path: str | Path | None) -> None:
        """Bind to ``path``; ``None`` keeps everything in memory only."""
        self._path = Path(path).expanduser() if path else None
        self.previous = PreviousExit(summary="", clean=True)
        self.started_at = _now()

    def start(self) -> PreviousExit:
        """Read the previous record and mark this process as running."""
        previous: dict[str, object] = {}
        if self._path is not None and self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                previous = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError) as exc:
                log.warning("Unreadable lifecycle record %s: %s", self._path, exc)
        if previous.get("state") == "running":
            self.previous = PreviousExit(
                summary=(
                    f"The connector started at {previous.get('started_at', 'an unknown time')} ended without an "
                    "orderly shutdown (crash, power loss, or systemd watchdog restart); check journalctl -u "
                    "sila2-connector for that time."
                ),
                clean=False,
            )
        elif previous.get("state") == "stopped":
            self.previous = PreviousExit(
                summary=f"Stopped at {previous.get('stopped_at', '?')}: {previous.get('reason', 'orderly shutdown')}",
                clean=True,
            )
        if not self.previous.clean:
            log.warning("%s", self.previous.summary)
        elif self.previous.summary:
            log.info("Previous run: %s", self.previous.summary)
        self._write({"state": "running", "started_at": self.started_at, "pid": os.getpid()})
        return self.previous

    def stop(self, reason: str) -> None:
        """Record an orderly stop and its reason."""
        self._write({"state": "stopped", "started_at": self.started_at, "stopped_at": _now(), "reason": reason})

    def _write(self, payload: dict[str, object]) -> None:
        if self._path is None:
            return
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(self._path)
        except OSError as exc:
            log.warning("Could not write lifecycle record %s: %s", self._path, exc)


__all__ = [
    "PERSISTENCE_ENV",
    "LifecycleRecord",
    "PersistenceInfo",
    "PreviousExit",
    "RobotEnvironmentError",
    "apply_robot_server_persistence",
    "quiet_noisy_loggers",
    "remove_stale_robot_server_tempdirs",
    "require_robot_environment",
    "robot_environment_detected",
    "sd_notify",
    "systemd_service",
    "watchdog_heartbeat",
    "watchdog_interval_s",
]
