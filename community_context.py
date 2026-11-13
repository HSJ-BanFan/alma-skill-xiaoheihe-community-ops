"""Job-scoped, read-only private source data for Alma's native willSend hook."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from functools import lru_cache
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import NotRequired, TypedDict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from community_admin import JOB_ID, MARKER, NativeAPI, get_job

CONFIG = Path.home() / ".config/alma"
CHAT_DB = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "alma/chat_threads.db"
LOG = CONFIG / "state/community-context.jsonl"
MAX_EVENT_CHARS = 800_000
SOURCE_CONTRACT_MARKER = "[xhh-community-boundary:2]"
NATIVE_ORIGIN_FILES = {
    Path(r"D:/03_Software/IM/Alma/alma-server/lib/server.mjs"): "0187c7c545ff47b57d7c7f1fa008496f8b32e4b127b8f309babbed1e61ae101e",
    Path(r"D:/03_Software/IM/Alma/renderer/assets/mermaid-NA5CF7SZ-C9eqadOO.js"): "832e3a5e1ef28e14ec7b81816a2b7d6806ad5b4c4413e1ee26446be1a4045152",
    Path(r"D:/03_Software/IM/Alma/alma-server/resources/cli/alma"): "22d4b90e0209b41d591af51471bd63ed20d244ce0c87b7810530b35e3a8548ff",
}
AUTOMATIC_SOURCE_FLAGS = ("automated", "isCompactionIndicator", "isCompactionSummary", "personalNudge",
    "personalApprovalId", "personalRunId", "personalDispatch", "personalBusy", "almaLoopTurn", "almaGoalTurn")


class ConversationRecord(TypedDict):
    message_id: str
    role: str
    created_at: object
    text: str
    native_completion_sources: NotRequired[list[str]]
    omitted_prefix_chars: NotRequired[int]


class SourceProblem(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def object_value(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value or "{}")
    return value if isinstance(value, dict) else {}


_REDACTIONS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:[A-Za-z0-9_]*(?:api[_-]?key|token|secret|password|pkey|credential)[A-Za-z0-9_]*|cookie|authorization)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s;,]+)", re.I),
    re.compile(r"(?:密码|密钥)\s*[:：=]\s*[^\s;,，。]+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"[\"'][A-Za-z]:[\\/][^\"'\r\n]+[\"']"),
    re.compile(r"(?:[A-Za-z]:[\\/]|~[/\\]|(?<!\w)/(?:Users|home)/)[^\s<>\"']+"),
)


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pattern in _REDACTIONS:
        text, changed = pattern.subn("[private value redacted]", text)
        count += changed
    return text, count


def programmatic(metadata: dict) -> bool:
    personal = metadata.get("personal")
    if metadata.get("isCompactionIndicator") or isinstance(personal, dict) and personal.get("type") == "routine":
        return True
    source = metadata.get("source")
    if isinstance(source, dict):
        source = source.get("type") or source.get("kind")
    return isinstance(source, str) and source in {"cron", "heartbeat", "personal", "personal-agent"}


def visible_conversation_parts(message: dict) -> tuple[str, list[str], int]:
    parts = message["parts"]
    plain = [part["text"] for part in parts if isinstance(part, dict) and
             part.get("type") == "text" and isinstance(part.get("text"), str)]
    duplicates = set(plain) | {"\n".join(plain)}
    segments, sources, omitted = [], [], 0
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            segments.append(part["text"])
            continue
        output = part.get("output") if isinstance(part, dict) else None
        if (message["role"] == "assistant" and isinstance(part, dict) and
                part.get("type") == "tool-AttemptCompletion" and part.get("state") == "output-available" and
                isinstance(output, dict) and output.get("success") is True):
            inputs = part.get("input")
            result = inputs.get("result") if isinstance(inputs, dict) else None
            field = "input"
            if not isinstance(result, str) or not result:
                result, field = output.get("result"), "output"
            if isinstance(result, str) and result.strip():
                sources.append("AttemptCompletion." + field + ".result")
                if result not in duplicates:
                    segments.append(result)
                continue
        omitted += 1
    return "\n".join(segments), sources, omitted


def emotion_record(path: Path, hours: int) -> dict:
    result = {"status": "unavailable", "native_window_hours": hours}
    if not path.is_file():
        return result
    with path.open(encoding="utf-8-sig") as stream:
        text = stream.read(32_000)
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        return dict(result, status="invalid_record")
    fields = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"mood", "energy", "valence", "updated"}:
            fields[key.strip()] = value.strip().strip("\"'")
    updated = fields.get("updated", "")
    try:
        captured = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return dict(result, status="undated_record")
    age = time.time() - captured
    result.update({"record_updated_at": updated, "age_seconds": round(age),
                   "status": "future_record" if age < -60 else "stale" if age >= hours * 3600 else "record_in_window"})
    if result["status"] == "record_in_window":
        values = {}
        for key in ("energy", "valence"):
            try:
                number = float(fields[key])
            except (KeyError, ValueError):
                continue
            if math.isfinite(number):
                values[key] = number
        if fields.get("mood"):
            values["mood"] = redact(fields["mood"])[0][:64]
        result["recorded_values"] = values
    return result


def _source_value(metadata: dict):
    value = metadata.get("source")
    if isinstance(value, dict):
        value = value.get("type") or value.get("kind")
    return value


def _automatic_source(metadata: dict) -> bool:
    if programmatic(metadata) or any(metadata.get(key) for key in AUTOMATIC_SOURCE_FLAGS):
        return True
    if metadata.get("personal"):
        return True
    source = _source_value(metadata)
    return source is not None and source != "app"


@lru_cache(maxsize=128)
def _known_native_timezone(value: str) -> bool:
    if not value or value == "unknown" or len(value) > 100 or any(char in value for char in "\x00\r\n"):
        return False
    executable = shutil.which("node")
    if executable is None:
        return False
    # Own inert validator, not Alma or any sample. Timezone is JSON stdin, never JavaScript.
    code = ('let s="";process.stdin.setEncoding("utf8");process.stdin.on("data",x=>s+=x);'
            'process.stdin.on("end",()=>{try{const z=JSON.parse(s);'
            'const c=new Intl.DateTimeFormat("en",{timeZone:z}).resolvedOptions().timeZone;'
            'process.stdout.write(c===z?"1":"0");}'
            'catch{process.stdout.write("0");}});')
    environment = dict(os.environ)
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    try:
        outcome = subprocess.run([executable, "-e", code], input=json.dumps(value), text=True, encoding="utf-8",
            capture_output=True, timeout=2, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        return False
    return outcome.returncode == 0 and outcome.stdout == "1"


def _desktop_user_signature(metadata: dict, embedded: dict, *, parts_is_array: bool) -> bool:
    zone = metadata.get("sentAtTimeZone")
    return (parts_is_array and metadata.get("source") == "app" and isinstance(zone, str) and _known_native_timezone(zone)
            and not _automatic_source(metadata) and not _automatic_source(embedded))


def _user_time(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        stamp = parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None
    return stamp if stamp is not None and math.isfinite(stamp) else None


def _native_emotion_time(record: dict) -> dict:
    if "record_updated_at" not in record or _user_time(record["record_updated_at"]) is not None:
        return record
    result = {key: value for key, value in record.items() if key not in {"recorded_values", "age_seconds"}}
    result["status"] = "undated_record"
    return result


class SourceReader:
    def __init__(self, db: Path = CHAT_DB, emotions: Path = CONFIG / "emotions", job_loader=None,
                 max_records: int = 12, max_chars: int = 12_000):
        self.db, self.emotions = db, emotions
        self.job_loader = job_loader or (lambda: get_job(NativeAPI(timeout=2)))
        self.max_records, self.max_chars = max_records, max_chars
        self.diagnostic: dict[str, object] = {"status": "not_matched"}

    def _conversation(self, conn, source_id: str, *, desktop_mode: bool = False) -> dict:
        row = conn.execute("select metadata from chat_threads where id=?", (source_id,)).fetchone()
        if not row:
            raise SourceProblem("source_thread_missing")
        metadata = object_value(row[0])
        if metadata.get("isCron"):
            raise SourceProblem("source_is_cron_thread")
        active = metadata.get("activePath")
        if not isinstance(active, list) or not all(isinstance(value, str) for value in active):
            return {"status": "active_path_unavailable", "records": [], "omissions": {}}
        tail = list(dict.fromkeys(active[-400:]))
        indexed = {}
        for start in range(0, len(tail), 200):
            ids = tail[start:start + 200]
            slots = ",".join("?" for _ in ids)
            columns = "id,message,metadata,created_at" + (",parent_tool_call_id" if desktop_mode else "")
            for message in conn.execute(f"select {columns} from chat_messages where thread_id=? and id in ({slots})", [source_id] + ids):
                indexed[message["id"]] = message
        omitted = {"outside_active_tail": max(0, len(active) - len(tail)), "programmatic": 0,
                   "nonconversation": 0, "nontext_parts": 0, "missing_or_invalid": 0, "record_budget": 0, "character_budget": 0}
        records: list[ConversationRecord] = []
        automatic_turn = desktop_mode
        for message_id in tail:
            row = indexed.get(message_id)
            if row is None:
                omitted["missing_or_invalid"] += 1
                continue
            try:
                message, meta = object_value(row["message"]), object_value(row["metadata"])
                embedded_meta = object_value(message.get("metadata"))
            except (ValueError, TypeError):
                omitted["missing_or_invalid"] += 1
                continue
            marked = programmatic(meta) or programmatic(embedded_meta)
            if desktop_mode:
                parts = message.get("parts")
                unfinished = (message.get("role") == "assistant" and
                    (meta.get("stopped") or embedded_meta.get("stopped") or isinstance(parts, list) and
                     any(isinstance(part, dict) and part.get("type") == "text" and part.get("state") == "streaming" for part in parts)))
                marked = (_automatic_source(meta) or _automatic_source(embedded_meta) or unfinished
                          or row["parent_tool_call_id"] is not None
                          or message.get("role") == "user" and not _desktop_user_signature(meta, embedded_meta, parts_is_array=isinstance(parts, list)))
            if message.get("role") == "user":
                automatic_turn = marked
            if marked or automatic_turn:
                omitted["programmatic"] += 1
                continue
            if message.get("role") not in {"user", "assistant"}:
                omitted["nonconversation"] += 1
                continue
            parts = message.get("parts")
            if not isinstance(parts, list):
                omitted["missing_or_invalid"] += 1
                continue
            text, native_sources, nontext = visible_conversation_parts(message)
            omitted["nontext_parts"] += nontext
            if text.strip():
                record: ConversationRecord = {"message_id": message_id, "role": message["role"],
                                              "created_at": row["created_at"], "text": text}
                if native_sources:
                    record["native_completion_sources"] = native_sources
                records.append(record)
        omitted["record_budget"] = max(0, len(records) - self.max_records)
        selected, remaining, redactions = [], self.max_chars, 0
        for record in reversed(records[-self.max_records:]):
            if remaining <= 0:
                omitted["character_budget"] += 1
                continue
            text, masked = redact(record["text"])
            redactions += masked
            item: ConversationRecord = {**record, "text": text}
            if len(text) > remaining:
                item["omitted_prefix_chars"] = len(text) - remaining
                item["text"] = text[-remaining:]
            remaining -= len(item["text"])
            selected.append(item)
        return {"status": "available" if selected else "no_conversation_text_in_scope", "records": list(reversed(selected)),
                "selection": "explicit_active_path_order", "examined_active_records": len(tail),
                "omissions": omitted, "redactions": redactions, "text_chars": self.max_chars - remaining}

    def _verify_native_origin(self) -> dict:
        for path, expected in NATIVE_ORIGIN_FILES.items():
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                raise SourceProblem("native_origin_contract_unavailable") from None
            if actual != expected:
                raise SourceProblem("native_origin_contract_changed")
        return {"status": "verified", "version": "0.4.578", "proof_scope": "known_native_app_message_producers"}

    def select_desktop_source(self, conn, *, max_threads: int = 256) -> dict:
        selection = {"status": "no_verified_desktop_source", "source_thread_id": None, "candidates": [],
                     "scope": {"active_path_tail_limit": 400, "thread_scan_complete": False},
                     "proof_scope": "stored_app_source_and_native_timezone_after_scheduler_exclusion"}
        try:
            row = conn.execute("select settings_data from app_settings where id='default'").fetchone()
            settings = object_value(row[0]) if row else {}
            heartbeat = settings.get("heartbeat")
            heartbeat_id = heartbeat.get("threadId") if isinstance(heartbeat, dict) else None
            if not isinstance(heartbeat_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", heartbeat_id) is None:
                return dict(selection, status="native_heartbeat_identity_unavailable")
            selection["heartbeat_source_thread_id"] = heartbeat_id
            external = {item[0] for item in conn.execute("select thread_id from channel_mappings")}
            threads = conn.execute("select id,metadata,is_incognito from chat_threads order by id limit ?", (max_threads + 1,)).fetchall()
        except (ValueError, TypeError, sqlite3.Error):
            return dict(selection, status="native_origin_schema_unavailable")
        selection["scope"]["threads_examined"] = min(len(threads), max_threads)
        if len(threads) > max_threads:
            return dict(selection, status="thread_scan_incomplete")
        selection["scope"]["thread_scan_complete"] = True
        eligible = []
        now = time.time()
        for thread in threads:
            tid = thread["id"]
            candidate = {"thread_id": tid, "status": "unproved", "reason": "no_qualified_app_user"}
            selection["candidates"].append(candidate)
            try:
                metadata = object_value(thread["metadata"])
            except (ValueError, TypeError):
                candidate["reason"] = "invalid_thread_metadata"
                continue
            if not isinstance(tid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tid) is None:
                candidate["reason"] = "invalid_thread_identifier"
                continue
            reason = ("native_heartbeat_identity" if tid == heartbeat_id else "incognito" if thread["is_incognito"] else
                      "external_channel_identity" if tid in external else next((key for key in ("isCron", "cronFor", "personalRun", "loop", "goal")
                        if metadata.get(key)), None))
            if reason:
                candidate.update(status="excluded", reason=reason)
                continue
            active = metadata.get("activePath")
            if not isinstance(active, list) or not all(isinstance(mid, str) and len(mid) <= 1000 for mid in active):
                candidate["reason"] = "active_path_unavailable"
                continue
            tail = list(dict.fromkeys(active[-400:]))
            candidate["active_path_tail_count"] = len(tail)
            indexed = {}
            try:
                for start in range(0, len(tail), 200):
                    ids = tail[start:start + 200]
                    slots = ",".join("?" for _ in ids)
                    sql = ("select id,metadata,created_at,parent_tool_call_id,"
                        "case when json_valid(message) then json_extract(message,'$.role') end as role,"
                        "case when json_valid(message) then json_type(message,'$.parts') end as parts_kind,"
                        "case when json_valid(message) then json_extract(message,'$.metadata') end as embedded_metadata "
                        "from chat_messages where thread_id=? and id in (" + slots + ")")
                    for item in conn.execute(sql, [tid] + ids):
                        indexed[item["id"]] = item
            except sqlite3.Error:
                candidate["reason"] = "message_origin_projection_unavailable"
                continue
            if len(indexed) != len(tail):
                candidate["reason"] = "active_path_incomplete"
                continue
            real_users, invalid_time, invalid_origin, historical_heartbeat = [], False, False, False
            for mid in tail:
                item = indexed[mid]
                if item["parent_tool_call_id"] is not None:
                    continue
                if item["role"] is None:
                    invalid_origin = True
                    continue
                if item["role"] != "user":
                    continue
                if item["parts_kind"] != "array":
                    invalid_origin = True
                    continue
                try:
                    saved, embedded = object_value(item["metadata"]), object_value(item["embedded_metadata"])
                except (ValueError, TypeError):
                    invalid_origin = True
                    continue
                historical_heartbeat |= _source_value(saved) == "heartbeat" or _source_value(embedded) == "heartbeat"
                if not _desktop_user_signature(saved, embedded, parts_is_array=item["parts_kind"] == "array"):
                    continue
                stamp = _user_time(item["created_at"])
                if stamp is None or stamp > now + 60:
                    invalid_time = True
                    continue
                real_users.append((stamp, mid, item["created_at"]))
            if historical_heartbeat:
                candidate.update(status="excluded", reason="native_heartbeat_turns_in_scope")
                continue
            if invalid_origin:
                candidate["reason"] = "message_origin_unproved"
                continue
            if invalid_time:
                candidate["reason"] = "real_user_time_unproved"
                continue
            if not real_users:
                continue
            stamp, mid, created = max(real_users, key=lambda item: item[0])
            candidate.update(status="eligible", reason="native_app_user_signature", last_real_user_at=created,
                             last_real_user_id=mid, qualified_user_records=len(real_users),
                             native_type_evidence="stored_app_source_and_validated_timezone")
            eligible.append((stamp, tid, created, tail))
        if not eligible:
            return selection
        newest = max(item[0] for item in eligible)
        latest = [item for item in eligible if item[0] == newest]
        if len(latest) != 1:
            return dict(selection, status="ambiguous_latest_source")
        _, tid, created, tail = latest[0]
        selection.update(status="selected", source_thread_id=tid, last_real_user_at=created, active_path_tail=tail)
        return selection

    def _desktop_event(self, value: dict, source_id: str) -> dict:
        contract = self._verify_native_origin()
        if not isinstance(contract, dict) or contract.get("status") != "verified":
            raise SourceProblem("native_origin_contract_unavailable")
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma query_only=on")
            conn.execute("begin")
            row = conn.execute("select metadata from chat_threads where id=?", (value.get("threadId"),)).fetchone()
            cron = object_value(row[0]) if row else {}
            if cron.get("isCron") is not True or cron.get("cronFor") != source_id:
                raise SourceProblem("cron_source_not_verified")
            selection = self.select_desktop_source(conn)
            selected = selection.get("source_thread_id")
            conversation = (self._conversation(conn, selected, desktop_mode=True) if isinstance(selected, str) else
                            {"status": selection["status"], "records": [], "omissions": {}, "text_chars": 0})
            try:
                emotions = self._emotion(conn, source_id)
            except (OSError, ValueError, sqlite3.Error) as error:
                emotions = {"base": {"status": "read_failed"}, "contextual": {"status": "read_failed"},
                            "error_type": type(error).__name__, "native_effective_state_recomputed": False}
        emotions["base"] = _native_emotion_time(emotions["base"])
        emotions["contextual"] = _native_emotion_time(emotions["contextual"])
        desktop = _native_emotion_time(self._desktop_emotion(selected)) if isinstance(selected, str) else {
            "status": "source_unavailable", "native_window_hours": 2, "source_thread_id": None}
        emotions["desktop_source"] = desktop
        data = {"version": 1, "kind": "private_source_data", "source_contract_version": 2, "data_only": True,
                "job_identity": {"job_id": JOB_ID, "source_thread_id": source_id, "cron_thread_id": value["threadId"]},
                "source_thread_id": selected, "source_selection": selection, "native_origin_contract": contract,
                "captured_at": datetime.now(timezone.utc).isoformat(), "conversation": conversation, "emotion_inputs": emotions,
                "native_layer": {"persona_files_modified": False, "historical_message_objects_replayed": False,
                                 "memory_query_rebuilt": False, "contextual_emotion_remapped": False}}
        self.diagnostic = {"status": "attached", "source_contract_version": 2, "thread_id": value["threadId"],
            "job_source_thread_id": source_id, "selected_source_thread_id": selected,
            "source_selection_status": selection["status"], "last_real_user_at": selection.get("last_real_user_at"),
            "candidates": selection["candidates"], "active_path_tail": selection.get("active_path_tail", []),
            "records": len(conversation["records"]), "text_chars": conversation.get("text_chars", 0),
            "redactions": conversation.get("redactions", 0), "desktop_status": desktop["status"],
            "desktop_record_updated_at": desktop.get("record_updated_at"), "desktop_age_seconds": desktop.get("age_seconds"),
            "base_status": emotions["base"]["status"], "contextual_status": emotions["contextual"]["status"]}
        return data

    def _emotion(self, conn, source_id: str) -> dict:
        base = emotion_record(self.emotions / "base.md", 6)
        mapping_count = conn.execute("select count(*) from channel_mappings where thread_id=? and is_active=1", (source_id,)).fetchone()[0]
        row = conn.execute("select platform,external_chat_id,external_user_id from channel_mappings where thread_id=? and is_active=1 limit 1", (source_id,)).fetchone()
        context = {"status": "no_active_mapping", "active_mappings": mapping_count,
                   "mapping_selection": "native_unordered_limit_1", "thread_id_fallback_used": False}
        if row:
            identifier = row["external_chat_id"]
            if not isinstance(identifier, str) or not identifier or any(char in identifier for char in "/\\\x00:\r\n"):
                return {"base": base, "contextual": dict(context, status="invalid_mapping_identifier")}
            root = (self.emotions / "context").resolve()
            path = (root / (identifier + ".md")).resolve()
            if path.parent != root:
                return {"base": base, "contextual": dict(context, status="mapping_path_outside_context")}
            context.update(emotion_record(path, 2))
            context.update({"platform": row["platform"], "external_chat_id": identifier, "multiple_active_mappings": mapping_count > 1})
        return {"base": base, "contextual": context, "native_effective_state_recomputed": False}

    def _desktop_emotion(self, source_id: str) -> dict:
        source = {"record_source": "authorized_desktop_thread", "source_thread_id": source_id}
        if not source_id or any(char in source_id for char in "/\\\x00:\r\n"):
            return {"status": "invalid_desktop_identifier"}
        root = (self.emotions / "context").resolve()
        path = (root / (source_id + ".md")).resolve()
        if path.parent != root:
            return {"status": "desktop_path_outside_context"}
        try:
            return dict(emotion_record(path, 2), **source)
        except (OSError, ValueError) as error:
            return dict(source, status="read_failed", error_type=type(error).__name__)

    def for_source(self, source_id: str) -> dict:
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma query_only=on")
            conn.execute("begin")
            conversation = self._conversation(conn, source_id)
            try:
                emotion = self._emotion(conn, source_id)
            except (OSError, ValueError, sqlite3.Error) as error:
                emotion = {"base": {"status": "read_failed"}, "contextual": {"status": "read_failed"},
                           "error_type": type(error).__name__, "native_effective_state_recomputed": False}
        return {"version": 1, "kind": "private_source_data", "data_only": True, "source_thread_id": source_id,
                "captured_at": datetime.now(timezone.utc).isoformat(), "conversation": conversation, "emotion_inputs": emotion,
                "native_layer": {"persona_files_modified": False, "historical_message_objects_replayed": False,
                                 "memory_query_rebuilt": False, "contextual_emotion_remapped": False}}

    def for_event(self, event: dict) -> dict | None:
        value = event.get("input")
        if event.get("hook") != "chat.message.willSend" or not isinstance(value, dict):
            return None
        parts = value.get("parts")
        text = "\n".join(part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)) if isinstance(parts, list) else value.get("content", "")
        if not isinstance(text, str) or MARKER not in text:
            return None
        job = self.job_loader()
        payload = job.get("payload") if isinstance(job, dict) else None
        if not isinstance(payload, dict):
            raise SourceProblem("job_source_not_verified")
        source_id = payload.get("threadId")
        if (job.get("id") != JOB_ID or job.get("executionMode") != "main" or not isinstance(source_id, str) or
                MARKER not in str(payload.get("agentTurn", ""))):
            raise SourceProblem("job_source_not_verified")
        if payload.get("sourceThreadId") not in (None, source_id):
            raise SourceProblem("job_source_ambiguous")
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            row = conn.execute("select metadata from chat_threads where id=?", (value.get("threadId"),)).fetchone()
            metadata = object_value(row[0]) if row else {}
        if metadata.get("isCron") is not True or metadata.get("cronFor") != source_id:
            raise SourceProblem("cron_source_not_verified")
        if SOURCE_CONTRACT_MARKER in text:
            configured = payload.get("agentTurn")
            if text.count(SOURCE_CONTRACT_MARKER) != 1 or not isinstance(configured, str) or configured.count(SOURCE_CONTRACT_MARKER) != 1:
                raise SourceProblem("source_contract_not_verified")
            return self._desktop_event(value, source_id)
        data = self.for_source(source_id)
        data["emotion_inputs"]["desktop_source"] = self._desktop_emotion(source_id)
        self.diagnostic = {"status": "attached", "thread_id": value["threadId"],
                           "records": len(data["conversation"]["records"]),
                           "text_chars": data["conversation"].get("text_chars", 0), "redactions": data["conversation"].get("redactions", 0),
                           "contextual_status": data["emotion_inputs"]["contextual"]["status"], "base_status": data["emotion_inputs"]["base"]["status"],
                           "desktop_status": data["emotion_inputs"]["desktop_source"]["status"]}
        return data


def render_private_context(data: dict) -> str:
    job = data.get("job_identity") or {}
    selection = data.get("source_selection") or {}
    selected = data.get("source_thread_id")
    lines = ["私有对话上下文", "作业配置源：" + str(job.get("source_thread_id", "未提供")),
             "桌面对话数据源：" + (selected if isinstance(selected, str) else "未取得可验证来源"),
             "来源状态：" + str(selection.get("status", "unavailable")),
             "来源证据：" + ("原生 app 用户提交及已校验时区，已排除心跳、Cron、后台和外部线程"
                 if isinstance(selected, str) and selection.get("status") == "selected" else "未取得满足原生来源合同的对话"), ""]
    conversation = data.get("conversation") or {}
    records = conversation.get("records") or []
    if not records:
        lines.extend(["对话文本状态：" + str(conversation.get("status", "unavailable")), ""])
    for record in records:
        role = record.get("role")
        text = record.get("text")
        if role not in {"user", "assistant"} or not isinstance(text, str):
            continue
        lines.extend(["时间：" + str(record.get("created_at", "未提供")),
                      "用户：" if role == "user" else "Alma：", redact(text)[0], ""])
    lines.append("情绪资料")
    inputs = data.get("emotion_inputs") or {}
    names = (("base", "基础情绪 · base.md · 原生6小时"),
             ("contextual", "原生作业映射情绪 · 配置来源 · 原生2小时"),
             ("desktop_source", "桌面情绪 · context/" + (selected if isinstance(selected, str) else "无来源") + ".md · 原生2小时"))
    for key, label in names:
        record = inputs.get(key) or {}
        lines.extend([label, "状态：" + str(record.get("status", "unavailable"))])
        if isinstance(record.get("record_updated_at"), str):
            lines.append("原始更新时间：" + record["record_updated_at"])
        if isinstance(record.get("age_seconds"), (int, float)):
            lines.append("记录年龄（秒）：" + str(record["age_seconds"]))
        values = record.get("recorded_values")
        if record.get("status") == "record_in_window" and isinstance(values, dict):
            for field in ("mood", "energy", "valence"):
                if field in values:
                    lines.append(field + "：" + redact(str(values[field]))[0])
        lines.append("")
    return "\n".join(lines)


def handle_event(event: dict, reader: SourceReader) -> dict:
    data = reader.for_event(event)
    if data is None:
        return {}
    value = event["input"]
    parts = value.get("parts")
    if not isinstance(parts, list):
        content = value.get("content")
        if not isinstance(content, str):
            raise SourceProblem("original_parts_unavailable")
        parts = [{"type": "text", "text": content}]
    if data.get("source_contract_version") == 2:
        text = render_private_context(data)
    else:
        text = "PRIVATE_SOURCE_DATA\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
    updated = {"updatedInput": {"parts": copy.deepcopy(parts) + [{"type": "text", "text": text}]}}
    if len(json.dumps(updated, ensure_ascii=False)) > MAX_EVENT_CHARS:
        raise SourceProblem("updated_input_too_large")
    reader.diagnostic.update({"updated_input_ready": True, "original_parts": len(parts),
                              "returned_parts": len(parts) + 1, "private_packet_chars": len(text)})
    return updated


def log_diagnostic(diagnostic: dict):
    if os.environ.get("ALMA_HOOK") != "true":
        return
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        row = dict(diagnostic, time=time.time(), job_id=JOB_ID, host_hook_env=True)
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def main() -> int:
    reader = SourceReader()
    output = {}
    try:
        text = sys.stdin.read(MAX_EVENT_CHARS + 1)
        if len(text) > MAX_EVENT_CHARS:
            raise SourceProblem("event_too_large")
        event = json.loads(text)
        if not isinstance(event, dict):
            raise SourceProblem("invalid_event")
        output = handle_event(event, reader)
    except Exception as error:
        reader.diagnostic = {"status": error.code if isinstance(error, SourceProblem) else "source_unavailable", "error_type": type(error).__name__}
    log_diagnostic(reader.diagnostic)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
