"""Tests for the flexctl one-command configuration and deployment tool."""

import json
from pathlib import Path

import pytest

from unitelabs.opentrons_flex import OpentronsFlexConfig, flexctl

_ROOT = Path(__file__).parents[2]


class _RecordingRunner(flexctl.Runner):
    """Record commands and answer SSH probes from a script of responses."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        super().__init__(dry_run=False, cwd=_ROOT, log=lambda _line: None, shell="sh")
        self.calls: list[tuple[list[str], str | None]] = []
        self.responses = responses or {}

    def run(self, argv, *, stdin=None, check=True, capture=False):
        self.calls.append((list(argv), stdin))
        joined = " ".join(argv)
        for needle, response in self.responses.items():
            if needle in joined:
                return response
        return ""


def _wheels(tmp_path: Path, version: str) -> Path:
    wheels = tmp_path / "dist_arm"
    wheels.mkdir()
    (wheels / "runtime-manifest.json").write_text(json.dumps({"schemaVersion": 1, "connectorVersion": version}))
    return wheels


@pytest.mark.parametrize("profile", flexctl.PROFILES)
def test_every_profile_produces_a_loadable_configuration(profile: str) -> None:
    config = OpentronsFlexConfig.validate(flexctl.build_config(profile))

    assert config.use_simulator is profile.startswith("simulator")
    if profile == "simulator-full":
        assert set(config.simulated_modules) == set(flexctl.ALL_SIMULATED_MODULES)
        assert config.with_robot_server
    if profile == "connector-unit":
        assert config.sila_server.hostname == "127.0.0.1"
        assert config.labware_movement_config


def test_config_init_refuses_to_overwrite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "config.json"
    output.write_text("{}")

    assert flexctl.main(["config", "init", "--profile", "simulator", "--output", str(output)]) == 1
    assert "--force" in capsys.readouterr().err
    assert flexctl.main(["config", "init", "--profile", "simulator", "--output", str(output), "--force"]) == 0
    assert json.loads(output.read_text())["use_simulator"] is True


def test_detect_maps_attached_hardware_to_simulated_modules() -> None:
    responses = {
        "/instruments": {
            "data": [
                {"mount": "left", "instrumentModel": "p1000_single_v3.6", "ok": True},
                {"mount": "extension", "instrumentModel": "gripperV1.3", "ok": True},
            ]
        },
        "/modules": {"data": [{"moduleModel": "heaterShakerModuleV1"}, {"moduleModel": "thermocyclerModuleV2"}]},
    }

    detected = flexctl.detect_robot_hardware("10.0.0.42", fetch=lambda url: responses["/" + url.rsplit("/", 1)[-1]])

    assert detected["simulated_modules"] == ["heater_shaker", "thermocycler", "gripper"]
    assert "left: p1000_single_v3.6" in detected["instruments"]


def test_doctor_flags_a_stale_wheel_bundle(tmp_path: Path) -> None:
    checks = {check.name: check for check in flexctl.doctor_checks(_ROOT, _wheels(tmp_path, "0.0.1"))}

    assert not checks["wheel bundle"].ok
    assert "rebuild" in checks["wheel bundle"].detail


def test_doctor_flags_a_bundle_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "dist_arm").mkdir()
    checks = {check.name: check for check in flexctl.doctor_checks(_ROOT, tmp_path / "dist_arm")}

    assert "old build" in checks["wheel bundle"].detail


def test_up_creates_credential_switches_off_connector_then_runs_reviewed_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wheels = _wheels(tmp_path, flexctl.checkout_version(_ROOT))
    monkeypatch.setattr(flexctl, "doctor_checks", lambda *_: [])
    runner = _RecordingRunner({"echo present || echo missing": "missing\n", "systemctl is-active": "active\n"})

    code = flexctl.main(
        ["up", "flex-1", "--mode", "unit", "--wheels", str(wheels), "--operator", "alice"],
        runner=runner,
    )

    assert code == 0
    commands = [argv for argv, _ in runner.calls]
    credential = next(stdin for argv, stdin in runner.calls if stdin is not None)
    assert "UNITELABS_RUN_MUTATION_ACTOR=alice" in credential
    assert all(credential.split("TOKEN=")[1].split()[0] not in " ".join(argv) for argv in commands)
    scripts = [argv[1:3] for argv in commands if argv[0] == "sh"]
    assert scripts == [
        ["scripts/switch_mode.sh", "flex-1"],
        ["deploy.sh", "flex-1"],
        ["scripts/install_connector_service.sh", "flex-1"],
        ["scripts/switch_mode.sh", "flex-1"],
    ]
    assert commands[-1][-1] == "--runtime-only"
    assert "shown once" in capsys.readouterr().out


def test_up_requires_an_operator_for_a_new_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flexctl, "doctor_checks", lambda *_: [])
    runner = _RecordingRunner({"echo present || echo missing": "missing\n"})

    assert flexctl.main(["up", "flex-1"], runner=runner) == 1
    assert not any(argv[0] == "sh" for argv, _ in runner.calls)


def test_up_stops_before_touching_the_robot_when_local_checks_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _RecordingRunner()

    assert flexctl.main(["up", "flex-1", "--no-build", "--wheels", str(tmp_path / "missing")], runner=runner) == 1
    assert runner.calls == []
    assert "Local checks failed" in capsys.readouterr().err


def test_up_fetches_a_missing_wheel_bundle_before_deploying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version = flexctl.checkout_version(_ROOT)
    fetched: list[Path] = []

    def fake_obtain(repository: Path, wheels: Path, wanted: str, **_kwargs: object) -> str:
        fetched.append(wheels)
        wheels.mkdir(parents=True, exist_ok=True)
        (wheels / "runtime-manifest.json").write_text(json.dumps({"schemaVersion": 1, "connectorVersion": wanted}))
        return "downloaded"

    monkeypatch.setattr(flexctl.setup_tools, "obtain_wheels", fake_obtain)
    monkeypatch.setattr(flexctl.shutil, "which", lambda _name: "/usr/bin/x")
    runner = _RecordingRunner({"echo present || echo missing": "present\n"})

    code = flexctl.main(["up", "flex-1", "--wheels", str(tmp_path / "dist_arm")], runner=runner)

    assert code == 0
    assert fetched == [(tmp_path / "dist_arm").resolve()]
    assert json.loads((tmp_path / "dist_arm" / "runtime-manifest.json").read_text())["connectorVersion"] == version


def test_deck_reconcile_refuses_while_connector_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _RecordingRunner({"systemctl is-active": "active\n"})

    code = flexctl.main(
        ["deck", "flex-1", "reconcile", "--operator", "alice", "--reason", "jam", "--empty", "--confirm"],
        runner=runner,
    )

    assert code == 1
    assert not any("deck_ledger reconcile" in " ".join(argv) for argv, _ in runner.calls)


def test_deck_reconcile_quotes_operator_input() -> None:
    runner = _RecordingRunner({"systemctl is-active": "inactive\n"})

    code = flexctl.main(
        [
            "deck",
            "flex-1",
            "reconcile",
            "--operator",
            "alice",
            "--reason",
            "plate jam; rm -rf /",
            "--set",
            "A1=asms-plate",
            "--confirm",
        ],
        runner=runner,
    )

    assert code == 0
    remote = runner.calls[-1][0][-1]
    assert "'plate jam; rm -rf /'" in remote
    assert "--set A1=asms-plate" in remote


@pytest.mark.parametrize("host", ["-oProxyCommand=x", "a b", ""])
def test_hosts_that_could_inject_ssh_options_are_rejected(host: str) -> None:
    with pytest.raises(flexctl.FlexctlError):
        flexctl._validate_host(host)


def test_deploy_rejects_a_wheel_bundle_without_manifest_before_ssh(tmp_path: Path) -> None:
    import subprocess

    stale = tmp_path / "dist_arm"
    stale.mkdir()
    result = subprocess.run(
        ["sh", str(_ROOT / "deploy.sh"), "flex-1", str(stale)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert "predates verified releases" in result.stderr


def test_deploy_rejects_a_bundle_built_for_another_version_before_ssh(tmp_path: Path) -> None:
    import subprocess

    wheels = _wheels(tmp_path, "0.0.1")
    result = subprocess.run(
        ["sh", str(_ROOT / "deploy.sh"), "flex-1", str(wheels)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert "was built for connector 0.0.1" in result.stderr


def test_dry_run_up_does_not_invent_a_credential(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _RecordingRunner()
    runner.dry_run = True

    assert flexctl.main(["--dry-run", "up", "flex-1", "--operator", "alice"], runner=runner) == 0

    assert "Token" not in capsys.readouterr().out
    assert all(stdin is None for _, stdin in runner.calls)
