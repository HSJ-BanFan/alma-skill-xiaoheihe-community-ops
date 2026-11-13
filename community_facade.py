"""Three-operation community facade; transport shapes and bookkeeping stay here."""
from __future__ import annotations

import ast
import copy
import json
import re
import shlex
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from community import bounded_summary, envelope
from community_bindings import DispatchProblem, PreflightRefusal, PublicKnowledge, ReadProblem, error_data, id_value, profile_records, reaction_receipt
from community_state import ImageIssue, ImagePlan, REACTIONS, RunLimit, State, StateConflict


@dataclass(frozen=True)
class ReadBudget:
    requests: int = 12
    rows: int = 300
    seconds: float = 40


@dataclass(frozen=True)
class Action:
    resource: str
    kind: str
    text: str = ""
    image_file: str | None = None
    image_url: str | None = None
    without_image_reason: str | None = None
    context_keys: tuple[str, ...] = ()


def timestamp(value) -> float:
    try:
        number = float(value)
        return number / 1000 if number > 1e12 else number
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0


def _python_sources(command: str) -> list[str]:
    sources = [command]
    try:
        tokens = shlex.split(command)
        sources.extend(tokens[index + 1] for index, token in enumerate(tokens[:-1]) if token == "-c")
    except ValueError:
        pass
    sources.extend(match.group(2) for match in re.finditer(r"(?s)<<\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\1(?:\n|$)", command))
    return sources


def literal_send_calls(command: str) -> list[dict]:
    """Parse captured Python as data. Never run a historical command or file."""
    found = []
    for source in _python_sources(command):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        constants = {}
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                try:
                    value = ast.literal_eval(statement.value)
                except (ValueError, TypeError):
                    continue
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = value
        def literal(node):
            if isinstance(node, ast.Name):
                return constants.get(node.id)
            try:
                return ast.literal_eval(node)
            except (ValueError, TypeError):
                return None
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = call.func.id if isinstance(call.func, ast.Name) else call.func.attr if isinstance(call.func, ast.Attribute) else ""
            if name not in {"send_comment", "send_reply"}:
                continue
            fields = ("link_id",) if name == "send_comment" else ("link_id", "root_id", "reply_id")
            values = {key: literal(node) for key, node in zip(fields, call.args)}
            values.update({item.arg: literal(item.value) for item in call.keywords if item.arg is not None and item.arg in fields})
            if not all(str(values.get(key) or "").isdigit() for key in fields):
                continue
            found.append({"kind": "comment" if name == "send_comment" else "reply", "post_id": str(values["link_id"]),
                          "root_cid": str(values.get("root_id") or ""), "incoming_cid": str(values.get("reply_id") or "")})
    return list({json.dumps(row, sort_keys=True): row for row in found}.values())


@dataclass(frozen=True)
class LiteralReactionPrint:
    text: str


@dataclass(frozen=True)
class OriginalReactionPrint:
    kind: str
    post_id: str


@dataclass(frozen=True)
class UnprovedReactionPrint:
    pass


ReactionPrint = LiteralReactionPrint | OriginalReactionPrint | UnprovedReactionPrint


@dataclass(frozen=True)
class ReactionHistoryTrace:
    calls: tuple[dict, ...]
    unresolved_kinds: frozenset[str]
    prints: tuple[ReactionPrint, ...]


def _reaction_history_trace(command: str) -> ReactionHistoryTrace:
    helpers = {"send_like": "like", "send_favourite": "favourite"}
    found: dict[tuple[str, str], dict] = {}
    unresolved: set[str] = set()
    plans: list[list[ReactionPrint]] = []
    for source in dict.fromkeys(_python_sources(command)):
        if len(source) > 200000:
            unresolved.update(kind for name, kind in helpers.items() if re.search(r"\b" + name + r"\b", source))
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        nodes = list(ast.walk(tree))
        names = {name: {kind} for name, kind in helpers.items()}
        original_names, module_names, foreign_names = set(helpers), {"xhh_tool"}, set()
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if alias.name == "xhh_tool":
                        module_names.add(name)
                    else:
                        foreign_names.add(name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if alias.name in helpers:
                        names.setdefault(name, set()).add(helpers[alias.name])
                        if node.module == "xhh_tool" and node.level == 0:
                            original_names.add(name)
                        else:
                            foreign_names.add(name)
                    elif not (node.module == "xhh_tool" and node.level == 0 and alias.name == "*"):
                        foreign_names.update(names if alias.name == "*" else {name})
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if not isinstance(node, ast.Assign):
                    continue
                value = node.value
                kinds = names.get(value.id, set()) if isinstance(value, ast.Name) else {helpers[value.attr]} if (
                    isinstance(value, ast.Attribute) and value.attr in helpers) else set()
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    before = len(names.get(target.id, set()))
                    if kinds:
                        names.setdefault(target.id, set()).update(kinds)
                    if len(names.get(target.id, set())) != before:
                        changed = True
                    if isinstance(value, ast.Name) and value.id in module_names and target.id not in module_names:
                        module_names.add(target.id)
                        changed = True
        shadowed = {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
        shadowed.update(node.name for node in nodes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        shadowed.update(foreign_names)
        mutated = {helpers[node.attr] for node in nodes if isinstance(node, ast.Attribute) and node.attr in helpers
                   and isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(node.value, ast.Name)
                   and node.value.id in module_names}
        prints: list[ReactionPrint] = []
        steps = 0

        def original_helper(node):
            if isinstance(node.func, ast.Name):
                kinds = names.get(node.func.id, set())
                return (len(kinds) == 1 and node.func.id in original_names and node.func.id not in shadowed
                        and not kinds.intersection(mutated))
            return (isinstance(node.func, ast.Attribute) and node.func.attr in helpers and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in module_names and node.func.value.id not in shadowed
                    and helpers[node.func.attr] not in mutated)

        def known_literal(node, values):
            try:
                value = values[node.id] if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) else ast.literal_eval(node)
            except (KeyError, ValueError, TypeError, RecursionError):
                return False, None
            return True, value

        def literal(node, values):
            return known_literal(node, values)[1]

        def callee_kinds(node, values):
            if isinstance(node, ast.Name):
                return names.get(node.id, set())
            if isinstance(node, ast.Attribute):
                return {helpers[node.attr]} if node.attr in helpers else set()
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
                name = literal(node.args[1], values)
                if isinstance(name, str):
                    return {helpers[name]} if name in helpers else set()
                if isinstance(node.args[0], ast.Name) and node.args[0].id in module_names:
                    return set(helpers.values())
            if isinstance(node, ast.Subscript):
                name = literal(node.slice, values)
                if isinstance(name, str) and name in helpers:
                    return {helpers[name]}
            return set()

        def call_target(node, values):
            kinds = callee_kinds(node.func, values)
            if len(kinds) != 1:
                unresolved.update(kinds)
                return None
            kind = next(iter(kinds))
            if len(node.args) == 1 and not node.keywords:
                value = literal(node.args[0], values)
            elif not node.args and len(node.keywords) == 1 and node.keywords[0].arg == "link_id":
                value = literal(node.keywords[0].value, values)
            else:
                value = None
            pid = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
            if re.fullmatch(r"[1-9][0-9]{0,31}", pid) is None:
                unresolved.add(kind)
                return None
            key = (kind, pid)
            found.setdefault(key, {"kind": kind, "post_id": pid, "root_cid": "", "incoming_cid": "", "stdout_expected": False})
            return key

        callee_nodes = {node.func for node in nodes if isinstance(node, ast.Call)}

        def builtin_print(node):
            return (isinstance(node.func, ast.Name) and node.func.id == "print"
                    and "print" not in shadowed and "print" not in names)

        def original_import(node):
            return all(alias.name == "xhh_tool" for alias in node.names) if isinstance(node, ast.Import) else (
                isinstance(node, ast.ImportFrom) and node.module == "xhh_tool" and node.level == 0)

        def preserves_values(node, values):
            if known_literal(node, values)[0]:
                return True
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                return True
            if not isinstance(node, ast.Call):
                return False
            if builtin_print(node):
                if len(node.args) != 1 or node.keywords:
                    return False
                argument = node.args[0]
                return (known_literal(argument, values)[0] or isinstance(argument, ast.Call)
                        and original_helper(argument) and preserves_values(argument, values))
            return (original_helper(node) and all(known_literal(argument, values)[0] for argument in node.args)
                    and all(keyword.arg is not None and known_literal(keyword.value, values)[0] for keyword in node.keywords))

        def invalidate(node, values):
            for child in ast.walk(node):
                if child in callee_nodes:
                    continue
                if isinstance(child, (ast.Name, ast.Attribute)) and not isinstance(child.ctx, ast.Load):
                    continue
                if isinstance(child, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
                    unresolved.update(callee_kinds(child, values))
            values.clear()

        def discover(node, values):
            nonlocal steps
            steps += 1
            if steps > 4096:
                unresolved.update(kind for kinds in names.values() for kind in kinds)
                return
            if isinstance(node, ast.expr) and not preserves_values(node, values):
                invalidate(node, values)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if not original_import(node) or any(alias.name == "*" for alias in node.names):
                    invalidate(node, values)
                else:
                    for alias in node.names:
                        values.pop(alias.asname or alias.name.split(".")[0], None)
                return
            if isinstance(node, ast.For):
                iterable = literal(node.iter, values)
                if isinstance(node.target, ast.Name) and isinstance(iterable, (list, tuple)) and len(iterable) <= 128:
                    for value in iterable:
                        values[node.target.id] = value
                        for statement in node.body:
                            discover(statement, values)
                else:
                    invalidate(node, values)
                    discover(node.iter, {})
                    discover(node.target, {})
                    for statement in node.body:
                        discover(statement, {})
                for statement in node.orelse:
                    discover(statement, {})
                if node.orelse:
                    invalidate(node, values)
                return
            if isinstance(node, ast.Assign):
                kinds = callee_kinds(node.value, values)
                discover(node.value, values)
                known, value = known_literal(node.value, values)
                if all(isinstance(target, ast.Name) for target in node.targets):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            if known:
                                values[target.id] = value
                            else:
                                values.pop(target.id, None)
                            if kinds:
                                names.setdefault(target.id, set()).update(kinds)
                else:
                    invalidate(node, values)
                    for target in node.targets:
                        discover(target, {})
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                invalidate(node, values)
                for child in ast.iter_child_nodes(node):
                    discover(child, {})
                return
            if isinstance(node, ast.Call):
                call_target(node, values)
                for child in ast.iter_child_nodes(node):
                    discover(child, dict(values))
                return
            if isinstance(node, (ast.Module, ast.Expr)):
                for child in ast.iter_child_nodes(node):
                    discover(child, values)
                return
            if isinstance(node, ast.stmt):
                invalidate(node, values)
            for child in ast.iter_child_nodes(node):
                discover(child, dict(values))

        def prove(statement, values, results):
            nonlocal steps
            steps += 1
            if steps > 4096:
                prints.append(UnprovedReactionPrint())
                return
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                if not original_import(statement):
                    prints.append(UnprovedReactionPrint())
                return
            if isinstance(statement, ast.For):
                iterable = literal(statement.iter, values)
                if not (isinstance(statement.target, ast.Name) and isinstance(iterable, (list, tuple)) and len(iterable) <= 128):
                    prints.append(UnprovedReactionPrint())
                    return
                for value in iterable:
                    values[statement.target.id] = value
                    results.pop(statement.target.id, None)
                    for child in statement.body:
                        prove(child, values, results)
                if statement.orelse:
                    prints.append(UnprovedReactionPrint())
                return
            if isinstance(statement, ast.Assign):
                identifiers = [target.id for target in statement.targets if isinstance(target, ast.Name)]
                if len(identifiers) != len(statement.targets):
                    prints.append(UnprovedReactionPrint())
                    return
                node = statement.value
                key = call_target(node, values) if isinstance(node, ast.Call) and original_helper(node) else None
                try:
                    value = values[node.id] if isinstance(node, ast.Name) else ast.literal_eval(node)
                except (KeyError, ValueError, TypeError, RecursionError):
                    if key is None:
                        prints.append(UnprovedReactionPrint())
                    for name in identifiers:
                        values.pop(name, None)
                        results.pop(name, None)
                        if key is not None:
                            results[name] = key
                else:
                    for name in identifiers:
                        values[name] = value
                        results.pop(name, None)
                return
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                node = statement.value
                is_print = builtin_print(node)
                if is_print and len(node.args) == 1 and not node.keywords:
                    argument = node.args[0]
                    key = results.get(argument.id) if isinstance(argument, ast.Name) else call_target(argument, values) if (
                        isinstance(argument, ast.Call) and original_helper(argument)) else None
                    if key is not None:
                        prints.append(OriginalReactionPrint(*key))
                        found[key]["stdout_expected"] = True
                        return
                    try:
                        value = values[argument.id] if isinstance(argument, ast.Name) else ast.literal_eval(argument)
                    except (KeyError, ValueError, TypeError, RecursionError):
                        pass
                    else:
                        prints.append(LiteralReactionPrint(str(value)))
                        return
            prints.append(UnprovedReactionPrint())

        values = {}
        for statement in tree.body:
            discover(statement, values)
        if steps > 4096:
            prints.append(UnprovedReactionPrint())
        else:
            steps = 0
            values, results = {}, {}
            for statement in tree.body:
                prove(statement, values, results)
        plans.append(prints)
    if not plans:
        unresolved.update(kind for name, kind in helpers.items() if re.search(r"\b" + name + r"\b", command))
    prints = plans[0] if len(plans) == 1 else [UnprovedReactionPrint()]
    return ReactionHistoryTrace(tuple(found.values()), frozenset(unresolved), tuple(prints))


def literal_reaction_calls(command: str) -> list[dict]:
    return list(_reaction_history_trace(command).calls)


def _literal_output_dict(line: str) -> dict | None:
    if len(line) > 20000:
        return None
    try:
        value = ast.literal_eval(line.strip())
    except (ValueError, SyntaxError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _reaction_printed_results(prints: tuple[ReactionPrint, ...], stdout: str, uid: str) -> dict:
    if len(stdout) > 2000000:
        return {}
    lines, position = stdout.splitlines(), 0
    matched: dict[tuple[str, str], list[dict]] = {}
    for step in prints:
        if isinstance(step, UnprovedReactionPrint):
            break
        if isinstance(step, LiteralReactionPrint):
            expected = (step.text + "\n").splitlines()
            if lines[position:position + len(expected)] != expected:
                break
            position += len(expected)
            continue
        if position >= len(lines):
            break
        value = _literal_output_dict(lines[position])
        kind, pid = step.kind, step.post_id
        try:
            ack = reaction_receipt(kind, pid, uid, value, historical=True)["reaction_ack"]
        except ReadProblem:
            break
        matched.setdefault((kind, pid), []).append(ack)
        position += 1
    for line in lines[position:]:
        value = _literal_output_dict(line)
        for kind, pid in matched:
            try:
                ack = reaction_receipt(kind, pid, uid, value, historical=True)["reaction_ack"]
            except ReadProblem:
                continue
            matched[(kind, pid)].append(ack)
    return matched


class Community:
    def __init__(self, backend, state_root: Path | None = None, catalog=None, *, contract_version: int = 1,
                 evidence=None, run_scope=None):
        self.backend = backend
        self.account = backend.configured_account()
        if run_scope is not None and (run_scope.account_uid != self.account.uid or run_scope.account_alias != self.account.alias):
            raise StateConflict("run_account_mismatch")
        if run_scope is not None and run_scope.contract_version != contract_version:
            raise StateConflict("run_contract_mismatch")
        self.state = State(self.account.uid, state_root, image_contract=contract_version >= 2)
        self.catalog = catalog
        self.contract_version = contract_version
        self.evidence = evidence
        self.run_scope = run_scope if contract_version >= 2 else None
        self._backend_calls = 0
        self._stop_reason = None
        if self.run_scope is not None:
            self.state.begin_run(self.run_scope)

    def _request(self, method: str, *args, **kwargs):
        if self._stop_reason is not None:
            raise ReadProblem("run_stopped")
        if self.run_scope is not None:
            self.state.consume_run_request(self.run_scope.run_id)
        self._backend_calls += 1
        try:
            return getattr(self.backend, method)(*args, **kwargs)
        except Exception as error:
            reason = error_data(error)
            if reason["code"] in {"captcha_stop", "session_expired", "rate_limited"}:
                self._stop_reason = reason
                if self.run_scope is not None:
                    try:
                        self.state.stop_run(self.run_scope.run_id, reason)
                    except Exception:
                        pass
            raise

    def result(self, kind: str, data=None, errors=None, coverage=None, *, delivery: dict | None = None,
               images: tuple[ImageIssue, ...] = ()) -> dict:
        problems = list(errors or [])
        if self.run_scope is not None:
            coverage = dict(coverage or {})
            try:
                coverage["run_budget"] = self.state.run_usage(self.run_scope.run_id)
            except Exception as error:
                coverage["run_budget"] = {"status": "read_failed", "error_type": type(error).__name__}
                problems.append({"code": "run_budget_unavailable", "type": type(error).__name__})
        result = envelope(kind, data, account=asdict(self.account), errors=problems, coverage=coverage)
        # The existing private collection profile is not copied into the public-result cache.
        if self.contract_version >= 2 and kind != "taste":
            try:
                return self.state.publish_view(result, delivery=delivery, images=images,
                                               run_id=self.run_scope.run_id if self.run_scope else None)
            except Exception as error:
                if self.run_scope is not None:
                    try:
                        self.state.stop_run(self.run_scope.run_id, {"code": "public_view_unavailable", "type": type(error).__name__})
                    except Exception:
                        pass
                summary = bounded_summary(data)
                safe_errors = [{key: value[:200 if key == "scope" else 80] for key, value in problem.items()
                                if key in {"code", "type", "scope", "origin"} and isinstance(value, str)} for problem in problems[:3]]
                safe_errors.append({"code": "public_view_unavailable", "type": type(error).__name__[:80]})
                return envelope(kind, summary, account=asdict(self.account), errors=safe_errors,
                                coverage={"full_history": False, "payload_state": "unavailable", "public_view_available": False,
                                          "state_projection": "partial" if summary else "unavailable",
                                          "error_records_omitted": max(0, len(problems) - 3)})
        if delivery:
            self.state.delivered(delivery["post_id"], delivery["cids"], delivery["scopes"])
        return result

    def _catalog(self):
        if self.catalog is None:
            self.catalog = PublicKnowledge()
        return self.catalog

    def _knowledge(self, text: str) -> dict:
        try:
            terms = self._catalog().lookup(text)
            return {"available": True, "status": "available", "terms": terms, "source": "alma-meme-watcher"}
        except Exception as error:
            return {"available": False, "status": "knowledge_unavailable", "terms": [], "source": "alma-meme-watcher",
                    "errors": [{"code": "knowledge_unavailable", "type": type(error).__name__}]}

    def _catalog_detail(self, detail: dict) -> dict:
        if self.catalog is False:
            return {}
        post = detail["post"]
        text = "\n".join([post["title"], post["body"]] + [node["text"] for node in detail["nodes"]])
        return self._knowledge(text)

    def _save_detail(self, detail: dict):
        records = [{"type": "detail", "data": detail["post"]}] + [{"type": "node", "data": node} for node in detail["nodes"]]
        self.state.ingest(records, scope=detail["scope"])

    def _origin_task(self, task: str, cursor: dict, session: dict, limit: int) -> tuple[list, dict, str]:
        lane = "comments" if task.startswith("comments") else "posts"
        head = task.endswith("head")
        saved = self.state.origin(lane)
        head_info = session.setdefault("heads", {}).get(lane, {"count": 0, "ids": []})
        overlap = min(5, max(1, head_info["count"] // 4)) if head_info["count"] else 0
        offset = 0 if head else head_info["count"] if not saved.get("offset") else max(head_info["count"], int(saved["offset"])) - overlap
        offset = max(0, offset)
        rows = self._request("own_comments" if lane == "comments" else "own_posts", offset, limit)
        records = profile_records(rows, self.account.uid, self.backend.clean) if lane == "comments" else self.backend.own_post_records(rows)
        ids = [row["data"].get("cid") or row["data"]["post_id"] for row in records]
        next_cursor = copy.deepcopy(cursor)
        next_cursor.pop("origin_update", None)
        stages = {"comments_head": "comments_deep", "comments_deep": "posts_head", "posts_head": "posts_deep", "posts_deep": "details"}
        next_cursor["task"] = stages[task]
        if head:
            session["heads"][lane] = {"count": len(rows), "ids": ids}
        else:
            repeated = bool(ids) and (set(ids) <= set(head_info["ids"]) or ids == saved.get("anchors"))
            if repeated:
                session["gaps"].append({"code": "profile_no_progress", "scope": f"{lane}/offset{offset}"})
            elif ids and saved.get("anchors") and not set(ids).intersection(saved["anchors"]):
                session["gaps"].append({"code": "profile_anchor_moved", "scope": f"{lane}/offset{offset}"})
            # Real APIs cap page size. Never infer end from len(rows) < limit.
            next_offset = 0 if not rows else offset + len(rows) + (overlap if repeated else 0)
            next_cursor["origin_update"] = {"lane": lane, "offset": next_offset, "anchors": ids}
            if not rows:
                session["scopes"].append({"scope": f"{lane}/offset{offset}", "end": "observed_empty_page"})
        if next_cursor["task"] == "details":
            recent = self.state.recent_posts(2)
            fair = self.state.retained_posts(100)
            next_cursor["queue"] = session.pop("deferred_subs", []) + [
                {"type": "detail", "post_id": pid, "page": 1} for pid in dict.fromkeys(recent + fair)]
            next_cursor["index"] = 0
        return records, next_cursor, f"own_{lane}/offset{offset}"

    def scan(self, area: str, budget: ReadBudget, resume: str | None = None, topics: tuple[str, ...] = ()) -> dict:
        start = time.monotonic()
        call_start = self._backend_calls
        requests, returned, errors, feed_results = 0, 0, [], []
        try:
            self._request("verify_account")
            requests += 1
            initial = {"cursor": {"task": "comments_head" if area == "incoming" else "feed", "topic_index": 0},
                       "pending": None, "scopes": [], "gaps": [], "heads": {}, "topics": list(topics)}
            session = self.state.start_scan(area, initial, resume=resume, seconds=budget.seconds)
        except Exception as error:
            return self.result("scan", errors=[error_data(error, area)])
        status, stop = "partial", "budget"
        try:
            while requests < budget.requests and returned < budget.rows and time.monotonic() - start < budget.seconds:
                if session.get("pending"):
                    returned += self.state.consume_staged(session, budget.rows - returned)
                    continue
                cursor = session["cursor"]
                task = cursor["task"]
                if task == "done":
                    status, stop = "done", "bounded_cycle_complete"
                    break
                captured = time.time()
                requests += 1
                if task == "comments_head" and area == "incoming":
                    # The platform queue names sender, root and post directly, so it
                    # finds replies under floors the profile scan never reaches. Import
                    # it first: otherwise the ledger can report itself clear while a
                    # reply is still sitting unread on an older post.
                    queued = self._request("messages", 20, 3)
                    imported = self.state.ingest_messages(queued)
                    session.setdefault("scopes", []).append({"scope": "user_message/queue",
                        "captured_at": captured, "rows": len(queued), "imported": imported})
                    next_cursor = dict(cursor, task="comments_deep")
                    session["pending"] = {"rows": [], "position": 0, "scope": "user_message/queue",
                                          "source": "user_message", "captured_at": captured,
                                          "next": next_cursor}
                    self.state.checkpoint(session)
                    self.state.consume_staged(session, 0)
                    continue
                if task in {"comments_head", "comments_deep", "posts_head", "posts_deep"}:
                    records, next_cursor, scope = self._origin_task(task, cursor, session, 20)
                    source = "own_profile"
                elif task == "feed":
                    available = session.get("topics") or ["7214"]
                    index = cursor.get("topic_index", 0)
                    if index >= len(available):
                        session["cursor"] = dict(cursor, task="done")
                        self.state.checkpoint(session)
                        requests -= 1
                        continue
                    topic = available[index]
                    origin = self.state.origin("feed:" + topic)
                    offset = int(origin.get("offset", 0))
                    rows = self._request("feed", topic, offset, 20)
                    records = []
                    for row in rows:
                        pid = id_value(row, "link_id", "linkid", "id")
                        if not pid:
                            raise ReadProblem("invalid_feed_row", topic)
                        prior = self.state.post(pid)
                        post = {"post_id": pid, "ref": "p:" + pid, "title": row.get("title") or "",
                                "body": row.get("desc") or "", "author_name": row.get("author") or "", "author_uid": "",
                                "award_num": row.get("award_num", 0), "comment_num": row.get("comment_num", 0),
                                "previously_summarized": bool(prior and prior["feed_at"]),
                                "detail_returned": bool(prior and prior["detail_at"])}
                        records.append({"type": "feed", "data": post})
                        feed_results.append(post)
                    next_cursor = dict(cursor, topic_index=index + 1,
                        origin_update={"lane": "feed:" + topic, "offset": offset + len(rows) if rows else 0,
                                       "anchors": [row["data"]["post_id"] for row in records]})
                    scope, source = f"feed/{topic}/offset{offset}", "feed"
                else:
                    queue = cursor.get("queue", [])
                    index = cursor.get("index", 0)
                    if index >= len(queue):
                        session["cursor"] = dict(cursor, task="done")
                        self.state.checkpoint(session)
                        requests -= 1
                        continue
                    item = queue[index]
                    pid = item["post_id"]
                    try:
                        if item["type"] == "detail":
                            detail = self._request("detail", pid, item.get("page", 1), 50)
                            records = [{"type": "detail", "data": detail["post"]}] + [{"type": "node", "data": node} for node in detail["nodes"]]
                            scope = detail["scope"]
                            session["gaps"].extend(detail["warnings"])
                            session["gaps"].append({"code": "floor_scope_limited", "scope": scope})
                            roots = set(self.state.roots_for(pid))
                            seen_roots = {node["root_id"] for node in detail["nodes"]}
                            children = set(detail["truncated_roots"]) | (roots - seen_roots)
                            next_cursor = copy.deepcopy(cursor)
                            next_cursor["queue"][index + 1:index + 1] = [
                                {"type": "sub", "post_id": pid, "root_id": root, "lastval": None}
                                for root in sorted(children) if root]
                            self._catalog_detail(detail)
                        else:
                            sub = self._request("sub_comments", pid, item["root_id"], item.get("lastval"))
                            records = [{"type": "node", "data": node} for node in sub["nodes"]]
                            scope = sub["scope"]
                            next_cursor = copy.deepcopy(cursor)
                            if not sub["terminal_proved"]:
                                session["gaps"].append({"code": "subcomment_end_unproved", "scope": scope})
                                if sub["next_cursor"] and sub["next_cursor"] != item.get("lastval"):
                                    next_cursor["queue"].append(dict(item, lastval=sub["next_cursor"]))
                        next_cursor["index"] = index + 1
                        self.state.mark_attempted(pid, True)
                    except Exception as error:
                        self.state.mark_attempted(pid, False)
                        problem = error_data(error, pid)
                        errors.append(problem)
                        session["gaps"].append(problem)
                        session["cursor"] = dict(cursor, index=index + 1)
                        if isinstance(error, RunLimit) or problem["code"] in {"captcha_stop", "session_expired", "rate_limited"}:
                            stop = problem["code"]
                            break
                        self.state.checkpoint(session)
                        continue
                    source = "live"
                captured = time.time()
                session["scopes"].append({"scope": scope, "captured_at": captured, "rows": len(records)})
                session["pending"] = {"rows": records, "position": 0, "scope": scope, "source": source,
                                      "captured_at": captured, "next": next_cursor}
                self.state.checkpoint(session)
                if not records:
                    self.state.consume_staged(session, 0)
            # A staged page can be consumed without another network request.
            if session.get("pending") and returned < budget.rows:
                returned += self.state.consume_staged(session, budget.rows - returned)
            if session["cursor"].get("task") == "done":
                status, stop = "done", "bounded_cycle_complete"
            self.state.checkpoint(session, status)
        except Exception as error:
            problem = error_data(error, area)
            errors.append(problem)
            session["gaps"].append(problem)
            try:
                self.state.checkpoint(session, "partial")
            except StateConflict:
                pass
            stop = problem["code"]
        coverage = {"status": "blocked" if stop in {"captcha_stop", "session_expired", "rate_limited"} or stop.startswith("run_") else status,
                    "stop": stop, "scopes": session["scopes"], "gaps": session["gaps"], "full_history": False,
                    "resume": session["_scan"] if status != "done" else None,
                    "usage": {"requests": self._backend_calls - call_start if self.contract_version >= 2 else requests,
                              "rows": returned, "seconds": round(time.monotonic() - start, 3)}}
        data: dict[str, object] = self.state.inbox() if area == "incoming" else {"posts": list({post["post_id"]: post for post in feed_results}.values())}
        if area == "incoming" and self.contract_version >= 2:
            try:
                data["recent_activity"] = self.state.recent_activity()
            except Exception as error:
                data["recent_activity"] = {"data_only": True, "source": "local_public_ledger", "status": "read_failed",
                                           "items": [], "full_history": False, "error_type": type(error).__name__}
        return self.result("scan", data, errors, coverage)

    def _prepare_assets(self, rows: list[dict], field: str, budget: ReadBudget, used: int) -> tuple[tuple[ImageIssue, ...], int]:
        issues, processed = [], set()
        for row in rows:
            source = row.get(field)
            if source in processed:
                continue
            processed.add(source)
            row["image_status"] = "not_observed"
            if used >= budget.requests:
                row["download_error"] = "image_request_budget"
                continue
            before = self._backend_calls
            try:
                prepared = self._request("prepare_image", source)
                issues.append(ImageIssue(source, prepared))
                row.pop("download_error", None)
            except Exception as error:
                row["download_error"] = error_data(error)["code"]
            used += self._backend_calls - before
        return tuple(issues), used

    def _read_detail(self, resource: str, budget: ReadBudget) -> dict:
        incoming = self.state.incoming(resource[2:]) if resource.startswith("i:") else None
        if resource.startswith("i:") and incoming is None:
            raise ReadProblem("incoming_not_discovered", resource)
        pid = incoming["post_id"] if incoming else resource[2:]
        if budget.requests < 2:
            raise ReadProblem("insufficient_read_budget", resource)
        if self.contract_version >= 2:
            self.state.invalidate_delivery(pid)
        self._request("verify_account")
        detail = self._request("detail", pid, 1, 50)
        self._save_detail(detail)
        scopes, requests = [detail["scope"]], 2
        observed = {node["cid"] for node in detail["nodes"]}
        target_seen = incoming is None or incoming["cid"] in observed
        if incoming and not target_seen and requests < budget.requests:
            sub = self._request("sub_comments", pid, incoming["root_id"])
            requests += 1
            self.state.ingest([{"type": "node", "data": node} for node in sub["nodes"]], scope=sub["scope"])
            detail["nodes"].extend(sub["nodes"])
            observed.update(node["cid"] for node in sub["nodes"])
            scopes.append(sub["scope"])
            target_seen = incoming["cid"] in observed
        asset_access, image_issues = "legacy_backend_result", ()
        if self.contract_version >= 2:
            post = detail["post"]
            # Reply targets are text-only, so preparing post images for them spends
            # budget on an attachment that can never be sent and leaves the caller
            # holding an image confirmation it has no use for.
            if incoming is None:
                image_issues, requests = self._prepare_assets(post["assets"], "url", budget, requests)
                asset_access = "prepared_source_snapshots" if image_issues else "no_post_assets" if not post["assets"] else "required_images_unavailable"
            else:
                asset_access = "reply_text_only"
        else:
            post = self._request("asset_files", detail["post"])
        self.state.ingest([{"type": "detail", "data": post}], scope=detail["scope"])
        self.state.observe_visibility(pid, observed, {"scopes": scopes, "full_history": False})
        floor = self.state.floor(pid, incoming["root_id"]) if incoming else detail["nodes"]
        data = {"post": post, "floor": [dict(node, currently_observed=node["cid"] in observed) for node in floor],
                "incoming": self.state.incoming(incoming["cid"]) if incoming else None,
                "target_currently_observed": target_seen, "knowledge": self._catalog_detail(detail)}
        coverage = {"scopes": scopes, "requests": requests, "full_history": False,
                    "warnings": detail["warnings"], "floor_scope": "returned_pages_only"}
        if self.contract_version >= 2:
            coverage["asset_access"] = asset_access
        delivered = {node["cid"] for node in data["floor"] if node["currently_observed"]}
        # Mark reply targets text-only so the view records no required images: the
        # nested-reply protocol carries text only.
        return self.result("detail", data, coverage=coverage,
                           delivery={"post_id": pid, "cids": delivered, "scopes": scopes,
                                     "text_only": incoming is not None}, images=image_issues)

    def _refresh_operation(self, operation: dict, budget: ReadBudget) -> dict:
        if operation.get("kind") in REACTIONS:
            return self.result("operation", operation, coverage={"full_history": False, "reaction_effect": "not_observed",
                                                                 "effect_refresh_supported": False})
        pid = operation["post_id"]
        is_operation = "operation_id" in operation
        cid = operation["returned_cid"] if is_operation else operation["cid"]
        kind = "operation" if is_operation else "comment"
        observed, scopes, problems, read_error = set(), [], [], None
        try:
            if budget.requests < 2:
                raise ReadProblem("insufficient_read_budget", operation["ref"])
            self._request("verify_account")
            detail = self._request("detail", pid, 1, 50)
            self._save_detail(detail)
            scopes.append(detail["scope"])
            observed.update(node["cid"] for node in detail["nodes"])
            if operation["root_id"] and cid not in observed and budget.requests > 2:
                sub = self._request("sub_comments", pid, operation["root_id"])
                self.state.ingest([{"type": "node", "data": node} for node in sub["nodes"]], scope=sub["scope"])
                observed.update(node["cid"] for node in sub["nodes"])
                scopes.append(sub["scope"])
        except Exception as error:
            read_error = type(error).__name__
            problems.append(error_data(error, pid))
        try:
            self.state.observe_visibility(pid, observed, {"scopes": scopes, "full_history": False}, read_error)
        except Exception as error:
            problems.append({"code": "state_projection_unavailable", "type": type(error).__name__})
        try:
            projection = self.state.operation(operation["operation_id"]) if is_operation else self.state.comment(cid)
        except Exception as error:
            projection = bounded_summary(operation)
            problems.append({"code": "state_projection_unavailable", "type": type(error).__name__})
        return self.result(kind, projection, errors=problems, coverage={"scopes": scopes, "full_history": False})

    def read(self, resource: str, budget: ReadBudget, query: str = "", include_handled: bool = False,
             limit: int = 30, refresh: bool = False, page: int = 1) -> dict:
        try:
            if resource.startswith("v:"):
                return self.state.view_page(resource[2:], page)
            if resource == "inbox":
                return self.result("inbox", self.state.inbox(include_handled, limit), coverage=self.state.latest_coverage())
            if resource == "account":
                self._request("verify_account")
                return self.result("account", {"configured_identity": self.account.uid, "api_identity_verified": True})
            if resource == "shortcodes":
                return self.result("shortcodes", self._request("shortcodes"))
            if resource == "stickers":
                candidates = self._request("stickers", query)
                images, used = self._prepare_assets(candidates, "file", budget, 1) if self.contract_version >= 2 else ((), 1)
                return self.result("stickers", candidates, images=images,
                                   coverage={"requests": used, "visual_scope": "first_frame_preview", "animation_observed": False})
            if resource == "taste":
                return self.result("taste", self._request("taste"))
            if resource == "knowledge":
                knowledge = self._knowledge(query)
                return self.result("knowledge", knowledge, errors=knowledge.get("errors"))
            if resource.startswith(("p:", "i:")):
                return self._read_detail(resource, budget)
            if resource.startswith("c:"):
                comment = self.state.comment(resource[2:])
                if comment is None:
                    raise ReadProblem("comment_not_recorded", resource)
                return self._refresh_operation(comment, budget) if refresh else self.result("comment", comment)
            if resource.startswith("op:"):
                operation = self.state.operation(resource[3:])
                if operation is None:
                    raise ReadProblem("operation_not_found", resource)
                return self._refresh_operation(operation, budget) if refresh else self.result("operation", operation)
            raise ReadProblem("invalid_resource", resource)
        except Exception as error:
            return self.result("read", errors=[error_data(error, resource)])

    def _dispatch_action(self, target: dict, payload: dict, action: Action, confirmation: dict | None,
                         image_plan: ImagePlan | None, image_proofs: tuple[dict, ...]) -> dict:
        result, problems = None, []
        try:
            reservation = self.state.reserve(target, payload, run_id=self.run_scope.run_id if self.run_scope else None,
                                             context_keys=action.context_keys if confirmation else None,
                                             image_plan=image_plan, image_proofs=image_proofs)
            result = reservation
            if not reservation["fresh"]:
                return self.result("action", reservation)
            operation = reservation["operation_id"]
            try:
                self._backend_calls += 1
                manager = getattr(self.backend, "dispatch_intent", None)
                with manager(self.state, operation) if manager else nullcontext():
                    receipt = self.backend.send(target, payload)
            except PreflightRefusal as error:
                problems = [error.data] if error.data.get("stop") else []
                result = self.state.record_result(operation, refusal=error.data)
                return self.result("action", result, errors=problems)
            except DispatchProblem as error:
                problems = [error.data]
                cid = id_value(error.receipt or {}, "comment_id")
                if cid and int(cid) > 0:
                    result = dict(reservation, state="returned", returned_cid=cid, receipt_persistence="unavailable")
                result = self.state.record_result(operation, receipt=error.receipt, error=error.data)
                return self.result("action", result, errors=problems, coverage={"stop": error.data["code"]})
            except Exception as error:
                problems = [{"code": "dispatch_uncertain", "type": type(error).__name__, "stop": True}]
                result = self.state.record_result(operation, error=problems[0])
                return self.result("action", result, errors=problems, coverage={"stop": "dispatch_uncertain"})
            cid = id_value(receipt, "comment_id") if isinstance(receipt, dict) else ""
            if cid and int(cid) > 0:
                result = dict(reservation, state="returned", returned_cid=cid, receipt_persistence="unavailable")
            result = self.state.record_result(operation, receipt=receipt)
            if result["state"] == "returned":
                return self._refresh_operation(result, ReadBudget(3, 300, 40))
            return self.result("action", result)
        except Exception as error:
            problem = error_data(error, action.resource)
            if result is not None and self.run_scope is not None:
                try:
                    self.state.stop_run(self.run_scope.run_id, problem)
                except Exception:
                    pass
            return self.result("action", bounded_summary(result), errors=problems + [problem])

    def act(self, action: Action) -> dict:
        result = None
        try:
            if action.kind not in {"comment", "reply", "close", "defer"} | REACTIONS:
                raise ReadProblem("invalid_action", action.resource)
            reaction = action.kind in REACTIONS
            if reaction and self.contract_version < 2:
                raise ReadProblem("reaction_not_active", action.resource)
            if reaction and not re.fullmatch(r"p:[1-9][0-9]{0,31}", action.resource):
                raise ReadProblem("invalid_action_target", action.resource)
            if reaction and (action.text or any(value is not None for value in (action.image_file, action.image_url, action.without_image_reason))):
                raise ReadProblem("reaction_payload_unsupported", action.resource)
            if action.kind in {"close", "defer"}:
                return self.result("disposition", self.state.dispose(action.resource[2:], action.kind))
            incoming = self.state.incoming(action.resource[2:]) if action.kind == "reply" else None
            if action.kind == "reply" and incoming is None:
                raise ReadProblem("incoming_not_discovered", action.resource)
            pid = incoming["post_id"] if incoming else action.resource[2:]
            post = self.state.post(pid)
            if not post or not post["delivered_at"] or time.time() - post["delivered_at"] > 900:
                raise ReadProblem("detail_required", "p:" + pid)
            if incoming and incoming["handled"]:
                return self.result("action", {"status": "already_handled", "incoming": action.resource})
            if incoming and incoming["cid"] not in self.state.delivery(pid)["cids"]:
                raise ReadProblem("target_not_currently_observed", action.resource)
            confirmation = None
            if self.contract_version >= 2:
                pages = self.state.context_pages(pid, action.context_keys)
                if self.evidence is not None and not self.evidence.context_pages_received(pages):
                    # Name the pages to replay instead of only naming the gate.
                    view = pages[0]["context"]["view_ref"]
                    need = " ; ".join(f"read {view} --page {i}" for i in range(1, len(pages) + 1))
                    raise ReadProblem("host_context_not_confirmed: 先把这些页的输出完整回显到本次 Bash stdout（不要 tail、重定向或二次解析），再重试 act。需要：" + need, action.resource)
                confirmation = {"view_ref": pages[0]["context"]["view_ref"], "pages": len(pages),
                                "status": "host_saved_pages" if self.evidence is not None else "caller_acknowledged",
                                "provider_bytes_observed": False}
            if not reaction and (not action.text.strip() or len(action.text) > 1000):
                raise ReadProblem("platform_text_limit", action.resource)
            if action.kind == "reply" and (action.image_file is not None or action.image_url is not None):
                raise ReadProblem("reply_image_unsupported", action.resource)
            if action.image_url is not None and not re.fullmatch(r"https?://[^\s]+", action.image_url):
                raise ReadProblem("invalid_image_url", action.resource)
            self._request("verify_account")
            target = {"kind": action.kind, "post_id": pid, "incoming_cid": incoming["cid"] if incoming else "",
                      "root_cid": incoming["root_id"] if incoming else ""}
            payload = {} if reaction else {"text": action.text, "image_file": action.image_file, "image_url": action.image_url,
                                           "without_image_reason": action.without_image_reason}
            if confirmation is not None:
                payload["context_confirmation"] = confirmation
            plan, proofs = None, ()
            # A nested reply is text-only on this platform: the app protocol has no
            # image field for it. Running the image plan anyway demanded that the
            # main post image be prepared and confirmed before a plain text reply
            # could go out, which made every reply on an image post fail. Skip the
            # whole image path for replies instead of validating an option that
            # cannot be sent.
            if self.contract_version >= 2 and action.kind != "reply":
                plan = self.state.image_plan(pid, action.context_keys, self.run_scope.run_id if self.run_scope else "",
                                             image_file=action.image_file, image_url=action.image_url)
                if plan.selected is not None:
                    payload["image_file"], payload["image_url"] = plan.selected["snapshot_file"], None
            required = plan.required if plan else ()
            if required and self.evidence is None:
                raise ReadProblem("native_image_evidence_required", action.resource)
            lease_manager = self.backend.lease_images(required) if required else nullcontext({"cleanup_errors": []})
            with lease_manager as lease:
                if required:
                    evidence = self.evidence
                    if evidence is None:
                        raise ReadProblem("native_image_evidence_required", action.resource)
                    proofs = tuple(evidence.image_bytes_received(asset) for asset in required)
                    if any(proof.get("status") != "host_preview_bytes_verified" for proof in proofs):
                        raise ReadProblem("native_image_bytes_not_confirmed", action.resource)
                    payload["image_confirmation"] = {"proofs": list(proofs), "scope": "main_post_and_selected_attachment",
                                                      "animation_observed": False, "provider_bytes_observed": False}
                response = self._dispatch_action(target, payload, action, confirmation, plan, proofs)
                result = response.get("data")
            cleanup = lease.get("cleanup_errors") or []
            if cleanup:
                response["errors"] = list(response.get("errors") or []) + cleanup[:3]
                response["ok"] = False
                response.pop("context", None)
                response["coverage"] = dict(response.get("coverage") or {}, stop="image_lease_cleanup_failed")
                if self.run_scope is not None:
                    try:
                        self.state.stop_run(self.run_scope.run_id, {"code": "image_lease_cleanup_failed"})
                    except Exception as error:
                        response["errors"].append({"code": "run_stop_unavailable", "type": type(error).__name__})
            return response
        except Exception as error:
            problem = error_data(error, action.resource)
            if result is not None and self.run_scope is not None:
                try:
                    self.state.stop_run(self.run_scope.run_id, problem)
                except Exception:
                    pass
            return self.result("action", bounded_summary(result), errors=[problem])

    def _import_reactions(self, evidence: dict) -> dict:
        counts = {"reaction_targets_acknowledged": 0, "reaction_targets_uncertain": 0,
                  "reaction_targets_unattributed": 0, "reaction_foreign_targets": 0, "reaction_unparsed_scopes": 0}
        for thread in evidence.get("threads", []):
            for event in thread.get("events", []):
                source_key = thread["thread_id"] + ":" + event["call_id"]
                captured_at = timestamp(event.get("time"))
                command = str((event.get("input") or {}).get("command") or "")
                trace = _reaction_history_trace(command)
                calls, unresolved = trace.calls, trace.unresolved_kinds
                if not calls and not unresolved:
                    continue
                binding = event.get("account_binding")
                bound = (isinstance(binding, dict) and isinstance(binding.get("uid"), str)
                         and re.fullmatch(r"[1-9][0-9]{0,31}", binding["uid"]) is not None
                         and isinstance(binding.get("alias"), str) and binding["alias"] in {"alt", "main"})
                foreign = bound and binding["uid"] != self.account.uid
                for kind in REACTIONS:
                    if foreign or (kind not in unresolved and any(call["kind"] == kind for call in calls)):
                        self.state.resolve_reaction_gap(kind, source_key, captured_at)
                    elif kind in unresolved:
                        self.state.observe_reaction_gap(kind, source_key, captured_at)
                        counts["reaction_unparsed_scopes"] += 1
                outputs = _reaction_printed_results(trace.prints, str(event.get("stdout") or ""), self.account.uid)
                for call in calls:
                    target = {key: call[key] for key in ("kind", "post_id", "root_cid", "incoming_cid")}
                    if foreign:
                        self.state.resolve_reaction_history(target, source_key, binding["uid"])
                        counts["reaction_foreign_targets"] += 1
                        continue
                    if not bound:
                        self.state.observe_unbound_reaction(target, source_key, captured_at)
                        counts["reaction_targets_unattributed"] += 1
                        continue
                    matching = outputs.get((call["kind"], call["post_id"]), [])
                    ack = matching[0] if len(matching) == 1 else None
                    self.state.import_reaction(target, source_key, captured_at, ack)
                    counts["reaction_targets_acknowledged" if ack else "reaction_targets_uncertain"] += 1
        return counts

    def import_evidence(self, evidence_path: Path, floors_path: Path, profile_path: Path) -> dict:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        floors = json.loads(floors_path.read_text(encoding="utf-8"))
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if floors.get("owner_userid") != self.account.uid or floors.get("account") != self.account.alias or profile.get("account") != self.account.alias:
            raise ReadProblem("migration_identity_mismatch")
        reaction_counts = self._import_reactions(evidence)
        events = {(thread["thread_id"], event["call_id"]): event for thread in evidence.get("threads", []) for event in thread.get("events", [])}
        returned_targets: dict[tuple[str, str], set[str]] = {}
        target_bound, target_unbound, uncertain = 0, 0, 0
        for send in evidence.get("sends", []):
            pid, cid = str(send.get("link_id") or ""), str(send.get("comment_id") or "")
            if not pid.isdigit() or not cid.isdigit():
                continue
            event = events.get((send["thread_id"], send["call_id"]), {})
            calls = literal_send_calls(str((event.get("input") or {}).get("command") or ""))
            root = str(send.get("root_id") or "")
            matching = [call for call in calls if call["post_id"] == pid and call["root_cid"] == root]
            candidates = {call["incoming_cid"] for call in matching}
            target = next(iter(candidates)) if len(candidates) == 1 else ""
            proof = "request" if target or (send.get("kind") == "comment" and "send_comment" in event.get("functions", [])) else "unknown"
            target_bound += bool(target)
            target_unbound += send.get("kind") == "reply" and not target
            if len(candidates) == 1:
                returned_targets.setdefault((send["thread_id"], send["call_id"]), set()).update(
                    json.dumps(call, sort_keys=True) for call in matching)
            self.state.import_receipt({"cid": cid, "post_id": pid, "target_cid": target, "root_id": root,
                "text": send.get("text") or "", "image_url": send.get("image_url") or "",
                "captured_at": timestamp(send.get("time")), "target_proof": proof})
        for thread in evidence.get("threads", []):
            for event in thread.get("events", []):
                command = str((event.get("input") or {}).get("command") or "")
                calls = literal_send_calls(command)
                output = str(event.get("stderr") or "") + str(event.get("stdout") or "")
                if len(calls) == 1 and re.search(r"ValueError: (?:配不配图还没决定。|还没决定配哪张。|短码 )", output):
                    continue
                returned = returned_targets.get((thread["thread_id"], event["call_id"]), set())
                for target in calls:
                    if json.dumps(target, sort_keys=True) in returned:
                        continue
                    self.state.import_uncertain(target, thread["thread_id"] + ":" + event["call_id"], timestamp(event.get("time")))
                    uncertain += 1
        self.state.ingest(profile_records(profile.get("comments", []), self.account.uid, self.backend.clean),
                          now=timestamp(profile.get("captured_at")), source="own_profile", scope="historical_profile")
        for post in floors.get("own_posts", []):
            self.state.ingest(self.backend.own_post_records([post]), now=timestamp(floors["captured_at"]), source="historical_own_posts")
        floor_time = timestamp(floors["captured_at"])
        for post in floors.get("posts", []):
            pid = str(post["link_id"])
            records, observed = [], set()
            for floor in post.get("own_floors", []):
                root = str(floor["root_id"])
                for node in floor.get("nodes", []):
                    records.append({"type": "node", "data": {"cid": str(node["comment_id"]), "post_id": pid, "root_id": root,
                        "author_uid": str(node.get("userid") or ""), "author_name": node.get("author") or "", "text": node.get("text") or "",
                        "reply_id": str(node.get("reply_id") or ""), "reply_uid": str(node.get("reply_userid") or ""),
                        "created_at": node.get("create_at") or 0, "images": node.get("images") or []}})
                    observed.add(str(node["comment_id"]))
            self.state.ingest(records, now=floor_time, source="historical_live_floor", scope=f"{pid}/archived-page{floors.get('page', 1)}")
            self.state.observe_visibility(pid, observed, {"page": floors.get("page", 1), "limit": floors.get("limit"), "archive": True},
                                          captured_at=floor_time, source="historical_live_floor")
        return {"imported": True, "counts": self.state.counts(), "reply_targets_bound_from_invocation": target_bound,
                "reply_targets_initially_unbound": target_unbound, "uncertain_event_targets": uncertain,
                "owner_removals_inferred": 0, "public_requests": 0, **reaction_counts}

