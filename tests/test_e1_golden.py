"""E1 (D-027): the adapter refactor changed no LinkedIn decision.

``tests/fixtures/e1_golden/expected.json`` was written by
``tests/e1_golden_harness.py`` from the code BEFORE the adapter boundary
existed (``main`` @ ``e8d58b7``). This test re-runs the same harness on the
current code and requires the result to be identical, line for line: the
poster's decisions, the scheduler's and the dashboard's selections and
handoffs, the composed LinkedIn and X prompts, the selector registry.

*If you are here because this failed:* regenerating the golden defeats it.
Either the change was unintended and belongs reverted, or a LinkedIn decision
is meant to change, in which case say so in the commit and re-capture
deliberately with ``E1_GOLDEN_CAPTURE=<path>``.
"""

import difflib
import os

import pytest

from e1_golden_harness import capture, render

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "e1_golden", "expected.json")


def test_linkedin_decisions_match_the_pre_refactor_golden(monkeypatch, tmp_path,
                                                         api_client):
    actual = render(capture(monkeypatch, tmp_path, api_client))
    target = os.environ.get("E1_GOLDEN_CAPTURE")
    if target:
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(actual)
        pytest.skip("golden captured to %s" % target)
    with open(GOLDEN, encoding="utf-8") as f:
        expected = f.read()
    if actual != expected:
        diff = list(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                         "golden", "now", lineterm="", n=2))
        pytest.fail("LinkedIn behaviour differs from the golden:\n"
                    + "\n".join(diff[:80]))
