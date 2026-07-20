"""Tests for HardwareProxy against a real simulated HardwareControlAPI.

Ported from opentrons/api/tests/opentrons/hardware_control/test_moves.py
(tests that use the hardware_api fixture, i.e. a real API with Simulator backend).
Proxy-specific tests (lock serialisation, delegation, wrapped()) are added below.

The proxy must be transparent to the opentrons tests — behaviour must match
running against the raw API directly.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from opentrons import types
from opentrons.hardware_control import API
from opentrons.hardware_control.backends.simulator import Simulator
from opentrons.hardware_control.errors import OutOfBoundsMove
from opentrons.hardware_control.types import Axis, MotionChecks, OT3Mount
from opentrons_shared_data.errors.exceptions import PositionUnknownError
from opentrons.drivers.smoothie_drivers.simulator import SimulatingDriver

from unitelabs.opentrons_flex.io import (
    DirectGripperControlDisabledError,
    FlexStackerController,
    LabwareMovementState,
    NotHomedError,
)
from unitelabs.opentrons_flex.io.hardware_proxy import HardwareProxy, _TimedLock
from unitelabs.opentrons_flex.io.recovery_state import stacker_recovery_state_for


@pytest_asyncio.fixture
async def api() -> API:
    return await API.build_hardware_simulator(loop=asyncio.get_running_loop())


@pytest_asyncio.fixture
async def proxy(api: API) -> HardwareProxy:
    return HardwareProxy(api)


# ── Delegation ────────────────────────────────────────────────────────────────


def test_wrapped_returns_self(proxy: HardwareProxy) -> None:
    assert proxy.wrapped() is proxy


def test_internal_attrs_not_proxied(proxy: HardwareProxy, api: API) -> None:
    assert proxy._api is api
    assert proxy._lock is not None


def test_sync_attr_passes_through(proxy: HardwareProxy, api: API) -> None:
    assert proxy.is_simulator == api.is_simulator


def test_private_state_accessible_via_getattr(proxy: HardwareProxy) -> None:
    """_current_position and _backend fall through to the real API."""
    assert isinstance(proxy._backend, Simulator)
    assert isinstance(proxy._current_position, dict)


def test_wraps_instance_ot2(proxy: HardwareProxy) -> None:
    """wraps_instance(API) must return True so get_ot2_hardware() routes work."""
    assert proxy.wraps_instance(API) is True


def test_wraps_instance_mismatch(proxy: HardwareProxy) -> None:
    """wraps_instance with a non-matching type must return False."""
    assert proxy.wraps_instance(str) is False


# ── Motion (ported from test_moves.py) ───────────────────────────────────────


async def test_home_specific_sim(proxy: HardwareProxy) -> None:
    await proxy.home()
    await proxy.move_to(types.Mount.RIGHT, types.Point(0, 10, 20))
    proxy._last_moved_mount = None
    await proxy.move_rel(types.Mount.LEFT, types.Point(0, 0, -20))
    await proxy.home([Axis.Z, Axis.C])
    assert proxy._current_position == {
        Axis.X: 0,
        Axis.Y: 10,
        Axis.Z: 218,
        Axis.A: -10,
        Axis.B: 19,
        Axis.C: 19,
    }


async def test_retract(proxy: HardwareProxy) -> None:
    await proxy.home()
    await proxy.move_to(types.Mount.RIGHT, types.Point(0, 10, 20))
    await proxy.retract(types.Mount.RIGHT, 10)
    assert proxy._current_position == {
        Axis.X: 0,
        Axis.Y: 10,
        Axis.Z: 218,
        Axis.A: 218,
        Axis.B: 19,
        Axis.C: 19,
    }


async def test_move(proxy: HardwareProxy) -> None:
    abs_position = types.Point(30, 20, 10)
    mount = types.Mount.RIGHT
    target_position1 = {
        Axis.X: 30,
        Axis.Y: 20,
        Axis.Z: 218,
        Axis.A: -20,
        Axis.B: 19,
        Axis.C: 19,
    }
    await proxy.home()
    await proxy.move_to(mount, abs_position)
    assert proxy._current_position == target_position1

    rel_position = types.Point(30, 20, -10)
    mount2 = types.Mount.LEFT
    target_position2 = {
        Axis.X: 60,
        Axis.Y: 40,
        Axis.Z: 208,
        Axis.A: 218,
        Axis.B: 19,
        Axis.C: 19,
    }
    await proxy.move_rel(mount2, rel_position)
    assert proxy._current_position == target_position2


async def test_move_rel_bounds(proxy: HardwareProxy) -> None:
    with pytest.raises(OutOfBoundsMove):
        await proxy.move_rel(types.Mount.RIGHT, types.Point(0, 0, 2000), check_bounds=MotionChecks.HIGH)


async def test_move_rel_homing_failures(proxy: HardwareProxy) -> None:
    await proxy.home()
    assert isinstance(proxy._backend._smoothie_driver, SimulatingDriver)
    proxy._backend._smoothie_driver._homed_flags = {
        "X": True,
        "Y": True,
        "Z": False,
        "A": True,
        "B": False,
        "C": False,
    }
    with pytest.raises(PositionUnknownError):
        await proxy.move_rel(types.Mount.LEFT, types.Point(0, 0, 2000), fail_on_not_homed=True)
    await proxy.move_rel(types.Mount.RIGHT, types.Point(0, 0, 2000), fail_on_not_homed=True)


async def test_current_position_homing_failures(proxy: HardwareProxy) -> None:
    await proxy.home()
    assert isinstance(proxy._backend._smoothie_driver, SimulatingDriver)
    proxy._backend._smoothie_driver._homed_flags = {
        "X": True,
        "Y": True,
        "Z": False,
        "A": True,
        "B": False,
        "C": True,
    }
    with pytest.raises(PositionUnknownError):
        await proxy.current_position(mount=types.Mount.LEFT, fail_on_not_homed=True)
    with pytest.raises(PositionUnknownError):
        await proxy.gantry_position(mount=types.Mount.LEFT, fail_on_not_homed=True)
    await proxy.current_position(mount=types.Mount.RIGHT, fail_on_not_homed=True)
    await proxy.gantry_position(mount=types.Mount.RIGHT, fail_on_not_homed=True)


# ── from_api shim ─────────────────────────────────────────────────────────────


async def test_from_api_shares_api_and_lock(api: API) -> None:
    """FlexMotionController.from_api() must share the API instance and lock with HardwareProxy."""
    from unitelabs.opentrons_flex.io import FlexMotionController

    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(api, lock=shared_lock)
    controller = FlexMotionController.from_api(api, lock=shared_lock)

    assert controller._lock._lock is proxy._lock._lock is shared_lock
    assert controller._api is api
    assert controller._recovery_state is proxy._recovery_state


async def test_proxy_halt_and_full_home_update_shared_recovery_state(api: API) -> None:
    """HTTP-side halt/home must be visible to the SiLA-side controller."""
    from unitelabs.opentrons_flex.io import FlexMotionController

    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(api, lock=shared_lock)
    controller = FlexMotionController.from_api(api, lock=shared_lock)

    await proxy.halt()
    assert controller._recovery_state.rehome_required

    with pytest.raises(NotHomedError, match="Fully re-home"):
        await proxy.home([Axis.X])
    assert controller._recovery_state.rehome_required

    await proxy.home()
    assert not controller._recovery_state.rehome_required


async def test_proxy_halt_bypasses_busy_shared_lock(api: API) -> None:
    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(api, lock=shared_lock)
    await shared_lock.acquire()
    try:
        await asyncio.wait_for(proxy.halt(), timeout=0.1)
    finally:
        shared_lock.release()


async def test_proxy_halt_gates_http_motion_until_full_home(api: API) -> None:
    proxy = HardwareProxy(api)
    await proxy.home()
    await proxy.halt()

    with pytest.raises(NotHomedError, match="Fully re-home"):
        await proxy.move_to(types.Mount.LEFT, types.Point(0, 0, 0))

    await proxy.home()
    await proxy.move_to(types.Mount.LEFT, types.Point(0, 0, 0))


async def test_cancelled_proxy_halt_finishes_before_propagating(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = HardwareProxy(api)
    halt_started = asyncio.Event()
    release_halt = asyncio.Event()

    async def blocked_halt() -> None:
        halt_started.set()
        await release_halt.wait()

    monkeypatch.setattr(api, "halt", blocked_halt)
    halt_task = asyncio.create_task(proxy.halt())
    await halt_started.wait()
    halt_task.cancel()
    await asyncio.sleep(0)
    assert not halt_task.done()
    release_halt.set()

    with pytest.raises(asyncio.CancelledError):
        await halt_task
    assert proxy._recovery_state.rehome_required


async def test_proxy_halt_during_full_home_keeps_recovery_gate(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(api, lock=shared_lock)
    home_started = asyncio.Event()
    release_home = asyncio.Event()

    async def blocked_home(*args: object, **kwargs: object) -> None:
        home_started.set()
        await release_home.wait()

    monkeypatch.setattr(api, "home", blocked_home)
    home_task = asyncio.create_task(proxy.home())
    await home_started.wait()
    await proxy.halt()
    release_home.set()
    with pytest.raises(asyncio.CancelledError):
        await home_task

    assert proxy._recovery_state.rehome_required


async def test_proxy_blocks_http_gripper_bypass_when_labware_state_is_managed(tmp_path: Path) -> None:
    class FakeHardwareAPI:
        async def grip(self) -> None:
            raise AssertionError("grip must be rejected before hardware delegation")

        async def move_to(self, mount: OT3Mount, position: object) -> None:
            raise AssertionError("move_to must be rejected before hardware delegation")

        async def move_axes(self, position: dict[Axis, float]) -> None:
            raise AssertionError("move_axes must be rejected before hardware delegation")

    state = LabwareMovementState(tmp_path / "labware-state.json", {"D1": "plate-1"})
    proxy = HardwareProxy(FakeHardwareAPI(), labware_state=state)  # type: ignore[arg-type]
    try:
        with pytest.raises(DirectGripperControlDisabledError, match="HTTP grip"):
            await proxy.grip()
        with pytest.raises(DirectGripperControlDisabledError, match="HTTP move_to"):
            await proxy.move_to(OT3Mount.GRIPPER, object())
        with pytest.raises(DirectGripperControlDisabledError, match="HTTP move_to"):
            await proxy.move_to(types.Mount.EXTENSION, object())
        with pytest.raises(DirectGripperControlDisabledError, match="HTTP move_axes"):
            await proxy.move_axes(position={Axis.Z_G: 100.0})
        with pytest.raises(DirectGripperControlDisabledError, match="HTTP move_axes"):
            await proxy.move_axes({Axis.G: 10.0})
    finally:
        state.close()


# ── Lock serialisation ────────────────────────────────────────────────────────


async def test_concurrent_calls_all_complete(proxy: HardwareProxy) -> None:
    """Concurrent awaits must all complete without deadlock."""
    await proxy.home()
    results = await asyncio.gather(
        proxy.current_position(types.Mount.RIGHT),
        proxy.current_position(types.Mount.LEFT),
        proxy.gantry_position(types.Mount.RIGHT),
    )
    assert len(results) == 3


async def test_lock_serialises_calls(proxy: HardwareProxy) -> None:
    """Calls made concurrently must not overlap on the driver."""
    await proxy.home()
    call_order: list[str] = []

    async def move_and_record(label: str, point: types.Point) -> None:
        await proxy.move_to(types.Mount.RIGHT, point)
        call_order.append(label)

    await asyncio.gather(
        move_and_record("a", types.Point(10, 10, 10)),
        move_and_record("b", types.Point(20, 20, 20)),
        move_and_record("c", types.Point(30, 30, 30)),
    )
    # All three must complete — order is scheduler-dependent but all must finish
    assert sorted(call_order) == ["a", "b", "c"]


# ── _TimedLock ────────────────────────────────────────────────────────────────


async def test_timed_lock_no_timeout_acquires() -> None:
    """timeout_s=None must behave identically to a plain asyncio.Lock."""
    raw = asyncio.Lock()
    tl = _TimedLock(raw, timeout_s=None)
    async with tl:
        assert raw.locked()
    assert not raw.locked()


async def test_timed_lock_raises_on_timeout() -> None:
    """When the underlying lock is held, _TimedLock must raise TimeoutError promptly."""
    raw = asyncio.Lock()
    await raw.acquire()  # simulate robot_server holding the lock

    tl = _TimedLock(raw, timeout_s=0.05)
    with pytest.raises(TimeoutError, match="robot_server may be holding the hardware API"):
        async with tl:
            pass  # should not reach here

    raw.release()


async def test_proxy_timeout_raises_on_held_lock(api: API) -> None:
    """HardwareProxy with lock_timeout_s must raise TimeoutError when the lock is held."""
    shared_lock = asyncio.Lock()
    await shared_lock.acquire()  # simulate robot_server holding the lock

    proxy = HardwareProxy(api, lock=shared_lock, lock_timeout_s=0.05)
    with pytest.raises(TimeoutError, match="robot_server may be holding the hardware API"):
        await proxy.home()

    shared_lock.release()


async def test_stacker_http_failure_and_sila_recovery_share_one_authority() -> None:
    """Both interfaces gate labware motion until the same complete HomeAll."""

    class _ModuleType:
        name = "FLEX_STACKER"

    class _StackerModule:
        MODULE_TYPE = _ModuleType()

        def __init__(self) -> None:
            self.status = SimpleNamespace(value="idle")
            self.latch_state = SimpleNamespace(value="closed")
            self.platform_state = SimpleNamespace(value="retracted")
            self.hopper_door_state = SimpleNamespace(value="closed")
            self.install_detected = True
            self.initialized = True
            self.limit_switch_status = {
                "x": SimpleNamespace(value="extended"),
                "z": SimpleNamespace(value="retracted"),
                "latch": SimpleNamespace(value="extended"),
            }
            self.live_data = {"data": {"errorDetails": None}}
            self.device_info = {"serial": "FS-1", "model": "flexStackerModuleV1", "version": "1.0"}
            self.fail_next_move = True
            self.calls: list[tuple] = []

        async def move_axis(self, *args: object, **kwargs: object) -> bool:
            self.calls.append(("move_axis", args, kwargs))
            if self.fail_next_move:
                raise RuntimeError("simulated HTTP-side interruption")
            return True

        async def dispense_labware(self, **kwargs: object) -> None:
            self.calls.append(("dispense_labware", kwargs))

        async def home_all(self, ignore_latch: bool) -> None:
            self.calls.append(("home_all", ignore_latch))

    class _HardwareApi:
        def __init__(self, module: object) -> None:
            self.attached_modules = [module]

    module = _StackerModule()
    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(_HardwareApi(module), lock=shared_lock)  # type: ignore[arg-type]
    sila_controller = FlexStackerController.from_module(module, lock=shared_lock)
    http_stacker = proxy.attached_modules[0]

    with pytest.raises(RuntimeError, match="HTTP-side interruption"):
        await http_stacker.move_axis("X", "extend", 10.0)

    assert sila_controller.state.recovery_required is True
    with pytest.raises(NotHomedError, match="HomeAll"):
        await http_stacker.dispense_labware(labware_height=14.4)

    module.fail_next_move = False
    await sila_controller.home_all(ignore_latch=False)
    assert sila_controller.state.recovery_required is False
    await http_stacker.dispense_labware(labware_height=14.4)


async def test_all_http_module_operations_share_the_connector_lock() -> None:
    """Non-Stacker robot-server module calls must not bypass SiLA ownership."""

    class _ModuleType:
        name = "HEATER_SHAKER"

    class _Module:
        MODULE_TYPE = _ModuleType()

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def actuate(self) -> None:
            self.started.set()
            await self.release.wait()

    first = _Module()
    second = _Module()
    api = SimpleNamespace(attached_modules=[first, second])
    proxy = HardwareProxy(api)  # type: ignore[arg-type]
    first_proxy, second_proxy = proxy.attached_modules
    assert first_proxy is proxy.attached_modules[0]
    assert second_proxy is proxy.attached_modules[1]

    first_task = asyncio.create_task(first_proxy.actuate())
    await first.started.wait()
    second_task = asyncio.create_task(second_proxy.actuate())
    await asyncio.sleep(0)
    assert not second.started.is_set()

    first.release.set()
    await second.started.wait()
    second.release.set()
    await asyncio.gather(first_task, second_task)


async def test_module_proxies_preserve_protocol_engine_type_identity() -> None:
    """Native movement flaggers must still find proxied H/S and Thermocycler modules."""
    from opentrons.hardware_control.modules import HeaterShaker, Thermocycler
    from opentrons.protocol_engine.execution.heater_shaker_movement_flagger import HeaterShakerMovementFlagger
    from opentrons.protocol_engine.execution.thermocycler_movement_flagger import ThermocyclerMovementFlagger

    heater_shaker = MagicMock(spec=HeaterShaker)
    heater_shaker.device_info = {"serial": "HS-1"}
    thermocycler = MagicMock(spec=Thermocycler)
    thermocycler.device_info = {"serial": "TC-1"}
    hardware = HardwareProxy(SimpleNamespace(attached_modules=[heater_shaker, thermocycler]))  # type: ignore[arg-type]
    proxied_heater_shaker, proxied_thermocycler = hardware.attached_modules

    assert isinstance(proxied_heater_shaker, HeaterShaker)
    assert isinstance(proxied_thermocycler, Thermocycler)

    heater_shaker_flagger = HeaterShakerMovementFlagger(MagicMock(), hardware)
    thermocycler_flagger = ThermocyclerMovementFlagger(MagicMock(), hardware, MagicMock())
    assert await heater_shaker_flagger._find_heater_shaker_by_serial("HS-1") is proxied_heater_shaker
    assert await thermocycler_flagger._find_thermocycler_by_serial("TC-1") is proxied_thermocycler


async def test_cancelled_http_reader_action_holds_lock_until_native_work_finishes() -> None:
    """HTTP Reader cancellation must not expose a still-running executor action."""

    class _ModuleType:
        name = "ABSORBANCE_READER"

    class _ReaderModule:
        MODULE_TYPE = _ModuleType()

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def start_measure(self) -> list[list[float]]:
            self.started.set()
            await self.release.wait()
            return [[0.0] * 96]

    module = _ReaderModule()
    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(SimpleNamespace(attached_modules=[module]), lock=shared_lock)  # type: ignore[arg-type]
    http_reader = proxy.attached_modules[0]

    measurement = asyncio.create_task(http_reader.start_measure())
    await module.started.wait()
    measurement.cancel()
    await asyncio.sleep(0)
    assert shared_lock.locked()
    assert not measurement.done()

    module.release.set()
    with pytest.raises(asyncio.CancelledError):
        await measurement
    assert not shared_lock.locked()


async def test_stacker_rechecks_recovery_after_acquiring_shared_lock() -> None:
    """A queued HTTP labware call must observe a SiLA failure before acting."""

    class _ModuleType:
        name = "FLEX_STACKER"

    class _StackerModule:
        MODULE_TYPE = _ModuleType()

        def __init__(self) -> None:
            self.calls = 0

        async def dispense_labware(self, **kwargs: object) -> None:
            self.calls += 1

    module = _StackerModule()
    shared_lock = asyncio.Lock()
    await shared_lock.acquire()
    proxy = HardwareProxy(SimpleNamespace(attached_modules=[module]), lock=shared_lock)  # type: ignore[arg-type]
    http_stacker = proxy.attached_modules[0]
    request = asyncio.create_task(http_stacker.dispense_labware(labware_height=14.4))
    await asyncio.sleep(0)

    stacker_recovery_state_for(module).require_full_home()
    shared_lock.release()

    with pytest.raises(NotHomedError, match="HomeAll"):
        await request
    assert module.calls == 0


async def test_stacker_http_false_motion_result_requires_full_home() -> None:
    """A firmware false result must quarantine later HTTP labware movement."""

    class _ModuleType:
        name = "FLEX_STACKER"

    class _StackerModule:
        MODULE_TYPE = _ModuleType()

        def __init__(self) -> None:
            self.dispense_calls = 0

        async def move_axis(self, *args: object) -> bool:
            return False

        async def dispense_labware(self, **kwargs: object) -> None:
            self.dispense_calls += 1

    module = _StackerModule()
    proxy = HardwareProxy(SimpleNamespace(attached_modules=[module]))  # type: ignore[arg-type]
    http_stacker = proxy.attached_modules[0]

    assert await http_stacker.move_axis("X", "extend", 10.0) is False
    assert stacker_recovery_state_for(module).full_home_required is True
    with pytest.raises(NotHomedError, match="HomeAll"):
        await http_stacker.dispense_labware(labware_height=14.4)
    assert module.dispense_calls == 0


async def test_cancelled_http_stacker_action_deactivates_before_unlocking() -> None:
    """Cancellation must stop Stacker motors before another owner can acquire the lock."""

    class _ModuleType:
        name = "FLEX_STACKER"

    class _StackerModule:
        MODULE_TYPE = _ModuleType()

        def __init__(self) -> None:
            self.move_started = asyncio.Event()
            self.deactivate_started = asyncio.Event()
            self.release_deactivate = asyncio.Event()

        async def move_axis(self, *args: object) -> bool:
            self.move_started.set()
            await asyncio.Event().wait()
            return True

        async def deactivate(self) -> None:
            self.deactivate_started.set()
            await self.release_deactivate.wait()

    module = _StackerModule()
    shared_lock = asyncio.Lock()
    proxy = HardwareProxy(SimpleNamespace(attached_modules=[module]), lock=shared_lock)  # type: ignore[arg-type]
    http_stacker = proxy.attached_modules[0]

    move = asyncio.create_task(http_stacker.move_axis("X", "extend", 10.0))
    await module.move_started.wait()
    move.cancel()
    await module.deactivate_started.wait()
    assert shared_lock.locked()
    assert not move.done()

    module.release_deactivate.set()
    with pytest.raises(asyncio.CancelledError):
        await move
    assert stacker_recovery_state_for(module).full_home_required is True
    assert not shared_lock.locked()


# ── locked_gen (async generator wrapping) ─────────────────────────────────────


async def test_locked_gen_yields_all_items(proxy: HardwareProxy) -> None:
    """Proxied async generator methods must yield every item under the lock."""
    from unittest.mock import patch

    async def fake_gen(*args, **kwargs):
        for i in range(3):
            yield i

    with patch.object(type(proxy._api), "attached_modules", new_callable=lambda: property(lambda self: None)):
        pass  # just checking the proxy routing — use a direct attribute patch instead

    # Inject a fake async-generator attribute directly onto the wrapped API
    proxy._api._fake_agen = fake_gen  # type: ignore[attr-defined]

    # Verify __getattr__ wraps it correctly
    import inspect as _inspect

    attr = getattr(proxy._api, "_fake_agen")
    assert _inspect.isasyncgenfunction(attr)

    collected = [item async for item in proxy._fake_agen()]  # type: ignore[attr-defined]

    assert collected == [0, 1, 2]


async def test_locked_gen_holds_lock_while_iterating(proxy: HardwareProxy) -> None:
    """The lock must be held for the full duration of the async generator iteration."""
    lock = proxy._lock._lock

    async def fake_gen(*args, **kwargs):
        assert lock.locked()
        yield 42
        assert lock.locked()

    proxy._api._checking_gen = fake_gen  # type: ignore[attr-defined]

    results = [item async for item in proxy._checking_gen()]  # type: ignore[attr-defined]

    assert results == [42]
    assert not lock.locked()
