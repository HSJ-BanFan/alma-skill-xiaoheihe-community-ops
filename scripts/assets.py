"""Credential-free platform media downloads and local first-frame previews."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from xhh_sdk.exceptions import XhhConfigError
from network_io import read_http, NetworkDeadline

MAX_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 40_000_000


def platform_url(url):
    value = urllib.parse.urlsplit(url)
    host = value.hostname or ""
    if (value.scheme != "https" or not (host == "max-c.com" or host.endswith(".max-c.com"))
            or value.username or value.password or value.port not in (None, 443)):
        raise XhhConfigError("media source must be an HTTPS Xiaoheihe CDN URL")
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        platform_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def owned(root, path):
    path = Path(path)
    resolved = path.resolve()
    base = root.resolve()
    if sys.platform == "win32":
        def comparable(value):
            value = str(value)
            if value.startswith("\\\\?\\UNC\\"):
                return Path("\\\\" + value[8:])
            return Path(value[4:] if value.startswith("\\\\?\\") else value)
        resolved, base = comparable(resolved), comparable(base)
    if not resolved.is_relative_to(base) or path.is_symlink():
        raise XhhConfigError("asset cache path escapes its root")
    return path


def atomic(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as file:
            file.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch(url, root, *, refresh=False, seconds=20, opener=None):
    platform_url(url)
    root = Path(root) / "assets"
    index = root / (digest(url.encode()) + ".json")
    if index.exists() and not refresh:
        value = json.loads(owned(root, index).read_text(encoding="utf-8"))
        for key in ("source", "preview"):
            file = owned(root, value[key + "_file"])
            if digest(file.read_bytes()) != value[key + "_sha256"]:
                raise XhhConfigError("asset cache digest mismatch")
        return {**value, "cached": True}
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise XhhConfigError("media previews require Pillow; install requirements-media.txt") from None
    request = urllib.request.Request(url, headers={"User-Agent": "xiaoheihe-tools-media/1.0"})
    if opener is None:
        try:
            data = read_http(request, seconds, profile="asset")
        except NetworkDeadline:
            raise XhhConfigError("media download deadline exceeded") from None
    else:
        with opener(request, timeout=seconds) as response:
            data = response.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise XhhConfigError("media source exceeds 16 MiB or is empty")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise XhhConfigError("media image exceeds pixel limit")
            suffixes = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}
            if image.format not in suffixes:
                raise XhhConfigError("unsupported media image format")
            suffix = suffixes[image.format]
            width, height, frames = image.width, image.height, getattr(image, "n_frames", 1)
            image.seek(0)
            preview = ImageOps.exif_transpose(image).convert("RGBA")
            preview.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            preview.info.clear()
            while True:
                stream = io.BytesIO()
                preview.save(stream, format="PNG", optimize=True)
                png = stream.getvalue()
                if len(png) <= 512 * 1024:
                    break
                if max(preview.size) <= 1:
                    raise XhhConfigError("preview size limit")
                preview.thumbnail((max(1, int(preview.width * .8)), max(1, int(preview.height * .8))), Image.Resampling.LANCZOS)
            preview_width, preview_height = preview.size
            preview.close()
    except XhhConfigError:
        raise
    except Exception as error:
        raise XhhConfigError("media cannot be decoded as an image") from error
    folder = owned(root, root / digest(data))
    folder.mkdir(parents=True, exist_ok=True)
    original = owned(root, folder / ("source" + suffix))
    preview_path = owned(root, folder / "preview.png")
    atomic(original, data)
    atomic(preview_path, png)
    value = {"url": url, "source_file": str(original.resolve()), "source_sha256": digest(data), "source_bytes": len(data),
             "preview_file": str(preview_path.resolve()), "preview_sha256": digest(png), "preview_bytes": len(png),
             "width": width, "height": height, "preview_width": preview_width, "preview_height": preview_height,
             "frames": frames, "animated": frames > 1, "preview_scope": "first_frame", "cached": False}
    atomic(owned(root, index), json.dumps(value, ensure_ascii=False).encode())
    return value
