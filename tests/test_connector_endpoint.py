"""Flask test-client tests for the manual connector-start endpoint's search-URL
resolution (Dispatch 38.2): an explicit URL is used as-is; an omitted one
rotates through connector.search_urls and advances the pointer.

No browser, no network. ``run_job`` is stubbed so nothing actually spawns.
"""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as linkedin_dashboard
from linkedin_automation.auto_connector import resolve_search_url


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


def test_start_connector_requires_url_or_configured_list(api_client, monkeypatch):
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda profile_name=None: {"connector": {}})
    resp = api_client.post("/api/connector/work/start", json={})
    assert resp.status_code == 400
    assert "search_url" in resp.get_json()["error"]


def test_start_connector_uses_rotation_when_no_explicit_url(api_client, monkeypatch):
    monkeypatch.setattr(
        pm, "get_profile_config",
        lambda profile_name=None: {"connector": {"search_urls": ["https://a", "https://b"]}},
    )
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/connector/work/start", json={})
    assert resp.status_code == 200
    assert calls
    # do_connect's positional args: (job_id, do_connect, profile, url, max, pages, note)
    assert calls[0][0][3] == "https://a"


def test_start_connector_explicit_url_bypasses_and_does_not_advance_rotation(
    api_client, monkeypatch
):
    monkeypatch.setattr(
        pm, "get_profile_config",
        lambda profile_name=None: {"connector": {"search_urls": ["https://a", "https://b"]}},
    )
    calls = []
    monkeypatch.setattr(linkedin_dashboard, "run_job",
                        lambda *a, **k: calls.append((a, k)))
    resp = api_client.post("/api/connector/work/start", json={"search_url": "https://explicit"})
    assert resp.status_code == 200
    assert calls[0][0][3] == "https://explicit"
    # The rotation pointer never moved: the next no-explicit resolve still
    # starts at the front of the list.
    assert resolve_search_url("work") == "https://a"
