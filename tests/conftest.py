"""Shared pytest fixtures for the LinkedIn automation test suite (Phase 5).

All fixtures keep tests hermetic: storage is redirected into a temp dir, the
browser/network boundary is never crossed, and no OpenAI/LinkedIn calls happen.
"""

import socket

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import buffer_client  # noqa: E402
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
def _no_live_buffer_calls(tmp_path, monkeypatch):
    """Make an outbound Buffer request impossible for EVERY test — no exceptions.

    Same shape and same reasoning as ``_dummy_api_keys``: the suite claimed to be
    offline and was offline only by coincidence. Eight tests in
    ``test_scheduled_ui.py`` really did reach ``api.buffer.com``, through the
    queue endpoint's poll-on-render reconcile, and nothing failed — the only
    symptom was a line in ``api_usage.jsonl``. A guard that produces no failure
    is not a guard.

    This closes it at the TRANSPORT, which is the one place every Buffer call
    must pass through, so a future caller reaching the network fails loudly
    instead of quietly spending the 3,000-request/30-day budget and making the
    result depend on a real key, a network and a third party's uptime.

    It patches the HTTP transport rather than ``gql``, because ``gql`` itself is
    legitimately exercised: ``test_buffer_client.py`` drives it with a fake
    ``session`` to test status handling, retries and the usage log, and never
    reaches the network. ``gql`` resolves ``(session or requests).post``, so
    replacing the module's ``requests`` blocks exactly the calls that would go
    out and leaves the session-injected ones alone.

    A test that wants Buffer behaviour mocks the boundary it uses —
    ``posts_by_status``, ``get_channel``, ``scheduled_slots``, or an injected
    ``session`` — which is what the rest of the suite already does.
    """
    class _RefusesToLeaveTheMachine(object):
        RequestException = buffer_client.requests.RequestException

        @staticmethod
        def post(url, **kwargs):
            raise AssertionError(
                "a test tried to reach %s for real. The suite is hermetic: mock "
                "the boundary you need (posts_by_status / get_channel / "
                "scheduled_slots), or inject a fake session, rather than letting "
                "the request out." % url)

    monkeypatch.setattr(buffer_client, "requests", _RefusesToLeaveTheMachine)

    # And never write to the REAL api_usage.jsonl. `gql` logs BEFORE it sends,
    # so a refused call still appended a line to a tracked project file — which
    # is both pollution and a misleading record of spend that never happened.
    # Tests that assert on the log redirect it themselves; this is the default.
    monkeypatch.setattr(buffer_client, "USAGE_LOG",
                        str(tmp_path / "api_usage.jsonl"))

    buffer_client.clear_caches()


class RealNetworkAccess(BaseException):
    """A test tried to leave the machine. A ``BaseException`` on purpose: code
    under test that catches ``Exception`` (``youtube_api._call`` maps every
    transport failure to an ambiguous ``YouTubeApiError``) cannot swallow it."""


_LOOPBACK = ("127.0.0.1", "::1", "localhost", "")


def _host_of(address):
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None  # AF_UNIX path or similar: local by definition


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Any connect or name lookup off the loopback fails the test (D-027 Y1).

    The Buffer guard above closes one transport. This closes the socket layer
    itself, so a Google client, an OpenAI client or anything else that slips
    past a mock fails loudly instead of reaching a real service. Loopback stays
    open. ``test_network_guard.py`` proves it fires.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create = socket.create_connection

    def refuse(what):
        raise RealNetworkAccess("a test tried to reach %s for real. The suite is "
                                "hermetic: mock the boundary instead." % (what,))

    def connect(self, address):
        host = _host_of(address)
        if host is not None and host not in _LOOPBACK:
            refuse(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        host = _host_of(address)
        if host is not None and host not in _LOOPBACK:
            refuse(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and str(host) not in _LOOPBACK:
            refuse(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        if _host_of(address) not in _LOOPBACK:
            refuse(address)
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)


@pytest.fixture(autouse=True)
def _isolate_production_guard(monkeypatch):
    """Never let the developer's real .env decide a test's production-guard
    outcome — the same hermeticity problem as _dummy_api_keys, for a
    different pair of variables (see profile_manager.check_production_guard).
    """
    monkeypatch.delenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, raising=False)
    monkeypatch.delenv(pm.ALLOW_PRODUCTION_ENV, raising=False)


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
