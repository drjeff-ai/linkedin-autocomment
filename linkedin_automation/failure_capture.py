"""Screenshot-on-failure diagnostics for the LinkedIn browser scripts.

When a browser interaction fails (feed won't load, a Connect/Send click misses,
the shadow-DOM "Send without a note" modal never appears, a selector goes BROKEN),
a log line and an HTML dump are hard to read. A PNG of the exact page state is
far faster to diagnose. ``capture_failure`` saves that PNG plus a small context
sidecar (URL + title) and, optionally, the page source — so a failure yields both
VISUAL and DOM state.

Design contract (see .dev/VISION_AUDIT.md — recommended scope (b)):
- Zero new dependencies: only Selenium's built-in ``save_screenshot`` / page_source.
- NEVER throws. A diagnostic must not crash the thing it's diagnosing, so every
  step is guarded and total failure returns None.
- Returns the saved PNG path (or None) so callers can log/surface it.
"""

import os
import re
import json
import logging
from datetime import datetime
from typing import Optional

from . import profile_manager as pm

logger = logging.getLogger(__name__)

_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_label(label: str) -> str:
    """Make ``label`` safe for a filename (alnum/._- only)."""
    cleaned = _LABEL_RE.sub("_", (label or "failure").strip()).strip("_")
    return cleaned or "failure"


def capture_failure(driver, label: str, profile_name: str = None,
                    page_source: bool = True) -> Optional[str]:
    """Capture a failure screenshot (+ context sidecar, + page source) for later review.

    Saves ``failure_<label>_<timestamp>.png`` under ``data/<profile>/failures/``,
    a ``.json`` sidecar with the current URL/title, and (when ``page_source``) a
    ``.html`` dump. Returns the PNG path on success, else None. Never raises.

    Args:
        driver: the Selenium WebDriver (may be dead — handled gracefully).
        label: short slug describing the failure (e.g. "feed_no_posts",
            "send_modal", "connect_click"). Sanitized for the filename.
        profile_name: profile whose ``data/<profile>/failures/`` dir to write to
            (defaults to the resolved default profile).
        page_source: also dump the DOM (page_source) alongside the screenshot.
    """
    try:
        safe = _safe_label(label)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"failure_{safe}_{timestamp}"

        try:
            out_dir = pm.get_data_dir(profile_name, "failures")
        except Exception:
            logger.debug("capture_failure: could not resolve failures dir", exc_info=True)
            return None

        png_path = os.path.join(out_dir, base + ".png")
        json_path = os.path.join(out_dir, base + ".json")
        html_path = os.path.join(out_dir, base + ".html")

        # ── Context sidecar (URL + title): each read guarded so a dead driver
        #    still yields whatever context is available. ──
        context = {"label": safe, "timestamp": timestamp}
        for key, getter in (("url", lambda: driver.current_url),
                            ("title", lambda: driver.title)):
            try:
                context[key] = getter()
            except Exception:
                context[key] = None

        # ── Screenshot (the primary artifact) ──
        saved_png = None
        try:
            if driver.save_screenshot(png_path):
                saved_png = png_path
                context["screenshot"] = os.path.basename(png_path)
        except Exception:
            logger.debug("capture_failure: screenshot failed", exc_info=True)

        # ── Page source (DOM state), best-effort ──
        if page_source:
            try:
                html = driver.page_source or ""
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)
                context["page_source"] = os.path.basename(html_path)
            except Exception:
                logger.debug("capture_failure: page_source dump failed", exc_info=True)

        # ── Write the sidecar last so it references whatever succeeded above ──
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(context, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.debug("capture_failure: sidecar write failed", exc_info=True)

        if saved_png:
            logger.info(f"Failure screenshot saved: {saved_png}")
            return saved_png

        # No PNG (dead driver / headless quirk) but context may still be useful.
        logger.warning(
            f"Failure capture for '{safe}': screenshot unavailable; context at {json_path}"
        )
        return None

    except Exception:
        # The absolute backstop — a diagnostic must never crash its caller.
        logger.debug("capture_failure: unexpected error (ignored)", exc_info=True)
        return None


# ─── PII scrubbing for captured evidence ─────────────────────────────────────
#
# A failure capture is read by a human and may be pasted into an issue, a chat
# or a commit. `data/` is gitignored, so these files do not reach the repo on
# their own - but the moment someone quotes one, whatever it holds travels with
# it. Identity is scrubbed at the WRITE boundary so a field added later is
# covered by default, the same rule the X dump scrubber follows.

_PII_PATTERNS = (
    # A LinkedIn vanity slug: the one thing in this DOM that names a real human.
    (re.compile(r"(/in/)[^/\s\"'?)]+"), r"\1<redacted-slug>"),
    (re.compile(r"(/company/)[^/\s\"'?)]+"), r"\1<redacted-company>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<redacted-email>"),
    # Activity/share URNs and any other long id run.
    (re.compile(r"\b\d{9,}\b"), "<redacted-id>"),
)


def scrub_pii(value):
    """Redact identity from a string (or recursively from a dict/list).

    Deliberately blunt: over-redacting a diagnostic costs a little context,
    while under-redacting puts a real person's profile into a file someone may
    paste somewhere. The SHAPE survives - a reader can still see that a slug or
    an id was present, and where.
    """
    if isinstance(value, str):
        out = value
        for pattern, replacement in _PII_PATTERNS:
            out = pattern.sub(replacement, out)
        return out
    if isinstance(value, dict):
        return {k: scrub_pii(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_pii(v) for v in value]
    return value


def capture_submit_state(driver, label, profile_name=None, extra=None,
                         submit_selectors=(), box_selectors=()):
    """Capture the evidence a failed comment-submit actually needs.

    `capture_failure` saves a screenshot and the whole page. That is a third of
    a megabyte to read through when the question is narrow: WAS THERE A SUBMIT
    BUTTON, and could it be clicked? This adds the answer directly - every
    candidate submit control with its text, aria-label, disabled state and
    dimensions - so the next dispatch can fix the submit from evidence instead
    of guessing.

    Returns the sidecar path, or None. Never raises: a diagnostic that breaks
    the run it is diagnosing is worse than no diagnostic.
    """
    try:
        from selenium.webdriver.common.by import By
    except Exception:
        return None

    def _probe(selectors, kind):
        found = []
        for selector in selectors or ():
            try:
                by = By.XPATH if selector.strip().startswith(("/", "(")) else By.CSS_SELECTOR
                for el in driver.find_elements(by, selector):
                    try:
                        found.append({
                            "kind": kind,
                            "selector": selector,
                            "tag": el.tag_name,
                            "text": (el.text or "")[:120],
                            "aria_label": el.get_attribute("aria-label"),
                            "disabled": el.get_attribute("disabled"),
                            "aria_disabled": el.get_attribute("aria-disabled"),
                            "class": (el.get_attribute("class") or "")[:200],
                            "displayed": el.is_displayed(),
                            "enabled": el.is_enabled(),
                            "size": el.size,
                        })
                    except Exception:
                        # One stale element must not lose the others.
                        found.append({"kind": kind, "selector": selector,
                                      "error": "element went stale while reading"})
            except Exception as exc:
                found.append({"kind": kind, "selector": selector,
                              "error": str(exc)[:200]})
        return found

    context = dict(extra or {})
    context["label"] = label
    context["timestamp"] = datetime.now().strftime("%Y%m%d_%H%M%S")
    for key, getter in (("url", lambda: driver.current_url),
                        ("title", lambda: driver.title)):
        try:
            context[key] = getter()
        except Exception:
            context[key] = None
    context["submit_candidates"] = _probe(submit_selectors, "submit")
    context["comment_box_candidates"] = _probe(box_selectors, "comment_box")

    try:
        out_dir = pm.get_data_dir(profile_name, "failures")
    except Exception:
        logger.debug("capture_submit_state: no failures dir", exc_info=True)
        return None

    path = os.path.join(
        out_dir, "failure_%s_%s_submitdom.json"
        % (_safe_label(label), context["timestamp"]))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scrub_pii(context), f, indent=2, ensure_ascii=False)
    except Exception:
        logger.debug("capture_submit_state: write failed", exc_info=True)
        return None
    logger.error("Submit-state evidence written: %s", path)
    return path


def capture_like_state(driver, label, profile_name=None, buttons=(),
                       region=None, extra=None):
    """The Like-miss counterpart of :func:`capture_submit_state`.

    Writes ``failure_<label>_<ts>_likedom.json`` beside the screenshot: every
    button in the post's action-bar region with its text, aria-label,
    aria-pressed, disabled state and size, PII-scrubbed at the write boundary.
    The shape matches ``_submitdom.json`` - a context dict with ``label``,
    ``timestamp``, ``url``, ``title``, the caller's ``extra``, and a list of
    per-element entries - so both captures read the same way.

    ``buttons`` are the elements the caller located (it owns the "where is
    the action bar" question). Returns the path, or None. Never raises.
    """
    entries = []
    for el in buttons or ():
        try:
            entries.append({
                "kind": "action_bar_button",
                "selector": "region:%s" % (region or "unknown"),
                "tag": el.tag_name,
                "text": (el.text or "")[:120],
                "aria_label": el.get_attribute("aria-label"),
                "aria_pressed": el.get_attribute("aria-pressed"),
                "disabled": el.get_attribute("disabled"),
                "aria_disabled": el.get_attribute("aria-disabled"),
                "class": (el.get_attribute("class") or "")[:200],
                "displayed": el.is_displayed(),
                "enabled": el.is_enabled(),
                "size": el.size,
            })
        except Exception:
            entries.append({"kind": "action_bar_button",
                            "error": "element went stale while reading"})

    context = dict(extra or {})
    context["label"] = label
    context["timestamp"] = datetime.now().strftime("%Y%m%d_%H%M%S")
    for key, getter in (("url", lambda: driver.current_url),
                        ("title", lambda: driver.title)):
        try:
            context[key] = getter()
        except Exception:
            context[key] = None
    context["action_bar_region"] = region
    context["action_bar_buttons"] = entries

    try:
        out_dir = pm.get_data_dir(profile_name, "failures")
        path = os.path.join(
            out_dir, "failure_%s_%s_likedom.json"
            % (_safe_label(label), context["timestamp"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scrub_pii(context), f, indent=2, ensure_ascii=False)
    except Exception:
        logger.debug("capture_like_state: write failed", exc_info=True)
        return None
    logger.warning("Like-miss evidence written: %s", path)
    return path
