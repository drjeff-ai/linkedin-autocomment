"""Per-row image status in the scheduled-posting queue.

A whole calendar once published imageless because the queue showed nothing
about images at all, and "nothing" is indistinguishable from "text-only, on
purpose". So the assertions that matter here are not that a thumbnail appears.
They are that every state renders SOMETHING, and that a row which asked for an
image and did not get one is loud rather than blank.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402

R2 = "https://pub-abc.r2.dev/posts/deadbeefdeadbeef.webp"


def _rec(**kw):
    rec = {"status": cp.PENDING, "row": {}}
    rec.update(kw)
    return rec


# ─── which column supplied the path ───────────────────────────────────────────

def test_it_reports_which_column_the_path_came_from():
    assert cp.row_image_column({"image_path": "/x/a.png"}) == "image_path"
    assert cp.row_image_column({"asset_path": "/x/a.png"}) == "asset_path"


def test_a_column_that_is_present_but_empty_is_not_a_source():
    """The exact shape of the calendar that published imageless: the column is
    there, matched, and blank on every row."""
    assert cp.row_image_column({"image_path": ""}) is None
    assert cp.row_image_column({"image_path": "   "}) is None


def test_no_image_column_at_all_is_not_a_source():
    assert cp.row_image_column({"post_text": "hi"}) is None


# ─── the five states ──────────────────────────────────────────────────────────

def test_an_uploaded_image_reads_as_attached():
    v = dashboard._scheduled_image_view(
        _rec(image_url=R2, post_id="p1", row={"image_path": "/x/a.png"}))
    assert v["state"] == dashboard.IMAGE_ATTACHED
    assert v["url"] == R2
    assert v["problem"] is False


def test_a_local_file_that_exists_reads_as_ready(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    v = dashboard._scheduled_image_view(_rec(row={"image_path": str(f)}))
    assert v["state"] == dashboard.IMAGE_READY
    assert v["problem"] is False
    assert "upload" in v["detail"]


def test_a_path_with_no_file_is_a_loud_problem():
    v = dashboard._scheduled_image_view(
        _rec(row={"image_path": r"S:\nope\missing.png"}))
    assert v["state"] == dashboard.IMAGE_MISSING
    assert v["problem"] is True
    assert "missing.png" in v["detail"]
    assert "image_path" in v["detail"], "it must name the column"


def test_a_scheduled_post_with_no_image_url_but_a_real_file_is_a_loud_problem(tmp_path):
    """THE failure being fixed: the post exists, the row asked for an image,
    the file is right there, and the post went out without it."""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    v = dashboard._scheduled_image_view(
        _rec(status=cp.SCHEDULED, post_id="p1", row={"image_path": str(f)}))
    assert v["state"] == dashboard.IMAGE_LOST
    assert v["problem"] is True
    assert "without it" in v["detail"]


def test_a_row_with_no_image_reads_as_text_only():
    v = dashboard._scheduled_image_view(_rec(row={"post_text": "hi"}))
    assert v["state"] == dashboard.IMAGE_NONE
    assert v["problem"] is False
    assert v["label"] == "Text-only"


def test_text_only_names_the_columns_it_looked_in():
    """So an unexpectedly text-only calendar points at the column, not the rows."""
    v = dashboard._scheduled_image_view(_rec(row={"post_text": "hi"}))
    for column in cp.IMAGE_PATH_COLUMNS:
        assert column in v["detail"]


def test_an_empty_image_column_still_reads_as_text_only_not_as_a_problem():
    v = dashboard._scheduled_image_view(_rec(row={"image_path": ""}))
    assert v["state"] == dashboard.IMAGE_NONE


def test_asset_path_is_honoured_the_same_as_image_path(tmp_path):
    f = tmp_path / "a.webp"
    f.write_bytes(b"x")
    v = dashboard._scheduled_image_view(_rec(row={"asset_path": str(f)}))
    assert v["state"] == dashboard.IMAGE_READY
    assert v["column"] == "asset_path"


# ─── it never renders nothing ─────────────────────────────────────────────────

@pytest.mark.parametrize("rec", [
    _rec(),
    _rec(row={"image_path": ""}),
    _rec(row={"image_path": "/nope/x.png"}),
    _rec(image_url=R2, post_id="p"),
    _rec(post_id="p", row={"image_path": "/nope/x.png"}),
])
def test_every_row_gets_a_label_and_a_detail(rec):
    v = dashboard._scheduled_image_view(rec)
    assert v["label"], "a blank label is the invisible failure again"
    assert v["detail"]
    assert v["state"] in (dashboard.IMAGE_ATTACHED, dashboard.IMAGE_READY,
                          dashboard.IMAGE_MISSING, dashboard.IMAGE_LOST,
                          dashboard.IMAGE_NONE)


def test_a_missing_row_dict_does_not_blow_up():
    v = dashboard._scheduled_image_view({"status": cp.COMMENTED})
    assert v["state"] == dashboard.IMAGE_NONE


# ─── the queue endpoint carries it ────────────────────────────────────────────

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cp.pm, "get_data_dir", lambda profile_name=None, *a: str(tmp_path))
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {})
    return tmp_path


def test_the_queue_row_carries_the_image_status(client, state_dir, tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "scheduled_posts_state.json"))
    state.update("a", status=cp.SCHEDULED, post_id="p1", image_url=R2,
                 row={"date": "2036-01-01", "image_path": "/x/a.png"})
    state.update("b", status=cp.PENDING,
                 row={"date": "2036-01-02", "post_text": "text only"})
    state.update("c", status=cp.PENDING,
                 row={"date": "2036-01-03", "image_path": "/nope/gone.png"})

    d = client.get("/api/scheduled/p/queue").get_json()
    by_key = {r["key"]: r["image"] for bucket in d["posts"].values()
              for r in bucket}
    assert by_key["a"]["state"] == dashboard.IMAGE_ATTACHED
    assert by_key["a"]["url"] == R2
    assert by_key["b"]["state"] == dashboard.IMAGE_NONE
    assert by_key["c"]["state"] == dashboard.IMAGE_MISSING
    assert by_key["c"]["problem"] is True


def test_the_queue_is_still_read_only(client, state_dir, tmp_path):
    """Reading image status must not touch the state file."""
    path = tmp_path / "scheduled_posts_state.json"
    state = cp.PipelineState(path=str(path))
    state.update("a", status=cp.PENDING, row={"image_path": "/nope/x.png"})
    before = path.read_bytes()
    client.get("/api/scheduled/p/queue")
    assert path.read_bytes() == before


# ─── the template renders every state ─────────────────────────────────────────

def _template():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "linkedin_automation", "templates", "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_row_builder_renders_the_image_cell():
    html = _template()
    assert "function schpImageCell(" in html
    assert "const img = schpImageCell(r);" in html
    assert "img.thumb +" in html
    assert "img.line + link" in html


def test_every_state_has_a_branch_in_the_renderer():
    html = _template()
    cell = html[html.index("function schpImageCell("):
                html.index("function schpThumbBroke(")]
    for state in ("attached", "ready", "missing", "lost"):
        assert "'%s'" % state in cell, state
    assert "text-only" in cell


def test_thumbnails_are_lazy_and_sized():
    html = _template()
    assert 'loading="lazy"' in html
    assert ".schp-thumb {" in html
    assert "width: 56px" in html


def test_a_broken_thumbnail_does_not_leave_a_blank_square():
    """A silently failed <img> is an empty cell, which is the original bug."""
    html = _template()
    assert "onerror=" in html
    assert "function schpThumbBroke(" in html
    assert "load<br>failed" in html


def test_a_ready_image_does_not_look_like_no_image():
    """Scanning for which rows carry a picture is the point; those two states
    sharing a style defeats it."""
    html = _template()
    assert ".schp-thumb-ready" in html
    cell = html[html.index("function schpImageCell("):
                html.index("function schpThumbBroke(")]
    assert "schp-thumb-ready" in cell
    ready = cell[cell.index("'ready'"):]
    assert "schp-thumb-none" not in ready.split("if (im.state === 'missing'")[0]


def test_the_problem_states_get_the_warning_style():
    html = _template()
    assert ".schp-thumb-problem" in html
    assert ".schp-imgline-problem" in html
