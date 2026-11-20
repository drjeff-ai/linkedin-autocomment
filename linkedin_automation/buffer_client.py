"""Schedule a LinkedIn post through Buffer's GraphQL API.

Phase 2 of the scheduled-posting hybrid. Buffer owns the post itself — text,
image and timing — and the browser tool adds the first comment afterwards,
because ``firstComment`` is paid-plan only.

**The call shape here is not a guess.** Every field was checked against the live
schema during the spike, and five of them were wrong on the first attempt:

* ``assets`` is ``[AssetInput!]!`` — required and non-null. Omitting it for a
  text-only post is a schema error, not a default, so it is always sent.
* ``mode`` is ``ShareMode!``; ``customScheduled`` is the member that honours
  ``dueAt``.
* ``needsApproval`` is ``Boolean!`` with no default.
* ``schedulingType`` is ``SchedulingType!`` whose members are ``automatic`` and
  ``notification``. There is **no** ``custom`` — scheduling is chosen by
  ``mode``, and sending ``custom`` fails the whole call.
* Every id is its own scalar (``ChannelId!``, ``OrganizationId!``, ``PostId!``),
  never ``String!``.

**``firstComment`` is deliberately never sent.** On the free plan it does not
degrade — it rejects the ENTIRE post, publishing nothing. The comment is Phase
4's job.

**A bad image fails here, loudly.** Buffer reads the image bytes at createPost
time and returns ``InvalidInputError`` ("Image could not be read from its URL")
if it cannot, creating nothing. That is a good property: the row fails while a
human can still fix it, rather than publishing a post without its picture.
"""

import hashlib
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from . import image_host

load_dotenv()

logger = logging.getLogger(__name__)

API_URL = "https://api.buffer.com/graphql"
USAGE_LOG = "api_usage.jsonl"

# Named windows, in the POSTING TIMEZONE (not UTC). These are the local hours a
# human would plausibly post in; the randomised minute inside them is what stops
# every post landing on the same round number.
#
# Chosen for a US-Pacific audience and configurable per deployment. See
# DECISIONS.md for why this is a local-time concept converted to UTC late.
#
# TIME_WINDOWS is every window a row may NAME (lookup and validation).
# DEFAULT_WINDOWS is the set a caller gets without naming any: anything that
# picks windows on its own (``csv_pipeline._window_slots``,
# ``_first_open_slot``, ``redate_pending`` without ``windows_for``) iterates
# DEFAULT_WINDOWS only. ``late_afternoon`` and ``night`` are opt-in
# (DEC-046): placed only when a caller names them, so no channel's cadence
# rises because they exist.
TIME_WINDOWS = {
    "morning": (8, 0, 11, 0),
    "afternoon": (12, 0, 15, 0),
    "late_afternoon": (15, 0, 17, 0),
    "evening": (17, 0, 20, 0),
    "night": (20, 0, 22, 0),
}
DEFAULT_WINDOWS = ("morning", "afternoon", "evening")

DEFAULT_TIMEZONE = "America/Los_Angeles"


class BufferError(RuntimeError):
    """Anything that stops a row becoming a scheduled Buffer post."""


class BufferRejected(BufferError):
    """Buffer refused the post. Nothing was created.

    Its own type because it is a ROW problem the human can fix — usually an
    image URL Buffer could not read — rather than a transport or auth failure.
    """


class BufferAtCapacity(BufferError):
    """No scheduled-post slot free. The row is fine; there is simply no room.

    Its own type so a caller can hold the row instead of condemning it. Marking
    a perfectly good post FAILED because the plan was full would tell the
    operator to go and fix something that is not broken.
    """


class BufferPublishFailed(BufferError):
    """Buffer tried to publish and LinkedIn refused it.

    Distinct from a timeout: the post is not slow, it is dead. Retrying the
    poll would never succeed, and the first comment must not be attempted.
    """


class BufferPublishBlocked(BufferError):
    """The post will never publish on its own - it is a draft or awaiting
    approval. Waiting out the full timeout would be a slow way to learn this."""


class BufferRateLimited(BufferError):
    """Buffer is refusing requests for now (RATE_LIMIT_EXCEEDED).

    Distinct from a generic BufferError because the correct response is to
    back off and show the last known state, never to retry in a loop. The free
    plan allows 3,000 requests per 30 days - about 100 a day - and there is a
    shorter rolling window on top of that.
    """


class BufferPublishTimeout(BufferError):
    """Still not published when the ceiling was reached.

    NOT the same as failure: the post may yet go out. It means only that this
    run stopped waiting, so the first comment is deferred rather than abandoned.
    """


def posting_timezone(env=None):
    """The timezone named windows are interpreted in."""
    from zoneinfo import ZoneInfo

    env = os.environ if env is None else env
    name = (env.get("POST_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise BufferError(
            "POST_TIMEZONE=%r is not a known zone (%s). On Windows this also "
            "needs the tzdata package, which is in requirements.txt."
            % (name, exc)) from exc


def due_at(date_str, window, rng=None, tz=None, now=None):
    """Turn ``('2026-09-08', 'morning')`` into an ISO 8601 UTC timestamp.

    The randomised minute is picked inside the window's LOCAL hours and only
    then converted to UTC, which is the order that survives DST: Los Angeles is
    UTC-7 in summer and UTC-8 in winter, so a fixed offset would put every post
    an hour out for half the year.
    """
    rng = rng or random
    tz = tz or posting_timezone()

    key = (window or "").strip().lower()
    if key not in TIME_WINDOWS:
        raise BufferError(
            "unknown time_window %r - expected one of %s"
            % (window, ", ".join(sorted(TIME_WINDOWS))))
    try:
        day = datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise BufferError(
            "bad date %r - expected YYYY-MM-DD (%s)" % (date_str, exc)) from exc

    sh, sm, eh, em = TIME_WINDOWS[key]
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
    span = int((end - start).total_seconds())
    local = start + timedelta(seconds=rng.randint(0, span) if span > 0 else 0)

    when = local.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if when <= now:
        raise BufferError(
            "%s %s resolves to %s, which is in the past. Buffer will not "
            "schedule it." % (date_str, window, when.isoformat()))
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _image_asset(image_url, alt_text=None):
    """One image asset. Alt text rides on it as ``metadata.altText``.

    ``ImageMetadataInput.altText`` is required whenever ``metadata`` is sent
    (pin.publisher's capture), so a blank alt text sends no ``metadata`` at
    all rather than an empty one Buffer would refuse.
    """
    image = {"url": image_url}
    alt = (alt_text or "").strip()
    if alt:
        image["metadata"] = {"altText": alt}
    return {"image": image}


def build_post_input(channel_id, text, image_url, due_at_iso, metadata=None,
                     alt_text=None):
    """The createPost input, in the shape the live schema actually accepts.

    ``metadata`` is the channel's Buffer metadata (Pinterest's board, title and
    link), added only when given, so a channel that declares none sends exactly
    the keys it always has.
    """
    payload = {
        "channelId": channel_id,
        "text": text,
        # Required and non-null even when there is no image.
        "assets": [_image_asset(image_url, alt_text)] if image_url else [],
        "mode": "customScheduled",
        "schedulingType": "automatic",
        "needsApproval": False,
        "dueAt": due_at_iso,
    }
    # metadata.firstComment is NEVER set. On the free plan it does not degrade
    # gracefully - it rejects the whole post and nothing is created.
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def compose_text(post_text, tags=None):
    """Body plus tags, with a blank line between them.

    Buffer is handed the text verbatim and does its own newline handling, so
    the browser composer's lossy-newline problem does not apply on this path -
    but that is worth confirming on the first live post rather than assuming.
    """
    body = (post_text or "").strip()
    tag_line = (tags or "").strip()
    if not body:
        raise BufferError("post_text is empty and no generated text was supplied")
    return "%s\n\n%s" % (body, tag_line) if tag_line else body


def _log_usage(endpoint, extra=None):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api": "buffer-graphql",
        "endpoint": endpoint,
        "estimated_cost": 0.0,
        "quota_unit": "1 of 3000 per 30 days",
    }
    if extra:
        record.update(extra)
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        logger.debug("could not append to %s", USAGE_LOG, exc_info=True)


def api_key(env=None):
    env = os.environ if env is None else env
    key = (env.get("BUFFER_API_KEY") or "").strip()
    if not key:
        raise BufferError(
            "BUFFER_API_KEY is not set. It lives in .env and is never "
            "committed; see .env.example.")
    return key


def gql(query, variables=None, key=None, label="graphql", session=None):
    """One GraphQL round trip. Raises on transport and auth failures."""
    key = key or api_key()
    _log_usage(label)
    post = (session or requests).post
    try:
        resp = post(API_URL,
                    headers={"Authorization": "Bearer %s" % key,
                             "Content-Type": "application/json"},
                    json={"query": query, "variables": variables or {}},
                    timeout=60)
    except requests.RequestException as exc:
        raise BufferError("Buffer request failed (%s): %s" % (label, exc)) from exc

    if resp.status_code == 401:
        raise BufferError("Buffer rejected the API key (401)")
    try:
        body = resp.json()
    except ValueError as exc:
        raise BufferError("Buffer returned non-JSON (HTTP %s)"
                          % resp.status_code) from exc

    # A GraphQL 200 carrying an `errors` array is a failure that looks like a
    # success to anything checking status codes.
    if body.get("errors"):
        blob = json.dumps(body["errors"])
        if "RATE_LIMIT_EXCEEDED" in blob or "Too many requests" in blob:
            raise BufferRateLimited(
                "Buffer is rate-limiting this client (%s). The free plan allows "
                "3,000 requests per 30 days; back off rather than retrying."
                % label)
        raise BufferError("Buffer GraphQL errors (%s): %s"
                          % (label, blob[:500]))
    return body


M_CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post { id status dueAt text assets { id mimeType source } }
    }
    ... on MutationError { message }
    ... on InvalidInputError { message }
  }
}
"""


Q_CHANNEL = """
query GetChannel($id: ChannelId!) {
  channel(input: { id: $id }) {
    id
    name
    displayName
    service
    externalLink
    isDisconnected
    organizationId
  }
}
"""


#: Channel metadata is effectively static (a channel's organizationId and
#: service do not change under us), but scheduled_slots() resolves the channel
#: on EVERY call, so a background feeder checking slots every 15 minutes was
#: spending two requests where it needed one. Over a day that was 181 getChannel
#: calls against a budget of about 100 requests total. Cached for an hour.
_CHANNEL_TTL_SECONDS = 3600
_channel_cache = {}


#: The slot count is read on every render of the scheduled-posting tab (the
#: drain's status line asks for it), which is far more often than it can
#: meaningfully change. Cached briefly so opening a tab twice costs one request.
#: Callers that must not act on a stale count pass max_age=0 - see below.
_SLOTS_TTL_SECONDS = 60
_slots_cache = {}


def _key_fingerprint(key=None):
    """A short, non-reversible tag for the key a call will actually use.

    Both caches used to key on ``channel_id`` ALONE. That is harmless with one
    Buffer account and a cross-account leak with two: the same channel id under
    a different key would be served the FIRST key's channel — and therefore the
    first key's organizationId, and therefore the first account's slot count.
    A feeder would then decide how many posts the other account could take
    based on a number that was never about it.

    The key itself is never used as a dict key: cache dicts get logged, repr'd
    and dumped in tracebacks, and a credential must not ride along. A 12-hex
    digest is ample to separate accounts and reveals nothing.

    ``key=None`` means "whatever the environment holds", which is what
    ``gql`` falls back to, so it is fingerprinted the same way and an explicit
    key equal to the env one shares its bucket rather than duplicating it.
    Read directly rather than through :func:`api_key` because a cache lookup
    must not raise on an unset variable.
    """
    effective = (key or os.environ.get("BUFFER_API_KEY") or "").strip()
    if not effective:
        return "unset"
    return hashlib.sha256(effective.encode("utf-8")).hexdigest()[:12]


def clear_caches():
    """Drop cached channel/post reads. For tests and for a forced refresh."""
    _channel_cache.clear()
    _slots_cache.clear()


def get_channel(channel_id, key=None, session=None, max_age=None):
    """Resolve a channel id to the account it actually posts as.

    Used by the UI's confirmation dialog. A name kept in local config could go
    stale and still read "dev" while the id now points somewhere else; asking
    Buffer what this id IS makes the dialog name the real destination.

    Cached for :data:`_CHANNEL_TTL_SECONDS`; pass ``max_age=0`` to force a read.
    """
    import time as _time

    ttl = _CHANNEL_TTL_SECONDS if max_age is None else max_age
    cache_key = (_key_fingerprint(key), channel_id)
    hit = _channel_cache.get(cache_key)
    if hit and ttl and (_time.time() - hit[0]) < ttl:
        return hit[1]

    body = gql(Q_CHANNEL, {"id": channel_id}, key=key, label="getChannel",
               session=session)
    channel = (body.get("data") or {}).get("channel")
    if not channel:
        raise BufferError("Buffer returned no channel for id %r" % channel_id)
    _channel_cache[cache_key] = (_time.time(), channel)
    return channel


#: Verbatim from pin.publisher ``pinterest_adapter.py`` (``_CHANNEL_BOARDS_QUERY``),
#: the query its live captures were taken with.
Q_CHANNEL_BOARDS = """
query GetChannelBoards($channelId: ChannelId!) {
  channel(input: { id: $channelId }) {
    id
    metadata {
      ... on PinterestMetadata {
        boards {
          id
          serviceId
          name
          url
          description
        }
      }
    }
  }
}
"""


class BoardLookupError(ValueError):
    """A row's board does not name exactly one of the channel's boards."""


class BoardNotFound(BoardLookupError):
    """No board on the channel has the row's board name."""


class BoardAmbiguous(BoardLookupError):
    """Two or more boards on the channel share the row's board name."""


def channel_boards(channel_id, key=None, session=None):
    """The Pinterest channel's boards, as ``[{id, serviceId, name}]``.

    Never cached: a board renamed or deleted in Pinterest must be seen on the
    next call, and a pin sent to a board that no longer exists is a pin in the
    wrong place. Raises :class:`BufferError` when Buffer returns no channel or
    no Pinterest metadata for it (not a Pinterest channel); an empty board list
    is returned as ``[]``.
    """
    body = gql(Q_CHANNEL_BOARDS, {"channelId": channel_id}, key=key,
               label="getChannelBoards", session=session)
    channel = (body.get("data") or {}).get("channel")
    if not channel:
        raise BufferError("Buffer returned no channel for id %r" % channel_id)
    metadata = channel.get("metadata")
    boards = metadata.get("boards") if isinstance(metadata, dict) else None
    if boards is None:
        raise BufferError(
            "Buffer returned no Pinterest boards for channel %r: it is not a "
            "Pinterest channel, or Buffer sent no board metadata." % channel_id)
    return [{"id": str(b.get("id") or ""),
             "serviceId": str(b.get("serviceId") or ""),
             "name": str(b.get("name") or "")}
            for b in boards if isinstance(b, dict)]


def resolve_board(boards, name):
    """The ``serviceId`` of the one board whose name is exactly ``name``.

    Exact: case and whitespace count, so "Secret" or "secret " never resolves
    to "secret". A near match is a guess, and a pin on the wrong board is a
    public post in the wrong place. Returns Buffer's ``serviceId`` (what
    ``metadata.pinterest.boardServiceId`` takes), never Buffer's own ``id``.

    Raises :class:`BoardNotFound` listing every board name, or
    :class:`BoardAmbiguous` when two boards share the name.
    """
    matches = [b for b in boards if b.get("name") == name]
    if not matches:
        names = ", ".join(repr(b.get("name")) for b in boards) or "none"
        raise BoardNotFound(
            "board %r is not on this Pinterest channel. Its boards are: %s. "
            "The name must match exactly, including case and spaces."
            % (name, names))
    if len(matches) > 1:
        raise BoardAmbiguous(
            "board %r names %d boards on this Pinterest channel, so which one "
            "is meant cannot be told. Rename one in Pinterest."
            % (name, len(matches)))
    service_id = (matches[0].get("serviceId") or "").strip()
    if not service_id:
        raise BoardNotFound(
            "board %r has no serviceId in Buffer's answer, so a pin cannot be "
            "addressed to it." % name)
    return service_id


Q_LIMITS = """
query GetLimits($org: OrganizationId!) {
  account { organizations { id limits { scheduledPosts } } }
  posts(first: 100, input: { organizationId: $org,
                             filter: { status: [scheduled] } }) {
    edges { node { id channelId } }
  }
}
"""


def scheduled_slots(channel_id, key=None, session=None, max_age=None):
    """How many more posts this channel can have scheduled at once.

    Buffer's free plan caps SCHEDULED (not yet published) posts, and the cap is
    read from the API rather than hardcoded - it is a plan property and a plan
    can change under us. Slots free again as posts publish.

    Sending more than this is not a soft failure: every post over the line is
    rejected with LimitReachedError, which is why the count is asked for BEFORE
    anything is sent rather than discovered one rejection at a time.

    Cached for :data:`_SLOTS_TTL_SECONDS`. **Anything about to actually send
    must pass ``max_age=0``**: acting on a stale count is precisely what HELD
    exists to prevent. The cache is for the status line and the preflight, which
    are re-read on every render and only inform a human.
    """
    import time as _time

    ttl = _SLOTS_TTL_SECONDS if max_age is None else max_age
    cache_key = (_key_fingerprint(key), channel_id)
    hit = _slots_cache.get(cache_key)
    if hit and ttl and (_time.time() - hit[0]) < ttl:
        return dict(hit[1])

    channel = get_channel(channel_id, key=key, session=session)
    org = channel.get("organizationId")
    if not org:
        raise BufferError("channel %r has no organizationId" % channel_id)

    body = gql(Q_LIMITS, {"org": org}, key=key, label="scheduledSlots",
               session=session)
    data = body.get("data") or {}
    orgs = ((data.get("account") or {}).get("organizations")) or []
    limit = None
    for o in orgs:
        if o.get("id") == org:
            limit = ((o.get("limits") or {}).get("scheduledPosts"))
    if limit is None:
        raise BufferError("Buffer did not report a scheduledPosts limit")

    edges = ((data.get("posts") or {}).get("edges")) or []
    # The limit is reported per ORGANISATION, so every scheduled post counts
    # toward it, not only this channel's. Counting just our own would overstate
    # the free slots and walk straight back into LimitReachedError.
    used = len(edges)
    mine = sum(1 for e in edges
               if ((e.get("node") or {}).get("channelId")) == channel_id)
    result = {"limit": int(limit), "used": used, "used_this_channel": mine,
              "free": max(0, int(limit) - used)}
    _slots_cache[cache_key] = (_time.time(), dict(result))
    return result


def create_post(channel_id, text, image_url, due_at_iso, key=None, session=None,
                metadata=None, alt_text=None):
    """Schedule one post. Returns the created post dict, or raises."""
    payload = build_post_input(channel_id, text, image_url, due_at_iso,
                               metadata=metadata, alt_text=alt_text)
    logger.info("Buffer createPost: dueAt=%s image=%s chars=%d",
                due_at_iso, bool(image_url), len(text))
    body = gql(M_CREATE_POST, {"input": payload}, key=key,
               label="createPost", session=session)

    result = (body.get("data") or {}).get("createPost") or {}
    kind = result.get("__typename")
    if kind == "PostActionSuccess":
        post = result.get("post") or {}
        logger.info("  scheduled: id=%s status=%s dueAt=%s",
                    post.get("id"), post.get("status"), post.get("dueAt"))
        return post

    message = result.get("message") or "no message"
    if kind == "LimitReachedError" or "limit reached" in message.lower():
        raise BufferAtCapacity(
            "Buffer has no scheduled-post slot free: %s" % message)
    # Buffer reads the image at createPost time, so an unreadable URL lands
    # here rather than silently publishing a picture-less post later.
    raise BufferRejected(
        "Buffer refused the post (%s): %s. NOTHING was created." % (kind, message))


def schedule_row(channel_id, post_text, tags, image_path, date_str, window,
                 rng=None, key=None, session=None, upload=None):
    """The Phase 1 + Phase 2 chain for one CSV row: image -> R2 -> Buffer.

    ``image_path`` is a LOCAL path. Buffer cannot take one, so it is uploaded
    first and the resulting public URL is what Buffer is given.
    """
    upload = upload or image_host.upload_image
    text = compose_text(post_text, tags)
    when = due_at(date_str, window, rng=rng)

    image_url = None
    if image_path and str(image_path).strip():
        # Any ImageHostError propagates: a row that names an image the pipeline
        # cannot host must fail, not quietly post without it.
        image_url = upload(str(image_path).strip())
        logger.info("  image hosted at %s", image_url)

    post = create_post(channel_id, text, image_url, when,
                       key=key, session=session)
    return {
        "post_id": post.get("id"),
        "status": post.get("status"),
        "due_at": post.get("dueAt") or when,
        "image_url": image_url,
        "text": text,
    }


# ─── Phase 3: wait for Buffer to actually publish ────────────────────────────
#
# The clock is not the signal. Buffer publishes NEAR the due time, not on it -
# the spike measured sentAt 34 seconds after dueAt - so "it is past dueAt,
# therefore it is live" is wrong, and wrong in the direction that sends the
# browser tool to comment on a post that does not exist yet.
#
# externalLink is null until the post is sent, so it cannot be used as the
# readiness signal either. Status is.

Q_POST = """
query GetPost($id: PostId!) {
  post(input: { id: $id }) {
    id
    status
    dueAt
    sentAt
    externalLink
    text
    # assets is read here because a post's images are the thing most worth
    # checking after the fact, and omitting them made a read-back look like a
    # verification while measuring nothing.
    assets { id mimeType source type }
    error { message rawError }
  }
}
"""

# Statuses that will never become `sent` without a human doing something.
BLOCKED_STATUSES = ("draft", "needs_approval")
IN_FLIGHT_STATUSES = ("scheduled", "sending")


def get_post(post_id, key=None, session=None):
    """Read one post. Raises if Buffer does not return it."""
    body = gql(Q_POST, {"id": post_id}, key=key, label="getPost",
               session=session)
    post = (body.get("data") or {}).get("post")
    if not post:
        raise BufferError("Buffer returned no post for id %r" % post_id)
    return post


Q_POSTS_BY_STATUS = """
query PostsByStatus($org: OrganizationId!, $n: Int!, $st: [PostStatus!]) {
  posts(first: $n, input: { organizationId: $org, filter: { status: $st } }) {
    edges { node { id status dueAt sentAt externalLink channelId } }
  }
}
"""


def posts_by_status(channel_id, statuses=("sent",), limit=100, key=None,
                    session=None):
    """Every post in this channel's ORGANISATION with one of ``statuses``.

    ONE request, however many posts are being reconciled. Asking per post would
    mean a request each, and the free plan's budget is about a hundred a day in
    total - the reconcile would cost more than everything else combined.

    Returns ``{post_id: node}``.
    """
    channel = get_channel(channel_id, key=key, session=session)
    org = channel.get("organizationId")
    if not org:
        raise BufferError("channel %r has no organizationId" % channel_id)

    body = gql(Q_POSTS_BY_STATUS,
               {"org": org, "n": int(limit), "st": list(statuses)},
               key=key, label="postsByStatus", session=session)
    edges = (((body.get("data") or {}).get("posts") or {}).get("edges")) or []
    out = {}
    for edge in edges:
        node = (edge or {}).get("node") or {}
        if node.get("id"):
            out[node["id"]] = node
    return out


def wait_for_publish(post_id, timeout=1800, interval=20, key=None,
                     session=None, sleep_fn=None, now_fn=None,
                     link_grace=3):
    """Poll until the post is published, then return its LinkedIn permalink.

    ``link_grace`` extra polls are allowed after the status reaches ``sent`` but
    before ``externalLink`` appears, because the two are not written atomically
    and Phase 4 is useless without the URL.
    """
    import time as _time

    sleep_fn = sleep_fn or _time.sleep
    now_fn = now_fn or _time.monotonic

    deadline = now_fn() + timeout
    last_status = None
    grace_left = link_grace
    polls = 0

    logger.info("Waiting for Buffer post %s to publish (ceiling %ss)",
                post_id, timeout)
    while True:
        post = get_post(post_id, key=key, session=session)
        polls += 1
        status = post.get("status")

        if status != last_status:
            logger.info("  [%d] status=%s sentAt=%s externalLink=%s",
                        polls, status, post.get("sentAt"),
                        post.get("externalLink") or "-")
            last_status = status

        if status == "error":
            err = post.get("error") or {}
            raise BufferPublishFailed(
                "Buffer could not publish post %s: %s"
                % (post_id, err.get("message") or "no message from Buffer"))

        if status in BLOCKED_STATUSES:
            raise BufferPublishBlocked(
                "post %s is %r, which never becomes 'sent' on its own - it "
                "needs a human in Buffer." % (post_id, status))

        if status == "sent":
            link = (post.get("externalLink") or "").strip()
            if link:
                logger.info("  published: %s", link)
                return link
            # Sent, but the permalink has not landed yet.
            if grace_left <= 0:
                raise BufferError(
                    "post %s reached 'sent' but Buffer never returned an "
                    "externalLink, so there is no URL to comment on." % post_id)
            grace_left -= 1
            logger.info("  sent, waiting for externalLink (%d tries left)",
                        grace_left)

        elif status not in IN_FLIGHT_STATUSES:
            # An unfamiliar status is worth saying out loud rather than
            # silently treating as "still working".
            logger.warning("  unrecognised status %r - continuing to wait",
                           status)

        if now_fn() >= deadline:
            raise BufferPublishTimeout(
                "post %s was still %r after %ss (%d polls). It may still "
                "publish - this run simply stopped waiting, so the first "
                "comment has NOT been attempted."
                % (post_id, last_status, timeout, polls))
        sleep_fn(interval)
