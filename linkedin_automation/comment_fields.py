"""Canonical comment-field normalization and the TXT serialization contract.

The comment generator emits ``post_url`` / ``post_author`` / ``post_text`` /
``post_category``, while dashboard-edited comments may use ``url`` / ``author`` /
``post_preview`` / ``post_type``. Consuming code that mixed these conventions
caused the poster to see ``URL: N/A`` for every comment and find zero comments.

This module is the single source of truth for:
  * mapping both naming conventions to one canonical comment shape, and
  * the plain-text block format that ``post_linkedin_comments.parse_comments_file``
    reads back. Keeping the writer and the parser agreeing on this format lives
    here so they cannot drift apart.
"""

from typing import Dict, List

# Canonical keys produced by normalize_comment_fields().
CANONICAL_KEYS = (
    "url", "author", "post_text", "category",
    "comment", "word_count", "style", "approach",
)

# Block separator used in the TXT file (must stay in sync with the parser).
SEPARATOR = "-" * 60
PREVIEW_MAX = 200


def _first(comment: Dict, *names: str):
    """Return the first present, non-empty value among the given keys."""
    for name in names:
        value = comment.get(name)
        if value not in (None, ""):
            return value
    return None


def normalize_comment_fields(comment: Dict) -> Dict:
    """Map a comment dict (either naming convention) to the canonical shape.

    Accepts generator-style keys (``post_url``, ``post_author``, ``post_text``,
    ``post_category``), dashboard-style keys (``url``, ``author``,
    ``post_preview``/``preview``, ``post_type``), and the post finder's
    ``author_name``. ``word_count`` is derived from the comment text when absent.
    Never raises on missing fields.
    """
    comment = comment or {}

    comment_text = _first(comment, "comment") or ""
    word_count = _first(comment, "word_count")
    if not word_count:
        word_count = len(comment_text.split())

    return {
        "url": _first(comment, "post_url", "url") or "",
        "author": _first(comment, "post_author", "author", "author_name") or "Unknown",
        "post_text": _first(comment, "post_text", "post_preview", "preview") or "",
        "category": _first(comment, "post_category", "post_type", "category") or "",
        "comment": comment_text,
        "word_count": word_count,
        "style": _first(comment, "style") or "",
        "approach": _first(comment, "approach") or "",
    }


def format_comment_block(comment: Dict) -> str:
    """Render one comment as a TXT block matching the poster's parser format."""
    c = normalize_comment_fields(comment)
    preview = c["post_text"][:PREVIEW_MAX]
    return (
        f'Post: {c["author"]} - {c["category"]}\n'
        f'URL: {c["url"]}\n'
        f'Post Preview:\n{preview}\n\n'
        f'Your Comment ({c["word_count"]} words):\n"{c["comment"]}"\n'
        f'\n{SEPARATOR}\n\n'
    )


def comments_to_txt(comments: List[Dict], header_timestamp: str) -> str:
    """Build the full curated-comments TXT file body from a list of comments."""
    header = (
        f"LinkedIn Comments - Curated {header_timestamp}\n"
        f"Total: {len(comments)}\n"
        + "=" * 60 + "\n\n"
    )
    return header + "".join(format_comment_block(c) for c in comments)
