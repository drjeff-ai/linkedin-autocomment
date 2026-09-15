# IMPROVED VERSION - Comments sound more human, less AI-generated
"""Generate authentic-sounding replies with OpenAI (GPT-4o-mini), per platform.

Reads scraped posts (``ai_posts_*.json``), generates a draft per post, and
rejects/regenerates output that trips the AI-pattern banned-phrase filters.
Writes ``comments_*.json`` for the dashboard review step.

**Platform-neutral by construction.** Length units and register are not baked in
here; they are declared in :mod:`platform_policy` and read from it. The generator
requires an explicit ``platform`` — there is no default, because a default would
resolve to LinkedIn and produce LinkedIn-voiced text for another platform without
failing anything. LinkedIn's rendered prompts are byte-identical to the
pre-parameterization literals, proven by ``tests/goldens/linkedin_prompts.json``.
"""

import json
import os
import re
import time
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# Per-platform length units and register. Not optional: the generator cannot
# build a prompt without knowing which platform it is writing for.
from . import platform_policy

# Route TLS verification through the OS trust store so the OpenAI client (httpx)
# works behind corporate TLS-intercepting proxies, which otherwise cause
# "Connection error" because httpx uses certifi, not the system CA store. No-op
# if truststore isn't installed or isn't needed.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from openai import OpenAI
from dotenv import load_dotenv
import argparse
import logging
import random

load_dotenv()

# Import profile manager for data directory resolution
try:
    from . import profile_manager as pm
    HAS_PROFILE_MANAGER = True
except ImportError:
    HAS_PROFILE_MANAGER = False

# Lifecycle store (NEW → GENERATED on each accepted comment). Optional so the
# generator still runs in the degraded no-profile-manager mode.
try:
    from . import post_store
    HAS_POST_STORE = True
except ImportError:
    HAS_POST_STORE = False


# The relevance check is always a cheap model regardless of the generation model
# the user picked, since it's one extra call per accepted comment.
RELEVANCE_MODEL = "gpt-4o-mini"

# The topic-discipline block now lives in ``platform_policy.on_topic_rules``
# so its nouns follow the platform ("the POST's topic" / "the TWEET's topic").
# LinkedIn's rendering is byte-identical to the constant that used to be here.


class AuthenticCommentGenerator:
    """Generate drafts that sound like actual humans, in a platform's register.

    ``platform`` is **keyword-only and required**. It is not defaulted, because
    the failure mode of a wrong default here is silent: LinkedIn-voiced text
    generated for another platform passes every automated check downstream. An
    explicit argument at every call site is the whole guard.
    """

    def __init__(self, input_file: str, model: str = "gpt-4o-mini",
                 max_comments: int = None, profile_name: str = None,
                 *, platform: str):
        self.input_file = input_file
        self.model = model
        # Raises UnknownPlatform rather than falling back — see policy_for.
        self.platform = platform
        self.policy = platform_policy.policy_for(platform)
        self.register = self.policy.register
        self.length = self.policy.length
        # Optional cap on how many comments to generate. None (the default) means
        # no cap — generate for every post worth engaging. Comment generation is
        # OpenAI-only (no LinkedIn interaction), so there's no rate-limit reason
        # to cap it; this is just an optional ceiling the user can set.
        self.max_comments = max_comments
        self.client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        # Populated by select_best_posts with every post it turned down, so
        # generate_all_comments can move them out of NEW (see
        # _trash_rejected_in_store). Initialized here so the attribute always
        # exists even if selection never runs.
        self.rejected_posts = []
        
        # Setup directories (profile-specific)
        if HAS_PROFILE_MANAGER:
            resolved_name = profile_name or pm.get_default_profile_name() or "default"
            self.output_dir = pm.get_comments_dir(resolved_name)
            self.resolved_profile = resolved_name
        else:
            self.output_dir = "data/quality_comments"
            os.makedirs(self.output_dir, exist_ok=True)
            self.resolved_profile = None
        
        # Setup logging FIRST
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info(f"Comments directory: {self.output_dir}")
        
        # Progress tracking (AFTER logger is initialized)
        if HAS_PROFILE_MANAGER:
            self.progress_file = pm.get_progress_file(resolved_name)
        else:
            self.progress_file = os.path.join(self.output_dir, "posting_progress.json")
        self.posted_urls = self.load_posted_urls()

        # Per-profile config drives persona/tone/voice/topics/avoid/style_mix in
        # the prompt. Falls back to {} (generic defaults) if unavailable; the
        # hard style rules stay enforced in detect_ai_patterns regardless.
        if HAS_PROFILE_MANAGER:
            self.config = pm.get_profile_config(resolved_name)
        else:
            self.config = {}

        # When true (default), every accepted comment is validated for on-topic
        # relevance with one extra cheap call, and off-topic/forced-expertise
        # comments are regenerated. Cost-conscious users can set it false.
        self.stay_on_post_topic = bool(
            (self.config or {}).get("comment_generator", {}).get("stay_on_post_topic", True)
        )
    
    def load_posted_urls(self) -> set:
        """Load URLs that have already been commented on."""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r') as f:
                    progress_data = json.load(f)
                    posted_urls = set(progress_data.get('posted_comments', []))
                    self.logger.info(f"Loaded {len(posted_urls)} already-posted URLs")
                    return posted_urls
            except Exception as e:
                self.logger.warning(f"Error loading progress file: {e}")
        return set()
    
    def load_posts(self) -> Dict:
        """Load posts from the JSON file."""
        with open(self.input_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def enhanced_quality_filter_prompt(self, post: Dict) -> str:
        """Decide WHETHER an AI/tech professional could meaningfully comment.

        Inclusive by design: the post finder already removed junk upstream, and
        the user is an AI training consultant — almost any AI/tech/business post
        is worth a comment. This decides engagement only, not comment quality.
        """
        r = self.register
        return f"""You decide whether a professional working in AI/tech could add a
meaningful {r.action} to this {r.surface} {r.item}.

{r.item.upper()}:
{post.get('text', '')[:400]}

AUTHOR: {post.get('author_name', 'Unknown')}
ENGAGEMENT: {post.get('likes', 0)} likes, {post.get('comments', 0)} comments

BE INCLUSIVE. If the {r.item} is even tangentially related to AI, machine learning,
LLMs, automation, data, software/technology, business, workforce or professional
development, or broader industry trends, then a thoughtful professional CAN add a
meaningful {r.action} — set "verdict" to "engage". Only set "skip" when the {r.item} is
clearly OFF-TOPIC for an AI/tech audience (e.g. a purely personal life update with
no professional angle, unrelated personal fundraising, or pure copy-paste spam
with zero substance). A {r.item} merely being an announcement, promotion, news item,
or lacking a question or personal story is NOT a reason to skip. When in doubt,
choose "engage".

Also rate 0-10 (these are used ONLY to pick a {r.action} style — they do NOT decide
engagement): conversation_potential, author_engagement, content_depth, authenticity.

RESPOND WITH JSON:
{{
    "conversation_potential": <number>,
    "author_engagement": <number>,
    "content_depth": <number>,
    "authenticity": <number>,
    "post_category": "<personal_story|technical_discussion|thought_piece|question|announcement|promotion|fluff>",
    "engagement_approach": "<relate_experience|ask_clarification|add_perspective|share_similar|respectful_disagree|none>",
    "verdict": "<engage|skip>",
    "reason": "<one sentence explaining decision>"
}}"""
    
    def _log_api_usage(self, endpoint: str, est_cost: float, model: str = None):
        """Append a paid-API-call record to api_usage.jsonl (CLAUDE.md cost discipline)."""
        try:
            with open("api_usage.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "api": "openai",
                    "model": model or self.model,
                    "endpoint": endpoint,
                    "estimated_cost": est_cost,
                }) + "\n")
        except Exception:
            self.logger.debug("Failed to write api_usage.jsonl", exc_info=True)

    def evaluate_post_quality(self, post: Dict) -> Dict:
        """Evaluate if post is worth commenting on."""
        try:
            # GPT-4o-mini per PROJECT.md (was hardcoded gpt-3.5-turbo).
            self._log_api_usage("chat.completions:evaluate", 0.0002)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.register.eval_system_message()},
                    {"role": "user", "content": self.enhanced_quality_filter_prompt(post)}
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )

            return json.loads(response.choices[0].message.content)

        except Exception as e:
            self.logger.error(f"Evaluation error: {e}")
            # Bias toward engaging on transient eval errors (the user is an AI
            # consultant; the post finder already removed junk upstream).
            return {"verdict": "engage", "reason": f"Evaluation error, defaulting to engage: {e}"}
    
    def _relevance_prompt(self, post: Dict, comment: str) -> str:
        """The on-topic YES/NO prompt, in the platform's nouns.

        Extracted from :meth:`check_relevance` so it can be asserted byte-for-byte
        without mocking the OpenAI client.
        """
        r = self.register
        post_text = (post.get('text') or '')[:600]
        return (
            f"Decide whether {r.article} {r.surface} {r.action} stays on the {r.item}'s topic.\n\n"
            f"{r.item.upper()}:\n{post_text}\n\n"
            f"{r.action.upper()}:\n{comment}\n\n"
            f"Does this {r.action} directly respond to what THIS {r.item} is about, without "
            f"forcing in an unrelated industry, product, or area of expertise the {r.item} "
            "did not raise? Answer with exactly YES or NO."
        )

    def check_relevance(self, post: Dict, comment: str) -> bool:
        """Lightweight YES/NO check that a comment stays on the post's topic.

        Uses the cheap RELEVANCE_MODEL (gpt-4o-mini) and is logged to
        api_usage.jsonl. Returns True when the comment directly responds to the
        post without forcing in an unrelated industry/expertise angle. Defaults
        to True on any API error so a transient failure never blocks generation.
        """
        prompt = self._relevance_prompt(post, comment)
        try:
            self._log_api_usage("chat.completions:relevance", 0.0001, model=RELEVANCE_MODEL)
            response = self.client.chat.completions.create(
                model=RELEVANCE_MODEL,
                messages=[
                    {"role": "system",
                     "content": "You judge whether a comment is on-topic for a post. "
                                "Answer only YES or NO."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=3,
            )
            answer = (response.choices[0].message.content or "").strip().upper()
            relevant = answer.startswith("YES")
            if not relevant:
                self.logger.info("  Relevance check: NO (off-topic / forced expertise) — regenerating")
            return relevant
        except Exception as e:
            self.logger.warning(f"Relevance check error (defaulting to relevant): {e}")
            return True

    def get_matched_comment_style(self, post_eval: Dict) -> Tuple[Dict, str]:
        """Match comment style to post type and engagement approach."""
        
        # IMPROVED: Added new styles for more variety and authenticity
        styles = {
            "genuine_curious": {
                "name": "genuine_curious",
                "instruction": "You're genuinely interested in their experience/approach",
                "example": "I've been thinking about this same problem. How long did it take you to see results? I tried something similar but hit scaling issues around month 3",
                "good_for": ["personal_story", "technical_discussion"],
                "approaches": ["ask_clarification", "relate_experience"]
            },
            "thoughtful_addition": {
                "name": "thoughtful_addition", 
                "instruction": "Add a useful perspective without overshadowing their point",
                "example": "The point about context windows is key. I found that chunking by semantic boundaries instead of character count made a huge difference. Still figuring out the edge cases though",
                "good_for": ["technical_discussion", "thought_piece"],
                "approaches": ["add_perspective", "share_similar"]
            },
            "respectful_different": {
                "name": "respectful_different",
                "instruction": "Share a different experience respectfully",
                "example": "I went the opposite direction. Kept it simple with basic tooling and focused on data quality instead. Different problems need different solutions I guess",
                "good_for": ["thought_piece", "technical_discussion"],
                "approaches": ["respectful_disagree", "add_perspective"]
            },
            "empathetic_relate": {
                "name": "empathetic_relate",
                "instruction": "Connect through shared experience, be human",
                "example": "Oh man, I feel this. Last week my entire pipeline crashed because someone changed a schema without telling anyone. The joys of distributed teams. How are you preventing this now?",
                "good_for": ["personal_story", "question"],
                "approaches": ["relate_experience", "share_similar"]
            },
            "practical_helper": {
                "name": "practical_helper",
                "instruction": "Offer specific, practical help based on their situation",
                "example": "For the memory leak issue, I had something similar with transformer models. Setting gradient_checkpointing=True cut my memory usage by 40%. Not a complete fix but might buy you time",
                "good_for": ["question", "technical_discussion"],
                "approaches": ["add_perspective", "share_similar"]
            },
            "brief_supportive": {
                "name": "brief_supportive",
                "instruction": "Short, genuine support without overdoing it",
                "example": "The visualization approach in your second diagram is really clever. Hadn't thought of representing state transitions that way",
                "good_for": ["announcement", "personal_story"],
                "approaches": ["relate_experience"]
            },
            # NEW STYLES FOR BETTER VARIETY
            "direct_reaction": {
                "name": "direct_reaction",
                "instruction": "React directly to one specific point. Be curious or constructive, not just skeptical.",
                "example": "The context window bottleneck is real. I hit this at 100k tokens and had to completely rethink my chunking strategy.",
                "good_for": ["technical_discussion", "thought_piece"],
                "approaches": ["add_perspective"]
            },
            "minimal_agreement": {
                "name": "minimal_agreement",
                "instruction": "Show agreement with minimal words. Sometimes less is more.",
                "example": "This. The distributed systems part especially.",
                "good_for": ["thought_piece", "technical_discussion", "personal_story"],
                "approaches": ["add_perspective", "relate_experience"]
            },
            "specific_question": {
                "name": "specific_question",
                "instruction": "Ask about ONE specific technical detail mentioned in their post.",
                "example": "Wait, you got latency under 50ms? What stack? I'm stuck at 200ms with LangChain.",
                "good_for": ["technical_discussion", "announcement"],
                "approaches": ["ask_clarification", "relate_experience"]
            },
            "direct_challenge": {
                "name": "direct_challenge",
                "instruction": "Respectfully challenge the premise with specifics. Use sparingly - be constructive.",
                "example": "Not sure about this. The data quality argument assumes you control your sources. Most teams don't have that luxury with legacy systems.",
                "good_for": ["thought_piece"],  # Removed technical_discussion to make it less common
                "approaches": ["respectful_disagree"]  # Removed add_perspective
            }
        }
        
        # Match style to post
        post_category = post_eval.get('post_category', 'thought_piece')
        engagement_approach = post_eval.get('engagement_approach', 'add_perspective')
        
        # Find matching styles
        matching_styles = []
        for style_name, style_data in styles.items():
            if (post_category in style_data['good_for'] and 
                engagement_approach in style_data['approaches']):
                matching_styles.append(style_data)
        
        # Fallback to any compatible style
        if not matching_styles:
            for style_name, style_data in styles.items():
                if post_category in style_data['good_for']:
                    matching_styles.append(style_data)
        
        # Last resort - pick based on approach
        if not matching_styles:
            matching_styles = list(styles.values())
        
        selected_style = random.choice(matching_styles)
        return selected_style, engagement_approach
    
    def _build_persona_block(self) -> str:
        """Build the persona/tone/voice/topics/avoid/style-mix block from config.

        Returns "" when no config is present, so the generic prompt still works.
        These are additive guidance; the hard style rules (no dash punctuation,
        charming-not-snarky, I-not-we) are enforced separately in
        detect_ai_patterns regardless of what the config says.
        """
        cg = (self.config or {}).get("comment_generator", {})
        lines = []
        if cg.get("persona"):
            lines.append(
                f"WHO YOU ARE (this shapes your VOICE and JUDGMENT, not the topic "
                f"of every {self.register.action}): {cg['persona']}"
            )
        if cg.get("tone"):
            lines.append(f"TONE: {cg['tone']}")
        if cg.get("voice"):
            lines.append(f"VOICE: {cg['voice']}")
        topics = cg.get("topics_of_expertise") or []
        if topics:
            lines.append(
                "YOUR BACKGROUND (informs HOW you see things; surface it ONLY when "
                f"the {self.register.item} is genuinely about this area, never force it in): {', '.join(topics)}"
            )
        avoid = cg.get("things_to_avoid") or []
        if avoid:
            lines.append("AVOID: " + "; ".join(avoid))
        style_mix = cg.get("style_mix") or {}
        if style_mix:
            mix = ", ".join(
                f"{k.replace('_', ' ')} {int(round(float(v) * 100))}%"
                for k, v in style_mix.items()
            )
            lines.append(f"Vary {self.register.action} styles across "
                         f"{self.register.item_plural} roughly: {mix}")
        length_range = cg.get("comment_length_range") or []
        if len(length_range) == 2:
            # The unit travels with the range — a bare "15-60" is what let a
            # word-count band silently govern a character-bounded platform.
            lines.append("Aim for roughly " + self.length.describe_range(
                int(length_range[0]), int(length_range[1])) + ".")

        if not lines:
            return ""
        return "PROFILE (write as this specific person):\n" + "\n".join(lines)

    def create_authentic_comment_prompt(self, post: Dict, style: Dict, approach: str) -> str:
        """Create prompt for genuine human comments."""

        # Length distribution, weighted toward shorter. Buckets and weights are
        # declared per platform: LinkedIn counts words, X counts characters, and
        # the bands are not convertible (a 60-word reply is ~400 chars).
        length_instruction = random.choices(
            list(self.length.buckets),
            weights=list(self.length.bucket_weights)
        )[0]
        
        # IMPROVED: Much more specific authenticity rules. Register-bearing
        # lines (what "I" is, what the artefact is called, the length rule)
        # come from the platform; the craft rules below are shared.
        r = self.register
        length = self.length
        craft_rules = r.craft_rules.format(cap=length.typical_cap_phrase)
        authenticity_rules = f"""
⛔ THREE HARD RULES ({r.article} {r.action} that breaks any of these is rejected):
1. PUNCTUATION: Never use hyphens or em dashes as punctuation between clauses.
   Use commas, periods, or separate sentences instead. (Compound words like
   "real-time" or "fine-tune" are fine.)
2. TONE: {r.tone_rule}
3. VOICE: Always speak as "I", never "we", "our team", or "our company". You are
   {r.voice_identity}.

🚫 BANNED WORDS - If you use ANY of these, the {r.action} will be rejected:
thought-provoking | intriguing | fascinating | insightful | compelling
kudos | hats off | resonate | leverage | synergy | game changer
deep dive | double-click | at the end of the day

🚫 BANNED PATTERNS - These get {r.action_plural} rejected:
- Starting with "The part about..." (way overused, always gets flagged)
- Starting with "Name, your..." (e.g. "Harry, your perspective...")
- Ending with "isn't it?" or "right?" or "don't you think?" or "huh?"
- Starting with "Interesting perspective" or "Great {r.item}" or "Thanks for sharing"
- Starting with "Interesting that..." (another overused opener)
- Any "we" / "our team" / "our company" (speak as "I" only)
- ANY hyphen or em dash used as punctuation between clauses (use commas/periods)
- Starting with "Nah," "Hmm," "Honestly," more than 30% of the time
- Ending with "huh?" (use max 10% of time)

✅ WRITE LIKE A REAL PERSON:

{r.tone_balance}

Opening styles (vary these — NEVER repeat the same opening across {r.action_plural}):
{r.opening_styles}

{r.overuse_guidance}

Sentence style:
{r.sentence_style}

Rules:
{craft_rules}

GOOD QUESTION ENDINGS (specific, genuine):
{r.good_question_endings}

BAD QUESTION ENDINGS (rhetorical, filler):
"huh?" / "right?" / "isn't it?" / "no?" / "yeah?"

SHORT EXAMPLES (this is the target):
{r.short_examples}
"""
        
        # Persona / style guidance from the profile config (falls back to {}).
        persona_block = self._build_persona_block()

        return f"""Write {r.article} {r.surface} {r.action} with this approach:

Style: {style['name']}
Instruction: {style['instruction']}
Engagement approach: {approach}
Length: {length_instruction}

{persona_block}
{platform_policy.on_topic_rules(r)}
Example of natural {r.action}: "{r.example_for(style)}"

{r.item.upper()} TO RESPOND TO:
Author: {post.get('author_name', 'Unknown')}
{post.get('text', '')[:500]}

{authenticity_rules}{r.extra_rules}

Additional guidance:
- Reference SPECIFIC details from their {r.item} (numbers, tools, situations)
- Don't try to sound smart - be clear and direct
- Incomplete sentences are fine
- Not every {r.action} needs a question
- Real humans are sometimes wrong or uncertain - that's ok
- Occasionally make a bold/contrarian point

Write ONLY the {r.action} text:"""
    
    def detect_ai_patterns(self, comment: str) -> Tuple[bool, List[str]]:
        """Detect if comment has obvious AI-generated patterns."""
        
        ai_tells = {
            "overused_adjectives": [
                "thought-provoking", "intriguing", "fascinating", 
                "insightful", "compelling", "innovative"
            ],
            "formal_phrases": [
                "kudos to", "hats off", "i resonate with", 
                "this resonates", "i appreciate", "well said"
            ],
            "tag_questions": [
                "isn't it?", "right?", "don't you think?", 
                "wouldn't you agree?", "doesn't it?", "huh?", ", huh?"
            ],
            "generic_openings": [
                "interesting perspective",
                "fascinating approach", 
                "intriguing concept",
                "great post",
                "thanks for sharing",
                "loved this",
                "really enjoyed",
                "the part about",
                "interesting that"
            ],
            "ai_connectors": [
                "at the end of the day",
                "when all is said and done",
                "it's all about finding the right balance",
                "it's a delicate balance"
            ],
            "overused_casual": [
                "nah,", "hmm,", "honestly,"
            ]
        }
        
        comment_lower = comment.lower()
        issues = []
        
        # Check for AI tells
        for category, phrases in ai_tells.items():
            for phrase in phrases:
                if phrase in comment_lower:
                    issues.append(f"{category}: '{phrase}'")
        
        # Length check, in the unit this platform actually measures. LinkedIn
        # counts words against 70; X counts characters against 280, where a
        # 60-word draft is ~400 chars and cannot be posted at all.
        if self.length.exceeds_hard_max(comment):
            issues.append(f"too_long: {self.length.measure(comment)} "
                          f"{self.length.unit} (aim for <{self.length.aim_below})")
        
        # RULE 1: no hyphen / em dash / en dash used as punctuation between
        # clauses. Compound words like "real-time" or "fine-tune" use no
        # surrounding spaces, so they are unaffected.
        dash_hits = []
        if ' - ' in comment:
            dash_hits.append("' - '")
        if '—' in comment:  # em dash
            dash_hits.append("em dash")
        if '–' in comment:  # en dash
            dash_hits.append("en dash")
        if dash_hits:
            issues.append(f"dash_as_punctuation: {', '.join(dash_hits)}")
        
        # Check if ends with common tag question
        comment_trimmed = comment.strip().rstrip('.')
        for tag_q in ai_tells["tag_questions"]:
            if comment_trimmed.lower().endswith(tag_q.rstrip('?')):
                issues.append("ends_with_tag_question")
                break
        
        # Check for formal name opening (e.g., "Harry, your perspective...")
        if "," in comment[:40]:
            words_before_comma = comment[:comment.find(",")].split()
            if len(words_before_comma) <= 3 and any(w[0].isupper() for w in words_before_comma):
                issues.append("formal_name_opening")
        
        # Check for "We've found" pattern (overused personal experience)
        personal_phrases = ["in my experience", "i discovered", "i learned"]
        personal_count = sum(1 for phrase in personal_phrases if phrase in comment_lower)
        if personal_count > 1:
            issues.append("too_many_personal_references")

        # RULE 3: first person singular only — reject first-person-plural voice.
        # \bwe\b also catches we've / we're / we'll / we'd (apostrophe is a
        # word boundary). "our team" / "our company" are covered by \bour\b.
        plural_patterns = [r"\bwe\b", r"\bour\b", r"\bours\b", r"\bus\b", r"\bourselves\b"]
        if any(re.search(p, comment_lower) for p in plural_patterns):
            issues.append("first_person_plural")

        # RULE 4: no self-positioning OPENER, where the platform bans them.
        # Checked only at the start: mid-draft self-reference is normal on both
        # platforms ("...we hit it at 100k, i chunked it"). It is leading WITH
        # yourself that reads as introducing your credentials before speaking.
        # LinkedIn declares no banned openers, so this is a no-op there.
        opening = comment_lower.lstrip().lstrip(chr(34)).lstrip(chr(39)).lstrip()
        for opener in self.register.banned_openers:
            if opening.startswith(opener):
                issues.append(f"self_positioning_opener: {opener!r}")
                break

        is_authentic = len(issues) == 0
        return is_authentic, issues
    
    def generate_comment(self, post: Dict, evaluation: Dict) -> Optional[Dict]:
        """Generate an authentic comment with AI pattern detection."""
        try:
            style, approach = self.get_matched_comment_style(evaluation)
            
            # Try up to 3 times to get an authentic comment
            best_attempt = None
            best_score = float('inf')  # Lower is better (fewer issues)
            
            for attempt in range(3):
                prompt = self.create_authentic_comment_prompt(post, style, approach)
                
                # IMPROVED: Higher temperature for more variety, increase with each attempt
                temp = random.uniform(0.6 + (attempt * 0.1), 0.9)

                self._log_api_usage("chat.completions:generate", 0.0003)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": self.register.system_voice
                        },
                        {"role": "user", "content": prompt}
                    ],
                    temperature=temp,
                    max_tokens=100  # Force brevity
                )
                
                comment = response.choices[0].message.content.strip()
                comment = comment.strip('"\'')  # Remove quotes if added
                
                # Check authenticity with new detector
                is_authentic, issues = self.detect_ai_patterns(comment)

                word_count = len(comment.split())

                # Relevance gate: only spend the extra call on a comment we'd
                # otherwise accept (authentic), and only when the flag is on.
                relevant = True
                if is_authentic and self.stay_on_post_topic:
                    relevant = self.check_relevance(post, comment)

                current = {
                    'comment': comment,
                    'word_count': word_count,
                    'style': style['name'],
                    'approach': approach,
                    'generated_at': datetime.now().isoformat(),
                    'issues': issues,
                    'relevant': relevant,
                    'attempt': attempt + 1
                }

                # Track best attempt for the fallback (fewest AI-pattern issues).
                if len(issues) < best_score:
                    best_score = len(issues)
                    best_attempt = current

                # If authentic AND on-topic, use THIS attempt immediately.
                if is_authentic and relevant:
                    self.logger.info(f"✓ Authentic, on-topic comment on attempt {attempt + 1}")
                    return current
                elif is_authentic and not relevant:
                    self.logger.debug(f"Attempt {attempt + 1}: off-topic / forced expertise")
                else:
                    self.logger.debug(f"Attempt {attempt + 1}: Issues - {', '.join(issues[:3])}")
            
            # Use best attempt even if not perfect — EXCEPT when it breaks a
            # limit the platform itself enforces. Truncation is not a length
            # policy: a reply cut mid-sentence at 280 characters is worse than no
            # reply, and an over-limit draft would fail at the composer anyway.
            # On LinkedIn hard_max is a quality heuristic (the platform accepts a
            # long comment), so the existing behaviour is unchanged there.
            if best_attempt and self.length.hard_max_is_postable_limit \
                    and self.length.exceeds_hard_max(best_attempt['comment']):
                self.logger.warning(
                    "Discarding best attempt: %d %s exceeds %s's hard limit of %d "
                    "after %d attempts. Not truncating — returning no draft.",
                    self.length.measure(best_attempt['comment']), self.length.unit,
                    self.register.surface, self.length.hard_max, 3)
                return None

            if best_attempt:
                if best_attempt['issues']:
                    self.logger.warning(f"Using best attempt with {len(best_attempt['issues'])} issues: {best_attempt['issues'][0]}")
                return best_attempt

            return None
            
        except Exception as e:
            self.logger.error(f"Generation error: {e}")
            return None
    
    def select_best_posts(self, posts: List[Dict]) -> List[Dict]:
        """Select posts worth commenting on with better criteria."""
        self.logger.info(f"Evaluating {len(posts)} posts...")
        
        # Drop already-commented posts (keep url-less posts so the evaluator is
        # still measured on them; generation later requires a URL).
        new_posts = []
        skipped_already = 0
        for post in posts:
            if post.get('url') and post['url'] in self.posted_urls:
                skipped_already += 1
                continue
            new_posts.append(post)
        if skipped_already:
            self.logger.info(f"Skipped {skipped_already} already-commented posts")

        selected = []
        engage_count = 0
        no_url_but_engage = 0
        # Posts this run looked at and turned down. Recorded so they can be moved
        # out of NEW — otherwise they are re-evaluated (and re-billed) forever.
        self.rejected_posts = []

        for i, post in enumerate(new_posts):
            text_lower = post.get('text', '').lower()

            # Cheap auto-rejects (blatant promo / hashtag spam).
            if any(spam in text_lower for spam in ['link in bio', 'dm me for', '🚨 bonus share']):
                self.logger.info(f"Post {i+1}: SKIP (auto: spam/promotion)")
                self.rejected_posts.append(post)
                continue
            hashtag_count = post.get('text', '').count('#')
            if hashtag_count > 8:
                self.logger.info(f"Post {i+1}: SKIP (auto: {hashtag_count} hashtags)")
                self.rejected_posts.append(post)
                continue

            evaluation = self.evaluate_post_quality(post)
            post['evaluation'] = evaluation
            verdict = evaluation.get('verdict', 'engage')
            reason = evaluation.get('reason', '')

            # Log EVERY post's evaluation result (engage AND skip) with reasoning.
            self.logger.info(
                f"Post {i+1}: verdict={verdict} | {evaluation.get('post_category', '?')} | "
                f"conv={evaluation.get('conversation_potential', '-')} "
                f"auth={evaluation.get('authenticity', '-')} "
                f"depth={evaluation.get('content_depth', '-')} | "
                f"{post.get('author_name', '?')[:20]} | reason: {reason}"
            )

            # Engage unless the model explicitly says skip. The 0-10 scores are
            # advisory (used for ranking / comment style), NOT hard gates — the
            # old authenticity<5 / conversation_potential<5 gates rejected good
            # AI posts (announcements, news, thought pieces).
            if verdict == 'skip':
                self.logger.info(f"Post {i+1}: REJECTED (verdict=skip) — {reason}")
                self.rejected_posts.append(post)
                continue

            engage_count += 1
            if not post.get('url'):
                no_url_but_engage += 1
                self.logger.warning(f"Post {i+1}: worth engaging but NO URL — cannot post, skipping")
                continue
            selected.append(post)

        self.logger.info(f"\n{engage_count}/{len(new_posts)} posts deemed worth engaging by the evaluator")
        if no_url_but_engage:
            self.logger.info(f"  ({no_url_but_engage} were worth engaging but had no URL to post to)")

        selected.sort(
            key=lambda x: (
                x['evaluation'].get('conversation_potential', 0) +
                x['evaluation'].get('authenticity', 0)
            ),
            reverse=True
        )
        self.logger.info(f"{len(selected)} engageable posts have URLs and will get comments")

        # No cap by default: generate for ALL engaging posts. Only slice when the
        # user set an explicit max_comments ceiling.
        if self.max_comments is None:
            return selected
        return selected[:self.max_comments]
    
    def generate_all_comments(self):
        """Generate varied authentic comments."""
        data = self.load_posts()
        posts = data.get('quality_posts', [])
        
        # Show filtering stats
        self.logger.info("\n📊 Comment Generation Summary:")
        self.logger.info(f"  Total posts in file: {len(posts)}")
        self.logger.info(f"  Already commented on: {len([p for p in posts if p.get('url') in self.posted_urls])}")
        self.logger.info(f"  New posts to evaluate: {len([p for p in posts if p.get('url') not in self.posted_urls])}")
        
        best_posts = self.select_best_posts(posts)

        # Record the turn-downs BEFORE the early return: a run where everything
        # was rejected is exactly the case that used to leave posts stuck in NEW.
        self._trash_rejected_in_store()

        if not best_posts:
            self.logger.warning("\n❌ No suitable posts found!")
            self.logger.info("Possible reasons:")
            self.logger.info("  - All posts have already been commented on")
            self.logger.info("  - Remaining posts are too promotional")
            self.logger.info("  - Need to find fresh posts with the post finder")
            return []
        
        # Cost guard: with no cap this can generate for every engaging post. Warn
        # (but proceed — the user asked for it) past a reasonable single-run size.
        # Every call is logged to api_usage.jsonl and the PROJECT.md $5/session cap
        # still applies, so spend stays visible.
        if len(best_posts) >= 100:
            self.logger.warning(
                f"⚠️  About to generate {len(best_posts)} comments in one run — that's "
                f"a lot of OpenAI calls. Proceeding as requested; watch api_usage.jsonl "
                f"and the PROJECT.md $5/session cap."
            )

        self.logger.info(f"\n✅ Generating {len(best_posts)} authentic comments...")

        results = []
        style_counts = {}
        approach_counts = {}
        
        for i, post in enumerate(best_posts):
            self.logger.info(f"\nPost {i+1}/{len(best_posts)}")
            self.logger.info(f"  Category: {post['evaluation'].get('post_category')}")
            self.logger.info(f"  Author: {post.get('author_name', 'Unknown')}")
            
            # Try to generate comment (now with built-in retries)
            comment_data = self.generate_comment(post, post['evaluation'])
            
            if comment_data:
                style = comment_data['style']
                approach = comment_data['approach']
                style_counts[style] = style_counts.get(style, 0) + 1
                approach_counts[approach] = approach_counts.get(approach, 0) + 1
                
                result = {
                    'post_url': post.get('url'),
                    'post_text': post.get('text'),
                    'post_author': post.get('author_name'),
                    'comment': comment_data['comment'],
                    'word_count': comment_data['word_count'],
                    'style': style,
                    'approach': approach,
                    'post_category': post['evaluation'].get('post_category'),
                    'generated_at': comment_data['generated_at']
                }
                results.append(result)

                self.logger.info(f"✓ {style} / {approach} ({comment_data['word_count']} words)")
                # Running tally so a long uncapped run shows progress + spend.
                self.logger.info(
                    f"  Running total: {len(results)}/{len(best_posts)} comments generated "
                    f"(see api_usage.jsonl for per-call cost)"
                )
            else:
                self.logger.error("Failed to generate acceptable comment")
            
            time.sleep(2)
        
        # Log variety metrics
        self.logger.info("\nStyle distribution:")
        for style, count in sorted(style_counts.items()):
            self.logger.info(f"  {style}: {count}")
            
        self.logger.info("\nApproach distribution:")
        for approach, count in sorted(approach_counts.items()):
            self.logger.info(f"  {approach}: {count}")
        
        self.save_results(results)
        self._mark_generated_in_store(results)
        return results

    def _trash_rejected_in_store(self):
        """Move every post this run turned down NEW → TRASH(evaluator_rejected).

        Without this a rejected post stays NEW forever and is re-sent to the LLM
        on every subsequent run — the "zombie NEW" bug. ``reject_by_evaluator``
        only touches records still in NEW, so nothing already acted on is
        affected. Best-effort: a store failure must never fail a generation run.
        """
        rejected = getattr(self, "rejected_posts", None)
        if not (HAS_POST_STORE and self.resolved_profile and rejected):
            return
        try:
            store = post_store.PostStore(self.resolved_profile)
            moved = 0
            for post in rejected:
                url = (post.get("url") or "").strip()
                if url and store.reject_by_evaluator(url):
                    moved += 1
            if moved:
                store.save()
                self.logger.info(
                    f"Moved {moved} evaluator-rejected post(s) out of NEW "
                    f"→ TRASH(evaluator_rejected); they will not be re-evaluated."
                )
        except Exception:
            self.logger.warning("Could not record evaluator rejections", exc_info=True)

    def _mark_generated_in_store(self, results: List[Dict]):
        """Move each commented-on post NEW → GENERATED in the lifecycle store.

        Best-effort: a store failure must not lose the comments (already saved to
        files), so this logs and continues. No-op without a profile manager.
        """
        if not (HAS_POST_STORE and self.resolved_profile and results):
            return
        try:
            store = post_store.PostStore(self.resolved_profile)
            for r in results:
                url = r.get("post_url") or ""
                if not url:
                    continue
                store.mark_generated(url, r.get("comment", ""), meta={
                    "style": r.get("style", ""),
                    "approach": r.get("approach", ""),
                    "word_count": r.get("word_count", 0),
                })
            store.save()
        except Exception:
            self.logger.warning("Could not update lifecycle store", exc_info=True)

    def save_results(self, results: List[Dict]):
        """Save comments to files."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # JSON
        json_file = os.path.join(self.output_dir, f'comments_{timestamp}.json')
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump({
                'generated': datetime.now().isoformat(),
                'total': len(results),
                'already_posted_urls_skipped': len(self.posted_urls),
                'comments': results
            }, f, indent=2, ensure_ascii=False)
        
        # Text file
        text_file = os.path.join(self.output_dir, f'daily_comments_{timestamp}.txt')
        with open(text_file, 'w', encoding='utf-8') as f:
            f.write(self.register.report_header())
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"Total: {len(results)}\n")
            f.write(f"Skipped (already posted): {len(self.posted_urls)}\n")
            f.write("="*60 + "\n\n")
            
            for i, r in enumerate(results):
                f.write(f"POST {i+1}\n")
                f.write(f"URL: {r['post_url']}\n")
                f.write(f"Style: {r['style']} / {r['approach']}\n")
                f.write(f"Category: {r['post_category']}\n")
                f.write(f"\nPost Preview:\n{r['post_text'][:150]}...\n")
                f.write(f"\nYour Comment ({r['word_count']} words):\n")
                f.write(f'"{r["comment"]}"\n')
                f.write("\n" + "-"*60 + "\n\n")
        
        self.logger.info("\nFiles saved:")
        self.logger.info(f"  {json_file}")
        self.logger.info(f"  {text_file}")
        
        if results:
            lengths = [r['word_count'] for r in results]
            self.logger.info("\nComment quality metrics:")
            self.logger.info(f"  Word count range: {min(lengths)}-{max(lengths)}")
            self.logger.info(f"  Average word count: {sum(lengths)//len(lengths)}")
            self.logger.info(f"  Unique styles: {len(set(r['style'] for r in results))}")
            self.logger.info(f"  Unique approaches: {len(set(r['approach'] for r in results))}")
        
        if len(self.posted_urls) > 0:
            self.logger.info(f"\n💰 Token savings: Skipped {len(self.posted_urls)} already-posted URLs")


# The class was ``AuthenticLinkedInCommentGenerator`` while LinkedIn was the only
# platform. The alias keeps existing imports working; new code should use the
# neutral name. (Kept deliberately: renaming callers is churn with no behavioural
# benefit, and the alias documents where the LinkedIn assumption used to live.)
AuthenticLinkedInCommentGenerator = AuthenticCommentGenerator


def build_arg_parser():
    """The CLI parser, built separately so its contract is testable.

    Specifically: that ``--platform`` is required and carries no default. That is
    an invariant worth a test, not an implementation detail — a default here
    would reintroduce the silent LinkedIn fallback at every subprocess caller.
    """
    parser = argparse.ArgumentParser(description='Generate authentic comments/replies')
    parser.add_argument('input_file', help='Path to AI posts JSON')
    parser.add_argument('--model', default='gpt-4o-mini', help='Model to use')
    parser.add_argument('--limit', type=int, default=None,
                        help='Max drafts to generate (default: no limit, generate for all engaging posts)')
    parser.add_argument('--profile', type=str, default=None, help='Profile name (for data directory)')
    # REQUIRED, with no default. A default here would be the same silent
    # LinkedIn fallback the constructor refuses — argparse would fill it in and
    # every subprocess caller would inherit a platform nobody chose. Missing it
    # exits 2 with a usage error, loudly.
    parser.add_argument('--platform', required=True,
                        choices=sorted(platform_policy.PLATFORMS),
                        help='Platform whose register and length policy to use')
    return parser


def main(argv=None):
    """CLI entry point: generate drafts for a scraped posts JSON file."""
    args = build_arg_parser().parse_args(argv)

    if not os.getenv('OPENAI_API_KEY'):
        print("Error: OPENAI_API_KEY missing")
        return

    generator = AuthenticCommentGenerator(
        args.input_file, args.model, args.limit, profile_name=args.profile,
        platform=args.platform
    )
    
    results = generator.generate_all_comments()
    
    if results:
        print(f"\n✓ Generated {len(results)} authentic comments")


if __name__ == "__main__":
    main()