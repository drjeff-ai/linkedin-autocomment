"""Tests for post generator reading per-profile config (post-generator-config).

Offline: OpenAI client + pm dirs/config are monkeypatched; no API calls."""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import post_generator as g
from linkedin_automation import human_behavior as hb


@pytest.fixture
def make_gen(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    monkeypatch.setattr(g, "OpenAI", lambda **k: object())

    def _make(config):
        monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config)
        return g.PostGenerator(profile_name="t")
    return _make


def test_reads_config_persona_and_topics(make_gen):
    cfg = {"post_generator": {
        "persona": "AI Tech Lead in VFX", "tone": "warm and clever",
        "voice": "I/my only", "topics": ["ai vfx pipelines"],
        "things_to_avoid": ["hashtags"], "styles": {"hot_take": "be bold"},
    }}
    gen = make_gen(cfg)
    assert gen.pg_persona == "AI Tech Lead in VFX"
    assert gen.pg_topics == ["ai vfx pipelines"]
    assert gen.pg_styles == {"hot_take": "be bold"}


def test_system_prompt_includes_config(make_gen):
    cfg = {"post_generator": {
        "persona": "AI VFX lead", "tone": "warm", "voice": "I/my only",
        "topics": ["t"], "things_to_avoid": ["hashtags", "saying we or our"],
    }}
    gen = make_gen(cfg)
    sp = gen._build_system_prompt()
    assert "AI VFX lead" in sp
    assert "warm" in sp
    assert "I/my only" in sp
    assert "hashtags" in sp
    assert "NEVER use hashtags" in sp  # fixed format rules always present
    art = gen._build_system_prompt(article=True)
    assert "article" in art.lower()


def test_falls_back_to_default_persona_without_config(make_gen):
    gen = make_gen({})  # no post_generator section
    assert gen.pg_persona == g.DEFAULT_PERSONA
    assert gen.pg_topics == list(g.TOPICS)          # hardcoded fallback
    assert gen.pg_styles == dict(g.STYLE_INSTRUCTIONS)
    assert "thought leader" in gen._build_system_prompt()


def test_partial_config_uses_defaults_for_missing(make_gen):
    # Only persona set; topics/styles fall back to hardcoded.
    gen = make_gen({"post_generator": {"persona": "Custom persona"}})
    assert gen.pg_persona == "Custom persona"
    assert gen.pg_topics == list(g.TOPICS)
    assert gen.pg_styles == dict(g.STYLE_INSTRUCTIONS)


def test_type_like_human_keys_exists():
    # Strategy 4 of the poster uses this focused-element typing helper.
    assert callable(getattr(hb, "type_like_human_keys", None))
