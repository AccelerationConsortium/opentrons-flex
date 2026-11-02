"""The AS-MS protocols replayed as SiLA commands over a real gRPC connection to the OT3 simulator."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import grpc.aio
import pytest
import pytest_asyncio
from opentrons.hardware_control.types import OT3Mount
from unitelabs.cdk import SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.asms_sila_workflow import (
    AsmsSilaClient,
    AsmsWorkflowError,
    build_plan,
    compile_plan,
    execute,
    load_runs,
    preflight,
)
from unitelabs.opentrons_flex.features.motion_control import Mount

ROOT = Path(__file__).resolve().parents[1]
_INSTRUMENTS = {
    OT3Mount.LEFT: {"model": "p50_multi_v3.5", "id": "P50M-SIM-1"},
    OT3Mount.RIGHT: {"model": "p1000_multi_v3.5", "id": "P1KM-SIM-1"},
    OT3Mount.GRIPPER: {"model": "gripperV1", "id": "GRIPPER-SIM-1"},
}


@pytest.fixture(scope="module")
def analysis_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("asms-analysis")


@pytest.fixture(scope="module")
def plans(analysis_dir: Path) -> dict[str, dict]:
    import asyncio

    return {name: asyncio.run(build_plan(run, analysis_dir)) for name, run in load_runs().items()}


@pytest_asyncio.fixture
async def connector(tmp_path: Path):
    movement = json.loads((ROOT / "config/asms_unit_labware_movement.json").read_text())
    movement["state_file"] = str(tmp_path / "deck-state.json")
    movement_path = tmp_path / "labware-movement.json"
    movement_path.write_text(json.dumps(movement))
    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_gripper=True,
        simulated_temperature_module=True,
        labware_movement_config=str(movement_path),
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
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


def _guard_moves(client: AsmsSilaClient, moves: list) -> None:
    """Fail the test on any MoveTo that is neither vertical nor level at the top of travel."""
    original = client.move_to

    async def move_to(mount: Mount, point):
        here = await client.position(mount)
        vertical = abs(here["x"] - point["x"]) < 0.01 and abs(here["y"] - point["y"]) < 0.01
        level = abs(here["z"] - point["z"]) < 0.01
        assert vertical or level, f"diagonal move on {mount.value} from {here} to {point}"
        if level and not vertical:
            assert point["z"] > 120.0, f"horizontal move below travel height on {mount.value}: {point}"
        moves.append((mount.value, here, dict(point)))
        return await original(mount, point)

    client.move_to = move_to  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_prep_then_wash_and_elute_run_end_to_end_over_grpc(connector: AsmsSilaClient, plans) -> None:
    moves: list = []
    _guard_moves(connector, moves)
    logged: list[str] = []

    prep = await execute(connector, plans["prep"], delay_scale=0.0, log=logged.append)
    wash = await execute(connector, plans["wash-elute"], delay_scale=0.0, log=logged.append)

    assert prep["status"] == wash["status"] == "completed"
    assert prep["simulating"] is True
    assert prep["steps_executed"] == len(plans["prep"]["steps"])
    assert wash["steps_executed"] == len(plans["wash-elute"]["steps"])
    assert wash["gripper_plans"] == ["asms-v1-a1-to-b2", "asms-v1-b1-to-b2", "asms-v1-b2-to-a1", "asms-v1-b2-to-b1"]
    deck = await connector.deck_state()
    assert {item.location_identifier: item.labware_identifier for item in deck.occupied_locations} == {
        "A1": "asms-plate",
        "B2": "elution-plate",
    }
    pipettes = await connector.attached_pipettes()
    assert not any(pipette.has_tip for pipette in pipettes)
    assert {mount for mount, _here, _to in moves} == {"LEFT", "RIGHT"}
    assert "# Elute beads in 80% methanol" in logged


@pytest.mark.asyncio
async def test_preflight_refuses_a_wrong_deck_ledger_and_moves_nothing(connector: AsmsSilaClient, plans) -> None:
    plan = copy.deepcopy(plans["wash-elute"])
    plan["initial_ledger"] = {"A1": "elution-plate", "B1": "asms-plate"}
    with pytest.raises(AsmsWorkflowError, match="deck ledger shows 'asms-plate' at A1"):
        await execute(connector, plan, delay_scale=0.0)

    plan = copy.deepcopy(plans["prep"])
    plan["pipettes"] = {"LEFT": "p1000_multi_flex", "RIGHT": "p1000_multi_flex"}
    with pytest.raises(AsmsWorkflowError, match="needs p1000_multi_flex on LEFT"):
        await preflight(connector, plan)


def test_plans_follow_the_corrected_lineage_and_single_tip_prep(plans) -> None:
    prep, wash = plans["prep"], plans["wash-elute"]
    pickups = [step for step in prep["steps"] if step["kind"] == "pick_up_tip"]
    assert {len(step["wells"]) for step in pickups} == {1}
    assert {step["layout"] for step in prep["steps"] if step["kind"] == "configure_nozzles"} == {"single_A1"}
    assert [len(s["wells"]) for s in wash["steps"] if s["kind"] == "pick_up_tip"] == [8] * 26
    trash = {
        json.dumps(s["location"]) for s in wash["steps"] if s["kind"] == "drop_tip" and s["destination"] == "trash"
    }
    # Trash targets are moved from the instrument centre to the A1 tip (31.5 mm back).
    assert all(json.loads(item)["y"] == 74.5 for item in trash)
    comments = [step["message"] for step in wash["steps"] if step["kind"] == "comment"]
    assert "Transfer clarified methanol eluate from the elution plate to the MS plate" in comments
    for plan in (prep, wash):
        for index, step in enumerate(plan["steps"]):
            if step["kind"] == "prepare_for_aspirate":
                previous = plan["steps"][index - 1]
                following = next(s for s in plan["steps"][index + 1 :] if s["kind"] in {"move", "aspirate"})
                # The plunger is prepared in air: right after reaching a well top, before descending.
                assert previous["kind"] == "move"
                assert following["kind"] == "aspirate" or following["target"]["z"] < previous["target"]["z"]


@pytest.mark.asyncio
async def test_compiler_refuses_unmapped_commands_and_unsafe_single_tip_pickups(plans, analysis_dir: Path) -> None:
    analysis = json.loads((analysis_dir / "prep.analysis.json").read_text())

    unknown = copy.deepcopy(analysis)
    unknown["commands"].insert(5, {"commandType": "liquidProbe", "params": {}, "result": {}, "status": "succeeded"})
    with pytest.raises(AsmsWorkflowError, match=r"liquidProbe.*no SiLA mapping"):
        await compile_plan(unknown, {})

    reordered = copy.deepcopy(analysis)
    pickups = [c for c in reordered["commands"] if c["commandType"] == "pickUpTip"]
    pickups[0]["params"]["wellName"] = "G1"  # G1 first: the B nozzle would land on the H1 tip
    with pytest.raises(AsmsWorkflowError, match="press nozzles onto tips H1"):
        await compile_plan(reordered, {})
