"""
Detect modules plugged in after connector startup and register them by restarting when idle.

SiLA features are fixed once the gRPC server starts, so a module type that was
not attached at startup cannot gain a feature in place. Re-plugging a module of a
type that *is* registered needs nothing: its controller rebinds automatically.
For a new type, this monitor waits until the module has been present for a few
seconds and the robot is idle, then asks the connector to restart so the next
startup registers the feature. It never restarts during a run, an active
command, or while duplicate modules make the inventory ambiguous.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from collections.abc import Callable, Iterable

log = logging.getLogger(__name__)


class ModuleHotPlugMonitor:
    """Poll the module inventory and request a restart for newly attached module types."""

    def __init__(
        self,
        *,
        registered_types: Iterable[str],
        attached: Callable[[], Iterable[tuple[str, str]]],
        idle: Callable[[], bool],
        restart: Callable[[str], None] | None,
        stable_s: float = 5.0,
        interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Configure the monitor.

        Args:
            registered_types: Module types that already have SiLA features.
            attached: Returns ``(type, serial)`` for every supported attached module.
            idle: Whether a restart would interrupt nothing (no run, no command, not starting).
            restart: Called with a reason to restart; ``None`` only reports (``module_hot_plug: report``).
            stable_s: Seconds a new module must stay attached before acting (debounces USB re-enumeration).
            interval_s: Polling interval.
            clock: Monotonic time source (injectable for tests).
        """
        self._registered = set(registered_types)
        self._attached = attached
        self._idle = idle
        self._restart = restart
        self._stable_s = stable_s
        self._interval_s = interval_s
        self._clock = clock
        self._first_seen: dict[str, float] = {}
        self._waiting_logged: set[str] = set()
        self.requested_reason: str | None = None

    @property
    def pending_types(self) -> tuple[str, ...]:
        """New module types currently attached but not registered."""
        return tuple(sorted(self._first_seen))

    def check(self) -> str | None:
        """Run one inspection; return the restart reason if a restart was requested."""
        if self.requested_reason is not None:
            return self.requested_reason
        try:
            counts = collections.Counter(module_type for module_type, _serial in self._attached())
        except Exception:  # noqa: BLE001 - an unreadable inventory is simply retried
            return None
        new_types = {module_type for module_type in counts if module_type not in self._registered}
        for module_type in list(self._first_seen):
            if module_type not in new_types:
                del self._first_seen[module_type]
        if not new_types:
            self._waiting_logged.clear()
        now = self._clock()
        for module_type in new_types:
            if module_type not in self._first_seen:
                self._first_seen[module_type] = now
                log.info("New %s module attached after startup.", module_type)
        if not new_types:
            return None
        duplicates = sorted(module_type for module_type, count in counts.items() if count > 1)
        if duplicates:
            self._log_once("duplicate", f"Not restarting: several modules of type {', '.join(duplicates)} attached.")
            return None
        if any(now - self._first_seen[module_type] < self._stable_s for module_type in new_types):
            return None
        reason = f"register newly attached module type(s) {', '.join(sorted(new_types))}"
        if self._restart is None:
            self._log_once("report", f"Restart the connector to {reason} (module_hot_plug is 'report').")
            return None
        if not self._idle():
            self._log_once("busy", f"Waiting for the robot to become idle to {reason}.")
            return None
        self.requested_reason = reason
        self._restart(reason)
        return reason

    async def run(self) -> None:
        """Poll until cancelled or a restart has been requested."""
        while self.requested_reason is None:
            self.check()
            await asyncio.sleep(self._interval_s)

    def _log_once(self, key: str, message: str) -> None:
        if key not in self._waiting_logged:
            self._waiting_logged.add(key)
            log.warning(message)


__all__ = ["ModuleHotPlugMonitor"]
