"""Tests for flexctl's workstation helpers: robot checks, SSH key installation, wheels, and cleanup."""

import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from unitelabs.opentrons_flex import flexctl, setup_tools

_ROOT = Path(__file__).parents[2]


def _completed(code: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


class _FakeShell:
    """Answer ssh probes and ssh-keygen like the real tools."""

    def __init__(self, ssh_ok_after_key: bool = True) -> None:
        self.key_installed = False
        self.ssh_ok_after_key = ssh_ok_after_key
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("PRIVATE")
            key.with_name(key.name + ".pub").write_text("ecdsa-sha2-nistp521 AAAA flexctl\n")
            return _completed()
        if argv[0] == "ssh":
            return _completed(0 if self.key_installed and self.ssh_ok_after_key else 255)
        return _completed()


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://robot", code, "x", {}, io.BytesIO(body.encode()))


def test_check_flex_accepts_only_a_flex() -> None:
    flex = setup_tools.check_flex("10.0.0.2", http_get=lambda *_: {"robot_model": "OT-3 Standard", "name": "Bob"})
    assert flex.name == "Bob"
    with pytest.raises(setup_tools.SetupError, match="not an Opentrons Flex"):
        setup_tools.check_flex("10.0.0.3", http_get=lambda *_: {"robot_model": "OT-2 Standard"})


def test_wired_connection_installs_an_ecdsa_key_and_ssh_config(tmp_path: Path) -> None:
    shell = _FakeShell()
    posted: list[tuple[str, dict | None]] = []

    def post(_host: str, path: str, data: dict | None) -> object:
        posted.append((path, data))
        shell.key_installed = True
        return {}

    result = setup_tools.ensure_ssh_access(
        "169.254.10.2", run=shell, http_post=post, home=tmp_path, log=lambda _m: None
    )

    assert "wired connection" in result
    keygen = next(call for call in shell.calls if call[0] == "ssh-keygen")
    assert keygen[keygen.index("-t") + 1] == "ecdsa", "the Flex rejects ed25519 keys"
    assert posted[0][0] == "/server/ssh_keys"
    assert posted[0][1] == {"key": "ecdsa-sha2-nistp521 AAAA flexctl"}
    config = (tmp_path / ".ssh" / "config").read_text()
    assert "Host 169.254.10.2" in config
    assert "User root" in config


def test_wireless_connection_falls_back_to_a_usb_drive(tmp_path: Path) -> None:
    shell = _FakeShell()
    posted: list[str] = []
    copied: list[Path] = []

    def post(_host: str, path: str, _data: dict | None) -> object:
        posted.append(path)
        if path == "/server/ssh_keys":
            raise _http_error(403, '{"error": "bad-interface"}')
        shell.key_installed = True
        return {}

    def usb_ready(public_key: Path) -> bool:
        copied.append(public_key)
        return True

    result = setup_tools.ensure_ssh_access(
        "10.0.0.2", run=shell, http_post=post, home=tmp_path, usb_ready=usb_ready, log=lambda _m: None
    )

    assert "USB drive" in result
    assert posted == ["/server/ssh_keys", "/server/ssh_keys/from_local"]
    assert copied[0].name.endswith(".pub")


def test_wireless_without_usb_explains_both_options(tmp_path: Path) -> None:
    def post(_host: str, _path: str, _data: dict | None) -> object:
        raise _http_error(403, '{"error": "bad-interface"}')

    with pytest.raises(setup_tools.SetupError, match="direct Ethernet cable"):
        setup_tools.ensure_ssh_access(
            "10.0.0.2", run=_FakeShell(), http_post=post, home=tmp_path, usb_ready=None, log=lambda _m: None
        )


def test_existing_ssh_access_changes_nothing(tmp_path: Path) -> None:
    shell = _FakeShell()
    shell.key_installed = True

    assert setup_tools.ensure_ssh_access("10.0.0.2", run=shell, home=tmp_path) == "SSH already works"
    assert not (tmp_path / ".ssh").exists()


def test_github_download_picks_the_run_built_for_this_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    wheels = tmp_path / "dist_arm"
    version = flexctl.checkout_version(_ROOT)

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "run", "list"]:
            return _completed(stdout=json.dumps([{"databaseId": 2, "headBranch": "old"}, {"databaseId": 1}]))
        if argv[:3] == ["gh", "run", "download"]:
            target = Path(argv[argv.index("-D") + 1])
            target.mkdir(parents=True)
            built = version if argv[3] == "1" else "0.0.1"
            (target / "runtime-manifest.json").write_text(json.dumps({"connectorVersion": built}))
            return _completed()
        msg = f"unexpected {argv}"
        raise AssertionError(msg)

    method = setup_tools.obtain_wheels(_ROOT, wheels, version, source="github", run=run, log=lambda _m: None)

    assert method == "downloaded from GitHub Actions"
    assert setup_tools.manifest_version(wheels) == version
    assert not (tmp_path / "dist_arm.download").exists()


def test_obtain_wheels_reports_every_failed_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_tools.shutil, "which", lambda _name: None)

    with pytest.raises(setup_tools.SetupError) as excinfo:
        setup_tools.obtain_wheels(_ROOT, tmp_path / "dist_arm", "9.9.9", log=lambda _m: None)

    assert "GitHub CLI" in str(excinfo.value)
    assert "Docker" in str(excinfo.value)


def test_clean_finds_caches_and_a_stale_bundle(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')
    (tmp_path / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "__pycache__").mkdir(parents=True)
    stale = tmp_path / "dist_arm"
    stale.mkdir()
    (stale / "old.whl").write_text("x")
    (tmp_path / "tmp").mkdir()

    paths = setup_tools.stale_paths(tmp_path, "1.0.0")

    assert set(paths) == {tmp_path / "src" / "pkg" / "__pycache__", stale, tmp_path / "tmp"}
    setup_tools.remove_paths(paths)
    assert not stale.exists()
    assert (tmp_path / ".venv" / "lib" / "__pycache__").exists()


def test_current_bundle_is_never_cleaned(tmp_path: Path) -> None:
    wheels = tmp_path / "dist_arm"
    wheels.mkdir()
    (wheels / "runtime-manifest.json").write_text(json.dumps({"connectorVersion": "1.0.0"}))

    assert setup_tools.stale_paths(tmp_path, "1.0.0") == []


def test_setup_non_interactive_runs_every_stage_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stages: list[str] = []
    monkeypatch.setattr(
        flexctl.setup_tools,
        "check_flex",
        lambda host: stages.append("check") or setup_tools.Robot("Bob", host, "OT-3 Standard"),
    )
    monkeypatch.setattr(flexctl.setup_tools, "ensure_ssh_access", lambda host, **_k: stages.append("ssh") or "ok")
    monkeypatch.setattr(flexctl.setup_tools, "obtain_wheels", lambda *_a, **_k: stages.append("wheels") or "ok")
    monkeypatch.setattr(flexctl, "command_up", lambda args, *_a: stages.append(f"up:{args.mode}:{args.operator}") or 0)
    monkeypatch.setattr(flexctl, "command_verify", lambda args, *_a: stages.append(f"verify:{args.level}") or 0)

    code = flexctl.main(
        ["setup", "--host", "10.0.0.2", "--operator", "alice", "--non-interactive", "--report-dir", str(tmp_path)],
        runner=flexctl.Runner(log=lambda _m: None),
    )

    assert code == 0
    assert stages == ["check", "ssh", "wheels", "up:pe:alice", "verify:smoke"]


def test_ssh_config_quotes_paths_with_spaces(tmp_path: Path) -> None:
    config = tmp_path / "config"
    setup_tools._add_ssh_config(config, "10.0.0.2", tmp_path / "My Keys" / "flex")
    assert f'IdentityFile "{(tmp_path / "My Keys" / "flex").as_posix()}"' in config.read_text()


def test_setup_always_confirms_the_discovered_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    robots = [setup_tools.Robot("Bob", "10.0.0.2", "OT-3 Standard")]
    monkeypatch.setattr(flexctl.setup_tools, "discover_robots", lambda: robots)
    prompts: list[str] = []

    def ask(answers: list[str]):
        return lambda prompt: prompts.append(prompt) or answers.pop(0)

    assert flexctl._choose_robot(lambda _m: None, ask(["1"])) == "10.0.0.2"
    assert "Type 1 to confirm" in prompts[0]
    assert flexctl._choose_robot(lambda _m: None, ask(["10.0.0.9"])) == "10.0.0.9"
    for bad in ("0", "2", ""):
        with pytest.raises(flexctl.FlexctlError):
            flexctl._choose_robot(lambda _m: None, ask([bad]))


def test_non_interactive_setup_requires_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flexctl.setup_tools, "discover_robots", lambda: [setup_tools.Robot("Bob", "10.0.0.2", "x")])
    code = flexctl.main(["setup", "--non-interactive"], runner=flexctl.Runner(log=lambda _m: None))
    assert code != 0


def test_usb_drive_with_other_keys_is_refused(tmp_path: Path) -> None:
    drive = tmp_path / "usb"
    drive.mkdir()
    (drive / "someone_else.pub").write_text("ssh-rsa AAAA x\n")
    key = tmp_path / "flex.pub"
    key.write_text("ecdsa AAAA\n")

    with pytest.raises(flexctl.FlexctlError, match="other public keys"):
        flexctl._usb_prompt(str(drive), lambda _p: "")(key)
    assert not (drive / "flex.pub").exists()


@pytest.mark.parametrize("speed", ["0", "-5", "100", "fast"])
def test_move_speed_must_be_slow_and_positive(speed: str) -> None:
    with pytest.raises(SystemExit):
        flexctl.build_parser().parse_args(["verify", "10.0.0.2", "--move-speed", speed])
