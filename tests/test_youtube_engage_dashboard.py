"""Y1 (D-027): the dashboard's YouTube tab and routes.

The Review routes take ``?platform=youtube``; without it they are LinkedIn's,
unchanged. Google is the same fake as ``test_youtube_engage.py``.
"""

import json
import os

import pytest

from linkedin_automation import dashboard
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import youtube_engage as ye
from test_youtube_engage import (  # noqa: F401, F811  (env is a fixture)
    CREATOR_B, NOW, PROFILE, FakeApi, env, inserted, seed, store)


@pytest.fixture
def client(env, monkeypatch):  # noqa: F811
    dashboard.app.config["TESTING"] = True
    api = FakeApi(env)
    monkeypatch.setattr(ye, "youtube_api", api)
    # The routes run at "now"; pin it inside the posting window.
    real_engage = ye.engage

    def engage_at_now(profile, dry_run=False, now=None, api=None, rng=None, out=print):
        import random
        return real_engage(profile, dry_run=dry_run, now=NOW, api=api,
                           rng=random.Random(3), out=out)
    monkeypatch.setattr(ye, "engage", engage_at_now)
    c = dashboard.app.test_client()
    c.api = api
    return c


def test_review_lists_youtube_drafts_only_with_the_platform(client):
    seed(approve=False)
    yt_res = client.get("/api/comments/%s?platform=youtube" % PROFILE).get_json()
    assert [c["key"] for c in yt_res["comments"]] == ["vidNEW1"]
    assert yt_res["platform"] == "youtube" and yt_res["comments"][0]["kind"] == "video"
    li_res = client.get("/api/comments/%s" % PROFILE).get_json()
    assert li_res["platform"] == "linkedin" and li_res["comments"] == []


def test_an_unknown_platform_is_refused(client):
    res = client.get("/api/comments/%s?platform=tiktok" % PROFILE)
    assert res.status_code == 400 and "unknown platform" in res.get_json()["error"]


def test_approve_refuses_a_promoting_draft_and_approves_a_clean_one(client):
    seed(approve=False, comment="subscribe to us")
    res = client.post("/api/comments/%s/approve?platform=youtube" % PROFILE,
                      json={"key": "vidNEW1", "comment": "subscribe to us"})
    assert res.status_code == 409 and "promotion: subscribe" in res.get_json()["error"]
    assert not post_store.is_approved(store().get("vidNEW1"))
    res = client.post("/api/comments/%s/approve?platform=youtube" % PROFILE,
                      json={"key": "vidNEW1", "comment": "the 4:10 chart made it click"})
    assert res.status_code == 200
    rec = store().get("vidNEW1")
    assert post_store.is_approved(rec) and rec["comment"] == "the 4:10 chart made it click"


def test_approve_all_refuses_the_flagged_item(client):
    seed("vidNEW1", approve=False, comment="a clean comment on the eval bit")
    seed("vidNEW2", approve=False, comment="#ai is the future", creator=CREATOR_B)
    res = client.post("/api/comments/%s/approve_all?platform=youtube" % PROFILE, json={
        "items": [{"key": "vidNEW1"}, {"key": "vidNEW2"}], "count": 2, "confirm": True})
    data = res.get_json()
    assert data["approved"] == 1 and data["refused"] == ["vidNEW2"]


def test_save_edits_the_youtube_store_and_approves_nothing(client):
    seed()
    res = client.post("/api/comments/%s/save?platform=youtube" % PROFILE,
                      json={"comments": [{"key": "vidNEW1", "comment": "an edit"}]})
    assert res.status_code == 200 and res.get_json()["edited"] == 1
    rec = store().get("vidNEW1")
    assert rec["comment"] == "an edit" and not post_store.is_approved(rec)
    # No LinkedIn poster handoff file is written for YouTube.
    assert not [f for f in os.listdir(pm.get_comments_dir(PROFILE))
                if f.startswith(("daily_comments_curated", "ready_"))]


def test_remove_rejects_in_the_youtube_store(client):
    seed()
    res = client.post("/api/posts/%s/reject?platform=youtube" % PROFILE, json={"key": "vidNEW1"})
    assert res.status_code == 200
    assert store().get("vidNEW1")["status"] == post_store.TRASH


def test_the_discover_route_adds_nothing(client):
    path = pm.get_config_path(PROFILE)
    before = open(path, "rb").read()
    res = client.post("/api/youtube/%s/discover" % PROFILE, json={"query": "evals"})
    assert res.status_code == 200
    assert [s["channel_id"] for s in res.get_json()["suggestions"]] == [CREATOR_B]
    assert open(path, "rb").read() == before


def test_watch_add_and_remove_routes(client):
    res = client.post("/api/youtube/%s/watch/add" % PROFILE,
                      json={"channel_id": CREATOR_B, "title": "Creator B", "subscribers": 9})
    assert res.status_code == 200 and res.get_json()["added"] is True
    ids = [e["channel_id"] for e in res.get_json()["watch_list"]]
    assert CREATOR_B in ids
    res = client.post("/api/youtube/%s/watch/remove" % PROFILE, json={"channel_id": CREATOR_B})
    assert res.get_json()["removed"] is True
    res = client.post("/api/youtube/%s/watch/add" % PROFILE, json={"channel_id": "bad"})
    assert res.status_code == 400


def test_the_post_button_posts_one_approved_comment(client):
    seed("vidNEW1")
    seed("cmtVIEWER", kind=ye.KIND_REPLY)
    res = client.post("/api/youtube/%s/engage" % PROFILE, json={})
    data = res.get_json()
    assert res.status_code == 200 and data["outcome"] == ye.POSTED_VISIBLE
    assert len(inserted(client.api)) == 1
    # The second click is inside the gap: nothing, and it says when.
    res = client.post("/api/youtube/%s/engage" % PROFILE, json={})
    data = res.get_json()
    assert len(inserted(client.api)) == 1
    assert data["next_allowed"] and "Next allowed" in data["lines"][-1]


def test_the_post_button_with_nothing_approved_says_so(client):
    seed(approve=False)
    data = client.post("/api/youtube/%s/engage" % PROFILE, json={}).get_json()
    assert "0 posted: 1 not approved" in data["lines"][-1]
    assert inserted(client.api) == []


def test_the_status_route_reads_local_files_only(client):
    data = client.get("/api/youtube/%s/status" % PROFILE).get_json()
    assert data["configured"] and data["cap_today"] == 3 and data["reply_cap"] == 20
    assert client.api.calls == []
    assert json.dumps(data)  # serialisable


def test_the_review_tab_is_in_the_page(client):
    html = client.get("/").get_data(as_text=True)
    assert "setReviewPlatform('youtube')" in html and 'id="youtubeEngage"' in html
