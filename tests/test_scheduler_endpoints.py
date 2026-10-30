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
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "connector"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "fired"
    assert calls


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
    pm.save_profile_config("work", {"connector": {"max_daily_requests": 1}})
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/scheduler/work/run-now", json={"job": "connector"})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["action"] == "at_cap"
    assert calls == []
