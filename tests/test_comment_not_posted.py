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
    def _click(driver, element):
        """Click the ELEMENT, the way human_click does.

        Falls back to the driver's own submit hook for the Dispatch-1 drivers,
        whose "button" is a throwaway stand-in.
        """
        clicker = getattr(element, "click", None)
        if callable(clicker):
            clicker()
        else:
            getattr(driver, "_submit", lambda: None)()

    monkeypatch.setattr(cpm.hb, "human_click", _click)
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


# ─── Dispatch 2: submit only an ENABLED button ───────────────────────────────
#
# The click used to go to SUBMIT_BUTTON_FALLBACK_SELECTORS[0] with no enabled
# check at all. LinkedIn keeps that button disabled until its editor registers
# the typed text, and a click on a disabled button raises nothing and does
# nothing - the reported symptom exactly.


class FakeButton:
    """A submit control that can start disabled and later enable."""

    def __init__(self, text="Comment", enabled=False, aria_label=None,
                 aria_disabled=None, classes="", on_click=None):
        self.text = text
        self._enabled = enabled
        self._aria_label = aria_label
        self._aria_disabled = aria_disabled
        self._classes = classes
        self.on_click = on_click
        self.clicks = 0

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return {"aria-label": self._aria_label,
                "aria-disabled": self._aria_disabled,
                "class": self._classes,
                "disabled": None if self._enabled else "true"}.get(name)

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()


class SubmitDriver:
    """A page whose submit button enables only once the input event fires."""

    def __init__(self, button, box=None, thread=None,
                 enable_on_input=True):
        self.button = button
        self.box = box
        self.thread = thread if thread is not None else []
        self.enable_on_input = enable_on_input
        self.current_url = "https://www.linkedin.com/feed/update/x/"
        self.title = "A post"
        self.scripts = []

    def find_elements(self, by, selector):
        if selector == cpm.LinkedInCommentPoster.POSTED_COMMENT_SELECTOR:
            return [FakeElement(t) for t in self.thread]
        if selector == "button":
            return [self.button]
        return [self.button]

    def find_element(self, by, selector):
        return self.button

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if "dispatchEvent" in script and self.enable_on_input:
            self.button._enabled = True     # React finally saw the text
        return None

    def save_screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"png")
        return True

    def quit(self):
        """run() closes the browser in its finally block."""
        return None

    @property
    def page_source(self):
        return "<html></html>"


def _wire(poster, driver, box):
    poster.driver = driver
    poster.open_comment_box = lambda: box
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True
    poster.SUBMIT_ENABLE_TIMEOUT = 0.3
    poster.SUBMIT_ENABLE_POLL = 0.01


def test_a_disabled_button_that_enables_after_the_input_event_gets_clicked(poster):
    """The dominant failure mode, fixed: wait for enabled, then click."""
    box = FakeElement("")
    button = FakeButton(enabled=False)
    driver = SubmitDriver(button, box=box, enable_on_input=True)

    def on_click():
        driver.thread.append("me: a comment that posts")

    button.on_click = on_click
    _wire(poster, driver, box)

    assert poster.post_comment("a comment that posts") is True
    assert button.clicks == 1
    assert any("dispatchEvent" in s for s in driver.scripts)


def test_a_button_that_never_enables_is_a_loud_captured_failure(poster, tmp_path):
    """Not a skip, not COMMENTED, and the evidence says WHY."""
    box = FakeElement("")
    button = FakeButton(enabled=False)
    driver = SubmitDriver(button, box=box, enable_on_input=False)
    _wire(poster, driver, box)

    assert poster.post_comment("a comment that never posts") is False
    assert button.clicks == 0, "a disabled button must never be clicked"

    captures = [f for f in os.listdir(tmp_path / "failures")
                if f.endswith("_submitdom.json")]
    assert captures
    data = json.loads((tmp_path / "failures" / captures[0]).read_text(encoding="utf-8"))
    assert data["reason"] == "submit_button_never_enabled"


def test_the_ctrl_enter_fallback_rescues_a_click_that_did_nothing(poster):
    """An enabled button whose click is a no-op - then the keyboard works."""
    box = FakeElement("")
    button = FakeButton(enabled=True)          # enabled, but click does nothing
    driver = SubmitDriver(button, box=box, enable_on_input=False)
    _wire(poster, driver, box)

    def on_ctrl_enter(*a):
        driver.thread.append("me: rescued by the keyboard")

    box.send_keys = on_ctrl_enter
    assert poster.post_comment("rescued by the keyboard") is True
    assert button.clicks == 1                  # the click was tried first


def test_the_action_bar_comment_button_is_never_mistaken_for_submit(poster):
    """The button that OPENS the box also reads "Comment" and is ALWAYS
    enabled. Accepting it would click the wrong control forever."""
    opener = FakeButton(text="Comment", enabled=True, aria_label="Comment")
    driver = SubmitDriver(opener, box=FakeElement(""), enable_on_input=False)
    poster.driver = driver
    poster.SUBMIT_ENABLE_TIMEOUT = 0.05
    poster.SUBMIT_ENABLE_POLL = 0.01
    assert poster.await_enabled_submit() is None


def test_aria_disabled_counts_as_disabled(poster):
    """Selenium's is_enabled() only reads the `disabled` property. A button
    carrying aria-disabled="true" reads as ENABLED to it and still does nothing
    when clicked - which is the no-op this dispatch is about."""
    button = FakeButton(enabled=True, aria_disabled="true")
    assert poster.submit_button_is_enabled(button) is False


def test_the_artdeco_disabled_class_counts_as_disabled(poster):
    button = FakeButton(enabled=True,
                        classes="artdeco-button artdeco-button--disabled")
    assert poster.submit_button_is_enabled(button) is False


def test_a_never_enabling_button_does_not_abort_the_batch(poster, tmp_path):
    """Resilient AND loud, through the whole run."""
    def outcome(comment, *a, **k):
        box = FakeElement("")
        button = FakeButton(enabled=False)
        driver = SubmitDriver(button, box=box, enable_on_input=False)
        _wire(poster, driver, box)
        # The REAL method, called unbound: _run_with has replaced the bound
        # attribute with this very function, so `poster.post_single_comment`
        # here would recurse into itself.
        return cpm.LinkedInCommentPoster.post_single_comment(poster, comment)

    result = _run_with(poster, tmp_path, ["a", "b"], outcome)
    assert result["posted"] == 0
    assert result["failed"] == 2
    assert poster.progress.get("posted_comments", []) == []
    reasons = [f["reason"] for f in poster.progress.get("failed_comments", [])]
    assert reasons == ["comment_not_posted", "comment_not_posted"]
