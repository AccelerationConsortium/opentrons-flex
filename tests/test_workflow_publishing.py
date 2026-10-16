from __future__ import annotations

import zipfile

from workflows.scripts import publish_workflows


def test_contract_change_marks_flex_acceptance_workflow_affected() -> None:
    workflows = publish_workflows.discover_workflows()

    affected = publish_workflows.affected_workflows(
        ["packages/flex-acceptance-contract/src/unitelabs_flex_acceptance_contract/__init__.py"],
        workflows,
    )

    assert affected == ["flex-system-acceptance"]


def test_repository_root_workflow_path_marks_workflow_affected() -> None:
    workflows = publish_workflows.discover_workflows()

    affected = publish_workflows.affected_workflows(
        ["workflows/flex-system-acceptance/src/flex_system_acceptance/workflow.py"],
        workflows,
    )

    assert affected == ["flex-system-acceptance"]


def test_publish_script_change_marks_every_workflow_affected() -> None:
    workflows = publish_workflows.discover_workflows()

    affected = publish_workflows.affected_workflows(
        ["workflows/scripts/publish_workflows.py"],
        workflows,
    )

    assert affected == sorted(workflows)


def test_flex_acceptance_contract_is_vendored_not_platform_installed() -> None:
    workflow_dir = publish_workflows.REPO_ROOT / "flex-system-acceptance"

    dependencies = publish_workflows.resolve_dependencies(workflow_dir)

    assert all(not dependency.startswith("unitelabs-flex-acceptance-contract") for dependency in dependencies)
    assert "unitelabs-sdk[automate]~=0.11.3" in dependencies


def test_flex_acceptance_bundle_contains_contract_source() -> None:
    workflow_dir = publish_workflows.REPO_ROOT / "flex-system-acceptance"
    bundle_path = publish_workflows.build_bundle(
        workflow_dir,
        publish_workflows.load_identity(workflow_dir),
    )

    try:
        with zipfile.ZipFile(bundle_path) as bundle:
            names = set(bundle.namelist())
        assert "unitelabs_flex_acceptance_contract/__init__.py" in names
        assert "flex-system-acceptance/src/flex_system_acceptance/workflow.py" in names
    finally:
        bundle_path.unlink(missing_ok=True)
