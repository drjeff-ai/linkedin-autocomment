# LinkedIn Automation Dashboard

Automated LinkedIn engagement system with a web dashboard for managing comments, posts, and connections across multiple LinkedIn profiles.

It finds relevant posts in your feed, drafts authentic first-person comments with
GPT, lets you review them, and posts them with human-like browser behavior. It can
also send connection requests and publish your own thought-leadership posts, all on
a randomized, human-paced schedule.

> ### ⚠️ Personal use only — read this first
>
> This project **automates a real LinkedIn account** using your own logged-in
> browser session. Automating LinkedIn is against
> [LinkedIn's User Agreement](https://www.linkedin.com/legal/user-agreement) and
> can get your account **restricted or permanently banned**.
>
> - It is provided **as-is, for personal and educational use, with no warranty.**
> - **Use it at your own risk.** You are solely responsible for how you use it and
>   for any consequences to your account.
> - Respect other people: don't spam, don't harvest data, keep volumes low, and
>   keep the human-in-the-loop review steps on.
>
> The author does not endorse violating any platform's terms of service.

## Quick Start

On Windows you can just run `setup.bat` (installs deps, creates `.env`, makes
`data/`) then `run.bat`. Or do it manually:

```bash
# 1. Install uv (if you don't have it)
# Windows: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# Mac/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Create venv and install dependencies
uv venv
uv pip install -r requirements.txt
uv pip install -r requirements-dev.txt   # test/lint tooling (optional)

# 3. Set up credentials
copy .env.example .env
# Edit .env with your OpenAI API key

# 4. Add a LinkedIn profile (prompts for username/password; special chars OK)
uv run python -m linkedin_automation.profile_manager add <name>

# 5. Log in once (opens Chrome; log in, then close it — the session persists)
uv run python tools/login_check.py --profile <name>

# 6. Launch dashboard
uv run python -m linkedin_automation.dashboard
# Open http://localhost:6500
```

> If a scrape/post/connect run reports **"Login required. Run tools/login_check.py
> --profile <name>"**, your LinkedIn session has expired — re-run step 5.

## Project Structure

The core code is a Python package (`linkedin_automation/`); standalone
maintenance scripts live in `tools/`. Launch everything from the project root.
For how the pieces fit together — and the rules a change must not break — read
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

```
linkedin-automation/
├── linkedin_automation/                # the application package
│   ├── __init__.py
│   ├── dashboard.py                    # Flask backend + JSON API (the entry point)
│   ├── templates/dashboard.html        # Web UI (served by the dashboard)
│   ├── profile_manager.py              # Multi-profile credentials, sessions, exit codes, CLI
│   ├── comment_fields.py               # Canonical comment-field normalization + TXT format
│   ├── human_behavior.py               # Human-like browser behavior (mouse/typing/scroll/breaks)
│   ├── post_finder.py                  # Scrapes the feed + classifies ads/job cards
│   ├── comment_generator.py            # GPT comment generation
│   ├── comment_poster.py               # Posts comments via Selenium
│   ├── auto_connector.py               # Sends connection requests
│   ├── post_generator.py               # Generates LinkedIn posts (thought leadership + articles)
│   ├── poster.py                       # Publishes posts to the LinkedIn feed
│   ├── post_store.py                   # Central per-profile post lifecycle store
│   ├── scheduler.py                    # Randomized twice-daily auto-post engine
│   ├── failure_capture.py              # Screenshots + DOM sidecars on browser errors
│   ├── run_log.py                      # Per-run file log + per-step timing for the poster
│   └── selector_health.py              # Detects when LinkedIn's DOM breaks selectors
├── tools/                              # standalone maintenance / diagnostic scripts
│   ├── login_check.py                  # Check/establish a profile's LinkedIn session
│   ├── reconcile_bins.py               # Reconcile + print lifecycle bins
│   ├── feed_dump.py / connector_dump.py  # Selector-debug helpers (dump live DOM)
│   └── run_linkedin_workflow.py        # Non-dashboard CLI pipeline runner
├── tests/                              # pytest suite (no browser/network/OpenAI)
├── default_profile_config.json         # Per-profile config template
├── requirements.txt
├── requirements-dev.txt                # pytest, pytest-cov, ruff (pinned)
├── conftest.py                         # puts project root on sys.path for tests
├── setup.bat / run.bat
├── .env                                # Your API keys (create from .env.example)
├── .env.example
├── .gitignore
├── ARCHITECTURE.md                      # How it fits together + the lifecycle contract
├── README.md / CONTRIBUTING.md / LICENSE
├── logs/                               # run_<profile>_<ts>.log per comment-posting run (git-ignored):
│                                       #   STEP/POLL/COMMENT/RUN timing lines, file only
└── data/                               # Auto-created at runtime (git-ignored)
    ├── profiles/
    │   ├── profiles.json               # Encrypted credentials
    │   └── chrome_sessions/            # Per-profile Chrome data
    └── <profile_name>/
        ├── posts_db.json               # Central post lifecycle store (status per post)
        ├── linkedin_timeline/          # Scraped posts JSON (stage transport)
        └── quality_comments/           # Generated comments, progress tracking
            ├── comments_*.json
            ├── daily_comments_*.txt
            ├── posting_progress.json   # Authoritative posted ledger
            └── archived/
```

Everything runs from the project root: the app as `python -m
linkedin_automation.dashboard`, package modules the dashboard shells out to as
`python -m linkedin_automation.<module>`, and the `tools/` scripts as `python
tools/<script>.py` (each puts the project root on `sys.path` so
`import linkedin_automation` resolves). Runtime data always lands in `data/` at
the project root, regardless of where you launch from.

### Post lifecycle (`posts_db.json`)

> **See [ARCHITECTURE.md](ARCHITECTURE.md) §1** for the full contract — every
> reader and its source, the reconciliation order, the load-bearing gotchas, and
> the invariants. This section is the summary.

Every scraped post has one explicit status in a central per-profile store
(`linkedin_automation/post_store.py` → `data/<profile>/posts_db.json`) instead of its state being
scattered across files:

```
NEW ──generate──▶ GENERATED ──post──▶ COMMENTED
 │                    │
 └────reject──────────┴──────▶ TRASH (ad | job_card | low_quality | no_url |
                                      evaluator_rejected | manual)
                                  └──restore──▶ NEW / GENERATED
```

**The store is the source of truth for every list the dashboard shows, not just
for the counts.** Files on disk are derived output, never state.

- **Scrape** writes quality posts as `NEW`; ads/job cards and below-threshold
  posts go straight to `TRASH` with a reason (previously they were silently
  dropped). **Generate** moves `NEW → GENERATED` and attaches the draft.
  **Reject** (or "Remove Unchecked" in Review Posts) sends a post to
  `TRASH(manual)` — remembered, not lost — and **Restore** brings it back.
- Recruiter job ads are caught at scrape time as `TRASH(job_card)`. Detection
  needs *both* a hiring headline and the structural shape of a job listing
  (`Location:` / `Experience:` / `send your resume` / `(1–2 years)` …), so a post
  that merely mentions hiring — including someone sharing a role with their own
  commentary — is not filtered. If you tune this, re-check it against your own
  `COMMENTED` + `GENERATED` posts first: a false positive silently costs you real
  engagement.
- A post with **no URL** can't be commented on or posted (nothing to link to),
  so it is trashed as `TRASH(no_url)` — at scrape time if URL extraction failed,
  and on `reconcile` for any URL-less post still stuck in `NEW`. A large `no_url`
  pile in the Trash view is a signal that clipboard URL extraction is missing
  posts. Restoring one puts it back in `NEW`; if it still has no URL the next
  reconcile re-trashes it.
- A post the comment **evaluator** turns down becomes `TRASH(evaluator_rejected)`.
  Without that transition it stayed `NEW` and was re-sent to the LLM — and
  re-billed — on every run. It is restorable like any trashed post.
- **Every tab reads this store, so bin counts and tab contents always agree.**
  Review Posts lists the `NEW` bin (across every scrape, not just the latest
  file); Review Comments lists the `GENERATED` bin with each draft; the Post step
  posts from the `GENERATED` bin. `bin count N` ⇒ the tab shows `N`.
- **A filtered list always says what it filtered.** Once you approve a draft in
  Review Comments it is stamped `reviewed_at` and leaves the review queue, and the
  subtitle then reads "N already reviewed and awaiting posting (M drafted in
  total)". A short list is never ambiguous with data loss.
- **Comment generation is driven by this store**, not by "the latest scrape
  file" and not by whatever you selected in Review Posts. Clicking *Generate*
  reconciles the store, takes every `NEW` post (across all scrapes), and generates
  for those. To generate from a specific file instead, use the CLI:
  `uv run python -m linkedin_automation.comment_generator <file> --profile <name>`.
- `posting_progress.json` stays the authoritative record of what was actually
  posted; the store *reconciles* on every read (`reconcile`): a `NEW` post whose
  URL was already posted becomes `COMMENTED`, a `NEW` post that already has a
  draft in a `comments_*`/`ready_*` file becomes `GENERATED`, a `GENERATED` post
  whose draft went missing has it recovered from a comment file (or is demoted to
  `NEW` to regenerate), and any post still `NEW` with no URL becomes
  `TRASH(no_url)` (that step runs last, so a post reconciled to
  COMMENTED/GENERATED is never trashed). This keeps the bin counts honest and
  never resurrects a manually-trashed post.
- On first use the store is seeded from existing scrape/comment/progress files
  (`migrate_from_legacy`), so no history is lost. Identity/dedup matches the
  dashboard's existing URL/hash rule.
- Diagnostic: `uv run python tools/reconcile_bins.py --profile <name>` reconciles the
  store and prints before/after bin counts, per-rule actions, and a TRASH-by-reason
  breakdown (add `--dry-run` to preview without saving). One-off cleanup for stores
  predating the `evaluator_rejected` state:
  `--backfill-evaluator-rejected` moves `NEW` posts that a past generator run
  evaluated and dropped into `TRASH(evaluator_rejected)`. That is *inferred* from
  past generator input files rather than recorded, so it is opt-in and restorable —
  preview it with `--dry-run` first.
- API: `GET /api/posts/<profile>/lifecycle[?status=]` (counts + posts),
  `GET /api/posts/<profile>` (the `NEW` bin), `GET /api/comments/<profile>`
  (the `GENERATED` bin; `?include_reviewed=1` for the whole bin),
  `POST .../reject`, `POST .../restore`. The dashboard shows a counts strip for
  all four bins and a Trash view (grouped by reason, with Restore).

## Dashboard Features

### Comment Pipeline (5-step)
1. **Scrape** — Find quality AI-related posts on LinkedIn
2. **Review Posts** — Remove low-quality posts before generating
3. **Generate** — Create authentic comments via GPT-4o-mini (no cap by default — generates for every engaging post; check "no limit" or set a max). Generation is OpenAI-only, so there's no rate-limit reason to cap it
4. **Review Comments** — Edit/remove before posting
5. **Post** — Automatically post comments with human-like behavior

### Post Creator
- Generate thought leadership posts (8 style types)
- Generate article reaction posts from URLs
- Queue system with publish controls

### Auto-Connector
- Bulk send connection requests from search results
- Handles LinkedIn's shadow DOM for "Send without a note"
- Rate limiting and daily caps

### Scheduler
- Auto-posts queued comments on a **randomized twice-daily** schedule (default
  morning 8–11am, afternoon 2–5pm), plus an optional daily scrape to refill the
  pipeline (off by default).
- Randomization is deliberate (avoids LinkedIn automation detection): the exact
  fire time within each window is re-rolled daily, the count per run is random
  within `count_min`–`count_max` (default 4–8), and each run has a `skip_chance`
  (default 0.1) of being skipped entirely. Two runs never fire closer than a
  minimum gap.
- Runs as a background thread **inside the dashboard** — active only while the
  dashboard is open. Missed windows (dashboard closed) are not caught up.
- Works through the GENERATED lifecycle queue until empty; an empty queue logs
  "No comments queued" and does nothing. Scheduled runs reuse the same
  `run_job` / one-browser-task-per-profile lock as manual actions.
- Config lives under `"scheduler"` in the profile config. API:
  `GET /api/scheduler/<profile>/status`, `POST .../toggle` (master or a job),
  `POST .../config` (windows/counts), `POST .../run-now` (fire immediately).

### Multi-Profile
- Manage multiple LinkedIn accounts
- Per-profile Chrome sessions (no cookie conflicts)
- One browser task per profile at a time, unlimited API tasks

## Profile Management

```bash
# Add a new profile
uv run python -m linkedin_automation.profile_manager add

# List profiles
uv run python -m linkedin_automation.profile_manager list

# Set default profile
uv run python -m linkedin_automation.profile_manager default <name>

# Migrate from .env (auto-runs on first dashboard launch)
uv run python -m linkedin_automation.profile_manager migrate
```

## Per-Profile Configuration

Each profile has its own config controlling the post finder's keywords/quality
and the comment generator's persona, tone, voice, topics, and style. It is
created from `default_profile_config.json` (project root) at
`data/<profile>/profile_config.json` the first time the profile is used, and is
deep-merged onto the defaults on read (so new default keys are picked up without
clobbering your edits).

```bash
uv run python -m linkedin_automation.profile_manager config <name>          # print the config
uv run python -m linkedin_automation.profile_manager config <name> --edit   # open it in $EDITOR
uv run python -m linkedin_automation.profile_manager config <name> --reset  # regenerate from default
```

The dashboard also exposes `GET`/`POST /api/profiles/<name>/config`.

The post finder loads `post_finder.keywords_tier1/tier2`, `min_quality_score`,
`max_posts_per_scan`, and `post_types_to_engage`. The comment generator injects
`comment_generator.persona/tone/voice/topics_of_expertise/things_to_avoid/
style_mix` into the prompt. The hard comment-style rules (no dash punctuation,
charming-not-snarky, first-person singular) are always enforced in the generator
regardless of config. Profiles without a config fall back to defaults.

### Human-behavior tuning (`behavior`)

Every script that drives a real LinkedIn session routes its clicks, typing,
scrolling, delays, and breaks through `human_behavior_selenium` (Bezier mouse
paths, char-by-char typing, variable scrolls, reading pauses, non-uniform breaks)
so behavior doesn't look automated. The timing ranges are tunable per profile
under a `behavior` section, applied at startup via `hb.configure_behavior()`:

| Key | Meaning |
|-----|---------|
| `typing_speed_range` | per-keystroke delay seconds `[min, max]` |
| `typing_pause_chance` | probability of a "thinking" pause between keystrokes |
| `reading_time_range` | base read-pause seconds (scaled up by post length) |
| `action_delay_range` | default between-action pause seconds |
| `scroll_pixels_range` | per-scroll distance in pixels |
| `scroll_delay_range` | pause after a scroll |
| `break_frequency` | actions between break checks |
| `break_duration_range` | longer-break length seconds |
| `posts_per_break_range` | re-rolled count of posts before a longer break, so the work-then-pause rhythm is never identical between runs |

Omitted keys keep the human-like defaults in `default_profile_config.json`.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes | For comment and post generation |
| `LINKEDIN_USERNAME` | No | Auto-migrates to a `default` profile on first run |
| `LINKEDIN_PASSWORD` | No | Auto-migrates to a `default` profile on first run |
| `LINKEDIN_ALT_USERNAME` | No | Fallback username for auto-migration |
| `LINKEDIN_ALT_PASSWORD` | No | Fallback password for auto-migration |

## Login & Sessions

LinkedIn login happens in a real Chrome window (anti-automation friendly) and is
persisted per profile under `data/profiles/chrome_sessions/<name>/`.

```bash
uv run python tools/login_check.py --profile <name>   # opens Chrome, reports status, waits for manual login
uv run python tools/login_check.py --profile <name> --no-wait   # just report, don't wait
```

CLI scripts exit `0` on success, `2` when login is required, `1` on other
errors, so the dashboard can show an actionable "Login required" message instead
of a cryptic failure.

## Testing & Development

```bash
uv pip install -r requirements-dev.txt
uv run pytest tests/ -v        # full suite (no browser, network, or OpenAI)
uv run ruff check .            # lint
```

The suite covers profile management, comment-field normalization, the
save→archive→post pipeline, the comment TXT parser, login/error handling, the
post lifecycle store, ad/job-card classification, the scheduler, and the
dashboard HTTP endpoints (via the Flask test client). No test touches the
network, a browser, or OpenAI. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
branch/commit conventions and the ruff lint gate.

## Maintenance: Selector Health Check

LinkedIn changes its DOM every few months, which breaks the scraper's CSS
selectors. This is the project's one recurring chore — **[docs/MAINTENANCE.md](docs/MAINTENANCE.md)
is the full runbook** (symptoms, the exact durable hooks to target, which
constant to edit in which module, and the hard-won gotchas). The short version:
`linkedin_automation/selector_health.py` detects the breakage:

```bash
uv run python -m linkedin_automation.selector_health --profile <name>
```

It opens the feed, tests every selector the scraper uses (pulled from
`linkedin_automation/post_finder.py` so the registry stays in sync), and reports
**HEALTHY / DEGRADED / BROKEN**. On failure it dumps the current DOM to
`selector_debug_dump.html`, suggests replacement selectors, and — if a critical
selector broke — writes `.dev/SELECTOR_FIX_NEEDED.md` with a ready-to-paste fix
prompt. It never auto-edits selectors. The dashboard exposes the same check at
`POST /api/health/<profile>/selectors` (runs as a background job).

When you need to hand-inspect the live DOM to write new selectors, the two
dumpers save the current HTML (git-ignored, since they contain feed content):

```bash
uv run python tools/feed_dump.py --profile <name>              # feed page + a selector probe
uv run python tools/connector_dump.py "<people-search-url>" --profile <name>   # search results
```

## Notes

- Chrome must be installed (Selenium uses your real Chrome)
- First run per profile will require LinkedIn login in the browser
- The dashboard runs on port 6500 by default
- Comments use GPT-4o-mini for cost efficiency
- All tracking files prevent duplicate actions (won't repost or regenerate)

## License

Released under the [MIT License](LICENSE).
