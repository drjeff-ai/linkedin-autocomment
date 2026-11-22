"""Books from the content folder into the queue, with app-side holds (F7).

The Tinderbox Pinterest operator flow (DEC-042, DEC-043, DEC-044), moved from
``.dev/scratch`` into the app. A channel opts in with a ``books`` section in
its overlay, ``scheduled_posting.platforms.<C>.books``::

    "books": {
      "content_root": "S:/new_comp/social.content/tinderbox",
      "series_order": ["unfold", "wild-littles", ...],
      "csv_board_series": ["unfold"],
      "board_map": {"<source_id>": "<board>", ...},
      "link_keep_prefix_series": ["unfold"]
    }

* A book is a folder under ``content_root`` holding a ``pins.csv``. Its series
  is the longest ``series_order`` entry its folder name starts with, plus "-".
* **Order** (DEC-042(2)): within a series, each book's cover first (books in
  name order), then round-robin across its books in file order. Across
  series, round-robin in ``series_order``. New rows are interleaved into the
  pending queue by that rule without moving any row already pending.
* **Board**: a ``csv_board_series`` pin uses its CSV ``board``; any other pin
  uses ``board_map[source_id]``, and a pin with no entry is held ``board``.
  The board must be in the overlay's ``approved_boards`` and on the channel's
  live board list (read once per run).
* **Link** (DEC-042(3), DEC-043(2)): a ``link_keep_prefix_series`` pin uses its
  CSV link. Any other pin uses the link with ``/books/<folder>`` replaced by
  ``/books/<folder without the series prefix>``; the prefixed form is never
  used. It must pass ``topup.link_problem``.
* **Identity**: a pin is its ``source_id``. A state row is matched to a pin by
  its resolved ``image_path``. A pin already in the state file (any state),
  in the hold file, or in this batch is never queued again.

Nothing here writes to Buffer. ``topup`` sends what this queues. Every command
takes the same production gate and per-channel publisher lock as ``topup``.
"""

import copy
import csv
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from . import buffer_client as bc
from . import channels
from . import csv_pipeline as cp
from . import holds as hl
from . import profile_manager as pm
from . import publisher_lock
from . import topup

logger = logging.getLogger(__name__)

EXIT_OK = topup.EXIT_OK
EXIT_BUFFER_ERROR = topup.EXIT_BUFFER_ERROR
EXIT_REFUSED = topup.EXIT_REFUSED
EXIT_PRODUCTION_REFUSED = topup.EXIT_PRODUCTION_REFUSED

PINS_CSV = "pins.csv"
EXCLUDED_FOLDER = "site_assets"


class BooksRefused(RuntimeError):
    """A refusal before anything was written: config, gate, lock, input."""


# ─── config ──────────────────────────────────────────────────────────────────

@dataclass
class BooksConfig:
    content_root: str
    series_order: tuple
    csv_board_series: frozenset
    board_map: dict
    link_keep_prefix_series: frozenset


def _names(value, where, allowed=None):
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() for v in value):
        raise BooksRefused("%s must be a list of names" % where)
    out = [v.strip() for v in value]
    if len(set(out)) != len(out):
        raise BooksRefused("%s lists a name twice" % where)
    if allowed is not None:
        unknown = [v for v in out if v not in allowed]
        if unknown:
            raise BooksRefused("%s names series not in series_order: %s"
                               % (where, ", ".join(unknown)))
    return out


def books_config(profile, platform, profile_cfg=None) -> BooksConfig:
    """The channel's ``books`` section, validated. Refuses a channel without
    one: there is no default content folder, board map or series list."""
    cfg = profile_cfg if profile_cfg is not None else pm.get_profile_config(profile)
    section = (cfg or {}).get("scheduled_posting")
    overlays = section.get("platforms") if isinstance(section, dict) else None
    overlay = overlays.get(platform) if isinstance(overlays, dict) else None
    where = "scheduled_posting.platforms.%s.books" % platform
    raw = overlay.get("books") if isinstance(overlay, dict) else None
    if not isinstance(raw, dict):
        raise BooksRefused(
            "profile %r has no %s section, so it has no content folder, "
            "series or board map. Books are not set up for this channel. "
            "Refusing." % (profile, where))
    root = raw.get("content_root")
    if not isinstance(root, str) or not root.strip():
        raise BooksRefused("%s.content_root must be a folder path" % where)
    series = _names(raw.get("series_order"), where + ".series_order")
    if not series:
        raise BooksRefused("%s.series_order must name at least one series"
                           % where)
    csv_board = _names(raw.get("csv_board_series", []),
                       where + ".csv_board_series", series)
    keep = _names(raw.get("link_keep_prefix_series", []),
                  where + ".link_keep_prefix_series", series)
    bmap = raw.get("board_map", {})
    if not isinstance(bmap, dict) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
            for k, v in bmap.items()):
        raise BooksRefused("%s.board_map must map source ids to board names"
                           % where)
    unknown = sorted(set(raw) - {"content_root", "series_order",
                                 "csv_board_series", "board_map",
                                 "link_keep_prefix_series"})
    if unknown:
        raise BooksRefused("%s has unknown keys: %s" % (where, ", ".join(unknown)))
    return BooksConfig(content_root=root.strip().replace("\\", "/").rstrip("/"),
                       series_order=tuple(series),
                       csv_board_series=frozenset(csv_board),
                       board_map=dict(bmap),
                       link_keep_prefix_series=frozenset(keep))


# ─── reading the content folder ─────────────────────────────────────────────

def norm_path(path):
    """A path for comparison: absolute, one separator, case folded on
    Windows. Stored image paths are compared through this, never raw."""
    return os.path.normcase(os.path.abspath(str(path or ""))).replace("\\", "/")


def image_path(root, book, filename):
    """The image path the way the scratch mapping stored it."""
    return os.path.abspath(os.path.join(root, book, filename)).replace(os.sep, "/")


def excluded_asset(filename):
    return (EXCLUDED_FOLDER + "/") in str(filename or "").replace("\\", "/")


def series_of(book, series_order):
    """The series a book folder belongs to, or None."""
    for s in sorted(series_order, key=len, reverse=True):
        if book.startswith(s + "-"):
            return s
    return None


def discover_books(root):
    """Every folder directly under ``root`` holding a pins.csv, except
    site_assets, in name order."""
    if not os.path.isdir(root):
        raise BooksRefused("content_root %s is not a folder" % root)
    out = []
    for name in sorted(os.listdir(root)):
        if name == EXCLUDED_FOLDER:
            continue
        if os.path.isfile(os.path.join(root, name, PINS_CSV)):
            out.append(name)
    return out


def read_pins(root, book):
    """The book's pins.csv rows, site_assets rows left out, in file order."""
    path = os.path.join(root, book, PINS_CSV)
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if not excluded_asset(r.get("filename"))]


@dataclass
class Pin:
    source_id: str
    book: str
    series: object          # str, or None for a book of no known series
    source: dict
    image: str              # image_path() form


class PinIndex:
    """Every pin under the content root, by source id and by image."""

    def __init__(self, conf: BooksConfig):
        self.conf = conf
        self.books = OrderedDict()
        self.by_sid = {}
        self.by_image = {}
        problems = []
        for book in discover_books(conf.content_root):
            series = series_of(book, conf.series_order)
            pins = []
            for r in read_pins(conf.content_root, book):
                sid = (r.get("source_id") or "").strip()
                if not sid:
                    problems.append("%s: a row has no source_id" % book)
                    continue
                if sid in self.by_sid:
                    problems.append("source_id %s is in %s and %s"
                                    % (sid, self.by_sid[sid].book, book))
                    continue
                pin = Pin(sid, book, series, dict(r),
                          image_path(conf.content_root, book, r.get("filename") or ""))
                pins.append(pin)
                self.by_sid[sid] = pin
                self.by_image[norm_path(pin.image)] = pin
            self.books[book] = pins
        if problems:
            # Identity is the source id. Two pins sharing one, or a pin with
            # none, would make dedupe guess. Refuse instead.
            raise BooksRefused("the content folder has pins with no usable "
                               "identity: %s" % "; ".join(problems))

    def pin_of_row(self, row):
        return self.by_image.get(norm_path(cp.row_image_path(row or {})))

    def series_sequence(self, series):
        """DEC-042(2) within one series: covers first (one per book, books in
        name order), then round-robin across the books in file order."""
        books = sorted(b for b, pins in self.books.items()
                       if self.series_of_book(b) == series)
        seq, rest = [], {}
        for book in books:
            pins = self.books[book]
            covers = [p for p in pins if p.source.get("variant") == "cover"]
            seq += covers[:1]
            rest[book] = [p for p in pins if p not in covers[:1]]
        while any(rest.values()):
            for book in books:
                if rest[book]:
                    seq.append(rest[book].pop(0))
        return seq

    def series_of_book(self, book):
        return series_of(book, self.conf.series_order)

    def rank(self):
        """source_id -> position in its series' sequence."""
        out = {}
        for s in self.conf.series_order:
            for i, p in enumerate(self.series_sequence(s)):
                out[p.source_id] = i
        return out


# ─── mapping and checks ─────────────────────────────────────────────────────

def link_candidates(book, series, csv_link, conf: BooksConfig):
    """The links a pin may use, best first. Never the prefixed form for a
    series whose site drops the prefix (DEC-043(2))."""
    csv_link = (csv_link or "").strip()
    if series in conf.link_keep_prefix_series:
        return [csv_link] if csv_link else []
    prefix = series + "-" if series else ""
    if prefix and book.startswith(prefix):
        prefixed = "/books/%s" % book
        if prefixed in csv_link:
            short = csv_link.replace(prefixed, "/books/%s" % book[len(prefix):], 1)
            return [short]
    return [csv_link] if csv_link else []


def board_for(pin, conf: BooksConfig):
    if pin.series in conf.csv_board_series:
        return (pin.source.get("board") or "").strip()
    return conf.board_map.get(pin.source_id, "")


def mapped_row(pin, conf: BooksConfig, link=None):
    """The app row a pin maps to, without a date (the scratch column map)."""
    r = pin.source
    cands = link_candidates(pin.book, pin.series, r.get("link"), conf)
    return {
        "post_text": r.get("description") or "",
        "title": r.get("title") or "",
        "board": board_for(pin, conf),
        "alt_text": r.get("alt_text") or "",
        "link": link or (cands[0] if cands else (r.get("link") or "")),
        "image_path": pin.image,
    }


class Checker:
    """The board, link and row checks for one run. Boards are read once, each
    URL is fetched once."""

    def __init__(self, conf, approved, live_boards, platform, fetch=None,
                 now=None, tz=None, slot=None):
        self.conf = conf
        self.approved = set(approved)
        self.live_boards = live_boards
        self.platform = platform
        self.fetch = fetch
        self.now = now or datetime.now(timezone.utc)
        self.tz = tz or bc.posting_timezone()
        self.slot = slot
        self._links = {}

    def link(self, url):
        if url not in self._links:
            self._links[url] = topup.link_problem(url, self.fetch)
        return self._links[url]

    def check(self, pin):
        """``(row, None, None)`` when the pin may be queued, else
        ``(row, kind, reason)``."""
        conf = self.conf
        if pin.series is None:
            return mapped_row(pin, conf), hl.KIND_REVIEW, \
                "book %s is in no series of series_order" % pin.book
        board = board_for(pin, conf)
        if not board:
            return mapped_row(pin, conf), hl.KIND_BOARD, \
                "no board map entry for %s" % pin.source_id
        row = mapped_row(pin, conf)
        why = topup.board_problem(row, self.approved, self.live_boards)
        if why:
            return row, hl.KIND_BOARD, why
        cands = link_candidates(pin.book, pin.series, pin.source.get("link"), conf)
        tried = []
        link = None
        for url in cands:
            problem = self.link(url)
            if problem is None:
                link = url
                break
            tried.append("%s: %s" % (url, problem))
        if link is None:
            return row, hl.KIND_LINK, ("; ".join(tried) or "no link")
        row = mapped_row(pin, conf, link=link)
        problems = cp.validate_row(dict(row, date=self.slot[0],
                                        time_window=self.slot[1]),
                                   now=self.now, tz=self.tz,
                                   platform=self.platform)
        if problems:
            return row, hl.KIND_REVIEW, "row check: " + " | ".join(problems)
        return row, None, None


# ─── order ──────────────────────────────────────────────────────────────────

def round_robin(items, series_order):
    """DEC-042(2): ``items`` are ``(id, series)``; series take turns in
    ``series_order``, each keeping its own order. A series not in the order
    (None included) goes after them."""
    queues = OrderedDict((s, [x for x in items if x[1] == s]) for s in series_order)
    others = sorted({x[1] for x in items} - set(series_order), key=str)
    for s in others:
        queues[s] = [x for x in items if x[1] == s]
    out = []
    while any(queues.values()):
        for q in queues.values():
            if q:
                out.append(q.pop(0))
    return out


def merge(existing, new, series_order):
    """Interleave ``new`` into ``existing`` (both ``[(id, series)]``) by the
    round-robin rule, without moving any existing row (ops7.plan).

    Returns ``[("E", id) | ("N", id)]``. The existing ids come out in exactly
    their input order; that is asserted."""
    tagged = [(("E", i), s) for i, s in existing] + [(("N", i), s) for i, s in new]
    ideal = round_robin(tagged, series_order)
    merged, emitted, ei = [], set(), 0
    for tag, _s in ideal:
        if tag[0] == "N":
            merged.append(tag)
            continue
        if tag in emitted:
            continue
        while ei < len(existing):
            e = ("E", existing[ei][0])
            ei += 1
            merged.append(e)
            emitted.add(e)
            if e == tag:
                break
    merged += [("E", i) for i, _s in existing[ei:]]
    assert [i for t, i in merged if t == "E"] == [i for i, _s in existing]
    assert len(merged) == len(existing) + len(new)
    return merged


# ─── the session: gate, lock, state, holds ──────────────────────────────────

class _Session:
    def __init__(self, profile, platform, state, store, resolved, conf,
                 tconf, channel, dry_run):
        self.profile = profile
        self.platform = platform
        self.state = state
        self.store = store
        self.resolved = resolved
        self.conf = conf
        self.tconf = tconf
        self.channel = channel
        self.dry_run = dry_run


def _open(profile, platform, action, dry_run):
    """Resolve, gate and load. Returns ``(session, None)`` or
    ``(None, (code, message))``. The lock is taken by the caller."""
    platform = (platform or "").strip().lower()
    try:
        resolved = pm.resolve_scheduled(profile, platform)
    except pm.ScheduledConfigError as exc:
        return None, (EXIT_REFUSED, str(exc))
    if not resolved.get("channel_id"):
        return None, (EXIT_REFUSED, "no buffer_channel_id for %s/%s"
                      % (profile, platform))
    profile_cfg = pm.get_profile_config(profile)
    try:
        conf = books_config(profile, platform, profile_cfg)
        tconf = topup.topup_config(profile, platform, profile_cfg)
    except (BooksRefused, topup.TopupRefused) as exc:
        return None, (EXIT_REFUSED, str(exc))
    try:
        pm.check_declared_production(resolved.get("identity_slug"),
                                     "%s on %s" % (action, platform),
                                     allow_production=False)
    except pm.ProductionAccessRefused as exc:
        return None, (EXIT_PRODUCTION_REFUSED, str(exc))
    state = cp.PipelineState(profile_name=profile, platform=platform)
    store = hl.HoldStore(hl.holds_path(state.path))
    return _Session(profile, platform, state, store, resolved, conf, tconf,
                    channels.resolve(platform), dry_run), None


def _run(profile, platform, action, dry_run, body, out):
    """Gate, lock, reload, then ``body(session, summary)``. Returns
    ``(exit_code, summary)``."""
    tag = "%s %s/%s%s" % (action, profile, (platform or "").lower(),
                          " [dry-run]" if dry_run else "")
    summary = {"action": action, "profile": profile,
               "platform": (platform or "").lower(), "dry_run": dry_run,
               "lines": []}

    def say(line):
        summary["lines"].append(line)
        out(line)

    def refuse(code, msg):
        line = "%s: REFUSED: %s" % (tag, msg)
        logger.warning(line)
        say(line)
        summary["refused"] = msg
        summary["exit"] = code
        return code, summary

    session, err = _open(profile, platform, action, dry_run)
    if err:
        return refuse(*err)
    try:
        with publisher_lock.channel_lock(session.state.path,
                                         "%s %s/%s" % (action, profile,
                                                       session.platform)):
            session.state.reload()
            session.store.reload()
            if dry_run:
                # Copies whose save() is a no-op: the whole run is shown,
                # nothing is written.
                session.state = copy.deepcopy(session.state)
                session.state.save = lambda: None
                session.store = copy.deepcopy(session.store)
                session.store.save = lambda: None
            try:
                code = body(session, summary, lambda s: say("%s: %s" % (tag, s)))
            except (BooksRefused, hl.HoldError) as exc:
                return refuse(EXIT_REFUSED, str(exc))
    except publisher_lock.PublisherBusy as exc:
        return refuse(EXIT_REFUSED, str(exc))
    summary["exit"] = code
    return code, summary


def _present(session, index):
    """source_id -> where it already is: ``state:<status>`` or
    ``held:<kind>``."""
    where = {}
    for rec in session.state.rows.values():
        pin = index.pin_of_row(rec.get("row"))
        if pin is not None:
            where[pin.source_id] = "state:%s" % (rec.get("status") or cp.PENDING)
    for sid, h in session.store.holds.items():
        where.setdefault(sid, "held:%s" % h.get("kind"))
    return where


def _live_boards(session):
    if not session.channel.board_lookup:
        return None
    try:
        return bc.channel_boards(session.resolved["channel_id"],
                                 key=session.resolved.get("api_key"))
    except Exception as exc:
        raise _BufferRead("could not read the channel's boards: %s" % exc)


class _BufferRead(RuntimeError):
    pass


def _checker(session, live_boards, fetch, now):
    tz = bc.posting_timezone()
    start = topup._redate_start(session.state, now)
    windows = sorted(bc.TIME_WINDOWS, key=cp.window_rank)
    day, window = cp._start_slot(start, now, tz, windows)
    return Checker(session.conf, session.tconf["approved_boards"], live_boards,
                   session.platform, fetch=fetch, now=now, tz=tz,
                   slot=(day.isoformat(), window)), start


def _queue(session, index, passing, checker, start, now, live_boards, say):
    """Queue ``passing`` (``[(pin, row)]``) PENDING, interleaved by the
    round-robin rule, then re-date the pending queue from ``start`` with the
    overlay cadence. Returns ``[(pin, key)]`` queued, and the hold list of any
    the queue itself rejected."""
    if not passing:
        return [], []
    state = session.state
    before = topup._pending(state)
    before_keys = [k for k, _r in before]
    existing = []
    for k, row in before:
        pin = index.pin_of_row(row)
        existing.append((k, pin.series if pin else None))
    rank = index.rank()
    passing = sorted(passing, key=lambda pr: (rank.get(pr[0].source_id, 0),
                                              pr[0].source_id))
    rows = [dict(row, date=checker.slot[0], time_window=checker.slot[1])
            for _pin, row in passing]
    accepted, rejected, skipped, _warn = cp.queue_rows(
        rows, state, now=now, boards=live_boards)
    by_index = {a["index"]: a["key"] for a in accepted}
    queued, refused = [], []
    for i, (pin, row) in enumerate(passing, start=1):
        if i in by_index:
            queued.append((pin, by_index[i]))
    for x in rejected:
        refused.append((passing[x["index"] - 1], "queue refused: %s"
                        % " | ".join(x["errors"])))
    for x in skipped:
        refused.append((passing[x["index"] - 1], "queue skipped: %s" % x["reason"]))
    if not queued:
        return [], refused
    key_of = {pin.source_id: key for pin, key in queued}
    merged = merge(existing, [(pin.source_id, pin.series) for pin, _k in queued],
                   session.conf.series_order)
    order = [i if t == "E" else key_of[i] for t, i in merged]
    cadence = session.tconf["cadence"]
    cp.redate_pending(session.profile, session.platform, cadence.per_day,
                      start=start, now=now, state=state,
                      windows_for=cadence.windows_for, order=order)
    after = [k for k, _r in topup._pending(state)]
    # DEC-033 and the no-move rule, checked on the result, not assumed: every
    # row pending before is still pending under the same key, in the same
    # relative order, and the queue is exactly the merged order.
    assert after == order, "pending order is not the merged order"
    assert [k for k in after if k in set(before_keys)] == before_keys, \
        "a pending row moved relative to another"
    say("queued %d; pending now %d, re-dated from %s %s to %s %s" % (
        len(queued), len(after),
        state.rows[after[0]]["row"]["date"], state.rows[after[0]]["row"]["time_window"],
        state.rows[after[-1]]["row"]["date"], state.rows[after[-1]]["row"]["time_window"]))
    return queued, refused


def _hold(session, pin, row, kind, reason, now):
    session.store.put(pin.source_id, book=pin.book, series=pin.series,
                      kind=kind, reason=reason, source=pin.source, row=row,
                      now=now)


def _decide(session, index, pins, checker, start, now, live_boards, say,
            summary):
    """Check ``pins``; queue the ones that pass, hold the rest. Holds are
    saved after the state, so a crash between leaves a pin queued and still
    listed as held, which dedupe reads as present: never queued twice."""
    passing = []
    for pin in pins:
        row, kind, reason = checker.check(pin)
        if kind:
            _hold(session, pin, row, kind, reason, now)
            summary["held"].append({"source_id": pin.source_id, "book": pin.book,
                                    "title": row.get("title"), "kind": kind,
                                    "reason": reason})
            say("%s %s %s: %s | %s" % ("would hold" if session.dry_run else "held",
                                       kind, pin.source_id, reason, row.get("title")))
        else:
            passing.append((pin, row))
    queued, refused = _queue(session, index, passing, checker, start, now,
                             live_boards, say)
    for (pin, row), reason in refused:
        _hold(session, pin, row, hl.KIND_REVIEW, reason, now)
        summary["held"].append({"source_id": pin.source_id, "book": pin.book,
                                "title": row.get("title"), "kind": hl.KIND_REVIEW,
                                "reason": reason})
        say("held review %s: %s" % (pin.source_id, reason))
    for pin, key in queued:
        session.store.remove(pin.source_id)
        rec = session.state.rows[key]["row"]
        summary["queued"].append({"source_id": pin.source_id, "key": key,
                                  "book": pin.book, "title": rec.get("title"),
                                  "date": rec.get("date"),
                                  "time_window": rec.get("time_window")})
        say("%s %s %s %s %s | %s" % ("would queue" if session.dry_run else "queued",
                                     pin.source_id, key, rec.get("date"),
                                     rec.get("time_window"), rec.get("title")))
    session.store.save()


# ─── commands ───────────────────────────────────────────────────────────────

def list_books(profile, platform, out=print):
    """``books``: every book folder with pins found, in state, held, new."""
    def body(session, summary, say):
        index = PinIndex(session.conf)
        where = _present(session, index)
        summary["books"] = []
        for book, pins in index.books.items():
            ids = [p.source_id for p in pins]
            in_state = sum(1 for i in ids if where.get(i, "").startswith("state:"))
            held = sum(1 for i in ids if where.get(i, "").startswith("held:"))
            new = [i for i in ids if i not in where]
            entry = {"book": book, "series": index.series_of_book(book),
                     "pins": len(ids), "in_state": in_state, "held": held,
                     "new": len(new), "new_ids": new}
            summary["books"].append(entry)
            say("%-42s series=%s pins=%d in_state=%d held=%d new=%d" % (
                book, entry["series"] or "NONE", len(ids), in_state, held,
                len(new)))
        say("books=%d pins=%d new=%d" % (
            len(summary["books"]), sum(b["pins"] for b in summary["books"]),
            sum(b["new"] for b in summary["books"])))
        return EXIT_OK
    return _run(profile, platform, "books", True, body, out)


def _with_checks(action, profile, platform, dry_run, pick, out, fetch=None,
                 now=None):
    """Shared by add-book, recheck-holds and release-hold: ``pick(session,
    index, summary, say)`` returns the pins to check, or raises BooksRefused."""
    now = now or datetime.now(timezone.utc)

    def body(session, summary, say):
        summary.update(queued=[], held=[])
        index = PinIndex(session.conf)
        pins = pick(session, index, summary, say)
        if not pins:
            say("nothing to check")
            return EXIT_OK
        try:
            live = _live_boards(session)
        except _BufferRead as exc:
            say(str(exc))
            return EXIT_BUFFER_ERROR
        checker, start = _checker(session, live, fetch, now)
        _decide(session, index, pins, checker, start, now, live, say, summary)
        say("queued=%d held=%d" % (len(summary["queued"]), len(summary["held"])))
        return EXIT_OK
    return _run(profile, platform, action, dry_run, body, out)


def add_book(profile, platform, book, dry_run=False, out=print, fetch=None,
             now=None):
    """``add-book``: map the book's new pins, queue the ones that pass, hold
    the rest."""
    def pick(session, index, summary, say):
        if book not in index.books:
            raise BooksRefused("no book folder %r with a %s under %s"
                               % (book, PINS_CSV, session.conf.content_root))
        if index.series_of_book(book) is None:
            raise BooksRefused("book %r is in no series of series_order (%s). "
                               "Add its series to the overlay first."
                               % (book, ", ".join(session.conf.series_order)))
        where = _present(session, index)
        seq = [p for p in index.series_sequence(index.series_of_book(book))
               if p.book == book]
        summary["already"] = {p.source_id: where[p.source_id]
                              for p in seq if p.source_id in where}
        new = [p for p in seq if p.source_id not in where]
        say("book %s: pins=%d already present=%d new=%d" % (
            book, len(seq), len(summary["already"]), len(new)))
        return new
    return _with_checks("add-book", profile, platform, dry_run, pick, out,
                        fetch, now)


def recheck_holds(profile, platform, kinds=None, dry_run=False, out=print,
                  fetch=None, now=None):
    """``recheck-holds``: re-run the checks on board and link holds."""
    kinds = tuple(kinds) if kinds else hl.RECHECKABLE
    bad = [k for k in kinds if k not in hl.RECHECKABLE]
    if bad:
        raise BooksRefused("recheck-holds re-checks only %s; %s holds are "
                           "released by release-hold" % (
                               " and ".join(hl.RECHECKABLE), ", ".join(bad)))

    def pick(session, index, summary, say):
        pins = []
        for h in session.store.of_kind(kinds):
            pin = index.by_sid.get(h["source_id"])
            if pin is None:
                say("kept %s: its pin is no longer in the content folder"
                    % h["source_id"])
                continue
            pins.append(pin)
        say("holds to recheck (%s): %d" % (", ".join(kinds), len(pins)))
        return pins
    return _with_checks("recheck-holds", profile, platform, dry_run, pick, out,
                        fetch, now)


def release_hold(profile, platform, keys, dry_run=False, out=print, fetch=None,
                 now=None):
    """``release-hold --key``: lift the hold on these pins and queue each one
    that passes the board and link checks. One that fails stays held, under
    the check's kind and reason."""
    def pick(session, index, summary, say):
        pins = []
        for k in keys:
            h = session.store.get(k)
            if h is None:
                raise BooksRefused("%s is not held" % k)
            pin = index.by_sid.get(k)
            if pin is None:
                raise BooksRefused("%s is held but its pin is no longer in the "
                                   "content folder" % k)
            pins.append(pin)
        return pins
    return _with_checks("release-hold", profile, platform, dry_run, pick, out,
                        fetch, now)


def hold_pins(profile, platform, keys, kind, reason, dry_run=False, out=print,
              now=None):
    """``hold --key ... --kind K --reason R``: hold pins that are not queued,
    without checking them (an operator decision, or a migration of a known
    reason). A pin already in the state file is refused: holding it would not
    unqueue it."""
    now = now or datetime.now(timezone.utc)
    if kind not in hl.KINDS:
        raise BooksRefused("unknown hold kind %r - expected one of %s"
                           % (kind, ", ".join(hl.KINDS)))

    def body(session, summary, say):
        summary["held"] = []
        index = PinIndex(session.conf)
        where = _present(session, index)
        for k in keys:
            pin = index.by_sid.get(k)
            if pin is None:
                raise BooksRefused("no pin %s under %s" % (k, session.conf.content_root))
            if where.get(k, "").startswith("state:"):
                raise BooksRefused("%s is already in the queue (%s); holding it "
                                   "would not unqueue it" % (k, where[k]))
        for k in keys:
            pin = index.by_sid[k]
            row = mapped_row(pin, session.conf)
            _hold(session, pin, row, kind, reason, now)
            summary["held"].append({"source_id": k, "book": pin.book,
                                    "kind": kind, "reason": reason})
            say("%s %s %s: %s | %s" % ("would hold" if session.dry_run else "held",
                                       kind, k, reason, row.get("title")))
        session.store.save()
        return EXIT_OK
    return _run(profile, platform, "hold", dry_run, body, out)


def list_holds(profile, platform, out=print):
    """Every hold, for the dashboard and the CLI ``holds`` listing."""
    def body(session, summary, say):
        summary["holds"] = session.store.of_kind()
        for h in summary["holds"]:
            say("%s %s %s: %s | %s" % (h["kind"], h["source_id"], h["book"],
                                       h["reason"], (h.get("row") or {}).get("title")))
        say("holds=%d" % len(summary["holds"]))
        return EXIT_OK
    return _run(profile, platform, "holds", True, body, out)
