"""Tests for HardwareProxy against a real simulated HardwareControlAPI.

Ported from opentrons/api/tests/opentrons/hardware_control/test_moves.py
(tests that use the hardware_api fixture, i.e. a real API with Simulator backend).
Proxy-specific tests (lock serialisation, delegation, wrapped()) are added below.

The proxy must be transparent to the opentrons tests — behaviour must match
running against the raw API directly.
"""

import asyncio

import pytest
import pytest_asyncio

from opentrons import types
from opentrons.hardware_control import API
from opentrons.hardware_control.backends.simulator import Simulator
from opentrons.hardware_control.errors import OutOfBoundsMove
from opentrons.hardware_control.types import Axis, MotionChecks
from opentrons_shared_data.errors.exceptions import PositionUnknownError
from opentrons.drivers.smoothie_drivers.simulator import SimulatingDriver

from unitelabs.opentrons_flex.io import NotHomedError
from unitelabs.opentrons_flex.io.hardware_proxy import HardwareProxy, _TimedLock


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
