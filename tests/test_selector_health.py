"""Tests for selector_health_check pure logic (selector-watchdog).

No browser: check_registry takes a count function, so we feed it fakes. Also
verifies the registry stays in sync with the scraper's selector constants."""

from linkedin_automation import selector_health as shc
from linkedin_automation.post_finder import LinkedInScraper


# ─── Registry stays in sync with the scraper ──────────────────────────────────

def test_registry_pulls_selectors_from_finder():
    reg = shc.SELECTOR_REGISTRY
    assert reg["feed_container"]["selectors"] == list(LinkedInScraper.POST_SELECTORS)
    assert reg["post_text"]["selectors"] == list(LinkedInScraper.TEXT_SELECTORS)
    assert reg["author"]["selectors"] == list(LinkedInScraper.AUTHOR_SELECTORS)
    assert reg["overflow_menu"]["selectors"] == [LinkedInScraper.CONTROL_MENU_SELECTOR]
    assert reg["scroll_container"]["selectors"] == list(LinkedInScraper.SCROLL_CONTAINER_SELECTORS)


def test_registry_criticality_and_keys():
    reg = shc.SELECTOR_REGISTRY
    feed_keys = {"feed_container", "post_text", "author",
                 "overflow_menu", "copy_link_item", "scroll_container"}
    assert feed_keys.issubset(set(reg))
    assert reg["feed_container"]["critical"] is True
    assert reg["overflow_menu"]["critical"] is True
    assert reg["copy_link_item"]["critical"] is False
    assert reg["copy_link_item"].get("requires_menu_open") is True


def test_registry_includes_connector_selectors_in_sync():
    from linkedin_automation.auto_connector import LinkedInAutoConnector as C
    reg = shc.SELECTOR_REGISTRY
    # Link-first: the Connect link is the CRITICAL primary signal.
    connect_sels = reg["connector_connect_link"]["selectors"]
    assert connect_sels[:len(C.CONNECT_LINK_SELECTORS)] == list(C.CONNECT_LINK_SELECTORS)
    assert reg["connector_connect_link"]["critical"] is True
    # The card + name checks are now FALLBACK-only (non-critical): LinkedIn removed
    # data-view-name, so a 0 there must not read as BROKEN.
    assert C.SEARCH_RESULT_SELECTOR in reg["connector_search_result"]["selectors"]
    assert reg["connector_search_result"]["critical"] is False
    assert reg["connector_result_name"]["selectors"] == [C.RESULT_NAME_SELECTOR]
    assert reg["connector_result_name"]["critical"] is False
    # Pagination is registered (non-critical) and pulls the connector's constants.
    assert reg["connector_pagination"]["selectors"][0] == C.PAGINATION_NEXT_SELECTORS[0]
    assert reg["connector_pagination"]["critical"] is False
    # The Send button is modal-only (appears after clicking Connect), so it's
    # skipped by the non-clicking search health check.
    assert reg["connector_send_button"].get("modal_only") is True
    # They are marked page="search" so the feed health run skips them.
    for key in ("connector_connect_link", "connector_search_result",
                "connector_result_name", "connector_pagination",
                "connector_send_button", "connector_interop_outlet"):
        assert reg[key]["page"] == "search"


# ─── check_registry ───────────────────────────────────────────────────────────

def test_all_healthy():
    chk = shc.check_registry(lambda sel: 5)
    assert shc.overall_status(chk) == "HEALTHY"
    assert all(c["ok"] for c in chk.values())


def test_min_expected_enforced_for_feed():
    # feed_container needs >= 3 matches.
    chk2 = shc.check_registry(lambda sel: 2, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk2["feed_container"]["ok"] is False
    chk3 = shc.check_registry(lambda sel: 3, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk3["feed_container"]["ok"] is True


def test_critical_failure_is_broken():
    # Only the primary feed selector breaks; fallbacks also 0 -> BROKEN.
    chk = shc.check_registry(lambda sel: 0, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk["feed_container"]["ok"] is False
    assert shc.overall_status(chk) == "BROKEN"


def test_noncritical_failure_is_degraded():
    sub = {"feed_container": shc.SELECTOR_REGISTRY["feed_container"],
           "scroll_container": shc.SELECTOR_REGISTRY["scroll_container"]}
    counts = {"mainFeed": 0, "scaffold-finite-scroll": 0}

    def fake(sel):
        return 0 if any(s in sel for s in counts) else 5

    chk = shc.check_registry(fake, sub)
    assert chk["feed_container"]["ok"] is True
    assert chk["scroll_container"]["ok"] is False
    assert shc.overall_status(chk) == "DEGRADED"


def test_matched_selector_is_first_working_fallback():
    # Primary feed selector fails, a fallback works -> ok via fallback.
    def fake(sel):
        return 0 if "feed-full-update" in sel else 4

    chk = shc.check_registry(fake, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk["feed_container"]["ok"] is True
    assert chk["feed_container"]["matched_selector"] != "div[data-view-name='feed-full-update']"


def test_per_selector_counts_recorded():
    chk = shc.check_registry(lambda sel: 7, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    counts = chk["feed_container"]["counts"]
    assert all(v == 7 for v in counts.values())
    assert "div[data-view-name='feed-full-update']" in counts


# ─── suggestions + fix markdown ───────────────────────────────────────────────

def test_suggest_selectors():
    out = shc.suggest_selectors({"data_view_names": ["feed-actor", "feed-full-update"],
                                 "data_testids": ["mainFeed"]})
    assert "[data-view-name='feed-actor']" in out
    assert "[data-testid='mainFeed']" in out


def test_build_fix_markdown_contains_essentials():
    chk = shc.check_registry(lambda sel: 0, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    md = shc.build_fix_markdown("demo", "BROKEN", chk, "selector_debug_dump.html",
                                ["[data-view-name='feed-full-update']"])
    assert "feed_container" in md
    assert "POST_SELECTORS" in md            # the symbol a fixer edits
    assert "selector_debug_dump.html" in md  # dump location
    assert "Ready-to-paste prompt" in md
    assert "demo" in md


# ─── Posting path (the blind spot that hid the 2026-07-30 breakage) ───────────
#
# selector_health reported HEALTHY while every post-detail selector was dead and
# a run had placed zero of three comments, because SELECTOR_REGISTRY held only
# scraping selectors. These tests assert the registry now covers the path that
# actually broke, and stays in sync with the constants the poster uses.

def test_registry_covers_the_posting_path():
    from linkedin_automation.comment_poster import LinkedInCommentPoster as P
    reg = shc.SELECTOR_REGISTRY
    post_keys = {"post_detail", "post_like_button", "comment_open_button",
                 "comment_input", "comment_submit_button"}
    assert post_keys.issubset(set(reg)), "the posting path is not registered"
    for key in post_keys:
        assert reg[key]["page"] == "post", f"{key} must not run on the feed check"
    # In sync with the constants the poster actually uses.
    assert reg["post_detail"]["selectors"] == list(P.POST_DETAIL_SELECTORS)
    assert reg["comment_input"]["selectors"] == list(P.COMMENT_INPUT_SELECTORS)
    assert P.SUBMIT_BUTTON_XPATH in reg["comment_submit_button"]["selectors"]


def test_the_selectors_that_prove_a_post_loaded_are_critical():
    """A dead post_detail selector must read BROKEN, not DEGRADED."""
    reg = shc.SELECTOR_REGISTRY
    assert reg["post_detail"]["critical"] is True
    assert reg["comment_input"]["critical"] is True
    assert reg["comment_submit_button"]["critical"] is True


def test_editor_and_submit_are_marked_as_needing_the_comment_box():
    """Both only exist after the box is opened, so a page-load check can't see them."""
    reg = shc.SELECTOR_REGISTRY
    assert reg["comment_input"].get("requires_comment_box") is True
    assert reg["comment_submit_button"].get("requires_comment_box") is True
    # ...and the ones that ARE present on page load must not be gated.
    assert not reg["post_detail"].get("requires_comment_box")
    assert not reg["comment_open_button"].get("requires_comment_box")


def test_the_feed_run_does_not_try_to_check_posting_selectors():
    """The feed registry filter must exclude page='post' entries."""
    feed_registry = {
        k: v for k, v in shc.SELECTOR_REGISTRY.items()
        if not v.get("requires_menu_open") and v.get("page", "feed") == "feed"
    }
    assert "post_detail" not in feed_registry
    assert "comment_submit_button" not in feed_registry
    assert "feed_container" in feed_registry


def test_a_dead_posting_path_now_reports_broken():
    """The regression itself: all post-detail selectors returning 0 must be BROKEN.

    Before the registry covered the posting path this scenario produced HEALTHY,
    because there was nothing registered to fail.
    """
    post_registry = {k: v for k, v in shc.SELECTOR_REGISTRY.items()
                     if v.get("page") == "post"}
    chk = shc.check_registry(lambda sel: 0, post_registry)
    assert shc.overall_status(chk) == "BROKEN"
    assert chk["post_detail"]["ok"] is False


def test_the_legacy_permalink_selectors_alone_would_be_broken():
    """Reproduces 2026-07-30: only the 4 legacy class selectors match -> still 0.

    The live counts were span[data-testid=...] -> 1, div[role='listitem'] -> 1,
    and 0 for all four legacy class names. Modelling that exact DOM must fail.
    """
    from linkedin_automation.comment_poster import LinkedInCommentPoster as P
    legacy = {"div.occludable-update", "div.feed-shared-update-v2",
              "article.feed-shared-article", "div[data-urn*='activity']"}
    chk = shc.check_registry(
        lambda sel: 0 if sel in legacy else 0,
        {"post_detail": shc.SELECTOR_REGISTRY["post_detail"]})
    assert chk["post_detail"]["ok"] is False
    # And with the modern selectors present, it passes.
    live = {"span[data-testid='expandable-text-box']": 1, "div[role='listitem']": 1}
    chk2 = shc.check_registry(
        lambda sel: live.get(sel, 0),
        {"post_detail": shc.SELECTOR_REGISTRY["post_detail"]})
    assert chk2["post_detail"]["ok"] is True
    assert chk2["post_detail"]["matched_selector"] == P.POST_DETAIL_SELECTORS[0]


def test_xpath_selectors_are_counted_with_the_xpath_strategy():
    """The submit button is text-matched, which CSS cannot express."""
    from selenium.webdriver.common.by import By
    assert shc.selector_by("//button[normalize-space(.)='Comment']") == By.XPATH
    assert shc.selector_by("div.feed-shared-update-v2") == By.CSS_SELECTOR
    assert shc.selector_by("button[aria-label*='Comment']") == By.CSS_SELECTOR


def test_the_fix_report_names_the_right_class_to_edit():
    """A posting-path failure must not be reported as a LinkedInScraper symbol."""
    chk = shc.check_registry(lambda sel: 0,
                             {"post_detail": shc.SELECTOR_REGISTRY["post_detail"]})
    md = shc.build_fix_markdown("demo", "BROKEN", chk, "dump.html", [])
    assert "LinkedInCommentPoster.POST_DETAIL_SELECTORS" in md
    assert "LinkedInScraper.LinkedInCommentPoster" not in md
