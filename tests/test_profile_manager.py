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
