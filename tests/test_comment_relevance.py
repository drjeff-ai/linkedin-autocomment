"""Tests for the on-topic relevance check in generate_authentic_linkedin_comments.

No network: the OpenAI client is replaced with a scripted fake, so the suite
makes zero live API calls (per the spec). Covers the relevance gate, the
stay_on_post_topic flag, the prompt reframing, and api_usage logging."""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import comment_generator as gen


# ─── Scripted fake OpenAI client (distinguishes relevance vs generation calls) ─

class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class _Resp:
    def __init__(self, content):
        self.choices = [_Msg(content)]


class _FakeCompletions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, model, messages, **kwargs):
        user = messages[-1]["content"]
        if "Answer with exactly YES or NO" in user:
            self.owner.relevance_calls += 1
            ans = self.owner.relevance_outputs.pop(0) if self.owner.relevance_outputs else "YES"
            return _Resp(ans)
        self.owner.generation_calls += 1
        out = self.owner.generation_outputs.pop(0) if self.owner.generation_outputs else "default comment."
        return _Resp(out)


class FakeClient:
    def __init__(self, generation_outputs=None, relevance_outputs=None):
        self.generation_outputs = list(generation_outputs or [])
        self.relevance_outputs = list(relevance_outputs or [])
        self.generation_calls = 0
        self.relevance_calls = 0
        self.chat = type("Chat", (), {"completions": _FakeCompletions(self)})()


@pytest.fixture
def make_generator(monkeypatch, comments_dir):
    """Build a generator with the OpenAI client stubbed out (no network)."""
    monkeypatch.setattr(gen, "OpenAI", lambda **k: object())

    def _make(config=None, client=None):
        monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config or {})
        g = gen.AuthenticLinkedInCommentGenerator("dummy_input.json", profile_name="t",
                                              platform=gen.platform_policy.LINKEDIN)
        g.client = client if client is not None else FakeClient()
        return g
    return _make


POST_ECON = {
    "author_name": "Some Analyst",
    "text": "AI hiring is up while layoffs are blamed on AI. The real driver is "
            "capex versus headcount on the balance sheet.",
}


# ─── stay_on_post_topic flag ───────────────────────────────────────────────────

def test_flag_defaults_true_when_absent(make_generator):
    g = make_generator(config={})
    assert g.stay_on_post_topic is True


def test_flag_respects_explicit_false(make_generator):
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": False}})
    assert g.stay_on_post_topic is False


# ─── check_relevance ───────────────────────────────────────────────────────────

def test_check_relevance_yes_and_no(make_generator):
    g = make_generator(client=FakeClient(relevance_outputs=["YES", "NO", "yes\n"]))
    assert g.check_relevance(POST_ECON, "on topic") is True
    assert g.check_relevance(POST_ECON, "off topic") is False
    assert g.check_relevance(POST_ECON, "case-insensitive") is True


def test_check_relevance_defaults_true_on_error(make_generator):
    class Boom:
        def __init__(self):
            self.chat = type("C", (), {"completions": self})()

        def create(self, **k):
            raise RuntimeError("api down")
    g = make_generator(client=Boom())
    assert g.check_relevance(POST_ECON, "whatever") is True


def test_check_relevance_logs_to_api_usage_with_mini_model(make_generator):
    g = make_generator(client=FakeClient(relevance_outputs=["YES"]))
    logged = []
    g._log_api_usage = lambda endpoint, cost, model=None: logged.append((endpoint, model))
    g.check_relevance(POST_ECON, "comment")
    assert logged == [("chat.completions:relevance", gen.RELEVANCE_MODEL)]
    assert gen.RELEVANCE_MODEL == "gpt-4o-mini"


# ─── Relevance gate inside generate_comment ───────────────────────────────────

def test_offtopic_comment_is_regenerated(make_generator, monkeypatch):
    # First generation is off-topic (VFX forced), relevance says NO; second is
    # on-topic, relevance says YES. The on-topic one must be returned.
    client = FakeClient(
        generation_outputs=[
            "Many production teams still rely heavily on traditional VFX skills.",
            "The capex versus headcount point is the one people skip.",
        ],
        relevance_outputs=["NO", "YES"],
    )
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": True}}, client=client)
    # Isolate the relevance logic from authenticity heuristics.
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result["comment"] == "The capex versus headcount point is the one people skip."
    assert client.generation_calls == 2
    assert client.relevance_calls == 2
    assert result["relevant"] is True


def test_flag_off_skips_relevance_call(make_generator, monkeypatch):
    client = FakeClient(generation_outputs=["Any authentic comment about the post."])
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": False}}, client=client)
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result is not None
    assert client.relevance_calls == 0          # no extra spend when flag is off
    assert client.generation_calls == 1


def test_relevant_comment_accepted_first_try(make_generator, monkeypatch):
    client = FakeClient(
        generation_outputs=["The capex versus headcount framing is the real story here."],
        relevance_outputs=["YES"],
    )
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": True}}, client=client)
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result["relevant"] is True
    assert client.generation_calls == 1
    assert client.relevance_calls == 1


# ─── Prompt reframing ──────────────────────────────────────────────────────────

def test_prompt_includes_on_topic_rules_and_examples(make_generator):
    g = make_generator(config={"comment_generator": {
        "persona": "AI Tech Lead in VFX", "topics_of_expertise": ["vfx pipelines"]}})
    style = {"name": "add_insight", "instruction": "Add a point", "example": "An example."}
    prompt = g.create_authentic_comment_prompt(POST_ECON, style, "build on idea")
    assert "STAY ON THE POST'S TOPIC" in prompt
    assert "do NOT force your industry or expertise" in prompt
    assert "BAD" in prompt and "GOOD" in prompt          # failure-mode examples
    # Persona reframed as voice/judgment, expertise surfaced only when relevant.
    assert "shapes your VOICE and JUDGMENT" in prompt
    assert "never force it in" in prompt


def test_on_topic_rules_constant_has_vfx_failure_example():
    # The block moved from gen.ON_TOPIC_RULES to platform_policy.on_topic_rules()
    # so its nouns follow the platform. Same content, same assertions.
    from linkedin_automation import platform_policy as pp
    rules = pp.on_topic_rules(pp.policy_for(pp.LINKEDIN).register)
    assert "VFX" in rules
    assert "capex" in rules.lower()
    # And it still renders for a platform with different nouns.
    x_rules = pp.on_topic_rules(pp.policy_for(pp.X).register)
    assert "VFX" in x_rules
    assert "STAY ON THE TWEET'S TOPIC" in x_rules


def test_default_config_exposes_flag():
    import json
    cfg = json.load(open("default_profile_config.json", encoding="utf-8"))
    assert cfg["comment_generator"]["stay_on_post_topic"] is True
