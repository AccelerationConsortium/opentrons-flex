from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "unitelabs" / "opentrons_flex"
_ALLOWED_ROBOT_SERVER_IMPORTS = {
    _PACKAGE_ROOT / "robot_server_compat.py",
}


def test_robot_server_imports_remain_inside_the_compatibility_adapter() -> None:
    violations: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "robot_server" or name.startswith("robot_server.") for name in names) and (
                path not in _ALLOWED_ROBOT_SERVER_IMPORTS
            ):
                violations.append(f"{path.relative_to(_PACKAGE_ROOT)}:{node.lineno}")

    assert violations == [], f"robot_server imports escaped the compatibility adapter: {violations}"
