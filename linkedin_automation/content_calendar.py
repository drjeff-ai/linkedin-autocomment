"""Read-only content calendar (U1): every post of a profile, on its day.

The calendar is built from a list of *sources*. A source is a callable
``profile -> iterable of item dicts``; :func:`collect` filters the items to a
date range and orders them. Today there is one source, the scheduled-posting
state files (:func:`state_file_items`). A later channel (YouTube, V1) adds a
source to the list; the route does not change.

Nothing here writes, and nothing here touches the network: it reads local
state files only. In particular it never runs the Buffer reconcile the queue
route runs, so the calendar shows last-known state.
"""

import os
from datetime import date, datetime, timezone

from . import buffer_client as bc
from . import channels
from . import csv_pipeline
from . import profile_manager as pm

#: How a pipeline state reads on the calendar. Every state the pipeline has
#: maps to exactly one group; an unknown state shows as itself rather than
#: being folded into a group it may not belong to.
STATE_GROUPS = {
    csv_pipeline.PENDING: "queued",
    csv_pipeline.HELD: "held",
    csv_pipeline.SCHEDULED: "in_buffer",
    csv_pipeline.PUBLISHED: "sent",
    csv_pipeline.COMMENTED: "sent",
    csv_pipeline.COMMENT_FAILED: "sent",
    csv_pipeline.FAILED: "failed",
}

GROUP_LABELS = {
    "queued": "Queued",
    "held": "Held",
    "in_buffer": "In Buffer",
    "sent": "Sent",
    "failed": "Failed",
    # V1: uploaded private, made public by YouTube at its publishAt.
    "scheduled_youtube": "Scheduled on YouTube",
}

#: The longest range one request may ask for. A month view with its leading
#: and trailing days is 42 days; this leaves room without letting a typo'd
#: year read every row into one response.
MAX_RANGE_DAYS = 400


def zone_name(tz):
    key = getattr(tz, "key", None) or str(tz)
    return "PT" if key == bc.DEFAULT_TIMEZONE else key


def _hour_label(hour, minute):
    h = hour % 12 or 12
    return "%d:%02d" % (h, minute) if minute else str(h)


def window_hours(window, tz=None):
    """``'morning'`` -> ``'8-11 PT'``. None for a window that is not declared."""
    spec = bc.TIME_WINDOWS.get(str(window or "").strip().lower())
    if spec is None:
        return None
    tz = tz or bc.posting_timezone()
    sh, sm, eh, em = spec
    return "%s-%s %s" % (_hour_label(sh, sm), _hour_label(eh, em),
                         zone_name(tz))


def planned_label(date_str, window, tz=None):
    """``('2026-11-24', 'morning')`` -> ``'Tue 11-24 morning, 8-11 PT'``.

    None when the date is missing or not ``YYYY-MM-DD``: a label that guessed
    a day would put the post on the wrong square. A window that is not
    declared is named as given, with no hours.
    """
    try:
        day = datetime.strptime(str(date_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    name = str(window or "").strip()
    head = "%s %s" % (day.strftime("%a"), day.strftime("%m-%d"))
    if not name:
        return head
    hours = window_hours(name, tz)
    shown = name.lower().replace("_", " ")
    return "%s %s, %s" % (head, shown, hours) if hours else "%s %s" % (head, name)


def _local(iso, tz):
    """A Buffer ``due_at`` (UTC ISO 8601) as a datetime in ``tz``, or None."""
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(tz)


def state_path(profile, platform):
    """Where ``PipelineState(profile_name=profile, platform=platform)`` reads.

    Computed rather than taken from ``PipelineState``, because building one
    without a path goes through ``pm.get_data_dir``, which creates the
    directory. The calendar writes nothing, not even an empty folder.
    """
    base = os.path.join(pm.DATA_ROOT, profile)
    if channels.resolve(platform).state_path == channels.LEGACY:
        return os.path.join(base, csv_pipeline.STATE_NAME)
    return os.path.join(base, platform, csv_pipeline.STATE_NAME)


def item_from_row(platform, key, rec, tz, row_view=None):
    """One calendar item for one state row."""
    status = rec.get("status") or csv_pipeline.PENDING
    row = rec.get("row") or {}
    when = _local(rec.get("due_at"), tz) if rec.get("due_at") else None
    window = (row.get("time_window") or "").strip() or None
    if when is not None:
        day = when.date().isoformat()
        minute = when.hour * 60 + when.minute
        time_label = "%s %s" % (when.strftime("%H:%M"), zone_name(tz))
    else:
        day = (row.get("date") or "").strip() or None
        spec = bc.TIME_WINDOWS.get((window or "").lower())
        minute = spec[0] * 60 + spec[1] if spec else None
        hours = window_hours(window, tz) if window else None
        time_label = ("%s, %s" % (window.replace("_", " "), hours) if hours
                      else window)
    text = (rec.get("text") or row.get("post_text") or "").strip()
    item = {
        "platform": platform,
        "platform_label": channels.resolve(platform).label,
        "key": key,
        "state": status,
        "group": STATE_GROUPS.get(status, status),
        "title": (row.get("title") or "").strip() or None,
        "preview": text[:160],
        "image_url": rec.get("image_url") or None,
        "day": day,
        "due_at": rec.get("due_at") or None,
        "time_window": window,
        "time_label": time_label,
        "sort_minute": minute,
        "hold_reason": rec.get("hold_reason"),
        "errors": rec.get("errors") or [],
    }
    if row_view is not None:
        item["detail"] = row_view(key, rec)
    return item


def state_file_items(profile, row_view=None, tz=None):
    """Every row of every scheduled-posting state file the profile has.

    "Has" means the file exists: a platform the profile never queued on has
    no file and contributes nothing. Read with ``PipelineState(path=...)``,
    so an unreadable file raises exactly as it does for the queue.
    """
    tz = tz or bc.posting_timezone()
    for platform in channels.REGISTRY:
        path = state_path(profile, platform)
        if not os.path.exists(path):
            continue
        state = csv_pipeline.PipelineState(path=path, platform=platform)
        for key, rec in state.rows.items():
            yield item_from_row(platform, key, rec, tz, row_view=row_view)


def parse_day(value, name):
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("%s must be YYYY-MM-DD, got %r" % (name, value))


def _order(item):
    minute = item.get("sort_minute")
    return (item.get("day") or "", 1 if minute is None else 0, minute or 0,
            item.get("platform") or "", item.get("key") or "")


def collect(profile, date_from, date_to, sources, platform=None):
    """Items from every source whose day is in ``[date_from, date_to]``.

    Returns ``{"items", "undated", "counts"}``: ``items`` ordered by day, then
    time (a row with no time last in its day), then platform and key;
    ``undated`` the items with no day at all, which cannot sit on a square;
    ``counts`` per day, ``{day: {group: n}}``. ``platform`` keeps one
    platform's items only.
    """
    if not isinstance(date_from, date) or not isinstance(date_to, date):
        raise TypeError("date_from and date_to must be dates")
    if date_to < date_from:
        raise ValueError("to (%s) is before from (%s)" % (date_to, date_from))
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise ValueError("range is over %d days" % MAX_RANGE_DAYS)
    lo, hi = date_from.isoformat(), date_to.isoformat()
    items, undated = [], []
    for source in sources:
        for item in source(profile):
            if platform and item.get("platform") != platform:
                continue
            day = item.get("day")
            if not day:
                undated.append(item)
            elif lo <= day <= hi:
                items.append(item)
    items.sort(key=_order)
    undated.sort(key=_order)
    counts = {}
    for item in items:
        per = counts.setdefault(item["day"], {})
        per[item["group"]] = per.get(item["group"], 0) + 1
    return {"items": items, "undated": undated, "counts": counts}


def item_from_hold(platform, hold, row_view=None):
    """One undated calendar item for one app hold (F7). A held pin has no
    slot, so it sits in ``undated``, beside the grid."""
    row = hold.get("row") or {}
    reason = "%s: %s" % (hold.get("kind"), hold.get("reason"))
    text = (row.get("post_text") or "").strip()
    item = {
        "platform": platform,
        "platform_label": channels.resolve(platform).label,
        "key": hold.get("source_id"),
        "state": "app_hold",
        "group": "held",
        "title": (row.get("title") or "").strip() or None,
        "preview": text[:160],
        "image_url": None,
        "day": None,
        "due_at": None,
        "time_window": None,
        "time_label": None,
        "sort_minute": None,
        "hold_reason": reason,
        "errors": [],
    }
    if row_view is not None:
        detail = row_view(hold.get("source_id"), {
            "status": csv_pipeline.HELD, "row": row, "text": text,
            "hold_reason": reason})
        detail.update(label="Held (not queued)",
                      meaning="not in the queue: %s" % reason,
                      action=("re-checked by Recheck holds" if hold.get("kind")
                              in ("board", "link") else "released only by "
                              "Release"),
                      date=None, time_window=None, planned=None)
        item["detail"] = detail
    return item


def hold_items(profile, row_view=None):
    """Every app hold of every channel the profile has a hold file for."""
    from . import holds as hl
    for platform in channels.REGISTRY:
        path = hl.holds_path(state_path(profile, platform))
        if not os.path.exists(path):
            continue
        for hold in hl.HoldStore(path).of_kind():
            yield item_from_hold(platform, hold, row_view=row_view)
