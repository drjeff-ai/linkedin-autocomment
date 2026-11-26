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


# ── video (I1, D-030) ───────────────────────────────────────────────────────
#
# Instagram Reels go through Buffer the same way images do: a public URL Buffer
# reads at createPost time. The checks mirror the image ones. The container is
# read from its own boxes, not the extension, and the upload is not done until
# the public URL has been fetched back anonymously and found to be the same mp4.

VIDEO_CONTENT_TYPE = "video/mp4"
#: The box path to a track's header and its handler.
_CONTAINER_BOXES = (b"moov", b"trak", b"mdia")


class NotAVideo(ImageHostError):
    """Not an mp4 this code can read the picture size of."""


class VideoTooLarge(ImageHostError):
    pass


class NotVertical(ImageHostError):
    pass


def _boxes(f, start, end):
    """``(type, payload_start, box_end)`` for each ISO-BMFF box in ``[start, end)``."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size = int.from_bytes(head[:4], "big")
        kind = head[4:8]
        payload = pos + 8
        if size == 1:
            big = f.read(8)
            if len(big) < 8:
                return
            size = int.from_bytes(big, "big")
            payload = pos + 16
        elif size == 0:
            size = end - pos
        if size < payload - pos or pos + size > end:
            raise NotAVideo("malformed mp4 box %r at byte %d" % (kind, pos))
        yield kind, payload, pos + size
        pos += size


def _video_track_size(f, trak_start, trak_end):
    """``(width, height)`` from a ``trak`` whose handler is ``vide``, else None."""
    size = None
    handler = None
    for kind, start, end in _boxes(f, trak_start, trak_end):
        if kind == b"tkhd":
            f.seek(start)
            body = f.read(end - start)
            # width and height are the last 8 bytes, 16.16 fixed point.
            if len(body) < 84:
                raise NotAVideo("mp4 track header is %d bytes" % len(body))
            size = (int.from_bytes(body[-8:-4], "big") >> 16,
                    int.from_bytes(body[-4:], "big") >> 16)
        elif kind == b"mdia":
            for sub, s2, e2 in _boxes(f, start, end):
                if sub == b"hdlr":
                    f.seek(s2 + 8)       # version/flags, pre_defined
                    handler = f.read(4)
    return size if handler == b"vide" else None


def mp4_dimensions(path):
    """``(width, height)`` of the first video track, from the file's own
    ``ftyp``/``moov``/``trak``/``tkhd`` boxes. Raises :class:`NotAVideo`.

    The track header's size is the picture before any rotation matrix, which
    is what a vertical render writes."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        total = f.tell()
        first = next(_boxes(f, 0, total), None)
        if first is None or first[0] != b"ftyp":
            raise NotAVideo("%s is not an mp4: it does not start with an ftyp box"
                            % path)
        for kind, start, end in _boxes(f, 0, total):
            if kind != b"moov":
                continue
            for sub, s2, e2 in _boxes(f, start, end):
                if sub == b"trak":
                    found = _video_track_size(f, s2, e2)
                    if found:
                        return found
        raise NotAVideo("%s has no video track this code can read" % path)


def read_video(path: str, max_bytes: int = None):
    """``(size_bytes, width, height)`` for a vertical mp4, or raise.

    Refused: a missing or empty file, one over ``max_bytes``, one that is not
    an mp4 with a readable video track, and one that is not taller than wide."""
    if not os.path.isfile(path):
        raise ImageNotFound("video file does not exist: %s" % path)
    if not path.lower().endswith(".mp4"):
        raise NotAVideo("%s is not an .mp4" % path)
    size = os.path.getsize(path)
    if size == 0:
        raise NotAVideo("video file is empty: %s" % path)
    if max_bytes is not None and size > max_bytes:
        raise VideoTooLarge("%s is %d bytes, over the %d-byte limit. It is never "
                            "re-encoded automatically." % (path, size, max_bytes))
    try:
        width, height = mp4_dimensions(path)
    except OSError as exc:
        raise ImageHostError("could not read %s: %s" % (path, exc)) from exc
    if height <= width:
        raise NotVertical("%s is %dx%d; a Reel needs a vertical video"
                          % (path, width, height))
    return size, width, height


def verify_public_video(url: str, expect_bytes: int, timeout: int = 30) -> int:
    """Fetch ``url`` with NO credentials and confirm the same mp4 comes back:
    HTTP 200, an ``ftyp`` box first, and ``Content-Length`` equal to the file's
    size. Streams, so a large video is not downloaded. Returns the size."""
    try:
        resp = requests.get(url, timeout=timeout, stream=True,
                            headers={"User-Agent": "linkedin-automation/1.0"})
    except requests.RequestException as exc:
        raise NotPublic("could not fetch %s anonymously: %s" % (url, exc)) from exc
    try:
        if resp.status_code != 200:
            raise NotPublic(
                "uploaded, but %s returns HTTP %s to an anonymous request. The "
                "BUCKET is not public." % (url, resp.status_code))
        head = next(resp.iter_content(chunk_size=16), b"")
        if head[4:8] != b"ftyp":
            raise NotPublic("uploaded, but %s does not serve an mp4 (starts %r)"
                            % (url, head[:8]))
        length = resp.headers.get("Content-Length")
        if length is None or not str(length).isdigit() or int(length) != expect_bytes:
            raise NotPublic("uploaded %d bytes but %s reports Content-Length %r"
                            % (expect_bytes, url, length))
    finally:
        resp.close()
    return expect_bytes


def upload_video(path: str, max_bytes: int = None, env=None, verify: bool = True) -> str:
    """Upload a vertical mp4 to R2 and return its public URL, after
    :func:`read_video`'s checks. Content-addressed like :func:`upload_image`."""
    cfg = load_config(env)
    size, width, height = read_video(path, max_bytes)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise ImageHostError("could not read %s: %s" % (path, exc)) from exc
    key = content_key(data, "mp4")
    url = public_url(key, cfg["R2_PUBLIC_BASE_URL"])
    logger.info("Uploading %s (%d bytes, %dx%d) -> %s", path, size, width, height, key)
    try:
        _client(cfg).put_object(
            Bucket=cfg["R2_BUCKET"], Key=key, Body=data,
            ContentType=VIDEO_CONTENT_TYPE,
            CacheControl="public, max-age=31536000, immutable")
    except Exception as exc:
        raise UploadFailed("upload to R2 failed for %s: %s" % (path, exc)) from exc
    if verify:
        verify_public_video(url, expect_bytes=len(data))
        logger.info("Verified public: %s (%d bytes)", url, len(data))
    return url
