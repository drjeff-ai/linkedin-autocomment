"""The YouTube policy is the register, verbatim (D-027 Y1, DEC-005, DEC-061(6)).

``tests/fixtures/engagement/youtube-register.md`` is a copy of the strategist's
``content/engagement/youtube-register.md``, taken at build time. Nothing reads
the strategy repo at runtime or here. If Jeff edits the register, the copy is
retaken and ``platform_policy.py`` re-copied; this test then says whether the
two agree, field by field.

The second half pins LinkedIn and X to their values at main 913a5ec
(``tests/goldens/platform_policies_main.json``): adding a platform must not move
either by one byte.
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
REGISTER = os.path.join(HERE, "fixtures", "engagement", "youtube-register.md")
MAIN_SNAPSHOT = os.path.join(HERE, "goldens", "platform_policies_main.json")

LENGTH_FIELDS = [f.name for f in dataclasses.fields(pp.LengthPolicy)]
REGISTER_FIELDS = [f.name for f in dataclasses.fields(pp.Register)]


@pytest.fixture(scope="module")
def register():
    with open(REGISTER, encoding="utf-8") as f:
        return parse_register(f.read())


def test_the_register_copy_parses_into_every_section(register):
    assert set(register) == {"length", "register", "reply"}
    assert set(register["reply"]) == {"system_voice", "extra_rules"}


@pytest.mark.parametrize("field", LENGTH_FIELDS)
def test_length_field_matches_the_register(register, field):
    assert field in register["length"], "the register has no length.%s" % field
    assert getattr(pp.policy_for("youtube").length, field) == register["length"][field]


@pytest.mark.parametrize("field", [f for f in REGISTER_FIELDS if f != "style_examples"])
def test_register_field_matches_the_register(register, field):
    assert field in register["register"], "the register has no register.%s" % field
    assert getattr(pp.policy_for("youtube").register, field) == register["register"][field]


def test_the_register_declares_nothing_the_policy_drops(register):
    """A field Jeff adds to the register must land somewhere, not vanish."""
    assert set(register["length"]) <= set(LENGTH_FIELDS)
    assert set(register["register"]) <= set(REGISTER_FIELDS)


def test_style_examples_are_not_declared_so_the_shared_table_is_used():
    assert pp.policy_for("youtube").register.style_examples == {}


def test_the_reply_variant_differs_only_where_the_register_says(register):
    main = pp.policy_for("youtube")
    reply = pp.policy_for("youtube", pp.REPLY)
    assert reply.register.system_voice == register["reply"]["system_voice"]
    assert reply.register.extra_rules == register["reply"]["extra_rules"]
    assert reply.length == main.length and reply.input_chars == main.input_chars
    for field in REGISTER_FIELDS:
        if field not in ("system_voice", "extra_rules"):
            assert getattr(reply.register, field) == getattr(main.register, field), field


def test_an_undeclared_variant_raises_and_never_falls_back():
    with pytest.raises(pp.UnknownPlatform):
        pp.policy_for("linkedin", pp.REPLY)
    with pytest.raises(pp.UnknownPlatform):
        pp.policy_for("youtube", "thread")


# ── LinkedIn and X did not move ─────────────────────────────────────────────

def _asdict(policy):
    return json.loads(json.dumps(dataclasses.asdict(policy), ensure_ascii=False))


@pytest.mark.parametrize("platform", ["linkedin", "x"])
def test_linkedin_and_x_policies_are_unchanged_from_main(platform):
    with open(MAIN_SNAPSHOT, encoding="utf-8") as f:
        main = json.load(f)[platform]
    now = _asdict(pp.policy_for(platform))
    # The one field Y1 added; it carries the literal both always used.
    assert now.pop("input_chars") == 500
    assert now == main


def test_linkedin_and_x_quote_500_chars_of_the_item_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(tmp_path / "p.json"))
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {})
    post = {"author_name": "A", "text": "y" * 4000}
    style = {"name": "add_insight", "instruction": "i", "example": "e"}
    for platform, cut in (("linkedin", 500), ("x", 500), ("youtube", 3000)):
        g = gen.AuthenticCommentGenerator("d.json", profile_name="t", platform=platform)
        prompt = g.create_authentic_comment_prompt(post, style, "add_perspective")
        assert ("y" * cut) in prompt and ("y" * (cut + 1)) not in prompt, platform


def test_the_youtube_prompt_uses_the_register_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(tmp_path / "p.json"))
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {})
    g = gen.AuthenticCommentGenerator("d.json", profile_name="t", platform="youtube")
    r = g.policy.register
    prompt = g.create_authentic_comment_prompt(
        {"author_name": "A", "text": "Title: t"},
        {"name": "add_insight", "instruction": "i", "example": "e"}, "add_perspective")
    assert prompt.startswith("Write a YouTube comment with this approach:")
    assert r.extra_rules in prompt and r.tone_balance in prompt
    assert "LinkedIn" not in prompt
    reply = gen.AuthenticCommentGenerator("d.json", profile_name="t", platform="youtube",
                                          variant=pp.REPLY)
    assert reply.register.system_voice.startswith("You're the creator of this Short")


def test_banned_openers_written_capitalised_still_reject():
    """The register writes "Great video"; the detector lowers it."""
    g = gen.AuthenticCommentGenerator.__new__(gen.AuthenticCommentGenerator)
    g.policy = pp.policy_for("youtube")
    g.register, g.length = g.policy.register, g.policy.length
    ok, issues = g.detect_ai_patterns("great video, the eval drift bit is good")
    assert any("self_positioning_opener" in i for i in issues)
    ok, issues = g.detect_ai_patterns("the eval drift bit is the useful part")
    assert not any("self_positioning_opener" in i for i in issues)
