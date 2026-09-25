"""A post that is GONE must cost seconds, be terminal, and not be a failure.

Three separate claims, and they fail in different directions:

  * Before this, a deleted post ran all six POST_DETAIL_SELECTORS through a
    20-second WebDriverWait - TWO MINUTES - and then returned a bare False.
  * Nothing marked it, so its record stayed GENERATED and the same two minutes
    were spent again on every subsequent run, forever.
  * The run loop counted it as a FAILURE, which is the count that is supposed
    to mean "a comment we wrote did not go out; go and look at it".

The hard part is not detecting a gone post. It is REFUSING to detect one: an
UNAVAILABLE mark is permanent and unreviewed, so a false positive silently
deletes a real post from the queue. Most of the tests below are about the
cases where the code must decline to decide.
"""
import json

import pytest

from linkedin_automation import comment_poster as cpm
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm


POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1111111111111111/"
OTHER_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1111111111110000/"


# ─── a driver that serves one page ───────────────────────────────────────────

class FakeEl:
    def __init__(self, text="", displayed=True):
        self._text = text
        self._displayed = displayed

    @property
    def text(self):
        return self._text

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return True


class PageDriver:
    """A page described as {css selector: [elements]}, plus a current URL."""

    def __init__(self, url, elements=None):
        self.current_url = url
        self.elements = elements or {}
        self.page_source = "<html></html>"
        self.title = "page"
        self.gets = []

    def get(self, url):
        self.gets.append(url)

    def find_elements(self, by, selector):
        return self.elements.get(selector, [])

    def save_screenshot(self, path):
        return False


@pytest.fixture
def poster(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "default")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n: str(tmp_path / "progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n: str(tmp_path))
    monkeypatch.setattr(pm, "get_profile_config", lambda n=None: {"behavior": {}})
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(
                            tmp_path / (subdir or "")))
    monkeypatch.setattr(cpm.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cpm.hb, "simulate_reading", lambda d: None)
    monkeypatch.setattr(cpm.hb, "human_scroll", lambda *a, **k: None)
    monkeypatch.setattr(cpm.time, "sleep", lambda s: None)

    p = cpm.LinkedInCommentPoster(profile_name="default")
    p.NAV_DECIDE_SECONDS = 0.3
    p.NAV_POLL_SECONDS = 0.01
    return p


# ─── STRONG: the post is gone ────────────────────────────────────────────────

def test_a_redirect_to_the_feed_is_a_gone_post(poster):
    """LinkedIn does not 404 a deleted post; it bounces you to the feed."""
    poster.driver = PageDriver("https://www.linkedin.com/feed/")
    assert poster.navigate_to_post(POST_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNAVAILABLE


def test_the_feeds_own_listitems_do_not_read_as_the_post_loading(poster):
    """THE TRAP.

    `div[role='listitem']` is in POST_DETAIL_SELECTORS, and the feed you get
    bounced to is FULL of them. Check content before redirect and every
    deleted post looks like a successful navigation - which is how these got
    all the way to the composer and wasted a comment on the wrong thread.
    """
    poster.driver = PageDriver(
        "https://www.linkedin.com/feed/",
        {"div[role='listitem']": [FakeEl("somebody else's post")]})
    assert poster.navigate_to_post(POST_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNAVAILABLE


def test_an_explicit_removed_marker_is_a_gone_post(poster):
    poster.driver = PageDriver(
        POST_URL, {".artdeco-empty-state": [FakeEl("empty")]})
    assert poster.navigate_to_post(POST_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNAVAILABLE


def test_the_removed_text_is_read_from_the_page_not_guessed_at(poster):
    poster.driver = PageDriver(
        POST_URL,
        {"main": [FakeEl("This post is no longer available.")]})
    assert poster.navigate_to_post(POST_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNAVAILABLE
    assert "no longer available" in poster.last_navigation["reason"]


# ─── the refusals: everything that must NOT be marked terminal ───────────────

def test_a_login_wall_is_never_a_gone_post(poster):
    """THE EXPENSIVE FALSE POSITIVE.

    One expired cookie redirects every post to the auth wall. Treating that
    as "the post is gone" would terminally delete the entire queue in a single
    run, with no way to tell afterwards which posts were real.
    """
    poster.driver = PageDriver("https://www.linkedin.com/login?session_redirect=x")
    assert poster.navigate_to_post(POST_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNCLEAR


@pytest.mark.parametrize("wall", [
    "https://www.linkedin.com/checkpoint/challenge/",
    "https://www.linkedin.com/authwall?trk=x",
    "https://www.linkedin.com/uas/login",
])
def test_no_auth_wall_is_a_gone_post(poster, wall):
    poster.driver = PageDriver(wall)
    poster.navigate_to_post(POST_URL)
    assert poster.last_navigation["outcome"] == poster.NAV_UNCLEAR


def test_content_that_simply_never_loads_is_not_a_gone_post(poster):
    """Slow page, flaky network, a selector that just died: all unknown.

    Unknown is retryable. Only a positive signal may be terminal - the same
    discipline the comment verifier is built on.
    """
    poster.driver = PageDriver(POST_URL)
    assert poster.navigate_to_post(POST_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNCLEAR


def test_a_normalised_url_is_not_a_redirect(poster):
    """LinkedIn rewrites /posts/<slug>-activity-<id>-xx to /feed/update/....

    Same post, different URL. Comparing URLs rather than activity ids would
    call every single post gone.
    """
    asked = ("https://www.linkedin.com/posts/some-slug_topic-"
             "activity-1111111111111111-AbCd")
    poster.driver = PageDriver(
        POST_URL, {"div[role='listitem']": [FakeEl("the post")]})
    assert poster.navigate_to_post(asked) is True
    assert poster.last_navigation["outcome"] == poster.NAV_OK


def test_a_url_with_no_activity_id_never_triggers_the_redirect_check(poster):
    """Nothing to key on means no opinion, not a guess."""
    poster.driver = PageDriver("https://www.linkedin.com/feed/")
    assert poster.navigated_away_from("https://example.com/post") is None


def test_the_marker_check_only_runs_when_no_post_content_was_found(poster):
    """`.artdeco-empty-state` is a generic LinkedIn empty state - an empty
    "no reactions yet" panel on a perfectly live post would carry it too."""
    poster.driver = PageDriver(POST_URL, {
        "div[role='listitem']": [FakeEl("the post, present and correct")],
        ".artdeco-empty-state": [FakeEl("no reactions yet")],
    })
    assert poster.navigate_to_post(POST_URL) is True
    assert poster.last_navigation["outcome"] == poster.NAV_OK


# ─── the bound ───────────────────────────────────────────────────────────────

def test_a_gone_post_is_decided_within_the_budget(poster, monkeypatch):
    """It used to be 6 selectors x a 20-second wait. Two minutes, every run."""
    import time as _t
    monkeypatch.setattr(cpm.time, "sleep", _t.sleep)
    poster.NAV_DECIDE_SECONDS = 0.4
    poster.NAV_POLL_SECONDS = 0.05
    poster.driver = PageDriver(POST_URL)

    started = _t.time()
    poster.navigate_to_post(POST_URL)
    elapsed = _t.time() - started
    assert elapsed < 2.0, "%.1fs - the bound is not holding" % elapsed


def test_the_budget_is_total_not_per_selector(poster, monkeypatch):
    """Adding a seventh selector must not add a seventh timeout.

    That multiplication is what made a deleted post cost two minutes rather
    than twenty seconds.
    """
    import time as _t
    monkeypatch.setattr(cpm.time, "sleep", _t.sleep)
    assert len(poster.POST_DETAIL_SELECTORS) >= 6, "need several to prove it"
    poster.NAV_DECIDE_SECONDS = 0.4
    poster.NAV_POLL_SECONDS = 0.05
    poster.driver = PageDriver(POST_URL)

    started = _t.time()
    poster.classify_navigation(POST_URL)
    elapsed = _t.time() - started
    assert elapsed < len(poster.POST_DETAIL_SELECTORS) * 0.4, (
        "%.2fs looks like a per-selector budget" % elapsed)


# ─── the diagnostic capture ──────────────────────────────────────────────────

def test_the_unclear_page_is_captured_once_per_run_not_once_per_post(poster,
                                                                    monkeypatch):
    """Forty unknown posts must not write forty page dumps.

    One is evidence - it is what will let NAV_UNAVAILABLE_SELECTORS stop being
    guesses. Forty is just disk.
    """
    calls = []
    monkeypatch.setattr(cpm, "__name__", cpm.__name__)  # keep the module intact
    from linkedin_automation import failure_capture
    monkeypatch.setattr(failure_capture, "capture_failure",
                        lambda *a, **k: calls.append(a) or "cap.json")

    poster.driver = PageDriver(POST_URL)
    poster.navigate_to_post(POST_URL)
    poster.navigate_to_post(OTHER_URL)
    poster.navigate_to_post(POST_URL)
    assert len(calls) == 1


def test_a_gone_post_writes_no_diagnostic_capture_at_all(poster, monkeypatch):
    """Nothing is unknown about it and nothing was lost. There is nothing to
    look at, so it must not land among the comment-not-posted captures where a
    real silent failure would then be buried."""
    calls = []
    from linkedin_automation import failure_capture
    monkeypatch.setattr(failure_capture, "capture_failure",
                        lambda *a, **k: calls.append(a) or "cap.json")
    poster.driver = PageDriver("https://www.linkedin.com/feed/")
    poster.navigate_to_post(POST_URL)
    assert calls == []


# ─── the record ──────────────────────────────────────────────────────────────

def test_a_gone_post_is_recorded_but_never_as_a_failure(poster):
    """THE POINT OF THE SEPARATION.

    `failed_comments` means "we wrote a comment and it did not go out". A post
    that no longer exists can never be retried and there is nothing to fix;
    putting it there would be permanent noise in the one list that is supposed
    to demand attention.
    """
    poster.driver = PageDriver("https://www.linkedin.com/feed/")
    assert poster.post_single_comment(
        {"url": POST_URL, "comment": "a drafted comment", "preview": "a post"}) is False

    assert poster.progress.get("failed_comments", []) == []
    gone = poster.progress.get("unavailable_posts", [])
    assert [e["url"] for e in gone] == [POST_URL]
    assert gone[0]["reason"]


def test_an_unclear_post_records_nothing_terminal(poster):
    poster.driver = PageDriver(POST_URL)
    poster.post_single_comment({"url": POST_URL, "comment": "a draft", "preview": "a post"})
    assert poster.progress.get("unavailable_posts", []) == []


def test_the_record_survives_the_run(poster, tmp_path):
    poster.driver = PageDriver("https://www.linkedin.com/feed/")
    poster.post_single_comment({"url": POST_URL, "comment": "a draft", "preview": "a post"})
    on_disk = json.loads((tmp_path / "progress.json").read_text())
    assert [e["url"] for e in on_disk["unavailable_posts"]] == [POST_URL]


def test_the_same_post_is_not_recorded_twice(poster):
    poster.record_unavailable(POST_URL, "redirected")
    poster.record_unavailable(POST_URL, "redirected")
    assert len(poster.progress["unavailable_posts"]) == 1


def test_a_gone_post_never_reaches_the_posted_ledger(poster):
    poster.driver = PageDriver("https://www.linkedin.com/feed/")
    poster.post_single_comment({"url": POST_URL, "comment": "a draft", "preview": "a post"})
    assert POST_URL not in poster.progress.get("posted_comments", [])


def test_a_stale_outcome_cannot_leak_onto_the_next_post(poster):
    """last_navigation is read AFTER navigate_to_post returns False. If a
    previous post left "unavailable" there and the next one raised before
    classifying, a live post would be marked gone."""
    poster.driver = PageDriver("https://www.linkedin.com/feed/")
    poster.navigate_to_post(POST_URL)
    assert poster.last_navigation["outcome"] == poster.NAV_UNAVAILABLE

    class Boom(PageDriver):
        def get(self, url):
            raise RuntimeError("driver died")

    poster.driver = Boom(POST_URL)
    assert poster.navigate_to_post(OTHER_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNCLEAR


# ─── the run summary ─────────────────────────────────────────────────────────

def test_gone_posts_are_counted_apart_from_failures(poster):
    result = poster._report_run(posted=0, failed=0, skipped=0, attempted=3,
                                total=3, unavailable=3)
    assert result["unavailable"] == 3
    assert result["failed"] == 0


def test_the_summary_still_works_without_the_new_count(poster):
    """Older callers pass five positional arguments."""
    result = poster._report_run(1, 0, 0, 1, 1)
    assert result["unavailable"] == 0


# ─── the store: terminal means terminal ──────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = post_store.PostStore("default", path=str(tmp_path / "posts_db.json"))
    s.upsert_scraped({"url": POST_URL, "text": "a post"},
                     status=post_store.GENERATED)
    return s


def test_marking_a_post_unavailable_moves_it_out_of_the_queue(store):
    """The queue is by_status(GENERATED). Nothing else has to change for the
    poster to stop picking it up - which is the whole reason the state lives
    in the store rather than in a skip-list beside it."""
    assert len(store.by_status(post_store.GENERATED)) == 1
    assert store.mark_unavailable(POST_URL) is True
    assert store.by_status(post_store.GENERATED) == []
    assert len(store.by_status(post_store.UNAVAILABLE)) == 1


def test_a_gone_post_is_counted_on_its_own(store):
    store.mark_unavailable(POST_URL)
    assert store.counts()[post_store.UNAVAILABLE] == 1
    assert store.counts()[post_store.TRASH] == 0


def test_a_rescrape_does_not_resurrect_a_gone_post(store):
    """Scrape files outlive the posts in them. Without this the post returns
    to NEW, is regenerated, and buys another navigation timeout every run."""
    store.mark_unavailable(POST_URL)
    store.upsert_scraped({"url": POST_URL, "text": "a post"})
    assert store._resolve(POST_URL)["status"] == post_store.UNAVAILABLE


def test_a_commented_post_is_never_marked_gone(store):
    """That we commented is history and stays true after the post comes down.
    Overwriting it would corrupt the one record the double-post guard uses."""
    store.sync_with_progress([POST_URL])   # the posted ledger is what commits it
    assert store.mark_unavailable(POST_URL) is False
    assert store._resolve(POST_URL)["status"] == post_store.COMMENTED


def test_a_stale_ledger_entry_cannot_revive_a_gone_post(store):
    store.mark_unavailable(POST_URL)
    store.sync_with_progress([POST_URL])
    assert store._resolve(POST_URL)["status"] == post_store.UNAVAILABLE


def test_a_trashed_post_may_still_be_marked_gone(store):
    """Removed is removed, whatever we previously thought of it."""
    store.reject_by_evaluator(POST_URL)
    assert store.mark_unavailable(POST_URL) is True


def test_marking_records_why_and_when(store):
    store.mark_unavailable(POST_URL, reason="redirected to /feed/")
    rec = store._resolve(POST_URL)
    assert rec["unavailable_reason"] == "redirected to /feed/"
    assert rec["unavailable_at"]


def test_an_unknown_url_is_not_invented(store):
    assert store.mark_unavailable(OTHER_URL) is False


# ─── the store: reconciled FROM the poster's record ──────────────────────────

def test_reconcile_marks_the_posts_the_poster_found_gone(store, tmp_path,
                                                         monkeypatch):
    """One writer, one reader. The poster is the only thing that can observe
    a gone post, so it writes the fact and the store reconciles from it -
    exactly as COMMENTED is reconciled from posted_comments. Two systems each
    holding their own opinion of which posts exist is how they drift."""
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({
        "posted_comments": [],
        "unavailable_posts": [{"url": POST_URL, "reason": "redirected"}],
    }))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(progress))
    monkeypatch.setattr(pm, "get_comments_dir",
                        lambda n=None: str(tmp_path / "comments"))

    stats = {}
    post_store.reconcile("default", store=store, stats=stats)
    assert stats["unavailable"] == 1
    assert store._resolve(POST_URL)["status"] == post_store.UNAVAILABLE


def test_reconcile_is_idempotent_for_gone_posts(store, tmp_path, monkeypatch):
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({
        "posted_comments": [],
        "unavailable_posts": [{"url": POST_URL, "reason": "redirected"}],
    }))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(progress))
    monkeypatch.setattr(pm, "get_comments_dir",
                        lambda n=None: str(tmp_path / "comments"))

    post_store.reconcile("default", store=store)
    stats = {}
    post_store.reconcile("default", store=store, stats=stats)
    assert stats["unavailable"] == 1        # re-asserted, not doubled
    assert store.counts()[post_store.UNAVAILABLE] == 1


def test_a_draft_on_disk_does_not_pull_a_gone_post_back_to_generated(
        store, tmp_path, monkeypatch):
    """Step 5 runs before the draft steps for exactly this reason: the comment
    file written before the post was deleted is still sitting there."""
    comments = tmp_path / "comments"
    comments.mkdir()
    (comments / "comments_x.txt").write_text(
        "POST: %s\nCOMMENT: a drafted comment\n" % POST_URL, encoding="utf-8")
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({
        "posted_comments": [],
        "unavailable_posts": [{"url": POST_URL, "reason": "redirected"}],
    }))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(progress))
    monkeypatch.setattr(pm, "get_comments_dir",
                        lambda n=None: str(comments))

    post_store.reconcile("default", store=store)
    assert store._resolve(POST_URL)["status"] == post_store.UNAVAILABLE


def test_a_progress_file_with_no_gone_list_still_reconciles(store, tmp_path,
                                                            monkeypatch):
    """Every progress file written before today lacks the key."""
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({"posted_comments": []}))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(progress))
    monkeypatch.setattr(pm, "get_comments_dir",
                        lambda n=None: str(tmp_path / "comments"))
    stats = {}
    post_store.reconcile("default", store=store, stats=stats)
    assert stats["unavailable"] == 0
