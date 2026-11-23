"""E0 (DEC-060): the server-side comment approval gate.

``post_store.is_approved`` is the one shared check: ``status == GENERATED``,
``reviewed_at`` set, and ``approved_hash`` equal to the sha256 of the current
text. Every engagement post path calls it at the last step before posting:

- the dashboard Post route (its "fall back to the rest of the bin" is gone)
- the scheduler's ``post_comments`` job
- ``auto_cycle``'s post stage (tests/test_generate_from_lifecycle.py)
- the ``comment_poster`` subprocess, which re-reads the store per comment

Each path has a True test (an approved draft is posted) and False tests
(unapproved, edited after approval, wrong status). Save approves nothing.
"""

import os

import pytest

from linkedin_automation import comment_fields as cf
from linkedin_automation import comment_poster as cpm
from linkedin_automation import dashboard as dash
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import scheduler as sched
from linkedin_automation.post_store import (
    COMMENTED, GENERATED, NEW, TRASH, PostStore, approval_refusal, is_approved,
    text_hash,
)

URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


def _rec(i, status=GENERATED, comment="a draft", approved=False, **extra):
    r = {
        "key": URL.format(i), "url": URL.format(i), "author": f"A{i}",
        "text": f"post {i}", "category": "AI", "relevance_score": 5,
        "status": status, "trash_reason": None, "comment": comment,
        "comment_meta": {}, "scraped_at": "t", "generated_at": "t",
        "commented_at": None, "reviewed_at": None, "approved_hash": None,
        "updated_at": "t",
    }
    if approved:
        r["reviewed_at"] = "2026-11-22T09:00:00"
        r["approved_hash"] = text_hash(comment)
    r.update(extra)
    return r


#: (name, record, expected refusal). One approved; every way not to be.
CASES = [
    ("approved", _rec(1, approved=True), None),
    ("unapproved", _rec(1), post_store.REFUSE_NOT_REVIEWED),
    ("reviewed_at only, pre-E0", _rec(1, reviewed_at="2026-07-29T00:00:00"),
     post_store.REFUSE_NO_HASH),
    ("edited after approval", dict(_rec(1, approved=True), comment="edited"),
     post_store.REFUSE_EDITED),
    ("wrong status: COMMENTED", _rec(1, status=COMMENTED, approved=True),
     post_store.REFUSE_WRONG_STATUS),
    ("wrong status: TRASH", _rec(1, status=TRASH, approved=True),
     post_store.REFUSE_WRONG_STATUS),
    ("wrong status: NEW", _rec(1, status=NEW, approved=True),
     post_store.REFUSE_WRONG_STATUS),
    ("no record", None, post_store.REFUSE_NO_RECORD),
]


# ─── The shared check ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, rec, reason", CASES, ids=[c[0] for c in CASES])
def test_is_approved_both_directions(name, rec, reason):
    assert approval_refusal(rec) == reason
    assert is_approved(rec) is (reason is None)


def test_hash_is_of_the_exact_text():
    rec = _rec(1, comment="Exact text.", approved=True)
    assert is_approved(rec)
    for variant in ("Exact text. ", "exact text.", "Exact text!"):
        assert not is_approved(dict(rec, comment=variant)), variant


# ─── Store transitions ───────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = PostStore(path=str(tmp_path / "posts_db.json"))
    s.mark_generated(URL.format(1), "first draft")
    return s


def test_approve_stores_time_and_hash(store):
    assert store.approve(URL.format(1))
    rec = store.get(URL.format(1))
    assert rec["reviewed_at"]
    assert rec["approved_hash"] == text_hash("first draft")
    assert is_approved(rec)


def test_approve_with_text_approves_that_text(store):
    assert store.approve(URL.format(1), comment="as shown")
    rec = store.get(URL.format(1))
    assert rec["comment"] == "as shown" and is_approved(rec)


def test_editing_clears_the_approval(store):
    store.approve(URL.format(1))
    assert store.save_edit(URL.format(1), "changed")
    rec = store.get(URL.format(1))
    assert rec["comment"] == "changed"
    assert rec["reviewed_at"] is None and rec["approved_hash"] is None
    assert not is_approved(rec)


def test_saving_the_same_text_keeps_the_approval(store):
    store.approve(URL.format(1))
    assert store.save_edit(URL.format(1), "first draft") is False
    assert is_approved(store.get(URL.format(1)))


def test_save_edit_never_approves(store):
    store.save_edit(URL.format(1), "an edit")
    assert not is_approved(store.get(URL.format(1)))


def test_regenerating_clears_the_approval(store):
    store.approve(URL.format(1))
    store.mark_generated(URL.format(1), "a new draft")
    rec = store.get(URL.format(1))
    assert rec["approved_hash"] is None and not is_approved(rec)


@pytest.mark.parametrize("status", [NEW, COMMENTED, TRASH])
def test_only_generated_can_be_approved(tmp_path, status):
    s = PostStore(path=str(tmp_path / "posts_db.json"))
    s.posts[URL.format(1)] = _rec(1, status=status)
    assert s.approve(URL.format(1)) is False
    assert not is_approved(s.get(URL.format(1)))


def test_pre_e0_reviewed_records_are_counted_and_queued(tmp_path):
    s = PostStore(path=str(tmp_path / "posts_db.json"))
    s.posts[URL.format(1)] = _rec(1, reviewed_at="2026-07-29T00:00:00")
    s.posts[URL.format(2)] = _rec(2, approved=True)
    assert s.unapproved_reviewed_count() == 1
    assert [r["key"] for r in s.review_queue()] == [URL.format(1)]
    assert s.reviewed_count() == 1


# ─── Dashboard routes ────────────────────────────────────────────────────────

def _seed(recs):
    s = PostStore("demo")
    for r in recs:
        s.posts[r["key"]] = r
    s.save()


@pytest.fixture
def captured(monkeypatch):
    seen = {}
    monkeypatch.setattr(dash, "run_job", lambda job_id, fn, *a, **k: seen.update(args=a))
    monkeypatch.setattr(dash, "can_start_browser_task", lambda *a, **k: True)
    return seen


def test_post_route_true_posts_only_the_approved(api_client, captured):
    _seed([_rec(1, comment="approved one", approved=True),
           _rec(2, comment="never approved"),
           dict(_rec(3, comment="was approved", approved=True), comment="then edited")])
    resp = api_client.post("/api/post/demo", json={"count": 10})
    assert resp.status_code == 200
    assert resp.get_json()["not_approved"] == 2
    body = open(captured["args"][1], encoding="utf-8").read()
    assert "approved one" in body
    assert "never approved" not in body and "then edited" not in body


@pytest.mark.parametrize("name, rec, reason",
                         [c for c in CASES if c[1] is not None and c[2] is not None],
                         ids=[c[0] for c in CASES if c[1] is not None and c[2] is not None])
def test_post_route_false_posts_nothing(api_client, captured, name, rec, reason):
    _seed([rec])
    resp = api_client.post("/api/post/demo", json={"count": 1})
    assert resp.status_code == 400
    assert "args" not in captured
    if rec["status"] == GENERATED:
        assert resp.get_json()["error"].startswith("0 posted: 1 not approved")


def test_post_route_refuses_a_hand_built_file(api_client, captured, tmp_path):
    _seed([_rec(1, approved=True)])
    resp = api_client.post("/api/post/demo", json={"comments_file": str(tmp_path / "x.txt")})
    assert resp.status_code == 400
    assert "args" not in captured


def test_save_route_approves_nothing(api_client):
    _seed([_rec(1, comment="original")])
    resp = api_client.post("/api/comments/demo/save", json={"comments": [
        {"key": URL.format(1), "url": URL.format(1), "comment": "edited"}]})
    assert resp.status_code == 200 and resp.get_json()["approved"] == 0
    rec = PostStore("demo").get(URL.format(1))
    assert rec["comment"] == "edited" and not is_approved(rec)


def test_save_route_edit_unapproves_an_approved_draft(api_client):
    _seed([_rec(1, comment="original", approved=True)])
    api_client.post("/api/comments/demo/save", json={"comments": [
        {"key": URL.format(1), "url": URL.format(1), "comment": "edited"}]})
    assert not is_approved(PostStore("demo").get(URL.format(1)))


def test_approve_route_approves_one(api_client):
    _seed([_rec(1, comment="one"), _rec(2, comment="two")])
    resp = api_client.post("/api/comments/demo/approve",
                           json={"key": URL.format(1), "comment": "one, as shown"})
    assert resp.status_code == 200
    s = PostStore("demo")
    assert is_approved(s.get(URL.format(1)))
    assert s.get(URL.format(1))["comment"] == "one, as shown"
    assert not is_approved(s.get(URL.format(2)))


@pytest.mark.parametrize("status", [NEW, COMMENTED, TRASH])
def test_approve_route_refuses_wrong_status(api_client, status):
    _seed([_rec(1, status=status)])
    resp = api_client.post("/api/comments/demo/approve", json={"key": URL.format(1)})
    assert resp.status_code == 409
    assert not is_approved(PostStore("demo").get(URL.format(1)))


@pytest.mark.parametrize("body", [
    {"count": 2},                       # no confirm
    {"count": 2, "confirm": "true"},    # a string is not the JSON true
    {"count": 3, "confirm": True},      # count does not match
    {"confirm": True},                  # no count
])
def test_approve_all_needs_count_and_confirm(api_client, body):
    _seed([_rec(1), _rec(2)])
    items = [{"key": URL.format(i), "comment": "a draft"} for i in (1, 2)]
    resp = api_client.post("/api/comments/demo/approve_all", json=dict(body, items=items))
    assert resp.status_code == 400
    s = PostStore("demo")
    assert not any(is_approved(s.get(URL.format(i))) for i in (1, 2))


def test_approve_all_with_count_and_confirm(api_client):
    _seed([_rec(1), _rec(2)])
    items = [{"key": URL.format(i), "comment": "a draft"} for i in (1, 2)]
    resp = api_client.post("/api/comments/demo/approve_all",
                           json={"items": items, "count": 2, "confirm": True})
    assert resp.status_code == 200 and resp.get_json()["approved"] == 2
    s = PostStore("demo")
    assert all(is_approved(s.get(URL.format(i))) for i in (1, 2))


def test_review_panel_has_per_item_approve_and_a_counted_confirm():
    html = open(os.path.join(os.path.dirname(dash.__file__), "templates",
                             "dashboard.html"), encoding="utf-8").read()
    assert "onclick='approveComment(" in html
    assert "/approve_all" in html and "confirm(`Approve all ${n}" in html
    save = html[html.index("async function saveComments()"):]
    save = save[:save.index("\n}\n")]
    assert "/approve" not in save        # Save stores edits only


# ─── The scheduler's post_comments job ───────────────────────────────────────

class _Exec:
    def __init__(self):
        self.calls = []

    def submit(self, profile, comments_file, count):
        self.calls.append((comments_file, count))
        return "job"


def _fire(monkeypatch, tmp_path, recs):
    s = PostStore(path=str(tmp_path / "posts_db.json"))
    for r in recs:
        s.posts[r["key"]] = r
    s.save()
    monkeypatch.setattr(post_store, "load_synced_store", lambda p=None, **kw: s)
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    ex = _Exec()
    engine = sched.Scheduler(submit_post_job=ex.submit, list_profiles=lambda: ["p"],
                             config_fn=lambda p: {"scheduler": {"draft_max_age_days": 0}})
    result = engine._fire_post_comments("p", {"count_min": 5, "count_max": 5},
                                        __import__("datetime").datetime(2026, 11, 22, 10))
    return result, ex


def test_scheduler_true_posts_only_the_approved(monkeypatch, tmp_path):
    result, ex = _fire(monkeypatch, tmp_path, [
        _rec(1, comment="approved one", approved=True), _rec(2, comment="unapproved")])
    assert result["action"] == "fired" and result["count"] == 1
    body = open(ex.calls[0][0], encoding="utf-8").read()
    assert "approved one" in body and "unapproved" not in body


@pytest.mark.parametrize("name, rec, reason",
                         [c for c in CASES if c[1] is not None and c[2] is not None],
                         ids=[c[0] for c in CASES if c[1] is not None and c[2] is not None])
def test_scheduler_false_posts_nothing(monkeypatch, tmp_path, name, rec, reason):
    result, ex = _fire(monkeypatch, tmp_path, [rec])
    assert ex.calls == []
    assert result["count"] == 0
    if rec["status"] == GENERATED:
        assert result["note"] == "0 posted: 1 not approved"


# ─── The comment_poster subprocess re-check ─────────────────────────────────

class _FakeDriver:
    def quit(self):
        pass


@pytest.fixture
def poster(comments_dir, monkeypatch):
    monkeypatch.setattr(cpm.time, "sleep", lambda *a, **k: None)
    p = cpm.LinkedInCommentPoster(profile_name="demo")
    p.setup_driver = lambda: setattr(p, "driver", _FakeDriver())
    p.login = lambda: True
    p.posted = []
    p.post_single_comment = lambda c, *a, **k: p.posted.append(c["comment"]) or True
    return p, comments_dir


def _txt(comments_dir, recs):
    comments = [{"post_url": r["url"], "post_author": r["author"],
                 "comment": r["comment"]} for r in recs]
    path = comments_dir / "daily_comments_curated_x.txt"
    path.write_text(cf.comments_to_txt(comments, "2026-11-22 10:00"), encoding="utf-8")
    return str(path)


def test_poster_true_posts_an_approved_record(poster):
    p, cdir = poster
    rec = _rec(1, comment="approved text", approved=True)
    _seed([rec])
    result = p.run(_txt(cdir, [rec]), post_count=5)
    assert p.posted == ["approved text"]
    assert result["posted"] == 1 and result["not_approved"] == 0


@pytest.mark.parametrize("name, rec, reason",
                         [c for c in CASES if c[1] is not None and c[2] is not None],
                         ids=[c[0] for c in CASES if c[1] is not None and c[2] is not None])
def test_poster_false_posts_nothing(poster, caplog, name, rec, reason):
    p, cdir = poster
    _seed([rec])
    result = p.run(_txt(cdir, [rec]), post_count=5)
    assert p.posted == []
    assert result["posted"] == 0 and result["not_approved"] == 1
    assert "0 posted: 1 not approved" in caplog.text
    assert reason in caplog.text


def test_poster_refuses_a_url_with_no_record(poster):
    p, cdir = poster
    result = p.run(_txt(cdir, [_rec(9, comment="free text")]), post_count=5)
    assert p.posted == [] and result["not_approved"] == 1


def test_poster_refuses_text_that_differs_from_the_approved(poster, caplog):
    """A TXT file altered after it was written is not what was approved."""
    p, cdir = poster
    rec = _rec(1, comment="approved text", approved=True)
    _seed([rec])
    path = _txt(cdir, [dict(rec, comment="tampered text")])
    result = p.run(path, post_count=5)
    assert p.posted == [] and result["not_approved"] == 1
    assert post_store.REFUSE_TEXT_DIFFERS in caplog.text


def test_poster_rechecks_the_store_per_comment(poster):
    """Approval withdrawn after the file was written: the second comment is
    refused because the store is read again before it."""
    p, cdir = poster
    recs = [_rec(1, comment="one", approved=True), _rec(2, comment="two", approved=True)]
    _seed(recs)
    path = _txt(cdir, recs)

    def post_and_unapprove(c, *a, **k):
        p.posted.append(c["comment"])
        s = PostStore("demo")
        s.save_edit(URL.format(2), "two, edited")
        s.save()
        return True

    p.post_single_comment = post_and_unapprove
    result = p.run(path, post_count=5)
    assert p.posted == ["one"]
    assert result["posted"] == 1 and result["not_approved"] == 1


def test_poster_test_url_mode_refuses_free_text(comments_dir, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["comment_poster", "--test-url", URL.format(1),
                                     "--test-comment", "free text", "--profile", "demo"])
    monkeypatch.setattr(cpm.LinkedInCommentPoster, "setup_driver",
                        lambda self: pytest.fail("browser opened"))
    _seed([_rec(1, comment="approved text", approved=True)])
    with pytest.raises(SystemExit) as exc:
        cpm.main()
    assert exc.value.code == pm.EXIT_ERROR
    assert "0 posted: 1 not approved" in capsys.readouterr().out


def test_poster_test_url_mode_refuses_an_unapproved_record(comments_dir, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["comment_poster", "--test-url", URL.format(1),
                                     "--profile", "demo"])
    monkeypatch.setattr(cpm.LinkedInCommentPoster, "setup_driver",
                        lambda self: pytest.fail("browser opened"))
    _seed([_rec(1, comment="a draft")])
    with pytest.raises(SystemExit) as exc:
        cpm.main()
    assert exc.value.code == pm.EXIT_ERROR
