"""The Google boundary for the YouTube channel (V1, DEC-058).

Every call this repo makes to Google is in this module, and nothing else
imports the Google libraries, so tests mock one place. Ported from
video-publisher's ``src/platforms/youtube.py`` (reference only, not imported).

Two differences from that reference, both deliberate:

* **No retry anywhere.** video-publisher retries a resumable chunk twice. Here
  an upload error stops the run (D-026 V1 step 2), and the item stays
  ``attempting`` until a person checks Studio. A retry that ran after the
  video was created would be a second video.
* **The token is a file, not ``accounts.yaml``.** :func:`credentials_from_token`
  reads the dict ``tools/youtube_auth.py`` stored.

Callers log every call to the paid-call ledger before making it; this module
does not, because only the caller knows what the call is for.
"""

import logging

logger = logging.getLogger(__name__)

#: The scopes the token is minted with. ``youtube`` covers the comment reads
#: and writes of Y1; ``youtube.upload`` is what videos.insert needs.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

#: Resumable chunk size, as video-publisher uses (10 MB).
CHUNK_BYTES = 10 * 1024 * 1024

#: Reasons in a Google error that mean "nothing was created, over quota".
QUOTA_REASONS = ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded",
                 "rateLimitExceeded")

#: Every field a stored token must carry. Never printed.
TOKEN_FIELDS = ("client_id", "client_secret", "refresh_token")


class YouTubeApiError(RuntimeError):
    """A Google call failed. ``quota`` is True when Google said "over quota",
    which means nothing was created.

    ``ambiguous`` (Y1) is True when nobody can know whether a write happened:
    no HTTP answer at all (a timeout, a dropped connection) or a 5xx. A 4xx is
    Google refusing the request, so it is not ambiguous. ``status`` is the HTTP
    status when there was one."""

    def __init__(self, message, quota=False, ambiguous=False, status=None):
        super().__init__(message)
        self.quota = quota
        self.ambiguous = ambiguous
        self.status = status


def credentials_from_token(token):
    """Google credentials from a stored token dict. No network call."""
    from google.oauth2.credentials import Credentials
    missing = [f for f in TOKEN_FIELDS if not token.get(f)]
    if missing:
        raise YouTubeApiError("the stored token has no %s" % ", ".join(missing))
    return Credentials(token=None, refresh_token=token["refresh_token"],
                       token_uri=token.get("token_uri") or TOKEN_URI,
                       client_id=token["client_id"],
                       client_secret=token["client_secret"], scopes=SCOPES)


def refresh(creds):
    """Exchange the refresh token for an access token (one network call)."""
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    try:
        creds.refresh(Request())
    except GoogleAuthError as exc:
        # The message from google-auth names the error, never the token.
        raise YouTubeApiError("token refresh failed: %s" % type(exc).__name__) from exc


def build_service(creds):
    """The YouTube v3 client. Builds from the bundled discovery document, so
    building makes no network call."""
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False,
                 static_discovery=True)


def _error(exc, what):
    reasons = []
    try:
        reasons = [d.get("reason") for d in (exc.error_details or [])
                   if isinstance(d, dict)]
    except AttributeError:
        pass
    text = str(exc)
    quota = any(r in QUOTA_REASONS for r in reasons) or any(
        r in text for r in QUOTA_REASONS)
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = None
    return YouTubeApiError("%s failed (HTTP %s): %s"
                           % (what, status, ", ".join(r for r in reasons if r)
                              or text[:300]), quota=quota,
                           ambiguous=not quota and (code is None or code >= 500),
                           status=code)


def my_channel_id(service):
    """The id of the channel the token answers for (``channels.list mine``,
    1 quota unit). Raises :class:`YouTubeApiError` when there is not exactly one."""
    from googleapiclient.errors import HttpError
    try:
        resp = service.channels().list(part="id", mine=True).execute()
    except HttpError as exc:
        raise _error(exc, "channels.list") from exc
    ids = [i.get("id") for i in resp.get("items") or []]
    if len(ids) != 1 or not ids[0]:
        raise YouTubeApiError("the token answers for %d channels, expected one"
                              % len(ids))
    return ids[0]


def upload(service, media_path, body):
    """One resumable ``videos.insert``. Returns the new video id.

    Raises :class:`YouTubeApiError` on any failure, with no retry.
    """
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(media_path), mimetype="video/*",
                            resumable=True, chunksize=CHUNK_BYTES)
    request = service.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("upload %.0f%%", status.progress() * 100)
    except HttpError as exc:
        raise _error(exc, "videos.insert") from exc
    except (OSError, ValueError) as exc:
        raise YouTubeApiError("videos.insert failed: %s: %s"
                              % (type(exc).__name__, exc)) from exc
    video_id = (response or {}).get("id")
    if not video_id:
        raise YouTubeApiError("videos.insert returned no video id")
    return video_id


#: Stored for a 403 from thumbnails.set that says the channel may not set
#: custom thumbnails (V2, D-028). The fix is on YouTube's side, not ours.
THUMBNAIL_NOT_ALLOWED = ("channel not verified for custom thumbnails "
                         "(phone verification in YouTube settings)")

#: Words in that 403's message. Inferred from Google's documented error for
#: thumbnails.set ("The authenticated user doesn't have permissions to upload
#: and set custom video thumbnails"), not from a captured response.
_THUMBNAIL_403_WORDS = ("thumbnail", "permission")


def set_thumbnail(service, video_id, image_path, mimetype):
    """One ``thumbnails.set`` (50 units) for ``video_id``. Returns None.

    Raises :class:`YouTubeApiError` on any failure, with no retry. A 403 whose
    message says the channel cannot set custom thumbnails carries
    :data:`THUMBNAIL_NOT_ALLOWED` as its message."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(image_path), mimetype=mimetype, resumable=False)
    try:
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
    except HttpError as exc:
        err = _error(exc, "thumbnails.set")
        # Google's message only: str(exc) carries the request URI, which
        # always names thumbnails.
        message = str(getattr(exc, "reason", "") or "").lower()
        if err.status == 403 and not err.quota \
                and all(w in message for w in _THUMBNAIL_403_WORDS):
            raise YouTubeApiError(THUMBNAIL_NOT_ALLOWED, status=403) from exc
        raise err from exc
    except (OSError, ValueError) as exc:
        raise YouTubeApiError("thumbnails.set failed: %s: %s"
                              % (type(exc).__name__, exc)) from exc
    except Exception as exc:  # timeouts, resets, TLS: no answer from Google
        raise YouTubeApiError("thumbnails.set failed with no answer: %s: %s"
                              % (type(exc).__name__, str(exc)[:200]),
                              ambiguous=True) from exc


def run_consent(client_secrets_path):
    """The installed-app consent in the operator's browser. Returns the new
    credentials (with a refresh token)."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path),
                                                     scopes=SCOPES)
    # offline + consent: Google only issues a refresh token on a fresh consent.
    return flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                 open_browser=True)


# ── engagement (Y1, D-027) ──────────────────────────────────────────────────
#
# Read calls cost 1 unit a page except ``search.list`` (100); the two inserts
# cost 50. The caller charges the quota ledger and the paid-call ledger before
# each call. Nothing here retries.

def _call(request, what):
    """Execute one request. Every failure is a :class:`YouTubeApiError`; one
    with no HTTP answer is ambiguous."""
    from googleapiclient.errors import HttpError
    try:
        return request.execute()
    except HttpError as exc:
        raise _error(exc, what) from exc
    except Exception as exc:  # timeouts, resets, TLS: no answer from Google
        raise YouTubeApiError("%s failed with no answer: %s: %s"
                              % (what, type(exc).__name__, str(exc)[:200]),
                              ambiguous=True) from exc


def _author_id(snippet):
    return ((snippet or {}).get("authorChannelId") or {}).get("value")


def search_channels(service, query, max_results=10):
    """``search.list type=channel`` (100 units). ``[{channel_id, title,
    description}]``."""
    resp = _call(service.search().list(part="snippet", type="channel", q=query,
                                       maxResults=max_results), "search.list")
    out = []
    for item in resp.get("items") or []:
        sn = item.get("snippet") or {}
        cid = (item.get("id") or {}).get("channelId") or sn.get("channelId")
        if cid:
            out.append({"channel_id": cid,
                        "title": sn.get("channelTitle") or sn.get("title") or "",
                        "description": sn.get("description") or ""})
    return out


def channels_info(service, channel_ids):
    """``channels.list id=...`` for up to 50 ids (1 unit). ``{channel_id:
    {title, subscribers, uploads_playlist}}``. ``subscribers`` is None when the
    channel hides its count."""
    resp = _call(service.channels().list(part="snippet,statistics,contentDetails",
                                         id=",".join(channel_ids), maxResults=50),
                 "channels.list")
    return {item.get("id"): _channel_entry(item) for item in resp.get("items") or []}


def _channel_entry(item):
    stats = item.get("statistics") or {}
    subs = None
    if not stats.get("hiddenSubscriberCount") and stats.get("subscriberCount") is not None:
        try:
            subs = int(stats["subscriberCount"])
        except (TypeError, ValueError):
            subs = None
    related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
    return {
        "title": (item.get("snippet") or {}).get("title") or "",
        "subscribers": subs,
        "uploads_playlist": related.get("uploads"),
    }


def channel_by_handle(service, handle):
    """``channels.list forHandle=...`` (1 unit). ``{channel_id, title,
    subscribers, uploads_playlist}``, or None when no channel has that handle.
    ``handle`` may carry its leading ``@``; Google accepts both."""
    handle = (handle or "").strip()
    if not handle or handle == "@":
        raise YouTubeApiError("channels.list forHandle needs a handle")
    resp = _call(service.channels().list(part="snippet,statistics,contentDetails",
                                         forHandle=handle), "channels.list")
    items = [i for i in resp.get("items") or [] if i.get("id")]
    if not items:
        return None
    if len(items) > 1:
        raise YouTubeApiError("channels.list forHandle=%s returned %d channels, "
                              "expected at most one" % (handle, len(items)))
    return dict(channel_id=items[0]["id"], **_channel_entry(items[0]))


def playlist_page(service, playlist_id, page_token=None):
    """One page of ``playlistItems.list`` (1 unit). ``([{video_id, title,
    description, published_at, channel_id, channel_title}], next_page_token)``."""
    kwargs = {"part": "snippet,contentDetails", "playlistId": playlist_id,
              "maxResults": 50}
    if page_token:
        kwargs["pageToken"] = page_token
    resp = _call(service.playlistItems().list(**kwargs), "playlistItems.list")
    items = []
    for item in resp.get("items") or []:
        sn = item.get("snippet") or {}
        cd = item.get("contentDetails") or {}
        vid = cd.get("videoId") or (sn.get("resourceId") or {}).get("videoId")
        if vid:
            items.append({
                "video_id": vid, "title": sn.get("title") or "",
                "description": sn.get("description") or "",
                "published_at": cd.get("videoPublishedAt") or sn.get("publishedAt"),
                "channel_id": sn.get("videoOwnerChannelId") or sn.get("channelId"),
                "channel_title": (sn.get("videoOwnerChannelTitle")
                                  or sn.get("channelTitle") or "")})
    return items, resp.get("nextPageToken")


def _text(snippet):
    snippet = snippet or {}
    return snippet.get("textOriginal") or snippet.get("textDisplay") or ""


def _thread(item):
    sn = item.get("snippet") or {}
    top = sn.get("topLevelComment") or {}
    tsn = top.get("snippet") or {}
    replies = [{"id": c.get("id"), "author_channel_id": _author_id(c.get("snippet")),
                "text": _text(c.get("snippet"))}
               for c in ((item.get("replies") or {}).get("comments") or [])]
    return {"thread_id": item.get("id"), "comment_id": top.get("id") or item.get("id"),
            "video_id": sn.get("videoId") or tsn.get("videoId"),
            "author_channel_id": _author_id(tsn),
            "author": tsn.get("authorDisplayName") or "",
            "text": _text(tsn),
            "published_at": tsn.get("publishedAt"),
            "like_count": tsn.get("likeCount") or 0,
            "total_reply_count": sn.get("totalReplyCount") or 0,
            "replies": replies}


def comment_threads_page(service, video_id=None, related_channel_id=None,
                         page_token=None, order="time", max_results=100):
    """One page of ``commentThreads.list`` (1 unit), for one video or for every
    thread on a channel's videos (``allThreadsRelatedToChannelId``).
    ``([thread], next_page_token)``."""
    kwargs = {"part": "snippet,replies", "maxResults": max_results, "order": order,
              "textFormat": "plainText"}
    if page_token:
        kwargs["pageToken"] = page_token
    if video_id:
        kwargs["videoId"] = video_id
    elif related_channel_id:
        kwargs["allThreadsRelatedToChannelId"] = related_channel_id
    else:
        raise ValueError("video_id or related_channel_id is required")
    resp = _call(service.commentThreads().list(**kwargs), "commentThreads.list")
    return [_thread(i) for i in resp.get("items") or []], resp.get("nextPageToken")


def replies_page(service, parent_id, page_token=None):
    """One page of ``comments.list parentId=...`` (1 unit). ``([{id,
    author_channel_id, text}], next_page_token)``."""
    kwargs = {"part": "snippet", "parentId": parent_id, "maxResults": 100,
              "textFormat": "plainText"}
    if page_token:
        kwargs["pageToken"] = page_token
    resp = _call(service.comments().list(**kwargs), "comments.list")
    out = [{"id": c.get("id"), "author_channel_id": _author_id(c.get("snippet")),
            "text": _text(c.get("snippet"))}
           for c in resp.get("items") or []]
    return out, resp.get("nextPageToken")


def video_titles(service, video_ids):
    """``videos.list id=...`` (1 unit). ``{video_id: title}``."""
    resp = _call(service.videos().list(part="snippet", id=",".join(video_ids),
                                       maxResults=50), "videos.list")
    return {i.get("id"): (i.get("snippet") or {}).get("title") or ""
            for i in resp.get("items") or []}


def insert_thread(service, channel_id, video_id, text):
    """``commentThreads.insert`` (50 units): a top-level comment on someone
    else's video. Returns the new comment's id, which is the proof it was
    created. A success answer with no id is ambiguous."""
    body = {"snippet": {"channelId": channel_id, "videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": text}}}}
    resp = _call(service.commentThreads().insert(part="snippet", body=body),
                 "commentThreads.insert")
    top = (resp.get("snippet") or {}).get("topLevelComment") or {}
    cid = top.get("id") or resp.get("id")
    if not cid:
        raise YouTubeApiError("commentThreads.insert answered with no comment id",
                              ambiguous=True)
    return cid


def insert_reply(service, parent_id, text):
    """``comments.insert`` (50 units): a reply under a viewer's comment.
    Returns the new comment's id."""
    body = {"snippet": {"parentId": parent_id, "textOriginal": text}}
    resp = _call(service.comments().insert(part="snippet", body=body),
                 "comments.insert")
    cid = resp.get("id")
    if not cid:
        raise YouTubeApiError("comments.insert answered with no comment id",
                              ambiguous=True)
    return cid
