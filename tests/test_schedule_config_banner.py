"""Dispatch 45: the unconfigured-platform banner (#schpConfigWarn) names the
missing fields and points at the Settings button, not at the config file.

The banner only exists once JavaScript has run, so the assertions run the
function from the page GET / actually serves, under node, against the config
block the real queue endpoint returns. A check of the endpoint alone would pass
with the old banner still on screen (docs/ARCHITECTURE.md §1.8).

The node tests skip where node is not installed. The source-level tests below
them always run, so the wiring and the wording stay gated either way.
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402


needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")

CONFIG_FILE_WORDING = ("profile_config", "scheduled_posting", "config")


@pytest.fixture
def client(monkeypatch):
    """The queue endpoint polls Buffer on render, so answer that from a fake."""
    monkeypatch.setattr(dashboard.buffer_client, "posts_by_status",
                        lambda *a, **kw: {})
    dashboard._reconcile_cache.clear()
    dashboard.app.config["TESTING"] = True
    yield dashboard.app.test_client()
    dashboard._reconcile_cache.clear()


def _save(scheduled_posting):
    cfg = pm.load_default_config()
    cfg["scheduled_posting"] = scheduled_posting
    pm.save_profile_config("p", cfg)


def _x(channel="chan_x", slug="example-x", key_env="BUFFER_API_KEY_X"):
    return {"buffer_channel_id": "chan_li", "identity_slug": "example-person",
            "platforms": {"x": {"buffer_api_key_env": key_env,
                                "buffer_channel_id": channel,
                                "identity_slug": slug}}}


def _page(client):
    return client.get("/").get_data(as_text=True)


def _function(source, name):
    """A top-level JS function, signature included, by brace matching."""
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                      source)
    assert match, "function %s not found in the served page" % name
    depth, i = 0, match.end() - 1
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():i + 1]
        i += 1
    raise AssertionError("unbalanced braces in %s" % name)


def _paint(client, platform, config):
    """Run the served page's banner code under node. Returns (display, html)."""
    page = _page(client)
    script = "\n".join([
        _function(page, "schpEsc"),
        _function(page, "schpPaintConfigWarn"),
        "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));",
        "const warn = {style: {display: 'unset'}, innerHTML: 'stale'};",
        "schpPaintConfigWarn(warn, input.platform, input.config);",
        "console.log(JSON.stringify({display: warn.style.display, html: warn.innerHTML}));",
    ])
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          input=json.dumps({"platform": platform, "config": config}),
                          timeout=30)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    return out["display"], out["html"]


def _banner(client, platform):
    """The banner for what the REAL queue endpoint says about this pair."""
    res = client.get("/api/scheduled/p/queue?platform=%s" % platform)
    assert res.status_code == 200
    return _paint(client, platform, res.get_json()["config"])


def _assert_no_config_file_talk(html):
    for word in CONFIG_FILE_WORDING:
        assert word not in html.lower(), "%r in banner: %s" % (word, html)


# ─── what the banner says, per state ─────────────────────────────────────────

@needs_node
def test_a_blank_x_pair_names_both_fields_and_points_at_settings(client, monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _save(_x(channel="", slug=""))
    display, html = _banner(client, "x")
    assert display == "block"
    assert "<code>buffer_channel_id</code>" in html
    assert "<code>identity_slug</code>" in html
    assert "<strong>Settings</strong> button" in html
    assert "these are set" in html
    _assert_no_config_file_talk(html)


@needs_node
def test_x_with_an_identity_but_no_channel_still_warns(client, monkeypatch):
    """The old banner was gated on the identity alone, so this pair showed
    nothing at all while scheduling refused."""
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _save(_x(channel=""))
    display, html = _banner(client, "x")
    assert display == "block"
    assert "<code>buffer_channel_id</code>" in html
    assert "identity_slug" not in html
    assert "this is set" in html
    assert "<strong>Settings</strong> button" in html


@needs_node
def test_a_blank_key_variable_name_is_named_as_a_settings_field(client, monkeypatch):
    """The resolver raises here, and its message names raw config paths. The
    banner must name the field without repeating that message."""
    _save(_x(key_env=""))
    display, html = _banner(client, "x")
    assert display == "block"
    assert "<code>buffer_api_key_env</code>" in html
    assert "<strong>Settings</strong> button" in html
    _assert_no_config_file_talk(html)


@needs_node
def test_an_unset_key_variable_is_not_offered_as_a_form_fix(client, monkeypatch):
    """Settings stores the variable's NAME, so telling someone to fix an unset
    variable there would send them to a form that cannot do it."""
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    _save(_x())
    display, html = _banner(client, "x")
    assert display == "block"
    assert "<code>BUFFER_API_KEY_X environment variable</code>" in html
    assert "Fix this with" not in html
    assert "<strong>Settings</strong> holds only its name" in html
    _assert_no_config_file_talk(html)


@needs_node
def test_a_blank_linkedin_identity_does_not_claim_scheduling_refuses(client):
    """LinkedIn's schedule path is ungated, but check_production_guard refuses
    every browser job on a blank identity."""
    _save({"buffer_channel_id": "chan_li", "identity_slug": ""})
    display, html = _banner(client, "linkedin")
    assert display == "block"
    assert "<code>identity_slug</code>" in html
    assert "Browser jobs, including the first-comment pass, refuse" in html
    assert "Scheduling" not in html
    assert "<strong>Settings</strong> button" in html
    _assert_no_config_file_talk(html)


@needs_node
def test_linkedin_with_nothing_set_names_both_without_contradiction(client):
    """A blank channel does refuse scheduling, so nothing may say it still runs."""
    _save({"buffer_channel_id": "", "identity_slug": ""})
    display, html = _banner(client, "linkedin")
    assert display == "block"
    assert "<code>buffer_channel_id</code>" in html
    assert "Scheduling refuses until this is set" in html
    assert "<code>identity_slug</code>" in html
    assert "Browser jobs, including the first-comment pass, refuse" in html
    assert "still runs" not in html


@needs_node
def test_an_x_overlay_never_saved_names_all_three(client, monkeypatch):
    """The commonest real state: the shipped default overlay, nothing filled in."""
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    _save({"buffer_channel_id": "chan_li", "identity_slug": "example-person"})
    display, html = _banner(client, "x")
    assert display == "block"
    assert "<code>buffer_channel_id</code>" in html
    assert "<code>identity_slug</code>" in html
    assert "<code>BUFFER_API_KEY_X environment variable</code>" in html
    assert "<strong>Settings</strong> button" in html
    assert "<strong>Settings</strong> holds only its name" in html
    _assert_no_config_file_talk(html)


@needs_node
@pytest.mark.parametrize("platform", ["linkedin", "x"])
def test_a_configured_platform_hides_and_clears_the_banner(client, monkeypatch, platform):
    """The other direction: a banner that can only warn is noise."""
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _save(_x())
    assert _banner(client, platform) == ("none", "")


@needs_node
def test_field_names_are_escaped(client):
    """An env-var name is user-supplied config, and this writes innerHTML."""
    _, html = _paint(client, "x", {"missing": ["<img src=x> environment variable"],
                                   "identity_configured": True})
    assert "<img" not in html
    assert "&lt;img src=x&gt; environment variable" in html


# ─── a hand-edited non-string is missing, not a crash ────────────────────────
#
# Both key states matter for X: with the key set the resolver reaches the fields
# and used to raise, with it unset the settings view did.

NON_STRINGS = [pytest.param(12345, id="integer"),
               pytest.param(None, id="null"),
               pytest.param(["chan"], id="list")]


def _set_x_key(monkeypatch, key_set):
    if key_set:
        monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    else:
        monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)


def _config_block(client, platform):
    res = client.get("/api/scheduled/p/queue?platform=%s" % platform)
    assert res.status_code == 200
    return res.get_json()["config"]


@pytest.mark.parametrize("key_set", [True, False], ids=["key_set", "key_unset"])
@pytest.mark.parametrize("value", NON_STRINGS)
@pytest.mark.parametrize("field,kwarg", [("buffer_channel_id", "channel"),
                                         ("identity_slug", "slug")])
def test_a_non_string_x_field_is_listed_as_missing(client, monkeypatch, field, kwarg,
                                                   value, key_set):
    _set_x_key(monkeypatch, key_set)
    _save(_x(**{kwarg: value}))
    other = "identity_slug" if field == "buffer_channel_id" else "buffer_channel_id"
    missing = _config_block(client, "x")["missing"]
    assert field in missing
    assert other not in missing


@pytest.mark.parametrize("value", NON_STRINGS)
def test_a_non_string_linkedin_channel_is_listed_as_missing(client, value):
    _save({"buffer_channel_id": value, "identity_slug": "example-person"})
    assert _config_block(client, "linkedin")["missing"] == ["buffer_channel_id"]


@pytest.mark.parametrize("value", NON_STRINGS)
def test_a_non_string_linkedin_identity_reads_as_unset(client, value):
    """A blank LinkedIn identity is deliberately not in `missing`, because the
    schedule path is ungated, so `identity_configured` is what carries it."""
    _save({"buffer_channel_id": "chan_li", "identity_slug": value})
    config = _config_block(client, "linkedin")
    assert config["identity_configured"] is False
    assert config["identity_slug"] == ""


@needs_node
@pytest.mark.parametrize("value", NON_STRINGS)
def test_the_banner_names_a_non_string_x_channel(client, monkeypatch, value):
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _save(_x(channel=value))
    display, html = _banner(client, "x")
    assert display == "block"
    assert "<code>buffer_channel_id</code>" in html
    assert "<strong>Settings</strong> button" in html


@needs_node
@pytest.mark.parametrize("value", NON_STRINGS)
def test_the_banner_names_a_non_string_linkedin_identity(client, value):
    _save({"buffer_channel_id": "chan_li", "identity_slug": value})
    display, html = _banner(client, "linkedin")
    assert display == "block"
    assert "<code>identity_slug</code>" in html


@pytest.mark.parametrize("value", NON_STRINGS)
def test_the_settings_form_loads_with_a_non_string_field(client, monkeypatch, value):
    """The form is where the banner sends you, so it must open to fix this."""
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    _save(_x(channel=value))
    res = client.get("/api/scheduled/p/settings?platform=x")
    assert res.status_code == 200
    assert res.get_json()["buffer_channel_id"] == ""
    assert "buffer_channel_id" in res.get_json()["missing"]


def _buffer_calls_on_render(client, monkeypatch, channel):
    """Queue render with a row awaiting publication. Without one the reconcile
    exits early whatever the channel is, and this could not see a bad call."""
    calls = []
    monkeypatch.setattr(dashboard.buffer_client, "posts_by_status",
                        lambda channel_id, *a, **kw: calls.append(channel_id) or {})
    _save({"buffer_channel_id": channel, "identity_slug": "example-person"})
    state = cp.PipelineState(profile_name="p")
    state.rows["k"] = {"status": cp.SCHEDULED, "post_id": "p1", "text": "Waiting."}
    state.save()
    assert client.get("/api/scheduled/p/queue").status_code == 200
    return calls


@pytest.mark.parametrize("value", NON_STRINGS)
def test_a_non_string_channel_never_reaches_buffer(client, monkeypatch, value):
    """str(12345) would be a channel id nobody configured."""
    assert _buffer_calls_on_render(client, monkeypatch, value) == []


def test_a_real_channel_does_reach_buffer(client, monkeypatch):
    """The other direction, so the test above cannot pass by never asking."""
    assert _buffer_calls_on_render(client, monkeypatch, "chan_li") == ["chan_li"]


@pytest.mark.parametrize("value", NON_STRINGS)
def test_the_browser_guard_refuses_a_non_string_identity_by_name(value):
    _save({"buffer_channel_id": "chan_li", "identity_slug": value})
    with pytest.raises(pm.ProductionAccessRefused, match="no declared identity"):
        pm.check_production_guard("p", allow_production=True)


# ─── the same for the key variable's name, on X ──────────────────────────────

NON_STRING_KEY_ENVS = [pytest.param(12345, id="integer"),
                       pytest.param(["BUFFER_API_KEY_X"], id="list"),
                       pytest.param({"name": "BUFFER_API_KEY_X"}, id="dict")]


@pytest.mark.parametrize("value", NON_STRING_KEY_ENVS)
def test_a_non_string_key_variable_is_named_by_the_queue(client, value):
    _save(_x(key_env=value))
    missing = _config_block(client, "x")["missing"]
    assert "buffer_api_key_env" in missing


@pytest.mark.parametrize("value", NON_STRING_KEY_ENVS)
def test_a_non_string_key_variable_leaves_settings_open_to_repair_it(client, value):
    _save(_x(key_env=value))
    res = client.get("/api/scheduled/p/settings?platform=x")
    assert res.status_code == 200
    body = res.get_json()
    assert "buffer_api_key_env" in body["missing"]
    assert body["buffer_api_key_env"] == ""
    assert body["buffer_channel_id"] == "chan_x"


@needs_node
@pytest.mark.parametrize("value", NON_STRING_KEY_ENVS)
def test_the_banner_names_a_non_string_key_variable(client, value):
    _save(_x(key_env=value))
    display, html = _banner(client, "x")
    assert display == "block"
    assert "<code>buffer_api_key_env</code>" in html
    assert "<strong>Settings</strong> button" in html
    _assert_no_config_file_talk(html)


@pytest.mark.parametrize("value", NON_STRING_KEY_ENVS)
def test_the_resolver_refuses_a_non_string_key_variable(value):
    _save(_x(key_env=value))
    with pytest.raises(pm.ScheduledConfigError, match="no buffer_api_key_env"):
        pm.resolve_scheduled("p", "x")


# ─── PINNED: a non-string LinkedIn identity behaves exactly as a blank one ───
#
# Blank, not a crash and not a special case: scheduling proceeds, because that
# path is deliberately ungated and never reads the slug, and browser jobs refuse.

NON_STRING_IDENTITIES = [pytest.param(12345, id="integer"),
                         pytest.param(["slug"], id="list"),
                         pytest.param({"slug": "x"}, id="dict"),
                         pytest.param(None, id="null")]


def _save_linkedin_identity(value):
    _save({"buffer_channel_id": "chan_li", "identity_slug": value})


class _SpyChrome:
    built = []

    def __init__(self, *a, **kw):
        _SpyChrome.built.append(self)

    def set_page_load_timeout(self, seconds):
        pass

    def maximize_window(self):
        pass

    def execute_script(self, *a):
        return None


@pytest.fixture
def browser_factory(monkeypatch, tmp_path):
    """Everything create_driver needs except the guard, which stays real and
    reads the real saved config. Chrome is a spy."""
    _SpyChrome.built = []
    monkeypatch.setattr(pm.webdriver, "Chrome", _SpyChrome)
    monkeypatch.setattr(pm, "Service", lambda *a, **kw: None)
    monkeypatch.setattr(pm, "ChromeDriverManager",
                        lambda: type("M", (), {"install": lambda self: "x"})())
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "get_profile", lambda name: {
        "username": "u", "session_dir": str(tmp_path)})
    monkeypatch.setattr(pm, "session_exists", lambda path: True)
    monkeypatch.setattr(pm, "update_last_used", lambda name: None)
    return _SpyChrome


@pytest.mark.parametrize("value", NON_STRING_IDENTITIES)
def test_the_config_block_is_identical_to_a_blank_identity(client, value):
    _save_linkedin_identity("")
    blank = _config_block(client, "linkedin")
    _save_linkedin_identity(value)
    config = _config_block(client, "linkedin")
    assert config == blank
    assert config["identity_configured"] is False
    assert config["identity_slug"] == ""


@needs_node
@pytest.mark.parametrize("value", NON_STRING_IDENTITIES)
def test_the_banner_names_it_and_does_not_claim_scheduling_refuses(client, value):
    _save_linkedin_identity(value)
    display, html = _banner(client, "linkedin")
    assert display == "block"
    assert "<code>identity_slug</code>" in html
    assert "Browser jobs, including the first-comment pass, refuse" in html
    assert "Scheduling" not in html


@pytest.mark.parametrize("value", NON_STRING_IDENTITIES)
def test_the_browser_factory_refuses_before_building_a_browser(browser_factory, value):
    """allow_production=True is what the dashboard always passes, so this is
    the refusal a real job meets. The spy is the assertion: no browser at all."""
    _save_linkedin_identity(value)
    with pytest.raises(pm.ProductionAccessRefused, match="no declared identity"):
        pm.create_driver("p", allow_production=True)
    assert browser_factory.built == []


def test_the_browser_factory_builds_for_a_real_identity(browser_factory):
    """The other direction, so the spy above cannot pass by never being reachable."""
    _save_linkedin_identity("example-person")
    pm.create_driver("p", allow_production=True)
    assert len(browser_factory.built) == 1


@pytest.mark.parametrize("value", NON_STRING_IDENTITIES)
def test_the_first_comment_pass_never_starts_its_browser_process(monkeypatch, value):
    def boom(job_id, cmd):
        pytest.fail("the first-comment subprocess must not start")
    monkeypatch.setattr(dashboard, "run_subprocess", boom)
    _save_linkedin_identity(value)
    with pytest.raises(RuntimeError, match="no identity_slug"):
        dashboard._first_comment_drain_job("job", "p")


@pytest.mark.parametrize("value", NON_STRING_IDENTITIES + [pytest.param("", id="blank")])
def test_scheduling_proceeds_to_create_post(client, monkeypatch, value):
    """The real route and the real schedule_pass; only Buffer's edge is faked."""
    created = []

    def fake_create(channel_id, body, image_url, when, key=None, session=None):
        created.append(channel_id)
        return {"id": "post1", "status": "scheduled", "dueAt": when}

    monkeypatch.setattr(cp.bc, "create_post", fake_create)
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda *a, **kw: {"used": 0, "limit": 10, "free": 10})
    jobs = []
    monkeypatch.setattr(dashboard, "run_job",
                        lambda job_id, func, *a, **kw: jobs.append((job_id, func, a)))

    _save_linkedin_identity(value)
    row = {"date": "2036-11-02", "time_window": "morning",
           "post_text": "A short post.", "topic": "t"}
    state = cp.PipelineState(profile_name="p")
    state.rows[cp.row_key(row)] = {"status": cp.PENDING, "row": row}
    state.save()

    res = client.post("/api/scheduled/p/schedule")
    assert res.status_code == 200
    assert len(jobs) == 1

    job_id, func, args = jobs[0]
    outcome = func(job_id, *args)
    assert created == ["chan_li"]
    assert outcome["scheduled"] == 1
    assert not outcome.get("error")


def test_a_real_string_is_still_stripped_and_kept():
    """The other direction: this must not read every value as missing."""
    assert pm.scheduled_config_str("  chan_li ") == "chan_li"
    _save({"buffer_channel_id": " chan_li ", "identity_slug": " example-person "})
    resolved = pm.resolve_scheduled("p", "linkedin")
    assert resolved["channel_id"] == "chan_li"
    assert resolved["identity_slug"] == "example-person"


# ─── the page itself, no node needed ─────────────────────────────────────────

def test_the_queue_loader_paints_the_banner_from_the_config_block(client):
    loader = _function(_page(client), "loadScheduledQueue")
    assert "schpPaintConfigWarn(document.getElementById('schpConfigWarn'), " \
           "schpPlatform, d.config)" in loader


def test_the_page_no_longer_tells_anyone_to_edit_the_config_file(client):
    page = _page(client)
    assert "profile\\'s config" not in page
    assert ".identity_slug</code>" not in page
    loader = _function(page, "loadScheduledQueue")
    assert "config.problem" not in loader


def test_the_banner_code_reads_missing_and_names_the_settings_button(client):
    page = _page(client)
    code = re.sub(r"//.*$", "", _function(page, "schpPaintConfigWarn"), flags=re.M)
    assert "config.missing" in code
    assert "config.problem" not in code
    assert "<strong>Settings</strong> button" in code
    for word in ("profile_config", "scheduled_posting"):
        assert word not in code


def test_the_button_the_banner_points_at_exists_in_the_same_panel(client):
    page = _page(client)
    panel = page[page.index('id="schpHeader"'):page.index('id="schpSettingsCard"')]
    assert re.search(r'onclick="schpToggleSettings\(\)"[^>]*>Settings</button>', panel)
    assert 'id="schpConfigWarn"' in panel


# ─── one list, two surfaces ──────────────────────────────────────────────────

@pytest.mark.parametrize("platform", ["linkedin", "x"])
def test_the_banner_and_the_settings_form_read_the_same_list(client, monkeypatch, platform):
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    _save({"buffer_channel_id": "", "identity_slug": "",
           "platforms": {"x": {"buffer_api_key_env": "BUFFER_API_KEY_X",
                               "buffer_channel_id": "", "identity_slug": ""}}})
    queue = client.get("/api/scheduled/p/queue?platform=%s" % platform).get_json()
    settings = client.get("/api/scheduled/p/settings?platform=%s" % platform).get_json()
    assert queue["config"]["missing"] == settings["missing"]
    assert settings["missing"]
