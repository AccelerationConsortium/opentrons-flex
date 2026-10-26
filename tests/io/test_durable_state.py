"""Tests for persisted recovery gates, deck-ledger reconciliation, and live module connection state."""

import json
from pathlib import Path

import pytest

from unitelabs.opentrons_flex import deck_ledger
from unitelabs.opentrons_flex.io._module_base import ModuleControllerBase
from unitelabs.opentrons_flex.io.labware_state import LabwareMovementState
from unitelabs.opentrons_flex.io.recovery_state import HardwareRecoveryState

# ------------------------------------------------------------------ recovery state


async def test_recovery_gates_survive_a_connector_restart(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    first = HardwareRecoveryState()
    first.bind_persistence(path)
    first.mark_fully_homed(first.generation)
    first.require_tip_reconciliation("LEFT")
    first.require_gripper_home()
    first.wait_for_persistence()

    restarted = HardwareRecoveryState()
    restarted.bind_persistence(path)

    assert restarted.tip_reconciliation_required == {"LEFT"}
    assert restarted.gripper_home_required
    assert not restarted.rehome_required, "a restart must not add gates that were not recorded"
    assert json.loads(path.read_text())["tip_reconciliation_required"] == ["LEFT"]


async def test_a_halt_before_restart_still_requires_a_full_home(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    first = HardwareRecoveryState()
    first.bind_persistence(path)
    first.mark_halted()
    first.wait_for_persistence()

    restarted = HardwareRecoveryState()
    restarted.bind_persistence(path)

    assert restarted.rehome_required
    assert not restarted.operation_ready


async def test_cleared_gates_are_cleared_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    state = HardwareRecoveryState()
    state.bind_persistence(path)
    state.require_tip_reconciliation("RIGHT")
    state.mark_tip_reconciled("RIGHT")
    state.mark_fully_homed(state.generation)
    state.wait_for_persistence()

    payload = json.loads(path.read_text())
    assert payload["tip_reconciliation_required"] == []
    assert payload["rehome_required"] is False


def test_unreadable_recovery_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "recovery.json"
    path.write_text("{not json")
    state = HardwareRecoveryState()
    state.bind_persistence(path)

    assert state.tip_reconciliation_required == {"LEFT", "RIGHT"}
    assert state.gripper_home_required
    assert state.rehome_required


# ---------------------------------------------------------------------- deck ledger


def _invalid_ledger(path: Path) -> None:
    state = LabwareMovementState(path, {"A1": "plate"})
    state.begin_move()  # an interrupted move leaves the ledger invalid


def test_reconcile_restores_a_valid_ledger_and_writes_an_audit_record(tmp_path: Path) -> None:
    ledger = tmp_path / "deck.json"
    _invalid_ledger(ledger)
    state = LabwareMovementState(ledger, {})
    assert not state.valid

    state.reconcile({"B2": "plate"}, operator="alice", reason="plate re-seated after jam")
    state.close()

    reopened = LabwareMovementState(ledger, {})
    assert reopened.valid
    assert reopened.occupancy == {"B2": "plate"}
    audit = [json.loads(line) for line in state.audit_path.read_text().splitlines()]
    assert audit[-1]["operator"] == "alice"
    assert audit[-1]["previous"] == {"valid": False, "occupancy": {"A1": "plate"}}


def test_reconcile_requires_identity_and_reason(tmp_path: Path) -> None:
    state = LabwareMovementState(tmp_path / "deck.json", {})
    with pytest.raises(ValueError, match="operator identity and reason"):
        state.reconcile({}, operator=" ", reason="x")


def test_deck_ledger_cli_requires_confirmation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "deck.json"
    _invalid_ledger(ledger)

    code = deck_ledger.main(["reconcile", "--ledger", str(ledger), "--operator", "alice", "--reason", "jam", "--empty"])

    assert code == 2
    assert "--confirm" in capsys.readouterr().err
    assert not LabwareMovementState(ledger, {}).valid


def test_deck_ledger_cli_reconciles_with_movement_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = json.loads(Path("config/asms_unit_labware_movement.json").read_text())
    source["state_file"] = str(tmp_path / "deck.json")
    movement_config = tmp_path / "movement.json"
    movement_config.write_text(json.dumps(source))
    _invalid_ledger(tmp_path / "deck.json")

    code = deck_ledger.main(
        [
            "reconcile",
            "--movement-config",
            str(movement_config),
            "--operator",
            "alice",
            "--reason",
            "verified deck",
            "--initial",
            "--confirm",
        ]
    )

    assert code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["valid"] is True
    assert shown["occupancy"] == source["initial_occupancy"]


def test_deck_ledger_cli_rejects_malformed_locations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = deck_ledger.main(
        [
            "reconcile",
            "--ledger",
            str(tmp_path / "deck.json"),
            "--operator",
            "alice",
            "--reason",
            "x",
            "--set",
            "A1",
            "--confirm",
        ]
    )
    assert code == 1
    assert "LOCATION=LABWARE" in capsys.readouterr().err


# ------------------------------------------------------------------ module state


class _Controller(ModuleControllerBase):
    pass


async def test_shared_module_reports_disconnect_after_usb_removal() -> None:
    module = object()
    attached = [module]
    controller = _Controller.from_module(module, attached_modules=lambda: attached)

    assert await controller.is_connected()
    attached.clear()
    assert not await controller.is_connected()
    assert not controller.module_connected


async def test_shared_module_without_inventory_keeps_legacy_behaviour() -> None:
    controller = _Controller.from_module(object())
    assert await controller.is_connected()


async def test_recording_a_halt_never_writes_on_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from unitelabs.opentrons_flex.io import recovery_state

    writer_threads: list[str] = []
    original = recovery_state._write_snapshot

    def recording(path: Path, payload: dict[str, object]) -> None:
        writer_threads.append(threading.current_thread().name)
        original(path, payload)

    monkeypatch.setattr(recovery_state, "_write_snapshot", recording)
    state = HardwareRecoveryState()
    state.bind_persistence(tmp_path / "recovery.json")
    state.mark_halted()
    state.wait_for_persistence()

    assert writer_threads
    assert threading.main_thread().name not in writer_threads


def test_show_does_not_modify_the_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "deck.json"
    LabwareMovementState(ledger, {"A1": "plate"}).close()
    before = ledger.read_bytes()

    assert deck_ledger.main(["show", "--ledger", str(ledger)]) == 0

    assert ledger.read_bytes() == before
    shown = json.loads(capsys.readouterr().out)
    assert shown["valid"] is True
    assert shown["clean_shutdown"] is True
