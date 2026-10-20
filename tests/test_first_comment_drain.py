"""Dispatch 33: the first-comment drain.

comment_pass places a real first comment on a real PUBLISHED row, proven live
on 2026-10-18 - but it was CLI-only, so a row with a first_comment_link just
sat there until someone remembered to run the command. This closes that gap
the way post_drain.py closed "HELD forever", but it is NOT post_drain's
shape underneath: post_drain only ever calls Buffer; this drives a real
browser, so it must take the SAME one-browser-task-per-profile lock the
comment poster and the scheduler's post job use - a collision with either of
those, not merely wasted API calls, is the failure this exists to prevent.

The load-bearing tests here are not "it runs". They are:

* a PUBLISHED row with a link and no ledger entry is picked up; one without a
  link, or already in the ledger, or not PUBLISHED, is not;
* the delay (plus jitter) is genuinely respected - a row published seconds
  ago does not get commented;
* it NEVER starts a browser session while one is already running for the
  profile - asserted on the lock being consulted and the executor never
  being called, not merely on an empty outcome;
* disabled is really disabled: with no config at all, nothing is ever
  submitted;
* a soft per-row failure (COMMENT_FAILED) is not a drain failure - only an
  exit code outside {0, 1}, or the login-required code, stops the job.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import first_comment_drain as fcd  # noqa: E402

T0 = datetime(2026, 10, 19, 9, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)
        return self.now


class ZeroRng:
    """No jitter: makes the delay deterministic for the tests that need it."""

    def uniform(self, a, b):
        return 0.0


def _make_state(tmp_path, rows):
    """A PipelineState with each (key, fields) written, updated_at overridden.

    ``state.update`` always stamps ``updated_at`` to "now" (real wall clock),
    so a test that wants a specific "published N minutes ago" timestamp must
    set it directly afterwards - mirroring the Dispatch-32 repair's own
    direct-mutation-plus-save approach, not calling update() a second time
    (which would re-stamp it again).
    """
    state = cp.PipelineState(path=str(tmp_path / "scheduled_posts_state.json"))
    for key, fields, updated_at in rows:
        entry = fields.copy()
        entry["updated_at"] = updated_at
        state.rows[key] = entry
    state.save()
    return state


class FakeLedger:
    def __init__(self, posted=None):
        self._posted = set(posted or [])

    def already_posted(self, permalink):
        return permalink in self._posted


# ─── rows_awaiting_first_comment: the eligibility filter ──────────────────────

def test_a_published_row_with_a_link_and_no_ledger_entry_is_picked_up(tmp_path):
    state = _make_state(tmp_path, [
        ("a", {"status": cp.PUBLISHED, "first_comment_link": "https://example.com/x",
               "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
         "2026-10-19T08:00:00+00:00"),
    ])
    ledger = FakeLedger()
    out = cp.rows_awaiting_first_comment(state, ledger)
    assert [k for k, _ in out] == ["a"]


def test_a_published_row_with_no_link_is_not_picked_up(tmp_path):
    state = _make_state(tmp_path, [
        ("a", {"status": cp.PUBLISHED, "first_comment_link": "",
               "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger())
    assert out == []


def test_a_row_already_in_the_ledger_is_not_picked_up(tmp_path):
    permalink = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
    state = _make_state(tmp_path, [
        ("a", {"status": cp.PUBLISHED, "first_comment_link": "https://example.com/x",
               "permalink": permalink},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger(posted=[permalink]))
    assert out == []


def test_a_row_in_a_non_owed_status_is_not_picked_up(tmp_path):
    """SCHEDULED (no permalink yet, by definition) and COMMENTED (already
    done) are the two statuses that must never be offered. COMMENT_FAILED is
    its own pair of tests below - it IS owed, unlike these two."""
    state = _make_state(tmp_path, [
        ("a", {"status": cp.SCHEDULED, "first_comment_link": "https://example.com/x",
               "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
         "2026-10-19T08:00:00+00:00"),
        ("b", {"status": cp.COMMENTED, "first_comment_link": "https://example.com/x",
               "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:2/"},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger())
    assert out == []


def test_a_comment_failed_row_with_a_link_and_no_ledger_entry_is_offered(tmp_path):
    """The gate: a row that failed once must be retried, not stranded until
    someone runs the CLI by hand - comment_pass's own retry set already
    includes COMMENT_FAILED, and the drain must agree."""
    state = _make_state(tmp_path, [
        ("a", {"status": cp.COMMENT_FAILED, "first_comment_link": "https://example.com/x",
               "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
               "errors": ["could not log in"]},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger())
    assert [k for k, _ in out] == ["a"]


def test_a_comment_failed_row_with_a_ledger_entry_is_not_offered(tmp_path):
    """The gate's other half: the ledger - not the status - is the only true
    duplicate guard. A COMMENT_FAILED row that actually posted (e.g. the
    status write and the ledger write raced, or it failed again later for an
    unrelated reason after a successful post) must never be double-commented
    just because its status still reads COMMENT_FAILED."""
    permalink = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
    state = _make_state(tmp_path, [
        ("a", {"status": cp.COMMENT_FAILED, "first_comment_link": "https://example.com/x",
               "permalink": permalink, "errors": ["could not log in"]},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger(posted=[permalink]))
    assert out == []


def test_a_comment_failed_row_with_no_link_is_not_offered(tmp_path):
    state = _make_state(tmp_path, [
        ("a", {"status": cp.COMMENT_FAILED, "first_comment_link": "",
               "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger())
    assert out == []


def test_a_published_row_with_no_permalink_is_not_picked_up(tmp_path):
    """Should not happen past PUBLISHED, but a filter that trusts that is
    trusting the thing it exists to double-check."""
    state = _make_state(tmp_path, [
        ("a", {"status": cp.PUBLISHED, "first_comment_link": "https://example.com/x",
               "permalink": ""},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger())
    assert out == []


def test_picks_the_right_subset_out_of_a_mixed_queue(tmp_path):
    permalink_b = "https://www.linkedin.com/feed/update/urn:li:activity:2/"
    state = _make_state(tmp_path, [
        ("eligible", {"status": cp.PUBLISHED, "first_comment_link": "https://example.com/1",
                     "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
         "2026-10-19T08:00:00+00:00"),
        ("already_commented", {"status": cp.PUBLISHED,
                               "first_comment_link": "https://example.com/2",
                               "permalink": permalink_b},
         "2026-10-19T08:00:00+00:00"),
        ("no_link", {"status": cp.PUBLISHED, "first_comment_link": "",
                     "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:3/"},
         "2026-10-19T08:00:00+00:00"),
        ("still_scheduled", {"status": cp.SCHEDULED, "first_comment_link": "https://example.com/4",
                             "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:4/"},
         "2026-10-19T08:00:00+00:00"),
        ("retrying", {"status": cp.COMMENT_FAILED, "first_comment_link": "https://example.com/5",
                     "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:5/"},
         "2026-10-19T08:00:00+00:00"),
    ])
    out = cp.rows_awaiting_first_comment(state, FakeLedger(posted=[permalink_b]))
    assert sorted(k for k, _ in out) == ["eligible", "retrying"]


# ─── the engine harness ────────────────────────────────────────────────────────

class Harness:
    """A FirstCommentDrain with every collaborator faked."""

    def __init__(self, rows=None, enabled=True, interval=15, delay=5, jitter=0,
                 available=True, rng=None, tmp_path=None):
        self.clock = Clock()
        self.submitted = []
        self.submit_returns = "job1"
        self.available = available
        self.availability_checks = []
        self.rows = rows if rows is not None else []
        self.ledger_posted = set()
        self.read_eligible_calls = 0
        self.cfg = {"scheduled_posting": {"first_comment_drain": {
            "enabled": enabled, "interval_minutes": interval,
            "delay_minutes": delay, "jitter_minutes": jitter}}}

        def state_fn(profile):
            self.read_eligible_calls += 1
            return _FakeState(self.rows)

        def ledger_fn(profile):
            return FakeLedger(posted=self.ledger_posted)

        def browser_available(profile):
            self.availability_checks.append(profile)
            return self.available

        def submit(profile):
            if not self.available:
                return None
            self.submitted.append(profile)
            return self.submit_returns

        self.drain = fcd.FirstCommentDrain(
            now_fn=self.clock,
            rng=rng or ZeroRng(),
            list_profiles=lambda: ["p"],
            config_fn=lambda profile: self.cfg,
            state_fn=state_fn,
            ledger_fn=ledger_fn,
            browser_available=browser_available,
            submit_comment_pass=submit,
        )


class _FakeState:
    """Enough of PipelineState's shape for rows_awaiting_first_comment."""

    def __init__(self, rows):
        # rows: list of (key, fields_dict_including_updated_at)
        self.rows = {k: dict(v) for k, v in rows}


def _row(status=None, link="https://example.com/x",
        permalink="https://www.linkedin.com/feed/update/urn:li:activity:1/",
        updated_at="2026-10-19T08:00:00+00:00"):
    return {"status": status or cp.PUBLISHED, "first_comment_link": link,
            "permalink": permalink, "updated_at": updated_at}


# ─── disabled by default ───────────────────────────────────────────────────────

def test_disabled_by_default_calls_nothing():
    """No config section at all - the shape a fresh profile actually has."""
    h = Harness(rows=[("a", _row(updated_at=T0.isoformat()))])
    h.cfg = {}
    h.drain.tick()
    assert h.submitted == []
    assert h.read_eligible_calls == 0, \
        "a disabled drain must not even read the state file"


def test_explicitly_disabled_calls_nothing():
    h = Harness(rows=[("a", _row(updated_at=T0.isoformat()))], enabled=False)
    h.drain.tick()
    assert h.submitted == []
    assert h.read_eligible_calls == 0


def test_a_merged_config_with_missing_keys_still_defaults_to_off():
    """merge_first_comment_drain_config must not accidentally default enabled
    to True when a profile's section is partial."""
    cfg = fcd.merge_first_comment_drain_config({"interval_minutes": 5})
    assert cfg["enabled"] is False


# ─── the delay (plus jitter) is respected ──────────────────────────────────────

def test_a_row_published_seconds_ago_is_not_commented():
    h = Harness(delay=5, jitter=0)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    h.clock.advance(seconds=10)
    h.drain.tick()
    assert h.submitted == []


def test_once_the_delay_elapses_it_is_commented():
    """run_now, not tick: the interval between CHECKS is a separate concern
    (covered below) from whether a row has cleared its delay once checked."""
    h = Harness(delay=5, jitter=0)
    published_at = h.clock.now
    h.rows = [("a", _row(updated_at=published_at.isoformat()))]
    h.clock.advance(minutes=4, seconds=59)
    h.drain.run_now("p")
    assert h.submitted == [], "one second short of the delay"
    h.clock.advance(seconds=2)
    h.drain.run_now("p")
    assert h.submitted == ["p"]


def test_jitter_extends_the_wait_beyond_the_bare_delay():
    class FixedRng:
        def uniform(self, a, b):
            return b  # always the maximum jitter

    h = Harness(delay=5, jitter=10, rng=FixedRng())
    published_at = h.clock.now
    h.rows = [("a", _row(updated_at=published_at.isoformat()))]
    h.clock.advance(minutes=6)   # past the bare delay, not past delay+jitter
    h.drain.run_now("p")
    assert h.submitted == [], "the bare delay elapsed but jitter had not"
    h.clock.advance(minutes=10)
    h.drain.run_now("p")
    assert h.submitted == ["p"]


def test_an_unreadable_updated_at_is_never_treated_as_ready():
    """Cannot tell must not mean go ahead - the same rule the already-posted
    thread check and the gone-post classifier both use."""
    h = Harness(delay=0, jitter=0)
    h.rows = [("a", _row(updated_at="not-a-timestamp"))]
    h.clock.advance(days=30)
    h.drain.tick()
    assert h.submitted == []


def test_multiple_rows_some_ready_some_not():
    h = Harness(delay=5, jitter=0)
    old = h.clock.now - timedelta(minutes=10)
    new = h.clock.now
    h.rows = [
        ("ready", _row(permalink="https://www.linkedin.com/feed/update/urn:li:activity:1/",
                       updated_at=old.isoformat())),
        ("waiting", _row(permalink="https://www.linkedin.com/feed/update/urn:li:activity:2/",
                         updated_at=new.isoformat())),
    ]
    h.drain.tick()
    assert h.submitted == ["p"], "one ready row is enough to trigger the pass"


def test_a_ready_comment_failed_row_is_picked_up_at_the_engine_level_too():
    """Not just the pure csv_pipeline filter - the engine's own tick/run_now
    path must act on a retried row the same way it acts on a fresh PUBLISHED
    one."""
    h = Harness(delay=0, jitter=0)
    old = h.clock.now - timedelta(minutes=10)
    h.rows = [("retrying", _row(status=cp.COMMENT_FAILED, updated_at=old.isoformat()))]
    h.drain.tick()
    assert h.submitted == ["p"]


def test_result_action_distinguishes_nothing_waiting_from_waiting_on_delay():
    h = Harness(delay=5, jitter=0)
    r1 = h.drain.run_now("p")
    assert r1["action"] == fcd.ACTION_NOTHING_WAITING

    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    r2 = h.drain.run_now("p")
    assert r2["action"] == fcd.ACTION_WAITING_ON_DELAY
    assert r2["waiting"] == 1
    assert r2["ready"] == 0


# ─── the browser lock ──────────────────────────────────────────────────────────

def test_with_a_browser_task_already_running_it_does_not_start_one():
    """Assert on the lock being consulted and the executor never reached -
    never on the outcome alone, which could pass for the wrong reason."""
    h = Harness(delay=0, jitter=0, available=False)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    h.drain.tick()
    assert h.availability_checks == ["p"], "the lock must be consulted"
    assert h.submitted == [], "the executor must never be reached"


def test_the_busy_outcome_is_recorded_distinctly():
    h = Harness(delay=0, jitter=0, available=False)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    result = h.drain.run_now("p")
    assert result["action"] == fcd.ACTION_BUSY


def test_it_resumes_once_the_browser_frees_up():
    h = Harness(delay=0, jitter=0, available=False, interval=15)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    h.drain.tick()
    assert h.submitted == []
    h.available = True
    h.clock.advance(minutes=20)
    h.drain.tick()
    assert h.submitted == ["p"]


def test_a_lock_lost_between_check_and_submit_is_not_an_error():
    """submit_comment_pass returning None (lost the race narrowly) must read
    as busy, not as a crash."""
    h = Harness(delay=0, jitter=0, available=True)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    h.drain._submit_comment_pass = lambda profile: None
    result = h.drain.run_now("p")
    assert result["action"] == fcd.ACTION_BUSY


# ─── a quiet check still answers when asked directly ───────────────────────────

def test_a_manual_check_always_answers_even_when_quiet():
    h = Harness(delay=0, jitter=0)
    result = h.drain.run_now("p")
    assert result["action"] == fcd.ACTION_NOTHING_WAITING
    runs = h.drain.status("p")["recent_runs"]
    assert runs[0]["manual"] is True


def test_run_now_ignores_the_interval():
    h = Harness(delay=0, jitter=0, interval=60)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    h.drain.tick()
    assert h.submitted == ["p"]
    h.drain.run_now("p")
    assert h.submitted == ["p", "p"]


def test_it_waits_out_its_interval_between_checks():
    h = Harness(delay=0, jitter=0, interval=15)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    h.drain.tick()
    assert len(h.submitted) == 1
    h.clock.advance(minutes=5)
    h.drain.tick()
    assert len(h.submitted) == 1, "checked again before the interval elapsed"


# ─── errors ────────────────────────────────────────────────────────────────────

def test_a_blown_up_read_is_recorded_not_raised():
    h = Harness(delay=0, jitter=0)

    def boom(profile):
        raise RuntimeError("state file is corrupt")
    h.drain._state_fn = boom
    h.drain.tick()      # must not raise
    runs = h.drain.status("p")["recent_runs"]
    assert runs[0]["action"] == fcd.ACTION_ERROR
    assert "state file is corrupt" in runs[0]["note"]


def test_one_profile_blowing_up_does_not_stop_the_others():
    h = Harness(delay=0, jitter=0)
    calls = []

    def state_fn(profile):
        calls.append(profile)
        if profile == "bad":
            raise RuntimeError("boom")
        return _FakeState([])
    h.drain._list_profiles = lambda: ["bad", "good"]
    h.drain._state_fn = state_fn
    h.drain.tick()      # must not raise
    assert calls == ["bad", "good"]


# ─── status / config ────────────────────────────────────────────────────────────

def test_status_reports_waiting_count_without_a_tick():
    h = Harness(delay=5, jitter=0)
    h.rows = [("a", _row(updated_at=h.clock.now.isoformat()))]
    s = h.drain.status("p")
    assert s["enabled"] is True
    assert s["waiting_count"] == 1
    assert s["interval_minutes"] == 15
    assert s["delay_minutes"] == 5


def test_toggling_one_profile_does_not_enable_another(monkeypatch):
    saved = {}
    monkeypatch.setattr(fcd.pm, "save_profile_config",
                        lambda name, cfg: saved.__setitem__(name, cfg))
    drain = fcd.FirstCommentDrain(config_fn=lambda p: {})
    drain.toggle("one", True)
    assert fcd.DEFAULT_FIRST_COMMENT_DRAIN["enabled"] is False
    assert fcd.first_comment_drain_config({})["enabled"] is False


def test_toggle_preserves_the_rest_of_the_scheduled_posting_config(monkeypatch):
    saved = {}
    monkeypatch.setattr(fcd.pm, "save_profile_config",
                        lambda name, cfg: saved.__setitem__(name, cfg))
    cfg = {"scheduled_posting": {"buffer_channel_id": "chan1",
                                 "identity_slug": "example-person"}}
    drain = fcd.FirstCommentDrain(config_fn=lambda p: cfg)
    drain.toggle("p", True)
    sp = saved["p"]["scheduled_posting"]
    assert sp["buffer_channel_id"] == "chan1"
    assert sp["identity_slug"] == "example-person"
    assert sp["first_comment_drain"]["enabled"] is True


def test_update_config_persists_delay_and_jitter(monkeypatch):
    saved = {}
    monkeypatch.setattr(fcd.pm, "save_profile_config",
                        lambda name, cfg: saved.__setitem__(name, cfg))
    drain = fcd.FirstCommentDrain(config_fn=lambda p: {})
    out = drain.update_config("p", {"delay_minutes": 20, "jitter_minutes": 1})
    assert out["delay_minutes"] == 20
    assert out["jitter_minutes"] == 1
    fc_cfg = saved["p"]["scheduled_posting"]["first_comment_drain"]
    assert fc_cfg["delay_minutes"] == 20
    assert fc_cfg["jitter_minutes"] == 1


# ─── thread lifecycle ───────────────────────────────────────────────────────────

def test_the_thread_has_its_own_name():
    d = fcd.FirstCommentDrain(list_profiles=lambda: [])
    d.start()
    try:
        assert d._thread.name == "first-comment-drain"
        assert d._thread.daemon is True
    finally:
        d.stop(timeout=2)


def test_start_is_idempotent():
    d = fcd.FirstCommentDrain(list_profiles=lambda: [])
    d.start()
    first = d._thread
    d.start()
    try:
        assert d._thread is first
    finally:
        d.stop(timeout=2)


def test_the_drain_has_its_own_job_category():
    assert fcd.JOB_CATEGORY == "first_comment_drain"


# ─── the dashboard job: soft failure handling ───────────────────────────────────

@pytest.fixture
def clean_jobs():
    dashboard.jobs.clear()
    yield dashboard.jobs
    dashboard.jobs.clear()


@pytest.fixture
def resolved_identity(monkeypatch):
    monkeypatch.setattr(dashboard.pm, "resolve_scheduled",
                        lambda profile, platform: {"identity_slug": "example-person"})


def test_a_comment_failed_row_is_a_successful_job_not_a_crash(
        resolved_identity, monkeypatch, clean_jobs):
    """returncode 1 means some row went COMMENT_FAILED - csv_pipeline.comment_
    pass already recorded that and moved on. The drain must not treat it as a
    crash: a soft per-row failure must never halt the feeder."""
    monkeypatch.setattr(dashboard, "run_subprocess", lambda job_id, cmd: (1, ""))
    out = dashboard._first_comment_drain_job("j", "p")
    assert out == {"returncode": 1}


def test_a_fully_clean_run_is_also_successful(resolved_identity, monkeypatch,
                                               clean_jobs):
    monkeypatch.setattr(dashboard, "run_subprocess", lambda job_id, cmd: (0, ""))
    out = dashboard._first_comment_drain_job("j", "p")
    assert out == {"returncode": 0}


def test_a_login_required_exit_code_raises_a_named_error(resolved_identity,
                                                          monkeypatch, clean_jobs):
    monkeypatch.setattr(dashboard, "run_subprocess",
                        lambda job_id, cmd: (dashboard.pm.EXIT_LOGIN_REQUIRED, ""))
    with pytest.raises(dashboard.pm.LoginRequiredError):
        dashboard._first_comment_drain_job("j", "p")


def test_any_other_exit_code_is_a_real_failure(resolved_identity, monkeypatch,
                                                clean_jobs):
    monkeypatch.setattr(dashboard, "run_subprocess", lambda job_id, cmd: (3, ""))
    with pytest.raises(RuntimeError):
        dashboard._first_comment_drain_job("j", "p")


def test_no_identity_slug_refuses_before_touching_a_browser(monkeypatch,
                                                              clean_jobs):
    monkeypatch.setattr(dashboard.pm, "resolve_scheduled",
                        lambda profile, platform: {"identity_slug": ""})

    def boom(job_id, cmd):
        pytest.fail("a subprocess must never start with no identity_slug")
    monkeypatch.setattr(dashboard, "run_subprocess", boom)
    with pytest.raises(RuntimeError):
        dashboard._first_comment_drain_job("j", "p")


# ─── the dashboard side: collisions ─────────────────────────────────────────────

def test_a_running_browser_job_blocks_the_submit(clean_jobs):
    clean_jobs["sched_post_p_1"] = {
        "status": "running", "profile": "p", "task_type": "browser",
        "category": "scheduled_post"}
    assert dashboard.can_start_browser_task("p") is False
    assert dashboard._first_comment_drain_submit("p") is None


def test_the_drains_own_job_blocks_a_second_browser_task(clean_jobs):
    """A collision with ITSELF - two first-comment passes at once - is just as
    unsafe as colliding with the comment poster."""
    clean_jobs["first_comment_drain_p_1"] = {
        "status": "running", "profile": "p", "task_type": "browser",
        "category": fcd.JOB_CATEGORY}
    assert dashboard.can_start_browser_task("p") is False


def test_the_drain_does_not_block_a_scheduling_task(clean_jobs):
    """It never calls schedule_pass, so it must not be gated by, or gate,
    the Buffer-schedule lock."""
    clean_jobs["first_comment_drain_p_1"] = {
        "status": "running", "profile": "p", "task_type": "browser",
        "category": fcd.JOB_CATEGORY}
    assert dashboard.can_start_scheduling_task("p") is True


def test_a_finished_job_does_not_block_anything(clean_jobs):
    clean_jobs["first_comment_drain_p_1"] = {
        "status": "completed", "profile": "p", "task_type": "browser",
        "category": fcd.JOB_CATEGORY}
    assert dashboard.can_start_browser_task("p") is True


def test_another_profiles_job_does_not_block_this_one(clean_jobs):
    clean_jobs["first_comment_drain_other_1"] = {
        "status": "running", "profile": "other", "task_type": "browser",
        "category": fcd.JOB_CATEGORY}
    assert dashboard.can_start_browser_task("p") is True


def test_the_two_engines_are_separate_objects():
    assert dashboard.first_comment_drain_engine is not dashboard.drain_engine
    assert isinstance(dashboard.first_comment_drain_engine, fcd.FirstCommentDrain)


# ─── the dashboard routes ───────────────────────────────────────────────────────

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cp.pm, "get_data_dir", lambda profile_name=None: str(tmp_path))
    return tmp_path


def test_status_endpoint_answers_without_a_browser(client, empty_state):
    d = client.get("/api/first-comment-drain/p/status").get_json()
    assert d["enabled"] is False
    assert d["waiting_count"] == 0


def test_toggle_refuses_without_an_identity_slug(client, empty_state, monkeypatch):
    monkeypatch.setattr(dashboard.pm, "resolve_scheduled",
                        lambda profile, platform: {"identity_slug": ""})
    r = client.post("/api/first-comment-drain/p/toggle", json={"enabled": True})
    assert r.status_code == 400
    assert "identity_slug" in r.get_json()["error"]


def test_toggle_requires_an_explicit_value(client, empty_state):
    assert client.post("/api/first-comment-drain/p/toggle", json={}).status_code == 400


def test_toggle_on_then_off(client, empty_state, monkeypatch):
    monkeypatch.setattr(dashboard.pm, "resolve_scheduled",
                        lambda profile, platform: {"identity_slug": "example-person"})
    monkeypatch.setattr(dashboard.pm, "save_profile_config", lambda name, cfg: None)
    on = client.post("/api/first-comment-drain/p/toggle", json={"enabled": True})
    assert on.status_code == 200
    assert on.get_json()["drain"]["enabled"] is True
    off = client.post("/api/first-comment-drain/p/toggle", json={"enabled": False})
    assert off.get_json()["drain"]["enabled"] is False


def test_config_endpoint_rejects_a_non_object(client, empty_state):
    assert client.post("/api/first-comment-drain/p/config", json=[1, 2]).status_code == 400


def test_config_endpoint_persists_a_patch(client, empty_state, monkeypatch):
    monkeypatch.setattr(dashboard.pm, "save_profile_config", lambda name, cfg: None)
    r = client.post("/api/first-comment-drain/p/config",
                    json={"delay_minutes": 30})
    assert r.status_code == 200
    assert r.get_json()["drain"]["delay_minutes"] == 30


def test_run_now_endpoint_answers_even_when_quiet(client, empty_state):
    r = client.post("/api/first-comment-drain/p/run-now")
    assert r.status_code == 200
    assert r.get_json()["result"]["action"] == fcd.ACTION_NOTHING_WAITING


# ─── default config: off everywhere ─────────────────────────────────────────────

def test_default_profile_config_ships_the_drain_off():
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "default_profile_config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    section = cfg["scheduled_posting"]["first_comment_drain"]
    assert section["enabled"] is False


# ─── the UI panel ────────────────────────────────────────────────────────────────

def _template():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "linkedin_automation", "templates", "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_panel_has_its_own_named_switch():
    html = _template()
    assert 'id="fcDrainEnabled"' in html
    assert "First-comment drain" in html


def test_the_panel_says_which_feeders_it_is_not():
    html = _template()
    idx = html.index('id="schpFirstCommentDrainCard"')
    section = html[idx:idx + 2000]
    assert "Not the Post drain" in section
    assert "Not the Comment sweeper" in section


def test_the_panel_ui_calls_only_functions_that_exist():
    html = _template()
    for fn in ("loadFirstCommentDrain", "fcDrainToggle", "fcDrainSaveConfig",
              "fcDrainRunNow"):
        assert "function %s(" % fn in html or "async function %s(" % fn in html, fn


def test_the_panel_is_loaded_when_the_section_opens():
    html = _template()
    idx = html.index("if (mode === 'scheduled')")
    assert "loadFirstCommentDrain" in html[idx:idx + 300]


def test_every_panel_element_the_js_touches_exists_in_the_markup():
    html = _template()
    for el in ("fcDrainEnabled", "fcDrainInterval", "fcDrainDelay", "fcDrainJitter",
              "fcDrainRunning", "fcDrainStatus", "fcDrainResult", "fcDrainRuns"):
        assert 'id="%s"' % el in html, el
