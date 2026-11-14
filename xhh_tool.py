"""Pure Xiaoheihe connection pipeline for Alma.

Zero decision-making, zero text-generation logic.
This file ONLY acts as Alma's hands and eyes:
- fetch_feed(topic_id, offset, limit): fetch raw posts
- fetch_post(link_id): fetch post text, images, and raw comment floors
- send_comment(link_id, text): send a comment to any public post
- send_reply(link_id, root_id, reply_id, text): reply in a thread
- send_favourite(link_id): bookmark/favourite (cy) a post
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

# 工具结果回显的表情和符号超出 GBK，不换掉编码会直接抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

UPSTREAM = Path(r"D:/02_Dev/Workbenches/ReverseEngineering/projects/xiaoheihe-api-collect/cli")
OPS_ROOT = Path(r"D:/02_Dev/Projects/self-media-ops")
JAVA = (OPS_ROOT / ".local/xiaoheihe/setup-cache/runtimes/"
        "bc21a93923103cdaac93ee337b0ae4365e739fde36df823dd456bc67c8a9d352/"
        "jdk-17.0.20.1+1-jre/bin/java.exe")
STORE = Path.home() / ".xhh_sdk"

os.environ.setdefault("XHH_SETUP_HOME", str(OPS_ROOT / ".local/xiaoheihe/setup-cache"))
os.environ.setdefault("XHH_BUNDLE_HOME", str(OPS_ROOT / ".local/xiaoheihe/bundles"))
sys.path.insert(0, str(UPSTREAM))

from xhh_sdk.accounts import AccountStore
from xhh_sdk.client import XhhClient
from xhh_sdk.interaction import _validate_text
from xhh_sdk.exceptions import XhhAPIError, XhhTransportError
from paths import scratch_root, state_root


class _RawClient(XhhClient):
    """Subclass that completely drops the client-side own-post write gate."""
    def _assert_own_post(self, link_id: str) -> dict:
        return {}


class RiskGate(RuntimeError):
    """详情接口连续被风控后，本轮不再发请求。"""


class WriteUncertain(RuntimeError):
    """写操作结果未知：可能已经发出去了。调用方不得重试。"""

    def __init__(self, message: str, *, kind: str = "unknown", http_status=None,
                 platform_status: str = "", platform_message: str = ""):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.platform_status = platform_status
        self.platform_message = platform_message
        self.definitive = False


class WriteRejected(RuntimeError):
    """已识别的平台拒绝。调用过发送接口的意图仍保持关闭。"""

    def __init__(self, message: str, *, kind: str = "api", http_status=None,
                 platform_status: str = "", platform_message: str = ""):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.platform_status = platform_status
        self.platform_message = platform_message
        self.definitive = True


def _redact(message: str, secrets: list[str] | None = None) -> str:
    """把 Cookie 值从错误文本里抹掉再往外抛。"""
    text = str(message or "")
    for secret in secrets or []:
        if secret and len(str(secret)) >= 8:
            text = text.replace(str(secret), "***")
    for part in os.environ.get("XHH_REDACT_COOKIE", "").split(";"):
        value = part.split("=", 1)[-1].strip()
        if len(value) >= 8:
            text = text.replace(value, "***")
    return text


# 平台明确说"没登录"或"被风控"的状态值。
_AUTH_STATUS = {"login", "relogin", "not_login", "no_login"}
_CAPTCHA_STATUS = {"show_captcha", "captcha", "need_captcha"}
_RATE_STATUS = {"rate_limit", "too_many_requests", "429", "10006"}


class WriteJournalError(RuntimeError):
    def __init__(self, *, dispatched: bool = False, receipt: dict | None = None):
        super().__init__("发送意图持久化失败，本轮停止。已调用发送接口的意图不得重发。")
        self.dispatched = dispatched
        self.receipt = receipt or {}
        self.stop = True


def _classify_write_error(exc: Exception, *, action: str = "写操作") -> Exception:
    text = _redact(str(exc))
    status = getattr(exc, "status", "") if type(exc) is XhhAPIError else ""
    status = status if isinstance(status, str) else ""
    recognized = "authentication" if status in _AUTH_STATUS else "captcha" if status in _CAPTCHA_STATUS else \
                 "rate_limit" if status in _RATE_STATUS else ""
    if recognized:
        return WriteRejected("%s被平台拒绝，本轮停止，不重发。%s" % (action, text),
                             kind=recognized, platform_status=status)
    kind = "transport" if isinstance(exc, (XhhTransportError, TimeoutError, ConnectionError)) else "unknown"
    return WriteUncertain("%s结果未知：%s。去平台上核对，不要重试。" % (action, text),
                          kind=kind, platform_status=status)


GATE_FILE = state_root() / "xhh_risk_gate.json"
GATE_FAILS = 2
GATE_WINDOW = 900


def _gate_read() -> dict:
    try:
        return json.loads(GATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _gate_write(state: dict) -> None:
    try:
        GATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        GATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _gate_arm() -> None:
    state = _gate_read()
    fails = int(state.get("fails") or 0)
    last = float(state.get("last") or 0)
    if fails >= GATE_FAILS and time.time() - last < GATE_WINDOW:
        wait = int(GATE_WINDOW - (time.time() - last))
        raise RiskGate(
            "详情接口连续 %d 次返回 show_captcha，已停止读取详情，约 %d 秒后再试。"
            "这一轮直接收尾，不要重试，也不要换其他入口绕过。" % (fails, wait))


def _gate_hit() -> None:
    state = _gate_read()
    state["fails"] = int(state.get("fails") or 0) + 1
    state["last"] = time.time()
    _gate_write(state)


def _gate_ok() -> None:
    if _gate_read().get("fails"):
        _gate_write({"fails": 0, "last": 0})


def _get_client(alias: str | None = None) -> _RawClient:
    if alias is None:
        alias = os.environ.get("XHH_DEFAULT_ACCOUNT", "alt")
    store = AccountStore(STORE)
    cfg = store.get_config(alias)
    cfg.java = str(JAVA)
    return _RawClient(cfg)


_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
# 只认真正的 HTML 标签名。`vector<int>` 这种代码里的尖括号必须留着。
_TAG_RE = re.compile(
    r"</?(?:a|b|i|u|em|strong|p|div|span|img|font|color|size|h[1-6]|"
    r"ul|ol|li|blockquote|code|pre|s|strike|del|ins|table|thead|tbody|"
    r"tr|td|th|hr)(?=[\s/>])[^<>]*>", re.I)
_ZW_RE = re.compile(r"[​-‏  ﻿]")


def _clean_text(text) -> str:
    """平台正文里的锚点、换行和 HTML 实体转成能直接读的纯文本。

    评论常带 `<a href="heybox://...">游戏名</a>`、`<br>` 和 `&quot;`，
    原样进入上下文会让回复跟着写标签。清一遍，只留下人说的话。
    """
    if text is None:
        return ""
    s = str(text)
    s = _ANCHOR_RE.sub(r"\1", s)
    s = _BR_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _ZW_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in s:
        # 多行文本可能是代码或排版，只去行尾空白，保留缩进。
        s = re.sub(r"[ \t]+$", "", s, flags=re.M)
    else:
        s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _fmt_time(value) -> str:
    """Unix 秒或毫秒转成本地 MM-DD HH:MM，读不出来就留空。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    if ts > 1e12:
        ts /= 1000
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _username(value) -> str:
    """replyuser 这类字段有时是名字，有时是整个 user 对象。"""
    if isinstance(value, dict):
        return str(value.get("username") or value.get("nickname") or "")
    return str(value or "")


def fetch_feed(topic_id: str = "7214", offset: int = 0, limit: int = 10) -> list[dict]:
    """Fetch raw post summaries from a topic.

    信息流里的 ``up`` 字段恒为 0，它不是点赞数，别拿它判断热度。这个平台
    的「点赞」动作走 ``/bbs/app/link/like/combo``，实际是充电，计数在
    ``link_award_num``。所以 ``award_num`` 才是点赞/充电数，``like_num``
    和 ``score`` 都映射到它；``comment_num`` 是另一条独立信号。
    """
    client = _get_client()
    data = client.transport.signed_request(
        "/bbs/app/topic/feeds",
        query={"topic_id": str(topic_id), "offset": str(offset),
               "limit": str(limit), "h_src": ""})
    links = (data.get("result") or {}).get("links") or []
    out = []
    for p in links:
        lid = str(p.get("linkid"))
        user = p.get("user") or {}
        name = user.get("username") or user.get("nickname")
        likes = _as_int(p.get("link_award_num"))
        out.append({
            "link_id": lid,
            "author": name,
            "user": {"username": name, "nickname": user.get("nickname")},
            "title": _clean_text(p.get("title")),
            "desc": _clean_text(p.get("description")),
            "comment_num": _as_int(p.get("comment_num")),
            "like_num": likes,
            "score": likes,
            "award_num": likes,
            "topics": [t.get("name") for t in (p.get("topics") or []) if t.get("name")],
            "url": post_url(lid),
        })
    return out


def _link_blocks(link: dict) -> list[tuple[str, str]]:
    """把帖子正文拆成有序的段落和图。结构固定，转写不用模型。"""
    raw = link.get("text") or ""
    if not raw.lstrip().startswith("["):
        # text 是纯文本时它就是正文；description 只有百来字预告，别拿它顶替。
        body = _clean_text(raw)
        desc = _clean_text(link.get("description") or "")
        body = body if body and not body.lower().startswith(("/data/", "/sdcard/")) else desc
        return [("text", body)] if body else []
    try:
        nodes = json.loads(raw)
    except Exception:
        body = _clean_text(link.get("description") or raw)
        return [("text", body)] if body else []
    out: list[tuple[str, str]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        url = n.get("url")
        if url:
            out.append(("image", str(url)))
            continue
        text = _clean_text(n.get("text"))
        if text and not text.lower().startswith(("/data/", "/sdcard/")):
            out.append(("text", text))
    return out


def _link_body(link: dict) -> str:
    """帖子正文。description 只有百来字预告，text 才是全文（块数组）。"""
    raw = link.get("text") or ""
    if raw.lstrip().startswith("["):
        try:
            cleaned = []
            for b in json.loads(raw):
                if not isinstance(b, dict):
                    continue
                t = _clean_text(b.get("text"))
                if t and not t.lower().startswith(("/data/", "/sdcard/")):
                    cleaned.append(t)
            joined = "\n".join(cleaned)
            if joined.strip():
                return joined
        except Exception:
            pass
        # 解析失败时 raw 是一段坏 JSON，读不出话；description 才是能看的预告。
        return _clean_text(link.get("description") or "") or _clean_text(raw)
    body = _clean_text(raw)
    if body and not body.lower().startswith(("/data/", "/sdcard/")):
        return body
    return _clean_text(link.get("description") or "")


def fetch_post(link_id: str) -> dict:
    """Fetch full raw post details including images and top comment floors."""
    client = _get_client()
    _gate_arm()
    try:
        data = client.transport.signed_request(
            "/bbs/app/link/tree",
            query={"link_id": str(link_id), "page": "1", "limit": "15"})
    except Exception as exc:
        if "show_captcha" in str(exc):
            _gate_hit()
        raise
    _gate_ok()
    res = data.get("result") or {}
    link = res.get("link") or {}

    images = []
    blocks = _link_blocks(link)
    images = [u for kind, u in blocks if kind == "image"]
    for t in (link.get("thumbs") or []):
        if t not in images:
            images.append(t)

    comments = []
    for floor in (res.get("comments") or []):
        nodes = floor.get("comment") or []
        if nodes:
            root = nodes[0]
            text = _clean_text(root.get("text"))
            if not text:
                continue
            comments.append({
                "comment_id": str(root.get("commentid")),
                "author": (root.get("user") or {}).get("username"),
                "text": text,
                "up": _as_int(root.get("up")),
                "child_num": _as_int(root.get("child_num")),
                "create_at": root.get("create_at"),
            })

    # 自动下载前几张关键图到本地 scratch，方便直接调 Read 工具肉眼看图。
    # 失败的位置留空串，保证 local_images 与 images 前 N 张一一对应。
    local_images: list[str] = []
    if images:
        import urllib.request as _urllib_req
        scratch_dir = scratch_root()
        scratch_dir.mkdir(parents=True, exist_ok=True)
        for idx, img_url in enumerate(images[:4]):
            ext = os.path.splitext(urllib.parse.urlparse(str(img_url)).path)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                ext = ".jpg"
            dest = scratch_dir / f"xhh_{link_id}_{idx}{ext}"
            try:
                req = _urllib_req.Request(str(img_url), headers={"User-Agent": "Mozilla/5.0"})
                with _urllib_req.urlopen(req, timeout=10) as resp, open(dest, "wb") as f_out:
                    f_out.write(resp.read())
                local_images.append(str(dest))
            except Exception:
                local_images.append("")

    award = _as_int(link.get("link_award_num"))
    return {
        "link_id": str(link_id),
        "author": (link.get("user") or {}).get("username"),
        "title": _clean_text(link.get("title")),
        "desc": _link_body(link),
        "blocks": blocks,
        "images": images,
        "local_images": local_images,
        "comments": comments,
        "comment_num": _as_int(link.get("comment_num")),
        # link.up 恒为 0，不是点赞数；点赞动作就是充电，计数在 link_award_num。
        "up": _as_int(link.get("up")),
        "award_num": award,
        "like_num": award,
        "topics": [t.get("name") for t in (link.get("topics") or []) if isinstance(t, dict) and t.get("name")],
        "create_at": link.get("create_at"),
        "is_favour": link.get("is_favour", 0)
    }


SEEN_FILE = state_root() / "xhh_seen.json"


def load_seen(keep: int = 2000) -> set:
    """Load the link_ids already looked at in previous runs."""
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        ids = data.get("seen") or []
        return set(ids[-keep:])
    except Exception:
        return set()


def save_seen(seen: set, keep: int = 2000) -> None:
    """Persist seen ids (trimmed to the newest `keep`)."""
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    ids = list(seen)[-keep:]
    SEEN_FILE.write_text(json.dumps({"seen": ids, "updated": time.time()},
                                    ensure_ascii=False), encoding="utf-8")


def browse(topic_ids: list[str] | None = None, pages: int = 3,
           per_page: int = 20, seen: set | None = None,
           pause: float = 2.0, remember: bool = True) -> list[dict]:
    """Page through one or more topics and return a deduped, seen-filtered list.

    The feed only ever hands back ~10-16 per request regardless of ``limit``,
    so "covering more posts" means issuing more offset pages, not raising the
    limit. Pagination keeps working well past offset=400 — verified.

    ``seen`` is a set of link_ids already handled. With ``remember=True`` it is
    loaded from and saved back to ``state_root()/xhh_seen.json``, so consecutive
    runs continue where the last one stopped instead of re-walking the same
    top-of-feed posts.

    Read-only; ~2.5s per page, no captcha seen across repeated runs.
    """
    import time as _time
    topics = [str(t) for t in (topic_ids or ["7214"])]
    if seen is None:
        seen = load_seen()
    fresh: list[dict] = []
    for tid in topics:
        for page in range(pages):
            try:
                batch = fetch_feed(tid, offset=page * per_page, limit=per_page)
            except Exception:
                break
            if not batch:
                break
            for p in batch:
                lid = p.get("link_id")
                if not lid or lid in seen:
                    continue
                seen.add(lid)
                p["topic_id"] = tid
                fresh.append(p)
            _time.sleep(pause)
    if remember:
        save_seen(seen)
    return fresh


def post_url(link_id: str) -> str:
    """帖子在网页端的固定地址。平台返回的 share_url 是分享落地页，
    又长又带参数；这里统一给能直接打开的 web 地址，也省一次请求。"""
    return f"https://www.xiaoheihe.cn/bbs/app/link/web/view?link_id={link_id}"


def _flatten_floor(floor: dict) -> list[dict]:
    """All comment nodes in one floor, root first, then sub-replies."""
    return [n for n in (floor.get("comment") or []) if isinstance(n, dict)]


def sweep(topics: dict | list | None = None, per_topic: int = 15,
          pages: int = 1, seen: set | None = None) -> dict:
    """Sweep topics at once and return posts not seen before.

    ``topics`` accepts either a ``{name: topic_id}`` dict or a list of ids.
    Defaults to a spread that matches HSJ-BanFan's actual interests:
    杂谈 / 校园 / PC / 主机 / 游戏开发 / 独立游戏 / Steam / 手游 / 手机游戏.

    ``seen`` is a set of link_ids to skip (pass the same set back in on the
    next call and you will only get genuinely new posts — that is how you keep
    advancing instead of re-reading the same front page).

    Returns ``{"posts": [...], "seen": set, "by_topic": {name: count}}``.
    Every post carries ``url``.
    """
    default_topics = {
        "盒友杂谈": "7214",
        "校园生活": "549999",
        "PC游戏": "1",
        "主机游戏": "23563",
        "游戏开发": "615783",
        "独立游戏开发": "807953",
        "Steam": "475516",
        "手机游戏": "21325",
    }
    if topics is None:
        topics = default_topics
    if isinstance(topics, list):
        topics = {str(t): str(t) for t in topics}

    seen = seen if seen is not None else set()
    collected: list[dict] = []
    by_topic: dict[str, int] = {}

    for name, tid in topics.items():
        got_new = 0
        for page in range(pages):
            try:
                batch = fetch_feed(str(tid), offset=page * per_topic, limit=per_topic)
            except Exception as exc:
                by_topic[name] = -1
                break
            for item in batch:
                lid = item.get("link_id")
                if not lid or lid in seen:
                    continue
                seen.add(lid)
                item["topic"] = name
                item["topic_name"] = name
                collected.append(item)
                got_new += 1
        by_topic[name] = got_new

    return {"posts": collected, "seen": seen, "by_topic": by_topic}


INBOX_SEEN = state_root() / "xhh_inbox_seen.json"


def _load_seen() -> dict:
    try:
        return json.loads(INBOX_SEEN.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_seen(seen: dict) -> None:
    try:
        INBOX_SEEN.parent.mkdir(parents=True, exist_ok=True)
        INBOX_SEEN.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def fetch_messages(limit: int = 20, max_pages: int = 5) -> list[dict]:
    """直接读平台的回复消息队列，而不是靠扫帖子反推。

    ``check_interactions`` 是"翻我自己评论过的帖子，看有没有新楼"——要发
    很多详情请求，还会漏掉我根本没评论过的帖子里对我的回复。

    ``/bbs/app/user/message`` 是平台自己维护的收件箱，一次请求拿到一条
    回复的完整关系：``comment_a_id``（来信）、``root_comment_id``（根楼层）、
    ``link``（哪个帖子）。定位准确、请求量小。

    只读，不改已读状态。
    """
    client = _get_client()
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(max_pages):
        raw = client.transport.signed_request(
            "/bbs/app/user/message",
            query={"list_type": "0", "offset": str(page * limit),
                   "limit": str(limit), "no_more": "false"},
        )
        # 消息在 result.messages 里，不在顶层。读错这一层会安静地永远返回空，
        # 让"读错了"和"真的没消息"长得一模一样。
        batch = (raw.get("result") or {}).get("messages") or []
        if not isinstance(batch, list) or not batch:
            break
        for m in batch:
            if not isinstance(m, dict):
                continue
            # comment_a 是来信（别人说的），comment_b 是我那条（被回复的）。
            incoming = str(m.get("comment_a_id") or "").strip()
            if not incoming or incoming in seen:
                continue
            seen.add(incoming)
            post_id = str(m.get("linkid") or "").strip()
            out.append({
                "comment_id": incoming,
                "root_id": str(m.get("root_comment_id") or "") or None,
                "my_comment_id": str(m.get("comment_b_id") or "") or None,
                "link_id": post_id or None,
                "title": _clean_text(m.get("link_title") or "")[:120],
                "topic": _clean_text(m.get("topic_name") or ""),
                "from": _username(m.get("user_a")),
                # userid_a is the sender; the ledger needs it to tell
                # "someone replied to me" from "I replied to them".
                "author_uid": str(m.get("userid_a") or "").strip(),
                "image_url": str(m.get("comment_img") or "").strip(),
                "text": _clean_text(m.get("comment_a_text") or "")[:400],
                "my_text": _clean_text(m.get("comment_b_text") or "")[:200],
                "created_at": _fmt_time(m.get("timestamp")),
                "created_ts": float(m.get("timestamp") or 0),
                "is_cy": bool(m.get("comment_a_is_cy")),
                "url": post_url(post_id) if post_id else "",
            })
        if len(batch) < limit:
            break
    return out


def check_interactions(page_size: int = 20, max_floors: int = 60,
                       include_cy: bool = False, days: int = 7) -> dict:
    """What happened to ME on 小黑盒 — the account inbox.

    Returns two buckets, each entry already carrying a ``url`` so a report can
    link straight to the post:

    - ``replies_to_my_comments``: someone replied under a comment I wrote
      (楼中楼). Grouped by post.
    - ``comments_on_my_posts``: new top-level comments under posts I authored
      (plus direct replies to me there). Side conversations between other
      people are left out.

    Identity is matched by ``userid`` from the account config, not by
    username — a wrong name used to count my own comments as replies.
    ``is_cy`` / ``is_meaningless`` placeholders and empty text are filtered
    unless ``include_cy=True``. Only interactions newer than ``days`` days
    are kept (``days=None`` keeps everything). Entries are newest first and
    each cid appears in at most one bucket.

    Read-only: this never writes anything.
    """
    import time as _time
    client = _get_client()
    me_id = str(getattr(getattr(client, "config", None), "heybox_id", "") or "")
    cutoff = None if days is None else (_time.time() - days * 86400)

    def _uid(node: dict) -> str:
        return str(node.get("userid")
                   or (node.get("user") or {}).get("userid") or "")

    def _mine(node: dict) -> bool:
        return bool(me_id) and _uid(node) == me_id

    def _ts(node: dict) -> float:
        try:
            return float(node.get("create_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _keep(node: dict) -> bool:
        if _mine(node):
            return False
        if not include_cy and (node.get("is_cy") or node.get("is_meaningless")):
            return False
        if not _clean_text(node.get("text")):
            return False
        if cutoff is not None and _ts(node) and _ts(node) < cutoff:
            return False
        return True

    def _parent_map(nodes: list[dict]) -> tuple[dict[str, str], str]:
        """cid → 父 cid。replyid 缺失或指向本页外时，挂到楼层根上。"""
        by_id = {str(n.get("commentid")): n for n in nodes}
        floor_root = str(nodes[0].get("commentid")) if nodes else ""
        parents: dict[str, str] = {}
        for n in nodes:
            cid = str(n.get("commentid"))
            pid = str(n.get("replyid") or "")
            if pid in ("", "0", "None") or pid == cid or pid not in by_id:
                pid = floor_root if cid != floor_root else ""
            parents[cid] = pid
        return parents, floor_root

    def _under(cid: str, target: str, parents: dict[str, str]) -> bool:
        seen = set()
        cur = parents.get(cid, "")
        while cur and cur not in seen:
            if cur == target:
                return True
            seen.add(cur)
            cur = parents.get(cur, "")
        return False

    buckets: dict[str, dict] = {}

    def _bucket(link_id: str, title: str, url: str) -> dict:
        return buckets.setdefault(link_id, {
            "link_id": link_id, "title": title, "url": url,
            "replies_to_my_comments": [], "comments_on_my_posts": [],
        })

    trees: dict[str, dict | None] = {}

    def _tree(link_id: str) -> dict | None:
        """一次请求 3.4 秒，同一个帖子只拉一次。"""
        if link_id not in trees:
            try:
                trees[link_id] = client.transport.signed_request(
                    "/bbs/app/link/tree",
                    query={"link_id": link_id, "page": "1", "limit": str(max_floors)})
            except Exception:
                trees[link_id] = None
        return trees[link_id]

    claimed: set[str] = set()

    # --- 1. replies under my own comments ---
    for mc in client.my_comments(limit=page_size):
        if not (mc.get("child_num") or 0):
            continue
        link = mc.get("link") or {}
        lid = str(link.get("id") or "")
        if not lid:
            continue
        my_cid = str(mc.get("comment_id") or mc.get("commentid") or "")
        if not my_cid:
            continue
        data = _tree(lid)
        if data is None:
            continue
        for floor in ((data.get("result") or {}).get("comments") or []):
            nodes = _flatten_floor(floor)
            if not any(str(n.get("commentid")) == my_cid for n in nodes):
                continue
            parents, floor_root = _parent_map(nodes)
            by_id = {str(n.get("commentid")): n for n in nodes}
            mine_at = [_ts(n) for n in nodes if _mine(n)]
            try:
                mine_at.append(float(mc.get("create_at") or 0))
            except (TypeError, ValueError):
                pass
            for n in nodes:
                cid = str(n.get("commentid"))
                if not _under(cid, my_cid, parents):
                    continue
                if not _keep(n):
                    continue
                if cid in claimed:
                    continue
                claimed.add(cid)
                parent_id = parents.get(cid, "")
                parent = by_id.get(parent_id) or {}
                b = _bucket(lid, _clean_text(link.get("title")), post_url(lid))
                b["replies_to_my_comments"].append({
                    "comment_id": cid,
                    "root_id": floor_root,
                    "my_comment_id": my_cid,
                    "author": (n.get("user") or {}).get("username"),
                    "text": _clean_text(n.get("text")),
                    "create_at": n.get("create_at"),
                    "created": _fmt_time(n.get("create_at")),
                    "reply_to": _username(n.get("replyuser"))
                                or _username(parent.get("user")),
                    "my_comment": _clean_text(mc.get("text")),
                    "answered": any(t and t > _ts(n) for t in mine_at),
                })

    # --- 2. new comments under my own posts ---
    for p in client.my_posts():
        lid = str(p.get("linkid") or "")
        if not lid or not (p.get("comment_num") or 0):
            continue
        data = _tree(lid)
        if data is None:
            continue
        for floor in ((data.get("result") or {}).get("comments") or []):
            nodes = _flatten_floor(floor)
            if not nodes:
                continue
            parents, floor_root = _parent_map(nodes)
            mine_at = [_ts(n) for n in nodes if _mine(n)]
            for n in nodes:
                cid = str(n.get("commentid"))
                # 顶楼评论，或者直接回我的那条；别人之间的楼中楼不进收件箱
                if cid != floor_root and str(n.get("replyuserid") or "") != me_id:
                    continue
                if not _keep(n):
                    continue
                if cid in claimed:
                    continue
                claimed.add(cid)
                b = _bucket(lid, _clean_text(p.get("title")), post_url(lid))
                b["comments_on_my_posts"].append({
                    "comment_id": cid,
                    "root_id": floor_root,
                    "author": (n.get("user") or {}).get("username"),
                    "text": _clean_text(n.get("text")),
                    "create_at": n.get("create_at"),
                    "created": _fmt_time(n.get("create_at")),
                    "answered": any(t and t > _ts(n) for t in mine_at),
                })

    out = []
    for b in buckets.values():
        if not b["url"]:
            b["url"] = post_url(b["link_id"])
        for key in ("replies_to_my_comments", "comments_on_my_posts"):
            b[key].sort(key=lambda it: float(it.get("create_at") or 0), reverse=True)
        out.append(b)

    # 收件箱没有平台级的已读，只能本地记「这一条我看过了」。
    # 第一次建账本时把当前的都记进去，免得补装那天全被当成新的。
    seen = _load_seen()
    seeded = not seen
    now = int(_time.time())
    new_count = 0
    for b in out:
        for key in ("replies_to_my_comments", "comments_on_my_posts"):
            for it in b[key]:
                cid = it["comment_id"]
                it["seen_at"] = seen.get(cid)
                it["is_new"] = (not seeded) and cid not in seen
                if it["is_new"]:
                    new_count += 1
                seen.setdefault(cid, now)
    _save_seen({k: v for k, v in seen.items() if v >= now - 30 * 86400})

    def _latest(b: dict) -> float:
        vals = []
        for key in ("replies_to_my_comments", "comments_on_my_posts"):
            for it in b[key]:
                try:
                    vals.append(float(it.get("create_at") or 0))
                except (TypeError, ValueError):
                    pass
        return max(vals) if vals else 0.0

    out.sort(key=lambda b: _latest(b), reverse=True)
    return {
        "posts": out,
        "total_replies_to_my_comments": sum(len(b["replies_to_my_comments"]) for b in out),
        "total_comments_on_my_posts": sum(len(b["comments_on_my_posts"]) for b in out),
        "new_since_last_check": new_count,
    }


def _meme_notes(text: str, link_id: str = "") -> list[str]:
    try:
        from community_bindings import PublicKnowledge
        return ["〔词条〕%s：%s" % (card["term"], card["meaning"]) for card in PublicKnowledge().lookup(text)]
    except Exception:
        return ["knowledge_unavailable"]


def read_post(link_id: str, hot: int = 3) -> str:
    """把帖子转写成一段能读的话。段落和图按原顺序走，图片给本地路径。"""
    p = fetch_post(link_id)
    blocks = p.get("blocks") or []
    locals_ = p.get("local_images") or []
    lines = ["《%s》  楼主 %s" % (p.get("title") or "", p.get("author") or "匿名")]
    meta = []
    if p.get("create_at"):
        meta.append(_fmt_time(p.get("create_at")))
    stats = []
    if p.get("up"):
        stats.append("顶 %d" % p["up"])
    if p.get("award_num"):
        stats.append("充电 %d" % p["award_num"])
    stats.append("评论 %d" % _as_int(p.get("comment_num")))
    meta.append(" · ".join(stats))
    if p.get("topics"):
        meta.append("话题 " + " / ".join(p["topics"]))
    if p.get("is_favour"):
        meta.append("已收藏")
    lines.append("  " + " · ".join(x for x in meta if x))
    if not blocks:
        body = (p.get("desc") or "").strip()
        lines.append(body if body else "（没写正文）")
    img_index = 0
    for kind, payload in blocks:
        if kind == "text":
            lines.append(payload)
        else:
            local = locals_[img_index] if img_index < len(locals_) else ""
            img_index += 1
            lines.append("〔第 %d 张图%s〕" % (
                img_index, "：" + local if local else "（没下下来）"))
    if p.get("images") and img_index == 0:
        lines.append("（正文里没有图位置，原帖带 %d 张图）" % len(p["images"]))
    comments = p.get("comments") or []
    if comments:
        lines.append("…… 已有评论（只给摘要，回复前用 read_floor 读全文）……")
        for c in comments[:hot]:
            extra = []
            if c.get("up"):
                extra.append("顶 %d" % c["up"])
            if c.get("child_num"):
                extra.append("%d 条回复" % c["child_num"])
            tag = "（%s）" % " · ".join(extra) if extra else ""
            lines.append("  %s%s〔cid=%s〕：%s" % (
                c.get("author") or "", tag, c.get("comment_id") or "",
                c.get("text") or ""))
    notes = _meme_notes("\n".join(lines), link_id)
    if notes:
        lines.extend(notes)
    lines.append("地址：%s" % post_url(link_id))
    return "\n".join(lines)


def scan_sub_comments(root_id: str, *, pages: int = 3, limit: int = 20,
                      cursor: str | None = None) -> dict:
    """顺着 ``lastval`` 游标往下翻楼中楼，一轮只翻有限页。

    楼中楼是游标分页，不是偏移分页。深楼一次翻到底既慢又容易触发风控，
    而且中间断掉就得从头再来。这里每次只翻 ``pages`` 页，把停在哪返回给
    调用方；下次带上 ``cursor`` 从断点继续，不会重扫前几页。

    返回 ``{"replies": [...], "cursor": 下一页游标或 None, "done": 是否到底}``。
    """
    if not str(root_id or "").strip():
        raise ValueError("缺少 root_id")
    client = _get_client()
    out: list[dict] = []
    seen: set[str] = set()
    next_cursor = cursor
    done = False
    for _ in range(max(1, pages)):
        query = {"root_comment_id": str(root_id), "limit": str(limit)}
        if next_cursor:
            query["lastval"] = str(next_cursor)
        try:
            raw = client.transport.signed_request(
                "/bbs/app/comment/sub/comments", query=query)
        except Exception as exc:
            if "show_captcha" in str(exc):
                _gate_hit()
            raise
        result = raw.get("result") or {}
        # 这一层同样容易读错：sub/comments 的正文在 result.comments 里。
        batch = result.get("comments") or []
        if not isinstance(batch, list) or not batch:
            done = True
            break
        for node in batch:
            if not isinstance(node, dict):
                continue
            cid = str(node.get("commentid") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append({
                "comment_id": cid,
                "root_id": str(node.get("root_comment_id") or root_id),
                "author": _username(node.get("user")),
                "text": _clean_text(node.get("text") or "")[:600],
                "created": _fmt_time(node.get("create_at")),
                "reply_to": _username(node.get("replyuser")),
                "is_cy": bool(node.get("is_cy")),
            })
        lastval = result.get("lastval") or (batch[-1].get("commentid") if batch else None)
        if not lastval or str(lastval) == str(next_cursor):
            done = True
            break
        next_cursor = str(lastval)
        if len(batch) < limit:
            done = True
            break
    if out:
        _gate_ok()
    return {"root_id": str(root_id), "replies": out,
            "cursor": None if done else next_cursor, "done": done}


def read_floor(link_id: str, comment_id: str) -> str:
    """按精确评论 ID 读取本页命中楼层，保留返回文本，不下载图片或更新已读。"""
    if link_id is None or not str(link_id).strip():
        raise ValueError("缺少 link_id")
    if comment_id is None or not str(comment_id).strip():
        raise ValueError("缺少 comment_id")
    link_id = str(link_id)
    comment_id = str(comment_id)
    client = _get_client()
    _gate_arm()
    try:
        floors = list(client.comment_thread(link_id, page=1, limit=50))
    except Exception as exc:
        if "show_captcha" in str(exc):
            _gate_hit()
        raise
    _gate_ok()
    for floor in floors:
        root = floor.get("root") or {}
        nodes = [root] + (floor.get("replies") or [])
        if not any(str(n.get("commentid")) == comment_id for n in nodes):
            continue
        root_id = root.get("commentid")
        if root_id is None or not str(root_id).strip():
            raise ValueError("命中楼层缺少可用 root_id")
        root_id = str(root_id)
        on_page = {str(n.get("commentid")): n for n in nodes}
        lines = [
            "本页返回的楼层上下文 page=1 limit=50，不保证全部祖先或回复。",
            "link_id=%s root_id=%s 目标 cid=%s" % (link_id, root_id, comment_id),
        ]
        for index, n in enumerate(nodes):
            cid = str(n.get("commentid"))
            label = "根评论" if index == 0 else "回复"
            if cid == comment_id:
                label += " · 目标"
            when = _fmt_time(n.get("create_at"))
            up = _as_int(n.get("up"))
            bits = [label, "cid=" + cid]
            if when:
                bits.append(when)
            if up:
                bits.append("顶 %d" % up)
            author = (n.get("user") or {}).get("username") or "匿名"
            head = "〔%s〕%s" % (" · ".join(bits), author)
            if index > 0:
                parent_id = str(n.get("replyid") or "")
                reply_to = _username(n.get("replyuser")) or _username(
                    (on_page.get(parent_id) or {}).get("user"))
                if parent_id and parent_id == root_id:
                    head += "  ↳ 回复根评论"
                elif reply_to:
                    head += "  ↳ 回复 @%s" % reply_to
                if parent_id and parent_id not in on_page:
                    head += "（接的那句不在本页 cid=%s）" % parent_id
            lines.append(head)
            text = _clean_text(n.get("text"))
            lines.append(text if text else "（没有文字）")
            imgs = n.get("imgs")
            if isinstance(imgs, list):
                for url in imgs:
                    if isinstance(url, str) and url:
                        lines.append("图片地址：%s" % url)
        notes = _meme_notes("\n".join(lines), link_id)
        if notes:
            lines.extend(notes)
        lines.append("地址：https://www.xiaoheihe.cn/bbs/app/link/web/view?link_id=%s" % link_id)
        return "\n".join(lines)
    raise ValueError("本页未找到 comment_id=%s 的楼层" % comment_id)


def inbox_brief(days: int = 3, max_lines: int = 30, text_len: int = 48) -> str:
    """收件箱的一页摘要，直接 print 就够看，不会撑爆 stdout。

    优先走平台自己的消息队列（``fetch_messages``）：一次请求就带齐
    「谁回了我哪句、在哪个帖子的哪层楼」，不用为了找新回复去翻一堆帖子详情。
    消息队列读不到时才退回 ``check_interactions`` 的帖子反查。

    每条给出回复要用的 cid / root。预览只用来挑选，回复前仍要
    read_floor 读完整楼层。
    """
    def _preview(text: str, limit: int) -> str:
        text = _clean_text(text).replace("\n", " ")
        return text[:limit] + ("…" if len(text) > limit else "")

    cutoff = time.time() - days * 86400
    try:
        raw = fetch_messages(limit=20, max_pages=3)
    except Exception:
        raw = None

    if raw is not None:
        msgs = [m for m in raw
                if not m.get("is_cy") and (m.get("created_ts") or 0) >= cutoff]
        head = "平台消息队列：%d 条回复（近 %d 天）" % (len(msgs), days)
        if not msgs:
            return head + "\n这段时间没有新回复。"
        lines = [head]
        for i, m in enumerate(msgs):
            if i >= max_lines:
                lines.append("……还有 %d 条没列" % (len(msgs) - max_lines))
                break
            lines.append("【%s】%s" % (m.get("link_id") or "", (m.get("title") or "")[:40]))
            lines.append("  %s %s%s" % (
                m.get("created_at") or "",
                m.get("from") or "?",
                (" · " + m["topic"]) if m.get("topic") else ""))
            lines.append("      「%s」" % _preview(m.get("text") or "", text_len))
            if m.get("my_text"):
                lines.append("      我原话：%s" % _preview(m["my_text"], 32))
            lines.append("      ↳ cid=%s root=%s" % (
                m.get("comment_id") or "", m.get("root_id") or ""))
        pending = pending_intents()
        if pending:
            lines.append("")
            lines.append("⚠ 有 %d 条写操作结果未确认，先上平台核对，别重发：" % len(pending))
            for p in pending[:5]:
                lines.append("   %s %s %s" % (
                    p.get("at_iso") or "", p.get("kind") or "",
                    p.get("link_id") or ""))
        lines.append("摘要仅供筛选，回复前先调 read_floor(link_id, cid) 读完整楼层。")
        return "\n".join(lines)

    # 回退：消息接口不通时的帖子反查。
    data = check_interactions(days=days)
    lines = ["消息队列不可用，已退回帖子反查",
             "新 %d 条 / 共 %d 条回复 · %d 条我帖子下的评论" % (
                 data["new_since_last_check"],
                 data["total_replies_to_my_comments"],
                 data["total_comments_on_my_posts"],
             )]
    shown = 0
    dropped = 0

    for bucket in data["posts"]:
        rows = []
        for label, key in (("回复我", "replies_to_my_comments"),
                           ("我帖下", "comments_on_my_posts")):
            for it in bucket[key]:
                rows.append((label, it))
        if not rows:
            continue
        rows.sort(key=lambda pair: float(pair[1].get("create_at") or 0), reverse=True)
        block = ["【%s】%s" % (bucket["link_id"], (bucket["title"] or "")[:40])]
        for label, it in rows:
            if shown >= max_lines:
                dropped += 1
                continue
            shown += 1
            who = it.get("author") or ""
            if it.get("reply_to"):
                who += " → @%s" % it["reply_to"]
            block.append("  %s %s %s %s %s" % (
                "新" if it.get("is_new") else "  ",
                "已回" if it.get("answered") else "未回",
                label,
                it.get("created") or "时间未知",
                who,
            ))
            block.append("      「%s」" % _preview(it.get("text") or "", text_len))
            tail = "      ↳ cid=%s root=%s" % (
                it.get("comment_id") or "", it.get("root_id") or "")
            if it.get("my_comment"):
                tail += " · 我原话：%s" % _preview(it["my_comment"], 28)
            block.append(tail)
        lines.extend(block)
    if dropped:
        lines.append("……还有 %d 条没列，需要时调 check_interactions" % dropped)
    lines.append("摘要仅供筛选，回复前先调 read_floor(link_id, cid) 读完整楼层。")
    return "\n".join(lines)


TASTE_FILE = state_root() / "xhh_taste.json"


def taste_profile() -> dict | None:
    """Read the taste profile that `xhh_profile.py` leaves behind.

    Returns ``None`` when no profile exists yet (never generated, or the file
    was cleared) — that is a real answer, not an error: fall back to your own
    judgement instead of inventing a preference.

    The profile is a *mirror of what was collected*, not an instruction. Its
    `taste` list tells you the historical share of each category; it does NOT
    say "collect more of this". Treating the percentages as a quota turns
    curation into arithmetic — reading it should only help you recognise
    whether a post sits inside or outside what this account has cared about.
    """
    try:
        return json.loads(TASTE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def taste_hint(title: str) -> dict:
    """Where one post's title lands against the stored taste profile.

    Returns ``{"category": ..., "share": ..., "known": bool}``. ``known`` is
    False when there is no profile or the category never appeared — in that
    case the share is 0.0 and you should not read anything into it.

    Reads only; makes no network call. Use it as one more input before
    favouriting, never as the deciding vote.
    """
    from xhh_profile import classify, RULES  # local import: avoid cycle

    cat = classify(title or "")
    prof = taste_profile()
    if not prof:
        return {"category": cat, "share": 0.0, "known": False}
    for row in prof.get("taste") or []:
        if row.get("category") == cat:
            return {"category": cat, "share": float(row.get("share") or 0.0),
                    "known": True}
    return {"category": cat, "share": 0.0, "known": False}


def send_like(link_id: str) -> dict:
    """Charge / upvote a post (/bbs/app/link/like/combo)."""
    client = _get_client()
    data = client.transport.signed_request(
        "/bbs/app/link/like/combo",
        query={"h_src": ""},
        payload={"link_id": str(link_id)})
    result = data.get("result") or {}
    return {"link_id": str(link_id), "is_max_charge": result.get("is_max_charge"),
            "msg": result.get("msg") or ""}


def send_favourite(link_id: str) -> dict:
    """Bookmark / favourite (cy) a post."""
    client = _get_client()
    return client.favourite(link_id, confirm=True)


def send_unfavourite(link_id: str) -> dict:
    """Unfavourite a post."""
    client = _get_client()
    return client.unfavourite(link_id, confirm=True)


def upload_image(file_path: str) -> dict:
    """把本地图片直接传到小黑盒自家图床，返回可用的 URL。

    这是最省事的一条路 —— 不需要外部图床、不需要额外配额：
      upload/info/v2 → upload/token/v2 → COS PUT → callback/v2

    实测：返回 https://imgheybox.max-c.com/...，发评论后服务端会把它
    转成带缩略图、宽高、mime 的完整图片对象。
    """
    client = _get_client()
    return client.upload(file_path)


def _as_url(image_url) -> str | None:
    """allow passing upload_image()'s dict straight through."""
    if not image_url:
        return None
    if isinstance(image_url, dict):
        return image_url.get("url")
    return image_url


def _prepare_upload(path: str) -> tuple[str, str | None]:
    """把文件弄成图床认的样子，返回 (上传用的路径, 需要删的临时文件)。

    图床按扩展名判断类型：webp 会被当成 application/octet-stream 存成 .obj，
    发出去是一张坏图；扩展名和内容不一致（.jpg 里装 GIF）也会传错类型。
    大于 10MB 的直接拒收，提前说清楚，免得发到一半失败。
    """
    import shutil
    import tempfile

    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError("这张图 %.1fMB，小黑盒图床上限 10MB，换一张或用短码。" % (size / 1048576))

    suffix = os.path.splitext(path)[1].lower()
    try:
        from PIL import Image
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
    except Exception:
        return path, None

    if fmt == "WEBP":
        handle, tmp = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        try:
            with Image.open(path) as im:
                im.convert("RGBA").save(tmp, "PNG")
        except Exception:
            os.unlink(tmp)
            return path, None
        return tmp, tmp

    want = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}.get(fmt)
    if want and suffix != want:
        handle, tmp = tempfile.mkstemp(suffix=want)
        os.close(handle)
        shutil.copyfile(path, tmp)
        return tmp, tmp
    return path, None


# 配图这一关：发顶楼评论前强制看一眼候选，避免"想配才配"变成"永远不配"。
# 分数只用来过滤明显不值得看的候选，不用来判断合不合适——合不合适由读 use_when 的人判断。
GALLERY_SEARCH = "http://127.0.0.1:8899/api/search"
STICKER_FLOOR = 0.42
STICKER_STRONG = 0.55  # 高于它的候选值得驳一次"不配"
STICKER_TOP = 4
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 小黑盒图床硬上限：超过直接报"文件大小不得超过10M"
GATE_LOG = state_root() / "xhh_sticker_gate.log"
GATE_STATE = state_root() / "xhh_sticker_asked.json"
GATE_MAX_INSIST = 2  # 同一条评论最多驳回两次，第三次放行，避免卡死


def _gallery_search(text: str, top: int) -> dict:
    query = urllib.parse.urlencode({"q": text, "top": top})
    with urllib.request.urlopen(f"{GALLERY_SEARCH}?{query}", timeout=15) as response:
        return json.load(response)


def _sticker_candidates(text: str, top: int = STICKER_TOP,
                        floor: float = STICKER_FLOOR) -> list[dict]:
    """问表情库要候选。画廊没在跑就静默跳过，不挡发评论。

    超过图床 10MB 的文件不返回——平台会直接拒收，摆出来也没用。
    向量模型空闲 10 分钟会退出，退出后的第一次检索只按关键词排（mode=tokens），
    分数经常掉到 0.2 上下、过不了门槛。等它醒过来再取一次，别让第一句话吃亏。
    """
    try:
        data = _gallery_search(text, top)
        for _ in range(8):
            if data.get("mode") != "tokens":
                break
            time.sleep(1.5)
            data = _gallery_search(text, top)
        picks = data.get("picks") or []
    except Exception:
        return []
    out = []
    for p in picks:
        if (p.get("score") or 0) < floor:
            continue
        try:
            if os.path.getsize(p.get("file") or "") > MAX_UPLOAD_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def sticker_for(text: str, top: int = 5, floor: float = 0.35) -> str:
    """按一句话找表情包，打印候选和本地路径；只读，不发图。

    定时线程拿不到插件工具时也能用——它走的是表情库的画廊接口，只要
    Alma 开着就有结果。挑中的 file 传给 send_comment(..., image_file=...)。
    """
    picks = _sticker_candidates(text, top=top, floor=floor)
    if not picks:
        line = "库里没有能对上「%s」的图。纯文字发，或者换个说法再找。" % text[:30]
        print(line)
        return ""
    lines = ["「%s」的候选：" % text[:30]]
    for i, p in enumerate(picks, 1):
        lines.append("%d. %s  (%.2f)" % (i, p.get("id"), p.get("score") or 0))
        if p.get("caption"):
            lines.append("   caption: %s" % _clip(p.get("caption")))
        lines.append("   use_when: %s" % (p.get("use_when") or ""))
        lines.append("   avoid_when: %s" % (p.get("avoid_when") or ""))
        lines.append("   file: %s" % (p.get("file") or ""))
    lines.append("分数只说明字面接近；合不合适看 caption 和 use_when。")
    print("\n".join(lines))
    return ""


def _log_gate(record: dict) -> None:
    """每次配图判断留一行，事后能数出真配了几次、拦住几次。"""
    try:
        GATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        record["t"] = round(time.time())
        with GATE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _gate_key(link_id: str, body: str) -> str:
    import hashlib
    return hashlib.sha1(("%s|%s" % (link_id, body)).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 发送意图台账：先落盘，再发请求。
#
# 进程在两次磁盘写之间被杀掉，重启后至少能知道"这条可能已经发出去了"。
# 没有这一步，崩溃就等于丢掉唯一的重发防线。
# ---------------------------------------------------------------------------

INTENT_FILE = state_root() / "xhh_write_intents.json"
_DISPATCH_INTENT: ContextVar[tuple | None] = ContextVar("community_dispatch_intent", default=None)


@contextmanager
def _community_dispatch(state, operation_id: str):
    token = _DISPATCH_INTENT.set((state, operation_id))
    try:
        yield
    finally:
        _DISPATCH_INTENT.reset(token)


def _intent_read() -> dict:
    try:
        data = json.loads(INTENT_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(not isinstance(value, dict) for value in data.values()):
            raise ValueError("invalid_intent_journal")
        return data
    except FileNotFoundError:
        return {}
    except Exception:
        raise WriteJournalError() from None


def _intent_write(state: dict) -> None:
    import tempfile
    temporary = None
    try:
        INTENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".xhh-intent-", dir=INTENT_FILE.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, INTENT_FILE)
    except Exception:
        raise WriteJournalError() from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _intent_begin(kind: str, link_id: str, body: str, extra: dict | None = None) -> str:
    import hashlib
    from community_state import State, StateConflict
    fields = dict(extra or {})
    uid = fields.get("account_uid")
    pid, incoming, root = str(link_id), str(fields.get("reply_id") or ""), str(fields.get("root_id") or "")
    if (kind not in {"comment", "reply"} or not isinstance(uid, str) or not uid.isascii() or not uid.isdigit() or
            not pid.isascii() or not pid.isdigit() or (kind == "reply" and (not incoming.isdigit() or not root.isdigit()))):
        raise WriteJournalError()
    key = hashlib.sha256(f"{uid}|{kind}|{pid}|{incoming}".encode()).hexdigest()
    target = {"kind": kind, "post_id": pid, "incoming_cid": incoming, "root_cid": root}
    try:
        with AccountStore(STORE)._locked():
            scope = _DISPATCH_INTENT.get()
            if scope is not None:
                canonical, operation = scope
                if canonical.uid != uid:
                    raise StateConflict("canonical_account_mismatch")
                State.legacy_send_guard(uid, target, body, root=canonical.path.parent, operation_id=operation)
            else:
                State.legacy_send_guard(uid, target, body, root=INTENT_FILE.parent / "community")
            state = _intent_read()
            for entry in state.values():
                prior_uid = entry.get("account_uid")
                if prior_uid not in (None, ""):
                    if not isinstance(prior_uid, str) or not prior_uid.isascii() or not prior_uid.isdigit():
                        raise WriteJournalError()
                    if prior_uid != uid:
                        continue
                if entry.get("kind") not in {"comment", "reply"} or not str(entry.get("link_id") or "").isdigit():
                    raise WriteJournalError()
                same_post = entry["kind"] == kind and str(entry["link_id"]) == pid
                same_target = kind == "comment" or not entry.get("reply_id") or str(entry["reply_id"]) == incoming
                if same_post and same_target:
                    raise WriteUncertain("已有账号或未归属的目标意图，本次不发送，本轮停止。", kind="prior_closed")
            state[key] = {"kind": kind, "link_id": pid, "len": len(body), "at": time.time(),
                          "at_iso": time.strftime("%m-%d %H:%M", time.localtime()),
                          "status": "uncertain", **fields}
            _intent_write(state)
            return key
    except StateConflict:
        raise WriteUncertain("账号 SQLite 已有关闭或未授权的目标意图，本次不发送。", kind="canonical_closed") from None
    except (WriteUncertain, WriteJournalError):
        raise
    except Exception:
        raise WriteJournalError() from None


def _intent_settle(key: str, status: str, **fields) -> None:
    with AccountStore(STORE)._locked():
        state = _intent_read()
        if key not in state:
            raise WriteJournalError(dispatched=True)
        entry = state[key]
        if entry.get("status") == "sent":
            return
        entry.update(**fields)
        entry["status"] = status
        entry["settled_at"] = time.time()
        _intent_write(state)


def pending_intents() -> list[dict]:
    """给上层看的"还没确认结果"的写操作清单。"""
    return [{"key": k, **v} for k, v in _intent_read().items()
            if v.get("status") == "uncertain"]


def _gate_state() -> dict:
    try:
        raw = json.loads(GATE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, dict):
            out[k] = v
        else:  # 旧格式直接存的时间戳
            out[k] = {"n": 0, "t": float(v or 0)}
    return out


def _gate_bump(key: str) -> int:
    state = _gate_state()
    now = time.time()
    state = {k: v for k, v in state.items()
             if now - float((v or {}).get("t") or 0) < 7200}
    entry = state.get(key) or {}
    entry["n"] = int(entry.get("n") or 0) + 1
    entry["t"] = now
    state[key] = entry
    try:
        GATE_STATE.parent.mkdir(parents=True, exist_ok=True)
        GATE_STATE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return entry["n"]


def _clip(text, limit: int = 110) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + "…" if len(text) > limit else text


def _format_picks(picks: list[dict]) -> str:
    return "\n".join(
        f"  {i}. {p.get('id')}\n     caption: {_clip(p.get('caption'))}"
        f"\n     use_when: {p.get('use_when') or ''}"
        f"\n     avoid_when: {p.get('avoid_when') or ''}\n     file: {p.get('file') or ''}"
        for i, p in enumerate(picks, 1))


def _guard_sticker(body: str, link_id: str, skip: bool, why_not: str = "") -> None:
    """顶楼评论发之前过一遍配图。跳过必须给出理由，不能靠重发把这一关撞过去。"""
    picks = _sticker_candidates(body)
    key = _gate_key(link_id, body)
    best = max((p.get("score") or 0) for p in picks) if picks else 0.0
    if skip:
        if why_not:
            _log_gate({"ev": "declined", "len": len(body), "best": round(best, 3),
                       "why_not": why_not[:120]})
            return
        strong = [p for p in picks if (p.get("score") or 0) >= STICKER_STRONG]
        if not strong:
            _log_gate({"ev": "declined", "len": len(body), "best": round(best, 3)})
            return
        n = _gate_bump(key)
        if n > GATE_MAX_INSIST:
            _log_gate({"ev": "forced", "len": len(body), "best": round(best, 3), "n": n})
            return
        _log_gate({"ev": "insist", "len": len(body), "best": round(best, 3), "n": n})
        raise ValueError(
            "还没决定配哪张。库里这几张和你这句话沾边：\n"
            + _format_picks(strong)
            + "\n\n只要有一张方向和语气不打架，就挑它配上，不用等到最贴切；"
            "确实都不合适（安慰、严肃求助，或 avoid_when 命中）才重发 no_sticker=True，"
            "并加 why_not='各自的哪里不合适'。\n"
            "不要把这次调用包进 try/except 自动重发——那等于没看。")
    if not picks:
        return
    _log_gate({"ev": "ask", "len": len(body),
               "scores": [round(p.get("score") or 0, 3) for p in picks]})
    raise ValueError(
        "配不配图还没决定。库里这几张和你这句话沾边，读 caption 和 use_when 挑一张：\n"
        + _format_picks(picks)
        + "\n\n方向上不打架就配上，不用等到最贴切；只有安慰、严肃求助或 avoid_when 命中时才跳过。"
        "\n要跳就传 no_sticker=True 加 why_not='各自的哪里不合适' 重发。"
        "\n（分数只说明字面接近，不说明合不合适。）")


EMOJI_WHITELIST = state_root() / "xhh_emoji_whitelist.json"
_EMOJI_GROUPS = ("cube_", "heygirl_", "grandemoji_", "bigemoji_")


def _known_shortcodes() -> set:
    try:
        data = json.loads(EMOJI_WHITELIST.read_text(encoding="utf-8"))
        return {str(e.get("shortcode")) for e in (data.get("usable") or []) if e.get("shortcode")}
    except Exception:
        return set()


def _check_shortcodes(body: str) -> None:
    """短码拼错不会报错，只会原样显示成方括号文字。发送前拦下来。"""
    known = _known_shortcodes()
    if not known:
        return
    import re
    for match in re.finditer(r"\[([a-z]+_[^\]]+)\]", body):
        code = match.group(0)
        if match.group(1).startswith(_EMOJI_GROUPS) and code not in known:
            raise ValueError(
                "短码 %s 不在可用列表里，发出去只会显示成一段方括号文字。"
                "对照 REFERENCE.md 里的 61 个短码，或者去掉。" % code)


_CODE_RE = re.compile(r"\[[^\[\]]{1,40}\]")


def _shortcode_count(text: str) -> int:
    """只数真正的短码，别把正文里的 [图片] 之类当表情；仅用于回执，不做门槛。"""
    matches = _CODE_RE.findall(text or "")
    known = _known_shortcodes()
    if not known:
        return len(matches)
    return sum(1 for code in matches if code in known)


def send_comment(link_id: str, text: str, image_url=None, image_file=None,
                 no_sticker: bool = False, why_not: str = "") -> dict:
    """Send a comment to any public post.

    ``image_url`` may be ANY publicly reachable URL — verified that xiaoheihe
    accepts foreign CDN hosts in the ``imgs`` field (tested with a
    CloudFlare-ImgBed URL, read back intact).

    ``image_file`` is a local sticker path; it gets uploaded first, so one
    call does search result → posted image.

      ``no_sticker=True`` means the sticker question was answered and nothing
    fit; ``why_not`` says why, and is what lets the comment through the gate.

    短码数量不设门槛，配不配图都可以带；该用几个按语气判断。
    """
    client = _get_client()
    body = _validate_text(text)
    _check_shortcodes(body)
    if body.strip().lower() in {"测试", "测试一下", "test", "测试评论", "1", "test message"}:
        raise ValueError("这看着是测试内容，不要发到公开帖上。试配图用 sticker_for()，不要用 send_comment。")
    has_image = image_url is not None or image_file is not None
    if not has_image:
        _guard_sticker(body, link_id, skip=no_sticker, why_not=why_not)
    key = _intent_begin("comment", link_id, body,
                        {"account_uid": str(client.config.heybox_id), "image": has_image, "root_id": "", "reply_id": ""})
    if image_file and image_url is None:
        up_path, tmp = _prepare_upload(image_file)
        try:
            uploaded = client.upload(up_path)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        image_url = uploaded.get("url") if isinstance(uploaded, dict) else uploaded
        if not image_url:
            raise ValueError(f"图没传上去，检查路径：{image_file}")
    image_url = _as_url(image_url)
    try:
        data = client.transport.signed_request(
            "/bbs/app/comment/create",
            query={"h_src": ""},
            payload={"link_id": str(link_id), "text": body,
                     "imgs": image_url or "", "is_cy": "0"})
    except Exception as exc:
        classified = _classify_write_error(exc, action="发评论")
        if getattr(classified, "definitive", False):
            try:
                _intent_settle(key, "rejected", error=str(classified)[:300])
            except Exception:
                raise WriteJournalError(dispatched=True) from None
        raise classified from exc
    cid = client._created_comment_id(data) or None
    if cid is None:
        # 平台回了 ok 但没给评论 ID：当作结果未知，避免上层以为已经发成功再补发一条。
        raise WriteUncertain(
            "平台接受了请求但没返回评论 ID，无法确认是否真的发出去了。"
            "去帖子下面核对，不要重发。", kind="no_receipt")
    receipt = {"comment_id": cid, "link_id": str(link_id), "text": body,
               "image_url": image_url, "shortcodes": _shortcode_count(body)}
    try:
        _intent_settle(key, "sent", comment_id=str(cid))
    except Exception:
        raise WriteJournalError(dispatched=True, receipt=receipt) from None
    if image_url or no_sticker:
        _log_gate({"ev": "sent", "image": bool(image_url), "len": len(body),
                   "file": os.path.basename(image_file) if image_file else ""})
    return receipt


def send_reply(link_id: str, root_id: str, reply_id: str, text: str,
               image_url=None) -> dict:
    """Reply in a comment thread (optionally with an image URL)."""
    client = _get_client()
    body = _validate_text(text)
    _check_shortcodes(body)
    image_url = _as_url(image_url)
    key = _intent_begin("reply", link_id, body,
                        {"account_uid": str(client.config.heybox_id), "root_id": str(root_id), "reply_id": str(reply_id)})
    try:
        data = client.transport.signed_request(
            "/bbs/app/comment/create",
            query={"h_src": ""},
            payload={"link_id": str(link_id), "text": body,
                     "imgs": image_url or "",
                     "root_id": str(root_id), "reply_id": str(reply_id), "is_cy": "0"})
    except Exception as exc:
        classified = _classify_write_error(exc, action="发楼中楼回复")
        if getattr(classified, "definitive", False):
            try:
                _intent_settle(key, "rejected", error=str(classified)[:300])
            except Exception:
                raise WriteJournalError(dispatched=True) from None
        raise classified from exc
    cid = client._created_comment_id(data) or None
    if cid is None:
        raise WriteUncertain(
            "平台接受了回复但没返回评论 ID，无法确认是否真的发出去了。"
            "去楼层下面核对，不要重发。", kind="no_receipt")
    receipt = {"comment_id": cid, "link_id": str(link_id), "root_id": str(root_id),
               "text": body, "image_url": image_url, "shortcodes": _shortcode_count(body)}
    try:
        _intent_settle(key, "sent", comment_id=str(cid))
    except Exception:
        raise WriteJournalError(dispatched=True, receipt=receipt) from None
    return receipt


def delete_comment(comment_id: str) -> dict:
    """Delete an own comment directly without client-side ownership restrictions."""
    client = _get_client()
    try:
        return client.transport.signed_request(
            "/bbs/app/comment/delete",
            payload={"comment_id": str(comment_id)})
    except Exception as exc:
        raise _classify_write_error(exc, action="删评论") from exc
