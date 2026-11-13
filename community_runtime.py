"""Native run authority, read-only host evidence, and a short-lived CLI deadline fence."""
from __future__ import annotations

import hashlib
import json
import base64
import binascii
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from community_admin import JOB_ID, MARKER, NativeAPI, get_job
from community_context import CHAT_DB

CONTRACT_MARKER = "[xhh-community-boundary:2]"
ACCOUNT_MARKER = re.compile(r"\[xhh-community-account:(alt|main):([0-9]{1,32})\]")


class RuntimeProblem(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _WorkerDeadline(subprocess.TimeoutExpired):
    def __init__(self, command, timeout, *, output=None, stderr=None, cleanup_errors=None):
        super().__init__(command, timeout, output=output, stderr=stderr)
        self.cleanup_errors = cleanup_errors if cleanup_errors is not None else []


class _WorkerCompleted(subprocess.CompletedProcess):
    def __init__(self, command, code, stdout, stderr, cleanup_errors):
        super().__init__(command, code, stdout, stderr)
        self.cleanup_errors = cleanup_errors


@dataclass(frozen=True)
class RunScope:
    run_id: str
    started_at: float
    deadline: float
    contract_version: int = 2
    request_limit: int = 120
    write_limit: int = 3
    minimum_write_interval: float = 15
    account_alias: str = "alt"
    account_uid: str | None = None


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def current_run_scope(db: Path = CHAT_DB, job_loader=None, environment=None) -> RunScope | None:
    env = os.environ if environment is None else environment
    thread_id = env.get("ALMA_THREAD_ID")
    if not thread_id:
        if env.get("ALMA_MESSAGE_ID") or env.get("ALMA_TOOL_CALL_ID"):
            raise RuntimeProblem("native_thread_required")
        return None
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma query_only=on")
            row = conn.execute("select title,metadata,created_at,is_generating from chat_threads where id=?", (thread_id,)).fetchone()
            if not row:
                return None
            meta = json.loads(row["metadata"] or "{}")
            if not isinstance(meta, dict) or meta.get("isCron") is not True:
                return None
            prompt = ""
            for message, in conn.execute("select message from chat_messages where thread_id=? order by created_at,id", (thread_id,)):
                value = json.loads(message)
                if not isinstance(value, dict) or value.get("role") != "user" or not isinstance(value.get("parts"), list):
                    continue
                text = "\n".join(part["text"] for part in value["parts"]
                                 if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
                if MARKER in text:
                    prompt = text
                    break
            if not prompt:
                return None
            if row["is_generating"] != 1:
                raise RuntimeProblem("cron_run_not_active")
        job = (job_loader or (lambda: get_job(NativeAPI(timeout=2))))()
        payload = job.get("payload") if isinstance(job, dict) else None
        if (not isinstance(payload, dict) or job.get("id") != JOB_ID or job.get("executionMode") != "main" or
                not isinstance(job.get("name"), str) or not isinstance(payload.get("threadId"), str) or not payload["threadId"] or
                meta.get("cronFor") != payload["threadId"] or payload.get("sourceThreadId") not in (None, payload["threadId"]) or
                not isinstance(payload.get("agentTurn"), str) or MARKER not in payload["agentTurn"] or
                row["title"] != "⏰ Cron: " + job["name"]):
            raise RuntimeProblem("cron_run_not_verified")
        try:
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            started = created.timestamp()
            native_limit = float(payload.get("timeoutSeconds", 1200))
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise RuntimeProblem("cron_run_time_not_verified") from None
        if (created.tzinfo is None or not math.isfinite(started) or not math.isfinite(native_limit) or
                not 0 < started <= time.time() + 5 or native_limit <= 30):
            raise RuntimeProblem("cron_run_time_not_verified")
        version = 2 if CONTRACT_MARKER in prompt else 1
        alias, uid = "alt", None
        if version == 2:
            issued_accounts = ACCOUNT_MARKER.findall(prompt)
            configured_accounts = ACCOUNT_MARKER.findall(payload["agentTurn"])
            if len(issued_accounts) != 1 or configured_accounts != issued_accounts:
                raise RuntimeProblem("cron_account_not_verified")
            alias, uid = issued_accounts[0]
        return RunScope(thread_id, started, started + min(1170, native_limit - 30), version,
                        account_alias=alias, account_uid=uid)
    except RuntimeProblem:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise RuntimeProblem("cron_authority_unavailable") from None


class NativeEvidence:
    def __init__(self, thread_id: str, db: Path = CHAT_DB):
        self.thread_id, self.db = thread_id, db

    def _parts(self):
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.execute("pragma query_only=on")
            conn.execute("begin")
            row = conn.execute("select metadata from chat_threads where id=?", (self.thread_id,)).fetchone()
            meta = json.loads(row[0] or "{}") if row else {}
            active = meta.get("activePath") if isinstance(meta, dict) else None
            if not isinstance(active, list) or not all(isinstance(mid, str) for mid in active):
                return
            tail = list(dict.fromkeys(active[-400:]))
            indexed = {}
            for start in range(0, len(tail), 200):
                ids = tail[start:start + 200]
                slots = ",".join("?" for _ in ids)
                indexed.update(conn.execute(f"select id,message from chat_messages where thread_id=? and id in ({slots})", [self.thread_id] + ids))
            for mid in tail:
                if mid not in indexed:
                    continue
                value = json.loads(indexed[mid])
                if not isinstance(value, dict) or value.get("role") != "assistant":
                    continue
                parts = value.get("parts")
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if isinstance(part, dict) and part.get("state") == "output-available":
                        yield part

    def context_pages_received(self, expected: list[dict]) -> bool:
        needed = {hashlib.sha256(canonical(page).encode("utf-8")).hexdigest() for page in expected}
        if not needed:
            return False
        try:
            for part in self._parts():
                if part.get("type") != "tool-Bash":
                    continue
                output = part.get("output")
                text = output.get("stdout") if isinstance(output, dict) else None
                if not isinstance(text, str):
                    continue
                for line in text.splitlines():
                    try:
                        value = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    context = value.get("context") if isinstance(value, dict) else None
                    if isinstance(context, dict) and context.get("key"):
                        needed.discard(hashlib.sha256(canonical(value).encode("utf-8")).hexdigest())
                if not needed:
                    return True
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False
        return False

    def image_received(self, file_path: str) -> dict:
        try:
            expected = str(Path(file_path).expanduser().resolve()).casefold()
            for part in self._parts():
                inputs, output = part.get("input"), part.get("output")
                if part.get("type") != "tool-Read" or not isinstance(inputs, dict) or not isinstance(output, dict):
                    continue
                source = inputs.get("file_path")
                cache = output.get("image_cache_file")
                if (not isinstance(source, str) or str(Path(source).expanduser().resolve()).casefold() != expected or
                        output.get("is_image") is not True or not isinstance(cache, str) or not Path(cache).is_file()):
                    continue
                return {"status": "host_image_result", "tool_call_id": part.get("toolCallId"),
                        "mime_type": output.get("mime_type"), "width": output.get("width"), "height": output.get("height"),
                        "evidence_scope": "current_cron_thread", "provider_bytes_observed": False}
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return {"status": "evidence_unavailable"}
        return {"status": "not_observed"}


    def image_bytes_received(self, expected: dict) -> dict:
        try:
            preview_file = expected.get("preview_file")
            digest, size = expected.get("preview_sha256"), expected.get("preview_bytes")
            if (not isinstance(preview_file, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                    type(size) is not int or not 0 < size <= 320 * 1024 or not isinstance(expected.get("asset_id"), str)):
                return {"status": "evidence_unavailable"}
            path = str(Path(preview_file).resolve()).casefold()
            cache_root = self.db.resolve().parent / "gallery_cache"
            for part in self._parts():
                inputs, output = part.get("input"), part.get("output")
                if part.get("type") != "tool-Read" or not isinstance(inputs, dict) or not isinstance(output, dict):
                    continue
                source = inputs.get("file_path")
                if (not isinstance(source, str) or str(Path(source).expanduser().resolve()).casefold() != path or
                        output.get("is_image") is not True or output.get("mime_type") != "image/png" or
                        not isinstance(part.get("toolCallId"), str) or not part["toolCallId"]):
                    continue
                if any(output.get(key) is not None and (type(output[key]) is not int or output[key] != expected.get("preview_" + key))
                       for key in ("width", "height")):
                    continue
                inline, cache = output.get("image_base64"), output.get("image_cache_file")
                if isinstance(inline, str) and cache is None:
                    if len(inline) > 4 * ((320 * 1024 + 2) // 3):
                        continue
                    try:
                        data = base64.b64decode(inline, validate=True)
                    except (ValueError, binascii.Error):
                        continue
                    scope = "host_saved_inline_bytes"
                elif isinstance(cache, str) and inline is None:
                    if not re.fullmatch(r"inline-[0-9a-f]{16}\.png", cache):
                        continue
                    file_path = cache_root / cache
                    if file_path.resolve().parent != cache_root.resolve():
                        continue
                    try:
                        with file_path.open("rb") as image:
                            data = image.read(320 * 1024 + 1)
                    except OSError:
                        continue
                    scope = "current_gallery_cache_bytes"
                else:
                    continue
                if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                    continue
                return {"status": "host_preview_bytes_verified", "asset_id": expected["asset_id"],
                        "preview_sha256": digest, "preview_bytes": size, "checked_at": time.time(),
                        "tool_call_id": part["toolCallId"], "byte_scope": scope, "provider_bytes_observed": False}
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return {"status": "evidence_unavailable"}
        return {"status": "not_observed"}


class _WindowsWorkerJob:
    """Own only the waiting CLI child and its descendants, never the Alma process."""
    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W
        class Basic(C.Structure):
            _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong), ("flags", W.DWORD),
                        ("minimum_ws", C.c_size_t), ("maximum_ws", C.c_size_t), ("active", W.DWORD),
                        ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]
        class IO(C.Structure):
            _fields_ = [(name, C.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Limits(C.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                        ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]
        class Accounting(C.Structure):
            _fields_ = [(name, C.c_longlong) for name in ("user_time", "kernel_time", "period_user", "period_kernel")] + [
                (name, W.DWORD) for name in ("page_faults", "total_processes", "active_processes", "terminated_processes")]
        self.accounting = Accounting
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        signatures = (("CreateJobObjectW", [C.c_void_p, W.LPCWSTR], W.HANDLE),
                      ("SetInformationJobObject", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
                      ("AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
                      ("TerminateJobObject", [W.HANDLE, W.UINT], W.BOOL),
                      ("QueryInformationJobObject", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
                      ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
                      ("CloseHandle", [W.HANDLE], W.BOOL))
        for name, args, result in signatures:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("worker_job_unavailable")
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if not self.kernel.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
            self.close()
            raise OSError("worker_job_limits_unavailable")

    def assign(self, pid: int):
        handle = self.kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE on our child only.
        if not handle:
            raise OSError("worker_tree_binding_unavailable")
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, handle):
                raise OSError("worker_tree_binding_unavailable")
        finally:
            self.kernel.CloseHandle(handle)

    def close(self) -> list[dict]:
        problems = []
        if self.handle:
            import ctypes as C
            handle, self.handle = self.handle, None
            try:
                if not self.kernel.TerminateJobObject(handle, 1):
                    raise OSError("worker_tree_termination_unavailable")
                deadline, pause = time.monotonic() + 5, threading.Event()
                while True:
                    accounting = self.accounting()
                    if not self.kernel.QueryInformationJobObject(handle, 1, C.byref(accounting), C.sizeof(accounting), None):
                        raise OSError("worker_tree_status_unavailable")
                    if accounting.active_processes == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise OSError("worker_tree_cleanup_incomplete")
                    pause.wait(0.01)
            except OSError as error:
                problems.append({"code": "worker_cleanup_unavailable", "stage": "job", "type": type(error).__name__})
            finally:
                self.kernel.CloseHandle(handle)
        return problems


class _WindowsWorkerProcess:
    """Create a suspended child, bind its Job, then resume its retained primary thread."""
    def __init__(self, command: list[str], job: _WindowsWorkerJob, deadline: float):
        import ctypes as C
        import msvcrt
        from ctypes import wintypes as W
        class Startup(C.Structure):
            _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR), ("title", W.LPWSTR)] + [
                (name, W.DWORD) for name in ("x", "y", "x_size", "y_size", "x_chars", "y_chars", "fill", "flags")] + [
                ("show", W.WORD), ("reserved_size", W.WORD), ("reserved_bytes", C.c_void_p),
                ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]
        class ExtendedStartup(C.Structure):
            _fields_ = [("startup", Startup), ("attributes", C.c_void_p)]
        class ProcessInfo(C.Structure):
            _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        signatures = (
            ("InitializeProcThreadAttributeList", [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
            ("UpdateProcThreadAttribute", [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
            ("DeleteProcThreadAttributeList", [C.c_void_p], None),
            ("CreateProcessW", [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p,
                                W.LPCWSTR, C.c_void_p, C.POINTER(ProcessInfo)], W.BOOL),
            ("ResumeThread", [W.HANDLE], W.DWORD),
            ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            ("GetExitCodeProcess", [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
            ("TerminateProcess", [W.HANDLE, W.UINT], W.BOOL),
            ("CloseHandle", [W.HANDLE], W.BOOL))
        for name, args, result in signatures:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.command, self.handle, self.returncode = command, None, None
        self.job, self.bound = job, False
        self.files, self.threads, self.output = [], [], {"stdout": b"", "stderr": b""}
        self.io_error, self.cleanup_errors = None, []
        descriptors, attributes, info = [], None, ProcessInfo()
        try:
            stdin_read, stdin_write = os.pipe()
            descriptors.extend((stdin_read, stdin_write))
            stdout_read, stdout_write = os.pipe()
            descriptors.extend((stdout_read, stdout_write))
            stderr_read, stderr_write = os.pipe()
            descriptors.extend((stderr_read, stderr_write))
            self.stdin = os.fdopen(stdin_write, "wb")
            descriptors.remove(stdin_write)
            self.files.append(self.stdin)
            self.stdout = os.fdopen(stdout_read, "rb")
            descriptors.remove(stdout_read)
            self.files.append(self.stdout)
            self.stderr = os.fdopen(stderr_read, "rb")
            descriptors.remove(stderr_read)
            self.files.append(self.stderr)
            handles = (W.HANDLE * 3)(*(msvcrt.get_osfhandle(fd) for fd in (stdin_read, stdout_write, stderr_write)))
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            size = C.c_size_t()
            self.kernel.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
            if not size.value:
                raise OSError("worker_startup_unavailable")
            buffer = C.create_string_buffer(size.value)
            if not self.kernel.InitializeProcThreadAttributeList(buffer, 1, 0, C.byref(size)):
                raise OSError("worker_startup_unavailable")
            attributes = buffer
            if not self.kernel.UpdateProcThreadAttribute(buffer, 0, 0x00020002, C.byref(handles), C.sizeof(handles), None, None):
                raise OSError("worker_pipe_binding_unavailable")
            startup = ExtendedStartup()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES.
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = handles
            startup.attributes = C.cast(buffer, C.c_void_p)
            flags = 0x4 | 0x08000000 | 0x00080000  # SUSPENDED | NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT.
            line = C.create_unicode_buffer(subprocess.list2cmdline(command))
            if not self.kernel.CreateProcessW(None, line, None, None, True, flags, None, None, C.byref(startup), C.byref(info)):
                raise OSError("worker_creation_unavailable")
            self.handle, self.pid = info.process, int(info.pid)
            job.assign(self.pid)
            self.bound = True
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, 0)
            if self.kernel.ResumeThread(info.thread) != 1:
                raise OSError("worker_resume_unavailable")
        except BaseException as error:
            self.close()
            if isinstance(error, subprocess.TimeoutExpired):
                raise _WorkerDeadline(command, error.timeout, cleanup_errors=self.cleanup_errors) from None
            raise
        finally:
            if info.thread:
                self.kernel.CloseHandle(info.thread)
            if attributes is not None:
                self.kernel.DeleteProcThreadAttributeList(attributes)
            for descriptor in descriptors:
                os.close(descriptor)

    def poll(self):
        import ctypes as C
        from ctypes import wintypes as W
        if self.returncode is None and self.kernel.WaitForSingleObject(self.handle, 0) == 0:
            code = W.DWORD()
            if not self.kernel.GetExitCodeProcess(self.handle, C.byref(code)):
                raise OSError("worker_status_unavailable")
            self.returncode = int(code.value)
        return self.returncode

    def kill(self):
        if self.handle and self.poll() is None:
            if not self.kernel.TerminateProcess(self.handle, 1):
                raise OSError("worker_termination_unavailable")

    def communicate(self, input: str | None = None, timeout: float = 5):
        deadline = time.monotonic() + timeout
        if not self.threads:
            data = (input or "").encode("utf-8")
            def read(stream, key):
                try:
                    self.output[key] = stream.read()
                except OSError as error:
                    self.io_error = error
                finally:
                    stream.close()
            def write():
                try:
                    self.stdin.write(data)
                    self.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    self.stdin.close()
            self.threads = [threading.Thread(target=read, args=(self.stdout, "stdout"), daemon=True),
                            threading.Thread(target=read, args=(self.stderr, "stderr"), daemon=True),
                            threading.Thread(target=write, daemon=True)]
            for thread in self.threads:
                thread.start()
        milliseconds = min(0xfffffffe, max(0, math.ceil((deadline - time.monotonic()) * 1000)))
        waited = self.kernel.WaitForSingleObject(self.handle, milliseconds)
        if waited == 0xffffffff:
            raise OSError("worker_wait_unavailable")
        if waited == 258:
            raise subprocess.TimeoutExpired(self.command, timeout)
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            raise subprocess.TimeoutExpired(self.command, timeout)
        if self.io_error is not None:
            raise OSError("worker_pipe_unavailable")
        self.poll()
        return self.captured_output()

    def captured_output(self) -> tuple[str, str]:
        # Match the former text=True pipe's universal-newline decoding.
        def text(value: bytes) -> str:
            return value.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        return text(self.output["stdout"]), text(self.output["stderr"])

    def close(self) -> list[dict]:
        problems = []
        if self.handle:
            try:
                if self.bound:
                    problems.extend(self.job.close())
                else:
                    self.kill()
                if self.kernel.WaitForSingleObject(self.handle, 5000) != 0:
                    raise OSError("worker_process_cleanup_incomplete")
            except OSError as error:
                problems.append({"code": "worker_cleanup_unavailable", "stage": "process", "type": type(error).__name__})
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        for stream in self.files:
            try:
                stream.close()
            except OSError as error:
                problems.append({"code": "worker_cleanup_unavailable", "stage": "pipe", "type": type(error).__name__})
        self.cleanup_errors.extend(problems)
        return problems

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _supervised_run(command, *, input: str, timeout: float):
    if os.name != "nt":
        raise OSError("worker_platform_unsupported")
    started = time.monotonic()
    job = _WindowsWorkerJob()
    try:
        with _WindowsWorkerProcess(command, job, started + timeout) as process:
            try:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                stdout, stderr = process.communicate(input=input, timeout=remaining)
                code = process.poll()
                if code is None:
                    raise OSError("worker_status_unavailable")
                return _WorkerCompleted(command, code, stdout, stderr, process.cleanup_errors)
            except subprocess.TimeoutExpired:
                process.cleanup_errors.extend(job.close())
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as error:
                    process.cleanup_errors.append({"code": "worker_cleanup_unavailable", "stage": "pipe_capture", "type": type(error).__name__})
                    stdout, stderr = process.captured_output()
                raise _WorkerDeadline(command, timeout, output=stdout, stderr=stderr, cleanup_errors=process.cleanup_errors) from None
            finally:
                process.cleanup_errors.extend(job.close())
    finally:
        job.close()


def validate_worker_request(payload: dict) -> None:
    if (not isinstance(payload, dict) or set(payload) != {"version", "argv", "worker_token", "scope_binding"} or
            type(payload.get("version")) is not int or payload["version"] != 1 or
            not isinstance(payload.get("argv"), list) or not 1 <= len(payload["argv"]) <= 2048 or
            not all(isinstance(arg, str) and len(arg) <= 32768 for arg in payload["argv"]) or
            sum(len(arg) for arg in payload["argv"]) > 100000 or not isinstance(payload.get("worker_token"), str) or
            not re.fullmatch(r"[a-f0-9]{32}", payload["worker_token"]) or
            (payload["scope_binding"] is not None and not isinstance(payload["scope_binding"], dict))):
        raise RuntimeProblem("worker_invalid_request")


def _request_digest(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def worker_result(payload: dict, result: dict, account_binding: dict | None) -> dict:
    validate_worker_request(payload)
    if account_binding is not None and result.get("account") is None:
        result = dict(result, account={key: account_binding[key] for key in ("uid", "alias")})
    return {"version": 1, "kind": "worker_result", "worker_token": payload["worker_token"],
            "request_digest": _request_digest(payload), "account_binding": account_binding, "result": result}


def _identity_matches(binding, payload: dict) -> bool:
    from community import BoundaryError, parser
    if (not isinstance(binding, dict) or set(binding) != {"uid", "alias", "revision"} or
            not isinstance(binding.get("uid"), str) or not re.fullmatch(r"[0-9]{1,32}", binding["uid"]) or
            not isinstance(binding.get("alias"), str) or binding["alias"] not in {"alt", "main"}):
        return False
    revision = binding["revision"]
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{16,128}", revision)):
        return False
    try:
        args = parser().parse_args(payload["argv"])
    except BoundaryError:
        return False
    scope = payload["scope_binding"]
    return (binding["alias"] == args.account and
            (scope is None or (binding["alias"] == scope.get("account_alias") and binding["uid"] == scope.get("account_uid"))))


def _stdout_lines(stdout) -> list[str]:
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    return stdout.strip().splitlines() if isinstance(stdout, str) else []


def _worker_binding(stdout, payload: dict) -> dict | None:
    try:
        validate_worker_request(payload)
    except RuntimeProblem:
        return None
    matches = []
    for line in _stdout_lines(stdout):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if (isinstance(value, dict) and set(value) == {"version", "kind", "worker_token", "request_digest", "data"} and
                type(value["version"]) is int and value["version"] == 1 and value["kind"] == "worker_binding" and
                value["worker_token"] == payload["worker_token"] and value["request_digest"] == _request_digest(payload) and
                _identity_matches(value["data"], payload)):
            matches.append(value["data"])
    return matches[0] if len(matches) == 1 else None


def _terminal_result(stdout, payload: dict, returncode: int) -> dict | None:
    from community import BoundaryError, parser
    try:
        validate_worker_request(payload)
        lines = _stdout_lines(stdout)
        frame = json.loads(lines[-1]) if lines else None
    except (RuntimeProblem, ValueError):
        return None
    if (not isinstance(frame, dict) or
            set(frame) != {"version", "kind", "worker_token", "request_digest", "account_binding", "result"} or
            type(frame["version"]) is not int or frame["version"] != 1 or frame["kind"] != "worker_result" or
            frame["worker_token"] != payload["worker_token"] or frame["request_digest"] != _request_digest(payload)):
        return None
    result, binding = frame["result"], frame["account_binding"]
    fields = {"version", "ok", "kind", "account", "data", "coverage", "errors"}
    if (not isinstance(result, dict) or not fields <= set(result) <= fields | {"context"} or
            type(result["version"]) is not int or result["version"] != 1 or not isinstance(result["ok"], bool) or
            not isinstance(result["kind"], str) or not result["kind"] or
            not isinstance(result["errors"], list) or
            not all(isinstance(error, dict) and isinstance(error.get("code"), str) and error["code"] for error in result["errors"]) or
            result["ok"] != (not result["errors"]) or
            (result["coverage"] is not None and not isinstance(result["coverage"], dict)) or
            ("context" in result and not isinstance(result["context"], dict))):
        return None
    expected_exit = 0 if result["ok"] else 2 if any(error.get("origin") == "arguments" for error in result["errors"]) else 1
    if returncode != expected_exit:
        return None
    if binding is None:
        if result["ok"] or result["kind"] != "worker" or result["account"] is not None or result["data"] is not None or "context" in result:
            return None
    elif (not _identity_matches(binding, payload) or binding != _worker_binding(stdout, payload) or
            # The worker reports the same identity plus where it came from. Compare the
            # identity fields only; the provenance tag is not part of the binding.
            {key: (result["account"] or {}).get(key) for key in ("uid", "alias")} !=
            {key: binding[key] for key in ("uid", "alias")}):
        return None
    if result["kind"] != "worker":
        try:
            args = parser().parse_args(payload["argv"])
        except BoundaryError:
            return None
        if args.command == "scan":
            allowed = {"scan"}
        elif args.command == "act":
            allowed = {"action", "operation", "disposition"}
        elif args.resource.startswith("v:"):
            context = result.get("context")
            if result["ok"] and (not isinstance(context, dict) or context.get("view_ref") != args.resource):
                return None
            allowed = {result["kind"]}
        else:
            kind = "detail" if args.resource.startswith(("p:", "i:")) else "comment" if args.resource.startswith("c:") else "operation" if args.resource.startswith("op:") else args.resource
            allowed = {kind}
        if not result["ok"]:
            allowed.add(args.command)
        if result["kind"] not in allowed:
            return None
    return result


def supervised_command(command: list[str], payload: dict, seconds: float, runner=None) -> dict:
    from community import envelope
    if not math.isfinite(seconds) or seconds <= 0:
        return envelope("worker", errors=[{"code": "worker_deadline", "send_outcome_inferred": False}])
    try:
        outcome = (runner or _supervised_run)(command, input=json.dumps(payload, ensure_ascii=False), timeout=seconds)
    except subprocess.TimeoutExpired as error:
        binding = _worker_binding(error.stdout, payload)
        problems = [{"code": "worker_deadline", "send_outcome_inferred": False}]
        if isinstance(error, _WorkerDeadline):
            problems.extend(error.cleanup_errors)
        return envelope("worker", {"worker_binding": binding} if binding else None, errors=problems)
    except OSError as error:
        return envelope("worker", errors=[{"code": "worker_unavailable", "type": type(error).__name__, "send_outcome_inferred": False}])
    result = _terminal_result(outcome.stdout, payload, outcome.returncode)
    if result is None:
        binding = _worker_binding(outcome.stdout, payload)
        return envelope("worker", {"worker_binding": binding} if binding else None,
                        errors=[{"code": "worker_invalid_result", "exit_code": outcome.returncode, "send_outcome_inferred": False}])
    if isinstance(outcome, _WorkerCompleted) and outcome.cleanup_errors:
        result = dict(result, ok=False, errors=result["errors"] + outcome.cleanup_errors,
                      coverage=dict(result.get("coverage") or {}, worker_tree_cleanup="unavailable"))
        result.pop("context", None)
    return result


def supervise_cli(argv: list[str], seconds: float, scope: RunScope | None, runner=None) -> dict:
    from community import bounded_summary, envelope, parser, validate
    from community_state import State, StateConflict
    started = time.monotonic()
    args = parser().parse_args(argv)
    validate(args, read_files=False)
    if scope is not None and (scope.contract_version != 2 or args.account != scope.account_alias):
        raise RuntimeProblem("cron_account_mismatch")
    remaining = seconds - (time.monotonic() - started)
    if scope is not None:
        remaining = min(remaining, scope.deadline - time.time())
    token = uuid.uuid4().hex
    result = supervised_command([sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("community.py")), "--worker"],
                                {"version": 1, "argv": argv, "worker_token": token,
                                 "scope_binding": asdict(scope) if scope else None}, remaining, runner)
    failures = {"worker_deadline", "worker_invalid_result", "worker_unavailable"}
    if not any(error.get("code") in failures for error in result.get("errors", [])):
        if result.get("kind") == "worker":
            result["kind"] = args.command
        return result
    data = result.get("data")
    binding = data.get("worker_binding") if isinstance(data, dict) else None
    if scope is not None:
        account = {"uid": scope.account_uid, "alias": scope.account_alias, "identity_source": "cron_bound_target"}
    elif isinstance(binding, dict) and binding.get("alias") == args.account:
        account = {"uid": binding["uid"], "alias": binding["alias"], "identity_source": "worker_configured_identity"}
    else:
        return envelope(args.command, errors=result["errors"], coverage={"full_history": False, "state_projection": "unavailable"})
    if not isinstance(account["uid"], str) or not re.fullmatch(r"[0-9]{1,32}", account["uid"]):
        raise RuntimeProblem("cron_account_not_verified")
    operation = None
    try:
        state = State(account["uid"])
        if args.command == "act" and args.action in {"comment", "reply", "like", "favourite"}:
            incoming = state.incoming(args.resource[2:]) if args.action == "reply" else None
            if args.action != "reply" or incoming:
                target = {"kind": args.action, "post_id": incoming["post_id"] if incoming else args.resource[2:],
                          "incoming_cid": incoming["cid"] if incoming else ""}
                operation = state.operation_for_target(target)
        projection = operation if operation else state.inbox() if args.command == "scan" and args.area == "incoming" else None
        coverage = dict(state.latest_coverage()) if args.command == "scan" else {}
        coverage.update({"full_history": False, "state_projection": "local_history", "worker_result_complete": False})
        if scope is not None:
            try:
                coverage["run_budget"] = state.run_usage(scope.run_id)
            except StateConflict:
                pass
        return state.publish_view(envelope(args.command, projection, account=account, errors=result["errors"], coverage=coverage))
    except Exception as error:
        summary = bounded_summary(operation)
        errors = result["errors"] + [{"code": "worker_state_unavailable", "type": type(error).__name__,
                                      "send_outcome_inferred": False}]
        return envelope(args.command, summary, account=account, errors=errors,
                        coverage={"full_history": False, "worker_result_complete": False,
                                  "state_projection": "partial" if summary else "unavailable"})


def worker_request(payload: dict, *, backend_factory=None, scope_loader=None, emitter=None, state_root=None, catalog=None) -> dict:
    from community import ACTIVE_CONTRACT, execute, parser, require_action_contract, validate
    from community_bindings import WorkingBackend
    validate_worker_request(payload)
    args = parser().parse_args(payload["argv"])
    validate(args, read_files=False)
    require_action_contract(args, 2 if backend_factory is not None else ACTIVE_CONTRACT)
    scope = (scope_loader or current_run_scope)()
    expected = asdict(scope) if scope is not None else None
    if payload["scope_binding"] != expected or (scope is not None and scope.contract_version != 2):
        raise RuntimeProblem("worker_run_binding_changed")
    if scope is not None and args.account != scope.account_alias:
        raise RuntimeProblem("cron_account_mismatch")
    text = validate(args)
    backend = (backend_factory or WorkingBackend)(args.account)
    account = backend.configured_account()
    if (account.alias != args.account or not isinstance(account.uid, str) or not re.fullmatch(r"[0-9]{1,32}", account.uid) or
            (scope is not None and (account.uid != scope.account_uid or account.alias != scope.account_alias))):
        raise RuntimeProblem("worker_account_not_verified")
    revision = backend.account_revision
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{16,128}", revision)):
        raise RuntimeProblem("worker_account_not_verified")
    if emitter is not None:
        emitter({"version": 1, "kind": "worker_binding", "worker_token": payload["worker_token"],
                 "request_digest": _request_digest(payload),
                 "data": {"uid": account.uid, "alias": account.alias, "revision": revision}})
    evidence = NativeEvidence(scope.run_id) if scope is not None else None
    return execute(args, text, backend=backend, state_root=state_root, catalog=catalog,
                   contract_version=2, scope=scope, evidence=evidence)
