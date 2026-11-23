"""E3 (D-027): the Instagram adapter, offline, against synthetic fixtures.

The fixtures (``tests/fixtures/instagram/``) are UNVERIFIED: written from the
nine-month-stale instagram_bot reference, never from a live capture. They
prove the adapter matches the shape it was written for, not what Instagram
serves today; the live pass (E4) does that.

Every verdict is shown both ways (PRINCIPLES §3): classify ok, gone and
unclear; the thread check True and False; the comment verifier and the like
verifier True and False; the live gate shut and open. Nothing here opens a
socket or a browser.
"""

import ast
import glob
import inspect
import json
import os
import time
from datetime import timedelta

import pytest

from linkedin_automation import dom_probe
from linkedin_automation import engagement_adapter as ea
from linkedin_automation import instagram_adapter as ia
from linkedin_automation import instagram_engage as ie
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import selector_health as sh

from fake_ig_driver import FakeIGDriver, Hands, LiveLookingDriver, fixture, post, profile

A = ia.InstagramAdapter
CODE = "SYNTHPOST01"
URL = post(CODE)
TEXT = "is the second page from the new series?"
CFG = ie.InstagramConfig(profile="t", identity_slug="dev_tester", max_followers=20000,
                         skip_verified=True)
OVERLAY = {"identity_slug": "dev_tester", "engagement": {"max_followers": 20000}}


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"

    def get_data_dir(profile_name=None, subdir=None):
        path = root / (profile_name or "t") / (subdir or "")
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    monkeypatch.setattr(pm, "get_data_dir", get_data_dir)
    monkeypatch.setattr(pm, "get_profile_config", lambda n=None: {
        "scheduled_posting": {"platforms": {"instagram": OVERLAY}}})
    monkeypatch.setattr(pm, "create_driver", _no_browser)
    monkeypatch.delenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, raising=False)
    monkeypatch.delenv(pm.ALLOW_PRODUCTION_ENV, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    for name, value in (("NAV_SECONDS", 0.03), ("INPUT_SECONDS", 0.03),
                        ("LIKE_VERIFY_SECONDS", 0.03), ("COMMENT_VERIFY_SECONDS", 0.03),
                        ("POLL_SECONDS", 0.005), ("FIND_MAX_SCROLLS", 2)):
        monkeypatch.setattr(A, name, value)
    return root


def _no_browser(*a, **k):
    raise AssertionError("a test tried to create a real browser")


def make(pages=None, driver_cls=FakeIGDriver, cfg=CFG, **kw):
    driver = driver_cls(pages if pages is not None else {URL: "ig_post"}, **kw)
    return A("t", driver, cfg=cfg, hands=Hands()), driver


def approved_record(text=TEXT, key=CODE, author="synthetic_painter"):
    store = ie.load_store("t")
    store.posts[key] = {
        "key": key, "url": post(key), "author": author,
        "text": "the second page of the new series, all gouache",
        "status": post_store.GENERATED, "comment": text,
        "instagram": {"shortcode": key, "author_handle": author}}
    store.approve(key)
    store.save()
    return ie.load_store("t").get(key)


def failures(root, label):
    return glob.glob(str(root / "t" / "failures" / ("failure_ig_%s_*" % label)))


# ─── the interface and the registry ──────────────────────────────────────────

def test_instagram_implements_the_interface_as_a_browser_adapter():
    assert issubclass(A, ea.EngagementAdapter)
    assert A.kind == ea.BROWSER and A.platform == "instagram"
    assert A.SELECTORS_EXEMPT_REASON is None


def test_its_selectors_are_registered_and_pass_the_contract():
    adapter = A("t", FakeIGDriver({}), cfg=CFG)
    assert ea.selector_contract_problems(adapter, sh.SELECTOR_REGISTRY) == []
    assert set(adapter.selectors()) <= set(sh.SELECTOR_REGISTRY)


def test_every_instagram_entry_is_marked_unverified():
    entries = {k: v for k, v in sh.SELECTOR_REGISTRY.items() if k.startswith("ig_")}
    assert entries == ia.registry_entries() and len(entries) >= 15
    for key, entry in entries.items():
        assert entry["verified"] is False, key
        assert entry["note"].startswith("UNVERIFIED"), key
        assert entry["page"] in ia.IG_PAGES, key
    assert not any(k.startswith("ig_") for k, v in sh.SELECTOR_REGISTRY.items()
                   if v.get("page", "feed") not in ia.IG_PAGES)


def test_every_selector_constant_is_registered_and_commented_unverified():
    registered = {s for v in ia.registry_entries().values() for s in v["selectors"]}
    source = inspect.getsource(A).splitlines()
    for name, value in vars(A).items():
        if not name.endswith("_SELECTORS"):
            continue
        for sel in value:
            assert sel in registered, (name, sel)
        line = next(i for i, ln in enumerate(source) if ln.strip().startswith(name + " ="))
        above = "\n".join(source[max(0, line - 4):line])
        assert "UNVERIFIED" in above, name


@pytest.mark.parametrize("name,page,status", [
    ("ig_feed", "ig_feed", "HEALTHY"),
    ("ig_post", "ig_post", "HEALTHY"),
    ("ig_post_liked", "ig_post", "HEALTHY"),
    ("ig_profile", "ig_profile", "HEALTHY"),
    ("ig_profile_mega_verified", "ig_profile", "HEALTHY"),
    ("ig_post_broken", "ig_post", "BROKEN"),
    ("ig_feed_logged_out", "ig_feed", "BROKEN"),
])
def test_the_offline_fixture_gate(name, page, status):
    check = sh.check_fixture(fixture(name), page=page)
    assert sh.overall_status(check) == status
    expected = {k for k, v in sh.SELECTOR_REGISTRY.items() if v.get("page") == page}
    assert set(check) == expected


def test_the_broken_fixture_fails_on_the_renamed_box():
    check = sh.check_fixture(fixture("ig_post_broken"), page="ig_post")
    assert [k for k, c in check.items() if not c["ok"]] == ["ig_comment_input"]


def test_the_gone_marker_matches_only_the_gone_page():
    count = dom_probe.make_counter(fixture("ig_post_gone"))
    assert any(count(s) for s in A.GONE_SELECTORS)
    count = dom_probe.make_counter(fixture("ig_post"))
    assert not any(count(s) for s in A.GONE_SELECTORS)


def test_the_fixtures_are_synthetic():
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "fixtures",
                                       "instagram", "*.html")):
        with open(path, encoding="utf-8") as f:
            html = f.read()
        assert html.startswith("<!-- UNVERIFIED synthetic Instagram fixture"), path
        assert "cdninstagram" not in html and "fbcdn" not in html, path


# ─── the live gate ───────────────────────────────────────────────────────────

LIVE_PAGES = {ia.FEED_URL: "ig_feed", URL: "ig_post", profile("dev_tester"): "ig_profile_self"}


@pytest.mark.parametrize("call", ["classify", "already_commented", "like",
                                  "post_and_verify", "find"])
def test_a_live_driver_is_refused_without_a_recorded_pass(data, call):
    adapter, driver = make(LIVE_PAGES, driver_cls=LiveLookingDriver)
    rec = approved_record()
    with pytest.raises(ia.LiveGateRefused, match="no live selector-health pass"):
        getattr(adapter, call)(*(() if call == "find" else (rec,)))
    assert driver.visits == [] and driver.clicks == [] and driver.typed == []


def test_the_adapter_never_creates_a_browser_without_a_pass(data, monkeypatch):
    adapter = A("t", None, cfg=CFG, hands=Hands())
    with pytest.raises(ia.LiveGateRefused):
        adapter.classify({"key": CODE})


def test_engage_item_is_refused_live_without_a_pass_and_keeps_the_approval(data):
    adapter, driver = make(LIVE_PAGES, driver_cls=LiveLookingDriver)
    approved_record()
    with pytest.raises(ia.LiveGateRefused):
        ia.engage_item("t", CODE, adapter)
    assert driver.visits == []
    assert post_store.is_approved(ie.load_store("t").get(CODE))


def test_a_healthy_live_pass_opens_the_gate(data):
    driver = LiveLookingDriver(LIVE_PAGES)
    code, result = ia.run_live_health_check("t", URL, driver=driver, out=lambda s: None)
    assert code == 0 and result["status"] == "HEALTHY", result["failed"]
    assert driver.clicks == [] and driver.typed == []      # read-only
    with open(ia.live_pass_path("t"), encoding="utf-8") as f:
        assert json.load(f)["identity_slug"] == "dev_tester"
    assert ia.live_pass_problem("t", CFG) is None
    adapter = A("t", driver, cfg=CFG, hands=Hands())
    assert adapter.classify({"key": CODE}).outcome == ea.OK


def _pass(data):
    code, _r = ia.run_live_health_check("t", URL, driver=LiveLookingDriver(LIVE_PAGES),
                                        out=lambda s: None)
    assert code == 0


def test_a_pass_for_other_selectors_does_not_open_the_gate(data, monkeypatch):
    _pass(data)
    monkeypatch.setattr(A, "LIKE_BUTTON_SELECTORS", ("section svg[aria-label='Like it']",))
    assert "selectors changed" in ia.live_pass_problem("t", CFG)


def test_a_stale_pass_does_not_open_the_gate(data):
    _pass(data)
    later = ia._now() + ia.LIVE_PASS_MAX_AGE + timedelta(minutes=1)
    assert "older than" in ia.live_pass_problem("t", CFG, now=later)


def test_a_pass_for_another_account_does_not_open_the_gate(data):
    _pass(data)
    other = ie.InstagramConfig("t", "other_dev", 20000, True)
    assert "not the configured @other_dev" in ia.live_pass_problem("t", other)


def test_a_broken_live_pass_is_recorded_and_keeps_the_gate_shut(data):
    pages = dict(LIVE_PAGES, **{URL: "ig_post_broken"})
    code, result = ia.run_live_health_check("t", URL, driver=LiveLookingDriver(pages),
                                            out=lambda s: None)
    assert code == 1 and result["failed"] == ["ig_comment_input"]
    assert "not HEALTHY" in ia.live_pass_problem("t", CFG)


def test_a_signed_out_live_pass_says_so_and_types_nothing(data):
    pages = dict(LIVE_PAGES, **{ia.FEED_URL: "ig_feed_logged_out"})
    driver = LiveLookingDriver(pages)
    code, result = ia.run_live_health_check("t", URL, driver=driver, out=lambda s: None)
    assert code == 1 and result["status"] == "BROKEN"
    assert any("sign in to Instagram by hand" in n for n in result["notes"])
    assert driver.typed == [] and driver.clicks == []


def test_a_pass_with_the_wrong_account_signed_in_is_broken(data):
    pages = dict(LIVE_PAGES, **{ia.FEED_URL: fixture("ig_feed").replace(
        "/dev_tester/", "/someone_else/")})
    code, result = ia.run_live_health_check("t", URL, driver=LiveLookingDriver(pages),
                                            out=lambda s: None)
    assert code == 1 and result["identity_proven"] is False


def test_a_production_identity_is_refused_live(data, monkeypatch):
    _pass(data)
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "dev_tester")
    adapter, _d = make(LIVE_PAGES, driver_cls=LiveLookingDriver)
    with pytest.raises(ia.LiveGateRefused, match="PRODUCTION"):
        adapter.classify({"key": CODE})
    with pytest.raises(pm.ProductionAccessRefused):
        ia.run_live_health_check("t", URL, driver=LiveLookingDriver(LIVE_PAGES),
                                 out=lambda s: None)


def test_no_identity_refuses_even_offline(data):
    adapter, _d = make(cfg=ie.InstagramConfig("t", "", 20000, True))
    with pytest.raises(ia.LiveGateRefused, match="identity_slug"):
        adapter.classify({"key": CODE})


# ─── classify ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("page,outcome,detail", [
    ("ig_post", ea.OK, None),
    ("ig_post_liked", ea.OK, None),
    ("ig_post_gone", ea.GONE, "removed"),
    (None, ea.UNCLEAR, "timeout"),
    ("ig_feed_logged_out", ea.UNCLEAR, "auth_wall"),
])
def test_classify(data, page, outcome, detail):
    adapter, _d = make({URL: page} if page else {})
    cls = adapter.classify({"key": CODE})
    assert (cls.outcome, cls.detail) == (outcome, detail)


def test_classify_never_says_ok_when_the_signed_in_account_is_not_ours(data):
    adapter, _d = make(cfg=ie.InstagramConfig("t", "someone_else", 20000, True))
    cls = adapter.classify({"key": CODE})
    assert (cls.outcome, cls.detail) == (ea.UNCLEAR, "identity_unproven")


def test_a_page_for_another_shortcode_is_not_ok(data):
    adapter, _d = make({post("SYNTHOTHER9"): "ig_post"})
    cls = adapter.classify({"key": "SYNTHOTHER9"})
    assert cls.outcome == ea.UNCLEAR


def test_unclear_leaves_a_capture(data):
    adapter, _d = make({})
    adapter.classify({"key": CODE})
    assert failures(data, "classify_timeout")


# ─── the thread and the comment verifier ─────────────────────────────────────

@pytest.mark.parametrize("page,already", [("ig_post_commented", True), ("ig_post", False)])
def test_already_commented_asks_the_thread(data, page, already):
    adapter, _d = make({URL: page})
    assert adapter.classify({"key": CODE}).ok
    assert adapter.already_commented({"key": CODE}).already is already


def test_already_commented_off_the_post_raises_never_says_no(data):
    adapter, driver = make({URL: "ig_post", ia.FEED_URL: "ig_feed"})
    driver.get(ia.FEED_URL)
    with pytest.raises(ie.InstagramRefused):
        adapter.already_commented({"key": CODE})


def test_the_comment_verifier_says_true_only_for_our_exact_comment(data):
    adapter, driver = make({URL: "ig_post_commented"})
    driver.get(URL)
    assert adapter.comment_seen("is this from the new series?") is True
    assert adapter.comment_seen("is this from the old series?") is False
    # someone else's comment with our text is not ours
    assert adapter.comment_seen("the colours on this one are so good") is False
    adapter2, d2 = make({URL: "ig_post"})
    d2.get(URL)
    assert adapter2.comment_seen("is this from the new series?") is False


# ─── like ────────────────────────────────────────────────────────────────────

def test_the_like_verifier_both_ways(data):
    adapter, driver = make({URL: "ig_post_liked", post("SYNTHPOST09"): "ig_post"})
    driver.get(URL)
    assert adapter.liked_seen() is True
    driver.get(post("SYNTHPOST09"))
    assert adapter.liked_seen() is False


def test_like_placed_is_proven_by_the_state_after_the_click(data):
    adapter, driver = make()
    adapter.classify({"key": CODE})
    res = adapter.like({"key": CODE})
    assert (res.state, res.proven) == (ea.LIKE_PLACED, True)
    assert len(driver.clicks) == 1


def test_like_already_liked_clicks_nothing(data):
    adapter, driver = make({URL: "ig_post_liked"})
    adapter.classify({"key": CODE})
    res = adapter.like({"key": CODE})
    assert (res.state, res.proven) == (ea.LIKE_ALREADY, True) and driver.clicks == []


def test_a_click_that_does_not_like_is_a_miss_never_liked(data):
    adapter, _d = make(like_works=False)
    adapter.classify({"key": CODE})
    res = adapter.like({"key": CODE})
    assert (res.state, res.proven, res.liked) == (ea.LIKE_MISS, False, False)
    assert res.reason.startswith("like_unverified")
    assert failures(data, "like_unverified")


def test_no_like_control_is_a_named_miss(data):
    html = fixture("ig_post").replace('<svg aria-label="Like"></svg></div></span>', "</div></span>")
    adapter, _d = make({URL: html})
    adapter.classify({"key": CODE})
    res = adapter.like({"key": CODE})
    assert (res.state, res.reason) == (ea.LIKE_MISS, "like_control_absent")


def test_the_comments_like_hearts_are_not_the_posts_like(data):
    """Decoys: the thread's own Like hearts sit outside the action bar."""
    adapter, driver = make()
    driver.get(URL)
    likes, liked = adapter.like_state()
    assert len(likes) == 1 and liked == []
    assert len(driver.find_elements("css selector", "svg[aria-label='Like']")) == 3


# ─── post and verify ─────────────────────────────────────────────────────────

def test_post_and_verify_posts_only_on_seeing_our_comment(data):
    rec = approved_record()
    adapter, driver = make()
    adapter.classify(rec)
    res = adapter.post_and_verify(rec)
    assert res.posted is True and res.outcome == ea.POSTED
    assert res.proof == {"author": "dev_tester", "text": TEXT, "after_reload": True}
    assert driver.typed == [TEXT]
    assert driver.visits.count(URL) == 2      # classify, then the proof reload


def test_a_comment_only_echoed_by_the_page_is_not_proof(data):
    """Seen right after submit, gone after a reload: failed, ambiguous."""
    rec = approved_record()
    adapter, _d = make(server_keeps=False)
    adapter.classify(rec)
    res = adapter.post_and_verify(rec)
    assert (res.posted, res.ambiguous) == (False, True)
    assert res.reason.startswith("comment_gone_after_reload")
    assert failures(data, "comment_gone_after_reload")


def test_a_submit_that_never_lands_is_failed_and_ambiguous(data):
    rec = approved_record()
    adapter, _d = make(submit_works=False)
    adapter.classify(rec)
    res = adapter.post_and_verify(rec)
    assert (res.posted, res.ambiguous) == (False, True)
    assert res.reason.startswith("comment_not_seen")
    assert failures(data, "comment_not_seen")


def test_a_different_text_in_the_thread_is_not_proof(data):
    rec = approved_record()
    adapter, _d = make(submit_text="something else entirely")
    adapter.classify(rec)
    assert adapter.post_and_verify(rec).posted is False


def test_an_unapproved_draft_types_nothing(data):
    rec = dict(approved_record(), reviewed_at=None)
    adapter, driver = make()
    adapter.classify(rec)
    res = adapter.post_and_verify(rec)
    assert res.posted is False and res.reason == "not_approved: not_reviewed"
    assert driver.typed == [] and driver.clicks == []


def test_an_edited_draft_types_nothing(data):
    rec = dict(approved_record(), comment=TEXT + " edited")
    adapter, driver = make()
    adapter.classify(rec)
    assert adapter.post_and_verify(rec).reason == "not_approved: edited_after_approval"
    assert driver.typed == []


def test_an_emoji_chromedriver_cannot_type_is_refused_before_typing(data):
    rec = approved_record(text="so good \U0001F60D")
    adapter, driver = make()
    adapter.classify(rec)
    res = adapter.post_and_verify(rec)
    assert res.posted is False and res.reason.startswith("untypeable")
    assert driver.typed == []


def test_a_missing_comment_box_is_named(data):
    rec = approved_record()
    adapter, _d = make({URL: "ig_post_broken"})
    adapter.classify(rec)
    res = adapter.post_and_verify(rec)
    assert (res.posted, res.reason) == (False, "comment_input_absent")


# ─── the shared order and the store ──────────────────────────────────────────

def test_engage_item_posts_likes_and_records(data):
    approved_record()
    adapter, _d = make()
    result, line = ia.engage_item("t", CODE, adapter)
    assert result.outcome == ea.ENGAGE_POSTED and line == "posted 1: %s" % CODE
    rec = ie.load_store("t").get(CODE)
    assert rec["status"] == post_store.COMMENTED
    assert rec["instagram"]["like"] == {"state": "placed", "proven": True,
                                        "reason": "the liked state was read after the click"}


def test_engage_item_refuses_an_unapproved_draft_and_opens_nothing(data):
    approved_record()
    store = ie.load_store("t")
    store.save_edit(CODE, TEXT + " changed")
    store.save()
    adapter, driver = make()
    result, line = ia.engage_item("t", CODE, adapter)
    assert result.outcome == ea.ENGAGE_NOT_APPROVED and "not approved" in line
    assert driver.visits == []


def test_engage_item_sends_a_rule_breaking_draft_back_to_review(data):
    approved_record(text="you look amazing #gouache")
    adapter, driver = make()
    result, line = ia.engage_item("t", CODE, adapter)
    assert result.outcome == ea.ENGAGE_NOT_APPROVED
    assert "hashtag" in result.reason and "appearance" in result.reason
    assert line.endswith("back to review") and driver.visits == []
    rec = ie.load_store("t").get(CODE)
    assert not post_store.is_approved(rec) and rec["comment"] == "you look amazing #gouache"


def test_approval_withdrawn_before_posting_is_honoured(data):
    rec = approved_record()
    adapter, driver = make()
    result = ea.engage_one(adapter, rec, reload=lambda k: dict(rec, reviewed_at=None))
    assert result.outcome == ea.ENGAGE_NOT_APPROVED and driver.typed == []


def test_engage_item_marks_a_gone_post_unavailable(data):
    approved_record()
    adapter, _d = make({URL: "ig_post_gone"})
    result, _line = ia.engage_item("t", CODE, adapter)
    assert result.outcome == ea.ENGAGE_GONE
    assert ie.load_store("t").get(CODE)["status"] == post_store.UNAVAILABLE


def test_engage_item_already_commented_marks_commented_without_typing(data):
    approved_record()
    adapter, driver = make({URL: "ig_post_commented"})
    result, _line = ia.engage_item("t", CODE, adapter)
    assert result.outcome == ea.ENGAGE_ALREADY and driver.typed == []
    assert ie.load_store("t").get(CODE)["status"] == post_store.COMMENTED


def test_engage_item_never_retries_an_unseen_comment(data):
    approved_record()
    adapter, _d = make(submit_works=False)
    result, _line = ia.engage_item("t", CODE, adapter)
    assert result.outcome == ea.ENGAGE_FAILED
    rec = ie.load_store("t").get(CODE)
    assert (rec["status"], rec["trash_reason"]) == (post_store.TRASH, ia.TRASH_UNVERIFIED)


def test_engage_item_unclear_stays_approved_for_later(data):
    approved_record()
    adapter, _d = make({})
    result, _line = ia.engage_item("t", CODE, adapter)
    assert result.outcome == ea.ENGAGE_UNCLEAR
    assert post_store.is_approved(ie.load_store("t").get(CODE))


# ─── find ────────────────────────────────────────────────────────────────────

def _feed_pages():
    p1 = fixture("ig_post")
    p2 = p1.replace(CODE, "SYNTHPOST02").replace("synthetic_painter", "mega_brand_synth")
    p3 = p1.replace(CODE, "SYNTHPOST03").replace("synthetic_painter", "tiny_letterer")
    return {ia.FEED_URL: "ig_feed", post(CODE): p1, post("SYNTHPOST02"): p2,
            post("SYNTHPOST03"): p3, profile("synthetic_painter"): "ig_profile",
            profile("mega_brand_synth"): "ig_profile_mega_verified"}


def test_find_lands_small_accounts_and_skips_mega_and_unknown(data):
    adapter, driver = make(_feed_pages())
    summary = adapter.find(max_posts=5)
    assert summary["added"] == [CODE]
    assert summary["filtered"] == {"mega_account: 2400000 followers > 20000": 1,
                                   "followers_unknown": 1}
    store = ie.load_store("t")
    rec = store.get(CODE)
    assert rec["status"] == post_store.NEW
    assert rec["text"] == "the second page of the new series, all gouache"
    assert rec["instagram"]["author_handle"] == "synthetic_painter"
    assert rec["instagram"]["followers"] == 8421
    skipped = store.get("SYNTHPOST02")
    assert (skipped["status"], skipped["trash_reason"]) == (post_store.TRASH,
                                                            ia.TRASH_ACCOUNT_SKIPPED)
    assert driver.clicks == [] and driver.typed == []


def test_find_with_no_threshold_configured_skips_everyone(data):
    adapter, _d = make(_feed_pages(), cfg=ie.InstagramConfig("t", "dev_tester", None, True))
    summary = adapter.find(max_posts=5)
    assert summary["added"] == []
    assert summary["filtered"] == {ie.SKIP_NO_THRESHOLD: 3}


def test_find_signed_out_stops_and_types_nothing(data):
    adapter, driver = make({ia.FEED_URL: "ig_feed_logged_out"})
    summary = adapter.find()
    assert summary["stopped"] == "auth_wall" and summary["added"] == []
    assert driver.typed == [] and driver.clicks == []
    assert failures(data, "find_auth_wall")


def test_find_skips_known_posts(data):
    adapter, _d = make(_feed_pages())
    adapter.find(max_posts=5)
    again = adapter.find(max_posts=5)
    assert again["added"] == [] and again["known"] == 3


@pytest.mark.parametrize("desc,meta", [
    ('1,204 likes, 37 comments - synthetic_painter on March 3, 2026: "page two".',
     {"author_handle": "synthetic_painter", "caption": "page two"}),
    ("12K likes, 3 comments - tiny_letterer on May 1, 2026: “new lettering”",
     {"author_handle": "tiny_letterer", "caption": "new lettering"}),
    ("something else", {"author_handle": "", "caption": ""}),
])
def test_parse_post_description(desc, meta):
    assert ia.parse_post_description(desc) == meta


@pytest.mark.parametrize("desc,n", [
    ("8,421 Followers, 312 Following, 96 Posts - See", 8421),
    ("2.4M Followers, 15 Following", 2400000), ("no count here", None)])
def test_parse_followers(desc, n):
    assert ia.parse_followers(desc) == n


@pytest.mark.parametrize("url,code", [
    ("https://www.instagram.com/p/SYNTHPOST01/", "SYNTHPOST01"),
    ("/p/Ab_c-12345/?img_index=1", "Ab_c-12345"), ("/reel/SYNTH/", None), ("", None)])
def test_shortcode_of(url, code):
    assert ia.shortcode_of(url) == code


# ─── no credential path ──────────────────────────────────────────────────────

def test_the_adapter_has_no_credential_path():
    tree = ast.parse(inspect.getsource(ia))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}
    words = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            words.append(node.value.lower())
        elif isinstance(node, ast.Name):
            words.append(node.id.lower())
        elif isinstance(node, ast.Attribute):
            words.append(node.attr.lower())
    assert not [w for w in words if "password" in w or "send_keys" in w
                or "credential" in w]
