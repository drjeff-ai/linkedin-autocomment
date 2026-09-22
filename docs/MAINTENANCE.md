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

---

## 6. LinkedIn comment posting (the 2026-09-20 outage)

For two weeks the tool typed comments, failed to submit them, and marked the
posts done. Everything below is from live captures, not documentation.

### 6.1 The editor is TipTap/ProseMirror, and typing works

The comment box is **not an input**. It is a contenteditable div:

```
div[role='textbox'][contenteditable='true']
    class="tiptap ProseMirror …"
    aria-label="Text editor for creating comment"
```

Its empty state is `<p><br class="ProseMirror-trailingBreak"></p>` — recognise
that, because it looks like "the text never arrived".

**Per-character `send_keys` DOES register.** This was doubted and the input path
was briefly replaced with CDP `Input.insertText` on the strength of one capture
showing an empty editor. That was wrong: a second capture from the same run
holds all 177 characters. **No CDP, no paste, no `execCommand` is needed** —
and the human typing cadence is worth keeping, so do not trade it away without
evidence that keystrokes are actually being ignored.

### 6.2 TWO buttons read "Comment" — scope the submit to the composer

A post page carries both:

- the **action-bar** button, which only *focuses* the comment box, and
- the **composer submit**, which actually posts.

Neither carries an `aria-label`, both can be enabled, and their classes are
hashed and nearly identical. **No attribute test separates them.**
`//button[normalize-space(.)='Comment']` matches both and takes the first in
document order — the action bar. That single line was the visible bug: comment
typed, wrong button clicked, nothing posted, nothing raised.

What separates them is structure. From the editor, the composer submit shares an
ancestor **6 levels up**; the action-bar button not until **11** — and level 11
is the post card, `role="listitem"`.

> **Walk up from the editor, take the first ancestor containing a
> submit-looking button, and stop before `role="listitem"`. Never match
> page-wide.**

There is no usable attribute hook in between: no `<form>`, and the only
`data-testid` (`ui-core-tiptap-text-editor-wrapper`) wraps the editor *without*
the submit. Everything else there is hashed classes.

**Do not add a page-wide fallback.** One was added for the case where the walk
finds nothing, and it resolved straight back to the action-bar button — the
same bug in a new costume. Outside the composer there is nothing safe to click.

### 6.3 Disabled submit + empty editor can mean SUCCESS

LinkedIn disables the composer submit while the ProseMirror document is empty.
So:

- **enabling is the signal the text registered** — a better one than reading the
  box back, and worth waiting for rather than filtering on; and
- **after a successful post the box clears and the submit re-disables.**

An empty editor beside a disabled submit therefore reads identically whether the
comment never landed or just published. Do not infer failure from it. This cost
a whole dispatch: the state was read as "text never registered" when the comment
had in fact just posted.

Check `is_enabled()` **and** `disabled`, `aria-disabled`, and the
`artdeco-button--disabled` class. Selenium's `is_enabled()` reads only the
`disabled` property, so a button disabled the other three ways looks clickable
and silently does nothing.

### 6.4 Verify under `-commentList`; the old selector is dead

`div.comments-comment-item` **matches zero elements** on the current DOM. Both
captures contain no class token with "comment" in it at all — the tiptap-era
markup is hashed classes only.

The live hook is a container whose `data-testid` **ends in `-commentList`** (the
prefix is per-post), whose children are the rendered comments plus chrome.

- Match on the **comment TEXT** inside that container.
- **Poll** for a few seconds before concluding anything: a comment still
  rendering is not a comment that failed, and reaching for a keyboard fallback
  too early is how a slow render becomes a *second* comment.
- Do **not** use that container's child count as a "thread grew" signal. It
  counts the post header, the "Most relevant" control and other chrome, so
  against a pre-submit snapshot it reads as huge growth and passes
  unconditionally. It is not a comment count.

### 6.5 A positive-proof verifier MUST have a test proving it can return True

**This was the actual root cause, and it is the lesson most worth keeping.**

The verifier was tightened to accept only positive proof — the comment visible
in the thread. Correct. But the selector it looked under was dead, so it could
**only ever return False**. The consequences compound:

- every genuinely posted comment is reported as a failure;
- the queue never drains, so the same posts are offered again;
- the records say "not posted" about comments that are live, and a re-run
  duplicates them; and
- a "not posted yet" reading can trigger a fallback submit — a double post.

A verifier that cannot say yes is worse than no verifier, because it looks like
rigour. **Any check whose passing condition is "we found the thing" needs a test
that feeds it a page where the thing IS present and asserts True** — plus the
matching False case so it has not become a rubber stamp.

### 6.6 Never comment twice: ask the thread, not your records

Before typing, check whether the thread already carries a **self-authored**
comment — LinkedIn marks your own with a `• You` byline — or the exact text
about to be posted. If so, skip and record the URL as posted.

This is the one guard that does not depend on our own state being right, and on
2026-09-20 every other one was wrong simultaneously: the ledger said "not
posted" about a comment that was live. Asking the thread survives a cleared
store, a restored archive, a re-scrape, a second machine, and bugs not yet
found. Be conservative in the safe direction — an *unreadable* thread should not
block posting, or an unrelated DOM change silently stops the tool.

### 6.7 Where the evidence lives

Failures write to `data/<profile>/failures/`:

- `failure_<reason>_<ts>.png` / `.html` — page state, and
- `failure_<reason>_<ts>_submitdom.json` — every candidate submit and
  comment-box control with its text, `aria-label`, disabled state and size,
  PII-scrubbed.

The reasons are distinct on purpose and want different fixes:
`text_did_not_register` (nothing was ever clickable), and `comment_not_posted`
(an enabled submit was clicked, the keyboard was tried, and the comment still
did not appear).


## 7. Posts that are GONE (deleted, taken down, made private)

LinkedIn does not 404 a deleted post. It **redirects you to the feed**, which
is why this was invisible for so long: `div[role='listitem']` is in
`POST_DETAIL_SELECTORS` and the feed is full of them, so the navigation looked
like it had succeeded.

Before Dispatch 11, a gone post cost **two minutes** — six `POST_DETAIL_SELECTORS`
each run through a 20-second `WebDriverWait` — and then returned a bare `False`
that marked nothing. The record stayed `GENERATED`, so the same two minutes were
spent again on the next run, and the one after that, forever.

### 7.1 How it is decided

`comment_poster.classify_navigation()` polls three questions inside one
`NAV_DECIDE_SECONDS` (8s) budget, in this order:

1. **Redirected off the post?** Keyed on the **activity id**, not the URL —
   LinkedIn rewrites `/posts/<slug>-activity-<id>-xx` to `/feed/update/urn:li:activity:<id>`
   freely, and comparing URLs would call every post gone. → `UNAVAILABLE`.
2. **Post content present?** → `OK`.
3. **An explicit "removed" marker?** → `UNAVAILABLE`.

Anything else, including the content simply never loading, is `UNCLEAR`.

### 7.2 UNCLEAR is not UNAVAILABLE, and that asymmetry is the whole design

An `UNAVAILABLE` mark is **terminal and unreviewed** — the post leaves the queue
and nothing ever looks at it again. A false positive therefore deletes a real
post silently. So only a *positive* signal may mark one, exactly as with the
comment verifier (§6.5).

The specific case that makes this non-negotiable: **an expired session
redirects every post to the auth wall.** Treating a redirect as "gone" without
excluding `/login`, `/checkpoint`, `/authwall`, `/uas/` would terminally delete
the entire queue in a single run, with no way afterwards to tell which posts
were real. `NAV_AUTH_URL_MARKERS` exists for that one scenario.

### 7.3 ⚠ `NAV_UNAVAILABLE_SELECTORS` / `NAV_UNAVAILABLE_TEXTS` are UNVERIFIED

**No capture of a taken-down post exists yet.** Those markers are LinkedIn's
documented empty-state shapes, not anything observed on this account — do not
read them as confirmed the way the Like selector (§6) is.

They are only consulted when **no post content was found at all**, which is what
keeps a wrong guess harmless: the worst case is that a gone post falls through
to `UNCLEAR`, which is the safe side.

**To replace them with real ones:** the first `UNCLEAR` post of a run writes one
`failure_post_unclear_*` capture to `data/<profile>/failures/` (once per run, not
per post — forty unknown posts would otherwise be forty page dumps). Open its
`.html`, find what the page actually says, and put the real selector at the top
of `NAV_UNAVAILABLE_SELECTORS` per §4. Redirect detection carries the feature
until then; the markers only matter for a post that renders a removed-notice
*in place* rather than bouncing.

### 7.4 Where the state lives

The poster is the only thing that can observe a gone post, so it writes the fact
and the store reconciles **from** it — the same one-writer/one-reader shape as
`posted_comments` → `COMMENTED`:

    posting_progress.json   unavailable_posts: [{url, reason, at}]   (poster writes)
        ↓ post_store.reconcile() step 5
    posts_db.json           status: UNAVAILABLE                      (store reads)

Step 5 runs **before** the draft steps, because the comment file written before
the post was deleted is still sitting on disk and would otherwise pull the record
back to `GENERATED`.

`UNAVAILABLE` is deliberately none of the three statuses it resembles:

| not          | because                                                       |
|--------------|---------------------------------------------------------------|
| `TRASH`      | trash is "we judged this not worth commenting on", and its auto reasons are restorable so a re-scrape can let the post back in. Nothing here is reconsiderable. |
| `FAILED`     | nothing failed. Counting it as a failure buries real failures — the count that means "a comment we wrote did not go out, go and look" — in permanent noise. |
| `COMMENTED`  | obviously. `mark_unavailable` refuses to overwrite `COMMENTED`: that we commented stays true after the post comes down, and it is what the double-post guard reads. |

It drops out of the run queue for free, because the queue is `by_status(GENERATED)`.
