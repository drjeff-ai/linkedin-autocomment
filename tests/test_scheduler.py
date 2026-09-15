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
    """Captures every submit_post_job / submit_scrape_job call."""
    def __init__(self, available=True, return_job=True):
        self.available = available
        self.return_job = return_job
        self.post_calls = []
        self.scrape_calls = []

    def submit_post(self, profile, comments_file, count):
        self.post_calls.append({"profile": profile, "file": comments_file, "count": count})
        return f"job_{len(self.post_calls)}" if self.return_job else None

    def submit_scrape(self, profile, max_posts, min_quality):
        self.scrape_calls.append({"profile": profile, "max_posts": max_posts,
                                  "min_quality": min_quality})
        return f"scrape_{len(self.scrape_calls)}" if self.return_job else None

    def browser_available(self, profile):
        return self.available


class FakeStore:
    """Stand-in for post_store: returns fixed GENERATED records."""
    def __init__(self, generated):
        self._generated = generated

    def by_status(self, status):
        return list(self._generated) if status == post_store.GENERATED else []


def _generated(n):
    return [{
        "key": f"k{i}", "url": f"https://example.com/{i}", "author": f"Author {i}",
        "text": f"Post body {i}", "category": "AI",
        "comment": f"Nice point {i}, thanks.", "comment_meta": {"word_count": 4},
    } for i in range(n)]


def _config(scheduler_section):
    """A profile config wrapping just the scheduler section."""
    return {"scheduler": scheduler_section}


def make_scheduler(*, now, rng=None, executor=None, config=None,
                   min_gap_minutes=90, profiles=("p",)):
    executor = executor or RecordingExecutor()
    cfg = config if config is not None else {}
    return sched.Scheduler(
        now_fn=now, rng=rng or FakeRng(), min_gap_minutes=min_gap_minutes,
        submit_post_job=executor.submit_post,
        submit_scrape_job=executor.submit_scrape,
        browser_available=executor.browser_available,
        list_profiles=lambda: list(profiles),
        config_fn=lambda profile: cfg,
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
    pc = st["jobs"]["post_comments"]
    assert pc["enabled"] is True
    assert pc["count_min"] == 4 and pc["count_max"] == 8
    assert [w["window"] for w in pc["windows"]] == [["08:00", "11:00"], ["14:00", "17:00"]]
    assert st["jobs"]["scrape"]["enabled"] is False


def test_partial_config_merges_defaults():
    """A partial scheduler section keeps user keys, fills the rest from defaults."""
    cfg = _config({"enabled": True, "post_comments": {"count_min": 2}})
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 0, 5)), config=cfg)
    st = engine.status("p")
    assert st["enabled"] is True
    pc = st["jobs"]["post_comments"]
    assert pc["count_min"] == 2 and pc["count_max"] == 8  # count_max from default
    assert pc["skip_chance"] == 0.1


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
    assert executor.scrape_calls == [{"profile": "p", "max_posts": 42, "min_quality": 7}]


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
