"""CloudFlare-ImgBed 图床客户端 —— 纯管道，只做上传/列目录。

配置放在 ~/.config/alma/imgbed.json:
    {"base": "https://xxx.pages.dev", "token": "imgbed_xxx"}

已验证：
- 上传端点 POST {base}/upload，multipart 字段名 file
- 认证 header: Authorization: Bearer {token}
- 返回：[{"src": "/file/xxx.png"}]  —— 相对路径，要拼 base
- /api/manage/list 可列出全部文件
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from paths import home_config

CONFIG_PATH = home_config() / "imgbed.json"


def _config() -> dict:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"图床配置不存在: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("base") or not cfg.get("token"):
        raise RuntimeError("图床配置缺少 base 或 token")
    return cfg


def _full_url(base: str, src: str) -> str:
    if src.startswith("http://") or src.startswith("https://"):
        return src
    return base.rstrip("/") + "/" + src.lstrip("/")


def upload(path: str | Path, *, timeout: int = 60) -> dict:
    """上传一个文件，返回 {src, url, bytes, file}。

    ``url`` 是可直接发给任何平台的完整地址。
    """
    cfg = _config()
    base, token = cfg["base"], cfg["token"]
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    data = path.read_bytes()
    boundary = "----AlmaImgBed" + uuid.uuid4().hex
    mime = _guess_mime(path)

    body = b""
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="file"; '
             f'filename="{path.name}"\r\n').encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        base.rstrip("/") + "/upload",
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
        })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(400).decode("utf-8", "replace")
        raise RuntimeError(f"上传失败 HTTP {exc.code}: {detail}") from exc

    # 返回是数组，取第一个的 src
    if isinstance(payload, list) and payload:
        src = payload[0].get("src") or ""
    elif isinstance(payload, dict):
        src = payload.get("src") or ""
    else:
        raise RuntimeError(f"无法解析上传响应: {payload}")

    if not src:
        raise RuntimeError(f"上传响应没有 src: {payload}")

    return {"src": src, "url": _full_url(base, src),
            "bytes": len(data), "file": str(path)}


def list_files(*, timeout: int = 30) -> list[dict]:
    """列出图床上的全部文件（只读）。"""
    cfg = _config()
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/api/manage/list",
        headers={"Authorization": f"Bearer {cfg['token']}",
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    return payload.get("files") or []


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".mp4": "video/mp4",
    }.get(ext, "application/octet-stream")
