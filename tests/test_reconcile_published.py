"""Poll-on-render: the queue must tell the truth without a loop being on.

Buffer publishes on its own timetable and nothing here notices unless something
asks. Until now the only thing that asked was a background sweep, so a post that
went out two days ago still read "created in Buffer, not published yet" whenever
the sweeper happened to be switched off. The truth depended on a toggle.

The cost discipline is not decoration. The free plan allows 3,000 requests per
30 days - about a hundred a DAY for everything - and the account was already
rate-limited by a feeder polling every 15 minutes. A reconcile that asked per
post would cost more than the work it is supporting.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402

LINK = "https://www.linkedin.com/feed/update/urn:li:share:1010101010101010101"


def _sent(post_id, link=LINK, at="2026-09-11T16:51:23.000Z"):
    return {"id": post_id, "status": "sent", "sentAt": at,
            "externalLink": link, "channelId": "chan1"}


def _state(tmp_path, rows):
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    for key, fields in rows.items():
        state.update(key, **fields)
    return state


def _fetch(mapping, calls=None):
    def fetch(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        if calls is not None:
            calls.append({"channel": channel_id, "statuses": tuple(statuses)})
        return mapping
    return fetch


# ─── it notices what Buffer already did ───────────────────────────────────────

def test_a_post_buffer_already_sent_becomes_published(tmp_path):
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED, "post_id": "p1"}})
    changed = cp.reconcile_published(state, "chan1", fetch=_fetch({"p1": _sent("p1")}))
    assert len(changed) == 1
    assert state.get("a")["status"] == cp.PUBLISHED


def test_it_captures_the_permalink(tmp_path):
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED, "post_id": "p1"}})
    cp.reconcile_published(state, "chan1", fetch=_fetch({"p1": _sent("p1")}))
    assert state.get("a")["permalink"] == LINK


def test_it_records_when_buffer_sent_it(tmp_path):
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED, "post_id": "p1"}})
    cp.reconcile_published(state, "chan1", fetch=_fetch({"p1": _sent("p1")}))
    assert state.get("a")["published_at"] == "2026-09-11T16:51:23.000Z"


def test_the_change_survives_a_reload(tmp_path):
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED, "post_id": "p1"}})
    cp.reconcile_published(state, "chan1", fetch=_fetch({"p1": _sent("p1")}))
    again = cp.PipelineState(path=str(tmp_path / "s.json"))
    assert again.get("a")["status"] == cp.PUBLISHED


def test_a_post_buffer_has_not_sent_is_left_alone(tmp_path):
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED, "post_id": "p1"}})
    assert cp.reconcile_published(state, "chan1", fetch=_fetch({})) == []
    assert state.get("a")["status"] == cp.SCHEDULED


def test_silence_about_one_row_does_not_touch_the_others(tmp_path):
    state = _state(tmp_path, {
        "a": {"status": cp.SCHEDULED, "post_id": "p1"},
        "b": {"status": cp.SCHEDULED, "post_id": "p2"}})
    cp.reconcile_published(state, "chan1", fetch=_fetch({"p1": _sent("p1")}))
    assert state.get("a")["status"] == cp.PUBLISHED
    assert state.get("b")["status"] == cp.SCHEDULED


def test_an_existing_permalink_is_not_overwritten_with_a_blank(tmp_path):
    """Buffer writes status and externalLink separately, so a post can read
    `sent` a moment before the link exists."""
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED, "post_id": "p1",
                                    "permalink": LINK}})
    node = _sent("p1", link="")
    cp.reconcile_published(state, "chan1", fetch=_fetch({"p1": node}))
    assert state.get("a")["permalink"] == LINK
    assert state.get("a")["status"] == cp.PUBLISHED


# ─── it stays cheap ───────────────────────────────────────────────────────────

def test_it_costs_one_request_for_any_number_of_rows(tmp_path):
    calls = []
    rows = {"k%d" % i: {"status": cp.SCHEDULED, "post_id": "p%d" % i}
            for i in range(25)}
    state = _state(tmp_path, rows)
    cp.reconcile_published(state, "chan1",
                           fetch=_fetch({"p3": _sent("p3")}, calls))
    assert len(calls) == 1, "asked Buffer once per post instead of once"


def test_it_asks_for_nothing_when_no_row_is_awaiting_publication(tmp_path):
    calls = []
    state = _state(tmp_path, {
        "done": {"status": cp.COMMENTED, "post_id": "p1"},
        "dead": {"status": cp.FAILED},
        "held": {"status": cp.HELD}})
    assert cp.reconcile_published(state, "chan1", fetch=_fetch({}, calls)) == []
    assert calls == [], "spent a request with nothing to reconcile"


@pytest.mark.parametrize("status", [cp.PUBLISHED, cp.COMMENTED,
                                    cp.COMMENT_FAILED, cp.FAILED, cp.HELD,
                                    cp.PENDING])
def test_only_scheduled_rows_are_ever_re_polled(tmp_path, status):
    """A finished post tells us nothing new, so the cost does not grow as the
    calendar fills up with completed work."""
    calls = []
    state = _state(tmp_path, {"a": {"status": status, "post_id": "p1"}})
    cp.reconcile_published(state, "chan1", fetch=_fetch({"p1": _sent("p1")}, calls))
    assert calls == []
    assert state.get("a")["status"] == status


def test_a_row_with_no_post_id_is_not_looked_up(tmp_path):
    calls = []
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED}})
    assert cp.reconcile_published(state, "chan1", fetch=_fetch({}, calls)) == []
    assert calls == []


def test_it_only_asks_for_sent_posts(tmp_path):
    calls = []
    state = _state(tmp_path, {"a": {"status": cp.SCHEDULED, "post_id": "p1"}})
    cp.reconcile_published(state, "chan1", fetch=_fetch({}, calls))
    assert calls[0]["statuses"] == ("sent",)


# ─── the render path ──────────────────────────────────────────────────────────

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(cp.pm, "get_data_dir",
                        lambda profile_name=None, *a: str(tmp_path))
    monkeypatch.setattr(dashboard.pm, "get_profile_config",
                        lambda name=None: {"scheduled_posting": {
                            "buffer_channel_id": "chan1",
                            "identity_slug": "example-person"}})
    dashboard._reconcile_cache.clear()
    yield tmp_path
    dashboard._reconcile_cache.clear()


def test_opening_the_queue_reconciles_without_any_loop(client, wired, monkeypatch):
    """The whole point: no sweeper, no drain, just a page load."""
    state = cp.PipelineState(path=str(wired / "scheduled_posts_state.json"))
    state.update("a", status=cp.SCHEDULED, post_id="p1", text="A post.")
    monkeypatch.setattr(bc, "posts_by_status",
                        lambda *a, **k: {"p1": _sent("p1")})

    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["counts"][cp.PUBLISHED] == 1
    assert d["counts"][cp.SCHEDULED] == 0
    assert d["reconcile"]["changed"] == 1


def test_a_published_post_no_longer_reads_as_about_to_publish(client, wired,
                                                               monkeypatch):
    state = cp.PipelineState(path=str(wired / "scheduled_posts_state.json"))
    state.update("a", status=cp.SCHEDULED, post_id="p1", text="A post.")
    monkeypatch.setattr(bc, "posts_by_status",
                        lambda *a, **k: {"p1": _sent("p1")})

    d = client.get("/api/scheduled/p/queue").get_json()
    row = d["posts"][cp.PUBLISHED][0]
    assert row["label"] == "Published"
    assert "not published yet" not in row["meaning"]
    assert "live on LinkedIn" in row["meaning"]
    assert "comment" in row["action"]


def test_the_render_is_cached_so_it_cannot_hammer_buffer(client, wired,
                                                          monkeypatch):
    calls = []
    state = cp.PipelineState(path=str(wired / "scheduled_posts_state.json"))
    state.update("a", status=cp.SCHEDULED, post_id="p1")

    def fetch(*a, **k):
        calls.append(1)
        return {}

    monkeypatch.setattr(bc, "posts_by_status", fetch)
    for _ in range(8):
        client.get("/api/scheduled/p/queue")
    assert len(calls) == 1, "%d requests for 8 renders" % len(calls)


def test_a_rate_limit_leaves_the_queue_readable(client, wired, monkeypatch):
    """A blank screen is worse than a stale one, and the stale one is labelled."""
    state = cp.PipelineState(path=str(wired / "scheduled_posts_state.json"))
    state.update("a", status=cp.SCHEDULED, post_id="p1", text="A post.")

    def limited(*a, **k):
        raise bc.BufferRateLimited("Buffer is rate-limiting this client")

    monkeypatch.setattr(bc, "posts_by_status", limited)
    r = client.get("/api/scheduled/p/queue")
    assert r.status_code == 200
    d = r.get_json()
    assert d["counts"][cp.SCHEDULED] == 1
    assert "rate-limiting" in d["reconcile"]["error"]


def test_any_buffer_outage_leaves_the_queue_readable(client, wired, monkeypatch):
    state = cp.PipelineState(path=str(wired / "scheduled_posts_state.json"))
    state.update("a", status=cp.SCHEDULED, post_id="p1", text="A post.")
    monkeypatch.setattr(bc, "posts_by_status",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("connection reset")))
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["counts"][cp.SCHEDULED] == 1
    assert "Could not reach Buffer" in d["reconcile"]["error"]


def test_no_channel_configured_is_not_an_error(client, wired, monkeypatch):
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["reconcile"]["error"] is None
    assert "no Buffer channel" in d["reconcile"]["note"]


def test_the_force_endpoint_bypasses_the_cache(client, wired, monkeypatch):
    calls = []
    state = cp.PipelineState(path=str(wired / "scheduled_posts_state.json"))
    state.update("a", status=cp.SCHEDULED, post_id="p1")
    monkeypatch.setattr(bc, "posts_by_status",
                        lambda *a, **k: calls.append(1) or {})
    client.get("/api/scheduled/p/queue")
    client.post("/api/scheduled/p/reconcile")
    assert len(calls) == 2


# ─── the sweeper still picks these up ─────────────────────────────────────────

def test_a_post_that_published_while_the_sweeper_was_off_is_not_skipped(tmp_path):
    """Reconciling moves rows to PUBLISHED. If the sweep only looked at
    SCHEDULED, reconciling would quietly orphan every one of them."""
    assert cp.PUBLISHED in (cp.SCHEDULED, cp.PUBLISHED, cp.COMMENT_FAILED)
    state = _state(tmp_path, {"a": {"status": cp.PUBLISHED, "post_id": "p1",
                                    "permalink": LINK,
                                    "first_comment_link": "https://example.com"}})
    swept = [k for k, e in state.rows.items()
             if e.get("status") in (cp.SCHEDULED, cp.PUBLISHED,
                                    cp.COMMENT_FAILED)]
    assert swept == ["a"]


def test_reconciling_first_makes_the_sweep_cheaper(tmp_path, monkeypatch):
    """A reconciled row already carries its permalink, so the sweep does not
    spend a per-post request rediscovering it."""
    state = _state(tmp_path, {"a": {"status": cp.PUBLISHED, "post_id": "p1",
                                    "permalink": LINK,
                                    "first_comment_link": ""}})

    def boom(*a, **k):
        raise AssertionError("the sweep re-polled a post it already had a link for")

    monkeypatch.setattr(cp.bc, "get_post", boom)
    results = cp.comment_pass(state, profile_name="p")
    assert results and results[0]["key"] == "a"


def test_a_commented_row_is_never_swept_again(tmp_path):
    state = _state(tmp_path, {"a": {"status": cp.COMMENTED, "post_id": "p1"}})
    swept = [k for k, e in state.rows.items()
             if e.get("status") in (cp.SCHEDULED, cp.PUBLISHED,
                                    cp.COMMENT_FAILED)]
    assert swept == []


# ─── the row says where it is, in words ───────────────────────────────────────

def _template():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "linkedin_automation", "templates", "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_row_renders_its_meaning():
    """The badge names the state; the meaning says what the name means, so
    "Published" cannot be misread as "about to publish"."""
    html = _template()
    assert "const meaning = r.meaning" in html
    assert "meaning + img.line + link" in html


def test_every_state_has_a_meaning_to_render():
    for status, help_ in dashboard._SCHEDULED_STATE_HELP.items():
        assert help_.get("meaning"), status


def test_published_and_scheduled_never_read_alike():
    pub = dashboard._SCHEDULED_STATE_HELP[cp.PUBLISHED]
    sch = dashboard._SCHEDULED_STATE_HELP[cp.SCHEDULED]
    assert "live on LinkedIn" in pub["meaning"]
    assert "not published yet" in sch["meaning"]
    assert pub["meaning"] != sch["meaning"]
    assert pub["label"] != sch["label"]


# ─── the slot-count cache, and the one thing it must never do ────────────────
#
# The slot count is read on every render of the scheduled tab, which is far more
# often than it can meaningfully change. But it also decides what actually gets
# SENT, and acting on a stale count is exactly what HELD exists to prevent - so
# the cache is for the status line, and the send paths opt out.

@pytest.fixture
def slot_calls(monkeypatch):
    calls = []

    def fake_gql(query, variables=None, key=None, label="graphql", session=None):
        calls.append(label)
        if label == "getChannel":
            return {"data": {"channel": {"id": "chan1", "name": "x",
                                         "organizationId": "org1"}}}
        return {"data": {
            "account": {"organizations": [
                {"id": "org1", "limits": {"scheduledPosts": 10}}]},
            "posts": {"edges": [{"node": {"id": "p1", "channelId": "chan1"}}]}}}

    monkeypatch.setattr(bc, "gql", fake_gql)
    bc.clear_caches()
    yield calls
    bc.clear_caches()


def test_repeated_status_reads_share_one_request(slot_calls):
    for _ in range(6):
        bc.scheduled_slots("chan1")
    assert slot_calls.count("scheduledSlots") == 1, slot_calls


def test_the_channel_is_not_re_resolved_for_every_slot_check(slot_calls):
    """181 getChannel calls in a day came from exactly this."""
    for _ in range(6):
        bc.scheduled_slots("chan1", max_age=0)
    assert slot_calls.count("getChannel") == 1
    assert slot_calls.count("scheduledSlots") == 6


def test_a_send_path_never_acts_on_a_cached_count(slot_calls):
    bc.scheduled_slots("chan1")
    before = slot_calls.count("scheduledSlots")
    bc.scheduled_slots("chan1", max_age=0)
    assert slot_calls.count("scheduledSlots") == before + 1


def test_the_cached_answer_is_the_same_answer(slot_calls):
    first = bc.scheduled_slots("chan1")
    second = bc.scheduled_slots("chan1")
    assert first == second


def test_a_caller_cannot_mutate_the_cached_entry(slot_calls):
    """A returned dict that aliases the cache would let one caller corrupt the
    count every later caller sees."""
    first = bc.scheduled_slots("chan1")
    first["free"] = 999
    assert bc.scheduled_slots("chan1")["free"] != 999


def test_clear_caches_forces_a_fresh_read(slot_calls):
    bc.scheduled_slots("chan1")
    bc.clear_caches()
    bc.scheduled_slots("chan1")
    assert slot_calls.count("scheduledSlots") == 2


def test_the_send_paths_in_the_dashboard_ask_for_a_fresh_count():
    """Asserted on the source: this is a correctness rule, not a preference."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "linkedin_automation", "dashboard.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for fn in ("_scheduled_schedule_job", "_drain_feed_job"):
        start = src.index("def %s(" % fn)
        body = src[start:start + 2500]
        assert "scheduled_slots(channel_id, max_age=0)" in body, fn
