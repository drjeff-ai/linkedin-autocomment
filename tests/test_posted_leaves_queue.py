"""A comment that was actually posted must leave the review queue.

COMMENTED is not written at posting time; it is reconciled from
posting_progress.json, the authoritative ledger of what really went out, by
matching URLs. The ledger records whatever URL the poster was handed and the
store holds whatever the scraper resolved — two different sources that have
drifted before. An exact string compare misses on a trailing slash or a
?utm_source, and a miss is not cosmetic: the post was commented on for real,
but stays GENERATED, sits in the review queue forever, and can be offered up to
be commented on a second time.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import post_store  # noqa: E402

CANON = "https://www.linkedin.com/posts/example-person-activity-1010101010101-aa/"


def _store(tmp_path, url, status=post_store.GENERATED):
    store = post_store.PostStore(path=str(tmp_path / "posts_db.json"))
    store.posts["k1"] = {
        "key": "k1", "url": url, "author": "Example Person",
        "text": "A post about agents.", "status": status,
        "comment": "A drafted reply.",
    }
    return store


# ─── the transition fires ─────────────────────────────────────────────────────

def test_a_posted_comment_becomes_commented(tmp_path):
    store = _store(tmp_path, CANON)
    assert store.sync_with_progress([CANON]) == 1
    assert store.posts["k1"]["status"] == post_store.COMMENTED


def test_it_leaves_the_review_queue(tmp_path):
    store = _store(tmp_path, CANON)
    assert len(store.review_queue()) == 1
    store.sync_with_progress([CANON])
    assert store.review_queue() == []


def test_it_stamps_when_it_was_commented(tmp_path):
    store = _store(tmp_path, CANON)
    store.sync_with_progress([CANON])
    assert store.posts["k1"]["commented_at"]


# ─── the drift that would strand it ───────────────────────────────────────────

@pytest.mark.parametrize("ledger_url", [
    CANON,
    CANON.rstrip("/"),                      # no trailing slash
    CANON + "?utm_source=share",            # tracking params
    CANON + "?utm_source=share&rcm=ACoAAA",
    CANON.rstrip("/") + "?utm_medium=member_desktop",
    CANON + "#comments",                    # a fragment
    CANON.upper().replace("HTTPS", "https"),  # case drift in the path
])
def test_url_drift_does_not_strand_a_posted_comment(tmp_path, ledger_url):
    store = _store(tmp_path, CANON)
    assert store.sync_with_progress([ledger_url]) == 1, \
        "posted as %r but the store kept it in the queue" % ledger_url
    assert store.posts["k1"]["status"] == post_store.COMMENTED


def test_drift_on_the_store_side_also_matches(tmp_path):
    """Either side can be the one carrying the query string."""
    store = _store(tmp_path, CANON + "?utm_source=share")
    assert store.sync_with_progress([CANON]) == 1


# ─── it must not over-match ───────────────────────────────────────────────────

def test_a_different_post_is_not_marked_commented(tmp_path):
    store = _store(tmp_path, CANON)
    other = "https://www.linkedin.com/posts/someone-else-activity-1111111111-bb/"
    assert store.sync_with_progress([other]) == 0
    assert store.posts["k1"]["status"] == post_store.GENERATED


def test_a_prefix_of_the_url_is_not_a_match(tmp_path):
    store = _store(tmp_path, CANON)
    assert store.sync_with_progress(["https://www.linkedin.com/posts/"]) == 0


def test_an_empty_ledger_changes_nothing(tmp_path):
    store = _store(tmp_path, CANON)
    assert store.sync_with_progress([]) == 0
    assert store.sync_with_progress(None) == 0


def test_a_record_with_no_url_is_never_matched(tmp_path):
    store = _store(tmp_path, "")
    assert store.sync_with_progress(["", CANON]) == 0


def test_an_already_commented_post_is_not_counted_twice(tmp_path):
    store = _store(tmp_path, CANON, status=post_store.COMMENTED)
    assert store.sync_with_progress([CANON]) == 0


# ─── the normaliser itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    (CANON, CANON.rstrip("/")),
    (CANON, CANON + "?x=1"),
    (CANON, CANON + "#frag"),
    ("  " + CANON + "  ", CANON),
])
def test_these_all_normalise_alike(a, b):
    assert post_store.PostStore._match_url(a) == post_store.PostStore._match_url(b)


def test_different_posts_do_not_normalise_alike():
    other = "https://www.linkedin.com/posts/someone-else-activity-1111111111-bb/"
    assert (post_store.PostStore._match_url(CANON)
            != post_store.PostStore._match_url(other))


@pytest.mark.parametrize("value", ["", None, "   "])
def test_the_normaliser_survives_nothing(value):
    assert post_store.PostStore._match_url(value) == ""
