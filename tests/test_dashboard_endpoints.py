"""Flask test-client tests for the dashboard HTTP endpoints (ROADMAP Phase 5).

No Selenium, no Chrome, no network. Profile CRUD, comment listing, and the
index route. Also guards the profile-route fix (handlers must accept the
``<name>`` URL var)."""

import json

import pytest

from linkedin_automation import profile_manager as pm


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    """Keep profile listing deterministic regardless of any .env credentials."""
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


# ─── Profiles: list / create ──────────────────────────────────────────────────

def test_profiles_empty(api_client):
    resp = api_client.get("/api/profiles")
    assert resp.status_code == 200
    assert resp.get_json() == {"profiles": {}, "default": None}


def test_create_profile_then_list(api_client):
    resp = api_client.post("/api/profiles", json={
        "name": "work", "username": "w@x.com", "password": "p@ss+1!",
    })
    assert resp.status_code == 200
    listing = api_client.get("/api/profiles").get_json()
    assert "work" in listing["profiles"]
    assert listing["profiles"]["work"]["username"] == "w@x.com"


def test_create_profile_missing_fields(api_client):
    resp = api_client.post("/api/profiles", json={"name": "x"})
    assert resp.status_code == 400


def test_create_duplicate_profile(api_client):
    api_client.post("/api/profiles", json={"name": "dup", "username": "a@b.com", "password": "x"})
    resp = api_client.post("/api/profiles", json={"name": "dup", "username": "a@b.com", "password": "x"})
    assert resp.status_code == 409


# ─── Profiles: set default / delete (route-param fix) ─────────────────────────

def test_set_default_profile_route(api_client):
    api_client.post("/api/profiles", json={"name": "a", "username": "a@b.com", "password": "x"})
    api_client.post("/api/profiles", json={"name": "b", "username": "b@b.com", "password": "y"})

    resp = api_client.post("/api/profiles/b/default")
    assert resp.status_code == 200
    assert api_client.get("/api/profiles").get_json()["default"] == "b"


def test_set_default_unknown_profile(api_client):
    resp = api_client.post("/api/profiles/ghost/default")
    assert resp.status_code == 404


def test_delete_profile_route(api_client):
    api_client.post("/api/profiles", json={"name": "temp", "username": "t@b.com", "password": "x"})
    resp = api_client.delete("/api/profiles/temp")
    assert resp.status_code == 200
    assert "temp" not in api_client.get("/api/profiles").get_json()["profiles"]


def test_delete_unknown_profile(api_client):
    resp = api_client.delete("/api/profiles/ghost")
    assert resp.status_code == 404


# ─── Comments listing ─────────────────────────────────────────────────────────

def test_get_comments_empty(api_client):
    resp = api_client.get("/api/comments/demo")
    assert resp.status_code == 200
    assert resp.get_json()["comments"] == []


def test_get_comments_lists_generated(api_client, comments_dir, make_comment):
    comments = [make_comment(i) for i in range(1, 4)]
    (comments_dir / "comments_20260625_000000.json").write_text(
        json.dumps({"comments": comments}), encoding="utf-8"
    )
    resp = api_client.get("/api/comments/demo")
    assert resp.status_code == 200
    assert len(resp.get_json()["comments"]) == 3


def test_save_endpoint_reports_count(api_client, comments_dir, make_comment):
    comments = [make_comment(i) for i in range(1, 4)]
    resp = api_client.post("/api/comments/demo/save", json={"comments": comments})
    assert resp.status_code == 200
    assert resp.get_json()["count"] == 3


def test_get_comments_returns_both_field_conventions(api_client, comments_dir, make_comment):
    # Generator output has only post_url/post_author; get_comments must merge the
    # normalized url/author so the frontend's c.url / c.author resolve (AUDIT 3d).
    comments = [make_comment(i) for i in range(1, 3)]  # post_url/post_author keys
    (comments_dir / "comments_20260628_000000.json").write_text(
        json.dumps({"comments": comments}), encoding="utf-8"
    )
    out = api_client.get("/api/comments/demo").get_json()["comments"]
    assert len(out) == 2
    for c in out:
        assert c["post_url"] and c["url"] == c["post_url"]       # both present, equal
        assert c["post_author"] and c["author"] == c["post_author"]


# ─── Profile config endpoints ─────────────────────────────────────────────────

def test_get_config_returns_structure(api_client):
    cfg = api_client.get("/api/profiles/demo/config").get_json()
    assert "post_finder" in cfg and "comment_generator" in cfg


def test_post_config_then_get_roundtrips(api_client):
    api_client.post("/api/profiles/demo/config",
                    json={"comment_generator": {"persona": "VFX lead"}})
    cfg = api_client.get("/api/profiles/demo/config").get_json()
    assert cfg["comment_generator"]["persona"] == "VFX lead"
    assert cfg["post_finder"]["keywords_tier1"]  # defaults still merged in


def test_reset_config(api_client):
    api_client.post("/api/profiles/demo/config", json={"display_name": "Custom"})
    resp = api_client.post("/api/profiles/demo/config/reset")
    assert resp.status_code == 200
    assert resp.get_json()["config"]["display_name"] == ""


# ─── Index route ──────────────────────────────────────────────────────────────

def test_index_serves_html(api_client):
    resp = api_client.get("/")
    assert resp.status_code == 200
    assert b"<!DOCTYPE html" in resp.data or b"<html" in resp.data.lower()
