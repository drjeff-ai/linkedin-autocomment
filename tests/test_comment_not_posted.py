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
    def _type(driver, el, text):
        """Typing lands the text AND lets the page react, as it does live."""
        el._text = text
        hook = getattr(driver, "on_text_entered", None)
        if hook:
            hook(text)

    monkeypatch.setattr(cpm.hb, "type_like_human", _type)
    monkeypatch.setattr(cpm.hb, "simulate_reading_for_text", lambda d, t: None)
    # The run loop waits 22-48s between posts and takes periodic breaks. Real
    # sleeps here would make this file the slowest in the suite for no signal.
    monkeypatch.setattr(cpm.hb, "random_delay", lambda *a, **k: 0)
    monkeypatch.setattr(cpm.hb, "random_break_threshold", lambda *a, **k: 10**6)
    monkeypatch.setattr(cpm.hb, "take_break", lambda *a, **k: None)
    monkeypatch.setattr(cpm.time, "sleep", lambda s: None)

    poster = cpm.LinkedInCommentPoster(profile_name="default")
    # The submit wait polls for 8s in production. Left at its real value every
    # test that reaches a disabled button would spend it, for no signal.
    poster.SUBMIT_ENABLE_TIMEOUT = 0.2
    poster.SUBMIT_ENABLE_POLL = 0.01
    poster.VERIFY_TIMEOUT = 0.2
    poster.VERIFY_POLL = 0.01
    return poster


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
                 enable_on_input=True, accepts_input=None, has_cdp=True):
        self.button = button
        self.box = box if box is not None else FakeElement("")
        self.thread = thread if thread is not None else []
        # Whether the editor ACCEPTS inserted text. False models the 15:00
        # capture: the insert does nothing, the document stays empty, and
        # LinkedIn never enables the submit.
        self.accepts_input = (enable_on_input if accepts_input is None
                              else accepts_input)
        self.cdp_calls = []
        if not has_cdp:
            del self.__class__.execute_cdp_cmd
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

    def on_text_entered(self, text):
        """LinkedIn enables the submit once its editor has the text."""
        if self.accepts_input:
            self.button._enabled = True

    def execute_cdp_cmd(self, cmd, params):
        """Chrome's input pipeline. The real one fires beforeinput/input."""
        self.cdp_calls.append((cmd, params))
        if cmd == "Input.insertText" and self.accepts_input:
            self.box._text = params["text"]
            self.button._enabled = True      # the editor registered the text
        return {}

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if "parentElement" in script:
            # The composer walk. This driver models a page where the button IS
            # inside the composer, so the walk finds it - enabled or not.
            return self.button
        if "selectAll" in script:
            self.box._text = ""
            self.button._enabled = False
            return None
        if "insertText" in script and self.accepts_input:
            self.box._text = args[1]
            self.button._enabled = True
            return None
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


#: Kept so a test can delete execute_cdp_cmd off the class and restore it.
_SUBMIT_DRIVER_CDP = SubmitDriver.execute_cdp_cmd


def _wire(poster, driver, box):
    poster.driver = driver
    poster.open_comment_box = lambda: box
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True
    poster.SUBMIT_ENABLE_TIMEOUT = 0.3
    poster.SUBMIT_ENABLE_POLL = 0.01
    poster.VERIFY_TIMEOUT = 0.3
    poster.VERIFY_POLL = 0.01


def test_typing_lands_the_text_and_enables_the_submit(poster):
    """The DEFAULT path: per-character typing, exactly as before.

    The 10:33 capture showed send_keys putting all 177 characters into the
    editor, so the cadence stays. The submit ENABLING is the proof it landed.
    """
    box = FakeElement("")
    button = FakeButton(enabled=False)
    driver = SubmitDriver(button, box=box, accepts_input=True)

    def on_click():
        driver.thread.append("me: a comment that posts")

    button.on_click = on_click
    _wire(poster, driver, box)

    assert poster.post_comment("a comment that posts") is True
    assert button.clicks == 1
    assert box._text == "a comment that posts"
    assert driver.cdp_calls == [], "the default path must not use CDP"


def test_the_insert_fallbacks_are_off_by_default(poster):
    """They fixed nothing and cost the cadence, so they stay switched off."""
    assert poster.HUMAN_TYPING is True
    assert poster.INSERT_FALLBACKS is False


def test_the_insert_fallbacks_run_AFTER_typing_when_enabled(poster):
    """Switched on, they are an addition to typing and never a replacement."""
    box = FakeElement("")
    button = FakeButton(enabled=False)
    # The editor ignores typed text, so typing cannot enable the submit...
    driver = SubmitDriver(button, box=box, accepts_input=False)
    _wire(poster, driver, box)
    poster.INSERT_FALLBACKS = True

    def cdp(cmd, params):
        driver.cdp_calls.append((cmd, params))
        box._text = params["text"]
        button._enabled = True              # ...but the CDP insert does
        return {}

    driver.execute_cdp_cmd = cdp
    driver.clear_hook = True

    def on_click():
        driver.thread.append("me: landed via the fallback")

    button.on_click = on_click
    assert poster.post_comment("landed via the fallback") is True
    assert driver.cdp_calls, "the fallback should have been reached"


def test_text_that_never_registers_is_a_loud_captured_failure(poster, tmp_path):
    """The 15:00 capture's shape: the insert does nothing, the document stays
    empty, LinkedIn never enables the submit.

    Nothing is clicked and nothing is posted, and the reason names the actual
    problem - the TEXT, not the button.
    """
    box = FakeElement("")
    button = FakeButton(enabled=False)
    driver = SubmitDriver(button, box=box, accepts_input=False)
    _wire(poster, driver, box)

    assert poster.post_comment("a comment that never lands") is False
    assert button.clicks == 0, "a disabled button must never be clicked"

    captures = [f for f in os.listdir(tmp_path / "failures")
                if f.endswith("_submitdom.json")]
    assert captures
    data = json.loads((tmp_path / "failures" / captures[0]).read_text(encoding="utf-8"))
    assert data["reason"] == "text_did_not_register"


def test_a_second_insert_is_refused_when_the_first_left_text_behind(poster):
    """The double-post guard.

    If an insert left text in the editor without enabling the submit, running
    another insert would post the comment twice - and a doubled comment cannot
    be unposted. It stops instead.
    """
    box = FakeElement("")
    button = FakeButton(enabled=False)
    driver = SubmitDriver(button, box=box, accepts_input=False)
    _wire(poster, driver, box)

    # The insert lands text but the submit stays disabled, and clearing fails.
    def stubborn_cdp(cmd, params):
        box._text = params["text"]
        return {}

    driver.execute_cdp_cmd = stubborn_cdp
    driver.execute_script = lambda script, *a: (
        driver.button if "parentElement" in script else None)

    assert poster.post_comment("a comment that sticks") is False
    assert button.clicks == 0
    assert driver.cdp_calls == [], "it must not have tried a second insert"


def test_the_keyboard_rescues_a_click_that_did_nothing(poster):
    """Text landed, the submit enabled, the click was a no-op.

    Only here does the keyboard fallback apply - AFTER the polling window, so
    a slow render cannot turn into a second comment.
    """
    box = FakeElement("")
    button = FakeButton(enabled=True)
    driver = SubmitDriver(button, box=box, accepts_input=True)
    _wire(poster, driver, box)

    def on_ctrl_enter(*a):
        driver.thread.append("me: rescued by the keyboard")

    box.send_keys = on_ctrl_enter
    assert poster.post_comment("rescued by the keyboard") is True
    assert button.clicks == 1          # the click was tried first


def test_there_is_no_page_wide_submit_path_at_all(poster):
    """The page-wide fallback WAS the bug, in its final form.

    With the composer submit disabled and the action-bar button enabled, any
    "pick the one enabled button on the page" rule resolves to the action bar.
    So no such rule may exist - asserted on the object, because a helper added
    back later would silently reintroduce it.
    """
    assert not hasattr(poster, "unambiguous_page_submit")
    assert not hasattr(poster, "await_enabled_submit")


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


# ─── Dispatch 3: TWO buttons read "Comment" ──────────────────────────────────
#
# Proved by failure_comment_not_posted_20260920_103314: a post page carries the
# action-bar "Comment" (focuses the box) AND the composer's submit "Comment"
# (posts it). Neither has an aria-label and BOTH are enabled, so no attribute
# test separates them - //button[normalize-space(.)='Comment'] matched both and
# took the first in document order, which is the wrong one.
#
# The capture also shows what DOES separate them: from the editor, the
# composer's submit shares an ancestor 6 levels up, the action-bar's not until
# 11 - and level 11 is the post card, role="listitem".

TWO_COMMENT_BUTTONS = """
<div role="listitem">                        <!-- the post card -->
  <div class="social-actions">
    <button id="action-bar">Comment</button>  <!-- only focuses the box -->
  </div>
  <div class="hashed-a">
    <div class="hashed-b">                    <!-- the composer container -->
      <div data-testid="ui-core-tiptap-text-editor-wrapper">
        <div role="textbox" contenteditable="true"
             aria-label="Text editor for creating comment"
             class="tiptap ProseMirror">typed text</div>
      </div>
      <div class="composer-footer">
        <button id="composer-submit">Comment</button>   <!-- posts it -->
      </div>
    </div>
  </div>
</div>
"""


class DomDriver:
    """Runs the real walk JS against a parsed fixture, in Python.

    dom_probe gives a real tree, so the walk's LOGIC is exercised - nearest
    enclosing ancestor, stopping at role="listitem" - rather than a mock that
    simply returns whatever the test wants.
    """

    def __init__(self, html):
        from linkedin_automation import dom_probe
        self.dom_probe = dom_probe
        self.root = dom_probe.parse_html(html)
        self.current_url = "https://www.linkedin.com/feed/update/x/"
        self.title = "A post"
        self.thread = []
        self.focused = None

    # -- the fixture's stand-ins for the real DOM API --
    def editor(self):
        return self.dom_probe.select_css(
            self.root, "div[role='textbox'][contenteditable='true']")[0]

    def find_elements(self, by, selector):
        if selector == cpm.LinkedInCommentPoster.POSTED_COMMENT_SELECTOR:
            return [FakeElement(t) for t in self.thread]
        if "normalize-space" in selector or selector == "button":
            return self._all_comment_buttons()
        return []

    def _all_comment_buttons(self):
        return [NodeButton(n) for n in self.root.walk()
                if n.tag == "button"
                and " ".join((n.text() or "").split()) == "Comment"]

    def execute_script(self, script, *args):
        if "focus()" in script:
            self.focused = args[0]
            return None
        if "dispatchEvent" in script:
            return None
        if "parentElement" in script:
            return self._walk(args[0])
        return None

    def _walk(self, box):
        """The same algorithm the JS implements."""
        texts = set(cpm.LinkedInCommentPoster.SUBMIT_BUTTON_TEXTS)
        stop = cpm.LinkedInCommentPoster.COMPOSER_STOP_ROLE
        node = getattr(box, "node", box).parent
        hops = 0
        while node is not None and hops < cpm.LinkedInCommentPoster.COMPOSER_MAX_HOPS:
            if (node.attrs.get("role") or "") == stop:
                return None                      # the post card: too far
            found = [n for n in node.walk()
                     if n.tag == "button"
                     and " ".join((n.text() or "").split()) in texts]
            if found:
                return NodeButton(found[-1])
            node = node.parent
            hops += 1
        return None

    def save_screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"png")
        return True

    def quit(self):
        return None

    @property
    def page_source(self):
        return "<html></html>"


class NodeButton:
    """A dom_probe node dressed as the bits of a WebElement we touch."""

    def __init__(self, node):
        self.node = node
        self.clicks = 0

    @property
    def text(self):
        return " ".join((self.node.text() or "").split())

    def get_attribute(self, name):
        return self.node.attrs.get(name)

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self.node.attrs.get("disabled") is None

    def click(self):
        self.clicks += 1

    def __eq__(self, other):
        return isinstance(other, NodeButton) and other.node is self.node

    def __hash__(self):
        return id(self.node)


def _editor_element(driver):
    return NodeButton(driver.editor())


def test_the_page_really_does_have_two_enabled_comment_buttons(poster):
    """The premise, asserted rather than assumed."""
    driver = DomDriver(TWO_COMMENT_BUTTONS)
    buttons = driver._all_comment_buttons()
    assert len(buttons) == 2
    assert all(b.is_enabled() for b in buttons)
    assert all(b.get_attribute("aria-label") is None for b in buttons)


def test_a_page_wide_text_match_takes_the_WRONG_button(poster):
    """What the old code did. Document order puts the action bar first."""
    driver = DomDriver(TWO_COMMENT_BUTTONS)
    first = driver.find_elements("xpath", "//button[normalize-space(.)='Comment']")[0]
    assert first.get_attribute("id") == "action-bar"


def test_the_scoped_locator_takes_the_COMPOSER_button(poster):
    """The fix: nearest ancestor of the editor that contains a submit."""
    driver = DomDriver(TWO_COMMENT_BUTTONS)
    poster.driver = driver
    chosen = poster.find_composer_submit(_editor_element(driver))
    assert chosen is not None
    assert chosen.get_attribute("id") == "composer-submit"


def test_the_walk_stops_at_the_post_card(poster):
    """With no composer submit, the walk must return None rather than climbing
    out to the action-bar button - returning that one is the bug."""
    html = TWO_COMMENT_BUTTONS.replace(
        '<button id="composer-submit">Comment</button>', "")
    driver = DomDriver(html)
    poster.driver = driver
    assert poster.find_composer_submit(_editor_element(driver)) is None


def test_no_composer_submit_means_refuse_not_reach_outside(poster):
    """With no submit in the composer, the answer is None - never the
    action-bar button that is sitting right there, enabled."""
    html = TWO_COMMENT_BUTTONS.replace(
        '<button id="composer-submit">Comment</button>', "")
    driver = DomDriver(html)
    poster.driver = driver
    poster.SUBMIT_ENABLE_TIMEOUT = 0.05
    poster.SUBMIT_ENABLE_POLL = 0.01
    button, enabled = poster.await_composer_submit(_editor_element(driver))
    assert button is None and enabled is False


def test_ctrl_enter_focuses_the_box_first(poster):
    """The shortcut goes wherever focus is. After a click that is the button,
    so the keyboard fallback may never have reached the editor at all."""
    driver = DomDriver(TWO_COMMENT_BUTTONS)
    poster.driver = driver
    box = FakeElement("")
    poster.post_comment_ctrl_enter(box, "some text", (0, []))
    assert driver.focused is box


# ─── the verifier must be ABLE to say yes ────────────────────────────────────
#
# Dispatch 1 made a thread match the only positive proof. Dispatch 4 then found
# POSTED_COMMENT_SELECTOR ("div.comments-comment-item") matches ZERO times on
# the current DOM - neither 2026-09-20 capture contains a single class token
# with "comment" in it - which made verification unsatisfiable: every posted
# comment would have been reported as a failure.
#
# The live hook is a data-testid ending in "-commentList". These fixtures are
# hand-authored in that shape, per tests/fixtures/README rule 1 (never paste a
# live DOM). The real capture was checked separately and behaves identically.

COMMENT_LIST_HTML = """
<div data-testid="AbC123-commentListXyz">
  <div>Feed post by Someone Else</div>
  <div>Most relevant</div>
  <div>
    Example Person  You
    Example Person • You
    AI Tech Lead — I work at the intersection of research and production.
    now
    How did you ensure real-time conversation accuracy? We were struggling with it too.
  </div>
</div>
"""


class ListDriver:
    """Serves a parsed comment-list fixture through the element API."""

    def __init__(self, html):
        from linkedin_automation import dom_probe
        self.dom_probe = dom_probe
        self.root = dom_probe.parse_html(html)

    def find_elements(self, by, selector):
        if selector == cpm.LinkedInCommentPoster.POSTED_COMMENT_SELECTOR:
            return []                       # the dead class selector
        if "commentList" in selector:
            return [_ListEl(n) for n in self.root.walk()
                    if "commentList" in (n.attrs.get("data-testid") or "")]
        return []


class _ListEl:
    def __init__(self, node):
        self.node = node

    @property
    def text(self):
        return " ".join((self.node.text() or "").split())

    def find_elements(self, by, selector):
        return [_ListEl(c) for c in self.node.children if hasattr(c, "tag")]


def test_the_dead_class_selector_really_does_match_nothing(poster):
    """The premise. If this ever starts matching again, the fallback is moot."""
    poster.driver = ListDriver(COMMENT_LIST_HTML)
    assert poster.driver.find_elements(
        None, cpm.LinkedInCommentPoster.POSTED_COMMENT_SELECTOR) == []


def test_the_verifier_returns_TRUE_for_a_comment_that_is_in_the_list(poster):
    """The whole point: a verifier that can only ever say no is not a verifier.

    Matched against the live commentList container, which is what the current
    DOM actually serves.
    """
    poster.driver = ListDriver(COMMENT_LIST_HTML)
    box = FakeElement("")
    assert poster.verify_comment_posted(
        box, "How did you ensure real-time conversation accuracy", (None, [])
    ) is True


def test_the_verifier_still_says_no_for_a_comment_that_is_not_there(poster):
    """And it must not have become a rubber stamp in the process."""
    poster.driver = ListDriver(COMMENT_LIST_HTML)
    box = FakeElement("")
    assert poster.verify_comment_posted(
        box, "a comment nobody ever wrote on this post", (None, [])
    ) is False


def test_container_mode_reports_no_countable_thread(poster):
    """count=None on purpose.

    The container's children are comments PLUS chrome, so the number moves for
    unrelated reasons. Against a pre-change snapshot of 0 it reads as enormous
    growth and the "thread grew" branch would fire on every attempt - turning
    "did it post?" into an unconditional yes.
    """
    poster.driver = ListDriver(COMMENT_LIST_HTML)
    count, texts = poster.comment_thread_snapshot()
    assert count is None
    assert texts and "real-time conversation accuracy" in texts[0]


def test_growth_alone_cannot_pass_in_container_mode(poster):
    """The false positive this caught: an empty box plus a 'grown' thread.

    With count=None the growth branch cannot fire at all, so an absent comment
    stays absent however much the container churns.
    """
    poster.driver = ListDriver(COMMENT_LIST_HTML)
    box = FakeElement("")                       # empty box, as after a submit
    assert poster.verify_comment_posted(
        box, "definitely not in this list", (0, [])) is False


# ─── the double-post guard ───────────────────────────────────────────────────
#
# The protection that does not depend on our records being right. Today they
# were not: a dead verifier reported three posted comments as failures, so the
# ledger says "not posted" about a comment that is live on LinkedIn. Asking the
# THREAD holds through a cleared store, a restored archive, a re-scrape, or a
# bug we have not found yet.

ALREADY_COMMENTED_HTML = """
<div data-testid="AbC123-commentListXyz">
  <div>Most relevant</div>
  <div>
    Example Person  You
    Example Person • You
    AI Tech Lead — I work at the intersection of research and production.
    now
    How did you ensure real-time conversation accuracy? We were struggling too.
  </div>
</div>
"""

SOMEONE_ELSE_COMMENTED_HTML = """
<div data-testid="AbC123-commentListXyz">
  <div>Most relevant</div>
  <div>
    Another Person • 2nd
    Some other job title
    3h
    A thoughtful comment from somebody who is not us at all.
  </div>
</div>
"""


def test_a_thread_we_already_commented_on_is_detected(poster):
    """The self byline is how LinkedIn marks your own comment."""
    poster.driver = ListDriver(ALREADY_COMMENTED_HTML)
    already, why = poster.already_commented_here("a completely different draft")
    assert already is True
    assert "already on this thread" in why


def test_a_thread_with_only_other_peoples_comments_is_not_blocked(poster):
    """The guard must not stop us commenting where we never have."""
    poster.driver = ListDriver(SOMEONE_ELSE_COMMENTED_HTML)
    already, why = poster.already_commented_here("our draft")
    assert already is False
    assert "no comment of ours" in why


def test_the_exact_text_being_present_also_counts(poster):
    """Even without the byline, our own words on the thread mean it is done."""
    html = SOMEONE_ELSE_COMMENTED_HTML.replace(
        "A thoughtful comment from somebody who is not us at all.",
        "How did you ensure real-time conversation accuracy?")
    poster.driver = ListDriver(html)
    already, _ = poster.already_commented_here(
        "How did you ensure real-time conversation accuracy?")
    assert already is True


def test_an_unreadable_thread_does_not_block_posting(poster):
    """Conservative in the SAFE direction.

    Refusing to post whenever the thread cannot be read would silently stop the
    tool, and every other guard still applies. The opposite default would be a
    duplicate comment, which is why the byline check exists at all.
    """
    class Blank:
        def find_elements(self, by, selector):
            return []

    poster.driver = Blank()
    already, why = poster.already_commented_here("our draft")
    assert already is False
    assert "not readable" in why


def test_the_guard_skips_without_typing_or_clicking(poster, monkeypatch):
    """End to end: nothing typed, nothing clicked, nothing posted."""
    typed, clicked = [], []
    monkeypatch.setattr(cpm.hb, "type_like_human",
                        lambda d, el, t: typed.append(t))
    monkeypatch.setattr(cpm.hb, "human_click", lambda d, el: clicked.append(el))

    poster.driver = ListDriver(ALREADY_COMMENTED_HTML)
    poster.open_comment_box = lambda: FakeElement("")
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True

    url = "https://www.linkedin.com/x/already-commented"
    assert poster.post_single_comment(
        {"url": url, "comment": "a fresh draft", "preview": "p"}) is True
    assert typed == [], "it typed into a thread it had already commented on"
    assert clicked == [], "it clicked on a thread it should have skipped"


def test_a_skipped_thread_is_recorded_as_posted_not_failed(poster):
    """The comment IS on LinkedIn, so the ledger is where this belongs.

    post_store reconciles COMMENTED from that file, so the record leaves the
    queue and stops being offered - which is the point, since re-offering it is
    how it gets posted twice.
    """
    poster.driver = ListDriver(ALREADY_COMMENTED_HTML)
    poster.open_comment_box = lambda: FakeElement("")
    poster.navigate_to_post = lambda url: True
    poster.like_post = lambda: True

    url = "https://www.linkedin.com/x/already-commented"
    poster.post_single_comment({"url": url, "comment": "a fresh draft",
                                "preview": "p"})

    assert url in poster.progress["posted_comments"]
    assert not poster.progress.get("failed_comments"), "a skip is not a failure"
    skipped = poster.progress["skipped_already_commented"]
    assert skipped[0]["url"] == url
    assert "already on this thread" in skipped[0]["reason"]


# ─── the like path: a real selector, and a bounded wait ──────────────────────
#
# All three previous LIKE_BUTTON_SELECTORS matched ZERO on the current DOM, and
# each was run through a 20-second WebDriverWait - so every comment spent SIXTY
# SECONDS discovering it could not like the post, then continued anyway. A third
# of the run, buying nothing.
#
# The fixture mirrors the action bar in the 2026-09-20 captures, including the
# reaction-menu decoys, hand-authored per fixtures/README rule 1.

ACTION_BAR_HTML = """
<div role="listitem">
  <button type="button" aria-label="Reaction button state: Like">Like</button>
  <button type="button" aria-label="Open reactions menu" aria-expanded="false"></button>
  <button type="button" aria-label="Open reactions menu" aria-expanded="false"></button>
  <button type="button">Comment</button>
  <button type="button">Repost</button>
  <button type="button">Send</button>
</div>
"""

ALREADY_LIKED_HTML = """
<div role="listitem">
  <button type="button" aria-label="Reaction button state: Liked">Liked</button>
  <button type="button" aria-label="Open reactions menu" aria-expanded="false"></button>
</div>
"""


class ActionBarDriver:
    """Serves a parsed action bar through the bits of the element API used."""

    def __init__(self, html=""):
        from linkedin_automation import dom_probe
        self.dom_probe = dom_probe
        self.root = dom_probe.parse_html(html) if html else None
        self.lookups = 0

    def find_elements(self, by, selector):
        self.lookups += 1
        if self.root is None:
            return []
        try:
            return [_BarEl(n)
                    for n in self.dom_probe.select_css(self.root, selector)]
        except Exception:
            return []


class _BarEl:
    """A node dressed as the WebElement surface the like path actually uses.

    _ListEl (above) has no is_displayed/is_enabled, and find_like_button
    swallows the AttributeError - so reusing it made every lookup silently
    return nothing.
    """

    def __init__(self, node):
        self.node = node

    @property
    def text(self):
        return ' '.join((self.node.text() or '').split())

    def get_attribute(self, name):
        return self.node.attrs.get(name)

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self.node.attrs.get('disabled') is None


def test_the_new_selector_finds_the_like_button(poster):
    """Derived from the capture, not guessed."""
    poster.driver = ActionBarDriver(ACTION_BAR_HTML)
    button = poster.find_like_button()
    assert button is not None
    assert button.get_attribute("aria-label") == "Reaction button state: Like"


def test_the_reaction_menu_decoys_are_never_returned(poster):
    """Six 'Open reactions menu' buttons sit in the same bar. Clicking one
    opens a menu instead of liking, so a loose [aria-label*='Like'] would be
    worse than the dead selector it replaced."""
    poster.driver = ActionBarDriver(ACTION_BAR_HTML)
    button = poster.find_like_button()
    assert button.get_attribute("aria-label") != "Open reactions menu"


def test_a_missing_like_button_returns_within_the_bound(poster):
    """THE DURABLE HALF OF THE FIX.

    The old path cost 60s to answer this. The next selector death must cost
    seconds, whatever the selectors are.
    """
    import time as _t
    poster.driver = ActionBarDriver("<div role='listitem'></div>")
    poster.LIKE_WAIT_SECONDS = 0.5
    poster.LIKE_POLL_SECONDS = 0.05

    started = _t.time()
    assert poster.find_like_button() is None
    elapsed = _t.time() - started
    assert elapsed < 2.0, "took %.1fs - the bound is not holding" % elapsed


def test_the_wait_is_bounded_in_total_not_per_selector(poster):
    """Three dead selectors must not cost three timeouts.

    That multiplication is exactly what made it 60s rather than 20s.
    """
    import time as _t
    poster.driver = ActionBarDriver("<div role='listitem'></div>")
    assert len(poster.LIKE_BUTTON_SELECTORS) >= 3, "need several to prove it"
    poster.LIKE_WAIT_SECONDS = 0.4
    poster.LIKE_POLL_SECONDS = 0.05

    started = _t.time()
    poster.find_like_button()
    elapsed = _t.time() - started
    assert elapsed < len(poster.LIKE_BUTTON_SELECTORS) * 0.4, (
        "%.2fs looks like a per-selector budget" % elapsed)


def test_a_missing_like_degrades_to_skip_and_continue(poster):
    """Liking is optional; failing to like must not stop the comment."""
    poster.driver = ActionBarDriver("<div role='listitem'></div>")
    poster.LIKE_WAIT_SECONDS = 0.2
    poster.LIKE_POLL_SECONDS = 0.05
    assert poster.like_post() is False          # reported, not raised


def test_an_already_liked_post_is_recognised(poster):
    """A different reaction state in the same aria-label family."""
    poster.driver = ActionBarDriver(ALREADY_LIKED_HTML)
    poster.LIKE_WAIT_SECONDS = 0.2
    poster.LIKE_POLL_SECONDS = 0.05
    assert poster.like_post() is True           # already liked, nothing to do


def test_the_old_dead_selectors_are_kept_as_fallbacks(poster):
    """MAINTENANCE step 4: new hooks first, old ones after.

    Affordable only because the wait is now bounded in total.
    """
    assert poster.LIKE_BUTTON_SELECTORS[0] == \
        "button[aria-label='Reaction button state: Like']"
    assert any("aria-pressed='false'" in s for s in poster.LIKE_BUTTON_SELECTORS)


# ─── the comment box is watched for, not slept through ───────────────────────

def test_the_comment_input_is_found_by_polling(poster):
    poster.driver = ActionBarDriver(
        "<div role='textbox' contenteditable='true' class='tiptap'></div>")
    assert poster.await_comment_input() is not None


def test_a_missing_comment_input_returns_within_its_cap(poster):
    import time as _t
    poster.driver = ActionBarDriver("<div></div>")
    poster.COMMENT_INPUT_WAIT_SECONDS = 0.4
    poster.COMMENT_INPUT_POLL_SECONDS = 0.05
    started = _t.time()
    assert poster.await_comment_input() is None
    assert _t.time() - started < 2.0


def test_the_flat_three_second_comment_box_sleep_is_gone(poster):
    """It cost three seconds whether the box took 200ms or never appeared."""
    import inspect
    src = inspect.getsource(cpm.LinkedInCommentPoster.open_comment_box)
    assert "human_sleep(2.5, 3.5)" not in src
    assert "await_comment_input" in src
