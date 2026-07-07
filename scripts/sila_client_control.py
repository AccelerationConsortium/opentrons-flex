import asyncio
import sys
import base64
import grpc.aio
from sila.server import CommandConfirmation, CommandExecutionUUID
from unitelabs.cdk import SiLAServerConfig
from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.features.motion_control import Mount, Position
from unitelabs.opentrons_flex.features.gripper import GripperStatus
from unitelabs.opentrons_flex.features.pipette import PipetteInfo

_PKG = "sila2.ca.accelerationconsortium.robots.motioncontrolfeature.v1"
_SERVICE = f"{_PKG}.MotionControlFeature"

_GRIPPER_PKG = "sila2.ca.accelerationconsortium.robots.gripperfeature.v1"
_GRIPPER_SERVICE = f"{_GRIPPER_PKG}.GripperFeature"

_PIPETTE_PKG = "sila2.ca.accelerationconsortium.robots.pipettefeature.v1"
_PIPETTE_SERVICE = f"{_PIPETTE_PKG}.PipetteFeature"

async def call_observable(
    channel: grpc.aio.Channel,
    pb: object,
    service: str,
    package: str,
    method: str,
    params: dict | None = None,
    timeout_s: float = 10.0,
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
            details = base64.b64decode(exc.details() or b"")
            result_not_ready = exc.code() is grpc.StatusCode.ABORTED and b"Result is not ready" in details
            if not result_not_ready:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"{method} did not finish within {timeout_s}s") from exc
            await asyncio.sleep(0.05)

async def main():
    robot_ip = "169.254.105.239"
    port = 50051
    address = f"{robot_ip}:{port}"
    
    print("Initializing protobuf codec locally...")
    # Start a mock connector locally just to compile the protobuf codec
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
    
    # Helper to query properties
    async def get_property(service_name: str, package_name: str, name: str) -> dict:
        stub = channel.unary_unary(f"/{service_name}/{name}")
        resp_bytes = await stub(b"")
        return await pb.decode(f"{package_name}.{name}_Responses", resp_bytes)
        
    # Helper to call observable commands
    async def call_command(service_name: str, package_name: str, method: str, params: dict | None = None) -> dict:
        return await call_observable(channel, pb, service_name, package_name, method, params, timeout_s=60.0)

    # MachineStatus check helper
    async def check_machine_status(stage: str):
        res = await get_property(_SERVICE, _PKG, "Get_MachineStatus")
        status = next(iter(res.values()))
        print(f"[{stage}] MachineStatus: estop={status.estop}, door_open={status.door_open}, is_error_state={status.is_error_state}, message={status.message}")
        if status.is_error_state:
            print(f"ERROR: Robot reports error state: {status.message}")
            sys.exit(1)

    # Slot centers absolute coordinate mapping on standard Flex deck
    SLOT_C1 = {"x": 64.0, "y": 150.0}   # Row C, Column 1
    SLOT_C2 = {"x": 228.0, "y": 150.0}  # Row C, Column 2
    SLOT_C3 = {"x": 392.0, "y": 150.0}  # Row C, Column 3

    SAFE_Z_GANTRY = 120.0     # Safe travel height above deck
    ACTION_Z_GANTRY = 100.0   # Safe action height (remains high in the air)

    try:
        # 1. Initial status and verification
        print("\n--- 1. Initial Status Check ---")
        await check_machine_status("START")
        
        res_gripper = await get_property(_GRIPPER_SERVICE, _GRIPPER_PKG, "Get_Status")
        g_status = next(iter(res_gripper.values()))
        print(f"Gripper Attached: {g_status.attached}")
        print(f"Gripper Model: {g_status.model}")
        
        if not g_status.attached:
            print("ERROR: Gripper must be attached for this workflow test.")
            sys.exit(1)
            
        # Query Pipette Status (Check if LEFT pipette has a tip attached)
        print("\n--- 2. Querying Pipette Status ---")
        res_pipettes = await call_command(_PIPETTE_SERVICE, _PIPETTE_PKG, "GetAttachedPipettes")
        pipettes = next(iter(res_pipettes.values()))
        left_pipette = next((p for p in pipettes if p.mount == Mount.LEFT), None)
        
        has_tip = False
        if left_pipette and left_pipette.attached:
            print(f"LEFT Pipette: {left_pipette.name} (ID: {left_pipette.pipette_id})")
            print(f"LEFT Pipette Channels: {left_pipette.channels}, Has Tip: {left_pipette.has_tip}")
            has_tip = left_pipette.has_tip
        else:
            print("Warning: No pipette detected on LEFT mount.")
            
        if not has_tip:
            print("\nNOTE: No physical tip detected on the LEFT pipette nozzle.")
            print("To run the full liquid handling (aspiration/dispense) plunger sequence, attach a tip to the nozzle.")
            print("The script will still move the gantry through all the target slots (C1, C2) to verify coordinates, but will bypass plunger commands.")
        
        # 2. Homing
        print("\n--- 3. Homing Robot ---")
        await call_command(_SERVICE, _PKG, "Home")
        await check_machine_status("after Home")
        
        # 3. PHASE 1: Simulated Labware Transport (Gripper)
        print("\n--- 4. PHASE 1: Labware Transport (Gripper Mount) ---")
        
        # 3.1 Move to Slot C3 (source labware)
        print(f"Moving Gripper to Slot C3 center (X={SLOT_C3['x']}, Y={SLOT_C3['y']}, Z={SAFE_Z_GANTRY}) [Speed: 50 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.GRIPPER, "x": SLOT_C3["x"], "y": SLOT_C3["y"], "z": SAFE_Z_GANTRY, "speed": 50.0
        })
        await check_machine_status("after Gripper Move to C3")
        
        # 3.2 Lower gripper
        print(f"Lowering Gripper to Z={ACTION_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.GRIPPER, "x": SLOT_C3["x"], "y": SLOT_C3["y"], "z": ACTION_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Gripper descend C3")
        
        # 3.3 Ungrip then Grip (simulate picking up plate)
        print("Opening gripper jaws (Ungrip)...")
        await call_command(_GRIPPER_SERVICE, _GRIPPER_PKG, "Ungrip")
        await check_machine_status("after Gripper Ungrip C3")
        
        print("Closing gripper jaws on plate (Grip 5.0 N)...")
        await call_command(_GRIPPER_SERVICE, _GRIPPER_PKG, "Grip", {"force": 5.0})
        await check_machine_status("after Gripper Grip C3")
        
        # 3.4 Raise Gripper
        print(f"Raising Gripper back to Z={SAFE_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.GRIPPER, "x": SLOT_C3["x"], "y": SLOT_C3["y"], "z": SAFE_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Gripper ascend C3")
        
        # 3.5 Move Gripper to Slot C2 (destination labware slot)
        print(f"Moving Gripper to Slot C2 center (X={SLOT_C2['x']}, Y={SLOT_C2['y']}, Z={SAFE_Z_GANTRY}) [Speed: 50 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.GRIPPER, "x": SLOT_C2["x"], "y": SLOT_C2["y"], "z": SAFE_Z_GANTRY, "speed": 50.0
        })
        await check_machine_status("after Gripper Move to C2")
        
        # 3.6 Lower gripper
        print(f"Lowering Gripper to Z={ACTION_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.GRIPPER, "x": SLOT_C2["x"], "y": SLOT_C2["y"], "z": ACTION_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Gripper descend C2")
        
        # 3.7 Ungrip (simulate releasing plate)
        print("Opening gripper jaws (Ungrip)...")
        await call_command(_GRIPPER_SERVICE, _GRIPPER_PKG, "Ungrip")
        await check_machine_status("after Gripper Ungrip C2")
        
        # 3.8 Raise Gripper
        print(f"Raising Gripper back to Z={SAFE_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.GRIPPER, "x": SLOT_C2["x"], "y": SLOT_C2["y"], "z": SAFE_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Gripper ascend C2")
        
        # 4. PHASE 2: Sample Preparation / Liquid Handling (Pipette Left Mount)
        print("\n--- 5. PHASE 2: Sample Preparation (Pipette LEFT Mount) ---")
        
        # 4.1 Move Pipette to Slot C1 (source reagent)
        print(f"Moving Pipette to Slot C1 center (X={SLOT_C1['x']}, Y={SLOT_C1['y']}, Z={SAFE_Z_GANTRY}) [Speed: 50 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.LEFT, "x": SLOT_C1["x"], "y": SLOT_C1["y"], "z": SAFE_Z_GANTRY, "speed": 50.0
        })
        await check_machine_status("after Pipette Move to C1")
        
        # 4.2 Lower pipette
        print(f"Lowering Pipette to Z={ACTION_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.LEFT, "x": SLOT_C1["x"], "y": SLOT_C1["y"], "z": ACTION_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Pipette descend C1")
        
        # 4.3 Reagent Aspiration (only if tip attached)
        if has_tip:
            print("Preparing for aspiration (lowering plunger)...")
            await call_command(_SERVICE, _PKG, "PrepareForAspirate", {"mount": Mount.LEFT})
            await check_machine_status("after PrepareForAspirate C1")
            
            print("Aspirating 20 uL sample reagent [Rate: 1.0]...")
            await call_command(_SERVICE, _PKG, "Aspirate", {"mount": Mount.LEFT, "volume": 20.0, "rate": 1.0})
            await check_machine_status("after Aspirate C1")
        else:
            print("[Skipping plunger preparation & aspiration: No tip attached]")
        
        # 4.4 Raise Pipette
        print(f"Raising Pipette back to Z={SAFE_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.LEFT, "x": SLOT_C1["x"], "y": SLOT_C1["y"], "z": SAFE_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Pipette ascend C1")
        
        # 4.5 Move Pipette to Slot C2 (destination plate)
        print(f"Moving Pipette to Slot C2 center (X={SLOT_C2['x']}, Y={SLOT_C2['y']}, Z={SAFE_Z_GANTRY}) [Speed: 50 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.LEFT, "x": SLOT_C2["x"], "y": SLOT_C2["y"], "z": SAFE_Z_GANTRY, "speed": 50.0
        })
        await check_machine_status("after Pipette Move to C2")
        
        # 4.6 Lower pipette
        print(f"Lowering Pipette to Z={ACTION_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.LEFT, "x": SLOT_C2["x"], "y": SLOT_C2["y"], "z": ACTION_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Pipette descend C2")
        
        # 4.7 Dispense sample & blowout (only if tip attached)
        if has_tip:
            print("Dispensing 20 uL sample [Rate: 1.0]...")
            await call_command(_SERVICE, _PKG, "Dispense", {
                "mount": Mount.LEFT, "volume": 20.0, "rate": 1.0, "push_out": 0.0
            })
            await check_machine_status("after Dispense C2")
            
            print("Executing blowout...")
            await call_command(_SERVICE, _PKG, "BlowOut", {"mount": Mount.LEFT})
            await check_machine_status("after BlowOut C2")
        else:
            print("[Skipping plunger dispense & blowout: No tip attached]")
        
        # 4.8 Raise Pipette
        print(f"Raising Pipette back to Z={SAFE_Z_GANTRY} [Speed: 30 mm/s]...")
        await call_command(_SERVICE, _PKG, "MoveTo", {
            "mount": Mount.LEFT, "x": SLOT_C2["x"], "y": SLOT_C2["y"], "z": SAFE_Z_GANTRY, "speed": 30.0
        })
        await check_machine_status("after Pipette ascend C2")
        
        # 5. Homing to finish
        print("\n--- 6. Homing Robot to Finish ---")
        await call_command(_SERVICE, _PKG, "Home")
        await check_machine_status("after final Home")
        
        print("\n=== Sample Preparation & Labware Transport Workflow Completed Successfully! ===")
        
    except Exception as e:
        print(f"\nError communicating with the robot: {e}")
    finally:
        await channel.close()
        await connector.stop()
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

if __name__ == "__main__":
    asyncio.run(main())
