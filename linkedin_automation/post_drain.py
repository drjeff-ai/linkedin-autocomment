"""Background feeder that pushes HELD scheduled posts into Buffer as slots free.

Buffer's free plan caps how many posts may be SCHEDULED (not yet published) at
once, org-wide. A calendar larger than that cap cannot all go in at once: Phase
A sends what fits and marks the remainder HELD. Those rows are correct and
already confirmed — they are waiting on a slot, not on a person.

This module is the thing that stops "waiting on a slot" from meaning "waiting
for you to remember to click Schedule again". One background thread ticks every
:data:`TICK_SECONDS`; for each profile whose drain is enabled and whose interval
has elapsed, it asks Buffer how many slots are free and feeds that many HELD
rows in, earliest first.

**It feeds HELD rows only, never PENDING ones.** A PENDING row has never been
through the confirmation dialog that names the account it will post as. A HELD
row has, and was stopped only by the plan limit. Anything else would mean a
background thread publishing posts to a real LinkedIn account on a timer that
nobody ever confirmed — see :func:`csv_pipeline.held_rows`.

Sibling to :mod:`scheduler` (the comment sweeper) but deliberately NOT a job
type inside it: the sweeper's semantics are randomized daily windows with a
skip chance, tuned so comment activity does not look mechanical. Feeding a slot
that Buffer has just freed has no such requirement — it wants a plain interval
and no random skipping, because a skipped drain silently leaves a post
unscheduled. Two different jobs, two different engines, two separate switches.

Everything external is injected (clock, profile list, config, the Buffer slot
read, the executor) so the whole engine is unit-testable with no network.
"""

import copy
import logging
import threading
from datetime import datetime, timedelta

from . import profile_manager as pm

logger = logging.getLogger(__name__)

# ─── Tunables ─────────────────────────────────────────────────────────────────

TICK_SECONDS = 30          # how often the loop re-evaluates
MAX_RECENT_RUNS = 50       # per-profile ring buffer of feed records for the UI
#: Every check costs a Buffer request, and the free plan allows 3,000 per 30
#: days - about a hundred a DAY for everything, including scheduling itself.
#: At 15 minutes this feeder alone ran 96 checks a day and, before the channel
#: lookup was cached, spent two requests on each: roughly twice the entire
#: monthly budget, which is how the account ended up rate-limited. Thirty
#: minutes with a cached channel is ~48 requests a day, which leaves room for
#: the work the feeder exists to enable.
DEFAULT_INTERVAL_MINUTES = 30

#: Job category used when the drain submits work. Distinct from the schedule
#: button's "buffer_schedule" so the two are separable in the jobs list — but
#: see ``can_start_scheduling_task`` in the dashboard: they must never run at
#: the same time for one profile.
JOB_CATEGORY = "post_drain"

DEFAULT_DRAIN = {
    "enabled": False,
    "interval_minutes": DEFAULT_INTERVAL_MINUTES,
}

# What a check actually resolved to.
ACTION_FED = "fed"                    # rows were pushed into Buffer
ACTION_NO_SLOTS = "no_slots"          # held rows exist, Buffer is full
ACTION_NOTHING_HELD = "nothing_held"  # slots may be free, nothing waiting
ACTION_BUSY = "busy"                  # a schedule/drain job is already running
ACTION_ERROR = "error"                # the slot read or the feed blew up


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge ``override`` onto ``base``; both deep-copied.

    Deep on purpose, for the same reason as the scheduler's: a shallow copy
    leaves untouched sections aliasing :data:`DEFAULT_DRAIN`, and callers mutate
    the result. One profile enabling the drain would enable it for every profile
    that had never configured one — a background thread posting to accounts
    nobody switched on.
    """
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_drain_config(section: dict) -> dict:
    """Fill a profile's raw ``scheduled_posting.drain`` section with defaults."""
    return _deep_merge(DEFAULT_DRAIN, section or {})


def drain_config(profile_cfg: dict) -> dict:
    """Pull the drain section out of a whole profile config."""
    scheduled = (profile_cfg or {}).get("scheduled_posting") or {}
    return merge_drain_config(scheduled.get("drain"))


def _default_list_profiles():
    try:
        return list(pm.list_profiles().get("profiles", {}).keys())
    except Exception:
        logger.exception("PostDrain: could not list profiles")
        return []


class PostDrain:
    """Feeds HELD posts into Buffer as scheduled-post slots free up.

    Injected collaborators:

    * ``now_fn`` — clock, defaults to ``datetime.now``.
    * ``list_profiles() -> list[str]`` and ``config_fn(profile) -> dict``.
    * ``channel_fn(profile) -> str | None`` — the profile's Buffer channel id.
    * ``held_fn(profile) -> list[dict]`` — HELD source rows, earliest first.
    * ``slots_fn(channel_id) -> {"limit","used","free",...}`` — org-wide count.
    * ``submit_feed(profile, channel_id, free) -> job_id | None`` — start the
      feed; returns ``None`` when it declined (something else is scheduling).
    * ``scheduling_available(profile) -> bool`` — false while a schedule-button
      or drain job is in flight for this profile.
    """

    def __init__(self, *, now_fn=None, tick_seconds=TICK_SECONDS,
                 list_profiles=None, config_fn=None, channel_fn=None,
                 held_fn=None, slots_fn=None, submit_feed=None,
                 scheduling_available=None):
        self._now = now_fn or datetime.now
        self._tick_seconds = tick_seconds
        self._list_profiles = list_profiles or _default_list_profiles
        self._config_fn = config_fn or pm.get_profile_config
        self._channel_fn = channel_fn or (lambda profile: None)
        self._held_fn = held_fn or (lambda profile: [])
        self._slots_fn = slots_fn
        self._submit_feed = submit_feed
        self._scheduling_available = scheduling_available or (lambda profile: True)

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {}  # profile -> {"last_check", "last_feed", "recent_runs"}

    # ── thread lifecycle ──────────────────────────────────────────────────────

    def start(self):
        """Start the background loop (idempotent)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="post-drain", daemon=True)
            self._thread.start()
            logger.info("PostDrain started (tick=%ss)", self._tick_seconds)

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
                logger.exception("PostDrain tick failed")
            self._stop.wait(self._tick_seconds)
        logger.info("PostDrain loop exited")

    # ── evaluation ────────────────────────────────────────────────────────────

    def tick(self, now=None):
        """Evaluate every enabled profile once. Safe to call directly in tests."""
        now = now or self._now()
        for profile in self._list_profiles():
            with self._lock:
                try:
                    self._tick_profile(profile, now)
                except Exception:
                    logger.exception("PostDrain: profile %s tick failed", profile)

    def _profile_state(self, profile):
        st = self._state.get(profile)
        if st is None:
            st = {"last_check": None, "last_feed": None, "recent_runs": []}
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

    def _tick_profile(self, profile, now):
        cfg = drain_config(self._config_fn(profile) or {})
        if not cfg.get("enabled"):
            return
        state = self._profile_state(profile)
        if not self._due(state, cfg, now):
            return
        self._check(profile, state, now)

    def _check(self, profile, state, now, manual=False):
        """One drain check. Records only when something happened or broke.

        ``last_check`` is stamped whatever the outcome, so the interval advances
        even on a quiet check and the UI can say when it last looked.
        """
        state["last_check"] = now

        # Cheapest test first: no held rows means no reason to call Buffer at
        # all. A drain that polls the API every interval forever, for a queue
        # with nothing in it, is just rate-limit consumption.
        held = list(self._held_fn(profile) or [])
        if not held:
            return {"action": ACTION_NOTHING_HELD, "count": 0, "held": 0,
                    "note": "nothing held"}

        channel_id = self._channel_fn(profile)
        if not channel_id:
            return self._record(state, now, action=ACTION_ERROR, held=len(held),
                                note="no Buffer channel configured",
                                manual=manual)

        # Never run two schedule passes for one profile at once. Both this and
        # the Schedule button call schedule_pass against the same state file;
        # overlapping runs could each read a row as HELD before either wrote,
        # and create two Buffer posts for it. There is no unpublishing.
        if not self._scheduling_available(profile):
            logger.debug("PostDrain[%s] waiting: another schedule job is running",
                         profile)
            return {"action": ACTION_BUSY, "count": 0, "held": len(held),
                    "note": "another schedule job is running"}

        try:
            slots = self._slots_fn(channel_id)
        except Exception as exc:
            logger.warning("PostDrain[%s] could not read Buffer slots: %s",
                           profile, exc)
            return self._record(state, now, action=ACTION_ERROR, held=len(held),
                                note="could not read Buffer's slot count: %s" % exc,
                                manual=manual)

        free = int(slots.get("free") or 0)
        if free <= 0:
            logger.debug("PostDrain[%s] no slots free (%s of %s used)",
                         profile, slots.get("used"), slots.get("limit"))
            return {"action": ACTION_NO_SLOTS, "count": 0, "held": len(held),
                    "slots": slots, "note": "no slot free"}

        count = min(free, len(held))
        job_id = None
        if self._submit_feed:
            job_id = self._submit_feed(profile, channel_id, free)
        if job_id is None:
            # The executor declined - almost always the same busy check, lost
            # narrowly. Not an error; the next interval retries.
            return {"action": ACTION_BUSY, "count": 0, "held": len(held),
                    "note": "feed not submitted"}

        state["last_feed"] = now
        logger.info("PostDrain[%s] feeding %d held post(s) into %d free slot(s) "
                    "(job=%s)", profile, count, free, job_id)
        return self._record(state, now, action=ACTION_FED, count=count,
                            held=len(held), slots=slots, job_id=job_id,
                            manual=manual)

    def _record(self, state, now, action, count=0, held=0, slots=None,
                job_id=None, note=None, manual=False):
        entry = {
            "at": now.isoformat(),
            "action": action,
            "count": count,
            "held": held,
            "free_slots": (slots or {}).get("free"),
            "slot_limit": (slots or {}).get("limit"),
            "job_id": job_id,
            "manual": manual,
            "note": note,
        }
        runs = state["recent_runs"]
        runs.append(entry)
        del runs[:-MAX_RECENT_RUNS]
        return entry

    # ── public API used by the dashboard endpoints ────────────────────────────

    def status(self, profile, include_slots=False):
        """Drain state for one profile.

        ``include_slots`` costs a Buffer round trip, so the polling status line
        does without it and the panel asks for it only when it refreshes.
        """
        with self._lock:
            now = self._now()
            cfg = drain_config(self._config_fn(profile) or {})
            state = self._profile_state(profile)
            interval = self._interval(cfg)
            last_check = state["last_check"]
            next_check = (last_check + interval) if last_check else None

            held = []
            try:
                held = list(self._held_fn(profile) or [])
            except Exception:
                logger.exception("PostDrain: could not read held rows for %s",
                                 profile)

            slots = None
            if include_slots and cfg.get("enabled"):
                channel_id = self._channel_fn(profile)
                if channel_id and self._slots_fn:
                    try:
                        slots = self._slots_fn(channel_id)
                    except Exception as exc:
                        logger.debug("PostDrain: slot read failed for %s: %s",
                                     profile, exc)

            return {
                "running": self.is_running(),
                "enabled": bool(cfg.get("enabled")),
                "interval_minutes": cfg.get("interval_minutes"),
                "now": now.isoformat(),
                "held_count": len(held),
                "last_check_at": last_check.isoformat() if last_check else None,
                "last_feed_at": (state["last_feed"].isoformat()
                                 if state["last_feed"] else None),
                "next_check_at": next_check.isoformat() if next_check else None,
                "slots": slots,
                "recent_runs": list(reversed(state["recent_runs"])),
            }

    def run_now(self, profile):
        """Check immediately, ignoring the interval. Returns the outcome dict."""
        with self._lock:
            now = self._now()
            state = self._profile_state(profile)
            result = self._check(profile, state, now, manual=True)
            # A quiet manual check still deserves a visible record - the person
            # pressed a button and is owed an answer.
            if result and result.get("action") in (ACTION_NOTHING_HELD,
                                                   ACTION_NO_SLOTS,
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
            drain = merge_drain_config(scheduled.get("drain"))
            drain["enabled"] = bool(enabled)
            scheduled["drain"] = drain
            cfg["scheduled_posting"] = scheduled
            pm.save_profile_config(profile, cfg)
            if enabled:
                # A freshly enabled drain checks on the next tick rather than
                # waiting out an interval that started before it was on.
                self._profile_state(profile)["last_check"] = None
            return drain

    def update_config(self, profile, patch):
        """Deep-merge a partial drain config (interval) and persist it."""
        with self._lock:
            cfg = self._config_fn(profile) or {}
            scheduled = cfg.get("scheduled_posting")
            if not isinstance(scheduled, dict):
                scheduled = {}
            drain = _deep_merge(merge_drain_config(scheduled.get("drain")),
                                patch or {})
            scheduled["drain"] = drain
            cfg["scheduled_posting"] = scheduled
            pm.save_profile_config(profile, cfg)
            return drain
