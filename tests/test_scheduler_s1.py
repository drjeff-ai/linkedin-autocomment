"""S1: one engine process, honest misses, schedule survives restart, stale
drafts expire.

The clock, RNG, executor and ``state_path_fn`` are injected everywhere; every
file lives under ``tmp_path`` (conftest also points ``DATA_ROOT`` there). No
engine thread is started, no browser, no Buffer.
"""

import glob
import json
import os
import random
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import post_store  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402
from linkedin_automation import scheduler as sched  # noqa: E402

from test_scheduler import Clock, FakeRng, FakeStore, RecordingExecutor, _generated  # noqa: E402

DAY = datetime(2026, 7, 7)


def at(hh, mm=0, day=0):
    return DAY + timedelta(days=day, hours=hh, minutes=mm)


def make_engine(clock, config, *, rng=None, executor=None, state_path_fn=None):
    executor = executor or RecordingExecutor()
    engine = sched.Scheduler(
        now_fn=clock, rng=rng or FakeRng(), min_gap_minutes=90,
        submit_post_job=executor.submit_post,
        submit_scrape_job=executor.submit_scrape,
        submit_cycle_job=executor.submit_cycle,
        submit_connector_job=executor.submit_connector,
        browser_available=executor.browser_available,
        list_profiles=lambda: ["p"],
        config_fn=lambda profile: config,
        state_path_fn=state_path_fn,
    )
    return engine, executor


@pytest.fixture
def fake_store(monkeypatch, tmp_path):
    """Comment files under tmp, and a store with drafts to post."""
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda profile=None, **kw: FakeStore(_generated(10)))


def runs_of(engine, job_type):
    return [r for r in engine.status("p")["recent_runs"] if r["job_type"] == job_type]


# ─── Gate 1: engines start only in the serving process ───────────────────────

def test_should_start_engines_truth_table():
    assert dashboard._should_start_engines(True, {}) is False
    assert dashboard._should_start_engines(True, {"WERKZEUG_RUN_MAIN": "true"}) is True
    assert dashboard._should_start_engines(False, {}) is True
    # Anything but the exact value werkzeug sets is the parent.
    assert dashboard._should_start_engines(True, {"WERKZEUG_RUN_MAIN": "false"}) is False


@pytest.fixture
def stubbed_main(monkeypatch):
    calls = {"scheduler": 0, "drain": 0, "first_comment": 0, "run": []}

    def counter(name):
        def start():
            calls[name] += 1
        return start

    monkeypatch.setattr(dashboard.scheduler_engine, "start", counter("scheduler"))
    monkeypatch.setattr(dashboard.drain_engine, "start", counter("drain"))
    monkeypatch.setattr(dashboard.first_comment_drain_engine, "start",
                        counter("first_comment"))
    monkeypatch.setattr(dashboard.app, "run", lambda **kw: calls["run"].append(kw))
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    return calls


def test_main_in_reloader_parent_starts_no_engine(stubbed_main, monkeypatch):
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    dashboard.main()
    assert (stubbed_main["scheduler"], stubbed_main["drain"],
            stubbed_main["first_comment"]) == (0, 0, 0)
    assert stubbed_main["run"] == [{"debug": True, "port": 6500, "use_reloader": True}]


def test_main_in_serving_child_starts_each_engine_once(stubbed_main, monkeypatch, caplog):
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    with caplog.at_level("INFO", logger=dashboard.logger.name):
        dashboard.main()
    assert (stubbed_main["scheduler"], stubbed_main["drain"],
            stubbed_main["first_comment"]) == (1, 1, 1)
    assert stubbed_main["run"] == [{"debug": True, "port": 6500, "use_reloader": True}]
    assert f"Background engines started in process {os.getpid()}" in caplog.text


def test_status_reports_engine_pid():
    engine, _ = make_engine(Clock(at(8)), {})
    assert engine.status("p")["engine_pid"] == os.getpid()


def test_dashboard_scheduler_persists_beside_the_store():
    path = dashboard.scheduler_engine._state_path_fn("someone")
    assert path == os.path.join(pm.get_data_dir("someone"), "scheduler_state.json")


# ─── Gate 2: the status page never rolls or records what the tick won't run ──

def _both_jobs_cfg(auto_mode):
    return {"scheduler": {
        "enabled": True,
        "auto_mode": auto_mode,
        "post_comments": {"enabled": True, "windows": [["08:00", "11:00"], ["14:00", "17:00"]],
                          "skip_chance": 0.0},
        "scrape": {"enabled": True, "windows": [["09:00", "12:00"]], "skip_chance": 0.0},
        "auto_cycle": {"enabled": True, "windows": [["09:00", "12:00"]], "skip_chance": 0.0},
    }}


def _simulate_48h(engine, clock, start):
    """tick() then status() every 10 minutes for 48 hours."""
    clock.dt = start
    end = start + timedelta(hours=48)
    while clock.dt <= end:
        engine.tick()
        engine.status("p")
        clock.dt += timedelta(minutes=10)


@pytest.mark.parametrize("start", [at(0), at(12, 30)])  # before and after windows
def test_auto_mode_on_status_and_tick_record_nothing_for_suppressed_jobs(fake_store, start):
    clock = Clock(start)
    engine, executor = make_engine(clock, _both_jobs_cfg(True), rng=random.Random(7))
    _simulate_48h(engine, clock, start)

    assert runs_of(engine, "scrape") == []
    assert runs_of(engine, "post_comments") == []
    assert executor.post_calls == [] and executor.scrape_calls == []
    st = engine.status("p")
    for job in ("scrape", "post_comments"):
        assert st["jobs"][job]["paused_by"] == "auto_mode"
        assert all(w["fire_at"] is None and w["status"] == "paused"
                   for w in st["jobs"][job]["windows"])
    # The cycle is what runs instead, and it is not paused.
    assert st["jobs"]["auto_cycle"]["paused_by"] is None
    assert len(executor.cycle_calls) >= 1


@pytest.mark.parametrize("start", [at(0), at(12, 30)])
def test_auto_mode_off_the_same_simulation_fires_both(fake_store, start):
    clock = Clock(start)
    engine, executor = make_engine(clock, _both_jobs_cfg(False), rng=random.Random(7))
    _simulate_48h(engine, clock, start)

    assert executor.post_calls and executor.scrape_calls
    assert any(r["action"] == "fired" for r in runs_of(engine, "post_comments"))
    assert any(r["action"] == "fired" for r in runs_of(engine, "scrape"))
    assert executor.cycle_calls == []           # no auto_mode, no cycle
    st = engine.status("p")
    assert st["jobs"]["scrape"]["paused_by"] is None
    assert st["jobs"]["post_comments"]["paused_by"] is None


def test_status_never_rolls_auto_cycle_without_auto_mode():
    """auto_cycle enabled, auto_mode off: the tick never runs it, so opening
    the page after its window closed must not record a MISSED run for it."""
    cfg = {"scheduler": {"enabled": True, "auto_mode": False,
                         "post_comments": {"enabled": False},
                         "auto_cycle": {"enabled": True, "windows": [["09:00", "12:00"]]}}}
    engine, _ = make_engine(Clock(at(13)), cfg)
    st = engine.status("p")
    assert st["recent_runs"] == []
    assert st["jobs"]["auto_cycle"]["windows"][0]["fire_at"] is None


def test_shared_predicate_agrees_with_the_tick():
    cfg = sched.merge_scheduler_config(_both_jobs_cfg(True)["scheduler"])
    assert [j for j in sched.JOB_TYPES if sched._job_runs_unattended(cfg, j)] == ["auto_cycle"]
    cfg = sched.merge_scheduler_config(_both_jobs_cfg(False)["scheduler"])
    assert [j for j in sched.JOB_TYPES if sched._job_runs_unattended(cfg, j)] == \
        ["post_comments", "scrape"]


# ─── Gate 3: persistence across a restart ────────────────────────────────────

def _persist_cfg():
    return {"scheduler": {
        "enabled": True,
        "post_comments": {"enabled": True, "windows": [["08:00", "11:00"]], "skip_chance": 0.1},
        "scrape": {"enabled": True, "windows": [["09:00", "12:00"]], "skip_chance": 0.1},
    }}


def _one_hour_rng(random_val=0.99):
    """Every roll lands one hour into its window: post_comments 09:00,
    scrape 10:00. A re-draw from ``now`` also lands one hour on."""
    return FakeRng(randint_fn=lambda a, b: min(3600, b), random_val=random_val)


@pytest.fixture
def state_path(tmp_path):
    path = str(tmp_path / "state" / "p" / "scheduler_state.json")
    return path, (lambda profile: path)


def test_restart_does_not_refire_and_carries_runs_and_min_gap(fake_store, state_path, tmp_path):
    path, path_fn = state_path
    clock_a = Clock(at(8, 30))
    a, exec_a = make_engine(clock_a, _persist_cfg(), rng=_one_hour_rng(),
                            state_path_fn=path_fn)
    a.tick()                                    # rolls at 08:30
    clock_a.dt = at(9)
    a.tick()                                    # post_comments fires at 09:00
    assert len(exec_a.post_calls) == 1
    on_disk = json.load(open(path, encoding="utf-8"))
    assert on_disk["last_fire"] == at(9).isoformat()

    # Restart at 09:40. Scheduler B, same file, fresh process state.
    clock_b = Clock(at(9, 40))
    b, exec_b = make_engine(clock_b, _persist_cfg(), rng=_one_hour_rng(),
                            state_path_fn=path_fn)
    for minutes in range(0, 50, 10):            # 09:40 .. 10:20
        clock_b.dt = at(9, 40) + timedelta(minutes=minutes)
        b.tick()
    assert exec_b.post_calls == []              # the 09:00 slot is not fired again
    assert exec_b.scrape_calls == []            # 10:00 slot held back by MIN_GAP
    st = b.status("p")
    assert [r["job_type"] for r in st["recent_runs"]] == ["post_comments"]
    assert st["recent_runs"][0]["fired_at"] == at(9).isoformat()
    assert b._state["p"]["last_fire"] == at(9)

    clock_b.dt = at(10, 30)                     # 90 minutes after A's fire
    b.tick()
    assert len(exec_b.scrape_calls) == 1
    assert exec_b.post_calls == []
    assert glob.glob(str(tmp_path / "**" / "*.tmp"), recursive=True) == []


def test_restart_before_fire_time_keeps_the_rolled_time_and_skip(fake_store, state_path):
    path, path_fn = state_path
    a, _ = make_engine(Clock(at(8, 30)), _persist_cfg(), rng=_one_hour_rng(random_val=0.99),
                       state_path_fn=path_fn)
    a.tick()
    before = a.status("p")["jobs"]["post_comments"]["windows"][0]
    assert before["fire_at"] == at(9).isoformat() and before["will_skip"] is False

    # B's RNG would roll a different time and a skip; it must not roll at all.
    b, _ = make_engine(Clock(at(8, 45)), _persist_cfg(),
                       rng=FakeRng(randint_fn=lambda a, b: 0, random_val=0.0),
                       state_path_fn=path_fn)
    after = b.status("p")["jobs"]["post_comments"]["windows"][0]
    assert after["fire_at"] == before["fire_at"]
    assert after["will_skip"] is False
    assert after["status"] == "pending"


def test_yesterdays_file_rerolls_but_keeps_runs(fake_store, state_path):
    path, path_fn = state_path
    clock = Clock(at(8, 30))
    a, _ = make_engine(clock, _persist_cfg(), rng=_one_hour_rng(), state_path_fn=path_fn)
    a.tick()
    clock.dt = at(9)
    a.tick()

    b, exec_b = make_engine(Clock(at(7, day=1)), _persist_cfg(), rng=_one_hour_rng(),
                            state_path_fn=path_fn)
    st = b.status("p")
    window = st["jobs"]["post_comments"]["windows"][0]
    assert window["fire_at"] == at(9, day=1).isoformat()     # re-rolled for today
    assert window["status"] == "pending"
    assert [r["fired_at"] for r in st["recent_runs"]] == [at(9).isoformat()]


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"slots": [{"job_type": "nope"}]}),
    json.dumps({"slots": "x"}),
    json.dumps({"slots": [], "recent_runs": "x"}),
    json.dumps({"slots": [], "last_fire": "yesterday-ish"}),
])
def test_malformed_state_file_starts_empty(fake_store, state_path, content, caplog):
    path, path_fn = state_path
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    engine, _ = make_engine(Clock(at(8, 30)), _persist_cfg(), rng=_one_hour_rng(),
                            state_path_fn=path_fn)
    st = engine.status("p")                       # no exception
    assert st["recent_runs"] == []
    assert "starting empty" in caplog.text


def test_missing_state_file_starts_empty_and_is_then_written(fake_store, state_path, tmp_path):
    path, path_fn = state_path
    engine, _ = make_engine(Clock(at(8, 30)), _persist_cfg(), rng=_one_hour_rng(),
                            state_path_fn=path_fn)
    assert engine.status("p")["recent_runs"] == []
    on_disk = json.load(open(path, encoding="utf-8"))
    assert {(s["job_type"], s["window_index"]) for s in on_disk["slots"]} == \
        {("post_comments", 0), ("scrape", 0)}
    assert glob.glob(str(tmp_path / "**" / "*.tmp"), recursive=True) == []


def test_no_state_path_means_no_file(fake_store, tmp_path):
    engine, executor = make_engine(Clock(at(8, 30)), _persist_cfg(), rng=_one_hour_rng())
    engine.tick()
    engine.status("p")
    assert glob.glob(str(tmp_path / "**" / "scheduler_state.json"), recursive=True) == []


def test_manual_run_now_is_persisted(fake_store, state_path):
    path, path_fn = state_path
    engine, _ = make_engine(Clock(at(8, 30)), _persist_cfg(), state_path_fn=path_fn)
    engine.run_now("p", "scrape")
    on_disk = json.load(open(path, encoding="utf-8"))
    assert [(r["job_type"], r["manual"]) for r in on_disk["recent_runs"]] == [("scrape", True)]


# ─── Gate 4: roll rules ──────────────────────────────────────────────────────

def _window_cfg():
    return {"scheduler": {"enabled": True,
                          "post_comments": {"enabled": True, "windows": [["09:00", "12:00"]],
                                            "skip_chance": 0.0}}}


@pytest.mark.parametrize("seed", range(20))
def test_fresh_roll_inside_open_window_redraws_after_now(seed):
    engine, executor = make_engine(Clock(at(10, 30)), _window_cfg(), rng=random.Random(seed))
    st = engine.status("p")
    window = st["jobs"]["post_comments"]["windows"][0]
    fire_at = datetime.fromisoformat(window["fire_at"])
    assert window["status"] == "pending"
    assert at(10, 30) <= fire_at <= at(12)
    assert st["recent_runs"] == []


def test_fresh_roll_whose_first_draw_has_passed_still_fires():
    """The first draw lands at 09:00, before now; the re-draw keeps the run."""
    clock = Clock(at(10, 30))
    rng = FakeRng(randint_fn=lambda a, b: 0)          # first draw: window start
    engine, executor = make_engine(clock, _window_cfg(), rng=rng)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(post_store, "load_synced_store",
                   lambda profile=None, **kw: FakeStore(_generated(3)))
        mp.setattr(pm, "get_comments_dir", lambda profile_name=None: os.getcwd())
        mp.setattr(sched, "write_comments_file", lambda *a: "c.txt")
        engine.tick()
    assert len(executor.post_calls) == 1


def test_fresh_roll_after_window_closed_is_missed_with_true_cause():
    engine, executor = make_engine(Clock(at(12, 30)), _window_cfg(), rng=random.Random(1))
    engine.tick()
    st = engine.status("p")
    assert st["jobs"]["post_comments"]["windows"][0]["status"] == "missed"
    assert [(r["action"], r["note"]) for r in st["recent_runs"]] == \
        [("missed", "dashboard started after this window closed")]
    assert executor.post_calls == []


def _loaded_pending(state_path):
    """A state file holding one PENDING slot rolled for 09:00 today."""
    path, path_fn = state_path
    a, _ = make_engine(Clock(at(8, 30)), _window_cfg(),
                       rng=FakeRng(randint_fn=lambda a, b: 0), state_path_fn=path_fn)
    a.status("p")                                 # rolls, persists, never fires
    on_disk = json.load(open(path, encoding="utf-8"))
    assert on_disk["slots"][0]["status"] == "pending"
    assert on_disk["slots"][0]["fire_at"] == at(9).isoformat()
    return path_fn


def test_loaded_pending_slot_past_its_time_fires_while_window_open(fake_store, state_path):
    path_fn = _loaded_pending(state_path)
    b, executor = make_engine(Clock(at(10, 15)), _window_cfg(), state_path_fn=path_fn)
    b.tick()
    assert len(executor.post_calls) == 1
    st = b.status("p")
    assert st["jobs"]["post_comments"]["windows"][0]["status"] == "fired"
    assert [r["action"] for r in st["recent_runs"]] == ["fired"]


def test_loaded_pending_slot_after_window_closed_is_missed(fake_store, state_path):
    path_fn = _loaded_pending(state_path)
    b, executor = make_engine(Clock(at(12, 15)), _window_cfg(), state_path_fn=path_fn)
    b.tick()
    assert executor.post_calls == []
    st = b.status("p")
    assert st["jobs"]["post_comments"]["windows"][0]["status"] == "missed"
    assert [(r["action"], r["note"]) for r in st["recent_runs"]] == \
        [("missed", "dashboard was not running at the scheduled time")]


def test_old_miss_note_is_gone_from_the_code():
    pkg = os.path.dirname(sched.__file__)
    hits = []
    for path in glob.glob(os.path.join(pkg, "**", "*.*"), recursive=True):
        if path.endswith((".py", ".html")):
            with open(path, encoding="utf-8") as f:
                if "dashboard not running at rolled time" in f.read():
                    hits.append(path)
    assert hits == []


# ─── Gate 5: stale drafts ────────────────────────────────────────────────────

NOW = datetime(2026, 7, 7, 12, 0)


def _rec(key, status, *, generated_at=None, updated_at=None, comment="draft", reason=None):
    return {"key": key, "url": key, "author": "A", "text": "t", "category": "c",
            "relevance_score": 50, "status": status, "trash_reason": reason,
            "comment": comment, "comment_meta": {}, "scraped_at": None,
            "generated_at": generated_at, "commented_at": None, "reviewed_at": None,
            "updated_at": updated_at}


def _iso(delta):
    return (NOW - delta).isoformat()


@pytest.fixture
def store(tmp_path):
    s = post_store.PostStore(path=str(tmp_path / "posts_db.json"))
    old = _iso(timedelta(days=30))
    for rec in [
        _rec("https://x/stale", post_store.GENERATED, generated_at=_iso(timedelta(days=3, minutes=1))),
        _rec("https://x/fresh", post_store.GENERATED, generated_at=_iso(timedelta(days=2, hours=23))),
        _rec("https://x/updated-only", post_store.GENERATED, updated_at=_iso(timedelta(days=4))),
        _rec("https://x/no-stamp", post_store.GENERATED),
        _rec("https://x/bad-stamp", post_store.GENERATED, generated_at="last tuesday"),
        _rec("https://x/manual", post_store.TRASH, generated_at=old, reason="manual"),
        _rec("https://x/new", post_store.NEW, generated_at=old, comment=None),
        _rec("https://x/commented", post_store.COMMENTED, generated_at=old),
    ]:
        s.posts[rec["key"]] = rec
    s.save()
    return s


def test_expiry_moves_only_old_generated(store):
    moved = store.expire_stale_generated(3, now=NOW, save=True)
    assert moved == 2
    reloaded = post_store.PostStore(path=store.path)
    status = {k: (r["status"], r["trash_reason"]) for k, r in reloaded.posts.items()}
    assert status == {
        "https://x/stale": ("TRASH", "stale"),
        "https://x/fresh": ("GENERATED", None),
        "https://x/updated-only": ("TRASH", "stale"),
        "https://x/no-stamp": ("GENERATED", None),
        "https://x/bad-stamp": ("GENERATED", None),
        "https://x/manual": ("TRASH", "manual"),
        "https://x/new": ("NEW", None),
        "https://x/commented": ("COMMENTED", None),
    }
    assert reloaded.get("https://x/stale")["comment"] == "draft"


def test_zero_max_age_expires_nothing(store):
    assert store.expire_stale_generated(0, now=NOW) == 0
    assert store.get("https://x/stale")["status"] == "GENERATED"


def test_rescrape_leaves_stale_in_trash_but_lets_auto_trash_back(store):
    store.expire_stale_generated(3, now=NOW)
    store.posts["https://x/lowq"] = _rec("https://x/lowq", post_store.TRASH,
                                         reason=post_store.REASON_LOW_QUALITY, comment=None)
    for url in ("https://x/stale", "https://x/lowq"):
        store.upsert_scraped({"url": url, "author": "A", "text": "t"}, status=post_store.NEW)
    assert (store.get("https://x/stale")["status"],
            store.get("https://x/stale")["trash_reason"]) == ("TRASH", "stale")
    assert store.get("https://x/lowq")["status"] == "NEW"     # auto reason: re-enters
    assert post_store.REASON_STALE not in post_store.AUTO_REASONS


def test_restore_returns_stale_to_generated_with_its_draft(store):
    store.expire_stale_generated(3, now=NOW)
    assert store.restore("https://x/stale") is True
    rec = store.get("https://x/stale")
    assert (rec["status"], rec["trash_reason"], rec["comment"]) == ("GENERATED", None, "draft")
    # A fresh generated_at, so the next expiry does not take it straight back.
    store.expire_stale_generated(3, now=datetime.now())
    assert rec["status"] == "GENERATED"


# Both scheduled post paths expire before selecting: asserted on the file.

@pytest.fixture
def real_store_for(monkeypatch, tmp_path):
    """A real store under tmp with one stale and one fresh draft; the profile's
    config optionally sets draft_max_age_days."""
    def build(profile, max_age=None, now=None):
        now = now or datetime.now()
        s = post_store.PostStore(path=str(tmp_path / f"{profile}_posts_db.json"))
        for url, age in (("https://x/stale", timedelta(days=3, hours=1)),
                         ("https://x/fresh", timedelta(hours=1))):
            stamp = (now - age).isoformat()
            s.posts[url] = _rec(url, post_store.GENERATED,
                                generated_at=stamp, updated_at=stamp)
        s.save()
        monkeypatch.setattr(post_store, "load_synced_store", lambda p=None, **kw: s)
        cfg = {"scheduler": {"enabled": True,
                             "post_comments": {"enabled": True, "windows": [["09:00", "12:00"]],
                                               "count_min": 5, "count_max": 5}}}
        if max_age is not None:
            cfg["scheduler"]["draft_max_age_days"] = max_age
        pm.save_profile_config(profile, cfg)
        return s, cfg
    return build


def _urls_in(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return {u for u in ("https://x/stale", "https://x/fresh") if u in text}


@pytest.mark.parametrize("max_age, expected", [
    (None, {"https://x/fresh"}),                 # default 3 days
    (0, {"https://x/stale", "https://x/fresh"}),  # disabled
])
def test_scheduled_post_comments_expires_before_selecting(real_store_for, max_age, expected):
    now = at(10)
    s, cfg = real_store_for("s1p", max_age, now=now)
    engine, executor = make_engine(Clock(now), cfg)
    engine._config_fn = lambda profile: pm.get_profile_config(profile)
    result = engine._fire_post_comments("s1p", cfg["scheduler"]["post_comments"], now)
    assert result["action"] == "fired"
    assert _urls_in(executor.post_calls[0]["file"]) == expected
    if max_age is None:
        reloaded = post_store.PostStore(path=s.path)
        assert reloaded.get("https://x/stale")["trash_reason"] == "stale"


@pytest.mark.parametrize("max_age, expected", [
    (None, {"https://x/fresh"}),
    (0, {"https://x/stale", "https://x/fresh"}),
])
def test_auto_cycle_post_stage_expires_before_selecting(real_store_for, monkeypatch,
                                                        max_age, expected):
    s, _ = real_store_for("s1c", max_age)
    posted = []
    monkeypatch.setattr(dashboard, "_scrape_job", lambda *a: {"count": 0})
    monkeypatch.setattr(dashboard, "_generate_job", lambda *a: {"generated": 0})
    monkeypatch.setattr(dashboard, "_post_comments_job",
                        lambda job_id, name, f, count: posted.append(f) or {"posted": count})
    monkeypatch.setattr(dashboard, "log_job", lambda *a: None)
    outcome = dashboard._auto_cycle_job("job", "s1c", {"count_min": 5, "count_max": 5})
    assert outcome["ok"] is True
    assert len(posted) == 1
    assert _urls_in(posted[0]) == expected


def test_draft_max_age_days_default_and_bad_values():
    assert sched.DEFAULT_SCHEDULER["draft_max_age_days"] == 3
    assert sched.draft_max_age_days({}) == 3
    assert sched.draft_max_age_days({"scheduler": {"draft_max_age_days": 0}}) == 0
    assert sched.draft_max_age_days({"scheduler": {"draft_max_age_days": "soon"}}) == 3
    assert sched.draft_max_age_days({"scheduler": {"draft_max_age_days": -1}}) == 3


# ─── The dashboard line ──────────────────────────────────────────────────────

def test_template_shows_paused_line_for_auto_mode():
    with open(os.path.join(dashboard._PACKAGE_DIR, "templates", "dashboard.html"),
              encoding="utf-8") as f:
        html = f.read()
    assert "Paused: auto mode runs this as part of the cycle." in html
    assert html.count("paused_by === 'auto_mode' ? SCHED_PAUSED_AUTO_MODE") == 2
    assert "stale:" in html[html.index("const REASON_LABEL"):]
