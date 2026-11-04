"""buffer_api_key_env picks which environment variable is sent to Buffer as the
Bearer token, so only BUFFER_API_KEY or BUFFER_API_KEY_<SUFFIX> may be read.

The hand-edited gate is asserted at the HTTP transport, on the Authorization
header of what would actually go out, across every dashboard path that can call
Buffer. A response saying "refused" would not show that nothing was sent.
"""

import importlib.util
import json
import os
import shutil
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402

from test_schedule_config_banner import _paint  # noqa: E402


REAL_PATH = os.environ["PATH"]
LEAK = "value-that-must-never-leave-this-machine"
BARE_KEY = "bare-buffer-key"
X_KEY = "x-buffer-key"

OUTSIDE_NAMES = [
    "PATH",
    "OPENAI_API_KEY",
    "MY_ARBITRARY_TOKEN",
]
ALLOWED = [("BUFFER_API_KEY", BARE_KEY), ("BUFFER_API_KEY_X", X_KEY)]


@pytest.fixture
def client():
    dashboard._reconcile_cache.clear()
    dashboard.app.config["TESTING"] = True
    yield dashboard.app.test_client()
    dashboard._reconcile_cache.clear()


@pytest.fixture
def sent(monkeypatch):
    """Every Authorization header that reached the transport. The request then
    fails, as a Buffer outage would, because only the header matters here."""
    headers = []

    class Transport:
        RequestException = requests.RequestException

        @staticmethod
        def post(url, **kwargs):
            headers.append(kwargs["headers"]["Authorization"])
            raise requests.ConnectionError("recorded, not sent")

    monkeypatch.setattr(dashboard.buffer_client, "requests", Transport)
    return headers


@pytest.fixture
def jobs(monkeypatch):
    """The schedule route's job, run inline by the test instead of on a thread."""
    captured = []
    monkeypatch.setattr(dashboard, "run_job",
                        lambda job_id, func, *a, **kw: captured.append((job_id, func, a)))
    return captured


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", BARE_KEY)
    monkeypatch.setenv("BUFFER_API_KEY_X", X_KEY)
    for name in OUTSIDE_NAMES:
        monkeypatch.setenv(name, LEAK)


def _save(key_env):
    cfg = pm.load_default_config()
    cfg["scheduled_posting"] = {"platforms": {"x": {
        "buffer_api_key_env": key_env,
        "buffer_channel_id": "chan_x",
        "identity_slug": "example-x"}}}
    pm.save_profile_config("p", cfg)


def _seed_rows():
    """One row awaiting publication and one pending. Without them the reconcile
    and the schedule pass exit before asking Buffer anything, whatever the key."""
    row = {"date": "2036-11-02", "time_window": "morning",
           "post_text": "A short post.", "topic": "t"}
    state = cp.PipelineState(profile_name="p", platform="x")
    state.rows["waiting"] = {"status": cp.SCHEDULED, "post_id": "p1", "text": "Waiting."}
    state.rows[cp.row_key(row)] = {"status": cp.PENDING, "row": row}
    state.save()


def _drive_every_buffer_path(client, sent, jobs):
    """Each dashboard action that can call Buffer. Returns {action: headers}."""
    _seed_rows()
    seen = {}
    bodies = []

    def step(name, call):
        del sent[:]
        bodies.append(call())
        seen[name] = list(sent)

    step("queue", lambda: client.get("/api/scheduled/p/queue?platform=x"))
    step("reconcile", lambda: client.post("/api/scheduled/p/reconcile?platform=x"))
    step("preflight", lambda: client.get("/api/scheduled/p/schedule/preflight?platform=x"))
    step("test connection", lambda: client.post("/api/scheduled/p/settings/test?platform=x"))
    step("settings", lambda: client.get("/api/scheduled/p/settings?platform=x"))

    def schedule():
        res = client.post("/api/scheduled/p/schedule?platform=x")
        for job_id, func, args in jobs:
            func(job_id, *args)
        return res

    step("schedule", schedule)
    return seen, bodies


# ─── the rule ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "BUFFER_API_KEY", "BUFFER_API_KEY_X", "BUFFER_API_KEY_BSKY", "BUFFER_API_KEY_ALT_2"])
def test_names_inside_the_rule_are_read(name):
    assert pm.scheduled_key_env(name) == name
    assert pm.buffer_key_env_rejected(name) is False


@pytest.mark.parametrize("name", OUTSIDE_NAMES + [
    "path",                         # Windows would read PATH for this
    "buffer_api_key_x",
    "XBUFFER_API_KEY",
    "BUFFER_API_KEYS",
    "BUFFER_API_KEY_",
    "BUFFER_API_KEY_x",
    "BUFFER_API_KEY-X",
    "BUFFER_API_KEY=PATH",
    "BUFFER_API_KEY\nPATH",
    "BUFFER_API_KEY_" + "A" * 60,   # past the name length limit
])
def test_names_outside_the_rule_are_blank_and_rejected(name):
    assert pm.scheduled_key_env(name) == ""
    assert pm.buffer_key_env_rejected(name) is True


@pytest.mark.parametrize("value", ["", "   ", None, 12345, ["PATH"], {"name": "PATH"}])
def test_blank_and_non_string_values_read_as_unset_not_rejected(value):
    assert pm.scheduled_key_env(value) == ""
    assert pm.buffer_key_env_rejected(value) is False


# ─── save time ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", OUTSIDE_NAMES)
def test_the_settings_form_refuses_a_name_outside_the_rule(client, name):
    _save("BUFFER_API_KEY_X")
    before = open(pm.get_config_path("p"), encoding="utf-8").read()
    res = client.post("/api/scheduled/p/settings?platform=x", json={
        "buffer_channel_id": "chan_x", "identity_slug": "example-x",
        "buffer_api_key_env": name})
    assert res.status_code == 400
    error = res.get_json()["error"]
    assert name in error
    assert "sent to Buffer" in error
    assert pm.BUFFER_KEY_ENV_RULE in error
    assert open(pm.get_config_path("p"), encoding="utf-8").read() == before


@pytest.mark.parametrize("name", OUTSIDE_NAMES)
def test_the_general_config_editor_refuses_it_too(client, name):
    _save("BUFFER_API_KEY_X")
    before = open(pm.get_config_path("p"), encoding="utf-8").read()
    res = client.post("/api/profiles/p/config", json={"scheduled_posting": {
        "platforms": {"x": {"buffer_api_key_env": name}}}})
    assert res.status_code == 400
    assert pm.BUFFER_KEY_ENV_RULE in res.get_json()["error"]
    assert open(pm.get_config_path("p"), encoding="utf-8").read() == before


@pytest.mark.parametrize("name,_value", ALLOWED)
def test_both_save_paths_accept_a_name_inside_the_rule(client, name, _value):
    _save("")
    res = client.post("/api/scheduled/p/settings?platform=x", json={
        "buffer_channel_id": "chan_x", "identity_slug": "example-x",
        "buffer_api_key_env": name})
    assert res.status_code == 200
    assert res.get_json()["settings"]["buffer_api_key_env"] == name
    assert res.get_json()["settings"]["missing"] == []

    _save("")
    res = client.post("/api/profiles/p/config", json={"scheduled_posting": {
        "platforms": {"x": {"buffer_api_key_env": name}}}})
    assert res.status_code == 200
    with open(pm.get_config_path("p"), encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["scheduled_posting"]["platforms"]["x"]["buffer_api_key_env"] == name


def test_the_general_config_editor_still_saves_unrelated_sections(client):
    _save("BUFFER_API_KEY_X")
    res = client.post("/api/profiles/p/config", json={"behavior": {"anything": 1}})
    assert res.status_code == 200


# ─── a hand-edited config: what reaches Buffer (THE GATE) ────────────────────

@pytest.mark.parametrize("name", OUTSIDE_NAMES + ["  PATH  ", "path"])
def test_a_hand_edited_name_never_reaches_buffer(client, sent, jobs, name):
    """No header at all, so this also catches a fall back to BUFFER_API_KEY."""
    _save(name)
    seen, bodies = _drive_every_buffer_path(client, sent, jobs)
    assert seen == {action: [] for action in seen}
    assert jobs == []
    for body in bodies:
        assert LEAK not in body.get_data(as_text=True)


@pytest.mark.parametrize("name,value", ALLOWED)
def test_a_name_inside_the_rule_reaches_buffer_on_every_path(client, sent, jobs, name, value):
    """The other direction, so the test above cannot pass by never asking."""
    _save(name)
    seen, _ = _drive_every_buffer_path(client, sent, jobs)
    for action in ("queue", "reconcile", "preflight", "test connection", "schedule"):
        assert seen[action], "%s never asked Buffer" % action
        assert set(seen[action]) == {"Bearer %s" % value}
    assert seen["settings"] == []


@pytest.mark.parametrize("name", OUTSIDE_NAMES)
def test_the_resolver_refuses_without_reading_the_variable(name):
    _save(name)

    class Env(dict):
        read = []

        def get(self, key, default=None):
            self.read.append(key)
            return LEAK

    env = Env()
    with pytest.raises(pm.ScheduledConfigError, match="not an allowed name"):
        pm.resolve_scheduled("p", "x", env=env)
    assert env.read == []


def test_the_cli_refuses_before_any_buffer_call(sent, monkeypatch, capsys):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "run_scheduled_posts.py")
    spec = importlib.util.spec_from_file_location("run_scheduled_posts", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    _save("OPENAI_API_KEY")
    monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py", "status",
                                      "--profile", "p", "--platform", "x"])
    assert cli.main() == 2
    out = capsys.readouterr().out
    assert "not an allowed name" in out
    assert LEAK not in out
    assert sent == []


# ─── a hand-edited config: how it is reported ────────────────────────────────

@pytest.mark.parametrize("name", OUTSIDE_NAMES)
def test_settings_report_it_blank_and_misconfigured(client, name):
    _save(name)
    body = client.get("/api/scheduled/p/settings?platform=x").get_json()
    assert body["buffer_api_key_env"] == ""
    assert body["api_key_env_set"] is False
    assert "buffer_api_key_env" in body["missing"]
    assert pm.BUFFER_KEY_ENV_RULE in body["key_env_problem"]
    assert name not in json.dumps(body)


@pytest.mark.parametrize("name,_value", ALLOWED)
def test_settings_report_no_problem_for_a_name_inside_the_rule(client, name, _value):
    _save(name)
    body = client.get("/api/scheduled/p/settings?platform=x").get_json()
    assert body["buffer_api_key_env"] == name
    assert body["api_key_env_set"] is True
    assert body["key_env_problem"] is None
    assert body["missing"] == []


@pytest.mark.parametrize("value", ["", None, 12345])
def test_a_blank_or_non_string_name_is_missing_with_no_problem_line(client, value):
    """Nothing was rejected, so nothing should say so."""
    _save(value)
    body = client.get("/api/scheduled/p/settings?platform=x").get_json()
    assert "buffer_api_key_env" in body["missing"]
    assert body["key_env_problem"] is None


def test_linkedin_has_no_problem_line():
    assert dashboard._scheduled_settings_view("p", "linkedin")["key_env_problem"] is None


def test_the_schedule_route_refuses_with_the_reason_not_a_500(client, jobs):
    _save("PATH")
    _seed_rows()
    res = client.post("/api/scheduled/p/schedule?platform=x")
    assert res.status_code == 400
    assert "not an allowed name" in res.get_json()["error"]
    assert jobs == []


def test_a_pasted_key_in_the_stored_field_is_not_repeated_anywhere(client, sent, jobs):
    """Hand-edited, the field could hold the key itself, and these responses
    reach the page and the logs."""
    pasted = "a1b2c3d4e5f6g7h8i9j0buffersecretlookingvalue"
    _save(pasted)
    seen, bodies = _drive_every_buffer_path(client, sent, jobs)
    assert seen == {action: [] for action in seen}
    for body in bodies:
        assert pasted not in body.get_data(as_text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_banner_says_it_is_not_allowed_and_points_at_settings(client, monkeypatch):
    monkeypatch.setenv("PATH", REAL_PATH)   # _env replaced it, and node must be found
    _save("OPENAI_API_KEY")
    config = client.get("/api/scheduled/p/queue?platform=x").get_json()["config"]
    display, html = _paint(client, "x", config)
    assert display == "block"
    assert "<code>buffer_api_key_env</code>" in html
    assert "is not allowed" in html
    assert "BUFFER_API_KEY_&lt;SUFFIX&gt;" in html
    assert "<strong>Settings</strong> button" in html
    assert "OPENAI_API_KEY" not in html
    assert "environment variable outside" not in html


def test_the_settings_form_shows_the_problem_line(client):
    page = client.get("/").get_data(as_text=True)
    assert "d.key_env_problem" in page
    assert "config.key_env_problem" in page
