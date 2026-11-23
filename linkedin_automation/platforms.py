"""Every platform id, defined once (.dev/PLATFORM_REFACTOR.md §3).

``platform_policy``, ``post_store``, ``csv_pipeline`` and ``profile_manager``
each used to spell these themselves, and only one pair was tested for
agreement. A store addressed as "x" and a policy addressed as "twitter" would
diverge silently, so now there is one spelling and everything imports it.

Nothing here says what a platform DOES. Publishing behaviour lives in
``channels.py`` (one declaration per Buffer channel); the comment generator's
voice lives in ``platform_policy.py``.
"""

LINKEDIN = "linkedin"
X = "x"
PINTEREST = "pinterest"

#: YouTube Shorts (V1, DEC-058). Published through the YouTube Data API, not
#: Buffer, so it is not in ``ALL``: ``ALL`` is the Buffer channels plus the
#: engagement stores, and every reader of it means those.
YOUTUBE = "youtube"

#: Instagram engagement (D-027 E2/E3): comments and likes in a browser, on
#: other people's posts. No publishing channel, so not in ``ALL`` either.
INSTAGRAM = "instagram"

#: Every Buffer and engagement platform id, in a fixed order.
ALL = (LINKEDIN, X, PINTEREST)

#: The platform a call means when it names none. LinkedIn, for history: its
#: settings are the flat ``scheduled_posting`` fields and its state file is the
#: original path, both of which predate any second platform.
DEFAULT = LINKEDIN
