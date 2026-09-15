"""Tests for the loosened engagement evaluation (engagement-eval-fix).

Offline: the OpenAI-calling evaluate_post_quality is monkeypatched, so no API
calls. Locks in: explicit-skip is the only rejection, low advisory scores no
longer reject, missing verdict defaults to engage, and url-less engaged posts
are not selected for generation."""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import comment_generator as gen

URL1 = "https://www.linkedin.com/posts/x-ugcPost-1/"
URL2 = "https://www.linkedin.com/posts/y-ugcPost-2/"


@pytest.fixture
def generator(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file", lambda profile_name=None: str(tmp_path / "progress.json"))
    return gen.AuthenticLinkedInCommentGenerator(
        "dummy.json", model="gpt-4o-mini", profile_name="t",
        platform=gen.platform_policy.LINKEDIN
    )


def _post(url, text):
    return {"url": url, "text": text, "author_name": "A"}


def test_low_scores_no_longer_reject(generator, monkeypatch):
    # Engage verdict with LOW advisory scores must still be selected (the old
    # authenticity<5 / conversation_potential<5 gates are gone).
    monkeypatch.setattr(generator, "evaluate_post_quality", lambda post: {
        "verdict": "engage", "conversation_potential": 2, "authenticity": 1,
        "reason": "low scores but on-topic",
    })
    posts = [_post(URL1, "A solid post about LLMs and AI agents in production today.")]
    selected = generator.select_best_posts(posts)
    assert len(selected) == 1
    assert selected[0]["url"] == URL1


def test_explicit_skip_is_rejected(generator, monkeypatch):
    monkeypatch.setattr(generator, "evaluate_post_quality", lambda post: {
        "verdict": "skip", "conversation_potential": 9, "authenticity": 9,
        "reason": "explicitly off-topic",
    })
    posts = [_post(URL1, "A personal life update with no professional angle at all here.")]
    assert generator.select_best_posts(posts) == []


def test_missing_verdict_defaults_to_engage(generator, monkeypatch):
    monkeypatch.setattr(generator, "evaluate_post_quality", lambda post: {"reason": "no verdict field"})
    posts = [_post(URL1, "AI automation and machine learning post with enough text to evaluate.")]
    selected = generator.select_best_posts(posts)
    assert len(selected) == 1


def test_engaged_but_no_url_not_selected(generator, monkeypatch):
    # Worth engaging, but no URL -> cannot post -> excluded from generation set.
    monkeypatch.setattr(generator, "evaluate_post_quality", lambda post: {
        "verdict": "engage", "conversation_potential": 8, "authenticity": 8, "reason": "good",
    })
    posts = [{"text": "Great AI thought piece about agents and automation here.", "author_name": "A"}]
    assert generator.select_best_posts(posts) == []


def test_mixed_batch(generator, monkeypatch):
    verdicts = iter([
        {"verdict": "engage", "conversation_potential": 2, "authenticity": 2, "reason": "engage"},
        {"verdict": "skip", "conversation_potential": 5, "authenticity": 5, "reason": "skip"},
    ])
    monkeypatch.setattr(generator, "evaluate_post_quality", lambda post: next(verdicts))
    posts = [_post(URL1, "AI post one with enough text to be evaluated by the model."),
             _post(URL2, "AI post two with enough text to be evaluated by the model.")]
    selected = generator.select_best_posts(posts)
    assert [p["url"] for p in selected] == [URL1]


def test_prompt_is_inclusive(generator):
    prompt = generator.enhanced_quality_filter_prompt({"text": "some AI post", "author_name": "A"})
    low = prompt.lower()
    assert "inclusive" in low
    assert "when in doubt" in low
    assert "engage" in low
