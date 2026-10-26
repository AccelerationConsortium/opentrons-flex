#!/usr/bin/env python3
"""Repeated-run stability campaign for the Opentrons Flex SiLA connector.

Runs the same guarded liquid-handling cycle N times against a live Flex and
reports completion rate, command failure rate, retries, per-run timing, and
gantry-position drift. This is the "run it more than once" evidence: a single
successful run proves nothing about state drift, tip tracking, or timeouts.

Each cycle: Home -> GetPosition (baseline) -> PickUpTip ->
TransferWithVerifiedLiquidClass (water) -> DropTip -> GetPosition (drift check).

A failed command is retried once (recorded); a cycle that still fails is
marked failed and the campaign continues, so completion rate is meaningful.

Usage
-----
    python scripts/stability_run.py --robot 169.254.105.239:50051 \\
        --acceptance-manifest /path/to/flex_acceptance.json \\
        --runs 5 --volume 50 --report ./stability.json
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
    PipetteMount,
    TipLocation,
    VerifiedLiquidClass,
    WellGeometry,
)
from unitelabs.opentrons_flex.features.motion_control import Mount

_MOTION_PACKAGE = "sila2.ca.accelerationconsortium.robots.motioncontroller.v2"
_MOTION_SERVICE = f"{_MOTION_PACKAGE}.MotionController"
_PIPETTE_PACKAGE = "sila2.ca.accelerationconsortium.robots.pipettecontroller.v1"
_PIPETTE_SERVICE = f"{_PIPETTE_PACKAGE}.PipetteController"
_TIP_PACKAGE = "sila2.ca.accelerationconsortium.robots.tipcontroller.v1"
_TIP_SERVICE = f"{_TIP_PACKAGE}.TipController"
_LIQUID_PACKAGE = "sila2.ca.accelerationconsortium.robots.liquidhandlingcontroller.v1"
_LIQUID_SERVICE = f"{_LIQUID_PACKAGE}.LiquidHandlingController"

_SCHEMA = "stability/v1"


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


def _well(value: Any) -> WellGeometry:
    return WellGeometry(value.x, value.y, value.bottom_z, value.top_z, value.length, value.width)


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def build_report(
    *,
    robot: str,
    runs: int,
    volume_ul: float,
    cycles: list[dict[str, Any]],
    drift_events: list[dict[str, Any]],
    drift_mm: float,
    started: float,
) -> dict[str, Any]:
    """Assemble the stability report with completion and failure statistics."""
    total_commands = sum(cycle["commands_total"] for cycle in cycles)
    failed_commands = sum(cycle["commands_failed"] for cycle in cycles)
    retries = sum(cycle["retries"] for cycle in cycles)
    completed = [cycle for cycle in cycles if cycle["ok"]]
    timings = [cycle["elapsed_s"] for cycle in completed]
    return {
        "schema": _SCHEMA,
        "robot": robot,
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.monotonic() - started, 3),
        "runs": runs,
        "volume_ul": volume_ul,
        "completion_rate": round(len(completed) / runs, 4) if runs else 0.0,
        "command_failure_rate": round(failed_commands / total_commands, 4) if total_commands else 0.0,
        "retries": retries,
        "mean_cycle_s": round(sum(timings) / len(timings), 3) if timings else None,
        "min_cycle_s": round(min(timings), 3) if timings else None,
        "max_cycle_s": round(max(timings), 3) if timings else None,
        "drift_mm": drift_mm,
        "drift_events": drift_events,
        "cycles": cycles,
        "overall_pass": (len(completed) == runs and failed_commands == 0 and not drift_events),
    }


async def run_campaign(
    args: argparse.Namespace,
    manifest: AcceptanceManifest,
    channel: grpc.aio.Channel,
    pb: object,
    *,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Execute the repeated-cycle campaign against the live SiLA server."""
    started = time.monotonic()
    p = manifest.pipetting
    pipette_mount = PipetteMount[p.mount]
    robot_mount = Mount[p.mount]
    tip_pickup = TipLocation(p.tip_pickup.x, p.tip_pickup.y, p.tip_pickup.z)
    tip_drop = TipLocation(p.tip_drop.x, p.tip_drop.y, p.tip_drop.z)
    source_well = _well(p.source_well)
    destination_well = _well(p.destination_well)
    volume = args.volume
    runs = args.runs
    drift_mm = args.drift_mm

    # Preflight -------------------------------------------------------------
    machine = await _call_property(channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "Get_MachineStatus")
    status = next(iter(machine.values()))
    if status.is_error_state:
        raise RuntimeError(f"Robot is in an error state: {status.message}")
    if status.estop != "DISENGAGED":
        raise RuntimeError("E-stop is not disengaged")
    if status.door_open:
        raise RuntimeError("Flex door is open; close it before actuation")
    simulating = await _call_property(channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "Get_IsSimulating")
    if next(iter(simulating.values())):
        raise RuntimeError("Stability campaign cannot run against a simulator")
    attached, _ = await _call_observable(
        channel, pb, _PIPETTE_SERVICE, _PIPETTE_PACKAGE, "GetAttachedPipettes", timeout_s=30.0
    )
    pipettes = next(iter(attached.values()))
    selected = next(item for item in pipettes if item.mount is pipette_mount)
    if selected is None or not selected.attached:
        raise RuntimeError(f"No attached pipette on {p.mount}")
    if volume > float(selected.max_volume):
        raise RuntimeError(f"Volume {volume}uL exceeds pipette capacity {selected.max_volume}uL")
    log(f"Pipette: {selected.model} channels={selected.channels}")

    # Baseline position (after homing) for drift detection.
    await _call_observable(channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "Home", timeout_s=180.0)
    pos, _ = await _call_observable(
        channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "GetPosition", {"mount": robot_mount}, timeout_s=60.0
    )
    baseline = (float(pos.x), float(pos.y), float(pos.z))
    log(f"Baseline gantry position after homing: {baseline}")

    cycles: list[dict[str, Any]] = []
    drift_events: list[dict[str, Any]] = []

    for run_index in range(1, runs + 1):
        cycle_started = time.monotonic()
        ok = True
        commands_total = 0
        commands_failed = 0
        retries = 0
        failures: list[str] = []

        async def one(command: str, **kwargs: Any) -> Any:
            """Execute one observable command with a single recorded retry."""
            nonlocal commands_total, commands_failed, retries
            service = kwargs["service"]
            package = kwargs["package"]
            params = kwargs["params"]
            timeout_s = kwargs.get("timeout_s", 300.0)
            commands_total += 1
            try:
                return await _call_observable(channel, pb, service, package, command, params, timeout_s=timeout_s)
            except (grpc.aio.AioRpcError, TimeoutError, asyncio.CancelledError) as exc:
                failures.append(f"{command}: {exc}")
                retries += 1
                commands_total += 1
                try:
                    return await _call_observable(channel, pb, service, package, command, params, timeout_s=timeout_s)
                except (grpc.aio.AioRpcError, TimeoutError, asyncio.CancelledError) as exc2:
                    commands_failed += 1
                    failures.append(f"{command} (retry): {exc2}")
                    return None

        log(f"\n== Run {run_index}/{runs} ==")
        try:
            await one(
                "Home",
                service=_MOTION_SERVICE,
                package=_MOTION_PACKAGE,
                params=None,
                timeout_s=180.0,
            )
            pickup, _ = await one(
                "PickUpTip",
                service=_TIP_SERVICE,
                package=_TIP_PACKAGE,
                params={
                    "mount": pipette_mount,
                    "location": tip_pickup,
                    "tip_length": p.tip_length,
                    "prep_after": False,
                },
                timeout_s=60.0,
            )
            if pickup is None:
                ok = False
            transfer, _ = await one(
                "TransferWithVerifiedLiquidClass",
                service=_LIQUID_SERVICE,
                package=_LIQUID_PACKAGE,
                params={
                    "mount": pipette_mount,
                    "source_well": source_well,
                    "destination_well": destination_well,
                    "volume": volume,
                    "liquid_class": VerifiedLiquidClass.WATER,
                    "tiprack_uri": p.tiprack_uri,
                },
                timeout_s=args.command_timeout,
            )
            if transfer is None:
                ok = False
            drop, _ = await one(
                "DropTip",
                service=_TIP_SERVICE,
                package=_TIP_PACKAGE,
                params={"mount": pipette_mount, "location": tip_drop, "home_after": False},
                timeout_s=60.0,
            )
            if drop is None:
                ok = False

            pos_after, _ = await _call_observable(
                channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "GetPosition", {"mount": robot_mount}, timeout_s=60.0
            )
            current = (float(pos_after.x), float(pos_after.y), float(pos_after.z))
            drift = _distance(baseline, current)
            if drift > drift_mm:
                drift_events.append(
                    {
                        "run": run_index,
                        "baseline_mm": baseline,
                        "observed_mm": current,
                        "drift_mm": round(drift, 4),
                    }
                )
                log(f"  DRIFT: {drift:.3f} mm from baseline")
        except Exception as exc:
            ok = False
            failures.append(f"cycle: {exc}")

        elapsed = time.monotonic() - cycle_started
        cycles.append(
            {
                "run": run_index,
                "ok": ok,
                "elapsed_s": round(elapsed, 3),
                "commands_total": commands_total,
                "commands_failed": commands_failed,
                "retries": retries,
                "failures": failures,
            }
        )
        log(f"  {'PASS' if ok else 'FAIL'} in {elapsed:.1f}s")

    return build_report(
        robot=args.robot,
        runs=runs,
        volume_ul=volume,
        cycles=cycles,
        drift_events=drift_events,
        drift_mm=drift_mm,
        started=started,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot", required=True, help="Flex SiLA gRPC address, e.g. 169.254.105.239:50051")
    parser.add_argument("--acceptance-manifest", type=Path, required=True, help="Validated acceptance manifest")
    parser.add_argument("--runs", type=int, default=5, help="Number of repeated cycles")
    parser.add_argument("--volume", type=float, default=50.0, help="Transfer volume in uL per cycle")
    parser.add_argument("--drift-mm", type=float, default=0.5, help="Position drift alert threshold in mm")
    parser.add_argument("--command-timeout", type=float, default=300.0, help="Per-command observable timeout seconds")
    parser.add_argument("--report", type=Path, default=None, help="Write JSON report")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    if args.runs < 1:
        print("ERROR: --runs must be >= 1", file=sys.stderr)
        return 2
    manifest = AcceptanceManifest.load(args.acceptance_manifest)
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
    try:
        print(f"Connecting to live Flex SiLA server at {address}...")
        report = await run_campaign(args, manifest, channel, pb)
    finally:
        await channel.close()
        await connector.stop()

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to {args.report}")

    print("\n== Summary ==")
    print(f"  completion_rate={report['completion_rate']}")
    print(f"  command_failure_rate={report['command_failure_rate']}")
    print(f"  retries={report['retries']}")
    print(f"  mean_cycle_s={report['mean_cycle_s']} min={report['min_cycle_s']} max={report['max_cycle_s']}")
    print(f"  drift_events={len(report['drift_events'])}")
    print(f"\nOVERALL: {'PASS' if report['overall_pass'] else 'FAIL'}")
    return 0 if report["overall_pass"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
