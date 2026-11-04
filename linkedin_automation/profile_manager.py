# Shared module for persistent Chrome sessions and multi-profile management
"""Multi-profile credential storage and Chrome-session management.

Stores LinkedIn profiles (username/password/session dir) in
``data/profiles/profiles.json``, builds Selenium Chrome drivers backed by
per-profile persistent ``user-data-dir`` sessions, and exposes the process exit
codes and ``LoginRequiredError`` used by the CLI scripts and dashboard.
"""

import os
import json
import re
import time
import logging
import getpass
from typing import Optional, Dict, Tuple

# Route TLS through the OS trust store so webdriver-manager (which uses requests)
# can fetch/verify chromedriver behind corporate TLS-intercepting proxies that
# otherwise cause "Could not reach host". No-op if truststore isn't installed.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ─── Process exit codes ───────────────────────────────────────────────────────
# CLI scripts (post finder, poster) use these so the dashboard can tell a login
# failure apart from any other error and show an actionable message.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOGIN_REQUIRED = 2
EXIT_PRODUCTION_REFUSED = 3


# ─── Page-load bound ──────────────────────────────────────────────────────────
#: The longest any single navigation (driver.get) may block, in seconds. Set on
#: every driver this project builds. Without it Selenium's default applies -
#: 300 seconds - and one stalled page silently eats five minutes of a run.
#: A navigation that hits this raises TimeoutException; callers treat that as
#: "do not know, retry later", never as "the post is gone".
PAGE_LOAD_TIMEOUT_SECONDS = 30


class LoginRequiredError(RuntimeError):
    """Raised when a LinkedIn session could not be established.

    Signals "the user must log in manually" (expired or missing persistent
    session) as opposed to a generic runtime failure.
    """


class ProductionAccessRefused(RuntimeError):
    """Raised when a browser-driving entry point would act as a declared
    PRODUCTION LinkedIn identity (or an identity that cannot be resolved at
    all) without explicit authorization.

    Production is declared, never inferred: a profile NAME says nothing about
    which account it resolves to ("jeff" and "prod" can both be production,
    and an unregistered profile resolves to no identity at all). Raised from
    ``check_production_guard``, called by ``create_driver`` before any browser
    is constructed - refusal must cost nothing. See docs/ARCHITECTURE.md §9.
    """


# ─── Configuration ────────────────────────────────────────────────────────────

# Project root = one level above this package dir. All runtime data and the
# default-config template live at the project root (never inside the package),
# so paths resolve identically no matter which directory the app is launched
# from. This module lives at <root>/linkedin_automation/profile_manager.py.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.join(PROJECT_ROOT, "data")

PROFILES_DIR = os.path.join(DATA_ROOT, "profiles")
PROFILES_FILE = os.path.join(PROFILES_DIR, "profiles.json")
CHROME_SESSIONS_DIR = os.path.join(PROFILES_DIR, "chrome_sessions")

os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(CHROME_SESSIONS_DIR, exist_ok=True)


# ─── Profile Storage ─────────────────────────────────────────────────────────

def load_profiles() -> Dict:
    """Load all profiles from disk, tolerating a missing, empty, or corrupt file.

    Always returns a dict with normalized ``profiles`` (dict) and ``default``
    (a name that exists in ``profiles``, else None) keys. A stale ``default``
    pointing at a removed profile is dropped.
    """
    default = {"profiles": {}, "default": None}

    if not os.path.exists(PROFILES_FILE):
        return default

    try:
        with open(PROFILES_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return default
        data = json.loads(content)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning(f"profiles.json unreadable ({e}); treating as empty")
        return default

    if not isinstance(data, dict):
        logger.warning("profiles.json is not a JSON object; treating as empty")
        return default

    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}

    default_name = data.get("default")
    if default_name not in profiles:
        default_name = None

    return {"profiles": profiles, "default": default_name}


def save_profiles(data: Dict):
    """Atomically save profiles to disk (write to a temp file, then rename)."""
    os.makedirs(PROFILES_DIR, exist_ok=True)
    tmp_path = PROFILES_FILE + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, PROFILES_FILE)


def get_profile(name: str) -> Optional[Dict]:
    """Get a single profile by name."""
    data = load_profiles()
    return data["profiles"].get(name)


def get_default_profile_name() -> Optional[str]:
    """Get the default profile name."""
    data = load_profiles()
    # If a default is explicitly set, use it
    if data.get("default") and data["default"] in data.get("profiles", {}):
        return data["default"]
    # If only one profile exists, it's the default
    profiles = data.get("profiles", {})
    if len(profiles) == 1:
        return list(profiles.keys())[0]
    return None


def add_profile(name: str, username: str, password: str, set_default: bool = False) -> Dict:
    """Add a new profile."""
    data = load_profiles()
    
    session_dir = os.path.join(CHROME_SESSIONS_DIR, name)
    os.makedirs(session_dir, exist_ok=True)
    
    data["profiles"][name] = {
        "username": username,
        "password": password,
        "session_dir": session_dir,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_used": None
    }
    
    # Set as default if requested or if it's the first profile
    if set_default or len(data["profiles"]) == 1:
        data["default"] = name
    
    save_profiles(data)
    logger.info(f"Profile '{name}' added successfully")
    return data["profiles"][name]


def remove_profile(name: str):
    """Remove a profile."""
    data = load_profiles()
    if name in data["profiles"]:
        del data["profiles"][name]
        if data.get("default") == name:
            # Set a new default if available
            remaining = list(data["profiles"].keys())
            data["default"] = remaining[0] if remaining else None
        save_profiles(data)
        logger.info(f"Profile '{name}' removed")
    else:
        logger.warning(f"Profile '{name}' not found")


def set_default_profile(name: str):
    """Set the default profile."""
    data = load_profiles()
    if name in data["profiles"]:
        data["default"] = name
        save_profiles(data)
        logger.info(f"Default profile set to '{name}'")
    else:
        logger.error(f"Profile '{name}' not found")


def list_profiles() -> Dict:
    """List all profiles."""
    data = load_profiles()
    return data


def update_last_used(name: str):
    """Update the last-used timestamp for a profile."""
    data = load_profiles()
    if name in data["profiles"]:
        data["profiles"][name]["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_profiles(data)


# ─── Profile-Specific Data Directories ────────────────────────────────────────

def get_data_dir(profile_name: str = None, subdir: str = None) -> str:
    """
    Get the profile-specific data directory.
    
    Structure:
        data/<profile_name>/
        data/<profile_name>/linkedin_timeline/
        data/<profile_name>/quality_comments/
        data/<profile_name>/quality_comments/debug_screenshots/
    
    Args:
        profile_name: Profile name (uses default if None)
        subdir: Optional subdirectory (e.g. 'linkedin_timeline', 'quality_comments')
    
    Returns:
        Absolute path to the directory (created if needed)
    """
    if not profile_name:
        profile_name = get_default_profile_name()
    if not profile_name:
        profile_name = "default"
    
    base = os.path.join(DATA_ROOT, profile_name)
    
    if subdir:
        path = os.path.join(base, subdir)
    else:
        path = base
    
    os.makedirs(path, exist_ok=True)
    return path


def get_timeline_dir(profile_name: str = None) -> str:
    """Get profile-specific linkedin_timeline directory."""
    return get_data_dir(profile_name, "linkedin_timeline")


def get_comments_dir(profile_name: str = None) -> str:
    """Get profile-specific quality_comments directory."""
    return get_data_dir(profile_name, "quality_comments")


def get_screenshots_dir(profile_name: str = None) -> str:
    """Get profile-specific debug_screenshots directory."""
    return get_data_dir(profile_name, os.path.join("quality_comments", "debug_screenshots"))


def get_progress_file(profile_name: str = None) -> str:
    """Get profile-specific posting_progress.json path."""
    comments_dir = get_comments_dir(profile_name)
    return os.path.join(comments_dir, "posting_progress.json")


# ─── Per-Profile Config ───────────────────────────────────────────────────────

# Default config template, shipped at the project root (not inside the package).
DEFAULT_CONFIG_FILE = os.path.join(PROJECT_ROOT, "default_profile_config.json")


def get_config_path(profile_name: str = None) -> str:
    """Path to a profile's config: data/<profile_name>/profile_config.json."""
    return os.path.join(get_data_dir(profile_name), "profile_config.json")


def load_default_config() -> Dict:
    """Load the default config template; fall back to a minimal dict if missing."""
    try:
        with open(DEFAULT_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"default_profile_config.json unreadable ({e}); using minimal defaults")
        return {
            "display_name": "", "headline": "", "niche": "",
            "post_finder": {
                "keywords_tier1": [], "keywords_tier2": [],
                "min_quality_score": 5, "max_posts_per_scan": 50,
                "post_types_to_engage": ["thought_leadership", "technical", "other"],
            },
            "comment_generator": {
                "persona": "", "tone": "", "voice": "",
                "topics_of_expertise": [], "things_to_avoid": [],
                "comment_length_range": [15, 60], "style_mix": {},
            },
            "connector": {"note_template": "", "max_daily_requests": 25},
        }


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Return base deep-merged with override (override wins; nested dicts merged).

    Used so a user config is filled in with any keys added to the default
    template later (additive) without overwriting the user's own values.
    """
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def save_profile_config(profile_name: str, config: Dict):
    """Atomically write a profile's config (temp file + rename)."""
    path = get_config_path(profile_name)
    tmp_path = path + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, path)


def read_stored_config(profile_name: str = None) -> Dict:
    """The profile's config AS STORED, with no default merge.

    This is the correct base for a partial update. get_profile_config cannot be
    used for that: it merges the default template in, so writing its result back
    would bake the entire template into every profile file and make a later
    change to a default invisible to existing profiles.

    An unreadable config returns {} rather than raising - the caller is about to
    overwrite it anyway, and refusing to merge would leave the profile stuck.
    """
    path = get_config_path(profile_name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            stored = json.load(f)
        return stored if isinstance(stored, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"profile_config.json unreadable ({e}); merging onto empty")
        return {}


def get_profile_config(profile_name: str = None) -> Dict:
    """Return a profile's config, creating it from the default on first use.

    Existing configs are deep-merged onto the default template so newly-added
    default keys appear without clobbering user values. Never raises: a corrupt
    config falls back to (and is treated as) the default.
    """
    path = get_config_path(profile_name)
    default = load_default_config()

    if not os.path.exists(path):
        save_profile_config(profile_name, default)
        logger.info(f"Created default profile config at {path}")
        return default

    try:
        with open(path, 'r', encoding='utf-8') as f:
            user_config = json.load(f)
        if not isinstance(user_config, dict):
            raise ValueError("config is not a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"profile_config.json unreadable ({e}); using defaults")
        return default

    return _deep_merge(default, user_config)


def reset_profile_config(profile_name: str = None) -> Dict:
    """Overwrite a profile's config with the default template and return it."""
    default = load_default_config()
    save_profile_config(profile_name, default)
    logger.info(f"Reset profile config for '{profile_name or get_default_profile_name()}'")
    return default


# ─── Scheduled-posting settings, per (profile, platform) ─────────────────────
#
# ONE resolver, so there is one answer to "which channel, which key, which
# identity". Before this there were two independent sources — the dashboard read
# the flat config and tools/run_scheduled_posts.py took them as CLI arguments —
# and two sources are two chances to post to the wrong account.
#
# The shape is deliberately backward-compatible. A profile's flat
# `scheduled_posting` fields ARE the LinkedIn settings; they did not move, so
# nothing migrates and LinkedIn's resolution is the expression it always was.
# Another platform gets an entry under `scheduled_posting.platforms.<name>`.

#: The platform whose settings live in the flat fields, for historical reasons.
SCHEDULED_DEFAULT_PLATFORM = "linkedin"

#: Where LinkedIn's Buffer key comes from. Matches buffer_client.api_key(), and
#: is the fallback recorded in the resolved dict so a caller can say which
#: variable it wanted if the key turns out to be missing.
SCHEDULED_DEFAULT_KEY_ENV = "BUFFER_API_KEY"


class ScheduledConfigError(RuntimeError):
    """A (profile, platform) cannot be resolved into usable settings."""


#: Every environment variable name this codebase actually reads
#: (BUFFER_API_KEY, BUFFER_API_KEY_X, PRODUCTION_IDENTITY_SLUGS,
#: OPENAI_API_KEY, R2_ACCOUNT_ID, ...) is upper-snake-case: a leading letter,
#: then letters/digits/underscores. A real Buffer or OpenAI API key is never
#: shaped like that - it is long, mixed-case, or carries punctuation a shell
#: variable name cannot. This is deliberately a shape check, not a secrets
#: scanner: it exists to catch the one mistake the settings form's env-name
#: field is built to prevent (pasting the KEY where only its NAME belongs),
#: not to detect every possible credential.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENV_VAR_NAME_MAX_LEN = 64


def valid_env_var_name(value: str) -> bool:
    """Does ``value`` look like an environment VARIABLE NAME, not a secret
    pasted into the wrong field?

    Used by the scheduled-posting settings form (dashboard.py) to reject an
    API key value entered where only ``buffer_api_key_env`` - the NAME of the
    variable holding it - belongs.
    """
    return bool(value) and len(value) <= _ENV_VAR_NAME_MAX_LEN and bool(
        _ENV_VAR_NAME_RE.match(value))


def scheduled_config_str(value) -> str:
    """A saved channel id, identity slug or key variable name, stripped - or ""
    if it is not a string.

    profile_config.json can be hand-edited, so a number or a list can arrive
    here. Coercing one with str() would invent a channel id or identity nobody
    configured, on a path that acts on a real account; reading it as unset
    gives it exactly the behaviour of a blank value. That is a refusal
    everywhere except LinkedIn's schedule path, which is deliberately ungated
    on the identity and never reads it.
    """
    return value.strip() if isinstance(value, str) else ""


def resolve_scheduled(profile_name: str = None,
                      platform: str = SCHEDULED_DEFAULT_PLATFORM,
                      env=None) -> Dict:
    """Resolve the scheduled-posting settings for one ``(profile, platform)``.

    Returns ``{platform, api_key, api_key_env, channel_id, identity_slug,
    drain}``.

    **LinkedIn resolves exactly as it did before this function existed.** Its
    settings are the flat `scheduled_posting` fields, stripped as the dashboard
    always did (a non-string reads as blank, see ``scheduled_config_str``), and
    its key is the bare ``BUFFER_API_KEY``. `tests/fixtures/scheduled_resolution_golden.json`
    is a snapshot taken from the pre-change code and is asserted against, so a
    drift here fails rather than being argued about.

    **The key is resolved but NOT required.** For LinkedIn a missing
    ``BUFFER_API_KEY`` yields ``api_key=None`` rather than raising, because the
    pre-change ``_scheduled_channel_id`` never touched the key and asking for a
    channel id must not start failing on an unrelated variable. A caller that
    needs the key passes it to ``buffer_client``, whose ``key or api_key()``
    fallback raises the message it always did.

    **An overlaid platform is stricter, because it has no fallback.** Its key
    comes from the environment variable its overlay NAMES
    (``buffer_api_key_env``), and a missing or blank one raises here, naming the
    variable — there is no bare default to silently land on.

    **An unknown platform raises rather than falling back to the flat fields.**
    Returning LinkedIn's channel for ``platform="x"`` because of a typo would
    publish X content to LinkedIn. That failure is unrecoverable and silent,
    which is the worst combination, so a platform that is not LinkedIn and has
    no overlay is an error.
    """
    env = os.environ if env is None else env
    platform = (platform or SCHEDULED_DEFAULT_PLATFORM).strip().lower()

    section = (get_profile_config(profile_name) or {}).get("scheduled_posting")
    section = section if isinstance(section, dict) else {}
    overlays = section.get("platforms")
    overlays = overlays if isinstance(overlays, dict) else {}
    overlay = overlays.get(platform)

    if platform == SCHEDULED_DEFAULT_PLATFORM:
        # The legacy path, unchanged. Note it reads the FLAT fields even if a
        # "linkedin" overlay were added later: moving LinkedIn under the
        # overlay is a migration, and this function does not perform one.
        return {
            "platform": platform,
            "api_key": (env.get(SCHEDULED_DEFAULT_KEY_ENV) or "").strip() or None,
            "api_key_env": SCHEDULED_DEFAULT_KEY_ENV,
            "channel_id": scheduled_config_str(section.get("buffer_channel_id")),
            "identity_slug": scheduled_config_str(section.get("identity_slug")),
            "drain": section.get("drain"),
        }

    if not isinstance(overlay, dict):
        raise ScheduledConfigError(
            "profile %r has no scheduled_posting.platforms.%s section, so there "
            "are no %s settings to use. Refusing to fall back to the flat "
            "fields: those are LinkedIn's, and using them here would publish to "
            "LinkedIn. Known platforms: %s"
            % (profile_name or get_default_profile_name(), platform, platform,
               ", ".join(sorted([SCHEDULED_DEFAULT_PLATFORM] + list(overlays)))))

    key_env = scheduled_config_str(overlay.get("buffer_api_key_env"))
    if not key_env:
        raise ScheduledConfigError(
            "scheduled_posting.platforms.%s has no buffer_api_key_env, so there "
            "is no way to know which environment variable holds this account's "
            "Buffer key. Set it (e.g. BUFFER_API_KEY_%s)."
            % (platform, platform.upper()))

    api_key = (env.get(key_env) or "").strip()
    if not api_key:
        raise ScheduledConfigError(
            "%s is not set (or is empty), so the Buffer key for platform %r on "
            "profile %r cannot be resolved. Add it to .env — it is never "
            "committed — or change scheduled_posting.platforms.%s."
            "buffer_api_key_env to name the variable that holds it."
            % (key_env, platform, profile_name or get_default_profile_name(),
               platform))

    return {
        "platform": platform,
        "api_key": api_key,
        "api_key_env": key_env,
        "channel_id": scheduled_config_str(overlay.get("buffer_channel_id")),
        "identity_slug": scheduled_config_str(overlay.get("identity_slug")),
        # Falls back to the flat drain so a platform that does not override it
        # still gets a usable dict rather than None.
        "drain": overlay.get("drain", section.get("drain")),
    }


# ─── Auto-migrate from .env ──────────────────────────────────────────────────

def auto_migrate_from_env():
    """If no profiles exist but .env has credentials, create a default profile."""
    data = load_profiles()
    if data["profiles"]:
        return  # Already have profiles
    
    username = os.getenv('LINKEDIN_USERNAME') or os.getenv('LINKEDIN_ALT_USERNAME')
    password = os.getenv('LINKEDIN_PASSWORD') or os.getenv('LINKEDIN_ALT_PASSWORD')
    
    if username and password:
        logger.info("Auto-migrating .env credentials to profile 'default'...")
        add_profile("default", username, password, set_default=True)


# ─── Production identity guard ────────────────────────────────────────────────
# A profile NAME does not say which LinkedIn account it drives - "jeff" and
# "prod" can both resolve to the real account, and an unregistered profile
# ("someprofile", "t") resolves to no identity at all. Nothing short of this
# guard stood between an ad-hoc CLI/module run and acting as the real account.
# So production is DECLARED here, off config alone, before any browser exists -
# never inferred from a profile's name or from anything that requires a login.

#: Comma-separated identity_slugs that ARE the real, production LinkedIn
#: account(s). Loaded from the environment (like every other credential this
#: module reads via .env) rather than committed to a config file, since a
#: LinkedIn vanity slug identifies a real person (docs/PRINCIPLES.md §10).
PRODUCTION_IDENTITY_SLUGS_ENV = "PRODUCTION_IDENTITY_SLUGS"

#: The --allow-production flag's environment equivalent, for a caller that
#: cannot thread a CLI flag through.
ALLOW_PRODUCTION_ENV = "LINKEDIN_ALLOW_PRODUCTION"


def get_production_identity_slugs() -> set:
    """The identity_slugs declared as the real, production account(s) - any
    platform, since this list is shared across all of them (see
    docs/ARCHITECTURE.md §9 and .dev/DECISIONS.md's 2026-09-29 entry).

    A leading ``@`` is stripped so ``@AI_Fun_times`` and ``AI_Fun_times``
    declare the same identity - matching csv_pipeline.verify_channel_identity,
    which does the same normalization on the OTHER side of this exact
    comparison (check_declared_production). LinkedIn slugs never carry a
    leading ``@``, so this is a no-op for every existing declaration.

    Never inferred from a profile name - see ProductionAccessRefused.
    """
    raw = os.environ.get(PRODUCTION_IDENTITY_SLUGS_ENV, "")
    return {s.strip().lstrip("@").lower() for s in raw.split(",") if s.strip()}


def get_identity_slug(profile_name: str) -> str:
    """The profile's declared identity: ``scheduled_posting.identity_slug`` in
    its profile_config.json, or "" if never set.

    Declared, not detected - this is a config read, no browser or login
    required, so the production guard can refuse before either exists.
    """
    config = get_profile_config(profile_name)
    scheduled = config.get("scheduled_posting") or {}
    return scheduled_config_str(scheduled.get("identity_slug"))


def _allow_production_from_env() -> bool:
    return os.environ.get(ALLOW_PRODUCTION_ENV, "").strip().lower() in ("1", "true", "yes")


def check_production_guard(profile_name: str, allow_production: bool = False) -> None:
    """Refuse before any browser is created if ``profile_name`` resolves to a
    declared production identity - or to no identity at all - without
    explicit authorization.

    An identity that resolves to NOTHING is a refusal, not a pass: "unknown"
    is not the same as "known dev", and treating it that way is exactly the
    trap of inferring safety from a profile name. Only a profile with a
    declared, non-production identity_slug proceeds without ``--allow-production``.

    Raises ProductionAccessRefused. Never raises for a missing/unknown
    profile_name itself - the caller (create_driver) checks that separately.
    """
    allow_production = allow_production or _allow_production_from_env()
    identity_slug = get_identity_slug(profile_name)

    if not identity_slug:
        raise ProductionAccessRefused(
            f"Refusing to drive a browser for profile '{profile_name}': it has "
            f"no declared identity (scheduled_posting.identity_slug is empty "
            f"in its profile_config.json). An unresolved identity is not "
            f"assumed safe - declare identity_slug for this profile before "
            f"running against it."
        )

    if identity_slug.lower() in get_production_identity_slugs() and not allow_production:
        raise ProductionAccessRefused(
            f"Refusing to drive a browser for profile '{profile_name}': its "
            f"identity_slug '{identity_slug}' is a declared PRODUCTION "
            f"identity. Pass --allow-production (or set "
            f"{ALLOW_PRODUCTION_ENV}=1) to proceed."
        )


def check_declared_production(identity_slug: str, context: str,
                               allow_production: bool = False) -> None:
    """The declared-production model, for a write path that never touches
    ``create_driver`` - e.g. X's Buffer posting, which has no browser and so
    cannot be gated by ``check_production_guard``.

    Same two rules as ``check_production_guard``, same env vars
    (``PRODUCTION_IDENTITY_SLUGS``, ``LINKEDIN_ALLOW_PRODUCTION``), same
    exception and exit code - but takes an already-resolved ``identity_slug``
    directly rather than a profile name, since a non-LinkedIn platform's
    identity resolves from its own config path (e.g.
    ``scheduled_posting.platforms.x.identity_slug``), not the flat
    ``get_identity_slug`` LinkedIn profiles use. ``context`` names the action
    being refused (e.g. "schedule a Buffer post on x") for the message.

    Deliberately NOT a code path inside ``check_production_guard`` or
    ``create_driver`` - this guards a write that has no browser at all, so
    routing it through the browser chokepoint is not an option, and this
    function must never be called from ``create_driver`` (LinkedIn's guard
    stays exactly as it is).

    Raises ProductionAccessRefused. An identity that resolves to NOTHING is a
    refusal, not a pass - unknown is not the same as known-non-production, and
    that holds with or without ``allow_production``.
    """
    allow_production = allow_production or _allow_production_from_env()

    # Normalized the same way csv_pipeline.verify_channel_identity normalizes
    # the identity it compares against a live Buffer channel - "@AI_Fun_times"
    # and "AI_Fun_times" must be the SAME declared identity on both sides of
    # this guard, or a slug written with a leading '@' here (natural for an X
    # handle) silently never matches an entry in PRODUCTION_IDENTITY_SLUGS
    # (also normalized, see get_production_identity_slugs), and a declared-
    # production account schedules unrefused.
    normalized = (identity_slug or "").strip().lstrip("@").lower()

    if not normalized:
        raise ProductionAccessRefused(
            f"Refusing to {context}: no declared identity. An unresolved "
            f"identity is not assumed safe - declare an identity_slug before "
            f"running against it."
        )

    if normalized in get_production_identity_slugs() and not allow_production:
        raise ProductionAccessRefused(
            f"Refusing to {context}: identity '{identity_slug}' is a "
            f"declared PRODUCTION identity. Pass --allow-production (or set "
            f"{ALLOW_PRODUCTION_ENV}=1) to proceed."
        )


# ─── Chrome Driver with Persistent Session ────────────────────────────────────

def session_exists(session_dir: str) -> bool:
    """Best-effort check for whether a profile already has a Chrome session.

    A freshly created profile's session dir is empty; Chrome populates a
    ``Default`` subdirectory (cookies, localStorage, etc.) after the first real
    run. Used only to decide whether to print first-run login instructions.
    """
    if not session_dir or not os.path.isdir(session_dir):
        return False
    default_dir = os.path.join(session_dir, "Default")
    return os.path.isdir(default_dir) and bool(os.listdir(default_dir))


def create_driver(profile_name: str = None, headless: bool = False,
                   allow_production: bool = False) -> Tuple[webdriver.Chrome, Dict]:
    """
    Create a Chrome driver with persistent session for the given profile.

    ``allow_production`` must be explicitly True (or LINKEDIN_ALLOW_PRODUCTION
    set) for this to proceed against a declared production identity - see
    check_production_guard. This is the ONE place every browser-driving entry
    point in the project funnels through, so it is the single chokepoint for
    that guard (docs/ARCHITECTURE.md §9).

    Returns (driver, profile_info) tuple.
    """
    # Auto-migrate on first use
    auto_migrate_from_env()

    # Resolve profile
    if not profile_name:
        profile_name = get_default_profile_name()

    if not profile_name:
        raise ValueError(
            "No profile specified and no default set.\n"
            "Run: python linkedin_profile_manager.py add <name> \n"
            "  or use --profile <name>"
        )

    profile = get_profile(profile_name)
    if not profile:
        raise ValueError(
            f"Profile '{profile_name}' not found.\n"
            f"Available profiles: {', '.join(load_profiles()['profiles'].keys()) or 'none'}\n"
            f"Run: python linkedin_profile_manager.py add {profile_name}"
        )

    # Refuse BEFORE anything below constructs a browser - a refusal must cost
    # nothing and leave no session behind.
    check_production_guard(profile_name, allow_production=allow_production)

    logger.info(f"Using profile: '{profile_name}' ({profile['username']})")

    # If this profile has no persistent Chrome session yet, tell the user how to
    # establish one instead of letting an automated login silently fail later.
    if not session_exists(profile.get('session_dir', '')):
        logger.warning(
            "No existing Chrome session for profile '%s'. A browser window will "
            "open: log in to LinkedIn manually, then close it — your session "
            "will persist for future runs. You can also run: "
            "python tools/login_check.py --profile %s",
            profile_name, profile_name,
        )

    # Setup Chrome options with persistent user-data-dir
    options = Options()
    options.add_argument(f"--user-data-dir={os.path.abspath(profile['session_dir'])}")
    options.add_argument("--profile-directory=Default")
    
    # Anti-detection
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    # Suppress noise
    options.add_argument('--log-level=3')
    options.add_argument('--disable-logging')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    
    if headless:
        options.add_argument('--headless=new')
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    driver.maximize_window()

    # Anti-detection JS
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    
    update_last_used(profile_name)
    
    return driver, profile


# URL fragments that mean LinkedIn bounced a logged-out session to an auth wall.
# Checked FIRST (a login redirect often carries the original path in a
# session_redirect param, so "login" must win over any "/search" in that param).
LOGGED_OUT_URL_MARKERS = ("login", "authwall", "checkpoint", "challenge", "/signup", "/uas/")

# URL fragments for authenticated LinkedIn pages a caller may have loaded. Includes
# people-search (/search/results/) so the connector's search-page callers aren't
# falsely reported as logged out just because the URL isn't /feed/.
LOGGED_IN_URL_MARKERS = ("/feed", "/in/", "mynetwork", "/search/results", "/search/")


def is_logged_in_on_page(driver: webdriver.Chrome) -> bool:
    """Determine login status from the page already loaded — without navigating.

    URL-based (no brittle element wait): a logged-out session is redirected to a
    login/authwall/checkpoint page, so those markers mean logged out; otherwise
    staying on an authenticated URL (the feed, a profile, my-network, or a
    /search/results/ page) means logged in. Works for callers that have already
    navigated to /feed/ (avoiding a degraded re-navigation, see BLOCKED.md) AND
    for the connector/search-health callers sitting on a search results page.

    Because it checks the URL and not the presence of any specific element, an
    empty search (a valid logged-in page with zero result cards) is correctly
    reported as logged in — "no results" is not "not logged in".
    """
    try:
        current_url = driver.current_url or ""
        if any(marker in current_url for marker in LOGGED_OUT_URL_MARKERS):
            return False
        if any(marker in current_url for marker in LOGGED_IN_URL_MARKERS):
            return True
        return False
    except Exception as e:
        logger.debug(f"Login check (current page) error: {e}")
        return False


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """Check login status by navigating to /feed/, then inspecting the URL.

    URL-based (no brittle element wait): a logged-out session is redirected to a
    login/authwall page, so reaching /feed/ (or another authenticated page) means
    the session is valid. The old element wait used stale selectors and required
    the feed to be scrolled (posts lazy-load), producing false "not logged in".
    """
    try:
        driver.get('https://www.linkedin.com/feed/')
        time.sleep(4)
        return is_logged_in_on_page(driver)
    except Exception as e:
        logger.debug(f"Login check error: {e}")
        return False


def login(driver: webdriver.Chrome, profile: Dict) -> bool:
    """
    Login to LinkedIn. Checks if already logged in first (persistent session).
    """
    logger.info("Checking login status...")
    
    if is_logged_in(driver):
        logger.info("✓ Already logged in (persistent session)")
        return True
    
    logger.info("Not logged in, performing fresh login...")
    
    try:
        driver.get('https://www.linkedin.com/login')
        time.sleep(2)
        
        # Check if we're already redirected to feed (race condition with cookie)
        if "feed" in driver.current_url:
            logger.info("✓ Session restored during navigation")
            return True
        
        username = profile.get('username', '')
        password = profile.get('password', '')
        
        if not username or not password:
            logger.error("Missing credentials in profile")
            return False
        
        driver.find_element(By.ID, "username").send_keys(username)
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        
        time.sleep(5)
        
        if "feed" in driver.current_url:
            logger.info("✓ Login successful")
            return True
        
        if "checkpoint" in driver.current_url or "challenge" in driver.current_url:
            logger.warning("⚠ Security checkpoint - please complete manually")
            input("Press Enter after completing the checkpoint...")
            if "feed" in driver.current_url or "/in/" in driver.current_url:
                logger.info("✓ Checkpoint completed, logged in")
                return True
        
        logger.error(f"Login failed - landed on: {driver.current_url}")
        return False
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return False


# ─── Convenience function for scripts ─────────────────────────────────────────

def setup_and_login(profile_name: str = None, headless: bool = False,
                     allow_production: bool = False) -> Tuple[webdriver.Chrome, Dict, str]:
    """
    One-call setup: create driver, login, return (driver, profile, profile_name).

    Usage in scripts:
        driver, profile, profile_name = setup_and_login(args.profile)
    """
    auto_migrate_from_env()

    if not profile_name:
        profile_name = get_default_profile_name()

    if not profile_name:
        raise ValueError("No profile available. Run: python linkedin_profile_manager.py add <name>")

    driver, profile = create_driver(profile_name, headless=headless, allow_production=allow_production)
    
    if not login(driver, profile):
        driver.quit()
        raise RuntimeError(f"Failed to log in with profile '{profile_name}'")
    
    return driver, profile, profile_name


# ─── CLI for Profile Management ──────────────────────────────────────────────

def cli():
    """Command-line interface for managing profiles."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='LinkedIn Profile Manager - Manage persistent sessions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all profiles
  python linkedin_profile_manager.py list
  
  # Add a new profile
  python linkedin_profile_manager.py add work
  
  # Add with credentials inline
  python linkedin_profile_manager.py add personal --username user@email.com --password mypass
  
  # Set default profile
  python linkedin_profile_manager.py default work
  
  # Remove a profile
  python linkedin_profile_manager.py remove old_profile
  
  # Test login for a profile
  python linkedin_profile_manager.py test work
  
  # Auto-migrate from .env
  python linkedin_profile_manager.py migrate
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command')
    
    # List
    subparsers.add_parser('list', help='List all profiles')
    
    # Add
    add_parser = subparsers.add_parser('add', help='Add a new profile')
    add_parser.add_argument('name', help='Profile name (e.g. work, personal)')
    add_parser.add_argument('--username', help='LinkedIn username/email')
    add_parser.add_argument('--password', help='LinkedIn password')
    add_parser.add_argument('--default', action='store_true', help='Set as default')
    
    # Remove
    remove_parser = subparsers.add_parser('remove', help='Remove a profile')
    remove_parser.add_argument('name', help='Profile name to remove')
    
    # Default
    default_parser = subparsers.add_parser('default', help='Set default profile')
    default_parser.add_argument('name', help='Profile name')
    
    # Test
    test_parser = subparsers.add_parser('test', help='Test login for a profile')
    test_parser.add_argument('name', nargs='?', help='Profile name (uses default if omitted)')
    test_parser.add_argument('--allow-production', action='store_true',
                              help='Allow running against a declared PRODUCTION identity')
    
    # Migrate
    subparsers.add_parser('migrate', help='Migrate credentials from .env to profile')

    # Config
    config_parser = subparsers.add_parser('config', help='View or edit a profile config')
    config_parser.add_argument('name', nargs='?', help='Profile name (uses default if omitted)')
    config_parser.add_argument('--edit', action='store_true', help='Open the config in your editor')
    config_parser.add_argument('--reset', action='store_true', help='Regenerate config from the default template')

    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == 'list':
        data = list_profiles()
        profiles = data.get("profiles", {})
        default = data.get("default")
        
        if not profiles:
            print("\nNo profiles configured.")
            print("Run: python linkedin_profile_manager.py add <name>")
            print("  or: python linkedin_profile_manager.py migrate  (import from .env)")
            return
        
        print(f"\n{'='*60}")
        print("LINKEDIN PROFILES")
        print(f"{'='*60}")
        
        for name, info in profiles.items():
            is_default = " [DEFAULT]" if name == default else ""
            print(f"\n  📋 {name}{is_default}")
            print(f"     Username:  {info['username']}")
            print(f"     Created:   {info.get('created', 'unknown')}")
            print(f"     Last used: {info.get('last_used', 'never')}")
            print(f"     Session:   {info.get('session_dir', 'N/A')}")
        
        print(f"\n  Total: {len(profiles)} profile(s)")
        print()
    
    elif args.command == 'add':
        name = args.name
        
        # Check if already exists
        if get_profile(name):
            print(f"Profile '{name}' already exists. Remove it first or choose a different name.")
            return
        
        username = args.username
        password = args.password
        
        if not username:
            username = input(f"LinkedIn username/email for '{name}': ").strip()
        if not password:
            # Do NOT strip the password: special chars and intentional leading/
            # trailing spaces must be preserved exactly. getpass avoids the
            # shell/argparse mangling that breaks passwords with +, &, !, etc.
            password = getpass.getpass(f"LinkedIn password for '{name}': ")

        if not username or not password:
            print("Username and password are required.")
            return
        
        add_profile(name, username, password, set_default=args.default)
        print(f"\n✅ Profile '{name}' created successfully!")
        
        if args.default:
            print("   Set as default profile.")
        
        print(f"\nTip: Run 'python linkedin_profile_manager.py test {name}' to verify login")
    
    elif args.command == 'remove':
        profile = get_profile(args.name)
        if not profile:
            print(f"Profile '{args.name}' not found.")
            return
        
        confirm = input(f"Remove profile '{args.name}' ({profile['username']})? [y/N]: ").strip().lower()
        if confirm == 'y':
            remove_profile(args.name)
            print(f"✅ Profile '{args.name}' removed.")
        else:
            print("Cancelled.")
    
    elif args.command == 'default':
        if get_profile(args.name):
            set_default_profile(args.name)
            print(f"✅ Default profile set to '{args.name}'")
        else:
            print(f"Profile '{args.name}' not found.")
    
    elif args.command == 'test':
        profile_name = args.name or get_default_profile_name()
        if not profile_name:
            print("No profile specified and no default set.")
            return
        
        print(f"Testing login for profile '{profile_name}'...")
        try:
            driver, profile, _ = setup_and_login(profile_name, allow_production=args.allow_production)
            print(f"\n✅ Login successful for '{profile_name}'!")
            print(f"   Current URL: {driver.current_url}")
            print("   Session will persist for next use.")
            input("\nPress Enter to close browser...")
            driver.quit()
        except Exception as e:
            print(f"\n❌ Login failed: {e}")
    
    elif args.command == 'migrate':
        auto_migrate_from_env()
        data = load_profiles()
        if data["profiles"]:
            print("✅ Migrated to profile 'default'")
            default_profile = data["profiles"].get("default", {})
            print(f"   Username: {default_profile.get('username', 'N/A')}")
        else:
            print("No credentials found in .env to migrate.")

    elif args.command == 'config':
        profile_name = args.name or get_default_profile_name()
        if not profile_name:
            print("No profile specified and no default set.")
            return
        config_path = get_config_path(profile_name)

        if args.reset:
            reset_profile_config(profile_name)
            print(f"✅ Reset config for '{profile_name}' -> {config_path}")
        elif args.edit:
            # Ensure the file exists (create default on first use), then open it.
            get_profile_config(profile_name)
            editor = os.environ.get('EDITOR') or ('notepad' if os.name == 'nt' else 'nano')
            print(f"Opening {config_path} in {editor}...")
            try:
                import subprocess
                subprocess.run([editor, config_path])
            except Exception as e:
                print(f"Could not open editor ({e}). Edit the file directly:\n  {config_path}")
        else:
            # Print the (effective) config.
            config = get_profile_config(profile_name)
            print(f"\nConfig for '{profile_name}' ({config_path}):\n")
            print(json.dumps(config, indent=2))


if __name__ == "__main__":
    cli()
