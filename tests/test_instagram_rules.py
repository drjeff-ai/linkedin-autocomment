"""E2 (D-027): the Instagram rules that are data checks, each shown both ways.

Every rule refuses a draft that breaks it and passes one that does not
(PRINCIPLES §3); every refusal names its reason. All text is synthetic.
"""

import pytest

from linkedin_automation import instagram_engage as ie
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm

CFG = ie.InstagramConfig(profile="t", identity_slug="dev_tester", max_followers=20000,
                         skip_verified=True)
CAPTION = "the second page of the new series, all gouache"
GOOD = "is this from the new series?"


def refusals(text, caption=CAPTION, cfg=CFG, previous=()):
    return ie.draft_refusals(text, caption=caption, cfg=cfg, previous=previous)


def test_a_good_draft_passes_every_rule():
    assert refusals(GOOD) == []
    assert refusals("the colour shift on the last slide is so good \U0001F60D") == []


# ── length ──────────────────────────────────────────────────────────────────

def test_length_cap_is_the_registers_hard_max():
    at_cap = "the second page " + "a" * (300 - len("the second page "))
    assert len(at_cap) == 300
    assert not any(r.startswith("too_long") for r in refusals(at_cap))
    assert any(r.startswith("too_long: 301 chars > 300") for r in refusals(at_cap + "a"))


def test_an_empty_draft_is_refused():
    assert "empty" in refusals("   ")


# ── emoji and hashtags ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,n", [
    ("plain words", 0), ("\U0001F600", 1), ("\U0001F600\U0001F600", 2),
    ("\U0001F468‍\U0001F469‍\U0001F467", 1), ("\U0001F1FA\U0001F1F8", 1),
    ("\U0001F44D\U0001F3FD", 1), ("❤️", 1), ("1️⃣", 1),
    ("© ™ →", 0),
])
def test_emoji_count(text, n):
    assert ie.emoji_count(text) == n


def test_at_most_one_emoji():
    assert not any(r.startswith("emoji") for r in refusals(GOOD + " \U0001F525"))
    assert "emoji: 2 (at most one)" in refusals(GOOD + " \U0001F525\U0001F525")


def test_no_hashtags():
    assert "hashtag" in refusals(GOOD + " #gouache")
    assert "hashtag" not in refusals(GOOD + " number 2 of 5")


# ── promotion, mirroring Y1 ─────────────────────────────────────────────────

@pytest.mark.parametrize("text,reason", [
    ("see synthetic.com for more", "link"),
    ("@another_reader you should see this", "@-mention"),
    ("check out my page for more gouache", "promotion phrase"),
    ("dev_tester here, nice series", "our account"),
    ("Wow the second page", "banned opener 'Wow'"),
])
def test_promotion_refusals(text, reason):
    assert reason in refusals(text)
    assert reason not in refusals(GOOD)


def test_no_identity_configured_refuses_every_draft():
    cfg = ie.InstagramConfig("t", "", 20000, True)
    assert "config: platforms.instagram.identity_slug must be set" in refusals(GOOD, cfg=cfg)


# ── language ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,lang", [
    ("is this from the new series?", "en"),
    ("me encanta el color de la segunda página, es muy bonito", "es"),
    ("c'est très beau, le travail sur la couleur est super", "fr"),
    ("das ist wirklich schön und die Farbe ist toll", "de"),
    ("это очень красиво", "cyrillic"),
    ("これはとても美しいです", "ja"),
    ("so good", "latin"),
    ("#art #gouache", "unknown"),
])
def test_detect_language(text, lang):
    assert ie.detect_language(text) == lang


def test_a_draft_in_another_language_than_the_caption_is_refused():
    es = "me encanta el color de la segunda página, es muy bonito"
    assert any(r.startswith("language: caption is es, draft is en")
               for r in refusals(GOOD, caption=es))
    assert not any(r.startswith("language") for r in refusals(GOOD))


def test_a_different_script_is_refused():
    ru = "это очень красиво"
    assert any(r.startswith("language: caption is cyrillic") for r in refusals(GOOD, caption=ru))


def test_an_unknown_caption_language_is_not_a_refusal():
    assert not any(r.startswith("language") for r in refusals(GOOD, caption="#gouache"))


# ── near-duplicates to one author ───────────────────────────────────────────

def test_similarity_measure():
    assert ie.similarity("is this from the new series?", "Is this from the new series") == 1.0
    assert ie.similarity("the colour shift is so good", "how long did the lettering take") < 0.2


def test_a_near_duplicate_to_the_same_author_is_refused():
    prev = ["is this from the new series??"]
    assert any(r.startswith("near_duplicate") for r in refusals(GOOD, previous=prev))
    assert not any(r.startswith("near_duplicate")
                   for r in refusals(GOOD, previous=["how long did the lettering take"]))


def test_previous_comments_are_ours_to_the_same_author(tmp_path):
    store = post_store.PostStore("t", path=str(tmp_path / "db.json"), platform="instagram")

    def add(key, author, status, comment, approved=False):
        store.posts[key] = {"key": key, "status": status, "comment": comment,
                            "instagram": {"author_handle": author}}
        if approved:
            store.posts[key].update(reviewed_at="x",
                                    approved_hash=post_store.text_hash(comment))
    add("A1", "synthetic_painter", post_store.COMMENTED, "first one")
    add("A2", "synthetic_painter", post_store.GENERATED, "approved draft", approved=True)
    add("A3", "synthetic_painter", post_store.GENERATED, "unapproved draft")
    add("B1", "another_reader", post_store.COMMENTED, "someone else")
    add("ME", "synthetic_painter", post_store.GENERATED, "this one")
    assert sorted(ie.previous_to_author(store, store.get("ME"))) == ["approved draft",
                                                                     "first one"]


# ── appearance ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "you look amazing here", "looking good in this one", "you're so pretty",
    "your smile makes this", "body goals honestly"])
def test_appearance_compliments_are_refused(text):
    assert any(r.startswith("appearance") for r in refusals(text))


@pytest.mark.parametrize("text", [
    "the colour on the second page is beautiful", "how long did the lettering take",
    "the light in this looks great"])
def test_comments_on_the_work_are_not_appearance(text):
    assert not any(r.startswith("appearance") for r in refusals(text))


# ── config ──────────────────────────────────────────────────────────────────

def _cfg(overlay):
    return {"scheduled_posting": {"platforms": {"instagram": overlay}}}


def test_config_reads_identity_and_threshold():
    cfg = ie.instagram_config("t", _cfg({"identity_slug": "@Dev_Tester",
                                         "engagement": {"max_followers": 5000}}))
    assert cfg.identity_slug == "dev_tester"
    assert cfg.max_followers == 5000 and cfg.skip_verified is True


def test_absent_config_denies():
    cfg = ie.instagram_config("t", {})
    assert cfg.identity_slug == "" and cfg.max_followers is None
    assert ie.account_refusal({"handle": "a", "followers": 10, "verified": False},
                              cfg) == ie.SKIP_NO_THRESHOLD


@pytest.mark.parametrize("overlay", [
    {"identity_slug": "x", "buffer_channel_id": "1"},
    {"engagement": {"max_followers": 10, "min_followers": 1}},
    {"engagement": {"max_followers": "many"}},
    {"engagement": {"max_followers": 0}},
    {"engagement": {"max_followers": True}},
    {"engagement": {"skip_verified": "yes"}},
    {"identity_slug": "not a handle!"},
    {"engagement": []},
])
def test_bad_config_is_refused(overlay):
    with pytest.raises(ie.InstagramRefused):
        ie.instagram_config("t", _cfg(overlay))


# ── the find-side account filter ────────────────────────────────────────────

@pytest.mark.parametrize("account,reason", [
    ({"handle": "small", "followers": 8421, "verified": False}, None),
    ({"handle": "big", "followers": 20001, "verified": False},
     "mega_account: 20001 followers > 20000"),
    ({"handle": "badge", "followers": 900, "verified": True}, "verified_account"),
    ({"handle": "q", "followers": None, "verified": False}, "followers_unknown"),
    ({"handle": "q", "followers": 10, "verified": None}, "verified_unknown"),
    ({"handle": None}, "account_unknown"),
    ({"handle": "dev_tester", "followers": 41, "verified": False}, "our_own_account"),
])
def test_account_filter(account, reason):
    assert ie.account_refusal(account, CFG) == reason


def test_verified_is_allowed_only_when_configured_so():
    cfg = ie.InstagramConfig("t", "dev_tester", 20000, False)
    assert ie.account_refusal({"handle": "badge", "followers": 900, "verified": True},
                              cfg) is None


@pytest.mark.parametrize("text,n", [("1,234", 1234), ("12.5K", 12500), ("1.2M", 1200000),
                                    ("41", 41), ("lots", None), ("", None)])
def test_parse_count(text, n):
    assert ie.parse_count(text) == n


# ── back to review ──────────────────────────────────────────────────────────

def test_a_refused_draft_goes_back_to_review_with_its_reason(tmp_path):
    store = post_store.PostStore("t", path=str(tmp_path / "db.json"), platform="instagram")
    store.posts["K"] = {"key": "K", "status": post_store.GENERATED, "comment": "#tag me",
                        "text": CAPTION, "instagram": {"author_handle": "a"}}
    store.approve("K")
    rec = store.get("K")
    assert post_store.is_approved(rec)
    reason = ie.draft_refusal(store, rec, CFG)
    assert reason.startswith("instagram_rules: ") and "hashtag" in reason
    ie.send_back_to_review(store, rec, reason)
    rec = post_store.PostStore("t", path=str(tmp_path / "db.json"),
                               platform="instagram").get("K")
    assert not post_store.is_approved(rec)
    assert rec["comment"] == "#tag me" and rec["status"] == post_store.GENERATED
    assert rec["comment_meta"]["instagram_rules"] == reason


def test_approve_refusal_runs_the_validator_on_the_text_being_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_profile_config", lambda n=None: _cfg(
        {"identity_slug": "dev_tester", "engagement": {"max_followers": 100}}))
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(tmp_path))
    rec = {"key": "K", "text": CAPTION, "comment": GOOD}
    assert ie.approve_refusal("t", rec, GOOD) is None
    assert "hashtag" in ie.approve_refusal("t", rec, GOOD + " #x")
