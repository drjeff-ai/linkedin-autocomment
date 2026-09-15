"""Platform policy: LinkedIn unchanged to the byte, X genuinely different.

Two jobs.

**Job one — prove LinkedIn did not move.** Parameterizing the generator's prompts
is a refactor of the working side, and the only honest proof that a refactor
changed nothing is a comparison against what it produced *before*.
``tests/goldens/linkedin_prompts.json`` was captured by running the generator at
``main`` @ ``cfd71a6`` (pre-A2) across a matrix of profile configs and all four
length buckets. If a rendered LinkedIn prompt differs from its golden by one
character, that is a behaviour change and this file fails.

*If you are here because a golden test failed:* regenerating the goldens defeats
the test. Either the change was unintended and belongs reverted, or LinkedIn's
prompts are meant to change — in which case say so explicitly in the commit and
re-capture deliberately.

**Job two — prove X is not LinkedIn with a smaller budget.** These tests can show
the nouns changed, that "LinkedIn" is absent, and that the length unit is
characters. They **cannot** show the register is right; that needs a human read
(ROADMAP §A2.2), and no assertion here should be mistaken for that check.
"""

import json
import re
import os

import pytest

from linkedin_automation import comment_generator as gen
from linkedin_automation import platform_policy as pp
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "goldens", "linkedin_prompts.json")

# The same matrix the goldens were captured over. Changing it invalidates them.
CONFIGS = {
    "empty": {},
    "full": {"comment_generator": {
        "persona": "AI Tech Lead in VFX", "tone": "warm and clever",
        "voice": "first person, concrete",
        "topics_of_expertise": ["virtual production", "LLM evaluation"],
        "things_to_avoid": ["being salesy", "buzzwords"],
        "style_mix": {"add_insight": 0.3, "ask_question": 0.7},
        "comment_length_range": [15, 60],
    }},
    "persona_only": {"comment_generator": {"persona": "Solo developer"}},
    "length_only": {"comment_generator": {"comment_length_range": [20, 45]}},
}

STYLE = {"name": "add_insight", "instruction": "Add a specific technical detail",
         "example": "The context window bottleneck is real."}
APPROACH = "add_perspective"
POST = {"author_name": "Example Author",
        "text": "A synthetic post body about model evaluation.",
        "likes": 12, "comments": 3}


@pytest.fixture
def make_generator(tmp_path, monkeypatch):
    """Build a generator with storage redirected and a chosen config/platform."""
    def _make(config, platform):
        monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
        monkeypatch.setattr(pm, "get_progress_file",
                            lambda profile_name=None: str(tmp_path / "p.json"))
        monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config)
        return gen.AuthenticCommentGenerator("dummy.json", model="gpt-4o-mini",
                                             profile_name="t", platform=platform)
    return _make


@pytest.fixture(scope="module")
def goldens():
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        return json.load(f)["prompts"]


def _pin_bucket(monkeypatch, index):
    """Force the length-bucket lottery so a prompt renders deterministically."""
    monkeypatch.setattr(gen.random, "choices",
                        lambda seq, weights=None, k=1: [list(seq)[index]])


# ─── Job one: LinkedIn is byte-identical ──────────────────────────────────────

@pytest.mark.parametrize("config_name", sorted(CONFIGS))
def test_linkedin_persona_block_is_byte_identical(make_generator, goldens, config_name):
    g = make_generator(CONFIGS[config_name], pp.LINKEDIN)
    assert g._build_persona_block() == goldens[f"persona_block::{config_name}"]


@pytest.mark.parametrize("config_name", sorted(CONFIGS))
def test_linkedin_quality_filter_prompt_is_byte_identical(make_generator, goldens,
                                                          config_name):
    g = make_generator(CONFIGS[config_name], pp.LINKEDIN)
    assert g.enhanced_quality_filter_prompt(POST) == goldens[f"quality_filter::{config_name}"]


@pytest.mark.parametrize("config_name", sorted(CONFIGS))
@pytest.mark.parametrize("bucket", range(4))
def test_linkedin_generation_prompt_is_byte_identical(make_generator, goldens,
                                                      monkeypatch, config_name, bucket):
    """The big one: the full generation prompt, every config, every length bucket."""
    g = make_generator(CONFIGS[config_name], pp.LINKEDIN)
    _pin_bucket(monkeypatch, bucket)
    rendered = g.create_authentic_comment_prompt(POST, STYLE, APPROACH)
    assert rendered == goldens[f"authentic_comment::{config_name}::bucket{bucket}"]


def test_linkedin_fixed_strings_are_unchanged(make_generator):
    """The literals that are not returned by a prompt builder."""
    g = make_generator({}, pp.LINKEDIN)
    assert g.register.eval_system_message() == \
        "Evaluate LinkedIn posts for authentic engagement potential."
    assert g.register.system_voice == (
        "You're a developer commenting casually on LinkedIn. Write like you're "
        "texting a colleague - direct, sometimes skeptical, no formality. Keep it "
        "short."
    )
    assert g.register.report_header() == "DAILY LINKEDIN COMMENTS\n"
    assert g._relevance_prompt(POST, "a draft comment") == (
        "Decide whether a LinkedIn comment stays on the post's topic.\n\n"
        f"POST:\n{POST['text']}\n\n"
        "COMMENT:\na draft comment\n\n"
        "Does this comment directly respond to what THIS post is about, without "
        "forcing in an unrelated industry, product, or area of expertise the post "
        "did not raise? Answer with exactly YES or NO."
    )


def test_linkedin_length_thresholds_are_unchanged(make_generator):
    g = make_generator({}, pp.LINKEDIN)
    assert g.length.unit == pp.WORDS
    assert g.length.hard_max == 70
    # 70 words passes, 71 fails — the original `word_count > 70`.
    assert g.detect_ai_patterns(" ".join(["word"] * 70))[1] == []
    issues = g.detect_ai_patterns(" ".join(["word"] * 71))[1]
    assert issues == ["too_long: 71 words (aim for <50)"]


# ─── The platform argument is required, everywhere ────────────────────────────

def test_generator_requires_an_explicit_platform(tmp_path, monkeypatch):
    """No default. A default would resolve to LinkedIn and fail invisibly."""
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(tmp_path / "p.json"))
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {})
    with pytest.raises(TypeError):
        gen.AuthenticCommentGenerator("dummy.json", profile_name="t")


def test_policy_for_refuses_an_unknown_platform():
    with pytest.raises(pp.UnknownPlatform):
        pp.policy_for("twitter")


@pytest.mark.parametrize("empty", [None, ""])
def test_policy_for_refuses_an_empty_platform(empty):
    """An empty value must raise, not quietly become LinkedIn."""
    with pytest.raises(pp.UnknownPlatform):
        pp.policy_for(empty)


def test_platform_identifiers_match_the_store():
    """A store addressed as "x" and a policy as "twitter" would silently diverge."""
    assert pp.LINKEDIN == post_store.LINKEDIN
    assert pp.X == post_store.X
    assert set(pp.PLATFORMS) == {post_store.LINKEDIN, post_store.X}


# ─── Job two: X is a different platform, not a shorter LinkedIn ───────────────

def test_x_is_character_bounded_at_280(make_generator):
    g = make_generator({}, pp.X)
    assert g.length.unit == pp.CHARS
    assert g.length.hard_max == 280
    assert g.detect_ai_patterns("x" * 280)[1] == []
    assert g.detect_ai_patterns("x" * 281)[1] == ["too_long: 281 chars (aim for <240)"]


def test_a_valid_linkedin_length_is_invalid_on_x(make_generator):
    """The defect A2.1 exists to fix, as a test.

    A 60-word draft is fine on LinkedIn and roughly 400 characters — unpostable
    on X. The same text must pass one validator and fail the other.
    """
    draft = " ".join(["evaluation"] * 60)          # 60 words, 659 chars
    assert len(draft.split()) == 60 and len(draft) > 280
    assert gen_issues(make_generator, pp.LINKEDIN, draft) == []
    assert any("too_long" in i for i in gen_issues(make_generator, pp.X, draft))


def gen_issues(make_generator, platform, text):
    return make_generator({}, platform).detect_ai_patterns(text)[1]


def test_the_unit_always_travels_with_the_range(make_generator):
    """A bare "15-60" is what let a word band govern a character platform."""
    cfg = {"comment_generator": {"comment_length_range": [15, 60]}}
    assert "15-60 words" in make_generator(cfg, pp.LINKEDIN)._build_persona_block()
    assert "15-60 chars" in make_generator(cfg, pp.X)._build_persona_block()


def test_hard_max_is_a_platform_fact_not_the_persona_preference(make_generator):
    """Config can move the preferred band; it cannot raise the platform ceiling."""
    cfg = {"comment_generator": {"comment_length_range": [400, 900]}}
    g = make_generator(cfg, pp.X)
    assert "400-900 chars" in g._build_persona_block()   # persona preference honoured
    assert g.length.hard_max == 280                       # ceiling unmoved
    assert g.detect_ai_patterns("x" * 300)[1] != []       # and still enforced


def test_x_prompts_never_mention_linkedin(make_generator, monkeypatch):
    """Register leakage check: no X-bound prompt may name the other platform."""
    g = make_generator(CONFIGS["full"], pp.X)
    surfaces = [g.enhanced_quality_filter_prompt(POST),
                g._relevance_prompt(POST, "a draft reply"),
                g._build_persona_block(),
                g.register.system_voice,
                g.register.eval_system_message(),
                g.register.report_header()]
    for bucket in range(4):
        _pin_bucket(monkeypatch, bucket)
        surfaces.append(g.create_authentic_comment_prompt(POST, STYLE, APPROACH))
    for text in surfaces:
        # Absolute, with no carve-out. Even a negative mention ("don't sound like
        # LinkedIn") names the register we are steering away from, and naming a
        # register in a prompt is a way of evoking it.
        assert "linkedin" not in text.lower()


def test_x_uses_its_own_nouns(make_generator, monkeypatch):
    g = make_generator({}, pp.X)
    _pin_bucket(monkeypatch, 0)
    prompt = g.create_authentic_comment_prompt(POST, STYLE, APPROACH)
    assert prompt.startswith("Write an X reply with this approach:")
    assert "TWEET TO RESPOND TO:" in prompt
    assert "Write ONLY the reply text:" in prompt
    assert g.register.report_header() == "DAILY X REPLIES\n"
    # Article agreement: "an X reply", never "a X reply".
    assert "a X " not in prompt


def test_x_register_is_more_than_a_noun_swap(make_generator, monkeypatch):
    """A weak proxy for the human check, NOT a substitute for it (ROADMAP §A2.2).

    This asserts X carries register guidance LinkedIn does not have at all. It
    says nothing about whether that guidance produces the right voice — only a
    human read can say that.
    """
    x = make_generator({}, pp.X)
    li = make_generator({}, pp.LINKEDIN)
    _pin_bucket(monkeypatch, 0)
    x_prompt = x.create_authentic_comment_prompt(POST, STYLE, APPROACH)
    li_prompt = li.create_authentic_comment_prompt(POST, STYLE, APPROACH)

    assert x.register.extra_rules and not li.register.extra_rules
    assert x.register.system_voice != li.register.system_voice
    assert x.register.voice_identity != li.register.voice_identity
    assert x.length.buckets != li.length.buckets
    # X's block must say things LinkedIn's never does.
    for phrase in ("No greeting", "No hashtags", "mid-conversation",
                   "No professional-network register"):
        assert phrase in x_prompt and phrase not in li_prompt


def test_x_never_truncates_an_over_limit_draft(make_generator, monkeypatch):
    """Truncation is not a length policy: return nothing rather than a cut reply."""
    g = make_generator({}, pp.X)
    over_limit = "x" * 400

    class _Msg:
        content = over_limit

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    monkeypatch.setattr(g.client.chat.completions, "create", lambda **kw: _Resp())
    monkeypatch.setattr(g, "check_relevance", lambda post, comment: True)
    monkeypatch.setattr(g, "_log_api_usage", lambda *a, **k: None)

    result = g.generate_comment(POST, {"post_category": "technical_discussion"})

    assert result is None, "an over-limit X draft must not be returned at all"


def test_linkedin_still_returns_a_long_best_attempt(make_generator, monkeypatch):
    """LinkedIn's hard_max is a quality heuristic, so its behaviour is unchanged."""
    g = make_generator({}, pp.LINKEDIN)
    long_draft = " ".join(["evaluation"] * 100)      # 100 words > 70

    class _Msg:
        content = long_draft

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    monkeypatch.setattr(g.client.chat.completions, "create", lambda **kw: _Resp())
    monkeypatch.setattr(g, "check_relevance", lambda post, comment: True)
    monkeypatch.setattr(g, "_log_api_usage", lambda *a, **k: None)

    result = g.generate_comment(POST, {"post_category": "technical_discussion"})

    assert result is not None
    assert result["comment"] == long_draft


# ─── The platform argument is required at the CALL SITES, not just in code ────
#
# The constructor raising is only half the guard. Both real invocation paths are
# subprocess calls to the CLI, so a LinkedIn default on the argparse flag would
# reintroduce exactly the silent fallback the constructor refuses — argparse
# would fill it in and every caller would inherit a platform nobody chose.

def test_cli_refuses_to_run_without_an_explicit_platform(capsys):
    """Missing --platform must exit loudly (argparse usage error), never default."""
    with pytest.raises(SystemExit) as exc:
        gen.main(["some_input.json", "--profile", "t"])
    assert exc.value.code == 2
    assert "--platform" in capsys.readouterr().err


def test_cli_rejects_an_unknown_platform(capsys):
    with pytest.raises(SystemExit) as exc:
        gen.main(["some_input.json", "--platform", "twitter"])
    assert exc.value.code == 2
    assert "--platform" in capsys.readouterr().err


def test_cli_platform_flag_has_no_default():
    """A default on the flag would be the fallback bug wearing a different hat."""
    import argparse
    for action in gen.build_arg_parser()._actions:
        if isinstance(action, argparse.Action) and "--platform" in (action.option_strings or []):
            assert action.required is True
            assert action.default is None
            return
    raise AssertionError("--platform flag not found on the CLI parser")


@pytest.mark.parametrize("source", [
    "linkedin_automation/dashboard.py",
    "tools/run_linkedin_workflow.py",
])
def test_every_subprocess_invocation_names_its_platform(source):
    """Guard the two real call paths.

    Both launch the generator as ``python -m linkedin_automation.comment_generator``.
    Neither passed a platform before A2 part 2; both relied on the CLI default.
    This fails if a third call site is added without one, or an existing one
    loses it — which is how the default would quietly come back.
    """
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, source), encoding="utf-8") as f:
        text = f.read()

    # Each cmd list literal that launches the generator module, up to its close.
    # The module may be named by literal ("linkedin_automation.comment_generator")
    # or by attribute (`self.comment_module`), so match both — a guard that only
    # sees one form would silently stop covering a call site that switched.
    invocations = re.findall(r"cmd\s*=\s*\[(.*?)\]", text, re.S)
    launching = [inv for inv in invocations
                 if "comment_generator" in inv or "comment_module" in inv]
    assert launching, f"no generator invocation found in {source}"
    for inv in launching:
        assert "--platform" in inv, (
            f"{source} launches the comment generator without naming a platform; "
            "it would inherit whatever the CLI defaults to."
        )


# ─── The shared block must not fight X's register ─────────────────────────────
#
# Found by the 2026-08-17 register review: X's own voice said "fragments are
# fine" while the shared rules block, in the same prompt, said "Complete thoughts
# preferred over fragments". A self-contradicting prompt resolves toward whatever
# the model saw most of — which was LinkedIn.

def test_x_is_not_told_to_prefer_complete_thoughts(make_generator, monkeypatch):
    g = make_generator({}, pp.X)
    _pin_bucket(monkeypatch, 0)
    prompt = g.create_authentic_comment_prompt(POST, STYLE, APPROACH)
    assert "Complete thoughts preferred over fragments" not in prompt
    assert "Fragments are fine" in prompt


def test_linkedin_still_prefers_complete_thoughts(make_generator, monkeypatch):
    g = make_generator({}, pp.LINKEDIN)
    _pin_bucket(monkeypatch, 0)
    prompt = g.create_authentic_comment_prompt(POST, STYLE, APPROACH)
    assert "Complete thoughts preferred over fragments" in prompt


def test_x_does_not_get_linkedins_warm_charming_tone(make_generator, monkeypatch):
    """"Warm, charming" is professional-network register, not X's."""
    g = make_generator({}, pp.X)
    _pin_bucket(monkeypatch, 0)
    prompt = g.create_authentic_comment_prompt(POST, STYLE, APPROACH)
    assert "Warm, charming, and clever" not in prompt
    assert "Plain and direct" in prompt
    # The guard half of the rule is a quality rule, not register — it stays.
    assert "Never snarky, sarcastic, dismissive, or condescending" in prompt


def test_the_worked_example_is_platform_shaped(make_generator, monkeypatch):
    """An exemplar out-steers an instruction, so it must not be LinkedIn's."""
    style = {"name": "direct_reaction", "instruction": "React directly",
             "example": "The context window bottleneck is real. I hit this at 100k "
                        "tokens and had to completely rethink my chunking strategy."}
    _pin_bucket(monkeypatch, 0)

    li = make_generator({}, pp.LINKEDIN).create_authentic_comment_prompt(POST, style, APPROACH)
    x = make_generator({}, pp.X).create_authentic_comment_prompt(POST, style, APPROACH)

    assert f'Example of natural comment: "{style["example"]}"' in li
    assert style["example"] not in x
    assert 'Example of natural reply: "hit this at 100k tokens too.' in x


def test_every_shared_style_has_an_x_example():
    """A style with no X example would silently fall back to LinkedIn's."""
    x_examples = pp.policy_for(pp.X).register.style_examples
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "linkedin_automation", "comment_generator.py"),
               encoding="utf-8").read()
    shared = set(re.findall(r'"name":\s*"(\w+)",\s*\n\s*"instruction"', src))
    assert shared, "could not find the shared style table"
    missing = shared - set(x_examples)
    assert not missing, f"styles with no X exemplar (they fall back to LinkedIn's): {missing}"


def test_linkedin_has_no_style_example_overrides():
    """LinkedIn must keep using the shared table verbatim — that is the goldens."""
    assert pp.policy_for(pp.LINKEDIN).register.style_examples == {}


# ─── Structural guards: stop the next contradiction before it ships ───────────
#
# Three shared-block elements contradicted X's register and were found ONE AT A
# TIME, each by reading live output: the tone rule, the fragments rule, then the
# opening-styles list. Each was LinkedIn-shaped, predated the register, and was
# invisible until X produced something wrong. These guards make the fourth one
# fail at test time instead.

def test_every_register_field_differs_between_platforms():
    """The next LinkedIn-shaped addition cannot silently reach X.

    Adding a field to Register and giving both platforms the same value means a
    shared literal just became a *declared* shared literal — which is how a
    LinkedIn cadence reaches X while looking deliberate. Anything genuinely
    platform-neutral belongs in the shared template, not in Register.

    If this fails on a field that really should match, add it to
    DELIBERATELY_SHARED with a comment saying why.
    """
    import dataclasses

    DELIBERATELY_SHARED = set()   # empty on purpose: nothing has earned it yet

    li = pp.policy_for(pp.LINKEDIN).register
    x = pp.policy_for(pp.X).register
    same = [f.name for f in dataclasses.fields(pp.Register)
            if f.name not in DELIBERATELY_SHARED
            and getattr(li, f.name) == getattr(x, f.name)]
    assert not same, (
        f"Register fields identical on both platforms: {same}. Either give X its "
        "own value, or move the text back into the shared template if it is "
        "genuinely platform-neutral."
    )


def test_the_rendered_x_prompt_contains_no_linkedin_nouns(make_generator, monkeypatch):
    """Catches a noun that slipped past the Register (two did: 'their post',
    'Great post'). Word-boundary, case-insensitive, whole rendered prompt."""
    import re
    g = make_generator(CONFIGS["full"], pp.X)
    _pin_bucket(monkeypatch, 0)
    # A real tweet may legitimately contain the word "post"; this guard is about
    # the PROMPT's own language, so the injected body carries none.
    neutral_post = dict(POST, text="a synthetic body about model evaluation")
    prompt = g.create_authentic_comment_prompt(neutral_post, STYLE, APPROACH)
    leaks = [ln.strip() for ln in prompt.split("\n")
             if re.search(r"\b(post|posts|comment|comments|linkedin)\b", ln, re.I)]
    assert not leaks, f"LinkedIn nouns reached the X prompt: {leaks}"


def test_the_x_prompt_never_shows_a_self_positioning_opener(make_generator, monkeypatch):
    """The third contradiction, as a permanent assertion.

    X's register bans an opener that frames the speaker before saying anything.
    The shared block used to sanction exactly that ('Build on idea: "The X point
    makes sense..."') and every live X reply copied it.
    """
    import re
    g = make_generator({}, pp.X)
    for bucket in range(4):
        _pin_bucket(monkeypatch, bucket)
        prompt = g.create_authentic_comment_prompt(POST, STYLE, APPROACH)
        offenders = [ln.strip() for ln in prompt.split("\n")
                     if re.search(r'"The \w+ (point|approach|part|thing)\b', ln)]
        assert not offenders, (
            f"X is being shown a self-positioning opener its register bans: {offenders}"
        )


def test_x_examples_and_openers_are_lowercase_led(make_generator):
    """A cheap proxy for 'tweet-shaped': X's exemplars start mid-thought.

    Not a register verdict — that needs a human read. But a capitalised,
    full-sentence exemplar block is a reliable sign LinkedIn's shape leaked in.
    """
    r = pp.policy_for(pp.X).register
    quoted = [q for q in re.findall(r'"([^"]+)"', r.short_examples + r.opening_styles)]
    assert quoted
    capitalised = [q for q in quoted if q[:1].isupper()]
    assert not capitalised, f"X exemplars reading as full sentences: {capitalised}"


# ─── Self-positioning openers: hard-rejected on X, valid on LinkedIn ──────────
#
# The register banned these from the start and the model kept producing them
# (2 of 3 live replies). The prior audit confirmed no shared-block element
# contradicted the rule — it was DILUTION, a rule sitting late in a 6k-char
# prompt. So it moved to where rules actually bind: the system message, and the
# hard-reject list that already catches em-dashes and we/our/us.

X_SELF_POSITIONING = [
    "i saw similar gains when optimizing for lower latency",
    "I found the same thing with batching",
    "i've seen this exact failure at 100k tokens",
    "In my experience the queue is usually the problem",
    "i hit the same wall last year",
    '"i noticed the same pattern"',
]


@pytest.mark.parametrize("draft", X_SELF_POSITIONING)
def test_x_rejects_a_self_positioning_opener(make_generator, draft):
    ok, issues = make_generator({}, pp.X).detect_ai_patterns(draft)
    assert ok is False
    assert any("self_positioning_opener" in i for i in issues), issues


@pytest.mark.parametrize("draft", X_SELF_POSITIONING)
def test_linkedin_accepts_the_same_openers(make_generator, draft):
    """"I've seen this" is valid professional register. LinkedIn is unchanged."""
    _, issues = make_generator({}, pp.LINKEDIN).detect_ai_patterns(draft)
    assert not any("self_positioning_opener" in i for i in issues), issues


def test_the_ban_is_opening_position_only(make_generator):
    """Mid-reply self-reference is fine; leading with yourself is not."""
    g = make_generator({}, pp.X)
    mid = "the queue was the whole problem. i saw it at 100k tokens."
    assert not any("self_positioning_opener" in i for i in g.detect_ai_patterns(mid)[1])
    lead = "i saw it at 100k tokens. the queue was the whole problem."
    assert any("self_positioning_opener" in i for i in g.detect_ai_patterns(lead)[1])


def test_linkedins_reject_list_is_untouched(make_generator):
    """LinkedIn declares no banned openers, so RULE 4 is a no-op there."""
    assert pp.policy_for(pp.LINKEDIN).register.banned_openers == ()
    g = make_generator({}, pp.LINKEDIN)
    # The pre-existing hard rejections still fire exactly as before.
    assert any("dash_as_punctuation" in i
               for i in g.detect_ai_patterns("This works - mostly.")[1])
    assert any("first_person_plural" in i
               for i in g.detect_ai_patterns("We tried that approach.")[1])
    assert g.detect_ai_patterns("I've seen this fail the same way twice.")[1] == []


def test_a_self_positioning_x_draft_is_regenerated(make_generator, monkeypatch):
    """The rejection must drive the retry loop, not just annotate the draft."""
    g = make_generator({}, pp.X)
    drafts = iter(["i saw similar gains when optimizing latency",
                   "i found the same thing",
                   "batching, always batching"])

    def fake_create(**kw):
        text = next(drafts)
        return type("R", (), {"choices": [type("C", (), {
            "message": type("M", (), {"content": text})()})()]})()

    monkeypatch.setattr(g.client.chat.completions, "create", fake_create)
    monkeypatch.setattr(g, "check_relevance", lambda post, comment: True)
    monkeypatch.setattr(g, "_log_api_usage", lambda *a, **k: None)

    result = g.generate_comment(POST, {"post_category": "technical_discussion"})

    assert result["comment"] == "batching, always batching"
    assert result["attempt"] == 3, "it should have regenerated past both bad openers"


def test_x_system_voice_carries_the_rule(make_generator):
    """Promoted into the system message, where the model actually weights it."""
    sv = pp.policy_for(pp.X).register.system_voice
    assert "Never open by positioning yourself" in sv
    assert "State the finding" in sv
    # And it sits early, not tacked on the end.
    assert sv.index("Never open by positioning") < len(sv) // 2
