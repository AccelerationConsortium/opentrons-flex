"""
Durable state for the step console: deck inventory, tip usage, liquid volumes, saved workflows, run history.

One SQLite file (``~/.unitelabs/opentrons-flex/console.db`` by default) keeps what the
robot cannot sense itself. The console updates it as steps succeed: a pickup marks tips
used, an aspirate takes volume out of the well the pipette last moved to, a dispense adds
it to the next well. The operator corrects it from the page whenever the deck changes.
"""

from __future__ import annotations

import datetime
import functools
import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".unitelabs" / "opentrons-flex" / "console.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS labware (
    slot TEXT PRIMARY KEY,
    load_name TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS used_tips (
    slot TEXT NOT NULL,
    well TEXT NOT NULL,
    used TEXT NOT NULL,
    PRIMARY KEY (slot, well)
);
CREATE TABLE IF NOT EXISTS liquids (
    slot TEXT NOT NULL,
    well TEXT NOT NULL,
    liquid TEXT NOT NULL,
    volume REAL NOT NULL,
    updated TEXT NOT NULL,
    PRIMARY KEY (slot, well)
);
CREATE TABLE IF NOT EXISTS workflows (
    name TEXT PRIMARY KEY,
    steps TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS console_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    params TEXT NOT NULL,
    ok INTEGER NOT NULL,
    message TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


@functools.cache
def labware_definition(load_name: str) -> dict[str, Any]:
    """Return the Opentrons shared-data definition of a labware load name (version 1)."""
    from opentrons_shared_data.labware import load_definition

    return dict(load_definition(load_name, 1))


class ConsoleStore:
    """Thread-safe access to the console database."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, tuple(args)))

    def _write(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self._lock, self._db:
            self._db.execute(sql, tuple(args))

    # -- deck inventory ------------------------------------------------------------

    def set_labware(self, slot: str, load_name: str, label: str = "") -> None:
        """Place ``load_name`` in ``slot``; a new item starts with fresh tips and no liquid recorded."""
        labware_definition(load_name)
        with self._lock, self._db:
            self._db.execute("DELETE FROM used_tips WHERE slot = ?", (slot,))
            self._db.execute("DELETE FROM liquids WHERE slot = ?", (slot,))
            self._db.execute(
                "INSERT OR REPLACE INTO labware (slot, load_name, label, updated) VALUES (?, ?, ?, ?)",
                (slot, load_name, label, _now()),
            )

    def remove_labware(self, slot: str) -> None:
        """Clear a slot and what was recorded about it."""
        with self._lock, self._db:
            for table in ("labware", "used_tips", "liquids"):
                self._db.execute(f"DELETE FROM {table} WHERE slot = ?", (slot,))

    def labware(self) -> dict[str, dict[str, Any]]:
        """Return slot -> {load_name, label, display_name, is_tiprack}."""
        result: dict[str, dict[str, Any]] = {}
        for row in self._rows("SELECT slot, load_name, label FROM labware ORDER BY slot"):
            definition = labware_definition(row["load_name"])
            result[row["slot"]] = {
                "load_name": row["load_name"],
                "label": row["label"],
                "display_name": definition["metadata"]["displayName"],
                "is_tiprack": bool(definition["parameters"]["isTiprack"]),
            }
        return result

    def labware_in(self, slot: str) -> str | None:
        """Return the load name recorded in ``slot``, if any."""
        rows = self._rows("SELECT load_name FROM labware WHERE slot = ?", (slot,))
        return rows[0]["load_name"] if rows else None

    # -- tips ----------------------------------------------------------------------

    def used_tips(self, slot: str) -> set[str]:
        """Return the wells already taken from the rack in ``slot``."""
        return {row["well"] for row in self._rows("SELECT well FROM used_tips WHERE slot = ?", (slot,))}

    def take_tips(self, slot: str, wells: Iterable[str]) -> None:
        """Mark wells of the rack in ``slot`` as used."""
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO used_tips (slot, well, used) VALUES (?, ?, ?)",
                [(slot, well, _now()) for well in wells],
            )

    def refill_tips(self, slot: str) -> None:
        """Forget the used tips of ``slot``: a full rack is in place."""
        self._write("DELETE FROM used_tips WHERE slot = ?", (slot,))

    # -- liquids -------------------------------------------------------------------

    def set_liquid(self, slot: str, well: str, liquid: str, volume: float) -> None:
        """Record ``volume`` uL of ``liquid`` in a well; zero or less clears it."""
        if volume <= 0:
            self._write("DELETE FROM liquids WHERE slot = ? AND well = ?", (slot, well))
            return
        self._write(
            "INSERT OR REPLACE INTO liquids (slot, well, liquid, volume, updated) VALUES (?, ?, ?, ?, ?)",
            (slot, well, liquid, float(volume), _now()),
        )

    def liquid(self, slot: str, well: str) -> tuple[str, float] | None:
        """Return (liquid, volume) recorded in a well, if any."""
        rows = self._rows("SELECT liquid, volume FROM liquids WHERE slot = ? AND well = ?", (slot, well))
        return (rows[0]["liquid"], float(rows[0]["volume"])) if rows else None

    def remove_volume(self, slot: str, well: str, volume: float) -> str | None:
        """Take ``volume`` out of a tracked well; return the liquid's name, or None when the well is untracked."""
        current = self.liquid(slot, well)
        if current is None:
            return None
        name, held = current
        self.set_liquid(slot, well, name, max(0.0, held - volume))
        return name

    def add_volume(self, slot: str, well: str, liquid: str, volume: float) -> None:
        """Add ``volume`` uL of ``liquid`` to a well, naming a mixture when liquids differ."""
        current = self.liquid(slot, well)
        if current is None:
            self.set_liquid(slot, well, liquid, volume)
            return
        name, held = current
        self.set_liquid(slot, well, name if name == liquid else f"{name} + {liquid}", held + volume)

    def liquids(self) -> list[dict[str, Any]]:
        """Return every tracked well."""
        return [
            {"slot": row["slot"], "well": row["well"], "liquid": row["liquid"], "volume": float(row["volume"])}
            for row in self._rows("SELECT slot, well, liquid, volume FROM liquids ORDER BY slot, well")
        ]

    # -- workflows -----------------------------------------------------------------

    def save_workflow(self, name: str, steps: list[Mapping[str, Any]], source: str = "") -> None:
        """Save (or replace) a named step list."""
        self._write(
            "INSERT OR REPLACE INTO workflows (name, steps, source, updated) VALUES (?, ?, ?, ?)",
            (name, json.dumps(list(steps)), source, _now()),
        )

    def workflows(self) -> list[dict[str, Any]]:
        """List saved workflows, newest first."""
        return [
            {
                "name": row["name"],
                "source": row["source"],
                "updated": row["updated"],
                "steps": len(json.loads(row["steps"])),
            }
            for row in self._rows("SELECT name, steps, source, updated FROM workflows ORDER BY updated DESC")
        ]

    def workflow(self, name: str) -> list[dict[str, Any]] | None:
        """Return the steps of a saved workflow, if it exists."""
        rows = self._rows("SELECT steps FROM workflows WHERE name = ?", (name,))
        return json.loads(rows[0]["steps"]) if rows else None

    def delete_workflow(self, name: str) -> None:
        """Delete a saved workflow."""
        self._write("DELETE FROM workflows WHERE name = ?", (name,))

    # -- console state that must survive a restart ----------------------------------

    def get_flag(self, key: str) -> bool:
        """Return a stored yes/no fact, such as whether the gripper may hold a plate."""
        rows = self._rows("SELECT value FROM console_state WHERE key = ?", (key,))
        return bool(rows) and rows[0]["value"] == "1"

    def set_flag(self, key: str, value: bool) -> None:
        """Store a yes/no fact."""
        self._write("INSERT OR REPLACE INTO console_state (key, value) VALUES (?, ?)", (key, "1" if value else "0"))

    # -- history -------------------------------------------------------------------

    def log(self, kind: str, params: Mapping[str, Any], ok: bool, message: str) -> None:
        """Append one executed step to the run history."""
        self._write(
            "INSERT INTO history (at, kind, params, ok, message) VALUES (?, ?, ?, ?, ?)",
            (_now(), kind, json.dumps(dict(params), default=str), int(ok), message),
        )

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the latest run-history entries, newest first."""
        return [
            {"at": row["at"], "kind": row["kind"], "ok": bool(row["ok"]), "message": row["message"]}
            for row in self._rows("SELECT at, kind, ok, message FROM history ORDER BY id DESC LIMIT ?", (limit,))
        ]

    def snapshot(self) -> dict[str, Any]:
        """Everything the inventory view shows."""
        labware = self.labware()
        tips = {slot: sorted(self.used_tips(slot)) for slot, item in labware.items() if item["is_tiprack"]}
        return {"labware": labware, "used_tips": tips, "liquids": self.liquids()}


__all__ = ["DEFAULT_PATH", "ConsoleStore", "labware_definition"]
