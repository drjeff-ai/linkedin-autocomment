"""Tests for comment field normalization and the TXT write/parse contract
(ROADMAP Phase 1). Eliminates the post_url vs url mismatch that made the poster
find zero comments."""

from linkedin_automation import comment_fields as cf
from linkedin_automation.comment_poster import LinkedInCommentPoster

URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


def _generator_comment(i):
    """A comment as the generator emits it (post_* keys)."""
    return {
        "post_url": URL.format(i),
        "post_author": f"Author {i}",
        "post_text": f"Some interesting post body number {i}.",
        "post_category": "AI",
        "comment": f"Great point number {i} here.",
        "word_count": 5,
        "style": "thoughtful",
        "approach": "agree-and-add",
    }


def _dashboard_comment(i):
    """A comment as the dashboard frontend may emit it (short keys)."""
    return {
        "url": URL.format(i),
        "author": f"Author {i}",
        "post_preview": f"Some interesting post body number {i}.",
        "post_type": "AI",
        "comment": f"Great point number {i} here.",
    }


# ─── normalize_comment_fields ─────────────────────────────────────────────────

def test_normalize_generator_keys():
    n = cf.normalize_comment_fields(_generator_comment(1))
    assert n["url"] == URL.format(1)
    assert n["author"] == "Author 1"
    assert n["post_text"].startswith("Some interesting")
    assert n["category"] == "AI"
    assert n["comment"] == "Great point number 1 here."
    assert n["word_count"] == 5


def test_normalize_dashboard_keys():
    n = cf.normalize_comment_fields(_dashboard_comment(1))
    assert n["url"] == URL.format(1)
    assert n["author"] == "Author 1"
    assert n["post_text"].startswith("Some interesting")
    assert n["category"] == "AI"
    assert n["comment"] == "Great point number 1 here."


def test_normalize_word_count_derived_when_missing():
    n = cf.normalize_comment_fields({"comment": "one two three four"})
    assert n["word_count"] == 4


def test_normalize_word_count_preserved_when_present():
    n = cf.normalize_comment_fields({"comment": "one two", "word_count": 99})
    assert n["word_count"] == 99


def test_normalize_missing_fields_defaults_no_raise():
    n = cf.normalize_comment_fields({})
    assert n["url"] == ""
    assert n["author"] == "Unknown"
    assert n["comment"] == ""
    assert n["word_count"] == 0
    assert set(n.keys()) == set(cf.CANONICAL_KEYS)


def test_normalize_none_input():
    n = cf.normalize_comment_fields(None)
    assert n["author"] == "Unknown"


def test_generator_key_wins_when_both_present():
    n = cf.normalize_comment_fields({"post_url": "A", "url": "B"})
    assert n["url"] == "A"


def test_falls_back_when_preferred_key_empty():
    n = cf.normalize_comment_fields({"post_url": "", "url": "B"})
    assert n["url"] == "B"


def test_author_name_maps_to_author():
    # The post finder emits author_name; normalization maps it to canonical author.
    n = cf.normalize_comment_fields({"author_name": "Morgan Diaz", "comment": "hi"})
    assert n["author"] == "Morgan Diaz"


def test_post_author_wins_over_author_name():
    n = cf.normalize_comment_fields({"post_author": "A", "author_name": "B"})
    assert n["author"] == "A"


# ─── Write → parse round-trip (the Phase 1 validation gate) ──────────────────

def _roundtrip_count(tmp_path, comments):
    txt = cf.comments_to_txt(comments, "2026-06-24 12:00")
    path = tmp_path / "daily_comments_curated.txt"
    path.write_text(txt, encoding="utf-8")
    return LinkedInCommentPoster.parse_comments_file(str(path))


def test_roundtrip_generator_fields_finds_all_five(tmp_path):
    comments = [_generator_comment(i) for i in range(1, 6)]
    parsed = _roundtrip_count(tmp_path, comments)
    assert len(parsed) == 5
    assert [p["url"] for p in parsed] == [URL.format(i) for i in range(1, 6)]


def test_roundtrip_dashboard_fields_finds_all_five(tmp_path):
    comments = [_dashboard_comment(i) for i in range(1, 6)]
    parsed = _roundtrip_count(tmp_path, comments)
    assert len(parsed) == 5
    assert [p["url"] for p in parsed] == [URL.format(i) for i in range(1, 6)]


def test_roundtrip_preserves_comment_text(tmp_path):
    comments = [_generator_comment(1)]
    parsed = _roundtrip_count(tmp_path, comments)
    assert parsed[0]["comment"] == "Great point number 1 here."


def test_roundtrip_unicode_emoji(tmp_path):
    c = _generator_comment(1)
    c["comment"] = "Love this — 100% agree 🔥 résumé café"
    parsed = _roundtrip_count(tmp_path, [c])
    assert len(parsed) == 1
    assert parsed[0]["comment"] == "Love this — 100% agree 🔥 résumé café"


def test_no_comment_dropped_for_missing_optional_fields(tmp_path):
    # Only url + comment present; author/category/preview absent.
    comments = [{"url": URL.format(i), "comment": f"comment {i}"} for i in range(1, 6)]
    parsed = _roundtrip_count(tmp_path, comments)
    assert len(parsed) == 5


def test_serialized_txt_has_no_na_urls(tmp_path):
    """Regression: the original bug wrote 'URL: N/A' for every comment."""
    txt = cf.comments_to_txt([_generator_comment(1)], "2026-06-24 12:00")
    assert "URL: N/A" not in txt
    assert f"URL: {URL.format(1)}" in txt
