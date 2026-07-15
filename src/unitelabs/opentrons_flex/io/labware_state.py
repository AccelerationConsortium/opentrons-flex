"""Durable, fail-closed deck state for allowlisted labware movement."""

import json
import os
from collections.abc import Mapping
from pathlib import Path

from ._errors import (
    DestinationOccupiedError,
    DirectGripperControlDisabledError,
    LabwareMovementNotAllowedError,
)


class LabwareMovementState:
    """Persist location-to-labware identity and crash-safe movement validity."""

    def __init__(self, path: str | Path, initial_occupancy: Mapping[str, str]) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._load()
            if not self._clean_shutdown:
                self._valid = False
        else:
            self._occupancy = self._validate_occupancy(initial_occupancy, "initial occupancy")
            self._valid = True
        self._clean_shutdown = False
        self._persist()

    @property
    def valid(self) -> bool:
        """Whether the durable deck model can authorize a new plan."""
        return self._valid

    @property
    def occupancy(self) -> dict[str, str]:
        """Return a copy of the current location-to-labware mapping."""
        return dict(self._occupancy)

    def assert_direct_gripper_control_allowed(self, operation: str) -> None:
        """Reject a raw gripper actuation that would bypass the durable deck ledger."""
        msg = (
            f"Direct gripper operation {operation} is disabled while allowlisted labware movement is configured. "
            "Use LabwareMovementController or disable the local plan registry for maintenance mode."
        )
        raise DirectGripperControlDisabledError(msg)

    def validate_move(self, source: str, destination: str, labware_identifier: str) -> None:
        """Validate source identity and destination vacancy against durable state."""
        if not self._valid:
            msg = (
                "Durable deck state is invalid after an interrupted, failed, or uncleanly stopped move. "
                "Reconcile the physical deck and replace the local state ledger before restarting the connector."
            )
            raise LabwareMovementNotAllowedError(msg)
        actual = self._occupancy.get(source)
        if actual != labware_identifier:
            msg = (
                f"Durable deck state does not show labware at source {source!r}; expected "
                f"{labware_identifier!r}, but the recorded occupant is {actual!r}. "
                "Reconcile the physical deck before retrying."
            )
            raise LabwareMovementNotAllowedError(msg)
        if destination in self._occupancy:
            msg = (
                f"Destination {destination!r} is occupied by {self._occupancy[destination]!r} "
                "in durable deck state. Clear the destination and reconcile the state before retrying."
            )
            raise DestinationOccupiedError(msg)

    def begin_move(self) -> None:
        """Persist invalid state before the first physical actuation."""
        self._valid = False
        self._persist()

    def complete_move(self, source: str, destination: str, labware_identifier: str) -> None:
        """Atomically commit the new identity mapping after verified command completion."""
        candidate_occupancy = dict(self._occupancy)
        candidate_occupancy.pop(source, None)
        candidate_occupancy[destination] = labware_identifier
        try:
            self._persist_snapshot(
                valid=True,
                clean_shutdown=self._clean_shutdown,
                occupancy=candidate_occupancy,
            )
        except RuntimeError:
            # The physical move completed but its durable commit did not. The
            # prior in-memory occupancy remains non-authoritative and invalid.
            self._valid = False
            raise
        self._occupancy = candidate_occupancy
        self._valid = True

    def close(self) -> None:
        """Mark a clean connector shutdown without clearing an invalid deck state."""
        self._clean_shutdown = True
        self._persist()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                msg = "state ledger root must be a JSON object"
                raise ValueError(msg)
            valid = raw.get("valid")
            clean_shutdown = raw.get("clean_shutdown")
            if not isinstance(valid, bool) or not isinstance(clean_shutdown, bool):
                msg = "state ledger valid and clean_shutdown fields must be booleans"
                raise ValueError(msg)
            self._valid = valid
            self._clean_shutdown = clean_shutdown
            self._occupancy = self._validate_occupancy(raw.get("occupancy"), "state ledger occupancy")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            msg = f"Cannot load durable labware state ledger {self._path}: {exc}"
            raise ValueError(msg) from exc

    @staticmethod
    def _validate_occupancy(value: object, label: str) -> dict[str, str]:
        if not isinstance(value, Mapping):
            msg = f"{label} must be a location-to-labware JSON object."
            raise ValueError(msg)
        result: dict[str, str] = {}
        for location, labware in value.items():
            if not isinstance(location, str) or not location.strip():
                msg = f"{label} contains an invalid location identifier."
                raise ValueError(msg)
            if not isinstance(labware, str) or not labware.strip():
                msg = f"{label} contains an invalid labware identifier at {location!r}."
                raise ValueError(msg)
            result[location] = labware
        return result

    def _persist(self) -> None:
        self._persist_snapshot(
            valid=self._valid,
            clean_shutdown=self._clean_shutdown,
            occupancy=self._occupancy,
        )

    def _persist_snapshot(
        self,
        *,
        valid: bool,
        clean_shutdown: bool,
        occupancy: Mapping[str, str],
    ) -> None:
        payload = {
            "version": 1,
            "valid": valid,
            "clean_shutdown": clean_shutdown,
            "occupancy": dict(occupancy),
        }
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
            directory_flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
            directory_fd = os.open(self._path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            msg = f"Cannot persist durable labware state ledger {self._path}: {exc}"
            raise RuntimeError(msg) from exc


__all__ = ["LabwareMovementState"]
