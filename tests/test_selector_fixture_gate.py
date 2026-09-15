"""The offline selector gate: registry vs saved DOM fixtures, no browser.

**What this proves and what it does not.** A fixture is a frozen snapshot of a
DOM shape that was once correct. Counting against it proves the selector
constants in the code still match the shape they were written for — so it
catches "someone edited a selector and broke the match", deterministically, in
CI, in milliseconds. It does **not** prove the selectors still match today's
live LinkedIn; only the live check (`--post-url` / the feed run) catches
"LinkedIn changed its DOM". Both failures are real and the two checks are not
substitutes. See ARCHITECTURE.md §8.3.

Every fixture is hand-authored synthetic markup. No live LinkedIn data.
"""

import glob
import os
import re

import pytest

from linkedin_automation import dom_probe
from linkedin_automation import selector_health as shc

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, f"{name}.html"), encoding="utf-8") as f:
        return f.read()


# ─── The three states a feed run must be able to tell apart ──────────────────

def test_a_healthy_feed_reports_healthy():
    assert shc.fixture_report(fixture("feed_healthy"), page="feed")["status"] == "HEALTHY"


def test_a_renamed_noncritical_hook_reports_degraded():
    """feed_degraded flips mainFeed -> mainFeedContainer.

    scroll_container (non-critical) dies; feed_container survives on its
    unscoped fallback. One non-critical failure and no critical one is exactly
    DEGRADED — a watchdog that only trips on total breakage warns too late.
    """
    report = shc.fixture_report(fixture("feed_degraded"), page="feed")
    assert report["status"] == "DEGRADED"
    assert report["failed"] == ["scroll_container"]


def test_a_renamed_critical_hook_reports_broken():
    """feed_broken flips role=listitem -> role=list-item.

    The shape of every real break this project has seen: a hook is renamed,
    nothing raises, and the scraper simply finds nothing.
    """
    report = shc.fixture_report(fixture("feed_broken"), page="feed")
    assert report["status"] == "BROKEN"
    assert "feed_container" in report["failed"]


def test_the_broken_fixture_only_breaks_the_one_entry():
    """A single renamed hook must not cascade — the report has to name the culprit."""
    report = shc.fixture_report(fixture("feed_broken"), page="feed")
    assert report["failed"] == ["feed_container"]


# ─── Gated entries: not checked, and saying so ───────────────────────────────

def test_a_closed_menu_reports_copy_link_as_not_checked():
    """A closed menu is not evidence the menu-item selector is stale."""
    report = shc.fixture_report(fixture("feed_healthy"), page="feed")
    entry = report["checks"]["copy_link_item"]
    assert entry["checked"] is False
    assert "overflow menu" in entry["skip_reason"]
    assert "copy_link_item" in report["not_checked"]


def test_an_open_menu_actually_checks_copy_link():
    """The same entry, on a fixture captured in the gated state, IS counted."""
    report = shc.fixture_report(fixture("feed_menu_open"), page="feed")
    entry = report["checks"]["copy_link_item"]
    assert entry["checked"] is True
    assert entry["count"] > 0
    assert report["not_checked"] == []


def test_a_closed_composer_reports_the_editor_and_submit_as_not_checked():
    report = shc.fixture_report(fixture("post_healthy"), page="post")
    assert set(report["not_checked"]) == {"comment_input", "comment_submit_button"}
    for key in ("comment_input", "comment_submit_button"):
        assert "composer" in report["checks"][key]["skip_reason"]


def test_an_open_composer_checks_the_editor_and_the_submit_button():
    report = shc.fixture_report(fixture("post_box_open"), page="post")
    assert report["not_checked"] == []
    assert report["checks"]["comment_input"]["count"] >= 1
    assert report["checks"]["comment_submit_button"]["count"] >= 1


def test_a_not_checked_entry_is_never_counted_as_a_failure():
    """Gated != broken. Reporting a gate as a failure sends repairs at the wrong target."""
    report = shc.fixture_report(fixture("post_healthy"), page="post")
    assert report["status"] == "HEALTHY"
    assert report["failed"] == []


def test_not_checked_entries_are_never_silently_absent():
    """Every registry entry for the page appears in the report, one way or another."""
    for name, page in [("feed_healthy", "feed"), ("post_healthy", "post")]:
        report = shc.fixture_report(fixture(name), page=page)
        expected = {k for k, v in shc.SELECTOR_REGISTRY.items()
                    if v.get("page", "feed") == page}
        assert set(report["checks"]) == expected


# ─── The submit-button collision, end to end ─────────────────────────────────

def test_the_submit_button_is_distinguished_from_the_button_that_opens_the_box():
    """post_box_open carries both. The XPath must find exactly the submit one."""
    count = dom_probe.make_counter(fixture("post_box_open"))
    from linkedin_automation.comment_poster import LinkedInCommentPoster as P
    assert count(P.SUBMIT_BUTTON_XPATH) == 1
    assert count("button[aria-label*='Comment']") == 1


# ─── Schema ──────────────────────────────────────────────────────────────────

def test_the_report_declares_its_schema_version_and_source():
    report = shc.fixture_report(fixture("feed_healthy"), page="feed")
    assert report["schema_version"] == shc.SCHEMA_VERSION
    assert shc.SCHEMA_VERSION >= 2
    assert report["source"] == "fixture"
    assert report["page"] == "feed"


def test_every_checked_entry_carries_the_checked_and_skip_reason_keys():
    """Consumers must not have to infer 'was this tested?' from which keys exist."""
    report = shc.fixture_report(fixture("post_box_open"), page="post")
    for entry in report["checks"].values():
        assert "checked" in entry and "skip_reason" in entry
        if entry["checked"]:
            assert entry["skip_reason"] is None
        else:
            assert entry["skip_reason"]


# ─── Determinism and integrity of the fixture set ────────────────────────────

def test_the_gate_is_deterministic():
    reports = [shc.fixture_report(fixture("feed_healthy"), page="feed")["checks"]
               for _ in range(5)]
    assert all(r == reports[0] for r in reports)


def test_an_unparseable_selector_raises_rather_than_reporting_a_clean_run():
    """A selector the engine cannot read must break the build, not count zero."""
    bogus = {"bogus": {"selectors": ["div:nth-child(2)"], "min_expected": 1,
                       "critical": True, "page": "feed"}}
    with pytest.raises(dom_probe.UnsupportedSelector):
        shc.check_fixture(fixture("feed_healthy"), page="feed", registry=bogus)


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.html"))))
def test_no_fixture_contains_personally_identifying_data(path):
    """Fixtures are committed markup. Real names and profile URLs must never be.

    Guards the sanitization: a future fixture captured from a live session by
    copy-paste would trip this rather than landing in the repo.
    """
    with open(path, encoding="utf-8") as f:
        html = f.read()

    # Real LinkedIn activity URNs are 19 digits; ours are all zeros.
    for urn in re.findall(r"urn:li:[a-zA-Z]+:(\d+)", html):
        assert set(urn) == {"0"}, f"{path}: real-looking URN {urn}"

    # Every profile/company slug must be an obvious placeholder.
    for slug in re.findall(r"/(?:in|company)/([^/\"'\s]+)", html):
        assert slug.startswith("example-"), f"{path}: non-placeholder slug {slug!r}"

    # No email addresses.
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", html), f"{path}: contains an email"


# ─── A renamed hook inside a REACHABLE state must fail, not be excused ────────
#
# The gate used to decide an entry was unreachable whenever none of its own
# selectors matched, which made these two indistinguishable:
#
#     the comment box is closed                        -> correctly "not checked"
#     the box is OPEN and the submit button was RENAMED -> ALSO "not checked"
#
# The second is a BROKEN gate reported as a shrug — the exact 2026-07-30 shape,
# excused by the flag that exists to keep reports honest. `state_witness` proves
# the state is on screen without being one of the selectors under test.

def test_a_renamed_comment_submit_button_is_not_excused_as_not_checked():
    """The one that actually broke in July, on the path that posts comments."""
    html = fixture("post_box_open").replace(
        '<button class="y7z8a9">Comment</button>',
        '<button class="y7z8a9">Reply</button>')
    report = shc.fixture_report(html, page="post")

    assert "comment_submit_button" not in report["not_checked"]
    assert report["checks"]["comment_submit_button"]["checked"] is True
    assert report["status"] == "BROKEN"
    assert report["failed"] == ["comment_submit_button"]


def test_a_renamed_comment_input_is_not_excused_either():
    """All four input selectors broken at once — the editor is genuinely gone,
    but the comment box (the witness) is still on screen."""
    html = (fixture("post_box_open")
            .replace('contenteditable="true"', 'contenteditable="yes"')
            .replace('class="ql-editor ql-blank"', 'class="editor blank"'))
    report = shc.fixture_report(html, page="post")
    assert "comment_input" not in report["not_checked"]
    assert report["checks"]["comment_input"]["checked"] is True
    assert report["status"] == "BROKEN"
    assert "comment_input" in report["failed"]


def test_the_open_menu_is_now_witnessed_rather_than_inferred():
    """copy_link_item gets the witness, but its selector cannot express a break.

    Documented rather than asserted-around, because the limitation is real:
    ``MENU_ITEM_SELECTORS`` ends in ``div[role='menu'] *``, a wildcard over the
    menu's descendants. Any open menu with any content matches it, so no rename
    of the *items* can ever drive this entry to zero. The witness makes the state
    detection honest; it cannot make a wildcard selective.

    The runtime check is a visible-text match on COPY_LINK_TEXT, which the
    registry has no way to express — so what this entry really proves is "a menu
    opened", not "the copy-link item is still there". Logged in .dev/BACKLOG.md.
    """
    html = fixture("feed_menu_open").replace('role="menuitem"', 'role="menu-item"')
    report = shc.fixture_report(html, page="feed")
    assert report["checks"]["copy_link_item"]["checked"] is True
    assert report["checks"]["copy_link_item"]["ok"] is True     # the wildcard

    count = dom_probe.make_counter(html)
    assert shc.gated_state_present(shc.SELECTOR_REGISTRY["copy_link_item"],
                                   count) is True


def test_a_closed_state_is_still_reported_as_not_checked():
    """The retrofit must not turn a legitimately-absent state into a failure."""
    post = shc.fixture_report(fixture("post_healthy"), page="post")
    assert set(post["not_checked"]) == {"comment_input", "comment_submit_button"}
    assert post["status"] == "HEALTHY"
    assert post["failed"] == []

    feed = shc.fixture_report(fixture("feed_healthy"), page="feed")
    assert feed["not_checked"] == ["copy_link_item"]
    assert feed["status"] == "HEALTHY"


def test_a_stale_witness_degrades_to_the_old_behaviour_never_to_less():
    """A witness can only ADD coverage.

    If LinkedIn renames the container too, the witness stops matching — and the
    entry must fall back to the any-selector-matched signal rather than becoming
    silently unchecked, which would be worse than before the retrofit.
    """
    spec = dict(shc.SELECTOR_REGISTRY["comment_submit_button"])
    spec["state_witness"] = "div[aria-label='NoSuchContainerAnywhere']"
    count = dom_probe.make_counter(fixture("post_box_open"))
    assert shc.gated_state_present(spec, count) is True     # selectors still match

    closed = dom_probe.make_counter(fixture("post_healthy"))
    assert shc.gated_state_present(spec, closed) is False   # genuinely absent


# ─── The same rule, applied to SOURCE - not just fixtures ────────────────────
#
# The fixture check above guards committed markup, and it worked. What it does
# not cover is a real identifier pasted into a TEST or a TOOL, which is exactly
# how four real LinkedIn URNs and a live profile slug reached the tree during
# the scheduled-posting build: a permalink copied out of a live run to use as
# test data, and a dev account's slug used in a --help example. This repo is
# public, so the rule is the repo's, not the fixture directory's.

_SOURCE_DIRS = ("tests", "tools", "linkedin_automation")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source_files():
    out = []
    for d in _SOURCE_DIRS:
        for root, _dirs, files in os.walk(os.path.join(_REPO_ROOT, d)):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith((".py", ".md")):
                    out.append(os.path.join(root, name))
    return sorted(out)


def test_no_source_file_carries_a_real_linkedin_urn():
    """A real URN points at a real post by a real person.

    Test data must be fabricated. All-zero ids are the convention the fixture
    README already sets; this extends it to the code that reads them.
    """
    offenders = []
    for path in _source_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for urn in re.findall(r"urn:li:[a-zA-Z_]+:(\d{6,})", text):
            if set(urn) - {"0", "1"}:
                offenders.append("%s: %s" % (os.path.relpath(path, _REPO_ROOT), urn))
    assert not offenders, (
        "real-looking LinkedIn URNs in committed source:\n  "
        + "\n  ".join(offenders))


def test_no_source_file_names_a_real_profile_slug():
    """A LinkedIn vanity slug identifies a real account.

    Narrower than the fixture rule on purpose. Requiring an `example-` prefix
    across all source would flag invented names like `jamie-lee`, which are
    perfectly good test data. What actually identifies someone is LinkedIn's
    auto-generated form - a name followed by a long numeric suffix, as in
    `example-person-011011`. That is the shape a copied-from-a-live-session
    slug
    takes, and the shape this catches.
    """
    offenders = []
    for path in _source_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        # Written as a URL...
        for slug in re.findall(r"linkedin\.com/in/([A-Za-z0-9\-]*-\d{6,})", text):
            offenders.append("%s: %s" % (os.path.relpath(path, _REPO_ROOT), slug))
        # ...and written bare, in prose. Three real slugs reached committed
        # source as documentation examples precisely because they were not
        # inside a URL, and the URL-shaped check above could not see them.
        for slug in re.findall(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+-(\d{6,}))\b",
                               text):
            whole, digits = slug
            # Digits made only of 0s and 1s are unmistakably invented, which is
            # the same exemption the URN rule uses. Anything else is a real
            # account until someone says otherwise.
            if set(digits) - {"0", "1"}:
                offenders.append("%s: %s" % (os.path.relpath(path, _REPO_ROOT),
                                             whole))
    assert not offenders, (
        "real-looking profile slugs in committed source:\n  "
        + "\n  ".join(offenders))
