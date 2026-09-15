"""
LinkedIn Post Creator
Posts text content to your LinkedIn feed.
Uses the same shadow DOM awareness as the connector.
"""

import os
import sys
import time
import logging
import threading
import argparse
import unicodedata

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from dotenv import load_dotenv

from . import profile_manager as pm
from . import human_behavior as hb
from .failure_capture import capture_failure

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─── The native file-dialog watchdog (Gate 1 fallback) ───────────────────────
#
# Prevention is the clean fix and is tried first, but it cannot be guaranteed:
# the picker may be opened by a path inside an iframe, or by a mechanism a page
# script can no longer reach. And when it does open, a native dialog BLOCKS
# WebDriver - the browser stops answering, so nothing in the attach flow can
# recover from inside the page.
#
# Which is why this runs OUTSIDE the browser, on its own thread: by the time the
# dialog is up, the thread driving Selenium is already stuck behind it. The file
# has ALREADY been delivered by send_keys at that point, so the dialog is
# redundant - closing it is not cancelling anything, it is dismissing a prompt
# whose answer we supplied by another route.
#
# Windows only, ctypes only, no new dependency (CLAUDE.md: prefer stdlib).
# Strictly bounded and always joined; it is not a daemon left running.
#
# It closes a window only when ALL of these hold, because closing the wrong
# window would be someone's unsaved work:
#   * the class is #32770, the Win32 common-dialog class
#   * the title is one Chrome actually uses for an upload dialog
#   * it is the FOREGROUND window
_FILE_DIALOG_TITLES = ("open", "choose file to upload", "choose files to upload",
                       "select file to upload", "select files to upload")
_DIALOG_CLASS = "#32770"
_WM_CLOSE = 0x0010


class NativeFileDialogWatchdog:
    """Close a Windows file-open dialog if one appears. Bounded; joined."""

    def __init__(self, timeout: float = 30.0, poll: float = 0.15):
        self.timeout, self.poll = timeout, poll
        self.closed = 0
        self.titles_seen = []
        self._stop = threading.Event()
        self._thread = None
        self._enabled = sys.platform == "win32"

    def _find_and_close(self):
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, buf, 256)
        if buf.value != _DIALOG_CLASS:
            return
        title = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, title, 512)
        name = (title.value or "").strip()
        if name.lower() not in _FILE_DIALOG_TITLES:
            # A dialog, but not one we recognise. Recorded, never touched.
            if name and name not in self.titles_seen:
                self.titles_seen.append(name)
            return
        u.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        self.closed += 1
        logger.warning("  Closed a native file dialog (%r) that blocked "
                       "WebDriver - the image was already delivered by "
                       "send_keys, so nothing was cancelled", name)

    def _run(self):
        deadline = time.time() + self.timeout
        while not self._stop.is_set() and time.time() < deadline:
            try:
                self._find_and_close()
            except Exception:
                logger.debug("file-dialog watchdog probe failed", exc_info=True)
            self._stop.wait(self.poll)

    def __enter__(self):
        if self._enabled:
            self._thread = threading.Thread(
                target=self._run, name="native-file-dialog-watchdog", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.titles_seen:
            logger.info("  Watchdog saw dialogs it did not recognise, and left "
                        "them alone: %s", self.titles_seen)
        return False


class LinkedInPoster:
    """Post content to LinkedIn feed."""

    # ─── Composer selectors ───────────────────────────────────────────────────
    #
    # These were inline literals inside the methods below, which is exactly why
    # selector_health could not cover the composer: its SELECTOR_REGISTRY is
    # built from class constants, and a literal buried in a method body is
    # invisible to it. The comment path was hoisted for this reason
    # (comment_poster.py) — the composer never was, so the one path that
    # PUBLISHES had no monitor at all. Same failure shape as the login form,
    # which rotted unnoticed for the same reason.
    #
    # Verified live against the dev account on 2026-08-30: a plain text post
    # published using exactly these. Keep the new-first / fallback ordering — a
    # stale selector tried first costs a full WebDriverWait timeout per post.

    # Step 1 — the feed control that OPENS the composer.
    COMPOSER_TRIGGER_TEXT = "start a post"       # matched on lowercased visible text
    COMPOSER_TRIGGER_SELECTORS = [
        "button[aria-label*='Start a post']",
        "div[aria-label*='Start a post']",
        "button[aria-label*='Create a post']",
        "div[role='button'][aria-placeholder*='Start']",
    ]
    # Deliberately NOT in the registry: dom_probe's XPath subset has no
    # contains(), and registering it would raise UnsupportedSelector and break
    # the build. It stays a live-only fallback, and the note on the registry
    # entry says so rather than leaving a reader to assume full coverage.
    COMPOSER_TRIGGER_XPATH = (
        "//*[contains(text(), 'Start a post') or contains(text(), 'start a post')]")

    # Step 2 — the editor inside the open composer.
    COMPOSER_EDITOR_SELECTORS = [
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']",
        "textarea",
    ]
    COMPOSER_EDITOR_ARIA_LABELS = ("Text editor", "Write", "Post", "Share")
    COMPOSER_EDITOR_ARIA_SELECTORS = [
        f"div[aria-label*='{label}'][contenteditable='true']"
        for label in COMPOSER_EDITOR_ARIA_LABELS
    ]

    # Step 3 — the button that PUBLISHES.
    #
    # XPath on visible text, for the same reason the comment submit button is:
    # the composer contains other controls and CSS cannot tell "the button whose
    # label is exactly Post" from the rest. A CSS attribute match would return a
    # confident wrong answer where this returns an obvious zero.
    COMPOSER_POST_BUTTON_TEXT = "Post"
    COMPOSER_POST_BUTTON_XPATH = "//button[normalize-space(.)='Post']"
    COMPOSER_POST_BUTTON_SELECTORS = [
        "button[aria-label='Post']",
        "button[aria-label='Post for anyone']",
    ]

    # ─── Phase 1b: image attachment (harvested live 2026-09-04, run 3) ────────
    #
    # Every constant below came off a staged live capture of the real flow, not
    # from guesswork. The observed state machine is:
    #
    #   composer open, text typed        Post ENABLED, no media editor
    #   click "Add media"                media editor MOUNTS, Post DISAPPEARS,
    #                                    "Next" appears, file input mounts
    #   send_keys the path               image loads, preview src is a data: URI
    #   click "Next"                     media editor UNMOUNTS
    #   image attached                   preview container MOUNTS, Post ENABLED
    #
    # Note what is NOT the completion signal: the preview image src is a `data:`
    # URI, never blob: and never a CDN host, so waiting for an upload URL would
    # wait forever. Completion is a DOM-shape change, not a URL change.
    COMPOSER_MEDIA_BUTTON_SELECTORS = [
        "button[aria-label='Add media']",
        "button.share-promoted-detour-button[aria-label='Add media']",
    ]

    # The file input LIVES IN A SHADOW ROOT under the media editor's file
    # selector panel, which is why plain Selenium CSS never finds it: a
    # find_elements(By.CSS_SELECTOR, "input[type=file]") returned 0 during the
    # harvest at the exact moment the element demonstrably existed. It has to be
    # reached by piercing shadow roots in JS (see _find_file_input).
    #
    # It is also NOT hidden in the is_displayed() sense: the harvest recorded
    # rects=1, display=block, visibility=visible, offsetParent set. The class is
    # `visually-hidden`, which clips it rather than removing it from layout. So
    # the roadmap's expectation ("is_displayed() will be false, a visibility
    # guard would reject it") is wrong for this element — the real obstacle was
    # the shadow boundary. Do not add a visibility guard to compensate for a
    # problem that does not exist.
    COMPOSER_FILE_INPUT_SELECTORS = [
        "input#media-editor-file-selector__file-input",
        "input.media-editor-file-selector__upload-media-input",
        "input[type='file'][name='file']",
        "input[type='file']",
    ]

    # The media editor: present between "Add media" and "Next".
    COMPOSER_MEDIA_EDITOR_SELECTORS = [
        "div.media-editor__layout-container",
        "div.media-detour__container",
        "div.media-editor-file-selector__container",
    ]

    # "Next" commits the media editor and returns to the composer. XPath on
    # visible text for the same reason the Post button uses one: CSS cannot say
    # "the button whose label is exactly Next", and the composer holds others.
    COMPOSER_MEDIA_NEXT_TEXT = "Next"
    COMPOSER_MEDIA_NEXT_XPATH = "//button[normalize-space(.)='Next']"
    COMPOSER_MEDIA_NEXT_SELECTORS = [
        "button[aria-label='Next']",
        "button.share-box-footer__primary-btn",
    ]

    # The image loaded INSIDE the media editor - the PRE-Next signal.
    #
    # This is a different thing from the composer preview below, and conflating
    # the two is what stalled the 2026-09-05 run: the editor only disappears
    # AFTER Next is clicked, so waiting for the composer preview before clicking
    # Next waits for a state that the un-taken action is supposed to produce.
    # Harvested from stage 5 of the run-3 capture.
    COMPOSER_MEDIA_LOADED_SELECTORS = [
        "div.media-editor-content-preview__container",
        "div.media-editor-content-preview__content-container",
        "button.media-editor-file-manager__file-preview",
    ]

    # The attached-image preview, back in the composer. THIS is the positive
    # completion signal the fail-closed wait keys off.
    COMPOSER_IMAGE_PREVIEW_SELECTORS = [
        "div.share-creation-state__preview-container",
        "div.update-components-image__container--preview",
        "button[aria-label='Edit media preview']",
    ]

    # Bounded waits. A timer is not a completion test, so these are only the
    # point at which we give up and FAIL CLOSED — never a substitute for the
    # signal itself.
    MEDIA_EDITOR_TIMEOUT = 20
    MEDIA_ATTACH_TIMEOUT = 60

    # ─── Typeahead containment (Phase 0 safety fix B) ─────────────────────────
    #
    # Typing is character-by-character (human_behavior.type_like_human), and `#`
    # opens LinkedIn's hashtag typeahead. Characters after it go into a live
    # dropdown, and a following newline can SELECT A SUGGESTION instead of
    # inserting a break — silently publishing text nobody wrote.
    #
    # These candidates are UNVERIFIED: the composer worked on its existing
    # selectors so no harvest was run, and the dropdown only exists mid-keystroke.
    # That is deliberately fine, because dismissal is best-effort and the
    # read-back guard below is the actual guarantee: if none of these match, no
    # Escape is sent, a corrupted body is caught before Post, and the run aborts
    # instead of publishing. If the live hashtag test aborts, harvest the real
    # hook and add it here.
    # The editor element the typing strategy actually used, so the read-back
    # guard reads the same thing it typed into. A CLASS attribute, not only an
    # instance one: callers (and tests) construct via __new__ without __init__,
    # and the guard must never fail with AttributeError on the publish path.
    _editor = None

    TYPEAHEAD_DROPDOWN_SELECTORS = [
        "div[role='listbox']",
        "ul[role='listbox']",
        "div[aria-live='polite'] [role='option']",
        "[role='option']",
    ]

    def __init__(self, profile_name: str = None, debug: bool = False):
        self.profile_name = profile_name
        self.debug = debug
        self.driver = None
        self.wait = None
        self._editor = None
        # Guards against a second attach appending to a `multiple` input.
        # Reset per create_post, since one poster can publish several posts.
        self._image_attached = False

        # Apply tunable human-behavior timing (typing/reading/scroll/break ranges)
        # from the profile config's "behavior" section so pacing is configurable
        # and consistent with the scraper/connector.
        hb.configure_behavior(pm.get_profile_config(profile_name).get("behavior"))

    def setup(self):
        """Setup browser and login."""
        logger.info("Setting up browser...")
        self.driver, profile = pm.create_driver(self.profile_name)
        self.wait = WebDriverWait(self.driver, 20)

        if not pm.login(self.driver, profile):
            raise RuntimeError("Failed to log in")

        logger.info("Logged in successfully")

    def navigate_to_feed(self):
        """Go to LinkedIn feed."""
        logger.info("Navigating to feed...")
        self.driver.get("https://www.linkedin.com/feed/")
        hb.human_sleep(3, 5)

    def create_post(self, text: str, image_path: str = None) -> bool:
        """Create a new LinkedIn post, optionally with one image attached.

        ``image_path`` defaults to None so every existing caller keeps working
        unchanged.

        FAILS CLOSED on the image. If an image was asked for and the attachment
        cannot be CONFIRMED, this aborts without publishing rather than falling
        back to a text-only post. A post that was meant to carry an image going
        out without one is a different post from the one that was authored, and
        it is visible to everyone who sees it — silently degrading is a worse
        outcome than publishing nothing, because nothing is recoverable.
        """
        logger.info(f"Creating post: \"{text[:80]}{'...' if len(text) > 80 else ''}\"")

        # A fresh composer gets a fresh attach budget of exactly one.
        self._image_attached = False

        # Step 1: Click "Start a post" to open the post modal
        if not self._open_post_modal():
            capture_failure(self.driver, "post_modal_failed", self.profile_name)
            return False

        hb.human_sleep(1.5, 2.5)

        # Step 2: Type the post content
        if not self._type_post_content(text):
            capture_failure(self.driver, "post_typing_failed", self.profile_name)
            return False

        hb.human_sleep(1.0, 2.0)

        # Step 2c: attach the image, if one was asked for. Before the read-back,
        # because the media editor replaces the composer body while it is open
        # and the editor would not be readable underneath it.
        if image_path:
            if not self.attach_image(image_path):
                logger.error("NOT PUBLISHED - an image was specified (%s) and the "
                             "attachment could not be confirmed", image_path)
                return False
            hb.human_sleep(0.8, 1.5)

        # Step 2b: READ BACK before publishing. The editor is the last thing that
        # touches the text, and the hashtag typeahead can rewrite a token between
        # keystroke and DOM without raising anything. An unverified publish is
        # irreversible, so a mismatch aborts here rather than shipping a post
        # nobody wrote.
        if not self.verify_composed_text(text):
            capture_failure(self.driver, "post_readback_mismatch", self.profile_name)
            logger.error("✗ NOT PUBLISHED — composer text did not match the intended text")
            return False

        # Step 3: Click Post.
        #
        # One more image check on the very edge of the irreversible action. The
        # attach was confirmed a moment ago, but "confirmed earlier" is not
        # "true now": the read-back guard sits between them and the media editor
        # can be dismissed by a stray interaction. Re-reading the signal here
        # costs one DOM query and closes the only remaining window in which a
        # text-only post could escape.
        if image_path and not self.media_is_attached():
            logger.error("NOT PUBLISHED - the image preview vanished between "
                         "attachment and Post")
            capture_failure(self.driver, "media_lost_before_post", self.profile_name)
            return False

        if not self._click_post_button():
            capture_failure(self.driver, "post_submit_failed", self.profile_name)
            return False

        logger.info("✓ Post published successfully!")
        return True

    # ─── Typing, and the guarantee that what publishes is what we composed ────

    @staticmethod
    def normalize_for_comparison(text: str) -> str:
        """Collapse a post body to the form the read-back guard compares on.

        Unicode-normalized (the editor re-renders quotes and dashes), whitespace
        collapsed (a contenteditable's innerText does not promise the newline
        shape we typed, and the typeahead workaround inserts a space before a
        newline), and lowercased. What survives is the WORDS — which is exactly
        what a typeahead substitution changes and what a re-render does not.

        Pure and static so it is unit-testable without a browser.
        """
        text = unicodedata.normalize("NFKC", text or "")
        text = text.replace(" ", " ")
        return " ".join(text.split()).strip().lower()

    def _typeahead_is_open(self) -> bool:
        """True if something that looks like LinkedIn's typeahead is on screen.

        Best-effort by design (see TYPEAHEAD_DROPDOWN_SELECTORS). A false
        negative costs nothing: no Escape is sent, the space fallback runs, and
        the read-back guard still refuses to publish corrupted text.
        """
        for sel in self.TYPEAHEAD_DROPDOWN_SELECTORS:
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        return True
            except Exception:
                continue
        return False

    def _dismiss_typeahead(self, element=None):
        """Close the hashtag typeahead before a newline can select a suggestion.

        NEVER sends a bare Escape. In an open composer modal, Escape with no
        dropdown showing closes the modal and discards the draft — trading a
        text-corruption bug for a lost-post bug. So Escape goes out only when a
        dropdown is actually visible; otherwise a single space closes the
        hashtag token, which the comparison's whitespace collapsing renders
        invisible.
        """
        if self._typeahead_is_open():
            try:
                if element is not None:
                    element.send_keys(Keys.ESCAPE)
                else:
                    ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                hb.human_sleep(0.15, 0.35)
                return "escape"
            except Exception:
                logger.debug("Typeahead Escape failed; falling back to space",
                             exc_info=True)
        try:
            if element is not None:
                element.send_keys(" ")
            else:
                ActionChains(self.driver).send_keys(" ").perform()
            return "space"
        except Exception:
            logger.debug("Typeahead space fallback failed", exc_info=True)
            return None

    def _segments_for_typing(self, text: str):
        """Split ``text`` so a hashtag is never immediately followed by a newline.

        Returns ``[(chunk, needs_dismiss), ...]``. ``needs_dismiss`` marks a chunk
        whose LAST token opened the typeahead and whose NEXT character is a
        newline — the exact sequence in which Enter selects a suggestion instead
        of inserting a break.

        Pure, so the split is unit-testable without a browser.
        """
        segments, buf = [], ""
        for i, ch in enumerate(text):
            if ch == "\n":
                last = buf.split()[-1] if buf.split() else ""
                segments.append((buf, last.startswith("#") and len(last) > 1))
                segments.append(("\n", False))
                buf = ""
            else:
                buf += ch
        if buf:
            segments.append((buf, False))
        return [seg for seg in segments if seg[0]]

    def _type_into(self, element, text: str):
        """Type into a WebElement, containing the typeahead at newline boundaries."""
        self._editor = element
        hb.human_click(self.driver, element)
        hb.human_sleep(0.3, 0.5)
        for chunk, needs_dismiss in self._segments_for_typing(text):
            if chunk == "\n":
                element.send_keys(Keys.ENTER)
                continue
            hb.type_like_human(self.driver, element, chunk)
            if needs_dismiss:
                self._dismiss_typeahead(element)

    def _type_focused(self, text: str):
        """Same, for the shadow-DOM path where there is no WebElement."""
        for chunk, needs_dismiss in self._segments_for_typing(text):
            if chunk == "\n":
                ActionChains(self.driver).send_keys(Keys.ENTER).perform()
                continue
            hb.type_like_human_keys(self.driver, chunk)
            if needs_dismiss:
                self._dismiss_typeahead(None)

    def read_editor_text(self) -> str:
        """Read back what is actually in the composer right now.

        Prefers the element the typing strategy used; falls back to a JS sweep of
        visible editors (including shadow roots) when there is no WebElement.
        """
        if self._editor is not None:
            try:
                value = (self._editor.get_attribute("innerText")
                         or self._editor.get_attribute("value")
                         or self._editor.text or "")
                if value.strip():
                    return value
            except Exception:
                logger.debug("Editor read-back via WebElement failed", exc_info=True)
        try:
            return self.driver.execute_script("""
                function pick(root) {
                    var els = root.querySelectorAll(
                        'div[contenteditable="true"], textarea');
                    for (var i = 0; i < els.length; i++) {
                        if (els[i].offsetParent !== null) {
                            return els[i].innerText || els[i].value || '';
                        }
                    }
                    return null;
                }
                var found = pick(document);
                if (found !== null) { return found; }
                var hosts = document.querySelectorAll('*');
                for (var i = 0; i < hosts.length; i++) {
                    if (hosts[i].shadowRoot) {
                        var v = pick(hosts[i].shadowRoot);
                        if (v !== null) { return v; }
                    }
                }
                return '';
            """) or ""
        except Exception:
            logger.debug("Editor read-back via JS failed", exc_info=True)
            return ""

    def verify_composed_text(self, intended: str) -> bool:
        """The Phase 0 guarantee: publish what we composed, or publish nothing.

        Mechanism-independent on purpose. Whatever the typing path did — human
        typing, a shadow-DOM fallback, a typeahead that swallowed a token — the
        editor's contents are compared against the intended text before Post is
        ever clicked. This is the guard; the typeahead dismissal above is only an
        attempt to avoid needing it.
        """
        actual = self.read_editor_text()
        if self.normalize_for_comparison(actual) == self.normalize_for_comparison(intended):
            return True
        logger.error(
            "Composer read-back MISMATCH — refusing to publish.\n"
            "  intended: %r\n"
            "  in editor: %r",
            intended[:400], actual[:400],
        )
        return False

    def _open_post_modal(self) -> bool:
        """Click 'Start a post' to open the post creation modal."""

        # Strategy 1: Button with "Start a post" text
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buttons:
                try:
                    text = btn.text.strip().lower()
                    if self.COMPOSER_TRIGGER_TEXT in text:
                        if btn.is_displayed():
                            logger.info("  Found 'Start a post' button")
                            hb.human_click(self.driver, btn)
                            return True
                except Exception:
                    continue
        except Exception:
            logger.debug("Strategy 1 (button text) failed", exc_info=True)

        # Strategy 2: The post prompt area (div/span with "Start a post")
        try:
            el = self.driver.find_element(By.XPATH, self.COMPOSER_TRIGGER_XPATH)
            if el.is_displayed():
                logger.info("  Found post prompt element")
                hb.human_click(self.driver, el)
                return True
        except Exception:
            logger.debug("Strategy 2 (post prompt element) failed", exc_info=True)

        # Strategy 3: aria-label or placeholder
        for sel in self.COMPOSER_TRIGGER_SELECTORS:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    logger.info(f"  Found post trigger: {sel}")
                    hb.human_click(self.driver, el)
                    return True
            except Exception:
                continue

        # Strategy 4: Shadow DOM
        try:
            clicked = self.driver.execute_script("""
                // Check regular DOM first
                var allEls = document.querySelectorAll('button, div[role="button"], span');
                for (var i = 0; i < allEls.length; i++) {
                    var text = allEls[i].textContent.trim().toLowerCase();
                    if (text.includes('start a post') && allEls[i].offsetParent !== null) {
                        allEls[i].click();
                        return 'regular DOM';
                    }
                }
                // Check shadow DOMs
                var all = document.querySelectorAll('*');
                for (var i = 0; i < all.length; i++) {
                    if (all[i].shadowRoot) {
                        var els = all[i].shadowRoot.querySelectorAll('button, div[role="button"], span');
                        for (var j = 0; j < els.length; j++) {
                            var text = els[j].textContent.trim().toLowerCase();
                            if (text.includes('start a post') && els[j].offsetParent !== null) {
                                els[j].click();
                                return 'shadow DOM';
                            }
                        }
                    }
                }
                return null;
            """)
            if clicked:
                logger.info(f"  Opened post modal via {clicked}")
                return True
        except Exception:
            logger.debug("Strategy 4 (shadow DOM) failed", exc_info=True)

        logger.error("Could not find 'Start a post' button")
        return False

    def _type_post_content(self, text: str) -> bool:
        """Type the post content into the editor."""

        # Wait for the modal/editor to appear
        hb.human_sleep(1.0, 1.5)

        # Strategy 1: contenteditable div (standard LinkedIn post editor)
        try:
            editor = self.wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, self.COMPOSER_EDITOR_SELECTORS[0])
            ))
            if editor.is_displayed():
                logger.info("  Found post editor (contenteditable)")
                self._type_into(editor, text)
                return True
        except TimeoutException:
            logger.debug("Strategy 1 (editor lookup) timed out; trying Strategy 2", exc_info=True)

        # Strategy 2: Any contenteditable in a modal/dialog
        try:
            editables = self.driver.find_elements(
                By.CSS_SELECTOR, self.COMPOSER_EDITOR_SELECTORS[1]
            )
            for ed in editables:
                if ed.is_displayed():
                    logger.info("  Found contenteditable div")
                    self._type_into(ed, text)
                    return True
        except Exception:
            logger.debug("Strategy 2 (contenteditable div) failed", exc_info=True)

        # Strategy 3: textarea fallback
        try:
            textareas = self.driver.find_elements(
                By.CSS_SELECTOR, self.COMPOSER_EDITOR_SELECTORS[2])
            for ta in textareas:
                if ta.is_displayed():
                    logger.info("  Found textarea")
                    self._type_into(ta, text)
                    return True
        except Exception:
            logger.debug("Strategy 3 (textarea) failed", exc_info=True)

        # Strategy 4: Shadow DOM editor
        try:
            typed = self.driver.execute_script("""
                var hosts = document.querySelectorAll('*');
                for (var i = 0; i < hosts.length; i++) {
                    if (hosts[i].shadowRoot) {
                        var editors = hosts[i].shadowRoot.querySelectorAll(
                            'div[contenteditable="true"], textarea'
                        );
                        for (var j = 0; j < editors.length; j++) {
                            if (editors[j].offsetParent !== null) {
                                editors[j].focus();
                                editors[j].click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            """)
            if typed:
                # Type with human-like timing into the JS-focused shadow-DOM editor
                # (no WebElement to target, so use the focused-element variant).
                # self._editor stays None; the read-back guard falls back to JS.
                self._editor = None
                self._type_focused(text)
                logger.info("  Typed into shadow DOM editor")
                return True
        except Exception:
            logger.debug("Strategy 4 (shadow DOM editor) failed", exc_info=True)

        # Strategy 5: aria-label based
        for label, sel in zip(self.COMPOSER_EDITOR_ARIA_LABELS,
                              self.COMPOSER_EDITOR_ARIA_SELECTORS):
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    self._type_into(el, text)
                    logger.info(f"  Typed into editor (aria-label: {label})")
                    return True
            except Exception:
                continue

        logger.error("Could not find post text editor")
        return False

    # --- Phase 1b: attaching an image ----------------------------------------

    # Shadow roots are separate trees: document.querySelectorAll does not cross
    # them and neither does Selenium's CSS. Returning the element itself from
    # execute_script hands Python a real WebElement, so send_keys works on it
    # exactly as it would on a light-DOM input.
    _DEEP_FIND_JS = """
        var sel = arguments[0];
        function walk(root) {
            var hit = null;
            try { hit = root.querySelector(sel); } catch (e) { return null; }
            if (hit) { return hit; }
            var all;
            try { all = root.querySelectorAll('*'); } catch (e) { return null; }
            for (var i = 0; i < all.length; i++) {
                if (all[i].shadowRoot) {
                    var deep = walk(all[i].shadowRoot);
                    if (deep) { return deep; }
                }
            }
            return null;
        }
        return walk(document);
    """

    _DEEP_COUNT_JS = """
        var sel = arguments[0];
        var n = 0;
        function walk(root) {
            try { n += root.querySelectorAll(sel).length; } catch (e) { return; }
            var all;
            try { all = root.querySelectorAll('*'); } catch (e) { return; }
            for (var i = 0; i < all.length; i++) {
                if (all[i].shadowRoot) { walk(all[i].shadowRoot); }
            }
        }
        walk(document);
        return n;
    """

    # THE NATIVE-PICKER FIX (v2, after Gate 1 runs A and B).
    #
    # Clicking "Add media" makes LinkedIn open the NATIVE OS file picker. That
    # is fatal twice over: a native dialog blocks WebDriver, so nothing after it
    # runs until the timeout; and it is a second delivery path into an input
    # that carries `multiple`, so anything it returns APPENDS to the file we
    # already delivered.
    #
    # v1 patched HTMLInputElement.prototype.click and was a NO-OP - the live
    # runs never intercepted anything. Two reasons, both fixed here:
    #
    #   * `click` is defined on HTMLElement.prototype, not HTMLInputElement's;
    #   * and the picker has at least three other doors. `showPicker()` opens it
    #     without dispatching a click at all. A <label> wrapping or pointing at
    #     the input activates it natively, with no JS call to intercept. A
    #     dispatched MouseEvent does the same.
    #
    # So the primary guard is now at the EVENT level: a capture-phase listener
    # that calls preventDefault() on any click landing on a file input. Opening
    # the picker is that click's DEFAULT ACTION, so cancelling the event stops
    # it no matter which door was used - prototype call, label activation, or a
    # real click. showPicker() is patched separately because it dispatches
    # nothing. The prototype patch is kept as a third layer.
    #
    # Everything is restored in a finally block, and every count is logged even
    # when it is zero: a silent guard is why run A and run B were ambiguous
    # between "not armed", "armed but never triggered" and "restore threw".
    _SUPPRESS_PICKER_JS = """
        if (!window.__lipGuard) { window.__lipGuard = {}; }
        var G = window.__lipGuard;
        G.events = 0; G.clicks = 0; G.showPicker = 0;

        function isFileInput(el) {
            return !!el && el.tagName === 'INPUT' && el.type === 'file';
        }

        // 1) THE PRIMARY GUARD. Opening the picker is the default action of a
        //    click on a file input, so cancelling the event in the capture
        //    phase stops it however the click originated - including a <label>
        //    activation, which involves no JS call to patch.
        //
        //    It MUST test composedPath(), not target. The input lives in a
        //    SHADOW ROOT, and by the time a click from inside one reaches
        //    document, the browser has RETARGETED event.target to the shadow
        //    HOST - so a target test is false for the very element we are
        //    guarding. That is exactly why the v2 guard reported
        //    "intercepted 0" while the picker opened anyway.
        //
        //    The whole path is scanned rather than just path[0], because label
        //    activation lands the first click on the LABEL and the browser then
        //    forwards a second click to the control; the input is somewhere in
        //    that path, not necessarily at its head.
        if (!G.listener) {
            G.listener = function (e) {
                var path = (e.composedPath && e.composedPath()) || [e.target];
                for (var i = 0; i < path.length; i++) {
                    if (isFileInput(path[i])) {
                        G.events += 1;
                        e.preventDefault();
                        e.stopImmediatePropagation();
                        return;
                    }
                }
            };
            document.addEventListener('click', G.listener, true);
        }

        // 1b) Belt and braces: shadow roots are EventTargets in their own
        //     right, so the guard is attached inside each one too. The document
        //     listener above already covers them via composedPath; this catches
        //     an event stopped before it ever reaches document. Re-runnable, so
        //     it can be re-applied once the media editor mounts its own root.
        G.shadowRoots = G.shadowRoots || [];
        (function armShadows(root) {
            var all;
            try { all = root.querySelectorAll('*'); } catch (e) { return; }
            for (var i = 0; i < all.length; i++) {
                var sr = all[i].shadowRoot;
                if (!sr) { continue; }
                if (G.shadowRoots.indexOf(sr) === -1) {
                    try {
                        sr.addEventListener('click', G.listener, true);
                        G.shadowRoots.push(sr);
                    } catch (e) {}
                }
                armShadows(sr);
            }
        })(document);

        // 2) showPicker() opens the dialog WITHOUT dispatching a click, so the
        //    listener above cannot see it.
        if (HTMLInputElement.prototype.showPicker && !G.origShowPicker) {
            G.origShowPicker = HTMLInputElement.prototype.showPicker;
            HTMLInputElement.prototype.showPicker = function () {
                if (this.type === 'file') { G.showPicker += 1; return undefined; }
                return G.origShowPicker.apply(this, arguments);
            };
        }

        // 3) The direct prototype call, on HTMLElement where click actually
        //    lives - v1 patched HTMLInputElement's own prototype, which the
        //    call does not necessarily go through.
        if (!G.origClick) {
            G.origClick = HTMLElement.prototype.click;
            HTMLElement.prototype.click = function () {
                if (isFileInput(this)) { G.clicks += 1; return undefined; }
                return G.origClick.apply(this, arguments);
            };
        }

        G.armed = true;
        return {armed: true,
                iframes: document.querySelectorAll('iframe').length,
                shadowRoots: (G.shadowRoots || []).length};
    """

    _RESTORE_PICKER_JS = """
        var G = window.__lipGuard;
        if (!G) { return null; }
        if (G.listener) {
            document.removeEventListener('click', G.listener, true);
            var roots = G.shadowRoots || [];
            for (var i = 0; i < roots.length; i++) {
                try { roots[i].removeEventListener('click', G.listener, true); }
                catch (e) {}
            }
            G.shadowRoots = [];
            G.listener = null;
        }
        if (G.origShowPicker) {
            HTMLInputElement.prototype.showPicker = G.origShowPicker;
            G.origShowPicker = null;
        }
        if (G.origClick) {
            HTMLElement.prototype.click = G.origClick;
            G.origClick = null;
        }
        G.armed = false;
        return {events: G.events, clicks: G.clicks, showPicker: G.showPicker};
    """

    # Authoritative post-send_keys check: how many files the input actually
    # holds. A count other than 1 means a second path delivered a file, which is
    # the bug this whole section exists to prevent.
    _FILE_COUNT_JS = """
        var el = arguments[0];
        return (el && el.files) ? el.files.length : -1;
    """

    def _suppress_native_picker(self, quiet: bool = False) -> bool:
        try:
            info = self.driver.execute_script(self._SUPPRESS_PICKER_JS) or {}
            logger.info("  Native picker guard %s (shadow roots: %s, "
                        "iframes: %s)", "RE-ARMED" if quiet else "ARMED",
                        info.get("shadowRoots"), info.get("iframes"))
            return True
        except Exception:
            logger.warning("  Native picker guard COULD NOT BE ARMED",
                           exc_info=True)
            return False

    def _restore_native_picker(self) -> dict:
        """Restore everything, and ALWAYS report what was intercepted.

        Logged even when every count is zero. Run A and run B printed nothing at
        all, which left "not armed", "armed but never triggered" and "restore
        threw" indistinguishable - the diagnostic that mattered most was the one
        the guard declined to emit.
        """
        try:
            counts = self.driver.execute_script(self._RESTORE_PICKER_JS)
        except Exception:
            logger.warning("  Native picker guard COULD NOT BE RESTORED",
                           exc_info=True)
            return {}
        if counts is None:
            logger.warning("  Native picker guard was never armed on this page")
            return {}
        total = sum(int(counts.get(k) or 0)
                    for k in ("events", "clicks", "showPicker"))
        logger.info("  Native picker guard released - intercepted %d "
                    "(click events=%s, .click() calls=%s, showPicker()=%s)",
                    total, counts.get("events"), counts.get("clicks"),
                    counts.get("showPicker"))
        if total == 0:
            logger.warning("    intercepted NOTHING. If the OS picker still "
                           "appeared, it was opened by a path this guard cannot "
                           "see; the watchdog below is what closes it.")
        return counts

    def _deep_find(self, selector):
        """First element matching ``selector``, piercing shadow roots."""
        try:
            return self.driver.execute_script(self._DEEP_FIND_JS, selector)
        except Exception:
            logger.debug("deep find failed for %r", selector, exc_info=True)
            return None

    def _deep_count(self, selector) -> int:
        """How many elements match, piercing shadow roots. 0 on error.

        Used only for the completion signals, where a failed probe and a genuine
        zero mean the same thing to the caller: not confirmed, keep waiting, and
        eventually fail closed. Nothing here can turn an error into a green light.
        """
        try:
            return int(self.driver.execute_script(self._DEEP_COUNT_JS, selector) or 0)
        except Exception:
            logger.debug("deep count failed for %r", selector, exc_info=True)
            return 0

    def _any_deep_count(self, selectors) -> int:
        for sel in selectors:
            n = self._deep_count(sel)
            if n:
                return n
        return 0

    # Clicking a button by its visible text, ACROSS SHADOW ROOTS.
    #
    # The media editor lives in a shadow root - the media button is already
    # clicked through _deep_find for exactly that reason ("Clicked media button
    # (shadow DOM)" in the logs). Its Next button is in the same root, and
    # driver.find_element cannot see either of them. XPath is no help here:
    # Selenium's XPath does not cross a shadow boundary, so text matching has to
    # happen inside the page.
    _DEEP_CLICK_BUTTON_JS = """
        var want = String(arguments[0]).trim().toLowerCase();
        function tryRoot(root) {
            var btns;
            try { btns = root.querySelectorAll('button'); } catch (e) { return null; }
            for (var i = 0; i < btns.length; i++) {
                var b = btns[i];
                var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                var a = (b.getAttribute('aria-label') || '').trim().toLowerCase();
                if ((t === want || a === want) && !b.disabled &&
                        b.getClientRects().length > 0) {
                    b.click();
                    return true;
                }
            }
            var all;
            try { all = root.querySelectorAll('*'); } catch (e) { return null; }
            for (var j = 0; j < all.length; j++) {
                if (all[j].shadowRoot) {
                    var hit = tryRoot(all[j].shadowRoot);
                    if (hit) { return true; }
                }
            }
            return null;
        }
        return tryRoot(document) ? true : false;
    """

    def _deep_click_button(self, text: str) -> bool:
        try:
            return bool(self.driver.execute_script(self._DEEP_CLICK_BUTTON_JS, text))
        except Exception:
            logger.debug("deep button click failed for %r", text, exc_info=True)
            return False

    def media_loaded_in_editor(self) -> bool:
        """Has the delivered image rendered INSIDE the media editor yet?

        The pre-Next signal. Next is only meaningful once the editor is showing
        the image; clicking it before then would commit an empty editor.
        """
        editor = self._any_deep_count(self.COMPOSER_MEDIA_EDITOR_SELECTORS)
        loaded = self._any_deep_count(self.COMPOSER_MEDIA_LOADED_SELECTORS)
        return editor > 0 and loaded > 0

    def _click_media_button(self) -> bool:
        """Open the media editor. This is what mounts the file input."""
        for sel in self.COMPOSER_MEDIA_BUTTON_SELECTORS:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed() and btn.is_enabled():
                    hb.human_click(self.driver, btn)
                    logger.info("  Clicked media button (%s)", sel)
                    return True
            except Exception:
                continue
        # Shadow fallback, matching how the Post button is found.
        el = self._deep_find(self.COMPOSER_MEDIA_BUTTON_SELECTORS[0])
        if el is not None:
            try:
                self.driver.execute_script("arguments[0].click();", el)
                logger.info("  Clicked media button (shadow DOM)")
                return True
            except Exception:
                logger.debug("shadow media-button click failed", exc_info=True)
        logger.error("Could not find the media button")
        return False

    def _find_file_input(self):
        """The file input, wherever it is mounted."""
        for sel in self.COMPOSER_FILE_INPUT_SELECTORS:
            el = self._deep_find(sel)
            if el is not None:
                logger.info("  Found file input via %r", sel)
                return el
        return None

    def _wait_for_file_input(self, timeout=None):
        """Poll for the input the media editor mounts.

        It does not exist before the media button is clicked, and it lives in a
        shadow root once it does — the harvest recorded Selenium finding zero
        file inputs at the exact moment the element demonstrably existed.
        """
        deadline = time.time() + (timeout or self.MEDIA_EDITOR_TIMEOUT)
        while time.time() < deadline:
            el = self._find_file_input()
            if el is not None:
                return el
            time.sleep(0.25)
        return None

    def media_is_attached(self) -> bool:
        """The positive completion signal, read off the live DOM.

        Confirmed against the live capture: with the image attached the media
        editor is GONE and the preview container is PRESENT. Requiring both is
        deliberate. The preview alone could be a stale node mid-transition, and
        the editor being gone alone is also what Cancel looks like.
        """
        editor = self._any_deep_count(self.COMPOSER_MEDIA_EDITOR_SELECTORS)
        preview = self._any_deep_count(self.COMPOSER_IMAGE_PREVIEW_SELECTORS)
        return preview > 0 and editor == 0

    def _click_media_next(self) -> bool:
        """Commit the media editor and return to the composer."""
        try:
            btn = self.driver.find_element(By.XPATH, self.COMPOSER_MEDIA_NEXT_XPATH)
            if btn.is_displayed() and btn.is_enabled():
                hb.human_click(self.driver, btn)
                logger.info("  Clicked Next")
                return True
        except Exception:
            logger.debug("Next via XPath failed", exc_info=True)
        for sel in self.COMPOSER_MEDIA_NEXT_SELECTORS:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed() and btn.is_enabled():
                    hb.human_click(self.driver, btn)
                    logger.info("  Clicked Next (%s)", sel)
                    return True
            except Exception:
                continue
        # Shadow fallback, the one this method was missing. The media editor is
        # in a shadow root, so every light-DOM lookup above returns nothing and
        # the flow simply never advanced.
        if self._deep_click_button(self.COMPOSER_MEDIA_NEXT_TEXT):
            logger.info("  Clicked Next (shadow DOM)")
            return True
        return False

    def attach_image(self, image_path: str) -> bool:
        """Attach one image to the OPEN composer. True only if CONFIRMED.

        Returns False rather than raising, and never publishes anything. The
        caller decides what to do about a failure, and for an image that was
        explicitly asked for the only correct decision is to abort.
        """
        abs_path = os.path.abspath(image_path)
        if not os.path.isfile(abs_path):
            logger.error("Image not found: %s", abs_path)
            return False

        # Refuse to run twice against the same composer. Attaching to an input
        # that carries `multiple` APPENDS, so a second call would add a second
        # copy of the image rather than replacing the first.
        if getattr(self, "_image_attached", False):
            logger.error("attach_image called twice for one composer - refusing")
            return False

        # Swallow LinkedIn's own programmatic open of the native picker BEFORE
        # the media button is clicked, so send_keys is the only path to the file.
        suppressed = self._suppress_native_picker()
        if not suppressed:
            logger.warning("  Could not suppress the native picker; a double "
                           "attach is possible, so the file count is checked below")

        # The watchdog runs for the whole attach, not just the click: run B
        # showed the picker appearing AFTER send_keys had already delivered the
        # file, so arming it only around the click would miss the case that
        # actually happened.
        with NativeFileDialogWatchdog(timeout=self.MEDIA_EDITOR_TIMEOUT
                                      + self.MEDIA_ATTACH_TIMEOUT) as watchdog:
            return self._attach_image_inner(abs_path, watchdog)

    def _attach_image_inner(self, abs_path, watchdog):
        try:
            if not self._click_media_button():
                capture_failure(self.driver, "media_button_not_found",
                                self.profile_name)
                return False

            # Re-arm immediately: the media editor mounts its OWN shadow root,
            # which did not exist when the guard was first installed. The
            # document-level composedPath listener already covers it, but this
            # attaches the in-root listener too, and it is idempotent.
            self._suppress_native_picker(quiet=True)
            hb.human_sleep(0.8, 1.5)

            file_input = self._wait_for_file_input()
            if file_input is None:
                logger.error("The media editor never mounted a file input")
                capture_failure(self.driver, "media_file_input_missing",
                                self.profile_name)
                return False

            # If the watchdog had to force a dialog shut before we delivered,
            # LinkedIn's file-selector flow was CANCELLED, not merely
            # interrupted: WM_CLOSE is that dialog's Cancel. The 2026-09-05 run
            # proved the consequence - send_keys still reported success, but the
            # media editor never advanced, because the handler waiting on that
            # picker was gone. Delivering into a dead panel and then waiting 60s
            # for it to advance is a slow way to reach the same answer.
            if watchdog is not None and watchdog.closed:
                logger.error("A native file dialog was force-closed BEFORE the "
                             "image was delivered. That cancels LinkedIn's file "
                             "selector, so the attach cannot complete - the "
                             "picker guard did not prevent the dialog.")
                capture_failure(self.driver, "media_picker_forced_close",
                                self.profile_name)
                return False

            try:
                # send_keys to the input itself. Never CLICK it: clicking is what
                # opens the native picker, and the picker is the second delivery
                # path that caused the double upload.
                logger.info("  Delivering the image path (dialogs closed so "
                            "far: %d)", watchdog.closed if watchdog else 0)
                file_input.send_keys(abs_path)
                self._image_attached = True
                logger.info("  Sent the image path to the file input "
                            "(dialogs closed so far: %d)",
                            watchdog.closed if watchdog else 0)
            except Exception:
                logger.error("send_keys to the file input failed", exc_info=True)
                capture_failure(self.driver, "media_send_keys_failed",
                                self.profile_name)
                return False

            # Authoritative check, straight off the element: exactly one file.
            # Anything else means a second path delivered one too, and since the
            # input is `multiple` that is an APPEND, not a replace.
            try:
                n = self.driver.execute_script(self._FILE_COUNT_JS, file_input)
            except Exception:
                n = None
                logger.debug("could not read files.length", exc_info=True)
            if n is not None and n != 1:
                logger.error("Expected exactly 1 file on the input, found %s - "
                             "aborting rather than publishing a duplicate", n)
                capture_failure(self.driver, "media_double_attach",
                                self.profile_name)
                return False
        finally:
            self._restore_native_picker()

        # The advance, in the three phases the harvest actually shows.
        #
        #   stage 5   media editor mounted, image rendered inside it, "Next"
        #   [Next]
        #   stage 7   editor GONE, composer preview PRESENT, Post enabled
        #
        # The previous version collapsed this into one loop that asked
        # media_is_attached() first and only clicked Next afterwards. That is
        # not wrong by itself - the loop did retry Next every pass - but it made
        # the failure unreadable, and it hid the real defect: _click_media_next
        # had no shadow fallback, so it returned False forever and nothing ever
        # advanced. Splitting the phases means each one fails with its own
        # reason instead of one generic timeout.
        #
        # Every phase waits on a SIGNAL. The deadlines are only the point at
        # which we give up and fail closed.

        # Phase 1 - the image renders inside the media editor.
        deadline = time.time() + self.MEDIA_EDITOR_TIMEOUT
        loaded = False
        while time.time() < deadline:
            if self.media_is_attached():
                # Some flows have no Next step at all and land straight in the
                # composer. Already done: nothing left to advance.
                logger.info("  Image attached without a Next step")
                return True
            if self.media_loaded_in_editor():
                loaded = True
                break
            hb.human_sleep(0.3, 0.6)

        if not loaded:
            logger.error("The image never rendered in the media editor within %ss",
                         self.MEDIA_EDITOR_TIMEOUT)
            if watchdog is not None and watchdog.closed:
                logger.error("  A native file dialog was closed %d time(s) during "
                             "this attach; it blocks WebDriver while it is up.",
                             watchdog.closed)
            capture_failure(self.driver, "media_never_loaded_in_editor",
                            self.profile_name)
            return False

        logger.info("  Image is loaded in the media editor")

        # Phase 2 - commit it with Next. Retried, because the button can be
        # momentarily disabled while the editor settles.
        deadline = time.time() + self.MEDIA_EDITOR_TIMEOUT
        clicked_next = False
        while time.time() < deadline:
            if self.media_is_attached():
                clicked_next = True
                break
            if self._click_media_next():
                clicked_next = True
                break
            hb.human_sleep(0.3, 0.6)

        if not clicked_next:
            logger.error("Could not click Next to commit the media editor "
                         "within %ss", self.MEDIA_EDITOR_TIMEOUT)
            capture_failure(self.driver, "media_next_not_clicked",
                            self.profile_name)
            return False

        # Phase 3 - back in the composer, with the preview and an enabled Post.
        deadline = time.time() + self.MEDIA_ATTACH_TIMEOUT
        while time.time() < deadline:
            if self.media_is_attached():
                logger.info("  Image attached (preview present, media editor gone)")
                return True
            hb.human_sleep(0.4, 0.8)

        logger.error("Image attachment NOT confirmed within %ss after Next",
                     self.MEDIA_ATTACH_TIMEOUT)
        if watchdog is not None and watchdog.closed:
            logger.error("  A native file dialog appeared and was closed %d "
                         "time(s) during this attach. It blocks WebDriver while "
                         "it is up, which is very likely what consumed the wait.",
                         watchdog.closed)
        capture_failure(self.driver, "media_attach_unconfirmed", self.profile_name)
        return False

    def _click_post_button(self) -> bool:
        """Click the Post button to publish."""

        # Strategy 1: Regular DOM button with "Post" text
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buttons:
                try:
                    text = btn.text.strip()
                    if text.lower() == self.COMPOSER_POST_BUTTON_TEXT.lower():
                        if btn.is_displayed() and btn.is_enabled():
                            logger.info("  Found 'Post' button")
                            hb.human_click(self.driver, btn)
                            hb.human_sleep(2.0, 3.0)
                            return True
                except Exception:
                    continue
        except Exception:
            logger.debug("Strategy 1 (Post button text) failed", exc_info=True)

        # Strategy 2: Shadow DOM Post button
        try:
            clicked = self.driver.execute_script("""
                // Regular DOM
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var text = buttons[i].textContent.trim();
                    if (text === 'Post' && buttons[i].offsetParent !== null && !buttons[i].disabled) {
                        buttons[i].click();
                        return 'regular';
                    }
                }
                // Shadow DOM
                var hosts = document.querySelectorAll('*');
                for (var i = 0; i < hosts.length; i++) {
                    if (hosts[i].shadowRoot) {
                        var btns = hosts[i].shadowRoot.querySelectorAll('button');
                        for (var b = 0; b < btns.length; b++) {
                            var text = btns[b].textContent.trim();
                            if (text === 'Post' && btns[b].offsetParent !== null && !btns[b].disabled) {
                                btns[b].click();
                                return 'shadow';
                            }
                        }
                    }
                }
                return null;
            """)
            if clicked:
                logger.info(f"  Clicked Post button ({clicked} DOM)")
                hb.human_sleep(2.0, 3.0)
                return True
        except Exception:
            logger.debug("Strategy 2 (shadow DOM Post button) failed", exc_info=True)

        # Strategy 3: aria-label
        for sel in self.COMPOSER_POST_BUTTON_SELECTORS:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed() and btn.is_enabled():
                    hb.human_click(self.driver, btn)
                    hb.human_sleep(2.0, 3.0)
                    return True
            except Exception:
                continue

        logger.error("Could not find Post button")
        return False

    def run(self, text: str, image_path: str = None) -> bool:
        """Full flow: setup, navigate, post."""
        try:
            self.setup()
            self.navigate_to_feed()
            result = self.create_post(text, image_path=image_path)
            hb.human_sleep(2.0, 3.0)
            return result
        except Exception as e:
            logger.error(f"Error: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False
        finally:
            if self.driver:
                self.driver.quit()
                logger.info("Browser closed")


def main():
    """CLI entry point: publish a post to the LinkedIn feed."""
    parser = argparse.ArgumentParser(description='LinkedIn Post Creator')
    parser.add_argument('text', nargs='?', help='Post text (or use --file)')
    parser.add_argument('--file', type=str, help='Read post text from file')
    parser.add_argument('--profile', type=str, default=None, help='LinkedIn profile name')
    parser.add_argument('--debug', action='store_true', help='Debug mode')

    parser.add_argument("--image", default=None,
                        help="Path to one image to attach. If attachment cannot "
                             "be confirmed, NOTHING is published.")
    args = parser.parse_args()

    if args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            text = f.read().strip()
    elif args.text:
        text = args.text
    else:
        parser.error("Provide post text as argument or use --file")

    if not text:
        parser.error("Post text cannot be empty")

    if args.image and not os.path.isfile(args.image):
        parser.error("Image not found: %s" % args.image)

    poster = LinkedInPoster(profile_name=args.profile, debug=args.debug)
    success = poster.run(text, image_path=args.image)

    if success:
        print("\n✓ Post published!")
    else:
        print("\n✗ Failed to publish post")
        sys.exit(1)


if __name__ == "__main__":
    main()
