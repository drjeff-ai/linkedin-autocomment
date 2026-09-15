"""Tests that the lifecycle bins and the tab views agree (tab-bin-consistency).

The bug: bins showed 48 NEW but Review Posts showed 37 (that tab read only the
latest ai_posts_*.json), and bins showed 12 GENERATED but the Generated tab
showed none (that view read comment files, which had been archived). Fix: both
tabs read the lifecycle store, so bin count == tab item count. These tests pin
that invariant, GENERATED-draft rendering, the recover/demote reconciliation, and
that the save/archive Review-Comments sub-view is left intact.

No browser, no network, no OpenAI.
"""

import json
from types import SimpleNamespace

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as dash
from linkedin_automation import post_store
from linkedin_automation.post_store import PostStore, NEW, GENERATED, COMMENTED, TRASH

ACTIVITY = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path
    comments = data / "quality_comments"
    timeline = data / "linkedin_timeline"
    (comments / "archived").mkdir(parents=True)
    timeline.mkdir()
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(data))
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(comments))
    monkeypatch.setattr(pm, "get_timeline_dir", lambda profile_name=None: str(timeline))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(comments / "posting_progress.json"))
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    return SimpleNamespace(data=data, comments=comments, timeline=timeline)


@pytest.fixture
def client(env):
    dash.app.config.update(TESTING=True)
    return dash.app.test_client()


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


def _rec(i, status=NEW, url=None, comment=None):
    u = ACTIVITY.format(i) if url is None else url
    return {
        "key": u or f"hash:{i}", "url": u or "", "author": f"Author {i}",
        "text": f"AI post body {i}", "category": "AI", "relevance_score": 10 + i,
        "status": status, "trash_reason": None, "comment": comment,
        "comment_meta": {"style": "warm", "approach": "x", "word_count": 2} if comment else None,
        "scraped_at": "t", "generated_at": "t" if status == GENERATED else None,
        "commented_at": None, "updated_at": "t",
    }


def _seed(records):
    store = PostStore("demo")
    for r in records:
        store.posts[r["key"]] = r
    store.save()
    return store


# ─── NEW bin == Review Posts tab ──────────────────────────────────────────────

def test_new_bin_equals_review_posts_tab(client, env):
    _seed([_rec(i, NEW) for i in range(1, 6)])
    counts = client.get("/api/posts/demo/lifecycle").get_json()["counts"]
    tab = client.get("/api/posts/demo").get_json()
    assert counts["NEW"] == 5
    assert len(tab["posts"]) == counts["NEW"]           # the invariant
    assert all(p["status"] == "NEW" for p in tab["posts"])
    assert tab["source"] == "lifecycle_store"


def test_review_posts_shows_store_posts_not_in_any_file(client, env):
    """The original drift: NEW posts live in the store across all scrapes; the tab
    must show them even when NO recent scrape file contains them."""
    _seed([_rec(i, NEW) for i in range(1, 9)])   # 8 NEW, zero scrape files on disk
    tab = client.get("/api/posts/demo").get_json()
    assert len(tab["posts"]) == 8
    assert tab["file"] is None                    # nothing to enrich from


def test_review_posts_enriched_from_scrape_file(client, env):
    _seed([_rec(1, NEW)])
    _write(env.timeline / "ai_posts_1.json", {"quality_posts": [
        {"url": ACTIVITY.format(1), "author_name": "Author 1", "text": "AI post body 1",
         "likes": 42, "reposts": 3, "quality": "high"}]})
    post = client.get("/api/posts/demo").get_json()["posts"][0]
    assert post["likes"] == 42 and post["quality"] == "high"   # display metadata merged


# ─── GENERATED bin == Generated tab ───────────────────────────────────────────

def test_generated_bin_equals_generated_tab_with_drafts(client, env):
    _seed([_rec(i, GENERATED, comment=f"draft {i}") for i in range(1, 5)])
    counts = client.get("/api/posts/demo/lifecycle").get_json()["counts"]
    tab = client.get("/api/posts/demo/lifecycle?status=GENERATED").get_json()
    assert counts["GENERATED"] == 4
    assert len(tab["posts"]) == counts["GENERATED"]        # the invariant
    assert all(p["comment"] for p in tab["posts"])          # each renders its draft


def test_review_queue_shows_drafts_even_when_comment_files_archived(client, env):
    """THE regression this branch exists to prevent.

    Every generated comment file gets moved into ``archived/`` by save_comments,
    so the old file-globbing Review Comments step went permanently empty while
    the store still held the drafts (the user saw 87 GENERATED / 0 in step 04).
    Both the Generated tab AND the Review queue are store-driven now, so an
    archived — or deleted — comment file cannot empty either one.
    """
    _seed([_rec(i, GENERATED, comment=f"draft {i}") for i in range(1, 4)])
    # The draft's source file exists ONLY under archived/ (post-save state).
    _write(env.comments / "archived" / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(i), "comment": f"draft {i}"} for i in range(1, 4)]})

    generated_tab = client.get("/api/posts/demo/lifecycle?status=GENERATED").get_json()
    review_queue = client.get("/api/comments/demo").get_json()

    assert len(generated_tab["posts"]) == 3                 # Generated tab: all shown
    assert len(review_queue["comments"]) == 3               # Review Comments: all shown too
    assert {c["comment"] for c in review_queue["comments"]} == {"draft 1", "draft 2", "draft 3"}
    # bin == tab, for the step the user actually walks through.
    assert len(review_queue["comments"]) == generated_tab["counts"]["GENERATED"]


# ─── reconcile: GENERATED without a draft ─────────────────────────────────────

def test_reconcile_recovers_generated_draft_from_active_file(env):
    _seed([_rec(1, GENERATED, comment=None)])               # GENERATED, draft missing
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(1), "comment": "recovered draft", "word_count": 2}]})
    stats = {}
    post_store.reconcile("demo", stats=stats)
    rec = PostStore("demo").get(ACTIVITY.format(1))
    assert rec["status"] == GENERATED and rec["comment"] == "recovered draft"
    assert stats["recovered_drafts"] == 1 and stats["demoted_generated"] == 0


def test_reconcile_recovers_generated_draft_from_archived_file(env):
    _seed([_rec(1, GENERATED, comment=None)])
    _write(env.comments / "archived" / "comments_old.json", {"comments": [
        {"post_url": ACTIVITY.format(1), "comment": "archived draft"}]})
    post_store.reconcile("demo")
    rec = PostStore("demo").get(ACTIVITY.format(1))
    assert rec["status"] == GENERATED and rec["comment"] == "archived draft"


def test_reconcile_demotes_generated_without_recoverable_draft(env):
    _seed([_rec(1, GENERATED, comment=None)])               # no comment file anywhere
    stats = {}
    post_store.reconcile("demo", stats=stats)
    rec = PostStore("demo").get(ACTIVITY.format(1))
    assert rec["status"] == NEW and not rec["comment"]      # demoted → will regenerate
    assert stats["demoted_generated"] == 1 and stats["recovered_drafts"] == 0


def test_reconcile_leaves_intact_generated_untouched(env):
    _seed([_rec(1, GENERATED, comment="already here")])
    stats = {}
    post_store.reconcile("demo", stats=stats)
    rec = PostStore("demo").get(ACTIVITY.format(1))
    assert rec["status"] == GENERATED and rec["comment"] == "already here"
    assert stats["recovered_drafts"] == 0 and stats["demoted_generated"] == 0


def test_reconcile_generated_recovery_is_idempotent(env):
    _seed([_rec(1, GENERATED, comment=None)])
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(1), "comment": "d", "word_count": 1}]})
    first = post_store.reconcile("demo")
    second = post_store.reconcile("demo")
    assert first == second == {"NEW": 0, "GENERATED": 1, "COMMENTED": 0, "TRASH": 0}


# ─── The two views coexist (save/archive workflow intact) ─────────────────────

def test_bin_count_matches_tab_after_mixed_lifecycle(client, env):
    """End-to-end invariant across a mixed store: NEW tab == NEW bin, GENERATED
    tab == GENERATED bin, simultaneously."""
    _seed(
        [_rec(i, NEW) for i in range(1, 4)]
        + [_rec(i, GENERATED, comment=f"d{i}") for i in range(10, 15)]
        + [_rec(i, COMMENTED, comment="posted") for i in range(20, 22)]
        + [_rec(i, TRASH) for i in range(30, 34)]
    )
    counts = client.get("/api/posts/demo/lifecycle").get_json()["counts"]
    new_tab = client.get("/api/posts/demo").get_json()["posts"]
    gen_tab = client.get("/api/posts/demo/lifecycle?status=GENERATED").get_json()["posts"]
    assert len(new_tab) == counts["NEW"] == 3
    assert len(gen_tab) == counts["GENERATED"] == 5
