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

from linkedin_automation import csv_pipeline as cp  # noqa: E402

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


def main():
    _make_output_unicode_safe()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pass_name", choices=["schedule", "comment", "status"])
    ap.add_argument("--csv", help="the content calendar (schedule pass)")
    ap.add_argument("--channel-id", help="Buffer LinkedIn channel id")
    ap.add_argument("--profile", default=None,
                    help="browser profile for the comment pass")
    ap.add_argument("--expect-identity", default=None, metavar="SLUG",
                    help="abort commenting unless the logged-in profile URL "
                         "contains this. The DEFAULT profile is the real "
                         "account, so set it.")
    ap.add_argument("--wait", action="store_true",
                    help="comment pass: block until a due post publishes, "
                         "instead of leaving it for the next sweep")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    state = cp.PipelineState(profile_name=args.profile)

    if args.pass_name == "status":
        print(cp.summarize(
            [dict(key=k, row={}, **{kk: vv for kk, vv in v.items()
                                    if kk != "row"})
             for k, v in state.rows.items()], []))
        return 0

    if args.pass_name == "schedule":
        if not args.csv or not args.channel_id:
            ap.error("schedule needs --csv and --channel-id")
        rows = cp.read_rows(args.csv)
        print("read %d rows from %s" % (len(rows), args.csv))
        results = cp.schedule_pass(rows, args.channel_id, state,
                                   profile_name=args.profile)
        print(cp.summarize(results, []))
        failed = [r for r in results if r.get("status") == cp.FAILED]
        return 1 if failed else 0

    if not args.expect_identity:
        print("REFUSING: --expect-identity is required for the comment pass.\n"
              "The default browser profile is the real account, and a comment "
              "on a live post cannot be undone.")
        return 2
    results = cp.comment_pass(state, profile_name=args.profile,
                              expect_slug=args.expect_identity,
                              wait=args.wait)
    print(cp.summarize([], results))
    return 1 if any(r.get("status") == cp.COMMENT_FAILED for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
