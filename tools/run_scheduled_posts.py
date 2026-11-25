"""Run the CSV -> scheduled post -> first comment pipeline.

TWO PASSES, because Buffer publishes at dueAt and a content calendar schedules
days apart:

    schedule   validate every row and create every Buffer post. Fast.
    comment    sweep for posts that have since published and comment them.
               Non-blocking, idempotent, safe to run on a timer.

    uv run python tools/run_scheduled_posts.py schedule --csv posts.csv \\
        --channel-id <id> --profile dev
    uv run python tools/run_scheduled_posts.py comment --profile dev \\
        --expect-identity your-profile-slug

    uv run python tools/run_scheduled_posts.py topup --profile tinderbox \
        --platform pinterest [--reserve 2] [--dry-run] [--log-file F]

`topup` sends the channel's earliest queued rows into Buffer up to its free
slots minus a reserve, from stored state (no CSV), after the board and link
checks in the profile overlay (linkedin_automation/topup.py). It is what the
Windows task from tools/setup_topup_task.ps1 runs.

Books and holds (F7, linkedin_automation/books.py), for a channel whose
overlay has a `books` section. None of these writes to Buffer:

    books          --profile P --platform C    every book: pins, in state, held, new
    add-book       --profile P --platform C --book FOLDER [--dry-run]
    recheck-holds  --profile P --platform C [--kind board|link] [--dry-run]
    release-hold   --profile P --platform C --key SOURCE_ID ... [--dry-run]
    hold           --profile P --platform C --key SOURCE_ID ... --kind K --reason R
    holds          --profile P --platform C    every hold with its reason

YouTube Shorts (V1, linkedin_automation/youtube_channel.py), for a profile with
a scheduled_posting.platforms.youtube overlay. Each takes the production gate
and the channel's publisher lock; none is run by any task or drain:

    youtube-intake    --profile P [--dry-run]   queue new C-NNN folders
    youtube-schedule  --profile P [--max-uploads N] [--dry-run]
                      date queued items, upload them private with publishAt
    youtube-status    --profile P               local state and today's quota
    youtube-import    --profile P [--dry-run]   video-publisher's history (V1-M)
    youtube-cutover   --profile P [--dry-run]   move the brand here (Jeff runs it)
    youtube-thumbnails --profile P [--max N] [--dry-run]
                      poster thumbnails on uploaded videos (after cutover)

YouTube commenting (Y1, linkedin_automation/youtube_engage.py, DEC-061). Same
gate and lock. Nothing here is on a schedule, and engage posts at most one
approved comment per run:

    youtube-discover  --profile P --query Q [--dry-run]   suggest channels; adds nothing
    youtube-watch     --profile P [--add ID [--title T] | --remove ID]
                      show or edit the watch list
    youtube-find      --profile P [--dry-run]   recent watch-list videos and
                      viewers' comments on our videos, as NEW
    youtube-draft     --profile P [--limit N] [--dry-run]   NEW -> GENERATED
    youtube-engage    --profile P [--dry-run]   post one approved comment if
                      pacing allows, else say when the next is allowed
`comment` is the one to put on a schedule - every 15 minutes is ample, since it
does nothing for rows whose posts have not published yet.

Nothing here can re-publish a post. A row that already has one is skipped by the
schedule pass, whatever else failed.
"""

import argparse
import logging
import os
import sys

_sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _sys_path not in sys.path:
    sys.path.insert(0, _sys_path)

from dotenv import load_dotenv  # noqa: E402

from linkedin_automation import channels  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import platforms  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402
from linkedin_automation import topup  # noqa: E402
from linkedin_automation import books  # noqa: E402
from linkedin_automation import youtube_channel as yt  # noqa: E402
from linkedin_automation import youtube_engage as ye  # noqa: E402

load_dotenv()


def _make_output_unicode_safe():
    """Stop a Windows console encoding from crashing a completed run.

    The run summary quotes post text, and real posts carry emoji and typographic
    dashes. On Windows stdout defaults to cp1252, which cannot encode them - so
    a schedule pass would CREATE every post and then die printing the report,
    leaving the operator with a traceback and no record of what was scheduled.
    The posts existed; the evidence did not.

    Reconfiguring at the entry point fixes it where the problem actually is -
    the terminal - rather than mangling the text everywhere it might be shown.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _books_command(ap, args):
    """F7: books and holds. None of these writes to Buffer."""
    if not args.profile:
        ap.error("%s needs --profile" % args.pass_name)
    name = args.pass_name
    try:
        if name == "books":
            code, _s = books.list_books(args.profile, args.platform)
        elif name == "holds":
            code, _s = books.list_holds(args.profile, args.platform)
        elif name == "add-book":
            if not args.book:
                ap.error("add-book needs --book")
            code, _s = books.add_book(args.profile, args.platform, args.book,
                                      dry_run=args.dry_run)
        elif name == "recheck-holds":
            code, _s = books.recheck_holds(args.profile, args.platform,
                                           kinds=args.kind,
                                           dry_run=args.dry_run)
        elif name == "release-hold":
            if not args.key:
                ap.error("release-hold needs --key")
            code, _s = books.release_hold(args.profile, args.platform,
                                          args.key, dry_run=args.dry_run)
        else:
            if not args.key or not args.reason or not args.kind or len(args.kind) != 1:
                ap.error("hold needs --key, one --kind and --reason")
            code, _s = books.hold_pins(args.profile, args.platform, args.key,
                                       args.kind[0], args.reason,
                                       dry_run=args.dry_run)
    except books.BooksRefused as exc:
        print("%s: REFUSED: %s" % (name, exc))
        return books.EXIT_REFUSED
    return code


def _youtube_engage_command(ap, args, name):
    """The Y1 commands. Each returns its exit code."""
    if name == "discover":
        if not args.query:
            ap.error("youtube-discover needs --query")
        return ye.discover(args.profile, args.query, dry_run=args.dry_run)[0]
    if name == "find":
        return ye.find(args.profile, dry_run=args.dry_run)[0]
    if name == "draft":
        if args.limit < 0:
            ap.error("--limit must be >= 0")
        return ye.draft(args.profile, limit=args.limit, dry_run=args.dry_run)[0]
    if name == "engage":
        return ye.engage(args.profile, dry_run=args.dry_run)[0]
    # watch
    if args.add and args.remove:
        ap.error("youtube-watch takes --add or --remove, not both")
    try:
        if args.add:
            added = ye.add_to_watch_list(args.profile, args.add, title=args.title)
            print("youtube-watch %s: %s %s" % (
                args.profile, "added" if added else "already listed:", args.add))
        elif args.remove:
            removed = ye.remove_from_watch_list(args.profile, args.remove)
            print("youtube-watch %s: %s %s" % (
                args.profile, "removed" if removed else "not listed:", args.remove))
        cfg = ye.engage_config(args.profile)
    except yt.YouTubeRefused as exc:
        print("youtube-watch %s: REFUSED: %s" % (args.profile, exc))
        return ye.EXIT_REFUSED
    for e in cfg.watch_list:
        subs = e.get("subscribers")
        print("  %s  %-40s %s" % (e["channel_id"], (e.get("title") or "")[:40],
                                  "-" if subs is None else "%s subscribers" % subs))
    print("youtube-watch %s: %d channel(s) on the watch list"
          % (args.profile, len(cfg.watch_list)))
    return ye.EXIT_OK


def main():
    _make_output_unicode_safe()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pass_name", choices=[
        "schedule", "comment", "status", "topup",
        "books", "add-book", "recheck-holds", "release-hold", "hold", "holds",
        "youtube-intake", "youtube-schedule", "youtube-status", "youtube-import",
        "youtube-cutover", "youtube-thumbnails", "youtube-discover", "youtube-watch", "youtube-find",
        "youtube-draft", "youtube-engage"])
    ap.add_argument("--csv", help="the content calendar (schedule pass)")
    ap.add_argument("--channel-id",
                    help="Buffer channel id. Defaults to the resolved "
                         "value for --profile/--platform; pass it only "
                         "to override.")
    ap.add_argument("--platform", default=platforms.DEFAULT,
                    help="which platform's settings to resolve "
                         "(default linkedin)")
    ap.add_argument("--profile", default=None,
                    help="browser profile for the comment pass")
    ap.add_argument("--expect-identity", default=None, metavar="SLUG",
                    help="abort commenting unless the logged-in profile URL "
                         "contains this. The DEFAULT profile is the real "
                         "account, so set it.")
    ap.add_argument("--wait", action="store_true",
                    help="comment pass: block until a due post publishes, "
                         "instead of leaving it for the next sweep")
    ap.add_argument("--allow-production", action="store_true",
                    help="allow the schedule or comment pass to run against "
                         "a declared PRODUCTION identity")
    ap.add_argument("--reserve", type=int, default=None,
                    help="topup: Buffer slots to leave free (default: the "
                         "overlay's topup_reserve, else 2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="topup: say what would be sent; no Buffer or state "
                         "write")
    ap.add_argument("--book", default=None,
                    help="add-book: the book folder under the content root")
    ap.add_argument("--kind", action="append", default=None,
                    choices=["board", "link", "review", "operator"],
                    help="recheck-holds: board or link (repeatable; default "
                         "both). hold: the hold kind")
    ap.add_argument("--key", nargs="+", default=None, metavar="SOURCE_ID",
                    help="release-hold / hold: the pins' source ids")
    ap.add_argument("--reason", default=None, help="hold: why")
    ap.add_argument("--max-uploads", type=int, default=None, metavar="N",
                    help="youtube-schedule: upload at most N this run")
    ap.add_argument("--max", type=int, default=None, metavar="N",
                    help="youtube-thumbnails: set at most N this run")
    ap.add_argument("--query", default=None,
                    help="youtube-discover: what to search channels for")
    ap.add_argument("--add", default=None, metavar="CHANNEL_ID",
                    help="youtube-watch: add this channel to the watch list")
    ap.add_argument("--remove", default=None, metavar="CHANNEL_ID",
                    help="youtube-watch: remove this channel")
    ap.add_argument("--title", default="",
                    help="youtube-watch --add: the channel's title, shown in lists")
    ap.add_argument("--limit", type=int, default=5,
                    help="youtube-draft: draft at most N (default 5)")
    ap.add_argument("--log-file", default=None,
                    help="also append this run's log to this file")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.log_file:
        parent = os.path.dirname(args.log_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handler = logging.FileHandler(args.log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(handler)

    if args.pass_name in ("books", "add-book", "recheck-holds", "release-hold",
                          "hold", "holds"):
        return _books_command(ap, args)

    if args.pass_name.startswith("youtube-"):
        if not args.profile:
            ap.error("%s needs --profile" % args.pass_name)
        if args.max_uploads is not None and args.max_uploads < 0:
            ap.error("--max-uploads must be >= 0")
        name = args.pass_name[len("youtube-"):]
        if name in ("discover", "watch", "find", "draft", "engage"):
            return _youtube_engage_command(ap, args, name)
        if name == "schedule":
            code, _s = yt.schedule(args.profile, max_uploads=args.max_uploads,
                                   dry_run=args.dry_run)
        elif name == "status":
            code, _s = yt.status(args.profile)
        elif name == "thumbnails":
            if args.max is not None and args.max < 0:
                ap.error("--max must be >= 0")
            code, _s = yt.thumbnails(args.profile, dry_run=args.dry_run,
                                     max_items=args.max)
        else:
            code, _s = {"intake": yt.intake, "import": yt.import_history,
                        "cutover": yt.cutover}[name](args.profile,
                                                    dry_run=args.dry_run)
        return code

    if args.pass_name == "topup":
        if not args.profile:
            ap.error("topup needs --profile")
        code, _summary = topup.run(args.profile, args.platform,
                                   reserve=args.reserve, dry_run=args.dry_run)
        return code

    # ONE source for the channel id and the identity. These used to be
    # CLI-only, which made this tool an independent SECOND source of the
    # same settings the dashboard read from config - and two sources are
    # two chances to post to the wrong account. The flags now OVERRIDE
    # the resolver rather than replacing it.
    try:
        resolved = pm.resolve_scheduled(args.profile, args.platform)
    except pm.ScheduledConfigError as exc:
        print("REFUSING: %s" % exc)
        return 2
    channel_id = args.channel_id or resolved["channel_id"]
    expect_identity = args.expect_identity or resolved["identity_slug"]

    state = cp.PipelineState(profile_name=args.profile,
                             platform=args.platform)

    if args.pass_name == "status":
        print(cp.summarize(
            [dict(key=k, row={}, **{kk: vv for kk, vv in v.items()
                                    if kk != "row"})
             for k, v in state.rows.items()], []))
        return 0

    if args.pass_name == "schedule":
        if not args.csv:
            ap.error("schedule needs --csv")
        if not channel_id:
            ap.error(
                "no Buffer channel id: pass --channel-id, or set the "
                "buffer_channel_id for profile %r on platform %r"
                % (args.profile, args.platform))
        rows = cp.read_rows(args.csv)
        print("read %d rows from %s" % (len(rows), args.csv))
        # The identity guard now lives INSIDE schedule_pass, so every caller
        # is covered rather than just this one. It raises rather than returning
        # a value that could be ignored.
        try:
            results = cp.schedule_pass(rows, channel_id, state,
                                       profile_name=args.profile,
                                       key=resolved["api_key"],
                                       platform=args.platform,
                                       identity_slug=expect_identity,
                                       allow_production=args.allow_production)
        except cp.IdentityRefused as exc:
            print("REFUSING: %s" % exc)
            return 2
        except pm.ProductionAccessRefused as exc:
            print("REFUSING: %s" % exc)
            return pm.EXIT_PRODUCTION_REFUSED
        print(cp.summarize(results, []))
        failed = [r for r in results if r.get("status") == cp.FAILED]
        return 1 if failed else 0

    # Checked FIRST, because it is the more specific answer. Asking an X run
    # to set an identity_slug sends the operator to fix a config field that
    # would not have helped: X has no comment pass at all. Only a channel whose
    # link_placement is first_comment has one.
    if (channels.resolve(args.platform).link_placement
            != channels.FIRST_COMMENT):
        print("REFUSING: the comment pass is LinkedIn-only. On %s the link "
              "rides in the post body, so there is nothing left to comment "
              "and no browser to comment with." % args.platform)
        return 2

    # The refusal STAYS. An empty slug now fails closed inside
    # verify_identity too, but this one names the fix, and it stops the run
    # before a browser is ever opened.
    if not expect_identity:
        print("REFUSING: no expected identity. Pass --expect-identity, or "
              "set scheduled_posting.identity_slug for profile %r.\n"
              "The default browser profile is the real account, and a "
              "comment on a live post cannot be undone." % args.profile)
        return 2

    try:
        results = cp.comment_pass(state, profile_name=args.profile,
                                  platform=args.platform,
                                  expect_slug=expect_identity,
                                  wait=args.wait,
                                  allow_production=args.allow_production)
    except pm.ProductionAccessRefused as e:
        print(f"REFUSING: {e}")
        return pm.EXIT_PRODUCTION_REFUSED
    print(cp.summarize([], results))
    return 1 if any(r.get("status") == cp.COMMENT_FAILED for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
