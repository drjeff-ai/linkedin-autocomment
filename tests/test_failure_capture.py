"""Tests for screenshot-on-failure diagnostics (screenshot-on-failure).

capture_failure must: write a PNG + JSON sidecar (+ page source), never throw
(even when the driver's screenshot raises), and return the saved path. The
wire-in points must call it on the right failure conditions. No real browser —
a fake driver stands in; capture_failure is monkeypatched at each call site to
assert it fires.
"""

import json
import logging
import os
import types

import pytest

from linkedin_automation import failure_capture as fc


# ─── Fakes ────────────────────────────────────────────────────────────────────

class FakeDriver:
    def __init__(self, screenshot_ok=True, raise_screenshot=False):
        self.current_url = "https://www.linkedin.com/feed/"
        self.title = "Feed | LinkedIn"
        self.page_source = "<html><body>feed</body></html>"
        self._ok = screenshot_ok
        self._raise = raise_screenshot

    def save_screenshot(self, path):
        if self._raise:
            raise RuntimeError("screenshot boom")
        if self._ok:
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")   # minimal PNG-ish bytes
            return True
        return False


@pytest.fixture
def failures_dir(tmp_path, monkeypatch):
    d = tmp_path / "failures"
    d.mkdir()
    monkeypatch.setattr(fc.pm, "get_data_dir", lambda profile_name=None, subdir=None: str(d))
    return d


# ─── capture_failure: artifacts ───────────────────────────────────────────────

def test_writes_png_sidecar_and_html(failures_dir):
    path = fc.capture_failure(FakeDriver(), "feed_no_posts", profile_name="demo")
    assert path is not None and path.endswith(".png")
    assert os.path.exists(path)

    sidecar = path[:-4] + ".json"
    assert os.path.exists(sidecar)
    data = json.load(open(sidecar, encoding="utf-8"))
    assert data["label"] == "feed_no_posts"
    assert data["url"] == "https://www.linkedin.com/feed/"
    assert data["title"] == "Feed | LinkedIn"
    assert data["screenshot"] == os.path.basename(path)

    html = path[:-4] + ".html"
    assert os.path.exists(html)
    assert "feed" in open(html, encoding="utf-8").read()


def test_returns_the_saved_path(failures_dir):
    path = fc.capture_failure(FakeDriver(), "x", profile_name="demo")
    assert path and os.path.basename(path).startswith("failure_x_") and path.endswith(".png")


def test_page_source_can_be_disabled(failures_dir):
    path = fc.capture_failure(FakeDriver(), "x", profile_name="demo", page_source=False)
    assert not os.path.exists(path[:-4] + ".html")


def test_label_is_sanitized_for_filename(failures_dir):
    path = fc.capture_failure(FakeDriver(), "weird label/with:chars", profile_name="demo")
    assert "weird_label_with_chars" in os.path.basename(path)


# ─── capture_failure: never throws ────────────────────────────────────────────

def test_screenshot_raising_returns_none_and_does_not_throw(failures_dir):
    path = fc.capture_failure(FakeDriver(raise_screenshot=True), "boom", profile_name="demo")
    assert path is None                         # no PNG, but no exception either
    # The context sidecar is still written so the failure isn't a total blank.
    assert list(failures_dir.glob("failure_boom_*.json"))


def test_screenshot_false_returns_none(failures_dir):
    assert fc.capture_failure(FakeDriver(screenshot_ok=False), "x", profile_name="demo") is None


def test_context_reads_are_guarded(failures_dir):
    class DeadDriver:
        """A driver whose context reads raise, but whose screenshot works."""
        title = "T"

        @property
        def current_url(self):
            raise RuntimeError("dead")

        @property
        def page_source(self):
            raise RuntimeError("dead")

        def save_screenshot(self, path):
            with open(path, "wb") as f:
                f.write(b"\x89PNG")
            return True

    path = fc.capture_failure(DeadDriver(), "x", profile_name="demo")   # must not throw
    assert path and os.path.exists(path)
    data = json.load(open(path[:-4] + ".json", encoding="utf-8"))
    assert data["url"] is None                  # guarded read -> None, not a crash
    assert not os.path.exists(path[:-4] + ".html")   # page_source raised -> no dump


def test_get_data_dir_failure_returns_none(monkeypatch):
    monkeypatch.setattr(fc.pm, "get_data_dir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no dir")))
    assert fc.capture_failure(FakeDriver(), "x") is None   # never throws


# ─── Wire-in points call capture_failure on the right failures ────────────────

def test_connector_captures_send_modal_missing(monkeypatch):
    from linkedin_automation import auto_connector as ac
    calls = []
    monkeypatch.setattr(ac, "capture_failure", lambda d, label, prof=None, **k: calls.append(label))
    monkeypatch.setattr(ac.hb, "human_sleep", lambda *a, **k: None)

    c = ac.LinkedInAutoConnector.__new__(ac.LinkedInAutoConnector)
    c.profile_name = "demo"
    c.driver = types.SimpleNamespace(current_url="https://www.linkedin.com/search/results/people/")
    monkeypatch.setattr(c, "_click_send_without_note", lambda: False)
    monkeypatch.setattr(c, "_check_limit_warning", lambda: False)
    monkeypatch.setattr(c, "_find_modal", lambda: None)

    assert c._handle_after_click("before") is False
    assert "send_modal_missing" in calls


def test_connector_captures_no_connect_links(monkeypatch):
    from linkedin_automation import auto_connector as ac
    calls = []
    monkeypatch.setattr(ac, "capture_failure", lambda d, label, prof=None, **k: calls.append(label))

    c = ac.LinkedInAutoConnector.__new__(ac.LinkedInAutoConnector)
    c.profile_name = "demo"
    c.driver = types.SimpleNamespace(current_url="x")
    monkeypatch.setattr(c, "_scroll_page", lambda: None)
    monkeypatch.setattr(c, "_collect_page_targets", lambda: ([], None, "none"))

    assert c.process_page() == 0
    assert "no_connect_links" in calls


def test_poster_captures_each_step(monkeypatch):
    from linkedin_automation import poster as lp
    monkeypatch.setattr(lp.hb, "human_sleep", lambda *a, **k: None)

    def run(open_ok, type_ok, submit_ok, readback_ok=True):
        calls = []
        monkeypatch.setattr(lp, "capture_failure", lambda d, label, prof=None, **k: calls.append(label))
        p = lp.LinkedInPoster.__new__(lp.LinkedInPoster)
        p.profile_name = "demo"
        p.driver = object()
        monkeypatch.setattr(p, "_open_post_modal", lambda: open_ok)
        monkeypatch.setattr(p, "_type_post_content", lambda t: type_ok)
        monkeypatch.setattr(p, "verify_composed_text", lambda t: readback_ok)
        monkeypatch.setattr(p, "_click_post_button", lambda: submit_ok)
        return p.create_post("hi"), calls

    assert run(False, True, True) == (False, ["post_modal_failed"])
    assert run(True, False, True) == (False, ["post_typing_failed"])
    assert run(True, True, False) == (False, ["post_submit_failed"])
    assert run(True, True, True) == (True, [])

    # Phase 0: the read-back guard is a fourth failure point, and it must abort
    # BEFORE the Post button is ever clicked — an unverified publish is
    # irreversible.
    clicked = []
    monkeypatch.setattr(lp, "capture_failure", lambda d, label, prof=None, **k: None)
    p = lp.LinkedInPoster.__new__(lp.LinkedInPoster)
    p.profile_name = "demo"
    p.driver = object()
    monkeypatch.setattr(p, "_open_post_modal", lambda: True)
    monkeypatch.setattr(p, "_type_post_content", lambda t: True)
    monkeypatch.setattr(p, "verify_composed_text", lambda t: False)
    monkeypatch.setattr(p, "_click_post_button", lambda: clicked.append(True) or True)
    assert p.create_post("hi") is False
    assert clicked == [], "Post was clicked despite a read-back mismatch"

    assert run(True, True, True, readback_ok=False) == (
        False, ["post_readback_mismatch"])


def test_comment_poster_captures_box_not_found(monkeypatch):
    from linkedin_automation import comment_poster as plc
    calls = []
    monkeypatch.setattr(plc, "capture_failure", lambda d, label, prof=None, **k: calls.append(label))

    p = plc.LinkedInCommentPoster.__new__(plc.LinkedInCommentPoster)
    p.profile_name = "demo"
    p.driver = object()
    p.logger = logging.getLogger("test_comment_poster")
    monkeypatch.setattr(p, "open_comment_box", lambda: None)   # box not found

    assert p.post_comment("hello world") is False
    assert calls == ["comment_box_not_found"]
