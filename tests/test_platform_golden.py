"""Golden corpus for the platform refactor (.dev/PLATFORM_REFACTOR.md Â§10, P1).

Every row in ``tests/fixtures/platform_golden/rows.json`` (real scheduled-post
rows from three profiles, scrubbed) plus the synthetic rows below is run
through the publishing path under EVERY channel. For each (row, channel) the
test records:

* ``row_key``
* the composed text (``compose_for_platform``)
* the validation result (``validate_row``)
* a full ``schedule_pass``: status, stage, errors, whether the Buffer identity
  check ran, and the exact createPost ``input`` that reached the transport
and, once per probe name, the paths that read config or have no row: the
state-file path, reconcile's Buffer statuses, the declared-production gate,
``verify_identity`` with nothing to check against, ``resolve_scheduled``, and
the dashboard's settings view and account header.

"Every channel" is linkedin and x plus the names a refactor could quietly
start treating differently (None, "", case and whitespace variants, and an
undeclared "pinterest"). The rendered JSON is compared exactly against
``expected.json`` (line endings aside). That file was captured from
``main``'s code BEFORE the refactor touched anything, so a refactor that
changes any of these outputs fails here.

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

#: Every channel the publishing path knows today, plus the names a refactor
#: could quietly start treating differently: no name, an empty one, case and
#: whitespace variants, and a channel nothing declares. Literal on purpose:
#: the corpus must not shrink because a registry did.
CHANNELS = ("linkedin", "x", None, "", "LinkedIn", "X", " x", "linkedin ",
            "pinterest")

#: The profile the config-reading probes resolve. It lives in a temporary
#: DATA_ROOT, never under data/.
PROFILE = "golden"
PROFILE_CONFIG = {"scheduled_posting": {
    "buffer_channel_id": "chan_li_golden", "identity_slug": "li-golden",
    "platforms": {
        "x": {"buffer_channel_id": "chan_x_golden", "identity_slug": "x-golden",
              "buffer_api_key_env": "BUFFER_API_KEY_X"},
        "pinterest": {"buffer_channel_id": "chan_pin_golden",
                      "identity_slug": "pin-golden",
                      "buffer_api_key_env": "BUFFER_API_KEY_PIN"}}}}
ENV_KEYS = {"BUFFER_API_KEY": "golden-li-key", "BUFFER_API_KEY_X": "golden-x-key",
            "BUFFER_API_KEY_PIN": "golden-pin-key"}


def channel_label(channel):
    """A JSON key for a probe name, keeping case and whitespace visible."""
    return "<None>" if channel is None else "<%s>" % channel


def _file_tag(channel):
    """A filename-safe tag for a probe name (Windows refuses < and >)."""
    return "c%d" % CHANNELS.index(channel)

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


def _outcome(fn):
    """Call ``fn``; a refusal is an output too, so record what was raised."""
    try:
        return {"returned": fn()}
    except Exception as exc:
        return {"raised": "%s: %s" % (type(exc).__name__, exc)}


def _schedule(row, channel, workdir, allow_production=True):
    session = _Session()
    fetches = []

    def fetch_channel(channel_id, key=None, session=None):
        fetches.append(channel_id)
        return {"id": channel_id, "name": HANDLE}

    state = csv_pipeline.PipelineState(
        path=os.path.join(workdir, "state_%s.json" % _file_tag(channel)))
    try:
        results = csv_pipeline.schedule_pass(
            [dict(row)], CHANNEL_ID, state, rng=random.Random(SEED), key=KEY,
            session=session, upload=_upload, generate=_generate, now=NOW,
            platform=channel, identity_slug=HANDLE,
            fetch_channel=fetch_channel, allow_production=allow_production)
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
        path=os.path.join(workdir, "reconcile_%s.json" % _file_tag(channel)))
    state.update("k", status=csv_pipeline.SCHEDULED, post_id="p1")
    csv_pipeline.reconcile_published(state, CHANNEL_ID, fetch=fetch,
                                     platform=channel)
    return asked


def _state_path(channel):
    """Where the state file goes with no explicit path: the state_path branch."""
    path = csv_pipeline.PipelineState(profile_name=PROFILE, platform=channel).path
    return os.path.relpath(path, pm.DATA_ROOT).replace(os.sep, "/")


def _production_gate(channel, workdir):
    """The same row, with HANDLE declared production and no authorization."""
    os.environ[pm.PRODUCTION_IDENTITY_SLUGS_ENV] = HANDLE
    try:
        return _schedule(SYNTHETIC[0][1], channel,
                         os.path.join(workdir, "prod"), allow_production=False)
    finally:
        os.environ.pop(pm.PRODUCTION_IDENTITY_SLUGS_ENV, None)


def _resolved(channel):
    resolved = pm.resolve_scheduled(PROFILE, channel)
    # "drain" is whatever default_profile_config.json says, not routing.
    return {k: v for k, v in resolved.items() if k != "drain"}


def _probes(channel, workdir):
    from linkedin_automation import dashboard
    os.makedirs(os.path.join(workdir, "prod"), exist_ok=True)
    return {
        "state_path": _outcome(lambda: _state_path(channel)),
        "reconcile_statuses": _outcome(lambda: _reconcile_statuses(channel, workdir)),
        "production_gate": _production_gate(channel, workdir),
        "verify_identity_with_nothing": _outcome(
            lambda: list(csv_pipeline.verify_identity(channel, HANDLE))),
        "resolve_scheduled": _outcome(lambda: _resolved(channel)),
        "settings_view": _outcome(
            lambda: dashboard._scheduled_settings_view(PROFILE, channel)),
        "account": _outcome(lambda: dashboard._scheduled_account(PROFILE, channel)),
    }


class _PinnedDataAndKeys(object):
    """A temporary DATA_ROOT holding PROFILE's config, and dummy Buffer keys.
    Restores both on exit, so nothing outside the temp dir is read or written."""

    def __init__(self, workdir):
        self.root = os.path.join(workdir, "data")

    def __enter__(self):
        self.saved_root = pm.DATA_ROOT
        self.saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        pm.DATA_ROOT = self.root
        os.makedirs(os.path.join(self.root, PROFILE))
        with open(os.path.join(self.root, PROFILE, "profile_config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(PROFILE_CONFIG, f)
        os.environ.update(ENV_KEYS)
        return self

    def __exit__(self, *exc):
        pm.DATA_ROOT = self.saved_root
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def compute():
    """The whole corpus's outputs. Caller pins cwd and environment."""
    out = {"rows": {}, "probes": {}}
    with tempfile.TemporaryDirectory() as workdir, _PinnedDataAndKeys(workdir):
        for row_id, row in _load_rows():
            entry = {"row_key": csv_pipeline.row_key(row), "channels": {}}
            for channel in CHANNELS:
                sub = os.path.join(workdir, row_id)
                os.makedirs(sub, exist_ok=True)
                # validate_row calls due_at with no rng, so the module-level
                # random picks the minute quoted in a past-date message. Seed
                # it, or that message differs on every run.
                random.seed(SEED)
                entry["channels"][channel_label(channel)] = {
                    "compose": _compose(row, channel),
                    "validate": csv_pipeline.validate_row(
                        row, now=NOW, tz=bc.posting_timezone(),
                        platform=channel),
                    "schedule": _schedule(row, channel, sub),
                }
            out["rows"][row_id] = entry
        for channel in CHANNELS:
            random.seed(SEED)
            out["probes"][channel_label(channel)] = _probes(channel, workdir)
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
    # Text mode on purpose: git may check the fixture out with CRLF on
    # Windows. All content is compared exactly; only the line ending is not.
    with open(EXPECTED_FILE, encoding="utf-8") as f:
        expected = f.read()
    actual = render(compute())
    if actual != expected:
        exp, act = json.loads(expected), json.loads(actual)
        changed = []
        for section in ("rows", "probes"):
            changed += ["%s/%s" % (section, k)
                        for k in sorted(set(exp[section]) | set(act[section]))
                        if exp[section].get(k) != act[section].get(k)]
        raise AssertionError("golden corpus changed for: %s" % ", ".join(changed))


def test_the_corpus_covers_every_branch_it_exists_to_pin():
    """A golden file is only as good as what it exercises. Assert the capture
    actually contains each case the refactor could break."""
    with open(EXPECTED_FILE, encoding="utf-8") as f:
        exp = json.load(f)
    rows, probes = exp["rows"], exp["probes"]
    real = [k for k in rows if not k.startswith("syn-")]
    assert {k.split("-")[0] for k in real} == {"dev", "jeff", "xtest"}
    assert len(real) == 33
    assert set(probes) == {channel_label(c) for c in CHANNELS}
    x = {k: v["channels"]["<x>"] for k, v in rows.items()}
    li = {k: v["channels"]["<linkedin>"] for k, v in rows.items()}
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
    # Each probe reaches the branch it is there for.
    assert probes["<linkedin>"]["reconcile_statuses"]["returned"] == [["sent"]]
    assert probes["<x>"]["reconcile_statuses"]["returned"] == [["sent", "error"]]
    assert probes["<linkedin>"]["state_path"]["returned"] == \
        "golden/scheduled_posts_state.json"
    assert probes["<x>"]["state_path"]["returned"] == \
        "golden/x/scheduled_posts_state.json"
    assert "ProductionAccessRefused" in probes["<x>"]["production_gate"]["raised"]
    assert probes["<linkedin>"]["production_gate"]["status"] == "scheduled"
    assert probes["<linkedin>"]["resolve_scheduled"]["returned"]["api_key_env"] \
        == "BUFFER_API_KEY"
    assert probes["<x>"]["resolve_scheduled"]["returned"]["channel_id"] \
        == "chan_x_golden"
    assert probes["<linkedin>"]["settings_view"]["returned"][
        "buffer_api_key_env_editable"] is False
    assert probes["<x>"]["settings_view"]["returned"][
        "buffer_api_key_env_editable"] is True


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
