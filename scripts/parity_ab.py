#!/usr/bin/env python3
"""Native-protocol vs SiLA-primitive parity experiment for the Flex connector.

Answers the question: "does decomposing an experiment into connector primitive
commands preserve the original experimental semantics?"

Arm A (native): upload a generated Python protocol to the connector's embedded
robot-server (HTTP 31950) and execute it through Protocol Engine. The protocol
performs 1x pickup -> Nx (aspirate/dispense at explicit deck coordinates) ->
1x drop.

Arm B (primitive): drive the same physical experiment through the connector's
SiLA gRPC primitives: PickUpTip -> Nx TransferWithVerifiedLiquidClass -> DropTip.

Both arms target the same source and destination coordinates from the
acceptance manifest and both end by weighing the destination container, so the
final liquid distribution is compared directly. The command sequences of both
arms are also recorded for semantic comparison.

Usage
-----
    python scripts/parity_ab.py --robot 169.254.105.239 \\
        --acceptance-manifest /path/to/flex_acceptance.json \\
        --transfers 8 --volume 25 --report ./parity.json

Requires connector-pe mode (embedded robot-server on HTTP 31950 for arm A)
and a tip rack physically placed so that its A1 well matches the manifest
tip_pickup coordinate.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import grpc.aio
import httpx
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

_MOTION_PACKAGE = "sila2.ca.accelerationconsortium.robots.motioncontroller.v2"
_MOTION_SERVICE = f"{_MOTION_PACKAGE}.MotionController"
_PIPETTE_PACKAGE = "sila2.ca.accelerationconsortium.robots.pipettecontroller.v1"
_PIPETTE_SERVICE = f"{_PIPETTE_PACKAGE}.PipetteController"
_TIP_PACKAGE = "sila2.ca.accelerationconsortium.robots.tipcontroller.v1"
_TIP_SERVICE = f"{_TIP_PACKAGE}.TipController"
_LIQUID_PACKAGE = "sila2.ca.accelerationconsortium.robots.liquidhandlingcontroller.v1"
_LIQUID_SERVICE = f"{_LIQUID_PACKAGE}.LiquidHandlingController"

_SCHEMA = "parity-ab/v1"
_HTTP_PORT = 31950
_TERMINAL_RUN_STATES = {"succeeded", "failed", "stopped"}
_TERMINAL_ANALYSIS_STATES = {"completed", "failed"}


# --------------------------------------------------------------------------- #
# Native protocol generation
# --------------------------------------------------------------------------- #


def native_protocol_source(
    *,
    volume: float,
    transfers: int,
    tip_pickup: tuple[float, float, float],
    tip_drop: tuple[float, float, float],
    source: tuple[float, float, float],
    destination: tuple[float, float, float],
) -> str:
    """Generate a minimal API 2.27 protocol performing the exact same moves."""
    dx, dy, dz = tip_drop
    sx, sy, sz = source
    gx, gy, gz = destination
    px, py, pz = tip_pickup
    return f'''\
"""Generated parity arm A: native Protocol Engine execution.

Tip rack A1 must sit at the manifest tip_pickup coordinate
({px}, {py}, {pz}) so both arms pick from the same physical position.
"""
from opentrons import protocol_api
from opentrons.types import Point, Location

metadata = {{"apiLevel": "2.27"}}

def run(protocol: protocol_api.ProtocolContext) -> None:
    pipette = protocol.load_instrument("flex_1channel_1000", "left")
    tip_rack = protocol.load_labware("opentrons_flex_96_tiprack_1000ul", 3)
    pipette.pick_up_tip(tip_rack["A1"])
    for _ in range({transfers}):
        pipette.move_to(Location(Point({sx}, {sy}, {sz}), None))
        pipette.aspirate({volume})
        pipette.move_to(Location(Point({gx}, {gy}, {gz}), None))
        pipette.dispense({volume})
    pipette.move_to(Location(Point({dx}, {dy}, {dz}), None))
    pipette.drop_tip()
'''


# --------------------------------------------------------------------------- #
# HTTP robot-server helpers (arm A)
# --------------------------------------------------------------------------- #


def _response_data(response: httpx.Response, *, action: str) -> dict:
    if not response.is_success:
        raise RuntimeError(f"{action} failed with HTTP {response.status_code}: {response.text}")
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"{action} returned an unexpected response: {payload}")
    return data


def _upload_and_analyze(client: httpx.Client, source: str) -> dict:
    files = [("files", ("parity_native.py", source.encode("utf-8"), "text/x-python"))]
    protocol = _response_data(
        client.post("/protocols", files=files, headers={"Opentrons-Version": "2"}),
        action="protocol upload",
    )
    protocol_id = protocol["id"]
    deadline = time.monotonic() + 120.0
    analysis = None
    while time.monotonic() < deadline:
        protocol = _response_data(client.get(f"/protocols/{protocol_id}"), action="protocol poll")
        summaries = protocol.get("analysisSummaries") or []
        analysis = summaries[-1] if summaries else None
        if analysis and analysis.get("status") in _TERMINAL_ANALYSIS_STATES:
            break
        time.sleep(0.5)
    if analysis is None or analysis.get("status") not in _TERMINAL_ANALYSIS_STATES:
        raise RuntimeError("Native protocol analysis did not finish")
    if analysis.get("status") != "completed":
        raise RuntimeError(f"Native protocol analysis failed: {analysis}")
    return protocol


def _run_native(client: httpx.Client, protocol: dict) -> list[str]:
    run = _response_data(
        client.post(
            "/runs",
            json={"data": {"protocolId": protocol["id"]}},
            headers={"Opentrons-Version": "2"},
        ),
        action="run create",
    )
    run_id = run["id"]
    _response_data(
        client.post(
            f"/runs/{run_id}/actions",
            json={"data": {"actionType": "play"}},
            headers={"Opentrons-Version": "2"},
        ),
        action="run play",
    )
    deadline = time.monotonic() + 1800.0
    while time.monotonic() < deadline:
        run = _response_data(client.get(f"/runs/{run_id}"), action="run poll")
        if run.get("status") in _TERMINAL_RUN_STATES:
            break
        time.sleep(1.0)
    status = run.get("status")
    if status != "succeeded":
        raise RuntimeError(f"Native run did not succeed: {status}")
    commands = _response_data(
        client.get(f"/runs/{run_id}/commands", params={"pageLength": 1000}),
        action="command log",
    )
    items = commands.get("data", [])
    return [str(item.get("commandType")) for item in items]


# --------------------------------------------------------------------------- #
# SiLA primitives (arm B)
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


async def _run_primitives(
    channel: grpc.aio.Channel,
    pb: object,
    manifest: AcceptanceManifest,
    *,
    volume: float,
    transfers: int,
) -> list[str]:
    """Drive the same experiment through SiLA primitives; returns command sequence."""
    p = manifest.pipetting
    pipette_mount = PipetteMount[p.mount]
    tip_pickup = TipLocation(p.tip_pickup.x, p.tip_pickup.y, p.tip_pickup.z)
    tip_drop = TipLocation(p.tip_drop.x, p.tip_drop.y, p.tip_drop.z)
    source_well = WellGeometry(
        p.source_well.x,
        p.source_well.y,
        p.source_well.bottom_z,
        p.source_well.top_z,
        p.source_well.length,
        p.source_well.width,
    )
    destination_well = WellGeometry(
        p.destination_well.x,
        p.destination_well.y,
        p.destination_well.bottom_z,
        p.destination_well.top_z,
        p.destination_well.length,
        p.destination_well.width,
    )
    sequence: list[str] = []
    await _call_observable(channel, pb, _MOTION_SERVICE, _MOTION_PACKAGE, "Home", timeout_s=180.0)
    sequence.append("Home")
    await _call_observable(
        channel,
        pb,
        _TIP_SERVICE,
        _TIP_PACKAGE,
        "PickUpTip",
        {
            "mount": pipette_mount,
            "location": tip_pickup,
            "tip_length": p.tip_length,
            "prep_after": False,
        },
        timeout_s=60.0,
    )
    sequence.append("PickUpTip")
    for _ in range(transfers):
        await _call_observable(
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
                "tiprack_uri": p.tiprack_uri,
            },
            timeout_s=300.0,
        )
        sequence.append("TransferWithVerifiedLiquidClass")
    await _call_observable(
        channel,
        pb,
        _TIP_SERVICE,
        _TIP_PACKAGE,
        "DropTip",
        {"mount": pipette_mount, "location": tip_drop, "home_after": False},
        timeout_s=60.0,
    )
    sequence.append("DropTip")
    return sequence


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def build_report(
    *,
    robot: str,
    volume: float,
    transfers: int,
    native_sequence: list[str],
    primitive_sequence: list[str],
    native_weight_g: float,
    primitive_weight_g: float,
    density_g_ml: float,
    started: float,
) -> dict[str, Any]:
    """Assemble the A/B parity report with measured distribution comparison."""
    expected_total_ul = volume * transfers
    native_ul = native_weight_g / density_g_ml * 1000.0
    primitive_ul = primitive_weight_g / density_g_ml * 1000.0
    native_error_pct = (native_ul - expected_total_ul) / expected_total_ul * 100.0
    primitive_error_pct = (primitive_ul - expected_total_ul) / expected_total_ul * 100.0
    arm_diff_pct = abs(native_ul - primitive_ul) / expected_total_ul * 100.0
    return {
        "schema": _SCHEMA,
        "robot": robot,
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.monotonic() - started, 3),
        "volume_ul": volume,
        "transfers": transfers,
        "expected_total_ul": expected_total_ul,
        "native": {
            "command_sequence": native_sequence,
            "command_count": len(native_sequence),
            "measured_ul": round(native_ul, 4),
            "error_pct": round(native_error_pct, 4),
        },
        "primitive": {
            "command_sequence": primitive_sequence,
            "command_count": len(primitive_sequence),
            "measured_ul": round(primitive_ul, 4),
            "error_pct": round(primitive_error_pct, 4),
        },
        "arm_difference_pct": round(arm_diff_pct, 4),
        "overall_pass": native_error_pct <= 2.0 and primitive_error_pct <= 2.0 and arm_diff_pct <= 2.0,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot", required=True, help=f"Flex hostname or IP (HTTP port {_HTTP_PORT} for arm A)")
    parser.add_argument("--acceptance-manifest", type=Path, required=True, help="Validated acceptance manifest")
    parser.add_argument("--transfers", type=int, default=8, help="Number of transfers per arm")
    parser.add_argument("--volume", type=float, default=25.0, help="Transfer volume in uL")
    parser.add_argument("--density", type=float, default=0.9982, help="Water density g/mL")
    parser.add_argument("--report", type=Path, default=None, help="Write JSON report")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    if args.transfers < 1:
        print("ERROR: --transfers must be >= 1", file=sys.stderr)
        return 2
    manifest = AcceptanceManifest.load(args.acceptance_manifest)
    started = time.monotonic()
    p = manifest.pipetting
    tip_pickup = (p.tip_pickup.x, p.tip_pickup.y, p.tip_pickup.z)
    tip_drop = (p.tip_drop.x, p.tip_drop.y, p.tip_drop.z)
    source = (p.source.x, p.source.y, p.source.z)
    destination = (p.destination.x, p.destination.y, p.destination.z)

    # ---- Arm A: native protocol through embedded robot-server ------------ #
    print("== Arm A: native Protocol Engine execution ==")
    http_base = f"http://{args.robot}:{_HTTP_PORT}"
    source_code = native_protocol_source(
        volume=args.volume,
        transfers=args.transfers,
        tip_pickup=tip_pickup,
        tip_drop=tip_drop,
        source=source,
        destination=destination,
    )
    with httpx.Client(base_url=http_base, timeout=30.0) as client:
        health = client.get("/health")  # noqa: ASYNC212
        if health.status_code != 200:
            raise RuntimeError(
                f"Embedded robot-server is not healthy at {http_base}: "
                f"{health.status_code} {health.text[:300]} (connector-pe mode required)"
            )
        protocol = _upload_and_analyze(client, source_code)
        native_sequence = _run_native(client, protocol)
    print(f"  native command sequence ({len(native_sequence)}): {native_sequence}")

    # ---- Arm B: SiLA primitives ------------------------------------------ #
    print("== Arm B: SiLA primitive execution ==")
    grpc_address = args.robot if ":" in args.robot else f"{args.robot}:50051"
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
    channel = grpc.aio.insecure_channel(grpc_address)
    try:
        primitive_sequence = await _run_primitives(channel, pb, manifest, volume=args.volume, transfers=args.transfers)
    finally:
        await channel.close()
        await connector.stop()
    print(f"  primitive command sequence ({len(primitive_sequence)}): {primitive_sequence}")

    # ---- Measurement: weigh the destination container -------------------- #
    print("\nWeigh the destination container and enter the reading (g):")
    native_weight = float(input("  weight after NATIVE arm (g) > ").strip())  # noqa: ASYNC250
    print("(Empty/reset the destination container between arms)")
    primitive_weight = float(input("  weight after PRIMITIVE arm (g) > ").strip())  # noqa: ASYNC250

    report = build_report(
        robot=args.robot,
        volume=args.volume,
        transfers=args.transfers,
        native_sequence=native_sequence,
        primitive_sequence=primitive_sequence,
        native_weight_g=native_weight,
        primitive_weight_g=primitive_weight,
        density_g_ml=args.density,
        started=started,
    )
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to {args.report}")

    print("\n== Summary ==")
    print(
        f"  expected {report['expected_total_ul']}uL | native {report['native']['measured_ul']}uL "
        f"({report['native']['error_pct']:+.2f}%) | primitive {report['primitive']['measured_ul']}uL "
        f"({report['primitive']['error_pct']:+.2f}%) | arm diff {report['arm_difference_pct']:.2f}%"
    )
    print(f"\nOVERALL: {'PASS' if report['overall_pass'] else 'FAIL'}")
    return 0 if report["overall_pass"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
