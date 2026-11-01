"""Guard against the exact failure the dispatch-39/40 merge produced: two
module-level functions named identically in one test file. Python keeps only
the later definition - the earlier one is silently shadowed and pytest never
runs it, with no error, no skip, nothing in the suite's own pass/fail count
to say coverage is thinner than it looks.

ruff's F811 already catches this, but a stale ``.ruff_cache`` let one
through once (dispatch-39's and dispatch-40's own
``test_toggle_connector_on_never_touches_auto_mode`` both landed in
``test_scheduler.py`` with no textual overlap, so the merge had nothing to
flag). This is a pytest-level backstop with no cache of its own: it parses
every test file's AST fresh on every run, so it cannot go stale the way a
lint cache can.
"""

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent


def _test_files():
    return sorted(TESTS_DIR.rglob("test_*.py"))


def _duplicate_test_function_names(path: Path) -> list[str]:
    """Module-level ``test_*`` names defined more than once in ``path``.

    Only ``tree.body`` (module scope) is walked - the same shadowing happens
    one level down inside a ``class Test...:``, but this repo has no test
    classes today (every test here is a flat module-level function), so that
    case is out of scope rather than silently unchecked-and-assumed-fine.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = [
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    seen = set()
    dupes = []
    for name in names:
        if name in seen and name not in dupes:
            dupes.append(name)
        seen.add(name)
    return dupes


@pytest.mark.parametrize("path", _test_files(), ids=lambda p: p.name)
def test_no_duplicate_test_function_names_in_file(path):
    dupes = _duplicate_test_function_names(path)
    assert not dupes, (
        f"{path.name} defines the same test function name more than once: "
        f"{dupes}. The earlier definition is silently shadowed and never "
        f"runs - rename one or remove the duplicate.")
