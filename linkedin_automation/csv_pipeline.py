"""CSV -> scheduled LinkedIn post -> first comment. Phase 5 of the hybrid.

Ties Phases 1-4 together:

    validate -> generate text if blank -> R2 upload -> Buffer createPost
             -> (later) poll to sent -> browser first comment

**TWO PASSES, NOT ONE.** Buffer publishes at ``dueAt``, and a content calendar
schedules days apart. An inline model - create a post, wait for it to publish,
comment, move on - would mean a process sitting for days, where a crash or a
closed laptop loses every row after the current one. So:

    SCHEDULE pass   validates and creates every post now. Minutes, not days.
    COMMENT pass    sweeps for posts that have since published and comments
                    them. Non-blocking: a row not yet due is simply left for
                    the next sweep, so this is safe to run on a timer.

The comment pass is idempotent by construction, which is what makes the whole
thing resumable: run it as often as you like.

**THE POST IS IRREVERSIBLE ONCE PUBLISHED**, and the state machine is built
around that. A row that already has a ``post_id`` is NEVER passed to createPost
again, whatever else failed. Re-running the CSV after a partial failure retries
the comment, never the post.
"""

import csv
import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime, timezone

from . import buffer_client as bc
from . import first_comment as fc
from . import image_host
from . import platform_policy
from . import profile_manager as pm

from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

STATE_NAME = "scheduled_posts_state.json"

#: The platform whose behaviour is the historical default everywhere
#: here. Mirrors post_store.X / platform_policy.PLATFORMS.
X_PLATFORM = "x"

COLUMNS = ("date", "time_window", "post_text", "topic", "tags", "image_path",
           "first_comment_link")

# Row states. The ordering matters: nothing ever moves backwards past SCHEDULED,
# because past SCHEDULED a real post exists on LinkedIn.
PENDING = "pending"
# Ready to go, but Buffer had no scheduled-post slot free. NOT a failure: the
# row is correct and needs nothing from anyone. Keeping it distinct from FAILED
# is the whole point - telling someone to "fix the row and schedule again" when
# the row is fine and the plan is simply full is worse than saying nothing.
HELD = "held"
SCHEDULED = "scheduled"      # createPost done; not yet published
PUBLISHED = "published"      # live, comment still owed
COMMENTED = "commented"      # fully done
COMMENT_FAILED = "comment_failed"   # LIVE post, comment needs a human
FAILED = "failed"            # failed BEFORE publishing; nothing exists

DONE_STATES = (COMMENTED,)
# States where a post exists on LinkedIn. createPost must never run again.
PUBLISHED_STATES = (SCHEDULED, PUBLISHED, COMMENTED, COMMENT_FAILED)
# States whose rows are still waiting to be sent to Buffer. A held row is
# schedulable the moment a slot frees, with no human involvement.
SCHEDULABLE_STATES = (PENDING, HELD)

#: Failure stages. "prepare" means nothing left this machine. "createPost"
#: means Buffer answered and refused. "createPost_unknown" means the request
#: was in flight when it failed - a post MAY exist, and only Buffer knows.
STAGE_PREPARE = "prepare"
STAGE_CREATE_REFUSED = "createPost"
STAGE_CREATE_UNKNOWN = "createPost_unknown"
#: Buffer accepted the post and then the network refused to publish it.
#: A FOURTH failure shape, distinct from the three above: the request
#: succeeded, the post exists, and it still did not go out.
STAGE_PUBLISH = "publish"

#: What to tell an operator holding a row whose outcome is genuinely unknown.
UNKNOWN_POST_WARNING = (
    "The request to Buffer was IN FLIGHT when this failed, so a post may or "
    "may not have been created. Check Buffer for a post on this row's date "
    "BEFORE re-queueing it - re-queueing blind can publish it twice.")


class RowError(Exception):
    """A row that cannot be acted on. Reported, then skipped."""


# Columns that mean "the local image for this post". `image_path` is the
# documented name; `asset_path` is what the media-gen export writes, and a CSV
# straight out of that tool would otherwise publish every post text-only while
# reporting success - which is exactly what happened on 2026-09-09.
IMAGE_PATH_COLUMNS = ("image_path", "asset_path")


def row_image_path(row):
    """The local image path for a row, under any of its accepted names."""
    for column in IMAGE_PATH_COLUMNS:
        value = (row.get(column) or "").strip()
        if value:
            return value
    return ""


def unknown_columns(row):
    """Columns present in the row that the pipeline does not read.

    Reported rather than ignored. A column the pipeline has never heard of is
    usually a rename - `asset_path` for `image_path` - and silently dropping it
    turns a wrong result into one that looks right.
    """
    known = set(COLUMNS) | set(IMAGE_PATH_COLUMNS)
    return sorted(k for k in row if k and k not in known)


def row_key(row):
    """A stable identity for a row, independent of its position in the file.

    Content-hashed rather than row-numbered on purpose: inserting a row at the
    top of a CSV must not renumber every later row and make the whole calendar
    look unpublished. Editing a row's content does make it a new row, which is
    the honest reading - different text is a different post.
    """
    parts = []
    for c in COLUMNS:
        # The image is hashed by its RESOLVED path, so the same picture under
        # `image_path` or `asset_path` is recognised as the same row rather
        # than queued twice.
        parts.append(row_image_path(row) if c == "image_path"
                     else str(row.get(c) or "").strip())
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def row_preview(row, limit=160):
    """A short identifier for a row, for reports and the queue table.

    Failed rows never reached the point of composing their text, so without this
    a failure is identifiable only by its hash - which tells a human nothing
    about WHICH row of their CSV to go and fix.
    """
    text = (row.get("post_text") or "").strip() or (row.get("topic") or "").strip()
    if not text:
        return ""
    return text[:limit] + "…" if len(text) > limit else text


def parse_rows(text):
    """Parse CSV text into row dicts.

    Split out from read_rows so an uploaded file and a file on disk go through
    exactly the same parser - a second reader for the UI is how the two inputs
    would drift apart.
    """
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in ("date", "time_window")
               if c not in (reader.fieldnames or [])]
    if missing:
        raise RowError("CSV is missing required column(s): %s. Found: %s"
                       % (", ".join(missing), ", ".join(reader.fieldnames or [])))
    return [dict(r) for r in reader]


def read_rows(path):
    if not os.path.isfile(path):
        raise RowError("CSV not found: %s" % path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        return parse_rows(f.read())


class IdentityRefused(RuntimeError):
    """The account a run would post as is not the one configured.

    Raised rather than returned so the whole run stops. A caller that ignored a
    return value would schedule to the wrong account, and there is no undo.
    """


#: Every link on X is rewritten to a t.co shortlink of FIXED width, so a URL
#: costs the same however long it is. Counting the raw URL would reject posts
#: that fit and accept ones that do not.
X_URL_COST = 23

#: Anything link-shaped. Deliberately loose: over-counting a non-link is a
#: rejected row a human can look at, while under-counting produces a post
#: Buffer refuses at publish time.
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)


def x_counted_length(text):
    """Length as X counts it, with every URL charged at :data:`X_URL_COST`."""
    if not text:
        return 0
    return len(_URL_RE.sub("u" * X_URL_COST, text))


def compose_for_platform(text, tags, link=None, platform=None):
    """The post body, with the first-comment link placed per PLATFORM.

    WHERE THE LINK GOES IS A PLATFORM DECISION, not a new CSV column. The same
    row means "put this link with the post"; what differs is how each site lets
    you do that.

    * **linkedin** — the link goes in a FIRST COMMENT, posted by the browser
      pass after publication. Buffer's ``firstComment`` is paywalled on the free
      plan and rejects the whole post, so the browser owns that half.
    * **x** — the link goes in the BODY, one tweet carrying image, text and
      link together. X's threaded self-reply (``metadata.twitter.thread``) is
      free, but Phase 1's live run showed it DROPS THE BASE POST'S IMAGE, and
      trading the image for a threaded reply is the wrong trade here.
    """
    body = bc.compose_text(text, tags)
    link = (link or "").strip()
    if link and (platform or "").lower() == X_PLATFORM:
        body = "%s\n\n%s" % (body, link)
    return body


def validate_row(row, now=None, tz=None, platform=None):
    """Return a list of problems. Empty means the row is actionable.

    Everything is checked BEFORE any row is acted on, so a bad row is never
    half-processed - an image uploaded for a row whose date is malformed would
    be work done for a post that can never be created.
    """
    problems = []
    text = (row.get("post_text") or "").strip()
    topic = (row.get("topic") or "").strip()
    if not text and not topic:
        problems.append("post_text and topic are both empty - nothing to post")

    try:
        bc.due_at(row.get("date"), row.get("time_window"),
                  tz=tz or bc.posting_timezone(), now=now)
    except bc.BufferError as exc:
        problems.append(str(exc))

    image = row_image_path(row)
    if image:
        if not os.path.isfile(image):
            problems.append("image_path does not exist: %s" % image)
        else:
            try:
                image_host.read_image(image)
            except image_host.ImageHostError as exc:
                problems.append(str(exc))

    # X is the only platform with a POSTABLE hard limit. platform_policy says
    # so directly: X counts characters with hard_max_is_postable_limit=True,
    # while LinkedIn's hard_max is 70 WORDS with that flag False - a style
    # guide, not a rule Buffer enforces. So LinkedIn stays unbounded here and
    # nothing about its validation changes.
    #
    # Rejected, never truncated: a post cut mid-sentence is worse than one that
    # did not go out, and the link is at the END, so truncation would silently
    # drop the thing the row exists to share.
    if (platform or "").lower() == X_PLATFORM and text:
        policy = platform_policy.policy_for(X_PLATFORM)
        limit = policy.length.hard_max
        body = compose_for_platform(text, row.get("tags"),
                                    row.get("first_comment_link"), X_PLATFORM)
        used = x_counted_length(body)
        if used > limit:
            problems.append(
                "too long for X: %d characters against a %d limit (every link "
                "counts as %d, whatever its real length). Shorten post_text, "
                "tags or the link - this is never truncated automatically."
                % (used, limit, X_URL_COST))
    return problems


class PipelineState:
    """Per-row progress, so a re-run resumes instead of re-publishing."""

    def __init__(self, path=None, profile_name=None, platform=None):
        # Two platforms are two state files, for the same reason post_store
        # splits its stores: a row keyed the same way on both would let an X
        # run read LinkedIn's progress and decide a post already exists.
        # LinkedIn keeps the path it has always had, so nothing migrates.
        self.platform = (platform or pm.SCHEDULED_DEFAULT_PLATFORM).lower()
        if path:
            self.path = path
        elif self.platform == pm.SCHEDULED_DEFAULT_PLATFORM:
            # LinkedIn's call is UNCHANGED, down to the argument count, so its
            # path cannot move and nothing needs migrating.
            self.path = os.path.join(pm.get_data_dir(profile_name), STATE_NAME)
        else:
            self.path = os.path.join(
                pm.get_data_dir(profile_name, self.platform), STATE_NAME)
        self.rows = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            # Reading a corrupt state file as empty would re-publish every post
            # it recorded. Refuse instead.
            logger.error("pipeline state at %s is unreadable; refusing to treat "
                         "it as empty", self.path)
            raise
        return data.get("rows", {}) if isinstance(data, dict) else {}

    def get(self, key):
        return self.rows.get(key) or {"status": PENDING}

    def update(self, key, **fields):
        entry = self.rows.setdefault(key, {"status": PENDING})
        entry.update(fields)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.save()
        return entry

    def save(self):
        # dirname is "" for a bare filename, and os.makedirs("") raises. A
        # relative path is a legitimate way to point at a state file.
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rows": self.rows}, f, indent=2)
        os.replace(tmp, self.path)

    def has_post(self, key):
        """Does a real LinkedIn post already exist for this row?

        A post_id counts on its own, not only a published-looking status. The
        two are written together, but this is the guard against giving one row
        two posts - and a guard that trusts a status column to be consistent
        with the id beside it is trusting the thing it exists to double-check.
        Nothing can unpublish the second post.
        """
        entry = self.get(key)
        return bool(entry.get("post_id")) or entry.get("status") in PUBLISHED_STATES


def generate_text(topic, profile_name=None, generator=None):
    """Generate post text from a topic.

    The existing generator ADDS its result to the browser tool's PostQueue as a
    side effect. That queue is a different pipeline's inbox - leaving an entry
    there would let the browser poster publish this text a second time, by
    itself. So the entry it just created is removed again.
    """
    if generator is None:
        from .post_generator import PostGenerator
        generator = PostGenerator(profile_name=profile_name)

    before = {p.get("id") for p in generator.queue.list_queued()}
    post = generator.generate_thought_leadership(topic=topic)
    after = generator.queue.list_queued()
    for entry in after:
        if entry.get("id") not in before:
            generator.queue.remove(entry["id"])
            logger.debug("removed generated post %s from the browser queue",
                         entry.get("id"))
    text = (post or {}).get("text", "").strip()
    if not text:
        raise RowError("the generator returned no text for topic %r" % topic)
    return text


def profile_slug(url):
    """The vanity slug out of a LinkedIn profile URL, or None.

    Returns the ``/in/<slug>`` path segment only. Anything else - a login
    redirect, a checkpoint, a feed URL - has no slug and must not be treated
    as an identity.
    """
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return None
    parts = [p for p in (parsed.path or "").split("/") if p]
    for i, part in enumerate(parts):
        if part.lower() == "in" and i + 1 < len(parts):
            return unquote(parts[i + 1]).strip().lower() or None
    return None


def channel_handle(channel):
    """The account handle a Buffer channel actually posts as, or None.

    Buffer does not expose a field called "handle". ``name`` carries it for X
    (the spike's channel listing showed ``AI_Fun_times service=twitter``), and
    ``externalLink`` carries the profile URL, whose last segment is the same
    handle. Both are read, in that order, so the check does not depend on which
    one Buffer happens to populate for a given service.

    ``displayName`` is deliberately NOT used: it is a human-facing label ("AI
    Fun Times"), not an identity, and accepting it would let a guard pass on a
    string nobody posts as.
    """
    if not isinstance(channel, dict):
        return None
    name = (channel.get("name") or "").strip().lstrip("@")
    if name:
        return name
    link = (channel.get("externalLink") or "").strip().rstrip("/")
    if link:
        tail = link.split("/")[-1].strip().lstrip("@")
        if tail and "." not in tail:          # not a bare domain
            return tail
    return None


def verify_channel_identity(expect_slug, channel):
    """Does this Buffer channel post as ``expect_slug``? Pure; no network.

    The Buffer-side half of the guard, kept separate from fetching so it can be
    unit-tested against a dict and reused by any platform that schedules through
    Buffer. LinkedIn does not use it yet — see .dev/BACKLOG.md for why that is a
    later, separately-verified change and not a free win.

    Comparison is EXACT after case-folding, because X handles are
    case-insensitive but not otherwise fuzzy. Nothing substring-matches here:
    the LinkedIn guard learned that lesson when ``example-person`` matched
    ``example-person-011011``, a different human.
    """
    want = (expect_slug or "").strip().lstrip("@").lower()
    if not want:
        return False, ("no expected identity configured - refusing to act as "
                       "this channel")

    got = channel_handle(channel)
    if not got:
        return False, ("Buffer did not say which account this channel posts as "
                       "(no name or externalLink), so the identity cannot be "
                       "confirmed - refusing")

    if got.lower() == want:
        return True, got
    return False, ("channel posts as %r, expected %r - refusing to schedule"
                   % (got, expect_slug))


def verify_buffer_identity(expect_slug, channel_id, key=None, session=None,
                           fetch=None):
    """Fetch the channel and confirm it posts as ``expect_slug``.

    ONE request, once per run, before anything is created. A wrong channel id
    with a valid key would otherwise publish to the wrong account and there is
    no undo.

    ``fetch`` is injectable so this is testable without a network, the same way
    ``reconcile_published`` takes one.
    """
    fetch = fetch or bc.get_channel
    try:
        channel = fetch(channel_id, key=key, session=session)
    except Exception as exc:
        return False, ("could not confirm the Buffer channel's identity: %s"
                       % exc)
    return verify_channel_identity(expect_slug, channel)


def verify_identity(platform, expect_slug, poster=None, channel_id=None,
                    key=None, session=None, fetch_channel=None):
    """Confirm we are about to act as the account we mean to act as.

    Platform-dispatched, because the two platforms act at different moments and
    a guard must fire before ITS action, not before some other platform's:

    * **linkedin** drives a browser and acts when it comments, so the guard is
      the existing ``/in/me/`` check at comment time. Unchanged.
    * **x** never drives a browser. Its only action is the scheduled post, so
      the guard is a Buffer channel-handle check at SCHEDULE time, before any
      ``createPost``.

    **An empty slug now fails CLOSED, on both paths.** It used to return True
    with "no expected identity configured", which meant an unconfigured profile
    had no protection at all from the one control standing between this tool and
    posting as the wrong real person. A guard whose default is "allow" is a
    guard that is off. The CLI already refused to run a comment pass without a
    slug; that refusal was the only thing making the open default survivable,
    and it did not cover any other caller.
    """
    want = (expect_slug or "").strip()
    if not want:
        return False, ("no expected identity configured - refusing to act. Set "
                       "scheduled_posting.identity_slug (or the platform's "
                       "overlay) for this profile.")

    platform = (platform or pm.SCHEDULED_DEFAULT_PLATFORM).strip().lower()

    if platform != pm.SCHEDULED_DEFAULT_PLATFORM:
        if not channel_id:
            return False, ("no Buffer channel to check the %s identity against "
                           "- refusing" % platform)
        return verify_buffer_identity(want, channel_id, key=key,
                                      session=session, fetch=fetch_channel)

    if poster is None:
        return False, ("no browser session to confirm the LinkedIn identity "
                       "with - refusing")
    return _verify_identity_linkedin(poster, want)


def _verify_identity_linkedin(poster, expect_slug):
    """Confirm the browser session is the account we mean to act as.

    The DEFAULT profile is the real account. Commenting from the wrong identity
    on a live post cannot be undone, so this runs before any browser action and
    a mismatch aborts the comment rather than guessing.

    The comparison is against the ``/in/<slug>`` segment, EXACTLY. It used to
    ask whether the configured slug appeared anywhere in the URL, which is a
    substring test: ``in`` matched every profile on the site, and a truncated
    vanity name like ``example-person`` matched ``example-person-011011`` - a
    different
    person. The one guard standing between this tool and commenting as the
    wrong real human cannot be the loosest comparison available.
    """
    # Reached only with a non-empty slug: the dispatcher refuses an empty one
    # for every platform, so this no longer has an open default.
    want = expect_slug.strip().lower().rstrip("/")
    # Tolerate a whole URL in the config as well as a bare slug.
    want = profile_slug(want) or want
    try:
        poster.driver.get("https://www.linkedin.com/in/me/")
        import time
        time.sleep(3)
        url = (poster.driver.current_url or "")
    except Exception as exc:
        return False, "could not resolve the logged-in identity: %s" % exc

    got = profile_slug(url)
    if got is None:
        return False, ("could not read a profile slug from %r - refusing to "
                       "comment. (Not logged in, or LinkedIn redirected to a "
                       "checkpoint.)" % url)
    if got == want:
        return True, url
    return False, ("logged in as %r, expected %r - refusing to comment"
                   % (got, want))


# ─── queueing: rows enter as PENDING, before anything is scheduled ───────────

def queue_rows(rows, state, now=None, tz=None):
    """Validate every row, then store only the ones that passed.

    Returns ``(accepted, rejected, skipped, warnings)``.

    Validation runs over the WHOLE batch before a single row is written, so a
    file with a bad row in the middle cannot leave half a calendar queued and
    half rejected depending on ordering.

    Queueing is not scheduling. A row here is PENDING: it exists in the queue
    and nothing has been sent to Buffer. The row itself is stored alongside the
    status, because that is what the schedule pass later needs to act on - a
    PENDING entry that only remembered its hash could never be scheduled.
    """
    tz = tz or bc.posting_timezone()
    accepted, rejected, skipped = [], [], []
    warnings = []

    if rows:
        unknown = unknown_columns(rows[0])
        if unknown:
            warnings.append(
                "These columns are not read by the pipeline and were ignored: %s. "
                "If one of them holds the image, rename it to image_path."
                % ", ".join(unknown))
        if not any(row_image_path(r) for r in rows):
            present = [c for c in IMAGE_PATH_COLUMNS if c in rows[0]]
            if not present:
                warnings.append(
                    "No image column found (expected one of: %s), so every row "
                    "will publish WITHOUT an image."
                    % ", ".join(IMAGE_PATH_COLUMNS))

    checked = []
    for index, row in enumerate(rows, start=1):
        problems = validate_row(row, now=now, tz=tz)
        checked.append((index, row, problems))

    for index, row, problems in checked:
        rkey = row_key(row)
        if problems:
            rejected.append({"index": index, "key": rkey,
                             "preview": row_preview(row), "errors": problems})
            continue

        if rkey in state.rows:
            # Already known, in ANY state. Never overwrite:
            #   * a row with a post must not be reset to PENDING and posted twice
            #   * a row already PENDING is not "accepted" again - reporting it as
            #     accepted would tell the user something entered the queue when
            #     nothing did
            existing = state.get(rkey)
            status = existing.get("status")
            skipped.append({"index": index, "key": rkey,
                            "preview": row_preview(row), "status": status,
                            "reason": ("this row already has a post"
                                       if existing.get("post_id")
                                       else "this row is already in the queue as %s"
                                       % status)})
            continue

        state.update(rkey, status=PENDING, row=dict(row),
                     text=row_preview(row),
                     first_comment_link=(row.get("first_comment_link") or "").strip(),
                     errors=[])
        accepted.append({"index": index, "key": rkey,
                         "preview": row_preview(row)})

    return accepted, rejected, skipped, warnings


def pending_rows(state):
    """Source rows of everything still waiting to be sent to Buffer.

    Includes HELD as well as PENDING: a held row is not waiting on a person,
    only on a slot, so the next schedule attempt should pick it up without
    anyone re-queueing it.

    Ordered by due date, so when slots are scarce the earliest post goes first
    rather than whichever row happened to be added first.
    """
    waiting = [(k, v) for k, v in state.rows.items()
               if v.get("status") in SCHEDULABLE_STATES and v.get("row")]
    waiting.sort(key=_waiting_sort_key)
    return [dict(v.get("row") or {}) for _k, v in waiting]


def row_image_column(row):
    """Which column actually supplied this row's image path, or None.

    The UI shows this so "text-only" can be told apart from "the image column
    is not the one you think". A calendar whose image column is present but
    empty on every row looks identical, in the queue, to a text-only calendar -
    until it publishes.
    """
    for column in IMAGE_PATH_COLUMNS:
        if (row.get(column) or "").strip():
            return column
    return None


def _waiting_sort_key(kv):
    """Earliest post first: date, then window, then when it was queued."""
    row = kv[1].get("row") or {}
    return (row.get("date") or "9999", row.get("time_window") or "",
            kv[1].get("updated_at") or "")


def held_rows(state):
    """Source rows of everything HELD - correct rows waiting only on a slot.

    The drain feeder acts on these and nothing else. A PENDING row has never
    been through the confirmation dialog that names the account it will post
    as; a HELD row already has, and was stopped only by Buffer's plan limit.
    Feeding PENDING rows automatically would publish, on a timer, posts the
    operator never confirmed - so the drain deliberately cannot.
    """
    waiting = [(k, v) for k, v in state.rows.items()
               if v.get("status") == HELD and v.get("row")]
    waiting.sort(key=_waiting_sort_key)
    return [dict(v.get("row") or {}) for _k, v in waiting]


#: Statuses a row can be in and still be owed a first comment. Matches
#: comment_pass's own retry set MINUS SCHEDULED: a SCHEDULED row normally
#: has no permalink yet (comment_pass still has to poll Buffer for one), so
#: it can usually never pass the permalink check below anyway - keeping it
#: out of this set makes that the common case explicit. It is not the ONLY
#: thing excluding a permalink-less row, though: comment_pass's identity-
#: guard failure path marks EVERY pending row COMMENT_FAILED in one sweep
#: (see comment_pass), including a still-permalink-less SCHEDULED one - for
#: that row, it really is the permalink check below doing the excluding,
#: this status filter alone would have let it through.
FIRST_COMMENT_OWED_STATES = (PUBLISHED, COMMENT_FAILED)


def rows_awaiting_first_comment(state, ledger):
    """(key, entry) pairs for rows whose first comment is still owed.

    A row qualifies when its status is PUBLISHED or COMMENT_FAILED, it
    carries a first_comment_link, has a permalink, and that permalink is not
    yet in the FirstCommentLedger. The permalink check is what actually
    excludes a still-SCHEDULED row that an identity-guard failure marked
    COMMENT_FAILED before it ever got a permalink (see
    FIRST_COMMENT_OWED_STATES and comment_pass) - the status filter alone
    would let it through. These are exactly the conditions comment_pass
    itself checks before calling post_first_comment, exposed here so the
    first-comment drain (first_comment_drain.py) can ask "is there anything
    waiting on its first comment" without driving a browser or calling
    Buffer - the same cheap-test-first shape post_drain.py uses for
    "nothing held".

    COMMENT_FAILED is included so a row that failed once (a transient login
    problem, an identity-guard refusal, a thread the verifier could not
    read) is retried by the drain rather than stranded until someone runs
    the CLI by hand - comment_pass itself already retries COMMENT_FAILED
    rows on its own next sweep, and the drain disagreeing with that would
    silently narrow what gets cleared automatically. The FirstCommentLedger
    - not the status - remains the only thing that can ever say "this one is
    truly done": a COMMENT_FAILED row with a ledger entry already (it posted,
    then failed on a LATER call for an unrelated reason, or the ledger write
    and the status write raced) is excluded exactly like a PUBLISHED one
    would be, never double-commented just because its status still reads
    COMMENT_FAILED.

    Returns full state entries, not just the source row (unlike held_rows/
    pending_rows), because the caller needs ``updated_at`` to judge how long
    a row has been eligible - the first-comment drain's delay is measured
    from it.
    """
    out = []
    for key, entry in state.rows.items():
        if entry.get("status") not in FIRST_COMMENT_OWED_STATES:
            continue
        link = (entry.get("first_comment_link") or "").strip()
        if not link:
            continue
        permalink = entry.get("permalink")
        if not permalink:
            continue
        if ledger.already_posted(permalink):
            continue
        out.append((key, entry))
    return out


def delete_row(state, key):
    """Remove a row from OUR queue. Returns the removed entry, or None.

    This deletes a record, never a post. A row that has a post_id refers to
    something that exists on Buffer and possibly on LinkedIn, and nothing here
    can take that back - the caller is responsible for saying so plainly before
    offering the action.
    """
    entry = state.rows.pop(key, None)
    if entry is None:
        return None
    state.save()
    return entry


def delete_rows(state, keys, confirm_live=False):
    """Delete many rows in one call, looping :func:`delete_row`.

    ALL-OR-NOTHING on the live-row guard: if any selected row has a real post
    and ``confirm_live`` is not set, NOTHING is deleted - not even the
    harmless rows in the same batch. The single-row DELETE route's
    confirm_live rule (one live row needs an explicit, informed yes) must not
    become bypassable by hiding a live row inside a larger selection that is
    mostly safe.

    Once past that gate, each row is deleted independently and a failure on
    one key must not stop the rest: the loop reports per-key outcomes rather
    than raising, so a batch of 20 with one bad key still removes the other
    19 and says which one did not go, rather than stopping silently or
    claiming success for the whole batch.

    Returns ``(results, live_without_confirm)``:

    * ``live_without_confirm`` - ``[{key, post_id}, ...]`` for every row that
      blocked the whole batch. Empty when nothing was blocked.
    * ``results`` - ``[{key, status, had_post, post_id, error}, ...]``, one
      entry per unique key, ``status`` one of ``"deleted"``, ``"not_found"``
      or ``"error"``. Empty when the batch was blocked by the guard above -
      a blocked batch has no outcomes to report because nothing ran.

    Duplicate keys in ``keys`` are deduplicated, order preserved; deleting the
    same key twice would mean nothing.
    """
    seen = []
    for k in keys:
        if k not in seen:
            seen.append(k)

    if not confirm_live:
        live = [{"key": k, "post_id": state.get(k).get("post_id")}
                for k in seen if k in state.rows and state.has_post(k)]
        if live:
            return [], live

    results = []
    for k in seen:
        if k not in state.rows:
            results.append({"key": k, "status": "not_found", "had_post": False,
                            "post_id": None,
                            "error": "no such row in the queue"})
            continue
        had_post = state.has_post(k)
        post_id = state.get(k).get("post_id")
        try:
            removed = delete_row(state, k)
        except Exception as exc:
            results.append({"key": k, "status": "error", "had_post": had_post,
                            "post_id": post_id, "error": str(exc)})
            continue
        if removed is None:
            # Raced with something else that deleted it first, between the
            # membership check above and this call.
            results.append({"key": k, "status": "not_found",
                            "had_post": had_post, "post_id": post_id,
                            "error": "no such row in the queue"})
            continue
        results.append({"key": k, "status": "deleted", "had_post": had_post,
                        "post_id": post_id, "error": None})
    return results, []


#: Rows worth asking Buffer about. A row Buffer has already published tells us
#: nothing new on a second look, and a row with no post id has nothing to look
#: up - so neither is ever re-polled.
RECONCILABLE_STATES = (SCHEDULED,)


def reconcile_published(state, channel_id, key=None, session=None, fetch=None,
                        limit=100, platform=None):
    """Ask Buffer what actually happened to rows we still believe are SCHEDULED.

    Buffer publishes on its own timetable. Nothing in this tool notices unless
    something asks, so a post that went out days ago sat in the queue reading
    "created in Buffer, not published yet" until a background loop happened to
    be running. That made the truth depend on a toggle.

    This is the ask. It is cheap on purpose: ONE batched request covers every
    row, and only rows in :data:`RECONCILABLE_STATES` are considered, so the
    cost does not grow as the calendar fills up with finished work.

    Returns a list of ``{key, post_id, status, permalink}`` for rows that moved.
    Rows Buffer does not mention are left exactly as they were - silence from
    the API is not evidence that something did not publish.
    """
    waiting = [(k, v) for k, v in state.rows.items()
               if v.get("status") in RECONCILABLE_STATES and v.get("post_id")]
    if not waiting:
        return []

    platform = (platform or pm.SCHEDULED_DEFAULT_PLATFORM).lower()

    # CREATE-SUCCESS IS NOT PUBLISH-SUCCESS ON X (Phase 1, live). A post that
    # createPost accepted can still fail at publish time - the picsum run came
    # back HTTP 405 "Media URL not publicly accessible" AFTER a clean create.
    # A reconcile that only asks about `sent` never learns that, and the row
    # sits reading "scheduled" forever while the truth is that it failed.
    #
    # So X asks about errors too. LinkedIn's query is untouched: adding a
    # status to it would change which posts the production path reconciles.
    statuses = ("sent",)
    if platform != pm.SCHEDULED_DEFAULT_PLATFORM:
        statuses = ("sent", "error")

    fetch = fetch or bc.posts_by_status
    sent = fetch(channel_id, statuses=statuses, limit=limit, key=key,
                 session=session) or {}

    changed = []
    for rkey, entry in waiting:
        node = sent.get(entry.get("post_id"))
        if not node:
            continue

        if (node.get("status") or "").lower() == "error":
            # Buffer tried and the network refused it. FAILED, not published:
            # calling it published would send the comment pass chasing a post
            # that does not exist, and would hide the reason from the operator.
            reason = ((node.get("error") or {}).get("message")
                      if isinstance(node.get("error"), dict)
                      else node.get("error")) or "Buffer reported status=error"
            state.update(rkey, status=FAILED, stage=STAGE_PUBLISH,
                         errors=["publish failed: %s" % reason])
            changed.append({"key": rkey, "post_id": entry.get("post_id"),
                            "status": FAILED, "permalink": None,
                            "error": reason})
            logger.warning("reconciled %s: Buffer FAILED to publish it (%s)",
                           rkey, reason)
            continue

        permalink = (node.get("externalLink") or "").strip() or None
        fields = {"status": PUBLISHED,
                  "published_at": node.get("sentAt") or entry.get("published_at")}
        # Never overwrite a permalink we already hold with a blank one: the
        # link and the status are not written atomically by Buffer, so a post
        # can read `sent` a moment before externalLink appears.
        if permalink:
            fields["permalink"] = permalink
        state.update(rkey, **fields)
        changed.append({"key": rkey, "post_id": entry.get("post_id"),
                        "status": PUBLISHED,
                        "permalink": permalink or entry.get("permalink")})
        logger.info("reconciled %s: Buffer published it at %s (%s)",
                    rkey, node.get("sentAt"), permalink or "no link yet")
    return changed


# ─── pass 1: create the posts ────────────────────────────────────────────────

def schedule_pass(rows, channel_id, state, rng=None, key=None, session=None,
                  upload=None, generate=None, profile_name=None, now=None,
                  max_new=None, slot_limit=None, platform=None,
                  identity_slug=None, fetch_channel=None,
                  allow_production=False):
    """Validate and create every post that does not already have one.

    ``max_new`` is the number of scheduled-post slots Buffer actually has free.
    Rows beyond it are marked HELD rather than sent, because sending them would
    produce a wall of LimitReachedError - one rejection per row, none of which
    means anything is wrong with the row.

    ``None`` means no budget was supplied and every row is attempted; a
    LimitReachedError still maps to HELD rather than FAILED.
    """
    results = []
    tz = bc.posting_timezone()
    platform = (platform or pm.SCHEDULED_DEFAULT_PLATFORM).lower()

    # THE IDENTITY GUARD LIVES HERE, not in the CLI.
    #
    # Phase 3 wired it into tools/run_scheduled_posts.py, which covered the one
    # caller that existed. This is the chokepoint every X schedule must pass
    # through - the CLI, the Phase 5 drain, and anything added later - so no
    # future caller can reach createPost without it. A guard one caller can
    # skip is a guard that will eventually be skipped.
    #
    # Once per CALL, not per row: one request, before anything is created.
    #
    # LinkedIn is deliberately not gated here - see .dev/BACKLOG.md. Its
    # schedule path is byte-identical, this branch simply does not run.
    if platform != pm.SCHEDULED_DEFAULT_PLATFORM:
        # PRODUCTION GATE, before verify_identity - which itself is a live
        # Buffer call. verify_identity answers "is this the right channel";
        # this answers "did the caller declare authorization to touch a
        # declared-production identity" (docs/ARCHITECTURE.md §9). Both must
        # run; this one runs first so a declared-production identity never
        # reaches Buffer at all, not even for the identity check itself.
        pm.check_declared_production(
            identity_slug, "schedule a Buffer post on %s" % platform,
            allow_production=allow_production)

        ok, detail = verify_identity(platform, identity_slug,
                                     channel_id=channel_id, key=key,
                                     session=session,
                                     fetch_channel=fetch_channel)
        if not ok:
            raise IdentityRefused(
                "refusing to schedule on %s: %s. Nothing was created."
                % (platform, detail))
        logger.info("identity confirmed on %s: channel posts as %s",
                    platform, detail)

    created = 0
    held = 0
    for row in rows:
        rkey = row_key(row)
        entry = state.get(rkey)
        result = {"key": rkey, "row": row, "status": entry.get("status")}

        # THE GUARD THAT MATTERS: a row with a post never gets another one.
        if state.has_post(rkey):
            result["status"] = entry.get("status")
            result["post_id"] = entry.get("post_id")
            result["permalink"] = entry.get("permalink")
            result["note"] = "already has a post - not re-created"
            results.append(result)
            continue

        problems = validate_row(row, now=now, tz=tz, platform=platform)
        if problems:
            state.update(rkey, status=FAILED, stage="validate", errors=problems,
                         text=row_preview(row))
            result.update(status=FAILED, stage="validate", errors=problems)
            results.append(result)
            continue

        if max_new is not None and created >= max_new:
            # Validated and fine - there is simply no room. Held, not failed.
            held += 1
            # The number quoted must be the PLAN LIMIT, not the free count.
            # "Buffer allows 2 scheduled posts" when the cap is 10 and 2 happen
            # to be free reads as a much smaller plan than the operator has.
            ahead = held - 1
            if slot_limit:
                note = ("waiting for a slot - Buffer allows %d scheduled posts "
                        "at once; %d ahead of this one" % (slot_limit, ahead))
            else:
                note = ("waiting for a slot - %d ahead of this one" % ahead)
            state.update(rkey, status=HELD, row=dict(row),
                         text=row_preview(row), hold_reason=note, errors=[])
            result.update(status=HELD, note=note)
            results.append(result)
            continue

        in_flight = False
        try:
            text = (row.get("post_text") or "").strip()
            if not text:
                text = (generate or generate_text)(
                    (row.get("topic") or "").strip(), profile_name=profile_name)
            # On X the link rides in the body; on LinkedIn it stays out and
            # the browser pass comments it. Same row, different placement.
            body = compose_for_platform(text, row.get("tags"),
                                        row.get("first_comment_link"),
                                        platform)

            image_url = None
            image = row_image_path(row)
            if image:
                image_url = (upload or image_host.upload_image)(image)

            when = bc.due_at(row.get("date"), row.get("time_window"),
                             rng=rng, tz=tz, now=now)
            # Everything above this line is local. From here until create_post
            # returns, a failure cannot tell us whether Buffer committed.
            in_flight = True
            post = bc.create_post(channel_id, body, image_url, when,
                                  key=key, session=session)
            in_flight = False
        except bc.BufferAtCapacity as exc:
            # Buffer said it is full. The row is correct; it waits.
            held += 1
            note = "waiting for a slot - %s" % exc
            state.update(rkey, status=HELD, row=dict(row),
                         text=row_preview(row), hold_reason=note, errors=[])
            result.update(status=HELD, note=note)
            results.append(result)
            continue
        except Exception as exc:
            # Three different failures that must NOT read alike:
            #
            #   prepare            - nothing left this machine. Retry freely.
            #   createPost         - Buffer answered and refused. Nothing
            #                        exists; it says so itself.
            #   createPost_unknown - the request was in flight. A post may
            #                        exist. Retrying blind can publish twice,
            #                        and nothing can unpublish the second one.
            #
            # The old code called the third one "prepare", which reads as
            # "nothing was sent" - the most dangerous thing it could say.
            errors = [str(exc)]
            if isinstance(exc, bc.BufferRejected):
                stage = STAGE_CREATE_REFUSED
            elif in_flight:
                stage = STAGE_CREATE_UNKNOWN
                errors.append(UNKNOWN_POST_WARNING)
            else:
                stage = STAGE_PREPARE
            state.update(rkey, status=FAILED, stage=stage, errors=errors,
                         text=row_preview(row))
            result.update(status=FAILED, stage=stage, errors=errors)
            results.append(result)
            logger.error("row %s failed at %s: %s", rkey, stage, exc)
            continue

        created += 1
        state.update(rkey, status=SCHEDULED, hold_reason=None,
                     post_id=post.get("id"),
                     due_at=post.get("dueAt") or when, image_url=image_url,
                     text=body, first_comment_link=(row.get("first_comment_link")
                                                    or "").strip(), errors=[])
        result.update(status=SCHEDULED, post_id=post.get("id"),
                      due_at=post.get("dueAt") or when, image_url=image_url)
        results.append(result)
        logger.info("row %s scheduled: post %s due %s", rkey, post.get("id"),
                    post.get("dueAt") or when)
    return results


# ─── pass 2: comment the ones that have published ────────────────────────────

def comment_pass(state, key=None, session=None, poster=None, ledger=None,
                 profile_name=None, expect_slug=None, wait=False,
                 wait_timeout=1800, platform=pm.SCHEDULED_DEFAULT_PLATFORM,
                 allow_production=False):
    """Sweep for published posts and add their first comments.

    Non-blocking by default: a row whose post has not published yet is left
    alone for the next sweep. ``wait=True`` blocks for a row that is due
    imminently, which is what an end-to-end test wants and a timer does not.
    """
    results = []
    pending = [(k, e) for k, e in state.rows.items()
               if e.get("status") in (SCHEDULED, PUBLISHED, COMMENT_FAILED)]
    if not pending:
        return results

    identity_checked = False
    for rkey, entry in pending:
        out = {"key": rkey, "post_id": entry.get("post_id"),
               "permalink": entry.get("permalink")}
        link = (entry.get("first_comment_link") or "").strip()

        permalink = entry.get("permalink")
        if not permalink:
            try:
                if wait:
                    permalink = bc.wait_for_publish(entry["post_id"], key=key,
                                                    session=session,
                                                    timeout=wait_timeout)
                else:
                    post = bc.get_post(entry["post_id"], key=key, session=session)
                    if post.get("status") != "sent":
                        out.update(status=entry.get("status"),
                                   note="not published yet (%s)" % post.get("status"))
                        results.append(out)
                        continue
                    permalink = (post.get("externalLink") or "").strip()
                    if not permalink:
                        out.update(status=entry.get("status"),
                                   note="sent but no externalLink yet")
                        results.append(out)
                        continue
            except bc.BufferError as exc:
                state.update(rkey, stage="poll", errors=[str(exc)])
                out.update(status=entry.get("status"), note=str(exc))
                results.append(out)
                continue
            state.update(rkey, status=PUBLISHED, permalink=permalink)
            out["permalink"] = permalink

        if not link:
            state.update(rkey, status=COMMENTED, comment="skipped - no link")
            out.update(status=COMMENTED, note="published; no first_comment_link")
            results.append(out)
            continue

        # The post is LIVE from here on. Nothing below may re-publish.
        if poster is None:
            from .comment_poster import LinkedInCommentPoster
            poster = LinkedInCommentPoster(profile_name=profile_name,
                                            allow_production=allow_production)
            poster.setup_driver()
            if not poster.login():
                out.update(status=COMMENT_FAILED, note="could not log in")
                state.update(rkey, status=COMMENT_FAILED,
                             errors=["could not log in to comment"])
                results.append(out)
                continue

        if not identity_checked:
            ok, detail = verify_identity(platform, expect_slug,
                                         poster=poster)
            if not ok:
                logger.error("IDENTITY GUARD: %s", detail)
                for k2, _ in pending:
                    state.update(k2, status=COMMENT_FAILED,
                                 errors=["identity guard: %s" % detail])
                out.update(status=COMMENT_FAILED, note="identity guard: %s" % detail)
                results.append(out)
                return results
            identity_checked = True
            logger.info("identity confirmed: %s", detail)

        ledger = ledger if ledger is not None else fc.FirstCommentLedger(
            profile_name=profile_name)
        res = fc.post_first_comment(permalink, link, poster=poster,
                                    ledger=ledger)
        if res["status"] in (fc.POSTED, fc.ALREADY, fc.SKIPPED):
            state.update(rkey, status=COMMENTED, comment=res["status"], errors=[])
            out.update(status=COMMENTED, note=res["status"])
        else:
            # The post stays live. This is a manual fixup, never a re-post.
            state.update(rkey, status=COMMENT_FAILED, errors=[res.get("error")])
            out.update(status=COMMENT_FAILED, note=res.get("error"))
        results.append(out)
    return results


# ─── reporting ───────────────────────────────────────────────────────────────

def summarize(schedule_results, comment_results, invalid=None):
    """A human-readable account of what happened to every row."""
    lines = []
    by_key = {r["key"]: r for r in (schedule_results or [])}
    for r in comment_results or []:
        by_key.setdefault(r["key"], {}).update(r)

    lines.append("=" * 72)
    lines.append("RUN SUMMARY")
    lines.append("=" * 72)
    counts = {}
    for key, r in by_key.items():
        status = r.get("status") or PENDING
        counts[status] = counts.get(status, 0) + 1
        row = r.get("row") or {}
        label = (row.get("post_text") or row.get("topic") or "")[:44]
        lines.append("")
        lines.append("row %s  [%s]  %s" % (key, status.upper(), label))
        if r.get("post_id"):
            lines.append("    buffer post : %s  due %s"
                         % (r["post_id"], r.get("due_at", "?")))
        if r.get("permalink"):
            lines.append("    published   : %s" % r["permalink"])
        if r.get("image_url"):
            lines.append("    image       : %s" % r["image_url"])
        if r.get("note"):
            lines.append("    note        : %s" % r["note"])
        for err in r.get("errors") or []:
            lines.append("    ERROR       : %s" % err)
        if status == COMMENT_FAILED:
            lines.append("    ACTION      : the post is LIVE. Add the comment by "
                         "hand; do NOT re-run the post.")
        if status == FAILED:
            lines.append("    ACTION      : nothing was published. Fix the row "
                         "and re-run.")

    for row, problems in (invalid or []):
        lines.append("")
        lines.append("row (invalid)  %s" % (row.get("post_text")
                                            or row.get("topic") or "")[:44])
        for p in problems:
            lines.append("    INVALID     : %s" % p)

    lines.append("")
    lines.append("-" * 72)
    lines.append("totals: " + ", ".join("%s=%d" % kv for kv in sorted(counts.items()))
                 or "nothing to do")
    if invalid:
        lines.append("invalid rows skipped: %d" % len(invalid))
    return "\n".join(lines)
