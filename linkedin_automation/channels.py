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
from typing import Optional, Tuple

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

    def __post_init__(self):
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

#: Every declared channel, by id.
REGISTRY = {c.id: c for c in (LINKEDIN, X)}


def get(channel_id: str) -> Channel:
    """The declaration for ``channel_id``. Raises :class:`UnknownChannel`."""
    try:
        return REGISTRY[channel_id]
    except KeyError:
        raise UnknownChannel(
            "no channel declared for %r; known channels: %s"
            % (channel_id, ", ".join(sorted(REGISTRY)))) from None


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
