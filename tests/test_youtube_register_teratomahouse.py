"""Teratoma House's YouTube register is the strategist's text, verbatim (D-033
Y2-T, DEC-005, DEC-077), and a profile picks it by name.

``tests/fixtures/engagement/youtube-register-teratomahouse.md`` is a copy of
``content/engagement/youtube-register-teratomahouse.md`` taken at build time;
nothing reads the strategy repo here or at runtime. Top Five's register, and
LinkedIn's and X's, are pinned by ``test_youtube_policy_golden.py``, which
this phase leaves as it was.
"""

import dataclasses
import os

import pytest

from linkedin_automation import comment_generator as gen
from linkedin_automation import platform_policy as pp
from linkedin_automation import profile_manager as pm
from linkedin_automation import youtube_engage as ye
from register_markdown import parse_register
import test_youtube_engage as base
from test_youtube_engage import PROFILE, FakeApi, FakeGenerator, NOW, store, write_config

env = base.env

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTER = os.path.join(HERE, "fixtures", "engagement", "youtube-register-teratomahouse.md")
TOPFIVE_REGISTER = os.path.join(HERE, "fixtures", "engagement", "youtube-register.md")
NAME = "teratomahouse"

LENGTH_FIELDS = [f.name for f in dataclasses.fields(pp.LengthPolicy)]
REGISTER_FIELDS = [f.name for f in dataclasses.fields(pp.Register)]


def terato(variant=None):
    return pp.policy_for("youtube", variant, NAME)


@pytest.fixture(scope="module")
def register():
    with open(REGISTER, encoding="utf-8") as f:
        return parse_register(f.read())


# ── verbatim ────────────────────────────────────────────────────────────────

def test_the_register_copy_parses_into_every_section(register):
    assert set(register) == {"length", "register", "reply"}
    assert set(register["reply"]) == {"system_voice", "extra_rules"}


@pytest.mark.parametrize("field", LENGTH_FIELDS)
def test_length_field_matches_the_register(register, field):
    assert field in register["length"], "the register has no length.%s" % field
    assert getattr(terato().length, field) == register["length"][field]


@pytest.mark.parametrize("field", [f for f in REGISTER_FIELDS if f != "style_examples"])
def test_register_field_matches_the_register(register, field):
    assert field in register["register"], "the register has no register.%s" % field
    assert getattr(terato().register, field) == register["register"][field]


def test_the_register_declares_nothing_the_policy_drops(register):
    assert set(register["length"]) <= set(LENGTH_FIELDS)
    assert set(register["register"]) <= set(REGISTER_FIELDS)
    assert terato().register.style_examples == {}


def test_the_reply_variant_differs_only_where_the_register_says(register):
    main, reply = terato(), terato(pp.REPLY)
    assert reply.register.system_voice == register["reply"]["system_voice"]
    assert reply.register.extra_rules == register["reply"]["extra_rules"]
    assert reply.length == main.length and reply.input_chars == main.input_chars == 3000
    for field in REGISTER_FIELDS:
        if field not in ("system_voice", "extra_rules"):
            assert getattr(reply.register, field) == getattr(main.register, field), field


def test_some_literal_text_from_the_file_is_in_the_policy():
    """Spot checks on the raw file, not through the parser."""
    with open(REGISTER, encoding="utf-8") as f:
        raw = f.read()
    for s in ("a horror fan reacting to this one video",
              "rule 7 contradicting rule 2 is the scariest part",
              "Never claim a real paranormal experience",
              "If they float a theory, you may hint"):
        assert s in raw
    assert terato().register.voice_identity == "a horror fan reacting to this one video"
    assert "rule 7 contradicting rule 2 is the scariest part" in terato().register.short_examples
    assert "Never claim a real paranormal experience" in terato().register.extra_rules
    assert "If they float a theory, you may hint" in terato(pp.REPLY).register.extra_rules


def test_the_two_registers_differ_and_the_default_is_top_fives(register):
    with open(TOPFIVE_REGISTER, encoding="utf-8") as f:
        topfive = parse_register(f.read())
    default = pp.policy_for("youtube")
    assert pp.policy_for("youtube", None, pp.DEFAULT_REGISTER) is default
    assert pp.policy_for("youtube", pp.REPLY, pp.DEFAULT_REGISTER) is pp.policy_for(
        "youtube", pp.REPLY)
    assert default.register.system_voice == topfive["register"]["system_voice"]
    assert terato().register.system_voice != default.register.system_voice


@pytest.mark.parametrize("platform,variant,name", [
    ("youtube", None, "nosuch"), ("linkedin", None, NAME), ("x", None, NAME),
    ("youtube", "thread", NAME)])
def test_an_unknown_register_or_variant_raises_and_never_falls_back(platform, variant, name):
    with pytest.raises(pp.UnknownPlatform):
        pp.policy_for(platform, variant, name)


def test_register_names():
    assert pp.register_names("youtube") == ("default", NAME)
    assert pp.register_names("linkedin") == ("default",)


# ── per-profile config ──────────────────────────────────────────────────────

def set_register(env, value):
    eng = env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["engagement"]
    eng["register"] = value
    write_config(env["cfg"])


def test_no_register_is_the_default(env):
    assert ye.engage_config(PROFILE).register == "default"


def test_the_register_is_read_from_config(env):
    set_register(env, NAME)
    assert ye.engage_config(PROFILE).register == NAME
    set_register(env, "default")
    assert ye.engage_config(PROFILE).register == "default"


@pytest.mark.parametrize("bad", ["nosuch", "Teratomahouse", "", None, 1, ["teratomahouse"]])
def test_an_unknown_register_is_refused_at_config_load(env, bad):
    set_register(env, bad)
    with pytest.raises(ye.EngageRefused, match="register is .*known registers: default, "
                                               "teratomahouse"):
        ye.engage_config(PROFILE)


def test_the_validator_uses_the_profiles_register(env):
    """"This gave me chills" is banned in Teratoma's register, not Top Five's."""
    rec = {"comment": "This gave me chills, the second voicemail",
           "youtube": {"kind": ye.KIND_VIDEO}}
    assert ye.promotion_refusal(rec, ye.engage_config(PROFILE)) is None
    set_register(env, NAME)
    assert "banned opener 'This gave me chills'" in ye.promotion_refusal(
        rec, ye.engage_config(PROFILE))
    reply = dict(rec, youtube={"kind": ye.KIND_REPLY})
    assert "banned opener" in ye.promotion_refusal(reply, ye.engage_config(PROFILE))


def test_the_default_generator_is_built_in_the_profiles_register(env):
    assert ye.default_generator(PROFILE, None).policy is pp.policy_for("youtube")
    set_register(env, NAME)
    assert ye.default_generator(PROFILE, None).policy is terato()
    assert ye.default_generator(PROFILE, pp.REPLY).policy is terato(pp.REPLY)


def test_draft_flags_a_draft_by_the_profiles_register(env):
    set_register(env, NAME)
    ye.find(PROFILE, now=NOW, api=FakeApi(env), out=env["out"])
    _c, summary = ye.draft(PROFILE, limit=1, now=NOW, api=FakeApi(env), out=env["out"],
                           make_generator=lambda p, v: FakeGenerator(
                               p, v, text="Who else is watching this at 3am"))
    assert summary["flagged"] == summary["drafted"] == ["vidNEW1"]
    meta = store().get("vidNEW1")["comment_meta"]
    assert meta["promotion"] == ["banned opener 'Who else is watching'"]


def test_the_teratoma_prompt_uses_its_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(tmp_path / "p.json"))
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {})
    g = gen.AuthenticCommentGenerator("d.json", profile_name="t", platform="youtube",
                                      register_name=NAME)
    r = g.policy.register
    prompt = g.create_authentic_comment_prompt(
        {"author_name": "A", "text": "Title: t"},
        {"name": "add_insight", "instruction": "i", "example": "e"}, "add_perspective")
    assert r.extra_rules in prompt and r.tone_balance in prompt
    assert "a horror fan reacting to this one video" in prompt
    assert pp.policy_for("youtube").register.extra_rules not in prompt
    ok, issues = g.detect_ai_patterns("i'm not scared but the alert repeating got me")
    assert any("self_positioning_opener" in i for i in issues)
