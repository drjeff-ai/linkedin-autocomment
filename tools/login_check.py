"""login_check.py — open a profile's Chrome session and report LinkedIn login status.

Manual utility. Launches Chrome with the profile's persistent ``user-data-dir``,
navigates to the LinkedIn feed, and reports whether you are logged in. If you are
not, it prints instructions and waits while you log in manually, then re-checks.
Your session persists, so subsequent scrape/post/connect runs reuse it without
prompting.

Usage:
    python tools/login_check.py --profile demo
    python tools/login_check.py                 # uses the default profile
    python tools/login_check.py --profile demo --no-wait   # report and exit

Exit codes:
    0  logged in
    1  error (no/unknown profile, browser failure)
    2  not logged in (login required)
"""

import argparse
import logging
import sys
import time

# Make `import linkedin_automation` resolve when run as `python tools/login_check.py`
# (project root is this file's grandparent directory).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from linkedin_automation import profile_manager as pm

logger = logging.getLogger(__name__)


def status_report(logged_in: bool, profile_name: str):
    """Return ``(human_message, exit_code)`` for a login-status result.

    Pure function (no browser), so it is unit-testable on its own.
    """
    name = profile_name or "default"
    if logged_in:
        return (
            f"✅ Profile '{name}' is logged in to LinkedIn. "
            f"Scrape / post / connect runs will reuse this session.",
            pm.EXIT_OK,
        )
    return (
        f"❌ Profile '{name}' is NOT logged in to LinkedIn.\n"
        f"   Log in manually in the Chrome window, then close it — your\n"
        f"   session will persist. Re-run to confirm:\n"
        f"   python tools/login_check.py --profile {name}",
        pm.EXIT_LOGIN_REQUIRED,
    )


def check_login(profile_name: str = None, wait_for_manual: bool = True) -> int:
    """Open the profile's Chrome session, report status, and return an exit code."""
    driver = None
    try:
        pm.auto_migrate_from_env()
        driver, _profile = pm.create_driver(profile_name)
        # URL-based detection (is_logged_in_on_page) after a single navigation —
        # avoids the false "not logged in" the old element wait produced.
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(4)
        logged_in = pm.is_logged_in_on_page(driver)
        message, code = status_report(logged_in, profile_name)
        print(message)

        if not logged_in and wait_for_manual:
            input("\nLog in in the browser, then press Enter to re-check (or quit)...")
            driver.get("https://www.linkedin.com/feed/")
            time.sleep(4)
            logged_in = pm.is_logged_in_on_page(driver)
            message, code = status_report(logged_in, profile_name)
            print(message)

        return code

    except ValueError as e:
        # Raised by create_driver for missing/unknown profile.
        print(f"❌ {e}")
        return pm.EXIT_ERROR
    except Exception as e:
        print(f"❌ Could not check login status: {e}")
        return pm.EXIT_ERROR
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                logger.debug("Driver already closed", exc_info=True)


def main(argv=None) -> int:
    """Parse args and run the login check. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Check LinkedIn login status for a Chrome-session profile"
    )
    parser.add_argument(
        "--profile", default=None,
        help="Profile name (uses the default profile if omitted)",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="Report status and exit; do not wait for manual login",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    return check_login(args.profile, wait_for_manual=not args.no_wait)


if __name__ == "__main__":
    sys.exit(main())
