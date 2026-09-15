"""Dump LinkedIn feed HTML so we can find current selectors."""

import time
import argparse

# Make `import linkedin_automation` resolve when run as `python tools/feed_dump.py`.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from linkedin_automation.profile_manager import create_driver


def main():
    """Scroll the feed, dump its HTML, and probe current post selectors."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    parser.add_argument("--scroll", type=int, default=3, help="Number of scrolls")
    args = parser.parse_args()

    driver, profile = create_driver(args.profile)

    try:
        print("Navigating to LinkedIn feed...")
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(5)

        for i in range(args.scroll):
            driver.execute_script("window.scrollBy(0, 1000);")
            time.sleep(2)
            print(f"Scrolled {i + 1}/{args.scroll}")

        html = driver.page_source

        with open("feed_dump.html", "w", encoding="utf-8") as f:
            f.write(html)

        print(f"\nSaved {len(html):,} chars to feed_dump.html")
        print("\nQuick selector check:")

        checks = [
            ("div[data-id*='urn:li:activity']", "activity data-id"),
            ("div.feed-shared-update-v2", "feed-shared-update-v2"),
            ("div.occludable-update", "occludable-update"),
            ("div.scaffold-finite-scroll__content", "scaffold-finite-scroll"),
            ("article", "article tags"),
            ("div[data-urn]", "data-urn divs"),
            ("div[class*='update']", "any *update* class"),
            ("div[class*='feed']", "any *feed* class"),
            ("div[class*='post']", "any *post* class"),
            ("div[class*='occludable']", "any *occludable* class"),
            ("span[dir='ltr']", "ltr text spans"),
            ("a[href*='/in/']", "profile links"),
        ]

        from selenium.webdriver.common.by import By
        for selector, label in checks:
            try:
                elems = driver.find_elements(By.CSS_SELECTOR, selector)
                count = len(elems)
                marker = "+" if count > 0 else " "
                print(f"  {marker} {count:3d}  {label:30s}  ({selector})")
            except Exception:
                print(f"    err  {label:30s}  ({selector})")

        # ── URN debug: inspect the first 3 feed-full-update posts ───────────
        print("\n" + "=" * 60)
        print("URN DEBUG — first 3 feed-full-update elements")
        print("=" * 60)

        posts = driver.find_elements(
            By.CSS_SELECTOR, "div[data-view-name='feed-full-update']"
        )
        print(f"feed-full-update elements found: {len(posts)}")

        # JS: collect tag + attributes for the element and its descendants
        # (up to `depth` levels), so we can see where any URN lives.
        walk_js = """
        const root = arguments[0], maxDepth = arguments[1];
        const out = [];
        function attrs(el){const o={}; for (const a of el.attributes) o[a.name]=a.value; return o;}
        (function rec(el, d){
            out.push({tag: el.tagName.toLowerCase(), depth: d, attrs: attrs(el)});
            if (d < maxDepth) for (const c of el.children) rec(c, d+1);
        })(root, 0);
        return out;
        """

        import re as _re
        urn_re = _re.compile(r'urn:li:[a-zA-Z]+:[0-9]+')

        for idx, post in enumerate(posts[:3]):
            print(f"\n----- POST {idx + 1} -----")

            outer = ""
            try:
                outer = post.get_attribute("outerHTML") or ""
                print(f"outerHTML length: {len(outer)}")
                print("outerHTML[:300]: " + outer[:300].replace("\n", " "))
            except Exception as e:
                print(f"  outerHTML error: {e}")

            # 1. ALL urn:li:* occurrences anywhere in the post's full HTML
            try:
                urns = sorted(set(urn_re.findall(outer)))
                print(f"\nurn:li:* in full outerHTML ({len(urns)} unique): {urns[:10]}")
            except Exception as e:
                print(f"  urn regex error: {e}")

            # 2. deep walk for any attribute whose NAME or VALUE contains 'urn'
            try:
                nodes = driver.execute_script(walk_js, post, 8)
                print("attributes containing 'urn' (8-level walk):")
                urn_hits = 0
                for n in nodes:
                    for k, v in n["attrs"].items():
                        if "urn" in k.lower() or "urn:" in (v or "").lower():
                            urn_hits += 1
                            print(f"  [{n['tag']} d{n['depth']}] {k} = {str(v)[:120]}")
                if not urn_hits:
                    print("  (none even at depth 8)")
            except Exception as e:
                print(f"  deep walk error: {e}")

            # 3. ALL anchor hrefs in the post (first 15)
            try:
                anchors = post.find_elements(By.CSS_SELECTOR, "a[href]")
                print(f"\nall anchor hrefs ({len(anchors)} total, first 15):")
                for a in anchors[:15]:
                    print(f"  {(a.get_attribute('href') or '')[:140]}")
            except Exception as e:
                print(f"  anchor scan error: {e}")

            # 4. componentkey values in the subtree (LinkedIn sometimes keys posts here)
            try:
                ck_nodes = driver.execute_script(walk_js, post, 4)
                cks = [n["attrs"]["componentkey"] for n in ck_nodes if "componentkey" in n["attrs"]]
                print(f"\ncomponentkey values (depth 4): {cks[:6]}")
            except Exception as e:
                print(f"  componentkey scan error: {e}")

        # ── Clickable elements: all <a>/<button> in the first 2 posts ───────
        print("\n" + "=" * 60)
        print("CLICKABLE ELEMENTS — <a> and <button> in first 2 posts")
        print("=" * 60)
        for idx, post in enumerate(posts[:2]):
            print(f"\n----- POST {idx + 1} clickables -----")
            try:
                clickables = post.find_elements(By.CSS_SELECTOR, "a, button")
                print(f"{len(clickables)} <a>/<button> elements:")
                for el in clickables:
                    try:
                        tag = el.tag_name
                        text = (el.text or "").replace("\n", " ").strip()[:50]
                        href = el.get_attribute("href") or ""
                        testid = el.get_attribute("data-testid") or ""
                        viewname = el.get_attribute("data-view-name") or ""
                        aria = el.get_attribute("aria-label") or ""
                        print(f"  [{tag:6s}] text={text!r:52s} href={href[:60]!r} "
                              f"testid={testid!r} view-name={viewname!r} aria={aria[:40]!r}")
                    except Exception as e:
                        print(f"  (element read error: {e})")
            except Exception as e:
                print(f"  clickable scan error: {e}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
