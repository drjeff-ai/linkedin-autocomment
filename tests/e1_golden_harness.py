"""E1 (D-027): the LinkedIn decision golden, captured BEFORE the adapter refactor.

Not a test module (no ``test_`` prefix). ``tests/test_e1_golden.py`` drives
:func:`capture` and compares its JSON, line for line, with
``tests/fixtures/e1_golden/expected.json``, which was written by this same
harness from the code at ``main`` @ ``e8d58b7`` (E0 + Y1, no adapter yet).

What it records, all on synthetic fixtures (no PII, no network, no browser):

* **poster** - ``LinkedInCommentPoster.run`` over a TXT handoff against a fake
  post page with one URL per outcome: posted, like missing, gone, page-load
  timeout, unclear page, our comment already there (marker and exact text),
  submit that never lands, not approved, edited after approval, bad URL,
  already in the ledger, and past the count. It keeps which comments were
  published and liked, the run result, the progress ledger, the store
  statuses reconciled from it, the poster's own log lines, the failure
  captures written and the RUN summary counts.
* **scheduler** - ``Scheduler._fire_post_comments``: which records it selects,
  the count, the note, the TXT it writes, what it submits, stale expiry.
* **dashboard** - the Post route and the auto_cycle post stage: the response,
  the TXT handed to the poster and the exact poster argv.
* **prompts** - the LinkedIn and X prompts the generator composes for fixture
  posts (evaluate, generate, relevance), with the OpenAI client faked.
* **selectors** - the selector_health registry, entry by entry.
* **identity** - ``post_identity`` on the URL shapes the poster keys on.

Normalisation removes only what changes from run to run (timestamps, temp
paths, float timings, the interpreter path). Anything a decision depends on
is kept.
"""

import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime

from linkedin_automation import comment_fields
from linkedin_automation import comment_generator as gen
from linkedin_automation import comment_poster as cpm
from linkedin_automation import platform_policy as pp
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm

from fake_post_page import FakePostPage

PROFILE = "goldenprof"
A = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"

# One synthetic URL per decision the poster can make.
URLS = {
    "ok_1": A.format("1000000000000000001"),
    "like_absent": A.format("1000000000000000010"),
    "gone": A.format("1000000000000000011"),
    "timeout": A.format("1000000000000000100"),
    "unclear": A.format("1000000000000000101"),
    "ours_marker": A.format("1000000000000000110"),
    "ours_exact": A.format("1000000000000000111"),
    "dead_submit": A.format("1000000000000001000"),
    "unapproved": A.format("1000000000000001001"),
    "edited": A.format("1000000000000001010"),
    "in_ledger": A.format("1000000000000001011"),
    "ok_2": A.format("1000000000000001100"),
    "ok_3": A.format("1000000000000001101"),
    "past_count": A.format("1000000000000001110"),
}
BAD_URL = "not-a-url/1000000000000111111"

#: The order of the TXT handoff. ``bad_url`` is the one invalid block.
ORDER = ["ok_1", "like_absent", "gone", "timeout", "unclear", "ours_marker",
         "ours_exact", "dead_submit", "unapproved", "edited", "bad_url",
         "in_ledger", "ok_2", "ok_3", "past_count"]

#: Successes before ``past_count``: ok_1, like_absent, ours_marker,
#: ours_exact, ok_2, ok_3 (the two thread skips count as posted). Six stops
#: the run at ok_3, so past_count is never reached.
POST_COUNT = 6


def _text(name):
    return "A synthetic golden comment for %s, about eval drift." % name


class GoldenPage(FakePostPage):
    """FakePostPage plus the page states the poster distinguishes."""

    def __init__(self, ours_urls=(), exact_urls=(), dead_submit_urls=(),
                 blank_urls=(), exact_text=None, **kw):
        self.ours_urls = set(ours_urls)
        self.exact_urls = set(exact_urls)
        self.dead_submit_urls = set(dead_submit_urls)
        self.blank_urls = set(blank_urls)
        self.exact_text = exact_text or {}
        super().__init__(**kw)

    def get(self, url):
        super().get(url)
        if url in self.ours_urls:
            self.thread = ["Golden Member • You\nAn earlier remark of ours."]
        elif url in self.exact_urls:
            self.thread = ["Someone Else\n%s" % self.exact_text[url]]

    def _submit(self):
        if self.current_url in self.dead_submit_urls:
            return                       # the click lands nowhere
        super()._submit()

    def find_elements(self, by, selector):
        if self.current_url in self.blank_urls:
            return []
        return super().find_elements(by, selector)


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        line = "%s %s" % (record.levelname, record.getMessage())
        # A bounded poll logs once per pass, and how many passes fit in its
        # budget is wall-clock, not a decision. A run of one line is kept once.
        if self.lines and self.lines[-1] == line:
            return
        self.lines.append(line)


def _norm(s, tmp):
    s = str(s)
    for root in {str(tmp), str(tmp).replace("\\", "/"),
                 str(tmp).replace("\\", "\\\\")}:
        s = s.replace(root, "<tmp>")
    s = s.replace(sys.executable, "<python>")
    s = s.replace("\\", "/")
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?", "<ts>", s)
    s = re.sub(r"\d{8}_\d{6}(_\d+)?", "<stamp>", s)
    s = re.sub(r"\d+\.\d+", "<f>", s)
    s = re.sub(r"Waiting \d+s", "Waiting <n>s", s)
    return s


def _norm_obj(obj, tmp):
    if isinstance(obj, dict):
        return {k: _norm_obj(v, tmp) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_norm_obj(v, tmp) for v in obj]
    if isinstance(obj, str):
        return _norm(obj, tmp)
    return obj


def _redirect(monkeypatch, tmp):
    data = tmp / "data"
    (data / "failures").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: PROFILE)
    monkeypatch.setattr(pm, "get_comments_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(data / "posting_progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda n=None: {"behavior": {}})

    def _data_dir(profile_name=None, subdir=None):
        path = data / subdir if subdir else data
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    monkeypatch.setattr(pm, "get_data_dir", _data_dir)
    monkeypatch.setattr(pm, "login", lambda d, p: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return data


def _seed_store(records):
    s = post_store.PostStore(profile_name=PROFILE)
    for key, text, approved in records:
        s.mark_generated(key, text)
        if approved:
            assert s.approve(key), key
    s.save()
    return s


def _store_view(store):
    out = {}
    for key in sorted(store.posts):
        r = store.posts[key]
        out[key] = {"status": r.get("status"),
                    "trash_reason": r.get("trash_reason"),
                    "approved": post_store.is_approved(r),
                    "comment": r.get("comment")}
    return out


# ─── poster ──────────────────────────────────────────────────────────────────

def capture_poster(monkeypatch, tmp):
    data = _redirect(monkeypatch, tmp)
    P = cpm.LinkedInCommentPoster
    for name, value in (("NAV_DECIDE_SECONDS", 0.05), ("LIKE_WAIT_SECONDS", 0.02),
                        ("VERIFY_TIMEOUT", 0.05), ("VERIFY_POLL", 0.01),
                        ("SUBMIT_ENABLE_TIMEOUT", 0.5),
                        ("COMMENT_INPUT_WAIT_SECONDS", 0.5)):
        monkeypatch.setattr(P, name, value)

    page = GoldenPage(
        gone_urls={URLS["gone"]}, like_absent_urls={URLS["like_absent"]},
        timeout_urls={URLS["timeout"]}, ours_urls={URLS["ours_marker"]},
        exact_urls={URLS["ours_exact"]},
        exact_text={URLS["ours_exact"]: _text("ours_exact")},
        dead_submit_urls={URLS["dead_submit"]}, blank_urls={URLS["unclear"]})
    monkeypatch.setattr(pm, "create_driver",
                        lambda name=None, headless=False, allow_production=False:
                        (page, {}))

    blocks, seed = [], []
    for name in ORDER:
        url = BAD_URL if name == "bad_url" else URLS[name]
        text = _text(name)
        blocks.append({"url": url, "comment": text, "post_preview": "preview %s" % name,
                       "author": "Golden Author", "post_text": "Body of %s." % name})
        if name == "bad_url":
            continue
        if name == "unapproved":
            seed.append((url, text, False))
        elif name == "edited":
            seed.append((url, "The approved text, before an edit.", True))
        else:
            seed.append((url, text, True))
    _seed_store(seed)
    with open(data / "posting_progress.json", "w") as f:
        json.dump({"posted_comments": [URLS["in_ledger"]]}, f)

    txt = data / "golden_comments.txt"
    txt.write_text(comment_fields.comments_to_txt(blocks, "2026-11-22 10:00"),
                   encoding="utf-8")

    handler = _Collect()
    log = logging.getLogger("linkedin_automation.comment_poster")
    old_level = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    random.seed(20261122)
    try:
        poster = P(profile_name=PROFILE)
        result = poster.run(str(txt), post_count=POST_COUNT)
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)

    with open(data / "posting_progress.json", encoding="utf-8") as f:
        progress = json.load(f)
    with open(poster.run_log_path, encoding="utf-8") as f:
        run_lines = [ln for ln in f.read().splitlines() if " RUN " in ln]
    run_counts = {}
    for ln in run_lines:
        for k, v in re.findall(r"\b(attempted|posted|skipped|failed|unavailable|"
                               r"like_misses|comments_timed)=(\d+)", ln):
            run_counts[k] = int(v)
    failures = sorted(_norm(n, tmp) for n in os.listdir(data / "failures"))
    store = post_store.load_synced_store(PROFILE)
    return _norm_obj({
        "result": result,
        "published": page.published,
        "liked": page.liked,
        "gets": page.gets,
        "progress": progress,
        "store": _store_view(store),
        "log": handler.lines,
        "failure_files": failures,
        "run_summary": run_counts,
        "like_misses": poster.like_misses,
    }, tmp)


# ─── scheduler ───────────────────────────────────────────────────────────────

def _rec(i, status=post_store.GENERATED, comment=None, approved=False,
         generated_at="2026-11-21T09:00:00", **extra):
    url = A.format("1100000000000" + format(i, "06b"))
    comment = comment if comment is not None else "Scheduler draft number %d." % i
    r = {"key": url, "url": url, "author": "Author %d" % i,
         "text": "Synthetic post body %d." % i, "category": "AI",
         "relevance_score": 5, "status": status, "trash_reason": None,
         "comment": comment, "comment_meta": {"word_count": 4, "style": "s%d" % i,
                                              "approach": "a%d" % i},
         "scraped_at": generated_at, "generated_at": generated_at,
         "commented_at": None, "reviewed_at": None, "approved_hash": None,
         "updated_at": generated_at}
    if approved:
        r["reviewed_at"] = "2026-11-22T08:00:00"
        r["approved_hash"] = post_store.text_hash(comment)
    r.update(extra)
    return r


def _sched_records():
    return [
        _rec(1, approved=True), _rec(2), _rec(3, approved=True),
        dict(_rec(4, approved=True), comment="edited after approval"),
        _rec(5, status=post_store.COMMENTED, approved=True),
        _rec(6, approved=True, generated_at="2026-10-01T09:00:00"),   # stale
        _rec(7, approved=True), _rec(8, reviewed_at="2026-07-01T00:00:00"),
        _rec(9, approved=True),
    ]


def capture_scheduler(monkeypatch, tmp):
    from linkedin_automation import scheduler as sched
    out = {}
    cases = {
        "mixed": (_sched_records(), {"count_min": 2, "count_max": 3}),
        "count_above_queue": (_sched_records(), {"count_min": 9, "count_max": 12}),
        "inverted_bounds": (_sched_records(), {"count_min": 5, "count_max": 1}),
        "none_approved": ([_rec(2), _rec(8, reviewed_at="2026-07-01T00:00:00")], {}),
        "empty": ([_rec(5, status=post_store.COMMENTED)], {}),
    }
    for name, (recs, job_cfg) in cases.items():
        d = tmp / ("sched_" + name)
        d.mkdir()
        s = post_store.PostStore(path=str(d / "posts_db.json"))
        for r in recs:
            s.posts[r["key"]] = dict(r)
        s.save()
        monkeypatch.setattr(post_store, "load_synced_store",
                            lambda p=None, _s=s, **kw: _s)
        monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None, _d=d: str(_d))
        calls = []
        engine = sched.Scheduler(
            submit_post_job=lambda p, f, c: calls.append((p, f, c)) or "job-1",
            list_profiles=lambda: ["p"], rng=random.Random(7),
            config_fn=lambda p: {"scheduler": {"draft_max_age_days": 14}})
        result = engine._fire_post_comments("p", job_cfg, datetime(2026, 11, 22, 10, 30))
        txts = {}
        for p, f, c in calls:
            with open(f, encoding="utf-8") as fh:
                txts[os.path.basename(f)] = fh.read()
        out[name] = _norm_obj({"result": result,
                               "calls": [(p, os.path.basename(f), c) for p, f, c in calls],
                               "txt": txts, "store": _store_view(s)}, tmp)
    return out


# ─── dashboard: the Post route and auto_cycle's post stage ───────────────────

def capture_dashboard(monkeypatch, tmp, api_client):
    from linkedin_automation import dashboard as dash
    from linkedin_automation import scheduler as sched
    out = {}
    cmds = []

    class _Frozen(datetime):
        """The dashboard's clock, pinned so stale-draft expiry is not a date."""
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 22, 11, 0)
    monkeypatch.setattr(dash, "datetime", _Frozen)

    def fake_subprocess(job_id, cmd, *a, **k):
        cmds.append(list(cmd))
        return 0, ""
    monkeypatch.setattr(dash, "run_subprocess", fake_subprocess)
    monkeypatch.setattr(dash, "can_start_browser_task", lambda *a, **k: True)
    monkeypatch.setattr(dash, "_automations_refusal", lambda *a, **k: None)
    jobs = []

    def fake_run_job(job_id, fn, *args, **kwargs):
        jobs.append({"fn": fn.__name__, "args": [os.path.basename(str(a))
                                                 if str(a).endswith(".txt") else a
                                                 for a in args],
                     "kwargs": kwargs})
        res = fn(_norm(job_id, tmp), *args)
        txt = next((a for a in args if str(a).endswith(".txt")), None)
        if txt:
            with open(txt, encoding="utf-8") as f:
                jobs[-1]["txt"] = f.read()
        jobs[-1]["returned"] = res
    monkeypatch.setattr(dash, "run_job", fake_run_job)

    def seed(recs):
        s = post_store.PostStore(PROFILE)
        s.posts.clear()
        for r in recs:
            s.posts[r["key"]] = dict(r)
        s.save()

    for name, recs, body in (
            ("post_mixed", _sched_records(), {"count": 2}),
            ("post_none_approved", [_rec(2)], {"count": 1}),
            ("post_empty", [], {"count": 1}),
            ("post_hand_file", [_rec(1, approved=True)], {"comments_file": "x.txt"})):
        seed(recs)
        jobs.clear()
        cmds.clear()
        resp = api_client.post("/api/post/%s" % PROFILE, json=body)
        js = resp.get_json() or {}
        js.pop("job_id", None)
        out[name] = _norm_obj({"status": resp.status_code, "json": js,
                               "jobs": jobs[:], "cmds": cmds[:]}, tmp)

    # auto_cycle's post stage, the scrape and generate stages stubbed.
    monkeypatch.setattr(dash, "_scrape_job", lambda *a, **k: {"count": 3})
    monkeypatch.setattr(dash, "_generate_job", lambda *a, **k: {"generated": 2})
    monkeypatch.setattr(pm, "profile_automations_enabled", lambda *a, **k: True)
    real_cfg = pm.get_profile_config
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda n=None: {"scheduler": {"draft_max_age_days": 14}})
    real_write = sched.write_comments_file
    written = []

    def spy_write(profile, records, now):
        path = real_write(profile, records, datetime(2026, 11, 22, 11, 0))
        with open(path, encoding="utf-8") as f:
            written.append(f.read())
        return path
    monkeypatch.setattr(sched, "write_comments_file", spy_write)
    for name, recs in (("cycle_mixed", _sched_records()),
                       ("cycle_none_approved", [_rec(2)]),
                       ("cycle_empty", [])):
        seed(recs)
        cmds.clear()
        written.clear()
        random.seed(99)
        res = dash._auto_cycle_job("cycle-job", PROFILE,
                                   {"count_min": 1, "count_max": 4})
        out[name] = _norm_obj({"result": res, "cmds": cmds[:], "txt": written[:],
                               "store": _store_view(post_store.PostStore(PROFILE))},
                              tmp)
    monkeypatch.setattr(pm, "get_profile_config", real_cfg)

    # The scheduler's executor callback, which runs the same job body.
    seed([_rec(1, approved=True)])
    cmds.clear()
    jobs.clear()
    path = tmp / "sched_cb.txt"
    path.write_text("x", encoding="utf-8")
    jid = dash._sched_submit_post_job(PROFILE, str(path), 2)
    out["sched_callback"] = _norm_obj({"job_id_prefix": jid.rsplit("_", 1)[0],
                                       "jobs": jobs[:], "cmds": cmds[:]}, tmp)
    return out


# ─── prompts ─────────────────────────────────────────────────────────────────

POSTS = [
    {"author_name": "Golden Author One", "url": A.format("1010000000000000001"),
     "text": "We cut our eval suite runtime in half by caching tokenizer output. "
             "The surprising part was how much variance it removed.",
     "likes": 41, "comments": 7},
    {"author_name": "Golden Author Two", "url": A.format("1010000000000000010"),
     "text": "Hot take: most teams do not need a vector database. A sorted list "
             "and a little patience go a long way. " * 3,
     "likes": 3, "comments": 0},
]
EVALS = [
    {"post_category": "technical_discussion", "engagement_approach": "add_perspective"},
    {"post_category": "thought_piece", "engagement_approach": "respectful_disagree"},
    {"post_category": "question", "engagement_approach": "share_similar"},
]
CONFIGS = {
    "empty": {},
    "full": {"comment_generator": {
        "persona": "Applied ML lead", "tone": "plain and warm",
        "voice": "first person, concrete",
        "topics_of_expertise": ["evaluation", "data pipelines"],
        "things_to_avoid": ["buzzwords"],
        "style_mix": {"add_insight": 0.5, "ask_question": 0.5},
        "comment_length_range": [12, 40]}},
}


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeOpenAI:
    """Records every create() call; answers evaluate, relevance and generate."""

    def __init__(self, generated):
        self.calls = []
        self.generated = list(generated)
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls.append({k: (round(v, 6) if isinstance(v, float) else v)
                           for k, v in kw.items()})
        if kw.get("response_format"):
            return _Resp(json.dumps({"verdict": "engage",
                                     "post_category": "technical_discussion",
                                     "engagement_approach": "add_perspective",
                                     "reason": "golden"}))
        if kw.get("max_tokens") == 3:
            return _Resp("YES")
        return _Resp(self.generated.pop(0) if self.generated else "ok")


def capture_prompts(monkeypatch, tmp):
    monkeypatch.chdir(tmp)                       # api_usage.jsonl lands in tmp
    out = {}
    for platform in (pp.LINKEDIN, pp.X):
        for cname, cfg in CONFIGS.items():
            monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp))
            monkeypatch.setattr(pm, "get_progress_file",
                                lambda profile_name=None: str(tmp / "p.json"))
            monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None, _c=cfg: _c)
            g = gen.AuthenticCommentGenerator("dummy.json", model="gpt-4o-mini",
                                              profile_name=PROFILE, platform=platform)
            fake = FakeOpenAI([
                "Caching the tokenizer is underrated, the variance drop matches what we saw.",
                "Sorted lists scale further than people expect, until the updates start.",
            ] * 6)
            g.client = fake
            random.seed(4242)
            rows = {"system_voice": g.register.system_voice,
                    "persona_block": g._build_persona_block()}
            for i, post in enumerate(POSTS):
                rows["evaluate_%d" % i] = g.evaluate_post_quality(post)
                for j, ev in enumerate(EVALS):
                    style, approach = g.get_matched_comment_style(ev)
                    rows["prompt_%d_%d" % (i, j)] = g.create_authentic_comment_prompt(
                        post, style, approach)
                rows["generated_%d" % i] = {
                    k: v for k, v in (g.generate_comment(post, EVALS[i]) or {}).items()
                    if k != "generated_at"}
                rows["relevance_%d" % i] = g._relevance_prompt(post, "A short reply.")
            rows["calls"] = fake.calls
            out["%s::%s" % (platform, cname)] = rows
    return out


# ─── selectors and identity ──────────────────────────────────────────────────

def capture_selectors():
    from linkedin_automation import selector_health as sh
    return json.loads(json.dumps(sh.SELECTOR_REGISTRY, default=str))


IDENTITY_URLS = [
    A.format("1000000000000000001"),
    "https://www.linkedin.com/feed/update/urn:li:ugcPost:1001000000000000001/",
    "https://www.linkedin.com/feed/update/urn:li:share:1001000000000000010/",
    "https://www.linkedin.com/feed/update/urn:li:groupPost:101-1001000000000000011/",
    "https://www.linkedin.com/posts/golden-member_share-5-tips-activity-1001000000000000100-AbCd",
    "https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A1001000000000000101/",
    "https://www.linkedin.com/feed/",
    "not a url",
]


def capture_identity():
    return {u: cpm.LinkedInCommentPoster.post_identity(u) for u in IDENTITY_URLS}


def capture(monkeypatch, tmp_path, api_client):
    """Everything, as one JSON-ready dict."""
    sections = {}
    # The dashboard first: it runs on api_client's redirects, which the later
    # sections replace with their own.
    sections["dashboard"] = capture_dashboard(monkeypatch, tmp_path, api_client)
    for name, fn in (("poster", lambda t: capture_poster(monkeypatch, t)),
                     ("scheduler", lambda t: capture_scheduler(monkeypatch, t)),
                     ("prompts", lambda t: capture_prompts(monkeypatch, t))):
        t = tmp_path / name
        t.mkdir()
        sections[name] = fn(t)
    sections["selectors"] = capture_selectors()
    sections["identity"] = capture_identity()
    return sections


def render(sections):
    """The golden's text form: one JSON value per line, sorted keys."""
    return json.dumps(sections, indent=1, sort_keys=True, ensure_ascii=True) + "\n"
