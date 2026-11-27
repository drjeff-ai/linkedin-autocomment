"""Per-platform properties the shared generator reads instead of hardcoding.

The generator is one piece of code writing for two very different surfaces. What
differs between them is not logic — it is **length units** and **register**, and
both were previously baked into LinkedIn-shaped literals:

* ``word_count > 70`` assumed a platform with no character limit. On X a
  perfectly ordinary 60-word reply is roughly 400 characters and **cannot be
  posted at all**.
* "Write a LinkedIn comment…" in a prompt produces output that is fluent,
  on-topic, inside 280 characters — and in the wrong voice. Nothing downstream
  catches that: the AI-tell filters look for em dashes, ``we/our/us``, tag
  questions and formal openings, none of which fire on a well-formed LinkedIn
  comment posted to X.

So each platform **declares** its constraints and its voice here, and the
generator asks. Three rules this module exists to enforce:

1. **The unit travels with the range.** A bare ``[15, 60]`` is what caused the
   problem; :class:`LengthPolicy` cannot be built without saying what it counts.
2. **``hard_max`` is a platform fact, distinct from the persona's preference.**
   The persona's ``comment_length_range`` is taste and lives in profile config;
   280 characters is physics and lives here.
3. **Truncation is not a length policy.** An over-length draft is regenerated,
   and if regeneration still fails on a platform where the limit is a *postable*
   limit, no draft is returned at all. Silently cutting a reply mid-sentence is
   worse than producing nothing.

**There is no default platform.** :func:`policy_for` requires one and raises on
anything it does not know. A default would resolve to LinkedIn, which is exactly
the "fallback quietly answers a different question" shape that has bitten this
project repeatedly — and it would fail *invisibly*, by generating LinkedIn-voiced
text for X rather than by raising.
"""

from dataclasses import dataclass, field, replace
from typing import Tuple

# Platform identifiers, from the one module that defines them. A store
# addressed as "x" and a policy addressed as "twitter" would silently diverge,
# so neither spells its own. ``test_platform_policy.py`` still asserts the two
# agree.
from .platforms import INSTAGRAM, LINKEDIN, X, YOUTUBE  # noqa: E402

# Length units.
WORDS = "words"
CHARS = "chars"


class UnknownPlatform(ValueError):
    """Raised for a platform with no declared policy. Never falls back."""


@dataclass(frozen=True)
class LengthPolicy:
    """How long a draft may be, in the unit the platform actually measures.

    ``preferred`` is the persona's target band (profile config may override it).
    ``hard_max`` is the platform's own ceiling and is not a matter of taste.
    """

    unit: str
    preferred: Tuple[int, int]
    hard_max: int
    # Whether exceeding ``hard_max`` makes the draft UNPOSTABLE (X: 280 chars is
    # a hard API/UI limit) or merely too long to be good (LinkedIn: 70 words is a
    # quality heuristic; the platform accepts it). This decides whether an
    # over-length best-attempt may still be returned.
    hard_max_is_postable_limit: bool
    # The four length options the prompt samples from, with their weights.
    buckets: Tuple[str, ...]
    bucket_weights: Tuple[float, ...]
    # Rendered into the prompt's rules list, e.g. "under 35 words (70% of the time)".
    typical_cap_phrase: str
    # Quoted in the too_long diagnostic: the length we actually aim below,
    # which sits under hard_max because hard_max is the failure point.
    aim_below: int

    def measure(self, text: str) -> int:
        """Length of ``text`` in this platform's unit."""
        text = text or ""
        return len(text.split()) if self.unit == WORDS else len(text)

    def exceeds_hard_max(self, text: str) -> bool:
        return self.measure(text) > self.hard_max

    def describe_range(self, low: int, high: int) -> str:
        """"15-60 words" / "40-240 chars" — the unit is never dropped."""
        return f"{low}-{high} {self.unit}"


@dataclass(frozen=True)
class Register:
    """What this platform's surface, artefacts and voice are called and sound like.

    Every field is used to build prompt text. LinkedIn's values are chosen so the
    rendered prompts are **byte-identical** to the pre-parameterization literals;
    ``tests/goldens/linkedin_prompts.json`` holds the proof.
    """

    surface: str            # "LinkedIn"
    article: str            # "a" / "an" — agreement matters: "an X reply"
    item: str               # "post" / "tweet"
    item_plural: str        # "posts" / "tweets"
    action: str             # "comment" / "reply"
    action_plural: str      # "comments" / "replies"
    # The system-role message for generation — the single strongest register lever.
    system_voice: str
    # Completes: 'You are\n   <voice_identity>.'
    voice_identity: str
    # The TONE hard rule. Its second half ("never snarky, dismissive...") is a
    # quality guard that applies everywhere; its first half is register and
    # differs. Declared whole so the line renders identically for LinkedIn.
    tone_rule: str
    # The "Sentence style:" bullets. LinkedIn asks for complete thoughts; X's
    # register says fragments are correct, so X must not be handed the opposite
    # instruction in the same prompt.
    sentence_style: str
    # The stance distribution. LinkedIn's 60/30/10 leans supportive;
    # a platform whose register is flat wants its own split.
    tone_balance: str
    # THE THIRD CONTRADICTION FOUND. LinkedIn's list sanctions
    # 'Build on idea: "The X point makes sense..."' — precisely the
    # self-positioning opener X's register bans, which is why every live X
    # reply opened with it despite the rule.
    opening_styles: str
    # LinkedIn caps "Nah"/"Hmm"/"Honestly" at 10%; those
    # openers are native on X, so the cap is LinkedIn cadence, not craft.
    overuse_guidance: str
    # The numbered craft rules. Contains a {cap} slot filled from the
    # LengthPolicy, and the item/action nouns, so it cannot stay shared.
    craft_rules: str
    # LinkedIn's are full capitalised sentences.
    good_question_endings: str
    # Labelled "this is the target", so the strongest exemplar in
    # the block — and LinkedIn's include "The multimodal point makes sense.",
    # a banned self-positioning opener shown to X as a target.
    short_examples: str
    # Openers that are hard-rejected, checked at the START of a draft only.
    # LinkedIn's is empty on purpose: "I've seen this fail the same way" is
    # valid professional register there. On X the same opener reads as someone
    # introducing themselves before speaking, so it is a rejection, not a hint.
    # Position matters — mid-reply self-reference ("...we hit it at 100k, i
    # chunked it") is fine on both; it is the OPENING that reads wrong.
    banned_openers: Tuple[str, ...] = ()
    # Appended after the shared authenticity rules. EMPTY for LinkedIn, so its
    # prompts do not change by a single byte; a real block for a platform whose
    # register genuinely differs.
    extra_rules: str = ""
    # Per-style worked examples, keyed by style name. EMPTY for LinkedIn, which
    # keeps using the shared table's example verbatim. A concrete exemplar
    # out-steers any instruction, so showing X a multi-sentence LinkedIn comment
    # labelled "Example of natural reply" undid much of the register work.
    style_examples: dict = field(default_factory=dict)

    def example_for(self, style: dict) -> str:
        """The worked example to show for ``style`` on this platform."""
        return self.style_examples.get(style.get("name"), style.get("example", ""))

    def eval_system_message(self) -> str:
        return (f"Evaluate {self.surface} {self.item_plural} for authentic "
                f"engagement potential.")

    def report_header(self) -> str:
        return f"DAILY {self.surface.upper()} {self.action_plural.upper()}\n"


@dataclass(frozen=True)
class PlatformPolicy:
    """Everything the generator needs to know about one platform."""

    name: str
    length: LengthPolicy
    register: Register
    # How much of the item's text the draft prompt quotes. 500 is the literal
    # LinkedIn and X always used; YouTube's input is a title, description,
    # chapters and up to 20 top comments (D-027 Y1), which 500 would cut to the
    # title and half a description.
    input_chars: int = 500


# ─── LinkedIn — today's behaviour, expressed as data ──────────────────────────
#
# Every literal below is lifted verbatim from the pre-A2 generator. Changing one
# changes LinkedIn's prompts, which the golden test will catch.

_LINKEDIN = PlatformPolicy(
    name=LINKEDIN,
    length=LengthPolicy(
        unit=WORDS,
        preferred=(15, 60),
        hard_max=70,
        # LinkedIn accepts long comments; 70 words is a quality signal, not a
        # wall. Keeping this False preserves the existing fallback behaviour.
        hard_max_is_postable_limit=False,
        buckets=(
            "Super short - 15-25 words (react to ONE specific thing)",
            "Short - 25-35 words (make one clear point)",
            "Medium - 35-50 words (add brief context or ask specific question)",
            "Longer - 50-70 words (tell a relevant story with specific details)",
        ),
        bucket_weights=(0.35, 0.35, 0.20, 0.10),  # 70% under 35 words
        typical_cap_phrase="under 35 words (70% of the time)",
        aim_below=50,
    ),
    register=Register(
        surface="LinkedIn",
        article="a",
        item="post",
        item_plural="posts",
        action="comment",
        action_plural="comments",
        system_voice=(
            "You're a developer commenting casually on LinkedIn. Write like "
            "you're texting a colleague - direct, sometimes skeptical, no "
            "formality. Keep it short."
        ),
        voice_identity="an individual professional sharing your own perspective",
        banned_openers=(),   # "I've seen..." is valid LinkedIn register
        tone_rule=("Warm, charming, and clever. Light wit is great. Never snarky,\n"
                   "   sarcastic, dismissive, or condescending."),
        sentence_style=("- Mix short and medium sentences\n"
                        "- Use commas and periods, never hyphens or dashes as punctuation\n"
                        "- Complete thoughts preferred over fragments\n"
                        "- Drop unnecessary words sparingly"),
        tone_balance='TONE BALANCE - Be constructive, not just critical:\n- 60% curious/supportive/neutral\n- 30% challenging but constructive  \n- 10% direct disagreement (use sparingly)',
        opening_styles='- Ask question: "How did you...?", "What\'s your..."\n- Build on idea: "The X point makes sense..."\n- Share observation: "Noticed that..."\n- Neutral reaction: "The X approach..."\n- Jump right in with your take: "This is exactly why..."\n- Relate to experience: "I ran into this same thing..."\n- Pick one detail: "The latency numbers are wild..."\n- Agree and extend: "Totally agree on X, and I\'d add..."\n- Short reaction: "That 50ms claim is impressive."',
        overuse_guidance='Avoid overusing:\n- "Nah" / "Hmm" / "Honestly" (use max 10% of time)\n- Starting with skepticism every time',
        craft_rules='1. Start direct but not always skeptical\n2. Pick ONE thing from their post to react to\n3. Keep {cap}\n4. Only ask questions if genuinely curious (not rhetorical)\n5. Share personal experience ONLY if truly relevant (20% of comments)\n6. Be specific with details/numbers or say nothing\n7. Default to curious/constructive over challenging\n8. Don\'t end with "huh?" - use real questions or statements instead',
        good_question_endings='"How did you handle the scaling issues?"\n"What stack are you using?"\n"Which framework gave you the best results?"\n"Wonder what the performance tradeoffs look like."',
        short_examples='"The context window bottleneck is real. I hit this at 100k tokens."\n"Wait, you got it under 50ms? What stack are you using?"\n"Data cleaning took me 3 months. Modeling took 2 weeks."\n"The multimodal point makes sense. Context understanding is still the gap."\n"Curious how you handled the scaling issues at that volume."\n"1 trillion tokens is massive. How did you manage the training process?"',
        extra_rules="",
    ),
)


# ─── X — a different surface, not LinkedIn with a smaller budget ──────────────
#
# The register below is deliberately not a noun-swap. A reply on X is a turn in a
# conversation: it starts mid-thought, carries no greeting and no sign-off, and
# stops as soon as the point lands. A shortened LinkedIn comment reads as an
# outsider on X — correct sentences, wrong room.

_X_EXTRA_RULES = """

🐦 THIS IS A REPLY IN A LIVE THREAD:
- Reply mid-conversation. No greeting, no "Great thread", no sign-off, no name.
- One thought. Land it and stop. Do not add a second point because there is room.
- No hashtags. No emoji unless the tweet itself is playful. Never @-mention anyone.
- Sentence fragments are normal here. "Fair." "Depends on the batch size." are
  complete replies.
- Lowercase openings are fine and often better.
- Do not restate the tweet before responding to it. Assume the reader just read it.
- Do not perform expertise. Credentials show through the specificity of the detail
  you pick, never through claiming them.
- No professional-network register: no "In my experience," openers, no "Couldn't
  agree more", no takeaway summaries, no "Thoughts?" endings, no framing your
  reply as a lesson.
- No opener that positions you before you say anything. Not "The batching detail
  is the interesting part" — just say the thing.
- If it would survive unchanged on a professional network, it is wrong here.
- No "I found" / "in my experience" framing. State the finding, not that it is
  yours.

Register targets (a real reply, not a shortened essay):
"that's a batching artifact, not a model thing"
"200ms at what batch size though"
"i hit the same wall at 100k tokens. gave up and chunked it."
"the eval set is doing a lot of work in that claim"
"""

_X = PlatformPolicy(
    name=X,
    length=LengthPolicy(
        unit=CHARS,
        preferred=(40, 240),
        # 280 is the standard-tier ceiling. CONFIRM LIVE for the account's tier
        # during Phase B/D — paid tiers raise it, and a wrong ceiling here means
        # drafts that cannot post.
        hard_max=280,
        # On X the limit is the platform's, not a preference: an over-length
        # reply is not "a bit long", it is unpostable. So it is never returned.
        hard_max_is_postable_limit=True,
        buckets=(
            "Very short - 40-80 characters (one reaction, nothing else)",
            "Short - 80-140 characters (one clear point)",
            "Medium - 140-200 characters (a point plus one specific detail)",
            "Longer - 200-260 characters (a short story or a real question, still one thought)",
        ),
        # Weighted shorter than LinkedIn: on X, brevity is the register.
        bucket_weights=(0.35, 0.40, 0.20, 0.05),
        typical_cap_phrase="under 180 characters (most of the time)",
        aim_below=240,
    ),
    register=Register(
        surface="X",
        article="an",
        item="tweet",
        item_plural="tweets",
        action="reply",
        action_plural="replies",
        # Sharpened after the 2026-08-17 register review, which found X was
        # producing trimmed LinkedIn comments. The previous voice ("developer
        # replying, punchy, keep it short") was the same register as LinkedIn's
        # ("developer texting a colleague, no formality, keep it short") — the
        # strings differed, the voice did not. This one names the *form* of a
        # reply, not just its length.
        system_voice=(
            "You are in the replies on X. Not writing a comment — replying. "
            "Start mid-thought; everyone has read the tweet. Never open by "
            "positioning yourself: no \"i saw\", \"i found\", \"i've seen\", "
            "\"i hit the same\", \"in my experience\". State the finding "
            "directly, not that it is yours. No greeting, no name, no "
            "sign-off, no closing summary. Say the one thing you would "
            "actually say and stop, even if that is six words. Fragments, "
            "lowercase openings and dropped subjects are correct here, not "
            "sloppy. 280 is the ceiling; most good replies are under 120."
        ),
        # Not "not a brand or a team" — the shared we/our/us rule already enforces
        # that, so the slot was being spent on a solved problem.
        voice_identity="someone in the replies who happens to know this specific thing",
        banned_openers=(
            "i saw", "i found", "i've seen", "ive seen", "i have seen",
            "i hit", "i ran into", "i noticed", "i tried", "i had",
            "in my experience", "from my experience", "i've found", "ive found",
            "i've hit", "i've had", "i once", "personally,",
        ),
        # "Warm, charming" is professional-network register and was dragging X
        # back toward LinkedIn. The guard half ("never snarky, dismissive...")
        # is a quality rule, not a register one, so it stays on both platforms.
        tone_rule=("Plain and direct. Dry wit is fine; warmth is not required.\n"
                   "   Never snarky, sarcastic, dismissive, or condescending."),
        # LinkedIn's "Complete thoughts preferred over fragments" is deleted here,
        # not softened: X's register says fragments are correct, and a prompt that
        # says both contradicts itself.
        sentence_style=("- Short sentences. Fragments are fine.\n"
                        "- Use commas and periods, never hyphens or dashes as punctuation\n"
                        "- Drop every word the reply does not need"),
        # X-shaped exemplars. The shared table's examples are multi-sentence
        # LinkedIn comments; shown to X under the label "Example of natural
        # reply", they undid the register work more than any instruction fixed.
        style_examples={
            "genuine_curious": "how long before it stabilized",
            "thoughtful_addition": "chunking on semantic boundaries instead of char count is what fixed it",
            "respectful_different": "went the other way. basic tooling, better data. same result, less rope.",
            "empathetic_relate": "oh this is painful. schema change with no warning, classic.",
            "practical_helper": "gradient_checkpointing=True cut my memory 40%. not a fix but it buys time.",
            "brief_supportive": "the state transition diagram is genuinely clever",
            "direct_reaction": "hit this at 100k tokens too. had to rethink chunking entirely.",
            "minimal_agreement": "this. the distributed part especially.",
            "specific_question": "under 50ms on what stack? stuck at 200ms here",
            "direct_challenge": "assumes you control your own pipeline though",
        },
        tone_balance='TONE BALANCE - flat is a valid register here:\n- 50% neutral, just the observation\n- 30% curious, one real question\n- 20% disagreement, said plainly and without softening',
        opening_styles='- Straight to the observation: "batching, always batching"\n- Bare question: "at what batch size though"\n- Correct one detail: "that\'s a serving artifact, not the model"\n- Lead with your own number: "we saw 8% from quantization"\n- One-word reaction, then the point: "same. the queue was the whole problem."\n- Pick it up mid-sentence: "and that\'s before you add concurrency"',
        overuse_guidance='Avoid overusing:\n- Opening every reply the same way\n- Hedging ("might be", "could be", "in some cases")',
        craft_rules='1. Start with the thing itself, never with a frame around it\n2. Pick ONE thing from their tweet to react to\n3. Keep {cap}\n4. Only ask questions if genuinely curious (not rhetorical)\n5. Share personal experience ONLY if truly relevant (20% of replies)\n6. Be specific with details/numbers or say nothing\n7. No summarising, no takeaways, no "the real question is"\n8. Don\'t end with "huh?" - use real questions or statements instead',
        good_question_endings='"at what batch size"\n"what stack"\n"which framework won"\n"how did that hold at higher concurrency"',
        short_examples='"hit this at 100k tokens. had to rethink chunking entirely."\n"under 50ms? on what stack"\n"data cleaning took 3 months. modeling took 2 weeks."\n"batching, always batching"\n"curious how that held at volume"\n"1 trillion tokens is wild. how long did training take?"',
        extra_rules=_X_EXTRA_RULES,
    ),
)


# ─── YouTube (D-027 Y1) ───────────────────────────────────────────────────────
#
# Every value below is copied verbatim from the strategist's register,
# ``content/engagement/youtube-register.md`` (DEC-005, DEC-061(6)). The coder
# never edits this text; Jeff edits the register and the copy is retaken.
# ``tests/test_youtube_policy_golden.py`` parses the copy in
# ``tests/fixtures/engagement/`` and compares every field. The register's
# bullet lists are laid out as the prompt's bullet lines (``- item``, numbered
# items keep their number), as LinkedIn's and X's are.

_YOUTUBE = PlatformPolicy(
    name=YOUTUBE,
    length=LengthPolicy(
        unit='chars',
        preferred=(30, 300),
        hard_max=500,
        hard_max_is_postable_limit=False,
        buckets=('Very short - 30-80 characters (one reaction to one moment)', 'Short - 80-160 characters (one clear point)', 'Medium - 160-260 characters (a point plus one specific detail)', 'Longer - 260-400 characters (a real question or a short first-hand detail)'),
        bucket_weights=(0.3, 0.4, 0.22, 0.08),
        typical_cap_phrase='under 160 characters (most of the time)',
        aim_below=260,
    ),
    register=Register(
        surface='YouTube',
        article='a',
        item='video',
        item_plural='videos',
        action='comment',
        action_plural='comments',
        system_voice="You're someone who builds with AI, leaving a comment under a video you just watched. Write like a viewer talking to the creator: direct, specific, relaxed. Keep it short.",
        voice_identity='a viewer who works with this technology, reacting to this one video',
        banned_openers=('Great video', 'Great content', 'Awesome video', 'Thanks for sharing', 'This is so helpful', 'As someone who', 'Love this', 'Amazing'),
        tone_rule='Friendly and genuine. Curious beats clever. Never snarky, never dismissive, never a pitch.',
        sentence_style='- Mix short and medium sentences\n- Plain words, no corporate phrasing\n- Commas and periods, never dashes as punctuation\n- A fragment is fine when it lands',
        tone_balance='TONE BALANCE:\n- 50% specific reaction to one moment or claim\n- 30% a real question the creator could answer in a reply\n- 20% a respectful different take or a first-hand detail',
        opening_styles='- Name the moment: "eval scores flipping halfway through"\n- A real question: "did this hold up on longer contexts"\n- One detail they got right: "the latency comparison is the useful bit here"\n- A first-hand number: "i saw the same drop past 32k tokens"\n- Gentle pushback: "fair, though this depends a lot on the dataset"',
        overuse_guidance="Avoid overusing:\n- Opening with praise\n- Ending every comment with a question\n- Restating the video's title",
        craft_rules="1. React to ONE specific thing from the video's title, description or chapters, never to the video in general\n2. Keep {cap}\n3. Only ask a question you would actually want answered\n4. First-hand experience only when it is relevant (about 20% of comments)\n5. Be specific or say less\n6. Never mention our channel, our videos, or that we make videos\n7. No links, no hashtags, no @-mentions, no calls to subscribe or watch anything\n8. No summarising the video back to the creator",
        good_question_endings='"did this hold on bigger models"\n"what did the eval set look like"\n"how long did the fine-tune take"\n"would this work without the GPU"',
        short_examples='"eval drift is the bit nobody covers"\n"did this hold on longer contexts? i saw it fall off past 32k"\n"fair, though the benchmark choice is doing a lot of work here"\n"the side by side at 4:10 made it click"\n"how long did the fine-tune take on that setup"',
        extra_rules="\n\nTHIS IS A YOUTUBE COMMENT UNDER SOMEONE ELSE'S VIDEO:\n- You are a viewer, not a creator. Nothing in the comment may promote anything.\n- No greeting, no sign-off, no 'great video' opener.\n- Never claim you watched a part the input does not describe. If only the title and description are known, react to those.\n- One thought. Stop when it lands.\n- Emoji only if the video itself is playful, and never more than one.",
    ),
    input_chars=3000,
)

# The own-video reply variant: the same register with the two fields the
# register's "own-video replies" section replaces.
_YOUTUBE_REPLY = replace(_YOUTUBE, register=replace(
    _YOUTUBE.register,
    system_voice="You're the creator of this Short, replying to a viewer's comment. Short, warm, specific to what they said.",
    extra_rules="\n\nTHIS IS A REPLY TO A VIEWER ON YOUR OWN VIDEO:\n- Answer what they said, nothing else.\n- No 'thanks for watching', no ask to subscribe, no link to another video.\n- If they asked a question, answer it in one or two sentences, or say plainly that you don't know.\n- Never argue. If they disagree, acknowledge the point once.",
))


# ─── YouTube, Teratoma House's register (D-033 Y2-T, DEC-077) ────────────────
#
# Copied verbatim from the strategist's
# ``content/engagement/youtube-register-teratomahouse.md`` (DEC-005). A profile
# picks it with ``engagement.register: "teratomahouse"``; no ``register`` means
# ``DEFAULT_REGISTER``, the policy above, unchanged.
# ``tests/test_youtube_policy_golden.py`` compares every field with the copy in
# ``tests/fixtures/engagement/``.

_YOUTUBE_TERATOMAHOUSE = PlatformPolicy(
    name=YOUTUBE,
    length=LengthPolicy(
        unit='chars',
        preferred=(20, 250),
        hard_max=400,
        hard_max_is_postable_limit=False,
        buckets=('Very short - 20-70 characters (one gut reaction to one moment)', 'Short - 70-150 characters (one clear reaction or theory)', 'Medium - 150-250 characters (a theory plus the detail behind it)', 'Longer - 250-350 characters (a real question or a small personal moment)'),
        bucket_weights=(0.35, 0.4, 0.2, 0.05),
        typical_cap_phrase='under 150 characters (most of the time)',
        aim_below=250,
    ),
    register=Register(
        surface='YouTube',
        article='a',
        item='video',
        item_plural='videos',
        action='comment',
        action_plural='comments',
        system_voice="You're a horror fan leaving a comment under a scary story or short you just watched. Write like a viewer talking to the creator and the other viewers: direct, specific, a little unsettled. Keep it short.",
        voice_identity='a horror fan reacting to this one video',
        banned_openers=('Great video', 'Great content', 'Awesome video', 'Thanks for sharing', 'Love this', 'Amazing', 'This gave me chills', "I'm not scared but", 'Who else is watching'),
        tone_rule='Genuine and a little playful. Curious beats clever. Never mean to the creator, never dismissive of the genre, never a pitch.',
        sentence_style='- Mix short and medium sentences\n- Plain words, the way people actually talk in a comment section\n- Commas and periods, never dashes as punctuation\n- A fragment is fine when it lands',
        tone_balance="TONE BALANCE:\n- 45% a reaction to one specific moment, detail or rule\n- 30% a theory or a question about what was really going on\n- 25% a small relatable moment (the thing you'd now check at night)",
        opening_styles='- Name the moment: "the second voicemail is what got me"\n- A theory: "pretty sure the neighbor was never the neighbor"\n- A question: "was the drawing there before they moved in"\n- A relatable moment: "now i\'m checking my own delivery photos"\n- Spot the detail: "the clock in the background changes, right"',
        overuse_guidance="Avoid overusing:\n- Opening with how scared you were\n- Ending every comment with a question\n- Restating the video's title\n- The words creepy, chills and goosebumps",
        craft_rules="1. React to ONE specific thing from the video's title, description or chapters, never to the video in general\n2. Keep {cap}\n3. Only ask a question or float a theory you would actually want answered\n4. A small personal moment only when it fits (about 25% of comments), and never a made-up encounter presented as real\n5. Be specific or say less\n6. Never mention our channel, our videos, or that we make videos\n7. No links, no hashtags, no @-mentions, no calls to subscribe or watch anything\n8. No summarising the story back to the creator, and no spoiling the ending for people reading the comments first",
        good_question_endings='"was the drawing there before they moved in"\n"did anyone else catch the time stamp"\n"is this the same house as the last one"\n"why does the alert repeat at the end"',
        short_examples='"the second voicemail is what got me"\n"pretty sure the neighbor was never the neighbor"\n"rule 7 contradicting rule 2 is the scariest part"\n"now i\'m checking my own delivery photos"\n"did anyone else catch the clock changing"',
        extra_rules="\n\nTHIS IS A YOUTUBE COMMENT UNDER SOMEONE ELSE'S HORROR VIDEO:\n- You are a viewer, not a creator. Nothing in the comment may promote anything.\n- No greeting, no sign-off, no 'great video' opener.\n- Never claim you watched a part the input does not describe. If only the title and description are known, react to those.\n- Never claim a real paranormal experience, and never treat a fictional story as real news.\n- No gore, no slurs, nothing about real tragedies or real people being harmed.\n- One thought. Stop when it lands.\n- At most one emoji, and only if it fits.",
    ),
    input_chars=3000,
)

_YOUTUBE_TERATOMAHOUSE_REPLY = replace(_YOUTUBE_TERATOMAHOUSE, register=replace(
    _YOUTUBE_TERATOMAHOUSE.register,
    system_voice="You're the creator of this horror Short, replying to a viewer's comment. Short, warm, a little in character, specific to what they said.",
    extra_rules="\n\nTHIS IS A REPLY TO A VIEWER ON YOUR OWN VIDEO:\n- Answer what they said, nothing else.\n- If they float a theory, you may hint, but never confirm or explain the ending.\n- No 'thanks for watching', no ask to subscribe, no link to another video.\n- Never argue. If they disagree, acknowledge the point once.",
))


# ─── Instagram (D-027 E2) ─────────────────────────────────────────────────────
#
# Copied verbatim from the strategist's register,
# ``content/engagement/instagram-register.md`` (DEC-005, DEC-061(6)), the same
# way YouTube's is. The coder never edits this text.
# ``tests/test_instagram_policy_golden.py`` parses the copy in
# ``tests/fixtures/engagement/`` and compares every field. ``input_chars`` is
# the shared 500: the input is one caption. The §9 rules that are data checks
# (length cap, emoji, hashtags, caption language, near-duplicates, appearance)
# are enforced by ``instagram_engage.draft_refusals``, not by this text.

_INSTAGRAM = PlatformPolicy(
    name=INSTAGRAM,
    length=LengthPolicy(
        unit='chars',
        preferred=(15, 150),
        hard_max=300,
        hard_max_is_postable_limit=False,
        buckets=('Very short - 15-50 characters (one reaction)', 'Short - 50-100 characters (one clear point)', 'Medium - 100-150 characters (a point plus one detail)', 'Longer - 150-220 characters (a real question)'),
        bucket_weights=(0.35, 0.4, 0.2, 0.05),
        typical_cap_phrase='under 100 characters (most of the time)',
        aim_below=150,
    ),
    register=Register(
        surface='Instagram',
        article='an',
        item='post',
        item_plural='posts',
        action='comment',
        action_plural='comments',
        system_voice="You're a person leaving a quick comment on a post you liked. Warm, specific, casual. Keep it short.",
        voice_identity='a person reacting to this one post',
        banned_openers=('Love this', 'So inspiring', 'Amazing', 'Great post', 'Wow', 'Beautiful', 'Gorgeous', 'Stunning'),
        tone_rule='Warm and specific. Never generic, never flirty, never a pitch.',
        sentence_style='- Short and casual\n- Lowercase is fine\n- Commas and periods, never dashes as punctuation',
        tone_balance='TONE BALANCE:\n- 60% specific reaction to something in the caption or the work\n- 30% a light real question\n- 10% a short first-hand connection',
        opening_styles='- Name the detail: "the colour on the second page"\n- A light question: "is this from the new series"\n- A small connection: "my kid asks this exact question"',
        overuse_guidance='Avoid overusing:\n- Exclamation marks (one at most)\n- Opening with praise\n- Emoji',
        craft_rules="1. React to ONE specific thing in the caption or the work\n2. Keep {cap}\n3. Never comment on anyone's appearance or body\n4. Write in the caption's language\n5. No links, no hashtags, no @-mentions, no promotion of anything\n6. Never mention our account or our work\n7. At most one emoji, and only if the post is playful",
        good_question_endings='"is this part of a series"\n"what was the hardest part to get right"\n"how long did this take"',
        short_examples='"the colour shift on the last slide is so good"\n"is this from the new series?"\n"my kid asks this exact question every night"\n"how long did the lettering take"',
        extra_rules='\n\nTHIS IS AN INSTAGRAM COMMENT:\n- One thought, then stop.\n- No greeting, no sign-off.\n- Never claim to see something the input does not describe.',
    ),
)


PLATFORMS = {LINKEDIN: _LINKEDIN, X: _X, YOUTUBE: _YOUTUBE, INSTAGRAM: _INSTAGRAM}

#: A variant is a second voice on one platform's surface. YouTube's ``reply``
#: is the creator answering a viewer under our own Short.
REPLY = "reply"
VARIANTS = {(YOUTUBE, REPLY): _YOUTUBE_REPLY}

#: A named register is a whole second voice for one platform, chosen per
#: profile (DEC-077): ``{(platform, name): {variant or None: policy}}``.
#: ``DEFAULT_REGISTER`` (or no name) is ``PLATFORMS``/``VARIANTS`` above.
DEFAULT_REGISTER = "default"
NAMED_REGISTERS = {
    (YOUTUBE, "teratomahouse"): {None: _YOUTUBE_TERATOMAHOUSE,
                                 REPLY: _YOUTUBE_TERATOMAHOUSE_REPLY},
}


def register_names(platform: str) -> Tuple[str, ...]:
    """The register names a profile may pick on ``platform``."""
    return (DEFAULT_REGISTER,) + tuple(sorted(
        name for (plat, name) in NAMED_REGISTERS if plat == platform))


def policy_for(platform: str, variant: str = None,
               register_name: str = None) -> PlatformPolicy:
    """Return the declared policy for ``platform``. **Required — never defaults.**

    Raises :class:`UnknownPlatform` for an unknown or empty value rather than
    quietly handing back LinkedIn's. A silent default here would not fail loudly;
    it would generate LinkedIn-voiced text for another platform and pass every
    automated gate on the way out.

    ``variant`` names a second declared voice on the platform (``REPLY`` on
    YouTube). An undeclared variant raises too; it never falls back to the
    platform's main voice.

    ``register_name`` picks a named register (``NAMED_REGISTERS``); None or
    ``DEFAULT_REGISTER`` is the platform's own. An unknown name raises.
    """
    if register_name not in (None, DEFAULT_REGISTER):
        named = NAMED_REGISTERS.get((platform, register_name))
        if named is None:
            raise UnknownPlatform(
                f"no register {register_name!r} declared for platform {platform!r}; "
                f"known: {', '.join(register_names(platform))}")
        if variant not in named:
            raise UnknownPlatform(
                f"no {variant!r} variant declared for register {register_name!r} "
                f"on platform {platform!r}")
        return named[variant]
    if variant:
        try:
            return VARIANTS[(platform, variant)]
        except KeyError:
            raise UnknownPlatform(
                f"no {variant!r} variant declared for platform {platform!r}; "
                f"known: {', '.join(sorted('%s/%s' % k for k in VARIANTS))}"
            ) from None
    if not platform:
        raise UnknownPlatform(
            "platform is required (no default). Pass platform_policy.LINKEDIN or "
            f"one of: {', '.join(sorted(PLATFORMS))}"
        )
    try:
        return PLATFORMS[platform]
    except KeyError:
        raise UnknownPlatform(
            f"no policy declared for platform {platform!r}; "
            f"known platforms: {', '.join(sorted(PLATFORMS))}"
        ) from None


def on_topic_rules(register: Register) -> str:
    """Topic-discipline block, in the platform's nouns.

    The persona is WHO is talking (voice/judgment), not WHAT every draft must be
    about — so the draft must engage the item's actual point and never shoehorn in
    the user's industry. Includes the concrete failure mode (VFX forced onto an
    AI-economics post) as a BAD/GOOD pair.
    """
    item_upper = register.item.upper()
    return f"""
🎯 STAY ON THE {item_upper}'S TOPIC (this rule outranks everything below):
{register.action.capitalize()} on what the {item_upper} is actually about. Engage directly with the SPECIFIC
point it makes. Your background and expertise shape HOW you see the topic, but
do NOT force your industry or expertise into the {register.action} if the {register.item} is not
about that area. {register.action.capitalize()} on what the {register.item} is actually about. Your background
should only come through when it genuinely connects to the {register.item}'s topic.

Concrete failure mode to AVOID:
- {item_upper} is about AI labor economics (hiring vs layoffs, capex vs headcount, who to
  blame for cuts).
  ❌ BAD: "While AI roles are booming, many production teams still rely heavily on
     traditional VFX skills." (Non-sequitur. The {register.item} never mentioned VFX. Forcing
     an unrelated industry angle like this gets the {register.action} REJECTED.)
  ✅ GOOD: "The capex-versus-headcount point is the one people skip. Blaming AI for
     the cuts is convenient cover when the real driver is balance-sheet math."
     (Engages the {register.item}'s ACTUAL argument. No forced industry reference.)

A VFX/AI lead can have a sharp take on AI labor economics without mentioning VFX
once. That is the goal: your voice and judgment, applied to the {register.item}'s real
subject.
"""
