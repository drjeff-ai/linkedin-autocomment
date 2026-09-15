"""The Phase 1a harvest tool's scrubber, proven before it is trusted live.

The scrubber runs once, on a capture that cannot be retaken without spending
another live session. So its two failure modes are both tested here:

  * it leaks identity  -> the dump is unsafe to keep
  * it eats structure  -> the dump is useless, and the session is wasted

The second is the one a naive regex-over-HTML scrubber fails. Attribute NAMES,
classes, roles and short visible labels ARE the selector evidence this harvest
exists to collect, so they must survive intact.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.composer_media_dump import scrub_html, signature, summarize  # noqa: E402


# A fabricated composer fragment. Every identifier here is invented; nothing in
# this file came off a real page.
SAMPLE = """
<html><body>
<script>window.__data={"member":"urn:li:person:AbCdEf123","email":"nobody@example.com"};</script>
<style>.x{color:red}</style>
<div role="dialog" aria-label="Create a post">
  <div class="share-box__author" data-member-urn="urn:li:person:AbCdEf123">
    <img class="avatar" src="https://media.licdn.com/dms/image/C4E03AQ/profile-photo.jpg"
         alt="Fake Person" />
    <span>Fake Person</span>
  </div>
  <div class="ql-editor" contenteditable="true">Phase 1 harvest test</div>
  <p>This is a long paragraph of member-authored prose that is well past the keep
     threshold and must not survive the scrub because it is exactly the kind of
     content a feed capture leaks.</p>
  <button aria-label="Add media" class="media-btn" data-test-icon="image-medium">
    Add media
  </button>
  <input type="file" accept="image/*" class="hidden-file-input" name="upload" />
  <a href="/in/fake-person-9a8b7c6d">Fake Person</a>
  <button class="share-actions__primary-action">Post</button>
</div>
</body></html>
"""


def _scrub(extra=()):
    return scrub_html(SAMPLE, extra)


# --- It must not leak identity ----------------------------------------------

def test_urns_are_scrubbed_in_text_and_in_attributes():
    out, residue = _scrub()
    assert "AbCdEf123" not in out
    assert "urn:li:person:[SCRUBBED]" in out
    assert residue["remaining_urn_like"] >= 0


def test_script_and_style_are_dropped_entirely():
    """They carry embedded JSON payloads of member data and prove nothing."""
    out, residue = _scrub()
    assert "window.__data" not in out
    assert "nobody@example.com" not in out
    assert residue["script_style_dropped"] >= 2


def test_media_urls_and_profile_paths_lose_their_identifiers():
    out, _ = _scrub()
    assert "profile-photo.jpg" not in out
    assert "fake-person-9a8b7c6d" not in out
    assert "/in/[SCRUBBED]" in out


def test_long_prose_is_dropped_but_counted():
    out, residue = _scrub()
    assert "member-authored prose" not in out
    assert "[TEXT:" in out
    assert residue["long_text_nodes_dropped"] >= 1


def test_an_explicit_name_is_redacted_everywhere_including_alt_text():
    """--scrub-extra is how the author chip's display name gets removed."""
    out, _ = _scrub(extra=("Fake Person",))
    assert "Fake Person" not in out
    assert "[NAME]" in out


# --- It must not eat the structure being harvested --------------------------

def test_the_file_input_survives_with_the_attributes_the_harvest_needs():
    """The whole point of Phase 1a: find the hidden input[type=file]."""
    out, _ = _scrub()
    assert 'type="file"' in out
    assert 'accept="image/*"' in out
    assert "hidden-file-input" in out
    assert 'name="upload"' in out


def test_short_button_labels_survive_because_selectors_are_built_on_them():
    """XPath-on-visible-text is how the composer's Post button is already found."""
    out, _ = _scrub()
    assert "Add media" in out
    assert ">Post<" in out or "Post" in out
    assert 'aria-label="Add media"' in out
    assert 'data-test-icon="image-medium"' in out


def test_structural_attributes_are_untouched():
    out, _ = _scrub()
    assert 'role="dialog"' in out
    assert 'aria-label="Create a post"' in out
    assert 'contenteditable="true"' in out
    assert "share-actions__primary-action" in out
    assert "ql-editor" in out


def test_scrubbing_is_idempotent():
    """A second pass must not corrupt placeholders from the first."""
    once, _ = _scrub(extra=("Fake Person",))
    twice, _ = scrub_html(once, ("Fake Person",))
    assert "[SCRUBBED][SCRUBBED]" not in twice
    assert "urn:li:person:[SCRUBBED]" in twice


# --- The change detector that drives the stage-5 burst ----------------------

def _probe(**over):
    p = {"fileInputCount": 1, "interactiveCount": 20,
         "signals": {"progressbars": 0, "ariaBusy": 0, "spinnerish": 0,
                     "imgSrcKinds": ["blob:"], "postButtons": [{"disabled": False}]}}
    p["signals"].update(over.pop("signals", {}))
    p.update(over)
    return p


def test_signature_changes_when_the_upload_state_changes():
    """blob: -> a real media host is the likely completion signal; the burst
    only writes HTML when the signature moves, so it must move here."""
    pending = _probe(signals={"imgSrcKinds": ["blob:"], "progressbars": 1})
    done = _probe(signals={"imgSrcKinds": ["media.licdn.com"], "progressbars": 0})
    assert signature(pending) != signature(done)


def test_signature_is_stable_when_nothing_relevant_changed():
    assert signature(_probe()) == signature(_probe())


def test_summarize_reports_hidden_file_inputs():
    """The count that answers the roadmap's open question at a glance."""
    p = _probe(fileInputs=[{"selenium_is_displayed": False},
                           {"selenium_is_displayed": True}])
    assert "file_inputs=2(hidden:1)" in summarize(p)


# --- Regressions from the void 2026-09-03 capture ---------------------------
#
# That run produced seven snapshots, six byte-identical, all of the idle feed
# with the composer shut. Nothing in the tool objected. These pin the signals
# that now make that state impossible to record silently.

def test_summarize_surfaces_the_composer_and_trigger_state():
    """`composer=False trigger=True` is the void capture's fingerprint: the
    'Start a post' button only exists while the composer is CLOSED, so seeing
    it contradicts any claim that the composer is open."""
    void = {"composerDetected": False, "triggerPresent": True, "dialogCount": 4,
            "fileInputs": [], "signals": {}}
    out = summarize(void)
    assert "composer=False" in out
    assert "trigger=True" in out

    good = {"composerDetected": True, "triggerPresent": False, "dialogCount": 5,
            "fileInputs": [], "signals": {}}
    assert "composer=True" in summarize(good)
    assert "trigger=False" in summarize(good)


def test_imagesrcset_is_scrubbed():
    """53 licdn URLs survived the first run through `imagesrcset`, an attribute
    the explicit list did not name. Suffix matching closes that class of gap."""
    html = ('<link imagesrcset="https://media.licdn.com/dms/image/AAA/x.jpg 20w" />'
            '<img srcset="https://media.licdn.com/dms/image/BBB/y.jpg 40w" />')
    out, residue = scrub_html(html)
    assert "AAA" not in out
    assert "BBB" not in out
    assert residue["remaining_licdn_like"] == 0


def test_value_attr_matching_covers_url_bearing_suffixes():
    from tools.composer_media_dump import _is_value_attr
    for attr in ("href", "src", "imagesrcset", "data-ghost-url",
                 "data-member-urn", "data-x-srcset", "data-canonical-href"):
        assert _is_value_attr(attr), attr
    # Structural attributes must NOT be scrubbed - they are the evidence.
    for attr in ("class", "role", "type", "aria-label", "contenteditable",
                 "data-test-icon", "accept"):
        assert not _is_value_attr(attr), attr


# --- Run-2 gaps: manifest scrubbing, scoped signals, the observer -----------

def test_the_manifest_is_scrubbed_not_just_the_html():
    """Run 2 wrote the manifest raw: 228 licdn URLs, base64 image data and
    filenames survived in the one file that actually gets read afterwards."""
    from tools.composer_media_dump import scrub_obj, residue_of
    import json as _json

    manifest = [{
        "stage": 7,
        "probe": {"interactive": [
            {"tag": "img", "attrs": {
                "src": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg" + "A" * 80,
                "class": "update-components-image__image"}},
            {"tag": "img", "attrs": {
                "src": "https://media.licdn.com/dms/image/REALID/photo.jpg"}},
        ]},
    }, {"kind": "note", "action": "send_keys_attach",
        "image": r"C:\Users\realname\pics\test_image.png"}]

    blob = _json.dumps(scrub_obj(manifest, ("Real Person",)))
    assert "iVBORw0KGgo" not in blob
    assert "REALID" not in blob
    assert "realname" not in blob
    assert residue_of(blob)["remaining_licdn_like"] == 0
    # Structure must survive - it is the selector evidence.
    assert "update-components-image__image" in blob
    assert "send_keys_attach" in blob


def test_scrub_obj_leaves_keys_alone_and_handles_nesting():
    from tools.composer_media_dump import scrub_obj
    out = scrub_obj({"src": ["urn:li:person:ABC", {"href": "/in/somebody"}]})
    assert list(out) == ["src"]
    assert out["src"][0] == "urn:li:person:[SCRUBBED]"
    assert out["src"][1]["href"] == "/in/[SCRUBBED]"


def test_signature_now_moves_on_scoped_composer_signals():
    """The run-2 failure: document-wide signals meant feed churn masked the
    composer transition. These are the scoped things that actually change."""
    def p(**sig):
        base = {"progressbars": 0, "ariaBusy": 0, "spinnerish": 0,
                "imgSrcKinds": [], "postButtons": [], "nextButtons": [],
                "previewContainers": 0, "mediaEditor": 0, "scopedElements": 100}
        base.update(sig)
        return {"fileInputCount": 0, "interactiveCount": 200, "signals": base}

    # Media editor open (Post gone, Next shown) -> back in composer with preview.
    editing = p(mediaEditor=1, nextButtons=[{"text": "Next", "disabled": False}])
    attached = p(previewContainers=1,
                 postButtons=[{"disabled": False, "visible": True}],
                 imgSrcKinds=["data:"])
    assert signature(editing) != signature(attached)

    # Post button flipping enabled is on its own enough to move the fingerprint.
    a = p(postButtons=[{"disabled": True, "visible": True}])
    b = p(postButtons=[{"disabled": False, "visible": True}])
    assert signature(a) != signature(b)

    # And the preview container appearing.
    assert signature(p(previewContainers=0)) != signature(p(previewContainers=1))


def test_summarize_reports_the_scoped_completion_candidates():
    p = {"composerDetected": True, "triggerPresent": True, "dialogCount": 6,
         "scopeFound": True, "fileInputs": [],
         "signals": {"previewContainers": 1, "mediaEditor": 0,
                     "nextButtons": [], "postButtons": [{"disabled": False}]}}
    out = summarize(p)
    assert "scope=True" in out
    assert "preview=1" in out
    assert "mediaEd=0" in out


def test_observer_js_records_both_add_and_remove():
    """The input is expected to be transient - mounted for the picker, unmounted
    on close - so capturing only additions would still lose it."""
    from tools.composer_media_dump import OBSERVER_JS, DRAIN_JS
    assert 'scanNode(n, "added")' in OBSERVER_JS
    assert 'scanNode(n, "removed")' in OBSERVER_JS
    assert "input[type=file]" in OBSERVER_JS
    assert "present-at-arm" in OBSERVER_JS      # pre-existing inputs too
    assert "shadowRoot" in OBSERVER_JS          # shadow trees are separate
    assert "__harvestLog = []" in DRAIN_JS      # draining must reset


def test_probe_js_scopes_signals_and_does_not_require_an_editor():
    """During media editing there is no contenteditable, and that is exactly the
    window the completion signal lives in - so scope resolution must not
    depend on one."""
    from tools.composer_media_dump import PROBE_JS
    assert "media-editor__layout-container" in PROBE_JS
    assert "share-creation-state__preview-container" in PROBE_JS
    assert "const scoped = (sel) => deepAll(sel, S);" in PROBE_JS
    for signal in ("progressbars", "ariaBusy", "spinnerish", "previewContainers",
                   "mediaEditor"):
        assert "%s: scoped(" % signal in PROBE_JS or "%s:" % signal in PROBE_JS


def test_compound_urns_are_scrubbed_including_the_payload():
    """The run-3 leak: a plain value class stops at the "(" so the outer URN kept
    two real activity ids. Only the inner fsd_profile had been caught."""
    html = ('<div data-x="urn:li:comment:(urn:li:activity:0000000000000000000,'
            '0000000000000001)">a</div>'
            '<div data-y="urn:li:msg_message:(urn:li:fsd_profile:AbC,'
            '2-FAKEMESSAGEIDFORTESTS)">b</div>')
    out, residue = scrub_html(html)
    # Was asserted against the real id this test was first written from; that
    # value is not in the input any more, so the assertion checked nothing while
    # still carrying a real activity id in committed source.
    assert "0000000000000000000" not in out
    assert "0000000000000001" not in out
    assert "2-FAKEMESSAGEIDFORTESTS" not in out
    assert "AbC" not in out
    assert residue["remaining_urn_like"] == 0
    # The URN TYPE survives - it is structure, and it says what broke.
    assert "urn:li:comment:" in out
    assert "urn:li:msg_message:" in out
