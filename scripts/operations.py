"""Platform operations and durable, caller-selected write identities."""
from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

from xhh_sdk.exceptions import XhhConfigError
from xhh_sdk.interaction import _validate_text
from xhh_sdk.payload import Post
from interaction_history import (database, bind_context, fingerprint, project_receipts, receipt_metadata,
                                 request_fingerprint, bind_message_context, read_message_context)


class OperationError(RuntimeError):
    def __init__(self, code: str, *, operation=None, receipt=None):
        self.code = code
        self.operation = operation
        self.receipt = receipt
        super().__init__(code)


def numeric_id(value) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,29}", str(value)):
        raise XhhConfigError("target ID must be a positive integer")
    return str(value)


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Journal:
    def __init__(self, root: Path):
        self.path = root / "operations.sqlite3"

    @contextmanager
    def connect(self):
        with database(self.path, write=True) as connection:
            yield connection

    def read(self, uid: str, operation_id: str):
        if not self.path.exists():
            return None
        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM operations WHERE uid=? AND operation_id=?",
                                     (uid, operation_id)).fetchone()
            context = read_message_context(connection, uid, operation_id)
        if row is None:
            return None
        result = {"operation_id": row["operation_id"], "kind": row["kind"], "status": row["status"],
                  "receipt": receipt_metadata(row["kind"], json.loads(row["receipt"])) if row["receipt"] else None,
                  "updated_at": row["updated_at"]}
        if context:
            result["context"] = context
        return result

    def execute(self, uid: str, operation_id: str, kind: str, payload: dict, send, *, identity_verified=False, context=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id or ""):
            raise XhhConfigError("--operation-id requires 1-128 letters, digits, underscores or hyphens")
        request_hash = hashlib.sha256(json_text({"kind": kind, "payload": payload}).encode()).hexdigest()
        replay = None
        closed = None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM operations WHERE uid=? AND operation_id=?",
                                     (uid, operation_id)).fetchone()
            if row is not None:
                if row["fingerprint"] != request_hash:
                    raise OperationError("operation_id_conflict", operation=self.read(uid, operation_id))
                bind_context(connection, uid, operation_id, kind, payload, identity_verified=identity_verified, context=context)
                bind_message_context(connection, uid, operation_id, context)
                if row["status"] == "returned":
                    replay = {"operation_id": operation_id, "replayed": True,
                              "status": "returned", "receipt": receipt_metadata(kind, json.loads(row["receipt"]))}
                else:
                    closed = self.read(uid, operation_id)
            else:
                connection.execute("INSERT INTO operations VALUES (?, ?, ?, ?, 'reserved', NULL, ?)",
                                   (uid, operation_id, request_hash, kind, time.time()))
                bind_context(connection, uid, operation_id, kind, payload, identity_verified=identity_verified, context=context)
                bind_message_context(connection, uid, operation_id, context)
        if replay is not None:
            return {**replay, **self.project(uid)}
        if closed is not None:
            raise OperationError("operation_pending_or_uncertain", operation=closed)
        try:
            receipt = send()
        except Exception as error:
            try:
                self.settle(uid, operation_id, "uncertain", None)
            except Exception:
                pass
            raise OperationError("write_uncertain", operation={"operation_id": operation_id,
                                 "status": "uncertain", "error_type": type(error).__name__}) from None
        try:
            self.settle(uid, operation_id, "returned", receipt)
        except Exception:
            raise OperationError("receipt_persistence_failed", operation={"operation_id": operation_id,
                                 "status": "reserved"}, receipt=receipt) from None
        return {"operation_id": operation_id, "replayed": False, "status": "returned", "receipt": receipt,
                **self.project(uid)}

    def project(self, uid):
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                project_receipts(connection, uid)
            return {"interaction_projection_complete": True}
        except Exception as error:
            return {"interaction_projection_complete": False, "interaction_projection_error": type(error).__name__}

    def bind_submitted_request(self, uid, operation_id, request):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""SELECT c.request_fingerprint,c.submission_fingerprint,o.kind,o.status
                                     FROM interaction_write_context c JOIN operations o USING(uid,operation_id)
                                     WHERE c.uid=? AND c.operation_id=?""",
                                     (uid, operation_id)).fetchone()
            if row is None or row["kind"] not in ("comment", "reply") or row["status"] != "reserved":
                raise XhhConfigError("submitted comment requires reserved context")
            if request_fingerprint(row["kind"], request) != row["request_fingerprint"]:
                raise XhhConfigError("submitted request differs from reserved context")
            submitted = fingerprint(request)
            if row["submission_fingerprint"] is not None and row["submission_fingerprint"] != submitted:
                raise XhhConfigError("submitted request differs from the first submission")
            connection.execute("UPDATE interaction_write_context SET submission_fingerprint=? WHERE uid=? AND operation_id=?",
                               (submitted, uid, operation_id))

    def settle(self, uid: str, operation_id: str, status: str, receipt):
        with self.connect() as connection:
            row = connection.execute("SELECT kind FROM operations WHERE uid=? AND operation_id=?", (uid, operation_id)).fetchone()
            receipt = receipt_metadata(row["kind"], receipt)
            connection.execute("UPDATE operations SET status=?, receipt=?, updated_at=? WHERE uid=? AND operation_id=?",
                               (status, json_text(receipt) if receipt is not None else None,
                                time.time(), uid, operation_id))


def verify_identity(client) -> dict:
    data = client.account_info()
    if not isinstance(data, dict):
        raise OperationError("account_identity_not_verified")
    identities = set()
    for container in (data, data.get("user"), data.get("account_detail")):
        if isinstance(container, dict):
            for key in ("heybox_id", "userid", "user_id", "id"):
                if container.get(key) is not None:
                    identities.add(str(container[key]))
    if identities != {str(client.config.heybox_id)}:
        raise OperationError("account_identity_not_verified")
    return data


def read(client, kind: str, args) -> tuple[object, dict]:
    query = {"offset": str(getattr(args, "offset", 0)), "limit": str(getattr(args, "limit", 20))}
    coverage = {"full_history": False}
    if kind == "account-info":
        return verify_identity(client), {"identity_verified": True}
    if kind == "inbox":
        data = client.transport.signed_request("/bbs/app/user/message",
                    query={**query, "list_type": "0", "no_more": "false"})
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise OperationError("invalid_inbox_response")
        messages = result.get("entries") if "entries" in result else result.get("messages")
        if not isinstance(messages, list):
            raise OperationError("invalid_inbox_response")
        return result, {**coverage, "offset": args.offset, "limit": args.limit,
                        "next_offset": args.offset + len(messages)}
    elif kind == "emojis":
        return client.emojis(), coverage
    elif kind == "creator-options":
        return client.topic_index(link_id=args.link_id), coverage
    elif kind == "drafts":
        return client.drafts(offset=args.offset, limit=args.limit), coverage
    elif kind == "my-posts":
        data = client.transport.signed_request("/bbs/app/profile/user/link/list",
                    query={**query, "userid": client.config.heybox_id})
        return data, coverage
    elif kind == "my-comments":
        data = client.transport.signed_request("/bbs/app/profile/bbs/comment/list",
                    query={**query, "userid": str(client.config.heybox_id), "only_cy": "0"})
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise OperationError("invalid_my_comments_response")
        return result, {**coverage, "offset": args.offset, "limit": args.limit,
                        "next_offset": args.offset + len(result)}
    else:
        raise XhhConfigError("unknown read operation")
    return data, {**coverage, "offset": args.offset, "limit": args.limit}


def prepare_write(client, kind: str, args, *, before_post=None):
    payload = {"link_id": getattr(args, "link_id", None)}
    if kind in ("comment", "reply"):
        body = _validate_text(args.text_file.read_text(encoding="utf-8-sig"))
        payload["text"] = body
        client.post_state(args.link_id)
        if kind == "reply":
            payload.update(root_id=args.root_id, reply_id=args.reply_id)
            floors = client.comment_thread(args.link_id, page=args.page, limit=args.limit, sort=args.sort)
            floor = next((floor for floor in floors if str(floor["root"].get("commentid") or
                         floor["root"].get("comment_id") or floor["root"].get("id")) == args.root_id), None)
            if floor is None:
                raise XhhConfigError("root ID not observed on this post/page")
            nodes = [floor["root"], *floor["replies"]]
            target_in_floor = any(str(node.get("commentid") or node.get("comment_id") or node.get("id")) == args.reply_id
                                  for node in nodes)
            if args.cursor is not None or not target_in_floor:
                batch = client.sub_comments(args.root_id, lastval=args.cursor)
                if not any(str(node.get("commentid") or node.get("comment_id") or node.get("id")) == args.reply_id
                           for node in batch.get("comments", []) if isinstance(node, dict)):
                    raise XhhConfigError("reply ID not observed in this root/cursor page")
        image = getattr(args, "image_file", None)
        image_url = getattr(args, "image_url", None)
        if image:
            data = image.read_bytes()
            if not data or len(data) > 10 * 1024 * 1024:
                raise XhhConfigError("comment image must be nonempty and at most 10 MiB")
            client._image_info(image, data)
            payload["image_sha256"] = hashlib.sha256(data).hexdigest()
        elif image_url:
            if not image_url.startswith("https://"):
                raise XhhConfigError("image URL must use HTTPS")
            payload["image_url"] = image_url

        def send():
            if image and hashlib.sha256(image.read_bytes()).hexdigest() != payload["image_sha256"]:
                raise XhhConfigError("image changed after preparation")
            url = client.upload(image)["url"] if image else image_url
            request = {"link_id": args.link_id, "text": body, "imgs": url or "", "is_cy": "0"}
            if kind == "reply":
                request.update(root_id=args.root_id, reply_id=args.reply_id)
            if before_post:
                before_post(request)
            result = client.transport.signed_request("/bbs/app/comment/create", query={"h_src": ""}, payload=request)
            cid = client._created_comment_id(result)
            if not cid:
                raise OperationError("missing_comment_receipt")
            receipt = {"comment_id": numeric_id(cid), "link_id": args.link_id,
                       "root_id": getattr(args, "root_id", None), "reply_id": getattr(args, "reply_id", None),
                       "visibility": "not_checked"}
            if url:
                receipt["image_url"] = url
            return receipt
        return payload, send
    if kind == "like":
        client.post_state(args.link_id)
        return payload, lambda: client.transport.signed_request("/bbs/app/link/like/combo",
                    query={"h_src": ""}, payload={"link_id": args.link_id})
    if kind in ("favourite", "unfavourite"):
        return payload, lambda: getattr(client, kind)(args.link_id, confirm=True)
    if kind == "delete-comment":
        payload["comment_id"] = args.comment_id
        if not client._owns_comment(args.link_id, args.comment_id):
            raise XhhConfigError("comment ownership not verified")
        return payload, lambda: client.delete_comment(args.link_id, args.comment_id, confirm=True)
    if kind == "delete-post":
        client._assert_own_post(args.link_id)
        return payload, lambda: client.delete(args.link_id)
    if kind == "upload":
        data = args.image_file.read_bytes()
        if not data or len(data) > 10 * 1024 * 1024:
            raise XhhConfigError("image must be nonempty and at most 10 MiB")
        client._image_info(args.image_file, data)
        payload = {"image_sha256": hashlib.sha256(data).hexdigest()}

        def send_upload():
            if hashlib.sha256(args.image_file.read_bytes()).hexdigest() != payload["image_sha256"]:
                raise XhhConfigError("image changed after preparation")
            return client.upload(args.image_file)
        return payload, send_upload
    if kind == "publish":
        spec = json.loads(args.spec_file.read_text(encoding="utf-8-sig"))
        if not isinstance(spec, dict):
            raise XhhConfigError("post spec must be a JSON object")
        post = Post(**{**spec, "draft": not args.public})
        if any(not str(image).startswith("https://") for image in post.images):
            raise XhhConfigError("upload images first and use their HTTPS URLs in the post spec")
        payload = post.build()
        if args.public and not post.topic_ids and client.config.protocol_mode == "web":
            raise XhhConfigError("public Web posts require topic_ids from creator-options")
        if post.edit_link_id:
            client._assert_own_post(numeric_id(post.edit_link_id))
        return payload, lambda: client.publish(post, confirm=True, upload_local=False)
    raise XhhConfigError("unknown write operation")
