"""X1: the post drain feeds held X rows, and topic-only X rows are refused.

Offline. Every Buffer boundary (slot read, channel fetch, createPost, image
upload) is replaced by a recorder, so each claim is asserted on the calls that
WOULD have gone to Buffer: which channel, with which key, and how many.

The claims, and why each matters:

* one drain check feeds LinkedIn and X, each to its own channel with its own
  key - a held X row sent with LinkedIn's key or channel publishes to the wrong
  account, and nothing can unpublish it;
* an X feed that cannot resolve its identity or its settings sends NOTHING to
  Buffer, not even a slot read, and says so in recent_runs;
* the drain treats a declared-production identity exactly as the Schedule
  button does (DEC-013);
* a topic-only X row is refused at queue time and at schedule time, and a
  composed X body over the limit never reaches Buffer.
"""

import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import post_drain as pd  # noqa: E402

PROFILE = "p"
X_HANDLE = "AI_Fun_times"
TOPIC_ONLY_X = ("X needs post_text: topic generation writes LinkedIn-length "
                "posts and is not supported on X.")
NOW = datetime(2026, 9, 10, 9, 0, 0)


# ─── the world: one profile, two platforms, a recorded transport ─────────────

def _li_config():
    return {"buffer_channel_id": "chan_li",
            "identity_slug": "example-person-one",
            "drain": {"enabled": True, "interval_minutes": 30}}


def _x_overlay(identity=X_HANDLE, key_env="BUFFER_API_KEY_X"):
    return {"buffer_api_key_env": key_env, "buffer_channel_id": "chan_x",
            "identity_slug": identity}


class World:
    """Data dir, profile config and a Buffer recorder for one test."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.calls = []
        self.uploads = []
        self.scheduled = dict(_li_config())
        self.scheduled["platforms"] = {"x": _x_overlay()}
        self.handle = X_HANDLE
        self.free = 5
        mp = monkeypatch

        def get_data_dir(profile_name=None, subdir=None):
            path = tmp_path / subdir if subdir else tmp_path
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        mp.setattr(cp.pm, "get_data_dir", get_data_dir)
        mp.setattr(dashboard.pm, "get_profile_config",
                   lambda name=None: {"scheduled_posting": self.scheduled})
        mp.setenv("BUFFER_API_KEY", "li-key")
        mp.setenv("BUFFER_API_KEY_X", "x-key")

        def slots(channel_id, key=None, session=None, max_age=None):
            self.calls.append(("slots", channel_id, key))
            return {"limit": 10, "used": 10 - self.free, "free": self.free}

        def get_channel(channel_id, key=None, session=None, max_age=None):
            self.calls.append(("get_channel", channel_id, key))
            return {"id": channel_id, "name": self.handle, "service": "twitter",
                    "externalLink": "https://x.com/%s" % self.handle}

        def create_post(channel_id, text, image_url, when, key=None,
                        session=None):
            self.calls.append(("create_post", channel_id, key))
            return {"id": "post-%d" % len(self.calls), "dueAt": when}

        mp.setattr(dashboard.buffer_client, "scheduled_slots", slots)
        mp.setattr(cp.bc, "get_channel", get_channel)
        mp.setattr(cp.bc, "create_post", create_post)
        mp.setattr(cp.image_host, "upload_image",
                   lambda path, **kw: self.uploads.append(path) or "https://img")
        dashboard.jobs.clear()
        dashboard.jobs["j"] = {"log": [], "progress": "", "status": "completed"}

    def state(self, platform):
        return cp.PipelineState(profile_name=PROFILE, platform=platform)

    def hold(self, platform, n, text="Held %s row %d."):
        state = self.state(platform)
        for i in range(n):
            row = _row(i, text % (platform, i) if "%" in text else text)
            state.update(cp.row_key(row, platform), status=cp.HELD,
                         row=dict(row), text=cp.row_preview(row),
                         hold_reason="waiting for a slot", errors=[])
        return state

    def to(self, channel):
        return [c for c in self.calls if c[1] == channel]

    def engine(self):
        """The real dashboard collaborators, with the feed run inline."""
        submitted = []

        def submit(profile, feeds):
            submitted.append(list(feeds))
            dashboard._drain_feed_profile_job("j", profile, feeds)
            return "j"

        engine = pd.PostDrain(
            now_fn=lambda: NOW,
            list_profiles=lambda: [PROFILE],
            config_fn=dashboard.pm.get_profile_config,
            channel_fn=dashboard._scheduled_channel_id,
            held_fn=dashboard._drain_held_rows,
            gate_fn=dashboard._drain_gate,
            slots_fn=dashboard.buffer_client.scheduled_slots,
            submit_feed=submit,
            scheduling_available=lambda profile: True)
        engine.submitted = submitted
        return engine


def _row(i, text):
    return {"date": "2036-11-%02d" % (i + 1), "time_window": "morning",
            "post_text": text, "topic": "", "tags": "", "image_path": "",
            "first_comment_link": ""}


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    dashboard.jobs.clear()


def _runs(engine):
    return engine.status(PROFILE)["recent_runs"]


def _statuses(state_obj):
    return sorted(v["status"] for v in state_obj.rows.values())


# ─── gate 2: one check, two channels, two keys ───────────────────────────────

def test_one_check_feeds_linkedin_and_x_each_with_its_own_channel_and_key(world):
    world.hold("linkedin", 2)
    world.hold("x", 2)
    engine = world.engine()
    engine.tick()

    assert len(engine.submitted) == 1, "one check, one feed job for the profile"
    assert sorted(f["platform"] for f in engine.submitted[0]) == ["linkedin", "x"]

    # Every call to X's channel used X's key, and LinkedIn's used LinkedIn's.
    assert world.to("chan_x") and {c[2] for c in world.to("chan_x")} == {"x-key"}
    assert world.to("chan_li") and {c[2] for c in world.to("chan_li")} == {"li-key"}
    assert world.calls.count(("create_post", "chan_x", "x-key")) == 2
    assert world.calls.count(("create_post", "chan_li", "li-key")) == 2
    assert ("slots", "chan_x", "x-key") in world.calls
    assert ("slots", "chan_li", "li-key") in world.calls

    assert _statuses(world.state("x")) == [cp.SCHEDULED] * 2
    assert _statuses(world.state("linkedin")) == [cp.SCHEDULED] * 2
    runs = _runs(engine)
    assert sorted((r["platform"], r["action"]) for r in runs) == [
        ("linkedin", pd.ACTION_FED), ("x", pd.ACTION_FED)]


def test_a_held_x_row_never_goes_to_linkedins_channel(world):
    world.hold("x", 3)
    world.engine().tick()
    assert world.to("chan_li") == []
    assert world.calls.count(("create_post", "chan_x", "x-key")) == 3


def test_a_linkedin_only_profile_drains_exactly_as_before(world):
    """No X settings and no X rows: the check never touches X at all."""
    world.scheduled.pop("platforms")
    world.hold("linkedin", 2)
    engine = world.engine()
    engine.tick()
    assert world.to("chan_x") == []
    assert world.calls.count(("create_post", "chan_li", "li-key")) == 2
    assert [(r["platform"], r["action"]) for r in _runs(engine)] == [
        ("linkedin", pd.ACTION_FED)]


def test_held_count_is_the_total_across_channels(world):
    world.hold("linkedin", 2)
    world.hold("x", 3)
    assert world.engine().status(PROFILE)["held_count"] == 5


def test_two_channels_sharing_one_buffer_org_do_not_over_send(world):
    """The feed re-reads the slot count per channel; one free slot is one post."""
    world.hold("linkedin", 2)
    world.hold("x", 2)
    world.free = 1

    posted = cp.bc.create_post

    def post_then_fill(*a, **k):
        world.free = 0          # Buffer now has no slot left, for either channel
        return posted(*a, **k)
    cp.bc.create_post = post_then_fill
    try:
        world.engine().tick()
    finally:
        cp.bc.create_post = posted
    assert sum(1 for c in world.calls if c[0] == "create_post") == 1


# ─── gate 3: refusals send nothing to Buffer ─────────────────────────────────

@pytest.mark.parametrize("identity", ["", "   ", None])
def test_an_unresolvable_x_identity_sends_nothing_and_records_an_error(world,
                                                                         identity):
    world.scheduled["platforms"]["x"]["identity_slug"] = identity
    world.hold("x", 2)
    engine = world.engine()
    engine.tick()
    assert world.calls == [], "not even a slot read"
    assert engine.submitted == []
    run = _runs(engine)[0]
    assert run["platform"] == "x"
    assert run["action"] == pd.ACTION_ERROR
    assert "no declared identity" in run["note"]
    assert _statuses(world.state("x")) == [cp.HELD] * 2


def test_missing_x_settings_send_nothing_and_record_an_error(world):
    world.scheduled.pop("platforms")
    world.hold("x", 2)
    engine = world.engine()
    engine.tick()
    assert world.calls == []
    run = _runs(engine)[0]
    assert (run["platform"], run["action"]) == ("x", pd.ACTION_ERROR)
    assert "platforms.x" in run["note"]


def test_a_missing_x_key_refuses_and_never_falls_back_to_linkedins(world,
                                                                    monkeypatch):
    monkeypatch.delenv("BUFFER_API_KEY_X")
    world.hold("x", 1)
    engine = world.engine()
    engine.tick()
    assert world.calls == []
    run = _runs(engine)[0]
    assert (run["platform"], run["action"]) == ("x", pd.ACTION_ERROR)
    assert "BUFFER_API_KEY_X" in run["note"]


def test_a_refused_x_channel_does_not_stop_linkedin_in_the_same_check(world):
    world.scheduled["platforms"]["x"]["identity_slug"] = ""
    world.hold("linkedin", 1)
    world.hold("x", 1)
    engine = world.engine()
    engine.tick()
    assert world.to("chan_x") == []
    assert world.calls.count(("create_post", "chan_li", "li-key")) == 1
    assert sorted((r["platform"], r["action"]) for r in _runs(engine)) == [
        ("linkedin", pd.ACTION_FED), ("x", pd.ACTION_ERROR)]


def test_the_x_feed_job_refuses_an_unresolvable_identity_before_any_call(world):
    """The job re-applies the gate itself: the check may be minutes old."""
    world.hold("x", 2)
    world.scheduled["platforms"]["x"]["identity_slug"] = ""
    out = dashboard._drain_feed_job("j", PROFILE, "chan_x", 2, platform="x")
    assert world.calls == []
    assert out["scheduled"] == 0
    assert "no declared identity" in out["error"]
    assert any("REFUSED" in m for m in dashboard.jobs["j"]["log"])


def test_the_x_feed_job_refuses_missing_settings_before_any_call(world):
    world.hold("x", 2)
    world.scheduled.pop("platforms")
    out = dashboard._drain_feed_job("j", PROFILE, "chan_x", 2, platform="x")
    assert world.calls == []
    assert "platforms.x" in out["error"]


def test_a_wrong_x_channel_identity_refuses_inside_the_feed(world):
    """The Buffer-side identity check still runs: one fetch, no createPost."""
    world.hold("x", 1)
    world.handle = "someone_else"
    out = dashboard._drain_feed_job("j", PROFILE, "chan_x", 1, platform="x")
    assert [c[0] for c in world.calls] == ["slots", "get_channel"]
    assert out["scheduled"] == 0 and "someone_else" in out["error"]


def test_a_declared_production_slug_gets_the_schedule_buttons_outcome(
        world, monkeypatch):
    """DEC-013: the drain applies the production gate exactly as the Schedule
    button does - allow_production=True, so a declared slug proceeds on both."""
    monkeypatch.setenv(cp.pm.PRODUCTION_IDENTITY_SLUGS_ENV, X_HANDLE)
    row = _row(0, "One X post.")

    # The Schedule button: the row PENDING.
    world.state("x").update(cp.row_key(row, "x"), status=cp.PENDING,
                            row=dict(row), text="t")
    button = dashboard._scheduled_schedule_job("j", PROFILE, "chan_x",
                                               platform="x")
    button_calls = list(world.calls)

    # The drain: the same row, HELD, from a clean state.
    os.remove(world.state("x").path)
    world.calls.clear()
    world.hold("x", 1, text="One X post.")
    drain = dashboard._drain_feed_job("j", PROFILE, "chan_x", 1, platform="x")

    def outcome(out):
        return (out["scheduled"], out["failed"], out["held"], out.get("error"))
    assert outcome(drain) == outcome(button) == (1, 0, 0, None)
    assert world.calls == button_calls


def test_an_empty_identity_gets_the_schedule_buttons_refusal(world):
    world.scheduled["platforms"]["x"]["identity_slug"] = ""
    row = _row(0, "One X post.")
    world.state("x").update(cp.row_key(row, "x"), status=cp.PENDING,
                            row=dict(row), text="t")
    button = dashboard._scheduled_schedule_job("j", PROFILE, "chan_x",
                                               platform="x")
    os.remove(world.state("x").path)
    world.hold("x", 1, text="One X post.")
    drain = dashboard._drain_feed_job("j", PROFILE, "chan_x", 1, platform="x")
    assert drain == button
    assert world.calls == []


# ─── the wiring the engine actually runs with ────────────────────────────────

def test_the_dashboard_engine_is_wired_to_the_gated_collaborators():
    eng = dashboard.drain_engine
    assert eng._gate_fn is dashboard._drain_gate
    assert eng._held_fn is dashboard._drain_held_rows
    assert eng._channel_fn is dashboard._scheduled_channel_id
    assert eng._submit_feed is dashboard._drain_submit_feed
    assert sorted(eng._platforms_fn()) == sorted(cp.channels.REGISTRY)


def test_submit_starts_one_job_covering_every_channel(world, monkeypatch):
    started = []
    monkeypatch.setattr(dashboard, "run_job",
                        lambda job_id, fn, *args, **kw: started.append(
                            (fn, args, kw.get("category"))))
    feeds = [{"platform": "linkedin", "channel_id": "chan_li", "free": 1},
             {"platform": "x", "channel_id": "chan_x", "free": 1}]
    assert dashboard._drain_submit_feed(PROFILE, feeds)
    assert len(started) == 1
    fn, args, category = started[0]
    assert fn is dashboard._drain_feed_profile_job
    assert args == (PROFILE, feeds)
    assert category == pd.JOB_CATEGORY


def test_toggle_accepts_a_profile_whose_only_channel_is_x(world):
    world.scheduled["buffer_channel_id"] = ""
    monkey_saved = []
    dashboard.app.config["TESTING"] = True
    orig = dashboard.pm.save_profile_config
    dashboard.pm.save_profile_config = lambda n, c: monkey_saved.append(c)
    try:
        r = dashboard.app.test_client().post("/api/drain/p/toggle",
                                             json={"enabled": True})
    finally:
        dashboard.pm.save_profile_config = orig
    assert r.status_code == 200
    assert monkey_saved


def test_toggle_still_refuses_with_no_channel_anywhere(world):
    world.scheduled["buffer_channel_id"] = ""
    world.scheduled.pop("platforms")
    dashboard.app.config["TESTING"] = True
    r = dashboard.app.test_client().post("/api/drain/p/toggle",
                                         json={"enabled": True})
    assert r.status_code == 400


def test_drain_status_route_keeps_its_shape_and_runs_name_the_platform(world):
    world.hold("x", 1)
    dashboard.drain_engine._state.pop(PROFILE, None)
    engine = world.engine()
    engine.run_now(PROFILE)
    status = engine.status(PROFILE)
    assert set(status) == {"running", "automations_enabled", "enabled",
                           "interval_minutes", "now", "held_count",
                           "last_check_at", "last_feed_at", "next_check_at",
                           "slots", "recent_runs"}
    assert all("platform" in r for r in status["recent_runs"])


def test_a_quiet_manual_check_says_so_per_platform(world):
    world.hold("x", 1)
    world.free = 0
    engine = world.engine()
    result = engine.run_now(PROFILE)
    assert result["action"] == pd.ACTION_NO_SLOTS
    assert [(r["platform"], r["action"], r["manual"]) for r in _runs(engine)] == [
        ("x", pd.ACTION_NO_SLOTS, True)]


# ─── gate 4: topic-only X rows, and the pre-send length check ────────────────

def _topic_row(**kw):
    row = {"date": "2036-11-01", "time_window": "morning", "post_text": "",
           "topic": "why small teams ship faster", "tags": "",
           "image_path": "", "first_comment_link": ""}
    row.update(kw)
    return row


def test_queue_rows_rejects_a_topic_only_x_row_with_the_message(world):
    state = world.state("x")
    accepted, rejected, _skipped, _warn = cp.queue_rows([_topic_row()], state)
    assert accepted == []
    assert [TOPIC_ONLY_X] == [p for r in rejected for p in r["errors"]]
    assert state.rows == {}, "a rejected row is written nowhere"


def test_queue_rows_still_accepts_a_topic_only_linkedin_row(world):
    state = world.state("linkedin")
    accepted, rejected, _skipped, _warn = cp.queue_rows([_topic_row()], state)
    assert rejected == []
    assert len(accepted) == 1
    assert len(state.rows) == 1


def test_a_both_empty_x_row_gets_the_old_message_not_the_new_one():
    problems = cp.validate_row(_topic_row(topic=""), platform="x")
    assert any("both empty" in p for p in problems)
    assert TOPIC_ONLY_X not in problems


def test_schedule_pass_refuses_a_topic_only_x_row(world):
    state = world.state("x")
    generated = []
    results = cp.schedule_pass(
        [_topic_row()], "chan_x", state, platform="x", identity_slug=X_HANDLE,
        key="x-key", allow_production=True,
        generate=lambda topic, **kw: generated.append(topic) or "text")
    assert results[0]["status"] == cp.FAILED
    assert results[0]["stage"] == "validate"
    assert results[0]["errors"] == [TOPIC_ONLY_X]
    assert generated == [], "no OpenAI call for a row that cannot be sent"
    assert [c for c in world.calls if c[0] == "create_post"] == []


def test_schedule_pass_still_generates_for_a_topic_only_linkedin_row(world):
    state = world.state("linkedin")
    results = cp.schedule_pass(
        [_topic_row()], "chan_li", state, platform="linkedin",
        generate=lambda topic, **kw: "A generated LinkedIn post about " + topic)
    assert results[0]["status"] == cp.SCHEDULED
    assert world.calls.count(("create_post", "chan_li", None)) == 1


def _over_limit_pass(world, monkeypatch, generated_text):
    """A body only the send-time check can catch: validation is stubbed out,
    as it would be for any text that only exists after generation."""
    monkeypatch.setattr(cp, "validate_row", lambda *a, **k: [])
    image = world.tmp / "img.png"
    image.write_bytes(b"not really a png")
    state = world.state("x")
    return cp.schedule_pass(
        [_topic_row(image_path=str(image))], "chan_x", state, platform="x",
        identity_slug=X_HANDLE, key="x-key", allow_production=True,
        generate=lambda topic, **kw: generated_text), state


def test_an_over_limit_composed_x_body_fails_at_validate_and_sends_nothing(
        world, monkeypatch):
    results, state = _over_limit_pass(world, monkeypatch, "w" * 281)
    assert results[0]["status"] == cp.FAILED
    assert results[0]["stage"] == "validate"
    assert any("too long for X: 281" in e for e in results[0]["errors"])
    assert [c for c in world.calls if c[0] == "create_post"] == []
    assert world.uploads == [], "no image uploaded for a post that cannot exist"
    stored = next(iter(state.rows.values()))
    assert (stored["status"], stored["stage"]) == (cp.FAILED, "validate")


def test_a_body_at_the_x_limit_passes_the_send_time_check(world, monkeypatch):
    """The other direction: the check is able to say yes."""
    results, _state = _over_limit_pass(world, monkeypatch, "w" * 280)
    assert results[0]["status"] == cp.SCHEDULED
    assert world.calls.count(("create_post", "chan_x", "x-key")) == 1
    assert len(world.uploads) == 1


def test_text_limit_problem_answers_both_ways():
    assert cp.text_limit_problem("w" * 280, "x") is None
    assert "too long for X" in cp.text_limit_problem("w" * 281, "x")
    assert cp.text_limit_problem("w" * 256 + " https://example.com/" + "a" * 200,
                                 "x") is None, "a link counts as 23"
    assert cp.text_limit_problem("w" * 5000, "linkedin") is None
    assert cp.text_limit_problem("w" * 5000, None) is None


def test_the_queue_route_rejects_a_topic_only_x_row(world):
    dashboard.app.config["TESTING"] = True
    r = dashboard.app.test_client().post(
        "/api/scheduled/p/rows", json={"platform": "x", **_topic_row()})
    body = json.dumps(r.get_json())
    assert TOPIC_ONLY_X in body
    assert world.state("x").rows == {}
