"""Tests for the ad/sponsored/job-card filter in linkedin_ai_post_finder.

No browser, no network: classify_ad is a pure function. Fixtures are synthetic
but modeled on the real feed shapes the filter must separate: company product
marketing and job/recommendation cards are filtered, while individuals' genuine
commentary passes through. (Person names and profile URLs here are invented;
company examples are public brands used only to exercise the ad heuristic.)"""

from linkedin_automation import post_finder as f
from linkedin_automation.post_finder import classify_ad

IN = "https://www.linkedin.com/in/"
CO = "https://www.linkedin.com/company/"


# ─── Representative posts from the scan (author, text, url, promoted) ──────────

CLICKUP_LIVE = (
    "ClickUp",
    "Brain is live. If you haven't tried it yet, here's why you need to. "
    "Every AI tool on the market has the same problem. Watch this. Then try it "
    "yourself. Don't take our word for it.",
    CO + "clickup-app/", False,
)
CLICKUP_MODELS = (
    "ClickUp",
    "Most AI tools lock you into one model. ClickUp Brain gives you the choice. "
    "No extra cost on any paid plan. Meet Brain, your company's AI.",
    CO + "clickup-app/", False,
)
STATS_PERFORM = (
    "Stats Perform",
    "In elite football, better decisions depend on connected data. Bayer 04 "
    "Leverkusen has expanded its partnership with Stats Perform, integrating "
    "Opta Vision data. Read more about the expanded partnership https://bit.ly/x",
    CO + "stats-perform/", False,
)
JOB_CARD = (
    "Unknown",
    "Feed post\nJobs recommended for you\nGen AI Consultant (Verified job)\n"
    "InterEx Group\nUnited States (Remote)\nActively reviewing applicants",
    "", False,
)
JOB_UPDATE = (
    "Dana Cole",
    "Feed post\nDana Cole's job update\nstarted a new position\n"
    "Senior Talent Acquisition Specialist",
    IN + "dana-cole", False,
)
PROMOTED = (
    "OpenAI Developers",
    "Bring Codex to your team without fixed seat costs. We're rolling out "
    "usage-based pricing so teams have a flexible way to get started.",
    CO + "openai-devs/", True,
)

# Should PASS — individuals teaching/informing, even when the opener reads like
# a pitch or mentions SaaS.
MORGAN_RESEARCH = (
    "Morgan Diaz",
    "Most AI coding agents treat each research attempt as a blank slate. A new "
    "open framework from a university lab and a large tech company builds a "
    "persistent hypothesis tree instead.",
    IN + "morgandiaz/", False,
)
CHRIS_ADVICE = (
    "Chris Bell",
    "If you are building with AI, start small and close. It could be something "
    "too specific to buy as a SaaS product but valuable to you.",
    IN + "chrisbell2911/", False,
)
PRIYA_INSIGHT = (
    "Priya Nair",
    "The most valuable thing in AI right now is not compute. It is the people "
    "who actually know how to build the models. Talent is the real moat.",
    IN + "priya-nair-536", False,
)
# Legitimate company post about the wider industry (must NOT be filtered).
SEER_INDUSTRY = (
    "Seer",
    "The three most important IPOs in a generation are happening right now. "
    "SpaceX. OpenAI. Anthropic. Combined valuation: ~$3.6 trillion.",
    CO + "seer/", False,
)


def _classify(post, ad_indicators=None):
    name, text, url, promoted = post
    return classify_ad(name, text, url, promoted, ad_indicators)


# ─── The spec's headline assertions ───────────────────────────────────────────

def test_clickup_posts_filtered_by_heuristic_without_blocklist():
    # No ad_indicators — the company self-promotion heuristic must catch both.
    assert _classify(CLICKUP_LIVE) == "likely ad"
    assert _classify(CLICKUP_MODELS) == "likely ad"


def test_stats_perform_filtered():
    # No literal CTA from the spec's list; caught via self-branding + "read more".
    assert _classify(STATS_PERFORM) == "likely ad"


def test_job_card_filtered():
    assert _classify(JOB_CARD) == "job/recommendation card"


def test_named_individuals_pass_through():
    assert _classify(MORGAN_RESEARCH) is None
    assert _classify(CHRIS_ADVICE) is None
    assert _classify(PRIYA_INSIGHT) is None


def test_legit_company_industry_post_passes():
    assert _classify(SEER_INDUSTRY) is None


# ─── Per-layer behaviour ──────────────────────────────────────────────────────

def test_promoted_is_filtered_first():
    assert _classify(PROMOTED) == "promoted/sponsored"


def test_job_update_card_filtered_even_for_individual():
    assert _classify(JOB_UPDATE) == "job/recommendation card"


def test_blocklist_substring_match():
    # Config blocklist catches the advertiser by name even if the body were tame.
    tame = ("ClickUp App", "Here is a neutral industry observation.", CO + "clickup-app/", False)
    assert _classify(tame, ad_indicators=["ClickUp"]) == "blocklisted advertiser"
    # And an unrelated company is untouched by that blocklist.
    assert _classify(SEER_INDUSTRY, ad_indicators=["ClickUp"]) is None


def test_individual_pitch_opener_not_treated_as_company_ad():
    # "Most AI ... tools" opener on an individual must not trip the company path.
    person = (MORGAN_RESEARCH[0],
              "Most AI tools are overhyped. Here is what the research actually shows.",
              IN + "morgandiaz/", False)
    assert _classify(person) is None


def test_single_signal_company_post_not_filtered():
    # One incidental CTA phrase (score 1) is below the >=2 threshold.
    company = ("Acme Research",
               "We published a study on transformers. Learn more in the paper.",
               CO + "acme/", False)
    assert _classify(company) is None


def test_unknown_author_feed_post_card_filtered():
    card = ("Unknown", "Feed post\nBoost your best posts easily\nLearn more", "", False)
    assert _classify(card) == "recommendation card"


def test_config_default_has_ad_indicators_key():
    # Loader reads post_finder.ad_indicators; the template must expose it.
    import json
    cfg = json.load(open("default_profile_config.json", encoding="utf-8"))
    assert "ad_indicators" in cfg["post_finder"]


def test_constants_exposed():
    assert "is live" in f.AD_CTA_SIGNALS
    assert "verified job" in f.JOB_CARD_PATTERNS
