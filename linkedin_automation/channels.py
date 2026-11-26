"""The publishing channel registry (.dev/PLATFORM_REFACTOR.md §4, §13).

A channel is one declaration. Everything about how publishing differs between
channels is a field here, and the shared publishing code reads the field instead
of asking which channel it is. A new ``if platform == ...`` anywhere else is a
defect, and ``tests/test_channel_registry.py`` fails on one by parsing the code.

**Only fields something reads are declared (§13.4).** The end-state table in §4
also has ``media``, ``extra_columns``, ``lookups``, ``build_metadata`` and
``drain``. Each arrives in the phase that first reads it: a field nothing reads
is a field that gets tuned and believed while doing nothing.

**An undeclared name gets the old else-branch, not a refusal.** Before this
registry every site was ``if platform == linkedin: A else: B``, so a name that
was neither LinkedIn nor X got B. :func:`resolve` keeps exactly that for P1,
which is a no-behaviour-change phase. Callers that already refuse an unknown
name (``_req_platform``, ``resolve_scheduled`` without an overlay) still refuse
it first.

This module is separate from ``platform_policy``, which is the comment
generator's registry. Publishing never depends on a voice it does not use, and
the generator never has to declare a channel it only publishes to.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from . import platform_policy
from . import platforms

# ── link_placement: where a row's link goes ─────────────────────────────────
#: Not in the post. The browser posts it as the first comment once the post is
#: live (LinkedIn: Buffer's own firstComment is paywalled and rejects the post).
FIRST_COMMENT = "first_comment"
#: Appended to the post body, after a blank line.
BODY = "body"
#: Nowhere. Only an undeclared name gets this.
NO_LINK = "none"
#: In the channel's Buffer metadata, never the body (Pinterest: the pin's
#: destination link is ``metadata.pinterest.url``). The channel's
#: ``metadata_builder`` places it.
METADATA = "metadata"
#: Nowhere, and a row that carries one is refused at queue time (Instagram: a
#: caption link is not clickable, and dropping it silently would lose the
#: thing the row exists to share).
REFUSED = "refused"

# ── identity_check: what is verified before a Buffer post is created ────────
#: Nothing at schedule time. The account is confirmed by the browser, against
#: the logged-in profile URL, before the first comment is placed.
UNGATED = "ungated"
#: The Buffer channel's own handle, checked against the configured
#: identity_slug before createPost, behind the declared-production gate.
BUFFER_HANDLE = "buffer_handle"

# ── config_layout: where a profile's settings for this channel live ─────────
#: The flat ``scheduled_posting`` fields (they predate any second channel).
FLAT = "flat"
#: ``scheduled_posting.platforms.<id>``.
OVERLAY = "overlay"

# ── state_path: where the per-row pipeline state file lives ─────────────────
#: ``data/<profile>/scheduled_posts_state.json``, the original path.
LEGACY = "legacy"
#: ``data/<profile>/<id>/scheduled_posts_state.json``.
PLATFORM_DIR = "platform_dir"

#: For a channel with no Buffer side: the account is proven when the OAuth
#: token is minted, by the channel id the token answers for, and that id is
#: checked against the configured one before every upload (``youtube_channel``).
OAUTH_CHANNEL = "oauth_channel"

# ── publisher: what sends this channel's posts ──────────────────────────────
#: Buffer, through ``csv_pipeline.schedule_pass``. Every channel before V1.
BUFFER = "buffer"
#: The YouTube Data API, through ``youtube_channel`` (V1, DEC-058).
YOUTUBE_API = "youtube_api"

#: The one Buffer key variable a channel with a fixed key reads.
FIXED_BUFFER_KEY_ENV = "BUFFER_API_KEY"

#: How a text limit's unit is named in a validation message.
_UNIT_WORDS = {"chars": "characters"}


class UnknownChannel(ValueError):
    """A name with no channel declaration, asked for by :func:`get`."""


@dataclass(frozen=True)
class TextLimit:
    """A body limit the channel itself enforces. Over it, the post cannot go out.

    ``url_cost`` is what one link counts as, whatever its real length (X wraps
    every link in a fixed-width t.co shortlink). ``None`` counts links as text.
    """

    unit: str
    maximum: int
    url_cost: Optional[int] = None

    @property
    def unit_word(self) -> str:
        return _UNIT_WORDS.get(self.unit, self.unit)


@dataclass(frozen=True)
class ExtraColumn:
    """An optional CSV column one channel reads beyond the base seven.

    ``required`` means a row for this channel must carry a non-empty value.
    ``limit`` is a character count the stripped value may not exceed; a longer
    one is rejected at queue time, never trimmed.
    """

    name: str
    required: bool = False
    limit: Optional[int] = None


@dataclass(frozen=True)
class Channel:
    """Everything shared publishing code needs to know about one channel."""

    id: str
    #: The name used in messages ("too long for X").
    label: str
    link_placement: str
    #: None means the channel enforces no body limit that this code checks.
    text_limit: Optional[TextLimit]
    identity_check: str
    config_layout: str
    #: The Buffer key variable, when it is fixed. None means each profile's
    #: config names its own (``buffer_api_key_env``), within
    #: ``profile_manager.BUFFER_KEY_ENV_RULE``.
    key_env: Optional[str]
    state_path: str
    #: The Buffer post statuses ``reconcile_published`` asks about.
    reconcile_statuses: Tuple[str, ...]
    #: Optional CSV columns this channel reads, validated for it alone and
    #: hashed into the row's identity when filled. Last, with a default, so a
    #: declaration that predates it still constructs.
    extra_columns: Tuple[ExtraColumn, ...] = ()
    #: ``(row, board_service_id) -> metadata or None``: the Buffer ``metadata``
    #: this channel's createPost carries. None sends no ``metadata`` key.
    metadata_builder: Optional[Callable[[dict, Optional[str]], Optional[dict]]] = None
    #: The CSV column whose stripped value is the image's alt text.
    alt_text_column: Optional[str] = None
    #: A row with no image is refused (every pin is an image).
    image_required: bool = False
    #: The row names a board that is resolved to Buffer's board serviceId
    #: before anything is sent.
    board_lookup: bool = False
    #: What sends this channel's posts (:data:`BUFFER` or :data:`YOUTUBE_API`).
    #: Only a Buffer channel is in :data:`REGISTRY`, which every Buffer path
    #: (queue, drain, Schedule, the panel's pills) iterates. Last, with a
    #: default, so every declaration before it is unchanged (DEC-059).
    publisher: str = BUFFER
    #: The CSV column holding a local video path. A row with one is a video
    #: post, hosted with ``image_host.upload_video``. None: no video posts.
    #: The fields from here on are I1's (D-030), last with defaults, so every
    #: declaration before them is unchanged.
    video_column: Optional[str] = None
    #: The largest video file accepted, in bytes. Over it is refused at queue
    #: time, never re-encoded.
    video_max_bytes: Optional[int] = None
    #: A row with neither an image nor a video is refused.
    media_required: bool = False
    #: The most hashtags the composed body may carry. Over it is refused at
    #: queue time, never trimmed. None: not counted.
    max_hashtags: Optional[int] = None
    #: ``(profile_name) -> refusal or None``, run once per schedule pass
    #: before the identity gate. A refusal stops the pass with nothing sent.
    preflight: Optional[Callable[[Optional[str]], Optional[str]]] = None

    def __post_init__(self):
        names = [column.name for column in self.extra_columns]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError("%s: extra column declared twice: %s"
                             % (self.id, ", ".join(duplicates)))
        # The fields are not independent, and a declaration that breaks one of
        # these would fail somewhere far from here: a flat channel reads
        # env.get(key_env), so it needs a fixed one, and an overlay channel
        # takes its key variable from each profile's overlay, so it must not
        # declare one that nothing would read.
        if self.config_layout == FLAT and not self.key_env:
            raise ValueError("%s: a flat channel needs a fixed key_env" % self.id)
        if self.config_layout == OVERLAY and self.key_env is not None:
            raise ValueError("%s: an overlay channel names its key per profile; "
                             "key_env must be None" % self.id)

    @property
    def key_env_editable(self) -> bool:
        return self.key_env is None


LINKEDIN = Channel(
    id=platforms.LINKEDIN,
    label="LinkedIn",
    link_placement=FIRST_COMMENT,
    # LinkedIn's 70-word "limit" is a style heuristic in platform_policy
    # (hard_max_is_postable_limit=False), not a rule Buffer enforces.
    text_limit=None,
    identity_check=UNGATED,
    config_layout=FLAT,
    key_env=FIXED_BUFFER_KEY_ENV,
    state_path=LEGACY,
    reconcile_statuses=("sent",),
)

X = Channel(
    id=platforms.X,
    label="X",
    link_placement=BODY,
    # ONE number for the postable ceiling: the generator refuses to return a
    # draft over it, and this refuses to schedule a post over it. Two copies
    # could disagree, and the gap between them is a post that cannot go out.
    text_limit=TextLimit(
        unit=platform_policy.CHARS,
        maximum=platform_policy.policy_for(platforms.X).length.hard_max,
        url_cost=23),
    identity_check=BUFFER_HANDLE,
    config_layout=OVERLAY,
    key_env=None,
    state_path=PLATFORM_DIR,
    # Create-success is not publish-success on X: a post Buffer accepted can
    # still fail at publish time, so reconcile asks about errors too.
    reconcile_statuses=("sent", "error"),
)


class MissingBoard(ValueError):
    """A Pinterest createPost was about to be built with no board serviceId."""


def _pinterest_metadata(row, board_service_id):
    """``{"pinterest": {boardServiceId, title, url}}`` for one row.

    The shape pin.publisher sends (``build_pinterest_metadata``). The board is
    required: Buffer's schema types it optional but refuses a create without
    it, so a blank one is refused here, before anything is uploaded. ``title``
    and ``url`` go in only when filled. The pin's description is the post's
    top-level ``text``, never this. The link is read under both of its names,
    ``first_comment_link`` first, the way ``csv_pipeline.row_link`` reads it
    (that module imports this one, so it cannot be called from here).
    """
    sid = (board_service_id or "").strip()
    if not sid:
        raise MissingBoard(
            "Pinterest needs the board's serviceId to create a pin, and none "
            "was resolved for board %r. Nothing was sent."
            % str(row.get("board") or "").strip())
    pin = {"boardServiceId": sid}
    title = str(row.get("title") or "").strip()
    link = (str(row.get("first_comment_link") or "").strip()
            or str(row.get("link") or "").strip())
    if title:
        pin["title"] = title
    if link:
        pin["url"] = link
    return {"pinterest": pin}


PINTEREST = Channel(
    id=platforms.PINTEREST,
    label="Pinterest",
    # The pin's destination link is metadata.pinterest.url, never the body.
    link_placement=METADATA,
    # Pinterest caps a pin description at 500 characters. A link is not in
    # the body, so links count as plain text (url_cost None) if one is typed.
    text_limit=TextLimit(unit=platform_policy.CHARS, maximum=500, url_cost=None),
    identity_check=BUFFER_HANDLE,
    config_layout=OVERLAY,
    key_env=None,
    state_path=PLATFORM_DIR,
    reconcile_statuses=("sent", "error"),
    extra_columns=(ExtraColumn("title", required=True, limit=100),
                   ExtraColumn("alt_text", limit=500),
                   ExtraColumn("board", required=True)),
    metadata_builder=_pinterest_metadata,
    alt_text_column="alt_text",
    image_required=True,
    board_lookup=True,
)

YOUTUBE = Channel(
    id=platforms.YOUTUBE,
    label="YouTube",
    # A Short has no link field the uploader fills; the description is verbatim.
    link_placement=NO_LINK,
    text_limit=None,
    identity_check=OAUTH_CHANNEL,
    # scheduled_posting.platforms.youtube holds the channel's settings.
    config_layout=OVERLAY,
    key_env=None,
    # data/<profile>/youtube/, where youtube_channel keeps its own files.
    state_path=PLATFORM_DIR,
    # Nothing to reconcile against Buffer.
    reconcile_statuses=(),
    publisher=YOUTUBE_API,
)

#: Instagram's caption limit and hashtag cap (DEC-071(2)). Refused, never trimmed.
INSTAGRAM_CAPTION_MAX = 2200
INSTAGRAM_MAX_HASHTAGS = 30
#: Buffer's help centre, "Sharing videos through Buffer", Instagram Reels:
#: "File size: Below 300MB". Decimal megabytes, the smaller reading.
INSTAGRAM_VIDEO_MAX_BYTES = 300 * 1000 * 1000
INSTAGRAM_VIDEO_COLUMN = "video_path"


def _instagram_metadata(row, board_service_id):
    """``{"instagram": {type, shouldShareToFeed}}`` for one row.

    From Buffer's ``InstagramPostMetadataInput``: ``type: PostType!`` ("eg,
    post, story, reel for Instagram") and ``shouldShareToFeed: Boolean!``
    ("Indicates whether post should be shared to feed"); both are required.
    A row with a video is a ``reel``, any other row a ``post`` (an image).
    Every Reel and image is shared to the feed. ``firstComment``, ``link``,
    ``collaborators``, ``geolocation``, ``isAiGenerated``, ``stickerFields`` and
    ``trialReelGraduationStrategy`` are left out: nothing here needs them.
    ``board_service_id`` is unused (no boards on Instagram).
    """
    video = str(row.get(INSTAGRAM_VIDEO_COLUMN) or "").strip()
    return {"instagram": {"type": "reel" if video else "post",
                          "shouldShareToFeed": True}}


def _instagram_preflight(profile_name):
    # Imported here: instagram_channel reads youtube_channel, which imports
    # this module.
    from . import instagram_channel
    return instagram_channel.preflight_refusal(profile_name)


INSTAGRAM = Channel(
    id=platforms.INSTAGRAM,
    label="Instagram",
    link_placement=REFUSED,
    # Links in the caption count as plain text.
    text_limit=TextLimit(unit=platform_policy.CHARS,
                         maximum=INSTAGRAM_CAPTION_MAX, url_cost=None),
    identity_check=BUFFER_HANDLE,
    config_layout=OVERLAY,
    key_env=None,
    state_path=PLATFORM_DIR,
    reconcile_statuses=("sent", "error"),
    extra_columns=(ExtraColumn(INSTAGRAM_VIDEO_COLUMN),),
    metadata_builder=_instagram_metadata,
    video_column=INSTAGRAM_VIDEO_COLUMN,
    video_max_bytes=INSTAGRAM_VIDEO_MAX_BYTES,
    media_required=True,
    max_hashtags=INSTAGRAM_MAX_HASHTAGS,
    preflight=_instagram_preflight,
)

#: Every declared channel, by id, in declaration order.
CHANNELS = {c.id: c for c in (LINKEDIN, X, PINTEREST, YOUTUBE, INSTAGRAM)}

#: The channels Buffer publishes, by id. Every Buffer path iterates this, so
#: a channel with another publisher never reaches a Buffer call.
REGISTRY = {cid: c for cid, c in CHANNELS.items() if c.publisher == BUFFER}


def get(channel_id: str) -> Channel:
    """The declaration for ``channel_id``, whatever its publisher. Raises
    :class:`UnknownChannel`."""
    try:
        return CHANNELS[channel_id]
    except KeyError:
        raise UnknownChannel(
            "no channel declared for %r; known channels: %s"
            % (channel_id, ", ".join(sorted(CHANNELS)))) from None


def undeclared(channel_id: str) -> Channel:
    """What a name with no declaration gets: the old non-LinkedIn branch of
    every site, field by field, so routing through the registry changes nothing.
    """
    return Channel(
        id=channel_id,
        label=channel_id,
        link_placement=NO_LINK,
        text_limit=None,
        identity_check=BUFFER_HANDLE,
        config_layout=OVERLAY,
        key_env=None,
        state_path=PLATFORM_DIR,
        reconcile_statuses=("sent", "error"),
    )


def resolve(channel_id) -> Channel:
    """The declaration for ``channel_id``, or :func:`undeclared` for it.

    No normalisation here: each caller keeps the casing and stripping it always
    applied, so its answer for any input is what it was before the registry.
    """
    channel = REGISTRY.get(channel_id) if isinstance(channel_id, str) else None
    return channel if channel is not None else undeclared(channel_id)


def flat_config_ids():
    """Ids whose settings live in the flat ``scheduled_posting`` fields."""
    return [c.id for c in REGISTRY.values() if c.config_layout == FLAT]
