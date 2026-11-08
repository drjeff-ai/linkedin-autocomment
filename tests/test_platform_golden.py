"""Golden corpus for the platform refactor (.dev/PLATFORM_REFACTOR.md §10, P1).

Every row in ``tests/fixtures/platform_golden/rows.json`` (real scheduled-post
rows from three profiles, scrubbed) plus the synthetic rows below is run
through the publishing path under EVERY channel. For each (row, channel) the
test records:

* ``row_key``
* the composed text (``compose_for_platform``)
* the validation result (``validate_row``)
* a full ``schedule_pass``: status, stage, errors, whether the Buffer identity
  check ran, and the exact createPost ``input`` that reached the transport
* the Buffer statuses ``reconcile_published`` asks for

and compares the whole thing, as bytes, against ``expected.json``. That file
was captured from ``main`` BEFORE the refactor touched anything, so a refactor
that changes any of these outputs fails here.

Clock, seed, timezone, upload and Buffer are all pinned. Nothing leaves the
machine: the Buffer session is a fake that records what it is handed.

To recapture (only ever on code whose behaviour is the intended baseline):

    .venv/Scripts/python.exe tests/test_platform_golden.py --write
"""

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import csv_pipeline  # noqa: E402
from linkedin_automation import image_host  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402

FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "platform_golden")
ROWS_FILE = os.path.join(FIXTURE_DIR, "rows.json")
EXPECTED_FILE = os.path.join(FIXTURE_DIR, "expected.json")

#: Every channel the publishing path knows today. Literal on purpose: the
#: corpus must not shrink because a registry did.
CHANNELS = ("linkedin", "x")

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
TIMEZONE = "America/Los_Angeles"
SEED = 4242
CHANNEL_ID = "chan_golden"
HANDLE = "golden-handle"
KEY = "golden-not-a-real-key"
IMAGE = "tests/fixtures/platform_golden/pixel.png"
LINK = "https://example.com/an/article"
LONG_LINK = "https://example.com/" + "q" * 100

SYNTHETIC = [
    ("syn-x-link", {"date": "2026-09-20", "time_window": "morning",
                    "post_text": "Short post that carries a link.",
                    "tags": "#ai", "first_comment_link": LINK}),
    ("syn-x-over-limit", {"date": "2026-09-20", "time_window": "morning",
                          "post_text": "word " * 60, "tags": ""}),
    ("syn-x-long-link-fits", {"date": "2026-09-20", "time_window": "evening",
                              "post_text": "a" * 240,
                              "first_comment_link": LONG_LINK}),
    ("syn-x-link-pushes-over", {"date": "2026-09-20", "time_window": "morning",
                                "post_text": "b" * 260,
                                "first_comment_link": LINK}),
    ("syn-li-link", {"date": "2026-09-21", "time_window": "afternoon",
                     "post_text": "LinkedIn post with a first comment link.",
                     "tags": "#one #two", "image_path": IMAGE,
                     "first_comment_link": LINK}),
    ("syn-li-nolink", {"date": "2026-09-21", "time_window": "morning",
                       "post_text": "LinkedIn post without a link.",
                       "tags": "", "image_path": IMAGE}),
    ("syn-asset-path-alias", {"date": "2026-09-22", "time_window": "morning",
                              "post_text": "Image under asset_path.",
                              "asset_path": IMAGE}),
    ("syn-topic-only", {"date": "2026-09-22", "time_window": "afternoon",
                        "post_text": "", "topic": "a topic to generate from"}),
    ("syn-past-date", {"date": "2026-08-01", "time_window": "morning",
                       "post_text": "Already in the past."}),
    ("syn-bad-window", {"date": "2026-09-22", "time_window": "midnight-ish",
                        "post_text": "Window that does not exist."}),
    ("syn-missing-image", {"date": "2026-09-22", "time_window": "morning",
                           "post_text": "Image that is not there.",
                           "image_path": "tests/fixtures/platform_golden/nope.png"}),
    ("syn-empty", {"date": "2026-09-22", "time_window": "morning",
                   "post_text": "", "topic": ""}),
    ("syn-unknown-column", {"date": "2026-09-22", "time_window": "morning",
                            "post_text": "Has a column nobody reads.",
                            "board": "Some Board"}),
]


def _generate(topic, profile_name=None):
    return "GENERATED from %r" % topic


def _upload(path):
    data, _ctype, ext = image_host.read_image(path)
    return "https://img.golden.invalid/" + image_host.content_key(data, ext)


class _Resp(object):
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Session(object):
    """Records every createPost input; answers success. Never networked."""

    def __init__(self):
        self.inputs = []

    def post(self, url, headers=None, json=None, timeout=None):
        payload = (json or {}).get("variables", {}).get("input")
        self.inputs.append(payload)
        return _Resp({"data": {"createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post-golden-%d" % len(self.inputs),
                     "status": "scheduled",
                     "dueAt": (payload or {}).get("dueAt")}}}})


def _load_rows():
    with open(ROWS_FILE, encoding="utf-8") as f:
        real = [(c["id"], c["row"]) for c in json.load(f)]
    return real + SYNTHETIC


def _compose(row, channel):
    text = (row.get("post_text") or "").strip()
    if not text:
        text = _generate((row.get("topic") or "").strip())
    try:
        return csv_pipeline.compose_for_platform(
            text, row.get("tags"), row.get("first_comment_link"), channel)
    except bc.BufferError as exc:
        return "ERROR: %s" % exc


def _schedule(row, channel, workdir):
    session = _Session()
    fetches = []

    def fetch_channel(channel_id, key=None, session=None):
        fetches.append(channel_id)
        return {"id": channel_id, "name": HANDLE}

    state = csv_pipeline.PipelineState(
        path=os.path.join(workdir, "state_%s.json" % channel))
    try:
        results = csv_pipeline.schedule_pass(
            [dict(row)], CHANNEL_ID, state, rng=random.Random(SEED), key=KEY,
            session=session, upload=_upload, generate=_generate, now=NOW,
            platform=channel, identity_slug=HANDLE,
            fetch_channel=fetch_channel, allow_production=True)
        result = results[0]
        out = {"status": result.get("status"), "stage": result.get("stage"),
               "errors": result.get("errors") or [],
               "note": result.get("note")}
    except Exception as exc:          # a refusal is an output too
        out = {"raised": "%s: %s" % (type(exc).__name__, exc)}
    out["identity_fetches"] = len(fetches)
    out["payloads"] = session.inputs
    return out


def _reconcile_statuses(channel, workdir):
    asked = []

    def fetch(channel_id, statuses=None, limit=None, key=None, session=None):
        asked.append(list(statuses))
        return {}

    state = csv_pipeline.PipelineState(
        path=os.path.join(workdir, "reconcile_%s.json" % channel))
    state.update("k", status=csv_pipeline.SCHEDULED, post_id="p1")
    csv_pipeline.reconcile_published(state, CHANNEL_ID, fetch=fetch,
                                     platform=channel)
    return asked


def compute():
    """The whole corpus's outputs. Caller pins cwd and environment."""
    out = {"rows": {}, "reconcile_statuses": {}}
    with tempfile.TemporaryDirectory() as workdir:
        for row_id, row in _load_rows():
            entry = {"row_key": csv_pipeline.row_key(row), "channels": {}}
            for channel in CHANNELS:
                sub = os.path.join(workdir, row_id)
                os.makedirs(sub, exist_ok=True)
                # validate_row calls due_at with no rng, so the module-level
                # random picks the minute quoted in a past-date message. Seed
                # it, or that message differs on every run.
                random.seed(SEED)
                entry["channels"][channel] = {
                    "compose": _compose(row, channel),
                    "validate": csv_pipeline.validate_row(
                        row, now=NOW, tz=bc.posting_timezone(),
                        platform=channel),
                    "schedule": _schedule(row, channel, sub),
                }
            out["rows"][row_id] = entry
        for channel in CHANNELS:
            out["reconcile_statuses"][channel] = _reconcile_statuses(
                channel, workdir)
    return out


def render(out):
    return json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _pin_environment(setenv, delenv):
    setenv("POST_TIMEZONE", TIMEZONE)
    delenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV)
    delenv(pm.ALLOW_PRODUCTION_ENV)


def test_publishing_outputs_are_byte_identical_to_the_golden_capture(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    _pin_environment(monkeypatch.setenv,
                     lambda name: monkeypatch.delenv(name, raising=False))
    with open(EXPECTED_FILE, "rb") as f:
        expected = f.read().decode("utf-8")
    actual = render(compute())
    if actual != expected:
        exp, act = json.loads(expected), json.loads(actual)
        changed = sorted(k for k in set(exp["rows"]) | set(act["rows"])
                         if exp["rows"].get(k) != act["rows"].get(k))
        if exp["reconcile_statuses"] != act["reconcile_statuses"]:
            changed.append("reconcile_statuses")
        raise AssertionError("golden corpus changed for: %s" % ", ".join(changed))


def test_the_corpus_covers_every_branch_it_exists_to_pin():
    """A golden file is only as good as what it exercises. Assert the capture
    actually contains each case the refactor could break."""
    with open(EXPECTED_FILE, encoding="utf-8") as f:
        exp = json.load(f)
    rows = exp["rows"]
    real = [k for k in rows if not k.startswith("syn-")]
    assert {k.split("-")[0] for k in real} == {"dev", "jeff", "xtest"}
    assert len(real) == 33
    x = {k: v["channels"]["x"] for k, v in rows.items()}
    li = {k: v["channels"]["linkedin"] for k, v in rows.items()}
    # X puts the link in the body; LinkedIn keeps it out.
    assert LINK in x["syn-x-link"]["compose"]
    assert LINK not in li["syn-x-link"]["compose"]
    # X's limit rejects; LinkedIn's does not.
    assert any("too long for X" in p for p in x["syn-x-over-limit"]["validate"])
    assert li["syn-x-over-limit"]["validate"] == []
    assert x["syn-x-long-link-fits"]["validate"] == []
    assert any("too long for X" in p for p in x["syn-x-link-pushes-over"]["validate"])
    # The identity check runs off LinkedIn only, and a payload reaches Buffer.
    assert x["syn-li-link"]["schedule"]["identity_fetches"] == 1
    assert li["syn-li-link"]["schedule"]["identity_fetches"] == 0
    assert li["syn-li-link"]["schedule"]["payloads"][0]["assets"]
    assert exp["reconcile_statuses"] == {"linkedin": [["sent"]],
                                         "x": [["sent", "error"]]}


if __name__ == "__main__":
    if "--write" not in sys.argv:
        sys.exit("pass --write to recapture %s" % EXPECTED_FILE)
    os.chdir(REPO_ROOT)
    # gql logs every call; outside pytest nothing redirects that, so do it here
    # rather than append fake createPost lines to the real usage log.
    bc.USAGE_LOG = os.path.join(tempfile.mkdtemp(), "api_usage.jsonl")
    _pin_environment(lambda k, v: os.environ.__setitem__(k, v),
                     lambda k: os.environ.pop(k, None))
    with open(EXPECTED_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(compute()))
    print("wrote", EXPECTED_FILE)
