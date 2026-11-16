"""Tests for linkedin_profile_manager: storage hardening, credential round-trips,
and .env auto-migration edge cases (ROADMAP Phase 0)."""

import json
import os
import sys

import pytest

from linkedin_automation import profile_manager as pm


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Redirect the module's storage globals into a temp dir so tests never
    touch the real data/profiles/profiles.json."""
    profiles_dir = tmp_path / "profiles"
    sessions_dir = profiles_dir / "chrome_sessions"
    profiles_dir.mkdir(parents=True)
    sessions_dir.mkdir()

    profiles_file = profiles_dir / "profiles.json"
    monkeypatch.setattr(pm, "PROFILES_DIR", str(profiles_dir))
    monkeypatch.setattr(pm, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(pm, "CHROME_SESSIONS_DIR", str(sessions_dir))
    return profiles_file


# ─── Credential round-trips ───────────────────────────────────────────────────

def test_add_and_get_roundtrip(isolated_store):
    pm.add_profile("work", "user@example.com", "plainpass")
    profile = pm.get_profile("work")
    assert profile is not None
    assert profile["username"] == "user@example.com"
    assert profile["password"] == "plainpass"


def test_special_char_password_roundtrip(isolated_store):
    """ROADMAP validation gate password must survive verbatim."""
    secret = "p@ss+w0rd&more!"
    pm.add_profile("special", "user@example.com", secret)
    assert pm.get_profile("special")["password"] == secret


@pytest.mark.parametrize("secret", [
    "p@ss+w0rd&more!",
    "has spaces and +&!@ symbols",
    "  leading-and-trailing  ",
    "semicolon;colon:bracket]quote\"",
    "emoji-🔥-unicode-é",
])
def test_passwords_preserved_exactly(isolated_store, secret):
    pm.add_profile("p", "user@example.com", secret)
    assert pm.get_profile("p")["password"] == secret


def test_cli_add_interactive_preserves_password(isolated_store, monkeypatch, capsys):
    """`add <name>` with no inline creds must prompt and store the password
    verbatim — no .strip(), no shell mangling."""
    secret = "  +Na!&MZB hard  "
    monkeypatch.setattr(sys, "argv", ["linkedin_profile_manager.py", "add", "myprofile"])
    monkeypatch.setattr("builtins.input", lambda prompt="": "user@example.com")
    monkeypatch.setattr(pm.getpass, "getpass", lambda prompt="": secret)

    pm.cli()

    stored = pm.get_profile("myprofile")
    assert stored is not None
    assert stored["password"] == secret


# ─── Default / remove / list ────────────────────────────────────────────────

def test_first_profile_becomes_default(isolated_store):
    pm.add_profile("solo", "a@b.com", "x")
    assert pm.get_default_profile_name() == "solo"


def test_set_default_profile(isolated_store):
    pm.add_profile("a", "a@b.com", "x")
    pm.add_profile("b", "b@b.com", "y")
    pm.set_default_profile("b")
    assert pm.get_default_profile_name() == "b"


def test_remove_profile_reassigns_default(isolated_store):
    pm.add_profile("a", "a@b.com", "x", set_default=True)
    pm.add_profile("b", "b@b.com", "y")
    pm.remove_profile("a")
    assert pm.get_profile("a") is None
    # Default was "a"; after removal a remaining profile is reassigned.
    assert pm.load_profiles()["default"] == "b"


def test_remove_last_profile_clears_default(isolated_store):
    pm.add_profile("only", "a@b.com", "x")
    pm.remove_profile("only")
    assert pm.load_profiles()["default"] is None


def test_list_profiles_returns_all(isolated_store):
    pm.add_profile("a", "a@b.com", "x")
    pm.add_profile("b", "b@b.com", "y")
    data = pm.list_profiles()
    assert set(data["profiles"].keys()) == {"a", "b"}


# ─── load_profiles hardening ──────────────────────────────────────────────────

def test_load_profiles_missing_file(isolated_store):
    assert pm.load_profiles() == {"profiles": {}, "default": None}


def test_load_profiles_empty_file(isolated_store):
    isolated_store.write_text("", encoding="utf-8")
    assert pm.load_profiles() == {"profiles": {}, "default": None}


def test_load_profiles_whitespace_only(isolated_store):
    isolated_store.write_text("   \n  ", encoding="utf-8")
    assert pm.load_profiles() == {"profiles": {}, "default": None}


def test_load_profiles_corrupt_json(isolated_store):
    isolated_store.write_text("{not valid json,,,", encoding="utf-8")
    # Must not raise.
    assert pm.load_profiles() == {"profiles": {}, "default": None}


def test_load_profiles_non_object_json(isolated_store):
    isolated_store.write_text("[1, 2, 3]", encoding="utf-8")
    assert pm.load_profiles() == {"profiles": {}, "default": None}


def test_load_profiles_drops_stale_default(isolated_store):
    isolated_store.write_text(json.dumps({
        "profiles": {"a": {"username": "a@b.com", "password": "x"}},
        "default": "ghost",  # points at a profile that no longer exists
    }), encoding="utf-8")
    loaded = pm.load_profiles()
    assert loaded["default"] is None
    assert "a" in loaded["profiles"]


# ─── save_profiles atomicity ──────────────────────────────────────────────────

def test_save_profiles_atomic_no_temp_left(isolated_store):
    pm.add_profile("a", "a@b.com", "x")
    # The .tmp file must have been renamed away, not left behind.
    assert not os.path.exists(str(isolated_store) + ".tmp")
    # And the result is valid JSON.
    json.loads(isolated_store.read_text(encoding="utf-8"))


# ─── auto_migrate_from_env edge cases ─────────────────────────────────────────

def test_auto_migrate_creates_default(isolated_store, monkeypatch):
    monkeypatch.setenv("LINKEDIN_USERNAME", "envuser@example.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "envp@ss+1!")
    pm.auto_migrate_from_env()
    profile = pm.get_profile("default")
    assert profile is not None
    assert profile["username"] == "envuser@example.com"
    assert profile["password"] == "envp@ss+1!"
    assert pm.get_default_profile_name() == "default"


def test_auto_migrate_skips_when_profiles_exist(isolated_store, monkeypatch):
    pm.add_profile("existing", "keep@example.com", "keep")
    monkeypatch.setenv("LINKEDIN_USERNAME", "envuser@example.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "envpass")
    pm.auto_migrate_from_env()
    # No "default" profile created; existing one untouched.
    assert pm.get_profile("default") is None
    assert pm.get_profile("existing")["username"] == "keep@example.com"


def test_auto_migrate_runs_when_profiles_json_corrupt(isolated_store, monkeypatch):
    isolated_store.write_text("{garbage", encoding="utf-8")
    monkeypatch.setenv("LINKEDIN_USERNAME", "envuser@example.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "envpass")
    pm.auto_migrate_from_env()
    assert pm.get_profile("default") is not None


def test_auto_migrate_no_env_does_nothing(isolated_store, monkeypatch):
    monkeypatch.delenv("LINKEDIN_USERNAME", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)
    monkeypatch.delenv("LINKEDIN_ALT_USERNAME", raising=False)
    monkeypatch.delenv("LINKEDIN_ALT_PASSWORD", raising=False)
    pm.auto_migrate_from_env()
    assert pm.load_profiles()["profiles"] == {}


# ─── B0: publishing-only profiles (D-011) ─────────────────────────────────────
# A profile with no LinkedIn login, for a brand that only publishes through
# Buffer. Every LinkedIn login or browser path refuses it before a browser
# exists.

REFUSAL = "Profile 'brand' is publishing-only: it has no LinkedIn login."


@pytest.fixture
def no_browser(monkeypatch):
    """Any attempt to build a Chrome driver fails the test."""
    def boom(*a, **k):
        pytest.fail("a browser was launched for a publishing-only profile")
    monkeypatch.setattr(pm.webdriver, "Chrome", boom)
    monkeypatch.setattr(pm, "ChromeDriverManager", boom)
    monkeypatch.setattr(pm, "Service", boom)


def _sessions(isolated_store):
    return sorted(os.listdir(os.path.join(os.path.dirname(isolated_store), "chrome_sessions")))


@pytest.mark.parametrize("username,password", [(None, None), ("", ""), (None, ""), ("", None)])
def test_both_blank_credentials_make_a_publishing_only_profile(isolated_store, username, password):
    pm.add_profile("brand", username, password)
    stored = json.loads(isolated_store.read_text(encoding="utf-8"))["profiles"]["brand"]
    assert stored["username"] is None and stored["password"] is None
    assert stored["session_dir"] is None
    assert _sessions(isolated_store) == []
    assert pm.is_publishing_only(stored)


def test_publishing_only_is_the_keyword_default(isolated_store):
    pm.add_profile("brand")
    assert pm.is_publishing_only(pm.get_profile("brand"))


@pytest.mark.parametrize("username,password,missing", [
    ("u@example.com", None, "password"),
    ("u@example.com", "", "password"),
    (None, "secret", "username"),
    ("", "secret", "username"),
])
def test_exactly_one_blank_credential_is_refused_naming_it(isolated_store, username, password, missing):
    with pytest.raises(ValueError, match=f"LinkedIn {missing} is missing"):
        pm.add_profile("brand", username, password)
    assert pm.get_profile("brand") is None
    assert _sessions(isolated_store) == []


def test_a_space_is_a_credential_not_a_blank(isolated_store):
    pm.add_profile("work", "u@example.com", " ")
    assert pm.get_profile("work")["password"] == " "
    assert not pm.is_publishing_only(pm.get_profile("work"))


def test_a_linkedin_profile_still_gets_a_session_dir(isolated_store):
    pm.add_profile("work", "u@example.com", "pw", True)
    assert _sessions(isolated_store) == ["work"]
    assert pm.load_profiles()["default"] == "work"


def test_a_publishing_only_first_profile_is_not_the_default(isolated_store):
    pm.add_profile("brand")
    assert pm.load_profiles()["default"] is None
    assert pm.get_default_profile_name() is None


def test_the_first_linkedin_profile_after_a_publishing_only_one_is_the_default(isolated_store):
    pm.add_profile("brand")
    pm.add_profile("work", "u@example.com", "pw")
    assert pm.load_profiles()["default"] == "work"
    assert pm.get_default_profile_name() == "work"


def test_a_second_linkedin_profile_does_not_take_the_default(isolated_store):
    pm.add_profile("work", "u@example.com", "pw")
    pm.add_profile("brand")
    pm.add_profile("other", "o@example.com", "pw")
    assert pm.load_profiles()["default"] == "work"


def test_set_default_is_honoured_for_a_publishing_only_profile(isolated_store):
    pm.add_profile("work", "u@example.com", "pw")
    pm.add_profile("brand", set_default=True)
    assert pm.load_profiles()["default"] == "brand"


def test_removing_the_default_never_hands_it_to_a_publishing_only_profile(isolated_store):
    pm.add_profile("work", "u@example.com", "pw")
    pm.add_profile("brand")
    pm.remove_profile("work")
    assert pm.load_profiles()["default"] is None
    assert pm.get_default_profile_name() is None


def test_adding_a_publishing_only_profile_leaves_existing_records_unchanged(isolated_store):
    pm.add_profile("work", "u@example.com", "p@ss+1!")
    before = json.loads(isolated_store.read_text(encoding="utf-8"))
    pm.add_profile("brand")
    after = json.loads(isolated_store.read_text(encoding="utf-8"))
    assert after["profiles"]["work"] == before["profiles"]["work"]
    assert after["default"] == before["default"]


def test_create_driver_refuses_a_publishing_only_profile_with_no_browser(isolated_store, no_browser):
    pm.add_profile("brand")
    with pytest.raises(pm.PublishingOnlyProfileError) as err:
        pm.create_driver("brand")
    assert str(err.value) == REFUSAL
    assert _sessions(isolated_store) == []


def test_setup_and_login_refuses_a_publishing_only_profile_with_no_browser(isolated_store, no_browser):
    pm.add_profile("brand")
    with pytest.raises(pm.PublishingOnlyProfileError) as err:
        pm.setup_and_login("brand")
    assert str(err.value) == REFUSAL


def test_login_refuses_a_publishing_only_profile_without_touching_the_driver(isolated_store):
    pm.add_profile("brand")

    class Untouchable:
        def __getattr__(self, name):
            pytest.fail(f"login touched the driver ({name}) for a publishing-only profile")

    with pytest.raises(pm.PublishingOnlyProfileError) as err:
        pm.login(Untouchable(), pm.get_profile("brand"))
    assert str(err.value) == REFUSAL


def test_cli_list_shows_publishing_only(isolated_store, monkeypatch, capsys):
    pm.add_profile("brand")
    monkeypatch.setattr(sys, "argv", ["pm", "list"])
    pm.cli()
    out = capsys.readouterr().out
    assert "Username:  (publishing-only)" in out
    assert "Session:   N/A" in out


def test_cli_remove_names_publishing_only(isolated_store, monkeypatch):
    pm.add_profile("brand")
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "y")
    monkeypatch.setattr(sys, "argv", ["pm", "remove", "brand"])
    pm.cli()
    assert prompts == ["Remove profile 'brand' ((publishing-only))? [y/N]: "]
    assert pm.get_profile("brand") is None


def test_cli_add_publishing_only(isolated_store, monkeypatch):
    monkeypatch.setattr(pm.getpass, "getpass",
                        lambda prompt="": pytest.fail("no password prompt for publishing-only"))
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": pytest.fail("no username prompt for publishing-only"))
    monkeypatch.setattr(sys, "argv", ["pm", "add", "brand", "--publishing-only"])
    pm.cli()
    assert pm.is_publishing_only(pm.get_profile("brand"))
    assert _sessions(isolated_store) == []


def test_cli_add_publishing_only_with_a_credential_is_refused(isolated_store, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pm", "add", "brand", "--publishing-only", "--username", "u"])
    pm.cli()
    assert pm.get_profile("brand") is None
    assert "--publishing-only takes no --username or --password" in capsys.readouterr().out


def test_cli_test_refuses_a_publishing_only_profile(isolated_store, no_browser, monkeypatch, capsys):
    pm.add_profile("brand")
    monkeypatch.setattr(sys, "argv", ["pm", "test", "brand"])
    pm.cli()
    assert REFUSAL in capsys.readouterr().out


# ─── B0: the dashboard route ─────────────────────────────────────────────────

def test_dashboard_creates_a_publishing_only_profile(api_client, monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    resp = api_client.post("/api/profiles", json={
        "name": "brand", "username": "", "password": ""})
    assert resp.status_code == 200
    assert "Publishing-only" in resp.get_json()["message"]
    listing = api_client.get("/api/profiles").get_json()
    assert listing["profiles"]["brand"]["username"] is None
    assert listing["profiles"]["brand"]["password"] is None
    assert listing["default"] is None


@pytest.mark.parametrize("body,missing", [
    ({"username": "u@example.com", "password": ""}, "password"),
    ({"username": "", "password": "pw"}, "username"),
    ({"username": "u@example.com"}, "password"),
    ({"password": "pw"}, "username"),
])
def test_dashboard_refuses_one_blank_credential(api_client, body, missing):
    resp = api_client.post("/api/profiles", json=dict(body, name="brand"))
    assert resp.status_code == 400
    assert f"LinkedIn {missing} is missing" in resp.get_json()["error"]
    assert pm.get_profile("brand") is None


def test_dashboard_body_without_credential_fields_is_refused(api_client):
    resp = api_client.post("/api/profiles", json={"name": "brand"})
    assert resp.status_code == 400
    assert pm.get_profile("brand") is None


def test_dashboard_both_set_still_makes_a_linkedin_profile(api_client):
    resp = api_client.post("/api/profiles", json={
        "name": "work", "username": "u@example.com", "password": "pw"})
    assert resp.status_code == 200
    assert not pm.is_publishing_only(pm.get_profile("work"))


# ─── B0: scheduled posting on a publishing-only profile ──────────────────────

def test_a_publishing_only_profile_queues_and_schedules_a_pin(isolated_store, tmp_path,
                                                              monkeypatch, no_browser):
    """Overlay on the profile -> resolve_scheduled -> queue_rows ->
    schedule_pass, with Buffer stubbed. One createPost; the pin is SCHEDULED."""
    import test_pinterest as tp
    from linkedin_automation import csv_pipeline as cp

    monkeypatch.setattr(pm, "DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, raising=False)
    pm.add_profile("brand")
    pm.save_profile_config("brand", {"scheduled_posting": {"platforms": {"pinterest": {
        "buffer_channel_id": tp.CHAN, "identity_slug": tp.HANDLE,
        "buffer_api_key_env": "BUFFER_API_KEY_BRAND", "drain": False}}}})

    resolved = pm.resolve_scheduled("brand", "pinterest",
                                    env={"BUFFER_API_KEY_BRAND": "brand-key"})
    assert (resolved["channel_id"], resolved["identity_slug"], resolved["api_key"]) == (
        tp.CHAN, tp.HANDLE, "brand-key")

    state = cp.PipelineState(path=os.path.join(pm.get_data_dir("brand"), "pinterest.json"),
                             platform="pinterest")
    accepted, rejected, _skipped, _warnings = cp.queue_rows(
        [tp.pin_row()], state, now=tp.NOW, boards=tp.BOARDS)
    assert (len(accepted), rejected) == (1, [])

    buffer = tp.Buffer()
    results = cp.schedule_pass(
        cp.pending_rows(state), resolved["channel_id"], state, key=resolved["api_key"],
        session=buffer, upload=tp.Uploads(), now=tp.NOW, platform="pinterest",
        identity_slug=resolved["identity_slug"], profile_name="brand",
        fetch_channel=tp._channel(),
        generate=lambda *a, **k: pytest.fail("nothing generates pin text"))
    assert len(buffer.inputs) == 1
    assert buffer.inputs[0]["channelId"] == tp.CHAN
    assert [r["status"] for r in results] == [cp.SCHEDULED]
    assert pm.get_profile("brand")["session_dir"] is None
