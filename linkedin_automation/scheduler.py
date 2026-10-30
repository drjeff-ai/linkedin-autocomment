"""In-dashboard randomized scheduler for posting comments, scraping, and
(under ``auto_mode``) generating drafts to feed the other two.

A single background thread, started when the Flask dashboard launches, ticks
every :data:`TICK_SECONDS`. On each tick it walks every profile whose
``scheduler`` config is enabled and, for each enabled job (``post_comments`` /
``scrape`` / ``generate``) and each of that job's time windows, decides
whether to fire.

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

It never "catches up" missed runs: if the dashboard was closed when a window's
rolled time passed, that window is simply marked missed for the day and the
normal schedule resumes (catching up would batch actions unnaturally). Firing a
real run goes through the injected executor callbacks, which use the dashboard's
``run_job`` / browser-task lock, so a scheduled run respects the "one browser
task per profile" rule and never collides with a manual run.

``auto_mode`` (default OFF for every profile) is the single switch that closes
the previously-manual-only gap in the comment pipeline: Generate. Audit 36
found that ``scrape`` and ``post_comments`` already ran unattended under the
plain ``enabled`` master flag — only Generate had no scheduler job type at
all. ``generate`` is now a third job type, but it is deliberately NOT governed
by the same plain ``enabled``-flag rule the other two use: it only ever fires
from the unattended tick loop when ``auto_mode`` is also on for that profile
(see ``_tick_profile``). Flipping ``auto_mode`` on is a convenience that also
turns on the master switch and all three jobs' own ``enabled`` flags in one
write (``toggle(profile, "auto_mode", True)``) — "a single toggle that enables
the chain" rather than four separate ones. A per-cycle cap
(``generate.max_comments``, default :data:`GENERATE_DEFAULT_CAP`) bounds how
many drafts one unattended Generate fire can produce, independent of how many
NEW posts a scrape returned — see ``.dev/DECISIONS.md`` for why the default
is a cap at all, given ``comment_generator``'s own default is no cap.

A circuit breaker protects the unattended chain specifically: if Generate
fails, or a scrape returns zero posts, on :data:`AUTO_MODE_FAILURE_THRESHOLD`
consecutive cycles, ``auto_mode`` disables itself for that profile — written
back through the same ``pm.save_profile_config`` path ``toggle``/
``update_config`` use, with the reason and timestamp persisted onto the
config (``auto_mode_stopped_reason`` / ``auto_mode_stopped_at``) so the
dashboard can surface it, not just a log line. Real scrape/generate jobs run
as background subprocesses the engine does not wait on (same "never catches
up" design as everything else here), so the two counters are fed
asynchronously via ``report_scrape_outcome`` / ``report_generate_outcome``,
called from the dashboard's job-completion hook — the engine's own tick/fire
path never blocks on a real job finishing.

The engine has no Flask/dashboard imports — the executor (``submit_post_job``
/ ``submit_scrape_job`` / ``submit_generate_job`` / ``browser_available``),
the clock (``now_fn``), the RNG (``rng``) and the profile list
(``list_profiles``) are all injected, so the whole thing is unit-testable
with a mock clock and no real browser/posting/generation.
"""

import os
import copy
import logging
import random
import threading
from datetime import datetime, time as dtime, timedelta

from . import profile_manager as pm
from . import post_store
from .comment_fields import comments_to_txt

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

#: Consecutive bad cycles (Generate failing, or a scrape returning zero
#: posts) before auto_mode disables itself for a profile. Tracked per job
#: type (see ``_score_auto_mode_cycle``), not shared — three Generate
#: failures in a row trips it even if scrape has been fine, and vice versa.
AUTO_MODE_FAILURE_THRESHOLD = 3

JOB_TYPES = ("post_comments", "scrape", "generate")

# Default scheduler config. get_profile_config already deep-merges the shipped
# default_profile_config.json, but we merge against this constant too so missing
# keys always fall back even if the on-disk default is older or partial.
DEFAULT_SCHEDULER = {
    "enabled": False,
    "auto_mode": False,
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
    "generate": {
        "enabled": False,
        # After scrape's default window (09:00-12:00), before post_comments'
        # afternoon window (14:00-17:00) - "generate runs after scrape in the
        # day's order" (Dispatch 37), expressed as window placement rather
        # than a hard dependency the engine enforces (see module docstring).
        "windows": [["12:30", "13:30"]],
        "max_comments": GENERATE_DEFAULT_CAP,
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


def merge_scheduler_config(section: dict) -> dict:
    """Fill a profile's raw ``scheduler`` section against :data:`DEFAULT_SCHEDULER`."""
    return _deep_merge(DEFAULT_SCHEDULER, section or {})


def _parse_hhmm(value: str):
    """Parse ``"HH:MM"`` into ``(hour, minute)``; tolerate bad input as midnight."""
    try:
        hh, mm = str(value).split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except (ValueError, AttributeError):
        logger.warning("Scheduler: bad time %r, treating as 00:00", value)
        return 0, 0


def _default_list_profiles():
    """Names of all configured profiles (used when no override is injected)."""
    try:
        return list(pm.list_profiles().get("profiles", {}).keys())
    except Exception:
        logger.exception("Scheduler: could not list profiles")
        return []


# ─── Scheduler engine ─────────────────────────────────────────────────────────

class Scheduler:
    """Randomized twice-daily scheduler running as one background thread.

    All external effects are injected so the engine is fully unit-testable:

    * ``now_fn`` — clock, defaults to ``datetime.now``.
    * ``rng`` — a ``random.Random``-like object (needs ``randint``/``random``).
    * ``submit_post_job(profile, comments_file, count) -> job_id | None`` — start
      a browser posting job (returns ``None`` if the browser lock is held).
    * ``submit_scrape_job(profile, max_posts, min_quality) -> job_id | None``.
    * ``submit_generate_job(profile, cap) -> job_id | None`` — start an API
      (non-browser) generate job capped at ``cap`` drafts.
    * ``browser_available(profile) -> bool`` — the browser-task lock check.
    * ``list_profiles() -> list[str]`` and ``config_fn(profile) -> dict``.
    """

    def __init__(self, *, now_fn=None, rng=None, tick_seconds=TICK_SECONDS,
                 min_gap_minutes=MIN_GAP_MINUTES, submit_post_job=None,
                 submit_scrape_job=None, submit_generate_job=None,
                 auto_mode_failure_threshold=AUTO_MODE_FAILURE_THRESHOLD,
                 browser_available=None, list_profiles=None, config_fn=None):
        self._now = now_fn or datetime.now
        self._rng = rng or random
        self._tick_seconds = tick_seconds
        self._min_gap = min_gap_minutes
        self._submit_post_job = submit_post_job
        self._submit_scrape_job = submit_scrape_job
        self._submit_generate_job = submit_generate_job
        self._auto_mode_threshold = auto_mode_failure_threshold
        self._browser_available = browser_available or (lambda profile: True)
        self._list_profiles = list_profiles or _default_list_profiles
        self._config_fn = config_fn or pm.get_profile_config

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {}  # profile -> {"slots", "last_fire", "recent_runs", "auto_mode_failures"}

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
        cfg = merge_scheduler_config((self._config_fn(profile) or {}).get("scheduler"))
        if not cfg.get("enabled"):
            return
        state = self._profile_state(profile)
        for job_type in JOB_TYPES:
            job_cfg = cfg.get(job_type, {})
            if not job_cfg.get("enabled"):
                continue
            # Generate never fires from the unattended tick loop without
            # auto_mode, regardless of its own enabled flag - this is the
            # one gate that distinguishes it from scrape/post_comments, which
            # already ran unattended under the plain master+job enabled flags
            # before auto_mode existed (Audit 36) and keep doing so unchanged.
            # run_now() bypasses this deliberately, same as it already
            # bypasses enabled - see run_now's docstring.
            if job_type == "generate" and not cfg.get("auto_mode"):
                continue
            for idx, window in enumerate(job_cfg.get("windows") or []):
                slot = self._slot(state, job_type, idx, window)
                self._ensure_rolled(profile, state, slot, job_cfg, now)
                self._maybe_fire(profile, state, slot, job_type, job_cfg, now)

    # ── per-slot state ────────────────────────────────────────────────────────

    def _profile_state(self, profile):
        st = self._state.get(profile)
        if st is None:
            st = {"slots": {}, "last_fire": None, "recent_runs": [],
                  "auto_mode_failures": {}}
            self._state[profile] = st
        return st

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

        If the freshly rolled time is already in the past (the dashboard started
        after the window, or after that minute), the slot is MISSED — we never
        fire late (no catch-up). Otherwise it is PENDING.
        """
        today = now.date()
        start_h, start_m = _parse_hhmm(slot["window"][0])
        end_h, end_m = _parse_hhmm(slot["window"][1])
        start_dt = datetime.combine(today, dtime(start_h, start_m))
        end_dt = datetime.combine(today, dtime(end_h, end_m))
        if end_dt < start_dt:
            end_dt = start_dt
        span = int((end_dt - start_dt).total_seconds())
        offset = self._rng.randint(0, span) if span > 0 else 0
        slot["fire_at"] = start_dt + timedelta(seconds=offset)
        slot["will_skip"] = self._rng.random() < float(job_cfg.get("skip_chance", 0.1))
        slot["rolled_date"] = today.isoformat()
        slot["rolled_window"] = list(slot["window"])
        slot["status"] = MISSED if slot["fire_at"] <= now else PENDING

    def _ensure_rolled(self, profile, state, slot, job_cfg, now):
        """Roll the slot if it hasn't been rolled today (or its window changed)."""
        today = now.date().isoformat()
        if slot["rolled_date"] == today and slot["rolled_window"] == slot["window"]:
            return
        self._roll(slot, job_cfg, now)
        logger.info(
            "Scheduler[%s] rolled %s window %s (%s-%s): fire_at=%s skip=%s status=%s",
            profile, slot["job_type"], slot["window_index"],
            slot["window"][0], slot["window"][1],
            slot["fire_at"].strftime("%H:%M:%S"), slot["will_skip"], slot["status"])
        if slot["status"] == MISSED:
            self._record(state, slot, now, action=ACTION_MISSED,
                         note="dashboard not running at rolled time")

    def _maybe_fire(self, profile, state, slot, job_type, job_cfg, now):
        """Fire (or skip/defer) a pending slot whose rolled time has arrived."""
        if slot["status"] != PENDING or now < slot["fire_at"]:
            return

        if slot["will_skip"]:
            slot["status"] = SKIPPED
            logger.info("Scheduler[%s] %s window %s SKIPPED (random skip roll)",
                        profile, job_type, slot["window_index"])
            self._record(state, slot, now, action=ACTION_SKIPPED)
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

    # ── actions ───────────────────────────────────────────────────────────────

    def _fire(self, profile, job_type, job_cfg, now):
        if job_type == "post_comments":
            return self._fire_post_comments(profile, job_cfg, now)
        if job_type == "scrape":
            return self._fire_scrape(profile, job_cfg, now)
        if job_type == "generate":
            return self._fire_generate(profile, job_cfg, now)
        return {"action": "error", "count": 0, "note": f"unknown job {job_type}"}

    def _fire_post_comments(self, profile, job_cfg, now):
        """Post a random count of GENERATED drafts; no-op when the queue is empty."""
        store = post_store.load_synced_store(profile)
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
        comments_file = self._write_comments_file(profile, generated[:count], now)

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

    def _fire_generate(self, profile, job_cfg, now):
        """Generate drafts for NEW posts, capped at ``max_comments`` per cycle.

        The cap is a ceiling passed to the real generate job, not a promise
        that exactly that many get produced - the generator's own evaluator
        can still turn posts down. ``count`` here (and in the recorded run)
        is "how many we asked for", matching how ``_fire_scrape`` already
        reports ``max_posts`` rather than a verified result (neither function
        waits for its real job to finish - see the module docstring).
        """
        store = post_store.load_synced_store(profile)
        new_posts = store.by_status(post_store.NEW)
        if not new_posts:
            logger.info("Scheduler[%s] generate: No NEW posts, "
                        "skipping scheduled run", profile)
            return {"action": ACTION_QUEUE_EMPTY, "count": 0,
                    "note": "no NEW posts"}

        cap = int(job_cfg.get("max_comments", GENERATE_DEFAULT_CAP))
        count = min(cap, len(new_posts))

        job_id = None
        if self._submit_generate_job:
            job_id = self._submit_generate_job(profile, count)
        logger.info("Scheduler[%s] generate FIRED: cap=%s new_posts=%s job=%s",
                    profile, count, len(new_posts), job_id)
        return {"action": ACTION_FIRED, "count": count, "job_id": job_id,
                "note": None if job_id else "not submitted"}

    # ── auto_mode circuit breaker ─────────────────────────────────────────────
    #
    # Real scrape/generate jobs run as background subprocesses this engine
    # does not wait on (same "never catches up" design as the rest of the
    # file) - so unlike everything above, these two are fed a real outcome
    # from OUTSIDE the tick/fire path, asynchronously, once the dashboard's
    # job actually finishes. They are safe to call whether or not auto_mode
    # is on: the circuit breaker only polices the unattended chain, so both
    # are a no-op unless this profile's auto_mode is currently True.

    def report_generate_outcome(self, profile, success, note=None):
        """Feed back a real (possibly async) generate job's success/failure."""
        with self._lock:
            cfg = merge_scheduler_config((self._config_fn(profile) or {}).get("scheduler"))
            if not cfg.get("auto_mode"):
                return
            self._score_auto_mode_cycle(profile, "generate", success, note)

    def report_scrape_outcome(self, profile, post_count, note=None):
        """Feed back a real (possibly async) scrape job's post count.

        ``post_count <= 0`` (including a scrape that errored before ever
        producing a count) scores as a failed cycle for the circuit breaker -
        "scrape returns zero posts" is the dispatch's own wording, and a
        crashed scrape produced exactly as little.
        """
        with self._lock:
            cfg = merge_scheduler_config((self._config_fn(profile) or {}).get("scheduler"))
            if not cfg.get("auto_mode"):
                return
            success = post_count > 0
            self._score_auto_mode_cycle(
                profile, "scrape", success,
                note or (None if success else "scrape returned 0 posts"))

    def _score_auto_mode_cycle(self, profile, job_type, success, note):
        """Update the per-job consecutive-failure counter; disable auto_mode
        at the threshold. Caller already checked auto_mode is on and holds
        ``self._lock`` (an ``RLock``, so this re-entrant acquire is fine)."""
        state = self._profile_state(profile)
        counters = state.setdefault("auto_mode_failures", {})
        if success:
            counters[job_type] = 0
            return
        counters[job_type] = counters.get(job_type, 0) + 1
        count = counters[job_type]
        logger.warning(
            "Scheduler[%s] auto_mode: %s failed (%d/%d consecutive) - %s",
            profile, job_type, count, self._auto_mode_threshold,
            note or "no detail")
        if count >= self._auto_mode_threshold:
            self._disable_auto_mode(profile, job_type, count, note)

    def _disable_auto_mode(self, profile, job_type, count, note):
        """Turn auto_mode off and persist why - the dashboard reads this back
        from config, not from the log line above. It does not loop quietly."""
        cfg = self._config_fn(profile) or {}
        sched = merge_scheduler_config(cfg.get("scheduler"))
        sched["auto_mode"] = False
        reason = f"{job_type} failed {count} consecutive cycles"
        if note:
            reason += f": {note}"
        sched["auto_mode_stopped_reason"] = reason
        sched["auto_mode_stopped_at"] = self._now().isoformat()
        cfg["scheduler"] = sched
        pm.save_profile_config(profile, cfg)
        logger.error("Scheduler[%s] AUTO MODE DISABLED ITSELF: %s", profile, reason)
        self._profile_state(profile)["auto_mode_failures"][job_type] = 0

    def _write_comments_file(self, profile, records, now):
        """Serialize GENERATED records to a poster-readable TXT file."""
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
        today's rolled fire time + skip flag, count ranges, and recent runs."""
        with self._lock:
            now = self._now()
            cfg = merge_scheduler_config((self._config_fn(profile) or {}).get("scheduler"))
            state = self._profile_state(profile)
            jobs = {}
            for job_type in JOB_TYPES:
                job_cfg = cfg.get(job_type, {})
                windows = []
                for idx, window in enumerate(job_cfg.get("windows") or []):
                    slot = self._slot(state, job_type, idx, window)
                    if cfg.get("enabled") and job_cfg.get("enabled"):
                        self._ensure_rolled(profile, state, slot, job_cfg, now)
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
                    "windows": windows,
                    "count_min": job_cfg.get("count_min"),
                    "count_max": job_cfg.get("count_max"),
                    "max_posts": job_cfg.get("max_posts"),
                    "min_quality": job_cfg.get("min_quality"),
                    "max_comments": job_cfg.get("max_comments"),
                    "skip_chance": job_cfg.get("skip_chance"),
                    "last_run": last_run,
                }
            return {
                "running": self.is_running(),
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
        """
        if job_type not in JOB_TYPES:
            return {"action": "error", "count": 0, "note": f"unknown job {job_type}"}
        with self._lock:
            now = self._now()
            cfg = merge_scheduler_config((self._config_fn(profile) or {}).get("scheduler"))
            result = self._fire(profile, job_type, cfg.get(job_type, {}), now)
            state = self._profile_state(profile)
            slot = {"job_type": job_type, "window_index": -1,
                    "window": None, "fire_at": now}
            self._record(state, slot, now, manual=True, **result)
            return result

    def toggle(self, profile, target, enabled):
        """Enable/disable the master switch, ``auto_mode``, or a specific job,
        persisting config.

        ``auto_mode`` is "a single toggle that enables the chain" (Dispatch
        37): turning it on also flips the master switch and all three jobs'
        ``enabled`` flags in the same write, rather than requiring four
        separate toggle calls. Turning it off only clears ``auto_mode``
        itself (and any stale self-disable reason) - it does not forcibly
        turn scrape/post_comments back off, since those have their own
        independent toggles and may have been running before auto_mode was
        ever touched.
        """
        with self._lock:
            cfg = self._config_fn(profile) or {}
            sched = merge_scheduler_config(cfg.get("scheduler"))
            if target == "master":
                sched["enabled"] = bool(enabled)
            elif target == "auto_mode":
                sched["auto_mode"] = bool(enabled)
                if enabled:
                    sched["enabled"] = True
                    for job_type in JOB_TYPES:
                        sched.setdefault(job_type, {})["enabled"] = True
                sched.pop("auto_mode_stopped_reason", None)
                sched.pop("auto_mode_stopped_at", None)
            elif target in JOB_TYPES:
                sched.setdefault(target, {})["enabled"] = bool(enabled)
            else:
                raise ValueError(f"unknown toggle target {target!r}")
            cfg["scheduler"] = sched
            pm.save_profile_config(profile, cfg)
            if target == "auto_mode":
                # A manual flip (either direction) starts the circuit
                # breaker's counters fresh - most relevant when re-enabling
                # after a self-disable, so it isn't tripped again by stale
                # counts from before the reason was even surfaced.
                self._profile_state(profile)["auto_mode_failures"] = {}
            return sched

    def update_config(self, profile, patch):
        """Deep-merge a partial scheduler config (windows/counts/etc.) and persist."""
        with self._lock:
            cfg = self._config_fn(profile) or {}
            sched = _deep_merge(merge_scheduler_config(cfg.get("scheduler")), patch or {})
            cfg["scheduler"] = sched
            pm.save_profile_config(profile, cfg)
            return sched
