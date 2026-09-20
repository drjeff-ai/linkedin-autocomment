"""Post curated comments to LinkedIn posts via Selenium.

Parses a daily/curated comments TXT file (see ``comment_fields``), then for each
comment navigates to the post, likes it, and submits the comment. Failures on a
single comment are skipped-and-logged so the run continues. Exits 0 on success,
2 on login failure, 1 on other errors.
"""

import time
import json
import os
import re
import sys
import random
import traceback
from datetime import datetime
from typing import Dict, List, Optional
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from dotenv import load_dotenv
import logging
from . import profile_manager as pm
from . import human_behavior as hb
from .failure_capture import capture_failure, capture_submit_state

load_dotenv()


class LinkedInCommentPoster:
    """Post generated comments to LinkedIn posts."""

    # ─── Posting-path selectors ───────────────────────────────────────────────
    #
    # These were inline literals inside the methods below. That is exactly why
    # selector_health could not cover the posting path: its SELECTOR_REGISTRY is
    # built from class constants so it stays in sync with what the code actually
    # uses, and a literal buried in a method body is invisible to it. Scraping
    # selectors were hoisted onto LinkedInScraper long ago and were therefore
    # watched; these were not, so the posting path had no monitor at all.
    #
    # Keep the new-first / legacy-fallback ordering: a stale selector that is
    # tried first costs a full WebDriverWait timeout per post.

    # Selectors that prove a post permalink page has actually rendered.
    #
    # LinkedIn moved the permalink page to data-testid attributes. post_finder
    # was updated for that; this list was not, so all four legacy selectors
    # stopped matching and every post failed with "Post content not found on
    # page" after burning 4 x 20s of WebDriverWait — scraping kept working while
    # posting placed zero comments. Counts verified against a live permalink
    # page on 2026-07-30 (see .dev/AUDIT_student_fork.md):
    #
    #     span[data-testid='expandable-text-box'] -> 1     div.occludable-update      -> 0
    #     div[role='listitem']                    -> 1     div.feed-shared-update-v2  -> 0
    #                                                      article.feed-shared-article-> 0
    #                                                      div[data-urn*='activity']  -> 0
    #
    # Deliberately NOT using "main": it matches on every LinkedIn page including
    # error pages, so it would report a post had loaded when nothing had.
    POST_DETAIL_SELECTORS = [
        "span[data-testid='expandable-text-box']",
        "div[role='listitem']",
        "div.occludable-update",
        "div.feed-shared-update-v2",
        "article.feed-shared-article",
        "div[data-urn*='activity']",
    ]

    LIKE_BUTTON_SELECTORS = [
        "button[aria-label*='Like'][aria-pressed='false']",
        "button.react-button__trigger:not(.react-button__trigger--active)",
        "button[data-control-name='like_toggle']",
    ]

    LIKED_STATE_SELECTORS = [
        "button[aria-label*='Like'][aria-pressed='true']",
        "button.react-button__trigger--active",
    ]

    # The action-bar button that OPENS the comment box. Distinct from the submit
    # button below — see SUBMIT_BUTTON_XPATH for why that distinction matters.
    COMMENT_BUTTON_LABEL_SELECTORS = [
        "button[aria-label*='Comment']",
        "button[aria-label*='comment']",
    ]
    COMMENT_BUTTON_TEXT_SELECTOR = "span.artdeco-button__text"
    COMMENT_BUTTON_TEXT = "Comment"

    # The editor itself. Only present after the comment box has been opened.
    COMMENT_INPUT_SELECTORS = [
        "div.ql-editor[contenteditable='true']",
        "div.ql-editor.ql-blank",
        "div[role='textbox'][contenteditable='true']",
        "div[contenteditable='true'][data-placeholder*='comment']",
    ]

    # The SUBMIT button. LinkedIn labels it with the visible text "Comment" and
    # no aria-label, while the action-bar button that OPENS the box carries
    # aria-label="Comment" and shows the comment count as its text. Matching on
    # exact visible text and excluding that aria-label is what separates them.
    SUBMIT_BUTTON_XPATH = "//button[normalize-space(.)='Comment']"
    SUBMIT_BUTTON_EXCLUDED_ARIA_LABEL = "Comment"

    # Class-based submit fallbacks, kept because they still match on some
    # accounts. Fragile by nature: LinkedIn ships hashed class names that change
    # between deploys, which is why the text-based XPath above is primary.
    SUBMIT_BUTTON_FALLBACK_SELECTORS = [
        "button.comments-comment-box__submit-button--cr",
        "button.comments-comment-box__submit-button",
        "button.ml1.artdeco-button--primary",
        "button.artdeco-button.artdeco-button--1.artdeco-button--primary",
        "button[aria-label*='Post comment']",
    ]

    # Posted comments on the permalink page, used to verify a submit landed.
    POSTED_COMMENT_SELECTOR = "div.comments-comment-item"

    def __init__(self, profile_name=None):
        self.profile_name = profile_name
        self.profile = None  # Set during setup_driver
        
        self.driver = None
        self.wait = None
        
        # Resolve profile name for data dirs
        resolved_name = profile_name or pm.get_default_profile_name() or "default"

        # Setup directories (profile-specific)
        self.data_dir = pm.get_comments_dir(resolved_name)
        self.progress_file = pm.get_progress_file(resolved_name)
        self.screenshots_dir = pm.get_screenshots_dir(resolved_name)

        # Apply tunable human-behavior timing (typing speed, reading time, scroll/
        # break cadence) from the profile config's "behavior" section. Posting
        # comments is the highest bot-detection risk, so pacing matters most here.
        self.config = pm.get_profile_config(resolved_name)
        hb.configure_behavior(self.config.get("behavior"))
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        # Load progress
        self.progress = self.load_progress()
    
    def load_progress(self) -> Dict:
        """Load posting progress from file."""
        if os.path.exists(self.progress_file):
            with open(self.progress_file, 'r') as f:
                return json.load(f)
        return {"posted_comments": []}
    
    def save_progress(self):
        """Save posting progress to file."""
        with open(self.progress_file, 'w') as f:
            json.dump(self.progress, f, indent=2)
    
    def setup_driver(self):
        """Initialize Chrome driver with persistent session via profile manager."""
        self.logger.info("Setting up browser with persistent session...")
        self.driver, self.profile = pm.create_driver(self.profile_name)
        self.wait = WebDriverWait(self.driver, 20)
        self.logger.info("Browser started successfully")
    
    def login(self) -> bool:
        """Login to LinkedIn using profile manager (checks persistent session first)."""
        return pm.login(self.driver, self.profile)
    
    @staticmethod
    def parse_comments_file(file_path: str) -> List[Dict]:
        """Parse the daily/curated comments text file into a list of dicts.

        Pure function (no driver/session needed). Accepts the canonical TXT
        format produced by ``comment_fields.comments_to_txt`` regardless of which
        field-naming convention the upstream comments used — the field mapping is
        resolved at write time, so every block here carries a real ``URL:`` line.
        """
        comments = []

        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Split by post separator
        posts = content.split('------------------------------------------------------------')

        for post in posts:
            if 'URL:' not in post or 'Your Comment' not in post:
                continue

            # Extract URL (accept http or https)
            url_match = re.search(r'URL: (https?://[^\n]+)', post)
            if not url_match:
                continue
            
            url = url_match.group(1).strip()
            
            # Extract comment. The block was already split off the separator, so
            # the comment's closing quote(s) are the last quote-run in the chunk,
            # followed only by whitespace. Anchoring the closing "+ to end-of-chunk
            # lets the comment contain its own double quotes while still stripping
            # both "text" and ""text"" wrappers.
            comment_match = re.search(
                r'Your Comment \(\d+ words\):\n"+(.+?)"+\s*$', post, re.DOTALL
            )
            
            if not comment_match:
                continue
            
            comment_text = comment_match.group(1).strip()
            
            # Extract post preview
            preview_match = re.search(r'Post Preview:\n([^\n]+)', post)
            preview = preview_match.group(1) if preview_match else "No preview"
            
            comments.append({
                'url': url,
                'comment': comment_text,
                'preview': preview
            })
        
        return comments
    
    def navigate_to_post(self, url: str) -> bool:
        """Navigate to a LinkedIn post with improved waiting."""
        try:
            self.logger.info(f"Navigating to post: {url}")
            self.driver.get(url)

            # Wait for the page to fully load (variable, not a flat 3s)
            hb.human_sleep(2.5, 4.0)

            # Wait for post content to be visible
            post_selectors = self.POST_DETAIL_SELECTORS

            post_found = False
            for selector in post_selectors:
                try:
                    self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                    post_found = True
                    self.logger.info(f"Post loaded with selector: {selector}")
                    break
                except TimeoutException:
                    continue
            
            if not post_found:
                self.logger.error("Post content not found on page")
                return False
            
            # Additional wait for dynamic content
            hb.human_sleep(1.5, 2.5)

            # Read the post like a human would before engaging, then a gentle
            # scroll down to take it in and back up to the action bar (variable
            # increments + drift instead of a uniform jump to the half-point).
            hb.simulate_reading(self.driver)
            hb.human_scroll(self.driver, direction="down", pixels=random.randint(300, 600))
            hb.human_sleep(0.8, 1.6)
            hb.human_scroll(self.driver, direction="up", pixels=random.randint(300, 600))
            hb.human_sleep(0.6, 1.2)

            # Check if we're on the right page
            if "feed/update" in self.driver.current_url or "posts" in self.driver.current_url:
                self.logger.info("Successfully navigated to post")
                return True
            
            self.logger.error("Failed to navigate to post - wrong URL")
            return False
            
        except Exception as e:
            self.logger.error(f"Navigation error: {e}")
            return False
    
    def like_post(self) -> bool:
        """Like the current post."""
        try:
            # Find the like button - multiple possible selectors
            like_selectors = self.LIKE_BUTTON_SELECTORS

            like_button = None
            for selector in like_selectors:
                try:
                    like_button = self.wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                    break
                except Exception:
                    continue

            if not like_button:
                # Check if already liked
                liked_selectors = self.LIKED_STATE_SELECTORS

                for selector in liked_selectors:
                    if self.driver.find_elements(By.CSS_SELECTOR, selector):
                        self.logger.info("Post already liked")
                        return True
                
                self.logger.warning("Like button not found")
                return False
            
            # Click like button with a natural mouse approach + click.
            hb.human_click(self.driver, like_button)
            hb.human_sleep(1.2, 2.4)

            self.logger.info("✅ Post liked successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error liking post: {e}")
            return False
    
    def open_comment_box(self) -> Optional:
        """Open the comment box and return the input element."""
        self.logger.info("Opening comment box...")
        
        # Find comment button - look for button with "Comment" text
        comment_button = None
        
        # Method 1: Find by span text
        try:
            spans = self.driver.find_elements(
                By.CSS_SELECTOR, self.COMMENT_BUTTON_TEXT_SELECTOR)
            for span in spans:
                if span.text.strip() == self.COMMENT_BUTTON_TEXT:
                    # Get the parent button
                    comment_button = span.find_element(By.XPATH, "./ancestor::button")
                    break
        except Exception:
            self.logger.debug("Comment-button span lookup failed; trying aria-label", exc_info=True)

        # Method 2: Try aria-label
        if not comment_button:
            comment_button_selectors = self.COMMENT_BUTTON_LABEL_SELECTORS

            for selector in comment_button_selectors:
                try:
                    buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for btn in buttons:
                        if btn.is_displayed():
                            comment_button = btn
                            break
                    if comment_button:
                        break
                except Exception:
                    continue
        
        if comment_button:
            hb.scroll_to_element(self.driver, comment_button)
            hb.human_click(self.driver, comment_button)
            hb.human_sleep(2.5, 3.5)  # Give time for comment box to fully load
            self.logger.info("Clicked comment button")
        else:
            self.logger.info("Comment button not found, trying to find comment box directly")
        
        # Find the comment input field
        comment_input_selectors = self.COMMENT_INPUT_SELECTORS

        comment_input = None
        for selector in comment_input_selectors:
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            for elem in elements:
                if elem.is_displayed():
                    comment_input = elem
                    self.logger.info(f"Found comment input with selector: {selector}")
                    break
            if comment_input:
                break
        
        return comment_input
    
    def comment_thread_snapshot(self):
        """(count, texts) of the comments currently rendered on this post.

        Taken BEFORE submitting so "did a new comment appear" is answerable.
        Without a before-count the only available signal is "the box changed",
        which a FAILED submit also produces.
        """
        try:
            found = self.driver.find_elements(
                By.CSS_SELECTOR, self.POSTED_COMMENT_SELECTOR)
        except Exception:
            return (None, [])
        texts = []
        for el in found:
            try:
                texts.append((el.text or "").strip())
            except Exception:
                texts.append("")
        return (len(found), texts)

    def verify_comment_posted(self, comment_input, comment_text, before=None):
        """Did the comment ACTUALLY land? Positive proof only.

        THIS IS THE GUARD THAT FAILED IN PRODUCTION. It used to return True on:

          * any change to the box's text - including a box a failed submit had
            simply cleared; and
          * a StaleElementReferenceException, logged as "likely posted", which
            is the opposite of proof: a stale handle means the DOM moved, and
            says nothing about whether anything was sent.

        Those two paths made the tool type a comment, fail to post it, and
        record it as done. The ledger (`posting_progress.json`) is what the
        store reconciles COMMENTED from, so the post was then marked done
        forever and never retried.

        Now nothing counts as posted unless the comment is VISIBLE IN THE
        THREAD, or the thread grew by one AND the box emptied. Ambiguity is a
        failure. A false negative costs a retry; a false positive loses the
        comment silently and permanently.
        """
        try:
            hb.human_sleep(1.5, 2.5)
            after_count, after_texts = self.comment_thread_snapshot()
            needle = (comment_text or "").strip()[:50]

            # Strongest proof: our text is rendered in the thread.
            if needle:
                for text in after_texts:
                    if needle in text:
                        self.logger.info("✅ Verified: the comment is in the thread")
                        return True

            # Weaker but still POSITIVE: the thread grew and the box emptied.
            # Both halves are required - a cleared box alone is exactly what a
            # failed submit leaves behind.
            before_count = (before or (None, []))[0]
            if before_count is not None and after_count is not None \
                    and after_count > before_count:
                if self._comment_box_is_empty(comment_input):
                    self.logger.info(
                        "✅ Verified: thread grew %d -> %d and the box cleared",
                        before_count, after_count)
                    return True
                self.logger.warning(
                    "Thread grew %d -> %d but the box still holds text - "
                    "not treating that as posted",
                    before_count, after_count)

            self.logger.warning(
                "❌ NOT verified as posted (thread %s -> %s)",
                before_count, after_count)
            return False

        except Exception as e:
            # An error while verifying is NOT a pass. It is the same unknown
            # the old code resolved in favour of success.
            self.logger.error(f"Verification error (treated as NOT posted): {e}")
            return False

    def _comment_box_is_empty(self, comment_input):
        """True only if the box is readable AND empty.

        A stale or unreadable box returns False: "I cannot tell" must not read
        as "it cleared, so it sent".
        """
        try:
            return not (comment_input.text or "").strip()
        except Exception:
            self.logger.debug("comment box unreadable during verification",
                              exc_info=True)
            return False

    # How long to wait for LinkedIn's editor to enable the submit button after
    # the text is entered. React re-renders on its own schedule; clicking
    # before it does is a no-op that raises nothing.
    SUBMIT_ENABLE_TIMEOUT = 8.0
    SUBMIT_ENABLE_POLL = 0.25

    def notify_editor_of_input(self, comment_input):
        """Nudge the editor's framework state after typing.

        `type_like_human` enters text with real per-character `send_keys`
        (human_behavior.py:319), so genuine key events already fire and a
        well-behaved editor has already updated. This dispatches an explicit
        `input` event anyway, because the cost is one JS call and the failure it
        guards against - a submit button that never leaves `disabled` because
        the framework never saw the text - is silent, and produces exactly the
        reported symptom: a comment visibly typed and never sent.

        Best-effort: a failure here is not a failure to comment, and the
        enabled-wait below is what actually decides.
        """
        try:
            self.driver.execute_script(
                """
                const el = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                """, comment_input)
        except Exception:
            self.logger.debug("could not dispatch an input event on the editor",
                              exc_info=True)

    def submit_button_candidates(self):
        """Every plausible comment-submit control, in priority order.

        The text-based XPath is FIRST because it is the one the class comments
        call primary: LinkedIn ships hashed class names that change between
        deploys, so the class selectors below it are fallbacks that may match
        nothing on a given day.

        The action-bar button that OPENS the comment box also reads "Comment",
        which is why `SUBMIT_BUTTON_EXCLUDED_ARIA_LABEL` is filtered out - that
        one is always enabled, so a check that accepted it would happily click
        the wrong control forever.
        """
        found = []
        try:
            for el in self.driver.find_elements(By.XPATH, self.SUBMIT_BUTTON_XPATH):
                found.append(el)
        except Exception:
            self.logger.debug("submit XPath failed", exc_info=True)
        for selector in self.SUBMIT_BUTTON_FALLBACK_SELECTORS:
            try:
                found.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                self.logger.debug("submit selector %s failed", selector,
                                  exc_info=True)

        out = []
        for el in found:
            try:
                if (el.get_attribute("aria-label") or "").strip() == \
                        self.SUBMIT_BUTTON_EXCLUDED_ARIA_LABEL:
                    continue          # the button that OPENS the box
                if not el.is_displayed():
                    continue
            except Exception:
                continue
            out.append(el)
        return out

    @staticmethod
    def submit_button_is_enabled(button):
        """Is this control actually clickable, by every signal it exposes?

        LinkedIn disables the submit in more than one way, and Selenium's
        `is_enabled()` only reads the `disabled` property. A button carrying
        `aria-disabled="true"` or the artdeco disabled class reads as ENABLED to
        `is_enabled()` while doing nothing when clicked - which is the no-op
        click this whole dispatch is about.
        """
        try:
            if not button.is_enabled():
                return False
            if (button.get_attribute("disabled") or "") not in ("", "false", None):
                return False
            if (button.get_attribute("aria-disabled") or "").lower() == "true":
                return False
            classes = button.get_attribute("class") or ""
            if "artdeco-button--disabled" in classes or "disabled" in classes.split():
                return False
        except Exception:
            return False
        return True

    def await_enabled_submit(self, timeout=None):
        """Wait for a submit control to become ENABLED. Returns it, or None.

        This is the fix. The old primary path took
        SUBMIT_BUTTON_FALLBACK_SELECTORS[0] and clicked it immediately with no
        enabled check at all (comment_poster.py:475-477 before this change), so
        a button LinkedIn had not yet enabled was clicked, nothing happened, and
        nothing raised.
        """
        timeout = self.SUBMIT_ENABLE_TIMEOUT if timeout is None else timeout
        deadline = time.time() + timeout
        seen_any = False
        while True:
            for button in self.submit_button_candidates():
                seen_any = True
                if self.submit_button_is_enabled(button):
                    return button
            if time.time() >= deadline:
                break
            time.sleep(self.SUBMIT_ENABLE_POLL)

        if seen_any:
            self.logger.error(
                "The comment submit button never became enabled within %.1fs - "
                "LinkedIn did not accept the typed text as input.", timeout)
        else:
            self.logger.error(
                "No comment submit control was found on the page at all.")
        return None

    def post_comment_ctrl_enter(self, comment_input, comment_text, before=None):
        """Keyboard submit, used as the fallback after a click that did nothing."""
        self.logger.info("Falling back to the Ctrl+Enter keyboard submit...")
        try:
            hb.human_sleep(0.4, 1.0)
            comment_input.send_keys(Keys.CONTROL + Keys.ENTER)
            hb.human_sleep(2.5, 3.5)
            if self.verify_comment_posted(comment_input, comment_text, before):
                self.logger.info("Comment posted via Ctrl+Enter")
                return True
        except Exception as e:
            self.logger.debug(f"Ctrl+Enter failed: {e}")
        return False

    def capture_submit_failure(self, comment_text, before, reason):
        """Everything a human needs to fix this, written once per failure.

        Screenshot and page for context, plus the narrow answer: every
        candidate submit and comment-box control with its aria-label and
        disabled state. `reason` separates the two failures that look identical
        in a log and are not the same problem at all:

          submit_button_never_enabled - the text never registered with the
              editor, so LinkedIn never enabled the control. Nothing was ever
              clickable.
          comment_not_posted - an ENABLED button was clicked, and the keyboard
              fallback was tried, and the comment still did not appear.
        """
        capture_failure(self.driver, reason, self.profile_name)
        buttons = []
        for button in self.submit_button_candidates():
            try:
                buttons.append({
                    "text": (button.text or "").strip()[:80],
                    "aria_label": button.get_attribute("aria-label"),
                    "disabled": button.get_attribute("disabled"),
                    "aria_disabled": button.get_attribute("aria-disabled"),
                    "class": (button.get_attribute("class") or "")[:200],
                    "enabled_by_our_check": self.submit_button_is_enabled(button),
                })
            except Exception:
                buttons.append({"error": "went stale while reading"})

        self.last_failure_evidence = capture_submit_state(
            self.driver, reason, self.profile_name,
            extra={"reason": reason,
                   "comment_length": len(comment_text or ""),
                   "thread_before": (before or (None, []))[0],
                   "thread_after": self.comment_thread_snapshot()[0],
                   "submit_candidates_seen": buttons},
            submit_selectors=([self.SUBMIT_BUTTON_XPATH]
                              + list(self.SUBMIT_BUTTON_FALLBACK_SELECTORS)),
            box_selectors=list(self.COMMENT_INPUT_SELECTORS))
        self.logger.error(
            "COMMENT NOT POSTED (%s): typed %d chars, nothing published.",
            reason, len(comment_text or ""))
        return self.last_failure_evidence

    def post_comment(self, comment_text: str) -> bool:
        """Post a comment on the current post using all available methods."""
        try:
            # Open comment box
            comment_input = self.open_comment_box()
            
            if not comment_input:
                self.logger.error("Comment input field not found")
                capture_failure(self.driver, "comment_box_not_found", self.profile_name)
                return False
            
            # Click to focus and type comment. Scroll the box into view with
            # human-like smoothness, then a natural mouse approach + click to
            # focus it (instead of a teleport click).
            hb.scroll_to_element(self.driver, comment_input)

            # Variable pause between reading the post and starting to type — a
            # human composes for a beat before the first keystroke.
            hb.human_sleep(0.8, 2.0)

            # Clear, then type character-by-character with human-like timing
            # (random 40-120ms keystrokes + occasional thinking pauses + mouse
            # drift) via human_behavior_selenium, instead of dumping all at once.
            hb.human_click(self.driver, comment_input)
            hb.human_sleep(0.3, 0.7)
            comment_input.clear()
            self.logger.info("Cleared comment input field")
            try:
                hb.type_like_human(self.driver, comment_input, comment_text)
            except Exception as e:
                # Fall back to plain typing if the human-typing helper fails
                # (e.g. mouse-move issues), so a comment is still entered.
                self.logger.warning(f"Human typing failed ({e}); falling back to send_keys")
                comment_input.send_keys(comment_text)
            self.logger.info(f"Typed comment ({len(comment_text)} chars) with human-like timing")
            # Variable pause to "review" between typing and submitting.
            hb.human_sleep(1.0, 2.5)
            
            # The thread as it stands BEFORE any submit. Verification compares
            # against this: without it, "the box changed" is the only signal,
            # and a failed submit changes the box too.
            before = self.comment_thread_snapshot()

            # STEP 1 - make sure the editor's framework state saw the text. The
            # submit button stays disabled until it does, and a click on a
            # disabled button is a silent no-op.
            self.notify_editor_of_input(comment_input)

            # STEP 2 - wait for an ENABLED submit control, then click THAT.
            #
            # This is the fix. The old primary path took
            # SUBMIT_BUTTON_FALLBACK_SELECTORS[0] and clicked it immediately
            # with no enabled check at all, so a button LinkedIn had not yet
            # enabled was clicked, nothing happened, and nothing raised.
            button = self.await_enabled_submit()
            if button is None:
                # Loud, captured, and NOT a skip. The capture records every
                # candidate control with its aria-label and disabled state,
                # which is what says whether the button was absent or merely
                # never enabled.
                self.capture_submit_failure(comment_text, before,
                                            "submit_button_never_enabled")
                return False

            try:
                self.logger.info("Clicking the enabled submit button: %r",
                                 (button.text or "").strip() or "<no text>")
                hb.human_click(self.driver, button)
            except Exception as exc:
                self.logger.warning("The submit click raised: %s", exc)
            hb.human_sleep(2.5, 3.5)

            if self.verify_comment_posted(comment_input, comment_text, before):
                self.logger.info("Comment posted and verified in the thread")
                return True

            # STEP 3 - the click landed on an ENABLED button and still nothing
            # posted. Try the keyboard submit ONCE, then re-verify.
            if self.post_comment_ctrl_enter(comment_input, comment_text, before):
                return True

            self.capture_submit_failure(comment_text, before,
                                        "comment_not_posted")
            return False
            
        except Exception as e:
            self.logger.error(f"Error posting comment: {e}")
            traceback.print_exc()
            return False
    
    def post_single_comment(self, comment_data: Dict, force: bool = False) -> bool:
        """Post a single comment to LinkedIn."""
        url = comment_data['url']
        comment_text = comment_data['comment']
        
        # Check if already posted (skip check if force=True)
        if not force and url in self.progress.get('posted_comments', []):
            self.logger.info(f"Comment already posted for: {url}")
            return True
        
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"Processing comment for post: {comment_data['preview'][:80]}...")
        self.logger.info(f"Comment: {comment_text[:100]}...")
        
        # Navigate to post
        if not self.navigate_to_post(url):
            return False

        # Read the post before engaging, scaled to the comment we intend to leave
        # (longer/considered comments imply a longer read of the post).
        hb.simulate_reading_for_text(self.driver, comment_text)

        # Like the post
        if not self.like_post():
            self.logger.warning("Failed to like post, continuing anyway...")

        # Variable pause between liking and opening the comment box.
        hb.human_sleep(0.8, 2.2)

        # Post the comment. A False here means NOT POSTED - it must never
        # reach the ledger below, which is what post_store reconciles COMMENTED
        # from. Marking an unposted comment done loses it permanently: the URL
        # is skipped on every future run.
        if not self.post_comment(comment_text):
            self.record_comment_failure(url, "comment_not_posted")
            return False

        # Mark as completed - reached ONLY when the comment was verified in the
        # thread.
        self.progress['posted_comments'].append(url)
        self.progress['last_posted'] = datetime.now().isoformat()
        self.save_progress()
        
        self.logger.info("✅ Successfully engaged with post!")

        # Wait before next action (variable, not a flat 5s)
        hb.human_sleep(4.0, 7.0)

        return True
    

    def _report_run(self, posted, failed, skipped, attempted, total):
        """The run summary. A batch that posted nothing must be unmistakable.

        The old line was `Done. Posted N, skipped M` at INFO with a tick, which
        read as success whatever N was - and "skipped" quietly absorbed posts
        that had been typed and lost.
        """
        result = {"posted": posted, "failed": failed, "skipped": skipped,
                  "attempted": attempted, "total": total}
        if failed:
            self.logger.error("")
            self.logger.error("!" * 68)
            self.logger.error(
                "  %d of %d comments FAILED to post - see data/%s/failures/",
                failed, attempted, self.profile_name or "<profile>")
            if not posted:
                self.logger.error(
                    "  NOTHING WAS POSTED THIS RUN. Do not read this as success.")
            self.logger.error(
                "  Failed posts were NOT marked commented and will be retried.")
            self.logger.error("!" * 68)
            self.logger.error("")
        elif posted:
            self.logger.info("Done. Posted %d of %d attempted (%d parsed, %d skipped).",
                             posted, attempted, total, skipped)
        else:
            self.logger.warning(
                "Nothing was posted: 0 of %d parsed (%d skipped, 0 attempted).",
                total, skipped)
        return result

    def record_comment_failure(self, url, reason):
        """Record a post whose comment did NOT go out, loudly and durably.

        Written beside the posted ledger rather than into it. `posted_comments`
        is the authoritative record of what actually published and nothing
        unverified belongs there; this is the parallel record of what did not,
        so a failure survives the run and can be retried rather than being a
        line in a log nobody reads.
        """
        entry = {
            "url": url,
            "reason": reason,
            "at": datetime.now().isoformat(),
            "evidence": getattr(self, "last_failure_evidence", None),
        }
        self.progress.setdefault("failed_comments", []).append(entry)
        try:
            self.save_progress()
        except Exception:
            self.logger.debug("could not persist the failure record",
                              exc_info=True)
        self.logger.error("=" * 68)
        self.logger.error("COMMENT FAILED TO POST (%s)", reason)
        self.logger.error("  post: %s", url)
        if entry["evidence"]:
            self.logger.error("  evidence: %s", entry["evidence"])
        self.logger.error("  this post was NOT marked commented and will be "
                          "retried on the next run")
        self.logger.error("=" * 68)
        self.last_failure_evidence = None
        return entry

    def run(self, comments_file: str, post_count: int = 1, manual_mode: bool = False):
        """Run the comment posting process."""
        # Parse comments
        comments = self.parse_comments_file(comments_file)
        
        if not comments:
            self.logger.error("No comments found in file")
            return
        
        self.logger.info(f"Found {len(comments)} comments to post")
        
        # Setup and login
        self.setup_driver()
        if not self.login():
            if self.driver:
                self.driver.quit()
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile "
                f"'{self.profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile "
                f"{self.profile_name or 'default'}"
            )
        
        try:
            # Post comments. Each comment is isolated: a failure on one is
            # logged as a skip and the run continues with the rest (skip-and-log,
            # not abort-the-whole-run).
            posted = 0
            skipped = 0
            # A FAILURE is not a skip. A skip is "we chose not to try"; a
            # failure is "we tried, typed a comment, and nothing published".
            # Counting them together is how a run that posted nothing read as
            # a quiet success.
            failed = 0
            attempted = 0
            total = len(comments)

            # Non-uniform session shape: take a longer break after a re-rolled
            # number of comments so the work-then-pause rhythm is never identical
            # between runs (mirrors the connector's natural break pattern).
            posts_since_break = 0
            break_threshold = hb.random_break_threshold()

            for i, comment in enumerate(comments):
                label = f"{i+1}/{total}"

                if posted >= post_count:
                    break

                url = comment.get('url', '')
                if not url or not url.lower().startswith('http'):
                    self.logger.warning(f"Skipping comment {label}: missing or invalid URL ({url!r})")
                    skipped += 1
                    continue

                if url in self.progress.get('posted_comments', []):
                    self.logger.info(f"Skipping comment {label}: already posted")
                    continue

                try:
                    if manual_mode:
                        self.logger.info("\n" + "="*60)
                        self.logger.info("MANUAL MODE - Please complete the following steps:")
                        self.logger.info(f"1. Navigate to: {comment['url']}")
                        self.logger.info("2. Like the post")
                        self.logger.info("3. Click the comment button")
                        self.logger.info(f"4. Type: {comment['comment'][:100]}...")
                        self.logger.info("5. Click the Post/Submit button")
                        input("\nPress Enter when you've completed these steps...")

                        # Mark as completed
                        self.progress['posted_comments'].append(comment['url'])
                        self.progress['last_posted'] = datetime.now().isoformat()
                        self.save_progress()
                        posted += 1

                    elif self.post_single_comment(comment):
                        attempted += 1
                        posted += 1
                    else:
                        attempted += 1
                        failed += 1
                        self.logger.error(
                            "❌ %s FAILED to post - see data/<profile>/failures/",
                            label)
                        continue

                except Exception as e:
                    # One comment failing must not stop the others - but it is
                    # a FAILURE, not a skip, and the run must say so.
                    attempted += 1
                    failed += 1
                    self.logger.error("❌ %s FAILED to post: %s", label, e,
                                      exc_info=True)
                    try:
                        self.record_comment_failure(url, "exception")
                    except Exception:
                        pass
                    continue

                # Wait between successful posts to avoid rate limiting. Vary the
                # gap widely (never a constant 30s) so the cadence isn't uniform.
                if posted < post_count and i < total - 1:
                    wait_time = hb.random_delay(22.0, 48.0)
                    self.logger.info(f"Waiting {wait_time:.0f}s before next post...")
                    time.sleep(wait_time)

                    # Longer break after a re-rolled number of comments.
                    posts_since_break += 1
                    if posts_since_break >= break_threshold:
                        self.logger.info(
                            f"Taking a natural break after {posts_since_break} comments...")
                        hb.take_break(self.driver)
                        posts_since_break = 0
                        break_threshold = hb.random_break_threshold()

            return self._report_run(posted, failed, skipped, attempted, total)

        finally:
            if self.driver:
                if not manual_mode:
                    self.driver.quit()
                    self.logger.info("Browser closed")
                else:
                    self.logger.info("Browser left open for manual inspection")
                    input("Press Enter to close browser...")
                    self.driver.quit()


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Post generated comments to LinkedIn')
    parser.add_argument('comments_file', nargs='?', help='Path to the daily comments file')
    parser.add_argument('--count', type=int, default=1, help='Number of comments to post (default: 1)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--manual', action='store_true', help='Manual mode - shows instructions instead of automating')
    parser.add_argument('--test-url', help='Test with a specific LinkedIn post URL')
    parser.add_argument('--test-comment', default='This is a test comment.', help='Comment text for test mode')
    parser.add_argument('--profile', type=str, default=None, help='LinkedIn profile name (uses default if omitted)')
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    poster = LinkedInCommentPoster(profile_name=args.profile)
    
    # Test mode
    if args.test_url:
        print(f"Running in test mode with URL: {args.test_url}")
        test_comment = {
            'url': args.test_url,
            'comment': args.test_comment,
            'preview': 'Test mode'
        }
        
        poster.setup_driver()
        if poster.login():
            success = poster.post_single_comment(test_comment, force=True)  # Force posting in test mode
            print(f"Test result: {'Success' if success else 'Failed'}")
            input("\nPress Enter to close browser...")
            poster.driver.quit()
            sys.exit(pm.EXIT_OK if success else pm.EXIT_ERROR)
        poster.driver.quit()
        sys.exit(pm.EXIT_LOGIN_REQUIRED)

    # Normal mode
    if not args.comments_file:
        parser.error("comments_file is required unless using --test-url")
        return

    if not os.path.exists(args.comments_file):
        print(f"Error: Comments file not found: {args.comments_file}")
        sys.exit(pm.EXIT_ERROR)

    try:
        poster.run(args.comments_file, args.count, manual_mode=args.manual)
        sys.exit(pm.EXIT_OK)
    except pm.LoginRequiredError as e:
        print(f"\n❌ {e}")
        sys.exit(pm.EXIT_LOGIN_REQUIRED)
    except Exception as e:
        print(f"\n❌ {e}")
        sys.exit(pm.EXIT_ERROR)


if __name__ == "__main__":
    main()