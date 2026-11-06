"""Spike-level checks on the pure parts of tools/buffer_spike.py.

Only the functions that need no network: the CSV -> createPost mapping and the
input builder. The spike's real verdict comes from the live API and the human
gate, not from here - this just stops the mapping from quietly drifting, and
pins the finding that matters most for the CSV design.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.buffer_spike import build_input, csv_row_to_input  # noqa: E402


ROW = {
    "date": "2026-09-08",
    "time_window": "09:00-11:00",
    "post_text": "Testing an automated scheduled post.",
    "topic": "automation",
    "tags": "#automation #python",
    "image_path": "S:/new_comp/test_image.png",
    "first_comment_link": "https://example.com/the-link",
}


def test_all_four_things_ride_on_one_call():
    """text, image, dueAt and firstComment together - the whole spike question."""
    payload = build_input("chan1", "hello #automation",
                          "https://cdn.example.com/x.png",
                          "2026-09-08T09:00:00.000Z", "first comment here")
    assert payload["channelId"] == "chan1"
    assert payload["text"] == "hello #automation"
    assert payload["dueAt"] == "2026-09-08T09:00:00.000Z"
    assert payload["assets"] == [{"image": {"url": "https://cdn.example.com/x.png"}}]
    assert payload["metadata"]["linkedin"]["firstComment"] == "first comment here"


def test_the_required_scalars_match_the_live_schema():
    """Four of these were wrong on the first attempt, and each would have failed
    createPost exactly the way String!/OrganizationId! failed GetChannels."""
    payload = build_input("chan1", "hi", None, "2026-09-08T09:00:00.000Z", None)
    # assets is [AssetInput!]! - required and non-null, so a text-only post
    # sends an empty list rather than omitting the key.
    assert payload["assets"] == []
    # mode is ShareMode!, and customScheduled is the member that honours dueAt.
    assert payload["mode"] == "customScheduled"
    # schedulingType is SchedulingType! whose members are automatic/notification.
    # There is no "custom" member - scheduling is chosen by mode.
    assert payload["schedulingType"] == "automatic"
    # needsApproval is Boolean! with no default.
    assert payload["needsApproval"] is False


def test_metadata_uses_the_lowercase_linkedin_key():
    """PostInputMetaData spells it `linkedin`. `linkedIn` is not a field, and
    would have been rejected as unknown."""
    payload = build_input("c", "t", None, None, "a comment")
    assert "linkedin" in payload["metadata"]
    assert "linkedIn" not in payload["metadata"]


def test_no_due_at_falls_back_to_the_queue():
    payload = build_input("chan1", "hi", None, None, None)
    assert payload["mode"] == "addToQueue"
    assert "dueAt" not in payload
    assert "metadata" not in payload


def test_the_csv_row_maps_date_and_window_onto_dueAt():
    payload, _ = csv_row_to_input(ROW, "chan1",
                                  image_url_override="https://cdn.example.com/x.png")
    assert payload["dueAt"] == "2026-09-08T09:00:00.000Z"   # window START
    assert payload["metadata"]["linkedin"]["firstComment"] == ROW["first_comment_link"]


def test_tags_are_appended_under_a_blank_line():
    payload, _ = csv_row_to_input(ROW, "chan1", "https://cdn.example.com/x.png")
    assert payload["text"].startswith("Testing an automated scheduled post.")
    assert payload["text"].endswith("#automation #python")
    assert "\n\n" in payload["text"]


def test_a_local_image_path_cannot_satisfy_buffer_and_says_so():
    """THE finding. Buffer takes a public URL; the CSV carries a local path, so
    the mapping is incomplete by design and must not pretend otherwise."""
    payload, notes = csv_row_to_input(ROW, "chan1")      # no URL override
    assert payload["assets"] == [], "a local path must never be sent as a URL"
    assert notes, "the gap has to be reported, not silently dropped"
    assert "LOCAL PATH" in notes[0]
    assert "without its image" in notes[0].lower()


def test_supplying_a_hosted_url_completes_the_mapping():
    payload, notes = csv_row_to_input(ROW, "chan1", "https://cdn.example.com/x.png")
    assert payload["assets"] == [{"image": {"url": "https://cdn.example.com/x.png"}}]
    assert notes == []


# ─── Phase 1: targeting a service other than LinkedIn ────────────────────────
#
# Still no network. These pin the two decisions that let the same spike drive an
# X channel: which channel it picks, and what metadata it is allowed to attach.

from tools.buffer_spike import SERVICE_ALIASES, discover  # noqa: E402


def _pick(channels, service):
    """Run discover's matcher over a channel list without touching the API."""
    wanted = SERVICE_ALIASES.get((service or "").lower(), ((service or "").lower(),))
    return [c for c in channels
            if str(c.get("service", "")).lower().startswith(wanted)]


CHANNELS = [
    {"name": "li", "service": "linkedin", "id": "c_li"},
    {"name": "x", "service": "twitter", "id": "c_x"},
]


def test_x_matches_buffers_old_twitter_service_name():
    """Buffer never renamed it: the schema still says Twitter, not X.

    Matching on "x" alone would report "no X channel" against an account that
    has one, which is the kind of wrong answer that sends someone to Buffer's UI
    looking for a bug that is in this file.
    """
    assert [c["id"] for c in _pick(CHANNELS, "x")] == ["c_x"]
    assert [c["id"] for c in _pick(CHANNELS, "twitter")] == ["c_x"]


def test_linkedin_is_unaffected_and_is_still_the_default():
    assert [c["id"] for c in _pick(CHANNELS, "linkedin")] == ["c_li"]
    assert SERVICE_ALIASES["linkedin"] == ("linkedin",)
    import inspect
    assert inspect.signature(discover).parameters["service"].default == "linkedin"


def test_an_unknown_service_matches_on_its_own_name_rather_than_everything():
    """A typo must find nothing, not silently pick the first channel."""
    assert _pick(CHANNELS, "mastodon") == []


def test_a_linkedin_post_still_carries_first_comment():
    """The existing behaviour, unchanged — this is the LinkedIn path."""
    payload = build_input("c_li", "hello", None, "2026-09-19T12:00:00.000Z",
                          "https://example.com/link")
    assert payload["metadata"] == {"linkedin": {"firstComment":
                                                "https://example.com/link"}}


def test_an_x_post_never_carries_linkedin_metadata():
    """Introspection is unambiguous: TwitterPostMetadataInput has no
    firstComment. Its self-reply seam is `thread`, a whole post rather than a
    string (see .dev/FINDING_x_buffer_thread.md).

    Sending {"linkedin": {...}} on an X post is a schema-valid lie: the block
    would be accepted and ignored, or rejected outright. Either way the caller
    would believe a first comment had been scheduled.
    """
    payload = build_input("c_x", "hello", None, "2026-09-19T12:00:00.000Z",
                          "https://example.com/link", service="x")
    assert "metadata" not in payload


def test_the_x_post_still_carries_text_image_and_due_at():
    """Dropping the metadata must not drop the post."""
    payload = build_input("c_x", "hello", "https://img.example/i.png",
                          "2026-09-19T12:00:00.000Z", None, service="x")
    assert payload["channelId"] == "c_x"
    assert payload["text"] == "hello"
    assert payload["assets"] == [{"image": {"url": "https://img.example/i.png"}}]
    assert payload["dueAt"] == "2026-09-19T12:00:00.000Z"
    assert payload["mode"] == "customScheduled"


# ─── --thread-text: X's self-reply, shaped from the recorded introspection ────
#
# The payload shape is NOT guessed. It is read off the introspection response
# recorded in .harvest/buffer_spike_transcript.jsonl:
#
#     TwitterPostMetadataInput.thread   [ThreadedPostInput!]   <- a LIST
#     ThreadedPostInput.assets          [..]!                  <- NON_NULL
#
# Both matter, and both are the kind of thing that fails only at the live call.


def test_a_thread_reply_is_sent_as_a_list():
    """`thread` is [ThreadedPostInput!], so one reply is a list of one.

    A bare object survives GraphQL's single-value-to-list coercion, but writing
    the list is what the schema actually says, and it is the shape a second
    thread item would extend.
    """
    payload = build_input("c_x", "base post", None, "2026-09-19T12:00:00.000Z",
                          None, service="x", thread_text="the self-reply")
    thread = payload["metadata"]["twitter"]["thread"]
    assert isinstance(thread, list)
    assert len(thread) == 1
    assert thread[0]["text"] == "the self-reply"


def test_the_thread_item_carries_the_required_empty_assets_list():
    """ThreadedPostInput.assets is NON_NULL — omitting it is a schema error.

    The same trap as CreatePostInput.assets, which failed the first createPost
    attempt: a required list is not an optional one with a default.
    """
    payload = build_input("c_x", "base post", None, None, None,
                          service="x", thread_text="reply")
    assert payload["metadata"]["twitter"]["thread"][0]["assets"] == []


def test_without_thread_text_an_x_post_sends_no_metadata_at_all():
    """The base post — step 1 of the free-or-paid experiment.

    If this carried metadata, a rejection could not be attributed to the thread
    field, which is the entire thing being measured.
    """
    payload = build_input("c_x", "base post", "https://img.example/i.png",
                          "2026-09-19T12:00:00.000Z", None, service="x")
    assert "metadata" not in payload


def test_thread_text_is_ignored_on_linkedin():
    """LinkedIn has no thread field; its self-reply is firstComment."""
    payload = build_input("c_li", "post", None, None, None,
                          service="linkedin", thread_text="nope")
    assert "metadata" not in payload


def test_a_thread_never_displaces_a_linkedin_first_comment():
    """The two metadata blocks are per-network and must not collide."""
    payload = build_input("c_li", "post", None, None, "https://example.com/x",
                          service="linkedin", thread_text="ignored")
    assert payload["metadata"] == {"linkedin":
                                   {"firstComment": "https://example.com/x"}}
    assert "twitter" not in payload["metadata"]


def test_the_base_post_survives_alongside_a_thread():
    """Attaching a reply must not disturb text, image or dueAt."""
    payload = build_input("c_x", "base", "https://img.example/i.png",
                          "2026-09-19T12:00:00.000Z", None,
                          service="x", thread_text="reply")
    assert payload["text"] == "base"
    assert payload["assets"] == [{"image": {"url": "https://img.example/i.png"}}]
    assert payload["dueAt"] == "2026-09-19T12:00:00.000Z"


# ─── --key-env: one Buffer account per environment variable ──────────────────

def test_the_key_env_default_is_still_buffer_api_key(capsys, monkeypatch):
    """Existing invocations, with no --key-env, must still read BUFFER_API_KEY.

    Exercised through main() rather than asserted against the source: what
    matters is which variable actually gets read, not what the parser declares.
    Unsetting it is what makes the answer visible without a network call.
    """
    import tools.buffer_spike as spike
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["buffer_spike.py"])
    assert spike.main() == 2
    assert "BUFFER_API_KEY is not set" in capsys.readouterr().out


def test_a_missing_key_variable_fails_loudly_and_names_itself(capsys, monkeypatch):
    """Naming the variable is the point: with --key-env the operator may not be
    using the default, and "BUFFER_API_KEY is not set" would send them to the
    wrong line of .env."""
    import tools.buffer_spike as spike
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    monkeypatch.setattr("sys.argv", ["buffer_spike.py", "--key-env",
                                     "BUFFER_API_KEY_X"])
    rc = spike.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "BUFFER_API_KEY_X is not set" in out
    assert "BUFFER_API_KEY" in out           # still points at the default


def test_an_empty_key_variable_is_treated_as_missing(capsys, monkeypatch):
    """A variable present but blank is the same failure, not a 401 later."""
    import tools.buffer_spike as spike
    monkeypatch.setenv("BUFFER_API_KEY_X", "   ")
    monkeypatch.setattr("sys.argv", ["buffer_spike.py", "--key-env",
                                     "BUFFER_API_KEY_X"])
    assert spike.main() == 2
    assert "BUFFER_API_KEY_X is not set" in capsys.readouterr().out


# ─── --image-file: the real local -> R2 -> Buffer path ───────────────────────
#
# No R2 and no network here: image_host.upload_image is the seam, and these
# check that the spike CALLS it rather than re-implementing an upload.

def test_host_local_image_delegates_to_the_projects_image_host(monkeypatch):
    """The point of the flag is that it uses the REAL layer.

    A spike with its own uploader would prove that uploader works and tell us
    nothing about the path the pipeline actually takes.
    """
    import tools.buffer_spike as spike
    from linkedin_automation import image_host

    for var in image_host.REQUIRED_VARS:
        monkeypatch.setenv(var, "set-for-test")

    seen = {}

    def fake_upload(path, **kw):
        seen["path"] = path
        return "https://cdn.example/posts/abc123.png"

    monkeypatch.setattr(image_host, "upload_image", fake_upload)
    url = spike.host_local_image("S:/pictures/post.png")

    assert url == "https://cdn.example/posts/abc123.png"
    assert seen["path"] == "S:/pictures/post.png"


def test_a_missing_r2_config_is_named_before_anything_is_uploaded(monkeypatch):
    """Fail on the config, not on a half-finished upload."""
    import tools.buffer_spike as spike
    from linkedin_automation import image_host

    for var in image_host.REQUIRED_VARS:
        monkeypatch.delenv(var, raising=False)

    def explode(*a, **k):                      # must never be reached
        raise AssertionError("upload attempted with no config")

    monkeypatch.setattr(image_host, "upload_image", explode)

    with pytest.raises(spike.BufferError) as exc:
        spike.host_local_image("whatever.png")
    assert "R2_ACCESS_KEY_ID" in str(exc.value)


def test_the_hosted_url_is_what_reaches_the_payload(monkeypatch):
    """local file -> R2 URL -> assets[].image.url, with nothing lost between."""
    hosted = "https://cdn.example/posts/deadbeef.png"
    payload = build_input("c_x", "post", hosted, None, None, service="x")
    assert payload["assets"] == [{"image": {"url": hosted}}]


def test_the_two_image_flags_are_mutually_exclusive(capsys, monkeypatch):
    """Silent precedence between them is how the wrong image gets posted.

    This spike already carries --check-image-url because that happened once.
    Refusing is cheaper than diagnosing it a second time.
    """
    import tools.buffer_spike as spike
    monkeypatch.setenv("BUFFER_API_KEY", "irrelevant-never-used")
    monkeypatch.setattr("sys.argv", ["buffer_spike.py",
                                     "--image-url", "https://example/x.png",
                                     "--image-file", "./local.png"])
    assert spike.main() == 2
    assert "mutually exclusive" in capsys.readouterr().out


def test_a_read_only_run_never_uploads(capsys, monkeypatch):
    """--image-file without --create must not write to the bucket.

    Uploading is a write. The read-only default covers R2 too, not just Buffer.
    """
    import tools.buffer_spike as spike

    monkeypatch.setenv("BUFFER_API_KEY", "irrelevant-never-used")
    monkeypatch.setattr(spike, "discover",
                        lambda key, service="linkedin": ("org1", "chan1", []))
    monkeypatch.setattr(spike, "introspect", lambda key, names: {})
    monkeypatch.setattr(spike, "verify_claims", lambda found: {})

    def explode(path):
        raise AssertionError("uploaded during a read-only run")

    monkeypatch.setattr(spike, "host_local_image", explode)
    monkeypatch.setattr("sys.argv", ["buffer_spike.py",
                                     "--image-file", "./local.png"])

    assert spike.main() == 0
    out = capsys.readouterr().out
    assert "nothing uploaded" in out
    assert "STEP 3 - SKIPPED" in out
