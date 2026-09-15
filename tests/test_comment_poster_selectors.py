"""Tests for the comment poster's selector constants (2026-07-30 posting break).

No browser: these assert the selector constants themselves — their content and
their ordering — which is all that was wrong.

**The bug.** LinkedIn moved the post permalink page to ``data-testid``
attributes. ``post_finder`` was migrated; ``comment_poster``'s inline permalink
selectors were not. All four legacy class selectors returned 0 matches against a
live permalink page, so ``navigate_to_post`` burned 4 x 20s of ``WebDriverWait``
and then failed with "Post content not found on page" — on *every* post. Scraping
kept working, so the pipeline looked healthy right up to "Posted 0, skipped 3".

Live counts verified 2026-07-30 (see .dev/AUDIT_student_fork.md):

    span[data-testid='expandable-text-box'] -> 1    div.occludable-update       -> 0
    div[role='listitem']                    -> 1    div.feed-shared-update-v2   -> 0
                                                    article.feed-shared-article -> 0
                                                    div[data-urn*='activity']   -> 0
"""

from linkedin_automation.comment_poster import LinkedInCommentPoster as P

# The four selectors that were the entire list, and all read 0 on a live page.
LEGACY_PERMALINK_SELECTORS = [
    "div.occludable-update",
    "div.feed-shared-update-v2",
    "article.feed-shared-article",
    "div[data-urn*='activity']",
]

# The two that actually matched.
VERIFIED_MODERN_SELECTORS = [
    "span[data-testid='expandable-text-box']",
    "div[role='listitem']",
]


# ─── The regression ───────────────────────────────────────────────────────────

def test_a_verified_modern_selector_is_present():
    """Without one of these, every post fails to load. This is the bug itself."""
    assert any(sel in P.POST_DETAIL_SELECTORS for sel in VERIFIED_MODERN_SELECTORS)


def test_no_legacy_selector_is_tried_first():
    """A stale selector in front costs a full WebDriverWait timeout per post."""
    assert P.POST_DETAIL_SELECTORS[0] not in LEGACY_PERMALINK_SELECTORS
    assert P.POST_DETAIL_SELECTORS[0] in VERIFIED_MODERN_SELECTORS


def test_the_legacy_selectors_are_kept_as_fallbacks():
    """New-first / old-fallback: they cost nothing once a working one matches."""
    for sel in LEGACY_PERMALINK_SELECTORS:
        assert sel in P.POST_DETAIL_SELECTORS
    first_legacy = min(P.POST_DETAIL_SELECTORS.index(s) for s in LEGACY_PERMALINK_SELECTORS)
    last_modern = max(P.POST_DETAIL_SELECTORS.index(s) for s in VERIFIED_MODERN_SELECTORS)
    assert last_modern < first_legacy, "every modern selector must precede every legacy one"


def test_main_is_not_used_as_a_post_detail_selector():
    """'main' matches on error pages too, so it would report a post that never loaded."""
    assert "main" not in P.POST_DETAIL_SELECTORS


# ─── Hoisting (what lets the watchdog see this path at all) ───────────────────

def test_the_posting_path_selectors_are_class_constants():
    """selector_health builds its registry from class constants.

    A selector inlined in a method body is invisible to the monitor, which is why
    the posting path went unwatched while the scraping path was covered.
    """
    for name in ("POST_DETAIL_SELECTORS", "LIKE_BUTTON_SELECTORS",
                 "LIKED_STATE_SELECTORS", "COMMENT_BUTTON_LABEL_SELECTORS",
                 "COMMENT_INPUT_SELECTORS", "SUBMIT_BUTTON_FALLBACK_SELECTORS"):
        assert isinstance(getattr(P, name), list) and getattr(P, name)
    assert isinstance(P.SUBMIT_BUTTON_XPATH, str)
    assert isinstance(P.COMMENT_BUTTON_TEXT_SELECTOR, str)


def test_no_posting_selector_is_still_inlined_in_a_method():
    """Guards the hoisting from regressing: literals must not creep back in."""
    import inspect
    src = inspect.getsource(P)
    body = src.split("def __init__", 1)[1]  # everything after the constants block
    for literal in ("div.occludable-update", "div.ql-editor[contenteditable='true']",
                    "span.artdeco-button__text", "div.comments-comment-item"):
        assert literal not in body, f"{literal!r} is inlined in a method again"


# ─── The submit button vs the button that opens the box ──────────────────────

def test_the_submit_button_is_matched_by_visible_text_not_class():
    """LinkedIn ships hashed class names that change between deploys."""
    assert "normalize-space" in P.SUBMIT_BUTTON_XPATH
    assert P.SUBMIT_BUTTON_XPATH.startswith("//button")


def test_submit_and_open_buttons_are_distinguishable():
    """Both say 'Comment'; only the aria-label separates them.

    The action-bar button that OPENS the box carries aria-label="Comment"; the
    SUBMIT button has the visible text and no aria-label. Confusing the two
    clicks the wrong control and silently posts nothing.
    """
    assert P.SUBMIT_BUTTON_EXCLUDED_ARIA_LABEL == "Comment"
    assert any("aria-label" in sel for sel in P.COMMENT_BUTTON_LABEL_SELECTORS)
    assert "aria-label" not in P.SUBMIT_BUTTON_XPATH
