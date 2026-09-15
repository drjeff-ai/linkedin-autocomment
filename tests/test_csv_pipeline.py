"""Phase 5: CSV orchestration, offline.

R2, Buffer and the browser are all fakes. What these pin is the sequencing and,
above all, the rule the whole design exists to protect:

    A ROW THAT ALREADY HAS A POST NEVER GETS ANOTHER ONE.

Buffer publishes irreversibly. Every resume path, every retry and every failure
mode below is checked against that one sentence.
"""

import json
import os
import random
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import first_comment as fc  # noqa: E402

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
LINK = "https://example.com/reach"
PERMALINK = "https://www.linkedin.com/feed/update/urn:li:share:0000000000000000000"


def make_row(**over):
    row = {"date": "2036-09-08", "time_window": "morning",
           "post_text": "A post body.", "topic": "", "tags": "#a #b",
           "image_path": "", "first_comment_link": LINK}
    row.update(over)
    return row


@pytest.fixture
def state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "state.json"))


@pytest.fixture
def ledger(tmp_path):
    return fc.FirstCommentLedger(path=str(tmp_path / "ledger.json"))


class FakeBuffer:
    """Buffer, scripted. Counts createPost calls - the number that matters."""

    def __init__(self, status="scheduled", link=None, reject=False):
        self.created = []
        self.status = status
        self.link = link
        self.reject = reject
        self.n = 0

    def create_post(self, channel_id, text, image_url, when, key=None, session=None):
        if self.reject:
            from linkedin_automation import buffer_client as bc
            raise bc.BufferRejected("Image could not be read from its URL.")
        self.n += 1
        pid = "post%d" % self.n
        self.created.append({"id": pid, "text": text, "image": image_url,
                             "due": when})
        return {"id": pid, "status": "scheduled", "dueAt": when}

    def get_post(self, post_id, key=None, session=None):
        return {"id": post_id, "status": self.status,
                "externalLink": self.link, "error": None}


class FakePoster:
    def __init__(self, comment_ok=True):
        self.comment_ok = comment_ok
        self.comments = []
        self.driver = None

    def navigate_to_post(self, url):
        return True

    def post_comment(self, text):
        self.comments.append(text)
        return self.comment_ok

    def like_post(self):
        raise AssertionError("must never like our own post")


@pytest.fixture
def patched(monkeypatch):
    """Wire the module's Buffer calls to a fake we can interrogate."""
    fake = FakeBuffer()
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    monkeypatch.setattr(cp, "verify_identity", lambda p, s: (True, "ok"))
    return fake


# --- validation happens before anything is acted on -------------------------

def test_a_row_with_neither_text_nor_topic_is_invalid():
    problems = cp.validate_row(make_row(post_text="", topic=""), now=NOW)
    assert any("nothing to post" in p for p in problems)


def test_a_bad_date_and_a_bad_window_are_both_caught():
    assert cp.validate_row(make_row(date="08/09/2036"), now=NOW)
    assert cp.validate_row(make_row(time_window="midnight"), now=NOW)


def test_a_past_date_is_invalid():
    future = datetime(2040, 1, 1, tzinfo=timezone.utc)
    assert any("past" in p for p in cp.validate_row(make_row(), now=future))


def test_a_missing_image_file_is_caught_before_upload(tmp_path):
    problems = cp.validate_row(
        make_row(image_path=str(tmp_path / "nope.png")), now=NOW)
    assert any("does not exist" in p for p in problems)


def test_a_non_image_file_is_caught_before_upload(tmp_path):
    f = tmp_path / "notes.png"
    f.write_bytes(b"plain text, not a picture")
    problems = cp.validate_row(make_row(image_path=str(f)), now=NOW)
    assert any("not a PNG" in p for p in problems)


def test_a_good_row_has_no_problems():
    assert cp.validate_row(make_row(), now=NOW) == []


def test_an_invalid_row_is_never_uploaded_or_posted(state, patched):
    """Half-processing is the thing to avoid: uploading an image for a row whose
    date cannot produce a dueAt is work done for a post that cannot exist."""
    uploaded = []
    res = cp.schedule_pass([make_row(date="nonsense")], "chan", state,
                           upload=lambda p: uploaded.append(p), now=NOW)
    assert res[0]["status"] == cp.FAILED
    assert res[0]["stage"] == "validate"
    assert uploaded == []
    assert patched.created == []


# --- row identity is stable -------------------------------------------------

def test_the_same_row_keys_the_same_whatever_its_position():
    a, b = make_row(), make_row()
    assert cp.row_key(a) == cp.row_key(b)


def test_editing_a_row_makes_it_a_new_row():
    assert cp.row_key(make_row()) != cp.row_key(make_row(post_text="different"))


# --- the schedule pass ------------------------------------------------------

def test_a_row_is_scheduled_with_its_tags_and_image(state, patched):
    res = cp.schedule_pass([make_row()], "chan", state,
                           rng=random.Random(1),
                           upload=lambda p: "https://pub/x.png", now=NOW)
    assert res[0]["status"] == cp.SCHEDULED
    created = patched.created[0]
    assert created["text"].endswith("#a #b")
    assert "\n\n" in created["text"]


def test_a_blank_post_text_is_generated_from_the_topic(state, patched):
    res = cp.schedule_pass(
        [make_row(post_text="", topic="automation")], "chan", state,
        generate=lambda topic, profile_name=None: "generated about %s" % topic,
        now=NOW)
    assert res[0]["status"] == cp.SCHEDULED
    assert "generated about automation" in patched.created[0]["text"]


def test_a_buffer_rejection_fails_the_row_cleanly(state, monkeypatch):
    """Nothing was published, so the row can be fixed and retried as-is."""
    fake = FakeBuffer(reject=True)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    res = cp.schedule_pass([make_row()], "chan", state, now=NOW)
    assert res[0]["status"] == cp.FAILED
    assert res[0]["stage"] == "createPost"
    assert state.get(res[0]["key"])["status"] == cp.FAILED


def test_an_upload_failure_fails_before_any_post_exists(state, patched):
    def boom(path):
        raise image_err()

    def image_err():
        from linkedin_automation import image_host
        return image_host.NotPublic("bucket is not public")

    res = cp.schedule_pass([make_row(image_path=__file__)], "chan", state,
                           upload=boom, now=NOW)
    assert res[0]["status"] == cp.FAILED
    assert patched.created == []


# --- THE RULE: never re-publish ---------------------------------------------

def test_rerunning_the_schedule_pass_does_not_create_a_second_post(state, patched):
    rows = [make_row()]
    cp.schedule_pass(rows, "chan", state, now=NOW)
    assert patched.n == 1
    again = cp.schedule_pass(rows, "chan", state, now=NOW)
    assert patched.n == 1, "a second post was created for the same row"
    assert "not re-created" in again[0]["note"]


def test_a_row_whose_comment_failed_is_never_re_posted(state, patched):
    rows = [make_row()]
    cp.schedule_pass(rows, "chan", state, now=NOW)
    key = cp.row_key(rows[0])
    state.update(key, status=cp.COMMENT_FAILED, permalink=PERMALINK)
    cp.schedule_pass(rows, "chan", state, now=NOW)
    assert patched.n == 1


def test_a_completed_row_is_skipped_entirely(state, patched):
    rows = [make_row()]
    cp.schedule_pass(rows, "chan", state, now=NOW)
    key = cp.row_key(rows[0])
    state.update(key, status=cp.COMMENTED)
    cp.schedule_pass(rows, "chan", state, now=NOW)
    assert patched.n == 1


def test_a_corrupt_state_file_is_not_read_as_empty(tmp_path):
    """Reading it as empty would re-publish every post it recorded."""
    p = tmp_path / "state.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError):
        cp.PipelineState(path=str(p))


# --- the comment pass -------------------------------------------------------

def test_a_post_not_yet_published_is_left_for_the_next_sweep(state, monkeypatch,
                                                             ledger):
    fake = FakeBuffer(status="scheduled")
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    cp.schedule_pass([make_row()], "chan", state, now=NOW)
    poster = FakePoster()
    out = cp.comment_pass(state, poster=poster, ledger=ledger)
    assert out[0]["status"] == cp.SCHEDULED
    assert "not published yet" in out[0]["note"]
    assert poster.comments == []


def test_a_published_post_gets_its_comment(state, monkeypatch, ledger):
    fake = FakeBuffer(status="sent", link=PERMALINK)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    monkeypatch.setattr(cp, "verify_identity", lambda p, s: (True, "ok"))
    cp.schedule_pass([make_row()], "chan", state, now=NOW)
    poster = FakePoster()
    out = cp.comment_pass(state, poster=poster, ledger=ledger)
    assert out[0]["status"] == cp.COMMENTED
    assert poster.comments == [LINK]


def test_a_row_with_no_link_is_done_once_published(state, monkeypatch, ledger):
    fake = FakeBuffer(status="sent", link=PERMALINK)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    cp.schedule_pass([make_row(first_comment_link="")], "chan", state, now=NOW)
    poster = FakePoster()
    out = cp.comment_pass(state, poster=poster, ledger=ledger)
    assert out[0]["status"] == cp.COMMENTED
    assert poster.comments == []


def test_a_failed_comment_leaves_the_post_live_and_flags_a_human(state,
                                                                 monkeypatch,
                                                                 ledger):
    fake = FakeBuffer(status="sent", link=PERMALINK)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    monkeypatch.setattr(cp, "verify_identity", lambda p, s: (True, "ok"))
    cp.schedule_pass([make_row()], "chan", state, now=NOW)
    out = cp.comment_pass(state, poster=FakePoster(comment_ok=False),
                          ledger=ledger)
    assert out[0]["status"] == cp.COMMENT_FAILED
    key = out[0]["key"]
    assert state.get(key)["status"] == cp.COMMENT_FAILED
    assert state.get(key)["permalink"] == PERMALINK
    assert fake.n == 1, "the post must not be re-created"


def test_a_comment_pass_rerun_retries_only_the_comment(state, monkeypatch,
                                                       ledger):
    fake = FakeBuffer(status="sent", link=PERMALINK)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    monkeypatch.setattr(cp, "verify_identity", lambda p, s: (True, "ok"))
    cp.schedule_pass([make_row()], "chan", state, now=NOW)
    cp.comment_pass(state, poster=FakePoster(comment_ok=False), ledger=ledger)
    good = FakePoster()
    out = cp.comment_pass(state, poster=good, ledger=ledger)
    assert out[0]["status"] == cp.COMMENTED
    assert good.comments == [LINK]
    assert fake.n == 1


def test_the_identity_guard_stops_every_comment_not_just_one(state, monkeypatch,
                                                             ledger):
    """Wrong account means wrong for the whole sweep, so it aborts the pass."""
    fake = FakeBuffer(status="sent", link=PERMALINK)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    monkeypatch.setattr(cp, "verify_identity",
                        lambda p, s: (False, "logged in as the real account"))
    cp.schedule_pass([make_row(), make_row(post_text="second")], "chan", state,
                     now=NOW)
    poster = FakePoster()
    out = cp.comment_pass(state, poster=poster, ledger=ledger,
                          expect_slug="dev-account")
    assert poster.comments == []
    assert all(e["status"] == cp.COMMENT_FAILED for e in state.rows.values())
    assert "identity guard" in out[-1]["note"]


def test_the_comment_pass_never_calls_create_post(state, monkeypatch, ledger):
    fake = FakeBuffer(status="sent", link=PERMALINK)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    monkeypatch.setattr(cp.bc, "get_post", fake.get_post)
    monkeypatch.setattr(cp, "verify_identity", lambda p, s: (True, "ok"))
    cp.schedule_pass([make_row()], "chan", state, now=NOW)
    before = fake.n
    cp.comment_pass(state, poster=FakePoster(), ledger=ledger)
    assert fake.n == before


# --- reading the CSV --------------------------------------------------------

def test_reading_a_csv(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("date,time_window,post_text,topic,tags,image_path,"
                 "first_comment_link\n2036-09-08,morning,Body,,#a,,%s\n" % LINK,
                 encoding="utf-8")
    rows = cp.read_rows(str(p))
    assert len(rows) == 1 and rows[0]["post_text"] == "Body"


def test_a_csv_missing_required_columns_is_refused(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("post_text,tags\nBody,#a\n", encoding="utf-8")
    with pytest.raises(cp.RowError, match="missing required column"):
        cp.read_rows(str(p))


def test_a_missing_csv_is_refused(tmp_path):
    with pytest.raises(cp.RowError, match="not found"):
        cp.read_rows(str(tmp_path / "nope.csv"))


# --- the summary ------------------------------------------------------------

def test_the_summary_tells_a_human_what_to_do_about_each_state():
    sched = [{"key": "k1", "row": make_row(), "status": cp.FAILED,
              "errors": ["bad date"]}]
    comm = [{"key": "k2", "status": cp.COMMENT_FAILED, "permalink": PERMALINK,
             "note": "submit button missing"}]
    text = cp.summarize(sched, comm)
    assert "do NOT re-run the post" in text
    assert "nothing was published" in text.lower()
    assert PERMALINK in text


def test_the_summary_lists_invalid_rows():
    text = cp.summarize([], [], invalid=[(make_row(), ["image missing"])])
    assert "INVALID" in text and "image missing" in text


def test_generate_text_removes_its_entry_from_the_browser_queue():
    """The generator enqueues into a different pipeline's inbox; leaving it
    there would let the browser poster publish this text a second time."""
    class FakeQueue:
        def __init__(self):
            self.items = [{"id": 1}]
            self.removed = []

        def list_queued(self):
            return list(self.items)

        def remove(self, pid):
            self.removed.append(pid)
            self.items = [i for i in self.items if i.get("id") != pid]

    class FakeGen:
        def __init__(self):
            self.queue = FakeQueue()

        def generate_thought_leadership(self, topic=None):
            self.queue.items.append({"id": 2})
            return {"text": "generated"}

    gen = FakeGen()
    assert cp.generate_text("automation", generator=gen) == "generated"
    assert gen.queue.removed == [2]
    assert json.dumps(gen.queue.items) == json.dumps([{"id": 1}])


# --- QA finding 2026-09-07: a real post's emoji crashed the CLI -------------
#
# The schedule pass CREATED every post, then the run died printing the summary,
# because Windows stdout is cp1252 and the summary quotes post text. The posts
# existed; the report did not. Found by QA using realistic content rather than
# "test post" strings, which is exactly why it was not found earlier.

def test_the_summary_survives_emoji_and_typographic_characters():
    row = make_row(post_text="Ship it \U0001F680 today \u2014 really")
    text = cp.summarize([{"key": "k1", "row": row, "status": cp.SCHEDULED}], [])
    assert "\U0001F680" in text or "Ship it" in text


def test_the_summary_can_be_encoded_for_a_legacy_console():
    """The actual failure: encoding to cp1252 raised. The CLI reconfigures
    stdout to utf-8 with errors=replace, so this must not raise there either."""
    row = make_row(post_text="Ship it \U0001F680 \u2014 now")
    text = cp.summarize([{"key": "k1", "row": row, "status": cp.SCHEDULED}], [])
    text.encode("utf-8")                       # the CLI's stream encoding
    text.encode("cp1252", errors="replace")    # and a legacy one, lossily


def test_the_cli_makes_its_output_unicode_safe():
    import inspect
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import run_scheduled_posts as cli
    src = inspect.getsource(cli)
    assert "reconfigure" in src
    assert "errors=\"replace\"" in src or "errors='replace'" in src
    assert "_make_output_unicode_safe()" in inspect.getsource(cli.main)


# --- UI Phase 1 finding: a failed row must be identifiable ------------------
#
# The queue table showed four failures as "(no text)", because text was only
# recorded once a row scheduled successfully. A failure a human cannot trace
# back to a CSV row is not actionable.

def test_a_row_that_fails_validation_still_records_which_row_it_was(state, patched):
    row = make_row(date="nonsense", post_text="The row about hiring.")
    cp.schedule_pass([row], "chan", state, now=NOW)
    saved = state.get(cp.row_key(row))
    assert saved["status"] == cp.FAILED
    assert "hiring" in saved["text"]


def test_a_row_that_fails_at_buffer_also_records_which_row_it_was(state, monkeypatch):
    fake = FakeBuffer(reject=True)
    monkeypatch.setattr(cp.bc, "create_post", fake.create_post)
    row = make_row(post_text="The row about onboarding.")
    cp.schedule_pass([row], "chan", state, now=NOW)
    assert "onboarding" in state.get(cp.row_key(row))["text"]


def test_a_generated_row_falls_back_to_its_topic_for_identification():
    assert cp.row_preview(make_row(post_text="", topic="AI hiring")) == "AI hiring"


def test_the_preview_is_truncated():
    assert len(cp.row_preview(make_row(post_text="x" * 500))) < 200


# --- the CSV contract, pinned ------------------------------------------------
#
# These exist so the documented contract in docs/SCHEDULED_POSTING.md cannot
# drift from the code. Each corresponds to a line in that table.

CONTRACT_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(**over):
    r = {"date": "2036-09-08", "time_window": "morning", "post_text": "Body.",
         "topic": "", "tags": "", "image_path": "", "first_comment_link": ""}
    r.update(over)
    return r


def _tz():
    from linkedin_automation import buffer_client as bc
    return bc.posting_timezone()


def test_only_date_and_time_window_are_structurally_required():
    """Every other column may be omitted from the file entirely, not merely
    left blank."""
    rows = cp.parse_rows("date,time_window,post_text\n2036-09-08,morning,Body.\n")
    assert cp.validate_row(rows[0], now=CONTRACT_NOW, tz=_tz()) == []
    for missing in ("date", "time_window"):
        cols = [c for c in ("date", "time_window") if c != missing]
        with pytest.raises(cp.RowError, match="missing required column"):
            cp.parse_rows(",".join(cols) + "\nx\n")


def test_column_order_does_not_matter():
    """csv.DictReader is header-NAME based."""
    rows = cp.parse_rows(
        "first_comment_link,time_window,post_text,date\n"
        "https://x.test/a,morning,Reordered.,2036-09-08\n")
    assert rows[0]["post_text"] == "Reordered."
    assert cp.validate_row(rows[0], now=CONTRACT_NOW, tz=_tz()) == []


def test_a_blank_first_comment_link_is_valid():
    assert cp.validate_row(_row(first_comment_link=""),
                           now=CONTRACT_NOW, tz=_tz()) == []


def test_a_blank_image_path_is_valid_and_sends_no_asset():
    from linkedin_automation import buffer_client as bc
    assert cp.validate_row(_row(image_path=""), now=CONTRACT_NOW, tz=_tz()) == []
    payload = bc.build_post_input("c", "t", None, "2036-09-08T16:00:00.000Z")
    assert payload["assets"] == []


def test_at_least_one_of_post_text_or_topic_is_required():
    tz = _tz()
    assert cp.validate_row(_row(post_text="B.", topic=""),
                           now=CONTRACT_NOW, tz=tz) == []
    assert cp.validate_row(_row(post_text="", topic="t"),
                           now=CONTRACT_NOW, tz=tz) == []
    assert cp.validate_row(_row(post_text="B.", topic="t"),
                           now=CONTRACT_NOW, tz=tz) == []
    both_blank = cp.validate_row(_row(post_text="", topic=""),
                                 now=CONTRACT_NOW, tz=tz)
    assert any("nothing to post" in p for p in both_blank)


def test_post_text_wins_and_topic_is_inert_when_both_are_present(tmp_path):
    """No generation happens when text is supplied."""
    sent = []
    state = cp.PipelineState(path=str(tmp_path / "s.json"))

    def create(channel_id, text, image_url, when, key=None, session=None):
        sent.append(text)
        return {"id": "p1", "status": "scheduled", "dueAt": when}

    def gen(topic, profile_name=None):
        raise AssertionError("generation must not run when post_text is set")

    real = cp.bc.create_post
    cp.bc.create_post = create
    try:
        cp.schedule_pass([_row(post_text="Explicit body.", topic="ignored")],
                         "chan", state, generate=gen, now=CONTRACT_NOW)
    finally:
        cp.bc.create_post = real
    assert sent[0].startswith("Explicit body.")


def test_the_shipped_example_calendar_validates():
    """The template handed to users must actually pass validation - bar the
    image paths a reader will not have on disk."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "docs", "example_calendar.csv"),
              encoding="utf-8-sig") as f:
        rows = cp.parse_rows(f.read())
    assert len(rows) == 5
    tz = _tz()
    for i, row in enumerate(rows, start=1):
        problems = [p for p in cp.validate_row(row, now=CONTRACT_NOW, tz=tz)
                    if "image_path does not exist" not in p]
        assert problems == [], "example row %d: %s" % (i, problems)
    # the variations the template exists to demonstrate
    assert rows[1]["first_comment_link"] == ""
    assert rows[2]["image_path"] == ""
    assert rows[3]["post_text"] == "" and rows[3]["topic"]


def test_state_can_be_saved_to_a_bare_filename(tmp_path, monkeypatch):
    """dirname is "" for a relative filename, and os.makedirs("") raises."""
    monkeypatch.chdir(tmp_path)
    state = cp.PipelineState(path="state.json")
    state.update("k", status=cp.PENDING)
    assert (tmp_path / "state.json").exists()


# --- the image column, and columns we do not read ---------------------------
#
# 2026-09-09: a real calendar exported from media-gen used `asset_path`, not
# `image_path`. Every row published text-only and reported SUCCESS, because an
# unrecognised column was silently ignored. The images were on disk and correct;
# the pipeline simply never saw them.

def test_asset_path_is_accepted_as_the_image_column(tmp_path):
    img = tmp_path / "pic.webp"
    img.write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64)
    row = make_row(image_path="", post_text="Body.")
    row.pop("image_path")
    row["asset_path"] = str(img)
    assert cp.row_image_path(row) == str(img)
    assert cp.validate_row(row, now=NOW) == []


def test_image_path_wins_when_both_columns_are_present(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    for f in (a, b):
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    row = make_row(image_path=str(a))
    row["asset_path"] = str(b)
    assert cp.row_image_path(row) == str(a)


def test_the_same_picture_under_either_column_is_the_same_row(tmp_path):
    """Otherwise renaming the column would queue a duplicate of a post that
    already exists."""
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    a = make_row(image_path=str(img))
    b = make_row(image_path="")
    b.pop("image_path")
    b["asset_path"] = str(img)
    assert cp.row_key(a) == cp.row_key(b)


def test_an_asset_path_row_actually_uploads_and_attaches(tmp_path):
    """The end of the chain: the resolved path reaches the uploader, and its
    URL reaches createPost as an asset."""
    img = tmp_path / "pic.webp"
    img.write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64)
    row = make_row(image_path="")
    row.pop("image_path")
    row["asset_path"] = str(img)

    uploaded, sent = [], []

    def upload(path):
        uploaded.append(path)
        return "https://pub-abc.r2.dev/posts/deadbeef.webp"

    def create(channel_id, text, image_url, when, key=None, session=None):
        sent.append(image_url)
        return {"id": "p1", "status": "scheduled", "dueAt": when}

    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    real = cp.bc.create_post
    cp.bc.create_post = create
    try:
        cp.schedule_pass([row], "chan", state, upload=upload, now=NOW)
    finally:
        cp.bc.create_post = real
    assert uploaded == [str(img)]
    assert sent == ["https://pub-abc.r2.dev/posts/deadbeef.webp"]


def test_unknown_columns_are_reported_not_silently_dropped(tmp_path):
    """The actual defect. Accepting a row while ignoring the column that held
    its image is how a wrong result looked like a right one."""
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    row = make_row()
    row.update({"job_id": "abc", "asset_id": "abc#0", "role": "image",
                "status": "succeeded"})
    _acc, _rej, _skip, warnings = cp.queue_rows([row], state, now=NOW)
    assert warnings, "an unread column must be reported"
    joined = " ".join(warnings)
    for col in ("job_id", "asset_id", "role", "status"):
        assert col in joined
    assert "rename it to image_path" in joined


def test_a_calendar_with_no_image_column_at_all_says_so(tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    row = make_row()
    row.pop("image_path")
    _acc, _rej, _skip, warnings = cp.queue_rows([row], state, now=NOW)
    assert any("publish WITHOUT an image" in w for w in warnings)


def test_no_warning_when_the_calendar_is_well_formed(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    _acc, _rej, _skip, warnings = cp.queue_rows(
        [make_row(image_path=str(img))], state, now=NOW)
    assert warnings == []
