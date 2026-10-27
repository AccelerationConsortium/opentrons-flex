"""End-to-end tests of the guided verifier against an in-process simulator connector."""

import asyncio
import contextlib

import pytest
from opentrons.hardware_control.ot3api import OT3API
from unitelabs.cdk import SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.client import FlexClient
from unitelabs.opentrons_flex.verify import UnattendedOperator, Verifier, summarize


@contextlib.asynccontextmanager
async def _simulator(**overrides):
    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_modules=["heater_shaker", "gripper"],
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
        **overrides,
    )
    generator = create_app(config)
    connector = await generator.__anext__()
    await connector.start()
    try:
        yield connector.sila_server._address
    finally:
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await generator.__anext__()


@pytest.fixture
def slow_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make simulated relative moves take long enough to be interrupted, like real hardware."""
    original = OT3API.move_rel

    async def slow_move_rel(self, *args, **kwargs):
        await asyncio.sleep(5.0)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(OT3API, "move_rel", slow_move_rel)


@pytest.fixture
def hardware_like_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slow moves whose cancellation is wrapped in PythonException, as opentrons_hardware does on a real Flex."""
    from opentrons_shared_data.errors.exceptions import PythonException

    original = OT3API.move_rel

    async def move_rel(self, *args, **kwargs):
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError as exc:
            raise PythonException(exc) from exc
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(OT3API, "move_rel", move_rel)


@pytest.mark.usefixtures("hardware_like_halt")
async def test_emergency_stop_step_passes_when_the_hardware_wraps_the_cancellation() -> None:
    async with _simulator() as address:
        results = await Verifier(
            lambda: FlexClient.connect(address),
            operator=UnattendedOperator(deck_clear=True, say=lambda _message: None),
            levels=("smoke", "motion"),
            allow_simulator=True,
        ).run()

    outcomes = {result.name: (result.outcome, result.detail) for result in results}
    assert outcomes["Emergency stop interrupts a move"][0] == "PASS", outcomes


@pytest.mark.usefixtures("slow_moves")
async def test_smoke_and_motion_levels_pass_on_the_simulator() -> None:
    messages: list[str] = []
    async with _simulator() as address:
        verifier = Verifier(
            lambda: FlexClient.connect(address),
            operator=UnattendedOperator(deck_clear=True, say=messages.append),
            levels=("smoke", "motion"),
            allow_simulator=True,
        )
        results = await verifier.run()

    outcomes = {result.name: result.outcome for result in results}
    assert outcomes["Connector status"] == "PASS"
    assert outcomes["Module status"] == "PASS"
    assert outcomes["Home all axes"] == "PASS"
    assert outcomes["Emergency stop interrupts a move"] == "PASS", results
    assert outcomes["Physical E-stop is reported"] == "SKIP"
    assert summarize(results)[1]


async def test_simulator_fails_the_status_step_unless_allowed() -> None:
    async with _simulator() as address:
        results = await Verifier(
            lambda: FlexClient.connect(address),
            operator=UnattendedOperator(say=lambda _message: None),
            levels=("smoke",),
        ).run()

    status = next(result for result in results if result.name == "Connector status")
    assert status.outcome == "FAIL"
    assert "SIMULATOR" in status.detail


async def test_motion_is_skipped_without_deck_confirmation() -> None:
    async with _simulator() as address:
        results = await Verifier(
            lambda: FlexClient.connect(address),
            operator=UnattendedOperator(say=lambda _message: None),
            levels=("smoke", "motion"),
            allow_simulator=True,
        ).run()

    motion = [result for result in results if result.level == "motion"]
    assert {result.outcome for result in motion} == {"SKIP"}


async def test_unreachable_server_is_a_single_failure() -> None:
    results = await Verifier(
        lambda: FlexClient.connect("127.0.0.1:9", timeout_s=0.5),
        operator=UnattendedOperator(say=lambda _message: None),
    ).run()

    assert [(result.name, result.outcome) for result in results] == [("Connect to the SiLA server", "FAIL")]


class _EstopOperator:
    """Presses and releases a simulated E-stop when the verifier asks."""

    def __init__(self, set_state) -> None:
        self._set_state = set_state

    def ask(self, prompt: str) -> bool:
        if prompt.startswith("Press and HOLD"):
            self._set_state("PHYSICALLY_ENGAGED")
        elif prompt.startswith("Release"):
            self._set_state("LOGICALLY_ENGAGED")
        return True

    def say(self, _message: str) -> None:
        pass


async def test_physical_estop_is_detected_and_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentrons.hardware_control.types import EstopState

    from unitelabs.opentrons_flex.verify import Verifier as _Verifier

    state = {"value": EstopState.DISENGAGED}
    monkeypatch.setattr(OT3API, "get_estop_state", lambda _self: state["value"])
    acknowledged: list[bool] = []

    def acknowledge(*_args: object) -> None:
        acknowledged.append(True)
        state["value"] = EstopState.DISENGAGED

    monkeypatch.setattr(OT3API, "estop_acknowledge_and_clear", acknowledge)

    async with _simulator() as address:
        verifier = _Verifier(
            lambda: FlexClient.connect(address),
            operator=_EstopOperator(lambda name: state.__setitem__("value", EstopState[name])),
            levels=("motion",),
            acknowledge_estop=acknowledge,
            allow_simulator=True,
        )
        verifier._deck_clear = True
        async with verifier._connect() as client:
            verifier._client = client
            await verifier._motion_home()
            detail = await verifier._motion_physical_estop()

    assert acknowledged == [True], "Recover acknowledges the released E-stop over SiLA"
    assert "PHYSICALLY_ENGAGED" in detail
    assert "LOGICALLY_ENGAGED" in detail
    assert "acknowledge the released E-stop" in detail


async def test_undetected_estop_reports_the_states_it_saw(monkeypatch: pytest.MonkeyPatch) -> None:
    from unitelabs.opentrons_flex.verify import Verifier as _Verifier

    async with _simulator() as address:
        verifier = _Verifier(
            lambda: FlexClient.connect(address),
            operator=_EstopOperator(lambda _name: None),
            levels=("motion",),
            allow_simulator=True,
        )
        verifier._deck_clear = True
        async with verifier._connect() as client:
            verifier._client = client
            monkeypatch.setattr(verifier, "_wait_for", _fast_timeout(verifier._wait_for))
            with pytest.raises(RuntimeError, match="values seen: DISENGAGED"):
                await verifier._motion_physical_estop()


def _fast_timeout(wait_for):
    async def wrapper(predicate, *, timeout_s: float, interval_s: float = 0.5):
        return await wait_for(predicate, timeout_s=min(timeout_s, 1.0), interval_s=0.1)

    return wrapper
