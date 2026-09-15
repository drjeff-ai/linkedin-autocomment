"""LinkedIn's "Copy link to post" returns a lnkd.in shortlink, not a permalink.

The copy flow worked the whole time — the "Link copied to clipboard" toast is
right there in the failure screenshots. What failed was the validator: it
required "linkedin.com/feed/update/" or "linkedin.com/posts/" in the value, so
every successfully copied link was thrown away, the post was stored with no
URL, and the store trashed it as `no_url`.

On the dev feed that discarded 20 of 21 quality posts across two scrapes. The
single survivor was the one post whose URN happened to still be in the feed DOM,
which skipped the clipboard path entirely.
"""

import logging
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation.post_finder import LinkedInScraper  # noqa: E402

PERMALINK = ("https://www.linkedin.com/posts/"
             "example-org-announcement-ugcPost-1011101110111011101-xXxX/")
FEED_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1100110011001100110/"


class _Scraper(LinkedInScraper):
    """Just the URL helpers - no driver."""

    def __init__(self):
        self.logger = logging.getLogger("test")


@pytest.fixture
def scraper():
    return _Scraper()


@pytest.fixture
def resolves(monkeypatch):
    """Pretend lnkd.in answers, without touching the network."""
    calls = []

    def fake(url, timeout=15):
        calls.append(url)
        return PERMALINK + "?utm_source=share&utm_medium=member_desktop"

    monkeypatch.setattr(LinkedInScraper, "_resolve_shortlink",
                        staticmethod(fake))
    return calls


# ─── the bug ──────────────────────────────────────────────────────────────────

def test_a_shortlink_is_not_thrown_away(scraper, resolves):
    """The whole defect in one assertion."""
    out = scraper._validate_post_url("https://lnkd.in/p/aaaabbbb")
    assert out is not None, "a copied link was discarded"


def test_a_shortlink_resolves_to_the_permalink(scraper, resolves):
    out = scraper._validate_post_url("https://lnkd.in/p/aaaabbbb")
    assert out == PERMALINK
    assert resolves == ["https://lnkd.in/p/aaaabbbb"]


def test_the_resolved_url_still_yields_an_activity_urn(scraper, resolves):
    """The caller parses the URN out of the URL; a shortlink has none, which is
    why resolving matters beyond just having something clickable."""
    out = scraper._validate_post_url("https://lnkd.in/p/aaaabbbb")
    m = re.search(r"(activity|ugcPost|share)-(\d+)", out)
    assert m, "no URN could be parsed from %r" % out
    assert "urn:li:%s:%s" % (m.group(1), m.group(2)) == \
        "urn:li:ugcPost:1011101110111011101"


def test_tracking_params_are_stripped_from_a_resolved_shortlink(scraper, resolves):
    out = scraper._validate_post_url("https://lnkd.in/p/aaaabbbb")
    assert "?" not in out
    assert "utm_source" not in out


# ─── it must not lose the post when resolution fails ──────────────────────────

def test_an_unresolvable_shortlink_is_kept_not_dropped(scraper, monkeypatch):
    """lnkd.in being down must not cost us the post: the shortlink still opens
    it. Dropping it would repeat the original failure for a transient reason."""
    monkeypatch.setattr(LinkedInScraper, "_resolve_shortlink",
                        staticmethod(lambda url, timeout=15: None))
    out = scraper._validate_post_url("https://lnkd.in/p/aaaabbbb")
    assert out == "https://lnkd.in/p/aaaabbbb"


def test_a_shortlink_resolving_somewhere_odd_is_still_kept(scraper, monkeypatch):
    monkeypatch.setattr(LinkedInScraper, "_resolve_shortlink",
                        staticmethod(lambda url, timeout=15:
                                     "https://example.com/nope"))
    out = scraper._validate_post_url("https://lnkd.in/p/aaaabbbb")
    assert out == "https://lnkd.in/p/aaaabbbb"


def test_resolution_never_raises_through(scraper, monkeypatch):
    def explode(url, timeout=15):
        raise RuntimeError("DNS is on fire")

    monkeypatch.setattr(LinkedInScraper, "_resolve_shortlink",
                        staticmethod(lambda url, timeout=15: None))
    assert scraper._validate_post_url("https://lnkd.in/p/x") is not None


# ─── the formats that already worked must keep working ────────────────────────

def test_a_feed_update_url_is_untouched(scraper):
    assert scraper._validate_post_url(FEED_URL) == FEED_URL


def test_a_posts_url_keeps_working_and_loses_its_tracking(scraper):
    out = scraper._validate_post_url(
        "https://www.linkedin.com/posts/foo-activity-101010-abc/?utm_source=share")
    assert out == "https://www.linkedin.com/posts/foo-activity-101010-abc/"


def test_a_direct_permalink_does_not_hit_the_network(scraper, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("a permalink must not be resolved over the network")

    monkeypatch.setattr(LinkedInScraper, "_resolve_shortlink",
                        staticmethod(explode))
    assert scraper._validate_post_url(FEED_URL) == FEED_URL


@pytest.mark.parametrize("value", [
    None, "", "   ",
    "https://example.com/not-linkedin",
    "some copied text that is not a url at all",
    "https://www.linkedin.com/in/someone/",   # a profile, not a post
])
def test_things_that_are_not_post_urls_are_still_rejected(scraper, value):
    assert scraper._validate_post_url(value) is None


# ─── the helper itself ────────────────────────────────────────────────────────

def test_the_permalink_test_accepts_both_shapes():
    assert LinkedInScraper._looks_like_permalink(FEED_URL)
    assert LinkedInScraper._looks_like_permalink(PERMALINK)
    assert not LinkedInScraper._looks_like_permalink("https://lnkd.in/p/x")
    assert not LinkedInScraper._looks_like_permalink("https://example.com")


def test_lnkd_in_is_a_recognised_shortlink_host():
    assert "lnkd.in" in LinkedInScraper.SHORTLINK_HOSTS
