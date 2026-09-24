"""Scrape the LinkedIn feed for high-quality AI-discussion posts.

Drives a Selenium Chrome session (via ``linkedin_profile_manager``) to scroll the
feed, score posts, and save the best ones as ``ai_posts_*.json`` for the comment
generator. Exits 0 on success, 2 on login failure, 1 on other errors.
"""

import json
import os
import re
import sys
import random
import hashlib
import subprocess
import requests
from datetime import datetime
from typing import List, Optional, Tuple, Set
from dataclasses import dataclass, field, asdict
from enum import Enum

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from dotenv import load_dotenv
import logging
from . import profile_manager as pm
from . import human_behavior as hb
from . import post_store
from . import post_urn
from .failure_capture import capture_failure

load_dotenv()


class PostQuality(Enum):
    """Post quality levels"""
    HIGH = "high"
    MEDIUM = "medium" 
    LOW = "low"
    SKIP = "skip"


@dataclass
class LinkedInPost:
    """Data class for LinkedIn posts"""
    url: Optional[str] = None
    activity_urn: Optional[str] = None
    author_name: str = "Unknown"
    author_title: str = ""
    author_profile_url: Optional[str] = None
    text: str = ""
    likes: int = 0
    comments: int = 0
    reposts: int = 0
    is_promoted: bool = False
    should_engage: bool = False
    engagement_reason: str = ""
    relevance_score: int = 0
    quality: PostQuality = PostQuality.SKIP
    post_type: str = ""  # discussion, career, announcement, etc.
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    keywords_matched: List[str] = field(default_factory=list)
    
    def get_identifier(self) -> str:
        """Get unique identifier for duplicate detection"""
        if self.activity_urn:
            return self.activity_urn
        if self.url:
            return self.url
        # Fallback to content hash
        content = f"{self.author_name}:{self.text[:200]}"
        return hashlib.md5(content.encode()).hexdigest()


# ─── Ad / sponsored / job-card detection ──────────────────────────────────────
# Runs BEFORE quality scoring so ads never waste an OpenAI evaluation call. The
# guiding distinction (see CLAUDE.md / the ad-filter spec): does the post
# teach/inform, or does it sell? A person discussing someone else's research
# passes; a company pitching its own product does not.

# Calls-to-action and product-pitch phrases that signal selling rather than
# informing. Only applied to COMPANY-authored posts, so phrases like "meet " or
# "gives you" can't misfire on an individual's genuine commentary.
AD_CTA_SIGNALS = (
    "our product", "we built", "try it", "try it yourself", "sign up",
    "get started", "now available", "launching", "is live", "you haven't tried",
    "you havent tried", "no extra cost", "rolling out", "meet ",
    "read more about", "learn more", "book a demo", "request a demo",
    "register today", "watch this", "don't take our word", "dont take our word",
)

# Phrases that mark a job listing, "people you may know", or other non-post card
# LinkedIn injects into the feed. Matched for ANY author (these are unambiguous).
JOB_CARD_PATTERNS = (
    "jobs recommended for you", "verified job", "started a new position",
    "job update", "we are hiring", "actively reviewing applicants",
    "recommended for you",
)

# ─── Recruiter job-ad heuristic ───────────────────────────────────────────────
# Member-authored recruiter posts are NOT feed cards, so JOB_CARD_PATTERNS misses
# them and they land in NEW, where the comment evaluator burns an LLM call
# rejecting each one. They are caught here instead — before any scoring or API
# call — but only when BOTH a hiring headline and the structural shape of a job
# listing are present.
#
# The two-signal rule is deliberate and calibrated against real data: this
# profile genuinely engages with conversational AI-hiring posts ("I'm hiring for
# a role at the intersection of quant research and ML...", "We're hiring across
# the company, but I'm spotlighting a few roles..."). Those carry a hiring
# headline and no listing structure, so they must keep passing. Validated on the
# live store: 7 recruiter ads caught, 0 false positives across all 297 posts this
# profile has drafted or posted a comment on.
RECRUITER_HEADLINE_PATTERNS = (
    r"\b(?:now|urgently|immediately|actively|massive|mass)\s+hiring\b",
    r"\bwe(?:'|’)?re\s+hiring\b",
    r"\bwe\s+are\s+hiring\b",
    r"\bhiring\s*[:\-–—|]",
    r"\bhiring\s+(?:for|alert|update|across)\b",
    r"\b(?:job|role|position)\s+opening[s]?\b",
    r"\bopen\s+position[s]?\b",
    r"\bvacanc(?:y|ies)\b",
    r"\bnow\s+recruiting\b",
    r"\bwalk[\s-]?in\s+(?:drive|interview)\b",
)

# Structural markers of a job listing (the fielded "Location: / Experience: /
# Job Type:" shape, application instructions, requirement blocks). Deliberately
# excludes soft phrases like "join our team" or "we're looking for", which appear
# constantly in ordinary posts.
RECRUITER_LISTING_SIGNALS = (
    "location:", "experience:", "job type:", "employment type:", "work mode:",
    "timings:", "salary", "ctc", "full-time", "full time", "part-time",
    "on-site", "onsite", "immediate joiner", "notice period",
    "send your resume", "share your resume", "share your cv", "drop your cv",
    "dm your resume", "apply at", "apply here", "how to apply", "apply now",
    "required skills", "responsibilities:", "requirements:", "qualifications:",
    "years of experience", "yrs of experience", "job description",
    "interested candidates", "eligible candidates", "shortlisted",
)
RECRUITER_LISTING_PATTERNS = (
    r"\(\s*\d+\s*[-–—]\s*\d+\s*(?:\+\s*)?(?:years|yrs)",   # "(1-2 years)"
    r"\b\d+\s*[-–—]\s*\d+\s*(?:years|yrs)\b",              # "3-8 Years"
    r"\b\d+\s+position[s]?\b",                                       # "3 Positions"
)

# A hiring headline plus this many listing signals marks a job ad.
RECRUITER_LISTING_THRESHOLD = 2


def is_recruiter_job_ad(text: str) -> bool:
    """True when ``text`` is a recruiter job listing rather than a post about work.

    Requires at least one hiring headline AND ``RECRUITER_LISTING_THRESHOLD``
    structural job-listing signals, so an industry post that merely mentions
    hiring in passing (e.g. "...even as much of the IT industry remains cautious
    on hiring") scores zero and is never filtered.
    """
    low = (text or "").lower()
    if not any(re.search(p, low) for p in RECRUITER_HEADLINE_PATTERNS):
        return False
    signals = sum(1 for s in RECRUITER_LISTING_SIGNALS if s in low)
    signals += sum(1 for p in RECRUITER_LISTING_PATTERNS if re.search(p, low))
    return signals >= RECRUITER_LISTING_THRESHOLD


def _is_company_author(author_profile_url: str) -> bool:
    """True when the actor is a company Page (a /company/ link, not a /in/ member)."""
    url = (author_profile_url or "").lower()
    if "/in/" in url:
        return False
    return "/company/" in url


def classify_ad(author_name: str, text: str, author_profile_url: str,
                is_promoted: bool, ad_indicators=None) -> Optional[str]:
    """Return a short reason string if the post is an ad/job/recommendation card,
    else None. Designed to be cheap and side-effect free so it can run on every
    post before any scoring or OpenAI call.

    Detection order: promoted/sponsored -> config blocklist -> job/recommendation
    card -> company self-promotion heuristic. Individual ("/in/") authors are
    never caught by the company heuristic, so genuine commentary that happens to
    open like a pitch (e.g. "Most AI agents...") still passes.
    """
    name = (author_name or "").strip()
    name_low = name.lower()
    low = (text or "").lower()
    indicators = [a.strip().lower() for a in (ad_indicators or []) if a and a.strip()]

    # 1. Sponsored/promoted (DOM "Promoted"/"Sponsored" marker via is_promoted).
    if is_promoted:
        return "promoted/sponsored"

    # 2. User-configured advertiser blocklist (substring so "ClickUp" matches
    #    "ClickUp App", etc.).
    for ind in indicators:
        if ind in name_low:
            return "blocklisted advertiser"

    # 3. Job listings, people-you-may-know, and other injected non-post cards.
    #    Explicit job/recommendation phrases first (most specific), then the
    #    generic "Unknown author + non-post card" catch-all.
    for pat in JOB_CARD_PATTERNS:
        if pat in low:
            return "job/recommendation card"
    # Member-authored recruiter ads (headline + job-listing structure).
    if is_recruiter_job_ad(text):
        return "job/recommendation card"
    stripped = low.lstrip()
    if name_low == "unknown" and (
        stripped.startswith("feed post")
        or "shared connection" in low
        or "followed by" in low
    ):
        return "recommendation card"

    # 4. Company product marketing. Score self-promotion signals; flag at >= 2 so
    #    a single incidental phrase can't filter a legitimate company post about
    #    AI research or industry insight (which scores 0).
    if _is_company_author(author_profile_url):
        score = sum(1 for s in AD_CTA_SIGNALS if s in low)
        # Product-pitch openers from the spec.
        if re.match(r"\s*most\s+[\w'\- ]{0,24}\btools\b", low):
            score += 1                       # "Most [product] tools..."
        if "gives you" in low:
            score += 1                       # "[Product] gives you..."
        # Self-reference: the company names its own brand in the body (selling
        # itself, vs. informing about the wider field).
        if name and re.search(r"\b" + re.escape(name_low) + r"\b", low):
            score += 1
        if score >= 2:
            return "likely ad"

    return None


class ContentAnalyzer:
    """Analyzes content for AI relevance and quality"""
    
    def __init__(self, keywords_tier1=None, keywords_tier2=None):
        # AI discussion keywords (prioritizing actual discussions). tier1/tier2
        # may be overridden by the profile config; tier3 keeps the built-in
        # strategy terms. Defaults below are used when no override is given.
        self.ai_discussion_keywords = {
            'tier1': {  # Strong indicators of AI discussion
                'llm', 'large language model', 'gpt-4', 'gpt-3', 'gpt-5', 'gpt-4o',
                'gpt', 'claude', 'chatgpt',
                'prompt engineering', 'prompt', 'rag', 'retrieval augmented',
                'fine-tuning', 'fine-tune', 'fine tuning', 'rlhf',
                'hallucination', 'token', 'context window', 'embeddings',
                'vector database', 'langchain', 'llamaindex',
                'agents', 'ai agents', 'ai agent', 'agentic',
                'function calling', 'chain of thought', 'in-context learning',
                'openai', 'anthropic', 'gemini', 'llama', 'mistral', 'copilot'
            },
            'tier2': {  # General AI tech discussion
                'transformer', 'attention mechanism', 'neural network',
                'deep learning', 'machine learning', 'model training',
                'inference', 'parameters', 'weights', 'gradient',
                'backpropagation', 'optimization', 'loss function',
                'artificial intelligence', 'a.i.', 'generative ai', 'genai',
                'foundation model', 'multimodal', 'vision model',
                'automation', 'reinforcement learning', 'training data',
                'data labeling', 'data annotation', 'data science', 'dataset',
                'chatbot', 'mlops'
            },
            'tier3': {  # AI applications and strategy
                'ai adoption', 'ai strategy', 'ai implementation',
                'ai governance', 'responsible ai', 'ai ethics',
                'ai bias', 'ai safety', 'alignment', 'interpretability',
                'ai transformation', 'ai tools', 'ai platform',
                'ai revolution', 'ai impact', 'ai future'
            }
        }

        # Override tier1/tier2 from the profile config when provided.
        if keywords_tier1:
            self.ai_discussion_keywords['tier1'] = {k.lower() for k in keywords_tier1}
        if keywords_tier2:
            self.ai_discussion_keywords['tier2'] = {k.lower() for k in keywords_tier2}

        # Career/personal achievement indicators to AVOID
        self.career_indicators = {
            # Personal achievements
            'thrilled to share', 'excited to announce', 'proud to',
            'happy to share', 'pleased to announce', 'honored to',
            'selected for', 'joined', 'starting my', 'new role',
            'new position', 'transitioning to', 'moving to',
            
            # Internship/job related
            'internship', 'intern', 'placement', 'offer',
            'hired', 'recruitment', 'joining as', 'started at',
            'first day', 'last day', 'farewell', 'goodbye',
            
            # Certifications/courses
            'completed course', 'certification', 'certificate',
            'passed exam', 'graduated', 'degree', 'diploma',
            
            # Job seeking
            'open to work', '#opentowork', 'looking for',
            'seeking opportunities', 'available for'
        }
        
        # Discussion indicators (what we WANT)
        self.discussion_indicators = {
            'what do you think', 'thoughts on', 'curious about',
            'wondering if', 'anyone else', 'have you tried',
            'in my experience', 'lessons learned', 'key takeaway',
            'interesting observation', 'noticed that', 'realized that',
            'challenge with', 'solution to', 'approach for',
            'debate', 'perspective', 'viewpoint', 'opinion',
            'agree or disagree', 'controversial', 'unpopular opinion'
        }
        
        # Quality thought leadership indicators
        self.quality_indicators = {
            'deep dive', 'analysis', 'research shows', 'data suggests',
            'case study', 'real-world example', 'practical application',
            'lessons from', 'insights from', 'patterns in',
            'trend analysis', 'future of', 'implications for'
        }
        
        # Spam/promotional indicators
        self.spam_indicators = {
            'link in comments', 'dm me', 'download now', 'sign up',
            'register today', 'limited offer', 'exclusive access',
            'webinar', 'masterclass', 'free course', 'ebook',
            'newsletter', 'subscribe', 'follow for more'
        }
    
    def classify_post_type(self, text: str) -> str:
        """Classify the type of post"""
        text_lower = text.lower()
        
        # Check for career/personal posts first (to filter out)
        career_count = sum(1 for indicator in self.career_indicators 
                          if indicator in text_lower)
        if career_count >= 2:
            return "career"
        
        # Check for spam
        spam_count = sum(1 for indicator in self.spam_indicators 
                        if indicator in text_lower)
        if spam_count >= 2:
            return "spam"
        
        # Check for discussion posts (what we want)
        discussion_count = sum(1 for indicator in self.discussion_indicators 
                             if indicator in text_lower)
        if discussion_count >= 1:
            return "discussion"
        
        # Check for thought leadership
        quality_count = sum(1 for indicator in self.quality_indicators 
                          if indicator in text_lower)
        if quality_count >= 1:
            return "thought_leadership"
        
        # Check if it's an announcement
        if any(phrase in text_lower for phrase in [
            'announcing', 'launched', 'released', 'introducing', 'new feature'
        ]):
            return "announcement"
        
        return "other"
    
    def analyze(self, text: str) -> Tuple[bool, int, List[str], PostQuality, str]:
        """Analyze text for AI relevance and quality"""
        if not text or len(text) < 30:  # Require a minimum amount of text
            return False, 0, [], PostQuality.SKIP, "other"
        
        text_lower = text.lower()
        
        # Classify post type
        post_type = self.classify_post_type(text)
        
        # Skip career and spam posts
        if post_type in ["career", "spam"]:
            return False, 0, [], PostQuality.SKIP, post_type
        
        # Calculate AI relevance
        keywords_found = []
        score = 0
        
        # Check AI discussion keywords
        for keyword in self.ai_discussion_keywords['tier1']:
            if keyword in text_lower:
                keywords_found.append(keyword)
                score += 20
        
        for keyword in self.ai_discussion_keywords['tier2']:
            if keyword in text_lower:
                keywords_found.append(keyword)
                score += 10
        
        for keyword in self.ai_discussion_keywords['tier3']:
            if keyword in text_lower:
                keywords_found.append(keyword)
                score += 5
        
        # Boost score for discussion posts
        if post_type == "discussion":
            score += 15
        elif post_type == "thought_leadership":
            score += 10
        
        # Penalize announcements unless highly technical
        if post_type == "announcement" and len(keywords_found) < 3:
            score -= 10
        
        # Determine if AI-related: any keyword match counts (the tier weighting
        # in `score` already separates strong from weak signals). The previous
        # rule (tier1, or >=2 tier2) silently dropped single-keyword AI posts.
        is_ai = len(keywords_found) > 0
        
        # Determine quality level
        if not is_ai:
            quality = PostQuality.SKIP
        elif score >= 35 and post_type in ["discussion", "thought_leadership"]:
            quality = PostQuality.HIGH
        elif score >= 20 and post_type in ["discussion", "thought_leadership", "other"]:
            quality = PostQuality.MEDIUM
        elif score >= 15:
            quality = PostQuality.LOW
        else:
            quality = PostQuality.SKIP
        
        return is_ai, score, keywords_found[:5], quality, post_type


class LinkedInScraper:
    """Handles all LinkedIn scraping operations"""
    
    # Updated selectors based on debug output
    POST_SELECTORS = [
        # Current LinkedIn DOM (verified via feed_dump.html, June 28 2026): the
        # data-view-name hooks were dropped and classes are now opaque hashes.
        # Each feed post is a div[role='listitem'] inside div[data-testid='mainFeed'];
        # scope to mainFeed so nav/side-rail listitems are excluded.
        "div[data-testid='mainFeed'] div[role='listitem']",
        "div[role='listitem']",
        # Fallbacks (older DOM)
        "div[data-view-name='feed-full-update']",
        "div[data-id*='urn:li:activity']",
        "div[componentkey*='urn:li:activity']",
        "article[data-id*='feed-update']",
        "div.feed-shared-update-v2",
        "div.occludable-update"
    ]

    # Scroll container holding the feed (verified June 2026); old class kept as fallback.
    SCROLL_CONTAINER_SELECTORS = [
        "div[data-testid='mainFeed']",
        "div.scaffold-finite-scroll__content",
    ]
    
    TEXT_SELECTORS = [
        # Current LinkedIn DOM (verified via feed_dump.html, June 2026)
        "span[data-testid='expandable-text-box']",
        "p[data-view-name='feed-commentary']",
        # Fallbacks (older DOM)
        "div[data-view-name='feed-commentary'] span[dir='ltr']",
        "div[data-view-name='feed-commentary'] span.break-words",
        "div.feed-shared-text span[dir='ltr']"
    ]

    AUTHOR_SELECTORS = [
        # Current LinkedIn DOM (verified via feed_dump.html, June 28 2026): classes
        # are opaque hashes and the actor name renders inside the profile/company
        # actor link (the duplicate avatar link with the same href has empty text).
        # The most reliable name hook is the control-menu aria-label
        # ("...post by NAME"), tried first in _extract_author_improved via
        # _author_from_menu; these link selectors back it up / supply the URL.
        "a[href*='/in/']",
        "a[href*='/company/']",
        # Fallbacks (older DOM)
        "a[data-view-name='feed-actor']",
        "div[data-view-name='feed-actor-sub-description'] a[href*='/in/']",
        "a[data-view-name='feed-actor-image'] + div a[href*='/in/']",
        "p.feed-shared-actor__name",
        "span.feed-shared-actor__name"
    ]

    # Post "..." overflow menu and the "Copy link to post" item inside it (used by
    # extract_url_via_clipboard, _author_from_menu, and selector_health_check via
    # these constants). June 28 2026: the button is identified by an aria-label
    # that starts "Open control menu for post by <author>"; old hook kept as a
    # comma fallback. The dropdown items still use role=menuitem.
    CONTROL_MENU_SELECTOR = (
        "button[aria-label^='Open control menu'], "
        "button[data-view-name='feed-control-menu']"
    )
    CONTROL_MENU_AUTHOR_PREFIX = "Open control menu for post by "
    MENU_ITEM_SELECTORS = "[role='menuitem'], div[role='menu'] *, .artdeco-dropdown__content *"
    COPY_LINK_TEXT = "copy link"
    
    def __init__(self, driver, logger):
        self.driver = driver
        self.logger = logger
        self.wait = WebDriverWait(driver, 20)
    
    def find_posts(self) -> List:
        """Find all posts on current page"""
        for selector in self.POST_SELECTORS:
            posts = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if posts:
                self.logger.info(f"Found {len(posts)} posts with selector: {selector}")
                return posts
        
        # Fallback
        self.logger.warning("Using fallback post search")
        return self.driver.find_elements(
            By.XPATH,
            "//*[contains(@data-id, 'activity') or contains(@componentkey, 'activity')]"
        )
    
    def extract_post_data(self, element) -> Optional[LinkedInPost]:
        """Extract data from a post element"""
        post = LinkedInPost()
        
        # Extract URN and URL
        urn = self._extract_urn(element)
        if urn:
            post.activity_urn = urn
            post.url = f"https://www.linkedin.com/feed/update/{urn}/"
        
        # Extract author info (improved)
        post.author_name, post.author_profile_url = self._extract_author_improved(element)
        
        # Extract text content
        post.text = self._extract_text(element)
        if not post.text or len(post.text) < 30:
            return None
        
        # Extract engagement metrics (improved)
        post.likes, post.comments, post.reposts = self._extract_engagement_improved(element)
        
        # Check if promoted
        post.is_promoted = self._is_promoted(element)
        
        return post
    
    # LinkedIn posts are keyed by one of these URN types; any of them forms a
    # valid /feed/update/<urn>/ URL.
    #
    # The SCRAPER's accepted set, and deliberately only these three. The
    # grammar lives in post_urn (shared with the poster, which also accepts
    # groupPost); widening this set changes which URNs the scraper records,
    # and that is a separate decision (Dispatch 15.3 kept it unchanged).
    URN_TYPES = ("activity", "ugcPost", "share")

    @classmethod
    def urn_in_text(cls, text: str) -> Optional[str]:
        """First ``urn:li:<type>:<id>`` of an accepted type in DOM text."""
        found = post_urn.find_post_urn(text, cls.URN_TYPES,
                                       forms=(post_urn.URN_FORM,))
        return found.urn if found else None

    @classmethod
    def urn_from_copied_link(cls, url: str) -> Optional[str]:
        """The URN a copied post link carries in its slug (``-<type>-<id>``).

        Slug form only, as it always was: a /feed/update/urn:li:... link
        yields nothing here, and ``activity_urn`` is then left as is.
        """
        found = post_urn.find_post_urn(url, cls.URN_TYPES,
                                       forms=(post_urn.SLUG_FORM,))
        return found.urn if found else None

    def _extract_urn(self, element) -> Optional[str]:
        """Extract a post URN (activity, ugcPost, or share) from the element.

        NOTE (verified June 2026 via feed_dump.py): the current feed DOM does NOT
        expose a post permalink/URN for native posts — the timestamp link points
        to /feed/, the post container's componentkey is an opaque hash, there is
        no entityUrn data-island, and an 8-level attribute walk finds no URN. The
        only URNs that leak are the *parent post* of a rendered comment
        (`urn:li:comment:(urn:li:ugcPost:X,…)` → step 4 below picks up `ugcPost:X`),
        so only reshares / posts with a visible comment yield a URL. Reliable
        per-post permalinks require the "..." menu "Copy link"/"Embed" flow or the
        voyager API — see BLOCKED.md.
        """
        # 1. componentkey attribute
        try:
            componentkey = element.get_attribute('componentkey')
            if componentkey:
                urn = self.urn_in_text(componentkey)
                if urn:
                    return urn
        except Exception:
            self.logger.debug("Failed to read componentkey for URN", exc_info=True)

        # 2. standard data attributes
        for attr in ['data-urn', 'data-id']:
            try:
                value = element.get_attribute(attr)
                if value:
                    urn = self.urn_in_text(value)
                    if urn:
                        return urn
            except Exception:
                continue

        # 3. child anchors linking to /feed/update/<urn>/ — most reliable on the
        #    current DOM, where the post container carries no URN attribute.
        try:
            for anchor in element.find_elements(By.CSS_SELECTOR, "a[href*='/feed/update/']"):
                href = anchor.get_attribute('href') or ''
                urn = self.urn_in_text(href)
                if urn:
                    return urn
        except Exception:
            self.logger.debug("Failed to scan anchors for URN", exc_info=True)

        # 4. fall back to scanning the element's inner HTML
        try:
            html = element.get_attribute('innerHTML') or ''
            urn = self.urn_in_text(html)
            if urn:
                return urn
        except Exception:
            self.logger.debug("Failed to extract URN from innerHTML", exc_info=True)

        return None

    # JavaScript that intercepts the page's clipboard writes so "Copy link to
    # post" lands in a JS variable instead of the OS clipboard. Re-injected per
    # post; resetting window.__interceptedClipboard = null makes staleness
    # impossible (a failed copy reads back null, never a previous post's URL).
    CLIPBOARD_INTERCEPT_JS = """
        window.__interceptedClipboard = null;
        try {
            navigator.clipboard.writeText = function(text) {
                window.__interceptedClipboard = text;
                return Promise.resolve();
            };
        } catch (e) {}
        document.execCommand = (function(original) {
            return function(cmd) {
                if (cmd === 'copy') {
                    var sel = window.getSelection().toString();
                    if (sel) window.__interceptedClipboard = sel;
                }
                return original.apply(document, arguments);
            };
        })(document.execCommand);
    """

    @staticmethod
    def _get_clipboard() -> str:
        """Read the Windows clipboard via PowerShell (fallback only)."""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=10,
        )
        return (result.stdout or "").strip()

    def _dismiss(self):
        """Press Escape to close any open dropdown/toast."""
        try:
            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
            hb.human_sleep(0.2, 0.5)
        except Exception:
            self.logger.debug("ESC dismiss failed", exc_info=True)

    #: Hosts LinkedIn hands out instead of a permalink when you copy a post
    #: link. They are NOT optional to handle: as of Sept 2026 this is the only
    #: thing "Copy link to post" produces.
    SHORTLINK_HOSTS = ("lnkd.in",)

    #: A plain desktop UA. The shortlink 301 is served without auth.
    SHORTLINK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36")

    @staticmethod
    def _looks_like_permalink(url: str) -> bool:
        return ("linkedin.com/feed/update/" in url
                or "linkedin.com/posts/" in url)

    @classmethod
    def _resolve_shortlink(cls, url: str, timeout: int = 15) -> Optional[str]:
        """Follow a lnkd.in shortlink to the real permalink, or None.

        One HEAD request; lnkd.in answers with a 301 and needs no cookies.
        """
        try:
            resp = requests.head(url, allow_redirects=True, timeout=timeout,
                                 headers={"User-Agent": cls.SHORTLINK_UA})
        except Exception:
            return None
        final = (resp.url or "").strip()
        return final or None

    def _validate_post_url(self, clip: Optional[str]) -> Optional[str]:
        """Return a clean post URL if clip looks like a LinkedIn permalink, else None.

        "Copy link to post" no longer yields a permalink. It yields a
        ``https://lnkd.in/p/<code>`` shortlink, which this used to reject
        outright - so every successfully copied link was thrown away and the
        post stored with no URL, then trashed as ``no_url``. On the dev feed
        that discarded 20 of 21 quality posts across two scrapes.

        A shortlink is therefore resolved to its target, which also restores
        the activity URN the caller parses out of it. If the resolution itself
        fails (offline, lnkd.in down), the shortlink is kept rather than
        dropped: it still navigates to the post, which is worth more than
        losing it.
        """
        if not clip:
            return None
        clip = clip.strip()

        if any(host in clip for host in self.SHORTLINK_HOSTS):
            resolved = self._resolve_shortlink(clip)
            if resolved and self._looks_like_permalink(resolved):
                return resolved.split("?")[0]
            self.logger.warning(
                "Could not resolve shortlink %s (got %r); keeping the "
                "shortlink - it still opens the post, but carries no URN",
                clip, (resolved or "")[:80])
            return clip.split("?")[0]

        if not self._looks_like_permalink(clip):
            self.logger.debug(f"Clipboard value is not a post URL: {clip[:80]!r}")
            return None
        return clip.split("?")[0]  # strip tracking query params

    def extract_url_via_clipboard(self, element) -> Optional[str]:
        """Resolve a post's permalink via "..." menu -> "Copy link to post".

        The current feed DOM exposes no post URN. Primary approach: inject a
        JavaScript clipboard intercept (override navigator.clipboard.writeText and
        document.execCommand('copy')) so the copied permalink is captured into
        window.__interceptedClipboard WITHOUT ever touching the OS clipboard.
        Fallback (with a warning): read the OS clipboard via PowerShell if the JS
        intercept caught nothing (e.g. LinkedIn used an uncaptured copy path).
        Returns a clean post URL or None. Best-effort: failures log and return None.
        """
        try:
            menu_btn = element.find_element(By.CSS_SELECTOR, self.CONTROL_MENU_SELECTOR)
        except Exception:
            self.logger.debug("No control-menu button on post", exc_info=True)
            return None

        try:
            # Install the in-page clipboard intercept BEFORE triggering the copy.
            self.driver.execute_script(self.CLIPBOARD_INTERCEPT_JS)

            # Open the "..." overflow menu with a natural mouse approach + click
            # (the most bot-like action if teleport-clicked), then pause for the
            # dropdown to render.
            hb.human_click(self.driver, menu_btn)
            hb.human_sleep(0.9, 1.5)

            # Find the "Copy link to post" item by visible text.
            copy_item = None
            for it in self.driver.find_elements(By.CSS_SELECTOR, self.MENU_ITEM_SELECTORS):
                try:
                    if self.COPY_LINK_TEXT in (it.text or "").strip().lower():
                        copy_item = it
                        break
                except Exception:
                    continue

            if copy_item is None:
                self.logger.debug("No 'Copy link to post' menu item found")
                self._dismiss()
                return None

            hb.human_click(self.driver, copy_item)
            hb.human_sleep(0.8, 1.4)  # let the in-page copy handler run

            # Primary: read the intercepted value straight from the page.
            clip = self.driver.execute_script("return window.__interceptedClipboard;")

            # Fallback: OS clipboard only if the JS intercept caught nothing.
            if not clip:
                self.logger.warning(
                    "JS clipboard intercept returned null; falling back to OS "
                    "clipboard (PowerShell Get-Clipboard)"
                )
                clip = self._get_clipboard()

            self._dismiss()  # close the menu / dismiss the "copied" toast
            return self._validate_post_url(clip)

        except Exception as e:
            self.logger.debug(f"Clipboard URL extraction failed: {e}", exc_info=True)
            self._dismiss()
            return None

    def _author_from_menu(self, element) -> Tuple[Optional[str], Optional[str]]:
        """Read the post author from the overflow button's aria-label.

        The current feed DOM (June 28 2026) labels every post's "..." button
        "Open control menu for post by <author>", which is the single most
        reliable author hook (present on both person and company posts, where the
        actor link text is empty). Returns (name, profile_url) or (None, None);
        the URL is matched from the post's profile/company links when available.
        """
        try:
            btn = element.find_element(By.CSS_SELECTOR, self.CONTROL_MENU_SELECTOR)
            label = (btn.get_attribute("aria-label") or "").strip()
        except Exception:
            return None, None
        if not label.startswith(self.CONTROL_MENU_AUTHOR_PREFIX):
            return None, None
        name = label[len(self.CONTROL_MENU_AUTHOR_PREFIX):].strip()
        if len(name) <= 2:
            return None, None
        # Best-effort URL: the actor link whose text matches the name, else the
        # first profile/company link in the post.
        url = None
        try:
            links = element.find_elements(By.CSS_SELECTOR, "a[href*='/in/'], a[href*='/company/']")
            for link in links:
                if (link.text or "").strip() and name.lower() in link.text.strip().lower():
                    url = link.get_attribute("href")
                    break
            if url is None and links:
                url = links[0].get_attribute("href")
        except Exception:
            self.logger.debug("author-from-menu URL lookup failed", exc_info=True)
        return name, url

    def _extract_author_improved(self, element) -> Tuple[str, Optional[str]]:
        """Improved author extraction - handles reposts and reactions correctly"""
        try:
            # Most reliable on the current DOM: the overflow button's aria-label
            # carries the author name. Try it first; fall through on a miss.
            menu_name, menu_url = self._author_from_menu(element)
            if menu_name:
                return menu_name, menu_url

            # Get the full text to analyze
            element_text = element.text if element else ''
            
            # Check if this is a reaction/repost/comment at the top
            reaction_indicators = [
                'reposted this', 'loves this', 'celebrates this', 
                'supports this', 'finds this insightful', 'finds this funny',
                'commented on this', 'liked this', 'shared this'
            ]
            
            # Find if there's a reaction and who did it
            reactor_name = None
            has_reaction = False
            
            # Check first few lines for reaction pattern
            lines = element_text.split('\n')[:5]  # Check first 5 lines
            for line in lines:
                for indicator in reaction_indicators:
                    if indicator in line.lower():
                        has_reaction = True
                        # Extract reactor name (everything before the indicator)
                        reactor_name = line.split(indicator)[0].strip()
                        break
                if has_reaction:
                    break
            
            if has_reaction and reactor_name:
                self.logger.debug(f"Found reaction from: {reactor_name}")
                
                # Now find the REAL author (not the reactor)
                # Strategy 1: Look through all the text lines after the reaction
                all_lines = element_text.split('\n')
                
                # Find where the reaction line ends
                reaction_line_index = -1
                for i, line in enumerate(all_lines):
                    if reactor_name in line and any(ind in line.lower() for ind in reaction_indicators):
                        reaction_line_index = i
                        break
                
                # Look for the author in lines after the reaction
                if reaction_line_index >= 0:
                    for i in range(reaction_line_index + 1, min(reaction_line_index + 10, len(all_lines))):
                        line = all_lines[i].strip()
                        
                        # Skip empty lines
                        if not line:
                            continue
                        
                        # Skip if it's the reactor name again
                        if reactor_name.lower() in line.lower():
                            continue
                        
                        # Skip common UI elements and labels
                        if any(skip in line.lower() for skip in [
                            'follow', 'connect', 'message', 'like', 'comment', 
                            'share', 'save', 'more', 'view', 'see', '•', '·'
                        ]):
                            continue
                        
                        # Skip time indicators (like "3h", "2d")
                        if len(line) <= 3 and any(c in line for c in ['h', 'd', 'w', 'm']):
                            continue
                        
                        # This could be the author name
                        if len(line) > 2 and len(line) < 50:  # Names aren't usually super long
                            # Check if the next line might be a title
                            if i + 1 < len(all_lines):
                                next_line = all_lines[i + 1].strip()
                                # If next line looks like a job title, current line is likely the name
                                job_indicators = [
                                    'scientist', 'engineer', 'developer', 'manager',
                                    'director', 'ceo', 'founder', 'coach', 'consultant',
                                    'analyst', 'designer', 'specialist', 'lead',
                                    'vp', 'president', 'head of', 'chief'
                                ]
                                if any(job in next_line.lower() for job in job_indicators):
                                    self.logger.debug(f"Found author after reaction: {line}")
                                    # Try to find the corresponding link
                                    profile_links = element.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
                                    for link in profile_links:
                                        link_text = link.text.strip()
                                        if link_text and line.lower() in link_text.lower():
                                            return line, link.get_attribute('href')
                                    return line, None
                            
                            # Even without a clear title, if it looks like a name, use it
                            # (Names often have 2-4 words, start with capitals)
                            words = line.split()
                            if 1 < len(words) <= 4 and words[0][0].isupper():
                                self.logger.debug(f"Found likely author name: {line}")
                                # Try to find corresponding link
                                profile_links = element.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
                                for link in profile_links:
                                    link_text = link.text.strip()
                                    if link_text and line.lower() in link_text.lower():
                                        return line, link.get_attribute('href')
                                return line, None
                
                # Strategy 2: Skip reactor in profile links
                profile_links = element.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
                for link in profile_links:
                    name = link.text.strip()
                    if not name:
                        try:
                            parent = link.find_element(By.XPATH, "..")
                            name = parent.text.strip().split('\n')[0]
                        except Exception:
                            continue
                    
                    # Skip if this is the reactor
                    if reactor_name.lower() in name.lower():
                        continue
                    
                    # Skip UI elements
                    if any(skip in name.lower() for skip in [
                        'follow', 'connect', 'message', 'like', 'comment', 'share'
                    ]):
                        continue
                    
                    # Found a valid name that's not the reactor
                    if name and len(name) > 2:
                        name = name.split('•')[0].strip()
                        self.logger.debug(f"Found author via links: {name}")
                        return name, link.get_attribute('href')
            
            # No reaction found - standard extraction
            else:
                # Try the original selectors
                for selector in self.AUTHOR_SELECTORS:
                    try:
                        author_elem = element.find_element(By.CSS_SELECTOR, selector)
                        name = author_elem.text.strip()
                        url = author_elem.get_attribute('href') if author_elem.tag_name == 'a' else None
                        
                        if name and len(name) > 2:
                            name = name.split('\n')[0].split('•')[0].strip()
                            return name, url
                    except Exception:
                        continue
                
                # Fallback to first profile link
                profile_links = element.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
                for link in profile_links[:3]:
                    name = link.text.strip()
                    if not name:
                        parent = link.find_element(By.XPATH, "..")
                        name = parent.text.strip().split('\n')[0]
                    
                    if name and len(name) > 2 and not any(
                        skip in name.lower() for skip in ['like', 'comment', 'share', 'follow']
                    ):
                        name = name.split('•')[0].strip()
                        return name, link.get_attribute('href')
                        
        except Exception as e:
            self.logger.debug(f"Author extraction error: {e}")
        
        return "Unknown", None
    
    def _extract_text(self, element) -> str:
        """Extract post text content"""
        # Click "see more" if present
        try:
            see_more = element.find_element(
                By.CSS_SELECTOR, 
                "button[data-testid='expandable-text-button'], button.see-more-less-toggle"
            )
            hb.human_click(self.driver, see_more)
            hb.human_sleep(0.3, 0.7)
        except Exception:
            self.logger.debug("No 'see more' button to expand", exc_info=True)
        
        # Try selectors in order (accept shorter clean text, not just >100).
        tried = []
        for selector in self.TEXT_SELECTORS:
            try:
                text_elem = element.find_element(By.CSS_SELECTOR, selector)
                text = text_elem.text.strip()
                tried.append(f"{selector}={len(text)}c")
                if text and len(text) >= 30:
                    self.logger.debug(f"[text] via {selector}: {len(text)} chars")
                    return text
            except Exception:
                tried.append(f"{selector}=miss")
                continue

        # Fallback 1: longest coherent span (older layouts).
        try:
            spans = element.find_elements(By.CSS_SELECTOR, "span[dir='ltr'], span.break-words")
            texts = [s.text.strip() for s in spans if s.text.strip()]
            if texts:
                longest = max(texts, key=len)
                if len(longest) >= 30:
                    self.logger.debug(f"[text] via span fallback: {len(longest)} chars")
                    return longest
        except Exception:
            self.logger.debug("Fallback span text extraction failed", exc_info=True)

        # Fallback 2 (last resort): the whole container's text blob. A rough blob
        # that scores > 0 beats no text. This catches reshares / articles / polls /
        # video posts whose body isn't in the primary selectors.
        try:
            blob = (element.text or "").strip()
            self.logger.debug(
                f"[text] selectors all failed (tried: {', '.join(tried) or 'none'}); "
                f"using element.text blob = {len(blob)} chars"
            )
            return blob
        except Exception:
            self.logger.debug("element.text blob extraction failed", exc_info=True)
            return ""
    
    def _extract_engagement_improved(self, element) -> Tuple[int, int, int]:
        """Improved engagement extraction"""
        likes = 0
        comments = 0
        reposts = 0
        
        try:
            # Get all text that might contain counts
            element_text = element.text.lower()
            
            # Extract likes/reactions
            reaction_patterns = [
                r'(\d+(?:,\d+)*(?:\.\d+)?[km]?)\s*reaction',
                r'(\d+(?:,\d+)*(?:\.\d+)?[km]?)\s*like'
            ]
            for pattern in reaction_patterns:
                match = re.search(pattern, element_text)
                if match:
                    likes = self._parse_count(match.group(1))
                    break
            
            # Extract comments
            comment_match = re.search(r'(\d+(?:,\d+)*(?:\.\d+)?[km]?)\s*comment', element_text)
            if comment_match:
                comments = self._parse_count(comment_match.group(1))
            
            # Extract reposts
            repost_match = re.search(r'(\d+(?:,\d+)*(?:\.\d+)?[km]?)\s*repost', element_text)
            if repost_match:
                reposts = self._parse_count(repost_match.group(1))
            
        except Exception as e:
            self.logger.debug(f"Engagement extraction error: {e}")
        
        return likes, comments, reposts
    
    def _parse_count(self, text: str) -> int:
        """Parse engagement count from text"""
        if not text:
            return 0
        
        text = text.lower().strip().replace(',', '')
        
        # Handle K and M suffixes
        if 'k' in text:
            return int(float(text.replace('k', '')) * 1000)
        elif 'm' in text:
            return int(float(text.replace('m', '')) * 1000000)
        
        try:
            return int(float(text))
        except Exception:
            return 0
    
    def _is_promoted(self, element) -> bool:
        """Check if post is promoted/sponsored"""
        try:
            text = element.text.lower()
            return 'promoted' in text or 'sponsored' in text
        except Exception:
            return False
    
    def scroll_feed(self, aggressive=False):
        """Scroll the feed to trigger lazy-loading of more posts.

        The current LinkedIn feed renders skeleton loaders until the *window* is
        scrolled, so window.scrollBy is the reliable trigger. A known inner
        scroll container (if present, older layouts) is nudged too as a belt-and-
        suspenders measure.
        """
        steps = 3 if aggressive else 1
        for _ in range(steps):
            # Window scroll — the reliable lazy-load trigger on the current feed.
            # human_scroll uses variable increments + micro-pauses + drift instead
            # of a uniform 1200px jump (uniform scrolls are a bot tell).
            hb.human_scroll(self.driver, direction="down")

            # Also nudge an inner scroll container if one matches (older DOM).
            # Jitter the nudge so it isn't a constant 1200px either.
            nudge = hb.jitter_int(1200, 0.2)
            for sel in self.SCROLL_CONTAINER_SELECTORS:
                try:
                    container = self.driver.find_element(By.CSS_SELECTOR, sel)
                    self.driver.execute_script(
                        "arguments[0].scrollTop += arguments[1];", container, nudge)
                    break
                except Exception:
                    continue

            hb.human_sleep(0.8, 1.4) if aggressive else hb.human_sleep(1.6, 2.6)

        # Wait for new content to load
        hb.human_sleep(0.8, 1.6)


class LinkedInAIPostFinder:
    """Main class for finding AI-related posts on LinkedIn"""
    
    def __init__(self, debug=False, profile_name=None):
        self.profile_name = profile_name
        self.profile = None  # Set during setup_driver

        self.debug = debug

        self.driver = None
        self.scraper = None
        self.processed_ids: Set[str] = set()
        self.posts: List[LinkedInPost] = []
        # Ads / job cards / recommendation cards filtered during the scan, kept
        # (instead of silently dropped) so they land in the lifecycle store as
        # TRASH with a reason. Each entry: {"post": <serializable dict>, "reason"}.
        self.trashed: List[dict] = []

        self.setup_logging()

        # Resolve profile name for data dirs (before driver setup)
        resolved_name = profile_name or pm.get_default_profile_name() or "default"

        # Load the per-profile config (created on first use). Keywords and quality
        # settings come from here; falls back to the default template if absent.
        self.config = pm.get_profile_config(resolved_name)
        pf = self.config.get("post_finder", {})
        self.analyzer = ContentAnalyzer(
            keywords_tier1=pf.get("keywords_tier1"),
            keywords_tier2=pf.get("keywords_tier2"),
        )
        self.cfg_min_quality = pf.get("min_quality_score")
        self.cfg_max_posts = pf.get("max_posts_per_scan")
        self.cfg_post_types = pf.get("post_types_to_engage") or []
        # Company names the user always wants skipped (e.g. ["ClickUp", "HubSpot"]).
        self.cfg_ad_indicators = pf.get("ad_indicators") or []

        # Apply the tunable human-behavior timing ranges (typing speed, reading
        # time, scroll/break cadence) from the profile config's "behavior"
        # section so anti-detection pacing is configurable without code changes.
        hb.configure_behavior(self.config.get("behavior"))

        self.output_dir = pm.get_timeline_dir(resolved_name)
        self.logger.info(f"Output directory: {self.output_dir}")
        self.logger.info(
            f"Config: {len(self.analyzer.ai_discussion_keywords['tier1'])} tier1 + "
            f"{len(self.analyzer.ai_discussion_keywords['tier2'])} tier2 keywords; "
            f"min_quality={self.cfg_min_quality}, max_posts={self.cfg_max_posts}"
        )
    
    def setup_logging(self):
        """Configure logging"""
        log_level = logging.DEBUG if self.debug else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
    
    def setup_driver(self):
        """Initialize Chrome driver with persistent session via profile manager"""
        self.logger.info("Setting up browser with persistent session...")
        
        self.driver, self.profile = pm.create_driver(self.profile_name)
        
        self.scraper = LinkedInScraper(self.driver, self.logger)
        
        self.logger.info("Browser ready")
    
    def login(self) -> bool:
        """Login to LinkedIn using profile manager (checks persistent session first)"""
        return pm.login(self.driver, self.profile)
    
    # The REAL fix - replace the find_posts method around line 440

    def find_posts(self, max_posts=50, min_quality=10):
        """Find AI discussion posts on the already-loaded feed.

        Assumes the caller (``run``) has already navigated to /feed/ exactly once.
        Navigating again degrades the feed, so we scroll the current page instead
        of reloading it.
        """
        self.logger.info(f"Scanning for AI discussion posts (max: {max_posts}, min quality: {min_quality})")

        # Scroll FIRST: the current feed renders skeleton loaders until scrolled,
        # so posts only appear after a few window scrolls. (No navigation here —
        # run() already loaded /feed/.)
        for _ in range(5):
            self.scraper.scroll_feed()

        # Now wait for at least one post to be present before scanning, so we
        # don't scan an empty / still-loading page and report zero.
        # Wait for any current post selector (top of POST_SELECTORS), not a
        # single hardcoded hook, so this guard tracks DOM rotations automatically.
        post_wait_selector = ", ".join(self.scraper.POST_SELECTORS[:2])
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, post_wait_selector))
            )
        except TimeoutException:
            self.logger.error(
                "Feed did not load any posts within 15s after scrolling (no "
                f"'{post_wait_selector}' present). The session may be throttled, "
                "the feed may be empty, or the DOM may have changed again. Aborting."
            )
            capture_failure(self.driver, "feed_no_posts", self.profile_name)
            raise RuntimeError("LinkedIn feed failed to load posts")

        quality_posts = 0
        posts_scanned = 0  # This should count UNIQUE posts only
        unique_posts_processed = 0  # Track actual processing
        last_element_count = 0
        stale_count = 0
        scroll_iterations = 0
        max_scrolls = 30  # sane cap on lazy-load scrolling
        self._scan_post_types = {}  # diagnostics: post_type -> count
        self._scan_qualities = {}   # diagnostics: quality -> count
        self._scan_ads = 0          # diagnostics: ads/job cards filtered out

        # Non-uniform session shape: take a longer break after a re-rolled number
        # of posts (e.g. 3-7) so the work-then-pause rhythm is never identical.
        posts_since_break = 0
        break_threshold = hb.random_break_threshold()

        # A human reads the feed for a beat before starting to scan/scroll.
        hb.simulate_reading(self.driver)

        while (unique_posts_processed < max_posts
               and quality_posts < min_quality
               and scroll_iterations < max_scrolls):
            # Find posts
            elements = self.scraper.find_posts()
            
            if not elements:
                self.logger.warning("No posts found")
                break
            
            # Check if we got new elements
            current_element_count = len(elements)
            if current_element_count == last_element_count:
                stale_count += 1
                if stale_count >= 5:
                    self.logger.info("No new posts loading after repeated scrolls")
                    break
            else:
                stale_count = 0
                last_element_count = current_element_count
            
            # Process elements we haven't seen yet
            for i in range(posts_scanned, current_element_count):
                if unique_posts_processed >= max_posts:
                    break
                    
                element = elements[i]
                posts_scanned = i + 1  # Track position in elements array

                # Scroll the post into view with human-like smoothness (variable,
                # not a uniform jump) and a small chance of mouse drift.
                try:
                    hb.scroll_to_element(self.driver, element)
                except Exception:
                    continue

                # Extract data
                post = self.scraper.extract_post_data(element)
                if not post:
                    continue

                # Pause to "read" the post before moving on, scaled to its length
                # (longer posts take longer to read). This is the per-post reading
                # simulation a real person does between scrolling on.
                hb.simulate_reading_for_text(self.driver, post.text)
                
                # Check duplicate BEFORE incrementing unique counter
                post_id = post.get_identifier()
                if post_id in self.processed_ids:
                    continue  # Skip duplicate, DON'T increment counters
                
                # Now we have a truly unique post
                self.processed_ids.add(post_id)
                unique_posts_processed += 1

                # Ad / sponsored / job-card filter — runs BEFORE quality scoring
                # so ads never reach the analyzer or a downstream OpenAI eval.
                ad_reason = classify_ad(
                    post.author_name, post.text, post.author_profile_url,
                    post.is_promoted, self.cfg_ad_indicators,
                )
                if ad_reason:
                    self._scan_ads += 1
                    # Keep it (as TRASH with a reason) instead of dropping it, so
                    # the Trash view can show what was filtered and why.
                    self.trashed.append({
                        "post": {
                            "url": post.url or "",
                            "author_name": post.author_name,
                            "text": post.text,
                            "post_type": "",
                            "relevance_score": 0,
                        },
                        "reason": post_store.ad_reason_to_trash_reason(ad_reason),
                    })
                    self.logger.info(
                        f"  [scan] Skipped ({ad_reason}): {post.author_name or 'unknown'}"
                    )
                    continue

                # Analyze content
                is_ai, score, keywords, quality, post_type = self.analyzer.analyze(post.text)
                
                post.relevance_score = score
                post.keywords_matched = keywords
                post.quality = quality
                post.post_type = post_type
                
                # Diagnostics: record where posts land so we can see losses.
                self._scan_post_types[post_type] = self._scan_post_types.get(post_type, 0) + 1
                self._scan_qualities[quality.value] = self._scan_qualities.get(quality.value, 0) + 1
                self.logger.info(
                    f"  [scan] {post.author_name[:22]:22s} | text={len(post.text):4d}c | "
                    f"type={post_type:16s} | q={quality.value:6s} | score={score:3d} | "
                    f"kw={keywords[:4]}"
                )

                # Engage with any AI-relevant HIGH/MEDIUM post. post_types_to_engage
                # from the config can restrict by type, but is applied permissively:
                # when the list includes "other" (the catch-all most posts fall
                # into, as in the default/demo configs) no type restriction applies,
                # preserving yield. A user can tighten by removing "other".
                type_allowed = (
                    not self.cfg_post_types
                    or "other" in self.cfg_post_types
                    or post_type in self.cfg_post_types
                )
                if quality in [PostQuality.HIGH, PostQuality.MEDIUM] and type_allowed:
                    post.should_engage = True
                    post.engagement_reason = f"{quality.value} quality {post_type} (score {score})"
                    quality_posts += 1
                else:
                    post.should_engage = False
                    post.engagement_reason = f"Type: {post_type}, Quality: {quality.value}"

                # Resolve the permalink ONLY for quality posts we intend to engage
                # with (keeps the click count low). The feed DOM no longer exposes
                # post URNs, so we copy the link via the "..." menu and read it
                # from the OS clipboard.
                if post.should_engage and not post.url:
                    clip_url = self.scraper.extract_url_via_clipboard(element)
                    if clip_url:
                        post.url = clip_url
                        urn = LinkedInScraper.urn_from_copied_link(clip_url)
                        if urn:
                            post.activity_urn = urn
                        self.logger.info(f"  Resolved URL: {post.url}")
                    else:
                        self.logger.warning(
                            f"  Could not resolve URL for quality post by {post.author_name} "
                            f"(url left null; generator will skip it)"
                        )
                        # The overflow-menu / copy-link click flow failed — capture
                        # the page state so the shadow/menu breakage can be seen.
                        capture_failure(self.driver, "url_extraction_failed", self.profile_name)
                    # Randomized delay between click-throughs (anti-detection).
                    hb.human_sleep(1.0, 2.2)

                self.posts.append(post)

                # Log progress
                status = '✓' if post.should_engage else '✗'
                self.logger.info(
                    f"Post {unique_posts_processed} (unique): {post.author_name} | "
                    f"Type: {post_type} | Quality: {quality.value} | "
                    f"Score: {score} | {status}"
                )
                
                if self.debug:
                    self.logger.debug(f"Text preview: {post.text[:150]}...")
                    self.logger.debug(f"Keywords: {', '.join(keywords[:3])}")

                # ── Non-uniform pacing between posts ──────────────────────────
                # Wide, never-constant pause: act on some posts quickly, linger on
                # others. A small drift keeps the cursor alive between posts.
                if random.random() < 0.4:
                    hb.random_mouse_drift(self.driver)
                hb.human_sleep(0.6, 3.5)

                # Occasionally scroll back up a little, like re-reading something,
                # before continuing down the feed.
                if random.random() < 0.12:
                    hb.human_scroll(self.driver, direction="up",
                                    pixels=random.randint(120, 300))
                    hb.human_sleep(0.8, 2.0)

                # Longer break after a re-rolled number of posts (session shape).
                posts_since_break += 1
                if posts_since_break >= break_threshold:
                    self.logger.info(
                        f"Taking a natural break after {posts_since_break} posts...")
                    hb.take_break(self.driver)
                    posts_since_break = 0
                    break_threshold = hb.random_break_threshold()

            self.logger.info(
                f"Progress: {quality_posts}/{min_quality} quality posts | "
                f"{unique_posts_processed} unique posts scanned | "
                f"{len(elements)} total elements visible"
            )
            
            # Scroll for more (scroll_feed pauses ~2s to let LinkedIn lazy-load)
            self.scraper.scroll_feed(aggressive=(stale_count > 0))
            scroll_iterations += 1
            if unique_posts_processed < max_posts and scroll_iterations >= max_scrolls:
                self.logger.info(f"Reached scroll cap ({max_scrolls})")

        self.logger.info(
            f"Scan complete: {unique_posts_processed} unique posts scanned, "
            f"{quality_posts} quality discussions found "
            f"(after {scroll_iterations} scroll iterations)"
        )
        # Diagnostics: where did posts land?
        self.logger.info(f"  post_type distribution: {dict(sorted(self._scan_post_types.items()))}")
        self.logger.info(f"  quality distribution:   {dict(sorted(self._scan_qualities.items()))}")
        self.logger.info(f"  ads/job cards filtered: {self._scan_ads}")

    def save_results(self) -> str:
        """Save results to file"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Filter quality posts
        quality_posts = [p for p in self.posts if p.should_engage]
        quality_posts.sort(key=lambda x: x.relevance_score, reverse=True)
        
        # Convert posts to dict with enum handling
        def post_to_dict(post):
            """Serialize a scored post dataclass to a JSON-ready dict."""
            post_dict = asdict(post)
            # Convert enum to string
            post_dict['quality'] = post.quality.value
            return post_dict
        
        # Prepare output
        output = {
            'scan_date': datetime.now().isoformat(),
            'total_scanned': len(self.posts),
            'quality_found': len(quality_posts),
            'post_type_distribution': {},
            'quality_posts': [post_to_dict(p) for p in quality_posts],
            'all_posts': [post_to_dict(p) for p in self.posts]
        }
        
        # Add type distribution
        for post in self.posts:
            output['post_type_distribution'][post.post_type] = \
                output['post_type_distribution'].get(post.post_type, 0) + 1
        
        # Save
        output_file = os.path.join(self.output_dir, f'ai_posts_{timestamp}.json')
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        # Update the lifecycle store: quality posts → NEW, low-quality → TRASH
        # (low_quality), ads/job cards → TRASH (ad/job_card). The ai_posts file
        # above stays the generator's input; the store is the status record.
        self._update_post_store()

        self.print_summary(quality_posts, output_file)

        return output_file

    def _update_post_store(self):
        """Upsert this scan's posts into the per-profile lifecycle store.

        Best-effort: a store failure must never lose the scrape (the ai_posts
        file is already written), so this logs and continues on error.
        """
        try:
            resolved = self.profile_name or pm.get_default_profile_name() or "default"
            store = post_store.PostStore(resolved)
            for post in self.posts:
                rec = {
                    "url": post.url or "",
                    "author_name": post.author_name,
                    "text": post.text,
                    "post_type": post.post_type,
                    "relevance_score": post.relevance_score,
                }
                if not post.should_engage:
                    store.upsert_scraped(rec, status=post_store.TRASH,
                                         reason=post_store.REASON_LOW_QUALITY)
                elif (post.url or "").strip():
                    store.upsert_scraped(rec, status=post_store.NEW)
                else:
                    # Worth engaging, but URL extraction (incl. the clipboard
                    # fallback) produced nothing — not actionable, so trash it as
                    # no_url instead of letting it sit in NEW forever.
                    store.upsert_scraped(rec, status=post_store.TRASH,
                                         reason=post_store.REASON_NO_URL)
            for entry in self.trashed:
                store.upsert_scraped(entry["post"], status=post_store.TRASH,
                                     reason=entry["reason"])
            store.save()
            counts = store.counts()
            self.logger.info(
                f"Lifecycle store updated: NEW={counts['NEW']} "
                f"GENERATED={counts['GENERATED']} COMMENTED={counts['COMMENTED']} "
                f"TRASH={counts['TRASH']}"
            )
        except Exception:
            self.logger.warning("Could not update lifecycle store", exc_info=True)
    
    def print_summary(self, quality_posts: List[LinkedInPost], output_file: str):
        """Print results summary"""
        print(f"\n{'='*60}")
        print("AI DISCUSSION POSTS FOUND")
        print(f"{'='*60}")
        print(f"Total posts scanned: {len(self.posts)}")
        print(f"Quality discussions found: {len(quality_posts)}")
        
        # Show post type distribution
        type_counts = {}
        for post in self.posts:
            type_counts[post.post_type] = type_counts.get(post.post_type, 0) + 1
        
        print("\nPost types encountered:")
        for post_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"  {post_type}: {count}")
        
        if quality_posts:
            print(f"\nTop {min(5, len(quality_posts))} AI discussion posts:")
            for i, post in enumerate(quality_posts[:5], 1):
                print(f"\n{i}. {post.author_name}")
                print(f"   Type: {post.post_type} | Quality: {post.quality.value}")
                print(f"   Score: {post.relevance_score}")
                print(f"   Engagement: {post.likes} likes, {post.comments} comments, {post.reposts} reposts")
                print(f"   Keywords: {', '.join(post.keywords_matched[:3])}")
                print(f"   Preview: {post.text[:150]}...")
                if post.url:
                    print(f"   URL: {post.url}")
        
        print(f"\nResults saved to: {output_file}")
    
    def run(self, max_posts=50, min_quality=10):
        """Run the complete process"""
        try:
            self.setup_driver()

            # Navigate to the feed exactly ONCE and reuse this loaded page for
            # both the login check and the scan. A second navigation to /feed/
            # makes LinkedIn serve a degraded, post-less feed (see BLOCKED.md).
            self.logger.info("Loading LinkedIn feed...")
            self.driver.get('https://www.linkedin.com/feed/')
            # Let the feed settle, then a brief reading beat (not a flat 5s wait).
            hb.human_sleep(3.0, 5.0)
            hb.simulate_reading(self.driver)

            if not pm.is_logged_in_on_page(self.driver):
                raise pm.LoginRequiredError(
                    f"LinkedIn login failed for profile "
                    f"'{self.profile_name or 'default'}'. "
                    f"Run: python tools/login_check.py --profile "
                    f"{self.profile_name or 'default'}"
                )
            self.logger.info("✓ Logged in (persistent session)")

            # Scan the already-loaded feed (does NOT navigate again).
            self.find_posts(max_posts, min_quality)

            return self.save_results()

        except pm.LoginRequiredError:
            # Distinct from generic errors so the caller can exit with code 2.
            raise
        except Exception as e:
            self.logger.error(f"Error: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return None

        finally:
            if self.driver:
                self.driver.quit()
                self.logger.info("Browser closed")


def main():
    """Main entry point"""
    import argparse

    # Ensure emoji in console output (✅/❌/✓) don't crash under the Windows
    # cp1252 console when run directly (the dashboard sets PYTHONIOENCODING=utf-8
    # for its subprocesses, but a direct `uv run` does not).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='Find AI discussion posts on LinkedIn')
    # Default None so the profile config can supply the value when not given.
    parser.add_argument('--max-posts', type=int, default=None, help='Maximum posts to scan (default: profile config)')
    parser.add_argument('--min-quality', type=int, default=None, help='Minimum quality posts to find (default: profile config)')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--profile', type=str, default=None, help='LinkedIn profile name (uses default if omitted)')

    args = parser.parse_args()

    try:
        finder = LinkedInAIPostFinder(debug=args.debug, profile_name=args.profile)
        # Explicit CLI args win; otherwise use the profile config, then hard defaults.
        max_posts = args.max_posts if args.max_posts is not None else (finder.cfg_max_posts or 50)
        min_quality = args.min_quality if args.min_quality is not None else (finder.cfg_min_quality or 10)
        output_file = finder.run(max_posts, min_quality)

        if output_file:
            print(f"\n✅ Success! AI discussion posts saved to: {output_file}")
            sys.exit(pm.EXIT_OK)
        else:
            print("\n❌ Failed to complete scan")
            sys.exit(pm.EXIT_ERROR)

    except pm.LoginRequiredError as e:
        print(f"\n❌ {e}")
        sys.exit(pm.EXIT_LOGIN_REQUIRED)

    except ValueError as e:
        print(f"\n❌ Configuration Error: {e}")
        print("\nSetup a profile:")
        print("  python linkedin_profile_manager.py add <n>")
        print("\nOr ensure your .env file contains:")
        print("  LINKEDIN_USERNAME=your_email@example.com")
        print("  LINKEDIN_PASSWORD=your_password")
        sys.exit(pm.EXIT_ERROR)


if __name__ == "__main__":
    main()