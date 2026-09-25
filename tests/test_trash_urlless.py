"""Tests for trashing URL-less posts (reason ``no_url``).

A post with no URL can't be commented on or posted, so it belongs in TRASH, not
stuck in NEW forever. Covered here: the store transition, the reconcile rule and
its ordering (non-NEW records are never trashed), the scraper trashing URL-less
engageable posts at scrape time, idempotency, and the diagnostic count. No
browser, no network, no OpenAI.
"""

import json
from types import SimpleNamespace

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import post_store
from linkedin_automation.post_store import (
    PostStore, NEW, GENERATED, COMMENTED, TRASH, REASON_NO_URL, REASON_MANUAL,
)

ACTIVITY = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


@pytest.fixture
def store(tmp_path):
    return PostStore(path=str(tmp_path / "posts_db.json"))


def _urlless_new(store, i, status=NEW, **extra):
    """Insert a URL-less record (content-hash keyed) in the given status."""
    rec = {
        "key": f"hash:{i}", "url": "", "author": f"Author {i}",
        "text": f"body {i}", "category": "AI", "relevance_score": 5,
        "status": status, "trash_reason": None, "comment": None,
        "comment_meta": None, "scraped_at": "t", "generated_at": None,
        "commented_at": None, "updated_at": "t",
    }
    rec.update(extra)
    store.posts[rec["key"]] = rec
    return rec["key"]


# ─── Store transition: trash_urlless_new ──────────────────────────────────────

def test_trash_urlless_new_moves_new_without_url(store):
    key = _urlless_new(store, 1)
    moved = store.trash_urlless_new()
    assert moved == 1
    rec = store.get(key)
    assert rec["status"] == TRASH and rec["trash_reason"] == REASON_NO_URL


def test_trash_urlless_new_keeps_new_with_url(store):
    key = store.upsert_scraped({"url": ACTIVITY.format(2), "author_name": "A", "text": "x"})
    assert store.trash_urlless_new() == 0
    assert store.get(key)["status"] == NEW


def test_trash_urlless_new_ignores_non_new(store):
    """A URL-less record already COMMENTED/GENERATED is never trashed (guarded on
    status == NEW), matching 'don't trash something matched via another field'."""
    c = _urlless_new(store, 1, status=COMMENTED)
    g = _urlless_new(store, 2, status=GENERATED, comment="draft")
    t = _urlless_new(store, 3, status=TRASH, trash_reason=REASON_MANUAL)
    assert store.trash_urlless_new() == 0
    assert store.get(c)["status"] == COMMENTED
    assert store.get(g)["status"] == GENERATED
    assert store.get(t)["trash_reason"] == REASON_MANUAL  # manual reason untouched


def test_trash_urlless_new_whitespace_url_is_urlless(store):
    key = _urlless_new(store, 1, url="   ")
    assert store.trash_urlless_new() == 1
    assert store.get(key)["trash_reason"] == REASON_NO_URL


def test_trash_urlless_new_is_idempotent(store):
    _urlless_new(store, 1)
    assert store.trash_urlless_new() == 1
    assert store.trash_urlless_new() == 0  # already trashed, nothing left to move


# ─── reconcile integration + ordering ─────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
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


def test_reconcile_trashes_urlless_new(env):
    store = PostStore("demo")
    store.upsert_scraped({"url": ACTIVITY.format(1), "author_name": "A", "text": "x"})  # NEW w/ url
    _urlless_new(store, 2)  # NEW w/o url
    store.save()

    counts = post_store.reconcile("demo")
    assert counts == {"NEW": 1, "GENERATED": 0, "COMMENTED": 0, "TRASH": 1, "UNAVAILABLE": 0}
    assert PostStore("demo").get("hash:2")["trash_reason"] == REASON_NO_URL


def test_reconcile_does_not_trash_commented_urlless(env):
    """Ordering: a URL-less record that is already COMMENTED stays COMMENTED —
    the no_url step runs last and only touches records still NEW."""
    store = PostStore("demo")
    _urlless_new(store, 1, status=COMMENTED)
    store.save()
    post_store.reconcile("demo")
    assert PostStore("demo").get("hash:1")["status"] == COMMENTED


def test_reconcile_urlless_idempotent(env):
    store = PostStore("demo")
    _urlless_new(store, 1)
    store.save()
    first = post_store.reconcile("demo")
    second = post_store.reconcile("demo")
    assert first == second == {"NEW": 0, "GENERATED": 0, "COMMENTED": 0, "TRASH": 1, "UNAVAILABLE": 0}


def test_restore_then_reconcile_retrashes_urlless(env):
    """Restoring a no_url post makes it NEW again; with still no URL, the next
    reconcile re-trashes it (idempotent round-trip)."""
    store = PostStore("demo")
    key = _urlless_new(store, 1)
    store.save()
    post_store.reconcile("demo")

    store = PostStore("demo")
    assert store.restore(key, save=True) is True
    assert store.get(key)["status"] == NEW  # back in the pipeline

    post_store.reconcile("demo")
    assert PostStore("demo").get(key)["trash_reason"] == REASON_NO_URL


def test_reconcile_urlless_new_matched_to_commented_by_url_not_trashed(env):
    """A NEW post WITH a url that was posted becomes COMMENTED (step 1), so it is
    never considered for no_url trashing."""
    store = PostStore("demo")
    store.upsert_scraped({"url": ACTIVITY.format(1), "author_name": "A", "text": "x"})
    store.save()
    _write(env.comments / "posting_progress.json", {"posted_comments": [ACTIVITY.format(1)]})
    post_store.reconcile("demo")
    assert PostStore("demo").get(ACTIVITY.format(1))["status"] == COMMENTED


# ─── trash_reason_counts (diagnostic support) ─────────────────────────────────

def test_trash_reason_counts(store):
    _urlless_new(store, 1)
    _urlless_new(store, 2)
    store.trash_urlless_new()
    store.upsert_scraped({"author_name": "B", "text": "ad body"}, status=TRASH,
                         reason=post_store.REASON_AD)
    counts = store.trash_reason_counts()
    assert counts[REASON_NO_URL] == 2
    assert counts[post_store.REASON_AD] == 1


# ─── Scraper trashes URL-less engageable posts at scrape time ─────────────────

def test_scraper_upserts_urlless_engageable_as_no_url(env):
    """_update_post_store: a should_engage post with no URL lands in TRASH(no_url)."""
    from linkedin_automation.post_finder import LinkedInAIPostFinder, LinkedInPost

    good = LinkedInPost(url=ACTIVITY.format(1), author_name="A", text="engage me",
                        should_engage=True, relevance_score=30, post_type="discussion")
    urlless = LinkedInPost(url=None, author_name="B", text="worth it but no link",
                           should_engage=True, relevance_score=25, post_type="discussion")
    low = LinkedInPost(url=ACTIVITY.format(3), author_name="C", text="meh",
                       should_engage=False, relevance_score=1)

    fake = SimpleNamespace(
        profile_name="demo", posts=[good, urlless, low], trashed=[],
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    LinkedInAIPostFinder._update_post_store(fake)

    store = PostStore("demo")
    assert store.get(ACTIVITY.format(1))["status"] == NEW
    assert store.get(ACTIVITY.format(3))["trash_reason"] == post_store.REASON_LOW_QUALITY
    urlless_recs = [r for r in store.by_status(TRASH) if r["trash_reason"] == REASON_NO_URL]
    assert len(urlless_recs) == 1 and urlless_recs[0]["author"] == "B"
