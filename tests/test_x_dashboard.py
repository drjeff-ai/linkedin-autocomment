"""Phase 5: the scheduled-posting section, per platform.

Offline — every Buffer boundary is injected or mocked. The claim that carries
the most risk is the one about the OTHER platform: with no `platform` param, or
with `platform=linkedin`, the section must behave exactly as it did, because
these are the routes production runs through.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402

PERMALINK = "https://www.linkedin.com/feed/update/urn:li:share:0000000000000000000"


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture(autouse=True)
def no_live_buffer(monkeypatch):
    """The reconcile must never reach Buffer from a test."""
    monkeypatch.setattr(dashboard.buffer_client, "posts_by_status",
                        lambda *a, **k: {})
    dashboard._reconcile_cache.clear()
    yield
    dashboard._reconcile_cache.clear()


@pytest.fixture
def two_platforms(tmp_path, monkeypatch):
    """A profile configured for BOTH platforms, with separate state files."""
    (tmp_path / "x").mkdir()

    def get_data_dir(profile_name=None, subdir=None):
        return str(tmp_path / subdir) if subdir else str(tmp_path)

    monkeypatch.setattr(cp.pm, "get_data_dir", get_data_dir)

    # A LinkedIn row and an X row, in their own stores.
    (tmp_path / cp.STATE_NAME).write_text(json.dumps({"rows": {
        "li_row": {"status": cp.SCHEDULED, "post_id": "li1",
                   "text": "A LinkedIn post.", "permalink": PERMALINK}}}),
        encoding="utf-8")
    (tmp_path / "x" / cp.STATE_NAME).write_text(json.dumps({"rows": {
        "x_row": {"status": cp.SCHEDULED, "post_id": "x1",
                  "text": "An X post."}}}), encoding="utf-8")

    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {
        "scheduled_posting": {
            "buffer_channel_id": "chan_li",
            "identity_slug": "example-person-one",
            "platforms": {"x": {
                "buffer_api_key_env": "BUFFER_API_KEY_X",
                "buffer_channel_id": "chan_x",
                "identity_slug": "AI_Fun_times"}}}})
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")
    monkeypatch.setenv("BUFFER_API_KEY", "li-key")
    return tmp_path


# ─── LinkedIn is unchanged ───────────────────────────────────────────────────

def test_the_queue_defaults_to_linkedin_with_no_param(client, two_platforms):
    """Every existing caller sends no platform. It must land on LinkedIn."""
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["platform"] == "linkedin"
    assert d["total"] == 1
    assert d["posts"][cp.SCHEDULED][0]["preview"] == "A LinkedIn post."


def test_linkedin_still_reports_a_first_comment_step(client, two_platforms):
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["first_comment"] is True


def test_the_linkedin_identity_still_comes_from_the_flat_fields(client,
                                                                two_platforms):
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["config"]["identity_slug"] == "example-person-one"
    assert d["config"]["buffer_channel_id"] == "chan_li"
    assert d["config"]["identity_configured"] is True


# ─── X reads its own store ───────────────────────────────────────────────────

def test_the_queue_reads_the_x_store_when_x_is_selected(client, two_platforms):
    """Separate stores. Showing LinkedIn's rows under an X header would be a
    lie about which account those posts belong to."""
    d = client.get("/api/scheduled/p/queue?platform=x").get_json()
    assert d["platform"] == "x"
    assert d["total"] == 1
    assert d["posts"][cp.SCHEDULED][0]["preview"] == "An X post."


def test_the_two_platforms_never_show_each_others_rows(client, two_platforms):
    li = client.get("/api/scheduled/p/queue").get_json()
    x = client.get("/api/scheduled/p/queue?platform=x").get_json()
    assert li["state_file"] != x["state_file"]
    assert li["posts"][cp.SCHEDULED][0]["preview"] != x["posts"][cp.SCHEDULED][0]["preview"]


def test_x_reports_no_first_comment_step(client, two_platforms):
    """X carries its link in the BODY. Implying a first comment would describe
    a step that does not exist and a browser that never runs."""
    d = client.get("/api/scheduled/p/queue?platform=x").get_json()
    assert d["first_comment"] is False


def test_the_x_identity_comes_from_the_overlay_not_the_flat_fields(client,
                                                                   two_platforms):
    """The header must name X's account, not LinkedIn's."""
    d = client.get("/api/scheduled/p/queue?platform=x").get_json()
    assert d["config"]["identity_slug"] == "AI_Fun_times"
    assert d["config"]["buffer_channel_id"] == "chan_x"


# ─── fail-closed is VISIBLE, not blank ───────────────────────────────────────

def test_an_unconfigured_x_says_it_will_refuse(client, tmp_path, monkeypatch):
    """A blank header reads as "not set up yet". The guard will actually
    REFUSE, and that is worth knowing before the button is pressed."""
    monkeypatch.setattr(cp.pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(tmp_path))
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {
        "scheduled_posting": {"buffer_channel_id": "chan_li",
                              "identity_slug": "example-person-one",
                              "platforms": {"x": {
                                  "buffer_api_key_env": "BUFFER_API_KEY_X",
                                  "buffer_channel_id": "",
                                  "identity_slug": ""}}}})
    monkeypatch.setenv("BUFFER_API_KEY_X", "x-key")

    d = client.get("/api/scheduled/p/queue?platform=x").get_json()
    assert d["config"]["configured"] is False
    problem = d["config"]["problem"]
    assert problem and "refuse" in problem
    assert "no Buffer channel" in problem and "no identity_slug" in problem


def test_a_blank_linkedin_identity_is_not_called_a_scheduling_blocker(
        client, tmp_path, monkeypatch):
    """LinkedIn's schedule path is deliberately ungated (see BACKLOG), so a
    blank slug there does not stop scheduling - saying it would be false."""
    monkeypatch.setattr(cp.pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(tmp_path))
    monkeypatch.setattr(dashboard.pm, "get_profile_config", lambda name=None: {
        "scheduled_posting": {"buffer_channel_id": "chan_li",
                              "identity_slug": ""}})
    d = client.get("/api/scheduled/p/queue").get_json()
    assert d["config"]["problem"] is None


def test_an_unknown_platform_is_a_400_not_a_500(client, two_platforms):
    """resolve_scheduled refuses an unknown platform rather than falling back
    to LinkedIn's channel. Saying so once here beats six routes raising."""
    res = client.get("/api/scheduled/p/queue?platform=mastodon")
    assert res.status_code == 400
    assert "mastodon" in res.get_json()["error"]


# ─── the reconcile, per platform ─────────────────────────────────────────────

def test_the_reconcile_cache_does_not_cross_platforms(client, two_platforms,
                                                      monkeypatch):
    """Two platforms are two Buffer accounts. A shared cache entry would let an
    X render be answered by LinkedIn's reconcile."""
    seen = []

    def fake(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        seen.append({"channel": channel_id, "statuses": tuple(statuses),
                     "key": key})
        return {}

    monkeypatch.setattr(dashboard.buffer_client, "posts_by_status", fake)
    client.get("/api/scheduled/p/queue")
    client.get("/api/scheduled/p/queue?platform=x")

    assert len(seen) == 2, "the second platform was served the first's cache"
    assert seen[0]["channel"] == "chan_li"
    assert seen[1]["channel"] == "chan_x"


def test_x_reconciles_against_errors_as_well_as_sent(client, two_platforms,
                                                     monkeypatch):
    """create-success is not publish-success on X (Phase 1, live)."""
    seen = {}

    def fake(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        seen["statuses"] = tuple(statuses)
        return {}

    monkeypatch.setattr(dashboard.buffer_client, "posts_by_status", fake)
    client.get("/api/scheduled/p/queue?platform=x")
    assert seen["statuses"] == ("sent", "error")


def test_linkedins_reconcile_still_asks_only_about_sent(client, two_platforms,
                                                        monkeypatch):
    seen = {}

    def fake(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        seen["statuses"] = tuple(statuses)
        return {}

    monkeypatch.setattr(dashboard.buffer_client, "posts_by_status", fake)
    client.get("/api/scheduled/p/queue")
    assert seen["statuses"] == ("sent",)


def test_an_x_post_that_errored_shows_as_failed_in_the_queue(client,
                                                             two_platforms,
                                                             monkeypatch):
    """The whole point of reconciling X on render."""
    def fake(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        return {"x1": {"id": "x1", "status": "error",
                       "error": {"message": "Media URL not publicly accessible"}}}

    monkeypatch.setattr(dashboard.buffer_client, "posts_by_status", fake)
    d = client.get("/api/scheduled/p/queue?platform=x").get_json()
    assert d["counts"][cp.FAILED] == 1
    assert d["counts"][cp.SCHEDULED] == 0
    row = d["posts"][cp.FAILED][0]
    assert any("Media URL" in e for e in row.get("errors") or [])


# ─── scheduling through the route ────────────────────────────────────────────

def test_the_schedule_job_refuses_a_wrong_x_identity(two_platforms, monkeypatch):
    """schedule_pass raises IdentityRefused; the job must surface it, not
    report a silent zero."""
    monkeypatch.setattr(dashboard.buffer_client, "get_channel",
                        lambda cid, key=None, session=None: {
                            "id": cid, "name": "someone_else"})
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda *a, **k: {"limit": 10, "used": 0, "free": 10})
    created = []
    monkeypatch.setattr(dashboard.buffer_client, "create_post",
                        lambda *a, **k: created.append(a) or {"id": "p"})
    monkeypatch.setattr(dashboard.csv_pipeline, "pending_rows",
                        lambda state: [{"date": "2036-11-02",
                                        "time_window": "morning",
                                        "post_text": "An X post about llms.",
                                        "topic": "t", "tags": "",
                                        "image_path": "",
                                        "first_comment_link": ""}])

    out = dashboard._scheduled_schedule_job("job1", "p", "chan_x", platform="x")
    assert created == []
    assert out["scheduled"] == 0
    assert "AI_Fun_times" in out["error"] and "someone_else" in out["error"]


def test_the_schedule_job_uses_the_x_key_and_channel(two_platforms, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard.buffer_client, "get_channel",
                        lambda cid, key=None, session=None: {
                            "id": cid, "name": "AI_Fun_times"})
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda *a, **k: {"limit": 10, "used": 0, "free": 10})

    def create(channel_id, body, image_url, when, key=None, session=None):
        seen.update({"channel": channel_id, "key": key, "body": body})
        return {"id": "p1", "status": "scheduled", "dueAt": when}

    monkeypatch.setattr(dashboard.buffer_client, "create_post", create)
    monkeypatch.setattr(dashboard.csv_pipeline, "pending_rows",
                        lambda state: [{"date": "2036-11-02",
                                        "time_window": "morning",
                                        "post_text": "An X post about llms.",
                                        "topic": "t", "tags": "",
                                        "image_path": "",
                                        "first_comment_link": "https://ex.com/a"}])

    out = dashboard._scheduled_schedule_job("job2", "p", "chan_x", platform="x")
    assert out["scheduled"] == 1
    assert seen["channel"] == "chan_x"
    assert seen["key"] == "x-key"
    # And the link is in the BODY on X.
    assert "https://ex.com/a" in seen["body"]


def test_an_over_280_x_row_is_rejected_through_the_route(two_platforms,
                                                         monkeypatch):
    """The validate_row rejection has to reach the UI the same way a missing
    image does - as a FAILED row carrying its reason."""
    monkeypatch.setattr(dashboard.buffer_client, "get_channel",
                        lambda cid, key=None, session=None: {
                            "id": cid, "name": "AI_Fun_times"})
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda *a, **k: {"limit": 10, "used": 0, "free": 10})
    created = []
    monkeypatch.setattr(dashboard.buffer_client, "create_post",
                        lambda *a, **k: created.append(a) or {"id": "p"})
    monkeypatch.setattr(dashboard.csv_pipeline, "pending_rows",
                        lambda state: [{"date": "2036-11-02",
                                        "time_window": "morning",
                                        "post_text": "a" * 300, "topic": "t",
                                        "tags": "", "image_path": "",
                                        "first_comment_link": ""}])

    out = dashboard._scheduled_schedule_job("job3", "p", "chan_x", platform="x")
    assert created == []
    assert out["failed"] == 1
    errors = out["results"][0]["errors"]
    assert any("too long for X" in e for e in errors)
    assert out["results"][0]["stage"] == "validate"


def test_the_preflight_names_the_handle_buffer_actually_reports(client,
                                                                two_platforms,
                                                                monkeypatch):
    """A label kept in config can go stale. The dialog asks Buffer."""
    monkeypatch.setattr(dashboard.buffer_client, "get_channel",
                        lambda cid, key=None, session=None: {
                            "id": cid, "name": "AI_Fun_times",
                            "externalLink": "https://x.com/AI_Fun_times"})
    monkeypatch.setattr(dashboard.buffer_client, "scheduled_slots",
                        lambda *a, **k: {"limit": 10, "used": 2, "free": 8})
    d = client.get("/api/scheduled/p/schedule/preflight?platform=x").get_json()
    assert d["platform"] == "x"
    assert d["actual_handle"] == "AI_Fun_times"
    assert d["account"]["identity_slug"] == "AI_Fun_times"


# ─── the drain is untouched ──────────────────────────────────────────────────

def test_the_drain_reads_each_platforms_store_through_the_gate():
    """X1 (DEC-011) made the drain per-platform. This test used to pin the
    opposite - "no X background path yet" - and is replaced, not loosened:
    an X background path now exists, so what must hold is that it reads the
    platform's own store and goes through the Schedule button's gate. The
    behaviour itself is proved in tests/test_x_drain.py."""
    import inspect
    src = inspect.getsource(dashboard._drain_feed_job)
    assert "PipelineState(profile_name=profile_name," in src
    assert "platform=platform" in src
    assert "_production_gate(platform, resolved)" in src
    assert "_gated_schedule_pass(" in src


def test_the_drain_wiring_is_unchanged():
    import inspect
    src = inspect.getsource(dashboard)
    assert "channel_fn=_scheduled_channel_id," in src
