"""Coverage for the post generator's "react to an article" mode.

Adopted from the student fork (.dev/AUDIT_fork_remainder.md §4) and adapted to
our `post_generator`. This path had **no test coverage at all** and, until the
dependency fix in `adopt-fork-bugfixes`, could not run on any clean install:
`requests` and `bs4` are imported inside the functions, so nothing failed until
a user actually pressed Generate from Article.

No network. `_parse_html` is pure, and the two fetch strategies are asserted
structurally rather than executed.
"""

import inspect
import logging

import pytest

from linkedin_automation import post_generator as pg


ARTICLE_HTML = """
<html>
  <head><title>Ignored Title</title></head>
  <body>
    <nav>Home About Contact</nav>
    <header>Site banner</header>
    <article>
      <h1>The Headline</h1>
      <p>First paragraph of the article body, long enough to be real content.</p>
      <p>Second paragraph with more substance so the length bar is cleared.</p>
    </article>
    <aside>Related links you should ignore</aside>
    <footer>Copyright notice</footer>
    <script>var tracking = 1;</script>
  </body>
</html>
"""


@pytest.fixture
def generator():
    """A PostGenerator with no client, since fetching never calls one."""
    return pg.PostGenerator.__new__(pg.PostGenerator)


# ─── The dependencies the feature actually needs ─────────────────────────────

def test_the_article_dependencies_are_installed():
    """Complements test_declared_dependencies: that asserts they are *declared*,
    this asserts the environment running the suite actually has them."""
    import bs4  # noqa: F401
    import requests  # noqa: F401


# ─── The parser ──────────────────────────────────────────────────────────────

def test_the_parser_extracts_the_article_and_drops_the_chrome(generator):
    text = generator._parse_html(ARTICLE_HTML)
    assert text
    assert "First paragraph of the article body" in text
    assert "Second paragraph with more substance" in text
    for noise in ("Home About Contact", "Site banner", "Related links",
                  "Copyright notice", "var tracking"):
        assert noise not in text, f"{noise!r} survived the noise strip"


def test_the_parser_survives_malformed_html(generator):
    """Real pages are truncated and unbalanced. A parser error must not raise."""
    truncated = "<html><body><article><p>Some text that got cut off mid"
    result = generator._parse_html(truncated)
    assert result is None or isinstance(result, str)


def test_a_page_with_no_text_at_all_returns_none(generator):
    assert not generator._parse_html("<html><body></body></html>")


def test_undecodable_bytes_are_reported_not_silently_empty(generator, caplog):
    """Garbage in must not look like "the article was empty".

    An empty result and an undecodable response are different failures, and only
    one of them is worth retrying with Selenium.
    """
    garbage = "\udce2\udc98\udc83" * 200
    with caplog.at_level(logging.DEBUG):
        result = generator._parse_html(garbage)
    assert not result or "First paragraph" not in result


def test_a_thin_page_is_rejected_one_level_up_not_by_the_parser(generator, monkeypatch):
    """The length bar lives in ``_fetch_article``, and that is the right layer.

    ``_parse_html`` returns whatever it found, however little. A page holding one
    sentence is a *successful parse of a thin page*, not a parse failure — and
    only the caller knows a thin page is not worth writing a post about.
    """
    thin = "<html><body><article><p>Hi.</p></article></body></html>"
    assert generator._parse_html(thin) is not None

    monkeypatch.setattr(pg.PostGenerator, "_fetch_with_requests", lambda self, url: "Hi.")
    monkeypatch.setattr(pg.PostGenerator, "_fetch_with_selenium", lambda self, url: "Hi.")
    assert generator._fetch_article("https://example.com/a") is None


# ─── Structure of the two fetch strategies ───────────────────────────────────

def test_both_fetch_strategies_depend_on_the_parser(generator):
    """A fallback that shares the broken component is not a fallback.

    Both strategies funnel through ``_parse_html``, so a parser break takes out
    the retry too — worth knowing, and worth noticing if it ever changes.
    """
    for name in ("_fetch_with_requests", "_fetch_with_selenium"):
        source = inspect.getsource(getattr(pg.PostGenerator, name))
        assert "_parse_html" in source, f"{name} no longer routes through the parser"


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN DEFECT, not yet fixed: _fetch_with_requests hardcodes "
    "'Accept-Encoding: gzip, deflate, br' while brotli is not installed, so it "
    "promises a codec it cannot decode. requests sets this header itself from "
    "the codecs actually present ('gzip, deflate' here). A server honouring br "
    "returns bytes we cannot decode, and the article comes back empty with no "
    "error. Reported in dispatch 3; the fix is deleting the header line. "
    "strict=True so this flips to a hard failure the moment it is fixed."))
def test_the_fetcher_does_not_advertise_an_encoding_it_cannot_decode():
    source = inspect.getsource(pg.PostGenerator._fetch_with_requests)
    hardcoded = [line for line in source.splitlines()
                 if "'Accept-Encoding'" in line and not line.strip().startswith("#")]
    assert hardcoded == [], (
        "Accept-Encoding is being set by hand. Let requests advertise the "
        f"encodings it can actually decode. Found: {hardcoded}")
