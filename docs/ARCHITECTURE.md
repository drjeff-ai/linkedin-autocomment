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
| `TRASH` | Rejected: not worth commenting on, with a reason. Always restorable. `manual` and `stale` (a draft past `draft_max_age_days`, §6.2) are sticky against a re-scrape; the computed reasons are not. |
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
| `approved_hash` | sha256 of the approved `comment` (E0, §4.1). Absent or `None` means not approved; no schema bump, because absent already denies |

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
| Post input (scheduled) | `scheduler._fire_post_comments`, auto_cycle post stage | store `by_status(GENERATED)` after stale expiry (§6.2) | random count in `[count_min, count_max]` |
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
    scheduler_state.json         ← today's scheduler slots, last_fire, recent runs (§6.2)
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

### 2.1 Content calendar and due times (U1)

**Every queue row says when it goes out.** `_scheduled_row_view` carries
`date`, `time_window` and `planned` (`content_calendar.planned_label`, e.g.
`Tue 11-24 morning, 8-11 PT`) from the stored row, alongside `due_at`. The row
shows `due <exact time>` once Buffer has a `due_at`, otherwise `planned
<label>`. Times are shown in Pacific with the zone named (`schpWhen`).

**The calendar** is the `📅 Content calendar` card in the Scheduled Posting
section: month and week views, previous/next, a platform filter, per-day counts
and one chip per post, coloured by group: queued (PENDING), held (HELD),
in Buffer (SCHEDULED), sent (PUBLISHED, COMMENTED, COMMENT_FAILED), failed
(FAILED). Clicking a chip shows `schpRow(detail, true)`, the queue row's own
detail with no checkbox and no Delete. **It has no write action of any kind.**
Moving a post is a later phase, because a move must go through
`redate_pending` so keys never move (DEC-033).

`GET /api/scheduled/<p>/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD[&platform=C]`
(`scheduled_calendar`) returns `{items, undated, counts, groups, timezone}`.
`items` are ordered by Pacific day, then time (`due_at`, else the window's
start; none last), then platform and key; each has platform, key, state,
group, title, preview, image url, day, `due_at` or window, `time_label`,
`hold_reason`, errors and `detail`. `undated` holds rows with no day, which
the panel lists beside the grid. The day of a Buffer row comes from its
`due_at` in Pacific, not from the UTC date. It reads **local files only**: no
reconcile-on-render (unlike the queue), no Buffer or other network call, no
write (state paths are computed by `content_calendar.state_path`, so not even
a data directory is created). It therefore shows last-known state.

The route collects from `dashboard._CALENDAR_SOURCES`, a list of callables
`profile -> items`. Today there is one, every scheduled-posting state file the
profile has (`content_calendar.state_file_items`), then the app holds, then
the YouTube channel's own state (`youtube_channel.calendar_items`, V1,
§10.13). A YouTube video sits on its `publishAt` day in Pacific: uploaded
and still private reads "Scheduled on YouTube" (group `scheduled_youtube`),
past its `publishAt` (or live with none, on its upload day) reads "Sent".
`?platform=` accepts any declared channel (`channels.CHANNELS`).

*Verification anchor (2.1): 2026-11-22, branch `u1-calendar`;
`tests/test_content_calendar.py` covers the view fields for PENDING, HELD and
SCHEDULED rows, the label of all five windows, the route's range, states,
order and counts against fixture state files, a no-network guard that is
shown to fail on a real attempt, no write, and the rendered panel with no
write action. Refreshed 2026-11-22, branch `v1-youtube`: the route shows
YouTube items from `tests/test_youtube_channel.py`'s fixture state.*

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

### 4.1 Approval: the generator's output waits for a human (E0, DEC-060)

A draft leaves the generator `GENERATED` and **unapproved**. Approval is per
item, on the record: `reviewed_at` plus `approved_hash`, the sha256 of the exact
approved text (`post_store.approve`). `post_store.is_approved(record)` is the
one shared check: `status == GENERATED`, `reviewed_at` set, and `approved_hash`
equal to the hash of the current `comment`. `approval_refusal` names the reason
when it is not (`wrong_status`, `not_reviewed`, `no_approved_hash`,
`edited_after_approval`, `no_record`).

- **Review panel.** Each card has an Approve button
  (`POST /api/comments/<p>/approve`, `{key, comment}`): the text in the box is
  the text approved. "Approve all shown (N)" states the count and asks to
  confirm; `/approve_all` refuses unless `confirm` is the JSON `true` and
  `count` equals the number of items.
- **Save stores edits only.** `/save` calls `save_edit`, which approves
  nothing, and an edit that changes the text clears `reviewed_at` and
  `approved_hash`, so the draft is back in the queue. `mark_generated` clears
  both too: a regenerated draft is a new draft.
- **Records stamped before E0** (`reviewed_at`, no hash) are not approved and
  are back in the review queue (`unapproved_reviewed_count`). A legacy
  `ready_*.json` seeds unapproved: a file is not an approval.
- `review_queue()` is the GENERATED drafts that are not approved, and
  `reviewed_count()` the approved ones, so queue + approved = the bin.

Every engagement post path calls the check at its last step before posting
and posts nothing that fails it, logging each refusal with its reason and
saying `0 posted: N not approved`:

| Path | Where |
|---|---|
| Dashboard Post route | `dashboard.post_comments`: approved only; the old "fall back to the rest of the bin" and the hand-built `comments_file` are gone |
| Scheduler `post_comments` | `Scheduler._fire_post_comments` |
| `auto_cycle` post stage | `dashboard._auto_cycle_job.run_post`. A draft the cycle just generated is never approved, so the cycle posts only what a human approved earlier |
| `comment_poster` subprocess | `LinkedInCommentPoster.approval_refusal`, re-reading the store before each comment of the TXT handoff (§5), and the `--test-url` mode; the check itself is the shared `engagement_adapter.approval_refusal_for` (§12) |
| YouTube `youtube-engage` and the panel's Post button | `youtube_engage._post_one`: a fresh read of the YouTube store and `approval_refusal` just before the insert (§11) |

*Verification anchor (4.1): 2026-11-22, branch `e0-approval-gate`;
`tests/test_approval_gate.py`, `tests/test_generate_from_lifecycle.py`
(`test_auto_cycle_posts_nothing_unapproved`).*

---

## 5. Comment poster — `comment_poster.py`

Selenium. Reads the TXT block format defined by `comment_fields.py` (one writer,
one parser, one module, so they cannot drift). All browser interaction is routed
through `human_behavior.py`. Writes `posting_progress.json` and **never the
store** (§1.5). The DOM facts behind every step are in `docs/MAINTENANCE.md`
§6–§7.

The TXT file is a handoff, not an authority (E0). The poster is only ever
given approved store records serialized to TXT, and before each comment it
re-reads the store: the URL's record must be `post_store.is_approved` and its
text must equal the text in the file, or the comment is refused with its
reason and counted `not_approved` in the run summary. `--test-url` posts only
an approved record's text; free text is refused.

Per post, in order:

1. **Ledger check.** A URL already in `posted_comments` is skipped.
   **Approval check.** `approval_refusal` against the store, as above.
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

*Verification anchor (5, approval re-check): 2026-11-22, branch
`e0-approval-gate`; `tests/test_approval_gate.py` (`test_poster_*`).*

---

## 6. Scheduler — `scheduler.py` _(sketch)_

In-process thread, no Flask imports; the dashboard injects executor callbacks.
Per-profile randomized windows for `post_comments`, `scrape`, `auto_cycle`, and
`connector` (§7), with a skip chance, a minimum gap between fires, and daily
roll of fire times. Submits through the dashboard's `run_job` + browser lock.

`_fire_post_comments` reads the store's GENERATED bin and writes
`scheduled_comments_<ts>.txt` for the poster. `_fire_connector` reads
`ConnectionTracker`'s cross-run cap directly and never submits a job for a
profile already at it (§7) — the one job type whose fire decision depends on
state outside this module.

`auto_cycle` is the third job type (Dispatch 40, replacing Dispatch 37's
separate `generate` job): unlike `post_comments` and `scrape`, it only ever
fires from the unattended tick loop when a profile's `auto_mode` is also on.
It is ONE job with ONE window: firing it submits a single async job
(`submit_cycle_job`) whose real body (`dashboard._auto_cycle_job`) runs
scrape, then generate against what the scrape produced, then post, in that
order, waiting for each stage to finish before starting the next and
stopping at the first one that fails — sequenced by the pure, module-level
`scheduler.run_auto_cycle(profile, run_scrape, run_generate, run_post)`,
which takes the three stage runners as plain injected callables and is
exactly what tests target for call-order and short-circuit behavior, with no
engine, Flask, or subprocess involved. A scrape that finds zero posts is not
a failure — the cycle still runs generate and post.

Any stage failing disables `auto_mode` on that FIRST failure (no
consecutive-failure counting — see `scheduler.report_cycle_outcome` /
`_disable_auto_mode`), writing `auto_mode_stopped_reason` naming the failing
stage. `auto_mode`'s single-toggle convenience now governs only `auto_cycle`'s
`enabled` flag; `post_comments`/`scrape` keep their own independent toggles
(Audit 36 — they can run unattended under the plain master+job `enabled`
flags, with no auto_mode involved) and their config is never written to by
`auto_mode` in either direction, same as `connector`.

**But while `auto_mode` is ON, `_tick_profile` suppresses `post_comments` and
`scrape` from firing unattended (Dispatch 41).** `auto_cycle` already runs a
scrape and a post stage itself; without the suppression, a profile with
`auto_mode` on and `scrape.enabled`/`post_comments.enabled` also true gets
the cycle's scrape/post AND the standalone jobs' own independent windows
firing — duplicated work, not a second opinion. The gate is in the tick loop
only: it reads `enabled` on both standalone jobs and skips evaluating their
windows entirely while `auto_mode` is on, but never writes to their config,
so turning `auto_mode` back off restores their own schedules exactly as
configured, with no separate re-toggle. `run_now()` bypasses the gate (same
as it already bypasses `enabled`), so a profile can still manually fire
`scrape`/`post_comments` on demand regardless of `auto_mode`. `connector` is
untouched by any of this, in both directions.

Holding the browser lock across the whole cycle, rather than taking it in
turn per stage, is possible and is what `_auto_cycle_job` does: the lock
(`can_start_browser_task`) is just whether a `task_type="browser"` job entry
is still `running`, not a literal webdriver handle, so one `run_job` call
covering all three stages (even the API-only generate stage in the middle)
holds it continuously for free. An existing config that still carries the
old `scheduler.generate` section keeps it as inert data — nothing reads it,
nothing fires from it, nothing deletes it.

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

### 6.2 One engine process, persisted state, honest misses, stale drafts (S1)

**One engine process.** `run.bat` runs `python -m linkedin_automation.dashboard`,
whose `main()` calls `app.run(debug=True, use_reloader=True)`. The reloader runs
the module twice: a parent that only watches files, and a serving child with
`WERKZEUG_RUN_MAIN=true`. `_should_start_engines(use_reloader, environ)` starts
the scheduler, the post drain and the first-comment drain only in the child, and
logs `Background engines started in process <pid>`. The old guard,
`not app.debug or ...`, was always true in the parent because `app.debug` is set
only inside `app.run`, so production ran two schedulers and two of each drain
(DEC-014, DEC-016). The scheduler status JSON carries `engine_pid`, the pid of the
process serving it; it should equal the logged pid.

**One predicate for tick and status.** `_job_runs_unattended(cfg, job_type)`
(job enabled; `auto_cycle` only with `auto_mode`; `scrape` and `post_comments`
not while `auto_mode` is on) gates both `_tick_profile` and `status()`. Before
S1, `status()` rolled every enabled job, so opening the page recorded a MISSED
run for jobs the tick never ran. A job `auto_mode` suppresses reports
`paused_by: "auto_mode"` and windows with `fire_at: null`, `status: "paused"`;
the dashboard shows `Paused: auto mode runs this as part of the cycle.`

**Persisted state.** `Scheduler(state_path_fn=...)`; the dashboard injects
`data/<profile>/scheduler_state.json`. It holds today's slots (job_type, window
index, window, rolled_window, rolled_date, fire_at, will_skip, status),
`last_fire` and `recent_runs`, written temp-then-`os.replace` after every roll,
fire and record (including `run_now`), and read once per profile on first
access. Slots rolled on another day are dropped; runs and `last_fire` are kept,
so MIN_GAP holds across a restart and across midnight. A missing, unreadable or
malformed file logs a warning and starts empty. With `state_path_fn=None` (every
older test) nothing is read or written.

**Roll rules.**

| Case | Result | Note on the run record |
|---|---|---|
| Fresh roll, time after now | PENDING | — |
| Fresh roll, time at or before now, window open | re-drawn uniformly in `[now, end]`, PENDING | — |
| Fresh roll, window closed | MISSED | `dashboard started after this window closed` |
| Slot loaded from the file, PENDING, time passed, window open | fires on the normal `_maybe_fire` path | — |
| Slot loaded from the file, PENDING, time passed, window closed | MISSED | `dashboard was not running at the scheduled time` |

A slot already FIRED, SKIPPED or MISSED today is loaded as such and never fires
again. MIN_GAP, the skip chance and the browser-busy wait are unchanged; a
PENDING slot deferred by them while the dashboard runs still fires late, as
before.

**Stale drafts.** `scheduler.draft_max_age_days` (default 3, top level beside
`auto_mode`, 0 disables, no UI field). `scheduler.expire_stale_drafts` calls
`PostStore.expire_stale_generated`, which moves GENERATED records whose
`generated_at` (else `updated_at`) is more than that many days old to TRASH with
reason `stale`, draft kept. Records with no or an unparseable timestamp are left
alone. Both scheduled post paths call it before selecting: `_fire_post_comments`
and the auto_cycle post stage (`dashboard._auto_cycle_job.run_post`, which reads
the full profile config because its `job_cfg` is the auto_cycle section only).
The manual Post button does not. `stale` is not in `AUTO_REASONS`, so a re-scrape
leaves it in TRASH (`_is_sticky_trash`); `restore()` returns it to GENERATED with
a fresh `generated_at`, so the next expiry does not take it straight back.

*Verification anchor (6.2): 2026-11-13, branch `s1-scheduler`;
`tests/test_scheduler_s1.py` covers the engine guard, status vs tick over 48
simulated hours, persistence across a restart, the roll rules and stale expiry.*

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
`page="post"`. Since E1 (§12) they are defined beside LinkedIn's engagement
adapter, `linkedin_adapter.registry_entries`, and spliced into the registry at
the same place in its order; the selectors are still the poster's class
constants. The editor and the submit button do not exist until the comment
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
- **Publishing-only profiles (B0).** A brand that only publishes through Buffer
  has no LinkedIn login. `add_profile(name)` with both credentials blank (None or
  `""`) stores `username: null, password: null, session_dir: null` and creates
  no Chrome session dir; exactly one blank raises `ValueError` naming it. Such a
  profile never becomes the default implicitly (first-profile rule, the
  single-profile fallback in `get_default_profile_name`, or reassignment on
  remove), because the default is what LinkedIn tools run as. `create_driver`
  and `login` raise `PublishingOnlyProfileError` ("Profile '<name>' is
  publishing-only: it has no LinkedIn login.") before any browser exists, so
  every LinkedIn entry point refuses it at the same chokepoint as the
  production guard. Scheduled-posting overlays, `resolve_scheduled`,
  `queue_rows` and `schedule_pass` never read credentials and work unchanged.
  `POST /api/profiles` takes both credential fields present and blank (a body
  without the fields is refused); the dashboard form gates it behind a
  Publishing-only checkbox, and the CLI uses `add <name> --publishing-only`.
  *Verification anchor (9, publishing-only): 2026-11-16, branch
  `b0-publishing-only`, `tests/test_profile_manager.py` B0 section.*
- **Passwords never reach the browser (F1).** `GET /api/profiles` passes each
  stored record through `dashboard._public_profile`, which drops `password`
  and adds `has_password: true|false`; the username and every other field stay.
  It is the only route that returns profile records, and the page reads
  `has_password` for its publishing-only label. There is no edit-profile route,
  so nothing ever needs the password back on the client.
  *Verification anchor (9, password): 2026-11-16, branch `f1-profile-secrets`,
  `tests/test_profile_api_secrets.py` (walks every `/api/profiles*` rule in the
  URL map).*
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
- **Per-profile master switch (`automations_enabled`).** A top-level key in
  `profile_config.json`, not under `scheduler` or `scheduled_posting`, because it
  governs both. While it is off, nothing acts for that profile:
  - **The background loops.** `pm.automations_enabled(config)` is the first check
    in each engine's `_tick_profile` (`scheduler.py`, `post_drain.py`,
    `first_comment_drain.py`), ahead of every feature flag, and in each engine's
    `run_now`.
  - **Manual jobs.** `dashboard._automations_refusal` is the first statement of
    every route that calls `run_job` or an engine's `run_now`. Those routes return
    409 with the reason before any side effect.

  `tests/test_automations_master_switch.py` fails any new job route that lacks
  the guard. An absent key is on, so a profile that never set it behaves as before.
  A present key is on only when it is `true`, so a value that does not clearly
  mean on stops things. The switch never writes an individual flag, so turning it
  back on restores exactly what was configured.

  **What it cannot stop:**
  - a job that is already running;
  - anything outside the dashboard process (CLI and `tools/` runs, another
    dashboard process).
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
"Schedule" route, the CLI (`tools/run_scheduled_posts.py`), and the automatic
drain, per channel (§10.7). `buffer_client.create_post()`
(`buffer_client.py:431`) is the sole GraphQL mutation that actually creates a
post; `schedule_pass` is its only live caller.

**Metadata and alt text come from the declaration (P3).** `build_post_input`
and `create_post` take `metadata=None, alt_text=None` after the old
arguments. `metadata` is added under `"metadata"` only when it is not None.
A non-blank stripped `alt_text` on a post with an image makes the asset
`{"image": {"url": ..., "metadata": {"altText": ...}}}`; otherwise the asset is
`{"image": {"url": ...}}` as before, because Buffer requires `altText`
whenever the image's `metadata` is sent. `schedule_pass` passes these keywords
only for a channel that declares a `metadata_builder` or `alt_text_column`
(`channel_post_extras`), so LinkedIn and X call `create_post` with exactly the
arguments they always did. The builder runs before the image upload, and one
that raises fails the row at `prepare` with nothing hosted.

**Where the link goes is a platform decision** (`compose_for_platform`, reading
the channel's `link_placement`, §10.9), not a CSV column: LinkedIn's link is
posted as a first comment by the browser pass (Buffer's `firstComment` field is
paywalled and rejects the whole post on the free plan); X has no comment pass
at all, so the link is appended to the tweet body instead.

**`link` is the canonical name of the link slot; `first_comment_link` is an
accepted alias, forever.** `csv_pipeline.row_link(row)` reads either (the older
name wins), and every place that read `first_comment_link` — validation, the
queue, `schedule_pass`, the state entry — goes through it. `link` is not in
`COLUMNS`, because `row_key` iterates `COLUMNS` and adding it would move every
key. A row that fills both names with different values is rejected on every
channel rather than letting one win.

> **Measured, 2026-09-19 (`.dev/DECISIONS.md`).** X's `metadata.twitter.thread`
> field — a free threaded self-reply — is not paywalled the way LinkedIn's
> `firstComment` is, but a live `--create` run through the Buffer spike showed
> it **drops the base post's image**. Trading the image for a threaded reply
> was the wrong trade for this project's posts, which is why X uses
> link-in-body rather than a self-reply thread. `--thread-text` stays in
> `tools/buffer_spike.py` as the evidence for that decision, not as a path
> production takes.

**Validation is platform-aware.** `validate_row()` enforces a body limit only
for a channel that declares a `text_limit` (§10.9): X's 280 characters, the
same number as `platform_policy.policy_for(X).length.hard_max`, counted by
`counted_length()` with every URL charged at X's fixed t.co cost, not its
literal length. **`queue_rows()` validates for the row's own channel**
(`state.platform`), so an over-limit X row is rejected at queue time and written
nowhere; before P2 it validated with no channel, queued the row, and failed it
later in `schedule_pass`. A row already in state that is over the limit now
reads as `rejected` rather than `skipped`, because validation runs before the
duplicate check.

**A topic-only row is refused on a channel with a `text_limit`** (X1):
`X needs post_text: topic generation writes LinkedIn-length posts and is not
supported on X.` The generator has one voice, and its prompt is out of bounds
(DEC-005), so this is refused at queue time while a person is looking. A
topic-only LinkedIn row is unchanged. As a last check, `schedule_pass` runs
`text_limit_problem()` on the body as it will actually be sent, after
composing and before any image upload or Buffer call. A body over the limit is
`FAILED` at stage `validate`, never trimmed. `validate_row` uses the same
function, so the two checks cannot disagree. `PipelineState` takes a `platform=` argument. A channel whose
`state_path` is `legacy` (LinkedIn) keeps the original on-disk state file path
verbatim, while every other platform gets its own file — the same "isolation
is the path" pattern the post store uses (§1.10).

**`comment_pass()` refuses outright for any platform but LinkedIn** — X has no
comment pass, because the link already rode in the body; asking it to comment
would imply a browser action that makes no sense for this platform. The CLI
names this explicitly rather than trying and failing.

**Waiting rows go out in clock order (F2).** `pending_rows` and `held_rows`
order by date, then by `window_rank(time_window)` (the window's start minute
in `buffer_client.TIME_WINDOWS`, so morning, afternoon, late_afternoon,
evening, night), then by
queue time. An undeclared window name sorts after every declared one. Before
F2 the window sorted by name, so a day's evening row went ahead of its morning
row when slots were scarce.

**Re-dating queued rows (F3).** `redate_pending(profile, channel, per_day,
start=None, rng=None)` gives a channel's PENDING rows (not HELD, not
SCHEDULED or later) consecutive dates in their current order, `per_day` a day
in clock-ordered windows, from `start` (a `(date, window)` pair or a datetime)
or else the next window not yet begun. A start that has begun is refused with
nothing written. It changes only each stored row's `date` and `time_window`,
and writes the state file once, atomically. The minute is still picked at
schedule time by `bc.due_at`. **Keys never move:** `row_key` hashes the date
and window, so the stored row also gets `_queued_key` (`PINNED_KEY`), the key
it was queued under. `schedule_pass` keys each row through
`effective_row_key`, which honours the pin only when the state entry under it
pins the same key and holds the same content. Otherwise, as for a CSV that
happens to carry the field, the row is keyed from its content. The same CSV
queued again therefore still reads as already queued.

**Per-day counts (F4).** `per_day` may also be a callable `date -> int`, 0 to
the number of windows, so each day gets its own count; a day given 0 gets no
rows. `windows_for=None` (keyword) is an optional callable `n -> window names`
choosing which windows a day with `n` rows uses; the default is the earliest
`n`, as with an int. On the start day only windows from `start` on are used.
A count out of range, a bad window list, or 400 consecutive days placing
nothing (`REDATE_MAX_EMPTY_DAYS`) raise `ValueError` before the write. The
calendar itself lives in the caller (for Tinderbox, operator scratch), not
the app.

**Opt-in windows (F5, DEC-046).** `buffer_client.TIME_WINDOWS` declares five
windows, Pacific: morning 8-11, afternoon 12-15, `late_afternoon` 15-17,
evening 17-20, `night` 20-22. It is the lookup and validation table: `due_at`,
`validate_row` (so a CSV row) and `window_rank` accept and order all five.
`buffer_client.DEFAULT_WINDOWS` is the three a caller gets without naming
any. Everything that picks windows on its own iterates only those:
`_window_slots`, `_first_open_slot`, `_start_slot`, and `redate_pending`
without `windows_for` (cap 3, start must be a default window). With
`windows_for`, `redate_pending` may name any of the five, `per_day` may go up
to 5, and the default start is the next of the five not yet begun. So the new
windows are placed only when a caller names them, and LinkedIn, X and
Pinterest placement without one is unchanged
(`tests/fixtures/window_placement_golden.json`, generated before F5). The
dashboard's add-one-post dropdown still offers the three defaults.

*Verification anchor (10.2): 2026-11-20, branch `f5-extra-windows`
(`tests/test_extra_windows.py`, `tests/test_window_defaults_golden.py`);
2026-11-17, branch `f4-per-day-count`
(`tests/test_redate_pending.py`); 2026-11-16, branch `f3-redate-pending`
(`tests/test_redate_pending.py`); `f2-window-order`
(`tests/test_window_order.py`); before that 2026-11-14, branch
`p3-buffer-metadata` (metadata and alt text, `tests/test_channel_metadata.py`); before that
2026-11-11, branch `x1-x-csv-drain`;
`tests/test_channel_extras.py` carries the CSV-to-createPost `link` proof and the
X queue-time limit, and `tests/test_x_drain.py` covers the topic-only refusal
and the send-time length check.*

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
LinkedIn's flat fields. The named variable's value goes to Buffer as the Bearer
token, so only `BUFFER_API_KEY` or `BUFFER_API_KEY_<SUFFIX>` is ever read: any
other name raises here, whether it came through the dashboard or a hand edit. An unknown platform with no overlay is an error, not a
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

### 10.7 The post drain covers every channel (X1)

The drain (`post_drain.py`) feeds HELD rows, never PENDING ones, into Buffer as
slots free up. **One check covers every channel in `channels.REGISTRY`** that
holds rows for the profile, and each channel uses its own state file
(`_drain_held_rows(profile, platform)`), its own Buffer channel
(`_scheduled_channel_id(profile, platform)`) and its own key. The switch, the
interval, the busy check and the master switch stay per *profile*.

**The gate runs before any Buffer call, the slot read included.** For each
channel with held rows, the check calls `gate_fn` (`dashboard._drain_gate`):
`pm.resolve_scheduled(profile, platform)`, then `_production_gate`, the same
`check_declared_production(..., allow_production=True)` the Schedule button
applies to `BUFFER_HANDLE` channels. Only then does it read slots with that
channel's key. Missing settings, a missing key variable, or an empty identity
are recorded as an `error` run naming the platform, and nothing goes to Buffer.
A declared-production identity proceeds, exactly as it does through the
Schedule button (DEC-013): the dashboard is production doing its job (§9).

**One feed job per profile, not per channel.** Every channel with room goes
into one `submit_feed(profile, feeds)` call, so one `post_drain` job
(`_drain_feed_profile_job`) runs `_drain_feed_job` for each channel in turn. A
job per channel would be refused by the per-profile busy check, and the second
channel would wait out a whole interval. Each channel re-reads its slot count
(`max_age=0`) before its pass, so two channels in one Buffer organisation never
over-send. One channel failing does not stop the next.

**`_drain_feed_job` is the Schedule button's steps for HELD rows.** It resolves,
builds `PipelineState(profile_name=..., platform=...)`, applies
`_production_gate`, reads slots, and calls `_gated_schedule_pass`, the helper
`_scheduled_schedule_job` also uses. That helper is `schedule_pass(...,
platform=, identity_slug=, key=, allow_production=True)`, with the same
refusals. There is one copy of the gate, not two. Since D-022 the helper runs
under the channel's publisher lock and re-reads the state first (§10.11).

`recent_runs` entries carry `platform`; `status()["held_count"]` is the total
across channels; the panel's slot count is read for the first channel holding
rows, gated the same way. The toggle accepts a profile whose only configured
channel is X.

*Verification anchor (10.7): 2026-11-11, branch `x1-x-csv-drain`;
`tests/test_x_drain.py` asserts channel and key on the recorded Buffer calls.*

### 10.8 What is not covered here

`docs/SCHEDULED_POSTING.md` is the operator runbook for this path. It predates
most of the X work. It now covers X's `post_text` requirement, X's length
limit and the drain feeding X, but not yet the CLI's `--platform` or
`--allow-production` flags on the schedule pass.

### 10.9 Platform ids and the channel registry

**One spelling of every platform id.** `linkedin_automation/platforms.py`
defines `LINKEDIN`, `X`, `PINTEREST`, `YOUTUBE`, `ALL` and `DEFAULT`
(`ALL` is the Buffer and engagement ids; YouTube is not in it, §10.13). The older names
(`platform_policy.LINKEDIN/X`, `post_store.LINKEDIN/X`,
`csv_pipeline.X_PLATFORM`, `profile_manager.SCHEDULED_DEFAULT_PLATFORM`) are
imports of it, not second definitions.

**A publishing channel is a declaration in `linkedin_automation/channels.py`.**
Shared publishing code reads the declaration's fields and never asks which
channel it is. The fields are:

| Field | LinkedIn | X | Pinterest | Read by |
|---|---|---|---|---|
| `label` | LinkedIn | X | Pinterest | validation and settings messages |
| `link_placement` | `first_comment` | `body` | `metadata` (the pin's `url`) | `compose_for_platform`, the queue's `first_comment` flag, the CLI's comment pass |
| `text_limit` | none | 280 chars, a URL counts 23 | 500 chars, a URL counts its length | `validate_row` (length, and refusing topic-only rows), `schedule_pass`'s send-time check, both through `text_limit_problem` |
| `identity_check` | `ungated` (the browser checks the account at comment time) | `buffer_handle` | `buffer_handle` | `verify_identity`, `schedule_pass`'s production gate and identity guard, the dashboard's account header, settings view and schedule job |
| `config_layout` | `flat` | `overlay` | `overlay` | `resolve_scheduled`, the settings view and save |
| `key_env` | fixed `BUFFER_API_KEY` | each profile's overlay names it | each profile's overlay names it | `resolve_scheduled`, the settings view and save |
| `state_path` | `legacy` | `platform_dir` | `platform_dir` | `PipelineState` |
| `reconcile_statuses` | `sent` | `sent`, `error` | `sent`, `error` | `reconcile_published` |
| `extra_columns` | none | none | `title` (required, 100), `alt_text` (500), `board` (required) | `row_key`, `unknown_columns`, `validate_row`, the dashboard form route |
| `metadata_builder` | none | none | `_pinterest_metadata` | `schedule_pass`, through `channel_post_extras`: its result is createPost's `metadata` |
| `alt_text_column` | none | none | `alt_text` | `schedule_pass`, through `channel_post_extras`: the column's stripped value is the image's `altText` |
| `image_required` | no | no | yes | `validate_row` (`<label> needs an image: every pin is an image.`) |
| `board_lookup` | no | no | yes | `queue_rows` (the board list is required), `schedule_pass` (boards read once per pass, row resolved), the dashboard's CSV queue route (§10.10) |
| `publisher` | `buffer` | `buffer` | `buffer` | `REGISTRY` is built from it (Buffer channels only); `youtube_channel` refuses unless YouTube is `youtube_api` (§10.13) |

**Only fields something reads are declared.** The rest of the design
(`media`, `drain`) arrives with the phase that first reads it; see
`PLATFORM_REFACTOR.md` in the strategy repo. The design's `build_metadata`
and `lookups` arrived in P3 as `metadata_builder` and `board_lookup`.

`config_layout` and `key_env` are checked against each other when a channel is
declared: a flat channel must fix its key, and an overlay channel must not.

**Extra columns and row identity.** `extra_columns` is a tuple of
`ExtraColumn(name, required=False, limit=None)`; a duplicate name is refused at
declaration. Pinterest (P5) is the first shipped channel that declares any. For
a channel that does:
- `validate_row` rejects a missing or whitespace-only required value, and a
  stripped value longer than `limit` characters, naming the channel, column,
  length and limit. It is never truncated.
- `unknown_columns` does not report `link` or the channel's declared extras;
  everything else is still reported, and an undeclared column never affects a key.
- `row_key(row, platform)` hashes the seven base slots exactly as before (the
  link slot through `row_link`), then appends `\x1e` and the sorted
  `name=value` pairs of the non-empty extras, and only if there is at least one.
  Every existing LinkedIn and X row therefore keeps its key; rows with every
  extra empty equal their base key; declaration order does not matter. A caller
  must pass the channel for the key to include extras: `queue_rows` and
  `schedule_pass` do. The dispatch left the seven other no-platform sites (the
  dashboard's `PipelineState(profile_name=...)` constructions and
  `first_comment_drain.py`) to P4.
- The dashboard form route stores exactly `COLUMNS`, plus `link` and the route
  channel's extras only when the request body fills them.

**An undeclared name gets the old non-LinkedIn branch.** `channels.resolve()`
returns the declaration, or for any other name `channels.undeclared()`. That
reproduces what every site's `else` did before the registry existed, so routing
through it changed nothing.

The dashboard's `_req_platform` validates against `channels.REGISTRY`, not the
comment generator's `platform_policy.PLATFORMS`. A channel that is only
published to never needs a voice.

**The scheduled-posting panel is rendered from the registry (P4).** `GET /`
renders `templates/dashboard.html` through Jinja with `channels`, a list of
`{id, label, config_layout, extra_columns}` from `channels.REGISTRY` in
registry order (`dashboard._page_channels`). The platform pills are one button
per entry, the page's `SCHP_CHANNELS` is the same list, the default platform is
its first id, and the settings and identity lines read labels through
`schpChannel(id)`. The queue payload's `config` carries the channel's `label`
and `config_layout`, so the unconfigured banner (`schpPaintConfigWarn`) names
the channel and shows the flat-only identity line without a platform id in the
page. A declared channel therefore appears in the panel with no template change.

**Two tests hold this:**
- `tests/test_channel_registry.py` parses `linkedin_automation/` and `tools/`
  and fails on any branch on a platform id outside `platforms.py` and
  `channels.py`. A branch means a comparison, a `case`, `startswith`, or a
  dict-literal dispatch keyed by ids. It allows three named exemptions:
  - `post_store.py`, the engagement store;
  - `tools/x_dump.py`, X discovery;
  - `tools/buffer_spike.py`, the spike.

  It also fails if an exemption goes stale.
- `tests/test_platform_golden.py` runs real scheduled-post rows (scrubbed) and
  synthetic rows through the publishing path, under each declared and
  undeclared name. It compares the output against a capture taken from the
  code before the registry existed. `tests/test_channel_extras.py` additionally
  recomputes every golden row's `row_key` with and without a channel.

*Verification anchor (10.9): 2026-11-14, branch `p5-pinterest`; the golden's
undeclared probe is `mastodon` since P5's rename commit (DEC-021), otherwise
unchanged from P1; `tests/test_channel_metadata.py` asserts the P3 fields on
the captured createPost input; `tests/test_dashboard_registry.py` renders the
three shipped pills and a test channel's, and drives its settings, CSV and
banner. Refreshed 2026-11-22, branch `v1-youtube`: the `publisher` field;
`tests/test_channel_registry_golden.py` pins every Buffer channel's fields
to their values before it. Refreshed 2026-11-26, branch `i1-instagram-buffer`:
I1's five fields (§10.14) are popped at their defaults and Instagram is
pinned in full.*

### 10.10 Pinterest (P5)

Pinterest is a declaration (`channels.PINTEREST`, after X in `REGISTRY`), not a
branch. A pin goes through the same `schedule_pass`, the same `BUFFER_HANDLE`
identity check and the same production gate as an X post.

**The CSV.** `post_text` is the pin description (top-level `text`, at most 500
characters). `title` (required, at most 100), `board` (required), `alt_text`
(at most 500), `link` (or `first_comment_link`) and `image_path` (required: every
pin is an image). A topic-only row is refused, as on X. Nothing generates pin
text; `platform_policy` has no Pinterest entry.

**The createPost input** is the shape `pin.publisher` sends
(`buffer_publisher.build_create_post_input`): `text` is the description, the
link never enters it; `metadata = {"pinterest": {"boardServiceId", "title",
"url"}}`, `title` and `url` only when filled, from `_pinterest_metadata`, which
refuses a blank board; the asset is `{"image": {"url", "metadata": {"altText"}}}`
when `alt_text` is filled. `mode`, `schedulingType` and `dueAt` follow this
repo (`customScheduled`, `automatic`, the row's window).

**Board lookup.** `buffer_client.channel_boards(channel_id, key, session)` runs
`pin.publisher`'s `GetChannelBoards` query and returns `[{id, serviceId, name}]`.
It is never cached. `resolve_board(boards, name)` is pure and exact: case and
whitespace count, so the row's `board` is matched unstripped. It returns the
board's `serviceId`, never Buffer's `id`. No match raises `BoardNotFound`
listing every board name; two matches raise `BoardAmbiguous`.

- `queue_rows(..., boards=)`: a `board_lookup` channel with no board list raises
  `BoardLookupUnavailable` before anything is written. A row whose board does
  not resolve is rejected with the resolver's message.
- `schedule_pass(..., fetch_boards=)`: after the identity gate and before any
  upload, the boards are read once per pass (default `bc.channel_boards` with
  the pass's key and session). A failed read raises `BoardLookupUnavailable`
  (`Nothing was created`). It subclasses `IdentityRefused`, so every caller
  that stops on an identity refusal stops on it (DEC-022). A row whose board no
  longer resolves is `FAILED` at stage `board` (`STAGE_BOARD`), after
  validation and before the HELD slot check, with no upload and no createPost.
- The dashboard's queue route (`scheduled_add_rows`) reads the boards with the
  profile's resolved Pinterest channel and key and passes them to
  `queue_rows`. A missing config or a failed read refuses the whole upload with
  the error, and nothing is written. The drain and the Schedule button pass
  nothing new: `schedule_pass` reads the boards itself.

**Profile config.** `scheduled_posting.platforms.pinterest = {buffer_channel_id,
identity_slug, buffer_api_key_env, drain}`, an overlay like X's (§10.4). The
identity is the Buffer channel's `name`, which for a Pinterest channel is the
account's handle. The overlay's `drain` value is resolved but nothing reads it
yet: the drain switch is per profile (§10.7).

**One publisher per channel.** `pin.publisher` also publishes to Pinterest
through Buffer. Once this repo posts pins to a channel, `pin.publisher`'s
scheduled task for it (`PinPublisherBufferDrain`) stays Disabled: two
publishers draining one channel would double-post.

*Verification anchor (10.10): 2026-11-14, branch `p5-pinterest`;
`tests/test_pinterest.py` compares the captured createPost input with
`pin.publisher`'s shape and covers board lookup, the row rules, identity and the
drain.*

### 10.11 Headless top-up and the publisher lock (F6, DEC-048)

`linkedin_automation/topup.py`, run as `tools/run_scheduled_posts.py topup
--profile P --platform C [--reserve N] [--dry-run] [--log-file F]`, is the one
headless path that sends **PENDING** rows to Buffer. A Windows task
(`tools/setup_topup_task.ps1`, 07:00, 13:00 and 21:00 local) runs it; the
dashboard does not need to be open. In order, it:

1. resolves the channel with `pm.resolve_scheduled` (no key fallback) and reads
   the overlay's top-up config (`topup_config`): `approved_boards`, `cadence`
   and `topup_reserve` (default 2) in `scheduled_posting.platforms.<C>`.
   Missing `approved_boards` or `cadence` is a refusal, never a default;
2. applies `check_declared_production(..., allow_production=False)`: a
   declared-production identity is refused (exit 3);
3. refuses while the post drain is on for the profile (the engine's
   `drain_config(...)["enabled"]`) or the overlay's own `drain` is truthy;
4. takes the channel's publisher lock and re-reads the state under it;
5. reads `scheduled_slots(max_age=0)`; `N = free - reserve`; `N <= 0` is a
   clean exit 0;
6. if the first PENDING row's window has begun, re-dates every PENDING row
   with `redate_pending` and the cadence (`Cadence.per_day`,
   `Cadence.windows_for`), from the first window that has not begun and is after
   the last SCHEDULED `due_at` (DEC-033: keys and order do not move);
7. walks PENDING rows in `_waiting_sort_key` order. A row on a board not in
   `approved_boards`, or not resolvable in `channel_boards` (read once), or
   whose link fails `link_problem` (2xx only, no redirect hop since redirects
   are not followed, a title that is present and neither "Redirecting..." nor
   "Page not found"), is skipped, stays PENDING, and the next row takes its
   place. Each URL is fetched once per run;
8. sends the chosen rows one `schedule_pass` call at a time (`max_new=1`, the
   boards read in step 7 injected) and stops at the first row that does not
   come back SCHEDULED (exit 1);
9. prints and logs one summary line: free, reserve, N, scheduled, skipped with
   reasons, re-dated count, the last Buffer due time, the first pending slot.

`--dry-run` does steps 1 to 7 on a copy of the state whose `save()` is a no-op:
no Buffer write and no state write. It reads slots and boards and fetches
links, and prints what it would send.

**The publisher lock** (`publisher_lock.channel_lock(state_path, holder)`) is an
OS byte-range lock on `<state file>.lock` (`msvcrt` on Windows, `fcntl`
elsewhere). The OS drops it when the holder dies, so a crash never leaves a
channel locked. It never waits: the second publisher refuses with the holder's
name. `topup` holds it for its whole run. `dashboard._gated_schedule_pass`
(the Schedule button and the post drain's feed) holds it around
`schedule_pass`, after `PipelineState.reload()`, and first drops rows that
changed since the job read them (`_rows_still_schedulable`): a row now
scheduled, a row stored differently, or a row pinned to a key it no longer
hashes to. Without that, a stale copy of a row a top-up had re-dated would hash
to a new key and be posted a second time. The CLI's `schedule --csv` pass does
not take the lock.

*Verification anchor (10.11): 2026-11-20, branch `f6-topup`;
`tests/test_topup.py` covers N and the reserve, order, each board and link
failure, the overdue re-date, both directions of the lock (a drain and a
Schedule-button job run from inside a live top-up are refused), the
read-then-top-up interleaving, the drain-on refusal, a dry run against a
snapshot, missing config, and the task script parsed (never run) by
PowerShell's parser.*

### 10.12 Books and app holds (F7)

`linkedin_automation/books.py` moves the Tinderbox operator flow (DEC-042,
DEC-043, DEC-044, scratch `tinderbox_map.map_all`, `ops6.round_robin`,
`ops7.plan`) into the app. A channel opts in with
`scheduled_posting.platforms.<C>.books`: `content_root`, `series_order`,
`csv_board_series` (series whose CSV `board` is used), `board_map`
(`source_id -> board` for the rest) and `link_keep_prefix_series`. A channel
without it is refused by these commands; nothing else reads it. The app never
reads `$STRAT`: the board maps were copied into the overlay.

**Identity and dedupe.** A pin is its `pins.csv` `source_id`. State rows carry
no source id, so a state row is matched to a pin by its resolved `image_path`
(`PinIndex.pin_of_row`, case and separator folded). A pin in the state file in
any state, or in the hold file, is never queued again; two pins sharing an id
refuse the whole run.

**Mapping.** Order within a series: one cover per book (books in name order),
then round-robin across its books in file order. Board: the CSV board for a
`csv_board_series` pin, else `board_map`; it must be approved and resolve on
the channel's live board list (`topup.board_problem`, boards read once per
run). Link: a `link_keep_prefix_series` pin uses its CSV link; any other uses
`/books/<folder minus its series prefix>`, never the prefixed form
(DEC-043(2)); it must pass `topup.link_problem` (each URL fetched once). Then
`validate_row` on a provisional slot.

**Queueing.** Passing pins are queued PENDING with `queue_rows` (boards
injected) on the provisional slot, interleaved into the pending queue by
`merge` (the round-robin across `series_order`, existing rows emitted in their
own order: `ops7.plan`), then the whole pending queue is re-dated with the
overlay `cadence` from the first window after the last SCHEDULED `due_at`, via
`redate_pending(..., order=)`. `order` is an optional list naming every
PENDING key once; without it `redate_pending` is unchanged. After the re-date
the module asserts the pending order is the merged order and the existing
rows' relative order is unchanged; keys are pinned as in DEC-033.

**Holds** (`linkedin_automation/holds.py`, the only writer) live in
`<state dir>/pin_holds.json`, keyed by source id, each with the full source
row, the mapped row, `kind` (`board`, `link`, `review`, `operator`), reason,
`held_at` (kept across updates) and `checked_at`. **Held pins are not in the
state file**, so `topup`, `pending_rows` and every state reader are
unchanged, and `csv_pipeline` has no new state. A failing pin goes to holds;
`recheck-holds` re-runs the checks on `board` and `link` holds; `review` and
`operator` holds move only by `release-hold`, which re-runs the checks (a pin
that still fails stays held under the failing check's kind). The state is
saved before the hold file, so a crash between leaves a pin queued and still
listed as held, which dedupe reads as present.

**Commands** (`tools/run_scheduled_posts.py`): `books`, `holds`, `add-book`,
`recheck-holds`, `release-hold`, `hold`, each with `--dry-run` where it
writes. Each resolves the channel (`pm.resolve_scheduled`), runs the
production gate, and holds the channel's publisher lock (§10.11) for the whole
run, so it and `topup`, the drain and the Schedule button refuse each other.
A dry run works on copies whose `save()` is a no-op. **None writes to
Buffer**; `topup` sends what they queue.

**Dashboard.** `GET /api/scheduled/<p>/books` (books and holds, or
`configured: false`), `POST .../books/add`, `.../holds/recheck`,
`.../holds/release` call the same functions. A POST writes only with
`"confirm": true`; otherwise it is a dry run returning the plan, which the
Books and holds card shows in a confirm dialog first. The calendar's sources
include the hold files, so held pins appear in its `undated` side list.

*Verification anchor (10.12): 2026-11-22, branch `f7-books`;
`tests/test_books.py` covers config refusal and validation, parity with the
scratch mapper on two real books' structure (`tests/fixtures/books`), board,
link and row failures to holds, the round-robin and merge, the re-date keeping
keys and order, dedupe across states and holds, dry runs against a snapshot,
the lock both ways, the routes' confirm and lock, and the calendar's held
list.*

### 10.13 YouTube Shorts (V1, DEC-058, DEC-059; layouts V3, DEC-069)

YouTube is a channel of the platform, published through the YouTube Data API
rather than Buffer. `channels.YOUTUBE` carries `publisher=youtube_api`; every
other channel carries the default `buffer`. `channels.REGISTRY` is the Buffer
channels only, so the queue, the drain, the Schedule button and the panel's
pills never see YouTube. `channels.CHANNELS` (and `channels.get`) holds all
four. `channels.resolve("youtube")` still answers the old else-branch, so no
Buffer site changed. `tests/test_channel_registry_golden.py` pins every field of
the three Buffer channels to their values before V1.

**Code.** `youtube_api.py` is the only module that imports Google: credentials
from the stored token, the refresh, `channels.list mine`, one resumable
`videos.insert`, one `thumbnails.set`, and the installed-app consent. It never retries: an upload
error stops the run. `youtube_channel.py` holds the rest, ported from
video-publisher (reference only, not imported).

**Scopes (Y1-S, D-032).** `tools/youtube_auth.py` mints a new token with
`youtube_api.SCOPES`: `youtube.upload`, `youtube` and `youtube.force-ssl` (the
only scope Google's comment endpoints accept). It stores the scopes Google
says it granted (`granted_scopes`, else what the flow asked for) and prints a
NOTE if force-ssl was not granted; its channel check and its refusal of a
wrong channel are unchanged. A stored token is always built and refreshed
with **its own stored scopes**, never the constant, because a refresh naming
a scope the token was never granted fails `invalid_scope` and would stop every
upload. A token that records no scopes (teratomahouse's) is built with
`LEGACY_SCOPES` (`youtube.upload`, `youtube`), the request it sent before.
`yt.comment_scope_problem(profile)` is the comment gate (§11).

**Config.** `scheduled_posting.platforms.youtube` of a profile:
`vp_brand`, `vp_root`, `channel_id`, `content_root`, `publish_time`, `tz`,
`per_day`, optional `quota {daily_limit, upload_units, comment_units,
thumbnail_units}` (10,000 / 1,600 / 50 / 50 by default) and `category_id` (27). Unknown keys are refused. The
profile must be registered. `topfive` is publishing-only, automations off.
Three optional layout keys (V3) say how the profile's content folder is laid
out; each default is the topfive layout:

| key | default | teratomahouse |
|---|---|---|
| `item_prefix` (letters only) | `C` | `TH` |
| `video_suffix` (ends in `.mp4`) | `-vertical.mp4` | `-vertical-post.mp4` |
| `metadata_format` | `shorts_section` | `title_sections` |

A bad value refuses at config load. The metadata format is only ever what the
config says; nothing detects it from a file, and each parser skips a file in
the other layout (`no YouTube Shorts section in youtube.md`, `youtube.md has
no ## Title section`).

**Files**, under `data/<profile>/youtube/`, all written temp-then-rename:
`youtube_state.json` (one record per video, keyed by its source folder name
without video-publisher's `-yt`), `quota_ledger.json` (units per Pacific
date, charged before each call), and `oauth_token.json` (from
`tools/youtube_auth.py`; never printed). An unreadable state file refuses: it
never reads as empty. Every paid call is also logged to `api_usage.jsonl`
first.

**States.** `queued`, `attempting` (saved before the upload call), `uploaded`
(shown as *scheduled* while `publish_at` is ahead, *live* after) and `failed`.
Any `attempting` record refuses the whole next upload run until a person has
checked Studio and set the record by hand: nobody knows whether that upload
created a video. A Google quota error is the one exception: nothing was
created, so the item returns to `queued`.

**Commands** (`tools/run_scheduled_posts.py`). Each takes the declared
production gate on `channel_id` and the channel's `publisher_lock` on the
state file:

- `youtube-intake`: queues every new `<item_prefix>-NNN-<slug>/` under
  `content_root` in number order (by the number, not the name, so `C-50`
  comes before `C-100`), with its `youtube.md` text verbatim and the one mp4
  ending in `video_suffix`. `ready-to-publish/` and every folder whose name
  starts with `_` (`_posted/`, `_superseded/`) are skipped as not video
  folders. Over a YouTube limit is a skip, never a cut.
  - `shorts_section`: the `- **Title:**`, `- **Description:**` and
    `- **Tags:**` bullets of the `### YouTube Shorts` section.
  - `title_sections`: the bodies of the `## Title`, `## Description` and
    `## Tags` headings, each running to the next heading of any level; the
    rest of the file (the H1, the `Post: ... Thumbnail: ...` note) is
    ignored. A section given twice is a skip.
  - Both: the title joined to one line, description paragraphs with their
    indentation removed and joined by one blank line, tags comma separated,
    then the same limits (title 100, description 4,900, tags 500 as YouTube
    counts them).
  The number is what `c_number` returns and what the record's `c_number`
  field holds for any prefix; dates, the upload order, the thumbnail backfill,
  status and the import all order by it.
- `youtube-schedule`: refuses unless `vp_brand` is in video-publisher's
  marker, the token exists and nothing is `attempting`. It dates undated queued
  items: the next day with a free slot at `publish_time` that is at least 15
  minutes ahead. Held days are never reused, and existing dates never change.
  It then checks that the token answers for `channel_id` (1 unit), and uploads
  dated items earliest first. Each upload is private with `status.publishAt`,
  refused if under 15 minutes ahead, and refused if the quota day would go over
  the limit. It stops at the first error.
- `youtube-status`: read-only, from the state, the ledger and the marker.
  Each line ends with the item's thumbnail outcome.
- `youtube-thumbnails [--max N] [--dry-run]` (V2): the backfill. It refuses
  unless `vp_brand` is in the marker. It covers items with a `video_id` and a
  poster whose outcome is not `set`, in C-number order. It checks the token's
  channel (1 unit), then sets each one, and stops at the first item that does
  not come back `set`. `--dry-run` lists each item and its units and writes
  nothing except the lock file.
- `youtube-import` (V1-M): reads video-publisher's `content/<brand>/{queue,
  published}` manifests and the latest `publish_log` row per item, opened
  `mode=ro` and closed at once, never through its `get_connection`. It
  excludes Instagram. It is idempotent. It never moves an existing date. Two
  video ids for one item are a conflict, reported and left alone.
- `youtube-cutover`: checks the token (no network), then checks that
  video-publisher has no `attempting` item and no undated queued item it has
  not published. It re-runs the import, adds `vp_brand` to the marker, and
  prints the state before and after. `--dry-run` writes nothing except the
  lock file.

**Thumbnails (V2, DEC-068).** The poster is exactly
`<source_folder>/<folder name>-poster-vertical.png`. There is no other name
and no fallback. The folder must sit under `content_root`, so imported `aif-c01`
items, whose folders are in video-publisher, have none. A JPEG or PNG at or
under 2 MB is sent as it is. Anything else becomes a JPEG copy under 2 MB at
`data/<p>/youtube/thumbnails/<item>.jpg` (Pillow), and the content folder is
only read. After an upload is saved as `uploaded`, `youtube-schedule` runs one
thumbnail step and records `thumbnail` on the item:
- `set`;
- `none` (no poster, and no call);
- `failed`, with `thumbnail_reason`;
- `deferred_quota` (the ledger would go over the limit, and no call is made).

The ledger and `api_usage.jsonl` are charged before the call. No outcome
fails or stops the upload run, and nothing retries. A 403 whose message names
thumbnail permissions is stored as `channel not verified for custom thumbnails
(phone verification in YouTube settings)`. That message match is inferred from
Google's documented error, not from a capture.

**One publisher (DEC-058(2)).** The marker is
`<vp_root>/config/moved_to_platform.yaml`, `{brands: [names]}`. The platform
uploads only for a listed brand. video-publisher's `src/moved.py` refuses a
listed brand in `publish`, `scheduled_publish.py`, `schedule_youtube.py`,
`retry-failed` and `_publish_youtube_item`, before any `publish_log` row. An
unreadable marker refuses on both sides. No task or drain runs any of this.

*Verification anchor (10.13, Y1-S): 2026-11-26, branch `y1s-comment-scope`;
`tests/test_youtube_channel.py` pins `SCOPES` and `LEGACY_SCOPES`, checks
consent asks for `SCOPES`, auth storing the granted scopes (all three, and
force-ssl unticked with its NOTE), and runs the real `credentials_from_token`
and `refresh` against an offline token endpoint that refuses an ungranted
scope as Google does: a token stored with the old two scopes refreshes and
uploads through `schedule`, a token with no scopes refreshes with the legacy
two, a new token with its three; credentials built with `SCOPES` fail on the
same endpoint. Building with `SCOPES` again turns two of them red. Live: the
real topfive token stores the old two scopes, teratomahouse's none.*

*Verification anchor (10.13, V3): 2026-11-25, branch `v3-intake-layouts`;
`tests/test_youtube_layouts.py` holds a golden of the topfive import, intake
(dry run and real), schedule dry run and status on fixtures, taken from main
732223c before V3 and compared line for line and field for field. It also
covers the layout keys and their refusals, the `title_sections` parser on six
synthetic copies of the real files' structure, CRLF and a BOM, the heading
boundary, every skip reason and the limits uncut, each parser refusing the
other layout, the video suffix rule, `_` folders, and the Teratoma House
layout end to end (number order with unpadded names, dates, posters, schedule
dry run). Nine mutations each turn it red. The real `topfive` dry run was
byte-identical before and after, and the nine real topfive files parse
identically under both versions.*

*Verification anchor (10.13, V2): 2026-11-24, branch `v2-youtube-thumbnails`;
`tests/test_youtube_thumbnails.py` covers the exact file name rule and the
content-root check, conversion over 2 MB and of a non-PNG with the original
untouched, each of the four outcomes, the 403 message both ways, ledger and
usage charged before the call, a failed thumbnail leaving the upload and the
run intact, and the backfill: no marker, dry run writing nothing, skips,
stop on error, wrong channel, quota, production gate and lock. Nine mutations
each turn it red.*

*Verification anchor (10.13): 2026-11-22, branch `v1-youtube`;
`tests/test_youtube_channel.py` covers config refusals, the eight topfive
layouts (synthetic text), dates (today vs tomorrow at the 15-minute edge,
held days, per_day 2, DST), the request body, every schedule refusal (marker
absent, other brand or unreadable, no token, attempting, quota, near time,
wrong channel, production), the attempting record saved before the call, the
lock both ways, atomic writes, the import's mapping and idempotence and
conflicts, cutover's refusals and dry run, auth, the calendar route, and a
socket guard shown to fail on a real connect. video-publisher's
`tests/test_moved_brands.py` covers each refusing path both ways.*

### 10.14 Instagram through Buffer (I1, D-030, DEC-071, DEC-072)

Instagram posts (images and Reels) are a Buffer channel, `channels.INSTAGRAM`,
declared after Pinterest. Queue, schedule, lock, reconcile and the U1 calendar
are the shared Buffer paths; the calendar reads `channels.REGISTRY`, so
Instagram rows appear with no calendar change. Five fields were added to
`Channel`, last and with defaults, so every earlier declaration is unchanged
(`tests/test_channel_registry_golden.py` pops them at their defaults and
compares the rest with the 230f1fc capture):

- `video_column` (`video_path`): a row with a local mp4 there is a Reel.
  `schedule_pass` hosts it with `image_host.upload_video` and createPost
  carries one `{"video": {"url"}}` asset (`build_post_input(video_url=)`).
  A row never carries an image and a video.
- `video_max_bytes`: 300,000,000, from Buffer's help centre ("File size:
  Below 300MB" for Reels).
- `media_required`: a row with neither image nor video is refused.
- `max_hashtags`: 30, counted by `csv_pipeline.HASHTAG_RE` on the composed
  body inside `text_limit_problem`, so queue time and send time agree. The
  caption limit is the `text_limit`, 2,200 characters. Over either is refused,
  never trimmed.
- `preflight`: `(profile) -> refusal`, run once per schedule pass before the
  production and identity gates, raising `PreflightRefused` (a subclass of
  `IdentityRefused`, so every caller that stops on a wrong account stops on it).

`link_placement` is the new `REFUSED`: a row with a link is refused at queue
time, because a caption link is not clickable and dropping it would be silent.

**Metadata.** `_instagram_metadata` sends `{"instagram": {"type": "reel" |
"post", "shouldShareToFeed": true}}`, the two required fields of Buffer's
`InstagramPostMetadataInput`. The other seven optional fields are not sent.

**Video hosting.** `image_host.read_video` refuses a file that is missing,
empty, not `.mp4`, over the cap, has no `ftyp` box first, has no video track
(`hdlr` `vide`), or is not taller than wide (the `tkhd` size, before any
rotation matrix). `upload_video` stores it content-addressed as `video/mp4`, then
`verify_public_video` fetches the public URL anonymously (streamed) and
requires HTTP 200, an `ftyp` box and a `Content-Length` equal to the file size.

**Preflight** (`instagram_channel.preflight_refusal`). It refuses when the
overlay `scheduled_posting.platforms.instagram` is missing, has no `cadence`
in topup's format (`topup.parse_cadence`), or has `drain` other than exactly
`false`. It also refuses while video-publisher has items with
`platform: instagram` in `content/<vp_brand>/queue/`, where `vp_brand` and
`vp_root` come from the profile's YouTube overlay. An unreachable `vp_root` or an
unreadable manifest also refuses. video-publisher has a Graph API Instagram path
but nothing uses it: `publish_log` has no Instagram row and nothing is in
`published/`. So the YouTube marker (`brands: [...]`) is unchanged and there is
no Instagram moved marker. The overlay also carries `buffer_channel_id`,
`identity_slug` and `buffer_api_key_env`, resolved by
`profile_manager.resolve_scheduled` like X and Pinterest. The E2/E3 engagement
keys (`engagement`) share the same section.

**Reels from YouTube items** (`instagram-from-youtube --profile P
[--dry-run]`, `instagram_channel.from_youtube`). It proposes one row per item
folder with a vertical mp4 (`find_vertical_mp4` plus `read_video`) whose
video path is in no Instagram state row. The caption is copied verbatim:
- `shorts_section`: the item section's `Caption (draft)` and `Tags` (the
  first `## ` heading's bullets). An item without `Caption (draft)` is skipped.
- `title_sections`: `## Description`.

Either way every `#shorts` token is dropped, and the title is never used. A
caption over the limits is skipped with the reason. A dry run writes nothing.
Otherwise it writes `data/<p>/instagram/proposed_reels.csv` atomically, with
`date` and `time_window` blank and a `source_item` column, and queues nothing.

*Verification anchor (10.14): 2026-11-26, branch `i1-instagram-buffer`;
`tests/test_instagram_buffer.py` covers each refusal both ways (caption
2200/2201, hashtags 30/31, media, both media, link, missing, horizontal and
oversize video, cadence, drain, the video-publisher queue, unreachable root,
unreadable manifest), the createPost input at the transport for a Reel and an
image, video hosting and its verify step, both caption layouts, the dry run
writing nothing, and the calendar. Reverting `csv_pipeline.py` alone turns
17 of them red.*

---

## 11. Engagement on YouTube (Y1, D-027, DEC-061)

Comments post as our channel, through the YouTube Data API, in two places: on
other creators' videos from a watch list Jeff picks, and as replies under
viewers' comments on our own Shorts. `youtube_engage.py` holds the path;
`youtube_api.py` stays the only module that imports Google (the engagement
reads and the two inserts were added there). V1's config, token, quota ledger,
production gate and publisher lock are reused (§10.13). No scheduler job
knows about it. Two Windows tasks per profile can run it (Y2-S, below); nothing
ever approves a draft but a person.

**The schedule (Y2-S, D-033, DEC-076).** `tools/setup_youtube_engage_tasks.ps1
-Profile P [-Remove]`, which Jeff runs, registers
`LinkedInCommentorYouTubeMorning-P` (daily 07:30, 20-minute limit:
`youtube-morning --profile P --scheduled`) and `LinkedInCommentorYouTubeEngage-P`
(daily from 08:00, every 15 minutes for 13 hours, 10-minute limit:
`youtube-engage --profile P --scheduled`), both with `--log-file
data\logs\youtube-engage.log`, uv by full path, the current user logged on,
`StartWhenAvailable` and `IgnoreNew`, as the topup task. `--scheduled` refuses
with exit 2, before the lock and any file or network, unless
`engagement.auto_run` is `true`: `scheduled runs are off for P
(engagement.auto_run is not true)`. Without the flag nothing changes.
`youtube-morning` (`morning`) runs `find`, then `draft` with per-kind limits
(`morning_limits`): video comments up to today's ramp cap, replies up to
`MORNING_REPLY_DRAFTS` (5), each minus the drafts of that kind already
waiting (GENERATED, approved or not), floored at 0. It prints one line with
those counts. A refusal or error in find is printed and draft still runs; the
exit is the most serious of the two. An engage run that posts nothing makes no
Google call and spends no unit: no approved draft returns before a `_Google`
session exists, then the window and the gap, then the per-record caps, all
from the clock and local files; only a chosen record reaches `_post_one`.
With `--log-file`, every printed line also goes to the file (a task has no
console), so the log shows what was drafted, posted or refused and when the
next comment is allowed.

**Comment scope (Y1-S, D-032).** Every comment endpoint needs
`youtube.force-ssl` in the stored token (§10.13). Before any network call,
each comment path checks `yt.comment_scope_problem(profile)`, which reads the
token file only, and refuses with `the <profile> YouTube token cannot use
comments (missing youtube.force-ssl). Re-run: .venv\Scripts\python.exe
tools\youtube_auth.py --profile <profile> --client-secrets <path>`, spending no
unit. `find` still lands watch-list videos and skips only the reply scan
(exit 2, `REFUSED the reply scan`); `draft` refuses before any Google or
OpenAI call when anything is NEW; `engage` refuses before the identity check,
dedupe and insert, leaving the approval in place. `_Google.call` refuses any
`commentThreads.*` or `comments.*` op the same way as a backstop. Each dry
run names the refusal a real run would hit. `status_view` carries
`comment_scope`. A missing token is left to `token_problem`.

**Policy.** `platform_policy.policy_for("youtube")` is the strategist's
`content/engagement/youtube-register.md`, copied verbatim (DEC-005); the
register's bullet lists are laid out as prompt bullet lines, as LinkedIn's and
X's are. `policy_for("youtube", platform_policy.REPLY)` is the own-video reply
variant: the same policy with the register's two replaced fields
(`system_voice`, `extra_rules`). `VARIANTS` declares it; an undeclared variant
raises, never falls back. `PlatformPolicy.input_chars` is how much of an item's
text the draft prompt quotes: 500 for LinkedIn and X (the literal they always
used), 3,000 for YouTube, whose input is a title, description, chapters and up
to 20 top comments. The register writes banned openers capitalised, so the
generator's opener check lowers them (X's were already lower case: no change
there). `tests/fixtures/engagement/youtube-register.md` is the build-time copy
the golden test parses; nothing reads the strategy repo at runtime.

**Named registers (Y2-T, D-033, DEC-077).** A profile picks its YouTube voice
with `engagement.register`. Absent or `"default"` is the policy above,
unchanged. `"teratomahouse"` is `content/engagement/youtube-register-teratomahouse.md`,
copied verbatim (DEC-005), with its own-video reply variant.
`platform_policy.NAMED_REGISTERS` holds them as `{(platform, name): {variant:
policy}}`. `policy_for(platform, variant, register_name)` returns one, and an
unknown name or variant raises, never falls back. `register_names(platform)`
lists the names the config may use; any other value refuses at config load.
`AuthenticCommentGenerator(..., register_name=)` and `default_generator` build
in the profile's register. `register_for(rec, cfg)` gives the no-promotion
validator the same register's banned openers, at draft, approve and engage.
The fixture copy is `tests/fixtures/engagement/youtube-register-teratomahouse.md`.

**Config.** `scheduled_posting.platforms.youtube.engagement`:
`channel_name`, `channel_handle` (the validator's own-channel check; both
missing is itself a refusal), `watch_list`, a list of `{channel_id, title,
subscribers, added_at}`, `auto_run` (true or false, default false; any
other type refuses), the `--scheduled` switch, and `register` (a name from
`register_names("youtube")`, default `"default"`). Unknown keys refuse, at both levels; a channel id must
be `UC` plus 22 characters and may appear once. V1's `CONFIG_KEYS` gained
`engagement`, which publishing never reads. The watch list is edited by
`youtube-watch --add/--remove` or the dashboard, through
`add_to_watch_list`/`remove_from_watch_list`, which rewrite the stored config
file (never the default-merged one).

**Files**, beside V1's under `data/<p>/youtube/`: `posts_db.json`, the shared
per-platform `PostStore(platform="youtube")` (no LinkedIn reconcile runs on
it), keyed by videoId, or for a reply by the viewer's top-level comment id;
and `engage_ledger.json`: `comments` (every attempt with its outcome, kind,
creator and whether it may be live), `ramp_started_at`, `next_allowed_at`,
`attempting`, and cached `uploads_playlists`. Each YouTube record carries a
`youtube` object (kind, video id, creator channel, title, description, parent
id, outcome, comment id). V1's `youtube_state.json` is only read, for our own
videos' titles.

**The path** (each step a function; `YouTubeAdapter` wraps them as the E1
engagement adapter, §12):

- `youtube-discover --query Q` (`discover`): `search.list` type=channel (100
  units) and one `channels.list` (1 unit) for titles and subscriber counts. It
  lists suggestions, flags those already listed, drops our own channel, and
  **adds nothing**. The dashboard's discover box is the same call; its Add
  button is the separate add route.
- `youtube-find` (`find`): the watch list's uploads playlists (one cached
  `channels.list`, then `playlistItems.list`, 1 unit a page, at most 2 pages,
  stopping at the first video older than 7 days), and our channel's threads
  (`commentThreads.list allThreadsRelatedToChannelId`, 1 unit) that are not
  ours, have no reply from us and are under 7 days old. Both land NEW. A key
  already in the store, in any state, or in the ledger, is never re-added.
- `youtube-draft --limit N` (`draft`): the shared generator with the YouTube
  policy (the reply variant for replies). A video's input is its title,
  description, the description's chapter lines and up to 20 top comments
  (`commentThreads.list order=relevance`, 1 unit); a reply's is our video's
  title (local first, else `videos.list`) and the viewer's comment. No
  transcript, nothing outside the API. The result is GENERATED and
  unapproved. The no-promotion check runs on it and its findings are stored
  in `comment_meta.promotion`.
- Review: the panel's YouTube tab (`?platform=youtube` on the comment, approve,
  approve_all, save and reject routes; no parameter is LinkedIn, unchanged).
  Approve on YouTube runs the no-promotion validator on the exact text and
  refuses (409, with the reason) until it passes. Save edits the store only.
- `youtube-engage` (`engage`), and the panel's "Post one approved comment"
  button: posts **at most one** comment per call, else says when the next is
  allowed. Below.

**Engage, in order.** An `attempting` mark left by a run that died mid-insert
is resolved first: that record is closed `TRASH(youtube_ambiguous)` and counted
live, never retried. Then: `split_approved` over the GENERATED drafts (none
approved: `0 posted: N not approved`); the window and the gap; per draft,
oldest approval first, the daily cap for its kind, one per creator per 3 days
and one per item; the no-promotion validator, whose refusal withdraws the
approval (`reviewed_at` and `approved_hash` cleared, text kept) so the draft is
back in review. The first draft through is the one. Live: a quota pre-check for
the worst case (1 + 3 + 50 + 1 units), the identity check (`channels.list
mine`, the token must answer for `channel_id`), and dedupe on YouTube
(`commentThreads.list` for the video matched on our channel id, or the thread's
replies, at most 3 pages). Found: the record is COMMENTED with outcome
`already_commented`, nothing inserted. Then **the last step: a fresh read of
the store and `post_store.approval_refusal` on it**, plus the text must equal
the text selected. Then the `attempting` mark is written, the insert is charged
(50) and made, once.

**Outcomes.** `posted_visible`: the insert returned a comment id and the
comment is in the public listing (1 unit). `posted_held`: the insert returned
an id but the comment is not listed, or the listing failed. Held is never
re-posted (the record is COMMENTED with `youtube.outcome = posted_held`) and is
never reported as posted. `failed`: a 4xx closes the record
`TRASH(youtube_post_failed)` and does not count against pacing; no HTTP answer,
a 5xx, or a success with no id is **ambiguous**: closed
`TRASH(youtube_ambiguous)`, counted against pacing, never retried. A Google
quota error created nothing: the draft stays GENERATED and approved. A 404 on
dedupe closes the record UNAVAILABLE; `commentsDisabled` closes it
`TRASH(youtube_comments_disabled)`; any other read error stops the run with the
draft untouched. Every live or ambiguous comment sets `next_allowed_at` to now
plus the random gap and, for others' content, starts the ramp if it has not
started.

**DEC-061 pins** (`youtube_engage.py` constants, each refusal naming itself):

| Pin | Code | Refusal |
|---|---|---|
| Ramp on others' videos: days 1-7 3/day, 8-14 5, 15-21 10, then 20; day 1 is the Pacific date of the first live comment on others' content | `RAMP`, `RAMP_MAX`, `ramp_day`, `cap_refusal` | `daily_cap: N of C ... (ramp day D)` |
| Replies on our own videos: 20/day, counted separately | `REPLY_DAILY_CAP` | `reply_daily_cap: N of 20` |
| 08:00-21:00 Pacific | `WINDOW_START`, `WINDOW_END`, `window_refusal` | `outside_window` |
| Random 25-90 minute gap after any comment | `GAP_MINUTES`, `draw_gap`, `gap_refusal` | `gap: ... runs to <time>` |
| One comment per creator (the video's channel) per 3 days | `CREATOR_COOLDOWN`, `creator_refusal` | `creator_3_days` |
| One comment per item (video, or viewer comment) | `item_refusal`, the store, YouTube dedupe | `already_commented_on_item` |
| No promotion: links, @-mentions, hashtags, "subscrib", our channel's name and handle, the register's banned openers | `promotion_refusals` | `promotion: <what>`, back to review |

Pacing counts ledger entries marked `live` (visible, held, ambiguous) on their
Pacific date. Each pacing refusal carries the time the next comment is allowed.

**Quota.** Every call is charged to V1's `quota_ledger.json` and logged to
`api_usage.jsonl` before it is made (`_Google.spend`); a call that would go
over the daily limit is not made. One comment costs 53 units with a one-page
dedupe (1 identity + 1 dedupe + 50 insert + 1 verify), at most 55.

*Verification anchor (11, Y2-T): 2026-11-27, branch `y2t-teratoma-comments`;
`tests/test_youtube_register_teratomahouse.py` compares every length and
register field and the reply variant's two fields with the fixture copy,
spot-checks raw text, refuses unknown names and variants, refuses six bad
`register` values at config load, and drives the validator, generator, draft
and prompt in the named register. `tests/test_youtube_policy_golden.py`,
unchanged, still pins Top Five's register and LinkedIn and X.
`tests/test_youtube_api.py` covers `my_channel_snippet`. Live, teratomahouse:
`channels.list part=snippet mine=true` answered `UCB-uYMWucCp50Uq0dvuOOGA`,
"Teratoma House", `@teratomahouse`; 6 of 14 candidates resolved and were added.*

*Verification anchor (11, Y2-S): 2026-11-27, branch `y2s-comment-schedule`;
`tests/test_youtube_schedule.py` covers `auto_run` absent, false, true and six
wrong types; `--scheduled` refusing engage and morning with a Google stand-in
that fails on any use, no quota or paid-call line, and no ledger written, and
proceeding when true; no approved draft, outside the window, inside the gap
and over the cap each posting nothing with no Google use, and the stand-in
failing a run that would post; morning's limits (waiting approved or not, the
floor at 0, the ramp day), never approving, both refusals printed without
force-ssl, draft after a find error, the dry run; the CLI routing,
`--scheduled` refused on other commands, the log file getting each line once;
and the task script parsed (both commands, triggers, repetition, limits,
principal, `-Remove`), never run. Turning the gate off turns four red.*

*Verification anchor (11, Y1-S): 2026-11-26, branch `y1s-comment-scope`;
`tests/test_youtube_engage.py` covers the refusal text for the old two
scopes, no scopes, malformed and empty; find landing videos with no comment
read and no unit; draft refusing with no Google, generator or ledger call;
engage refusing for a video and a reply with the approval kept; the
`_Google.call` backstop on all four comment ops; each dry run; and each path
proceeding with force-ssl. Making the gate always pass turns ten red. Live,
topfive: `youtube-draft --limit 1` printed the refusal with exit 2, and
`youtube-find --dry-run` named it; `api_usage.jsonl` and the quota ledger
were unchanged.*

*Verification anchor (11): 2026-11-22, branch `y1-youtube-comments`;
`tests/test_youtube_engage.py` (every pin both ways, each promotion kind, the
approval gate True and False: approved, unapproved, edited after approval,
wrong status, withdrawn mid-run; dedupe local, remote page 2, the 3-page cap,
replies, 404; the three outcomes plus ambiguous, quota and the mid-insert
crash; exact quota charges with each fake Google call asserting its ledger
lines were written first; discover adds nothing; find and draft inputs; the
CLI), `tests/test_youtube_engage_dashboard.py` (the YouTube tab's routes),
`tests/test_youtube_policy_golden.py` (every field against the register copy;
LinkedIn and X against `tests/goldens/platform_policies_main.json` from main
913a5ec), and `tests/test_network_guard.py` (the suite-wide socket guard fails
a raw connect, a lookup, urllib and a real Google client call).*

---

## 12. Engagement adapters (E1, D-027)

PLATFORM_REFACTOR §9's adapter boundary, extracted from the LinkedIn path with
no LinkedIn behaviour change. `engagement_adapter.py` is the interface and the
shared side; each platform's engagement is one adapter.

**The interface** (`EngagementAdapter`, an ABC), per platform:

| Method | Answers | Rule |
|---|---|---|
| `find(**options)` | candidate posts, landing NEW in the platform's store | |
| `classify(record)` | `Classification`: `ok`, `gone` or `unclear` | `gone` only on a positive signal; everything else `unclear`, retried (PRINCIPLES §5) |
| `already_commented(record)` | `ThreadCheck(already, reason, evidence)` | asked of the thread, never of our own records |
| `post_and_verify(record)` | `PostResult(posted, outcome, proof, ...)` | `posted` only on positive proof |
| `like(record)` | `LikeResult(state, proven, reason)` | `placed`, `already_liked`, `miss`, or `not_applicable`; never inferred by negation |
| `selectors()` | a **browser** adapter's selector_health entries; `None` for an **API** adapter | see below |

**Selectors (amends §9's wording).** §9 says an adapter registers its
selectors as class constants in `selector_health`. That holds for **browser**
adapters (`kind = BROWSER`): `selectors()` returns its registry entries and
`selector_contract_problems` requires each to be in `SELECTOR_REGISTRY`
exactly as declared. An **API** adapter (`kind = API`) has no page and so no
selectors: `selectors()` returns `None` and `SELECTORS_EXEMPT_REASON` must say
why. YouTube is an API adapter and is exempt.

**The shared side** keeps the E0 gate: `approval_refusal_for(record, text,
strip=)` is `post_store.approval_refusal` plus "the text to post is the
approved text". LinkedIn's poster compares stripped (its TXT parser strips),
YouTube exactly; both call it on a fresh store read, last. `engage_one(adapter,
record, reload=)` is the shared one-item order: approval, classify, thread,
like, approval again on `reload(key)`, post and verify. LinkedIn's batch run
does not use it (below); it is what a new adapter is driven by, and the tests
drive the LinkedIn adapter through it against the poster's fake page.

**LinkedIn** (`linkedin_adapter.LinkedInAdapter`, browser) is a wrapper over
`LinkedInCommentPoster` and `LinkedInAIPostFinder` with no behaviour of its
own: `classify` is `navigate_to_post` plus `last_navigation` (`UNAVAILABLE` is
`gone`; `timeout`, `unclear` and a navigation error are `unclear`, named in
`detail`); `already_commented` is `already_commented_here`; `like` is
`like_post`, its state read from `last_like_outcome`, which `note_like_state`
now records on every verdict (`already_liked` is proven by a known liked-state
selector; `placed` is reported unproven, because the liked state is not re-read
after the click); `post_and_verify` is `post_comment`, which verifies in the
thread. The batch path is unchanged: the dashboard and the scheduler still hand
a TXT file to `comment_poster` in a subprocess and its loop (§5) decides.
What moved into `linkedin_adapter`, from where E0-A found it:

| LinkedIn-specific | Now | Was |
|---|---|---|
| TXT handoff | `write_comments_file` (re-exported as `scheduler.write_comments_file`), `write_handoff` (the dashboard's `_write_poster_input` writes through it) | `scheduler.py`, `dashboard.py` |
| Subprocess commands with `--allow-production` | `poster_command`, `finder_command` | `dashboard._post_comments_job`, `_scrape_job` |
| Posting-path selector_health entries | `registry_entries` (§8.2) | `selector_health.SELECTOR_REGISTRY` literal |
| Post identity (`activity`/`ugcPost`/`share`/`groupPost`) | `LinkedInAdapter.native_id`, over the poster's `post_identity` | unchanged |
| Ledger keys the store reconciles from; `ad`/`job_card` trash reasons | `LEDGER_KEYS`, `TRASH_REASONS` (named; the reconcile is still `post_store`'s) | `post_store.py` |

Not moved, on purpose: routes defaulting to `post_store.LINKEDIN` (a missing
`?platform=` is LinkedIn, DEC-063), the store's UNAVAILABLE meaning, and the UI
step labels. First comments on our own posts (`first_comment.py`,
`csv_pipeline.py`) keep calling the poster's primitives directly: they are
publishing, not engagement, and their "already there" check deliberately
differs (`first_comment_already_present`, MAINTENANCE §6.6).

**YouTube** (`youtube_engage.YouTubeAdapter`, API) wraps the Y1 functions, and
`_post_one` now goes through it. `classify` and `already_commented` are one
read, the dedupe listing, made once per record: its items are the thread, a
404 on it is `gone`, `commentsDisabled` is `gone` with that `detail`, anything
else `unclear` (which stops the run, as before). `post_and_verify` is the one
insert plus verify; `posted` is True only for `posted_visible`, and
`posted_held` carries its comment id as proof but is not success (DEC-063).
`like` is `not_applicable` and says so; nothing is attempted.

**The no-change proof.** `tests/test_e1_golden.py` re-runs
`tests/e1_golden_harness.py` and compares, line for line, with
`tests/fixtures/e1_golden/expected.json`, which the same harness wrote from the
pre-E1 code (`main` @ `e8d58b7`): the poster's decisions over a TXT handoff
against a fake page (posted, like missing, gone, timeout, unclear, our comment
already there by marker and by text, a submit that never lands, not approved,
edited after approval, already in the ledger, past the count: what was
published and liked, the run result, the ledger, the reconciled store, the
poster's log lines, the failure captures, the RUN counts), the scheduler's and
the dashboard's selections, notes, TXT handoffs and poster argv, the composed
LinkedIn and X prompts with OpenAI faked, the selector registry, and
`post_identity`. Regenerating the golden defeats it.

*Verification anchor (12): 2026-11-22, branch `e1-adapter-boundary`;
`tests/test_e1_golden.py`, `tests/test_engagement_adapter.py` (each adapter
verdict both ways on LinkedIn's fake page and on stubbed YouTube reads, the
selector contract held and broken, the registry's selectors against the
golden's, the shared approval check, `engage_one`).*

### 12.1 Instagram (E2, E3, D-027): offline only, every selector unverified

Instagram is a **browser** adapter, `instagram_adapter.InstagramAdapter`, in
Selenium, written fresh. `instagram_bot` (Playwright, read only) was the
reference for the selectors, the `/p/<shortcode>/` permalink, the og-meta
reading and the pacing; nothing is imported from it. It has never run live.

**Policy (E2).** `policy_for("instagram")` is
`content/engagement/instagram-register.md`, copied verbatim (DEC-005), with a
golden test against the copy in `tests/fixtures/engagement/`
(`tests/test_instagram_policy_golden.py`). LinkedIn, X and YouTube are pinned
unchanged in the same test. `input_chars` is the shared 500 (one caption).
The id is `platforms.INSTAGRAM`, outside `platforms.ALL` (no Buffer channel).

**Rules that are data checks (E2).** `instagram_engage.draft_refusals(text,
caption=, cfg=, previous=)` returns named reasons, each tested both ways
(`tests/test_instagram_rules.py`):

| Rule | Check |
|---|---|
| length | over the register's `hard_max` (300 chars); `empty` |
| emoji | `emoji_count > 1`. Emoji blocks; a ZWJ sequence, flag pair, keycap or skin-tone variant is one; ©, ™, arrows are not emoji |
| hashtags, links, @-mentions, promotion | as Y1's validator: `#tag`, a URL or bare domain, `@name`, "follow me", "check out my", "link in bio"..., our `identity_slug`, the register's banned openers. No `identity_slug` refuses everything |
| caption language | `detect_language`, stdlib: dominant Unicode script, then function-word hits for en/es/fr/de/pt/it/nl (at least 2 hits, leading the runner-up by 1.5x). Only a confident disagreement refuses; short text is `latin`/`unknown` and passes; other Latin languages read as `latin`; Russian and Ukrainian are both `cyrillic` |
| near-duplicates | Jaccard similarity of character trigrams (lowercased, NFKC, punctuation as spaces) against our COMMENTED comments and other approved drafts to the same `author_handle`; refused at `NEAR_DUPLICATE_THRESHOLD = 0.6` |
| appearance | `APPEARANCE_PATTERNS`, an English phrase list ("you look", "your smile", "you're so pretty", ...), the backstop to the register's craft rule 3 |

A refusal at engage time is `instagram_rules: <reasons>`; `send_back_to_review`
withdraws the approval and keeps the text and the reason in `comment_meta`.
`approve_refusal(profile, rec, text)` is the same check for an approve route
(none is wired yet: the Review routes still serve LinkedIn and YouTube only).

**Config.** `scheduled_posting.platforms.instagram` =
`{identity_slug, engagement: {max_followers, skip_verified}}`; unknown keys
and wrong types refuse. `identity_slug` is the account we act as. The account
filter (`account_refusal`, used by `find`) skips an author over
`max_followers`, with a verified badge (when `skip_verified`, default true),
or with either one unread. `max_followers` has **no default**: unset skips
every account.

**Store.** `PostStore(platform="instagram")`, `data/<p>/instagram/posts_db.json`,
keyed by shortcode, with an `instagram` block (`author_handle`, `followers`,
`verified`, `filter_reason`, outcome, like state). `find` lands a filtered
author's post as `TRASH(instagram_account_skipped)` so the skip is counted.
A comment submitted but not seen is `TRASH(instagram_unverified)`, never
retried; gone is UNAVAILABLE; already commented and posted are COMMENTED;
anything unclear stays GENERATED and approved.

**Selectors.** Every one is a class constant on `InstagramAdapter`, with an
`UNVERIFIED` comment naming its source, registered through
`instagram_adapter.registry_entries()` with `"verified": False` on pages
`ig_feed`, `ig_post`, `ig_profile` (invisible to every LinkedIn and X check).
The offline gate is `tests/fixtures/instagram/*.html`, hand-written and
synthetic, also marked unverified. The action-bar like is scoped to
`section` because the thread's own Like hearts are decoys; the submit is the
one `Post` inside the form.

**The live gate.** The adapter treats every driver as live unless it declares
`IG_OFFLINE_FIXTURE = True` (only the test fake does). A live run needs
`live_pass_problem()` to be None: `data/<p>/instagram/selector_health_live.json`
exists, is a live run, HEALTHY, for the configured `identity_slug`, for the
exact selectors now in the code (sha256 fingerprint), covering all three
pages, and is under 7 days old. It is written only by
`python -m linkedin_automation.instagram_adapter health --profile <p>
--post-url <post>`, which opens the feed, the post and our own profile,
counts every entry and clicks and types nothing. An identity in
`PRODUCTION_IDENTITY_SLUGS` is refused with no override, on top of
`create_driver`'s own guard.

**Verdicts.** `classify`: `gone` only on Instagram's "page isn't available"
text; signed out is `unclear(auth_wall)`; the configured account not proven
signed in (its avatar link outside the post) is `unclear(identity_unproven)`;
a post that does not render as its own shortcode within the bounded wait is
`unclear`. `already_commented` asks the shown thread for a comment by
`identity_slug` (Instagram shows only part of a long thread). `like` is
`placed` only when the liked state is read after the click, `already_liked`
on the liked state, otherwise a named `miss`. `post_and_verify` re-checks the
E0 gate, refuses text ChromeDriver cannot type (outside the BMP, so most
emoji), checks the typed value, and reports posted only when a comment by
`identity_slug` with the exact text is in the thread, and still there after a
reload (Instagram draws a new comment before the server keeps it, so the
first sighting alone is not proof). Every wait is one
bounded, polled deadline; every failure is captured as `failure_ig_<reason>`.
`engage_item(profile, key, adapter)` is the one-item path: E0 gate, E2 rules,
live gate, `engage_one` with a fresh store read last, then the store outcome.

**Not here yet (E4).** No live call has been made. No CLI or dashboard
button posts, no scheduler job exists, nothing is enabled. DEC-061's pacing
pins (ramp, window, gap, one per creator per 3 days) are not applied to
Instagram; they belong with E4's run path.

*Verification anchor (12.1): 2026-11-22, branch `e3-instagram`;
`tests/test_instagram_policy_golden.py`, `tests/test_instagram_rules.py`,
`tests/test_instagram_adapter.py` (each verifier True and False against the
synthetic fixtures, the live gate shut and opened by a recorded pass, the
fixture gate per page, the registry entries unverified).*
