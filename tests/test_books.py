"""F7: books from the content folder into the queue, with app-side holds.

Fixtures: ``tests/fixtures/books`` holds two real books' pins.csv structure
(ids, filenames, variants, boards, links) with synthetic text, and what the
scratch mapper (``tinderbox_map.map_all`` / ``ops7.map_new``) produced for
them. Two more books are synthetic: an Unfold-style one (CSV board, prefixed
link kept) and a Pip-and-Spark-style one whose link is dead.

Buffer and HTTP are mocked everywhere: a fixture fails any socket use.
"""

import csv
import json
import os
import shutil
import socket
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import books  # noqa: E402
from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import holds as hl  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402
from linkedin_automation import publisher_lock  # noqa: E402
from linkedin_automation import topup  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "books")
EXPECTED = json.load(open(os.path.join(FIXTURES, "expected_scratch.json"),
                          encoding="utf-8"))
WL = "wild-littles-two-houses-now"
LU = "lumora-the-loudest-color"
UN = "unfold-test"
PS = "pip-and-spark-test"
SITE = "https://tinderboxpublishing.com/books/"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
NOW = datetime(2026, 11, 22, 22, 0, tzinfo=timezone.utc)   # 14:00 PT
SERIES = ["unfold", "wild-littles", "lumora", "thimble-hollow", "pip-and-spark"]
PROFILE, PLATFORM = "tb", "pinterest"
FIELDS = ("source_id", "filename", "title", "description", "alt_text", "board",
          "link", "pin_type", "variant", "publish_at")


def _write_book(root, book, rows):
    os.makedirs(os.path.join(root, book), exist_ok=True)
    with open(os.path.join(root, book, "pins.csv"), "w", encoding="utf-8",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _synthetic(book, n, board, link, prefix):
    rows = []
    for i in range(n):
        rows.append({"source_id": "%s-%02d" % (prefix, i),
                     "filename": "%s-%02d.png" % (book, i),
                     "title": "Synthetic %s %d" % (book, i),
                     "description": "Synthetic text %d." % i,
                     "alt_text": "Alt %d" % i, "board": board, "link": link,
                     "pin_type": "standard",
                     "variant": "cover" if i == 0 else "page"})
    return rows


def _board_map():
    out = {}
    for book in (WL, LU):
        out.update(EXPECTED[book]["board"])
    out.update({"ps-%02d" % i: "STEM Picture Books for Kids" for i in range(3)})
    return out


APPROVED = sorted({"Art Nouveau Coloring Pages", "STEM Picture Books for Kids",
                   *_board_map().values()})


def _overlay(**books_over):
    books_cfg = {"content_root": None, "series_order": SERIES,
                 "csv_board_series": ["unfold"],
                 "board_map": _board_map(),
                 "link_keep_prefix_series": ["unfold"]}
    books_cfg.update(books_over)
    return {"buffer_channel_id": "chan1", "identity_slug": "tbpub",
            "buffer_api_key_env": "BUFFER_API_KEY_TB", "drain": False,
            "approved_boards": APPROVED,
            "cadence": {"per_day": 5, "quiet_days": [],
                        "windows": {"5": ["morning", "afternoon",
                                          "late_afternoon", "evening",
                                          "night"]}},
            "books": books_cfg}


class NetworkUsed(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise NetworkUsed("network access attempted: %r" % (a,))
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    import requests
    monkeypatch.setattr(requests.Session, "send", refuse)


class Env:
    """The fake world: content root, data root, config, Buffer boards, links."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = str(tmp_path / "content")
        self.data = str(tmp_path / "data")
        for book in (WL, LU):
            shutil.copytree(os.path.join(FIXTURES, book),
                            os.path.join(self.root, book))
        _write_book(self.root, UN, _synthetic(
            UN, 3, "Art Nouveau Coloring Pages", SITE + UN + "#design-01", "un"))
        _write_book(self.root, PS, _synthetic(
            PS, 3, "Pip & Spark - New", SITE + PS, "ps"))
        for book in os.listdir(self.root):
            with open(os.path.join(self.root, book, "pins.csv"),
                      encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    with open(os.path.join(self.root, book, r["filename"]),
                              "wb") as img:
                        img.write(PNG)
        self.overlay = _overlay(content_root=self.root)
        self.cfg = {"scheduled_posting": {"platforms": {PLATFORM: self.overlay}}}
        self.boards = [{"id": "b%d" % i, "serviceId": "s%d" % i, "name": n}
                       for i, n in enumerate(APPROVED)]
        self.board_reads = 0
        self.links = {SITE + "two-houses-now": (200, "Two Houses Now"),
                      SITE + "the-loudest-color": (200, "The Loudest Color"),
                      SITE + UN + "#design-01": (200, "Unfold Test")}
        self.fetched = []
        monkeypatch.setattr(pm, "DATA_ROOT", self.data)
        monkeypatch.setattr(pm, "get_profile_config", lambda name=None: self.cfg)
        monkeypatch.setattr(pm, "resolve_scheduled", lambda p, c: {
            "channel_id": "chan1", "api_key": "k", "identity_slug": "tbpub",
            "drain": False})
        monkeypatch.setenv("PRODUCTION_IDENTITY_SLUGS", "")

        def boards(channel_id, key=None, session=None):
            self.board_reads += 1
            return list(self.boards)
        monkeypatch.setattr(bc, "channel_boards", boards)

    def fetch(self, url):
        self.fetched.append(url)
        return self.links.get(url, (404, "Page not found"))

    def state(self):
        return cp.PipelineState(profile_name=PROFILE, platform=PLATFORM)

    def store(self):
        return hl.HoldStore(hl.holds_path(self.state().path))

    def add(self, book, dry_run=False, **kw):
        return books.add_book(PROFILE, PLATFORM, book, dry_run=dry_run,
                              out=lambda s: None, fetch=self.fetch, now=NOW, **kw)

    def snapshot(self):
        out = {}
        for root, _dirs, files in os.walk(self.data):
            for f in files:
                # The publisher lock writes its holder's name into its own
                # file on every run, dry or not (as topup's does). That is the
                # lock, not data, so it is left out of the compare.
                if f.endswith(".lock"):
                    continue
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[p] = fh.read()
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def _pending_keys(state):
    return [k for k, _r in topup._pending(state)]


# ── config ──────────────────────────────────────────────────────────────────

def test_channel_without_books_config_is_refused(env):
    del env.overlay["books"]
    code, summary = env.add(UN)
    assert code == books.EXIT_REFUSED
    assert "Books are not set up for this channel" in summary["refused"]
    code, summary = books.list_books(PROFILE, PLATFORM, out=lambda s: None)
    assert code == books.EXIT_REFUSED
    assert not os.path.exists(env.data)


@pytest.mark.parametrize("override,message", [
    ({"content_root": ""}, "content_root"),
    ({"series_order": []}, "series_order"),
    ({"series_order": ["a", "a"]}, "twice"),
    ({"csv_board_series": ["nope"]}, "not in series_order"),
    ({"link_keep_prefix_series": ["nope"]}, "not in series_order"),
    ({"board_map": {"x": ""}}, "board_map"),
    ({"board_map": ["x"]}, "board_map"),
    ({"surprise": 1}, "unknown keys"),
])
def test_books_config_validation(env, override, message):
    env.overlay["books"].update(override)
    with pytest.raises(books.BooksRefused, match=message):
        books.books_config(PROFILE, PLATFORM)


def test_books_config_parses(env):
    conf = books.books_config(PROFILE, PLATFORM)
    assert conf.series_order == tuple(SERIES)
    assert conf.csv_board_series == {"unfold"}


def test_missing_content_root_folder_is_refused(env):
    env.overlay["books"]["content_root"] = env.root + "/missing"
    code, summary = books.list_books(PROFILE, PLATFORM, out=lambda s: None)
    assert code == books.EXIT_REFUSED and "is not a folder" in summary["refused"]


def test_unknown_book_and_unknown_series_refused(env):
    code, summary = env.add("no-such-book")
    assert code == books.EXIT_REFUSED and "no book folder" in summary["refused"]
    _write_book(env.root, "zebra-stripes", _synthetic("zebra-stripes", 1, "B", SITE, "z"))
    code, summary = env.add("zebra-stripes")
    assert code == books.EXIT_REFUSED and "no series" in summary["refused"]


def test_duplicate_source_id_refused(env):
    _write_book(env.root, "unfold-dup", _synthetic("unfold-dup", 1, "B", SITE, "un"))
    code, summary = books.list_books(PROFILE, PLATFORM, out=lambda s: None)
    assert code == books.EXIT_REFUSED and "un-00" in summary["refused"]


# ── mapping parity with the scratch ─────────────────────────────────────────

@pytest.mark.parametrize("book", [WL, LU])
def test_mapping_matches_scratch_map_all(env, book):
    conf = books.books_config(PROFILE, PLATFORM)
    index = books.PinIndex(conf)
    exp = EXPECTED[book]
    seq = [p for p in index.series_sequence(exp["series"]) if p.book == book]
    assert [p.source_id for p in seq] == exp["order"]
    for p in seq:
        assert books.board_for(p, conf) == exp["board"][p.source_id]
        cands = books.link_candidates(p.book, p.series, p.source["link"], conf)
        assert cands == [exp["link"][p.source_id]]
        # The prefixed form is never a candidate (DEC-043(2)).
        assert all("/books/%s" % book not in u for u in cands)


def test_mapping_end_to_end_matches_scratch(env):
    code, summary = env.add(LU)
    assert code == 0, summary
    state = env.state()
    exp = EXPECTED[LU]
    by_sid = {q["source_id"]: state.rows[q["key"]]["row"] for q in summary["queued"]}
    assert [q["source_id"] for q in summary["queued"]] == exp["order"]
    for sid, row in by_sid.items():
        assert row["board"] == exp["board"][sid]
        assert row["link"] == exp["link"][sid]
        assert row["image_path"].endswith(".png")
        assert row["post_text"].startswith("Synthetic description")


def test_unfold_keeps_its_prefix_and_csv_board(env):
    conf = books.books_config(PROFILE, PLATFORM)
    index = books.PinIndex(conf)
    p = index.by_sid["un-00"]
    assert books.link_candidates(p.book, p.series, p.source["link"], conf) == \
        [SITE + UN + "#design-01"]
    assert books.board_for(p, conf) == "Art Nouveau Coloring Pages"


# ── checks to holds ─────────────────────────────────────────────────────────

def test_dead_link_goes_to_link_hold(env):
    code, summary = env.add(PS)
    assert code == 0
    assert summary["queued"] == []
    assert [h["kind"] for h in summary["held"]] == ["link"] * 3
    h = env.store().get("ps-00")
    assert h["kind"] == "link"
    assert h["reason"] == SITE + "test: link status 404"
    assert h["source"]["source_id"] == "ps-00"
    assert h["row"]["link"] == SITE + "test"
    assert h["held_at"] and h["book"] == PS and h["series"] == "pip-and-spark"
    # Not in the state file: holds never are.
    assert env.state().rows == {}
    # Each URL fetched once per run.
    assert env.fetched.count(SITE + "test") == 1


def test_board_not_approved_and_not_in_buffer_go_to_board_holds(env):
    env.overlay["approved_boards"] = [b for b in APPROVED
                                      if b != "Picture Books About Being Brave"]
    env.boards = [b for b in env.boards
                  if b["name"] != "Picture Books About Big Feelings"]
    env.add(LU)
    env.add(WL)
    holds = env.store().of_kind(["board"])
    reasons = {h["source_id"]: h["reason"] for h in holds}
    brave = [s for s, b in EXPECTED[LU]["board"].items()
             if b == "Picture Books About Being Brave"]
    feelings = [s for s, b in EXPECTED[WL]["board"].items()
                if b == "Picture Books About Big Feelings"]
    assert brave and feelings
    for s in brave:
        assert "not approved" in reasons[s]
    for s in feelings:
        assert "not usable in Buffer" in reasons[s]
    assert env.board_reads == 2      # once per run


def test_no_board_map_entry_is_a_board_hold(env):
    del env.overlay["books"]["board_map"][EXPECTED[LU]["order"][0]]
    env.add(LU)
    h = env.store().get(EXPECTED[LU]["order"][0])
    assert h["kind"] == "board" and "no board map entry" in h["reason"]


def test_row_check_failure_is_a_review_hold(env):
    os.remove(os.path.join(env.root, UN, "%s-01.png" % UN))
    env.add(UN)
    h = env.store().get("un-01")
    assert h["kind"] == "review" and "image_path does not exist" in h["reason"]


# ── order ───────────────────────────────────────────────────────────────────

def test_round_robin_rule():
    items = [("u1", "unfold"), ("u2", "unfold"), ("w1", "wild-littles"),
             ("l1", "lumora"), ("x1", None), ("l2", "lumora")]
    got = [i for i, _s in books.round_robin(items, SERIES)]
    assert got == ["u1", "w1", "l1", "x1", "u2", "l2"]


def test_merge_keeps_existing_order_and_interleaves():
    existing = [("U%d" % i, "unfold") for i in range(1, 6)]
    new = [("L%d" % i, "lumora") for i in range(1, 4)]
    merged = books.merge(existing, new, SERIES)
    assert [i for _t, i in merged] == ["U1", "L1", "U2", "L2", "U3", "L3",
                                       "U4", "U5"]
    # Existing rows that are not round-robin themselves stay as they are.
    existing = [("U1", "unfold"), ("U2", "unfold"), ("W1", "wild-littles")]
    merged = books.merge(existing, [("L1", "lumora")], SERIES)
    assert [i for t, i in merged if t == "E"] == ["U1", "U2", "W1"]


def test_series_sequence_is_covers_first_then_round_robin(env, tmp_path):
    # The cover is LAST in this book's file: it must still come first, ahead
    # of every page of every book in the series.
    rows = _synthetic("unfold-zz", 3, "Art Nouveau Coloring Pages",
                      SITE + "unfold-zz", "zz")
    _write_book(env.root, "unfold-zz", rows[1:] + rows[:1])
    index = books.PinIndex(books.books_config(PROFILE, PLATFORM))
    assert [p.source_id for p in index.series_sequence("unfold")] == \
        ["un-00", "zz-00", "un-01", "zz-01", "un-02", "zz-02"]


def test_add_book_interleaves_and_redates_without_moving_keys(env):
    code, s1 = env.add(UN)
    assert code == 0 and len(s1["queued"]) == 3
    state = env.state()
    before = _pending_keys(state)
    before_rows = {k: dict(state.rows[k]["row"]) for k in before}
    code, s2 = env.add(LU)
    assert code == 0 and len(s2["queued"]) == 11
    state = env.state()
    after = _pending_keys(state)
    # No existing row's key moved, and none changed state.
    for k in before:
        assert state.rows[k]["status"] == cp.PENDING
        assert state.rows[k]["row"][cp.PINNED_KEY] == k
        assert cp.effective_row_key(state.rows[k]["row"], state, PLATFORM) == k
        assert state.rows[k]["row"]["post_text"] == before_rows[k]["post_text"]
    # Relative order of the existing rows unchanged.
    assert [k for k in after if k in before] == before
    # Round-robin: U L U L U L L L ...
    series = ["U" if k in before else "L" for k in after]
    assert "".join(series) == "ULULUL" + "L" * 8
    # Consecutive slots at 5 a day from the first window that has not begun.
    slots = [(state.rows[k]["row"]["date"], state.rows[k]["row"]["time_window"])
             for k in after]
    assert slots[0] == ("2026-11-22", "late_afternoon")
    assert slots[:6] == [("2026-11-22", "late_afternoon"),
                         ("2026-11-22", "evening"), ("2026-11-22", "night"),
                         ("2026-11-23", "morning"), ("2026-11-23", "afternoon"),
                         ("2026-11-23", "late_afternoon")]
    assert len(set(slots)) == len(slots)


def test_redate_starts_after_last_scheduled_due(env):
    env.add(UN)
    state = env.state()
    k = _pending_keys(state)[0]
    state.update(k, status=cp.SCHEDULED, post_id="b1",
                 due_at="2026-11-24T01:26:39.000Z")    # 11-23 17:26 PT
    env.add(LU)
    state = env.state()
    first = state.rows[_pending_keys(state)[0]]["row"]
    assert (first["date"], first["time_window"]) == ("2026-11-23", "night")


def test_redate_pending_order_must_name_every_pending_row(env):
    env.add(UN)
    state = env.state()
    keys = _pending_keys(state)
    snap = env.snapshot()
    for bad in (keys[:-1], keys + ["nope"], keys + keys[:1]):
        with pytest.raises(ValueError, match="exactly once"):
            cp.redate_pending(PROFILE, PLATFORM, 5, now=NOW, state=state,
                              windows_for=lambda n: ["morning", "afternoon",
                                                     "late_afternoon",
                                                     "evening", "night"][:n],
                              order=bad)
    assert env.snapshot() == snap


# ── dedupe ──────────────────────────────────────────────────────────────────

def test_dedupe_across_every_state_and_holds(env):
    env.add(UN)
    state = env.state()
    keys = _pending_keys(state)
    state.update(keys[0], status=cp.SCHEDULED, post_id="b1",
                 due_at="2026-11-23T17:00:00.000Z")
    state.update(keys[1], status=cp.FAILED)
    code, again = env.add(UN)
    assert code == 0
    assert again["queued"] == [] and again["held"] == []
    assert set(again["already"].values()) == {"state:scheduled", "state:failed",
                                             "state:pending"}
    env.add(PS)
    code, again = env.add(PS)
    assert again["queued"] == [] and again["held"] == []
    assert set(again["already"].values()) == {"held:link"}
    assert len(env.state().rows) == 3


def test_dedupe_matches_state_rows_by_image_not_by_key(env):
    # A pin queued earlier by another path (no source id in the row) is found.
    env.add(UN)
    state = env.state()
    k = _pending_keys(state)[0]
    row = state.rows[k]["row"]
    row["image_path"] = row["image_path"].replace("/", "\\")
    state.save()
    code, again = env.add(UN)
    assert again["queued"] == []


def test_books_listing_counts(env):
    env.add(UN)
    env.add(PS)
    code, s = books.list_books(PROFILE, PLATFORM, out=lambda x: None)
    assert code == 0
    by = {b["book"]: b for b in s["books"]}
    assert (by[UN]["pins"], by[UN]["in_state"], by[UN]["held"], by[UN]["new"]) == (3, 3, 0, 0)
    assert (by[PS]["pins"], by[PS]["in_state"], by[PS]["held"], by[PS]["new"]) == (3, 0, 3, 0)
    assert (by[LU]["new"], by[WL]["new"]) == (11, 9)
    assert by[LU]["series"] == "lumora"


# ── dry run ─────────────────────────────────────────────────────────────────

def test_dry_runs_write_nothing(env):
    env.add(UN)
    env.add(PS)
    hold = hl.HoldStore(hl.holds_path(env.state().path))
    hold.put("ps-01", book=PS, series="pip-and-spark", kind="review",
             reason="operator review", source={}, row={})
    hold.save()
    snap = env.snapshot()
    code, s = env.add(LU, dry_run=True)
    assert code == 0 and len(s["queued"]) == 11 and s["dry_run"]
    env.links[SITE + "test"] = (200, "Test")
    code, s = books.recheck_holds(PROFILE, PLATFORM, dry_run=True,
                                  out=lambda x: None, fetch=env.fetch, now=NOW)
    assert code == 0 and len(s["queued"]) == 2
    code, s = books.release_hold(PROFILE, PLATFORM, ["ps-01"], dry_run=True,
                                 out=lambda x: None, fetch=env.fetch, now=NOW)
    assert code == 0 and len(s["queued"]) == 1
    code, s = books.hold_pins(PROFILE, PLATFORM, ["un-00"], "operator", "x",
                              dry_run=True, out=lambda x: None)
    assert code == books.EXIT_REFUSED       # it is queued
    code, s = books.hold_pins(PROFILE, PLATFORM, [EXPECTED[LU]["order"][0]],
                              "operator", "x", dry_run=True, out=lambda x: None)
    assert code == 0
    assert env.snapshot() == snap


def test_dry_run_snapshot_compare_can_fail(env):
    # The snapshot compare must see a real write, or it proves nothing.
    snap = env.snapshot()
    env.add(UN)
    assert env.snapshot() != snap


# ── recheck and release ─────────────────────────────────────────────────────

def test_recheck_queues_what_now_passes_and_keeps_the_rest(env):
    env.add(UN)
    env.add(PS)
    env.links[SITE + "test"] = (200, "Test")
    env.overlay["books"]["board_map"]["ps-02"] = "Not A Board"
    code, s = books.recheck_holds(PROFILE, PLATFORM, kinds=["link"],
                                  out=lambda x: None, fetch=env.fetch, now=NOW)
    assert code == 0
    assert sorted(q["source_id"] for q in s["queued"]) == ["ps-00", "ps-01"]
    store = env.store()
    assert store.keys() == {"ps-02"}
    assert store.get("ps-02")["kind"] == "board"
    state = env.state()
    assert len(_pending_keys(state)) == 5


def test_recheck_never_touches_review_or_operator(env):
    env.add(PS)
    store = env.store()
    for sid in ("ps-00", "ps-01"):
        h = store.get(sid)
        store.put(sid, book=h["book"], series=h["series"],
                  kind="review" if sid == "ps-00" else "operator",
                  reason="waiting on Jeff", source=h["source"], row=h["row"])
    store.save()
    env.links[SITE + "test"] = (200, "Test")
    code, s = books.recheck_holds(PROFILE, PLATFORM, out=lambda x: None,
                                  fetch=env.fetch, now=NOW)
    assert [q["source_id"] for q in s["queued"]] == ["ps-02"]
    assert env.store().keys() == {"ps-00", "ps-01"}
    with pytest.raises(books.BooksRefused):
        books.recheck_holds(PROFILE, PLATFORM, kinds=["review"],
                            out=lambda x: None, fetch=env.fetch, now=NOW)
    code, s = books.release_hold(PROFILE, PLATFORM, ["ps-00"],
                                 out=lambda x: None, fetch=env.fetch, now=NOW)
    assert code == 0 and [q["source_id"] for q in s["queued"]] == ["ps-00"]
    assert env.store().keys() == {"ps-01"}


def test_release_of_a_pin_that_still_fails_stays_held_under_the_check(env):
    env.add(PS)
    store = env.store()
    h = store.get("ps-00")
    store.put("ps-00", book=h["book"], series=h["series"], kind="review",
              reason="waiting", source=h["source"], row=h["row"])
    store.save()
    code, s = books.release_hold(PROFILE, PLATFORM, ["ps-00"],
                                 out=lambda x: None, fetch=env.fetch, now=NOW)
    assert s["queued"] == []
    assert env.store().get("ps-00")["kind"] == "link"
    code, s = books.release_hold(PROFILE, PLATFORM, ["not-held"],
                                 out=lambda x: None, fetch=env.fetch, now=NOW)
    assert code == books.EXIT_REFUSED


def test_hold_command_holds_unqueued_pins_and_refuses_queued(env):
    sid = EXPECTED[WL]["order"][0]
    code, s = books.hold_pins(PROFILE, PLATFORM, [sid], "review", "Jeff review",
                              out=lambda x: None)
    assert code == 0
    h = env.store().get(sid)
    assert (h["kind"], h["reason"], h["book"]) == ("review", "Jeff review", WL)
    assert h["row"]["board"] == EXPECTED[WL]["board"][sid]
    env.add(UN)
    code, s = books.hold_pins(PROFILE, PLATFORM, ["un-00"], "operator", "x",
                              out=lambda x: None)
    assert code == books.EXIT_REFUSED and "already in the queue" in s["refused"]
    with pytest.raises(books.BooksRefused):
        books.hold_pins(PROFILE, PLATFORM, [sid], "bogus", "x", out=lambda x: None)


# ── the hold store ──────────────────────────────────────────────────────────

def test_hold_store_round_trip_and_refusals(tmp_path):
    path = str(tmp_path / "pin_holds.json")
    s = hl.HoldStore(path)
    s.put("a", book="b", series="s", kind="link", reason="404", source={"x": 1},
          row={"title": "t"}, now=NOW)
    s.save()
    first = hl.HoldStore(path).get("a")["held_at"]
    s.put("a", book="b", series="s", kind="board", reason="gone", source={},
          row={}, now=datetime(2026, 12, 1, tzinfo=timezone.utc))
    s.save()
    again = hl.HoldStore(path).get("a")
    assert again["held_at"] == first and again["kind"] == "board"
    with pytest.raises(hl.HoldError):
        s.put("a", book="b", series="s", kind="nope", reason="r", source={}, row={})
    with pytest.raises(hl.HoldError):
        s.put("a", book="b", series="s", kind="link", reason=" ", source={}, row={})
    with open(path, "w", encoding="utf-8") as f:
        f.write("{broken")
    with pytest.raises(hl.HoldError):
        hl.HoldStore(path)


# ── the lock, both ways ─────────────────────────────────────────────────────

def test_books_refuses_while_topup_holds_the_lock(env):
    path = env.state().path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with publisher_lock.channel_lock(path, "topup tb/pinterest"):
        for code, s in (env.add(UN),
                        books.list_books(PROFILE, PLATFORM, out=lambda x: None),
                        books.recheck_holds(PROFILE, PLATFORM, out=lambda x: None),
                        books.release_hold(PROFILE, PLATFORM, ["x"], out=lambda x: None),
                        books.hold_pins(PROFILE, PLATFORM, ["un-00"], "review", "r",
                                        out=lambda x: None)):
            assert code == books.EXIT_REFUSED
            assert "topup" in s["refused"]
    assert env.state().rows == {}


def test_topup_refuses_while_add_book_runs(env, monkeypatch):
    seen = []
    monkeypatch.setattr(topup, "drain_is_on", lambda cfg, res: False)

    def fetch_and_try_topup(url):
        if not seen:
            seen.append(topup.run(PROFILE, PLATFORM, now=NOW, out=lambda x: None))
        return env.fetch(url)

    code, _s = books.add_book(PROFILE, PLATFORM, UN, out=lambda x: None,
                              fetch=fetch_and_try_topup, now=NOW)
    assert code == 0
    tcode, tsummary = seen[0]
    assert tcode == topup.EXIT_REFUSED
    assert "add-book" in tsummary["refused"]


# ── dashboard routes ────────────────────────────────────────────────────────

@pytest.fixture
def client(env, monkeypatch):
    # The routes pass no fetch, so link_problem uses topup.fetch_page: the
    # same fake answers.
    monkeypatch.setattr(topup, "fetch_page", env.fetch)
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def test_route_lists_books_and_holds(client, env):
    env.add(PS)
    r = client.get("/api/scheduled/tb/books?platform=pinterest")
    assert r.status_code == 200
    d = r.get_json()
    assert d["configured"] is True
    assert {b["book"] for b in d["books"]} == {WL, LU, UN, PS}
    assert len(d["holds"]) == 3


def test_route_says_not_configured(client, env):
    del env.overlay["books"]
    d = client.get("/api/scheduled/tb/books?platform=pinterest").get_json()
    assert d["configured"] is False


def test_route_add_needs_confirm(client, env):
    snap = env.snapshot()
    r = client.post("/api/scheduled/tb/books/add",
                    json={"platform": "pinterest", "book": UN})
    assert r.status_code == 200 and r.get_json()["dry_run"] is True
    assert len(r.get_json()["queued"]) == 3
    r = client.post("/api/scheduled/tb/books/add",
                    json={"platform": "pinterest", "book": UN, "confirm": "yes"})
    assert r.get_json()["dry_run"] is True
    assert env.snapshot() == snap
    r = client.post("/api/scheduled/tb/books/add",
                    json={"platform": "pinterest", "book": UN, "confirm": True})
    assert r.status_code == 200 and r.get_json()["dry_run"] is False
    assert len(_pending_keys(env.state())) == 3


def test_routes_recheck_and_release_need_confirm(client, env):
    env.add(PS)
    env.links[SITE + "test"] = (200, "Test")
    snap = env.snapshot()
    for path, body in (("holds/recheck", {}), ("holds/release", {"key": "ps-00"})):
        r = client.post("/api/scheduled/tb/" + path,
                        json=dict(body, platform="pinterest"))
        assert r.status_code == 200 and r.get_json()["dry_run"] is True
    assert env.snapshot() == snap
    r = client.post("/api/scheduled/tb/holds/release",
                    json={"platform": "pinterest", "key": "ps-00", "confirm": True})
    assert r.status_code == 200 and len(r.get_json()["queued"]) == 1
    r = client.post("/api/scheduled/tb/holds/recheck",
                    json={"platform": "pinterest", "confirm": True})
    assert len(r.get_json()["queued"]) == 2
    assert env.store().keys() == set()


def test_routes_take_the_lock(client, env):
    path = env.state().path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with publisher_lock.channel_lock(path, "topup tb/pinterest"):
        r = client.post("/api/scheduled/tb/books/add",
                        json={"platform": "pinterest", "book": UN, "confirm": True})
        assert r.status_code == 409 and "topup" in r.get_json()["refused"]
        r = client.get("/api/scheduled/tb/books?platform=pinterest")
        assert r.status_code == 409
        r = client.post("/api/scheduled/tb/holds/release",
                        json={"platform": "pinterest", "key": "x", "confirm": True})
        assert r.status_code == 409
    assert env.state().rows == {}


def test_route_bad_input(client, env):
    assert client.post("/api/scheduled/tb/books/add",
                       json={"platform": "pinterest"}).status_code == 400
    assert client.post("/api/scheduled/tb/holds/release",
                       json={"platform": "pinterest"}).status_code == 400
    assert client.post("/api/scheduled/tb/holds/recheck",
                       json={"platform": "pinterest", "kind": "review"}).status_code == 400


def test_calendar_lists_holds_beside_the_grid(client, env):
    env.add(PS)
    env.add(UN)
    d = client.get("/api/scheduled/tb/calendar?from=2026-11-01&to=2026-11-30").get_json()
    undated = {i["key"]: i for i in d["undated"]}
    assert set(undated) == {"ps-00", "ps-01", "ps-02"}
    assert undated["ps-00"]["group"] == "held"
    assert undated["ps-00"]["hold_reason"].startswith("link: ")
    assert undated["ps-00"]["detail"]["meaning"].startswith("not in the queue")
    assert len(d["items"]) == 3


def test_page_has_books_card_with_confirm_flow(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="schpBooksCard"' in html
    js = html[html.index("// ─── Books and holds (F7)"):html.index("// ─── Content calendar (U1)")]
    # Every write goes through the plan-then-confirm helper.
    assert js.count("{confirm: true}") == 1
    assert "if (!confirm(schpBooksPlanText(plan))) return;" in js
    for fn in ("schpAddBook", "schpRecheckHolds", "schpReleaseHold"):
        body = js[js.index("function %s(" % fn):]
        body = body[:body.index("\n}\n")]
        assert "schpBooksConfirmed(" in body


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_dispatches_books_commands(env, monkeypatch, capsys):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools"))
    import run_scheduled_posts as rsp
    monkeypatch.setattr(sys, "argv", ["x", "books", "--profile", PROFILE,
                                      "--platform", PLATFORM])
    assert rsp.main() == 0
    assert "books=4 pins=26 new=26" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["x", "recheck-holds", "--profile", PROFILE,
                                      "--platform", PLATFORM, "--kind", "review"])
    assert rsp.main() == books.EXIT_REFUSED
    monkeypatch.setattr(sys, "argv", ["x", "add-book", "--profile", PROFILE,
                                      "--platform", PLATFORM, "--book", "nope",
                                      "--dry-run"])
    assert rsp.main() == books.EXIT_REFUSED


def test_add_book_redates_at_the_overlays_per_day_not_a_constant(env):
    # D-028 T1: Tinderbox drops to 2 a day. add-book must take the count and
    # its windows from the overlay at run time: the existing rows and the new
    # book both land at 2 a day, in morning and evening only.
    env.overlay["cadence"] = {"per_day": 2, "quiet_days": [],
                              "windows": {"2": ["morning", "evening"]}}
    code, _s1 = env.add(UN)
    assert code == 0
    before = _pending_keys(env.state())
    code, s2 = env.add(LU)
    assert code == 0 and len(s2["queued"]) == 11
    state = env.state()
    after = _pending_keys(state)
    assert [k for k in after if k in before] == before
    series = "".join("U" if k in before else "L" for k in after)
    assert series == "ULULUL" + "L" * 8
    slots = [(state.rows[k]["row"]["date"], state.rows[k]["row"]["time_window"])
             for k in after]
    # 14:00 PT on 11-22: morning has begun, so the first slot is that evening.
    assert slots[:5] == [("2026-11-22", "evening"), ("2026-11-23", "morning"),
                         ("2026-11-23", "evening"), ("2026-11-24", "morning"),
                         ("2026-11-24", "evening")]
    per_day = {}
    for d, w in slots:
        per_day.setdefault(d, []).append(w)
    assert all(len(ws) <= 2 and set(ws) <= {"morning", "evening"}
               for ws in per_day.values())
    assert len(set(slots)) == len(slots)
