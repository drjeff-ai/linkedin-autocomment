"""Tests for the link-first connector refactor (connector-link-first).

LinkedIn removed data-view-name (no clean card/name selector), so the connector
iterates the Connect LINKS directly: each link's aria-label "Invite <Name> to
connect" gives the name and its /preload/search-custom-invite/ href gives the
invite action + vanity (used as a stable dedup id). No browser and NO invites
sent — click_connect is stubbed in the flow test.
"""

from linkedin_automation import auto_connector as ac
from linkedin_automation.auto_connector import LinkedInAutoConnector as C


def _conn():
    return C.__new__(C)


class _Link:
    def __init__(self, name, vanity, extra_href=""):
        self._aria = f"Invite {name} to connect"
        self._href = f"/preload/search-custom-invite/?vanityName={vanity}{extra_href}"

    def get_attribute(self, name):
        return {"aria-label": self._aria, "href": self._href}.get(name)

    def is_displayed(self):
        return True

    text = ""


# ─── name_from_invite_label ───────────────────────────────────────────────────

def test_name_parse_basic():
    assert C.name_from_invite_label("Invite Jordan Rivera to connect") == "Jordan Rivera"


def test_name_parse_case_insensitive_affixes():
    assert C.name_from_invite_label("invite Ada Byron TO CONNECT") == "Ada Byron"


def test_name_parse_extra_whitespace():
    assert C.name_from_invite_label("  Invite   Grace Hopper to connect  ") == "Grace Hopper"


def test_name_parse_empty_or_nonmatching():
    assert C.name_from_invite_label("") == "Unknown"
    assert C.name_from_invite_label("Invite  to connect") == "Unknown"


def test_name_parse_keeps_name_with_no_suffix():
    # Defensive: a truncated label without the suffix still yields the name part.
    assert C.name_from_invite_label("Invite Marie Curie") == "Marie Curie"


# ─── CONNECT_LINK_SELECTORS constant ──────────────────────────────────────────

def test_connect_link_selector_is_arialabel_href():
    assert C.CONNECT_LINK_SELECTORS[0] == (
        "a[aria-label^='Invite'][href*='/preload/search-custom-invite/']"
    )


# ─── extract_person_from_link ─────────────────────────────────────────────────

def test_extract_person_from_link_full():
    link = _Link("Jamie Lee", "jamie-lee")
    info = _conn().extract_person_from_link(link)
    assert info["name"] == "Jamie Lee"
    assert info["has_connect"] is True
    assert info["connect_element"] is link
    assert info["vanity_name"] == "jamie-lee"
    # Dedup id derived from the person's own invite vanity.
    assert info["profile_url"] == "https://www.linkedin.com/in/jamie-lee/"


def test_extract_person_from_link_strips_extra_href_params():
    link = _Link("Sam Carter", "sam-carter-000000000", extra_href="&trk=x")
    info = _conn().extract_person_from_link(link)
    assert info["vanity_name"] == "sam-carter-000000000"
    assert info["profile_url"].endswith("/in/sam-carter-000000000/")


# ─── find_connect_links (page-level discovery) ────────────────────────────────

class _Driver:
    def __init__(self, links_by_selector, url="https://www.linkedin.com/search/results/people/"):
        self._by_sel = links_by_selector
        self.current_url = url

    def find_elements(self, by, sel):
        return self._by_sel.get(sel, [])

    def execute_script(self, *a, **k):
        return None


def test_find_connect_links_returns_invite_links():
    links = [_Link("A B", "ab"), _Link("C D", "cd")]
    conn = _conn()
    conn.driver = _Driver({C.CONNECT_LINK_SELECTORS[0]: links})
    assert conn.find_connect_links() == links


def test_find_connect_links_empty_when_none():
    conn = _conn()
    conn.driver = _Driver({})
    assert conn.find_connect_links() == []


# ─── _collect_page_targets: link-first primary, card fallback ─────────────────

def test_collect_targets_prefers_links():
    links = [_Link("A B", "ab")]
    conn = _conn()
    conn.driver = _Driver({C.CONNECT_LINK_SELECTORS[0]: links})
    items, extractor, source = conn._collect_page_targets()
    assert items == links
    assert extractor == conn.extract_person_from_link
    assert source == "connect-links"


def test_collect_targets_falls_back_to_cards():
    conn = _conn()
    # No connect links, but the card selector matches.
    conn.driver = _Driver({C.SEARCH_RESULT_SELECTOR: ["card1", "card2"]})
    items, extractor, source = conn._collect_page_targets()
    assert items == ["card1", "card2"]
    assert extractor == conn.extract_person_info
    assert source == "result-cards"


def test_collect_targets_empty_when_nothing():
    conn = _conn()
    conn.driver = _Driver({})
    items, extractor, source = conn._collect_page_targets()
    assert items == [] and extractor is None and source == "none"


# ─── process_page end-to-end (link-first), NO invites actually sent ───────────

class _Tracker:
    def __init__(self, seeded=None):
        self.sent = list(seeded or [])
        self.skipped = []
        self.errors = []

    def already_sent(self, url):
        return any(s[1] == url for s in self.sent)

    def record_sent(self, name, url, title=""):
        self.sent.append((name, url, title))

    def record_skip(self, name, url, reason):
        self.skipped.append((name, url, reason))

    def record_error(self, name, url, err):
        self.errors.append((name, url, err))


def _neutralize_timing(monkeypatch):
    for fn in ("scroll_to_element", "simulate_reading", "human_sleep", "take_break"):
        monkeypatch.setattr(ac.hb, fn, lambda *a, **k: None)
    monkeypatch.setattr(ac.hb, "random_delay", lambda *a, **k: 0)
    monkeypatch.setattr(ac.hb, "should_take_break", lambda *a, **k: False)
    monkeypatch.setattr(ac.time, "sleep", lambda *a, **k: None)


def _make_conn(monkeypatch, links, tracker):
    conn = _conn()
    conn.driver = _Driver({C.CONNECT_LINK_SELECTORS[0]: links})
    conn.tracker = tracker
    conn.max_requests = 10
    conn.sent_count = conn.skipped_count = conn.error_count = 0
    conn.debug = False
    conn.stop_file = "___no_such_stop_file___"
    # CRITICAL: do NOT click/send for real — stub the click as a successful send.
    monkeypatch.setattr(conn, "click_connect", lambda info: True)
    return conn


def test_process_page_link_first_sends_each(monkeypatch):
    _neutralize_timing(monkeypatch)
    links = [_Link("Jordan Rivera", "jordan-rivera"), _Link("Jamie Lee", "jlee")]
    tracker = _Tracker()
    conn = _make_conn(monkeypatch, links, tracker)

    sent = conn.process_page()

    assert sent == 2 and conn.sent_count == 2
    assert [s[0] for s in tracker.sent] == ["Jordan Rivera", "Jamie Lee"]
    assert [s[1] for s in tracker.sent] == [
        "https://www.linkedin.com/in/jordan-rivera/",
        "https://www.linkedin.com/in/jlee/",
    ]


def test_process_page_dedups_already_sent(monkeypatch):
    _neutralize_timing(monkeypatch)
    links = [_Link("Jordan Rivera", "jordan-rivera"), _Link("New Person", "newp")]
    # Jordan already invited previously (by vanity-derived URL).
    tracker = _Tracker(seeded=[("Jordan Rivera", "https://www.linkedin.com/in/jordan-rivera/", "")])
    conn = _make_conn(monkeypatch, links, tracker)

    sent = conn.process_page()

    assert sent == 1 and conn.sent_count == 1
    assert conn.skipped_count == 1
    assert [s[0] for s in tracker.sent] == ["Jordan Rivera", "New Person"]  # 1 seeded + 1 new
    assert tracker.sent[-1][1] == "https://www.linkedin.com/in/newp/"


def test_process_page_respects_max_requests(monkeypatch):
    _neutralize_timing(monkeypatch)
    links = [_Link(f"P{i}", f"p{i}") for i in range(5)]
    tracker = _Tracker()
    conn = _make_conn(monkeypatch, links, tracker)
    conn.max_requests = 2

    sent = conn.process_page()
    assert sent == 2 and conn.sent_count == 2
