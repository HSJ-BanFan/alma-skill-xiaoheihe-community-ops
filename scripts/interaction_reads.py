"""Validated private imports and bounded read-only interaction synchronization."""
from __future__ import annotations

import time
import sqlite3
from types import SimpleNamespace

from documents import author, comment, floors, identifier, integer, result_object, source
from interaction_history import Scope, database
from operations import OperationError, read, verify_identity
from paging import Budget, ReadLimit, coverage, decode_resume, encode_resume
from xhh_sdk.exceptions import XhhConfigError, XhhError


def private_nodes(kind, data, uid):
    nodes, events, gaps = [], [], []
    if kind == "my-comments":
        for row in data:
            raw = row.get("comment", row)
            if not isinstance(raw, dict):
                raise OperationError("invalid_my_comments_response")
            link = row.get("link") if isinstance(row.get("link"), dict) else {}
            post = identifier(raw.get("linkid") or raw.get("link_id") or row.get("linkid") or row.get("link_id") or link.get("linkid") or link.get("id"))
            if not post:
                gaps.append("own_comment_post_unknown")
                continue
            root = identifier(raw.get("root_comment_id") or raw.get("rootid") or raw.get("root_id"))
            try:
                node = comment(raw, post, root)
            except XhhConfigError:
                raise OperationError("invalid_my_comments_response") from None
            if raw.get("text") is not None and not isinstance(raw["text"], str):
                raise OperationError("invalid_my_comments_response")
            if node["author"]["uid"] not in (None, uid):
                raise OperationError("own_comment_uid_mismatch")
            node["author"]["uid"] = uid
            nodes.append(node)
        return nodes, events, gaps
    messages = data["entries"] if "entries" in data else data["messages"]
    for row in messages:
        if not isinstance(row, dict):
            raise OperationError("invalid_inbox_response")
        if any(row.get(field) is not None and not isinstance(row[field], str) for field in ("comment_a_text", "comment_b_text")):
            raise OperationError("invalid_inbox_response")
        cid = identifier(row.get("comment_a_id"))
        if not cid:
            if row.get("comment_a_id") not in (None, ""):
                gaps.append("incoming_cid_invalid")
            continue
        post = identifier(row.get("linkid") or row.get("link_id"))
        root = identifier(row.get("root_comment_id"))
        parent = identifier(row.get("comment_b_id"))
        if not post:
            gaps.append("incoming_post_unknown")
            continue
        a_user = row.get("user_a") if isinstance(row.get("user_a"), dict) else row.get("comment_a_user") if isinstance(row.get("comment_a_user"), dict) else row.get("user")
        b_user = row.get("user_b") if isinstance(row.get("user_b"), dict) else row.get("comment_b_user")
        peer = author({"userid": row.get("userid_a") or row.get("comment_a_userid") or row.get("comment_a_uid"), "user": a_user})["uid"]
        target_uid = author({"userid": row.get("userid_b") or row.get("comment_b_userid") or row.get("comment_b_uid"), "user": b_user})["uid"]
        incoming = {"id": cid, "post_id": post, "root_id": root, "author": {"uid": peer},
            "reply_to_id": parent, "reply_to_uid": target_uid,
            "created_at": integer(row.get("comment_a_create_at"))}
        nodes.append(incoming)
        if parent:
            nodes.append({"id": parent, "post_id": post, "root_id": root, "author": {"uid": target_uid},
                          "created_at": integer(row.get("comment_b_create_at"))})
        if parent and peer != uid and target_uid in (None, uid):
            events.append({"event_key": "message:" + str(row["message_id"]) if row.get("message_id") is not None else "incoming:" + cid,
                "post_id": post, "incoming_comment_id": cid, "root_id": root, "peer_uid": peer})
    return nodes, events, gaps


def capture_private(store, client, kind, data, page_coverage, *, sanitize=lambda value: value, scope=None):
    uid = str(client.config.heybox_id)
    nodes, events, gaps = private_nodes(kind, data, uid)
    if scope:
        nodes = [node for node in nodes if node["post_id"] == scope.post_id]
        events = [event for event in events if event["post_id"] == scope.post_id]
    evidence = source(client, "/bbs/app/user/message" if kind == "inbox" else "/bbs/app/profile/bbs/comment/list")
    evidence.update(identity_verified=True, identity_verified_this_read=True,
                    actor_origin="explicit_user_fields" if kind == "inbox" else "verified_own_comment_endpoint")
    result = store.capture(uid, sanitize(nodes), "inbox" if kind == "inbox" else "own_list", evidence,
                           inbox=sanitize(events), scope=scope, coverage=page_coverage)
    return {**result, "gaps": gaps}


KNOWN_LIMIT = 100
LEGACY_STATE_FIELDS = frozenset(("phase", "offset", "page", "roots", "cursor", "comments_done",
                                 "inbox_offset", "own_list_offset"))
STATE_FIELDS = LEGACY_STATE_FIELDS | frozenset(("known_roots", "known_done", "return_phase"))


def initial_state():
    return {"phase": "inbox", "offset": 0, "page": 1, "roots": [], "cursor": None, "comments_done": False,
            "inbox_offset": 0, "own_list_offset": 0, "known_roots": [], "known_done": [], "return_phase": "inbox"}


def normalize_state(state):
    if not isinstance(state, dict):
        raise XhhConfigError("invalid sync continuation")
    if set(state) == STATE_FIELDS:
        return state
    if set(state) != LEGACY_STATE_FIELDS:
        raise XhhConfigError("invalid sync continuation")
    return {**state, "known_roots": [], "known_done": [],
            "return_phase": state["phase"] if state["phase"] in ("inbox", "own_list", "comments", "done") else "inbox"}


def private_continuation(state, preferred="inbox"):
    phases = (preferred, "own_list" if preferred == "inbox" else "inbox")
    for phase in phases:
        offset = state[phase + "_offset"]
        if offset is not None:
            state.update(phase=phase, offset=offset)
            return
    state["phase"] = "done"


def validate_state(state):
    if not isinstance(state, dict) or set(state) != STATE_FIELDS:
        raise XhhConfigError("invalid sync continuation")
    if state["phase"] not in ("inbox", "own_list", "comments", "known_replies", "replies", "done"):
        raise XhhConfigError("invalid sync phase")
    if state["return_phase"] not in ("inbox", "own_list", "comments", "done"):
        raise XhhConfigError("invalid sync return phase")
    if type(state["offset"]) is not int or state["offset"] < 0 or type(state["page"]) is not int or state["page"] < 1:
        raise XhhConfigError("invalid sync position")
    for field in ("roots", "known_roots", "known_done"):
        values = state[field]
        if (not isinstance(values, list) or len(values) > KNOWN_LIMIT
                or any(not isinstance(cid, str) or not identifier(cid) for cid in values)
                or len(set(values)) != len(values)):
            raise XhhConfigError("invalid sync " + field)
    if state["phase"] == "replies" and not state["roots"]:
        raise XhhConfigError("sync replies require a root")
    if state["phase"] != "replies" and state["roots"]:
        raise XhhConfigError("sync roots require the replies phase")
    if state["phase"] == "known_replies" and not state["known_roots"]:
        raise XhhConfigError("sync known replies require a root")
    if state["phase"] != "known_replies" and state["known_roots"]:
        raise XhhConfigError("sync known reply roots require the known_replies phase")
    if set(state["known_roots"]) & set(state["known_done"]):
        raise XhhConfigError("sync known reply roots conflict with completed roots")
    if state["cursor"] is not None and (not isinstance(state["cursor"], str) or len(state["cursor"]) > 4096):
        raise XhhConfigError("invalid sync cursor")
    if type(state["comments_done"]) is not bool:
        raise XhhConfigError("invalid sync completion")
    for field in ("inbox_offset", "own_list_offset"):
        if state[field] is not None and (type(state[field]) is not int or state[field] < 0):
            raise XhhConfigError("invalid private sync continuation")


def seed_known_roots(store, scope, state, gaps):
    candidates = store.root_candidates(scope, limit=KNOWN_LIMIT)
    if candidates["truncated"]:
        gaps.append("known_root_candidates_truncated")
    gaps.extend(candidates["gaps"])
    seen = set(state["known_roots"]) | set(state["known_done"])
    added = []
    for root in candidates["root_ids"]:
        if root in seen:
            continue
        if not store.root_reference_valid(scope, root):
            gaps.append("unsupported_known_root:" + root)
            continue
        seen.add(root)
        added.append(root)
    room = KNOWN_LIMIT - len(state["known_roots"])
    if len(added) > room:
        gaps.append("known_roots_truncated")
        added = added[:room]
    if added:
        state["known_roots"].extend(added)
        if state["phase"] in ("inbox", "own_list", "comments", "done"):
            state["return_phase"] = state["phase"]
        state["phase"] = "known_replies"


def complete_known_root(state, root, gaps):
    done = list(state["known_done"])
    done.append(root)
    if len(done) > KNOWN_LIMIT:
        done = done[-KNOWN_LIMIT:]
        gaps.append("known_roots_truncated")
    state["known_done"] = done


def validate_references(store, scope, state):
    if state["phase"] == "replies" and any(
            store.root_post(scope.account_uid, root) != scope.post_id or (scope.root_id and root != scope.root_id)
            for root in state["roots"]):
        raise XhhConfigError("sync continuation root is not established on the selected post")
    for root in (*state["known_roots"], *state["known_done"]):
        if (scope.root_id and root != scope.root_id) or not store.root_reference_valid(scope, root):
            raise XhhConfigError("sync continuation root reference is not established on the selected post")


def synchronize(store, client, scope, args, *, sanitize=lambda value: value):
    binding = {**scope.binding(), "protocol": client.config.protocol_mode, "limit": args.limit, "sort": args.sort}
    state = initial_state()
    if args.resume:
        token = decode_resume(args.resume, "interaction-sync", scope.account_uid)
        if set(token) != {"binding", "state"} or token["binding"] != binding:
            raise XhhConfigError("sync resume belongs to a different account, protocol or query")
        state = normalize_state(token["state"])
        validate_state(state)
        validate_references(store, scope, state)
    budget = Budget(args.requests, args.seconds)
    imported, pages, gaps, stop = 0, 0, [], None
    last_error = None
    unpersisted_page = None
    verified = False
    try:
        budget.call(client, lambda: verify_identity(client))
        verified = True
    except ReadLimit as error:
        stop = error.reason
    except (XhhError, OperationError) as error:
        stop = "platform_error"
        last_error = {"type": type(error).__name__, "code": getattr(error, "code", None), "status": getattr(error, "status", None)}
    if stop is None:
        try:
            store.establish(scope.account_uid, {"source": "sync_identity_check", "protocol": client.config.protocol_mode})
        except (sqlite3.Error, OSError) as error:
            stop = "storage_error"
            last_error = {"type": type(error).__name__, "code": "interaction_cache_incomplete"}
    if stop is None and not args.resume:
        try:
            seed_known_roots(store, scope, state, gaps)
        except (sqlite3.Error, OSError) as error:
            stop = "storage_error"
            last_error = {"type": type(error).__name__, "code": "interaction_cache_incomplete"}
    seen_positions = set()
    while stop is None and state["phase"] != "done" and pages < args.pages:
        position = (state["phase"], state["offset"], state["page"], tuple(state["roots"]),
                    tuple(state["known_roots"]), tuple(state["known_done"]), state["cursor"])
        if position in seen_positions:
            stop = "cursor_stalled"
            gaps.append("repeated_continuation")
            break
        seen_positions.add(position)
        phase = state["phase"]
        if phase == "known_replies":
            root = state["known_roots"][0]
            if (scope.root_id and root != scope.root_id) or not store.root_reference_valid(scope, root):
                raise XhhConfigError("sync known reply root is not established on the selected post")
        next_state = {**state, "roots": list(state["roots"]), "known_roots": list(state["known_roots"]),
                      "known_done": list(state["known_done"])}
        read_page = None
        try:
            if phase in ("inbox", "own_list"):
                kind = "inbox" if phase == "inbox" else "my-comments"
                private_args = SimpleNamespace(offset=state["offset"], limit=args.limit)
                data, page_scope = budget.call(client, lambda: read(client, kind, private_args))
                read_page = data
                records = data["entries"] if kind == "inbox" and "entries" in data else data["messages"] if kind == "inbox" else data
                if kind == "inbox":
                    marker = coverage(data, count=len(records), position=state["offset"])
                else:
                    marker = {"completion": "unknown", "next": state["offset"] + len(records)}
                ended = marker["completion"] == "complete" or not records
                next_state[phase + "_offset"] = None if ended else state["offset"] + len(records)
                if ended:
                    if marker["completion"] != "complete":
                        gaps.append(phase + "_endpoint_unknown")
                if state["comments_done"]:
                    private_continuation(next_state, "own_list" if phase == "inbox" else "inbox")
                else:
                    next_state.update(phase="own_list" if phase == "inbox" else "comments", offset=0)
                result = capture_private(store, client, kind, data, {**page_scope, "continuation": next_state}, sanitize=sanitize, scope=scope)
                gaps.extend(result["gaps"])
                imported += result["comments_observed"]
                if kind == "my-comments":
                    read_page = None
                    seed_known_roots(store, scope, next_state, gaps)
            elif phase == "comments":
                raw = budget.call(client, lambda: client.transport.signed_request("/bbs/app/link/tree", query={"link_id": scope.post_id,
                    "page": str(state["page"]), "limit": str(args.limit), "sort_filter": args.sort}))
                result = result_object(raw)
                read_page = raw
                nodes = floors(result.get("comments"), scope.post_id)
                page_scope = coverage(result, count=len(result["comments"]), position=state["page"], mode="page")
                roots = [node for node in nodes if node["id"] == node["root_id"] and (not scope.root_id or node["id"] == scope.root_id)]
                completed_known = set(state["known_done"])
                queued = []
                for node in roots:
                    if node["id"] in completed_known:
                        continue
                    known = len({child["id"] for child in nodes if child["root_id"] == node["id"] and child["id"] != node["id"]})
                    count = node["children_count"]
                    if node["has_more"] is True or count is not None and known < count or node["has_more"] is None and count is None:
                        queued.append(node["id"])
                    if node["has_more"] is False and count is not None and known < count:
                        gaps.append("root_children_markers_conflict:" + node["id"])
                queued = list(dict.fromkeys(queued))
                found_root = bool(scope.root_id and roots)
                done = found_root or page_scope["completion"] == "complete" or page_scope["next"] is None
                gaps.extend(page_scope["gaps"])
                if page_scope["completion"] == "unknown" and page_scope["next"] is None:
                    gaps.append("comment_continuation_unknown")
                if done and scope.root_id and not found_root:
                    gaps.append("requested_root_not_observed")
                next_state.update(page=page_scope["next"] or state["page"], roots=queued, cursor=None, comments_done=done,
                                  phase="replies" if queued else "done" if done else "comments")
                if next_state["phase"] == "done":
                    private_continuation(next_state)
                evidence = source(client, "/bbs/app/link/tree")
                evidence.update(identity_verified=True, identity_verified_this_read=True)
                result = store.capture(scope.account_uid, sanitize(nodes), "public", evidence, scope=scope,
                                       coverage={**page_scope, "continuation": next_state})
                imported += result["comments_observed"]
            else:
                pending = "known_roots" if phase == "known_replies" else "roots"
                root = state[pending][0]
                query = {"root_comment_id": root, "hide_cy": "0", "h_src": ""}
                if state["cursor"] is not None:
                    query["lastval"] = state["cursor"]
                raw = budget.call(client, lambda: client.transport.signed_request("/bbs/app/comment/sub/comments", query=query))
                result = result_object(raw)
                read_page = raw
                records = result.get("comments")
                if not isinstance(records, list):
                    raise XhhConfigError("sync reply list missing from platform result")
                nodes = [comment(record, scope.post_id, root) for record in records]
                page_scope = coverage(result, count=len(records), position=state["cursor"], mode="cursor")
                gaps.extend(page_scope["gaps"])
                stalled = page_scope["next"] is not None and page_scope["next"] == state["cursor"]
                if stalled:
                    stop = "cursor_stalled"
                    gaps.append("repeated_continuation")
                elif page_scope["completion"] == "complete" or page_scope["next"] is None:
                    remaining = list(state[pending][1:])
                    next_state.update({pending: remaining, "cursor": None})
                    if phase == "known_replies":
                        complete_known_root(next_state, root, gaps)
                        if not remaining:
                            next_state["phase"] = state["return_phase"]
                    elif not remaining:
                        next_state["phase"] = "done" if state["comments_done"] else "comments"
                        if next_state["phase"] == "done":
                            private_continuation(next_state)
                    if page_scope["completion"] != "complete":
                        gaps.append("reply_continuation_missing:" + root)
                else:
                    next_state["cursor"] = page_scope["next"]
                evidence = source(client, "/bbs/app/comment/sub/comments")
                evidence.update(identity_verified=True, identity_verified_this_read=True)
                if phase == "known_replies":
                    evidence["post_id_source"] = "verified_own_list_reference"
                result = store.capture(scope.account_uid, sanitize(nodes), "public", evidence, scope=scope,
                                       coverage={**page_scope, "continuation": next_state})
                imported += result["comments_observed"]
            state = next_state
            pages += 1
        except ReadLimit as error:
            stop = error.reason
        except (XhhError, OperationError) as error:
            stop = "platform_error"
            last_error = {"type": type(error).__name__, "code": getattr(error, "code", None), "status": getattr(error, "status", None)}
        except (sqlite3.Error, OSError) as error:
            stop = "storage_error"
            last_error = {"type": type(error).__name__, "code": "interaction_cache_incomplete"}
            unpersisted_page = sanitize(read_page)
    if stop is None and state["phase"] != "done":
        stop = "page_budget"
    token = encode_resume("interaction-sync", scope.account_uid, {"binding": binding, "state": state}) if state["phase"] != "done" else None
    scope_coverage = {"history_complete": False, "scope": "bounded_sources", "completion": "unknown", "scan_finished": state["phase"] == "done",
                      "stop_reason": stop, "gaps": sorted(set(gaps)), "resume": token, "continuation": state,
                      "pages_read": pages, "budget": budget.view(), "error": last_error,
                      "interaction_capture_complete": stop != "storage_error"}
    if verified and stop != "storage_error" and (pages or store.trusted(scope.account_uid)):
        try:
            with database(store.path, write=True) as connection:
                store.save_coverage(connection, scope, "sync", scope_coverage, time.time())
        except (sqlite3.Error, OSError) as error:
            scope_coverage.update(stop_reason="storage_error", interaction_capture_complete=False,
                                  error={"type": type(error).__name__, "code": "interaction_cache_incomplete"})
    return {"account_uid": scope.account_uid, **scope.binding(), "comments_observed": imported,
            "identity_verified_this_read": verified, "unpersisted_page": unpersisted_page, "status": store.status(scope)}, scope_coverage
