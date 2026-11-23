"""LinkedIn's engagement adapter (PLATFORM_REFACTOR §9, D-027 E1).

The LinkedIn side of ``engagement_adapter.EngagementAdapter``, and the home of
what E0-A listed as LinkedIn-specific in the shared engagement path:

* the TXT handoff to the poster subprocess (``write_comments_file``,
  ``write_handoff``), which used to live in ``scheduler`` and ``dashboard``;
* the poster and finder subprocess command lines, with ``--allow-production``
  (the dashboard is always an authorized caller, ARCHITECTURE §9), which used
  to be spelled out in ``dashboard``;
* the post identity: the URN types a LinkedIn URL is keyed on
  (``LinkedInCommentPoster.POST_IDENTITY_TYPES``), via ``native_id``;
* the ``posting_progress.json`` ledger the store reconciles COMMENTED and
  UNAVAILABLE from (``LEDGER_KEYS``), and what UNAVAILABLE and the auto trash
  reasons mean here (``TRASH_REASONS``);
* the posting-path selector_health entries (``registry_entries``), which
  selector_health now takes from here.

**No behaviour of its own.** Every method delegates to the code that already
did the work: ``LinkedInCommentPoster`` (navigate/classify, the thread check,
the like, post-and-verify) and ``LinkedInAIPostFinder`` (find). The batch run
is still ``LinkedInCommentPoster.run`` in a subprocess, its loop unchanged; the
golden in ``tests/test_e1_golden.py`` holds every decision to the pre-E1 code.

Selenium is imported lazily (inside the methods), so the scheduler and the
dashboard, which import this module for the handoff, load no browser code.
"""

import os
import sys

from . import engagement_adapter as ea
from . import post_store
from . import profile_manager as pm
from .comment_fields import comments_to_txt
from .platforms import LINKEDIN

#: The subprocess modules the dashboard runs.
POSTER_MODULE = "linkedin_automation.comment_poster"
FINDER_MODULE = "linkedin_automation.post_finder"

#: The keys of ``posting_progress.json`` the poster writes and the store
#: reconciles from (post_store.sync_with_progress).
LEDGER_KEYS = ("posted_comments", "unavailable_posts", "failed_comments",
               "skipped_already_commented")

#: Auto trash reasons that only LinkedIn's finder produces.
TRASH_REASONS = (post_store.REASON_AD, post_store.REASON_JOB)

#: selector_health entries this adapter's methods depend on: the finder's
#: (feed) and the poster's (post page). The poster's five are defined below.
FIND_REGISTRY_KEYS = ("feed_container", "post_text", "author",
                      "scroll_container", "overflow_menu", "copy_link_item")
POST_REGISTRY_KEYS = ("post_detail", "post_like_button", "comment_open_button",
                      "comment_input", "comment_submit_button")


# ─── The handoff and the subprocess commands ─────────────────────────────────

def poster_command(comments_file, count, profile_name):
    """argv of the poster subprocess for ``count`` comments from a TXT file."""
    return [
        sys.executable, "-m", POSTER_MODULE,
        comments_file,
        "--count", str(count),
        "--profile", profile_name,
        # See finder_command: the dashboard is always an authorized caller.
        "--allow-production",
    ]


def finder_command(max_posts, min_quality, profile_name):
    """argv of the post finder subprocess."""
    return [
        sys.executable, "-m", FINDER_MODULE,
        "--max-posts", str(max_posts),
        "--min-quality", str(min_quality),
        "--profile", profile_name,
        # The dashboard IS production doing its job (manual click or
        # scheduler fire, both routed through this same job body) - unlike an
        # ad-hoc CLI/module run, it is always authorized. See docs/ARCHITECTURE.md §9.
        "--allow-production",
    ]


def write_handoff(profile, comments, filename, header):
    """Write poster-readable ``comments`` to ``<comments dir>/<filename>``."""
    path = os.path.join(pm.get_comments_dir(profile), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(comments_to_txt(comments, header))
    return path


def handoff_comment(rec):
    """One store record as the scheduler's TXT block fields."""
    meta = rec.get("comment_meta") or {}
    return {
        "url": rec.get("url", ""),
        "author": rec.get("author", ""),
        "post_text": rec.get("text", ""),
        "category": rec.get("category", ""),
        "comment": rec.get("comment", ""),
        "word_count": meta.get("word_count"),
        "style": meta.get("style", ""),
        "approach": meta.get("approach", ""),
    }


def write_comments_file(profile, records, now):
    """Serialize GENERATED records to a poster-readable TXT file.

    Shared by the scheduler's ``_fire_post_comments`` (the independent
    post_comments job) and the auto_cycle post stage (dashboard's real
    ``run_post`` closure) - one serialization, used by both. Re-exported as
    ``scheduler.write_comments_file``.
    """
    ts = now.strftime("%Y%m%d_%H%M%S")
    return write_handoff(profile, [handoff_comment(r) for r in records],
                         f"scheduled_comments_{ts}.txt",
                         now.strftime("%Y-%m-%d %H:%M"))


# ─── selector_health ─────────────────────────────────────────────────────────

def registry_entries(comment_box_witness):
    """The poster's selector_health entries, in registry order.

    ``comment_box_witness`` is selector_health's ``COMMENT_BOX_SELECTOR``: the
    state witness proving the comment box is open, which the two
    box-dependent entries are only checked behind.
    """
    from .comment_poster import LinkedInCommentPoster as P
    return {
        "post_detail": {
            "selectors": list(P.POST_DETAIL_SELECTORS),
            "min_expected": 1, "critical": True, "page": "post",
            "fix_symbol": "LinkedInCommentPoster.POST_DETAIL_SELECTORS",
            "note": "proves the permalink page rendered; all 4 legacy entries read 0 on 2026-07-30",
        },
        "post_like_button": {
            "selectors": list(P.LIKE_BUTTON_SELECTORS)
            + list(P.LIKED_STATE_SELECTORS),
            "min_expected": 1, "critical": False, "page": "post",
            "fix_symbol": "LinkedInCommentPoster.LIKE_BUTTON_SELECTORS",
            "note": "either an un-liked Like button or the already-liked state counts",
        },
        "comment_open_button": {
            "selectors": list(P.COMMENT_BUTTON_LABEL_SELECTORS)
            + [P.COMMENT_BUTTON_TEXT_SELECTOR],
            "min_expected": 1, "critical": True, "page": "post",
            "fix_symbol": "LinkedInCommentPoster.COMMENT_BUTTON_LABEL_SELECTORS",
            "note": "the action-bar button that opens the comment box",
        },
        "comment_input": {
            "selectors": list(P.COMMENT_INPUT_SELECTORS),
            "min_expected": 1, "critical": True, "page": "post",
            "requires_comment_box": True,
            "state_witness": comment_box_witness,
            "fix_symbol": "LinkedInCommentPoster.COMMENT_INPUT_SELECTORS",
            "note": "the editor; only present after the comment box is opened",
        },
        "comment_submit_button": {
            "selectors": [P.SUBMIT_BUTTON_XPATH]
            + list(P.SUBMIT_BUTTON_FALLBACK_SELECTORS),
            "min_expected": 1, "critical": True, "page": "post",
            "requires_comment_box": True,
            "state_witness": comment_box_witness,
            "fix_symbol": "LinkedInCommentPoster.SUBMIT_BUTTON_XPATH / SUBMIT_BUTTON_FALLBACK_SELECTORS",
            "note": ("first entry is an XPath matched on visible text; only present "
                     "after the comment box is opened, and NEVER clicked by the check"),
        },
    }


# ─── The adapter ─────────────────────────────────────────────────────────────

class LinkedInAdapter(ea.EngagementAdapter):
    """LinkedIn through Selenium: a wrapper over the poster and the finder.

    ``poster`` is a ``LinkedInCommentPoster`` with a driver set up, or None to
    build one for ``profile_name`` on first use. ``classify`` navigates, so
    ``already_commented``, ``like`` and ``post_and_verify`` act on the post
    the last ``classify`` opened, as the poster's own loop does.
    """

    platform = LINKEDIN
    kind = ea.BROWSER

    def __init__(self, profile_name=None, poster=None, allow_production=False):
        self.profile_name = profile_name
        self.allow_production = allow_production
        self._poster = poster

    @property
    def poster(self):
        if self._poster is None:
            from .comment_poster import LinkedInCommentPoster
            self._poster = LinkedInCommentPoster(
                profile_name=self.profile_name,
                allow_production=self.allow_production)
        return self._poster

    # ── find ──────────────────────────────────────────────────────────────

    def find(self, max_posts=50, min_quality=10, finder=None):
        """Scrape the feed; quality posts land NEW, the rest TRASH. Returns
        the finder's output file, or None. Opens its own browser."""
        if finder is None:
            from .post_finder import LinkedInAIPostFinder
            finder = LinkedInAIPostFinder(profile_name=self.profile_name,
                                          allow_production=self.allow_production)
        return finder.run(max_posts, min_quality)

    # ── classify ──────────────────────────────────────────────────────────

    def native_id(self, record):
        """The type-qualified post id (``activity:<id>``) the poster keys on."""
        return self.poster.post_identity((record or {}).get("url") or "")

    def classify(self, record):
        p = self.poster
        url = record.get("url") or record.get("key") or ""
        if p.navigate_to_post(url):
            return ea.Classification(ea.OK)
        nav = getattr(p, "last_navigation", None) or {}
        outcome = nav.get("outcome")
        if outcome == p.NAV_UNAVAILABLE:
            return ea.Classification(ea.GONE, nav.get("reason"), detail=outcome)
        # NAV_TIMEOUT and NAV_UNCLEAR, and a navigation error: retryable.
        return ea.Classification(ea.UNCLEAR, nav.get("reason"), detail=outcome)

    # ── the thread ────────────────────────────────────────────────────────

    def already_commented(self, record):
        already, why = self.poster.already_commented_here(record.get("comment"))
        return ea.ThreadCheck(bool(already), why)

    # ── like ──────────────────────────────────────────────────────────────

    def like(self, record):
        p = self.poster
        p.last_like_outcome = None
        p.like_post()
        state = getattr(p, "last_like_outcome", None) or ea.LIKE_MISS
        if state == ea.LIKE_ALREADY:
            return ea.LikeResult(state, True, "a known liked state is on the page")
        if state == ea.LIKE_PLACED:
            # The click was on the un-liked control; the liked state is not
            # re-read after it. Said plainly rather than claimed.
            return ea.LikeResult(state, False,
                                 "clicked the un-liked Like; the liked state "
                                 "is not re-read after the click")
        return ea.LikeResult(ea.LIKE_MISS, False, "no Like control and no known "
                             "liked state (captured as a like miss)")

    # ── post ──────────────────────────────────────────────────────────────

    def post_and_verify(self, record):
        p = self.poster
        if p.post_comment(record.get("comment") or ""):
            return ea.PostResult(True, ea.POSTED,
                                 proof="the comment text is in the thread")
        return ea.PostResult(False, ea.FAILED, reason="comment_not_posted",
                             proof=getattr(p, "last_failure_evidence", None))

    # ── selectors ─────────────────────────────────────────────────────────

    def selectors(self):
        """The finder's entries as selector_health holds them, plus the
        poster's as this module defines them (selector_health takes those
        from ``registry_entries``)."""
        from . import selector_health as sh
        entries = {k: sh.SELECTOR_REGISTRY.get(k) for k in FIND_REGISTRY_KEYS}
        entries.update(registry_entries(sh.COMMENT_BOX_SELECTOR))
        return entries
