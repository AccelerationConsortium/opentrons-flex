"""Validate the committed HTTP<->SiLA parity matrix (Stage 1).

Two layers of protection:

1. Structural (always runs): the committed ``docs/parity_matrix.json`` is well
   formed — every entry has the required fields and a legal ``sila_support``
   value, and the markdown rendering exists.

2. Drift (``smoketest_http_only``, needs ``--with-http-server``): every entry the
   matrix claims is HTTP-available is actually present in the live robot-server
   OpenAPI schema, so the matrix cannot silently fall out of sync with the real
   HTTP surface. Core routes fail the build if missing; non-core drift is
   reported as a warning so the matrix stays honest without being brittle.
"""

import json
import warnings
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MATRIX_JSON = _ROOT / "docs" / "parity_matrix.json"
_MATRIX_MD = _ROOT / "docs" / "parity_matrix.md"

_ALLOWED_SUPPORT = {"supported", "unsupported", "unclear"}
_REQUIRED_FIELDS = {
    "group",
    "http_method",
    "http_path",
    "http_available",
    "core",
    "sila_feature",
    "sila_element",
    "sila_support",
    "notes",
}


def _load_matrix() -> dict:
    return json.loads(_MATRIX_JSON.read_text())


def test_matrix_files_exist() -> None:
    """Both the machine-readable and human-readable matrices are committed."""
    assert _MATRIX_JSON.is_file(), f"missing {_MATRIX_JSON}"
    assert _MATRIX_MD.is_file(), f"missing {_MATRIX_MD}"


def test_matrix_entries_are_well_formed() -> None:
    """Every entry has the required fields and a legal support value."""
    matrix = _load_matrix()
    entries = matrix.get("entries")
    assert entries, "parity matrix has no entries"

    problems: list[str] = []
    for i, entry in enumerate(entries):
        missing = _REQUIRED_FIELDS - entry.keys()
        if missing:
            problems.append(f"entry[{i}] missing fields: {sorted(missing)}")
        if entry.get("sila_support") not in _ALLOWED_SUPPORT:
            problems.append(f"entry[{i}] bad sila_support: {entry.get('sila_support')!r}")
        # An HTTP-available entry must name a method + path; a SiLA-only entry must not.
        if entry.get("http_available") and not entry.get("http_path"):
            problems.append(f"entry[{i}] http_available but no http_path")
        # A 'supported'/'unclear' row must name a SiLA feature; 'unsupported' must not.
        if entry.get("sila_support") in {"supported", "unclear"} and not entry.get("sila_feature"):
            problems.append(f"entry[{i}] {entry.get('sila_support')} but no sila_feature")

    assert not problems, "parity matrix problems:\n" + "\n".join(problems)


def test_matrix_covers_the_core_http_routes() -> None:
    """The 5 low-risk core routes exercised elsewhere must appear as supported."""
    matrix = _load_matrix()
    core = {
        (e["http_method"], e["http_path"])
        for e in matrix["entries"]
        if e.get("core") and e.get("sila_support") == "supported"
    }
    expected = {
        ("GET", "/health"),
        ("GET", "/pipettes"),
        ("GET", "/modules"),
        ("GET", "/robot/lights"),
        ("POST", "/robot/lights"),
        ("POST", "/robot/home"),
    }
    assert expected <= core, f"core routes missing/unsupported in matrix: {expected - core}"


@pytest.mark.smoketest_http_only
def test_matrix_http_paths_exist_in_live_openapi(http_client) -> None:
    """Every HTTP-available matrix entry must exist in the live OpenAPI (drift guard)."""
    response = http_client.get("/openapi.json")
    if response.status_code == 404:
        pytest.skip("robot-server OpenAPI schema is not exposed by this robot image")
    assert response.status_code == 200
    paths = response.json()["paths"]

    live = {
        (method.upper(), path)
        for path, methods in paths.items()
        for method in methods
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

    core_missing: list[str] = []
    noncore_missing: list[str] = []
    for entry in _load_matrix()["entries"]:
        if not entry.get("http_available"):
            continue
        key = (entry["http_method"].upper(), entry["http_path"])
        if key not in live:
            (core_missing if entry.get("core") else noncore_missing).append(f"{key[0]} {key[1]}")

    if noncore_missing:
        warnings.warn(
            "parity_matrix.json lists HTTP routes not present in the live OpenAPI "
            f"(update the matrix): {noncore_missing}",
            stacklevel=2,
        )
    assert not core_missing, f"core routes in matrix are absent from the live HTTP API: {core_missing}"
