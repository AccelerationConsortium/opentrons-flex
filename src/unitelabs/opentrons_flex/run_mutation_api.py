"""FastAPI adapter for controlled Protocol Engine mutations."""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from .run_mutation import (
    MutationError,
    MutationGateReleaseRequest,
    MutationLedgerError,
    MutationNotAllowedError,
    MutationRequest,
    MutationResult,
    MutationSnapshot,
    MutationValidationError,
    RunMutationCoordinator,
)


def create_run_mutation_router(coordinator: RunMutationCoordinator, *, api_token: str) -> APIRouter:
    """Create connector-owned run-mutation routes around one coordinator."""
    if len(api_token) < 32:
        message = "Controlled run mutation requires an API token of at least 32 random characters."
        raise ValueError(message)
    router = APIRouter(prefix="/unitelabs", tags=["UNITELABS run mutation"])

    @router.get(
        "/runs/{run_id}/mutation-snapshot",
        response_model=MutationSnapshot,
        summary="Read authoritative state for a controlled run mutation",
    )
    async def get_mutation_snapshot(run_id: str, request: Request) -> MutationSnapshot:
        _authorize(request, api_token)
        try:
            return coordinator.snapshot(run_id)
        except MutationNotAllowedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.get(
        "/runs/{run_id}/mutations",
        summary="Read the durable mutation audit trail",
    )
    async def get_mutations(run_id: str, request: Request) -> list[dict[str, Any]]:
        _authorize(request, api_token)
        return coordinator.audit_records(run_id)

    @router.post(
        "/runs/{run_id}/mutations",
        response_model=MutationResult,
        status_code=status.HTTP_201_CREATED,
        summary="Validate and enqueue steps at an explicit mutation checkpoint",
    )
    async def create_mutation(
        run_id: str,
        mutation: MutationRequest,
        request: Request,
    ) -> MutationResult:
        _authorize(request, api_token)
        _authorize_actor(mutation.actor, coordinator.authenticated_actor)
        try:
            return await coordinator.mutate(run_id, mutation, _client_host(request))
        except (MutationNotAllowedError, MutationValidationError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except MutationLedgerError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except MutationError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    @router.post(
        "/runs/{run_id}/mutation-hold/release",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
        summary="Audit and discard a rejected proposal so the paused run may resume",
    )
    async def release_mutation_hold(
        run_id: str,
        release: MutationGateReleaseRequest,
        request: Request,
    ) -> Response:
        _authorize(request, api_token)
        _authorize_actor(release.actor, coordinator.authenticated_actor)
        try:
            await coordinator.release_rejected_hold(run_id, release, _client_host(request))
        except MutationValidationError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _client_host(request: Request) -> str:
    client: Annotated[object | None, "Starlette client address"] = request.client
    host = getattr(client, "host", None)
    return host if isinstance(host, str) else "unknown"


def _authorize(request: Request, expected_token: str) -> None:
    supplied = request.headers.get("authorization", "")
    scheme, separator, token = supplied.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not hmac.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid controlled-mutation bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _authorize_actor(claimed_actor: str, authenticated_actor: str | None) -> None:
    if authenticated_actor is not None and not hmac.compare_digest(claimed_actor, authenticated_actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The request actor does not match the identity bound to this mutation credential.",
        )


__all__ = ["create_run_mutation_router"]
