"""Shared pytest fixtures for the LinkedIn automation test suite (Phase 5).

All fixtures keep tests hermetic: storage is redirected into a temp dir, the
browser/network boundary is never crossed, and no OpenAI/LinkedIn calls happen.
"""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as linkedin_dashboard


# Canonical activity-URL template used across tests.
ACTIVITY_URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"

# The only API key any test may see. Obviously fake, and long enough that code
# which merely checks "is a key present" behaves as it would in production.
#
# Deliberately carries NO vendor prefix (no sk-/xai-/gsk_). test_suite_hermetic
# scans the environment for anything shaped like a real credential, and a dummy
# that matched that shape would mask every real key it exists to catch.
DUMMY_API_KEY = "test-dummy-not-a-real-key-0000000000"

# Environment variables that can hold a funded vendor credential.
API_KEY_ENV_VARS = ("OPENAI_API_KEY",)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    """Pin every API key to a dummy for EVERY test — no exceptions.

    Two problems this closes, both real in this repo and neither caught by any
    existing test (adopted from the fork's hermeticity guard, see
    .dev/AUDIT_fork_remainder.md §4):

    **A funded key was visible to every test.** ``load_dotenv()`` runs at import
    time in several modules, so merely importing the package loaded the
    developer's real ``.env`` into ``os.environ``. Nothing spent it, because the
    OpenAI client is mocked everywhere — but that is protection by coincidence.
    One missed mock reaches a real vendor and bills a real account.

    **The suite depended on ambient environment.** Code that resolves a key
    raises when none is set, so a result could depend on whether the developer
    happened to have one exported. A suite whose outcome varies with the machine
    is not a suite you can act on.

    Setting rather than deleting: code paths that check for a key's *presence*
    must still take the same branch they take in production.
    """
    for var in API_KEY_ENV_VARS:
        monkeypatch.setenv(var, DUMMY_API_KEY)


@pytest.fixture(autouse=True)
def _isolate_data_root(tmp_path, monkeypatch):
    """Point DATA_ROOT at a temp dir for EVERY test — no exceptions.

    Safety net, not a convenience. The pipeline endpoints all read the lifecycle
    store now, so any test whose fixture forgets to redirect ``get_data_dir``
    would otherwise read *and rewrite* the developer's real
    ``data/<profile>/posts_db.json``. This makes the default outcome a temp dir
    instead of live user data; per-test fixtures that patch the pm path helpers
    still take precedence.
    """
    monkeypatch.setattr(pm, "DATA_ROOT", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _isolate_run_logs(tmp_path, monkeypatch):
    """Keep the poster's per-run file log out of the real ``logs/`` directory.

    ``LinkedInCommentPoster.run`` opens ``logs/run_<profile>_<ts>.log`` under
    PROJECT_ROOT (Dispatch 15.1). Any test that drives ``run`` would otherwise
    litter the working tree with log files.
    """
    from linkedin_automation import run_log
    monkeypatch.setattr(run_log, "logs_dir", lambda: str(tmp_path / "logs"))


@pytest.fixture
def make_comment():
    """Factory for generator-style comment dicts (post_* keys)."""
    def _make(i, **overrides):
        comment = {
            "post_url": ACTIVITY_URL.format(i),
            "post_author": f"Author {i}",
            "post_text": f"Interesting post body number {i}.",
            "post_category": "AI",
            "comment": f"Great point {i}, thanks for sharing.",
            "word_count": 5,
            "style": "thoughtful",
        }
        comment.update(overrides)
        return comment
    return _make


@pytest.fixture
def profiles_store(tmp_path, monkeypatch):
    """Redirect the profile manager's storage globals into a temp dir."""
    profiles_dir = tmp_path / "profiles"
    sessions_dir = profiles_dir / "chrome_sessions"
    sessions_dir.mkdir(parents=True)
    profiles_file = profiles_dir / "profiles.json"
    monkeypatch.setattr(pm, "PROFILES_DIR", str(profiles_dir))
    monkeypatch.setattr(pm, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(pm, "CHROME_SESSIONS_DIR", str(sessions_dir))
    return profiles_file


@pytest.fixture
def comments_dir(tmp_path, monkeypatch):
    """Redirect the profile-specific comments/progress/screenshots dirs to tmp."""
    cdir = tmp_path / "quality_comments"
    shots = cdir / "debug_screenshots"
    shots.mkdir(parents=True)
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(cdir))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda profile_name=None: str(shots))
    monkeypatch.setattr(
        pm, "get_progress_file",
        lambda profile_name=None: str(cdir / "posting_progress.json"),
    )
    # Redirect the profile data root too (used by get_config_path) so config
    # endpoints write to tmp, never the real data/<profile>/profile_config.json.
    #
    # ``subdir`` MUST be honoured. The real ``get_data_dir`` uses it to separate
    # per-platform storage (``data/<profile>/x/posts_db.json`` vs LinkedIn's
    # ``data/<profile>/posts_db.json``). A stub that accepted ``subdir`` and
    # ignored it — as this one did — collapsed both platforms onto ONE path under
    # every fixture that uses it, including ``api_client``. Nothing failed at the
    # time, because nothing requested a platform yet; the danger was later, when a
    # platform-aware endpoint test would have asserted isolation while both
    # platforms shared a file, and passed. A green test proving nothing is worse
    # than no test. Guarded by test_store_platform_isolation.py's fixture test.
    def _fake_get_data_dir(profile_name=None, subdir=None):
        path = tmp_path / subdir if subdir else tmp_path
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    monkeypatch.setattr(pm, "get_data_dir", _fake_get_data_dir)
    return cdir


@pytest.fixture
def api_client(comments_dir, profiles_store):
    """Flask test client with storage redirected to tmp (no browser/network)."""
    linkedin_dashboard.app.config.update(TESTING=True)
    return linkedin_dashboard.app.test_client()
