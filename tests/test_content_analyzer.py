"""Tests for ContentAnalyzer scoring/keyword breadth (pipeline-fixes / BUG 1).

Pure text-in -> classification-out; no browser. Locks in the broadened keywords
and the relaxed is_ai rule (any keyword match counts)."""

import pytest

from linkedin_automation import post_finder as finder

A = finder.ContentAnalyzer()


def test_ai_agent_post_qualifies():
    text = ("We're seeing huge gains from AI agents and agentic workflows. Our "
            "GPT-based automation now handles most support tickets end to end.")
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is True
    assert score > 0
    assert quality != finder.PostQuality.SKIP


@pytest.mark.parametrize("kw", [
    "automation", "gpt", "agentic", "training data", "fine-tune",
    "rag", "machine learning", "rlhf", "chatbot",
])
def test_broadened_keywords_match(kw):
    text = f"A practical look at {kw} in real production systems this year and beyond."
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is True, f"{kw!r} should mark the post AI-related"
    assert score > 0


def test_single_tier2_keyword_now_counts():
    # Previously a lone tier2 keyword -> is_ai False (dropped). Now it counts.
    text = "Our team relies heavily on machine learning to forecast demand each week."
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is True


def test_recruiting_post_not_ai():
    text = ("We are hiring a senior backend developer. Competitive salary and "
            "benefits. Apply today through our careers page.")
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is False
    assert quality == finder.PostQuality.SKIP


def test_short_text_skipped():
    is_ai, score, kws, quality, ptype = A.analyze("AI")
    assert quality == finder.PostQuality.SKIP
    assert score == 0


def test_non_ai_post_skipped():
    text = "Had a wonderful weekend hiking with the family in the mountains. So refreshing!"
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is False
