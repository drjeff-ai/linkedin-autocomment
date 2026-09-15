"""Tests for the per-profile config system (profile-config branch).

Covers: default-on-first-use, deep-merge (additive), reset, corrupt-config
fallback, finder keyword overrides, generator persona injection, and that the
hard comment-style rules stay enforced even if a config omits them."""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import post_finder as finder
from linkedin_automation import comment_generator as gen


# ─── pm config storage ────────────────────────────────────────────────────────

@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    return tmp_path


def test_creates_default_on_first_use(cfg_dir):
    cfg = pm.get_profile_config("x")
    assert (cfg_dir / "profile_config.json").exists()
    assert "post_finder" in cfg and "comment_generator" in cfg and "connector" in cfg
    assert cfg["post_finder"]["keywords_tier1"]


def test_deep_merge_user_wins_defaults_fill(cfg_dir):
    pm.save_profile_config("x", {"post_finder": {"min_quality_score": 9}})
    cfg = pm.get_profile_config("x")
    assert cfg["post_finder"]["min_quality_score"] == 9          # user value wins
    assert cfg["post_finder"]["keywords_tier1"]                  # filled from default
    assert "comment_generator" in cfg                            # filled from default


def test_reset_regenerates_default(cfg_dir):
    pm.save_profile_config("x", {"display_name": "Custom Name"})
    pm.reset_profile_config("x")
    assert pm.get_profile_config("x")["display_name"] == ""


def test_corrupt_config_falls_back(cfg_dir):
    (cfg_dir / "profile_config.json").write_text("{ not valid json", encoding="utf-8")
    cfg = pm.get_profile_config("x")   # must not raise
    assert "post_finder" in cfg


def test_load_default_config_structure():
    d = pm.load_default_config()
    assert {"display_name", "headline", "niche",
            "post_finder", "comment_generator", "connector"}.issubset(d.keys())


def test_deep_merge_helper():
    base = {"a": 1, "b": {"x": 1, "y": 2}}
    over = {"b": {"y": 9, "z": 3}, "c": 4}
    assert pm._deep_merge(base, over) == {"a": 1, "b": {"x": 1, "y": 9, "z": 3}, "c": 4}


# ─── finder keyword overrides ─────────────────────────────────────────────────

def test_analyzer_keyword_override():
    a = finder.ContentAnalyzer(keywords_tier1=["ai vfx", "virtual production"], keywords_tier2=["sora"])
    assert "ai vfx" in a.ai_discussion_keywords["tier1"]
    assert "sora" in a.ai_discussion_keywords["tier2"]
    assert a.ai_discussion_keywords["tier3"]  # tier3 keeps built-in defaults
    is_ai, score, kw, q, pt = a.analyze(
        "Loving the new AI VFX tools. Sora is wild for virtual production previs work these days."
    )
    assert is_ai is True


def test_analyzer_defaults_when_no_override():
    a = finder.ContentAnalyzer()
    assert "machine learning" in a.ai_discussion_keywords["tier2"]
    assert "claude" in a.ai_discussion_keywords["tier1"]


# ─── generator persona injection + hard-rule independence ─────────────────────

def _make_generator(tmp_path, monkeypatch, config):
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file", lambda profile_name=None: str(tmp_path / "p.json"))
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config)
    return gen.AuthenticLinkedInCommentGenerator("dummy.json", model="gpt-4o-mini", profile_name="t",
                                                 platform=gen.platform_policy.LINKEDIN)


def test_persona_block_injected_from_config(tmp_path, monkeypatch):
    g = _make_generator(tmp_path, monkeypatch, {"comment_generator": {
        "persona": "AI Tech Lead in VFX", "tone": "warm and clever",
        "topics_of_expertise": ["virtual production"], "things_to_avoid": ["being salesy"],
        "style_mix": {"add_insight": 0.3}, "comment_length_range": [15, 60],
    }})
    block = g._build_persona_block()
    assert "AI Tech Lead in VFX" in block
    assert "virtual production" in block
    assert "being salesy" in block


def test_persona_block_empty_without_config(tmp_path, monkeypatch):
    g = _make_generator(tmp_path, monkeypatch, {})
    assert g._build_persona_block() == ""


def test_hard_rules_enforced_even_if_config_omits_them(tmp_path, monkeypatch):
    # Config explicitly removes the dash/we guidance from things_to_avoid;
    # detect_ai_patterns must STILL reject them (item 6).
    g = _make_generator(tmp_path, monkeypatch, {"comment_generator": {"things_to_avoid": []}})
    ok, issues = g.detect_ai_patterns("We tried this approach - it worked for the team.")
    assert ok is False
    assert any("dash_as_punctuation" in i for i in issues)
    assert any("first_person_plural" in i for i in issues)
