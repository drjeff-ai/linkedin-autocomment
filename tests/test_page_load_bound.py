"""Dispatch 15.4: no navigation can block for 300 seconds.

Selenium's default page-load timeout is 300s, and nothing set another, so one
stalled page could silently eat five minutes of a run (AUDIT_15A §C). Now every
driver this project builds carries pm.PAGE_LOAD_TIMEOUT_SECONDS, and the
poster turns the resulting TimeoutException into an outcome of its own.

The dangerous way to get this wrong is to let a timeout reach UNAVAILABLE.
That mark is terminal and unreviewed (MAINTENANCE §7.2): a slow network
would silently delete live posts from the queue. So the first test pins the
outcome, and the rest pin what must NOT happen alongside it.
"""

import json
import logging
import time

import pytest

from linkedin_automation import comment_fields
from linkedin_automation import comment_poster as cpm
from linkedin_automation import post_finder  # noqa: F401  (driver users import cleanly)
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm

from fake_post_page import FakePostPage

SLOW = "https://www.linkedin.com/feed/update/urn:li:activity:1011011011011011/"
FINE = "https://www.linkedin.com/feed/update/urn:li:activity:1101101101101101/"


@pytest.fixture
def env(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "failures").mkdir(parents=True)
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "t")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(data / "progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda n=None: {"behavior": {}})
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(
                            data / (subdir or "")))
    monkeypatch.setattr(pm, "login", lambda d, p: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return data


def _poster(page):
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    poster.driver = page
    return poster


# ─── the outcome ─────────────────────────────────────────────────────────────

def test_a_page_load_timeout_is_its_own_outcome_never_unavailable(env):
    poster = _poster(FakePostPage(timeout_urls={SLOW}))

    assert poster.navigate_to_post(SLOW) is False

    nav = poster.last_navigation
    assert nav["outcome"] == poster.NAV_TIMEOUT
    assert nav["outcome"] != poster.NAV_UNAVAILABLE
    assert "timed out" in nav["reason"]
    assert "limit %ss" % pm.PAGE_LOAD_TIMEOUT_SECONDS in nav["reason"]
    assert nav["elapsed"] >= 0


def test_a_timed_out_post_stays_in_the_queue(env, tmp_path, monkeypatch):
    poster = _poster(FakePostPage(timeout_urls={SLOW}))

    assert poster.post_single_comment(
        {"url": SLOW, "comment": "A point.", "preview": "p"}) is False

    progress = json.loads((env / "progress.json").read_text()) \
        if (env / "progress.json").exists() else {}
    assert SLOW not in progress.get("posted_comments", [])
    assert not progress.get("unavailable_posts")
    assert not progress.get("failed_comments")
    assert poster.last_comment_timing.outcome == "navigation_timeout"

    # And the store, reconciled from that ledger, still offers it.
    store = post_store.PostStore("t", path=str(tmp_path / "db.json"))
    store.upsert_scraped({"url": SLOW, "text": "a post"})
    assert store.mark_generated(SLOW, "A point.")
    post_store.reconcile("t", store=store)
    assert store._resolve(SLOW)["status"] == post_store.GENERATED


def test_the_log_line_names_the_timeout_and_the_elapsed(env, caplog):
    caplog.set_level(logging.WARNING)
    poster = _poster(FakePostPage(timeout_urls={SLOW}))
    poster.navigate_to_post(SLOW)
    lines = [r.getMessage() for r in caplog.records
             if "PAGE LOAD TIMEOUT" in r.getMessage()]
    assert len(lines) == 1
    assert "1011011011011011" in lines[0]
    assert "timed out after" in lines[0] and "NOT marking it gone" in lines[0]


def test_one_attempt_no_retry_loop(env):
    page = FakePostPage(timeout_urls={SLOW})
    _poster(page).navigate_to_post(SLOW)
    assert page.gets == [SLOW]


def test_the_run_continues_to_the_next_comment(env, monkeypatch, tmp_path):
    page = FakePostPage(timeout_urls={SLOW})
    monkeypatch.setattr(pm, "create_driver",
                        lambda name=None, headless=False: (page, {}))
    txt = tmp_path / "c.txt"
    txt.write_text(comment_fields.comments_to_txt(
        [{"url": u, "comment": "A point %d." % i, "post_preview": "p",
          "author": "a"} for i, u in enumerate((SLOW, FINE))], "ts"),
        encoding="utf-8")
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    result = poster.run(str(txt), post_count=2)

    assert page.gets == [SLOW, FINE]
    assert page.published == [(FINE, "A point 1.")]
    assert result["posted"] == 1
    assert result["unavailable"] == 0
    # Counted like UNCLEAR (retryable), but nothing durable was written for it.
    assert result["failed"] == 1
    progress = json.loads((env / "progress.json").read_text())
    assert progress["posted_comments"] == [FINE]
    assert not progress.get("failed_comments")
    assert not progress.get("unavailable_posts")


# ─── a normal load is unaffected ─────────────────────────────────────────────

def test_a_normal_load_is_unaffected(env, caplog):
    caplog.set_level(logging.WARNING)
    page = FakePostPage()
    poster = _poster(page)
    assert poster.navigate_to_post(FINE) is True
    assert poster.last_navigation["outcome"] == poster.NAV_OK
    assert "PAGE LOAD TIMEOUT" not in caplog.text
    assert poster.post_single_comment(
        {"url": FINE, "comment": "Fine.", "preview": "p"}) is True


# ─── every driver carries the bound ──────────────────────────────────────────

class RecordingChrome:
    instances = []

    def __init__(self, *a, **k):
        self.timeouts = []
        self.calls = []
        RecordingChrome.instances.append(self)

    def set_page_load_timeout(self, seconds):
        self.timeouts.append(seconds)

    def maximize_window(self):
        self.calls.append("maximize")

    def execute_script(self, *a):
        return None

    def get(self, url):
        self.calls.append(("get", url))

    page_source = "<html></html>"

    def quit(self):
        self.calls.append("quit")


def test_the_bound_is_one_named_value_of_30s():
    assert pm.PAGE_LOAD_TIMEOUT_SECONDS == 30


def test_create_driver_sets_the_bound(monkeypatch, tmp_path):
    RecordingChrome.instances = []
    monkeypatch.setattr(pm.webdriver, "Chrome", RecordingChrome)
    monkeypatch.setattr(pm, "Service", lambda *a, **k: None)
    monkeypatch.setattr(pm, "ChromeDriverManager",
                        lambda: type("M", (), {"install": lambda self: "x"})())
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "get_profile", lambda n: {
        "username": "u", "session_dir": str(tmp_path)})
    monkeypatch.setattr(pm, "session_exists", lambda d: True)
    monkeypatch.setattr(pm, "update_last_used", lambda n: None)

    driver, _ = pm.create_driver("t")

    assert driver.timeouts == [pm.PAGE_LOAD_TIMEOUT_SECONDS]


def test_the_article_fetcher_driver_sets_the_bound_too(monkeypatch):
    """post_generator builds its own Chrome for article pages."""
    import selenium.webdriver
    import webdriver_manager.chrome
    from selenium.webdriver.chrome import service as chrome_service
    from linkedin_automation import post_generator

    RecordingChrome.instances = []
    monkeypatch.setattr(selenium.webdriver, "Chrome", RecordingChrome)
    monkeypatch.setattr(chrome_service, "Service", lambda *a, **k: None)
    monkeypatch.setattr(
        webdriver_manager.chrome, "ChromeDriverManager",
        lambda: type("M", (), {"install": lambda self: "x"})())

    gen = post_generator.PostGenerator.__new__(post_generator.PostGenerator)
    gen._parse_html = lambda html: "parsed"
    assert gen._fetch_with_selenium("https://example.com/a") == "parsed"

    [driver] = RecordingChrome.instances
    assert driver.timeouts == [pm.PAGE_LOAD_TIMEOUT_SECONDS]
    assert "quit" in driver.calls
