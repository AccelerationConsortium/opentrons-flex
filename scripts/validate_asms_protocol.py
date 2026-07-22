#!/usr/bin/env python3
"""Validate the prepared AS-MS Flex protocol without contacting a robot."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from unitelabs.opentrons_flex.protocol_preflight import inspect_protocol

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PROTOCOL = _ROOT / "protocols" / "asms" / "asms_single_point_wash_and_elute.py"
_DEFAULT_LABWARE_DIR = _ROOT / "protocols" / "asms" / "labware"
_SHADOW_LABWARE = {
    "thermokingfisherdeepwell_96_wellplate_2000ul": "nest_96_wellplate_2ml_deep",
    "azenta_96_wellplate_200ul_pcr": "opentrons_96_wellplate_200ul_pcr_full_skirt",
}
_EXACT_LABWARE_HASHES = {
    "azenta_96_wellplate_200ul_pcr": "43506d5482e3dfebff377e56b709150c81415473efc9cf2c6362dc4b68a1e20f",
    "thermokingfisherdeepwell_96_wellplate_2000ul": (
        "2ea9c15468816ace3970fe497cef7e1dc22d5f9ab033656bf9472a62396dfb47"
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight the AS-MS protocol. Exact mode requires the two physical custom "
            "labware JSON definitions. --shadow exercises command logic only and is not "
            "evidence that real labware geometry is safe."
        )
    )
    parser.add_argument(
        "--labware-dir",
        action="append",
        default=None,
        type=Path,
        help=(
            "Directory containing exact custom labware JSON definitions (repeatable). "
            "Defaults to protocols/asms/labware in exact mode."
        ),
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Use explicit built-in geometry substitutes for logic-only simulation.",
    )
    parser.add_argument("--json", action="store_true", help="Print the complete report as JSON.")
    return parser


def _human_report(report) -> str:
    if report.exact_bundle_ready:
        status = "READY: exact labware bundle simulated successfully"
    elif report.shadow_passed:
        status = "SHADOW PASS: command logic passed; exact labware remains required"
    else:
        status = "BLOCKED: exact protocol bundle is not ready"

    lines = [
        status,
        f"Protocol: {report.protocol_name}",
        f"Robot/API: {report.robot_type} / {report.api_level} (installed max {report.maximum_supported_api_level})",
        f"Commands: {report.command_count}",
        (
            "Tips: "
            f"{report.tip_pickups} pickups, {report.returned_tip_pickups} returned reusable pickups, "
            f"{report.discarded_tip_pickups} discarded columns"
        ),
        f"Gripper moves: {report.gripper_moves}",
        f"Aspirate/dispense commands: {report.aspirate_commands}/{report.dispense_commands}",
        f"Programmed delays: {report.delay_seconds:g} s",
        f"Temperature cleanup commands: {report.temperature_deactivations}",
    ]
    if report.missing_labware:
        lines.append("Missing exact labware: " + ", ".join(report.missing_labware))
    if report.shadow_substitutions:
        lines.append(
            "Shadow substitutions: "
            + ", ".join(f"{source} -> {target}" for source, target in report.shadow_substitutions)
        )
    if report.simulation_error:
        lines.append(f"Simulation error: {report.simulation_error}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    labware_dirs = args.labware_dir or ([] if args.shadow else [_DEFAULT_LABWARE_DIR])
    report = inspect_protocol(
        _DEFAULT_PROTOCOL,
        custom_labware_paths=labware_dirs,
        shadow_labware=_SHADOW_LABWARE if args.shadow else None,
        expected_custom_labware_hashes=_EXACT_LABWARE_HASHES,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_human_report(report))
    return 0 if report.simulation_completed and (args.shadow or report.exact_bundle_ready) else 2


if __name__ == "__main__":
    raise SystemExit(main())
