"""Wire/account boundary around existing SDK and unchanged working sends."""
from __future__ import annotations

import hashlib
import html
import io
import uuid
import importlib.util
import json
import os
import re
import sys
import subprocess
import math
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from community_state import PreparedImage, REACTIONS, RunLimit, StateConflict, valid_reaction_ack
from paths import scratch_root

IMAGE_SOURCE_BYTES = 10 * 1024 * 1024
IMAGE_PIXEL_LIMIT = 32_000_000
PREVIEW_EDGE = 1600
PREVIEW_BYTES = 320 * 1024


@dataclass(frozen=True)
class Account:
    uid: str
    alias: str


class ReadProblem(RuntimeError):
    def __init__(self, code: str, scope: str = ""):
        self.code, self.scope = code, scope
        super().__init__(code)


class PreflightRefusal(RuntimeError):
    def __init__(self, data: dict):
        self.data = data
        super().__init__(data["code"])


class DispatchProblem(RuntimeError):
    def __init__(self, data: dict, receipt: dict | None = None):
        self.data = data
        self.receipt = receipt
        super().__init__(data["code"])


def id_value(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdigit():
            return str(value)
    return ""


def plain(text) -> str:
    value = html.unescape(str(text or ""))
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    return re.sub(r"</?(?:a|b|i|u|em|strong|p|div|span|img|font|h[1-6]|ul|ol|li|blockquote|pre|code)(?=[\s/>])[^<>]*>", "", value, flags=re.I).strip()


def images(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("https://", "http://")) else []
    if isinstance(value, dict):
        return images(value.get("url") or value.get("img") or "")
    if isinstance(value, list):
        return [url for item in value for url in images(item)]
    return []


def normalize_node(raw: dict, post_id: str, root_id: str, clean=plain) -> dict:
    if not isinstance(raw, dict) or not id_value(raw, "commentid", "comment_id", "id"):
        raise ReadProblem("invalid_comment_node", post_id)
    user = raw.get("user")
    if not isinstance(user, dict):
        user = {}
    return {"cid": id_value(raw, "commentid", "comment_id", "id"), "post_id": post_id, "root_id": root_id,
            "author_uid": id_value(raw, "userid", "user_id") or id_value(user, "userid", "user_id", "heybox_id"),
            "author_name": str(user.get("username") or user.get("nickname") or raw.get("username") or ""),
            "reply_id": id_value(raw, "replyid", "reply_id"), "reply_uid": id_value(raw, "replyuserid", "reply_user_id"),
            "text": clean(raw.get("text")), "created_at": raw.get("create_at") or raw.get("created_at") or 0,
            "images": images(raw.get("imgs")), "child_num": int(raw.get("child_num") or 0)}


def profile_records(rows: list, uid: str, clean=plain) -> list[dict]:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ReadProblem("invalid_own_comment", "own_comments")
        link = row.get("link")
        if not isinstance(link, dict):
            link = {}
        user = row.get("user")
        if not isinstance(user, dict):
            user = {}
        explicit = {value for value in (id_value(row, "userid", "user_id"),
                    id_value(user, "userid", "user_id", "heybox_id")) if value}
        if explicit - {uid}:
            raise ReadProblem("profile_author_mismatch", "own_comments")
        pid = id_value(link, "id", "linkid", "link_id") or id_value(row, "link_id", "linkid")
        cid = id_value(row, "comment_id", "commentid", "id")
        if not pid or not cid:
            raise ReadProblem("invalid_own_comment", "own_comments")
        root = id_value(row, "root_comment_id", "root_id")
        kind = row.get("comment_type")
        if kind is not None and (isinstance(kind, bool) or not isinstance(kind, (str, int)) or not str(kind).isdigit()):
            raise ReadProblem("invalid_profile_comment_type", "own_comments")
        if root == "0":
            root = ""
        # Captured own-profile rows prove kind 2 is a root; kind 1 is a child.
        if not root and str(kind) == "2":
            root = cid
        normalized = normalize_node(dict(row, commentid=cid, userid=uid), pid, root, clean)
        out.append({"type": "node", "data": normalized})
    return out


def parse_sticker_refusal(message: str) -> dict | None:
    if not message.startswith(("配不配图还没决定。", "还没决定配哪张。")):
        return None
    # Only asset metadata is retained; legacy wording after the list is not returned.
    picks = []
    pattern = r"(?m)^\s+(\d+)\. ([^\n]+)\n\s+caption: ([^\n]*)\n\s+use_when: ([^\n]*)\n\s+avoid_when: ([^\n]*)\n\s+file: ([^\n]*)"
    for match in re.finditer(pattern, message):
        picks.append(dict(zip(("index", "id", "caption", "use_when", "avoid_when", "file"), match.groups())))
    return {"code": "sticker_selection_pending", "dispatched": False, "candidates": picks}


def error_data(error: Exception, scope: str = "") -> dict:
    if isinstance(error, RunLimit):
        result = {"code": error.code, "scope": scope}
        if error.retry_after is not None:
            result["retry_after"] = round(error.retry_after, 3)
        return result
    if isinstance(error, StateConflict):
        return {"code": str(error), "scope": scope}
    if isinstance(error, ReadProblem):
        return {"code": error.code, "scope": error.scope or scope}
    if isinstance(error, (PreflightRefusal, DispatchProblem)):
        return dict(error.data, scope=scope)
    name = type(error).__name__
    code = "captcha_stop" if name == "RiskGate" or "show_captcha" in str(error) else \
           "session_expired" if name == "XhhAuthError" else "rate_limited" if name == "XhhRateLimitError" else \
           "timeout" if isinstance(error, TimeoutError) else "read_failed"
    return {"code": code, "scope": scope, "type": name}


def reaction_receipt(kind: str, post_id: str, uid: str, raw, *, historical: bool = False) -> dict:
    if not isinstance(raw, dict) or id_value(raw, "link_id") != post_id:
        raise ReadProblem("invalid_reaction_ack", post_id)
    if kind == "like" and set(raw) == {"link_id", "is_max_charge", "msg"} and isinstance(raw.get("msg"), str):
        facts = {"is_max_charge": raw["is_max_charge"]}
    elif (kind == "favourite" and set(raw) == {"link_id", "favour_type", "folder_id", "raw"}
            and raw.get("favour_type") == "1" and raw.get("folder_id") is None and isinstance(raw.get("raw"), dict)):
        facts = {"favour_type": "1", "default_folder": True, "api_status": raw["raw"].get("status")}
    else:
        raise ReadProblem("invalid_reaction_ack", post_id)
    ack = {"kind": kind, "post_id": post_id, "account_uid": uid,
           "source": "historical_helper_stdout" if historical else "original_helper_return",
           "account_proof": "event_binding" if historical else "configured_binding", "target_proof": "request", "facts": facts}
    if not valid_reaction_ack(ack, kind, post_id, uid):
        raise ReadProblem("invalid_reaction_ack", post_id)
    return {"reaction_ack": ack}


class PublicKnowledge:
    def __init__(self, entry: Path | None = None, runner=None):
        self.entry = entry or Path("D:/02_Dev/Projects/alma-meme-watcher/scripts/lookup.mjs")
        self.runner = runner or subprocess.run

    def lookup(self, text: str, limit: int = 3) -> list[dict]:
        try:
            completed = self.runner(["node", str(self.entry), "--text=" + text, "--limit", str(limit)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    encoding="utf-8", errors="strict", timeout=5, shell=False)
            if completed.returncode != 0 or completed.stderr or not completed.stdout:
                raise ReadProblem("knowledge_unavailable")
            result = json.loads(completed.stdout)
            if not isinstance(result, dict) or set(result) != {"terms"} or not isinstance(result["terms"], list) or len(result["terms"]) > limit:
                raise ReadProblem("knowledge_unavailable")
            strings = {"term", "meaning", "usage_context", "avoid_when", "origin", "source_url"}
            fields = strings | {"aliases", "matched_keys", "confidence", "days_left"}
            for card in result["terms"]:
                if (not isinstance(card, dict) or set(card) != fields or any(not isinstance(card[key], str) for key in strings) or
                        any(not isinstance(card[key], list) or any(not isinstance(value, str) for value in card[key])
                            for key in ("aliases", "matched_keys")) or
                        type(card["confidence"]) not in {int, float} or not math.isfinite(card["confidence"]) or
                        (card["days_left"] is not None and (type(card["days_left"]) is not int or card["days_left"] <= 0))):
                    raise ReadProblem("knowledge_unavailable")
            return result["terms"]
        except ReadProblem:
            raise
        except Exception as error:
            raise ReadProblem("knowledge_unavailable") from error


class SDKBackend:
    """Inject a client/helpers for tests; no SDK is opened by importing this module."""
    def __init__(self, client, alias: str = "alt", helpers=None):
        self.client, self.helpers = client, helpers
        self.account = Account(str(client.config.heybox_id), alias)
        self.clean = helpers._clean_text if helpers is not None else plain
        self.store = helpers.AccountStore(helpers.STORE) if helpers is not None else None
        self.account_revision = self.store.get(alias).revision if self.store is not None else None
        self.image_root = scratch_root() / "community-assets-issued"

    @contextmanager
    def bound_account(self):
        if self.store is None:
            yield
            return
        with ExitStack() as stack:
            try:
                stack.enter_context(self.store._locked())
                current = self.store.get(self.account.alias)
            except Exception:
                raise PreflightRefusal({"code": "account_binding_unavailable", "dispatched": False}) from None
            if (os.environ.get("XHH_DEFAULT_ACCOUNT") != self.account.alias or
                    current.revision != self.account_revision or current.identity != self.account.uid or
                    current.pkey != self.client.config.pkey):
                raise PreflightRefusal({"code": "account_binding_changed", "dispatched": False})
            # SDK-managed changes share this reentrant mutex, including the old sender's get_config.
            yield

    @contextmanager
    def dispatch_intent(self, state, operation_id: str):
        manager = getattr(self.helpers, "_community_dispatch", None)
        if manager is None:
            yield
        else:
            with manager(state, operation_id):
                yield

    def _write_failure(self, error: Exception):
        journal = getattr(self.helpers, "WriteJournalError", None)
        if isinstance(journal, type) and isinstance(error, journal):
            data = {"code": "intent_persistence_failed", "type": type(error).__name__, "stop": True,
                    "dispatched": error.dispatched}
            if error.dispatched is False:
                raise PreflightRefusal(data) from None
            raise DispatchProblem(data, error.receipt) from None
        rejected = getattr(self.helpers, "WriteRejected", None)
        uncertain = getattr(self.helpers, "WriteUncertain", None)
        if isinstance(rejected, type) and isinstance(error, rejected):
            raise DispatchProblem({"code": "platform_rejected", "type": type(error).__name__,
                                   "stop": True, "dispatched": True}) from None
        if isinstance(uncertain, type) and isinstance(error, uncertain):
            raise DispatchProblem({"code": "dispatch_uncertain", "type": type(error).__name__, "stop": True,
                                   "dispatched": error.kind not in {"prior_closed", "canonical_closed"}}) from None
        raise error

    def configured_account(self) -> Account:
        return self.account

    def verify_account(self) -> Account:
        with self.bound_account():
            pass
        data = self.client.account_info()
        if not isinstance(data, dict):
            raise ReadProblem("identity_not_verified", "account")
        identities = set()
        for container in (data, data.get("user"), data.get("account_detail")):
            if isinstance(container, dict):
                for key in ("heybox_id", "userid", "user_id", "id"):
                    value = id_value(container, key)
                    if value:
                        identities.add(value)
        if identities != {self.account.uid}:
            raise ReadProblem("identity_mismatch", "account")
        return self.account

    def own_comments(self, offset: int, limit: int) -> list:
        rows = self.client.my_comments(offset=offset, limit=limit, only_cy="0")
        if not isinstance(rows, list):
            raise ReadProblem("invalid_profile_shape", "own_comments")
        return rows

    def own_posts(self, offset: int, limit: int) -> list:
        rows = self.client.my_posts(offset=offset, limit=limit)
        if not isinstance(rows, list):
            raise ReadProblem("invalid_profile_shape", "own_posts")
        return rows

    def own_post_records(self, rows: list) -> list[dict]:
        records = []
        for row in rows:
            if not isinstance(row, dict):
                raise ReadProblem("invalid_own_post", "own_posts")
            pid = id_value(row, "linkid", "link_id", "id")
            if not pid:
                raise ReadProblem("invalid_own_post", "own_posts")
            user = row.get("user")
            if not isinstance(user, dict):
                user = {}
            explicit = {value for value in (id_value(row, "userid", "user_id"),
                        id_value(user, "userid", "user_id", "heybox_id")) if value}
            if explicit - {self.account.uid}:
                raise ReadProblem("profile_author_mismatch", "own_posts")
            records.append({"type": "own_post", "data": {"post_id": pid, "title": self.clean(row.get("title")),
                            "body": self.clean(row.get("description")), "author_uid": self.account.uid,
                            "author_name": "", "owned": True}})
        return records

    def _guarded(self, function):
        if self.helpers is not None:
            self.helpers._gate_arm()
        try:
            result = function()
        except Exception as error:
            if self.helpers is not None and "show_captcha" in str(error):
                self.helpers._gate_hit()
            raise
        if self.helpers is not None:
            self.helpers._gate_ok()
        return result

    def detail(self, post_id: str, page: int = 1, limit: int = 50) -> dict:
        data = self._guarded(lambda: self.client.read_post(post_id, page=page, limit=limit))
        if not isinstance(data, dict) or data.get("status") not in (None, "ok"):
            raise ReadProblem("invalid_detail_envelope", post_id)
        result = data.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("link"), dict) or not result["link"]:
            raise ReadProblem("invalid_detail_shape", post_id)
        link = result["link"]
        actual = id_value(link, "linkid", "link_id", "id")
        if actual and actual != post_id:
            raise ReadProblem("post_identity_mismatch", post_id)
        floors = result.get("comments")
        if not isinstance(floors, list):
            raise ReadProblem("missing_comment_page", f"{post_id}/page{page}")
        nodes, warnings, truncated_roots = [], [], []
        for floor in floors:
            raw_nodes = floor.get("comment") if isinstance(floor, dict) else None
            if not isinstance(raw_nodes, list) or not raw_nodes:
                warnings.append({"code": "invalid_floor", "scope": f"{post_id}/page{page}"})
                continue
            root = id_value(raw_nodes[0], "commentid", "comment_id", "id")
            for raw in raw_nodes:
                try:
                    nodes.append(normalize_node(raw, post_id, root, self.clean))
                except ReadProblem as error:
                    warnings.append(error_data(error))
            declared = int(raw_nodes[0].get("child_num") or 0)
            if declared > len(raw_nodes) - 1:
                truncated_roots.append(root)
        user = link.get("user") if isinstance(link.get("user"), dict) else {}
        blocks = self.helpers._link_blocks(link) if self.helpers is not None else [("text", self.clean(link.get("text") or link.get("description")))]
        body = "\n".join(value for kind, value in blocks if kind == "text")
        urls = [value for kind, value in blocks if kind == "image"]
        urls = list(dict.fromkeys(urls + images(link.get("thumbs"))))
        post = {"post_id": post_id, "ref": "p:" + post_id, "title": self.clean(link.get("title")), "body": body,
                "author_uid": id_value(user, "userid", "user_id", "heybox_id"),
                "author_name": str(user.get("username") or user.get("nickname") or ""),
                "blocks": [{"type": kind, "value": value} for kind, value in blocks],
                "assets": [{"url": url, "local_file": ""} for url in urls],
                "award_num": int(link.get("link_award_num") or 0), "comment_num": int(link.get("comment_num") or 0),
                "created_at": link.get("create_at") or 0}
        return {"post": post, "nodes": nodes, "warnings": warnings, "truncated_roots": truncated_roots,
                "floor_count": len(floors), "scope": f"{post_id}/page{page}", "page": page}

    def sub_comments(self, post_id: str, root_cid: str, lastval: str | None = None) -> dict:
        result = self._guarded(lambda: self.client.sub_comments(root_cid, lastval=lastval, hide_cy="0"))
        if not isinstance(result, dict):
            raise ReadProblem("invalid_subcomment_shape", root_cid)
        rows = result.get("comments")
        if not isinstance(rows, list):
            raise ReadProblem("missing_subcomment_page", root_cid)
        nodes = [normalize_node(row, post_id, root_cid, self.clean) for row in rows]
        terminal = result.get("has_more") is False
        cursor = result.get("lastval")
        return {"nodes": nodes, "terminal_proved": terminal, "next_cursor": str(cursor) if cursor else None,
                "scope": f"{post_id}/root{root_cid}/sub:{lastval or 'first'}"}

    def feed(self, topic_id: str, offset: int, limit: int) -> list:
        if self.helpers is None:
            return []
        return self.helpers.fetch_feed(topic_id, offset=offset, limit=limit)

    def asset_files(self, post: dict) -> dict:
        return post

    def prepare_image(self, source_ref: str) -> PreparedImage:
        from PIL import Image, ImageOps
        try:
            if source_ref.startswith(("http://", "https://")):
                if not platform_image_source(source_ref):
                    raise ReadProblem("asset_source_outside_platform")
                request = urllib.request.Request(source_ref, headers={"User-Agent": "local-community-reader/1.0"})
                with urllib.request.build_opener(PlatformImageRedirect()).open(request, timeout=8) as response:
                    data = response.read(IMAGE_SOURCE_BYTES + 1)
            else:
                with Path(source_ref).open("rb") as source:
                    data = source.read(IMAGE_SOURCE_BYTES + 1)
            if not data or len(data) > IMAGE_SOURCE_BYTES:
                raise ReadProblem("image_source_size_limit")
            with Image.open(io.BytesIO(data)) as original:
                formats = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"),
                           "GIF": (".gif", "image/gif"), "WEBP": (".webp", "image/webp")}
                if original.format not in formats or original.width * original.height > IMAGE_PIXEL_LIMIT:
                    raise ReadProblem("image_decode_limit")
                suffix, mime = formats[original.format]
                animated = bool(getattr(original, "is_animated", False))
                original.seek(0)
                preview = ImageOps.exif_transpose(original)
                preview.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
                preview = preview.convert("RGBA")
                preview.info.clear()
                while True:
                    encoded = io.BytesIO()
                    preview.save(encoded, format="PNG", optimize=True)
                    png = encoded.getvalue()
                    if len(png) <= PREVIEW_BYTES:
                        break
                    if max(preview.size) <= 1:
                        raise ReadProblem("image_preview_size_limit")
                    preview.thumbnail((max(1, int(preview.width * .78)), max(1, int(preview.height * .78))),
                                      Image.Resampling.LANCZOS)
                width, height = preview.size
                preview.close()
            folder = self.image_root / uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=False)
            snapshot = folder / ("source" + suffix)
            preview_file = folder / ("preview-" + uuid.uuid4().hex + ".png")
            with snapshot.open("xb") as output:
                output.write(data)
            with preview_file.open("xb") as output:
                output.write(png)
            return {"snapshot_file": str(snapshot.resolve()), "source_sha256": hashlib.sha256(data).hexdigest(),
                    "source_bytes": len(data), "source_mime_type": mime, "source_animated": animated,
                    "preview_file": str(preview_file.resolve()), "preview_sha256": hashlib.sha256(png).hexdigest(),
                    "preview_bytes": len(png), "preview_width": width, "preview_height": height, "preview_scope": "first_frame"}
        except ReadProblem:
            raise
        except Exception as error:
            raise ReadProblem("image_preparation_failed") from error

    @contextmanager
    def lease_images(self, assets: tuple[dict, ...]):
        lease = {"cleanup_errors": []}
        if not assets:
            yield lease
            return
        if sys.platform != "win32":
            raise ReadProblem("image_lease_unavailable")
        import ctypes as C
        import msvcrt
        from ctypes import wintypes as W
        kernel = C.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        kernel.CreateFileW.restype = W.HANDLE
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [W.HANDLE], W.BOOL
        handles = []
        try:
            root = self.image_root.resolve()
            for asset in assets:
                for prefix, path_key in (("source", "snapshot_file"), ("preview", "preview_file")):
                    path = Path(asset[path_key])
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(root) or path.is_symlink() or path.parent.is_symlink():
                        raise ReadProblem("image_path_not_owned")
                    handle = kernel.CreateFileW(str(resolved), 0x80000000, 1, None, 3, 0x80, None)
                    if handle == C.c_void_p(-1).value:
                        raise ReadProblem("image_lease_unavailable")
                    try:
                        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                    except Exception:
                        kernel.CloseHandle(handle)
                        raise
                    stream = os.fdopen(descriptor, "rb")
                    handles.append(stream)
                    data = stream.read(IMAGE_SOURCE_BYTES + 1 if prefix == "source" else PREVIEW_BYTES + 1)
                    if len(data) != asset[prefix + "_bytes"] or hashlib.sha256(data).hexdigest() != asset[prefix + "_sha256"]:
                        raise ReadProblem("image_bytes_changed")
            yield lease
        except ReadProblem:
            raise
        except Exception as error:
            raise ReadProblem("image_lease_unavailable") from error
        finally:
            for stream in reversed(handles):
                try:
                    stream.close()
                except Exception as error:
                    lease["cleanup_errors"].append({"code": "image_lease_cleanup_failed", "type": type(error).__name__})

    def send(self, target: dict, action: dict) -> dict:
        if target["kind"] in REACTIONS:
            with self.bound_account():
                if self.helpers is None:
                    raw = self.client.react_for_test(target["kind"], target["post_id"])
                elif target["kind"] == "like":
                    raw = self.helpers.send_like(target["post_id"])
                else:
                    raw = self.helpers.send_favourite(target["post_id"])
                return reaction_receipt(target["kind"], target["post_id"], self.account.uid, raw)
        if self.helpers is None:
            try:
                return self.client.create_for_test(target["kind"], target["post_id"], action["text"],
                    root_id=target.get("root_cid", ""), reply_id=target.get("incoming_cid", ""),
                    image_url=action.get("image_url"), image_file=action.get("image_file"))
            except ValueError as error:
                refusal = parse_sticker_refusal(str(error))
                if refusal:
                    raise PreflightRefusal(refusal) from None
                raise
        with self.bound_account():
            codes = self.helpers._known_shortcodes()
            tokens = re.findall(r"\[(?:cube|heygirl|grandemoji|bigemoji)_[^\]]+\]", action["text"])
            if tokens and not codes:
                raise PreflightRefusal({"code": "shortcode_catalog_unavailable", "dispatched": False})
            invalid = [token for token in tokens if token not in codes]
            if invalid:
                raise PreflightRefusal({"code": "invalid_shortcodes", "shortcodes": invalid, "resource": "shortcodes", "dispatched": False})
            if action["text"].strip().lower() in {"测试", "测试一下", "test", "测试评论", "1", "test message"}:
                raise PreflightRefusal({"code": "public_test_text", "dispatched": False})
            try:
                if target["kind"] == "reply":
                    return self.helpers.send_reply(target["post_id"], target["root_cid"], target["incoming_cid"], action["text"])
                return self.helpers.send_comment(target["post_id"], action["text"], image_url=action.get("image_url"),
                    image_file=action.get("image_file"), no_sticker=action.get("without_image_reason") is not None,
                    why_not=action.get("without_image_reason") or "")
            except ValueError as error:
                refusal = parse_sticker_refusal(str(error))
                if refusal:
                    raise PreflightRefusal(refusal) from None
                raise
            except Exception as error:
                self._write_failure(error)

    def shortcodes(self) -> list:
        if self.helpers is None:
            return []
        return json.loads(self.helpers.EMOJI_WHITELIST.read_text(encoding="utf-8")).get("usable") or []

    def stickers(self, query: str) -> list:
        return self.helpers._sticker_candidates(query, top=5, floor=0.35) if self.helpers is not None else []

    def taste(self) -> dict:
        return self.helpers.taste_profile() if self.helpers is not None else {}

    def messages(self, limit: int = 20, max_pages: int = 3) -> list:
        """Platform reply queue. Names sender, root and post without scanning floors."""
        if self.helpers is None:
            return []
        return self.helpers.fetch_messages(limit=limit, max_pages=max_pages)


def platform_image_source(url: str) -> bool:
    try:
        value = urllib.parse.urlsplit(url)
        host = value.hostname or ""
        return (value.scheme == "https" and bool(host) and (host == "max-c.com" or host.endswith(".max-c.com"))
                and value.username is None and value.password is None and value.port in (None, 443)
                and not any(char.isspace() for char in url))
    except ValueError:
        return False


class PlatformImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not platform_image_source(newurl):
            raise ReadProblem("asset_redirect_outside_platform")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WorkingBackend(SDKBackend):
    def __init__(self, alias: str = "alt"):
        path = Path(__file__).with_name("xhh_tool.py")
        spec = importlib.util.spec_from_file_location("_community_working_tools", path)
        if spec is None or spec.loader is None:
            raise ReadProblem("working_tools_unavailable")
        helpers = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = helpers
        spec.loader.exec_module(helpers)
        os.environ["XHH_DEFAULT_ACCOUNT"] = alias
        with helpers.AccountStore(helpers.STORE)._locked():
            super().__init__(helpers._get_client(alias), alias, helpers)

    def own_comments(self, offset: int, limit: int) -> list:
        data = self.client.transport.signed_request("/bbs/app/profile/bbs/comment/list",
            query={"userid": self.account.uid, "offset": str(offset), "limit": str(limit), "only_cy": "0"})
        rows = data.get("result")
        if not isinstance(rows, list):
            raise ReadProblem("invalid_profile_shape", "own_comments")
        return rows

    def own_posts(self, offset: int, limit: int) -> list:
        data = self.client.transport.signed_request("/bbs/app/profile/user/link/list",
            query={"userid": self.account.uid, "offset": str(offset), "limit": str(limit)})
        rows = data.get("post_links")
        if not isinstance(rows, list):
            raise ReadProblem("invalid_profile_shape", "own_posts")
        return rows

    def asset_files(self, post: dict) -> dict:
        folder = scratch_root() / "community-assets"
        folder.mkdir(parents=True, exist_ok=True)
        for asset in post["assets"][:4]:
            url = asset["url"]
            suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                suffix = ".jpg"
            name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            asset_folder = folder / name
            asset_folder.mkdir(parents=True, exist_ok=True)
            dest = asset_folder / ("source" + suffix)
            try:
                if not platform_image_source(url):
                    raise ReadProblem("asset_source_outside_platform")
                if not dest.is_file():
                    request = urllib.request.Request(url, headers={"User-Agent": "local-community-reader/1.0"})
                    opener = urllib.request.build_opener(PlatformImageRedirect())
                    with opener.open(request, timeout=8) as response:
                        data = response.read(16 * 1024 * 1024 + 1)
                    if len(data) > 16 * 1024 * 1024:
                        raise ReadProblem("asset_too_large")
                    dest.write_bytes(data)
                asset["local_file"] = str(dest)
                asset.pop("download_error", None)
            except Exception as error:
                asset["download_error"] = error_data(error)["code"]
        return post
