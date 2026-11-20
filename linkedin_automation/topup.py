"""Top Buffer up from a channel's queued rows, headless (D-022 F6, DEC-048).

One call, safe to run from a Windows task several times a day::

    uv run python tools/run_scheduled_posts.py topup --profile tinderbox \\
        --platform pinterest [--reserve 2] [--dry-run]

It reads the channel's PENDING rows in the order the Schedule button sends
them, and sends the earliest into Buffer until ``free - reserve`` are in,
through the same ``schedule_pass`` every other path uses. Before any Buffer
call it resolves the channel through the app's own loader and key (no fallback),
applies the production gate, refuses when the profile's drain is on, and takes
the channel's publisher lock (:mod:`publisher_lock`), which the dashboard's
Schedule button and post drain take too.

Each row it sends must pass two checks first, or it is skipped (left PENDING,
never deleted) and the next row takes its place:

* **board**: in the overlay's ``approved_boards`` AND on the channel's live
  board list (read once per run), on a channel that names boards;
* **link**: answers 2xx with no redirect, and a page title that is neither
  "Redirecting..." nor "Page not found" (DEC-042(3), DEC-043(2)).

When the first queued row's window has already begun, every PENDING row is
first re-dated with ``redate_pending`` and the overlay's ``cadence``, from the
first window that has not begun (and is after the last post already in Buffer).
Keys and order do not move (DEC-033).

Config lives in the profile overlay ``scheduled_posting.platforms.<platform>``:

    "approved_boards": ["Board A", ...],
    "topup_reserve": 2,
    "cadence": {
        "per_day": 5,
        "quiet_days": [{"dates": ["12-24", "12-25"], "per_day": 2}],
        "windows": {"5": ["morning", ...], "2": ["morning", "evening"]}
    }

A channel without ``approved_boards`` or ``cadence`` is refused: there are no
defaults, because silence is not authorization to publish.
"""

import copy
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from . import buffer_client as bc
from . import channels
from . import csv_pipeline as cp
from . import post_drain
from . import profile_manager as pm
from . import publisher_lock

logger = logging.getLogger(__name__)

DEFAULT_RESERVE = 2

EXIT_OK = 0
EXIT_BUFFER_ERROR = 1
EXIT_REFUSED = 2
EXIT_PRODUCTION_REFUSED = pm.EXIT_PRODUCTION_REFUSED

LINK_TIMEOUT_SECONDS = 15
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_MMDD_RE = re.compile(r"^(\d\d)-(\d\d)$")


class TopupRefused(RuntimeError):
    """A refusal before anything was sent: config, drain, or lock."""


# ─── config ──────────────────────────────────────────────────────────────────

@dataclass
class Cadence:
    """A daily count with quiet days, and the windows each count uses."""
    default: int
    quiet: dict = field(default_factory=dict)      # (month, day) -> count
    windows: dict = field(default_factory=dict)    # count -> [window, ...]

    def per_day(self, day: date) -> int:
        return self.quiet.get((day.month, day.day), self.default)

    def windows_for(self, n: int) -> list:
        return [] if n == 0 else list(self.windows[n])


def _count(value, where):
    if not isinstance(value, int) or isinstance(value, bool) \
            or not 0 <= value <= len(bc.TIME_WINDOWS):
        raise TopupRefused("%s must be a whole number 0 to %d, got %r"
                           % (where, len(bc.TIME_WINDOWS), value))
    return value


def parse_cadence(raw, where="cadence") -> Cadence:
    """Validate an overlay's ``cadence`` and return a :class:`Cadence`.

    Every count the calendar can produce must have a window list of exactly
    that many distinct declared windows, so ``redate_pending`` never meets a
    count it cannot place."""
    if not isinstance(raw, dict):
        raise TopupRefused("%s must be an object, got %r" % (where, raw))
    default = _count(raw.get("per_day"), where + ".per_day")
    quiet = {}
    groups = raw.get("quiet_days") or []
    if not isinstance(groups, list):
        raise TopupRefused("%s.quiet_days must be a list" % where)
    for i, group in enumerate(groups):
        gw = "%s.quiet_days[%d]" % (where, i)
        if not isinstance(group, dict) or not isinstance(group.get("dates"), list):
            raise TopupRefused("%s must be {\"dates\": [\"MM-DD\", ...], "
                               "\"per_day\": n}" % gw)
        n = _count(group.get("per_day"), gw + ".per_day")
        for d in group["dates"]:
            m = _MMDD_RE.match(str(d))
            if not m:
                raise TopupRefused("%s date %r is not MM-DD" % (gw, d))
            month, day = int(m.group(1)), int(m.group(2))
            try:
                date(2028, month, day)      # a leap year, so 02-29 is allowed
            except ValueError:
                raise TopupRefused("%s date %r is not a day" % (gw, d)) from None
            if (month, day) in quiet:
                raise TopupRefused("%s date %r is listed twice" % (gw, d))
            quiet[(month, day)] = n
    raw_windows = raw.get("windows")
    if not isinstance(raw_windows, dict):
        raise TopupRefused("%s.windows must map each count to its windows"
                           % where)
    windows = {}
    for k, names in raw_windows.items():
        n = _count(int(k) if str(k).isdigit() else k, "%s.windows key" % where)
        if not isinstance(names, list):
            raise TopupRefused("%s.windows[%r] must be a list" % (where, k))
        names = [str(w).strip().lower() for w in names]
        if len(names) != n or len(set(names)) != n \
                or any(w not in bc.TIME_WINDOWS for w in names):
            raise TopupRefused(
                "%s.windows[%r] must name %d distinct windows of %s, got %r"
                % (where, k, n, ", ".join(bc.TIME_WINDOWS), names))
        windows[n] = sorted(names, key=cp.window_rank)
    for n in {default, *quiet.values()} - {0}:
        if n not in windows:
            raise TopupRefused("%s.windows has no list for a day of %d"
                               % (where, n))
    return Cadence(default=default, quiet=quiet, windows=windows)


def topup_config(profile, platform, profile_cfg=None) -> dict:
    """``{"approved_boards", "cadence", "reserve"}`` from the channel's overlay.

    Raises :class:`TopupRefused` when the overlay or either required field is
    missing or malformed. Nothing is defaulted except the reserve."""
    cfg = profile_cfg if profile_cfg is not None else pm.get_profile_config(profile)
    section = (cfg or {}).get("scheduled_posting")
    overlays = section.get("platforms") if isinstance(section, dict) else None
    overlay = overlays.get(platform) if isinstance(overlays, dict) else None
    where = "scheduled_posting.platforms.%s" % platform
    if not isinstance(overlay, dict):
        raise TopupRefused("profile %r has no %s section, so it has no top-up "
                           "settings. Refusing." % (profile, where))
    missing = [f for f in ("approved_boards", "cadence") if f not in overlay]
    if missing:
        raise TopupRefused(
            "profile %r %s has no %s. topup needs both approved_boards and "
            "cadence for a channel and invents neither. Refusing."
            % (profile, where, " or ".join(missing)))
    boards = overlay["approved_boards"]
    if not isinstance(boards, list) or not boards \
            or not all(isinstance(b, str) and b.strip() for b in boards):
        raise TopupRefused("%s.approved_boards must be a non-empty list of "
                           "board names" % where)
    reserve = overlay.get("topup_reserve", DEFAULT_RESERVE)
    if not isinstance(reserve, int) or isinstance(reserve, bool) or reserve < 0:
        raise TopupRefused("%s.topup_reserve must be a whole number >= 0, "
                           "got %r" % (where, reserve))
    return {"approved_boards": list(boards),
            "cadence": parse_cadence(overlay["cadence"], where + ".cadence"),
            "reserve": reserve}


def drain_is_on(profile_cfg, resolved) -> bool:
    """Is the post drain switched on for this profile or this channel?

    The drain engine reads the profile-wide switch; an overlay may also carry
    one (a bool or a ``{"enabled": ...}`` section). Either one on refuses."""
    if post_drain.drain_config(profile_cfg)["enabled"]:
        return True
    own = (resolved or {}).get("drain")
    if isinstance(own, dict):
        return bool(own.get("enabled"))
    return bool(own)


# ─── checks ──────────────────────────────────────────────────────────────────

def fetch_page(url):
    """GET ``url`` without following redirects. Returns ``(status, title)``.

    The fragment is not sent (it never is). Raises ``requests`` errors."""
    import requests
    resp = requests.get(url.split("#", 1)[0], allow_redirects=False,
                        timeout=LINK_TIMEOUT_SECONDS,
                        headers={"User-Agent": "Mozilla/5.0"})
    m = _TITLE_RE.search(resp.text or "")
    title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else None
    return resp.status_code, title


def link_problem(url, fetch=None):
    """Why ``url`` must not be sent, or None when it passes."""
    if not url:
        return "no link"
    try:
        status, title = (fetch or fetch_page)(url)
    except Exception as exc:
        return "link error %s" % type(exc).__name__
    if 300 <= status < 400:
        return "link redirects (%d)" % status
    if not 200 <= status < 300:
        return "link status %d" % status
    if not title:
        return "link page has no title"
    if title.startswith("Redirecting"):
        return "link title %r" % title
    if "page not found" in title.lower():
        return "link title %r" % title
    return None


def board_problem(row, approved, live_boards):
    """Why the row's board must not be used, or None when it passes."""
    name = cp.row_board(row)
    if name not in approved:
        return "board %r not approved" % name
    try:
        bc.resolve_board(live_boards, name)
    except bc.BoardLookupError as exc:
        return "board %r not usable in Buffer: %s" % (name, exc)
    return None


# ─── the run ─────────────────────────────────────────────────────────────────

def _window_start(row, tz):
    sh, sm, _eh, _em = bc.TIME_WINDOWS[str(row["time_window"]).strip().lower()]
    d = datetime.strptime(str(row["date"]).strip(), "%Y-%m-%d")
    return d.replace(hour=sh, minute=sm, tzinfo=tz)


def _pending(state):
    """PENDING entries only, in the Schedule button's order: ``(key, row)``."""
    waiting = [(k, v) for k, v in state.rows.items()
               if v.get("status") == cp.PENDING and v.get("row")]
    waiting.sort(key=cp._waiting_sort_key)
    return [(k, dict(v["row"])) for k, v in waiting]


def _last_buffer_due(state):
    dues = [v.get("due_at") for v in state.rows.values()
            if v.get("status") == cp.SCHEDULED and v.get("due_at")]
    return max(dues, key=_parse_iso) if dues else None


def _parse_iso(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _redate_start(state, now):
    """Re-date from the first window that has not begun, and that is after
    the last post already in Buffer."""
    last = _last_buffer_due(state)
    if last and _parse_iso(last) > now:
        return _parse_iso(last)
    return None


def run(profile, platform, reserve=None, dry_run=False, now=None,
        fetch=None, out=print):
    """One top-up. Returns ``(exit_code, summary)``; ``out`` gets each line."""
    platform = (platform or "").strip().lower()
    now = now or datetime.now(timezone.utc)
    tag = "topup %s/%s%s" % (profile, platform, " [dry-run]" if dry_run else "")
    summary = {"profile": profile, "platform": platform, "dry_run": dry_run,
               "free": None, "reserve": None, "n": None, "scheduled": [],
               "skipped": [], "redated": 0, "planned": [],
               "last_buffer_due": None, "first_pending_due": None}

    def refuse(code, msg):
        line = "%s: REFUSED: %s" % (tag, msg)
        logger.warning(line)
        out(line)
        summary["refused"] = msg
        return code, summary

    try:
        resolved = pm.resolve_scheduled(profile, platform)
    except pm.ScheduledConfigError as exc:
        return refuse(EXIT_REFUSED, str(exc))
    channel_id = resolved.get("channel_id")
    if not channel_id:
        return refuse(EXIT_REFUSED, "no buffer_channel_id for %s/%s"
                      % (profile, platform))
    profile_cfg = pm.get_profile_config(profile)
    try:
        conf = topup_config(profile, platform, profile_cfg)
    except TopupRefused as exc:
        return refuse(EXIT_REFUSED, str(exc))
    reserve = conf["reserve"] if reserve is None else reserve
    if not isinstance(reserve, int) or reserve < 0:
        return refuse(EXIT_REFUSED, "reserve must be >= 0, got %r" % reserve)
    summary["reserve"] = reserve

    try:
        pm.check_declared_production(
            resolved.get("identity_slug"),
            "top up Buffer on %s" % platform, allow_production=False)
    except pm.ProductionAccessRefused as exc:
        return refuse(EXIT_PRODUCTION_REFUSED, str(exc))

    if drain_is_on(profile_cfg, resolved):
        return refuse(EXIT_REFUSED,
                      "the post drain is on for profile %r, and two "
                      "publishers must never feed one channel. Turn the drain "
                      "off, or let it feed this channel instead." % profile)

    channel = channels.resolve(platform)
    state = cp.PipelineState(profile_name=profile, platform=platform)
    try:
        with publisher_lock.channel_lock(
                state.path, "topup %s/%s%s" % (profile, platform,
                                              " dry-run" if dry_run else "")):
            state.reload()
            return _locked(tag, summary, state, resolved, channel, conf,
                           reserve, dry_run, now, fetch, out)
    except publisher_lock.PublisherBusy as exc:
        return refuse(EXIT_REFUSED, str(exc))


def _locked(tag, summary, state, resolved, channel, conf, reserve, dry_run,
            now, fetch, out):
    platform = summary["platform"]
    key = resolved.get("api_key")
    channel_id = resolved["channel_id"]
    tz = bc.posting_timezone()

    try:
        slots = bc.scheduled_slots(channel_id, key=key, max_age=0)
    except Exception as exc:
        return _finish(tag, summary, state, out, EXIT_BUFFER_ERROR,
                       "could not read Buffer's free slots: %s" % exc)
    summary["free"] = slots["free"]
    n = slots["free"] - reserve
    summary["n"] = n
    if n <= 0:
        return _finish(tag, summary, state, out, EXIT_OK, "nothing to do")

    work = state
    if dry_run:
        # A copy whose save() is a no-op, so a re-date can be shown in full
        # without a write.
        work = copy.deepcopy(state)
        work.save = lambda: None

    pending = _pending(work)
    if not pending:
        return _finish(tag, summary, work, out, EXIT_OK, "no PENDING rows")

    if _window_start(pending[0][1], tz) <= now.astimezone(tz):
        cadence = conf["cadence"]
        changes = cp.redate_pending(
            summary["profile"], platform, cadence.per_day,
            start=_redate_start(work, now), now=now, state=work,
            windows_for=cadence.windows_for)
        summary["redated"] = len(changes)
        if changes:
            line = ("%s: first queued row was overdue; re-dated %d row(s) "
                    "from %s %s" % (tag, len(changes), changes[0]["date"],
                                    changes[0]["time_window"]))
            logger.info(line)
            out(line)
        pending = _pending(work)

    live_boards = None
    if channel.board_lookup:
        try:
            live_boards = bc.channel_boards(channel_id, key=key)
        except Exception as exc:
            return _finish(tag, summary, work, out, EXIT_BUFFER_ERROR,
                           "could not read the channel's boards: %s" % exc)
    approved = set(conf["approved_boards"])

    chosen = []
    link_cache = {}

    def checked_link(url):
        if url not in link_cache:
            link_cache[url] = link_problem(url, fetch)
        return link_cache[url]

    for rkey, row in pending:
        if len(chosen) >= n:
            break
        why = None
        if channel.board_lookup:
            why = board_problem(row, approved, live_boards)
        if why is None:
            why = checked_link(cp.row_link(row))
        if why:
            summary["skipped"].append({"key": rkey, "title": row.get("title"),
                                       "reason": why})
            logger.warning("%s: skipped %s (%s): %s", tag, rkey,
                           row.get("title"), why)
            continue
        chosen.append((rkey, row))

    summary["planned"] = [{"key": k, "date": r.get("date"),
                           "time_window": r.get("time_window"),
                           "board": r.get("board"), "title": r.get("title")}
                          for k, r in chosen]
    for p in summary["planned"]:
        out("%s: %s %s %s %-14s %s | %s" % (
            tag, "would schedule" if dry_run else "scheduling", p["key"],
            p["date"], p["time_window"], p["board"], p["title"]))
    if dry_run:
        return _finish(tag, summary, work, out, EXIT_OK, None)

    for rkey, row in chosen:
        try:
            results = cp.schedule_pass(
                [row], channel_id, state, key=key,
                profile_name=summary["profile"], platform=platform,
                identity_slug=resolved.get("identity_slug"), max_new=1,
                slot_limit=slots["limit"], allow_production=False,
                fetch_boards=(lambda *a, **k: live_boards)
                if channel.board_lookup else None)
        except pm.ProductionAccessRefused as exc:
            return _finish(tag, summary, state, out, EXIT_PRODUCTION_REFUSED,
                           "refused: %s" % exc)
        except cp.IdentityRefused as exc:
            return _finish(tag, summary, state, out, EXIT_REFUSED,
                           "refused: %s" % exc)
        res = results[0] if results else {}
        if res.get("status") != cp.SCHEDULED or res.get("key") != rkey:
            return _finish(
                tag, summary, state, out, EXIT_BUFFER_ERROR,
                "row %s came back %s (%s %s); stopped, nothing further sent"
                % (rkey, res.get("status"), res.get("stage") or "",
                   "; ".join(res.get("errors") or [res.get("note") or ""])))
        summary["scheduled"].append({"key": rkey, "post_id": res.get("post_id"),
                                     "due_at": res.get("due_at")})
    return _finish(tag, summary, state, out, EXIT_OK, None)


def _finish(tag, summary, state, out, code, note):
    """The one summary line, printed and logged, on every exit after the
    lock."""
    summary["last_buffer_due"] = _last_buffer_due(state)
    rest = _pending(state)
    summary["first_pending_due"] = ("%s %s" % (rest[0][1].get("date"),
                                               rest[0][1].get("time_window"))
                                    if rest else None)
    skipped = summary["skipped"]
    line = ("%s: free=%s reserve=%s N=%s %s=%d skipped=%d redated=%d "
            "last_buffer_due=%s first_pending_due=%s%s%s" % (
                tag, summary["free"], summary["reserve"], summary["n"],
                "would_schedule" if summary["dry_run"] else "scheduled",
                len(summary["planned"]) if summary["dry_run"]
                else len(summary["scheduled"]),
                len(skipped), summary["redated"], summary["last_buffer_due"],
                summary["first_pending_due"],
                (" skips=[%s]" % "; ".join("%s: %s" % (s["key"], s["reason"])
                                           for s in skipped)) if skipped else "",
                (" note=%s" % note) if note else ""))
    summary["note"] = note
    summary["exit"] = code
    (logger.info if code == EXIT_OK else logger.error)(line)
    out(line)
    return code, summary
