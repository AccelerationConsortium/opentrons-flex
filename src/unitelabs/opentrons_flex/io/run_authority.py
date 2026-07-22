"""Protocol Engine ownership and fail-closed mutation holds."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ._errors import RunOwnershipError

_Receive = Callable[[], Awaitable[dict[str, object]]]
_Send = Callable[[dict[str, object]], Awaitable[None]]
_AsgiApp = Callable[[dict[str, object], _Receive, _Send], Awaitable[None]]
_PRESTART_COMMAND_TYPES = frozenset(
    {
        "comment",
        "loadLabware",
        "loadLid",
        "loadLidStack",
        "loadLiquid",
        "loadModule",
        "loadPipette",
    }
)


@dataclass(frozen=True)
class ProtocolRunState:
    """Minimal authoritative state needed to arbitrate shared hardware."""

    run_id: str | None
    status: str
    started: bool
    terminal: bool
    mutation_checkpoint_id: str | None = None
    protocol_less: bool = False

    @property
    def owns_hardware(self) -> bool:
        """Whether Protocol Engine has a non-terminal current run."""
        return self.run_id is not None and not self.terminal


class ProtocolRunAuthority:
    """Read current Protocol Engine ownership before every direct operation."""

    def __init__(self, state_provider: Callable[[], ProtocolRunState]) -> None:
        self._state_provider = state_provider
        self._last_state = ProtocolRunState(run_id=None, status="idle", started=False, terminal=True)

    def current(self) -> ProtocolRunState:
        """Return authoritative run state, retaining an active gate on provider failure."""
        try:
            state = self._state_provider()
        except Exception as exc:
            if self._last_state.owns_hardware:
                message = (
                    "Protocol Engine ownership could not be refreshed while a run was active. "
                    "Direct connector control remains disabled; inspect or stop the HTTP run before retrying."
                )
                raise RunOwnershipError(message) from exc
            raise
        self._last_state = state
        return state

    def assert_direct_control_allowed(self) -> None:
        """Reject uncoordinated SiLA hardware control while PE owns the Flex."""
        state = self.current()
        if state.owns_hardware:
            message = (
                f"Protocol Engine run {state.run_id} owns the Flex in state {state.status!r}. "
                "Stop that run, or use a controlled UNITELABS mutation checkpoint; "
                "independent SiLA actuation is disabled."
            )
            raise RunOwnershipError(message)


class RunAwareLock:
    """An asyncio-lock-compatible hardware lock with direct-control gating."""

    def __init__(self, authority: ProtocolRunAuthority) -> None:
        self._lock = asyncio.Lock()
        self.authority = authority

    async def acquire(self) -> bool:
        """Acquire for SiLA/direct control after checking PE ownership."""
        self.authority.assert_direct_control_allowed()
        acquired = await self._lock.acquire()
        try:
            # A run may have started while this caller was queued behind a
            # Protocol Engine operation. Re-check after ownership of the
            # hardware mutex transfers to close that waiting-call race.
            self.authority.assert_direct_control_allowed()
        except BaseException:
            self._lock.release()
            raise
        return acquired

    async def acquire_protocol_engine(self) -> bool:
        """Acquire for the embedded robot-server/Protocol Engine path."""
        return await self._lock.acquire()

    async def acquire_observation(self) -> bool:
        """Acquire for serialized read-only observation without actuation authority."""
        return await self._lock.acquire()

    @asynccontextmanager
    async def observation(self) -> AsyncIterator[None]:
        """Serialize a read without denying it merely because PE owns actuation."""
        await self.acquire_observation()
        try:
            yield
        finally:
            self.release()

    def release(self) -> None:
        """Release the underlying hardware lock."""
        self._lock.release()

    def locked(self) -> bool:
        """Return whether the underlying hardware lock is held."""
        return self._lock.locked()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *args: object) -> None:
        self.release()


@dataclass(frozen=True)
class MutationHold:
    """Reason that a run must remain paused."""

    reason: str
    fatal: bool


class RunMutationGate:
    """Latch validation or partial-enqueue failures until explicitly resolved."""

    def __init__(self) -> None:
        self._holds: dict[str, MutationHold] = {}
        self.transition_lock = asyncio.Lock()

    def hold(self, run_id: str, reason: str, *, fatal: bool) -> None:
        """Prevent play/resume for a run."""
        self._holds[run_id] = MutationHold(reason=reason, fatal=fatal)

    def clear(self, run_id: str) -> None:
        """Clear a recoverable validation hold after correction or acknowledgement."""
        hold = self._holds.get(run_id)
        if hold is not None and hold.fatal:
            message = "A partial mutation was enqueued; this run must be stopped and cannot be released."
            raise RuntimeError(message)
        self._holds.pop(run_id, None)

    def get(self, run_id: str) -> MutationHold | None:
        """Return the current mutation hold, if any."""
        return self._holds.get(run_id)


class RunMutationHttpGuard:
    """Block raw mid-run commands and play actions that would bypass a mutation hold."""

    def __init__(
        self,
        app: _AsgiApp,
        authority: ProtocolRunAuthority,
        gate: RunMutationGate,
        *,
        mutation_api_token: str | None = None,
        checkpoint_resume_authorizer: Callable[[str, str, str], None] | None = None,
        recovery_resume_authorizer: Callable[[str, str], None] | None = None,
        prestart_setup_authorizer: Callable[[str, str, str, str], None] | None = None,
    ) -> None:
        self._app = app
        self._authority = authority
        self._gate = gate
        self._mutation_api_token = mutation_api_token
        self._checkpoint_resume_authorizer = checkpoint_resume_authorizer
        self._recovery_resume_authorizer = recovery_resume_authorizer
        self._prestart_setup_authorizer = prestart_setup_authorizer

    async def __call__(
        self,
        scope: dict[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        """Enforce the connector-owned mutation boundary."""
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method in {"GET", "HEAD", "OPTIONS"}:
            await self._app(scope, receive, send)
            return

        action_run_id = _run_action_id(path) if method == "POST" else None
        action_type: str | None = None
        replay_receive: _Receive | None = None
        if action_run_id is not None:
            body, replay_receive = await _read_and_replay_body(receive)
            action_type = _action_type(body)
            # Stop is the fail-safe operation. Let it reach robot-server even
            # if authoritative run-state inspection is itself degraded.
            if action_type == "stop":
                await self._app(scope, replay_receive, send)
                return

        state = self._authority.current()

        if method == "POST" and _is_prestart_command(path, state):
            body, setup_receive = await _read_and_replay_body(receive)
            command_type = _approved_prestart_command_type(body)
            if not state.protocol_less:
                await _send_conflict(
                    send,
                    "Pre-start raw setup is available only for an authoritative protocol-less run.",
                )
                return
            if not _has_valid_bearer(scope, self._mutation_api_token):
                await _send_unauthorized(
                    send,
                    "A valid controlled-mutation bearer token is required for pre-start run setup.",
                )
                return
            if command_type is None:
                await _send_conflict(
                    send,
                    "Pre-start setup accepts only non-actuating setup commands for built-in resources.",
                )
                return
            if self._prestart_setup_authorizer is None:
                await _send_conflict(send, "Pre-start setup is disabled because durable audit is unavailable.")
                return
            async with self._gate.transition_lock:
                current = self._authority.current()
                if not _is_prestart_command(path, current) or not current.protocol_less:
                    await _send_conflict(
                        send,
                        "The run started or changed while setup authorization was being checked.",
                    )
                    return
                try:
                    self._prestart_setup_authorizer(
                        current.run_id,
                        command_type,
                        hashlib.sha256(body).hexdigest(),
                        _client_host(scope),
                    )
                except RuntimeError as exc:
                    await _send_conflict(send, f"Pre-start setup could not be durably authorized: {exc}")
                    return
                # Hold the same transition lock through downstream acceptance,
                # so play cannot race setup after the second state check.
                await self._app(scope, setup_receive, send)
                return

        if method == "POST" and _is_raw_run_command(path, state.run_id):
            await _send_conflict(
                send,
                "Raw protocol commands are disabled after a run starts. "
                "Use the UNITELABS controlled mutation endpoint.",
            )
            return

        if method == "POST" and state.run_id is not None and action_run_id == state.run_id:
            if replay_receive is None:  # pragma: no cover - action paths are consumed above
                await _send_conflict(send, "Run action body could not be inspected safely.")
                return
            async with self._gate.transition_lock:
                current = self._authority.current()
                hold = self._gate.get(state.run_id)
                if (
                    current.run_id == state.run_id
                    and hold is not None
                    and action_type
                    in {
                        "play",
                        "resume-from-recovery",
                        "resume-from-recovery-assuming-false-positive",
                    }
                ):
                    await _send_conflict(send, f"Run remains paused by the mutation gate: {hold.reason}")
                    return
                if (
                    current.run_id == state.run_id
                    and action_type == "play"
                    and current.mutation_checkpoint_id is not None
                ):
                    if not _has_valid_bearer(scope, self._mutation_api_token):
                        await _send_unauthorized(
                            send,
                            "A valid controlled-mutation bearer token is required to resume a named checkpoint.",
                        )
                        return
                    if self._checkpoint_resume_authorizer is None:
                        await _send_conflict(
                            send,
                            "Named mutation checkpoint resume is disabled because durable audit is unavailable.",
                        )
                        return
                    try:
                        self._checkpoint_resume_authorizer(
                            state.run_id,
                            current.mutation_checkpoint_id,
                            _client_host(scope),
                        )
                    except RuntimeError as exc:
                        await _send_conflict(send, f"Checkpoint resume could not be durably authorized: {exc}")
                        return
                if (
                    current.run_id == state.run_id
                    and current.status == "awaiting-recovery"
                    and action_type
                    in {
                        "resume-from-recovery",
                        "resume-from-recovery-assuming-false-positive",
                    }
                ):
                    if not _has_valid_bearer(scope, self._mutation_api_token):
                        await _send_unauthorized(
                            send,
                            "A valid controlled-mutation bearer token is required to resume error recovery.",
                        )
                        return
                    if self._recovery_resume_authorizer is None:
                        await _send_conflict(
                            send,
                            "Recovery resume is disabled because durable audit is unavailable.",
                        )
                        return
                    try:
                        self._recovery_resume_authorizer(state.run_id, _client_host(scope))
                    except RuntimeError as exc:
                        await _send_conflict(send, f"Recovery resume could not be durably authorized: {exc}")
                        return
                await self._app(scope, replay_receive, send)
                return

        if (
            method in {"POST", "PUT", "PATCH", "DELETE"}
            and state.owns_hardware
            and not _is_connector_mutation(path, state.run_id)
        ):
            await _send_conflict(
                send,
                f"Protocol Engine run {state.run_id} exclusively owns all Flex and run-state writes. "
                "Use the current run action or the authenticated UNITELABS mutation endpoint.",
            )
            return

        await self._app(scope, receive, send)


def _is_raw_run_command(path: str, run_id: str | None) -> bool:
    return run_id is not None and path.rstrip("/") == f"/runs/{run_id}/commands"


def _run_action_id(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "runs" and parts[1] and parts[2] == "actions":
        return parts[1]
    return None


def _is_prestart_command(path: str, state: ProtocolRunState) -> bool:
    if state.run_id is None or state.started or state.terminal:
        return False
    return path.rstrip("/") == f"/runs/{state.run_id}/commands"


def _approved_prestart_command_type(body: bytes) -> str | None:
    import json

    try:
        payload = json.loads(body)
        data = payload["data"]
        command_type = data["commandType"]
        intent = data.get("intent")
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if intent not in {None, "setup"}:
        return None
    if command_type in {"loadLabware", "loadLid", "loadLidStack"}:
        params = data.get("params")
        if not isinstance(params, dict) or params.get("namespace") != "opentrons":
            return None
    return command_type if command_type in _PRESTART_COMMAND_TYPES else None


def _is_connector_mutation(path: str, run_id: str | None) -> bool:
    return run_id is not None and path.rstrip("/").startswith(f"/unitelabs/runs/{run_id}/")


def _has_valid_bearer(scope: dict[str, object], expected_token: str | None) -> bool:
    if expected_token is None:
        return False
    headers = scope.get("headers", [])
    if not isinstance(headers, list):
        return False
    supplied = ""
    for name, value in headers:
        if name.lower() == b"authorization":
            supplied = value.decode("latin-1")
            break
    scheme, separator, token = supplied.partition(" ")
    return separator == " " and scheme.lower() == "bearer" and hmac.compare_digest(token, expected_token)


def _client_host(scope: dict[str, object]) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client and isinstance(client[0], str):
        return client[0]
    return "unknown"


async def _read_and_replay_body(
    receive: _Receive,
) -> tuple[bytes, _Receive]:
    messages: list[dict[str, object]] = []
    body_parts: list[bytes] = []
    while True:
        message = await receive()
        messages.append(message)
        body = message.get("body", b"")
        if isinstance(body, bytes):
            body_parts.append(body)
        if not bool(message.get("more_body", False)):
            break
    index = 0

    async def replay() -> dict[str, object]:
        nonlocal index
        if index < len(messages):
            result = messages[index]
            index += 1
            return result
        # After replaying the consumed request body, preserve the original
        # connection lifecycle. Synthesizing an immediate disconnect here can
        # cancel Starlette's streaming response before its body is sent.
        return await receive()

    return b"".join(body_parts), replay


def _action_type(body: bytes) -> str | None:
    import json

    try:
        payload = json.loads(body)
        value = payload["data"]["actionType"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


async def _send_conflict(send: _Send, detail: str) -> None:
    await _send_json_error(
        send, status=409, error_id="RunMutationRequired", title="Controlled Run Mutation Required", detail=detail
    )


async def _send_unauthorized(send: _Send, detail: str) -> None:
    await _send_json_error(
        send,
        status=401,
        error_id="RunMutationAuthenticationRequired",
        title="Controlled Run Mutation Authentication Required",
        detail=detail,
        extra_headers=[(b"www-authenticate", b"Bearer")],
    )


async def _send_json_error(
    send: _Send,
    *,
    status: int,
    error_id: str,
    title: str,
    detail: str,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    import json

    content = json.dumps(
        {
            "errors": [
                {
                    "id": error_id,
                    "title": title,
                    "detail": detail,
                }
            ]
        }
    ).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(content)).encode())]
    if extra_headers:
        headers.extend(extra_headers)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": content})


__all__ = [
    "MutationHold",
    "ProtocolRunAuthority",
    "ProtocolRunState",
    "RunAwareLock",
    "RunMutationGate",
    "RunMutationHttpGuard",
]
