"""Unit tests for the in-dashboard randomized scheduler (scheduler.py).

The clock, RNG, profile list, config, and the browser-job executor are all
injected, so nothing here touches a real browser, real posting, or real time.
We assert the randomization is *real*: fire times stay inside their window,
counts stay inside their range, skip_chance is honored, and the browser lock /
min-gap / queue-empty edge cases behave as specified.
"""

import copy
import random
from datetime import datetime

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import post_store
from linkedin_automation import scheduler as sched


# ─── Test doubles ─────────────────────────────────────────────────────────────

class Clock:
    """Mutable callable clock."""
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


class FakeRng:
    """Deterministic RNG. ``randint_fn(a, b)`` picks the value; ``random()`` is
    a fixed float (default 0.99 so the default 0.1 skip_chance never skips)."""
    def __init__(self, randint_fn=None, random_val=0.99):
        self._randint_fn = randint_fn or (lambda a, b: a)
        self.random_val = random_val

    def randint(self, a, b):
        return self._randint_fn(a, b)

    def random(self):
        return self.random_val


class RecordingExecutor:
    """Captures every submit_post_job / submit_scrape_job / submit_generate_job
    call. ``seq`` is a single shared counter across all three, so tests can
    assert ORDER between job types (not just each list's own order)."""
    def __init__(self, available=True, return_job=True):
        self.available = available
        self.return_job = return_job
        self.post_calls = []
        self.scrape_calls = []
        self.generate_calls = []
        self._seq = 0

    def _next_seq(self):
        self._seq += 1
        return self._seq

    def submit_post(self, profile, comments_file, count):
        self.post_calls.append({"profile": profile, "file": comments_file,
                                "count": count, "seq": self._next_seq()})
        return f"job_{len(self.post_calls)}" if self.return_job else None

    def submit_scrape(self, profile, max_posts, min_quality):
        self.scrape_calls.append({"profile": profile, "max_posts": max_posts,
                                  "min_quality": min_quality, "seq": self._next_seq()})
        return f"scrape_{len(self.scrape_calls)}" if self.return_job else None

    def submit_generate(self, profile, cap):
        self.generate_calls.append({"profile": profile, "cap": cap,
                                    "seq": self._next_seq()})
        return f"generate_{len(self.generate_calls)}" if self.return_job else None

    def browser_available(self, profile):
        return self.available


class FakeStore:
    """Stand-in for post_store: returns fixed records per status."""
    def __init__(self, generated=None, new=None):
        self._by_status = {
            post_store.GENERATED: list(generated or []),
            post_store.NEW: list(new or []),
        }

    def by_status(self, status):
        return list(self._by_status.get(status, []))


def _generated(n):
    return [{
        "key": f"k{i}", "url": f"https://example.com/{i}", "author": f"Author {i}",
        "text": f"Post body {i}", "category": "AI",
        "comment": f"Nice point {i}, thanks.", "comment_meta": {"word_count": 4},
    } for i in range(n)]


def _new_posts(n):
    return [{"key": f"n{i}", "url": f"https://example.com/new/{i}",
             "author": f"Author {i}", "text": f"New post body {i}"} for i in range(n)]


def _config(scheduler_section):
    """A profile config wrapping just the scheduler section."""
    return {"scheduler": scheduler_section}


def make_scheduler(*, now, rng=None, executor=None, config=None,
                   min_gap_minutes=90, profiles=("p",),
                   auto_mode_failure_threshold=None):
    executor = executor or RecordingExecutor()
    cfg = config if config is not None else {}
    kwargs = {}
    if auto_mode_failure_threshold is not None:
        kwargs["auto_mode_failure_threshold"] = auto_mode_failure_threshold
    return sched.Scheduler(
        now_fn=now, rng=rng or FakeRng(), min_gap_minutes=min_gap_minutes,
        submit_post_job=executor.submit_post,
        submit_scrape_job=executor.submit_scrape,
        submit_generate_job=executor.submit_generate,
        browser_available=executor.browser_available,
        list_profiles=lambda: list(profiles),
        config_fn=lambda profile: cfg,
        **kwargs,
    ), executor


@pytest.fixture
def redirect_store(monkeypatch, tmp_path):
    """Point comment-file writes at tmp and stub the lifecycle store loader."""
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    holder = {"store": FakeStore(_generated(10))}
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda profile=None, **kw: holder["store"])
    return holder


# ─── Config fallback ──────────────────────────────────────────────────────────

def test_config_defaults_when_missing():
    """An empty config falls back to the shipped scheduler defaults."""
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 0, 5)), config={})
    st = engine.status("p")
    assert st["enabled"] is False
    assert st["auto_mode"] is False
    assert st["auto_mode_stopped_reason"] is None
    pc = st["jobs"]["post_comments"]
    assert pc["enabled"] is True
    assert pc["count_min"] == 4 and pc["count_max"] == 8
    assert [w["window"] for w in pc["windows"]] == [["08:00", "11:00"], ["14:00", "17:00"]]
    assert st["jobs"]["scrape"]["enabled"] is False
    gc = st["jobs"]["generate"]
    assert gc["enabled"] is False
    assert gc["max_comments"] == sched.GENERATE_DEFAULT_CAP


def test_partial_config_merges_defaults():
    """A partial scheduler section keeps user keys, fills the rest from defaults."""
    cfg = _config({"enabled": True, "post_comments": {"count_min": 2}})
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 0, 5)), config=cfg)
    st = engine.status("p")
    assert st["enabled"] is True
    pc = st["jobs"]["post_comments"]
    assert pc["count_min"] == 2 and pc["count_max"] == 8  # count_max from default
    assert pc["skip_chance"] == 0.1


# ─── Generate/scrape window overlap warning ───────────────────────────────────
#
# The engine never enforces or depends on scrape finishing before generate -
# window placement only. This warns (never refuses, never reorders) so an
# overlapping edit is visible instead of silent. Assert on the warning, not
# on behavior: these tests never check that anything actually fires in a
# different order.

def _overlap_warnings(caplog):
    return [r for r in caplog.records
            if r.levelname == "WARNING" and "starts before scrape window" in r.getMessage()]


def test_default_windows_do_not_warn(caplog):
    """The shipped defaults (generate 12:30-13:30, scrape 09:00-12:00) never
    trigger the warning."""
    with caplog.at_level("WARNING", logger="linkedin_automation.scheduler"):
        sched.merge_scheduler_config({})
    assert _overlap_warnings(caplog) == []


def test_overlapping_windows_warn_naming_both(caplog):
    """A generate window starting before a scrape window ends warns, by name."""
    section = {
        "generate": {"windows": [["06:00", "07:00"]]},
        "scrape": {"windows": [["06:30", "12:00"]]},
    }
    with caplog.at_level("WARNING", logger="linkedin_automation.scheduler"):
        sched.merge_scheduler_config(section, "p")
    warnings = _overlap_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "p" in message
    assert "06:00" in message and "07:00" in message  # the generate window
    assert "06:30" in message and "12:00" in message  # the scrape window


def test_non_overlapping_custom_windows_do_not_warn(caplog):
    """A custom but still-sequential pair (generate strictly after scrape)
    does not warn."""
    section = {
        "generate": {"windows": [["13:00", "14:00"]]},
        "scrape": {"windows": [["09:00", "12:00"]]},
    }
    with caplog.at_level("WARNING", logger="linkedin_automation.scheduler"):
        sched.merge_scheduler_config(section, "p")
    assert _overlap_warnings(caplog) == []


def test_overlap_warning_does_not_change_the_merged_config(caplog):
    """The warning is observational only - it never refuses or reorders."""
    section = {
        "generate": {"windows": [["06:00", "07:00"]]},
        "scrape": {"windows": [["06:30", "12:00"]]},
    }
    with caplog.at_level("WARNING", logger="linkedin_automation.scheduler"):
        merged = sched.merge_scheduler_config(section, "p")
    assert merged["generate"]["windows"] == [["06:00", "07:00"]]
    assert merged["scrape"]["windows"] == [["06:30", "12:00"]]


def test_status_read_warns_for_an_overlapping_profile(caplog):
    """The warning fires from a real read path (status()), not just a direct
    merge_scheduler_config call."""
    cfg = _config({
        "enabled": True,
        "generate": {"windows": [["06:00", "07:00"]]},
        "scrape": {"windows": [["06:30", "12:00"]]},
    })
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 0, 5)), config=cfg)
    with caplog.at_level("WARNING", logger="linkedin_automation.scheduler"):
        engine.status("p")
    assert _overlap_warnings(caplog)


def test_update_config_warns_immediately_on_an_overlapping_edit(persist, caplog):
    """An edit that introduces the overlap is visible at the moment it is
    made, not only on the next tick/status read."""
    cfg = {"scheduler": {"enabled": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    with caplog.at_level("WARNING", logger="linkedin_automation.scheduler"):
        engine.update_config("p", {
            "generate": {"windows": [["06:00", "07:00"]]},
            "scrape": {"windows": [["06:30", "12:00"]]},
        })
    assert _overlap_warnings(caplog)


# ─── Fire-time rolling stays within window bounds ─────────────────────────────

@pytest.mark.parametrize("seed", range(25))
def test_rolled_time_within_window(seed):
    """The daily-rolled fire time is always inside its window, for any RNG seed."""
    cfg = _config({"enabled": True, "post_comments": {"enabled": True}})
    # Roll at 00:05 so all windows are ahead → PENDING (not MISSED).
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 0, 5)),
                               rng=random.Random(seed), config=cfg)
    st = engine.status("p")
    day = datetime(2026, 7, 7).date()
    for w in st["jobs"]["post_comments"]["windows"]:
        fire = datetime.fromisoformat(w["fire_at"])
        s_h, s_m = map(int, w["window"][0].split(":"))
        e_h, e_m = map(int, w["window"][1].split(":"))
        start = datetime.combine(day, datetime.min.time()).replace(hour=s_h, minute=s_m)
        end = datetime.combine(day, datetime.min.time()).replace(hour=e_h, minute=e_m)
        assert start <= fire <= end


def test_time_rerolls_next_day():
    """A new day re-rolls the fire time (different minute than the day before)."""
    clock = Clock(datetime(2026, 7, 7, 0, 5))
    cfg = _config({"enabled": True, "post_comments": {"enabled": True,
                   "windows": [["08:00", "11:00"]]}})
    engine, _ = make_scheduler(now=clock, rng=random.Random(1), config=cfg)
    day1 = engine.status("p")["jobs"]["post_comments"]["windows"][0]["fire_at"]
    clock.dt = datetime(2026, 7, 8, 0, 5)
    day2 = engine.status("p")["jobs"]["post_comments"]["windows"][0]["fire_at"]
    assert day1 != day2  # re-rolled, not carried over


# ─── Count randomization within range ─────────────────────────────────────────

@pytest.mark.parametrize("seed", range(30))
def test_count_within_range(seed, redirect_store):
    """Each post run's count is within [count_min, count_max] and never exceeds
    the number of queued drafts."""
    cfg = _config({"enabled": True, "post_comments": {
        "enabled": True, "count_min": 4, "count_max": 8}})
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 9, 0)),
                               rng=random.Random(seed), executor=executor, config=cfg)
    engine.run_now("p", "post_comments")
    assert len(executor.post_calls) == 1
    assert 4 <= executor.post_calls[0]["count"] <= 8


def test_count_clamped_to_available(redirect_store):
    """When fewer drafts are queued than the rolled count, only that many post."""
    redirect_store["store"] = FakeStore(_generated(3))
    cfg = _config({"enabled": True, "post_comments": {
        "enabled": True, "count_min": 6, "count_max": 8}})
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 9, 0)),
                               rng=FakeRng(randint_fn=lambda a, b: 7),
                               executor=executor, config=cfg)
    engine.run_now("p", "post_comments")
    assert executor.post_calls[0]["count"] == 3  # clamped from 7 to 3 available


# ─── Skip-chance behavior ─────────────────────────────────────────────────────

def _post_cfg(**over):
    base = {"enabled": True, "windows": [["08:00", "11:00"]],
            "count_min": 4, "count_max": 8, "skip_chance": 0.1}
    base.update(over)
    return _config({"enabled": True, "post_comments": base})


def _fire_via_tick(engine, clock, roll_at, fire_at):
    """Roll a slot at ``roll_at`` (future window) then advance to ``fire_at``."""
    clock.dt = roll_at
    engine.tick()          # rolls the slot (PENDING)
    clock.dt = fire_at
    engine.tick()          # fire time reached


def test_skip_chance_skips_run(redirect_store):
    """random() < skip_chance marks the window SKIPPED and posts nothing."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    # random()=0.0 < 0.1 → skip; randint offset 0 so fire_at = 08:00.
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.0)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor, config=_post_cfg())
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 8, 30))
    assert executor.post_calls == []
    st = engine.status("p")
    assert st["jobs"]["post_comments"]["windows"][0]["status"] == sched.SKIPPED
    assert any(r["action"] == sched.ACTION_SKIPPED for r in st["recent_runs"])


def test_no_skip_fires_run(redirect_store):
    """random() >= skip_chance → the run fires normally."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0 if a == 0 else 5, random_val=0.99)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor, config=_post_cfg())
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 8, 30))
    assert len(executor.post_calls) == 1
    assert executor.post_calls[0]["count"] == 5
    assert engine.status("p")["jobs"]["post_comments"]["windows"][0]["status"] == sched.FIRED


# ─── Queue-empty handling ─────────────────────────────────────────────────────

def test_queue_empty_no_error(redirect_store):
    """An empty GENERATED queue logs/records queue_empty and posts nothing."""
    redirect_store["store"] = FakeStore([])
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 9, 0)),
                               executor=executor, config=_post_cfg())
    result = engine.run_now("p", "post_comments")
    assert result["action"] == sched.ACTION_QUEUE_EMPTY
    assert executor.post_calls == []


def test_queue_empty_still_consumes_window(redirect_store):
    """A queue-empty scheduled window is done for the day (won't retry)."""
    redirect_store["store"] = FakeStore([])
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor, config=_post_cfg())
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 8, 30))
    st = engine.status("p")
    assert st["jobs"]["post_comments"]["windows"][0]["status"] == sched.FIRED
    assert any(r["action"] == sched.ACTION_QUEUE_EMPTY for r in st["recent_runs"])


# ─── Browser-lock interaction ─────────────────────────────────────────────────

def test_scheduled_run_waits_for_browser_lock(redirect_store):
    """A held browser lock defers the run (stays PENDING); it fires once free."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor(available=False)
    rng = FakeRng(randint_fn=lambda a, b: 0 if a == 0 else 5, random_val=0.99)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor, config=_post_cfg())

    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 8, 30))
    assert executor.post_calls == []  # lock held → not submitted
    assert engine.status("p")["jobs"]["post_comments"]["windows"][0]["status"] == sched.PENDING

    executor.available = True
    engine.tick()  # same day, slot still pending, now fires
    assert len(executor.post_calls) == 1


# ─── Minimum gap between runs ─────────────────────────────────────────────────

def test_min_gap_between_runs(redirect_store):
    """Two windows rolling close together never fire within the min gap."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    # Both windows 08:00-09:00; offset 0 → both fire_at 08:00. No skip.
    rng = FakeRng(randint_fn=lambda a, b: 0 if a == 0 else 5, random_val=0.99)
    cfg = _config({"enabled": True, "post_comments": {
        "enabled": True, "windows": [["08:00", "09:00"], ["08:00", "09:00"]],
        "count_min": 4, "count_max": 8, "skip_chance": 0.1}})
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor,
                               min_gap_minutes=90, config=cfg)

    clock.dt = datetime(2026, 7, 7, 7, 0)
    engine.tick()  # rolls both windows
    clock.dt = datetime(2026, 7, 7, 8, 30)
    engine.tick()  # first fires, second deferred by min-gap
    assert len(executor.post_calls) == 1

    clock.dt = datetime(2026, 7, 7, 10, 5)  # >90min after first fire
    engine.tick()
    assert len(executor.post_calls) == 2


# ─── No catch-up for missed windows ───────────────────────────────────────────

def test_no_catchup_when_started_after_window(redirect_store):
    """If the rolled time is already past when first rolled, the window is MISSED
    (never fired late) — no catch-up."""
    clock = Clock(datetime(2026, 7, 7, 12, 0))  # started after both windows' rolls
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)  # fire_at at window start
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor, config=_post_cfg())
    engine.tick()
    assert executor.post_calls == []
    st = engine.status("p")
    assert st["jobs"]["post_comments"]["windows"][0]["status"] == sched.MISSED


# ─── Scrape job ───────────────────────────────────────────────────────────────

def test_scrape_run_now_uses_config_params():
    cfg = _config({"enabled": True, "scrape": {
        "enabled": True, "max_posts": 42, "min_quality": 7}})
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 10, 0)),
                               executor=executor, config=cfg)
    engine.run_now("p", "scrape")
    assert len(executor.scrape_calls) == 1
    call = dict(executor.scrape_calls[0])
    del call["seq"]
    assert call == {"profile": "p", "max_posts": 42, "min_quality": 7}


def test_disabled_job_never_fires(redirect_store):
    """A disabled job type is skipped even when the master switch is on."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    cfg = _config({"enabled": True, "post_comments": {"enabled": False}})
    engine, _ = make_scheduler(now=clock, rng=FakeRng(randint_fn=lambda a, b: 0),
                               executor=executor, config=cfg)
    clock.dt = datetime(2026, 7, 7, 9, 0)
    engine.tick()
    assert executor.post_calls == []


def test_master_disabled_never_fires(redirect_store):
    """Master switch off → nothing fires regardless of per-job enabled flags."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    cfg = _config({"enabled": False, "post_comments": {"enabled": True}})
    engine, _ = make_scheduler(now=clock, rng=FakeRng(randint_fn=lambda a, b: 0),
                               executor=executor, config=cfg)
    clock.dt = datetime(2026, 7, 7, 9, 0)
    engine.tick()
    assert executor.post_calls == []


# ─── Config persistence (toggle / update_config) ──────────────────────────────

@pytest.fixture
def persist(monkeypatch):
    """Capture save_profile_config writes into an in-memory store."""
    saved = {}
    monkeypatch.setattr(pm, "save_profile_config",
                        lambda profile, cfg: saved.__setitem__(profile, cfg))
    return saved


def test_toggle_master_persists(persist):
    cfg = {"scheduler": {"enabled": False}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    engine.toggle("p", "master", True)
    assert persist["p"]["scheduler"]["enabled"] is True


def test_toggle_job_persists(persist):
    cfg = {"scheduler": {"enabled": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    engine.toggle("p", "scrape", True)
    assert persist["p"]["scheduler"]["scrape"]["enabled"] is True


def test_update_config_merges_and_persists(persist):
    cfg = {"scheduler": {"enabled": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    engine.update_config("p", {"post_comments": {"count_max": 12,
                               "windows": [["07:00", "09:00"]]}})
    sect = persist["p"]["scheduler"]["post_comments"]
    assert sect["count_max"] == 12
    assert sect["count_min"] == 4  # default preserved
    assert sect["windows"] == [["07:00", "09:00"]]


# ─── run_now records a manual run ─────────────────────────────────────────────

def test_run_now_records_manual(redirect_store):
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 9, 0)), config=_post_cfg())
    engine.run_now("p", "post_comments")
    runs = engine.status("p")["recent_runs"]
    manual = [r for r in runs if r["manual"]]
    assert len(manual) == 1 and manual[0]["action"] == sched.ACTION_FIRED


# ─── Config isolation between profiles (regression) ───────────────────────────
#
# _deep_merge used to shallow-copy DEFAULT_SCHEDULER, so any section a profile
# did not override stayed the SAME OBJECT as the module-level default. toggle()
# does `sched.setdefault(target, {})["enabled"] = enabled`, which then wrote
# straight through into the defaults for the life of the process. In a
# long-running dashboard serving every profile from one process, enabling scrape
# for one profile silently enabled it for every profile that had never
# configured a scrape section — the scheduler would then drive a browser against
# LinkedIn for accounts nobody switched on. See .dev/AUDIT_student_fork.md.

def test_merged_config_does_not_alias_the_module_defaults():
    """Nested sections must be copies, not references into DEFAULT_SCHEDULER."""
    merged = sched.merge_scheduler_config({})
    for job_type in sched.JOB_TYPES:
        assert merged[job_type] is not sched.DEFAULT_SCHEDULER[job_type], (
            f"{job_type} section is the module default object itself")
    assert merged["scrape"]["windows"] is not sched.DEFAULT_SCHEDULER["scrape"]["windows"]


def test_mutating_a_merged_config_never_touches_the_defaults():
    """The exact write toggle() performs must not reach DEFAULT_SCHEDULER."""
    before = copy.deepcopy(sched.DEFAULT_SCHEDULER)
    merged = sched.merge_scheduler_config({})
    merged.setdefault("scrape", {})["enabled"] = True
    merged["scrape"]["windows"].append(["23:00", "23:30"])
    assert sched.DEFAULT_SCHEDULER == before


def test_toggling_one_profile_does_not_enable_another(monkeypatch):
    """Enabling scrape for profile A must not enable it for profile B.

    The end-to-end version of the bug: two profiles, neither with a scrape
    section, sharing one process — exactly the dashboard's situation.
    """
    before = copy.deepcopy(sched.DEFAULT_SCHEDULER)
    # The fake store must actually persist, so status() reads back what toggle()
    # wrote — otherwise the test passes for the wrong reason.
    configs = {"A": {}, "B": {}}
    monkeypatch.setattr(pm, "save_profile_config",
                        lambda profile, cfg: configs.__setitem__(profile, cfg))

    engine = sched.Scheduler(
        now_fn=Clock(datetime(2026, 7, 7, 9, 0)), rng=FakeRng(),
        submit_post_job=lambda *a, **k: None,
        submit_scrape_job=lambda *a, **k: None,
        browser_available=lambda *a, **k: True,
        list_profiles=lambda: ["A", "B"],
        config_fn=lambda profile: configs[profile],
    )

    engine.toggle("A", "scrape", True)

    assert engine.status("A")["jobs"]["scrape"]["enabled"] is True
    assert engine.status("B")["jobs"]["scrape"]["enabled"] is False, (
        "profile B was never configured for scrape but inherited A's toggle")
    assert sched.DEFAULT_SCHEDULER == before, "the module defaults were mutated"


def test_update_config_does_not_alias_the_caller_patch():
    """A persisted config must not share list/dict objects with the patch."""
    patch = {"scrape": {"windows": [["08:00", "09:00"]]}}
    merged = sched._deep_merge(sched.merge_scheduler_config({}), patch)
    merged["scrape"]["windows"].append(["23:00", "23:30"])
    assert patch["scrape"]["windows"] == [["08:00", "09:00"]]


# ─── Generate job (Dispatch 37) ────────────────────────────────────────────────

def _generate_cfg(**over):
    base = {"enabled": True, "windows": [["12:00", "13:00"]],
            "max_comments": 10, "skip_chance": 0.1}
    base.update(over)
    return _config({"enabled": True, "auto_mode": True, "generate": base})


def test_generate_run_now_uses_config_cap(redirect_store):
    redirect_store["store"] = FakeStore(new=_new_posts(5))
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 12, 30)),
                               executor=executor, config=_generate_cfg(max_comments=3))
    result = engine.run_now("p", "generate")
    assert result["action"] == sched.ACTION_FIRED
    assert executor.generate_calls == [{"profile": "p", "cap": 3, "seq": 1}]


def test_generate_cap_holds_against_a_large_new_bin(redirect_store):
    """A scrape of 60 must not generate more than the configured cap."""
    redirect_store["store"] = FakeStore(new=_new_posts(60))
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 12, 30)),
                               executor=executor, config=_generate_cfg())
    engine.run_now("p", "generate")
    assert executor.generate_calls[0]["cap"] == sched.GENERATE_DEFAULT_CAP == 10


def test_generate_cap_clamped_to_available_new_posts(redirect_store):
    """Fewer NEW posts than the cap means only that many are asked for."""
    redirect_store["store"] = FakeStore(new=_new_posts(4))
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 12, 30)),
                               executor=executor, config=_generate_cfg(max_comments=10))
    engine.run_now("p", "generate")
    assert executor.generate_calls[0]["cap"] == 4


def test_generate_no_new_posts_is_queue_empty(redirect_store):
    redirect_store["store"] = FakeStore(new=[])
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 12, 30)),
                               executor=executor, config=_generate_cfg())
    result = engine.run_now("p", "generate")
    assert result["action"] == sched.ACTION_QUEUE_EMPTY
    assert executor.generate_calls == []


# ─── auto_mode gates generate from the unattended tick loop ──────────────────

def test_auto_mode_off_generate_never_fires_from_tick(redirect_store):
    """Auto mode off: nothing new fires. The generate job is never submitted,
    even though its own enabled flag is on and its window has arrived."""
    redirect_store["store"] = FakeStore(new=_new_posts(5))
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)
    cfg = _config({"enabled": True, "auto_mode": False,
                   "generate": {"enabled": True, "windows": [["12:00", "13:00"]],
                                "max_comments": 10, "skip_chance": 0.1}})
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor, config=cfg)
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 12, 30))
    assert executor.generate_calls == []


def test_auto_mode_on_generate_fires_from_tick(redirect_store):
    """The mirror case: auto_mode on, generate's own window has arrived."""
    redirect_store["store"] = FakeStore(new=_new_posts(5))
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor,
                               config=_generate_cfg())
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 12, 30))
    assert len(executor.generate_calls) == 1


def test_run_now_generate_bypasses_auto_mode(redirect_store):
    """run_now is a manual, supervised action - it ignores auto_mode, the same
    way it already ignores each job's own enabled flag (existing behavior)."""
    redirect_store["store"] = FakeStore(new=_new_posts(5))
    cfg = _config({"enabled": True, "auto_mode": False,
                   "generate": {"enabled": False, "windows": [["12:00", "13:00"]],
                                "max_comments": 10}})
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 12, 30)),
                               executor=executor, config=cfg)
    result = engine.run_now("p", "generate")
    assert result["action"] == sched.ACTION_FIRED
    assert len(executor.generate_calls) == 1


# ─── A full auto_mode cycle fires scrape, then generate, then post, in order ──

def test_auto_mode_cycle_runs_scrape_then_generate_then_post_in_order(redirect_store):
    """One simulated day: scrape's window, then generate's (after it), then
    post_comments' (after that) - each fires in turn, in that order."""
    redirect_store["store"] = FakeStore(new=_new_posts(5), generated=_generated(5))

    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    # offset 0 everywhere: each window fires at its own start time; no skips.
    rng = FakeRng(randint_fn=lambda a, b: 0 if a == 0 else a, random_val=0.99)
    cfg = _config({
        "enabled": True, "auto_mode": True,
        "scrape": {"enabled": True, "windows": [["09:00", "09:30"]],
                   "max_posts": 60, "min_quality": 10, "skip_chance": 0.1},
        "generate": {"enabled": True, "windows": [["12:00", "12:30"]],
                     "max_comments": 10, "skip_chance": 0.1},
        "post_comments": {"enabled": True, "windows": [["15:00", "15:30"]],
                          "count_min": 4, "count_max": 4, "skip_chance": 0.1},
    })
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor,
                               min_gap_minutes=0, config=cfg)

    clock.dt = datetime(2026, 7, 7, 7, 0)
    engine.tick()  # rolls all three windows for today

    clock.dt = datetime(2026, 7, 7, 9, 15)
    engine.tick()  # scrape window has arrived
    clock.dt = datetime(2026, 7, 7, 12, 15)
    engine.tick()  # generate window has arrived
    clock.dt = datetime(2026, 7, 7, 15, 15)
    engine.tick()  # post window has arrived

    assert len(executor.scrape_calls) == 1
    assert len(executor.generate_calls) == 1
    assert len(executor.post_calls) == 1
    assert (executor.scrape_calls[0]["seq"] < executor.generate_calls[0]["seq"]
            < executor.post_calls[0]["seq"])


# ─── toggle("auto_mode", ...) enables/disables the chain in one write ─────────

def test_toggle_auto_mode_on_enables_the_whole_chain(persist):
    cfg = {"scheduler": {"enabled": False, "scrape": {"enabled": False},
                        "generate": {"enabled": False},
                        "post_comments": {"enabled": True}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "auto_mode", True)
    assert sched_out["auto_mode"] is True
    assert sched_out["enabled"] is True
    assert sched_out["scrape"]["enabled"] is True
    assert sched_out["generate"]["enabled"] is True
    assert sched_out["post_comments"]["enabled"] is True
    assert persist["p"]["scheduler"]["auto_mode"] is True


def test_toggle_auto_mode_off_leaves_other_flags_alone(persist):
    cfg = {"scheduler": {"enabled": True, "auto_mode": True,
                        "scrape": {"enabled": True},
                        "post_comments": {"enabled": True}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "auto_mode", False)
    assert sched_out["auto_mode"] is False
    assert sched_out["enabled"] is True  # untouched
    assert sched_out["scrape"]["enabled"] is True  # untouched


def test_toggle_auto_mode_on_clears_a_stale_stopped_reason(persist):
    cfg = {"scheduler": {"enabled": False, "auto_mode": False,
                        "auto_mode_stopped_reason": "generate failed 3 consecutive cycles",
                        "auto_mode_stopped_at": "2026-07-01T00:00:00"}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "auto_mode", True)
    assert "auto_mode_stopped_reason" not in sched_out
    assert "auto_mode_stopped_at" not in sched_out


# ─── The circuit breaker: N consecutive failures disables auto_mode ──────────

def test_generate_failures_disable_auto_mode_after_threshold(persist):
    cfg = {"scheduler": {"enabled": True, "auto_mode": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg, auto_mode_failure_threshold=3)

    engine.report_generate_outcome("p", False, "boom 1")
    assert "p" not in persist  # not yet - only 1 consecutive failure
    engine.report_generate_outcome("p", False, "boom 2")
    assert "p" not in persist  # 2 consecutive
    engine.report_generate_outcome("p", False, "boom 3")

    # The gate: assert on the config WRITE, not the log line.
    assert "p" in persist
    assert persist["p"]["scheduler"]["auto_mode"] is False
    reason = persist["p"]["scheduler"]["auto_mode_stopped_reason"]
    assert "generate" in reason and "3 consecutive cycles" in reason
    assert "boom 3" in reason
    assert persist["p"]["scheduler"]["auto_mode_stopped_at"]


def test_scrape_zero_posts_disables_auto_mode_after_threshold(persist):
    cfg = {"scheduler": {"enabled": True, "auto_mode": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg, auto_mode_failure_threshold=3)

    engine.report_scrape_outcome("p", 0)
    engine.report_scrape_outcome("p", 0)
    assert "p" not in persist
    engine.report_scrape_outcome("p", 0)

    assert persist["p"]["scheduler"]["auto_mode"] is False
    assert "scrape" in persist["p"]["scheduler"]["auto_mode_stopped_reason"]


def test_a_success_resets_the_consecutive_counter(persist):
    """2 failures, then a success, then 2 more failures must NOT trip a
    threshold of 3 - the counter is CONSECUTIVE, not cumulative."""
    cfg = {"scheduler": {"enabled": True, "auto_mode": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg, auto_mode_failure_threshold=3)

    engine.report_generate_outcome("p", False)
    engine.report_generate_outcome("p", False)
    engine.report_generate_outcome("p", True)   # resets to 0
    engine.report_generate_outcome("p", False)
    engine.report_generate_outcome("p", False)
    assert "p" not in persist  # only 2 consecutive since the reset


def test_scrape_and_generate_failures_are_counted_independently(persist):
    """Two generate failures plus two scrape failures must not sum to trip a
    threshold of 3 for either - each job type gets its own counter."""
    cfg = {"scheduler": {"enabled": True, "auto_mode": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg, auto_mode_failure_threshold=3)

    engine.report_generate_outcome("p", False)
    engine.report_generate_outcome("p", False)
    engine.report_scrape_outcome("p", 0)
    engine.report_scrape_outcome("p", 0)
    assert "p" not in persist


def test_report_outcome_is_a_noop_when_auto_mode_is_off(persist):
    """The circuit breaker only polices the unattended chain - with auto_mode
    off, failures are not even counted, let alone written back."""
    cfg = {"scheduler": {"enabled": True, "auto_mode": False}}
    engine = sched.Scheduler(config_fn=lambda p: cfg, auto_mode_failure_threshold=1)

    engine.report_generate_outcome("p", False, "whatever")
    engine.report_scrape_outcome("p", 0)
    assert "p" not in persist


def test_reenabling_auto_mode_resets_the_failure_counters(persist):
    """After a self-disable, re-enabling (the same engine instance - the
    dashboard runs exactly one) must start the counter fresh, not resume
    counting from the streak that just tripped it."""
    cfg_holder = {"cfg": {"scheduler": {"enabled": True, "auto_mode": True}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg_holder["cfg"],
                             auto_mode_failure_threshold=2)

    engine.report_generate_outcome("p", False)
    engine.report_generate_outcome("p", False)  # trips, disables auto_mode
    assert persist["p"]["scheduler"]["auto_mode"] is False

    # config_fn reads back whatever was last persisted, same as the real
    # pm.get_profile_config would after the write above.
    cfg_holder["cfg"] = persist["p"]
    engine.toggle("p", "auto_mode", True)
    persist.clear()  # isolate what happens next from the toggle's own write

    engine.report_generate_outcome("p", False)
    assert "p" not in persist  # one failure after the reset, not tripped again
