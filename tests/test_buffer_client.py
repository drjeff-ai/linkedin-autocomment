"""Phase 2: Buffer post creation, offline.

The Buffer API is stubbed throughout. What these pin is the payload shape — which
the spike proved is unforgiving, five fields wrong on the first attempt — the
window-to-UTC arithmetic, and the failures that must stay loud.

The single most important assertion in this file is that `firstComment` is never
sent. On the free plan it does not degrade: it rejects the whole post and nothing
is created.
"""

import json
import os
import random
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import buffer_client as bc  # noqa: E402

LA = "America/Los_Angeles"
PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


def tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(LA)


# --- the payload shape ------------------------------------------------------

def test_the_payload_carries_every_required_field():
    p = bc.build_post_input("chan", "hello", "https://cdn/x.png", "2026-09-08T16:00:00.000Z")
    assert p["channelId"] == "chan"
    assert p["text"] == "hello"
    assert p["assets"] == [{"image": {"url": "https://cdn/x.png"}}]
    assert p["mode"] == "customScheduled"
    assert p["schedulingType"] == "automatic"
    assert p["needsApproval"] is False
    assert p["dueAt"] == "2026-09-08T16:00:00.000Z"


def test_firstComment_is_NEVER_sent():
    """The whole reason the hybrid exists. Including it on the free plan
    rejects the entire post rather than dropping the field."""
    for image in ("https://cdn/x.png", None):
        p = bc.build_post_input("chan", "hello", image, "2026-09-08T16:00:00.000Z")
        blob = json.dumps(p).lower()
        assert "firstcomment" not in blob
        assert "metadata" not in p


def test_assets_is_present_even_with_no_image():
    """[AssetInput!]! is non-null: omitting it is a schema error, not a default."""
    p = bc.build_post_input("chan", "hello", None, "2026-09-08T16:00:00.000Z")
    assert p["assets"] == []


def test_scheduling_type_is_never_the_nonexistent_custom():
    """SchedulingType is {automatic, notification}. 'custom' is not a member and
    sending it fails the whole call - the spike's mistake."""
    p = bc.build_post_input("c", "t", None, "2026-09-08T16:00:00.000Z")
    assert p["schedulingType"] != "custom"
    assert p["schedulingType"] == "automatic"


# --- window -> randomized minute -> UTC -------------------------------------

@pytest.mark.parametrize("window,lo,hi", [
    ("morning", 8, 11), ("afternoon", 12, 15), ("evening", 17, 20),
])
def test_the_rolled_time_lands_inside_its_local_window(window, lo, hi):
    """Sampled, because the point is the RANGE, not one draw."""
    zone = tz()
    for seed in range(40):
        iso = bc.due_at("2026-09-08", window, rng=random.Random(seed),
                        tz=zone, now=PAST)
        local = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(zone)
        assert lo <= local.hour < hi or (local.hour == hi and local.minute == 0)
        assert local.date().isoformat() == "2026-09-08"


def test_the_minute_actually_varies():
    """A fixed time would make every post land on the same round number."""
    zone = tz()
    seen = {bc.due_at("2026-09-08", "morning", rng=random.Random(s),
                      tz=zone, now=PAST) for s in range(30)}
    assert len(seen) > 5


def test_the_output_is_iso_8601_utc_with_a_Z():
    iso = bc.due_at("2026-09-08", "morning", rng=random.Random(1),
                    tz=tz(), now=PAST)
    assert iso.endswith("Z")
    assert "+00:00" not in iso
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_dst_is_honoured_rather_than_a_fixed_offset():
    """LA is UTC-7 in summer and UTC-8 in winter. A fixed offset would put every
    post an hour out for half the year."""
    zone = tz()
    rng = lambda: random.Random(0)  # noqa: E731 - same draw both times
    summer = bc.due_at("2026-09-08", "morning", rng=rng(), tz=zone, now=PAST)
    winter = bc.due_at("2026-01-08", "morning", rng=rng(), tz=zone, now=PAST)
    s_utc = datetime.fromisoformat(summer.replace("Z", "+00:00"))
    w_utc = datetime.fromisoformat(winter.replace("Z", "+00:00"))
    s_local = s_utc.astimezone(zone)
    w_local = w_utc.astimezone(zone)
    assert (s_local.hour, s_local.minute) == (w_local.hour, w_local.minute)
    assert s_utc.hour != w_utc.hour      # same local clock, different UTC hour


def test_a_bad_window_name_is_refused():
    with pytest.raises(bc.BufferError, match="unknown time_window"):
        bc.due_at("2026-09-08", "midnight", tz=tz(), now=PAST)


def test_a_bad_date_is_refused():
    with pytest.raises(bc.BufferError, match="expected YYYY-MM-DD"):
        bc.due_at("08/09/2026", "morning", tz=tz(), now=PAST)


def test_a_time_already_past_is_refused():
    """Buffer will not schedule into the past; catching it here names the row."""
    future_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(bc.BufferError, match="in the past"):
        bc.due_at("2026-09-08", "morning", tz=tz(), now=future_now)


# --- text composition -------------------------------------------------------

def test_tags_sit_under_a_blank_line():
    assert bc.compose_text("Body here.", "#a #b") == "Body here.\n\n#a #b"


def test_no_tags_leaves_the_body_alone():
    assert bc.compose_text("Body here.", "") == "Body here."
    assert bc.compose_text("Body here.", None) == "Body here."


def test_empty_body_is_refused():
    with pytest.raises(bc.BufferError, match="empty"):
        bc.compose_text("   ", "#a")


# --- the API call ------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse(self.body, self.status)


SUCCESS = {"data": {"createPost": {
    "__typename": "PostActionSuccess",
    "post": {"id": "post123", "status": "scheduled",
             "dueAt": "2026-09-08T16:00:00.000Z", "text": "hi",
             "assets": [{"id": None, "mimeType": "image/png"}]}}}}


@pytest.fixture(autouse=True)
def _no_usage_log(tmp_path, monkeypatch):
    """Keep the real api_usage.jsonl out of the test run."""
    monkeypatch.setattr(bc, "USAGE_LOG", str(tmp_path / "api_usage.jsonl"))


def test_a_successful_create_returns_the_post():
    s = FakeSession(SUCCESS)
    post = bc.create_post("chan", "hi", "https://cdn/x.png",
                          "2026-09-08T16:00:00.000Z", key="k", session=s)
    assert post["id"] == "post123"
    assert post["status"] == "scheduled"


def test_the_bearer_token_is_sent_and_the_body_is_the_payload():
    s = FakeSession(SUCCESS)
    bc.create_post("chan", "hi", None, "2026-09-08T16:00:00.000Z",
                   key="secret", session=s)
    call = s.calls[0]
    assert call["headers"]["Authorization"] == "Bearer secret"
    sent = call["json"]["variables"]["input"]
    assert sent["mode"] == "customScheduled"
    assert "metadata" not in sent


def test_an_unreadable_image_is_a_row_failure_not_a_crash():
    """The spike's InvalidInputError. Buffer reads the image at createPost, so
    a bad URL fails here - while a human can still fix the row."""
    s = FakeSession({"data": {"createPost": {
        "__typename": "InvalidInputError",
        "message": "Invalid post: Image could not be read from its URL."}}})
    with pytest.raises(bc.BufferRejected) as exc:
        bc.create_post("chan", "hi", "https://cdn/missing.png",
                       "2026-09-08T16:00:00.000Z", key="k", session=s)
    assert "Image could not be read" in str(exc.value)
    assert "NOTHING was created" in str(exc.value)


def test_a_mutation_error_is_also_a_row_failure():
    s = FakeSession({"data": {"createPost": {
        "__typename": "MutationError", "message": "requires a paid plan"}}})
    with pytest.raises(bc.BufferRejected, match="paid plan"):
        bc.create_post("c", "t", None, "2026-09-08T16:00:00.000Z",
                       key="k", session=s)


def test_a_graphql_errors_array_is_not_mistaken_for_success():
    """HTTP 200 with an errors array looks fine to a status-code check."""
    s = FakeSession({"errors": [{"message": "Variable type mismatch"}]})
    with pytest.raises(bc.BufferError, match="GraphQL errors"):
        bc.create_post("c", "t", None, "2026-09-08T16:00:00.000Z",
                       key="k", session=s)


def test_a_401_names_the_key():
    s = FakeSession({}, status=401)
    with pytest.raises(bc.BufferError, match="API key"):
        bc.create_post("c", "t", None, "2026-09-08T16:00:00.000Z",
                       key="k", session=s)


def test_every_call_is_logged_to_api_usage(tmp_path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setattr(bc, "USAGE_LOG", str(log))
    bc.create_post("c", "t", None, "2026-09-08T16:00:00.000Z",
                   key="k", session=FakeSession(SUCCESS))
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert lines and lines[0]["api"] == "buffer-graphql"
    assert lines[0]["endpoint"] == "createPost"


# --- the Phase 1 + 2 chain --------------------------------------------------

def test_schedule_row_uploads_the_image_then_posts_the_public_url():
    s = FakeSession(SUCCESS)
    uploaded = []

    def fake_upload(path):
        uploaded.append(path)
        return "https://pub-abc.r2.dev/posts/deadbeef.png"

    out = bc.schedule_row("chan", "Body.", "#a", r"S:\pics\x.png",
                          "2036-09-08", "morning", rng=random.Random(3),
                          key="k", session=s, upload=fake_upload)
    assert uploaded == [r"S:\pics\x.png"]
    sent = s.calls[0]["json"]["variables"]["input"]
    assert sent["assets"][0]["image"]["url"].startswith("https://pub-abc.r2.dev/")
    assert out["post_id"] == "post123"
    assert out["image_url"].endswith(".png")


def test_a_row_with_no_image_still_schedules():
    s = FakeSession(SUCCESS)
    out = bc.schedule_row("chan", "Body.", "", "", "2036-09-08", "morning",
                          rng=random.Random(3), key="k", session=s,
                          upload=lambda p: pytest.fail("should not upload"))
    assert out["image_url"] is None
    assert s.calls[0]["json"]["variables"]["input"]["assets"] == []


def test_an_unhostable_image_fails_the_row_and_never_posts():
    """A row naming an image the pipeline cannot host must fail, not quietly
    publish without it."""
    from linkedin_automation import image_host

    s = FakeSession(SUCCESS)

    def boom(path):
        raise image_host.ImageNotFound("no such file: %s" % path)

    with pytest.raises(image_host.ImageNotFound):
        bc.schedule_row("chan", "Body.", "", r"S:\gone.png", "2036-09-08",
                        "morning", rng=random.Random(3), key="k", session=s,
                        upload=boom)
    assert s.calls == [], "nothing may be posted when the image failed"


# --- Phase 3: waiting for the post to actually publish ----------------------
#
# The clock is not the signal. Buffer publishes NEAR dueAt, not on it - the
# spike measured sentAt 34s late - so "it is past due, therefore it is live"
# would send the browser tool to comment on a post that does not exist yet.

class ScriptedSession:
    """Returns a scripted sequence of post states, one per poll."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def post(self, url, headers=None, json=None, timeout=None):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return FakeResponse({"data": {"post": state}})


def _state(status, link=None, err=None):
    return {"id": "p1", "status": status, "dueAt": "2026-09-08T16:00:00.000Z",
            "sentAt": "2026-09-08T16:00:34.000Z" if status == "sent" else None,
            "externalLink": link,
            "error": {"message": err, "rawError": None} if err else None}


LINK = "https://www.linkedin.com/feed/update/urn:li:share:0000000000000000000"


def test_it_waits_through_scheduled_and_sending_then_captures_the_link():
    s = ScriptedSession([
        _state("scheduled"), _state("scheduled"), _state("sending"),
        _state("sent", LINK),
    ])
    slept = []
    got = bc.wait_for_publish("p1", key="k", session=s, interval=20,
                              sleep_fn=slept.append)
    assert got == LINK
    assert s.calls == 4, "it must not stop before status reaches sent"
    assert slept == [20, 20, 20]


def test_being_past_due_is_not_treated_as_published():
    """The whole point: a post can sit at 'scheduled' after its dueAt."""
    s = ScriptedSession([_state("scheduled")] * 3 + [_state("sent", LINK)])
    assert bc.wait_for_publish("p1", key="k", session=s,
                               sleep_fn=lambda _: None) == LINK
    assert s.calls == 4


def test_an_errored_post_fails_distinctly_from_a_timeout():
    s = ScriptedSession([_state("scheduled"),
                         _state("error", err="LinkedIn rejected the media")])
    with pytest.raises(bc.BufferPublishFailed, match="LinkedIn rejected"):
        bc.wait_for_publish("p1", key="k", session=s, sleep_fn=lambda _: None)


def test_a_draft_is_blocked_rather_than_waited_out():
    """It will never become 'sent' on its own, so waiting the full ceiling
    would be a slow way to learn nothing."""
    s = ScriptedSession([_state("draft")])
    with pytest.raises(bc.BufferPublishBlocked, match="never becomes"):
        bc.wait_for_publish("p1", key="k", session=s, sleep_fn=lambda _: None)
    assert s.calls == 1


def test_needs_approval_is_blocked_too():
    s = ScriptedSession([_state("needs_approval")])
    with pytest.raises(bc.BufferPublishBlocked):
        bc.wait_for_publish("p1", key="k", session=s, sleep_fn=lambda _: None)


def test_a_timeout_says_the_comment_was_not_attempted():
    """A timeout is not a failure - the post may still go out - so the message
    must not imply the post is dead."""
    s = ScriptedSession([_state("scheduled")])
    clock = iter([0, 10, 20, 30, 40, 50, 60, 70, 80])
    with pytest.raises(bc.BufferPublishTimeout) as exc:
        bc.wait_for_publish("p1", key="k", session=s, timeout=30, interval=10,
                            sleep_fn=lambda _: None, now_fn=lambda: next(clock))
    assert "may still" in str(exc.value)
    assert "has NOT been attempted" in str(exc.value)


def test_sent_without_a_link_gets_a_grace_period_then_fails():
    """status and externalLink are not written atomically, but Phase 4 is
    useless without the URL - so it waits briefly, then says so plainly."""
    s = ScriptedSession([_state("sent", None)])
    with pytest.raises(bc.BufferError, match="never returned an externalLink"):
        bc.wait_for_publish("p1", key="k", session=s, link_grace=2,
                            sleep_fn=lambda _: None)
    assert s.calls == 3, "one initial poll plus two grace polls"


def test_a_link_arriving_just_after_sent_is_still_captured():
    s = ScriptedSession([_state("sent", None), _state("sent", LINK)])
    assert bc.wait_for_publish("p1", key="k", session=s, link_grace=3,
                               sleep_fn=lambda _: None) == LINK


def test_an_unknown_status_keeps_waiting_rather_than_guessing():
    s = ScriptedSession([_state("queued_somehow"), _state("sent", LINK)])
    assert bc.wait_for_publish("p1", key="k", session=s,
                               sleep_fn=lambda _: None) == LINK


def test_a_missing_post_is_an_error_not_a_wait():
    class Missing:
        def post(self, *a, **k):
            return FakeResponse({"data": {"post": None}})
    with pytest.raises(bc.BufferError, match="no post for id"):
        bc.wait_for_publish("nope", key="k", session=Missing(),
                            sleep_fn=lambda _: None)
