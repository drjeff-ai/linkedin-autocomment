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

#: The scopes the token is minted with. ``youtube`` covers comment writes
#: later (Y1); ``youtube.upload`` is what videos.insert needs.
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
    which means nothing was created."""

    def __init__(self, message, quota=False):
        super().__init__(message)
        self.quota = quota


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
    return YouTubeApiError("%s failed (HTTP %s): %s"
                           % (what, status, ", ".join(r for r in reasons if r)
                              or text[:300]), quota=quota)


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


def run_consent(client_secrets_path):
    """The installed-app consent in the operator's browser. Returns the new
    credentials (with a refresh token)."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path),
                                                     scopes=SCOPES)
    # offline + consent: Google only issues a refresh token on a fresh consent.
    return flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                 open_browser=True)
