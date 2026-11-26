"""V1 (D-026): YouTube Shorts as a platform channel.

Google is mocked everywhere, through a fake of ``youtube_api``'s functions, and
any real socket connect fails the test (``_no_network``). The topfive fixtures
copy the structure of the eight real ``youtube.md`` files (their three layouts,
their draft Description/Tags lines above the Shorts section, their paragraph
counts, eight tags each) with synthetic text, so no post text is committed
(PRINCIPLES §10). That the port parses the real files exactly as
video-publisher does was checked outside the suite (8 of 8 agree, UPDATES D-026).
"""

import hashlib
import json
import os
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from linkedin_automation import channels  # noqa: E402
from linkedin_automation import content_calendar  # noqa: E402
from linkedin_automation import publisher_lock  # noqa: E402
from linkedin_automation import youtube_api  # noqa: E402
from linkedin_automation import youtube_channel as yt  # noqa: E402

CHANNEL_ID = "UC_test_channel"
PROFILE = "topfive"
UTC = timezone.utc
# Sunday 2026-11-22 15:00 Pacific.
NOW = datetime(2026, 11, 22, 23, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ── synthetic copies of the eight real youtube.md layouts ───────────────────

#: name -> (layout, Shorts description paragraphs before the link and hashtags)
LAYOUTS = {
    "C-001-where-did-my-rule-go": ("line", 1),
    "C-002-your-ai-doesnt-read-letters": ("line", 1),
    "C-003-it-sounded-so-sure": ("line", 1),
    "C-004-somebody-still-has-to-shoot-the-movie": ("essay_line", 2),
    "C-006-borrow-the-wheels": ("essay_line", 1),
    "C-011-fail-faster-at-scale": ("what", 2),
    "C-012-ask-first-accept-later": ("what", 2),
    "C-013-grade-it-like-a-new-hire": ("what", 2),
}


def shorts_expected(name):
    n = yt.c_number(name)
    _layout, paras = LAYOUTS[name]
    body = ["Synthetic paragraph %d of short %d, with words and a comma." % (i + 1, n)
            for i in range(paras)]
    return {"title": "Synthetic short %d #shorts" % n,
            "description": "\n\n".join(body + ["More at example.com",
                                               "#Synth #Short%d" % n]),
            "tags": ["tag %d-%d" % (n, i) if i % 2 else "tag%d%d" % (n, i)
                     for i in range(8)]}


def youtube_md(name):
    n = yt.c_number(name)
    layout, _paras = LAYOUTS[name]
    exp = shorts_expected(name)
    lines = ["## C-%03d · Synthetic %d (40.1 s)" % (n, n),
             "- **Files:** `%s-{vertical,horizontal}.mp4`" % name]
    if layout == "essay_line":
        lines.append("- **Source essay:** essay-%d" % n)
    if layout in ("line", "essay_line"):
        lines += ["- **Line:** A draft line.",
                  "- **Description:** A DRAFT description that must be ignored.",
                  "- **Tags:** #Draft #Ignored"]
    else:
        lines += ["- **What it is:** A draft summary.",
                  "- **Caption (draft):** A draft caption.",
                  "- **Essay:** essay-%d" % n,
                  "- **Tags:** #Draft #Ignored"]
    lines += ["", "### YouTube Shorts (2026-11-20)",
              "- **Title:** %s" % exp["title"], "- **Description:**"]
    for i, para in enumerate(exp["description"].split("\n\n")):
        if i:
            lines.append("")
        lines.append("  " + para)
    lines.append("- **Tags:** " + ", ".join(exp["tags"]))
    return "\n".join(lines) + "\n"


def make_topfive(root, names=LAYOUTS, horizontal=True):
    os.makedirs(root, exist_ok=True)
    for name in names:
        d = os.path.join(root, name)
        os.makedirs(d)
        with open(os.path.join(d, "youtube.md"), "w", encoding="utf-8") as f:
            f.write(youtube_md(name))
        with open(os.path.join(d, name + "-vertical.mp4"), "wb") as f:
            f.write(b"v" * 64)
        if horizontal:
            with open(os.path.join(d, name + "-horizontal.mp4"), "wb") as f:
                f.write(b"h" * 64)
    for skip in ("_posted", "ready-to-publish"):
        os.makedirs(os.path.join(root, skip, "C-099-old"), exist_ok=True)


# ── video-publisher fixture ─────────────────────────────────────────────────

def vp_manifest(name, **over):
    m = {"platform": "youtube", "content_type": "video", "brand": "aws",
         "account_id": "aws-yt", "title": "VP title %s" % name,
         "description": "VP description", "tags": ["a", "b"], "category": "27",
         "privacy": "public", "scheduled_for": None}
    m.update(over)
    return m


def vp_write(vp_root, sub, name, manifest):
    d = os.path.join(vp_root, "content", "aws", sub, name)
    os.makedirs(d)
    with open(os.path.join(d, "manifest.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(manifest, f, sort_keys=False)


def vp_db(vp_root, rows):
    os.makedirs(os.path.join(vp_root, "data"), exist_ok=True)
    conn = sqlite3.connect(os.path.join(vp_root, "data", "publish_log.db"))
    conn.execute("CREATE TABLE publish_log (id INTEGER PRIMARY KEY, brand TEXT, "
                 "account_id TEXT, platform TEXT, content_folder TEXT, content_type TEXT, "
                 "status TEXT, platform_response TEXT, error_message TEXT, "
                 "created_at TEXT, updated_at TEXT, publish_at TEXT)")
    for r in rows:
        conn.execute("INSERT INTO publish_log (id, brand, platform, content_folder, status, "
                     "platform_response, updated_at, publish_at) VALUES (?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()


def resp(video_id):
    return json.dumps({"video_id": video_id, "video_url": "u"})


def make_vp(vp_root):
    """The aws history's shape: one live with no date, scheduled ones, two
    queued with dates not yet uploaded, an aif item in both queue/ and
    published/ that the DB says is published, and an Instagram item."""
    published = [("C-010-you-know-ai", None, "LIVE10"),
                 ("C-001-where-did-my-rule-go", "2026-11-23T20:00:00Z", "SCHED01"),
                 ("C-002-your-ai-doesnt-read-letters", "2026-11-24T20:00:00Z", "SCHED02")]
    rows = [(1, "aws", "youtube", "aws/queue/aif-c01-001-yt", "published", resp("AIF001"),
             "2026-05-01 10:00:00", None)]
    for i, (name, at, vid) in enumerate(published, start=2):
        vp_write(vp_root, "published", name + "-yt",
                 vp_manifest(name, publish_at=at and at.replace("20:00:00Z", "12:00:00-08:00")))
        rows.append((i, "aws", "youtube", "aws/queue/%s-yt" % name, "published", resp(vid),
                     "2026-11-22 19:00:00", at))
    vp_write(vp_root, "queue", "C-012-ask-first-accept-later-yt",
             vp_manifest("C-012", publish_at="2026-11-29T12:00:00-08:00"))
    vp_write(vp_root, "queue", "C-013-grade-it-like-a-new-hire-yt",
             vp_manifest("C-013", publish_at="2026-11-30T12:00:00-08:00"))
    vp_write(vp_root, "queue", "aif-c01-001-yt", vp_manifest("aif"))
    vp_write(vp_root, "published", "aif-c01-001-yt", vp_manifest("aif"))
    vp_write(vp_root, "queue", "aif-c01-001-ig",
             {"platform": "instagram", "brand": "aws", "account_id": "aws-ig"})
    rows.append((9, "aws", "instagram", "aws/queue/aif-c01-001-ig", "published",
                 resp("IG1"), "2026-05-01 10:00:00", None))
    vp_db(vp_root, rows)
    os.makedirs(os.path.join(vp_root, "config"), exist_ok=True)


# ── the environment ─────────────────────────────────────────────────────────

class FakeApi:
    """Stands in for youtube_api's functions. Records every call."""

    YouTubeApiError = youtube_api.YouTubeApiError

    def __init__(self, channel=CHANNEL_ID, fail_on=None, quota_fail=False, ids=None):
        self.channel = channel
        self.fail_on = fail_on
        self.quota_fail = quota_fail
        self.ids = list(ids or ["VID%d" % i for i in range(1, 50)])
        self.calls = []
        self.uploads = []
        self.during_upload = None

    def credentials_from_token(self, token):
        self.calls.append("creds")
        assert token.get("refresh_token")
        return object()

    def refresh(self, creds):
        self.calls.append("refresh")

    def build_service(self, creds):
        self.calls.append("build")
        return "service"

    def my_channel_id(self, service):
        self.calls.append("channels.list")
        return self.channel

    def upload(self, service, media, body):
        self.calls.append("upload")
        if self.during_upload:
            self.during_upload()
        self.uploads.append((media, body))
        if self.fail_on is not None and len(self.uploads) == self.fail_on:
            raise youtube_api.YouTubeApiError("videos.insert failed (HTTP 500): boom",
                                              quota=self.quota_fail)
        return self.ids.pop(0)


def overlay(env, **over):
    o = {"vp_brand": "aws", "vp_root": env["vp_root"], "channel_id": CHANNEL_ID,
         "content_root": env["content_root"], "publish_time": "12:00",
         "tz": "America/Los_Angeles", "per_day": 1,
         "quota": {"daily_limit": 10000, "upload_units": 1600, "comment_units": 50}}
    o.update(over)
    return o


@pytest.fixture
def env(tmp_path, monkeypatch):
    data_root = str(tmp_path / "data")
    e = {"tmp": tmp_path, "data_root": data_root, "vp_root": str(tmp_path / "vp"),
         "content_root": str(tmp_path / "topfive"), "lines": []}
    os.makedirs(data_root)
    make_topfive(e["content_root"])
    make_vp(e["vp_root"])
    e["cfg"] = {"scheduled_posting": {"platforms": {"youtube": overlay(e)}}}
    monkeypatch.setattr(yt.pm, "DATA_ROOT", data_root)
    monkeypatch.setattr(yt.pm, "load_profiles",
                        lambda: {"profiles": {PROFILE: {}}, "default": None})
    monkeypatch.setattr(yt.pm, "get_profile_config", lambda p=None: e["cfg"])
    monkeypatch.setattr(yt, "USAGE_LOG", str(tmp_path / "api_usage.jsonl"))
    monkeypatch.delenv("PRODUCTION_IDENTITY_SLUGS", raising=False)
    monkeypatch.delenv("LINKEDIN_ALLOW_PRODUCTION", raising=False)
    e["out"] = e["lines"].append
    return e


def give_token(env):
    yt.save_token(PROFILE, {"client_id": "cid", "client_secret": "SECRET-xyz",
                            "refresh_token": "REFRESH-abc", "channel_id": CHANNEL_ID})


def mark_moved(env, brands=("aws",)):
    yt.write_marker(yt.marker_path(yt.youtube_config(PROFILE)), set(brands))


def state(env):
    return yt.load_state(PROFILE)


def tree(root):
    out = {}
    for dirpath, _d, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.md5(fh.read()).hexdigest()
    return out


def without_locks(t):
    return {k: v for k, v in t.items() if not k.endswith(".lock")}


def ready(env, n_dated=None):
    """Intake done, token stored, brand marked moved."""
    assert yt.intake(PROFILE, now=NOW, out=env["out"])[0] == 0
    give_token(env)
    mark_moved(env)


# ── the registry ────────────────────────────────────────────────────────────

def test_youtube_is_declared_with_its_own_publisher():
    c = channels.get("youtube")
    assert c is yt.CHANNEL and c.publisher == channels.YOUTUBE_API
    assert list(channels.CHANNELS) == ["linkedin", "x", "pinterest", "youtube",
                                       "instagram"]
    # No Buffer path iterates it.
    assert "youtube" not in channels.REGISTRY


# ── config ──────────────────────────────────────────────────────────────────

def test_config_reads_the_overlay(env):
    cfg = yt.youtube_config(PROFILE)
    assert (cfg.vp_brand, cfg.hour, cfg.minute, cfg.per_day, cfg.daily_limit,
            cfg.upload_units, cfg.comment_units) == ("aws", 12, 0, 1, 10000, 1600, 50)
    assert cfg.tz.key == "America/Los_Angeles"


@pytest.mark.parametrize("change,msg", [
    ({"extra": 1}, "unknown keys: extra"),
    ({"vp_brand": ""}, "vp_brand is missing"),
    ({"publish_time": "25:00"}, "publish_time must be HH:MM"),
    ({"tz": "Mars/Base"}, "tz is not a time zone"),
    ({"per_day": 0}, "per_day must be"),
    ({"quota": {"daily": 1}}, "quota takes only"),
])
def test_config_refusals(env, change, msg):
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"].update(change)
    with pytest.raises(yt.YouTubeRefused, match=msg):
        yt.youtube_config(PROFILE)


def test_config_refuses_an_unregistered_profile_and_a_missing_section(env, monkeypatch):
    with pytest.raises(yt.YouTubeRefused, match="no profile 'nobody'"):
        yt.youtube_config("nobody")
    env["cfg"]["scheduled_posting"]["platforms"].pop("youtube")
    with pytest.raises(yt.YouTubeRefused, match="no scheduled_posting.platforms.youtube"):
        yt.youtube_config(PROFILE)


# ── intake parsing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(LAYOUTS))
def test_each_layout_parses_to_its_shorts_section_verbatim(name):
    fields, reason = yt.shorts_fields(yt.parse_youtube_shorts(youtube_md(name)))
    assert reason is None
    assert fields == shorts_expected(name)
    assert "DRAFT" not in fields["description"] and "#Draft" not in fields["tags"]


def test_parse_handles_crlf_and_a_bom():
    name = "C-004-somebody-still-has-to-shoot-the-movie"
    text = youtube_md(name).replace("\n", "\r\n")
    assert yt.shorts_fields(yt.parse_youtube_shorts(text))[0] == shorts_expected(name)


@pytest.mark.parametrize("text,reason", [
    ("## C-1\n- **Title:** x\n", "no YouTube Shorts section"),
    ("### YouTube Shorts\n- **Title:** t\n- **Tags:** a\n", "has no Description"),
    ("### YouTube Shorts\n- **Title:** %s\n- **Description:** d\n- **Tags:** a\n" % ("x" * 101),
     "title is 101 characters"),
    ("### YouTube Shorts\n- **Title:** t\n- **Description:** d\n- **Tags:** %s\n"
     % ", ".join(["long tag %02d words" % i for i in range(30)]), "YouTube-counted"),
])
def test_shorts_refusals_skip_never_cut(text, reason):
    fields, why = yt.shorts_fields(yt.parse_youtube_shorts(text))
    assert fields is None and reason in why


def test_angle_brackets_are_stripped():
    text = "### YouTube Shorts\n- **Title:** a <b> c\n- **Description:** <d>\n- **Tags:** x\n"
    fields, _ = yt.shorts_fields(yt.parse_youtube_shorts(text))
    assert fields["title"] == "a b c" and fields["description"] == "d"


def test_intake_queues_vertical_only_in_c_order_and_skips_posted(env):
    before = tree(env["content_root"])
    code, s = yt.intake(PROFILE, now=NOW, out=env["out"])
    assert code == 0
    assert s["new"] == sorted(LAYOUTS, key=yt.c_number)
    items = state(env)["items"]
    assert set(items) == set(LAYOUTS)
    for name, rec in items.items():
        assert rec["status"] == yt.QUEUED and rec["publish_at"] is None
        assert rec["media_path"].endswith(name + "-vertical.mp4")
        assert {k: rec[k] for k in ("title", "description", "tags")} == shorts_expected(name)
    assert "C-099-old" not in items
    assert any("_posted/" in line for line in env["lines"])
    assert tree(env["content_root"]) == before  # content is read-only


def test_intake_skips_a_folder_without_a_vertical_mp4_and_is_idempotent(env):
    os.remove(os.path.join(env["content_root"], "C-006-borrow-the-wheels",
                           "C-006-borrow-the-wheels-vertical.mp4"))
    code, s = yt.intake(PROFILE, now=NOW, out=env["out"])
    assert s["skipped"] == [("C-006-borrow-the-wheels", "no -vertical.mp4")]
    first = json.dumps(state(env), sort_keys=True)
    code, s = yt.intake(PROFILE, now=NOW + timedelta(hours=1), out=env["out"])
    assert s["new"] == [] and len(s["known"]) == 7
    assert json.dumps(state(env), sort_keys=True) == first


def test_intake_dry_run_writes_nothing(env):
    before = without_locks(tree(env["data_root"]))
    code, s = yt.intake(PROFILE, dry_run=True, now=NOW, out=env["out"])
    assert code == 0 and len(s["new"]) == 8
    assert without_locks(tree(env["data_root"])) == before


# ── dates ───────────────────────────────────────────────────────────────────

def _cfg(env):
    return yt.youtube_config(PROFILE)


def test_dates_start_tomorrow_after_noon_and_go_one_a_day_in_c_order(env):
    yt.intake(PROFILE, now=NOW, out=env["out"])
    st = state(env)
    got = yt.assign_dates(st, _cfg(env), NOW)
    assert [i for i, _ in got] == sorted(LAYOUTS, key=yt.c_number)
    assert [at for _, at in got] == ["2026-11-%02dT20:00:00Z" % d for d in range(23, 31)]


def test_dates_use_today_when_noon_is_more_than_15_minutes_ahead(env):
    yt.intake(PROFILE, now=NOW, out=env["out"])
    morning = datetime(2026, 11, 22, 19, 44, tzinfo=UTC)   # 11:44 PT
    late = datetime(2026, 11, 22, 19, 46, tzinfo=UTC)      # 11:46 PT
    assert yt.assign_dates(state(env), _cfg(env), morning)[0][1] == "2026-11-22T20:00:00Z"
    assert yt.assign_dates(state(env), _cfg(env), late)[0][1] == "2026-11-23T20:00:00Z"


def test_held_dates_are_never_reused_and_existing_dates_never_change(env):
    yt.intake(PROFILE, now=NOW, out=env["out"])
    st = state(env)
    items = st["items"]
    items["C-003-it-sounded-so-sure"]["publish_at"] = "2026-11-23T20:00:00Z"
    items["C-001-where-did-my-rule-go"].update(status=yt.UPLOADED,
                                               publish_at="2026-11-25T20:00:00Z")
    # Went live today with no date: today is held too.
    items["C-010-x"] = yt.new_record("C-010-x", NOW, status=yt.UPLOADED,
                                     uploaded_at="2026-11-22T19:00:00Z")
    got = dict(yt.assign_dates(st, _cfg(env), datetime(2026, 11, 22, 16, 0, tzinfo=UTC)))
    assert items["C-003-it-sounded-so-sure"]["publish_at"] == "2026-11-23T20:00:00Z"
    assert items["C-001-where-did-my-rule-go"]["publish_at"] == "2026-11-25T20:00:00Z"
    assert "C-003-it-sounded-so-sure" not in got and "C-001-where-did-my-rule-go" not in got
    assert got["C-002-your-ai-doesnt-read-letters"] == "2026-11-24T20:00:00Z"
    assert got["C-004-somebody-still-has-to-shoot-the-movie"] == "2026-11-26T20:00:00Z"
    days = [at for at in got.values()] + ["2026-11-23T20:00:00Z", "2026-11-25T20:00:00Z"]
    assert len(days) == len(set(days))
    assert "2026-11-22T20:00:00Z" not in days


def test_per_day_two_uses_publish_time_then_an_hour_later(env):
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["per_day"] = 2
    yt.intake(PROFILE, now=NOW, out=env["out"])
    got = [at for _, at in yt.assign_dates(state(env), _cfg(env), NOW)][:3]
    assert got == ["2026-11-23T20:00:00Z", "2026-11-23T21:00:00Z", "2026-11-24T20:00:00Z"]


def test_per_day_two_never_gives_a_held_time_to_a_new_item(env):
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["per_day"] = 2
    yt.intake(PROFILE, now=NOW, out=env["out"])
    st = state(env)
    # One item already holds the second slot (13:00 PT) of 11-23.
    st["items"]["C-013-grade-it-like-a-new-hire"]["publish_at"] = "2026-11-23T21:00:00Z"
    got = [at for _, at in yt.assign_dates(st, _cfg(env), NOW)][:3]
    assert got == ["2026-11-23T20:00:00Z", "2026-11-24T20:00:00Z", "2026-11-24T21:00:00Z"]


def test_dates_cross_the_dst_change_at_noon_local(env):
    yt.intake(PROFILE, now=NOW, out=env["out"])
    march = datetime(2027, 3, 13, 23, 0, tzinfo=UTC)  # Sat 15:00 PST; DST starts Sunday
    got = [at for _, at in yt.assign_dates(state(env), _cfg(env), march)][:2]
    assert got == ["2027-03-14T19:00:00Z", "2027-03-15T19:00:00Z"]  # noon PDT


# ── request body ────────────────────────────────────────────────────────────

def test_request_body_is_private_with_publish_at_in_utc(env):
    rec = yt.new_record("C-001-a", NOW, title="T #shorts", description="D", tags=["a", "b c"],
                        publish_at="2026-11-23T20:00:00Z")
    assert yt.build_upload_body(rec, _cfg(env)) == {
        "snippet": {"title": "T #shorts", "description": "D", "tags": ["a", "b c"],
                    "categoryId": "27"},
        "status": {"privacyStatus": "private", "publishAt": "2026-11-23T20:00:00Z",
                   "selfDeclaredMadeForKids": False}}


@pytest.mark.parametrize("at,ok", [
    ("2026-11-22T23:16:00Z", True), ("2026-11-22T23:14:59Z", False),
    ("2026-11-22T22:00:00Z", False), (None, False), ("2026-11-22T23:30:00", False),
])
def test_publish_at_refusal_needs_15_minutes_and_a_zone(at, ok):
    assert (yt.publish_at_refusal(at, NOW) is None) is ok


# ── youtube-schedule ────────────────────────────────────────────────────────

def test_schedule_refuses_while_the_marker_does_not_list_the_brand(env):
    yt.intake(PROFILE, now=NOW, out=env["out"])
    give_token(env)
    api = FakeApi()
    code, s = yt.schedule(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_REFUSED and "video-publisher still publishes brand 'aws'" in s["refused"]
    mark_moved(env, brands=("other",))
    code, s = yt.schedule(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_REFUSED and api.calls == []
    assert all(r["publish_at"] is None for r in state(env)["items"].values())


def test_schedule_refuses_an_unreadable_marker(env):
    ready(env)
    with open(yt.marker_path(_cfg(env)), "w", encoding="utf-8") as f:
        f.write("brands: aws\n")
    api = FakeApi()
    code, s = yt.schedule(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_REFUSED and "is not {brands: [names]}" in s["refused"]
    assert api.calls == []


def test_schedule_refuses_without_a_token(env):
    yt.intake(PROFILE, now=NOW, out=env["out"])
    mark_moved(env)
    code, s = yt.schedule(PROFILE, now=NOW, api=FakeApi(), out=env["out"])
    assert code == yt.EXIT_REFUSED and "no OAuth token" in s["refused"]


def test_schedule_dates_and_uploads_earliest_first(env):
    ready(env)
    api = FakeApi()
    code, s = yt.schedule(PROFILE, max_uploads=2, now=NOW, api=api, out=env["out"])
    assert code == 0
    assert api.calls[:4] == ["creds", "refresh", "build", "channels.list"]
    assert [b["status"]["publishAt"] for _m, b in api.uploads] == [
        "2026-11-23T20:00:00Z", "2026-11-24T20:00:00Z"]
    items = state(env)["items"]
    assert items["C-001-where-did-my-rule-go"]["video_id"] == "VID1"
    assert yt.display_state(items["C-001-where-did-my-rule-go"], NOW) == "scheduled"
    assert items["C-003-it-sounded-so-sure"]["status"] == yt.QUEUED
    assert items["C-003-it-sounded-so-sure"]["publish_at"] == "2026-11-25T20:00:00Z"
    ledger = yt.load_quota(PROFILE)
    assert yt.quota_used(ledger, "2026-11-22") == 1 + 2 * 1600
    usage = [json.loads(x) for x in open(yt.USAGE_LOG, encoding="utf-8")]
    assert [u["api"] for u in usage] == ["youtube"] * 4
    assert all(set(u) == {"timestamp", "api", "endpoint", "estimated_cost"} for u in usage)


def test_schedule_stops_at_the_first_error_with_no_retry(env):
    ready(env)
    api = FakeApi(fail_on=2)
    code, s = yt.schedule(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_ERROR
    assert api.calls.count("upload") == 2
    items = state(env)["items"]
    failed = items["C-002-your-ai-doesnt-read-letters"]
    assert failed["status"] == yt.ATTEMPTING and "boom" in failed["error"]
    rest = [items[n] for n in sorted(LAYOUTS, key=yt.c_number)[2:]]
    assert all(r["status"] == yt.QUEUED and r["publish_at"] for r in rest)
    # The unresolved attempt blocks the next run before any Google call.
    api2 = FakeApi()
    code, s = yt.schedule(PROFILE, now=NOW, api=api2, out=env["out"])
    assert code == yt.EXIT_REFUSED and "not resolved: C-002" in s["refused"]
    assert api2.calls == []


def test_a_quota_error_from_google_leaves_the_item_queued(env):
    ready(env)
    api = FakeApi(fail_on=1, quota_fail=True)
    code, s = yt.schedule(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_ERROR
    rec = state(env)["items"]["C-001-where-did-my-rule-go"]
    assert rec["status"] == yt.QUEUED and rec["publish_at"] == "2026-11-23T20:00:00Z"


def test_the_attempting_record_is_on_disk_before_the_upload_call(env):
    ready(env)
    api = FakeApi()
    seen = []
    api.during_upload = lambda: seen.append(
        [r["id"] for r in yt.load_state(PROFILE)["items"].values()
         if r["status"] == yt.ATTEMPTING])
    yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    assert seen == [["C-001-where-did-my-rule-go"]]


def test_quota_ledger_refuses_before_an_upload_that_would_exceed_it(env):
    ready(env)
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["quota"]["daily_limit"] = 4000
    api = FakeApi()
    code, s = yt.schedule(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_ERROR and s["stopped"]["quota"] is True
    assert api.calls.count("upload") == 2  # 1 + 1600 + 1600 = 3201; a third would be 4801
    assert yt.quota_used(yt.load_quota(PROFILE), "2026-11-22") == 3201
    # Already over: refused before even the token refresh.
    api2 = FakeApi()
    code, s = yt.schedule(PROFILE, now=NOW, api=api2, out=env["out"])
    assert code == yt.EXIT_REFUSED and "quota" in s["refused"] and api2.calls == []
    # The next Pacific day is a fresh ledger day.
    tomorrow = datetime(2026, 11, 23, 8, 1, tzinfo=UTC)  # 00:01 PT
    assert yt.quota_day(tomorrow) == "2026-11-23"
    assert yt.quota_day(datetime(2026, 11, 23, 7, 59, tzinfo=UTC)) == "2026-11-22"


def test_an_item_whose_date_is_too_near_is_skipped_and_left_queued(env):
    ready(env)
    st = state(env)
    st["items"]["C-001-where-did-my-rule-go"]["publish_at"] = "2026-11-22T23:10:00Z"
    yt.save_state(PROFILE, st)
    api = FakeApi()
    code, s = yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    assert s["skipped"][0][0] == "C-001-where-did-my-rule-go"
    rec = state(env)["items"]["C-001-where-did-my-rule-go"]
    assert rec["status"] == yt.QUEUED and rec["publish_at"] == "2026-11-22T23:10:00Z"
    assert [b["status"]["publishAt"] for _m, b in api.uploads] == ["2026-11-23T20:00:00Z"]


def test_schedule_refuses_a_token_for_another_channel(env):
    ready(env)
    api = FakeApi(channel="UC_someone_else")
    code, s = yt.schedule(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_REFUSED and "UC_someone_else" in s["refused"]
    assert "upload" not in api.calls


def test_schedule_dry_run_writes_nothing_and_calls_nothing(env):
    ready(env)
    before = without_locks(tree(env["data_root"]))
    api = FakeApi()
    code, s = yt.schedule(PROFILE, dry_run=True, now=NOW, api=api, out=env["out"])
    assert code == 0 and api.calls == []
    assert sum("WOULD UPLOAD" in x for x in env["lines"]) == 6  # 1 + 6 * 1600 <= 10000
    assert without_locks(tree(env["data_root"])) == before
    assert not os.path.exists(yt.USAGE_LOG)


def test_the_production_gate_refuses_a_declared_production_channel(env, monkeypatch):
    ready(env)
    monkeypatch.setenv("PRODUCTION_IDENTITY_SLUGS", CHANNEL_ID)
    for run in (lambda: yt.schedule(PROFILE, now=NOW, api=FakeApi(), out=env["out"]),
                lambda: yt.status(PROFILE, now=NOW, out=env["out"]),
                lambda: yt.cutover(PROFILE, dry_run=True, now=NOW, out=env["out"])):
        assert run()[0] == yt.EXIT_PRODUCTION_REFUSED


# ── the channel lock, both ways ─────────────────────────────────────────────

def test_a_held_channel_lock_refuses_every_command(env):
    ready(env)
    api = FakeApi()
    with publisher_lock.channel_lock(yt.state_path(PROFILE), "someone else"):
        for run in (lambda: yt.schedule(PROFILE, now=NOW, api=api, out=env["out"]),
                    lambda: yt.intake(PROFILE, now=NOW, out=env["out"]),
                    lambda: yt.status(PROFILE, now=NOW, out=env["out"]),
                    lambda: yt.import_history(PROFILE, now=NOW, out=env["out"]),
                    lambda: yt.cutover(PROFILE, dry_run=True, now=NOW, out=env["out"])):
            code, s = run()
            assert code == yt.EXIT_REFUSED and "someone else" in s["refused"]
    assert api.calls == []


def test_an_upload_run_holds_the_lock_against_a_second_publisher(env):
    ready(env)
    api = FakeApi()
    second = []

    def try_second():
        try:
            with publisher_lock.channel_lock(yt.state_path(PROFILE), "second"):
                second.append("got it")
        except publisher_lock.PublisherBusy as exc:
            second.append(str(exc))
    api.during_upload = try_second
    yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    assert len(second) == 1 and "youtube-schedule topfive" in second[0]


# ── atomic writes ───────────────────────────────────────────────────────────

def test_a_failed_write_leaves_the_old_file_whole(env, tmp_path):
    path = str(tmp_path / "s.json")
    yt.write_json_atomic(path, {"a": 1})
    with pytest.raises(TypeError):
        yt.write_json_atomic(path, {"a": object()})
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"a": 1}


def test_writes_go_through_a_temp_file_and_a_rename(env, tmp_path, monkeypatch):
    calls = []
    real = os.replace
    monkeypatch.setattr(yt.os, "replace", lambda a, b: calls.append((a, b)) or real(a, b))
    path = str(tmp_path / "s.json")
    yt.write_json_atomic(path, {"a": 1})
    assert calls == [(path + ".tmp", path)]


def test_an_unreadable_state_refuses_rather_than_reading_as_empty(env):
    os.makedirs(yt.channel_dir(PROFILE))
    with open(yt.state_path(PROFILE), "w", encoding="utf-8") as f:
        f.write("{not json")
    code, s = yt.intake(PROFILE, now=NOW, out=env["out"])
    assert code == yt.EXIT_REFUSED and "unreadable" in s["refused"]


# ── V1-M import ─────────────────────────────────────────────────────────────

def test_import_maps_video_publisher_history(env):
    code, s = yt.import_history(PROFILE, now=NOW, out=env["out"])
    assert code == 0 and s["unmatched"] == []
    items = state(env)["items"]
    assert set(items) == {"C-010-you-know-ai", "C-001-where-did-my-rule-go",
                          "C-002-your-ai-doesnt-read-letters", "C-012-ask-first-accept-later",
                          "C-013-grade-it-like-a-new-hire", "aif-c01-001"}
    live = items["C-010-you-know-ai"]
    assert (live["status"], live["video_id"], live["publish_at"]) == (yt.UPLOADED, "LIVE10", None)
    assert yt.display_state(live, NOW) == "live"
    sched = items["C-001-where-did-my-rule-go"]
    assert (sched["video_id"], sched["publish_at"]) == ("SCHED01", "2026-11-23T20:00:00Z")
    assert yt.display_state(sched, NOW) == "scheduled"
    q = items["C-012-ask-first-accept-later"]
    assert (q["status"], q["publish_at"]) == (yt.QUEUED, "2026-11-29T20:00:00Z")
    assert q["media_path"].endswith("C-012-ask-first-accept-later-vertical.mp4")
    aif = items["aif-c01-001"]
    assert (aif["status"], aif["video_id"]) == (yt.UPLOADED, "AIF001")
    assert not any(k.endswith("-ig") for k in items)


def test_import_is_idempotent(env):
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    first = json.dumps(state(env), sort_keys=True)
    code, s = yt.import_history(PROFILE, now=NOW + timedelta(hours=2), out=env["out"])
    assert s["counts"] == {"unchanged": 6}
    assert json.dumps(state(env), sort_keys=True) == first


def test_a_later_upload_by_video_publisher_updates_the_queued_record(env):
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    conn = sqlite3.connect(os.path.join(env["vp_root"], "data", "publish_log.db"))
    conn.execute("INSERT INTO publish_log (id, brand, platform, content_folder, status, "
                 "platform_response, updated_at, publish_at) VALUES (20, 'aws', 'youtube', "
                 "'aws/queue/C-012-ask-first-accept-later-yt', 'published', ?, "
                 "'2026-11-23 18:00:00', '2026-11-29T20:00:00Z')", (resp("NEW12"),))
    conn.commit()
    conn.close()
    code, s = yt.import_history(PROFILE, now=NOW, out=env["out"])
    assert s["counts"].get("updated") == 1
    rec = state(env)["items"]["C-012-ask-first-accept-later"]
    assert (rec["status"], rec["video_id"], rec["publish_at"]) == (
        yt.UPLOADED, "NEW12", "2026-11-29T20:00:00Z")
    assert len(state(env)["items"]) == 6


def test_import_never_overwrites_a_different_video_id(env):
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    st = state(env)
    st["items"]["C-001-where-did-my-rule-go"].update(video_id="OURS", origin="intake")
    yt.save_state(PROFILE, st)
    code, s = yt.import_history(PROFILE, now=NOW, out=env["out"])
    assert code == yt.EXIT_ERROR and s["counts"]["conflict"] == 1
    assert state(env)["items"]["C-001-where-did-my-rule-go"]["video_id"] == "OURS"


def test_import_keeps_an_existing_date(env):
    yt.intake(PROFILE, now=NOW, out=env["out"])
    st = state(env)
    st["items"]["C-012-ask-first-accept-later"]["publish_at"] = "2026-12-01T20:00:00Z"
    yt.save_state(PROFILE, st)
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    assert state(env)["items"]["C-012-ask-first-accept-later"]["publish_at"] == \
        "2026-12-01T20:00:00Z"
    assert any("dates differ" in x for x in env["lines"])


def test_import_lists_a_published_row_with_no_video_id_and_imports_the_rest(env):
    conn = sqlite3.connect(os.path.join(env["vp_root"], "data", "publish_log.db"))
    conn.execute("UPDATE publish_log SET platform_response = '{}' WHERE id = 2")
    conn.commit()
    conn.close()
    code, s = yt.import_history(PROFILE, now=NOW, out=env["out"])
    assert [n for n, _ in s["unmatched"]] == ["C-010-you-know-ai-yt"]
    assert len(state(env)["items"]) == 5


def test_import_dry_run_writes_nothing_and_leaves_video_publisher_alone(env):
    before_data = without_locks(tree(env["data_root"]))
    before_vp = tree(env["vp_root"])
    code, s = yt.import_history(PROFILE, dry_run=True, now=NOW, out=env["out"])
    assert s["counts"] == {"created": 6}
    assert without_locks(tree(env["data_root"])) == before_data
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    assert tree(env["vp_root"]) == before_vp


# ── cutover ─────────────────────────────────────────────────────────────────

def test_cutover_refuses_without_a_token_and_writes_nothing(env):
    before = without_locks(tree(env["data_root"]))
    code, s = yt.cutover(PROFILE, now=NOW, out=env["out"])
    assert code == yt.EXIT_REFUSED and "no OAuth token" in s["blockers"][0]
    assert not os.path.exists(yt.marker_path(_cfg(env)))
    assert without_locks(tree(env["data_root"])) == before


def test_cutover_refuses_while_video_publisher_is_attempting(env):
    give_token(env)
    conn = sqlite3.connect(os.path.join(env["vp_root"], "data", "publish_log.db"))
    conn.execute("INSERT INTO publish_log (id, brand, platform, content_folder, status, "
                 "updated_at) VALUES (30, 'aws', 'youtube', "
                 "'aws/queue/C-012-ask-first-accept-later-yt', 'attempting', "
                 "'2026-11-23 18:00:00')")
    conn.commit()
    conn.close()
    code, s = yt.cutover(PROFILE, now=NOW, out=env["out"])
    assert code == yt.EXIT_REFUSED
    assert s["blockers"] == ["C-012-ask-first-accept-later-yt: video-publisher has it "
                             "attempting (publish_log row 30)"]
    assert not os.path.exists(yt.marker_path(_cfg(env)))


def test_cutover_refuses_an_undated_queued_item_video_publisher_still_means_to_upload(env):
    give_token(env)
    vp_write(env["vp_root"], "queue", "C-014-new-yt", vp_manifest("C-014"))
    code, s = yt.cutover(PROFILE, now=NOW, out=env["out"])
    assert code == yt.EXIT_REFUSED
    assert s["blockers"] == ["C-014-new-yt: queued in video-publisher with no publish_at, "
                             "not yet uploaded"]


def test_cutover_with_an_import_conflict_writes_nothing(env):
    give_token(env)
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    st = state(env)
    st["items"]["C-001-where-did-my-rule-go"].update(video_id="OURS", origin="intake")
    st["items"].pop("C-012-ask-first-accept-later")  # the import would re-create it
    yt.save_state(PROFILE, st)
    before = without_locks(tree(env["data_root"]))
    code, s = yt.cutover(PROFILE, now=NOW, out=env["out"])
    assert code == yt.EXIT_REFUSED and "conflicts" in s["refused"]
    assert without_locks(tree(env["data_root"])) == before
    assert not os.path.exists(yt.marker_path(_cfg(env)))


def test_cutover_dry_run_writes_nothing(env):
    give_token(env)
    before_data = without_locks(tree(env["data_root"]))
    before_vp = tree(env["vp_root"])
    code, s = yt.cutover(PROFILE, dry_run=True, now=NOW, out=env["out"])
    assert code == 0 and s["blockers"] == []
    assert any("would add aws" in x for x in env["lines"])
    assert without_locks(tree(env["data_root"])) == before_data
    assert tree(env["vp_root"]) == before_vp


def test_cutover_imports_marks_and_lets_schedule_run(env):
    give_token(env)
    code, s = yt.cutover(PROFILE, now=NOW, out=env["out"])
    assert code == 0
    assert yt.read_marker(yt.marker_path(_cfg(env))) == {"aws"}
    assert len(state(env)["items"]) == 6
    assert env["lines"][0].startswith("BEFORE: marker")
    assert env["lines"][-1].startswith("AFTER: marker lists aws")
    # The marker file is what video-publisher's src/moved.py reads.
    with open(yt.marker_path(_cfg(env)), encoding="utf-8") as f:
        assert yaml.safe_load(f) == {"brands": ["aws"]}
    yt.intake(PROFILE, now=NOW, out=env["out"])
    api = FakeApi()
    code, s = yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    assert code == 0 and len(api.uploads) == 1
    # Imported dates are held: C-003 gets the first day no one holds (11-25).
    assert state(env)["items"]["C-003-it-sounded-so-sure"]["publish_at"] == "2026-11-25T20:00:00Z"
    # A second cutover is a no-op for the marker.
    code, s = yt.cutover(PROFILE, now=NOW, out=env["out"])
    assert code == 0 and any("already lists aws" in x for x in env["lines"])


# ── status ──────────────────────────────────────────────────────────────────

def test_status_is_read_only(env):
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    before = without_locks(tree(env["data_root"]))
    code, s = yt.status(PROFILE, now=NOW, out=env["out"])
    assert code == 0
    assert s["counts"] == {"live": 2, "scheduled": 2, "queued": 2}
    assert "token missing" in env["lines"][-1]
    assert without_locks(tree(env["data_root"])) == before


# ── auth ────────────────────────────────────────────────────────────────────

class _Creds:
    client_id = "cid"
    client_secret = "SECRET-xyz"
    refresh_token = "REFRESH-abc"
    token_uri = youtube_api.TOKEN_URI


def _auth(env, channel=CHANNEL_ID, creds=None):
    import youtube_auth
    api = FakeApi(channel=channel)
    api.run_consent = lambda path: creds or _Creds()
    secrets = env["tmp"] / "client_secret.json"
    secrets.write_text("{}", encoding="utf-8")
    lines = []
    code = youtube_auth.run(PROFILE, str(secrets), api=api, out=lines.append, now=NOW)
    return code, lines


def test_auth_stores_the_token_for_the_right_channel_and_prints_no_secret(env):
    code, lines = _auth(env)
    assert code == 0
    with open(yt.token_path(PROFILE), encoding="utf-8") as f:
        token = json.load(f)
    assert token["refresh_token"] == "REFRESH-abc" and token["channel_id"] == CHANNEL_ID
    assert token["scopes"] == youtube_api.SCOPES
    text = "\n".join(lines)
    assert "SECRET" not in text and "REFRESH" not in text
    assert yt.token_problem(PROFILE) is None


def test_auth_refuses_another_channel_and_stores_nothing(env):
    code, lines = _auth(env, channel="UC_someone_else")
    assert code == yt.EXIT_REFUSED and "UC_someone_else" in lines[-1]
    assert not os.path.exists(yt.token_path(PROFILE))


def test_auth_refuses_when_google_gives_no_refresh_token(env):
    class NoRefresh(_Creds):
        refresh_token = None
    code, lines = _auth(env, creds=NoRefresh())
    assert code == yt.EXIT_REFUSED and not os.path.exists(yt.token_path(PROFILE))


def test_the_scopes_are_upload_and_youtube():
    assert youtube_api.SCOPES == ["https://www.googleapis.com/auth/youtube.upload",
                                  "https://www.googleapis.com/auth/youtube"]


# ── the Google boundary ─────────────────────────────────────────────────────

def test_credentials_from_token_refuses_a_partial_token():
    with pytest.raises(youtube_api.YouTubeApiError, match="refresh_token"):
        youtube_api.credentials_from_token({"client_id": "a", "client_secret": "b"})


def test_upload_makes_one_insert_with_no_retry(tmp_path, monkeypatch):
    from googleapiclient.errors import HttpError

    class Resp(dict):
        status = 503
        reason = "x"
    media = tmp_path / "m.mp4"
    media.write_bytes(b"x")
    attempts = []

    class Req:
        def next_chunk(self):
            attempts.append(1)
            raise HttpError(Resp(), b'{"error": {"message": "busy"}}')

    class Videos:
        def insert(self, **kw):
            assert kw["part"] == "snippet,status"
            return Req()

    class Service:
        def videos(self):
            return Videos()
    with pytest.raises(youtube_api.YouTubeApiError, match="HTTP 503"):
        youtube_api.upload(Service(), media, {"snippet": {}, "status": {}})
    assert attempts == [1]


def test_a_quota_reason_is_classified_as_quota():
    from googleapiclient.errors import HttpError

    class Resp(dict):
        status = 403
        reason = "x"
    body = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}],
                                 "message": "q"}}).encode()
    err = youtube_api._error(HttpError(Resp(), body), "videos.insert")
    assert err.quota is True
    body = json.dumps({"error": {"errors": [{"reason": "invalidDescription"}],
                                 "message": "d"}}).encode()
    assert youtube_api._error(HttpError(Resp(), body), "videos.insert").quota is False


# ── calendar source ─────────────────────────────────────────────────────────

def test_calendar_items_on_their_pacific_day(env):
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    items = {i["key"]: i for i in yt.calendar_items(PROFILE, now=NOW)}
    sched = items["C-001-where-did-my-rule-go"]
    assert (sched["day"], sched["time_label"], sched["group"], sched["platform_label"]) == (
        "2026-11-23", "12:00 PT", "scheduled_youtube", "YouTube")
    assert "Scheduled on YouTube" in sched["preview"]
    live = items["C-010-you-know-ai"]
    assert (live["group"], live["day"], live["time_label"]) == ("sent", "2026-11-22", "11:00 PT")
    assert items["C-012-ask-first-accept-later"]["group"] == "queued"
    assert items["C-012-ask-first-accept-later"]["day"] == "2026-11-29"
    later = {i["key"]: i for i in yt.calendar_items(
        PROFILE, now=datetime(2026, 11, 24, 0, 0, tzinfo=UTC))}
    assert later["C-001-where-did-my-rule-go"]["group"] == "sent"


def test_calendar_source_with_no_state_is_empty_and_creates_nothing(env):
    assert yt.calendar_items(PROFILE, now=NOW) == []
    assert not os.path.exists(yt.channel_dir(PROFILE))


def test_the_calendar_route_shows_youtube_items(env, monkeypatch):
    from linkedin_automation import dashboard
    yt.import_history(PROFILE, now=NOW, out=env["out"])
    monkeypatch.setattr(content_calendar.pm, "DATA_ROOT", env["data_root"])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    res = client.get("/api/scheduled/%s/calendar?from=2026-11-20&to=2026-11-30" % PROFILE)
    assert res.status_code == 200
    body = res.get_json()
    keys = [(i["platform"], i["key"], i["day"]) for i in body["items"]]
    assert ("youtube", "C-001-where-did-my-rule-go", "2026-11-23") in keys
    assert ("youtube", "C-013-grade-it-like-a-new-hire", "2026-11-30") in keys
    assert body["groups"]["scheduled_youtube"] == "Scheduled on YouTube"
    res = client.get("/api/scheduled/%s/calendar?from=2026-11-20&to=2026-11-30"
                     "&platform=youtube" % PROFILE)
    assert res.status_code == 200
    assert {i["platform"] for i in res.get_json()["items"]} == {"youtube"}


# ── the CLI ─────────────────────────────────────────────────────────────────

def test_the_cli_routes_each_youtube_command(env, monkeypatch, capsys):
    import run_scheduled_posts as cli
    seen = []
    monkeypatch.setattr(cli.yt, "schedule",
                        lambda p, **kw: seen.append(("schedule", p, kw)) or (0, {}))
    monkeypatch.setattr(cli.yt, "status", lambda p: seen.append(("status", p)) or (0, {}))
    for name in ("intake", "import_history", "cutover"):
        monkeypatch.setattr(cli.yt, name,
                            lambda p, _n=name, **kw: seen.append((_n, p, kw)) or (0, {}))
    for argv in (["youtube-schedule", "--max-uploads", "2", "--dry-run"],
                 ["youtube-status"], ["youtube-intake"], ["youtube-import", "--dry-run"],
                 ["youtube-cutover", "--dry-run"]):
        monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py"] + argv
                            + ["--profile", PROFILE])
        assert cli.main() == 0
    assert seen == [("schedule", PROFILE, {"max_uploads": 2, "dry_run": True}),
                    ("status", PROFILE),
                    ("intake", PROFILE, {"dry_run": False}),
                    ("import_history", PROFILE, {"dry_run": True}),
                    ("cutover", PROFILE, {"dry_run": True})]
