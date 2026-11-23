"""The Instagram policy is the register, verbatim (D-027 E2, DEC-005, DEC-061(6)).

``tests/fixtures/engagement/instagram-register.md`` is a copy of the
strategist's ``content/engagement/instagram-register.md``, taken at build
time. Nothing reads the strategy repo at runtime or here. If Jeff edits the
register, the copy is retaken and ``platform_policy.py`` re-copied; this test
then says whether the two agree, field by field.

The second half pins LinkedIn and X to their values at main 913a5ec, and
YouTube to its own register copy: adding Instagram moved none of them.
"""

import dataclasses
import json
import os

import pytest

from linkedin_automation import comment_generator as gen
from linkedin_automation import platform_policy as pp
from linkedin_automation import profile_manager as pm
from register_markdown import parse_register

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTER = os.path.join(HERE, "fixtures", "engagement", "instagram-register.md")
YT_REGISTER = os.path.join(HERE, "fixtures", "engagement", "youtube-register.md")
MAIN_SNAPSHOT = os.path.join(HERE, "goldens", "platform_policies_main.json")

LENGTH_FIELDS = [f.name for f in dataclasses.fields(pp.LengthPolicy)]
REGISTER_FIELDS = [f.name for f in dataclasses.fields(pp.Register)]


@pytest.fixture(scope="module")
def register():
    with open(REGISTER, encoding="utf-8") as f:
        return parse_register(f.read())


def test_the_register_copy_parses_into_its_two_sections(register):
    assert set(register) == {"length", "register"}


@pytest.mark.parametrize("field", LENGTH_FIELDS)
def test_length_field_matches_the_register(register, field):
    assert field in register["length"], "the register has no length.%s" % field
    assert getattr(pp.policy_for("instagram").length, field) == register["length"][field]


@pytest.mark.parametrize("field", [f for f in REGISTER_FIELDS if f != "style_examples"])
def test_register_field_matches_the_register(register, field):
    assert field in register["register"], "the register has no register.%s" % field
    assert getattr(pp.policy_for("instagram").register, field) == register["register"][field]


def test_the_register_declares_nothing_the_policy_drops(register):
    assert set(register["length"]) <= set(LENGTH_FIELDS)
    assert set(register["register"]) <= set(REGISTER_FIELDS)


def test_instagram_keeps_the_shared_defaults_it_does_not_declare():
    policy = pp.policy_for("instagram")
    assert policy.register.style_examples == {}
    assert policy.input_chars == 500
    assert policy.length.hard_max == 300 and policy.length.unit == pp.CHARS


def test_instagram_declares_no_variant():
    with pytest.raises(pp.UnknownPlatform):
        pp.policy_for("instagram", pp.REPLY)


# ── LinkedIn, X and YouTube did not move ────────────────────────────────────

def _asdict(policy):
    return json.loads(json.dumps(dataclasses.asdict(policy), ensure_ascii=False))


@pytest.mark.parametrize("platform", ["linkedin", "x"])
def test_linkedin_and_x_are_unchanged_from_main(platform):
    with open(MAIN_SNAPSHOT, encoding="utf-8") as f:
        main = json.load(f)[platform]
    now = _asdict(pp.policy_for(platform))
    assert now.pop("input_chars") == 500
    assert now == main


def test_youtube_still_equals_its_register():
    with open(YT_REGISTER, encoding="utf-8") as f:
        yt = parse_register(f.read())
    policy = pp.policy_for("youtube")
    for field, value in yt["length"].items():
        assert getattr(policy.length, field) == value, field
    for field, value in yt["register"].items():
        assert getattr(policy.register, field) == value, field


def test_the_instagram_prompt_uses_the_register_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(tmp_path / "p.json"))
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {})
    g = gen.AuthenticCommentGenerator("d.json", profile_name="t", platform="instagram")
    r = g.policy.register
    prompt = g.create_authentic_comment_prompt(
        {"author_name": "synthetic_painter", "text": "page two of the series"},
        {"name": "add_insight", "instruction": "i", "example": "e"}, "add_perspective")
    assert prompt.startswith("Write an Instagram comment with this approach:")
    assert r.extra_rules in prompt and r.tone_balance in prompt
    assert r.craft_rules.format(cap=g.length.typical_cap_phrase) in prompt
    assert "LinkedIn" not in prompt


def test_banned_openers_written_capitalised_still_reject():
    g = gen.AuthenticCommentGenerator.__new__(gen.AuthenticCommentGenerator)
    g.policy = pp.policy_for("instagram")
    g.register, g.length = g.policy.register, g.policy.length
    _ok, issues = g.detect_ai_patterns("wow the lettering on page two")
    assert any("self_positioning_opener" in i for i in issues)
    _ok, issues = g.detect_ai_patterns("the lettering on page two is so clean")
    assert not any("self_positioning_opener" in i for i in issues)
