"""A synthetic LinkedIn post page that drives the comment poster end to end.

Not a test module (no ``test_`` prefix). It is shared by tests that need the
WHOLE per-comment path to run - navigate, classify, like, open the composer,
type, wait for the submit to enable, click, verify - through the real
``human_behavior`` code rather than stubs, and by the Dispatch 15 dry-run
harness in ``.dev/scratch``.

Elements subclass Selenium's ``WebElement`` because ``ActionChains`` refuses
anything else; every command they would send is answered locally. Nothing here
opens a socket.

The page models the 2026-09-20 DOM facts the poster depends on:

* the Like control is ``button[aria-label='Reaction button state: Like']``;
* the composer submit is DISABLED until the editor holds text (MAINTENANCE
  §6.3), and clicking it publishes the comment into a ``-commentList``
  container and clears the editor;
* a deleted post redirects to the feed (MAINTENANCE §7).
"""

import itertools
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.remote.webelement import WebElement

from linkedin_automation.comment_poster import LinkedInCommentPoster as P

FEED_URL = "https://www.linkedin.com/feed/"

_ids = itertools.count(1)


class FakeElement(WebElement):
    def __init__(self, page, text="", tag="button", attrs=None, on_click=None,
                 ancestor_button=None):
        super().__init__(page, "fake-%d" % next(_ids))
        self.page = page
        self._text = text
        self._tag = tag
        self.attrs = dict(attrs or {})
        self._on_click = on_click
        self._ancestor_button = ancestor_button
        self.clicks = 0
        self.enabled_when = None      # callable -> bool, for the submit

    # ─── what the poster reads ──────────────────────────────────────────────
    @property
    def text(self):
        return self._text

    @property
    def tag_name(self):
        return self._tag

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_displayed(self):
        return True

    def is_enabled(self):
        if self.enabled_when is not None:
            return bool(self.enabled_when())
        return True

    @property
    def rect(self):
        return {"x": 100, "y": 200, "width": 80, "height": 24}

    @property
    def size(self):
        return {"width": 80, "height": 24}

    @property
    def location(self):
        return {"x": 100, "y": 200}

    # ─── what the poster does ───────────────────────────────────────────────
    def click(self):
        self.clicks += 1
        if self._on_click:
            self._on_click()

    def send_keys(self, *value):
        self.page.keys_into(self, "".join(value))

    def clear(self):
        self._text = ""

    def find_element(self, by, value):
        if "ancestor::button" in value and self._ancestor_button is not None:
            return self._ancestor_button
        raise LookupError("no element for %r" % value)

    def find_elements(self, by, value):
        return []


class FakePostPage:
    """One browser, any number of post URLs.

    ``gone_urls`` redirect to the feed. ``like_absent_urls`` render no Like
    control. ``render_delay`` is how long (real seconds) a submitted comment
    takes to appear in the thread. ``timeout_urls`` raise a page-load
    TimeoutException from ``get``.
    """

    def __init__(self, gone_urls=(), like_absent_urls=(), render_delay=0.0,
                 timeout_urls=(), author="Dry Run Author"):
        self.session_id = "fake-session"
        self.current_url = "about:blank"
        self.title = "LinkedIn"
        self.gone_urls = set(gone_urls)
        self.like_absent_urls = set(like_absent_urls)
        self.timeout_urls = set(timeout_urls)
        self.render_delay = render_delay
        self.author = author
        self.gets = []
        self.commands = []
        self.published = []          # (url, text) of every comment posted
        self.liked = []              # urls liked
        self.page_load_timeout = None
        self._reset_post()

    # ─── page state ─────────────────────────────────────────────────────────
    def _reset_post(self):
        self.composer_open = False
        self.thread = []             # rendered comment texts
        self._pending = []           # (visible_at, text)
        self.post_text = FakeElement(self, "A post about applied ML.", tag="span")
        self.like_button = FakeElement(
            self, "Like", attrs={"aria-label": "Reaction button state: Like"},
            on_click=self._like)
        self.comment_open_button = FakeElement(
            self, "Comment", on_click=self._open_composer)
        self.comment_span = FakeElement(
            self, "Comment", tag="span",
            ancestor_button=self.comment_open_button)
        self.box = FakeElement(
            self, "", tag="div",
            attrs={"role": "textbox", "contenteditable": "true",
                   "aria-label": "Text editor for creating comment"})
        self.submit = FakeElement(self, "Comment", on_click=self._submit)
        self.submit.enabled_when = lambda: bool(self.box._text.strip())
        self.comment_list = FakeElement(self, "", tag="div",
                                        attrs={"data-testid": "x-commentList"})

    @property
    def on_post(self):
        return self.current_url not in ("about:blank", FEED_URL)

    def _like(self):
        self.liked.append(self.current_url)
        self.like_button.attrs["aria-label"] = "Reaction button state: Liked"

    def _open_composer(self):
        self.composer_open = True

    def _submit(self):
        text = self.box._text.strip()
        if not text:
            return
        self.published.append((self.current_url, text))
        self._pending.append((time.monotonic() + self.render_delay, text))
        self.box._text = ""

    def _render(self):
        now = time.monotonic()
        still = []
        for at, text in self._pending:
            if at <= now:
                self.thread.append("%s\n%s" % (self.author, text))
            else:
                still.append((at, text))
        self._pending = still
        self.comment_list._text = "\n".join(self.thread)

    def keys_into(self, element, value):
        if "" in value:        # Keys.CONTROL (+ ENTER): keyboard submit
            if element is self.box:
                self._submit()
            return
        element._text += value

    # ─── the WebDriver surface the poster and human_behavior touch ─────────
    def set_page_load_timeout(self, seconds):
        self.page_load_timeout = seconds

    def get(self, url):
        self.gets.append(url)
        if url in self.timeout_urls:
            raise TimeoutException("timeout: Timed out receiving message "
                                   "from renderer: %.3f" % (
                                       self.page_load_timeout or 300))
        self._reset_post()
        self.current_url = FEED_URL if url in self.gone_urls else url

    def find_elements(self, by, selector):
        self._render()
        if not self.on_post:
            return []
        if selector == P.POST_DETAIL_SELECTORS[0]:
            return [self.post_text]
        if selector == P.LIKE_BUTTON_SELECTORS[0]:
            if self.current_url in self.like_absent_urls:
                return []
            if self.like_button.attrs["aria-label"].endswith("Like"):
                return [self.like_button]
            return []
        if selector == P.COMMENT_BUTTON_TEXT_SELECTOR:
            return [self.comment_span]
        if selector == "div[role='textbox'][contenteditable='true']":
            return [self.box] if self.composer_open else []
        if selector == P.POSTED_COMMENT_CONTAINER_SELECTOR:
            return [self.comment_list] if self.thread else []
        return []

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise LookupError(selector)
        return found[0]

    def execute_script(self, script, *args):
        if script == P._FIND_COMPOSER_SUBMIT_JS:
            return self.submit if self.composer_open else None
        if "getBoundingClientRect" in script:
            return {"x": 100, "y": 200, "width": 80, "height": 24}
        if "selectAll" in script:
            if args and args[0] is self.box:
                self.box._text = ""
            return None
        if "arguments[0].click()" in script and args:
            args[0].click()
            return None
        return None

    def execute(self, command, params=None):
        # ActionChains.perform lands here (W3C actions). Nothing to do.
        self.commands.append(command)
        return {"value": None}

    def save_screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"\x89PNG fake")
        return True

    @property
    def page_source(self):
        return "<html><body><main>fake post page</main></body></html>"

    def quit(self):
        self.commands.append("quit")
