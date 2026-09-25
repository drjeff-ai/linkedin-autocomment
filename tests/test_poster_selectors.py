"""Phase 0: the composer's selectors, its monitor, and the read-back guard.

`poster.py` is the one code path that PUBLISHES, and until Phase 0 it had no
test, no registry entry, and no fixture — every selector was a literal buried in
a method body, which is precisely why `selector_health` could not see it. That is
the same blind spot that let the login form's `[id="username"]` rot unnoticed,
and the same one that let the comment path break for a month in July.

Nothing here touches a browser. The live half of the gate is a human publishing a
real post; see docs/ARCHITECTURE.md §8.3.
"""

import io
import os

import pytest

from linkedin_automation import dom_probe
from linkedin_automation import selector_health as shc
from linkedin_automation.poster import LinkedInPoster

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    with io.open(os.path.join(FIXTURE_DIR, f"{name}.html"), encoding="utf-8") as f:
        return f.read()


# ─── The hoist: selectors are constants, and the registry can see them ───────

COMPOSER_KEYS = ("composer_trigger", "composer_editor", "composer_post_button")


@pytest.mark.parametrize("name", [
    "COMPOSER_TRIGGER_TEXT", "COMPOSER_TRIGGER_SELECTORS", "COMPOSER_TRIGGER_XPATH",
    "COMPOSER_EDITOR_SELECTORS", "COMPOSER_EDITOR_ARIA_LABELS",
    "COMPOSER_EDITOR_ARIA_SELECTORS",
    "COMPOSER_POST_BUTTON_TEXT", "COMPOSER_POST_BUTTON_XPATH",
    "COMPOSER_POST_BUTTON_SELECTORS",
])
def test_the_composer_selectors_are_class_constants(name):
    """The registry is built from class constants. A literal inside a method body
    is invisible to it — which is how the composer went unmonitored."""
    assert hasattr(LinkedInPoster, name), f"{name} is not a class constant"


@pytest.mark.parametrize("key", COMPOSER_KEYS)
def test_the_composer_is_in_the_registry(key):
    assert key in shc.SELECTOR_REGISTRY


@pytest.mark.parametrize("key", COMPOSER_KEYS)
def test_every_composer_entry_names_the_symbol_a_fixer_must_edit(key):
    """A report that says what broke but not where to fix it wastes the finding."""
    assert shc.SELECTOR_REGISTRY[key]["fix_symbol"]


@pytest.mark.parametrize("key", COMPOSER_KEYS)
def test_the_registry_stays_in_sync_with_the_poster(key):
    """The entries must be BUILT from the constants, not copies that can drift.

    A copied selector list is worse than none: it passes the gate while the code
    uses something else.
    """
    registered = set(shc.SELECTOR_REGISTRY[key]["selectors"])
    live = set(LinkedInPoster.COMPOSER_TRIGGER_SELECTORS
               + LinkedInPoster.COMPOSER_EDITOR_SELECTORS
               + LinkedInPoster.COMPOSER_EDITOR_ARIA_SELECTORS
               + LinkedInPoster.COMPOSER_POST_BUTTON_SELECTORS
               + [LinkedInPoster.COMPOSER_POST_BUTTON_XPATH])
    assert registered <= live, f"{key} has selectors the poster does not use"


@pytest.mark.parametrize("key", COMPOSER_KEYS)
def test_every_composer_selector_parses(key):
    """An unreadable selector must break the build, never quietly count zero."""
    count = dom_probe.make_counter("<html><body></body></html>")
    spec = shc.SELECTOR_REGISTRY[key]
    for sel in list(spec["selectors"]) + (
            [spec["state_witness"]] if spec.get("state_witness") else []):
        count(sel)          # raises UnsupportedSelector if the grammar lacks it


def test_the_contains_xpath_is_deliberately_not_registered():
    """COMPOSER_TRIGGER_XPATH uses contains(), which dom_probe's grammar lacks.

    Registering it would raise UnsupportedSelector and break the build, so it
    stays a live-only fallback — and the entry's note says so, rather than
    letting a reader assume the fallback is covered too.
    """
    assert "contains(" in LinkedInPoster.COMPOSER_TRIGGER_XPATH
    entry = shc.SELECTOR_REGISTRY["composer_trigger"]
    assert LinkedInPoster.COMPOSER_TRIGGER_XPATH not in entry["selectors"]
    assert "contains()" in entry["note"]
    with pytest.raises(dom_probe.UnsupportedSelector):
        dom_probe.select_xpath(dom_probe.parse_html("<html></html>"),
                               LinkedInPoster.COMPOSER_TRIGGER_XPATH)


# ─── The gate: open composer counted, closed composer NOT SILENTLY PASSED ────

def test_an_open_composer_checks_the_editor_and_the_post_button():
    report = shc.fixture_report(fixture("composer_open"), page="composer")
    assert report["status"] == "HEALTHY"
    assert report["not_checked"] == []
    assert report["checks"]["composer_editor"]["count"] >= 1
    assert report["checks"]["composer_post_button"]["count"] >= 1


def test_a_closed_composer_reports_them_as_not_checked_with_a_reason():
    """The honesty rule: "not looked at" must never read as "looked at and fine"."""
    report = shc.fixture_report(fixture("feed_healthy"), page="composer")
    assert set(report["not_checked"]) == {"composer_editor", "composer_post_button",
                                          "composer_media_button"}
    for key in ("composer_editor", "composer_post_button", "composer_media_button"):
        entry = report["checks"][key]
        assert entry["checked"] is False
        assert "composer must be open" in entry["skip_reason"]
    # Gated != broken.
    assert report["status"] == "HEALTHY"
    assert report["failed"] == []


def test_the_trigger_is_checked_on_a_plain_feed_load():
    """composer_trigger is ungated on purpose: the "Start a post" box needs no
    interaction, so a normal feed health run covers it live."""
    report = shc.fixture_report(fixture("feed_healthy"), page="feed")
    entry = report["checks"]["composer_trigger"]
    assert entry["checked"] is True
    assert entry["count"] >= 1


def test_the_post_button_xpath_is_not_fooled_by_the_audience_selector():
    """The trap, end to end. composer_open carries three controls saying "Post";
    only one publishes.

    A CSS substring match would hit all three and return a confident wrong
    answer. The XPath matches exactly the submit button — same reasoning as
    LinkedInCommentPoster.SUBMIT_BUTTON_XPATH.
    """
    count = dom_probe.make_counter(fixture("composer_open"))
    assert count(LinkedInPoster.COMPOSER_POST_BUTTON_XPATH) == 1
    html = fixture("composer_open")
    assert "Post to Anyone" in html, "the trap must stay in the fixture"
    assert "Start a post" in html, "the trigger must stay in the fixture"


def test_a_renamed_post_button_breaks_the_composer_check():
    """The gate must FAIL when the publish button is relabelled — otherwise it
    is decoration. This is the shape of every real break here."""
    html = fixture("composer_open").replace(
        '<button class="s1t2u3">Post</button>',
        '<button class="s1t2u3">Publish</button>')
    report = shc.fixture_report(html, page="composer")
    assert report["status"] == "BROKEN"
    assert report["failed"] == ["composer_post_button"]


# ─── Safety fix B: the read-back guard ───────────────────────────────────────

def test_normalization_ignores_what_the_editor_re_renders():
    """Whitespace and unicode differences are the editor's, not a corruption."""
    n = LinkedInPoster.normalize_for_comparison
    assert n("Hello   world\n\n#ai") == n("hello world #ai")
    assert n("A B") == n("A B")           # nbsp the editor inserts
    assert n(" trailing ") == n("trailing")


def test_normalization_still_notices_a_changed_word():
    """It must not normalize away the thing it exists to catch."""
    n = LinkedInPoster.normalize_for_comparison
    assert n("we shipped #ai") != n("we shipped #aiagents")
    assert n("#ml today") != n("#machinelearning today")


def test_a_hashtag_before_a_newline_is_flagged_for_dismissal():
    """The exact hazard: `#tag` then Enter, where Enter selects a suggestion."""
    segs = LinkedInPoster._segments_for_typing(None, "body text\n\n#ai #agents\ntail")
    flagged = [chunk for chunk, needs in segs if needs]
    assert flagged == ["#ai #agents"]


def test_text_without_hashtags_needs_no_dismissal():
    segs = LinkedInPoster._segments_for_typing(None, "one line\ntwo lines\n")
    assert not any(needs for _, needs in segs)


def test_the_split_preserves_the_text_exactly():
    """The containment must not change the post. Blank lines included."""
    for text in ("a\n\nb", "#tag\nmore", "no newlines at all", "trailing\n"):
        segs = LinkedInPoster._segments_for_typing(None, text)
        assert "".join(chunk for chunk, _ in segs) == text


class _FakeEditor:
    def __init__(self, text):
        self._text = text

    def get_attribute(self, name):
        return self._text if name == "innerText" else None

    @property
    def text(self):
        return self._text


def _poster_with_editor(text):
    poster = LinkedInPoster.__new__(LinkedInPoster)      # no browser, no config
    poster._editor = _FakeEditor(text)
    poster.driver = None
    return poster


def test_the_guard_passes_when_the_editor_holds_what_we_composed():
    poster = _poster_with_editor("We shipped it.\n\n#ai #agents")
    assert poster.verify_composed_text("We shipped it.\n\n#ai #agents") is True


def test_the_guard_passes_through_harmless_re_rendering():
    """A newline collapsed by innerText is not a corruption."""
    poster = _poster_with_editor("We shipped it. #ai")
    assert poster.verify_composed_text("We shipped it.\n#ai") is True


def test_the_guard_catches_a_typeahead_substitution():
    """The failure this whole fix exists for: the tag became a suggestion."""
    poster = _poster_with_editor("We shipped it. #artificialintelligence")
    assert poster.verify_composed_text("We shipped it. #ai") is False


def test_the_guard_catches_a_swallowed_line():
    poster = _poster_with_editor("We shipped it.")
    assert poster.verify_composed_text("We shipped it.\n\n#ai") is False


def test_the_guard_catches_an_empty_editor():
    """Typing silently failing must not publish an empty post."""
    poster = _poster_with_editor("")
    assert poster.verify_composed_text("We shipped it.") is False


def test_a_renamed_hook_inside_an_OPEN_composer_is_not_excused_as_not_checked():
    """The state_witness exists for exactly this.

    Without it, "composer closed" and "composer open but the hook was renamed"
    both produce zero matches and both get excused as *not checked* — a BROKEN
    gate reported as a shrug, which is the July breakage shape all over again.
    The witness proves the state is on screen, so the rename fails loudly.
    """
    html = fixture("composer_open").replace(
        '<button class="s1t2u3">Post</button>',
        '<button class="s1t2u3">Publish</button>')
    count = dom_probe.make_counter(html)
    spec = shc.SELECTOR_REGISTRY["composer_post_button"]

    # The state IS present...
    assert shc.gated_state_present(spec, count) is True
    # ...and none of the entry's own selectors match any more.
    assert all(count(sel) == 0 for sel in spec["selectors"])

    report = shc.fixture_report(html, page="composer")
    assert report["status"] == "BROKEN"
    assert report["failed"] == ["composer_post_button"]
    assert "composer_post_button" not in report["not_checked"]


def test_a_witnessless_entry_keeps_the_older_behaviour():
    """Adding the mechanism must not change entries that do not opt in.

    `connector_send_button` is the one entry deliberately left without a witness:
    there is no Connect-dialog fixture and the live search run excludes
    modal_only entries, so nothing reaches that state and any witness would be an
    unverified guess dressed up as coverage.
    """
    spec = shc.SELECTOR_REGISTRY["connector_send_button"]
    assert "state_witness" not in spec
    assert shc.gated_state_present(
        spec, dom_probe.make_counter(fixture("feed_healthy"))) is False


def test_every_other_gated_entry_now_has_a_witness():
    """The retrofit's scope, asserted so a new gated entry cannot quietly skip it."""
    witnessless = [k for k, v in shc.SELECTOR_REGISTRY.items()
                   if shc.gate_reason(v) and not v.get("state_witness")]
    assert witnessless == ["connector_send_button"]


# ─── Phase 1b: the image-attachment selectors, offline ───────────────────────
#
# Both fixtures are hand-authored from the 2026-09-04 staged harvest. They pin
# the two states the fail-closed wait has to tell apart, and the registry
# entries that read them.

def test_the_attached_image_state_checks_the_preview():
    report = shc.fixture_report(fixture("composer_image_attached"),
                                page="composer_image")
    assert report["status"] == "HEALTHY"
    assert report["checks"]["composer_image_preview"]["count"] >= 1
    assert report["not_checked"] == []


def test_the_media_editor_state_checks_the_editor_and_next():
    report = shc.fixture_report(fixture("composer_media_editor_open"),
                                page="composer_media")
    assert report["status"] == "HEALTHY"
    assert report["checks"]["composer_media_editor"]["count"] >= 1
    assert report["checks"]["composer_media_next"]["count"] >= 1


def test_a_renamed_image_preview_is_not_excused_as_not_checked():
    """The witness pattern applied to the new entries: with the composer open,
    a renamed preview hook must fail loudly. If this ever silently passed, every
    image post would fail closed with no explanation."""
    html = fixture("composer_image_attached").replace(
        "share-creation-state__preview-container",
        "share-creation-state__preview-box").replace(
        "update-components-image__container--preview",
        "update-components-image__container--gone").replace(
        'aria-label="Edit media preview"', 'aria-label="Edit media thing"')
    report = shc.fixture_report(html, page="composer_image")
    assert "composer_image_preview" not in report["not_checked"]
    assert report["checks"]["composer_image_preview"]["checked"] is True
    assert report["status"] == "BROKEN"
    assert "composer_image_preview" in report["failed"]


def test_the_media_entries_are_not_checked_when_the_editor_never_opened():
    """Gated != broken: a composer with no media editor must report the media
    entries as not-checked, not as failures."""
    report = shc.fixture_report(fixture("composer_image_attached"),
                                page="composer_media")
    assert set(report["not_checked"]) == {"composer_media_editor",
                                          "composer_media_next"}
    assert report["status"] == "HEALTHY"
    assert report["failed"] == []


def test_the_new_media_entries_all_carry_a_witness():
    """No new gated entry may join without one - that hole cost weeks in July."""
    for key in ("composer_media_button", "composer_media_editor",
                "composer_media_next", "composer_image_preview"):
        spec = shc.SELECTOR_REGISTRY[key]
        assert shc.gate_reason(spec), key
        assert spec.get("state_witness"), key


def test_the_fixtures_carry_no_real_identifiers():
    """These ship in the repo. The live dumps carried compound URNs like
    urn:li:comment:(urn:li:activity:...) that the harvest scrubber missed."""
    import re
    for name in ("composer_image_attached", "composer_media_editor_open"):
        # Strip HTML comments first: the fixtures document WHY they carry no
        # real identifiers, and naming the pattern in that explanation is not
        # the same as containing one. The markup is what ships.
        markup = re.sub(r"<!--.*?-->", "", fixture(name), flags=re.S)
        assert not re.search(r"urn:li:", markup), name
        assert "licdn.com" not in markup, name
        assert not re.search(r"base64,[A-Za-z0-9+/=]{60,}", markup), name
        assert "[TEXT:" not in markup, name + " looks like a raw capture"
