# ARCHITECTURE

How this system is put together, why the pieces are arranged that way, and the
non-obvious rules a change must not break.

This file is **architecture only**. It says what the parts are and how state
moves between them. It does not record run results, record counts, what has or
has not been live-verified lately, or what is in flight. That is current state,
and it lives in the handoff (see `docs/HANDOFF.md`). If the code and this
document disagree about *structure*, the document is the bug. Sections marked
_(sketch)_ are deliberately shallow and should be deepened when that subsystem
is next worked on.

---

## 0. Where the documentation lives

| File | Contains |
|---|---|
| `docs/ARCHITECTURE.md` | This file. Pipeline, state model, data layout, the rules a change must not break |
| `docs/MAINTENANCE.md` | The selector-repair runbook, plus the live DOM facts for commenting (§6), liking (§6.8) and dead-post detection (§7) |
| `docs/PRINCIPLES.md` | How work is done on this repo: evidence over hypothesis, bounded waits, terminal vs transient, and the rest |
| `docs/SCHEDULED_POSTING.md` | The Buffer/CSV scheduled-posting path |
| `docs/HANDOFF.md` | A thin pointer for an executor session: where the canonical handoff is, and the next action |

Everything about *running* the project lives in `.dev/`, which is gitignored.
Those files are the operating layer: they record cost limits, host-specific
constraints, and the project's own history, none of which belongs in a public
tree. A clone will not have them, and does not need them to understand the code.

| File | Contains |
|---|---|
| `.dev/PROJECT.md` | Cost caps, APIs touched, filesystem rules, known failure modes, and the recurring environment gotchas (TLS-intercepting proxy, chromedriver drift, Windows specifics, LinkedIn selector rotation) |
| `.dev/ROADMAP*.md` | Phased plans for work in flight. Written per feature when one is scoped |
| `.dev/DECISIONS.md` | Architectural decision log — date, decision, rationale, alternatives considered. The "why" behind choices this file only states |
| `.dev/UPDATES.md` | Session/phase log: what changed, what was tested, what was deferred |
| `.dev/BACKLOG.md` | Known-but-unfixed items and out-of-scope ideas |
| `.dev/AUDIT_*.md` | Forensic write-ups for specific investigations, referenced from the sections below |
| `CLAUDE.md` | AI-assistant guardrails. Local, gitignored — no AI operating instructions in the public tree |

If this file and a `.dev/` document disagree about a decision,
`.dev/DECISIONS.md` holds the reasoning and wins on *why*; this file wins on
*what the structure is*.

---

## 1. Post lifecycle / data flow

This is the core of the system and the part that has broken repeatedly. Read
`.dev/AUDIT_bin_mismatch.md` for the full forensic history; this section is the
resulting contract.

### 1.0 The pipeline

```
 scrape ──▶ generate ──▶ review / approve ──▶ post + like ──▶ reconcile
 post_finder  comment_generator   dashboard       comment_poster   post_store
```

| Stage | Module | Reads | Writes |
|---|---|---|---|
| **Scrape** | `post_finder.py` | the logged-in feed (Selenium) | `linkedin_timeline/ai_posts_<ts>.json` **and** a store upsert per post: `NEW`, or `TRASH(<reason>)` |
| **Generate** | `comment_generator.py` | the store's `NEW` bin (URL-bearing only) | a draft on the record → `GENERATED`; or `TRASH(evaluator_rejected)` |
| **Review / approve** | `dashboard.py` (Review Comments) | `GENERATED` records with `reviewed_at is None` | the edited text onto the record, and `reviewed_at` |
| **Post + like** | `comment_poster.py` | a TXT file built from `by_status(GENERATED)` | `posting_progress.json` only — never the store (§1.5) |
| **Reconcile** | `post_store.reconcile()` | `posting_progress.json` + comment files on disk | the store: `COMMENTED`, `UNAVAILABLE`, draft repair, `no_url` trash (§1.6) |

The poster is fed either by the dashboard's Post step (`POST /api/post/<p>`) or
by the scheduler (`_fire_post_comments`). Both build the same TXT block format
(`comment_fields.py`) from the store's `GENERATED` bin.

### 1.1 The one rule

> **`data/<profile>/posts_db.json` is the source of truth for which posts exist
> and what state they are in. Every list the user sees is a query against it.
> Files on disk are derived output or process scratch — never state.**
>
> **The one exception is `posting_progress.json`, which is the authoritative
> ledger of what the poster actually did on LinkedIn (§1.5). The store is
> reconciled *from* it, never the other way round.**

The system spent three fix cycles violating the first rule in a narrower way
each time. The failure mode was always identical: a *count* came from the store
while a *list* came from a file glob or a browser variable, and the two
disagreed. The user saw "49 NEW" next to an empty tab.

**Corollary — the trap that keeps catching us:** "bin == tab" must mean *the
rendered list*, not *the HTTP response*. See §1.8.

### 1.2 States

```
                                         ┌──────────────── (poster: post is gone) ──────────┐
                                         │                                                   ▼
  scrape ──▶ NEW ──generate──▶ GENERATED ──post──▶ COMMENTED                          UNAVAILABLE
              │                    │
              │  evaluator says    │ (draft attached; APPROVED = reviewed_at set)
              │  "skip"            │
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
| `TRASH` | Rejected: not worth commenting on, with a reason. Always restorable. |
| `UNAVAILABLE` | The post is **gone from LinkedIn** — deleted, taken down, made private. Terminal. |

**Approved is not a status.** A draft is approved when its `GENERATED` record
carries a `reviewed_at` timestamp. `mark_generated` clears it, so a regenerated
draft goes back through review rather than inheriting the old approval.

**The posting queue is `by_status(GENERATED)`, sorted reviewed-first**
(`reviewed_at` set sorts ahead of `reviewed_at is None`). Approval changes the
order, not membership: an unreviewed draft is still postable, just after every
approved one.

**Why `UNAVAILABLE` is none of the statuses it resembles:**

| not | because |
|---|---|
| `TRASH` | trash is "we judged this not worth commenting on", and its auto reasons are restorable so a re-scrape can let the post back in. Nothing about a deleted post is reconsiderable. |
| `FAILED` | nothing failed. Counting a gone post as a failure buries real failures in permanent noise. |
| `COMMENTED` | `mark_unavailable` refuses to overwrite `COMMENTED`: that we commented stays true after the post comes down, and it is what the double-post guard reads. |

It leaves the run queue for free, because the queue is `by_status(GENERATED)`.
`upsert_scraped` treats it as protected (a stale scrape file must not put a
dead post back in the queue), and it outranks every other status in `_RANK`.

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
re-sent to the LLM, and re-billed, on every subsequent run. `NEW` was an
accumulator, not a queue.

### 1.3 Record schema (`SCHEMA_VERSION = 2`)

```json
{ "version": 2, "updated_at": "<iso>", "posts": { "<key>": { ...record } } }
```

| Field | Notes |
|---|---|
| `key` | Identity. Always equals the dict key. |
| `url` `author` `text` `category` `relevance_score` | Content, refreshed by re-scrapes |
| `status` `trash_reason` | The lifecycle state |
| `unavailable_reason` `unavailable_at` | Set only by `mark_unavailable` |
| `comment` `comment_meta` | The draft, and its style/approach/word_count |
| `scraped_at` `generated_at` `commented_at` `reviewed_at` `updated_at` | Timestamps |

**Identity** is the post's URL, else `hash:md5(author + text[:100])` —
`post_store.post_key`. There is exactly one identity function in the codebase;
anything that needs a post key calls it.

`reviewed_at` was added in v2. `PostStore._load` forward-migrates v1 records by
defaulting it to `None`, so no reader ever branches on version.

### 1.4 Every reader of post state

**Every entry in this table reads the store.** If a future row says "files",
that is the bug returning.

| Reader | Endpoint / entry point | Source | Filtering |
|---|---|---|---|
| Bin chips | `GET /api/posts/<p>/lifecycle` | store `counts()` | none |
| New chip | → `showNewPosts()` → `GET /api/posts/<p>` | store `by_status(NEW)` | none |
| Other lifecycle chips | `GET /api/posts/<p>/lifecycle?status=X` | store `by_status(X)` | none |
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
| `comment_poster` | ❌ **by design** | Writes `posting_progress.json` only; reconcile syncs `COMMENTED` and `UNAVAILABLE` back on the next load |

### 1.5 Authority split — `posting_progress.json` is the ledger

`data/<profile>/quality_comments/posting_progress.json` is authoritative for
**what the poster actually found and did on LinkedIn**:

| Key | Written when | Reconciled into |
|---|---|---|
| `posted_comments` | a comment was posted and verified in the thread, or the thread already carried ours | `COMMENTED` |
| `unavailable_posts` — `[{url, reason, at}]` | navigation classified the post as gone (MAINTENANCE §7) | `UNAVAILABLE` |
| `skipped_already_commented` — `[{url, reason, at}]` | the thread already carried our comment; the URL also goes into `posted_comments` | (via `posted_comments`) |
| `failed_comments` — `[{url, reason, at, evidence}]` | a comment could not be confirmed posted | nothing — the record stays `GENERATED` and is retried |

The store never competes with the ledger: reconcile flips records *from* it.
This is the only cross-file authority relationship in the system and it is
deliberate. The poster is store-blind so that a store failure can never lose the
record of a real LinkedIn action, and because the poster is the only component
that can observe a post being gone or a comment being live: one writer, one
reader, no competing record.

### 1.6 Reconciliation

_Verified against the code: `post_store.reconcile()` and the store methods it
calls, at `edae1dd`, 2026-09-25._

**Who runs it.** `post_store.reconcile()` runs inside `load_synced_store()`, which
the dashboard's read and bulk-action endpoints (generate, save, post) and the scheduler's `post_comments` job use. The
`tools/reconcile_bins.py` diagnostic calls it directly. It runs for the LinkedIn
store only: another platform's store is reconciled only if its caller injects
a `reconciler=`. The store **writers** (`post_finder`, `comment_generator`, and
the dashboard's per-post mutation endpoints) open `PostStore` directly and do
**not** reconcile. Their changes are picked up by the next reconciling read.

**The steps, in execution order.** The docstring numbers them 1–5, but the code
runs them **1 → 5 → 2 → 3 → 4**, and that order is load-bearing:

1. **step 1: posted ledger → `COMMENTED`** (`sync_with_progress`). Every URL in
   `posting_progress.json` `posted_comments` marks its record `COMMENTED` and
   clears `trash_reason`, **whatever its status**, except a record that is
   already `COMMENTED` or is `UNAVAILABLE`. URLs are compared *normalised*
   (query and fragment stripped, trailing slash removed, lowercased).
2. **step 5: gone posts → `UNAVAILABLE`** (`mark_unavailable`). Every entry in
   `posting_progress.json` `unavailable_posts` marks its record `UNAVAILABLE`,
   with `unavailable_reason` and `unavailable_at`, from **any** status except
   `COMMENTED`, including `TRASH`. The URL is matched exactly (store key or
   URL), not normalised. It runs before the draft steps because the comment
   file written before the post was deleted is still on disk, and step 2 would
   otherwise pull the record back to `GENERATED`.
3. **step 2: `NEW` with a draft on disk → `GENERATED`** (`mark_generated`).
   Drafts come from `_collect_drafts`: `archived/*.json`, then `comments_*.json`,
   then `ready_*.json`, and a later source overwrites an earlier one for the
   same URL. **Only records currently `NEW`** are touched.
4. **step 3: `GENERATED` without a draft → recover or demote**
   (`recover_or_demote_generated`). A `GENERATED` record whose `comment` is
   empty gets its draft re-attached from the same collection, and stays
   `GENERATED`. If no draft exists it is demoted to `NEW`, with the comment,
   metadata and `generated_at` cleared, so it regenerates.
5. **step 4: `NEW` without a URL → `TRASH(no_url)`** (`trash_urlless_new`).
   Last, so anything the earlier steps moved out of `NEW` is never trashed.

**What that guarantees, and what it doesn't.**
- **Statuses** are idempotent: a second run moves no record to a different
  status.
- The second run is **not a no-op** while `unavailable_posts` has any entry.
  Step 5 re-asserts an already-`UNAVAILABLE` record: it re-stamps
  `unavailable_at` and `updated_at`, counts it again in `stats["unavailable"]`,
  and that count makes the store save. So a store with any gone post is
  rewritten on every reconciling read. Every other step does count only real
  changes.
- The store is saved only when some step reports a change. Per-step counts come
  back in `stats`: `commented`, `unavailable`, `generated_from_files`,
  `recovered_drafts`, `demoted_generated`, `trashed_no_url`.
- Steps 2 and 4 touch only `NEW`, and step 3 only draftless `GENERATED`, so
  none of them resurrects a trashed post or downgrades `COMMENTED` or
  `UNAVAILABLE`.
- The two ledger steps are **not** restricted that way, on purpose. A post
  that is on the posted ledger is `COMMENTED` even if it was trashed. A post
  the poster found gone is `UNAVAILABLE` even if it was trashed.
- `COMMENTED` and `UNAVAILABLE` never overwrite each other. Step 1 skips an
  `UNAVAILABLE` record and step 5 skips a `COMMENTED` one, so whichever a record
  reached first is kept. A URL on both ledgers that reaches its first reconcile
  still unmarked ends up `COMMENTED`, because step 1 runs first.

Reconcile is a **repair** mechanism for the store-writing stages and the
**primary** path for the poster's outcomes. If it is routinely changing
anything other than `COMMENTED`/`UNAVAILABLE`, a writer is failing to write.

### 1.7 The recruiter-ad filter

Member-authored recruiter posts are not LinkedIn feed cards, so the
`JOB_CARD_PATTERNS` list misses them; they landed in `NEW` and burned an LLM call
each. `post_finder.is_recruiter_job_ad` catches them at scrape time, requiring
**both**:

- a hiring **headline** (`now hiring`, `we're hiring`, `Hiring:`, `open
  position`, `vacancy`, …), **and**
- ≥2 **structural job-listing signals** (`Location:`, `Experience:`, `Job Type:`,
  `send your resume`, `(1–2 years)`, `3 Positions`, …).

**The two-signal rule is a calibration, not a style choice.** The profile
genuinely engages with conversational AI-hiring posts ("I'm hiring for a role at
the intersection of quant research and ML…"). Those carry a headline and no
listing structure. Loosening either half of the rule reintroduces false
positives. If you change it, re-run the calibration against `COMMENTED` +
`GENERATED` before shipping.

### 1.8 Load-bearing gotchas

- **The rendered DOM is the contract, not the HTTP response.** Three fixes
  asserted `bin == endpoint` and all three passed while the user saw empty tabs.
  `tests/test_lifecycle_consistency.py` asserts on `dashboard.html` source
  directly for this reason.
- **`postsData` / `commentsData` / `postQueue` are render state, never truth.**
  All three of `goStep('posts')`/`goStep('review')`/`goStep('post')` re-read
  their cache unconditionally on every visit — not only when it happens to be
  empty. `goStep('post')` got this first (Dispatch 23: the Post step is the
  one that actually fires a write, so a stale-but-nonempty queue there risks
  posting the wrong drafts); `'posts'`/`'review'` followed in the same
  dispatch's immediate follow-up once the same staleness risk was recognized
  as structural rather than Post-step-specific — a non-empty-but-stale cache
  was rendering untouched on navigation back to either panel after a mutation
  made elsewhere. `.dev/scratch/AUDIT_22.md` is the audit that found the
  original gap. Every mutation that can change the
  GENERATED bin (reject, restore, save/approve, generate, post) re-reads both
  `lifecycleCounts` and `postQueue` after its own request, rather than
  splicing — `removeComment` included: "Remove" is a real store mutation
  (`TRASH(manual)` via the same reject endpoint the Posts panel uses), not a
  local array splice. A scheduled run, a second tab, or a CLI run can still
  change the store with no request this tab makes; `startCacheRefreshPolling()`
  polls `lifecycleCounts`/`postQueue` every 20s as the one mechanism that
  catches that case (`postsData`/`commentsData` are deliberately excluded —
  Review Comments holds live unsaved per-draft edits a poll-triggered
  re-render would discard).
- **The New chip must do a server read.** It is the only chip that ever pointed
  at a client cache, and that is exactly the chip that broke.
- **`save_comments` archives every `comments_*.json`.** Harmless now — nothing
  reads them for state — but a file-based reader introduced later will go
  *permanently* empty after the first save, not transiently.
- **`_files_within_days` is non-recursive** (skips `archived/`);
  `_collect_drafts` **is** recursive into `archived/`. Two globs over one
  directory with opposite intent. Know which you want.
- **`glob("daily_comments_*.txt")` also matches `daily_comments_curated_*.txt`**,
  and `glob("ai_posts_*.json")` also matches `ai_posts_curated_*.json` but *not*
  `lifecycle_new_*.json`.
- **`upsert_scraped` never downgrades.** COMMENTED/GENERATED/UNAVAILABLE and
  manual TRASH are immune to a re-scrape; only NEW and auto-TRASH adopt a freshly
  computed status.
- **`mark_generated` resolves by URL.** A post with no URL can never become
  GENERATED — which is why `TRASH(no_url)` exists.
- **Edits must reach the store.** The scheduler posts from the store, so a draft
  edited in the Review tab and saved only to a TXT file would be posted in its
  *unedited* form. `save_comments` writes the edited text onto the record.
- **Never show a filtered subset without saying what was filtered.** The review
  queue returns `reviewed_count` and the UI renders it. An unexplained short list
  is indistinguishable from data loss.
- **No cross-request locking on the store.** Concurrent mutation endpoints
  read-modify-write the whole file under Flask's threaded dev server. Known
  lost-update risk (see `.dev/BACKLOG.md`).
- **Tests must never touch real user data.** `tests/conftest.py` has an autouse
  fixture pinning `pm.DATA_ROOT` to a temp dir for every test.

### 1.9 Invariants

Asserted in `tests/test_lifecycle_consistency.py`,
`tests/test_tab_bin_consistency.py` and `tests/test_unavailable_posts.py`:

| Invariant | |
|---|---|
| `counts()[s] == len(by_status(s))` | by construction |
| `len(GET /api/posts) == counts["NEW"]` | ✅ |
| `len(GET /lifecycle?status=X) == counts[X]` | ✅ |
| `len(review queue) + reviewed_count == counts["GENERATED"]` | ✅ |
| all pipeline endpoints return the same `counts` object | ✅ |
| every GENERATED record has a non-empty `comment` | ✅ (reconcile step 3 enforces) |
| every NEW record has a URL | ✅ (reconcile step 4 enforces) |
| every URL in `posted_comments` is COMMENTED | ✅ (reconcile step 1 enforces) |
| every URL in `unavailable_posts` is UNAVAILABLE, unless COMMENTED | ✅ (reconcile step 5 enforces) |
| a post the evaluator rejects leaves NEW | ✅ |
| rendered card count == bin count | ⚠️ **requires a human browser check** |

### 1.10 On-disk layout

Everything under `data/` is runtime output and gitignored. Paths are anchored to
`profile_manager.PROJECT_ROOT`, never to cwd or `__file__`.

```
data/
  profiles/
    profiles.json                ← profile registry + encrypted credentials
    chrome_sessions/<profile>/   ← per-profile Chrome user-data dir (the LinkedIn session)
  <profile>/
    posts_db.json                ← THE STORE (source of truth) — LinkedIn
    <platform>/posts_db.json     ← a second platform's OWN store, e.g. x/posts_db.json
    profile_config.json          ← deep-merged over default_profile_config.json
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
      debug_screenshots/
      posting_progress.json      ← THE LEDGER: posted_comments, unavailable_posts, failed_comments
    failures/                    ← failure_<reason>_<ts>.{png,html,json} (raw), *_submitdom.json (PII-scrubbed)
    connections/  posts/         ← auto-connector state, scheduled-posting state
    selector_health*.json        ← last selector-health report per page
```

**A platform is a separate store, not a column** (`post_store.py`). `PostStore`
and `load_synced_store` take `platform=`, defaulting to `"linkedin"`. Isolation
is the *path*: two platforms are two files, so a write to one cannot corrupt the
other, and the record schema is unchanged.

The one thing that is **not** shared is reconciliation. `reconcile()` and
`migrate_from_legacy()` read LinkedIn's derived files, which is LinkedIn's
policy rather than generic store logic. `load_synced_store` therefore takes an
injectable `reconciler=`: LinkedIn gets `reconcile` by default, another platform
gets none unless its caller supplies one.

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
Post — plus one lifecycle chip per status. All of these views read the store
(§1.4).

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

OpenAI; never touches a browser. Input is a `{"quality_posts": [...]}` JSON file
written from the store's NEW bin. **There is no `input_file` override on the
endpoint** — the CLI (`python -m linkedin_automation.comment_generator <file>`)
is the advanced path.

`select_best_posts`: drop already-posted → cheap spam/hashtag rejects → LLM
`evaluate_post_quality` → require a URL → sort by conversation potential +
authenticity. Scores are advisory (used for ranking and style), **not** hard
gates.

Every turn-down — cheap pre-filter or LLM verdict — is written back as
`TRASH(evaluator_rejected)`. Accepted comments go through style/persona rules
and an on-topic relevance check before `mark_generated`.

Cost discipline: every paid call is appended to `api_usage.jsonl`; the
`PROJECT.md` per-session cap applies.

---

## 5. Comment poster — `comment_poster.py`

Selenium. Reads the TXT block format defined by `comment_fields.py` (one writer,
one parser, one module, so they cannot drift). All browser interaction is routed
through `human_behavior.py`. Writes `posting_progress.json` and **never the
store** (§1.5). The DOM facts behind every step are in `docs/MAINTENANCE.md`
§6–§7.

Per post, in order:

1. **Ledger check.** A URL already in `posted_comments` is skipped.
2. **Navigate and classify** (`classify_navigation`, one bounded budget):
   `OK`, `UNAVAILABLE` (redirected off the activity id, or an explicit removed
   marker — recorded in `unavailable_posts`, terminal), or `UNCLEAR` (anything
   else, including an auth-wall redirect — retryable, never terminal).
3. **Ask the thread.** If a self-authored comment or the exact text is already
   there, record the URL as posted and stop. This guard does not depend on any
   of our own state being right.
4. **Like** — one exact selector, bounded in total, non-critical: a failed like
   never blocks the comment.
5. **Comment** — open the composer, type with human cadence, wait for the
   submit to *enable*, click the submit **scoped to the composer** (never
   page-wide).
6. **Verify** — poll for the comment text under the `-commentList` container.
   Only positive proof goes into `posted_comments`; anything else is a
   `failed_comments` entry plus a failure capture.

**Terminal vs transient is the design axis.** Only a positive signal marks a
record terminal (`posted_comments`, `unavailable_posts`); every ambiguous
outcome leaves it `GENERATED` for the next run.

---

## 6. Scheduler — `scheduler.py` _(sketch)_

In-process thread, no Flask imports; the dashboard injects executor callbacks.
Per-profile randomized windows for `post_comments`, `scrape`, `generate`, and
`connector` (§7), with a skip chance, a minimum gap between fires, and daily
roll of fire times. Submits through the dashboard's `run_job` + browser lock.

`_fire_post_comments` reads the store's GENERATED bin and writes
`scheduled_comments_<ts>.txt` for the poster. `_fire_connector` reads
`ConnectionTracker`'s cross-run cap directly and never submits a job for a
profile already at it (§7) — the one job type whose fire decision depends on
state outside this module.

`generate` is the third job type (Dispatch 37): unlike `post_comments` and
`scrape`, it only ever fires from the unattended tick loop when a profile's
`auto_mode` is also on — see the module docstring in `scheduler.py` for the
per-cycle cap and the circuit breaker that disables `auto_mode` after
consecutive failures. `auto_mode`'s single-toggle convenience governs exactly
`post_comments`/`scrape`/`generate` (`AUTO_MODE_JOB_TYPES`); `connector` is
unrelated and is never touched by it.

### 6.1 Config isolation between profiles

> **A merged scheduler config must never alias `DEFAULT_SCHEDULER`.** One
> long-running process serves every profile, so a shared nested section is a
> cross-profile leak.

`_deep_merge` deep-copies both the base and each override value. A shallow copy
leaves any section a profile did not override as the *same object* as the
module-level default, and `toggle()`'s `setdefault(...)["enabled"] = ...` then
writes straight through into `DEFAULT_SCHEDULER`: enabling `scrape` for one
profile silently enables it for every profile that never configured one.

That is an account-safety problem, not a config bug — unexpected automated
activity is exactly what gets an account restricted. Regression coverage is in
`tests/test_scheduler.py` ("Config isolation between profiles").

---

## 7. Auto-connector — `auto_connector.py` _(sketch)_

Selenium over search-result pages, iterating Connect links (not result cards).
Handles the shadow-DOM Send button via JS injection. State in
`connections/connection_tracker.json` (`ConnectionTracker`) + per-session JSON.
A stop file (`.stop_connector_<profile>`) and subprocess termination both halt
a run.

**The real config fields, measured against the code (`AUDIT_36.md` §6), not
assumed from naming.** The profile config's `connector` section has exactly
three fields the code reads: `max_daily_requests` (daily cap, default 25),
`weekly_limit` (weekly cap, default 100), `note_template` (optional note
text). **There is no "randomized session target."** A profile config was once
found carrying `daily_limit`/`session_min`/`session_max` that nothing in
`auto_connector.py` ever read (`conn.get(...)` only ever reads the three names
above) — dead config that would be tuned and believed
(`docs/PRINCIPLES.md` §2's "a silent failure is the bug" applies to config as
much as runtime behavior). Those three keys were removed from the live
profile that carried them rather than wired to a feature nothing asked for.

**Both caps are cross-run, not per-session.** `ConnectionTracker.get_remaining()`
sums every `record_sent()` ever made for that profile against the calendar
day / ISO week, read from `connection_tracker.json` — the file is the
authority, not any in-memory count. `run()` reads it and clamps
`max_requests` to `remaining` **before `setup()` ever opens a browser**: a
profile already at its daily or weekly cap returns a (non-failure) result
with zero sent and never calls `pm.create_driver` at all. Before this, the
clamp happened after `setup()`, so a capped-out profile still paid for a full
browser session that could never send anything.

**Search-URL rotation.** `connector.search_urls` (a list) is the per-profile
home for a rotating queue of search-results URLs. `resolve_search_url(profile,
explicit_url)` is the one decision point: an explicit URL (a human pasting a
fresh one, in the dashboard or on the CLI) is used as-is and never touches the
rotation; with none given, `ConnectionTracker.next_search_url(urls)` returns
the next URL and persists the advance **in the tracker file**, not memory, so
it survives a restart and wraps at the end — a single-URL list therefore
behaves exactly as it always did. Nothing else about the connector gained
"resume where I left off" state; the pointer is specifically about which URL a
run uses, not which page or which candidate within a run.

**Scheduling.** `connector` is a fourth `scheduler.py` job type (alongside
`post_comments`/`scrape`/`generate`, §6), default off, with its own randomized windows
and a `pages` field (how many search-result pages one scheduled session
paginates — deliberately half the CLI's own manual default of 10, since this
fires unattended). It goes through the same browser-task lock every other
scheduled job does. Unlike `post_comments` (queue-gated on the GENERATED bin)
or `scrape` (nothing to pre-check), `connector` is **cap-gated**:
`_fire_connector` reads `ConnectionTracker.get_remaining()` before ever
calling the executor, so a profile already at its cap never submits a job,
let alone reaches the browser factory — mirroring how a queue-empty check
keeps `post_comments` from submitting a pointless job.

---

## 8. Selector health — `selector_health.py` _(sketch)_

Runs the live DOM against `SELECTOR_REGISTRY` and reports HEALTHY/DEGRADED/BROKEN
with per-selector match counts. Exposed in the dashboard as "Check Selectors".
`docs/MAINTENANCE.md` is the repair runbook; `tools/feed_dump.py` and
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
> constant on its owner class, and pulled into the registry from there.

### 8.2 Why the posting path is registered

The registry originally held only scraping selectors. When LinkedIn moved the
post permalink page to `data-testid` attributes, the poster's inline selectors
all went dead and every post failed — while a health check still reported
**HEALTHY**, because nothing on the posting path was registered to fail. A
monitor that does not cover the risky path turns a loud failure into a
confident all-clear, which is worse than no monitor.

The posting path is five registry entries (`post_detail`, `post_like_button`,
`comment_open_button`, `comment_input`, `comment_submit_button`), all
`page="post"`. The editor and the submit button do not exist until the comment
box is opened, so they carry `requires_comment_box: True` and are checked by
`_check_comment_box()`, which opens the box and counts. **It never types and
never clicks submit**, so a health run cannot post a comment.

`comment_submit_button` is a presence check: its XPath
(`//button[normalize-space(.)='Comment']`) matches the action-bar button too.
The poster itself never uses it page-wide — it scopes the search to the
composer (MAINTENANCE §6.2).

### 8.3 Two gates: offline fixtures and the live check

Selector health has two halves, and **neither substitutes for the other**.

| Gate | Answers | Catches | Misses | Cost |
|---|---|---|---|---|
| **Offline** — `--fixture`, `dom_probe` | does the code still match the shape it was written for? | *someone edited a selector and broke the match* | LinkedIn changing its DOM | ms, in CI, no session |
| **Live** — feed run / `--search-url` / `--post-url` | does the code still match what LinkedIn serves today? | *LinkedIn changed its DOM* | nothing — but needs a browser and a human | minutes, manual |

A fixture is a frozen snapshot of a DOM shape that was once correct. Passing
against it proves the selector constants still match that shape. It proves
**nothing** about today's live LinkedIn. Conversely the live check cannot run in
CI and only runs when a human remembers to run it.

#### The offline gate

`linkedin_automation/dom_probe.py` is a pure-stdlib subset engine for CSS and the
XPath the codebase uses. `selector_health.check_fixture()` / `fixture_report()`
run the same `check_registry` logic a browser run uses, against saved HTML.

```
python -m linkedin_automation.selector_health --fixture tests/fixtures/post_box_open.html --page post
```

Fixtures live in `tests/fixtures/` (see its README) and are hand-authored
synthetic markup — never captured from a live session. `feed_healthy` /
`feed_degraded` / `feed_broken` differ by exactly one attribute each, so the gate
is asserted to distinguish HEALTHY / DEGRADED / BROKEN rather than merely "runs".

> **A selector the engine cannot parse raises `UnsupportedSelector`; it is never
> scored 0.** `check_registry` swallows browser-side errors — one bad selector
> must not abort a live run — but deliberately re-raises this one. Silently
> counting an unreadable selector as zero manufactures exactly the false
> all-clear §8.2 is about.

#### Honesty of a report (schema v2)

Every check result carries `checked` and `skip_reason`, and every report carries
`schema_version`, `source` (`live` | `fixture`) and a `not_checked` list. An entry
a run could not reach is reported as **not checked, with the reason** — never as a
silent pass and never as a failure. The gate flags are `requires_menu_open`,
`requires_comment_box`, `modal_only`.

---

## 9. Cross-cutting

- **Profiles.** `profile_manager.py` owns per-profile credentials, Chrome session
  dirs, data dirs, and config. All paths anchor to a computed `PROJECT_ROOT` —
  never `__file__`-relative, never cwd-relative.
- **Config.** `default_profile_config.json` is deep-merged under each profile's
  `profile_config.json`, so new default keys appear without clobbering user
  values. A corrupt config falls back to the default rather than raising.
- **Identity guard (`identity_slug`).** Acting as the wrong real person cannot
  be undone, so the write paths that act on a live account verify who is logged
  in first. For LinkedIn (`csv_pipeline.verify_identity`) the comparison is the
  `/in/<slug>` segment of the resolved profile URL, **exactly** — not a
  substring, and a login or checkpoint redirect is a refusal, not a pass. A
  mismatch aborts the sweep. The expected slug comes only from profile config
  (`identity_slug`); **an empty `identity_slug` disables THIS guard**, so it must
  be set for any profile that posts. (The production access guard below reads
  the same field but has the opposite failure direction: for IT, an empty
  `identity_slug` is a refusal, not a no-op — so as of that guard,
  `identity_slug` must be set for every profile that drives a browser at all,
  not only ones that post.) X has its own fail-closed guard against the
  Buffer channel handle. See `docs/SCHEDULED_POSTING.md`.
- **Production access guard (`check_production_guard`).** A different risk from
  the one above: `verify_identity` guards against being the *wrong* account,
  live, mid-run — this guards against being the *real* one at all, before any
  browser exists. A profile *name* does not say which account it drives
  (`jeff` and `prod` can both be production; an unregistered profile resolves
  to no identity at all), so production is **declared**, in
  `PRODUCTION_IDENTITY_SLUGS` (an environment variable, not committed config —
  a LinkedIn vanity slug identifies a real person, §10), never inferred from a
  profile's name. `profile_manager.create_driver` — the one function every
  browser-driving entry point in the project funnels through — refuses
  (`ProductionAccessRefused`, exit code `EXIT_PRODUCTION_REFUSED`) if the
  resolved profile's declared `scheduled_posting.identity_slug` is either a
  listed production identity, or empty (unresolvable is not the same as known
  dev — it is refused, not assumed safe). `--allow-production` (or
  `LINKEDIN_ALLOW_PRODUCTION=1`) overrides the production case. The dashboard
  and the scheduler always pass it — both route every action through the same
  job bodies in `dashboard.py`, and that is production doing its job — so only
  an ad-hoc CLI/module run is ever refused.

  X's Buffer write path has no browser and so never reaches `create_driver`,
  which means it bypasses `check_production_guard` entirely — `verify_identity`
  (above) checks it is the *right* channel, but nothing checked it was
  *authorized to be a production one at all*. `profile_manager.
  check_declared_production(identity_slug, context, allow_production=False)`
  is the sibling for that path: same two rules, same env vars, same exception
  and exit code, but it takes an already-resolved identity_slug (X's resolves
  from `scheduled_posting.platforms.x.identity_slug`, not the flat field
  LinkedIn profiles use) rather than a profile name. It is called from inside
  `csv_pipeline.schedule_pass` — the one chokepoint every Buffer write passes
  through — **before** `verify_identity`, so a declared-production identity
  without authorization never reaches Buffer at all, not even for the identity
  check's own request. Deliberately a separate function rather than a second
  call into `check_production_guard`, so LinkedIn's guard and its exact-message
  tests are untouched by a change on a path LinkedIn doesn't use. Rationale in
  `.dev/DECISIONS.md`'s 2026-09-29 entry.
- **Field naming.** `comment_fields.normalize_comment_fields` maps both historical
  conventions (`post_url`/`url`, `post_author`/`author`) to one canonical shape.
  Anything consuming a comment dict calls it.
- **Windows-only assumptions.** Task Scheduler (not cron), `.venv/Scripts/`,
  backslash paths in PowerShell.
- **Secrets and PII.** Secrets come from environment variables only; `.env*` is
  never committed. `data/`, DOM dumps and `api_usage.jsonl` are gitignored
  because they carry account and member content. The `*_submitdom.json`
  capture is PII-scrubbed at write (`failure_capture.scrub_pii`); the `.png` /
  `.html` page captures are raw and must never leave `data/`.

---

## 10. X / Buffer scheduled posting

X is a second platform bolted onto the LinkedIn-shaped pipeline without
reshaping it: one store per platform (§1.10), one policy module for
per-platform text rules, and one scheduling chokepoint both platforms share.
The write path never opens a browser at all; the read path (`x_finder.py`) is
scrape-only and does not post.

### 10.1 The read path — a pure function from HTML to posts

`linkedin_automation/x_finder.py`. Structurally the opposite of
`post_finder.py`: LinkedIn's scraper must **interact** with the page (open the
"…" menu, click Copy link, read the clipboard) to learn a post's URL.
`x_finder.py:158` (`canonical_permalink`) never has to — every X card's
permalink is a plain `<a href>` — so the whole parse is a pure function,
`parse_timeline(html) -> [XPost]` (`x_finder.py:292`), tested offline against
saved DOM with no browser and no session. `XTimelineFinder` (`x_finder.py:421`)
is the thin live half: navigate, scroll, hand HTML to the pure parser. Scrolling
is not optional — X virtualizes the timeline, so a harvester that scrolls first
and reads once misses everything that scrolled past; `collect()`
(`x_finder.py:450`) reads after every scroll and accumulates by permalink.

Cards nest: a quote-tweet carries two `User-Name` blocks and two avatars, and
every extractor (`_author_name`, `_posted_at`, `canonical_permalink`) takes the
**first** match inside the card, not the only one, for exactly that reason.

**What is confirmed against saved DOM vs. inferred**, per the module's own
docstring (`x_finder.py:14-52`): the card/action-row/permalink selectors and
engagement-count parsing are confirmed. `_author_name`'s `@`-split
(`x_finder.py:235`) and `parse_search_results`'s aliasing to `parse_timeline`
(`x_finder.py:314`) are marked `PROVISIONAL` — the scrubbed captures prove
structure, not real text content, and no search-page DOM was ever saved to test
against. A wrong display name costs a label, never a mis-addressed post — the
post's identity (`get_identifier`, `x_finder.py:121`) is always the permalink,
confirmed separately.

**Selector health does not reach this page yet.** `SELECTOR_REGISTRY` in
`selector_health.py` registers `x_timeline`/`x_status`/`x_search` entries
built from `x_selectors.py`'s constants (e.g. `selector_health.py:471`), but
the module's `--page` CLI flag only accepts `feed`/`search`/`post`/`composer`
(`selector_health.py:1304-1305`) — LinkedIn's four pages. There is no live or
fixture path to check an X selector through the shipped tool today; logged in
`.dev/BACKLOG.md`.

### 10.2 The write path — Buffer only, never a browser

X posts through Buffer's API; nothing on this path constructs a `webdriver`.
`csv_pipeline.schedule_pass()` (`csv_pipeline.py:776`) is the one chokepoint
every platform's Buffer write passes through, reached by the dashboard's manual
"Schedule" route, the CLI (`tools/run_scheduled_posts.py`), and (for
LinkedIn only today) the automatic drain. `buffer_client.create_post()`
(`buffer_client.py:431`) is the sole GraphQL mutation that actually creates a
post; `schedule_pass` is its only live caller.

**Where the link goes is a platform decision** (`compose_for_platform`,
`csv_pipeline.py:205`), not a CSV column: LinkedIn's `first_comment_link` is
posted as a first comment by the browser pass (Buffer's `firstComment` field is
paywalled and rejects the whole post on the free plan); X has no comment pass
at all, so the link is appended to the tweet body instead.

> **Measured, 2026-09-19 (`.dev/DECISIONS.md`).** X's `metadata.twitter.thread`
> field — a free threaded self-reply — is not paywalled the way LinkedIn's
> `firstComment` is, but a live `--create` run through the Buffer spike showed
> it **drops the base post's image**. Trading the image for a threaded reply
> was the wrong trade for this project's posts, which is why X uses
> link-in-body rather than a self-reply thread. `--thread-text` stays in
> `tools/buffer_spike.py` as the evidence for that decision, not as a path
> production takes.

**Validation is platform-aware.** `validate_row()` (`csv_pipeline.py:227`)
enforces X's hard character limit only when `platform == X_PLATFORM`, via
`platform_policy.policy_for(X).length.hard_max` and `x_counted_length()`
(`csv_pipeline.py:198`, which counts every URL as X's fixed t.co cost, not its
literal length). `PipelineState` (`csv_pipeline.py:280`) takes a `platform=`
argument and keeps LinkedIn's on-disk state file path completely unchanged
(`platform == SCHEDULED_DEFAULT_PLATFORM` is the one branch that returns the
legacy path verbatim) while giving every other platform its own file — the
same "isolation is the path" pattern the post store uses (§1.10).

**`comment_pass()` refuses outright for any platform but LinkedIn** — X has no
comment pass, because the link already rode in the body; asking it to comment
would imply a browser action that makes no sense for this platform. The CLI
names this explicitly rather than trying and failing.

### 10.3 Two identity guards, answering two different questions

- **`verify_identity` / `verify_channel_identity` / `verify_buffer_identity`**
  (`csv_pipeline.py:468,418,448`) — "is this the right account". For X this
  compares Buffer's own channel handle (`channel_handle`, `csv_pipeline.py:392`,
  reading `name` then falling back to `externalLink`'s last segment) against
  the configured `identity_slug`, exact after case-folding and a stripped
  leading `@`. This is the Buffer-channel analogue of LinkedIn's browser
  profile-URL check (§9) — same shape, different surface, because X has no
  browser session to read a URL from.
- **`profile_manager.check_declared_production`** (§9) — "was this run
  authorized to touch a declared-production identity at all". A different
  axis entirely: a *correctly configured*, genuinely-production X channel
  passes `verify_identity` every time. `check_declared_production` is what
  refuses it without `--allow-production`, called inside `schedule_pass`
  before `verify_identity`, so a refused identity never reaches Buffer even
  for the identity check's own request.

Both checks run; neither substitutes for the other.

### 10.4 Config resolution — LinkedIn is flat, every other platform is an overlay

`profile_manager.resolve_scheduled(profile_name, platform)`
(`profile_manager.py:422`) is the one place `(profile, platform)` becomes
usable settings (`api_key`, `channel_id`, `identity_slug`, `drain`).
**LinkedIn resolves exactly as it always has** — the flat `scheduled_posting.*`
fields, key from the bare `BUFFER_API_KEY`, missing key tolerated (`api_key`
comes back `None` rather than raising, since resolving a channel id must not
fail on an unrelated variable). **Every other platform requires its own
`scheduled_posting.platforms.<platform>` overlay** naming its own
`buffer_api_key_env`, and raises `ScheduledConfigError` if the overlay, the key
variable name, or the variable's value is missing — there is no fallback to
LinkedIn's flat fields. An unknown platform with no overlay is an error, not a
silent LinkedIn default: returning LinkedIn's channel for a typo'd platform
name would publish X content to LinkedIn, unrecoverably and silently.

### 10.5 Multi-account Buffer caching

`buffer_client.get_channel`/`scheduled_slots` used to cache on `channel_id`
alone, which is a cross-account leak the moment a second Buffer key exists: the
same channel id under a different key would silently serve the *first* key's
channel, org id, and slot count. `_key_fingerprint(key)`
(`buffer_client.py:309`) — a 12-hex SHA-256 prefix, never the raw key, since
cache dicts get logged and repr'd — is folded into both cache keys so two
accounts' reads can never cross. `clear_caches()` (`buffer_client.py:335`) is
the reset used by tests and forced refreshes.

### 10.6 Platform policy — text rules, not posting rules

`platform_policy.policy_for(platform)` (`platform_policy.py:363`) is the
generator's per-platform contract: length unit and ceiling (`LengthPolicy`),
whether exceeding it makes a draft literally unpostable (X, 280 chars, a hard
API limit) or merely too long to be good (LinkedIn, a quality heuristic), and
voice. **There is no default** — an empty or unrecognized platform raises
`UnknownPlatform` rather than falling back to LinkedIn's policy, because a
silent LinkedIn default here would generate LinkedIn-voiced text for X and pass
every automated gate on the way out. This is the same "refuse rather than
default" shape `resolve_scheduled` (§10.4) and the identity guards (§10.3) all
share.

### 10.7 The post drain is LinkedIn-only today

`post_drain.py` and its dashboard job body (`dashboard.py:1956`,
`_drain_feed_job`) never reference `platform` at all (zero matches) — every
automatic drain resolves to `platform="linkedin"` inside `schedule_pass`
regardless of what a profile is configured for. X rows are never fed
automatically; only the manual "Schedule" route (`dashboard.py:1724`,
`_scheduled_schedule_job`, which threads `platform=`/`identity_slug=`
correctly) and the CLI schedule the X write path today. Logged in
`.dev/BACKLOG.md` as a gap for whoever makes X draining work, not a design
decision.

### 10.8 What is not covered here

`docs/SCHEDULED_POSTING.md` is the operator runbook for this path and predates
the X work — it documents the LinkedIn CSV/Buffer flow only. It has not yet
been extended for X's CSV shape, the `--platform` flag, or the
`--allow-production` flag on the schedule pass; treat it as LinkedIn-only until
it is.
