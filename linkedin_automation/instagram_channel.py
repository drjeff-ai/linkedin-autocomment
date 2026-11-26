"""Instagram as a Buffer channel (I1, D-030, DEC-071(2)).

The declaration is ``channels.INSTAGRAM``; queueing, scheduling, the lock, the
calendar and reconcile are the shared Buffer paths. This module holds the two
things that are Instagram's own:

* **The preflight** a schedule pass runs before anything is sent
  (:func:`preflight_refusal`). It refuses when the profile's overlay has no
  valid ``cadence`` (topup's format) or does not say ``drain: false``, and
  when video-publisher still has Instagram items queued for the profile's
  ``vp_brand``. video-publisher has an Instagram code path but publishes
  nothing through it (no ``publish_log`` row, nothing in ``published/``); its
  queued ``-ig`` items still mean "this app intends to post here", so while
  any is queued the platform refuses. There is no moved marker for
  Instagram: the YouTube marker (``brands: [...]``) is unchanged.
* **Reels from YouTube items** (:func:`from_youtube`, the
  ``instagram-from-youtube`` command): one proposed CSV row per item folder with
  a vertical mp4 and no Instagram row yet, with a caption copied verbatim from
  ``youtube.md``. It never queues; Jeff reviews the CSV and queues it with the
  existing CSV path.

The overlay, ``scheduled_posting.platforms.instagram``::

    {"buffer_channel_id": "...", "identity_slug": "...",
     "buffer_api_key_env": "BUFFER_API_KEY_...", "drain": false,
     "cadence": {"per_day": 1, "windows": {"1": ["afternoon"]}}}
"""

import csv
import io
import os
import re

from . import buffer_client as bc
from . import channels
from . import csv_pipeline as cp
from . import image_host
from . import platforms
from . import profile_manager as pm
from . import youtube_channel as yt

PLATFORM = platforms.INSTAGRAM
CHANNEL = channels.INSTAGRAM
OVERLAY_WHERE = "scheduled_posting.platforms.%s" % PLATFORM
PROPOSAL_NAME = "proposed_reels.csv"
PROPOSAL_COLUMNS = ("date", "time_window", "post_text", "tags",
                    channels.INSTAGRAM_VIDEO_COLUMN, "source_item")
#: The one YouTube-only token kept out of every Instagram caption.
SHORTS_TOKEN_RE = re.compile(r"(?<!\w)#shorts\b", re.IGNORECASE)
CAPTION_FIELD = "caption (draft)"
TAGS_FIELD = "tags"

EXIT_OK = 0
EXIT_REFUSED = 2


class InstagramRefused(RuntimeError):
    """Something this module will not do, with the reason."""


# ── the preflight ───────────────────────────────────────────────────────────

def _overlays(profile_cfg):
    section = (profile_cfg or {}).get("scheduled_posting")
    overlays = section.get("platforms") if isinstance(section, dict) else None
    return overlays if isinstance(overlays, dict) else {}


def overlay_refusal(profile_cfg):
    """Why the Instagram overlay cannot be used, or None. Reads config only."""
    from . import topup  # topup imports csv_pipeline; keep this module light
    overlay = _overlays(profile_cfg).get(PLATFORM)
    if not isinstance(overlay, dict):
        return "the profile has no %s section" % OVERLAY_WHERE
    if "cadence" not in overlay:
        return ("%s has no cadence. Instagram needs one (topup's format) and "
                "invents none" % OVERLAY_WHERE)
    try:
        topup.parse_cadence(overlay["cadence"], OVERLAY_WHERE + ".cadence")
    except topup.TopupRefused as exc:
        return str(exc)
    if overlay.get("drain") is not False:
        return ("%s.drain must be false: nothing drains Instagram yet (got %r)"
                % (OVERLAY_WHERE, overlay.get("drain")))
    return None


def vp_source(profile_cfg):
    """``(vp_root, vp_brand)`` from the profile's YouTube overlay, or None
    when the profile names no video-publisher brand."""
    yt_overlay = _overlays(profile_cfg).get(platforms.YOUTUBE)
    if not isinstance(yt_overlay, dict):
        return None
    brand = str(yt_overlay.get("vp_brand") or "").strip()
    root = str(yt_overlay.get("vp_root") or "").strip()
    if not brand:
        return None
    if not root:
        raise InstagramRefused("%s.vp_brand is %r but vp_root is empty, so "
                               "video-publisher's queue cannot be read"
                               % ("scheduled_posting.platforms.youtube", brand))
    return root, brand


def vp_instagram_queued(vp_root, brand):
    """Names of the items in ``<vp_root>/content/<brand>/queue/`` whose
    manifest says ``platform: instagram``. Read only. A missing vp_root
    refuses (who publishes cannot be known); a brand with no content folder
    has nothing queued; an unreadable manifest refuses."""
    import yaml
    if not os.path.isdir(vp_root):
        raise InstagramRefused("video-publisher root %s is not a folder, so "
                               "its Instagram queue cannot be checked" % vp_root)
    queue = os.path.join(vp_root, "content", brand, "queue")
    if not os.path.isdir(queue):
        return []
    found = []
    for name in sorted(os.listdir(queue)):
        mpath = os.path.join(queue, name, "manifest.yaml")
        if not os.path.isfile(mpath):
            continue
        try:
            with open(mpath, encoding="utf-8") as f:
                manifest = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            raise InstagramRefused("%s is unreadable (%s)"
                                   % (mpath, type(exc).__name__)) from exc
        if isinstance(manifest, dict) and manifest.get("platform") == PLATFORM:
            found.append(name)
    return found


def publisher_refusal(profile_cfg):
    """The one-publisher check: a refusal while video-publisher has Instagram
    items queued for this profile's brand, else None."""
    try:
        source = vp_source(profile_cfg)
        if source is None:
            return None
        root, brand = source
        queued = vp_instagram_queued(root, brand)
    except InstagramRefused as exc:
        return str(exc)
    if queued:
        return ("video-publisher still has %d Instagram item(s) queued for brand "
                "%r (e.g. %s). One publisher per channel: clear them there first"
                % (len(queued), brand, ", ".join(queued[:3])))
    return None


def preflight_refusal(profile_name, profile_cfg=None):
    """``channels.INSTAGRAM.preflight``: the overlay check, then the
    one-publisher check. None means the pass may go on to the identity gate."""
    if not profile_name:
        return "no profile was named, so its Instagram settings cannot be checked"
    cfg = profile_cfg if profile_cfg is not None else pm.get_profile_config(profile_name)
    return overlay_refusal(cfg) or publisher_refusal(cfg)


# ── captions, verbatim from youtube.md ─────────────────────────────────────

def drop_shorts_token(text):
    """``text`` with every ``#shorts`` token removed. Only a line that held
    one has its spacing tidied; every other line is untouched."""
    out = []
    for line in text.split("\n"):
        if SHORTS_TOKEN_RE.search(line):
            line = " ".join(SHORTS_TOKEN_RE.sub("", line).split())
        out.append(line)
    return "\n".join(out).strip()


def item_section_fields(text):
    """The ``- **Field:** value`` bullets of the item's own section: from the
    first ``## `` heading to the next heading of any level. Keys lower-cased,
    continuation lines kept, paragraphs joined by one blank line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    if start is None:
        return {}
    raw = {}
    current = None
    for line in lines[start + 1:]:
        if yt.HEADING_RE.match(line):
            break
        m = yt.FIELD_RE.match(line)
        if m:
            current = m.group(1).strip().lower()
            raw[current] = [m.group(2)] if m.group(2).strip() else []
        elif current is not None:
            raw[current].append(line)
    return {k: "\n\n".join(yt._paragraphs(v)) for k, v in raw.items()}


def caption_from_shorts_section(text):
    """``(post_text, tags, None)`` or ``(None, None, reason)`` for a topfive
    layout: the item section's ``Caption (draft)`` and ``Tags``."""
    fields = item_section_fields(text)
    caption = drop_shorts_token(fields.get(CAPTION_FIELD) or "")
    if not caption:
        return None, None, "youtube.md has no Caption (draft) field"
    return caption, drop_shorts_token(fields.get(TAGS_FIELD) or ""), None


def caption_from_title_sections(text):
    """``(post_text, "", None)`` or ``(None, None, reason)`` for a Teratoma
    layout: the ``## Description`` section minus any ``#shorts`` token."""
    try:
        parsed = yt.parse_title_sections(text)
    except ValueError as exc:
        return None, None, str(exc)
    description = drop_shorts_token(parsed.get("description") or "")
    if not description:
        return None, None, "youtube.md has no ## Description section"
    return description, "", None


CAPTION_BUILDERS = {
    yt.METADATA_SHORTS_SECTION: caption_from_shorts_section,
    yt.METADATA_TITLE_SECTIONS: caption_from_title_sections,
}


# ── instagram-from-youtube ──────────────────────────────────────────────────

def state_path(profile):
    """The Instagram state file, computed so a dry run creates no folder."""
    return os.path.join(pm.DATA_ROOT, profile, PLATFORM, cp.STATE_NAME)


def proposal_path(profile):
    return os.path.join(pm.DATA_ROOT, profile, PLATFORM, PROPOSAL_NAME)


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def queued_videos(profile):
    """Normalised video paths of every Instagram row the profile already has."""
    path = state_path(profile)
    if not os.path.exists(path):
        return set()
    state = cp.PipelineState(path=path, platform=PLATFORM)
    return {_norm(str((rec.get("row") or {}).get(CHANNEL.video_column) or "").strip())
            for rec in state.rows.values()} - {""}


def propose(profile, cfg):
    """``(rows, skips)``: one row per item folder with a vertical mp4, a
    caption and no Instagram row yet, in item order. Writes nothing."""
    builder = CAPTION_BUILDERS.get(cfg.metadata_format)
    if builder is None:
        raise InstagramRefused("no caption rule for metadata_format %r"
                               % cfg.metadata_format)
    names, _messages = yt.find_item_folders(cfg.content_root, cfg.item_prefix)
    have = queued_videos(profile)
    rows, skips = [], []
    for name in names:
        folder = os.path.join(cfg.content_root, name)
        mp4, reason = yt.find_vertical_mp4(folder, cfg.video_suffix)
        if reason:
            skips.append((name, reason))
            continue
        if _norm(mp4) in have:
            skips.append((name, "already has an Instagram row"))
            continue
        try:
            image_host.read_video(mp4, CHANNEL.video_max_bytes)
        except image_host.ImageHostError as exc:
            skips.append((name, str(exc)))
            continue
        md = os.path.join(folder, "youtube.md")
        if not os.path.isfile(md):
            skips.append((name, "no youtube.md"))
            continue
        with open(md, encoding="utf-8-sig", errors="replace") as f:
            post_text, tags, reason = builder(f.read())
        if reason:
            skips.append((name, reason))
            continue
        problem = cp.text_limit_problem(bc.compose_text(post_text, tags), PLATFORM)
        if problem:
            skips.append((name, problem))
            continue
        rows.append({"date": "", "time_window": "", "post_text": post_text,
                     "tags": tags, CHANNEL.video_column: mp4, "source_item": name})
    return rows, skips


def write_proposal(path, rows):
    """The rows as CSV, through ``<path>.tmp`` and a rename."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=PROPOSAL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)


def from_youtube(profile, dry_run=False, out=print):
    """The ``instagram-from-youtube`` command. Returns ``(exit_code, summary)``.

    Proposes rows; never queues them. ``date`` and ``time_window`` are left
    blank for Jeff to fill before he queues the CSV."""
    tag = "instagram-from-youtube %s%s" % (profile, " [dry-run]" if dry_run else "")
    try:
        cfg = yt.youtube_config(profile)
        rows, skips = propose(profile, cfg)
    except (yt.YouTubeRefused, InstagramRefused, cp.RowError) as exc:
        out("%s: REFUSED: %s" % (tag, exc))
        return EXIT_REFUSED, {"refused": str(exc)}
    for row in rows:
        body = bc.compose_text(row["post_text"], row["tags"])
        out("  PROPOSE %s: reel %s, caption %d characters, %d hashtag(s)"
            % (row["source_item"], os.path.basename(row[CHANNEL.video_column]),
               len(body), len(cp.HASHTAG_RE.findall(body))))
    for name, reason in skips:
        out("  SKIP %s: %s" % (name, reason))
    summary = {"proposed": len(rows), "skipped": len(skips), "written": None}
    if dry_run or not rows:
        out("%s: %d proposed, %d skipped; nothing written"
            % (tag, len(rows), len(skips)))
        return EXIT_OK, summary
    path = proposal_path(profile)
    write_proposal(path, rows)
    summary["written"] = path
    out("%s: %d proposed, %d skipped; wrote %s for review. Nothing was queued: "
        "fill date and time_window, then queue it on the instagram platform."
        % (tag, len(rows), len(skips), path))
    return EXIT_OK, summary
