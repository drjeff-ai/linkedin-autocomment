"""Y1 (D-027): YouTube commenting through the API, under DEC-061's pins.

Google is mocked everywhere through a fake of ``youtube_api``'s functions; the
suite's ``_no_real_network`` guard stays on, and ``test_network_guard.py``
proves a real call fails. The clock is frozen (``NOW``) and the gap's RNG is
seeded. Every fake Google call asserts that its quota charge and its paid-call
line were written BEFORE it ran.
"""

import json
import os
import random
from datetime import datetime, timedelta, timezone

import pytest

from linkedin_automation import platform_policy as pp
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import youtube_api
from linkedin_automation import youtube_channel as yt
from linkedin_automation import youtube_engage as ye

PROFILE = "topfive"
OURS = "UC" + "o" * 22
CREATOR_A = "UC" + "a" * 22
CREATOR_B = "UC" + "b" * 22
UTC = timezone.utc
PT = yt.PACIFIC
#: Monday 2026-11-23 10:00 Pacific.
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=PT).astimezone(UTC)


def pt(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=PT).astimezone(UTC)


# ── the environment ─────────────────────────────────────────────────────────

def overlay(tmp, **eng):
    engagement = {"channel_name": "Top Five AI Tech", "channel_handle": "@topfiveaitech",
                  "watch_list": [{"channel_id": CREATOR_A, "title": "Creator A",
                                  "subscribers": 1200, "added_at": "2026-11-20T00:00:00Z"}]}
    engagement.update(eng)
    return {"vp_brand": "aws", "vp_root": str(tmp / "vp"), "channel_id": OURS,
            "content_root": str(tmp / "content"), "publish_time": "12:00",
            "tz": "America/Los_Angeles", "per_day": 1,
            "quota": {"daily_limit": 10000, "upload_units": 1600, "comment_units": 50},
            "engagement": engagement}


def write_config(cfg):
    path = pm.get_config_path(PROFILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "load_profiles",
                        lambda: {"profiles": {PROFILE: {}}, "default": None})
    usage = tmp_path / "api_usage.jsonl"
    monkeypatch.setattr(yt, "USAGE_LOG", str(usage))
    e = {"tmp": tmp_path, "usage": usage, "lines": []}
    e["cfg"] = {"scheduled_posting": {"platforms": {"youtube": overlay(tmp_path)}}}
    write_config(e["cfg"])
    yt.save_token(PROFILE, {"client_id": "cid", "client_secret": "SECRET-xyz",
                            "refresh_token": "REFRESH-abc", "channel_id": OURS})
    e["out"] = e["lines"].append
    return e


def quota_entries():
    if not os.path.exists(yt.quota_path(PROFILE)):
        return []
    led = yt.load_quota(PROFILE)
    return [x for d in led["days"].values() for x in d["entries"]]


def usage_lines(env):
    if not env["usage"].exists():
        return []
    return [json.loads(x) for x in env["usage"].read_text(encoding="utf-8").splitlines()]


class FakeApi:
    """Stands in for youtube_api. Every call checks the ledgers were written
    first, and is recorded."""

    YouTubeApiError = youtube_api.YouTubeApiError

    def __init__(self, env, channel=OURS):
        self.env = env
        self.channel = channel
        self.calls = []
        self.search = [{"channel_id": CREATOR_B, "title": "Creator B", "description": "d"},
                       {"channel_id": OURS, "title": "Us", "description": ""}]
        self.info = {CREATOR_A: {"title": "Creator A", "subscribers": 1200,
                                 "uploads_playlist": "UU" + "a" * 22},
                     CREATOR_B: {"title": "Creator B", "subscribers": None,
                                 "uploads_playlist": "UU" + "b" * 22}}
        self.playlist = {"UU" + "a" * 22: [[
            video("vidNEW1", days_ago=1), video("vidNEW2", days_ago=6),
            video("vidOLD", days_ago=8)]]}
        self.threads_related = [
            thread("cmtVIEWER", "UCviewer", days_ago=1),
            thread("cmtOURS", OURS, days_ago=1),
            thread("cmtREPLIED", "UCviewer2", days_ago=1, replies=[OURS]),
            thread("cmtOLD", "UCviewer3", days_ago=9)]
        #: video id -> pages of threads (dedupe and verify read these)
        self.video_threads = {}
        self.replies = {}
        self.top = {}
        self.titles = {}
        self.insert_error = None
        self.insert_id = "newCOMMENT1"
        self.listed_after_insert = True
        self.before_insert = None
        self.on_dedupe = None
        self.inserts = []

    def _check(self, op_prefix):
        q = quota_entries()
        assert q and q[-1]["op"].startswith(op_prefix), (op_prefix, q[-1:] if q else q)
        u = usage_lines(self.env)
        assert u and u[-1]["endpoint"].startswith(op_prefix), (op_prefix, u[-1:])
        assert u[-1]["api"] == "youtube"

    def credentials_from_token(self, token):
        self.calls.append("creds")
        return object()

    def refresh(self, creds):
        self.calls.append("refresh")

    def build_service(self, creds):
        self.calls.append("build")
        return "service"

    def my_channel_id(self, service):
        self._check("channels.list mine")
        self.calls.append("channels.list mine")
        return self.channel

    def search_channels(self, service, query, max_results=10):
        self._check("search.list")
        self.calls.append("search.list")
        return list(self.search)

    def channels_info(self, service, ids):
        self._check("channels.list")
        self.calls.append("channels.list")
        return {i: self.info[i] for i in ids if i in self.info}

    def playlist_page(self, service, playlist_id, page_token=None):
        self._check("playlistItems.list")
        self.calls.append("playlistItems.list")
        pages = self.playlist.get(playlist_id) or [[]]
        i = int(page_token or 0)
        nxt = str(i + 1) if i + 1 < len(pages) else None
        return [dict(v) for v in pages[i]], nxt

    def comment_threads_page(self, service, video_id=None, related_channel_id=None,
                             page_token=None, order="time", max_results=100):
        self._check("commentThreads.list")
        if related_channel_id:
            self.calls.append("commentThreads.list related")
            assert related_channel_id == OURS
            return [dict(t) for t in self.threads_related], None
        if order == "relevance":
            self.calls.append("commentThreads.list relevance")
            return [{"text": t} for t in self.top.get(video_id, [])], None
        if self.on_dedupe and not self.inserts:
            self.on_dedupe()
        self.calls.append("commentThreads.list %s" % video_id)
        pages = list(self.video_threads.get(video_id) or [[]])
        if self.inserts and self.listed_after_insert:
            pages[0] = pages[0] + [{"comment_id": self.insert_id, "thread_id": self.insert_id,
                                    "author_channel_id": OURS}]
        i = int(page_token or 0)
        nxt = str(i + 1) if i + 1 < len(pages) else None
        return pages[i], nxt

    def replies_page(self, service, parent_id, page_token=None):
        self._check("comments.list")
        if self.on_dedupe and not self.inserts:
            self.on_dedupe()
        self.calls.append("comments.list %s" % parent_id)
        items = list(self.replies.get(parent_id) or [])
        if self.inserts and self.listed_after_insert:
            items.append({"id": self.insert_id, "author_channel_id": OURS})
        return items, None

    def video_titles(self, service, ids):
        self._check("videos.list")
        self.calls.append("videos.list")
        return {i: self.titles.get(i, "") for i in ids}

    def _insert(self, op, *args):
        self._check(op)
        if self.before_insert:
            self.before_insert()
        self.calls.append(op)
        self.inserts.append(args)
        if self.insert_error:
            raise self.insert_error
        return self.insert_id

    def insert_thread(self, service, channel_id, video_id, text):
        assert channel_id == OURS
        return self._insert("commentThreads.insert", video_id, text)

    def insert_reply(self, service, parent_id, text):
        return self._insert("comments.insert", parent_id, text)


def video(vid, days_ago, title=None, description="A synthetic description.",
          channel=CREATOR_A):
    return {"video_id": vid, "title": title or "Synthetic video %s" % vid,
            "description": description,
            "published_at": yt.utc_z(NOW - timedelta(days=days_ago)),
            "channel_id": channel, "channel_title": "Creator A"}


def thread(cid, author, days_ago, replies=(), video_id="ourVID1"):
    return {"thread_id": cid, "comment_id": cid, "video_id": video_id,
            "author_channel_id": author, "author": "Viewer %s" % cid,
            "text": "a synthetic viewer comment", "published_at":
            yt.utc_z(NOW - timedelta(days=days_ago)), "like_count": 0,
            "total_reply_count": len(replies),
            "replies": [{"id": "r%d" % i, "author_channel_id": a, "text": "x"}
                        for i, a in enumerate(replies)]}


class FakeGenerator:
    def __init__(self, profile, variant, text="the eval drift point is the useful bit"):
        self.variant = variant
        self.text = text
        self.posts = []

    def generate_comment(self, post, evaluation):
        self.posts.append(post)
        return {"comment": self.text, "style": "add_insight", "approach": "a",
                "word_count": len(self.text.split()), "issues": []}


def store():
    return ye.load_store(PROFILE)


def seed(key="vidNEW1", kind=ye.KIND_VIDEO, comment="the eval drift point is the useful bit",
         approve=True, creator=CREATOR_A, status=post_store.GENERATED):
    """A drafted record, approved through the real PostStore.approve."""
    s = store()
    if kind == ye.KIND_VIDEO:
        rec = ye._video_record(video(key, 1, channel=creator), NOW)
    else:
        rec = ye._reply_record(thread(key, "UCviewer", 1), NOW)
    s.posts[key] = rec
    s.mark_generated(key, comment, {})
    if approve:
        assert s.approve(key)
    s.posts[key]["status"] = status
    s.save()
    return key


def ledger_with(*entries, **fields):
    led = ye.empty_ledger()
    led["comments"] = list(entries)
    led.update(fields)
    ye.save_ledger(PROFILE, led)
    return led


def live(at, kind=ye.KIND_VIDEO, creator=CREATOR_B, key="other", video_id="otherVID"):
    return {"at": yt.utc_z(at), "key": key, "kind": kind, "video_id": video_id,
            "creator": creator if kind == ye.KIND_VIDEO else None,
            "outcome": ye.POSTED_VISIBLE, "comment_id": "c-" + key, "live": True}


def engage(env, api=None, now=NOW, dry_run=False, seed_rng=7):
    api = api or FakeApi(env)
    code, summary = ye.engage(PROFILE, dry_run=dry_run, now=now, api=api,
                              rng=random.Random(seed_rng), out=env["out"])
    return code, summary, api


def inserted(api):
    return [c for c in api.calls if c.endswith(".insert")]


# ── config and the watch list ───────────────────────────────────────────────

def test_config_reads_the_engagement_block(env):
    cfg = ye.engage_config(PROFILE)
    assert cfg.channel_id == OURS and cfg.channel_handle == "@topfiveaitech"
    assert [e["channel_id"] for e in cfg.watch_list] == [CREATOR_A]


@pytest.mark.parametrize("bad,match", [
    ({"surprise": 1}, "unknown keys: surprise"),
    ({"watch_list": [{"channel_id": CREATOR_A, "x": 1}]}, "unknown keys: x"),
    ({"watch_list": [{"channel_id": "not-a-channel"}]}, "not a channel id"),
    ({"watch_list": [{"channel_id": CREATOR_A}, {"channel_id": CREATOR_A}]}, "twice"),
])
def test_bad_engagement_config_is_refused(env, bad, match):
    o = overlay(env["tmp"])
    o["engagement"].update(bad)
    write_config({"scheduled_posting": {"platforms": {"youtube": o}}})
    with pytest.raises(yt.YouTubeRefused, match=match):
        ye.engage_config(PROFILE)


def test_v1_still_refuses_an_unknown_overlay_key(env):
    o = overlay(env["tmp"])
    o["comments"] = {}
    with pytest.raises(yt.YouTubeRefused, match="unknown keys: comments"):
        yt.youtube_config(PROFILE, {"scheduled_posting": {"platforms": {"youtube": o}}})


def test_add_and_remove_on_the_watch_list(env):
    assert ye.add_to_watch_list(PROFILE, CREATOR_B, title="Creator B", subscribers=5,
                                now=NOW) is True
    assert ye.add_to_watch_list(PROFILE, CREATOR_B, now=NOW) is False
    ids = [e["channel_id"] for e in ye.engage_config(PROFILE).watch_list]
    assert ids == [CREATOR_A, CREATOR_B]
    entry = ye.engage_config(PROFILE).watch_list[1]
    assert entry == {"channel_id": CREATOR_B, "title": "Creator B", "subscribers": 5,
                     "added_at": yt.utc_z(NOW)}
    assert ye.remove_from_watch_list(PROFILE, CREATOR_A) is True
    assert ye.remove_from_watch_list(PROFILE, CREATOR_A) is False
    assert [e["channel_id"] for e in ye.engage_config(PROFILE).watch_list] == [CREATOR_B]
    with pytest.raises(yt.YouTubeRefused, match="not a channel id"):
        ye.add_to_watch_list(PROFILE, "@somebody")


# ── discover adds nothing ───────────────────────────────────────────────────

def test_discover_suggests_and_adds_nothing(env):
    before = open(pm.get_config_path(PROFILE), "rb").read()
    api = FakeApi(env)
    code, summary = ye.discover(PROFILE, "llm evals", now=NOW, api=api, out=env["out"])
    assert code == 0
    assert [s["channel_id"] for s in summary["suggestions"]] == [CREATOR_B]  # not us
    assert summary["suggestions"][0]["subscribers"] is None
    assert open(pm.get_config_path(PROFILE), "rb").read() == before
    assert not os.path.exists(post_store.PostStore._default_path(PROFILE, "youtube"))
    assert [(q["op"], q["units"]) for q in quota_entries()] == [
        ("search.list", 100), ("channels.list", 1)]
    assert "nothing was added" in env["lines"][-1]


def test_discover_marks_channels_already_listed(env):
    api = FakeApi(env)
    api.search = [{"channel_id": CREATOR_A, "title": "Creator A", "description": ""}]
    _c, summary = ye.discover(PROFILE, "q", now=NOW, api=api, out=env["out"])
    assert summary["suggestions"][0]["on_watch_list"] is True


def test_discover_dry_run_calls_nothing(env):
    api = FakeApi(env)
    code, _s = ye.discover(PROFILE, "q", dry_run=True, now=NOW, api=api, out=env["out"])
    assert code == 0 and api.calls == [] and quota_entries() == []


def test_discover_refuses_without_quota(env):
    led = yt.load_quota(PROFILE)
    led["days"][yt.quota_day(NOW)] = {"units": 9950, "entries": []}
    yt.write_json_atomic(yt.quota_path(PROFILE), led)
    api = FakeApi(env)
    code, _s = ye.discover(PROFILE, "q", now=NOW, api=api, out=env["out"])
    assert code == ye.EXIT_REFUSED and "search.list" not in api.calls


# ── find ────────────────────────────────────────────────────────────────────

def test_find_lands_recent_videos_and_unanswered_threads_as_new(env):
    api = FakeApi(env)
    code, summary = ye.find(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == 0
    assert summary["videos"] == ["vidNEW1", "vidNEW2"]
    assert summary["replies"] == ["cmtVIEWER"]
    s = store()
    assert {k: r["status"] for k, r in s.posts.items()} == {
        "vidNEW1": "NEW", "vidNEW2": "NEW", "cmtVIEWER": "NEW"}
    assert s.get("vidNEW1")["youtube"]["channel_id"] == CREATOR_A
    assert s.get("cmtVIEWER")["youtube"]["parent_id"] == "cmtVIEWER"
    assert [(q["op"], q["units"]) for q in quota_entries()] == [
        ("channels.list contentDetails", 1), ("playlistItems.list", 1),
        ("commentThreads.list allThreadsRelatedToChannelId", 1)]


def test_find_twice_adds_nothing_new_and_uses_the_cached_playlist(env):
    ye.find(PROFILE, now=NOW, api=FakeApi(env), out=env["out"])
    seed_key = "vidNEW1"
    s = store()
    s.reject(seed_key, save=True)  # whatever its state, a known key is never re-added
    api = FakeApi(env)
    _c, summary = ye.find(PROFILE, now=NOW, api=api, out=env["out"])
    assert summary["videos"] == [] and summary["replies"] == []
    assert "channels.list" not in api.calls
    assert store().get(seed_key)["status"] == post_store.TRASH


def test_find_skips_a_video_the_ledger_says_we_commented(env):
    ledger_with(live(NOW - timedelta(days=4), creator=CREATOR_A, key="vidNEW1",
                     video_id="vidNEW1"))
    _c, summary = ye.find(PROFILE, now=NOW, api=FakeApi(env), out=env["out"])
    assert summary["videos"] == ["vidNEW2"]


def test_find_dry_run_calls_nothing(env):
    api = FakeApi(env)
    assert ye.find(PROFILE, dry_run=True, now=NOW, api=api, out=env["out"])[0] == 0
    assert api.calls == [] and not store().posts


# ── draft ───────────────────────────────────────────────────────────────────

def test_draft_uses_title_description_chapters_and_top_comments(env):
    api = FakeApi(env)
    api.playlist["UU" + "a" * 22] = [[video(
        "vidCH", 1, description="About evals.\n0:00 Intro\n4:10 Side by side\nmore")]]
    api.threads_related = []
    ye.find(PROFILE, now=NOW, api=api, out=env["out"])
    api.top["vidCH"] = ["first top comment", "second\ntop comment"]
    gens = {}

    def make(profile, variant):
        gens[variant] = FakeGenerator(profile, variant)
        return gens[variant]
    code, summary = ye.draft(PROFILE, now=NOW, api=api, make_generator=make, out=env["out"])
    assert code == 0 and summary["drafted"] == ["vidCH"]
    text = gens[None].posts[0]["text"]
    assert text == ("Title: Synthetic video vidCH\n\nDescription:\nAbout evals.\n0:00 Intro"
                    "\n4:10 Side by side\nmore\n\nChapters:\n0:00 Intro\n4:10 Side by side"
                    "\n\nTop comments:\n- first top comment\n- second top comment")
    rec = store().get("vidCH")
    assert rec["status"] == post_store.GENERATED and not post_store.is_approved(rec)
    assert rec["comment_meta"]["promotion"] == []
    # Only API reads; the relevance listing is the only per-video call.
    assert api.calls.count("commentThreads.list relevance") == 1


def test_draft_caps_top_comments_at_20():
    rec = {"youtube": {"title": "t", "description": ""}}
    text = ye.video_input(rec, ["c%d" % i for i in range(30)])
    assert text.count("\n- c") == 20


def test_draft_of_a_reply_uses_the_reply_voice_and_our_local_title(env):
    api = FakeApi(env)
    api.playlist = {}
    ye.find(PROFILE, now=NOW, api=api, out=env["out"])
    yt.save_state(PROFILE, {"version": 1, "items": {"C-001": {
        "id": "C-001", "video_id": "ourVID1", "title": "Our synthetic short"}}})
    gens = {}

    def make(profile, variant):
        gens[variant] = FakeGenerator(profile, variant)
        return gens[variant]
    ye.draft(PROFILE, now=NOW, api=api, make_generator=make, out=env["out"])
    assert list(gens) == [pp.REPLY]
    assert gens[pp.REPLY].posts[0]["text"].startswith("Video: Our synthetic short")
    assert "videos.list" not in api.calls  # local first


def test_draft_flags_promotion_and_approval_is_refused_until_edited(env):
    api = FakeApi(env)
    api.threads_related = []
    ye.find(PROFILE, now=NOW, api=api, out=env["out"])
    ye.draft(PROFILE, limit=1, now=NOW, api=api, out=env["out"],
             make_generator=lambda p, v: FakeGenerator(p, v, "subscribe for more"))
    rec = store().get("vidNEW1")
    assert rec["status"] == post_store.GENERATED
    assert rec["comment_meta"]["promotion"] == ["subscribe"]
    assert ye.approve_refusal(PROFILE, rec, None) == "promotion: subscribe"
    assert ye.approve_refusal(PROFILE, rec, "the 4:10 comparison made it click") is None


# ── the approval gate: posts only via is_approved ───────────────────────────

def test_an_approved_draft_posts(env):
    seed()
    code, summary, api = engage(env)
    assert code == 0 and summary["outcome"] == ye.POSTED_VISIBLE
    assert inserted(api) == ["commentThreads.insert"]
    assert api.inserts == [("vidNEW1", "the eval drift point is the useful bit")]


def test_an_unapproved_draft_does_not_post(env):
    seed(approve=False)
    code, summary, api = engage(env)
    assert code == 0 and inserted(api) == [] and api.calls == []
    assert summary["not_approved"] == 1
    assert "0 posted: 1 not approved" in env["lines"][-1]


def test_a_draft_edited_after_approval_does_not_post(env):
    key = seed()
    s = store()
    s.posts[key]["comment"] = "an edit nobody approved"
    s.save()
    _c, summary, api = engage(env)
    assert inserted(api) == [] and summary["not_approved"] == 1


@pytest.mark.parametrize("status", [post_store.NEW, post_store.COMMENTED, post_store.TRASH])
def test_a_wrong_status_does_not_post(env, status):
    seed(status=status)
    _c, _s, api = engage(env)
    assert inserted(api) == []


def test_an_approval_withdrawn_mid_run_is_honoured_at_the_last_step(env):
    key = seed()
    api = FakeApi(env)

    def withdraw():
        s = store()
        s.save_edit(key, comment="edited while the run was in dedupe")
        s.save()
    api.on_dedupe = withdraw
    _c, summary, _a = engage(env, api)
    assert inserted(api) == []
    # save_edit withdrew the approval, so the fresh read says not_reviewed.
    assert any(post_store.REFUSE_NOT_REVIEWED in r for r in summary["refusals"])
    assert "0 posted: 1 not approved" in env["lines"][-1]


# ── DEC-061 pins ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("when,allowed", [
    (pt(23, 7, 59), False), (pt(23, 8, 0), True), (pt(23, 20, 59), True),
    (pt(23, 21, 0), False), (pt(23, 23, 30), False)])
def test_the_window_is_8_to_21_pacific(env, when, allowed):
    seed()
    _c, summary, api = engage(env, now=when)
    assert bool(inserted(api)) is allowed
    if not allowed:
        assert summary["refusals"][0].startswith(ye.REFUSE_WINDOW)
        nxt = yt.parse_instant(summary["next_allowed"]).astimezone(PT)
        assert (nxt.hour, nxt.minute) == (8, 0) and nxt > when


def test_the_gap_is_random_25_to_90_minutes_and_refuses_inside_it(env):
    seed("vidNEW1")
    engage(env)
    led = ye.load_ledger(PROFILE)
    gap = yt.parse_instant(led["next_allowed_at"]) - NOW
    expected = timedelta(minutes=random.Random(7).uniform(25, 90))
    # The ledger stores whole seconds.
    assert abs(gap - expected) < timedelta(seconds=1)
    assert timedelta(minutes=25) <= gap <= timedelta(minutes=90)
    seed("vidNEW2", creator=CREATOR_B)
    _c, summary, api = engage(env, now=NOW + gap - timedelta(seconds=1))
    assert inserted(api) == [] and summary["refusals"][0].startswith(ye.REFUSE_GAP)
    assert summary["next_allowed"] == led["next_allowed_at"]
    _c, summary, api = engage(env, now=NOW + gap)
    assert inserted(api) == ["commentThreads.insert"]


def test_draw_gap_stays_inside_its_bounds():
    rng = random.Random(1)
    gaps = [ye.draw_gap(rng) for _ in range(500)]
    assert min(gaps) >= timedelta(minutes=25) and max(gaps) <= timedelta(minutes=90)


@pytest.mark.parametrize("day,cap", [(1, 3), (7, 3), (8, 5), (14, 5), (15, 10),
                                     (21, 10), (22, 20), (60, 20)])
def test_the_ramp_counts_from_the_first_live_comment(env, day, cap):
    start = NOW - timedelta(days=day - 1)
    led = ledger_with(ramp_started_at=yt.utc_z(start))
    assert ye.ramp_day(led, NOW) == day
    assert ye.daily_cap(led, ye.KIND_VIDEO, NOW) == cap


def test_before_any_comment_the_cap_is_3(env):
    assert ye.daily_cap(ye.empty_ledger(), ye.KIND_VIDEO, NOW) == 3


def _today(n, kind=ye.KIND_VIDEO):
    return [live(NOW - timedelta(minutes=100 + i), kind=kind, creator="UCc%d" % i,
                 key="k%s%d" % (kind, i), video_id="v%s%d" % (kind, i)) for i in range(n)]


def test_the_daily_cap_refuses_the_fourth_on_day_one(env):
    seed()
    ledger_with(*_today(3), ramp_started_at=yt.utc_z(NOW - timedelta(hours=3)))
    _c, summary, api = engage(env)
    assert inserted(api) == []
    assert any(ye.REFUSE_CAP + ": 3 of 3" in r for r in summary["refusals"])
    nxt = yt.parse_instant(summary["next_allowed"]).astimezone(PT)
    assert (nxt.date().isoformat(), nxt.hour) == ("2026-11-24", 8)


def test_two_today_on_day_one_still_allows_a_third(env):
    seed()
    ledger_with(*_today(2), ramp_started_at=yt.utc_z(NOW - timedelta(hours=3)))
    _c, _s, api = engage(env)
    assert inserted(api) == ["commentThreads.insert"]


def test_replies_have_their_own_cap_of_20(env):
    seed("cmtVIEWER", kind=ye.KIND_REPLY)
    ledger_with(*_today(3), ramp_started_at=yt.utc_z(NOW - timedelta(hours=3)))
    _c, _s, api = engage(env)
    assert inserted(api) == ["comments.insert"]  # the video cap does not block replies
    seed("cmtVIEWER2", kind=ye.KIND_REPLY)
    ledger_with(*_today(20, ye.KIND_REPLY))
    _c, summary, api = engage(env)
    assert inserted(api) == []
    assert any(ye.REFUSE_REPLY_CAP + ": 20 of 20" in r for r in summary["refusals"])


def test_a_reply_does_not_start_the_ramp(env):
    seed("cmtVIEWER", kind=ye.KIND_REPLY)
    engage(env)
    assert ye.load_ledger(PROFILE)["ramp_started_at"] is None
    seed("vidNEW1")
    engage(env, now=NOW + timedelta(hours=2))
    assert ye.load_ledger(PROFILE)["ramp_started_at"] == yt.utc_z(NOW + timedelta(hours=2))


@pytest.mark.parametrize("ago,allowed", [(timedelta(days=2, hours=23), False),
                                         (timedelta(days=3), True)])
def test_one_comment_per_creator_per_3_days(env, ago, allowed):
    seed(creator=CREATOR_A)
    ledger_with(live(NOW - ago, creator=CREATOR_A, key="earlier", video_id="earlierVID"))
    _c, summary, api = engage(env)
    assert bool(inserted(api)) is allowed
    if not allowed:
        assert any(ye.REFUSE_CREATOR in r for r in summary["refusals"])


def test_one_comment_per_item(env):
    seed("vidNEW1")
    ledger_with(live(NOW - timedelta(days=10), creator=CREATOR_B, key="vidNEW1",
                     video_id="vidNEW1"))
    _c, summary, api = engage(env)
    assert inserted(api) == [] and api.calls == []
    assert any(ye.REFUSE_ITEM in r for r in summary["refusals"])


@pytest.mark.parametrize("text,reason", [
    ("see https://example.com for the data", "link"),
    ("the numbers at www.example.org match", "link"),
    ("same as youtu.be/abc", "link"),
    ("@creator nailed the eval bit", "@-mention"),
    ("the eval drift bit #ai", "hashtag"),
    ("Subscribed for the eval drift bit", "subscribe"),
    ("top five ai tech covered this too", "our channel name"),
    ("we covered it on topfiveaitech", "our channel handle"),
    ("Great video, the eval drift bit is good", "banned opener 'Great video'"),
    ("  \"amazing work on the latency chart", "banned opener 'Amazing'"),
])
def test_the_no_promotion_validator_refuses_back_to_review(env, text, reason):
    key = seed(comment=text)
    _c, summary, api = engage(env)
    assert inserted(api) == []
    assert any(reason in r for r in summary["refusals"]), summary["refusals"]
    rec = store().get(key)
    assert rec["status"] == post_store.GENERATED and not post_store.is_approved(rec)
    assert rec["comment"] == text
    assert rec in store().review_queue()


def test_the_validator_passes_a_plain_comment(env):
    cfg = ye.engage_config(PROFILE)
    reg = pp.policy_for("youtube").register
    assert ye.promotion_refusals("did this hold on longer contexts", cfg, reg) == []
    assert ye.promotion_refusals("the side by side at 4:10 made it click", cfg, reg) == []


def test_the_validator_refuses_when_our_name_is_not_configured(env):
    o = overlay(env["tmp"], channel_name="", channel_handle="")
    write_config({"scheduled_posting": {"platforms": {"youtube": o}}})
    reasons = ye.promotion_refusals("a plain comment", ye.engage_config(PROFILE),
                                    pp.policy_for("youtube").register)
    assert reasons and reasons[0].startswith("config:")


def test_the_reply_variant_has_the_same_banned_openers(env):
    seed("cmtVIEWER", kind=ye.KIND_REPLY, comment="Love this, thanks")
    _c, summary, api = engage(env)
    assert inserted(api) == [] and any("banned opener" in r for r in summary["refusals"])


def test_at_most_one_comment_per_call(env):
    seed("vidNEW1", creator=CREATOR_A)
    seed("vidNEW2", creator=CREATOR_B)
    seed("cmtVIEWER", kind=ye.KIND_REPLY)
    _c, _s, api = engage(env)
    assert len(inserted(api)) == 1


# ── dedupe ──────────────────────────────────────────────────────────────────

def test_dedupe_local_store_never_calls_google(env):
    seed(status=post_store.COMMENTED)
    _c, _s, api = engage(env)
    assert api.calls == []


def test_dedupe_finds_our_comment_on_page_two_and_inserts_nothing(env):
    seed()
    api = FakeApi(env)
    api.video_threads["vidNEW1"] = [[{"comment_id": "x", "author_channel_id": "UCz"}],
                                    [{"comment_id": "ourOLD", "author_channel_id": OURS}]]
    code, summary, _a = engage(env, api)
    assert code == 0 and inserted(api) == [] and summary["outcome"] == ye.ALREADY
    rec = store().get("vidNEW1")
    assert rec["status"] == post_store.COMMENTED and rec["youtube"]["comment_id"] == "ourOLD"
    led = ye.load_ledger(PROFILE)
    assert led["comments"][-1]["outcome"] == ye.ALREADY and not led["comments"][-1]["live"]


def test_dedupe_reads_at_most_3_pages(env):
    seed()
    api = FakeApi(env)
    api.video_threads["vidNEW1"] = [[{"comment_id": "p%d" % i, "author_channel_id": "UCz"}]
                                    for i in range(5)]
    api.video_threads["vidNEW1"][4][0]["author_channel_id"] = OURS  # past the cap
    engage(env, api)
    dedupe = [q for q in quota_entries() if q["op"] == "commentThreads.list dedupe"]
    assert len(dedupe) == 3
    assert inserted(api) == ["commentThreads.insert"]


def test_dedupe_for_a_reply_reads_the_thread_replies(env):
    seed("cmtVIEWER", kind=ye.KIND_REPLY)
    api = FakeApi(env)
    api.replies["cmtVIEWER"] = [{"id": "ourREPLY", "author_channel_id": OURS}]
    _c, summary, _a = engage(env, api)
    assert inserted(api) == [] and summary["outcome"] == ye.ALREADY


def test_a_deleted_video_is_closed_as_unavailable_on_a_404(env):
    seed()
    api = FakeApi(env)

    def gone(*a, **k):
        raise youtube_api.YouTubeApiError("commentThreads.list failed (HTTP 404): "
                                          "videoNotFound", status=404)
    api.comment_threads_page = gone
    _c, _s, _a = engage(env, api)
    assert store().get("vidNEW1")["status"] == post_store.UNAVAILABLE


def test_an_unclear_dedupe_error_stops_and_leaves_the_draft_approved(env):
    seed()
    api = FakeApi(env)

    def boom(*a, **k):
        raise youtube_api.YouTubeApiError("boom", ambiguous=True)
    api.comment_threads_page = boom
    code, _s, _a = engage(env, api)
    assert code == ye.EXIT_ERROR and inserted(api) == []
    assert post_store.is_approved(store().get("vidNEW1"))


# ── the three verify outcomes ───────────────────────────────────────────────

def test_posted_visible_when_the_comment_is_listed(env):
    seed()
    _c, summary, _a = engage(env)
    rec = store().get("vidNEW1")
    assert summary["outcome"] == ye.POSTED_VISIBLE and summary["posted"] == "vidNEW1"
    assert rec["status"] == post_store.COMMENTED
    assert rec["youtube"]["outcome"] == ye.POSTED_VISIBLE
    assert rec["youtube"]["comment_id"] == "newCOMMENT1"


def test_posted_held_is_not_success_and_is_never_reposted(env):
    seed()
    api = FakeApi(env)
    api.listed_after_insert = False
    code, summary, _a = engage(env, api)
    assert summary["outcome"] == ye.POSTED_HELD and summary["posted"] is None
    assert "Not counted as posted" in env["lines"][-1]
    rec = store().get("vidNEW1")
    assert rec["status"] == post_store.COMMENTED and rec["youtube"]["outcome"] == ye.POSTED_HELD
    # It still counts against pacing, and a later run never posts it again.
    led = ye.load_ledger(PROFILE)
    assert led["comments"][-1]["live"] is True
    _c, _s, api2 = engage(env, now=NOW + timedelta(days=4))
    assert inserted(api2) == []
    assert ye.status_view(PROFILE, now=NOW)["outcomes"] == {ye.POSTED_HELD: 1}


def test_a_failed_verify_call_is_held_not_visible(env):
    seed()
    api = FakeApi(env)
    real = api.comment_threads_page

    def fail_after_insert(*a, **k):
        if api.inserts:
            raise youtube_api.YouTubeApiError("verify boom")
        return real(*a, **k)
    api.comment_threads_page = fail_after_insert
    _c, summary, _a = engage(env, api)
    assert summary["outcome"] == ye.POSTED_HELD


def test_failed_on_a_refused_insert_is_closed_and_not_counted(env):
    seed()
    api = FakeApi(env)
    api.insert_error = youtube_api.YouTubeApiError(
        "commentThreads.insert failed (HTTP 403): forbidden", status=403)
    code, summary, _a = engage(env, api)
    assert code == ye.EXIT_ERROR and summary["outcome"] == ye.FAILED
    rec = store().get("vidNEW1")
    assert (rec["status"], rec["trash_reason"]) == (post_store.TRASH, ye.REASON_POST_FAILED)
    led = ye.load_ledger(PROFILE)
    assert led["comments"][-1]["live"] is False and led["next_allowed_at"] is None
    assert led["attempting"] is None


@pytest.mark.parametrize("err", [
    youtube_api.YouTubeApiError("commentThreads.insert failed (HTTP 500): x",
                                ambiguous=True, status=500),
    youtube_api.YouTubeApiError("commentThreads.insert failed with no answer: timeout",
                                ambiguous=True)])
def test_an_ambiguous_insert_is_never_retried(env, err):
    seed()
    api = FakeApi(env)
    api.insert_error = err
    _c, summary, _a = engage(env, api)
    assert len(api.inserts) == 1 and summary["outcome"] == ye.FAILED
    rec = store().get("vidNEW1")
    assert (rec["status"], rec["trash_reason"]) == (post_store.TRASH, ye.REASON_AMBIGUOUS)
    led = ye.load_ledger(PROFILE)
    assert led["comments"][-1]["live"] is True and led["comments"][-1]["ambiguous"]
    _c, _s, api2 = engage(env, now=NOW + timedelta(days=4))
    assert inserted(api2) == []


def test_a_quota_refusal_on_insert_leaves_the_draft_approved(env):
    seed()
    api = FakeApi(env)
    api.insert_error = youtube_api.YouTubeApiError("quotaExceeded", quota=True, status=403)
    code, _s, _a = engage(env, api)
    assert code == ye.EXIT_ERROR
    rec = store().get("vidNEW1")
    assert rec["status"] == post_store.GENERATED and post_store.is_approved(rec)
    assert ye.load_ledger(PROFILE)["comments"] == []


def test_a_run_that_died_mid_insert_is_resolved_as_ambiguous(env):
    key = seed()
    led = ye.empty_ledger()
    led["attempting"] = ye._entry(store().get(key), NOW - timedelta(hours=2), None, None, True)
    ye.save_ledger(PROFILE, led)
    _c, _s, api = engage(env)
    assert inserted(api) == []
    rec = store().get(key)
    assert (rec["status"], rec["trash_reason"]) == (post_store.TRASH, ye.REASON_AMBIGUOUS)
    led = ye.load_ledger(PROFILE)
    assert led["attempting"] is None and led["comments"][-1]["ambiguous"] is True


def test_the_attempt_mark_is_on_disk_during_the_insert(env):
    seed()
    api = FakeApi(env)
    seen = {}
    api.before_insert = lambda: seen.update(att=ye.load_ledger(PROFILE)["attempting"])
    engage(env, api)
    assert seen["att"]["key"] == "vidNEW1"
    assert ye.load_ledger(PROFILE)["attempting"] is None


# ── quota accounting and the gates ──────────────────────────────────────────

def test_one_comment_charges_exactly_its_calls_before_each(env):
    seed()
    engage(env)
    assert [(q["op"], q["units"]) for q in quota_entries()] == [
        ("channels.list mine", 1), ("commentThreads.list dedupe", 1),
        ("commentThreads.insert", 50), ("commentThreads.list verify", 1)]
    endpoints = [u["endpoint"] for u in usage_lines(env)]
    assert endpoints[0].startswith("oauth2 token refresh")
    assert endpoints[3] == "commentThreads.insert vidNEW1, 50 quota units"


def test_too_little_quota_refuses_before_any_call(env):
    seed()
    led = yt.load_quota(PROFILE)
    led["days"][yt.quota_day(NOW)] = {"units": 10000 - 54, "entries": []}
    yt.write_json_atomic(yt.quota_path(PROFILE), led)
    code, summary, api = engage(env)
    assert code == ye.EXIT_REFUSED and api.calls == []


def test_a_token_for_another_channel_posts_nothing(env):
    seed()
    code, summary, api = engage(env, FakeApi(env, channel=CREATOR_B))
    assert code == ye.EXIT_REFUSED and inserted(api) == []


def test_no_token_posts_nothing(env):
    seed()
    os.remove(yt.token_path(PROFILE))
    code, summary, api = engage(env)
    assert code == ye.EXIT_REFUSED and "youtube_auth.py" in summary["refused"]


def test_a_declared_production_channel_is_refused(env, monkeypatch):
    seed()
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, OURS)
    code, _s, api = engage(env)
    assert code == ye.EXIT_PRODUCTION_REFUSED and api.calls == []


def test_dry_run_calls_and_writes_nothing(env):
    seed()
    before = open(post_store.PostStore._default_path(PROFILE, "youtube"), "rb").read()
    code, summary, api = engage(env, dry_run=True)
    assert code == 0 and api.calls == [] and summary["would_post"] == "vidNEW1"
    assert open(post_store.PostStore._default_path(PROFILE, "youtube"), "rb").read() == before
    assert not os.path.exists(ye.ledger_path(PROFILE))


def test_store_and_ledger_live_beside_v1_files(env):
    seed()
    engage(env)
    d = yt.channel_dir(PROFILE)
    assert os.path.exists(os.path.join(d, "posts_db.json"))
    assert os.path.exists(os.path.join(d, "engage_ledger.json"))
    assert not os.path.exists(yt.state_path(PROFILE))  # V1's state is never written


def test_no_scheduler_job_or_task_knows_about_youtube_engage():
    import linkedin_automation.scheduler as scheduler
    src = open(scheduler.__file__, encoding="utf-8").read()
    assert "youtube_engage" not in src and "youtube-engage" not in src


# ── the CLI ─────────────────────────────────────────────────────────────────

def test_the_cli_routes_each_engagement_command(env, monkeypatch, capsys):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools"))
    import run_scheduled_posts as cli
    seen = []
    monkeypatch.setattr(cli.ye, "discover",
                        lambda p, q, **kw: seen.append(("discover", p, q, kw)) or (0, {}))
    for name in ("find", "engage"):
        monkeypatch.setattr(cli.ye, name,
                            lambda p, _n=name, **kw: seen.append((_n, p, kw)) or (0, {}))
    monkeypatch.setattr(cli.ye, "draft",
                        lambda p, **kw: seen.append(("draft", p, kw)) or (0, {}))
    for argv in (["youtube-discover", "--query", "llm evals", "--dry-run"],
                 ["youtube-find"], ["youtube-draft", "--limit", "2"],
                 ["youtube-engage", "--dry-run"],
                 ["youtube-watch", "--add", CREATOR_B, "--title", "Creator B"],
                 ["youtube-watch", "--remove", CREATOR_A], ["youtube-watch"]):
        monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py"] + argv
                            + ["--profile", PROFILE])
        assert cli.main() == 0
    assert seen == [("discover", PROFILE, "llm evals", {"dry_run": True}),
                    ("find", PROFILE, {"dry_run": False}),
                    ("draft", PROFILE, {"limit": 2, "dry_run": False}),
                    ("engage", PROFILE, {"dry_run": True})]
    assert [e["channel_id"] for e in ye.engage_config(PROFILE).watch_list] == [CREATOR_B]
    assert "1 channel(s) on the watch list" in capsys.readouterr().out


def test_subscription_talk_is_not_a_call_to_subscribe(env):
    cfg = ye.engage_config(PROFILE)
    reg = pp.policy_for("youtube").register
    assert ye.promotion_refusals("the API subscription tier caps this", cfg, reg) == []
    assert ye.promotion_refusals("hit subscribe if this helped", cfg, reg) == ["subscribe"]


def test_the_default_generator_is_the_shared_one_with_the_youtube_policy(env):
    g = ye.default_generator(PROFILE, None)
    assert g.policy is pp.policy_for("youtube")
    r = ye.default_generator(PROFILE, pp.REPLY)
    assert r.policy is pp.policy_for("youtube", pp.REPLY)
