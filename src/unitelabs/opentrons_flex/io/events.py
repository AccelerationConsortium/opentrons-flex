"""
A bounded, in-memory journal of what happened to the connector.

Operators and orchestration software read it to understand a state change after
the fact (which command failed and why, when a halt happened, what a recovery
did) without access to the robot's system journal. Every entry is also logged.
"""

from __future__ import annotations

import collections
import datetime
import enum
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_EVENTS = 100
REPEAT_WINDOW_S = 600.0


class EventKind(enum.Enum):
    """Category of a connector event."""

    STATE = "STATE"
    ERROR = "ERROR"
    HALT = "HALT"
    RECOVERY = "RECOVERY"
    MODULE = "MODULE"
    LIFECYCLE = "LIFECYCLE"


@dataclass(frozen=True)
class ConnectorEvent:
    """One journal entry."""

    sequence: int
    time: datetime.datetime
    kind: EventKind
    source: str
    message: str


class EventLog:
    """Thread-safe ring buffer of the most recent connector events."""

    def __init__(self, capacity: int = MAX_EVENTS, *, repeat_window_s: float = REPEAT_WINDOW_S) -> None:
        """Keep at most ``capacity`` events; identical events within ``repeat_window_s`` are folded."""
        self._events: collections.deque[ConnectorEvent] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._sequence = 0
        self._last_error: ConnectorEvent | None = None
        self._repeat_window_s = repeat_window_s
        self._recent: dict[tuple[EventKind, str, str], tuple[float, ConnectorEvent, int]] = {}

    def record(self, kind: EventKind, source: str, message: str) -> ConnectorEvent:
        """
        Append one event and log it at a level matching its kind.

        A status poll that keeps failing (for example while a module is unplugged)
        would otherwise add the same error several times per second. Identical
        errors within the repeat window are counted instead, and the count is
        reported on the next occurrence after the window. Other kinds of events
        (state changes, halts, recovery steps) are always recorded.
        """
        key = (kind, source, message)
        now = time.monotonic()
        with self._lock:
            previous = self._recent.get(key) if kind is EventKind.ERROR else None
            still_listed = previous is not None and previous[1].sequence > self._sequence - len(self._events)
            if previous is not None and still_listed and now - previous[0] < self._repeat_window_s:
                self._recent[key] = (previous[0], previous[1], previous[2] + 1)
                return previous[1]
            repeats = previous[2] if previous is not None else 0
            if repeats:
                message = f"{message} (repeated {repeats} more time{'s' if repeats != 1 else ''})"
            if len(self._recent) > 4 * MAX_EVENTS:
                self._recent = {k: v for k, v in self._recent.items() if now - v[0] < self._repeat_window_s}
            self._sequence += 1
            event = ConnectorEvent(
                sequence=self._sequence,
                time=datetime.datetime.now(datetime.UTC),
                kind=kind,
                source=source,
                message=message,
            )
            self._events.append(event)
            self._recent[key] = (now, event, 0)
            if kind in (EventKind.ERROR, EventKind.HALT):
                self._last_error = event
        level = logging.WARNING if kind in (EventKind.ERROR, EventKind.HALT) else logging.INFO
        log.log(level, "[%s] %s: %s", kind.value, source, message)
        return event

    def record_error(self, source: str, error: BaseException) -> ConnectorEvent:
        """Record a failed command or hardware call."""
        detail = str(error).strip() or type(error).__name__
        return self.record(EventKind.ERROR, source, f"{type(error).__name__}: {detail}")

    def recent(self, count: int = MAX_EVENTS) -> list[ConnectorEvent]:
        """Return the newest ``count`` events, oldest first."""
        with self._lock:
            return list(self._events)[-count:]

    @property
    def last_error(self) -> ConnectorEvent | None:
        """The most recent error or halt, if any."""
        with self._lock:
            return self._last_error

    @property
    def sequence(self) -> int:
        """Sequence number of the newest event (changes whenever an event is added)."""
        with self._lock:
            return self._sequence

    def clear(self) -> None:
        """Forget every event (tests)."""
        with self._lock:
            self._events.clear()
            self._recent.clear()
            self._last_error = None


EVENTS = EventLog()
"""The process-wide event journal."""


def describe_error(event: ConnectorEvent | None) -> str:
    """One-line description of an error event, empty when there is none."""
    if event is None:
        return ""
    stamp = event.time.isoformat(timespec="seconds")
    return f"{stamp} {event.source}: {event.message}"


__all__ = ["EVENTS", "MAX_EVENTS", "ConnectorEvent", "EventKind", "EventLog", "describe_error"]
