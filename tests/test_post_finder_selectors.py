"""Tests for the June 28 2026 selector refresh in linkedin_ai_post_finder.

No browser: LinkedInScraper is built with a dummy driver and its author-from-menu
helper is exercised with duck-typed fake elements. Also pins the new-first
ordering of the refreshed selector constants."""

import logging

from selenium.webdriver.common.by import By

from linkedin_automation.post_finder import LinkedInScraper


def _scraper():
    # _author_from_menu and the constants never touch the driver.
    return LinkedInScraper(driver=None, logger=logging.getLogger("test"))


class _FakeEl:
    def __init__(self, aria=None, text="", href=None):
        self._aria, self.text, self._href = aria, text, href

    def get_attribute(self, name):
        return {"aria-label": self._aria, "href": self._href}.get(name)


class _FakePost:
    """Minimal stand-in for a Selenium post element."""

    def __init__(self, button=None, links=None):
        self._button, self._links = button, links or []

    def find_element(self, by, sel):
        if self._button is None:
            raise Exception("no overflow menu")
        return self._button

    def find_elements(self, by, sel):
        return self._links


# ─── Refreshed selector constants (new hooks first, old kept as fallbacks) ─────

def test_post_selectors_listitem_first():
    sels = LinkedInScraper.POST_SELECTORS
    assert sels[0] == "div[data-testid='mainFeed'] div[role='listitem']"
    assert "div[role='listitem']" in sels
    # Old DOM hook retained as a fallback.
    assert "div[data-view-name='feed-full-update']" in sels


def test_control_menu_selector_uses_aria_label_first():
    sel = LinkedInScraper.CONTROL_MENU_SELECTOR
    assert sel.startswith("button[aria-label^='Open control menu']")
    # Old hook still present as a comma fallback.
    assert "feed-control-menu" in sel
    assert LinkedInScraper.CONTROL_MENU_AUTHOR_PREFIX == "Open control menu for post by "


def test_author_selectors_keep_old_fallbacks():
    sels = LinkedInScraper.AUTHOR_SELECTORS
    assert "a[href*='/in/']" in sels
    assert "a[href*='/company/']" in sels
    assert "a[data-view-name='feed-actor']" in sels  # fallback retained


# ─── _author_from_menu ────────────────────────────────────────────────────────

def test_author_from_menu_person_matches_profile_link():
    btn = _FakeEl(aria="Open control menu for post by Priya Nair")
    avatar = _FakeEl(text="", href="https://www.linkedin.com/in/priya-nair/")
    named = _FakeEl(text="Priya Nair", href="https://www.linkedin.com/in/priya-nair/")
    name, url = _scraper()._author_from_menu(_FakePost(btn, [avatar, named]))
    assert name == "Priya Nair"
    assert url == "https://www.linkedin.com/in/priya-nair/"


def test_author_from_menu_company_falls_back_to_first_link():
    btn = _FakeEl(aria="Open control menu for post by Cursor")
    logo = _FakeEl(text="", href="https://www.linkedin.com/company/cursor/")
    name, url = _scraper()._author_from_menu(_FakePost(btn, [logo]))
    assert name == "Cursor"
    assert url == "https://www.linkedin.com/company/cursor/"


def test_author_from_menu_no_button_returns_none():
    name, url = _scraper()._author_from_menu(_FakePost(button=None))
    assert (name, url) == (None, None)


def test_author_from_menu_wrong_label_returns_none():
    btn = _FakeEl(aria="Reaction button state: no reaction")
    name, url = _scraper()._author_from_menu(_FakePost(btn, []))
    assert (name, url) == (None, None)


def test_author_from_menu_used_first_by_extract(monkeypatch):
    # _extract_author_improved should short-circuit on a menu hit without
    # touching the text-analysis path (element.text not required).
    btn = _FakeEl(aria="Open control menu for post by Robin Shah")
    link = _FakeEl(text="Robin Shah", href="https://www.linkedin.com/in/robinshah/")
    name, url = _scraper()._extract_author_improved(_FakePost(btn, [link]))
    assert name == "Robin Shah"
    assert url == "https://www.linkedin.com/in/robinshah/"


def test_by_css_selector_constant_is_used():
    # Guard: the helper passes By.CSS_SELECTOR (string), so fakes ignoring `by`
    # mirror real usage.
    assert By.CSS_SELECTOR == "css selector"
