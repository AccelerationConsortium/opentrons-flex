"""
Local operator tool for the durable labware-movement deck ledger.

Run on the robot while the connector service is stopped::

    python -m unitelabs.opentrons_flex.deck_ledger show --movement-config CONFIG.json
    python -m unitelabs.opentrons_flex.deck_ledger reconcile --movement-config CONFIG.json
        --operator alice --reason "re-seated plate after jam"
        --set A1=asms-plate --set B1=elution-plate --confirm

``flexctl deck HOST ...`` wraps the same commands over SSH.

An invalid ledger (after an interrupted move or unclean shutdown) blocks every
allowlisted gripper plan until an operator has physically inspected the deck and
recorded the verified occupancy here. Remote SiLA clients can never do this.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .io.labware_movement import load_labware_movement_config
from .io.labware_state import LabwareMovementState


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or reconcile the durable Flex deck ledger.")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_source(sub: argparse.ArgumentParser) -> None:
        source = sub.add_mutually_exclusive_group(required=True)
        source.add_argument("--movement-config", type=Path, help="Labware movement configuration JSON.")
        source.add_argument("--ledger", type=Path, help="Deck ledger JSON path.")

    show = commands.add_parser("show", help="Print ledger validity and occupancy.")
    add_source(show)

    reconcile = commands.add_parser("reconcile", help="Record an operator-verified occupancy and mark it valid.")
    add_source(reconcile)
    occupancy = reconcile.add_mutually_exclusive_group(required=True)
    occupancy.add_argument("--set", action="append", metavar="LOCATION=LABWARE", help="Occupied location.")
    occupancy.add_argument("--empty", action="store_true", help="The verified deck holds no tracked labware.")
    occupancy.add_argument("--occupancy-file", type=Path, help="JSON object mapping location to labware.")
    occupancy.add_argument(
        "--initial", action="store_true", help="Restore the configured initial occupancy (needs --movement-config)."
    )
    reconcile.add_argument("--operator", required=True, help="Identity of the person who inspected the deck.")
    reconcile.add_argument("--reason", required=True, help="Why reconciliation was necessary.")
    reconcile.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm the physical deck was inspected and matches the supplied occupancy.",
    )
    return parser


def _resolve(args: argparse.Namespace) -> tuple[Path, dict[str, str]]:
    if args.movement_config is not None:
        loaded = load_labware_movement_config(str(args.movement_config))
        if loaded.state_file is None:
            msg = "The movement configuration does not declare a state_file."
            raise ValueError(msg)
        return Path(loaded.state_file), dict(loaded.initial_occupancy)
    return Path(args.ledger), {}


def _occupancy(args: argparse.Namespace, initial: dict[str, str]) -> dict[str, str]:
    if args.empty:
        return {}
    if args.initial:
        if not initial:
            msg = "--initial requires --movement-config with a non-empty initial_occupancy."
            raise ValueError(msg)
        return initial
    if args.occupancy_file is not None:
        data = json.loads(args.occupancy_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = "--occupancy-file must contain a JSON object."
            raise ValueError(msg)
        return {str(key): str(value) for key, value in data.items()}
    result: dict[str, str] = {}
    for item in args.set:
        location, separator, labware = item.partition("=")
        if not separator or not location.strip() or not labware.strip():
            msg = f"--set expects LOCATION=LABWARE, got {item!r}."
            raise ValueError(msg)
        if location.strip() in result:
            msg = f"Location {location.strip()!r} was given more than once."
            raise ValueError(msg)
        result[location.strip()] = labware.strip()
    return result


def _describe(state: LabwareMovementState) -> dict[str, object]:
    return {"ledger": str(state.path), "valid": state.valid, "occupancy": state.occupancy}


def _read_only(ledger: Path) -> dict[str, object]:
    """Describe the ledger without opening it for writing (safe while the connector runs)."""
    raw = json.loads(ledger.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = "state ledger root must be a JSON object"
        raise ValueError(msg)
    valid = raw.get("valid") is True
    clean = raw.get("clean_shutdown") is True
    return {
        "ledger": str(ledger),
        "valid": valid,
        "clean_shutdown": clean,
        "occupancy": raw.get("occupancy", {}),
        "note": (
            "A running connector keeps clean_shutdown false. If it stopped uncleanly, the ledger "
            "is treated as invalid at the next start until it is reconciled."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    """Run the deck-ledger operator tool."""
    args = _parser().parse_args(argv)
    try:
        ledger, initial = _resolve(args)
        if args.command == "show":
            payload = _read_only(ledger) if ledger.exists() else {"ledger": str(ledger), "exists": False}
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0
        if not args.confirm:
            sys.stderr.write("ERROR: inspect the physical deck first, then rerun with --confirm.\n")
            return 2
        occupancy = _occupancy(args, initial)
        state = LabwareMovementState(ledger, initial)
        try:
            state.reconcile(occupancy, operator=args.operator, reason=args.reason)
            sys.stdout.write(json.dumps(_describe(state), indent=2, sort_keys=True) + "\n")
        finally:
            state.close()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
