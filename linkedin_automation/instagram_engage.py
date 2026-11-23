"""Instagram engagement rules (D-027 E2, PLATFORM_REFACTOR §9).

The register (``platform_policy.policy_for("instagram")``) tells the model how
to write. This module is what is *checked*, as data, before a draft may go
out. Each check returns named reasons; a draft with any reason is refused and
goes back to review (its approval withdrawn, its text kept). Nothing here
talks to Instagram: the adapter (``instagram_adapter.py``) reads the page,
this module judges what it read.

**Config** lives in the profile's ``scheduled_posting.platforms.instagram``
overlay, the same place X's and YouTube's do::

    "instagram": {
        "identity_slug": "<the dev account's handle>",
        "engagement": {"max_followers": 20000, "skip_verified": true}
    }

Unknown keys refuse, as Y1's do. ``identity_slug`` is the account we act as:
the adapter proves it is the signed-in account before any action, the comment
verifier looks for it as the author, and the no-promotion check refuses a
draft that names it. Absent, every draft is refused (the check cannot be
done). ``max_followers`` absent means **every account is skipped** at find:
there is no default threshold, because a guessed one decides whose posts we
comment on. ``skip_verified`` absent means True.

**The draft checks** (:func:`draft_refusals`), each a named reason:

* ``too_long`` - over the register's ``hard_max`` (300 chars). ``empty`` for
  no text.
* ``emoji`` - more than one emoji (:func:`emoji_count`: Unicode emoji blocks;
  a ZWJ sequence, a flag pair or a skin-tone variant is one emoji; text
  symbols such as ©, ™ and arrows are not counted).
* ``hashtag``, ``link``, ``@-mention``, ``promotion phrase``, ``our account``,
  ``banned opener``, as Y1's no-promotion validator does for YouTube.
* ``language`` - the draft is not in the caption's language
  (:func:`detect_language`, a stdlib heuristic; its limits are in its
  docstring). Only a confident disagreement refuses.
* ``near_duplicate`` - too close to a comment we already made, or have
  approved, to the same author (:func:`similarity`: Jaccard of character
  trigrams; refused at ``NEAR_DUPLICATE_THRESHOLD``).
* ``appearance`` - a compliment on someone's looks or body
  (``APPEARANCE_PATTERNS``, an English phrase list, the backstop to the
  register's craft rule 3).

**The find-side filter** (:func:`account_refusal`) skips brand, celebrity and
mega accounts: over ``max_followers``, a verified badge (when
``skip_verified``), or either one unknown. An account we could not read is
skipped, never assumed small.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional

from . import platform_policy
from . import platforms
from . import post_store
from . import profile_manager as pm

PLATFORM = platforms.INSTAGRAM

OVERLAY_KEYS = {"identity_slug", "engagement"}
ENGAGEMENT_KEY = "engagement"
ENGAGEMENT_KEYS = {"max_followers", "skip_verified"}
HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

#: The reason prefix a refused draft carries back to review.
REFUSE_RULES = "instagram_rules"


class InstagramRefused(RuntimeError):
    """A config or rule check failed before anything was done."""


# ── config ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstagramConfig:
    profile: str
    identity_slug: str            # "" when unset: every draft refuses
    max_followers: Optional[int]  # None when unset: every account is skipped
    skip_verified: bool


def _overlay(cfg):
    section = (cfg or {}).get("scheduled_posting")
    overlays = section.get("platforms") if isinstance(section, dict) else None
    return overlays.get(PLATFORM) if isinstance(overlays, dict) else None


def parse_overlay(raw, where="scheduled_posting.platforms.instagram"):
    """Validate the Instagram overlay. Unknown keys and wrong types refuse."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InstagramRefused("%s must be an object" % where)
    unknown = sorted(set(raw) - OVERLAY_KEYS)
    if unknown:
        raise InstagramRefused("%s has unknown keys: %s" % (where, ", ".join(unknown)))
    slug = raw.get("identity_slug")
    if slug is not None:
        if not isinstance(slug, str):
            raise InstagramRefused("%s.identity_slug must be text" % where)
        if slug.strip() and not HANDLE_RE.match(slug.strip().lstrip("@")):
            raise InstagramRefused("%s.identity_slug is not an Instagram handle: %r"
                                   % (where, slug))
    eng = raw.get(ENGAGEMENT_KEY, {})
    if not isinstance(eng, dict):
        raise InstagramRefused("%s.engagement must be an object" % where)
    bad = sorted(set(eng) - ENGAGEMENT_KEYS)
    if bad:
        raise InstagramRefused("%s.engagement has unknown keys: %s"
                               % (where, ", ".join(bad)))
    mf = eng.get("max_followers")
    if mf is not None and (isinstance(mf, bool) or not isinstance(mf, int) or mf < 1):
        raise InstagramRefused("%s.engagement.max_followers must be a whole number "
                               "of at least 1" % where)
    sv = eng.get("skip_verified")
    if sv is not None and not isinstance(sv, bool):
        raise InstagramRefused("%s.engagement.skip_verified must be true or false"
                               % where)
    return raw


def instagram_config(profile, profile_cfg=None) -> InstagramConfig:
    cfg = profile_cfg if profile_cfg is not None else pm.get_profile_config(profile)
    raw = parse_overlay(_overlay(cfg))
    eng = raw.get(ENGAGEMENT_KEY) or {}
    sv = eng.get("skip_verified")
    return InstagramConfig(
        profile=profile,
        identity_slug=(raw.get("identity_slug") or "").strip().lstrip("@").lower(),
        max_followers=eng.get("max_followers"),
        skip_verified=True if sv is None else sv)


def load_store(profile):
    """``data/<p>/instagram/posts_db.json``, keyed by shortcode. No LinkedIn
    reconcile runs on it."""
    return post_store.load_synced_store(profile, platform=PLATFORM)


# ── emoji ───────────────────────────────────────────────────────────────────

_ZWJ = 0x200D
_KEYCAP = 0x20E3


def _is_modifier(cp):
    return (cp in (0xFE0E, 0xFE0F) or 0x1F3FB <= cp <= 0x1F3FF
            or 0xE0020 <= cp <= 0xE007F)


def _is_emoji(cp):
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
            or 0x2300 <= cp <= 0x23FF or 0x2B00 <= cp <= 0x2BFF
            or cp == _KEYCAP)


def emoji_count(text) -> int:
    """How many emoji a reader sees in ``text``.

    A ZWJ sequence (family, profession), a skin-tone or presentation variant
    and a regional-indicator pair (a flag) each count once. Keycaps count
    once. Plain text symbols outside the emoji blocks (©, ™, arrows) do not.
    """
    n = 0
    joined = False
    regional = 0
    for ch in text or "":
        cp = ord(ch)
        if cp == _ZWJ:
            joined = True
            continue
        if _is_modifier(cp):
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF:
            regional += 1
            if regional % 2 == 1:
                n += 1
            joined = False
            continue
        regional = 0
        if cp == _KEYCAP or (_is_emoji(cp) and not joined):
            n += 1
        joined = False
    return n


# ── language ────────────────────────────────────────────────────────────────

UNKNOWN = "unknown"

#: Small, distinctive function-word sets for the Latin-script languages the
#: heuristic names. A word in two sets counts for both; the margin rule below
#: decides.
STOPWORDS = {
    "en": {"the", "and", "is", "this", "that", "with", "for", "you", "are", "was",
           "of", "to", "in", "my", "so", "how", "what", "did", "just", "your",
           "from", "have", "it's", "it", "these", "those", "does", "on"},
    "es": {"el", "los", "las", "que", "y", "es", "por", "para", "con", "una", "muy",
           "del", "pero", "como", "esto", "este", "qué", "mi", "tu", "más", "cómo"},
    "fr": {"le", "les", "et", "est", "une", "des", "pour", "avec", "pas", "qui",
           "c'est", "très", "mon", "ce", "cette", "du", "sur", "je", "vous", "mais"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "ein", "eine", "mit", "für",
           "ich", "auf", "sehr", "auch", "wie", "dem", "den", "zu", "wirklich"},
    "pt": {"o", "os", "um", "uma", "não", "muito", "isso", "meu", "você", "mais",
           "é", "com", "do", "da", "que", "para", "como"},
    "it": {"il", "lo", "gli", "che", "è", "un", "per", "non", "molto", "della",
           "questo", "mio", "sono", "ma", "più", "con", "del"},
    "nl": {"het", "een", "niet", "van", "voor", "met", "dit", "dat", "ik", "zijn",
           "heel", "ook", "op", "en", "de", "is"},
}

_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)
_STRIP_RE = re.compile(r"(https?://\S+|www\.\S+|[#@][\w.]+)", re.UNICODE)

#: (name, low, high) code point ranges for the non-Latin scripts the
#: heuristic tells apart.
_SCRIPTS = (
    ("cyrillic", 0x0400, 0x04FF), ("greek", 0x0370, 0x03FF),
    ("arabic", 0x0600, 0x06FF), ("hebrew", 0x0590, 0x05FF),
    ("devanagari", 0x0900, 0x097F), ("thai", 0x0E00, 0x0E7F),
    ("hangul", 0xAC00, 0xD7AF), ("hangul", 0x1100, 0x11FF),
    ("kana", 0x3040, 0x30FF), ("han", 0x4E00, 0x9FFF),
)

#: Fewest letters a text needs before its script is named.
MIN_LETTERS = 3
#: Fewest stopword hits before a Latin language is named, and the factor it
#: must lead the runner-up by.
MIN_HITS = 2
LEAD_FACTOR = 1.5


def _script_of(ch):
    cp = ord(ch)
    for name, lo, hi in _SCRIPTS:
        if lo <= cp <= hi:
            return name
    if ch.isalpha() and unicodedata.name(ch, "").startswith("LATIN"):
        return "latin"
    return None


def detect_language(text) -> str:
    """A language code, a script name, or ``"unknown"``.

    A stdlib heuristic, not a language model. Links, hashtags and @-mentions
    are removed first. The dominant script (at least 60% of the letters, and
    at least ``MIN_LETTERS``) decides non-Latin text: ``cyrillic``, ``greek``,
    ``arabic``, ``hebrew``, ``devanagari``, ``thai``, ``hangul``, ``ja`` (any
    kana among Han and kana), ``han``. Latin text is named ``en``, ``es``,
    ``fr``, ``de``, ``pt``, ``it`` or ``nl`` by function-word hits
    (``STOPWORDS``) when the best has ``MIN_HITS`` and leads the next by
    ``LEAD_FACTOR``; otherwise ``latin`` (a Latin-script language it cannot
    name).

    **Limits.** Short text (a few words) is usually ``latin`` or ``unknown``.
    Only the seven Latin languages above are named; any other (Indonesian,
    Turkish, Polish...) reads as ``latin``. Cyrillic is one script, so
    Russian and Ukrainian are the same here; Chinese and kanji-only Japanese
    are both ``han``. Mixed-language captions take the majority.
    """
    text = _STRIP_RE.sub(" ", text or "")
    counts = {}
    letters = 0
    for ch in text:
        script = _script_of(ch)
        if script:
            letters += 1
            counts[script] = counts.get(script, 0) + 1
    if letters < MIN_LETTERS:
        return UNKNOWN
    if counts.get("kana") and counts.get("kana", 0) + counts.get("han", 0) >= 0.6 * letters:
        return "ja"
    script, top = max(counts.items(), key=lambda kv: kv[1])
    if top < 0.6 * letters:
        return UNKNOWN
    if script != "latin":
        return script
    words = [w.lower() for w in _WORD_RE.findall(text)]
    hits = sorted(((sum(1 for w in words if w in sw), lang)
                   for lang, sw in STOPWORDS.items()), reverse=True)
    (best, lang), (second, _l2) = hits[0], hits[1]
    if best >= MIN_HITS and best >= LEAD_FACTOR * second:
        return lang
    return "latin"


def _script_family(lang):
    return "latin" if lang in STOPWORDS or lang == "latin" else lang


def language_refusal(caption, draft) -> Optional[str]:
    """``"language: ..."`` when the draft is confidently not in the caption's
    language, else None. Unknown on either side is not a refusal: the
    heuristic only refuses what it can tell."""
    want, have = detect_language(caption), detect_language(draft)
    if UNKNOWN in (want, have):
        return None
    if _script_family(want) != _script_family(have):
        return "language: caption is %s, draft is %s" % (want, have)
    if want in STOPWORDS and have in STOPWORDS and want != have:
        return "language: caption is %s, draft is %s" % (want, have)
    return None


# ── near-duplicates ─────────────────────────────────────────────────────────

#: Jaccard similarity of character trigrams at or above which a draft is a
#: near-duplicate of an earlier comment to the same author.
NEAR_DUPLICATE_THRESHOLD = 0.6


def _normal(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def _trigrams(text):
    t = _normal(text)
    if len(t) < 3:
        return {t} if t else set()
    return {t[i:i + 3] for i in range(len(t) - 2)}


def similarity(a, b) -> float:
    """Jaccard similarity of the two texts' character trigrams, after
    lowercasing, NFKC and replacing punctuation with spaces. 1.0 is the same
    words; unrelated short comments score under 0.2."""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def near_duplicate_refusal(draft, previous: Iterable[str]) -> Optional[str]:
    best = 0.0
    for text in previous:
        best = max(best, similarity(draft, text))
    if best >= NEAR_DUPLICATE_THRESHOLD:
        return "near_duplicate: %.2f similar to an earlier comment to this author" % best
    return None


def previous_to_author(store, rec) -> List[str]:
    """Our comments to ``rec``'s author: COMMENTED records, and other approved
    drafts, for the same handle. ``rec`` itself is left out."""
    handle = author_of(rec)
    if not handle:
        return []
    out = []
    for other in store.posts.values():
        if other.get("key") == rec.get("key") or author_of(other) != handle:
            continue
        if other.get("status") == post_store.COMMENTED or post_store.is_approved(other):
            if other.get("comment"):
                out.append(other["comment"])
    return out


def author_of(rec):
    return ((rec or {}).get("instagram") or {}).get("author_handle") or ""


# ── appearance ──────────────────────────────────────────────────────────────

#: Compliments on someone's looks or body. English only; the register's
#: craft rule 3 is the instruction, this is the backstop.
APPEARANCE_PATTERNS = (
    r"\byou(?:'re| are)? (?:look|looking|looks)\b",
    r"\blooking (?:so |really )?(?:good|great|hot|beautiful|gorgeous|stunning|fit|"
    r"cute|amazing|fine|pretty)\b",
    r"\b(?:you(?:'re| are)|ur|u r) (?:so |really |very )?(?:beautiful|gorgeous|"
    r"pretty|hot|sexy|cute|handsome|stunning|fit|lovely)\b",
    r"\b(?:your|ur) (?:smile|eyes|body|hair|face|skin|figure|legs|abs|lips|"
    r"physique|curves|outfit|look)\b",
    r"\b(?:sexy|hottie|handsome|babe|goddess|body goals|beautiful (?:woman|girl|"
    r"lady|man)|pretty (?:girl|woman|lady)|gorgeous (?:woman|girl|lady|man))\b",
)
_APPEARANCE_RES = tuple(re.compile(p, re.IGNORECASE) for p in APPEARANCE_PATTERNS)


def appearance_refusal(text) -> Optional[str]:
    for rx in _APPEARANCE_RES:
        m = rx.search(text or "")
        if m:
            return "appearance: %r" % m.group(0)
    return None


# ── promotion (mirrors Y1's validator) ──────────────────────────────────────

LINK_RE = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|ai|co|dev|ly|gg|tv|"
    r"me|app|xyz|link|to)\b)", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w])@[\w.]+")
HASHTAG_RE = re.compile(r"(?<![\w&])#[^\s#]+")
PROMOTION_RE = re.compile(
    r"\b(follow (?:me|us|back)|check out my|link in (?:my )?bio|dm me|"
    r"my (?:page|account|profile|shop|channel)|f4f|l4l)\b", re.IGNORECASE)


def promotion_refusals(text, cfg, register) -> List[str]:
    text = text or ""
    low = text.lower()
    out = []
    if LINK_RE.search(text):
        out.append("link")
    if MENTION_RE.search(text):
        out.append("@-mention")
    if HASHTAG_RE.search(text):
        out.append("hashtag")
    if PROMOTION_RE.search(text):
        out.append("promotion phrase")
    if not cfg.identity_slug:
        out.append("config: platforms.instagram.identity_slug must be set")
    elif cfg.identity_slug in low:
        out.append("our account")
    opening = low.lstrip().lstrip("\"'").lstrip()
    for opener in register.banned_openers:
        if opening.startswith(opener.lower()):
            out.append("banned opener %r" % opener)
            break
    return out


# ── the validator ───────────────────────────────────────────────────────────

def draft_refusals(text, *, caption, cfg, previous=()) -> List[str]:
    """Every reason ``text`` may not be posted under ``caption``, or ``[]``."""
    policy = platform_policy.policy_for(PLATFORM)
    text = text or ""
    out = []
    if not text.strip():
        out.append("empty")
    if policy.length.exceeds_hard_max(text):
        out.append("too_long: %d chars > %d" % (policy.length.measure(text),
                                                policy.length.hard_max))
    n = emoji_count(text)
    if n > 1:
        out.append("emoji: %d (at most one)" % n)
    out.extend(promotion_refusals(text, cfg, policy.register))
    for check in (language_refusal(caption, text),
                  near_duplicate_refusal(text, previous),
                  appearance_refusal(text)):
        if check:
            out.append(check)
    return out


def draft_refusal(store, rec, cfg, text=None) -> Optional[str]:
    """``"instagram_rules: ..."`` for a record whose draft may not go out."""
    probe = rec.get("comment") if text is None else text
    reasons = draft_refusals(probe, caption=rec.get("text") or "", cfg=cfg,
                             previous=previous_to_author(store, rec))
    return "%s: %s" % (REFUSE_RULES, "; ".join(reasons)) if reasons else None


def send_back_to_review(store, rec, reason, save=True):
    """A refused draft goes back to review: the approval is withdrawn, the
    text and the reason kept."""
    rec["reviewed_at"] = None
    rec["approved_hash"] = None
    meta = rec.get("comment_meta") or {}
    meta[REFUSE_RULES] = reason
    rec["comment_meta"] = meta
    rec["updated_at"] = post_store._now()
    if save:
        store.save()


def approve_refusal(profile, rec, text):
    """What an approve route must refuse for an Instagram draft: the
    validator, on the exact text being approved."""
    try:
        cfg = instagram_config(profile)
    except InstagramRefused as exc:
        return "config: %s" % exc
    store = load_store(profile)
    return draft_refusal(store, rec or {}, cfg, text=text if (text or "").strip() else None)


# ── find-side account filter ────────────────────────────────────────────────

SKIP_NO_THRESHOLD = "config: engagement.max_followers is not set"


def account_refusal(account, cfg) -> Optional[str]:
    """Why we skip this account's posts, or None.

    ``account``: ``{"handle", "followers" (int or None), "verified" (bool or
    None)}``, as read from the profile page. Over ``max_followers`` is a mega
    account; a verified badge is the celebrity/brand signal. Unknown is a
    skip, never a pass.
    """
    if cfg.max_followers is None:
        return SKIP_NO_THRESHOLD
    handle = (account or {}).get("handle")
    if not handle:
        return "account_unknown"
    if cfg.identity_slug and handle.lower() == cfg.identity_slug:
        return "our_own_account"
    followers = account.get("followers")
    if followers is None:
        return "followers_unknown"
    if followers > cfg.max_followers:
        return "mega_account: %d followers > %d" % (followers, cfg.max_followers)
    verified = account.get("verified")
    if cfg.skip_verified:
        if verified is None:
            return "verified_unknown"
        if verified:
            return "verified_account"
    return None


_COUNT_RE = re.compile(r"^([\d][\d,.]*)\s*([KkMmBb]?)$")


def parse_count(text) -> Optional[int]:
    """``"1,234"`` -> 1234, ``"12.5K"`` -> 12500, ``"1.2M"`` -> 1200000.
    None for anything else."""
    m = _COUNT_RE.match((text or "").strip())
    if not m:
        return None
    num, suffix = m.group(1), m.group(2).lower()
    if suffix:
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            return None
        return int(round(value * {"k": 1e3, "m": 1e6, "b": 1e9}[suffix]))
    digits = num.replace(",", "").replace(".", "")
    return int(digits) if digits.isdigit() else None
