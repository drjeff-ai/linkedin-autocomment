"""P4 (Pinterest part): the scheduled-posting panel lists the registry's channels.

The page is rendered with the registry's channels, in registry order, and the
platform pills, labels and default platform come from that list. A third
channel monkeypatched into ``channels.REGISTRY`` must appear as a pill and be
answered by the settings and queue routes. LinkedIn and X look as before.
"""

import dataclasses
import io
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from linkedin_automation import channels  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402

TEMPLATE = os.path.join(dashboard._PACKAGE_DIR, "templates", "dashboard.html")
PILL = re.compile(r'<button class="schp-plat" data-plat="([^"]*)"[^>]*>([^<]*)</button>')

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def zz(monkeypatch):
    """A third overlay channel, after LinkedIn and X in registry order."""
    channel = channels.Channel(
        id="zz", label="Test Channel", link_placement=channels.BODY,
        text_limit=None, identity_check=channels.BUFFER_HANDLE,
        config_layout=channels.OVERLAY, key_env=None,
        state_path=channels.PLATFORM_DIR, reconcile_statuses=("sent", "error"),
        extra_columns=(channels.ExtraColumn("title"),))
    monkeypatch.setitem(channels.REGISTRY, "zz", channel)
    return channel


def _page(client):
    res = client.get("/")
    assert res.status_code == 200
    return res.get_data(as_text=True)


def _pills(page):
    return PILL.findall(page)


def _page_channel_json(page):
    match = re.search(r"const SCHP_CHANNELS = (.*);\n", page.replace("\r\n", "\n"))
    assert match, "SCHP_CHANNELS not rendered"
    return json.loads(match.group(1))


# --- the pills ---------------------------------------------------------------

def test_the_real_registry_renders_exactly_linkedin_x_and_pinterest(client):
    assert _pills(_page(client)) == [("linkedin", "LinkedIn"), ("x", "X"),
                                     ("pinterest", "Pinterest")]


def test_a_third_channel_appears_as_a_pill_in_registry_order(client, zz):
    assert _pills(_page(client)) == [("linkedin", "LinkedIn"), ("x", "X"),
                                     ("pinterest", "Pinterest"),
                                     ("zz", "Test Channel")]


def test_the_page_channel_list_comes_from_the_registry(client, zz):
    listed = _page_channel_json(_page(client))
    assert listed == [
        {"id": "linkedin", "label": "LinkedIn", "config_layout": "flat",
         "extra_columns": []},
        {"id": "x", "label": "X", "config_layout": "overlay", "extra_columns": []},
        {"id": "pinterest", "label": "Pinterest", "config_layout": "overlay",
         "extra_columns": ["title", "alt_text", "board"]},
        {"id": "zz", "label": "Test Channel", "config_layout": "overlay",
         "extra_columns": ["title"]},
    ]
    assert listed == dashboard._page_channels()


def test_the_default_platform_is_the_first_channel(client):
    page = _page(client)
    assert "let schpPlatform = SCHP_CHANNELS[0].id;" in page
    assert _page_channel_json(page)[0]["id"] == "linkedin"


def test_a_label_is_html_escaped_in_the_pill(client, monkeypatch):
    odd = dataclasses.replace(channels.undeclared("odd"), label="<b>Odd</b>")
    monkeypatch.setitem(channels.REGISTRY, "odd", odd)
    page = _page(client)
    assert ("odd", "&lt;b&gt;Odd&lt;/b&gt;") in _pills(page)
    assert "<b>Odd</b>" not in page.split("const SCHP_CHANNELS")[0]


def test_no_platform_id_is_hard_coded_in_the_scheduled_posting_code():
    """The dispatch's gate-3 patterns, read from the template file itself."""
    with open(TEMPLATE, encoding="utf-8") as f:
        source = f.read()
    pattern = re.compile(r"=== 'x'|=== 'linkedin'|\? 'X' : 'LinkedIn'|"
                         r"schpPlatform = 'linkedin'|\|\| 'linkedin'")
    assert pattern.findall(source) == []
    assert 'data-plat="linkedin"' not in source and 'data-plat="x"' not in source


def test_the_hard_coding_check_can_go_red():
    pattern = re.compile(r"=== 'x'|schpPlatform = 'linkedin'")
    assert pattern.findall("let schpPlatform = 'linkedin'; if (p === 'x') {}")


# --- the routes answer for the third channel --------------------------------

def test_settings_get_and_post_work_for_the_third_channel(client, zz, monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY_ZZ", "zz-key")
    res = client.get("/api/scheduled/p/settings?platform=zz")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["platform"] == "zz"
    assert res.get_json()["buffer_api_key_env_editable"] is True

    res = client.post("/api/scheduled/p/settings", json={
        "platform": "zz", "buffer_channel_id": "chan_zz",
        "identity_slug": "zz-handle", "buffer_api_key_env": "BUFFER_API_KEY_ZZ"})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["settings"]["configured"] is True
    stored = pm.read_stored_config("p")["scheduled_posting"]["platforms"]["zz"]
    assert stored == {"buffer_channel_id": "chan_zz", "identity_slug": "zz-handle",
                      "buffer_api_key_env": "BUFFER_API_KEY_ZZ"}
    again = client.get("/api/scheduled/p/settings?platform=zz").get_json()
    assert again["buffer_channel_id"] == "chan_zz"


def test_a_csv_upload_for_the_third_channel_queues_into_its_own_dir(client, zz):
    csv_text = ("date,time_window,post_text,topic,tags,image_path,title\n"
                "2036-09-08,morning,Third channel post,,,,A title\n")
    res = client.post("/api/scheduled/p/rows?platform=zz",
                      data={"file": (io.BytesIO(csv_text.encode("utf-8")), "z.csv")},
                      content_type="multipart/form-data")
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["counts"]["accepted"] == 1, body
    state_file = os.path.join(pm.DATA_ROOT, "p", "zz", "scheduled_posts_state.json")
    with open(state_file, encoding="utf-8") as f:
        (entry,) = json.load(f)["rows"].values()
    assert entry["row"]["title"] == "A title"
    assert not os.path.exists(os.path.join(pm.DATA_ROOT, "p",
                                           "scheduled_posts_state.json"))


def test_the_queue_config_carries_the_channel_label_and_layout(client, zz):
    for platform, label, layout in (("linkedin", "LinkedIn", "flat"),
                                    ("x", "X", "overlay"),
                                    ("zz", "Test Channel", "overlay")):
        config = client.get("/api/scheduled/p/queue?platform=%s"
                            % platform).get_json()["config"]
        assert (config["label"], config["config_layout"]) == (label, layout)


# --- the banner, run under node from the served page -------------------------

def _function(source, name):
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name)
                      + r"\s*\([^)]*\)\s*\{", source)
    assert match, name
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


def _banner(client, platform):
    page = _page(client)
    config = client.get("/api/scheduled/p/queue?platform=%s"
                        % platform).get_json()["config"]
    script = "\n".join([
        _function(page, "schpEsc"), _function(page, "schpPaintConfigWarn"),
        "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));",
        "const warn = {style: {display: 'unset'}, innerHTML: ''};",
        "schpPaintConfigWarn(warn, input.platform, input.config);",
        "console.log(JSON.stringify(warn.innerHTML));"])
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          input=json.dumps({"platform": platform, "config": config}),
                          timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_the_banner_names_the_third_channel_by_its_label(client, zz):
    html = _banner(client, "zz")
    assert html.startswith("Test Channel is missing ")
    assert "has no <code>identity_slug</code>" not in html, \
        "an overlay channel's slug is in missing, not the flat-only line"


@needs_node
def test_the_banner_keeps_linkedins_flat_identity_line(client):
    html = _banner(client, "linkedin")
    assert "LinkedIn has no <code>identity_slug</code>" in html
