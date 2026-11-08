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

#: Every platform id, in a fixed order.
ALL = (LINKEDIN, X)

#: The platform a call means when it names none. LinkedIn, for history: its
#: settings are the flat ``scheduled_posting`` fields and its state file is the
#: original path, both of which predate any second platform.
DEFAULT = LINKEDIN
