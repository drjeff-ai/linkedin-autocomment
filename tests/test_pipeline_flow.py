"""End-to-end pipeline-flow tests: save → archive → post file discovery
(ROADMAP Phase 2). Uses the Flask test client; no Selenium, no OpenAI."""

import glob
import json
import os

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as linkedin_dashboard
from linkedin_automation.comment_poster import LinkedInCommentPoster

URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


def _generator_comment(i):
    return {
        "post_url": URL.format(i),
        "post_author": f"Author {i}",
        "post_text": f"Interesting post body {i}.",
        "post_category": "AI",
        "comment": f"Great point {i} here, thanks.",
        "word_count": 5,
        "style": "thoughtful",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with the profile data dir redirected into tmp."""
    comments_dir = tmp_path / "quality_comments"
    comments_dir.mkdir()
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(comments_dir))
    monkeypatch.setattr(
        pm, "get_progress_file",
        lambda profile_name=None: str(comments_dir / "posting_progress.json"),
    )
    linkedin_dashboard.app.config.update(TESTING=True)
    return linkedin_dashboard.app.test_client(), comments_dir


def _seed_review_file(comments_dir, comments):
    """Write a generator-style comments_*.json so save() has originals to archive."""
    path = comments_dir / "comments_20260624_000000.json"
    path.write_text(json.dumps({"comments": comments}), encoding="utf-8")
    return path


# ─── Save endpoint ────────────────────────────────────────────────────────────

def test_save_writes_both_files_and_count(client):
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    resp = c.post("/api/comments/demo/save", json={"comments": comments})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 3
    assert glob.glob(str(comments_dir / "ready_*.json"))
    assert glob.glob(str(comments_dir / "daily_comments_curated_*.txt"))


def test_saved_txt_parses_to_three(client):
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    c.post("/api/comments/demo/save", json={"comments": comments})
    txt = glob.glob(str(comments_dir / "daily_comments_curated_*.txt"))[0]
    parsed = LinkedInCommentPoster.parse_comments_file(txt)
    assert len(parsed) == 3
    assert [p["url"] for p in parsed] == [URL.format(i) for i in range(1, 4)]


def test_originals_archived_after_save(client):
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    c.post("/api/comments/demo/save", json={"comments": comments})
    # No comments_*.json left in the main dir...
    assert not glob.glob(str(comments_dir / "comments_*.json"))
    # ...but present under archived/.
    assert glob.glob(str(comments_dir / "archived" / "comments_*.json"))


def test_review_empty_after_save_because_drafts_are_reviewed(client):
    """Saving empties the Review queue via store state (``reviewed_at``), not by
    archiving files — and the drafts are still in the GENERATED bin, which is what
    the count now says out loud instead of them appearing to vanish."""
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    c.post("/api/comments/demo/save", json={"comments": comments})
    resp = c.get("/api/comments/demo")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["comments"] == []                    # nothing left to review
    assert data["reviewed_count"] == 3               # ...and we say why
    assert data["counts"]["GENERATED"] == 3          # still drafted, awaiting posting
    # The invariant: queue + reviewed always reconstructs the bin.
    assert len(data["comments"]) + data["reviewed_count"] == data["counts"]["GENERATED"]


def test_save_persists_edited_text_to_the_store(client):
    """An edit made in the Review tab must reach the STORE, not just the TXT file.

    The scheduler posts from the store, so a draft edited here and saved only to
    a file would be posted in its unedited form.
    """
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 3)]
    _seed_review_file(comments_dir, comments)
    edited = [{**cm, "comment": f"EDITED {i}"} for i, cm in enumerate(comments, start=1)]

    c.post("/api/comments/demo/save", json={"comments": edited})

    everything = c.get("/api/comments/demo?include_reviewed=1").get_json()
    stored = {x["url"]: x["comment"] for x in everything["comments"]}
    assert stored[URL.format(1)] == "EDITED 1"
    assert stored[URL.format(2)] == "EDITED 2"


def test_save_rejects_an_empty_list(client):
    """Saving nothing used to write a `Total: 0` TXT that then became the
    poster's default input, making posting a silent no-op."""
    c, _ = client
    resp = c.post("/api/comments/demo/save", json={"comments": []})
    assert resp.status_code == 400
    assert not glob.glob(os.path.join(str(_), "ready_*.json"))


# ─── get_comments filtering ───────────────────────────────────────────────────

def test_get_comments_excludes_already_posted(client):
    """A posted comment reconciles to COMMENTED and leaves the GENERATED bin."""
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)
    # Mark comment 2 as already posted.
    (comments_dir / "posting_progress.json").write_text(
        json.dumps({"posted_comments": [URL.format(2)]}), encoding="utf-8"
    )
    resp = c.get("/api/comments/demo")
    data = resp.get_json()
    urls = [normalized_url(x) for x in data["comments"]]
    assert URL.format(2) not in urls
    assert len(data["comments"]) == 2
    assert data["counts"]["COMMENTED"] == 1


def normalized_url(comment):
    return comment.get("post_url") or comment.get("url")


# ─── Post endpoint file discovery ─────────────────────────────────────────────

def test_post_builds_input_from_the_store_not_a_file_glob(client, monkeypatch):
    """No comments_file supplied → the poster input is written from the store's
    GENERATED bin. It used to glob the newest daily_comments_*.txt, which after
    an empty save resolved to a `Total: 0` file and made posting a no-op."""
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)
    c.post("/api/comments/demo/save", json={"comments": comments})

    # A stale EMPTY txt that is newer than everything else — the old glob would
    # have picked exactly this file.
    stale = comments_dir / "daily_comments_curated_29999999_999999.txt"
    stale.write_text("LinkedIn Comments - Curated\nTotal: 0\n" + "=" * 60 + "\n\n",
                     encoding="utf-8")

    captured = {}

    def fake_run_job(job_id, fn, *args, **kwargs):
        captured["args"] = args  # (profile_name, comments_file, count)

    monkeypatch.setattr(linkedin_dashboard, "run_job", fake_run_job)
    monkeypatch.setattr(linkedin_dashboard, "can_start_browser_task", lambda *a, **k: True)

    resp = c.post("/api/post/demo", json={"count": 3})  # no comments_file
    assert resp.status_code == 200
    assert "job_id" in resp.get_json()
    resolved_file = captured["args"][1]
    assert os.path.exists(resolved_file)
    assert str(resolved_file) != str(stale)
    # The file the poster will read actually contains the drafts.
    body = open(resolved_file, encoding="utf-8").read()
    assert "Total: 3" in body
    for i in range(1, 4):
        assert URL.format(i) in body
    # And it parses back into 3 postable comments.
    assert len(LinkedInCommentPoster.parse_comments_file(resolved_file)) == 3


def test_post_returns_400_when_generated_bin_is_empty(client, monkeypatch):
    c, comments_dir = client
    monkeypatch.setattr(linkedin_dashboard, "run_job", lambda *a, **k: None)
    monkeypatch.setattr(linkedin_dashboard, "can_start_browser_task", lambda *a, **k: True)
    resp = c.post("/api/post/demo", json={"count": 1})
    assert resp.status_code == 400
    assert "No drafted comments" in resp.get_json()["error"]
