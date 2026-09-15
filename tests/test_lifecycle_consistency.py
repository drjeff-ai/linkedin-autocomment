"""Tests for fix-lifecycle-consistency — the store is the source of truth for
WORKING, not just for COUNTING.

Background (see .dev/AUDIT_bin_mismatch.md): three previous fixes each asserted
``bin_count == endpoint_response`` and each held, while the user still saw empty
tabs. The endpoint is not the tab. So this file covers three layers:

  1. **Store transitions** — the lifecycle states the pipeline depends on.
  2. **Endpoint contract** — the store-backed invariants the tabs rely on.
  3. **Client contract** (``_client_source``) — static assertions about the
     JavaScript in ``dashboard.html``, because every one of the client-side
     defects was a *specific line* of JS: the New chip calling ``goStep`` instead
     of reading the server, a missing reload-if-empty fallback, and an
     ``input_file`` being posted. Those lines are asserted directly.

╔══════════════════════════════════════════════════════════════════════════════╗
║ HUMAN VERIFICATION STILL REQUIRED — this suite CANNOT drive a browser.       ║
║                                                                              ║
║ Layer 3 proves the wiring is present in the source. It does NOT prove the    ║
║ page renders. Nothing here executes JavaScript, builds a DOM, or observes a  ║
║ card. A syntax error elsewhere in dashboard.html, a CSS rule hiding a panel, ║
║ or a broken template would pass every test below.                            ║
║                                                                              ║
║ The named checks a human must perform in a real browser are listed in        ║
║ test_HUMAN_CHECKLIST_for_the_rendering_path() at the bottom of this file.    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json
import os
import re
from types import SimpleNamespace

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import post_store
from linkedin_automation import comment_generator as gen
from linkedin_automation import dashboard as dash
from linkedin_automation.post_finder import classify_ad, is_recruiter_job_ad
from linkedin_automation.post_store import (
    NEW, GENERATED, COMMENTED, TRASH, PostStore,
    REASON_EVALUATOR_REJECTED, REASON_MANUAL,
)

ACTIVITY = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"

_DASHBOARD_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "linkedin_automation", "templates", "dashboard.html",
)


@pytest.fixture(scope="module")
def client_source():
    """The dashboard's client source, for asserting on the rendering path."""
    with open(_DASHBOARD_HTML, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Profile storage redirected to tmp; nothing touches real user data."""
    data = tmp_path / "data"
    comments = data / "quality_comments"
    timeline = data / "linkedin_timeline"
    for d in (comments, timeline):
        d.mkdir(parents=True)
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(data))
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(comments))
    monkeypatch.setattr(pm, "get_timeline_dir", lambda profile_name=None: str(timeline))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(comments / "posting_progress.json"))
    return SimpleNamespace(data=data, comments=comments, timeline=timeline,
                           store_path=str(data / "posts_db.json"))


@pytest.fixture
def client(env):
    dash.app.config.update(TESTING=True)
    return dash.app.test_client()


def _rec(i, status, comment=None, reviewed_at=None, url=None):
    return {
        "key": ACTIVITY.format(i) if url is None else url,
        "url": ACTIVITY.format(i) if url is None else url,
        "author": f"Author {i}", "text": f"Post body {i} about AI agents.",
        "category": "AI", "relevance_score": 10 + i, "status": status,
        "trash_reason": None, "comment": comment, "comment_meta": {"style": "s"},
        "scraped_at": "2026-07-01T00:00:00", "generated_at": None,
        "commented_at": None, "reviewed_at": reviewed_at,
        "updated_at": "2026-07-01T00:00:00",
    }


def _seed(env, records):
    with open(env.store_path, "w", encoding="utf-8") as f:
        json.dump({"version": post_store.SCHEMA_VERSION, "updated_at": "x",
                   "posts": {r["key"]: r for r in records}}, f)


# ══════════════════════════════════════════════════════════════════════════════
# DEFECT C — evaluator-rejected posts must leave NEW
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def make_generator(env, monkeypatch):
    monkeypatch.setattr(gen.time, "sleep", lambda *a, **k: None)

    def _make(**kw):
        return gen.AuthenticLinkedInCommentGenerator(
            "dummy.json", model="gpt-4o-mini", profile_name="demo",
            platform=gen.platform_policy.LINKEDIN, **kw)
    return _make


def _gen_posts(n):
    return [{"url": ACTIVITY.format(i), "text": f"AI agents post number {i} with body.",
             "author_name": f"Author {i}"} for i in range(1, n + 1)]


def _verdicts(generator, monkeypatch, mapping):
    """Stub the LLM evaluator: url -> 'engage' | 'skip'. Records every call."""
    seen = []

    def _eval(post):
        seen.append(post["url"])
        return {"verdict": mapping.get(post["url"], "engage"),
                "conversation_potential": 5, "authenticity": 5,
                "post_category": "technical_discussion", "reason": "stubbed"}
    monkeypatch.setattr(generator, "evaluate_post_quality", _eval)
    monkeypatch.setattr(generator, "generate_comment", lambda post, ev: {
        "comment": "Useful point.", "word_count": 2, "style": "add_insight",
        "approach": "add_perspective", "generated_at": "2026-07-01T00:00:00"})
    return seen


def test_skip_verdict_moves_post_to_trash_evaluator_rejected(env, make_generator, monkeypatch):
    """A 'skip' verdict is now a lifecycle transition, not a silent drop."""
    _seed(env, [_rec(1, NEW), _rec(2, NEW)])
    g = make_generator()
    _verdicts(g, monkeypatch, {ACTIVITY.format(2): "skip"})
    monkeypatch.setattr(g, "load_posts", lambda: {"quality_posts": _gen_posts(2)})

    g.generate_all_comments()

    store = PostStore("demo")
    assert store.get(ACTIVITY.format(1))["status"] == GENERATED
    rejected = store.get(ACTIVITY.format(2))
    assert rejected["status"] == TRASH
    assert rejected["trash_reason"] == REASON_EVALUATOR_REJECTED
    assert store.counts()["NEW"] == 0


def test_rejected_post_is_not_re_evaluated_on_the_next_run(env, make_generator, monkeypatch):
    """The cost bug: 43 of 49 NEW posts were re-sent to the LLM on every run.

    After rejection the post is out of NEW, so the store-driven generate input
    no longer contains it and the evaluator never sees it again.
    """
    _seed(env, [_rec(1, NEW), _rec(2, NEW)])

    g1 = make_generator()
    _verdicts(g1, monkeypatch, {ACTIVITY.format(1): "skip", ACTIVITY.format(2): "skip"})
    monkeypatch.setattr(g1, "load_posts", lambda: {"quality_posts": _gen_posts(2)})
    g1.generate_all_comments()

    store = PostStore("demo")
    assert store.counts()["NEW"] == 0
    assert store.counts()["TRASH"] == 2

    # Second run: the store-driven input is what the dashboard would hand over.
    remaining = store.get_posts_by_status(NEW)
    assert remaining == []

    g2 = make_generator()
    seen = _verdicts(g2, monkeypatch, {})
    monkeypatch.setattr(g2, "load_posts", lambda: {"quality_posts": []})
    g2.generate_all_comments()
    assert seen == []                      # zero LLM evaluator calls, zero spend


def test_all_rejected_run_still_records_the_rejections(env, make_generator, monkeypatch):
    """The exact production case: every post rejected → early return. The
    turn-downs must still be written back, or NEW never drains."""
    _seed(env, [_rec(1, NEW)])
    g = make_generator()
    _verdicts(g, monkeypatch, {ACTIVITY.format(1): "skip"})
    monkeypatch.setattr(g, "load_posts", lambda: {"quality_posts": _gen_posts(1)})

    assert g.generate_all_comments() == []
    assert PostStore("demo").get(ACTIVITY.format(1))["trash_reason"] == REASON_EVALUATOR_REJECTED


def test_cheap_prefilter_rejections_are_recorded_too(env, make_generator, monkeypatch):
    """Spam/hashtag auto-rejects never reach the LLM but are the same zombie
    problem — they must also leave NEW."""
    _seed(env, [_rec(1, NEW)])
    g = make_generator()
    _verdicts(g, monkeypatch, {})
    monkeypatch.setattr(g, "load_posts", lambda: {"quality_posts": [
        {"url": ACTIVITY.format(1), "text": "Amazing AI tool, link in bio!",
         "author_name": "Spammer"}]})

    g.generate_all_comments()
    assert PostStore("demo").get(ACTIVITY.format(1))["trash_reason"] == REASON_EVALUATOR_REJECTED


def test_evaluator_rejection_never_downgrades_an_acted_on_post(env):
    """Guard rails: only records still in NEW move."""
    store = PostStore("demo", path=str(env.store_path))
    for status in (GENERATED, COMMENTED):
        store.posts.clear()
        store.posts[ACTIVITY.format(1)] = _rec(1, status, comment="draft")
        assert store.reject_by_evaluator(ACTIVITY.format(1)) is False
        assert store.get(ACTIVITY.format(1))["status"] == status
    # A manual trash is never re-labelled as an evaluator rejection either.
    store.posts.clear()
    manual = _rec(1, TRASH)
    manual["trash_reason"] = REASON_MANUAL
    store.posts[ACTIVITY.format(1)] = manual
    assert store.reject_by_evaluator(ACTIVITY.format(1)) is False
    assert store.get(ACTIVITY.format(1))["trash_reason"] == REASON_MANUAL


def test_rejected_post_is_restorable(env, client):
    """TRASH(evaluator_rejected) is auto, so the user can put it back."""
    rec = _rec(1, TRASH)
    rec["trash_reason"] = REASON_EVALUATOR_REJECTED
    _seed(env, [rec])
    resp = client.post("/api/posts/demo/restore", json={"key": ACTIVITY.format(1)})
    assert resp.status_code == 200
    assert PostStore("demo").get(ACTIVITY.format(1))["status"] == NEW


def test_evaluator_rejected_is_an_auto_reason():
    assert REASON_EVALUATOR_REJECTED in post_store.AUTO_REASONS
    assert REASON_EVALUATOR_REJECTED not in (REASON_MANUAL,)


def test_trash_view_labels_the_new_reason(client_source):
    """The Trash view must explain the reason, not print a raw slug."""
    assert "evaluator_rejected: 'Rejected by evaluator'" in client_source


# ══════════════════════════════════════════════════════════════════════════════
# DEFECT C (backfill) — the pre-existing zombies
# ══════════════════════════════════════════════════════════════════════════════

def test_backfill_identifies_posts_offered_to_a_past_generator_run(env):
    """A NEW post whose URL was in a past generator INPUT file was evaluated and
    dropped before the transition existed."""
    _seed(env, [_rec(1, NEW), _rec(2, NEW), _rec(3, GENERATED, comment="d")])
    with open(env.timeline / "lifecycle_new_20260711_000000.json", "w", encoding="utf-8") as f:
        json.dump({"quality_posts": [{"url": ACTIVITY.format(1)}]}, f)
    with open(env.timeline / "ai_posts_curated_20260728_000000.json", "w", encoding="utf-8") as f:
        json.dump({"quality_posts": [{"url": ACTIVITY.format(3)}]}, f)

    affected = post_store.backfill_evaluator_rejected("demo")
    # Post 1 was offered and is still NEW. Post 2 was never offered. Post 3 was
    # offered but is GENERATED, so it succeeded — not a zombie.
    assert [r["key"] for r in affected] == [ACTIVITY.format(1)]
    # Dry by default.
    assert PostStore("demo").get(ACTIVITY.format(1))["status"] == NEW


def test_backfill_applies_and_is_restorable(env):
    _seed(env, [_rec(1, NEW)])
    with open(env.timeline / "lifecycle_new_1.json", "w", encoding="utf-8") as f:
        json.dump({"quality_posts": [{"url": ACTIVITY.format(1)}]}, f)

    post_store.backfill_evaluator_rejected("demo", apply=True)
    store = PostStore("demo")
    assert store.get(ACTIVITY.format(1))["status"] == TRASH
    assert store.get(ACTIVITY.format(1))["trash_reason"] == REASON_EVALUATOR_REJECTED
    assert store.restore(ACTIVITY.format(1)) is True


def test_backfill_ignores_raw_scrape_files(env):
    """Being scraped is not the same as being offered to the evaluator."""
    _seed(env, [_rec(1, NEW)])
    with open(env.timeline / "ai_posts_20260728_000000.json", "w", encoding="utf-8") as f:
        json.dump({"quality_posts": [{"url": ACTIVITY.format(1)}]}, f)
    assert post_store.backfill_evaluator_rejected("demo") == []


# ══════════════════════════════════════════════════════════════════════════════
# DEFECT B — the review queue is the GENERATED bin
# ══════════════════════════════════════════════════════════════════════════════

def test_review_queue_equals_generated_bin_when_nothing_reviewed(env, client):
    _seed(env, [_rec(i, GENERATED, comment=f"draft {i}") for i in range(1, 6)])
    data = client.get("/api/comments/demo").get_json()
    assert len(data["comments"]) == data["counts"]["GENERATED"] == 5
    assert data["reviewed_count"] == 0
    assert data["source"] == "lifecycle_store"


def test_review_queue_plus_reviewed_always_reconstructs_the_bin(env, client):
    """The invariant that replaces 'the queue is empty because files moved'."""
    _seed(env, [_rec(1, GENERATED, comment="a"),
                _rec(2, GENERATED, comment="b", reviewed_at="2026-07-29T00:00:00"),
                _rec(3, GENERATED, comment="c", reviewed_at="2026-07-29T00:00:00")])
    data = client.get("/api/comments/demo").get_json()
    assert len(data["comments"]) == 1
    assert data["reviewed_count"] == 2
    assert len(data["comments"]) + data["reviewed_count"] == data["counts"]["GENERATED"] == 3


def test_review_queue_survives_deletion_of_every_comment_file(env, client):
    """The drafts live on the record. No file anywhere → queue still full."""
    _seed(env, [_rec(i, GENERATED, comment=f"draft {i}") for i in range(1, 4)])
    assert not os.listdir(env.comments)
    data = client.get("/api/comments/demo").get_json()
    assert len(data["comments"]) == 3
    assert all(c["comment"].startswith("draft") for c in data["comments"])


def test_saving_marks_reviewed_in_the_store_and_persists_edits(env, client):
    _seed(env, [_rec(1, GENERATED, comment="original")])
    resp = client.post("/api/comments/demo/save", json={"comments": [
        {"key": ACTIVITY.format(1), "url": ACTIVITY.format(1), "comment": "edited by user"}]})
    assert resp.status_code == 200
    rec = PostStore("demo").get(ACTIVITY.format(1))
    assert rec["comment"] == "edited by user"     # the store, not just the TXT
    assert rec["reviewed_at"] is not None
    assert rec["status"] == GENERATED             # reviewing is not posting


def test_regenerating_clears_the_reviewed_flag(env):
    """A new draft must go back through review rather than inheriting approval."""
    store = PostStore("demo", path=str(env.store_path))
    store.posts[ACTIVITY.format(1)] = _rec(1, GENERATED, comment="old",
                                           reviewed_at="2026-07-29T00:00:00")
    store.mark_generated(ACTIVITY.format(1), "brand new draft")
    assert store.get(ACTIVITY.format(1))["reviewed_at"] is None


def test_include_reviewed_returns_the_whole_bin(env, client):
    _seed(env, [_rec(1, GENERATED, comment="a"),
                _rec(2, GENERATED, comment="b", reviewed_at="2026-07-29T00:00:00")])
    data = client.get("/api/comments/demo?include_reviewed=1").get_json()
    assert len(data["comments"]) == data["counts"]["GENERATED"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# Post step — sourced from the store, never from a stale file
# ══════════════════════════════════════════════════════════════════════════════

def test_post_step_prefers_reviewed_drafts(env, client, monkeypatch):
    _seed(env, [_rec(1, GENERATED, comment="unreviewed"),
                _rec(2, GENERATED, comment="approved", reviewed_at="2026-07-29T00:00:00")])
    captured = {}
    monkeypatch.setattr(dash, "run_job",
                        lambda job_id, fn, *a, **k: captured.update(args=a))
    monkeypatch.setattr(dash, "can_start_browser_task", lambda *a, **k: True)

    assert client.post("/api/post/demo", json={"count": 1}).status_code == 200
    body = open(captured["args"][1], encoding="utf-8").read()
    assert body.index("approved") < body.index("unreviewed")


# ══════════════════════════════════════════════════════════════════════════════
# DEFECT A — client contract (static assertions on the rendering path)
# ══════════════════════════════════════════════════════════════════════════════

def test_new_chip_reads_the_server_like_every_other_chip(client_source):
    """THE defect A line. ``onclick="goStep('posts')"`` rendered the stale
    postsData cache — the reason the tab showed nothing while the chip said 49."""
    chips = re.findall(r'class="lc-chip lc-(\w+)"[^>]*onclick="([^"]+)"', client_source)
    handlers = dict(chips)
    assert set(handlers) == {"new", "generated", "commented", "trash"}
    assert handlers["new"] == "showNewPosts()", (
        "the New chip must do a fresh store read, not render the client cache"
    )
    for status in ("generated", "commented", "trash"):
        assert handlers[status].startswith("showLifecycle(")


def test_show_new_posts_fetches_before_rendering(client_source):
    body = _function_body(client_source, "showNewPosts")
    assert "await loadExistingPosts()" in body
    assert "renderPosts()" in body


def test_go_step_reloads_every_panel_whose_cache_is_empty(client_source):
    """Defect A's other half: goStep('posts') had no reload-if-empty fallback
    (goStep('review') did), so the tab stayed empty for the rest of the session."""
    body = _function_body(client_source, "goStep")
    for cache, loader in (("postsData.posts", "loadExistingPosts"),
                          ("commentsData.comments", "loadExistingComments"),
                          ("postQueue", "loadPostQueue")):
        assert cache in body and loader in body, f"goStep must reload {cache} when empty"


def test_load_existing_posts_can_refill_a_cleared_cache(client_source):
    """The old ``if (data.posts.length > 0)`` guard meant an emptied cache could
    never be refilled from the server."""
    body = _function_body(client_source, "loadExistingPosts")
    assert "postsData = data;" in body
    assert "data.posts.length > 0" not in body


def test_generate_never_posts_an_input_file(client_source):
    """Defect D at the client: sending input_file bypassed the store-driven path."""
    body = _code_only(_function_body(client_source, "startGenerate"))
    assert "input_file" not in body
    assert "savedPostsFile" not in _code_only(client_source), \
        "the curated-file override is gone"


def test_mutations_re_read_the_store_instead_of_splicing_the_cache(client_source):
    for fn in ("rejectPost", "removeUncheckedPosts"):
        assert "loadExistingPosts" in _function_body(client_source, fn), (
            f"{fn} must re-read the store; the server owns the resulting bin state"
        )


def test_empty_saves_are_blocked_client_side(client_source):
    """Empty saves produced the `Total: 0` artifacts that broke the Post step."""
    assert "if (!kept.length)" in _function_body(client_source, "savePosts")
    assert "if (!comments.length)" in _function_body(client_source, "saveComments")


def test_review_render_explains_a_short_queue(client_source):
    """Never show a filtered subset without saying what was filtered — that
    ambiguity is what made 87 GENERATED / 0 shown look like data loss."""
    body = _function_body(client_source, "renderComments")
    assert "reviewed_count" in body
    assert "already reviewed" in body


def _code_only(source):
    """Strip ``//`` line comments so an assertion tests code, not prose."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())


def _function_body(source, name):
    """Extract a top-level JS function body by brace matching."""
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                      source)
    assert match, f"function {name} not found in dashboard.html"
    depth, i = 0, match.end() - 1
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[match.end():i]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}")


# ══════════════════════════════════════════════════════════════════════════════
# Recruiter job-ad filter
# ══════════════════════════════════════════════════════════════════════════════

RECRUITER_ADS = [
    "NOW HIRING – Entry-Level Business Development Representatives 📍 Farmington "
    "Hills, Michigan, USA 💼 Full-Time | On-Site 💰 Competitive Salary",
    "🚀 Hiring – Computer Vision Engineers (3 Positions) 📍 Location: On-site "
    "🕘 Timings: 8:00 AM – 5:00 PM 💼 Experience: 1 – 1.5 Years",
    "We're Hiring: AI Engineer – Agentic AI and LLMs Location: Lahore "
    "Job Type: Full-Time (Onsite) Experience: 0-2 years",
    "🚀 Hiring: AI Engineer (GenAI Engineer) 📍 Location: Remote (India) "
    "💼 Experience: 3–8 Years Required Skills: Python",
    "We're expanding our AI/ML teams and currently hiring across multiple levels "
    "and locations 🔹 Associate AI/ML Engineer (1–2 years) 🔹 Senior (5-8 years)",
]

# Real posts this profile drafted or posted a comment on. Filtering these would
# destroy genuine engagement, so they are the calibration floor.
LEGITIMATE_POSTS = [
    "I'm hiring for a new role at the intersection of quantitative research and "
    "machine learning. At CFM, we operate in a domain where data is non-stationary "
    "and the signal-to-noise ratio is brutal.",
    "I'm one month in at Runway and the energy here is incredible. We're hiring "
    "across the company, but I'm spotlighting a few roles within the product org.",
    "❗️ We're hiring ❗️ Please like, comment, share for reach. Our lab automates "
    "distillation of small language models.",
    "TCS doubles down on AI: 8,900 deployment engineers. The company added 9,300 "
    "employees during the quarter, even as much of the global IT industry remains "
    "cautious on hiring.",
    "We're Hiring: AI Engineer (LLMs | Agentic AI | RAG). We're looking for an "
    "experienced AI Engineer to join our team and build production-ready AI.",
    "Most people think LLMs think. They don't. Every response comes from prediction.",
]


@pytest.mark.parametrize("text", RECRUITER_ADS)
def test_recruiter_job_ads_are_caught(text):
    assert is_recruiter_job_ad(text) is True
    assert classify_ad("Recruiter", text, "https://linkedin.com/in/rec", False) == \
        "job/recommendation card"


@pytest.mark.parametrize("text", LEGITIMATE_POSTS)
def test_legitimate_posts_are_never_filtered(text):
    """Zero false positives across all 297 posts this profile engaged with —
    these are the representative hard cases from that set."""
    assert is_recruiter_job_ad(text) is False
    assert classify_ad("Author", text, "https://linkedin.com/in/author", False) is None


def test_recruiter_filter_needs_both_a_headline_and_listing_structure():
    """Either signal alone is not enough — that is what protects the posts above."""
    assert is_recruiter_job_ad("We're hiring! Come join us, it's a great team.") is False
    assert is_recruiter_job_ad("Location: Remote. Experience: 5 years. Salary: high.") is False
    assert is_recruiter_job_ad(
        "We're hiring. Location: Remote. Experience: 5 years.") is True


def test_recruiter_ad_lands_in_trash_not_new(env):
    """End to end: the filter's verdict becomes a lifecycle state."""
    store = PostStore("demo", path=str(env.store_path))
    reason = classify_ad("Recruiter", RECRUITER_ADS[0], "https://linkedin.com/in/r", False)
    store.upsert_scraped(
        {"url": ACTIVITY.format(9), "text": RECRUITER_ADS[0], "author_name": "Recruiter"},
        status=TRASH, reason=post_store.ad_reason_to_trash_reason(reason))
    assert store.counts()["NEW"] == 0
    assert store.get(ACTIVITY.format(9))["trash_reason"] == post_store.REASON_JOB


# ══════════════════════════════════════════════════════════════════════════════
# Cross-layer invariants
# ══════════════════════════════════════════════════════════════════════════════

def test_every_pipeline_list_reports_the_same_counts(env, client):
    """The three lists the user walks through all report the same bin object,
    so no two views of the pipeline can disagree about the numbers."""
    _seed(env, [_rec(1, NEW), _rec(2, NEW),
                _rec(3, GENERATED, comment="d"),
                _rec(4, COMMENTED, comment="posted")])
    posts = client.get("/api/posts/demo").get_json()
    comments = client.get("/api/comments/demo").get_json()
    lifecycle = client.get("/api/posts/demo/lifecycle").get_json()

    assert posts["counts"] == comments["counts"] == lifecycle["counts"]
    assert len(posts["posts"]) == lifecycle["counts"]["NEW"] == 2
    assert len(comments["comments"]) == lifecycle["counts"]["GENERATED"] == 1


def test_HUMAN_CHECKLIST_for_the_rendering_path():
    """NOT A TEST — an executable reminder of what this suite cannot prove.

    Everything above asserts source text and HTTP responses. None of it renders
    a page. Before trusting the client-side fix, a human must open
    http://localhost:6500 and confirm, in the browser:

      1. NEW CHIP → the "New <n>" chip opens Review Posts showing exactly n
         cards. (Was: chip 49, tab empty.)
      2. REVIEW COMMENTS → step 04 shows the same number as the Generated chip.
         (Was: chip 87, step 04 empty.)
      3. CACHE INVALIDATION → reject a post, navigate away to Scrape, come back
         to Review Posts: the list is still populated and the chip agrees.
      4. GENERATE → click "Save & Continue" on Review Posts, then Generate.
         Confirm the job log names a `lifecycle_new_*.json` input file, NOT an
         `ai_posts_curated_*.json`. (Was: the curated file, sometimes empty.)
      5. POST STEP → step 05 previews the drafts and posting starts from them,
         with no `Total: 0` file involved.
      6. TRASH VIEW → evaluator-rejected posts read "Rejected by evaluator" and
         the Restore button returns them to NEW.

    Items 1-3 and 5-6 are pure rendering; item 4 is observable in the job log.
    """
    assert True
