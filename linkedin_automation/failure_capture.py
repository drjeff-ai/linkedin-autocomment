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
