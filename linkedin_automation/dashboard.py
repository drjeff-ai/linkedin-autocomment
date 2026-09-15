# Flask backend for the LinkedIn Automation Dashboard
"""Flask backend for the LinkedIn Automation Dashboard.

Serves the single-page UI and the JSON API that drives the scrape → generate →
review → post pipeline. Long-running browser/AI tasks run as background jobs
(see ``run_job`` / ``run_subprocess``); profile management is delegated to
``linkedin_profile_manager``. Runs on port 6500.
"""

import os
import sys
import json
import time
import threading
import subprocess
import glob
import logging
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv

from .comment_fields import normalize_comment_fields, comments_to_txt
# Profile manager calls load_dotenv() on import; importing it here (before our
# own load_dotenv) is intentional and order-independent.
from . import profile_manager as pm
from . import post_store
from . import scheduler as scheduler_mod
from . import post_drain as post_drain_mod
from . import buffer_client
from . import csv_pipeline

load_dotenv()

# Directory holding this package's bundled files (the dashboard HTML template).
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

logger = logging.getLogger(__name__)

# ─── Subprocess Environment (fix Windows cp1252 encoding) ─────────────────────

_subprocess_env = os.environ.copy()
_subprocess_env['PYTHONIOENCODING'] = 'utf-8'
_subprocess_env['PYTHONUNBUFFERED'] = '1'

# ─── Job Tracking ─────────────────────────────────────────────────────────────

jobs = {}  # job_id -> {status, progress, result, error, log, profile, task_type}

# Task types:
#   "browser" = needs a Chrome instance (connect, post_comments, publish)
#   "api"     = just API calls, no browser (generate_comments, generate_posts, article_gen)
# Rule: Only ONE browser task per profile at a time. API tasks are unlimited.

def get_active_jobs(profile=None, task_type=None):
    """Get currently running jobs, optionally filtered."""
    active = {jid: j for jid, j in jobs.items() if j["status"] == "running"}
    if profile:
        active = {jid: j for jid, j in active.items() if j.get("profile") == profile}
    if task_type:
        active = {jid: j for jid, j in active.items() if j.get("task_type") == task_type}
    return active


def can_start_browser_task(profile):
    """Check if a browser task can start for this profile."""
    return len(get_active_jobs(profile=profile, task_type="browser")) == 0


#: Job categories that run ``csv_pipeline.schedule_pass`` against one profile's
#: state file. They must never overlap.
SCHEDULING_CATEGORIES = ("buffer_schedule", post_drain_mod.JOB_CATEGORY)


def can_start_scheduling_task(profile):
    """True when no schedule pass is already in flight for this profile.

    The Schedule button and the post drain both call ``schedule_pass`` on the
    same state file, and both are task_type="api", so the browser lock does not
    separate them. Two overlapping passes could each read a row as HELD before
    either wrote its post_id back, and create two Buffer posts for one row -
    the same post published twice, with no way to unpublish either.

    This is why the drain has its own category but shares this gate.
    """
    active = get_active_jobs(profile=profile, task_type="api")
    return not any(j.get("category") in SCHEDULING_CATEGORIES
                   for j in active.values())


def run_job(job_id, func, *args, profile=None, task_type="api", category="", **kwargs):
    """Run a function in a background thread and track its progress."""
    jobs[job_id] = {
        "status": "running",
        "progress": "",
        "log": [],
        "result": None,
        "error": None,
        "started": datetime.now().isoformat(),
        "profile": profile,
        "task_type": task_type,
        "category": category,
    }
    
    def wrapper():
        """Run the job function in the thread, recording result/error + status."""
        # Record result/error and logs BEFORE flipping status, so a poller that
        # observes a terminal status always sees the accompanying data.
        try:
            result = func(job_id, *args, **kwargs)
            jobs[job_id]["result"] = result
            jobs[job_id]["status"] = "completed"
        except pm.LoginRequiredError as e:
            # Actionable, traceback-free: the user just needs to log in.
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["login_required"] = True
            jobs[job_id]["log"].append(str(e))
            jobs[job_id]["status"] = "failed"
        except FileNotFoundError as e:
            msg = f"File not found: {e.filename or e}. The expected input/output file is missing."
            jobs[job_id]["error"] = msg
            jobs[job_id]["log"].append(msg)
            jobs[job_id]["status"] = "failed"
        except PermissionError as e:
            msg = f"Permission denied: {e.filename or e}. Check the file is not open elsewhere and is writable."
            jobs[job_id]["error"] = msg
            jobs[job_id]["log"].append(msg)
            jobs[job_id]["status"] = "failed"
        except Exception as e:
            jobs[job_id]["error"] = str(e)
            import traceback
            jobs[job_id]["log"].append(f"ERROR: {traceback.format_exc()}")
            jobs[job_id]["status"] = "failed"
    
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return job_id


def log_job(job_id, message):
    """Append a log message to a job."""
    if job_id in jobs:
        jobs[job_id]["log"].append(message)
        jobs[job_id]["progress"] = message


def run_subprocess(job_id, cmd, on_start=None):
    """Run ``cmd``, streaming stdout to the job log while capturing stderr
    separately. On a non-zero exit, the captured stderr is logged so failures
    are diagnosable instead of silently merged into stdout.

    ``on_start(process)``, if given, is called right after the process starts
    (e.g. to register the handle so the job can be killed).

    Returns ``(returncode, stderr_text)``.
    """
    log_job(job_id, f"Running: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
        env=_subprocess_env,
    )

    if on_start is not None:
        on_start(process)

    # Drain stderr on a separate thread so a large stderr cannot deadlock the
    # process while we stream stdout.
    stderr_chunks = []

    def _drain_stderr():
        for err_line in process.stderr:
            stderr_chunks.append(err_line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    for line in process.stdout:
        line = line.strip()
        if line:
            log_job(job_id, line)

    process.wait()
    stderr_thread.join(timeout=5)

    stderr_text = "".join(stderr_chunks).strip()
    if process.returncode != 0 and stderr_text:
        log_job(job_id, f"stderr: {stderr_text}")

    return process.returncode, stderr_text


# ─── Reusable browser-task job bodies ─────────────────────────────────────────
# Extracted from the scrape/post endpoints so the scheduler can run the exact
# same jobs (via run_job / the browser lock) that a manual click would.

def _scrape_job(job_id, profile_name, max_posts, min_quality):
    """Run the post finder subprocess and return the newest output file."""
    log_job(job_id, "Starting post finder...")
    cmd = [
        sys.executable, "-m", "linkedin_automation.post_finder",
        "--max-posts", str(max_posts),
        "--min-quality", str(min_quality),
        "--profile", profile_name,
    ]
    returncode, _ = run_subprocess(job_id, cmd)

    if returncode == pm.EXIT_LOGIN_REQUIRED:
        raise pm.LoginRequiredError(
            f"Login required. Run: python tools/login_check.py --profile {profile_name}"
        )
    if returncode != 0:
        raise RuntimeError(f"Post finder exited with code {returncode}")

    timeline_dir = pm.get_timeline_dir(profile_name)
    json_files = sorted(
        glob.glob(os.path.join(timeline_dir, "ai_posts_*.json")),
        key=os.path.getmtime, reverse=True,
    )
    if json_files:
        log_job(job_id, f"Posts saved to: {json_files[0]}")
        return {"file": json_files[0]}
    raise RuntimeError("No output file found")


def _post_comments_job(job_id, profile_name, comments_file, count):
    """Run the comment poster subprocess for ``count`` comments from a TXT file."""
    log_job(job_id, f"Posting {count} comment(s) from: {comments_file}")
    cmd = [
        sys.executable, "-m", "linkedin_automation.comment_poster",
        comments_file,
        "--count", str(count),
        "--profile", profile_name,
    ]
    returncode, _ = run_subprocess(job_id, cmd)

    if returncode == pm.EXIT_LOGIN_REQUIRED:
        raise pm.LoginRequiredError(
            f"Login required. Run: python tools/login_check.py --profile {profile_name}"
        )
    if returncode != 0:
        raise RuntimeError(f"Comment poster exited with code {returncode}")

    log_job(job_id, "Finished posting")
    return {"posted": count}


# ─── Scrape-file helpers ───────────────────────────────────────────────────────
# Every pipeline list is now served from the lifecycle store; scrape files are
# read only to *enrich* store records with display metadata (likes/quality) that
# the trimmed record doesn't keep. The pre-store merge/dedupe/filter helpers that
# used to build those lists (merge_posts, merge_comments, _pipeline_comment_urls,
# _posted_urls, _post_score) are gone — see ARCHITECTURE.md "Post lifecycle".

MERGE_WINDOW_DAYS = 7    # only read scrape files touched in the last week


def _files_within_days(dirpath, pattern, days, now=None):
    """Return JSON files in ``dirpath`` matching ``pattern`` whose mtime is within
    ``days``, newest first. Non-recursive (so a ``archived/`` subdir is skipped)."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    dated = []
    for fp in glob.glob(os.path.join(dirpath, pattern)):
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        if mtime >= cutoff:
            dated.append((fp, mtime))
    dated.sort(key=lambda t: t[1], reverse=True)
    return [fp for fp, _ in dated]


def _load_json(path):
    """Read a JSON file, returning None (and logging) on any error."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        logger.debug("Could not read JSON file %s", path, exc_info=True)
        return None


def _dedupe_key(item):
    """Identity for a post or comment: its URL, else a hash of author + first 100
    chars of text. Delegates to ``post_store.post_key`` so the file-merge dedup
    and the lifecycle store share one identity rule and can never diverge."""
    return post_store.post_key(item)


# ─── API: Profiles ───────────────────────────────────────────────────────────

@app.route('/api/profiles', methods=['GET'])
def get_profiles():
    """GET /api/profiles — list all profiles (auto-migrating from .env first)."""
    pm.auto_migrate_from_env()
    data = pm.list_profiles()
    return jsonify(data)


@app.route('/api/profiles', methods=['POST'])
def create_profile():
    """POST /api/profiles — create a new profile from name/username/password."""
    body = request.json
    name = body.get('name', '').strip()
    username = body.get('username', '').strip()
    password = body.get('password', '').strip()
    is_default = body.get('set_default', False)
    
    if not name or not username or not password:
        return jsonify({"error": "name, username, and password are required"}), 400
    
    if pm.get_profile(name):
        return jsonify({"error": f"Profile '{name}' already exists"}), 409
    
    pm.add_profile(name, username, password, set_default=is_default)
    return jsonify({"ok": True, "message": f"Profile '{name}' created"})


@app.route('/api/profiles/<name>/default', methods=['POST'])
def set_default_route(name):
    """POST /api/profiles/<name>/default — set the default profile."""
    if not pm.get_profile(name):
        return jsonify({"error": f"Profile '{name}' not found"}), 404
    pm.set_default_profile(name)
    return jsonify({"ok": True})


@app.route('/api/profiles/<name>', methods=['DELETE'])
def delete_profile(name):
    """DELETE /api/profiles/<name> — remove a profile."""
    if not pm.get_profile(name):
        return jsonify({"error": f"Profile '{name}' not found"}), 404
    pm.remove_profile(name)
    return jsonify({"ok": True})


@app.route('/api/profiles/<name>/config', methods=['GET'])
def get_profile_config_route(name):
    """GET /api/profiles/<name>/config — return the profile's config (creates default on first use)."""
    return jsonify(pm.get_profile_config(name))


@app.route('/api/profiles/<name>/config', methods=['POST'])
def update_profile_config_route(name):
    """POST /api/profiles/<name>/config — deep-merge a PARTIAL config update.

    The body is merged onto what is already stored, so posting one section
    leaves the rest alone.

    It did not used to be. save_profile_config REPLACES the file, and this route
    handed it the request body directly - so a partial update silently deleted
    every key it did not mention. The dashboard's own editor happens to post the
    whole config, which hid the problem; posting just one section wiped the
    profile's persona, keywords, behaviour and scheduler settings, with no
    backup, because data/ is gitignored. That is what "update" must never mean.
    """
    body = request.json
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    stored = pm.read_stored_config(name)
    pm.save_profile_config(name, pm._deep_merge(stored, body))
    # Return the effective (default-merged) config so the client sees the result.
    return jsonify({"ok": True, "config": pm.get_profile_config(name)})


@app.route('/api/profiles/<name>/config/reset', methods=['POST'])
def reset_profile_config_route(name):
    """POST /api/profiles/<name>/config/reset — regenerate the config from defaults."""
    return jsonify({"ok": True, "config": pm.reset_profile_config(name)})


# ─── API: Posts ───────────────────────────────────────────────────────────────

@app.route('/api/posts/<profile_name>', methods=['GET'])
def get_posts(profile_name):
    """Review Posts list = the lifecycle store's NEW bin (the source of truth).

    Reads the reconciled store, so ``len(posts)`` here always equals the NEW
    lifecycle bin. (These used to diverge: this endpoint read only the recent
    ``ai_posts_*.json`` files, while NEW posts live across *every* scrape and in
    the store — so the store could show 48 NEW while this showed 37.) Recent
    scrape files are still read, but only to *enrich* each NEW post with
    display-only metadata (likes/comments/reposts/quality) that the trimmed store
    record doesn't keep.
    """
    store = post_store.load_synced_store(profile_name)
    new_recs = store.by_status(post_store.NEW)

    # Enrichment map: post key → scrape post dict (display metadata only).
    timeline_dir = pm.get_timeline_dir(profile_name)
    files = _files_within_days(timeline_dir, "ai_posts_*.json", MERGE_WINDOW_DAYS)
    enrich = {}
    for data in (d for d in (_load_json(fp) for fp in files) if d is not None):
        for p in data.get("quality_posts", []) or []:
            enrich[post_store.post_key(p)] = p

    posts = []
    for r in new_recs:
        meta = enrich.get(r["key"], {})
        posts.append({
            # Display-only extras from the scrape file (absent for store-only posts).
            "likes": meta.get("likes"),
            "comments": meta.get("comments"),
            "reposts": meta.get("reposts"),
            "quality": meta.get("quality"),
            # The store is the source of truth for identity / content / status.
            "key": r["key"],
            "url": r.get("url", ""),
            "author_name": r.get("author", ""),
            "author": r.get("author", ""),
            "text": r.get("text", ""),
            "post_type": r.get("category", "") or meta.get("post_type", ""),
            "relevance_score": r.get("relevance_score", 0),
            "status": post_store.NEW,
        })

    counts = store.counts()
    logger.info("Review Posts for %s: %d NEW from store (bin=%d), enriched from %d file(s).",
                profile_name, len(posts), counts["NEW"], len(files))
    return jsonify({
        "posts": posts,
        "counts": counts,
        "source": "lifecycle_store",
        "file": files[0] if files else None,
        "files": files,
    })


@app.route('/api/posts/<profile_name>/scrape', methods=['POST'])
def scrape_posts(profile_name):
    """Start a post scraping job."""
    body = request.json or {}
    max_posts = body.get('max_posts', 50)
    min_quality = body.get('min_quality', 10)
    
    job_id = f"scrape_{profile_name}_{int(time.time())}"

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, _scrape_job, profile_name, max_posts, min_quality,
            profile=profile_name, task_type="browser", category="scrape")
    return jsonify({"job_id": job_id})


@app.route('/api/posts/<profile_name>/save', methods=['POST'])
def save_posts(profile_name):
    """Save curated posts (after user deletes some)."""
    body = request.json
    posts = body.get('posts', [])
    source_file = body.get('source_file', '')
    
    # Load the original file to preserve metadata
    if source_file and os.path.exists(source_file):
        with open(source_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {}
    
    # Update with curated posts
    data['quality_posts'] = posts
    data['quality_found'] = len(posts)
    data['curated_at'] = datetime.now().isoformat()
    
    # Save as a new curated file
    timeline_dir = pm.get_timeline_dir(profile_name)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    curated_file = os.path.join(timeline_dir, f'ai_posts_curated_{timestamp}.json')
    
    with open(curated_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    return jsonify({"ok": True, "file": curated_file, "count": len(posts)})


# ─── API: Post lifecycle (NEW / GENERATED / COMMENTED / TRASH) ────────────────

def _lifecycle_record_view(rec):
    """Trim a store record to the fields the dashboard UI needs."""
    return {
        "key": rec.get("key"),
        "url": rec.get("url", ""),
        "author": rec.get("author", ""),
        "text": rec.get("text", ""),
        "category": rec.get("category", ""),
        "relevance_score": rec.get("relevance_score", 0),
        "status": rec.get("status"),
        "trash_reason": rec.get("trash_reason"),
        "comment": rec.get("comment"),
        "scraped_at": rec.get("scraped_at"),
        "generated_at": rec.get("generated_at"),
        "commented_at": rec.get("commented_at"),
    }


@app.route('/api/posts/<profile_name>/lifecycle', methods=['GET'])
def posts_lifecycle(profile_name):
    """Lifecycle counts + posts for a profile.

    Migrates the store from legacy files on first use and reconciles COMMENTED
    from posting_progress.json, so the four bins are always consistent with the
    authoritative posted ledger. Optional ``?status=NEW|GENERATED|COMMENTED|TRASH``
    returns just that bin; otherwise all posts are returned grouped under
    ``posts`` keyed by status.
    """
    store = post_store.load_synced_store(profile_name)
    counts = store.counts()

    status = (request.args.get("status") or "").upper()
    if status in post_store.STATUSES:
        posts = [_lifecycle_record_view(r) for r in store.by_status(status)]
        return jsonify({"counts": counts, "status": status, "posts": posts})

    grouped = {
        s: [_lifecycle_record_view(r) for r in store.by_status(s)]
        for s in post_store.STATUSES
    }
    return jsonify({"counts": counts, "posts": grouped})


@app.route('/api/posts/<profile_name>/reject', methods=['POST'])
def reject_post(profile_name):
    """Move a post → TRASH (manual). Body: ``{"key"|"url": ...}``."""
    body = request.json or {}
    ident = body.get("key") or body.get("url")
    if not ident:
        return jsonify({"error": "key or url is required"}), 400
    store = post_store.PostStore(profile_name)
    if not store.reject(ident, save=True):
        return jsonify({"error": "Post not found in store"}), 404
    return jsonify({"ok": True, "counts": store.counts()})


@app.route('/api/posts/<profile_name>/restore', methods=['POST'])
def restore_post(profile_name):
    """Restore a trashed post → NEW (or GENERATED if it has a draft).

    Body: ``{"key"|"url": ...}``.
    """
    body = request.json or {}
    ident = body.get("key") or body.get("url")
    if not ident:
        return jsonify({"error": "key or url is required"}), 400
    store = post_store.PostStore(profile_name)
    if not store.restore(ident, save=True):
        return jsonify({"error": "Post not found in trash"}), 404
    return jsonify({"ok": True, "counts": store.counts()})


# ─── API: Comments ────────────────────────────────────────────────────────────

def _record_to_comment(rec):
    """Render a GENERATED store record in the shape the Review UI + poster expect.

    Emits BOTH naming conventions (``url``/``post_url``, ``author``/``post_author``)
    because the frontend and ``comment_fields`` each historically read one of them.
    """
    meta = rec.get("comment_meta") or {}
    return {
        "key": rec.get("key"),
        "url": rec.get("url", ""),
        "post_url": rec.get("url", ""),
        "author": rec.get("author", ""),
        "post_author": rec.get("author", ""),
        "post_text": rec.get("text", ""),
        "post_preview": rec.get("text", ""),
        "category": rec.get("category", ""),
        "post_category": rec.get("category", ""),
        "comment": rec.get("comment", "") or "",
        "word_count": meta.get("word_count"),
        "style": meta.get("style", ""),
        "approach": meta.get("approach", ""),
        "reviewed_at": rec.get("reviewed_at"),
    }


@app.route('/api/comments/<profile_name>', methods=['GET'])
def get_comments(profile_name):
    """Review Comments list = the lifecycle store's GENERATED bin (source of truth).

    This used to glob ``comments_*.json``. That was the last file-based reader in
    the pipeline, and ``save_comments`` archives every one of those files on each
    save — so the step went **permanently** empty while the store still held the
    drafts (87 GENERATED vs. 0 shown). Drafts live on the record, so we serve them
    from the store and the tab can no longer disagree with the bin.

    "Already reviewed" is now store state (``reviewed_at``) rather than the
    absence of a file: approving a draft is what removes it from this queue. The
    reviewed count is returned alongside, so a short queue is always explained by
    a number instead of looking like data loss. ``?include_reviewed=1`` returns
    the whole GENERATED bin.

    Invariant: ``total + reviewed_count == counts["GENERATED"]``.
    """
    include_reviewed = (request.args.get("include_reviewed") or "").lower() in ("1", "true", "yes")

    store = post_store.load_synced_store(profile_name)
    recs = store.review_queue(include_reviewed=include_reviewed)
    comments = [_record_to_comment(r) for r in recs]
    counts = store.counts()
    reviewed = store.reviewed_count()

    logger.info(
        "Review Comments for %s: %d draft(s) from store (GENERATED bin=%d, "
        "already reviewed=%d, include_reviewed=%s).",
        profile_name, len(comments), counts["GENERATED"], reviewed, include_reviewed,
    )

    return jsonify({
        "comments": comments,
        "total": len(comments),
        "counts": counts,
        "reviewed_count": reviewed,
        "include_reviewed": include_reviewed,
        "source": "lifecycle_store",
    })


def _write_lifecycle_input(profile_name, records):
    """Serialize the store's NEW records to a generator-shaped input JSON file.

    The generator reads ``{"quality_posts": [...]}`` with per-post ``url`` /
    ``text`` / ``author_name`` keys, then marks each commented post GENERATED in
    the same store (by URL). Only URL-bearing records are written — a post with
    no URL can't be commented on or posted, so it stays NEW.
    """
    quality_posts = []
    for r in records:
        if not r.get("url"):
            continue
        quality_posts.append({
            "url": r["url"],
            "text": r.get("text", ""),
            "author_name": r.get("author", ""),
            "relevance_score": r.get("relevance_score", 0),
            "post_type": r.get("category", ""),
            "should_engage": True,
        })
    timeline_dir = pm.get_timeline_dir(profile_name)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(timeline_dir, f'lifecycle_new_{ts}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({
            "source": "lifecycle_store",
            "generated_at": datetime.now().isoformat(),
            "quality_posts": quality_posts,
        }, f, indent=2, ensure_ascii=False)
    return path, len(quality_posts)


@app.route('/api/comments/<profile_name>/generate', methods=['POST'])
def generate_comments(profile_name):
    """Start a comment generation job driven by the lifecycle store.

    The lifecycle store (posts_db.json) is the source of truth for which posts
    are actionable. We reconcile it, take every NEW post (across ALL scrapes, not
    just the latest file), write those to a generator-shaped input file, and run
    the generator on it.

    There is deliberately **no** ``input_file`` override any more. It existed for
    "advanced use" but in practice the dashboard sent it on every click that
    followed "Save & Continue", which routed generation through a curated file
    instead of the store — including, once, an *empty* one. That silently
    bypassed this whole endpoint for three weeks. The genuine advanced path is
    the CLI: ``python -m linkedin_automation.comment_generator <file>``.
    """
    body = request.json or {}
    model = body.get('model', 'gpt-4o-mini')
    # Optional cap. None/blank/<=0 means "generate for all engaging posts" — there
    # is no rate-limit reason to cap (it's OpenAI-only). A positive int is a ceiling.
    limit = body.get('limit')
    if limit in (None, "", 0):
        limit = None
    else:
        try:
            limit = int(limit)
            if limit <= 0:
                limit = None
        except (TypeError, ValueError):
            limit = None

    # Source of truth: the reconciled lifecycle store, NOT a scrape/curated file.
    # NEW posts live across many scrape files, and the latest file's posts may all
    # already be handled.
    store = post_store.load_synced_store(profile_name)
    new_posts = store.get_posts_by_status(post_store.NEW)
    input_file, actionable = _write_lifecycle_input(profile_name, new_posts)
    if actionable == 0:
        counts = store.counts()
        return jsonify({
            "error": "No NEW posts to generate for. Scrape fresh posts, or "
                     "the queue may already be generated/commented.",
            "counts": counts,
        }), 400

    job_id = f"generate_{profile_name}_{int(time.time())}"
    
    def do_generate(jid, pname, infile, mdl, lmt):
        """Background job: run the comment generator subprocess."""
        log_job(jid, f"Generating comments from: {infile}")
        log_job(jid, f"Comment cap: {lmt if lmt is not None else 'none (all engaging posts)'}")

        cmd = [
            sys.executable, "-m", "linkedin_automation.comment_generator",
            infile,
            "--model", mdl,
            "--profile", pname,
            # Named explicitly, never defaulted. This endpoint is LinkedIn-only
            # until Phase F gives the routes a ?platform= parameter; when it
            # does, this literal becomes the request's platform. Spelling it out
            # means the day X is wired in, an unconverted call site is a visible
            # "linkedin" in the diff rather than an invisible default.
            "--platform", post_store.LINKEDIN,
        ]
        # Only pass --limit when the user set a cap; omitting it means no limit.
        if lmt is not None:
            cmd.extend(["--limit", str(lmt)])

        returncode, _ = run_subprocess(jid, cmd)

        if returncode != 0:
            raise RuntimeError(f"Comment generator exited with code {returncode}")
        
        # Find the output file
        comments_dir = pm.get_comments_dir(pname)
        json_files = sorted(
            glob.glob(os.path.join(comments_dir, "comments_*.json")),
            key=os.path.getmtime, reverse=True
        )
        
        if json_files:
            log_job(jid, f"Comments saved to: {json_files[0]}")
            return {"file": json_files[0]}
        
        raise RuntimeError("No comments file found")
    
    run_job(job_id, do_generate, profile_name, input_file, model, limit,
            profile=profile_name, task_type="api", category="generate_comments")
    return jsonify({"job_id": job_id})


@app.route('/api/comments/<profile_name>/save', methods=['POST'])
def save_comments(profile_name):
    """Approve reviewed drafts: persist edits to the store, then write poster files.

    The **store** is what makes a draft leave the review queue now (``reviewed_at``),
    not the archiving of a file. Critically, the user's edited text is written back
    onto the record — previously an edit made here only ever reached the TXT file,
    so the scheduler (which posts from the store) would post the *unedited* draft.

    An empty list is rejected rather than written: saving nothing used to produce
    a ``Total: 0`` TXT that then became the poster's default input.
    """
    body = request.json or {}
    comments = body.get('comments', [])

    if not comments:
        return jsonify({"error": "No comments to save. The review queue is empty."}), 400

    # 1. Store first — it is the source of truth for what gets posted. Loaded via
    #    load_synced_store so a first-run store is seeded/reconciled before we
    #    mark anything reviewed (a bare PostStore would write an empty store file
    #    here and permanently block the legacy migration).
    store = post_store.load_synced_store(profile_name)
    reviewed = 0
    for c in comments:
        norm = normalize_comment_fields(c)
        ident = c.get("key") or norm["url"]
        if ident and store.mark_reviewed(ident, comment=norm["comment"]):
            reviewed += 1
    store.save()

    comments_dir = pm.get_comments_dir(profile_name)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # 2. Files are now derived output for the poster / the record, not state.
    json_file = os.path.join(comments_dir, f'ready_{timestamp}.json')
    json_data = {
        "generated_at": datetime.now().isoformat(),
        "total": len(comments),
        "curated": True,
        "comments": comments
    }
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)

    # Save TXT for the poster script (canonical format owned by comment_fields)
    txt_file = os.path.join(comments_dir, f'daily_comments_curated_{timestamp}.txt')
    txt_content = comments_to_txt(comments, datetime.now().strftime('%Y-%m-%d %H:%M'))
    with open(txt_file, 'w', encoding='utf-8') as f:
        f.write(txt_content)

    # Archive originals so a stale comments_*.json can't be re-read by the legacy
    # tools. This no longer affects what the Review step shows.
    archive_dir = os.path.join(comments_dir, "archived")
    os.makedirs(archive_dir, exist_ok=True)
    for f_path in glob.glob(os.path.join(comments_dir, "comments_*.json")):
        try:
            dest = os.path.join(archive_dir, os.path.basename(f_path))
            os.rename(f_path, dest)
        except Exception:
            logger.warning("Could not archive original comments file %s", f_path, exc_info=True)

    logger.info("Saved %d comment(s) for %s; %d marked reviewed in the store.",
                len(comments), profile_name, reviewed)
    return jsonify({"ok": True, "json_file": json_file, "txt_file": txt_file,
                    "count": len(comments), "reviewed": reviewed,
                    "counts": store.counts()})


# ─── API: Posting ─────────────────────────────────────────────────────────────

def _write_poster_input(profile_name, records):
    """Serialize GENERATED store records to a poster-readable TXT file.

    Mirrors what the scheduler already does (``scheduler._write_comments_file``),
    so the manual Post step and the scheduled one post the same drafts from the
    same source.
    """
    comments = [_record_to_comment(r) for r in records]
    comments_dir = pm.get_comments_dir(profile_name)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(comments_dir, f'daily_comments_curated_{ts}.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(comments_to_txt(comments, datetime.now().strftime('%Y-%m-%d %H:%M')))
    return path


@app.route('/api/post/<profile_name>', methods=['POST'])
def post_comments(profile_name):
    """Start posting comments, sourced from the store's GENERATED bin.

    This used to glob the newest ``daily_comments_*.txt``, which after an empty
    save resolved to a ``Total: 0`` file and made posting a guaranteed no-op. Now
    the drafts come from the store (reviewed ones first, since those carry the
    user's edits), exactly like the scheduler's posting job. An explicit
    ``comments_file`` still overrides for a hand-built file.
    """
    body = request.json or {}
    comments_file = body.get('comments_file', '')
    count = body.get('count', 1)

    if not comments_file:
        store = post_store.load_synced_store(profile_name)
        generated = store.by_status(post_store.GENERATED)
        # Reviewed drafts are the ones the user explicitly approved — post those
        # first, then fall back to the rest of the bin.
        generated.sort(key=lambda r: (0 if r.get("reviewed_at") else 1))
        if not generated:
            return jsonify({
                "error": "No drafted comments to post. Generate comments first.",
                "counts": store.counts(),
            }), 400
        comments_file = _write_poster_input(profile_name, generated)

    job_id = f"post_{profile_name}_{int(time.time())}"

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, _post_comments_job, profile_name, comments_file, count,
            profile=profile_name, task_type="browser", category="post_comments")
    return jsonify({"job_id": job_id})


# ─── API: Selector Health ─────────────────────────────────────────────────────

@app.route('/api/health/<profile_name>/selectors', methods=['POST'])
def selector_health(profile_name):
    """POST /api/health/<name>/selectors — run the selector health check as a job.

    The job result is the structured health report (status HEALTHY/DEGRADED/BROKEN
    plus per-selector counts) that the selector-health module writes; poll
    /api/jobs/<job_id> for it.
    """
    job_id = f"health_{profile_name}_{int(time.time())}"

    def do_health(jid, pname):
        cmd = [sys.executable, "-m", "linkedin_automation.selector_health", "--profile", pname]
        returncode, _ = run_subprocess(jid, cmd)

        if returncode == pm.EXIT_LOGIN_REQUIRED:
            raise pm.LoginRequiredError(
                f"Login required. Run: python tools/login_check.py --profile {pname}"
            )

        # The script writes the structured result even when status is BROKEN
        # (exit code 1), so read it rather than treating non-zero as failure.
        result_path = os.path.join(pm.get_data_dir(pname), "selector_health.json")
        if os.path.exists(result_path):
            with open(result_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"status": "UNKNOWN", "returncode": returncode}

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, do_health, profile_name,
            profile=profile_name, task_type="browser", category="selector_health")
    return jsonify({"job_id": job_id})


# ─── API: Auto-Connector ─────────────────────────────────────────────────────

@app.route('/api/connector/<profile_name>/stats', methods=['GET'])
def connector_stats(profile_name):
    """Get connection stats for a profile."""
    from .auto_connector import ConnectionTracker
    tracker = ConnectionTracker(profile_name)
    stats = tracker.get_stats()
    return jsonify(stats)


@app.route('/api/connector/<profile_name>/start', methods=['POST'])
def start_connector(profile_name):
    """Start the auto-connector."""
    body = request.json or {}
    search_url = body.get('search_url', '')
    max_requests = body.get('max_requests', 25)
    max_pages = body.get('max_pages', 10)
    note = body.get('note', '')

    if not search_url:
        return jsonify({"error": "search_url is required"}), 400

    job_id = f"connect_{profile_name}_{int(time.time())}"

    def do_connect(jid, pname, url, mx, pg, nt):
        """Background job: run the auto-connector subprocess."""
        log_job(jid, f"Starting auto-connector for {pname}...")

        cmd = [
            sys.executable, "-m", "linkedin_automation.auto_connector",
            url,
            "--max", str(mx),
            "--pages", str(pg),
            "--profile", pname
        ]

        if nt:
            cmd.extend(["--note", nt])

        def _register(proc):
            # Store process ref so the stop-connector endpoint can kill it.
            jobs[jid]["process"] = proc
            jobs[jid]["profile"] = pname

        returncode, _ = run_subprocess(jid, cmd, on_start=_register)

        if returncode == pm.EXIT_LOGIN_REQUIRED:
            raise pm.LoginRequiredError(
                f"Login required. Run: python tools/login_check.py --profile {pname}"
            )
        if returncode != 0:
            raise RuntimeError(f"Auto-connector exited with code {returncode}")

        log_job(jid, "Connector finished")
        return {"completed": True}

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, do_connect, profile_name, search_url, max_requests, max_pages, note,
            profile=profile_name, task_type="browser", category="connector")
    return jsonify({"job_id": job_id})


@app.route('/api/connector/<profile_name>/stop', methods=['POST'])
def stop_connector(profile_name):
    """Stop a running auto-connector."""
    # Method 1: Create a stop file that the connector checks for
    stop_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f".stop_connector_{profile_name}")
    try:
        with open(stop_file, 'w') as f:
            f.write("stop")
    except Exception as e:
        return jsonify({"error": f"Failed to create stop file: {e}"}), 500

    # Method 2: Also try to terminate the subprocess directly
    for jid, job in jobs.items():
        if (job.get("status") == "running" and
            job.get("profile") == profile_name and
            "process" in job):
            try:
                proc = job["process"]
                if proc.poll() is None:  # Still running
                    proc.terminate()
                    log_job(jid, "⛔ Stop requested — terminating process...")
                    job["status"] = "stopped"
            except Exception as e:
                log_job(jid, f"Error terminating: {e}")

    return jsonify({"stopped": True, "message": f"Stop signal sent for {profile_name}"})


@app.route('/api/connector/<profile_name>/history', methods=['GET'])
def connector_history(profile_name):
    """Get recent connection request history."""
    from .auto_connector import ConnectionTracker
    tracker = ConnectionTracker(profile_name)
    recent = tracker.data.get("sent_requests", [])[-50:]  # Last 50
    recent.reverse()
    return jsonify({"requests": recent})


# ─── API: Post Generator & Queue ─────────────────────────────────────────────

@app.route('/api/poster/<profile_name>/queue', methods=['GET'])
def poster_queue(profile_name):
    """Get the post queue."""
    from .post_generator import PostQueue
    queue = PostQueue(profile_name)
    posts = queue.list_queued()
    next_post = queue.get_next()
    next_id = next_post["id"] if next_post else None
    history = queue.history[-20:]
    history.reverse()
    return jsonify({"posts": posts, "next_id": next_id, "history": history})


@app.route('/api/poster/<profile_name>/generate', methods=['POST'])
def poster_generate(profile_name):
    """Generate thought leadership post(s)."""
    body = request.json or {}
    count = body.get('count', 1)
    style = body.get('style', None)
    topic = body.get('topic', None)
    model = body.get('model', 'gpt-4o-mini')

    job_id = f"postgen_{profile_name}_{int(time.time())}"

    def do_generate(jid, pname, cnt, sty, top, mdl):
        """Background job: run the LinkedIn post generator subprocess."""
        from .post_generator import PostGenerator
        gen = PostGenerator(profile_name=pname, model=mdl)
        results = []
        for i in range(cnt):
            log_job(jid, f"Generating post {i+1}/{cnt}...")
            post = gen.generate_thought_leadership(style=sty, topic=top)
            log_job(jid, f"✓ Post #{post.get('id', '?')} created [{post.get('style', '')}]")
            log_job(jid, f"  {post['text'][:120]}...")
            results.append(post)
            if i < cnt - 1:
                time.sleep(1)
        log_job(jid, f"Done — {len(results)} post(s) generated")
        return {"posts": results}

    run_job(job_id, do_generate, profile_name, count, style, topic, model,
            profile=profile_name, task_type="api", category="generate_posts")
    return jsonify({"job_id": job_id})


@app.route('/api/poster/<profile_name>/article', methods=['POST'])
def poster_article(profile_name):
    """Generate a post from an article URL."""
    body = request.json or {}
    url = body.get('url', '')
    model = body.get('model', 'gpt-4o-mini')

    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = f"article_{profile_name}_{int(time.time())}"

    def do_article(jid, pname, article_url, mdl):
        """Background job: generate a post from an article URL."""
        from .post_generator import PostGenerator
        gen = PostGenerator(profile_name=pname, model=mdl)
        log_job(jid, f"Fetching article: {article_url}")
        post = gen.generate_from_article(article_url)
        log_job(jid, f"✓ Article post #{post.get('id', '?')} created")
        log_job(jid, f"  {post['text'][:120]}...")
        return {"post": post}

    run_job(job_id, do_article, profile_name, url, model,
            profile=profile_name, task_type="api", category="generate_article")
    return jsonify({"job_id": job_id})


@app.route('/api/poster/<profile_name>/post', methods=['POST'])
def poster_publish(profile_name):
    """Publish a post to LinkedIn."""
    body = request.json or {}
    post_id = body.get('id', None)

    job_id = f"publish_{profile_name}_{int(time.time())}"

    def do_publish(jid, pname, pid):
        """Background job: publish a queued post to the feed."""
        from .post_generator import PostGenerator
        gen = PostGenerator(profile_name=pname)

        if pid:
            log_job(jid, f"Publishing post #{pid}...")
            success = gen.post_by_id(pid, profile_name=pname)
        else:
            log_job(jid, "Publishing next post in queue...")
            success = gen.post_next(profile_name=pname)

        if success:
            log_job(jid, "✓ Post published to LinkedIn!")
        else:
            raise RuntimeError("Failed to publish post")
        return {"published": success}

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, do_publish, profile_name, post_id,
            profile=profile_name, task_type="browser", category="publish")
    return jsonify({"job_id": job_id})


@app.route('/api/poster/<profile_name>/queue/<int:post_id>', methods=['DELETE'])
def poster_remove(profile_name, post_id):
    """Remove a post from the queue."""
    from .post_generator import PostQueue
    queue = PostQueue(profile_name)
    if queue.remove(post_id):
        return jsonify({"removed": True})
    return jsonify({"error": "Post not found"}), 404


# ─── API: Jobs ────────────────────────────────────────────────────────────────

@app.route('/api/jobs/active', methods=['GET'])
def active_jobs_list():
    """List all active/running jobs."""
    active = get_active_jobs()
    result = []
    for jid, j in active.items():
        result.append({
            "job_id": jid,
            "profile": j.get("profile", ""),
            "task_type": j.get("task_type", ""),
            "category": j.get("category", ""),
            "started": j.get("started", ""),
            "last_log": j["log"][-1] if j["log"] else "",
        })
    return jsonify({"jobs": result})


@app.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    """GET /api/jobs/<job_id> — return a serializable snapshot of a job's state."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    # Filter out non-serializable keys (like subprocess.Popen objects)
    safe_keys = {"status", "progress", "log", "result", "error", "started", "login_required"}
    safe_job = {k: v for k, v in jobs[job_id].items() if k in safe_keys}
    return jsonify(safe_job)


# ─── Scheduled posting (Buffer hybrid) ───────────────────────────────────────
# A front end over the proven pipeline. Every operation here calls the same
# csv_pipeline functions the CLI calls; nothing is reimplemented.
#
# scheduled_posts_state.json remains the single source of truth. These routes
# read it through PipelineState and never keep a parallel copy.

# What a human needs to know per state, in the state's own terms. FAILED and
# COMMENT_FAILED look similar in a table and could not be more different: one
# published nothing, the other left a real post live without its comment.
_SCHEDULED_STATE_HELP = {
    csv_pipeline.PENDING: {
        "label": "Pending",
        "meaning": "queued, not sent to Buffer yet",
        "action": "",
    },
    csv_pipeline.HELD: {
        "label": "Held",
        "meaning": "ready, but Buffer has no scheduled-post slot free",
        "action": "nothing to fix - it goes as soon as a slot opens",
    },
    csv_pipeline.SCHEDULED: {
        "label": "Scheduled",
        "meaning": "created in Buffer, not published yet",
        "action": "",
    },
    csv_pipeline.PUBLISHED: {
        "label": "Published",
        "meaning": "live on LinkedIn, first comment still owed",
        "action": "the comment sweep will pick it up",
    },
    csv_pipeline.COMMENTED: {
        "label": "Done",
        "meaning": "published and commented",
        "action": "",
    },
    csv_pipeline.COMMENT_FAILED: {
        "label": "Comment failed",
        "meaning": "THE POST IS LIVE - the comment did not land",
        "action": "add the comment by hand. Do NOT re-run the post.",
    },
    csv_pipeline.FAILED: {
        "label": "Failed",
        "meaning": "failed before publishing - nothing exists",
        "action": "fix the row and schedule again",
    },
}

# Display order: PROBLEMS, then work to do, then finished.
#
# PENDING heads the work-to-do band - it is the queue you are about to act on.
# But the two problem states stay above it, deliberately: a large pending batch
# must not push a live-but-uncommented post off the top of the table, which is
# the one row where delay actually costs something.
#
# Within work-to-do: PENDING needs a human click, SCHEDULED is waiting on
# Buffer, PUBLISHED is waiting on the sweep. Descending order of "needs you".
_SCHEDULED_STATE_ORDER = (
    csv_pipeline.COMMENT_FAILED,
    csv_pipeline.FAILED,
    csv_pipeline.PENDING,
    csv_pipeline.HELD,
    csv_pipeline.SCHEDULED,
    csv_pipeline.PUBLISHED,
    csv_pipeline.COMMENTED,
)


#: What happened to a row's image, as one value the table can render.
IMAGE_ATTACHED = "attached"   # uploaded to R2 and on the Buffer post
IMAGE_READY = "ready"         # a local file that exists, not uploaded yet
IMAGE_MISSING = "missing"     # a path was given; nothing is there
IMAGE_LOST = "lost"           # a path was given, the post went out without it
IMAGE_NONE = "none"           # no image asked for: text-only, on purpose


def _scheduled_image_view(rec):
    """What happened to this row's image, end to end.

    An image that was asked for and did not make it must never render as a
    blank cell. Blank reads as "text-only, on purpose", which is exactly how a
    calendar-wide image loss stayed invisible until the posts published.
    """
    source_row = rec.get("row") or {}
    url = (rec.get("image_url") or "").strip()
    path = csv_pipeline.row_image_path(source_row)
    column = csv_pipeline.row_image_column(source_row)
    view = {"state": IMAGE_NONE, "url": None, "path": path or None,
            "column": column, "filename": None, "problem": False,
            "label": "Text-only", "detail": "No image for this row."}
    if path:
        view["filename"] = os.path.basename(path)

    if url:
        view.update(state=IMAGE_ATTACHED, url=url, problem=False,
                    label="Image attached",
                    detail="Uploaded and attached to the scheduled post.")
        return view

    if not path:
        # No path at all. Say which column was looked in, so a calendar that
        # is unexpectedly text-only points at the column rather than the rows.
        looked = ", ".join(csv_pipeline.IMAGE_PATH_COLUMNS)
        view["detail"] = ("No image path in this row (looked in: %s)." % looked)
        return view

    if not os.path.isfile(path):
        view.update(state=IMAGE_MISSING, problem=True,
                    label="Image file not found",
                    detail="%s names %s, which is not on disk."
                           % (column, path))
        return view

    if rec.get("post_id"):
        # The post exists and carries no image URL, but the row asked for one.
        view.update(state=IMAGE_LOST, problem=True,
                    label="Image did NOT make it",
                    detail="%s names %s and the file is there, but this post "
                           "was created without it." % (column, path))
        return view

    view.update(state=IMAGE_READY, problem=False, label="Image ready",
                detail="%s - will upload when this row is scheduled." % path)
    return view


def _scheduled_row_view(key, rec):
    """Trim a pipeline state row to what the queue table needs."""
    status = rec.get("status") or csv_pipeline.PENDING
    help_ = _SCHEDULED_STATE_HELP.get(status, {})
    # "failed before publishing - nothing exists" is the right thing to say
    # about almost every FAILED row, and exactly the wrong thing to say about
    # one whose Buffer request was in flight when it died. That row invites a
    # blind re-queue, which is how the same post goes out twice.
    if rec.get("stage") == csv_pipeline.STAGE_CREATE_UNKNOWN:
        help_ = {
            "label": "Failed - outcome UNKNOWN",
            "meaning": "the Buffer request was in flight - a post MAY exist",
            "action": ("check Buffer for a post on this row's date BEFORE "
                       "re-queueing. Do NOT schedule again blind."),
        }
    text = (rec.get("text") or "").strip()
    # `text` is only a LABEL for a row that has not been scheduled yet -
    # queue_rows stores row_preview(), which is already truncated. The whole
    # post lives on the stored row until scheduling composes the final body,
    # so the expand control has to read from whichever is actually complete.
    source_row = rec.get("row") or {}
    composed = text if rec.get("post_id") else ""
    full_text = composed or (source_row.get("post_text") or "").strip() or text
    return {
        "key": key,
        "status": status,
        "label": help_.get("label", status),
        "meaning": help_.get("meaning", ""),
        "action": help_.get("action", ""),
        # Truncated for the table; the full text is not needed to identify a row
        # and a whole post in a cell makes the table unreadable.
        "preview": (text[:160] + "\u2026") if len(text) > 160 else text,
        # The whole post, for the expand control. An operator reviewing what is
        # about to go out needs to read it, not its first line.
        "full_text": full_text,
        "hold_reason": rec.get("hold_reason"),
        "stage": rec.get("stage"),
        # A row whose Buffer request was in flight when it failed is NOT
        # "nothing exists". The queue must not invite a blind re-queue.
        "outcome_unknown": rec.get("stage") == csv_pipeline.STAGE_CREATE_UNKNOWN,
        "post_id": rec.get("post_id"),
        "due_at": rec.get("due_at"),
        "permalink": rec.get("permalink"),
        "image_url": rec.get("image_url"),
        # Not just the URL: what happened to the image, for every state a row
        # can be in. The table renders this, never a bare blank.
        "image": _scheduled_image_view(rec),
        "first_comment_link": rec.get("first_comment_link"),
        "errors": rec.get("errors") or [],
        "updated_at": rec.get("updated_at"),
        # Anything with a post_id exists on LinkedIn. The UI uses this to refuse
        # to offer any control that would imply re-posting.
        "has_post": bool(rec.get("post_id")),
    }


#: Poll-on-render must not become poll-on-every-keystroke. The queue view is
#: re-fetched on tab switches and after every action, and Buffer's free plan
#: allows about a hundred requests a DAY, so the reconcile is shared by every
#: render inside this window.
_RECONCILE_TTL_SECONDS = 120
_reconcile_cache = {}   # profile -> {"at": epoch, "changed": [...], "error": str|None}


def _reconcile_on_render(profile_name, force=False):
    """Bring SCHEDULED rows up to date with what Buffer actually did.

    Runs when the queue is rendered, so the view tells the truth without
    depending on any background loop being switched on - which is exactly how a
    post that published two days ago kept reading "not published yet".

    Never raises. A Buffer outage, a rate limit or a missing channel must leave
    the queue rendering the last known state with a note, not an error page:
    the stale answer is still useful, and a blank screen is not.
    """
    import time as _time

    channel_id = _scheduled_channel_id(profile_name)
    if not channel_id:
        return {"ran": False, "changed": [], "error": None,
                "note": "no Buffer channel configured"}

    cached = _reconcile_cache.get(profile_name)
    if not force and cached and (_time.time() - cached["at"]) < _RECONCILE_TTL_SECONDS:
        return {"ran": False, "changed": cached["changed"],
                "error": cached["error"], "note": "cached",
                "age_seconds": int(_time.time() - cached["at"])}

    state = csv_pipeline.PipelineState(profile_name=profile_name)
    # Cheapest possible early exit: if nothing is awaiting publication there is
    # nothing Buffer can tell us, so do not spend a request finding that out.
    if not [v for v in state.rows.values()
            if v.get("status") in csv_pipeline.RECONCILABLE_STATES
            and v.get("post_id")]:
        _reconcile_cache[profile_name] = {"at": _time.time(), "changed": [],
                                          "error": None}
        return {"ran": False, "changed": [], "error": None,
                "note": "nothing awaiting publication"}

    error = None
    changed = []
    try:
        changed = csv_pipeline.reconcile_published(state, channel_id)
    except buffer_client.BufferRateLimited as exc:
        error = str(exc)
        logger.warning("Reconcile skipped for %s: %s", profile_name, exc)
    except Exception as exc:
        error = "Could not reach Buffer to check for published posts: %s" % exc
        logger.warning("Reconcile failed for %s: %s", profile_name, exc)

    _reconcile_cache[profile_name] = {"at": _time.time(), "changed": changed,
                                      "error": error}
    return {"ran": True, "changed": changed, "error": error, "note": None}


@app.route('/api/scheduled/<profile_name>/reconcile', methods=['POST'])
def scheduled_reconcile(profile_name):
    """POST - force a reconcile now, ignoring the render cache."""
    return jsonify(_reconcile_on_render(profile_name, force=True))


@app.route('/api/scheduled/<profile_name>/queue', methods=['GET'])
def scheduled_queue(profile_name):
    """GET - the scheduled-posting queue, grouped by status.

    Same shape as the posts lifecycle endpoint: counts plus records grouped, in
    one payload, so the front end renders the whole table from a single fetch.

    READ-ONLY. This reads scheduled_posts_state.json and writes nothing.
    """
    # Ask Buffer what it actually did BEFORE reading the state file, so the
    # table below renders the reconciled truth rather than last-known state.
    reconcile = _reconcile_on_render(profile_name)

    state = csv_pipeline.PipelineState(profile_name=profile_name)
    rows = [_scheduled_row_view(k, v) for k, v in state.rows.items()]

    counts = {s: 0 for s in _SCHEDULED_STATE_ORDER}
    grouped = {s: [] for s in _SCHEDULED_STATE_ORDER}
    for row in rows:
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
        grouped.setdefault(status, []).append(row)

    for bucket in grouped.values():
        bucket.sort(key=lambda r: (r.get("due_at") or "", r.get("updated_at") or ""))

    cfg = (pm.get_profile_config(profile_name) or {}).get("scheduled_posting", {})
    identity = (cfg.get("identity_slug") or "").strip()
    return jsonify({
        "counts": counts,
        "order": list(_SCHEDULED_STATE_ORDER),
        # What the poll-on-render just learned. `error` being set means the
        # table is last-known state, and the UI says so rather than implying
        # these statuses were confirmed this second.
        "reconcile": {
            "ran": reconcile.get("ran"),
            "changed": len(reconcile.get("changed") or []),
            "error": reconcile.get("error"),
            "note": reconcile.get("note"),
        },
        # Labels come from the server, not from the first row in a bucket - an
        # empty bucket has no first row, and derived labels made the chips read
        # "comment_failed 0" instead of "Comment failed 0".
        "labels": {s: _SCHEDULED_STATE_HELP.get(s, {}).get("label", s)
                   for s in _SCHEDULED_STATE_ORDER},
        "meanings": {s: _SCHEDULED_STATE_HELP.get(s, {}).get("meaning", "")
                     for s in _SCHEDULED_STATE_ORDER},
        "posts": grouped,
        "total": len(rows),
        "state_file": state.path,
        "config": {
            "buffer_channel_id": (cfg.get("buffer_channel_id") or "").strip(),
            "identity_slug": identity,
            # The sweeper cannot be enabled without an identity to enforce.
            # Surfaced here so the UI can disable the control and say why,
            # rather than letting someone turn it on and find out later.
            "identity_configured": bool(identity),
        },
    })


@app.route('/api/scheduled/<profile_name>/rows', methods=['POST'])
def scheduled_add_rows(profile_name):
    """POST - add rows to the queue as PENDING. Does NOT schedule anything.

    Two inputs, ONE path. A CSV upload (multipart ``file``) and the add-post
    form (a JSON body) are both turned into row dicts and handed to the same
    ``queue_rows``, so a post added by hand and a post from a spreadsheet are
    indistinguishable once queued.

    Queueing is not scheduling. Nothing here contacts Buffer, uploads an image
    or opens a browser - PENDING rows simply sit in the queue until the
    schedule action is used.
    """
    state = csv_pipeline.PipelineState(profile_name=profile_name)

    upload = request.files.get("file")
    if upload is not None:
        raw = upload.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify({"error": "The file is not UTF-8 text. Save the CSV "
                                     "as UTF-8 and try again."}), 400
        try:
            rows = csv_pipeline.parse_rows(text)
        except csv_pipeline.RowError as exc:
            return jsonify({"error": str(exc)}), 400
        source = "csv"
    else:
        body = request.json or {}
        if not isinstance(body, dict):
            return jsonify({"error": "Request body must be a JSON object"}), 400
        # Only the known columns, so a stray field cannot change a row's
        # identity hash and silently create a duplicate.
        rows = [{c: (body.get(c) or "") for c in csv_pipeline.COLUMNS}]
        source = "form"

    if not rows:
        return jsonify({"error": "No rows found"}), 400

    accepted, rejected, skipped, warnings = csv_pipeline.queue_rows(rows, state)
    return jsonify({
        "ok": True,
        "source": source,
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "warnings": warnings,
        "counts": {"accepted": len(accepted), "rejected": len(rejected),
                   "skipped": len(skipped), "read": len(rows)},
    })


def _scheduled_channel_id(profile_name):
    cfg = (pm.get_profile_config(profile_name) or {}).get("scheduled_posting", {})
    return (cfg.get("buffer_channel_id") or "").strip()


@app.route('/api/scheduled/<profile_name>/schedule/preflight', methods=['GET'])
def scheduled_preflight(profile_name):
    """GET - everything the confirmation dialog needs, before anything fires.

    Read-only. Scheduling creates real posts that publish on a timer and cannot
    be unpublished, so the dialog names the account the channel id ACTUALLY
    resolves to rather than a label kept in local config, which could go stale
    and still read like the dev account.
    """
    state = csv_pipeline.PipelineState(profile_name=profile_name)
    pending = csv_pipeline.pending_rows(state)
    channel_id = _scheduled_channel_id(profile_name)

    problems = []
    if not channel_id:
        problems.append("No Buffer channel configured. Set "
                        "scheduled_posting.buffer_channel_id in this profile.")
    if not pending:
        problems.append("Nothing is pending. Add rows to the queue first.")

    slots = None
    channel = None
    if channel_id:
        try:
            slots = buffer_client.scheduled_slots(channel_id)
        except Exception as exc:
            problems.append("Could not read Buffer's scheduled-post limit: %s" % exc)
        try:
            channel = buffer_client.get_channel(channel_id)
        except Exception as exc:
            # Never invent a name. If the id cannot be resolved, say so and let
            # the dialog show the raw id instead of a reassuring guess.
            problems.append("Could not resolve the Buffer channel: %s" % exc)

    will_send = len(pending) if slots is None else min(len(pending), slots["free"])
    return jsonify({
        "pending_count": len(pending),
        "slots": slots,
        # What the confirmation must actually say. Promising to schedule 15 when
        # Buffer will take 3 is how the wall of LimitReachedError read as a bug
        # in the calendar rather than a plan limit.
        "will_send": will_send,
        "will_hold": max(0, len(pending) - will_send),
        "channel_id": channel_id,
        "channel": channel,
        "ready": not problems,
        "problems": problems,
        "previews": [csv_pipeline.row_preview(r, 80) for r in pending[:5]],
    })


def _scheduled_schedule_job(job_id, profile_name, channel_id):
    """Run the proven schedule pass. Nothing here is a reimplementation."""
    state = csv_pipeline.PipelineState(profile_name=profile_name)
    rows = csv_pipeline.pending_rows(state)
    log_job(job_id, "Scheduling %d pending row(s) to channel %s"
            % (len(rows), channel_id))
    if not rows:
        return {"scheduled": 0, "failed": 0, "results": [],
                "note": "nothing pending"}

    try:
        # max_age=0: this decides what actually gets sent, so it must not act
        # on a cached count.
        slots = buffer_client.scheduled_slots(channel_id, max_age=0)
        log_job(job_id, "Buffer slots: %d of %d used, %d free"
                % (slots["used"], slots["limit"], slots["free"]))
        max_new = slots["free"]
        slot_limit = slots["limit"]
    except Exception as exc:
        # Without a slot count we still schedule, and a capacity error maps to
        # HELD anyway - we simply lose the ability to hold rows in advance.
        log_job(job_id, "Could not read Buffer's slot count (%s); "
                        "scheduling without a budget" % exc)
        max_new = None
        slot_limit = None

    results = csv_pipeline.schedule_pass(rows, channel_id, state,
                                         profile_name=profile_name,
                                         max_new=max_new,
                                         slot_limit=slot_limit)
    scheduled = [r for r in results if r.get("status") == csv_pipeline.SCHEDULED]
    failed = [r for r in results if r.get("status") == csv_pipeline.FAILED]
    on_hold = [r for r in results if r.get("status") == csv_pipeline.HELD]
    for r in results:
        if r.get("status") == csv_pipeline.SCHEDULED:
            log_job(job_id, "scheduled %s due %s"
                    % (r.get("post_id"), r.get("due_at")))
        elif r.get("status") == csv_pipeline.HELD:
            log_job(job_id, "held: %s" % r.get("note"))
        else:
            log_job(job_id, "FAILED at %s: %s"
                    % (r.get("stage"), "; ".join(r.get("errors") or [])))
    if on_hold:
        log_job(job_id, "%d row(s) held - no Buffer slot free" % len(on_hold))
    return {
        "scheduled": len(scheduled),
        "failed": len(failed),
        "held": len(on_hold),
        "results": [{"key": r["key"], "status": r.get("status"),
                     "post_id": r.get("post_id"), "due_at": r.get("due_at"),
                     "stage": r.get("stage"), "errors": r.get("errors") or []}
                    for r in results],
    }


@app.route('/api/scheduled/<profile_name>/schedule', methods=['POST'])
def scheduled_schedule(profile_name):
    """POST - schedule every PENDING row through Buffer.

    task_type is "api": this uploads to R2 and calls Buffer, and never opens a
    browser, so it neither takes nor waits on the browser lock.

    Acts on PENDING rows ONLY. A row that already has a post is not in
    pending_rows, and schedule_pass refuses it again on its own - two
    independent guards against the one thing that must never happen.
    """
    channel_id = _scheduled_channel_id(profile_name)
    if not channel_id:
        return jsonify({"error": "No Buffer channel configured for this "
                                 "profile. Set scheduled_posting."
                                 "buffer_channel_id first."}), 400

    if not can_start_scheduling_task(profile_name):
        return jsonify({"error": "A scheduling pass is already running for this "
                                 "profile (the post drain, or another Schedule "
                                 "click). Wait for it to finish - running two "
                                 "at once could post the same row twice."}), 409

    state = csv_pipeline.PipelineState(profile_name=profile_name)
    if not csv_pipeline.pending_rows(state):
        return jsonify({"ok": True, "job_id": None,
                        "note": "Nothing pending or held - nothing to schedule."})

    job_id = f"buffer_schedule_{profile_name}_{int(time.time())}"
    run_job(job_id, _scheduled_schedule_job, profile_name, channel_id,
            profile=profile_name, task_type="api", category="buffer_schedule")
    return jsonify({"ok": True, "job_id": job_id})


@app.route('/api/scheduled/<profile_name>/rows/<key>', methods=['DELETE'])
def scheduled_delete_row(profile_name, key):
    """DELETE - remove a row from OUR queue.

    This deletes a RECORD, not a post. A row with a post_id refers to something
    that already exists on Buffer, and possibly on LinkedIn, and nothing here
    can take that back.

    Deleting such a row is allowed - an operator may legitimately want the
    record gone - but only with ``?confirm_live=1``, so it cannot happen from a
    single click by someone who thinks it unschedules the post. The response
    says exactly what was and was not affected.
    """
    state = csv_pipeline.PipelineState(profile_name=profile_name)
    entry = state.get(key)
    if key not in state.rows:
        return jsonify({"error": "No such row in the queue"}), 404

    has_post = bool(entry.get("post_id"))
    confirmed = request.args.get("confirm_live") in ("1", "true", "yes")
    if has_post and not confirmed:
        return jsonify({
            "error": "This row has a live Buffer post (%s). Removing it here "
                     "deletes only our record - the post stays scheduled or "
                     "published. Re-send with confirm_live=1 to remove the "
                     "record anyway." % entry.get("post_id"),
            "has_post": True,
            "post_id": entry.get("post_id"),
            "permalink": entry.get("permalink"),
        }), 409

    removed = csv_pipeline.delete_row(state, key)
    return jsonify({
        "ok": True,
        "removed": key,
        "had_post": has_post,
        "post_id": removed.get("post_id") if removed else None,
        "note": ("Our record is gone. The Buffer post still exists - this did "
                 "not unschedule or unpublish anything."
                 if has_post else "Removed from the queue."),
    })


# ─── Scheduler ────────────────────────────────────────────────────────────────
# The scheduler runs its own actions through run_job / the browser lock, exactly
# like a manual click. These executor callbacks are the only coupling; the engine
# itself (scheduler.py) has no Flask imports.

def _sched_submit_post_job(profile_name, comments_file, count):
    """Start a scheduled posting job unless the browser lock is held."""
    if not can_start_browser_task(profile_name):
        logger.info("Scheduler: browser busy for %s, post job not submitted", profile_name)
        return None
    job_id = f"sched_post_{profile_name}_{int(time.time())}"
    run_job(job_id, _post_comments_job, profile_name, comments_file, count,
            profile=profile_name, task_type="browser", category="scheduled_post")
    return job_id


def _sched_submit_scrape_job(profile_name, max_posts, min_quality):
    """Start a scheduled scrape job unless the browser lock is held."""
    if not can_start_browser_task(profile_name):
        logger.info("Scheduler: browser busy for %s, scrape job not submitted", profile_name)
        return None
    job_id = f"sched_scrape_{profile_name}_{int(time.time())}"
    run_job(job_id, _scrape_job, profile_name, max_posts, min_quality,
            profile=profile_name, task_type="browser", category="scheduled_scrape")
    return job_id


scheduler_engine = scheduler_mod.Scheduler(
    submit_post_job=_sched_submit_post_job,
    submit_scrape_job=_sched_submit_scrape_job,
    browser_available=can_start_browser_task,
    list_profiles=lambda: list(pm.list_profiles().get("profiles", {}).keys()),
)


# --- Post drain feeder -------------------------------------------------------
#
# The second background feeder. Where the scheduler above drives the BROWSER to
# post comments, this one only talks to Buffer's API to push HELD posts into
# slots that have freed up. Separate engine, separate switch, separate status -
# the two must never be mistaken for each other in the UI.

def _drain_feed_job(job_id, profile_name, channel_id, free):
    """Feed up to ``free`` HELD rows into Buffer. Reuses the proven pass."""
    state = csv_pipeline.PipelineState(profile_name=profile_name)
    rows = csv_pipeline.held_rows(state)
    if not rows:
        return {"scheduled": 0, "held": 0, "failed": 0, "results": [],
                "note": "nothing held"}

    log_job(job_id, "Drain: %d held row(s), %d slot(s) free" % (len(rows), free))
    slot_limit = None
    try:
        slots = buffer_client.scheduled_slots(channel_id, max_age=0)
        # Re-read rather than trusting the count the tick passed in: minutes may
        # have passed, and over-sending is what HELD exists to prevent.
        free = slots["free"]
        slot_limit = slots["limit"]
        log_job(job_id, "Buffer slots now: %d of %d used, %d free"
                % (slots["used"], slots["limit"], free))
    except Exception as exc:
        log_job(job_id, "Could not re-read Buffer's slot count (%s); "
                        "using the count from the check" % exc)

    if free <= 0:
        return {"scheduled": 0, "held": len(rows), "failed": 0, "results": [],
                "note": "no slot free by the time the feed ran"}

    results = csv_pipeline.schedule_pass(rows, channel_id, state,
                                         profile_name=profile_name,
                                         max_new=free, slot_limit=slot_limit)
    scheduled = [r for r in results if r.get("status") == csv_pipeline.SCHEDULED]
    failed = [r for r in results if r.get("status") == csv_pipeline.FAILED]
    on_hold = [r for r in results if r.get("status") == csv_pipeline.HELD]
    for r in scheduled:
        log_job(job_id, "drained %s -> scheduled %s due %s"
                % (r["key"], r.get("post_id"), r.get("due_at")))
    for r in failed:
        log_job(job_id, "FAILED at %s: %s"
                % (r.get("stage"), "; ".join(r.get("errors") or [])))
    log_job(job_id, "Drain done: %d fed, %d still held, %d failed"
            % (len(scheduled), len(on_hold), len(failed)))
    return {
        "scheduled": len(scheduled), "held": len(on_hold),
        "failed": len(failed),
        "results": [{"key": r["key"], "status": r.get("status"),
                     "post_id": r.get("post_id"), "due_at": r.get("due_at"),
                     "errors": r.get("errors") or []} for r in results],
    }


def _drain_submit_feed(profile_name, channel_id, free):
    """Start a drain feed unless another schedule pass is already running."""
    if not can_start_scheduling_task(profile_name):
        logger.info("PostDrain: a schedule pass is already running for %s, "
                    "feed not submitted", profile_name)
        return None
    job_id = f"post_drain_{profile_name}_{int(time.time())}"
    run_job(job_id, _drain_feed_job, profile_name, channel_id, free,
            profile=profile_name, task_type="api",
            category=post_drain_mod.JOB_CATEGORY)
    return job_id


def _drain_held_rows(profile_name):
    """HELD source rows for a profile, earliest first."""
    return csv_pipeline.held_rows(
        csv_pipeline.PipelineState(profile_name=profile_name))


drain_engine = post_drain_mod.PostDrain(
    channel_fn=_scheduled_channel_id,
    held_fn=_drain_held_rows,
    slots_fn=buffer_client.scheduled_slots,
    submit_feed=_drain_submit_feed,
    scheduling_available=can_start_scheduling_task,
    list_profiles=lambda: list(pm.list_profiles().get("profiles", {}).keys()),
)


@app.route('/api/drain/<profile_name>/status', methods=['GET'])
def drain_status(profile_name):
    """GET - post-drain state: on/off, interval, held count, last check/feed.

    ``?slots=1`` adds a live Buffer slot read (one round trip); the polling
    status line omits it.
    """
    want_slots = request.args.get("slots") in ("1", "true", "yes")
    return jsonify(drain_engine.status(profile_name, include_slots=want_slots))


@app.route('/api/drain/<profile_name>/toggle', methods=['POST'])
def drain_toggle(profile_name):
    """POST - turn the post drain on or off. Body: ``{"enabled": bool}``."""
    body = request.json or {}
    enabled = body.get("enabled")
    if enabled is None:
        return jsonify({"error": "enabled (bool) is required"}), 400
    if enabled and not _scheduled_channel_id(profile_name):
        # Switching on a feeder that cannot possibly feed anything would show a
        # green light and do nothing at all.
        return jsonify({"error": "No Buffer channel configured for this "
                                 "profile. Set scheduled_posting."
                                 "buffer_channel_id before enabling the "
                                 "drain."}), 400
    drain = drain_engine.toggle(profile_name, bool(enabled))
    return jsonify({"ok": True, "drain": drain})


@app.route('/api/drain/<profile_name>/config', methods=['POST'])
def drain_config_route(profile_name):
    """POST - deep-merge a partial drain config (``interval_minutes``)."""
    body = request.json
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    drain = drain_engine.update_config(profile_name, body)
    return jsonify({"ok": True, "drain": drain})


@app.route('/api/drain/<profile_name>/run-now', methods=['POST'])
def drain_run_now(profile_name):
    """POST - run one drain check immediately, ignoring the interval."""
    result = drain_engine.run_now(profile_name)
    return jsonify({"ok": True, "result": result})


@app.route('/api/scheduler/<profile_name>/status', methods=['GET'])
def scheduler_status(profile_name):
    """GET — scheduler state: master flag, per-job windows with today's rolled
    fire times + skip flags, count ranges, and recent scheduled runs."""
    return jsonify(scheduler_engine.status(profile_name))


@app.route('/api/scheduler/<profile_name>/toggle', methods=['POST'])
def scheduler_toggle(profile_name):
    """POST — enable/disable the master switch or a specific job.

    Body: ``{"job": "master"|"post_comments"|"scrape", "enabled": bool}``.
    """
    body = request.json or {}
    target = body.get("job", "master")
    enabled = body.get("enabled")
    if enabled is None:
        return jsonify({"error": "enabled (bool) is required"}), 400
    if target not in ("master",) + scheduler_mod.JOB_TYPES:
        return jsonify({"error": f"unknown job target '{target}'"}), 400
    sched = scheduler_engine.toggle(profile_name, target, bool(enabled))
    return jsonify({"ok": True, "scheduler": sched})


@app.route('/api/scheduler/<profile_name>/config', methods=['POST'])
def scheduler_config(profile_name):
    """POST — deep-merge a partial scheduler config (windows/counts/skip_chance)."""
    body = request.json
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    sched = scheduler_engine.update_config(profile_name, body)
    return jsonify({"ok": True, "scheduler": sched})


@app.route('/api/scheduler/<profile_name>/run-now', methods=['POST'])
def scheduler_run_now(profile_name):
    """POST — fire a job immediately for testing, ignoring window/skip/min-gap.

    Body: ``{"job": "post_comments"|"scrape"}``.
    """
    body = request.json or {}
    job = body.get("job")
    if job not in scheduler_mod.JOB_TYPES:
        return jsonify({"error": "job must be post_comments or scrape"}), 400
    result = scheduler_engine.run_now(profile_name, job)
    return jsonify({"ok": True, "result": result})


# ─── Serve Frontend ──────────────────────────────────────────────────────────

@app.route('/')
def index():
    """GET / — serve the single-page dashboard UI."""
    return send_file(os.path.join(_PACKAGE_DIR, 'templates', 'dashboard.html'))


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    pm.auto_migrate_from_env()
    print("\n" + "=" * 50)
    print("  LinkedIn Automation Dashboard")
    print("  http://localhost:6500")
    print("=" * 50 + "\n")
    # Start the background scheduler. use_reloader is left on (Flask debug), so
    # only start in the reloader's child process to avoid two scheduler threads.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        scheduler_engine.start()
        drain_engine.start()
    app.run(debug=True, port=6500)