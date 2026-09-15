"""The three ways a row can fail to reach Buffer, and why they must differ.

`prepare` means nothing left this machine: retry freely.
`createPost` means Buffer answered and refused: nothing exists, it said so.
`createPost_unknown` means the request was IN FLIGHT: a post may exist.

The third used to be reported as `prepare`, whose queue text reads "failed
before publishing - nothing exists". That is the most dangerous sentence the
tool could show, because it invites a re-queue that publishes the post twice,
and nothing can unpublish the second one.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402

ROW = {"date": "2036-11-01", "time_window": "morning", "topic": "",
       "tags": "", "first_comment_link": "", "image_path": ""}


def _run(tmp_path, exc, upload=None, image=None):
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    row = dict(ROW, post_text="A post.")
    if image:
        row["image_path"] = str(image)
    cp.queue_rows([row], state)

    def boom(*a, **k):
        raise exc

    original = cp.bc.create_post
    cp.bc.create_post = boom
    try:
        results = cp.schedule_pass(cp.pending_rows(state), "chan1", state,
                                   max_new=10, upload=upload)
    finally:
        cp.bc.create_post = original
    return results[0], state


def _png(tmp_path):
    f = tmp_path / "g.png"
    f.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 60)
    return f


def test_a_transport_failure_is_recorded_as_unknown(tmp_path):
    r, _ = _run(tmp_path, cp.bc.BufferError(
        "Buffer request failed (createPost): timed out"))
    assert r["stage"] == cp.STAGE_CREATE_UNKNOWN


def test_an_unknown_outcome_tells_the_operator_to_check_buffer(tmp_path):
    r, _ = _run(tmp_path, cp.bc.BufferError("connection reset"))
    joined = " ".join(r["errors"])
    assert "may or may not have been created" in joined
    assert "Check Buffer" in joined
    assert "BEFORE re-queueing" in joined


def test_a_refusal_from_buffer_is_not_unknown(tmp_path):
    """Buffer answered. It said nothing was created, so say that."""
    r, _ = _run(tmp_path, cp.bc.BufferRejected(
        "Buffer refused the post (X): bad input. NOTHING was created."))
    assert r["stage"] == cp.STAGE_CREATE_REFUSED
    assert not any("may or may not" in e for e in r["errors"])


def test_a_failure_before_the_request_is_prepare(tmp_path):
    """An upload that never got as far as Buffer must stay retryable."""
    from linkedin_automation import image_host as ih

    def bad_upload(path):
        raise ih.UploadFailed("R2 down")

    r, _ = _run(tmp_path, RuntimeError("unused"), upload=bad_upload,
                image=_png(tmp_path))
    assert r["stage"] == cp.STAGE_PREPARE
    assert not any("may or may not" in e for e in r["errors"])


def test_a_generation_failure_is_also_prepare(tmp_path):
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    row = dict(ROW, post_text="", topic="a topic")
    cp.queue_rows([row], state)

    def bad_gen(topic, profile_name=None):
        raise RuntimeError("OpenAI is down")

    results = cp.schedule_pass(cp.pending_rows(state), "chan1", state,
                               max_new=10, generate=bad_gen)
    assert results[0]["stage"] == cp.STAGE_PREPARE


def test_capacity_is_still_held_not_failed(tmp_path):
    r, _ = _run(tmp_path, cp.bc.BufferAtCapacity("limit reached"))
    assert r["status"] == cp.HELD
    assert r.get("stage") is None


# ─── what the queue tells the operator ────────────────────────────────────────

def test_the_queue_never_says_nothing_exists_for_an_unknown_outcome():
    view = dashboard._scheduled_row_view("k", {
        "status": cp.FAILED, "stage": cp.STAGE_CREATE_UNKNOWN,
        "errors": ["timed out"], "text": "A post."})
    assert "nothing exists" not in view["meaning"]
    assert "MAY exist" in view["meaning"]
    assert "check Buffer" in view["action"]
    assert "Do NOT schedule again blind" in view["action"]
    assert view["outcome_unknown"] is True


def test_an_ordinary_failure_still_reads_as_nothing_exists():
    view = dashboard._scheduled_row_view("k", {
        "status": cp.FAILED, "stage": cp.STAGE_PREPARE,
        "errors": ["bad date"], "text": "A post."})
    assert "nothing exists" in view["meaning"]
    assert view["outcome_unknown"] is False


def test_the_two_failure_texts_are_not_the_same():
    unknown = dashboard._scheduled_row_view(
        "k", {"status": cp.FAILED, "stage": cp.STAGE_CREATE_UNKNOWN})
    ordinary = dashboard._scheduled_row_view(
        "k", {"status": cp.FAILED, "stage": cp.STAGE_PREPARE})
    assert unknown["meaning"] != ordinary["meaning"]
    assert unknown["action"] != ordinary["action"]
    assert unknown["label"] != ordinary["label"]


# ─── the automatic paths never retry a failed row ─────────────────────────────

@pytest.mark.parametrize("stage", [cp.STAGE_PREPARE, cp.STAGE_CREATE_REFUSED,
                                   cp.STAGE_CREATE_UNKNOWN])
def test_a_failed_row_is_never_picked_up_again_on_its_own(tmp_path, stage):
    """Neither the Schedule button nor the drain may retry a FAILED row: an
    unknown-outcome row retried automatically is a double post."""
    state = cp.PipelineState(path=str(tmp_path / "s.json"))
    state.update("k", status=cp.FAILED, stage=stage,
                 row=dict(ROW, post_text="A post."))
    assert cp.pending_rows(state) == []
    assert cp.held_rows(state) == []
