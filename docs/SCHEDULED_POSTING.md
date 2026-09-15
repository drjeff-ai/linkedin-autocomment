# Runbook: scheduled posting (Buffer hybrid)

How to actually run this. Written for the person operating it, not the person
who built it.

## What it does

One CSV row becomes one scheduled LinkedIn post with an image, published by
Buffer at a time you choose, with a link posted as the first comment.

```
CSV row
  -> generate text if post_text is blank
  -> upload the local image to Cloudflare R2, get a public URL
  -> Buffer createPost (text + tags + image URL + dueAt)
  -> Buffer publishes at dueAt
  -> browser posts first_comment_link as a comment on the published post
```

Buffer handles the post; the browser handles only the comment. Buffer's
`firstComment` is paid-plan only and, on the free plan, including it rejects the
**entire post** — so it is never sent.

## The two commands

**Schedule** — run once per calendar. Creates every post. Takes minutes.

```
uv run python tools/run_scheduled_posts.py schedule \
    --csv content/week-of-2026-09-14.csv \
    --channel-id <your Buffer LinkedIn channel id> \
    --profile dev
```

**Comment** — run on a timer. Sweeps for posts that have published and comments
them.

```
uv run python tools/run_scheduled_posts.py comment \
    --profile dev --expect-identity your-profile-slug
```

**Status** — what the pipeline currently thinks, without doing anything.

```
uv run python tools/run_scheduled_posts.py status --profile dev
```

### Cadence

Run `comment` **every 15 minutes**. It is cheap and idempotent: with nothing due
it makes one API call per outstanding row and stops. It cannot double-comment —
a permalink that already has its first comment is skipped from a ledger on disk.

Buffer publishes **20–35 seconds after `dueAt`**, never early (measured
repeatedly: 24.7s, 26s, 34s). The comment pass polls the post's *status* and
never trusts the clock, so a 15-minute cadence just means a post waits at most
15 minutes for its comment.

On Windows, schedule it with `scripts/jobs/setup_task_scheduler.ps1` rather than
cron.

## The CSV

```csv
date,time_window,post_text,topic,tags,image_path,first_comment_link
2026-09-14,morning,"Most teams adopting AI agents start by asking what they can automate.

I think that's the wrong first question.",,#AI #FutureOfWork,S:\content\img\agents.png,https://example.com/the-article
2026-09-15,afternoon,,how AI changes hiring,#AI #Hiring,S:\content\img\hiring.png,
```

| column | notes |
|---|---|
| `date` | `YYYY-MM-DD`. Must be in the future. |
| `time_window` | `morning`, `afternoon` or `evening`. |
| `post_text` | The body. Leave **blank** to generate from `topic`. Quote it if it contains commas or line breaks. |
| `topic` | Only used when `post_text` is blank. |
| `tags` | Appended after a blank line. Hashtags inline in `post_text` also work. |
| `image_path` | A **local** path. Uploaded to R2 automatically. Leave blank for a text-only post. |
| `first_comment_link` | Posted as the first comment. Leave blank for no comment. |

### Time windows

Interpreted in **`POST_TIMEZONE`** (default `America/Los_Angeles`), with a random
minute inside the window so posts do not land on round numbers.

| window | local |
|---|---|
| morning | 08:00–11:00 |
| afternoon | 12:00–15:00 |
| evening | 17:00–20:00 |

DST is handled: the same local hour becomes a different UTC hour in summer and
winter, which is the point of converting late.

## Reading the run summary

```
row <row-key>  [COMMENTED]  The first line of the post text, truncated
    buffer post : <buffer-post-id>  due 2036-09-14T16:28:00.000Z
    published   : https://www.linkedin.com/feed/update/urn:li:share:00000...
    image       : https://pub-....r2.dev/posts/<content-hash>.png
```

| state | meaning | what to do |
|---|---|---|
| `SCHEDULED` | post created in Buffer, not published yet | nothing — the comment pass will pick it up |
| `PUBLISHED` | live, comment still owed | nothing — next sweep comments it |
| `COMMENTED` | **done** | nothing |
| `FAILED` | failed **before** publishing — nothing exists | fix the row, re-run `schedule` |
| `COMMENT_FAILED` | **the post is LIVE**, the comment did not land | add the comment **by hand**. Do **not** re-run the post. |
| `INVALID` | rejected at validation, never acted on | fix the row, re-run `schedule` |

The distinction that matters: `FAILED` means nothing was published and a re-run
is safe. `COMMENT_FAILED` means a real post exists — re-running `schedule` will
not touch it (verified), but the comment is yours to finish.

## Failure types and what they mean

| message | cause | fix |
|---|---|---|
| `bad date 'x' - expected YYYY-MM-DD` | malformed date | fix the cell |
| `unknown time_window 'x'` | not one of the three | fix the cell |
| `... resolves to ... which is in the past` | the window already passed today | move the date forward |
| `post_text and topic are both empty` | nothing to post | fill one |
| `image_path does not exist` | wrong path | fix the path |
| `... is not a PNG, JPEG, GIF or WEBP` | file is not an image | replace it |
| `Image could not be read from its URL` | Buffer could not fetch the R2 image | check the bucket is public |
| `the BUCKET is not public` | R2 public access is off | enable the r2.dev URL or attach a custom domain |
| `identity guard: logged in as ...` | wrong browser profile | check `--profile` / `--expect-identity` |
| `Failed - outcome UNKNOWN` | the request to Buffer was **in flight** when it failed | **check Buffer before re-queueing** — see below |
| `post ... was still 'scheduled' after ...` | Buffer had not published yet | **not a failure** — the next sweep continues |

### The one failure that is not safe to retry blind

Almost every failure happens before anything leaves this machine, so the row can
be fixed and scheduled again freely. One cannot.

If the network drops *after* Buffer accepted the post but *before* the reply
arrived, nothing on this side knows whether it was created. That row is marked
`Failed - outcome UNKNOWN`, and the queue says so instead of the usual "nothing
exists". **Open Buffer, look for a post on that row's date, and delete the row
or the post accordingly.** Re-queueing blind is how the same post publishes
twice, and nothing can unpublish the second one.

No automatic path ever retries it: `FAILED` rows are excluded from both the
Schedule button and the post drain. Only a human re-queue can double-post.

## Safety properties

* **A row that already has a post is never posted again.** Re-running `schedule`
  on the same CSV creates nothing (verified against live state: 0 API calls).
* **`--expect-identity` is required** for the comment pass. The default browser
  profile is the real account, and a comment on a live post cannot be undone, so
  the command refuses to run without it and aborts the whole sweep on a mismatch.
* **The identity check is exact.** It compares the `/in/<slug>` segment of the
  resolved profile URL, not a substring of the whole URL. A truncated vanity
  name (`example-person` for `example-person-011011`) is a *different person*
  and is
  refused; so is anything that is not a profile page at all, such as a login
  redirect or a checkpoint. **An empty `identity_slug` disables the guard
  entirely** — always set it in production.
* **Validation runs over every row before any row is acted on**, so a bad row
  never leaves a half-uploaded image behind.
* **Nothing self-likes.** The comment path never likes our own post.

## Configuration

`.env` (never committed — see `.env.example`):

```
BUFFER_API_KEY=            # Buffer GraphQL API
OPENAI_API_KEY=            # only needed for blank post_text
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=
R2_ENDPOINT=               # https://<account>.r2.cloudflarestorage.com  (UPLOADS)
R2_PUBLIC_BASE_URL=        # https://pub-<hash>.r2.dev                   (PUBLIC READS)
POST_TIMEZONE=America/Los_Angeles
```

`R2_ENDPOINT` and `R2_PUBLIC_BASE_URL` are **different hosts** and neither is
derivable from the other. Nothing public is served from the endpoint.

## State files

Per profile, under `data/<profile>/` (gitignored):

* `scheduled_posts_state.json` — per-row progress. **Deleting it makes the
  pipeline forget which posts exist, and a re-run would publish duplicates.**
* `scheduled_first_comments.json` — which permalinks already have their comment.

Both refuse to load if corrupt rather than reading as empty, because empty would
mean re-publishing everything they recorded.

## Known limits

* One identity. Multi-identity is deferred (see `.dev/BACKLOG.md`).
* `R2_PUBLIC_BASE_URL` points at the r2.dev development URL, which Cloudflare
  rate-limits and does not intend for production. Move to a custom domain before
  volume.
* Buffer free plan: 3,000 API requests per 30 days. Every call is logged to
  `api_usage.jsonl`.

## The image column

The image column may be called **`image_path`** or **`asset_path`** — the latter
is what the media-gen export writes. `image_path` wins if both are present.

A column the pipeline does not read is now **reported** at upload time rather
than ignored:

```
These columns are not read by the pipeline and were ignored: asset_id, job_id,
role, status. If one of them holds the image, rename it to image_path.
```

and, when no image column exists at all:

```
No image column found (expected one of: image_path, asset_path), so every row
will publish WITHOUT an image.
```

This matters because the failure was silent: rows were ACCEPTED, posts were
CREATED, and every one published text-only while the run reported success.

## Buffer free-plan limits

The free plan caps **scheduled (not yet published) posts at 10**. Past that,
`createPost` returns:

```
Buffer refused the post (LimitReachedError): Scheduled posts limit reached.
```

The row fails cleanly with that reason and nothing is created, so it is safe to
re-run once posts have published or been deleted — but a large calendar cannot
be scheduled all at once on the free plan.

## Held posts and the post drain

Buffer's free plan caps how many posts may be **scheduled at once**, counted
across the whole organisation rather than per channel. A calendar bigger than
that cap cannot all go in at once.

When you click **Schedule pending…**, the dashboard asks Buffer how many slots
are free and sends exactly that many. Everything beyond the cap is marked
**Held** — not Failed. A held row is correct and complete; it is waiting on a
slot, not on you. The queue says so, and offers nothing to "fix".

Slots free up on their own as scheduled posts publish. The **Post drain** feeds
held posts into those slots automatically.

### Turning it on

On the **Scheduled Posting** tab, in the *Post drain* card, tick the switch. Set
*Check every (min)* to how often it should look (default 15). The status line
shows how many posts are held, how many Buffer slots are free, when it last
looked, and when it will look next. **Check now** runs a check immediately.

Each check: if nothing is held, it does nothing and does not call Buffer at all.
Otherwise it asks how many slots are free and feeds that many held posts,
earliest date first. Held posts that still do not fit stay held.

### What it will not do

* **It never schedules a Pending row.** Only Held ones. A pending row has not
  been through the confirmation dialog that names the account it will post as;
  the drain has no authority to publish something you have not confirmed. Click
  **Schedule pending…** for those.
* **It never gives a row a second post.** A row that already has a Buffer post
  is refused, by the drain and by the schedule pass independently.
* **It never opens a browser.** It only calls Buffer's API, so it cannot collide
  with commenting, scraping or the comment sweeper.
* **It never runs at the same time as the Schedule button.** Whichever starts
  first wins; the other waits. Two passes over one queue could post a row twice.
* **It only runs while the dashboard is open.** Like the comment sweeper.

### The two background feeders

They are different things and the UI keeps them apart:

| | Comment sweeper | Post drain |
|---|---|---|
| Tab | Engagement Scheduler | Scheduled Posting |
| Does | posts queued **comments** | schedules **held posts** |
| Uses | a real browser | Buffer's API only |
| Timing | randomized daily windows, with random skips | a plain interval, no skipping |
| Accent | purple 💬 | teal 📤 |

Turning one on or off has no effect on the other.

## Seeing what happened to each row's image

Every row in the queue shows its image status: a thumbnail where there is one,
and a plain statement where there is not. Nothing renders as a blank cell —
blank reads as "text-only, on purpose", which is how a whole calendar once
scheduled imageless without anyone noticing until it published.

| Thumbnail | Line | Means |
|---|---|---|
| the picture | 🖼 image attached ↗ | uploaded to R2 and on the Buffer post |
| 🖼 ready (teal, dashed) | 🖼 image ready · `file.webp` | a local file that exists; uploads when scheduled |
| text only (grey, dashed) | text-only — no image for this row | no image path in the row |
| ⚠ (orange) | ⚠ image file NOT found | a path was given; nothing is at it |
| ⚠ (orange) | ⚠ image did NOT make it onto this post | the row asked for an image, the file is there, the post was created without it |

The thumbnails load straight from the public R2 URL, lazily and at 56px. If one
fails to load, the tile says so rather than sitting empty.

**Scanning for trouble:** a calendar that is unexpectedly *all* text-only is the
signature of the wrong CSV, or of an image column that is present but blank on
every row. The text-only tooltip names the columns that were searched
(`image_path`, `asset_path`), so the problem points at the column rather than at
the rows.

Status is visible **before** scheduling: a Pending row already shows whether its
file exists, so "file not found" is caught while the row can still be fixed.

## The queue reconciles itself when you open it

Buffer publishes on its own timetable and nothing here notices unless something
asks. Opening the Scheduled Posting tab **is** the asking: any row the tool still
has as `Scheduled` is checked against Buffer, and anything Buffer has actually
sent becomes `Published`, with its permalink captured.

**No background loop has to be running.** A post that went out two days ago
shows as published the moment you open the dashboard, whether or not the drain
or the sweeper has ever been switched on.

### The five states

| State | Means | Who moves it on |
|---|---|---|
| **Pending** | queued here, not sent to Buffer | you, via Schedule |
| **Held** | correct, but Buffer has no slot free | the post drain, or Schedule |
| **Scheduled** | created in Buffer, not published yet | Buffer, at its due time |
| **Published** | **live on LinkedIn**, first comment still owed | the comment sweep |
| **Done** | published and commented | — |

A `Published` row is not "about to publish". It is already on LinkedIn; only its
first comment is outstanding. The row says so in words under the post text, not
just in the badge colour.

### What it costs

The free plan allows 3,000 requests per 30 days — about **a hundred a day** for
everything, scheduling included. So the reconcile is deliberately frugal:

* **One request**, however many rows are being reconciled — a single batched
  query, never one per post.
* **Only `Scheduled` rows** are ever re-polled. A published or finished post
  tells us nothing new, so the cost does not grow as the calendar fills up.
* **Nothing at all** is spent when no row is awaiting publication.
* **Cached for two minutes**, so tab switches and post-action refreshes share
  one answer instead of spending a request each.
* The channel lookup is cached for an hour. It used to be re-fetched on every
  slot check, which meant every slot check cost two requests instead of one.
* The slot count is cached for a minute. It is read on every render of this tab
  by the drain's status line, which is far more often than it can change.
  **Anything about to actually send asks for a fresh count**, because acting on
  a stale one is what Held exists to prevent.

If Buffer is unreachable — or rate-limiting — the queue still renders. It shows
the last known state and says the reconcile could not run, because a stale
answer is useful and a blank screen is not.

### Posts that published while the sweeper was off

They are not skipped. The comment sweep acts on `Scheduled`, `Published` **and**
`Comment failed` rows, so anything that published unattended still gets its
first comment on the next sweep. Reconciling first actually makes the sweep
*cheaper*: the permalink is already stored, so the sweep does not spend a
request rediscovering it.
