"""Static guard: only clock.py may read real time, and only rng.py may use random.

Uses the AST so docstrings and comments cannot trigger false positives.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "lifeline"

TIME_MODULES = {"time"}
RANDOM_MODULES = {"random", "secrets"}
OS_RANDOM_CALLS = {"urandom"}


def _python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def _attribute_calls(tree: ast.AST) -> set[tuple[str, str]]:
    """Return (object, attribute) pairs for calls like asyncio.sleep(...)."""
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                pairs.add((node.func.value.id, node.func.attr))
    return pairs


def test_package_files_exist():
    assert _python_files(), "no files found under lifeline/"


def test_only_clock_imports_time():
    offenders = []
    for path in _python_files():
        if path.name == "clock.py":
            continue
        if _imports(ast.parse(path.read_text("utf-8"))) & TIME_MODULES:
            offenders.append(str(path))
    assert offenders == []


def test_only_clock_calls_asyncio_sleep():
    offenders = []
    for path in _python_files():
        if path.name == "clock.py":
            continue
        if ("asyncio", "sleep") in _attribute_calls(ast.parse(path.read_text("utf-8"))):
            offenders.append(str(path))
    assert offenders == []


def test_only_rng_imports_random():
    offenders = []
    for path in _python_files():
        if path.name == "rng.py":
            continue
        if _imports(ast.parse(path.read_text("utf-8"))) & RANDOM_MODULES:
            offenders.append(str(path))
    assert offenders == []


def test_no_os_urandom_anywhere():
    offenders = []
    for path in _python_files():
        if any(attr in OS_RANDOM_CALLS for _, attr in _attribute_calls(ast.parse(path.read_text("utf-8")))):
            offenders.append(str(path))
    assert offenders == []