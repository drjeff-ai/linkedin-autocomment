"""Staged live harvest of LinkedIn's composer media-upload flow (Phase 1, 1a).

The composer's media DOM does not exist until a human clicks, and upload states
cannot be paused, so this tool is a PURE OBSERVER driven by a human at the
keyboard: you perform each step in the browser, press Enter here, and it
snapshots. By default it takes NO action at all - it only observes.

The single exception is opt-in and explicit: --attach-via-send-keys IMAGE
attaches one image at stage 5 by send_keys to the hidden file input, which is
exactly the technique Phase 1b will use, so the harvest proves the mechanism
while a human is watching. Even then it never clicks Post: nothing this tool
does can publish.

    uv run python tools/composer_media_dump.py --profile dev --scrub-extra "Your Name"

What it captures per stage (roadmap Phase 1a table):

    1 feed baseline      2 composer open      3 text typed
    4 pre-media          5 upload IN FLIGHT   6 upload complete
    7 image attached

WHAT RUN 1 TAUGHT (void), AND WHAT RUN 2 TAUGHT (real but incomplete)
---------------------------------------------------------------------
Run 1 was VOID: seven snapshots, six byte-identical, every one the idle feed
with the composer shut. WebDriver watched one Chrome window while the human
drove another, and nothing said so. Hence the liveness hashing, the composer
gate, and the window check.

Run 2 captured the composer for real (6 distinct DOM states, editors present,
the media editor overlay, the attached preview) but still missed the two things
Phase 1b actually needs:

  * THE FILE INPUT was never in the DOM at any observed instant - not in light
    DOM, not in shadow. The cause was this tool's own stage-4 instruction to
    press ESC on the native picker: LinkedIn appears to mount input[type=file],
    open the picker, and unmount it on cancel, so ESC destroyed the very element
    being hunted. Fixed by arming a MutationObserver BEFORE the click, which
    records the input on add AND on remove, with full attributes - so it no
    longer matters how briefly the element lives, and ESC is no longer needed.

  * THE COMPLETION TRANSITION was masked. Signals were computed document-wide,
    and the feed behind the modal is full of images and a video carrying its own
    role=progressbar, so composer-internal changes never moved the fingerprint.
    Fixed by scoping every signal to the composer/media-editor dialog.

Run 2 also showed the preview src is a `data:` URI, NOT `blob:` and NOT a CDN
host - so "blob -> CDN" is the wrong completion signal. What actually moves is
scoped: the Post button vanishes during media editing and returns enabled, the
share-creation-state preview container appears, and in-dialog progress clears.
Those are the candidates this tool now tracks.

Every artifact lands in .harvest/ (gitignored as a directory, so a new artifact
type cannot escape by not matching a filename pattern). BOTH the HTML and the
manifest are scrubbed before they are written - run 2 left 228 licdn URLs and
base64 image data in the manifest, which is the file actually read afterwards.
The raw page source is never persisted. The repo gets a hand-authored synthetic
fixture later, never this capture.
"""

import argparse
import hashlib
import json
import os as _os
import re
import sys as _sys
import time
from datetime import datetime

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from selenium.webdriver.common.by import By  # noqa: E402

from linkedin_automation import profile_manager as pm  # noqa: E402

OUT_DIR = ".harvest"

# --- Scrubbing --------------------------------------------------------------
#
# Structural scrubbing via BeautifulSoup rather than regex-over-HTML, because the
# whole point of this capture is the STRUCTURE: attribute names, classes, roles
# and short visible labels are the selector evidence. A regex sweep that mangles
# an aria-label destroys the thing being harvested.
#
# Rule for text: keep <= TEXT_KEEP chars, drop the rest. Button labels ("Add
# media", "Post", "Done") are short and are exactly what an XPath-on-visible-text
# selector needs; prose that long is feed or member content.

TEXT_KEEP = 60

# COMPOUND urns are the ones that leaked. A plain value class stops dead at
# the "(" in urn:li:comment:(urn:li:activity:123,456), so the outer URN kept
# its parenthesised payload - two real activity ids - while only the inner
# fsd_profile got scrubbed. The alternation takes a parenthesised group first,
# then falls back to a plain token.
URN_RE = re.compile(
    r"(urn:li:[a-zA-Z_]+:)(\([^)]*\)|[A-Za-z0-9_\-=%.]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
LICDN_RE = re.compile(r"(https?://[a-z0-9.\-]*licdn\.com/)[^\"'\s)]+", re.I)
DATAURI_RE = re.compile(r"(data:[a-z/+.\-]+;base64,)[A-Za-z0-9+/=]+", re.I)
PROFILE_PATH_RE = re.compile(r"(/in/)[A-Za-z0-9\-%]+")
# A local path carries the OS username. The manifest records the image path the
# operator passed, so this matters there even though it never appears in HTML.
USERPATH_RE = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\"']+", re.I)

# Residue counting needs its own pattern. LICDN_RE happily re-matches a URL
# that has ALREADY been scrubbed - "licdn.com/[SCRUBBED]" still has a tail -
# so using it to count leftovers reports scrubbed URLs as leaks. That is what
# inflated the first run's "53 licdn-like remaining". The lookahead makes the
# residue number mean what it says.
RESIDUE_LICDN_RE = re.compile(
    r"https?://[a-z0-9.\-]*licdn\.com/(?!\[SCRUBBED\])[^\"'\s)]+", re.I)

# Attribute values that may carry identity. Structural attributes (class, role,
# type, aria-label, data-test-*) are deliberately NOT in this list.
#
# imagesrcset/imagesizes were MISSING on the first run and 53 licdn URLs
# survived into the dumps because of it. Any attribute that can hold a URL has
# to be here, so matching is suffix-based too (see _is_value_attr).
VALUE_ATTRS = ("href", "src", "srcset", "imagesrcset", "imagesizes", "style",
               "content", "value", "alt", "title", "placeholder",
               "data-ghost-url", "poster", "data-delayed-url", "action")

# Placeholders use [BRACKETS], not <angle brackets>, deliberately: the scrubbed
# text is re-serialized as HTML, so <SCRUBBED> comes back out as &lt;SCRUBBED&gt;
# - ambiguous to read, awkward to grep, and easy to mistake for a tag. Brackets
# survive serialization byte-for-byte and match the [TEXT:Nchars] marker.


def _scrub_text(s, extra):
    if not s:
        return s
    s = URN_RE.sub(r"\1[SCRUBBED]", s)
    s = EMAIL_RE.sub("[EMAIL]", s)
    s = LICDN_RE.sub(r"\1[SCRUBBED]", s)
    s = DATAURI_RE.sub(r"\1[SCRUBBED]", s)
    s = PROFILE_PATH_RE.sub(r"\1[SCRUBBED]", s)
    s = USERPATH_RE.sub(r"\1[SCRUBBED]", s)
    for term in extra:
        if term:
            s = re.sub(re.escape(term), "[NAME]", s, flags=re.I)
    return s


def _is_value_attr(attr):
    """Attribute names whose VALUE may carry identity.

    Suffix matching as well as the explicit list, because the first run leaked
    licdn URLs through `imagesrcset` - an attribute nobody thought to enumerate.
    """
    return (attr in VALUE_ATTRS
            or attr.endswith("-url") or attr.endswith("-urn")
            or attr.endswith("srcset") or attr.endswith("href"))


def scrub_obj(obj, extra=()):
    """Scrub every string inside a nested structure, for the MANIFEST.

    Run 2 wrote the manifest raw and it ended up holding 228 licdn URLs and five
    base64 image fragments - in the one file that actually gets read afterwards.
    The HTML was scrubbed and the summary of it was not, which is the wrong way
    round. Keys are left alone (they are schema, not data); values are scrubbed.
    """
    if isinstance(obj, str):
        return _scrub_text(obj, extra)
    if isinstance(obj, list):
        return [scrub_obj(v, extra) for v in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v, extra) for k, v in obj.items()}
    return obj


def residue_of(text):
    """Leak counts for an already-scrubbed blob, HTML or JSON alike."""
    return {
        "remaining_urn_like": len(URN_RE.findall(text)),
        "remaining_email_like": len(EMAIL_RE.findall(text)),
        "remaining_licdn_like": len(RESIDUE_LICDN_RE.findall(text)),
    }


def scrub_html(html, extra=()):
    """Return (scrubbed_html, residue_report). Never returns the raw source."""
    from bs4 import BeautifulSoup, Comment, NavigableString

    soup = BeautifulSoup(html, "html.parser")

    # script/style carry embedded JSON payloads full of member data and are
    # useless for selector derivation. Drop them outright.
    dropped = 0
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
        dropped += 1
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    long_text = 0
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        text = str(node)
        if len(text.strip()) > TEXT_KEEP:
            node.replace_with("[TEXT:%dchars]" % len(text.strip()))
            long_text += 1
        else:
            scrubbed = _scrub_text(text, extra)
            if scrubbed != text:
                node.replace_with(scrubbed)

    attrs_scrubbed = 0
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            val = tag.attrs[attr]
            if isinstance(val, list):
                val = " ".join(val)
            val = str(val)
            if _is_value_attr(attr):
                new = _scrub_text(val, extra)
            else:
                # A URN or an email is NEVER selector evidence, wherever it
                # sits, so those two are scrubbed out of EVERY attribute rather
                # than only the enumerated ones. Run 3 leaked compound URNs
                # through attributes nobody had thought to list - the same shape
                # of miss as imagesrcset - and enumerating attribute names is a
                # game that keeps being lost. Everything else is left alone,
                # because class/role/aria-label ARE the evidence.
                new = EMAIL_RE.sub("[EMAIL]", URN_RE.sub(r"\1[SCRUBBED]", val))
            if new != val:
                attrs_scrubbed += 1
            tag.attrs[attr] = new

    out = str(soup)
    residue = {
        "script_style_dropped": dropped,
        "long_text_nodes_dropped": long_text,
        "attr_values_scrubbed": attrs_scrubbed,
    }
    residue.update(residue_of(out))
    return out, residue


# --- The file-input observer ------------------------------------------------
#
# THE FIX FOR RUN 2's BIGGEST GAP. LinkedIn never showed an input[type=file] at
# any moment this tool sampled - it appears to mount one, open the native
# picker, and unmount it when the picker closes. Polling can never catch that
# reliably, and a native dialog blocks WebDriver while it is open, so the one
# instant worth sampling is the one instant unavailable.
#
# A MutationObserver sidesteps the timing problem entirely: it records the
# element as it is ADDED and again as it is REMOVED, with full attributes both
# times, into a log this tool drains later. The element may live for 200ms; the
# record of it is permanent.
#
# Armed before the Add-media click. Re-armable and idempotent.

OBSERVER_JS = r"""
if (!window.__harvestLog) { window.__harvestLog = []; }
if (window.__harvestObs) { try { window.__harvestObs.disconnect(); } catch (e) {} }

function snapEl(el, action) {
  const a = {};
  try {
    for (const at of el.attributes) {
      a[at.name] = at.value.length > 300 ? at.value.slice(0,300) + "..." : at.value;
    }
  } catch (e) {}
  let path = "";
  try {
    let n = el, parts = [];
    while (n && n.nodeType === 1 && parts.length < 6) {
      let seg = n.tagName.toLowerCase();
      if (n.id) seg += "#" + n.id;
      else if (n.className && typeof n.className === "string" && n.className.trim())
        seg += "." + n.className.trim().split(/\s+/).slice(0,3).join(".");
      parts.unshift(seg);
      n = n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
    }
    path = parts.join(" > ");
  } catch (e) {}
  let vis = null;
  try {
    const cs = getComputedStyle(el);
    vis = {rects: el.getClientRects().length, display: cs.display,
           visibility: cs.visibility, opacity: cs.opacity,
           offsetParent: el.offsetParent !== null};
  } catch (e) {}
  window.__harvestLog.push({
    action: action, at: Date.now(), t: Math.round(performance.now()),
    tag: el.tagName ? el.tagName.toLowerCase() : "?",
    attrs: a, path: path, vis: vis
  });
}

function scanNode(node, action) {
  if (!node || node.nodeType !== 1) return;
  try {
    if (node.matches && node.matches("input[type=file]")) snapEl(node, action);
    if (node.querySelectorAll) {
      for (const el of node.querySelectorAll("input[type=file]")) snapEl(el, action);
    }
  } catch (e) {}
}

window.__harvestObs = new MutationObserver(function (muts) {
  for (const m of muts) {
    for (const n of m.addedNodes) scanNode(n, "added");
    for (const n of m.removedNodes) scanNode(n, "removed");
    if (m.type === "attributes" && m.target && m.target.matches &&
        m.target.matches("input[type=file]")) {
      snapEl(m.target, "attr:" + m.attributeName);
    }
  }
});
window.__harvestObs.observe(document, {
  childList: true, subtree: true, attributes: true,
  attributeFilter: ["type", "accept", "multiple", "class", "style", "hidden"]
});

// Shadow roots are separate trees; observing `document` does not cover them.
window.__harvestShadowObs = window.__harvestShadowObs || [];
for (const h of document.querySelectorAll("*")) {
  if (!h.shadowRoot) continue;
  try {
    const o = new MutationObserver(function (muts) {
      for (const m of muts) {
        for (const n of m.addedNodes) scanNode(n, "added:shadow");
        for (const n of m.removedNodes) scanNode(n, "removed:shadow");
      }
    });
    o.observe(h.shadowRoot, {childList: true, subtree: true});
    window.__harvestShadowObs.push(o);
  } catch (e) {}
}

// Anything already present at arming time, so a pre-existing input is not
// mistaken for "never existed".
for (const el of document.querySelectorAll("input[type=file]")) snapEl(el, "present-at-arm");
return window.__harvestLog.length;
"""

DRAIN_JS = ("var l = window.__harvestLog || []; window.__harvestLog = []; return l;")


# --- Probing ----------------------------------------------------------------
#
# One execute_script round trip per probe. During the stage-5 burst a probe must
# finish well inside the 250ms tick, and per-element Selenium calls (a round trip
# each) would not - they would also go stale as the DOM re-renders mid-upload.
#
# The probe PIERCES SHADOW ROOTS. driver.page_source does not serialize them and
# document.querySelectorAll does not cross them, so a composer living in a shadow
# root would look exactly like no composer at all.
#
# SIGNALS ARE SCOPED to the composer/media-editor dialog. Run 2 computed them
# document-wide and the feed behind the modal - dozens of images, a video with
# its own role=progressbar - swamped every composer-internal change, so the
# burst fingerprint never moved during the upload. Scope resolution deliberately
# does NOT require a contenteditable: during media editing the text editor is
# gone, which is exactly the window that matters.

PROBE_JS = r"""
function deepAll(sel, root) {
  const out = [];
  function walk(r) {
    let found;
    try { found = r.querySelectorAll(sel); } catch (e) { return; }
    for (const el of found) out.push(el);
    let hosts;
    try { hosts = r.querySelectorAll("*"); } catch (e) { return; }
    for (const h of hosts) if (h.shadowRoot) walk(h.shadowRoot);
  }
  walk(root || document);
  return out;
}
function inShadow(el) {
  let n = el.parentNode;
  while (n) {
    if (n.nodeType === 11 && n.host) return true;
    n = n.parentNode || n.host;
  }
  return false;
}
function vis(el) {
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    rects: el.getClientRects().length,
    display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
    w: Math.round(r.width), h: Math.round(r.height),
    offsetParent: el.offsetParent !== null
  };
}
function rec(el) {
  const a = {};
  for (const at of el.attributes) {
    a[at.name] = at.value.length > 180 ? at.value.slice(0,180) + "..." : at.value;
  }
  const t = (el.innerText || el.textContent || "").trim();
  return {
    tag: el.tagName.toLowerCase(),
    attrs: a,
    text: t.length > 60 ? "[TEXT:" + t.length + "chars]" : t,
    vis: vis(el),
    shadow: inShadow(el)
  };
}

// EVERY dialog, described - not just the first one. The idle feed carries
// placeholder dialogs labelled "Modal Window" that hold nothing.
const allDialogs = deepAll("[role=dialog]");
const dialogs = allDialogs.map(d => ({
  label: d.getAttribute("aria-label") || "",
  cls: (d.className || "").toString().slice(0, 120),
  buttons: d.querySelectorAll("button").length,
  editors: d.querySelectorAll("[contenteditable=true],textarea").length,
  fileInputs: d.querySelectorAll("input[type=file]").length,
  imgs: d.querySelectorAll("img,video").length,
  visible: d.getClientRects().length > 0
}));

// SCOPE RESOLUTION. A dialog counts as the composer if it holds an editor OR
// any of the composer/media-editor markers seen in run 2. Not requiring an
// editor is the point: during media editing there isn't one.
const MARKERS = ".media-editor__layout-container,.media-editor-content-preview__container," +
                ".share-creation-state__preview-container,.share-box-footer__primary-btn," +
                ".share-actions__primary-action,.media-modifiers-drag-and-drop__dropzone," +
                "[contenteditable=true]";
let scope = null, scopeWhy = "none";
for (const d of allDialogs) {
  if (d.getClientRects().length === 0) continue;
  let hit = false;
  try { hit = !!d.querySelector(MARKERS); } catch (e) {}
  if (hit) {
    if (!scope || d.querySelectorAll("*").length > scope.querySelectorAll("*").length) {
      scope = d; scopeWhy = "dialog-with-composer-markers";
    }
  }
}
if (!scope) {
  const m = document.querySelector(".media-editor__layout-container,.share-creation-state");
  if (m) { scope = m.closest("[role=dialog]") || m; scopeWhy = "media-editor-fallback"; }
}
const S = scope || document;
const scoped = (sel) => deepAll(sel, S);

const editors = deepAll("[contenteditable=true],textarea").filter(
  e => e.getClientRects().length > 0);
const fileInputs = deepAll("input[type=file]").map(rec);

const interactiveSel = [
  "button", "[role=button]", "input", "[role=progressbar]", "[aria-busy]",
  "img", "video", "[class*=progress]", "[class*=spinner]", "[class*=loading]",
  "[class*=upload]", "[class*=media]", "[class*=image]", "[class*=preview]",
  "[class*=attach]", "[data-test-icon]"
].join(",");
const interactive = deepAll(interactiveSel).map(rec);

// --- SCOPED completion-signal candidates ---
// Run 2 proved the preview src is a `data:` URI, so "blob -> CDN host" is NOT
// the signal. These are what actually move inside the dialog.
const scopedImgs = scoped("img,video").map(e => {
  const s = e.getAttribute("src") || "";
  return s.startsWith("blob:") ? "blob:" : (s.startsWith("data:") ? "data:" :
         (s ? (s.split("/")[2] || "other") : "empty"));
});
const postBtns = scoped("button")
  .filter(b => (b.innerText||"").trim().toLowerCase() === "post")
  .map(b => ({disabled: b.disabled, ariaDisabled: b.getAttribute("aria-disabled"),
              visible: b.getClientRects().length > 0}));
const nextBtns = scoped("button")
  .filter(b => {
    const t = (b.innerText||"").trim().toLowerCase();
    return t === "next" || t === "done";
  })
  .map(b => ({text: (b.innerText||"").trim(), disabled: b.disabled,
              visible: b.getClientRects().length > 0}));

return {
  url: location.href,
  title: document.title,
  dialogs: dialogs,
  dialogCount: dialogs.length,
  scopeFound: !!scope,
  scopeWhy: scopeWhy,
  scopeCls: scope ? (scope.className || "").toString().slice(0, 120) : null,
  scopeElementCount: scope ? scope.querySelectorAll("*").length : null,
  composerDetected: editors.length > 0,
  triggerPresent: deepAll("[aria-label*='Start a post']").length > 0,
  editorCount: editors.length,
  fileInputCount: fileInputs.length,
  fileInputs: fileInputs,
  interactiveCount: interactive.length,
  interactive: interactive,
  liveObserverEvents: (window.__harvestLog || []).length,
  signals: {
    // every one of these is SCOPED to the composer dialog
    progressbars: scoped("[role=progressbar]").length,
    ariaBusy: scoped("[aria-busy=true]").length,
    spinnerish: scoped("[class*=spinner],[class*=loading],[class*=progress]").length,
    imgSrcKinds: scopedImgs,
    postButtons: postBtns,
    nextButtons: nextBtns,
    previewContainers: scoped(".share-creation-state__preview-container").length,
    mediaEditor: scoped(".media-editor__layout-container").length,
    imgCount: scoped("img,video").length,
    scopedElements: scope ? scope.querySelectorAll("*").length : 0
  }
};
"""

# Shadow roots are invisible to page_source, so their markup is collected
# separately and appended to the dump. Without this the HTML artifact would be
# silently incomplete for any composer that renders in a shadow root.
SHADOW_HTML_JS = r"""
const out = [];
function walk(root, path) {
  let hosts;
  try { hosts = root.querySelectorAll("*"); } catch (e) { return; }
  for (const h of hosts) {
    if (!h.shadowRoot) continue;
    const id = path + " > " + h.tagName.toLowerCase() + (h.id ? "#" + h.id : "");
    try { out.push({host: id, html: h.shadowRoot.innerHTML}); } catch (e) {}
    walk(h.shadowRoot, id);
  }
}
walk(document, "document");
return out;
"""


def probe(driver):
    data = driver.execute_script(PROBE_JS)
    # Selenium's own is_displayed() for the file inputs, which is the check any
    # naive visibility guard would apply. Cheap: there are only ever a few.
    # This only sees LIGHT-dom inputs; the JS count above sees both, and a
    # disagreement between the two is itself a finding.
    try:
        found = driver.find_elements(By.CSS_SELECTOR, "input[type=file]")
        data["seleniumFileInputCount"] = len(found)
        for i, el in enumerate(found):
            if i < len(data.get("fileInputs", [])):
                try:
                    data["fileInputs"][i]["selenium_is_displayed"] = el.is_displayed()
                    data["fileInputs"][i]["selenium_enabled"] = el.is_enabled()
                except Exception as exc:
                    data["fileInputs"][i]["selenium_is_displayed"] = "error: %r" % (exc,)
    except Exception:
        pass
    return data


def signature(p):
    """Compact fingerprint used to detect that the DOM actually changed.

    Reads the SCOPED signals, so feed churn behind the modal no longer masks the
    composer-internal transition the burst exists to catch.
    """
    s = p.get("signals", {})
    return json.dumps({
        "fi": p.get("fileInputCount"),
        "ic": p.get("interactiveCount"),
        "pb": s.get("progressbars"),
        "ab": s.get("ariaBusy"),
        "sp": s.get("spinnerish"),
        "img": s.get("imgSrcKinds"),
        "post": s.get("postButtons"),
        "next": s.get("nextButtons"),
        "prev": s.get("previewContainers"),
        "med": s.get("mediaEditor"),
        "sel": s.get("scopedElements"),
    }, sort_keys=True)


def summarize(p):
    s = p.get("signals", {})
    fi = p.get("fileInputs", [])
    hidden = [f for f in fi if f.get("selenium_is_displayed") is False]
    return ("composer=%s trigger=%s dialogs=%s scope=%s file_inputs=%d(hidden:%d) "
            "progress=%s busy=%s spin=%s preview=%s mediaEd=%s img=%s post=%s next=%s"
            % (p.get("composerDetected"), p.get("triggerPresent"),
               p.get("dialogCount"), p.get("scopeFound"), len(fi), len(hidden),
               s.get("progressbars"), s.get("ariaBusy"), s.get("spinnerish"),
               s.get("previewContainers"), s.get("mediaEditor"),
               s.get("imgSrcKinds"), s.get("postButtons"), s.get("nextButtons")))


# --- Capture ----------------------------------------------------------------

class Harvest:
    def __init__(self, driver, extra, outdir):
        self.driver, self.extra, self.outdir = driver, extra, outdir
        self.manifest = []
        self._last_dom_hash = None
        self._last_stage = None
        _os.makedirs(outdir, exist_ok=True)

    # -- the file-input observer --
    def arm_observer(self):
        try:
            n = self.driver.execute_script(OBSERVER_JS)
            self.note(kind="note", action="observer_armed", preexisting_events=n)
            return True
        except Exception as exc:
            self.note(kind="note", action="observer_arm_failed", error=repr(exc))
            return False

    def drain_observer(self):
        try:
            return self.driver.execute_script(DRAIN_JS) or []
        except Exception:
            return []

    def _collect_html(self):
        """page_source PLUS every shadow root's markup, which it omits."""
        html = self.driver.page_source
        try:
            shadows = self.driver.execute_script(SHADOW_HTML_JS) or []
        except Exception:
            shadows = []
        if shadows:
            parts = [html, "\n<!-- ==== SHADOW ROOTS (not in page_source) ==== -->\n"]
            for sh in shadows:
                parts.append("\n<section data-shadow-host=\"%s\">\n%s\n</section>\n"
                             % (sh.get("host", "?"), sh.get("html", "")))
            html = "".join(parts)
        return html, len(shadows)

    def snapshot(self, stage, label, write_html=True):
        stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
        base = "composer_s%d_%s_%s" % (stage, label, stamp)
        rec = {"stage": stage, "label": label, "at": datetime.now().isoformat()}
        try:
            p = probe(self.driver)
            rec["probe"] = p
            rec["summary"] = summarize(p)
        except Exception as exc:
            rec["probe_error"] = repr(exc)

        events = self.drain_observer()
        if events:
            rec["file_input_events"] = events
            print("  file-input events since last drain: %d" % len(events))
            for e in events[:4]:
                print("     %s %s accept=%r rects=%s"
                      % (e.get("action"), e.get("path", "")[:60],
                         (e.get("attrs") or {}).get("accept"),
                         (e.get("vis") or {}).get("rects")))

        if write_html:
            try:
                raw, n_shadow = self._collect_html()
                dom_hash = hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()
                rec["dom_md5"] = dom_hash
                rec["shadow_roots"] = n_shadow
                # THE CHECK THAT WOULD HAVE SAVED THE FIRST RUN.
                if self._last_dom_hash == dom_hash:
                    rec["identical_to_previous"] = self._last_stage
                    print("\n  *** WARNING: the DOM is BYTE-IDENTICAL to stage %s."
                          % self._last_stage)
                    print("      Nothing you did reached the browser this tool is")
                    print("      watching. Check you are driving the SAME window.")
                    print("      This capture is almost certainly worthless.\n")
                self._last_dom_hash, self._last_stage = dom_hash, stage

                scrubbed, residue = scrub_html(raw, self.extra)
                path = _os.path.join(self.outdir, base + "_dump.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(scrubbed)
                rec["html"] = path
                rec["residue"] = residue
                rec["html_chars"] = len(scrubbed)
            except Exception as exc:
                rec["html_error"] = repr(exc)

        self.manifest.append(rec)
        return rec

    def burst(self, stage, label, seconds, interval):
        """Probe repeatedly, writing HTML only when the DOM signature changes.

        Writing every tick would be gigabytes and would itself slow the loop past
        the interval. Writing on CHANGE captures the pending->complete transition,
        which is the thing Phase 1 needs, without the noise. The signature is now
        SCOPED, so a change means a change inside the composer.
        """
        print("\n  bursting %ss @ %dms - watching the COMPOSER for change"
              % (seconds, int(interval * 1000)))
        end, last, n, changes = time.time() + seconds, None, 0, 0
        while time.time() < end:
            t0 = time.time()
            try:
                p = probe(self.driver)
                sig = signature(p)
                n += 1
                if sig != last:
                    changes += 1
                    rec = {"stage": stage, "label": "%s_change%d" % (label, changes),
                           "at": datetime.now().isoformat(), "probe": p,
                           "summary": summarize(p), "burst_tick": n}
                    ev = self.drain_observer()
                    if ev:
                        rec["file_input_events"] = ev
                    try:
                        raw, _ = self._collect_html()
                        scrubbed, residue = scrub_html(raw, self.extra)
                        path = _os.path.join(
                            self.outdir,
                            "composer_s%d_%s_chg%02d_dump.html"
                            % (stage, label, changes))
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(scrubbed)
                        rec["html"] = path
                        rec["residue"] = residue
                    except Exception as exc:
                        rec["html_error"] = repr(exc)
                    self.manifest.append(rec)
                    print("    [%3d] CHANGE #%d: %s" % (n, changes, summarize(p)))
                    last = sig
            except Exception as exc:
                print("    [%3d] probe error: %r" % (n, exc))
            time.sleep(max(0, interval - (time.time() - t0)))
        print("  burst done: %d probes, %d distinct states" % (n, changes))
        if changes <= 1:
            print("  *** WARNING: the composer never changed during the burst.")
            print("      Either no upload happened in THIS window, or it")
            print("      finished before the burst began. Signals are scoped now,")
            print("      so feed churn is not the explanation.")

    def note(self, **kw):
        """Record a fact that is not a snapshot (e.g. the send_keys outcome).

        Run 1 printed the attach result to the console and stored it nowhere, so
        afterwards there was no record of whether the mechanism had worked - the
        single most important thing the run was meant to answer.
        """
        rec = {"at": datetime.now().isoformat()}
        rec.setdefault("kind", "note")
        rec.update(kw)
        self.manifest.append(rec)
        return rec

    def write_manifest(self):
        """Scrub, then write. Run 2 wrote this raw and it held 228 licdn URLs."""
        path = _os.path.join(self.outdir, "composer_media_manifest.json")
        clean = scrub_obj(self.manifest, self.extra)
        blob = json.dumps(clean, indent=2, ensure_ascii=False)
        with open(path, "w", encoding="utf-8") as f:
            f.write(blob)
        return path, residue_of(blob)


STAGES = [
    (1, "feed_baseline", "Feed loaded, composer CLOSED.",
     "Just be on the feed. Do not click anything."),
    (2, "composer_open", "Composer OPEN and EMPTY.",
     "Click 'Start a post' IN THE BROWSER WINDOW THIS TOOL OPENED. "
     "Wait for the modal. Do NOT type yet."),
    (3, "text_typed", "Text typed, no image yet.",
     "Type a short line, e.g. 'Phase 1 harvest test'. Note whether Post is enabled."),
    # Stage 4 no longer asks anyone to touch the media button. Run 2's ESC
    # instruction is what destroyed the file input: this snapshot is now the
    # BEFORE picture, taken with the observer already armed, and the click
    # happens in the stage-5 prompt where the observer is watching for it.
    (4, "pre_media", "Composer ready, media button NOT yet clicked.",
     "Do NOT click the media button yet - just confirm the composer is open "
     "with your text in it. The observer is armed at this point."),
]


def _window_report(driver):
    """The trap that voided run 1: more than one window, the human drives the
    wrong one, and every snapshot is of an idle feed. Returned as data so it
    lands in the manifest - run 2 could not answer 'how many windows?' because
    this was printed and never recorded."""
    info = {}
    try:
        info["window_count"] = len(driver.window_handles)
    except Exception as exc:
        info["window_count_error"] = repr(exc)
    try:
        info["current_title"] = driver.title
        info["current_url"] = driver.current_url
    except Exception:
        pass
    print("browser windows/tabs under WebDriver control: %s"
          % info.get("window_count", "?"))
    if info.get("window_count", 1) > 1:
        print("  *** MORE THAN ONE. This tool only ever watches the CURRENT one.")
        print("  *** Close the extras, or you will drive a window it cannot see.")
    print("  current title: %r" % (info.get("current_title"),))
    print("  Drive THAT window - the one WebDriver just opened. If another")
    print("  Chrome is open on the same profile, its clicks are invisible here.")
    return info


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="dev", help="profile name (default: dev)")
    ap.add_argument("--scrub-extra", action="append", default=[],
                    help="extra literal to redact, e.g. your display name (repeatable)")
    ap.add_argument("--outdir", default=OUT_DIR)
    ap.add_argument("--burst-seconds", type=float, default=15.0)
    ap.add_argument("--burst-interval", type=float, default=0.25)
    ap.add_argument("--attach-via-send-keys", metavar="IMAGE",
                    help="OPTIONAL. At stage 5, attach this image by send_keys "
                         "to the hidden file input instead of using the OS "
                         "picker. The ONLY action this tool ever takes, and "
                         "exactly the technique Phase 1b will use - so it "
                         "proves the mechanism during the harvest. Never "
                         "clicks Post.")
    args = ap.parse_args()

    # Fail before spending a live session, not halfway through one.
    if args.attach_via_send_keys and not _os.path.isfile(args.attach_via_send_keys):
        print("image not found: %s" % args.attach_via_send_keys)
        return 2

    print("Staged live harvest of the composer media-upload flow (Phase 1a).")
    print("\nprofile=%s  outdir=%s" % (args.profile, args.outdir))
    print("Observer only. It never clicks Post and cannot publish.\n")
    if not args.scrub_extra:
        print("  ! no --scrub-extra given. Pass your display name so the composer's")
        print("    author chip is redacted, e.g. --scrub-extra \"Jane Doe\"\n")

    driver, profile = pm.create_driver(args.profile)
    h = Harvest(driver, args.scrub_extra, args.outdir)
    try:
        if not pm.login(driver, profile):
            print("Login failed - aborting before any capture.")
            return 2
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(4)
        h.note(action="window_report", **_window_report(driver))

        for stage, label, what, todo in STAGES:
            while True:
                print("\n" + "=" * 72)
                print("STAGE %d - %s" % (stage, what))
                print("  DO THIS: %s" % todo)
                input("  press Enter when ready to snapshot > ")
                # Arm BEFORE the media click, which happens in stage 5's prompt.
                if stage == 3:
                    if h.arm_observer():
                        print("  file-input observer ARMED (survives the picker)")
                rec = h.snapshot(stage, label)
                print("  captured: %s" % rec.get("summary", rec.get("probe_error")))
                print("  html: %s" % rec.get("html", rec.get("html_error")))

                p = rec.get("probe") or {}
                # GATE: from stage 2 on, a composer must actually be visible.
                if stage >= 2 and not p.get("composerDetected"):
                    print("\n  *** NO COMPOSER FOUND - no visible editor anywhere,")
                    print("      including inside shadow roots.")
                    if p.get("triggerPresent"):
                        print("      NOTE: 'Start a post' stays in the DOM behind the")
                        print("      modal, so it alone does not mean 'closed' - the")
                        print("      zero editor count is what does.")
                    print("      Almost always: you are driving a different window.")
                    again = input("  [r]etry this stage, or [c]ontinue anyway? > ")
                    if again.strip().lower().startswith("r"):
                        continue
                break

        print("\n" + "=" * 72)
        print("STAGE 5 - CLICK ADD MEDIA, PICK THE FILE, THEN BURST")
        print("  The observer is armed, so the file input is recorded even if it")
        print("  exists only while the picker is open. You do NOT need to press")
        print("  ESC any more - that is what destroyed it on the last run.")
        if args.attach_via_send_keys:
            path = _os.path.abspath(args.attach_via_send_keys)
            print("\n  --attach-via-send-keys was given, but run 2 showed no file")
            print("  input exists before the media button is clicked, so the")
            print("  attach is attempted AFTER you click it.")
            print("  DO THIS: click 'Add media'. If a native picker opens, leave")
            print("  it open and press Enter here - this tool will try send_keys.")
            input("  press Enter once you have clicked Add media > ")
            found = driver.find_elements(By.CSS_SELECTOR, "input[type=file]")
            displayed = []
            for el in found:
                try:
                    displayed.append(el.is_displayed())
                except Exception:
                    displayed.append("error")
            print("    %d input[type=file] found; displayed=%s" % (len(found), displayed))
            if not found:
                print("    ! none live right now - the observer may still have caught")
                print("      it. Pick the file by hand in the picker.")
                h.note(action="send_keys_attach", outcome="no_file_input_found",
                       image=path)
                input("    press Enter the moment you confirm the file > ")
            else:
                try:
                    found[0].send_keys(path)
                    print("    send_keys OK -> a hidden input DOES accept a path")
                    h.note(action="send_keys_attach", outcome="ok", image=path,
                           inputs_found=len(found), inputs_displayed=displayed)
                except Exception as exc:
                    print("    send_keys FAILED: %r" % (exc,))
                    h.note(action="send_keys_attach", outcome="failed",
                           error=repr(exc), image=path,
                           inputs_found=len(found), inputs_displayed=displayed)
                    input("    press Enter the moment you confirm the file > ")
        else:
            print("\n  DO THIS: click 'Add media', choose your image, and confirm.")
            print("  Press Enter the INSTANT the picker closes, so the burst")
            print("  catches the upload while it is still in flight.")
            input("  press Enter the moment you confirm the file > ")
        h.burst(5, "upload_inflight", args.burst_seconds, args.burst_interval)

        print("\n" + "=" * 72)
        print("STAGE 6 - Upload COMPLETE, preview shown.")
        print("  DO THIS: wait until the preview looks settled. If LinkedIn shows")
        print("  a Next/Done step, snapshot BEFORE clicking it.")
        input("  press Enter when ready to snapshot > ")
        rec = h.snapshot(6, "upload_complete")
        print("  captured: %s" % rec.get("summary", rec.get("probe_error")))

        print("\n" + "=" * 72)
        print("STAGE 7 - Back in composer, image ATTACHED, ready to post.")
        print("  DO THIS: click Next/Done if one exists, so you are back in the")
        print("  composer with the image attached. DO NOT CLICK POST.")
        input("  press Enter when ready to snapshot > ")
        rec = h.snapshot(7, "image_attached")
        print("  captured: %s" % rec.get("summary", rec.get("probe_error")))

        path, mres = h.write_manifest()
        print("\n" + "=" * 72)
        print("manifest: %s (scrubbed)" % path)
        print("artifacts: %d records in %s/" % (len(h.manifest), args.outdir))

        evs = sum(len(r.get("file_input_events", [])) for r in h.manifest)
        print("FILE-INPUT EVENTS RECORDED: %d %s"
              % (evs, "" if evs else "  <-- still none; the input never appeared"))

        snaps = [r for r in h.manifest if "dom_md5" in r]
        distinct = len({r["dom_md5"] for r in snaps})
        print("distinct DOM states across %d snapshots: %d" % (len(snaps), distinct))
        if snaps and distinct < max(2, len(snaps) // 2):
            print("*** THIS CAPTURE IS PROBABLY VOID: too few distinct states.")
            print("*** Re-run, driving the window WebDriver opened.")

        for key, label in (("remaining_urn_like", "urn-like"),
                           ("remaining_email_like", "email-like"),
                           ("remaining_licdn_like", "licdn-like")):
            tot = sum(r.get("residue", {}).get(key, 0) for r in h.manifest)
            flag = "  <-- CHECK" if tot else ""
            print("residue %-12s html: %d%s   manifest: %d"
                  % (label, tot, flag, mres.get(key, 0)))
        print("\nDISCARD THE DRAFT in the browser when you are done (do not post).")
        return 0
    finally:
        try:
            input("\npress Enter to close the browser > ")
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
