"""Post the first comment on a post Buffer already published.

Phase 4 of the scheduled-posting hybrid, and the only browser step in it.
Buffer's ``firstComment`` is paid-plan only — and worse, sending it on the free
plan rejects the entire post rather than dropping the field — so the comment is
added afterwards, through the browser, on the permalink Phase 3 captured.

**The post is already live and cannot be unpublished.** Everything here is built
around that one fact:

* A comment failure is reported, never retried into a re-post. There is nothing
  to re-post — the post exists. Re-running the row must not create a second one.
* Results come back as a structured outcome rather than an exception, so a
  caller cannot mistake "the comment did not land" for "the row failed" and undo
  work that actually succeeded. The post standing without its comment is a
  manual fixup, not a pipeline failure.
* A permalink gets exactly one first comment. The ledger is the first line of
  defense, but a comment can land on LinkedIn while ``post_comment()``'s own
  verifier reports False (a false-negative, MAINTENANCE §6.5) — leaving no
  ledger entry at all. So before ever calling ``post_comment()``, this also
  asks the thread itself via ``first_comment_already_present()``
  (MAINTENANCE §6.6's dated correction): a positive finding — our link/text
  found inside a REAL rendered comment entry, never the post card or the
  author byline — records the ledger entry and skips the post. This is
  deliberately NOT the main engagement path's ``already_commented_here()``:
  that check's "'• You'" marker means "we already commented" on someone
  else's post, but means only "this post is ours" here, since this path's
  post is always our own. Reusing it (Dispatch 31) produced a confirmed live
  false positive before this correction.

It composes ``LinkedInCommentPoster``'s primitives rather than calling
``post_single_comment``, for two reasons: that method always likes the post, and
we do not self-like; and it writes to the engagement tool's own posted-ledger,
which answers a different question. The primitives it does use —
``navigate_to_post`` and ``post_comment`` — are the ones hardened in Phase 0b,
where ``comment_submit_button`` got the ``state_witness`` that makes a renamed
hook fail loudly instead of reporting "not checked".
"""

import json
import logging
import os
from datetime import datetime, timezone

from . import profile_manager as pm

logger = logging.getLogger(__name__)

LEDGER_NAME = "scheduled_first_comments.json"

# Outcome kinds. Only POSTED means the comment is on the post.
POSTED = "posted"
ALREADY = "already_posted"
SKIPPED = "skipped_no_link"
FAILED = "failed"


class FirstCommentResult(dict):
    """The outcome of one attempt, as data rather than an exception.

    Deliberately not an exception: the caller must be able to record "the post
    is live but its comment did not land" without any code path that looks like
    the row failed and should be redone.
    """

    @property
    def ok(self):
        return self["status"] in (POSTED, ALREADY, SKIPPED)

    @property
    def needs_human(self):
        return self["status"] == FAILED


def _result(status, permalink, link=None, error=None):
    out = FirstCommentResult(status=status, permalink=permalink, link=link,
                             error=error, at=datetime.now(timezone.utc).isoformat())
    return out


class FirstCommentLedger:
    """Which permalinks have already had their first comment posted.

    Its own file, separate from the engagement tool's ``posted_comments``. The
    two answer different questions — "did we already reach out to someone
    else's post" versus "did our own scheduled post get its first comment" — and
    sharing one ledger would let either silently suppress the other.
    """

    def __init__(self, path=None, profile_name=None):
        self.path = path or os.path.join(
            pm.get_data_dir(profile_name), LEDGER_NAME)
        self._entries = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            # A corrupt ledger must not silently read as "nothing posted yet",
            # which would double-comment every post it had recorded.
            logger.error("first-comment ledger at %s is unreadable; refusing to "
                         "treat it as empty", self.path)
            raise
        return data.get("commented", {}) if isinstance(data, dict) else {}

    def already_posted(self, permalink) -> bool:
        return permalink in self._entries

    def record(self, permalink, link):
        self._entries[permalink] = {
            "link": link,
            "commented_at": datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"commented": self._entries}, f, indent=2)
        os.replace(tmp, self.path)


def post_first_comment(permalink, comment_link, poster=None, ledger=None,
                       profile_name=None):
    """Comment ``comment_link`` on ``permalink``. Returns a FirstCommentResult.

    Never raises for a comment problem, and never re-publishes anything.
    """
    permalink = (permalink or "").strip()
    comment_link = (comment_link or "").strip()

    if not permalink:
        return _result(FAILED, permalink, comment_link,
                       "no permalink - Phase 3 must supply the published URL")

    # A row without a link simply does not get a first comment. That is a
    # legitimate shape, not a failure.
    if not comment_link:
        logger.info("No first_comment_link for %s - skipping the comment",
                    permalink)
        return _result(SKIPPED, permalink)

    ledger = ledger if ledger is not None else FirstCommentLedger(
        profile_name=profile_name)
    if ledger.already_posted(permalink):
        logger.info("First comment already posted on %s - not commenting again",
                    permalink)
        return _result(ALREADY, permalink, comment_link)

    owns_poster = poster is None
    if owns_poster:
        from .comment_poster import LinkedInCommentPoster
        poster = LinkedInCommentPoster(profile_name=profile_name)

    try:
        if owns_poster:
            poster.setup_driver()
            if not poster.login():
                return _result(FAILED, permalink, comment_link,
                               "could not log in to LinkedIn")

        if not poster.navigate_to_post(permalink):
            return _result(FAILED, permalink, comment_link,
                           "could not open the published post at %s" % permalink)

        # BEFORE typing: is this comment already on the thread? NOT
        # already_commented_here() - MEASURED 2026-10-18 (Dispatch 32,
        # MAINTENANCE §6.6's dated correction): that check's "• You" marker
        # means "we left a comment here" on someone else's post, but "this
        # post is ours" on our own - and this path's post is always ours, so
        # the marker alone is present before any comment exists. Reusing it
        # (Dispatch 31) produced a confirmed false positive on a real post
        # (urn:li:share:0000000000000000000 stands in for it here, per
        # PRINCIPLES §10 - no real identifiers in committed source): row
        # marked COMMENTED, ledger written, zero actual comments.
        # first_comment_already_present() requires the link/text to be found
        # INSIDE a real rendered comment entry, never the post card or the
        # author byline. Conservative in the same safe direction as §6.6: an
        # unreadable thread returns False and posting proceeds.
        already, why = poster.first_comment_already_present(comment_link)
        if already:
            logger.info("First comment already on %s - %s", permalink, why)
            ledger.record(permalink, comment_link)
            return _result(ALREADY, permalink, comment_link)

        # NOTE: like_post() is deliberately not called. This is our own post;
        # self-liking is not the behaviour we want.
        if not poster.post_comment(comment_link):
            return _result(FAILED, permalink, comment_link,
                           "the comment box or submit button did not accept the "
                           "comment - the post is LIVE and uncommented")

        ledger.record(permalink, comment_link)
        logger.info("First comment posted on %s", permalink)
        return _result(POSTED, permalink, comment_link)

    except Exception as exc:
        # Swallowed on purpose, and reported as data. An exception escaping here
        # could be caught by a caller that treats row failure as "redo the row",
        # and the row's post is already published.
        logger.error("First comment failed on %s: %s", permalink, exc,
                     exc_info=True)
        return _result(FAILED, permalink, comment_link, str(exc))
    finally:
        if owns_poster and getattr(poster, "driver", None):
            try:
                poster.driver.quit()
            except Exception:
                logger.debug("browser quit failed", exc_info=True)
