"""Parity smoke tests for the Opentrons robot HTTP API exposed by the connector."""

import pytest

CORE_HTTP_ROUTES = (
    ("GET", "/health"),
    ("GET", "/pipettes"),
    ("GET", "/modules"),
    ("GET", "/robot/lights"),
    ("POST", "/robot/lights"),
    ("POST", "/robot/home"),
)

SAFE_SMOKETEST_POSTS: dict[str, dict] = {
    "/robot/lights": {"on": True},
    "/robot/home": {"target": "pipette", "mount": "left"},
}

_CLIENT_SUPPLIED_HEADERS = {"opentrons-version"}


def _openapi_paths(http_client) -> dict:
    response = http_client.get("/openapi.json")
    if response.status_code == 404:
        pytest.skip("robot-server OpenAPI schema is not exposed by this robot image")
    assert response.status_code == 200
    return response.json()["paths"]


def _operations(paths: dict) -> list[tuple[str, str, dict]]:
    operations: list[tuple[str, str, dict]] = []
    for path, methods in paths.items():
        for method, operation in methods.items():
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                operations.append((method.upper(), path, operation))
    return operations


def _has_required_parameters(operation: dict) -> bool:
    """Return whether an operation needs input not supplied by ``http_client``."""
    for parameter in operation.get("parameters", []):
        if not parameter.get("required", False):
            continue
        if parameter.get("in") == "header" and str(parameter.get("name", "")).casefold() in _CLIENT_SUPPLIED_HEADERS:
            continue
        return True
    return False


@pytest.mark.parametrize(
    ("parameters", "expected"),
    (
        ([], False),
        ([{"name": "Opentrons-Version", "in": "header", "required": True}], False),
        ([{"name": "opentrons-version", "in": "header", "required": True}], False),
        ([{"name": "runId", "in": "query", "required": True}], True),
        ([{"name": "Authorization", "in": "header", "required": True}], True),
        ([{"name": "optional", "in": "query", "required": False}], False),
    ),
)
def test_required_parameter_filter_accounts_for_client_headers(parameters: list[dict], expected: bool) -> None:
    assert _has_required_parameters({"parameters": parameters}) is expected


@pytest.mark.robot_http_only
def test_core_opentrons_http_routes_are_exposed(http_client) -> None:
    """The connector should keep the core Opentrons HTTP route surface available."""
    paths = _openapi_paths(http_client)

    missing: list[str] = []
    for method, path in CORE_HTTP_ROUTES:
        methods = paths.get(path, {})
        if method.lower() not in methods:
            missing.append(f"{method} {path}")

    assert not missing, f"Missing Opentrons HTTP routes: {missing}"


@pytest.mark.smoketest_http_only
def test_opentrons_http_openapi_inventory_is_available(http_client) -> None:
    """Smoketest mode should expose the robot-server operation inventory for parity checks."""
    operations = _operations(_openapi_paths(http_client))

    assert operations, "No Opentrons HTTP API operations were discovered"
    operation_ids = [operation.get("operationId") for _, _, operation in operations]
    assert all(operation_ids), f"Operations without operationId: {operations}"
    assert len(operation_ids) == len(set(operation_ids)), "Duplicate OpenAPI operationId values found"


@pytest.mark.smoketest_http_only
def test_all_no_argument_get_routes_execute_in_smoketest(http_client) -> None:
    """Every no-argument GET route in the Opentrons HTTP API should remain callable."""
    operations = _operations(_openapi_paths(http_client))
    get_routes = [
        path
        for method, path, operation in operations
        if method == "GET" and "{" not in path and not _has_required_parameters(operation)
    ]

    assert get_routes, "No safe GET routes were discovered in the Opentrons HTTP API"

    failures: list[str] = []
    for path in sorted(set(get_routes)):
        response = http_client.get(path)
        if response.status_code in {404, 405} or response.status_code >= 500:
            failures.append(f"GET {path} -> {response.status_code}: {response.text[:200]}")

    assert not failures, "Opentrons HTTP GET parity failures:\n" + "\n".join(failures)


@pytest.mark.smoketest_http_only
def test_known_low_risk_post_routes_execute_in_smoketest(http_client) -> None:
    """Known safe POST routes should execute against the local simulator-backed connector."""
    paths = _openapi_paths(http_client)

    failures: list[str] = []
    for path, payload in SAFE_SMOKETEST_POSTS.items():
        if "post" not in paths.get(path, {}):
            failures.append(f"POST {path} is missing from OpenAPI")
            continue
        response = http_client.post(path, json=payload)
        if response.status_code >= 400:
            failures.append(f"POST {path} -> {response.status_code}: {response.text[:200]}")

    assert not failures, "Opentrons HTTP POST parity failures:\n" + "\n".join(failures)


@pytest.mark.robot_http_only
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    (
        ("GET", "/health", None),
        ("GET", "/pipettes", None),
        ("GET", "/modules", None),
        ("GET", "/robot/lights", None),
        ("POST", "/robot/lights", {"on": True}),
    ),
)
def test_core_opentrons_http_routes_respond(http_client, method: str, path: str, payload: dict | None) -> None:
    """Low-risk Opentrons HTTP endpoints should execute successfully in smoketest mode."""
    response = http_client.request(method, path, json=payload)
    assert response.status_code < 400, response.text
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.robot_http_only
def test_robot_http_api_surfaces_bad_command_errors(http_client) -> None:
    """Invalid HTTP commands should return an explicit client/server error, not a silent success."""
    response = http_client.post("/robot/home", json={"target": "not-a-target", "mount": "left"})

    assert 400 <= response.status_code < 500
    assert response.text
