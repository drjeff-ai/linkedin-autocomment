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
from linkedin_automation import auto_connector as ac


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
    """Captures every submit_post_job / submit_scrape_job / submit_cycle_job /
    submit_connector_job call. ``seq`` is a single shared counter across
    post/scrape/cycle, so tests can assert ORDER between those job types
    (not just each list's own order). ``connector_calls`` predates that
    counter and is asserted elsewhere by exact dict equality, so it is left
    without a ``seq`` key rather than widening those assertions."""
    def __init__(self, available=True, return_job=True):
        self.available = available
        self.return_job = return_job
        self.post_calls = []
        self.scrape_calls = []
        self.cycle_calls = []
        self.connector_calls = []
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

    def submit_cycle(self, profile, job_cfg):
        self.cycle_calls.append({"profile": profile, "job_cfg": job_cfg,
                                 "seq": self._next_seq()})
        return f"cycle_{len(self.cycle_calls)}" if self.return_job else None

    def submit_connector(self, profile, pages):
        self.connector_calls.append({"profile": profile, "pages": pages})
        return f"connect_{len(self.connector_calls)}" if self.return_job else None

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

    def expire_stale_generated(self, max_age_days, now=None, save=False):
        # S1: the scheduled post path expires stale drafts before selecting.
        # These records carry no timestamps, so nothing here is stale; the
        # real expiry is tested on a real PostStore in test_scheduler_s1.py.
        return 0


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
        submit_cycle_job=executor.submit_cycle,
        submit_connector_job=executor.submit_connector,
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
    assert st["auto_mode"] is False
    assert st["auto_mode_stopped_reason"] is None
    pc = st["jobs"]["post_comments"]
    assert pc["enabled"] is True
    assert pc["count_min"] == 4 and pc["count_max"] == 8
    assert [w["window"] for w in pc["windows"]] == [["08:00", "11:00"], ["14:00", "17:00"]]
    assert st["jobs"]["scrape"]["enabled"] is False
    cyc = st["jobs"]["auto_cycle"]
    assert cyc["enabled"] is False
    assert cyc["max_comments"] == sched.GENERATE_DEFAULT_CAP
    assert cyc["count_min"] == 4 and cyc["count_max"] == 8
    assert st["jobs"]["connector"]["enabled"] is False
    assert st["jobs"]["connector"]["pages"] == 5


def test_partial_config_merges_defaults():
    """A partial scheduler section keeps user keys, fills the rest from defaults."""
    cfg = _config({"enabled": True, "post_comments": {"count_min": 2}})
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 0, 5)), config=cfg)
    st = engine.status("p")
    assert st["enabled"] is True
    pc = st["jobs"]["post_comments"]
    assert pc["count_min"] == 2 and pc["count_max"] == 8  # count_max from default
    assert pc["skip_chance"] == 0.1


# ─── An old config's leftover scheduler.generate section is inert (Dispatch 40) ─

def test_leftover_generate_section_survives_merge_as_inert_data():
    """A config saved before Dispatch 40 may still carry scheduler.generate.
    merge_scheduler_config must not choke on it, and nothing reads it - it
    just sits in the merged result unused."""
    section = {"generate": {"enabled": True, "windows": [["12:30", "13:30"]]}}
    merged = sched.merge_scheduler_config(section, "p")
    assert merged["generate"]["enabled"] is True  # untouched, but...
    assert "generate" not in sched.JOB_TYPES  # ...nothing iterates it anymore


def test_leftover_generate_section_never_fires(redirect_store):
    """Even enabled with a window that has arrived, the orphaned generate
    section never fires - it is not in JOB_TYPES, so _tick_profile never
    looks at it."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    cfg = _config({"enabled": True,
                   "generate": {"enabled": True, "windows": [["07:00", "07:30"]]}})
    engine, _ = make_scheduler(now=clock, rng=FakeRng(randint_fn=lambda a, b: 0),
                               executor=executor, config=cfg)
    clock.dt = datetime(2026, 7, 7, 7, 15)
    engine.tick()
    assert executor.cycle_calls == []
    assert executor.post_calls == []


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


# ─── Connector job ────────────────────────────────────────────────────────────

@pytest.fixture
def tracker_dir(tmp_path, monkeypatch):
    """Redirect ConnectionTracker's file at tmp, like test_connector_config.py."""
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    return tmp_path


def test_connector_run_now_uses_config_pages(tracker_dir):
    cfg = _config({"enabled": True, "connector": {"enabled": True, "pages": 3}})
    cfg["connector"] = {"search_urls": ["https://search-url"]}
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 10, 0)),
                               executor=executor, config=cfg)
    engine.run_now("p", "connector")
    assert executor.connector_calls == [{"profile": "p", "pages": 3}]


def test_connector_at_daily_cap_does_not_submit(tracker_dir):
    """A profile already at its daily cap: the job is never submitted."""
    tracker = ac.ConnectionTracker("p", weekly_limit=100, daily_limit=5)
    for i in range(5):
        tracker.record_sent(f"n{i}", f"https://www.linkedin.com/in/p{i}/")
    cfg = _config({"enabled": True, "connector": {"enabled": True, "pages": 5}})
    # connector.max_daily_requests must match the tracker's daily_limit above.
    cfg["connector"] = {"max_daily_requests": 5, "weekly_limit": 100,
                        "search_urls": ["https://search-url"]}
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 10, 0)),
                               executor=executor, config=cfg)
    result = engine.run_now("p", "connector")
    assert result["action"] == sched.ACTION_AT_CAP
    assert executor.connector_calls == []  # never reached the browser factory


def test_connector_at_weekly_cap_does_not_submit(tracker_dir):
    """A profile already at its weekly cap (even with daily room): no submit."""
    tracker = ac.ConnectionTracker("p", weekly_limit=3, daily_limit=25)
    for i in range(3):
        tracker.record_sent(f"n{i}", f"https://www.linkedin.com/in/p{i}/")
    cfg = _config({"enabled": True, "connector": {"enabled": True, "pages": 5}})
    cfg["connector"] = {"max_daily_requests": 25, "weekly_limit": 3,
                        "search_urls": ["https://search-url"]}
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 10, 0)),
                               executor=executor, config=cfg)
    result = engine.run_now("p", "connector")
    assert result["action"] == sched.ACTION_AT_CAP
    assert executor.connector_calls == []


def test_connector_under_cap_submits(tracker_dir):
    """Below the cap, the connector job is submitted normally."""
    cfg = _config({"enabled": True, "connector": {"enabled": True, "pages": 5}})
    cfg["connector"] = {"max_daily_requests": 25, "weekly_limit": 100,
                        "search_urls": ["https://search-url"]}
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 10, 0)),
                               executor=executor, config=cfg)
    result = engine.run_now("p", "connector")
    assert result["action"] == sched.ACTION_FIRED
    assert len(executor.connector_calls) == 1


def test_connector_empty_search_urls_does_not_submit(tracker_dir):
    """No search_urls configured: never reaches the cap check or the executor
    (the browser factory), even if the job is enabled and under cap."""
    cfg = _config({"enabled": True, "connector": {"enabled": True, "pages": 5}})
    cfg["connector"] = {"max_daily_requests": 25, "weekly_limit": 100,
                        "search_urls": []}
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 10, 0)),
                               executor=executor, config=cfg)
    result = engine.run_now("p", "connector")
    assert result["action"] == sched.ACTION_QUEUE_EMPTY
    assert executor.connector_calls == []


def test_connector_missing_search_urls_key_does_not_submit(tracker_dir):
    """Same as above, but connector.search_urls is simply absent rather than
    an explicit empty list - the default-merge path."""
    cfg = _config({"enabled": True, "connector": {"enabled": True, "pages": 5}})
    cfg["connector"] = {"max_daily_requests": 25, "weekly_limit": 100}
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 10, 0)),
                               executor=executor, config=cfg)
    result = engine.run_now("p", "connector")
    assert result["action"] == sched.ACTION_QUEUE_EMPTY
    assert executor.connector_calls == []


def test_connector_default_off_never_fires_from_tick(tracker_dir):
    """Connector's own enabled flag defaults False: a tick never fires it."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    cfg = _config({"enabled": True})  # master on; connector section omitted entirely
    engine, _ = make_scheduler(now=clock, rng=FakeRng(randint_fn=lambda a, b: 0),
                               executor=executor, config=cfg)
    clock.dt = datetime(2026, 7, 7, 12, 0)
    engine.tick()
    assert executor.connector_calls == []


def test_toggle_connector_persists(persist):
    cfg = {"scheduler": {"enabled": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    engine.toggle("p", "connector", True)
    assert persist["p"]["scheduler"]["connector"]["enabled"] is True


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


# ─── Auto cycle job (Dispatch 40) ──────────────────────────────────────────────
# auto_mode now drives ONE job type: a single submit_cycle_job call per fire.
# The real scrape->generate->post sequencing is NOT this engine's concern -
# it lives inside run_auto_cycle (tested in isolation below) and the real
# job body it runs in (dashboard._auto_cycle_job, tested in
# tests/test_generate_from_lifecycle.py). These tests only cover the
# engine's own responsibility: firing exactly one async submit, gated the
# same way the old generate job was.

def _cycle_cfg(**over):
    base = {"enabled": True, "windows": [["09:00", "12:00"]],
            "max_posts": 100, "min_quality": 10, "max_comments": 10,
            "count_min": 4, "count_max": 8, "skip_chance": 0.1}
    base.update(over)
    return _config({"enabled": True, "auto_mode": True, "auto_cycle": base})


def test_auto_cycle_run_now_submits_one_job_with_the_config(redirect_store):
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 9, 30)),
                               executor=executor, config=_cycle_cfg(max_comments=3))
    result = engine.run_now("p", "auto_cycle")
    assert result["action"] == sched.ACTION_FIRED
    assert len(executor.cycle_calls) == 1
    assert executor.cycle_calls[0]["job_cfg"]["max_comments"] == 3


# ─── auto_mode gates auto_cycle from the unattended tick loop ────────────────

def test_auto_mode_off_cycle_never_fires_from_tick(redirect_store):
    """Auto mode off: nothing new fires. The cycle job is never submitted,
    even though its own enabled flag is on and its window has arrived."""
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)
    cfg = _config({"enabled": True, "auto_mode": False,
                   "auto_cycle": {"enabled": True, "windows": [["12:00", "13:00"]]}})
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor, config=cfg)
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 12, 30))
    assert executor.cycle_calls == []


def test_auto_mode_on_cycle_fires_from_tick(redirect_store):
    """The mirror case: auto_mode on, the cycle's own window has arrived.

    An empty GENERATED bin (post_comments' default windows are also enabled
    in this merged config) means post_comments' own window resolves to
    queue_empty rather than ACTION_FIRED - so it never consumes the min-gap
    slot and this stays a clean test of auto_cycle's own gating, same shape
    as the pre-Dispatch-40 version of this test.
    """
    redirect_store["store"] = FakeStore([])
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor,
                               config=_cycle_cfg(windows=[["12:00", "13:00"]]))
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 12, 30))
    assert len(executor.cycle_calls) == 1


def test_run_now_cycle_bypasses_auto_mode(redirect_store):
    """run_now is a manual, supervised action - it ignores auto_mode, the same
    way it already ignores each job's own enabled flag (existing behavior)."""
    cfg = _config({"enabled": True, "auto_mode": False,
                   "auto_cycle": {"enabled": False, "windows": [["12:00", "13:00"]]}})
    executor = RecordingExecutor()
    engine, _ = make_scheduler(now=Clock(datetime(2026, 7, 7, 12, 30)),
                               executor=executor, config=cfg)
    result = engine.run_now("p", "auto_cycle")
    assert result["action"] == sched.ACTION_FIRED
    assert len(executor.cycle_calls) == 1


# ─── auto_mode suppresses the standalone scrape/post_comments jobs (Dispatch 41) ─
# Traced before the fix: _tick_profile's only auto_mode gate was on auto_cycle
# itself ("if job_type == 'auto_cycle' and not cfg.get('auto_mode'): continue").
# Nothing stopped scrape/post_comments from being evaluated exactly as if
# auto_mode did not exist - so a profile with auto_mode on AND
# scrape.enabled/post_comments.enabled also true fired the cycle (which
# already runs its own scrape and post stages) AND the two standalone jobs
# from their own independent windows: duplicated work every cycle, not a
# second opinion. Answer: YES, both fired.

def _combined_auto_cfg(**over):
    """auto_mode on, with scrape, post_comments and auto_cycle all
    independently enabled and sharing one window - the exact shape the
    dispatch asks about."""
    base = {
        "enabled": True, "auto_mode": True,
        "scrape": {"enabled": True, "windows": [["12:00", "13:00"]],
                   "max_posts": 50, "min_quality": 5, "skip_chance": 0.1},
        "post_comments": {"enabled": True, "windows": [["12:00", "13:00"]],
                          "count_min": 4, "count_max": 8, "skip_chance": 0.1},
        "auto_cycle": {"enabled": True, "windows": [["12:00", "13:00"]],
                       "max_posts": 100, "min_quality": 10, "max_comments": 10,
                       "count_min": 4, "count_max": 8, "skip_chance": 0.1},
    }
    base.update(over)
    return _config(base)


def test_auto_mode_on_fires_the_cycle_and_not_the_standalone_jobs(redirect_store):
    """Dispatch 41's gate: auto_mode on, with scrape.enabled and
    post_comments.enabled both true, fires the cycle and NOT the standalone
    jobs. Assert on which jobs are submitted. A non-empty GENERATED bin
    proves post_comments would have fired for real (not resolved
    queue_empty) had the suppression not caught it."""
    redirect_store["store"] = FakeStore(_generated(5))
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor,
                               config=_combined_auto_cfg())
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 12, 30))
    assert len(executor.cycle_calls) == 1
    assert executor.scrape_calls == []
    assert executor.post_calls == []


def test_auto_mode_off_restores_the_standalone_jobs(redirect_store, persist):
    """The mirror case: flipping auto_mode back off - without touching
    scrape/post_comments's own config - lets their suppressed windows fire
    again, exactly as the dispatch requires ("Their config values are left
    alone so turning auto mode off restores them")."""
    redirect_store["store"] = FakeStore(_generated(5))
    clock = Clock(datetime(2026, 7, 7, 7, 0))
    executor = RecordingExecutor()
    rng = FakeRng(randint_fn=lambda a, b: 0, random_val=0.99)
    engine, _ = make_scheduler(now=clock, rng=rng, executor=executor,
                               config=_combined_auto_cfg())

    # auto_mode on: suppressed, as proven above.
    _fire_via_tick(engine, clock, datetime(2026, 7, 7, 7, 0), datetime(2026, 7, 7, 12, 30))
    assert executor.scrape_calls == [] and executor.post_calls == []
    assert len(executor.cycle_calls) == 1

    # Flip auto_mode off. scrape/post_comments's own `enabled` flags were
    # never written to by the suppression, so they are still True underneath.
    sched_out = engine.toggle("p", "auto_mode", False)
    assert sched_out["scrape"]["enabled"] is True
    assert sched_out["post_comments"]["enabled"] is True

    # Next day's roll fires their own window again. post_comments is
    # evaluated first in JOB_TYPES order and consumes the shared min-gap
    # slot by firing; scrape, evaluated right after in the same tick, waits
    # out the gap - the same shared-slot mechanics test_min_gap_between_runs
    # exercises in isolation.
    clock.dt = datetime(2026, 7, 8, 7, 0)
    engine.tick()
    clock.dt = datetime(2026, 7, 8, 12, 30)
    engine.tick()
    assert len(executor.post_calls) == 1
    assert executor.scrape_calls == []

    clock.dt = datetime(2026, 7, 8, 14, 1)
    engine.tick()
    assert len(executor.scrape_calls) == 1

    # auto_cycle stayed off (toggling auto_mode off does not force it off,
    # but it also never fires without auto_mode) - it does not pile a second
    # cycle on top of the restored standalone jobs.
    assert len(executor.cycle_calls) == 1


def test_auto_mode_on_suppression_never_writes_to_scrape_or_post_comments_config(persist):
    """The suppression is a tick-loop gate only - toggling auto_mode on must
    not persist any change to scrape/post_comments's own sections."""
    cfg = {"scheduler": {"enabled": True, "scrape": {"enabled": True},
                        "post_comments": {"enabled": True}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "auto_mode", True)
    assert sched_out["scrape"]["enabled"] is True
    assert sched_out["post_comments"]["enabled"] is True
    assert persist["p"]["scheduler"]["scrape"]["enabled"] is True
    assert persist["p"]["scheduler"]["post_comments"]["enabled"] is True


# ─── run_auto_cycle: scrape -> generate -> post, in order, stop on failure ────
# Pure, dependency-injected (scheduler.py module-level function) - no engine,
# no Flask, no subprocess. This is the gate's own "assert on the call order,
# not on the end state" target.

def _stage(label, calls, *, error=None, **extra):
    """Build a run_X closure that records its call and returns a result dict."""
    def _run():
        calls.append(label)
        result = {"error": error}
        result.update(extra)
        return result
    return _run


def test_run_auto_cycle_happy_path_calls_all_three_in_order():
    calls = []
    outcome = sched.run_auto_cycle(
        "p",
        _stage("scrape", calls, count=5),
        _stage("generate", calls, generated=3),
        _stage("post", calls, posted=2),
    )
    assert calls == ["scrape", "generate", "post"]
    assert outcome["ok"] is True
    assert outcome["failed_stage"] is None


def test_run_auto_cycle_generate_failure_means_post_never_runs():
    calls = []
    outcome = sched.run_auto_cycle(
        "p",
        _stage("scrape", calls, count=5),
        _stage("generate", calls, error="boom"),
        _stage("post", calls, posted=2),
    )
    assert calls == ["scrape", "generate"]  # post never called
    assert outcome["ok"] is False
    assert outcome["failed_stage"] == "generate"
    assert outcome["note"] == "boom"


def test_run_auto_cycle_scrape_failure_means_neither_generate_nor_post_runs():
    calls = []
    outcome = sched.run_auto_cycle(
        "p",
        _stage("scrape", calls, error="login wall"),
        _stage("generate", calls, generated=3),
        _stage("post", calls, posted=2),
    )
    assert calls == ["scrape"]
    assert outcome["ok"] is False
    assert outcome["failed_stage"] == "scrape"
    assert outcome["note"] == "login wall"


def test_run_auto_cycle_post_failure_is_recorded_as_the_failed_stage():
    calls = []
    outcome = sched.run_auto_cycle(
        "p",
        _stage("scrape", calls, count=5),
        _stage("generate", calls, generated=3),
        _stage("post", calls, error="submit never enabled"),
    )
    assert calls == ["scrape", "generate", "post"]
    assert outcome["ok"] is False
    assert outcome["failed_stage"] == "post"


def test_run_auto_cycle_zero_scrape_posts_is_not_an_error_and_continues():
    """A quiet scrape (count=0, no error) is not a failure - the cycle
    continues to generate and post."""
    calls = []
    outcome = sched.run_auto_cycle(
        "p",
        _stage("scrape", calls, count=0),
        _stage("generate", calls, generated=0),
        _stage("post", calls, posted=0),
    )
    assert calls == ["scrape", "generate", "post"]
    assert outcome["ok"] is True
    assert outcome["failed_stage"] is None


# ─── toggle("auto_mode", ...) enables/disables the cycle in one write ────────

def test_toggle_auto_mode_on_enables_only_the_cycle(persist):
    """auto_mode drives exactly one job now - it must not touch scrape or
    post_comments's own independent toggles."""
    cfg = {"scheduler": {"enabled": False, "scrape": {"enabled": False},
                        "auto_cycle": {"enabled": False},
                        "post_comments": {"enabled": True}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "auto_mode", True)
    assert sched_out["auto_mode"] is True
    assert sched_out["enabled"] is True
    assert sched_out["auto_cycle"]["enabled"] is True
    assert sched_out["scrape"]["enabled"] is False  # untouched
    assert sched_out["post_comments"]["enabled"] is True  # untouched (was already True)
    assert persist["p"]["scheduler"]["auto_mode"] is True


def test_toggle_auto_mode_on_never_touches_connector(persist):
    """connector (Dispatch 38) is an unrelated feature sharing JOB_TYPES with
    the cycle auto_mode governs - enabling auto_mode must not enable it as a
    side effect."""
    cfg = {"scheduler": {"enabled": False, "scrape": {"enabled": False},
                        "auto_cycle": {"enabled": False},
                        "post_comments": {"enabled": True},
                        "connector": {"enabled": False}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "auto_mode", True)
    assert sched_out["connector"]["enabled"] is False


def test_toggle_connector_on_never_touches_auto_mode(persist):
    """The mirror case: enabling connector must not enable auto_mode/cycle."""
    cfg = {"scheduler": {"enabled": False, "auto_mode": False,
                        "auto_cycle": {"enabled": False},
                        "connector": {"enabled": False}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "connector", True)
    assert sched_out["connector"]["enabled"] is True
    assert sched_out["auto_mode"] is False
    assert sched_out["auto_cycle"]["enabled"] is False


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
                        "auto_mode_stopped_reason": "scrape failed: boom",
                        "auto_mode_stopped_at": "2026-07-01T00:00:00"}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    sched_out = engine.toggle("p", "auto_mode", True)
    assert "auto_mode_stopped_reason" not in sched_out
    assert "auto_mode_stopped_at" not in sched_out


# ─── The circuit breaker: one failed cycle disables auto_mode (Dispatch 40) ──
# "One failure, not three": no consecutive-failure counting any more - this
# replaces the old per-job-type 3-strikes threshold entirely.

def test_a_failed_cycle_disables_auto_mode_on_the_first_failure(persist):
    cfg = {"scheduler": {"enabled": True, "auto_mode": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)

    engine.report_cycle_outcome("p", False, "generate", "boom 1")

    # The gate: assert on the config WRITE, not the log line.
    assert "p" in persist
    assert persist["p"]["scheduler"]["auto_mode"] is False
    reason = persist["p"]["scheduler"]["auto_mode_stopped_reason"]
    assert "generate" in reason and "boom 1" in reason
    assert persist["p"]["scheduler"]["auto_mode_stopped_at"]


def test_a_failed_cycle_names_whichever_stage_failed(persist):
    cfg = {"scheduler": {"enabled": True, "auto_mode": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    engine.report_cycle_outcome("p", False, "scrape", "login wall")
    assert "scrape" in persist["p"]["scheduler"]["auto_mode_stopped_reason"]


def test_a_successful_cycle_never_disables_auto_mode(persist):
    """Includes the "scrape found zero posts" case - run_auto_cycle never
    reports that as a failure, so a cycle that reaches report_cycle_outcome
    with success=True (whatever each stage individually did) is a no-op."""
    cfg = {"scheduler": {"enabled": True, "auto_mode": True}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    engine.report_cycle_outcome("p", True)
    assert "p" not in persist


def test_report_cycle_outcome_is_a_noop_when_auto_mode_is_off(persist):
    """The circuit breaker only polices the unattended chain - with auto_mode
    off, a failure is not even written back."""
    cfg = {"scheduler": {"enabled": True, "auto_mode": False}}
    engine = sched.Scheduler(config_fn=lambda p: cfg)
    engine.report_cycle_outcome("p", False, "post", "whatever")
    assert "p" not in persist


def test_reenabling_auto_mode_lets_a_fresh_cycle_disable_it_again(persist):
    """After a self-disable, re-enabling (the same engine instance - the
    dashboard runs exactly one) and a new failure disables it again - there
    is no stale counter to carry over since there is no counter at all."""
    cfg_holder = {"cfg": {"scheduler": {"enabled": True, "auto_mode": True}}}
    engine = sched.Scheduler(config_fn=lambda p: cfg_holder["cfg"])

    engine.report_cycle_outcome("p", False, "generate", "boom")
    assert persist["p"]["scheduler"]["auto_mode"] is False

    # config_fn reads back whatever was last persisted, same as the real
    # pm.get_profile_config would after the write above.
    cfg_holder["cfg"] = persist["p"]
    engine.toggle("p", "auto_mode", True)
    persist.clear()  # isolate what happens next from the toggle's own write

    engine.report_cycle_outcome("p", False, "post", "boom again")
    assert persist["p"]["scheduler"]["auto_mode"] is False
