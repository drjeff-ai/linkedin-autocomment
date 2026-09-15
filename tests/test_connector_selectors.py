"""Tests for the June 30 2026 connector selector refresh (connector-selectors-fix).

No browser: the connector is built with __new__ (skipping __init__) and exercised
with duck-typed fake cards/elements. Locks in that the Connect control is detected
by aria-label/href (it's an icon link with NO visible text — the prior text-only
check found 0 Connects) and the refreshed selector constants.
"""

from linkedin_automation import auto_connector as ac
from linkedin_automation.auto_connector import LinkedInAutoConnector as C


def _conn():
    # _is_connect_control / _find_connect_in_card don't need a real session.
    return C.__new__(C)


class _FakeEl:
    def __init__(self, aria=None, href=None, text="", displayed=True):
        self._attrs = {"aria-label": aria, "href": href}
        self.text = text
        self._displayed = displayed

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_displayed(self):
        return self._displayed


class _FakeCard:
    """Maps a CSS selector -> list of elements (ignores the By arg, like the real
    find_elements call site). find_element raises (no profile link / title)."""

    def __init__(self, mapping=None):
        self._m = mapping or {}

    def find_elements(self, by, sel):
        return self._m.get(sel, [])

    def find_element(self, by, sel):
        raise Exception("not present")

    text = ""


# ─── Refreshed selector constants (new hooks first, legacy as fallbacks) ──────

def test_card_selector_is_people_search_result_first():
    assert C.RESULT_CARD_SELECTORS[0] == "div[data-view-name='people-search-result']"
    assert C.SEARCH_RESULT_SELECTOR == "div[data-view-name='people-search-result']"


def test_connect_selectors_use_arialabel_href_first():
    assert C.CONNECT_BUTTON_SELECTORS[0] == (
        "a[aria-label^='Invite'][href*='/preload/search-custom-invite/']"
    )
    # The wrapper div is retained as a hook.
    assert C.CONNECT_ACTION_SELECTOR == "div[data-view-name='edge-creation-connect-action']"


def test_pagination_selectors_use_datatestid_first():
    assert C.PAGINATION_NEXT_SELECTORS[0] == (
        "button[data-testid='pagination-controls-next-button-visible']"
    )
    # Legacy artdeco Next kept as a fallback.
    assert "button.artdeco-pagination__button--next" in C.PAGINATION_NEXT_SELECTORS
    assert C.NEXT_PAGE_SELECTORS is C.PAGINATION_NEXT_SELECTORS


def test_name_selector_unchanged():
    assert C.RESULT_NAME_SELECTOR == "a[data-view-name='search-result-lockup-title']"


# ─── _is_connect_control: aria-label / href, NOT visible text ─────────────────

def test_is_connect_control_via_aria_label():
    el = _FakeEl(aria="Invite Jordan Rivera to connect", href="/preload/search-custom-invite/?vanityName=jor", text="")
    assert _conn()._is_connect_control(el) is True


def test_is_connect_control_via_href_even_with_empty_text():
    # The real Connect link is icon-only: empty .text but a preload href.
    el = _FakeEl(aria=None, href="/preload/search-custom-invite/?vanityName=x", text="")
    assert _conn()._is_connect_control(el) is True


def test_is_connect_control_via_text_fallback():
    el = _FakeEl(text="Connect")
    assert _conn()._is_connect_control(el) is True


def test_is_connect_control_rejects_follow_and_message():
    follow = _FakeEl(aria="Follow Taylor Brooks", href="https://www.linkedin.com/in/x/", text="Follow")
    message = _FakeEl(aria="Send a message to Casey M", href="/messaging/compose/?x", text="Message")
    assert _conn()._is_connect_control(follow) is False
    assert _conn()._is_connect_control(message) is False


# ─── _find_connect_in_card ────────────────────────────────────────────────────

def test_find_connect_in_card_returns_connect_element():
    connect = _FakeEl(aria="Invite Alex Kim to connect",
                      href="/preload/search-custom-invite/?vanityName=alx")
    card = _FakeCard({C.CONNECT_BUTTON_SELECTORS[0]: [connect]})
    assert _conn()._find_connect_in_card(card) is connect


def test_find_connect_in_card_none_when_no_connect_and_no_more():
    # No connect hooks and no 'More' button -> None (driver untouched).
    assert _conn()._find_connect_in_card(_FakeCard({})) is None


def test_extract_person_info_sets_has_connect_from_arialabel():
    connect = _FakeEl(aria="Invite Jamie Lee to connect",
                      href="/preload/search-custom-invite/?vanityName=jamie-lee")
    card = _FakeCard({C.CONNECT_BUTTON_SELECTORS[0]: [connect]})
    info = _conn().extract_person_info(card)
    assert info["has_connect"] is True
    assert info["connect_element"] is connect
    assert info["vanity_name"] == "jamie-lee"


# ─── 'More' overflow fallback (point 3, defensive) ────────────────────────────

class _FakeDriver:
    def __init__(self, menu_elements):
        self._menu = menu_elements

    def find_elements(self, by, sel):
        return self._menu

    def find_element(self, by, sel):
        raise Exception("not present")


def test_find_connect_behind_more_opens_menu_and_finds_connect(monkeypatch):
    monkeypatch.setattr(ac.hb, "human_click", lambda *a, **k: None)
    monkeypatch.setattr(ac.hb, "human_sleep", lambda *a, **k: None)
    more_btn = _FakeEl(aria="More actions", displayed=True)
    connect = _FakeEl(aria="Invite Pat Q to connect",
                      href="/preload/search-custom-invite/?vanityName=patq", displayed=True)
    card = _FakeCard({C.MORE_BUTTON_SELECTORS[0]: [more_btn]})
    conn = _conn()
    conn.driver = _FakeDriver([connect])
    assert conn._find_connect_behind_more(card) is connect


def test_find_connect_behind_more_none_without_more_button():
    # No More button on the card -> None, and the driver is never touched.
    assert _conn()._find_connect_behind_more(_FakeCard({})) is None
