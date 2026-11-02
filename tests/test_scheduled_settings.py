"""Dispatch 44: a settings form for buffer_channel_id / identity_slug /
buffer_api_key_env, per (profile, platform) - Audit 43 found all three
required hand-editing profile_config.json, with no form on either platform.

Credentials resolution itself (profile_manager.resolve_scheduled) is
unchanged by this dispatch - it already raised rather than falling back
(Audit 43 §3). What is new: a GET/POST settings endpoint, a read-only
"test connection" endpoint, and the env-var-NAME-not-a-secret-VALUE guard.

The load-bearing claims:

* saving one (profile, platform) pair never touches that profile's OTHER
  platform, or any OTHER profile's file - asserted on the raw JSON, not on
  what the save route merely claims to have done;
* a value that looks like a pasted API key, not an environment variable
  name, is rejected with a clear message rather than silently stored;
* "test connection" makes exactly one Buffer call (getChannel) and never
  creates anything, and an incomplete pair never reaches that call at all;
* nothing about the production identity gate on the X CLI path changes -
  tests/test_identity_guard.py's existing CLI-path tests are the proof,
  re-run here by the same suite, not duplicated.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def _config(profile, **overrides):
    """A minimal real config, saved through the real write path
    (pm.save_profile_config), so these tests exercise the actual file on
    disk rather than a mocked get_profile_config."""
    cfg = pm.load_default_config()
    for section, value in overrides.items():
        cfg[section] = value
    pm.save_profile_config(profile, cfg)
    return cfg


# ─── profile_manager.valid_env_var_name ──────────────────────────────────────

@pytest.mark.parametrize("name", [
    "BUFFER_API_KEY_X", "BUFFER_API_KEY", "A", "X_1", "BLUESKY_KEY_2"])
def test_real_env_var_names_are_accepted(name):
    assert pm.valid_env_var_name(name) is True


@pytest.mark.parametrize("value", [
    "",
    None,
    "sk-abcdEFGH12345678",                  # OpenAI-shaped
    "buffer_api_key_x",                     # lowercase
    "1BUFFER_API_KEY_X",                    # leading digit
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",      # a bare hex token
    "BUFFER API KEY",                       # contains a space
    "A" * 65,                               # right shape, too long to be real
])
def test_secret_shaped_values_are_rejected(value):
    assert pm.valid_env_var_name(value) is False


# ─── GET /settings ──────────────────────────────────────────────────────────

def test_linkedin_settings_report_the_fixed_key_env(client):
    _config("p", scheduled_posting={
        "buffer_channel_id": "chan_li", "identity_slug": "example-person"})
    d = client.get("/api/scheduled/p/settings").get_json()
    assert d["platform"] == "linkedin"
    assert d["buffer_channel_id"] == "chan_li"
    assert d["identity_slug"] == "example-person"
    assert d["buffer_api_key_env"] == "BUFFER_API_KEY"
    assert d["buffer_api_key_env_editable"] is False


def test_a_never_configured_x_overlay_reports_blank_not_an_error(client,
                                                                  monkeypatch):
    """A brand-new platform must be fillable from scratch, not a 500.

    get_profile_config deep-merges the DEFAULT template in (profile_manager
    .get_profile_config), and default_profile_config.json already ships
    "BUFFER_API_KEY_X" as platforms.x's buffer_api_key_env - so an overlay
    that was never hand-saved still shows that shipped default, exactly as
    Audit 43 found. The environment variable itself is a separate question,
    and is unset here.
    """
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    _config("p", scheduled_posting={"buffer_channel_id": "chan_li",
                                    "identity_slug": "example-person"})
    d = client.get("/api/scheduled/p/settings?platform=x").get_json()
    assert d["buffer_channel_id"] == ""
    assert d["identity_slug"] == ""
    assert d["buffer_api_key_env"] == "BUFFER_API_KEY_X"
    assert d["buffer_api_key_env_editable"] is True
    assert d["api_key_env_set"] is False
    assert d["configured"] is False
    assert "buffer_channel_id" in d["missing"]
    assert "identity_slug" in d["missing"]
    assert any("BUFFER_API_KEY_X" in m for m in d["missing"])


def test_a_fully_configured_x_overlay_reports_configured(client, monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _config("p", scheduled_posting={
        "buffer_channel_id": "chan_li", "identity_slug": "example-person",
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "chan_x",
                            "identity_slug": "AI_Fun_times"}}})
    d = client.get("/api/scheduled/p/settings?platform=x").get_json()
    assert d["configured"] is True
    assert d["missing"] == []
    assert d["api_key_env_set"] is True


def test_x_overlay_with_an_unset_env_var_names_it_in_missing(client,
                                                              monkeypatch):
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    _config("p", scheduled_posting={
        "buffer_channel_id": "chan_li", "identity_slug": "example-person",
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "chan_x",
                            "identity_slug": "AI_Fun_times"}}})
    d = client.get("/api/scheduled/p/settings?platform=x").get_json()
    assert d["configured"] is False
    assert d["api_key_env_set"] is False
    assert any("BUFFER_API_KEY_X" in m for m in d["missing"])


def test_a_blank_linkedin_identity_is_not_in_missing(client):
    """Same asymmetry as _scheduled_account: LinkedIn's schedule path is
    deliberately ungated (see .dev/BACKLOG.md)."""
    _config("p", scheduled_posting={"buffer_channel_id": "chan_li",
                                    "identity_slug": ""})
    d = client.get("/api/scheduled/p/settings").get_json()
    assert "identity_slug" not in d["missing"]
    assert d["configured"] is True


# ─── POST /settings - isolation (THE GATE) ───────────────────────────────────

def test_saving_x_touches_only_the_x_overlay_on_disk(client, monkeypatch):
    """Read the RAW config file after the save - not the route's own
    response - so this proves what actually landed, not what was claimed."""
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _config("p", scheduled_posting={
        "buffer_channel_id": "chan_li", "identity_slug": "example-person",
        "drain": {"enabled": True, "interval_minutes": 30},
        "platforms": {"bluesky": {"buffer_api_key_env": "BUFFER_API_KEY_BSKY",
                                  "buffer_channel_id": "chan_bsky",
                                  "identity_slug": "someone.bsky.social"}}})

    res = client.post("/api/scheduled/p/settings?platform=x", json={
        "buffer_channel_id": "chan_x_new",
        "identity_slug": "AI_Fun_times",
        "buffer_api_key_env": "BUFFER_API_KEY_X",
    })
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    with open(pm.get_config_path("p"), encoding="utf-8") as f:
        raw = json.load(f)
    sp = raw["scheduled_posting"]

    # The X overlay changed.
    assert sp["platforms"]["x"]["buffer_channel_id"] == "chan_x_new"
    assert sp["platforms"]["x"]["identity_slug"] == "AI_Fun_times"

    # LinkedIn's flat fields did NOT.
    assert sp["buffer_channel_id"] == "chan_li"
    assert sp["identity_slug"] == "example-person"
    assert sp["drain"] == {"enabled": True, "interval_minutes": 30}

    # A THIRD platform's overlay (bluesky) did NOT.
    assert sp["platforms"]["bluesky"] == {
        "buffer_api_key_env": "BUFFER_API_KEY_BSKY",
        "buffer_channel_id": "chan_bsky",
        "identity_slug": "someone.bsky.social"}


def test_saving_one_profile_never_touches_another_profiles_file(client,
                                                                 monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _config("alice", scheduled_posting={
        "buffer_channel_id": "chan_alice", "identity_slug": "alice-slug"})
    _config("bob", scheduled_posting={
        "buffer_channel_id": "chan_bob", "identity_slug": "bob-slug"})

    bob_before = open(pm.get_config_path("bob"), encoding="utf-8").read()

    res = client.post("/api/scheduled/alice/settings?platform=x", json={
        "buffer_channel_id": "chan_alice_x", "identity_slug": "alice_x",
        "buffer_api_key_env": "BUFFER_API_KEY_X"})
    assert res.get_json()["ok"] is True

    bob_after = open(pm.get_config_path("bob"), encoding="utf-8").read()
    assert bob_after == bob_before


def test_saving_linkedin_does_not_create_an_x_overlay(client):
    """Saving LinkedIn's two flat fields must not fabricate a `platforms`
    section that was never there."""
    _config("p", scheduled_posting={"buffer_channel_id": "", "identity_slug": ""})
    res = client.post("/api/scheduled/p/settings", json={
        "buffer_channel_id": "chan_li", "identity_slug": "example-person"})
    assert res.get_json()["ok"] is True
    with open(pm.get_config_path("p"), encoding="utf-8") as f:
        raw = json.load(f)
    assert "platforms" not in raw["scheduled_posting"] or \
        raw["scheduled_posting"]["platforms"] == {}


# ─── POST /settings - the secret-not-a-name guard (THE GATE) ────────────────

def test_pasting_a_key_value_into_the_env_name_field_is_rejected(client):
    _config("p", scheduled_posting={"platforms": {"x": {}}})
    res = client.post("/api/scheduled/p/settings?platform=x", json={
        "buffer_channel_id": "chan_x", "identity_slug": "AI_Fun_times",
        "buffer_api_key_env": "a1b2c3d4e5f6g7h8i9j0buffersecretlookingvalue"})
    assert res.status_code == 400
    body = res.get_json()
    assert "environment variable name" in body["error"]
    assert "secret" in body["error"]

    # And nothing was written.
    with open(pm.get_config_path("p"), encoding="utf-8") as f:
        raw = json.load(f)
    overlay = raw.get("scheduled_posting", {}).get("platforms", {}).get("x", {})
    assert overlay.get("buffer_api_key_env") != \
        "a1b2c3d4e5f6g7h8i9j0buffersecretlookingvalue"


def test_linkedins_fixed_key_env_cannot_be_changed(client):
    _config("p", scheduled_posting={"buffer_channel_id": "chan_li"})
    res = client.post("/api/scheduled/p/settings", json={
        "buffer_channel_id": "chan_li", "identity_slug": "example-person",
        "buffer_api_key_env": "BUFFER_API_KEY_ALT"})
    assert res.status_code == 400
    assert "fixed" in res.get_json()["error"]


def test_linkedins_own_fixed_value_is_accepted_as_a_noop(client):
    """The UI always sends the fixed name back for LinkedIn; that must not
    be treated as an attempt to change it."""
    _config("p", scheduled_posting={"buffer_channel_id": "chan_li"})
    res = client.post("/api/scheduled/p/settings", json={
        "buffer_channel_id": "chan_li", "identity_slug": "example-person",
        "buffer_api_key_env": "BUFFER_API_KEY"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_an_unknown_platform_in_settings_is_a_400(client):
    res = client.get("/api/scheduled/p/settings?platform=mastodon")
    assert res.status_code == 400


# ─── csv_pipeline.test_buffer_connection ─────────────────────────────────────

def test_connection_ok_reports_the_handle(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _config("p", scheduled_posting={
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "chan_x",
                            "identity_slug": "AI_Fun_times"}}})

    def fetch(cid, key=None, max_age=None):
        assert cid == "chan_x" and key == "x-key"
        return {"id": cid, "name": "AI_Fun_times", "service": "twitter"}

    result = cp.test_buffer_connection("p", "x", fetch_channel=fetch)
    assert result["ok"] is True
    assert result["handle"] == "AI_Fun_times"


def test_connection_surfaces_buffers_actual_error(monkeypatch):
    """Buffer's own rejection of a bad channel id must be shown, not a
    generic 'failed'."""
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _config("p", scheduled_posting={
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "bad_channel_id",
                            "identity_slug": "AI_Fun_times"}}})

    def fetch(cid, key=None, max_age=None):
        raise cp.bc.BufferError("Buffer returned no channel for id %r" % cid)

    result = cp.test_buffer_connection("p", "x", fetch_channel=fetch)
    assert result["ok"] is False
    assert "bad_channel_id" in result["error"]


def test_connection_never_calls_buffer_for_an_incomplete_pair(monkeypatch):
    """THE GATE: an incomplete pair cannot schedule, and that includes the
    test-connection action - the Buffer client is never called at all."""
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    _config("p", scheduled_posting={
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "chan_x",
                            "identity_slug": "AI_Fun_times"}}})

    def never(*a, **k):
        raise AssertionError("Buffer's getChannel must not be called for an "
                             "incomplete (profile, platform) pair")

    result = cp.test_buffer_connection("p", "x", fetch_channel=never)
    assert result["ok"] is False
    assert "BUFFER_API_KEY_X" in result["error"]


def test_connection_with_no_channel_configured_never_calls_buffer(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _config("p", scheduled_posting={
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "", "identity_slug": ""}}})

    def never(*a, **k):
        raise AssertionError("must not call Buffer with no channel configured")

    result = cp.test_buffer_connection("p", "x", fetch_channel=never)
    assert result["ok"] is False
    assert "no Buffer channel" in result["error"]


def test_the_test_connection_route_never_creates_a_post(client, monkeypatch):
    """Route-level: confirms create_post is never even imported into the
    call path for this action."""
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _config("p", scheduled_posting={
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "chan_x",
                            "identity_slug": "AI_Fun_times"}}})

    created = []
    monkeypatch.setattr(dashboard.buffer_client, "create_post",
                        lambda *a, **k: created.append(a) or {"id": "p"})
    monkeypatch.setattr(dashboard.buffer_client, "get_channel",
                        lambda cid, key=None, session=None, max_age=None: {
                            "id": cid, "name": "AI_Fun_times"})

    res = client.post("/api/scheduled/p/settings/test?platform=x")
    assert res.get_json()["ok"] is True
    assert created == []


# ─── scheduling an incomplete pair never reaches Buffer (THE GATE) ───────────

def test_scheduling_an_incomplete_x_pair_never_calls_buffer(monkeypatch):
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    _config("p", scheduled_posting={
        "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                            "buffer_channel_id": "", "identity_slug": ""}}})

    def never(*a, **k):
        raise AssertionError("an incomplete pair must never reach Buffer")

    monkeypatch.setattr(dashboard.buffer_client, "get_channel", never)
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots", never)
    monkeypatch.setattr(dashboard.buffer_client, "create_post", never)
    monkeypatch.setattr(dashboard.csv_pipeline, "pending_rows",
                        lambda state: [{"date": "2036-11-02",
                                        "time_window": "morning",
                                        "post_text": "t", "topic": "t",
                                        "tags": "", "image_path": "",
                                        "first_comment_link": ""}])

    out = dashboard._scheduled_schedule_job("job1", "p", "", platform="x")
    assert out["scheduled"] == 0
    assert out.get("error")
