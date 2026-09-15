"""Tests for uncapped comment generation (uncap-comment-generation).

Comment generation is OpenAI-only (no LinkedIn interaction), so the old
``daily_limit`` cap is now an optional ``max_comments`` ceiling that defaults to
None (no cap). These tests lock in: default is uncapped, None generates for ALL
engaging posts, an explicit cap slices exactly, and a large uncapped run proceeds
(with a warning). OpenAI is never called — evaluate/generate are monkeypatched.
"""

import logging

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import comment_generator as gen


@pytest.fixture
def make_generator(tmp_path, monkeypatch):
    """Factory for a generator with all storage redirected to tmp and no sleeps."""
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file", lambda profile_name=None: str(tmp_path / "progress.json"))
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    monkeypatch.setattr(gen.time, "sleep", lambda *a, **k: None)

    def _make(max_comments=None):
        return gen.AuthenticLinkedInCommentGenerator(
            "dummy.json", model="gpt-4o-mini", max_comments=max_comments, profile_name="t",
            platform=gen.platform_policy.LINKEDIN
        )
    return _make


def _posts(n):
    return [
        {
            "url": f"https://www.linkedin.com/posts/p-{i}/",
            "text": f"AI agents and automation post number {i}, with enough body to evaluate.",
            "author_name": f"Author {i}",
        }
        for i in range(n)
    ]


def _engage_all(generator, monkeypatch):
    monkeypatch.setattr(generator, "evaluate_post_quality", lambda post: {
        "verdict": "engage", "conversation_potential": 5, "authenticity": 5,
        "post_category": "technical_discussion", "reason": "on-topic",
    })


def _fake_comment(generator, monkeypatch):
    monkeypatch.setattr(generator, "generate_comment", lambda post, ev: {
        "comment": "Genuinely useful point.", "word_count": 3,
        "style": "add_insight", "approach": "add_perspective", "generated_at": "2026-06-30T00:00:00",
    })


# ─── Rename + default ─────────────────────────────────────────────────────────

def test_default_is_uncapped(make_generator):
    g = make_generator()
    assert g.max_comments is None
    assert not hasattr(g, "daily_limit")   # renamed, not aliased


def test_constructor_accepts_explicit_cap(make_generator):
    assert make_generator(max_comments=5).max_comments == 5


# ─── select_best_posts: cap vs. no cap ────────────────────────────────────────

def test_no_cap_returns_all_engaging(make_generator, monkeypatch):
    g = make_generator(max_comments=None)
    _engage_all(g, monkeypatch)
    selected = g.select_best_posts(_posts(7))
    assert len(selected) == 7


def test_explicit_cap_slices_exactly(make_generator, monkeypatch):
    g = make_generator(max_comments=5)
    _engage_all(g, monkeypatch)
    selected = g.select_best_posts(_posts(7))
    assert len(selected) == 5


def test_cap_larger_than_available_returns_all(make_generator, monkeypatch):
    g = make_generator(max_comments=20)
    _engage_all(g, monkeypatch)
    assert len(g.select_best_posts(_posts(7))) == 7


# ─── generate_all_comments: end-to-end count ──────────────────────────────────

def test_generate_all_comments_uncapped_does_all(make_generator, monkeypatch):
    g = make_generator(max_comments=None)
    _engage_all(g, monkeypatch)
    _fake_comment(g, monkeypatch)
    monkeypatch.setattr(g, "load_posts", lambda: {"quality_posts": _posts(8)})
    results = g.generate_all_comments()
    assert len(results) == 8


def test_generate_all_comments_capped(make_generator, monkeypatch):
    g = make_generator(max_comments=3)
    _engage_all(g, monkeypatch)
    _fake_comment(g, monkeypatch)
    monkeypatch.setattr(g, "load_posts", lambda: {"quality_posts": _posts(8)})
    results = g.generate_all_comments()
    assert len(results) == 3


# ─── Cost guard: large uncapped run warns but proceeds ────────────────────────

def test_large_uncapped_run_warns_but_proceeds(make_generator, monkeypatch, caplog):
    g = make_generator(max_comments=None)
    _engage_all(g, monkeypatch)
    _fake_comment(g, monkeypatch)
    monkeypatch.setattr(g, "load_posts", lambda: {"quality_posts": _posts(100)})
    with caplog.at_level(logging.WARNING):
        results = g.generate_all_comments()
    assert len(results) == 100                       # proceeded
    assert any("in one run" in r.getMessage() for r in caplog.records)   # warned
