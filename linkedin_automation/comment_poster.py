"""Post curated comments to LinkedIn posts via Selenium.

Parses a daily/curated comments TXT file (see ``comment_fields``), then for each
comment navigates to the post, likes it, and submits the comment. Failures on a
single comment are skipped-and-logged so the run continues. Exits 0 on success,
2 on login failure, 1 on other errors.
"""

import time
import json
import os
import re
import sys
import random
import traceback
from datetime import datetime
from typing import Dict, List, Optional
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from dotenv import load_dotenv
import logging
from . import profile_manager as pm
from . import human_behavior as hb
from .failure_capture import capture_failure

load_dotenv()


class LinkedInCommentPoster:
    """Post generated comments to LinkedIn posts."""

    # ─── Posting-path selectors ───────────────────────────────────────────────
    #
    # These were inline literals inside the methods below. That is exactly why
    # selector_health could not cover the posting path: its SELECTOR_REGISTRY is
    # built from class constants so it stays in sync with what the code actually
    # uses, and a literal buried in a method body is invisible to it. Scraping
    # selectors were hoisted onto LinkedInScraper long ago and were therefore
    # watched; these were not, so the posting path had no monitor at all.
    #
    # Keep the new-first / legacy-fallback ordering: a stale selector that is
    # tried first costs a full WebDriverWait timeout per post.

    # Selectors that prove a post permalink page has actually rendered.
    #
    # LinkedIn moved the permalink page to data-testid attributes. post_finder
    # was updated for that; this list was not, so all four legacy selectors
    # stopped matching and every post failed with "Post content not found on
    # page" after burning 4 x 20s of WebDriverWait — scraping kept working while
    # posting placed zero comments. Counts verified against a live permalink
    # page on 2026-07-30 (see .dev/AUDIT_student_fork.md):
    #
    #     span[data-testid='expandable-text-box'] -> 1     div.occludable-update      -> 0
    #     div[role='listitem']                    -> 1     div.feed-shared-update-v2  -> 0
    #                                                      article.feed-shared-article-> 0
    #                                                      div[data-urn*='activity']  -> 0
    #
    # Deliberately NOT using "main": it matches on every LinkedIn page including
    # error pages, so it would report a post had loaded when nothing had.
    POST_DETAIL_SELECTORS = [
        "span[data-testid='expandable-text-box']",
        "div[role='listitem']",
        "div.occludable-update",
        "div.feed-shared-update-v2",
        "article.feed-shared-article",
        "div[data-urn*='activity']",
    ]

    LIKE_BUTTON_SELECTORS = [
        "button[aria-label*='Like'][aria-pressed='false']",
        "button.react-button__trigger:not(.react-button__trigger--active)",
        "button[data-control-name='like_toggle']",
    ]

    LIKED_STATE_SELECTORS = [
        "button[aria-label*='Like'][aria-pressed='true']",
        "button.react-button__trigger--active",
    ]

    # The action-bar button that OPENS the comment box. Distinct from the submit
    # button below — see SUBMIT_BUTTON_XPATH for why that distinction matters.
    COMMENT_BUTTON_LABEL_SELECTORS = [
        "button[aria-label*='Comment']",
        "button[aria-label*='comment']",
    ]
    COMMENT_BUTTON_TEXT_SELECTOR = "span.artdeco-button__text"
    COMMENT_BUTTON_TEXT = "Comment"

    # The editor itself. Only present after the comment box has been opened.
    COMMENT_INPUT_SELECTORS = [
        "div.ql-editor[contenteditable='true']",
        "div.ql-editor.ql-blank",
        "div[role='textbox'][contenteditable='true']",
        "div[contenteditable='true'][data-placeholder*='comment']",
    ]

    # The SUBMIT button. LinkedIn labels it with the visible text "Comment" and
    # no aria-label, while the action-bar button that OPENS the box carries
    # aria-label="Comment" and shows the comment count as its text. Matching on
    # exact visible text and excluding that aria-label is what separates them.
    SUBMIT_BUTTON_XPATH = "//button[normalize-space(.)='Comment']"
    SUBMIT_BUTTON_EXCLUDED_ARIA_LABEL = "Comment"

    # Class-based submit fallbacks, kept because they still match on some
    # accounts. Fragile by nature: LinkedIn ships hashed class names that change
    # between deploys, which is why the text-based XPath above is primary.
    SUBMIT_BUTTON_FALLBACK_SELECTORS = [
        "button.comments-comment-box__submit-button--cr",
        "button.comments-comment-box__submit-button",
        "button.ml1.artdeco-button--primary",
        "button.artdeco-button.artdeco-button--1.artdeco-button--primary",
        "button[aria-label*='Post comment']",
    ]

    # Posted comments on the permalink page, used to verify a submit landed.
    POSTED_COMMENT_SELECTOR = "div.comments-comment-item"

    def __init__(self, profile_name=None):
        self.profile_name = profile_name
        self.profile = None  # Set during setup_driver
        
        self.driver = None
        self.wait = None
        
        # Resolve profile name for data dirs
        resolved_name = profile_name or pm.get_default_profile_name() or "default"

        # Setup directories (profile-specific)
        self.data_dir = pm.get_comments_dir(resolved_name)
        self.progress_file = pm.get_progress_file(resolved_name)
        self.screenshots_dir = pm.get_screenshots_dir(resolved_name)

        # Apply tunable human-behavior timing (typing speed, reading time, scroll/
        # break cadence) from the profile config's "behavior" section. Posting
        # comments is the highest bot-detection risk, so pacing matters most here.
        self.config = pm.get_profile_config(resolved_name)
        hb.configure_behavior(self.config.get("behavior"))
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        # Load progress
        self.progress = self.load_progress()
    
    def load_progress(self) -> Dict:
        """Load posting progress from file."""
        if os.path.exists(self.progress_file):
            with open(self.progress_file, 'r') as f:
                return json.load(f)
        return {"posted_comments": []}
    
    def save_progress(self):
        """Save posting progress to file."""
        with open(self.progress_file, 'w') as f:
            json.dump(self.progress, f, indent=2)
    
    def setup_driver(self):
        """Initialize Chrome driver with persistent session via profile manager."""
        self.logger.info("Setting up browser with persistent session...")
        self.driver, self.profile = pm.create_driver(self.profile_name)
        self.wait = WebDriverWait(self.driver, 20)
        self.logger.info("Browser started successfully")
    
    def login(self) -> bool:
        """Login to LinkedIn using profile manager (checks persistent session first)."""
        return pm.login(self.driver, self.profile)
    
    @staticmethod
    def parse_comments_file(file_path: str) -> List[Dict]:
        """Parse the daily/curated comments text file into a list of dicts.

        Pure function (no driver/session needed). Accepts the canonical TXT
        format produced by ``comment_fields.comments_to_txt`` regardless of which
        field-naming convention the upstream comments used — the field mapping is
        resolved at write time, so every block here carries a real ``URL:`` line.
        """
        comments = []

        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Split by post separator
        posts = content.split('------------------------------------------------------------')

        for post in posts:
            if 'URL:' not in post or 'Your Comment' not in post:
                continue

            # Extract URL (accept http or https)
            url_match = re.search(r'URL: (https?://[^\n]+)', post)
            if not url_match:
                continue
            
            url = url_match.group(1).strip()
            
            # Extract comment. The block was already split off the separator, so
            # the comment's closing quote(s) are the last quote-run in the chunk,
            # followed only by whitespace. Anchoring the closing "+ to end-of-chunk
            # lets the comment contain its own double quotes while still stripping
            # both "text" and ""text"" wrappers.
            comment_match = re.search(
                r'Your Comment \(\d+ words\):\n"+(.+?)"+\s*$', post, re.DOTALL
            )
            
            if not comment_match:
                continue
            
            comment_text = comment_match.group(1).strip()
            
            # Extract post preview
            preview_match = re.search(r'Post Preview:\n([^\n]+)', post)
            preview = preview_match.group(1) if preview_match else "No preview"
            
            comments.append({
                'url': url,
                'comment': comment_text,
                'preview': preview
            })
        
        return comments
    
    def navigate_to_post(self, url: str) -> bool:
        """Navigate to a LinkedIn post with improved waiting."""
        try:
            self.logger.info(f"Navigating to post: {url}")
            self.driver.get(url)

            # Wait for the page to fully load (variable, not a flat 3s)
            hb.human_sleep(2.5, 4.0)

            # Wait for post content to be visible
            post_selectors = self.POST_DETAIL_SELECTORS

            post_found = False
            for selector in post_selectors:
                try:
                    self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                    post_found = True
                    self.logger.info(f"Post loaded with selector: {selector}")
                    break
                except TimeoutException:
                    continue
            
            if not post_found:
                self.logger.error("Post content not found on page")
                return False
            
            # Additional wait for dynamic content
            hb.human_sleep(1.5, 2.5)

            # Read the post like a human would before engaging, then a gentle
            # scroll down to take it in and back up to the action bar (variable
            # increments + drift instead of a uniform jump to the half-point).
            hb.simulate_reading(self.driver)
            hb.human_scroll(self.driver, direction="down", pixels=random.randint(300, 600))
            hb.human_sleep(0.8, 1.6)
            hb.human_scroll(self.driver, direction="up", pixels=random.randint(300, 600))
            hb.human_sleep(0.6, 1.2)

            # Check if we're on the right page
            if "feed/update" in self.driver.current_url or "posts" in self.driver.current_url:
                self.logger.info("Successfully navigated to post")
                return True
            
            self.logger.error("Failed to navigate to post - wrong URL")
            return False
            
        except Exception as e:
            self.logger.error(f"Navigation error: {e}")
            return False
    
    def like_post(self) -> bool:
        """Like the current post."""
        try:
            # Find the like button - multiple possible selectors
            like_selectors = self.LIKE_BUTTON_SELECTORS

            like_button = None
            for selector in like_selectors:
                try:
                    like_button = self.wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                    break
                except Exception:
                    continue

            if not like_button:
                # Check if already liked
                liked_selectors = self.LIKED_STATE_SELECTORS

                for selector in liked_selectors:
                    if self.driver.find_elements(By.CSS_SELECTOR, selector):
                        self.logger.info("Post already liked")
                        return True
                
                self.logger.warning("Like button not found")
                return False
            
            # Click like button with a natural mouse approach + click.
            hb.human_click(self.driver, like_button)
            hb.human_sleep(1.2, 2.4)

            self.logger.info("✅ Post liked successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error liking post: {e}")
            return False
    
    def open_comment_box(self) -> Optional:
        """Open the comment box and return the input element."""
        self.logger.info("Opening comment box...")
        
        # Find comment button - look for button with "Comment" text
        comment_button = None
        
        # Method 1: Find by span text
        try:
            spans = self.driver.find_elements(
                By.CSS_SELECTOR, self.COMMENT_BUTTON_TEXT_SELECTOR)
            for span in spans:
                if span.text.strip() == self.COMMENT_BUTTON_TEXT:
                    # Get the parent button
                    comment_button = span.find_element(By.XPATH, "./ancestor::button")
                    break
        except Exception:
            self.logger.debug("Comment-button span lookup failed; trying aria-label", exc_info=True)

        # Method 2: Try aria-label
        if not comment_button:
            comment_button_selectors = self.COMMENT_BUTTON_LABEL_SELECTORS

            for selector in comment_button_selectors:
                try:
                    buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for btn in buttons:
                        if btn.is_displayed():
                            comment_button = btn
                            break
                    if comment_button:
                        break
                except Exception:
                    continue
        
        if comment_button:
            hb.scroll_to_element(self.driver, comment_button)
            hb.human_click(self.driver, comment_button)
            hb.human_sleep(2.5, 3.5)  # Give time for comment box to fully load
            self.logger.info("Clicked comment button")
        else:
            self.logger.info("Comment button not found, trying to find comment box directly")
        
        # Find the comment input field
        comment_input_selectors = self.COMMENT_INPUT_SELECTORS

        comment_input = None
        for selector in comment_input_selectors:
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            for elem in elements:
                if elem.is_displayed():
                    comment_input = elem
                    self.logger.info(f"Found comment input with selector: {selector}")
                    break
            if comment_input:
                break
        
        return comment_input
    
    def verify_comment_posted(self, comment_input, comment_text):
        """Verify if comment was actually posted."""
        try:
            # Method 1: Check if input is cleared
            try:
                current_text = comment_input.text.strip()
                if current_text != comment_text:
                    self.logger.info("✅ Comment input changed/cleared")
                    return True
            except Exception:
                # Element might be stale (good sign)
                self.logger.info("✅ Comment input element changed (likely posted)")
                return True
            
            # Method 2: Check for comment in the list
            hb.human_sleep(1.5, 2.5)
            comments = self.driver.find_elements(
                By.CSS_SELECTOR, self.POSTED_COMMENT_SELECTOR)
            for comment in comments[-5:]:  # Check last 5 comments
                if comment_text[:50] in comment.text:
                    self.logger.info("✅ Comment found in comment list!")
                    return True
            
            # Ambiguous: the input didn't clear and the comment isn't in the list —
            # could be a silent submit failure or an already-commented edge case.
            # Capture so the state can be inspected.
            self.logger.warning("❌ Comment not verified as posted")
            capture_failure(self.driver, "comment_unverified", self.profile_name)
            return False
            
        except Exception as e:
            self.logger.error(f"Verification error: {e}")
            return False
    
    def post_comment_method1(self, comment_input, comment_text):
        """Method 1: Direct selector from debug script."""
        primary = self.SUBMIT_BUTTON_FALLBACK_SELECTORS[0]
        self.logger.info(f"Trying Method 1: Direct selector ({primary})")
        try:
            post_button = self.driver.find_element(By.CSS_SELECTOR, primary)
            self.logger.info(f"Found submit button: '{post_button.text}'")
            hb.human_click(self.driver, post_button)
            hb.human_sleep(2.5, 3.5)
            
            if self.verify_comment_posted(comment_input, comment_text):
                self.logger.info("✅ Comment posted using Method 1!")
                return True
        except Exception as e:
            self.logger.debug(f"Method 1 failed: {e}")
        return False
    
    def post_comment_method5(self, comment_input, comment_text):
        """Method 5: JavaScript approach from debug script."""
        self.logger.info("Trying Method 5: JavaScript approach")
        try:
            script = """
            console.log('Starting button search...');
            const buttons = document.querySelectorAll('button');
            let foundButtons = [];
            
            for (let btn of buttons) {
                if (btn.offsetParent !== null && !btn.disabled) {
                    const text = btn.textContent.trim();
                    const classes = btn.className;
                    const aria = btn.getAttribute('aria-label') || '';
                    
                    foundButtons.push({
                        text: text,
                        classes: classes,
                        aria: aria
                    });
                    
                    // Check various conditions
                    if (classes.includes('comments-comment-box__submit-button') ||
                        (classes.includes('artdeco-button--primary') && text !== 'Comment') ||
                        aria.toLowerCase().includes('post comment')) {
                        
                        console.log('Clicking button:', text || 'Submit');
                        btn.click();
                        return {clicked: true, button: text || 'Submit'};
                    }
                }
            }
            
            return {clicked: false, buttons_found: foundButtons};
            """
            
            result_js = self.driver.execute_script(script)
            self.logger.info(f"JavaScript result: {result_js}")
            
            if result_js and result_js.get('clicked'):
                hb.human_sleep(2.5, 3.5)
                if self.verify_comment_posted(comment_input, comment_text):
                    self.logger.info("✅ Comment posted using Method 5!")
                    return True
        except Exception as e:
            self.logger.debug(f"Method 5 failed: {e}")
        return False
    
    def post_comment_ctrl_enter(self, comment_input, comment_text):
        """Try Ctrl+Enter to post comment."""
        self.logger.info("Trying Ctrl+Enter shortcut...")
        try:
            # Brief pause before the keyboard submit (a human doesn't fire the
            # shortcut the instant typing ends).
            hb.human_sleep(0.4, 1.0)
            comment_input.send_keys(Keys.CONTROL + Keys.ENTER)
            hb.human_sleep(2.5, 3.5)
            
            if self.verify_comment_posted(comment_input, comment_text):
                self.logger.info("✅ Comment posted using Ctrl+Enter!")
                return True
        except Exception as e:
            self.logger.debug(f"Ctrl+Enter failed: {e}")
        return False
    
    def post_comment_alternative_selectors(self, comment_input, comment_text):
        """Try alternative button selectors."""
        self.logger.info("Trying alternative selectors...")
        
        selectors = self.SUBMIT_BUTTON_FALLBACK_SELECTORS

        for selector in selectors:
            try:
                buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for button in buttons:
                    if button.is_displayed() and button.is_enabled():
                        btn_text = button.text.strip()
                        if btn_text != "Comment":  # Skip comment opener
                            self.logger.info(f"Found button with selector {selector}: '{btn_text}'")
                            hb.human_click(self.driver, button)
                            hb.human_sleep(2.5, 3.5)
                            
                            if self.verify_comment_posted(comment_input, comment_text):
                                self.logger.info(f"✅ Comment posted using selector: {selector}")
                                return True
            except Exception as e:
                self.logger.debug(f"Selector {selector} failed: {e}")
        
        return False
    
    def post_comment(self, comment_text: str) -> bool:
        """Post a comment on the current post using all available methods."""
        try:
            # Open comment box
            comment_input = self.open_comment_box()
            
            if not comment_input:
                self.logger.error("Comment input field not found")
                capture_failure(self.driver, "comment_box_not_found", self.profile_name)
                return False
            
            # Click to focus and type comment. Scroll the box into view with
            # human-like smoothness, then a natural mouse approach + click to
            # focus it (instead of a teleport click).
            hb.scroll_to_element(self.driver, comment_input)

            # Variable pause between reading the post and starting to type — a
            # human composes for a beat before the first keystroke.
            hb.human_sleep(0.8, 2.0)

            # Clear, then type character-by-character with human-like timing
            # (random 40-120ms keystrokes + occasional thinking pauses + mouse
            # drift) via human_behavior_selenium, instead of dumping all at once.
            hb.human_click(self.driver, comment_input)
            hb.human_sleep(0.3, 0.7)
            comment_input.clear()
            self.logger.info("Cleared comment input field")
            try:
                hb.type_like_human(self.driver, comment_input, comment_text)
            except Exception as e:
                # Fall back to plain typing if the human-typing helper fails
                # (e.g. mouse-move issues), so a comment is still entered.
                self.logger.warning(f"Human typing failed ({e}); falling back to send_keys")
                comment_input.send_keys(comment_text)
            self.logger.info(f"Typed comment ({len(comment_text)} chars) with human-like timing")
            # Variable pause to "review" between typing and submitting.
            hb.human_sleep(1.0, 2.5)
            
            # Try all posting methods in order of success rate
            posting_methods = [
                self.post_comment_method1,  # Direct selector that works in debug
                self.post_comment_method5,  # JavaScript method that works in debug
                self.post_comment_ctrl_enter,  # Keyboard shortcut
                self.post_comment_alternative_selectors  # Alternative selectors
            ]
            
            for method in posting_methods:
                if method(comment_input, comment_text):
                    return True
            
            # If all submit methods fail, capture the page state (screenshot + DOM).
            capture_failure(self.driver, "comment_submit_failed", self.profile_name)
            self.logger.error("All posting methods failed.")
            
            # Log all visible buttons for debugging
            self.logger.info("Debugging - All visible buttons:")
            all_buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for i, btn in enumerate(all_buttons):
                if btn.is_displayed():
                    self.logger.info(f"Button {i}: text='{btn.text}', class='{btn.get_attribute('class')[:100]}'")
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error posting comment: {e}")
            traceback.print_exc()
            return False
    
    def post_single_comment(self, comment_data: Dict, force: bool = False) -> bool:
        """Post a single comment to LinkedIn."""
        url = comment_data['url']
        comment_text = comment_data['comment']
        
        # Check if already posted (skip check if force=True)
        if not force and url in self.progress.get('posted_comments', []):
            self.logger.info(f"Comment already posted for: {url}")
            return True
        
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"Processing comment for post: {comment_data['preview'][:80]}...")
        self.logger.info(f"Comment: {comment_text[:100]}...")
        
        # Navigate to post
        if not self.navigate_to_post(url):
            return False

        # Read the post before engaging, scaled to the comment we intend to leave
        # (longer/considered comments imply a longer read of the post).
        hb.simulate_reading_for_text(self.driver, comment_text)

        # Like the post
        if not self.like_post():
            self.logger.warning("Failed to like post, continuing anyway...")

        # Variable pause between liking and opening the comment box.
        hb.human_sleep(0.8, 2.2)

        # Post the comment
        if not self.post_comment(comment_text):
            return False
        
        # Mark as completed
        self.progress['posted_comments'].append(url)
        self.progress['last_posted'] = datetime.now().isoformat()
        self.save_progress()
        
        self.logger.info("✅ Successfully engaged with post!")

        # Wait before next action (variable, not a flat 5s)
        hb.human_sleep(4.0, 7.0)

        return True
    
    def run(self, comments_file: str, post_count: int = 1, manual_mode: bool = False):
        """Run the comment posting process."""
        # Parse comments
        comments = self.parse_comments_file(comments_file)
        
        if not comments:
            self.logger.error("No comments found in file")
            return
        
        self.logger.info(f"Found {len(comments)} comments to post")
        
        # Setup and login
        self.setup_driver()
        if not self.login():
            if self.driver:
                self.driver.quit()
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile "
                f"'{self.profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile "
                f"{self.profile_name or 'default'}"
            )
        
        try:
            # Post comments. Each comment is isolated: a failure on one is
            # logged as a skip and the run continues with the rest (skip-and-log,
            # not abort-the-whole-run).
            posted = 0
            skipped = 0
            total = len(comments)

            # Non-uniform session shape: take a longer break after a re-rolled
            # number of comments so the work-then-pause rhythm is never identical
            # between runs (mirrors the connector's natural break pattern).
            posts_since_break = 0
            break_threshold = hb.random_break_threshold()

            for i, comment in enumerate(comments):
                label = f"{i+1}/{total}"

                if posted >= post_count:
                    break

                url = comment.get('url', '')
                if not url or not url.lower().startswith('http'):
                    self.logger.warning(f"Skipping comment {label}: missing or invalid URL ({url!r})")
                    skipped += 1
                    continue

                if url in self.progress.get('posted_comments', []):
                    self.logger.info(f"Skipping comment {label}: already posted")
                    continue

                try:
                    if manual_mode:
                        self.logger.info("\n" + "="*60)
                        self.logger.info("MANUAL MODE - Please complete the following steps:")
                        self.logger.info(f"1. Navigate to: {comment['url']}")
                        self.logger.info("2. Like the post")
                        self.logger.info("3. Click the comment button")
                        self.logger.info(f"4. Type: {comment['comment'][:100]}...")
                        self.logger.info("5. Click the Post/Submit button")
                        input("\nPress Enter when you've completed these steps...")

                        # Mark as completed
                        self.progress['posted_comments'].append(comment['url'])
                        self.progress['last_posted'] = datetime.now().isoformat()
                        self.save_progress()
                        posted += 1

                    elif self.post_single_comment(comment):
                        posted += 1
                    else:
                        self.logger.warning(f"Skipping comment {label}: post attempt did not succeed")
                        skipped += 1
                        continue

                except Exception as e:
                    # One comment failing must not stop the others.
                    self.logger.warning(f"Skipping comment {label}: {e}", exc_info=True)
                    skipped += 1
                    continue

                # Wait between successful posts to avoid rate limiting. Vary the
                # gap widely (never a constant 30s) so the cadence isn't uniform.
                if posted < post_count and i < total - 1:
                    wait_time = hb.random_delay(22.0, 48.0)
                    self.logger.info(f"Waiting {wait_time:.0f}s before next post...")
                    time.sleep(wait_time)

                    # Longer break after a re-rolled number of comments.
                    posts_since_break += 1
                    if posts_since_break >= break_threshold:
                        self.logger.info(
                            f"Taking a natural break after {posts_since_break} comments...")
                        hb.take_break(self.driver)
                        posts_since_break = 0
                        break_threshold = hb.random_break_threshold()

            self.logger.info(f"\n✅ Done. Posted {posted}, skipped {skipped}, of {total} parsed.")
            return {"posted": posted, "skipped": skipped, "total": total}

        finally:
            if self.driver:
                if not manual_mode:
                    self.driver.quit()
                    self.logger.info("Browser closed")
                else:
                    self.logger.info("Browser left open for manual inspection")
                    input("Press Enter to close browser...")
                    self.driver.quit()


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Post generated comments to LinkedIn')
    parser.add_argument('comments_file', nargs='?', help='Path to the daily comments file')
    parser.add_argument('--count', type=int, default=1, help='Number of comments to post (default: 1)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--manual', action='store_true', help='Manual mode - shows instructions instead of automating')
    parser.add_argument('--test-url', help='Test with a specific LinkedIn post URL')
    parser.add_argument('--test-comment', default='This is a test comment.', help='Comment text for test mode')
    parser.add_argument('--profile', type=str, default=None, help='LinkedIn profile name (uses default if omitted)')
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    poster = LinkedInCommentPoster(profile_name=args.profile)
    
    # Test mode
    if args.test_url:
        print(f"Running in test mode with URL: {args.test_url}")
        test_comment = {
            'url': args.test_url,
            'comment': args.test_comment,
            'preview': 'Test mode'
        }
        
        poster.setup_driver()
        if poster.login():
            success = poster.post_single_comment(test_comment, force=True)  # Force posting in test mode
            print(f"Test result: {'Success' if success else 'Failed'}")
            input("\nPress Enter to close browser...")
            poster.driver.quit()
            sys.exit(pm.EXIT_OK if success else pm.EXIT_ERROR)
        poster.driver.quit()
        sys.exit(pm.EXIT_LOGIN_REQUIRED)

    # Normal mode
    if not args.comments_file:
        parser.error("comments_file is required unless using --test-url")
        return

    if not os.path.exists(args.comments_file):
        print(f"Error: Comments file not found: {args.comments_file}")
        sys.exit(pm.EXIT_ERROR)

    try:
        poster.run(args.comments_file, args.count, manual_mode=args.manual)
        sys.exit(pm.EXIT_OK)
    except pm.LoginRequiredError as e:
        print(f"\n❌ {e}")
        sys.exit(pm.EXIT_LOGIN_REQUIRED)
    except Exception as e:
        print(f"\n❌ {e}")
        sys.exit(pm.EXIT_ERROR)


if __name__ == "__main__":
    main()