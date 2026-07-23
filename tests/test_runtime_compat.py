from __future__ import annotations

from unittest.mock import patch

import pytest

from unitelabs.opentrons_flex import runtime_compat


def _matching_report(*, require_robot_server: bool = True) -> runtime_compat.RuntimeCompatibilityReport:
    package_versions = {
        "unitelabs-opentrons-flex": "0.9.1",
        "opentrons": "9.0.0",
        **runtime_compat.SUPPORTED_RUNTIME_PACKAGES,
    }
    with (
        patch.object(
            runtime_compat,
            "_package_version",
            side_effect=package_versions.__getitem__,
        ),
        patch.object(runtime_compat.sys, "version_info", (3, 10, 20)),
        patch.object(runtime_compat, "_inspect_robot_server", return_value=("9.0.0", "/robot_server", ())),
        patch.object(runtime_compat, "_missing_symbol_issue", return_value=None),
        patch.object(runtime_compat, "_module_outside_release_issue", return_value=None),
    ):
        return runtime_compat.inspect_runtime_compatibility(require_robot_server=require_robot_server)


def test_matching_runtime_enables_base_and_mutation() -> None:
    report = _matching_report()

    assert report.base_compatible is True
    assert report.mutation_compatible is True
    assert report.robot_server_version == "9.0.0"
    assert report.issues == ()


def test_opentrons_version_mismatch_fails_before_hardware() -> None:
    package_versions = {
        "unitelabs-opentrons-flex": "0.9.1",
        "opentrons": "8.8.1",
        **runtime_compat.SUPPORTED_RUNTIME_PACKAGES,
    }
    with (
        patch.object(
            runtime_compat,
            "_package_version",
            side_effect=package_versions.__getitem__,
        ),
        patch.object(runtime_compat.sys, "version_info", (3, 10, 20)),
        patch.object(runtime_compat, "_missing_symbol_issue", return_value=None),
        patch.object(runtime_compat, "_module_outside_release_issue", return_value=None),
    ):
        report = runtime_compat.inspect_runtime_compatibility(require_robot_server=False)

    assert report.base_compatible is False
    assert "expected Opentrons 9.0.0" in report.issues[0]


def test_http_dependency_drift_only_blocks_embedded_robot_server_profile() -> None:
    package_versions = {
        "unitelabs-opentrons-flex": "0.9.1",
        "opentrons": "9.0.0",
        **runtime_compat.SUPPORTED_RUNTIME_PACKAGES,
        "python-multipart": "0.0.6",
    }
    with (
        patch.object(
            runtime_compat,
            "_package_version",
            side_effect=package_versions.__getitem__,
        ),
        patch.object(runtime_compat.sys, "version_info", (3, 10, 20)),
        patch.object(runtime_compat, "_inspect_robot_server", return_value=("9.0.0", "/robot_server", ())),
        patch.object(runtime_compat, "_missing_symbol_issue", return_value=None),
        patch.object(runtime_compat, "_module_outside_release_issue", return_value=None),
    ):
        standalone = runtime_compat.inspect_runtime_compatibility(require_robot_server=False)
        embedded = runtime_compat.inspect_runtime_compatibility(require_robot_server=True)

    assert standalone.base_compatible is True
    assert embedded.base_compatible is False
    assert "python-multipart 0.0.6 is unsupported" in embedded.issues[0]


def test_require_compatible_runtime_raises_actionable_error() -> None:
    incompatible = runtime_compat.RuntimeCompatibilityReport(
        connector_version="0.9.1",
        python_version="3.11.0",
        opentrons_version="9.0.0",
        robot_server_version=None,
        robot_server_source=None,
        runtime_package_versions={},
        base_compatible=False,
        mutation_compatible=False,
        issues=("Python 3.11.0 is unsupported; expected Python 3.10.x.",),
        mutation_issues=("The base connector runtime is incompatible.",),
    )
    with (
        patch.object(runtime_compat, "inspect_runtime_compatibility", return_value=incompatible),
        pytest.raises(RuntimeError, match=r"before hardware|runtime preflight failed"),
    ):
        runtime_compat.require_compatible_runtime(require_robot_server=False)


def test_mutation_configuration_reports_every_missing_guard() -> None:
    report = _matching_report(require_robot_server=False)

    issues = runtime_compat.mutation_configuration_issues(
        report,
        ledger_path=None,
        token="short",
        actor=" ",
    )

    assert len(issues) == 3
    assert "ledger" in issues[0]
    assert "32 characters" in issues[1]
    assert "actor" in issues[2]


def test_opentrons_module_outside_active_release_is_rejected() -> None:
    package_versions = {
        "unitelabs-opentrons-flex": "0.9.1",
        "opentrons": "9.0.0",
        **runtime_compat.SUPPORTED_RUNTIME_PACKAGES,
    }
    with (
        patch.object(
            runtime_compat,
            "_package_version",
            side_effect=package_versions.__getitem__,
        ),
        patch.object(runtime_compat.sys, "version_info", (3, 10, 20)),
        patch.object(runtime_compat, "_missing_symbol_issue", return_value=None),
        patch.object(
            runtime_compat,
            "_module_outside_release_issue",
            side_effect=lambda name: "opentrons is loaded outside the active release." if name == "opentrons" else None,
        ),
    ):
        report = runtime_compat.inspect_runtime_compatibility(require_robot_server=False)

    assert report.base_compatible is False
    assert "outside the active release" in report.issues[0]
