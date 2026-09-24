"""One grammar for LinkedIn post URNs, shared by the scraper and the poster.

Before this, three regexes parsed post URNs and disagreed about which types
exist (AUDIT_15A, BLOCKED 15.3): the poster knew only ``activity``, the
scraper knew ``activity``/``ugcPost``/``share``, and nobody knew
``groupPost``. So a group post reached the poster with no identity at all.

This module owns the GRAMMAR and nothing else. Which types a caller accepts,
which URL forms it reads, and how short an id it will believe are policy, and
they stay with the caller as arguments. The scraper keeps exactly its old
three types; the poster accepts all four.

Two forms occur in LinkedIn post URLs:

    urn    urn:li:<type>:<id>   /feed/update/urn:li:activity:1010101010/
    slug   <type>-<id>          /posts/some-slug-activity-1010101010-AbCd/

Ids are digits, except ``groupPost``: two numbers, the group then the post,
joined by a hyphen (``urn:li:groupPost:1010101-1010101010``, seen in the live
store). A type-qualified id (``activity:1010``, ``groupPost:1010101-1010``)
keeps the same digits under two types distinct.
"""

import re
from functools import lru_cache
from typing import Iterable, NamedTuple, Optional

URN_FORM = "urn"
SLUG_FORM = "slug"
ALL_FORMS = (URN_FORM, SLUG_FORM)

#: Every post URN type the grammar knows. A caller passes the subset it accepts.
KNOWN_TYPES = ("activity", "ugcPost", "share", "groupPost")

#: Id shape per type. Anything not listed is plain digits.
_COMPOUND_ID_TYPES = ("groupPost",)


class PostUrn(NamedTuple):
    type: str
    id: str
    form: str

    @property
    def qualified(self) -> str:
        """``<type>:<id>``, the identity the poster keys on."""
        return "%s:%s" % (self.type, self.id)

    @property
    def urn(self) -> str:
        """``urn:li:<type>:<id>``, the shape the scraper records."""
        return "urn:li:%s:%s" % (self.type, self.id)


@lru_cache(maxsize=None)
def _pattern(types: tuple) -> "re.Pattern":
    alternation = "|".join(re.escape(t) for t in types)
    # No left boundary, deliberately: the regexes this replaces had none, and
    # a slug form sits directly after "-" in /posts/<slug>-activity-<id>.
    return re.compile(
        r"(?P<prefix>urn:li:)?(?P<type>%s)(?P<sep>[:\-])(?P<id>\d+(?:-\d+)?)"
        % alternation)


def _shape(type_: str, raw_id: str) -> Optional[str]:
    """The id as this type spells it, or None if the capture does not fit."""
    if type_ in _COMPOUND_ID_TYPES:
        return raw_id if "-" in raw_id else None
    return raw_id.split("-", 1)[0]


def find_post_urn(text: str, types: Iterable[str], forms=ALL_FORMS,
                  min_digits: int = 1) -> Optional[PostUrn]:
    """The FIRST post URN of an accepted type and form in ``text``, or None.

    ``types``: the URN types the caller accepts (a subset of KNOWN_TYPES).
    ``forms``: which of URN_FORM / SLUG_FORM the caller reads.
    ``min_digits``: shortest POST number the caller will believe (for a
    groupPost, the part after the hyphen; the group number is not held to it).

    First means leftmost, as ``re.search`` over the old per-site regex gave.
    """
    return next(iter_post_urns(text, types, forms, min_digits), None)


def iter_post_urns(text: str, types: Iterable[str], forms=ALL_FORMS,
                   min_digits: int = 1):
    """Every post URN of an accepted type and form in ``text``, leftmost first.

    Same grammar and arguments as :func:`find_post_urn`.
    """
    types = tuple(types)
    unknown = set(types) - set(KNOWN_TYPES)
    if unknown:
        raise ValueError("unknown post URN type(s): %s" % sorted(unknown))
    if not text or not types:
        return
    for m in _pattern(types).finditer(text):
        if m.group("sep") == ":":
            if not m.group("prefix"):
                continue           # "activity:123" outside a urn:li: URN
            form = URN_FORM
        else:
            form = SLUG_FORM
        if form not in forms:
            continue
        ident = _shape(m.group("type"), m.group("id"))
        if ident is None:
            continue
        # The POST number is what min_digits guards. A groupPost's leading
        # group number may legitimately be short (older groups).
        if len(ident.rsplit("-", 1)[-1]) < min_digits:
            continue
        yield PostUrn(m.group("type"), ident, form)
