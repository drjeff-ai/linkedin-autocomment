"""D-012 F3: ``csv_pipeline.redate_pending`` re-dates queued rows in place.

Pending (queued, not scheduled, not held) rows get consecutive dates,
``per_day`` a day in clock-ordered windows, from the next window not yet
begun or ``start``. Row keys never move: the re-dated row goes through
``schedule_pass`` under the key it was queued with, and the same CSV queued
again is still recognised. All times are fixed; the posting zone is
America/Los_Angeles (UTC-8 in January).
"""

import copy
import json
import random
from datetime import datetime, timezone

import pytest

from linkedin_automation import buffer_client as bc
from linkedin_automation import csv_pipeline as cp

# 2036-01-05 09:00 in Los Angeles: morning has begun, afternoon has not.
NOW = datetime(2036, 1, 5, 17, 0, tzinfo=timezone.utc)
LINK = "https://example.com/a"


@pytest.fixture(autouse=True)
def _la(monkeypatch):
    monkeypatch.setenv("POST_TIMEZONE", "America/Los_Angeles")


def make_row(i, date="2036-01-02", window="evening"):
    return {"date": date, "time_window": window, "post_text": "Body %d." % i,
            "topic": "", "tags": "", "image_path": "",
            "first_comment_link": LINK}


@pytest.fixture
def state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "state.json"), platform="linkedin")


def _queue(state, rows, now=None):
    accepted, rejected, skipped, _w = cp.queue_rows(
        rows, state, now=now or datetime(2036, 1, 1, tzinfo=timezone.utc))
    assert not rejected and not skipped, (rejected, skipped)
    return [a["key"] for a in accepted]


def _pending_dates(state):
    return [(r["date"], r["time_window"]) for r in cp.pending_rows(state)]


def _on_disk(state):
    with open(state.path, encoding="utf-8") as f:
        return json.load(f)["rows"]


def test_keys_do_not_move(state):
    keys = _queue(state, [make_row(i) for i in range(4)])
    before = copy.deepcopy(state.rows)
    cp.redate_pending("p", "linkedin", 3, now=NOW, state=state)
    assert set(state.rows) == set(before) == set(keys)
    disk = _on_disk(state)
    assert set(disk) == set(keys)
    for k in keys:
        old, new = before[k]["row"], disk[k]["row"]
        assert new[cp.PINNED_KEY] == k
        changed = {f for f in set(old) | set(new) if old.get(f) != new.get(f)}
        assert changed <= {"date", "time_window", cp.PINNED_KEY}


def test_order_is_preserved(state):
    rows = [make_row(0, "2036-01-02", "morning"),
            make_row(1, "2036-01-02", "evening"),
            make_row(2, "2036-01-03", "morning"),
            make_row(3, "2036-01-03", "afternoon")]
    _queue(state, rows)
    order_before = [r["post_text"] for r in cp.pending_rows(state)]
    out = cp.redate_pending("p", "linkedin", 2, now=NOW, state=state)
    assert [r["post_text"] for r in cp.pending_rows(state)] == order_before
    assert [o["key"] for o in out] == [
        cp.row_key(r, "linkedin") for r in rows]


def test_default_start_is_the_next_window_not_yet_begun(state):
    _queue(state, [make_row(i) for i in range(4)])
    cp.redate_pending("p", "linkedin", 3, now=NOW, state=state)
    # 09:00 local: morning has begun, so afternoon is first; the start day
    # has only two windows left.
    assert _pending_dates(state) == [
        ("2036-01-05", "afternoon"), ("2036-01-05", "evening"),
        ("2036-01-06", "morning"), ("2036-01-06", "afternoon")]


def test_no_row_is_dated_in_the_past(state):
    _queue(state, [make_row(i) for i in range(7)])
    cp.redate_pending("p", "linkedin", 3, now=NOW, state=state)
    for d, w in _pending_dates(state):
        for seed in range(20):
            # Every minute the picker could choose is after NOW.
            assert bc.due_at(d, w, rng=random.Random(seed), now=NOW)
        sh, sm, _eh, _em = bc.TIME_WINDOWS[w]
        start = datetime.fromisoformat(d).replace(
            hour=sh, minute=sm, tzinfo=bc.posting_timezone())
        assert start > NOW
    assert not cp.validate_row(cp.pending_rows(state)[0], now=NOW,
                               platform="linkedin")


def test_a_start_in_the_past_is_refused_and_nothing_written(state):
    _queue(state, [make_row(0)])
    before = json.dumps(_on_disk(state), sort_keys=True)
    with pytest.raises(ValueError, match="already begun"):
        cp.redate_pending("p", "linkedin", 3, start=("2036-01-05", "morning"),
                          now=NOW, state=state)
    assert json.dumps(_on_disk(state), sort_keys=True) == before


def test_per_day_3_across_2_days_gives_3_plus_3_in_clock_order(state):
    _queue(state, [make_row(i) for i in range(6)])
    out = cp.redate_pending("p", "linkedin", 3, start=("2036-01-06", "morning"),
                            now=NOW, state=state)
    assert [(o["date"], o["time_window"]) for o in out] == [
        ("2036-01-06", "morning"), ("2036-01-06", "afternoon"),
        ("2036-01-06", "evening"),
        ("2036-01-07", "morning"), ("2036-01-07", "afternoon"),
        ("2036-01-07", "evening")]
    assert _pending_dates(state) == [(o["date"], o["time_window"]) for o in out]


def test_per_day_below_the_window_count_uses_the_earliest_windows(state):
    _queue(state, [make_row(i) for i in range(4)])
    out = cp.redate_pending("p", "linkedin", 2, start=("2036-01-06", "afternoon"),
                            now=NOW, state=state)
    assert [(o["date"], o["time_window"]) for o in out] == [
        ("2036-01-06", "afternoon"), ("2036-01-06", "evening"),
        ("2036-01-07", "morning"), ("2036-01-07", "afternoon")]


def test_a_datetime_start_takes_the_first_window_at_or_after_it(state):
    _queue(state, [make_row(0)])
    # 2036-01-06 13:00 local (21:00Z): afternoon began at 12:00, so evening.
    out = cp.redate_pending("p", "linkedin", 3,
                            start=datetime(2036, 1, 6, 21, 0, tzinfo=timezone.utc),
                            now=NOW, state=state)
    assert (out[0]["date"], out[0]["time_window"]) == ("2036-01-06", "evening")


def test_scheduled_and_held_rows_are_untouched(state):
    keys = _queue(state, [make_row(i) for i in range(4)])
    state.update(keys[0], status=cp.SCHEDULED, post_id="b1")
    state.update(keys[1], status=cp.HELD, hold_reason="waiting for a slot")
    before = copy.deepcopy(state.rows)
    out = cp.redate_pending("p", "linkedin", 3, now=NOW, state=state)
    assert {o["key"] for o in out} == {keys[2], keys[3]}
    assert state.rows[keys[0]] == before[keys[0]]
    assert state.rows[keys[1]] == before[keys[1]]
    disk = _on_disk(state)
    assert disk[keys[0]] == before[keys[0]]
    assert disk[keys[1]] == before[keys[1]]


@pytest.mark.parametrize("bad", [0, 4, -1, "3", True, None])
def test_per_day_out_of_range_is_refused(state, bad):
    _queue(state, [make_row(0)])
    with pytest.raises(ValueError, match="per_day"):
        cp.redate_pending("p", "linkedin", bad, now=NOW, state=state)


def test_an_empty_queue_writes_nothing(state):
    assert cp.redate_pending("p", "linkedin", 3, now=NOW, state=state) == []
    assert not state.rows


def test_a_state_for_another_channel_is_refused(state):
    with pytest.raises(ValueError, match="not 'x'"):
        cp.redate_pending("p", "x", 3, now=NOW, state=state)


class _Fake:
    def __init__(self):
        self.created = []

    def create_post(self, channel_id, text, image_url, when, key=None,
                    session=None, **_kw):
        self.created.append(when)
        return {"id": "post%d" % len(self.created), "status": "scheduled",
                "dueAt": when}


def test_a_redated_row_schedules_under_its_queued_key(state, monkeypatch):
    fake = _Fake()
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    originals = [make_row(i) for i in range(3)]
    keys = _queue(state, originals)
    cp.redate_pending("p", "linkedin", 3, now=NOW, state=state)

    res = cp.schedule_pass(cp.pending_rows(state), "chan", state, now=NOW,
                           rng=random.Random(1))
    assert [r["key"] for r in res] == keys
    assert len(fake.created) == 3
    assert set(state.rows) == set(keys)
    assert all(state.rows[k]["status"] == cp.SCHEDULED for k in keys)

    # The same CSV queued again is recognised, not queued a second time.
    accepted, _rej, skipped, _w = cp.queue_rows(
        originals, state, now=datetime(2036, 1, 1, tzinfo=timezone.utc))
    assert accepted == [] and len(skipped) == 3

    # And a second pass creates nothing.
    cp.schedule_pass(cp.pending_rows(state), "chan", state, now=NOW)
    assert len(fake.created) == 3


def test_a_redated_pending_row_is_not_queued_twice_by_its_csv(state):
    originals = [make_row(i) for i in range(2)]
    _queue(state, originals)
    cp.redate_pending("p", "linkedin", 3, now=NOW, state=state)
    accepted, _rej, skipped, _w = cp.queue_rows(
        originals, state, now=datetime(2036, 1, 1, tzinfo=timezone.utc))
    assert accepted == [] and len(skipped) == 2
    assert len(state.rows) == 2


def test_a_forged_pinned_key_is_ignored(state):
    keys = _queue(state, [make_row(0)])
    forged = dict(make_row(9), **{cp.PINNED_KEY: keys[0]})
    assert cp.effective_row_key(forged, state, "linkedin") == cp.row_key(
        forged, "linkedin") != keys[0]
    stray = dict(make_row(9), **{cp.PINNED_KEY: "0000000000000000"})
    assert cp.effective_row_key(stray, state, "linkedin") == cp.row_key(
        stray, "linkedin")


def test_the_pin_is_not_a_hashed_column(state):
    row = make_row(0)
    assert cp.row_key(dict(row, **{cp.PINNED_KEY: "x"}), "linkedin") == \
        cp.row_key(row, "linkedin")


def test_an_edited_copy_of_a_redated_row_does_not_inherit_its_key(state):
    keys = _queue(state, [make_row(0)])
    cp.redate_pending("p", "linkedin", 3, now=NOW, state=state)
    redated = cp.pending_rows(state)[0]
    assert cp.effective_row_key(redated, state, "linkedin") == keys[0]
    edited = dict(redated, post_text="Different text.")
    assert cp.effective_row_key(edited, state, "linkedin") == cp.row_key(
        edited, "linkedin") != keys[0]
