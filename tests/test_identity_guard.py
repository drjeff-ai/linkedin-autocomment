"""The identity guard: the one control between this tool and the wrong account.

It used to ask whether the configured slug appeared ANYWHERE in the resolved
profile URL. That is a substring test, so `in` matched every profile on
LinkedIn and a truncated vanity name matched a different human with a similar
one. Commenting as the wrong real person on a live post cannot be undone, so
this compares the `/in/<slug>` segment exactly and fails closed on anything it
cannot read.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402

REAL = "https://www.linkedin.com/in/example-person-one/?isSelfProfile=true"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _poster(url, raises=None):
    class Driver:
        current_url = url

        def get(self, u):
            if raises:
                raise raises

    class Poster:
        driver = Driver()

    return Poster()


# ─── the slug parser ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/in/abc-123/", "abc-123"),
    ("https://www.linkedin.com/in/abc-123", "abc-123"),
    ("https://www.linkedin.com/in/abc-123/?isSelfProfile=true", "abc-123"),
    ("https://www.linkedin.com/in/ABC-123/", "abc-123"),
    ("https://www.linkedin.com/in/abc-123/detail/contact-info/", "abc-123"),
    ("https://www.linkedin.com/feed/", None),
    ("https://www.linkedin.com/login", None),
    ("https://www.linkedin.com/checkpoint/challenge/", None),
    ("", None),
    (None, None),
])
def test_the_slug_parser(url, expected):
    assert cp.profile_slug(url) == expected


# ─── exact matching ───────────────────────────────────────────────────────────

def test_the_right_account_passes():
    ok, detail = cp.verify_identity(_poster(REAL), "example-person-one")
    assert ok is True
    assert "example-person-one" in detail


def test_case_does_not_matter():
    ok, _ = cp.verify_identity(_poster(REAL), "EXAMPLE-Person-One")
    assert ok is True


def test_a_whole_url_in_the_config_also_works():
    ok, _ = cp.verify_identity(
        _poster(REAL), "https://www.linkedin.com/in/example-person-one/")
    assert ok is True


@pytest.mark.parametrize("slug", [
    "example-person",       # a truncated vanity name: a DIFFERENT human
    "example",
    "person-one",           # a suffix
    "in",                   # matched every profile on the site
    "linkedin",
    "e",
    "example-person-one-x",  # a superset
])
def test_a_slug_that_is_merely_similar_is_refused(slug):
    ok, detail = cp.verify_identity(_poster(REAL), slug)
    assert ok is False, "%r was accepted for %s" % (slug, REAL)
    assert "refusing to comment" in detail


def test_a_different_account_is_refused_and_names_both():
    ok, detail = cp.verify_identity(_poster(REAL), "someone-else")
    assert ok is False
    assert "example-person-one" in detail
    assert "someone-else" in detail


# ─── failing closed ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/login",
    "https://www.linkedin.com/checkpoint/challenge/",
    "",
])
def test_anything_that_is_not_a_profile_page_is_refused(url):
    """Not logged in, or bounced to a checkpoint, is not an identity."""
    ok, detail = cp.verify_identity(_poster(url), "example-person-one")
    assert ok is False
    assert "refusing to comment" in detail


def test_a_browser_error_is_refused_not_assumed():
    ok, detail = cp.verify_identity(
        _poster(REAL, raises=RuntimeError("session died")), "example-person-one")
    assert ok is False
    assert "could not resolve" in detail


def test_no_configured_identity_is_a_documented_no_op():
    """Legal, but it disables the guard - the runbook says configure it."""
    ok, detail = cp.verify_identity(_poster(REAL), "")
    assert ok is True
    assert "no expected identity configured" in detail
