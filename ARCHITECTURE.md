# ARCHITECTURE

How this system is put together, why the pieces are arranged that way, and the
non-obvious rules a change must not break.

**Verification anchor.** Each section records what it was last checked against.
This file describes *current reality*, not intent — if code and this document
disagree, the document is the bug. Sections marked _(sketch)_ are deliberately
shallow and should be deepened when that subsystem is next worked on.

| Section | Last verified | Commit | Against |
|---|---|---|---|
| Post lifecycle / data flow | 2026-07-29 | `baed22f` | Endpoints executed against live profile `demo` — 1020 records: NEW 6 / GENERATED 79 / COMMENTED 218 / TRASH 717. All §1.9 invariants asserted green except the browser-render one. |
| Dashboard | 2026-07-29 | `baed22f` | `dashboard.py` + `templates/dashboard.html` read-through; endpoint behaviour exercised by the test suite |
| Scraper _(sketch)_ | 2026-07-29 | `baed22f` | `post_finder.py` read-through; recruiter filter calibrated against live data |
| Comment generator _(sketch)_ | 2026-07-29 | `baed22f` | `comment_generator.py` read-through; rejection path covered by tests |
| Comment poster _(sketch)_ | 2026-08-05 | `adopt-fork-bugfixes` | Selector constants asserted by `tests/test_comment_poster_selectors.py`. Selector *content* verified against a live permalink page on 2026-07-30 by the fork author, **not** re-verified here. Posting itself **still not** exercised against a live browser — see §8.3. |
| Scheduler _(sketch)_ | 2026-08-05 | `adopt-fork-bugfixes` | `scheduler.py` read-through + config-isolation regression tests (§6.1); leak reproduced against the pre-fix code |
| Auto-connector _(sketch)_ | 2026-07-29 | `fab3197` | `auto_connector.py` read-through; untouched by this branch |
| Selector health _(sketch)_ | 2026-08-05 | `adopt-fork-testinfra` | Registry composition and filtering asserted by `tests/test_selector_health.py`. Offline fixture gate executed and verified deterministic (200 identical runs, hash-seed independent, all sockets blocked) — §8.3. The browser-driven `run_post_health_check` is still **unexecuted**. |
| Offline selector gate | 2026-08-05 | `adopt-fork-testinfra` | `dom_probe.py` + 6 synthetic fixtures; `tests/test_dom_probe.py` (25) and `tests/test_selector_fixture_gate.py` (21) executed on Windows. Pure stdlib, no network. |

---

## 0. Where the documentation lives

**This file is the only operating document in version control.** It describes
current reality — what the system is and the rules a change must not break — so
it travels with the repo and is useful to anyone who clones it.

Everything about *running* the project lives in `.dev/`, which is gitignored.
Those files are the operating layer: they record cost limits, host-specific
constraints, and the project's own history, none of which belongs in a public
tree. A clone will not have them, and does not need them to understand the code.

| File | Contains |
|---|---|
| `.dev/PROJECT.md` | Cost caps, APIs touched, filesystem rules, known failure modes, and the recurring environment gotchas (TLS-intercepting proxy, chromedriver drift, Windows specifics, LinkedIn selector rotation) |
| `.dev/ROADMAP.md` | Phased plan for the work in flight. Written per feature when one is scoped, not maintained as a standing document |
| `.dev/DECISIONS.md` | Architectural decision log — date, decision, rationale, alternatives considered. The "why" behind choices this file only states |
| `.dev/UPDATES.md` | Session/phase log: what changed, what was tested, what was deferred |
| `.dev/BACKLOG.md` | Known-but-unfixed items and out-of-scope ideas. Where deferred work goes instead of into the current phase |
| `.dev/AUDIT_*.md` | Forensic write-ups for specific investigations, referenced from the sections below |
| `CLAUDE.md` | AI-assistant guardrails. Local, gitignored — no AI operating instructions in the public tree |

If this file and the code disagree, **this file is the bug**. If this file and a
`.dev/` document disagree about a decision, `.dev/DECISIONS.md` holds the
reasoning and wins on *why*; this file wins on *what is true now*.

---

## 1. Post lifecycle / data flow

This is the core of the system and the part that has broken repeatedly. Read
`.dev/AUDIT_bin_mismatch.md` for the full forensic history; this section is the
resulting contract.

### 1.1 The one rule

> **`data/<profile>/posts_db.json` is the source of truth for which posts exist
> and what state they are in. Every list the user sees is a query against it.
> Files on disk are derived output or process scratch — never state.**

The system spent three fix cycles violating this rule in a narrower way each
time. The failure mode was always identical: a *count* came from the store while
a *list* came from a file glob or a browser variable, and the two disagreed. The
user saw "49 NEW" next to an empty tab.

**Corollary — the trap that keeps catching us:** "bin == tab" must mean *the
rendered list*, not *the HTTP response*. Every previous fix asserted
`len(response.json["posts"]) == counts[bin]`, and every one of those assertions
was passing while the user saw zero. See §1.8.

### 1.2 States

```
                    ┌──────────────────────────────────────┐
                    │                                      │
  scrape ──▶ NEW ──generate──▶ GENERATED ──post──▶ COMMENTED
              │                    │
              │  evaluator says    │ (draft attached; reviewed_at
              │  "skip"            │  set when the user approves it)
              ▼                    ▼
            TRASH ◀────reject──────┘
              │
              └──restore──▶ GENERATED (if a draft survives) else NEW
```

| Status | Meaning |
|---|---|
| `NEW` | Scraped, passed the cheap filters, not yet evaluated or drafted. **A work queue** — if something can never be acted on, it does not belong here. |
| `GENERATED` | Has a draft comment on the record. Awaiting review and/or posting. |
| `COMMENTED` | The comment was posted to LinkedIn. Terminal. |
| `TRASH` | Not actionable, with a reason. Always restorable. |

**Trash reasons** (`post_store.py`). All except `manual` are "auto": a re-scrape
that produces better data can pull the post back out.

| Reason | Set by | Meaning |
|---|---|---|
| `ad` | `classify_ad` at scrape time | Promoted/sponsored, blocklisted advertiser, or company self-promotion |
| `job_card` | `classify_ad` at scrape time | LinkedIn job/recommendation card, **or** a member-authored recruiter ad (§1.7) |
| `low_quality` | scorer at scrape time | Below the relevance threshold |
| `no_url` | `trash_urlless_new`, reconcile step 4 | No URL, so it can never be commented on or posted |
| `evaluator_rejected` | the generator, on `verdict == "skip"` | The LLM evaluator (or a cheap spam pre-filter) turned it down |
| `manual` | the user, via the Reject button | Sticky — never resurrected by a re-scrape |

**Why `evaluator_rejected` exists.** Before it, a post the evaluator declined was
silently dropped from the generator's output and left in `NEW` — so it was
re-sent to the LLM, and re-billed, on every subsequent run. 43 of 49 `NEW` posts
were these zombies. `NEW` was an accumulator, not a queue.

### 1.3 Record schema (`SCHEMA_VERSION = 2`)

```json
{ "version": 2, "updated_at": "<iso>", "posts": { "<key>": { ...record } } }
```

| Field | Notes |
|---|---|
| `key` | Identity. Always equals the dict key. |
| `url` `author` `text` `category` `relevance_score` | Content, refreshed by re-scrapes |
| `status` `trash_reason` | The lifecycle state |
| `comment` `comment_meta` | The draft, and its style/approach/word_count |
| `scraped_at` `generated_at` `commented_at` `reviewed_at` `updated_at` | Timestamps |

**Identity** is the post's URL, else `hash:md5(author + text[:100])` —
`post_store.post_key`. There is exactly one identity function in the codebase;
anything that needs a post key calls it.

`reviewed_at` was added in v2. `PostStore._load` forward-migrates v1 records by
defaulting it to `None`, so no reader ever branches on version.

### 1.4 Every reader of post state

Since `fix-lifecycle-consistency`, **every entry in this table reads the store.**
If a future row says "files", that is the bug returning.

| Reader | Endpoint / entry point | Source | Filtering |
|---|---|---|---|
| Bin chips | `GET /api/posts/<p>/lifecycle` | store `counts()` | none |
| New chip | → `showNewPosts()` → `GET /api/posts/<p>` | store `by_status(NEW)` | none |
| Generated / Commented / Trash chips | `GET /api/posts/<p>/lifecycle?status=X` | store `by_status(X)` | none |
| Review Posts (step 02) | `GET /api/posts/<p>` | store `by_status(NEW)` | none; scrape files read **only** to enrich display metadata (likes/quality) |
| Review Comments (step 04) | `GET /api/comments/<p>` | store `review_queue()` | `reviewed_at is None`, unless `?include_reviewed=1` |
| Post preview (step 05) | `GET /api/comments/<p>?include_reviewed=1` | store `by_status(GENERATED)` | none |
| Generate input | `POST /api/comments/<p>/generate` | store `by_status(NEW)` | URL-bearing only |
| Post input (manual) | `POST /api/post/<p>` | store `by_status(GENERATED)` | reviewed drafts sorted first |
| Post input (scheduled) | `scheduler._fire_post_comments` | store `by_status(GENERATED)` | random count in `[count_min, count_max]` |
| `tools/reconcile_bins.py` | CLI | store | diagnostic only |

**Writers:**

| Writer | Writes to store? | Notes |
|---|---|---|
| `post_finder` | ✅ | Dual-writes: `ai_posts_<ts>.json` **and** an upsert per post with its classification |
| `comment_generator` | ✅ | `mark_generated` on success, `reject_by_evaluator` on turn-down |
| `save_comments` | ✅ | Writes the **edited** text back and stamps `reviewed_at` |
| `comment_poster` | ❌ **by design** | Appends to `posting_progress.json` only; reconcile syncs COMMENTED back on the next load |

### 1.5 Authority split — the one place two files are both authoritative

`posting_progress.json` is authoritative for **what was actually posted**. The
store never competes with it: `sync_with_progress` reconciles COMMENTED *from*
it. This is the only cross-file authority relationship in the system, it is
deliberate, and it works — all 218 posted URLs are COMMENTED in the store.

The poster is intentionally store-blind so that a store failure can never lose
the record of a real LinkedIn action.

### 1.6 Reconciliation

`post_store.reconcile()`, run by `load_synced_store()` on **every** store read
(all pipeline endpoints and the scheduler). Order is load-bearing:

1. URLs in `posting_progress.json` → `COMMENTED`
2. `NEW` with a draft on disk → `GENERATED` (drafts collected from `archived/` +
   `comments_*` + `ready_*`; later sources win)
3. `GENERATED` with a missing draft → recover from disk, else demote to `NEW`
4. Still-`NEW` with no URL → `TRASH(no_url)`

Idempotent and conservative: steps 2 and 4 only touch records still in `NEW`, so
manual TRASH is never resurrected and COMMENTED/GENERATED are never downgraded.

Reconcile is a **repair** mechanism, not the primary path. On a healthy store it
is a no-op. If it is routinely making changes, a writer is failing to write.

### 1.7 The recruiter-ad filter

Member-authored recruiter posts are not LinkedIn feed cards, so the
`JOB_CARD_PATTERNS` list misses them; they landed in `NEW` and burned an LLM call
each. `post_finder.is_recruiter_job_ad` catches them at scrape time, requiring
**both**:

- a hiring **headline** (`now hiring`, `we're hiring`, `Hiring:`, `open
  position`, `vacancy`, …), **and**
- ≥2 **structural job-listing signals** (`Location:`, `Experience:`, `Job Type:`,
  `send your resume`, `(1–2 years)`, `3 Positions`, …).

**The two-signal rule is a calibration, not a style choice.** This profile
genuinely engages with conversational AI-hiring posts ("I'm hiring for a role at
the intersection of quant research and ML…"). Those carry a headline and no
listing structure. Validated against the live store: **7 recruiter ads caught, 0
false positives across all 297 posts the profile has drafted or posted a comment
on.** Loosening either half of the rule breaks that. If you change it, re-run the
calibration against `COMMENTED` + `GENERATED` before shipping.

### 1.8 Load-bearing gotchas

- **The rendered DOM is the contract, not the HTTP response.** Three fixes
  asserted `bin == endpoint` and all three passed while the user saw empty tabs.
  `tests/test_lifecycle_consistency.py` asserts on `dashboard.html` source
  directly for this reason, and names the checks a human must still do in a
  browser.
- **`postsData` / `commentsData` / `postQueue` are render state, never truth.**
  Every panel re-reads the store when its cache is empty (`goStep`), and every
  mutation re-reads rather than splicing. Optimistic updates are fine; the reload
  must follow.
- **The New chip must do a server read.** It is the only chip that ever pointed
  at a client cache, and that is exactly the chip that broke.
- **`save_comments` archives every `comments_*.json`.** That is now harmless —
  nothing reads them for state — but it means a file-based reader introduced
  later will go *permanently* empty after the first save, not transiently.
- **`_files_within_days` is non-recursive** (skips `archived/`);
  `_collect_drafts` **is** recursive into `archived/`. Two globs over one
  directory with opposite intent. Know which you want.
- **`glob("daily_comments_*.txt")` also matches `daily_comments_curated_*.txt`**,
  and `glob("ai_posts_*.json")` also matches `ai_posts_curated_*.json` but *not*
  `lifecycle_new_*.json`.
- **`upsert_scraped` never downgrades.** COMMENTED/GENERATED and manual TRASH are
  immune to a re-scrape; only NEW and auto-TRASH adopt a freshly computed status.
- **`mark_generated` resolves by URL.** A post with no URL can never become
  GENERATED — which is why `TRASH(no_url)` exists.
- **`mark_generated` clears `reviewed_at`.** A regenerated draft goes back through
  review rather than inheriting the old approval.
- **Edits must reach the store.** The scheduler posts from the store, so a draft
  edited in the Review tab and saved only to a TXT file would be posted in its
  *unedited* form. `save_comments` writes the edited text onto the record.
- **Never show a filtered subset without saying what was filtered.** The review
  queue returns `reviewed_count` and the UI renders it. An unexplained short list
  is indistinguishable from data loss — that ambiguity is what made this class of
  bug so hard to see.
- **No cross-request locking on the store.** Concurrent mutation endpoints
  read-modify-write the whole file under Flask's threaded dev server. Known
  lost-update risk; not yet addressed (see `.dev/BACKLOG.md`).
- **Tests must never touch real user data.** `tests/conftest.py` has an autouse
  fixture pinning `pm.DATA_ROOT` to a temp dir for every test, because the
  pipeline endpoints now all read the store.

### 1.9 Invariants

Asserted in `tests/test_lifecycle_consistency.py` and
`tests/test_tab_bin_consistency.py`:

| Invariant | |
|---|---|
| `counts()[s] == len(by_status(s))` | by construction |
| `len(GET /api/posts) == counts["NEW"]` | ✅ |
| `len(GET /lifecycle?status=X) == counts[X]` | ✅ |
| `len(review queue) + reviewed_count == counts["GENERATED"]` | ✅ |
| all pipeline endpoints return the same `counts` object | ✅ |
| every GENERATED record has a non-empty `comment` | ✅ (reconcile step 3 enforces) |
| every NEW record has a URL | ✅ (reconcile step 4 enforces) |
| every URL in `posting_progress.json` is COMMENTED | ✅ (reconcile step 1 enforces) |
| a post the evaluator rejects leaves NEW | ✅ |
| rendered card count == bin count | ⚠️ **requires a human browser check** |

### 1.10 On-disk layout

```
data/<profile>/
  posts_db.json                  ← THE STORE (source of truth) — LinkedIn
  <platform>/
    posts_db.json                ← a second platform's OWN store, e.g. x/posts_db.json
  profile_config.json
  linkedin_timeline/
    ai_posts_<ts>.json           scrape output (also enrichment metadata)
    ai_posts_curated_<ts>.json   "Save & Continue" snapshot — NOT a generator input
    lifecycle_new_<ts>.json      store-driven generator input
  quality_comments/
    comments_<ts>.json           generator output (archived on save)
    ready_<ts>.json              curated snapshot
    daily_comments*<ts>.txt      poster input format
    scheduled_comments_<ts>.txt  scheduler-built poster input
    archived/                    drained comments_* files
    posting_progress.json        ← authoritative posted ledger
  connections/  failures/  posts/
```

**A platform is a separate store, not a column** (`post_store.py`). `PostStore`
and `load_synced_store` take `platform=`, defaulting to `"linkedin"` — so every
existing caller addresses exactly the file it always did, and LinkedIn's store
never moved. Isolation is the *path*: two platforms are two files, so a write to
one cannot corrupt the other, and the record schema is unchanged.

The one thing that is **not** shared is reconciliation. `reconcile()` and
`migrate_from_legacy()` read LinkedIn's derived files (`posting_progress.json`,
`comments_*`/`ready_*`), which is LinkedIn's policy rather than generic store
logic. `load_synced_store` therefore takes an injectable `reconciler=`: LinkedIn
gets `reconcile` by default, another platform gets none unless its caller supplies
one. Gating on `if platform == "linkedin"` inside the store would work, but it
puts a platform conditional in the shared class — which is what separate stores
exist to avoid.

---

## 2. Dashboard

Flask app on port 6500 (`dashboard.py`) serving a single-page UI
(`templates/dashboard.html`) plus a JSON API. Long-running browser/AI work runs
as background jobs (`run_job`) tracked in an in-memory `jobs` dict and polled via
`GET /api/jobs/<id>`.

**Browser lock:** at most one `task_type="browser"` job per profile at a time
(`can_start_browser_task`); `task_type="api"` jobs are unlimited. The scheduler
submits through the same lock as a manual click, so the two can never collide.

The pipeline is five steps — Scrape → Review Posts → Generate → Review Comments →
Post — plus four lifecycle chips. All nine views read the store (§1.4).

---

## 3. Scraper — `post_finder.py` _(sketch)_

Selenium against the logged-in feed. Per post: extract author/text/URL/engagement
→ `classify_ad` (promoted → blocklist → job card → recruiter ad → company
self-promotion) → relevance scoring against tiered AI keyword sets from the
profile config → `should_engage`.

Dual-writes `ai_posts_<ts>.json` and the store, mapping each outcome to a
lifecycle state: ad/job → `TRASH(ad|job_card)`, `!should_engage` →
`TRASH(low_quality)`, engageable but URL-less → `TRASH(no_url)`, else `NEW`.

URL extraction has a clipboard-based fallback; when it fails the post is trashed
as `no_url` rather than left stuck. A large `no_url` pile is the signal that URL
extraction is degrading.

---

## 4. Comment generator — `comment_generator.py` _(sketch)_

OpenAI only; never touches a browser. Input is a `{"quality_posts": [...]}` JSON
file written from the store's NEW bin. **There is no `input_file` override on the
endpoint** — the CLI (`python -m linkedin_automation.comment_generator <file>`)
is the advanced path.

`select_best_posts`: drop already-posted → cheap spam/hashtag rejects → LLM
`evaluate_post_quality` → require a URL → sort by conversation potential +
authenticity. Scores are advisory (used for ranking and style), **not** hard
gates. Uncapped by default; `--limit` is an optional ceiling.

Every turn-down — cheap pre-filter or LLM verdict — is written back as
`TRASH(evaluator_rejected)`, including on the "everything was rejected" early
return. Accepted comments go through style/persona rules and an on-topic
relevance check before `mark_generated`.

Cost discipline: every paid call is appended to `api_usage.jsonl`; the
`PROJECT.md` per-session cap applies.

---

## 5. Comment poster — `comment_poster.py` _(sketch)_

Selenium. Reads the TXT block format defined by `comment_fields.py` (one writer,
one parser, one module, so they cannot drift). Multiple posting strategies are
tried in order with verification after each; failures capture a screenshot + DOM
via `failure_capture.py`.

Appends posted URLs to `posting_progress.json` and **does not write the store**
(§1.5). All browser interaction is routed through `human_behavior.py`.

---

## 6. Scheduler — `scheduler.py` _(sketch)_

In-process thread, no Flask imports; the dashboard injects executor callbacks.
Per-profile randomized windows for `post_comments` and `scrape`, with a skip
chance, a minimum gap between fires, and daily roll of fire times. Submits
through the dashboard's `run_job` + browser lock.

`_fire_post_comments` reads the store's GENERATED bin and writes
`scheduled_comments_<ts>.txt` for the poster. It is currently the most reliable
path for draining GENERATED.

### 6.1 Config isolation between profiles

> **A merged scheduler config must never alias `DEFAULT_SCHEDULER`.** One
> long-running process serves every profile, so a shared nested section is a
> cross-profile leak.

`_deep_merge` deep-copies both the base and each override value.  It previously
shallow-copied, so any section a profile did not override stayed the *same
object* as the module-level default. `toggle()` does
`sched.setdefault(target, {})["enabled"] = enabled`, which then wrote straight
through into `DEFAULT_SCHEDULER` for the life of the process: enabling `scrape`
for one profile silently enabled it for **every** profile that had never
configured a scrape section, and the scheduler would drive a browser against
LinkedIn for accounts nobody switched on.

That is an account-safety problem, not a config bug — unexpected automated
activity is exactly what gets an account restricted. Regression coverage is in
`tests/test_scheduler.py` ("Config isolation between profiles").

---

## 7. Auto-connector — `auto_connector.py` _(sketch)_

Selenium over search-result pages, iterating Connect links (not result cards).
Handles the shadow-DOM Send button via JS injection. Rate limits — randomized
session target, daily soft cap, 200/week — resolved from the profile config.
State in `connections/connection_tracker.json` + per-session JSON. A stop file
(`.stop_connector_<profile>`) and subprocess termination both halt a run.

---

## 8. Selector health — `selector_health.py` _(sketch)_

Runs the live DOM against `SELECTOR_REGISTRY` and reports HEALTHY/DEGRADED/BROKEN
with per-selector match counts. Exposed in the dashboard as "Check Selectors".
`MAINTENANCE.md` is the repair runbook; `tools/feed_dump.py` and
`tools/connector_dump.py` dump live DOM for writing new selectors.

### 8.1 The registry covers three pages, not one

Each automation path's selectors live on a different LinkedIn page, and no single
run can reach them all. Every entry carries a `page` key, and each run filters to
its own:

| `page` | Owner class | Reached by | Result file |
|---|---|---|---|
| `feed` (default) | `LinkedInScraper` | default run | `selector_health.json` |
| `search` | `LinkedInAutoConnector` | `--search-url` | `selector_health_search.json` |
| `post` | `LinkedInCommentPoster` | `--post-url` | `selector_health_post.json` |

> **The registry is built from class constants, and that is load-bearing.** A
> selector inlined in a method body cannot be registered, so it cannot be
> watched. Every selector the automation depends on must be hoisted to a class
> constant on its owner class, and pulled into the registry from there — that is
> what keeps the monitor in sync with what the code actually uses.

### 8.2 Why the posting path is registered (2026-07-30)

The registry originally held only scraping selectors. LinkedIn moved the post
permalink page to `data-testid` attributes; `post_finder` was migrated and
`comment_poster`'s inline permalink selectors were not. All four went dead, every
post failed with "Post content not found on page" after 4 × 20s of
`WebDriverWait`, and a run placed **zero of three** comments — while a health
check minutes later still reported **HEALTHY**, because nothing on the posting
path was registered to fail.

A monitor that does not cover the risky path turns a loud failure into a
confident all-clear, which is worse than no monitor. The posting path is now
five registry entries (`post_detail`, `post_like_button`, `comment_open_button`,
`comment_input`, `comment_submit_button`), all `page="post"`.

Two of them — the editor and the submit button — do not exist until the comment
box is opened, so they carry `requires_comment_box: True` and are checked by
`_check_comment_box()`, which opens the box and counts. **It never types and
never clicks submit**, so a health run cannot post a comment.

`comment_submit_button`'s primary selector is an XPath matched on visible text
(`//button[normalize-space(.)='Comment']`): LinkedIn ships hashed class names,
and the button that *opens* the box carries `aria-label="Comment"` while the
*submit* button has the visible text and no aria-label. `selector_by()` reads the
strategy off the selector's shape, so `count_fn(selector)` stays one-argument.

### 8.3 Two gates: offline fixtures and the live check

Selector health has two halves, and **neither substitutes for the other**. They
fail for different reasons and catch different things.

| Gate | Answers | Catches | Misses | Cost |
|---|---|---|---|---|
| **Offline** — `--fixture`, `dom_probe` | does the code still match the shape it was written for? | *someone edited a selector and broke the match* | LinkedIn changing its DOM | ms, in CI, no session |
| **Live** — feed run / `--search-url` / `--post-url` | does the code still match what LinkedIn serves today? | *LinkedIn changed its DOM* | nothing — but needs a browser and a human | minutes, manual |

A fixture is a frozen snapshot of a DOM shape that was once correct. Passing
against it proves the selector constants still match that shape. It proves
**nothing** about today's live LinkedIn. Conversely the live check cannot run in
CI, cannot run on a PR, and only runs when a human remembers to run it — which is
why the 2026-07-30 breakage survived to production.

#### The offline gate

`linkedin_automation/dom_probe.py` is a pure-stdlib subset engine for CSS and the
one XPath the codebase uses. `selector_health.check_fixture()` / `fixture_report()`
run the same `check_registry` logic a browser run uses, against saved HTML.

```
uv run python -m linkedin_automation.selector_health --fixture tests/fixtures/post_box_open.html --page post
```

Fixtures live in `tests/fixtures/` (see its README) and are hand-authored
synthetic markup — never captured from a live session. `feed_healthy` /
`feed_degraded` / `feed_broken` differ by exactly one attribute each, so the gate
is asserted to distinguish HEALTHY / DEGRADED / BROKEN rather than merely "runs".
`feed_menu_open` and `post_box_open` are captured in the *gated* states, so the
overflow-menu and comment-composer entries are checkable at all.

> **A selector the engine cannot parse raises `UnsupportedSelector`; it is never
> scored 0.** `check_registry` swallows browser-side errors — one bad selector
> must not abort a live run — but deliberately re-raises this one. Silently
> counting an unreadable selector as zero manufactures exactly the false
> all-clear §8.2 is about. `test_dom_probe.py` asserts every registry selector
> parses, so a construct the grammar lacks breaks the build instead of degrading
> the check.

#### Honesty of a report (schema v2)

Every check result carries `checked` and `skip_reason`, and every report carries
`schema_version`, `source` (`live` | `fixture`) and a `not_checked` list. An entry
a run could not reach is reported as **not checked, with the reason** — never as a
silent pass and never as a failure. Both distortions are dangerous: a silent pass
is an unearned all-clear, and a false failure sends a repair at the wrong target.
The gate flags are `requires_menu_open`, `requires_comment_box`, `modal_only`.

#### What is STILL not verified

- The `POST_DETAIL_SELECTORS` values are **inherited from the fork author's
  2026-07-30 live-page verification**, not independently re-confirmed. The
  fixture gate cannot re-confirm them — it only proves the code matches the
  fixture, and the fixture was written from the same claim. Treat 2026-07-30 as
  the freshness bound.
- `run_post_health_check()` and `_check_comment_box()` are **still unexecuted**:
  their pure logic and the offline path are covered, their Selenium interaction
  is not.
- Whether a comment actually *lands* is untested end-to-end.

**Human checks still required before trusting the posting path:**

1. `uv run python -m linkedin_automation.selector_health --profile <p> --post-url <permalink>`
   → confirm it reports on all five `page="post"` entries and is not BROKEN.
2. Post one comment to a real post and confirm it appears on LinkedIn.
3. Confirm the check reports BROKEN when it should — run it against a non-post
   URL and check `post_detail` fails rather than passing vacuously.

Until (1) and (2) are done, the posting path's live status remains *plausible,
unverified*. The offline gate does not change that; it only stops the code
drifting away from the shape it was verified against.

---

## 9. Cross-cutting

- **Profiles.** `profile_manager.py` owns per-profile credentials, Chrome session
  dirs, data dirs, and config. All paths anchor to a computed `PROJECT_ROOT` —
  never `__file__`-relative, never cwd-relative.
- **Config.** `default_profile_config.json` is deep-merged under each profile's
  `profile_config.json`, so new default keys appear without clobbering user
  values. A corrupt config falls back to the default rather than raising.
- **Field naming.** `comment_fields.normalize_comment_fields` maps both historical
  conventions (`post_url`/`url`, `post_author`/`author`) to one canonical shape.
  Anything consuming a comment dict calls it.
- **Windows-only assumptions.** Task Scheduler (not cron), `.venv/Scripts/`,
  backslash paths in PowerShell. See `CLAUDE.md`.
- **Secrets** come from environment variables only; `.env*` is never committed.
