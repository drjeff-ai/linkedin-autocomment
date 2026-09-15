"""Tests for connector config + daily/weekly tracking (connector-hardening).

Offline: pm.get_data_dir and pm.get_profile_config are redirected; no browser."""

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
