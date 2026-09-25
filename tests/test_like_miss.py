"""Dispatch 15.2: a Like miss stays soft, and becomes loud.

Liking is optional, so a missing Like button must never stop the comment. The
Like selector is also the one that went dead silently before (MAINTENANCE
§6.8). So a miss has to leave evidence: a WARN naming the post and the
selectors tried, a per-run count, and a capture of the action bar that the
next repair can read the new hook from.

Gated in BOTH directions. The miss path must write all three artifacts, bump
the counter and still post. The success path must write nothing and leave the
counter at zero. Without the second half, "always capture" would pass.
"""

import glob
import json
import logging
import os
import time

import pytest

from linkedin_automation import comment_poster as cpm
from linkedin_automation import profile_manager as pm

from fake_post_page import FakePostPage

URL = "https://www.linkedin.com/feed/update/urn:li:activity:1010101010101010/"
URL2 = "https://www.linkedin.com/feed/update/urn:li:activity:1100110011001100/"
COMMENT = {"url": URL, "comment": "A careful point about eval drift.",
           "preview": "preview"}


@pytest.fixture
def failures(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "failures").mkdir(parents=True)
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "t")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(data / "progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda n=None: {"behavior": {}})
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(
                            data / (subdir or "")))
    monkeypatch.setattr(pm, "login", lambda d, p: True)
    # The real human_behavior code runs; only the waiting is removed.
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return data / "failures"


def _poster(page):
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    poster.driver = page
    poster.LIKE_WAIT_SECONDS = 0.05
    return poster


def _like_miss_files(failures):
    return sorted(os.path.basename(p) for p in
                  glob.glob(str(failures / "failure_like_miss_*")))


# ─── the miss path ───────────────────────────────────────────────────────────

def test_a_like_miss_writes_all_three_artifacts_and_still_posts(failures,
                                                                 caplog):
    caplog.set_level(logging.WARNING)
    page = FakePostPage(like_absent_urls={URL})
    poster = _poster(page)

    assert poster.post_single_comment(dict(COMMENT)) is True

    # The comment went out, and the ledger says so.
    assert page.published == [(URL, COMMENT["comment"])]
    assert URL in poster.progress["posted_comments"]
    # Counted.
    assert poster.like_misses == 1
    # All three artifacts.
    files = _like_miss_files(failures)
    assert any(f.endswith(".png") for f in files), files
    assert any(f.endswith(".html") for f in files), files
    assert any(f.endswith("_likedom.json") for f in files), files
    # The WARN names the post and the selectors that were tried.
    warn = [r for r in caplog.records if "LIKE MISS" in r.getMessage()]
    assert warn and warn[0].levelno == logging.WARNING
    assert "1010101010101010" in warn[0].getMessage()
    assert cpm.LinkedInCommentPoster.LIKE_BUTTON_SELECTORS[0] in \
        warn[0].getMessage()


def test_the_likedom_lists_the_action_bar_and_is_scrubbed(failures):
    page = FakePostPage(like_absent_urls={URL})
    _poster(page).post_single_comment(dict(COMMENT))

    [path] = glob.glob(str(failures / "failure_like_miss_*_likedom.json"))
    with open(path, encoding="utf-8") as f:
        dom = json.load(f)

    assert dom["label"] == "like_miss"
    assert dom["action_bar_region"] == "action_bar"
    assert dom["reason"] == "like button not found"
    assert dom["selectors_tried"] == list(
        cpm.LinkedInCommentPoster.LIKE_BUTTON_SELECTORS)
    buttons = dom["action_bar_buttons"]
    assert [b["text"] for b in buttons] == ["Comment", "Repost", "Send"]
    for b in buttons:
        for key in ("text", "aria_label", "aria_pressed", "disabled", "size"):
            assert key in b, key
    assert buttons[2]["aria_pressed"] == "false"
    # PII scrubbed at the write boundary.
    raw = json.dumps(dom)
    assert "some-member-slug" not in raw
    assert "123456789012345" not in raw
    assert "/in/<redacted-slug>" in raw


def test_a_click_that_raises_is_a_miss_too(failures, monkeypatch):
    page = FakePostPage()
    poster = _poster(page)

    def boom(driver, element):
        raise RuntimeError("element click intercepted")
    monkeypatch.setattr(cpm.hb, "human_click", boom)
    assert poster.like_post() is False
    assert poster.like_misses == 1
    assert any(f.endswith("_likedom.json") for f in _like_miss_files(failures))


def test_the_run_summary_counts_the_misses(failures, monkeypatch, tmp_path):
    from linkedin_automation import comment_fields
    page = FakePostPage(like_absent_urls={URL, URL2})
    monkeypatch.setattr(pm, "create_driver",
                        lambda name=None, headless=False: (page, {}))
    txt = tmp_path / "c.txt"
    txt.write_text(comment_fields.comments_to_txt(
        [{"url": u, "comment": "A point %d." % i, "post_preview": "p",
          "author": "a"} for i, u in enumerate((URL, URL2))], "ts"),
        encoding="utf-8")
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    poster.LIKE_WAIT_SECONDS = 0.05
    poster.run(str(txt), post_count=2)
    with open(poster.run_log_path, encoding="utf-8") as f:
        assert "like_misses=2" in f.read()


# ─── the success path: nothing written, nothing counted ─────────────────────

def test_a_successful_like_writes_nothing_and_counts_nothing(failures):
    page = FakePostPage()
    poster = _poster(page)

    assert poster.post_single_comment(dict(COMMENT)) is True

    assert page.liked == [URL]
    assert page.published == [(URL, COMMENT["comment"])]
    assert poster.like_misses == 0
    assert _like_miss_files(failures) == []
    assert os.listdir(failures) == []


def test_an_already_liked_post_is_not_a_miss(failures):
    page = FakePostPage()
    poster = _poster(page)
    page.get(URL)
    page._like()                       # reacted on a previous visit
    # The fake renders no LIKED_STATE match, so recognise it the way the
    # production selector would: the Reaction-state label is no longer "Like".
    orig = page.find_elements

    def with_liked_state(by, selector):
        if selector == cpm.LinkedInCommentPoster.LIKED_STATE_SELECTORS[0]:
            return [page.like_button]
        return orig(by, selector)
    page.find_elements = with_liked_state

    assert poster.like_post() is True
    assert poster.like_misses == 0
    assert _like_miss_files(failures) == []
