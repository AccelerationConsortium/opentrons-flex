"""Fail-closed HTTP helpers for robot readiness checks."""

from __future__ import annotations

import http.client
import urllib.request
from typing import cast


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent readiness probes from following redirects to another endpoint."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """Decline every redirect."""


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def open_no_redirect(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> http.client.HTTPResponse:
    """Open one HTTP request without following redirects."""
    return cast(
        http.client.HTTPResponse,
        _NO_REDIRECT_OPENER.open(request, timeout=timeout),
    )
