"""The engagement adapter boundary (PLATFORM_REFACTOR §9, D-027 E1).

One platform's engagement is an adapter: the platform-specific half of
"find a post, look at it, comment on it, like it". Everything else is shared
and stays outside the adapter: the comment generator, ``PostStore(platform=)``,
the E0 approval gate, the scheduler and auto cycle, profiles and the
production guard, the master switch, failure capture, the run log, the
dashboard.

An adapter provides, per platform:

* ``find``             - candidate posts, landing NEW in the store
* ``classify``         - is this post there: ``ok``, ``gone`` or ``unclear``
* ``already_commented``- asked of the thread itself, never of our own records
* ``post_and_verify``  - post, and say ``posted`` only on positive proof
* ``like``             - with the resulting state named, never inferred by
  negation; an adapter that cannot like says ``not_applicable``
* ``selectors``        - a **browser** adapter returns its selector_health
  registry entries (every selector a class constant, so the health check can
  watch it). An **API** adapter has no selectors: it returns ``None`` and must
  say why in ``SELECTORS_EXEMPT_REASON``.

The verdict types are the contract. ``classify`` may only say ``gone`` on a
positive signal (PRINCIPLES §5); everything ambiguous is ``unclear`` and is
retried. ``post_and_verify`` reports ``posted`` only when the comment was seen
(LinkedIn: in the thread; YouTube: the insert returned its id).

LinkedIn's implementation is ``linkedin_adapter.LinkedInAdapter``, a wrapper
over ``LinkedInCommentPoster`` and ``LinkedInAIPostFinder`` with no behaviour
of its own (E1 changes no LinkedIn decision; the golden in
``tests/test_e1_golden.py`` proves it). YouTube's is
``youtube_engage.YouTubeAdapter``.
"""

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from . import post_store

# ─── Verdicts ────────────────────────────────────────────────────────────────

#: classify outcomes. Only GONE is terminal, and only on a positive signal.
OK = "ok"
GONE = "gone"
UNCLEAR = "unclear"
CLASSIFY_OUTCOMES = (OK, GONE, UNCLEAR)

#: post_and_verify outcomes shared by every adapter. An adapter may name a
#: finer outcome (YouTube's posted_visible / posted_held) in ``outcome``;
#: ``posted`` is the shared yes/no, and is True only on positive proof.
POSTED = "posted"
FAILED = "failed"

#: like states.
LIKE_PLACED = "placed"                  # we clicked the un-liked control
LIKE_ALREADY = "already_liked"          # a known liked state was on the page
LIKE_MISS = "miss"                      # neither: unknown, never "liked"
LIKE_NOT_APPLICABLE = "not_applicable"  # the adapter cannot like at all
LIKE_STATES = (LIKE_PLACED, LIKE_ALREADY, LIKE_MISS, LIKE_NOT_APPLICABLE)

#: adapter kinds.
BROWSER = "browser"
API = "api"


@dataclass(frozen=True)
class Classification:
    """Is the post there. ``detail`` is the adapter's own finer reading
    (LinkedIn: ``timeout``; YouTube: ``comments_disabled``)."""
    outcome: str
    reason: Optional[str] = None
    detail: Optional[str] = None
    error: Any = None

    def __post_init__(self):
        if self.outcome not in CLASSIFY_OUTCOMES:
            raise ValueError("classify outcome must be one of %s, not %r"
                             % (CLASSIFY_OUTCOMES, self.outcome))

    @property
    def ok(self):
        return self.outcome == OK

    @property
    def gone(self):
        return self.outcome == GONE


@dataclass(frozen=True)
class ThreadCheck:
    """Did the thread itself show a comment of ours. ``evidence`` is what
    proved it (YouTube: our comment's id)."""
    already: bool
    reason: str
    evidence: Any = None


@dataclass(frozen=True)
class PostResult:
    """``posted`` is True only on positive proof of the comment existing."""
    posted: bool
    outcome: str
    proof: Any = None
    reason: Optional[str] = None
    ambiguous: bool = False
    quota: bool = False


@dataclass(frozen=True)
class LikeResult:
    """``state`` names what happened; ``proven`` says whether a page or API
    state confirmed it. ``liked`` is never True for a miss or n/a."""
    state: str
    proven: bool
    reason: Optional[str] = None
    evidence: Dict = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in LIKE_STATES:
            raise ValueError("like state must be one of %s, not %r"
                             % (LIKE_STATES, self.state))

    @property
    def liked(self):
        return self.state in (LIKE_PLACED, LIKE_ALREADY)


# ─── The interface ───────────────────────────────────────────────────────────

class EngagementAdapter(abc.ABC):
    """One platform's engagement. See the module docstring for the contract."""

    #: ``platforms.<NAME>``; also the store's ``platform``.
    platform: str = ""
    #: ``BROWSER`` or ``API``.
    kind: str = ""
    #: An API adapter's reason for having no selectors. Required when
    #: ``kind == API``; must stay None for a browser adapter.
    SELECTORS_EXEMPT_REASON: Optional[str] = None

    @abc.abstractmethod
    def find(self, **options):
        """Find candidate posts; they land NEW in the platform's store."""

    @abc.abstractmethod
    def classify(self, record) -> Classification:
        """Open/look up the post and say ok, gone or unclear."""

    @abc.abstractmethod
    def already_commented(self, record) -> ThreadCheck:
        """Ask the thread whether a comment of ours is already there."""

    @abc.abstractmethod
    def post_and_verify(self, record) -> PostResult:
        """Post ``record['comment']`` and verify it with positive proof."""

    @abc.abstractmethod
    def like(self, record) -> LikeResult:
        """Like the post, naming the resulting state."""

    @abc.abstractmethod
    def selectors(self) -> Optional[Dict[str, Dict]]:
        """A browser adapter's selector_health registry entries, or None for
        an API adapter (see ``SELECTORS_EXEMPT_REASON``)."""


def selector_contract_problems(adapter, registry) -> list:
    """What is wrong with ``adapter``'s selector declaration, as sentences.

    Empty means the contract holds: a browser adapter returns a non-empty set
    of entries, each registered in ``registry`` (selector_health's
    ``SELECTOR_REGISTRY``) exactly as declared; an API adapter returns None
    and says why it is exempt.
    """
    entries = adapter.selectors()
    if adapter.kind == API:
        problems = []
        if entries is not None:
            problems.append("an API adapter declares no selectors; got %r" % (entries,))
        if not adapter.SELECTORS_EXEMPT_REASON:
            problems.append("an API adapter must say why it has no selectors")
        return problems
    if adapter.kind != BROWSER:
        return ["unknown adapter kind %r" % (adapter.kind,)]
    if adapter.SELECTORS_EXEMPT_REASON:
        return ["a browser adapter is never exempt from selector_health"]
    if not entries:
        return ["a browser adapter must register its selectors"]
    problems = []
    for key, entry in entries.items():
        if key not in registry:
            problems.append("%s is not in the selector_health registry" % key)
        elif registry[key] != entry:
            problems.append("%s differs from its selector_health entry" % key)
    return problems


# ─── The shared side: the E0 approval check ──────────────────────────────────

def approval_refusal_for(record, text, *, strip=True) -> Optional[str]:
    """The E0 gate (DEC-062) for posting ``text`` from ``record``.

    ``None`` only when ``post_store.is_approved(record)`` and ``text`` is the
    record's approved text; otherwise the refusal reason. Call it on a FRESH
    read of the store, at the last step before posting. ``strip`` compares the
    texts with surrounding whitespace removed, which the LinkedIn TXT handoff
    needs (its parser strips); YouTube compares exactly.
    """
    reason = post_store.approval_refusal(record)
    if reason:
        return reason
    have = record.get("comment") or ""
    want = text or ""
    if strip:
        have, want = have.strip(), want.strip()
    if have != want:
        return post_store.REFUSE_TEXT_DIFFERS
    return None


# ─── The shared one-item flow ────────────────────────────────────────────────

#: engage_one outcomes.
ENGAGE_NOT_APPROVED = "not_approved"
ENGAGE_GONE = "gone"
ENGAGE_UNCLEAR = "unclear"
ENGAGE_ALREADY = "already_commented"
ENGAGE_POSTED = "posted"
ENGAGE_FAILED = "failed"


@dataclass(frozen=True)
class EngageResult:
    outcome: str
    reason: Optional[str] = None
    classification: Optional[Classification] = None
    thread: Optional[ThreadCheck] = None
    like: Optional[LikeResult] = None
    post: Optional[PostResult] = None


def engage_one(adapter, record, *, reload=None, like=True) -> EngageResult:
    """Classify, ask the thread, like, re-check approval, post and verify.

    The shared order every adapter is driven in. The approval gate runs first
    (nothing is opened for an unapproved draft) and again, on ``reload(key)``
    when given, as the last step before ``post_and_verify``, so an approval
    withdrawn meanwhile is honoured. A like miss never stops the comment.

    LinkedIn's batch path does not call this: ``LinkedInCommentPoster.run``
    keeps its own loop, which E1 leaves byte-for-byte as it was. This is the
    flow a new adapter (Instagram, E3) is driven by, and the tests drive the
    LinkedIn adapter through it against the same fake page as the poster.
    """
    text = record.get("comment")
    reason = approval_refusal_for(record, text)
    if reason:
        return EngageResult(ENGAGE_NOT_APPROVED, reason)
    cls = adapter.classify(record)
    if cls.gone:
        return EngageResult(ENGAGE_GONE, cls.reason, classification=cls)
    if not cls.ok:
        return EngageResult(ENGAGE_UNCLEAR, cls.reason, classification=cls)
    thread = adapter.already_commented(record)
    if thread.already:
        return EngageResult(ENGAGE_ALREADY, thread.reason, classification=cls,
                            thread=thread)
    liked = adapter.like(record) if like else None
    if reload is not None:
        fresh = reload(record.get("key") or record.get("url"))
        reason = post_store.REFUSE_NO_RECORD if fresh is None \
            else approval_refusal_for(fresh, text)
        if reason:
            return EngageResult(ENGAGE_NOT_APPROVED, reason, classification=cls,
                                thread=thread, like=liked)
    result = adapter.post_and_verify(record)
    return EngageResult(ENGAGE_POSTED if result.posted else ENGAGE_FAILED,
                        result.reason, classification=cls, thread=thread,
                        like=liked, post=result)
