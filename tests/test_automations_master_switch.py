"""Dispatch 47: one per-profile switch, ``automations_enabled``, stops every
automation for that profile.

That covers the scheduler's four jobs, the post drain, the first-comment drain,
every engine's run_now, and every dashboard route that starts a job. Each
feature's own flag stays untouched underneath.

What these tests watch is what is SUBMITTED: executor and run_job calls, the
subprocess spawn, the browser factory and the Buffer transport. An outcome
string saying "refused" would not show that nothing left. Every refusal is
asserted next to a profile with the switch on doing the same thing and getting
through, so a check that refuses everyone cannot pass.
"""

import ast
import inspect
import json
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import first_comment_drain as fcd  # noqa: E402
from linkedin_automation import post_drain as pd  # noqa: E402
from linkedin_automation import post_store  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402
from linkedin_automation import scheduler as sched  # noqa: E402

from test_scheduler import Clock, FakeRng, FakeStore, RecordingExecutor, _generated  # noqa: E402

OFF, ON, NOFLAG = "off_profile", "on_profile", "noflag_profile"
# The name the dispatch fixed, spelled out so this file still collects (and
# fails on behaviour, not on import) against code without the switch.
KEY = "automations_enabled"


class RouteStore(FakeStore):
    """FakeStore plus the two reads the generate route makes."""
    def get_posts_by_status(self, status):
        return self.by_status(status)

    def counts(self):
        return {s: len(v) for s, v in self._by_status.items()}


# ─── The helper ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("config, expected", [
    ({}, True),                         # absent: exactly as before the switch
    ({"scheduler": {"enabled": True}}, True),
    ({KEY: True}, True),
    ({KEY: False}, False),
    ({KEY: "false"}, False),            # a hand edit that is not clearly "on"
    ({KEY: "true"}, False),
    ({KEY: 0}, False),
    ({KEY: 1}, False),
    ({KEY: None}, False),
    (None, True),                       # no config at all reads as the default
])
def test_the_helper_is_on_only_when_absent_or_true(config, expected):
    assert pm.automations_enabled(config) is expected


def test_the_key_lives_at_the_top_level_under_the_dispatched_name():
    assert pm.AUTOMATIONS_ENABLED_KEY == KEY


def test_reading_the_switch_creates_nothing_for_an_unregistered_name():
    assert pm.profile_automations_enabled("never_registered") is True
    assert not os.path.exists(os.path.join(pm.DATA_ROOT, "never_registered"))


# ─── The three loops: nothing submitted while off ─────────────────────────────

def _all_jobs_config(auto_mode, flag):
    """Every scheduler job on, all in one window, both drains on."""
    window = {"enabled": True, "windows": [["08:00", "11:00"]], "skip_chance": 0}
    cfg = {
        "scheduler": {
            "enabled": True, "auto_mode": auto_mode,
            "post_comments": dict(window), "scrape": dict(window),
            "auto_cycle": dict(window), "connector": dict(window, pages=2),
        },
        "connector": {"search_urls": ["https://www.linkedin.com/search/results/people/?k=x"]},
        "scheduled_posting": {"drain": {"enabled": True},
                              "first_comment_drain": {"enabled": True,
                                                      "delay_minutes": 0,
                                                      "jitter_minutes": 0}},
    }
    if flag is not None:
        cfg[KEY] = flag
    return cfg


@pytest.fixture
def drafts(monkeypatch, tmp_path):
    """GENERATED drafts for every profile, so post_comments has work to submit."""
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda profile=None, **kw: FakeStore(_generated(5)))


def _submitted_by(executor, profile):
    calls = (executor.post_calls + executor.scrape_calls
             + executor.cycle_calls + executor.connector_calls)
    return [c for c in calls if c["profile"] == profile]


@pytest.mark.parametrize("auto_mode, expected_jobs", [
    # auto_mode off: post_comments, scrape and connector fire; auto_cycle cannot.
    (False, {"post_calls", "scrape_calls", "connector_calls"}),
    # auto_mode on: auto_cycle and connector fire; scrape/post are suppressed.
    (True, {"cycle_calls", "connector_calls"}),
])
def test_scheduler_submits_nothing_for_an_off_profile_and_everything_for_an_on_one(
        drafts, auto_mode, expected_jobs):
    configs = {OFF: _all_jobs_config(auto_mode, False),
               ON: _all_jobs_config(auto_mode, True)}
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    engine = sched.Scheduler(
        now_fn=clock, rng=FakeRng(randint_fn=lambda a, b: 0 if a == 0 else a),
        min_gap_minutes=0,
        submit_post_job=executor.submit_post,
        submit_scrape_job=executor.submit_scrape,
        submit_cycle_job=executor.submit_cycle,
        submit_connector_job=executor.submit_connector,
        browser_available=executor.browser_available,
        list_profiles=lambda: [OFF, ON],
        config_fn=lambda p: configs[p],
    )
    engine.tick()                                   # 07:00: roll, all PENDING
    clock.dt = datetime(2026, 7, 7, 8, 30)
    engine.tick()                                   # 08:30: every window due

    assert _submitted_by(executor, OFF) == []
    on_kinds = {name for name in ("post_calls", "scrape_calls", "cycle_calls",
                                  "connector_calls")
                if any(c["profile"] == ON for c in getattr(executor, name))}
    assert on_kinds == expected_jobs
    # Not even a MISSED or SKIPPED record: the off profile was never evaluated.
    assert engine.status(OFF)["recent_runs"] == []
    assert engine.status(OFF)["automations_enabled"] is False
    assert engine.status(ON)["automations_enabled"] is True


def _drain_pair(configs):
    slot_reads, feeds = [], []

    def slots(channel_id):
        slot_reads.append(channel_id)
        return {"limit": 10, "used": 0, "free": 3}

    engine = pd.PostDrain(
        now_fn=lambda: datetime(2026, 7, 7, 9, 0),
        list_profiles=lambda: [OFF, ON],
        config_fn=lambda p: configs[p],
        channel_fn=lambda p: "chan_" + p,
        held_fn=lambda p: [{"key": p + "_row"}],
        slots_fn=slots,
        submit_feed=lambda p, ch, free: feeds.append(p) or "job_" + p,
    )
    return engine, slot_reads, feeds


def test_post_drain_reads_and_feeds_nothing_for_an_off_profile():
    configs = {OFF: _all_jobs_config(False, False), ON: _all_jobs_config(False, True)}
    engine, slot_reads, feeds = _drain_pair(configs)
    engine.tick()
    assert feeds == [ON]
    assert slot_reads == ["chan_" + ON]             # no Buffer read for OFF either


def _first_comment_pair(monkeypatch, configs):
    published = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    monkeypatch.setattr(csv_pipeline, "rows_awaiting_first_comment",
                        lambda state, ledger: [("row", {"updated_at": published})])
    passes = []
    engine = fcd.FirstCommentDrain(
        list_profiles=lambda: [OFF, ON],
        config_fn=lambda p: configs[p],
        state_fn=lambda p: None, ledger_fn=lambda p: None,
        submit_comment_pass=lambda p: passes.append(p) or "job_" + p,
    )
    return engine, passes


def test_first_comment_drain_submits_nothing_for_an_off_profile(monkeypatch):
    configs = {OFF: _all_jobs_config(False, False), ON: _all_jobs_config(False, True)}
    engine, passes = _first_comment_pair(monkeypatch, configs)
    engine.tick()
    assert passes == [ON]


# ─── Each engine's run_now ────────────────────────────────────────────────────

@pytest.mark.parametrize("job", sched.JOB_TYPES)
def test_scheduler_run_now_submits_nothing_for_an_off_profile(drafts, job):
    configs = {OFF: _all_jobs_config(True, False), ON: _all_jobs_config(True, True)}
    executor = RecordingExecutor()
    engine = sched.Scheduler(
        now_fn=Clock(datetime(2026, 7, 7, 9, 0)), rng=FakeRng(),
        submit_post_job=executor.submit_post,
        submit_scrape_job=executor.submit_scrape,
        submit_cycle_job=executor.submit_cycle,
        submit_connector_job=executor.submit_connector,
        browser_available=executor.browser_available,
        config_fn=lambda p: configs[p],
    )
    refused = engine.run_now(OFF, job)
    engine.run_now(ON, job)
    assert _submitted_by(executor, OFF) == []
    assert len(_submitted_by(executor, ON)) == 1
    assert refused["action"] == pm.AUTOMATIONS_DISABLED_ACTION
    assert "Automations are disabled" in refused["note"]


def test_post_drain_run_now_reads_nothing_for_an_off_profile():
    configs = {OFF: _all_jobs_config(False, False), ON: _all_jobs_config(False, True)}
    engine, slot_reads, feeds = _drain_pair(configs)
    assert engine.run_now(OFF)["action"] == pm.AUTOMATIONS_DISABLED_ACTION
    engine.run_now(ON)
    assert feeds == [ON]
    assert slot_reads == ["chan_" + ON]


def test_first_comment_drain_run_now_submits_nothing_for_an_off_profile(monkeypatch):
    configs = {OFF: _all_jobs_config(False, False), ON: _all_jobs_config(False, True)}
    engine, passes = _first_comment_pair(monkeypatch, configs)
    assert engine.run_now(OFF)["action"] == pm.AUTOMATIONS_DISABLED_ACTION
    engine.run_now(ON)
    assert passes == [ON]


# ─── A profile with no flag behaves as today ─────────────────────────────────

def test_a_profile_without_the_flag_fires_exactly_like_an_explicit_on(drafts, monkeypatch):
    configs = {NOFLAG: _all_jobs_config(False, None), ON: _all_jobs_config(False, True)}
    assert KEY not in configs[NOFLAG]
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    engine = sched.Scheduler(
        now_fn=clock, rng=FakeRng(randint_fn=lambda a, b: 0 if a == 0 else a),
        min_gap_minutes=0,
        submit_post_job=executor.submit_post,
        submit_scrape_job=executor.submit_scrape,
        submit_cycle_job=executor.submit_cycle,
        submit_connector_job=executor.submit_connector,
        browser_available=executor.browser_available,
        list_profiles=lambda: [NOFLAG, ON],
        config_fn=lambda p: configs[p],
    )
    engine.tick()
    clock.dt = datetime(2026, 7, 7, 8, 30)
    engine.tick()

    def shape(profile):
        return sorted((name, c.get("count"), c.get("max_posts"), c.get("pages"))
                      for name in ("post_calls", "scrape_calls", "cycle_calls",
                                   "connector_calls")
                      for c in getattr(executor, name) if c["profile"] == profile)
    assert shape(NOFLAG) == shape(ON) != []

    drain, _, feeds = _drain_pair({OFF: configs[NOFLAG], ON: configs[ON]})
    drain.tick()
    assert sorted(feeds) == [OFF, ON]               # OFF here is the no-flag config
    fc_engine, passes = _first_comment_pair(monkeypatch, {OFF: configs[NOFLAG],
                                                          ON: configs[ON]})
    fc_engine.tick()
    assert sorted(passes) == [OFF, ON]


# ─── Dashboard routes: every manual job route and every Run Now ──────────────

ROW_PUBLISHED = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

#: (method, url template, json body). Each one, with the switch on and the
#: setup in ``outside_world``, reaches run_job.
JOB_ROUTES = [
    ("POST", "/api/posts/{p}/scrape", {}),
    ("POST", "/api/comments/{p}/generate", {}),
    ("POST", "/api/post/{p}", {"comments_file": "hand_built.txt", "count": 1}),
    ("POST", "/api/health/{p}/selectors", {}),
    ("POST", "/api/connector/{p}/start",
     {"search_url": "https://www.linkedin.com/search/results/people/?k=x"}),
    ("POST", "/api/poster/{p}/generate", {"count": 1}),
    ("POST", "/api/poster/{p}/article", {"url": "https://example.com/a"}),
    ("POST", "/api/poster/{p}/post", {}),
    ("POST", "/api/scheduled/{p}/schedule", {}),
    ("POST", "/api/scheduler/{p}/run-now", {"job": "scrape"}),
    ("POST", "/api/drain/{p}/run-now", None),
    ("POST", "/api/first-comment-drain/{p}/run-now", None),
]


@pytest.fixture
def outside_world(monkeypatch):
    """Records everything that would leave the dashboard process: job
    submissions, subprocess spawns, the browser factory and the Buffer
    transport. Also supplies the minimum each route needs to get as far as
    submitting a job, for both profiles alike."""
    seen = {"run_job": [], "popen": [], "create_driver": [], "buffer": []}

    monkeypatch.setattr(dashboard, "run_job", lambda job_id, func, *a, **kw:
                        seen["run_job"].append(kw.get("profile")) or job_id)
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *a, **kw:
                        seen["popen"].append(a) or pytest.fail("subprocess spawned"))
    monkeypatch.setattr(pm, "create_driver", lambda *a, **kw:
                        seen["create_driver"].append(a) or pytest.fail("browser opened"))

    class Transport:
        RequestException = dashboard.buffer_client.requests.RequestException

        @staticmethod
        def post(url, **kwargs):
            seen["buffer"].append(url)
            raise AssertionError("Buffer reached")

    monkeypatch.setattr(dashboard.buffer_client, "requests", Transport)
    registered = {OFF, ON, NOFLAG}
    monkeypatch.setattr(pm, "get_profile",
                        lambda name: {"name": name} if name in registered else None)

    # generate: NEW posts in the store.
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda profile=None, **kw: RouteStore(new=_generated(2)))
    monkeypatch.setattr(dashboard, "_write_lifecycle_input",
                        lambda profile, posts: ("lifecycle_new.json", 2))
    # schedule: a channel and one pending row.
    monkeypatch.setattr(dashboard, "_scheduled_channel_id",
                        lambda profile, platform=None: "chan")
    monkeypatch.setattr(csv_pipeline, "pending_rows", lambda state: [{"key": "row"}])
    # post drain run-now: held rows, a channel, free slots. The slot read is a
    # Buffer call, so it is recorded with the transport's.
    eng = dashboard.drain_engine
    monkeypatch.setattr(eng, "_held_fn", lambda p: [{"key": "row"}])
    monkeypatch.setattr(eng, "_channel_fn", lambda p: "chan")
    monkeypatch.setattr(eng, "_slots_fn", lambda ch: seen["buffer"].append("slots")
                        or {"limit": 10, "used": 0, "free": 1})
    # first-comment drain run-now: one published row past its delay.
    monkeypatch.setattr(csv_pipeline, "rows_awaiting_first_comment",
                        lambda state, ledger: [("row", {"updated_at": ROW_PUBLISHED})])
    fce = dashboard.first_comment_drain_engine
    monkeypatch.setattr(fce, "_state_fn", lambda p: None)
    monkeypatch.setattr(fce, "_ledger_fn", lambda p: None)

    for name, flag in ((OFF, False), (ON, True), (NOFLAG, None)):
        cfg = {"scheduled_posting": {"first_comment_drain": {"delay_minutes": 0,
                                                             "jitter_minutes": 0}}}
        if flag is not None:
            cfg[KEY] = flag
        pm.save_profile_config(name, cfg)
    dashboard.app.config["TESTING"] = True
    return seen


def _call(client, method, url, body, profile):
    url = url.format(p=profile)
    if body is None:
        return client.open(url, method=method)
    return client.open(url, method=method, json=body)


@pytest.mark.parametrize("method, url, body", JOB_ROUTES,
                         ids=[u for _, u, _ in JOB_ROUTES])
def test_every_job_route_refuses_an_off_profile_and_serves_an_on_one(
        outside_world, method, url, body):
    client = dashboard.app.test_client()

    off = _call(client, method, url, body, OFF)
    assert off.status_code == 409
    assert "Automations are disabled for profile 'off_profile'" in off.get_json()["error"]
    assert outside_world == {"run_job": [], "popen": [], "create_driver": [],
                             "buffer": []}

    on = _call(client, method, url, body, ON)
    assert on.status_code == 200, on.get_json()
    assert outside_world["run_job"] == [ON]

    none = _call(client, method, url, body, NOFLAG)
    assert none.status_code == 200, none.get_json()
    assert outside_world["run_job"] == [ON, NOFLAG]


def test_the_refusal_leaves_no_side_effect_behind(outside_world, monkeypatch):
    """The guard is the first statement, so the post route writes no poster
    input file and the connector route does not advance its URL rotation."""
    from linkedin_automation import auto_connector
    written, rotated = [], []
    monkeypatch.setattr(dashboard, "_write_poster_input",
                        lambda *a: written.append(a) or "x.txt")
    monkeypatch.setattr(auto_connector, "resolve_search_url",
                        lambda *a: rotated.append(a) or "https://x")
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda profile=None, **kw: FakeStore(_generated(2)))
    client = dashboard.app.test_client()
    assert client.post("/api/post/%s" % OFF, json={}).status_code == 409
    assert client.post("/api/connector/%s/start" % OFF, json={}).status_code == 409
    assert written == [] and rotated == []


def test_every_route_that_starts_a_job_checks_the_switch_first():
    """Structural: a route added later that calls run_job or an engine's
    run_now without the guard as its first statement fails here, before it
    can ever fire for an off profile."""
    starters = []
    for rule in dashboard.app.url_map.iter_rules():
        view = dashboard.app.view_functions[rule.endpoint]
        src = textwrap.dedent(inspect.getsource(view))
        if "run_job(" not in src and ".run_now(" not in src:
            continue
        starters.append(rule.endpoint)
        body = ast.parse(src).body[0].body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]                         # the docstring
        first, second = body[0], body[1]
        assert (isinstance(first, ast.Assign)
                and isinstance(first.value, ast.Call)
                and getattr(first.value.func, "id", None) == "_automations_refusal"), \
            "%s starts a job without checking automations_enabled first" % rule.endpoint
        # ...and the refusal is actually returned, not computed and dropped.
        assert (isinstance(second, ast.If)
                and isinstance(second.body[0], ast.Return)
                and getattr(second.body[0].value, "id", None) == first.targets[0].id), \
            "%s checks automations_enabled but does not return the refusal" % rule.endpoint
    # The route list above and this scan agree on what a job route is.
    assert len(starters) == len(JOB_ROUTES)


# ─── The switch route: off and back on leaves every flag byte-identical ──────

RICH = {
    "display_name": "Example",
    "scheduler": {
        "enabled": True, "auto_mode": False,
        "post_comments": {"enabled": True, "windows": [["08:00", "11:00"]]},
        "scrape": {"enabled": False, "windows": [["09:00", "12:00"]]},
        "auto_cycle": {"enabled": True},
        "connector": {"enabled": True, "pages": 3},
    },
    "scheduled_posting": {
        "buffer_channel_id": "chan",
        "drain": {"enabled": True, "interval_minutes": 45},
        "first_comment_drain": {"enabled": False, "delay_minutes": 7},
    },
}


def _section_bytes(cfg):
    return {k: json.dumps(cfg[k], indent=2) for k in ("scheduler", "scheduled_posting")}


def test_off_and_back_on_leaves_every_individual_flag_byte_identical(outside_world):
    pm.save_profile_config(ON, RICH)
    path = pm.get_config_path(ON)
    with open(path, "rb") as f:
        original = f.read()
    client = dashboard.app.test_client()

    off = client.post("/api/profiles/%s/automations" % ON, json={"enabled": False})
    assert off.get_json() == {"ok": True, "enabled": False}
    with open(path, "rb") as f:
        while_off = json.loads(f.read())
    assert _section_bytes(while_off) == _section_bytes(RICH)
    assert {k: v for k, v in while_off.items() if k != KEY} == RICH
    # The engines still see every flag as configured, under the master.
    status = client.get("/api/scheduler/%s/status" % ON).get_json()
    assert status["automations_enabled"] is False and status["enabled"] is True

    on = client.post("/api/profiles/%s/automations" % ON, json={"enabled": True})
    assert on.get_json() == {"ok": True, "enabled": True}
    with open(path, "rb") as f:
        after = f.read()
    # The whole file is the original plus the one key, byte for byte (in
    # whatever line ending the platform wrote the original with).
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    expected = json.dumps(dict(RICH, **{KEY: True}), indent=2).encode("utf-8")
    assert after == expected.replace(b"\n", newline)
    assert after.startswith(original[:-(len(newline) + 1)])   # all but the closing "}"
    assert _section_bytes(json.loads(after)) == _section_bytes(RICH)


def test_the_switch_route_rejects_unregistered_names_and_non_booleans(outside_world):
    client = dashboard.app.test_client()
    assert client.post("/api/profiles/ghost/automations",
                       json={"enabled": False}).status_code == 404
    assert not os.path.exists(os.path.join(pm.DATA_ROOT, "ghost"))
    for bad in ("false", 0, None, "off"):
        res = client.post("/api/profiles/%s/automations" % ON, json={"enabled": bad})
        assert res.status_code == 400
    assert pm.profile_automations_enabled(ON) is True
    assert client.get("/api/profiles/%s/automations" % OFF).get_json() == {"enabled": False}


def test_an_unreadable_config_is_not_replaced_by_the_switch(outside_world):
    path = pm.get_config_path(ON)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    res = dashboard.app.test_client().post("/api/profiles/%s/automations" % ON,
                                           json={"enabled": False})
    assert res.status_code == 500
    with open(path, encoding="utf-8") as f:
        assert f.read() == "{ not json"


def test_the_general_config_editor_cannot_turn_the_switch_back_on(outside_world):
    """A stale editor copy posting the whole config back, key included, keeps
    the stored switch as it is; every other field it carries is still saved."""
    client = dashboard.app.test_client()
    stale = dict(pm.get_profile_config(OFF), display_name="Edited", **{KEY: True})
    assert client.post("/api/profiles/%s/config" % OFF, json=stale).status_code == 200
    stored = pm.read_stored_config(OFF)
    assert stored[KEY] is False
    assert stored["display_name"] == "Edited"
    assert pm.profile_automations_enabled(OFF) is False


@pytest.mark.parametrize("profile, flag, expected_stages", [
    (OFF, False, ["scrape"]),                       # turned off mid-scrape
    (ON, True, ["scrape", "generate", "post"]),
])
def test_a_running_cycle_stops_before_its_next_stage_once_switched_off(
        outside_world, monkeypatch, profile, flag, expected_stages):
    """The cycle re-reads the switch between stages. Skipping is not an error,
    so the auto_mode circuit breaker never fires and writes nothing."""
    pm.save_profile_config(profile, dict(RICH, **{KEY: True}))
    stages = []

    def scrape(job_id, name, max_posts, min_quality):
        stages.append("scrape")
        pm.set_automations_enabled(name, flag)      # the user flips the header switch
        return {"count": 3}

    monkeypatch.setattr(dashboard, "_scrape_job", scrape)
    monkeypatch.setattr(dashboard, "_generate_job",
                        lambda job_id, name, cap: stages.append("generate") or {"generated": 1})
    monkeypatch.setattr(dashboard, "_post_comments_job",
                        lambda job_id, name, f, count: stages.append("post") or {"posted": 1})
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda p=None, **kw: RouteStore(_generated(2)))
    monkeypatch.setattr(sched, "write_comments_file", lambda *a: "c.txt")
    outcome = dashboard._auto_cycle_job("job", profile, {})

    assert stages == expected_stages
    # ok is what the circuit breaker (report_cycle_outcome) reads: True means
    # it never disables auto_mode, so no individual flag is written.
    assert outcome["ok"] is True and outcome["failed_stage"] is None
    stored = pm.read_stored_config(profile)
    assert stored[KEY] is flag
    assert {k: v for k, v in stored.items() if k != KEY} == RICH


# ─── The dashboard shows it ──────────────────────────────────────────────────

def test_the_dashboard_renders_the_switch_and_hides_jobs_while_off():
    with open(os.path.join(dashboard._PACKAGE_DIR, "templates", "dashboard.html"),
              encoding="utf-8") as f:
        html = f.read()
    # In the header, so it is on screen whichever tab or profile is shown.
    header = html[html.index("<header>"):html.index("</header>")]
    assert 'id="automationsMaster"' in header
    assert "/api/profiles/${currentProfile}/automations" in html
    # The scheduler panel says so instead of showing schedulable jobs.
    assert 'id="schedAutomationsOff"' in html
    assert "getElementById('schedBody').style.display = automationsOn ? '' : 'none'" in html
    assert 'id="drainAutomationsOff"' in html and 'id="fcDrainAutomationsOff"' in html
    # A profile switch re-reads every automation panel.
    switch = html[html.index("function switchProfile()"):html.index("function applyAutomationsState")]
    for loader in ("loadAutomations()", "loadScheduler()", "loadDrain(false)",
                   "loadFirstCommentDrain()"):
        assert loader in switch
