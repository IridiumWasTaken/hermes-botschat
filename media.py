"""Media helpers: inbound download/decrypt, outbound R2 upload.

Port of the reference plugin's readMedia / plugin-upload logic
(packages/plugin/src/channel.ts) using only stdlib urllib.
"""

import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger("botschat")

EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "video/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "application/pdf": "pdf",
}

_EXT_FALLBACK = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".pdf": "application/pdf",
}


def resolve_url(base: str, media_url: str) -> str:
    """Prepend the cloud base to relative media URLs."""
    if media_url.startswith("/"):
        return base.rstrip("/") + media_url
    return media_url


def guess_content_type(path_or_url: str) -> str:
    import os

    ext = os.path.splitext(path_or_url.split("?")[0])[1].lower()
    return _EXT_FALLBACK.get(ext, "application/octet-stream")


def fetch_bytes(url: str, timeout: float = 30.0) -> Optional[tuple]:
    """Fetch (bytes, content_type) or None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-botschat/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
    except Exception as exc:
        logger.warning(f"[botschat] media fetch failed for {url}: {exc}")
        return None


def upload_to_r2(
    base: str,
    pairing_token: str,
    data: bytes,
    content_type: str,
    filename: str,
    timeout: float = 60.0,
) -> Optional[str]:
    """POST bytes to /api/plugin-upload (X-Pairing-Token); return the R2 URL."""
    import uuid as _uuid

    boundary = f"----hermes{_uuid.uuid4().hex}"
    ext = EXT_BY_MIME.get(content_type, "bin")
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}.{ext}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/plugin-upload",
        data=body,
        method="POST",
        headers={
            "X-Pairing-Token": pairing_token,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        url = payload.get("url") if isinstance(payload, dict) else None
        if not url:
            logger.warning(f"[botschat] plugin-upload response had no url: {payload}")
        return url
    except Exception as exc:
        logger.warning(f"[botschat] R2 upload failed: {exc}")
        return None
