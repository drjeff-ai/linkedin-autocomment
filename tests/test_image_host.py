"""Phase 1: the R2 uploader, offline.

No network and no boto3 calls - the client is stubbed. The live upload is the
human-verifiable half; this half pins the logic that decides WHAT gets uploaded
and under what name, and the failures that must be loud.

Two of these guard mistakes that would not surface here at all, but three phases
later inside Buffer: a wrong content type, and a bucket that is not public.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import image_host as ih  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64

ENV = {
    "R2_ACCESS_KEY_ID": "ak",
    "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET": "bucket",
    "R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
    "R2_PUBLIC_BASE_URL": "https://pub-abc.r2.dev",
}


# --- content type comes from the bytes, never the name ----------------------

@pytest.mark.parametrize("data,ctype,ext", [
    (PNG, "image/png", "png"),
    (JPEG, "image/jpeg", "jpg"),
    (GIF, "image/gif", "gif"),
    (WEBP, "image/webp", "webp"),
])
def test_each_format_is_recognised_by_its_magic_bytes(data, ctype, ext):
    assert ih.sniff_image_type(data) == (ctype, ext)


def test_non_images_are_not_recognised():
    for junk in (b"not an image at all", b"<html><body>hi</body></html>",
                 b"%PDF-1.4", b"", b"RIFFxxxxNOTWEBP"):
        assert ih.sniff_image_type(junk) is None


def test_a_jpeg_named_png_is_stored_as_a_jpeg(tmp_path):
    """The extension lies; the bytes do not. Trusting the name here is what
    would store the wrong content type and make Buffer refuse the post."""
    f = tmp_path / "actually_a_jpeg.png"
    f.write_bytes(JPEG)
    data, ctype, ext = ih.read_image(str(f))
    assert ctype == "image/jpeg"
    assert ext == "jpg"
    assert ih.content_key(data, ext).endswith(".jpg")


# --- keys are content-addressed ---------------------------------------------

def test_the_same_bytes_always_produce_the_same_key():
    assert ih.content_key(PNG, "png") == ih.content_key(PNG, "png")


def test_different_bytes_produce_different_keys():
    assert ih.content_key(PNG, "png") != ih.content_key(PNG + b"x", "png")


def test_the_key_shape_is_posts_hash_ext():
    key = ih.content_key(PNG, "png")
    prefix, _, name = key.partition("/")
    assert prefix == "posts"
    stem, _, ext = name.partition(".")
    assert ext == "png"
    assert len(stem) == ih.HASH_CHARS
    assert all(c in "0123456789abcdef" for c in stem)


# --- the public URL is built from the PUBLIC base, never the endpoint --------

def test_public_url_uses_the_public_base():
    url = ih.public_url("posts/abc.png", "https://pub-abc.r2.dev")
    assert url == "https://pub-abc.r2.dev/posts/abc.png"


def test_public_url_tolerates_a_trailing_slash():
    assert (ih.public_url("posts/a.png", "https://pub-abc.r2.dev/")
            == "https://pub-abc.r2.dev/posts/a.png")


def test_the_endpoint_pasted_into_the_public_slot_is_rejected():
    """The classic misconfiguration. It would upload fine and then fail inside
    Buffer, a long way from the cause."""
    env = dict(ENV, R2_PUBLIC_BASE_URL="https://acct.r2.cloudflarestorage.com")
    with pytest.raises(ih.ImageHostError, match="upload-only"):
        ih.load_config(env)


@pytest.mark.parametrize("missing", sorted(ENV))
def test_every_missing_setting_is_named(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ih.ImageHostError, match=missing):
        ih.load_config(env)


def test_missing_settings_are_reported_together():
    with pytest.raises(ih.ImageHostError) as exc:
        ih.load_config({})
    for name in ENV:
        assert name in str(exc.value)


# --- the loud failures ------------------------------------------------------

def test_a_missing_file_is_its_own_error(tmp_path):
    with pytest.raises(ih.ImageNotFound, match="does not exist"):
        ih.read_image(str(tmp_path / "nope.png"))


def test_a_non_image_file_is_refused_before_upload(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"just some text, definitely not a picture")
    with pytest.raises(ih.NotAnImage, match="not a PNG"):
        ih.read_image(str(f))


def test_an_empty_file_is_refused(tmp_path):
    f = tmp_path / "empty.png"
    f.write_bytes(b"")
    with pytest.raises(ih.NotAnImage, match="empty"):
        ih.read_image(str(f))


# --- verify-public: the check that must not be skippable --------------------

class FakeResponse:
    def __init__(self, status=200, content=PNG):
        self.status_code = status
        self.content = content


def test_verify_public_accepts_a_real_image(monkeypatch):
    monkeypatch.setattr(ih.requests, "get", lambda *a, **k: FakeResponse())
    assert ih.verify_public("https://pub-abc.r2.dev/posts/a.png") == len(PNG)


def test_a_403_means_the_bucket_is_not_public(monkeypatch):
    """The message has to point at the bucket, because the bytes and the
    credentials are both fine in this case."""
    monkeypatch.setattr(ih.requests, "get",
                        lambda *a, **k: FakeResponse(status=403))
    with pytest.raises(ih.NotPublic, match="BUCKET is not public"):
        ih.verify_public("https://pub-abc.r2.dev/posts/a.png")


def test_an_html_error_page_is_not_an_image(monkeypatch):
    monkeypatch.setattr(ih.requests, "get", lambda *a, **k: FakeResponse(
        content=b"<html><body>Sign in</body></html>"))
    with pytest.raises(ih.NotPublic, match="not an image"):
        ih.verify_public("https://pub-abc.r2.dev/posts/a.png")


def test_a_type_mismatch_between_upload_and_serve_is_caught(monkeypatch):
    monkeypatch.setattr(ih.requests, "get",
                        lambda *a, **k: FakeResponse(content=JPEG))
    with pytest.raises(ih.NotPublic, match="serves"):
        ih.verify_public("https://pub-abc.r2.dev/a.png", expect_type="image/png")


def test_a_network_failure_is_reported_as_not_public(monkeypatch):
    def boom(*a, **k):
        raise ih.requests.RequestException("dns went away")
    monkeypatch.setattr(ih.requests, "get", boom)
    with pytest.raises(ih.NotPublic, match="anonymously"):
        ih.verify_public("https://pub-abc.r2.dev/a.png")


# --- upload_image, with boto3 stubbed ---------------------------------------

class FakeClient:
    def __init__(self):
        self.calls = []

    def put_object(self, **kw):
        self.calls.append(kw)
        return {}


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(ih, "_client", lambda cfg: client)
    monkeypatch.setattr(ih.requests, "get", lambda *a, **k: FakeResponse())
    return client


def test_upload_sets_the_real_content_type_not_octet_stream(tmp_path, fake_client):
    f = tmp_path / "pic.png"
    f.write_bytes(PNG)
    ih.upload_image(str(f), env=ENV)
    put = fake_client.calls[0]
    assert put["ContentType"] == "image/png"
    assert put["Bucket"] == "bucket"
    assert put["Key"].startswith("posts/")
    assert put["Body"] == PNG


def test_upload_returns_the_public_url(tmp_path, fake_client):
    f = tmp_path / "pic.png"
    f.write_bytes(PNG)
    url = ih.upload_image(str(f), env=ENV)
    assert url.startswith("https://pub-abc.r2.dev/posts/")
    assert url.endswith(".png")
    assert "r2.cloudflarestorage.com" not in url


def test_uploading_the_same_file_twice_reuses_the_key(tmp_path, fake_client):
    f = tmp_path / "pic.png"
    f.write_bytes(PNG)
    first = ih.upload_image(str(f), env=ENV)
    second = ih.upload_image(str(f), env=ENV)
    assert first == second
    assert fake_client.calls[0]["Key"] == fake_client.calls[1]["Key"]


def test_an_upload_error_is_wrapped_not_leaked(tmp_path, monkeypatch):
    class Broken:
        def put_object(self, **kw):
            raise RuntimeError("connection reset")
    monkeypatch.setattr(ih, "_client", lambda cfg: Broken())
    f = tmp_path / "pic.png"
    f.write_bytes(PNG)
    with pytest.raises(ih.UploadFailed, match="connection reset"):
        ih.upload_image(str(f), env=ENV)


def test_a_bucket_that_is_not_public_fails_the_upload(tmp_path, monkeypatch):
    """The whole reason verify runs here: this must not be discovered later,
    inside Buffer's createPost, where the message would be about an image URL."""
    monkeypatch.setattr(ih, "_client", lambda cfg: FakeClient())
    monkeypatch.setattr(ih.requests, "get",
                        lambda *a, **k: FakeResponse(status=403))
    f = tmp_path / "pic.png"
    f.write_bytes(PNG)
    with pytest.raises(ih.NotPublic):
        ih.upload_image(str(f), env=ENV)


def test_verification_can_be_skipped_only_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "_client", lambda cfg: FakeClient())

    def boom(*a, **k):
        raise AssertionError("verify_public must not run when verify=False")
    monkeypatch.setattr(ih.requests, "get", boom)
    f = tmp_path / "pic.png"
    f.write_bytes(PNG)
    assert ih.upload_image(str(f), env=ENV, verify=False)
