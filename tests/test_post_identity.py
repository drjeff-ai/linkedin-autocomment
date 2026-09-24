"""Dispatch 15.3: one URN grammar, two policies, and they must agree.

Three regexes used to parse post URNs and disagreed about which types exist.
The poster knew only ``activity``, so a group post (``urn:li:groupPost:<g>-<p>``,
95 of them in the live store) reached it with no identity at all.

Now ``post_urn`` owns the grammar. The poster (``post_identity``) accepts all
four types with type-qualified ids. The scraper keeps exactly its old three,
through the same grammar. The tests below are about AGREEMENT between those
two policies, not an enumeration of today's cases: adding a type to one site
and not the other has to turn something red.
"""

import re

import pytest

from linkedin_automation import comment_poster as cpm
from linkedin_automation import post_urn
from linkedin_automation import profile_manager as pm
from linkedin_automation.post_finder import LinkedInScraper

from fake_post_page import FakePostPage, FEED_URL

Poster = cpm.LinkedInCommentPoster

#: Fabricated ids (0/1 only, per the repo's URN hygiene rule).
ID = "1010101010101010"
GROUP_ID = "1010101-1010101010101010"


def _id_for(type_):
    return GROUP_ID if type_ == "groupPost" else ID


def _inputs(type_):
    """Every URL shape a post of this type arrives in, keyed by form."""
    i = _id_for(type_)
    return {
        post_urn.URN_FORM: [
            "https://www.linkedin.com/feed/update/urn:li:%s:%s/" % (type_, i),
            "https://www.linkedin.com/feed/update/urn:li:%s:%s/?utm=x" % (type_, i),
            '<a href="/feed/update/urn:li:%s:%s/">' % (type_, i),
        ],
        post_urn.SLUG_FORM: [
            "https://www.linkedin.com/posts/some-slug_topic-%s-%s-AbCd/" % (type_, i),
        ],
    }


def _scraper_qualified(form, text):
    """What the scraper records for this text, as a qualified id (or None)."""
    fn = (LinkedInScraper.urn_in_text if form == post_urn.URN_FORM
          else LinkedInScraper.urn_from_copied_link)
    urn = fn(text)
    return urn[len("urn:li:"):] if urn else None


# ─── agreement ───────────────────────────────────────────────────────────────

def test_the_two_type_sets_differ_by_exactly_groupPost():
    """THE TRIPWIRE. Widening either site alone turns this red.

    The scraper must never accept a type the poster cannot key on, and the
    only type the poster accepts beyond the scraper is groupPost - on purpose,
    because the scraper was deliberately left unwidened (Dispatch 15.3).
    """
    poster, scraper = set(Poster.POST_IDENTITY_TYPES), set(LinkedInScraper.URN_TYPES)
    assert scraper <= poster, "scraper accepts %s the poster cannot key on" % (
        scraper - poster)
    assert poster - scraper == {"groupPost"}


#: Parametrised over the grammar's known types rather than the two policy
#: sets, so the sets are read inside each test: a missing or changed set
#: fails that test, not the whole module at collection. A type added to only
#: one set is caught by the tripwire above.
@pytest.mark.parametrize("type_", ["activity", "ugcPost", "share", "groupPost"])
def test_both_parsers_agree_on_every_type(type_):
    if type_ not in set(Poster.POST_IDENTITY_TYPES) | set(
            LinkedInScraper.URN_TYPES):
        pytest.fail("%s is accepted by neither site" % type_)
    both = type_ in Poster.POST_IDENTITY_TYPES and \
        type_ in LinkedInScraper.URN_TYPES
    for form, texts in _inputs(type_).items():
        for text in texts:
            mine = Poster.post_identity(text)
            theirs = _scraper_qualified(form, text)
            if both:
                assert mine is not None, text
                assert mine == theirs, (text, mine, theirs)
            elif type_ in Poster.POST_IDENTITY_TYPES:
                assert theirs is None, "scraper widened to %s" % type_
                assert mine == "%s:%s" % (type_, _id_for(type_))
            else:
                pytest.fail("scraper accepts %s, the poster does not" % type_)


# ─── per type, distinctness, rewrite ─────────────────────────────────────────

@pytest.mark.parametrize("type_", ["activity", "ugcPost", "share", "groupPost"])
def test_every_type_parses_to_a_qualified_id(type_):
    url = "https://www.linkedin.com/feed/update/urn:li:%s:%s/" % (
        type_, _id_for(type_))
    assert Poster.post_identity(url) == "%s:%s" % (type_, _id_for(type_))


def test_a_group_post_id_keeps_both_numbers():
    url = "https://www.linkedin.com/feed/update/urn:li:groupPost:%s/" % GROUP_ID
    assert Poster.post_identity(url) == "groupPost:" + GROUP_ID


def test_the_same_digits_under_two_types_are_two_posts():
    ids = {Poster.post_identity(
        "https://www.linkedin.com/feed/update/urn:li:%s:%s/" % (t, ID))
        for t in ("activity", "ugcPost", "share")}
    assert len(ids) == 3
    assert Poster.post_identity(
        "https://www.linkedin.com/feed/update/urn:li:groupPost:1010101-%s/" % ID
    ) not in ids


def test_a_slug_url_and_its_rewrite_are_the_same_post():
    slug = "https://www.linkedin.com/posts/some-slug_topic-activity-%s-AbCd" % ID
    rewritten = "https://www.linkedin.com/feed/update/urn:li:activity:%s/" % ID
    assert Poster.post_identity(slug) == Poster.post_identity(rewritten) \
        == "activity:" + ID


def test_a_short_number_in_the_slug_is_not_the_id():
    """ "share-5-tips" is words, not a share URN."""
    url = ("https://www.linkedin.com/posts/jane_share-5-tips-activity-%s-AbCd"
           % ID)
    assert Poster.post_identity(url) == "activity:" + ID


# ─── the scraper is unchanged ────────────────────────────────────────────────

#: The two regexes the scraper used before Dispatch 15.3, verbatim. The new
#: routing must reproduce them exactly on every input below.
OLD_URN_RE = re.compile(r'urn:li:(?:activity|ugcPost|share):\d+')
OLD_COPIED_LINK_RE = re.compile(r'(activity|ugcPost|share)-(\d+)')

CORPUS = [
    "https://www.linkedin.com/feed/update/urn:li:activity:%s/" % ID,
    "https://www.linkedin.com/feed/update/urn:li:ugcPost:%s/" % ID,
    "https://www.linkedin.com/feed/update/urn:li:share:%s" % ID,
    "https://www.linkedin.com/feed/update/urn:li:groupPost:%s/" % GROUP_ID,
    "https://www.linkedin.com/posts/x_y-activity-%s-AbCd/" % ID,
    "https://www.linkedin.com/posts/x-ugcPost-%s-xXxX/" % ID,
    "https://www.linkedin.com/posts/jane_share-5-tips-activity-%s-AbCd" % ID,
    "https://www.linkedin.com/posts/x-groupPost-%s-AbCd/" % GROUP_ID,
    '<div data-x="urn:li:comment:(urn:li:ugcPost:%s,1)">' % ID,
    "urn:li:activity:%s-1010" % ID,
    "urn:li:fsd_update:(urn:li:activity:%s,MAIN)" % ID,
    "activity:%s without a prefix" % ID,
    "https://lnkd.in/p/aaaabbbb",
    "https://www.linkedin.com/feed/",
    "",
]


@pytest.mark.parametrize("text", CORPUS)
def test_the_scraper_dom_parser_is_unchanged(text):
    m = OLD_URN_RE.search(text)
    assert LinkedInScraper.urn_in_text(text) == (m.group() if m else None)


@pytest.mark.parametrize("text", CORPUS)
def test_the_scraper_copied_link_parser_is_unchanged(text):
    m = OLD_COPIED_LINK_RE.search(text)
    old = "urn:li:%s:%s" % (m.group(1), m.group(2)) if m else None
    assert LinkedInScraper.urn_from_copied_link(text) == old


def test_the_scraper_still_rejects_groupPost():
    url = "https://www.linkedin.com/feed/update/urn:li:groupPost:%s/" % GROUP_ID
    assert LinkedInScraper.urn_in_text(url) is None
    assert LinkedInScraper.urn_from_copied_link(url) is None
    assert "groupPost" not in LinkedInScraper.URN_TYPES


def test_an_unknown_type_is_refused_not_guessed():
    with pytest.raises(ValueError):
        post_urn.find_post_urn("x", ("activity", "article"))


# ─── navigation: group posts decide, auth walls never do ─────────────────────

@pytest.fixture
def poster(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "t")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(tmp_path / "progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda n=None: {"behavior": {}})
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(
                            tmp_path / (subdir or "")))
    monkeypatch.setattr(cpm.time, "sleep", lambda s: None)
    monkeypatch.setattr(cpm.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cpm.hb, "simulate_reading", lambda *a, **k: None)
    monkeypatch.setattr(cpm.hb, "human_scroll", lambda *a, **k: None)
    p = Poster(profile_name="t")
    p.NAV_DECIDE_SECONDS = 0.2
    p.NAV_POLL_SECONDS = 0.01
    return p


GROUP_URL = "https://www.linkedin.com/feed/update/urn:li:groupPost:%s/" % GROUP_ID


def test_a_live_group_post_loads(poster):
    poster.driver = FakePostPage()
    assert poster.navigate_to_post(GROUP_URL) is True
    assert poster.last_navigation["outcome"] == poster.NAV_OK


def test_a_group_post_bounced_to_the_feed_is_gone_not_unclear(poster):
    """THE 15.3 SYMPTOM. With no identity, a deleted group post could only
    ever be UNCLEAR - retried every run, forever."""
    poster.driver = FakePostPage(gone_urls={GROUP_URL})
    assert poster.navigate_to_post(GROUP_URL) is False
    assert poster.last_navigation["outcome"] == poster.NAV_UNAVAILABLE


@pytest.mark.parametrize("type_", ["activity", "ugcPost", "share", "groupPost"])
@pytest.mark.parametrize("wall", list(Poster.NAV_AUTH_URL_MARKERS))
def test_an_auth_wall_is_never_a_gone_post(poster, type_, wall):
    url = "https://www.linkedin.com/feed/update/urn:li:%s:%s/" % (
        type_, _id_for(type_))

    class Wall(FakePostPage):
        def get(self, u):
            self.gets.append(u)
            self.current_url = "https://www.linkedin.com%s?session_redirect=x" % wall

        def find_elements(self, by, selector):
            return []                  # a login page renders no post

    poster.driver = Wall()
    poster.navigate_to_post(url)
    assert poster.last_navigation["outcome"] != poster.NAV_UNAVAILABLE
    assert poster.last_navigation["outcome"] == poster.NAV_UNCLEAR
    # And the redirect check itself, on the wall it landed on.
    assert poster.navigated_away_from(url) is None


def test_landing_on_another_urn_type_is_no_opinion(poster):
    """share -> activity under a different number may be LinkedIn normalising.
    Indistinguishable from a redirect, and UNAVAILABLE is terminal."""
    asked = "https://www.linkedin.com/feed/update/urn:li:share:%s/" % ID
    landed = "https://www.linkedin.com/feed/update/urn:li:activity:1011011011011011/"
    poster.driver = FakePostPage()
    poster.driver.current_url = landed
    assert poster.navigated_away_from(asked) is None


def test_the_same_type_under_another_number_is_a_redirect(poster):
    asked = "https://www.linkedin.com/feed/update/urn:li:activity:%s/" % ID
    poster.driver = FakePostPage()
    poster.driver.current_url = \
        "https://www.linkedin.com/feed/update/urn:li:activity:1011011011011011/"
    assert poster.navigated_away_from(asked).startswith("redirected to")


def test_a_slug_url_landing_on_its_rewrite_is_not_a_redirect(poster):
    """Qualified ids must be compared parsed. As a substring,
    "activity:<id>" never occurs in the slug URL, and every such post
    would be called gone."""
    asked = "https://www.linkedin.com/posts/some-slug-activity-%s-AbCd" % ID
    poster.driver = FakePostPage()
    poster.driver.current_url = FEED_URL.replace(
        "/feed/", "/feed/update/urn:li:activity:%s/" % ID)
    assert poster.navigated_away_from(asked) is None


# ─── the 15.1 log lines carry the qualified id ───────────────────────────────

def test_the_run_log_uses_the_qualified_id(poster, monkeypatch, tmp_path):
    from linkedin_automation import comment_fields
    page = FakePostPage()
    monkeypatch.setattr(pm, "create_driver",
                        lambda name=None, headless=False: (page, {}))
    monkeypatch.setattr(pm, "login", lambda d, p: True)
    txt = tmp_path / "c.txt"
    txt.write_text(comment_fields.comments_to_txt(
        [{"url": GROUP_URL, "comment": "A point.", "post_preview": "p",
          "author": "a"}], "ts"), encoding="utf-8")
    poster.run(str(txt), post_count=1)
    with open(poster.run_log_path, encoding="utf-8") as f:
        log = f.read()
    assert "COMMENT post=groupPost:%s outcome=posted" % GROUP_ID in log
    assert "STEP post=groupPost:%s step=navigate " % GROUP_ID in log
    assert "POLL post=groupPost:%s poll=classify_navigation" % GROUP_ID in log


# ─── 15.3 review findings ────────────────────────────────────────────────────

@pytest.mark.parametrize("type_", ["activity", "ugcPost", "share", "groupPost"])
@pytest.mark.parametrize("shape", [
    "https://www.linkedin.com/feed/update/urn%%3Ali%%3A%s%%3A%s/",
    "https://www.linkedin.com/feed/?highlightedUpdateUrn=urn%%3Ali%%3A%s%%3A%s",
])
def test_the_same_post_at_a_percent_encoded_url_is_not_a_redirect(
        poster, type_, shape):
    """BLOCKING in review. The old digit-substring check matched encoded
    URLs; parsing the raw URL finds no id there and would call a live post
    gone - terminally."""
    asked = "https://www.linkedin.com/feed/update/urn:li:%s:%s/" % (
        type_, _id_for(type_))
    poster.driver = FakePostPage()
    poster.driver.current_url = shape % (type_, _id_for(type_))
    assert poster.navigated_away_from(asked) is None


@pytest.mark.parametrize("type_", ["ugcPost", "share", "groupPost"])
def test_a_newly_identified_post_off_the_feed_with_no_id_is_no_opinion(
        poster, type_):
    """A group post may be served at /groups/<g>/posts/... with no URN.
    Only the observed gone signal - a bounce to the feed - counts."""
    asked = "https://www.linkedin.com/feed/update/urn:li:%s:%s/" % (
        type_, _id_for(type_))
    poster.driver = FakePostPage()
    poster.driver.current_url = "https://www.linkedin.com/groups/1010101/posts/"
    assert poster.navigated_away_from(asked) is None
    poster.driver.current_url = FEED_URL
    assert poster.navigated_away_from(asked).startswith("redirected to")


def test_activity_keeps_its_pre_15_3_redirect_rule(poster):
    """Unchanged: an activity post that lands anywhere without its id is a
    redirect, feed or not."""
    asked = "https://www.linkedin.com/feed/update/urn:li:activity:%s/" % ID
    poster.driver = FakePostPage()
    poster.driver.current_url = "https://www.linkedin.com/groups/1010101/posts/"
    assert poster.navigated_away_from(asked).startswith("redirected to")


def test_a_short_group_number_still_identifies_the_post():
    """min_digits guards the POST number. Older groups have short ids."""
    url = "https://www.linkedin.com/feed/update/urn:li:groupPost:10101-%s/" % ID
    assert Poster.post_identity(url) == "groupPost:10101-" + ID


def test_the_scraper_matches_the_old_regexes_on_generated_strings():
    """The corpus above is hand-picked; this is not. Every string of up to 4
    tokens from an alphabet built to provoke overlap, prefix and separator
    confusion, compared against the two regexes the scraper used to run."""
    import itertools
    tokens = ["urn:li:", "urn:li", "activity", "share", "ugcPost",
              "groupPost", ":", "-", "1", "10", "x", "%3A"]
    checked = 0
    for n in range(1, 5):
        for combo in itertools.product(tokens, repeat=n):
            text = "".join(combo)
            m = OLD_URN_RE.search(text)
            assert LinkedInScraper.urn_in_text(text) == (
                m.group() if m else None), text
            m = OLD_COPIED_LINK_RE.search(text)
            assert LinkedInScraper.urn_from_copied_link(text) == (
                "urn:li:%s:%s" % (m.group(1), m.group(2)) if m else None), text
            checked += 1
    assert checked > 20000


# ─── 15.3 re-review hardening (safe side only) ───────────────────────────────

def test_our_id_anywhere_in_the_landed_url_is_not_a_redirect(poster):
    """A slug's opening words, or a query parameter, can carry another URN
    ahead of ours. Finding ours anywhere is the safe reading."""
    asked = "https://www.linkedin.com/feed/update/urn:li:activity:%s/" % ID
    poster.driver = FakePostPage()
    for landed in (
            "https://www.linkedin.com/posts/jane_q3-activity-101010-activity-%s-AbCd" % ID,
            "https://www.linkedin.com/feed/update/urn:li:activity:1011011011011011/"
            "?x=urn:li:activity:%s" % ID):
        poster.driver.current_url = landed
        assert poster.navigated_away_from(asked) is None, landed


def test_a_double_encoded_landing_is_decoded(poster):
    asked = "https://www.linkedin.com/feed/update/urn:li:activity:%s/" % ID
    poster.driver = FakePostPage()
    poster.driver.current_url = (
        "https://www.linkedin.com/feed/update/urn%%253Ali%%253Aactivity%%253A%s/" % ID)
    assert poster.navigated_away_from(asked) is None
