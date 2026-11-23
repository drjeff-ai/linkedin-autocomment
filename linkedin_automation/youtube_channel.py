"""YouTube Shorts as a channel of the platform (V1, DEC-058, DEC-059).

Ported from video-publisher (``tools/intake_videos.py``, ``tools/schedule_youtube.py``,
``src/manifest.py``, ``src/db.py``), not imported from it. The Google calls are
all in :mod:`youtube_api`.

**One publisher per channel (DEC-058(2)).** video-publisher's
``config/moved_to_platform.yaml`` lists the brands that have moved here. The
upload path here refuses unless the profile's ``vp_brand`` is in it, and
video-publisher refuses every brand that is. Jeff adds the brand himself, with
``youtube-cutover``. Every command here also holds this channel's
``publisher_lock``, so two runs of this app never upload at once.

**Files**, all under ``data/<profile>/youtube/`` and all written temp-then-rename:

* ``youtube_state.json``: one record per video, keyed by its source folder name
  (``C-012-ask-first-accept-later``; video-publisher's ``-yt`` suffix dropped).
* ``quota_ledger.json``: units spent per Pacific date (YouTube's quota day).
* ``oauth_token.json``: the refresh token ``tools/youtube_auth.py`` stores.
  Gitignored with the rest of ``data/``; never printed.

**States.** ``queued`` (known, not uploaded), ``attempting`` (written before the
upload call; if a run dies here nobody knows whether the video exists, so it
blocks every later upload run until a person checks Studio and edits the
record), ``uploaded`` (YouTube has it; *scheduled* while its ``publish_at`` is
ahead, *live* after), ``failed``.

Every upload is ``private`` with ``status.publishAt``: YouTube makes it public
at that time. An item with no date is dated first; an existing date never
changes.
"""

import json
import logging
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import channels
from . import platforms
from . import profile_manager as pm
from . import publisher_lock
from . import youtube_api

logger = logging.getLogger(__name__)

CHANNEL = channels.get(platforms.YOUTUBE)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_PRODUCTION_REFUSED = pm.EXIT_PRODUCTION_REFUSED

QUEUED = "queued"
ATTEMPTING = "attempting"
UPLOADED = "uploaded"
FAILED = "failed"
STATES = (QUEUED, ATTEMPTING, UPLOADED, FAILED)

#: YouTube's quota day runs on Pacific time, whatever the profile's zone.
PACIFIC = ZoneInfo("America/Los_Angeles")
#: YouTube refuses a publishAt that is not comfortably in the future.
PUBLISH_AT_MIN_LEAD = timedelta(minutes=15)

DEFAULT_DAILY_LIMIT = 10000
DEFAULT_UPLOAD_UNITS = 1600
DEFAULT_COMMENT_UNITS = 50
#: ``channels.list mine`` (the identity check before uploads).
CHANNEL_CHECK_UNITS = 1
DEFAULT_CATEGORY_ID = "27"  # what every aws item used (video-publisher intake)

TITLE_MAX = 100             # YouTube's limit; over it the item is skipped, never cut
DESCRIPTION_MAX = 4900      # video-publisher's margin under YouTube's 5000
TAGS_MAX_TOTAL = 500        # YouTube's limit on the combined tag string

ITEM_DIR_RE = re.compile(r"^C-(\d+)-.+$")
SKIP_DIRS = {"_posted", "ready-to-publish"}
SHORTS_HEADING_RE = re.compile(r"^#{1,6}\s+YouTube Shorts\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^#{1,6}\s")
FIELD_RE = re.compile(r"^-\s+\*\*([^*]+?):\*\*\s*(.*)$")

#: video-publisher's item folders carry this suffix; ids here do not.
VP_SUFFIX = "-yt"
MARKER_RELPATH = os.path.join("config", "moved_to_platform.yaml")
VP_DB_RELPATH = os.path.join("data", "publish_log.db")

STATE_NAME = "youtube_state.json"
QUOTA_NAME = "quota_ledger.json"
TOKEN_NAME = "oauth_token.json"
#: The paid-call ledger, by relative path, as every other caller writes it.
USAGE_LOG = "api_usage.jsonl"

CONFIG_KEYS = {"vp_brand", "vp_root", "channel_id", "content_root",
               "publish_time", "tz", "per_day", "quota", "category_id"}
REQUIRED_KEYS = ("vp_brand", "vp_root", "channel_id", "content_root",
                 "publish_time", "tz")
QUOTA_KEYS = {"daily_limit", "upload_units", "comment_units"}


class YouTubeRefused(RuntimeError):
    """A check failed before anything was sent. Nothing was uploaded."""


# ── config ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class YouTubeConfig:
    profile: str
    vp_brand: str
    vp_root: str
    channel_id: str
    content_root: str
    hour: int
    minute: int
    tz: ZoneInfo
    per_day: int
    daily_limit: int
    upload_units: int
    comment_units: int
    category_id: str


def _positive_int(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise YouTubeRefused("%s must be a whole number >= 1, got %r" % (where, value))
    return value


def youtube_config(profile, profile_cfg=None) -> YouTubeConfig:
    """``scheduled_posting.platforms.youtube`` of ``profile``, validated.

    Refuses a profile that is not registered (reading its config would create
    a folder for it), a missing section, a missing or unknown key.
    """
    if profile not in pm.load_profiles()["profiles"]:
        raise YouTubeRefused("no profile %r is registered" % profile)
    cfg = profile_cfg if profile_cfg is not None else pm.get_profile_config(profile)
    section = (cfg or {}).get("scheduled_posting")
    overlays = section.get("platforms") if isinstance(section, dict) else None
    raw = overlays.get(CHANNEL.id) if isinstance(overlays, dict) else None
    if not isinstance(raw, dict):
        raise YouTubeRefused("profile %r has no scheduled_posting.platforms.%s "
                             "section, so YouTube is not set up for it"
                             % (profile, CHANNEL.id))
    where = "scheduled_posting.platforms.%s" % CHANNEL.id
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise YouTubeRefused("%s has unknown keys: %s" % (where, ", ".join(unknown)))
    for key in REQUIRED_KEYS:
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise YouTubeRefused("%s.%s is missing" % (where, key))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw["publish_time"].strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise YouTubeRefused("%s.publish_time must be HH:MM, got %r"
                             % (where, raw["publish_time"]))
    try:
        tz = ZoneInfo(raw["tz"].strip())
    except (KeyError, ValueError) as exc:
        raise YouTubeRefused("%s.tz is not a time zone: %r (%s)"
                             % (where, raw["tz"], type(exc).__name__))
    quota = raw.get("quota", {})
    if not isinstance(quota, dict) or set(quota) - QUOTA_KEYS:
        raise YouTubeRefused("%s.quota takes only %s"
                             % (where, ", ".join(sorted(QUOTA_KEYS))))
    return YouTubeConfig(
        profile=profile,
        vp_brand=raw["vp_brand"].strip(),
        vp_root=raw["vp_root"].strip(),
        channel_id=raw["channel_id"].strip(),
        content_root=raw["content_root"].strip(),
        hour=int(m.group(1)), minute=int(m.group(2)), tz=tz,
        per_day=_positive_int(raw.get("per_day", 1), where + ".per_day"),
        daily_limit=_positive_int(quota.get("daily_limit", DEFAULT_DAILY_LIMIT),
                                  where + ".quota.daily_limit"),
        upload_units=_positive_int(quota.get("upload_units", DEFAULT_UPLOAD_UNITS),
                                   where + ".quota.upload_units"),
        comment_units=_positive_int(quota.get("comment_units", DEFAULT_COMMENT_UNITS),
                                    where + ".quota.comment_units"),
        category_id=str(raw.get("category_id") or DEFAULT_CATEGORY_ID),
    )


# ── files ───────────────────────────────────────────────────────────────────

def channel_dir(profile):
    return os.path.join(pm.DATA_ROOT, profile, CHANNEL.id)


def state_path(profile):
    return os.path.join(channel_dir(profile), STATE_NAME)


def quota_path(profile):
    return os.path.join(channel_dir(profile), QUOTA_NAME)


def token_path(profile):
    return os.path.join(channel_dir(profile), TOKEN_NAME)


def write_json_atomic(path, obj):
    """Write ``obj`` to ``path`` through ``<path>.tmp`` and a rename, so a
    reader sees the old file or the new one, never half of one."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default):
    """The JSON object at ``path``, or ``default`` when there is no file.

    An unreadable file refuses rather than reading as empty: an empty state
    would make every uploaded video look new.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise YouTubeRefused("%s is unreadable (%s); nothing was done"
                             % (path, type(exc).__name__))
    if not isinstance(data, dict):
        raise YouTubeRefused("%s is not a JSON object; nothing was done" % path)
    return data


def load_state(profile):
    state = read_json(state_path(profile), {"version": 1, "items": {}})
    if not isinstance(state.get("items"), dict):
        raise YouTubeRefused("%s has no items object" % state_path(profile))
    return state


def save_state(profile, state):
    write_json_atomic(state_path(profile), state)


# ── time ────────────────────────────────────────────────────────────────────

def utc_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(value):
    """An ISO-8601 instant (``Z``, an offset, or a YAML datetime) as aware UTC.
    A value with no zone refuses: a time without one is a guess."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("%r has no UTC offset" % (value,))
    return dt.astimezone(timezone.utc)


def publish_at_refusal(publish_at, now):
    """Why an item may not be uploaded now, or None. No date is a refusal here:
    this channel only uploads scheduled videos."""
    try:
        when = parse_instant(publish_at)
    except ValueError as exc:
        return "invalid publish_at: %s" % exc
    if when is None:
        return "no publish_at"
    if when - now < PUBLISH_AT_MIN_LEAD:
        return ("publish_at %s is less than %d minutes ahead"
                % (utc_z(when), PUBLISH_AT_MIN_LEAD.total_seconds() // 60))
    return None


# ── intake (the topfive layout) ─────────────────────────────────────────────

def strip_angle_brackets(text):
    """YouTube rejects < and > in titles and descriptions."""
    return text.replace("<", "").replace(">", "")


def parse_youtube_shorts(text):
    """``{"title", "description", "tags"}`` from the ``### YouTube Shorts``
    section of ``youtube.md``, or None when there is no such section.

    Only that section is read. Description: continuation lines with their
    indentation removed, paragraphs joined by one blank line. Tags: comma
    separated. A field the section lacks is None.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = next((i for i, line in enumerate(lines)
                  if SHORTS_HEADING_RE.match(line)), None)
    if start is None:
        return None
    fields = {}
    current = None
    for line in lines[start + 1:]:
        if HEADING_RE.match(line):
            break
        m = FIELD_RE.match(line)
        if m:
            current = m.group(1).strip().lower()
            fields[current] = [m.group(2)] if m.group(2).strip() else []
        elif current is not None:
            fields[current].append(line)

    def paragraphs(raw):
        paras, cur = [], []
        for line in raw:
            if line.strip():
                cur.append(line.strip())
            elif cur:
                paras.append("\n".join(cur))
                cur = []
        if cur:
            paras.append("\n".join(cur))
        return paras

    title = description = tags = None
    if "title" in fields:
        title = " ".join(" ".join(x.strip() for x in fields["title"]).split()) or None
    if "description" in fields:
        description = "\n\n".join(paragraphs(fields["description"])) or None
    if "tags" in fields:
        joined = " ".join(x.strip() for x in fields["tags"])
        tags = [t.strip() for t in joined.split(",") if t.strip()] or None
    return {"title": title, "description": description, "tags": tags}


def youtube_tags_length(tags):
    """What YouTube counts against its 500: a tag with a space counts with
    its quotes, plus one comma between tags."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)


def shorts_fields(shorts):
    """``(fields, None)`` or ``(None, reason)``. Never cuts: over a limit skips."""
    if shorts is None:
        return None, "no YouTube Shorts section in youtube.md"
    for key in ("title", "description", "tags"):
        if not shorts.get(key):
            return None, "YouTube Shorts section has no %s" % key.capitalize()
    title = strip_angle_brackets(shorts["title"]).strip()
    description = strip_angle_brackets(shorts["description"]).strip()
    tags = [t for t in (strip_angle_brackets(t).strip() for t in shorts["tags"]) if t]
    if len(title) > TITLE_MAX:
        return None, "title is %d characters, over %d" % (len(title), TITLE_MAX)
    if len(description) > DESCRIPTION_MAX or len(description.encode("utf-8")) > DESCRIPTION_MAX:
        return None, "description is over %d characters/bytes" % DESCRIPTION_MAX
    tag_len = youtube_tags_length(tags)
    if tag_len > TAGS_MAX_TOTAL:
        return None, "tags are %d YouTube-counted characters, over %d" % (tag_len, TAGS_MAX_TOTAL)
    return {"title": title, "description": description, "tags": tags}, None


def find_vertical_mp4(folder):
    """``(path, None)`` for the folder's one vertical mp4, else ``(None, reason)``.
    A horizontal mp4 is never used."""
    exact = os.path.join(folder, os.path.basename(folder) + "-vertical.mp4")
    if os.path.isfile(exact):
        return exact, None
    found = sorted(n for n in os.listdir(folder)
                   if n.lower().endswith("-vertical.mp4")
                   and os.path.isfile(os.path.join(folder, n)))
    if len(found) == 1:
        return os.path.join(folder, found[0]), None
    if not found:
        return None, "no -vertical.mp4"
    return None, "%d -vertical.mp4 files, expected one" % len(found)


def c_number(name):
    m = ITEM_DIR_RE.match(name or "")
    return int(m.group(1)) if m else None


def _order_key(item_id, rec=None):
    n = c_number(item_id)
    return (n if n is not None else float("inf"), item_id)


def find_item_folders(root):
    """``(C-NNN folder names in C-number order, skip messages)``.
    ``_posted/`` and ``ready-to-publish/`` are skipped, as is any other name."""
    if not os.path.isdir(root):
        raise YouTubeRefused("content_root %s is not a folder" % root)
    names, messages = [], []
    for name in sorted(os.listdir(root)):
        if not os.path.isdir(os.path.join(root, name)):
            continue
        if name in SKIP_DIRS:
            messages.append("SKIP (not a video folder): %s/" % name)
        elif c_number(name) is None:
            messages.append("SKIP (not a C-NNN-<slug> folder): %s/" % name)
        else:
            names.append(name)
    return sorted(names, key=_order_key), messages


def read_item_folder(folder):
    """``(fields with media_path, None)`` or ``(None, reason)`` for one folder."""
    md = os.path.join(folder, "youtube.md")
    if not os.path.isfile(md):
        return None, "no youtube.md"
    with open(md, encoding="utf-8-sig", errors="replace") as f:
        fields, reason = shorts_fields(parse_youtube_shorts(f.read()))
    if reason:
        return None, reason
    mp4, reason = find_vertical_mp4(folder)
    if reason:
        return None, reason
    fields["media_path"] = mp4
    return fields, None


def new_record(item_id, now, **fields):
    rec = {"id": item_id, "c_number": c_number(item_id), "status": QUEUED,
           "publish_at": None, "video_id": None, "title": None,
           "description": None, "tags": [], "media_path": None,
           "source_folder": None, "origin": "intake", "vp_folder": None,
           "uploaded_at": None, "attempt_started_at": None, "error": None,
           "created_at": utc_z(now), "updated_at": utc_z(now)}
    rec.update(fields)
    return rec


# ── dates ───────────────────────────────────────────────────────────────────

def slot_time(day, cfg, index):
    """The ``index``-th slot of a day: publish_time, then an hour later each."""
    return (datetime(day.year, day.month, day.day, cfg.hour, cfg.minute, tzinfo=cfg.tz)
            + timedelta(hours=index))


def held_days(state, tz):
    """How many slots each local day already holds: every dated item in any
    state, plus a video that went live with no date on its upload day."""
    held = Counter()
    for rec in state["items"].values():
        when = parse_instant(rec.get("publish_at"))
        if when is None and rec.get("status") == UPLOADED:
            when = parse_instant(rec.get("uploaded_at"))
        if when is not None:
            held[when.astimezone(tz).date()] += 1
    return held


def assign_dates(state, cfg, now):
    """Date every queued item that has no ``publish_at``, in C-number order:
    each gets the next day with a free slot whose time is at least 15 minutes
    ahead. Days already held are not reused; existing dates never change.
    Returns ``[(item_id, publish_at)]`` and updates ``state`` in place."""
    held = held_days(state, cfg.tz)
    taken = {parse_instant(r.get("publish_at")) for r in state["items"].values()
             if r.get("publish_at")}
    todo = sorted((i for i, r in state["items"].items()
                   if r.get("status") == QUEUED and not r.get("publish_at")),
                  key=_order_key)
    assigned = []
    day = now.astimezone(cfg.tz).date()

    def free_slot(d):
        """The first slot of day ``d`` that no item holds and is far enough
        ahead, or None when the day is full (``per_day`` held) or past."""
        if held[d] >= cfg.per_day:
            return None
        for index in range(cfg.per_day):
            when = slot_time(d, cfg, index)
            if when not in taken and when - now >= PUBLISH_AT_MIN_LEAD:
                return when
        return None

    for item_id in todo:
        when = free_slot(day)
        while when is None:
            day += timedelta(days=1)
            when = free_slot(day)
        held[day] += 1
        taken.add(when.astimezone(timezone.utc))
        rec = state["items"][item_id]
        rec["publish_at"] = utc_z(when)
        rec["updated_at"] = utc_z(now)
        assigned.append((item_id, rec["publish_at"]))
    return assigned


# ── quota ledger ────────────────────────────────────────────────────────────

def quota_day(now):
    return now.astimezone(PACIFIC).date().isoformat()


def load_quota(profile):
    ledger = read_json(quota_path(profile), {"version": 1, "days": {}})
    if not isinstance(ledger.get("days"), dict):
        raise YouTubeRefused("%s has no days object" % quota_path(profile))
    return ledger


def quota_used(ledger, day):
    return int((ledger["days"].get(day) or {}).get("units", 0))


def quota_refusal(ledger, day, units, limit):
    used = quota_used(ledger, day)
    if used + units > limit:
        return ("quota: %d of %d units used on %s (Pacific); %d more would "
                "exceed it" % (used, limit, day, units))
    return None


def charge(profile, ledger, now, units, op, item=None):
    """Record ``units`` against today's Pacific quota day, before the call."""
    day = quota_day(now)
    entry = ledger["days"].setdefault(day, {"units": 0, "entries": []})
    entry["units"] = int(entry.get("units", 0)) + units
    entry.setdefault("entries", []).append(
        {"at": utc_z(now), "op": op, "units": units, "item": item})
    write_json_atomic(quota_path(profile), ledger)


def log_paid_call(endpoint, estimated_cost=0.0, now=None):
    """Append ``{timestamp, api, endpoint, estimated_cost}`` to the paid-call
    ledger. Called before the call; if it cannot be written the call is not made."""
    now = now or datetime.now(timezone.utc)
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": now.isoformat(), "api": "youtube",
                            "endpoint": endpoint,
                            "estimated_cost": estimated_cost}) + "\n")


# ── the one-publisher marker ────────────────────────────────────────────────

def marker_path(cfg):
    return os.path.join(cfg.vp_root, MARKER_RELPATH)


def read_marker(path):
    """The brands listed as moved. No file is no brands. A file that is not
    ``{brands: [names]}`` refuses: an unreadable marker cannot say who publishes."""
    import yaml
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise YouTubeRefused("%s is unreadable (%s)" % (path, type(exc).__name__))
    brands = data.get("brands") if isinstance(data, dict) else None
    if not isinstance(brands, list) or not all(isinstance(b, str) and b.strip()
                                               for b in brands):
        raise YouTubeRefused("%s is not {brands: [names]}" % path)
    return {b.strip() for b in brands}


def write_marker(path, brands):
    import yaml
    parent = os.path.dirname(path)
    tmp = path + ".tmp"
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Brands whose YouTube channel the linkedin.commentor platform "
                "publishes (DEC-058).\n# video-publisher refuses to upload for "
                "these. Written by youtube-cutover.\n")
        yaml.safe_dump({"brands": sorted(brands)}, f, default_flow_style=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── token ───────────────────────────────────────────────────────────────────

def token_problem(profile):
    """Why the stored token cannot be used, or None. Reads the file only."""
    path = token_path(profile)
    if not os.path.exists(path):
        return ("no OAuth token at %s. Run tools/youtube_auth.py --profile %s "
                "--client-secrets <path>" % (path, profile))
    try:
        token = read_json(path, {})
    except YouTubeRefused as exc:
        return str(exc)
    missing = [f for f in youtube_api.TOKEN_FIELDS + ("channel_id",) if not token.get(f)]
    if missing:
        return "the token at %s has no %s" % (path, ", ".join(missing))
    return None


def save_token(profile, token):
    write_json_atomic(token_path(profile), token)


# ── video-publisher's records (read-only) ───────────────────────────────────

def read_vp(cfg):
    """video-publisher's view of this brand's YouTube items, read-only.

    Returns ``{vp item name: {"sub", "manifest", "row"}}``: the manifest from
    ``content/<brand>/{queue,published}/`` and the latest ``publish_log`` row
    (by id) for that item. Instagram (``platform`` other than youtube) is
    excluded. The DB is opened ``mode=ro`` and closed before returning;
    video-publisher's own ``get_connection`` writes, so it is never used.
    """
    import yaml
    found = {}
    for sub in ("queue", "published"):
        root = os.path.join(cfg.vp_root, "content", cfg.vp_brand, sub)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            mpath = os.path.join(root, name, "manifest.yaml")
            if not os.path.isfile(mpath):
                continue
            try:
                with open(mpath, encoding="utf-8") as f:
                    manifest = yaml.safe_load(f)
            except (OSError, yaml.YAMLError) as exc:
                raise YouTubeRefused("%s is unreadable (%s); try again later"
                                     % (mpath, type(exc).__name__))
            if not isinstance(manifest, dict) or manifest.get("platform") != CHANNEL.id:
                continue
            entry = found.setdefault(name, {"subs": [], "manifest": None, "row": None})
            entry["subs"].append(sub)
            # published/ wins: it is the copy the upload moved.
            if entry["manifest"] is None or sub == "published":
                entry["manifest"] = manifest
    db = os.path.join(cfg.vp_root, VP_DB_RELPATH)
    if os.path.isfile(db):
        uri = "file:%s?mode=ro" % db.replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, content_folder, status, publish_at, updated_at, "
                "platform_response FROM publish_log WHERE brand = ? AND "
                "platform = ? ORDER BY id", (cfg.vp_brand, CHANNEL.id)).fetchall()
        finally:
            conn.close()
        for r in rows:
            name = r["content_folder"].rsplit("/", 1)[-1]
            entry = found.setdefault(name, {"subs": [], "manifest": None, "row": None})
            entry["row"] = dict(r)  # ORDER BY id: the last one wins
    return found


def _vp_video_id(row):
    try:
        return (json.loads(row.get("platform_response") or "null") or {}).get("video_id")
    except (ValueError, AttributeError):
        return None


def _utc_naive(value):
    """video-publisher's ``updated_at`` is UTC with no zone marker (V1-A 3.6)."""
    if not value:
        return None
    return utc_z(datetime.fromisoformat(str(value).strip()).replace(tzinfo=timezone.utc))


def imported_record(cfg, name, entry, now):
    """``(record, None)`` for one video-publisher item, or ``(None, reason)``."""
    item_id = name[:-len(VP_SUFFIX)] if name.endswith(VP_SUFFIX) else name
    manifest = entry.get("manifest") or {}
    row = entry.get("row")
    try:
        manifest_at = parse_instant(manifest.get("publish_at"))
        row_at = parse_instant(row.get("publish_at")) if row else None
    except ValueError as exc:
        return None, "bad publish_at: %s" % exc
    publish_at = row_at or manifest_at
    fields = {"title": manifest.get("title"), "description": manifest.get("description"),
              "tags": manifest.get("tags") or [], "origin": "video-publisher",
              "vp_folder": "%s/%s/%s" % (cfg.vp_brand, (entry.get("subs") or ["?"])[-1], name),
              "publish_at": utc_z(publish_at) if publish_at else None}
    source = None
    if c_number(item_id) is not None:
        for cand in (os.path.join(cfg.content_root, item_id),
                     os.path.join(cfg.content_root, "_posted", item_id)):
            if os.path.isdir(cand):
                source = cand
                break
    else:
        source = os.path.join(cfg.vp_root, "content", cfg.vp_brand,
                              (entry.get("subs") or ["queue"])[-1], name)
    fields["source_folder"] = source
    if source and os.path.isdir(source) and c_number(item_id) is not None:
        mp4, _reason = find_vertical_mp4(source)
        fields["media_path"] = mp4
    status = row.get("status") if row else None
    if status == "published":
        video_id = _vp_video_id(row)
        if not video_id:
            return None, "publish_log row %s says published but has no video id" % row.get("id")
        fields.update(status=UPLOADED, video_id=video_id,
                      uploaded_at=_utc_naive(row.get("updated_at")))
    elif status == "attempting":
        fields.update(status=ATTEMPTING, attempt_started_at=_utc_naive(row.get("updated_at")),
                      error="video-publisher has this item attempting (publish_log row %s)"
                            % row.get("id"))
    elif "queue" in (entry.get("subs") or []):
        fields.update(status=QUEUED)
    elif status == "failed":
        fields.update(status=FAILED, error="video-publisher recorded it failed")
    else:
        return None, "no publish_log row and not in queue/"
    return new_record(item_id, now, **fields), None


_COMPARED = ("status", "publish_at", "video_id", "uploaded_at", "vp_folder",
             "source_folder", "media_path", "title", "description", "tags")


def merge_item(existing, new, now):
    """``(action, record, note)`` for importing ``new`` over ``existing``.

    Never duplicates (one key per item), never moves an existing date, never
    lets video-publisher's record overwrite an upload this app made, and never
    picks between two video ids for one item: that is a conflict, reported.
    """
    if existing is None:
        return "created", new, None
    rec = dict(existing)
    note = None
    if existing.get("status") == UPLOADED:
        if new["status"] == UPLOADED and new["video_id"] != existing.get("video_id"):
            return "conflict", existing, ("two video ids: here %s, video-publisher %s"
                                          % (existing.get("video_id"), new["video_id"]))
        if new["status"] != UPLOADED:
            return "unchanged", existing, None
        keep = ("uploaded_at",) if existing.get("origin") != "video-publisher" else ()
        for k in _COMPARED:
            if k not in keep and new.get(k) is not None:
                rec[k] = new[k]
    elif new["status"] == UPLOADED:
        if existing.get("status") == ATTEMPTING and existing.get("origin") != "video-publisher":
            return "conflict", existing, ("this app is attempting it and video-publisher "
                                          "says it uploaded %s" % new["video_id"])
        for k in _COMPARED:
            if new.get(k) is not None:
                rec[k] = new[k]
        rec.update(attempt_started_at=None, error=None)
    elif new["status"] == ATTEMPTING and existing.get("status") == QUEUED:
        rec.update(status=ATTEMPTING, attempt_started_at=new["attempt_started_at"],
                   error=new["error"])
    else:
        for k in ("vp_folder", "title", "description", "tags", "source_folder", "media_path"):
            if not rec.get(k) and new.get(k):
                rec[k] = new[k]
        if not rec.get("publish_at") and new.get("publish_at"):
            rec["publish_at"] = new["publish_at"]
        elif new.get("publish_at") and rec.get("publish_at") != new["publish_at"]:
            note = ("dates differ: kept %s, video-publisher has %s"
                    % (rec["publish_at"], new["publish_at"]))
    if all(rec.get(k) == existing.get(k) for k in _COMPARED + ("attempt_started_at", "error")):
        return "unchanged", existing, note
    rec["updated_at"] = utc_z(now)
    return "updated", rec, note


def plan_import(cfg, state, now):
    """Every merge the import would make, without writing. Returns a summary
    with ``plan`` = ``[(action, item_id, record, note)]`` and ``unmatched``."""
    vp = read_vp(cfg)
    plan, unmatched = [], []
    for name in sorted(vp, key=lambda n: _order_key(n)):
        new, reason = imported_record(cfg, name, vp[name], now)
        if new is None:
            unmatched.append((name, reason))
            continue
        action, rec, note = merge_item(state["items"].get(new["id"]), new, now)
        plan.append((action, new["id"], rec, note))
    counts = Counter(a for a, *_ in plan)
    return {"plan": plan, "unmatched": unmatched, "counts": dict(counts)}


def vp_blockers(vp):
    """What in video-publisher must settle before cutover: an item whose latest
    publish_log row is ``attempting``, or a queued item with no ``publish_at``
    that it has not already published (so it still means to upload it)."""
    out = []
    for name in sorted(vp, key=_order_key):
        entry = vp[name]
        row = entry.get("row") or {}
        if row.get("status") == "attempting":
            out.append("%s: video-publisher has it attempting (publish_log row %s)"
                       % (name, row.get("id")))
        elif ("queue" in entry.get("subs", []) and row.get("status") != "published"
              and not (entry.get("manifest") or {}).get("publish_at")):
            out.append("%s: queued in video-publisher with no publish_at, not yet "
                       "uploaded" % name)
    return out


# ── display ─────────────────────────────────────────────────────────────────

def display_state(rec, now):
    """queued, attempting, failed, scheduled (uploaded, private until its
    publish_at) or live."""
    status = rec.get("status")
    if status != UPLOADED:
        return status
    when = parse_instant(rec.get("publish_at"))
    return "scheduled" if when is not None and when > now else "live"


def _local_label(iso, tz=PACIFIC):
    when = parse_instant(iso)
    return when.astimezone(tz).strftime("%a %Y-%m-%d %H:%M %Z") if when else "-"


def _state_lines(state, now):
    counts = Counter(display_state(r, now) for r in state["items"].values())
    return ", ".join("%s %d" % (k, counts[k]) for k in
                     ("live", "scheduled", "queued", "attempting", "failed")
                     if counts.get(k)) or "no items"


# ── command runner ──────────────────────────────────────────────────────────

def _run(profile, name, dry_run, out, body):
    """Config, the production gate, then ``body(cfg, summary)`` under the
    channel's publisher lock. Returns ``(exit_code, summary)``."""
    tag = "youtube-%s %s%s" % (name, profile, " [dry-run]" if dry_run else "")
    summary = {"profile": profile, "command": name, "dry_run": dry_run}

    def refuse(code, msg):
        line = "%s: REFUSED: %s" % (tag, msg)
        logger.warning(line)
        out(line)
        summary["refused"] = msg
        return code, summary

    if CHANNEL.publisher != channels.YOUTUBE_API:
        return refuse(EXIT_REFUSED, "the youtube channel is not declared as "
                      "published through the YouTube API")
    try:
        cfg = youtube_config(profile)
        pm.check_declared_production(
            cfg.channel_id, "publish to YouTube channel %s" % cfg.channel_id,
            allow_production=False)
    except YouTubeRefused as exc:
        return refuse(EXIT_REFUSED, str(exc))
    except pm.ProductionAccessRefused as exc:
        return refuse(EXIT_PRODUCTION_REFUSED, str(exc))
    try:
        with publisher_lock.channel_lock(state_path(profile), tag):
            return body(cfg, summary, tag, refuse)
    except publisher_lock.PublisherBusy as exc:
        return refuse(EXIT_REFUSED, str(exc))
    except YouTubeRefused as exc:
        return refuse(EXIT_REFUSED, str(exc))


# ── youtube-intake ──────────────────────────────────────────────────────────

def intake(profile, dry_run=False, now=None, out=print):
    """Add every new ``C-NNN-<slug>/`` folder under ``content_root`` to the
    state as queued, with its ``youtube.md`` Shorts text verbatim. A folder
    already known (by name) is left alone. Content folders are only read."""
    now = now or datetime.now(timezone.utc)

    def body(cfg, summary, tag, refuse):
        state = load_state(profile)
        names, messages = find_item_folders(cfg.content_root)
        for m in messages:
            out("  " + m)
        new, known, skipped = [], [], []
        for item_id in names:
            if item_id in state["items"]:
                known.append(item_id)
                out("  KNOWN (%s): %s" % (display_state(state["items"][item_id], now), item_id))
                continue
            folder = os.path.join(cfg.content_root, item_id)
            fields, reason = read_item_folder(folder)
            if reason:
                skipped.append((item_id, reason))
                out("  SKIP (%s): %s" % (reason, item_id))
                continue
            state["items"][item_id] = new_record(item_id, now, source_folder=folder, **fields)
            new.append(item_id)
            out("  %s: %s  %r" % ("WOULD ADD" if dry_run else "ADDED", item_id, fields["title"]))
        if new and not dry_run:
            save_state(profile, state)
        summary.update(new=new, known=known, skipped=skipped)
        out("%s: %s %d, known %d, skipped %d" % (
            tag, "would add" if dry_run else "added", len(new), len(known), len(skipped)))
        return EXIT_OK, summary

    return _run(profile, "intake", dry_run, out, body)


# ── youtube-import (V1-M) ───────────────────────────────────────────────────

def _apply_import(profile, state, result, dry_run, out):
    for action, item_id, rec, note in result["plan"]:
        if action in ("created", "updated") and not dry_run:
            state["items"][item_id] = rec
        line = "  %s: %s  %s  %s  %s" % (
            action.upper(), item_id, rec.get("status"), rec.get("video_id") or "-",
            rec.get("publish_at") or "-")
        out(line + ("  (%s)" % note if note else ""))
    for name, reason in result["unmatched"]:
        out("  UNMATCHED: %s  (%s)" % (name, reason))
    if not dry_run and any(a in ("created", "updated") for a, *_ in result["plan"]):
        save_state(profile, state)


def import_history(profile, dry_run=False, now=None, out=print):
    """Import video-publisher's history for ``vp_brand`` (V1-M). Read-only on
    video-publisher; idempotent here (a re-run updates, never duplicates)."""
    now = now or datetime.now(timezone.utc)

    def body(cfg, summary, tag, refuse):
        state = load_state(profile)
        result = plan_import(cfg, state, now)
        _apply_import(profile, state, result, dry_run, out)
        summary.update(counts=result["counts"], unmatched=result["unmatched"])
        c = result["counts"]
        out("%s: created %d, updated %d, unchanged %d, conflict %d, unmatched %d"
            % (tag, c.get("created", 0), c.get("updated", 0), c.get("unchanged", 0),
               c.get("conflict", 0), len(result["unmatched"])))
        return (EXIT_ERROR if c.get("conflict") else EXIT_OK), summary

    return _run(profile, "import", dry_run, out, body)


# ── youtube-cutover ─────────────────────────────────────────────────────────

def cutover(profile, dry_run=False, now=None, out=print):
    """Move ``vp_brand``'s YouTube publishing here. In order: the token exists
    (no network); video-publisher has nothing attempting and no undated queued
    item still to upload; re-run the import; add the brand to the marker;
    print the state before and after. ``--dry-run`` writes nothing."""
    now = now or datetime.now(timezone.utc)

    def body(cfg, summary, tag, refuse):
        mpath = marker_path(cfg)
        state = load_state(profile)
        brands = read_marker(mpath)
        out("BEFORE: marker %s lists %s; here: %s"
            % (mpath, ", ".join(sorted(brands)) or "no brands", _state_lines(state, now)))
        blockers = []
        problem = token_problem(profile)
        if problem:
            blockers.append(problem)
        blockers.extend(vp_blockers(read_vp(cfg)))
        for b in blockers:
            out("  BLOCKER: %s" % b)
        summary["blockers"] = blockers
        if blockers and not dry_run:
            return refuse(EXIT_REFUSED, "%d blocker(s) above; nothing was written"
                          % len(blockers))
        result = plan_import(cfg, state, now)
        summary["counts"] = result["counts"]
        if result["counts"].get("conflict"):
            _apply_import(profile, state, result, True, out)  # list it, write nothing
            return refuse(EXIT_REFUSED, "the import has conflicts; nothing was "
                          "written and the marker was not touched")
        _apply_import(profile, state, result, dry_run, out)
        if cfg.vp_brand in brands:
            out("  MARKER: %s already lists %s" % (mpath, cfg.vp_brand))
        elif dry_run:
            out("  MARKER: would add %s to %s" % (cfg.vp_brand, mpath))
        else:
            write_marker(mpath, brands | {cfg.vp_brand})
            out("  MARKER: added %s to %s" % (cfg.vp_brand, mpath))
        after = load_state(profile) if not dry_run else state
        out("AFTER%s: marker lists %s; here: %s" % (
            " (dry run, nothing written)" if dry_run else "",
            ", ".join(sorted(read_marker(mpath) if not dry_run else brands | {cfg.vp_brand})),
            _state_lines(after, now)))
        if blockers:
            return refuse(EXIT_REFUSED, "%d blocker(s) above; a real run would "
                          "write nothing" % len(blockers))
        return EXIT_OK, summary

    return _run(profile, "cutover", dry_run, out, body)


# ── youtube-schedule ────────────────────────────────────────────────────────

def build_upload_body(rec, cfg):
    """The ``videos.insert`` body: private, with ``status.publishAt`` in UTC."""
    return {
        "snippet": {"title": rec["title"], "description": rec["description"],
                    "tags": list(rec.get("tags") or []), "categoryId": cfg.category_id},
        "status": {"privacyStatus": "private", "publishAt": rec["publish_at"],
                   "selfDeclaredMadeForKids": False},
    }


def schedule(profile, max_uploads=None, dry_run=False, now=None, api=None, out=print):
    """Date undated queued items, then upload dated ones, earliest first, each
    private with its publishAt. Stops at the first upload error with no retry;
    the rest stay queued with their dates."""
    now = now or datetime.now(timezone.utc)
    api = api or youtube_api
    if max_uploads is not None and (isinstance(max_uploads, bool)
                                    or not isinstance(max_uploads, int) or max_uploads < 0):
        raise ValueError("max_uploads must be >= 0")

    def body(cfg, summary, tag, refuse):
        brands = read_marker(marker_path(cfg))
        if cfg.vp_brand not in brands:
            return refuse(EXIT_REFUSED, "video-publisher still publishes brand %r "
                          "(not in %s). Run youtube-cutover first."
                          % (cfg.vp_brand, marker_path(cfg)))
        problem = token_problem(profile)
        if problem:
            return refuse(EXIT_REFUSED, problem)
        state = load_state(profile)
        stuck = sorted(i for i, r in state["items"].items() if r.get("status") == ATTEMPTING)
        if stuck:
            return refuse(EXIT_REFUSED, "upload attempt(s) not resolved: %s. Check "
                          "YouTube Studio, then set each record's status by hand "
                          "(uploaded with its video_id, or queued)" % ", ".join(stuck))
        assigned = assign_dates(state, cfg, now)
        for item_id, at in assigned:
            out("  %s: %s  %s" % ("WOULD DATE" if dry_run else "DATED", item_id,
                                  _local_label(at, cfg.tz)))
        if assigned and not dry_run:
            save_state(profile, state)
        dated = sorted((i for i, r in state["items"].items()
                        if r.get("status") == QUEUED and r.get("publish_at")),
                       key=lambda i: (state["items"][i]["publish_at"], _order_key(i)))
        summary.update(dated=assigned, uploaded=[], skipped=[], stopped=None, remaining=[])
        ledger = load_quota(profile)
        day = quota_day(now)
        if dry_run:
            projected = quota_used(ledger, day) + CHANNEL_CHECK_UNITS
            n = 0
            for item_id in dated:
                rec = state["items"][item_id]
                reason = publish_at_refusal(rec["publish_at"], now)
                if reason:
                    out("  WOULD SKIP: %s  (%s)" % (item_id, reason))
                    continue
                if max_uploads is not None and n >= max_uploads:
                    out("  WOULD LEAVE QUEUED: %s  (--max-uploads %d)" % (item_id, max_uploads))
                    continue
                if projected + cfg.upload_units > cfg.daily_limit:
                    out("  WOULD LEAVE QUEUED: %s  (quota)" % item_id)
                    continue
                projected += cfg.upload_units
                n += 1
                out("  WOULD UPLOAD: %s  publishAt %s (%s)"
                    % (item_id, rec["publish_at"], _local_label(rec["publish_at"], cfg.tz)))
            out("%s: would date %d, would upload %d; quota %d of %d on %s"
                % (tag, len(assigned), n, quota_used(ledger, day), cfg.daily_limit, day))
            return EXIT_OK, summary
        return _upload_dated(profile, cfg, state, ledger, dated, max_uploads, now,
                             api, out, summary, tag, refuse)

    return _run(profile, "schedule", dry_run, out, body)


def _upload_dated(profile, cfg, state, ledger, dated, max_uploads, now, api, out,
                  summary, tag, refuse):
    todo = []
    for item_id in dated:
        reason = publish_at_refusal(state["items"][item_id]["publish_at"], now)
        if reason:
            out("  SKIP: %s  (%s; left queued, re-date it by hand)" % (item_id, reason))
            summary["skipped"].append((item_id, reason))
        else:
            todo.append(item_id)
    if max_uploads is not None:
        summary["remaining"].extend(todo[max_uploads:])
        todo = todo[:max_uploads]
    if not todo:
        out("%s: nothing to upload" % tag)
        return EXIT_OK, summary
    reason = quota_refusal(ledger, quota_day(now), CHANNEL_CHECK_UNITS + cfg.upload_units,
                           cfg.daily_limit)
    if reason:
        summary["remaining"] = todo + summary["remaining"]
        return refuse(EXIT_REFUSED, reason + "; nothing was uploaded")
    try:
        creds = api.credentials_from_token(read_json(token_path(profile), {}))
        log_paid_call("oauth2 token refresh (%s)" % profile, now=now)
        api.refresh(creds)
        service = api.build_service(creds)
        charge(profile, ledger, now, CHANNEL_CHECK_UNITS, "channels.list")
        log_paid_call("channels.list mine (identity check, 1 unit)", now=now)
        found = api.my_channel_id(service)
    except youtube_api.YouTubeApiError as exc:
        summary["remaining"] = todo + summary["remaining"]
        summary["stopped"] = {"item": None, "error": str(exc)}
        out("%s: STOP before any upload: %s" % (tag, exc))
        return EXIT_ERROR, summary
    if found != cfg.channel_id:
        summary["remaining"] = todo + summary["remaining"]
        return refuse(EXIT_REFUSED, "the token answers for channel %s, not the "
                      "configured %s; nothing was uploaded" % (found, cfg.channel_id))
    for idx, item_id in enumerate(todo):
        rec = state["items"][item_id]
        media = rec.get("media_path")
        problem = None
        if not media or not os.path.isfile(media):
            problem = "media file missing: %s" % media
        elif not rec.get("title") or not rec.get("description"):
            problem = "no title or description"
        reason = quota_refusal(ledger, quota_day(now), cfg.upload_units, cfg.daily_limit)
        if problem or reason:
            summary["remaining"] = todo[idx:] + summary["remaining"]
            summary["stopped"] = {"item": item_id, "error": problem or reason,
                                  "quota": bool(reason and not problem)}
            out("  STOP: %s  (%s; left queued)" % (item_id, problem or reason))
            break
        rec.update(status=ATTEMPTING, attempt_started_at=utc_z(now), error=None,
                   updated_at=utc_z(now))
        save_state(profile, state)
        charge(profile, ledger, now, cfg.upload_units, "videos.insert", item_id)
        log_paid_call("videos.insert resumable %s, %d quota units, publishAt=%s"
                      % (item_id, cfg.upload_units, rec["publish_at"]), now=now)
        out("  UPLOADING: %s" % item_id)
        try:
            video_id = api.upload(service, media, build_upload_body(rec, cfg))
        except youtube_api.YouTubeApiError as exc:
            if exc.quota:
                # Google refused before creating anything.
                rec.update(status=QUEUED, attempt_started_at=None,
                           error="quota: %s" % exc)
                where = "left queued"
            else:
                rec["error"] = str(exc)
                where = "left attempting: check Studio before the next run"
            rec["updated_at"] = utc_z(now)
            save_state(profile, state)
            summary["remaining"] = todo[idx + 1:] + summary["remaining"]
            summary["stopped"] = {"item": item_id, "error": str(exc), "quota": exc.quota}
            out("  STOP: %s  (%s; %s)" % (item_id, exc, where))
            break
        rec.update(status=UPLOADED, video_id=video_id, uploaded_at=utc_z(now),
                   attempt_started_at=None, error=None, updated_at=utc_z(now))
        save_state(profile, state)
        summary["uploaded"].append((item_id, video_id, rec["publish_at"]))
        out("  UPLOADED: %s  video_id=%s  publishAt=%s (%s)"
            % (item_id, video_id, rec["publish_at"], _local_label(rec["publish_at"], cfg.tz)))
    out("%s: uploaded %d, skipped %d, left queued %d%s" % (
        tag, len(summary["uploaded"]), len(summary["skipped"]), len(summary["remaining"]),
        ", stopped" if summary["stopped"] else ""))
    return (EXIT_ERROR if summary["stopped"] else EXIT_OK), summary


# ── youtube-status ──────────────────────────────────────────────────────────

def status(profile, now=None, out=print):
    """Read-only: every item from local state, and today's quota."""
    now = now or datetime.now(timezone.utc)

    def body(cfg, summary, tag, refuse):
        state = load_state(profile)
        ledger = load_quota(profile)
        day = quota_day(now)
        items = sorted(state["items"].values(),
                       key=lambda r: (r.get("publish_at") or r.get("uploaded_at") or "~",
                                      _order_key(r["id"])))
        for r in items:
            out("  %-11s %-48s %-12s %s%s" % (
                display_state(r, now), r["id"], r.get("video_id") or "-",
                _local_label(r.get("publish_at"), cfg.tz) if r.get("publish_at")
                else "uploaded " + _local_label(r.get("uploaded_at"), cfg.tz)
                if r.get("uploaded_at") else "no date",
                "  (%s)" % r["error"] if r.get("error") else ""))
        brands = read_marker(marker_path(cfg))
        out("%s: %s; quota %d of %d on %s (Pacific); token %s; video-publisher "
            "marker %s %s" % (
                tag, _state_lines(state, now), quota_used(ledger, day), cfg.daily_limit,
                day, "missing" if token_problem(profile) else "present",
                "lists" if cfg.vp_brand in brands else "does not list", cfg.vp_brand))
        summary.update(counts=dict(Counter(display_state(r, now) for r in items)),
                       quota_used=quota_used(ledger, day))
        return EXIT_OK, summary

    return _run(profile, "status", False, out, body)


# ── calendar source (U1) ────────────────────────────────────────────────────

#: Calendar group per displayed state.
CALENDAR_GROUPS = {QUEUED: "queued", ATTEMPTING: "failed", FAILED: "failed",
                   "scheduled": "scheduled_youtube", "live": "sent"}
CALENDAR_LABELS = {QUEUED: "Queued", ATTEMPTING: "Upload not resolved",
                   FAILED: "Failed", "scheduled": "Scheduled on YouTube",
                   "live": "Sent"}


def calendar_items(profile, now=None):
    """One calendar item per video in the profile's YouTube state, on its
    Pacific day. Reads the state file only; no file is no items, and nothing
    is created."""
    path = state_path(profile)
    if not os.path.exists(path):
        return []
    now = now or datetime.now(timezone.utc)
    state = read_json(path, {"items": {}})
    out = []
    for item_id, rec in (state.get("items") or {}).items():
        shown = display_state(rec, now)
        iso = rec.get("publish_at") or (rec.get("uploaded_at") if shown == "live" else None)
        when = parse_instant(iso)
        local = when.astimezone(PACIFIC) if when else None
        out.append({
            "platform": CHANNEL.id,
            "platform_label": CHANNEL.label,
            "key": item_id,
            "state": shown,
            "group": CALENDAR_GROUPS.get(shown, shown),
            "title": rec.get("title") or None,
            "preview": "%s%s" % (CALENDAR_LABELS.get(shown, shown),
                                 " - https://youtu.be/%s" % rec["video_id"]
                                 if rec.get("video_id") else ""),
            "image_url": None,
            "day": local.date().isoformat() if local else None,
            "due_at": utc_z(when) if when else None,
            "time_window": None,
            "time_label": local.strftime("%H:%M PT") if local else None,
            "sort_minute": local.hour * 60 + local.minute if local else None,
            "hold_reason": rec.get("error") if shown in (ATTEMPTING, FAILED) else None,
            "errors": [rec["error"]] if rec.get("error") else [],
        })
    return out

