"""Tests for the JS clipboard-intercept URL extraction (clipboard-intercept).

Offline: _validate_post_url is pure and the intercept JS is a static string.
The browser-dependent extract_url_via_clipboard is verified live separately."""

import logging

from linkedin_automation.post_finder import LinkedInScraper


def _scraper():
    # driver=None is fine: _validate_post_url and the JS constant don't touch it.
    return LinkedInScraper(driver=None, logger=logging.getLogger("test"))


def test_validate_accepts_posts_url_and_strips_query():
    s = _scraper()
    out = s._validate_post_url("https://www.linkedin.com/posts/x-ugcPost-1010101010101010101-xXxX/?utm_source=share")
    assert out == "https://www.linkedin.com/posts/x-ugcPost-1010101010101010101-xXxX/"


def test_validate_accepts_feed_update_url():
    s = _scraper()
    url = "https://www.linkedin.com/feed/update/urn:li:activity:0000000000000000000/"
    assert s._validate_post_url(url) == url


def test_validate_rejects_non_post_url():
    s = _scraper()
    assert s._validate_post_url("https://www.linkedin.com/in/someone/") is None
    assert s._validate_post_url("https://example.com/foo") is None


def test_validate_rejects_none_and_empty():
    s = _scraper()
    assert s._validate_post_url(None) is None
    assert s._validate_post_url("") is None


def test_intercept_js_overrides_clipboard_apis():
    js = LinkedInScraper.CLIPBOARD_INTERCEPT_JS
    # Resets the capture var, overrides writeText, and hooks execCommand('copy').
    assert "window.__interceptedClipboard = null" in js
    assert "navigator.clipboard.writeText" in js
    assert "document.execCommand" in js
    assert "'copy'" in js


def test_no_set_clipboard_method():
    # The OS-clipboard WRITE (sentinel) was removed; primary path must not touch
    # the OS clipboard. Only the read-only fallback (_get_clipboard) remains.
    assert not hasattr(LinkedInScraper, "_set_clipboard")
    assert hasattr(LinkedInScraper, "_get_clipboard")
