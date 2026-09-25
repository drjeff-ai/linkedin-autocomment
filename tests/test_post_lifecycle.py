"""Tests for the central post lifecycle store (post-lifecycle).

Covers state transitions, posting_progress reconciliation, migration from legacy
files, and the dashboard lifecycle/reject/restore endpoints. No browser, no
network, no OpenAI — pure store logic plus the Flask test client with storage
redirected to a temp dir.
"""

import json
from types import SimpleNamespace

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as dash
from linkedin_automation import post_store
from linkedin_automation.post_store import PostStore, NEW, GENERATED, COMMENTED, TRASH


ACTIVITY = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


def _post(i, score=20, author=None, text=None):
    return {
        "url": ACTIVITY.format(i),
        "author_name": author or f"Author {i}",
        "text": text or f"A genuinely interesting post body number {i}.",
        "post_type": "discussion",
        "relevance_score": score,
    }


@pytest.fixture
def store(tmp_path):
    return PostStore(path=str(tmp_path / "posts_db.json"))


# ─── Identity / dedup parity with the dashboard ───────────────────────────────

def test_post_key_uses_url_when_present():
    assert post_store.post_key({"url": "https://x/1"}) == "https://x/1"


def test_post_key_hashes_when_urlless():
    key = post_store.post_key({"author_name": "Jane", "text": "x" * 200})
    assert key.startswith("hash:")
    # Same author + first 100 chars → same key.
    assert key == post_store.post_key({"author_name": "Jane", "text": "x" * 150})


def test_dashboard_dedupe_delegates_to_post_key():
    for item in ({"url": "u1"}, {"author_name": "A", "text": "body"}, {"post_url": "p2"}):
        assert dash._dedupe_key(item) == post_store.post_key(item)


def test_ad_reason_mapping():
    assert post_store.ad_reason_to_trash_reason("job/recommendation card") == post_store.REASON_JOB
    assert post_store.ad_reason_to_trash_reason("recommendation card") == post_store.REASON_JOB
    assert post_store.ad_reason_to_trash_reason("promoted/sponsored") == post_store.REASON_AD
    assert post_store.ad_reason_to_trash_reason("likely ad") == post_store.REASON_AD


# ─── Transitions ──────────────────────────────────────────────────────────────

def test_upsert_inserts_new(store):
    key = store.upsert_scraped(_post(1))
    rec = store.get(key)
    assert rec["status"] == NEW
    assert rec["url"] == ACTIVITY.format(1)
    assert rec["scraped_at"] and rec["comment"] is None


def test_upsert_low_quality_is_trash_with_reason(store):
    key = store.upsert_scraped(_post(2, score=2), status=TRASH,
                               reason=post_store.REASON_LOW_QUALITY)
    rec = store.get(key)
    assert rec["status"] == TRASH and rec["trash_reason"] == "low_quality"


def test_rescrape_refreshes_content_but_keeps_new(store):
    key = store.upsert_scraped(_post(3, score=10))
    store.upsert_scraped(_post(3, score=40, text="updated longer body text here"))
    rec = store.get(key)
    assert rec["status"] == NEW
    assert rec["relevance_score"] == 40            # refreshed
    assert "updated longer body" in rec["text"]


def test_rescrape_does_not_downgrade_generated_or_commented(store):
    key = store.upsert_scraped(_post(4))
    store.mark_generated(key, "nice point")
    store.upsert_scraped(_post(4))                  # re-scrape
    assert store.get(key)["status"] == GENERATED    # not knocked back to NEW
    assert store.get(key)["comment"] == "nice point"

    store.sync_with_progress([ACTIVITY.format(4)])
    store.upsert_scraped(_post(4))
    assert store.get(key)["status"] == COMMENTED


def test_rescrape_does_not_resurrect_manual_trash(store):
    key = store.upsert_scraped(_post(5))
    store.reject(key)
    store.upsert_scraped(_post(5))                  # re-scrape must not revive it
    assert store.get(key)["status"] == TRASH
    assert store.get(key)["trash_reason"] == "manual"


def test_mark_generated_attaches_comment_and_meta(store):
    key = store.upsert_scraped(_post(6))
    assert store.mark_generated(key, "great take", meta={"style": "witty", "word_count": 7})
    rec = store.get(key)
    assert rec["status"] == GENERATED
    assert rec["comment"] == "great take"
    assert rec["comment_meta"]["style"] == "witty"
    assert rec["generated_at"]


def test_mark_generated_creates_record_when_post_absent(store):
    # The generator may mark a post GENERATED that was never scraped into the
    # store (stray input file); it should create the record, not lose the draft.
    url = ACTIVITY.format(99)
    assert store.mark_generated(url, "fresh draft") is True
    rec = store.get(url)
    assert rec is not None
    assert rec["status"] == GENERATED and rec["comment"] == "fresh draft"


def test_mark_generated_noop_when_commented(store):
    key = store.upsert_scraped(_post(7))
    store.sync_with_progress([ACTIVITY.format(7)])
    assert store.mark_generated(key, "late comment") is False
    assert store.get(key)["status"] == COMMENTED


def test_reject_then_restore_roundtrip(store):
    key = store.upsert_scraped(_post(8))
    store.mark_generated(key, "draft here")
    assert store.reject(key) is True
    assert store.get(key)["status"] == TRASH and store.get(key)["trash_reason"] == "manual"
    # Draft preserved across reject.
    assert store.get(key)["comment"] == "draft here"
    assert store.restore(key) is True
    # Has a draft → restores to GENERATED.
    assert store.get(key)["status"] == GENERATED


def test_restore_without_draft_goes_to_new(store):
    key = store.upsert_scraped(_post(9), status=TRASH, reason=post_store.REASON_AD)
    assert store.restore(key) is True
    assert store.get(key)["status"] == NEW
    assert store.get(key)["trash_reason"] is None


def test_restore_only_from_trash(store):
    key = store.upsert_scraped(_post(10))
    assert store.restore(key) is False             # NEW isn't trash


def test_sync_with_progress_marks_commented_idempotently(store):
    k1 = store.upsert_scraped(_post(11))
    store.upsert_scraped(_post(12))
    assert store.sync_with_progress([ACTIVITY.format(11)]) == 1
    assert store.get(k1)["status"] == COMMENTED
    assert store.get(k1)["commented_at"]
    # Second run changes nothing.
    assert store.sync_with_progress([ACTIVITY.format(11)]) == 0


def test_counts_and_by_status(store):
    store.upsert_scraped(_post(13, score=30))
    store.upsert_scraped(_post(14, score=50))
    k = store.upsert_scraped(_post(15))
    store.mark_generated(k, "x")
    store.upsert_scraped(_post(16), status=TRASH, reason=post_store.REASON_AD)
    counts = store.counts()
    assert counts == {"NEW": 2, "GENERATED": 1, "COMMENTED": 0, "TRASH": 1, "UNAVAILABLE": 0}
    # NEW sorted by score desc.
    new_urls = [r["url"] for r in store.by_status(NEW)]
    assert new_urls == [ACTIVITY.format(14), ACTIVITY.format(13)]


def test_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "posts_db.json")
    s1 = PostStore(path=path)
    key = s1.upsert_scraped(_post(17), save=True)
    s2 = PostStore(path=path)
    assert s2.get(key)["status"] == NEW


def test_corrupt_store_starts_fresh(tmp_path):
    path = tmp_path / "posts_db.json"
    path.write_text("{ not json", encoding="utf-8")
    s = PostStore(path=str(path))
    assert s.posts == {}


# ─── Migration ────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect every storage path the store/dashboard touches into tmp."""
    data = tmp_path
    comments = data / "quality_comments"
    timeline = data / "linkedin_timeline"
    comments.mkdir()
    timeline.mkdir()
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(data))
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(comments))
    monkeypatch.setattr(pm, "get_timeline_dir", lambda profile_name=None: str(timeline))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(comments / "posting_progress.json"))
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    return SimpleNamespace(data=data, comments=comments, timeline=timeline)


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_migration_classifies_from_legacy_files(env):
    # Scrape: q1/q2 quality (NEW), lq1 low-quality (TRASH low_quality).
    _write(env.timeline / "ai_posts_1.json", {
        "quality_posts": [_post(1), _post(2)],
        "all_posts": [
            dict(_post(1), should_engage=True),
            dict(_post(3, score=2), should_engage=False),   # low quality
        ],
    })
    # Comment generated for post 2 → GENERATED.
    _write(env.comments / "comments_1.json", {
        "comments": [{"post_url": ACTIVITY.format(2), "comment": "well said",
                      "style": "warm", "word_count": 2}]
    })
    # Post 1 already posted → COMMENTED (overrides NEW).
    _write(env.comments / "posting_progress.json",
           {"posted_comments": [ACTIVITY.format(1)]})

    counts = post_store.migrate_from_legacy("demo")
    assert counts == {"NEW": 0, "GENERATED": 1, "COMMENTED": 1, "TRASH": 1, "UNAVAILABLE": 0}

    store = PostStore("demo")
    assert store.get(ACTIVITY.format(1))["status"] == COMMENTED
    gen = store.get(ACTIVITY.format(2))
    assert gen["status"] == GENERATED and gen["comment"] == "well said"
    assert store.get(ACTIVITY.format(3))["status"] == TRASH
    assert store.get(ACTIVITY.format(3))["trash_reason"] == "low_quality"


def test_migration_is_idempotent(env):
    _write(env.timeline / "ai_posts_1.json", {"quality_posts": [_post(1)]})
    _write(env.comments / "posting_progress.json",
           {"posted_comments": [ACTIVITY.format(1)]})
    first = post_store.migrate_from_legacy("demo")
    second = post_store.migrate_from_legacy("demo")
    assert first == second == {"NEW": 0, "GENERATED": 0, "COMMENTED": 1, "TRASH": 0, "UNAVAILABLE": 0}


def test_load_synced_store_migrates_and_reconciles(env):
    _write(env.timeline / "ai_posts_1.json", {"quality_posts": [_post(1), _post(2)]})
    # Mark post 2 posted AFTER the store would be seeded → reconcile must catch it.
    _write(env.comments / "posting_progress.json",
           {"posted_comments": [ACTIVITY.format(2)]})
    store = post_store.load_synced_store("demo")
    assert store.get(ACTIVITY.format(2))["status"] == COMMENTED
    assert store.get(ACTIVITY.format(1))["status"] == NEW


# ─── Dashboard endpoints ──────────────────────────────────────────────────────

@pytest.fixture
def client(env):
    dash.app.config.update(TESTING=True)
    return dash.app.test_client()


def test_lifecycle_endpoint_returns_counts_and_groups(client, env):
    _write(env.timeline / "ai_posts_1.json", {
        "quality_posts": [_post(1), _post(2)],
        "all_posts": [dict(_post(9, score=1), should_engage=False)],
    })
    _write(env.comments / "posting_progress.json",
           {"posted_comments": [ACTIVITY.format(1)]})

    data = client.get("/api/posts/demo/lifecycle").get_json()
    assert data["counts"]["COMMENTED"] == 1
    assert data["counts"]["NEW"] == 1
    assert data["counts"]["TRASH"] == 1
    assert {p["url"] for p in data["posts"]["NEW"]} == {ACTIVITY.format(2)}


def test_lifecycle_status_filter_returns_trash_with_reason(client, env):
    _write(env.timeline / "ai_posts_1.json", {
        "all_posts": [dict(_post(5, score=1), should_engage=False)],
    })
    data = client.get("/api/posts/demo/lifecycle?status=TRASH").get_json()
    assert data["status"] == "TRASH"
    assert len(data["posts"]) == 1
    assert data["posts"][0]["trash_reason"] == "low_quality"


def test_reject_then_excluded_from_review_and_in_trash(client, env):
    _write(env.timeline / "ai_posts_1.json", {"quality_posts": [_post(1), _post(2)]})
    # Seed the store via the lifecycle endpoint (migration).
    client.get("/api/posts/demo/lifecycle")

    # Reject post 1.
    resp = client.post("/api/posts/demo/reject", json={"key": ACTIVITY.format(1)})
    assert resp.status_code == 200
    assert resp.get_json()["counts"]["TRASH"] == 1

    # It disappears from the Review-Posts list...
    review = client.get("/api/posts/demo").get_json()
    assert ACTIVITY.format(1) not in [p["url"] for p in review["posts"]]
    assert ACTIVITY.format(2) in [p["url"] for p in review["posts"]]
    # ...and appears in Trash.
    trash = client.get("/api/posts/demo/lifecycle?status=TRASH").get_json()
    assert ACTIVITY.format(1) in [p["url"] for p in trash["posts"]]


def test_restore_brings_post_back(client, env):
    _write(env.timeline / "ai_posts_1.json", {"quality_posts": [_post(1)]})
    client.get("/api/posts/demo/lifecycle")
    client.post("/api/posts/demo/reject", json={"key": ACTIVITY.format(1)})

    resp = client.post("/api/posts/demo/restore", json={"key": ACTIVITY.format(1)})
    assert resp.status_code == 200
    assert resp.get_json()["counts"]["NEW"] == 1
    assert resp.get_json()["counts"]["TRASH"] == 0
    review = client.get("/api/posts/demo").get_json()
    assert ACTIVITY.format(1) in [p["url"] for p in review["posts"]]


def test_reject_unknown_is_404(client, env):
    _write(env.timeline / "ai_posts_1.json", {"quality_posts": [_post(1)]})
    client.get("/api/posts/demo/lifecycle")
    resp = client.post("/api/posts/demo/reject", json={"key": "https://nope/x"})
    assert resp.status_code == 404


def test_reject_requires_identifier(client, env):
    resp = client.post("/api/posts/demo/reject", json={})
    assert resp.status_code == 400


def test_get_posts_annotates_status_and_key(client, env):
    _write(env.timeline / "ai_posts_1.json", {"quality_posts": [_post(1)]})
    data = client.get("/api/posts/demo").get_json()
    assert data["posts"][0]["status"] == "NEW"
    assert data["posts"][0]["key"] == ACTIVITY.format(1)


# ─── Producer glue: finder writes the store (NEW / TRASH) ──────────────────────

def test_finder_update_post_store_maps_quality_and_trash(env):
    import logging
    from linkedin_automation import post_finder as finder_mod
    from linkedin_automation.post_finder import LinkedInAIPostFinder, LinkedInPost, PostQuality

    def _lp(i, engage, score):
        return LinkedInPost(
            url=ACTIVITY.format(i), author_name=f"A{i}", text=f"body {i} text",
            post_type="discussion", relevance_score=score,
            quality=PostQuality.MEDIUM if engage else PostQuality.LOW,
            should_engage=engage,
        )

    # Build a finder without its heavy __init__; exercise the real store glue.
    finder = LinkedInAIPostFinder.__new__(LinkedInAIPostFinder)
    finder.profile_name = "demo"
    finder.logger = logging.getLogger("test_finder")
    finder.posts = [_lp(1, True, 40), _lp(2, False, 3)]   # NEW + low_quality TRASH
    finder.trashed = [{
        "post": {"url": "", "author_name": "Acme Co", "text": "Try our product now",
                 "post_type": "", "relevance_score": 0},
        "reason": finder_mod.post_store.REASON_AD,
    }]

    finder._update_post_store()

    store = PostStore("demo")
    assert store.counts() == {"NEW": 1, "GENERATED": 0, "COMMENTED": 0, "TRASH": 2, "UNAVAILABLE": 0}
    assert store.get(ACTIVITY.format(1))["status"] == NEW
    assert store.get(ACTIVITY.format(2))["trash_reason"] == "low_quality"
    # The ad (no URL) is hash-keyed and trashed as "ad".
    ad = [r for r in store.by_status(TRASH) if r["trash_reason"] == "ad"]
    assert len(ad) == 1 and ad[0]["author"] == "Acme Co"
