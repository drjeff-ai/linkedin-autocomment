"""Tests for the scrape-file helpers and the store-driven pipeline endpoints.

Originally this covered the pre-store ``merge_posts`` / ``merge_comments``
helpers that built the review lists by globbing and deduping files. Both lists
are now served from the lifecycle store (fix-lifecycle-consistency), those
helpers are gone, and what remains here is ``_files_within_days`` (still used to
find scrape files for display-metadata enrichment) plus endpoint-level coverage
of the store-backed contract."""

import json
import os
import time

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as dash


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def timeline_dir(tmp_path, monkeypatch):
    """Redirect the profile timeline dir (scrape output) to tmp."""
    tdir = tmp_path / "linkedin_timeline"
    tdir.mkdir(parents=True)
    monkeypatch.setattr(pm, "get_timeline_dir", lambda profile_name=None: str(tdir))
    return tdir


def _post(url=None, author="Author A", text="some interesting body", score=10):
    p = {"author_name": author, "text": text, "relevance_score": score, "quality": "medium"}
    if url is not None:
        p["url"] = url
    return p


def _write_scrape(tdir, name, posts, age_seconds=0):
    path = tdir / name
    path.write_text(json.dumps({"scan_date": "2026-06-29", "total_scanned": len(posts),
                                "quality_posts": posts}), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _write_comments(cdir, name, comments, curated=False, age_seconds=0):
    path = cdir / name
    path.write_text(json.dumps({"curated": curated, "comments": comments}), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


# ─── Pre-store merge helpers are gone (the store serves every list now) ───────

def test_prestore_file_merge_helpers_are_removed():
    """Regression guard for the root defect.

    ``merge_posts`` / ``merge_comments`` / ``_pipeline_comment_urls`` /
    ``_posted_urls`` / ``_post_score`` built the pipeline lists by globbing files.
    Every one of them was a way for a tab to disagree with its bin. If one comes
    back, a file-based reader has been reintroduced — fix that, don't relax this.
    """
    for name in ("merge_posts", "merge_comments", "_pipeline_comment_urls",
                 "_posted_urls", "_post_score"):
        assert not hasattr(dash, name), (
            f"dashboard.{name} is back — pipeline lists must come from the store"
        )


# ─── _files_within_days ──────────────────────────────────────────────────────--

def test_files_within_days_excludes_old_and_sorts_newest_first(tmp_path):
    recent = tmp_path / "ai_posts_recent.json"
    mid = tmp_path / "ai_posts_mid.json"
    old = tmp_path / "ai_posts_old.json"
    for fp in (recent, mid, old):
        fp.write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(recent, (now, now))
    os.utime(mid, (now - 2 * 86400, now - 2 * 86400))
    os.utime(old, (now - 10 * 86400, now - 10 * 86400))   # outside the 7-day window
    found = dash._files_within_days(str(tmp_path), "ai_posts_*.json", 7, now=now)
    assert [os.path.basename(f) for f in found] == ["ai_posts_recent.json", "ai_posts_mid.json"]


# ─── /api/posts endpoint ───────────────────────────────────────────────────────

def test_get_posts_returns_store_new_bin(api_client, timeline_dir, comments_dir):
    """get_posts is store-driven now: the store is seeded from the scrape/comment/
    progress files, then only its NEW bin is returned (u1 posted→COMMENTED,
    u3 has a draft→GENERATED, u2 stays NEW)."""
    _write_scrape(timeline_dir, "ai_posts_1.json",
                  [_post("u1", score=8), _post("u2", score=5)], age_seconds=3600)
    _write_scrape(timeline_dir, "ai_posts_2.json",
                  [_post("u2", score=9), _post("u3", score=7)])
    (comments_dir / "posting_progress.json").write_text(
        json.dumps({"posted_comments": ["u1"]}), encoding="utf-8")
    _write_comments(comments_dir, "comments_x.json", [{"post_url": "u3", "comment": "hi"}])

    data = api_client.get("/api/posts/demo").get_json()
    urls = [p["url"] for p in data["posts"]]
    assert urls == ["u2"]                       # only the NEW bin
    assert data["source"] == "lifecycle_store"
    # Invariant: the tab list length equals the NEW lifecycle bin.
    assert len(data["posts"]) == data["counts"]["NEW"] == 1
    assert all(p["status"] == "NEW" for p in data["posts"])


def test_get_posts_empty_when_no_data(api_client, timeline_dir, comments_dir):
    data = api_client.get("/api/posts/demo").get_json()
    assert data["posts"] == []
    assert data["counts"]["NEW"] == 0


# ─── /api/comments endpoint ─────────────────────────────────────────────────--

def test_get_comments_returns_store_generated_bin(api_client, comments_dir):
    """The review queue is the GENERATED bin, not a file glob. A posted comment
    reconciles to COMMENTED and leaves the bin; the rest stay."""
    _write_comments(comments_dir, "comments_1.json",
                    [{"post_url": "cu1", "comment": "old one"}, {"post_url": "cu2", "comment": "posted"}],
                    age_seconds=3600)
    _write_comments(comments_dir, "comments_2.json",
                    [{"post_url": "cu3", "comment": "fresh"}])
    # cu2 already posted.
    (comments_dir / "posting_progress.json").write_text(
        json.dumps({"posted_comments": ["cu2"]}), encoding="utf-8")

    data = api_client.get("/api/comments/demo").get_json()
    urls = {c["url"] for c in data["comments"]}
    assert urls == {"cu1", "cu3"}          # cu2 posted → COMMENTED, not GENERATED
    assert data["source"] == "lifecycle_store"
    # Both naming conventions present for the frontend.
    assert all("post_url" in c and "url" in c for c in data["comments"])
    # The invariant the whole fix exists to hold.
    assert len(data["comments"]) + data["reviewed_count"] == data["counts"]["GENERATED"]


def test_get_comments_excludes_already_curated_ready_files(api_client, comments_dir):
    """A draft seeded from ready_*.json was already curated, so it is seeded
    reviewed and stays out of the review queue — expressed as store state
    (reviewed_at) rather than as which file the draft happens to live in."""
    _write_comments(comments_dir, "comments_1.json", [{"post_url": "cu1", "comment": "review me"}])
    _write_comments(comments_dir, "ready_1.json",
                    [{"post_url": "cuR", "comment": "already curated"}], curated=True)

    data = api_client.get("/api/comments/demo").get_json()
    assert {c["url"] for c in data["comments"]} == {"cu1"}
    assert data["reviewed_count"] == 1
    # ...but it is still a drafted post, so it remains in the GENERATED bin and
    # is visible when the caller asks for everything.
    assert data["counts"]["GENERATED"] == 2
    everything = api_client.get("/api/comments/demo?include_reviewed=1").get_json()
    assert {c["url"] for c in everything["comments"]} == {"cu1", "cuR"}
