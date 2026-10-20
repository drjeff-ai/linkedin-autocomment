"""Background feeder that places first comments on PUBLISHED or COMMENT_FAILED
rows once their delay has elapsed.

Proven 2026-10-18: ``comment_pass`` (``tools/run_scheduled_posts.py comment``)
places a real first comment on a real published row. It was CLI-only -
nothing ever ran it - so a PUBLISHED row with a ``first_comment_link`` just
sat there until someone remembered to run the command by hand. This closes
that gap the same way ``post_drain.py`` closed the "HELD forever" gap, but it
is a different beast underneath: post_drain only ever talks to Buffer's API.
This one drives a real logged-in browser, so it is gated by the SAME
one-browser-task-per-profile lock the comment poster and the scheduler's post
job use (``browser_available``, wired to ``can_start_browser_task`` in
``dashboard.py``) - never post_drain's scheduling-task lock, which guards a
different collision (two ``schedule_pass`` calls racing each other, not a
browser session being shared).

Default OFF for every profile, including ``dev`` - this is a new live-posting
surface and nobody gets it on for free. A configurable delay, plus jitter,
holds off commenting on a row for a few minutes after it was last observed
eligible (``updated_at`` at that point) - mirroring the scheduler's own
reasoning that un-jittered, immediate automated activity looks mechanical.
For a row that has never failed, that timestamp is the moment it was first
observed PUBLISHED, and ``comment_pass`` never touches it again while
nothing else changes the row. For a row that already failed once and is
being retried (see below), it is instead the moment of that failure -
``PipelineState.update`` re-stamps ``updated_at`` on every call, including
the one that sets ``status=COMMENT_FAILED`` - so a retry waits out the same
delay+jitter again rather than being attempted again immediately.

A failure is a soft failure: ``comment_pass`` already records a row that could
not be commented as ``COMMENT_FAILED`` and moves on (``csv_pipeline.py``) -
this module adds no new failure handling on top of that. The drain's own job
only raises for something ``comment_pass`` itself cannot recover from (login
required, a declared-production refusal, an unexpected exit code); a normal
run that left some rows ``COMMENT_FAILED`` is still a successful drain tick.
``COMMENT_FAILED`` is not terminal here: ``rows_awaiting_first_comment``
(``csv_pipeline.py``) re-offers it on the next check, same as ``comment_pass``
itself already retries it on its own next sweep - a row that failed once
(a transient login problem, an identity-guard refusal) is retried
automatically rather than stranded until someone runs the CLI by hand. The
``FirstCommentLedger`` remains the only true duplicate guard: a
``COMMENT_FAILED`` row already carrying a ledger entry is excluded exactly
like a ``PUBLISHED`` one would be.

Everything external is injected (clock, rng, profile list, config, the
row-eligibility reads, the browser-lock check, the executor), so the whole
engine is unit-testable with no network and no browser - same shape as
``post_drain.py``.
"""

import copy
import logging
import random
import threading
from datetime import datetime, timedelta, timezone

from . import csv_pipeline
from . import first_comment as fc
from . import profile_manager as pm

logger = logging.getLogger(__name__)

# ─── Tunables ─────────────────────────────────────────────────────────────────

TICK_SECONDS = 30          # how often the loop re-evaluates (mirrors post_drain)
MAX_RECENT_RUNS = 50       # per-profile ring buffer of check records for the UI

#: How often a profile is actually re-checked. Cheap (no browser, no Buffer -
#: just a local state-file read) until something is actually ready to run, so
#: this can be shorter than post_drain's interval without any rate-limit risk.
DEFAULT_INTERVAL_MINUTES = 15

#: How long a row must have been PUBLISHED before its first comment is placed.
#: "The user wants this prompt" (Dispatch 33) - a few minutes, not hours - but
#: configurable per profile, and never zero by default: commenting the instant
#: Buffer reports a post live is the most mechanical-looking timing available.
DEFAULT_DELAY_MINUTES = 5

#: Extra random minutes added on top of the delay, re-rolled every check, so
#: the exact moment a comment lands does not line up with the delay to the
#: second on every post.
DEFAULT_JITTER_MINUTES = 3

#: Job category used when the drain submits work. Distinct from the main
#: engagement path's "post_comments" category and from post_drain's own
#: category so all three are separable in the jobs list.
JOB_CATEGORY = "first_comment_drain"

DEFAULT_FIRST_COMMENT_DRAIN = {
    "enabled": False,
    "interval_minutes": DEFAULT_INTERVAL_MINUTES,
    "delay_minutes": DEFAULT_DELAY_MINUTES,
    "jitter_minutes": DEFAULT_JITTER_MINUTES,
}

# What a check actually resolved to.
ACTION_RAN = "ran"                            # the comment pass was submitted
ACTION_NOTHING_WAITING = "nothing_waiting"    # no row is owed a first comment
ACTION_WAITING_ON_DELAY = "waiting_on_delay"  # row(s) exist, none have cleared delay+jitter
ACTION_BUSY = "busy"                          # a browser task is already running
ACTION_ERROR = "error"                        # reading eligible rows blew up


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge ``override`` onto ``base``; both deep-copied.

    Identical reasoning to post_drain's and the scheduler's own: a shallow
    copy leaves an untouched profile's section aliasing the module default,
    and enabling the drain for one profile would silently enable it for every
    profile that never configured one - a background thread driving a browser
    on an account nobody switched it on for.
    """
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_first_comment_drain_config(section: dict) -> dict:
    """Fill a profile's raw ``scheduled_posting.first_comment_drain`` section
    with defaults."""
    return _deep_merge(DEFAULT_FIRST_COMMENT_DRAIN, section or {})


def first_comment_drain_config(profile_cfg: dict) -> dict:
    """Pull the first-comment-drain section out of a whole profile config."""
    scheduled = (profile_cfg or {}).get("scheduled_posting") or {}
    return merge_first_comment_drain_config(scheduled.get("first_comment_drain"))


def _default_list_profiles():
    try:
        return list(pm.list_profiles().get("profiles", {}).keys())
    except Exception:
        logger.exception("FirstCommentDrain: could not list profiles")
        return []


def _default_state_fn(profile):
    return csv_pipeline.PipelineState(profile_name=profile)


def _default_ledger_fn(profile):
    return fc.FirstCommentLedger(profile_name=profile)


def _parse_iso(value):
    """Best-effort parse of a stored ``updated_at``. ``None`` on anything odd.

    A row with no readable timestamp must not be silently treated as "just
    published" (which would skip the delay) or "published forever ago" (which
    would skip it from consideration) - see ``_ready_rows``: no timestamp
    means not ready yet, the same safe-side default the delay itself uses.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class FirstCommentDrain:
    """Runs the first-comment pass for rows whose delay has elapsed.

    Injected collaborators:

    * ``now_fn`` — clock, defaults to ``datetime.now(timezone.utc)``. Timezone
      -aware on purpose: stored ``updated_at`` values are UTC ISO strings, and
      comparing them against a naive clock would raise.
    * ``rng`` — a ``random.Random``-like object (needs ``uniform``), defaults
      to the ``random`` module. Same injection shape as ``scheduler.py``'s.
    * ``list_profiles() -> list[str]`` and ``config_fn(profile) -> dict``.
    * ``state_fn(profile) -> PipelineState`` and ``ledger_fn(profile) ->
      FirstCommentLedger`` — how eligible rows are read.
    * ``browser_available(profile) -> bool`` — the SAME one-browser-task lock
      the comment poster and the scheduler's post job use. Never the
      scheduling-task lock post_drain shares with the Schedule button; that
      guards a different collision (two Buffer schedule passes), and this
      drain never calls ``schedule_pass`` at all.
    * ``submit_comment_pass(profile) -> job_id | None`` — start the comment
      pass; returns ``None`` when it declined (the lock was lost narrowly
      between this engine's own check and the submit).
    """

    def __init__(self, *, now_fn=None, rng=None, tick_seconds=TICK_SECONDS,
                 list_profiles=None, config_fn=None, state_fn=None,
                 ledger_fn=None, browser_available=None,
                 submit_comment_pass=None):
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random
        self._tick_seconds = tick_seconds
        self._list_profiles = list_profiles or _default_list_profiles
        self._config_fn = config_fn or pm.get_profile_config
        self._state_fn = state_fn or _default_state_fn
        self._ledger_fn = ledger_fn or _default_ledger_fn
        self._browser_available = browser_available or (lambda profile: True)
        self._submit_comment_pass = submit_comment_pass

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {}  # profile -> {"last_check", "last_run", "recent_runs"}

    # ── thread lifecycle ──────────────────────────────────────────────────────

    def start(self):
        """Start the background loop (idempotent)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="first-comment-drain", daemon=True)
            self._thread.start()
            logger.info("FirstCommentDrain started (tick=%ss)", self._tick_seconds)

    def stop(self, timeout=None):
        """Signal the loop to stop and optionally join it."""
        self._stop.set()
        thread = self._thread
        if thread and timeout is not None:
            thread.join(timeout)

    def is_running(self) -> bool:
        """True while the background drain thread is alive."""
        return bool(self._thread and self._thread.is_alive())

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("FirstCommentDrain tick failed")
            self._stop.wait(self._tick_seconds)
        logger.info("FirstCommentDrain loop exited")

    # ── evaluation ────────────────────────────────────────────────────────────

    def tick(self, now=None):
        """Evaluate every enabled profile once. Safe to call directly in tests."""
        now = now or self._now()
        for profile in self._list_profiles():
            with self._lock:
                try:
                    self._tick_profile(profile, now)
                except Exception:
                    logger.exception("FirstCommentDrain: profile %s tick failed",
                                     profile)

    def _profile_state(self, profile):
        st = self._state.get(profile)
        if st is None:
            st = {"last_check": None, "last_run": None, "recent_runs": []}
            self._state[profile] = st
        return st

    def _interval(self, cfg):
        try:
            minutes = float(cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))
        except (TypeError, ValueError):
            minutes = DEFAULT_INTERVAL_MINUTES
        return timedelta(minutes=max(1.0, minutes))

    def _due(self, state, cfg, now):
        last = state["last_check"]
        return last is None or (now - last) >= self._interval(cfg)

    def _delay(self, cfg):
        """The effective "how long since PUBLISHED" threshold for this check.

        Re-rolled every check (not cached per row), so jitter is genuinely a
        per-check random amount rather than a fixed-per-row offset that would
        make the exact comment time predictable from the row alone.
        """
        try:
            delay_minutes = float(cfg.get("delay_minutes", DEFAULT_DELAY_MINUTES))
        except (TypeError, ValueError):
            delay_minutes = DEFAULT_DELAY_MINUTES
        try:
            jitter_minutes = float(cfg.get("jitter_minutes", DEFAULT_JITTER_MINUTES))
        except (TypeError, ValueError):
            jitter_minutes = DEFAULT_JITTER_MINUTES
        delay_minutes = max(0.0, delay_minutes)
        jitter_minutes = max(0.0, jitter_minutes)
        jitter = self._rng.uniform(0, jitter_minutes) if jitter_minutes > 0 else 0.0
        return timedelta(minutes=delay_minutes + jitter)

    def _tick_profile(self, profile, now):
        cfg = first_comment_drain_config(self._config_fn(profile) or {})
        if not cfg.get("enabled"):
            return
        state = self._profile_state(profile)
        if not self._due(state, cfg, now):
            return
        self._check(profile, state, now, cfg)

    def _ready_rows(self, profile, now, delay):
        """(candidates, ready): every eligible row, and the subset past delay.

        A row with no readable ``updated_at`` is a candidate but never ready -
        the safe side, same reasoning as an unreadable thread in
        ``already_commented_here`` (MAINTENANCE §6.6): "cannot tell" must not
        read as "go ahead".
        """
        state = self._state_fn(profile)
        ledger = self._ledger_fn(profile)
        candidates = csv_pipeline.rows_awaiting_first_comment(state, ledger)
        ready = []
        for key, entry in candidates:
            published_at = _parse_iso(entry.get("updated_at"))
            if published_at is not None and (now - published_at) >= delay:
                ready.append((key, entry))
        return candidates, ready

    def _check(self, profile, state, now, cfg=None, manual=False):
        """One drain check. Records only when something happened or broke.

        ``last_check`` is stamped whatever the outcome, so the interval
        advances even on a quiet check and the UI can say when it last looked.
        """
        state["last_check"] = now
        cfg = cfg if cfg is not None else first_comment_drain_config(
            self._config_fn(profile) or {})
        delay = self._delay(cfg)

        try:
            candidates, ready = self._ready_rows(profile, now, delay)
        except Exception as exc:
            logger.warning("FirstCommentDrain[%s] could not read eligible rows: %s",
                           profile, exc)
            return self._record(state, now, action=ACTION_ERROR, waiting=0,
                                ready=0, note="could not read eligible rows: %s"
                                % exc, manual=manual)

        # Cheapest test first: no eligible row at all means no reason to touch
        # the browser lock or spawn anything.
        if not candidates:
            return {"action": ACTION_NOTHING_WAITING, "waiting": 0, "ready": 0,
                    "note": "nothing published is waiting on its first comment"}

        if not ready:
            return {"action": ACTION_WAITING_ON_DELAY, "waiting": len(candidates),
                    "ready": 0, "note": "%d row(s) waiting, none have cleared "
                    "the delay yet" % len(candidates)}

        # Never start a second browser session for this profile. A collision
        # with the comment poster or the scheduler's post job is the failure
        # this lock exists to prevent - not merely wasteful, since LinkedIn
        # read/write races from two sessions is exactly the kind of thing that
        # gets an account restricted.
        if not self._browser_available(profile):
            logger.debug("FirstCommentDrain[%s] waiting: a browser task is "
                         "already running for this profile", profile)
            return {"action": ACTION_BUSY, "waiting": len(candidates),
                    "ready": len(ready),
                    "note": "a browser task is already running for this profile"}

        job_id = None
        if self._submit_comment_pass:
            job_id = self._submit_comment_pass(profile)
        if job_id is None:
            # The lock was lost narrowly between the check above and the
            # submit. Not an error; the next interval retries.
            return {"action": ACTION_BUSY, "waiting": len(candidates),
                    "ready": len(ready), "note": "comment pass not submitted"}

        state["last_run"] = now
        logger.info("FirstCommentDrain[%s] running the comment pass for %d "
                    "ready row(s) of %d waiting (job=%s)",
                    profile, len(ready), len(candidates), job_id)
        return self._record(state, now, action=ACTION_RAN, waiting=len(candidates),
                            ready=len(ready), job_id=job_id, manual=manual)

    def _record(self, state, now, action, waiting=0, ready=0, job_id=None,
               note=None, manual=False):
        entry = {
            "at": now.isoformat(),
            "action": action,
            "waiting": waiting,
            "ready": ready,
            "job_id": job_id,
            "manual": manual,
            "note": note,
        }
        runs = state["recent_runs"]
        runs.append(entry)
        del runs[:-MAX_RECENT_RUNS]
        return entry

    # ── public API used by the dashboard endpoints ────────────────────────────

    def status(self, profile):
        """Drain state for one profile: on/off, interval/delay/jitter, how
        many rows are waiting, last/next check, recent runs."""
        with self._lock:
            now = self._now()
            cfg = first_comment_drain_config(self._config_fn(profile) or {})
            state = self._profile_state(profile)
            interval = self._interval(cfg)
            last_check = state["last_check"]
            next_check = (last_check + interval) if last_check else None

            waiting = 0
            try:
                ledger = self._ledger_fn(profile)
                pipeline_state = self._state_fn(profile)
                waiting = len(csv_pipeline.rows_awaiting_first_comment(
                    pipeline_state, ledger))
            except Exception:
                logger.exception("FirstCommentDrain: could not count waiting "
                                 "rows for %s", profile)

            return {
                "running": self.is_running(),
                "enabled": bool(cfg.get("enabled")),
                "interval_minutes": cfg.get("interval_minutes"),
                "delay_minutes": cfg.get("delay_minutes"),
                "jitter_minutes": cfg.get("jitter_minutes"),
                "now": now.isoformat(),
                "waiting_count": waiting,
                "last_check_at": last_check.isoformat() if last_check else None,
                "last_run_at": (state["last_run"].isoformat()
                               if state["last_run"] else None),
                "next_check_at": next_check.isoformat() if next_check else None,
                "recent_runs": list(reversed(state["recent_runs"])),
            }

    def run_now(self, profile):
        """Check immediately, ignoring the interval. Returns the outcome dict."""
        with self._lock:
            now = self._now()
            state = self._profile_state(profile)
            result = self._check(profile, state, now, manual=True)
            # A quiet manual check still deserves a visible record - the
            # person pressed a button and is owed an answer.
            if result and result.get("action") in (ACTION_NOTHING_WAITING,
                                                   ACTION_WAITING_ON_DELAY,
                                                   ACTION_BUSY):
                self._record(state, now, manual=True, **result)
            return result

    def toggle(self, profile, enabled):
        """Turn the drain on or off for a profile, persisting the config."""
        with self._lock:
            cfg = self._config_fn(profile) or {}
            scheduled = cfg.get("scheduled_posting")
            if not isinstance(scheduled, dict):
                scheduled = {}
            drain = merge_first_comment_drain_config(
                scheduled.get("first_comment_drain"))
            drain["enabled"] = bool(enabled)
            scheduled["first_comment_drain"] = drain
            cfg["scheduled_posting"] = scheduled
            pm.save_profile_config(profile, cfg)
            if enabled:
                # A freshly enabled drain checks on the next tick rather than
                # waiting out an interval that started before it was on.
                self._profile_state(profile)["last_check"] = None
            return drain

    def update_config(self, profile, patch):
        """Deep-merge a partial config (interval/delay/jitter) and persist it."""
        with self._lock:
            cfg = self._config_fn(profile) or {}
            scheduled = cfg.get("scheduled_posting")
            if not isinstance(scheduled, dict):
                scheduled = {}
            drain = _deep_merge(
                merge_first_comment_drain_config(scheduled.get("first_comment_drain")),
                patch or {})
            scheduled["first_comment_drain"] = drain
            cfg["scheduled_posting"] = scheduled
            pm.save_profile_config(profile, cfg)
            return drain
