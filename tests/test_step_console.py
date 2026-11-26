"""The step console's operations, sent over a real gRPC connection to the OT3 simulator connector."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import grpc.aio
import pytest
import pytest_asyncio
from opentrons.hardware_control.types import OT3Mount
from unitelabs.cdk import SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.asms_sila_workflow import AsmsSilaClient
from unitelabs.opentrons_flex.features.motion_control import Mount
from unitelabs.opentrons_flex.step_console import (
    TRAVEL_Z,
    Console,
    StepError,
    StepRunner,
    TipRackTracker,
    slot_centre,
    tip_well_location,
)

ROOT = Path(__file__).resolve().parents[1]

_INSTRUMENTS = {
    OT3Mount.LEFT: {"model": "p50_multi_v3.5", "id": "P50M-SIM-1"},
    OT3Mount.RIGHT: {"model": "p1000_multi_v3.5", "id": "P1KM-SIM-1"},
    OT3Mount.GRIPPER: {"model": "gripperV1", "id": "GRIPPER-SIM-1"},
}


async def _serve(**extra):
    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_gripper=True,
        simulated_temperature_module=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
        **extra,
    )
    with patch("unitelabs.opentrons_flex._simulator_attached_instruments", return_value=_INSTRUMENTS):
        application = create_app(config)
        server = await application.__anext__()
        await server.start()
        channel = grpc.aio.insecure_channel(server.sila_server._address)
        try:
            yield AsmsSilaClient(channel, server.sila_server.protobuf)
        finally:
            await channel.close()
            await server.stop()
            with pytest.raises(StopAsyncIteration):
                await application.__anext__()


@pytest_asyncio.fixture
async def client():
    async for connected in _serve():
        yield connected


@pytest_asyncio.fixture
async def unit_client(tmp_path: Path):
    """The connector-unit configuration: preset gripper plans, direct jaw control disabled."""
    movement = json.loads((ROOT / "config/asms_unit_labware_movement.json").read_text())
    movement["state_file"] = str(tmp_path / "deck-state.json")
    movement_path = tmp_path / "labware-movement.json"
    movement_path.write_text(json.dumps(movement))
    async for connected in _serve(labware_movement_config=str(movement_path)):
        yield connected


def _guard_moves(client: AsmsSilaClient, moves: list) -> None:
    """Fail on any MoveTo that is neither vertical nor level at or above the travel height."""
    original = client.move_to

    async def move_to(mount: Mount, point):
        here = await client.position(mount)
        vertical = abs(here["x"] - point["x"]) < 0.01 and abs(here["y"] - point["y"]) < 0.01
        level = abs(here["z"] - point["z"]) < 0.01
        assert vertical or level, f"diagonal move on {mount.value} from {here} to {point}"
        if level and not vertical:
            assert point["z"] >= TRAVEL_Z, f"horizontal move below travel height on {mount.value}: {point}"
        moves.append((mount.value, dict(point)))
        return await original(mount, point)

    client.move_to = move_to  # type: ignore[method-assign]


def test_geometry_matches_the_protocol_engine_coordinates() -> None:
    # Pickups Protocol Engine analysed for the AS-MS protocols: wash/elute A1 tips, prep H1 tips.
    location = tip_well_location("C3", "A1")
    assert (location.x, location.y, location.z) == (342.38, 181.38, 99.0)
    location = tip_well_location("A3", "A1")
    assert (location.x, location.y, location.z) == (342.38, 395.38, 99.0)
    location = tip_well_location("C3", "H1")
    assert (location.x, location.y) == (342.38, 118.38)
    location = tip_well_location("A3", "H1")
    assert (location.x, location.y) == (342.38, 332.38)
    assert tip_well_location("C3", "A5").x == pytest.approx(378.38)
    assert slot_centre("d2") == (228.0, 43.0)
    with pytest.raises(StepError, match="A1-D3"):
        slot_centre("E9")
    with pytest.raises(StepError, match="A1-H12"):
        tip_well_location("C3", "A13")


def test_single_tips_must_come_from_row_h_upwards() -> None:
    racks = TipRackTracker()
    rack = "opentrons_flex_96_tiprack_1000ul"

    with pytest.raises(StepError, match=r"press onto B1, C1, D1, E1, F1, G1, H1.*first H1"):
        racks.check_single("C3", rack, "A1")
    racks.check_single("C3", rack, "H1")
    racks.take("C3", rack, ["H1"])
    with pytest.raises(StepError, match="already taken"):
        racks.check_single("C3", rack, "H1")
    racks.check_single("C3", rack, "G1")
    with pytest.raises(StepError, match="not full"):
        racks.check_column("C3", rack, 1)
    racks.check_column("C3", rack, 2)

    racks.refill("C3")
    racks.check_column("C3", rack, 1)


@pytest.mark.asyncio
async def test_a_built_sequence_runs_step_by_step_and_stays_in_place(client: AsmsSilaClient) -> None:
    moves: list = []
    _guard_moves(client, moves)
    console = Console(StepRunner(client))

    async def step(kind: str, **params):
        result = await console.run_step(kind, params)
        assert result["ok"], result
        return result

    await step("home")
    # The eight nozzles are centred over D2, so the back-most (A1) nozzle sits 31.5 mm behind the centre.
    await step("move_to_slot", mount="RIGHT", slot="D2", z=120)
    assert await client.position(Mount.RIGHT) == pytest.approx({"x": 228.0, "y": 74.5, "z": 120.0})

    # The next step starts from where the last one stopped: no home in between.
    await step("move_relative", mount="RIGHT", dx=10, dy=0, dz=-20)
    assert await client.position(Mount.RIGHT) == pytest.approx({"x": 238.0, "y": 74.5, "z": 100.0})

    # Over tip A1 of a rack in D2, 10 mm above the rack top: the nozzle column lines up with rack column 1.
    await step("move_to_tip_well", mount="RIGHT", slot="D2", well="A1", clearance=10)
    assert await client.position(Mount.RIGHT) == pytest.approx({"x": 178.38, "y": 74.38, "z": 109.0})

    # Moving the other pipette first lifts the lowered RIGHT mount at least to the travel height.
    await step("move_to_slot", mount="LEFT", slot="B1", z=130)
    assert (await client.position(Mount.RIGHT))["z"] >= TRAVEL_Z

    picked = await step(
        "pick_up_tip", mount="RIGHT", tiprack="opentrons_flex_96_tiprack_1000ul", slot="C3", mode="column", column=1
    )
    assert "PRESENT" in picked["message"]
    await step("move_to_slot", mount="RIGHT", slot="C2", z=110)
    dropped = await step("drop_tip_trash", mount="RIGHT")
    assert "ABSENT" in dropped["message"]

    single = await step(
        "pick_up_tip", mount="RIGHT", tiprack="opentrons_flex_96_tiprack_1000ul", slot="C3", mode="single", well="H2"
    )
    assert "H2" in single["message"]
    assert "PRESENT" in single["message"]
    refused = await console.run_step(
        "pick_up_tip", {"mount": "LEFT", "tiprack": "opentrons_flex_96_tiprack_1000ul", "slot": "C3", "well": "F2"}
    )
    assert "G2" in refused["error"]
    await step("drop_tip_trash", mount="RIGHT")

    await step("gripper_home_jaw")
    await step("move_to_slot", mount="GRIPPER", slot="B2", z=120)
    await step("gripper_close", force=15)
    await step("gripper_open", force=15)
    assert await client.position(Mount.GRIPPER) == pytest.approx({"x": 228.0, "y": 257.0, "z": 120.0})

    assert {mount for mount, _point in moves} == {"LEFT", "RIGHT", "GRIPPER"}
    state = await console.state()
    assert state["simulating"] is True
    assert state["tips"]["RIGHT"] == "ABSENT"
    assert state["racks"]["C3"]["taken"] == ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1", "H2"]


@pytest.mark.asyncio
async def test_invalid_steps_are_refused_before_anything_moves(client: AsmsSilaClient) -> None:
    console = Console(StepRunner(client))

    assert (await console.run_step("teleport", {}))["error"] == "Unknown step 'teleport'."
    assert "A1-D3" in (await console.run_step("move_to_slot", {"mount": "RIGHT", "slot": "Z9"}))["error"]
    assert "LEFT, RIGHT" in (await console.run_step("pick_up_tip", {"mount": "GRIPPER"}))["error"]
    assert "5-25 N" in (await console.run_step("gripper_close", {"force": 40}))["error"]


@pytest.mark.asyncio
async def test_stop_cancels_the_running_step_and_halts_the_robot(client: AsmsSilaClient) -> None:
    console = Console(StepRunner(client))
    assert (await console.run_step("home", {}))["ok"]
    assert console.stop() is False

    # Simulated moves finish instantly; hold MoveTo open so Stop lands mid-command.
    original = client._poll_observable
    moving = asyncio.Event()

    async def slow_move(service, package, method, parameters, *, timeout):  # noqa: ASYNC109 - mirrors the patched method
        if method == "MoveTo":
            moving.set()
            await asyncio.sleep(30)
        return await original(service, package, method, parameters, timeout=timeout)

    client._poll_observable = slow_move  # type: ignore[method-assign]
    running = asyncio.create_task(console.run_step("move_to_slot", {"mount": "RIGHT", "slot": "D3", "z": 100}))
    await asyncio.wait_for(moving.wait(), 10)
    assert console.stop() is True
    result = await running

    assert result["stopped"] is True
    assert "EmergencyStop" in result["error"]
    client._poll_observable = original  # type: ignore[method-assign]
    assert (await console.run_step("recover", {}))["ok"]


@pytest.mark.asyncio
async def test_gripper_moves_home_its_z_first_when_the_connector_lost_it(client: AsmsSilaClient) -> None:
    runner = StepRunner(client)
    await runner.run("home", {})
    runner._gripper_z_known = False
    homed: list[str] = []
    original = client._observable

    async def record(service, package, method, parameters=None, **kwargs):
        homed.append(method)
        return await original(service, package, method, parameters, **kwargs)

    client._observable = record  # type: ignore[method-assign]
    assert "GRIPPER" not in (await runner.snapshot())["positions"]
    homed.clear()

    await runner.run("move_to_slot", {"mount": "GRIPPER", "slot": "B2", "z": 150})

    assert homed[0] == "HomeMount"
    assert runner._gripper_z_known is True
    assert (await runner.snapshot())["positions"]["GRIPPER"]["z"] == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_unit_mode_moves_labware_only_along_preset_plans(unit_client: AsmsSilaClient) -> None:
    console = Console(StepRunner(unit_client))
    assert (await console.run_step("home", {}))["ok"]

    refused = await console.run_step("gripper_close", {"force": 15})
    assert "needs connector-pe mode" in refused["error"]
    assert "--robot" in refused["error"]
    assert (await console.run_step("gripper_home_jaw", {}))["ok"]

    info = await console.info()
    assert {plan["id"] for plan in info["plans"]} >= {"asms-v1-a1-to-b2"}
    assert info["deck"]["occupied"] == {"A1": "asms-plate", "B1": "elution-plate"}
    moved = await console.run_step("move_labware", {"plan": "asms-v1-a1-to-b2"})
    assert moved["ok"], moved
    assert "asms-plate moved to B2" in moved["message"]


@pytest.mark.asyncio
async def test_pick_and_place_plate_are_one_vertical_gripper_action_each(client: AsmsSilaClient) -> None:
    moves: list = []
    _guard_moves(client, moves)
    gripper_calls: list[str] = []
    original = client._observable

    async def record(service, package, method, parameters=None, **kwargs):
        if "grippercontroller" in service:
            gripper_calls.append(method)
        return await original(service, package, method, parameters, **kwargs)

    client._observable = record  # type: ignore[method-assign]
    console = Console(StepRunner(client))
    assert (await console.run_step("home", {}))["ok"]

    picked = await console.run_step("pick_plate", {"slot": "A1", "grip_z": 7.175, "force": 15})
    assert picked["ok"], picked
    assert await client.position(Mount.GRIPPER) == pytest.approx({"x": 64.0, "y": 364.0, "z": TRAVEL_Z})
    placed = await console.run_step("place_plate", {"slot": "C2", "grip_z": 7.175, "force": 15})
    assert placed["ok"], placed
    assert await client.position(Mount.GRIPPER) == pytest.approx({"x": 228.0, "y": 150.0, "z": TRAVEL_Z})

    assert gripper_calls == ["Ungrip", "Grip", "Ungrip"]
    gripper_moves = [point for mount, point in moves if mount == "GRIPPER"]
    assert {"x": 64.0, "y": 364.0, "z": 7.175} in gripper_moves
    assert {"x": 228.0, "y": 150.0, "z": 7.175} in gripper_moves


@pytest.mark.asyncio
async def test_pick_plate_fails_before_moving_where_direct_jaw_control_is_disabled(unit_client: AsmsSilaClient) -> None:
    console = Console(StepRunner(unit_client))
    assert (await console.run_step("home", {}))["ok"]
    before = await unit_client.position(Mount.GRIPPER)

    refused = await console.run_step("pick_plate", {"slot": "A1", "grip_z": 7.175, "force": 15})

    assert "needs connector-pe mode" in refused["error"]
    assert await unit_client.position(Mount.GRIPPER) == pytest.approx(before)


class _RecordingSwitcher:
    def __init__(self) -> None:
        self.modes: list[str] = []

    async def switch(self, mode: str) -> None:
        self.modes.append(mode)


@pytest.mark.asyncio
async def test_a_step_needing_the_other_mode_switches_it_then_homes(unit_client: AsmsSilaClient) -> None:
    switcher = _RecordingSwitcher()
    runner = StepRunner(unit_client, switcher)  # type: ignore[arg-type]
    homes: list[str] = []
    original = unit_client.home

    async def home() -> None:
        homes.append("home")
        await original()

    unit_client.home = home  # type: ignore[method-assign]
    assert await runner.mode() == "unit"
    assert await runner.run("gripper_home_jaw", {}) == "Gripper jaw homed (fully open)."
    assert switcher.modes == []

    # The simulated connector cannot really restart in pe mode, so the jaw command itself is still refused.
    with pytest.raises(grpc.aio.AioRpcError):
        await runner.run("gripper_close", {"force": 15})
    assert switcher.modes == ["pe"]
    assert homes == ["home"]


@pytest.mark.asyncio
async def test_no_mode_switch_while_holding_a_plate_or_carrying_a_tip(client: AsmsSilaClient) -> None:
    switcher = _RecordingSwitcher()
    console = Console(StepRunner(client, switcher))  # type: ignore[arg-type]
    assert (await console.run_step("home", {}))["ok"]
    assert (await console.run_step("pick_plate", {"slot": "A1", "grip_z": 7.175, "force": 15}))["ok"]

    holding = await console.run_step("move_labware", {"plan": "asms-v1-a1-to-b2"})
    assert "may be holding a plate" in holding["error"]

    assert (await console.run_step("place_plate", {"slot": "A1", "grip_z": 7.175, "force": 15}))["ok"]
    assert (await console.run_step("gripper_home_jaw", {}))["ok"]
    picked = await console.run_step(
        "pick_up_tip", {"mount": "RIGHT", "tiprack": "opentrons_flex_96_tiprack_1000ul", "slot": "C3", "well": "H1"}
    )
    assert picked["ok"], picked
    carrying = await console.run_step("move_labware", {"plan": "asms-v1-a1-to-b2"})
    assert "RIGHT carries a tip" in carrying["error"]
    assert switcher.modes == []


@pytest.mark.asyncio
async def test_preset_plans_stay_selectable_in_pe_mode(client: AsmsSilaClient) -> None:
    info = await Console(StepRunner(client)).info()

    assert await StepRunner(client).mode() == "pe"
    assert "asms-v1-a1-to-b2" in {plan["id"] for plan in info["plans"]}


class _SlowSwitcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = False

    async def switch(self, _mode: str) -> None:
        self.started.set()
        await asyncio.sleep(0.5)
        self.finished = True


@pytest.mark.asyncio
async def test_stop_during_a_mode_switch_waits_for_it_and_reports_progress(unit_client: AsmsSilaClient) -> None:
    switcher = _SlowSwitcher()
    console = Console(StepRunner(unit_client, switcher))  # type: ignore[arg-type]

    running = asyncio.create_task(console.run_step("gripper_close", {"force": 15}))
    await asyncio.wait_for(switcher.started.wait(), 10)
    state = await console.state()
    assert state["busy"] is True
    assert "connector-unit to connector-pe" in state["activity"]

    assert console.stop() is True
    result = await running

    assert result["stopped"] is True
    assert switcher.finished is True
    assert "mode switch in progress was allowed to finish" in result["error"]


@pytest.mark.asyncio
async def test_home_is_refused_while_a_plate_may_be_in_the_jaw(client: AsmsSilaClient) -> None:
    console = Console(StepRunner(client))
    assert (await console.run_step("home", {}))["ok"]
    assert (await console.run_step("pick_plate", {"slot": "A1", "grip_z": 7.175, "force": 15}))["ok"]

    for kind in ("home", "recover"):
        refused = await console.run_step(kind, {})
        assert "plate would fall" in refused["error"]

    assert (await console.run_step("gripper_empty", {}))["ok"]
    assert (await console.run_step("home", {}))["ok"]


_RECOVERY = "sila2.ca.accelerationconsortium.robots.recoverycontroller.v1"
_MOTION = "sila2.ca.accelerationconsortium.robots.motioncontroller.v2"


@pytest.mark.asyncio
async def test_release_gripper_in_place_opens_only_the_jaw_while_recovery_is_pending(client: AsmsSilaClient) -> None:
    console = Console(StepRunner(client))
    assert (await console.run_step("home", {}))["ok"]
    assert (await console.run_step("pick_plate", {"slot": "A1", "grip_z": 7.175, "force": 15}))["ok"]

    # Not allowed while the robot is ready: the usual jaw commands apply then.
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await client._observable(f"{_RECOVERY}.RecoveryController", _RECOVERY, "ReleaseGripperInPlace")
    assert "GripperRecoveryNotAllowedError" in console_error(refused.value)

    await client._observable(f"{_MOTION}.MotionController", _MOTION, "EmergencyStop", emergency_on_failure=False)
    before = await client.position(Mount.RIGHT)
    lifted = await console.run_step("lift_gripper_in_place", {})
    assert lifted["ok"], lifted
    assert (await client.position(Mount.GRIPPER))["z"] > TRAVEL_Z
    assert "GRIPPING" in str((await client.gripper_status()).state)
    released = await console.run_step("release_gripper_in_place", {})
    assert released["ok"], released

    assert "GRIPPING" not in str((await client.gripper_status()).state)
    assert await client.position(Mount.RIGHT) == pytest.approx(before)
    # Recover is still required, and then succeeds.
    blocked = await console.run_step("move_to_slot", {"mount": "RIGHT", "slot": "D2", "z": 150})
    assert not blocked["ok"]
    assert (await console.run_step("gripper_empty", {}))["ok"]
    assert (await console.run_step("recover", {}))["ok"]


def console_error(exc: BaseException) -> str:
    from unitelabs.opentrons_flex.step_console import _error_text

    return _error_text(exc)


def test_store_tracks_inventory_tips_liquids_and_workflows(tmp_path: Path) -> None:
    from unitelabs.opentrons_flex.console_store import ConsoleStore

    store = ConsoleStore(tmp_path / "console.db")
    store.set_labware("C2", "nest_96_wellplate_2ml_deep", "samples")
    store.set_liquid("C2", "A1", "Water", 500)
    assert store.remove_volume("C2", "A1", 120) == "Water"
    store.add_volume("C2", "A2", "Water", 120)
    store.take_tips("C3", ["H1", "G1"])
    store.save_workflow("demo", [{"kind": "home", "params": {}}])

    reopened = ConsoleStore(tmp_path / "console.db")
    assert reopened.labware()["C2"]["label"] == "samples"
    assert {(item["well"], item["volume"]) for item in reopened.liquids()} == {("A1", 380.0), ("A2", 120.0)}
    assert reopened.used_tips("C3") == {"G1", "H1"}
    assert reopened.workflow("demo") == [{"kind": "home", "params": {}}]
    # Replacing the labware in a slot starts it fresh.
    reopened.set_labware("C2", "nest_12_reservoir_15ml")
    assert reopened.liquids() == []


@pytest.mark.asyncio
async def test_manual_liquid_steps_update_the_well_volumes(client: AsmsSilaClient) -> None:
    from unitelabs.opentrons_flex.console_store import ConsoleStore

    store = ConsoleStore(":memory:")
    store.set_labware("C3", "opentrons_flex_96_tiprack_1000ul")
    store.set_labware("C2", "nest_96_wellplate_2ml_deep")
    store.set_liquid("C2", "A1", "Water", 500)
    console = Console(StepRunner(client, store=store))

    async def step(kind: str, **params):
        result = await console.run_step(kind, params)
        assert result["ok"], result
        return result["message"]

    await step("home")
    await step("pick_up_tip", mount="RIGHT", slot="C3", mode="single", well="H1")
    refused = await console.run_step("aspirate", {"mount": "RIGHT", "volume": 100})
    assert "not prepared" in refused["error"]
    await step("move_to_well", mount="RIGHT", slot="C2", well="A1", origin="top", offset=5)
    await step("prepare_for_aspirate", mount="RIGHT")
    await step("move_to_well", mount="RIGHT", slot="C2", well="A1", origin="bottom", offset=2)
    assert "from C2 A1" in await step("aspirate", mount="RIGHT", volume=100)
    await step("move_to_well", mount="RIGHT", slot="C2", well="A2", origin="top", offset=-2)
    assert "into C2 A2" in await step("dispense", mount="RIGHT", volume=100, push_out="default")

    assert {(item["well"], item["liquid"], item["volume"]) for item in store.liquids()} == {
        ("A1", "Water", 400.0),
        ("A2", "Water", 100.0),
    }
    assert store.used_tips("C3") == {"H1"}
    assert store.history(1)[0]["kind"] == "dispense"


@pytest.mark.asyncio
async def test_an_uploaded_protocol_becomes_editable_steps_that_run(client: AsmsSilaClient, tmp_path: Path) -> None:
    from unitelabs.opentrons_flex.console_store import ConsoleStore
    from unitelabs.opentrons_flex.step_console import import_protocol

    source = (ROOT / "tests/fixtures/console_transfer_protocol.py").read_bytes()
    imported = await import_protocol(source, "transfer.py", tmp_path)

    assert imported["inventory"]["labware"] == {
        "C3": "opentrons_flex_96_tiprack_1000ul",
        "C2": "nest_96_wellplate_2ml_deep",
    }
    assert imported["inventory"]["liquids"] == [{"slot": "C2", "well": "A1", "liquid": "Water", "volume": 500.0}]
    kinds = [step["kind"] for step in imported["steps"]]
    assert kinds[:4] == ["home", "configure_tip_type", "configure_nozzles", "pick_up_tip"]
    assert {"aspirate", "dispense", "drop_tip"} <= set(kinds)

    store = ConsoleStore(":memory:")
    for slot, load_name in imported["inventory"]["labware"].items():
        store.set_labware(slot, load_name)
    for item in imported["inventory"]["liquids"]:
        store.set_liquid(item["slot"], item["well"], item["liquid"], item["volume"])
    moves: list = []
    _guard_moves(client, moves)
    console = Console(StepRunner(client, store=store))
    assert (await console.run_step("home", {}))["ok"]
    for item in imported["steps"]:
        result = await console.run_step(item["kind"], item["params"])
        assert result["ok"], (item, result)

    assert {(item["well"], item["volume"]) for item in store.liquids()} == {("A1", 400.0), ("A2", 100.0)}
    assert store.used_tips("C3") == {"H1"}
