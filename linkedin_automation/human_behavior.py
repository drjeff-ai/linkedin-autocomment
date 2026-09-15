"""
Human-like behavior simulation for Selenium.
Adapted from Playwright async version for use with Selenium WebDriver.
Bezier-curve mouse movement, natural typing, scrolling, reading simulation.
"""

import random
import time
import logging
from typing import Tuple, List

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

logger = logging.getLogger(__name__)

# ─── Config Defaults ──────────────────────────────────────────────────────────

VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080

ACTION_DELAY_MIN = 0.5
ACTION_DELAY_MAX = 1.5

TYPING_MIN_DELAY = 0.04
TYPING_MAX_DELAY = 0.12
TYPING_PAUSE_CHANCE = 0.08
TYPING_PAUSE_MIN = 0.3
TYPING_PAUSE_MAX = 0.8

SCROLL_PIXELS_MIN = 200
SCROLL_PIXELS_MAX = 600
SCROLL_DELAY_MIN = 0.8
SCROLL_DELAY_MAX = 2.0

READ_TIME_MIN = 1.5
READ_TIME_MAX = 4.0

# Session-shape / break defaults. BREAK_FREQUENCY is the modulo used by
# should_take_break; POSTS_PER_BREAK_* feed random_break_threshold so the
# work-then-pause rhythm is re-rolled each cycle (never a constant cadence).
BREAK_DURATION_MIN = 8.0
BREAK_DURATION_MAX = 25.0
BREAK_FREQUENCY = 5
POSTS_PER_BREAK_MIN = 3
POSTS_PER_BREAK_MAX = 7

# Track last known mouse position
_last_mouse_pos = None


# ─── Configurable behavior (profile_config.json "behavior" section) ───────────

def configure_behavior(behavior: dict = None) -> dict:
    """Apply tunable timing ranges from a profile config "behavior" section.

    Mutates the module-level timing globals so every human_* helper picks up the
    configured ranges without threading parameters through each call site.
    Missing keys keep their built-in human-like defaults; safe to call with
    None/{} (no-op). Returns the effective settings as a dict (handy for tests
    and logging).
    """
    global TYPING_MIN_DELAY, TYPING_MAX_DELAY, TYPING_PAUSE_CHANCE
    global READ_TIME_MIN, READ_TIME_MAX, ACTION_DELAY_MIN, ACTION_DELAY_MAX
    global SCROLL_PIXELS_MIN, SCROLL_PIXELS_MAX, SCROLL_DELAY_MIN, SCROLL_DELAY_MAX
    global BREAK_DURATION_MIN, BREAK_DURATION_MAX, BREAK_FREQUENCY
    global POSTS_PER_BREAK_MIN, POSTS_PER_BREAK_MAX

    behavior = behavior or {}

    def _range(key, lo_default, hi_default):
        val = behavior.get(key)
        if isinstance(val, (list, tuple)) and len(val) == 2:
            lo, hi = float(val[0]), float(val[1])
            if lo > hi:
                lo, hi = hi, lo
            return lo, hi
        return lo_default, hi_default

    TYPING_MIN_DELAY, TYPING_MAX_DELAY = _range(
        "typing_speed_range", TYPING_MIN_DELAY, TYPING_MAX_DELAY)
    READ_TIME_MIN, READ_TIME_MAX = _range(
        "reading_time_range", READ_TIME_MIN, READ_TIME_MAX)
    ACTION_DELAY_MIN, ACTION_DELAY_MAX = _range(
        "action_delay_range", ACTION_DELAY_MIN, ACTION_DELAY_MAX)
    SCROLL_PIXELS_MIN, SCROLL_PIXELS_MAX = (
        int(SCROLL_PIXELS_MIN), int(SCROLL_PIXELS_MAX))
    sp_lo, sp_hi = _range("scroll_pixels_range", SCROLL_PIXELS_MIN, SCROLL_PIXELS_MAX)
    SCROLL_PIXELS_MIN, SCROLL_PIXELS_MAX = int(sp_lo), int(sp_hi)
    SCROLL_DELAY_MIN, SCROLL_DELAY_MAX = _range(
        "scroll_delay_range", SCROLL_DELAY_MIN, SCROLL_DELAY_MAX)
    BREAK_DURATION_MIN, BREAK_DURATION_MAX = _range(
        "break_duration_range", BREAK_DURATION_MIN, BREAK_DURATION_MAX)
    pb_lo, pb_hi = _range("posts_per_break_range", POSTS_PER_BREAK_MIN, POSTS_PER_BREAK_MAX)
    POSTS_PER_BREAK_MIN, POSTS_PER_BREAK_MAX = int(pb_lo), int(pb_hi)

    if isinstance(behavior.get("typing_pause_chance"), (int, float)):
        TYPING_PAUSE_CHANCE = float(behavior["typing_pause_chance"])
    if isinstance(behavior.get("break_frequency"), int):
        BREAK_FREQUENCY = int(behavior["break_frequency"])

    return {
        "typing_speed_range": [TYPING_MIN_DELAY, TYPING_MAX_DELAY],
        "typing_pause_chance": TYPING_PAUSE_CHANCE,
        "reading_time_range": [READ_TIME_MIN, READ_TIME_MAX],
        "action_delay_range": [ACTION_DELAY_MIN, ACTION_DELAY_MAX],
        "scroll_pixels_range": [SCROLL_PIXELS_MIN, SCROLL_PIXELS_MAX],
        "scroll_delay_range": [SCROLL_DELAY_MIN, SCROLL_DELAY_MAX],
        "break_frequency": BREAK_FREQUENCY,
        "break_duration_range": [BREAK_DURATION_MIN, BREAK_DURATION_MAX],
        "posts_per_break_range": [POSTS_PER_BREAK_MIN, POSTS_PER_BREAK_MAX],
    }


# ─── Core Utilities ───────────────────────────────────────────────────────────

def random_delay(min_sec: float, max_sec: float) -> float:
    """Generate a random delay with slight bias toward the middle."""
    mid = (min_sec + max_sec) / 2
    return random.triangular(min_sec, max_sec, mid)


def human_sleep(min_sec: float = None, max_sec: float = None):
    """Sleep with human-like random duration."""
    min_sec = min_sec or ACTION_DELAY_MIN
    max_sec = max_sec or ACTION_DELAY_MAX
    time.sleep(random_delay(min_sec, max_sec))


# ─── Bezier Curve Mouse Movement ─────────────────────────────────────────────

def generate_bezier_points(
    start: Tuple[int, int],
    end: Tuple[int, int],
    num_points: int = 20
) -> List[Tuple[int, int]]:
    """Generate points along a bezier curve for natural mouse movement."""
    ctrl1 = (
        start[0] + (end[0] - start[0]) * 0.3 + random.randint(-50, 50),
        start[1] + (end[1] - start[1]) * 0.3 + random.randint(-50, 50)
    )
    ctrl2 = (
        start[0] + (end[0] - start[0]) * 0.7 + random.randint(-30, 30),
        start[1] + (end[1] - start[1]) * 0.7 + random.randint(-30, 30)
    )

    points = []
    for i in range(num_points + 1):
        t = i / num_points
        x = (1-t)**3 * start[0] + 3*(1-t)**2*t * ctrl1[0] + 3*(1-t)*t**2 * ctrl2[0] + t**3 * end[0]
        y = (1-t)**3 * start[1] + 3*(1-t)**2*t * ctrl1[1] + 3*(1-t)*t**2 * ctrl2[1] + t**3 * end[1]
        points.append((int(x), int(y)))

    return points


def human_mouse_move(driver: WebDriver, target_x: int, target_y: int):
    """Move mouse along a natural curved path to target coordinates."""
    global _last_mouse_pos

    if _last_mouse_pos is None:
        _last_mouse_pos = (VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)

    current = _last_mouse_pos
    num_points = random.randint(12, 25)
    points = generate_bezier_points(current, (target_x, target_y), num_points)

    actions = ActionChains(driver)

    # Move through each point with micro-delays
    prev = current
    for point in points:
        dx = point[0] - prev[0]
        dy = point[1] - prev[1]
        actions.move_by_offset(dx, dy)
        actions.pause(random_delay(0.005, 0.02))
        prev = point

    try:
        actions.perform()
    except Exception as e:
        logger.debug(f"Mouse move error (non-critical): {e}")

    _last_mouse_pos = (target_x, target_y)


def human_move_to_element(driver: WebDriver, element: WebElement):
    """Move mouse to an element using natural bezier curve path."""
    global _last_mouse_pos

    try:
        rect = driver.execute_script("""
            var r = arguments[0].getBoundingClientRect();
            return {x: r.x, y: r.y, width: r.width, height: r.height};
        """, element)

        # Aim for a random point within the element (not dead center)
        target_x = int(rect['x'] + rect['width'] * random.uniform(0.25, 0.75))
        target_y = int(rect['y'] + rect['height'] * random.uniform(0.25, 0.75))

        if _last_mouse_pos is None:
            _last_mouse_pos = (VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)

        # Use ActionChains move_to_element with offset for the final position

        actions = ActionChains(driver)
        # Move to element first (resets chain), then we do fine movement
        actions.move_to_element_with_offset(
            element,
            int(rect['width'] * random.uniform(0.25, 0.75) - rect['width'] / 2),
            int(rect['height'] * random.uniform(0.25, 0.75) - rect['height'] / 2)
        )
        actions.pause(random_delay(0.01, 0.05))
        actions.perform()

        _last_mouse_pos = (target_x, target_y)

    except Exception as e:
        logger.debug(f"Move to element fallback: {e}")
        # Fallback: simple move
        ActionChains(driver).move_to_element(element).perform()
        try:
            rect = element.rect
            _last_mouse_pos = (rect['x'] + rect['width'] // 2, rect['y'] + rect['height'] // 2)
        except Exception:
            logger.debug("Failed to update last mouse position from element rect", exc_info=True)


def random_mouse_drift(driver: WebDriver):
    """Small random mouse movement — simulates natural hand micro-movements."""
    global _last_mouse_pos

    if _last_mouse_pos is None:
        _last_mouse_pos = (VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)

    drift_x = _last_mouse_pos[0] + random.randint(-40, 40)
    drift_y = _last_mouse_pos[1] + random.randint(-25, 25)

    drift_x = max(50, min(VIEWPORT_WIDTH - 50, drift_x))
    drift_y = max(50, min(VIEWPORT_HEIGHT - 50, drift_y))

    actions = ActionChains(driver)
    dx = drift_x - _last_mouse_pos[0]
    dy = drift_y - _last_mouse_pos[1]

    # Small drift in 3-5 micro-steps
    steps = random.randint(3, 5)
    for i in range(steps):
        actions.move_by_offset(dx // steps, dy // steps)
        actions.pause(random_delay(0.01, 0.03))

    try:
        actions.perform()
    except Exception:
        logger.debug("Mouse drift perform failed (non-critical)", exc_info=True)

    _last_mouse_pos = (drift_x, drift_y)


# ─── Human Click ──────────────────────────────────────────────────────────────

def human_click(driver: WebDriver, element: WebElement):
    """Click an element with human-like mouse movement first."""
    # Scroll into view
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
        element
    )
    human_sleep(0.3, 0.6)

    # Move to element naturally
    human_move_to_element(driver, element)
    human_sleep(0.1, 0.3)

    # Click
    try:
        element.click()
    except Exception:
        # Fallback to JS click if regular click fails
        driver.execute_script("arguments[0].click();", element)

    human_sleep(0.2, 0.5)


def human_click_js(driver: WebDriver, element: WebElement):
    """Click via JavaScript after human-like movement (for stubborn elements)."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
        element
    )
    human_sleep(0.3, 0.6)
    human_move_to_element(driver, element)
    human_sleep(0.1, 0.25)
    driver.execute_script("arguments[0].click();", element)
    human_sleep(0.2, 0.5)


# ─── Human Typing ─────────────────────────────────────────────────────────────

def type_like_human(driver: WebDriver, element: WebElement, text: str):
    """Type text with human-like timing variations."""
    # Move to and click the input
    human_move_to_element(driver, element)
    human_sleep(0.1, 0.3)
    element.click()
    human_sleep(0.3, 0.7)

    for i, char in enumerate(text):
        delay = random_delay(TYPING_MIN_DELAY, TYPING_MAX_DELAY)

        # Occasional longer pauses (thinking)
        if random.random() < TYPING_PAUSE_CHANCE:
            delay += random_delay(TYPING_PAUSE_MIN, TYPING_PAUSE_MAX)
            # Sometimes drift the mouse while "thinking"
            if random.random() < 0.3:
                random_mouse_drift(driver)

        element.send_keys(char)
        time.sleep(delay)

        # Occasional mouse drift while typing
        if i > 0 and i % random.randint(10, 18) == 0 and random.random() < 0.3:
            random_mouse_drift(driver)


def type_like_human_keys(driver: WebDriver, text: str):
    """Type text into the currently-focused element with human-like timing.

    Like type_like_human but sends keystrokes via ActionChains to whatever element
    has focus — for editors focused via JS (e.g. shadow-DOM) where there is no
    Selenium WebElement to target.
    """
    for char in text:
        delay = random_delay(TYPING_MIN_DELAY, TYPING_MAX_DELAY)
        if random.random() < TYPING_PAUSE_CHANCE:
            delay += random_delay(TYPING_PAUSE_MIN, TYPING_PAUSE_MAX)
        ActionChains(driver).send_keys(char).perform()
        time.sleep(delay)


# ─── Human Scrolling ─────────────────────────────────────────────────────────

def human_scroll(driver: WebDriver, direction: str = "down", pixels: int = None):
    """Scroll with human-like behavior — incremental with micro-pauses."""
    if pixels is None:
        pixels = random.randint(SCROLL_PIXELS_MIN, SCROLL_PIXELS_MAX)

    if direction == "up":
        pixels = -pixels

    # Scroll in increments
    increments = random.randint(3, 6)
    per_increment = pixels // increments

    for i in range(increments):
        driver.execute_script(f"window.scrollBy(0, {per_increment});")
        time.sleep(random_delay(0.05, 0.15))

        # Occasional mouse drift while scrolling
        if random.random() < 0.25:
            random_mouse_drift(driver)

    human_sleep(SCROLL_DELAY_MIN, SCROLL_DELAY_MAX)


def scroll_to_element(driver: WebDriver, element: WebElement):
    """Scroll to bring an element into view with human-like smoothness."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
        element
    )
    human_sleep(0.5, 1.0)
    # Small drift after scrolling
    if random.random() < 0.4:
        random_mouse_drift(driver)


# ─── Reading / Breaks ─────────────────────────────────────────────────────────

def simulate_reading(driver: WebDriver = None, min_time: float = None, max_time: float = None):
    """Simulate time spent reading content."""
    min_time = min_time or READ_TIME_MIN
    max_time = max_time or READ_TIME_MAX

    total_time = random_delay(min_time, max_time)

    if driver:
        segments = random.randint(2, 4)
        time_per_segment = total_time / segments

        for _ in range(segments):
            time.sleep(time_per_segment)
            if random.random() < 0.5:
                random_mouse_drift(driver)
    else:
        time.sleep(total_time)


def simulate_reading_for_text(driver: WebDriver = None, text: str = "",
                              min_time: float = None, max_time: float = None):
    """Reading pause scaled to content length — longer posts take longer to read.

    A human dwells longer on a wall of text than on a one-liner. Scales the read
    window with the text length (~16 chars/sec reading speed) on top of the base
    reading-time range, with the added time capped so a huge post can't stall the
    run. Falls back to the base simulate_reading window for empty text.
    """
    base_min = min_time if min_time is not None else READ_TIME_MIN
    base_max = max_time if max_time is not None else READ_TIME_MAX
    length = len(text or "")
    extra = min(length / 16.0, 12.0)  # cap the length bonus at 12s
    simulate_reading(driver, base_min + extra * 0.4, base_max + extra)


def should_take_break(action_count: int, threshold: int = None) -> bool:
    """Determine if we should take a longer break between actions."""
    threshold = BREAK_FREQUENCY if threshold is None else threshold
    if action_count > 0 and action_count % threshold == 0:
        return random.random() < 0.35
    return False


def random_break_threshold(min_posts: int = None, max_posts: int = None) -> int:
    """Re-rollable count of actions before a longer break (e.g. randint 3-7).

    Call this after each break to vary how many posts get processed before the
    next pause, so the work-then-pause rhythm is never identical run to run.
    """
    min_posts = POSTS_PER_BREAK_MIN if min_posts is None else min_posts
    max_posts = POSTS_PER_BREAK_MAX if max_posts is None else max_posts
    if min_posts > max_posts:
        min_posts, max_posts = max_posts, min_posts
    return random.randint(min_posts, max_posts)


def take_break(driver: WebDriver = None):
    """Take a longer break between action clusters."""
    break_time = random_delay(BREAK_DURATION_MIN, BREAK_DURATION_MAX)
    logger.info(f"Taking a break ({break_time:.0f}s)...")

    if driver:
        segments = int(break_time // 3)
        for i in range(segments):
            time.sleep(random_delay(2, 4))
            if random.random() < 0.5:
                random_mouse_drift(driver)
        remaining = break_time - (segments * 3)
        if remaining > 0:
            time.sleep(remaining)
    else:
        time.sleep(break_time)


# ─── Utility ──────────────────────────────────────────────────────────────────

def jitter_int(value: int, pct: float = 0.15) -> int:
    """Add random jitter to an integer value (e.g., delays, pixel counts)."""
    delta = int(value * pct)
    return value + random.randint(-delta, delta)


def random_viewport_point() -> Tuple[int, int]:
    """Get a random point within the viewport (for idle movement)."""
    return (
        random.randint(100, VIEWPORT_WIDTH - 100),
        random.randint(100, VIEWPORT_HEIGHT - 100)
    )
