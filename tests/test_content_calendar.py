"""U1: the read-only content calendar and the due time on every queue row.

What has to hold:
  * every queue row says when it goes out: ``due_at`` once Buffer has it,
    otherwise a ``planned`` label from the row's date and window;
  * the calendar route returns the profile's rows on every platform, in a date
    range, ordered, grouped by state, and it reads local files ONLY: no Buffer
    call, no network, no write;
  * the page carries the panel and the panel has no write action.
"""

import json
import os
import re
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import content_calendar as cal  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

def _pin(date, window, title, **extra):
    row = {"date": date, "time_window": window, "post_text": "About " + title,
           "title": title, "board": "Board A", "image_path": "x.png"}
    rec = {"status": cp.PENDING, "row": row, "text": "About " + title,
           "errors": []}
    rec.update(extra)
    return rec


PIN_ROWS = {
    "p_pending_late": _pin("2026-11-25", "night", "Late pin"),
    "p_pending_early": _pin("2026-11-25", "morning", "Early pin"),
    "p_held": _pin("2026-11-26", "afternoon", "Held pin", status=cp.HELD,
                   hold_reason="Buffer has no free slot"),
    # due_at 2026-11-24T01:26:39Z is 2026-11-23 17:26 PST: the DAY comes from
    # the Pacific time, not from the UTC date and not from row.date.
    "p_sched": _pin("2026-11-24", "evening", "Scheduled pin",
                    status=cp.SCHEDULED, post_id="b1",
                    due_at="2026-11-24T01:26:39.000Z"),
    "p_sent": _pin("2026-11-20", "morning", "Sent pin", status=cp.PUBLISHED,
                   post_id="b2", due_at="2026-11-20T17:00:00.000Z",
                   image_url="https://img.example/sent.png"),
    "p_failed": _pin("2026-11-27", "evening", "Failed pin", status=cp.FAILED,
                     errors=["board not found"]),
    "p_out_of_range": _pin("2027-02-01", "morning", "Far pin"),
    "p_undated": _pin("", "", "No date pin"),
}

LI_ROWS = {
    "l_commented": {"status": cp.COMMENTED, "post_id": "li1",
                    "text": "A LinkedIn post.",
                    "due_at": "2026-11-25T16:30:00.000Z",
                    "row": {"date": "2026-11-25", "time_window": "morning",
                            "post_text": "A LinkedIn post."}},
    "l_comment_failed": {"status": cp.COMMENT_FAILED, "post_id": "li2",
                         "text": "Live, no comment.",
                         "due_at": "2026-11-26T20:00:00.000Z",
                         "errors": ["comment box missing"],
                         "row": {"date": "2026-11-26",
                                 "time_window": "afternoon",
                                 "post_text": "Live, no comment."}},
}


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """A data root with a LinkedIn state file and a Pinterest one for 'p'."""
    monkeypatch.setattr(pm, "DATA_ROOT", str(tmp_path))
    (tmp_path / "p" / "pinterest").mkdir(parents=True)
    (tmp_path / "p" / cp.STATE_NAME).write_text(
        json.dumps({"rows": LI_ROWS}), encoding="utf-8")
    (tmp_path / "p" / "pinterest" / cp.STATE_NAME).write_text(
        json.dumps({"rows": PIN_ROWS}), encoding="utf-8")
    return tmp_path


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


class NetworkUsed(AssertionError):
    pass


@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect, DNS lookup or requests send raises NetworkUsed.

    Returns the list of attempts, so a test can assert it is empty even when
    the code under test swallowed the exception (the queue's reconcile does).
    """
    attempts = []

    def refuse(*a, **k):
        attempts.append((a, k))
        raise NetworkUsed("network access attempted: %r" % (a,))

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    import requests
    monkeypatch.setattr(requests.Session, "send", refuse)
    return attempts


def _cal(client, frm="2026-11-01", to="2026-11-30", **extra):
    q = "from=%s&to=%s" % (frm, to)
    for k, v in extra.items():
        q += "&%s=%s" % (k, v)
    return client.get("/api/scheduled/p/calendar?" + q)


# ── labels ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("window,label", [
    ("morning", "Tue 11-24 morning, 8-11 PT"),
    ("afternoon", "Tue 11-24 afternoon, 12-3 PT"),
    ("late_afternoon", "Tue 11-24 late afternoon, 3-5 PT"),
    ("evening", "Tue 11-24 evening, 5-8 PT"),
    ("night", "Tue 11-24 night, 8-10 PT"),
])
def test_planned_label_for_each_of_the_five_windows(window, label):
    assert cal.planned_label("2026-11-24", window) == label


def test_every_declared_window_has_a_label_case():
    # The parametrize above must cover every declared window, so a sixth
    # window added later fails here until it is given a label test.
    assert set(bc.TIME_WINDOWS) == {
        "morning", "afternoon", "late_afternoon", "evening", "night"}


def test_planned_label_edge_cases():
    assert cal.planned_label("", "morning") is None
    assert cal.planned_label(None, None) is None
    assert cal.planned_label("2026-13-01", "morning") is None
    assert cal.planned_label("2026-11-24", "") == "Tue 11-24"
    # An undeclared window is named as given, with no invented hours.
    assert cal.planned_label("2026-11-24", "brunch") == "Tue 11-24 brunch"
    assert cal.planned_label(" 2026-11-24 ", " Morning ") == \
        "Tue 11-24 morning, 8-11 PT"


# ── the queue row view ──────────────────────────────────────────────────────

def test_row_view_adds_date_window_planned_for_pending():
    v = dashboard._scheduled_row_view("k", PIN_ROWS["p_pending_early"])
    assert v["date"] == "2026-11-25"
    assert v["time_window"] == "morning"
    assert v["planned"] == "Wed 11-25 morning, 8-11 PT"
    assert v["due_at"] is None


def test_row_view_adds_date_window_planned_for_held():
    v = dashboard._scheduled_row_view("k", PIN_ROWS["p_held"])
    assert (v["date"], v["time_window"]) == ("2026-11-26", "afternoon")
    assert v["planned"] == "Thu 11-26 afternoon, 12-3 PT"
    assert v["hold_reason"] == "Buffer has no free slot"


def test_row_view_scheduled_keeps_due_at_and_gains_planned():
    v = dashboard._scheduled_row_view("k", PIN_ROWS["p_sched"])
    assert v["due_at"] == "2026-11-24T01:26:39.000Z"
    assert v["planned"] == "Tue 11-24 evening, 5-8 PT"


def test_row_view_without_a_row_has_none_not_a_guess():
    v = dashboard._scheduled_row_view("k", {"status": cp.FAILED, "text": "x"})
    assert v["date"] is None and v["time_window"] is None
    assert v["planned"] is None


def test_row_view_fields_are_additive():
    # Every key the queue had before U1 is still there with the same value.
    rec = PIN_ROWS["p_sent"]
    v = dashboard._scheduled_row_view("k", rec)
    for key in ("key", "status", "label", "preview", "full_text", "post_id",
                "due_at", "image_url", "errors", "has_post", "hold_reason"):
        assert key in v
    assert v["post_id"] == "b2" and v["has_post"] is True


# ── the calendar route ──────────────────────────────────────────────────────

def test_state_path_matches_pipeline_state(data_root):
    for platform in ("linkedin", "x", "pinterest"):
        expected = cp.PipelineState(profile_name="q", platform=platform).path
        assert cal.state_path("q", platform) == expected


def test_route_returns_every_platform_in_range_ordered(client, data_root,
                                                       no_network):
    r = _cal(client)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    got = [(i["day"], i["platform"], i["key"]) for i in d["items"]]
    assert got == [
        ("2026-11-20", "pinterest", "p_sent"),
        ("2026-11-23", "pinterest", "p_sched"),
        ("2026-11-25", "pinterest", "p_pending_early"),   # 08:00 window start
        ("2026-11-25", "linkedin", "l_commented"),         # 08:30 PST
        ("2026-11-25", "pinterest", "p_pending_late"),     # 20:00
        ("2026-11-26", "linkedin", "l_comment_failed"),    # 12:00 PST
        ("2026-11-26", "pinterest", "p_held"),             # 12:00 window
        ("2026-11-27", "pinterest", "p_failed"),
    ]
    assert [i["key"] for i in d["undated"]] == ["p_undated"]
    assert "p_out_of_range" not in [i["key"] for i in d["items"]]
    assert no_network == []


def test_route_groups_states_and_carries_reasons(client, data_root,
                                                 no_network):
    d = _cal(client).get_json()
    by = {i["key"]: i for i in d["items"]}
    assert by["p_pending_early"]["group"] == "queued"
    assert by["p_held"]["group"] == "held"
    assert by["p_held"]["hold_reason"] == "Buffer has no free slot"
    assert by["p_sched"]["group"] == "in_buffer"
    assert by["p_sent"]["group"] == "sent"
    assert by["l_commented"]["group"] == "sent"
    assert by["l_comment_failed"]["group"] == "sent"
    assert by["l_comment_failed"]["errors"] == ["comment box missing"]
    assert by["p_failed"]["group"] == "failed"
    assert by["p_failed"]["errors"] == ["board not found"]
    # scheduled_youtube is V1's: a YouTube video uploaded private until its
    # publishAt. No Buffer state maps to it.
    assert set(d["groups"]) == {"queued", "held", "in_buffer", "sent",
                                "failed", "scheduled_youtube"}
    # Every pipeline state has a group: none falls through as itself.
    assert set(cal.STATE_GROUPS) == {cp.PENDING, cp.HELD, cp.SCHEDULED,
                                     cp.PUBLISHED, cp.COMMENTED,
                                     cp.COMMENT_FAILED, cp.FAILED}


def test_route_item_fields(client, data_root, no_network):
    by = {i["key"]: i for i in _cal(client).get_json()["items"]}
    sent = by["p_sent"]
    assert sent["title"] == "Sent pin"
    assert sent["image_url"] == "https://img.example/sent.png"
    assert sent["platform_label"] == "Pinterest"
    assert sent["time_label"] == "09:00 PT"
    sched = by["p_sched"]
    assert sched["due_at"] == "2026-11-24T01:26:39.000Z"
    assert sched["time_label"] == "17:26 PT"
    pend = by["p_pending_late"]
    assert pend["due_at"] is None and pend["time_window"] == "night"
    assert pend["time_label"] == "night, 8-10 PT"
    # The chip's detail is the queue row's own view.
    assert pend["detail"] == dashboard._scheduled_row_view(
        "p_pending_late", PIN_ROWS["p_pending_late"])


def test_route_counts_per_day(client, data_root, no_network):
    d = _cal(client).get_json()
    assert d["counts"]["2026-11-25"] == {"queued": 2, "sent": 1}
    assert d["counts"]["2026-11-26"] == {"held": 1, "sent": 1}
    assert sum(sum(c.values()) for c in d["counts"].values()) == \
        len(d["items"])


def test_route_date_range_is_inclusive_both_ends(client, data_root,
                                                 no_network):
    d = _cal(client, "2026-11-25", "2026-11-25").get_json()
    assert sorted(i["key"] for i in d["items"]) == \
        ["l_commented", "p_pending_early", "p_pending_late"]
    d = _cal(client, "2026-11-28", "2026-12-31").get_json()
    assert d["items"] == []


def test_route_platform_filter(client, data_root, no_network):
    d = _cal(client, platform="linkedin").get_json()
    assert {i["platform"] for i in d["items"]} == {"linkedin"}
    assert d["undated"] == []
    assert _cal(client, platform="myspace").status_code == 400


@pytest.mark.parametrize("frm,to", [
    ("", "2026-11-30"), ("2026-11-01", ""), ("11/01/2026", "2026-11-30"),
    ("2026-11-30", "2026-11-01"), ("2026-01-01", "2028-01-01"),
])
def test_route_rejects_bad_ranges(client, data_root, frm, to):
    r = _cal(client, frm, to)
    assert r.status_code == 400
    assert r.get_json()["error"]


def test_route_rejects_path_profile(client, data_root):
    r = client.get("/api/scheduled/../calendar?from=2026-11-01&to=2026-11-30")
    assert r.status_code in (400, 404)
    r = client.get("/api/scheduled/..%5Cx/calendar?from=2026-11-01&to=2026-11-30")
    assert r.status_code in (400, 404)


def test_route_for_profile_with_no_files_is_empty_and_creates_nothing(
        client, data_root, no_network):
    r = client.get("/api/scheduled/nobody/calendar?from=2026-11-01&to=2026-11-30")
    assert r.status_code == 200
    assert r.get_json()["items"] == [] and r.get_json()["undated"] == []
    assert not (data_root / "nobody").exists()


def test_route_writes_nothing(client, data_root, no_network):
    def snapshot():
        out = {}
        for root, dirs, files in os.walk(data_root):
            for f in files:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[p] = (os.path.getmtime(p), fh.read())
            for dname in dirs:
                out[os.path.join(root, dname)] = "dir"
        return out

    before = snapshot()
    assert _cal(client).status_code == 200
    assert snapshot() == before


def test_route_makes_no_network_call_and_no_reconcile(client, data_root,
                                                      no_network,
                                                      monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the calendar must not reconcile with Buffer")

    monkeypatch.setattr(dashboard, "_reconcile_on_render", boom)
    monkeypatch.setattr(cp, "reconcile_published", boom)
    r = _cal(client)
    assert r.status_code == 200
    assert no_network == []


def test_no_network_guard_can_fail(no_network):
    # The guard must be able to say yes: a real connection attempt is caught
    # and recorded. Without this, an empty `no_network` proves nothing.
    with pytest.raises(NetworkUsed):
        socket.create_connection(("example.com", 80), timeout=1)
    import requests
    with pytest.raises(NetworkUsed):
        requests.get("https://api.buffer.com/", timeout=1)
    assert len(no_network) == 2


def test_no_network_guard_catches_a_source_that_calls_out(client, data_root,
                                                          no_network,
                                                          monkeypatch):
    import requests

    def chatty(profile):
        requests.get("https://api.buffer.com/", timeout=1)
        return []

    monkeypatch.setattr(dashboard, "_CALENDAR_SOURCES", [chatty])
    with pytest.raises(NetworkUsed):
        _cal(client)
    assert len(no_network) == 1


def test_sources_list_is_the_extension_point(client, data_root, no_network,
                                             monkeypatch):
    # V1 adds a source; the route does not change.
    def extra(profile):
        yield {"platform": "youtube", "key": "yt1", "day": "2026-11-23",
               "sort_minute": 720, "group": "in_buffer"}

    monkeypatch.setattr(dashboard, "_CALENDAR_SOURCES",
                        dashboard._CALENDAR_SOURCES + [extra])
    keys = [i["key"] for i in _cal(client).get_json()["items"]]
    assert "yt1" in keys and "p_sched" in keys


def test_collect_rejects_non_dates():
    with pytest.raises(TypeError):
        cal.collect("p", "2026-11-01", "2026-11-30", [])


def test_unreadable_state_file_is_an_error_not_an_empty_calendar(
        client, data_root):
    (data_root / "p" / "pinterest" / cp.STATE_NAME).write_text(
        "{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        list(cal.state_file_items("p"))


# ── the page ────────────────────────────────────────────────────────────────

def _calendar_js(html):
    start = html.index("// ─── Content calendar (U1)")
    end = html.index("// What happened to a row's image", start)
    return html[start:end]


def test_page_renders_with_the_panel(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="schpCalendarCard"' in html
    for el in ("schpCalGrid", "schpCalPlatform", "schpCalMonthBtn",
               "schpCalWeekBtn", "schpCalUndated", "schpCalDetail"):
        assert 'id="%s"' % el in html
    assert "schpLoadCalendar();" in html
    assert "/calendar?from=" in _calendar_js(html)


def test_panel_has_no_write_action(client):
    html = client.get("/").get_data(as_text=True)
    js = _calendar_js(html)
    card = html[html.index('id="schpCalendarCard"'):]
    card = card[:card.index("<!-- end mode-scheduled -->")]
    # Every fetch in the calendar is a plain GET.
    assert re.findall(r"fetch\(", js) == ["fetch("]
    assert "method" not in js
    for verb in ("POST", "DELETE", "PUT", "PATCH"):
        assert verb not in js
    # No drag, no edit, no delete, no call into the queue's write helpers.
    for word in ("draggable", "ondrop", "dragstart", "contenteditable",
                 "schpDelete", "schpToggleSelect", "<input", "<textarea"):
        assert word not in js and word not in card
    # The detail is the queue row in its read-only form.
    assert "schpRow(it.detail, true)" in js


def test_schprow_readonly_leaves_out_the_controls(client):
    html = client.get("/").get_data(as_text=True)
    body = html[html.index("function schpRow(r, readOnly)"):]
    body = body[:body.index("\n}\n")]
    assert "const del = readOnly === true ? '' :" in body
    assert "const check = readOnly === true ? '' :" in body
    # The queue's own rows keep their controls. schpRow must never be handed
    # to map() or forEach() bare: they pass the index as the second argument,
    # which would make every row after the first read-only.
    assert "rows.map(r => schpRow(r))" in html
    assert not re.search(r"\.(map|forEach|filter)\(\s*schpRow\s*\)", html)


def test_queue_row_shows_due_or_planned_in_pacific(client):
    html = client.get("/").get_data(as_text=True)
    body = html[html.index("function schpRow(r, readOnly)"):]
    assert "if (r.due_at) bits.push('due ' + schpWhen(r.due_at));" in body
    assert "else if (r.planned) bits.push('planned ' + r.planned);" in body
    when = html[html.index("function schpWhen(iso)"):]
    when = when[:when.index("\n}\n")]
    assert "timeZone: 'America/Los_Angeles'" in when
    assert "timeZoneName: 'short'" in when
