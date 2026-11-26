"""I1 (D-030): Instagram posts through Buffer, images and Reels.

Buffer and R2 are fakes at the transport (a recording session, a fake S3
client, a fake ``requests.get``); conftest's guards fail any real network
access. Every refusal is shown in both directions: the row or overlay that is
refused, and the one next to it that is not.
"""

import csv
import dataclasses
import json
import os
import sys
import types
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from linkedin_automation import buffer_client as bc  # noqa: E402
from linkedin_automation import channels  # noqa: E402
from linkedin_automation import content_calendar  # noqa: E402
from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import image_host  # noqa: E402
from linkedin_automation import instagram_channel as ig  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402
from linkedin_automation import youtube_channel as yt  # noqa: E402

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
IMAGE = os.path.join(HERE, "fixtures", "platform_golden", "pixel.png")
HOSTED_IMAGE = "https://img.test.invalid/pixel.png"
HOSTED_VIDEO = "https://img.test.invalid/posts/abc.mp4"
HANDLE = "ig_dev_account"
GOOD_OVERLAY = {"buffer_channel_id": "chan", "identity_slug": HANDLE,
                "buffer_api_key_env": "BUFFER_API_KEY_IG", "drain": False,
                "cadence": {"per_day": 1, "windows": {"1": ["afternoon"]}}}


# ─── a synthetic mp4 ────────────────────────────────────────────────────────

def _box(kind, payload=b""):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def mp4_bytes(width=1080, height=1920, handler=b"vide", ftyp=True):
    tkhd = _box(b"tkhd", b"\x00" * 76 + (width << 16).to_bytes(4, "big")
                + (height << 16).to_bytes(4, "big"))
    hdlr = _box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 13)
    trak = _box(b"trak", tkhd + _box(b"mdia", hdlr))
    head = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") if ftyp else _box(b"free")
    return head + _box(b"moov", trak) + _box(b"mdat", b"\x00" * 32)


def write_mp4(path, **kw):
    with open(path, "wb") as f:
        f.write(mp4_bytes(**kw))
    return str(path)


@pytest.fixture
def vertical(tmp_path):
    return write_mp4(tmp_path / "reel-vertical.mp4")


# ─── fakes ──────────────────────────────────────────────────────────────────

class _Resp(object):
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class Buffer(object):
    """Records every createPost input; answers success. Never networked."""

    def __init__(self):
        self.inputs = []

    def post(self, url, headers=None, json=None, timeout=None):
        payload = (json or {}).get("variables", {}).get("input")
        self.inputs.append(payload)
        return _Resp({"data": {"createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post-%d" % len(self.inputs), "status": "scheduled",
                     "dueAt": (payload or {}).get("dueAt")}}}})


class Recorder(object):
    def __init__(self, url):
        self.url, self.calls = url, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.url


def _fetch_channel(channel_id, key=None, session=None):
    return {"id": channel_id, "name": HANDLE, "service": "instagram"}


@pytest.fixture
def profile_cfg(monkeypatch):
    """The profile config every preflight reads, as a dict the test edits."""
    cfg = {"scheduled_posting": {"platforms": {"instagram": dict(GOOD_OVERLAY)}}}
    monkeypatch.setattr(pm, "get_profile_config", lambda name=None: cfg)
    return cfg


def make_row(**over):
    row = {"date": "2036-09-08", "time_window": "morning",
           "post_text": "A caption.", "topic": "", "tags": "#one #two",
           "image_path": "", "first_comment_link": "", "video_path": ""}
    row.update(over)
    return row


def _state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "instagram.json"),
                            platform="instagram")


def _schedule(rows, state, **kw):
    buffer = Buffer()
    images, videos = Recorder(HOSTED_IMAGE), Recorder(HOSTED_VIDEO)
    results = cp.schedule_pass(
        rows, "chan", state, key="ig-key", session=buffer, upload=images,
        upload_video=videos, now=NOW, platform="instagram",
        identity_slug=HANDLE, fetch_channel=_fetch_channel,
        profile_name="igprofile",
        generate=lambda *a, **k: pytest.fail("nothing generates captions"), **kw)
    return results, buffer, images, videos


def problems(row):
    return cp.validate_row(row, now=NOW, platform="instagram")


# ─── the declaration ───────────────────────────────────────────────────────

def test_instagram_is_a_buffer_overlay_channel_after_pinterest():
    assert list(channels.REGISTRY) == ["linkedin", "x", "pinterest", "instagram"]
    c = channels.get("instagram")
    assert (c.publisher, c.config_layout, c.key_env, c.identity_check,
            c.state_path, c.link_placement) == (
        channels.BUFFER, channels.OVERLAY, None, channels.BUFFER_HANDLE,
        channels.PLATFORM_DIR, channels.REFUSED)
    assert (c.text_limit.maximum, c.max_hashtags, c.video_max_bytes) == (
        2200, 30, 300 * 1000 * 1000)
    assert c.media_required and not c.image_required and not c.board_lookup


# ─── queue-time refusals, both directions ──────────────────────────────────

def test_a_caption_of_2200_characters_passes_and_2201_is_refused(vertical):
    assert problems(make_row(post_text="a" * 2200, tags="", video_path=vertical)) == []
    found = problems(make_row(post_text="a" * 2201, tags="", video_path=vertical))
    assert len(found) == 1 and "2201 characters against a 2200 limit" in found[0]
    assert "never truncated" in found[0]


def test_tags_count_toward_the_caption_limit(vertical):
    found = problems(make_row(post_text="a" * 2194, tags="#abcd", video_path=vertical))
    assert any("2201 characters" in p for p in found)


def test_thirty_hashtags_pass_and_thirty_one_are_refused(vertical):
    tags = " ".join("#t%d" % i for i in range(30))
    assert problems(make_row(tags=tags, video_path=vertical)) == []
    found = problems(make_row(tags=tags + " #t30", video_path=vertical))
    assert found == ["too many hashtags for Instagram: 31 against a 30 limit. "
                     "This is never trimmed automatically."]


def test_hashtags_in_the_caption_text_count_too(vertical):
    text = " ".join("#c%d" % i for i in range(31))
    assert any("31 against a 30" in p for p in problems(make_row(
        post_text=text, tags="", video_path=vertical)))


def test_a_hash_inside_a_word_is_not_a_hashtag():
    assert cp.HASHTAG_RE.findall("C# and a#b and #real") == ["#real"]


def test_a_row_with_no_image_and_no_video_is_refused(vertical):
    found = problems(make_row())
    assert found == ["Instagram needs an image or a video (video_path)."]
    assert problems(make_row(image_path=IMAGE)) == []
    assert problems(make_row(video_path=vertical)) == []


def test_a_row_with_an_image_and_a_video_is_refused(vertical):
    assert problems(make_row(image_path=IMAGE, video_path=vertical)) == [
        "Instagram posts one image or one video; this row has both."]


def test_a_link_is_refused_not_dropped(vertical):
    for col in ("first_comment_link", "link"):
        found = problems(make_row(video_path=vertical, **{col: "https://e.x/a"}))
        assert len(found) == 1 and "cannot carry a link" in found[0]


def test_a_missing_video_is_refused(tmp_path):
    found = problems(make_row(video_path=str(tmp_path / "nope.mp4")))
    assert len(found) == 1 and "video file does not exist" in found[0]


def test_a_horizontal_video_is_refused(tmp_path):
    flat = write_mp4(tmp_path / "flat.mp4", width=1920, height=1080)
    found = problems(make_row(video_path=flat))
    assert len(found) == 1 and "1920x1080; a Reel needs a vertical video" in found[0]


def test_a_video_over_the_channel_cap_is_refused(tmp_path, vertical, monkeypatch):
    size = os.path.getsize(vertical)
    small = dataclasses.replace(channels.INSTAGRAM, video_max_bytes=size - 1)
    monkeypatch.setitem(channels.REGISTRY, "instagram", small)
    found = problems(make_row(video_path=vertical))
    assert len(found) == 1 and "over the %d-byte limit" % (size - 1) in found[0]
    exact = dataclasses.replace(channels.INSTAGRAM, video_max_bytes=size)
    monkeypatch.setitem(channels.REGISTRY, "instagram", exact)
    assert problems(make_row(video_path=vertical)) == []


def test_queue_rows_rejects_and_stores_nothing_for_a_bad_row(tmp_path, vertical):
    state = _state(tmp_path)
    accepted, rejected, _s, _w = cp.queue_rows(
        [make_row(video_path=vertical), make_row()], state, now=NOW)
    assert len(accepted) == 1 and len(rejected) == 1
    assert "image or a video" in json.dumps(rejected)


def test_the_video_path_is_part_of_the_row_key(vertical, tmp_path):
    other = write_mp4(tmp_path / "other.mp4")
    a = cp.row_key(make_row(video_path=vertical), platform="instagram")
    b = cp.row_key(make_row(video_path=other), platform="instagram")
    assert a != b


# ─── the metadata body, at the transport ───────────────────────────────────

def test_a_reel_sends_one_video_asset_and_reel_metadata(tmp_path, vertical, profile_cfg):
    state = _state(tmp_path)
    results, buffer, images, videos = _schedule([make_row(video_path=vertical)], state)
    assert [r["status"] for r in results] == [cp.SCHEDULED]
    (sent,) = buffer.inputs
    assert sent["assets"] == [{"video": {"url": HOSTED_VIDEO}}]
    assert sent["metadata"] == {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    assert sent["text"] == "A caption.\n\n#one #two"
    assert videos.calls == [(vertical, 300 * 1000 * 1000)] and images.calls == []


def test_an_image_post_sends_one_image_asset_and_post_metadata(tmp_path, profile_cfg):
    state = _state(tmp_path)
    results, buffer, images, videos = _schedule([make_row(image_path=IMAGE)], state)
    assert [r["status"] for r in results] == [cp.SCHEDULED]
    (sent,) = buffer.inputs
    assert sent["assets"] == [{"image": {"url": HOSTED_IMAGE}}]
    assert sent["metadata"] == {"instagram": {"type": "post", "shouldShareToFeed": True}}
    assert videos.calls == [] and len(images.calls) == 1


def test_build_post_input_refuses_an_image_and_a_video_together():
    with pytest.raises(bc.BufferError):
        bc.build_post_input("c", "t", HOSTED_IMAGE, "2036-01-01T00:00:00Z",
                            video_url=HOSTED_VIDEO)


def test_build_post_input_without_a_video_is_unchanged():
    old = bc.build_post_input("c", "t", HOSTED_IMAGE, "2036-01-01T00:00:00Z")
    assert old["assets"] == [{"image": {"url": HOSTED_IMAGE}}]
    assert bc.build_post_input("c", "t", None, "2036-01-01T00:00:00Z")["assets"] == []


# ─── the preflight: cadence, drain, one publisher ─────────────────────────

def test_a_good_overlay_with_no_vp_brand_passes(profile_cfg):
    assert ig.preflight_refusal("p") is None


def test_missing_cadence_is_refused_and_nothing_is_sent(tmp_path, vertical, profile_cfg):
    del profile_cfg["scheduled_posting"]["platforms"]["instagram"]["cadence"]
    assert "has no cadence" in ig.preflight_refusal("p")
    with pytest.raises(cp.PreflightRefused) as exc:
        _schedule([make_row(video_path=vertical)], _state(tmp_path))
    assert "Nothing was created" in str(exc.value)
    assert isinstance(exc.value, cp.IdentityRefused)


def test_preflight_runs_before_any_upload_or_buffer_call(tmp_path, vertical,
                                                         profile_cfg, monkeypatch):
    del profile_cfg["scheduled_posting"]["platforms"]["instagram"]
    calls = []
    monkeypatch.setattr(cp, "verify_identity",
                        lambda *a, **k: calls.append("identity") or (True, "x"))
    buffer, videos = Buffer(), Recorder(HOSTED_VIDEO)
    with pytest.raises(cp.PreflightRefused):
        cp.schedule_pass([make_row(video_path=vertical)], "chan", _state(tmp_path),
                         key="k", session=buffer, upload_video=videos, now=NOW,
                         platform="instagram", identity_slug=HANDLE,
                         profile_name="p")
    assert (calls, buffer.inputs, videos.calls) == ([], [], [])


def test_a_malformed_cadence_is_refused(profile_cfg):
    profile_cfg["scheduled_posting"]["platforms"]["instagram"]["cadence"] = {"per_day": 1}
    assert "windows" in ig.preflight_refusal("p")


@pytest.mark.parametrize("drain", [True, None, "false", {"enabled": False}])
def test_drain_must_be_exactly_false(profile_cfg, drain):
    overlay = profile_cfg["scheduled_posting"]["platforms"]["instagram"]
    if drain is None:
        del overlay["drain"]
    else:
        overlay["drain"] = drain
    assert "drain must be false" in ig.preflight_refusal("p")


def test_no_overlay_and_no_profile_are_refused(profile_cfg):
    assert ig.preflight_refusal(None).startswith("no profile was named")
    del profile_cfg["scheduled_posting"]["platforms"]["instagram"]
    assert "no scheduled_posting.platforms.instagram" in ig.preflight_refusal("p")


def _vp(tmp_path, brand, items):
    """A video-publisher tree: ``items`` is ``[(sub, name, platform)]``."""
    root = tmp_path / "vp"
    for sub, name, platform in items:
        d = root / "content" / brand / sub / name
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text("platform: %s\nbrand: %s\n" % (platform, brand),
                                         encoding="utf-8")
    root.mkdir(exist_ok=True)
    return str(root)


def _with_vp(profile_cfg, root, brand):
    profile_cfg["scheduled_posting"]["platforms"]["youtube"] = {
        "vp_root": root, "vp_brand": brand}


def test_queued_vp_instagram_items_refuse(tmp_path, profile_cfg):
    root = _vp(tmp_path, "aws", [("queue", "a-001-ig", "instagram"),
                                 ("queue", "a-001-yt", "youtube")])
    _with_vp(profile_cfg, root, "aws")
    refusal = ig.preflight_refusal("p")
    assert "1 Instagram item(s) queued for brand 'aws'" in refusal
    assert "a-001-ig" in refusal and "a-001-yt" not in refusal


def test_vp_youtube_or_published_instagram_items_do_not_refuse(tmp_path, profile_cfg):
    root = _vp(tmp_path, "aws", [("queue", "a-001-yt", "youtube"),
                                 ("published", "a-002-ig", "instagram")])
    _with_vp(profile_cfg, root, "aws")
    assert ig.preflight_refusal("p") is None


def test_a_brand_with_no_vp_content_folder_passes(tmp_path, profile_cfg):
    root = _vp(tmp_path, "aws", [("queue", "a-001-ig", "instagram")])
    _with_vp(profile_cfg, root, "teratomahouse")
    assert ig.preflight_refusal("p") is None


def test_an_unreachable_vp_root_or_unreadable_manifest_refuses(tmp_path, profile_cfg):
    _with_vp(profile_cfg, str(tmp_path / "missing"), "aws")
    assert "is not a folder" in ig.preflight_refusal("p")
    root = _vp(tmp_path, "aws", [("queue", "a-001-ig", "instagram")])
    bad = os.path.join(root, "content", "aws", "queue", "a-001-ig", "manifest.yaml")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("platform: [unclosed\n")
    _with_vp(profile_cfg, root, "aws")
    assert "is unreadable" in ig.preflight_refusal("p")


def test_a_vp_brand_with_no_root_refuses(profile_cfg):
    profile_cfg["scheduled_posting"]["platforms"]["youtube"] = {"vp_brand": "aws"}
    assert "vp_root is empty" in ig.preflight_refusal("p")


def test_schedule_pass_is_refused_while_vp_has_instagram_queued(tmp_path, vertical,
                                                                 profile_cfg):
    _with_vp(profile_cfg, _vp(tmp_path, "aws", [("queue", "x-ig", "instagram")]), "aws")
    with pytest.raises(cp.PreflightRefused) as exc:
        _schedule([make_row(video_path=vertical)], _state(tmp_path))
    assert "video-publisher still has 1 Instagram item(s)" in str(exc.value)


def test_no_existing_channel_declares_a_preflight():
    for cid in ("linkedin", "x", "pinterest"):
        c = channels.get(cid)
        assert (c.preflight, c.video_column, c.max_hashtags, c.media_required,
                c.video_max_bytes) == (None, None, None, False, None), cid


# ─── video hosting ─────────────────────────────────────────────────────────

def test_mp4_dimensions_reads_the_video_track(tmp_path):
    assert image_host.mp4_dimensions(write_mp4(tmp_path / "v.mp4")) == (1080, 1920)
    assert image_host.mp4_dimensions(
        write_mp4(tmp_path / "h.mp4", width=1920, height=1080)) == (1920, 1080)


def test_a_file_that_is_not_an_mp4_or_has_no_video_track_is_refused(tmp_path):
    with pytest.raises(image_host.NotAVideo):
        image_host.read_video(write_mp4(tmp_path / "a.mp4", ftyp=False))
    with pytest.raises(image_host.NotAVideo):
        image_host.read_video(write_mp4(tmp_path / "b.mp4", handler=b"soun"))
    with pytest.raises(image_host.NotAVideo):
        image_host.read_video(write_mp4(tmp_path / "c.mov"))
    empty = tmp_path / "d.mp4"
    empty.write_bytes(b"")
    with pytest.raises(image_host.NotAVideo):
        image_host.read_video(str(empty))


def test_a_square_video_is_not_vertical(tmp_path):
    with pytest.raises(image_host.NotVertical):
        image_host.read_video(write_mp4(tmp_path / "s.mp4", width=1080, height=1080))


class _S3(object):
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


class _Get(object):
    def __init__(self, status=200, head=None, length=None):
        self.status_code, self._head, self.closed = status, head, False
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def iter_content(self, chunk_size=1):
        yield self._head

    def close(self):
        self.closed = True


R2_ENV = {"R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET": "b",
          "R2_ENDPOINT": "https://acct.r2.cloudflarestorage.test",
          "R2_PUBLIC_BASE_URL": "https://pub.r2.test"}


def test_upload_video_stores_mp4_and_verifies_the_public_copy(vertical, monkeypatch):
    s3 = _S3()
    monkeypatch.setattr(image_host, "_client", lambda cfg: s3)
    data = open(vertical, "rb").read()
    got = {}

    def get(url, **kw):
        got["url"], got["stream"] = url, kw.get("stream")
        got["resp"] = _Get(head=data[:16], length=len(data))
        return got["resp"]
    monkeypatch.setattr(image_host.requests, "get", get)
    url = image_host.upload_video(vertical, max_bytes=10 ** 6, env=R2_ENV)
    (put,) = s3.puts
    assert put["ContentType"] == "video/mp4" and put["Body"] == data
    assert put["Key"].startswith("posts/") and put["Key"].endswith(".mp4")
    assert url == "https://pub.r2.test/" + put["Key"] == got["url"]
    assert got["stream"] is True and got["resp"].closed


def test_upload_video_refuses_before_uploading_an_oversize_file(vertical, monkeypatch):
    s3 = _S3()
    monkeypatch.setattr(image_host, "_client", lambda cfg: s3)
    with pytest.raises(image_host.VideoTooLarge):
        image_host.upload_video(vertical, max_bytes=10, env=R2_ENV)
    assert s3.puts == []


@pytest.mark.parametrize("resp,why", [
    (_Get(status=403), "HTTP 403"),
    (_Get(head=b"<html>login page", length=99), "does not serve an mp4"),
    (_Get(head=b"\x00\x00\x00\x18ftypisom", length=5), "Content-Length '5'"),
    (_Get(head=b"\x00\x00\x00\x18ftypisom"), "Content-Length None"),
])
def test_verify_public_video_refuses_what_is_not_the_same_mp4(monkeypatch, resp, why):
    monkeypatch.setattr(image_host.requests, "get", lambda url, **kw: resp)
    with pytest.raises(image_host.NotPublic) as exc:
        image_host.verify_public_video("https://pub.r2.test/v.mp4", expect_bytes=99)
    assert why in str(exc.value)


def test_verify_public_video_accepts_the_same_mp4(monkeypatch):
    resp = _Get(head=b"\x00\x00\x00\x18ftypisom", length=99)
    monkeypatch.setattr(image_host.requests, "get", lambda url, **kw: resp)
    assert image_host.verify_public_video("https://pub.r2.test/v.mp4", 99) == 99


# ─── caption builders, both layouts ────────────────────────────────────────

SHORTS_MD = """## C-900 · A Synthetic Item (40 s)
- **Files:** `C-900-synthetic-vertical.mp4`
- **Caption (draft):** One plain sentence about a made-up idea.
  It continues here. #shorts
- **Essay:** example.invalid/essay
- **Tags:** #AI #Synthetic #shorts #Test

### YouTube Shorts (2026-01-01)
- **Title:** A synthetic title #shorts
- **Description:**
  A description that must not be used.
- **Tags:** synthetic, test
"""

TITLE_MD = """# TH-900

## Title
A synthetic horror title #shorts

## Description
A made-up first line.

A second paragraph.

#horror #shorts #synthetic

## Tags
horror, synthetic
"""


def test_the_shorts_layout_uses_the_item_sections_caption_and_tags():
    text, tags, reason = ig.caption_from_shorts_section(SHORTS_MD)
    assert reason is None
    assert text == "One plain sentence about a made-up idea.\nIt continues here."
    assert tags == "#AI #Synthetic #Test"
    assert "title" not in text.lower() and "description that" not in text


def test_the_shorts_layout_skips_an_item_with_no_caption_draft():
    md = SHORTS_MD.replace("- **Caption (draft):**", "- **Hook:**")
    assert ig.caption_from_shorts_section(md) == (
        None, None, "youtube.md has no Caption (draft) field")


def test_the_title_layout_uses_the_description_minus_shorts():
    text, tags, reason = ig.caption_from_title_sections(TITLE_MD)
    assert reason is None and tags == ""
    assert text == ("A made-up first line.\n\nA second paragraph.\n\n"
                    "#horror #synthetic")
    assert "synthetic horror title" not in text


def test_the_title_layout_skips_an_item_with_no_description():
    md = TITLE_MD.replace("## Description", "## Notes")
    assert ig.caption_from_title_sections(md)[2] == "youtube.md has no ## Description section"


def test_drop_shorts_token_leaves_other_lines_verbatim():
    assert ig.drop_shorts_token("a  b\n#x #Shorts #y\n#shortstop") == "a  b\n#x #y\n#shortstop"


# ─── instagram-from-youtube ────────────────────────────────────────────────

def _content(tmp_path, monkeypatch, fmt=yt.METADATA_SHORTS_SECTION):
    root = tmp_path / "content"
    for name, md, mp4 in (("C-900-synthetic", SHORTS_MD, "vertical"),
                          ("C-901-flat", SHORTS_MD, "horizontal"),
                          ("C-902-nocap", SHORTS_MD.replace("Caption (draft)", "Hook"),
                           "vertical")):
        d = root / name
        d.mkdir(parents=True)
        (d / "youtube.md").write_text(md, encoding="utf-8")
        kw = {} if mp4 == "vertical" else {"width": 1920, "height": 1080}
        write_mp4(d / ("%s-vertical.mp4" % name), **kw)
    cfg = types.SimpleNamespace(content_root=str(root), item_prefix="C",
                                video_suffix="-vertical.mp4", metadata_format=fmt)
    monkeypatch.setattr(yt, "youtube_config", lambda profile: cfg)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(pm, "DATA_ROOT", str(data))
    return root, data


def test_the_dry_run_proposes_and_writes_nothing(tmp_path, monkeypatch):
    _root, data = _content(tmp_path, monkeypatch)
    lines = []
    code, summary = ig.from_youtube("p", dry_run=True, out=lines.append)
    assert code == ig.EXIT_OK
    assert (summary["proposed"], summary["skipped"], summary["written"]) == (1, 2, None)
    assert lines[-1] == ("instagram-from-youtube p [dry-run]: 1 proposed, 2 skipped; "
                         "nothing written")
    assert any("SKIP C-901-flat" in line and "vertical video" in line for line in lines)
    assert any("SKIP C-902-nocap: youtube.md has no Caption (draft) field" == line.strip()
               for line in lines)
    assert list(os.walk(str(data))) == [(str(data), [], [])]


def test_without_dry_run_it_writes_the_csv_and_queues_nothing(tmp_path, monkeypatch):
    root, data = _content(tmp_path, monkeypatch)
    code, summary = ig.from_youtube("p", dry_run=False, out=lambda s: None)
    assert code == ig.EXIT_OK and summary["written"] == ig.proposal_path("p")
    with open(summary["written"], encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["source_item"] for r in rows] == ["C-900-synthetic"]
    assert rows[0]["post_text"].startswith("One plain sentence")
    assert rows[0]["tags"] == "#AI #Synthetic #Test"
    assert rows[0]["video_path"] == str(root / "C-900-synthetic" / "C-900-synthetic-vertical.mp4")
    assert (rows[0]["date"], rows[0]["time_window"]) == ("", "")
    assert not os.path.exists(ig.state_path("p"))


def test_an_item_with_an_instagram_row_is_not_proposed_again(tmp_path, monkeypatch):
    root, data = _content(tmp_path, monkeypatch)
    mp4 = str(root / "C-900-synthetic" / "C-900-synthetic-vertical.mp4")
    os.makedirs(os.path.dirname(ig.state_path("p")))
    state = cp.PipelineState(path=ig.state_path("p"), platform="instagram")
    cp.queue_rows([make_row(video_path=mp4)], state, now=NOW)
    code, summary = ig.from_youtube("p", dry_run=True, out=lambda s: None)
    assert summary["proposed"] == 0


def test_a_layout_with_no_caption_rule_is_refused(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch, fmt="other")
    lines = []
    code, _s = ig.from_youtube("p", dry_run=True, out=lines.append)
    assert code == ig.EXIT_REFUSED and "no caption rule" in lines[-1]


# ─── the calendar source ───────────────────────────────────────────────────

def test_instagram_rows_appear_in_the_calendar(tmp_path, monkeypatch, vertical):
    monkeypatch.setattr(pm, "DATA_ROOT", str(tmp_path))
    path = content_calendar.state_path("p", "instagram")
    assert path == os.path.join(str(tmp_path), "p", "instagram", cp.STATE_NAME)
    os.makedirs(os.path.dirname(path))
    state = cp.PipelineState(path=path, platform="instagram")
    cp.queue_rows([make_row(video_path=vertical)], state, now=NOW)
    items = list(content_calendar.state_file_items("p"))
    assert [i["platform"] for i in items] == ["instagram"]
