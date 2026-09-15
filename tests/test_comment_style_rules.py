"""Tests for the three hard comment-style rules in detect_ai_patterns
(comment-style-rules): no dash punctuation, first-person singular voice.
(Tone is prompt-only and not machine-checkable.)

Offline — detect_ai_patterns is pure (string in -> (ok, issues)). No API calls."""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import comment_generator as gen


@pytest.fixture
def g(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file", lambda profile_name=None: str(tmp_path / "progress.json"))
    return gen.AuthenticLinkedInCommentGenerator("dummy.json", model="gpt-4o-mini", profile_name="t",
                                                 platform=gen.platform_policy.LINKEDIN)


def _issue_names(issues):
    return " ".join(issues)


# ─── Rule 1: no dash punctuation ──────────────────────────────────────────────

def test_spaced_hyphen_rejected(g):
    ok, issues = g.detect_ai_patterns("I tried this approach it worked but the latency was bad - so I moved on.")
    assert ok is False
    assert "dash_as_punctuation" in _issue_names(issues)


def test_em_dash_rejected(g):
    ok, issues = g.detect_ai_patterns("I built the pipeline fast — then the schema changed on me.")
    assert ok is False
    assert "dash_as_punctuation" in _issue_names(issues)


def test_en_dash_rejected(g):
    ok, issues = g.detect_ai_patterns("I shipped it quickly – then the metrics dropped a bit.")
    assert ok is False
    assert "dash_as_punctuation" in _issue_names(issues)


def test_compound_word_hyphen_is_fine(g):
    # "real-time" and "fine-tune" are compound words, not punctuation dashes.
    ok, issues = g.detect_ai_patterns("I lean on real-time fine-tune workflows for my models these days.")
    assert ok is True, issues


# ─── Rule 3: first person singular ────────────────────────────────────────────

def test_we_rejected(g):
    ok, issues = g.detect_ai_patterns("We tried this approach and it worked well for the dataset.")
    assert ok is False
    assert "first_person_plural" in _issue_names(issues)


def test_weve_contraction_rejected(g):
    ok, issues = g.detect_ai_patterns("We've shipped this twice and it still surprises me sometimes.")
    assert ok is False
    assert "first_person_plural" in _issue_names(issues)


def test_our_team_rejected(g):
    ok, issues = g.detect_ai_patterns("My approach worked, but our team prefers a different stack here.")
    assert ok is False
    assert "first_person_plural" in _issue_names(issues)


def test_first_person_singular_passes(g):
    ok, issues = g.detect_ai_patterns("I hit this exact wall at 100k tokens and had to rethink my chunking.")
    assert ok is True, issues


# ─── A fully compliant comment passes ─────────────────────────────────────────

def test_clean_compliant_comment_passes(g):
    ok, issues = g.detect_ai_patterns("The latency numbers are wild. I got stuck at 200ms with my own setup.")
    assert ok is True, issues
