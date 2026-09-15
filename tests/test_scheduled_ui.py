"""Phase 1 of the scheduled-posting UI: config keys and the read-only queue.

The gate for this phase is that the section shows the truth and can do nothing.
So the load-bearing test is not "it renders" - it is that the endpoint writes
NOTHING, and that the two failure states which look alike in a table never read
alike to a human.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402


PERMALINK = "https://www.linkedin.com/feed/update/urn:li:share:0000000000000000000"


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Point the pipeline's per-profile state at a temp file with known rows."""
    path = tmp_path / "scheduled_posts_state.json"
    rows = {
        "k_done": {"status": cp.COMMENTED, "post_id": "p1", "permalink": PERMALINK,
                   "text": "A finished post.", "due_at": "2026-09-08T16:00:00.000Z"},
        "k_live_nocomment": {"status": cp.COMMENT_FAILED, "post_id": "p2",
                             "permalink": PERMALINK, "text": "Live but uncommented.",
                             "errors": ["the submit button did not accept it"]},
        "k_nothing": {"status": cp.FAILED, "text": "Never published.",
                      "errors": ["bad date 'x' - expected YYYY-MM-DD"]},
        "k_waiting": {"status": cp.SCHEDULED, "post_id": "p4",
                      "text": "Waiting to publish.",
                      "due_at": "2026-09-09T16:00:00.000Z"},
    }
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    monkeypatch.setattr(cp.pm, "get_data_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(dashboard.pm, "get_profile_config",
                        lambda name=None: {"scheduled_posting": {
                            "buffer_channel_id": "chan123",
                            "identity_slug": "example-person"}})
    return path


def test_the_queue_groups_rows_by_status_with_counts(client, state_file):
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["total"] == 4
    assert d["counts"][cp.COMMENTED] == 1
    assert d["counts"][cp.COMMENT_FAILED] == 1
    assert d["counts"][cp.FAILED] == 1
    assert d["counts"][cp.SCHEDULED] == 1
    assert len(d["posts"][cp.COMMENTED]) == 1


def test_work_still_to_do_is_ordered_before_finished_work(client, state_file):
    """A table that buries a live-but-uncommented post under fifty done ones
    is a table nobody reads."""
    order = client.get("/api/scheduled/p/queue").get_json()["order"]
    assert order.index(cp.COMMENT_FAILED) < order.index(cp.COMMENTED)
    assert order.index(cp.FAILED) < order.index(cp.COMMENTED)
    assert order[-1] == cp.COMMENTED


def test_the_two_failure_states_never_read_alike(client, state_file):
    """FAILED published nothing. COMMENT_FAILED left a real post live. Confusing
    them is how someone re-runs a row that already posted."""
    d = client.get("/api/scheduled/p/queue").get_json()
    live = d["posts"][cp.COMMENT_FAILED][0]
    nothing = d["posts"][cp.FAILED][0]

    assert "LIVE" in live["meaning"]
    assert "by hand" in live["action"]
    assert "Do NOT re-run" in live["action"]

    assert "nothing exists" in nothing["meaning"]
    assert "schedule again" in nothing["action"]
    assert live["action"] != nothing["action"]


def test_a_row_with_a_post_is_flagged_so_the_ui_can_refuse_to_re_run(client,
                                                                    state_file):
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["posts"][cp.COMMENT_FAILED][0]["has_post"] is True
    assert d["posts"][cp.COMMENTED][0]["has_post"] is True
    assert d["posts"][cp.FAILED][0]["has_post"] is False


def test_failures_carry_their_reason(client, state_file):
    d = client.get("/api/scheduled/p/queue").get_json()
    assert "bad date" in d["posts"][cp.FAILED][0]["errors"][0]


def test_the_permalink_is_returned_for_published_rows(client, state_file):
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["posts"][cp.COMMENTED][0]["permalink"] == PERMALINK


def test_long_text_is_truncated_for_the_table(client, state_file, tmp_path):
    path = tmp_path / "scheduled_posts_state.json"
    path.write_text(json.dumps({"rows": {"k": {"status": cp.SCHEDULED,
                                               "text": "x" * 400}}}), encoding="utf-8")
    d = client.get("/api/scheduled/p/queue").get_json()
    assert len(d["posts"][cp.SCHEDULED][0]["preview"]) < 200


# --- the identity, surfaced rather than typed -------------------------------

def test_the_configured_identity_comes_back_with_the_queue(client, state_file):
    cfg = client.get("/api/scheduled/p/queue").get_json()["config"]
    assert cfg["identity_slug"] == "example-person"
    assert cfg["identity_configured"] is True
    assert cfg["buffer_channel_id"] == "chan123"


def test_an_unset_identity_is_reported_as_unconfigured(client, state_file,
                                                       monkeypatch):
    """The sweeper cannot be enabled without one, so the UI must be able to
    disable the control and say why instead of letting it be switched on."""
    monkeypatch.setattr(dashboard.pm, "get_profile_config",
                        lambda name=None: {"scheduled_posting": {
                            "buffer_channel_id": "", "identity_slug": "   "}})
    cfg = client.get("/api/scheduled/p/queue").get_json()["config"]
    assert cfg["identity_configured"] is False
    assert cfg["identity_slug"] == ""


def test_a_profile_with_no_scheduled_posting_config_still_loads(client, state_file,
                                                                monkeypatch):
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["config"]["identity_configured"] is False
    assert d["total"] == 4


# --- READ-ONLY: the gate for this phase -------------------------------------

def test_the_queue_endpoint_writes_nothing(client, state_file):
    """The whole point of Phase 1. Compare the file byte for byte, and its
    mtime, across repeated reads."""
    before = state_file.read_bytes()
    before_mtime = os.path.getmtime(state_file)
    for _ in range(3):
        assert client.get("/api/scheduled/p/queue").status_code == 200
    assert state_file.read_bytes() == before
    assert os.path.getmtime(state_file) == before_mtime


def test_an_empty_state_renders_rather_than_erroring(client, tmp_path, monkeypatch):
    monkeypatch.setattr(cp.pm, "get_data_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["total"] == 0
    assert all(v == 0 for v in d["counts"].values())


# --- the section is wired without disturbing the existing dashboard ---------

def test_the_section_is_present_in_the_rendered_page(client):
    html = client.get("/").get_data(as_text=True)
    for probe in ('data-mode="scheduled"', 'id="mode-scheduled"',
                  "loadScheduledQueue", "schpIdentityLine", "schpQueue"):
        assert probe in html, probe


def test_the_existing_sections_are_untouched(client):
    html = client.get("/").get_data(as_text=True)
    for mode in ("comments", "connector", "poster", "scheduler"):
        assert 'data-mode="%s"' % mode in html
        assert 'id="mode-%s"' % mode in html


def test_the_new_job_category_does_not_collide_with_the_existing_one():
    """`scheduled_post` is already taken by the BROWSER poster's publish job.
    Reusing it would conflate two unrelated things in the jobs bar."""
    import inspect
    src = inspect.getsource(dashboard)
    assert 'category="scheduled_post"' in src        # the existing one
    assert src.count('category="scheduled_post"') == 1


def test_the_config_template_carries_the_new_keys():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "default_profile_config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert "scheduled_posting" in cfg
    assert "buffer_channel_id" in cfg["scheduled_posting"]
    assert "identity_slug" in cfg["scheduled_posting"]


# ─── Phase 2: the inputs ─────────────────────────────────────────────────────
#
# Two inputs, one queue. The load-bearing assertions are that validation runs
# over the whole batch before anything is stored, that a bad row never enters,
# and that NOTHING here schedules or posts.

CSV_HEADER = ("date,time_window,post_text,topic,tags,image_path,"
              "first_comment_link\n")
GOOD = "2036-09-08,morning,A good post.,,#a #b,,https://example.com/x\n"
BAD_DATE = "not-a-date,morning,Bad date row.,,,,\n"
BAD_WINDOW = "2036-09-08,midnight,Bad window row.,,,,\n"
EMPTY_ROW = "2036-09-08,morning,,,,,\n"


@pytest.fixture
def empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cp.pm, "get_data_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    return tmp_path / "scheduled_posts_state.json"


@pytest.fixture
def no_buffer(monkeypatch):
    """Any Buffer or browser call during Phase 2 is a bug. Make them explode."""
    def boom(*a, **k):
        raise AssertionError("Phase 2 must not contact Buffer or a browser")
    monkeypatch.setattr(cp.bc, "create_post", boom)
    monkeypatch.setattr(cp.bc, "get_post", boom)
    monkeypatch.setattr(cp.image_host, "upload_image", boom)
    return True


def _upload(client, profile, csv_text):
    import io as _io
    return client.post("/api/scheduled/%s/rows" % profile,
                       data={"file": (_io.BytesIO(csv_text.encode("utf-8")),
                                      "calendar.csv")},
                       content_type="multipart/form-data")


def test_a_csv_upload_queues_valid_rows_as_pending(client, empty_state, no_buffer):
    d = _upload(client, "p", CSV_HEADER + GOOD).get_json()
    assert d["counts"]["accepted"] == 1
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.PENDING] == 1
    assert q["posts"][cp.PENDING][0]["preview"] == "A good post."


def test_bad_rows_are_rejected_with_which_row_and_what_is_wrong(client, empty_state,
                                                                no_buffer):
    d = _upload(client, "p",
                CSV_HEADER + GOOD + BAD_DATE + BAD_WINDOW + EMPTY_ROW).get_json()
    assert d["counts"] == {"read": 4, "accepted": 1, "rejected": 3, "skipped": 0}
    by_index = {r["index"]: r for r in d["rejected"]}
    assert "expected YYYY-MM-DD" in by_index[2]["errors"][0]
    assert "unknown time_window" in by_index[3]["errors"][0]
    assert "nothing to post" in by_index[4]["errors"][0]
    assert by_index[2]["preview"] == "Bad date row."


def test_only_the_valid_rows_enter_the_queue(client, empty_state, no_buffer):
    _upload(client, "p", CSV_HEADER + GOOD + BAD_DATE + BAD_WINDOW)
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["total"] == 1
    assert q["counts"][cp.PENDING] == 1


def test_validation_covers_the_whole_batch_before_anything_is_stored(tmp_path):
    """A bad row in the middle must not depend on ordering: every row is
    checked before the first write."""
    seen = []
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    rows = cp.parse_rows(CSV_HEADER + GOOD + BAD_DATE
                         + GOOD.replace("A good", "A second"))

    real_update = state.update

    def spy(key, **fields):
        seen.append(key)
        return real_update(key, **fields)
    state.update = spy

    real_validate = cp.validate_row
    order = []

    def watch(row, **kw):
        order.append(("validate", len(seen)))
        return real_validate(row, **kw)
    cp.validate_row = watch
    try:
        cp.queue_rows(rows, state)  # returns a 4-tuple; unused here
    finally:
        cp.validate_row = real_validate
    assert all(writes == 0 for _, writes in order), order


def test_the_form_and_a_csv_produce_the_same_queue_shape(client, empty_state,
                                                         no_buffer, tmp_path):
    """A post added by hand and one from a spreadsheet must be
    indistinguishable once queued."""
    client.post("/api/scheduled/p/rows", json={
        "date": "2036-09-08", "time_window": "morning",
        "post_text": "A good post.", "topic": "", "tags": "#a #b",
        "image_path": "", "first_comment_link": "https://example.com/x"})
    form_row = client.get("/api/scheduled/p/queue").get_json()["posts"][cp.PENDING][0]

    import json as _json
    (tmp_path / "scheduled_posts_state.json").write_text(
        _json.dumps({"rows": {}}), encoding="utf-8")
    _upload(client, "p", CSV_HEADER + GOOD)
    csv_row = client.get("/api/scheduled/p/queue").get_json()["posts"][cp.PENDING][0]

    assert form_row["key"] == csv_row["key"]
    assert form_row["status"] == csv_row["status"] == cp.PENDING
    assert form_row["preview"] == csv_row["preview"]


def test_the_form_rejects_a_bad_row_inline(client, empty_state, no_buffer):
    d = client.post("/api/scheduled/p/rows", json={
        "date": "nope", "time_window": "morning",
        "post_text": "Form row."}).get_json()
    assert d["counts"]["accepted"] == 0
    assert "expected YYYY-MM-DD" in d["rejected"][0]["errors"][0]
    assert client.get("/api/scheduled/p/queue").get_json()["total"] == 0


def test_a_pending_row_keeps_its_source_row_for_the_schedule_pass(client,
                                                                  empty_state,
                                                                  no_buffer):
    """A PENDING entry that only remembered its hash could never be scheduled."""
    _upload(client, "p", CSV_HEADER + GOOD)
    state = cp.PipelineState(path=str(empty_state))
    rows = cp.pending_rows(state)
    assert len(rows) == 1
    assert rows[0]["post_text"] == "A good post."
    assert rows[0]["time_window"] == "morning"


def test_re_uploading_the_same_csv_does_not_duplicate(client, empty_state,
                                                      no_buffer):
    _upload(client, "p", CSV_HEADER + GOOD)
    d = _upload(client, "p", CSV_HEADER + GOOD).get_json()
    assert d["counts"]["accepted"] == 0
    assert d["counts"]["skipped"] == 1
    assert client.get("/api/scheduled/p/queue").get_json()["total"] == 1


def test_a_row_that_already_has_a_post_is_never_reset_to_pending(client,
                                                                 empty_state,
                                                                 no_buffer):
    """THE rule, at the queueing layer: re-adding a scheduled row must not put
    it back in line to be posted a second time."""
    _upload(client, "p", CSV_HEADER + GOOD)
    state = cp.PipelineState(path=str(empty_state))
    key = list(state.rows)[0]
    state.update(key, status=cp.COMMENTED, post_id="p1")

    d = _upload(client, "p", CSV_HEADER + GOOD).get_json()
    assert d["counts"]["accepted"] == 0
    assert "already has a post" in d["skipped"][0]["reason"]
    after = cp.PipelineState(path=str(empty_state))
    assert after.get(key)["status"] == cp.COMMENTED
    assert after.get(key)["post_id"] == "p1"


def test_a_csv_missing_columns_is_refused_whole(client, empty_state, no_buffer):
    r = _upload(client, "p", "post_text,tags\nhi,#a\n")
    assert r.status_code == 400
    assert "missing required column" in r.get_json()["error"]
    assert client.get("/api/scheduled/p/queue").get_json()["total"] == 0


def test_a_non_utf8_upload_is_refused_clearly(client, empty_state, no_buffer):
    import io as _io
    r = client.post("/api/scheduled/p/rows",
                    data={"file": (_io.BytesIO(b"\xff\xfe\x00bad"), "c.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert "UTF-8" in r.get_json()["error"]


def test_only_known_columns_are_taken_from_the_form(client, empty_state, no_buffer):
    """A stray field must not change the identity hash and create a duplicate."""
    base = {"date": "2036-09-08", "time_window": "morning",
            "post_text": "A good post.", "tags": "#a #b",
            "first_comment_link": "https://example.com/x"}
    client.post("/api/scheduled/p/rows", json=base)
    d = client.post("/api/scheduled/p/rows",
                    json=dict(base, sneaky="value")).get_json()
    assert d["counts"]["skipped"] == 1, "a stray field changed the row identity"


def test_phase_2_still_schedules_nothing(client, empty_state, no_buffer):
    """The gate. The no_buffer fixture makes any Buffer/browser call explode;
    reaching the end of this test means none was made."""
    _upload(client, "p", CSV_HEADER + GOOD + BAD_DATE)
    client.post("/api/scheduled/p/rows", json={
        "date": "2036-09-09", "time_window": "evening", "post_text": "Another."})
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.PENDING] == 2
    for st in (cp.SCHEDULED, cp.PUBLISHED, cp.COMMENTED, cp.COMMENT_FAILED):
        assert q["counts"][st] == 0
    for row in q["posts"][cp.PENDING]:
        assert row["has_post"] is False
        assert row["post_id"] is None


def test_pending_heads_the_work_to_do_band_but_problems_stay_above_it(client,
                                                                      empty_state):
    order = client.get("/api/scheduled/p/queue").get_json()["order"]
    assert order.index(cp.COMMENT_FAILED) < order.index(cp.PENDING)
    assert order.index(cp.FAILED) < order.index(cp.PENDING)
    assert order.index(cp.PENDING) < order.index(cp.SCHEDULED)
    assert order.index(cp.PENDING) < order.index(cp.COMMENTED)


# ─── Phase 3: the schedule button ────────────────────────────────────────────
#
# The first action that creates something real. What these pin is that it acts
# on PENDING and nothing else, that a row with a post can never be reached, and
# that a Buffer rejection fails the row cleanly rather than half-scheduling it.

CHANNEL = {"id": "chan1", "name": "example-person-one", "displayName": "Example One",
           "service": "linkedin",
           "externalLink": "https://www.linkedin.com/in/example-person-one",
           "isDisconnected": False}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(dashboard.pm, "get_profile_config",
                        lambda name=None: {"scheduled_posting": {
                            "buffer_channel_id": "chan1",
                            "identity_slug": "example-person-one"}})
    monkeypatch.setattr(dashboard.buffer_client, "get_channel",
                        lambda cid, **kw: dict(CHANNEL, id=cid))
    # Slots are read before scheduling; without this the preflight reports a
    # problem and every configured test would be testing the error path.
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: {"limit": 10, "used": 0,
                                           "used_this_channel": 0, "free": 10})


@pytest.fixture
def fake_buffer(monkeypatch):
    """Records every createPost. The count is the assertion that matters."""
    calls = []

    def create_post(channel_id, text, image_url, when, key=None, session=None):
        calls.append({"channel_id": channel_id, "text": text,
                      "image": image_url, "due": when})
        return {"id": "post%d" % len(calls), "status": "scheduled", "dueAt": when}

    monkeypatch.setattr(cp.bc, "create_post", create_post)
    monkeypatch.setattr(cp.image_host, "upload_image",
                        lambda path: "https://pub-abc.r2.dev/posts/x.png")
    return calls


def _queue(client, profile, text="A queued post.", date="2036-11-02"):
    return client.post("/api/scheduled/%s/rows" % profile, json={
        "date": date, "time_window": "morning", "post_text": text,
        "topic": "", "tags": "#a", "image_path": "",
        "first_comment_link": "https://example.com/x"})


def _run(client, profile="p"):
    """Fire the schedule action and wait for its background job."""
    d = client.post("/api/scheduled/%s/schedule" % profile).get_json()
    if not d.get("job_id"):
        return d
    for _ in range(200):
        job = dashboard.jobs.get(d["job_id"], {})
        if job.get("status") in ("completed", "failed"):
            return {"job": job, "job_id": d["job_id"]}
        time.sleep(0.02)
    raise AssertionError("the schedule job never finished")


# --- the confirmation dialog's data -----------------------------------------

def test_preflight_names_the_account_and_the_row_count(client, empty_state,
                                                       configured, fake_buffer):
    _queue(client, "p")
    d = client.get("/api/scheduled/p/schedule/preflight").get_json()
    assert d["ready"] is True
    assert d["pending_count"] == 1
    assert d["channel"]["displayName"] == "Example One"
    assert d["channel"]["service"] == "linkedin"
    assert d["previews"] == ["A queued post."]


def test_preflight_resolves_the_channel_from_buffer_not_local_config(client,
                                                                     empty_state,
                                                                     configured,
                                                                     fake_buffer):
    """A name kept locally could go stale and still read like the dev account.
    The dialog must name what the id actually IS."""
    _queue(client, "p")
    d = client.get("/api/scheduled/p/schedule/preflight").get_json()
    assert d["channel"]["id"] == "chan1"
    assert d["channel"]["externalLink"].endswith("example-person-one")


def test_preflight_refuses_when_no_channel_is_configured(client, empty_state,
                                                         monkeypatch):
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    _queue(client, "p")
    d = client.get("/api/scheduled/p/schedule/preflight").get_json()
    assert d["ready"] is False
    assert any("No Buffer channel configured" in p for p in d["problems"])


def test_preflight_refuses_when_nothing_is_pending(client, empty_state,
                                                   configured, fake_buffer):
    d = client.get("/api/scheduled/p/schedule/preflight").get_json()
    assert d["ready"] is False
    assert any("Nothing is pending" in p for p in d["problems"])


def test_an_unresolvable_channel_is_reported_not_guessed(client, empty_state,
                                                         monkeypatch, fake_buffer):
    """Never invent a reassuring name for an id Buffer would not confirm."""
    monkeypatch.setattr(dashboard.pm, "get_profile_config",
                        lambda name=None: {"scheduled_posting":
                                           {"buffer_channel_id": "ghost"}})

    def boom(cid, **kw):
        raise RuntimeError("no such channel")
    monkeypatch.setattr(dashboard.buffer_client, "get_channel", boom)
    _queue(client, "p")
    d = client.get("/api/scheduled/p/schedule/preflight").get_json()
    assert d["ready"] is False
    assert d["channel"] is None
    assert any("Could not resolve" in p for p in d["problems"])


# --- the action itself -------------------------------------------------------

def test_scheduling_moves_pending_rows_to_scheduled(client, empty_state,
                                                    configured, fake_buffer):
    _queue(client, "p")
    out = _run(client)
    assert out["job"]["status"] == "completed"
    assert out["job"]["result"]["scheduled"] == 1
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.SCHEDULED] == 1
    assert q["counts"][cp.PENDING] == 0
    row = q["posts"][cp.SCHEDULED][0]
    assert row["post_id"] == "post1"
    assert row["due_at"]
    assert row["has_post"] is True


def test_it_uses_the_configured_channel(client, empty_state, configured,
                                        fake_buffer):
    _queue(client, "p")
    _run(client)
    assert fake_buffer[0]["channel_id"] == "chan1"


def test_the_job_is_an_api_task_so_it_never_takes_the_browser_lock(client,
                                                                   empty_state,
                                                                   configured,
                                                                   fake_buffer):
    """R2 and Buffer only. A browser task type would make scheduling queue
    behind, or block, an engagement run for no reason."""
    _queue(client, "p")
    out = _run(client)
    job = dashboard.jobs[out["job_id"]]
    assert job["task_type"] == "api"
    assert job["category"] == "buffer_schedule"


def test_the_category_does_not_collide_with_the_browser_publish_job():
    import inspect
    src = inspect.getsource(dashboard)
    assert 'category="buffer_schedule"' in src
    assert src.count('category="scheduled_post"') == 1


# --- idempotency: the rule, through the UI -----------------------------------

def test_re_clicking_schedule_creates_nothing(client, empty_state, configured,
                                              fake_buffer):
    _queue(client, "p")
    _run(client)
    assert len(fake_buffer) == 1
    again = _run(client)
    assert again.get("job_id") is None
    assert "Nothing pending" in again.get("note", "")
    assert len(fake_buffer) == 1, "a second post was created for the same row"


def test_a_row_that_already_has_a_post_is_never_in_the_pending_set(client,
                                                                   empty_state,
                                                                   configured,
                                                                   fake_buffer):
    _queue(client, "p")
    _run(client)
    state = cp.PipelineState(path=str(empty_state))
    assert cp.pending_rows(state) == []


def test_scheduling_only_touches_pending_rows(client, empty_state, configured,
                                              fake_buffer):
    """A queue holding finished and failed rows must not re-post them."""
    _queue(client, "p", text="Fresh row.")
    state = cp.PipelineState(path=str(empty_state))
    state.update("done_row", status=cp.COMMENTED, post_id="old1",
                 row={"date": "2036-11-02", "time_window": "morning",
                      "post_text": "Already published."})
    state.update("failed_row", status=cp.FAILED, text="Broken row.",
                 row={"date": "nope", "time_window": "morning",
                      "post_text": "Broken row."})
    _run(client)
    assert len(fake_buffer) == 1
    assert fake_buffer[0]["text"].startswith("Fresh row.")


# --- a Buffer rejection fails the row, cleanly -------------------------------

def test_a_buffer_rejection_marks_the_row_failed_not_half_done(client,
                                                               empty_state,
                                                               configured,
                                                               monkeypatch):
    def reject(*a, **k):
        raise cp.bc.BufferRejected("Image could not be read from its URL.")
    monkeypatch.setattr(cp.bc, "create_post", reject)
    monkeypatch.setattr(cp.image_host, "upload_image",
                        lambda path: "https://pub-abc.r2.dev/x.png")
    _queue(client, "p")
    out = _run(client)
    assert out["job"]["result"]["failed"] == 1
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.FAILED] == 1
    row = q["posts"][cp.FAILED][0]
    assert row["has_post"] is False
    assert row["post_id"] is None
    assert "Image could not be read" in row["errors"][0]
    assert "nothing exists" in row["meaning"]


def test_a_failed_row_can_be_retried_because_nothing_was_published(client,
                                                                   empty_state,
                                                                   configured,
                                                                   monkeypatch):
    calls = []

    def flaky(channel_id, text, image_url, when, key=None, session=None):
        calls.append(text)
        if len(calls) == 1:
            raise cp.bc.BufferRejected("transient")
        return {"id": "post1", "status": "scheduled", "dueAt": when}
    monkeypatch.setattr(cp.bc, "create_post", flaky)
    _queue(client, "p")
    _run(client)
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.FAILED] == 1

    # Re-queueing the same row is refused, so a retry means fixing the row -
    # the failed row is not silently re-attempted behind the user's back.
    d = _queue(client, "p").get_json()
    assert d["counts"]["skipped"] == 1


def test_the_endpoint_refuses_without_a_configured_channel(client, empty_state,
                                                           monkeypatch,
                                                           fake_buffer):
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    _queue(client, "p")
    r = client.post("/api/scheduled/p/schedule")
    assert r.status_code == 400
    assert "No Buffer channel configured" in r.get_json()["error"]
    assert fake_buffer == []


def test_still_no_commenting_route_exists():
    """Nothing under /api/scheduled may comment on a live post.

    This enumerates rather than pattern-matches so that ANY new write route has
    to be added here deliberately. `reconcile` is listed because it only asks
    Buffer what it already did and writes the answer to our own state file - it
    cannot create a post and cannot comment. The comment sweep stays on its own
    path.
    """
    writes = sorted(str(r) for r in dashboard.app.url_map.iter_rules()
                    if str(r).startswith("/api/scheduled")
                    and {"POST", "PUT", "DELETE", "PATCH"} & r.methods)
    assert writes == ["/api/scheduled/<profile_name>/reconcile",
                      "/api/scheduled/<profile_name>/rows",
                      "/api/scheduled/<profile_name>/rows/<key>",
                      "/api/scheduled/<profile_name>/schedule"], writes


# ─── Rendering: the failure every API test missed ────────────────────────────
#
# Phases 1-3 all passed while the section rendered COMPLETELY BLANK in a real
# browser. The markup was present, the endpoints were correct, the JS parsed -
# and every card was display:none, because they wore the `.panel` class, which
# the comment pipeline's step switcher owns and toggles document-wide.
#
# "The string is in the HTML" was never evidence that anything was visible.
# These are the cheap structural checks that would have caught it.

def _section(client):
    from bs4 import BeautifulSoup
    html = client.get("/").get_data(as_text=True)
    return BeautifulSoup(html, "html.parser"), html


def test_no_card_in_the_section_wears_the_pipeline_panel_class(client):
    """`.panel` is display:none by default AND is force-toggled on every match
    by showStep(). Any card of ours wearing it is invisible, and would be
    switched off again the moment a pipeline step is shown."""
    soup, _ = _section(client)
    section = soup.find(id="mode-scheduled")
    offenders = [str(e)[:80] for e in section.select(".panel")]
    assert offenders == [], offenders


def test_the_section_uses_its_own_always_visible_card_class(client):
    soup, html = _section(client)
    section = soup.find(id="mode-scheduled")
    assert len(section.select(".schp-card")) >= 3
    assert ".schp-card { display: block;" in html


def test_no_element_in_the_section_uses_a_panel_prefixed_id(client):
    """showStep() activates whichever `.panel` has id 'panel-<step>'. An id in
    that namespace invites the same collision back."""
    soup, _ = _section(client)
    section = soup.find(id="mode-scheduled")
    bad = [e.get("id") for e in section.select("[id]")
           if str(e.get("id")).startswith("panel-")]
    assert bad == [], bad


def test_the_section_panel_is_a_sibling_of_the_other_modes(client):
    """Nested inside another mode-panel it would inherit that panel's
    display:none and never show, whatever its own class said."""
    soup, _ = _section(client)
    section = soup.find(id="mode-scheduled")
    assert "mode-panel" in (section.get("class") or [])
    assert section.parent.get("id") == "app"


def test_the_two_scheduling_tabs_are_not_confusable(client):
    """Two tabs both reading 'Scheduler' is how the wrong one gets clicked."""
    soup, _ = _section(client)
    labels = {t.get("data-mode"): t.get_text(strip=True)
              for t in soup.select(".mode-tab")}
    assert labels["scheduler"] == "Engagement Scheduler"
    assert labels["scheduled"] == "Scheduled Posting"
    assert labels["scheduler"] != labels["scheduled"]
    assert len(set(labels.values())) == len(labels), labels


# --- the chips must read as words, empty or not -----------------------------

def test_every_status_has_a_label_even_with_no_rows(client, empty_state):
    """An empty bucket has no first row to borrow a label from, which is how the
    chips came to read 'comment_failed 0'."""
    d = client.get("/api/scheduled/p/queue").get_json()
    for st in d["order"]:
        assert d["labels"][st], st
        assert d["labels"][st] != st, "raw status key leaked into the label"
    assert d["labels"][cp.COMMENT_FAILED] == "Comment failed"
    assert d["labels"][cp.COMMENTED] == "Done"


def test_meanings_are_available_per_status_not_per_row(client, empty_state):
    d = client.get("/api/scheduled/p/queue").get_json()
    assert "LIVE" in d["meanings"][cp.COMMENT_FAILED]
    assert "nothing exists" in d["meanings"][cp.FAILED]


# --- a partial config update must never wipe the rest ------------------------
#
# save_profile_config REPLACES the file, and the update route used to hand it
# the request body directly. The dashboard editor posts the whole config, which
# hid it; posting one section deleted the profile's persona, keywords, behaviour
# and scheduler settings, unrecoverably - data/ is gitignored.

def test_a_partial_config_post_leaves_the_other_sections_alone(client, tmp_path,
                                                               monkeypatch):
    from linkedin_automation import profile_manager as pm

    path = tmp_path / "profile_config.json"
    monkeypatch.setattr(pm, "get_config_path", lambda profile_name=None: str(path))
    path.write_text(json.dumps({
        "display_name": "Someone",
        "comment_generator": {"persona": "a specific persona",
                              "tone": "a specific tone"},
        "scheduled_posting": {"identity_slug": "", "buffer_channel_id": ""},
    }), encoding="utf-8")

    r = client.post("/api/profiles/p/config", json={
        "scheduled_posting": {"identity_slug": "example-person-one"}})
    assert r.status_code == 200

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["display_name"] == "Someone", "a partial post wiped the profile"
    assert stored["comment_generator"]["persona"] == "a specific persona"
    assert stored["comment_generator"]["tone"] == "a specific tone"
    assert stored["scheduled_posting"]["identity_slug"] == "example-person-one"
    # merged, not replaced, within the section too
    assert "buffer_channel_id" in stored["scheduled_posting"]


def test_read_stored_config_does_not_merge_the_default_template(tmp_path,
                                                                monkeypatch):
    """Writing a default-merged config back would bake the whole template into
    every profile file, so a later change to a default would never reach them."""
    from linkedin_automation import profile_manager as pm

    path = tmp_path / "profile_config.json"
    monkeypatch.setattr(pm, "get_config_path", lambda profile_name=None: str(path))
    path.write_text(json.dumps({"display_name": "Someone"}), encoding="utf-8")
    stored = pm.read_stored_config("p")
    assert stored == {"display_name": "Someone"}
    assert "comment_generator" not in stored


def test_an_unreadable_stored_config_merges_onto_empty(tmp_path, monkeypatch):
    from linkedin_automation import profile_manager as pm

    path = tmp_path / "profile_config.json"
    monkeypatch.setattr(pm, "get_config_path", lambda profile_name=None: str(path))
    path.write_text("{ not json", encoding="utf-8")
    assert pm.read_stored_config("p") == {}


# ─── Phase A: slot-aware scheduling, HELD, expand and delete ────────────────
#
# 2026-09-09: eight good rows were marked FAILED with "fix the row and schedule
# again" because Buffer's free plan was full. Nothing was wrong with any of
# them. A capacity limit and a broken row must never read alike.

def _slots(free, limit=10):
    return {"limit": limit, "used": limit - free, "used_this_channel": limit - free,
            "free": free}


@pytest.fixture
def slots_free(monkeypatch):
    """Buffer reports plenty of room unless a test says otherwise."""
    holder = {"value": _slots(10)}
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda cid, **kw: holder["value"])
    return holder


def test_over_limit_rows_are_HELD_not_FAILED(client, empty_state, configured,
                                             fake_buffer, slots_free):
    slots_free["value"] = _slots(2)
    for i in range(5):
        _queue(client, "p", text="Row %d." % i, date="2036-11-0%d" % (i + 1))
    out = _run(client)
    r = out["job"]["result"]
    assert r["scheduled"] == 2
    assert r["held"] == 3
    assert r["failed"] == 0
    assert len(fake_buffer) == 2, "it sent more than Buffer had room for"

    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.HELD] == 3
    assert q["counts"][cp.FAILED] == 0


def test_a_held_row_never_reads_like_a_broken_one(client, empty_state, configured,
                                                  fake_buffer, slots_free):
    slots_free["value"] = _slots(0)
    _queue(client, "p")
    _run(client)
    q = client.get("/api/scheduled/p/queue").get_json()
    held = q["posts"][cp.HELD][0]
    assert "no scheduled-post slot free" in held["meaning"]
    assert "nothing to fix" in held["action"]
    assert "fix the row" not in held["action"]
    assert held["errors"] == []
    assert "waiting for a slot" in (held["hold_reason"] or "")


def test_nothing_is_sent_when_no_slot_is_free(client, empty_state, configured,
                                              fake_buffer, slots_free):
    """The bug: sending anyway produced a wall of LimitReachedError."""
    slots_free["value"] = _slots(0)
    _queue(client, "p")
    _run(client)
    assert fake_buffer == []


def test_a_capacity_error_from_buffer_also_maps_to_held(client, empty_state,
                                                        configured, monkeypatch,
                                                        slots_free):
    """Even without a budget, Buffer's own limit error must not condemn a row."""
    def at_capacity(*a, **k):
        raise cp.bc.BufferAtCapacity("Scheduled posts limit reached.")
    monkeypatch.setattr(cp.bc, "create_post", at_capacity)
    _queue(client, "p")
    _run(client)
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.HELD] == 1
    assert q["counts"][cp.FAILED] == 0


def test_held_rows_are_picked_up_again_without_re_queueing(client, empty_state,
                                                           configured,
                                                           fake_buffer,
                                                           slots_free):
    """A held row waits on a slot, not on a person."""
    slots_free["value"] = _slots(0)
    _queue(client, "p")
    _run(client)
    assert fake_buffer == []

    slots_free["value"] = _slots(5)          # a slot frees
    _run(client)
    assert len(fake_buffer) == 1
    q = client.get("/api/scheduled/p/queue").get_json()
    assert q["counts"][cp.SCHEDULED] == 1
    assert q["counts"][cp.HELD] == 0
    assert q["posts"][cp.SCHEDULED][0]["hold_reason"] is None


def test_scarce_slots_go_to_the_earliest_post_first(client, empty_state,
                                                    configured, fake_buffer,
                                                    slots_free):
    slots_free["value"] = _slots(1)
    _queue(client, "p", text="Later.", date="2036-12-01")
    _queue(client, "p", text="Earlier.", date="2036-11-01")
    _run(client)
    assert len(fake_buffer) == 1
    assert fake_buffer[0]["text"].startswith("Earlier.")


def test_the_preflight_says_how_many_will_actually_send(client, empty_state,
                                                        configured, fake_buffer,
                                                        slots_free):
    slots_free["value"] = _slots(2)
    for i in range(5):
        _queue(client, "p", text="Row %d." % i, date="2036-11-0%d" % (i + 1))
    d = client.get("/api/scheduled/p/schedule/preflight").get_json()
    assert d["pending_count"] == 5
    assert d["will_send"] == 2
    assert d["will_hold"] == 3
    assert d["slots"]["limit"] == 10


# --- expand -----------------------------------------------------------------

def test_the_row_carries_its_full_text_for_the_expand_control(client,
                                                              empty_state,
                                                              no_buffer):
    long_text = "First line. " + ("x" * 400)
    client.post("/api/scheduled/p/rows", json={
        "date": "2036-09-08", "time_window": "morning", "post_text": long_text})
    row = client.get("/api/scheduled/p/queue").get_json()["posts"][cp.PENDING][0]
    assert len(row["preview"]) < len(row["full_text"])
    assert row["full_text"].startswith("First line.")
    assert len(row["full_text"]) >= 400


# --- delete -----------------------------------------------------------------

def test_deleting_a_pending_row_removes_it(client, empty_state, no_buffer):
    _queue(client, "p")
    key = client.get("/api/scheduled/p/queue").get_json()["posts"][cp.PENDING][0]["key"]
    r = client.delete("/api/scheduled/p/rows/%s" % key)
    assert r.status_code == 200
    assert r.get_json()["had_post"] is False
    assert client.get("/api/scheduled/p/queue").get_json()["total"] == 0


def test_deleting_a_row_with_a_live_post_needs_explicit_confirmation(client,
                                                                     empty_state,
                                                                     configured,
                                                                     fake_buffer,
                                                                     slots_free):
    """One click must not remove the record of something that still exists."""
    _queue(client, "p")
    _run(client)
    key = client.get("/api/scheduled/p/queue").get_json()["posts"][cp.SCHEDULED][0]["key"]

    r = client.delete("/api/scheduled/p/rows/%s" % key)
    assert r.status_code == 409
    body = r.get_json()
    assert body["has_post"] is True
    assert "deletes only our record" in body["error"]
    assert "stays scheduled or published" in body["error"]
    assert client.get("/api/scheduled/p/queue").get_json()["total"] == 1


def test_a_confirmed_delete_of_a_live_row_says_what_it_did_not_do(client,
                                                                  empty_state,
                                                                  configured,
                                                                  fake_buffer,
                                                                  slots_free):
    _queue(client, "p")
    _run(client)
    key = client.get("/api/scheduled/p/queue").get_json()["posts"][cp.SCHEDULED][0]["key"]
    r = client.delete("/api/scheduled/p/rows/%s?confirm_live=1" % key)
    assert r.status_code == 200
    note = r.get_json()["note"]
    assert "still exists" in note
    assert "did not unschedule or unpublish" in note
    assert client.get("/api/scheduled/p/queue").get_json()["total"] == 0


def test_deleting_an_unknown_row_is_a_404(client, empty_state, no_buffer):
    assert client.delete("/api/scheduled/p/rows/nosuchkey").status_code == 404


def test_delete_leaves_the_other_rows_and_the_file_intact(client, empty_state,
                                                          no_buffer):
    _queue(client, "p", text="Keep me.", date="2036-11-01")
    _queue(client, "p", text="Delete me.", date="2036-11-02")
    rows = client.get("/api/scheduled/p/queue").get_json()["posts"][cp.PENDING]
    doomed = [r for r in rows if r["preview"] == "Delete me."][0]
    client.delete("/api/scheduled/p/rows/%s" % doomed["key"])

    after = client.get("/api/scheduled/p/queue").get_json()
    assert after["total"] == 1
    assert after["posts"][cp.PENDING][0]["preview"] == "Keep me."
    # the state file is still valid JSON with the expected shape
    saved = json.loads(empty_state.read_text(encoding="utf-8"))
    assert list(saved) == ["rows"]
    assert len(saved["rows"]) == 1


def test_held_sits_between_pending_and_scheduled_in_the_ordering(client,
                                                                 empty_state):
    order = client.get("/api/scheduled/p/queue").get_json()["order"]
    assert order.index(cp.PENDING) < order.index(cp.HELD)
    assert order.index(cp.HELD) < order.index(cp.SCHEDULED)
    # and still below the genuine problems
    assert order.index(cp.FAILED) < order.index(cp.HELD)
