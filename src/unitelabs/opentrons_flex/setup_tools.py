"""
Workstation helpers behind ``flexctl``: robot discovery, SSH access, wheel bundles, and cleanup.

Everything here runs on the operator's computer. Remote changes still go through
the reviewed deployment scripts.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

ROBOT_HTTP_PORT = 31950
FLEX_MODEL = "OT-3 Standard"
DEFAULT_KEY_NAME = "opentrons_flex_ecdsa"
ARM_WORKFLOW = "build-flex-arm-wheels.yml"
ARM_ARTIFACT = "flex-arm-wheels"


class SetupError(RuntimeError):
    """A setup step failed; the message says what to do next."""


# ------------------------------------------------------------------ HTTP / discovery


def robot_http(
    host: str, path: str, *, data: dict | None = None, method: str | None = None, timeout: float = 6.0
) -> object:
    """GET (or POST ``data``, or ``method``) to the robot HTTP API and decode JSON."""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        f"http://{host}:{ROBOT_HTTP_PORT}{path}",
        data=body,
        headers={"Opentrons-Version": "*", "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


@dataclass(frozen=True)
class Robot:
    """A Flex found on the network."""

    name: str
    host: str
    model: str


def check_flex(host: str, *, http_get: Callable[[str, str], object] = robot_http) -> Robot:
    """Confirm ``host`` is an Opentrons Flex robot-server and return its identity."""
    try:
        health = http_get(host, "/health")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        message = (
            f"No Opentrons robot answered at http://{host}:{ROBOT_HTTP_PORT}/health ({exc}). "
            "Check the IP on the robot touchscreen (Settings > Network) and that your computer is on the same network."
        )
        raise SetupError(message) from exc
    if not isinstance(health, dict):
        message = f"{host} answered /health with unexpected data."
        raise SetupError(message)
    model = str(health.get("robot_model", ""))
    if model != FLEX_MODEL:
        message = f"{host} is a {model or 'unknown'} robot, not an Opentrons Flex ({FLEX_MODEL})."
        raise SetupError(message)
    return Robot(name=str(health.get("name", host)), host=host, model=model)


def discover_robots(timeout_s: float = 4.0) -> list[Robot]:
    """Browse mDNS for Opentrons robots on the local network and keep the Flex ones."""
    try:
        from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    except ImportError:  # pragma: no cover - zeroconf ships with the connector dependencies
        return []
    found: dict[str, str] = {}

    def on_change(zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        info = zeroconf.get_service_info(service_type, name, timeout=1500)
        if info is None or info.port != ROBOT_HTTP_PORT:
            return
        for address in info.parsed_addresses():
            if ":" not in address:
                found[address] = name.split(".")[0]

    zeroconf = Zeroconf()
    try:
        ServiceBrowser(zeroconf, "_http._tcp.local.", handlers=[on_change])
        time.sleep(timeout_s)
    finally:
        zeroconf.close()
    robots = []
    for host in sorted(found):
        try:
            robots.append(check_flex(host))
        except SetupError:
            continue
    return robots


# ------------------------------------------------------------------------- SSH


def ssh_works(host: str, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> bool:
    """Return whether key-based ``ssh root@host`` works without a password prompt."""
    completed = run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"root@{host}",
            "true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def create_ssh_key(
    home: Path | None = None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    log: Callable[[str], None] = print,
) -> Path:
    """
    Create (once) the key flexctl uses for robots and return its private-key path.

    The Flex update server accepts only ``ssh-rsa`` and ``ecdsa`` public keys, so an
    ECDSA key is generated instead of ed25519.
    """
    ssh_dir = (home or Path.home()) / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    key = ssh_dir / DEFAULT_KEY_NAME
    if not key.exists():
        log(f"Creating SSH key {key}")
        run(
            ["ssh-keygen", "-t", "ecdsa", "-b", "521", "-N", "", "-C", "flexctl", "-f", str(key)],
            check=True,
            capture_output=True,
            text=True,
        )
    return key


class LinkLocalRequired(SetupError):
    """The robot refused the key because the request did not come over the wired link-local connection."""


def ensure_ssh_access(
    host: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    http_post: Callable[[str, str, dict | None], object] | None = None,
    home: Path | None = None,
    usb_ready: Callable[[Path], bool] | None = None,
    log: Callable[[str], None] = print,
) -> str:
    """
    Make ``ssh root@host`` work and return a short description of what was done.

    1. If key-based SSH already works, nothing changes.
    2. Otherwise a key is created and sent to ``POST /server/ssh_keys``. The Flex
       accepts this only over a direct Ethernet cable (its ``169.254.x.x`` address).
    3. If the robot refuses because the connection is wireless or routed,
       ``usb_ready`` is asked to put the public key on a USB drive and plug it into
       the robot; the key is then imported with ``POST /server/ssh_keys/from_local``.
    """
    if ssh_works(host, run):
        return "SSH already works"
    home = home or Path.home()
    key = create_ssh_key(home, run=run, log=log)
    public_key = key.with_name(key.name + ".pub").read_text(encoding="utf-8").strip()
    post = http_post or (lambda robot, path, data: robot_http(robot, path, data=data if data is not None else {}))
    method = "the robot API over the wired connection"
    try:
        _post_key(post, host, "/server/ssh_keys", {"key": public_key})
    except LinkLocalRequired:
        if usb_ready is None or not usb_ready(key.with_name(key.name + ".pub")):
            message = (
                "The Flex installs SSH keys over the network only through a direct Ethernet cable. Either connect "
                "a cable and use the robot's 169.254.x.x address, or rerun with a USB drive: flexctl will copy "
                f"{key.name}.pub to it for you to plug into the robot."
            )
            raise SetupError(message) from None
        _post_key(post, host, "/server/ssh_keys/from_local", None)
        method = "a USB drive"
    _add_ssh_config(home / ".ssh" / "config", host, key)
    if not ssh_works(host, run):
        message = f"The key was installed but ssh root@{host} still fails. Try: ssh -v -i {key} root@{host}"
        raise SetupError(message)
    return f"installed {key.name}.pub through {method} and added {host} to ~/.ssh/config"


def _post_key(post: Callable[[str, str, dict | None], object], host: str, path: str, data: dict | None) -> None:
    try:
        post(host, path, data)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        if exc.code == 403 and "bad-interface" in body:
            raise LinkLocalRequired(body) from exc
        if exc.code == 404 and path.endswith("from_local"):
            message = "The robot found no .pub file on the USB drive. Copy the key to the drive's top level and retry."
            raise SetupError(message) from exc
        message = f"The robot rejected the SSH key ({exc.code}): {body}"
        raise SetupError(message) from exc
    except (OSError, urllib.error.URLError) as exc:
        message = f"Could not reach http://{host}:{ROBOT_HTTP_PORT}{path} ({exc})."
        raise SetupError(message) from exc


def _add_ssh_config(config: Path, host: str, key: Path) -> None:
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    if re.search(rf"^Host\s+{re.escape(host)}\s*$", existing, re.MULTILINE):
        return
    block = (
        f"\n# Added by flexctl for the Opentrons Flex at {host}\n"
        f"Host {host}\n"
        "    User root\n"
        f'    IdentityFile "{key.as_posix()}"\n'
        "    IdentitiesOnly yes\n"
        "    StrictHostKeyChecking accept-new\n"
    )
    with config.open("a", encoding="utf-8") as stream:
        stream.write(block)
    with contextlib.suppress(OSError):
        config.chmod(0o600)


# ---------------------------------------------------------------------- wheels


def repository_slug(repository: Path) -> str | None:
    """Return ``owner/name`` of the GitHub repository from pyproject.toml."""
    match = re.search(
        r'^repository = "https://github\.com/([^/"]+/[^/"]+?)(?:\.git)?"',
        (repository / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return match.group(1) if match else None


def manifest_version(wheels: Path) -> str | None:
    """Connector version recorded in a wheel bundle, if it is a verified bundle."""
    try:
        manifest = json.loads((wheels / "runtime-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = manifest.get("connectorVersion") if isinstance(manifest, dict) else None
    return str(version) if version else None


def obtain_wheels(
    repository: Path,
    wheels: Path,
    version: str,
    *,
    source: str = "auto",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    log: Callable[[str], None] = print,
) -> str:
    """Download the CI-built ARM wheel bundle (GitHub CLI) or build it with Docker; return the method used."""
    if manifest_version(wheels) == version:
        return "already up to date"
    attempts: list[str] = []
    if source in {"auto", "github"}:
        result = _download_from_github(repository, wheels, version, run=run, log=log)
        if result is None:
            return "downloaded from GitHub Actions"
        attempts.append(result)
    if source in {"auto", "docker"}:
        result = _build_with_docker(repository, wheels, version, run=run, log=log)
        if result is None:
            return "built with Docker"
        attempts.append(result)
    message = "Could not obtain the ARM wheel bundle:\n  - " + "\n  - ".join(attempts)
    raise SetupError(message)


def _download_from_github(
    repository: Path,
    wheels: Path,
    version: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
    log: Callable[[str], None],
) -> str | None:
    if shutil.which("gh") is None:
        return "GitHub CLI (gh) is not installed; install it and run 'gh auth login', or use Docker"
    slug = repository_slug(repository)
    if slug is None:
        return "pyproject.toml has no GitHub repository URL"
    listing = run(
        [
            "gh",
            "run",
            "list",
            "-R",
            slug,
            "--workflow",
            ARM_WORKFLOW,
            "--status",
            "success",
            "--limit",
            "20",
            "--json",
            "databaseId,headBranch",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        return f"gh could not list workflow runs ({listing.stderr.strip() or 'run gh auth login'})"
    try:
        runs = json.loads(listing.stdout or "[]")
    except json.JSONDecodeError:
        return "gh returned an unreadable run list"
    staging = wheels.with_name(wheels.name + ".download")
    for candidate in runs:
        shutil.rmtree(staging, ignore_errors=True)
        log(f"Trying wheel artifact from workflow run {candidate['databaseId']} ({candidate.get('headBranch')})")
        downloaded = run(
            ["gh", "run", "download", str(candidate["databaseId"]), "-R", slug, "-n", ARM_ARTIFACT, "-D", str(staging)],
            capture_output=True,
            text=True,
            check=False,
        )
        if downloaded.returncode == 0 and manifest_version(staging) == version:
            _replace_directory(staging, wheels)
            return None
    shutil.rmtree(staging, ignore_errors=True)
    return f"no successful {ARM_WORKFLOW} run has an artifact for connector {version}; push this version first"


def _build_with_docker(
    repository: Path,
    wheels: Path,
    version: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
    log: Callable[[str], None],
) -> str | None:
    if shutil.which("docker") is None:
        return "Docker is not installed (needed to build the ARM wheels locally)"
    staging = wheels.with_name(wheels.name + ".build")
    shutil.rmtree(staging, ignore_errors=True)
    log("Building ARM wheels with Docker buildx (first build takes several minutes)...")
    built = run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/arm64",
            "-f",
            "Dockerfile.build",
            "--target",
            "export",
            "--output",
            f"type=local,dest={staging}",
            ".",
        ],
        cwd=repository,
        check=False,
    )
    if built.returncode != 0 or manifest_version(staging) != version:
        shutil.rmtree(staging, ignore_errors=True)
        return "docker buildx failed; see the output above"
    _replace_directory(staging, wheels)
    return None


def _replace_directory(source: Path, target: Path) -> None:
    backup = target.with_name(target.name + ".old")
    shutil.rmtree(backup, ignore_errors=True)
    if target.exists():
        target.rename(backup)
    source.rename(target)
    shutil.rmtree(backup, ignore_errors=True)


# ---------------------------------------------------------------- robot-server


def robot_server_installed() -> bool:
    """Whether the Opentrons robot-server (needed for the HTTP API in the simulator) is importable."""
    import importlib.util

    return all(importlib.util.find_spec(name) is not None for name in ("robot_server", "server_utils"))


def install_robot_server(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    log: Callable[[str], None] = print,
) -> None:
    """
    Install the pinned Opentrons robot-server into this environment (same steps as CI).

    robot-server is not published on PyPI, so it is installed from the Opentrons
    repository at the exact commit the connector is validated against. Running
    ``uv sync`` again removes it; rerun this (``flexctl sim --with-http``) afterwards.
    """
    from .runtime_compat import SUPPORTED_OPENTRONS_SOURCE_COMMIT as commit

    uv = shutil.which("uv")
    if uv is None:
        message = "uv is required to install robot-server; run ./bootstrap.sh first."
        raise SetupError(message)
    base = f"git+https://github.com/Opentrons/opentrons.git@{commit}#subdirectory="
    steps = [
        ["--no-deps", f"robot_server @ {base}robot-server", f"server_utils @ {base}server-utils"],
        [
            "--no-deps",
            "--reinstall",
            f"opentrons @ {base}api",
            f"opentrons_shared_data @ {base}shared-data",
            f"opentrons_hardware @ {base}hardware",
        ],
    ]
    log("Installing the Opentrons robot-server for the simulator HTTP API (one time, needs GitHub access)...")
    for arguments in steps:
        completed = run([uv, "pip", "install", "--python", sys.executable, *arguments], check=False)
        if completed.returncode != 0:
            message = "Installing robot-server failed; check network access to github.com and retry."
            raise SetupError(message)


# ----------------------------------------------------------------------- clean


_CACHE_DIRS = ("__pycache__", ".pytest_cache", ".ruff_cache")
_SKIP_DIRS = {".git", ".venv", ".venv-ci", ".venv-introspect", "node_modules"}


def stale_paths(repository: Path, version: str, wheels_names: Iterable[str] = ("dist_arm",)) -> list[Path]:
    """Return caches and wheel bundles that do not belong to this checkout."""
    paths: list[Path] = []
    for root, directories, _files in os.walk(repository):
        directories[:] = [name for name in directories if name not in _SKIP_DIRS]
        for name in list(directories):
            if name in _CACHE_DIRS:
                paths.append(Path(root) / name)
                directories.remove(name)
    for name in wheels_names:
        wheels = repository / name
        if wheels.is_dir() and manifest_version(wheels) != version:
            paths.append(wheels)
    scratch = repository / "tmp"
    if scratch.is_dir() and not any(scratch.iterdir()):
        paths.append(scratch)
    leftovers = (repository / name for name in ("dist_arm.download", "dist_arm.build", "dist_arm.old"))
    paths.extend(path for path in leftovers if path.exists())
    return sorted(set(paths))


def remove_paths(paths: Iterable[Path]) -> None:
    """Delete the given files and directories."""
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


# ------------------------------------------------------------------- platform


def posix_shell() -> str:
    """Return a POSIX ``sh`` for the deployment scripts (Git for Windows' sh on Windows)."""
    found = shutil.which("sh")
    if found:
        return found
    if platform.system() == "Windows":
        for candidate in (
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "bin" / "sh.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "sh.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    message = "No POSIX shell found. On Windows install Git for Windows (https://git-scm.com/download/win)."
    raise SetupError(message)


def free_local_port() -> int:
    """Return an unused TCP port on localhost (for SSH tunnels)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def python_ok() -> bool:
    """Whether this interpreter matches the connector's Python requirement."""
    return sys.version_info[:2] == (3, 12)


__all__ = [
    "LinkLocalRequired",
    "Robot",
    "SetupError",
    "check_flex",
    "create_ssh_key",
    "discover_robots",
    "ensure_ssh_access",
    "free_local_port",
    "install_robot_server",
    "manifest_version",
    "obtain_wheels",
    "posix_shell",
    "remove_paths",
    "robot_http",
    "robot_server_installed",
    "ssh_works",
    "stale_paths",
]
