"""Mint and store the YouTube OAuth token for one profile (V1, DEC-058).

    .venv\\Scripts\\python.exe tools\\youtube_auth.py --profile topfive \\
        --client-secrets <path to the Google OAuth client secrets .json>

Runs Google's installed-app consent in your browser with the
``youtube.upload`` and ``youtube`` scopes. Sign in as the account that owns
the channel. The token must answer for the channel id in the profile's
``scheduled_posting.platforms.youtube.channel_id``; any other channel is
refused and nothing is stored.

The token (client id, client secret and refresh token) is written to
``data/<profile>/youtube/oauth_token.json``, which is gitignored with the rest
of ``data/``. Nothing secret is ever printed. Uploading is a separate command
(``run_scheduled_posts.py youtube-schedule``); this one uploads nothing.

Exit codes: 0 stored, 2 refused (config, wrong channel, no refresh token),
1 a Google call failed.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from linkedin_automation import youtube_api  # noqa: E402
from linkedin_automation import youtube_channel as yt  # noqa: E402


def run(profile, client_secrets, api=youtube_api, out=print, now=None):
    """Consent, verify the channel, store the token. Returns an exit code."""
    now = now or datetime.now(timezone.utc)
    try:
        cfg = yt.youtube_config(profile)
    except yt.YouTubeRefused as exc:
        out("REFUSED: %s" % exc)
        return yt.EXIT_REFUSED
    if not os.path.isfile(client_secrets):
        out("REFUSED: no client secrets file at %s" % client_secrets)
        return yt.EXIT_REFUSED
    out("Opening the Google consent page. Sign in as the owner of channel %s."
        % cfg.channel_id)
    try:
        yt.log_paid_call("oauth2 installed-app consent (%s)" % profile, now=now)
        creds = api.run_consent(client_secrets)
        if not getattr(creds, "refresh_token", None):
            out("REFUSED: Google returned no refresh token; nothing was stored. "
                "Remove the app's access at myaccount.google.com/permissions "
                "and run this again.")
            return yt.EXIT_REFUSED
        yt.log_paid_call("channels.list mine (auth channel check, 1 unit)", now=now)
        found = api.my_channel_id(api.build_service(creds))
    except youtube_api.YouTubeApiError as exc:
        out("FAILED: %s; nothing was stored" % exc)
        return yt.EXIT_ERROR
    if found != cfg.channel_id:
        out("REFUSED: this Google account's channel is %s, not %s. Nothing was "
            "stored. Run again and pick the account that owns %s."
            % (found, cfg.channel_id, cfg.channel_id))
        return yt.EXIT_REFUSED
    yt.save_token(profile, {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri": getattr(creds, "token_uri", None) or youtube_api.TOKEN_URI,
        "scopes": list(youtube_api.SCOPES),
        "channel_id": found,
        "created_at": yt.utc_z(now),
    })
    out("Stored the token for channel %s at %s" % (found, yt.token_path(profile)))
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--client-secrets", required=True,
                    help="the Google OAuth client secrets .json")
    args = ap.parse_args()
    return run(args.profile, args.client_secrets)


if __name__ == "__main__":
    raise SystemExit(main())
