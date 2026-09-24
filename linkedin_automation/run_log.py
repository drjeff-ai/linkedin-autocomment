"""Per-run file log and per-comment step timing for the comment poster.

One run of the poster should answer, from a file, the question a slow run
raises: is the time going to a BUG (a poll exhausting its budget, a lookup
that never succeeds) or to HUMANIZATION DWELL (the deliberate reading, typing
and pausing)? Those need opposite responses - fix the bug, or leave the dwell
alone - and a console log cannot tell them apart.

So every comment is partitioned into named steps, each logged with its elapsed
seconds, and a closing ``COMMENT`` line gives the total alongside the sum of
its steps. If the two disagree, time is going somewhere no step names. Every
bounded poll logs its DECLARED budget and its ACTUAL elapsed as two separate
fields, so a poll that overruns is visible as an overrun rather than absorbed
into a step total.

Everything written here goes to the file only. The timing logger does not
propagate, so the console output of a run is unchanged.
"""

import logging
import os
import re
import statistics
import time
from contextlib import contextmanager
from datetime import datetime

from . import profile_manager as pm

#: The logger every timing line is written through. File only: it does not
#: propagate to the root logger, which is where the console handler lives.
TIMING_LOGGER_NAME = "linkedin_automation.run_timing"
timing_logger = logging.getLogger(TIMING_LOGGER_NAME)
timing_logger.propagate = False
timing_logger.setLevel(logging.INFO)
# Without a handler of its own, a non-propagating logger falls through to
# logging.lastResort (stderr). NullHandler keeps it silent when no run log is
# open, e.g. first_comment.py driving the poster's primitives directly.
timing_logger.addHandler(logging.NullHandler())

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

_PROFILE_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def logs_dir() -> str:
    """``<project>/logs``. Anchored to PROJECT_ROOT, never the cwd."""
    return os.path.join(pm.PROJECT_ROOT, "logs")


def open_run_log(profile_name: str = None, log_dir: str = None):
    """Start ``logs/run_<profile>_<ts>.log``. Returns ``(handler, path)``.

    The handler goes on the root logger (so the poster's existing lines land in
    the file as well as on the console) and on the timing logger (so the
    timing lines land in the file and ONLY there). UTF-8 explicitly: the
    Windows default is cp1252, and the poster logs emoji.
    """
    directory = log_dir or logs_dir()
    os.makedirs(directory, exist_ok=True)
    safe = _PROFILE_SAFE.sub("_", profile_name or "default").strip("_") or "default"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"run_{safe}_{stamp}.log")

    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    timing_logger.addHandler(handler)
    return handler, path


def close_run_log(handler) -> None:
    """Detach and close a handler from :func:`open_run_log`. Never raises."""
    if handler is None:
        return
    for lg in (logging.getLogger(), timing_logger):
        try:
            lg.removeHandler(handler)
        except Exception:
            pass
    try:
        handler.close()
    except Exception:
        pass


def log_poll(post_id, name, declared, actual, outcome) -> dict:
    """One line per bounded poll: the budget and the reality, as two numbers.

    ``overrun`` is ``yes`` whenever actual exceeds declared. The polls check
    their deadline after doing their work, so an exhausted poll always runs a
    little over; how far over is exactly what this line exists to show.
    """
    over = actual - declared
    timing_logger.info(
        "POLL post=%s poll=%s declared=%.3fs actual=%.3fs overrun=%s "
        "overrun_by=%+.3fs outcome=%s",
        post_id or "-", name, declared, actual,
        "yes" if actual > declared else "no", over, outcome)
    return {"poll": name, "declared": declared, "actual": actual,
            "outcome": outcome}


class CommentTiming:
    """The step-by-step timing of one comment, from queue pop to ledger write.

    Steps must not nest: they partition the comment, and the check that matters
    is that their sum equals the total. Time that no step covers shows up as
    ``unaccounted`` on the closing line.
    """

    def __init__(self, post_id, clock=time.monotonic):
        self.post_id = post_id
        self.clock = clock
        self.started = clock()
        self.steps = []
        self.polls = []
        self.total = None
        self.outcome = None

    @contextmanager
    def step(self, name):
        t0 = self.clock()
        try:
            yield
        finally:
            elapsed = self.clock() - t0
            self.steps.append((name, elapsed))
            timing_logger.info("STEP post=%s step=%s elapsed=%.3fs",
                               self.post_id, name, elapsed)

    def poll(self, name, declared, actual, outcome):
        self.polls.append(log_poll(self.post_id, name, declared, actual,
                                   outcome))

    @property
    def steps_sum(self):
        return sum(e for _, e in self.steps)

    def finish(self, outcome):
        self.total = self.clock() - self.started
        self.outcome = outcome
        timing_logger.info(
            "COMMENT post=%s outcome=%s total=%.3fs steps_sum=%.3fs "
            "unaccounted=%.3fs steps=%d",
            self.post_id, outcome, self.total, self.steps_sum,
            self.total - self.steps_sum, len(self.steps))
        return self.total


class RunTiming:
    """Wall clock for a whole run, and where it went.

    ``accounted`` is run-level steps (setup, waits between posts, breaks,
    teardown) plus every comment's total. Against the wall clock, the gap is
    time spent outside anything named.
    """

    def __init__(self, profile_name=None, clock=time.monotonic):
        self.profile_name = profile_name or "default"
        self.clock = clock
        self.started = clock()
        self.run_steps = []
        self.comments = []

    @contextmanager
    def step(self, name):
        t0 = self.clock()
        try:
            yield
        finally:
            elapsed = self.clock() - t0
            self.run_steps.append((name, elapsed))
            timing_logger.info("RUNSTEP step=%s elapsed=%.3fs", name, elapsed)

    def add_comment(self, timing: CommentTiming):
        self.comments.append(timing)

    def summary(self, attempted=0, posted=0, skipped=0, failed=0,
                unavailable=0, like_misses=0) -> dict:
        wall = self.clock() - self.started
        totals = [c.total for c in self.comments if c.total is not None]
        accounted = sum(e for _, e in self.run_steps) + sum(totals)
        median = statistics.median(totals) if totals else 0.0
        worst = max(totals) if totals else 0.0
        out = {
            "attempted": attempted, "posted": posted, "skipped": skipped,
            "failed": failed, "unavailable": unavailable,
            "like_misses": like_misses, "comments_timed": len(totals),
            "wall_clock": wall, "accounted": accounted,
            "median_per_comment": median, "max_per_comment": worst,
        }
        timing_logger.info(
            "RUN profile=%s attempted=%d posted=%d skipped=%d failed=%d "
            "unavailable=%d like_misses=%d comments_timed=%d "
            "wall_clock=%.3fs accounted=%.3fs unaccounted=%.3fs "
            "median_per_comment=%.3fs max_per_comment=%.3fs",
            self.profile_name, attempted, posted, skipped, failed,
            unavailable, like_misses, len(totals), wall, accounted,
            wall - accounted, median, worst)
        return out
