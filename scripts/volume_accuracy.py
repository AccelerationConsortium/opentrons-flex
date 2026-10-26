#!/usr/bin/env python3
"""Gravimetric volume-accuracy campaign for the Opentrons Flex SiLA connector.

Runs explicit, measurable liquid transfers against a live Flex and records
target / measured / error / CV per volume band, exactly the "connector
parameters -> Flex execution -> acceptable physical result" evidence the
connector validation needs.

Method
------
- Water is aspirated from the manifest source well and dispensed into a
  destination container (a tared 96-deep-well plate or single tubes) using
  the connector's SiLA primitive commands (PickUpTip / DropTip /
  TransferWithVerifiedLiquidClass with the Opentrons verified WATER class, or
  an explicit TransferProfile).
- The operator weighs the destination container before and after every
  replicate on an analytical balance; the script prompts for each reading.
  Measured volume is ``(W_after - W_before) / density``, reported per channel
  (an 8-channel transfer delivers ``channels * volume``).
- Every command's observable feedback (acceptance -> result, elapsed time,
  success/failure) is captured into the report so status feedback is
  evidenced, not assumed.

Usage
-----
    python scripts/volume_accuracy.py --robot 169.254.105.239:50051 \\
        --acceptance-manifest /path/to/flex_acceptance.json \\
        --volumes "10,25,50,100,200,500" --replicates 5 \\
        --report ./volume-accuracy.json

The default uses the Opentrons verified WATER liquid class. For an explicit
profile (rates, air gap, push-out, blow-out), pass --explicit-profile.

Balances with 0.1 mg resolution are sufficient for >=10 uL transfers.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import sys
import time
from collections.abc import Callable
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import grpc.aio
from sila.server import CommandConfirmation, CommandExecutionUUID

from unitelabs.cdk import SiLAServerConfig
from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.acceptance import AcceptanceManifest
from unitelabs.opentrons_flex.features import (
    LiquidPosition,
    PipetteMount,
    TipLocation,
    TransferProfile,
    VerifiedLiquidClass,
    WellGeometry,
)

_DENSITY_WATER_20C_G_PER_ML = 0.9982

_MOTION_PACKAGE = "sila2.ca.accelerationconsortium.robots.motioncontroller.v2"
_MOTION_SERVICE = f"{_MOTION_PACKAGE}.MotionController"
_PIPETTE_PACKAGE = "sila2.ca.accelerationconsortium.robots.pipettecontroller.v1"
_PIPETTE_SERVICE = f"{_PIPETTE_PACKAGE}.PipetteController"
_TIP_PACKAGE = "sila2.ca.accelerationconsortium.robots.tipcontroller.v1"
_TIP_SERVICE = f"{_TIP_PACKAGE}.TipController"
_LIQUID_PACKAGE = "sila2.ca.accelerationconsortium.robots.liquidhandlingcontroller.v1"
_LIQUID_SERVICE = f"{_LIQUID_PACKAGE}.LiquidHandlingController"

_SCHEMA = "volume-accuracy/v1"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without hardware)
# --------------------------------------------------------------------------- #


def parse_volumes(text: str) -> list[float]:
    """Parse a comma-separated volume list into positive, finite uL values."""
    raw = [item.strip() for item in text.split(",") if item.strip()]
    if not raw:
        raise ValueError("--volumes must contain at least one volume")
    volumes: list[float] = []
    for item in raw:
        try:
            value = float(item)
        except ValueError as exc:
            raise ValueError(f"Invalid volume {item!r} in --volumes") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Volume {item!r} must be finite and greater than zero")
        volumes.append(value)
    return volumes


def water_volume_from_weight(weight_g: float, density_g_ml: float) -> float:
    """Convert a dispensed water weight (grams) to volume (uL)."""
    if not math.isfinite(weight_g):
        raise ValueError("Weight must be finite")
    if not math.isfinite(density_g_ml) or density_g_ml <= 0:
        raise ValueError("Density must be finite and greater than zero")
    return weight_g / density_g_ml * 1000.0


def summarize_measurements(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-volume-band summary: mean, std, CV%, max error and pass verdict."""
    if not measurements:
        raise ValueError("Cannot summarize an empty measurement set")
    target = float(measurements[0]["target_ul"])
    measured = [float(item["measured_ul"]) for item in measurements]
    n = len(measured)
    mean = sum(measured) / n
    variance = sum((value - mean) ** 2 for value in measured) / max(1, n - 1)
    std = math.sqrt(variance)
    errors_pct = [
        (float(item["measured_ul"]) - float(item["target_ul"])) / float(item["target_ul"]) * 100.0
        for item in measurements
    ]
    mean_error_pct = sum(errors_pct) / n
    return {
        "target_ul": target,
        "n": n,
        "mean_ul": round(mean, 4),
        "std_ul": round(std, 4),
        "cv_pct": round(std / mean * 100.0, 4) if mean else 0.0,
        "mean_error_pct": round(mean_error_pct, 4),
        "max_error_pct": round(max(abs(value) for value in errors_pct), 4),
        "min_ul": round(min(measured), 4),
        "max_ul": round(max(measured), 4),
    }


def verdict(summary: dict[str, Any], tolerance_pct: float, repeatability_pct: float) -> dict[str, Any]:
    """Decide pass/fail from tolerance and repeatability limits."""
    accuracy_pass = summary["max_error_pct"] <= tolerance_pct
    repeatability_pass = summary["cv_pct"] <= repeatability_pct
    return {
        "accuracy_pass": accuracy_pass,
        "repeatability_pass": repeatability_pass,
        "pass": accuracy_pass and repeatability_pass,
        "tolerance_pct": tolerance_pct,
        "repeatability_pct": repeatability_pct,
    }


def build_report(
    *,
    robot: str,
    pipette: dict[str, Any],
    density_g_ml: float,
    measurements: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    command_feedback: list[dict[str, Any]],
    tolerance_pct: float,
    repeatability_pct: float,
    start_time: float,
    mode: str,
) -> dict[str, Any]:
    """Assemble the full campaign report (measurements, summaries, feedback)."""
    overall_pass = True
    for summary in summaries:
        verdict_data = verdict(summary, tolerance_pct, repeatability_pct)
        summary.update(verdict_data)
        overall_pass = overall_pass and verdict_data["pass"]
    return {
        "schema": _SCHEMA,
        "robot": robot,
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.monotonic() - start_time, 3),
        "mode": mode,
        "density_g_ml": density_g_ml,
        "pipette": pipette,
        "measurements": measurements,
        "summaries": summaries,
        "command_feedback": command_feedback,
        "overall_pass": overall_pass,
    }


def render_measurements_csv(measurements: list[dict[str, Any]]) -> str:
    """Render the raw measurement table as CSV."""
    header = ["target_ul", "replicate", "measured_ul", "error_ul", "error_pct"]
    lines = [",".join(header)]
    for item in measurements:
        measured = float(item["measured_ul"])
        target = float(item["target_ul"])
        lines.append(
            ",".join(
                [
                    str(target),
                    str(item["replicate"]),
                    f"{measured:.4f}",
                    f"{measured - target:.4f}",
                    f"{(measured - target) / target * 100:.4f}",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def render_summary_csv(summaries: list[dict[str, Any]]) -> str:
    """Render the per-volume summary table as CSV."""
    header = [
        "target_ul",
        "n",
        "mean_ul",
        "std_ul",
        "cv_pct",
        "mean_error_pct",
        "max_error_pct",
        "accuracy_pass",
        "repeatability_pass",
        "pass",
    ]
    lines = [",".join(header)]
    for item in summaries:
        row = [
            str(item["target_ul"]),
            str(item["n"]),
            str(item["mean_ul"]),
            str(item["std_ul"]),
            str(item["cv_pct"]),
            str(item["mean_error_pct"]),
            str(item["max_error_pct"]),
            str(item["accuracy_pass"]),
            str(item["repeatability_pass"]),
            str(item["pass"]),
        ]
        lines.append(",".join(row))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# SiLA client plumbing (same pattern as scripts/sila_client_control.py)
# --------------------------------------------------------------------------- #


async def _call_observable(
    channel: grpc.aio.Channel,
    pb: object,
    service: str,
    package: str,
    method: str,
    params: dict | None = None,
    timeout_s: float = 300.0,
) -> tuple[dict, float]:
    """Start an observable command, poll its result, return (decoded, elapsed)."""
    started = time.monotonic()
    req = await pb.encode(f"{package}.{method}_Parameters", params or {})
    start = channel.unary_unary(f"/{service}/{method}")
    confirmation = CommandConfirmation.decode(await start(req))
    uuid = confirmation.command_execution_uuid.value

    result = channel.unary_unary(f"/{service}/{method}_Result")
    uuid_bytes = CommandExecutionUUID(value=uuid).encode()
    deadline = asyncio.get_running_loop().time() + timeout_s

    while True:
        try:
            resp_bytes = await result(uuid_bytes)
            decoded = await pb.decode(f"{package}.{method}_Responses", resp_bytes)
            return decoded, time.monotonic() - started
        except grpc.aio.AioRpcError as exc:
            details = base64.b64decode(exc.details() or b"")
            result_not_ready = exc.code() is grpc.StatusCode.ABORTED and b"Result is not ready" in details
            if not result_not_ready:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"{method} did not finish within {timeout_s}s") from exc
            await asyncio.sleep(0.05)


async def _call_property(channel: grpc.aio.Channel, pb: object, service: str, package: str, name: str) -> dict:
    stub = channel.unary_unary(f"/{service}/{name}")
    resp_bytes = await stub(b"")
    return await pb.decode(f"{package}.{name}_Responses", resp_bytes)


class _CommandRecorder:
    """Record per-command feedback: ok, elapsed, error detail."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def ok(self, command: str, elapsed_s: float, detail: str = "") -> None:
        self.events.append({"command": command, "ok": True, "elapsed_s": round(elapsed_s, 3), "detail": detail})

    def fail(self, command: str, error: str) -> None:
        self.events.append({"command": command, "ok": False, "elapsed_s": None, "detail": str(error)[:500]})


# --------------------------------------------------------------------------- #
# Campaign
# --------------------------------------------------------------------------- #


async def run_campaign(
    args: argparse.Namespace,
    manifest: AcceptanceManifest | None,
    channel: grpc.aio.Channel,
    pb: object,
    recorder: _CommandRecorder,
    *,
    read_weight: Callable[[str], str],
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Execute the full gravimetric campaign against the live SiLA server."""
    started = time.monotonic()
    pipette_cfg = manifest.pipetting if manifest is not None else None
    mount_name = (pipette_cfg.mount if pipette_cfg is not None else args.mount) or "LEFT"
    pipette_mount = PipetteMount[mount_name]
    density = args.density

    def tip_location(x: float, y: float, z: float) -> TipLocation:
        return TipLocation(x, y, z)

    def point(value: Any) -> LiquidPosition:
        return LiquidPosition(value.x, value.y, value.z)

    def well(value: Any) -> WellGeometry:
        return WellGeometry(value.x, value.y, value.bottom_z, value.top_z, value.length, value.width)

    if pipette_cfg is not None:
        tip_pickup = tip_location(pipette_cfg.tip_pickup.x, pipette_cfg.tip_pickup.y, pipette_cfg.tip_pickup.z)
        tip_drop = tip_location(pipette_cfg.tip_drop.x, pipette_cfg.tip_drop.y, pipette_cfg.tip_drop.z)
        tip_length = pipette_cfg.tip_length
        tiprack_uri = pipette_cfg.tiprack_uri
        source_well = well(pipette_cfg.source_well)
        destination_well = well(pipette_cfg.destination_well)
        source = point(pipette_cfg.source)
        source_retract = point(pipette_cfg.source_retract)
        destination = point(pipette_cfg.destination)
        destination_retract = point(pipette_cfg.destination_retract)
    else:
        tip_pickup = tip_location(*args.tip_pickup)
        tip_drop = tip_location(*args.tip_drop)
        tip_length = args.tip_length
        tiprack_uri = args.tiprack_uri
        source_well = well(args.source_well)
        destination_well = well(args.destination_well)
        source = point(args.source)
        source_retract = point(args.source_retract)
        destination = point(args.destination)
        destination_retract = point(args.destination_retract)

    volumes = parse_volumes(args.volumes)
    replicates = args.replicates

    # ---- Preflight ------------------------------------------------------- #
    log("== Preflight ==")
    machine = await _call_property(channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "Get_MachineStatus")
    status = next(iter(machine.values()))
    log(
        f"MachineStatus: estop={status.estop} door_open={status.door_open} "
        f"is_error_state={status.is_error_state} message={status.message}"
    )
    if status.is_error_state:
        raise RuntimeError(f"Robot is in an error state: {status.message}")
    if status.estop != "DISENGAGED":
        raise RuntimeError(f"E-stop is not disengaged: {status.estop}")
    if status.door_open:
        raise RuntimeError("Flex door is open; close it before actuation")

    simulating = await _call_property(channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "Get_IsSimulating")
    is_sim = next(iter(simulating.values()))
    if is_sim:
        raise RuntimeError("Volume accuracy cannot run against a simulator; a real Flex is required")
    log("Not simulating; live hardware confirmed")

    attached, elapsed = await _call_observable(
        channel, pb, _PIPETTE_SERVICE, _PIPETTE_PACKAGE, "GetAttachedPipettes", timeout_s=30.0
    )
    recorder.ok("GetAttachedPipettes", elapsed)
    pipettes = next(iter(attached.values()))
    selected = next((item for item in pipettes if item.mount is pipette_mount), None)
    if selected is None or not selected.attached:
        raise RuntimeError(f"No attached pipette on {mount_name}")
    channels = int(selected.channels)
    max_volume = float(selected.max_volume)
    log(f"Pipette: model={selected.model} id={selected.pipette_id} channels={channels} max={max_volume}uL")
    for volume in volumes:
        if volume > max_volume:
            raise RuntimeError(f"Requested volume {volume}uL exceeds pipette capacity {max_volume}uL")
        if volume < float(selected.min_volume):
            log(f"WARNING: {volume}uL is below pipette minimum {selected.min_volume}uL")
    pipette_info = {
        "mount": mount_name,
        "model": selected.model,
        "pipette_id": selected.pipette_id,
        "channels": channels,
        "max_volume_ul": max_volume,
    }

    await _call_observable(channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "Home", timeout_s=180.0)
    recorder.ok("Home", 0.0)
    log("Homed")

    # ---- Per-replicate transfers ----------------------------------------- #
    measurements: list[dict[str, Any]] = []
    replicate_index = 0
    for volume in volumes:
        for replicate in range(1, replicates + 1):
            replicate_index += 1
            label = f"V={volume}uL rep={replicate}/{replicates}"
            log(f"\n== {label} ==")
            try:
                # Fresh tip unless reuse is explicitly requested.
                if not args.reuse_tip:
                    _, el = await _call_observable(
                        channel,
                        pb,
                        _TIP_SERVICE,
                        _TIP_PACKAGE,
                        "PickUpTip",
                        {
                            "mount": pipette_mount,
                            "location": tip_pickup,
                            "tip_length": tip_length,
                            "prep_after": False,
                        },
                        timeout_s=60.0,
                    )
                    recorder.ok("PickUpTip", el)
                else:
                    log("Reusing already-attached tip (--reuse-tip)")

                log("Weigh destination container BEFORE dispense, then enter the reading (g):")
                weight_before = float(read_weight("  W_before [g] > ").strip())
                log("Dispensing...")
                if args.explicit_profile:
                    profile = TransferProfile(
                        aspirate_rate=args.aspirate_rate,
                        dispense_rate=args.dispense_rate,
                        push_out=args.push_out,
                        air_gap=args.air_gap,
                        mix_before_cycles=0,
                        mix_before_volume=0.0,
                        mix_after_cycles=0,
                        mix_after_volume=0.0,
                        blow_out=args.blow_out,
                    )
                    _, el = await _call_observable(
                        channel,
                        pb,
                        _LIQUID_SERVICE,
                        _LIQUID_PACKAGE,
                        "Transfer",
                        {
                            "mount": pipette_mount,
                            "source": source,
                            "source_retract": source_retract,
                            "destination": destination,
                            "destination_retract": destination_retract,
                            "volume": volume,
                            "profile": profile,
                        },
                        timeout_s=args.command_timeout,
                    )
                    recorder.ok("Transfer", el, f"explicit profile volume={volume}")
                    liquid_mode = "explicit-profile"
                else:
                    _, el = await _call_observable(
                        channel,
                        pb,
                        _LIQUID_SERVICE,
                        _LIQUID_PACKAGE,
                        "TransferWithVerifiedLiquidClass",
                        {
                            "mount": pipette_mount,
                            "source_well": source_well,
                            "destination_well": destination_well,
                            "volume": volume,
                            "liquid_class": VerifiedLiquidClass.WATER,
                            "tiprack_uri": tiprack_uri,
                        },
                        timeout_s=args.command_timeout,
                    )
                    recorder.ok("TransferWithVerifiedLiquidClass", el, f"water volume={volume}")
                    liquid_mode = "verified-water"

                log("Weigh destination container AFTER dispense, then enter the reading (g):")
                weight_after = float(read_weight("  W_after [g] > ").strip())

                delta_g = weight_after - weight_before
                measured_total = water_volume_from_weight(delta_g, density)
                measured_per_channel = measured_total / channels
                error_ul = measured_per_channel - volume
                error_pct = error_ul / volume * 100.0
                log(
                    f"  delta={delta_g * 1000:.1f}mg -> {measured_total:.2f}uL total, "
                    f"{measured_per_channel:.2f}uL/channel "
                    f"(target {volume}uL, error {error_pct:+.2f}%)"
                )
                measurements.append(
                    {
                        "target_ul": volume,
                        "replicate": replicate,
                        "measured_ul": round(measured_per_channel, 4),
                        "error_ul": round(error_ul, 4),
                        "error_pct": round(error_pct, 4),
                        "delta_g": round(delta_g, 6),
                        "measured_total_ul": round(measured_total, 4),
                        "channels": channels,
                        "liquid_mode": liquid_mode,
                    }
                )
            except (grpc.aio.AioRpcError, asyncio.CancelledError, TimeoutError, ValueError, RuntimeError) as exc:
                recorder.fail(f"replicate {replicate_index} ({label})", str(exc))
                log(f"  FAILED: {exc}")
                if args.fail_fast:
                    raise
                continue
            finally:
                if not args.reuse_tip:
                    try:
                        _, el = await _call_observable(
                            channel,
                            pb,
                            _TIP_SERVICE,
                            _TIP_PACKAGE,
                            "DropTip",
                            {"mount": pipette_mount, "location": tip_drop, "home_after": False},
                            timeout_s=60.0,
                        )
                        recorder.ok("DropTip", el)
                    except (grpc.aio.AioRpcError, asyncio.CancelledError, TimeoutError) as exc:
                        recorder.fail("DropTip", str(exc))
                        log(f"  DropTip FAILED: {exc}")

    if not measurements:
        raise RuntimeError("No successful measurements; nothing to summarize")

    summaries = [
        summarize_measurements([item for item in measurements if item["target_ul"] == target]) for target in volumes
    ]
    # Only summarize bands that produced measurements.
    summaries = [s for s in summaries if s["n"] > 0]
    return build_report(
        robot=args.robot,
        pipette=pipette_info,
        density_g_ml=density,
        measurements=measurements,
        summaries=summaries,
        command_feedback=recorder.events,
        tolerance_pct=args.tolerance_pct,
        repeatability_pct=args.repeatability_pct,
        start_time=started,
        mode="verified-water" if not args.explicit_profile else "explicit-profile",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot", required=True, help="Flex SiLA gRPC address, e.g. 169.254.105.239:50051")
    parser.add_argument(
        "--acceptance-manifest",
        type=Path,
        default=None,
        help="Validated acceptance manifest (coordinates, tiprack, mount). Preferred source of geometry.",
    )
    parser.add_argument("--volumes", default="10,25,50,100,200,500", help="Comma-separated target volumes in uL")
    parser.add_argument("--replicates", type=int, default=5, help="Replicates per volume band")
    parser.add_argument("--density", type=float, default=_DENSITY_WATER_20C_G_PER_ML, help="Water density g/mL")
    parser.add_argument("--tolerance-pct", type=float, default=2.0, help="Max |error| %% for accuracy pass")
    parser.add_argument("--repeatability-pct", type=float, default=1.0, help="Max CV %% for repeatability pass")
    parser.add_argument("--reuse-tip", action="store_true", help="Reuse the attached tip across all replicates")
    parser.add_argument("--fail-fast", action="store_true", help="Abort the campaign on the first failed replicate")
    parser.add_argument("--command-timeout", type=float, default=300.0, help="Per-command observable timeout seconds")
    parser.add_argument("--report", type=Path, default=None, help="Write JSON report (plus .csv sidecars)")

    group = parser.add_argument_group("explicit profile (instead of verified WATER liquid class)")
    group.add_argument("--explicit-profile", action="store_true", help="Use an explicit TransferProfile")
    group.add_argument("--aspirate-rate", type=float, default=1.0)
    group.add_argument("--dispense-rate", type=float, default=1.0)
    group.add_argument("--push-out", type=float, default=0.0)
    group.add_argument("--air-gap", type=float, default=0.0)
    group.add_argument("--blow-out", action="store_true")

    group = parser.add_argument_group("geometry (only when --acceptance-manifest is omitted)")
    group.add_argument("--mount", default="LEFT", choices=("LEFT", "RIGHT"))
    group.add_argument("--tip-length", type=float, default=95.6)
    group.add_argument("--tiprack-uri", default="opentrons/opentrons_flex_96_tiprack_1000ul/1")
    group.add_argument("--tip-pickup", nargs=3, type=float, metavar=("X", "Y", "Z"))
    group.add_argument("--tip-drop", nargs=3, type=float, metavar=("X", "Y", "Z"))
    group.add_argument("--source", nargs=3, type=float, metavar=("X", "Y", "Z"))
    group.add_argument("--source-retract", nargs=3, type=float, metavar=("X", "Y", "Z"))
    group.add_argument("--destination", nargs=3, type=float, metavar=("X", "Y", "Z"))
    group.add_argument("--destination-retract", nargs=3, type=float, metavar=("X", "Y", "Z"))
    group.add_argument(
        "--source-well", nargs=6, type=float, metavar=("X", "Y", "BOTTOM_Z", "TOP_Z", "SIZE_X", "SIZE_Y")
    )
    group.add_argument(
        "--destination-well",
        nargs=6,
        type=float,
        metavar=("X", "Y", "BOTTOM_Z", "TOP_Z", "SIZE_X", "SIZE_Y"),
    )
    return parser


async def _amain(args: argparse.Namespace) -> int:
    manifest: AcceptanceManifest | None = None
    if args.acceptance_manifest is not None:
        manifest = AcceptanceManifest.load(args.acceptance_manifest)
    if manifest is None and any(
        value is None
        for value in (
            args.tip_pickup,
            args.tip_drop,
            args.source,
            args.source_retract,
            args.destination,
            args.destination_retract,
            args.source_well,
            args.destination_well,
        )
    ):
        print("ERROR: --acceptance-manifest or complete geometry arguments are required", file=sys.stderr)
        return 2

    address = args.robot if ":" in args.robot else f"{args.robot}:50051"
    print("Initializing protobuf codec locally...")
    config = OpentronsFlexConfig(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    gen = create_app(config)
    connector = await gen.__anext__()
    await connector.start()
    pb = connector.sila_server.protobuf
    channel = grpc.aio.insecure_channel(address)
    recorder = _CommandRecorder()
    try:
        print(f"Connecting to live Flex SiLA server at {address}...")
        report = await run_campaign(
            args,
            manifest,
            channel,
            pb,
            recorder,
            read_weight=input,
        )
    finally:
        await channel.close()
        await connector.stop()

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        csv_dir = args.report.with_suffix("")
        csv_dir.parent.mkdir(parents=True, exist_ok=True)
        (csv_dir.parent / f"{csv_dir.name}.measurements.csv").write_text(
            render_measurements_csv(report["measurements"]), encoding="utf-8"
        )
        (csv_dir.parent / f"{csv_dir.name}.summary.csv").write_text(
            render_summary_csv(report["summaries"]), encoding="utf-8"
        )
        print(f"Report written to {args.report}")

    print("\n== Summary ==")
    for summary in report["summaries"]:
        print(
            f"  {summary['target_ul']:>6}uL: n={summary['n']} mean={summary['mean_ul']:.2f}uL "
            f"CV={summary['cv_pct']:.2f}% max|err|={summary['max_error_pct']:.2f}% "
            f"-> {'PASS' if summary['pass'] else 'FAIL'}"
        )
    failed_commands = [event for event in report["command_feedback"] if not event["ok"]]
    if failed_commands:
        print(f"\n  {len(failed_commands)} command failure(s) recorded:")
        for event in failed_commands:
            print(f"    {event['command']}: {event['detail']}")
    print(f"\nOVERALL: {'PASS' if report['overall_pass'] else 'FAIL'}")
    return 0 if report["overall_pass"] and not failed_commands else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.replicates < 1:
        print("ERROR: --replicates must be >= 1", file=sys.stderr)
        return 2
    try:
        parse_volumes(args.volumes)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
