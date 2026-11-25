"""D-022 F6: the headless ``topup`` command (linkedin_automation/topup.py).

Buffer is mocked at its boundary (``scheduled_slots``, ``channel_boards``,
``get_channel``, ``create_post``) and links through an injected ``fetch``, so
nothing leaves the machine. Times are fixed in America/Los_Angeles; 2036-11-03
is a Monday in PST (UTC-8).
"""

import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402
from linkedin_automation import publisher_lock  # noqa: E402
from linkedin_automation import topup  # noqa: E402

PROFILE = "brand"
PLATFORM = "pinterest"
CHAN = "chan_pin"
HANDLE = "synthetic_pinner"
KEY_ENV = "BUFFER_API_KEY_PIN"
IMAGE = os.path.join(HERE, "fixtures", "platform_golden", "pixel.png")
# 06:00 local on Mon 2036-11-03: no window of the day has begun.
NOW = datetime(2036, 11, 3, 14, 0, tzinfo=timezone.utc)

BOARDS = [{"id": "b1", "serviceId": "svc-1", "name": "Approved One"},
          {"id": "b2", "serviceId": "svc-2", "name": "Approved Two"},
          {"id": "b3", "serviceId": "svc-3", "name": "Series - Not Approved"}]
APPROVED = ["Approved One", "Approved Two", "Approved Not In Buffer"]
FIVE = ["morning", "afternoon", "late_afternoon", "evening", "night"]
CADENCE = {"per_day": 5,
           "quiet_days": [{"dates": ["12-24", "12-25", "12-26", "12-31",
                                     "01-01"], "per_day": 2}],
           "windows": {"5": FIVE, "2": ["morning", "evening"],
                       "1": ["afternoon"]}}
GOOD = (200, "A Book · Site")


def pin(i, date="2036-11-03", window="morning", board="Approved One",
        link=None):
    return {"date": date, "time_window": window,
            "post_text": "Pin %d description." % i, "topic": "", "tags": "",
            "image_path": IMAGE, "first_comment_link": "",
            "link": link or "https://site.test/books/b%d" % i,
            "title": "Pin %d" % i, "board": board, "alt_text": "A square."}


def five_a_day(n, start_day=3):
    """``n`` rows on consecutive five-window days from 2036-11-<start_day>."""
    return [pin(i, "2036-11-%02d" % (start_day + i // 5), FIVE[i % 5])
            for i in range(n)]


class World(object):
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.calls = []
        self.free = 8
        self.limit = 10
        self.boards = list(BOARDS)
        self.links = {}
        self.fetched = []
        self.overlay = {"buffer_api_key_env": KEY_ENV,
                        "buffer_channel_id": CHAN, "identity_slug": HANDLE,
                        "drain": False, "approved_boards": list(APPROVED),
                        "cadence": copy.deepcopy(CADENCE),
                        "topup_reserve": 2}
        self.section = {"platforms": {PLATFORM: self.overlay}}
        self.create_error = None
        mp = monkeypatch
        mp.setenv("POST_TIMEZONE", "America/Los_Angeles")
        mp.setenv(KEY_ENV, "pin-key")
        mp.setattr(pm, "get_profile_config",
                   lambda name=None: {"scheduled_posting": self.section})

        def slots(channel_id, key=None, session=None, max_age=None):
            self.calls.append(("slots", channel_id, key, max_age))
            return {"limit": self.limit, "used": self.limit - self.free,
                    "free": self.free}

        def get_channel(channel_id, key=None, session=None, max_age=None):
            self.calls.append(("get_channel", channel_id, key))
            return {"id": channel_id, "name": HANDLE, "service": "pinterest",
                    "organizationId": "org"}

        def channel_boards(channel_id, key=None, session=None):
            self.calls.append(("channel_boards", channel_id, key))
            return list(self.boards)

        def create_post(channel_id, text, image_url, when, key=None,
                        session=None, **extras):
            self.calls.append(("create_post", channel_id, key))
            if self.create_error:
                raise self.create_error
            return {"id": "post-%d" % len(self.calls), "dueAt": when}

        mp.setattr(bc, "scheduled_slots", slots)
        mp.setattr(bc, "get_channel", get_channel)
        mp.setattr(bc, "channel_boards", channel_boards)
        mp.setattr(bc, "create_post", create_post)
        mp.setattr(cp.image_host, "upload_image",
                   lambda path, **kw: "https://img.test.invalid/p.png")
        dashboard.jobs.clear()
        dashboard.jobs["j"] = {"log": [], "progress": "", "status": "running"}

    def state(self):
        return cp.PipelineState(profile_name=PROFILE, platform=PLATFORM)

    def queue(self, rows, status=cp.PENDING):
        state = self.state()
        keys = []
        for row in rows:
            k = cp.row_key(row, PLATFORM)
            state.update(k, status=status, row=dict(row),
                         text=cp.row_preview(row), errors=[])
            keys.append(k)
        return keys

    def fetch(self, url):
        self.fetched.append(url)
        v = self.links.get(url, GOOD)
        if isinstance(v, Exception):
            raise v
        return v

    def run(self, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("fetch", self.fetch)
        lines = []
        code, summary = topup.run(PROFILE, PLATFORM, out=lines.append, **kw)
        summary["lines"] = lines
        return code, summary

    def created(self):
        return [c for c in self.calls if c[0] == "create_post"]


@pytest.fixture
def world(monkeypatch):
    w = World(monkeypatch)
    yield w
    dashboard.jobs.clear()


def _status(state, key):
    return state.rows[key]["status"]


def _sched_keys(summary):
    return [s["key"] for s in summary["scheduled"]]


# ─── N and the reserve ───────────────────────────────────────────────────────

def test_n_is_free_minus_the_configured_reserve(world):
    keys = world.queue(five_a_day(8))
    world.free = 5
    code, s = world.run()
    assert code == 0
    assert (s["free"], s["reserve"], s["n"]) == (5, 2, 3)
    assert _sched_keys(s) == keys[:3]
    assert len(world.created()) == 3
    st = world.state()
    assert [_status(st, k) for k in keys] == [cp.SCHEDULED] * 3 + [cp.PENDING] * 5
    assert "free=5 reserve=2 N=3 scheduled=3 skipped=0 redated=0" in s["lines"][-1]


def test_the_reserve_flag_overrides_the_config(world):
    keys = world.queue(five_a_day(8))
    world.free = 5
    code, s = world.run(reserve=4)
    assert code == 0 and s["n"] == 1 and _sched_keys(s) == keys[:1]


def test_reserve_defaults_to_2_when_the_overlay_has_none(world):
    del world.overlay["topup_reserve"]
    world.queue(five_a_day(8))
    world.free = 6
    code, s = world.run()
    assert (s["reserve"], s["n"], len(s["scheduled"])) == (2, 4, 4)


@pytest.mark.parametrize("free", [2, 1, 0])
def test_nothing_to_do_when_n_is_not_positive(world, free):
    keys = world.queue(five_a_day(3))
    world.free = free
    before = open(world.state().path, encoding="utf-8").read()
    code, s = world.run()
    assert code == 0
    assert s["n"] == free - 2 and s["scheduled"] == []
    assert world.created() == []
    assert ("channel_boards", CHAN, "pin-key") not in world.calls
    assert open(world.state().path, encoding="utf-8").read() == before
    assert "note=nothing to do" in s["lines"][-1]
    assert [_status(world.state(), k) for k in keys] == [cp.PENDING] * 3


def test_no_pending_rows_is_ok_and_sends_nothing(world):
    world.queue([pin(0)], status=cp.SCHEDULED)
    code, s = world.run()
    assert code == 0 and world.created() == []
    assert "note=no PENDING rows" in s["lines"][-1]


# ─── order ───────────────────────────────────────────────────────────────────

def test_pending_order_is_respected_and_other_states_are_untouched(world):
    rows = five_a_day(6)
    # Queued latest first, so insertion order is the reverse of due order.
    keys = world.queue(list(reversed(rows)))
    held = world.queue([pin(90, "2036-11-03", "morning",
                            link="https://site.test/held")], status=cp.HELD)
    world.free = 5
    code, s = world.run()
    assert code == 0
    assert _sched_keys(s) == list(reversed(keys))[:3]
    assert [p["time_window"] for p in s["planned"]] == FIVE[:3]
    assert _status(world.state(), held[0]) == cp.HELD
    assert "https://site.test/held" not in world.fetched


# ─── board and link checks ───────────────────────────────────────────────────

@pytest.mark.parametrize("board,reason", [
    ("Series - Not Approved", "not approved"),
    ("Approved Not In Buffer", "not usable in Buffer"),
])
def test_a_bad_board_is_skipped_and_the_next_row_takes_its_place(
        world, board, reason):
    rows = five_a_day(5)
    rows[1]["board"] = board
    keys = world.queue(rows)
    world.free = 5
    code, s = world.run()
    assert code == 0
    assert _sched_keys(s) == [keys[0], keys[2], keys[3]]
    assert [x["key"] for x in s["skipped"]] == [keys[1]]
    assert reason in s["skipped"][0]["reason"]
    st = world.state()
    assert _status(st, keys[1]) == cp.PENDING
    assert st.rows[keys[1]]["row"] == rows[1]
    assert "skipped=1" in s["lines"][-1] and keys[1] in s["lines"][-1]


@pytest.mark.parametrize("answer,reason", [
    ((404, "Page not found"), "link status 404"),
    ((500, "Error"), "link status 500"),
    ((301, None), "link redirects (301)"),
    ((302, "Redirecting..."), "link redirects (302)"),
    ((200, "Redirecting..."), "link title 'Redirecting...'"),
    ((200, "Page not found · Site"), "link title 'Page not found · Site'"),
    ((200, None), "link page has no title"),
    (ConnectionError("down"), "link error ConnectionError"),
])
def test_each_link_failure_is_skipped_and_the_next_row_taken(
        world, answer, reason):
    rows = five_a_day(5)
    keys = world.queue(rows)
    world.links[rows[0]["link"]] = answer
    world.free = 4
    code, s = world.run()
    assert code == 0
    assert _sched_keys(s) == keys[1:3]
    assert s["skipped"] == [{"key": keys[0], "title": "Pin 0",
                             "reason": reason}]
    assert _status(world.state(), keys[0]) == cp.PENDING


def test_a_row_with_no_link_is_skipped(world):
    rows = five_a_day(2)
    rows[0]["link"] = ""
    keys = world.queue(rows)
    world.free = 3
    code, s = world.run()
    assert _sched_keys(s) == [keys[1]]
    assert s["skipped"][0]["reason"] == "no link"


def test_the_link_check_returns_both_verdicts():
    assert topup.link_problem("https://a", lambda u: (200, "Real Page")) is None
    assert topup.link_problem("https://a", lambda u: (404, "x")) is not None


def test_the_board_check_returns_both_verdicts():
    assert topup.board_problem({"board": "Approved One"}, set(APPROVED),
                               BOARDS) is None
    assert topup.board_problem({"board": "Approved One"}, set(), BOARDS)
    assert topup.board_problem({"board": "Approved Not In Buffer"},
                               set(APPROVED), BOARDS)


def test_boards_are_read_once_and_each_link_once_per_run(world):
    rows = five_a_day(4)
    for r in rows:
        r["link"] = "https://site.test/books/same#design-0%s" % r["title"][-1]
    world.queue(rows)
    world.free = 6
    code, s = world.run()
    assert code == 0 and len(s["scheduled"]) == 4
    assert world.calls.count(("channel_boards", CHAN, "pin-key")) == 1
    assert len(world.fetched) == 4      # one per distinct URL as given


def test_fetch_page_drops_the_fragment_and_does_not_follow_redirects(
        monkeypatch):
    seen = {}

    class Resp(object):
        status_code = 200
        text = "<html><title>\n Ada&#39;s   Book </title></html>"

    def get(url, **kw):
        seen.update(url=url, **kw)
        return Resp()

    import requests
    monkeypatch.setattr(requests, "get", get)
    assert topup.fetch_page("https://s.test/books/x#design-08") == (
        200, "Ada's Book")
    assert seen["url"] == "https://s.test/books/x"
    assert seen["allow_redirects"] is False and seen["timeout"]


# ─── overdue rows ────────────────────────────────────────────────────────────

def test_overdue_rows_are_redated_before_scheduling_with_keys_unchanged(world):
    # The queue starts at 2036-11-03 morning; it is now 13:30 local, so
    # morning and afternoon have begun.
    keys = world.queue(five_a_day(7))
    order = [r["title"] for r in cp.pending_rows(world.state())]
    now = datetime(2036, 11, 3, 21, 30, tzinfo=timezone.utc)
    world.free = 4
    code, s = world.run(now=now)
    assert code == 0
    assert s["redated"] == 7
    st = world.state()
    assert set(st.rows) == set(keys), "no key moved"
    for k in keys:
        assert st.rows[k]["row"][cp.PINNED_KEY] == k
    # Order unchanged; the first open window is late_afternoon.
    assert [p["key"] for p in s["planned"]] == keys[:2]
    assert [(p["date"], p["time_window"]) for p in s["planned"]] == [
        ("2036-11-03", "late_afternoon"), ("2036-11-03", "evening")]
    assert [r["title"] for r in cp.pending_rows(st)] == order[2:]
    assert [st.rows[k]["status"] for k in keys[:2]] == [cp.SCHEDULED] * 2
    assert "redated=7" in s["lines"][-1]


def test_a_redate_starts_after_the_last_post_already_in_buffer(world):
    keys = world.queue(five_a_day(3))
    # A post already in Buffer for this evening, 19:10 local.
    st = world.state()
    st.update("already", status=cp.SCHEDULED, post_id="p0",
              due_at="2036-11-04T03:10:00.000Z", row=pin(99))
    now = datetime(2036, 11, 3, 21, 30, tzinfo=timezone.utc)
    world.free = 3
    code, s = world.run(now=now)
    assert code == 0 and s["redated"] == 3
    assert (s["planned"][0]["date"], s["planned"][0]["time_window"]) == (
        "2036-11-03", "night")
    assert s["planned"][0]["key"] == keys[0]


def test_rows_not_overdue_are_not_redated(world):
    world.queue(five_a_day(3))
    before = {k: dict(v["row"]) for k, v in world.state().rows.items()}
    code, s = world.run(reserve=10)
    assert s["redated"] == 0
    assert {k: v["row"] for k, v in world.state().rows.items()} == before


# ─── lock, drain, production, config ─────────────────────────────────────────

def test_topup_refuses_while_the_dashboard_publisher_holds_the_lock(world):
    world.queue(five_a_day(3))
    with publisher_lock.channel_lock(world.state().path, "dashboard job j"):
        code, s = world.run()
    assert code == topup.EXIT_REFUSED
    assert "dashboard job j" in s["refused"]
    assert world.calls == [], "no Buffer call at all"


def test_the_drain_and_schedule_button_refuse_while_topup_holds_the_lock(
        world):
    """Run the dashboard's two publishers from inside a real topup run, while
    it holds the lock (during its link check), and see both refused."""
    world.queue(five_a_day(3))
    world.queue([pin(50, "2036-11-20", "morning")], status=cp.HELD)
    inside = {}

    def fetch(url):
        if not inside:
            inside["drain"] = dashboard._drain_feed_job(
                "j", PROFILE, CHAN, 5, platform=PLATFORM)
            inside["button"] = dashboard._scheduled_schedule_job(
                "j", PROFILE, CHAN, platform=PLATFORM)
            inside["created_meanwhile"] = len(world.created())
        return GOOD

    world.free = 5
    code, s = world.run(fetch=fetch)
    assert code == 0 and len(s["scheduled"]) == 3
    for who in ("drain", "button"):
        assert "another publisher" in inside[who]["error"], who
        assert "topup %s/%s" % (PROFILE, PLATFORM) in inside[who]["error"]
    assert inside["created_meanwhile"] == 0
    assert len(world.created()) == 3


@pytest.mark.parametrize("where,value", [
    ("profile", {"enabled": True}),
    ("overlay", True),
    ("overlay", {"enabled": True}),
])
def test_topup_refuses_when_the_drain_is_on(world, where, value):
    world.queue(five_a_day(3))
    if where == "profile":
        world.section["drain"] = value
    else:
        world.overlay["drain"] = value
    code, s = world.run()
    assert code == topup.EXIT_REFUSED
    assert "drain is on" in s["refused"]
    assert world.calls == []


def test_a_drain_switched_off_does_not_refuse(world):
    world.section["drain"] = {"enabled": False}
    world.overlay["drain"] = {"enabled": False}
    world.queue(five_a_day(1))
    assert world.run()[0] == 0


@pytest.mark.parametrize("drop", ["approved_boards", "cadence"])
def test_a_channel_without_topup_config_is_refused(world, drop):
    del world.overlay[drop]
    world.queue(five_a_day(3))
    code, s = world.run()
    assert code == topup.EXIT_REFUSED
    assert drop in s["refused"] and "invents neither" in s["refused"]
    assert world.calls == []


def test_a_platform_with_no_overlay_is_refused(world, monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "li-key")
    world.section["buffer_channel_id"] = "chan_li"
    world.section["identity_slug"] = "someone"
    lines = []
    code, s = topup.run(PROFILE, "linkedin", now=NOW, fetch=world.fetch,
                        out=lines.append)
    assert code == topup.EXIT_REFUSED
    assert "no scheduled_posting.platforms.linkedin" in s["refused"]
    assert world.calls == []


def test_a_missing_key_is_refused_with_no_fallback(world, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    monkeypatch.setenv("BUFFER_API_KEY", "li-key")
    world.queue(five_a_day(1))
    code, s = world.run()
    assert code == topup.EXIT_REFUSED and KEY_ENV in s["refused"]
    assert world.calls == []


def test_a_declared_production_identity_is_refused_before_buffer(
        world, monkeypatch):
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, HANDLE)
    world.queue(five_a_day(1))
    code, s = world.run()
    assert code == topup.EXIT_PRODUCTION_REFUSED
    assert world.calls == []


def test_a_wrong_channel_identity_is_refused_and_nothing_created(
        world, monkeypatch):
    monkeypatch.setattr(bc, "get_channel", lambda *a, **k: {
        "id": CHAN, "name": "someone_else", "service": "pinterest"})
    world.queue(five_a_day(2))
    code, s = world.run()
    assert code == topup.EXIT_REFUSED and world.created() == []


# ─── Buffer errors ───────────────────────────────────────────────────────────

def test_a_failed_slot_read_exits_nonzero_and_sends_nothing(world,
                                                            monkeypatch):
    def boom(*a, **k):
        raise bc.BufferError("HTTP 500")
    monkeypatch.setattr(bc, "scheduled_slots", boom)
    world.queue(five_a_day(2))
    code, s = world.run()
    assert code == topup.EXIT_BUFFER_ERROR and world.created() == []
    assert "HTTP 500" in s["lines"][-1]


def test_a_buffer_rejection_stops_the_run_and_sends_no_further_row(world):
    keys = world.queue(five_a_day(4))
    world.create_error = bc.BufferRejected("bad pin shape")
    world.free = 6
    code, s = world.run()
    assert code == topup.EXIT_BUFFER_ERROR
    assert len(world.created()) == 1, "stopped after the first rejection"
    assert "bad pin shape" in s["lines"][-1]
    st = world.state()
    assert _status(st, keys[0]) == cp.FAILED
    assert [_status(st, k) for k in keys[1:]] == [cp.PENDING] * 3


# ─── dry run ─────────────────────────────────────────────────────────────────

def _snapshot(world):
    st = world.state()
    files = {}
    root = os.path.dirname(st.path)
    for name in sorted(os.listdir(root)):
        if name.endswith(".lock"):
            continue
        with open(os.path.join(root, name), "rb") as f:
            files[name] = f.read()
    return files


def test_a_dry_run_writes_nothing_even_when_it_would_redate(world):
    world.queue(five_a_day(6))
    before = _snapshot(world)
    now = datetime(2036, 11, 3, 21, 30, tzinfo=timezone.utc)
    world.free = 5
    code, s = world.run(now=now, dry_run=True)
    assert code == 0
    assert _snapshot(world) == before
    assert world.created() == []
    assert not [c for c in world.calls if c[0] == "get_channel"]
    assert s["redated"] == 6 and len(s["planned"]) == 3
    assert [p["time_window"] for p in s["planned"]] == [
        "late_afternoon", "evening", "night"]
    assert all("would schedule" in ln for ln in s["lines"][1:4])
    assert "[dry-run]" in s["lines"][-1] and "would_schedule=3" in s["lines"][-1]


def test_a_dry_run_plan_matches_the_real_run(world):
    world.queue(five_a_day(6))
    world.free = 6
    _code, dry = world.run(dry_run=True)
    _code, real = world.run()
    assert [p["key"] for p in dry["planned"]] == _sched_keys(real)


# ─── cadence config ──────────────────────────────────────────────────────────

def test_the_dec_046_cadence_parses_to_five_a_day_and_two_on_quiet_days():
    from datetime import date
    c = topup.parse_cadence(CADENCE)
    assert c.per_day(date(2026, 11, 22)) == 5
    assert c.per_day(date(2026, 12, 23)) == 5
    for d in (date(2026, 12, 24), date(2026, 12, 25), date(2026, 12, 26),
              date(2026, 12, 31), date(2027, 1, 1)):
        assert c.per_day(d) == 2
    assert c.windows_for(5) == FIVE
    assert c.windows_for(2) == ["morning", "evening"]
    assert c.windows_for(1) == ["afternoon"]
    assert c.windows_for(0) == []


@pytest.mark.parametrize("bad", [
    {"per_day": 5, "windows": {"2": ["morning", "evening"]}},
    {"per_day": 2, "windows": {"2": ["morning", "teatime"]}},
    {"per_day": 2, "windows": {"2": ["morning"]}},
    {"per_day": 2, "windows": {"2": ["morning", "morning"]}},
    {"per_day": 6, "windows": {}},
    {"per_day": 2, "windows": {"2": ["morning", "evening"]},
     "quiet_days": [{"dates": ["13-01"], "per_day": 1}]},
    {"per_day": 2, "windows": {"2": ["morning", "evening"]},
     "quiet_days": [{"dates": ["12-24"], "per_day": 1}]},
    {"per_day": 2},
    "5 a day",
])
def test_a_malformed_cadence_is_refused(bad):
    with pytest.raises(topup.TopupRefused):
        topup.parse_cadence(bad)


def test_a_malformed_cadence_refuses_the_run(world):
    world.overlay["cadence"] = {"per_day": 5, "windows": {}}
    world.queue(five_a_day(1))
    code, s = world.run()
    assert code == topup.EXIT_REFUSED and world.calls == []


# ─── the dashboard drops rows another publisher changed ──────────────────────

def test_the_dashboard_drops_rows_scheduled_or_redated_since_it_read_them(
        world):
    keys = world.queue(five_a_day(3))
    stale = cp.pending_rows(world.state())
    unknown = pin(77, "2036-11-09")
    # Another publisher schedules row 0 and re-dates row 1.
    st = world.state()
    st.update(keys[0], status=cp.SCHEDULED, post_id="p")
    cp.redate_pending(PROFILE, PLATFORM, 1, start=("2036-11-10", "morning"),
                      now=NOW, state=st)
    st.reload()
    kept = dashboard._rows_still_schedulable("j", stale + [unknown], st,
                                             PLATFORM)
    assert kept == [unknown]
    # Row 2 was re-dated too (every PENDING row is), so its stale copy goes.
    fresh = cp.pending_rows(st)
    assert dashboard._rows_still_schedulable("j", fresh, st, PLATFORM) == fresh


# ─── CLI and task script ─────────────────────────────────────────────────────

def test_the_cli_routes_topup_and_returns_its_exit_code(monkeypatch):
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import run_scheduled_posts as rsp
    seen = {}

    def fake_run(profile, platform, reserve=None, dry_run=False):
        seen.update(profile=profile, platform=platform, reserve=reserve,
                    dry_run=dry_run)
        return 2, {}
    monkeypatch.setattr(rsp.topup, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "run_scheduled_posts.py", "topup", "--profile", "tinderbox",
        "--platform", "pinterest", "--reserve", "3", "--dry-run"])
    assert rsp.main() == 2
    assert seen == {"profile": "tinderbox", "platform": "pinterest",
                    "reserve": 3, "dry_run": True}


_PS_PARSE = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $args[0], [ref]$tokens, [ref]$errors)
$cmds = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.CommandAst] }, $true)
$out = @{
  errors = @($errors | ForEach-Object { $_.Message })
  commands = @($cmds | ForEach-Object { ,@($_.CommandElements | ForEach-Object { $_.Extent.Text }) })
  arguments = @($ast.FindAll({ param($n)
      $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
      $n.Left.Extent.Text -eq '$Arguments' }, $true) | ForEach-Object { $_.Right.Extent.Text })
}
$out | ConvertTo-Json -Depth 5 -Compress
"""


@pytest.mark.skipif(sys.platform != "win32",
                    reason="PowerShell's parser is on Windows only")
def test_the_task_script_parses_with_three_triggers_and_the_topup_command(
        tmp_path):
    """Parsed, never run (D-022): no task is registered by this test."""
    script = os.path.join(REPO, "tools", "setup_topup_task.ps1")
    probe = tmp_path / "parse.ps1"
    probe.write_text(_PS_PARSE, encoding="utf-8")
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(probe), script],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    parsed = json.loads(out.stdout)
    assert parsed["errors"] == []
    cmds = parsed["commands"]
    triggers = [c for c in cmds if c[0] == "New-ScheduledTaskTrigger"]
    assert [c[c.index("-At") + 1] for c in triggers] == [
        "'07:00'", "'13:00'", "'21:00'"]
    assert all("-Daily" in c for c in triggers)
    action = [c for c in cmds if c[0] == "New-ScheduledTaskAction"]
    assert len(action) == 1
    assert action[0][action[0].index("-Execute") + 1] == "$Uv"
    assert any(c[:2] == ["Get-Command", "uv.exe"] for c in cmds)
    assert parsed["arguments"] == [
        '"run --no-sync python tools\\run_scheduled_posts.py topup '
        '--profile $Profile --platform $Platform '
        '--log-file data\\logs\\topup.log"']
    registers = [c for c in cmds if c[0] == "Register-ScheduledTask"]
    assert len(registers) == 1 and "$Triggers" in registers[0]
    assert any(c[0] == "Unregister-ScheduledTask" for c in cmds)


def test_a_schedule_button_job_that_read_rows_before_a_topup_sends_none_twice(
        world, monkeypatch):
    """The real interleaving: the button's job reads its rows, then a topup
    runs to completion (re-dating an overdue queue and scheduling two rows)
    before the job takes the lock. The job must send none of its stale rows:
    the two now in Buffer would be second posts, and the rest carry dates the
    topup has since moved."""
    keys = world.queue(five_a_day(6))
    # Pinned by an earlier re-date, as every Tinderbox row is.
    cp.redate_pending(PROFILE, PLATFORM, topup.parse_cadence(CADENCE).per_day,
                      start=("2036-11-03", "morning"), now=NOW,
                      windows_for=topup.parse_cadence(CADENCE).windows_for)
    world.free = 4
    real_slots = bc.scheduled_slots
    ran = {}

    def slots_then_topup(channel_id, key=None, session=None, max_age=None):
        if not ran:
            ran["topup"] = world.run(
                now=datetime(2036, 11, 3, 21, 30, tzinfo=timezone.utc))
        return real_slots(channel_id, key=key, session=session,
                          max_age=max_age)

    monkeypatch.setattr(bc, "scheduled_slots", slots_then_topup)
    out = dashboard._scheduled_schedule_job("j", PROFILE, CHAN,
                                            platform=PLATFORM)
    code, s = ran["topup"]
    assert code == 0 and _sched_keys(s) == keys[:2]
    assert out["scheduled"] == 0
    assert len(world.created()) == 2, "only the topup's two posts exist"
    st = world.state()
    assert set(st.rows) == set(keys)
    assert [st.rows[k]["status"] for k in keys] == [cp.SCHEDULED] * 2 + [
        cp.PENDING] * 4


def test_an_overdue_redate_uses_the_overlays_per_day(world):
    # D-028 T1: the automatic re-date reads the overlay's per_day (2), so an
    # overdue queue lands at 2 a day in morning and evening.
    world.overlay["cadence"] = dict(CADENCE, per_day=2)
    keys = world.queue(five_a_day(6))
    now = datetime(2036, 11, 3, 21, 30, tzinfo=timezone.utc)   # 13:30 local
    world.free = 4
    code, s = world.run(now=now)
    assert code == 0 and s["redated"] == 6
    st = world.state()
    assert set(st.rows) == set(keys)
    slots = [(st.rows[k]["row"]["date"], st.rows[k]["row"]["time_window"])
             for k in keys]
    assert slots == [("2036-11-03", "evening"), ("2036-11-04", "morning"),
                     ("2036-11-04", "evening"), ("2036-11-05", "morning"),
                     ("2036-11-05", "evening"), ("2036-11-06", "morning")]
