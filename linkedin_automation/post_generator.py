"""
LinkedIn Post Generator & Queue Manager

Two modes:
  1. Thought leadership - generates posts about AI, LLMs, agentic workflows, automation
  2. Article reaction - give it a URL, it generates a post about that article

Posts are saved to a queue. Article-based posts get priority (oldest first).

Usage:
  python linkedin_post_generator.py generate                     # Generate a thought leadership post
  python linkedin_post_generator.py generate --count 5           # Generate 5 posts
  python linkedin_post_generator.py article "https://..."        # Generate post from article URL
  python linkedin_post_generator.py queue                        # Show the queue
  python linkedin_post_generator.py next                         # Show next post to publish
  python linkedin_post_generator.py post                         # Post the next one to LinkedIn
  python linkedin_post_generator.py post --id 3                  # Post a specific one
  python linkedin_post_generator.py remove --id 3                # Remove a post from queue
"""

import os
import json
import time
import random
import logging
import argparse
from datetime import datetime
from typing import List, Dict, Optional

# Route TLS through the OS trust store so the OpenAI client (httpx) works behind
# corporate TLS-intercepting proxies, which otherwise cause "Connection error"
# because httpx uses certifi, not the system CA store. No-op if truststore isn't
# installed or isn't needed. (Explicit guard, not relying on the pm import.)
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

try:
    from . import profile_manager as pm
    HAS_PM = True
except ImportError:
    HAS_PM = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ─── Post Types & Styles ────────────────────────────────────────────────────

POST_STYLES = [
    "hot_take",
    "question",
    "observation",
    "prediction",
    "story",
    "tip",
    "contrarian",
    "celebration",
]

STYLE_INSTRUCTIONS = {
    "hot_take": "Write a bold, confident take on the topic. Have a strong opinion. Don't hedge.",
    "question": "Ask a thought-provoking question that invites discussion. End with the question. Maybe share a brief thought first to set context.",
    "observation": "Share something you've noticed or a pattern you're seeing in the industry. Be specific and insightful.",
    "prediction": "Make a prediction about where things are heading. Be specific about timeframes and what you expect.",
    "story": "Share a brief anecdote or experience related to the topic. Keep it real and relatable. First person.",
    "tip": "Share a practical tip or insight that people can actually use. Be concrete and actionable.",
    "contrarian": "Challenge a common assumption or popular opinion. Respectfully disagree with conventional wisdom.",
    "celebration": "Highlight something exciting happening in the space. Show genuine enthusiasm without being over the top.",
}

TOPICS = [
    "how AI is changing everyday business workflows",
    "practical uses of LLMs that people overlook",
    "agentic AI workflows and why they matter",
    "automation replacing repetitive tasks so people can do real work",
    "AI literacy for non-technical professionals",
    "the gap between AI hype and practical AI implementation",
    "how small businesses can leverage AI right now",
    "the future of work with AI assistants",
    "building AI skills in your team",
    "real ROI from AI automation projects",
    "why most companies are underusing AI",
    "AI tools that actually save time vs ones that waste it",
    "the difference between knowing about AI and actually using it",
    "how AI agents are changing how we approach complex tasks",
    "making AI accessible to everyday workers, not just engineers",
    "what I'm seeing from companies that are actually succeeding with AI",
    "the biggest mistakes companies make when adopting AI",
    "why AI training matters more than AI tools",
    "generative AI for content, communication, and productivity",
    "the next wave of AI automation nobody is talking about",
]

# Persona used when the profile config has no post_generator.persona (fallback).
DEFAULT_PERSONA = (
    "a LinkedIn thought leader who specializes in practical AI adoption, LLMs, "
    "agentic workflows, and business automation. You help everyday professionals "
    "and businesses understand and use AI effectively. You're a practitioner who "
    "makes AI accessible and actionable, not an engineer posting about model "
    "architectures."
)

# Formatting / voice rules applied to every post regardless of persona or config.
POST_FORMAT_RULES = """Formatting and voice rules (always apply):
- Confident but not arrogant; practical over theoretical; speak from experience.
- Conversational, not corporate. Short paragraphs and line breaks for readability.
- 0-2 relevant emojis per post max. NEVER use hashtags.
- NEVER start with "I'm excited to announce" or similar LinkedIn cliches.
- Don't use "game-changer", "leverage", "synergy", or "deep dive".
- Vary post length dramatically: some 1-2 sentences (15-30 words), some a short
  paragraph (50-80 words), some a full post (150-250 words). Variety = authenticity.
- If you end with a question, make it specific and interesting (never a lazy
  "What do you think?"). No bullet points unless they genuinely help.
- Write like you're talking to a smart colleague, not presenting at a conference."""


# ─── Queue Manager ───────────────────────────────────────────────────────────

class PostQueue:
    """Manages the queue of generated posts."""

    def __init__(self, profile_name: str = None):
        if HAS_PM:
            resolved = profile_name or pm.get_default_profile_name() or "default"
            self.data_dir = pm.get_data_dir(resolved, "posts")
        else:
            self.data_dir = os.path.join("data", "posts")
            os.makedirs(self.data_dir, exist_ok=True)

        self.queue_file = os.path.join(self.data_dir, "post_queue.json")
        self.history_file = os.path.join(self.data_dir, "post_history.json")
        self.queue = self._load(self.queue_file, [])
        self.history = self._load(self.history_file, [])

    def _load(self, path, default):
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default

    def _save_queue(self):
        with open(self.queue_file, 'w', encoding='utf-8') as f:
            json.dump(self.queue, f, indent=2, ensure_ascii=False)

    def _save_history(self):
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False)

    def add(self, post: Dict):
        """Add a post to the queue."""
        post["id"] = max([p["id"] for p in self.queue], default=0) + 1
        post["queued_at"] = datetime.now().isoformat()
        post["status"] = "queued"
        self.queue.append(post)
        self._save_queue()
        return post["id"]

    def get_next(self) -> Optional[Dict]:
        """Get the next post to publish. Article posts (oldest first) have priority."""
        queued = [p for p in self.queue if p["status"] == "queued"]
        if not queued:
            return None

        # Priority: article posts first (oldest), then thought leadership (oldest)
        article_posts = [p for p in queued if p.get("type") == "article"]
        if article_posts:
            return min(article_posts, key=lambda p: p["queued_at"])

        return min(queued, key=lambda p: p["queued_at"])

    def get_by_id(self, post_id: int) -> Optional[Dict]:
        """Get a specific post by ID."""
        for p in self.queue:
            if p["id"] == post_id:
                return p
        return None

    def mark_posted(self, post_id: int):
        """Move a post from queue to history."""
        post = self.get_by_id(post_id)
        if post:
            post["status"] = "posted"
            post["posted_at"] = datetime.now().isoformat()
            self.history.append(post)
            self.queue = [p for p in self.queue if p["id"] != post_id]
            self._save_queue()
            self._save_history()

    def remove(self, post_id: int) -> bool:
        """Remove a post from the queue."""
        before = len(self.queue)
        self.queue = [p for p in self.queue if p["id"] != post_id]
        if len(self.queue) < before:
            self._save_queue()
            return True
        return False

    def list_queued(self) -> List[Dict]:
        """List all queued posts."""
        return [p for p in self.queue if p["status"] == "queued"]


# ─── Post Generator ──────────────────────────────────────────────────────────

class PostGenerator:
    """Generate LinkedIn posts using OpenAI."""

    def __init__(self, profile_name: str = None, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        self.model = model
        self.queue = PostQueue(profile_name)
        self.profile_name = profile_name

        # Per-profile post_generator config; falls back to hardcoded defaults.
        pg = {}
        if HAS_PM:
            try:
                pg = pm.get_profile_config(profile_name).get("post_generator", {}) or {}
            except Exception:
                logger.debug("Could not load post_generator config; using defaults", exc_info=True)
        self.pg_persona = pg.get("persona") or DEFAULT_PERSONA
        self.pg_tone = pg.get("tone") or ""
        self.pg_voice = pg.get("voice") or ""
        self.pg_things_to_avoid = pg.get("things_to_avoid") or []
        self.pg_topics = pg.get("topics") or list(TOPICS)
        self.pg_styles = pg.get("styles") or dict(STYLE_INSTRUCTIONS)

    def _log_api_usage(self, endpoint: str, est_cost: float, model: str = None):
        """Append a paid-API-call record to api_usage.jsonl (CLAUDE.md cost discipline).

        `comment_generator` has logged every paid call since the cost rule was
        written; this module never did, so post generation was invisible spend —
        and scheduled generation would have made it recurring invisible spend.
        Same format and same best-effort failure handling, so one ledger covers
        both generators.
        """
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
            logger.debug("Failed to write api_usage.jsonl", exc_info=True)

    def _build_system_prompt(self, article: bool = False) -> str:
        """Build the system prompt from the profile config (persona/tone/voice/avoid)
        plus the fixed formatting rules. Used for both thought-leadership and article posts."""
        parts = [f"You are {self.pg_persona}"]
        if self.pg_tone:
            parts.append(f"\nVoice and tone: {self.pg_tone}")
        if self.pg_voice:
            parts.append(self.pg_voice)
        if article:
            parts.append(
                "\nSomeone shared a news article with you; write a LinkedIn post reacting "
                "to it. Lead with YOUR take on why it matters, not a summary. Add your own "
                "insight or prediction. Reference the article naturally. The reader sees the "
                "link preview below your text, so don't include the URL in the text."
            )
        parts.append("\n" + POST_FORMAT_RULES)
        if self.pg_things_to_avoid:
            parts.append("\nAlso avoid: " + "; ".join(self.pg_things_to_avoid))
        return "\n".join(parts)

    def generate_thought_leadership(self, style: str = None, topic: str = None) -> Dict:
        """Generate a thought leadership post."""
        if not style:
            style = random.choice(list(self.pg_styles.keys()) or POST_STYLES)
        if not topic:
            topic = random.choice(self.pg_topics)

        style_instruction = self.pg_styles.get(style, STYLE_INSTRUCTIONS.get(style, ""))

        # Random length target for variety
        length_targets = [
            "Write just 1-2 sentences. Be punchy and direct. Under 30 words.",
            "Keep this short — 2-3 sentences max. Around 30-50 words.",
            "A short paragraph. 50-80 words.",
            "Medium length. 80-150 words.",
            "Full post. 150-250 words. Use line breaks between thoughts.",
            "One bold sentence. That's it. Make it count.",
            "A few sentences, maybe 40-60 words. Don't overthink it.",
            "Go longer on this one. 150-250 words with some substance.",
        ]
        length_instruction = random.choice(length_targets)

        prompt = f"""Write a LinkedIn post about: {topic}

Style: {style_instruction}

Length: {length_instruction}

Remember: No hashtags. No corporate buzzwords. Keep it real and conversational. 
Write ONLY the post text, nothing else."""

        logger.info(f"Generating {style} post about: {topic[:60]}...")

        self._log_api_usage("chat.completions:post_thought_leadership", 0.0004)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self._build_system_prompt()},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=500,
        )

        text = response.choices[0].message.content.strip()
        # Clean up any quotes the model might wrap it in
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]

        post = {
            "type": "thought_leadership",
            "style": style,
            "topic": topic,
            "text": text,
            "model": self.model,
        }

        post_id = self.queue.add(post)
        logger.info(f"✓ Post #{post_id} added to queue ({style})")

        return post

    def generate_from_article(self, url: str) -> Dict:
        """Generate a post reacting to a news article."""
        logger.info(f"Fetching article: {url}")

        # Fetch article content
        article_text = self._fetch_article(url)
        if not article_text:
            raise ValueError(f"Could not fetch article content from: {url}")

        # Random length target for variety
        article_lengths = [
            "React in just 1-2 sentences. Quick and punchy.",
            "Keep it to 2-3 sentences. Brief reaction.",
            "A short paragraph, 50-80 words.",
            "Medium reaction, 80-150 words.",
            "Full reaction post, 150-250 words with your analysis.",
            "One sharp sentence reacting to this. Make it hit.",
        ]
        length_instruction = random.choice(article_lengths)

        prompt = f"""Here's a news article I want to react to on LinkedIn:

---
{article_text[:3000]}
---

Article URL: {url}

Write a LinkedIn post reacting to this article. Your commentary goes above the link.
The reader will see the link preview below your text automatically.
Write ONLY the post text. Don't include the URL in the text — LinkedIn will show it as a link preview.

Length: {length_instruction}"""

        logger.info("Generating article reaction post...")

        self._log_api_usage("chat.completions:post_article", 0.0006)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self._build_system_prompt(article=True)},
                {"role": "user", "content": prompt}
            ],
            temperature=0.85,
            max_tokens=500,
        )

        text = response.choices[0].message.content.strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]

        post = {
            "type": "article",
            "url": url,
            "text": text,
            "model": self.model,
        }

        post_id = self.queue.add(post)
        logger.info(f"✓ Article post #{post_id} added to queue")

        return post

    def _fetch_article(self, url: str) -> Optional[str]:
        """Fetch article text from URL. Tries requests first, falls back to Selenium."""
        # Try requests first (fast)
        text = self._fetch_with_requests(url)
        if text and len(text) > 100:
            return text

        # Fallback: Selenium (handles JS-rendered sites)
        logger.info("  Requests fetch failed or got too little content, trying Selenium...")
        text = self._fetch_with_selenium(url)
        if text and len(text) > 100:
            return text

        logger.error(f"Could not extract content from: {url}")
        return None

    def _fetch_with_requests(self, url: str) -> Optional[str]:
        """Fetch article with requests library."""
        try:
            import requests
            from bs4 import BeautifulSoup  # noqa: F401  # availability check; parsing done in _parse_html

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            }
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()

            return self._parse_html(resp.text)

        except ImportError:
            logger.error("Install requests and beautifulsoup4: pip install requests beautifulsoup4")
            return None
        except Exception as e:
            logger.debug(f"Requests fetch failed: {e}")
            return None

    def _fetch_with_selenium(self, url: str) -> Optional[str]:
        """Fetch article with Selenium (handles JS-rendered pages)."""
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from webdriver_manager.chrome import ChromeDriverManager
            from selenium.webdriver.chrome.service import Service

            options = Options()
            options.add_argument('--headless=new')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            options.add_argument('--window-size=1920,1080')
            options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36')

            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)

            try:
                # The same bound as every LinkedIn driver (pm). A stalled
                # article page raises TimeoutException into the except below.
                if HAS_PM:
                    driver.set_page_load_timeout(pm.PAGE_LOAD_TIMEOUT_SECONDS)
                driver.get(url)
                # Let JS render. Headless article fetch (not LinkedIn), but jitter
                # the wait anyway rather than a flat 3s.
                time.sleep(random.uniform(2.5, 4.0))
                html = driver.page_source
                return self._parse_html(html)
            finally:
                driver.quit()

        except Exception as e:
            logger.debug(f"Selenium fetch failed: {e}")
            return None

    def _parse_html(self, html: str) -> Optional[str]:
        """Parse HTML and extract article text."""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, 'html.parser')

            # Remove noise elements
            for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside',
                            'iframe', 'noscript', 'svg', 'form']):
                tag.decompose()

            # Get title
            title = ""
            h1 = soup.find('h1')
            if h1:
                title = h1.get_text(strip=True)
            elif soup.find('title'):
                title = soup.find('title').get_text(strip=True)

            # Try to find article body (most specific to least)
            text = ""

            # 1. <article> tag
            article = soup.find('article')
            if article:
                text = article.get_text(separator='\n', strip=True)

            # 2. Main content area
            if not text or len(text) < 100:
                main = soup.find('main') or soup.find('div', {'role': 'main'})
                if main:
                    text = main.get_text(separator='\n', strip=True)

            # 3. Common content class names
            if not text or len(text) < 100:
                for selector in ['[class*="article-body"]', '[class*="post-content"]',
                                '[class*="entry-content"]', '[class*="content-body"]',
                                '[class*="blog-post"]', '[class*="page-content"]',
                                '[id*="content"]', '[id*="article"]']:
                    el = soup.select_one(selector)
                    if el:
                        candidate = el.get_text(separator='\n', strip=True)
                        if len(candidate) > len(text):
                            text = candidate

            # 4. All paragraphs with meaningful content
            if not text or len(text) < 100:
                paragraphs = soup.find_all('p')
                text = '\n'.join(
                    p.get_text(strip=True) for p in paragraphs
                    if len(p.get_text(strip=True)) > 30
                )

            # 5. Last resort: body text
            if not text or len(text) < 100:
                body = soup.find('body')
                if body:
                    text = body.get_text(separator='\n', strip=True)

            # Clean up
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            text = '\n'.join(lines)

            return f"Title: {title}\n\n{text}" if text else None

        except Exception as e:
            logger.debug(f"HTML parsing failed: {e}")
            return None

    def post_next(self, profile_name: str = None) -> bool:
        """Post the next item in the queue to LinkedIn."""
        post = self.queue.get_next()
        if not post:
            logger.info("Queue is empty — nothing to post")
            return False
        return self._publish(post, profile_name)

    def post_by_id(self, post_id: int, profile_name: str = None) -> bool:
        """Post a specific queued post to LinkedIn."""
        post = self.queue.get_by_id(post_id)
        if not post:
            logger.error(f"Post #{post_id} not found in queue")
            return False
        return self._publish(post, profile_name)

    def _publish(self, post: Dict, profile_name: str = None) -> bool:
        """Publish a post to LinkedIn using linkedin_poster."""
        from .poster import LinkedInPoster

        pname = profile_name or self.profile_name
        text = post["text"]

        # For article posts, append the URL so LinkedIn generates a preview
        if post.get("type") == "article" and post.get("url"):
            text = f"{text}\n\n{post['url']}"

        logger.info(f"Publishing post #{post['id']}...")
        logger.info(f"Text: {text[:100]}...")

        poster = LinkedInPoster(profile_name=pname)
        try:
            poster.setup()
            poster.navigate_to_feed()
            success = poster.create_post(text)

            if success:
                self.queue.mark_posted(post["id"])
                logger.info(f"✓ Post #{post['id']} published and moved to history")
                return True
            else:
                logger.error(f"Failed to publish post #{post['id']}")
                return False
        finally:
            if poster.driver:
                poster.driver.quit()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    """CLI entry point for the LinkedIn post generator / queue manager."""
    parser = argparse.ArgumentParser(
        description='LinkedIn Post Generator & Queue Manager',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest='command', help='Command')

    # generate
    gen_p = subparsers.add_parser('generate', help='Generate thought leadership post(s)')
    gen_p.add_argument('--count', type=int, default=1, help='Number of posts to generate')
    gen_p.add_argument('--style', type=str, choices=POST_STYLES, help='Post style')
    gen_p.add_argument('--topic', type=str, help='Custom topic')
    gen_p.add_argument('--model', type=str, default='gpt-4o-mini', help='OpenAI model')
    gen_p.add_argument('--profile', type=str, default=None)

    # article
    art_p = subparsers.add_parser('article', help='Generate post from article URL')
    art_p.add_argument('url', help='Article URL')
    art_p.add_argument('--model', type=str, default='gpt-4o-mini', help='OpenAI model')
    art_p.add_argument('--profile', type=str, default=None)

    # queue
    q_p = subparsers.add_parser('queue', help='Show the post queue')
    q_p.add_argument('--profile', type=str, default=None)

    # next
    n_p = subparsers.add_parser('next', help='Show the next post to publish')
    n_p.add_argument('--profile', type=str, default=None)

    # post
    post_p = subparsers.add_parser('post', help='Publish next post (or specific post) to LinkedIn')
    post_p.add_argument('--id', type=int, help='Specific post ID to publish')
    post_p.add_argument('--profile', type=str, default=None)

    # remove
    rm_p = subparsers.add_parser('remove', help='Remove a post from the queue')
    rm_p.add_argument('--id', type=int, required=True, help='Post ID to remove')
    rm_p.add_argument('--profile', type=str, default=None)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # ── Generate ──
    if args.command == 'generate':
        gen = PostGenerator(profile_name=args.profile, model=args.model)
        for i in range(args.count):
            post = gen.generate_thought_leadership(style=args.style, topic=args.topic)
            print(f"\n{'─'*60}")
            print(f"Post #{post.get('id', '?')} [{post['style']}]")
            print(f"{'─'*60}")
            print(post['text'])
            print()
            if i < args.count - 1:
                time.sleep(1)  # Small delay between API calls

    # ── Article ──
    elif args.command == 'article':
        gen = PostGenerator(profile_name=args.profile, model=args.model)
        post = gen.generate_from_article(args.url)
        print(f"\n{'─'*60}")
        print(f"Article Post #{post.get('id', '?')}")
        print(f"URL: {args.url}")
        print(f"{'─'*60}")
        print(post['text'])
        print()

    # ── Queue ──
    elif args.command == 'queue':
        queue = PostQueue(args.profile)
        posts = queue.list_queued()
        if not posts:
            print("\nQueue is empty")
            return

        next_post = queue.get_next()
        next_id = next_post["id"] if next_post else None

        print(f"\n{'═'*60}")
        print(f"  POST QUEUE ({len(posts)} posts)")
        print(f"{'═'*60}")

        for p in posts:
            marker = " ➤ NEXT" if p["id"] == next_id else ""
            ptype = "📰 Article" if p["type"] == "article" else f"💡 {p.get('style', 'thought')}"
            queued = p.get("queued_at", "")[:16]
            print(f"\n  #{p['id']} [{ptype}] {queued}{marker}")
            print(f"  {p['text'][:120]}...")
            if p.get("url"):
                print(f"  Link: {p['url']}")

        print(f"\n{'═'*60}\n")

    # ── Next ──
    elif args.command == 'next':
        queue = PostQueue(args.profile)
        post = queue.get_next()
        if not post:
            print("\nQueue is empty — nothing to post")
            return

        ptype = "Article" if post["type"] == "article" else f"Thought Leadership ({post.get('style', '')})"
        print(f"\n{'═'*60}")
        print(f"  NEXT UP: Post #{post['id']} [{ptype}]")
        print(f"  Queued: {post.get('queued_at', '')[:16]}")
        if post.get("url"):
            print(f"  Article: {post['url']}")
        print(f"{'═'*60}")
        print()
        print(post['text'])
        if post.get("type") == "article" and post.get("url"):
            print(f"\n{post['url']}")
        print(f"\n{'═'*60}")
        print(f"  Run: python linkedin_post_generator.py post --profile {args.profile or 'default'}")
        print()

    # ── Post ──
    elif args.command == 'post':
        gen = PostGenerator(profile_name=args.profile)
        if args.id:
            gen.post_by_id(args.id, profile_name=args.profile)
        else:
            gen.post_next(profile_name=args.profile)

    # ── Remove ──
    elif args.command == 'remove':
        queue = PostQueue(args.profile)
        if queue.remove(args.id):
            print(f"✓ Post #{args.id} removed from queue")
        else:
            print(f"✗ Post #{args.id} not found in queue")


if __name__ == "__main__":
    main()
