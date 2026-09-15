"""Phase 1b: image attachment, and the guarantee that it FAILS CLOSED.

The one thing this file exists to prove: when an image was asked for and the
attachment cannot be confirmed, **Post is never clicked**. A post that was meant
to carry an image going out without one is a different post from the one that
was authored, it is visible to everyone who sees it, and it is not recoverable.
Publishing nothing is the safe direction; silently degrading to text-only is not.

Everything is driven through a fake driver, so these run in CI with no browser.
The selectors and the state machine they encode came off a staged live harvest
of the dev account on 2026-09-04, not from guesswork.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation.poster import LinkedInPoster  # noqa: E402


# --- A fake driver -----------------------------------------------------------

class FakeElement:
    def __init__(self, tag="button", displayed=True, enabled=True):
        self.tag, self._displayed, self._enabled = tag, displayed, enabled
        self.sent = []
        self.clicked = 0
        self.files = 0

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def send_keys(self, value):
        # A real input[type=file][multiple] APPENDS. Modelling that is the whole
        # point: it is what turned a second delivery into a duplicate image.
        self.sent.append(value)
        self.files += 1

    def click(self):
        self.clicked += 1

    @property
    def text(self):
        return ""


class FakeDriver:
    """Answers deep-JS counts from a scripted sequence of DOM states.

    ``states`` is a list of dicts: {"editor": n, "preview": n}. Each completed
    media_is_attached() check consumes one state and moves to the next, so a
    test scripts progress simply by listing it: [mid-flight, mid-flight, done].
    The last state repeats forever, which is what lets a test express "this
    never completes" without an unbounded loop.
    """

    def __init__(self, states=None, file_input=None, media_button=True):
        self.states = states or [{"editor": 0, "preview": 0}]
        self.i = 0
        self.file_input = file_input
        self.media_button = media_button
        self.script_calls = []
        self.found = []
        self.captured = []
        self.picker_suppressed = False
        self.iframes = 0
        self.intercepted = 0           # picker opens the guard swallowed
        self.suppress_events = []      # ("suppress"|"restore"|"media_click", ...)
        self.extra_files_on_click = 0  # simulates LinkedIn's own picker delivering
                                       # a file too, when it is NOT suppressed

    @property
    def state(self):
        return self.states[min(self.i, len(self.states) - 1)]

    def execute_script(self, script, *args):
        self.script_calls.append((script, args))
        sel = args[0] if args else ""
        if "G.armed = true;" in script:                 # _SUPPRESS_PICKER_JS
            self.picker_suppressed = True
            self.suppress_events.append("suppress")
            return {"armed": True, "iframes": self.iframes}
        if "G.armed = false;" in script:                # _RESTORE_PICKER_JS
            was = self.picker_suppressed
            self.picker_suppressed = False
            self.suppress_events.append("restore")
            if not was:
                return None          # never armed, as the real page reports it
            return {"events": self.intercepted, "clicks": 0, "showPicker": 0}
        if "el.files" in script:                        # _FILE_COUNT_JS
            el = args[0] if args else None
            return getattr(el, "files", -1)
        if "return walk(document);" in script:          # _DEEP_FIND_JS
            if "file" in str(sel):
                return self.file_input
            if "Add media" in str(sel):
                return FakeElement() if self.media_button else None
            return None
        if "return n;" in script:                        # _DEEP_COUNT_JS
            s = str(sel)
            # Checked BEFORE the editor branch: every one of these also
            # contains "media-editor".
            if "content-preview" in s or "file-manager" in s:
                return self.state.get("loaded", self.state["editor"])
            if "media-editor" in s or "media-detour" in s:
                return self.state["editor"]
            if "preview" in s or "update-components-image" in s:
                n = self.state["preview"]
                # Advance on the FIRST preview selector only. _any_deep_count
                # tries every selector when they all come back zero, so keying
                # the advance to "any preview read" moved the script three
                # states per check instead of one.
                if "share-creation-state__preview-container" in s:
                    self.i += 1
                return n
            return 0
        return None

    def find_element(self, by, sel):
        self.found.append(sel)
        if "Add media" in str(sel) and self.media_button:
            self.suppress_events.append("media_click")
            # An UNSUPPRESSED picker is a second delivery path: LinkedIn opens
            # it on the Add-media click and whatever it returns lands on the
            # same input. This is what produced the double upload live.
            if self.picker_suppressed:
                self.intercepted += self.extra_files_on_click
            elif self.file_input is not None:
                self.file_input.files += self.extra_files_on_click
            return FakeElement()
        raise Exception("not found: %s" % (sel,))

    def find_elements(self, by, sel):
        return []


def make_poster(driver):
    p = LinkedInPoster.__new__(LinkedInPoster)
    p.driver = driver
    p.profile_name = "test"
    p.debug = False
    p._editor = None
    p.wait = None
    return p


@pytest.fixture(autouse=True)
def _no_sleeping_and_no_screenshots(monkeypatch):
    """Keep the suite fast and stop capture_failure touching a real browser."""
    import linkedin_automation.poster as mod
    monkeypatch.setattr(mod.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod.hb, "human_click", lambda drv, el: el.click())
    monkeypatch.setattr(mod, "capture_failure",
                        lambda drv, name, prof=None: drv.captured.append(name))
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)


@pytest.fixture
def image(tmp_path):
    f = tmp_path / "sample.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(f)


# --- The completion signal ---------------------------------------------------

def test_attached_means_preview_present_AND_editor_gone():
    """Both halves are required, and each on its own is a real failure mode."""
    both = make_poster(FakeDriver([{"editor": 0, "preview": 1}]))
    assert both.media_is_attached() is True

    # Mid-flight: the editor is up and the preview has not mounted.
    mid = make_poster(FakeDriver([{"editor": 1, "preview": 0}]))
    assert mid.media_is_attached() is False

    # The editor still up WITH a preview inside it - the media editor shows the
    # image before Next is clicked, so preview-alone would call this done.
    editing = make_poster(FakeDriver([{"editor": 1, "preview": 1}]))
    assert editing.media_is_attached() is False

    # Editor gone but nothing attached: this is what Cancel looks like.
    cancelled = make_poster(FakeDriver([{"editor": 0, "preview": 0}]))
    assert cancelled.media_is_attached() is False


def test_a_failing_dom_probe_never_reads_as_attached():
    """An exception in the count must not become a green light."""
    class Broken(FakeDriver):
        def execute_script(self, script, *args):
            raise RuntimeError("browser went away")

    p = make_poster(Broken())
    assert p.media_is_attached() is False


# --- The attach path ---------------------------------------------------------

def test_attach_sends_the_absolute_path_to_the_file_input(image):
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 1, "preview": 0}, {"editor": 0, "preview": 1}],
                     file_input=fi)
    p = make_poster(drv)
    assert p.attach_image(image) is True
    assert fi.sent == [os.path.abspath(image)]


def test_attach_never_clicks_the_file_input(image):
    """Clicking it opens the native OS picker, which is outside the DOM and
    blocks WebDriver. send_keys is the whole technique."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 1, "preview": 0}, {"editor": 0, "preview": 1}],
                     file_input=fi)
    assert make_poster(drv).attach_image(image) is True
    assert fi.clicked == 0


def test_the_file_input_is_looked_for_with_a_shadow_piercing_query(image):
    """It lives in a shadow root: the harvest recorded Selenium finding zero
    file inputs at the exact moment the element demonstrably existed."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    make_poster(drv).attach_image(image)
    deep = [s for s, a in drv.script_calls if "shadowRoot" in s and a
            and "file" in str(a[0])]
    assert deep, "the file input must be sought through shadow roots"


def test_a_missing_image_file_fails_before_touching_the_browser(tmp_path):
    drv = FakeDriver()
    assert make_poster(drv).attach_image(str(tmp_path / "nope.png")) is False
    assert drv.script_calls == []
    assert drv.found == []


def test_a_missing_media_button_aborts_and_captures(image):
    drv = FakeDriver([{"editor": 0, "preview": 0}], media_button=False)
    assert make_poster(drv).attach_image(image) is False
    assert "media_button_not_found" in drv.captured


def test_a_file_input_that_never_mounts_aborts_and_captures(image):
    drv = FakeDriver([{"editor": 1, "preview": 0}], file_input=None)
    p = make_poster(drv)
    p.MEDIA_EDITOR_TIMEOUT = 0.2
    assert p.attach_image(image) is False
    assert "media_file_input_missing" in drv.captured


def test_attachment_that_is_never_confirmed_times_out_and_captures(image):
    """The upload starts and simply never completes.

    The reason is now phase-specific: the image never rendered in the editor,
    so it fails there rather than in a single undifferentiated timeout.
    """
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 1, "preview": 0, "loaded": 0}], file_input=fi)
    p = make_poster(drv)
    p.MEDIA_EDITOR_TIMEOUT = 0.3
    p.MEDIA_ATTACH_TIMEOUT = 0.3
    assert p.attach_image(image) is False
    assert fi.sent, "the path was sent; it is the confirmation that failed"
    assert "media_never_loaded_in_editor" in drv.captured


# --- FAIL CLOSED: the point of the whole phase -------------------------------

class PostSpy(LinkedInPoster):
    """Records whether Post was ever clicked, and stubs the composer steps that
    are not under test here."""

    def __init__(self, driver, attach_ok, attached_at_post=True):
        self.driver = driver
        self.profile_name = "test"
        self.debug = False
        self._editor = None
        self.wait = None
        self.post_clicked = 0
        self._attach_ok = attach_ok
        self._attached_at_post = attached_at_post

    def _open_post_modal(self):
        return True

    def _type_post_content(self, text):
        return True

    def verify_composed_text(self, text):
        return True

    def attach_image(self, image_path):
        return self._attach_ok

    def media_is_attached(self):
        return self._attached_at_post

    def _click_post_button(self):
        self.post_clicked += 1
        return True


def test_failed_attachment_does_NOT_publish_text_only(image):
    """THE fail-closed proof."""
    drv = FakeDriver()
    p = PostSpy(drv, attach_ok=False)
    assert p.create_post("hello", image_path=image) is False
    assert p.post_clicked == 0, "text-only post escaped after a failed attach"


def test_successful_attachment_publishes(image):
    drv = FakeDriver()
    p = PostSpy(drv, attach_ok=True)
    assert p.create_post("hello", image_path=image) is True
    assert p.post_clicked == 1


def test_a_preview_that_vanishes_between_attach_and_post_aborts(image):
    """Confirmed-earlier is not true-now: the read-back guard runs in between,
    and a stray interaction can dismiss the media editor."""
    drv = FakeDriver()
    p = PostSpy(drv, attach_ok=True, attached_at_post=False)
    assert p.create_post("hello", image_path=image) is False
    assert p.post_clicked == 0
    assert "media_lost_before_post" in drv.captured


def test_a_post_with_no_image_behaves_exactly_as_before():
    """The default keeps every existing caller working, and must not acquire an
    image check that could block an ordinary text post."""
    drv = FakeDriver()
    p = PostSpy(drv, attach_ok=False, attached_at_post=False)
    assert p.create_post("hello") is True
    assert p.post_clicked == 1


def test_create_post_signature_keeps_image_path_optional():
    import inspect
    sig = inspect.signature(LinkedInPoster.create_post)
    assert sig.parameters["image_path"].default is None


# --- The double-attach regression (live Gate 1, 2026-09-05) ------------------
#
# Symptom: the image uploaded, then uploaded again. Not a repeated call in our
# code - attach_image, send_keys and the Next click each fire once, verified by
# tracing every call site. The cause is that clicking "Add media" makes LinkedIn
# call .click() on the hidden input itself, opening the NATIVE OS picker. That
# leaves two independent delivery paths to one input, and the input carries
# `multiple` with filecountlimit=20, so the second delivery APPENDS.

def test_send_keys_fires_exactly_once_per_create_post(image):
    """The headline assertion: one create_post, one delivery."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 1, "preview": 0}, {"editor": 0, "preview": 1}],
                     file_input=fi)
    p = make_poster(drv)
    assert p.attach_image(image) is True
    assert len(fi.sent) == 1, "send_keys fired %d times" % len(fi.sent)
    assert fi.files == 1


def test_the_native_picker_is_suppressed_BEFORE_the_media_button_is_clicked(image):
    """Ordering is the whole fix. Suppressing after the click is too late: the
    picker has already opened and a file can already be on its way."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    make_poster(drv).attach_image(image)
    assert "suppress" in drv.suppress_events
    assert "media_click" in drv.suppress_events
    assert (drv.suppress_events.index("suppress")
            < drv.suppress_events.index("media_click"))


def test_the_input_click_prototype_is_always_restored(image):
    """It is monkey-patched on the page. Leaving it patched would break every
    later file input in the session."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    make_poster(drv).attach_image(image)
    assert drv.suppress_events[-1] == "restore"
    assert drv.picker_suppressed is False


def test_the_prototype_is_restored_even_when_the_attach_fails(image):
    """A failure path that left the prototype patched would poison the session."""
    drv = FakeDriver([{"editor": 0, "preview": 0}], media_button=False)
    assert make_poster(drv).attach_image(image) is False
    assert drv.picker_suppressed is False
    assert "restore" in drv.suppress_events


def test_an_unsuppressed_picker_would_have_double_attached(image):
    """Reproduces the live bug, to prove the guard catches it rather than
    trusting that suppression always works."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    drv.extra_files_on_click = 1
    # Suppression disabled, exactly as if the page refused the patch.
    drv.execute_script = _no_suppress(drv)
    p = make_poster(drv)
    assert p.attach_image(image) is False, "a duplicate attach must not proceed"
    assert fi.files == 2, "the duplicate is what the guard is detecting"
    assert "media_double_attach" in drv.captured


def _no_suppress(drv):
    """execute_script with the picker suppression neutered."""
    real = FakeDriver.execute_script

    def patched(script, *args):
        if "G.armed = true;" in script:
            drv.suppress_events.append("suppress-failed")
            return None            # the page refused the patch
        return real(drv, script, *args)
    return patched


def test_attach_image_refuses_a_second_call_on_the_same_composer(image):
    """Belt and braces: even if a caller looped, the input would APPEND."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    p = make_poster(drv)
    assert p.attach_image(image) is True
    assert p.attach_image(image) is False
    assert len(fi.sent) == 1


def test_the_attach_budget_resets_for_the_next_post(image):
    """One poster instance can publish several posts; each gets one attach."""
    drv = FakeDriver()
    p = PostSpy(drv, attach_ok=True)
    p._image_attached = True          # left over from a previous post
    assert p.create_post("hello", image_path=image) is True
    assert p._image_attached is False or p.post_clicked == 1


def test_a_file_count_other_than_one_aborts_without_publishing(image):
    """Whatever the cause, more than one file on the input means the post would
    carry a duplicate image. Nothing publishes."""
    fi = FakeElement(tag="input")
    fi.files = 3
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)

    class Poster(LinkedInPoster):
        pass

    p = make_poster(drv)
    assert p.attach_image(image) is False
    assert "media_double_attach" in drv.captured


# --- Gate 1 runs A and B: the guard was a no-op ------------------------------
#
# Neither run printed the interception line and the OS picker still opened. Two
# defects: the log was emitted only when the count was non-zero, so silence was
# ambiguous between "not armed", "armed but idle" and "restore threw"; and the
# patch covered only HTMLInputElement.prototype.click, which is one of at least
# four ways that dialog opens.

def test_the_guard_cancels_the_click_default_action_not_just_the_prototype():
    """The primary guard must work for a <label> activation, which involves no
    JS call to patch - so it has to cancel the EVENT, not wrap a function."""
    from linkedin_automation.poster import LinkedInPoster as P
    js = P._SUPPRESS_PICKER_JS
    assert "addEventListener('click'" in js
    assert "true)" in js                      # capture phase
    assert "preventDefault()" in js
    assert "stopImmediatePropagation()" in js


def test_the_guard_also_covers_showPicker_which_dispatches_no_click():
    from linkedin_automation.poster import LinkedInPoster as P
    assert "showPicker" in P._SUPPRESS_PICKER_JS
    assert "showPicker" in P._RESTORE_PICKER_JS


def test_the_prototype_patch_is_on_HTMLElement_where_click_actually_lives():
    """v1 patched HTMLInputElement.prototype.click. `click` is defined on
    HTMLElement.prototype; that mismatch is part of why v1 intercepted nothing."""
    from linkedin_automation.poster import LinkedInPoster as P
    assert "HTMLElement.prototype.click" in P._SUPPRESS_PICKER_JS
    assert "HTMLElement.prototype.click" in P._RESTORE_PICKER_JS


def test_the_interception_count_is_logged_even_when_it_is_zero(image, caplog):
    """The diagnostic that runs A and B needed and did not get."""
    import logging
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    with caplog.at_level(logging.INFO, logger="linkedin_automation.poster"):
        make_poster(drv).attach_image(image)
    text = caplog.text
    assert "guard ARMED" in text
    assert "guard released" in text
    assert "intercepted 0" in text
    assert "intercepted NOTHING" in text


def test_a_guard_that_was_never_armed_says_so(image, caplog):
    import logging
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    drv.execute_script = _no_suppress(drv)
    with caplog.at_level(logging.WARNING, logger="linkedin_automation.poster"):
        make_poster(drv).attach_image(image)
    assert "never armed" in caplog.text


def test_an_armed_guard_swallows_the_picker_instead_of_it_delivering_a_file(image):
    """With the guard working, LinkedIn's picker open is intercepted and the
    input keeps exactly the one file send_keys put there."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    drv.extra_files_on_click = 1
    assert make_poster(drv).attach_image(image) is True
    assert fi.files == 1
    assert drv.intercepted == 1


# --- The watchdog fallback ---------------------------------------------------

def test_the_watchdog_only_closes_recognised_file_dialogs():
    """Closing the wrong window would be someone's unsaved work. Class, title
    and foreground must all match."""
    from linkedin_automation import poster as mod
    assert mod._DIALOG_CLASS == "#32770"
    for t in ("open", "choose file to upload"):
        assert t in mod._FILE_DIALOG_TITLES
    # The guard conditions live in code, so assert all three probes are used.
    import inspect
    body = inspect.getsource(mod.NativeFileDialogWatchdog._find_and_close)
    assert "GetForegroundWindow" in body
    assert "GetClassNameW" in body
    assert "GetWindowTextW" in body
    assert "_FILE_DIALOG_TITLES" in body


def test_the_watchdog_is_bounded_and_joined_not_a_daemon_left_running():
    from linkedin_automation.poster import NativeFileDialogWatchdog
    w = NativeFileDialogWatchdog(timeout=0.2, poll=0.05)
    with w:
        pass
    assert w._stop.is_set()
    if w._thread is not None:
        assert not w._thread.is_alive()


def test_the_watchdog_is_inert_off_windows(monkeypatch):
    from linkedin_automation.poster import NativeFileDialogWatchdog
    monkeypatch.setattr("linkedin_automation.poster.sys.platform", "linux")
    w = NativeFileDialogWatchdog(timeout=0.2)
    with w:
        pass
    assert w._thread is None
    assert w.closed == 0


def test_an_unrecognised_dialog_title_is_recorded_but_never_closed():
    """It must not close a dialog it cannot identify."""
    from linkedin_automation import poster as mod
    w = mod.NativeFileDialogWatchdog(timeout=0.1)
    w.titles_seen.append("Save changes to Document1?")
    assert w.closed == 0
    assert "Save changes to Document1?" in w.titles_seen


# --- Gate 1 run C: the guard watched the wrong TARGET, not the wrong document -
#
# Log: "ARMED (iframes on page: 3)" ... "intercepted 0" ... picker opened anyway.
#
# The iframes were a red herring. _DEEP_FIND_JS recurses only into shadowRoot,
# never contentDocument, and execute_script runs in the top-level browsing
# context - so "Found file input via input#media-editor-file-selector__file-input"
# is proof the input is in the MAIN document. It is in a SHADOW ROOT, and for a
# click originating inside one, the browser RETARGETS event.target to the shadow
# HOST by the time it reaches document. A target test can therefore never match
# the guarded element.

def test_the_guard_tests_composedPath_because_target_is_retargeted():
    from linkedin_automation.poster import LinkedInPoster as P
    js = P._SUPPRESS_PICKER_JS
    assert "composedPath" in js, "a target test cannot see into a shadow root"
    assert "isFileInput(e.target)" not in js, "the retargeting bug, still present"


def test_the_whole_composed_path_is_scanned_not_just_its_head():
    """Label activation lands the first click on the LABEL; the browser then
    forwards a click to the control. The input is somewhere in the path, not
    necessarily at path[0]."""
    from linkedin_automation.poster import LinkedInPoster as P
    js = P._SUPPRESS_PICKER_JS
    assert "for (var i = 0; i < path.length; i++)" in js
    assert "isFileInput(path[i])" in js


def test_the_guard_also_attaches_inside_shadow_roots_and_detaches_them():
    from linkedin_automation.poster import LinkedInPoster as P
    assert "sr.addEventListener('click', G.listener, true)" in P._SUPPRESS_PICKER_JS
    assert "removeEventListener('click', G.listener, true)" in P._RESTORE_PICKER_JS


def test_the_guard_is_rearmed_after_the_media_editor_mounts(image):
    """The media editor brings its own shadow root, which did not exist when the
    guard was first installed."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1}], file_input=fi)
    make_poster(drv).attach_image(image)
    arms = [e for e in drv.suppress_events if e == "suppress"]
    assert len(arms) >= 2, "expected an arm and a re-arm, got %d" % len(arms)
    assert drv.suppress_events.index("media_click") < len(drv.suppress_events) - 1


def test_a_forced_close_before_delivery_aborts_fast_instead_of_waiting(image):
    """WM_CLOSE is the dialog's Cancel, so LinkedIn's selector flow is dead.
    The 2026-09-05 run delivered anyway and then waited the full 60s to learn
    what a single check could have told it."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 1, "preview": 0}], file_input=fi)
    p = make_poster(drv)
    p.MEDIA_ATTACH_TIMEOUT = 30      # would dominate the test if it were used

    import linkedin_automation.poster as mod

    class FiredWatchdog(mod.NativeFileDialogWatchdog):
        def __enter__(self):
            self.closed = 1          # a dialog appeared and was forced shut
            return self

        def __exit__(self, *exc):
            return False

    mod_watchdog = mod.NativeFileDialogWatchdog
    mod.NativeFileDialogWatchdog = FiredWatchdog
    try:
        assert p.attach_image(image) is False
    finally:
        mod.NativeFileDialogWatchdog = mod_watchdog

    assert fi.sent == [], "nothing should be delivered into a cancelled selector"
    assert "media_picker_forced_close" in drv.captured


def test_fail_closed_still_holds_after_all_of_this(image):
    """The guarantee that must survive every iteration of this bug."""
    drv = FakeDriver()
    p = PostSpy(drv, attach_ok=False)
    assert p.create_post("hello", image_path=image) is False
    assert p.post_clicked == 0


# --- Gate 1 run D: the image attached, but Next was never clicked ------------
#
# Log: guard armed, no OS picker at all, "Sent the image path", then 60s and
# NOT PUBLISHED - with no "Clicked Next" line.
#
# Not the chicken-and-egg it looked like: the wait loop DID call
# _click_media_next() on every pass, before checking anything. The defect was
# that _click_media_next had no shadow fallback while _click_media_button did -
# which is why the log says "Clicked media button (shadow DOM)" but Next never
# reported anything. The media editor is in a shadow root, so every light-DOM
# lookup returned nothing, forever.

def test_clicking_next_has_a_shadow_fallback_like_the_media_button():
    """The asymmetry that stalled run D."""
    import inspect
    from linkedin_automation.poster import LinkedInPoster as P
    nxt = inspect.getsource(P._click_media_next)
    btn = inspect.getsource(P._click_media_button)
    assert "_deep_click_button" in nxt, "Next still cannot reach a shadow root"
    assert "_deep_find" in btn
    assert "shadow" in nxt.lower()


def test_the_deep_click_helper_crosses_shadow_roots_and_matches_by_text():
    from linkedin_automation.poster import LinkedInPoster as P
    js = P._DEEP_CLICK_BUTTON_JS
    assert "shadowRoot" in js
    assert "innerText" in js
    assert "aria-label" in js
    assert "b.disabled" in js               # never click a disabled Next
    assert "getClientRects().length > 0" in js


def test_next_is_clicked_via_the_shadow_path_when_light_dom_has_nothing(image):
    """FakeDriver.find_element raises for everything but Add media, which is
    exactly the live shape: the editor is invisible to light-DOM lookups."""
    clicks = []

    class ShadowDriver(FakeDriver):
        def execute_script(self, script, *args):
            if "tryRoot(document)" in script:
                clicks.append(args[0] if args else None)
                return True
            return FakeDriver.execute_script(self, script, *args)

    fi = FakeElement(tag="input")
    drv = ShadowDriver([{"editor": 1, "preview": 0, "loaded": 1},
                        {"editor": 1, "preview": 0, "loaded": 1},
                        {"editor": 0, "preview": 1, "loaded": 0}], file_input=fi)
    p = make_poster(drv)
    assert p.attach_image(image) is True
    assert clicks == ["Next"], "Next should be clicked once, through the shadow path"


# --- The three phases, each failing with its own reason ----------------------

def test_phase1_the_image_never_renders_in_the_editor(image):
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 1, "preview": 0, "loaded": 0}], file_input=fi)
    p = make_poster(drv)
    p.MEDIA_EDITOR_TIMEOUT = 0.3
    assert p.attach_image(image) is False
    assert "media_never_loaded_in_editor" in drv.captured


def test_phase2_next_can_never_be_clicked(image):
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 1, "preview": 0, "loaded": 1}], file_input=fi)
    p = make_poster(drv)
    p.MEDIA_EDITOR_TIMEOUT = 0.3
    assert p.attach_image(image) is False
    assert "media_next_not_clicked" in drv.captured


def test_phase3_next_worked_but_the_composer_never_showed_the_preview(image):
    """Next commits, the editor closes, and nothing arrives. Still fails closed."""
    clicks = []

    class ShadowDriver(FakeDriver):
        def execute_script(self, script, *args):
            if "tryRoot(document)" in script:
                clicks.append("next")
                return True
            return FakeDriver.execute_script(self, script, *args)

    fi = FakeElement(tag="input")
    drv = ShadowDriver([{"editor": 1, "preview": 0, "loaded": 1},
                        {"editor": 1, "preview": 0, "loaded": 1},
                        {"editor": 0, "preview": 0, "loaded": 0}], file_input=fi)
    p = make_poster(drv)
    p.MEDIA_EDITOR_TIMEOUT = 0.5
    p.MEDIA_ATTACH_TIMEOUT = 0.3
    assert p.attach_image(image) is False
    assert clicks, "Next was clicked"
    assert "media_attach_unconfirmed" in drv.captured


def test_a_flow_with_no_next_step_still_succeeds(image):
    """Phase 1 exits early if the composer preview is already there."""
    fi = FakeElement(tag="input")
    drv = FakeDriver([{"editor": 0, "preview": 1, "loaded": 0}], file_input=fi)
    assert make_poster(drv).attach_image(image) is True


def test_next_is_never_clicked_before_the_image_is_loaded(image):
    """Committing an empty editor would attach nothing."""
    order = []

    class OrderDriver(FakeDriver):
        def execute_script(self, script, *args):
            if "tryRoot(document)" in script:
                order.append(("next", self.i))
                return True
            if "return n;" in script and "content-preview" in str(args[0]):
                order.append(("loaded?", self.i))
            return FakeDriver.execute_script(self, script, *args)

    fi = FakeElement(tag="input")
    drv = OrderDriver([{"editor": 1, "preview": 0, "loaded": 0},
                       {"editor": 1, "preview": 0, "loaded": 1},
                       {"editor": 0, "preview": 1, "loaded": 0}], file_input=fi)
    p = make_poster(drv)
    p.MEDIA_EDITOR_TIMEOUT = 5
    p.attach_image(image)
    kinds = [k for k, _ in order]
    assert "next" in kinds
    assert kinds.index("loaded?") < kinds.index("next")
