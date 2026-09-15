"""Upload a local image to Cloudflare R2 and return a PUBLICLY fetchable URL.

Phase 1 of the scheduled-posting hybrid. Buffer takes neither a local file nor
base64: it needs a URL, and it READS THE BYTES at createPost time — the spike
proved that a URL serving no image returns ``InvalidInputError`` ("Image could
not be read from its URL") and creates nothing at all. So the CSV's local
``image_path`` has to be hosted publicly before Buffer ever sees the row.

Two things this module refuses to get wrong, because both fail late and quietly
otherwise:

**The two R2 URLs are different hosts.** ``R2_ENDPOINT`` is the authenticated S3
API and serves nothing publicly; ``R2_PUBLIC_BASE_URL`` is what the world
fetches. Neither is derivable from the other. Handing Buffer an endpoint link
would fail at createPost, far from the cause.

**Content type comes from the file's MAGIC BYTES, not its extension.** An object
stored as ``application/octet-stream`` — which is what boto3 defaults to — is
exactly the shape that makes Buffer's fetch fail. A file named ``.png`` that is
really a JPEG, or really a text file, is caught here rather than three phases
later.

And the upload is not considered done until the result has been fetched back
**anonymously** and confirmed to be an image. A bucket whose public access was
never switched on is a configuration error that must surface at upload time,
while a human is watching, not silently at Buffer's createPost later.
"""

import hashlib
import logging
import os

import requests
from dotenv import load_dotenv

# Matches poster.py and post_finder.py. Without it a caller with a populated
# .env still gets "R2 is not configured", which sends the reader to check
# credentials that are actually fine.
load_dotenv()

logger = logging.getLogger(__name__)

# Magic-byte signatures, longest first so a prefix cannot shadow a longer match.
# Only formats LinkedIn actually accepts are listed: anything else should fail
# loudly here rather than be uploaded and rejected downstream.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF89a", "image/gif", "gif"),
    (b"GIF87a", "image/gif", "gif"),
)

# WEBP is RIFF....WEBP - the marker is at offset 8, so it needs its own check.
_RIFF = b"RIFF"
_WEBP = b"WEBP"

REQUIRED_VARS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
                 "R2_ENDPOINT", "R2_PUBLIC_BASE_URL")

KEY_PREFIX = "posts"

# Content-addressed keys, so re-running a CSV row cannot create a second copy of
# the same picture. 16 hex chars of sha256 is 64 bits - ample for this, and short
# enough to read in a URL.
HASH_CHARS = 16


class ImageHostError(RuntimeError):
    """Anything that stops a local image becoming a public URL."""


class ImageNotFound(ImageHostError):
    pass


class NotAnImage(ImageHostError):
    pass


class UploadFailed(ImageHostError):
    pass


class NotPublic(ImageHostError):
    """Uploaded, but the object is not anonymously fetchable.

    Its own type because the fix is different from every other failure here:
    the bytes are fine and the credentials are fine; the BUCKET is not public.
    """


def sniff_image_type(data: bytes):
    """Return ``(content_type, extension)`` from the bytes themselves.

    Returns ``None`` for anything not a recognised image. The extension is
    derived from the sniffed type too, so a mislabelled ``.png`` is stored under
    the extension it actually is.
    """
    for sig, ctype, ext in _SIGNATURES:
        if data.startswith(sig):
            return ctype, ext
    if data[:4] == _RIFF and data[8:12] == _WEBP:
        return "image/webp", "webp"
    return None


def content_key(data: bytes, ext: str, prefix: str = KEY_PREFIX) -> str:
    """``posts/<sha256[:16]>.<ext>`` - same bytes, same key, always."""
    digest = hashlib.sha256(data).hexdigest()[:HASH_CHARS]
    return "%s/%s.%s" % (prefix, digest, ext)


def public_url(key: str, base: str) -> str:
    return "%s/%s" % (base.rstrip("/"), key.lstrip("/"))


def load_config(env=None):
    """Read R2 settings from the environment, failing on the first gap.

    Missing configuration is reported as one list rather than one variable at a
    time, so a half-filled .env is fixed in a single pass.
    """
    env = os.environ if env is None else env
    missing = [v for v in REQUIRED_VARS if not (env.get(v) or "").strip()]
    if missing:
        raise ImageHostError(
            "R2 is not configured - missing %s. These live in .env and are "
            "never committed; see .env.example." % ", ".join(missing))
    cfg = {v: env[v].strip() for v in REQUIRED_VARS}
    if cfg["R2_PUBLIC_BASE_URL"].rstrip("/") in cfg["R2_ENDPOINT"].rstrip("/"):
        # The classic misconfiguration: the S3 endpoint pasted into both slots.
        # Nothing is served publicly from the endpoint, so this would upload
        # fine and then fail at Buffer, a long way from the cause.
        raise ImageHostError(
            "R2_PUBLIC_BASE_URL looks like the S3 endpoint. The endpoint is "
            "upload-only and serves nothing publicly; the public base is the "
            "bucket's r2.dev URL or a custom domain.")
    return cfg


def _client(cfg):
    import boto3
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=cfg["R2_ENDPOINT"],
        aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
        # R2 ignores the region but the signer requires one; "auto" is what
        # Cloudflare documents.
        region_name="auto",
        config=Config(signature_version="s3v4",
                      retries={"max_attempts": 3, "mode": "standard"}),
    )


def read_image(path: str):
    """Return ``(bytes, content_type, ext)`` or raise with a usable message."""
    if not os.path.isfile(path):
        raise ImageNotFound("image file does not exist: %s" % path)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise ImageHostError("could not read %s: %s" % (path, exc)) from exc
    if not data:
        raise NotAnImage("image file is empty: %s" % path)
    sniffed = sniff_image_type(data)
    if sniffed is None:
        raise NotAnImage(
            "%s is not a PNG, JPEG, GIF or WEBP - its first bytes are %r. "
            "Uploading it would store the wrong content type and Buffer would "
            "then refuse the post." % (path, data[:8]))
    ctype, ext = sniffed
    return data, ctype, ext


def verify_public(url: str, expect_type: str = None, timeout: int = 30) -> int:
    """Fetch ``url`` with NO credentials and confirm an image comes back.

    Anonymous on purpose: the boto3 client could read the object whatever the
    bucket's public setting, so verifying through it would prove nothing about
    what Buffer will see. Returns the byte count.
    """
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "linkedin-automation/1.0"})
    except requests.RequestException as exc:
        raise NotPublic("could not fetch %s anonymously: %s" % (url, exc)) from exc

    if resp.status_code != 200:
        raise NotPublic(
            "uploaded, but %s returns HTTP %s to an anonymous request. The "
            "object is there and the credentials worked - the BUCKET is not "
            "public. Enable the r2.dev development URL or attach a custom "
            "domain." % (url, resp.status_code))

    body = resp.content
    sniffed = sniff_image_type(body)
    if sniffed is None:
        raise NotPublic(
            "uploaded, but %s served %d bytes that are not an image (starts "
            "%r). A login or error page is the usual cause."
            % (url, len(body), body[:8]))
    if expect_type and sniffed[0] != expect_type:
        raise NotPublic("uploaded %s but the URL serves %s"
                        % (expect_type, sniffed[0]))
    return len(body)


def upload_image(path: str, env=None, verify: bool = True) -> str:
    """Upload ``path`` to R2 and return its public URL.

    Idempotent: the key is the content hash, so re-uploading the same picture
    overwrites itself rather than accumulating copies.
    """
    cfg = load_config(env)
    data, ctype, ext = read_image(path)
    key = content_key(data, ext)
    url = public_url(key, cfg["R2_PUBLIC_BASE_URL"])

    logger.info("Uploading %s (%d bytes, %s) -> %s", path, len(data), ctype, key)
    try:
        _client(cfg).put_object(
            Bucket=cfg["R2_BUCKET"],
            Key=key,
            Body=data,
            # The whole point: never let this default to octet-stream.
            ContentType=ctype,
            # Content-addressed, so the bytes behind a key can never change.
            CacheControl="public, max-age=31536000, immutable",
        )
    except Exception as exc:
        raise UploadFailed("upload to R2 failed for %s: %s" % (path, exc)) from exc

    if verify:
        size = verify_public(url, expect_type=ctype)
        logger.info("Verified public: %s (%d bytes)", url, size)
    return url
