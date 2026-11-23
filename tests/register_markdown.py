"""Parse an engagement register markdown file into PlatformPolicy field values.

Test-side only. The register files are prompt content owned by the strategist
(DEC-005, DEC-061(6)); the app never reads them. A copy of each file is taken
into ``tests/fixtures/engagement/`` at build time, and the golden tests compare
the policy in ``platform_policy.py`` against what this parser reads from that
copy, field by field.

The only transformation is layout. A nested bullet list becomes the bullet
lines the prompt shows (``- item`` per line, numbered items keep their
number), the way LinkedIn's and X's ``sentence_style``, ``opening_styles``
and ``craft_rules`` are laid out. A quoted value is a JSON string literal, so
``\n`` is a newline and ``\\"`` a quote. Nothing is reworded.
"""

import json
import re

TOP_RE = re.compile(r"^- (\w+):\s?(.*)$")
NESTED_RE = re.compile(r"^  (?:- |(\d+)\. )(.*)$")
SECTION_RE = re.compile(r"^## (.+)$")

#: Keys whose value is a bullet list rendered as prompt lines.
BULLET_KEYS = ("sentence_style", "opening_styles", "craft_rules")


def _section_name(heading):
    h = heading.strip().lower()
    if h.startswith("own-video replies"):
        return "reply"
    return h


def parse_sections(text):
    """``{section: {key: raw value or [nested items]}}``. Nested items are
    ``(number or None, text)``."""
    sections = {}
    current = None
    key = None
    for line in text.replace("\r\n", "\n").split("\n"):
        m = SECTION_RE.match(line)
        if m:
            current = sections.setdefault(_section_name(m.group(1)), {})
            key = None
            continue
        if current is None:
            continue
        m = TOP_RE.match(line)
        if m:
            key = m.group(1)
            current[key] = m.group(2) if m.group(2) else []
            continue
        m = NESTED_RE.match(line)
        if m and key is not None and isinstance(current[key], list):
            current[key].append((m.group(1), m.group(2)))
    return sections


def _value(key, raw):
    if isinstance(raw, list):
        if key in BULLET_KEYS:
            return "\n".join(("%s. %s" % (n, t)) if n else ("- " + t) for n, t in raw)
        return tuple(json.loads(t) for _n, t in raw)
    raw = raw.strip()
    if key == "preferred":
        low, high = raw.split("-")
        return (int(low), int(high))
    if key in ("hard_max", "aim_below"):
        return int(re.match(r"\d+", raw).group(0))
    if key == "hard_max_is_postable_limit":
        return {"true": True, "false": False}[raw]
    if key == "bucket_weights":
        return tuple(float(x) for x in raw.split(","))
    if key == "banned_openers":
        return tuple(json.loads("[" + raw + "]"))
    if raw.startswith('"'):
        return json.loads(raw)
    return raw


def parse_register(text):
    """``{"length": {...}, "register": {...}, "reply": {...}}`` of field values."""
    out = {}
    for name, fields in parse_sections(text).items():
        out[name] = {k: _value(k, v) for k, v in fields.items()}
    return out
