"""P5: Pinterest pins through Buffer, from the channel declaration.

Offline. Every Buffer boundary is a recorder: the GraphQL session, the channel
fetch, the board fetch and the image upload. Each claim is asserted on what
WOULD have gone to Buffer, starting from a CSV row.

What these pin, in the order of the P5 gate:

* gate 2: a Pinterest row through ``queue_rows`` then ``schedule_pass`` sends
  the createPost input pin.publisher sends for the same row (its
  ``build_create_post_input``, transcribed below), apart from ``mode``,
  ``schedulingType`` and ``dueAt``;
* gate 3: the board is matched exactly on its name and addressed by Buffer's
  ``serviceId``; an unknown board is refused at queue time with the board
  list, and a board gone by schedule time fails the row at stage ``board``
  with nothing uploaded or created; a board read that fails sends nothing;
* gate 4: the Pinterest row rules, each refused with its message and written
  nowhere;
* gate 5: held Pinterest rows feed through the X1 drain with Pinterest's own
  key and channel, in the same check as LinkedIn and X;
* identity: the same ``BUFFER_HANDLE`` gate and production guard as X, and no
  credential fallback.
"""

import json
import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import channels  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import post_drain as pd  # noqa: E402

import test_x_drain as xd  # noqa: E402

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
IMAGE = os.path.join(HERE, "fixtures", "platform_golden", "pixel.png")
HOSTED = "https://img.test.invalid/pixel.png"
LINK = "https://example.com/pin-target"
HANDLE = "synthetic_pinner"
CHAN = "chan_pin"
DESCRIPTION = "A synthetic pin description."

#: Buffer's own ids differ from the boards' serviceIds on purpose: a pin
#: addressed by Buffer's id would go nowhere, and these tell the two apart.
BOARDS = [{"id": "buf-1", "serviceId": "svc-111", "name": "secret"},
          {"id": "buf-2", "serviceId": "svc-222", "name": "Recipes"}]


def pin_row(**over):
    row = {"date": "2036-09-08", "time_window": "morning",
           "post_text": DESCRIPTION, "topic": "", "tags": "",
           "image_path": IMAGE, "first_comment_link": "", "link": LINK,
           "title": "A pin title", "board": "secret",
           "alt_text": "A red square."}
    row.update(over)
    return row


def pin_publisher_input(*, channel_id, board_service_id, text, title, link,
                        image_url, alt_text, due_at):
    """pin.publisher's ``build_create_post_input`` (buffer_publisher.py:130-174)
    with its ``build_pinterest_metadata`` (pinterest_adapter.py:40-65),
    transcribed for the scheduled branch. The reference the request is
    compared against, field for field."""
    image_asset = {"url": image_url}
    if alt_text.strip():
        image_asset["metadata"] = {"altText": alt_text.strip()}
    pinterest = {"boardServiceId": board_service_id.strip()}
    if title.strip():
        pinterest["title"] = title.strip()
    if link.strip():
        pinterest["url"] = link.strip()
    return {
        "assets": [{"image": image_asset}],
        "channelId": channel_id,
        "needsApproval": False,
        "metadata": {"pinterest": pinterest},
        "text": text,
        "mode": "customScheduled",
        "schedulingType": "automatic",
        "dueAt": due_at,
    }


# ─── a recorded Buffer ───────────────────────────────────────────────────────

class _Resp(object):
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class Buffer(object):
    """A fake GraphQL session. Answers getChannelBoards and createPost by
    the query each request carries, and records both. Never networked."""

    def __init__(self, boards=None):
        self.boards = BOARDS if boards is None else boards
        self.board_reads = []
        self.inputs = []
        self.keys = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.keys.append((headers or {}).get("Authorization"))
        query = (json or {}).get("query") or ""
        variables = (json or {}).get("variables") or {}
        if "GetChannelBoards" in query:
            self.board_reads.append(variables.get("channelId"))
            return _Resp({"data": {"channel": {
                "id": variables.get("channelId"),
                "metadata": {"boards": [dict(b, url="u", description="d")
                                        for b in self.boards]}}}})
        payload = variables.get("input")
        self.inputs.append(payload)
        return _Resp({"data": {"createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post-%d" % len(self.inputs), "status": "scheduled",
                     "dueAt": (payload or {}).get("dueAt")}}}})


class Uploads(object):
    def __init__(self):
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return HOSTED


def _channel(handle=HANDLE, service="pinterest"):
    calls = []

    def fetch(channel_id, key=None, session=None):
        calls.append((channel_id, key))
        return {"id": channel_id, "name": handle, "service": service}
    fetch.calls = calls
    return fetch


def _state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "pinterest.json"),
                            platform="pinterest")


def _schedule(rows, state, buffer, uploads=None, fetch_channel=None, **kw):
    uploads = uploads if uploads is not None else Uploads()
    kw.setdefault("allow_production", False)
    results = cp.schedule_pass(
        rows, CHAN, state, key="pin-key", session=buffer, upload=uploads,
        now=NOW, platform="pinterest", identity_slug=HANDLE,
        fetch_channel=fetch_channel or _channel(),
        generate=lambda *a, **k: pytest.fail("nothing generates pin text"),
        **kw)
    return results, uploads


# ─── the declaration ─────────────────────────────────────────────────────────

def test_pinterest_is_declared_after_x_with_the_dispatch_fields():
    assert list(channels.REGISTRY)[:3] == ["linkedin", "x", "pinterest"]
    pin = channels.get("pinterest")
    assert pin.label == "Pinterest"
    assert pin.link_placement == channels.METADATA
    assert pin.text_limit == channels.TextLimit(unit="chars", maximum=500,
                                                url_cost=None)
    assert pin.identity_check == channels.BUFFER_HANDLE
    assert pin.config_layout == channels.OVERLAY
    assert pin.key_env is None
    assert pin.state_path == channels.PLATFORM_DIR
    assert pin.reconcile_statuses == ("sent", "error")


def test_counted_length_with_no_url_cost_is_plain_characters():
    """Sprint note 4: a Pinterest description counts every character."""
    text = "x" * 10 + " https://example.com/" + "a" * 100
    assert cp.counted_length(text, None) == len(text)


def test_the_builder_puts_title_and_url_in_only_when_filled():
    build = channels.PINTEREST.metadata_builder
    assert build(pin_row(), "svc") == {"pinterest": {
        "boardServiceId": "svc", "title": "A pin title", "url": LINK}}
    assert build(pin_row(title="", link=""), "svc") == {
        "pinterest": {"boardServiceId": "svc"}}
    assert build(pin_row(link="", first_comment_link=LINK), "svc")[
        "pinterest"]["url"] == LINK


@pytest.mark.parametrize("sid", [None, "", "   "])
def test_the_builder_refuses_a_blank_board(sid):
    with pytest.raises(channels.MissingBoard):
        channels.PINTEREST.metadata_builder(pin_row(), sid)


def test_the_pin_body_never_carries_the_link():
    assert cp.compose_for_platform(DESCRIPTION, "", LINK, "pinterest") == DESCRIPTION


# ─── gate 2: the createPost input, against pin.publisher's ───────────────────

def test_a_pinterest_row_reaches_buffer_in_pin_publishers_shape(tmp_path):
    state = _state(tmp_path)
    accepted, rejected, _s, _w = cp.queue_rows([pin_row()], state, now=NOW,
                                               boards=BOARDS)
    assert rejected == [] and len(accepted) == 1
    buffer = Buffer()
    results, uploads = _schedule(cp.pending_rows(state), state, buffer)

    assert [r["status"] for r in results] == [cp.SCHEDULED]
    assert uploads.paths == [IMAGE]
    assert len(buffer.inputs) == 1
    sent = buffer.inputs[0]
    expected = pin_publisher_input(
        channel_id=CHAN, board_service_id="svc-111", text=DESCRIPTION,
        title="A pin title", link=LINK, image_url=HOSTED,
        alt_text="A red square.", due_at=sent["dueAt"])
    assert sent == expected
    # The parts the acceptance criteria name, spelled out.
    assert sent["text"] == DESCRIPTION and LINK not in sent["text"]
    assert sent["metadata"] == {"pinterest": {
        "boardServiceId": "svc-111", "title": "A pin title", "url": LINK}}
    assert sent["assets"] == [{"image": {
        "url": HOSTED, "metadata": {"altText": "A red square."}}}]


def test_the_default_board_read_uses_the_passes_key_once_per_pass(tmp_path):
    state = _state(tmp_path)
    rows = [pin_row(), pin_row(title="Second pin", date="2036-09-09")]
    buffer = Buffer()
    results, _u = _schedule(rows, state, buffer)
    assert [r["status"] for r in results] == [cp.SCHEDULED] * 2
    assert buffer.board_reads == [CHAN], "one read per pass, not per row"
    assert set(buffer.keys) == {"Bearer pin-key"}


def test_channel_boards_returns_id_service_id_and_name_and_never_caches():
    buffer = Buffer()
    first = bc.channel_boards(CHAN, key="k", session=buffer)
    assert first == BOARDS
    buffer.boards = BOARDS[:1]
    assert bc.channel_boards(CHAN, key="k", session=buffer) == BOARDS[:1]
    assert buffer.board_reads == [CHAN, CHAN]


def test_channel_boards_refuses_a_channel_with_no_pinterest_metadata():
    class NotPinterest(Buffer):
        def post(self, url, headers=None, json=None, timeout=None):
            return _Resp({"data": {"channel": {"id": CHAN, "metadata": {}}}})
    with pytest.raises(bc.BufferError, match="not a Pinterest channel"):
        bc.channel_boards(CHAN, key="k", session=NotPinterest())


def test_the_board_query_is_pin_publishers():
    assert "query GetChannelBoards($channelId: ChannelId!)" in bc.Q_CHANNEL_BOARDS
    for field in ("serviceId", "name", "... on PinterestMetadata"):
        assert field in bc.Q_CHANNEL_BOARDS


# ─── gate 3: the board ───────────────────────────────────────────────────────

def test_an_exact_board_name_resolves_to_its_service_id_not_buffers_id():
    assert bc.resolve_board(BOARDS, "secret") == "svc-111"
    assert bc.resolve_board(BOARDS, "Recipes") == "svc-222"


@pytest.mark.parametrize("name", ["Secret", "SECRET", "secret ", " secret"])
def test_a_case_or_space_difference_does_not_resolve(name):
    with pytest.raises(bc.BoardNotFound):
        bc.resolve_board(BOARDS, name)


def test_a_duplicate_board_name_is_ambiguous():
    boards = BOARDS + [{"id": "buf-3", "serviceId": "svc-333", "name": "secret"}]
    with pytest.raises(bc.BoardAmbiguous, match="names 2 boards"):
        bc.resolve_board(boards, "secret")


def test_an_unknown_board_lists_the_board_names():
    with pytest.raises(bc.BoardNotFound) as info:
        bc.resolve_board(BOARDS, "Travel")
    assert "'secret', 'Recipes'" in str(info.value)


def test_queue_rows_rejects_an_unknown_board_with_the_list_and_writes_nothing(
        tmp_path):
    state = _state(tmp_path)
    accepted, rejected, _s, _w = cp.queue_rows([pin_row(board="Travel")],
                                               state, now=NOW, boards=BOARDS)
    assert accepted == []
    errors = [e for r in rejected for e in r["errors"]]
    assert len(errors) == 1
    assert "'Travel'" in errors[0] and "'secret', 'Recipes'" in errors[0]
    assert state.rows == {}
    assert not os.path.exists(state.path)


def test_queue_rows_rejects_a_case_only_board_difference(tmp_path):
    state = _state(tmp_path)
    accepted, rejected, _s, _w = cp.queue_rows([pin_row(board="Secret")],
                                               state, now=NOW, boards=BOARDS)
    assert accepted == [] and len(rejected) == 1
    assert state.rows == {}


def test_queue_rows_rejects_an_ambiguous_board(tmp_path):
    state = _state(tmp_path)
    boards = BOARDS + [{"id": "buf-3", "serviceId": "svc-333", "name": "secret"}]
    accepted, rejected, _s, _w = cp.queue_rows([pin_row()], state, now=NOW,
                                               boards=boards)
    assert accepted == []
    assert "names 2 boards" in rejected[0]["errors"][0]
    assert state.rows == {}


def test_queue_rows_without_a_board_list_raises_before_writing(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(cp.BoardLookupUnavailable, match="Nothing was written"):
        cp.queue_rows([pin_row()], state, now=NOW)
    assert state.rows == {}
    assert not os.path.exists(state.path)


def test_queue_rows_on_x_needs_no_board_list(tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "x.json"), platform="x")
    row = {"date": "2036-09-08", "time_window": "morning", "post_text": "Hi.",
           "topic": "", "tags": "", "image_path": "", "first_comment_link": ""}
    accepted, rejected, _s, _w = cp.queue_rows([row], state, now=NOW)
    assert rejected == [] and len(accepted) == 1


def test_a_board_removed_between_queue_and_schedule_fails_at_board(tmp_path):
    state = _state(tmp_path)
    cp.queue_rows([pin_row()], state, now=NOW, boards=BOARDS)
    buffer = Buffer(boards=BOARDS[1:])       # "secret" deleted since
    results, uploads = _schedule(cp.pending_rows(state), state, buffer)
    assert results[0]["status"] == cp.FAILED
    assert results[0]["stage"] == cp.STAGE_BOARD == "board"
    assert "'secret'" in results[0]["errors"][0]
    assert uploads.paths == [] and buffer.inputs == []
    stored = next(iter(state.rows.values()))
    assert (stored["status"], stored["stage"]) == (cp.FAILED, "board")


def test_a_failed_board_read_sends_nothing(tmp_path):
    fetch = _channel()
    buffer, uploads = Buffer(), Uploads()

    def unreadable(*a, **k):
        raise bc.BufferError("boards unreadable")

    with pytest.raises(cp.BoardLookupUnavailable, match="Nothing was created"):
        _schedule([pin_row()], _state(tmp_path), buffer, uploads=uploads,
                  fetch_channel=fetch, fetch_boards=unreadable)
    assert len(fetch.calls) == 1
    assert uploads.paths == [] and buffer.inputs == []


def test_a_board_read_refusal_is_an_identity_refusal_to_callers():
    """DEC-022: every caller that stops on IdentityRefused stops on this."""
    assert issubclass(cp.BoardLookupUnavailable, cp.IdentityRefused)


# ─── gate 4: the Pinterest row rules ─────────────────────────────────────────

REFUSALS = [
    ("missing title", {"title": ""}, "Pinterest requires title, and it is empty"),
    ("missing board", {"board": ""}, "Pinterest requires board, and it is empty"),
    ("title 101", {"title": "t" * 101},
     "Pinterest title is 101 characters against a 100 limit. It is never "
     "truncated automatically."),
    ("alt text 501", {"alt_text": "a" * 501},
     "Pinterest alt_text is 501 characters against a 500 limit. It is never "
     "truncated automatically."),
    ("description 501", {"post_text": "d" * 501}, "too long for Pinterest: 501"),
    ("no image", {"image_path": ""},
     "Pinterest needs an image: every pin is an image."),
    ("topic only", {"post_text": "", "topic": "a topic"},
     "Pinterest needs post_text: topic generation writes LinkedIn-length "
     "posts and is not supported on Pinterest."),
]


@pytest.mark.parametrize("label,over,message", REFUSALS,
                         ids=[r[0] for r in REFUSALS])
def test_each_pinterest_rule_refuses_with_its_message_and_writes_nothing(
        tmp_path, label, over, message):
    state = _state(tmp_path)
    accepted, rejected, _s, _w = cp.queue_rows([pin_row(**over)], state,
                                               now=NOW, boards=BOARDS)
    assert accepted == []
    errors = [e for r in rejected for e in r["errors"]]
    assert len(errors) == 1, errors
    assert message in errors[0]
    assert state.rows == {}
    assert not os.path.exists(state.path)


@pytest.mark.parametrize("over", [{"title": "t" * 100}, {"alt_text": "a" * 500},
                                  {"post_text": "d" * 500}, {"alt_text": ""}])
def test_values_at_their_limit_are_accepted(tmp_path, over):
    """The other direction: each rule is able to say yes."""
    state = _state(tmp_path)
    accepted, rejected, _s, _w = cp.queue_rows([pin_row(**over)], state,
                                               now=NOW, boards=BOARDS)
    assert rejected == [] and len(accepted) == 1


def test_schedule_time_validation_refuses_before_any_upload(tmp_path):
    buffer = Buffer()
    results, uploads = _schedule([pin_row(title="")], _state(tmp_path), buffer)
    assert (results[0]["status"], results[0]["stage"]) == (cp.FAILED, "validate")
    assert uploads.paths == [] and buffer.inputs == []


# ─── identity: X's gate, X's production guard, no credential fallback ────────

def test_a_wrong_handle_refuses_before_the_board_read(tmp_path):
    buffer = Buffer()
    with pytest.raises(cp.IdentityRefused) as info:
        _schedule([pin_row()], _state(tmp_path), buffer,
                  fetch_channel=_channel(handle="someone_else"))
    assert not isinstance(info.value, cp.BoardLookupUnavailable)
    assert buffer.board_reads == [] and buffer.inputs == []


@pytest.mark.parametrize("slug", ["", "   ", None])
def test_an_empty_identity_refuses_with_no_buffer_call(tmp_path, slug):
    """The production guard refuses an unresolved identity first, as on X."""
    buffer, fetch = Buffer(), _channel()
    with pytest.raises(cp.pm.ProductionAccessRefused, match="no declared identity"):
        cp.schedule_pass([pin_row()], CHAN, _state(tmp_path), key="pin-key",
                         session=buffer, upload=Uploads(), now=NOW,
                         platform="pinterest", identity_slug=slug,
                         fetch_channel=fetch)
    assert fetch.calls == [] and buffer.board_reads == [] and buffer.inputs == []


def test_a_declared_production_handle_is_refused_before_buffer(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv(cp.pm.PRODUCTION_IDENTITY_SLUGS_ENV, HANDLE)
    buffer, fetch = Buffer(), _channel()
    with pytest.raises(cp.pm.ProductionAccessRefused):
        _schedule([pin_row()], _state(tmp_path), buffer, fetch_channel=fetch)
    assert fetch.calls == [] and buffer.board_reads == [] and buffer.inputs == []


# ─── gate 5, and the dashboard: one profile with three channels ──────────────

PIN_KEY_ENV = "BUFFER_API_KEY_PIN"


def _pin_overlay(identity=HANDLE, key_env=PIN_KEY_ENV):
    return {"buffer_api_key_env": key_env, "buffer_channel_id": CHAN,
            "identity_slug": identity, "drain": False}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """test_x_drain's world (LinkedIn and X), with Pinterest added: its
    overlay, its key, its boards, and a create_post that records extras."""
    w = xd.World(tmp_path, monkeypatch)
    w.scheduled["platforms"]["pinterest"] = _pin_overlay()
    w.board_reads = []
    w.extras = []
    monkeypatch.setenv(PIN_KEY_ENV, "pin-key")
    handles = {"chan_x": xd.X_HANDLE, CHAN: HANDLE}

    def get_channel(channel_id, key=None, session=None, max_age=None):
        w.calls.append(("get_channel", channel_id, key))
        return {"id": channel_id, "name": handles.get(channel_id, w.handle),
                "service": "pinterest" if channel_id == CHAN else "twitter"}

    def channel_boards(channel_id, key=None, session=None):
        w.calls.append(("channel_boards", channel_id, key))
        return list(BOARDS)

    def create_post(channel_id, text, image_url, when, key=None, session=None,
                    **extras):
        w.calls.append(("create_post", channel_id, key))
        w.extras.append((channel_id, extras))
        return {"id": "post-%d" % len(w.calls), "dueAt": when}

    monkeypatch.setattr(cp.bc, "get_channel", get_channel)
    monkeypatch.setattr(cp.bc, "channel_boards", channel_boards)
    monkeypatch.setattr(cp.bc, "create_post", create_post)
    yield w
    dashboard.jobs.clear()


def _hold_pins(world, n):
    state = world.state("pinterest")
    for i in range(n):
        row = pin_row(date="2036-11-%02d" % (i + 1), title="Held pin %d" % i)
        state.update(cp.row_key(row, "pinterest"), status=cp.HELD,
                     row=dict(row), text=cp.row_preview(row),
                     hold_reason="waiting for a slot", errors=[])
    return state


def test_one_drain_check_feeds_linkedin_x_and_pinterest_each_with_its_own_key(
        world):
    world.hold("linkedin", 1)
    world.hold("x", 1)
    _hold_pins(world, 2)
    engine = world.engine()
    engine.tick()

    assert len(engine.submitted) == 1, "one check, one feed job for the profile"
    assert sorted(f["platform"] for f in engine.submitted[0]) == [
        "linkedin", "pinterest", "x"]
    assert world.to(CHAN) and {c[2] for c in world.to(CHAN)} == {"pin-key"}
    assert {c[2] for c in world.to("chan_x")} == {"x-key"}
    assert {c[2] for c in world.to("chan_li")} == {"li-key"}
    assert world.calls.count(("create_post", CHAN, "pin-key")) == 2
    assert world.calls.count(("channel_boards", CHAN, "pin-key")) == 1
    assert world.calls.count(("create_post", "chan_x", "x-key")) == 1
    assert world.calls.count(("create_post", "chan_li", "li-key")) == 1
    pin_extras = [e for c, e in world.extras if c == CHAN]
    assert [e["metadata"]["pinterest"]["boardServiceId"] for e in pin_extras] == [
        "svc-111", "svc-111"]
    assert all(e == {} for c, e in world.extras if c != CHAN), \
        "LinkedIn and X get no new createPost keywords"
    assert xd._statuses(world.state("pinterest")) == [cp.SCHEDULED] * 2
    assert sorted((r["platform"], r["action"]) for r in xd._runs(engine)) == [
        ("linkedin", pd.ACTION_FED), ("pinterest", pd.ACTION_FED),
        ("x", pd.ACTION_FED)]


def test_a_missing_pinterest_key_never_falls_back_to_another_key(world,
                                                                 monkeypatch):
    monkeypatch.delenv(PIN_KEY_ENV)
    _hold_pins(world, 1)
    engine = world.engine()
    engine.tick()
    assert world.calls == []
    run = xd._runs(engine)[0]
    assert (run["platform"], run["action"]) == ("pinterest", pd.ACTION_ERROR)
    assert PIN_KEY_ENV in run["note"]


def _upload(world, **row):
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client().post(
        "/api/scheduled/p/rows", json={"platform": "pinterest", **row})


def test_the_queue_route_refuses_an_unknown_board_with_the_board_list(world):
    r = _upload(world, **pin_row(board="Travel"))
    body = r.get_json()
    assert body["counts"]["accepted"] == 0 and body["counts"]["rejected"] == 1
    assert "'secret', 'Recipes'" in json.dumps(body)
    assert world.calls == [("channel_boards", CHAN, "pin-key")]
    assert world.state("pinterest").rows == {}


def test_the_queue_route_accepts_a_known_board(world):
    body = _upload(world, **pin_row()).get_json()
    assert body["counts"]["accepted"] == 1
    assert len(world.state("pinterest").rows) == 1


def test_the_queue_route_refuses_the_whole_upload_when_boards_cannot_be_read(
        world, monkeypatch):
    def unreadable(*a, **k):
        raise bc.BufferError("Buffer is down")
    monkeypatch.setattr(dashboard.buffer_client, "channel_boards", unreadable)
    r = _upload(world, **pin_row())
    assert r.status_code == 400
    assert "nothing was queued" in r.get_json()["error"]
    assert "Buffer is down" in r.get_json()["error"]
    assert world.state("pinterest").rows == {}


def test_the_queue_route_refuses_when_pinterest_is_not_configured(world):
    world.scheduled["platforms"].pop("pinterest")
    r = _upload(world, **pin_row())
    assert r.status_code == 400
    assert world.calls == []
    assert world.state("pinterest").rows == {}
