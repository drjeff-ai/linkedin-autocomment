"""Tests for connector config + daily/weekly tracking (connector-hardening).

Offline: pm.get_data_dir and pm.get_profile_config are redirected; no browser."""

import json

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import auto_connector as ac


@pytest.fixture
def tracker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    return tmp_path


# ─── ConnectionTracker daily + weekly ─────────────────────────────────────────

def test_daily_and_weekly_counts_increment(tracker_dir):
    t = ac.ConnectionTracker("x", weekly_limit=100, daily_limit=10)
    for i in range(3):
        t.record_sent(f"n{i}", f"https://www.linkedin.com/in/p{i}/")
    assert t.get_daily_count() == 3
    assert t.get_weekly_count() == 3


def test_remaining_is_min_of_daily_and_weekly(tracker_dir):
    # daily cap 5 binds before weekly cap 100.
    t = ac.ConnectionTracker("x", weekly_limit=100, daily_limit=5)
    for i in range(4):
        t.record_sent(f"n{i}", f"https://www.linkedin.com/in/p{i}/")
    assert t.get_remaining() == 1  # min(100-4, 5-4)


def test_weekly_cap_binds_when_lower(tracker_dir):
    t = ac.ConnectionTracker("x", weekly_limit=3, daily_limit=25)
    for i in range(2):
        t.record_sent(f"n{i}", f"https://www.linkedin.com/in/p{i}/")
    assert t.get_remaining() == 1  # min(3-2, 25-2)


def test_stats_include_daily_and_limits(tracker_dir):
    t = ac.ConnectionTracker("x", weekly_limit=40, daily_limit=7)
    t.record_sent("n", "https://www.linkedin.com/in/p/")
    s = t.get_stats()
    assert s["today"] == 1 and s["daily_limit"] == 7
    assert s["this_week"] == 1 and s["weekly_limit"] == 40
    assert s["remaining"] == min(40 - 1, 7 - 1)


def test_old_tracker_file_without_daily_counts(tracker_dir):
    # Pre-existing tracker files have no daily_counts — must not crash.
    (tracker_dir / "connection_tracker.json").write_text(
        '{"sent_requests": [], "weekly_counts": {}, "skipped": [], "errors": []}',
        encoding="utf-8",
    )
    t = ac.ConnectionTracker("x")
    assert t.get_daily_count() == 0


# ─── Connector reads config; CLI overrides ────────────────────────────────────

@pytest.fixture
def config(monkeypatch):
    cfg = {"connector": {"max_daily_requests": 7, "weekly_limit": 40,
                         "note_template": "hi from config"}}
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: cfg)
    return cfg


def test_connector_uses_config_defaults(config):
    c = ac.LinkedInAutoConnector(profile_name="x")
    assert c.max_requests == 7          # from max_daily_requests
    assert c.note_text == "hi from config"
    assert c.add_note is True            # derived from note presence
    assert c.weekly_limit == 40
    assert c.daily_limit == 7


def test_cli_args_override_config(config):
    c = ac.LinkedInAutoConnector(profile_name="x", max_requests=3, note_text="cli note")
    assert c.max_requests == 3
    assert c.note_text == "cli note"


def test_empty_note_means_no_note(config, monkeypatch):
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda profile_name=None: {"connector": {"max_daily_requests": 5}})
    c = ac.LinkedInAutoConnector(profile_name="x")
    assert c.note_text == ""
    assert c.add_note is False


# ─── Search-URL rotation (Dispatch 38.2) ──────────────────────────────────────

def test_next_search_url_advances_and_wraps(tracker_dir):
    t = ac.ConnectionTracker("x")
    urls = ["https://a", "https://b", "https://c"]
    seen = [t.next_search_url(urls) for _ in range(5)]
    assert seen == ["https://a", "https://b", "https://c", "https://a", "https://b"]


def test_next_search_url_single_url_repeats(tracker_dir):
    """A single-URL list behaves like today: every call returns the same URL."""
    t = ac.ConnectionTracker("x")
    urls = ["https://only"]
    assert [t.next_search_url(urls) for _ in range(3)] == ["https://only"] * 3


def test_next_search_url_empty_raises(tracker_dir):
    t = ac.ConnectionTracker("x")
    with pytest.raises(ValueError):
        t.next_search_url([])


def test_next_search_url_persists_across_restart(tracker_dir):
    """The pointer lives in the tracker FILE, not memory — a fresh tracker
    instance (simulating a process restart) continues where the last one
    left off."""
    urls = ["https://a", "https://b", "https://c"]
    first = ac.ConnectionTracker("x")
    assert first.next_search_url(urls) == "https://a"

    # A brand-new instance, as a restarted process would construct.
    second = ac.ConnectionTracker("x")
    assert second.next_search_url(urls) == "https://b"

    # Assert on the tracker file itself, not just the object's behavior.
    tracker_file = tracker_dir / "connection_tracker.json"
    assert tracker_file.exists()
    saved = json.loads(tracker_file.read_text(encoding="utf-8"))
    assert saved["rotation_index"] == 2


# ─── peek_next_search_url: read-only, never advances (Dispatch 39) ───────────

def test_peek_next_search_url_matches_what_next_would_return(tracker_dir):
    t = ac.ConnectionTracker("x")
    urls = ["https://a", "https://b", "https://c"]
    assert t.peek_next_search_url(urls) == "https://a"
    assert t.peek_next_search_url(urls) == "https://a"  # repeated peeks: same answer
    assert t.next_search_url(urls) == "https://a"  # the real call advances it
    assert t.peek_next_search_url(urls) == "https://b"


def test_peek_next_search_url_empty_returns_none_not_raise(tracker_dir):
    """Unlike next_search_url, peeking an empty list is a normal display
    state ("nothing queued"), not an error."""
    t = ac.ConnectionTracker("x")
    assert t.peek_next_search_url([]) is None


def test_peek_next_search_url_does_not_persist(tracker_dir):
    """Peeking never writes to disk - a fresh instance sees the same pointer."""
    urls = ["https://a", "https://b"]
    first = ac.ConnectionTracker("x")
    for _ in range(5):
        first.peek_next_search_url(urls)
    second = ac.ConnectionTracker("x")
    assert second.peek_next_search_url(urls) == "https://a"


def test_resolve_search_url_explicit_does_not_advance(tracker_dir, monkeypatch):
    """An explicit URL is used as-is and never even touches the tracker file."""
    monkeypatch.setattr(
        pm, "get_profile_config",
        lambda profile_name=None: {"connector": {"search_urls": ["https://a", "https://b"]}},
    )
    url = ac.resolve_search_url("x", explicit_url="https://explicit")
    assert url == "https://explicit"
    assert not (tracker_dir / "connection_tracker.json").exists()


def test_resolve_search_url_rotates_when_no_explicit(tracker_dir, monkeypatch):
    monkeypatch.setattr(
        pm, "get_profile_config",
        lambda profile_name=None: {"connector": {"search_urls": ["https://a", "https://b"]}},
    )
    assert ac.resolve_search_url("x") == "https://a"
    assert ac.resolve_search_url("x") == "https://b"
    assert ac.resolve_search_url("x") == "https://a"  # wraps


def test_resolve_search_url_raises_when_nothing_configured(tracker_dir, monkeypatch):
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda profile_name=None: {"connector": {}})
    with pytest.raises(ValueError):
        ac.resolve_search_url("x")


# ─── Cap check happens before the browser factory (Dispatch 38.3) ────────────

def test_run_at_daily_cap_never_creates_browser(tracker_dir, monkeypatch):
    """A profile already at its daily cap must not open a browser at all."""
    tracker = ac.ConnectionTracker("x", weekly_limit=100, daily_limit=3)
    for i in range(3):
        tracker.record_sent(f"n{i}", f"https://www.linkedin.com/in/p{i}/")

    def _must_not_be_called(*a, **k):
        raise AssertionError("pm.create_driver was called at the daily cap")
    monkeypatch.setattr(pm, "create_driver", _must_not_be_called)
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {"connector": {}})

    c = ac.LinkedInAutoConnector(profile_name="x", max_requests=10)
    c.weekly_limit, c.daily_limit = 100, 3
    results = c.run("https://search-url")
    assert results["ok"] is True
    assert results["session"]["sent"] == 0


def test_run_at_weekly_cap_never_creates_browser(tracker_dir, monkeypatch):
    """Weekly cap binds the same way, even with daily room to spare."""
    tracker = ac.ConnectionTracker("x", weekly_limit=2, daily_limit=25)
    for i in range(2):
        tracker.record_sent(f"n{i}", f"https://www.linkedin.com/in/p{i}/")

    def _must_not_be_called(*a, **k):
        raise AssertionError("pm.create_driver was called at the weekly cap")
    monkeypatch.setattr(pm, "create_driver", _must_not_be_called)
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {"connector": {}})

    c = ac.LinkedInAutoConnector(profile_name="x", max_requests=10)
    c.weekly_limit, c.daily_limit = 2, 25
    results = c.run("https://search-url")
    assert results["ok"] is True
    assert results["session"]["sent"] == 0


def test_run_under_cap_still_calls_browser_factory(tracker_dir, monkeypatch):
    """Sanity check for the fix above: under the cap, the factory IS called."""
    calls = []

    class FakeDriver:
        def quit(self):
            pass

    def fake_create_driver(profile_name, allow_production=False):
        calls.append(profile_name)
        return FakeDriver(), {"profile": profile_name}

    monkeypatch.setattr(pm, "create_driver", fake_create_driver)
    monkeypatch.setattr(pm, "login", lambda driver, profile: True)
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {"connector": {}})

    c = ac.LinkedInAutoConnector(profile_name="x", max_requests=10)
    c.weekly_limit, c.daily_limit = 100, 25
    c.run("https://search-url")  # fails past setup (no real driver) — fine, we
    # only care that the factory was reached at all.
    assert calls == ["x"]
