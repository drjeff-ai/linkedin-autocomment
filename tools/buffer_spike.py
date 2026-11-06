"""THROWAWAY SPIKE - can Buffer's API replace the browser-based scheduled poster?

Branch `buffer-spike`. Not production code and not wired into anything.

It imported nothing from linkedin_automation until --image-file, which
deliberately calls the REAL `image_host` layer rather than a spike copy: the
thing being tested is the actual local -> R2 -> Buffer -> X path, and a
second uploader would prove that second uploader works and nothing else.
image_host is USED, never modified. If the answer is no, this file is
deleted; if it is yes, the browser post/image/schedule build is dropped and this
gets rewritten properly.

The question is narrow: does ONE Buffer API call schedule a LinkedIn post with

    text (+ a hashtag)   an image   a dueAt   AND a first comment

on the dev channel, and does it actually publish that way? The first comment is
the whole reason the feature exists, so a spike that proves everything except
that has proved nothing.

VERIFY, DO NOT TRUST THE DOCS. Step 2 introspects the live schema and reports
what the API actually accepts - the field names, whether
LinkedInPostMetadataInput really has firstComment, and what shape `assets`
takes - before any post is created. Docs describing a field is not evidence the
field works.

Read-only by default. Nothing is created unless --create is passed, because
createPost against a connected channel schedules a real post on a real account.

    uv run python tools/buffer_spike.py                     # inspect only
    uv run python tools/buffer_spike.py --create --image-url https://...

The Buffer key comes from the environment (.env) — by default BUFFER_API_KEY,
or whichever variable --key-env names, so a second Buffer account is a second
variable rather than a second checkout. Only BUFFER_API_KEY or
BUFFER_API_KEY_<SUFFIX> is accepted, because the value goes to Buffer as the
Bearer token. It is never printed, never logged, and
never committed. buffer_client.api_key() is NOT touched: that is shared
production code on the LinkedIn scheduling path.
"""

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://api.buffer.com/graphql"

#: Which env var the key came from, for error messages only — never the key
#: itself. Set by main() from --key-env so a 401 names the variable the operator
#: actually passed rather than the default they may not be using.
KEY_ENV_VAR = "BUFFER_API_KEY"

#: The --key-env value is sent to Buffer as the Bearer token, so only a name
#: that says it holds a Buffer key is read: the same rule as
#: profile_manager.scheduled_key_env. Copied rather than imported because this
#: spike runs as a plain script, where linkedin_automation is not importable
#: unless --image-file is used. tests/test_buffer_spike_key_env.py checks that
#: the two agree.
KEY_ENV_RE = re.compile(r"BUFFER_API_KEY(?:_[A-Z0-9_]+)?")
KEY_ENV_MAX_LEN = 64
KEY_ENV_RULE = "BUFFER_API_KEY or BUFFER_API_KEY_<SUFFIX>"


def key_env_allowed(name):
    return (isinstance(name, str) and len(name) <= KEY_ENV_MAX_LEN
            and KEY_ENV_RE.fullmatch(name) is not None)


USAGE_LOG = "api_usage.jsonl"
TRANSCRIPT = ".harvest/buffer_spike_transcript.jsonl"


# ─── transport ───────────────────────────────────────────────────────────────

class BufferError(RuntimeError):
    pass


def _redact(headers):
    out = dict(headers)
    if "Authorization" in out:
        out["Authorization"] = "Bearer [REDACTED]"
    return out


def gql(key, query, variables=None, label=""):
    """One GraphQL round trip, with the full request and response recorded.

    Every call is written to a transcript so the exact wire traffic can be read
    afterwards - the point of a spike is the evidence, not the summary.
    """
    headers = {"Authorization": "Bearer %s" % key,
               "Content-Type": "application/json"}
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    started = time.time()
    resp = requests.post(API_URL, headers=headers, json=payload, timeout=60)
    elapsed = round(time.time() - started, 3)

    try:
        body = resp.json()
    except ValueError:
        body = {"_raw": resp.text[:2000]}

    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "request": {"url": API_URL, "headers": _redact(headers),
                    "body": payload},
        "response": {"status": resp.status_code, "body": body},
        "elapsed_s": elapsed,
    }
    os.makedirs(os.path.dirname(TRANSCRIPT), exist_ok=True)
    with open(TRANSCRIPT, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # CLAUDE.md: log every API call. Buffer's free plan is not billed per call,
    # but it is capped at 3,000 requests / 30 days, so the budget is real.
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api": "buffer-graphql", "endpoint": label or "graphql",
            "estimated_cost": 0.0, "quota_unit": "1 of 3000 per 30 days",
        }) + "\n")

    print("  [%s] HTTP %s in %ss" % (label or "gql", resp.status_code, elapsed))
    if resp.status_code == 401:
        raise BufferError("401 - the API key was rejected. Check %s."
                          % KEY_ENV_VAR)
    if body.get("errors"):
        # Surfaced, not swallowed: a GraphQL 200 with an errors array is a
        # failure that looks like a success to anything checking status codes.
        print("  GraphQL errors: %s" % json.dumps(body["errors"])[:400])
    return body


# ─── step 1: who am I, and which channel ─────────────────────────────────────

# channelCount is asked for alongside the org because it settles, in one field,
# the question an empty channels list cannot: whether Buffer has any channel
# connected at all. Without it, [] is ambiguous between "nothing connected",
# "the key lacks scope" and "the query is wrong".
Q_ORGS = """
query GetOrganizations {
  account { organizations { id name channelCount } }
}
"""

# EVERY id in this schema is a custom scalar, not String. Declaring $orgId as
# String! got the whole query rejected before it ran:
#
#   Variable "$orgId" of type "String!" used in position expecting type
#   "OrganizationId!"
#
# Confirmed by introspection: OrganizationId, ChannelId, PostId, DraftId,
# IdeaId, TagId, AccountId are all distinct scalars. Guessing String for any of
# them fails the same way, so all of them are spelled out here.
Q_CHANNELS = """
query GetChannels($orgId: OrganizationId!) {
  channels(input: { organizationId: $orgId }) {
    id
    name
    service
  }
}
"""


#: What Buffer calls each network, as a `service` value on Channel.
#:
#: X is listed under its OLD name too. Buffer's schema still says "twitter"
#: (the metadata input is TwitterPostMetadataInput, not XPostMetadataInput), so
#: a spike matching on "x" alone would report "no X channel" against an account
#: that has one. Both spellings are accepted, and the real service value is
#: printed, so the answer comes off the wire rather than out of this table.
SERVICE_ALIASES = {
    "linkedin": ("linkedin",),
    "x": ("x", "twitter"),
    "twitter": ("x", "twitter"),
}


def discover(key, service="linkedin"):
    print("\n=== STEP 1 - organization and channel ===")
    orgs = gql(key, Q_ORGS, label="GetOrganizations")
    data = (orgs.get("data") or {}).get("account") or {}
    if orgs.get("errors"):
        raise BufferError("GetOrganizations was REJECTED: %s"
                          % json.dumps(orgs["errors"])[:400])
    org_list = data.get("organizations") or []
    if not org_list:
        raise BufferError("the query succeeded and returned zero organizations")
    for o in org_list:
        print("  org: %s  (%s)  channelCount=%s"
              % (o.get("name"), o.get("id"), o.get("channelCount")))
    org_id = org_list[0]["id"]
    channel_count = org_list[0].get("channelCount")

    chans = gql(key, Q_CHANNELS, {"orgId": org_id}, label="GetChannels")
    # A rejected query and an empty account are completely different answers,
    # and the first version reported both as "no channels - connect the dev
    # LinkedIn", which sent the reader looking at Buffer's UI for a bug that was
    # in this file. Report what actually happened.
    if chans.get("errors"):
        raise BufferError(
            "GetChannels was REJECTED by the API - the query never ran, so "
            "nothing is known about whether channels exist. Errors: %s"
            % json.dumps(chans["errors"])[:400])
    channels = (chans.get("data") or {}).get("channels")
    if channels is None:
        raise BufferError("GetChannels returned no data block at all: %s"
                          % json.dumps(chans)[:300])
    if not channels:
        if channel_count == 0:
            raise BufferError(
                "the query SUCCEEDED and the organization reports "
                "channelCount=0, so no channel is connected to Buffer at all. "
                "This is not an API or permissions problem: connect the dev "
                "LinkedIn as a Buffer channel, then re-run.")
        raise BufferError(
            "the query succeeded and returned zero channels, but the "
            "organization reports channelCount=%s. An empty list alongside a "
            "non-zero count points at the key's scope, not at Buffer's UI."
            % channel_count)
    print("\n  channels on org %s:" % org_id)
    for c in channels:
        print("    %-38s service=%-12s id=%s"
              % (c.get("name"), c.get("service"), c.get("id")))

    wanted = SERVICE_ALIASES.get((service or "").lower(),
                                 ((service or "").lower(),))
    matched = [c for c in channels
               if str(c.get("service", "")).lower().startswith(wanted)]
    if not matched:
        raise BufferError(
            "no %s channel found among %d channels (services present: %s). "
            "Connect one in Buffer, or pass --channel-id to target a channel "
            "this matcher does not recognise."
            % (service, len(channels),
               ", ".join(sorted({str(c.get("service")) for c in channels}))))
    if len(matched) > 1:
        print("\n  ! more than one %s channel. Pass --channel-id to pick the "
              "right one explicitly rather than letting this guess." % service)
    print("\n  %s channel: %s -> %s  (service=%r)"
          % (service, matched[0].get("name"), matched[0]["id"],
             matched[0].get("service")))
    return org_id, matched[0]["id"], matched


# ─── step 2: introspect, because docs are not evidence ───────────────────────

Q_INPUT_TYPE = """
query IntrospectInput($name: String!) {
  __type(name: $name) {
    name
    kind
    inputFields {
      name
      type { name kind ofType { name kind ofType { name kind } } }
    }
    fields {
      name
      type { name kind ofType { name kind ofType { name kind } } }
    }
  }
}
"""


def _type_name(t):
    while t:
        if t.get("name"):
            return t["name"]
        t = t.get("ofType")
    return "?"


def introspect(key, names):
    print("\n=== STEP 2 - what the API ACTUALLY accepts (introspection) ===")
    found = {}
    for name in names:
        body = gql(key, Q_INPUT_TYPE, {"name": name}, label="introspect:%s" % name)
        t = (body.get("data") or {}).get("__type")
        if not t:
            print("  %-32s NOT PRESENT in the schema" % name)
            found[name] = None
            continue
        # An output type carries `fields`, an input object `inputFields`.
        # Reporting only the latter makes an output type look EMPTY, which is a
        # different claim from "not in the schema" and was hiding the shape of
        # the read-back side.
        kind = t.get("kind")
        raw_fields = (t.get("inputFields") or []) + (t.get("fields") or [])
        fields = {f["name"]: _type_name(f["type"]) for f in raw_fields}
        found[name] = fields
        print("  %s [%s] (%d fields):" % (name, kind, len(fields)))
        for fname, ftype in sorted(fields.items()):
            print("      %-22s %s" % (fname, ftype))
    return found


def verify_claims(found):
    """The three things the whole decision rests on."""
    print("\n  --- claims checked against the live schema ---")
    verdicts = {}

    li = found.get("LinkedInPostMetadataInput")
    ok = bool(li and "firstComment" in li)
    verdicts["firstComment"] = ok
    print("  firstComment on LinkedInPostMetadataInput : %s"
          % ("YES (%s)" % li["firstComment"] if ok else "NO - the feature's whole point"))

    cp = found.get("CreatePostInput") or {}
    for field in ("dueAt", "assets", "metadata", "channelId", "text"):
        present = field in cp
        verdicts[field] = present
        print("  CreatePostInput.%-10s : %s"
              % (field, "yes (%s)" % cp[field] if present else "MISSING"))

    img = found.get("PostAssetInput") or found.get("AssetInput") or {}
    if img:
        print("  asset input fields: %s" % ", ".join(sorted(img)))
    return verdicts


# ─── step 3: the actual post ─────────────────────────────────────────────────

M_CREATE = """
mutation CreateSpikePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post { id text dueAt status assets { id mimeType } }
    }
    ... on MutationError { message }
  }
}
"""


def build_input(channel_id, text, image_url, due_at, first_comment,
                service="linkedin", thread_text=None):
    """The one call that has to carry all four things at once.

    Every field here was checked against the live schema, because four of them
    were wrong on the first attempt and each would have failed createPost the
    same way the String!/OrganizationId! mismatch failed GetChannels:

      assets          [AssetInput!]!  - REQUIRED and non-null. Omitting it for a
                                        text-only post is a schema error, not a
                                        default. Send [] instead.
      mode            ShareMode!      - REQUIRED. customScheduled is the one
                                        that honours dueAt.
      needsApproval   Boolean!        - REQUIRED, no default.
      schedulingType  SchedulingType! - enum is {automatic, notification}. There
                                        is no "custom" member; scheduling is
                                        chosen by mode, not by this.
      metadata.linkedin               - LOWERCASE. PostInputMetaData spells it
                                        `linkedin`; `linkedIn` is not a field.
    """
    payload = {
        "channelId": channel_id,
        "text": text,
        # Required and non-null. A text-only post sends an empty list.
        "assets": [],
        "mode": "customScheduled" if due_at else "addToQueue",
        "schedulingType": "automatic",
        "needsApproval": False,
    }
    if due_at:
        payload["dueAt"] = due_at
    if image_url:
        # Buffer takes a PUBLICLY ACCESSIBLE URL here, not a file upload and not
        # base64. ImageAssetInput.url is String! and is the only required field.
        payload["assets"] = [{"image": {"url": image_url}}]
    # Metadata is per-NETWORK and the key is the network's name in
    # PostInputMetaData. firstComment exists ONLY on linkedin: introspection
    # shows TwitterPostMetadataInput has no such field (its self-reply seam is
    # `thread`, a whole post rather than a string). Sending {"linkedin": ...}
    # on an X post would be a schema-valid lie — the block would be accepted and
    # silently ignored, or rejected — so it is gated on the service.
    if first_comment:
        if (service or "").lower() == "linkedin":
            payload["metadata"] = {"linkedin": {"firstComment": first_comment}}
        else:
            print("  (first comment ignored: %s has no firstComment field; "
                  "X threads via metadata.twitter.thread — see "
                  ".dev/FINDING_x_buffer_thread.md)" % service)

    if thread_text:
        if (service or "").lower() == "linkedin":
            print("  (--thread-text ignored: LinkedIn has no thread field; its "
                  "self-reply is firstComment)")
        else:
            # `thread` is [ThreadedPostInput!] — a LIST, read off the recorded
            # introspection rather than guessed, so one reply is a list of one.
            #
            # `assets` inside it is [..]! — REQUIRED and non-null, exactly like
            # CreatePostInput.assets. Omitting it for a text-only reply is a
            # schema error, not a default, which is the same trap that failed
            # the first createPost attempt. Send [].
            payload.setdefault("metadata", {})["twitter"] = {
                "thread": [{"text": thread_text, "assets": []}]
            }
    return payload


def host_local_image(path):
    """Upload a local file via the project's image_host and return its URL.

    THE POINT IS THAT THIS IS THE REAL LAYER. `image_host.upload_image` is what
    the scheduled-posting pipeline already calls, so a spike that re-implemented
    the upload would be testing the re-implementation. It is imported here and
    not modified.

    Two properties it brings that matter to the failure this flag exists for:

    * the key is the CONTENT HASH, so re-running with the same picture
      overwrites itself instead of accumulating copies;
    * `verify=True` re-fetches the URL ANONYMOUSLY and fails if the bucket is
      not actually public.

    That second one is precisely the class of problem that killed the picsum
    attempt. X's publish-side fetch returned HTTP 405 "Media URL not publicly
    accessible" because picsum REDIRECTS, and X would not follow it. An R2
    public URL is a direct 200 with a real image content-type, which is what
    the verify step proves before anything is scheduled.
    """
    # Imported lazily so the rest of the spike keeps working with no R2 config
    # and no boto3 installed - only --image-file needs them.
    from linkedin_automation import image_host

    missing = [v for v in image_host.REQUIRED_VARS
               if not (os.environ.get(v) or "").strip()]
    if missing:
        raise BufferError(
            "--image-file needs R2 configured; missing from .env: %s"
            % ", ".join(missing))

    url = image_host.upload_image(path)      # verify=True by default
    return url


def create_post(key, payload):
    print("\n=== STEP 3 - createPost ===")
    print("  input: %s" % json.dumps(payload, indent=2)[:900])
    body = gql(key, M_CREATE, {"input": payload}, label="createPost")
    result = ((body.get("data") or {}).get("createPost")) or {}
    kind = result.get("__typename")
    if kind == "PostActionSuccess":
        post = result.get("post") or {}
        print("\n  SUCCESS")
        print("    id     : %s" % post.get("id"))
        print("    dueAt  : %s" % post.get("dueAt"))
        print("    status : %s" % post.get("status"))
        print("    assets : %s" % json.dumps(post.get("assets")))
        return post
    print("\n  FAILED (%s): %s" % (kind, result.get("message")))
    if body.get("errors"):
        print("  errors: %s" % json.dumps(body["errors"])[:600])
    blob = "%s %s" % (result.get("message") or "", json.dumps(body.get("errors") or ""))
    if "paid plan" in blob.lower() or "upgrade" in blob.lower():
        print("\n  This is the FREE-PLAN PAYWALL, and NOTHING was created - the")
        print("  whole post was rejected, not just the field. firstComment is the")
        print("  paid part, and it is the half the browser tool owns anyway.")
        print("  Re-run with --no-first-comment to publish the post itself.")
    return None


# Everything a Post exposes that could answer the coordination question.
#
# THE HYBRID HINGES ON `externalLink`. Post has no serviceUpdateId, no
# servicePostId and no permalink field - introspection lists id, status, dueAt,
# sentAt, text, assets, channel, metadata, error, externalLink and nothing else
# that could carry a URL. So externalLink is the only candidate, and its name is
# not proof: on Channel the same field name holds the PROFILE url, and on a Post
# it could just as easily mean "the link attached to this post". Only reading it
# back after a real publish settles which.
Q_READBACK = """
query ReadBack($id: PostId!) {
  post(input: { id: $id }) {
    id
    status
    dueAt
    sentAt
    createdAt
    text
    externalLink
    isCustomScheduled
    shareMode
    schedulingType
    channelService
    assets { id mimeType source thumbnail type }
    error { message rawError }
    metadata {
      __typename
      ... on LinkedInPostMetadata { type firstComment }
      ... on TwitterPostMetadata { type threadCount isAiGenerated }
    }
  }
}
"""


def read_back(key, post_id, label="readback"):
    """Persistence is not the same as acceptance.

    A mutation returning success only proves the request was accepted. Reading
    the post back is what shows whether the image and the schedule survived.
    """
    body = gql(key, Q_READBACK, {"id": post_id}, label=label)
    post = (body.get("data") or {}).get("post")
    if body.get("errors"):
        print("  read-back REJECTED: %s" % json.dumps(body["errors"])[:300])
        return None
    return post


def watch_until_sent(key, post_id, minutes=15, every=30):
    """Poll until Buffer says the post published, then report what it exposes.

    This is the actual experiment. `status` moves scheduled -> sending -> sent,
    and the question is whether `externalLink` is populated once it does - that
    single field decides whether the hybrid is clean or fiddly.
    """
    print("\n=== STEP 3c - watching until the post publishes ===")
    print("  polling every %ss for up to %s minutes" % (every, minutes))
    deadline = time.time() + minutes * 60
    seen = None
    while time.time() < deadline:
        post = read_back(key, post_id, label="watch")
        if post is None:
            print("  (read-back failed; stopping the watch)")
            return None
        status = post.get("status")
        if status != seen:
            print("  status=%-10s sentAt=%-26s externalLink=%r"
                  % (status, post.get("sentAt"), post.get("externalLink")))
            seen = status
        if status in ("sent", "error"):
            print("\n  FINAL POST OBJECT:")
            print(json.dumps(post, indent=2)[:1800])
            return post
        time.sleep(every)
    print("  still %r at the deadline - not published yet" % seen)
    return None


# ─── step 4: one CSV row -> one Buffer call ──────────────────────────────────

SAMPLE_CSV = """date,time_window,post_text,topic,tags,image_path,first_comment_link
2026-09-08,09:00-11:00,"Testing an automated scheduled post.",automation,"#automation #python",S:/new_comp/test_image.png,https://example.com/the-link
"""


def csv_row_to_input(row, channel_id, image_url_override=None):
    """Map ONE CSV row onto a createPost input, and be explicit about the gap.

    `image_path` is a LOCAL path, and Buffer needs a public URL - so this
    mapping cannot be completed from the CSV alone. That is the finding, not a
    bug in the mapping: either the column becomes `image_url`, or something has
    to host the file first and substitute the URL here.
    """
    text = row["post_text"].strip()
    tags = (row.get("tags") or "").strip()
    if tags:
        text = "%s\n\n%s" % (text, tags)

    start = (row.get("time_window") or "09:00").split("-")[0].strip()
    naive = datetime.strptime("%s %s" % (row["date"].strip(), start),
                              "%Y-%m-%d %H:%M")
    due_at = naive.replace(tzinfo=timezone.utc).isoformat().replace(
        "+00:00", ".000Z")

    image_path = (row.get("image_path") or "").strip()
    image_url = image_url_override
    notes = []
    if image_path and not image_url:
        notes.append("image_path=%r is a LOCAL PATH; Buffer needs a public URL. "
                     "No URL supplied, so this row would post WITHOUT its image."
                     % image_path)

    link = (row.get("first_comment_link") or "").strip()
    payload = build_input(channel_id, text, image_url, due_at, link or None)
    return payload, notes


def step_csv(channel_id, image_url):
    print("\n=== STEP 4 - CSV -> createPost input (one row) ===")
    path = os.path.join(".harvest", "buffer_spike_sample.csv")
    os.makedirs(".harvest", exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(SAMPLE_CSV)
        print("  wrote sample CSV: %s" % path)

    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("  the CSV has no rows")
        return None
    payload, notes = csv_row_to_input(rows[0], channel_id, image_url)
    print("  row 1 -> %s" % json.dumps(payload, indent=2)[:900])
    for n in notes:
        print("  ! %s" % n)
    return payload


# ─── main ────────────────────────────────────────────────────────────────────

def check_image_url(url):
    """Fetch a URL the way Buffer's server would and report what comes back.

    Buffer does not re-serve the bytes it fetched - its API echoes the URL we
    supplied and LinkedIn's opaque image urn, and nothing else - so if the wrong
    picture appears on LinkedIn, the only place to look is what the URL serves
    to an anonymous server-side fetcher. Hotlink protection keys on exactly
    that: no Referer, a non-browser User-Agent.
    """
    print("\n=== image URL check ===")
    print("  %s" % url)
    try:
        r = requests.get(url, timeout=30, allow_redirects=True,
                         headers={"User-Agent": "python-requests/2.x"})
    except Exception as exc:
        print("  FETCH FAILED: %r" % exc)
        return False
    body = r.content
    sig = body[:12]
    kind = ("JPEG" if sig[:3] == b"\xff\xd8\xff" else
            "PNG" if sig[:8] == b"\x89PNG\r\n\x1a\n" else
            "GIF" if sig[:6] in (b"GIF87a", b"GIF89a") else
            "WEBP" if sig[:4] == b"RIFF" else "NOT-AN-IMAGE")
    print("  status       : %s" % r.status_code)
    print("  final url    : %s" % r.url[:140])
    print("  redirects    : %d" % len(r.history))
    print("  content-type : %s" % r.headers.get("Content-Type"))
    print("  bytes        : %d" % len(body))
    print("  magic bytes  : %s -> %s" % (sig.hex()[:24], kind))
    ok = r.status_code == 200 and kind != "NOT-AN-IMAGE"
    if not ok:
        print("  ! this URL does NOT serve an image to an anonymous fetcher.")
    elif len(body) < 5000:
        print("  ! only %d bytes - placeholder and 'image unavailable' graphics "
              "are typically this small. Compare against the real file." % len(body))
        ok = False
    else:
        print("  looks like a genuine image to a server-side fetcher.")
    return ok


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--create", action="store_true",
                    help="actually schedule a post. WITHOUT THIS NOTHING IS "
                         "CREATED - createPost schedules on a real account.")
    ap.add_argument("--key-env", default="BUFFER_API_KEY", metavar="VAR",
                    help="environment variable holding the Buffer key "
                         "(default BUFFER_API_KEY). Must be BUFFER_API_KEY or "
                         "BUFFER_API_KEY_<SUFFIX>. One Buffer account per "
                         "variable, so a second account is a second variable "
                         "in .env rather than a second checkout.")
    ap.add_argument("--service", default="linkedin",
                    help="which network's channel to target: linkedin (default) "
                         "or x. Buffer still calls X 'twitter' internally and "
                         "both spellings are accepted.")
    ap.add_argument("--channel-id", default=None,
                    help="target this channel id instead of auto-picking by "
                         "--service. Wins over --service.")
    ap.add_argument("--image-url", default=None,
                    help="PUBLIC image URL, used as given. Buffer does not take "
                         "a local file. NOTE: X's publish-side fetch rejects "
                         "some hosts outright — picsum.photos returned HTTP 405 "
                         "('Media URL not publicly accessible') because it "
                         "redirects. Use --image-file to route through R2.")
    ap.add_argument("--image-file", default=None, metavar="PATH",
                    help="a LOCAL image. Uploads it through the project's "
                         "image_host (R2) and attaches the returned public URL "
                         "— the same layer the real pipeline uses, so this "
                         "exercises local -> R2 -> Buffer -> X end to end. "
                         "Requires --create; needs the R2_* vars in .env.")
    ap.add_argument("--minutes", type=int, default=12,
                    help="schedule this many minutes from now (default 12)")
    ap.add_argument("--text", default="Buffer API spike - scheduled post test.")
    ap.add_argument("--first-comment",
                    default="Spike first comment: https://example.com/the-link")
    ap.add_argument("--thread-text", default=None, metavar="TEXT",
                    help="X ONLY. With --create, attach this as a threaded "
                         "self-reply via metadata.twitter.thread. Omit it and "
                         "--create sends NO metadata at all (the base post). "
                         "Running both, in that order, is what settles whether "
                         "the reply is free or paid — see "
                         ".dev/FINDING_x_buffer_thread.md")
    ap.add_argument("--no-first-comment", action="store_true",
                    help="omit metadata.firstComment entirely - the FREE-PLAN "
                         "shape, since Buffer paywalls that field. This is the "
                         "half of the hybrid Buffer is meant to handle.")
    ap.add_argument("--watch", type=int, default=0, metavar="MINUTES",
                    help="after creating, poll until the post publishes and "
                         "report whether externalLink carries the LinkedIn URL")
    ap.add_argument("--check-image-url", default=None, metavar="URL",
                    help="fetch a URL the way Buffer's server would and report "
                         "what it serves, then exit. Use this when the wrong "
                         "image appears on a post.")
    ap.add_argument("--watch-post", default=None, metavar="POST_ID",
                    help="skip creating and just watch an existing post id")
    args = ap.parse_args()

    # Needs no API key: it is a plain HTTP fetch of a public URL.
    if args.check_image_url:
        return 0 if check_image_url(args.check_image_url) else 1

    # Resolved HERE, in the spike, from whichever variable --key-env names.
    # buffer_client.api_key() is deliberately not touched: it is shared
    # production code on the LinkedIn scheduling path, and a spike proving out
    # multi-account must not change how the working account resolves its key.
    # Refuse the ambiguous case rather than picking one. This spike already
    # carries --check-image-url because "the wrong image appeared on a post"
    # actually happened, and a silent precedence rule between two image flags is
    # how that recurs.
    if args.image_url and args.image_file:
        print("--image-url and --image-file are mutually exclusive: one is a "
              "URL used as given, the other is a local file to upload. Pass "
              "exactly one.")
        return 2

    # Checked before the variable is read. The name is not echoed: a key
    # pasted into the flag by mistake would otherwise be printed.
    if not key_env_allowed(args.key_env):
        print("--key-env is not an allowed name. Its value is sent to Buffer as "
              "the API key, so only %s can be used. Nothing was sent."
              % KEY_ENV_RULE)
        return 2

    global KEY_ENV_VAR
    KEY_ENV_VAR = args.key_env
    key = (os.getenv(args.key_env) or "").strip()
    if not key:
        print("%s is not set (or is empty). Add it to .env, never commit it:"
              % args.key_env)
        print("    %s=..." % args.key_env)
        print("\nThe variable is named by --key-env; the default is "
              "BUFFER_API_KEY.")
        return 2
    print("Buffer spike - key loaded from %s (%d chars, never printed)"
          % (args.key_env, len(key)))
    print("transcript: %s" % TRANSCRIPT)

    if args.watch_post:
        watch_until_sent(key, args.watch_post, minutes=args.watch or 15)
        return 0

    try:
        org_id, channel_id, li_channels = discover(key, args.service)
    except BufferError as exc:
        print("\nSTOPPED: %s" % exc)
        return 1
    if args.channel_id:
        channel_id = args.channel_id
        print("  using --channel-id %s" % channel_id)

    found = introspect(key, [
        "CreatePostInput",
        "PostInputMetaData",
        "LinkedInPostMetadataInput",
        # The Phase 1 question. Buffer's schema still uses the old name, so both
        # spellings are asked for and "NOT PRESENT" becomes an answer rather
        # than a suspected typo. The output type is introspected too: a
        # self-reply that can be SET but never READ BACK is not a usable seam.
        "TwitterPostMetadataInput",
        "XPostMetadataInput",
        "TwitterPostMetadata",
        # X's self-reply seam, whatever it turns out to be.
        "ThreadedPostInput",
        "RetweetMetadataInput",
        "PostAssetInput",
        "ImageAssetInput",
    ])
    verdicts = verify_claims(found)

    due_at = (datetime.now(timezone.utc)
              + timedelta(minutes=args.minutes)).isoformat(
                  timespec="milliseconds").replace("+00:00", "Z")

    text = "%s #automation" % args.text
    first_comment = None if args.no_first_comment else args.first_comment
    image_url = args.image_url
    if args.image_file:
        # Uploading is a WRITE (to R2), so it lives behind --create with
        # everything else that writes. A read-only run says what it would do
        # and does not touch the bucket.
        if not args.create:
            print("\n  --image-file %s would be uploaded to R2 on --create; "
                  "read-only run, nothing uploaded." % args.image_file)
        else:
            print("\n=== STEP 2b - hosting the local image through image_host ===")
            try:
                image_url = host_local_image(args.image_file)
            except Exception as exc:
                print("  UPLOAD FAILED: %s" % exc)
                print("  Nothing was scheduled - a post with a dead image URL is")
                print("  worse than no post, and X rejects the whole publish.")
                return 1
            print("  public URL: %s" % image_url)

    payload = build_input(channel_id, text, image_url, due_at,
                          first_comment, service=args.service,
                          thread_text=args.thread_text)
    if args.no_first_comment:
        print("\n  --no-first-comment: metadata is omitted entirely, which is "
              "the free-plan shape.")

    if not args.create:
        print("\n=== STEP 3 - SKIPPED (no --create) ===")
        print("  would send: %s" % json.dumps(payload, indent=2)[:900])
        if not image_url and not args.image_file:
            print("  ! no --image-url or --image-file, so the image half would "
                  "be untested")
    else:
        if not image_url:
            print("\n  ! --create with no image: the post would carry none, "
                  "which leaves the main open question unanswered. Pass "
                  "--image-file (local, via R2) or --image-url (public URL).")
        post = create_post(key, payload)
        if post and post.get("id"):
            back = read_back(key, post["id"])
            if back:
                print("\n=== STEP 3b - read back immediately ===")
                print(json.dumps(back, indent=2)[:1200])
            if args.watch:
                watch_until_sent(key, post["id"], minutes=args.watch)

    step_csv(channel_id, args.image_url)

    print("\n=== SPIKE SUMMARY ===")
    print("  organization      : %s" % org_id)
    print("  LinkedIn channel  : %s" % channel_id)
    print("  firstComment field: %s" % ("present" if verdicts.get("firstComment")
                                        else "ABSENT"))
    print("  image mechanism   : hosted URL (assets[].image.url) - NOT a file upload")
    print("  scheduled for     : %s" % due_at)
    print("\n  Human gate: watch the dev LinkedIn at that time. The post must "
          "appear WITH the image AND a first comment carrying the link.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
