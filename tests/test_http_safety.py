from __future__ import annotations

import urllib.request

from unitelabs.opentrons_flex.http_safety import _NoRedirectHandler


def test_readiness_http_redirect_handler_declines_redirects() -> None:
    handler = _NoRedirectHandler()
    request = urllib.request.Request("http://127.0.0.1:31950/health")

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {"Location": "http://192.0.2.10/internal"},
        "http://192.0.2.10/internal",
    )

    assert redirected is None
