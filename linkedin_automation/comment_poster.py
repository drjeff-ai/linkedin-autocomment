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
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from dotenv import load_dotenv
import logging
from . import profile_manager as pm
from . import human_behavior as hb
from . import post_urn
from . import run_log
from .failure_capture import (capture_failure, capture_like_state,
                              capture_submit_state)

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

    # The post's Like control, read off the 2026-09-20 captures. It is a plain
    # button carrying its state in the aria-label and NOTHING else - no
    # aria-pressed, no data-testid, hashed classes:
    #
    #     <button type="button" aria-label="Reaction button state: Like">Like</button>
    #
    # Exactly one per page. Do NOT loosen this to `[aria-label*='Like']`: the
    # same action bar carries six `aria-label="Open reactions menu"` buttons
    # (the hover reaction pickers), and clicking one of those opens a menu
    # instead of liking.
    #
    # The three selectors below it are the previous generation. All three match
    # ZERO on the current DOM - they are kept as fallbacks per MAINTENANCE.md
    # step 4, which is only affordable because the lookup is now bounded in
    # TOTAL rather than per selector.
    LIKE_BUTTON_SELECTORS = [
        "button[aria-label='Reaction button state: Like']",
        "button[aria-label*='Like'][aria-pressed='false']",
        "button.react-button__trigger:not(.react-button__trigger--active)",
        "button[data-control-name='like_toggle']",
    ]

    # Already reacted. The same control reports a DIFFERENT state in its
    # aria-label once a reaction is applied ("... : Liked", "... : Celebrate",
    # and so on), so anything in that family which is not the plain "Like"
    # state means the post has already been reacted to.
    #
    # No capture of a liked post exists yet, so this is inference from the
    # unliked shape rather than an observation - it is a non-critical path
    # (worst case we try to like an already-liked post and the click no-ops),
    # and it is marked so nobody reads it as verified.
    LIKED_STATE_SELECTORS = [
        "button[aria-label^='Reaction button state:']"
        ":not([aria-label='Reaction button state: Like'])",
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
    #
    # THE CLASS-BASED ONE IS DEAD ON THE CURRENT DOM. Both 2026-09-20 captures
    # match it ZERO times, and neither page contains a single class token with
    # "comment" in it - LinkedIn's tiptap-era markup is hashed classes only.
    # Left in place because it still matches on older accounts, and because a
    # verifier that can only ever return False is worse than one with a stale
    # branch: it turns every posted comment into a reported failure.
    POSTED_COMMENT_SELECTOR = "div.comments-comment-item"

    # The live hook. The comment list carries a data-testid ending in
    # "-commentList" (the prefix is per-post), and its children are the
    # rendered comments plus some chrome. Counting children is a GROWTH signal
    # only; the text match against the list is the real proof.
    POSTED_COMMENT_CONTAINER_SELECTOR = "[data-testid*='commentList']"

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

        # Timing (Dispatch 15.1). _timing is the comment in flight, if any;
        # run_timing is the run in flight, if any. Both None outside run(),
        # which keeps the primitives usable on their own (first_comment.py).
        self._timing = None
        self.run_timing = None
        self.last_comment_timing = None
        self.like_misses = 0

    # ─── Timing (Dispatch 15.1) ──────────────────────────────────────────────

    @contextmanager
    def _step(self, name):
        """Time a step of the comment in flight. A no-op outside one."""
        # getattr: some callers build the poster without __init__.
        timing = getattr(self, "_timing", None)
        if timing is None:
            yield
            return
        with timing.step(name):
            yield

    @contextmanager
    def _run_step(self, name):
        """Time a run-level step (setup, the wait between posts, a break)."""
        timing = getattr(self, "run_timing", None)
        if timing is None:
            yield
            return
        with timing.step(name):
            yield

    def _poll_done(self, name, declared, started, outcome):
        """Log a bounded poll's budget and its actual elapsed, separately.

        Measured on time.time(), the clock the poll loops set their deadlines
        on. Mixing in time.monotonic() let an exhausted poll read a hair UNDER
        its budget on Windows, where the two clocks tick at different
        resolutions - an overrun reported as no overrun.
        """
        actual = time.time() - started
        timing = getattr(self, "_timing", None)
        if timing is not None:
            timing.poll(name, declared, actual, outcome)
        else:
            run_log.log_poll("-", name, declared, actual, outcome)

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
    
    #: Outcomes of a navigation attempt, reported on ``last_navigation``.
    NAV_OK = "ok"                     #: the post is there
    NAV_UNAVAILABLE = "unavailable"   #: the post is GONE - terminal
    NAV_UNCLEAR = "unclear"           #: we do not know. Retryable, never terminal.
    #: The page did not finish loading within pm.PAGE_LOAD_TIMEOUT_SECONDS.
    #: Retryable, never terminal: a slow load is not a taken-down post, and
    #: UNAVAILABLE is terminal and unreviewed (MAINTENANCE §7.2).
    NAV_TIMEOUT = "timeout"

    #: TOTAL budget for deciding whether the post is there, across every
    #: selector and every signal.
    #:
    #: It used to be 6 selectors x a 20-second WebDriverWait = TWO MINUTES on a
    #: deleted post, every run, forever, because nothing ever marked the post
    #: gone. The budget is shared now, so a dead post costs seconds and that
    #: cost does not grow when a selector is added.
    NAV_DECIDE_SECONDS = 8.0
    NAV_POLL_SECONDS = 0.25

    #: Pages that mean "your session is the problem", NOT "the post is gone".
    #: Being bounced to a login wall must never mark a post terminal - that
    #: would burn the entire queue on one expired cookie.
    NAV_AUTH_URL_MARKERS = ("/login", "/checkpoint", "/authwall", "/uas/",
                            "/signup")

    #: Explicit "this post is gone" markers.
    #:
    #: UNVERIFIED. No capture of a taken-down post exists yet, so these are
    #: LinkedIn's documented empty-state shapes rather than anything observed
    #: on this account - do not read them as confirmed the way the Like
    #: selector is. They are only ever consulted when no post content was
    #: found at all, which is what keeps a wrong guess here harmless: the
    #: worst case is that a gone post falls through to the UNCLEAR path, which
    #: is the safe side, and which is also what produces the capture that lets
    #: these be replaced with observed ones.
    NAV_UNAVAILABLE_SELECTORS = [
        "[data-testid='unavailable-post']",
        ".feed-shared-update-v2__removed",
        ".artdeco-empty-state",
    ]
    NAV_UNAVAILABLE_TEXTS = (
        "this post is no longer available",
        "content is not available",
        "page doesn't exist",
        "post has been deleted",
        "no longer exists",
    )
    #: Where that text is looked for. Never the whole page_source: that carries
    #: script and JSON payloads which can contain anything at all.
    NAV_TEXT_SCOPES = ("main", "[role='main']", ".artdeco-empty-state")

    #: The URN types a post URL may be keyed on (Dispatch 15.3). Wider than
    #: the scraper's LinkedInScraper.URN_TYPES by exactly groupPost: the
    #: scraper does not record group-post URNs, but it does store their URLs,
    #: and the poster has to be able to recognise the post it lands on.
    POST_IDENTITY_TYPES = ("activity", "ugcPost", "share", "groupPost")

    #: Shortest numeric segment believed as an id. Guards the slug form, where
    #: words like "share-5-tips" sit in the same URL as the real id.
    POST_IDENTITY_MIN_DIGITS = 6

    @classmethod
    def _identity_urn(cls, url):
        return post_urn.find_post_urn(url or "", cls.POST_IDENTITY_TYPES,
                                      min_digits=cls.POST_IDENTITY_MIN_DIGITS)

    @classmethod
    def post_identity(cls, url: str):
        """The type-qualified post id in a URL (``activity:1010…``), or None.

        LinkedIn rewrites post URLs freely - it drops query strings, swaps
        /posts/<slug>-activity-<id>-xx for /feed/update/urn:li:activity:<id>,
        re-cases the slug. The post id is the one part that does not move, so
        it is what "are we still on the post we asked for" gets decided on.
        Qualified by type, because the same digits under two URN types are two
        different posts. Returns None when there is no id to key on, and the
        redirect check then declines to fire at all.
        """
        found = cls._identity_urn(url)
        return found.qualified if found else None

    @staticmethod
    def _is_feed_url(url: str) -> bool:
        """The home feed - where LinkedIn bounces a deleted post (MAINT. §7)."""
        path = urlparse(url or "").path.rstrip("/")
        return path in ("", "/feed")

    @staticmethod
    def _fully_decoded(url: str) -> str:
        """Percent-decode until stable (bounded): %253A -> %3A -> ':'."""
        out = url or ""
        for _ in range(5):
            nxt = unquote(out)
            if nxt == out:
                break
            out = nxt
        return out

    def navigated_away_from(self, url: str):
        """Reason string if we were redirected off the post, else None.

        Decided on PARSED ids - the one we asked for against the one the
        browser is on - never on the URL string. A substring test of a
        qualified id against the URL would miss every slug-form URL and call
        each of those posts gone.
        """
        wanted = self._identity_urn(self._fully_decoded(url))
        if wanted is None:
            return None
        try:
            current = self.driver.current_url or ""
        except Exception:
            return None
        if not current or current == url:
            return None
        low = current.lower()
        if any(marker in low for marker in self.NAV_AUTH_URL_MARKERS):
            return None          # a session problem, not a missing post
        # Parse the DECODED URL: LinkedIn can serve the very post we asked for
        # at urn%3Ali%3Aactivity%3A<id>. Parsing it raw finds no id there and
        # would call a live post gone (15.3 review, blocking).
        decoded = self._fully_decoded(current)
        # EVERY id in the landed URL, not just the first: a slug's opening
        # words or a query parameter can carry another URN ahead of ours, and
        # finding ours anywhere is the safe reading - UNAVAILABLE is terminal.
        if any(u.qualified == wanted.qualified for u in post_urn.iter_post_urns(
                decoded, self.POST_IDENTITY_TYPES,
                min_digits=self.POST_IDENTITY_MIN_DIGITS)):
            return None
        landed = self._identity_urn(decoded)
        if landed is None and wanted.type != "activity" \
                and not self._is_feed_url(current):
            # Newly identified types (ugcPost/share/groupPost, 15.3) have no
            # track record of where LinkedIn serves them - a group post may
            # live at /groups/<g>/posts/... with no URN. Only the observed
            # gone-post signal, a bounce to the FEED, counts for them.
            # activity keeps its pre-15.3 rule unchanged.
            self.logger.info(
                "Landed on %s (no post id) after asking for %s - not the "
                "feed, so no opinion", current, wanted.qualified)
            return None
        if landed is not None and landed.type != wanted.type:
            # A post URL of ANOTHER type: LinkedIn may normalise share/ugcPost
            # to activity under a different number. That cannot be told apart
            # from a redirect, and UNAVAILABLE is terminal, so: no opinion.
            self.logger.info(
                "Landed on %s after asking for %s - a different URN type, "
                "not treated as a redirect", landed.qualified, wanted.qualified)
            return None
        return "redirected to %s" % current

    def unavailable_marker_on_page(self):
        """Reason string if the page says the post is gone, else None."""
        for selector in self.NAV_UNAVAILABLE_SELECTORS:
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if el.is_displayed():
                        return "unavailable marker: %s" % selector
            except Exception:
                continue
        for scope in self.NAV_TEXT_SCOPES:
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, scope):
                    text = (el.text or "").strip().lower()
                    if not text or len(text) > 2000:
                        continue
                    for phrase in self.NAV_UNAVAILABLE_TEXTS:
                        if phrase in text:
                            return "unavailable text: %r" % phrase
            except Exception:
                continue
        return None

    def post_content_present(self):
        """True once any post-detail selector is on screen."""
        for selector in self.POST_DETAIL_SELECTORS:
            try:
                if self.driver.find_elements(By.CSS_SELECTOR, selector):
                    self.logger.info("Post loaded with selector: %s", selector)
                    return True
            except Exception:
                continue
        return False

    def classify_navigation(self, url: str):
        """Decide, within NAV_DECIDE_SECONDS, what happened to this post.

        One shared budget polling three questions, rather than a chain of
        per-selector waits. Order matters: the redirect check runs FIRST,
        because a bounce to the feed puts perfectly real `div[role=listitem]`
        elements on the page and those would otherwise read as "the post
        loaded" - which is exactly how a gone post used to get all the way to
        the composer.
        """
        started = time.time()
        outcome, reason = self._classify_navigation_loop(url)
        self._poll_done("classify_navigation", self.NAV_DECIDE_SECONDS,
                        started, outcome)
        return outcome, reason

    def _classify_navigation_loop(self, url: str):
        deadline = time.time() + self.NAV_DECIDE_SECONDS
        while True:
            reason = self.navigated_away_from(url)
            if reason:
                return self.NAV_UNAVAILABLE, reason
            if self.post_content_present():
                return self.NAV_OK, None
            reason = self.unavailable_marker_on_page()
            if reason:
                return self.NAV_UNAVAILABLE, reason
            if time.time() >= deadline:
                return self.NAV_UNCLEAR, "post content never loaded"
            time.sleep(self.NAV_POLL_SECONDS)

    def note_unclear_navigation(self, url: str):
        """ONE diagnostic capture per run for posts we could not classify.

        Per run, not per post: the point is to learn what a dead post actually
        looks like, so NAV_UNAVAILABLE_SELECTORS can stop being guesses. The
        first example teaches that; the next forty are disk.

        Deliberately not capture_submit_state - nothing was typed and nothing
        was lost here, so this must not land among the comment-not-posted
        captures, where a real silent failure would then be buried in it.
        """
        if getattr(self, "_unclear_capture_written", False):
            return None
        self._unclear_capture_written = True
        try:
            from . import failure_capture
            return failure_capture.capture_failure(
                self.driver, "post_unclear", self.profile_name,
                page_source=True)
        except Exception:
            self.logger.debug("could not capture the unclear page",
                              exc_info=True)
            return None

    def navigate_to_post(self, url: str) -> bool:
        """Navigate to a LinkedIn post with improved waiting.

        Returns a bool, as its callers expect. The richer answer - whether a
        failure means "gone" or "do not know" - is on ``last_navigation``,
        because only one of those two may ever be acted on terminally.
        """
        self.last_navigation = {"url": url, "outcome": self.NAV_UNCLEAR,
                                "reason": "navigation did not complete"}
        try:
            self.logger.info(f"Navigating to post: {url}")
            with self._step("navigate"):
                # ONE bounded attempt (pm.PAGE_LOAD_TIMEOUT_SECONDS, set on
                # the driver), no retry loop. A timeout is its own outcome:
                # retryable, loud, and never UNAVAILABLE - the post may be
                # perfectly alive behind a slow network.
                started = time.time()
                try:
                    self.driver.get(url)
                except TimeoutException:
                    elapsed = time.time() - started
                    reason = ("page load timed out after %.1fs (limit %ss)"
                              % (elapsed, pm.PAGE_LOAD_TIMEOUT_SECONDS))
                    self.last_navigation = {
                        "url": url, "outcome": self.NAV_TIMEOUT,
                        "reason": reason, "elapsed": elapsed}
                    self.logger.warning(
                        "PAGE LOAD TIMEOUT post=%s %s - NOT marking it gone; "
                        "it stays queued and will be retried: %s",
                        self.post_identity(url) or "-", reason, url)
                    return False

                # A brief settle, NOT a page-load wait: classify_navigation
                # below polls for the real readiness signal. Sleeping 2.5-4s
                # first only delayed asking.
                hb.human_sleep(0.5, 1.0)

            with self._step("classify_navigation"):
                outcome, reason = self.classify_navigation(url)
            self.last_navigation = {"url": url, "outcome": outcome,
                                    "reason": reason}

            if outcome == self.NAV_UNAVAILABLE:
                # Not an error. The tool did exactly the right thing and the
                # post was not there; logging it at ERROR would train the
                # reader to ignore the level real failures use.
                self.logger.info("Post is gone from LinkedIn (%s)", reason)
                return False

            if outcome == self.NAV_UNCLEAR:
                self.logger.warning(
                    "Post content not found on page (%s) - NOT marking it "
                    "gone; it will be retried", reason)
                self.note_unclear_navigation(url)
                return False

            with self._step("navigate_dwell"):
                # Additional wait for dynamic content
                hb.human_sleep(1.5, 2.5)

                # Read the post like a human would before engaging, then a
                # gentle scroll down to take it in and back up to the action
                # bar (variable increments + drift instead of a uniform jump
                # to the half-point).
                hb.simulate_reading(self.driver)
                hb.human_scroll(self.driver, direction="down", pixels=random.randint(300, 600))
                hb.human_sleep(0.8, 1.6)
                hb.human_scroll(self.driver, direction="up", pixels=random.randint(300, 600))
                hb.human_sleep(0.6, 1.2)

            self.logger.info("Successfully navigated to post")
            return True

        except Exception as e:
            self.logger.error(f"Navigation error: {e}")
            self.last_navigation = {"url": url, "outcome": self.NAV_UNCLEAR,
                                    "reason": "navigation error: %s" % e}
            return False
    
    #: TOTAL budget for finding the Like button, across every selector.
    #:
    #: This bound is the durable half of the fix. The old code ran each of
    #: three selectors through a 20-SECOND WebDriverWait, so once they all went
    #: stale every comment paid SIXTY SECONDS to discover it could not like the
    #: post - a third of the run - and then continued anyway. Liking is
    #: optional; waiting a minute to find out it failed is not.
    #:
    #: With the budget shared across selectors, the next selector death costs
    #: seconds. Keeping stale selectors as fallbacks only stays affordable
    #: because of this.
    LIKE_WAIT_SECONDS = 3.0
    LIKE_POLL_SECONDS = 0.25

    def find_like_button(self, timeout=None):
        """The first clickable Like control, or None. Bounded in TOTAL."""
        declared = self.LIKE_WAIT_SECONDS if timeout is None else timeout
        started = time.time()
        found = self._find_like_button_loop(declared)
        self._poll_done("find_like_button", declared, started,
                        "found" if found is not None else "not_found")
        return found

    def _find_like_button_loop(self, timeout):
        deadline = time.time() + timeout
        while True:
            for selector in self.LIKE_BUTTON_SELECTORS:
                try:
                    for el in self.driver.find_elements(By.CSS_SELECTOR, selector):
                        if el.is_displayed() and el.is_enabled():
                            return el
                except Exception:
                    continue
            if time.time() >= deadline:
                return None
            time.sleep(self.LIKE_POLL_SECONDS)

    def like_post(self) -> bool:
        """Like the current post."""
        try:
            like_button = self.find_like_button()

            if not like_button:
                # Check if already liked
                liked_selectors = self.LIKED_STATE_SELECTORS

                for selector in liked_selectors:
                    if self.driver.find_elements(By.CSS_SELECTOR, selector):
                        self.logger.info("Post already liked")
                        return True

                self.note_like_miss("like button not found")
                return False

            # Click like button with a natural mouse approach + click.
            hb.human_click(self.driver, like_button)
            hb.human_sleep(1.2, 2.4)

            self.logger.info("✅ Post liked successfully")
            return True

        except Exception as e:
            self.logger.error(f"Error liking post: {e}")
            self.note_like_miss("error: %s" % e)
            return False

    # ─── A Like miss is soft, but LOUD (Dispatch 15.2) ───────────────────────
    #
    # Liking is optional, so a miss never stops the comment. That does NOT
    # make it quiet. The Like selector is the one that went dead silently
    # before (MAINTENANCE §6.8), and a miss that only says "Like button not
    # found" gives the next repair nothing to work from. On every miss:
    #   * a WARN naming the post and every selector tried;
    #   * the per-run counter the RUN summary reports;
    #   * failure_like_miss_<ts>.png / .html, and _likedom.json listing the
    #     buttons in the post's action bar region, so the new Like hook can be
    #     read straight off the capture.

    #: The labels an action-bar button carries. Used ONLY to locate the region
    #: for the diagnostic capture - never to choose anything to click.
    LIKE_REGION_ACTION_LABELS = ("Like", "Comment", "Repost", "Send")

    _LIKE_REGION_BUTTONS_JS = """
    const labels = arguments[0];
    const stopRole = arguments[1];
    function isAction(b) {
      const t = (b.textContent || '').trim();
      const a = b.getAttribute('aria-label') || '';
      return labels.some(l => t === l || a.indexOf(l) !== -1);
    }
    // The action bar is the nearest container holding three or more
    // action-looking buttons, climbing from any one of them and stopping at
    // the post card.
    const seeds = Array.from(document.querySelectorAll('button')).filter(isAction);
    let region = null, how = 'page';
    for (const seed of seeds) {
      let node = seed.parentElement, hops = 0;
      while (node && hops < 8) {
        if (Array.from(node.querySelectorAll('button')).filter(isAction).length >= 3) {
          region = node; how = 'action_bar'; break;
        }
        if (node.getAttribute && node.getAttribute('role') === stopRole) break;
        node = node.parentElement; hops += 1;
      }
      if (region) break;
    }
    if (!region) {
      region = document.querySelector('[role="' + stopRole + '"]');
      how = region ? 'post_card' : 'page';
    }
    return {region: how,
            buttons: Array.from((region || document).querySelectorAll('button')).slice(0, 80)};
    """

    def like_region_buttons(self):
        """``(region, [button elements])`` around where the Like should be."""
        try:
            found = self.driver.execute_script(
                self._LIKE_REGION_BUTTONS_JS,
                list(self.LIKE_REGION_ACTION_LABELS), self.COMPOSER_STOP_ROLE)
        except Exception as exc:
            self.logger.debug("could not read the action bar region: %s", exc)
            return "unreadable", []
        if not isinstance(found, dict):
            return "unreadable", []
        return found.get("region") or "unknown", list(found.get("buttons") or [])

    def note_like_miss(self, reason):
        """Count, WARN and capture a Like miss. Never raises.

        Called from inside like_post's try, so anything escaping here would
        land in its except, count the miss twice and escape like_post.
        """
        self.like_misses = getattr(self, "like_misses", 0) + 1
        try:
            self._report_like_miss(reason)
        except Exception:
            self.logger.debug("like-miss report failed", exc_info=True)

    def _report_like_miss(self, reason):
        timing = getattr(self, "_timing", None)
        post = timing.post_id if timing is not None else None
        if post is None:
            try:
                post = self.post_identity(self.driver.current_url) or \
                    self.driver.current_url
            except Exception:
                post = "-"
        self.logger.warning(
            "LIKE MISS post=%s reason=%s selectors_tried=%s - continuing to "
            "the comment", post, reason, list(self.LIKE_BUTTON_SELECTORS))
        try:
            capture_failure(self.driver, "like_miss", self.profile_name,
                            page_source=True)
            region, buttons = self.like_region_buttons()
            capture_like_state(
                self.driver, "like_miss", self.profile_name, buttons,
                region=region,
                extra={"reason": reason, "post": post,
                       "selectors_tried": list(self.LIKE_BUTTON_SELECTORS),
                       "liked_state_selectors": list(
                           self.LIKED_STATE_SELECTORS)})
        except Exception:
            self.logger.debug("like-miss capture failed", exc_info=True)

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
            self.logger.info("Clicked comment button")
        else:
            self.logger.info("Comment button not found, trying to find comment box directly")

        # Wait for the box by WATCHING for it, not by sleeping through the
        # worst case. This used to be a flat 2.5-3.5s "give time for the
        # comment box to fully load" followed by a single non-waiting lookup -
        # so a box that appeared in 200ms still cost three seconds, and one
        # that took four was missed anyway.
        return self.await_comment_input()

    #: Cap on waiting for the comment editor to mount.
    COMMENT_INPUT_WAIT_SECONDS = 3.0
    COMMENT_INPUT_POLL_SECONDS = 0.15

    def await_comment_input(self, timeout=None):
        """The comment editor once it is on screen, or None within the cap."""
        declared = (self.COMMENT_INPUT_WAIT_SECONDS if timeout is None
                    else timeout)
        started = time.time()
        found = self._await_comment_input_loop(declared)
        self._poll_done("await_comment_input", declared, started,
                        "found" if found is not None else "not_found")
        return found

    def _await_comment_input_loop(self, timeout):
        deadline = time.time() + timeout
        while True:
            for selector in self.COMMENT_INPUT_SELECTORS:
                try:
                    for elem in self.driver.find_elements(By.CSS_SELECTOR, selector):
                        if elem.is_displayed():
                            self.logger.info(
                                "Found comment input with selector: %s", selector)
                            return elem
                except Exception:
                    continue
            if time.time() >= deadline:
                return None
            time.sleep(self.COMMENT_INPUT_POLL_SECONDS)
    
    def comment_thread_snapshot(self):
        """(count, texts) of the comments currently rendered on this post.

        Taken BEFORE submitting so "did a new comment appear" is answerable.
        Without a before-count the only available signal is "the box changed",
        which a FAILED submit also produces.
        """
        def _texts(elements):
            out = []
            for el in elements:
                try:
                    out.append((el.text or "").strip())
                except Exception:
                    out.append("")
            return out

        # Per-comment elements, when that selector still matches.
        try:
            items = self.driver.find_elements(
                By.CSS_SELECTOR, self.POSTED_COMMENT_SELECTOR)
        except Exception:
            items = []
        if items:
            return (len(items), _texts(items))

        # Otherwise the list container, whose children are the comments plus
        # some chrome. The child COUNT is only a growth signal; the container's
        # text is what the posted comment is matched against.
        try:
            lists = self.driver.find_elements(
                By.CSS_SELECTOR, self.POSTED_COMMENT_CONTAINER_SELECTOR)
        except Exception:
            return (None, [])
        if not lists:
            return (None, [])

        # count=None DELIBERATELY. The container's children are the comments
        # PLUS the post header, the "Most relevant" control and whatever else
        # the list renders, so the number moves for reasons having nothing to
        # do with our comment. Feeding it to the "the thread grew" branch
        # against a pre-change snapshot of 0 reads as enormous growth and turns
        # "did it post?" into an unconditional yes - a false positive, which
        # loses the comment silently. None means "cannot count", leaving the
        # text match as the only proof on offer.
        return (None, _texts(lists))

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
            # Short on purpose. verify_with_polling calls this repeatedly for
            # up to VERIFY_TIMEOUT, so this pause only needs to let one render
            # tick land - the waiting is the loop's job, not this call's.
            hb.human_sleep(0.5, 1.0)
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
                # DEDUPE. The XPath and the class fallbacks routinely match
                # the SAME button, and counting it twice makes an
                # unambiguous page look ambiguous - which now decides
                # whether we post at all, not merely which selector won.
                if any(el == seen for seen in out):
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

    # ─── Scoping the submit button to the composer ───────────────────────────
    #
    # A post page carries TWO buttons whose visible text is exactly "Comment"
    # (proved by the 2026-09-20 captures): the ACTION-BAR button, which only
    # focuses the box, and the COMPOSER's submit, which posts. Neither carries
    # an aria-label, so no attribute test separates them and a page-wide text
    # match takes the first in document order - the action bar.
    #
    # Structure separates them. From the editor, the composer's submit shares
    # an ancestor 6 levels up; the action-bar button not until 11, and level 11
    # IS the post card (role="listitem"). So: walk up from the editor, take the
    # first ancestor containing a submit-looking button, and stop before the
    # post card.
    #
    # There is NO page-wide fallback, by design. The 15:00 capture showed the
    # composer submit DISABLED while the action-bar button was ENABLED, so any
    # "pick the one enabled button on the page" rule resolves to the action bar
    # - the original bug in a new costume. Outside the composer there is
    # nothing safe to click.

    #: Visible labels a comment-submit control uses.
    SUBMIT_BUTTON_TEXTS = ("Comment", "Post", "Reply")

    #: The boundary the walk must not cross: the post card. The action-bar
    #: "Comment" lives at this level, so stopping here makes selecting it
    #: impossible rather than merely unlikely.
    COMPOSER_STOP_ROLE = "listitem"

    #: Guard against an unbounded climb if the DOM shape changes.
    COMPOSER_MAX_HOPS = 12

    _FIND_COMPOSER_SUBMIT_JS = """
    const box = arguments[0];
    const texts = arguments[1];
    const stopRole = arguments[2];
    const maxHops = arguments[3];

    function isSubmit(b) {
      // Deliberately NOT filtering on disabled. A disabled composer submit is
      // still THE submit, and its ENABLING is the signal we wait for - it is
      // how LinkedIn says the typed text registered. Filtering it out is what
      // made the walk return nothing and hand control to a page-wide fallback
      // that clicked the action-bar button instead.
      const t = (b.textContent || '').trim();
      if (texts.indexOf(t) === -1) return false;
      if (b.offsetParent === null) return false;
      return true;
    }

    let node = box.parentElement;
    let hops = 0;
    while (node && hops < maxHops) {
      if (node.getAttribute && node.getAttribute('role') === stopRole) break;
      const found = Array.from(node.querySelectorAll('button')).filter(isSubmit);
      if (found.length) {
        // The submit sits after the editor in the composer footer, so on a
        // container holding more than one, the last is ours.
        return found[found.length - 1];
      }
      node = node.parentElement;
      hops += 1;
    }
    return null;
    """

    def find_composer_submit(self, comment_input):
        """The submit button belonging to THIS comment box, or None.

        Never a page-wide text match: that is the bug. Returns None rather than
        guessing, because a wrong guess clicks a button that silently does
        nothing and the run then has no idea the comment was lost.
        """
        try:
            button = self.driver.execute_script(
                self._FIND_COMPOSER_SUBMIT_JS, comment_input,
                list(self.SUBMIT_BUTTON_TEXTS), self.COMPOSER_STOP_ROLE,
                self.COMPOSER_MAX_HOPS)
        except Exception as exc:
            self.logger.warning("Could not scope the submit button: %s", exc)
            return None
        return button

    def await_composer_submit(self, comment_input, timeout=None):
        """Find the composer's submit and WAIT for it to become enabled.

        Returns ``(button, enabled)``; ``button`` is None only when no submit
        exists inside the composer at all.

        THE ENABLED STATE IS A GATE, NOT A FILTER - that distinction is the
        whole of this fix. LinkedIn enables this button once its editor has
        registered the typed text, so "it became enabled" IS the proof the text
        landed in ProseMirror, and a better signal than reading the box back.
        """
        timeout = self.SUBMIT_ENABLE_TIMEOUT if timeout is None else timeout
        started = time.time()
        button, enabled = self._await_composer_submit_loop(comment_input,
                                                           timeout)
        self._poll_done(
            "await_composer_submit", timeout, started,
            "enabled" if enabled else
            ("never_enabled" if button is not None else "no_button"))
        return button, enabled

    def _await_composer_submit_loop(self, comment_input, timeout):
        deadline = time.time() + timeout
        button = None
        while True:
            found = self.find_composer_submit(comment_input)
            if found is not None:
                button = found
                if self.submit_button_is_enabled(found):
                    return found, True
            if time.time() >= deadline:
                break
            time.sleep(self.SUBMIT_ENABLE_POLL)

        if button is None:
            self.logger.error(
                "No submit button inside the comment composer at all.")
        else:
            self.logger.error(
                "The composer submit never became enabled within %.1fs - the "
                "typed text did not register with LinkedIn's editor.", timeout)
        return button, False

    def focus_comment_box(self, comment_input):
        """Put the caret back in the editor before a keyboard submit.

        Ctrl+Enter goes to whatever has focus. After a click on a button, that
        is the button - so the shortcut may never have reached the box at all.
        """
        try:
            self.driver.execute_script("arguments[0].focus();", comment_input)
            return True
        except Exception:
            self.logger.debug("could not focus the comment box", exc_info=True)
            return False

    def post_comment_ctrl_enter(self, comment_input, comment_text, before=None):
        """Keyboard submit, used as the fallback after a click that did nothing."""
        self.logger.info("Falling back to the Ctrl+Enter keyboard submit...")
        try:
            # Focus FIRST. The shortcut goes wherever focus is, and after the
            # click above that is the button, not the editor.
            self.focus_comment_box(comment_input)
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

    # ─── Getting the text INTO the editor ────────────────────────────────────
    #
    # PER-CHARACTER send_keys IS THE DEFAULT, AND IT WORKS. An earlier reading
    # of the 2026-09-20 15:00 capture concluded it did not - the editor was
    # empty there - and swapped in a whole-comment CDP insert. That reading was
    # wrong, and the 10:33 capture disproves it directly: the editor holds all
    # 177 characters. The 15:00 editor was empty because the comment HAD JUST
    # POSTED and the box cleared; the listed comment and the store's intended
    # comment are the same 177-character string, and the byline reads
    # "Jeff Wurfel - You" with an age of "now".
    #
    # So the cadence stays. Typing a comment at human speed is the point of
    # this tool, and nothing was gained by dropping it.
    #
    # The insert paths remain, switched OFF, because they are a genuine
    # fallback for an editor that really does ignore key events - and because
    # the confirm-each-method loop below is worth keeping either way. Turning
    # INSERT_FALLBACKS on adds them AFTER typing, never instead of it.
    HUMAN_TYPING = True
    INSERT_FALLBACKS = False

    #: How long to keep looking for the comment before trying anything else.
    VERIFY_TIMEOUT = 8.0
    VERIFY_POLL = 1.0

    def clear_comment_box(self, comment_input):
        """Empty the editor through its own input pipeline. True if now empty."""
        try:
            self.driver.execute_script(
                "arguments[0].focus();"
                "document.execCommand('selectAll', false, null);"
                "document.execCommand('delete', false, null);", comment_input)
        except Exception:
            self.logger.debug("could not clear the editor", exc_info=True)
        return self._comment_box_is_empty(comment_input)

    def insert_via_cdp(self, comment_input, text):
        """Insert through Chrome's input pipeline. Fires beforeinput/input."""
        self.driver.execute_cdp_cmd("Input.insertText", {"text": text})

    def insert_via_exec_command(self, comment_input, text):
        """The in-page equivalent, for a driver with no CDP."""
        self.driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('insertText', false, arguments[1]);",
            comment_input, text)

    def enter_comment_text(self, comment_input, comment_text):
        """Get the text in AND confirm it registered. Returns (button, path).

        Confirmation is the enable gate, not a read-back: LinkedIn enables the
        composer submit once its editor has accepted the text, so an enabled
        button is the editor's own word that the document is non-empty.

        Each method is CONFIRMED before the next is considered, and between
        attempts the box must be verifiably EMPTY. If an attempt left text
        behind without enabling the button, inserting again would post the
        comment twice over and a doubled comment cannot be unposted - so this
        stops and lets the caller fail loud instead.
        """
        methods = []
        if self.HUMAN_TYPING:
            methods.append(("send_keys", lambda el, t: hb.type_like_human(
                self.driver, el, t)))
        if self.INSERT_FALLBACKS:
            methods.append(("cdp", self.insert_via_cdp))
            methods.append(("execCommand", self.insert_via_exec_command))
        if not methods:                       # never leave no way to type
            methods.append(("send_keys", lambda el, t: hb.type_like_human(
                self.driver, el, t)))

        for path, insert in methods:
            with self._step("type"):
                self.focus_comment_box(comment_input)
                try:
                    insert(comment_input, comment_text)
                except Exception as exc:
                    self.logger.warning("input path %s failed: %s", path, exc)
                    continue

            with self._step("await_submit_enabled"):
                button, enabled = self.await_composer_submit(comment_input)
            if enabled:
                self.logger.info("Text landed  input_path=%s", path)
                return button, path

            self.logger.warning("input path %s did not enable the submit", path)
            with self._step("clear_after_failed_input"):
                cleared = self.clear_comment_box(comment_input)
            if not cleared:
                self.logger.error(
                    "input path %s left text in the editor that did not enable "
                    "the submit. NOT inserting again - a second insert would "
                    "post the comment twice.", path)
                return button, None

        return None, None

    def verify_with_polling(self, comment_input, comment_text, before,
                            timeout=None):
        """Verification, given time for the thread to render.

        A comment can take a moment to appear. Checking once and moving on to
        the keyboard fallback is how a slow render becomes a SECOND comment, so
        the window is waited out before anything else is tried.
        """
        timeout = self.VERIFY_TIMEOUT if timeout is None else timeout
        started = time.time()
        ok = self._verify_with_polling_loop(comment_input, comment_text,
                                            before, timeout)
        self._poll_done("verify_with_polling", timeout, started,
                        "verified" if ok else "not_verified")
        return ok

    def _verify_with_polling_loop(self, comment_input, comment_text, before,
                                  timeout):
        deadline = time.time() + timeout
        while True:
            if self.verify_comment_posted(comment_input, comment_text, before):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(self.VERIFY_POLL)

    # ─── The double-post guard ───────────────────────────────────────────────
    #
    # THIS IS THE PROTECTION THAT DOES NOT DEPEND ON US BEING RIGHT.
    #
    # Every other safeguard here reasons from OUR records: the posted ledger,
    # the store's status, the progress file. Today proved those can all be
    # wrong at once - a dead verifier reported three posted comments as
    # failures, so the ledger says "not posted" about a comment that is live on
    # LinkedIn right now. Re-running any of them would comment twice, and a
    # duplicate comment cannot be withdrawn.
    #
    # So this asks the THREAD instead. If a comment by us is already there, the
    # work is done, whatever our files believe. That holds through a cleared
    # store, a restored archive, a re-scrape, a second machine, or a bug we
    # have not found yet.
    #
    # LinkedIn marks your own comments with a self byline - "Name • You" - in
    # the comment list. That marker, or the exact intended text, is enough.

    #: How LinkedIn labels your own comment in a thread.
    SELF_COMMENT_MARKER = "• You"

    def already_commented_here(self, comment_text=None):
        """Is one of OUR comments already on this thread?

        Returns ``(True, reason)`` when the thread should be left alone.

        Deliberately conservative in the SAFE direction: when the thread cannot
        be read at all this returns False, because refusing to ever post on an
        unreadable page would silently stop the tool. The cost of that choice
        is bounded by every other guard; the cost of the opposite choice is a
        duplicate comment.
        """
        _, texts = self.comment_thread_snapshot()
        blob = " ".join(texts or [])
        if not blob:
            return False, "thread not readable"

        needle = " ".join((comment_text or "").split())[:80]
        if needle and needle in " ".join(blob.split()):
            return True, "this exact comment is already on the thread"

        if self.SELF_COMMENT_MARKER and self.SELF_COMMENT_MARKER in blob:
            return True, ("a comment of ours is already on this thread (%r)"
                          % self.SELF_COMMENT_MARKER)

        return False, "no comment of ours found on this thread"

    def post_comment(self, comment_text: str) -> bool:
        """Post a comment on the current post using all available methods."""
        try:
            # Open comment box
            with self._step("open_composer"):
                comment_input = self.open_comment_box()

            if not comment_input:
                self.logger.error("Comment input field not found")
                with self._step("failure_capture"):
                    capture_failure(self.driver, "comment_box_not_found", self.profile_name)
                return False

            with self._step("compose_prep"):
                # Click to focus and type comment. Scroll the box into view
                # with human-like smoothness, then a natural mouse approach +
                # click to focus it (instead of a teleport click).
                hb.scroll_to_element(self.driver, comment_input)

                # Variable pause between reading the post and starting to type
                # — a human composes for a beat before the first keystroke.
                hb.human_sleep(0.8, 2.0)

                # Clear, then type character-by-character with human-like
                # timing (random 40-120ms keystrokes + occasional thinking
                # pauses + mouse drift) via human_behavior, instead of dumping
                # all at once.
                hb.human_click(self.driver, comment_input)
                hb.human_sleep(0.3, 0.7)
                self.clear_comment_box(comment_input)

                # The thread BEFORE anything is entered.
                before = self.comment_thread_snapshot()

            # STEP 1 - get the text in, and confirm the editor registered it.
            # The submit enabling IS that confirmation. Timed inside, as
            # "type" and "await_submit_enabled".
            button, input_path = self.enter_comment_text(comment_input,
                                                         comment_text)
            if button is None or input_path is None:
                with self._step("failure_capture"):
                    self.capture_submit_failure(comment_text, before,
                                                "text_did_not_register")
                return False
            self.logger.info("Entered comment (%d chars)  input_path=%s",
                             len(comment_text), input_path)

            with self._step("review_dwell"):
                # A beat to "review" between entering and submitting.
                hb.human_sleep(1.0, 2.5)

            # STEP 2 - the submit is already located AND enabled, which is what
            # proved the text registered. Click it.
            with self._step("submit"):
                try:
                    self.logger.info("Clicking the composer submit: %r",
                                     (button.text or "").strip() or "<no text>")
                    hb.human_click(self.driver, button)
                except Exception as exc:
                    self.logger.warning("The submit click raised: %s", exc)

            # STEP 3 - POLL before trying anything else. A comment still
            # rendering is not a comment that failed, and reaching for the
            # keyboard here is how a slow render becomes a SECOND comment.
            with self._step("verify"):
                verified = self.verify_with_polling(comment_input,
                                                    comment_text, before)
            if verified:
                self.logger.info("Comment POSTED  path=scoped_button")
                return True

            # STEP 4 - only now, the keyboard, into the FOCUSED editor, once.
            with self._step("fallback_submit"):
                fallback = self.post_comment_ctrl_enter(comment_input,
                                                        comment_text, before)
            if fallback:
                self.logger.info("Comment POSTED  path=ctrl_enter")
                return True

            with self._step("failure_capture"):
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

        # From here to the ledger write, every stretch of time is a named step
        # (Dispatch 15.1): the COMMENT line compares their sum to the total, so
        # time spent outside any step is visible instead of silently absorbed.
        timing = run_log.CommentTiming(self.post_identity(url) or url)
        self._timing = timing
        self._timing_outcome = "error"
        try:
            return self._post_single_comment_timed(url, comment_text,
                                                   comment_data)
        finally:
            self._timing = None
            timing.finish(self._timing_outcome)
            self.last_comment_timing = timing
            if getattr(self, "run_timing", None) is not None:
                self.run_timing.add_comment(timing)

    def _post_single_comment_timed(self, url, comment_text, comment_data):
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"Processing comment for post: {comment_data['preview'][:80]}...")
        self.logger.info(f"Comment: {comment_text[:100]}...")

        # Navigate to post. Timed inside, as "navigate",
        # "classify_navigation" and "navigate_dwell".
        if not self.navigate_to_post(url):
            nav = getattr(self, "last_navigation", None) or {}
            if nav.get("outcome") == self.NAV_UNAVAILABLE:
                self._timing_outcome = "unavailable"
                with self._step("progress_write"):
                    self.record_unavailable(url, nav.get("reason"))
            elif nav.get("outcome") == self.NAV_TIMEOUT:
                # Nothing recorded: the post stays queued for the next run.
                self._timing_outcome = "navigation_timeout"
            elif str(nav.get("reason") or "").startswith("navigation error"):
                self._timing_outcome = "navigation_error"
            else:
                self._timing_outcome = "navigation_unclear"
            return False

        with self._step("read_dwell"):
            # Read the post before engaging, scaled to the comment we intend
            # to leave (longer/considered comments imply a longer read).
            hb.simulate_reading_for_text(self.driver, comment_text)

        # BEFORE anything is typed: is our comment already here? Asking the
        # thread beats trusting our own records, which today were wrong.
        with self._step("thread_check"):
            already, why = self.already_commented_here(comment_text)
        if already:
            self.logger.info("SKIPPING %s - %s", url, why)
            self._timing_outcome = "already_commented"
            with self._step("progress_write"):
                self.mark_already_commented(url, why)
            return True

        # Like the post
        with self._step("like"):
            liked = self.like_post()
        if not liked:
            # Already counted, WARNed and captured by note_like_miss.
            self.logger.warning("Failed to like post, continuing anyway...")

        with self._step("pre_compose_dwell"):
            # Variable pause between liking and opening the comment box.
            hb.human_sleep(0.8, 2.2)

        # Post the comment. A False here means NOT POSTED - it must never
        # reach the ledger below, which is what post_store reconciles COMMENTED
        # from. Marking an unposted comment done loses it permanently: the URL
        # is skipped on every future run.
        if not self.post_comment(comment_text):
            self._timing_outcome = "failed"
            with self._step("progress_write"):
                self.record_comment_failure(url, "comment_not_posted")
            return False

        # Mark as completed - reached ONLY when the comment was verified in the
        # thread.
        with self._step("progress_write"):
            self.progress['posted_comments'].append(url)
            self.progress['last_posted'] = datetime.now().isoformat()
            self.save_progress()
        self._timing_outcome = "posted"

        self.logger.info("✅ Successfully engaged with post!")

        with self._step("post_dwell"):
            # Wait before next action (variable, not a flat 5s)
            hb.human_sleep(4.0, 7.0)

        return True
    

    def _report_run(self, posted, failed, skipped, attempted, total,
                    unavailable=0):
        """The run summary. A batch that posted nothing must be unmistakable.

        The old line was `Done. Posted N, skipped M` at INFO with a tick, which
        read as success whatever N was - and "skipped" quietly absorbed posts
        that had been typed and lost.
        """
        result = {"posted": posted, "failed": failed, "skipped": skipped,
                  "attempted": attempted, "total": total,
                  "unavailable": unavailable}
        if unavailable:
            # Its own line, at INFO. Gone posts are not failures and must not
            # inflate the failure count - but they are not invisible either: a
            # run where everything turned out to be gone should be legible as
            # exactly that, rather than as a quiet nothing-happened.
            self.logger.info(
                "%d post(s) were gone from LinkedIn and will not be retried.",
                unavailable)
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

    def mark_already_commented(self, url, reason):
        """Record a thread we found already commented on, without posting.

        Written to the posted ledger because that is exactly what it is: the
        comment IS on LinkedIn. post_store reconciles COMMENTED from this file,
        so the record leaves the queue and stops being offered - which is the
        point, since re-offering it is how it would be posted twice.
        """
        posted = self.progress.setdefault("posted_comments", [])
        if url not in posted:
            posted.append(url)
        self.progress.setdefault("skipped_already_commented", []).append({
            "url": url,
            "reason": reason,
            "at": datetime.now().isoformat(),
        })
        try:
            self.save_progress()
        except Exception:
            self.logger.debug("could not persist the skip record",
                              exc_info=True)
        return True

    def record_unavailable(self, url, reason=None):
        """Record a post that is GONE. Terminal, and deliberately not a failure.

        Written to `unavailable_posts` in the same progress file that holds
        `posted_comments`, and for the same reason: the poster is the only
        thing that can observe this, so the poster writes it and the store
        reconciles FROM it. Two systems each keeping their own opinion of
        which posts still exist is how they drift.

        Pointedly NOT `failed_comments`. A failure means a comment we meant to
        leave did not go out and should be retried; this post cannot be
        retried and there is nothing to fix. Mixing them would put permanent
        noise in the one list that is supposed to demand attention.
        """
        entry = {
            "url": url,
            "reason": reason or "post not reachable",
            "at": datetime.now().isoformat(),
        }
        known = self.progress.setdefault("unavailable_posts", [])
        if not any((e.get("url") if isinstance(e, dict) else e) == url
                   for e in known):
            known.append(entry)
        try:
            self.save_progress()
        except Exception:
            self.logger.debug("could not persist the unavailable record",
                              exc_info=True)
        self.logger.info("Post is no longer on LinkedIn - skipping "
                         "permanently (%s): %s", entry["reason"], url)
        return entry

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
        """Run the comment posting process, with a file log of where time went.

        Opens ``logs/run_<profile>_<ts>.log`` for the length of the run and
        closes it with a RUN summary line, however the run ends (Dispatch 15.1).
        """
        handler, self.run_log_path = run_log.open_run_log(
            self.profile_name or "default")
        self.run_timing = run_log.RunTiming(self.profile_name)
        self.like_misses = 0
        self.last_run_result = None
        try:
            return self._run(comments_file, post_count, manual_mode)
        finally:
            r = self.last_run_result or {}
            try:
                self.run_timing.summary(
                    attempted=r.get("attempted", 0), posted=r.get("posted", 0),
                    skipped=r.get("skipped", 0), failed=r.get("failed", 0),
                    unavailable=r.get("unavailable", 0),
                    like_misses=self.like_misses)
            finally:
                self.run_timing = None
                run_log.close_run_log(handler)

    def _run(self, comments_file: str, post_count: int = 1, manual_mode: bool = False):
        # Parse comments
        comments = self.parse_comments_file(comments_file)

        if not comments:
            self.logger.error("No comments found in file")
            return

        self.logger.info(f"Found {len(comments)} comments to post")

        # Setup and login
        with self._run_step("setup"):
            self.setup_driver()
            logged_in = self.login()
        if not logged_in:
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
            # And a GONE post is neither. Nothing failed and nothing
            # was skipped by choice - the post stopped existing.
            unavailable = 0
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
                    elif (getattr(self, "last_navigation", None) or {}).get(
                            "outcome") == self.NAV_UNAVAILABLE:
                        # The post is gone. Counted apart from failed on
                        # purpose: a failure is something to go and look at,
                        # and a run of twelve deleted posts reporting twelve
                        # failures would make the alarm meaningless.
                        attempted += 1
                        unavailable += 1
                        self.logger.info(
                            "%s post is gone from LinkedIn - skipped "
                            "permanently", label)
                        continue
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
                    with self._run_step("interpost_wait"):
                        time.sleep(wait_time)

                    # Longer break after a re-rolled number of comments.
                    posts_since_break += 1
                    if posts_since_break >= break_threshold:
                        self.logger.info(
                            f"Taking a natural break after {posts_since_break} comments...")
                        with self._run_step("break"):
                            hb.take_break(self.driver)
                        posts_since_break = 0
                        break_threshold = hb.random_break_threshold()

            self.last_run_result = self._report_run(
                posted, failed, skipped, attempted, total,
                unavailable=unavailable)
            return self.last_run_result

        finally:
            if self.driver:
                if not manual_mode:
                    with self._run_step("teardown"):
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