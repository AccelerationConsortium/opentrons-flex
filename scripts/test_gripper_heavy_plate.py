"""Targeted gripper test: transport heavy plate from slot B3 to B4.

Uses the SiLA connector's GripperFeature with adjustable grip/ungrip force
to pick up a heavy plate at B3 and place it at B4.

Usage:
    python scripts/test_gripper_heavy_plate.py --host 169.254.105.239 [--port 50051]

    FLEX_SILA_HOST=169.254.105.239 python scripts/test_gripper_heavy_plate.py
"""

import argparse
import asyncio
import contextlib
import os
import sys

import grpc.aio
from sila.server import CommandConfirmation, CommandExecutionUUID
from unitelabs.cdk import SiLAServerConfig
from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.features.motion_control import Mount

_MOTION_PKG = "sila2.ca.accelerationconsortium.robots.motioncontrolfeature.v2"
_MOTION_SERVICE = f"{_MOTION_PKG}.MotionControlFeature"

_GRIPPER_PKG = "sila2.ca.accelerationconsortium.robots.grippercontroller.v1"
_GRIPPER_SERVICE = f"{_GRIPPER_PKG}.GripperController"


async def call_observable(
    channel: grpc.aio.Channel,
    pb: object,
    service: str,
    package: str,
    method: str,
    params: dict | None = None,
    timeout_s: float = 60.0,
) -> dict:
    """Start an observable command, poll its result, and decode the response."""
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
            return await pb.decode(f"{package}.{method}_Responses", resp_bytes)
        except grpc.aio.AioRpcError as exc:
            # "Result is not ready" is expected while the command runs
            err_msg = exc.details() or ""
            if exc.code() is grpc.StatusCode.ABORTED and "Result is not ready" in err_msg:
                pass
            elif exc.code() is not grpc.StatusCode.ABORTED:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"{method} did not finish within {timeout_s}s") from exc
            await asyncio.sleep(0.5)


# ── Slot centre coordinates (Flex deck, absolute mm, v4 definition) ──────────
# Cutout offset + half-bounding-box (128×86 → 64, 43).
SLOT_B3 = {"x": 392.0, "y": 257.0}  # B row, col 3
SLOT_B4 = {"x": 556.0, "y": 257.0}  # B row, col 4

SAFE_Z = 120.0   # safe travel height above deck
PICK_Z = 75.0    # Z to lower gripper onto plate (tune for your labware)

# Grip force for heavy plates – bump to 18 N (well within 5-25 N range)
HEAVY_GRIP_FORCE_N = 18.0
HEAVY_UNGRIP_FORCE_N = 18.0


async def main(robot_ip: str, port: int):
    address = f"{robot_ip}:{port}"

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

    print(f"Connecting to live robot SiLA server at {address}...")
    channel = grpc.aio.insecure_channel(address)

    async def get_property(service_name: str, package_name: str, name: str) -> dict:
        stub = channel.unary_unary(f"/{service_name}/{name}")
        resp_bytes = await stub(b"")
        return await pb.decode(f"{package_name}.{name}_Responses", resp_bytes)

    async def call_command(service_name: str, package_name: str, method: str, params: dict | None = None) -> dict:
        return await call_observable(channel, pb, service_name, package_name, method, params, timeout_s=60.0)

    async def check_machine_status(stage: str):
        res = await get_property(_MOTION_SERVICE, _MOTION_PKG, "Get_MachineStatus")
        status = next(iter(res.values()))
        print(
            f"  [{stage}] estop={status.estop} door={status.door_open} "
            f"error={status.is_error_state} msg={status.message!r}"
        )
        if status.is_error_state:
            print(f"FATAL: Robot reports error state: {status.message}")
            sys.exit(1)

    try:
        # ── 0. Status + gripper check ────────────────────────────────────────
        print("\n=== B3→B4 Heavy-Plate Gripper Test ===\n")
        print(f"Grip force:  {HEAVY_GRIP_FORCE_N} N")
        print(f"Ungrip force: {HEAVY_UNGRIP_FORCE_N} N\n")

        await check_machine_status("start")

        res_gripper = await get_property(_GRIPPER_SERVICE, _GRIPPER_PKG, "Get_Status")
        g_status = next(iter(res_gripper.values()))
        print(f"Gripper attached: {g_status.attached}")
        if not g_status.attached:
            print("FATAL: Gripper must be attached for this test.")
            sys.exit(1)
        jaw = await get_property(_GRIPPER_SERVICE, _GRIPPER_PKG, "Get_JawWidth")
        jw = next(iter(jaw.values()))
        print(f"Initial jaw width: {jw:.1f} mm")

        # ── 1. Move gripper to B3 (above deck) ──────────────────────────────
        print(f"\n--- 1. Move Gripper to B3 safe Z={SAFE_Z} ---")
        await call_command(
            _MOTION_SERVICE, _MOTION_PKG, "MoveTo",
            {"mount": Mount.GRIPPER, "x": SLOT_B3["x"], "y": SLOT_B3["y"], "z": SAFE_Z, "speed": 50.0},
        )
        await check_machine_status("B3-safe")

        # ── 2. Lower to pick height ─────────────────────────────────────────
        print(f"--- 2. Lower Gripper to B3 pick Z={PICK_Z} ---")
        await call_command(
            _MOTION_SERVICE, _MOTION_PKG, "MoveTo",
            {"mount": Mount.GRIPPER, "x": SLOT_B3["x"], "y": SLOT_B3["y"], "z": PICK_Z, "speed": 30.0},
        )
        await check_machine_status("B3-pick")

        # ── 3. Ungrip (open jaw) then Grip (close on plate) ─────────────────
        print(f"--- 3. Ungrip (open) then Grip at {HEAVY_GRIP_FORCE_N} N ---")
        await call_command(_GRIPPER_SERVICE, _GRIPPER_PKG, "Ungrip", {"force": HEAVY_UNGRIP_FORCE_N})
        await check_machine_status("B3-ungrip")
        await call_command(_GRIPPER_SERVICE, _GRIPPER_PKG, "Grip", {"force": HEAVY_GRIP_FORCE_N})
        await check_machine_status("B3-grip")

        jaw = await get_property(_GRIPPER_SERVICE, _GRIPPER_PKG, "Get_JawWidth")
        jw = next(iter(jaw.values()))
        print(f"  Jaw width after grip: {jw:.1f} mm")

        # ── 4. Raise gripper ────────────────────────────────────────────────
        print(f"--- 4. Raise Gripper to safe Z={SAFE_Z} ---")
        await call_command(
            _MOTION_SERVICE, _MOTION_PKG, "MoveTo",
            {"mount": Mount.GRIPPER, "x": SLOT_B3["x"], "y": SLOT_B3["y"], "z": SAFE_Z, "speed": 30.0},
        )
        await check_machine_status("B3-ascend")

        # ── 5. Move gripper to B4 ───────────────────────────────────────────
        print(f"--- 5. Move Gripper to B4 safe Z={SAFE_Z} ---")
        await call_command(
            _MOTION_SERVICE, _MOTION_PKG, "MoveTo",
            {"mount": Mount.GRIPPER, "x": SLOT_B4["x"], "y": SLOT_B4["y"], "z": SAFE_Z, "speed": 50.0},
        )
        await check_machine_status("B4-safe")

        # ── 6. Lower to place height ────────────────────────────────────────
        print(f"--- 6. Lower Gripper to B4 place Z={PICK_Z} ---")
        await call_command(
            _MOTION_SERVICE, _MOTION_PKG, "MoveTo",
            {"mount": Mount.GRIPPER, "x": SLOT_B4["x"], "y": SLOT_B4["y"], "z": PICK_Z, "speed": 30.0},
        )
        await check_machine_status("B4-place")

        # ── 7. Ungrip (release plate) ───────────────────────────────────────
        print(f"--- 7. Ungrip (release) at {HEAVY_UNGRIP_FORCE_N} N ---")
        await call_command(_GRIPPER_SERVICE, _GRIPPER_PKG, "Ungrip", {"force": HEAVY_UNGRIP_FORCE_N})
        await check_machine_status("B4-ungrip")

        jaw = await get_property(_GRIPPER_SERVICE, _GRIPPER_PKG, "Get_JawWidth")
        jw = next(iter(jaw.values()))
        print(f"  Jaw width after release: {jw:.1f} mm")

        # ── 8. Raise gripper ────────────────────────────────────────────────
        print(f"--- 8. Raise Gripper to safe Z={SAFE_Z} ---")
        await call_command(
            _MOTION_SERVICE, _MOTION_PKG, "MoveTo",
            {"mount": Mount.GRIPPER, "x": SLOT_B4["x"], "y": SLOT_B4["y"], "z": SAFE_Z, "speed": 30.0},
        )
        await check_machine_status("B4-ascend")

        print("\n=== B3→B4 Heavy-Plate Gripper Test PASSED ===")

    except Exception as exc:
        print(f"\nTEST FAILED: {exc}")
        sys.exit(1)
    finally:
        await channel.close()
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test gripper heavy-plate transport B3→B4 via SiLA connector.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FLEX_SILA_HOST"),
        help="Robot SiLA server IP/hostname (or set FLEX_SILA_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FLEX_SILA_PORT", "50051")),
        help="Robot SiLA server port.",
    )
    args = parser.parse_args()
    if not args.host:
        parser.error("robot host is required: pass --host <ip> or set FLEX_SILA_HOST")
    return args


if __name__ == "__main__":
    _args = _parse_args()
    asyncio.run(main(_args.host, _args.port))
