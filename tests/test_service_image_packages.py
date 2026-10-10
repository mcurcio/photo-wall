"""A service image ships every first-party package its shipped packages import."""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
FIRST_PARTY = {p.name for p in REPOSITORY.iterdir() if (p / "__init__.py").is_file()}


def _shipped() -> set[str]:
    """Packages the Dockerfile's `source` stage copies into /app."""
    return set(re.findall(r"^COPY --link (\w+) /app/\1$", (REPOSITORY / "Dockerfile").read_text(), re.M))


def _imports(package: str) -> set[str]:
    found: set[str] = set()
    for path in (REPOSITORY / package).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def test_every_first_party_package_a_shipped_package_imports_is_shipped():
    shipped = _shipped()
    assert shipped, "the Dockerfile source stage lists no package"
    missing = {
        (package, needed)
        for package in shipped
        for needed in (_imports(package) & FIRST_PARTY) - shipped - {package}
    }
    assert not missing, f"imported by a shipped package but not copied into the image: {sorted(missing)}"
