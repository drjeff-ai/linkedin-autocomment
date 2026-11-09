"""P2: extra columns, the ``link`` alias, row identity and per-channel validation.

What these pin, in the order the dispatch's gate lists them:

* No existing row's key moves, on any entry point (the golden corpus, with and
  without a channel).
* ``link`` and ``first_comment_link`` are one slot: same key, same placement,
  carried from CSV text all the way to the createPost input.
* A channel's declared extras change a row's key only when filled, in an order
  that does not matter, and never for a channel that did not declare them.
* ``queue_rows`` validates for the row's own channel, so X's 280 limit is
  enforced at queue time and nothing over it is written anywhere.

Extras are tested against a channel monkeypatched into ``channels.REGISTRY``:
no shipped channel declares one yet.
"""

import dataclasses
import json
import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from linkedin_automation import channels  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402

import test_platform_golden as golden  # noqa: E402

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
LINK = "https://example.com/reach"
TEST_CHANNEL = "testch"


def make_row(**over):
    row = {"date": "2036-09-08", "time_window": "morning",
           "post_text": "A post body.", "topic": "", "tags": "#a #b",
           "image_path": "", "first_comment_link": LINK}
    row.update(over)
    return row


def without_link_columns(row):
    return {k: v for k, v in row.items() if k != "first_comment_link"}


@pytest.fixture
def testch(monkeypatch):
    """A channel that declares extras, the way Pinterest will."""
    channel = channels.Channel(
        id=TEST_CHANNEL, label="TestCh", link_placement=channels.NO_LINK,
        text_limit=None, identity_check=channels.UNGATED,
        config_layout=channels.OVERLAY, key_env=None,
        state_path=channels.PLATFORM_DIR, reconcile_statuses=("sent",),
        extra_columns=(channels.ExtraColumn("board", required=True),
                       channels.ExtraColumn("title", limit=10),
                       channels.ExtraColumn("alt_text", limit=20)))
    monkeypatch.setitem(channels.REGISTRY, TEST_CHANNEL, channel)
    return channel


# --- the declaration -------------------------------------------------------

def test_a_channel_without_extras_declares_none():
    assert channels.LINKEDIN.extra_columns == ()
    assert channels.X.extra_columns == ()
    assert channels.undeclared("anything").extra_columns == ()


def test_a_duplicate_extra_name_is_refused():
    with pytest.raises(ValueError, match="declared twice"):
        channels.Channel(
            id="dup", label="Dup", link_placement=channels.NO_LINK,
            text_limit=None, identity_check=channels.UNGATED,
            config_layout=channels.OVERLAY, key_env=None,
            state_path=channels.PLATFORM_DIR, reconcile_statuses=("sent",),
            extra_columns=(channels.ExtraColumn("board"),
                           channels.ExtraColumn("board", required=True)))


def test_pinterest_stays_undeclared():
    assert "pinterest" not in channels.REGISTRY


# --- row identity: nothing existing moves ----------------------------------

def _golden_expected_keys():
    with open(golden.EXPECTED_FILE, encoding="utf-8") as f:
        expected = json.load(f)["rows"]
    return [(row_id, row, expected[row_id]["row_key"])
            for row_id, row in golden._load_rows()]


def test_every_golden_key_is_unchanged_with_and_without_a_channel():
    cases = _golden_expected_keys()
    assert len(cases) >= 40, "the corpus shrank; this would pass on nothing"
    for row_id, row, key in cases:
        assert cp.row_key(row, "linkedin") == key, row_id
        assert cp.row_key(row, "x") == key, row_id
        assert cp.row_key(row) == key, row_id


def test_a_key_check_can_go_red():
    """The comparison above must be able to fail: a changed row differs."""
    _, row, key = _golden_expected_keys()[0]
    assert cp.row_key(dict(row, post_text=row["post_text"] + "!"), "x") != key


def test_link_and_first_comment_link_are_one_slot():
    old = make_row()
    new = without_link_columns(old)
    new["link"] = LINK
    both = dict(old, link=LINK)
    assert cp.row_key(new) == cp.row_key(old) == cp.row_key(both)
    for platform in ("linkedin", "x"):
        assert cp.row_key(new, platform) == cp.row_key(old, platform)
    # and the link really is part of the identity
    assert cp.row_key(dict(new, link=LINK + "/2")) != cp.row_key(old)


def test_whitespace_in_the_link_does_not_move_a_key():
    assert cp.row_key(make_row(first_comment_link="  %s  " % LINK)) == \
        cp.row_key(make_row())


def test_extras_change_the_key_only_when_filled(testch):
    base = make_row()
    a = dict(base, board="b1", title="A")
    b = dict(base, board="b2", title="A")
    keys = {cp.row_key(r, TEST_CHANNEL) for r in (base, a, b)}
    assert len(keys) == 3, "rows differing only in an extra must differ"


def test_blank_and_absent_extras_are_one_key(testch):
    keys = {cp.row_key(r, TEST_CHANNEL) for r in
            (make_row(title="A"),
             make_row(title="A", board=""),
             make_row(title="A", board="   "),
             make_row(title="A", board=None))}
    assert len(keys) == 1


def test_every_extra_empty_is_the_base_key(testch):
    base = make_row()
    blank = dict(base, board="", title=" ", alt_text="")
    assert cp.row_key(blank, TEST_CHANNEL) == cp.row_key(base)


def test_declaration_order_does_not_change_a_key(testch, monkeypatch):
    row = make_row(board="b1", title="T", alt_text="alt")
    first = cp.row_key(row, TEST_CHANNEL)
    reordered = dataclasses.replace(
        testch, extra_columns=tuple(reversed(testch.extra_columns)))
    monkeypatch.setitem(channels.REGISTRY, TEST_CHANNEL, reordered)
    assert cp.row_key(row, TEST_CHANNEL) == first


def test_extras_are_a_pair_not_a_bare_value(testch):
    """board=X and title=X are different posts; name=value keeps them apart."""
    assert cp.row_key(make_row(board="X"), TEST_CHANNEL) != \
        cp.row_key(make_row(title="X"), TEST_CHANNEL)


def test_a_stray_column_never_changes_a_key():
    row = make_row()
    stray = dict(row, board="b1")
    for platform in (None, "linkedin", "x"):
        assert cp.row_key(stray, platform) == cp.row_key(row, platform)
    assert cp.unknown_columns(stray, "linkedin") == ["board"]
    assert cp.unknown_columns(stray, "x") == ["board"]
    assert cp.unknown_columns(stray) == ["board"]


def test_link_is_known_and_declared_extras_are_known_for_their_channel(testch):
    row = make_row(link=LINK, board="b1", stray="s")
    assert cp.unknown_columns(row, "linkedin") == ["board", "stray"]
    assert cp.unknown_columns(row, TEST_CHANNEL) == ["stray"]


# --- validation ------------------------------------------------------------

def test_a_required_extra_must_be_present(testch):
    for row in (make_row(), make_row(board=""), make_row(board="   ")):
        problems = cp.validate_row(row, now=NOW, platform=TEST_CHANNEL)
        assert any("TestCh requires board" in p for p in problems), row
    assert cp.validate_row(make_row(board="b1"), now=NOW,
                           platform=TEST_CHANNEL) == []


def test_an_extra_over_its_limit_is_rejected_and_never_trimmed(testch):
    at_limit = make_row(board="b", title="x" * 10)
    over = make_row(board="b", title="x" * 11)
    assert cp.validate_row(at_limit, now=NOW, platform=TEST_CHANNEL) == []
    problems = cp.validate_row(over, now=NOW, platform=TEST_CHANNEL)
    assert len(problems) == 1
    assert "TestCh title is 11 characters against a 10 limit" in problems[0]
    assert "never truncated" in problems[0]
    assert over["title"] == "x" * 11, "validation must not alter the row"


def test_an_extra_limit_counts_the_stripped_value(testch):
    padded = make_row(board="b", title="  " + "x" * 10 + "  ")
    assert cp.validate_row(padded, now=NOW, platform=TEST_CHANNEL) == []


def test_extras_are_not_validated_for_a_channel_that_did_not_declare_them(testch):
    for platform in (None, "linkedin", "x"):
        assert cp.validate_row(make_row(title="x" * 99), now=NOW,
                               platform=platform) == []


def test_differing_link_names_are_rejected_on_every_channel(testch):
    row = make_row(link="https://example.com/other")
    for platform in (None, "linkedin", "x", TEST_CHANNEL):
        problems = cp.validate_row(dict(row, board="b"), now=NOW,
                                   platform=platform)
        assert any("first_comment_link" in p and "link" in p and "differ" in p
                   for p in problems), platform


def test_equal_link_names_are_accepted():
    assert cp.validate_row(make_row(link=LINK), now=NOW) == []
    assert cp.validate_row(make_row(link="  %s " % LINK), now=NOW) == []


# --- queue_rows validates for the row's own channel ------------------------

def _x_row(length):
    """An X row whose counted length is exactly ``length`` (no tags, no link)."""
    return make_row(post_text="a" * length, tags="", first_comment_link="")


@pytest.fixture
def x_state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "x_state.json"), platform="x")


def test_x_accepts_280_and_rejects_281_at_queue_time(x_state):
    ok, rejected, _, _ = cp.queue_rows([_x_row(280)], x_state, now=NOW)
    assert (len(ok), len(rejected)) == (1, 0)
    rows_before = dict(x_state.rows)

    ok, rejected, _, _ = cp.queue_rows([_x_row(281)], x_state, now=NOW)
    assert (len(ok), len(rejected)) == (0, 1)
    assert "too long for X" in rejected[0]["errors"][0]
    assert x_state.rows == rows_before, "an over-limit row must be written nowhere"
    assert rejected[0]["key"] not in x_state.rows


def test_an_over_limit_x_row_is_written_nowhere_on_disk(tmp_path):
    path = tmp_path / "x_state.json"
    state = cp.PipelineState(path=str(path), platform="x")
    cp.queue_rows([_x_row(281)], state, now=NOW)
    assert state.rows == {}
    assert not path.exists() or json.loads(path.read_text("utf-8")) == {"rows": {}}


def test_x_counts_a_long_url_as_23(x_state):
    url = "https://example.com/" + "p" * 200
    pad = "a" * (280 - 23 - 2)            # two newlines join text and link
    row = make_row(post_text=pad, tags="", first_comment_link=url)
    ok, rejected, _, _ = cp.queue_rows([row], x_state, now=NOW)
    assert (len(ok), len(rejected)) == (1, 0), rejected
    over = make_row(post_text=pad + "a", tags="", first_comment_link=url)
    ok, rejected, _, _ = cp.queue_rows([over], x_state, now=NOW)
    assert len(rejected) == 1 and "too long for X" in rejected[0]["errors"][0]


def test_an_over_limit_row_already_in_state_reads_rejected_not_skipped(x_state):
    """Validation runs before the duplicate check (A-024 F-11)."""
    row = _x_row(281)
    x_state.update(cp.row_key(row, "x"), status=cp.PENDING, row=dict(row))
    ok, rejected, skipped, _ = cp.queue_rows([row], x_state, now=NOW)
    assert (len(ok), len(rejected), len(skipped)) == (0, 1, 0)
    # and the same row at the limit is still just skipped as already queued
    fine = _x_row(280)
    x_state.update(cp.row_key(fine, "x"), status=cp.PENDING, row=dict(fine))
    ok, rejected, skipped, _ = cp.queue_rows([fine], x_state, now=NOW)
    assert (len(ok), len(rejected), len(skipped)) == (0, 0, 1)


def test_linkedin_stays_unbounded_at_queue_time(tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "li.json"))
    ok, rejected, _, _ = cp.queue_rows([_x_row(5000)], state, now=NOW)
    assert (len(ok), len(rejected)) == (1, 0)


def test_a_queued_row_and_a_scheduled_row_share_one_key(testch, tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "t.json"),
                             platform=TEST_CHANNEL)
    row = make_row(board="b1", title="T")
    ok, _, _, _ = cp.queue_rows([row], state, now=NOW)
    assert len(ok) == 1
    queued_key = ok[0]["key"]
    assert queued_key == cp.row_key(row, TEST_CHANNEL)

    session = _Session()
    results = cp.schedule_pass(
        cp.pending_rows(state), "chan", state, key="k", session=session,
        now=NOW, platform=TEST_CHANNEL, generate=lambda *a, **k: "g")
    assert [r["status"] for r in results] == [cp.SCHEDULED]
    assert list(state.rows) == [queued_key], "one entry, no second row"
    assert state.rows[queued_key]["status"] == cp.SCHEDULED
    assert len(session.inputs) == 1


# --- the link, from CSV text to the createPost input ------------------------

CSV_LINK_ONLY = (
    "date,time_window,post_text,topic,tags,image_path,link\n"
    "2036-09-08,morning,Body text,,#a,,%s\n" % LINK)


class _Resp(object):
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Session(object):
    def __init__(self):
        self.inputs = []

    def post(self, url, headers=None, json=None, timeout=None):
        payload = (json or {}).get("variables", {}).get("input")
        self.inputs.append(payload)
        return _Resp({"data": {"createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post-%d" % len(self.inputs), "status": "scheduled",
                     "dueAt": (payload or {}).get("dueAt")}}}})


def _csv_to_create_post(platform, tmp_path, monkeypatch, text=CSV_LINK_ONLY):
    monkeypatch.setattr(cp, "verify_identity", lambda *a, **k: (True, "ok"))
    rows = cp.parse_rows(text)
    state = cp.PipelineState(path=str(tmp_path / ("s_%s.json" % platform)),
                             platform=platform)
    ok, rejected, skipped, warnings = cp.queue_rows(rows, state, now=NOW)
    assert (len(ok), len(rejected), len(skipped)) == (1, 0, 0), rejected
    assert warnings == [w for w in warnings if "link" not in w], warnings
    session = _Session()
    results = cp.schedule_pass(
        cp.pending_rows(state), "chan", state, key="k", session=session,
        now=NOW, platform=platform, identity_slug="someone",
        allow_production=True)
    assert [r["status"] for r in results] == [cp.SCHEDULED], results
    return state, session


def test_a_link_only_csv_puts_the_link_in_the_x_post_text(tmp_path, monkeypatch):
    state, session = _csv_to_create_post("x", tmp_path, monkeypatch)
    assert len(session.inputs) == 1
    assert LINK in session.inputs[0]["text"]
    assert len(state.rows) == 1
    entry = next(iter(state.rows.values()))
    assert entry["first_comment_link"] == LINK


def test_a_link_only_csv_keeps_the_link_out_of_the_linkedin_post_but_owes_the_comment(
        tmp_path, monkeypatch):
    state, session = _csv_to_create_post("linkedin", tmp_path, monkeypatch)
    assert len(session.inputs) == 1
    assert LINK not in session.inputs[0]["text"]
    assert len(state.rows) == 1
    entry = next(iter(state.rows.values()))
    assert entry["first_comment_link"] == LINK, "the browser pass reads this"


def test_the_link_path_can_go_red(tmp_path, monkeypatch):
    """Without any link column nothing is owed and nothing is in the body."""
    text = CSV_LINK_ONLY.replace(",link\n", ",\n").replace(LINK, "")
    state, session = _csv_to_create_post("x", tmp_path, monkeypatch, text)
    assert LINK not in session.inputs[0]["text"]
    assert next(iter(state.rows.values()))["first_comment_link"] == ""


# --- the dashboard form route ----------------------------------------------

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def form_env(tmp_path, monkeypatch):
    def data_dir(profile_name=None, platform=None):
        return str(tmp_path / platform) if platform else str(tmp_path)
    monkeypatch.setattr(cp.pm, "get_data_dir", data_dir)
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    return tmp_path


def _stored_rows(path):
    with open(path, encoding="utf-8") as f:
        return [e["row"] for e in json.load(f)["rows"].values()]


FORM = {"date": "2036-09-08", "time_window": "morning",
        "post_text": "Form body.", "topic": "", "tags": "#a #b",
        "image_path": "", "first_comment_link": ""}


def test_a_link_in_the_form_body_reaches_the_stored_row(client, form_env):
    body = dict(FORM, link=LINK)
    resp = client.post("/api/scheduled/p/rows", json=body)
    assert resp.get_json()["counts"]["accepted"] == 1
    (row,) = _stored_rows(form_env / "scheduled_posts_state.json")
    assert row["link"] == LINK


def test_a_form_row_without_a_link_stores_exactly_the_columns(client, form_env):
    resp = client.post("/api/scheduled/p/rows", json=dict(FORM))
    assert resp.get_json()["counts"]["accepted"] == 1
    (row,) = _stored_rows(form_env / "scheduled_posts_state.json")
    assert tuple(row) == cp.COLUMNS


def test_a_stray_form_field_changes_neither_the_row_nor_its_key(client, form_env):
    plain = client.post("/api/scheduled/p/rows", json=dict(FORM)).get_json()
    state_file = form_env / "scheduled_posts_state.json"
    state_file.unlink()
    stray = client.post("/api/scheduled/p/rows",
                        json=dict(FORM, board="b1", junk="j")).get_json()
    assert stray["accepted"][0]["key"] == plain["accepted"][0]["key"]
    (row,) = _stored_rows(state_file)
    assert tuple(row) == cp.COLUMNS


def test_the_form_keeps_a_declared_extra_only_for_its_channel(client, form_env,
                                                              testch):
    body = dict(FORM, board="b1", platform=TEST_CHANNEL)
    resp = client.post("/api/scheduled/p/rows", json=body).get_json()
    assert resp["counts"]["accepted"] == 1, resp
    (row,) = _stored_rows(form_env / TEST_CHANNEL / "scheduled_posts_state.json")
    assert row["board"] == "b1"


def test_the_x_rejection_reaches_the_json_response(client, form_env):
    body = dict(FORM, post_text="a" * 281, tags="", platform="x")
    resp = client.post("/api/scheduled/p/rows", json=body).get_json()
    assert resp["counts"]["accepted"] == 0
    assert "too long for X" in resp["rejected"][0]["errors"][0]
    state_file = form_env / "x" / "scheduled_posts_state.json"
    assert not state_file.exists() or _stored_rows(state_file) == []
