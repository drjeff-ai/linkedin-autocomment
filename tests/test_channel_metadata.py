"""P3: Buffer ``metadata`` and image alt text, from a channel's declaration.

Every claim here is asserted on the createPost input that reached the
transport (a fake session that records what it is handed), not on the builder
or on ``build_post_input`` alone: the claim is "declared metadata reaches
Buffer", so the test starts at the row and ends at the request.

The channel is a test channel monkeypatched into ``channels.REGISTRY``, as
``test_channel_extras.py`` does. No shipped channel declares these fields yet,
and LinkedIn and X must send exactly what they always have (the golden corpus
pins that byte for byte).
"""

import dataclasses
import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import channels  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
TEST_CHANNEL = "metach"
IMAGE = os.path.join(os.path.dirname(HERE), "tests", "fixtures",
                     "platform_golden", "pixel.png")
HOSTED = "https://img.test.invalid/pixel.png"
LINK = "https://example.com/meta"

#: What the test builder returns. A constant, so the assertion compares the
#: request against a value the test wrote, not against the builder's own output.
BUILT = {"testnet": {"boardServiceId": "none", "title": "T", "url": LINK}}


def _builder(row, board_service_id):
    return {"testnet": {"boardServiceId": str(board_service_id or "none"),
                        "title": row.get("title"), "url": cp.row_link(row)}}


def _channel(**over):
    fields = dict(
        id=TEST_CHANNEL, label="MetaCh", link_placement=channels.METADATA,
        text_limit=None, identity_check=channels.UNGATED,
        config_layout=channels.OVERLAY, key_env=None,
        state_path=channels.PLATFORM_DIR, reconcile_statuses=("sent",),
        extra_columns=(channels.ExtraColumn("title"),
                       channels.ExtraColumn("alt_text")),
        metadata_builder=_builder, alt_text_column="alt_text")
    fields.update(over)
    return channels.Channel(**fields)


@pytest.fixture
def register(monkeypatch):
    def _register(channel):
        monkeypatch.setitem(channels.REGISTRY, channel.id, channel)
        return channel
    return _register


def make_row(**over):
    row = {"date": "2036-09-08", "time_window": "morning",
           "post_text": "A description.", "topic": "", "tags": "",
           "image_path": IMAGE, "first_comment_link": "", "link": LINK,
           "title": "T", "alt_text": "  A red square.  "}
    row.update(over)
    return row


class _Resp(object):
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Session(object):
    """Records every createPost input; answers success. Never networked."""

    def __init__(self):
        self.inputs = []

    def post(self, url, headers=None, json=None, timeout=None):
        payload = (json or {}).get("variables", {}).get("input")
        self.inputs.append(payload)
        return _Resp({"data": {"createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post-%d" % len(self.inputs), "status": "scheduled",
                     "dueAt": (payload or {}).get("dueAt")}}}})


class _Uploads(object):
    def __init__(self):
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return HOSTED


def _schedule(rows, state, platform=TEST_CHANNEL, **kw):
    session, uploads = _Session(), _Uploads()
    results = cp.schedule_pass(
        rows, "chan", state, key="k", session=session, upload=uploads,
        now=NOW, platform=platform, generate=lambda *a, **k: "g", **kw)
    return results, session, uploads


def _state(tmp_path, platform=TEST_CHANNEL):
    return cp.PipelineState(path=str(tmp_path / ("s_%s.json" % platform)),
                            platform=platform)


# --- the declaration -------------------------------------------------------

def test_linkedin_and_x_declare_none_of_the_new_fields():
    for ch in (channels.LINKEDIN, channels.X, channels.undeclared("zz")):
        assert ch.metadata_builder is None
        assert ch.alt_text_column is None
        assert ch.image_required is False
        assert ch.board_lookup is False


def test_metadata_is_a_link_placement_distinct_from_the_others():
    assert channels.METADATA == "metadata"
    assert channels.METADATA not in (channels.FIRST_COMMENT, channels.BODY,
                                     channels.NO_LINK)


# --- build_post_input: the shape ---------------------------------------------

def test_build_post_input_without_the_new_arguments_has_exactly_the_old_keys():
    p = bc.build_post_input("c", "t", "https://cdn/x.png", "2036-09-08T16:00:00.000Z")
    assert sorted(p) == sorted(["channelId", "text", "assets", "mode",
                                "schedulingType", "needsApproval", "dueAt"])
    assert p["assets"] == [{"image": {"url": "https://cdn/x.png"}}]


def test_build_post_input_keeps_the_positional_order():
    p = bc.build_post_input("c", "t", None, "2036-09-08T16:00:00.000Z",
                            {"a": 1}, " alt ")
    assert (p["channelId"], p["text"], p["assets"], p["dueAt"],
            p["metadata"]) == ("c", "t", [], "2036-09-08T16:00:00.000Z", {"a": 1})


def test_alt_text_without_an_image_sends_no_asset():
    p = bc.build_post_input("c", "t", None, "d", alt_text="alt")
    assert p["assets"] == []


# --- schedule_pass: declared metadata and alt text reach the transport -----

def test_declared_metadata_reaches_the_createpost_input(tmp_path, register):
    register(_channel())
    results, session, _ = _schedule([make_row()], _state(tmp_path))
    assert [r["status"] for r in results] == [cp.SCHEDULED], results
    (sent,) = session.inputs
    assert sent["metadata"] == BUILT


def test_alt_text_reaches_the_image_asset_stripped(tmp_path, register):
    register(_channel())
    _, session, _ = _schedule([make_row()], _state(tmp_path))
    (sent,) = session.inputs
    assert sent["assets"] == [{"image": {"url": HOSTED,
                                         "metadata": {"altText": "A red square."}}}]
    assert sent["assets"][0]["image"]["metadata"]["altText"] == "A red square."


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_alt_text_sends_the_asset_without_metadata(tmp_path, register,
                                                           blank):
    register(_channel())
    _, session, _ = _schedule([make_row(alt_text=blank)], _state(tmp_path))
    (sent,) = session.inputs
    assert sent["assets"] == [{"image": {"url": HOSTED}}]


def test_no_builder_sends_no_metadata_key(tmp_path, register):
    register(_channel(metadata_builder=None, alt_text_column=None))
    _, session, _ = _schedule([make_row()], _state(tmp_path))
    (sent,) = session.inputs
    assert "metadata" not in sent
    assert sent["assets"] == [{"image": {"url": HOSTED}}], \
        "no alt_text_column, so the column is not read"


def test_a_builder_that_returns_none_sends_no_metadata_key(tmp_path, register):
    register(_channel(metadata_builder=lambda row, sid: None))
    _, session, _ = _schedule([make_row()], _state(tmp_path))
    (sent,) = session.inputs
    assert "metadata" not in sent


@pytest.mark.parametrize("platform", ["linkedin", "x"])
def test_linkedin_and_x_send_no_metadata_and_a_plain_asset(tmp_path, monkeypatch,
                                                           platform):
    """The row carries alt_text and title columns; neither channel reads them."""
    monkeypatch.setattr(cp, "verify_identity", lambda *a, **k: (True, "ok"))
    _, session, _ = _schedule([make_row(post_text="short")],
                              _state(tmp_path, platform), platform=platform,
                              identity_slug="someone", allow_production=True)
    (sent,) = session.inputs
    assert "metadata" not in sent
    assert sent["assets"] == [{"image": {"url": HOSTED}}]


def test_linkedin_calls_create_post_with_exactly_the_old_arguments(tmp_path,
                                                                    monkeypatch):
    """A transport double with the old signature still works: nothing new is
    passed to create_post for a channel that declares nothing."""
    calls = []

    def create_post(channel_id, text, image_url, when, key=None, session=None):
        calls.append(channel_id)
        return {"id": "p1", "status": "scheduled", "dueAt": when}
    monkeypatch.setattr(cp.bc, "create_post", create_post)
    results = cp.schedule_pass([make_row()], "chan", _state(tmp_path, "linkedin"),
                               upload=_Uploads(), now=NOW, platform="linkedin")
    assert [r["status"] for r in results] == [cp.SCHEDULED], results
    assert calls == ["chan"]


def test_a_builder_that_raises_fails_the_row_before_any_upload(tmp_path, register):
    def refuse(row, sid):
        raise ValueError("no board")
    register(_channel(metadata_builder=refuse))
    results, session, uploads = _schedule([make_row()], _state(tmp_path))
    assert results[0]["status"] == cp.FAILED
    assert results[0]["stage"] == cp.STAGE_PREPARE
    assert "no board" in results[0]["errors"][0]
    assert uploads.paths == [] and session.inputs == []


# --- image_required ----------------------------------------------------------

MESSAGE = "MetaCh needs an image: every pin is an image."


@pytest.mark.parametrize("column", ["image_path", "asset_path"])
def test_an_image_under_either_name_satisfies_image_required(tmp_path, register,
                                                             column):
    register(_channel(image_required=True))
    row = make_row(image_path="")
    row[column] = IMAGE
    assert cp.validate_row(row, now=NOW, platform=TEST_CHANNEL) == []


def test_image_required_refuses_at_queue_time(tmp_path, register):
    register(_channel(image_required=True))
    state = _state(tmp_path)
    ok, rejected, skipped, _ = cp.queue_rows([make_row(image_path="")], state,
                                             now=NOW)
    assert ok == [] and skipped == []
    assert rejected[0]["errors"] == [MESSAGE]
    assert state.rows == {}
    assert not os.path.exists(state.path), "nothing written"


def test_image_required_refuses_at_schedule_time_with_nothing_sent(tmp_path,
                                                                  register):
    register(_channel(image_required=True))
    results, session, uploads = _schedule([make_row(image_path="")],
                                          _state(tmp_path))
    assert results[0]["status"] == cp.FAILED
    assert results[0]["stage"] == "validate"
    assert results[0]["errors"] == [MESSAGE]
    assert uploads.paths == [] and session.inputs == []


def test_without_image_required_an_imageless_row_is_accepted(tmp_path, register):
    register(_channel())
    assert cp.validate_row(make_row(image_path=""), now=NOW,
                           platform=TEST_CHANNEL) == []


# --- board_lookup: boards that cannot be read ---------------------------------

def test_a_board_lookup_channel_whose_boards_cannot_be_read_creates_nothing(
        tmp_path, register):
    """P5 (DEC-022) replaced P3's refuse-before-identity placeholder with a
    board read after the identity gate. A read that fails still sends
    nothing: identity ran once, nothing uploaded, nothing created."""
    register(_channel(board_lookup=True, identity_check=channels.BUFFER_HANDLE))
    fetches = []
    session, uploads = _Session(), _Uploads()

    def no_boards(*a, **k):
        raise bc.BufferError("boards unreadable")

    with pytest.raises(cp.BoardLookupUnavailable, match="Nothing was created"):
        cp.schedule_pass(
            [make_row(board="b")], "chan", _state(tmp_path), key="k",
            session=session, upload=uploads, now=NOW, platform=TEST_CHANNEL,
            generate=lambda *a, **k: "g", identity_slug="someone",
            fetch_channel=lambda *a, **k: fetches.append(a) or {"name": "someone"},
            fetch_boards=no_boards)
    assert len(fetches) == 1
    assert uploads.paths == [] and session.inputs == []


def test_the_test_channel_differs_from_the_default_only_where_declared():
    """Guards the fixture: the shipped defaults are what LinkedIn and X get."""
    plain = _channel(metadata_builder=None, alt_text_column=None)
    defaults = {f.name: f.default for f in dataclasses.fields(channels.Channel)
                if f.name in ("metadata_builder", "alt_text_column",
                              "image_required", "board_lookup")}
    assert defaults == {"metadata_builder": None, "alt_text_column": None,
                        "image_required": False, "board_lookup": False}
    assert plain.image_required is False and plain.board_lookup is False
