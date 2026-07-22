"""Controlled Protocol Engine run-mutation safety tests."""

from __future__ import annotations

import asyncio
import stat
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from opentrons.hardware_control.nozzle_manager import NozzleMap
from opentrons.protocol_engine import commands as pe_commands
from opentrons.protocol_engine.state.tips import TipState
from opentrons.protocol_engine.types import TipRackWellState
from opentrons.types import NozzleConfigurationType, Point

from unitelabs.opentrons_flex.io.run_authority import RunMutationGate
from unitelabs.opentrons_flex.run_mutation import (
    MUTATION_CHECKPOINT_PREFIX,
    MutationError,
    MutationGateReleaseRequest,
    MutationLedger,
    MutationLedgerError,
    MutationNotAllowedError,
    MutationRequest,
    MutationSnapshot,
    MutationValidationError,
    RunMutationCoordinator,
    _ProtocolEngineStateAdapter,
)


def _request(*steps: dict, mutation_id: str | None = None, mode: str = "checkpoint") -> MutationRequest:
    return MutationRequest.model_validate(
        {
            "mutationId": mutation_id or str(uuid4()),
            "actor": "offline-test-operator",
            "reason": "verify controlled insertion",
            "mode": mode,
            "steps": list(steps) or [{"stepType": "comment", "message": "audited inserted step"}],
        }
    )


class _FakeStore:
    current_run_id = "run-1"

    def __init__(self, *, fail_on_add: bool = False) -> None:
        self.fail_on_add = fail_on_add
        self.requests: list[pe_commands.CommandCreate] = []

    async def add_command_and_wait_for_interval(self, *, request, **kwargs):
        del kwargs
        self.requests.append(request)
        if self.fail_on_add:
            raise RuntimeError("ambiguous Protocol Engine response")
        return SimpleNamespace(id=f"inserted-{len(self.requests)}")


class _CancelledStore(_FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def add_command_and_wait_for_interval(self, *, request, **kwargs):
        del kwargs
        self.requests.append(request)
        self.started.set()
        await asyncio.Event().wait()


class _FakeAdapter:
    def __init__(self, store: _FakeStore, *, reject: bool = False) -> None:
        self.store = store
        self.reject = reject

    def assert_current_run(self, run_id: str) -> None:
        assert run_id == self.store.current_run_id

    def snapshot(self, run_id: str) -> MutationSnapshot:
        return MutationSnapshot.model_validate(
            {
                "runId": run_id,
                "capturedAt": "2026-07-22T12:00:00Z",
                "fingerprint": "authoritative-state-hash",
                "status": "paused",
                "currentCommand": {"commandType": "waitForResume"},
                "commands": [],
                "pipettes": [],
                "tipRacks": {},
                "labware": [],
                "modules": [],
                "disposalAreas": [],
                "wells": [],
            }
        )

    def checkpoint(self, mode: str):
        if self.reject:
            raise MutationNotAllowedError("manual pause cannot accept inserted steps")
        assert mode == "checkpoint"
        return "checkpoint-command", object()

    def compile(self, request: MutationRequest):
        assert request.mode == "checkpoint"
        command = pe_commands.CommentCreate(
            params=pe_commands.CommentParams(message="audited inserted step"),
            intent=pe_commands.CommandIntent.PROTOCOL,
        )
        return [command], {"source/A1": 20.0, "destination/A1": 10.0}, []


def _coordinator(tmp_path: Path, adapter: _FakeAdapter) -> tuple[RunMutationCoordinator, RunMutationGate]:
    gate = RunMutationGate()
    coordinator = RunMutationCoordinator(
        store_provider=lambda: adapter.store,
        ledger=MutationLedger(tmp_path / "mutations.jsonl"),
        gate=gate,
        adapter_factory=lambda store: adapter,
    )
    return coordinator, gate


@pytest.mark.asyncio
async def test_success_is_idempotent_compiles_only_protocol_engine_commands_and_audits(tmp_path: Path) -> None:
    store = _FakeStore()
    coordinator, gate = _coordinator(tmp_path, _FakeAdapter(store))
    request = _request()

    first = await coordinator.mutate("run-1", request, "127.0.0.1")
    second = await coordinator.mutate("run-1", request, "127.0.0.1")

    assert first == second
    assert len(store.requests) == 1
    assert isinstance(store.requests[0], pe_commands.CommentCreate)
    assert store.requests[0].intent is pe_commands.CommandIntent.PROTOCOL
    assert gate.get("run-1") is None
    records = coordinator.audit_records("run-1")
    assert [record["event"] for record in records] == [
        "mutation_requested",
        "mutation_approved",
        "mutation_commands_enqueued",
        "mutation_enqueued",
    ]
    assert all(record["actor"] == "offline-test-operator" for record in records)
    assert all(record["requestHash"] for record in records)


@pytest.mark.asyncio
async def test_only_one_resource_reserving_batch_is_allowed_per_checkpoint(tmp_path: Path) -> None:
    store = _FakeStore()
    coordinator, gate = _coordinator(tmp_path, _FakeAdapter(store))

    await coordinator.mutate("run-1", _request(), "127.0.0.1")
    with pytest.raises(MutationValidationError, match="already has completed mutation batch"):
        await coordinator.mutate("run-1", _request(), "127.0.0.1")

    assert len(store.requests) == 1
    hold = gate.get("run-1")
    assert hold is not None and hold.fatal is False


@pytest.mark.asyncio
async def test_validation_rejection_holds_run_until_audited_release(tmp_path: Path) -> None:
    coordinator, gate = _coordinator(tmp_path, _FakeAdapter(_FakeStore(), reject=True))

    with pytest.raises(MutationNotAllowedError, match="manual pause"):
        await coordinator.mutate("run-1", _request(), "127.0.0.1")

    hold = gate.get("run-1")
    assert hold is not None and hold.fatal is False
    await coordinator.release_rejected_hold(
        "run-1",
        MutationGateReleaseRequest(actor="offline-test-operator", reason="discard rejected proposal"),
        "127.0.0.1",
    )
    assert gate.get("run-1") is None
    assert coordinator.audit_records("run-1")[-1]["event"] == "mutation_hold_released"


@pytest.mark.asyncio
async def test_ambiguous_enqueue_failure_is_fatal_and_cannot_be_released(tmp_path: Path) -> None:
    coordinator, gate = _coordinator(tmp_path, _FakeAdapter(_FakeStore(fail_on_add=True)))

    with pytest.raises(MutationError, match="ambiguous Protocol Engine response"):
        await coordinator.mutate("run-1", _request(), "127.0.0.1")

    hold = gate.get("run-1")
    assert hold is not None and hold.fatal is True
    with pytest.raises(MutationValidationError, match="stop the run"):
        await coordinator.release_rejected_hold(
            "run-1",
            MutationGateReleaseRequest(actor="offline-test-operator", reason="unsafe release attempt"),
            "127.0.0.1",
        )


@pytest.mark.asyncio
async def test_enqueue_cancellation_is_durably_fatal(tmp_path: Path) -> None:
    store = _CancelledStore()
    coordinator, gate = _coordinator(tmp_path, _FakeAdapter(store))
    task = asyncio.create_task(coordinator.mutate("run-1", _request(), "127.0.0.1"))
    await store.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    hold = gate.get("run-1")
    assert hold is not None and hold.fatal is True
    assert coordinator.audit_records("run-1")[-1]["event"] == "mutation_enqueue_cancelled"


def test_ledger_detects_startup_and_live_tampering(tmp_path: Path) -> None:
    path = tmp_path / "mutations.jsonl"
    ledger = MutationLedger(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ledger.append("mutation_requested", {"runId": "run-1"})
    path.write_text(path.read_text().replace("run-1", "run-X"), encoding="utf-8")

    with pytest.raises(MutationLedgerError, match="recordHash"):
        MutationLedger(path)
    with pytest.raises(MutationLedgerError, match="recordHash"):
        ledger.append("mutation_rejected", {"runId": "run-1"})


def test_coordinator_restores_recoverable_hold_from_durable_ledger(tmp_path: Path) -> None:
    ledger = MutationLedger(tmp_path / "mutations.jsonl")
    ledger.append(
        "mutation_rejected",
        {
            "runId": "run-1",
            "mutationId": str(uuid4()),
            "detail": "insufficient reagent",
        },
    )
    gate = RunMutationGate()

    RunMutationCoordinator(
        store_provider=lambda: None,
        ledger=ledger,
        gate=gate,
    )

    hold = gate.get("run-1")
    assert hold is not None
    assert hold.fatal is False
    assert hold.reason == "insufficient reagent"


def test_coordinator_restores_fatal_hold_after_possible_partial_enqueue(tmp_path: Path) -> None:
    ledger = MutationLedger(tmp_path / "mutations.jsonl")
    mutation_id = str(uuid4())
    ledger.append(
        "mutation_commands_enqueued",
        {
            "runId": "run-1",
            "mutationId": mutation_id,
            "commandId": "command-1",
        },
    )
    gate = RunMutationGate()

    RunMutationCoordinator(
        store_provider=lambda: None,
        ledger=ledger,
        gate=gate,
    )

    hold = gate.get("run-1")
    assert hold is not None
    assert hold.fatal is True
    assert mutation_id in hold.reason


def test_only_named_wait_for_resume_checkpoint_is_accepted() -> None:
    command = SimpleNamespace(
        id="checkpoint-command",
        commandType="waitForResume",
        status="running",
        params=SimpleNamespace(message="ordinary operator pause"),
    )
    store = SimpleNamespace(
        get_status=lambda: "paused",
        get_current_command=lambda: SimpleNamespace(command_id=command.id),
        get_command=lambda command_id: command,
    )
    adapter = object.__new__(_ProtocolEngineStateAdapter)
    adapter.store = store

    with pytest.raises(MutationNotAllowedError, match="must start"):
        adapter.checkpoint("checkpoint")

    command.params.message = f"{MUTATION_CHECKPOINT_PREFIX}before-wash"
    checkpoint_id, _ = adapter.checkpoint("checkpoint")
    assert checkpoint_id == command.id


def _single_nozzle_map() -> NozzleMap:
    point = Point(0, 0, 0)
    nozzles = OrderedDict([("A1", point)])
    rows = OrderedDict([("A", ["A1"])])
    columns = OrderedDict([("1", ["A1"])])
    return NozzleMap(
        starting_nozzle="A1",
        map_store=nozzles,
        valid_map_key="all",
        rows=rows,
        columns=columns,
        configuration=NozzleConfigurationType.FULL,
        full_instrument_map_store=nozzles,
        full_instrument_rows=rows,
        full_instrument_columns=columns,
    )


def _resource_adapter(*, source: float = 50, destination: float = 0) -> _ProtocolEngineStateAdapter:
    tip_columns = [[f"{row}{column}" for row in "ABCDEFGH"] for column in range(1, 13)]
    tips = {well: TipRackWellState.CLEAN for column in tip_columns for well in column}
    definitions = {
        "tips": SimpleNamespace(
            wells={well: SimpleNamespace(totalLiquidVolume=1000) for well in tips},
            parameters=SimpleNamespace(isTiprack=True),
        ),
        "source": SimpleNamespace(
            wells={"A1": SimpleNamespace(totalLiquidVolume=100)},
            parameters=SimpleNamespace(isTiprack=False),
        ),
        "destination": SimpleNamespace(
            wells={"A1": SimpleNamespace(totalLiquidVolume=100)},
            parameters=SimpleNamespace(isTiprack=False),
        ),
        "trash": SimpleNamespace(
            wells={"A1": SimpleNamespace(totalLiquidVolume=10000)},
            parameters=SimpleNamespace(isTiprack=False),
        ),
    }
    geometry = MagicMock()
    geometry.get_wells_covered_by_pipette_with_active_well.side_effect = lambda labware_id, well_name, pipette_id: [
        well_name
    ]
    geometry.get_nozzles_per_well.return_value = 1
    addressable_areas = MagicMock()
    addressable_areas.get_addressable_area.return_value = SimpleNamespace(area_type="movableTrash")
    state_view = SimpleNamespace(
        tips=SimpleNamespace(state=None, _state=TipState({"tips": tips}, {"tips": tip_columns})),
        pipettes=SimpleNamespace(
            get_minimum_volume=lambda pipette_id: 1,
            get_maximum_volume=lambda pipette_id: 1000,
        ),
        geometry=geometry,
        labware=SimpleNamespace(get_definition=lambda labware_id: definitions[labware_id]),
        addressable_areas=addressable_areas,
    )
    wells = [
        SimpleNamespace(labware_id="source", well_name="A1", loaded_volume=source, probed_volume=None),
        SimpleNamespace(
            labware_id="destination",
            well_name="A1",
            loaded_volume=destination,
            probed_volume=None,
        ),
    ]
    store = SimpleNamespace(
        get_state_summary=lambda: SimpleNamespace(wells=wells),
        get_tip_attached=lambda: {"pipette": False},
        get_nozzle_maps=lambda: {"pipette": _single_nozzle_map()},
    )
    adapter = object.__new__(_ProtocolEngineStateAdapter)
    adapter.store = store
    adapter.state_view = state_view
    return adapter


def _transfer(volume: float) -> dict:
    return {
        "stepType": "transfer",
        "pipetteId": "pipette",
        "tipRackIds": ["tips"],
        "source": {"labwareId": "source", "wellName": "A1"},
        "destination": {"labwareId": "destination", "wellName": "A1"},
        "disposal": {
            "disposalType": "labware",
            "labwareId": "trash",
            "wellName": "A1",
        },
        "volume": volume,
        "aspirateFlowRate": 20,
        "dispenseFlowRate": 30,
    }


def test_resource_validator_allocates_clean_tips_and_recalculates_liquid() -> None:
    commands, volumes, tips = _resource_adapter().compile(_request(_transfer(20)))

    assert [command.commandType for command in commands] == ["pickUpTip", "aspirate", "dispense", "dropTip"]
    assert all(command.intent is pe_commands.CommandIntent.PROTOCOL for command in commands)
    assert volumes["source/A1"] == 30
    assert volumes["destination/A1"] == 20
    assert tips == [
        {
            "pipetteId": "pipette",
            "labwareId": "tips",
            "anchorWell": "A1",
            "wells": ["A1"],
            "tipCount": 1,
        }
    ]


def test_resource_validator_rejects_out_of_range_volume_and_flow() -> None:
    below_minimum = _transfer(0.5)
    with pytest.raises(MutationValidationError, match=r"below the 1\.0 µL minimum"):
        _resource_adapter().compile(_request(below_minimum))

    excessive_flow = _transfer(20)
    excessive_flow["aspirateFlowRate"] = 1001
    with pytest.raises(MutationValidationError, match="exceeds the conservative"):
        _resource_adapter().compile(_request(excessive_flow))

    non_finite = _transfer(20)
    non_finite["dispenseFlowRate"] = float("inf")
    with pytest.raises(ValueError, match="finite number"):
        _request(non_finite)


def test_addressable_trash_compiles_to_protocol_engine_move_and_drop() -> None:
    transfer = _transfer(20)
    transfer["disposal"] = {
        "disposalType": "addressableArea",
        "addressableAreaName": "movableTrashA3",
    }

    commands, _, _ = _resource_adapter().compile(_request(transfer))

    assert [command.commandType for command in commands] == [
        "pickUpTip",
        "aspirate",
        "dispense",
        "moveToAddressableAreaForDropTip",
        "dropTipInPlace",
    ]
    assert all(command.intent is pe_commands.CommandIntent.PROTOCOL for command in commands)


def test_addressable_non_disposal_area_is_rejected() -> None:
    adapter = _resource_adapter()
    adapter.state_view.addressable_areas.get_addressable_area.return_value = SimpleNamespace(area_type="stage")
    transfer = _transfer(20)
    transfer["disposal"] = {
        "disposalType": "addressableArea",
        "addressableAreaName": "not-trash",
    }

    with pytest.raises(MutationValidationError, match="only loaded trash"):
        adapter.compile(_request(transfer))


@pytest.mark.parametrize(
    ("source", "destination", "match"),
    [
        (10, 0, "below zero"),
        (50, 90, "exceed destination/A1 capacity"),
    ],
)
def test_resource_validator_rejects_insufficient_reagent_or_destination_capacity(
    source: float,
    destination: float,
    match: str,
) -> None:
    with pytest.raises(MutationValidationError, match=match):
        _resource_adapter(source=source, destination=destination).compile(_request(_transfer(20)))
