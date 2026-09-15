"""
LinkedIn Auto-Connector
Goes through search results and sends connection requests with human-like behavior.
Respects weekly limits, tracks sent requests, paginates through pages.

Updated Feb 2026: LinkedIn uses data-view-name attributes for search results.
Connect actions are <a> links to /preload/search-custom-invite/ (NOT <button>s).
"""

import os
import json
import sys
import time
import logging
import argparse
import re
from datetime import datetime
from typing import List, Dict, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException, ElementClickInterceptedException
)
from dotenv import load_dotenv

from . import profile_manager as pm
from . import human_behavior as hb
from .failure_capture import capture_failure

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─── Connection Tracker ──────────────────────────────────────────────────────

class ConnectionTracker:
    """Track connection requests to avoid duplicates and respect daily/weekly limits."""

    DEFAULT_WEEKLY_LIMIT = 100  # LinkedIn's approximate weekly limit
    DEFAULT_DAILY_LIMIT = 25

    def __init__(self, profile_name: str, weekly_limit: int = None, daily_limit: int = None):
        self.profile_name = profile_name
        self.weekly_limit = weekly_limit if weekly_limit is not None else self.DEFAULT_WEEKLY_LIMIT
        self.daily_limit = daily_limit if daily_limit is not None else self.DEFAULT_DAILY_LIMIT
        self.data_dir = pm.get_data_dir(profile_name, "connections")
        self.tracker_file = os.path.join(self.data_dir, "connection_tracker.json")
        self.data = self._load()

    def _load(self) -> Dict:
        if os.path.exists(self.tracker_file):
            with open(self.tracker_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data.setdefault("daily_counts", {})  # additive for older tracker files
            return data
        return {
            "sent_requests": [],
            "weekly_counts": {},
            "daily_counts": {},
            "skipped": [],
            "errors": []
        }

    def save(self):
        """Persist the connection tracker data to disk."""
        with open(self.tracker_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get_week_key(self) -> str:
        """Get ISO week key for tracking weekly limits."""
        now = datetime.now()
        return f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

    def get_day_key(self) -> str:
        """Get the day key (YYYY-MM-DD) for tracking daily limits."""
        return datetime.now().strftime("%Y-%m-%d")

    def get_weekly_count(self) -> int:
        """How many requests sent this week."""
        week = self.get_week_key()
        return self.data.get("weekly_counts", {}).get(week, 0)

    def get_daily_count(self) -> int:
        """How many requests sent today."""
        day = self.get_day_key()
        return self.data.get("daily_counts", {}).get(day, 0)

    def get_remaining(self) -> int:
        """How many requests we can still send, respecting BOTH the daily and weekly caps."""
        weekly_remaining = max(0, self.weekly_limit - self.get_weekly_count())
        daily_remaining = max(0, self.daily_limit - self.get_daily_count())
        return min(weekly_remaining, daily_remaining)

    def already_sent(self, profile_url: str) -> bool:
        """Check if we already sent a request to this person."""
        normalized = self._normalize_url(profile_url)
        if not normalized:
            return False
        return any(
            self._normalize_url(r.get("profile_url", "")) == normalized
            for r in self.data.get("sent_requests", [])
        )

    def record_sent(self, name: str, profile_url: str, title: str = ""):
        """Record a sent connection request (increments both weekly and daily counts)."""
        week = self.get_week_key()
        day = self.get_day_key()

        self.data["sent_requests"].append({
            "name": name,
            "profile_url": profile_url,
            "title": title,
            "sent_at": datetime.now().isoformat(),
            "week": week,
            "day": day
        })

        weekly = self.data.setdefault("weekly_counts", {})
        weekly[week] = weekly.get(week, 0) + 1
        daily = self.data.setdefault("daily_counts", {})
        daily[day] = daily.get(day, 0) + 1

        self.save()

    def record_skip(self, name: str, profile_url: str, reason: str):
        """Record a skipped connection."""
        self.data["skipped"].append({
            "name": name,
            "profile_url": profile_url,
            "reason": reason,
            "at": datetime.now().isoformat()
        })
        self.save()

    def record_error(self, name: str, profile_url: str, error: str):
        """Record an error."""
        self.data["errors"].append({
            "name": name,
            "profile_url": profile_url,
            "error": error,
            "at": datetime.now().isoformat()
        })
        self.save()

    def _normalize_url(self, url: str) -> str:
        """Normalize profile URL for comparison."""
        if not url:
            return ""
        url = url.split("?")[0].rstrip("/").lower()
        return url

    def get_stats(self) -> Dict:
        """Get summary stats."""
        return {
            "total_sent": len(self.data.get("sent_requests", [])),
            "this_week": self.get_weekly_count(),
            "today": self.get_daily_count(),
            "remaining": self.get_remaining(),
            "daily_limit": self.daily_limit,
            "weekly_limit": self.weekly_limit,
            "total_skipped": len(self.data.get("skipped", [])),
            "total_errors": len(self.data.get("errors", []))
        }


# ─── Auto Connector ─────────────────────────────────────────────────────────

class LinkedInAutoConnector:
    """Automated LinkedIn connection sender with human-like behavior.

    Current DOM (July 2026; see .dev/DECISIONS.md for the rotation history):
    - LinkedIn REMOVED data-view-name from the search results. Cards are now
        <div componentkey="..."> with hashed class names — there is no clean card
        wrapper or name selector anymore. So we iterate the CONNECT LINKS directly
        (link-first) instead of iterating cards.
    - Connect: an ICON-ONLY <a aria-label="Invite <Name> to connect"
        href="/preload/search-custom-invite/?vanityName=<vanity>">. The link
        carries everything we need: the NAME (aria-label) and the invite action +
        vanity (href). No visible "Connect" text (icon), so match on aria-label/href.
    - Name: parsed from the link's aria-label ("Invite <NAME> to connect").
    - Profile URL / dedup id: derived from the href's vanityName
        (https://www.linkedin.com/in/<vanity>/) — the person's own invite link, so
        it's the correct person and a stable dedup key even if the profile URL
        can't be isolated from the (hashed) card DOM.
    - Card + name selectors (data-view-name) are kept only as a FALLBACK for if
        LinkedIn rotates again.
    - Pagination: numbered page buttons + a dedicated Next
        <button data-testid="pagination-controls-next-button-visible">.
    """

    # ── Selectors (extracted as constants so selector_health_check can reference
    #    them and stay in sync). New data-view-name/data-testid/aria hooks FIRST,
    #    legacy classes as fallbacks. ──
    SEARCH_RESULT_SELECTOR = "div[data-view-name='people-search-result']"
    RESULT_CARD_SELECTORS = [
        SEARCH_RESULT_SELECTOR,
        "[data-view-name='people-search-result']",       # tag-agnostic
    ]
    RESULT_CARD_FALLBACK_SELECTORS = [
        "[data-view-name='people-search-result']",
        "li[data-chameleon-result-urn]",
        "li.reusable-search__result-container",
        "div.entity-result",
    ]
    RESULT_NAME_SELECTOR = "a[data-view-name='search-result-lockup-title']"

    # ── Link-first (PRIMARY): iterate the Connect/Invite links directly. Each link
    #    carries the name (aria-label) and the invite action + vanity (href), so we
    #    don't need to isolate a card container or a name element. ──
    CONNECT_LINK_SELECTORS = [
        "a[aria-label^='Invite'][href*='/preload/search-custom-invite/']",
        "a[href*='/preload/search-custom-invite/']",
        "button[aria-label^='Invite']",              # button variant (if rotated)
    ]

    # Connect: matched by aria-label/href, NOT visible text (icon-only link).
    CONNECT_ACTION_SELECTOR = "div[data-view-name='edge-creation-connect-action']"
    CONNECT_BUTTON_SELECTORS = [
        "a[aria-label^='Invite'][href*='/preload/search-custom-invite/']",
        "div[data-view-name='edge-creation-connect-action'] a[href*='/preload/search-custom-invite/']",
        "a[href*='/preload/search-custom-invite/']",
        "button[aria-label^='Invite']",                  # button variant (if rotated)
        "button[aria-label*='to connect']",
    ]
    # aria-label substrings that identify a Connect/Invite control.
    CONNECT_ARIA_HINTS = ("to connect", "invite")
    # 'More' overflow on a card (when LinkedIn nests Connect behind it). Not seen
    # in the June 30 dump, but handled defensively (see _find_connect_behind_more).
    MORE_BUTTON_SELECTORS = [
        "button[aria-label^='More actions']",
        "button[aria-label*='More actions']",
        "button[aria-label^='More']",
    ]

    # Pagination: data-testid Next button first; numbered pages as a fallback.
    PAGINATION_NEXT_SELECTORS = [
        "button[data-testid='pagination-controls-next-button-visible']",
        "button[data-testid^='pagination-controls-next']",
        "button[aria-label='Next']",                     # legacy fallback
        "button.artdeco-pagination__button--next",
    ]
    NEXT_PAGE_SELECTORS = PAGINATION_NEXT_SELECTORS      # task-spec alias
    PAGE_INDICATOR_SELECTOR = "button[data-testid^='pagination-indicator-']"
    PAGE_CURRENT_SELECTOR = "button[aria-current='true'][data-testid^='pagination-indicator-']"

    # Send button in the connect modal. NOTE: the modal only appears AFTER clicking
    # Connect, so it is NOT in the search-page dump and can't be verified from it;
    # these aria-label hooks are kept from the last working connect flow.
    SEND_BUTTON_ARIA_LABELS = ["Send without a note", "Send now", "Send"]
    SEND_BUTTON_SELECTORS = [
        "button[aria-label='Send without a note']",
        "button[aria-label='Send now']",
        "button[aria-label='Send']",
        "button[aria-label*='Send without']",
    ]
    SEND_BUTTON_WAIT_SELECTOR = (
        "button[aria-label*='Send'], button[aria-label*='send'], span.artdeco-button__text"
    )
    SEND_BUTTON_LEGACY_SELECTOR = "span.artdeco-button__text"
    MODAL_SELECTORS = ["div[role='dialog']", "div.artdeco-modal", "div.send-invite"]
    INTEROP_OUTLET_SELECTOR = "#interop-outlet"

    def __init__(self, profile_name: str = None, max_requests: int = None,
                 add_note: bool = None, note_text: str = None, debug: bool = False):
        self.profile_name = profile_name

        # Per-profile connector config; explicit CLI args (passed in) still win.
        full_config = pm.get_profile_config(profile_name)
        # Apply tunable human-behavior timing (typing/reading/scroll/break ranges)
        # from the "behavior" section so the connector's pacing is configurable
        # and consistent with the scraper/poster.
        hb.configure_behavior(full_config.get("behavior"))
        conn = full_config.get("connector", {})
        self.daily_limit = conn.get("max_daily_requests", 25)
        self.weekly_limit = conn.get("weekly_limit", 100)
        self.max_requests = max_requests if max_requests is not None else self.daily_limit
        self.note_text = note_text if note_text is not None else conn.get("note_template", "")
        self.add_note = add_note if add_note is not None else bool(self.note_text)
        self.debug = debug

        self.driver = None
        #: Set by run() when the session dies; None on a clean run.
        self.failure = None
        #: Which phase run() is in, so a crash says WHERE it happened.
        self.stage = "init"
        self.wait = None
        self.tracker = None

        self.sent_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.pages_processed = 0

        # Stop file: if this file exists, the connector will gracefully stop
        resolved = profile_name or "default"
        self.stop_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f".stop_connector_{resolved}"
        )
        if os.path.exists(self.stop_file):
            os.remove(self.stop_file)

    def should_stop(self) -> bool:
        """Check if a stop has been requested via stop file."""
        if os.path.exists(self.stop_file):
            logger.info("⛔ Stop requested — shutting down gracefully...")
            try:
                os.remove(self.stop_file)
            except Exception:
                logger.debug("Suppressed non-critical exception", exc_info=True)
            return True
        return False

    def setup(self):
        """Setup driver and login."""
        logger.info("Setting up browser...")
        self.driver, profile = pm.create_driver(self.profile_name)
        self.wait = WebDriverWait(self.driver, 20)

        if not pm.login(self.driver, profile):
            raise RuntimeError("Failed to log in")

        resolved = self.profile_name or pm.get_default_profile_name() or "default"
        self.tracker = ConnectionTracker(
            resolved, weekly_limit=self.weekly_limit, daily_limit=self.daily_limit
        )

        remaining = self.tracker.get_remaining()
        logger.info(
            f"Limits: {self.tracker.get_daily_count()}/{self.daily_limit} today, "
            f"{self.tracker.get_weekly_count()}/{self.weekly_limit} this week — "
            f"{remaining} remaining"
        )

        if remaining == 0:
            logger.warning("Daily or weekly connection limit reached! Try again later.")

        self.max_requests = min(self.max_requests, remaining)
        logger.info(f"Will send up to {self.max_requests} requests this session")

    # ─── Navigation ──────────────────────────────────────────────────────

    def navigate_to_search(self, url: str):
        """Navigate to a LinkedIn search results page."""
        logger.info(f"Navigating to: {url}")
        self.driver.get(url)
        hb.human_sleep(5, 8)

        # Wait for the link-first primary hook (Connect links) OR the fallback card
        # wrapper — whichever renders. (Waiting only on the dead card selector would
        # burn the full timeout every run now that data-view-name is gone.)
        ready_selector = f"{self.CONNECT_LINK_SELECTORS[0]}, {self.SEARCH_RESULT_SELECTOR}"
        try:
            self.wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, ready_selector)
            ))
            logger.info("Search results loaded")
        except TimeoutException:
            logger.warning("Timed out waiting for results — scrolling to trigger lazy load")

        self._scroll_page()
        return True

    def _scroll_page(self):
        """Scroll through page to trigger lazy loading."""
        try:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 3);")
            hb.human_sleep(1.5, 2.5)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 2 / 3);")
            hb.human_sleep(1.5, 2.5)
            self.driver.execute_script("window.scrollTo(0, 0);")
            hb.human_sleep(1.0, 2.0)
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

    def go_to_next_page(self) -> bool:
        """Navigate to the next page of results."""
        hb.human_sleep(1.0, 2.0)

        for selector in self.PAGINATION_NEXT_SELECTORS:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed() and btn.is_enabled():
                    hb.scroll_to_element(self.driver, btn)
                    hb.human_sleep(0.5, 1.0)
                    hb.human_click(self.driver, btn)
                    hb.human_sleep(4.0, 6.0)
                    self.pages_processed += 1
                    logger.info(f"Navigated to page {self.pages_processed + 1}")
                    return True
            except Exception:
                continue

        # Numbered-pagination fallback: read the active page (aria-current) and
        # click "Page <current+1>" directly.
        try:
            current = self.driver.find_element(By.CSS_SELECTOR, self.PAGE_CURRENT_SELECTOR)
            label = current.get_attribute("aria-label") or ""
            match = re.search(r'Page (\d+)', label)
            if match:
                next_page = int(match.group(1)) + 1
                btn = self.driver.find_element(
                    By.CSS_SELECTOR, f"button[aria-label='Page {next_page}']"
                )
                if btn.is_displayed() and btn.is_enabled():
                    hb.scroll_to_element(self.driver, btn)
                    hb.human_sleep(0.5, 1.0)
                    hb.human_click(self.driver, btn)
                    hb.human_sleep(4.0, 6.0)
                    self.pages_processed += 1
                    logger.info(f"Navigated to page {next_page} (numbered pagination)")
                    return True
        except Exception:
            logger.debug("Numbered-pagination fallback did not apply", exc_info=True)

        # URL-based pagination fallback
        try:
            current_url = self.driver.current_url
            if "page=" in current_url:
                current_page = int(re.search(r'page=(\d+)', current_url).group(1))
                next_url = re.sub(r'page=\d+', f'page={current_page + 1}', current_url)
            else:
                separator = "&" if "?" in current_url else "?"
                next_url = f"{current_url}{separator}page=2"

            self.driver.get(next_url)
            hb.human_sleep(5.0, 7.0)
            self.pages_processed += 1
            logger.info(f"Navigated to page {self.pages_processed + 1} via URL")
            return True
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

        logger.info("No more pages available")
        return False

    # ─── Finding Cards ───────────────────────────────────────────────────

    # ─── Link-first discovery (PRIMARY) ──────────────────────────────────

    def find_connect_links(self) -> List:
        """Find all Connect/Invite links on the page (the link-first primary path).

        LinkedIn dropped data-view-name and there's no clean card/name selector,
        but each Connect link still has aria-label "Invite <Name> to connect" and a
        /preload/search-custom-invite/ href — everything needed to connect. We
        iterate these directly instead of isolating cards.
        """
        for selector in self.CONNECT_LINK_SELECTORS:
            links = [
                el for el in self.driver.find_elements(By.CSS_SELECTOR, selector)
                if self._is_connect_control(el)
            ]
            if links:
                logger.info(f"Found {len(links)} Connect links (via {selector})")
                return links
        return []

    @staticmethod
    def name_from_invite_label(label: str) -> str:
        """Parse the person's name from an invite aria-label.

        "Invite Jordan Rivera to connect" -> "Jordan Rivera". Strips the leading
        "Invite " and the trailing " to connect" (case-insensitively). Returns
        "Unknown" when the label isn't an invite label.
        """
        if not label:
            return "Unknown"
        text = label.strip()
        if text.lower().startswith("invite "):
            text = text[len("invite "):]
        idx = text.lower().rfind(" to connect")
        if idx != -1:
            text = text[:idx]
        return text.strip() or "Unknown"

    def extract_person_from_link(self, link) -> Dict:
        """Build a person-info dict from a Connect/Invite link (link-first).

        Name comes from the aria-label; the invite action is the link itself; the
        profile URL / dedup id is derived from the href's vanityName (the person's
        own invite link, so it's the correct person and a stable dedup key even
        when the card DOM can't be isolated).
        """
        info = {
            "name": "Unknown",
            "title": "",
            "profile_url": "",
            "has_connect": True,
            "connect_element": link,
            "vanity_name": "",
        }
        try:
            info["name"] = self.name_from_invite_label(link.get_attribute("aria-label") or "")
            href = link.get_attribute("href") or ""
            if "vanityName=" in href:
                vanity = href.split("vanityName=")[-1].split("&")[0].strip()
                if vanity:
                    info["vanity_name"] = vanity
                    # The vanity is the member's own — construct their profile URL
                    # as the dedup id (correct person; stable even if the card DOM
                    # never exposes an /in/ link).
                    info["profile_url"] = f"https://www.linkedin.com/in/{vanity}/"
        except Exception:
            logger.debug("extract_person_from_link failed", exc_info=True)
        return info

    def find_result_cards(self) -> List:
        """Find all people search result cards (FALLBACK path if no Connect links)."""
        # Primary (data-view-name) then legacy fallbacks, in order.
        for selector in self.RESULT_CARD_SELECTORS + self.RESULT_CARD_FALLBACK_SELECTORS:
            cards = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if cards:
                logger.info(f"Found {len(cards)} result cards (via {selector})")
                return cards

        logger.warning("No result cards found on this page")
        self._dump_diagnostics()
        return []

    def _dump_diagnostics(self):
        """Quick diagnostic when cards aren't found."""
        try:
            info = self.driver.execute_script("""
                var dvn = document.querySelectorAll('[data-view-name]');
                var counts = {};
                for (var i = 0; i < dvn.length; i++) {
                    var n = dvn[i].getAttribute('data-view-name');
                    counts[n] = (counts[n] || 0) + 1;
                }
                return JSON.stringify(counts);
            """)
            logger.info(f"Page data-view-name counts: {info}")
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

    # ─── Extracting Person Info ──────────────────────────────────────────

    def extract_person_info(self, card) -> Dict:
        """Extract name, title, profile URL, and connect status from a result card."""
        info = {
            "name": "Unknown",
            "title": "",
            "profile_url": "",
            "has_connect": False,
            "connect_element": None,
            "vanity_name": ""
        }

        try:
            # ── Name: from search-result-lockup-title ──
            try:
                name_links = card.find_elements(
                    By.CSS_SELECTOR, 'a[data-view-name="search-result-lockup-title"]'
                )
                for link in name_links:
                    name = link.text.strip()
                    if name and 1 < len(name) < 60:
                        info["name"] = name
                        break
            except Exception:
                logger.debug("Suppressed non-critical exception", exc_info=True)

            # ── Profile URL ──
            try:
                link = card.find_element(By.CSS_SELECTOR, 'a[href*="/in/"]')
                href = link.get_attribute("href")
                if href and "/in/" in href:
                    info["profile_url"] = href.split("?")[0]
            except Exception:
                logger.debug("Suppressed non-critical exception", exc_info=True)

            # ── Title/headline ──
            try:
                card_text = card.text.strip()
                lines = [ln.strip() for ln in card_text.split("\n") if ln.strip()]
                skip_words = {"connect", "follow", "message", "pending", ""}
                for idx, line in enumerate(lines):
                    if info["name"] != "Unknown" and info["name"] in line:
                        for subsequent in lines[idx + 1:]:
                            if subsequent.lower() not in skip_words:
                                info["title"] = subsequent[:120]
                                break
                        break
            except Exception:
                logger.debug("Suppressed non-critical exception", exc_info=True)

            # ── Connect control ──
            # The Connect link is icon-only (aria-label "Invite <Name> to
            # connect", href /preload/search-custom-invite/, NO visible text), so
            # match on aria-label/href, not element.text. Falls back to a per-card
            # "More" overflow menu if Connect is nested behind one.
            connect_el = self._find_connect_in_card(card)
            if connect_el is not None:
                info["has_connect"] = True
                info["connect_element"] = connect_el
                href = connect_el.get_attribute("href") or ""
                if "vanityName=" in href:
                    info["vanity_name"] = href.split("vanityName=")[-1].split("&")[0]

        except Exception as e:
            logger.debug(f"Error extracting person info: {e}")

        return info

    def _is_connect_control(self, el) -> bool:
        """True if ``el`` is a Connect/Invite control (by aria-label/href/text)."""
        try:
            label = (el.get_attribute("aria-label") or "").lower()
            if any(hint in label for hint in self.CONNECT_ARIA_HINTS):
                return True
            if "/preload/search-custom-invite/" in (el.get_attribute("href") or ""):
                return True
            return (el.text or "").strip().lower() == "connect"
        except Exception:
            return False

    def _find_connect_in_card(self, card):
        """Return this card's Connect control, or None.

        Tries the direct Connect hooks first (icon <a>/<button> identified by
        aria-label/href), then a per-card "More" overflow menu as a fallback.
        """
        for selector in self.CONNECT_BUTTON_SELECTORS:
            try:
                for el in card.find_elements(By.CSS_SELECTOR, selector):
                    if self._is_connect_control(el):
                        return el
            except Exception:
                continue
        return self._find_connect_behind_more(card)

    def _find_connect_behind_more(self, card):
        """If a card hides Connect behind a 'More' overflow, open it and return the
        Connect item (leaving the menu open for the click), else None.

        Best-effort and defensive: the June 30 dump shows no per-card More menu,
        but LinkedIn nests Connect under 'More' for some profiles. If the menu
        opens with no Connect inside, it is dismissed so the next card is clean.
        """
        more_btn = None
        for selector in self.MORE_BUTTON_SELECTORS:
            try:
                for b in card.find_elements(By.CSS_SELECTOR, selector):
                    if b.is_displayed():
                        more_btn = b
                        break
            except Exception:
                continue
            if more_btn is not None:
                break
        if more_btn is None:
            return None

        try:
            hb.human_click(self.driver, more_btn)
            hb.human_sleep(0.5, 1.0)
        except Exception:
            logger.debug("Could not open card 'More' menu", exc_info=True)
            return None

        # The dropdown renders at the document level (role=menu), not inside the card.
        menu_candidates = self.driver.find_elements(
            By.CSS_SELECTOR,
            "div[role='menu'] a, div[role='menu'] button, "
            "a[href*='/preload/search-custom-invite/'], [aria-label*='to connect']",
        )
        for el in menu_candidates:
            try:
                if el.is_displayed() and self._is_connect_control(el):
                    return el          # leave menu open; click_connect will click it
            except Exception:
                continue

        # No Connect in the menu — close it so it doesn't block the next card.
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            hb.human_sleep(0.2, 0.5)
        except Exception:
            logger.debug("Could not dismiss empty 'More' menu", exc_info=True)
        return None

    # ─── Clicking Connect ────────────────────────────────────────────────

    def click_connect(self, info: Dict) -> bool:
        """Click the Connect link and handle what follows."""
        connect_el = info.get("connect_element")
        if not connect_el:
            return False

        try:
            hb.scroll_to_element(self.driver, connect_el)
            hb.human_sleep(0.5, 1.0)

            url_before = self.driver.current_url

            hb.human_click(self.driver, connect_el)
            hb.human_sleep(2.0, 3.5)

            return self._handle_after_click(url_before)

        except ElementClickInterceptedException:
            logger.debug("Click intercepted, trying JS click")
            try:
                url_before = self.driver.current_url
                self.driver.execute_script("arguments[0].click();", connect_el)
                hb.human_sleep(2.0, 3.5)
                return self._handle_after_click(url_before)
            except Exception as e:
                logger.warning(f"  ↳ JS click also failed: {e}")
                return False
        except Exception as e:
            logger.warning(f"  ↳ Error clicking connect: {e}")
            return False

    def _handle_after_click(self, url_before: str) -> bool:
        """Handle whatever happens after clicking Connect.
        
        Known flow (Feb 2026): Click Connect → popup appears with 
        'Send without a note' → click it → Pending. If dismissed, reverts.
        """
        current_url = self.driver.current_url

        # Case 1: Navigated to invite page
        if "search-custom-invite" in current_url:
            logger.info("  ↳ Landed on invite page")
            return self._handle_invite_page()

        # Case 2: Popup appeared on same page — find "Send without a note"
        # The popup may not be a standard modal. Search broadly.
        
        # Wait for popup to render (it comes up quickly in practice)
        hb.human_sleep(0.5, 1.0)
        
        # Try multiple strategies to find and click "Send without a note"
        if self._click_send_without_note():
            hb.human_sleep(0.3, 0.6)
            return True

        # Retry after a bit more time
        hb.human_sleep(0.5, 1.0)
        if self._click_send_without_note():
            hb.human_sleep(0.3, 0.6)
            return True

        # Check for limit warning
        if self._check_limit_warning():
            return False

        # Check standard modals
        modal = self._find_modal()
        if modal:
            return self._handle_modal(modal)

        # Last resort: check if it went to Pending anyway
        logger.warning("  ↳ Could not find 'Send without a note' popup")
        # Highest-value capture: the shadow-DOM Send modal never appeared, so a
        # screenshot + DOM is the fastest way to see what LinkedIn rendered.
        capture_failure(self.driver, "send_modal_missing", self.profile_name)
        return False

    def _click_send_without_note(self) -> bool:
        """Find and click 'Send without a note' — it lives inside the shadow DOM
        of <div id="interop-outlet">. Normal Selenium selectors can't reach it."""

        # Strategy 1: Pierce shadow DOM of #interop-outlet (Feb 2026 LinkedIn)
        try:
            clicked = self.driver.execute_script("""
                var host = document.querySelector('#interop-outlet');
                if (!host || !host.shadowRoot) return null;
                var buttons = host.shadowRoot.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var text = buttons[i].textContent.trim();
                    if (text === 'Send without a note') {
                        buttons[i].click();
                        return 'Send without a note';
                    }
                }
                // Fallback: look for "Send" or "Send now"
                for (var i = 0; i < buttons.length; i++) {
                    var text = buttons[i].textContent.trim();
                    if (text === 'Send now' || text === 'Send') {
                        buttons[i].click();
                        return text;
                    }
                }
                return null;
            """)
            if clicked:
                logger.info(f"  ↳ Clicked '{clicked}' in shadow DOM")
                return True
        except Exception as e:
            logger.debug(f"Shadow DOM strategy failed: {e}")

        # Strategy 2: Try any shadow root on the page
        try:
            clicked = self.driver.execute_script("""
                var allEls = document.querySelectorAll('*');
                for (var i = 0; i < allEls.length; i++) {
                    if (allEls[i].shadowRoot) {
                        var buttons = allEls[i].shadowRoot.querySelectorAll('button');
                        for (var b = 0; b < buttons.length; b++) {
                            var text = buttons[b].textContent.trim();
                            if (text === 'Send without a note' || text === 'Send now' || text === 'Send') {
                                buttons[b].click();
                                return text;
                            }
                        }
                    }
                }
                return null;
            """)
            if clicked:
                logger.info(f"  ↳ Clicked '{clicked}' in shadow DOM (broad search)")
                return True
        except Exception as e:
            logger.debug(f"Broad shadow DOM search failed: {e}")

        # Strategy 3: Regular DOM fallback (in case LinkedIn changes back)
        try:
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "button"):
                try:
                    text = btn.text.strip().lower()
                    if "send without" in text or text == "send now" or text == "send":
                        if btn.is_displayed() and btn.is_enabled():
                            logger.info(f"  ↳ Found button in regular DOM: '{btn.text.strip()}'")
                            hb.human_click(self.driver, btn)
                            return True
                except StaleElementReferenceException:
                    continue
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

        return False

    def _check_limit_warning(self) -> bool:
        """Check for LinkedIn's connection limit warning.
        Returns True if limit was hit (meaning we should stop)."""
        try:
            # Check regular DOM
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()

            # Also check shadow DOM text — the #interop-outlet host first, then a
            # broad fallback over ALL shadow roots in case LinkedIn renamed the outlet.
            shadow_text = self.driver.execute_script(f"""
                var host = document.querySelector('{self.INTEROP_OUTLET_SELECTOR}');
                if (host && host.shadowRoot) return host.shadowRoot.textContent.toLowerCase();
                var txt = '';
                var all = document.querySelectorAll('*');
                for (var i = 0; i < all.length; i++) {{
                    if (all[i].shadowRoot) txt += ' ' + all[i].shadowRoot.textContent;
                }}
                return txt.toLowerCase();
            """) or ""

            combined = page_text + " " + shadow_text

            limit_phrases = [
                "weekly invitation limit",
                "invitation limit",
                "you've reached the weekly",
                "you've reached your weekly",
                "connection limit",
                "too many invitations",
                "can't send more invitations",
                "limit on invitations",
                "monthly invitation limit",
                "you've reached the monthly",
            ]

            for phrase in limit_phrases:
                if phrase in combined:
                    logger.warning(f"  ⚠️ LinkedIn limit warning detected: '{phrase}'")

                    # Try to dismiss any popup/dialog
                    for sel in [
                        "button[aria-label='Dismiss']",
                        "button[aria-label='Got it']",
                        "button[aria-label='Close']",
                        "button[aria-label='OK']",
                    ]:
                        try:
                            btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                            if btn.is_displayed():
                                hb.human_click(self.driver, btn)
                                hb.human_sleep(0.5, 1.0)
                                break
                        except Exception:
                            continue

                    # Also try clicking any button with dismiss-like text
                    try:
                        for btn in self.driver.find_elements(By.CSS_SELECTOR, "button"):
                            text = btn.text.strip().lower()
                            if text in ("got it", "ok", "dismiss", "close", "done"):
                                if btn.is_displayed():
                                    hb.human_click(self.driver, btn)
                                    hb.human_sleep(0.5, 1.0)
                                    break
                    except Exception:
                        logger.debug("Suppressed non-critical exception", exc_info=True)

                    # Also try dismissing inside shadow DOM
                    try:
                        self.driver.execute_script("""
                            var host = document.querySelector('#interop-outlet');
                            if (host && host.shadowRoot) {
                                var buttons = host.shadowRoot.querySelectorAll('button');
                                for (var i = 0; i < buttons.length; i++) {
                                    var text = buttons[i].textContent.trim().toLowerCase();
                                    if (text === 'got it' || text === 'ok' || text === 'dismiss' || text === 'close' || text === 'done') {
                                        buttons[i].click();
                                        return;
                                    }
                                }
                            }
                        """)
                    except Exception:
                        logger.debug("Suppressed non-critical exception", exc_info=True)

                    # Reduce remaining requests to 0 to stop the session
                    logger.warning("  ⛔ Stopping session — LinkedIn limit reached")
                    self.max_requests = self.sent_count  # Force stop
                    return True

        except Exception as e:
            logger.debug(f"Error checking limit warning: {e}")

        return False

    def _handle_invite_page(self) -> bool:
        """Handle the custom invite page."""
        hb.human_sleep(0.8, 1.5)

        # Check for limit warning on invite page too
        if self._check_limit_warning():
            return False

        try:
            # Wait for the page to have buttons
            try:
                self.wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "button")
                ))
            except TimeoutException:
                logger.warning("  ↳ Invite page loaded but no buttons found")
                return False

            if self.add_note and self.note_text:
                try:
                    add_note_btn = self.driver.find_element(
                        By.XPATH, "//button[contains(., 'Add a note')]"
                    )
                    hb.human_click(self.driver, add_note_btn)
                    hb.human_sleep(0.5, 1.0)
                    textarea = self.driver.find_element(By.CSS_SELECTOR, "textarea")
                    hb.type_like_human(self.driver, textarea, self.note_text)
                    hb.human_sleep(0.5, 1.0)
                except Exception as e:
                    logger.debug(f"Could not add note: {e}")

            return self._click_send_button()

        except Exception as e:
            logger.error(f"Error on invite page: {e}")
            return False

    def _click_send_button(self) -> bool:
        """Find and click a Send/Send without a note button.
        Works in both modals and invite pages. Uses explicit waits."""

        # Wait for any Send button (aria-label OR legacy artdeco) so we don't burn
        # the full timeout when the stale artdeco class is gone.
        try:
            self.wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, self.SEND_BUTTON_WAIT_SELECTOR)
            ))
            hb.human_sleep(0.5, 1.0)
        except TimeoutException:
            logger.debug("No Send button appeared within timeout")

        # Strategy 1 (PRIMARY): exact aria-label — robust, current-style.
        for label in self.SEND_BUTTON_ARIA_LABELS:
            try:
                btn = self.driver.find_element(
                    By.CSS_SELECTOR, f"button[aria-label='{label}']"
                )
                if btn.is_displayed() and btn.is_enabled():
                    logger.info(f"  ↳ Found button: aria-label='{label}'")
                    hb.human_click(self.driver, btn)
                    hb.human_sleep(1.5, 2.5)
                    return True
            except Exception:
                continue

        # Strategy 2: partial aria-label match.
        try:
            btns = self.driver.find_elements(
                By.CSS_SELECTOR, "button[aria-label*='Send'], button[aria-label*='send']"
            )
            for btn in btns:
                if btn.is_displayed() and btn.is_enabled():
                    label = btn.get_attribute("aria-label") or ""
                    logger.info(f"  ↳ Found button: aria-label='{label}'")
                    hb.human_click(self.driver, btn)
                    hb.human_sleep(1.5, 2.5)
                    return True
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

        # Strategy 3 (FALLBACK): legacy artdeco class — find span, click parent button.
        try:
            spans = self.driver.find_elements(By.CSS_SELECTOR, self.SEND_BUTTON_LEGACY_SELECTOR)
            for span in spans:
                if "send" in span.text.strip().lower():
                    parent_btn = span.find_element(By.XPATH, "./ancestor::button")
                    if parent_btn.is_displayed() and parent_btn.is_enabled():
                        logger.info(f"  ↳ Found artdeco button (fallback): '{span.text.strip()}'")
                        hb.human_click(self.driver, parent_btn)
                        hb.human_sleep(1.5, 2.5)
                        return True
        except Exception as e:
            logger.debug(f"artdeco-button fallback failed: {e}")

        # Strategy 3: button text match (case-insensitive)
        send_texts = {
            "send without a note", "send now", "send",
            "send invitation", "connect",
        }
        try:
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "button"):
                try:
                    text = btn.text.strip().lower()
                    if text in send_texts:
                        if btn.is_displayed() and btn.is_enabled():
                            logger.info(f"  ↳ Found button by text: '{text}'")
                            hb.human_click(self.driver, btn)
                            hb.human_sleep(1.5, 2.5)
                            return True
                except StaleElementReferenceException:
                    continue
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

        # Strategy 4: XPath text contains (catches partial matches)
        for search_text in ["Send without", "Send now", "Send"]:
            try:
                btn = self.driver.find_element(
                    By.XPATH, f"//button[contains(., '{search_text}')]"
                )
                if btn.is_displayed() and btn.is_enabled():
                    logger.info(f"  ↳ Found button via XPath: contains '{search_text}'")
                    hb.human_click(self.driver, btn)
                    hb.human_sleep(1.5, 2.5)
                    return True
            except Exception:
                continue

        # Strategy 5: JavaScript click as last resort
        try:
            clicked = self.driver.execute_script("""
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var text = buttons[i].textContent.trim().toLowerCase();
                    var label = (buttons[i].getAttribute('aria-label') || '').toLowerCase();
                    if (text.includes('send') || label.includes('send')) {
                        if (buttons[i].offsetParent !== null) {  // is visible
                            buttons[i].click();
                            return text || label;
                        }
                    }
                }
                return null;
            """)
            if clicked:
                logger.info(f"  ↳ JS-clicked button: '{clicked}'")
                hb.human_sleep(1.5, 2.5)
                return True
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

        # Debug: log what buttons ARE on the page
        try:
            all_buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            btn_info = []
            for btn in all_buttons[:10]:
                try:
                    text = btn.text.strip()[:40]
                    label = (btn.get_attribute("aria-label") or "")[:40]
                    visible = btn.is_displayed()
                    btn_info.append(f'"{text}" (aria="{label}", visible={visible})')
                except Exception:
                    continue
            logger.warning(f"  ↳ Could not find Send button. Buttons on page: {'; '.join(btn_info)}")
        except Exception:
            logger.warning("  ↳ Could not find Send button")

        return False

    def _find_modal(self) -> Optional:
        """Look for an open modal/dialog."""
        for sel in ["div.artdeco-modal", "div[role='dialog']", "div.send-invite"]:
            try:
                for m in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if m.is_displayed():
                        return m
            except Exception:
                continue
        return None

    def _handle_modal(self, modal) -> bool:
        """Handle a connection request modal."""
        logger.info("  ↳ Modal appeared")
        hb.human_sleep(0.5, 1.0)

        try:
            modal_text = modal.text.lower()

            # Check for connection limit warning in modal
            limit_phrases = ["invitation limit", "weekly", "monthly", "too many", "can't send"]
            if any(phrase in modal_text for phrase in limit_phrases):
                logger.warning("  ⚠️ Limit warning detected in modal")
                self._close_modal(modal)
                self.max_requests = self.sent_count  # Force stop
                return False

            # Email required
            if "email" in modal_text and ("enter" in modal_text or "provide" in modal_text):
                logger.warning("  ↳ Email required — skipping")
                self._close_modal(modal)
                return False

            # "How do you know" selection
            if "how do you know" in modal_text:
                for opt in modal.find_elements(By.CSS_SELECTOR, "label, button"):
                    opt_text = opt.text.strip().lower()
                    if any(kw in opt_text for kw in ["other", "colleague", "we've done business"]):
                        hb.human_click(self.driver, opt)
                        hb.human_sleep(0.5, 1.0)
                        break

            # Add note
            if self.add_note and self.note_text:
                try:
                    add_note_btn = modal.find_element(By.XPATH, ".//button[contains(., 'Add a note')]")
                    hb.human_click(self.driver, add_note_btn)
                    hb.human_sleep(0.5, 1.0)
                    textarea = modal.find_element(By.CSS_SELECTOR, "textarea")
                    hb.type_like_human(self.driver, textarea, self.note_text)
                    hb.human_sleep(0.5, 1.0)
                except Exception:
                    logger.debug("Suppressed non-critical exception", exc_info=True)

            # Click Send
            send_clicked = False
            for sel in ["button[aria-label='Send now']", "button[aria-label='Send without a note']", "button[aria-label*='Send']"]:
                try:
                    btn = modal.find_element(By.CSS_SELECTOR, sel)
                    if btn.is_displayed() and btn.is_enabled():
                        hb.human_click(self.driver, btn)
                        send_clicked = True
                        break
                except Exception:
                    continue

            if not send_clicked:
                for btn in modal.find_elements(By.CSS_SELECTOR, "button"):
                    try:
                        text = btn.text.strip().lower()
                        if text in ("send", "send now", "connect") and "add a note" not in text:
                            if btn.is_displayed() and btn.is_enabled():
                                hb.human_click(self.driver, btn)
                                send_clicked = True
                                break
                    except Exception:
                        continue

            if not send_clicked:
                logger.warning("  ↳ Could not find Send in modal")
                self._close_modal(modal)
                return False

            hb.human_sleep(1.0, 2.0)
            return True

        except Exception as e:
            logger.error(f"Error handling modal: {e}")
            self._close_modal(modal)
            return False

    def _close_modal(self, modal=None):
        """Close an open modal."""
        try:
            for sel in ["button[aria-label='Dismiss']", "button[aria-label='Close']", "button.artdeco-modal__dismiss"]:
                try:
                    btn = (modal or self.driver).find_element(By.CSS_SELECTOR, sel)
                    if btn.is_displayed():
                        hb.human_click(self.driver, btn)
                        hb.human_sleep(0.5, 1.0)
                        return
                except Exception:
                    continue
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            hb.human_sleep(0.5, 1.0)
        except Exception:
            logger.debug("Suppressed non-critical exception", exc_info=True)

    # ─── Page Processing ─────────────────────────────────────────────────

    def _collect_page_targets(self):
        """Return ``(items, extractor, source)`` for the current page.

        PRIMARY: the Connect links (link-first) with ``extract_person_from_link``.
        FALLBACK: result cards with ``extract_person_info`` (only if no Connect
        links are found, in case LinkedIn rotates the DOM again).
        """
        links = self.find_connect_links()
        if links:
            return links, self.extract_person_from_link, "connect-links"
        cards = self.find_result_cards()
        if cards:
            logger.info("No Connect links found — falling back to result cards")
            return cards, self.extract_person_info, "result-cards"
        return [], None, "none"

    def process_page(self) -> int:
        """Process all results on the current page (link-first, card fallback)."""
        self._scroll_page()

        items, extractor, source = self._collect_page_targets()
        if not items:
            # No Connect links AND no result cards — capture so we can tell an
            # empty search from a selector break (the link-first hooks going 0).
            logger.warning("No Connect links or result cards found on this page")
            capture_failure(self.driver, "no_connect_links", self.profile_name)
            return 0
        logger.info(f"Processing {len(items)} targets (via {source})")

        sent_this_page = 0
        search_url = self.driver.current_url

        for i, item in enumerate(items):
            if self.sent_count >= self.max_requests:
                logger.info(f"Reached session limit ({self.max_requests})")
                return sent_this_page

            if self.should_stop():
                return sent_this_page

            try:
                hb.scroll_to_element(self.driver, item)
                hb.simulate_reading(self.driver, 0.5, 1.5)

                info = extractor(item)
                title_preview = info['title'][:50] if info['title'] else '(no title)'
                logger.info(f"[{i+1}/{len(items)}] {info['name']} — {title_preview}")

                # Skip if already requested
                if info["profile_url"] and self.tracker.already_sent(info["profile_url"]):
                    logger.info("  ↳ Already sent request — skipping")
                    self.skipped_count += 1
                    continue

                if not info["has_connect"]:
                    logger.info("  ↳ No Connect option (Follow/Message/Pending)")
                    self.tracker.record_skip(info["name"], info["profile_url"], "no_connect")
                    self.skipped_count += 1
                    continue

                if not info["connect_element"]:
                    logger.info("  ↳ Connect text found but no clickable element")
                    self.tracker.record_skip(info["name"], info["profile_url"], "no_connect_element")
                    self.skipped_count += 1
                    continue

                hb.simulate_reading(self.driver, 1.0, 2.5)

                logger.info("  ↳ Clicking Connect...")
                if self.click_connect(info):
                    self.tracker.record_sent(info["name"], info["profile_url"], info["title"])
                    self.sent_count += 1
                    sent_this_page += 1
                    logger.info(f"  ✓ Connection request sent! ({self.sent_count}/{self.max_requests})")
                else:
                    self.tracker.record_error(info["name"], info["profile_url"], "connect_failed")
                    self.error_count += 1
                    logger.warning("  ✗ Failed to send request")
                    capture_failure(self.driver, "connect_click_failed", self.profile_name)

                # If we navigated away (invite page), return and break
                if self.driver.current_url != search_url:
                    logger.info("  ↳ Navigating back to search results...")
                    self.driver.get(search_url)
                    hb.human_sleep(4.0, 6.0)
                    self._scroll_page()
                    # Cards are stale now — break and let the outer loop re-process
                    break

                delay = hb.random_delay(3.0, 8.0)
                logger.info(f"  Waiting {delay:.0f}s...")
                time.sleep(delay)

                if hb.should_take_break(self.sent_count, threshold=4):
                    hb.take_break(self.driver)

            except StaleElementReferenceException:
                logger.debug(f"Stale element at target {i+1} — results refreshed, breaking")
                break
            except Exception as e:
                logger.error(f"Error processing target {i+1}: {e}")
                self.error_count += 1
                if self.debug:
                    import traceback
                    traceback.print_exc()
                continue

        return sent_this_page

    # ─── Main Run Loop ───────────────────────────────────────────────────

    def run(self, search_url: str, max_pages: int = 10):
        """Run the full auto-connect process.

        Returns a results dict that ALWAYS says whether the session actually
        ran. It used to return the same shape whether it sent twenty requests
        or died before the search page loaded, so a crash printed "SESSION
        COMPLETE", exited 0, and the dashboard reported the job finished.
        A failure nobody is told about is worse than a failure.
        """
        self.failure = None
        self.stage = "setup"
        try:
            self.setup()

            if self.max_requests == 0:
                logger.warning("No requests remaining this week. Exiting.")
                return self._get_results()

            self.stage = "navigate"
            self.navigate_to_search(search_url)
            self.stage = "process"

            pages = 0
            while pages < max_pages and self.sent_count < self.max_requests:
                if self.should_stop():
                    logger.info("Stopped by user request")
                    break

                logger.info(f"\n{'='*50}")
                logger.info(f"Processing page {pages + 1}")
                logger.info(f"{'='*50}")

                self.process_page()

                if self.should_stop():
                    logger.info("Stopped by user request")
                    break

                if self.sent_count >= self.max_requests:
                    logger.info("Session limit reached")
                    break

                if not self.go_to_next_page():
                    break

                pages += 1
                hb.human_sleep(2.0, 4.0)

            return self._get_results()

        except KeyboardInterrupt:
            logger.warning("\nInterrupted by user")
            self.failure = {"stage": self.stage, "type": "KeyboardInterrupt",
                            "message": "interrupted by user", "interrupted": True}
            return self._get_results()
        except Exception as e:
            import traceback
            self.failure = {
                "stage": self.stage,
                "type": type(e).__name__,
                "message": str(e).strip().split("\n")[0][:300],
                "traceback": traceback.format_exc()[-2000:],
            }
            logger.error("AUTO-CONNECTOR FAILED during %s: %s: %s",
                         self.stage, type(e).__name__, e)
            if self.debug:
                traceback.print_exc()
            return self._get_results()
        finally:
            if self.driver:
                # A crashed browser makes quit() raise too; that must not
                # replace the real error with a confusing one.
                try:
                    self.driver.quit()
                    logger.info("Browser closed")
                except Exception as exc:
                    logger.warning("Browser did not close cleanly: %s", exc)

    def _get_results(self) -> Dict:
        """Get session results summary, including whether it actually ran."""
        stats = self.tracker.get_stats() if self.tracker else {}
        failure = getattr(self, "failure", None)
        results = {
            "ok": failure is None,
            "session": {
                "sent": self.sent_count,
                "skipped": self.skipped_count,
                "errors": self.error_count,
                # pages_processed counts COMPLETED pages. It used to be
                # reported as +1, so a run that died before the first page
                # still claimed "Pages: 1".
                "pages_processed": self.pages_processed,
            },
            "weekly": stats,
            "timestamp": datetime.now().isoformat(),
        }
        if failure:
            results["failure"] = failure
            logger.error("%s", "=" * 50)
            logger.error("SESSION FAILED during %s: %s: %s",
                         failure["stage"], failure["type"], failure["message"])
            logger.error("Nothing was sent. This is NOT a completed run.")
            logger.error("%s", "=" * 50)
            return results

        logger.info(f"\n{'='*50}")
        logger.info("SESSION COMPLETE")
        logger.info(f"{'='*50}")
        logger.info(f"Sent:    {self.sent_count}")
        logger.info(f"Skipped: {self.skipped_count}")
        logger.info(f"Errors:  {self.error_count}")
        logger.info(f"Pages:   {self.pages_processed}")
        if stats:
            logger.info(f"\nToday:        {stats.get('today', 0)}/{stats.get('daily_limit', 0)}")
            logger.info(f"Weekly total: {stats.get('this_week', 0)}/{stats.get('weekly_limit', 0)}")
            logger.info(f"Remaining:    {stats.get('remaining', 0)}")

        return results


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    """CLI entry point: send connection requests from a search-results URL."""
    parser = argparse.ArgumentParser(
        description='LinkedIn Auto-Connector — send connection requests from search results',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Connect with people from a search URL
  python -m linkedin_automation.auto_connector "https://www.linkedin.com/search/results/people/?keywords=AI%20engineer"

  # Limit to 10 requests, use a specific profile
  python -m linkedin_automation.auto_connector "https://..." --max 10 --profile work

  # Add a note to each request
  python -m linkedin_automation.auto_connector "https://..." --note "Hi! I'm interested in connecting about AI."

  # Process up to 5 pages
  python -m linkedin_automation.auto_connector "https://..." --pages 5

  # Check weekly stats
  python -m linkedin_automation.auto_connector --stats --profile work
        """
    )

    parser.add_argument('search_url', nargs='?', help='LinkedIn search results URL')
    parser.add_argument('--max', type=int, default=None,
                        help='Max connection requests to send (default: connector.max_daily_requests from profile config)')
    parser.add_argument('--pages', type=int, default=10, help='Max pages to process (default: 10)')
    parser.add_argument('--profile', type=str, default=None, help='LinkedIn profile name')
    parser.add_argument('--note', type=str, default=None,
                        help='Note to add to connection requests (default: connector.note_template from profile config)')
    parser.add_argument('--stats', action='store_true', help='Show daily/weekly connection stats and exit')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.stats:
        resolved = args.profile or pm.get_default_profile_name() or "default"
        conn = pm.get_profile_config(resolved).get("connector", {})
        tracker = ConnectionTracker(
            resolved,
            weekly_limit=conn.get("weekly_limit", 100),
            daily_limit=conn.get("max_daily_requests", 25),
        )
        stats = tracker.get_stats()
        print(f"\n{'='*40}")
        print(f"Connection Stats — {resolved}")
        print(f"{'='*40}")
        print(f"Total sent (all time): {stats['total_sent']}")
        print(f"Today:                 {stats['today']}/{stats['daily_limit']}")
        print(f"This week:             {stats['this_week']}/{stats['weekly_limit']}")
        print(f"Remaining:             {stats['remaining']}")
        print(f"Skipped:               {stats['total_skipped']}")
        print(f"Errors:                {stats['total_errors']}")
        return

    if not args.search_url:
        parser.error("search_url is required (unless using --stats)")

    # max_requests/note_text default to None so the connector falls back to config.
    connector = LinkedInAutoConnector(
        profile_name=args.profile,
        max_requests=args.max,
        add_note=None,
        note_text=args.note,
        debug=args.debug
    )

    results = connector.run(args.search_url, max_pages=args.pages)

    resolved = args.profile or pm.get_default_profile_name() or "default"
    results_dir = pm.get_data_dir(resolved, "connections")
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    results_file = os.path.join(results_dir, f"session_{timestamp}.json")
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info(f"Session results saved to: {results_file}")

    # Exit non-zero on a failed session. The dashboard runs this as a
    # subprocess and decides success from the return code alone, so returning 0
    # after a crash is what made a dead browser report "Connector finished".
    failure = results.get("failure")
    if failure:
        logger.error("Exiting non-zero: the connector failed during %s (%s)",
                     failure.get("stage"), failure.get("type"))
        sys.exit(1)


if __name__ == "__main__":
    main()
