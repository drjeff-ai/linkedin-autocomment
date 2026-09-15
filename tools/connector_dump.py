"""Dump a LinkedIn people-search results page so we can find current
connector selectors. Pass the same search URL you use with the connector."""

import time
import argparse

# Make `import linkedin_automation` resolve when run as `python tools/connector_dump.py`.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from selenium.webdriver.common.by import By
from linkedin_automation import profile_manager as pm


def main():
    """Open a people-search URL and dump its HTML for connector selector work."""
    parser = argparse.ArgumentParser()
    parser.add_argument("search_url", help="LinkedIn people-search results URL")
    parser.add_argument("--profile", default=None)
    args = parser.parse_args()

    driver, profile = pm.create_driver(args.profile)

    try:
        print("Navigating to search URL...")
        driver.get(args.search_url)
        time.sleep(6)

        # Scroll a bit to trigger lazy loading of result cards
        for i in range(3):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(2)

        html = driver.page_source
        with open("connector_dump.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nSaved {len(html):,} chars to connector_dump.html")

        print("\n=== RESULT CARD CANDIDATES ===")
        card_checks = [
            # Current DOM (verified June 30 2026):
            "div[data-view-name='people-search-result']",
            "[data-view-name='people-search-result']",
            "a[data-view-name='search-result-lockup-title']",
            "div[data-view-name='relationship-building-button']",
            # Legacy fallbacks:
            "li.reusable-search__result-container",
            "div.entity-result",
            "div[componentkey]",
            "div[role='listitem']",
            "a[href*='/in/']",
        ]
        for sel in card_checks:
            try:
                n = len(driver.find_elements(By.CSS_SELECTOR, sel))
                mark = "+" if n else " "
                print(f"  {mark} {n:4d}  {sel}")
            except Exception:
                print(f"    err  {sel}")

        print("\n=== CONNECT BUTTON CANDIDATES ===")
        btn_checks = [
            # Current DOM: Connect is an icon <a> (aria-label 'Invite ... to
            # connect', href /preload/search-custom-invite/), NOT a button.
            "a[aria-label^='Invite'][href*='/preload/search-custom-invite/']",
            "div[data-view-name='edge-creation-connect-action'] a",
            "a[href*='/preload/search-custom-invite/']",
            "div[data-view-name='edge-creation-connect-action']",
            # Legacy fallbacks:
            "button[aria-label*='Connect']",
            "button[aria-label*='Invite']",
        ]
        for sel in btn_checks:
            try:
                elems = driver.find_elements(By.CSS_SELECTOR, sel)
                # Count how many actually say "Connect"
                connect_count = sum(
                    1 for e in elems
                    if "connect" in (e.text or "").lower()
                    or "connect" in (e.get_attribute("aria-label") or "").lower()
                )
                n = len(elems)
                mark = "+" if connect_count else " "
                print(f"  {mark} {n:4d} total, {connect_count:3d} say 'Connect'  {sel}")
            except Exception:
                print(f"    err  {sel}")

        print("\n=== PAGINATION CANDIDATES ===")
        page_checks = [
            "button[data-testid='pagination-controls-next-button-visible']",
            "button[data-testid^='pagination-controls-next']",
            "button[data-testid^='pagination-indicator-']",
            "button[aria-current='true'][data-testid^='pagination-indicator-']",
            # Legacy fallbacks:
            "button[aria-label='Next']",
            "button.artdeco-pagination__button--next",
        ]
        for sel in page_checks:
            try:
                n = len(driver.find_elements(By.CSS_SELECTOR, sel))
                mark = "+" if n else " "
                print(f"  {mark} {n:4d}  {sel}")
            except Exception:
                print(f"    err  {sel}")

        print("\n=== ALL BUTTON aria-labels (first 30) ===")
        buttons = driver.find_elements(By.CSS_SELECTOR, "button")
        seen = set()
        shown = 0
        for b in buttons:
            label = b.get_attribute("aria-label") or ""
            text = (b.text or "").strip()
            key = label or text
            if key and key not in seen:
                seen.add(key)
                print(f"  aria-label={label!r:45s} text={text!r}")
                shown += 1
                if shown >= 30:
                    break

        print("\n=== DATA ATTRIBUTES on likely card elements ===")
        # Find elements containing a /in/ profile link and dump their data-* attrs
        profile_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")[:3]
        for i, link in enumerate(profile_links):
            print(f"\n  Profile link {i + 1}: {link.get_attribute('href')[:60]}")
            # Walk up to find the card container
            container = driver.execute_script("""
                let el = arguments[0];
                for (let i = 0; i < 6; i++) {
                    if (!el.parentElement) break;
                    el = el.parentElement;
                }
                let attrs = {};
                for (let a of el.attributes) attrs[a.name] = a.value;
                return JSON.stringify(attrs);
            """, link)
            print(f"    container attrs (6 levels up): {container}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
