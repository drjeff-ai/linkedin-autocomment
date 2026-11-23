"""E1 (D-027): the engagement adapter boundary, PLATFORM_REFACTOR §9.

Every verdict an adapter gives is tested both ways (PRINCIPLES §3): classify
ok and gone and unclear, the thread check True and False, the like placed,
already liked and missed, post-and-verify True and False, the selector
contract held and broken. LinkedIn runs against the synthetic post page the
poster's own tests use; YouTube against stubbed API reads. Nothing here opens
a socket or a browser.
"""

import json
import os
import time

import pytest

from linkedin_automation import comment_poster as cpm
from linkedin_automation import engagement_adapter as ea
from linkedin_automation import linkedin_adapter as la
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import selector_health as sh
from linkedin_automation import youtube_api
from linkedin_automation import youtube_engage as ye

from e1_golden_harness import GoldenPage
from fake_post_page import FakeElement

A = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"
OK_URL = A.format("1110000000000000001")
GONE_URL = A.format("1110000000000000010")
SLOW_URL = A.format("1110000000000000011")
BLANK_URL = A.format("1110000000000000100")
OURS_URL = A.format("1110000000000000101")
DEAD_URL = A.format("1110000000000000110")
NOLIKE_URL = A.format("1110000000000000111")
LIKED_URL = A.format("1110000000000001000")
TEXT = "A synthetic adapter comment about eval drift."

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "e1_golden", "expected.json")


class _Page(GoldenPage):
    """GoldenPage plus a post that is already liked (a known liked state)."""

    def __init__(self, liked_urls=(), **kw):
        self.liked_urls = set(liked_urls)
        super().__init__(**kw)
        self.liked_marker = FakeElement(
            self, "Like", attrs={"aria-label": "Reaction button state: Liked"})

    def find_elements(self, by, selector):
        if self.current_url in self.liked_urls:
            P = cpm.LinkedInCommentPoster
            if selector == P.LIKE_BUTTON_SELECTORS[0]:
                return []
            if selector == P.LIKED_STATE_SELECTORS[0]:
                return [self.liked_marker]
        return super().find_elements(by, selector)


@pytest.fixture
def page(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "failures").mkdir(parents=True)
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "t")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_progress_file", lambda n=None: str(data / "p.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_profile_config", lambda n=None: {"behavior": {}})
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(data / (subdir or "")))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    P = cpm.LinkedInCommentPoster
    for name, value in (("NAV_DECIDE_SECONDS", 0.05), ("LIKE_WAIT_SECONDS", 0.02),
                        ("VERIFY_TIMEOUT", 0.05), ("VERIFY_POLL", 0.01)):
        monkeypatch.setattr(P, name, value)
    return _Page(gone_urls={GONE_URL}, timeout_urls={SLOW_URL}, blank_urls={BLANK_URL},
                 ours_urls={OURS_URL}, dead_submit_urls={DEAD_URL},
                 like_absent_urls={NOLIKE_URL}, liked_urls={LIKED_URL})


@pytest.fixture
def adapter(page):
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    poster.driver = page
    return la.LinkedInAdapter(profile_name="t", poster=poster)


def _rec(url, text=TEXT):
    return {"key": url, "url": url, "comment": text}


# ─── The interface ───────────────────────────────────────────────────────────

def test_both_platforms_implement_the_interface():
    assert issubclass(la.LinkedInAdapter, ea.EngagementAdapter)
    assert issubclass(ye.YouTubeAdapter, ea.EngagementAdapter)
    assert la.LinkedInAdapter.kind == ea.BROWSER
    assert ye.YouTubeAdapter.kind == ea.API


def test_an_incomplete_adapter_cannot_be_built():
    class Half(ea.EngagementAdapter):
        def find(self, **o):
            return None
    with pytest.raises(TypeError):
        Half()


@pytest.mark.parametrize("bad", ["", "maybe", "UNAVAILABLE"])
def test_classify_outcomes_are_closed(bad):
    with pytest.raises(ValueError):
        ea.Classification(bad)


def test_like_states_are_closed_and_liked_only_when_placed_or_already():
    with pytest.raises(ValueError):
        ea.LikeResult("liked", True)
    assert ea.LikeResult(ea.LIKE_PLACED, False).liked
    assert ea.LikeResult(ea.LIKE_ALREADY, True).liked
    assert not ea.LikeResult(ea.LIKE_MISS, False).liked
    assert not ea.LikeResult(ea.LIKE_NOT_APPLICABLE, False).liked


# ─── LinkedIn, method by method ──────────────────────────────────────────────

@pytest.mark.parametrize("url, outcome, detail", [
    (OK_URL, ea.OK, None),
    (GONE_URL, ea.GONE, cpm.LinkedInCommentPoster.NAV_UNAVAILABLE),
    (SLOW_URL, ea.UNCLEAR, cpm.LinkedInCommentPoster.NAV_TIMEOUT),
    (BLANK_URL, ea.UNCLEAR, cpm.LinkedInCommentPoster.NAV_UNCLEAR),
])
def test_linkedin_classify(adapter, url, outcome, detail):
    cls = adapter.classify(_rec(url))
    assert cls.outcome == outcome
    assert cls.detail == detail
    assert cls.gone is (outcome == ea.GONE)


@pytest.mark.parametrize("url, already", [(OURS_URL, True), (OK_URL, False)])
def test_linkedin_already_commented_asks_the_thread(adapter, url, already):
    assert adapter.classify(_rec(url)).ok
    check = adapter.already_commented(_rec(url))
    assert check.already is already
    assert check.reason


def test_linkedin_already_commented_on_the_exact_text(adapter, page):
    page.exact_urls.add(OK_URL)
    page.exact_text[OK_URL] = TEXT
    adapter.classify(_rec(OK_URL))
    assert adapter.already_commented(_rec(OK_URL)).already is True


@pytest.mark.parametrize("url, state, proven", [
    (OK_URL, ea.LIKE_PLACED, False),
    (LIKED_URL, ea.LIKE_ALREADY, True),
    (NOLIKE_URL, ea.LIKE_MISS, False),
])
def test_linkedin_like_names_the_state(adapter, page, url, state, proven):
    adapter.classify(_rec(url))
    result = adapter.like(_rec(url))
    assert (result.state, result.proven) == (state, proven)
    assert result.liked is (state != ea.LIKE_MISS)
    assert (url in page.liked) is (state == ea.LIKE_PLACED)


@pytest.mark.parametrize("url, posted", [(OK_URL, True), (DEAD_URL, False)])
def test_linkedin_post_and_verify(adapter, page, url, posted):
    adapter.classify(_rec(url))
    result = adapter.post_and_verify(_rec(url))
    assert result.posted is posted
    assert result.outcome == (ea.POSTED if posted else ea.FAILED)
    assert ((url, TEXT) in page.published) is posted


def test_linkedin_native_id_is_the_posters_identity(adapter):
    assert adapter.native_id(_rec(OK_URL)) == "activity:1110000000000000001"
    assert adapter.native_id({"url": "https://www.linkedin.com/feed/"}) is None


def test_linkedin_find_delegates_to_the_finder():
    class Finder:
        def run(self, max_posts, min_quality):
            self.args = (max_posts, min_quality)
            return "out.json"
    f = Finder()
    assert la.LinkedInAdapter("t").find(max_posts=7, min_quality=3, finder=f) == "out.json"
    assert f.args == (7, 3)


# ─── The shared flow, driven through the LinkedIn adapter ────────────────────

def _approved(url, text=TEXT):
    s = post_store.PostStore(profile_name="t")
    s.mark_generated(url, text)
    assert s.approve(url)
    s.save()
    return s.get(url)


@pytest.mark.parametrize("url, outcome", [
    (OK_URL, ea.ENGAGE_POSTED), (GONE_URL, ea.ENGAGE_GONE),
    (BLANK_URL, ea.ENGAGE_UNCLEAR), (OURS_URL, ea.ENGAGE_ALREADY),
    (DEAD_URL, ea.ENGAGE_FAILED),
])
def test_engage_one_gives_the_posters_decisions(adapter, page, url, outcome):
    rec = _approved(url)
    result = ea.engage_one(adapter, rec, reload=post_store.PostStore("t").get)
    assert result.outcome == outcome
    assert bool(page.published) is (outcome == ea.ENGAGE_POSTED)


def test_engage_one_opens_nothing_for_an_unapproved_draft(adapter, page):
    s = post_store.PostStore(profile_name="t")
    s.mark_generated(OK_URL, TEXT)
    s.save()
    result = ea.engage_one(adapter, s.get(OK_URL))
    assert result.outcome == ea.ENGAGE_NOT_APPROVED
    assert result.reason == post_store.REFUSE_NOT_REVIEWED
    assert page.gets == [] and page.published == []


def test_engage_one_rechecks_approval_last(adapter, page):
    """Approval withdrawn after the post was opened: nothing is posted."""
    rec = _approved(OK_URL)

    def reload(key):
        s = post_store.PostStore("t")
        s.save_edit(key, "an edit made while the post was open")
        s.save()
        return s.get(key)
    result = ea.engage_one(adapter, rec, reload=reload)
    assert result.outcome == ea.ENGAGE_NOT_APPROVED
    assert page.gets == [OK_URL] and page.published == []


@pytest.mark.parametrize("record, text, strip, reason", [
    ("approved", TEXT, True, None),
    ("approved", "  " + TEXT + " ", True, None),
    ("approved", "  " + TEXT + " ", False, post_store.REFUSE_TEXT_DIFFERS),
    ("approved", "other text", True, post_store.REFUSE_TEXT_DIFFERS),
    ("unapproved", TEXT, True, post_store.REFUSE_NOT_REVIEWED),
    ("missing", TEXT, True, post_store.REFUSE_NO_RECORD),
])
def test_the_shared_approval_check(tmp_path, record, text, strip, reason):
    s = post_store.PostStore(path=str(tmp_path / "db.json"))
    s.mark_generated(OK_URL, TEXT)
    if record == "approved":
        s.approve(OK_URL)
    rec = None if record == "missing" else s.get(OK_URL)
    assert ea.approval_refusal_for(rec, text, strip=strip) == reason


# ─── selector_health ─────────────────────────────────────────────────────────

def test_linkedin_selectors_are_registered():
    assert ea.selector_contract_problems(la.LinkedInAdapter("t"), sh.SELECTOR_REGISTRY) == []


def test_youtube_is_exempt_and_says_why():
    a = ye.YouTubeAdapter("p")
    assert a.selectors() is None and a.SELECTORS_EXEMPT_REASON
    assert ea.selector_contract_problems(a, sh.SELECTOR_REGISTRY) == []


def test_the_selector_contract_can_fail():
    class Unregistered(la.LinkedInAdapter):
        def selectors(self):
            return {"not_in_the_registry": {"selectors": ["div.x"]}}

    class Drifted(la.LinkedInAdapter):
        def selectors(self):
            out = super().selectors()
            out["post_detail"] = dict(out["post_detail"], selectors=["div.other"])
            return out

    class Silent(la.LinkedInAdapter):
        def selectors(self):
            return {}

    class ApiWithSelectors(ye.YouTubeAdapter):
        def selectors(self):
            return {"post_detail": {}}

    class ApiNoReason(ye.YouTubeAdapter):
        SELECTORS_EXEMPT_REASON = None

    for cls in (Unregistered, Drifted, Silent):
        assert ea.selector_contract_problems(cls("t"), sh.SELECTOR_REGISTRY), cls
    for cls in (ApiWithSelectors, ApiNoReason):
        assert ea.selector_contract_problems(cls("p"), sh.SELECTOR_REGISTRY), cls


def _selector_sets(registry):
    return {k: list(v["selectors"]) for k, v in registry.items()}


def test_the_registry_covers_the_same_selectors_as_before_e1():
    """Every key, and every selector under it, as the pre-E1 registry had."""
    with open(GOLDEN, encoding="utf-8") as f:
        before = json.load(f)["selectors"]
    # Instagram's ig_* entries (D-027 E3) are additions; everything else is
    # exactly the pre-E1 registry.
    now = {k: v for k, v in sh.SELECTOR_REGISTRY.items() if not k.startswith("ig_")}
    assert _selector_sets(now) == _selector_sets(before)
    linkedin_before = {s for k, v in before.items() if not k.startswith("x_")
                       for s in v["selectors"]}
    linkedin_now = {s for k, v in now.items() if not k.startswith("x_")
                    for s in v["selectors"]}
    assert linkedin_now == linkedin_before and linkedin_now


def test_the_posting_entries_come_from_the_adapter():
    entries = la.registry_entries(sh.COMMENT_BOX_SELECTOR)
    assert tuple(entries) == la.POST_REGISTRY_KEYS
    for key, entry in entries.items():
        assert sh.SELECTOR_REGISTRY[key] == entry


# ─── The LinkedIn handoff ────────────────────────────────────────────────────

def test_the_scheduler_handoff_is_the_adapters():
    from linkedin_automation import scheduler
    assert scheduler.write_comments_file is la.write_comments_file


def test_the_subprocess_commands_keep_allow_production():
    assert la.poster_command("c.txt", 3, "p")[1:] == [
        "-m", "linkedin_automation.comment_poster", "c.txt", "--count", "3",
        "--profile", "p", "--allow-production"]
    assert la.finder_command(50, 10, "p")[1:] == [
        "-m", "linkedin_automation.post_finder", "--max-posts", "50",
        "--min-quality", "10", "--profile", "p", "--allow-production"]


# ─── YouTube as an adapter ───────────────────────────────────────────────────

YREC = {"key": "vid00000001", "comment": "A synthetic reply.",
        "youtube": {"kind": ye.KIND_VIDEO, "video_id": "vid00000001"}}


@pytest.fixture
def reads(monkeypatch):
    calls = []

    def answer(result):
        def fake(g, rec):
            calls.append(rec["key"])
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(ye, "already_commented", fake)
    return answer, calls


@pytest.mark.parametrize("result, outcome, detail", [
    (None, ea.OK, None),
    ("Ugx_ours", ea.OK, None),
    (youtube_api.YouTubeApiError("videoNotFound", status=404), ea.GONE, "gone"),
    (youtube_api.YouTubeApiError("commentsDisabled", status=403), ea.GONE,
     "comments_disabled"),
    (youtube_api.YouTubeApiError("backendError", status=503, ambiguous=True),
     ea.UNCLEAR, "unclear"),
])
def test_youtube_classify(reads, result, outcome, detail):
    answer, _calls = reads
    answer(result)
    cls = ye.YouTubeAdapter("p").classify(YREC)
    assert (cls.outcome, cls.detail) == (outcome, detail)


@pytest.mark.parametrize("found, already", [("Ugx_ours", True), (None, False)])
def test_youtube_already_commented_and_one_read(reads, found, already):
    answer, calls = reads
    answer(found)
    a = ye.YouTubeAdapter("p")
    assert a.classify(YREC).ok
    check = a.already_commented(YREC)
    assert check.already is already and check.evidence == found
    assert calls == [YREC["key"]]          # classify and the thread: one read


def test_youtube_already_commented_raises_the_read_error(reads):
    answer, _ = reads
    answer(youtube_api.YouTubeApiError("backendError", status=500, ambiguous=True))
    with pytest.raises(youtube_api.YouTubeApiError):
        ye.YouTubeAdapter("p").already_commented(YREC)


@pytest.mark.parametrize("ret, posted", [
    ((ye.POSTED_VISIBLE, "Ugx1", None, False, False), True),
    ((ye.POSTED_HELD, "Ugx1", "not listed", False, False), False),
    ((ye.FAILED, None, "badRequest", False, False), False),
])
def test_youtube_post_and_verify(monkeypatch, ret, posted):
    monkeypatch.setattr(ye, "post_and_verify", lambda g, rec: ret)
    result = ye.YouTubeAdapter("p").post_and_verify(YREC)
    assert result.posted is posted
    assert (result.outcome, result.proof, result.reason) == ret[:3]


def test_youtube_like_is_not_applicable():
    result = ye.YouTubeAdapter("p").like(YREC)
    assert result.state == ea.LIKE_NOT_APPLICABLE
    assert not result.liked and not result.proven and result.reason
