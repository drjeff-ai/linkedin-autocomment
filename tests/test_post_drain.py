"""Phase B: the post drain feeder.

The drain exists because Buffer's free plan caps scheduled posts org-wide, so a
calendar bigger than the cap leaves correct rows HELD. Phase A stopped those
rows reading as broken; this makes them stop waiting on a person.

The load-bearing tests here are not "it feeds". They are:

* it feeds HELD rows and **never** PENDING ones (a pending row was never
  confirmed against the account it would post as);
* it never runs at the same time as the Schedule button (both call
  ``schedule_pass`` on one state file; overlapping runs could post a row
  twice, and nothing can unpublish it);
* it is not the comment sweeper, and cannot be mistaken for it.
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import post_drain as pd  # noqa: E402
from linkedin_automation import scheduler as sched_mod  # noqa: E402


T0 = datetime(2026, 9, 10, 9, 0, 0)


def _slots(free, limit=10):
    return {"limit": limit, "used": limit - free,
            "used_this_channel": limit - free, "free": free}


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)
        return self.now


class Harness:
    """A drain with every collaborator faked; records what it was asked to do."""

    def __init__(self, held=2, free=3, enabled=True, interval=15,
                 channel="chan1", available=True, slots_raises=None):
        self.clock = Clock()
        self.feeds = []
        self.held = ["row%d" % i for i in range(held)]
        self.slots = _slots(free)
        self.slots_raises = slots_raises
        self.slot_reads = 0
        self.available = available
        self.submit_returns = "job1"
        self.cfg = {"scheduled_posting": {
            "buffer_channel_id": channel,
            "drain": {"enabled": enabled, "interval_minutes": interval}}}

        def slots_fn(cid):
            self.slot_reads += 1
            if self.slots_raises:
                raise self.slots_raises
            return self.slots

        def submit(profile, channel_id, free):
            self.feeds.append({"profile": profile, "channel": channel_id,
                               "free": free})
            return self.submit_returns

        self.drain = pd.PostDrain(
            now_fn=self.clock,
            list_profiles=lambda: ["p"],
            config_fn=lambda profile: self.cfg,
            channel_fn=lambda profile: (self.cfg["scheduled_posting"]
                                        .get("buffer_channel_id")),
            held_fn=lambda profile: list(self.held),
            slots_fn=slots_fn,
            submit_feed=submit,
            scheduling_available=lambda profile: self.available,
        )


# ─── what it feeds ────────────────────────────────────────────────────────────

def test_it_feeds_held_posts_when_slots_are_free():
    h = Harness(held=2, free=3)
    h.drain.tick()
    assert len(h.feeds) == 1
    assert h.feeds[0]["free"] == 3


def test_it_does_nothing_when_nothing_is_held():
    h = Harness(held=0, free=10)
    h.drain.tick()
    assert h.feeds == []


def test_it_does_not_even_ask_buffer_when_nothing_is_held():
    """A drain that polls the API forever for an empty queue is just quota."""
    h = Harness(held=0, free=10)
    h.drain.tick()
    assert h.slot_reads == 0


def test_it_does_nothing_when_no_slot_is_free():
    h = Harness(held=5, free=0)
    h.drain.tick()
    assert h.feeds == []


def test_it_never_asks_for_more_than_buffer_has_room_for():
    h = Harness(held=9, free=2)
    h.drain.tick()
    assert h.feeds[0]["free"] == 2


# ─── HELD only: the safety property ───────────────────────────────────────────

def test_held_rows_selects_held_and_only_held(tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    state.update("a", status=cp.HELD, row={"date": "2036-01-02", "post_text": "h"})
    state.update("b", status=cp.PENDING, row={"date": "2036-01-01", "post_text": "p"})
    state.update("c", status=cp.SCHEDULED, row={"date": "2036-01-03"}, post_id="x")
    state.update("d", status=cp.FAILED, row={"date": "2036-01-04"})

    rows = cp.held_rows(state)
    assert [r.get("post_text") for r in rows] == ["h"]


def test_the_drain_never_schedules_a_pending_row(tmp_path, monkeypatch):
    """A PENDING row has never been confirmed against the acting account.

    Feeding one would mean a background thread publishing to a real LinkedIn
    profile on a timer that nobody approved.
    """
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    state.update("pending_one", status=cp.PENDING,
                 row={"date": "2036-01-01", "time_window": "morning",
                      "post_text": "Never confirmed."})
    created = []
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda *a, **k: created.append(a) or {"id": "x",
                                                              "dueAt": "z"})
    rows = cp.held_rows(state)
    assert rows == []
    cp.schedule_pass(rows, "chan1", state, max_new=10)
    assert created == []
    assert state.get("pending_one")["status"] == cp.PENDING


def test_held_rows_are_ordered_earliest_first(tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    state.update("late", status=cp.HELD,
                 row={"date": "2036-03-01", "post_text": "late"})
    state.update("early", status=cp.HELD,
                 row={"date": "2036-01-01", "post_text": "early"})
    state.update("mid", status=cp.HELD,
                 row={"date": "2036-02-01", "post_text": "mid"})
    assert [r["post_text"] for r in cp.held_rows(state)] == ["early", "mid", "late"]


def test_held_and_pending_share_one_ordering_rule(tmp_path):
    """Two orderings that can drift is two orderings that will."""
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    state.update("b", status=cp.HELD, row={"date": "2036-02-01", "post_text": "b"})
    state.update("a", status=cp.HELD, row={"date": "2036-01-01", "post_text": "a"})
    assert ([r["post_text"] for r in cp.held_rows(state)] ==
            [r["post_text"] for r in cp.pending_rows(state)])


# ─── the interval ─────────────────────────────────────────────────────────────

def test_it_waits_out_its_interval_between_checks():
    h = Harness(held=3, free=3, interval=15)
    h.drain.tick()
    assert len(h.feeds) == 1
    h.clock.advance(minutes=5)
    h.drain.tick()
    assert len(h.feeds) == 1, "it checked again before the interval elapsed"
    h.clock.advance(minutes=11)
    h.drain.tick()
    assert len(h.feeds) == 2


def test_a_disabled_drain_does_nothing_at_all():
    h = Harness(held=5, free=5, enabled=False)
    h.drain.tick()
    assert h.feeds == []
    assert h.slot_reads == 0


def test_a_silly_interval_falls_back_to_something_sane():
    h = Harness(held=1, free=1, interval=0)
    assert h.drain._interval({"interval_minutes": 0}) >= timedelta(minutes=1)
    h.cfg["scheduled_posting"]["drain"]["interval_minutes"] = "nonsense"
    h.drain.tick()   # must not raise
    assert len(h.feeds) == 1


# ─── collisions ───────────────────────────────────────────────────────────────

def test_it_stands_down_while_another_schedule_pass_is_running():
    """THE race: two passes over one state file can post a row twice."""
    h = Harness(held=4, free=4, available=False)
    h.drain.tick()
    assert h.feeds == []


def test_it_does_not_read_buffer_while_another_pass_is_running():
    h = Harness(held=4, free=4, available=False)
    h.drain.tick()
    assert h.slot_reads == 0


def test_it_resumes_once_the_other_pass_finishes():
    h = Harness(held=4, free=4, available=False)
    h.drain.tick()
    assert h.feeds == []
    h.available = True
    h.clock.advance(minutes=20)
    h.drain.tick()
    assert len(h.feeds) == 1


def test_an_executor_that_declines_is_not_recorded_as_a_feed():
    h = Harness(held=4, free=4)
    h.submit_returns = None
    h.drain.tick()
    assert h.drain.status("p")["last_feed_at"] is None


# ─── failures ─────────────────────────────────────────────────────────────────

def test_a_buffer_outage_is_recorded_not_raised():
    h = Harness(held=2, free=2, slots_raises=RuntimeError("Buffer is down"))
    h.drain.tick()          # must not raise
    assert h.feeds == []
    runs = h.drain.status("p")["recent_runs"]
    assert runs[0]["action"] == pd.ACTION_ERROR
    assert "Buffer is down" in runs[0]["note"]


def test_a_missing_channel_is_an_error_not_a_silent_no_op():
    h = Harness(held=2, free=2, channel=None)
    h.drain.tick()
    runs = h.drain.status("p")["recent_runs"]
    assert runs[0]["action"] == pd.ACTION_ERROR
    assert "channel" in runs[0]["note"]


def test_one_profile_blowing_up_does_not_stop_the_others():
    h = Harness(held=1, free=1)
    calls = []

    def held_fn(profile):
        calls.append(profile)
        if profile == "bad":
            raise RuntimeError("state file is corrupt")
        return ["row"]

    h.drain._list_profiles = lambda: ["bad", "good"]
    h.drain._held_fn = held_fn
    h.drain.tick()          # must not raise
    assert calls == ["bad", "good"]


# ─── status ───────────────────────────────────────────────────────────────────

def test_status_says_whether_it_is_on_and_what_is_waiting():
    h = Harness(held=4, free=2)
    s = h.drain.status("p")
    assert s["enabled"] is True
    assert s["held_count"] == 4
    assert s["interval_minutes"] == 15


def test_status_does_not_call_buffer_unless_asked():
    h = Harness(held=4, free=2)
    h.drain.status("p")
    assert h.slot_reads == 0
    h.drain.status("p", include_slots=True)
    assert h.slot_reads == 1


def test_status_reports_the_next_check_after_one_has_happened():
    h = Harness(held=1, free=1, interval=15)
    h.drain.tick()
    s = h.drain.status("p")
    assert s["last_check_at"] is not None
    assert s["next_check_at"] == (T0 + timedelta(minutes=15)).isoformat()


def test_a_feed_shows_up_in_recent_runs_with_its_count():
    h = Harness(held=5, free=2)
    h.drain.tick()
    run = h.drain.status("p")["recent_runs"][0]
    assert run["action"] == pd.ACTION_FED
    assert run["count"] == 2, "it reports what it fed, not what was waiting"
    assert run["held"] == 5
    assert run["free_slots"] == 2


def test_quiet_checks_do_not_crowd_out_real_feeds():
    """Recording every 'nothing held' tick would push real activity off screen."""
    h = Harness(held=2, free=2)
    h.drain.tick()                       # a real feed
    h.held = []
    for _ in range(30):
        h.clock.advance(minutes=20)
        h.drain.tick()                   # quiet
    runs = h.drain.status("p")["recent_runs"]
    assert any(r["action"] == pd.ACTION_FED for r in runs)


def test_a_manual_check_always_answers_even_when_quiet():
    """Someone pressed a button; silence is not an answer."""
    h = Harness(held=0, free=10)
    result = h.drain.run_now("p")
    assert result["action"] == pd.ACTION_NOTHING_HELD
    runs = h.drain.status("p")["recent_runs"]
    assert runs[0]["manual"] is True


def test_run_now_ignores_the_interval():
    h = Harness(held=2, free=2)
    h.drain.tick()
    assert len(h.feeds) == 1
    h.drain.run_now("p")                 # no clock advance
    assert len(h.feeds) == 2


# ─── config ───────────────────────────────────────────────────────────────────

def test_toggling_one_profile_does_not_enable_another(monkeypatch):
    """The scheduler's aliasing bug, which enabled automation for accounts
    nobody switched on. Same shape here, same guard."""
    saved = {}
    monkeypatch.setattr(pd.pm, "save_profile_config",
                        lambda name, cfg: saved.__setitem__(name, cfg))
    drain = pd.PostDrain(config_fn=lambda p: {})
    drain.toggle("one", True)
    assert pd.DEFAULT_DRAIN["enabled"] is False
    assert pd.drain_config({})["enabled"] is False


def test_toggle_preserves_the_rest_of_the_scheduled_posting_config(monkeypatch):
    """Wiping buffer_channel_id on a toggle would silently orphan the profile."""
    saved = {}
    monkeypatch.setattr(pd.pm, "save_profile_config",
                        lambda name, cfg: saved.__setitem__(name, cfg))
    cfg = {"scheduled_posting": {"buffer_channel_id": "chan1",
                                 "identity_slug": "example-person"}}
    drain = pd.PostDrain(config_fn=lambda p: cfg)
    drain.toggle("p", True)
    sp = saved["p"]["scheduled_posting"]
    assert sp["buffer_channel_id"] == "chan1"
    assert sp["identity_slug"] == "example-person"
    assert sp["drain"]["enabled"] is True


def test_enabling_checks_promptly_rather_than_waiting_out_an_interval():
    h = Harness(held=2, free=2, enabled=False, interval=60)
    h.drain.tick()
    assert h.feeds == []
    h.cfg["scheduled_posting"]["drain"]["enabled"] = True
    h.drain._profile_state("p")["last_check"] = h.clock.now   # a stale check
    h.drain.toggle = pd.PostDrain.toggle.__get__(h.drain)
    h.drain._profile_state("p")["last_check"] = None          # what toggle does
    h.drain.tick()
    assert len(h.feeds) == 1


def test_update_config_persists_the_interval(monkeypatch):
    saved = {}
    monkeypatch.setattr(pd.pm, "save_profile_config",
                        lambda name, cfg: saved.__setitem__(name, cfg))
    drain = pd.PostDrain(config_fn=lambda p: {})
    out = drain.update_config("p", {"interval_minutes": 45})
    assert out["interval_minutes"] == 45
    assert saved["p"]["scheduled_posting"]["drain"]["interval_minutes"] == 45


# ─── it is not the comment sweeper ────────────────────────────────────────────

def test_the_drain_is_not_a_scheduler_job_type():
    """Adding it to JOB_TYPES would give it the sweeper's random skip chance -
    and a skipped drain silently leaves a post unscheduled."""
    assert "drain" not in sched_mod.JOB_TYPES
    assert "post_drain" not in sched_mod.JOB_TYPES
    assert sched_mod.JOB_TYPES == ("post_comments", "scrape")


def test_the_two_feeders_use_different_job_categories():
    assert pd.JOB_CATEGORY == "post_drain"
    assert pd.JOB_CATEGORY not in ("scheduled_post", "scheduled_scrape")


def test_the_drain_has_no_skip_chance():
    """A random skip is right for looking human; wrong for filling a slot."""
    assert "skip_chance" not in pd.DEFAULT_DRAIN


def test_the_two_engines_are_separate_objects():
    assert dashboard.drain_engine is not dashboard.scheduler_engine
    assert isinstance(dashboard.drain_engine, pd.PostDrain)
    assert isinstance(dashboard.scheduler_engine, sched_mod.Scheduler)


def test_stopping_one_feeder_does_not_stop_the_other():
    a = pd.PostDrain(list_profiles=lambda: [])
    a.start()
    try:
        assert a.is_running()
        assert not sched_mod.Scheduler(list_profiles=lambda: []).is_running()
    finally:
        a.stop(timeout=2)


def test_the_thread_has_its_own_name():
    """'scheduler' twice in a stack dump is how you debug the wrong feeder."""
    d = pd.PostDrain(list_profiles=lambda: [])
    d.start()
    try:
        assert d._thread.name == "post-drain"
        assert d._thread.daemon is True
    finally:
        d.stop(timeout=2)


def test_start_is_idempotent():
    d = pd.PostDrain(list_profiles=lambda: [])
    d.start()
    first = d._thread
    d.start()
    try:
        assert d._thread is first
    finally:
        d.stop(timeout=2)


# ─── the dashboard side ───────────────────────────────────────────────────────

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(dashboard.pm, "get_profile_config",
                        lambda name=None: {"scheduled_posting": {
                            "buffer_channel_id": "chan1",
                            "identity_slug": "example-person-one"}})
    monkeypatch.setattr(dashboard.pm, "save_profile_config",
                        lambda name, cfg: None)
    monkeypatch.setattr(pd.pm, "save_profile_config", lambda name, cfg: None)


@pytest.fixture
def empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cp.pm, "get_data_dir", lambda profile_name=None: str(tmp_path))
    return tmp_path / "scheduled_posts_state.json"


@pytest.fixture
def clean_jobs():
    dashboard.jobs.clear()
    yield dashboard.jobs
    dashboard.jobs.clear()


def test_status_endpoint_answers_without_touching_buffer(client, configured,
                                                          empty_state, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the polling status line must not call Buffer")
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots", boom)
    d = client.get("/api/drain/p/status").get_json()
    assert d["enabled"] is False
    assert d["held_count"] == 0
    assert d["slots"] is None


def test_toggle_refuses_without_a_buffer_channel(client, empty_state, monkeypatch):
    """A green light on a feeder that cannot feed is worse than no light."""
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    r = client.post("/api/drain/p/toggle", json={"enabled": True})
    assert r.status_code == 400
    assert "buffer_channel_id" in r.get_json()["error"]


def test_toggle_requires_an_explicit_value(client, configured, empty_state):
    assert client.post("/api/drain/p/toggle", json={}).status_code == 400


def test_toggle_on_then_off(client, configured, empty_state):
    on = client.post("/api/drain/p/toggle", json={"enabled": True})
    assert on.status_code == 200
    assert on.get_json()["drain"]["enabled"] is True
    off = client.post("/api/drain/p/toggle", json={"enabled": False})
    assert off.get_json()["drain"]["enabled"] is False


def test_config_endpoint_rejects_a_non_object(client, configured, empty_state):
    assert client.post("/api/drain/p/config", json=[1, 2]).status_code == 400


def test_the_schedule_button_is_refused_while_a_drain_runs(client, configured,
                                                            empty_state,
                                                            clean_jobs):
    """Two schedule passes over one state file is the double-post race."""
    clean_jobs["post_drain_p_1"] = {
        "status": "running", "profile": "p", "task_type": "api",
        "category": pd.JOB_CATEGORY}
    r = client.post("/api/scheduled/p/schedule")
    assert r.status_code == 409
    assert "post the same row twice" in r.get_json()["error"]


def test_the_drain_is_refused_while_the_schedule_button_runs(configured,
                                                              clean_jobs):
    clean_jobs["buffer_schedule_p_1"] = {
        "status": "running", "profile": "p", "task_type": "api",
        "category": "buffer_schedule"}
    assert dashboard.can_start_scheduling_task("p") is False
    assert dashboard._drain_submit_feed("p", "chan1", 3) is None


def test_a_browser_job_does_not_block_the_drain(configured, clean_jobs):
    """The drain never opens a browser, so the sweeper must not gate it."""
    clean_jobs["sched_post_p_1"] = {
        "status": "running", "profile": "p", "task_type": "browser",
        "category": "scheduled_post"}
    assert dashboard.can_start_scheduling_task("p") is True


def test_the_drain_does_not_block_a_browser_job(configured, clean_jobs):
    clean_jobs["post_drain_p_1"] = {
        "status": "running", "profile": "p", "task_type": "api",
        "category": pd.JOB_CATEGORY}
    assert dashboard.can_start_browser_task("p") is True


def test_another_profiles_pass_does_not_block_this_one(configured, clean_jobs):
    clean_jobs["post_drain_other_1"] = {
        "status": "running", "profile": "other", "task_type": "api",
        "category": pd.JOB_CATEGORY}
    assert dashboard.can_start_scheduling_task("p") is True


def test_a_finished_pass_does_not_block_anything(configured, clean_jobs):
    clean_jobs["post_drain_p_1"] = {
        "status": "completed", "profile": "p", "task_type": "api",
        "category": pd.JOB_CATEGORY}
    assert dashboard.can_start_scheduling_task("p") is True


# ─── the feed job itself ──────────────────────────────────────────────────────

def _row(i, date=None, text=None):
    return {"date": date or "2036-11-%02d" % (i + 1), "time_window": "morning",
            "post_text": text or "Held row %d." % i, "topic": "", "tags": "",
            "image_path": "", "first_comment_link": ""}


def _hold(state, row, reason="waiting for a slot"):
    """Hold one row under the key schedule_pass will re-derive for it.

    Rows are keyed by ``row_key(row)``, not by anything the caller picks:
    ``queue_rows`` stores them that way and ``schedule_pass`` recomputes it to
    find the entry again. A fixture that invents its own keys silently tests a
    pass that writes NEW rows and leaves the originals held forever.
    """
    state.update(cp.row_key(row), status=cp.HELD, hold_reason=reason,
                 row=dict(row), text=cp.row_preview(row), errors=[])
    return cp.row_key(row)


def _held_state(tmp_path, n, reason="waiting for a slot"):
    state = cp.PipelineState(path=str(tmp_path / "scheduled_posts_state.json"))
    for i in range(n):
        _hold(state, _row(i), reason)
    return state


def test_the_feed_turns_held_into_scheduled(tmp_path, empty_state, monkeypatch,
                                            clean_jobs):
    _held_state(tmp_path, 3)
    created = []
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda cid, text, img, when, **kw: (
                            created.append(text) or
                            {"id": "post%d" % len(created), "status": "scheduled",
                             "dueAt": when}))
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: _slots(2))
    clean_jobs["j"] = {"log": [], "progress": ""}

    out = dashboard._drain_feed_job("j", "p", "chan1", 2)
    assert out["scheduled"] == 2
    assert out["held"] == 1, "the third had nowhere to go and stayed held"
    assert len(created) == 2

    after = cp.PipelineState(path=str(tmp_path / "scheduled_posts_state.json"))
    assert sum(1 for v in after.rows.values()
               if v["status"] == cp.SCHEDULED) == 2
    assert sum(1 for v in after.rows.values() if v["status"] == cp.HELD) == 1


def test_the_feed_rereads_the_slot_count_rather_than_trusting_the_check(
        tmp_path, empty_state, monkeypatch, clean_jobs):
    """Minutes pass between the check and the feed. Over-sending is the whole
    thing HELD exists to prevent."""
    _held_state(tmp_path, 5)
    created = []
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda cid, text, img, when, **kw: (
                            created.append(text) or {"id": "p", "dueAt": when}))
    # The tick saw 5 free; by now only 1 is.
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: _slots(1))
    clean_jobs["j"] = {"log": [], "progress": ""}

    out = dashboard._drain_feed_job("j", "p", "chan1", 5)
    assert len(created) == 1
    assert out["scheduled"] == 1


def test_the_feed_sends_nothing_when_the_slots_closed_up(tmp_path, empty_state,
                                                          monkeypatch, clean_jobs):
    _held_state(tmp_path, 3)
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda *a, **k: pytest.fail("it sent with no slot free"))
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: _slots(0))
    clean_jobs["j"] = {"log": [], "progress": ""}
    out = dashboard._drain_feed_job("j", "p", "chan1", 4)
    assert out["scheduled"] == 0
    assert out["held"] == 3


def test_a_real_row_error_fails_that_row_and_not_the_batch(tmp_path, empty_state,
                                                            monkeypatch,
                                                            clean_jobs):
    """LimitReached means wait; a broken row means fix it. Never the same."""
    state = _held_state(tmp_path, 1)
    _hold(state, _row(9, date="not-a-date", text="Broken row."))
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda cid, text, img, when, **kw: {"id": "ok",
                                                            "dueAt": when})
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: _slots(5))
    clean_jobs["j"] = {"log": [], "progress": ""}

    out = dashboard._drain_feed_job("j", "p", "chan1", 5)
    assert out["failed"] == 1
    assert out["scheduled"] == 1


def test_buffer_saying_it_is_full_keeps_the_row_held(tmp_path, empty_state,
                                                      monkeypatch, clean_jobs):
    _held_state(tmp_path, 1)

    def at_capacity(*a, **k):
        raise cp.bc.BufferAtCapacity("Scheduled posts limit reached.")
    monkeypatch.setattr(cp.bc, "create_post", at_capacity)
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: _slots(5))
    clean_jobs["j"] = {"log": [], "progress": ""}

    out = dashboard._drain_feed_job("j", "p", "chan1", 5)
    assert out["held"] == 1
    assert out["failed"] == 0


def test_the_feed_never_gives_a_row_a_second_post(tmp_path, empty_state,
                                                   monkeypatch, clean_jobs):
    """The one thing that must never happen, asserted from the drain's side."""
    state = cp.PipelineState(path=str(tmp_path / "scheduled_posts_state.json"))
    key = _hold(state, _row(0))
    # HELD, yet already carrying a post: the shape that must never be re-sent.
    state.update(key, post_id="already-there")
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda *a, **k: pytest.fail("it re-created an existing post"))
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: _slots(5))
    clean_jobs["j"] = {"log": [], "progress": ""}
    out = dashboard._drain_feed_job("j", "p", "chan1", 5)
    assert out["scheduled"] == 0


def test_an_empty_hold_queue_is_a_no_op(tmp_path, empty_state, monkeypatch,
                                        clean_jobs):
    cp.PipelineState(path=str(tmp_path / "scheduled_posts_state.json")).save()
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: pytest.fail("no need to ask Buffer"))
    clean_jobs["j"] = {"log": [], "progress": ""}
    out = dashboard._drain_feed_job("j", "p", "chan1", 5)
    assert out["note"] == "nothing held"


# ─── the UI actually contains the two distinct controls ───────────────────────

def _template():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "linkedin_automation", "templates", "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_both_feeders_have_their_own_named_switch():
    html = _template()
    assert 'id="drainEnabled"' in html
    assert 'id="schedMaster"' in html
    assert "Post drain" in html
    assert "Comment sweeper" in html


def test_each_feeder_says_which_one_it_is_not():
    """The whole point of the labelling: never wonder which is running."""
    html = _template()
    assert html.count("feeder-other") >= 2
    assert "Not the Post drain" in html
    assert "Not the Comment sweeper" in html


def test_the_two_feeders_do_not_share_an_accent_colour():
    html = _template()
    assert ".feeder-comment { --feeder-accent:" in html
    assert ".feeder-drain   { --feeder-accent:" in html


def test_the_drain_card_is_not_a_panel():
    """`.panel` is toggled document-wide by the comment pipeline's step
    switcher; a card wearing it renders blank. This already happened once."""
    html = _template()
    idx = html.index('id="schpDrainCard"')
    card_open = html.rindex("<div", 0, idx)
    assert 'class="schp-card feeder feeder-drain"' in html[card_open:idx + 60]


def test_the_drain_ui_calls_only_functions_that_exist():
    """A handler naming a function that was never defined is a dead control."""
    html = _template()
    for fn in ("loadDrain", "drainToggle", "drainSaveConfig", "drainRunNow",
               "renderDrainRuns", "fmtWhen"):
        assert "function %s(" % fn in html or "async function %s(" % fn in html, fn
    assert "escapeHtml(" not in html, "escapeHtml is not defined in this template"


def test_the_drain_is_loaded_when_the_section_opens():
    html = _template()
    idx = html.index("if (mode === 'scheduled')")
    assert "loadDrain" in html[idx:idx + 200]


def test_every_drain_element_the_js_touches_exists_in_the_markup():
    html = _template()
    for el in ("drainEnabled", "drainInterval", "drainRunning", "drainStatus",
               "drainResult", "drainRuns"):
        assert 'id="%s"' % el in html, el
