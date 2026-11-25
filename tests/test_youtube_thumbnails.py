"""V2 (D-028): custom YouTube thumbnails from the item's vertical poster.

Google is mocked everywhere: a fake of ``youtube_api``'s functions for the
channel paths, a fake service for ``youtube_api.set_thumbnail``, and any real
socket use fails the test. Posters are synthetic images made here.
"""
# The ``env`` fixture is imported from test_youtube_channel and taken as an
# argument by name, which is how pytest shares a fixture; ruff reads that as a
# redefinition.
# ruff: noqa: F811
import hashlib
import json
import os
import random
import socket
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from linkedin_automation import youtube_api  # noqa: E402
from linkedin_automation import youtube_channel as yt  # noqa: E402
from test_youtube_channel import (  # noqa: E402,F401
    NOW, PROFILE, FakeApi, env, give_token, mark_moved, ready, state, tree,
    without_locks)

C1 = "C-001-where-did-my-rule-go"
C2 = "C-002-your-ai-doesnt-read-letters"
C3 = "C-003-it-sounded-so-sure"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class ThumbApi(FakeApi):
    """FakeApi plus thumbnails.set. ``thumb_errors`` maps a video id to the
    error that call raises."""

    def __init__(self, thumb_errors=None, during_thumb=None, **kw):
        super().__init__(**kw)
        self.thumb_errors = thumb_errors or {}
        self.during_thumb = during_thumb
        self.thumbs = []

    def set_thumbnail(self, service, video_id, path, mimetype):
        self.calls.append("thumbnails.set")
        if self.during_thumb:
            self.during_thumb(video_id)
        self.thumbs.append((video_id, path, mimetype))
        if video_id in self.thumb_errors:
            raise self.thumb_errors[video_id]


def folder(env, name):
    return os.path.join(env["content_root"], name)


def make_poster(path, size=(64, 96), noise=False, fmt="PNG"):
    from PIL import Image
    if noise:
        rnd = random.Random(7)
        im = Image.frombytes("RGB", size, bytes(rnd.getrandbits(8)
                                                for _ in range(size[0] * size[1] * 3)))
    else:
        im = Image.new("RGB", size, (200, 40, 40))
    im.save(path, format=fmt)
    return path


def poster_for(env, name, **kw):
    return make_poster(os.path.join(folder(env, name), name + yt.POSTER_SUFFIX), **kw)


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def cfg():
    return yt.youtube_config(PROFILE)


def rec_for(env, name):
    return {"id": name, "source_folder": folder(env, name), "video_id": "V"}


def usage():
    with open(yt.USAGE_LOG, encoding="utf-8") as f:
        return [json.loads(x) for x in f]


# ── the network guard ───────────────────────────────────────────────────────

def test_a_real_network_call_fails_the_test():
    with pytest.raises(AssertionError, match="reached the network"):
        socket.create_connection(("www.googleapis.com", 443), timeout=1)


# ── the file name rule ──────────────────────────────────────────────────────

def test_the_poster_is_exactly_folder_name_poster_vertical_png(env):
    rec = rec_for(env, C1)
    assert yt.poster_path(rec, cfg()) is None
    # Near misses never count: no other name, no fallback.
    for wrong in ("thumbnail.jpg", "poster-vertical.png", C1 + "-poster-horizontal.png",
                  C1 + "-poster-vertical.jpg", "where-did-my-rule-go-poster-vertical.png"):
        make_poster(os.path.join(folder(env, C1), wrong))
        assert yt.poster_path(rec, cfg()) is None, wrong
    p = poster_for(env, C1)
    assert yt.poster_path(rec, cfg()) == p


def test_an_item_folder_outside_the_content_root_has_no_poster(env, tmp_path):
    other = tmp_path / "elsewhere" / C1
    other.mkdir(parents=True)
    make_poster(str(other / (C1 + yt.POSTER_SUFFIX)))
    rec = {"id": C1, "source_folder": str(other), "video_id": "V"}
    assert yt.poster_path(rec, cfg()) is None
    assert yt.poster_path({"id": C1, "source_folder": None}, cfg()) is None


# ── the upload file ─────────────────────────────────────────────────────────

def test_a_small_png_is_sent_as_it_is(env):
    p = poster_for(env, C1)
    assert yt.thumbnail_upload_file(PROFILE, C1, p) == (p, "image/png")
    assert not os.path.exists(yt.thumbnail_dir(PROFILE))


def test_a_poster_over_2_mb_becomes_a_jpeg_copy_under_data(env):
    p = poster_for(env, C1, size=(1000, 1000), noise=True)
    assert os.path.getsize(p) > yt.THUMBNAIL_MAX_BYTES
    before_content = tree(env["content_root"])
    before = md5(p)
    path, mime = yt.thumbnail_upload_file(PROFILE, C1, p)
    assert mime == "image/jpeg"
    assert os.path.getsize(path) <= yt.THUMBNAIL_MAX_BYTES
    assert os.path.dirname(path) == yt.thumbnail_dir(PROFILE)
    assert os.path.commonpath([path, env["data_root"]]) == env["data_root"]
    from PIL import Image
    with Image.open(path) as im:
        assert im.format == "JPEG"
    # The content folder is never written.
    assert md5(p) == before
    assert tree(env["content_root"]) == before_content


def test_a_format_youtube_does_not_take_is_converted(env):
    p = os.path.join(folder(env, C1), C1 + yt.POSTER_SUFFIX)
    make_poster(p, fmt="BMP")          # named .png, but not a PNG
    path, mime = yt.thumbnail_upload_file(PROFILE, C1, p)
    assert mime == "image/jpeg" and path.endswith(C1 + ".jpg")


# ── the four outcomes on upload ─────────────────────────────────────────────

def test_upload_sets_the_thumbnail_and_records_set(env):
    ready(env)
    poster_for(env, C1)
    api = ThumbApi()
    code, s = yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    assert code == 0
    assert api.calls == ["creds", "refresh", "build", "channels.list", "upload",
                         "thumbnails.set"]
    assert api.thumbs == [("VID1", os.path.join(folder(env, C1), C1 + yt.POSTER_SUFFIX),
                           "image/png")]
    rec = state(env)["items"][C1]
    assert rec["status"] == yt.UPLOADED and rec["thumbnail"] == yt.THUMB_SET
    assert rec["thumbnail_reason"] is None
    assert s["thumbnails"] == [(C1, yt.THUMB_SET)]
    assert "  THUMBNAIL: %s  set" % C1 in env["lines"]


def test_no_poster_records_none_and_makes_no_call(env):
    ready(env)
    api = ThumbApi()
    code, s = yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    assert code == 0 and "thumbnails.set" not in api.calls
    rec = state(env)["items"][C1]
    assert rec["thumbnail"] == yt.THUMB_NONE
    assert yt.quota_used(yt.load_quota(PROFILE), "2026-11-22") == 1 + 1600


def test_a_failed_thumbnail_leaves_the_upload_successful_and_the_run_going(env):
    ready(env)
    poster_for(env, C1)
    poster_for(env, C2)
    err = youtube_api.YouTubeApiError("thumbnails.set failed (HTTP 400): invalidImage",
                                      status=400)
    api = ThumbApi(thumb_errors={"VID1": err})
    code, s = yt.schedule(PROFILE, max_uploads=2, now=NOW, api=api, out=env["out"])
    assert code == 0 and s["stopped"] is None
    items = state(env)["items"]
    assert items[C1]["status"] == yt.UPLOADED and items[C1]["video_id"] == "VID1"
    assert items[C1]["error"] is None
    assert items[C1]["thumbnail"] == yt.THUMB_FAILED
    assert "invalidImage" in items[C1]["thumbnail_reason"]
    # No retry inside the run: one call for VID1.
    assert [t[0] for t in api.thumbs] == ["VID1", "VID2"]
    assert items[C2]["status"] == yt.UPLOADED and items[C2]["thumbnail"] == yt.THUMB_SET


def test_an_unusable_poster_is_failed_not_a_failed_upload(env):
    ready(env)
    with open(os.path.join(folder(env, C1), C1 + yt.POSTER_SUFFIX), "wb") as f:
        f.write(b"not an image")
    api = ThumbApi()
    code, _s = yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    rec = state(env)["items"][C1]
    assert code == 0 and rec["status"] == yt.UPLOADED
    assert rec["thumbnail"] == yt.THUMB_FAILED
    assert rec["thumbnail_reason"].startswith("poster unusable")
    assert "thumbnails.set" not in api.calls


def test_a_thumbnail_that_would_exceed_the_quota_is_deferred(env):
    ready(env)
    poster_for(env, C1)
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["quota"]["daily_limit"] = 1 + 1600 + 49
    api = ThumbApi()
    code, _s = yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    rec = state(env)["items"][C1]
    assert code == 0 and rec["status"] == yt.UPLOADED
    assert rec["thumbnail"] == yt.THUMB_DEFERRED
    assert "quota" in rec["thumbnail_reason"]
    assert "thumbnails.set" not in api.calls
    assert yt.quota_used(yt.load_quota(PROFILE), "2026-11-22") == 1 + 1600


def test_the_ledgers_are_charged_before_the_thumbnail_call(env):
    ready(env)
    poster_for(env, C1)
    seen = []

    def during(video_id):
        ledger = yt.load_quota(PROFILE)
        seen.append((yt.quota_used(ledger, "2026-11-22"),
                     ledger["days"]["2026-11-22"]["entries"][-1]["op"],
                     usage()[-1]["endpoint"]))
    api = ThumbApi(during_thumb=during)
    yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    assert seen == [(1 + 1600 + 50, "thumbnails.set",
                     "thumbnails.set %s, 50 quota units" % C1)]
    assert [u["api"] for u in usage()] == ["youtube"] * 4
    entry = yt.load_quota(PROFILE)["days"]["2026-11-22"]["entries"][-1]
    assert (entry["units"], entry["item"]) == (50, C1)


def test_thumbnail_units_come_from_the_config(env):
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["quota"]["thumbnail_units"] = 7
    assert cfg().thumbnail_units == 7
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["quota"]["thumbnail_units"] = 0
    with pytest.raises(yt.YouTubeRefused, match="thumbnail_units"):
        cfg()


# ── the 403 ─────────────────────────────────────────────────────────────────

class _Resp(dict):
    reason = "Forbidden"

    def __init__(self, status):
        super().__init__()
        self.status = status


def _service_raising(status, body):
    from googleapiclient.errors import HttpError
    calls = []

    class Req:
        def execute(self):
            calls.append(1)
            raise HttpError(_Resp(status), json.dumps(body).encode(),
                            uri="https://www.googleapis.com/upload/youtube/v3/thumbnails/set")

    class Thumbs:
        def set(self, **kw):
            assert kw["videoId"] == "VID1"
            return Req()

    class Service:
        def thumbnails(self):
            return Thumbs()
    return Service(), calls


def _thumb_file(tmp_path):
    return make_poster(str(tmp_path / "t.png"))


def test_a_403_for_an_unverified_channel_is_named(tmp_path):
    body = {"error": {"code": 403, "message": "The authenticated user doesn't have "
                      "permissions to upload and set custom video thumbnails.",
                      "errors": [{"reason": "forbidden"}]}}
    service, calls = _service_raising(403, body)
    with pytest.raises(youtube_api.YouTubeApiError) as e:
        youtube_api.set_thumbnail(service, "VID1", _thumb_file(tmp_path), "image/png")
    assert str(e.value) == ("channel not verified for custom thumbnails "
                            "(phone verification in YouTube settings)")
    assert e.value.status == 403 and calls == [1]


def test_another_403_is_not_called_unverified(tmp_path):
    body = {"error": {"code": 403, "message": "Forbidden",
                      "errors": [{"reason": "forbidden"}]}}
    service, _c = _service_raising(403, body)
    with pytest.raises(youtube_api.YouTubeApiError) as e:
        youtube_api.set_thumbnail(service, "VID1", _thumb_file(tmp_path), "image/png")
    assert "not verified" not in str(e.value) and "HTTP 403" in str(e.value)
    body = {"error": {"code": 403, "message": "quota permission thumbnail",
                      "errors": [{"reason": "quotaExceeded"}]}}
    service, _c = _service_raising(403, body)
    with pytest.raises(youtube_api.YouTubeApiError) as e:
        youtube_api.set_thumbnail(service, "VID1", _thumb_file(tmp_path), "image/png")
    assert e.value.quota is True and "not verified" not in str(e.value)


def test_the_unverified_403_is_recorded_on_the_item(env):
    ready(env)
    poster_for(env, C1)
    err = youtube_api.YouTubeApiError(youtube_api.THUMBNAIL_NOT_ALLOWED, status=403)
    api = ThumbApi(thumb_errors={"VID1": err})
    yt.schedule(PROFILE, max_uploads=1, now=NOW, api=api, out=env["out"])
    rec = state(env)["items"][C1]
    assert yt.thumbnail_label(rec) == (
        "failed: channel not verified for custom thumbnails "
        "(phone verification in YouTube settings)")


# ── the backfill ────────────────────────────────────────────────────────────

def imported(env):
    """video-publisher's history imported: C-010, C-001 and C-002 uploaded,
    and an aif item uploaded whose folder is outside the content root."""
    assert yt.import_history(PROFILE, now=NOW, out=env["out"])[0] == 0
    give_token(env)


def test_the_backfill_refuses_without_the_marker(env):
    imported(env)
    poster_for(env, C1)
    before = without_locks(tree(env["data_root"]))
    api = ThumbApi()
    for dry in (True, False):
        code, s = yt.thumbnails(PROFILE, dry_run=dry, now=NOW, api=api, out=env["out"])
        assert code == yt.EXIT_REFUSED and "youtube-cutover" in s["refused"]
    assert api.calls == []
    assert without_locks(tree(env["data_root"])) == before


def test_the_backfill_dry_run_lists_and_writes_nothing(env):
    imported(env)
    mark_moved(env)
    poster_for(env, C1)
    poster_for(env, C2)
    before = without_locks(tree(env["data_root"]))
    content = tree(env["content_root"])
    api = ThumbApi()
    code, s = yt.thumbnails(PROFILE, dry_run=True, now=NOW, api=api, out=env["out"])
    assert code == 0 and api.calls == []
    assert s["candidates"] == [C1, C2] and s["units"] == 1 + 2 * 50
    assert any(line.startswith("  WOULD SET: %s" % C1) and line.endswith("50 units")
               for line in env["lines"])
    assert "would set 2 thumbnail(s), 101 units" in env["lines"][-1]
    assert without_locks(tree(env["data_root"])) == before
    assert tree(env["content_root"]) == content
    assert not os.path.exists(yt.USAGE_LOG)


def test_the_backfill_sets_skips_no_poster_items_and_already_set(env):
    imported(env)
    mark_moved(env)
    poster_for(env, C1)
    poster_for(env, C2)
    st = state(env)
    aif = [k for k in st["items"] if k.startswith("aif")]
    assert aif and all(yt.poster_path(st["items"][k], cfg()) is None for k in aif)
    api = ThumbApi()
    code, s = yt.thumbnails(PROFILE, max_items=1, now=NOW, api=api, out=env["out"])
    assert code == 0 and s["results"] == [(C1, yt.THUMB_SET)]
    assert api.calls == ["creds", "refresh", "build", "channels.list", "thumbnails.set"]
    # The next run covers only what is left.
    api2 = ThumbApi()
    code, s = yt.thumbnails(PROFILE, now=NOW, api=api2, out=env["out"])
    assert s["results"] == [(C2, yt.THUMB_SET)]
    assert [t[0] for t in api2.thumbs] == ["SCHED02"]
    items = state(env)["items"]
    assert items[C1]["thumbnail"] == items[C2]["thumbnail"] == yt.THUMB_SET
    assert all(not items[k].get("thumbnail") for k in aif)
    assert yt.quota_used(yt.load_quota(PROFILE), "2026-11-22") == 2 * (1 + 50)


def test_the_backfill_stops_at_the_first_error(env):
    imported(env)
    mark_moved(env)
    poster_for(env, C1)
    poster_for(env, C2)
    err = youtube_api.YouTubeApiError("thumbnails.set failed (HTTP 500): boom", status=500,
                                      ambiguous=True)
    api = ThumbApi(thumb_errors={"SCHED01": err})
    code, s = yt.thumbnails(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_ERROR and s["stopped"]["item"] == C1
    assert len(api.thumbs) == 1
    items = state(env)["items"]
    assert items[C1]["thumbnail"] == yt.THUMB_FAILED and not items[C2].get("thumbnail")


def test_the_backfill_refuses_a_token_for_another_channel(env):
    imported(env)
    mark_moved(env)
    poster_for(env, C1)
    api = ThumbApi(channel="UC_someone_else")
    code, s = yt.thumbnails(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_REFUSED and "UC_someone_else" in s["refused"]
    assert "thumbnails.set" not in api.calls


def test_the_backfill_refuses_when_the_quota_is_spent(env):
    imported(env)
    mark_moved(env)
    poster_for(env, C1)
    env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["quota"]["daily_limit"] = 50
    api = ThumbApi()
    code, s = yt.thumbnails(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_REFUSED and "quota" in s["refused"] and api.calls == []


def test_the_backfill_takes_the_production_gate(env, monkeypatch):
    imported(env)
    mark_moved(env)
    monkeypatch.setenv("PRODUCTION_IDENTITY_SLUGS", "UC_test_channel")
    api = ThumbApi()
    code, _s = yt.thumbnails(PROFILE, now=NOW, api=api, out=env["out"])
    assert code == yt.EXIT_PRODUCTION_REFUSED and api.calls == []


def test_the_backfill_holds_the_channel_lock(env):
    from linkedin_automation import publisher_lock
    imported(env)
    mark_moved(env)
    with publisher_lock.channel_lock(yt.state_path(PROFILE), "someone else"):
        code, s = yt.thumbnails(PROFILE, dry_run=True, now=NOW, api=ThumbApi(),
                                out=env["out"])
    assert code == yt.EXIT_REFUSED and "someone else" in s["refused"]


# ── status and the CLI ──────────────────────────────────────────────────────

def test_status_shows_each_items_thumbnail_outcome(env):
    ready(env)
    poster_for(env, C1)
    err = youtube_api.YouTubeApiError(youtube_api.THUMBNAIL_NOT_ALLOWED, status=403)
    yt.schedule(PROFILE, max_uploads=2, now=NOW, api=ThumbApi(thumb_errors={"VID1": err}),
                out=env["out"])
    env["lines"].clear()
    yt.status(PROFILE, now=NOW, out=env["out"])
    by_id = {line.split()[1]: line for line in env["lines"][:-1]}
    assert by_id[C1].rstrip().endswith("thumbnail failed: " + youtube_api.THUMBNAIL_NOT_ALLOWED)
    assert by_id[C2].rstrip().endswith("thumbnail none")
    assert by_id[C3].rstrip().endswith("thumbnail -")


def test_the_cli_dispatches_youtube_thumbnails(monkeypatch):
    import run_scheduled_posts as rsp
    seen = {}

    def fake(profile, dry_run=False, max_items=None):
        seen.update(profile=profile, dry_run=dry_run, max_items=max_items)
        return 0, {}
    monkeypatch.setattr(rsp.yt, "thumbnails", fake)
    monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py", "youtube-thumbnails",
                                      "--profile", "topfive", "--dry-run", "--max", "3"])
    assert rsp.main() == 0
    assert seen == {"profile": "topfive", "dry_run": True, "max_items": 3}
