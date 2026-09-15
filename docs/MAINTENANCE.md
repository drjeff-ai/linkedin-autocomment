# Maintenance: fixing selectors when LinkedIn changes its DOM

This is the one recurring maintenance chore for this project. Everything else is
stable; the scraper and connector break periodically because they read LinkedIn's
live HTML, and LinkedIn changes that HTML.

This runbook is the distilled, hard-won knowledge from several real repairs. Follow
it top to bottom when scraping or connecting stops working.

---

## 1. Why this happens

The app drives a real logged-in browser and locates posts, authors, buttons, and
search results by **CSS selectors**. LinkedIn periodically rotates its front-end:

- **Class names become opaque hashes** (e.g. a readable class one month is
  `._a1b2c3` the next). Anything keyed on a class name is fragile by design.
- **`data-view-name` attributes get dropped.** These used to be everywhere (one
  DOM dump matched `data-view-name` 238 times); a later rotation removed them from
  the feed entirely.

When that happens, selectors that matched yesterday match **zero** elements today.
This is **expected and recurring — not a bug in the code.** The fix is always the
same shape: find the new stable hook, add it, keep the old one as a fallback.

**The core insight that makes fixes fast:** rotations churn **class names** and
**`data-view-name`**. They almost never churn **`aria-label`**, **visible button
text**, **`data-testid`**, or **`href` patterns** — those are user-facing or
accessibility-required, so LinkedIn keeps them stable. **Always prefer those hooks.**

---

## 2. How to know it broke

### Symptoms

- The scraper finds **0 posts** ("feed did not load", empty Review Posts tab).
- The connector pages forward and **connects with nobody** (0 connectable people).
- Generated/Review tabs are empty because nothing upstream produced data.

### The diagnostic: run the selector health check

Feed selectors:

```bash
uv run python -m linkedin_automation.selector_health --profile <name>
```

Connector / people-search selectors (pass the same search URL you use with the
connector):

```bash
uv run python -m linkedin_automation.selector_health --profile <name> \
    --search-url "<people-search results URL>"
```

Optional: `--scrolls N` (feed scrolls before checking, default 3) and `--json`
(print the raw result).

### Reading the output

The check reports an overall status and per-selector match counts:

- **HEALTHY** — every critical selector matched > 0 elements. Trust it.
- **DEGRADED** — some non-critical selectors failed but the pipeline can still run.
- **BROKEN** — a critical selector matched 0 elements. Scraping/connecting is down.

On failure it writes three things:

- `data/<name>/selector_health.json` — the structured per-selector report.
- `selector_debug_dump.html` (repo root, git-ignored) — the first few post
  containers' HTML so you can eyeball the current DOM.
- `.dev/SELECTOR_FIX_NEEDED.md` — on BROKEN only: the list of failed selectors plus
  a ready-to-paste fix prompt. It never edits selectors itself.

> **Before you assume selectors broke:** a "0 posts" result can be **throttling**,
> not a selector mismatch (see Lesson 2). The dump tools below navigate once and
> print candidate counts, which distinguishes the two.

---

## 3. How to fix it

### Step 1 — capture the current DOM

Run the matching dump tool. Each opens your logged-in session, navigates **once**,
and saves the live HTML plus a printed probe of candidate selectors (match counts,
`aria-label`s, `data-*` attributes):

```bash
# Feed
uv run python tools/feed_dump.py --profile <name>
#   -> writes feed_dump.html (git-ignored) + prints a selector probe

# Connector / people search
uv run python tools/connector_dump.py "<people-search results URL>" --profile <name>
#   -> writes connector_dump.html (git-ignored) + prints card/connect/pagination counts
```

Both output files are git-ignored (`*_dump.html`) because they contain live feed
and member content.

### Step 2 — find the new hook (prefer durable attributes)

Open the dump HTML (or read the probe counts) and look for the **stable** hooks in
this order: `data-testid` → `aria-label` → `href` pattern → `role`. Only fall back
to class names if nothing else identifies the element.

These durable hooks have survived multiple rotations and are what the code targets
today:

| What | Durable hook | Notes |
|------|--------------|-------|
| Feed scroll container | `div[data-testid='mainFeed']` | Scope post lookups to this to exclude nav/side-rail. |
| A feed post | `div[data-testid='mainFeed'] div[role='listitem']` | `role='listitem']` is the only stable per-post wrapper since classes went hashed. |
| Post body text | `span[data-testid='expandable-text-box']` | |
| Post "…" control menu | `button[aria-label^='Open control menu']` | The label is `Open control menu for post by <Person Name>`. |
| **Post author name** | the control-menu `aria-label` above | The single hook present on **both** person and company posts — the actor link's visible text is empty and company posts have no `/in/` link. |
| Author profile URL | `a[href*='/in/']` / `a[href*='/company/']` | Used for the URL, not the name. |
| Connect / Invite (people search) | `a[aria-label^='Invite'][href*='/preload/search-custom-invite/']` | Icon-only link (see Lesson 1). |
| Invited person's name | the Connect link's `aria-label` | `Invite <Person Name> to connect` → parsed by `name_from_invite_label`. |
| Invited person's profile URL / dedup id | the link's `href` `vanityName` | `https://www.linkedin.com/in/<vanity>/` — the member's own invite link. |
| Search result card | `div[data-view-name='people-search-result']` | Kept as a fallback path; the link-first path above no longer needs it. |
| Pagination "next" | `button[data-testid='pagination-controls-next-button-visible']` | LinkedIn replaced the old "Next" with numbered pages + this `data-testid`. |
| "See more" text expander | `button[data-testid='expandable-text-button']` | Best-effort/optional. |
| Connect modal "Send" | button **text** `Send without a note` / `Send now` / `Send` | Inside a **shadow DOM** (see Lesson 6). |

### Step 3 — update the constants (which file, which constant)

After the restructure, selectors live as class constants near the top of two
modules:

**`linkedin_automation/post_finder.py`** (`LinkedInScraper`):

- `POST_SELECTORS` — feed post containers.
- `SCROLL_CONTAINER_SELECTORS` — the scrollable feed container.
- `TEXT_SELECTORS` — post body text.
- `AUTHOR_SELECTORS` — author profile/company links (for the URL).
- `CONTROL_MENU_SELECTOR` + `CONTROL_MENU_AUTHOR_PREFIX` — the "…" menu button and
  the aria-label prefix the author name is parsed from.
- `MENU_ITEM_SELECTORS` — items inside the opened "…" menu (incl. "Copy link").

**`linkedin_automation/auto_connector.py`** (`LinkedInAutoConnector`):

- `CONNECT_LINK_SELECTORS` / `CONNECT_BUTTON_SELECTORS` — the Invite/Connect link.
- `CONNECT_ACTION_SELECTOR`, `CONNECT_ARIA_HINTS` — supporting hooks.
- `SEARCH_RESULT_SELECTOR` / `RESULT_CARD_SELECTORS` — result cards (fallback path).
- `RESULT_NAME_SELECTOR` — name/title link on a card.
- `PAGINATION_NEXT_SELECTORS`, `PAGE_CURRENT_SELECTOR` — pagination.
- `SEND_BUTTON_*`, `MODAL_SELECTORS`, `INTEROP_OUTLET_SELECTOR` — the connect modal
  (shadow DOM; only appears after clicking Connect, so it can't be verified by the
  non-clicking health check — it's marked modal-only).

The health check reads these same constants from the modules, so the registry stays
in sync automatically — you only edit the constant.

### Step 4 — the golden rule: **new hooks first, old ones as fallbacks**

Add the new working selector to the **front** of the list and leave the previous
entries after it. This is non-destructive: older DOM variants and A/B buckets keep
working, and you never regress a still-valid selector. Every constant above is an
ordered list for exactly this reason.

### Step 5 — confirm HEALTHY before trusting it

Re-run the health check from Step 2 until it reports **HEALTHY** with non-zero
counts for the previously-failed selectors:

```bash
uv run python -m linkedin_automation.selector_health --profile <name>
```

Then do a small real run (a low `--max-posts` scrape, or a connector run capped low)
and confirm authors and post URLs come through correctly.

---

## 4. Hard-won lessons (the specific gotchas)

1. **The connector "Connect" is an icon-only link with a visually-hidden label.**
   It renders as `<a aria-label="Invite <Person Name> to connect"
   href="/preload/search-custom-invite/?vanityName=…">` inside
   `div[data-view-name='edge-creation-connect-action']`. Selenium's `element.text`
   is **empty** for it. The original code required `.text` to contain "connect", so
   every Connect was rejected → "pages forward, connects with nobody." **Detect by
   `aria-label`/`href`, never by visible text.**

2. **A "0 posts" result can be throttling, not a selector break.** After heavy
   automated access LinkedIn serves a degraded/skeleton feed with no real posts.
   The selectors are fine; there's just nothing to match. Confirm with
   `feed_dump.py`: if the dump's probe shows the post selector matching > 0 in the
   saved HTML, it's throttling (back off, run less often, re-run later), not a
   selector problem.

3. **Post permalinks are not in the feed DOM.** The public post URL isn't an
   attribute on the post element. The scraper gets it by opening the post's "…"
   menu and using **"Copy link to post"** (a JS clipboard intercept), then reading
   the copied URL. If URLs come back empty but posts are found, check the control
   menu + copy-link path, not the author selectors. (URL-less posts can't be
   commented on, so they're routed to `TRASH(no_url)`.)

4. **Navigate to `/feed/` exactly once.** A second navigation to `/feed/` triggers
   the skeleton/degraded feed. The startup path deliberately navigates once; if you
   add a "reload to be safe" it will *cause* the empty-feed symptom.

5. **Login is decided by URL/authwall, not by a DOM element.** A logged-in session
   stays on the requested authenticated path; a logged-out one bounces to an auth
   wall. This detection is rotation-proof — don't "fix" it by waiting for a feed
   element. A consequence: an empty search (0 result cards) still counts as
   **logged in**, so 0 cards means "empty search or stale selector," never "logged
   out."

6. **The connect modal's "Send" button lives in a shadow DOM.** "Send without a
   note" / "Send now" sit under `#interop-outlet`'s `shadowRoot`, which Selenium's
   normal `find_element` can't pierce. The code reaches it via JS
   (`document.querySelector('#interop-outlet').shadowRoot…`) and matches on the
   button **text** (rotation-resilient). This modal only appears *after* clicking
   Connect, so the health check can't verify it without sending an invite — it's
   documented as modal-only. If sends silently fail, inspect the shadow root.

7. **Author name comes from the control-menu aria-label, not the actor link.** The
   visible actor link text is empty (the name is in a nested span), and company
   posts have no `/in/` link at all. The `aria-label` "Open control menu for post by
   `<Person Name>`" is the one hook present on **both** post types — parse the name
   from there.

8. **Don't pin hashed class names.** They rotate constantly. If the only thing that
   uniquely identifies an element is a hashed class, widen the search (scope by a
   `data-testid` ancestor, match by `role`/`aria-label`) rather than pinning the
   hash — it'll break again next rotation.

---

## 5. The screenshot-on-failure aid

Browser-interaction failures **auto-save a screenshot plus the DOM** so you can see
the exact page state when something broke — no need to reproduce it live.

Look in **`data/<name>/failures/`** for:

- `failure_<label>_<timestamp>.png` — a screenshot of the page at the moment of
  failure,
- a small context sidecar (what was being attempted), and
- the page source (`.html`) alongside it.

This directory is git-ignored (it contains your live session). The highest-value
capture is the connector's `send_modal_missing` — the shadow-DOM Send button from
Lesson 6 — which is exactly where a silent selector break is otherwise hardest to
see.
