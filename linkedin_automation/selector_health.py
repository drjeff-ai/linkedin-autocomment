"""linkedin_automation/selector_health.py — detect broken LinkedIn selectors and generate
diagnostics for a human + Claude Code to fix them.

Opens the profile's Chrome session and checks every selector the automation
relies on, pulled from the classes that actually use them so the registry stays
in sync. Reports HEALTHY / DEGRADED / BROKEN, and when something breaks it dumps
the current DOM, suggests replacement selectors, and (if a critical selector
failed) writes ``.dev/SELECTOR_FIX_NEEDED.md`` with a ready-to-paste fix prompt.
It NEVER auto-edits selectors.

The registry spans three pages, because each lives somewhere different and no
single run can reach them all:

* ``page="feed"`` — the scraper (:class:`LinkedInScraper`), checked by default.
* ``page="search"`` — the connector, via ``--search-url``.
* ``page="post"`` — the comment poster, via ``--post-url``.

The posting path was added after it broke undetected: on 2026-07-30 every
post-detail selector was dead, a run placed zero of three comments, and a health
check minutes later still reported HEALTHY, because the registry then held only
scraping selectors. A monitor that does not cover the risky path turns a loud
failure into a confident all-clear.

Usage:
    uv run python -m linkedin_automation.selector_health --profile demo
    uv run python -m linkedin_automation.selector_health --profile demo --post-url <permalink>
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from . import dom_probe
from . import profile_manager as pm
from .post_finder import LinkedInScraper
from .auto_connector import LinkedInAutoConnector
from .comment_poster import LinkedInCommentPoster
from .poster import LinkedInPoster
from .failure_capture import capture_failure

logger = logging.getLogger(__name__)

DEBUG_DUMP_FILE = "selector_debug_dump.html"
FIX_NEEDED_FILE = os.path.join(".dev", "SELECTOR_FIX_NEEDED.md")
HEALTH_RESULT_FILE = "selector_health.json"  # written under the profile data dir
POST_HEALTH_RESULT_FILE = "selector_health_post.json"

# Bumped whenever the shape of a health result changes, so a consumer reading a
# stored selector_health*.json can tell whether it understands the file rather
# than guessing from which keys happen to be present. Ported from the fork's
# schema-versioning idea (see .dev/AUDIT_fork_remainder.md §2.4).
#
# 1 — the original ad-hoc shape (no version field at all; absence means 1).
# 2 — adds schema_version, source, and per-check "checked"/"skip_reason".
SCHEMA_VERSION = 2

# A result is only trustworthy if it says what it did NOT look at. Each of these
# marks a registry entry that a plain page-load check cannot reach, together with
# the reason a reader sees instead of a silent pass.
#
# This is the honesty rule that the 2026-07-30 breakage turned into a hard
# requirement: an entry that is absent from a report, or present and passing
# without having been tested, reads as an all-clear it did not earn.
GATE_FLAGS = {
    "requires_menu_open": (
        "not checked: only present after the post's overflow menu is opened"),
    "requires_comment_box": (
        "not checked: this element does not exist until the comment composer "
        "is opened"),
    "requires_composer_open": (
        "not checked: composer must be open — this element does not exist "
        "until 'Start a post' has been clicked"),
    "modal_only": (
        "not checked: only present after the Connect dialog is opened, which "
        "would send an invitation"),
}


def gate_reason(spec: Dict) -> Optional[str]:
    """Return why a registry entry cannot be checked on a plain page load, or None."""
    for flag, reason in GATE_FLAGS.items():
        if spec.get(flag):
            return reason
    return None


def gated_state_present(spec: Dict, count_fn) -> bool:
    """Is the interaction-gated state this entry needs actually rendered?

    Without a witness the only available signal is "did any of this entry's own
    selectors match", and that conflates two very different situations:

        the composer is closed          -> correctly "not checked"
        the composer is OPEN and the Post button was RENAMED -> also "not checked"

    The second is a BROKEN gate reported as a shrug. It is the July breakage
    shape exactly — a hook renamed, nothing raises, the tool finds nothing — and
    the gate flag was quietly hiding it.

    A ``state_witness`` is a selector that proves the state is on screen without
    being one of the selectors under test, so a rename of the thing under test
    fails loudly instead of being excused.

    Entries with no witness keep the older any-selector-matched behaviour.
    """
    witness = spec.get("state_witness")
    if witness:
        try:
            if count_fn(witness):
                return True
        except dom_probe.UnsupportedSelector:
            raise
        except Exception:
            logger.debug("state_witness %r could not be counted", witness,
                         exc_info=True)
    # Falling through to the older any-selector-matched signal on purpose: a
    # witness that goes stale (LinkedIn renames the container too) must degrade
    # to today's behaviour, never to *less* checking than today. A witness can
    # only ever ADD coverage.
    return any(count_fn(sel) for sel in spec["selectors"])


def skipped_entry(key: str, spec: Dict, reason: str = None) -> Dict:
    """Build a check result for an entry that was deliberately NOT tested.

    ``ok`` is True so a gated entry does not read as a failure, but ``checked``
    is False and ``skip_reason`` says why — the two together are what stop a
    reader mistaking "not looked at" for "looked at and fine".
    """
    return {
        "ok": True,
        "checked": False,
        "skip_reason": reason or gate_reason(spec) or "not checked",
        "count": 0,
        "matched_selector": None,
        "counts": {},
        "critical": spec.get("critical", False),
        "min_expected": spec.get("min_expected", 0),
        "note": spec.get("note", ""),
    }


def selector_by(selector: str):
    """Return the Selenium ``By`` strategy a registry selector is written in.

    The registry is overwhelmingly CSS, but the comment SUBMIT button can only be
    told apart from the button that OPENS the comment box by its visible text,
    which CSS cannot express. Rather than give every entry a ``by`` key, the
    strategy is read off the selector's own shape.

    Defers to :func:`dom_probe.is_xpath` so the live counter and the offline
    fixture counter can never disagree about how to read a selector.
    """
    return By.XPATH if dom_probe.is_xpath(selector) else By.CSS_SELECTOR

# Proof that the post composer is actually open, used as the `state_witness` for
# the two gated composer entries. It is deliberately NOT one of the selectors
# under test: its whole job is to tell "composer closed" apart from "composer
# open and the hook was renamed".
#
# Verified against tests/fixtures/composer_open.html. A live composer check does
# not exist yet (no runner clicks the composer open), so this is offline-verified
# only — re-confirm it against a live dump before any live composer run.
COMPOSER_DIALOG_SELECTOR = "div[role='dialog'][aria-label*='post']"

# Proof that the MEDIA EDITOR is open, for the two entries that exist only while
# it is. Like every witness it is deliberately NOT one of the selectors under
# test: its whole job is to tell "the media editor was never opened" apart from
# "it is open and the hook was renamed".
#
# Harvested live 2026-09-04 (run 3) and verified against
# tests/fixtures/composer_image_attached.html.
MEDIA_EDITOR_SELECTOR = "div.media-editor__layout-container"

# The same proof-of-state for the three gated entries that predate the composer.
#
# WHERE THIS HOLE ACTUALLY LIVES: the offline gate. The live post run reaches the
# comment box through _check_comment_box, which calls check_registry directly and
# so already fails loudly on a rename. The live feed run excludes
# requires_menu_open entirely and the live search run excludes modal_only, so
# copy_link_item and connector_send_button have no live check at all — for
# copy_link_item the fixture gate is the ONLY gate it ever gets, which is exactly
# why it must be able to fail.
#
# Both verified against the fixtures named beside them.
MENU_OPEN_SELECTOR = "div[role='menu']"                 # feed_menu_open.html
COMMENT_BOX_SELECTOR = "div[aria-label='Comment box']"  # post_box_open.html

# Registry of every selector the scraper depends on. Selectors are pulled from
# LinkedInScraper so this stays in sync with what the scraper actually uses.
# Each registry key maps to the finder symbol a fixer should edit.
SELECTOR_REGISTRY: Dict[str, Dict] = {
    "feed_container": {
        "selectors": list(LinkedInScraper.POST_SELECTORS),
        "min_expected": 3,
        "critical": True,
        "fix_symbol": "POST_SELECTORS",
    },
    "post_text": {
        "selectors": list(LinkedInScraper.TEXT_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "fix_symbol": "TEXT_SELECTORS",
    },
    "author": {
        "selectors": list(LinkedInScraper.AUTHOR_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "fix_symbol": "AUTHOR_SELECTORS",
    },
    "overflow_menu": {
        "selectors": [LinkedInScraper.CONTROL_MENU_SELECTOR],
        "min_expected": 1,
        "critical": True,
        "fix_symbol": "CONTROL_MENU_SELECTOR",
    },
    "copy_link_item": {
        "selectors": [LinkedInScraper.MENU_ITEM_SELECTORS],
        "min_expected": 1,
        "critical": False,
        "requires_menu_open": True,
        "state_witness": MENU_OPEN_SELECTOR,
        "fix_symbol": "MENU_ITEM_SELECTORS / COPY_LINK_TEXT",
        "note": (f"text-matched '{LinkedInScraper.COPY_LINK_TEXT}'; only present "
                 "after the overflow menu is opened"),
    },
    "scroll_container": {
        "selectors": list(LinkedInScraper.SCROLL_CONTAINER_SELECTORS),
        "min_expected": 1,
        "critical": False,
        "fix_symbol": "SCROLL_CONTAINER_SELECTORS",
    },
    # Connector (auto-connect) selectors live on the people-SEARCH page, not the
    # feed, so the feed health run does not test them (page="search"); they are
    # registered here, in sync with LinkedInAutoConnector's constants, so the
    # watchdog tracks them and a future search-page check can use them.
    # The connector now iterates Connect LINKS directly (link-first), so the
    # Connect link is the one CRITICAL signal that it can work. The card/name
    # checks below are FALLBACK-only (LinkedIn removed data-view-name), so they
    # are non-critical — a 0 there is expected on the current DOM and must not
    # read as BROKEN.
    "connector_connect_link": {
        "selectors": list(LinkedInAutoConnector.CONNECT_LINK_SELECTORS)
        + [LinkedInAutoConnector.CONNECT_ACTION_SELECTOR],
        "min_expected": 1, "critical": True, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.CONNECT_LINK_SELECTORS",
        "note": "link-first PRIMARY: the 'Invite <Name> to connect' links",
    },
    "connector_search_result": {
        "selectors": [LinkedInAutoConnector.SEARCH_RESULT_SELECTOR]
        + list(LinkedInAutoConnector.RESULT_CARD_FALLBACK_SELECTORS),
        "min_expected": 1, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.SEARCH_RESULT_SELECTOR",
        "note": "fallback-only card wrapper (data-view-name removed); link-first is primary",
    },
    "connector_result_name": {
        "selectors": [LinkedInAutoConnector.RESULT_NAME_SELECTOR],
        "min_expected": 1, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.RESULT_NAME_SELECTOR",
        "note": "fallback-only name selector; link-first parses the name from the invite aria-label",
    },
    "connector_pagination": {
        "selectors": list(LinkedInAutoConnector.PAGINATION_NEXT_SELECTORS)
        + [LinkedInAutoConnector.PAGE_INDICATOR_SELECTOR],
        "min_expected": 1, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.PAGINATION_NEXT_SELECTORS",
        "note": "page-2+ Next button / numbered page indicators",
    },
    "connector_send_button": {
        "selectors": list(LinkedInAutoConnector.SEND_BUTTON_SELECTORS)
        + [LinkedInAutoConnector.SEND_BUTTON_LEGACY_SELECTOR],
        "min_expected": 1, "critical": True, "page": "search", "modal_only": True,
        # No state_witness ON PURPOSE. There is no Connect-dialog fixture and the
        # live search run excludes modal_only entries, so this entry is counted
        # nowhere today and any witness would be an unverified guess. Adding one
        # would look like coverage without being it. See .dev/BACKLOG.md.
        "fix_symbol": "LinkedInAutoConnector.SEND_BUTTON_SELECTORS / SEND_BUTTON_LEGACY_SELECTOR",
        "note": ("modal-only: the Send button appears AFTER clicking Connect, so "
                 "it is NOT on the search results page and is skipped by the "
                 "non-clicking search health check. No state_witness: nothing "
                 "reaches this state, so there is nothing to verify one against"),
    },
    "connector_interop_outlet": {
        "selectors": [LinkedInAutoConnector.INTEROP_OUTLET_SELECTOR],
        "min_expected": 0, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.INTEROP_OUTLET_SELECTOR",
        "note": "shadow-DOM host for the connect dialog; only present mid-connect",
    },
    # ─── Posting path (page="post") ───────────────────────────────────────────
    # These live on a post PERMALINK page, not the feed, so the feed run does not
    # test them — run_post_health_check() does, against a real post URL.
    #
    # This whole block exists because the registry used to cover only the
    # scraping path. On 2026-07-30 every post-detail selector was dead, posting
    # placed zero of three comments, and a health check minutes later still
    # reported HEALTHY: the watchdog was not watching the path that broke. A
    # monitor that misses the risky path turns a loud failure into a confident
    # all-clear, which is worse than no monitor at all.
    # ─── The composer: the path that PUBLISHES, previously unmonitored ───────
    #
    # Hoisted from poster.py method bodies in Phase 0. Until then the registry
    # could not see them at all, so the one code path that writes to LinkedIn had
    # no watchdog — the same blind spot that let the login form rot unnoticed.
    #
    # `composer_trigger` is on the FEED and needs no interaction, so a normal feed
    # health run covers it live. The editor and the Post button do not exist until
    # the composer is open, so they are gated and checked offline against
    # composer_open.html.
    "composer_trigger": {
        "selectors": list(LinkedInPoster.COMPOSER_TRIGGER_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "fix_symbol": "LinkedInPoster.COMPOSER_TRIGGER_SELECTORS",
        "note": ("opens the composer. Two further live-only fallbacks exist and "
                 "are NOT counted here: the visible-text match "
                 f"({LinkedInPoster.COMPOSER_TRIGGER_TEXT!r}) and "
                 "COMPOSER_TRIGGER_XPATH, whose contains() is outside "
                 "dom_probe's grammar"),
    },
    "composer_editor": {
        "selectors": (list(LinkedInPoster.COMPOSER_EDITOR_SELECTORS)
                      + list(LinkedInPoster.COMPOSER_EDITOR_ARIA_SELECTORS)),
        "min_expected": 1,
        "critical": True,
        "page": "composer",
        "requires_composer_open": True,
        "state_witness": COMPOSER_DIALOG_SELECTOR,
        "fix_symbol": "LinkedInPoster.COMPOSER_EDITOR_SELECTORS",
        "note": "the contenteditable the post body is typed into",
    },
    "composer_post_button": {
        "selectors": ([LinkedInPoster.COMPOSER_POST_BUTTON_XPATH]
                      + list(LinkedInPoster.COMPOSER_POST_BUTTON_SELECTORS)),
        "min_expected": 1,
        "critical": True,
        "page": "composer",
        "requires_composer_open": True,
        "state_witness": COMPOSER_DIALOG_SELECTOR,
        "fix_symbol": "LinkedInPoster.COMPOSER_POST_BUTTON_XPATH",
        "note": ("XPath on visible text, like the comment submit button: the "
                 "composer holds other controls and CSS cannot express "
                 "'the button whose label is exactly Post'"),
    },
    # --- Phase 1b: the image-attachment path (harvested live 2026-09-04) ------
    #
    # The media button lives in the OPEN composer, so it is gated the same way
    # the editor and Post button are, with the same witness.
    "composer_media_button": {
        "selectors": list(LinkedInPoster.COMPOSER_MEDIA_BUTTON_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "page": "composer",
        "requires_composer_open": True,
        "state_witness": COMPOSER_DIALOG_SELECTOR,
        "fix_symbol": "LinkedInPoster.COMPOSER_MEDIA_BUTTON_SELECTORS",
        "note": "opens the media editor, which is what mounts the file input",
    },
    # The file input is NOT registered as a normal entry on purpose.
    #
    # It lives in a SHADOW ROOT, and dom_probe parses saved HTML with no shadow
    # concept at all — a fixture cannot represent it, so an offline count would
    # be a confident zero for an element that is really there. That is precisely
    # the false all-clear this registry exists to prevent, so the honest move is
    # to leave it out and say why. It is covered instead by
    # test_poster_media_attach.py, which asserts the shadow-piercing find is
    # what looks for it.
    "composer_media_editor": {
        "selectors": list(LinkedInPoster.COMPOSER_MEDIA_EDITOR_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "page": "composer_media",
        "requires_composer_open": True,
        "state_witness": MEDIA_EDITOR_SELECTOR,
        "fix_symbol": "LinkedInPoster.COMPOSER_MEDIA_EDITOR_SELECTORS",
        "note": ("the media editor panel; its ABSENCE is half the completion "
                 "signal, so a rename here would make attachment look complete "
                 "the instant it started"),
    },
    "composer_media_next": {
        "selectors": ([LinkedInPoster.COMPOSER_MEDIA_NEXT_XPATH]
                      + list(LinkedInPoster.COMPOSER_MEDIA_NEXT_SELECTORS)),
        "min_expected": 1,
        "critical": False,
        "page": "composer_media",
        "requires_composer_open": True,
        "state_witness": MEDIA_EDITOR_SELECTOR,
        "fix_symbol": "LinkedInPoster.COMPOSER_MEDIA_NEXT_XPATH",
        "note": ("commits the media editor. XPath on visible text for the same "
                 "reason the Post button uses one. Non-critical: the attach "
                 "wait re-checks the real signal, so a missed Next times out "
                 "and fails closed rather than publishing something wrong"),
    },
    "composer_image_preview": {
        "selectors": list(LinkedInPoster.COMPOSER_IMAGE_PREVIEW_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "page": "composer_image",
        "requires_composer_open": True,
        "state_witness": COMPOSER_DIALOG_SELECTOR,
        "fix_symbol": "LinkedInPoster.COMPOSER_IMAGE_PREVIEW_SELECTORS",
        "note": ("the attached-image preview — the POSITIVE half of the "
                 "completion signal. If this selector dies, every image post "
                 "fails closed and nothing publishes, which is the safe "
                 "direction but still needs fixing"),
    },
    "post_detail": {
        "selectors": list(LinkedInCommentPoster.POST_DETAIL_SELECTORS),
        "min_expected": 1, "critical": True, "page": "post",
        "fix_symbol": "LinkedInCommentPoster.POST_DETAIL_SELECTORS",
        "note": "proves the permalink page rendered; all 4 legacy entries read 0 on 2026-07-30",
    },
    "post_like_button": {
        "selectors": list(LinkedInCommentPoster.LIKE_BUTTON_SELECTORS)
        + list(LinkedInCommentPoster.LIKED_STATE_SELECTORS),
        "min_expected": 1, "critical": False, "page": "post",
        "fix_symbol": "LinkedInCommentPoster.LIKE_BUTTON_SELECTORS",
        "note": "either an un-liked Like button or the already-liked state counts",
    },
    "comment_open_button": {
        "selectors": list(LinkedInCommentPoster.COMMENT_BUTTON_LABEL_SELECTORS)
        + [LinkedInCommentPoster.COMMENT_BUTTON_TEXT_SELECTOR],
        "min_expected": 1, "critical": True, "page": "post",
        "fix_symbol": "LinkedInCommentPoster.COMMENT_BUTTON_LABEL_SELECTORS",
        "note": "the action-bar button that opens the comment box",
    },
    "comment_input": {
        "selectors": list(LinkedInCommentPoster.COMMENT_INPUT_SELECTORS),
        "min_expected": 1, "critical": True, "page": "post",
        "requires_comment_box": True,
        "state_witness": COMMENT_BOX_SELECTOR,
        "fix_symbol": "LinkedInCommentPoster.COMMENT_INPUT_SELECTORS",
        "note": "the editor; only present after the comment box is opened",
    },
    "comment_submit_button": {
        "selectors": [LinkedInCommentPoster.SUBMIT_BUTTON_XPATH]
        + list(LinkedInCommentPoster.SUBMIT_BUTTON_FALLBACK_SELECTORS),
        "min_expected": 1, "critical": True, "page": "post",
        "requires_comment_box": True,
        "state_witness": COMMENT_BOX_SELECTOR,
        "fix_symbol": "LinkedInCommentPoster.SUBMIT_BUTTON_XPATH / SUBMIT_BUTTON_FALLBACK_SELECTORS",
        "note": ("first entry is an XPath matched on visible text; only present "
                 "after the comment box is opened, and NEVER clicked by the check"),
    },
}


# ─── Pure logic (unit-tested without a browser) ───────────────────────────────

def check_registry(count_fn: Callable[[str], int], registry: Dict = None) -> Dict:
    """Evaluate every registry entry using ``count_fn(selector) -> int``.

    Returns ``{key: {ok, checked, skip_reason, count, matched_selector, counts,
    critical, min_expected, note}}``. A key is ``ok`` when its best-matching
    selector reaches ``min_expected`` (or when ``min_expected`` is 0).

    A selector that *errors* counts 0 — a browser rejecting one selector must not
    abort a whole run. The single exception is
    :class:`dom_probe.UnsupportedSelector`, which is deliberately allowed to
    propagate: it means the offline engine could not read the selector at all,
    and silently scoring that 0 would manufacture the false all-clear this whole
    module exists to prevent (docs/ARCHITECTURE.md §8.2). "I cannot check this" has to
    stay louder than "I checked this and found nothing".
    """
    registry = registry if registry is not None else SELECTOR_REGISTRY
    result = {}
    for key, spec in registry.items():
        counts = {}
        for sel in spec["selectors"]:
            try:
                counts[sel] = int(count_fn(sel))
            except dom_probe.UnsupportedSelector:
                raise
            except Exception:
                counts[sel] = 0
        best_sel, best_count = None, 0
        for sel in spec["selectors"]:
            if counts[sel] > best_count:
                best_count, best_sel = counts[sel], sel
        min_exp = spec.get("min_expected", 0)
        ok = (best_count >= min_exp) if min_exp > 0 else True
        result[key] = {
            "ok": ok,
            # This entry was actually counted. Gated entries never reach here;
            # they get skipped_entry(), which sets checked=False and says why.
            "checked": True,
            "skip_reason": None,
            "count": best_count,
            "matched_selector": best_sel if best_count > 0 else None,
            "counts": counts,
            "critical": spec.get("critical", False),
            "min_expected": min_exp,
            "note": spec.get("note", ""),
        }
    return result


def check_fixture(html: str, page: str = "feed", registry: Dict = None) -> Dict:
    """Run the registry for ``page`` against saved ``html``. No browser, no network.

    The offline half of the gate. Entries that a static snapshot genuinely cannot
    reach are reported via :func:`skipped_entry` with a reason, never silently
    passed — except that a fixture *captured in the gated state* (the comment box
    already open, the overflow menu already expanded) can check them, so an entry
    whose selectors are present is counted rather than skipped.

    Raises :class:`dom_probe.UnsupportedSelector` if a registry selector cannot be
    parsed. That is deliberate: a selector the engine cannot read must break the
    build, not quietly count zero.
    """
    registry = registry if registry is not None else SELECTOR_REGISTRY
    page_registry = {k: v for k, v in registry.items()
                     if v.get("page", "feed") == page}
    count_fn = dom_probe.make_counter(html)

    checked, skipped = {}, {}
    for key, spec in page_registry.items():
        reason = gate_reason(spec)
        if reason and not gated_state_present(spec, count_fn):
            # Gated AND the state is genuinely absent — the fixture is not in the
            # state that reveals it. An entry WITH a state_witness gets here only
            # when the witness is missing too, so a renamed hook inside a present
            # state fails rather than being excused as "not checked".
            skipped[key] = skipped_entry(key, spec, reason)
        else:
            checked[key] = spec

    result = check_registry(count_fn, checked)
    result.update(skipped)
    return result


def fixture_report(html: str, page: str = "feed", source: str = "fixture") -> Dict:
    """A full, versioned health result for saved ``html`` — the CI-able check."""
    check = check_fixture(html, page=page)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "page": page,
        "status": overall_status(check),
        "failed": [k for k, c in check.items() if not c["ok"]],
        "not_checked": [k for k, c in check.items() if not c.get("checked", True)],
        "checks": check,
    }


def overall_status(check: Dict) -> str:
    """HEALTHY (all ok), DEGRADED (a non-critical fail), or BROKEN (a critical fail)."""
    if any((not c["ok"]) and c["critical"] for c in check.values()):
        return "BROKEN"
    if any(not c["ok"] for c in check.values()):
        return "DEGRADED"
    return "HEALTHY"


def classify_search_page(logged_in: bool, card_count: int) -> str:
    """Distinguish 'not logged in' from 'logged in but no results' for a search page.

    Login is decided by the URL/authwall (``logged_in``), NEVER by the presence of
    result cards — an empty search is a valid logged-in page. Returns:
      - ``not_logged_in`` — the URL bounced to login/authwall,
      - ``no_results`` — logged in, but zero cards (an empty search OR a stale card
        selector; the two can't be told apart from card count alone),
      - ``ok`` — logged in with result cards present.
    """
    if not logged_in:
        return "not_logged_in"
    if card_count <= 0:
        return "no_results"
    return "ok"


def suggest_selectors(diag: Dict) -> List[str]:
    """Suggest candidate selectors from stable hooks found in the live DOM."""
    out = []
    for v in sorted(set(diag.get("data_view_names", []))):
        out.append(f"[data-view-name='{v}']")
    for t in sorted(set(diag.get("data_testids", []))):
        out.append(f"[data-testid='{t}']")
    return out


def build_fix_markdown(profile_name: str, status: str, check: Dict,
                       dump_path: str, suggestions: List[str]) -> str:
    """Build the .dev/SELECTOR_FIX_NEEDED.md content (failed selectors + fix prompt)."""
    failed = {k: c for k, c in check.items() if not c["ok"]}
    lines = [
        "# SELECTOR FIX NEEDED",
        "",
        f"- **Status:** {status}",
        f"- **Profile:** {profile_name}",
        f"- **Detected:** {datetime.now().isoformat()}",
        f"- **DOM dump:** `{dump_path}`",
        "",
        "## Failed selectors",
        "",
    ]
    for key, c in failed.items():
        spec = SELECTOR_REGISTRY.get(key, {})
        crit = "CRITICAL" if c["critical"] else "non-critical"
        # Connector and posting-path entries already carry their own class in
        # fix_symbol; only the scraper's bare names need qualifying.
        symbol = spec.get("fix_symbol", "?")
        if "." not in symbol:
            symbol = f"LinkedInScraper.{symbol}"
        lines.append(f"### `{key}` ({crit}) — edit `{symbol}`")
        lines.append(f"- expected >= {c['min_expected']} matches, got {c['count']}")
        for sel, n in c.get("counts", {}).items():
            lines.append(f"  - `{sel}` -> {n}")
        if c.get("note"):
            lines.append(f"- note: {c['note']}")
        lines.append("")

    # What this run did NOT look at. Omitting it is how a partial check reads as
    # a full one — the exact mistake that let the posting path break unnoticed.
    unchecked = {k: c for k, c in check.items() if not c.get("checked", True)}
    if unchecked:
        lines.append("## Not checked by this run")
        lines.append("")
        for key, c in unchecked.items():
            lines.append(f"- `{key}` — {c.get('skip_reason', 'not checked')}")
        lines.append("")

    lines.append("## Suggested hooks from the current DOM")
    lines.append("")
    if suggestions:
        for s in suggestions[:40]:
            lines.append(f"- `{s}`")
    else:
        lines.append("- (none collected)")
    lines.append("")

    failed_keys = ", ".join(failed.keys())
    lines += [
        "## Ready-to-paste prompt for Claude Code",
        "",
        "```",
        f"The LinkedIn scraper's selectors broke ({status}). Failed: {failed_keys}.",
        f"Read `{dump_path}` (the current feed DOM for the first few posts) and update",
        "the matching selector lists in linkedin_automation/post_finder.py "
        "(POST_SELECTORS / TEXT_SELECTORS / AUTHOR_SELECTORS / CONTROL_MENU_SELECTOR /",
        "MENU_ITEM_SELECTORS / SCROLL_CONTAINER_SELECTORS) so they match the current DOM.",
        "Add the new working selectors FIRST, keep the old ones as fallbacks. Then run",
        f"`uv run python -m linkedin_automation.selector_health --profile {profile_name}` and confirm HEALTHY.",
        "```",
        "",
    ]
    return "\n".join(lines)


# ─── Browser-driven checks ────────────────────────────────────────────────────

_WALK_JS = """
const root = arguments[0], maxDepth = arguments[1];
const out = [];
function attrs(el){const o={}; for (const a of el.attributes) o[a.name]=a.value; return o;}
(function rec(el, d){
    out.push({tag: el.tagName.toLowerCase(), attrs: attrs(el)});
    if (d < maxDepth) for (const c of el.children) rec(c, d+1);
})(root, 0);
return out;
"""


def _check_copy_link(driver) -> Dict:
    """Open the first post's overflow menu and look for the 'Copy link' item."""
    spec = SELECTOR_REGISTRY["copy_link_item"]
    base = {
        "critical": spec["critical"], "min_expected": spec["min_expected"],
        "note": spec.get("note", ""), "counts": {}, "matched_selector": None,
        "checked": True, "skip_reason": None,
    }
    try:
        btn = driver.find_element(By.CSS_SELECTOR, LinkedInScraper.CONTROL_MENU_SELECTOR)
    except Exception:
        # No menu to open is not evidence the menu-item selector is stale.
        return {**skipped_entry("copy_link_item", spec,
                                "not checked: no overflow menu to open"),
                "error": "no overflow menu to open"}
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.4)
        btn.click()
        time.sleep(1.2)
        items = driver.find_elements(By.CSS_SELECTOR, LinkedInScraper.MENU_ITEM_SELECTORS)
        n = sum(1 for it in items
                if LinkedInScraper.COPY_LINK_TEXT in (it.text or "").strip().lower())
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            logger.debug("ESC dismiss failed", exc_info=True)
        return {
            **base, "ok": n >= spec["min_expected"], "count": n,
            "matched_selector": LinkedInScraper.MENU_ITEM_SELECTORS if n else None,
            "counts": {LinkedInScraper.MENU_ITEM_SELECTORS: n},
        }
    except Exception as e:
        return {**skipped_entry("copy_link_item", spec,
                                f"not checked: could not open the overflow menu ({e})"),
                "error": str(e)}


def collect_diagnostics(driver, max_posts: int = 3) -> Dict:
    """Dump the first ``max_posts`` post containers and summarize their attributes."""
    posts = []
    for sel in LinkedInScraper.POST_SELECTORS:
        posts = driver.find_elements(By.CSS_SELECTOR, sel)
        if posts:
            break
    posts = posts[:max_posts]

    htmls, view_names, testids, classes, arias = [], [], [], [], []
    for p in posts:
        try:
            htmls.append(p.get_attribute("outerHTML") or "")
        except Exception:
            logger.debug("outerHTML read failed", exc_info=True)
        try:
            for node in driver.execute_script(_WALK_JS, p, 6):
                a = node.get("attrs", {})
                if a.get("data-view-name"):
                    view_names.append(a["data-view-name"])
                if a.get("data-testid"):
                    testids.append(a["data-testid"])
                if a.get("class"):
                    classes.extend(a["class"].split())
                if a.get("aria-label") and node.get("tag") == "button":
                    arias.append(a["aria-label"])
        except Exception:
            logger.debug("attribute walk failed", exc_info=True)

    try:
        with open(DEBUG_DUMP_FILE, "w", encoding="utf-8") as f:
            f.write(f"<!-- selector_debug_dump: first {len(posts)} feed posts, "
                    f"{datetime.now().isoformat()} -->\n")
            for h in htmls:
                f.write(h + "\n\n<hr/>\n\n")
        logger.info(f"Dumped {len(posts)} post containers to {DEBUG_DUMP_FILE}")
    except Exception:
        logger.debug("Could not write debug dump", exc_info=True)

    return {
        "posts_dumped": len(posts),
        "data_view_names": sorted(set(view_names)),
        "data_testids": sorted(set(testids)),
        "class_tokens": sorted(set(classes))[:60],
        "button_aria_labels": sorted(set(arias)),
    }


def run_health_check(profile_name: str = None, scrolls: int = 3) -> Dict:
    """Open Chrome, scan the feed, and return the structured health result.

    Also writes selector_health.json under the profile data dir, a DOM dump when
    anything failed, and .dev/SELECTOR_FIX_NEEDED.md when a critical selector failed.
    """
    driver, _profile = pm.create_driver(profile_name)
    try:
        logger.info("Loading LinkedIn feed...")
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(5)

        if not pm.is_logged_in_on_page(driver):
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile '{profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile {profile_name or 'default'}"
            )

        for _ in range(scrolls):
            driver.execute_script("window.scrollBy(0, 1200);")
            time.sleep(2)

        def count_fn(sel):
            return len(driver.find_elements(selector_by(sel), sel))

        # Check feed-page selectors here. Skip the menu-dependent copy_link_item
        # (checked separately below) and any page="search" entries (connector
        # selectors live on the search page, not the feed, so they can't be
        # tested by this feed run).
        page_registry = {
            k: v for k, v in SELECTOR_REGISTRY.items()
            if not v.get("requires_menu_open") and v.get("page", "feed") == "feed"
        }
        check = check_registry(count_fn, page_registry)
        check["copy_link_item"] = _check_copy_link(driver)
        logger.info("(connector search-page selectors are registered but not tested on the feed)")

        status = overall_status(check)
        failed = [k for k, c in check.items() if not c["ok"]]
        for key, c in check.items():
            mark = "PASS" if c["ok"] else "FAIL"
            crit = "critical" if c["critical"] else "optional"
            logger.info(f"  [{mark}] {key:16s} ({crit}) count={c['count']} "
                        f"min={c['min_expected']} via={c['matched_selector']}")

        result = {
            "schema_version": SCHEMA_VERSION,
            "source": "live",
            "page": "feed",
            "profile": profile_name,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "failed": failed,
            "not_checked": [k for k, c in check.items() if not c.get("checked", True)],
            "checks": check,
        }

        if failed:
            diag = collect_diagnostics(driver)
            result["debug_dump"] = DEBUG_DUMP_FILE
            result["diagnostics"] = diag
            result["suggested_selectors"] = suggest_selectors(diag)
            logger.warning(f"Failed selectors: {failed}")
            logger.info(f"Suggested hooks: {result['suggested_selectors']}")

        if status == "BROKEN":
            md = build_fix_markdown(
                profile_name or "default", status, check,
                DEBUG_DUMP_FILE, result.get("suggested_selectors", []),
            )
            os.makedirs(os.path.dirname(FIX_NEEDED_FILE), exist_ok=True)
            with open(FIX_NEEDED_FILE, "w", encoding="utf-8") as f:
                f.write(md)
            result["fix_file"] = FIX_NEEDED_FILE
            logger.warning(f"BROKEN — wrote fix instructions to {FIX_NEEDED_FILE}")
            # A critical selector broke — capture a screenshot so the BROKEN report
            # has a VISUAL of the feed alongside the HTML DOM dump.
            shot = capture_failure(driver, "feed_selectors_broken", profile_name)
            if shot:
                result["failure_screenshot"] = shot

        # Persist the result for the dashboard endpoint to read.
        try:
            out_path = os.path.join(pm.get_data_dir(profile_name), HEALTH_RESULT_FILE)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["result_file"] = out_path
        except Exception:
            logger.debug("Could not write selector_health.json", exc_info=True)

        return result
    finally:
        driver.quit()


def run_search_health_check(profile_name: str, search_url: str, scrolls: int = 3) -> Dict:
    """Check the connector's people-SEARCH-page selectors against a live search URL.

    Navigates to ``search_url`` (the same one passed to the connector), scrolls to
    trigger lazy-loading, and runs check_registry over the page="search" entries —
    WITHOUT clicking Connect (so no invitations are sent). Modal-only entries (the
    Send button, which appears only after a Connect click) are skipped. Writes
    selector_health_search.json under the profile data dir.
    """
    driver, _profile = pm.create_driver(profile_name)
    try:
        logger.info(f"Loading people-search results: {search_url}")
        driver.get(search_url)
        time.sleep(6)

        if not pm.is_logged_in_on_page(driver):
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile '{profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile {profile_name or 'default'}"
            )

        for _ in range(scrolls):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        def count_fn(sel):
            return len(driver.find_elements(selector_by(sel), sel))

        # Only the connector's search-page selectors, minus modal-only ones (the
        # Send button isn't present until Connect is clicked, which we never do).
        search_registry = {
            k: v for k, v in SELECTOR_REGISTRY.items()
            if v.get("page") == "search" and not v.get("modal_only")
        }
        check = check_registry(count_fn, search_registry)

        status = overall_status(check)
        failed = [k for k, c in check.items() if not c["ok"]]
        for key, c in check.items():
            mark = "PASS" if c["ok"] else "FAIL"
            crit = "critical" if c["critical"] else "optional"
            logger.info(f"  [{mark}] {key:24s} ({crit}) count={c['count']} "
                        f"min={c['min_expected']} via={c['matched_selector']}")

        # We only reach here past the URL/authwall login gate, so login is
        # confirmed regardless of how many results rendered. Base "has results" on
        # the link-first Connect-link count (cards are a dead fallback now); this
        # distinguishes an empty search from stale selectors without conflating
        # either with "not logged in".
        results_count = max(
            check.get("connector_connect_link", {}).get("count", 0),
            check.get("connector_search_result", {}).get("count", 0),
        )
        search_state = classify_search_page(True, results_count)
        if search_state == "no_results":
            logger.info(
                "Logged in (URL confirms), but 0 result cards found — the search "
                "may simply have no people results, or the card selector may be "
                "stale. Try a broader search URL to disambiguate."
            )

        result = {
            "schema_version": SCHEMA_VERSION,
            "source": "live",
            "profile": profile_name,
            "page": "search",
            "search_url": search_url,
            "status": status,
            "logged_in": True,
            "search_state": search_state,
            "timestamp": datetime.now().isoformat(),
            "failed": failed,
            "not_checked": [k for k, c in check.items() if not c.get("checked", True)],
            "checks": check,
        }

        # A critical search-page selector broke — capture a visual alongside the
        # JSON report (the search page, not the feed).
        if status == "BROKEN":
            shot = capture_failure(driver, "search_selectors_broken", profile_name)
            if shot:
                result["failure_screenshot"] = shot

        try:
            out_path = os.path.join(pm.get_data_dir(profile_name), "selector_health_search.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["result_file"] = out_path
        except Exception:
            logger.debug("Could not write selector_health_search.json", exc_info=True)

        return result
    finally:
        driver.quit()


def _check_comment_box(driver) -> Dict:
    """Open the comment box and count the editor + submit-button selectors.

    The editor and the submit button do not exist until the box is opened, which
    is precisely why they went unwatched: a check that only loads the page cannot
    see them. This opens the box and counts. It **never clicks submit and never
    types**, so no comment can be posted by a health run.
    """
    keys = [k for k, v in SELECTOR_REGISTRY.items() if v.get("requires_comment_box")]

    def _fail(reason: str) -> Dict:
        """Could not reach the gated state — report it as not checked, with why.

        Deliberately not ``ok: False``. Failing to *open* the box is not evidence
        that the editor selector is stale, and reporting it as a selector failure
        would send a repair at the wrong target. ``checked=False`` plus a reason
        is the honest answer.
        """
        return {
            key: {**skipped_entry(key, SELECTOR_REGISTRY[key],
                                  f"not checked: {reason}"), "error": reason}
            for key in keys
        }

    opener = None
    for sel in LinkedInCommentPoster.COMMENT_BUTTON_LABEL_SELECTORS:
        for btn in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if btn.is_displayed():
                    opener = btn
                    break
            except Exception:
                continue
        if opener:
            break

    if opener is None:
        return _fail("no comment button to open the box with")

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", opener)
        time.sleep(0.4)
        opener.click()
        time.sleep(1.5)
    except Exception as e:
        return _fail(f"could not open the comment box: {e}")

    def count_fn(sel):
        return len(driver.find_elements(selector_by(sel), sel))

    checked = check_registry(count_fn, {k: SELECTOR_REGISTRY[k] for k in keys})

    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        logger.debug("ESC dismiss failed", exc_info=True)

    return checked


def run_post_health_check(profile_name: str, post_url: str) -> Dict:
    """Check the POSTING path's selectors against a live post permalink URL.

    Navigates to ``post_url``, counts the page="post" registry entries, then opens
    the comment box to reach the editor and submit button. **Nothing is typed and
    submit is never clicked**, so this cannot place a comment.

    This is the check that was missing when posting broke on 2026-07-30: the feed
    run reported HEALTHY while every post-detail selector was dead. Writes
    selector_health_post.json under the profile data dir.
    """
    driver, _profile = pm.create_driver(profile_name)
    try:
        logger.info(f"Loading post permalink: {post_url}")
        driver.get(post_url)
        time.sleep(5)

        if not pm.is_logged_in_on_page(driver):
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile '{profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile {profile_name or 'default'}"
            )

        def count_fn(sel):
            return len(driver.find_elements(selector_by(sel), sel))

        page_registry = {
            k: v for k, v in SELECTOR_REGISTRY.items()
            if v.get("page") == "post" and not v.get("requires_comment_box")
        }
        check = check_registry(count_fn, page_registry)
        check.update(_check_comment_box(driver))

        status = overall_status(check)
        failed = [k for k, c in check.items() if not c["ok"]]
        for key, c in check.items():
            mark = "PASS" if c["ok"] else "FAIL"
            crit = "critical" if c["critical"] else "optional"
            logger.info(f"  [{mark}] {key:22s} ({crit}) count={c['count']} "
                        f"min={c['min_expected']} via={c['matched_selector']}")

        result = {
            "schema_version": SCHEMA_VERSION,
            "source": "live",
            "profile": profile_name,
            "page": "post",
            "post_url": post_url,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "failed": failed,
            "not_checked": [k for k, c in check.items() if not c.get("checked", True)],
            "checks": check,
        }

        if status == "BROKEN":
            md = build_fix_markdown(
                profile_name or "default", status, check, DEBUG_DUMP_FILE, [])
            os.makedirs(os.path.dirname(FIX_NEEDED_FILE), exist_ok=True)
            with open(FIX_NEEDED_FILE, "w", encoding="utf-8") as f:
                f.write(md)
            result["fix_file"] = FIX_NEEDED_FILE
            logger.warning(f"BROKEN — wrote fix instructions to {FIX_NEEDED_FILE}")
            shot = capture_failure(driver, "post_selectors_broken", profile_name)
            if shot:
                result["failure_screenshot"] = shot

        try:
            out_path = os.path.join(pm.get_data_dir(profile_name), POST_HEALTH_RESULT_FILE)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["result_file"] = out_path
        except Exception:
            logger.debug("Could not write selector_health_post.json", exc_info=True)

        return result
    finally:
        driver.quit()


def _print_summary(result: Dict):
    print("\n" + "=" * 60)
    print(f"  SELECTOR HEALTH: {result['status']}")
    print("=" * 60)
    for key, c in result["checks"].items():
        if not c.get("checked", True):
            print(f"  – {key:22s} {c.get('skip_reason', 'not checked')}")
            continue
        mark = "✓" if c["ok"] else "✗"
        print(f"  {mark} {key:22s} count={c['count']:>3} (min {c['min_expected']}) "
              f"{'[critical]' if c['critical'] else ''}")
    if result.get("debug_dump"):
        print(f"\n  DOM dump: {result['debug_dump']}")
    if result.get("suggested_selectors"):
        print("  Suggested hooks:")
        for s in result["suggested_selectors"][:20]:
            print(f"    {s}")
    if result.get("fix_file"):
        print(f"\n  ⚠ BROKEN — fix instructions written to {result['fix_file']}")
    print()


def main(argv=None) -> int:
    """CLI entry point. Exit 0 = HEALTHY/DEGRADED, 1 = BROKEN/error, 2 = login required."""
    # Make emoji output safe on the Windows cp1252 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Check LinkedIn scraper selector health")
    parser.add_argument("--profile", default=None, help="Profile name (default profile if omitted)")
    parser.add_argument("--scrolls", type=int, default=3, help="Feed scrolls before checking")
    parser.add_argument("--search-url", default=None,
                        help="Check the connector's people-SEARCH-page selectors against this "
                             "URL instead of the feed (does NOT click Connect / send invites)")
    parser.add_argument("--post-url", default=None,
                        help="Check the POSTING path's selectors against this post permalink "
                             "instead of the feed. Opens the comment box to reach the editor "
                             "and submit button, but never types and never clicks submit.")
    parser.add_argument("--fixture", default=None,
                        help="Check a SAVED HTML file offline instead of opening a "
                             "browser: no LinkedIn session, no network. Proves the "
                             "selectors still match the shape they were written for, "
                             "NOT that they match live LinkedIn. Use with --page.")
    parser.add_argument("--page", default="feed",
                        choices=("feed", "search", "post", "composer"),
                        help="Which page's registry entries --fixture holds (default: feed)")
    parser.add_argument("--json", action="store_true", help="Print the JSON result to stdout")
    args = parser.parse_args(argv)

    passed = [n for n, v in (("--search-url", args.search_url),
                             ("--post-url", args.post_url),
                             ("--fixture", args.fixture)) if v]
    if len(passed) > 1:
        parser.error(f"{' and '.join(passed)} check different things; pass one at a time")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # Offline: no profile, no browser, no migration. Deliberately handled before
    # anything that could touch a Chrome session or the network.
    if args.fixture:
        try:
            with open(args.fixture, encoding="utf-8") as f:
                html = f.read()
        except OSError as e:
            print(f"\n❌ Could not read fixture: {e}")
            return pm.EXIT_ERROR
        try:
            result = fixture_report(html, page=args.page, source="fixture")
        except dom_probe.UnsupportedSelector as e:
            print(f"\n❌ A registry selector cannot be parsed offline: {e}")
            return pm.EXIT_ERROR
        if args.json:
            print(json.dumps(result))
        else:
            _print_summary(result)
            print("  NOTE: a fixture proves the selectors still match the shape they\n"
                  "  were written for. It does NOT prove they match live LinkedIn.\n")
        return pm.EXIT_ERROR if result["status"] == "BROKEN" else pm.EXIT_OK

    try:
        pm.auto_migrate_from_env()
        if args.search_url:
            result = run_search_health_check(args.profile, args.search_url, scrolls=args.scrolls)
        elif args.post_url:
            result = run_post_health_check(args.profile, args.post_url)
        else:
            result = run_health_check(args.profile, scrolls=args.scrolls)
    except pm.LoginRequiredError as e:
        print(f"\n❌ {e}")
        return pm.EXIT_LOGIN_REQUIRED
    except Exception as e:
        print(f"\n❌ Health check failed: {e}")
        return pm.EXIT_ERROR

    if args.json:
        print(json.dumps(result))
    else:
        _print_summary(result)

    return pm.EXIT_ERROR if result["status"] == "BROKEN" else pm.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
