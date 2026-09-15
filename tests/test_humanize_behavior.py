"""Tests for the humanize-fixes work: every LinkedIn-interacting script must
route browser interactions through human_behavior_selenium (hb) instead of raw
Selenium (.click(), send_keys dumps, uniform scrollBy / time.sleep).

No browser and no real sleeps: the driver/elements are MagicMocks and the hb
primitives are monkeypatched to recorders, so each test asserts the human_*
helper was called (and the raw method was NOT) without any wall-clock cost.
"""

import logging
from unittest.mock import MagicMock

import pytest

from linkedin_automation import human_behavior as hb
from linkedin_automation import profile_manager as pm
from linkedin_automation.post_finder import LinkedInScraper


# ─── Restore hb's tunable globals after each test (configure_behavior mutates) ─

_HB_GLOBALS = [
    "TYPING_MIN_DELAY", "TYPING_MAX_DELAY", "TYPING_PAUSE_CHANCE",
    "READ_TIME_MIN", "READ_TIME_MAX", "ACTION_DELAY_MIN", "ACTION_DELAY_MAX",
    "SCROLL_PIXELS_MIN", "SCROLL_PIXELS_MAX", "SCROLL_DELAY_MIN", "SCROLL_DELAY_MAX",
    "BREAK_DURATION_MIN", "BREAK_DURATION_MAX", "BREAK_FREQUENCY",
    "POSTS_PER_BREAK_MIN", "POSTS_PER_BREAK_MAX",
]


@pytest.fixture(autouse=True)
def _restore_hb_defaults():
    saved = {name: getattr(hb, name) for name in _HB_GLOBALS}
    yield
    for name, value in saved.items():
        setattr(hb, name, value)


@pytest.fixture
def no_sleep(monkeypatch):
    """Neutralize the real timing helpers so tests never actually sleep."""
    monkeypatch.setattr(hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(hb, "simulate_reading", lambda *a, **k: None)
    monkeypatch.setattr(hb, "simulate_reading_for_text", lambda *a, **k: None)
    monkeypatch.setattr(hb, "random_mouse_drift", lambda *a, **k: None)
    monkeypatch.setattr(hb, "take_break", lambda *a, **k: None)


def _log():
    return logging.getLogger("test_humanize")


# ─── hb.configure_behavior ────────────────────────────────────────────────────

def test_configure_behavior_applies_ranges():
    out = hb.configure_behavior({
        "typing_speed_range": [0.01, 0.02],
        "reading_time_range": [5, 10],
        "scroll_pixels_range": [100, 300],
        "posts_per_break_range": [2, 4],
        "break_duration_range": [3, 6],
        "typing_pause_chance": 0.5,
        "break_frequency": 9,
    })
    assert (hb.TYPING_MIN_DELAY, hb.TYPING_MAX_DELAY) == (0.01, 0.02)
    assert (hb.READ_TIME_MIN, hb.READ_TIME_MAX) == (5, 10)
    assert (hb.SCROLL_PIXELS_MIN, hb.SCROLL_PIXELS_MAX) == (100, 300)
    assert (hb.POSTS_PER_BREAK_MIN, hb.POSTS_PER_BREAK_MAX) == (2, 4)
    assert (hb.BREAK_DURATION_MIN, hb.BREAK_DURATION_MAX) == (3, 6)
    assert hb.TYPING_PAUSE_CHANCE == 0.5
    assert hb.BREAK_FREQUENCY == 9
    # Returned effective settings mirror the applied globals.
    assert out["typing_speed_range"] == [0.01, 0.02]
    assert out["posts_per_break_range"] == [2, 4]


def test_configure_behavior_none_and_missing_keys_keep_defaults():
    base_typing = (hb.TYPING_MIN_DELAY, hb.TYPING_MAX_DELAY)
    hb.configure_behavior(None)
    assert (hb.TYPING_MIN_DELAY, hb.TYPING_MAX_DELAY) == base_typing
    # Partial config only touches provided keys.
    hb.configure_behavior({"reading_time_range": [7, 8]})
    assert (hb.TYPING_MIN_DELAY, hb.TYPING_MAX_DELAY) == base_typing
    assert (hb.READ_TIME_MIN, hb.READ_TIME_MAX) == (7, 8)


def test_configure_behavior_swaps_reversed_range():
    hb.configure_behavior({"reading_time_range": [9, 2]})
    assert hb.READ_TIME_MIN == 2 and hb.READ_TIME_MAX == 9


# ─── hb.random_break_threshold (re-rollable session shape) ─────────────────────

def test_random_break_threshold_within_configured_range():
    hb.configure_behavior({"posts_per_break_range": [3, 7]})
    values = {hb.random_break_threshold() for _ in range(100)}
    assert values  # non-empty
    assert all(3 <= v <= 7 for v in values)
    # Varies (not a constant) given a non-degenerate range.
    assert len(values) > 1


# ─── hb.simulate_reading_for_text scales with content length ──────────────────

def test_reading_time_scales_with_text_length(monkeypatch):
    captured = []
    monkeypatch.setattr(
        hb, "simulate_reading",
        lambda driver=None, min_time=None, max_time=None: captured.append((min_time, max_time)),
    )
    hb.simulate_reading_for_text(None, "short")
    hb.simulate_reading_for_text(None, "x" * 1500)
    assert captured[1][1] > captured[0][1]  # longer post -> longer read window


# ─── linkedin_ai_post_finder: scrolling, clicks ───────────────────────────────

def test_scroll_feed_uses_human_scroll_not_raw_scrollby(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(hb, "human_scroll", lambda driver, **kw: calls.append(kw))
    driver = MagicMock()
    driver.find_element.side_effect = Exception("no inner container")
    LinkedInScraper(driver, _log()).scroll_feed()
    assert calls, "human_scroll should be used for feed scrolling"
    # No uniform window.scrollBy anywhere.
    for call in driver.execute_script.call_args_list:
        assert "scrollBy" not in str(call)


def test_clipboard_extraction_uses_human_click(monkeypatch, no_sleep):
    clicked = []
    monkeypatch.setattr(hb, "human_click", lambda driver, el: clicked.append(el))

    menu_btn = MagicMock(name="menu_btn")
    copy_item = MagicMock(name="copy_item")
    copy_item.text = "Copy link to post"
    element = MagicMock()
    element.find_element.return_value = menu_btn

    driver = MagicMock()
    driver.find_elements.return_value = [copy_item]
    driver.execute_script.return_value = (
        "https://www.linkedin.com/feed/update/urn:li:activity:123/"
    )

    url = LinkedInScraper(driver, _log()).extract_url_via_clipboard(element)

    assert menu_btn in clicked and copy_item in clicked
    assert url == "https://www.linkedin.com/feed/update/urn:li:activity:123/"
    menu_btn.click.assert_not_called()
    copy_item.click.assert_not_called()
    # The copy item is no longer clicked via a raw JS .click().
    for call in driver.execute_script.call_args_list:
        assert "arguments[0].click()" not in str(call)


def test_see_more_expander_uses_human_click(monkeypatch, no_sleep):
    clicked = []
    monkeypatch.setattr(hb, "human_click", lambda driver, el: clicked.append(el))

    see_more = MagicMock(name="see_more")
    text_el = MagicMock(name="text_el")
    text_el.text = "x" * 60
    element = MagicMock()
    element.find_element.side_effect = [see_more, text_el]

    text = LinkedInScraper(MagicMock(), _log())._extract_text(element)
    assert text == "x" * 60
    assert see_more in clicked
    see_more.click.assert_not_called()


# ─── post_linkedin_comments: like, comment box, typing ────────────────────────

@pytest.fixture
def comment_poster(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "default")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file", lambda n: str(tmp_path / "progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n: str(tmp_path))
    monkeypatch.setattr(pm, "get_profile_config", lambda n=None: {"behavior": {}})
    from linkedin_automation import comment_poster as plc
    return plc.LinkedInCommentPoster(profile_name="default")


def test_like_post_uses_human_click(monkeypatch, no_sleep, comment_poster):
    clicked = []
    monkeypatch.setattr(hb, "human_click", lambda driver, el: clicked.append(el))
    like_btn = MagicMock(name="like_btn")
    comment_poster.driver = MagicMock()
    comment_poster.wait = MagicMock()
    comment_poster.wait.until.return_value = like_btn

    assert comment_poster.like_post() is True
    assert like_btn in clicked
    like_btn.click.assert_not_called()
    # No raw JS .click() on the like button.
    for call in comment_poster.driver.execute_script.call_args_list:
        assert "click" not in str(call).lower()


def test_open_comment_box_uses_human_click_and_scroll(monkeypatch, no_sleep, comment_poster):
    clicked, scrolled = [], []
    monkeypatch.setattr(hb, "human_click", lambda driver, el: clicked.append(el))
    monkeypatch.setattr(hb, "scroll_to_element", lambda driver, el: scrolled.append(el))

    btn = MagicMock(name="comment_btn")
    btn.is_displayed.return_value = True
    inp = MagicMock(name="comment_input")
    inp.is_displayed.return_value = True

    def find_elements(by, sel):
        if "artdeco-button__text" in sel:
            return []          # Method 1 (span text) misses
        if "aria-label" in sel:
            return [btn]       # Method 2 (aria-label) finds the button
        return [inp]           # comment-input selectors

    driver = MagicMock()
    driver.find_elements.side_effect = find_elements
    comment_poster.driver = driver

    assert comment_poster.open_comment_box() is inp
    assert btn in clicked and btn in scrolled
    btn.click.assert_not_called()


def test_post_comment_types_like_human_not_send_keys(monkeypatch, no_sleep, comment_poster):
    typed, clicked = [], []
    monkeypatch.setattr(hb, "type_like_human", lambda driver, el, text: typed.append(text))
    monkeypatch.setattr(hb, "human_click", lambda driver, el: clicked.append(el))
    monkeypatch.setattr(hb, "scroll_to_element", lambda driver, el: None)

    inp = MagicMock(name="comment_input")
    monkeypatch.setattr(comment_poster, "open_comment_box", lambda: inp)
    # First posting method succeeds so we don't exercise the button fallbacks.
    monkeypatch.setattr(comment_poster, "post_comment_method1", lambda ci, ct: True)
    comment_poster.driver = MagicMock()

    assert comment_poster.post_comment("hello there world") is True
    assert typed == ["hello there world"]
    assert inp in clicked
    inp.send_keys.assert_not_called()  # never an instant dump


# ─── linkedin_auto_connector: fallback dismiss clicks ─────────────────────────

def test_connector_close_modal_uses_human_click(monkeypatch, no_sleep):
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda n=None: {"connector": {}, "behavior": {}})
    from linkedin_automation import auto_connector as ac

    clicked = []
    monkeypatch.setattr(hb, "human_click", lambda driver, el: clicked.append(el))

    conn = ac.LinkedInAutoConnector(profile_name="x")
    btn = MagicMock(name="dismiss_btn")
    btn.is_displayed.return_value = True
    driver = MagicMock()
    driver.find_element.return_value = btn
    conn.driver = driver

    conn._close_modal()
    assert btn in clicked
    btn.click.assert_not_called()


# ─── linkedin_poster: editor focus + typing ───────────────────────────────────

def test_poster_type_content_uses_human_click_and_type(monkeypatch, no_sleep):
    monkeypatch.setattr(pm, "get_profile_config", lambda n=None: {"behavior": {}})
    from linkedin_automation import poster as lp

    typed, clicked = [], []
    monkeypatch.setattr(hb, "human_click", lambda driver, el: clicked.append(el))
    monkeypatch.setattr(hb, "type_like_human", lambda driver, el, text: typed.append(text))

    poster = lp.LinkedInPoster(profile_name="x")
    editor = MagicMock(name="editor")
    editor.is_displayed.return_value = True
    poster.driver = MagicMock()
    poster.wait = MagicMock()
    poster.wait.until.return_value = editor

    assert poster._type_post_content("my new post") is True
    assert editor in clicked
    assert typed == ["my new post"]
    editor.click.assert_not_called()


# ─── Config: default template ships a tunable behavior section ────────────────

def test_default_config_has_behavior_section():
    cfg = pm.load_default_config()
    behavior = cfg.get("behavior")
    assert isinstance(behavior, dict)
    for key in ("typing_speed_range", "reading_time_range", "break_frequency",
                "posts_per_break_range", "break_duration_range"):
        assert key in behavior
