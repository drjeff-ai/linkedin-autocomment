"""D-012 F2: rows waiting to be sent come back in clock order within a day.

``pending_rows`` and ``held_rows`` sort by date, then by the window's start
time from ``buffer_client.TIME_WINDOWS``, never by the window's name (which
put afternoon and evening before morning).
"""

import pytest

from linkedin_automation import buffer_client as bc
from linkedin_automation import csv_pipeline as cp


@pytest.fixture
def state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "state.json"))


def _put(state, key, date, window, status=cp.PENDING, updated_at="2026-01-01T00:00:00"):
    state.rows[key] = {"status": status, "updated_at": updated_at,
                       "row": {"date": date, "time_window": window,
                               "post_text": key}}


def _order(rows):
    return [(r["date"], r["time_window"]) for r in rows]


def test_the_declared_windows_are_the_ones_this_test_assumes():
    # D-021 F5: two opt-in windows join the three defaults (DEC-046).
    assert set(bc.DEFAULT_WINDOWS) == {"morning", "afternoon", "evening"}
    assert set(bc.TIME_WINDOWS) == set(bc.DEFAULT_WINDOWS) | {
        "late_afternoon", "night"}
    # Alphabetical order differs from clock order, so the tests below can tell.
    assert sorted(bc.DEFAULT_WINDOWS) == ["afternoon", "evening", "morning"]


@pytest.mark.parametrize("insert", [
    ["evening", "afternoon", "morning"],
    ["afternoon", "evening", "morning"],
    ["morning", "evening", "afternoon"],
])
def test_one_row_per_window_on_one_day_comes_back_in_clock_order(state, insert):
    # Later-queued first, so neither insertion nor queue time can produce
    # the right answer by accident.
    for i, window in enumerate(insert):
        _put(state, "k-" + window, "2036-01-05", window,
             updated_at="2026-01-01T00:00:0%d" % (9 - i))
    assert [r["time_window"] for r in cp.pending_rows(state)] == [
        "morning", "afternoon", "evening"]


def test_held_rows_use_the_same_clock_order(state):
    for window in ("evening", "afternoon", "morning"):
        _put(state, "k-" + window, "2036-01-05", window, status=cp.HELD)
    assert [r["time_window"] for r in cp.held_rows(state)] == [
        "morning", "afternoon", "evening"]


def test_rows_across_days_come_back_by_date_first(state):
    _put(state, "a", "2036-01-06", "morning")
    _put(state, "b", "2036-01-05", "evening")
    _put(state, "c", "2036-01-07", "afternoon")
    _put(state, "d", "2036-01-06", "evening")
    _put(state, "e", "2036-01-05", "morning")
    _put(state, "f", "2036-01-06", "afternoon")
    assert _order(cp.pending_rows(state)) == [
        ("2036-01-05", "morning"), ("2036-01-05", "evening"),
        ("2036-01-06", "morning"), ("2036-01-06", "afternoon"),
        ("2036-01-06", "evening"), ("2036-01-07", "afternoon")]


def test_same_day_and_window_falls_back_to_queue_time(state):
    _put(state, "late", "2036-01-05", "morning", updated_at="2026-01-02T00:00:00")
    _put(state, "early", "2036-01-05", "morning", updated_at="2026-01-01T00:00:00")
    assert [r["post_text"] for r in cp.pending_rows(state)] == ["early", "late"]


def test_window_rank_is_clock_order_and_tolerates_case_and_space():
    ranks = [cp.window_rank(w) for w in (" Morning ", "AFTERNOON", "evening")]
    assert ranks == sorted(ranks)
    assert cp.window_rank(" Morning ") == cp.window_rank("morning")


def test_an_undeclared_window_sorts_after_every_declared_one(state):
    _put(state, "x", "2036-01-05", "zz-unknown")
    _put(state, "y", "2036-01-05", "")
    _put(state, "z", "2036-01-05", "evening")
    windows = [r["time_window"] for r in cp.pending_rows(state)]
    assert windows[0] == "evening"
    assert set(windows[1:]) == {"zz-unknown", ""}


def test_scheduled_rows_are_not_waiting(state):
    _put(state, "s", "2036-01-05", "morning", status=cp.SCHEDULED)
    _put(state, "p", "2036-01-05", "evening")
    assert [r["post_text"] for r in cp.pending_rows(state)] == ["p"]
