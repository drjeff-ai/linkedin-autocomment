"""Flask test-client tests for the scheduler HTTP endpoints.

No browser, no network. Uses the shared api_client fixture (storage redirected
to tmp). run-now for post_comments is safe here because the tmp GENERATED queue
is empty, so it resolves to queue_empty without spawning a browser job.
"""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as linkedin_dashboard


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


def test_status_returns_defaults(api_client):
    resp = api_client.get("/api/scheduler/work/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is False
    assert data["jobs"]["post_comments"]["enabled"] is True
    assert data["jobs"]["post_comments"]["count_min"] == 4
    assert "recent_runs" in data


def test_toggle_master_enables(api_client):
    resp = api_client.post("/api/scheduler/work/toggle", json={"job": "master", "enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["scheduler"]["enabled"] is True
    # Persisted: status now reflects it.
    assert api_client.get("/api/scheduler/work/status").get_json()["enabled"] is True


def test_toggle_requires_enabled(api_client):
    resp = api_client.post("/api/scheduler/work/toggle", json={"job": "master"})
    assert resp.status_code == 400


def test_toggle_rejects_unknown_target(api_client):
    resp = api_client.post("/api/scheduler/work/toggle", json={"job": "bogus", "enabled": True})
    assert resp.status_code == 400


def test_config_updates_windows_and_counts(api_client):
    resp = api_client.post("/api/scheduler/work/config", json={
        "post_comments": {"count_max": 12, "windows": [["07:00", "09:00"]]},
    })
    assert resp.status_code == 200
    pc = resp.get_json()["scheduler"]["post_comments"]
    assert pc["count_max"] == 12
    assert pc["windows"] == [["07:00", "09:00"]]
    assert pc["count_min"] == 4  # default preserved


def test_run_now_post_comments_empty_queue(api_client):
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "post_comments"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "queue_empty"


def test_run_now_rejects_bad_job(api_client):
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "nope"})
    assert resp.status_code == 400


def test_run_now_scrape_submits_job(api_client, monkeypatch):
    """run-now scrape reaches the executor; stub run_job so no browser spawns."""
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "scrape"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "fired"
    assert calls  # a (stubbed) browser job was submitted


# ─── auto_mode and auto_cycle (Dispatch 40) ────────────────────────────────────

def test_status_includes_auto_cycle_job_and_auto_mode_off_by_default(api_client):
    data = api_client.get("/api/scheduler/work/status").get_json()
    assert data["auto_mode"] is False
    assert data["auto_mode_stopped_reason"] is None
    cyc = data["jobs"]["auto_cycle"]
    assert cyc["enabled"] is False
    from linkedin_automation import scheduler as sched_mod
    assert cyc["max_comments"] == sched_mod.GENERATE_DEFAULT_CAP
    assert cyc["count_min"] == 4 and cyc["count_max"] == 8


def test_toggle_auto_mode_enables_the_cycle_only(api_client):
    """auto_mode now drives exactly one job (auto_cycle) - it must not touch
    scrape/post_comments's own independent toggles in either direction."""
    resp = api_client.post("/api/scheduler/work/toggle",
                           json={"job": "auto_mode", "enabled": True})
    assert resp.status_code == 200
    sched = resp.get_json()["scheduler"]
    assert sched["auto_mode"] is True
    assert sched["enabled"] is True
    assert sched["auto_cycle"]["enabled"] is True
    assert sched["scrape"]["enabled"] is False  # untouched (shipped default)

    status = api_client.get("/api/scheduler/work/status").get_json()
    assert status["auto_mode"] is True
    assert status["jobs"]["auto_cycle"]["enabled"] is True


def test_toggle_auto_mode_off(api_client):
    api_client.post("/api/scheduler/work/toggle", json={"job": "auto_mode", "enabled": True})
    resp = api_client.post("/api/scheduler/work/toggle",
                           json={"job": "auto_mode", "enabled": False})
    assert resp.status_code == 200
    assert resp.get_json()["scheduler"]["auto_mode"] is False


def test_run_now_auto_cycle_submits_job(api_client, monkeypatch):
    """run-now auto_cycle reaches the executor; stub run_job so no browser
    spawns. The real stage sequencing lives inside the stubbed job body, so
    this only proves the submit path is reached - see test_scheduler.py for
    run_auto_cycle's own call-order/short-circuit tests."""
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "auto_cycle"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "fired"
    assert calls


# ─── connector (Dispatch 38) ───────────────────────────────────────────────────

def test_status_includes_connector_job(api_client):
    """connector joins the job list, default off, with its own pages field."""
    resp = api_client.get("/api/scheduler/work/status")
    data = resp.get_json()
    assert data["jobs"]["connector"]["enabled"] is False
    assert data["jobs"]["connector"]["pages"] == 5


def test_toggle_connector_job(api_client):
    resp = api_client.post("/api/scheduler/work/toggle",
                           json={"job": "connector", "enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["scheduler"]["connector"]["enabled"] is True
    assert api_client.get("/api/scheduler/work/status").get_json()["jobs"]["connector"]["enabled"] is True


def test_run_now_connector_submits_job(api_client, monkeypatch):
    """run-now connector reaches the executor (under the empty-tracker cap);
    stub run_job so no subprocess spawns."""
    pm.save_profile_config("work", {"connector": {"search_urls": ["https://search-url"]}})
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "connector"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "fired"
    assert calls


def test_run_now_connector_empty_search_urls_does_not_submit(api_client, monkeypatch):
    """No search_urls configured: run-now reports queue_empty, no submit -
    the empty-list case never reaches run_job (the browser factory)."""
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "connector"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "queue_empty"
    assert calls == []


def test_run_now_connector_at_cap_does_not_submit(api_client, monkeypatch):
    """A profile already at its daily cap: run-now reports at_cap, no submit.

    The scheduler's config_fn is bound to the real pm.get_profile_config at
    construction time, so this writes through the real (tmp-redirected)
    config file rather than monkeypatching get_profile_config itself — a
    patched attribute wouldn't be seen by that already-captured reference.
    """
    from linkedin_automation.auto_connector import ConnectionTracker
    tracker = ConnectionTracker("work", weekly_limit=100, daily_limit=1)
    tracker.record_sent("n", "https://www.linkedin.com/in/p/")
    pm.save_profile_config("work", {"connector": {"max_daily_requests": 1,
                                                   "search_urls": ["https://search-url"]}})
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "connector"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "at_cap"
    assert calls == []


# ─── connector scheduler status + search-URL rotation UI (Dispatch 39) ────────

def test_connector_status_defaults(api_client):
    """No config yet: empty list, no next URL, cap stats from the shipped
    defaults (not a separate calculation from ConnectionTracker's own)."""
    from linkedin_automation.auto_connector import ConnectionTracker
    resp = api_client.get("/api/scheduler/work/connector/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["search_urls"] == []
    assert data["next_url"] is None
    assert data["sent_today"] == 0
    assert data["sent_this_week"] == 0
    assert data["daily_limit"] == ConnectionTracker.DEFAULT_DAILY_LIMIT
    assert data["weekly_limit"] == ConnectionTracker.DEFAULT_WEEKLY_LIMIT
    assert data["at_cap"] is False


def test_connector_status_reflects_configured_urls_and_caps(api_client):
    pm.save_profile_config("work", {"connector": {
        "search_urls": ["https://u1", "https://u2"],
        "max_daily_requests": 3, "weekly_limit": 10,
    }})
    resp = api_client.get("/api/scheduler/work/connector/status")
    data = resp.get_json()
    assert data["search_urls"] == ["https://u1", "https://u2"]
    assert data["next_url"] == "https://u1"  # rotation_index starts at 0
    assert data["daily_limit"] == 3
    assert data["weekly_limit"] == 10


def test_connector_status_matches_connection_tracker_counts(api_client):
    """The caps/counts shown are ConnectionTracker's own numbers, not a
    separate calculation - record a real send and confirm it shows up."""
    from linkedin_automation.auto_connector import ConnectionTracker
    pm.save_profile_config("work", {"connector": {
        "search_urls": ["https://u1"], "max_daily_requests": 5, "weekly_limit": 50,
    }})
    tracker = ConnectionTracker("work", weekly_limit=50, daily_limit=5)
    tracker.record_sent("n", "https://www.linkedin.com/in/p/")
    resp = api_client.get("/api/scheduler/work/connector/status")
    data = resp.get_json()
    assert data["sent_today"] == 1
    assert data["sent_this_week"] == 1
    assert data["at_cap"] is False


def test_connector_status_at_cap_true_when_exhausted(api_client):
    from linkedin_automation.auto_connector import ConnectionTracker
    pm.save_profile_config("work", {"connector": {
        "search_urls": ["https://u1"], "max_daily_requests": 1, "weekly_limit": 50,
    }})
    tracker = ConnectionTracker("work", weekly_limit=50, daily_limit=1)
    tracker.record_sent("n", "https://www.linkedin.com/in/p/")
    resp = api_client.get("/api/scheduler/work/connector/status")
    assert resp.get_json()["at_cap"] is True


def test_connector_urls_add_persists_across_reload(api_client):
    resp = api_client.post("/api/scheduler/work/connector/urls",
                           json={"search_urls": ["https://a", "https://b"]})
    assert resp.status_code == 200
    assert resp.get_json()["search_urls"] == ["https://a", "https://b"]
    reread = api_client.get("/api/scheduler/work/connector/status").get_json()
    assert reread["search_urls"] == ["https://a", "https://b"]


def test_connector_urls_remove_persists(api_client):
    api_client.post("/api/scheduler/work/connector/urls",
                    json={"search_urls": ["https://a", "https://b"]})
    api_client.post("/api/scheduler/work/connector/urls",
                    json={"search_urls": ["https://b"]})
    reread = api_client.get("/api/scheduler/work/connector/status").get_json()
    assert reread["search_urls"] == ["https://b"]


def test_connector_urls_reorder_persists_and_next_url_matches_pointer(api_client):
    """Reordering changes which URL is "next" only if the rotation pointer
    still points at the same INDEX - this asserts the next-URL indicator
    always matches the stored pointer, not a guess."""
    api_client.post("/api/scheduler/work/connector/urls",
                    json={"search_urls": ["https://a", "https://b"]})
    status = api_client.get("/api/scheduler/work/connector/status").get_json()
    assert status["next_url"] == "https://a"  # pointer at index 0

    api_client.post("/api/scheduler/work/connector/urls",
                    json={"search_urls": ["https://b", "https://a"]})
    status = api_client.get("/api/scheduler/work/connector/status").get_json()
    assert status["search_urls"] == ["https://b", "https://a"]
    assert status["next_url"] == "https://b"  # pointer still at index 0


def test_connector_urls_blank_entries_dropped(api_client):
    resp = api_client.post("/api/scheduler/work/connector/urls",
                           json={"search_urls": ["https://a", "  ", ""]})
    assert resp.get_json()["search_urls"] == ["https://a"]


def test_connector_urls_rejects_non_list(api_client):
    resp = api_client.post("/api/scheduler/work/connector/urls",
                           json={"search_urls": "https://a"})
    assert resp.status_code == 400


def test_connector_urls_leaves_other_connector_keys_alone(api_client):
    """Saving the URL list must not clobber max_daily_requests/weekly_limit -
    read_stored_config + a scoped merge, not a blind overwrite."""
    pm.save_profile_config("work", {"connector": {
        "max_daily_requests": 7, "weekly_limit": 77, "note_template": "hi",
    }})
    api_client.post("/api/scheduler/work/connector/urls",
                    json={"search_urls": ["https://a"]})
    stored = pm.read_stored_config("work")
    assert stored["connector"]["max_daily_requests"] == 7
    assert stored["connector"]["weekly_limit"] == 77
    assert stored["connector"]["note_template"] == "hi"
    assert stored["connector"]["search_urls"] == ["https://a"]
