"""D-021 F5: ``late_afternoon`` and ``night``, opt-in windows (DEC-046).

They exist for lookup, validation and ordering, but are placed only when a
caller names them through ``redate_pending``'s ``windows_for`` (or a row that
names them). Without ``windows_for`` the cap and placement are unchanged;
``test_window_defaults_golden.py`` proves that against pre-F5 output.
America/Los_Angeles is UTC-8 in January.
"""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from linkedin_automation import buffer_client as bc
from linkedin_automation import csv_pipeline as cp

NOW = datetime(2036, 1, 5, 17, 0, tzinfo=timezone.utc)  # 09:00 local
FIVE = ["morning", "afternoon", "late_afternoon", "evening", "night"]


@pytest.fixture(autouse=True)
def _la(monkeypatch):
    monkeypatch.setenv("POST_TIMEZONE", "America/Los_Angeles")


@pytest.fixture
def state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "s.json"), platform="pinterest")


def _put(state, n, window="evening"):
    for i in range(n):
        state.rows["k%02d" % i] = {
            "status": cp.PENDING, "updated_at": "2026-01-01T00:00:%02d" % i,
            "row": {"date": "2036-01-02", "time_window": window,
                    "post_text": "row %d" % i}}


def _five_a_day(n):
    # The OPS-8 shape: 5 -> all five, 2 -> morning and evening, 1 -> afternoon.
    return {0: [], 1: ["afternoon"], 2: ["evening", "morning"],
            5: list(reversed(FIVE))}[n]


def _placed(out):
    return [(o["date"], o["time_window"]) for o in out]


def test_window_rank_orders_all_five_by_start_minute():
    shuffled = ["night", "Late_Afternoon ", "morning", "EVENING", "afternoon"]
    assert [w.strip().lower() for w in sorted(shuffled, key=cp.window_rank)] \
        == FIVE
    assert sorted(bc.TIME_WINDOWS, key=cp.window_rank) == FIVE
    starts = [bc.TIME_WINDOWS[w][0] * 60 + bc.TIME_WINDOWS[w][1] for w in FIVE]
    assert starts == sorted(starts) and len(set(starts)) == 5


def test_new_windows_sort_in_clock_order_in_the_pending_queue(state):
    for i, w in enumerate(reversed(FIVE)):
        state.rows[w] = {"status": cp.PENDING,
                         "updated_at": "2026-01-01T00:00:0%d" % i,
                         "row": {"date": "2036-01-06", "time_window": w,
                                 "post_text": w}}
    assert [r["post_text"] for r in cp.pending_rows(state)] == FIVE


@pytest.mark.parametrize("window,lo,hi", [("late_afternoon", 15, 17),
                                          ("night", 20, 22)])
def test_due_at_lands_inside_the_new_window(window, lo, hi):
    tz = bc.posting_timezone()
    seen = set()
    for seed in range(200):
        when = datetime.fromisoformat(bc.due_at(
            "2036-01-06", window, rng=random.Random(seed), tz=tz,
            now=NOW).replace("Z", "+00:00")).astimezone(tz)
        assert when.date().isoformat() == "2036-01-06"
        start = when.replace(hour=lo, minute=0, second=0, microsecond=0)
        end = when.replace(hour=hi, minute=0, second=0, microsecond=0)
        assert start <= when <= end, when
        seen.add(when.hour)
    assert set(range(lo, hi)) <= seen  # the minute spreads across the window


def test_due_at_window_edges_are_the_declared_bounds():
    tz = bc.posting_timezone()

    class _Edge:
        def __init__(self, pick_max):
            self.pick_max = pick_max

        def randint(self, a, b):
            return b if self.pick_max else a

    for window, lo, hi in (("late_afternoon", "15:00", "17:00"),
                           ("night", "20:00", "22:00")):
        first = datetime.fromisoformat(bc.due_at(
            "2036-01-06", window, rng=_Edge(False), tz=tz, now=NOW)
            .replace("Z", "+00:00")).astimezone(tz)
        last = datetime.fromisoformat(bc.due_at(
            "2036-01-06", window, rng=_Edge(True), tz=tz, now=NOW)
            .replace("Z", "+00:00")).astimezone(tz)
        assert first.strftime("%H:%M") == lo
        assert last.strftime("%H:%M") == hi


def test_five_a_day_with_five_windows_places_five_in_clock_order(state):
    _put(state, 12)
    out = cp.redate_pending("p", "pinterest", lambda _d: 5,
                            start=("2036-01-06", "morning"), now=NOW,
                            state=state, windows_for=_five_a_day)
    assert _placed(out) == (
        [("2036-01-06", w) for w in FIVE] + [("2036-01-07", w) for w in FIVE]
        + [("2036-01-08", "morning"), ("2036-01-08", "afternoon")])
    assert [o["key"] for o in out] == ["k%02d" % i for i in range(12)]
    # The queue reads back in the same order it was placed.
    assert [(r["date"], r["time_window"]) for r in cp.pending_rows(state)] \
        == _placed(out)


def test_five_a_day_start_day_uses_only_windows_not_yet_begun(state):
    # DEC-043(1): on the start day only windows_for's choices from the start
    # window on are used.
    _put(state, 7)
    out = cp.redate_pending("p", "pinterest", lambda _d: 5,
                            start=("2036-01-06", "late_afternoon"), now=NOW,
                            state=state, windows_for=_five_a_day)
    assert _placed(out) == [
        ("2036-01-06", "late_afternoon"), ("2036-01-06", "evening"),
        ("2036-01-06", "night")] + [("2036-01-07", w) for w in FIVE[:4]]


def test_default_start_with_five_windows_is_the_next_window_of_the_five(state):
    # 15:30 local: late_afternoon began at 15:00, so evening then night today.
    _put(state, 3)
    now = datetime(2036, 1, 5, 23, 30, tzinfo=timezone.utc)
    out = cp.redate_pending("p", "pinterest", lambda _d: 5, now=now,
                            state=state, windows_for=_five_a_day)
    assert _placed(out) == [("2036-01-05", "evening"), ("2036-01-05", "night"),
                            ("2036-01-06", "morning")]


def test_a_start_in_night_is_accepted_with_windows_for(state):
    _put(state, 3)
    out = cp.redate_pending("p", "pinterest", lambda _d: 5,
                            start=("2036-01-06", "night"), now=NOW,
                            state=state, windows_for=_five_a_day)
    assert _placed(out) == [("2036-01-06", "night"), ("2036-01-07", "morning"),
                            ("2036-01-07", "afternoon")]


def test_quiet_day_calendar_mixes_5_2_1(state):
    base = datetime(2036, 1, 6).date()
    counts = {0: 5, 1: 2, 2: 1, 3: 0, 4: 5}

    def per_day(d):
        return counts.get((d - base).days, 5)
    _put(state, 13)
    out = cp.redate_pending("p", "pinterest", per_day,
                            start=("2036-01-06", "morning"), now=NOW,
                            state=state, windows_for=_five_a_day)
    assert _placed(out) == (
        [("2036-01-06", w) for w in FIVE]
        + [("2036-01-07", "morning"), ("2036-01-07", "evening"),
           ("2036-01-08", "afternoon")]
        + [("2036-01-10", w) for w in FIVE])


@pytest.mark.parametrize("per_day", [4, 5, lambda _d: 4, lambda _d: 5])
def test_without_windows_for_the_cap_stays_three(state, per_day):
    _put(state, 3)
    with pytest.raises(ValueError, match=r"per_day.* [01] to 3, got"):
        cp.redate_pending("p", "pinterest", per_day, now=NOW, state=state)


def test_with_windows_for_six_is_still_refused(state):
    _put(state, 3)
    before = json.dumps(state.rows, sort_keys=True)
    with pytest.raises(ValueError, match="0 to 5"):
        cp.redate_pending("p", "pinterest", lambda _d: 6,
                          start=("2036-01-06", "morning"), now=NOW,
                          state=state, windows_for=_five_a_day)
    assert json.dumps(state.rows, sort_keys=True) == before


@pytest.mark.parametrize("window", ["late_afternoon", "night"])
def test_without_windows_for_a_new_window_start_is_refused(state, window):
    _put(state, 1)
    with pytest.raises(ValueError, match="not one of morning, afternoon, evening"):
        cp.redate_pending("p", "pinterest", 3, start=("2036-01-06", window),
                          now=NOW, state=state)


def test_without_windows_for_no_new_window_is_ever_placed(state):
    _put(state, 30)
    for h in range(24):
        now = datetime(2036, 1, 5, 8, 0, tzinfo=timezone.utc) + timedelta(hours=h)
        st = cp.PipelineState(path=state.path + str(h), platform="pinterest")
        st.rows = json.loads(json.dumps(state.rows))
        out = cp.redate_pending("p", "pinterest", 3, now=now, state=st)
        assert {o["time_window"] for o in out} <= set(bc.DEFAULT_WINDOWS)


def test_window_slots_and_first_open_slot_iterate_only_the_defaults():
    tz = bc.posting_timezone()
    day = datetime(2036, 1, 5).date()
    assert [n for _d, n in cp._window_slots(day)] == list(bc.DEFAULT_WINDOWS)
    # 15:30 local, inside late_afternoon's hours: the default answer is evening.
    now = datetime(2036, 1, 5, 23, 30, tzinfo=timezone.utc)
    assert cp._first_open_slot(now, tz) == (day, "evening")
    # 20:30 local: night has begun anyway, and the default is next morning.
    now = datetime(2036, 1, 6, 4, 30, tzinfo=timezone.utc)
    assert cp._first_open_slot(now, tz)[1] == "morning"
    # 19:00 local: only a caller that names the five sees night.
    now = datetime(2036, 1, 6, 3, 0, tzinfo=timezone.utc)
    assert cp._first_open_slot(now, tz)[1] == "morning"
    assert cp._first_open_slot(now, tz, bc.TIME_WINDOWS) == (day, "night")


@pytest.mark.parametrize("window", ["late_afternoon", "night", " Night "])
def test_a_csv_row_naming_a_new_window_validates(window):
    row = {"date": "2036-01-06", "time_window": window, "post_text": "Hi.",
           "topic": "", "tags": "", "image_path": "", "first_comment_link": ""}
    assert cp.validate_row(row, now=NOW, platform="linkedin") == []


def test_a_csv_row_naming_an_undeclared_window_is_still_refused():
    row = {"date": "2036-01-06", "time_window": "midnight", "post_text": "Hi.",
           "topic": "", "tags": "", "image_path": "", "first_comment_link": ""}
    problems = cp.validate_row(row, now=NOW, platform="linkedin")
    assert any("unknown time_window" in p for p in problems)
