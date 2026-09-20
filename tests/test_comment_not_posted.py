"""The silent-success regression: a comment that was typed but never posted.

In production the tool typed a comment, failed to submit it, and recorded the
post as done. It could do that because `verify_comment_posted` treated two
non-proofs as proof:

  * any change to the compose box's text - which a FAILED submit also produces,
    since a failed submit can clear the box; and
  * a StaleElementReferenceException, logged as "likely posted". A stale handle
    means the DOM moved. It says nothing about whether anything was sent.

The consequence was permanent, not cosmetic. `posting_progress.json` is the
authoritative ledger of what published, and `post_store.sync_with_progress`
reconciles COMMENTED from it - so an unposted comment was marked done forever
and never retried.

These tests drive a poster whose submit is a genuine no-op.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import comment_poster as cpm  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402


class FakeElement:
    """A compose box or a rendered comment."""

    def __init__(self, text="", stale=False):
        self._text = text
        self._stale = stale
        self.sent = []

    @property
    def text(self):
        if self._stale:
            raise RuntimeError("stale element reference")
        return self._text

    def clear(self):
        self._text = ""

    def send_keys(self, *a):
        self.sent.append(a)

    def get_attribute(self, name):
        return None

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    @property
    def size(self):
        return {"width": 10, "height": 10}

    @property
    def tag_name(self):
        return "button"


class NoOpSubmitDriver:
    """A page whose submit button exists, is clickable, and does NOTHING.

    The exact production shape: the click lands, no exception is raised, and no
    comment appears in the thread.
    """

    def __init__(self, thread=(), clears_box=False, box=None):
        self.thread = list(thread)
        self.clears_box = clears_box
        self.box = box
        self.current_url = "https://www.linkedin.com/feed/update/urn:li:activity:0000000000000000001/"
        self.title = "A post"
        self.clicked = []

    def find_elements(self, by, selector):
        if selector == cpm.LinkedInCommentPoster.POSTED_COMMENT_SELECTOR:
            return [FakeElement(t) for t in self.thread]
        return [FakeElement("Comment")]

    def find_element(self, by, selector):
        return FakeElement("Comment")

    def execute_script(self, script, *a):
        # The JS submit path reports a click it did not really make.
        if "querySelectorAll" in script:
            self._submit()
            return {"clicked": True, "button": "Comment"}
        return None

    def _submit(self):
        self.clicked.append(True)
        if self.clears_box and self.box is not None:
            self.box._text = ""      # the trap: cleared, but nothing posted

    def save_screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"png")
        return True

    @property
    def page_source(self):
        return "<html></html>"


@pytest.fixture
def poster(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "default")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file", lambda n: str(tmp_path / "progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n: str(tmp_path))
    monkeypatch.setattr(pm, "get_profile_config", lambda n=None: {"behavior": {}})
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(
                            tmp_path / (subdir or "")))
    (tmp_path / "failures").mkdir(exist_ok=True)

    # No real waiting, no real mouse.
    monkeypatch.setattr(cpm.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cpm.hb, "human_click", lambda d, e: getattr(d, "_submit", lambda: None)())
    monkeypatch.setattr(cpm.hb, "scroll_to_element", lambda d, e: None)
    monkeypatch.setattr(cpm.hb, "type_like_human",
                        lambda d, el, text: setattr(el, "_text", text))
    monkeypatch.setattr(cpm.hb, "simulate_reading_for_text", lambda d, t: None)
    # The run loop waits 22-48s between posts and takes periodic breaks. Real
    # sleeps here would make this file the slowest in the suite for no signal.
    monkeypatch.setattr(cpm.hb, "random_delay", lambda *a, **k: 0)
    monkeypatch.setattr(cpm.hb, "random_break_threshold", lambda *a, **k: 10**6)
    monkeypatch.setattr(cpm.hb, "take_break", lambda *a, **k: None)
    monkeypatch.setattr(cpm.time, "sleep", lambda s: None)

    return cpm.LinkedInCommentPoster(profile_name="default")


# ─── the two false proofs ────────────────────────────────────────────────────

def test_a_cleared_box_alone_is_not_proof_the_comment_posted(poster):
    """THE PRODUCTION BUG. A failed submit can clear the box too."""
    box = FakeElement("my comment text")
    poster.driver = NoOpSubmitDriver(thread=[], clears_box=True, box=box)
    before = (0, [])
    box._text = ""                      # submit "worked": box is empty
    assert poster.verify_comment_posted(box, "my comment text", before) is False


def test_a_stale_box_is_not_proof_the_comment_posted(poster):
    """It used to log "element changed (likely posted)" and return True.

    A stale handle means the DOM moved. Nothing about that says a comment was
    sent - and resolving the unknown in favour of success is what lost them.
    """
    box = FakeElement("my comment text", stale=True)
    poster.driver = NoOpSubmitDriver(thread=[])
    assert poster.verify_comment_posted(box, "my comment text", (0, [])) is False


def test_the_comment_appearing_in_the_thread_IS_proof(poster):
    box = FakeElement("")
    poster.driver = NoOpSubmitDriver(thread=["Someone: my comment text"])
    assert poster.verify_comment_posted(box, "my comment text", (0, [])) is True


def test_a_grown_thread_plus_an_empty_box_is_proof(poster):
    """The weaker positive: the rendered text may be truncated or reformatted,
    so a count that grew alongside a cleared box still counts."""
    box = FakeElement("")
    poster.driver = NoOpSubmitDriver(thread=["a", "b"])
    assert poster.verify_comment_posted(box, "unmatchable text", (1, ["a"])) is True


def test_a_grown_thread_with_text_still_in_the_box_is_NOT_proof(poster):
    """Someone else commenting while we failed must not read as our success."""
    box = FakeElement("my comment text")
    poster.driver = NoOpSubmitDriver(thread=["a", "b"])
    assert poster.verify_comment_posted(box, "my comment text", (1, ["a"])) is False


# ─── end to end: the no-op submit ────────────────────────────────────────────

def test_a_noop_submit_does_not_reach_the_posted_ledger(poster, tmp_path):
    """The whole point. `posted_comments` is what COMMENTED is reconciled from,
    so anything unverified landing there marks the post done forever."""
    box = FakeElement("")
    poster.driver = NoOpSubmitDriver(thread=[], clears_box=True, box=box)
    poster.open_comment_box = lambda: box
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True

    ok = poster.post_single_comment({"url": "https://www.linkedin.com/x/1",
                                     "comment": "a comment that never posts",
                                     "preview": "p"})

    assert ok is False
    assert poster.progress.get("posted_comments", []) == []


def test_a_noop_submit_records_a_distinct_failure_reason(poster):
    box = FakeElement("")
    poster.driver = NoOpSubmitDriver(thread=[], clears_box=True, box=box)
    poster.open_comment_box = lambda: box
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True

    poster.post_single_comment({"url": "https://www.linkedin.com/x/1",
                                "comment": "a comment that never posts",
                                "preview": "p"})

    failures = poster.progress.get("failed_comments", [])
    assert len(failures) == 1
    assert failures[0]["reason"] == "comment_not_posted"
    assert failures[0]["url"] == "https://www.linkedin.com/x/1"


def test_the_failure_is_persisted_so_it_survives_the_run(poster, tmp_path):
    box = FakeElement("")
    poster.driver = NoOpSubmitDriver(thread=[], clears_box=True, box=box)
    poster.open_comment_box = lambda: box
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True
    poster.post_single_comment({"url": "https://www.linkedin.com/x/1",
                                "comment": "a comment that never posts",
                                "preview": "p"})

    saved = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert saved["posted_comments"] == []
    assert saved["failed_comments"][0]["reason"] == "comment_not_posted"


def test_the_submit_dom_is_captured_for_the_next_fix(poster, tmp_path):
    """The evidence this run exists to gather: which submit controls were on
    the page, their aria-labels and whether they were disabled."""
    box = FakeElement("")
    poster.driver = NoOpSubmitDriver(thread=[], clears_box=True, box=box)
    poster.open_comment_box = lambda: box
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True
    poster.post_single_comment({"url": "https://www.linkedin.com/x/1",
                                "comment": "a comment that never posts",
                                "preview": "p"})

    captures = [f for f in os.listdir(tmp_path / "failures")
                if f.endswith("_submitdom.json")]
    assert captures, "no submit-DOM evidence was written"
    data = json.loads((tmp_path / "failures" / captures[0]).read_text(encoding="utf-8"))
    assert data["submit_candidates"], "no submit controls were recorded"
    probe = data["submit_candidates"][0]
    assert "aria_label" in probe and "disabled" in probe


def test_the_captured_evidence_is_pii_scrubbed(poster, tmp_path):
    """`data/` is gitignored, but a capture gets pasted into issues and chats."""
    box = FakeElement("")
    driver = NoOpSubmitDriver(thread=[], clears_box=True, box=box)
    # No digit run in the slug: the repo's own fixture gate forbids a
    # real-SHAPED linkedin.com/in/<slug>-<digits> anywhere in committed source,
    # with no all-zeros exemption for the URL form. The /in/ rule is what is
    # under test here anyway.
    driver.current_url = "https://www.linkedin.com/in/some-real-person/recent-activity/"
    poster.driver = driver
    poster.open_comment_box = lambda: box
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True
    poster.post_single_comment({"url": "https://www.linkedin.com/x/1",
                                "comment": "a comment that never posts",
                                "preview": "p"})

    captures = [f for f in os.listdir(tmp_path / "failures")
                if f.endswith("_submitdom.json")]
    raw = (tmp_path / "failures" / captures[0]).read_text(encoding="utf-8")
    assert "some-real-person" not in raw
    assert "redacted-slug" in raw


# ─── the run must be impossible to mistake for success ───────────────────────

def _run_with(poster, tmp_path, comments, outcome):
    path = tmp_path / "comments.txt"
    blocks = []
    for c in comments:
        blocks.append("POST: %s\nURL: %s\nCOMMENT: %s\n" % (c, c, c))
    path.write_text("\n" + ("\n" + "=" * 20 + "\n").join(blocks), encoding="utf-8")
    poster.parse_comments_file = staticmethod(
        lambda p: [{"url": "https://www.linkedin.com/x/%d" % i,
                    "comment": "c%d" % i, "preview": "p"}
                   for i in range(len(comments))])
    poster.setup_driver = lambda: None
    poster.login = lambda: True
    poster.driver = None
    poster.post_single_comment = outcome
    return poster.run(str(path), post_count=10)


def test_a_run_that_posted_nothing_reports_failures_not_success(poster, tmp_path,
                                                                caplog):
    with caplog.at_level("ERROR"):
        result = _run_with(poster, tmp_path, ["a", "b", "c"],
                           lambda c, *a, **k: False)

    assert result["posted"] == 0
    assert result["failed"] == 3
    assert result["skipped"] == 0
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "3 of 3 comments FAILED to post" in messages
    assert "NOTHING WAS POSTED THIS RUN" in messages
    assert "failures/" in messages


def test_one_failure_does_not_abort_the_batch(poster, tmp_path):
    """Resilient AND loud: the other comments still go out."""
    def outcome(c, *a, **k):
        return c["url"] != "https://www.linkedin.com/x/1"

    result = _run_with(poster, tmp_path, ["a", "b", "c"], outcome)
    assert result["posted"] == 2
    assert result["failed"] == 1


def test_a_fully_successful_run_still_reads_as_success(poster, tmp_path, caplog):
    with caplog.at_level("INFO"):
        result = _run_with(poster, tmp_path, ["a", "b"], lambda c, *a, **k: True)
    assert result["failed"] == 0
    assert result["posted"] == 2
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "FAILED" not in messages
