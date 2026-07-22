"""Controlled, resource-aware Protocol Engine run mutation."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal

from opentrons.protocol_engine import commands as pe_commands
from opentrons.protocol_engine.errors import (
    AddressableAreaDoesNotExistError,
    IncompatibleAddressableAreaError,
)
from opentrons.protocol_engine.state.tips import TipView
from opentrons.protocol_engine.types import MotorAxis, TipRackWellState
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, PositiveInt, field_validator

from .io.run_authority import RunMutationGate

MUTATION_CHECKPOINT_PREFIX = "UNITELABS_MUTATION_CHECKPOINT:"
_LEDGER_COMMAND_BATCH_SIZE = 100
PositiveFiniteFloat = Annotated[FiniteFloat, Field(gt=0)]


class MutationError(RuntimeError):
    """Base class for controlled mutation failures."""


class MutationNotAllowedError(MutationError):
    """The run is not at an authoritative mutation point."""


class MutationValidationError(MutationError):
    """The proposed steps exceed or conflict with authoritative resources."""


class MutationLedgerError(MutationError):
    """The append-only mutation ledger cannot prove its integrity."""


class _MutationModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class WellReference(_MutationModel):
    """A well in currently loaded Protocol Engine labware."""

    labware_id: str = Field(alias="labwareId", min_length=1)
    well_name: str = Field(alias="wellName", min_length=1)


class LabwareDisposal(WellReference):
    """Discard a tip into a loaded non-tip-rack labware well."""

    disposal_type: Literal["labware"] = Field(alias="disposalType")


class AddressableAreaDisposal(_MutationModel):
    """Discard a tip into a loaded Flex trash or waste-chute area."""

    disposal_type: Literal["addressableArea"] = Field(alias="disposalType")
    addressable_area_name: str = Field(alias="addressableAreaName", min_length=1)


DisposalReference = Annotated[
    LabwareDisposal | AddressableAreaDisposal,
    Field(discriminator="disposal_type"),
]


class TransferStep(_MutationModel):
    """Pick up a fresh tip, transfer liquid, and discard the tip."""

    step_type: Literal["transfer"] = Field(alias="stepType")
    pipette_id: str = Field(alias="pipetteId", min_length=1)
    tip_rack_ids: list[str] = Field(alias="tipRackIds", min_length=1)
    source: WellReference
    destination: WellReference
    disposal: DisposalReference
    volume: PositiveFiniteFloat
    aspirate_flow_rate: PositiveFiniteFloat = Field(alias="aspirateFlowRate")
    dispense_flow_rate: PositiveFiniteFloat = Field(alias="dispenseFlowRate")


class MixStep(_MutationModel):
    """Pick up a fresh tip, mix one tracked well, and discard the tip."""

    step_type: Literal["mix"] = Field(alias="stepType")
    pipette_id: str = Field(alias="pipetteId", min_length=1)
    tip_rack_ids: list[str] = Field(alias="tipRackIds", min_length=1)
    target: WellReference
    disposal: DisposalReference
    volume: PositiveFiniteFloat
    repetitions: PositiveInt = Field(le=100)
    aspirate_flow_rate: PositiveFiniteFloat = Field(alias="aspirateFlowRate")
    dispense_flow_rate: PositiveFiniteFloat = Field(alias="dispenseFlowRate")


class DelayStep(_MutationModel):
    """Wait for a bounded duration inside Protocol Engine."""

    step_type: Literal["delay"] = Field(alias="stepType")
    seconds: PositiveFiniteFloat = Field(le=86400)
    message: str = Field(min_length=1, max_length=500)


class CommentStep(_MutationModel):
    """Add an operator-visible Protocol Engine comment."""

    step_type: Literal["comment"] = Field(alias="stepType")
    message: str = Field(min_length=1, max_length=500)


class RecoveryDropTipStep(_MutationModel):
    """Drop an attached tip into loaded disposal labware during recovery."""

    step_type: Literal["recoveryDropTip"] = Field(alias="stepType")
    pipette_id: str = Field(alias="pipetteId", min_length=1)
    disposal: DisposalReference


class RecoveryHomeStep(_MutationModel):
    """Home explicit Flex motor axes during Protocol Engine recovery."""

    step_type: Literal["recoveryHome"] = Field(alias="stepType")
    axes: list[MotorAxis] = Field(min_length=1)


MutationStep = Annotated[
    TransferStep | MixStep | DelayStep | CommentStep | RecoveryDropTipStep | RecoveryHomeStep,
    Field(discriminator="step_type"),
]


class MutationRequest(_MutationModel):
    """One idempotent, operator-attributed run mutation request."""

    mutation_id: str = Field(alias="mutationId")
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    mode: Literal["checkpoint", "recovery"]
    steps: list[MutationStep] = Field(min_length=1, max_length=100)

    @field_validator("mutation_id")
    @classmethod
    def _valid_mutation_id(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            message = "mutationId must be a UUID."
            raise ValueError(message) from exc
        return value


class MutationGateReleaseRequest(_MutationModel):
    """Audited acknowledgement that discards a rejected proposal."""

    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class MutationSnapshot(_MutationModel):
    """Authoritative Protocol Engine state exposed for mutation planning."""

    run_id: str = Field(alias="runId")
    captured_at: datetime = Field(alias="capturedAt")
    fingerprint: str
    status: str
    current_command: dict[str, Any] | None = Field(alias="currentCommand")
    commands: list[dict[str, Any]]
    pipettes: list[dict[str, Any]]
    tip_racks: dict[str, dict[str, str]] = Field(alias="tipRacks")
    labware: list[dict[str, Any]]
    modules: list[dict[str, Any]]
    disposal_areas: list[dict[str, Any]] = Field(alias="disposalAreas")
    wells: list[dict[str, Any]]


class MutationResult(_MutationModel):
    """A mutation that was validated and enqueued while the run stayed paused."""

    mutation_id: str = Field(alias="mutationId")
    run_id: str = Field(alias="runId")
    status: Literal["enqueued"]
    checkpoint_command_id: str = Field(alias="checkpointCommandId")
    command_ids: list[str] = Field(alias="commandIds")
    resource_snapshot_before: str = Field(alias="resourceSnapshotBefore")
    predicted_well_volumes: dict[str, float] = Field(alias="predictedWellVolumes")
    allocated_tips: list[dict[str, Any]] = Field(alias="allocatedTips")


class MutationLedger:
    """Append-only, fsynced, hash-chained JSON Lines mutation ledger."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(mode=0o600, exist_ok=True)
        self._path.chmod(0o600)
        self._io_lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._load_and_verify()
        self._capture_disk_state()

    @property
    def path(self) -> Path:
        """Return the configured durable ledger path."""
        return self._path

    def append(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append one hash-linked event and make it durable before returning."""
        with self._io_lock:
            previous_hash = self._records[-1]["recordHash"] if self._records else "GENESIS"
            record: dict[str, Any] = {
                "version": 1,
                "sequence": len(self._records) + 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "previousHash": previous_hash,
                **_jsonable(payload),
            }
            record["recordHash"] = _record_hash(record)
            encoded = (_canonical_json(record) + "\n").encode("utf-8")
            try:
                with self._path.open("r+b") as stream:
                    self._assert_incremental_integrity(stream)
                    stream.seek(0, os.SEEK_END)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stat_result = os.fstat(stream.fileno())
            except OSError as exc:
                message = f"Cannot append durable run-mutation ledger {self._path}: {exc}"
                raise MutationLedgerError(message) from exc
            self._records.append(record)
            self._capture_stat(stat_result, encoded)
            return dict(record)

    def records_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return a copy of every audit event for one run."""
        return [dict(record) for record in self._records if record.get("runId") == run_id]

    def latest_for_mutation(self, run_id: str, mutation_id: str) -> dict[str, Any] | None:
        """Return the latest event for an idempotency key."""
        for record in reversed(self._records):
            if record.get("runId") == run_id and record.get("mutationId") == mutation_id:
                return dict(record)
        return None

    def completed_mutation_at_checkpoint(self, run_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        """Return the completed mutation batch for a checkpoint, if one exists."""
        for record in reversed(self._records):
            result = record.get("result")
            if (
                record.get("event") == "mutation_enqueued"
                and record.get("runId") == run_id
                and isinstance(result, dict)
                and result.get("checkpointCommandId") == checkpoint_id
            ):
                return dict(record)
        return None

    def unresolved_holds(self) -> dict[str, tuple[str, bool]]:
        """Replay the durable ledger into unresolved per-run mutation holds."""
        holds: dict[str, tuple[str, bool]] = {}
        for record in self._records:
            run_id = record.get("runId")
            if not isinstance(run_id, str):
                continue
            event = record.get("event")
            if event == "mutation_requested":
                holds[run_id] = (
                    f"Mutation {record.get('mutationId')} was interrupted during validation; "
                    "review its audit record before releasing the run.",
                    False,
                )
            elif event in {"mutation_rejected", "mutation_validation_failed"}:
                holds[run_id] = (str(record.get("detail") or "Mutation validation did not complete."), False)
            elif event in {
                "mutation_approved",
                "mutation_command_enqueued",
                "mutation_commands_enqueued",
                "mutation_enqueue_cancelled",
                "mutation_enqueue_failed",
            }:
                holds[run_id] = (
                    f"Mutation {record.get('mutationId')} may have been partially enqueued; stop the run.",
                    True,
                )
            elif event in {"mutation_enqueued", "mutation_hold_released"}:
                holds.pop(run_id, None)
        return holds

    def _load_and_verify(self) -> None:
        self._records = self._read_and_verify_records()

    def _capture_disk_state(self) -> None:
        try:
            stat_result = self._path.stat()
        except OSError as exc:
            message = f"Cannot inspect run-mutation ledger {self._path}: {exc}"
            raise MutationLedgerError(message) from exc
        tail = b""
        if self._records:
            tail = (_canonical_json(self._records[-1]) + "\n").encode("utf-8")
        self._capture_stat(stat_result, tail)

    def _capture_stat(self, stat_result: os.stat_result, tail: bytes) -> None:
        self._verified_device = stat_result.st_dev
        self._verified_inode = stat_result.st_ino
        self._verified_size = stat_result.st_size
        self._verified_mtime_ns = stat_result.st_mtime_ns
        self._verified_tail = tail

    def _assert_incremental_integrity(self, stream: BinaryIO) -> None:
        """Verify the previously authenticated file identity and tail in constant time."""
        stat_result = os.fstat(stream.fileno())
        if (
            stat_result.st_dev != self._verified_device
            or stat_result.st_ino != self._verified_inode
            or stat_result.st_size != self._verified_size
            or stat_result.st_mtime_ns != self._verified_mtime_ns
        ):
            message = f"Run-mutation ledger {self._path} changed outside this process (recordHash metadata mismatch)."
            raise MutationLedgerError(message)
        if self._verified_tail:
            stream.seek(-len(self._verified_tail), os.SEEK_END)
            if stream.read(len(self._verified_tail)) != self._verified_tail:
                message = f"Run-mutation ledger {self._path} changed outside this process (recordHash tail mismatch)."
                raise MutationLedgerError(message)

    def _read_and_verify_records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        previous_hash = "GENESIS"
        records: list[dict[str, Any]] = []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
            for sequence, line in enumerate(lines, start=1):
                record = json.loads(line)
                if not isinstance(record, dict):
                    message = "record is not a JSON object"
                    raise ValueError(message)
                if record.get("sequence") != sequence:
                    message = f"expected sequence {sequence}"
                    raise ValueError(message)
                if record.get("previousHash") != previous_hash:
                    message = f"broken previousHash at sequence {sequence}"
                    raise ValueError(message)
                if record.get("recordHash") != _record_hash(record):
                    message = f"invalid recordHash at sequence {sequence}"
                    raise ValueError(message)
                previous_hash = str(record["recordHash"])
                records.append(record)
        except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
            message = f"Cannot verify run-mutation ledger {self._path}: {exc}"
            raise MutationLedgerError(message) from exc
        return records


class _ProtocolEngineStateAdapter:
    """Version-pinned access to the current run's full authoritative state."""

    def __init__(self, store: object) -> None:
        self.store = store
        try:
            self.orchestrator = store.run_orchestrator
            self.engine = self.orchestrator._protocol_engine
            self.state_view = self.engine.state_view
        except AttributeError as exc:
            message = "Installed robot-server does not expose the Protocol Engine state required for safe mutation."
            raise MutationNotAllowedError(message) from exc

    def assert_current_run(self, run_id: str) -> None:
        """Require the requested run to be the in-memory current run."""
        if getattr(self.store, "current_run_id", None) != run_id:
            message = f"Run {run_id} is not the current Protocol Engine run."
            raise MutationNotAllowedError(message)

    def checkpoint(self, mode: str) -> tuple[str, object]:
        """Validate an explicit checkpoint or error-recovery insertion point."""
        status = _enum_value(self.store.get_status())
        if mode == "checkpoint":
            if status != "paused":
                message = f"Checkpoint mutation requires run status 'paused'; observed {status!r}."
                raise MutationNotAllowedError(message)
            pointer = self.store.get_current_command()
            command = self.store.get_command(pointer.command_id) if pointer is not None else None
            if command is None or command.commandType != "waitForResume" or _enum_value(command.status) != "running":
                message = (
                    "The pause is not a running waitForResume checkpoint; manual pause cannot accept inserted steps."
                )
                raise MutationNotAllowedError(message)
            checkpoint_message = getattr(command.params, "message", None)
            if not isinstance(checkpoint_message, str) or not checkpoint_message.startswith(MUTATION_CHECKPOINT_PREFIX):
                message = f"Checkpoint message must start with {MUTATION_CHECKPOINT_PREFIX!r}."
                raise MutationNotAllowedError(message)
            return command.id, command

        if status != "awaiting-recovery":
            message = f"Recovery mutation requires run status 'awaiting-recovery'; observed {status!r}."
            raise MutationNotAllowedError(message)
        pointer = self.store.get_command_recovery_target()
        command = self.store.get_command(pointer.command_id) if pointer is not None else None
        if command is None:
            message = "Protocol Engine did not expose an authoritative failed recovery target."
            raise MutationNotAllowedError(message)
        return command.id, command

    def snapshot(self, run_id: str) -> MutationSnapshot:
        """Build a complete state snapshot used by clients and the validator."""
        self.assert_current_run(run_id)
        summary = self.store.get_state_summary()
        current_pointer = self.store.get_current_command()
        current = self.store.get_command(current_pointer.command_id) if current_pointer is not None else None
        commands = [_command_summary(command) for command in self.orchestrator.get_all_commands()]
        tip_racks = {
            labware_id: {well_name: _enum_value(state) for well_name, state in wells.items()}
            for labware_id, wells in self.state_view.tips._state.tips_by_labware_id.items()
        }
        pipette_tips = self.store.get_tip_attached()
        pipettes = []
        for pipette in summary.pipettes:
            pipette_id = pipette.id
            pipettes.append(
                {
                    **pipette.model_dump(mode="json", by_alias=True),
                    "tipAttached": bool(pipette_tips.get(pipette_id, False)),
                    "maximumVolume": self.state_view.pipettes.get_maximum_volume(pipette_id),
                }
            )
        wells = []
        for well in summary.wells:
            capacity = self._well_capacity(well.labware_id, well.well_name)
            wells.append({**well.model_dump(mode="json", by_alias=True), "capacity": capacity})
        disposal_areas = []
        for area_name in self.state_view.addressable_areas.get_all():
            area = self.state_view.addressable_areas.get_addressable_area(area_name)
            area_type = _enum_value(area.area_type)
            if area_type in {"fixedTrash", "movableTrash", "wasteChute"}:
                disposal_areas.append(
                    {
                        "addressableAreaName": area.area_name,
                        "areaType": area_type,
                        "displayName": area.display_name,
                    }
                )
        state_payload = {
            "runId": run_id,
            "status": _enum_value(summary.status),
            "currentCommand": _command_summary(current) if current is not None else None,
            "commands": commands,
            "pipettes": pipettes,
            "tipRacks": tip_racks,
            "labware": [item.model_dump(mode="json", by_alias=True) for item in summary.labware],
            "modules": [item.model_dump(mode="json", by_alias=True) for item in summary.modules],
            "disposalAreas": disposal_areas,
            "wells": wells,
        }
        return MutationSnapshot(
            **state_payload,
            capturedAt=datetime.now(timezone.utc),
            fingerprint=hashlib.sha256(_canonical_json(state_payload).encode()).hexdigest(),
        )

    def compile(
        self,
        request: MutationRequest,
    ) -> tuple[list[pe_commands.CommandCreate], dict[str, float], list[dict[str, Any]]]:
        """Validate resources in a shadow state and compile only PE commands."""
        intent = pe_commands.CommandIntent.PROTOCOL if request.mode == "checkpoint" else pe_commands.CommandIntent.FIXIT
        if request.mode == "checkpoint" and any(
            isinstance(step, (RecoveryDropTipStep, RecoveryHomeStep)) for step in request.steps
        ):
            message = "Recovery-only steps are not allowed at a normal checkpoint."
            raise MutationValidationError(message)
        if request.mode == "recovery" and any(isinstance(step, (TransferStep, MixStep)) for step in request.steps):
            message = "Recovery mode only accepts recoveryDropTip, recoveryHome, delay, or comment steps."
            raise MutationValidationError(message)

        summary = self.store.get_state_summary()
        well_volumes = _tracked_well_volumes(summary.wells)
        tip_attached = dict(self.store.get_tip_attached())
        tip_state = copy.deepcopy(self.state_view.tips._state)
        tip_view = TipView(tip_state)
        commands: list[pe_commands.CommandCreate] = []
        allocated_tips: list[dict[str, Any]] = []

        for step in request.steps:
            if isinstance(step, TransferStep):
                self._require_empty_pipette(step.pipette_id, tip_attached)
                selected = self._allocate_tip(step.pipette_id, step.tip_rack_ids, tip_view)
                self._validate_working_volume(step.pipette_id, selected[0], step.volume)
                self._validate_flow_rates(
                    step.pipette_id,
                    step.aspirate_flow_rate,
                    step.dispense_flow_rate,
                )
                self._apply_liquid_delta(step.source, step.pipette_id, -step.volume, well_volumes)
                self._apply_liquid_delta(step.destination, step.pipette_id, step.volume, well_volumes)
                self._validate_disposal(step.disposal)
                commands.extend(_compile_transfer(step, selected[0], intent))
                allocated_tips.append(_tip_allocation(step.pipette_id, selected))
            elif isinstance(step, MixStep):
                self._require_empty_pipette(step.pipette_id, tip_attached)
                selected = self._allocate_tip(step.pipette_id, step.tip_rack_ids, tip_view)
                self._validate_working_volume(step.pipette_id, selected[0], step.volume)
                self._validate_flow_rates(
                    step.pipette_id,
                    step.aspirate_flow_rate,
                    step.dispense_flow_rate,
                )
                self._validate_mix_volume(step.target, step.pipette_id, step.volume, well_volumes)
                self._validate_disposal(step.disposal)
                commands.extend(_compile_mix(step, selected[0], intent))
                allocated_tips.append(_tip_allocation(step.pipette_id, selected))
            elif isinstance(step, DelayStep):
                commands.append(
                    pe_commands.WaitForDurationCreate(
                        params=pe_commands.WaitForDurationParams(seconds=step.seconds, message=step.message),
                        intent=intent,
                    )
                )
            elif isinstance(step, CommentStep):
                commands.append(
                    pe_commands.CommentCreate(params=pe_commands.CommentParams(message=step.message), intent=intent)
                )
            elif isinstance(step, RecoveryDropTipStep):
                if request.mode != "recovery":
                    message = "recoveryDropTip is only valid in recovery mode."
                    raise MutationValidationError(message)
                if not tip_attached.get(step.pipette_id, False):
                    message = f"Pipette {step.pipette_id!r} has no authoritative attached tip to drop."
                    raise MutationValidationError(message)
                self._validate_disposal(step.disposal)
                commands.extend(_compile_tip_disposal(step.pipette_id, step.disposal, intent))
                tip_attached[step.pipette_id] = False
            elif isinstance(step, RecoveryHomeStep):
                if request.mode != "recovery":
                    message = "recoveryHome is only valid in recovery mode."
                    raise MutationValidationError(message)
                commands.append(pe_commands.HomeCreate(params=pe_commands.HomeParams(axes=step.axes), intent=intent))

        return commands, well_volumes, allocated_tips

    def _allocate_tip(
        self,
        pipette_id: str,
        tip_rack_ids: Sequence[str],
        tip_view: TipView,
    ) -> tuple[WellReference, list[str]]:
        try:
            nozzle_map = self.store.get_nozzle_maps()[pipette_id]
        except KeyError as exc:
            message = f"Pipette {pipette_id!r} is not loaded in Protocol Engine state."
            raise MutationValidationError(message) from exc
        for labware_id in tip_rack_ids:
            if labware_id not in tip_view._state.tips_by_labware_id:
                continue
            anchor = tip_view.get_next_tip(
                labware_id=labware_id,
                num_tips=nozzle_map.tip_count,
                starting_tip_name=None,
                nozzle_map=nozzle_map,
            )
            if anchor is None:
                continue
            selected_wells = tip_view.compute_tips_to_mark_as_used_or_empty(labware_id, anchor, nozzle_map)
            for well_name in selected_wells:
                tip_view._state.tips_by_labware_id[labware_id][well_name] = TipRackWellState.EMPTY
            return WellReference(labwareId=labware_id, wellName=anchor), selected_wells
        racks = ", ".join(tip_rack_ids)
        message = f"No complete clean tip set is available for pipette {pipette_id!r} in requested racks: {racks}."
        raise MutationValidationError(message)

    def _require_empty_pipette(self, pipette_id: str, attached: Mapping[str, bool]) -> None:
        if pipette_id not in attached:
            message = f"Pipette {pipette_id!r} is not loaded in Protocol Engine state."
            raise MutationValidationError(message)
        if attached[pipette_id]:
            message = f"Pipette {pipette_id!r} already has a tip; checkpoint transfer/mix requires an empty pipette."
            raise MutationValidationError(message)

    def _validate_working_volume(self, pipette_id: str, tip: WellReference, volume: float) -> None:
        pipette_minimum = float(self.state_view.pipettes.get_minimum_volume(pipette_id))
        pipette_limit = float(self.state_view.pipettes.get_maximum_volume(pipette_id))
        tip_limit = self._well_capacity(tip.labware_id, tip.well_name)
        working_limit = min(pipette_limit, tip_limit)
        if volume < pipette_minimum:
            message = f"Requested {volume} µL is below the {pipette_minimum} µL minimum for pipette {pipette_id!r}."
            raise MutationValidationError(message)
        if volume > working_limit:
            message = (
                f"Requested {volume} µL exceeds the {working_limit} µL working limit for pipette "
                f"{pipette_id!r} with tip {tip.labware_id}/{tip.well_name}."
            )
            raise MutationValidationError(message)

    def _validate_flow_rates(
        self,
        pipette_id: str,
        aspirate_flow_rate: float,
        dispense_flow_rate: float,
    ) -> None:
        # The PE static pipette config does not publish absolute device flow-rate
        # maxima. Cap at one full nominal stroke per second, which is a
        # conservative, device-derived ceiling for this remote mutation surface.
        maximum = float(self.state_view.pipettes.get_maximum_volume(pipette_id))
        for operation, flow_rate in (
            ("aspirate", aspirate_flow_rate),
            ("dispense", dispense_flow_rate),
        ):
            if flow_rate > maximum:
                message = (
                    f"Requested {operation} flow rate {flow_rate} µL/s exceeds the conservative "
                    f"{maximum} µL/s limit for pipette {pipette_id!r}."
                )
                raise MutationValidationError(message)

    def _validate_mix_volume(
        self,
        target: WellReference,
        pipette_id: str,
        volume: float,
        well_volumes: Mapping[str, float],
    ) -> None:
        delta_per_well = volume * self._nozzles_per_well(target, pipette_id)
        affected = self.state_view.geometry.get_wells_covered_by_pipette_with_active_well(
            target.labware_id, target.well_name, pipette_id
        )
        for well_name in affected:
            key = f"{target.labware_id}/{well_name}"
            current = self._known_volume(key, well_volumes)
            if current < delta_per_well:
                message = (
                    f"Mix requires {delta_per_well} µL in {key}, but authoritative tracked volume is {current} µL."
                )
                raise MutationValidationError(message)

    def _apply_liquid_delta(
        self,
        well: WellReference,
        pipette_id: str,
        volume_per_nozzle: float,
        well_volumes: dict[str, float],
    ) -> None:
        affected = self.state_view.geometry.get_wells_covered_by_pipette_with_active_well(
            well.labware_id, well.well_name, pipette_id
        )
        nozzles_per_well = self._nozzles_per_well(well, pipette_id)
        delta = volume_per_nozzle * nozzles_per_well
        for well_name in affected:
            reference = WellReference(labwareId=well.labware_id, wellName=well_name)
            key = _well_key(reference)
            current = self._known_volume(key, well_volumes)
            predicted = current + delta
            if predicted < -1e-9:
                message = (
                    f"Mutation would aspirate below zero in {key}: current {current} µL, requested change {delta} µL."
                )
                raise MutationValidationError(message)
            capacity = self._well_capacity(well.labware_id, well_name)
            if predicted > capacity + 1e-9:
                message = f"Mutation would exceed {key} capacity: predicted {predicted} µL, capacity {capacity} µL."
                raise MutationValidationError(message)
            well_volumes[key] = max(0.0, predicted)

    def _known_volume(self, key: str, well_volumes: Mapping[str, float]) -> float:
        try:
            return well_volumes[key]
        except KeyError as exc:
            message = (
                f"No authoritative liquid volume is tracked for {key}. "
                "Load an initial liquid volume, including 0 µL for empty destination/waste wells, in the protocol."
            )
            raise MutationValidationError(message) from exc

    def _well_capacity(self, labware_id: str, well_name: str) -> float:
        try:
            definition = self.state_view.labware.get_definition(labware_id)
            return float(definition.wells[well_name].totalLiquidVolume)
        except (KeyError, AttributeError, TypeError) as exc:
            message = f"Cannot resolve well capacity for {labware_id}/{well_name}."
            raise MutationValidationError(message) from exc

    def _nozzles_per_well(self, well: WellReference, pipette_id: str) -> int:
        return int(self.state_view.geometry.get_nozzles_per_well(well.labware_id, well.well_name, pipette_id))

    def _validate_disposal(self, disposal: DisposalReference) -> None:
        if isinstance(disposal, AddressableAreaDisposal):
            try:
                area = self.state_view.addressable_areas.get_addressable_area(disposal.addressable_area_name)
            except (AddressableAreaDoesNotExistError, IncompatibleAddressableAreaError) as exc:
                message = f"Disposal area {disposal.addressable_area_name!r} is not loaded in the deck configuration."
                raise MutationValidationError(message) from exc
            area_type = _enum_value(area.area_type)
            if area_type not in {"fixedTrash", "movableTrash", "wasteChute"}:
                message = (
                    f"Addressable area {disposal.addressable_area_name!r} has type {area_type!r}; "
                    "only loaded trash or waste-chute areas may receive mutation tips."
                )
                raise MutationValidationError(message)
            return
        try:
            definition = self.state_view.labware.get_definition(disposal.labware_id)
            definition.wells[disposal.well_name]
        except (KeyError, AttributeError) as exc:
            message = f"Disposal well {disposal.labware_id}/{disposal.well_name} is not loaded."
            raise MutationValidationError(message) from exc
        if definition.parameters.isTiprack:
            message = (
                "Mutation tip disposal must be non-tip-rack labware; "
                "returned tips are not authorized for automatic reuse."
            )
            raise MutationValidationError(message)


class RunMutationCoordinator:
    """Serialize validation, durable audit, and Protocol Engine enqueueing."""

    def __init__(
        self,
        store_provider: Callable[[], object | None],
        ledger: MutationLedger,
        gate: RunMutationGate,
        adapter_factory: Callable[[object], _ProtocolEngineStateAdapter] = _ProtocolEngineStateAdapter,
        authenticated_actor: str | None = None,
    ) -> None:
        self._store_provider = store_provider
        self._ledger = ledger
        self._gate = gate
        self._adapter_factory = adapter_factory
        self._authenticated_actor = authenticated_actor
        self._lock = asyncio.Lock()
        for run_id, (reason, fatal) in self._ledger.unresolved_holds().items():
            self._gate.hold(run_id, reason, fatal=fatal)

    def snapshot(self, run_id: str) -> MutationSnapshot:
        """Return the current authoritative state without mutating the run."""
        return self._adapter(run_id).snapshot(run_id)

    def audit_records(self, run_id: str) -> list[dict[str, Any]]:
        """Return the durable mutation history for one run."""
        return self._ledger.records_for_run(run_id)

    @property
    def authenticated_actor(self) -> str | None:
        """Return the identity bound to this connector's mutation credential."""
        return self._authenticated_actor

    def authorize_checkpoint_resume(self, run_id: str, checkpoint_id: str, client_host: str) -> None:
        """Durably authorize an authenticated resume from one named checkpoint."""
        hold = self._gate.get(run_id)
        if hold is not None:
            message = f"Run remains paused by the mutation gate: {hold.reason}"
            raise MutationValidationError(message)
        adapter = self._adapter(run_id)
        current_checkpoint_id, _ = adapter.checkpoint("checkpoint")
        if current_checkpoint_id != checkpoint_id:
            message = (
                f"Checkpoint changed from {checkpoint_id} to {current_checkpoint_id}; refresh state before resuming."
            )
            raise MutationValidationError(message)
        completed = self._ledger.completed_mutation_at_checkpoint(run_id, checkpoint_id)
        self._ledger.append(
            "mutation_checkpoint_resume_authorized",
            {
                "runId": run_id,
                "mutationId": completed.get("mutationId") if completed is not None else None,
                "checkpointCommandId": checkpoint_id,
                "actor": self._authenticated_actor or "authenticated-mutation-token",
                "clientHost": client_host,
                "reason": (
                    "Resume after the completed mutation batch."
                    if completed is not None
                    else "Authenticated acknowledgement to resume without a mutation batch."
                ),
            },
        )

    def authorize_recovery_resume(self, run_id: str, client_host: str) -> None:
        """Durably authorize an authenticated resume from native error recovery."""
        hold = self._gate.get(run_id)
        if hold is not None:
            message = f"Run remains paused by the mutation gate: {hold.reason}"
            raise MutationValidationError(message)
        adapter = self._adapter(run_id)
        failed_command_id, _ = adapter.checkpoint("recovery")
        self._ledger.append(
            "mutation_recovery_resume_authorized",
            {
                "runId": run_id,
                "failedCommandId": failed_command_id,
                "actor": self._authenticated_actor or "authenticated-mutation-token",
                "clientHost": client_host,
                "reason": "Authenticated acknowledgement to resume Protocol Engine error recovery.",
            },
        )

    def authorize_prestart_setup(
        self,
        run_id: str,
        command_type: str,
        request_hash: str,
        client_host: str,
    ) -> None:
        """Durably authorize one bounded, non-actuating protocol-less setup command."""
        self._ledger.append(
            "protocol_less_setup_authorized",
            {
                "runId": run_id,
                "commandType": command_type,
                "requestHash": request_hash,
                "actor": self._authenticated_actor or "authenticated-mutation-token",
                "clientHost": client_host,
            },
        )

    async def mutate(self, run_id: str, request: MutationRequest, client_host: str) -> MutationResult:
        """Validate, audit, and enqueue one idempotent mutation."""
        async with self._lock:
            async with self._gate.transition_lock:
                adapter = self._adapter(run_id)
                request_payload = request.model_dump(mode="json", by_alias=True)
                request_hash = hashlib.sha256(_canonical_json(request_payload).encode()).hexdigest()
                existing = self._ledger.latest_for_mutation(run_id, request.mutation_id)
                if existing is not None:
                    if existing.get("requestHash") != request_hash:
                        message = f"mutationId {request.mutation_id} was already used with a different request."
                        raise MutationValidationError(message)
                    if existing.get("event") == "mutation_enqueued":
                        return MutationResult.model_validate(existing["result"])
                    enqueued_ids = existing.get("enqueuedCommandIds") or []
                    fatal = existing.get("event") in {
                        "mutation_approved",
                        "mutation_command_enqueued",
                        "mutation_commands_enqueued",
                        "mutation_enqueue_cancelled",
                        "mutation_enqueue_failed",
                    } or bool(enqueued_ids)
                    message = f"mutationId {request.mutation_id} already reached audit event {existing.get('event')!r}."
                    self._gate.hold(run_id, message, fatal=fatal)
                    raise MutationValidationError(message)

                unresolved = self._gate.get(run_id)
                if unresolved is not None:
                    disposition = "stop the run" if unresolved.fatal else "release the rejected hold"
                    message = (
                        f"Run {run_id} already has an unresolved mutation hold: {unresolved.reason} "
                        f"Review the audit trail and {disposition} before submitting another mutation."
                    )
                    raise MutationValidationError(message)

                self._gate.hold(
                    run_id,
                    f"Mutation {request.mutation_id} is being validated and durably enqueued.",
                    fatal=False,
                )

            common_audit = {
                "runId": run_id,
                "mutationId": request.mutation_id,
                "actor": self._authenticated_actor or request.actor,
                "claimedActor": request.actor,
                "clientHost": client_host,
                "reason": request.reason,
                "mode": request.mode,
                "steps": request_payload["steps"],
                "requestHash": request_hash,
            }
            try:
                self._ledger.append("mutation_requested", common_audit)
                snapshot = adapter.snapshot(run_id)
                checkpoint_id, _ = adapter.checkpoint(request.mode)
                completed = self._ledger.completed_mutation_at_checkpoint(run_id, checkpoint_id)
                if completed is not None:
                    message = (
                        f"Checkpoint {checkpoint_id} already has completed mutation batch "
                        f"{completed.get('mutationId')}; include all new steps in one request, "
                        "or resume to the next checkpoint."
                    )
                    raise MutationValidationError(message)
                commands, predicted_volumes, allocated_tips = adapter.compile(request)
            except (MutationNotAllowedError, MutationValidationError) as exc:
                self._gate.hold(run_id, str(exc), fatal=False)
                with contextlib.suppress(MutationLedgerError):
                    self._ledger.append("mutation_rejected", {**common_audit, "detail": str(exc)})
                raise
            except MutationLedgerError as exc:
                self._gate.hold(run_id, str(exc), fatal=False)
                raise
            except Exception as exc:
                detail = f"Authoritative mutation validation failed unexpectedly: {exc}"
                self._gate.hold(run_id, detail, fatal=False)
                with contextlib.suppress(MutationLedgerError):
                    self._ledger.append("mutation_validation_failed", {**common_audit, "detail": detail})
                raise MutationError(detail) from exc

            common_audit["snapshotFingerprint"] = snapshot.fingerprint

            serialized_commands = [
                command.model_dump(mode="json", by_alias=True, exclude_none=True) for command in commands
            ]
            try:
                self._ledger.append(
                    "mutation_approved",
                    {
                        **common_audit,
                        "checkpointCommandId": checkpoint_id,
                        "compiledCommands": serialized_commands,
                        "predictedWellVolumes": predicted_volumes,
                        "allocatedTips": allocated_tips,
                    },
                )
            except MutationLedgerError as exc:
                self._gate.hold(run_id, str(exc), fatal=False)
                raise
            command_ids: list[str] = []
            failed_command_id = checkpoint_id if request.mode == "recovery" else None
            try:
                for command in commands:
                    enqueued = await adapter.store.add_command_and_wait_for_interval(
                        request=command,
                        failed_command_id=failed_command_id,
                        wait_until_complete=False,
                        timeout=None,
                    )
                    command_ids.append(enqueued.id)
            except asyncio.CancelledError:
                detail = (
                    f"Mutation enqueue was cancelled after {len(command_ids)} of {len(commands)} commands; "
                    "Protocol Engine acceptance is ambiguous."
                )
                self._gate.hold(run_id, detail, fatal=True)
                with contextlib.suppress(MutationLedgerError):
                    self._ledger.append(
                        "mutation_enqueue_cancelled",
                        {**common_audit, "detail": detail, "enqueuedCommandIds": command_ids},
                    )
                raise
            except Exception as exc:
                detail = f"Mutation enqueue failed after {len(command_ids)} of {len(commands)} commands: {exc}"
                # Once an enqueue call has begun, an exception is ambiguous: PE
                # may have accepted the command before the response failed.
                self._gate.hold(run_id, detail, fatal=True)
                with contextlib.suppress(MutationLedgerError):
                    self._ledger.append(
                        "mutation_enqueue_failed",
                        {**common_audit, "detail": detail, "enqueuedCommandIds": command_ids},
                    )
                raise MutationError(detail) from exc

            try:
                for batch_start in range(0, len(command_ids), _LEDGER_COMMAND_BATCH_SIZE):
                    batch_ids = command_ids[batch_start : batch_start + _LEDGER_COMMAND_BATCH_SIZE]
                    self._ledger.append(
                        "mutation_commands_enqueued",
                        {
                            **common_audit,
                            "checkpointCommandId": checkpoint_id,
                            "commandIndexStart": batch_start,
                            "commandIds": batch_ids,
                        },
                    )
            except MutationLedgerError as exc:
                self._gate.hold(
                    run_id,
                    f"Mutation commands were accepted but enqueue audit failed: {exc}",
                    fatal=True,
                )
                raise

            result = MutationResult(
                mutationId=request.mutation_id,
                runId=run_id,
                status="enqueued",
                checkpointCommandId=checkpoint_id,
                commandIds=command_ids,
                resourceSnapshotBefore=snapshot.fingerprint,
                predictedWellVolumes=predicted_volumes,
                allocatedTips=allocated_tips,
            )
            try:
                self._ledger.append(
                    "mutation_enqueued",
                    {**common_audit, "result": result.model_dump(mode="json", by_alias=True)},
                )
            except MutationLedgerError as exc:
                self._gate.hold(
                    run_id,
                    f"All mutation commands were enqueued but final audit failed: {exc}",
                    fatal=True,
                )
                raise
            async with self._gate.transition_lock:
                self._gate.clear(run_id)
            return result

    async def release_rejected_hold(
        self,
        run_id: str,
        request: MutationGateReleaseRequest,
        client_host: str,
    ) -> None:
        """Audit an operator decision to discard a rejected proposal and allow resume."""
        async with self._lock, self._gate.transition_lock:
            hold = self._gate.get(run_id)
            if hold is None:
                message = f"Run {run_id} has no mutation hold to release."
                raise MutationValidationError(message)
            if hold.fatal:
                message = "A partially enqueued mutation is fatal for this run; stop the run."
                raise MutationValidationError(message)
            self._ledger.append(
                "mutation_hold_released",
                {
                    "runId": run_id,
                    "actor": self._authenticated_actor or request.actor,
                    "claimedActor": request.actor,
                    "clientHost": client_host,
                    "reason": request.reason,
                    "heldReason": hold.reason,
                },
            )
            self._gate.clear(run_id)

    def _adapter(self, run_id: str) -> _ProtocolEngineStateAdapter:
        store = self._store_provider()
        if store is None:
            message = "No current Protocol Engine run exists."
            raise MutationNotAllowedError(message)
        adapter = self._adapter_factory(store)
        adapter.assert_current_run(run_id)
        return adapter


def _compile_transfer(
    step: TransferStep,
    tip: WellReference,
    intent: pe_commands.CommandIntent,
) -> list[pe_commands.CommandCreate]:
    return [
        pe_commands.PickUpTipCreate(
            params=pe_commands.PickUpTipParams(
                pipetteId=step.pipette_id,
                labwareId=tip.labware_id,
                wellName=tip.well_name,
            ),
            intent=intent,
        ),
        pe_commands.AspirateCreate(
            params=pe_commands.AspirateParams(
                pipetteId=step.pipette_id,
                labwareId=step.source.labware_id,
                wellName=step.source.well_name,
                volume=step.volume,
                flowRate=step.aspirate_flow_rate,
            ),
            intent=intent,
        ),
        pe_commands.DispenseCreate(
            params=pe_commands.DispenseParams(
                pipetteId=step.pipette_id,
                labwareId=step.destination.labware_id,
                wellName=step.destination.well_name,
                volume=step.volume,
                flowRate=step.dispense_flow_rate,
            ),
            intent=intent,
        ),
        *_compile_tip_disposal(step.pipette_id, step.disposal, intent),
    ]


def _compile_mix(
    step: MixStep,
    tip: WellReference,
    intent: pe_commands.CommandIntent,
) -> list[pe_commands.CommandCreate]:
    commands: list[pe_commands.CommandCreate] = [
        pe_commands.PickUpTipCreate(
            params=pe_commands.PickUpTipParams(
                pipetteId=step.pipette_id,
                labwareId=tip.labware_id,
                wellName=tip.well_name,
            ),
            intent=intent,
        )
    ]
    for _ in range(step.repetitions):
        commands.extend(
            [
                pe_commands.AspirateCreate(
                    params=pe_commands.AspirateParams(
                        pipetteId=step.pipette_id,
                        labwareId=step.target.labware_id,
                        wellName=step.target.well_name,
                        volume=step.volume,
                        flowRate=step.aspirate_flow_rate,
                    ),
                    intent=intent,
                ),
                pe_commands.DispenseCreate(
                    params=pe_commands.DispenseParams(
                        pipetteId=step.pipette_id,
                        labwareId=step.target.labware_id,
                        wellName=step.target.well_name,
                        volume=step.volume,
                        flowRate=step.dispense_flow_rate,
                    ),
                    intent=intent,
                ),
            ]
        )
    commands.extend(_compile_tip_disposal(step.pipette_id, step.disposal, intent))
    return commands


def _compile_tip_disposal(
    pipette_id: str,
    disposal: DisposalReference,
    intent: pe_commands.CommandIntent,
) -> list[pe_commands.CommandCreate]:
    if isinstance(disposal, LabwareDisposal):
        return [
            pe_commands.DropTipCreate(
                params=pe_commands.DropTipParams(
                    pipetteId=pipette_id,
                    labwareId=disposal.labware_id,
                    wellName=disposal.well_name,
                ),
                intent=intent,
            )
        ]
    return [
        pe_commands.MoveToAddressableAreaForDropTipCreate(
            params=pe_commands.MoveToAddressableAreaForDropTipParams(
                pipetteId=pipette_id,
                addressableAreaName=disposal.addressable_area_name,
            ),
            intent=intent,
        ),
        pe_commands.DropTipInPlaceCreate(
            params=pe_commands.DropTipInPlaceParams(pipetteId=pipette_id),
            intent=intent,
        ),
    ]


def _tracked_well_volumes(wells: Sequence[object]) -> dict[str, float]:
    result: dict[str, float] = {}
    for well in wells:
        loaded = getattr(well, "loaded_volume", None)
        probed = getattr(well, "probed_volume", None)
        value = loaded if isinstance(loaded, (int, float)) else probed
        if isinstance(value, (int, float)):
            result[f"{well.labware_id}/{well.well_name}"] = float(value)
    return result


def _tip_allocation(pipette_id: str, selected: tuple[WellReference, list[str]]) -> dict[str, Any]:
    anchor, wells = selected
    return {
        "pipetteId": pipette_id,
        "labwareId": anchor.labware_id,
        "anchorWell": anchor.well_name,
        "wells": wells,
        "tipCount": len(wells),
    }


def _well_key(reference: WellReference) -> str:
    return f"{reference.labware_id}/{reference.well_name}"


def _command_summary(command: object) -> dict[str, Any]:
    payload = command.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {
        "id": payload.get("id"),
        "commandType": payload.get("commandType"),
        "status": payload.get("status"),
        "intent": payload.get("intent"),
        "params": payload.get("params", {}),
        "error": payload.get("error"),
    }


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return json.loads(json.dumps(value, default=str))


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _record_hash(record: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in record.items() if key != "recordHash"}
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


__all__ = [
    "MUTATION_CHECKPOINT_PREFIX",
    "AddressableAreaDisposal",
    "CommentStep",
    "DelayStep",
    "LabwareDisposal",
    "MixStep",
    "MutationError",
    "MutationGateReleaseRequest",
    "MutationLedger",
    "MutationLedgerError",
    "MutationNotAllowedError",
    "MutationRequest",
    "MutationResult",
    "MutationSnapshot",
    "MutationValidationError",
    "RecoveryDropTipStep",
    "RecoveryHomeStep",
    "RunMutationCoordinator",
    "TransferStep",
    "WellReference",
]
