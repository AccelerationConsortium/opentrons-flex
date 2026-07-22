"""Protocol Engine ownership and run-mutation HTTP boundary tests."""

from __future__ import annotations

import httpx
import pytest

from unitelabs.opentrons_flex.io import RunOwnershipError
from unitelabs.opentrons_flex.io.run_authority import (
    ProtocolRunAuthority,
    ProtocolRunState,
    RunAwareLock,
    RunMutationGate,
    RunMutationHttpGuard,
)


def _state(
    *,
    started: bool = True,
    terminal: bool = False,
    checkpoint_id: str | None = None,
    status: str = "paused",
    protocol_less: bool = False,
) -> ProtocolRunState:
    return ProtocolRunState(
        run_id="run-1",
        status=status,
        started=started,
        terminal=terminal,
        mutation_checkpoint_id=checkpoint_id,
        protocol_less=protocol_less,
    )


@pytest.mark.asyncio
async def test_direct_lock_rechecks_ownership_after_waiting() -> None:
    states = iter(
        [
            ProtocolRunState(run_id=None, status="idle", started=False, terminal=True),
            _state(),
        ]
    )
    lock = RunAwareLock(ProtocolRunAuthority(lambda: next(states)))

    with pytest.raises(RunOwnershipError, match="owns the Flex"):
        await lock.acquire()

    assert lock.locked() is False


@pytest.mark.asyncio
async def test_observation_remains_serialized_and_available_during_active_run() -> None:
    lock = RunAwareLock(ProtocolRunAuthority(_state))

    async with lock.observation():
        assert lock.locked() is True

    with pytest.raises(RunOwnershipError, match="owns the Flex"):
        await lock.acquire()


def test_authority_fails_closed_if_refresh_breaks_during_active_run() -> None:
    calls = 0

    def provider() -> ProtocolRunState:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _state()
        raise RuntimeError("state backend unavailable")

    authority = ProtocolRunAuthority(provider)
    assert authority.current().owns_hardware is True

    with pytest.raises(RunOwnershipError, match="remains disabled"):
        authority.assert_direct_control_allowed()


async def _downstream(scope, receive, send) -> None:
    del scope, receive
    body = b'{"accepted":true}'
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


@pytest.mark.asyncio
async def test_http_guard_blocks_raw_commands_and_all_resume_variants() -> None:
    authority = ProtocolRunAuthority(_state)
    gate = RunMutationGate()
    gate.hold("run-1", "resource validation failed", fatal=False)
    transport = httpx.ASGITransport(app=RunMutationHttpGuard(_downstream, authority, gate))

    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        raw = await client.post("/runs/run-1/commands", json={"data": {"commandType": "home"}})
        assert raw.status_code == 409

        for action in ("play", "resume-from-recovery", "resume-from-recovery-assuming-false-positive"):
            response = await client.post("/runs/run-1/actions", json={"data": {"actionType": action}})
            assert response.status_code == 409
            assert "resource validation failed" in response.text

        pause = await client.post("/runs/run-1/actions", json={"data": {"actionType": "pause"}})
        assert pause.status_code == 200
        assert pause.json() == {"accepted": True}

        for path in (
            "/robot/move",
            "/modules/TM-1",
            "/commands",
            "/maintenance_runs",
            "/deck_configuration",
        ):
            response = await client.post(path, json={})
            assert response.status_code == 409
            assert "exclusively owns" in response.text

        controlled = await client.post("/unitelabs/runs/run-1/mutations", json={})
        assert controlled.status_code == 200


@pytest.mark.asyncio
async def test_raw_setup_command_requires_token_before_run_starts() -> None:
    token = "a-secure-offline-mutation-token-1234"
    authorized: list[tuple[str, str, str, str]] = []
    authority = ProtocolRunAuthority(lambda: _state(started=False, protocol_less=True))
    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(
            _downstream,
            authority,
            RunMutationGate(),
            mutation_api_token=token,
            prestart_setup_authorizer=lambda *args: authorized.append(args),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        body = {"data": {"commandType": "comment", "params": {"message": "setup note"}}}
        missing = await client.post("/runs/run-1/commands", json=body)
        accepted = await client.post(
            "/runs/run-1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )

    assert missing.status_code == 401
    assert accepted.status_code == 200
    assert len(authorized) == 1
    assert authorized[0][:2] == ("run-1", "comment")


@pytest.mark.asyncio
async def test_prestart_allowlist_does_not_authorize_direct_motion() -> None:
    token = "a-secure-offline-mutation-token-1234"
    authority = ProtocolRunAuthority(lambda: _state(started=False, protocol_less=True))
    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(
            _downstream,
            authority,
            RunMutationGate(),
            mutation_api_token=token,
            prestart_setup_authorizer=lambda *_: None,
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        setup = await client.post(
            "/runs/run-1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "data": {
                    "commandType": "loadLabware",
                    "params": {
                        "loadName": "opentrons_96_wellplate_200ul_pcr_full_skirt",
                        "namespace": "opentrons",
                        "version": 1,
                        "location": {"slotName": "A1"},
                    },
                }
            },
        )
        custom_definition = await client.post(
            "/runs/run-1/labware_definitions",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        custom_load = await client.post(
            "/runs/run-1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "data": {
                    "commandType": "loadLabware",
                    "params": {
                        "loadName": "unapproved_custom_plate",
                        "namespace": "custom_beta",
                        "version": 1,
                        "location": {"slotName": "B1"},
                    },
                }
            },
        )
        raw_home = await client.post(
            "/runs/run-1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"data": {"commandType": "home", "params": {}}},
        )
        direct_motion = await client.post(
            "/robot/home",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )

    assert setup.status_code == 200
    assert custom_definition.status_code == 409
    assert custom_load.status_code == 409
    assert raw_home.status_code == 409
    assert direct_motion.status_code == 409


@pytest.mark.asyncio
async def test_prestart_setup_cannot_target_python_protocol_run() -> None:
    token = "a-secure-offline-mutation-token-1234"
    authority = ProtocolRunAuthority(lambda: _state(started=False, protocol_less=False))
    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(
            _downstream,
            authority,
            RunMutationGate(),
            mutation_api_token=token,
            prestart_setup_authorizer=lambda *_: None,
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        response = await client.post(
            "/runs/run-1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"data": {"commandType": "comment", "params": {"message": "bypass"}}},
        )

    assert response.status_code == 409
    assert "protocol-less" in response.text


@pytest.mark.asyncio
async def test_prestart_setup_rechecks_state_inside_transition_lock() -> None:
    token = "a-secure-offline-mutation-token-1234"
    states = iter(
        [
            _state(started=False, protocol_less=True),
            _state(started=True, protocol_less=True),
        ]
    )
    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(
            _downstream,
            ProtocolRunAuthority(lambda: next(states)),
            RunMutationGate(),
            mutation_api_token=token,
            prestart_setup_authorizer=lambda *_: None,
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        response = await client.post(
            "/runs/run-1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"data": {"commandType": "comment", "params": {"message": "setup"}}},
        )

    assert response.status_code == 409
    assert "started or changed" in response.text


@pytest.mark.asyncio
async def test_named_checkpoint_resume_requires_token_and_is_audited() -> None:
    token = "a-secure-offline-mutation-token-1234"
    authorized: list[tuple[str, str, str]] = []
    authority = ProtocolRunAuthority(lambda: _state(checkpoint_id="checkpoint-1"))
    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(
            _downstream,
            authority,
            RunMutationGate(),
            mutation_api_token=token,
            checkpoint_resume_authorizer=lambda run_id, checkpoint_id, host: authorized.append(
                (run_id, checkpoint_id, host)
            ),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        missing = await client.post("/runs/run-1/actions", json={"data": {"actionType": "play"}})
        wrong = await client.post(
            "/runs/run-1/actions",
            headers={"Authorization": "Bearer wrong"},
            json={"data": {"actionType": "play"}},
        )
        accepted = await client.post(
            "/runs/run-1/actions",
            headers={"Authorization": f"Bearer {token}"},
            json={"data": {"actionType": "play"}},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert authorized == [("run-1", "checkpoint-1", "127.0.0.1")]


@pytest.mark.asyncio
async def test_read_only_health_survives_run_state_provider_failure() -> None:
    def broken_state() -> ProtocolRunState:
        raise RuntimeError("state unavailable")

    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(_downstream, ProtocolRunAuthority(broken_state), RunMutationGate())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        response = await client.get("/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stop_survives_run_state_provider_failure() -> None:
    def broken_state() -> ProtocolRunState:
        raise RuntimeError("state unavailable")

    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(_downstream, ProtocolRunAuthority(broken_state), RunMutationGate())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        response = await client.post(
            "/runs/run-1/actions",
            json={"data": {"actionType": "stop"}},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_error_recovery_resume_requires_token_and_is_audited() -> None:
    token = "a-secure-offline-mutation-token-1234"
    authorized: list[tuple[str, str]] = []
    authority = ProtocolRunAuthority(lambda: _state(status="awaiting-recovery"))
    transport = httpx.ASGITransport(
        app=RunMutationHttpGuard(
            _downstream,
            authority,
            RunMutationGate(),
            mutation_api_token=token,
            recovery_resume_authorizer=lambda run_id, host: authorized.append((run_id, host)),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://connector") as client:
        missing = await client.post(
            "/runs/run-1/actions",
            json={"data": {"actionType": "resume-from-recovery"}},
        )
        accepted = await client.post(
            "/runs/run-1/actions",
            headers={"Authorization": f"Bearer {token}"},
            json={"data": {"actionType": "resume-from-recovery"}},
        )

    assert missing.status_code == 401
    assert accepted.status_code == 200
    assert authorized == [("run-1", "127.0.0.1")]
