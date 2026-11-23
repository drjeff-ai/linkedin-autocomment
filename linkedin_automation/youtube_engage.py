"""YouTube commenting through the Data API (Y1, D-027, DEC-061).

Comments post as our channel on other creators' videos from a watch list Jeff
picks, and as replies under viewers' comments on our own Shorts. Every Google
call is in :mod:`youtube_api`; the quota ledger, token and production gate are
V1's (:mod:`youtube_channel`).

**The path, in four steps, each a function an E1 adapter can wrap:**

* :func:`find` lands candidates as NEW in the shared per-platform store
  (``data/<p>/youtube/posts_db.json``), keyed by videoId, or for a reply by the
  viewer's comment id. Watch-list channels' uploads playlists (1 unit a page),
  videos from the last 7 days; and our channel's comment threads
  (``allThreadsRelatedToChannelId``, 1 unit) with no reply from us.
* :func:`draft` writes each NEW one with the shared generator and the YouTube
  policy (the reply variant for replies). The input is the title, description,
  chapters and up to 20 top comments: nothing outside the API is fetched, no
  transcript. The result is GENERATED and waits in Review.
* :func:`engage` posts at most one approved comment per call. ``classify`` and
  ``already_commented`` run first; ``post_and_verify`` gives one of three
  outcomes. Nothing is retried.
* :func:`discover` suggests channels (``search.list``, 100 units) and adds
  nothing; :func:`add_to_watch_list` is the separate, explicit add.

**DEC-061 pins** are the constants below and the ``*_refusal`` functions. Each
refusal says what it was and, when it is pacing, when the next comment is
allowed. **Approval** is ``post_store.is_approved``, checked on a fresh read of
the store as the last step before the insert.

**Files**, beside V1's under ``data/<p>/youtube/``: ``posts_db.json`` (the
store) and ``engage_ledger.json`` (the comments we made, the ramp start, the
next allowed time, an unresolved attempt, cached uploads playlist ids). V1's
``youtube_state.json`` is read only, for our own videos' titles; the shared
``quota_ledger.json`` is charged before every call.
"""

import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from . import channels
from . import platform_policy
from . import platforms
from . import post_store
from . import profile_manager as pm
from . import publisher_lock
from . import youtube_api
from . import youtube_channel as yt

logger = logging.getLogger(__name__)

PLATFORM = platforms.YOUTUBE
PACIFIC = yt.PACIFIC

EXIT_OK = yt.EXIT_OK
EXIT_ERROR = yt.EXIT_ERROR
EXIT_REFUSED = yt.EXIT_REFUSED
EXIT_PRODUCTION_REFUSED = yt.EXIT_PRODUCTION_REFUSED

KIND_VIDEO = "video"    # a top-level comment on someone else's video
KIND_REPLY = "reply"    # a reply under a viewer's comment on our own video

# ── DEC-061 pins ────────────────────────────────────────────────────────────

#: Others' content, a day, counted from the first live comment on others'
#: content: ``(last day of the step, cap)``. After the last step, RAMP_MAX.
RAMP = ((7, 3), (14, 5), (21, 10))
RAMP_MAX = 20
#: Replies on our own videos, a day, separately.
REPLY_DAILY_CAP = 20
#: Comments only in [08:00, 21:00) Pacific.
WINDOW_START = time(8, 0)
WINDOW_END = time(21, 0)
#: A random gap between two comments, of either kind, in minutes.
GAP_MINUTES = (25, 90)
#: At most one comment per creator (the video's channel) in this time.
CREATOR_COOLDOWN = timedelta(days=3)

#: Find: videos and threads younger than this.
LOOKBACK = timedelta(days=7)
#: Find: pages of one uploads playlist (50 a page).
PLAYLIST_MAX_PAGES = 2
#: Dedupe: pages of the video's threads (or the thread's replies) to search.
DEDUPE_MAX_PAGES = 3
#: Draft: top comments given to the generator as context.
TOP_COMMENTS = 20
TOP_COMMENT_CHARS = 300

SEARCH_UNITS = 100
LIST_UNITS = 1

# ── outcomes ────────────────────────────────────────────────────────────────

POSTED_VISIBLE = "posted_visible"   # created, and in the public listing
POSTED_HELD = "posted_held"         # created (we have its id), not publicly listed
FAILED = "failed"                   # not created, or nobody can tell
ALREADY = "already_commented"       # dedupe found our comment; nothing inserted
OUTCOMES = (POSTED_VISIBLE, POSTED_HELD, FAILED)

#: TRASH reasons this module sets. Neither is an auto reason, so a re-find
#: never brings the record back; a person can restore it.
REASON_POST_FAILED = "youtube_post_failed"
REASON_AMBIGUOUS = "youtube_ambiguous"
REASON_COMMENTS_DISABLED = "youtube_comments_disabled"

# Refusals.
REFUSE_WINDOW = "outside_window"
REFUSE_GAP = "gap"
REFUSE_CAP = "daily_cap"
REFUSE_REPLY_CAP = "reply_daily_cap"
REFUSE_CREATOR = "creator_3_days"
REFUSE_ITEM = "already_commented_on_item"
REFUSE_PROMOTION = "promotion"
REFUSE_ATTEMPT = "unresolved_attempt"

ENGAGEMENT_KEY = "engagement"
ENGAGEMENT_KEYS = {"channel_name", "channel_handle", "watch_list"}
WATCH_KEYS = {"channel_id", "title", "subscribers", "added_at"}
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

LEDGER_NAME = "engage_ledger.json"


class EngageRefused(yt.YouTubeRefused):
    """A check failed before anything was sent."""


# ── config ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EngageConfig:
    profile: str
    channel_id: str          # ours, from the V1 overlay
    channel_name: str        # for the no-promotion validator
    channel_handle: str
    watch_list: tuple        # ({channel_id, title, subscribers, added_at}, ...)
    daily_limit: int
    comment_units: int


def _raw_engagement(cfg, profile):
    section = (cfg or {}).get("scheduled_posting")
    overlays = section.get("platforms") if isinstance(section, dict) else None
    overlay = overlays.get(PLATFORM) if isinstance(overlays, dict) else None
    raw = overlay.get(ENGAGEMENT_KEY) if isinstance(overlay, dict) else None
    return raw


def parse_engagement(raw, where="scheduled_posting.platforms.youtube.engagement"):
    """Validate an ``engagement`` block. Unknown keys refuse, as V1's do."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise EngageRefused("%s must be an object" % where)
    unknown = sorted(set(raw) - ENGAGEMENT_KEYS)
    if unknown:
        raise EngageRefused("%s has unknown keys: %s" % (where, ", ".join(unknown)))
    for key in ("channel_name", "channel_handle"):
        if key in raw and not isinstance(raw[key], str):
            raise EngageRefused("%s.%s must be text" % (where, key))
    watch = raw.get("watch_list", [])
    if not isinstance(watch, list):
        raise EngageRefused("%s.watch_list must be a list" % where)
    seen = set()
    for i, entry in enumerate(watch):
        if not isinstance(entry, dict):
            raise EngageRefused("%s.watch_list[%d] must be an object" % (where, i))
        bad = sorted(set(entry) - WATCH_KEYS)
        if bad:
            raise EngageRefused("%s.watch_list[%d] has unknown keys: %s"
                                % (where, i, ", ".join(bad)))
        cid = entry.get("channel_id")
        if not isinstance(cid, str) or not CHANNEL_ID_RE.match(cid):
            raise EngageRefused("%s.watch_list[%d].channel_id is not a channel id: %r"
                                % (where, i, cid))
        if cid in seen:
            raise EngageRefused("%s.watch_list lists %s twice" % (where, cid))
        seen.add(cid)
    return raw


def engage_config(profile, profile_cfg=None) -> EngageConfig:
    """V1's ``youtube_config`` (which refuses a missing or unknown key) plus
    its ``engagement`` block."""
    cfg = profile_cfg if profile_cfg is not None else pm.get_profile_config(profile)
    base = yt.youtube_config(profile, cfg)
    raw = parse_engagement(_raw_engagement(cfg, profile))
    return EngageConfig(
        profile=profile, channel_id=base.channel_id,
        channel_name=(raw.get("channel_name") or "").strip(),
        channel_handle=(raw.get("channel_handle") or "").strip(),
        watch_list=tuple(dict(e) for e in raw.get("watch_list", [])),
        daily_limit=base.daily_limit, comment_units=base.comment_units)


def _edit_engagement(profile, mutate):
    """Apply ``mutate(engagement_dict)`` to the profile's stored config and
    write it. The V1 overlay must already be in the stored file."""
    if profile not in pm.load_profiles()["profiles"]:
        raise EngageRefused("no profile %r is registered" % profile)
    stored = pm.read_stored_config(profile)
    overlay = (((stored.get("scheduled_posting") or {}).get("platforms") or {})
               .get(PLATFORM))
    if not isinstance(overlay, dict):
        raise EngageRefused("profile %r has no scheduled_posting.platforms.%s in "
                            "its config file" % (profile, PLATFORM))
    eng = overlay.get(ENGAGEMENT_KEY)
    if eng is None:
        eng = overlay[ENGAGEMENT_KEY] = {}
    parse_engagement(eng)
    result = mutate(eng)
    parse_engagement(eng)
    pm.save_profile_config(profile, stored)
    return result


def add_to_watch_list(profile, channel_id, title="", subscribers=None, now=None):
    """Add one channel. The explicit add: discover never calls this.
    Returns True when it was added, False when it was already listed."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(channel_id, str) or not CHANNEL_ID_RE.match(channel_id):
        raise EngageRefused("not a channel id: %r" % (channel_id,))
    if subscribers is not None and (isinstance(subscribers, bool)
                                    or not isinstance(subscribers, int)):
        raise EngageRefused("subscribers must be a whole number or empty")

    def mutate(eng):
        watch = eng.setdefault("watch_list", [])
        if any(e.get("channel_id") == channel_id for e in watch):
            return False
        watch.append({"channel_id": channel_id, "title": str(title or "")[:200],
                      "subscribers": subscribers, "added_at": yt.utc_z(now)})
        return True
    return _edit_engagement(profile, mutate)


def remove_from_watch_list(profile, channel_id):
    """Remove one channel. Returns True when it was listed."""
    def mutate(eng):
        watch = eng.get("watch_list") or []
        kept = [e for e in watch if e.get("channel_id") != channel_id]
        eng["watch_list"] = kept
        return len(kept) != len(watch)
    return _edit_engagement(profile, mutate)


# ── files ───────────────────────────────────────────────────────────────────

def ledger_path(profile):
    return os.path.join(yt.channel_dir(profile), LEDGER_NAME)


def empty_ledger():
    return {"version": 1, "ramp_started_at": None, "next_allowed_at": None,
            "attempting": None, "uploads_playlists": {}, "comments": []}


def load_ledger(profile):
    led = yt.read_json(ledger_path(profile), empty_ledger())
    for key, default in empty_ledger().items():
        led.setdefault(key, default)
    if not isinstance(led["comments"], list):
        raise EngageRefused("%s has no comments list" % ledger_path(profile))
    return led


def save_ledger(profile, ledger):
    yt.write_json_atomic(ledger_path(profile), ledger)


def load_store(profile):
    """The shared per-platform store, ``data/<p>/youtube/posts_db.json``. No
    LinkedIn reconcile runs on it (``load_synced_store`` injects none)."""
    return post_store.load_synced_store(profile, platform=PLATFORM)


def _parse(iso):
    return yt.parse_instant(iso) if iso else None


def _pt(dt):
    return dt.astimezone(PACIFIC)


def _pt_label(dt):
    return _pt(dt).strftime("%Y-%m-%d %H:%M PT") if dt else "-"


def _pt_at(day, t):
    return datetime.combine(day, t, tzinfo=PACIFIC).astimezone(timezone.utc)


# ── pacing (DEC-061) ────────────────────────────────────────────────────────

def ramp_day(ledger, now):
    """Day 1 is the Pacific date of the first live comment on others' content
    (or today, before there is one)."""
    start = _parse(ledger.get("ramp_started_at"))
    if start is None:
        return 1
    return (_pt(now).date() - _pt(start).date()).days + 1


def daily_cap(ledger, kind, now):
    if kind == KIND_REPLY:
        return REPLY_DAILY_CAP
    day = ramp_day(ledger, now)
    for last_day, cap in RAMP:
        if day <= last_day:
            return cap
    return RAMP_MAX


def _live(ledger):
    """Ledger entries that may be a comment on YouTube: created, or unknown."""
    return [c for c in ledger["comments"] if c.get("live")]


def used_today(ledger, kind, now):
    today = _pt(now).date()
    return sum(1 for c in _live(ledger)
               if c.get("kind") == kind and _pt(_parse(c["at"])).date() == today)


def next_window_open(now):
    local = _pt(now)
    if local.time() < WINDOW_START:
        return _pt_at(local.date(), WINDOW_START)
    return _pt_at(local.date() + timedelta(days=1), WINDOW_START)


def window_refusal(now):
    """``(reason, next_allowed)`` outside 08:00-21:00 Pacific, else None."""
    t = _pt(now).time()
    if WINDOW_START <= t < WINDOW_END:
        return None
    nxt = next_window_open(now)
    return ("%s: comments only 08:00-21:00 Pacific; it is %s"
            % (REFUSE_WINDOW, _pt_label(now)), nxt)


def gap_refusal(ledger, now):
    nxt = _parse(ledger.get("next_allowed_at"))
    if nxt and now < nxt:
        return ("%s: the random gap after the last comment runs to %s"
                % (REFUSE_GAP, _pt_label(nxt)), nxt)
    return None


def cap_refusal(ledger, kind, now):
    cap = daily_cap(ledger, kind, now)
    used = used_today(ledger, kind, now)
    if used < cap:
        return None
    nxt = _pt_at(_pt(now).date() + timedelta(days=1), WINDOW_START)
    if kind == KIND_REPLY:
        return ("%s: %d of %d replies on our own videos today"
                % (REFUSE_REPLY_CAP, used, cap), nxt)
    return ("%s: %d of %d comments on others' videos today (ramp day %d)"
            % (REFUSE_CAP, used, cap, ramp_day(ledger, now)), nxt)


def creator_refusal(ledger, creator, now):
    """One comment per creator per 3 days, on others' content."""
    if not creator:
        return None
    last = None
    for c in _live(ledger):
        if c.get("kind") == KIND_VIDEO and c.get("creator") == creator:
            at = _parse(c["at"])
            last = at if last is None or at > last else last
    if last is not None and now < last + CREATOR_COOLDOWN:
        nxt = last + CREATOR_COOLDOWN
        return ("%s: we commented on channel %s at %s; next allowed %s"
                % (REFUSE_CREATOR, creator, _pt_label(last), _pt_label(nxt)), nxt)
    return None


def item_refusal(ledger, rec):
    """One comment per item: per video, or per viewer comment for a reply."""
    yd = rec.get("youtube") or {}
    for c in ledger["comments"]:
        if not (c.get("live") or c.get("outcome") == ALREADY):
            continue
        if c.get("key") == rec.get("key") or (
                yd.get("kind") == KIND_VIDEO and c.get("kind") == KIND_VIDEO
                and yd.get("video_id") and c.get("video_id") == yd.get("video_id")):
            return ("%s: %s already has our comment (%s)"
                    % (REFUSE_ITEM, rec.get("key"), c.get("outcome")), None)
    if rec.get("status") == post_store.COMMENTED:
        return ("%s: %s is COMMENTED" % (REFUSE_ITEM, rec.get("key")), None)
    return None


def record_refusals(ledger, rec, now):
    """Every per-record pacing refusal, ``[(reason, next_allowed or None)]``."""
    yd = rec.get("youtube") or {}
    kind = yd.get("kind")
    out = []
    for r in (cap_refusal(ledger, kind, now),
              creator_refusal(ledger, yd.get("channel_id"), now) if kind == KIND_VIDEO
              else None,
              item_refusal(ledger, rec)):
        if r:
            out.append(r)
    return out


def draw_gap(rng):
    return timedelta(minutes=rng.uniform(*GAP_MINUTES))


# ── the no-promotion validator (DEC-061(3)) ─────────────────────────────────

LINK_RE = re.compile(
    r"(https?://|www\.|youtu\.be|\b[a-z0-9-]+\.(com|net|org|io|ai|co|dev|ly|gg|tv|"
    r"me|app|xyz|link|to)\b)", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w])@[\w.-]+")
HASHTAG_RE = re.compile(r"(?<![\w&])#[^\s#]+")
#: A call to subscribe in any tense. "subscription" (an API plan) is not one.
SUBSCRIBE_RE = re.compile(r"\bsubscrib(e|ed|es|ing|ers?)\b", re.IGNORECASE)


def promotion_refusals(text, cfg, register):
    """Every reason ``text`` promotes something, or ``[]``.

    Links, @-mentions, hashtags, "subscribe", our channel's name or handle,
    and the register's banned openers. No channel name and handle in config is
    itself a refusal: the check cannot be done.
    """
    text = text or ""
    low = text.lower()
    out = []
    if LINK_RE.search(text):
        out.append("link")
    if MENTION_RE.search(text):
        out.append("@-mention")
    if HASHTAG_RE.search(text):
        out.append("hashtag")
    if SUBSCRIBE_RE.search(text):
        out.append("subscribe")
    name = (cfg.channel_name or "").strip().lower()
    handle = (cfg.channel_handle or "").strip().lstrip("@").lower()
    if not name or not handle:
        out.append("config: engagement.channel_name and channel_handle must be set")
    else:
        if name in low:
            out.append("our channel name")
        if handle in low:
            out.append("our channel handle")
    opening = low.lstrip().lstrip("\"'").lstrip()
    for opener in register.banned_openers:
        if opening.startswith(opener.lower()):
            out.append("banned opener %r" % opener)
            break
    return out


def register_for(rec):
    variant = platform_policy.REPLY if (rec.get("youtube") or {}).get("kind") == KIND_REPLY else None
    return platform_policy.policy_for(PLATFORM, variant).register


def promotion_refusal(rec, cfg):
    """``"promotion: ..."`` for a draft that may not go out, else None."""
    reasons = promotion_refusals(rec.get("comment"), cfg, register_for(rec))
    return "%s: %s" % (REFUSE_PROMOTION, ", ".join(reasons)) if reasons else None


def approve_refusal(profile, rec, text):
    """What the Review panel's approve must refuse for a YouTube draft: the
    validator, on the exact text being approved. None when it may be approved."""
    try:
        cfg = engage_config(profile)
    except yt.YouTubeRefused as exc:
        return "config: %s" % exc
    probe = dict(rec or {})
    if text is not None and text.strip():
        probe["comment"] = text
    return promotion_refusal(probe, cfg)


# ── command runner ──────────────────────────────────────────────────────────

def _run(profile, name, dry_run, out, body):
    """Config, the production gate, then ``body(ctx)`` under the channel's
    publisher lock (shared with V1's uploads, which write the same quota
    ledger). Returns ``(exit_code, summary)``."""
    tag = "youtube-%s %s%s" % (name, profile, " [dry-run]" if dry_run else "")
    summary = {"profile": profile, "command": name, "dry_run": dry_run, "lines": []}

    def emit(line):
        summary["lines"].append(line)
        out(line)

    def refuse(code, msg):
        line = "%s: REFUSED: %s" % (tag, msg)
        logger.warning(line)
        emit(line)
        summary["refused"] = msg
        return code, summary

    if yt.CHANNEL.publisher != channels.YOUTUBE_API:
        return refuse(EXIT_REFUSED, "the youtube channel is not declared as "
                      "published through the YouTube API")
    try:
        cfg = engage_config(profile)
        pm.check_declared_production(
            cfg.channel_id, "comment as YouTube channel %s" % cfg.channel_id,
            allow_production=False)
    except yt.YouTubeRefused as exc:
        return refuse(EXIT_REFUSED, str(exc))
    except pm.ProductionAccessRefused as exc:
        return refuse(EXIT_PRODUCTION_REFUSED, str(exc))
    ctx = _Ctx(profile=profile, cfg=cfg, dry_run=dry_run, tag=tag, emit=emit,
               refuse=refuse, summary=summary)
    try:
        with publisher_lock.channel_lock(yt.state_path(profile), tag):
            return body(ctx)
    except publisher_lock.PublisherBusy as exc:
        return refuse(EXIT_REFUSED, str(exc))
    except yt.YouTubeRefused as exc:
        return refuse(EXIT_REFUSED, str(exc))


@dataclass
class _Ctx:
    profile: str
    cfg: EngageConfig
    dry_run: bool
    tag: str
    emit: object
    refuse: object
    summary: dict


class _Google:
    """One run's Google session: the service, built once, and every call
    charged to the quota ledger and the paid-call ledger BEFORE it is made."""

    def __init__(self, ctx, api, now):
        self.ctx = ctx
        self.api = api
        self.now = now
        self.quota = yt.load_quota(ctx.profile)
        self._service = None

    def units_left(self):
        return self.ctx.cfg.daily_limit - yt.quota_used(self.quota, yt.quota_day(self.now))

    def spend(self, units, op, item=None):
        reason = yt.quota_refusal(self.quota, yt.quota_day(self.now), units,
                                  self.ctx.cfg.daily_limit)
        if reason:
            raise EngageRefused(reason + "; %s was not called" % op)
        yt.charge(self.ctx.profile, self.quota, self.now, units, op, item)
        yt.log_paid_call("%s%s, %d quota units" % (op, " " + item if item else "", units),
                         now=self.now)

    def service(self):
        if self._service is None:
            problem = yt.token_problem(self.ctx.profile)
            if problem:
                raise EngageRefused(problem)
            creds = self.api.credentials_from_token(
                yt.read_json(yt.token_path(self.ctx.profile), {}))
            yt.log_paid_call("oauth2 token refresh (%s)" % self.ctx.profile, now=self.now)
            self.api.refresh(creds)
            self._service = self.api.build_service(creds)
        return self._service

    def check_identity(self):
        """The token must answer for the configured channel before we write."""
        service = self.service()
        self.spend(yt.CHANNEL_CHECK_UNITS, "channels.list mine")
        found = self.api.my_channel_id(service)
        if found != self.ctx.cfg.channel_id:
            raise EngageRefused("the token answers for channel %s, not the "
                                "configured %s; nothing was posted"
                                % (found, self.ctx.cfg.channel_id))

    def call(self, units, op, item, fn, *args, **kwargs):
        service = self.service()
        self.spend(units, op, item)
        return fn(service, *args, **kwargs)


# ── youtube-discover ────────────────────────────────────────────────────────

def discover(profile, query, dry_run=False, now=None, api=None, out=print):
    """Suggest channels for ``query``. **Adds nothing**: the watch list is
    only changed by :func:`add_to_watch_list`. ``search.list`` (100 units) and
    one ``channels.list`` (1 unit) for titles and subscriber counts."""
    now = now or datetime.now(timezone.utc)
    api = api or youtube_api
    query = (query or "").strip()

    def body(ctx):
        if not query:
            return ctx.refuse(EXIT_REFUSED, "--query is required")
        listed = {e["channel_id"] for e in ctx.cfg.watch_list}
        g = _Google(ctx, api, now)
        need = SEARCH_UNITS + LIST_UNITS
        if dry_run:
            ctx.emit("%s: would call search.list (q=%r, %d units) and channels.list "
                     "(%d unit); %d units left today; adds nothing"
                     % (ctx.tag, query, SEARCH_UNITS, LIST_UNITS, g.units_left()))
            ctx.summary["suggestions"] = []
            return EXIT_OK, ctx.summary
        if g.units_left() < need:
            return ctx.refuse(EXIT_REFUSED, "quota: %d units left today, discover "
                              "needs %d" % (g.units_left(), need))
        try:
            found = g.call(SEARCH_UNITS, "search.list", "q=%s" % query,
                           api.search_channels, query)
            ids = [f["channel_id"] for f in found]
            info = g.call(LIST_UNITS, "channels.list", None, api.channels_info,
                          ids) if ids else {}
        except youtube_api.YouTubeApiError as exc:
            ctx.emit("%s: STOP: %s" % (ctx.tag, exc))
            ctx.summary["error"] = str(exc)
            return EXIT_ERROR, ctx.summary
        suggestions = []
        for f in found:
            i = info.get(f["channel_id"]) or {}
            if f["channel_id"] == ctx.cfg.channel_id:
                continue
            suggestions.append({"channel_id": f["channel_id"],
                                "title": i.get("title") or f["title"],
                                "subscribers": i.get("subscribers"),
                                "description": (f.get("description") or "")[:200],
                                "on_watch_list": f["channel_id"] in listed})
        for s in suggestions:
            ctx.emit("  %s  %-40s %s subscribers%s" % (
                s["channel_id"], s["title"][:40],
                "hidden" if s["subscribers"] is None else s["subscribers"],
                "  (on the watch list)" if s["on_watch_list"] else ""))
        ctx.emit("%s: %d suggestion(s); nothing was added. Add one with "
                 "youtube-watch --profile %s --add CHANNEL_ID, or in the dashboard"
                 % (ctx.tag, len(suggestions), profile))
        ctx.summary["suggestions"] = suggestions
        return EXIT_OK, ctx.summary

    return _run(profile, "discover", dry_run, out, body)


# ── find ────────────────────────────────────────────────────────────────────

def _video_record(v, now):
    vid = v["video_id"]
    return {
        "key": vid, "url": "https://www.youtube.com/watch?v=%s" % vid,
        "author": v.get("channel_title") or "", "text": _video_text(v),
        "category": KIND_VIDEO, "relevance_score": 0,
        "status": post_store.NEW, "trash_reason": None, "comment": None,
        "comment_meta": None, "scraped_at": yt.utc_z(now), "generated_at": None,
        "commented_at": None, "reviewed_at": None, "approved_hash": None,
        "updated_at": yt.utc_z(now),
        "youtube": {"kind": KIND_VIDEO, "video_id": vid,
                    "channel_id": v.get("channel_id"),
                    "channel_title": v.get("channel_title") or "",
                    "title": v.get("title") or "",
                    "description": v.get("description") or "",
                    "published_at": v.get("published_at")},
    }


def _video_text(v):
    title = v.get("title") or ""
    desc = v.get("description") or ""
    return title + ("\n\n" + desc if desc else "")


def _reply_record(t, now):
    cid = t["comment_id"]
    return {
        "key": cid,
        "url": "https://www.youtube.com/watch?v=%s&lc=%s" % (t.get("video_id"), cid),
        "author": t.get("author") or "", "text": t.get("text") or "",
        "category": KIND_REPLY, "relevance_score": 0,
        "status": post_store.NEW, "trash_reason": None, "comment": None,
        "comment_meta": None, "scraped_at": yt.utc_z(now), "generated_at": None,
        "commented_at": None, "reviewed_at": None, "approved_hash": None,
        "updated_at": yt.utc_z(now),
        "youtube": {"kind": KIND_REPLY, "video_id": t.get("video_id"),
                    "parent_id": cid, "commenter": t.get("author") or "",
                    "commenter_channel_id": t.get("author_channel_id"),
                    "published_at": t.get("published_at")},
    }


def _recent(iso, now):
    try:
        when = _parse(iso)
    except ValueError:
        return False
    return when is not None and now - when <= LOOKBACK


def find(profile, dry_run=False, now=None, api=None, out=print):
    """Land new candidates as NEW: recent videos from watch-list channels we
    have not commented on, and viewers' threads on our videos with no reply
    from us. A key already in the store, in any state, is never re-added."""
    now = now or datetime.now(timezone.utc)
    api = api or youtube_api

    def body(ctx):
        cfg = ctx.cfg
        store = load_store(profile)
        ledger = load_ledger(profile)
        known = set(store.posts) | {c.get("key") for c in ledger["comments"]}
        known_videos = {c.get("video_id") for c in ledger["comments"]
                        if c.get("kind") == KIND_VIDEO}
        cached = ledger.setdefault("uploads_playlists", {})
        watch = [e for e in cfg.watch_list if e["channel_id"] != cfg.channel_id]
        uncached = [e["channel_id"] for e in watch if not cached.get(e["channel_id"])]
        if dry_run:
            ctx.emit("%s: would read %d watch-list channel(s) (%s channels.list for "
                     "uploads playlists, then up to %d playlistItems page(s) each, "
                     "1 unit a page) and 1 commentThreads.list for replies on %s; "
                     "store has %d record(s)"
                     % (ctx.tag, len(watch), "1" if uncached else "no",
                        PLAYLIST_MAX_PAGES, cfg.channel_id, len(store.posts)))
            return EXIT_OK, ctx.summary
        g = _Google(ctx, api, now)
        added_videos, added_replies, skipped = [], [], {}

        def skip(why):
            skipped[why] = skipped.get(why, 0) + 1
        try:
            if uncached:
                info = g.call(LIST_UNITS, "channels.list contentDetails", None,
                              api.channels_info, uncached)
                for cid in uncached:
                    pl = (info.get(cid) or {}).get("uploads_playlist")
                    if pl:
                        cached[cid] = pl
                save_ledger(profile, ledger)
            for e in watch:
                pl = cached.get(e["channel_id"])
                if not pl:
                    skip("no uploads playlist")
                    continue
                token = None
                for _page in range(PLAYLIST_MAX_PAGES):
                    items, token = g.call(LIST_UNITS, "playlistItems.list",
                                          e["channel_id"], api.playlist_page, pl, token)
                    old = False
                    for v in items:
                        if not _recent(v.get("published_at"), now):
                            old = True
                            continue
                        if v["video_id"] in known or v["video_id"] in known_videos:
                            skip("already known")
                            continue
                        # The creator is the watch-list channel whose
                        # uploads these are (one comment per creator per 3 days).
                        v["channel_id"] = e["channel_id"]
                        rec = _video_record(v, now)
                        store.posts[rec["key"]] = rec
                        known.add(rec["key"])
                        added_videos.append(rec["key"])
                    if old or not token:
                        break
            threads, _next = g.call(LIST_UNITS, "commentThreads.list "
                                    "allThreadsRelatedToChannelId", cfg.channel_id,
                                    api.comment_threads_page,
                                    related_channel_id=cfg.channel_id)
            for t in threads:
                if t.get("author_channel_id") == cfg.channel_id:
                    skip("our own comment")
                    continue
                if any(r.get("author_channel_id") == cfg.channel_id
                       for r in t.get("replies") or []):
                    skip("already replied")
                    continue
                if not _recent(t.get("published_at"), now):
                    skip("older than 7 days")
                    continue
                if t["comment_id"] in known:
                    skip("already known")
                    continue
                rec = _reply_record(t, now)
                store.posts[rec["key"]] = rec
                known.add(rec["key"])
                added_replies.append(rec["key"])
        except youtube_api.YouTubeApiError as exc:
            store.save()
            ctx.emit("%s: STOP: %s (%d video(s), %d repl(ies) found before it)"
                     % (ctx.tag, exc, len(added_videos), len(added_replies)))
            ctx.summary.update(videos=added_videos, replies=added_replies,
                               error=str(exc))
            return EXIT_ERROR, ctx.summary
        store.save()
        ctx.summary.update(videos=added_videos, replies=added_replies, skipped=skipped)
        ctx.emit("%s: %d new video(s), %d new repl(ies) to draft, as NEW; skipped %s"
                 % (ctx.tag, len(added_videos), len(added_replies),
                    ", ".join("%d %s" % (n, w) for w, n in sorted(skipped.items()))
                    or "none"))
        return EXIT_OK, ctx.summary

    return _run(profile, "find", dry_run, out, body)


# ── draft ───────────────────────────────────────────────────────────────────

CHAPTER_RE = re.compile(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s+\S.*$")


def parse_chapters(description):
    """The description's chapter lines (``0:00 Intro``), as written."""
    return [line.strip() for line in (description or "").splitlines()
            if CHAPTER_RE.match(line)]


def video_input(rec, top_comments):
    """The generator's input for a video: title, description, chapters, up to
    20 top comments. Labels only; no instruction is added here."""
    yd = rec.get("youtube") or {}
    parts = ["Title: %s" % (yd.get("title") or "")]
    if yd.get("description"):
        parts.append("Description:\n%s" % yd["description"])
    chapters = parse_chapters(yd.get("description"))
    if chapters:
        parts.append("Chapters:\n" + "\n".join(chapters))
    if top_comments:
        parts.append("Top comments:\n" + "\n".join(
            "- " + c[:TOP_COMMENT_CHARS].replace("\n", " ")
            for c in top_comments[:TOP_COMMENTS]))
    return "\n\n".join(parts)


def reply_input(rec, video_title):
    yd = rec.get("youtube") or {}
    return "Video: %s\n\nComment by %s:\n%s" % (
        video_title or "", yd.get("commenter") or "a viewer", rec.get("text") or "")


def _local_titles(profile):
    """Our videos' titles from V1's state file. Read only; none is fine."""
    try:
        state = yt.read_json(yt.state_path(profile), {"items": {}})
    except yt.YouTubeRefused:
        return {}
    return {r.get("video_id"): r.get("title") or ""
            for r in (state.get("items") or {}).values() if r.get("video_id")}


def default_generator(profile, variant):
    from .comment_generator import AuthenticCommentGenerator
    return AuthenticCommentGenerator("youtube-engage", profile_name=profile,
                                     platform=PLATFORM, variant=variant)


def draft(profile, limit=5, dry_run=False, now=None, api=None, make_generator=None,
          out=print):
    """Draft NEW records into GENERATED, at most ``limit``. A draft that fails
    the no-promotion validator is still stored, with the reason, and cannot be
    approved until it is edited."""
    now = now or datetime.now(timezone.utc)
    api = api or youtube_api
    make_generator = make_generator or default_generator

    def body(ctx):
        store = load_store(profile)
        todo = sorted(store.by_status(post_store.NEW),
                      key=lambda r: r.get("scraped_at") or "")[:max(0, int(limit))]
        if dry_run:
            for r in todo:
                ctx.emit("  WOULD DRAFT: %s  %s" % (r["key"], (r.get("youtube") or {}).get("kind")))
            ctx.emit("%s: would draft %d of %d NEW" % (
                ctx.tag, len(todo), len(store.by_status(post_store.NEW))))
            return EXIT_OK, ctx.summary
        g = _Google(ctx, api, now)
        titles = _local_titles(profile)
        gens = {}
        drafted, failed, flagged = [], [], []
        for rec in todo:
            yd = rec.get("youtube") or {}
            kind = yd.get("kind")
            try:
                if kind == KIND_VIDEO:
                    try:
                        threads, _n = g.call(LIST_UNITS, "commentThreads.list relevance",
                                             rec["key"], api.comment_threads_page,
                                             video_id=yd.get("video_id"),
                                             order="relevance", max_results=TOP_COMMENTS)
                        tops = [t.get("text") or "" for t in threads]
                    except youtube_api.YouTubeApiError as exc:
                        if _classify_error(exc) in ("gone", "comments_disabled"):
                            _close_gone(store, rec, exc)
                            failed.append((rec["key"], str(exc)))
                            continue
                        raise
                    post = {"author_name": yd.get("channel_title") or rec.get("author"),
                            "text": video_input(rec, tops)}
                    variant = None
                else:
                    title = titles.get(yd.get("video_id"))
                    if title is None and yd.get("video_id"):
                        title = g.call(LIST_UNITS, "videos.list", yd["video_id"],
                                       api.video_titles, [yd["video_id"]]).get(yd["video_id"])
                    post = {"author_name": yd.get("commenter") or rec.get("author"),
                            "text": reply_input(rec, title)}
                    variant = platform_policy.REPLY
            except youtube_api.YouTubeApiError as exc:
                store.save()
                ctx.emit("%s: STOP at %s: %s" % (ctx.tag, rec["key"], exc))
                ctx.summary.update(drafted=drafted, failed=failed, error=str(exc))
                return EXIT_ERROR, ctx.summary
            if variant not in gens:
                gens[variant] = make_generator(profile, variant)
            result = gens[variant].generate_comment(post, {})
            text = (result or {}).get("comment")
            if not text:
                failed.append((rec["key"], "the generator returned no draft"))
                ctx.emit("  NO DRAFT: %s (left NEW)" % rec["key"])
                continue
            reasons = promotion_refusals(text, ctx.cfg, register_for(rec))
            meta = {"style": result.get("style"), "approach": result.get("approach"),
                    "word_count": result.get("word_count"),
                    "chars": len(text), "issues": result.get("issues") or [],
                    "promotion": reasons, "kind": kind}
            store.mark_generated(rec["key"], text, meta)
            drafted.append(rec["key"])
            if reasons:
                flagged.append(rec["key"])
            ctx.emit("  DRAFTED: %s  (%s)%s" % (
                rec["key"], kind, "  promotion: %s; edit before approving"
                % ", ".join(reasons) if reasons else ""))
        store.save()
        ctx.summary.update(drafted=drafted, failed=failed, flagged=flagged)
        ctx.emit("%s: drafted %d (GENERATED, waiting for approval in Review, YouTube "
                 "tab), %d flagged by the no-promotion check, %d not drafted"
                 % (ctx.tag, len(drafted), len(flagged), len(failed)))
        return EXIT_OK, ctx.summary

    return _run(profile, "draft", dry_run, out, body)


# ── classify / already_commented / post_and_verify ──────────────────────────

def _classify_error(exc):
    """``gone`` (404: the video or comment is not there), ``comments_disabled``,
    or ``unclear``. Only the first two are terminal (PRINCIPLES §5)."""
    msg = str(exc)
    if getattr(exc, "status", None) == 404 or "videoNotFound" in msg \
            or "commentNotFound" in msg or "parentCommentNotFound" in msg:
        return "gone"
    if "commentsDisabled" in msg:
        return "comments_disabled"
    return "unclear"


def _close_gone(store, rec, exc):
    if _classify_error(exc) == "gone":
        store.mark_unavailable(rec["key"], reason="youtube: %s" % exc)
    else:
        rec.update(status=post_store.TRASH, trash_reason=REASON_COMMENTS_DISABLED,
                   updated_at=yt.utc_z(datetime.now(timezone.utc)))
        (rec.setdefault("youtube", {}))["error"] = str(exc)


def already_commented(g, rec):
    """Our comment on this item, by YouTube's own listing, or None.

    A video: its threads matched on our channel id; a reply: the thread's
    replies. At most ``DEDUPE_MAX_PAGES`` pages, 1 unit each."""
    yd = rec.get("youtube") or {}
    ours = g.ctx.cfg.channel_id
    token = None
    for _page in range(DEDUPE_MAX_PAGES):
        if yd.get("kind") == KIND_REPLY:
            items, token = g.call(LIST_UNITS, "comments.list dedupe", rec["key"],
                                  g.api.replies_page, yd.get("parent_id"), token)
            for c in items:
                if c.get("author_channel_id") == ours:
                    return c.get("id")
        else:
            items, token = g.call(LIST_UNITS, "commentThreads.list dedupe", rec["key"],
                                  g.api.comment_threads_page,
                                  video_id=yd.get("video_id"), page_token=token)
            for t in items:
                if t.get("author_channel_id") == ours:
                    return t.get("comment_id")
        if not token:
            break
    return None


def verify(g, rec, comment_id):
    """``posted_visible`` when the new comment is in the public listing,
    ``posted_held`` when it was created but is not listed (or the check could
    not be made). One page, 1 unit."""
    yd = rec.get("youtube") or {}
    try:
        if yd.get("kind") == KIND_REPLY:
            items, _t = g.call(LIST_UNITS, "comments.list verify", rec["key"],
                               g.api.replies_page, yd.get("parent_id"))
            ids = {c.get("id") for c in items}
        else:
            items, _t = g.call(LIST_UNITS, "commentThreads.list verify", rec["key"],
                               g.api.comment_threads_page,
                               video_id=yd.get("video_id"), order="time",
                               max_results=50)
            ids = {t.get("comment_id") for t in items} | {t.get("thread_id") for t in items}
    except (youtube_api.YouTubeApiError, EngageRefused) as exc:
        return POSTED_HELD, "verify not made: %s" % exc
    if comment_id in ids:
        return POSTED_VISIBLE, None
    return POSTED_HELD, "created as %s, not in the public listing" % comment_id


def post_and_verify(g, rec):
    """One insert, never retried. ``(outcome, comment_id, error, ambiguous,
    quota)``: ``posted_visible`` / ``posted_held`` with the new id, or
    ``failed``."""
    yd = rec.get("youtube") or {}
    text = rec.get("comment")
    try:
        if yd.get("kind") == KIND_REPLY:
            cid = g.call(g.ctx.cfg.comment_units, "comments.insert", rec["key"],
                         g.api.insert_reply, yd.get("parent_id"), text)
        else:
            cid = g.call(g.ctx.cfg.comment_units, "commentThreads.insert", rec["key"],
                         g.api.insert_thread, g.ctx.cfg.channel_id, yd.get("video_id"), text)
    except youtube_api.YouTubeApiError as exc:
        return FAILED, None, str(exc), bool(exc.ambiguous), bool(exc.quota)
    outcome, note = verify(g, rec, cid)
    return outcome, cid, note, False, False


# ── youtube-engage ──────────────────────────────────────────────────────────

def pacing_status(ledger, now):
    """A plain line: what today allows."""
    return ("ramp day %d: %d of %d on others' videos, %d of %d replies today; "
            "next allowed %s" % (
                ramp_day(ledger, now), used_today(ledger, KIND_VIDEO, now),
                daily_cap(ledger, KIND_VIDEO, now), used_today(ledger, KIND_REPLY, now),
                REPLY_DAILY_CAP, _pt_label(next_allowed(ledger, now))))


def next_allowed(ledger, now):
    """The earliest time the window and the gap both allow a comment."""
    candidates = [now]
    gap = _parse(ledger.get("next_allowed_at"))
    if gap and gap > now:
        candidates.append(gap)
    when = max(candidates)
    if window_refusal(when):
        when = next_window_open(when)
    return when


def _resolve_attempt(profile, store, ledger, now, ctx):
    """A run that died between the ledger write and the answer left an
    ``attempting`` mark. Nobody knows whether that comment exists, so it is
    ambiguous: closed, counted against pacing, never retried."""
    att = ledger.get("attempting")
    if not att:
        return
    rec = store.get(att.get("key"))
    if rec is not None and rec.get("status") == post_store.GENERATED:
        rec.update(status=post_store.TRASH, trash_reason=REASON_AMBIGUOUS,
                   updated_at=yt.utc_z(now))
        rec.setdefault("youtube", {}).update(
            outcome=FAILED, error="a run ended during the insert; not retried")
        store.save()
    ledger["comments"].append(dict(att, outcome=FAILED, live=True, ambiguous=True,
                                   error="a run ended during the insert"))
    ledger["attempting"] = None
    save_ledger(profile, ledger)
    ctx.emit("  RESOLVED: %s was mid-insert in an earlier run; marked ambiguous, not "
             "retried. Check the video on YouTube." % att.get("key"))


def engage(profile, dry_run=False, now=None, api=None, rng=None, out=print):
    """Post at most ONE approved comment, if pacing allows; otherwise say
    when the next one is allowed. No retry, no second comment in one call."""
    now = now or datetime.now(timezone.utc)
    api = api or youtube_api
    rng = rng or random.Random()

    def body(ctx):
        cfg = ctx.cfg
        store = load_store(profile)
        ledger = load_ledger(profile)
        if ledger.get("attempting"):
            if dry_run:
                ctx.emit("  WOULD RESOLVE: %s was mid-insert in an earlier run; it "
                         "would be marked ambiguous, never retried"
                         % ledger["attempting"].get("key"))
            else:
                _resolve_attempt(profile, store, ledger, now, ctx)
        ctx.summary.update(posted=None, outcome=None, refusals=[], not_approved=0,
                           next_allowed=None)
        drafts = store.by_status(post_store.GENERATED)
        approved, refused = post_store.split_approved(drafts)
        ctx.summary["not_approved"] = len(refused)
        for rec, reason in refused:
            logger.info("%s: refused %s (%s)", ctx.tag, rec.get("key"), reason)
        if not approved:
            ctx.emit("%s: %s%s" % (ctx.tag, post_store.not_approved_message(len(refused)),
                                   "" if refused else " (no drafts)"))
            return EXIT_OK, ctx.summary
        blocked = window_refusal(now) or gap_refusal(ledger, now)
        if blocked:
            reason, nxt = blocked
            ctx.summary["refusals"].append(reason)
            ctx.summary["next_allowed"] = yt.utc_z(nxt)
            ctx.emit("%s: 0 posted: %s. Next allowed %s. (%s)"
                     % (ctx.tag, reason, _pt_label(nxt), pacing_status(ledger, now)))
            return EXIT_OK, ctx.summary
        chosen = None
        nexts = []
        for rec in sorted(approved, key=lambda r: r.get("reviewed_at") or ""):
            why = record_refusals(ledger, rec, now)
            if why:
                for reason, nxt in why:
                    ctx.summary["refusals"].append("%s: %s" % (rec["key"], reason))
                    ctx.emit("  REFUSED: %s  %s" % (rec["key"], reason))
                    if nxt:
                        nexts.append(nxt)
                continue
            promo = promotion_refusal(rec, cfg)
            if promo:
                ctx.summary["refusals"].append("%s: %s" % (rec["key"], promo))
                if not dry_run:
                    # Back to review: the approval is withdrawn, the text kept.
                    rec["reviewed_at"] = None
                    rec["approved_hash"] = None
                    meta = rec.get("comment_meta") or {}
                    meta["promotion"] = promo
                    rec["comment_meta"] = meta
                    rec["updated_at"] = yt.utc_z(now)
                    store.save()
                ctx.emit("  REFUSED: %s  %s; %s to review" % (
                    rec["key"], promo, "would go back" if dry_run else "back"))
                continue
            chosen = rec
            break
        if chosen is None:
            nxt = min(nexts) if nexts else None
            ctx.summary["next_allowed"] = yt.utc_z(nxt) if nxt else None
            ctx.emit("%s: 0 posted: no approved comment is allowed now%s. (%s)"
                     % (ctx.tag, "; next allowed %s" % _pt_label(nxt) if nxt else "",
                        pacing_status(ledger, now)))
            return EXIT_OK, ctx.summary
        key = chosen["key"]
        kind = (chosen.get("youtube") or {}).get("kind")
        if dry_run:
            ctx.emit("  WOULD POST: %s  (%s) %r" % (key, kind, chosen.get("comment")))
            ctx.emit("%s: would post 1 (dedupe up to %d pages, insert %d units, verify "
                     "1); %s" % (ctx.tag, DEDUPE_MAX_PAGES, cfg.comment_units,
                                 pacing_status(ledger, now)))
            ctx.summary["would_post"] = key
            return EXIT_OK, ctx.summary
        return _post_one(profile, ctx, store, ledger, key, now, api, rng)

    return _run(profile, "engage", dry_run, out, body)


def _post_one(profile, ctx, store, ledger, key, now, api, rng):
    cfg = ctx.cfg
    g = _Google(ctx, api, now)
    need = yt.CHANNEL_CHECK_UNITS + DEDUPE_MAX_PAGES * LIST_UNITS + cfg.comment_units + LIST_UNITS
    if g.units_left() < need:
        return ctx.refuse(EXIT_REFUSED, "quota: %d units left today, one comment can "
                          "need %d; nothing was posted" % (g.units_left(), need))
    try:
        g.check_identity()
        rec = store.get(key)
        try:
            found = already_commented(g, rec)
        except youtube_api.YouTubeApiError as exc:
            kind = _classify_error(exc)
            if kind in ("gone", "comments_disabled"):
                _close_gone(store, rec, exc)
                store.save()
                ctx.emit("%s: 0 posted: %s is %s (%s)" % (ctx.tag, key, kind, exc))
                ctx.summary.update(outcome=FAILED, error=str(exc))
                return EXIT_OK, ctx.summary
            raise
    except youtube_api.YouTubeApiError as exc:
        ctx.emit("%s: STOP before any insert: %s" % (ctx.tag, exc))
        ctx.summary["error"] = str(exc)
        return EXIT_ERROR, ctx.summary
    if found:
        rec.update(status=post_store.COMMENTED, commented_at=yt.utc_z(now),
                   updated_at=yt.utc_z(now))
        rec.setdefault("youtube", {}).update(outcome=ALREADY, comment_id=found)
        store.save()
        ledger["comments"].append(_entry(rec, now, ALREADY, found, live=False))
        save_ledger(profile, ledger)
        ctx.emit("%s: 0 posted: %s already has our comment %s on YouTube; marked "
                 "COMMENTED, nothing inserted" % (ctx.tag, key, found))
        ctx.summary.update(outcome=ALREADY, comment_id=found)
        return EXIT_OK, ctx.summary

    # The last step before the insert: a fresh read of the store, and the one
    # shared approval check on it. An approval withdrawn or an edit made since
    # this run started is honoured.
    fresh = load_store(profile).get(key)
    reason = post_store.approval_refusal(fresh)
    if reason is None and (fresh.get("comment") != rec.get("comment")):
        reason = post_store.REFUSE_TEXT_DIFFERS
    if reason is not None:
        ctx.emit("%s: %s; %s refused (%s)" % (
            ctx.tag, post_store.not_approved_message(1), key, reason))
        ctx.summary.update(not_approved=ctx.summary.get("not_approved", 0) + 1,
                           refusals=ctx.summary.get("refusals", []) + ["%s: %s" % (key, reason)])
        return EXIT_OK, ctx.summary
    store = load_store(profile)
    rec = store.get(key)

    ledger["attempting"] = _entry(rec, now, None, None, live=True)
    save_ledger(profile, ledger)
    try:
        outcome, cid, note, ambiguous, quota = post_and_verify(g, rec)
    except EngageRefused:
        ledger["attempting"] = None
        save_ledger(profile, ledger)
        raise
    ledger["attempting"] = None
    yd = rec.setdefault("youtube", {})
    if outcome == FAILED and quota:
        # Google refused before creating anything: still approved, still GENERATED.
        yd["error"] = note
        store.save()
        save_ledger(profile, ledger)
        ctx.emit("%s: 0 posted: %s failed on quota, nothing was created: %s"
                 % (ctx.tag, key, note))
        ctx.summary.update(outcome=FAILED, error=note)
        return EXIT_ERROR, ctx.summary
    live = outcome != FAILED or ambiguous
    entry = _entry(rec, now, outcome, cid, live=live)
    if ambiguous:
        entry["ambiguous"] = True
    if note:
        entry["error" if outcome == FAILED else "note"] = note
    ledger["comments"].append(entry)
    if live:
        if kind_of(rec) == KIND_VIDEO and not ledger.get("ramp_started_at"):
            ledger["ramp_started_at"] = yt.utc_z(now)
        ledger["next_allowed_at"] = yt.utc_z(now + draw_gap(rng))
    save_ledger(profile, ledger)
    yd.update(outcome=outcome, comment_id=cid, attempt_at=yt.utc_z(now))
    if outcome == FAILED:
        yd["error"] = note
        rec.update(status=post_store.TRASH,
                   trash_reason=REASON_AMBIGUOUS if ambiguous else REASON_POST_FAILED,
                   updated_at=yt.utc_z(now))
    else:
        if note:
            yd["note"] = note
        rec.update(status=post_store.COMMENTED, commented_at=yt.utc_z(now),
                   updated_at=yt.utc_z(now))
    store.save()
    ctx.summary.update(outcome=outcome, comment_id=cid, key=key,
                       posted=key if outcome == POSTED_VISIBLE else None,
                       next_allowed=ledger.get("next_allowed_at") if live else None)
    if outcome == POSTED_VISIBLE:
        ctx.emit("%s: posted 1: %s (%s) comment %s, visible. Next allowed %s"
                 % (ctx.tag, key, kind_of(rec), cid,
                    _pt_label(next_allowed(ledger, now))))
        return EXIT_OK, ctx.summary
    if outcome == POSTED_HELD:
        ctx.emit("%s: 0 posted visibly: %s comment %s is posted_held (%s). Not counted "
                 "as posted and never re-posted. Next allowed %s"
                 % (ctx.tag, key, cid, note, _pt_label(next_allowed(ledger, now))))
        return EXIT_OK, ctx.summary
    ctx.emit("%s: 0 posted: %s failed%s: %s. Not retried."
             % (ctx.tag, key, " (ambiguous: it may exist; check the video)"
                if ambiguous else "", note))
    return EXIT_ERROR, ctx.summary


def kind_of(rec):
    return (rec.get("youtube") or {}).get("kind")


def _entry(rec, now, outcome, comment_id, live):
    yd = rec.get("youtube") or {}
    return {"at": yt.utc_z(now), "key": rec.get("key"), "kind": yd.get("kind"),
            "video_id": yd.get("video_id"),
            "creator": yd.get("channel_id") if yd.get("kind") == KIND_VIDEO else None,
            "outcome": outcome, "comment_id": comment_id, "live": live}


# ── read-only status for the dashboard ──────────────────────────────────────

def status_view(profile, now=None):
    """Watch list, pacing and outcome counts, from local files only."""
    now = now or datetime.now(timezone.utc)
    view = {"configured": False, "watch_list": [], "error": None}
    try:
        cfg = engage_config(profile)
    except yt.YouTubeRefused as exc:
        view["error"] = str(exc)
        return view
    view.update(configured=True, channel_id=cfg.channel_id,
                channel_name=cfg.channel_name, channel_handle=cfg.channel_handle,
                watch_list=list(cfg.watch_list),
                token=yt.token_problem(profile) is None)
    try:
        ledger = load_ledger(profile)
    except yt.YouTubeRefused as exc:
        view["error"] = str(exc)
        return view
    counts = {}
    for c in ledger["comments"]:
        key = c.get("outcome") or "unknown"
        if c.get("ambiguous"):
            key = "failed_ambiguous"
        counts[key] = counts.get(key, 0) + 1
    view.update(pacing=pacing_status(ledger, now),
                next_allowed=yt.utc_z(next_allowed(ledger, now)),
                outcomes=counts, ramp_day=ramp_day(ledger, now),
                cap_today=daily_cap(ledger, KIND_VIDEO, now),
                used_today=used_today(ledger, KIND_VIDEO, now),
                replies_today=used_today(ledger, KIND_REPLY, now),
                reply_cap=REPLY_DAILY_CAP)
    return view
