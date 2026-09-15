"""Tests for lifecycle-store-driven comment generation and bin reconciliation.

Covers the bug where the dashboard showed NEW posts in the lifecycle bins but
"Generate Comments" said "No posts found" — because generation read only the
latest scrape file while NEW posts lived across older files / were falsely NEW.

No OpenAI, no browser: the generator subprocess is never launched (run_job is
stubbed), and reconciliation works purely on JSON files in a temp dir.
"""

import json
import os
from types import SimpleNamespace

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import post_store
from linkedin_automation.post_store import NEW, GENERATED, COMMENTED, TRASH, PostStore
from linkedin_automation import dashboard as dash

ACTIVITY = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


def _post(i, score=7, **extra):
    p = {"url": ACTIVITY.format(i), "author_name": f"Author {i}",
         "text": f"AI post body {i}", "relevance_score": score}
    p.update(extra)
    return p


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


def _seed_store(records):
    """Create a posts_db.json directly with the given (key->record) states."""
    store = PostStore("demo")
    for rec in records:
        store.posts[rec["key"]] = rec
    store.save()
    return store


def _rec(i, status=NEW, comment=None, **extra):
    r = {
        "key": ACTIVITY.format(i), "url": ACTIVITY.format(i),
        "author": f"Author {i}", "text": f"AI post body {i}", "category": "AI",
        "relevance_score": 7, "status": status,
        "trash_reason": "manual" if status == TRASH else None,
        "comment": comment, "comment_meta": None,
        "scraped_at": "t", "generated_at": None, "commented_at": None, "updated_at": "t",
    }
    r.update(extra)
    return r


# ─── get_posts_by_status ──────────────────────────────────────────────────────

def test_get_posts_by_status_returns_new(env):
    _seed_store([_rec(1, NEW), _rec(2, GENERATED, comment="hi"), _rec(3, NEW)])
    store = PostStore("demo")
    new = store.get_posts_by_status(NEW)
    assert {r["url"] for r in new} == {ACTIVITY.format(1), ACTIVITY.format(3)}


# ─── reconcile ────────────────────────────────────────────────────────────────

def test_reconcile_downgrades_falsely_new_to_commented(env):
    """A NEW post whose URL is in posting_progress.json becomes COMMENTED."""
    _seed_store([_rec(1, NEW), _rec(2, NEW)])
    _write(env.comments / "posting_progress.json", {"posted_comments": [ACTIVITY.format(1)]})
    counts = post_store.reconcile("demo")
    assert counts == {"NEW": 1, "GENERATED": 0, "COMMENTED": 1, "TRASH": 0}
    assert PostStore("demo").get(ACTIVITY.format(1))["status"] == COMMENTED


def test_reconcile_downgrades_falsely_new_to_generated(env):
    """A NEW post that already has a draft in a comment file becomes GENERATED."""
    _seed_store([_rec(1, NEW), _rec(2, NEW)])
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(2), "comment": "sharp take", "style": "warm",
         "word_count": 2}]})
    counts = post_store.reconcile("demo")
    assert counts == {"NEW": 1, "GENERATED": 1, "COMMENTED": 0, "TRASH": 0}
    gen = PostStore("demo").get(ACTIVITY.format(2))
    assert gen["status"] == GENERATED and gen["comment"] == "sharp take"


def test_reconcile_reads_ready_files_too(env):
    _seed_store([_rec(5, NEW)])
    _write(env.comments / "ready_20260101.json", {"comments": [
        {"url": ACTIVITY.format(5), "comment": "curated draft", "word_count": 2}]})
    post_store.reconcile("demo")
    assert PostStore("demo").get(ACTIVITY.format(5))["status"] == GENERATED


def test_reconcile_does_not_resurrect_manual_trash(env):
    """A manually-trashed post with a leftover comment-file draft stays TRASH."""
    _seed_store([_rec(9, TRASH, comment="old draft")])
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(9), "comment": "old draft", "word_count": 2}]})
    post_store.reconcile("demo")
    assert PostStore("demo").get(ACTIVITY.format(9))["status"] == TRASH


def test_reconcile_does_not_downgrade_commented(env):
    """A comment file for an already-COMMENTED post never pulls it back."""
    _seed_store([_rec(4, COMMENTED, comment="posted one")])
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(4), "comment": "posted one", "word_count": 2}]})
    post_store.reconcile("demo")
    assert PostStore("demo").get(ACTIVITY.format(4))["status"] == COMMENTED


def test_reconcile_is_idempotent(env):
    _seed_store([_rec(1, NEW), _rec(2, NEW)])
    _write(env.comments / "posting_progress.json", {"posted_comments": [ACTIVITY.format(1)]})
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(2), "comment": "draft", "word_count": 1}]})
    first = post_store.reconcile("demo")
    second = post_store.reconcile("demo")
    assert first == second == {"NEW": 0, "GENERATED": 1, "COMMENTED": 1, "TRASH": 0}


def test_load_synced_store_reconciles_generated(env):
    """load_synced_store runs the full reconcile (progress + comment files)."""
    _seed_store([_rec(1, NEW), _rec(2, NEW)])
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(1), "comment": "d1", "word_count": 1}]})
    _write(env.comments / "posting_progress.json", {"posted_comments": [ACTIVITY.format(2)]})
    store = post_store.load_synced_store("demo")
    assert store.get(ACTIVITY.format(1))["status"] == GENERATED
    assert store.get(ACTIVITY.format(2))["status"] == COMMENTED


# ─── Bin counts match what generate finds ─────────────────────────────────────

@pytest.fixture
def client(env, monkeypatch):
    dash.app.config.update(TESTING=True)
    monkeypatch.setattr(dash.pm, "auto_migrate_from_env", lambda: None)
    return dash.app.test_client()


@pytest.fixture
def captured_jobs(monkeypatch):
    """Stub run_job so the generator subprocess never launches; capture its args."""
    calls = []

    def fake_run_job(job_id, func, *args, **kwargs):
        calls.append({"job_id": job_id, "func": func, "args": args, "kwargs": kwargs})
        return job_id

    monkeypatch.setattr(dash, "run_job", fake_run_job)
    return calls


def test_generate_uses_store_new_not_latest_file(env, client, captured_jobs):
    """The bug scenario: the latest scrape file's posts are all already handled,
    but the store has NEW posts in older files. Generate must act on the store."""
    # Store: two genuinely-NEW posts.
    _seed_store([_rec(1, NEW), _rec(2, NEW)])
    # A latest scrape file whose posts are already COMMENTED (the trap).
    _write(env.timeline / "ai_posts_LATEST.json", {"quality_posts": [_post(99)]})
    _write(env.comments / "posting_progress.json", {"posted_comments": [ACTIVITY.format(99)]})

    resp = client.post("/api/comments/demo/generate", json={"limit": None})
    assert resp.status_code == 200

    # The generator was handed a lifecycle input file, not ai_posts_LATEST.json.
    infile = captured_jobs[0]["args"][1]
    assert "lifecycle_new_" in infile
    data = json.loads(open(infile, encoding="utf-8").read())
    urls = {p["url"] for p in data["quality_posts"]}
    assert urls == {ACTIVITY.format(1), ACTIVITY.format(2)}


def test_generate_new_count_matches_store_bin(env, client, captured_jobs):
    _seed_store([_rec(i, NEW) for i in range(1, 6)])
    resp = client.post("/api/comments/demo/generate", json={})
    assert resp.status_code == 200
    infile = captured_jobs[0]["args"][1]
    data = json.loads(open(infile, encoding="utf-8").read())
    store = PostStore("demo")
    assert len(data["quality_posts"]) == store.counts()[NEW] == 5


def test_generate_empty_queue_returns_400_with_counts(env, client, captured_jobs):
    _seed_store([_rec(1, COMMENTED, comment="x"), _rec(2, TRASH)])
    resp = client.post("/api/comments/demo/generate", json={})
    assert resp.status_code == 400
    body = resp.get_json()
    assert "No NEW posts" in body["error"]
    assert body["counts"][NEW] == 0
    assert captured_jobs == []  # no job launched


def test_generate_reconciles_before_selecting(env, client, captured_jobs):
    """Falsely-NEW posts (already generated) are excluded from the generate set."""
    _seed_store([_rec(1, NEW), _rec(2, NEW), _rec(3, NEW)])
    # Post 2 already has a draft on disk → reconcile moves it to GENERATED.
    _write(env.comments / "comments_1.json", {"comments": [
        {"post_url": ACTIVITY.format(2), "comment": "already drafted", "word_count": 2}]})

    resp = client.post("/api/comments/demo/generate", json={})
    assert resp.status_code == 200
    infile = captured_jobs[0]["args"][1]
    data = json.loads(open(infile, encoding="utf-8").read())
    urls = {p["url"] for p in data["quality_posts"]}
    assert urls == {ACTIVITY.format(1), ACTIVITY.format(3)}  # post 2 excluded


def test_generate_ignores_an_input_file_override(env, client, captured_jobs):
    """The ``input_file`` override is GONE — generation always reads the store.

    The dashboard sent this on every click that followed "Save & Continue", so
    generation ran on a curated file instead of the NEW bin (once, on an *empty*
    one). That silently bypassed the store-driven path for three weeks. If the
    override comes back, this fails.
    """
    _seed_store([_rec(1, NEW)])
    explicit = env.timeline / "ai_posts_curated.json"
    _write(explicit, {"quality_posts": [_post(50)]})

    resp = client.post("/api/comments/demo/generate", json={"input_file": str(explicit)})
    assert resp.status_code == 200

    used = captured_jobs[0]["args"][1]
    assert used != str(explicit)
    assert os.path.basename(used).startswith("lifecycle_new_")
    # And it carries the store's NEW post, not the curated file's.
    written = json.loads(open(used, encoding="utf-8").read())
    assert [p["url"] for p in written["quality_posts"]] == [ACTIVITY.format(1)]


def test_url_less_new_posts_are_not_actionable(env, client, captured_jobs):
    """A NEW post with no URL cannot be commented on; it is excluded and, if it's
    the only NEW post, generation reports the queue empty."""
    rec = _rec(1, NEW)
    rec["url"] = ""
    rec["key"] = "hash:abc"
    _seed_store([rec])
    resp = client.post("/api/comments/demo/generate", json={})
    assert resp.status_code == 400
    assert captured_jobs == []
