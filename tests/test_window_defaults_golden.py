"""D-021 F5: the two opt-in windows change nothing for a caller that names none.

``tests/fixtures/window_placement_golden.json`` was generated from the code
BEFORE ``late_afternoon`` and ``night`` were added (main at 5bd84d4). Every
consumer that picks windows without being told which ones -
``_window_slots``, ``_first_open_slot``, ``_start_slot``, ``redate_pending``
with no ``windows_for`` (int and callable ``per_day``), ``redate_pending``
with a ``windows_for`` that names only the default three, ``window_rank`` and
the pending/held sort - is run here for a LinkedIn, an X and a Pinterest
state at times across the day, and must match the golden exactly.

Regenerate (only from code whose default placement is known good)::

    python tests/test_window_defaults_golden.py --write
"""

import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

if __name__ == "__main__":  # pragma: no cover - golden regeneration only
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures",
                      "window_placement_golden.json")
PLATFORMS = ("linkedin", "x", "pinterest")
DEFAULT_THREE = ("morning", "afternoon", "evening")

# Local Los Angeles times on 2036-01-05 (UTC-8), chosen to fall before,
# inside and between every window, old and new: 07:00, 09:00, 11:30, 13:00,
# 15:30, 16:59, 17:00, 18:00, 20:30, 21:30, 23:30.
LOCAL_TIMES = ((7, 0), (9, 0), (11, 30), (13, 0), (15, 30), (16, 59),
               (17, 0), (18, 0), (20, 30), (21, 30), (23, 30))


def _now(h, m):
    return datetime(2036, 1, 5, h, m, tzinfo=timezone(timedelta(hours=-8))
                    ).astimezone(timezone.utc)


def _calendar(d):
    # Varies 0..3 by weekday so every count is exercised.
    return (3, 2, 0, 1, 3, 2, 3)[d.weekday()]


def _two_ends(n):
    return {0: [], 1: ["afternoon"], 2: ["evening", "morning"],
            3: ["morning", "afternoon", "evening"]}[n]


def _state(tmp, platform, n=9):
    st = cp.PipelineState(path=os.path.join(tmp, "%s-%d.json" % (
        platform, random.random() * 1e9)), platform=platform)
    for i in range(n):
        st.rows["k%02d" % i] = {
            "status": cp.PENDING, "updated_at": "2026-01-01T00:00:%02d" % i,
            "row": {"date": "2036-01-02", "time_window": "evening",
                    "post_text": "row %d" % i}}
    return st


def _redate(tmp, platform, per_day, now, **kw):
    st = _state(tmp, platform)
    try:
        out = cp.redate_pending("p", platform, per_day, now=now, state=st,
                                rng=random.Random(7), **kw)
    except ValueError as exc:
        return "ValueError: %s" % exc
    return [[o["key"], o["date"], o["time_window"]] for o in out]


def scenarios(tmp):
    """Everything the golden pins, as a JSON-able dict."""
    tz = bc.posting_timezone()
    out = {}
    out["window_slots"] = {
        str(fw): [[d.isoformat(), n] for d, n in
                  cp._window_slots(date(2036, 1, 5), fw)]
        for fw in (None,) + DEFAULT_THREE}
    out["rank_order"] = sorted(
        ["evening", "Morning ", "afternoon", "zz", "", "AFTERNOON"],
        key=cp.window_rank)
    first_open, start_slot, due = {}, {}, {}
    for h, m in LOCAL_TIMES:
        label = "%02d:%02d" % (h, m)
        now = _now(h, m)
        d, n = cp._first_open_slot(now, tz)
        first_open[label] = [d.isoformat(), n]
        d, n = cp._start_slot(now + timedelta(minutes=1), now, tz)
        start_slot[label] = [d.isoformat(), n]
    for w in DEFAULT_THREE:
        due[w] = [bc.due_at("2036-01-06", w, rng=random.Random(s), tz=tz,
                            now=_now(0, 0)) for s in range(5)]
    out["first_open_slot"] = first_open
    out["start_slot_datetime"] = start_slot
    out["due_at"] = due
    redate = {}
    for platform in PLATFORMS:
        for h, m in LOCAL_TIMES:
            now = _now(h, m)
            base = "%s %02d:%02d " % (platform, h, m)
            for per_day in (1, 2, 3):
                redate[base + "int%d" % per_day] = _redate(
                    tmp, platform, per_day, now)
            redate[base + "calendar"] = _redate(tmp, platform, _calendar, now)
            redate[base + "calendar two_ends"] = _redate(
                tmp, platform, _calendar, now, windows_for=_two_ends)
            redate[base + "int3 two_ends"] = _redate(
                tmp, platform, 3, now, windows_for=_two_ends)
        for w in DEFAULT_THREE:
            redate["%s start 2036-01-06 %s int2" % (platform, w)] = _redate(
                tmp, platform, 2, _now(0, 0), start=("2036-01-06", w))
        for bad in (0, 4):
            redate["%s int%d refused" % (platform, bad)] = _redate(
                tmp, platform, bad, _now(0, 0))
        redate["%s calendar returns 4 refused" % platform] = _redate(
            tmp, platform, lambda _d: 4, _now(0, 0))
    out["redate"] = redate
    st = _state(tmp, "pinterest", n=0)
    for i, (d, w) in enumerate([("2036-01-06", "evening"),
                                ("2036-01-05", "evening"),
                                ("2036-01-06", "morning"),
                                ("2036-01-05", "afternoon"),
                                ("2036-01-05", "morning")]):
        st.rows["s%d" % i] = {"status": cp.PENDING,
                              "updated_at": "2026-01-01T00:00:0%d" % (9 - i),
                              "row": {"date": d, "time_window": w,
                                      "post_text": "s%d" % i}}
    out["pending_order"] = [r["post_text"] for r in cp.pending_rows(st)]
    return out


@pytest.fixture(autouse=True)
def _la(monkeypatch):
    monkeypatch.setenv("POST_TIMEZONE", "America/Los_Angeles")


def test_default_placement_matches_the_pre_f5_golden(tmp_path):
    with open(GOLDEN, encoding="utf-8") as f:
        golden = json.load(f)
    got = json.loads(json.dumps(scenarios(str(tmp_path))))
    assert set(got) == set(golden)
    for section in golden:
        if isinstance(golden[section], dict):
            assert set(got[section]) == set(golden[section]), section
            for k in golden[section]:
                assert got[section][k] == golden[section][k], (section, k)
        else:
            assert got[section] == golden[section], section


def test_the_golden_covers_every_platform_and_both_redate_paths():
    with open(GOLDEN, encoding="utf-8") as f:
        golden = json.load(f)
    keys = list(golden["redate"])
    for platform in PLATFORMS:
        assert any(k.startswith(platform + " ") and k.endswith("int3")
                   for k in keys)
        assert any(k.startswith(platform + " ") and k.endswith("two_ends")
                   for k in keys)
    placed = [w for v in golden["redate"].values() if isinstance(v, list)
              for _k, _d, w in v]
    assert placed and set(placed) == set(DEFAULT_THREE)


if __name__ == "__main__":  # pragma: no cover
    import tempfile
    os.environ["POST_TIMEZONE"] = "America/Los_Angeles"
    if "--write" not in sys.argv:
        sys.exit("pass --write to regenerate %s" % GOLDEN)
    with tempfile.TemporaryDirectory() as tmp:
        data = scenarios(tmp)
    with open(GOLDEN, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
    print("wrote", GOLDEN)
