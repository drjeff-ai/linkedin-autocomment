"""Tests for the offline selector engine (dom_probe).

`dom_probe` is what lets the selector gate run in CI with no browser and no
LinkedIn session. It is a deliberate SUBSET of CSS and XPath, so the property
that matters most is not breadth: it is that anything outside the subset raises
instead of quietly counting zero. A selector watchdog that answers "0 matches"
when it means "I did not understand this" reports a clean bill of health for
something it never tested — the exact failure docs/ARCHITECTURE.md §8.2 records.

Adopted from the student fork (see .dev/AUDIT_fork_remainder.md), adapted to our
one-argument ``count(selector)`` contract: the CSS/XPath split is read off the
selector's shape by ``dom_probe.is_xpath`` rather than passed in by the caller.

All markup here is synthetic. No live LinkedIn data.
"""

import pytest

from linkedin_automation import dom_probe
from linkedin_automation import selector_health as shc


DOC = """
<html><body>
  <div id="root" class="wrap">
    <div data-testid="mainFeed">
      <div role="listitem" class="card new"><span data-testid="expandable-text-box">one</span></div>
      <div role="listitem" class="card"><span data-testid="expandable-text-box">two</span></div>
      <a data-view-name="feed-actor-image" href="/in/example-person/"><img></a>
      <div><a href="/in/example-person/">Example Person</a></div>
    </div>
    <aside><div role="listitem" class="card">outside</div></aside>
    <button aria-label="Comment"><span class="artdeco-button__text">7</span></button>
    <button>Comment</button>
    <button class="trigger active">Liked</button>
    <button class="trigger">Like</button>
    <script>var comment = "Comment";</script>
  </div>
</body></html>
"""


@pytest.fixture
def count():
    return dom_probe.make_counter(DOC)


# ─── The supported CSS grammar ────────────────────────────────────────────────

def test_type_class_and_id(count):
    assert count("div") == 6
    assert count(".card") == 3
    assert count("#root") == 1
    assert count("div.card.new") == 1


def test_attribute_operators(count):
    assert count("[data-testid]") == 3
    assert count("[data-testid='mainFeed']") == 1
    assert count("[data-testid*='Feed']") == 1
    assert count("[href^='/in/']") == 2
    assert count("[href$='/example-person/']") == 2
    assert count("[class~='card']") == 3


def test_descendant_and_child_combinators(count):
    # The scoped selector is the one the scraper actually uses, and the aside's
    # listitem is exactly the noise it exists to exclude.
    assert count("div[data-testid='mainFeed'] div[role='listitem']") == 2
    assert count("div[role='listitem']") == 3
    assert count("div[data-testid='mainFeed'] > div[role='listitem']") == 2
    assert count("#root > div[role='listitem']") == 0


def test_adjacent_sibling_combinator(count):
    assert count("a[data-view-name='feed-actor-image'] + div a[href*='/in/']") == 1


def test_negation(count):
    assert count("button.trigger") == 2
    assert count("button.trigger:not(.active)") == 1


def test_selector_lists_are_the_union(count):
    assert count("#root, .card") == 4


def test_universal_after_a_descendant(count):
    assert count("div[data-testid='mainFeed'] *") == 8


# ─── XPath, and the collision it exists to resolve ────────────────────────────

def test_xpath_matches_on_visible_text_not_aria_label(count):
    """The posting-path trap, asserted directly.

    Two buttons involve the word Comment. The action-bar one carries
    aria-label="Comment" and shows a count; the submit one has no aria-label and
    reads "Comment". Only the second is the submit button, and only an XPath
    text match can tell them apart.
    """
    assert count("//button[normalize-space(.)='Comment']") == 1
    assert count("button[aria-label*='Comment']") == 1


def test_xpath_ignores_script_text(count):
    """A script body is text to a parser and invisible to a human. If script
    contents counted, a page could satisfy a text match with nothing rendered."""
    assert count("//script[normalize-space(.)='Comment']") == 0


def test_xpath_attribute_and_bare_forms(count):
    assert count("//div[@role='listitem']") == 3
    assert count("//button") == 4


def test_the_css_xpath_split_is_read_off_the_selector_shape():
    """One rule, shared by the live counter and the offline one.

    If these ever disagreed, the fixture gate and the browser check would be
    silently testing different things.
    """
    from selenium.webdriver.common.by import By
    assert dom_probe.is_xpath("//button[normalize-space(.)='Comment']") is True
    assert dom_probe.is_xpath("div[role='listitem']") is False
    assert shc.selector_by("//button[normalize-space(.)='Comment']") == By.XPATH
    assert shc.selector_by("div[role='listitem']") == By.CSS_SELECTOR


# ─── The property that keeps the gate honest ──────────────────────────────────

@pytest.mark.parametrize("selector", [
    "div:nth-child(2)",
    "div::before",
    "input:checked",
    "div >> span",
    "",
])
def test_unsupported_css_raises_rather_than_counting_zero(count, selector):
    with pytest.raises(dom_probe.UnsupportedSelector):
        count(selector)


@pytest.mark.parametrize("expression", [
    "//button[contains(text(), 'Comment')]",
    "//div/span[1]",
    "(//button)[1]",
])
def test_unsupported_xpath_raises_rather_than_counting_zero(count, expression):
    with pytest.raises(dom_probe.UnsupportedSelector):
        count(expression)


def test_every_registry_selector_is_supported():
    """The gate that stops the grammar from silently falling behind the code.

    A new selector using a construct this engine lacks would otherwise be
    counted as 0 on every fixture run, and the watchdog would report a break
    that is not there — or worse, be adjusted until it stopped complaining.
    """
    count = dom_probe.make_counter(DOC)
    unsupported = []
    for key, spec in shc.SELECTOR_REGISTRY.items():
        # Witnesses are counted by the gate exactly like the selectors under
        # test, so an unreadable one would break the same way.
        for selector in list(spec["selectors"]) + (
                [spec["state_witness"]] if spec.get("state_witness") else []):
            try:
                count(selector)
            except dom_probe.UnsupportedSelector as e:
                unsupported.append(f"{key}: {selector} ({e})")
    assert unsupported == []


# ─── Parsing tolerance and hook harvesting ────────────────────────────────────

def test_unclosed_tags_do_not_derail_the_tree():
    count = dom_probe.make_counter(
        "<div class='a'><p>one<p>two</div><div class='a'>three</div>")
    assert count("div.a") == 2
    assert count("p") == 2


def test_stray_end_tag_is_ignored():
    count = dom_probe.make_counter("</span><div class='a'><span>x</span></div>")
    assert count("div.a span") == 1


def test_harvest_hooks_collects_the_stable_attributes():
    hooks = dom_probe.harvest_hooks(DOC)
    assert hooks["data_testids"] == ["expandable-text-box", "mainFeed"]
    assert hooks["data_view_names"] == ["feed-actor-image"]
    # Only buttons contribute aria-labels: those are the actionable ones.
    assert hooks["button_aria_labels"] == ["Comment"]
    assert "card" in hooks["class_tokens"]


def test_candidate_selectors_are_pasteable():
    candidates = dom_probe.candidate_selectors(dom_probe.harvest_hooks(DOC))
    assert "[data-testid='mainFeed']" in candidates
    assert "[data-view-name='feed-actor-image']" in candidates
    assert "button[aria-label='Comment']" in candidates
    # Every candidate must itself parse, or the repair loop hands you a
    # suggestion the probe cannot then evaluate.
    count = dom_probe.make_counter(DOC)
    for candidate in candidates:
        count(candidate)


def test_the_engine_is_deterministic():
    """Same input, same answer, every time — the whole point of a CI gate."""
    selectors = ["div[role='listitem']", "//button[normalize-space(.)='Comment']",
                 "button.trigger:not(.active)", "[data-testid]"]
    runs = [[dom_probe.make_counter(DOC)(s) for s in selectors] for _ in range(5)]
    assert all(r == runs[0] for r in runs)
