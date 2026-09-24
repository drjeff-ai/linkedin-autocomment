"""Dispatch 15.1: one run produces a file log that settles bug vs dwell.

The claims, each of which a plausible implementation gets wrong:

  * the steps PARTITION a comment - their sum is its total, so time spent
    outside every named step is visible rather than absorbed;
  * every bounded poll reports its declared budget and its actual elapsed as
    TWO fields, so an overrun reads as an overrun;
  * the run summary accounts for the wall clock;
  * all of it goes to the file and none of it to the console;
  * the file is UTF-8 whatever the Windows code page says.

The end-to-end tests drive the REAL human_behavior code through a synthetic
page, with time.sleep scaled down rather than stubbed out, so the dwell is
really spent - just faster - and the steps have real time to account for.
"""

import json
import logging
import os
import re
import time

import pytest

from linkedin_automation import comment_fields
from linkedin_automation import comment_poster as cpm
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import run_log

from fake_post_page import FakePostPage

ACT = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"
URLS = [ACT.format(str(d) * 16) for d in (1, 2, 3, 4)]

#: Real sleeps, scaled. 1/200th keeps a whole 4-comment run near a second
#: while every dwell still costs SOMETHING, which is what the partition needs.
SLEEP_SCALE = 0.005

STEP_RE = re.compile(r"STEP post=(\S+) step=(\S+) elapsed=([\d.]+)s")
COMMENT_RE = re.compile(
    r"COMMENT post=(\S+) outcome=(\S+) total=([\d.]+)s steps_sum=([\d.]+)s")
POLL_RE = re.compile(
    r"POLL post=(\S+) poll=(\S+) declared=([\d.]+)s actual=([\d.]+)s "
    r"overrun=(yes|no)")
RUN_RE = re.compile(r"RUN .*wall_clock=([\d.]+)s accounted=([\d.]+)s")


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
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: real_sleep(s * SLEEP_SCALE))
    return tmp_path


def _comments_file(tmp_path, urls):
    comments = [{"url": u, "comment": "A considered point about eval %d." % i,
                 "post_preview": "preview", "author": "someone"}
                for i, u in enumerate(urls)]
    path = tmp_path / "comments.txt"
    path.write_text(comment_fields.comments_to_txt(comments, "ts"),
                    encoding="utf-8")
    return str(path)


def _run(env, monkeypatch, page, urls):
    monkeypatch.setattr(pm, "create_driver",
                        lambda name=None, headless=False: (page, {}))
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    # A poll that must exhaust (the missing Like) keeps a real deadline.
    poster.LIKE_WAIT_SECONDS = 0.2
    t0 = time.monotonic()
    result = poster.run(_comments_file(env, urls), post_count=len(urls))
    wall = time.monotonic() - t0
    with open(poster.run_log_path, encoding="utf-8") as f:
        return poster, result, wall, f.read()


# ─── the whole path, end to end ──────────────────────────────────────────────

def test_the_steps_partition_every_comment(env, monkeypatch):
    page = FakePostPage(gone_urls={URLS[2]}, like_absent_urls={URLS[1]})
    poster, result, wall, text = _run(env, monkeypatch, page, URLS)

    assert result["posted"] == 3 and result["unavailable"] == 1

    steps = {}
    for post, _name, elapsed in STEP_RE.findall(text):
        steps.setdefault(post, 0.0)
        steps[post] += float(elapsed)
    comments = COMMENT_RE.findall(text)
    assert len(comments) == 4
    for post, _outcome, total, steps_sum in comments:
        # Rounded to the millisecond per line, so allow a few ms of drift.
        assert abs(steps[post] - float(total)) < 0.05, (post, steps[post], total)
        assert abs(float(steps_sum) - float(total)) < 0.05


def test_the_dwell_is_named_as_dwell(env, monkeypatch):
    """The point of the log: humanization time lands in steps named as such,
    so it cannot be mistaken for a slow lookup."""
    page = FakePostPage()
    _, _, _, text = _run(env, monkeypatch, page, URLS[:1])
    names = {name for _, name, _ in STEP_RE.findall(text)}
    for required in ("navigate", "classify_navigation", "open_composer",
                     "type", "submit", "verify", "like", "progress_write"):
        assert required in names, required
    for dwell in ("navigate_dwell", "read_dwell", "pre_compose_dwell",
                  "review_dwell", "post_dwell"):
        assert dwell in names, dwell


def test_every_poll_logs_budget_and_reality_as_two_fields(env, monkeypatch):
    page = FakePostPage(like_absent_urls={URLS[0]})
    _, _, _, text = _run(env, monkeypatch, page, URLS[:1])
    polls = {name: (float(d), float(a), over)
             for _, name, d, a, over in POLL_RE.findall(text)}
    assert set(polls) == {"classify_navigation", "find_like_button",
                          "await_comment_input", "await_composer_submit",
                          "verify_with_polling"}
    # The exhausted poll: it ran the whole budget and is flagged over it. (The
    # loop checks its deadline after its work, so it overruns by a sliver -
    # sometimes under the 1 ms the log prints, hence >= and not >.)
    declared, actual, over = polls["find_like_button"]
    assert declared == pytest.approx(0.2)
    assert actual >= declared and over == "yes"
    # A poll that succeeded early: actual is its own number, under budget.
    declared, actual, over = polls["classify_navigation"]
    assert actual != declared
    assert actual < declared and over == "no"


def test_the_run_summary_accounts_for_the_wall_clock(env, monkeypatch):
    page = FakePostPage(gone_urls={URLS[2]}, like_absent_urls={URLS[1]})
    poster, _, wall, text = _run(env, monkeypatch, page, URLS)
    m = RUN_RE.search(text)
    assert m, "no RUN summary line"
    logged_wall, accounted = float(m.group(1)), float(m.group(2))
    assert abs(logged_wall - wall) < 0.25
    assert abs(accounted - logged_wall) < 0.25
    assert "like_misses=1" in text
    assert "unavailable=1" in text
    assert "median_per_comment=" in text and "max_per_comment=" in text
    assert poster.like_misses == 1


def test_a_run_that_cannot_log_in_still_writes_its_summary(env, monkeypatch):
    monkeypatch.setattr(pm, "login", lambda d, p: False)
    monkeypatch.setattr(pm, "create_driver",
                        lambda name=None, headless=False: (FakePostPage(), {}))
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    with pytest.raises(pm.LoginRequiredError):
        poster.run(_comments_file(env, URLS[:1]), post_count=1)
    with open(poster.run_log_path, encoding="utf-8") as f:
        assert "RUN profile=t attempted=0" in f.read()


# ─── file only, UTF-8 ────────────────────────────────────────────────────────

def test_timing_lines_never_reach_the_console(env, monkeypatch, caplog):
    """Console output is unchanged: the timing logger does not propagate."""
    assert run_log.timing_logger.propagate is False
    caplog.set_level(logging.INFO)
    _, _, _, text = _run(env, monkeypatch, FakePostPage(), URLS[:1])
    assert "STEP post=" in text
    assert "STEP post=" not in caplog.text
    assert "POLL post=" not in caplog.text


def test_the_log_is_utf8_and_named_for_the_profile(tmp_path):
    handler, path = run_log.open_run_log("jeff", log_dir=str(tmp_path))
    try:
        logging.getLogger("linkedin_automation.comment_poster").warning(
            "✅ posted — café")
    finally:
        run_log.close_run_log(handler)
    assert re.fullmatch(r"run_jeff_\d{8}_\d{6}\.log", os.path.basename(path))
    raw = open(path, "rb").read()
    assert "✅ posted — café".encode("utf-8") in raw


def test_the_handler_is_removed_when_the_run_ends(tmp_path):
    handler, _ = run_log.open_run_log("x", log_dir=str(tmp_path))
    run_log.close_run_log(handler)
    assert handler not in logging.getLogger().handlers
    assert handler not in run_log.timing_logger.handlers


# ─── the units ───────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_unaccounted_time_is_reported_not_hidden():
    clock = FakeClock()
    timing = run_log.CommentTiming("p", clock=clock)
    with timing.step("a"):
        clock.t += 2.0
    clock.t += 5.0                       # time no step covers
    with timing.step("b"):
        clock.t += 1.0
    assert timing.finish("posted") == pytest.approx(8.0)
    assert timing.steps_sum == pytest.approx(3.0)


def test_an_overrun_is_flagged(caplog):
    caplog.set_level(logging.INFO)
    run_log.timing_logger.propagate = True
    try:
        run_log.log_poll("p", "verify_with_polling", 8.0, 10.25, "not_verified")
    finally:
        run_log.timing_logger.propagate = False
    assert "declared=8.000s actual=10.250s overrun=yes overrun_by=+2.250s" \
        in caplog.text


def test_a_step_that_raises_is_still_timed():
    clock = FakeClock()
    timing = run_log.CommentTiming("p", clock=clock)
    with pytest.raises(RuntimeError):
        with timing.step("boom"):
            clock.t += 1.5
            raise RuntimeError
    assert timing.steps == [("boom", 1.5)]


def test_median_and_max_per_comment():
    clock = FakeClock()
    run = run_log.RunTiming("p", clock=clock)
    for total in (10.0, 30.0, 20.0):
        c = run_log.CommentTiming("x", clock=clock)
        clock.t += total
        c.finish("posted")
        run.add_comment(c)
    s = run.summary(attempted=3, posted=3)
    assert s["median_per_comment"] == pytest.approx(20.0)
    assert s["max_per_comment"] == pytest.approx(30.0)
    assert s["accounted"] == pytest.approx(60.0)


# ─── reconcile's log line names the unavailable count ────────────────────────

def test_the_reconcile_line_names_unavailable(tmp_path, monkeypatch, caplog):
    url = URLS[0]
    store = post_store.PostStore("default", path=str(tmp_path / "db.json"))
    store.upsert_scraped({"url": url, "text": "a post"},
                         status=post_store.GENERATED)
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({
        "posted_comments": [],
        "unavailable_posts": [{"url": url, "reason": "redirected"}]}))
    monkeypatch.setattr(pm, "get_progress_file", lambda n=None: str(progress))
    monkeypatch.setattr(pm, "get_comments_dir",
                        lambda n=None: str(tmp_path / "c"))
    caplog.set_level(logging.INFO, logger="linkedin_automation.post_store")
    post_store.reconcile("default", store=store)
    assert "unavailable=1" in caplog.text
