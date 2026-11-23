"""Instagram's engagement adapter, in Selenium (D-027 E3, PLATFORM_REFACTOR §9).

A **browser** adapter of ``engagement_adapter.EngagementAdapter``, written
fresh. ``instagram_bot`` (Playwright, read only) was the reference for the
selectors, the shortcode, the og-meta reading and the pacing; none of its code
is imported and none of its behaviour is carried where it breaks §9 or §11
(it typed the password, counted an emptied input as posted and returned
"liked" on "could not verify").

**Every selector is UNVERIFIED.** They are the reference's, nine months stale,
plus shapes inferred from it, and no live Instagram page has confirmed one.
Each is a class constant below, registered in ``selector_health`` through
:func:`registry_entries` with ``"verified": False``. The fixtures in
``tests/fixtures/instagram/`` were written from the same reference and prove
only that the code matches the shape it was written for.

**The live gate.** The adapter refuses to act on a live browser until a live
selector-health pass is recorded for the dev account:
``data/<profile>/instagram/selector_health_live.json``, written only by
:func:`run_live_health_check` (``python -m linkedin_automation.instagram_adapter
health``). :func:`live_pass_problem` accepts it only when it is a live run,
HEALTHY, for the identity now configured, for exactly the selectors now in the
code (a fingerprint), and younger than ``LIVE_PASS_MAX_AGE``. A driver is
offline only when it declares ``IG_OFFLINE_FIXTURE = True`` (the test fake);
every other driver, and the driver the adapter would create itself, is live.

**Login is the human's**, in the profile's Chrome session, as on LinkedIn.
Nothing here reads a credential or types into a login form; a login form on
screen is ``unclear`` (``auth_wall``) and the run stops.

**Identity.** Before any action the page must show the configured account
(``platforms.instagram.identity_slug``) as the signed-in one: its profile
link with its avatar, outside the post. Not proven is ``unclear``, never a
guess. The production guard applies twice: ``create_driver``'s (the
profile's declared identity) and ``check_declared_production`` on the
Instagram identity, with no override: Instagram never runs as production.

**Verdicts, positive only.** ``classify`` says ``gone`` only on Instagram's
"page isn't available" text; a redirect, a login wall, a slow load or an
unknown shape is ``unclear``. ``post_and_verify`` says posted only when a
comment by our handle with the exact text is seen in the thread, and again
after a reload.
``like`` says ``placed`` only when the liked state is read after the click.
Every wait is bounded in total and polled (PRINCIPLES §4); every failure
leaves a named reason and a capture in ``data/<profile>/failures/``.

**Posting only through the E0 gate.** ``post_and_verify`` re-checks
``approval_refusal_for`` itself; :func:`engage_item` runs the E2 validator,
then ``engage_one`` with a fresh store read as the last step.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from selenium.webdriver.common.by import By

from . import engagement_adapter as ea
from . import instagram_engage as ie
from . import post_store
from . import profile_manager as pm
from .failure_capture import capture_failure

logger = logging.getLogger(__name__)

PLATFORM = ie.PLATFORM
BASE_URL = "https://www.instagram.com"
FEED_URL = BASE_URL + "/"

#: A post's shortcode, from a permalink (``/p/<code>/``) as the reference did.
SHORTCODE_RE = re.compile(r"/p/([A-Za-z0-9_-]{5,40})(?:/|$|\?)")

LIVE_PASS_FILE = "selector_health_live.json"
LIVE_PASS_SCHEMA = 1
#: A pass older than this no longer opens the gate.
LIVE_PASS_MAX_AGE = timedelta(days=7)

HEALTH_COMMAND = (".venv/Scripts/python.exe -m linkedin_automation.instagram_adapter health "
                  "--profile <profile> --post-url https://www.instagram.com/p/<code>/")

#: Store outcomes.
TRASH_UNVERIFIED = "instagram_unverified"     # sent, not seen: never retried
TRASH_ACCOUNT_SKIPPED = "instagram_account_skipped"

PROVENANCE = ("instagram_bot reference (Playwright, ~2026-02, nine months stale) "
              "and shapes inferred from it; no live capture yet")


class LiveGateRefused(ie.InstagramRefused):
    """The adapter was asked to act on a live browser without a recorded,
    current live selector-health pass."""


def shortcode_of(url) -> Optional[str]:
    m = SHORTCODE_RE.search(url or "")
    return m.group(1) if m else None


def post_url(shortcode):
    return "%s/p/%s/" % (BASE_URL, shortcode)


def _norm(text):
    return " ".join((text or "").split())


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── The live hands (tests pass their own) ───────────────────────────────────

class HumanHands:
    """Clicks and typing through ``human_behavior``, as LinkedIn's poster does."""

    def click(self, driver, element):
        from . import human_behavior as hb
        hb.human_click(driver, element)

    def type(self, driver, element, text):
        from . import human_behavior as hb
        hb.type_like_human(driver, element, text)


# ─── The adapter ─────────────────────────────────────────────────────────────

class InstagramAdapter(ea.EngagementAdapter):
    """Instagram engagement in a Selenium browser. See the module docstring."""

    platform = PLATFORM
    kind = ea.BROWSER

    # ── selectors: ALL UNVERIFIED (reference or inferred; see PROVENANCE) ──
    # Feed (https://www.instagram.com/).
    # UNVERIFIED (reference 00_login/03: the Home icon means signed in).
    LOGGED_IN_SELECTORS = ("svg[aria-label='Home']",)
    # UNVERIFIED (reference 01: the login form's username field). Detected
    # only, never typed into.
    LOGIN_FORM_SELECTORS = ("input[name='username']",)
    # UNVERIFIED (inferred: feed posts are <article>s).
    FEED_POST_SELECTORS = ("article",)
    # UNVERIFIED (reference 01:156-185 collected a[href*="/p/"]).
    PERMALINK_SELECTORS = ("article a[href*='/p/']", "a[href*='/p/']")
    # UNVERIFIED (inferred: the signed-in account's nav link carries its
    # avatar). Formatted with the handle by IDENTITY_LINK_TEMPLATE.
    IDENTITY_AVATAR_SELECTORS = ("a[href^='/'] img[alt$='profile picture']",)
    IDENTITY_LINK_TEMPLATE = "a[href='/{handle}/'] img[alt$='profile picture']"
    IDENTITY_IN_POST_TEMPLATE = "article a[href='/{handle}/'] img[alt$='profile picture']"

    # Post page (https://www.instagram.com/p/<shortcode>/).
    # UNVERIFIED (inferred).
    POST_CONTAINER_SELECTORS = ("article",)
    # UNVERIFIED (inferred: og:url / canonical name the post's permalink).
    POST_URL_META_SELECTORS = ("meta[property='og:url']", "link[rel='canonical']")
    # UNVERIFIED (reference 01:209-231 read the caption from og:description).
    CAPTION_META_SELECTORS = ("meta[property='og:description']",)
    # UNVERIFIED (inferred: Instagram's removed-post page text). The ONLY
    # gone signal; absent on a healthy post, so min_expected is 0.
    GONE_SELECTORS = ('//span[normalize-space(.)="Sorry, this page isn\'t available."]',
                      '//h2[normalize-space(.)="Sorry, this page isn\'t available."]')
    # UNVERIFIED (reference 03:78-115: svg aria-label Like / Unlike). Scoped
    # to the action bar <section>: the comments' own Like hearts are the
    # decoys the reference told apart by screen position.
    LIKE_BUTTON_SELECTORS = ("section svg[aria-label='Like']",)
    LIKED_STATE_SELECTORS = ("section svg[aria-label='Unlike']",)
    # UNVERIFIED (reference 03:224-229).
    COMMENT_INPUT_SELECTORS = ("form textarea[aria-label*='Add a comment']",
                               "form textarea[placeholder*='Add a comment']",
                               "form textarea")
    # UNVERIFIED (reference 03:262: a button / div[role=button] reading Post).
    # Text-matched on COMMENT_SUBMIT_TEXT, exactly one, inside the form.
    COMMENT_SUBMIT_SELECTORS = ("form div[role='button']", "form button[type='submit']")
    COMMENT_SUBMIT_TEXT = "Post"
    # UNVERIFIED (inferred: the thread is a list under the post; each item
    # has its author's profile link and the text in a span[dir=auto]).
    COMMENT_ITEM_SELECTORS = ("article ul li",)
    COMMENT_AUTHOR_IN_ITEM = "a[href^='/']"
    COMMENT_TEXT_IN_ITEM = "span[dir='auto']"

    # Profile page (https://www.instagram.com/<handle>/), for the find filter.
    # UNVERIFIED (inferred: "<n> Followers, <n> Following, <n> Posts - ...").
    PROFILE_META_SELECTORS = ("meta[property='og:description']",)
    # UNVERIFIED (inferred).
    PROFILE_HEADER_SELECTORS = ("header section", "header")
    # UNVERIFIED (inferred). Absent on an unverified account: min_expected 0.
    VERIFIED_BADGE_SELECTORS = ("header svg[aria-label='Verified']",)

    # ── bounded waits (seconds, total per step, polled) ────────────────────
    NAV_SECONDS = 15.0
    INPUT_SECONDS = 6.0
    LIKE_VERIFY_SECONDS = 5.0
    COMMENT_VERIFY_SECONDS = 15.0
    POLL_SECONDS = 0.3
    #: Feed scrolls per find, and the pause the reference used between them.
    FIND_MAX_SCROLLS = 5

    def __init__(self, profile, driver=None, *, cfg=None, hands=None, now=None):
        self.profile = profile
        self._driver = driver
        self._cfg = cfg
        self.hands = hands or HumanHands()
        self.now = now
        self.last_capture = None

    # ── config, gate, driver ────────────────────────────────────────────────

    @property
    def cfg(self) -> ie.InstagramConfig:
        if self._cfg is None:
            self._cfg = ie.instagram_config(self.profile)
        return self._cfg

    @property
    def handle(self):
        return self.cfg.identity_slug

    def is_live(self):
        return not getattr(self._driver, "IG_OFFLINE_FIXTURE", False) \
            if self._driver is not None else True

    def check_gate(self):
        """Raise :class:`LiveGateRefused` unless this run may act. Offline
        fixtures pass; a live browser needs an identity that is not
        production and a current live selector-health pass."""
        if not self.handle:
            raise LiveGateRefused("platforms.instagram.identity_slug is not set for "
                                  "profile %r; the adapter does not know who it is"
                                  % self.profile)
        if not self.is_live():
            return
        try:
            pm.check_declared_production(
                self.handle, "drive Instagram for profile %r" % self.profile)
        except pm.ProductionAccessRefused as exc:
            raise LiveGateRefused(str(exc)) from None
        problem = live_pass_problem(self.profile, self.cfg, now=self.now)
        if problem:
            raise LiveGateRefused(problem)

    @property
    def driver(self):
        self.check_gate()
        if self._driver is None:
            self._driver, _info = pm.create_driver(self.profile)
        return self._driver

    # ── page reading ────────────────────────────────────────────────────────

    def _find(self, selector, root=None):
        by = By.XPATH if selector.startswith(("/", "(")) else By.CSS_SELECTOR
        try:
            return list((root if root is not None else self._driver)
                        .find_elements(by, selector))
        except Exception:
            logger.debug("find_elements failed for %r", selector, exc_info=True)
            return []

    def _first(self, selectors, root=None):
        for sel in selectors:
            found = self._find(sel, root)
            if found:
                return found
        return []

    def _poll(self, seconds, check):
        """``check()`` until it returns something truthy or ``seconds`` pass.
        One deadline for the whole step."""
        deadline = time.monotonic() + seconds
        while True:
            got = check()
            if got:
                return got
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.POLL_SECONDS)

    def _capture(self, label):
        self.last_capture = capture_failure(self._driver, "ig_" + label, self.profile)
        return self.last_capture

    def identity_proven(self) -> bool:
        """The configured account is the signed-in one: its own profile link,
        with its avatar, outside the post (a post's author link carries an
        avatar too, so the ones inside <article> do not count)."""
        if not self.handle:
            return False
        anywhere = len(self._find(self.IDENTITY_LINK_TEMPLATE.format(handle=self.handle)))
        in_post = len(self._find(self.IDENTITY_IN_POST_TEMPLATE.format(handle=self.handle)))
        return anywhere - in_post > 0

    def logged_out(self) -> bool:
        url = (getattr(self._driver, "current_url", "") or "").lower()
        return "/accounts/login" in url or bool(self._first(self.LOGIN_FORM_SELECTORS))

    def _meta(self, selectors, attr="content"):
        for el in self._first(selectors):
            value = el.get_attribute(attr) or el.get_attribute("href")
            if value:
                return value
        return None

    def read_post(self):
        """``{"author_handle", "caption"}`` from og:description, as the
        reference read it. Missing parts are ""."""
        return parse_post_description(self._meta(self.CAPTION_META_SELECTORS) or "")

    def read_account(self, handle):
        """``{"handle", "followers", "verified"}`` from the profile page.
        ``verified`` is None unless the header rendered."""
        self.driver.get("%s/%s/" % (BASE_URL, handle))
        rendered = self._poll(self.NAV_SECONDS, lambda: (
            self._first(self.PROFILE_HEADER_SELECTORS)
            and self._meta(self.PROFILE_META_SELECTORS)))
        if not rendered:
            self._capture("profile_unread")
            return {"handle": handle, "followers": None, "verified": None}
        desc = self._meta(self.PROFILE_META_SELECTORS) or ""
        return {"handle": handle, "followers": parse_followers(desc),
                "verified": bool(self._find(self.VERIFIED_BADGE_SELECTORS[0]))}

    # ── find ────────────────────────────────────────────────────────────────

    def find(self, max_posts=10, store=None):
        """Land up to ``max_posts`` new feed posts in the store: NEW when the
        author passes the account filter, TRASH(instagram_account_skipped)
        with the reason when not. Returns a summary that counts every skip."""
        drv = self.driver
        store = store if store is not None else ie.load_store(self.profile)
        summary = {"added": [], "filtered": {}, "known": 0, "unread": 0,
                   "stopped": None}
        drv.get(FEED_URL)
        state = self._poll(self.NAV_SECONDS, lambda: (
            "feed" if self._first(self.LOGGED_IN_SELECTORS) else
            "login" if self.logged_out() else None))
        if state != "feed":
            summary["stopped"] = "auth_wall" if state == "login" else "feed_timeout"
            self._capture("find_" + summary["stopped"])
            return summary
        if not self.identity_proven():
            summary["stopped"] = "identity_unproven"
            self._capture("find_identity_unproven")
            return summary
        codes = []
        for _scroll in range(self.FIND_MAX_SCROLLS):
            for el in self._first(self.PERMALINK_SELECTORS):
                code = shortcode_of(el.get_attribute("href"))
                if code and code not in codes:
                    codes.append(code)
            if len(codes) >= max_posts * 2:
                break
            drv.execute_script("window.scrollBy(0, 900);")
            time.sleep(self.POLL_SECONDS)
        accounts = {}
        for code in codes:
            if len(summary["added"]) >= max_posts:
                break
            if store.get(code) is not None:
                summary["known"] += 1
                continue
            drv.get(post_url(code))
            if not self._poll(self.NAV_SECONDS, lambda: self._meta(self.CAPTION_META_SELECTORS)):
                summary["unread"] += 1
                self._capture("find_post_unread")
                continue
            meta = self.read_post()
            handle = meta["author_handle"]
            if handle and handle not in accounts:
                accounts[handle] = self.read_account(handle)
            account = accounts.get(handle) or {"handle": None}
            why = ie.account_refusal(account, self.cfg)
            rec = _record(code, meta, account, why)
            store.posts[code] = rec
            if why:
                summary["filtered"][why] = summary["filtered"].get(why, 0) + 1
            else:
                summary["added"].append(code)
        store.save()
        return summary

    # ── classify ────────────────────────────────────────────────────────────

    def _on_post(self, code):
        def rendered():
            if not self._first(self.POST_CONTAINER_SELECTORS):
                return None
            url = self._meta(self.POST_URL_META_SELECTORS) or ""
            return shortcode_of(url) == code
        return rendered()

    def classify(self, record) -> ea.Classification:
        code = record.get("key") or shortcode_of(record.get("url"))
        drv = self.driver
        drv.get(post_url(code))

        def state():
            if self._first(self.GONE_SELECTORS):
                return "gone"
            if self.logged_out():
                return "auth_wall"
            if self._on_post(code):
                return "ok"
            return None
        got = self._poll(self.NAV_SECONDS, state)
        if got == "gone":
            return ea.Classification(ea.GONE, "post_removed: Instagram says the page "
                                     "isn't available", detail="removed")
        if got == "auth_wall":
            self._capture("auth_wall")
            return ea.Classification(ea.UNCLEAR, "auth_wall: signed out; sign in by "
                                     "hand in the profile's browser", detail="auth_wall")
        if got is None:
            self._capture("classify_timeout")
            detail = "redirected" if shortcode_of(drv.current_url) != code else "timeout"
            return ea.Classification(ea.UNCLEAR, "%s: the post did not render as %s "
                                     "within %.0fs" % (detail, code, self.NAV_SECONDS),
                                     detail=detail)
        if not self.identity_proven():
            self._capture("identity_unproven")
            return ea.Classification(ea.UNCLEAR, "identity_unproven: the page does not "
                                     "show @%s as signed in" % self.handle,
                                     detail="identity_unproven")
        return ea.Classification(ea.OK, "the post rendered as %s" % code)

    # ── the thread ──────────────────────────────────────────────────────────

    def _thread(self):
        """``[(author handles, texts)]`` for each comment item shown."""
        out = []
        for item in self._first(self.COMMENT_ITEM_SELECTORS):
            authors = set()
            for a in self._find(self.COMMENT_AUTHOR_IN_ITEM, item):
                h = (a.get_attribute("href") or "").strip("/").split("/")[-1].lower()
                if h:
                    authors.add(h)
            texts = [_norm(s.text) for s in self._find(self.COMMENT_TEXT_IN_ITEM, item)]
            out.append((authors, texts))
        return out

    def already_commented(self, record) -> ea.ThreadCheck:
        self.driver  # the gate
        code = record.get("key") or shortcode_of(record.get("url"))
        if not self._on_post(code):
            # Not on the post, so the thread cannot be asked. Never "no".
            raise ie.InstagramRefused("already_commented: the page is not post %s; "
                                      "classify it first" % code)
        items = self._thread()
        for authors, texts in items:
            if self.handle in authors:
                return ea.ThreadCheck(True, "a comment by @%s is in the thread"
                                      % self.handle, evidence=texts)
        return ea.ThreadCheck(False, "no comment by @%s among the %d shown (Instagram "
                              "shows part of a long thread)" % (self.handle, len(items)))

    def comment_seen(self, text) -> bool:
        """The verifier: a comment by our handle whose text is exactly ``text``
        (whitespace collapsed) is in the thread."""
        want = _norm(text)
        return any(self.handle in authors and want in texts
                   for authors, texts in self._thread())

    # ── like ────────────────────────────────────────────────────────────────

    def like_state(self):
        """``(like controls, liked markers)`` in the post's action bar."""
        return (self._first(self.LIKE_BUTTON_SELECTORS),
                self._first(self.LIKED_STATE_SELECTORS))

    def liked_seen(self) -> bool:
        """The like verifier: exactly one liked marker and no like control."""
        likes, liked = self.like_state()
        return len(liked) == 1 and not likes

    def like(self, record) -> ea.LikeResult:
        drv = self.driver
        likes, liked = self.like_state()
        counts = {"like": len(likes), "unlike": len(liked)}
        if liked and not likes:
            if len(liked) == 1:
                return ea.LikeResult(ea.LIKE_ALREADY, True, "the liked state was on "
                                     "the page", evidence=counts)
        if len(likes) == 1 and not liked:
            self.hands.click(drv, likes[0])
            if self._poll(self.LIKE_VERIFY_SECONDS, self.liked_seen):
                return ea.LikeResult(ea.LIKE_PLACED, True, "the liked state was read "
                                     "after the click", evidence=counts)
            self._capture("like_unverified")
            return ea.LikeResult(ea.LIKE_MISS, False, "like_unverified: clicked, the "
                                 "liked state was not seen within %.0fs"
                                 % self.LIKE_VERIFY_SECONDS, evidence=counts)
        if not likes and not liked:
            self._capture("like_control_absent")
            return ea.LikeResult(ea.LIKE_MISS, False, "like_control_absent",
                                 evidence=counts)
        self._capture("like_ambiguous")
        return ea.LikeResult(ea.LIKE_MISS, False, "like_ambiguous: %d like and %d "
                             "unlike controls in the action bar" % (len(likes), len(liked)),
                             evidence=counts)

    # ── post and verify ─────────────────────────────────────────────────────

    def post_and_verify(self, record) -> ea.PostResult:
        text = record.get("comment") or ""
        reason = ea.approval_refusal_for(record, text, strip=False)
        if reason:
            return ea.PostResult(False, ea.FAILED, reason="not_approved: %s" % reason)
        drv = self.driver
        if any(ord(ch) > 0xFFFF for ch in text):
            return ea.PostResult(False, ea.FAILED, reason=(
                "untypeable: ChromeDriver cannot type characters outside the Basic "
                "Multilingual Plane (most emoji); nothing was typed"))
        code = record.get("key") or shortcode_of(record.get("url"))
        if not self._on_post(code):
            return ea.PostResult(False, ea.FAILED, reason="not_on_post: classify first")
        if not self.identity_proven():
            self._capture("post_identity_unproven")
            return ea.PostResult(False, ea.FAILED, reason="identity_unproven")
        boxes = self._poll(self.INPUT_SECONDS, lambda: self._first(self.COMMENT_INPUT_SELECTORS))
        if not boxes or len(boxes) != 1:
            self._capture("comment_input")
            return ea.PostResult(False, ea.FAILED, reason="comment_input_%s" % (
                "absent" if not boxes else "ambiguous: %d boxes" % len(boxes)))
        box = boxes[0]
        self.hands.type(drv, box, text)
        typed = box.get_attribute("value") or ""
        if typed != text:
            self._capture("typed_text_differs")
            return ea.PostResult(False, ea.FAILED, reason="typed_text_differs: the box "
                                 "holds %d chars, not the %d approved" % (len(typed), len(text)))
        submits = [el for sel in self.COMMENT_SUBMIT_SELECTORS for el in self._find(sel)
                   if _norm(el.text) == self.COMMENT_SUBMIT_TEXT]
        if len(submits) != 1:
            self._capture("comment_submit")
            return ea.PostResult(False, ea.FAILED, reason="comment_submit_%s" % (
                "absent" if not submits else "ambiguous: %d" % len(submits)))
        self.hands.click(drv, submits[0])
        if not self._poll(self.COMMENT_VERIFY_SECONDS, lambda: self.comment_seen(text)):
            self._capture("comment_not_seen")
            return ea.PostResult(False, ea.FAILED, ambiguous=True, reason=(
                "comment_not_seen: submitted, no comment by @%s with this text in the "
                "thread within %.0fs; it may exist, never retried"
                % (self.handle, self.COMMENT_VERIFY_SECONDS)))
        # Instagram draws a new comment before the server has kept it, so the
        # first sighting can be the page's own echo. The proof is the thread
        # as the server sends it: reload and read again.
        drv.get(post_url(code))
        if self._poll(self.COMMENT_VERIFY_SECONDS,
                      lambda: self._on_post(code) and self.comment_seen(text)):
            return ea.PostResult(True, ea.POSTED, proof={"author": self.handle,
                                                         "text": _norm(text),
                                                         "after_reload": True})
        self._capture("comment_gone_after_reload")
        return ea.PostResult(False, ea.FAILED, ambiguous=True, reason=(
            "comment_gone_after_reload: shown after submit, not in the thread after "
            "a reload within %.0fs; it may exist, never retried"
            % self.COMMENT_VERIFY_SECONDS))

    def selectors(self):
        return registry_entries()


# ─── selector_health entries ─────────────────────────────────────────────────

def _entry(selectors, page, fix_symbol, note, critical=True, min_expected=1):
    return {"selectors": list(selectors), "min_expected": min_expected,
            "critical": critical, "page": page, "fix_symbol": fix_symbol,
            "verified": False, "provenance": PROVENANCE,
            "note": "UNVERIFIED. " + note}


def registry_entries() -> Dict[str, Dict]:
    """The adapter's selector_health entries. Every one is ``verified: False``
    until a live pass and a live comment confirm it (E4). Pages are
    Instagram-only (``ig_feed``, ``ig_post``, ``ig_profile``), so no LinkedIn
    or X check ever counts them."""
    A = InstagramAdapter
    item = A.COMMENT_ITEM_SELECTORS[0]
    return {
        "ig_feed_logged_in": _entry(A.LOGGED_IN_SELECTORS, "ig_feed",
                                    "InstagramAdapter.LOGGED_IN_SELECTORS",
                                    "the signed-in marker"),
        "ig_feed_login_form": _entry(A.LOGIN_FORM_SELECTORS, "ig_feed",
                                     "InstagramAdapter.LOGIN_FORM_SELECTORS",
                                     "detected only, never typed into; absent when "
                                     "signed in", critical=False, min_expected=0),
        "ig_feed_post": _entry(A.FEED_POST_SELECTORS, "ig_feed",
                               "InstagramAdapter.FEED_POST_SELECTORS", "a feed post"),
        "ig_feed_permalink": _entry(A.PERMALINK_SELECTORS, "ig_feed",
                                    "InstagramAdapter.PERMALINK_SELECTORS",
                                    "the /p/<shortcode>/ links find collects"),
        "ig_feed_identity": _entry(A.IDENTITY_AVATAR_SELECTORS, "ig_feed",
                                   "InstagramAdapter.IDENTITY_AVATAR_SELECTORS",
                                   "the code formats IDENTITY_LINK_TEMPLATE with "
                                   "identity_slug; this counts any avatar link"),
        "ig_post_container": _entry(A.POST_CONTAINER_SELECTORS, "ig_post",
                                    "InstagramAdapter.POST_CONTAINER_SELECTORS",
                                    "proves the post rendered"),
        "ig_post_url_meta": _entry(A.POST_URL_META_SELECTORS, "ig_post",
                                   "InstagramAdapter.POST_URL_META_SELECTORS",
                                   "names the shortcode the page rendered"),
        "ig_post_caption_meta": _entry(A.CAPTION_META_SELECTORS, "ig_post",
                                       "InstagramAdapter.CAPTION_META_SELECTORS",
                                       "author and caption"),
        "ig_post_identity": _entry(A.IDENTITY_AVATAR_SELECTORS, "ig_post",
                                   "InstagramAdapter.IDENTITY_AVATAR_SELECTORS",
                                   "the signed-in account's link, outside the post"),
        "ig_post_gone": _entry(A.GONE_SELECTORS, "ig_post",
                               "InstagramAdapter.GONE_SELECTORS",
                               "the only gone signal; absent on a healthy post, so "
                               "a live pass cannot confirm it (checked offline "
                               "against ig_post_gone.html)",
                               critical=False, min_expected=0),
        "ig_post_like": _entry(A.LIKE_BUTTON_SELECTORS + A.LIKED_STATE_SELECTORS,
                               "ig_post", "InstagramAdapter.LIKE_BUTTON_SELECTORS",
                               "either the like control or the liked state counts",
                               critical=False),
        "ig_comment_input": _entry(A.COMMENT_INPUT_SELECTORS, "ig_post",
                                   "InstagramAdapter.COMMENT_INPUT_SELECTORS",
                                   "the comment box"),
        "ig_comment_submit": _entry(A.COMMENT_SUBMIT_SELECTORS, "ig_post",
                                    "InstagramAdapter.COMMENT_SUBMIT_SELECTORS",
                                    "text-matched %r, exactly one in the form. If "
                                    "Instagram renders it only after typing, a live "
                                    "pass reads 0 here and the gate stays shut"
                                    % A.COMMENT_SUBMIT_TEXT),
        "ig_comment_item": _entry(A.COMMENT_ITEM_SELECTORS, "ig_post",
                                  "InstagramAdapter.COMMENT_ITEM_SELECTORS",
                                  "the thread; the verifier reads it, so run the "
                                  "health check on a post with comments"),
        "ig_comment_author": _entry(["%s %s" % (item, A.COMMENT_AUTHOR_IN_ITEM)],
                                    "ig_post",
                                    "InstagramAdapter.COMMENT_AUTHOR_IN_ITEM",
                                    "read inside each comment item"),
        "ig_comment_text": _entry(["%s %s" % (item, A.COMMENT_TEXT_IN_ITEM)], "ig_post",
                                  "InstagramAdapter.COMMENT_TEXT_IN_ITEM",
                                  "read inside each comment item"),
        "ig_profile_meta": _entry(A.PROFILE_META_SELECTORS, "ig_profile",
                                  "InstagramAdapter.PROFILE_META_SELECTORS",
                                  "the follower count for the account filter"),
        "ig_profile_header": _entry(A.PROFILE_HEADER_SELECTORS, "ig_profile",
                                    "InstagramAdapter.PROFILE_HEADER_SELECTORS",
                                    "proves the header rendered, so a missing badge "
                                    "means not verified"),
        "ig_profile_verified": _entry(A.VERIFIED_BADGE_SELECTORS, "ig_profile",
                                      "InstagramAdapter.VERIFIED_BADGE_SELECTORS",
                                      "absent on an unverified account",
                                      critical=False, min_expected=0),
    }


IG_PAGES = ("ig_feed", "ig_post", "ig_profile")


def selectors_fingerprint(entries=None) -> str:
    entries = entries if entries is not None else registry_entries()
    payload = json.dumps({k: v["selectors"] for k, v in sorted(entries.items())},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── og-meta parsing (the reference's idea, rewritten) ───────────────────────

_POST_DESC_RE = re.compile(
    r"^\s*[\d.,]+[KkMm]?\s+likes?,\s*[\d.,]+[KkMm]?\s+comments?\s+-\s+"
    r"(?P<handle>[A-Za-z0-9._]{1,30})\s+on\s+[^:]{1,60}:\s*(?P<caption>.*)$", re.DOTALL)
_FOLLOWERS_RE = re.compile(r"([\d][\d.,]*\s*[KkMmBb]?)\s+Followers\b")


def parse_post_description(desc):
    m = _POST_DESC_RE.match(desc or "")
    if not m:
        return {"author_handle": "", "caption": ""}
    caption = m.group("caption").strip()
    if len(caption) >= 2 and caption[0] in "\"“" and caption[-1] in "\"”.":
        caption = caption[1:].rstrip(".").rstrip("\"”")
    return {"author_handle": m.group("handle").lower(), "caption": caption.strip()}


def parse_followers(desc) -> Optional[int]:
    m = _FOLLOWERS_RE.search(desc or "")
    return ie.parse_count(m.group(1).replace(" ", "")) if m else None


def _record(code, meta, account, why):
    now = post_store._now()
    return {
        "key": code, "url": post_url(code),
        "author": meta.get("author_handle") or "", "text": meta.get("caption") or "",
        "category": "post", "relevance_score": 0,
        "status": post_store.TRASH if why else post_store.NEW,
        "trash_reason": TRASH_ACCOUNT_SKIPPED if why else None,
        "comment": None, "comment_meta": None, "scraped_at": now,
        "generated_at": None, "commented_at": None, "reviewed_at": None,
        "approved_hash": None, "updated_at": now,
        "instagram": {"shortcode": code, "author_handle": meta.get("author_handle") or "",
                      "followers": account.get("followers"),
                      "verified": account.get("verified"), "filter_reason": why},
    }


# ─── the live selector-health pass ───────────────────────────────────────────

def live_pass_path(profile):
    return os.path.join(pm.get_data_dir(profile, PLATFORM), LIVE_PASS_FILE)


def live_pass_problem(profile, cfg, now=None) -> Optional[str]:
    """Why the recorded live pass does not open the gate, or None."""
    path = live_pass_path(profile)
    run = "run: %s" % HEALTH_COMMAND.replace("<profile>", profile)
    if not os.path.exists(path):
        return ("no live selector-health pass is recorded for profile %r (%s). The "
                "selectors are unverified; %s" % (profile, path, run))
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as exc:
        return "the live pass record %s is unreadable (%s); %s" % (path, exc, run)
    if not isinstance(rec, dict) or rec.get("schema_version") != LIVE_PASS_SCHEMA \
            or rec.get("source") != "live":
        return "the live pass record %s is not a live run of this version; %s" % (path, run)
    if rec.get("status") != "HEALTHY":
        return "the last live pass was %s, not HEALTHY (failed: %s); %s" % (
            rec.get("status"), ", ".join(rec.get("failed") or []) or "-", run)
    if (rec.get("identity_slug") or "") != cfg.identity_slug:
        return ("the live pass was for @%s, not the configured @%s; %s"
                % (rec.get("identity_slug"), cfg.identity_slug, run))
    if rec.get("selectors_fingerprint") != selectors_fingerprint():
        return "the selectors changed since the live pass; %s" % run
    if sorted(rec.get("pages") or []) != sorted(IG_PAGES):
        return "the live pass did not check every Instagram page; %s" % run
    try:
        at = datetime.strptime(rec.get("checked_at") or "", "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return "the live pass has no valid checked_at; %s" % run
    now = now or _now()
    if now - at > LIVE_PASS_MAX_AGE or at > now + timedelta(minutes=5):
        return "the live pass from %s is older than %d days; %s" % (
            rec.get("checked_at"), LIVE_PASS_MAX_AGE.days, run)
    return None


def _count_fn(driver):
    from . import selector_health as sh

    def count(sel):
        return len(driver.find_elements(sh.selector_by(sel), sel))
    return count


def run_live_health_check(profile, post_url_, driver=None, now=None, out=print):
    """The live pass: read-only. Opens the feed, the given post and our own
    profile, counts every Instagram registry entry and writes the record the
    gate reads. Clicks nothing and types nothing. Returns ``(exit_code,
    result)``: 0 when HEALTHY."""
    from . import selector_health as sh
    cfg = ie.instagram_config(profile)
    if not cfg.identity_slug:
        out("REFUSED: set scheduled_posting.platforms.instagram.identity_slug for "
            "profile %r to the dev account's handle first" % profile)
        return 2, None
    pm.check_declared_production(cfg.identity_slug,
                                 "run the Instagram health check for %r" % profile)
    code = shortcode_of(post_url_)
    if not code:
        out("REFUSED: --post-url must be https://www.instagram.com/p/<shortcode>/")
        return 2, None
    adapter = InstagramAdapter(profile, driver=driver, cfg=cfg)
    if driver is None:
        driver, _info = pm.create_driver(profile)
        adapter._driver = driver
    entries = registry_entries()
    checks = {}
    notes = []

    def run(page, url, ready):
        driver.get(url)
        adapter._poll(adapter.NAV_SECONDS, ready)
        page_entries = {k: v for k, v in entries.items() if v["page"] == page}
        checks.update(sh.check_registry(_count_fn(driver), page_entries))

    run("ig_feed", FEED_URL, lambda: adapter._first(adapter.LOGGED_IN_SELECTORS)
        or adapter._first(adapter.LOGIN_FORM_SELECTORS))
    signed_in = bool(adapter._first(adapter.LOGGED_IN_SELECTORS)) and not adapter.logged_out()
    identity = signed_in and adapter.identity_proven()
    if not signed_in:
        notes.append("signed out: sign in to Instagram by hand in this profile's "
                     "browser, then run this again")
    elif not identity:
        notes.append("the signed-in account is not @%s" % cfg.identity_slug)
    run("ig_post", post_url(code), lambda: adapter._on_post(code))
    run("ig_profile", "%s/%s/" % (BASE_URL, cfg.identity_slug),
        lambda: adapter._first(adapter.PROFILE_HEADER_SELECTORS))
    status = sh.overall_status(checks)
    if not identity:
        status = "BROKEN"
    when = now or _now()
    result = {
        "schema_version": LIVE_PASS_SCHEMA, "source": "live", "platform": PLATFORM,
        "profile": profile, "identity_slug": cfg.identity_slug,
        "identity_proven": identity, "checked_at": _iso(when),
        "selectors_fingerprint": selectors_fingerprint(entries),
        "pages": list(IG_PAGES), "status": status,
        "failed": sorted(k for k, c in checks.items() if not c["ok"]),
        "notes": notes,
        "checks": {k: {"ok": c["ok"], "count": c["count"],
                       "matched_selector": c["matched_selector"],
                       "critical": c["critical"]} for k, c in checks.items()},
    }
    path = live_pass_path(profile)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    os.replace(tmp, path)
    if status != "HEALTHY":
        adapter._capture("health_" + status.lower())
    out("instagram health %s: %s (failed: %s)%s. Written to %s" % (
        profile, status, ", ".join(result["failed"]) or "none",
        "; " + "; ".join(notes) if notes else "", path))
    return (0 if status == "HEALTHY" else 1), result


# ─── one item, end to end ────────────────────────────────────────────────────

def engage_item(profile, key, adapter, like=True):
    """Post one approved Instagram comment, the shared way. Returns
    ``(EngageResult or None, line)``.

    Order: the E0 gate on a fresh read; the E2 validator (a refusal sends
    the draft back to review); the live gate; ``engage_one`` with a fresh
    store read as the last step before posting. The store records the
    outcome: posted and already are COMMENTED, gone is UNAVAILABLE, a comment
    sent but not seen is TRASH(instagram_unverified) and never retried,
    everything else stays GENERATED and approved for a later run.
    """
    store = ie.load_store(profile)
    rec = store.get(key)
    reason = ea.approval_refusal_for(rec, (rec or {}).get("comment"), strip=False) \
        if rec else post_store.REFUSE_NO_RECORD
    if reason:
        return ea.EngageResult(ea.ENGAGE_NOT_APPROVED, reason), \
            "%s; %s refused (%s)" % (post_store.not_approved_message(1), key, reason)
    rules = ie.draft_refusal(store, rec, adapter.cfg)
    if rules:
        ie.send_back_to_review(store, rec, rules)
        return ea.EngageResult(ea.ENGAGE_NOT_APPROVED, rules), \
            "0 posted: %s %s; back to review" % (key, rules)
    adapter.check_gate()
    result = ea.engage_one(adapter, rec, like=like,
                           reload=lambda k: ie.load_store(profile).get(k))
    store = ie.load_store(profile)
    rec = store.get(key)
    ig = rec.setdefault("instagram", {})
    now = post_store._now()
    if result.like is not None:
        ig["like"] = {"state": result.like.state, "proven": result.like.proven,
                      "reason": result.like.reason}
    if result.outcome == ea.ENGAGE_POSTED:
        rec.update(status=post_store.COMMENTED, commented_at=now, updated_at=now)
        ig.update(outcome="posted", proof=result.post.proof)
        line = "posted 1: %s" % key
    elif result.outcome == ea.ENGAGE_ALREADY:
        rec.update(status=post_store.COMMENTED, commented_at=now, updated_at=now)
        ig.update(outcome="already_commented")
        line = "0 posted: %s already has our comment; marked COMMENTED" % key
    elif result.outcome == ea.ENGAGE_GONE:
        store.mark_unavailable(key, result.reason)
        line = "0 posted: %s is gone (%s)" % (key, result.reason)
    elif result.outcome == ea.ENGAGE_FAILED and result.post and result.post.ambiguous:
        rec.update(status=post_store.TRASH, trash_reason=TRASH_UNVERIFIED, updated_at=now)
        ig.update(outcome="failed_ambiguous", error=result.reason)
        line = "0 posted: %s %s. Check the post on Instagram." % (key, result.reason)
    else:
        ig.update(outcome=result.outcome, error=result.reason)
        rec["updated_at"] = now
        line = "0 posted: %s %s (%s)" % (key, result.outcome, result.reason)
    store.save()
    return result, line


# ─── CLI: the live health pass only ──────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m linkedin_automation.instagram_adapter",
        description="Instagram adapter tools. 'health' is the read-only live "
                    "selector-health pass the adapter's live gate requires.")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("health", help="record a live selector-health pass")
    h.add_argument("--profile", required=True)
    h.add_argument("--post-url", required=True,
                   help="a post with at least one comment, on someone else's account")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        code, _result = run_live_health_check(args.profile, args.post_url)
    except (ie.InstagramRefused, pm.ProductionAccessRefused,
            pm.PublishingOnlyProfileError, ValueError) as exc:
        print("REFUSED: %s" % exc)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
