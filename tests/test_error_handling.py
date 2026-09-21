"""Tests for Phase 4 error handling: the poster skips-and-logs a failing comment
instead of aborting the run, and the dashboard job runner maps file/permission
errors to clean messages. No Selenium, no network."""

import time

import pytest

from linkedin_automation import comment_fields as cf
from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as linkedin_dashboard
from linkedin_automation import comment_poster as post_linkedin_comments
from linkedin_automation.comment_poster import LinkedInCommentPoster

URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


class _FakeDriver:
    def quit(self):
        pass


@pytest.fixture
def poster(tmp_path, monkeypatch):
    comments_dir = tmp_path / "quality_comments"
    shots_dir = comments_dir / "debug_screenshots"
    shots_dir.mkdir(parents=True)
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(comments_dir))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda profile_name=None: str(shots_dir))
    monkeypatch.setattr(
        pm, "get_progress_file",
        lambda profile_name=None: str(comments_dir / "posting_progress.json"),
    )
    # Never actually sleep between posts.
    monkeypatch.setattr(post_linkedin_comments.time, "sleep", lambda *a, **k: None)

    p = LinkedInCommentPoster(profile_name="t")
    # Stub the browser boundary.
    p.setup_driver = lambda: setattr(p, "driver", _FakeDriver())
    p.login = lambda: True
    return p, comments_dir


def _write_txt(comments_dir, comments):
    txt = cf.comments_to_txt(comments, "2026-06-24 12:00")
    path = comments_dir / "daily_comments_curated_x.txt"
    path.write_text(txt, encoding="utf-8")
    return str(path)


def _comment(i):
    return {"post_url": URL.format(i), "post_author": f"A{i}", "comment": f"comment {i}"}


def test_one_failing_comment_does_not_abort_run(poster):
    """6 comments, comment 3 raises → posts 5, FAILS 1, no raise.

    The resilience claim is unchanged: one bad comment must not stop the rest.
    What changed is the BOOKKEEPING. A raised exception used to be counted as a
    "skip", which is what let a run that published nothing read as a quiet
    success. A skip is "we chose not to try"; this is "we tried and nothing
    published", and the two must not share a counter.
    """
    p, comments_dir = poster
    comments = [_comment(i) for i in range(1, 7)]
    txt = _write_txt(comments_dir, comments)

    def fake_post(comment, *a, **k):
        if "activity:3/" in comment["url"]:
            raise RuntimeError("navigation failed")
        return True

    p.post_single_comment = fake_post

    result = p.run(txt, post_count=10)
    assert result["posted"] == 5
    assert result["failed"] == 1          # NOT skipped
    assert result["skipped"] == 0
    assert result["attempted"] == 6
    assert result["total"] == 6


def test_failed_post_returns_false_is_counted_as_a_failure(poster):
    p, comments_dir = poster
    comments = [_comment(i) for i in range(1, 4)]
    txt = _write_txt(comments_dir, comments)

    def fake_post(comment, *a, **k):
        return "activity:2/" not in comment["url"]  # comment 2 "fails"

    p.post_single_comment = fake_post

    result = p.run(txt, post_count=10)
    assert result["posted"] == 2
    # post_single_comment returning False means the comment was attempted and
    # did not go out. That is a failure, and the summary must say so.
    assert result["failed"] == 1
    assert result["skipped"] == 0


def test_post_count_limit_respected(poster):
    p, comments_dir = poster
    comments = [_comment(i) for i in range(1, 6)]
    txt = _write_txt(comments_dir, comments)
    p.post_single_comment = lambda c, *a, **k: True

    result = p.run(txt, post_count=2)
    assert result["posted"] == 2


def test_a_failing_comment_is_logged_loudly_with_its_reason(poster, caplog):
    p, comments_dir = poster
    comments = [_comment(1), _comment(2)]
    txt = _write_txt(comments_dir, comments)

    def fake_post(comment, *a, **k):
        if "activity:2/" in comment["url"]:
            raise RuntimeError("boom reason")
        return True

    p.post_single_comment = fake_post
    with caplog.at_level("WARNING"):
        p.run(txt, post_count=10)
    # ERROR, not a warning about a "skip": a typed comment that never posted is
    # the failure this whole module exists to make impossible to miss.
    failures = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("2/2 FAILED to post" in r.getMessage() for r in failures)
    assert any("boom reason" in r.getMessage() for r in failures)


# ─── Dashboard job runner: distinct error mapping ─────────────────────────────

def _wait(job_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = linkedin_dashboard.jobs.get(job_id)
        if job and job["status"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_run_job_file_not_found_is_clean():
    def boom(jid):
        raise FileNotFoundError(2, "No such file", "missing.txt")

    linkedin_dashboard.run_job("eh_fnf", boom)
    job = _wait("eh_fnf")
    assert job["status"] == "failed"
    assert "File not found" in job["error"]
    assert not any("Traceback" in line for line in job["log"])


def test_run_job_permission_error_is_clean():
    def boom(jid):
        raise PermissionError(13, "Permission denied", "locked.json")

    linkedin_dashboard.run_job("eh_perm", boom)
    job = _wait("eh_perm")
    assert job["status"] == "failed"
    assert "Permission denied" in job["error"]
    assert not any("Traceback" in line for line in job["log"])
