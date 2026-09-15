"""Tests for the search-page login false-negative fix (search-health-login-fix).

The connector's people-search page URL is /search/results/people/..., which the
old is_logged_in_on_page allowlist (feed/in/mynetwork only) rejected — reporting
"login failed" for a logged-in session. Login is decided by the URL/authwall, not
by the presence of result cards, so an empty search is still "logged in".

No browser: is_logged_in_on_page reads driver.current_url, so a fake driver with a
settable URL exercises every case. run_search_health_check is driven with a fully
faked driver (no network, no Connect clicks).
"""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import selector_health as shc
from linkedin_automation.auto_connector import LinkedInAutoConnector as C


SEARCH_URL = "https://www.linkedin.com/search/results/people/?keywords=ai%20consultant&origin=FACETED_SEARCH"


class _UrlDriver:
    """Minimal driver stand-in exposing a settable current_url."""

    def __init__(self, url):
        self.current_url = url


# ─── is_logged_in_on_page: URL-based across feed AND search ───────────────────

@pytest.mark.parametrize("url", [
    SEARCH_URL,
    "https://www.linkedin.com/search/results/people/",
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/in/some-person/",
    "https://www.linkedin.com/mynetwork/",
])
def test_logged_in_urls(url):
    assert pm.is_logged_in_on_page(_UrlDriver(url)) is True


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/login",
    "https://www.linkedin.com/authwall?trk=x",
    "https://www.linkedin.com/checkpoint/challenge/",
    "https://www.linkedin.com/uas/login",
    "https://www.linkedin.com/signup/cold-join",
    "",
    "https://www.linkedin.com/",           # bare host, no authenticated path
])
def test_logged_out_urls(url):
    assert pm.is_logged_in_on_page(_UrlDriver(url)) is False


def test_login_redirect_carrying_search_path_is_logged_out():
    # A logged-out search bounces to /login with the original path as a param;
    # the "login" marker must win over the "/search" in the redirect param.
    url = ("https://www.linkedin.com/login?session_redirect="
           "https%3A%2F%2Fwww.linkedin.com%2Fsearch%2Fresults%2Fpeople%2F")
    assert pm.is_logged_in_on_page(_UrlDriver(url)) is False


def test_authwall_redirect_from_search_is_logged_out():
    url = "https://www.linkedin.com/authwall?sessionRedirect=%2Fsearch%2Fresults%2Fpeople%2F"
    assert pm.is_logged_in_on_page(_UrlDriver(url)) is False


def test_current_url_error_is_logged_out():
    class _Boom:
        @property
        def current_url(self):
            raise RuntimeError("no session")
    assert pm.is_logged_in_on_page(_Boom()) is False


def test_search_results_marker_registered():
    # Guard the fix: the search-results path is in the logged-in allowlist.
    assert "/search/results" in pm.LOGGED_IN_URL_MARKERS
    assert "authwall" in pm.LOGGED_OUT_URL_MARKERS


# ─── classify_search_page: login vs. empty-vs-broken ──────────────────────────

def test_classify_not_logged_in():
    assert shc.classify_search_page(False, 0) == "not_logged_in"
    assert shc.classify_search_page(False, 10) == "not_logged_in"   # cards irrelevant


def test_classify_logged_in_no_results():
    # Logged in but zero cards — empty search OR stale selector, NOT "not logged in".
    assert shc.classify_search_page(True, 0) == "no_results"


def test_classify_logged_in_ok():
    assert shc.classify_search_page(True, 5) == "ok"


# ─── run_search_health_check: end-to-end with a faked driver ──────────────────

class _FakeDriver:
    """Fake webdriver: get() lands on ``landing_url`` (simulating redirects);
    find_elements returns ``counts[selector]`` sham elements."""

    def __init__(self, landing_url, counts=None):
        self._landing = landing_url
        self.current_url = landing_url
        self._counts = counts or {}
        self.quit_called = False

    def get(self, url):
        self.current_url = self._landing

    def find_elements(self, by, sel):
        return [object()] * self._counts.get(sel, 0)

    def execute_script(self, *a, **k):
        return None

    def quit(self):
        self.quit_called = True


@pytest.fixture
def patch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(shc.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))

    def _install(driver):
        monkeypatch.setattr(pm, "create_driver", lambda profile_name=None: (driver, {}))
    return _install


def test_search_health_logged_in_with_results(patch_env):
    driver = _FakeDriver(SEARCH_URL, counts={C.SEARCH_RESULT_SELECTOR: 5})
    patch_env(driver)
    result = shc.run_search_health_check("demo", SEARCH_URL)
    assert result["logged_in"] is True
    assert result["search_state"] == "ok"
    assert driver.quit_called is True


def test_search_health_empty_search_is_still_logged_in(patch_env):
    # Zero result cards, but the URL confirms login -> NOT a login failure.
    driver = _FakeDriver(SEARCH_URL, counts={})
    patch_env(driver)
    result = shc.run_search_health_check("demo", SEARCH_URL)
    assert result["logged_in"] is True
    assert result["search_state"] == "no_results"


def test_search_health_authwall_raises_login_required(patch_env):
    driver = _FakeDriver("https://www.linkedin.com/authwall?trk=x", counts={})
    patch_env(driver)
    with pytest.raises(pm.LoginRequiredError):
        shc.run_search_health_check("demo", SEARCH_URL)
    assert driver.quit_called is True    # cleaned up even on the login-required path
