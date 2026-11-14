"""In-dashboard randomized scheduler for posting comments, scraping,
connecting, and (under ``auto_mode``) running the full scrape-generate-post
cycle unattended.

A single background thread, started when the Flask dashboard launches, ticks
every :data:`TICK_SECONDS`. On each tick it walks every profile whose
``scheduler`` config is enabled and, for each enabled job (``post_comments`` /
``scrape`` / ``auto_cycle`` / ``connector``) and each of that job's time
windows, decides whether to fire.

Randomization is the whole point — LinkedIn punishes mechanical automation, so:

* **Daily re-roll.** Each window rolls a fresh random fire time within its
  ``[start, end]`` bounds once per day; it is never the same minute two days
  running.
* **Random count.** ``post_comments`` posts a random number of drafts within
  ``[count_min, count_max]`` each run.
* **Random skips.** With probability ``skip_chance`` (default 0.1) a rolled run
  is skipped entirely — humans miss windows.
* **Minimum gap.** Two real runs for a profile never fire closer together than
  :data:`MIN_GAP_MINUTES`.

It never "catches up" a window that has closed: if the dashboard was not running
for the whole of a window, that window is marked missed for the day, with the
true cause in the note, and the normal schedule resumes (catching up would batch
actions unnaturally). A window that is still open is never lost to a late start
or a restart: a fresh roll whose time has already passed re-draws between now
and the window's end (S1), and the per-profile ``scheduler_state.json``
(injected via ``state_path_fn``) carries today's slots, ``last_fire`` and the run
history across a restart, so a slot that already fired is not fired again. Firing a
real run goes through the injected executor callbacks, which use the dashboard's
``run_job`` / browser-task lock, so a scheduled run respects the "one browser
task per profile" rule and never collides with a manual run.

``auto_mode`` (default OFF for every profile) is the single switch for the
unattended comment pipeline. Dispatch 37 gave scrape, generate and post
separate windows and separate fire times; the result was that work sat
between stages and no cycle ever completed end to end in one unattended run.
Dispatch 40 replaced that with ONE job type, ``auto_cycle``, that runs scrape,
then generate against what the scrape produced, then post - in order,
waiting for each stage to finish before starting the next. Like the old
``generate`` job it replaces, ``auto_cycle`` is deliberately NOT governed by
the same plain ``enabled``-flag rule ``post_comments``/``scrape`` use: it only
ever fires from the unattended tick loop when ``auto_mode`` is also on for
that profile (see ``_tick_profile``). Flipping ``auto_mode`` on is a
convenience that also turns on the master switch and ``auto_cycle``'s own
``enabled`` flag in one write (``toggle(profile, "auto_mode", True)``).

``post_comments``/``scrape`` keep their own independent schedules (Audit 36 -
they can run unattended under the plain master+job ``enabled`` flags with no
auto_mode involved), but while ``auto_mode`` is ON for a profile, the
unattended tick loop suppresses both of them from firing on their own
(Dispatch 41) - the cycle already runs a scrape and a post stage itself, and
letting the standalone jobs fire too would duplicate that work every cycle.
Suppression is a tick-loop gate only: their ``enabled`` flags and every other
config value are left exactly as configured, so turning ``auto_mode`` back
off restores them with no separate re-toggle, and ``run_now()`` still fires
either one on demand regardless of ``auto_mode`` (same bypass it already
gives ``enabled``). ``connector`` is untouched by ``auto_mode`` in both
directions, same as before. A per-cycle cap (``auto_cycle.max_comments``,
default :data:`GENERATE_DEFAULT_CAP`) and count range
(``auto_cycle.count_min``/``count_max``) bound the generate and post stages
exactly as they did under the old separate jobs - same numbers, now read from
one section.

Any stage failing stops the cycle immediately - later stages do not run - and
disables ``auto_mode`` for that profile on that FIRST failure, not after a
run of consecutive ones ("one failure, not three": the user wants to look at
it, not have it silently retried). This is written back through the same
``pm.save_profile_config`` path ``toggle``/``update_config`` use, with the
failing stage and a note persisted onto the config
(``auto_mode_stopped_reason`` / ``auto_mode_stopped_at``) so the dashboard can
surface it, not just a log line. A scrape that finds zero posts is explicitly
NOT a failure - the cycle continues to generate (which will find nothing new)
and then to post (which drains whatever is already GENERATED); a quiet day is
not a fault. The real cycle runs as a background job this engine does not
wait on (same "never catches up" design as everything else here) - the
engine fires it as a single async submit and is fed the outcome
asynchronously via ``report_cycle_outcome``, called from the dashboard's
job-completion hook once the whole sequence actually finishes. The engine's
own tick/fire path never blocks on it - see ``run_auto_cycle`` below for
where the real sequential waiting happens, and .dev/DECISIONS.md for why
holding the browser lock across the whole cycle is possible without that.

The engine has no Flask/dashboard imports — the executor (``submit_post_job``
/ ``submit_scrape_job`` / ``submit_cycle_job`` / ``submit_connector_job``
/ ``browser_available``), the clock (``now_fn``), the RNG (``rng``) and the
profile list (``list_profiles``) are all injected, so the whole thing is
unit-testable with a mock clock and no real browser/posting/generation.
``run_auto_cycle`` (module-level, pure, also dependency-injected) is what
actually sequences scrape/generate/post and is the one gate tests target
directly for call-order and short-circuit-on-failure, with no Scheduler, no
Flask and no subprocess involved at all.

``connector`` is cap-gated, not queue-gated: unlike ``post_comments`` (empty
GENERATED bin) or ``scrape`` (nothing to check beforehand), a connector run
has a cross-run daily/weekly cap tracked in
:class:`auto_connector.ConnectionTracker`'s on-disk file. ``_fire_connector``
reads that cap BEFORE calling the executor, so a profile already at its cap
never even submits a job. ``connector`` is an unrelated feature - auto
connecting to people, not part of the comment pipeline - and auto_mode never
touches it, in either direction.
"""

import os
import copy
import json
import logging
import random
import threading
from datetime import datetime, time as dtime, timedelta

from . import profile_manager as pm
from . import post_store
from .comment_fields import comments_to_txt
from .auto_connector import ConnectionTracker

logger = logging.getLogger(__name__)

# ─── Tunables ─────────────────────────────────────────────────────────────────

TICK_SECONDS = 30          # how often the background loop re-evaluates
MIN_GAP_MINUTES = 90       # never fire two real runs for a profile closer than this
MAX_RECENT_RUNS = 50       # per-profile ring buffer of run records for the UI

#: Per-cycle cap on how many drafts an unattended Generate fire asks for.
#: comment_generator.py's OWN default is NO cap (``max_comments=None``,
#: CLI ``--limit`` default ``None`` — "generate for all engaging posts").
#: That is fine for a human clicking Generate once on a bounded NEW backlog,
#: but an unattended cycle can follow a scrape of 50-100 posts (scrape's own
#: default ``max_posts``), and nothing upstream caps how many of those end up
#: NEW. Capping here, not in the generator, keeps the generator's manual
#: behavior (no cap) unchanged and bounds auto_mode's OpenAI spend per cycle
#: regardless of scrape size. See .dev/DECISIONS.md for the chosen value.
GENERATE_DEFAULT_CAP = 10

JOB_TYPES = ("post_comments", "scrape", "auto_cycle", "connector")

# Default scheduler config. get_profile_config already deep-merges the shipped
# default_profile_config.json, but we merge against this constant too so missing
# keys always fall back even if the on-disk default is older or partial.
DEFAULT_SCHEDULER = {
    "enabled": False,
    "auto_mode": False,
    # GENERATED drafts older than this many days are moved to TRASH (reason
    # ``stale``, restorable) before any scheduled post selects drafts (S1).
    # 0 disables expiry. No UI field.
    "draft_max_age_days": 3,
    "post_comments": {
        "enabled": True,
        "windows": [["08:00", "11:00"], ["14:00", "17:00"]],
        "count_min": 4,
        "count_max": 8,
        "skip_chance": 0.1,
    },
    "scrape": {
        "enabled": False,
        "windows": [["09:00", "12:00"]],
        "max_posts": 100,
        "min_quality": 10,
        "skip_chance": 0.1,
    },
    "auto_cycle": {
        "enabled": False,
        # One window, one fire time, for the whole scrape -> generate -> post
        # cycle (Dispatch 40) - replaces the old separate scrape/generate/
        # post_comments windows auto_mode used to drive independently. See
        # .dev/DECISIONS.md for what happens to an existing config that still
        # carries the old scheduler.generate section.
        "windows": [["09:00", "12:00"]],
        "max_posts": 100,
        "min_quality": 10,
        "max_comments": GENERATE_DEFAULT_CAP,
        "count_min": 4,
        "count_max": 8,
        "skip_chance": 0.1,
    },
    "connector": {
        "enabled": False,
        "windows": [["10:00", "16:00"]],
        # How many search-result pages ONE scheduled session walks. Deliberately
        # half the CLI's own manual default (10) — unattended, conservative.
        # The real ceiling is the cross-run daily/weekly cap in
        # ConnectionTracker, not this; pages only bounds how far a single
        # session paginates toward sending that many.
        "pages": 5,
        "skip_chance": 0.1,
    },
}

# Slot lifecycle within a single day.
PENDING = "pending"
FIRED = "fired"
SKIPPED = "skipped"
MISSED = "missed"

# Run-record action values (what actually happened when a window resolved).
ACTION_FIRED = "fired"
ACTION_SKIPPED = "skipped"
ACTION_QUEUE_EMPTY = "queue_empty"
ACTION_MISSED = "missed"
ACTION_AT_CAP = "at_cap"

# Run-record notes for a MISSED window. Each names the one true cause.
NOTE_STARTED_AFTER_CLOSE = "dashboard started after this window closed"
NOTE_DOWN_AT_FIRE_TIME = "dashboard was not running at the scheduled time"

# What status() reports for a job auto_mode keeps from running on its own.
PAUSED_BY_AUTO_MODE = "auto_mode"
PAUSED = "paused"

_SLOT_STATUSES = (PENDING, FIRED, SKIPPED, MISSED)


# ─── Config helpers ───────────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """Return ``base`` deep-merged with ``override`` (override wins, dicts merged).

    The copies are deep on purpose. ``dict(base)`` copies only the top level, so
    any section the override does not mention stayed the *same object* as the one
    in :data:`DEFAULT_SCHEDULER`. Callers then mutate the result:

    ``toggle()`` does ``sched.setdefault(target, {})["enabled"] = enabled``. For a
    profile whose config has no ``scrape`` section, that wrote straight through
    into the module-level defaults, for the life of the process.

    The dashboard is long-running and serves every profile from one process, so
    enabling scrape for one profile silently enabled it for **every** other
    profile that had never configured a scrape section — and the scheduler would
    then drive a browser against LinkedIn for accounts nobody switched on. That
    is an account-safety problem, not just a config bug.

    ``value`` is copied too, so a persisted config never aliases a caller's dict
    or list (``update_config`` passes a patch straight through).
    """
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_scheduler_config(section: dict, profile: str = None) -> dict:
    """Fill a profile's raw ``scheduler`` section against :data:`DEFAULT_SCHEDULER`.

    This is the one "config load" point every reader (tick, status, run_now,
    toggle, the circuit breaker's outcome reporter) funnels through. An
    existing config that still carries the old (pre-Dispatch-40)
    ``scheduler.generate`` section keeps it: ``_deep_merge`` copies every key
    the override has, regardless of whether :data:`DEFAULT_SCHEDULER` still
    has a matching key, so that section survives in the merged result as
    inert data. Nothing reads it, nothing fires from it, and nothing deletes
    it - it just sits there until the profile's config is edited or reset.
    """
    return _deep_merge(DEFAULT_SCHEDULER, section or {})


def _suppressed_by_auto_mode(cfg: dict, job_type: str) -> bool:
    """True for a standalone job auto_mode's cycle already covers.

    While auto_mode is on, the cycle runs scrape and post as its first and
    third stages; the standalone scrape / post_comments jobs must not ALSO
    fire unattended, or every cycle duplicates its own work (Dispatch 41).
    Their ``enabled`` flags are left untouched, so turning auto_mode off
    restores them exactly as configured.
    """
    return job_type in ("scrape", "post_comments") and bool(cfg.get("auto_mode"))


def _job_runs_unattended(cfg: dict, job_type: str) -> bool:
    """Would the unattended tick run this job? ``cfg`` is the merged scheduler
    section. The master switches (``automations`` and ``cfg["enabled"]``) are
    the caller's to check.

    The one predicate both ``_tick_profile`` and ``status()`` use (S1): before
    it, ``status()`` rolled jobs the tick never ran and recorded MISSED runs
    for them every time the page was opened. ``run_now()`` bypasses it on
    purpose.
    """
    job_cfg = cfg.get(job_type) or {}
    if not job_cfg.get("enabled"):
        return False
    # auto_cycle never fires from the unattended tick loop without auto_mode,
    # regardless of its own enabled flag.
    if job_type == "auto_cycle" and not cfg.get("auto_mode"):
        return False
    if _suppressed_by_auto_mode(cfg, job_type):
        return False
    return True


def draft_max_age_days(full_config: dict) -> float:
    """``scheduler.draft_max_age_days`` from a profile's full config, merged
    against :data:`DEFAULT_SCHEDULER`. A value that is not a non-negative
    number falls back to the default, with a warning."""
    cfg = merge_scheduler_config((full_config or {}).get("scheduler"))
    raw = cfg.get("draft_max_age_days")
    try:
        days = float(raw)
    except (TypeError, ValueError):
        days = -1
    if days < 0:
        default = DEFAULT_SCHEDULER["draft_max_age_days"]
        logger.warning("Scheduler: bad draft_max_age_days %r, using %s", raw, default)
        return default
    return days


def expire_stale_drafts(store, full_config: dict, now=None) -> int:
    """Trash GENERATED drafts older than the profile's ``draft_max_age_days``
    (restorable, reason ``stale``) and save the store when any moved.

    Called by both scheduled post paths, ``_fire_post_comments`` and the
    auto_cycle post stage, before they select drafts, so the two cannot
    disagree. The manual Post button does not call it.
    """
    days = draft_max_age_days(full_config)
    moved = store.expire_stale_generated(days, now=now, save=True)
    if moved:
        logger.info("Scheduler: %d GENERATED draft(s) older than %s day(s) "
                    "moved to TRASH (stale)", moved, days)
    return moved


def _parse_hhmm(value: str):
    """Parse ``"HH:MM"`` into ``(hour, minute)``; tolerate bad input as midnight."""
    try:
        hh, mm = str(value).split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except (ValueError, AttributeError):
        logger.warning("Scheduler: bad time %r, treating as 00:00", value)
        return 0, 0


def _window_bounds(window, day):
    """``(start, end)`` datetimes of an ``["HH:MM", "HH:MM"]`` window on ``day``.
    An end before the start collapses to the start."""
    start_h, start_m = _parse_hhmm(window[0])
    end_h, end_m = _parse_hhmm(window[1])
    start_dt = datetime.combine(day, dtime(start_h, start_m))
    end_dt = datetime.combine(day, dtime(end_h, end_m))
    return start_dt, max(start_dt, end_dt)


_PERSISTED_SLOT_KEYS = ("job_type", "window_index", "window", "rolled_window",
                        "rolled_date", "fire_at", "will_skip", "status")


def _state_to_json(state):
    """The JSON form of one profile's runtime state (see ``_state_from_json``)."""
    slots = []
    for slot in state["slots"].values():
        if not slot.get("rolled_date"):
            continue  # created but never rolled: nothing to carry over
        out = {k: slot.get(k) for k in _PERSISTED_SLOT_KEYS}
        out["fire_at"] = slot["fire_at"].isoformat() if slot.get("fire_at") else None
        slots.append(out)
    last = state.get("last_fire")
    return {
        "version": 1,
        "slots": slots,
        "last_fire": last.isoformat() if last else None,
        "recent_runs": list(state.get("recent_runs") or []),
    }


def _state_from_json(raw, today):
    """Rebuild runtime state from the file's JSON. Raises ``ValueError`` (or
    ``TypeError``/``KeyError``) on any malformed part; the caller then starts
    empty. Slots rolled on a day other than ``today`` are dropped; a PENDING
    slot is marked ``loaded`` so its first evaluation can tell a time that
    passed while the dashboard was down from one that passed while it ran."""
    if not isinstance(raw, dict):
        raise ValueError("state is not an object")
    slots = {}
    for item in raw.get("slots") or []:
        if not isinstance(item, dict):
            raise ValueError("slot is not an object")
        job_type = item["job_type"]
        idx = item["window_index"]
        status = item["status"]
        window = item["window"]
        rolled_window = item["rolled_window"]
        if job_type not in JOB_TYPES or not isinstance(idx, int) \
                or status not in _SLOT_STATUSES:
            raise ValueError(f"bad slot {job_type!r}/{idx!r}/{status!r}")
        if not (isinstance(window, list) and len(window) == 2
                and isinstance(rolled_window, list) and len(rolled_window) == 2):
            raise ValueError(f"bad window in slot {job_type}/{idx}")
        if item["rolled_date"] != today:
            continue
        slots[(job_type, idx)] = {
            "job_type": job_type, "window_index": idx,
            "window": list(window), "rolled_window": list(rolled_window),
            "rolled_date": today,
            "fire_at": datetime.fromisoformat(item["fire_at"]),
            "will_skip": bool(item["will_skip"]),
            "status": status,
            "loaded": status == PENDING,
        }
    last = raw.get("last_fire")
    runs = raw.get("recent_runs") or []
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        raise ValueError("recent_runs is not a list of objects")
    return {
        "slots": slots,
        "last_fire": datetime.fromisoformat(last) if last else None,
        "recent_runs": runs[-MAX_RECENT_RUNS:],
    }


def _default_list_profiles():
    """Names of all configured profiles (used when no override is injected)."""
    try:
        return list(pm.list_profiles().get("profiles", {}).keys())
    except Exception:
        logger.exception("Scheduler: could not list profiles")
        return []


def write_comments_file(profile, records, now):
    """Serialize GENERATED records to a poster-readable TXT file.

    Shared by ``_fire_post_comments`` (the independent post_comments job) and
    the auto_cycle post stage (dashboard's real ``run_post`` closure) - one
    serialization, used by both.
    """
    comments = []
    for rec in records:
        meta = rec.get("comment_meta") or {}
        comments.append({
            "url": rec.get("url", ""),
            "author": rec.get("author", ""),
            "post_text": rec.get("text", ""),
            "category": rec.get("category", ""),
            "comment": rec.get("comment", ""),
            "word_count": meta.get("word_count"),
            "style": meta.get("style", ""),
            "approach": meta.get("approach", ""),
        })
    comments_dir = pm.get_comments_dir(profile)
    ts = now.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(comments_dir, f"scheduled_comments_{ts}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(comments_to_txt(comments, now.strftime("%Y-%m-%d %H:%M")))
    return path


def run_auto_cycle(profile, run_scrape, run_generate, run_post):
    """Run one profile's unattended cycle: scrape, then generate against what
    the scrape produced, then post - in order, waiting for each stage to
    finish before starting the next, stopping at the first stage that fails.

    Each ``run_X`` callable takes no arguments (the caller closes over
    profile/params) and returns a dict with an ``"error"`` key, ``None`` on
    success. A scrape that finds zero posts is NOT an error - only a real
    failure (crash, login wall, bad exit code) stops the cycle; a quiet
    scrape still runs generate (which will find nothing new) and post (which
    drains whatever is already GENERATED) - a quiet day is not a fault.

    Pure and dependency-injected - no Flask, no subprocess, no browser - so
    the call order and the short-circuit-on-failure behavior are directly
    unit-testable with fakes (see tests/test_scheduler.py). In production,
    dashboard._auto_cycle_job builds the real (blocking, subprocess/API
    calling) closures and is the only caller; this function only sequences
    them and never touches the browser lock, Flask, or any job bookkeeping
    itself.
    """
    outcome = {"ok": True, "failed_stage": None, "note": None,
               "scrape": None, "generate": None, "post": None}

    scrape_result = run_scrape()
    outcome["scrape"] = scrape_result
    if scrape_result.get("error"):
        outcome["ok"] = False
        outcome["failed_stage"] = "scrape"
        outcome["note"] = scrape_result["error"]
        return outcome

    generate_result = run_generate()
    outcome["generate"] = generate_result
    if generate_result.get("error"):
        outcome["ok"] = False
        outcome["failed_stage"] = "generate"
        outcome["note"] = generate_result["error"]
        return outcome

    post_result = run_post()
    outcome["post"] = post_result
    if post_result.get("error"):
        outcome["ok"] = False
        outcome["failed_stage"] = "post"
        outcome["note"] = post_result["error"]
        return outcome

    return outcome


# ─── Scheduler engine ─────────────────────────────────────────────────────────

class Scheduler:
    """Randomized twice-daily scheduler running as one background thread.

    All external effects are injected so the engine is fully unit-testable:

    * ``now_fn`` — clock, defaults to ``datetime.now``.
    * ``rng`` — a ``random.Random``-like object (needs ``randint``/``random``).
    * ``submit_post_job(profile, comments_file, count) -> job_id | None`` — start
      a browser posting job (returns ``None`` if the browser lock is held).
    * ``submit_scrape_job(profile, max_posts, min_quality) -> job_id | None``.
    * ``submit_cycle_job(profile, job_cfg) -> job_id | None`` — start the one
      unattended auto_cycle job (scrape -> generate -> post, in order). The
      real sequential waiting happens inside this job's own body
      (``run_auto_cycle`` below), on its own background thread - this call
      itself returns immediately, same as every other submit here.
    * ``submit_connector_job(profile, pages) -> job_id | None`` — start a
      browser connector (auto-connect) job walking at most ``pages`` of
      search results.
    * ``browser_available(profile) -> bool`` — the browser-task lock check.
    * ``list_profiles() -> list[str]`` and ``config_fn(profile) -> dict``.
    * ``state_path_fn(profile) -> str`` — where the profile's schedule state
      (today's slots, ``last_fire``, ``recent_runs``) is persisted. ``None``
      (the default, and every test that does not ask for it) means nothing is
      read or written; the dashboard injects ``data/<profile>/scheduler_state.json``.
    """

    def __init__(self, *, now_fn=None, rng=None, tick_seconds=TICK_SECONDS,
                 min_gap_minutes=MIN_GAP_MINUTES, submit_post_job=None,
                 submit_scrape_job=None, submit_cycle_job=None,
                 submit_connector_job=None,
                 browser_available=None, list_profiles=None, config_fn=None,
                 state_path_fn=None):
        self._now = now_fn or datetime.now
        self._rng = rng or random
        self._tick_seconds = tick_seconds
        self._min_gap = min_gap_minutes
        self._submit_post_job = submit_post_job
        self._submit_scrape_job = submit_scrape_job
        self._submit_cycle_job = submit_cycle_job
        self._submit_connector_job = submit_connector_job
        self._browser_available = browser_available or (lambda profile: True)
        self._list_profiles = list_profiles or _default_list_profiles
        self._config_fn = config_fn or pm.get_profile_config
        self._state_path_fn = state_path_fn

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {}  # profile -> {"slots", "last_fire", "recent_runs"}

    # ── thread lifecycle ──────────────────────────────────────────────────────

    def start(self):
        """Start the background loop (idempotent)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="scheduler", daemon=True)
            self._thread.start()
            logger.info("Scheduler started (tick=%ss, min_gap=%smin)",
                        self._tick_seconds, self._min_gap)

    def stop(self, timeout=None):
        """Signal the loop to stop and optionally join it."""
        self._stop.set()
        thread = self._thread
        if thread and timeout is not None:
            thread.join(timeout)

    def is_running(self) -> bool:
        """True while the background scheduler thread is alive."""
        return bool(self._thread and self._thread.is_alive())

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop.wait(self._tick_seconds)
        logger.info("Scheduler loop exited")

    # ── evaluation ────────────────────────────────────────────────────────────

    def tick(self, now=None):
        """Evaluate every enabled profile once. Safe to call directly in tests."""
        now = now or self._now()
        for profile in self._list_profiles():
            with self._lock:
                try:
                    self._tick_profile(profile, now)
                except Exception:
                    logger.exception("Scheduler: profile %s tick failed", profile)

    def _tick_profile(self, profile, now):
        full_config = self._config_fn(profile) or {}
        # The per-profile master switch, ahead of every scheduler flag: while
        # it is off, no job rolls, records or fires for this profile.
        if not pm.automations_enabled(full_config):
            return
        cfg = merge_scheduler_config(full_config.get("scheduler"), profile)
        if not cfg.get("enabled"):
            return
        state = self._profile_state(profile, now)
        for job_type in JOB_TYPES:
            # run_now() bypasses this predicate deliberately, the same way it
            # bypasses enabled - see run_now's docstring.
            if not _job_runs_unattended(cfg, job_type):
                continue
            job_cfg = cfg.get(job_type, {})
            for idx, window in enumerate(job_cfg.get("windows") or []):
                slot = self._slot(state, job_type, idx, window)
                self._ensure_rolled(profile, state, slot, job_cfg, now)
                self._maybe_fire(profile, state, slot, job_type, job_cfg, now)

    # ── per-slot state ────────────────────────────────────────────────────────

    def _profile_state(self, profile, now=None):
        """The profile's runtime state, loaded from its state file on first
        access when persistence is injected."""
        st = self._state.get(profile)
        if st is None:
            st = self._load_state(profile, now or self._now())
            self._state[profile] = st
        return st

    # ── persistence (S1) ──────────────────────────────────────────────────────
    #
    # Without it a restart forgot which of today's slots had fired, the
    # last_fire behind MIN_GAP, and the run history. The file is written after
    # every roll, fire and record, atomically, and read once per profile.

    def _state_path(self, profile):
        if self._state_path_fn is None:
            return None
        return self._state_path_fn(profile)

    def _load_state(self, profile, now):
        """Read the profile's state file. Slots rolled on another day are
        dropped; runs and ``last_fire`` are kept. A missing, unreadable or
        malformed file logs a warning and starts empty. Never raises."""
        empty = {"slots": {}, "last_fire": None, "recent_runs": []}
        path = self._state_path(profile)
        if path is None:
            return empty
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            state = _state_from_json(raw, now.date().isoformat())
        except FileNotFoundError:
            logger.warning("Scheduler[%s] no state file at %s; starting empty",
                           profile, path)
            return empty
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning("Scheduler[%s] state file %s unreadable (%s); "
                           "starting empty", profile, path, exc)
            return empty
        logger.info("Scheduler[%s] loaded state: %d slot(s) for today, %d run(s)",
                    profile, len(state["slots"]), len(state["recent_runs"]))
        return state

    def _save_state(self, profile):
        """Write the profile's state atomically (temp file, then ``os.replace``).
        A failed write is logged, never raised: the tick must go on."""
        path = self._state_path(profile)
        if path is None:
            return
        state = self._state.get(profile)
        if state is None:
            return
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_state_to_json(state), f, indent=2)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            logger.exception("Scheduler[%s] could not write state file %s",
                             profile, path)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    logger.warning("Scheduler[%s] could not remove %s", profile, tmp)

    def _slot(self, state, job_type, idx, window):
        """Get-or-create the runtime slot for one (job, window index)."""
        key = (job_type, idx)
        slot = state["slots"].get(key)
        window = list(window)
        if slot is None:
            slot = {
                "job_type": job_type, "window_index": idx, "window": window,
                "rolled_window": None, "rolled_date": None, "fire_at": None,
                "will_skip": False, "status": PENDING,
            }
            state["slots"][key] = slot
        else:
            slot["window"] = window
        return slot

    def _roll(self, slot, job_cfg, now):
        """Re-roll a slot for today: random fire time within its window + skip flag.

        If the freshly rolled time is already at or before now (the dashboard
        started late in the window, or after it):

        * window still open: re-draw uniformly between now and the window end,
          PENDING. A late start inside a window no longer loses that run.
        * window closed: MISSED. We never fire after the window (no catch-up).
        """
        today = now.date()
        start_dt, end_dt = _window_bounds(slot["window"], today)
        span = int((end_dt - start_dt).total_seconds())
        offset = self._rng.randint(0, span) if span > 0 else 0
        slot["fire_at"] = start_dt + timedelta(seconds=offset)
        slot["will_skip"] = self._rng.random() < float(job_cfg.get("skip_chance", 0.1))
        slot["rolled_date"] = today.isoformat()
        slot["rolled_window"] = list(slot["window"])
        slot["loaded"] = False
        if slot["fire_at"] > now:
            slot["status"] = PENDING
        elif now < end_dt:
            remaining = int((end_dt - now).total_seconds())
            slot["fire_at"] = now + timedelta(seconds=self._rng.randint(0, remaining))
            slot["status"] = PENDING
        else:
            slot["status"] = MISSED

    def _ensure_rolled(self, profile, state, slot, job_cfg, now):
        """Roll the slot if it hasn't been rolled today (or its window changed).

        A PENDING slot loaded from the state file whose time passed while the
        dashboard was down is left PENDING while its window is open (the
        normal ``_maybe_fire`` path fires it) and is MISSED once it has closed.
        """
        today = now.date().isoformat()
        if slot["rolled_date"] == today and slot["rolled_window"] == slot["window"]:
            self._resolve_loaded(profile, state, slot, now)
            return
        self._roll(slot, job_cfg, now)
        logger.info(
            "Scheduler[%s] rolled %s window %s (%s-%s): fire_at=%s skip=%s status=%s",
            profile, slot["job_type"], slot["window_index"],
            slot["window"][0], slot["window"][1],
            slot["fire_at"].strftime("%H:%M:%S"), slot["will_skip"], slot["status"])
        if slot["status"] == MISSED:
            self._record(state, slot, now, action=ACTION_MISSED,
                         note=NOTE_STARTED_AFTER_CLOSE)
        self._save_state(profile)

    def _resolve_loaded(self, profile, state, slot, now):
        """First evaluation of a slot read from the state file."""
        if not slot.get("loaded"):
            return
        slot["loaded"] = False
        if slot["status"] != PENDING or now < slot["fire_at"]:
            return
        _, end_dt = _window_bounds(slot["window"], now.date())
        if now < end_dt:
            return  # still open: _maybe_fire fires it on the normal path
        slot["status"] = MISSED
        logger.info("Scheduler[%s] %s window %s MISSED: %s", profile,
                    slot["job_type"], slot["window_index"], NOTE_DOWN_AT_FIRE_TIME)
        self._record(state, slot, now, action=ACTION_MISSED,
                     note=NOTE_DOWN_AT_FIRE_TIME)
        self._save_state(profile)

    def _maybe_fire(self, profile, state, slot, job_type, job_cfg, now):
        """Fire (or skip/defer) a pending slot whose rolled time has arrived."""
        if slot["status"] != PENDING or now < slot["fire_at"]:
            return

        if slot["will_skip"]:
            slot["status"] = SKIPPED
            logger.info("Scheduler[%s] %s window %s SKIPPED (random skip roll)",
                        profile, job_type, slot["window_index"])
            self._record(state, slot, now, action=ACTION_SKIPPED)
            self._save_state(profile)
            return

        # Respect the browser lock: an active manual (or other scheduled) task
        # means we simply wait and retry on the next tick — never queue up.
        if not self._browser_available(profile):
            logger.debug("Scheduler[%s] %s waiting: browser busy", profile, job_type)
            return

        # Never fire two real runs closer than the minimum gap.
        last = state["last_fire"]
        if last is not None and (now - last) < timedelta(minutes=self._min_gap):
            logger.debug("Scheduler[%s] %s deferring: within %smin min-gap",
                         profile, job_type, self._min_gap)
            return

        result = self._fire(profile, job_type, job_cfg, now)
        slot["status"] = FIRED
        if result.get("action") == ACTION_FIRED:
            state["last_fire"] = now
        self._record(state, slot, now, **result)
        self._save_state(profile)

    # ── actions ───────────────────────────────────────────────────────────────

    def _fire(self, profile, job_type, job_cfg, now):
        if job_type == "post_comments":
            return self._fire_post_comments(profile, job_cfg, now)
        if job_type == "scrape":
            return self._fire_scrape(profile, job_cfg, now)
        if job_type == "auto_cycle":
            return self._fire_auto_cycle(profile, job_cfg, now)
        if job_type == "connector":
            return self._fire_connector(profile, job_cfg, now)
        return {"action": "error", "count": 0, "note": f"unknown job {job_type}"}

    def _fire_post_comments(self, profile, job_cfg, now):
        """Post a random count of GENERATED drafts; no-op when the queue is empty.
        Drafts older than ``draft_max_age_days`` are trashed first (S1)."""
        store = post_store.load_synced_store(profile)
        expire_stale_drafts(store, self._config_fn(profile) or {}, now)
        generated = store.by_status(post_store.GENERATED)
        if not generated:
            logger.info("Scheduler[%s] post_comments: No comments queued, "
                        "skipping scheduled run", profile)
            return {"action": ACTION_QUEUE_EMPTY, "count": 0,
                    "note": "no comments queued"}

        lo = int(job_cfg.get("count_min", 4))
        hi = int(job_cfg.get("count_max", 8))
        if hi < lo:
            lo, hi = hi, lo
        count = min(self._rng.randint(lo, hi), len(generated))
        comments_file = write_comments_file(profile, generated[:count], now)

        job_id = None
        if self._submit_post_job:
            job_id = self._submit_post_job(profile, comments_file, count)
        logger.info("Scheduler[%s] post_comments FIRED: count=%s file=%s job=%s",
                    profile, count, os.path.basename(comments_file), job_id)
        return {"action": ACTION_FIRED, "count": count, "job_id": job_id,
                "note": None if job_id else "browser busy, not submitted"}

    def _fire_scrape(self, profile, job_cfg, now):
        """Kick a scrape to refill the pipeline."""
        max_posts = int(job_cfg.get("max_posts", 100))
        min_quality = int(job_cfg.get("min_quality", 10))
        job_id = None
        if self._submit_scrape_job:
            job_id = self._submit_scrape_job(profile, max_posts, min_quality)
        logger.info("Scheduler[%s] scrape FIRED: max_posts=%s min_quality=%s job=%s",
                    profile, max_posts, min_quality, job_id)
        return {"action": ACTION_FIRED, "count": max_posts, "job_id": job_id,
                "note": None if job_id else "browser busy, not submitted"}

    def _fire_auto_cycle(self, profile, job_cfg, now):
        """Fire the one unattended cycle (scrape -> generate -> post) as a
        single async job - this call returns immediately, same as every
        other ``_fire_*`` here, so the tick thread never blocks on it.

        The real sequential waiting - scrape, then generate against what the
        scrape produced, then post, stopping at the first stage that fails -
        happens inside the executor's own job body, on its own dedicated
        background thread (one ``run_job`` call, held ``task_type="browser"``
        for the whole cycle: see ``run_auto_cycle`` below and
        .dev/DECISIONS.md for why that single job holds the browser lock
        continuously rather than taking it in turn per stage).
        """
        job_id = None
        if self._submit_cycle_job:
            job_id = self._submit_cycle_job(profile, dict(job_cfg))
        logger.info("Scheduler[%s] auto_cycle FIRED: job=%s", profile, job_id)
        return {"action": ACTION_FIRED, "count": 0, "job_id": job_id,
                "note": None if job_id else "browser busy, not submitted"}

    # ── auto_mode circuit breaker ─────────────────────────────────────────────
    #
    # The real cycle runs as a background job this engine does not wait on
    # (same "never catches up" design as the rest of the file) - so unlike
    # everything above, this is fed a real outcome from OUTSIDE the tick/fire
    # path, asynchronously, once the dashboard's job actually finishes. Safe
    # to call whether or not auto_mode is on: it only polices the unattended
    # chain, so it is a no-op unless this profile's auto_mode is currently
    # True.

    def report_cycle_outcome(self, profile, success, failed_stage=None, note=None):
        """Feed back a completed auto_cycle job's outcome.

        One failure disables auto_mode immediately - no consecutive-failure
        counting. Dispatch 40: "one failure, not three" - the user wants to
        look at a stopped cycle, not have it silently retried. A scrape that
        found zero posts is not a failure (``run_auto_cycle`` never reports
        it as one), so it never reaches here as one.
        """
        with self._lock:
            cfg = merge_scheduler_config((self._config_fn(profile) or {}).get("scheduler"), profile)
            if not cfg.get("auto_mode"):
                return
            if success:
                return
            self._disable_auto_mode(profile, failed_stage or "cycle", note)

    def _disable_auto_mode(self, profile, stage, note):
        """Turn auto_mode off and persist why - the dashboard reads this back
        from config, not from the log line above. It does not loop quietly."""
        cfg = self._config_fn(profile) or {}
        sched = merge_scheduler_config(cfg.get("scheduler"), profile)
        sched["auto_mode"] = False
        reason = f"{stage} failed"
        if note:
            reason += f": {note}"
        sched["auto_mode_stopped_reason"] = reason
        sched["auto_mode_stopped_at"] = self._now().isoformat()
        cfg["scheduler"] = sched
        pm.save_profile_config(profile, cfg)
        logger.error("Scheduler[%s] AUTO MODE DISABLED ITSELF: %s", profile, reason)

    def _fire_connector(self, profile, job_cfg, now):
        """Fire a connector session; no-op if there is nothing to search with
        or if the cross-run cap is already spent.

        Both checks happen here, before the executor (and so before any
        browser factory) is ever reached: a profile with no configured
        ``search_urls``, or one already at its daily or weekly cap, never
        submits a job, let alone opens a browser that can't send anything.
        """
        full_config = self._config_fn(profile) or {}
        conn_cfg = full_config.get("connector", {}) or {}
        urls = conn_cfg.get("search_urls") or []
        if not urls:
            logger.info("Scheduler[%s] connector: no search_urls configured, skipping", profile)
            return {"action": ACTION_QUEUE_EMPTY, "count": 0,
                    "note": "no search URLs configured"}

        daily_limit = conn_cfg.get("max_daily_requests", ConnectionTracker.DEFAULT_DAILY_LIMIT)
        weekly_limit = conn_cfg.get("weekly_limit", ConnectionTracker.DEFAULT_WEEKLY_LIMIT)
        tracker = ConnectionTracker(profile, weekly_limit=weekly_limit, daily_limit=daily_limit)
        remaining = tracker.get_remaining()
        if remaining <= 0:
            logger.info("Scheduler[%s] connector: at daily/weekly cap, skipping", profile)
            return {"action": ACTION_AT_CAP, "count": 0,
                    "note": "daily or weekly connection cap reached"}

        pages = int(job_cfg.get("pages", 5))
        job_id = None
        if self._submit_connector_job:
            job_id = self._submit_connector_job(profile, pages)
        logger.info("Scheduler[%s] connector FIRED: pages=%s job=%s",
                    profile, pages, job_id)
        return {"action": ACTION_FIRED, "count": 0, "job_id": job_id,
                "note": None if job_id else "browser busy, not submitted"}

    def _record(self, state, slot, now, action, count=0, job_id=None,
                note=None, manual=False, **_ignore):
        entry = {
            "job_type": slot["job_type"],
            "window_index": slot.get("window_index"),
            "window": slot.get("window"),
            "scheduled_for": slot["fire_at"].isoformat() if slot.get("fire_at") else None,
            "fired_at": now.isoformat(),
            "action": action,
            "count": count,
            "job_id": job_id,
            "manual": manual,
            "note": note,
        }
        runs = state["recent_runs"]
        runs.append(entry)
        del runs[:-MAX_RECENT_RUNS]

    # ── public API used by the dashboard endpoints ────────────────────────────

    def status(self, profile):
        """Full scheduler state for a profile: master flag, per-job windows with
        today's rolled fire time + skip flag, count ranges, and recent runs.

        Opening it rolls only what the tick itself would run (the shared
        ``_job_runs_unattended`` predicate), so viewing the page can never
        record a run for a job that is not scheduled to run. A job auto_mode
        suppresses reports ``paused_by: "auto_mode"`` and paused windows."""
        with self._lock:
            now = self._now()
            full_config = self._config_fn(profile) or {}
            automations = pm.automations_enabled(full_config)
            cfg = merge_scheduler_config(full_config.get("scheduler"), profile)
            state = self._profile_state(profile, now)
            masters_on = bool(automations and cfg.get("enabled"))
            jobs = {}
            for job_type in JOB_TYPES:
                job_cfg = cfg.get(job_type, {})
                paused_by = (PAUSED_BY_AUTO_MODE
                             if _suppressed_by_auto_mode(cfg, job_type) else None)
                rolls = masters_on and _job_runs_unattended(cfg, job_type)
                windows = []
                for idx, window in enumerate(job_cfg.get("windows") or []):
                    slot = self._slot(state, job_type, idx, window)
                    # No rolling unless the tick would run this job: a roll
                    # can record a MISSED run.
                    if rolls:
                        self._ensure_rolled(profile, state, slot, job_cfg, now)
                    if paused_by:
                        windows.append({"index": idx, "window": list(window),
                                        "fire_at": None, "will_skip": False,
                                        "status": PAUSED})
                        continue
                    windows.append({
                        "index": idx,
                        "window": list(window),
                        "fire_at": slot["fire_at"].isoformat() if slot["fire_at"] else None,
                        "will_skip": slot["will_skip"],
                        "status": slot["status"],
                    })
                last_run = next(
                    (r for r in reversed(state["recent_runs"])
                     if r.get("job_type") == job_type), None)
                jobs[job_type] = {
                    "enabled": job_cfg.get("enabled", False),
                    "paused_by": paused_by,
                    "windows": windows,
                    "count_min": job_cfg.get("count_min"),
                    "count_max": job_cfg.get("count_max"),
                    "max_posts": job_cfg.get("max_posts"),
                    "min_quality": job_cfg.get("min_quality"),
                    "max_comments": job_cfg.get("max_comments"),
                    "pages": job_cfg.get("pages"),
                    "skip_chance": job_cfg.get("skip_chance"),
                    "last_run": last_run,
                }
            return {
                "running": self.is_running(),
                # The process serving this status. With the engines started
                # only in the serving process (S1), it equals the pid in the
                # "Background engines started in process <pid>" log line.
                "engine_pid": os.getpid(),
                "automations_enabled": automations,
                "enabled": cfg.get("enabled", False),
                "auto_mode": cfg.get("auto_mode", False),
                "auto_mode_stopped_reason": cfg.get("auto_mode_stopped_reason"),
                "auto_mode_stopped_at": cfg.get("auto_mode_stopped_at"),
                "min_gap_minutes": self._min_gap,
                "now": now.isoformat(),
                "jobs": jobs,
                "recent_runs": list(reversed(state["recent_runs"])),
            }

    def run_now(self, profile, job_type):
        """Fire a job immediately, ignoring window / skip / min-gap (for testing).

        The browser lock still applies via the executor: if it is held the
        submit returns no job id and the result note says so.

        The per-profile master switch is NOT bypassed: while it is off this
        fires nothing and records nothing.
        """
        if job_type not in JOB_TYPES:
            return {"action": "error", "count": 0, "note": f"unknown job {job_type}"}
        with self._lock:
            now = self._now()
            full_config = self._config_fn(profile) or {}
            if not pm.automations_enabled(full_config):
                return {"action": pm.AUTOMATIONS_DISABLED_ACTION, "count": 0,
                        "note": pm.automations_disabled_message(profile)}
            cfg = merge_scheduler_config(full_config.get("scheduler"), profile)
            result = self._fire(profile, job_type, cfg.get(job_type, {}), now)
            state = self._profile_state(profile, now)
            slot = {"job_type": job_type, "window_index": -1,
                    "window": None, "fire_at": now}
            self._record(state, slot, now, manual=True, **result)
            self._save_state(profile)
            return result

    def toggle(self, profile, target, enabled):
        """Enable/disable the master switch, ``auto_mode``, or a specific job,
        persisting config.

        ``auto_mode`` turns on the master switch and ``auto_cycle``'s own
        ``enabled`` flag in the same write, rather than requiring two separate
        toggle calls. Turning it off only clears ``auto_mode`` itself (and any
        stale self-disable reason) - it does not forcibly turn
        scrape/post_comments/auto_cycle back off, since scrape/post_comments
        have their own independent toggles and may have been running before
        auto_mode was ever touched, and a profile re-enabling auto_mode
        expects the cycle to resume without re-flipping it.
        """
        with self._lock:
            cfg = self._config_fn(profile) or {}
            sched = merge_scheduler_config(cfg.get("scheduler"), profile)
            if target == "master":
                sched["enabled"] = bool(enabled)
            elif target == "auto_mode":
                sched["auto_mode"] = bool(enabled)
                if enabled:
                    sched["enabled"] = True
                    sched.setdefault("auto_cycle", {})["enabled"] = True
                sched.pop("auto_mode_stopped_reason", None)
                sched.pop("auto_mode_stopped_at", None)
            elif target in JOB_TYPES:
                sched.setdefault(target, {})["enabled"] = bool(enabled)
            else:
                raise ValueError(f"unknown toggle target {target!r}")
            cfg["scheduler"] = sched
            pm.save_profile_config(profile, cfg)
            return sched

    def update_config(self, profile, patch):
        """Deep-merge a partial scheduler config (windows/counts/etc.) and persist."""
        with self._lock:
            cfg = self._config_fn(profile) or {}
            sched = _deep_merge(merge_scheduler_config(cfg.get("scheduler"), profile), patch or {})
            cfg["scheduler"] = sched
            pm.save_profile_config(profile, cfg)
            return sched
