"""youtube_api.channel_by_handle (D-031 W1): ``channels.list forHandle``.

Built on the real (static) discovery document with a mocked HTTP layer, so
the test proves the client accepts ``forHandle`` and sends it, not only that a
fake object was called.
"""

import json

import pytest
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from linkedin_automation import youtube_api

CID = "UC" + "a" * 22


def _service(*responses):
    seq = [({"status": str(status)}, json.dumps(body)) for status, body in responses]
    http = HttpMockSequence(seq)
    service = build("youtube", "v3", http=http, cache_discovery=False,
                    static_discovery=True)
    return service, http


def _item(cid=CID, title="Some Channel", subs="12345", hidden=False):
    return {"id": cid, "snippet": {"title": title},
            "statistics": {"subscriberCount": subs, "hiddenSubscriberCount": hidden},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}


def test_found_returns_the_channel_and_sends_for_handle():
    service, http = _service((200, {"items": [_item()]}))
    got = youtube_api.channel_by_handle(service, "@somechannel")
    assert got == {"channel_id": CID, "title": "Some Channel", "subscribers": 12345,
                   "uploads_playlist": "UU" + CID[2:]}
    (uri, method, _body, _headers), = http.request_sequence
    assert method == "GET"
    assert "/youtube/v3/channels?" in uri
    assert "forHandle=%40somechannel" in uri
    assert "&id=" not in uri and "mine=" not in uri


def test_surrounding_space_is_stripped():
    service, http = _service((200, {"items": [_item()]}))
    youtube_api.channel_by_handle(service, "  @somechannel ")
    assert "forHandle=%40somechannel&" in http.request_sequence[0][0] + "&"


def test_no_channel_returns_none():
    service, _ = _service((200, {"items": []}))
    assert youtube_api.channel_by_handle(service, "@nobody") is None
    service, _ = _service((200, {}))
    assert youtube_api.channel_by_handle(service, "@nobody") is None


def test_hidden_subscribers_are_none():
    service, _ = _service((200, {"items": [_item(hidden=True)]}))
    assert youtube_api.channel_by_handle(service, "@x")["subscribers"] is None


def test_more_than_one_channel_is_an_error():
    service, _ = _service((200, {"items": [_item(), _item(cid="UC" + "b" * 22)]}))
    with pytest.raises(youtube_api.YouTubeApiError, match="expected at most one"):
        youtube_api.channel_by_handle(service, "@x")


@pytest.mark.parametrize("handle", ["", "   ", "@", None])
def test_empty_handle_is_refused_without_a_call(handle):
    service, http = _service()
    with pytest.raises(youtube_api.YouTubeApiError, match="needs a handle"):
        youtube_api.channel_by_handle(service, handle)
    assert http.request_sequence == []


def test_an_http_error_is_a_youtube_api_error():
    service, _ = _service((403, {"error": {"errors": [{"reason": "quotaExceeded"}],
                                           "message": "quota"}}))
    with pytest.raises(youtube_api.YouTubeApiError) as info:
        youtube_api.channel_by_handle(service, "@x")
    assert info.value.quota is True


def test_channels_info_still_parses_after_the_refactor():
    service, _ = _service((200, {"items": [_item(), _item(cid="UC" + "b" * 22,
                                                          subs="7", title="B")]}))
    got = youtube_api.channels_info(service, [CID, "UC" + "b" * 22])
    assert got[CID]["subscribers"] == 12345
    assert got["UC" + "b" * 22] == {"title": "B", "subscribers": 7,
                                    "uploads_playlist": "UU" + "b" * 22}


# ── my_channel_snippet (D-033 Y2-T) ─────────────────────────────────────────

def test_my_channel_snippet_sends_mine_and_snippet_and_returns_the_handle():
    item = {"id": CID, "snippet": {"title": "Some Channel", "customUrl": "@somechannel"}}
    service, http = _service((200, {"items": [item]}))
    assert youtube_api.my_channel_snippet(service) == {
        "channel_id": CID, "title": "Some Channel", "handle": "@somechannel"}
    (uri, method, _b, _h), = http.request_sequence
    assert method == "GET" and "/youtube/v3/channels?" in uri
    assert "mine=true" in uri and "part=snippet" in uri and "forHandle" not in uri


def test_my_channel_snippet_without_a_custom_url_has_an_empty_handle():
    service, _ = _service((200, {"items": [{"id": CID, "snippet": {"title": "T"}}]}))
    assert youtube_api.my_channel_snippet(service)["handle"] == ""


@pytest.mark.parametrize("items", [[], [{"id": CID}, {"id": "UC" + "b" * 22}]])
def test_my_channel_snippet_needs_exactly_one_channel(items):
    service, _ = _service((200, {"items": items}))
    with pytest.raises(youtube_api.YouTubeApiError, match="expected one"):
        youtube_api.my_channel_snippet(service)
