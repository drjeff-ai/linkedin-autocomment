"""Edge-case tests for the poster's parse_comments_file (ROADMAP Phase 5).

Covers: empty file, single comment, multiple, special chars, internal quotes,
double-wrapped quotes, missing/invalid URL, missing comment block, and
unicode/emoji. parse_comments_file is a staticmethod, so no browser is needed."""

from linkedin_automation import comment_fields as cf
from linkedin_automation.comment_poster import LinkedInCommentPoster

URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"
SEP = "-" * 60
parse = LinkedInCommentPoster.parse_comments_file


def _write(tmp_path, text):
    p = tmp_path / "comments.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _block(url, comment, words=5, author="A", category="AI", preview="Body"):
    return (
        f"Post: {author} - {category}\n"
        f"URL: {url}\n"
        f"Post Preview:\n{preview}\n\n"
        f'Your Comment ({words} words):\n"{comment}"\n'
        f"\n{SEP}\n\n"
    )


# ─── Empty / trivial ──────────────────────────────────────────────────────────

def test_empty_file(tmp_path):
    assert parse(_write(tmp_path, "")) == []


def test_whitespace_only_file(tmp_path):
    assert parse(_write(tmp_path, "   \n\n  ")) == []


def test_header_only_no_comments(tmp_path):
    text = "LinkedIn Comments - Curated 2026-06-25\nTotal: 0\n" + "=" * 60 + "\n\n"
    assert parse(_write(tmp_path, text)) == []


# ─── Counts ───────────────────────────────────────────────────────────────────

def test_single_comment(tmp_path):
    text = _block(URL.format(1), "Nice work here")
    result = parse(_write(tmp_path, text))
    assert len(result) == 1
    assert result[0]["url"] == URL.format(1)
    assert result[0]["comment"] == "Nice work here"


def test_multiple_comments(tmp_path):
    text = "".join(_block(URL.format(i), f"comment {i}") for i in range(1, 6))
    result = parse(_write(tmp_path, text))
    assert len(result) == 5
    assert [r["url"] for r in result] == [URL.format(i) for i in range(1, 6)]


def test_roundtrip_via_comments_to_txt(make_comment, tmp_path):
    comments = [make_comment(i) for i in range(1, 4)]
    text = cf.comments_to_txt(comments, "2026-06-25 10:00")
    result = parse(_write(tmp_path, text))
    assert len(result) == 3


# ─── Special characters / quotes / unicode ────────────────────────────────────

def test_special_chars_in_comment(tmp_path):
    secret = "Loved the +1 & the @mention — 100%!"
    text = _block(URL.format(1), secret)
    result = parse(_write(tmp_path, text))
    assert result[0]["comment"] == secret


def test_internal_double_quotes_preserved(tmp_path):
    # Internal quotes mid-text survive. (A comment that *ends* in a literal quote
    # is inherently ambiguous against the wrapper and is not supported.)
    comment = 'She said "hello" to the whole team today'
    text = _block(URL.format(1), comment)
    result = parse(_write(tmp_path, text))
    assert len(result) == 1
    assert result[0]["comment"] == comment


def test_double_wrapped_quotes(tmp_path):
    # Some upstreams wrap the comment in doubled quotes: ""text""
    block = (
        f"URL: {URL.format(1)}\n"
        f'Your Comment (2 words):\n""double wrapped""\n'
        f"\n{SEP}\n\n"
    )
    result = parse(_write(tmp_path, block))
    assert len(result) == 1
    assert result[0]["comment"] == "double wrapped"


def test_unicode_emoji_comment(tmp_path):
    comment = "Love this — 100% 🔥 résumé café déjà vu"
    text = _block(URL.format(1), comment)
    result = parse(_write(tmp_path, text))
    assert result[0]["comment"] == comment


# ─── Missing / invalid fields ─────────────────────────────────────────────────

def test_block_missing_url_is_skipped(tmp_path):
    good = _block(URL.format(1), "good one")
    bad = (
        "Post: B - AI\nPost Preview:\nbody\n\n"
        f'Your Comment (2 words):\n"no url here"\n\n{SEP}\n\n'
    )
    result = parse(_write(tmp_path, good + bad))
    assert len(result) == 1
    assert result[0]["url"] == URL.format(1)


def test_non_http_url_is_skipped(tmp_path):
    bad = (
        "URL: not-a-real-url\n"
        f'Your Comment (2 words):\n"bad url"\n\n{SEP}\n\n'
    )
    good = _block(URL.format(2), "ok")
    result = parse(_write(tmp_path, bad + good))
    assert len(result) == 1
    assert result[0]["url"] == URL.format(2)


def test_block_missing_comment_is_skipped(tmp_path):
    bad = f"URL: {URL.format(1)}\nPost Preview:\nbody\n\n{SEP}\n\n"
    result = parse(_write(tmp_path, bad))
    assert result == []


def test_preview_extracted(tmp_path):
    text = _block(URL.format(1), "c", preview="This is the preview line")
    result = parse(_write(tmp_path, text))
    assert result[0]["preview"] == "This is the preview line"


def test_one_bad_among_good_keeps_the_rest(tmp_path):
    good1 = _block(URL.format(1), "first")
    bad = f"URL: not-http\nYour Comment (1 words):\n\"x\"\n\n{SEP}\n\n"
    good2 = _block(URL.format(2), "second")
    result = parse(_write(tmp_path, good1 + bad + good2))
    assert [r["url"] for r in result] == [URL.format(1), URL.format(2)]
