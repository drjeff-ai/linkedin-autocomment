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

**One check covers every channel of the profile.** Each declared channel
(:data:`channels.REGISTRY`) with HELD rows is checked in turn, with its own
state file, its own Buffer channel and its own key. The switch, the interval,
the busy check and the master switch stay per PROFILE, and every channel that
has room goes into ONE feed job, so one check can feed LinkedIn and X both
without the second being turned away as "busy" by the first.

Before any Buffer call for a channel - including the slot read - the injected
``gate_fn`` resolves that channel's settings and applies the same production
gate as the Schedule button. A refusal is recorded as an error run naming the
platform, and nothing goes to Buffer.
"""

import copy
import logging
import threading
from datetime import datetime, timedelta

from . import channels
from . import platforms
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

#: When one check covers several channels, the summary it returns names the
#: action that matters most. A feed outranks an error only because both are
#: recorded per channel in recent_runs; the summary is for the button's answer.
_SUMMARY_PRIORITY = (ACTION_FED, ACTION_ERROR, ACTION_BUSY, ACTION_NO_SLOTS,
                     ACTION_NOTHING_HELD)
_QUIET_ACTIONS = (ACTION_NOTHING_HELD, ACTION_NO_SLOTS, ACTION_BUSY)


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


def _summarize(outcomes):
    """One answer for a check that may have covered several channels.

    A single outcome is returned as it is. Several are folded into one dict
    whose ``action`` is the most significant, with counts summed and every
    channel's outcome kept under ``by_platform``. None at all means nothing
    was held on any channel.
    """
    if not outcomes:
        return {"action": ACTION_NOTHING_HELD, "count": 0, "held": 0,
                "platform": None, "note": "nothing held"}
    if len(outcomes) == 1:
        return outcomes[0]
    actions = [o.get("action") for o in outcomes]
    action = next((a for a in _SUMMARY_PRIORITY if a in actions), actions[0])
    return {
        "action": action,
        "count": sum(o.get("count") or 0 for o in outcomes),
        "held": sum(o.get("held") or 0 for o in outcomes),
        "platform": None,
        "note": "; ".join("%s: %s" % (o.get("platform"),
                                      o.get("note") or o.get("action"))
                          for o in outcomes),
        "by_platform": outcomes,
    }


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
    * ``platforms_fn() -> list[str]`` — the channels to check, defaults to
      every id in :data:`channels.REGISTRY`.
    * ``channel_fn(profile, platform) -> str | None`` — that channel's Buffer
      channel id. May raise when the platform has no settings.
    * ``held_fn(profile, platform) -> list[dict]`` — HELD source rows from that
      platform's state, earliest first.
    * ``gate_fn(profile, platform) -> key | None`` — resolves the platform's
      Buffer key and applies the production gate. Raises to refuse; runs
      before ANY Buffer call for that platform.
    * ``slots_fn(channel_id, key=...) -> {"limit","used","free",...}`` —
      org-wide count, read with that platform's key.
    * ``submit_feed(profile, feeds) -> job_id | None`` — start ONE feed for
      every channel in ``feeds`` (``[{"platform", "channel_id", "free"}]``);
      returns ``None`` when it declined (something else is scheduling).
    * ``scheduling_available(profile) -> bool`` — false while a schedule-button
      or drain job is in flight for this profile.
    """

    def __init__(self, *, now_fn=None, tick_seconds=TICK_SECONDS,
                 list_profiles=None, config_fn=None, channel_fn=None,
                 held_fn=None, slots_fn=None, submit_feed=None,
                 scheduling_available=None, gate_fn=None, platforms_fn=None):
        self._now = now_fn or datetime.now
        self._tick_seconds = tick_seconds
        self._list_profiles = list_profiles or _default_list_profiles
        self._config_fn = config_fn or pm.get_profile_config
        self._platforms_fn = platforms_fn or (lambda: list(channels.REGISTRY))
        self._channel_fn = channel_fn or (lambda profile, platform: None)
        self._held_fn = held_fn or (lambda profile, platform: [])
        self._gate_fn = gate_fn or (lambda profile, platform: None)
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
        full_config = self._config_fn(profile) or {}
        # The per-profile master switch, ahead of the drain's own flag.
        if not pm.automations_enabled(full_config):
            return
        cfg = drain_config(full_config)
        if not cfg.get("enabled"):
            return
        state = self._profile_state(profile)
        if not self._due(state, cfg, now):
            return
        self._check(profile, state, now)

    def _check(self, profile, state, now, manual=False):
        """One drain check over every channel. Returns one summary outcome."""
        return _summarize(self._check_channels(profile, state, now, manual))

    def _check_channels(self, profile, state, now, manual=False):
        """One drain check, per channel. Records only what happened or broke.

        ``last_check`` is stamped whatever the outcome, so the interval advances
        even on a quiet check and the UI can say when it last looked. Returns
        the per-channel outcomes; an empty list means nothing was held anywhere.
        """
        state["last_check"] = now
        outcomes = []
        feeds = []
        for platform in self._platforms_fn():
            outcome, feed = self._check_platform(profile, platform, state, now,
                                                 manual)
            if outcome:
                outcomes.append(outcome)
            if feed:
                feeds.append(feed)
        if feeds:
            outcomes.extend(self._feed(profile, feeds, state, now, manual))
        return outcomes

    def _check_platform(self, profile, platform, state, now, manual):
        """Check one channel. Returns ``(outcome, feed)``; at most one is set.

        No outcome and no feed means nothing is held on this channel, which is
        not worth a record.
        """
        # Cheapest test first: no held rows means no reason to call Buffer at
        # all. A drain that polls the API every interval forever, for a queue
        # with nothing in it, is just rate-limit consumption.
        try:
            held = list(self._held_fn(profile, platform) or [])
        except Exception as exc:
            logger.exception("PostDrain[%s/%s] could not read held rows",
                             profile, platform)
            return self._record(state, now, platform, action=ACTION_ERROR,
                                note="could not read held rows: %s" % exc,
                                manual=manual), None
        if not held:
            return None, None

        try:
            channel_id = self._channel_fn(profile, platform)
        except Exception as exc:
            # An X row held for a profile with no X settings: refused, never
            # sent to LinkedIn's channel instead.
            return self._record(state, now, platform, action=ACTION_ERROR,
                                held=len(held),
                                note="%s settings could not be resolved: %s"
                                % (platform, exc), manual=manual), None
        if not channel_id:
            return self._record(state, now, platform, action=ACTION_ERROR,
                                held=len(held),
                                note="no Buffer channel configured",
                                manual=manual), None

        # The same gate the Schedule button applies, BEFORE any Buffer call -
        # the slot read below included. An unresolvable identity is refused.
        try:
            key = self._gate_fn(profile, platform)
        except Exception as exc:
            logger.warning("PostDrain[%s/%s] refused: %s", profile, platform, exc)
            return self._record(state, now, platform, action=ACTION_ERROR,
                                held=len(held), note="refused: %s" % exc,
                                manual=manual), None

        # Never run two schedule passes for one profile at once. Both this and
        # the Schedule button call schedule_pass against the same state file;
        # overlapping runs could each read a row as HELD before either wrote,
        # and create two Buffer posts for it. There is no unpublishing.
        if not self._scheduling_available(profile):
            logger.debug("PostDrain[%s] waiting: another schedule job is running",
                         profile)
            return {"action": ACTION_BUSY, "count": 0, "held": len(held),
                    "platform": platform,
                    "note": "another schedule job is running"}, None

        try:
            slots = self._slots_fn(channel_id, key=key)
        except Exception as exc:
            logger.warning("PostDrain[%s/%s] could not read Buffer slots: %s",
                           profile, platform, exc)
            return self._record(state, now, platform, action=ACTION_ERROR,
                                held=len(held),
                                note="could not read Buffer's slot count: %s" % exc,
                                manual=manual), None

        free = int(slots.get("free") or 0)
        if free <= 0:
            logger.debug("PostDrain[%s/%s] no slots free (%s of %s used)",
                         profile, platform, slots.get("used"), slots.get("limit"))
            return {"action": ACTION_NO_SLOTS, "count": 0, "held": len(held),
                    "platform": platform, "slots": slots,
                    "note": "no slot free"}, None

        return None, {"platform": platform, "channel_id": channel_id,
                      "free": free, "held": len(held), "slots": slots}

    def _feed(self, profile, feeds, state, now, manual):
        """Submit ONE job for every channel with room, and record each."""
        job_id = None
        if self._submit_feed:
            job_id = self._submit_feed(
                profile, [{"platform": f["platform"],
                           "channel_id": f["channel_id"], "free": f["free"]}
                          for f in feeds])
        if job_id is None:
            # The executor declined - almost always the same busy check, lost
            # narrowly. Not an error; the next interval retries.
            return [{"action": ACTION_BUSY, "count": 0, "held": f["held"],
                     "platform": f["platform"], "note": "feed not submitted"}
                    for f in feeds]

        state["last_feed"] = now
        entries = []
        for f in feeds:
            count = min(f["free"], f["held"])
            logger.info("PostDrain[%s/%s] feeding %d held post(s) into %d free "
                        "slot(s) (job=%s)", profile, f["platform"], count,
                        f["free"], job_id)
            entries.append(self._record(state, now, f["platform"],
                                        action=ACTION_FED, count=count,
                                        held=f["held"], slots=f["slots"],
                                        job_id=job_id, manual=manual))
        return entries

    def _record(self, state, now, platform=None, action=ACTION_ERROR, count=0,
                held=0, slots=None, job_id=None, note=None, manual=False):
        entry = {
            "at": now.isoformat(),
            "platform": platform,
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
            full_config = self._config_fn(profile) or {}
            automations = pm.automations_enabled(full_config)
            cfg = drain_config(full_config)
            state = self._profile_state(profile)
            interval = self._interval(cfg)
            last_check = state["last_check"]
            next_check = (last_check + interval) if last_check else None

            held_count = 0
            holding = []
            for platform in self._platforms_fn():
                try:
                    count = len(list(self._held_fn(profile, platform) or []))
                except Exception:
                    logger.exception("PostDrain: could not read held rows for "
                                     "%s/%s", profile, platform)
                    continue
                held_count += count
                if count:
                    holding.append(platform)

            slots = None
            if include_slots and automations and cfg.get("enabled"):
                slots = self._status_slots(profile, holding)

            return {
                "running": self.is_running(),
                "automations_enabled": automations,
                "enabled": bool(cfg.get("enabled")),
                "interval_minutes": cfg.get("interval_minutes"),
                "now": now.isoformat(),
                "held_count": held_count,
                "last_check_at": last_check.isoformat() if last_check else None,
                "last_feed_at": (state["last_feed"].isoformat()
                                 if state["last_feed"] else None),
                "next_check_at": next_check.isoformat() if next_check else None,
                "slots": slots,
                "recent_runs": list(reversed(state["recent_runs"])),
            }

    def _status_slots(self, profile, holding):
        """The slot count for the panel: the first channel holding rows, else
        the default channel. Gated like a check, so a refused channel is never
        read; any failure leaves the count unknown (None), never zero."""
        platform = holding[0] if holding else platforms.DEFAULT
        if not self._slots_fn:
            return None
        try:
            channel_id = self._channel_fn(profile, platform)
            if not channel_id:
                return None
            key = self._gate_fn(profile, platform)
            return self._slots_fn(channel_id, key=key)
        except Exception as exc:
            logger.debug("PostDrain: slot read failed for %s/%s: %s",
                         profile, platform, exc)
            return None

    def run_now(self, profile):
        """Check immediately, ignoring the interval. Returns the outcome dict.

        Ignores the interval, never the per-profile master switch: while that
        is off nothing is checked, so not even Buffer's slot count is read.
        """
        with self._lock:
            if not pm.automations_enabled(self._config_fn(profile) or {}):
                return {"action": pm.AUTOMATIONS_DISABLED_ACTION, "count": 0,
                        "note": pm.automations_disabled_message(profile)}
            now = self._now()
            state = self._profile_state(profile)
            outcomes = self._check_channels(profile, state, now, manual=True)
            result = _summarize(outcomes)
            # A quiet manual check still deserves a visible record - the person
            # pressed a button and is owed an answer. One per channel, so the
            # record says which channel was quiet and why.
            for outcome in (outcomes or [result]):
                if outcome.get("action") in _QUIET_ACTIONS:
                    self._record(state, now, manual=True, **outcome)
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
