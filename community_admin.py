"""Supported local administration. Does not expose model execution or public writes."""
from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.request
from pathlib import Path

JOB_ID = "e146f2c4-58b7-4528-a6cf-7237dc2ab8ac"
MARKER = f"[xhh-community:{JOB_ID}]"
MATCHER = r"\[xhh-community:" + JOB_ID + r"\]"
HERE = Path(__file__).resolve().parent
PROTECTED_JOB = ("id", "name", "scheduleType", "schedule", "executionMode", "enabled", "createdAt")


class AdminError(Exception):
    pass


class NativeAPI:
    def __init__(self, timeout: float = 15):
        self.timeout = timeout

    def request(self, method: str, path: str, body=None):
        allowed = {("GET", "/api/cron/jobs"), ("PUT", f"/api/cron/jobs/{JOB_ID}"),
                   ("GET", "/api/hooks"), ("PUT", "/api/hooks"), ("POST", "/api/hooks/reload")}
        if (method, path) not in allowed:
            raise AdminError("unsupported_route_or_method")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request("http://127.0.0.1:23001" + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", "User-Agent": "local-community-admin/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)


def get_job(api) -> dict:
    rows = api.request("GET", "/api/cron/jobs")
    if not isinstance(rows, list):
        raise AdminError("unexpected_job_list")
    matches = [job for job in rows if isinstance(job, dict) and job.get("id") == JOB_ID]
    if len(matches) != 1 or not isinstance(matches[0].get("payload"), dict):
        raise AdminError("job_not_resolved")
    return copy.deepcopy(matches[0])


def update_cron(prompt: str, api=None) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or MARKER not in prompt:
        raise AdminError("operational_marker_required")
    api = api or NativeAPI()
    before = get_job(api)
    expected_payload = dict(before["payload"], agentTurn=prompt)
    try:
        api.request("PUT", f"/api/cron/jobs/{JOB_ID}", {"payload": {"agentTurn": prompt}})
    except Exception:
        # PUT may have reached the service; verify, never retry the write blindly.
        after = get_job(api)
        if after["payload"].get("agentTurn") != prompt:
            raise AdminError("cron_update_uncertain") from None
    else:
        after = get_job(api)
    if after["payload"] != expected_payload or any(after.get(key) != before.get(key) for key in PROTECTED_JOB):
        raise AdminError("cron_readback_drift")
    return {"job_id": JOB_ID, "updated": True, "prompt_verified": True,
            "preserved": list(PROTECTED_JOB) + ["payload fields other than agentTurn"],
            "run_requested": False}


def hook_command(executable: str | None = None) -> str:
    exe = Path(executable or sys.executable).as_posix()
    script = (HERE / "community_context.py").as_posix()
    return f'"{exe}" -I -X utf8 "{script}"'


def merge_hook_config(config: dict, command: str) -> dict:
    if not isinstance(config, dict):
        raise AdminError("unexpected_hook_config")
    merged = copy.deepcopy(config)
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AdminError("unexpected_hook_groups")
    rules = hooks.setdefault("chat.message.willSend", [])
    if not isinstance(rules, list):
        raise AdminError("unexpected_hook_rules")
    owned = {"matcher": MATCHER, "hooks": [{"command": command, "timeout": 10000, "enabled": True}]}
    hooks["chat.message.willSend"] = [rule for rule in rules
                                     if not isinstance(rule, dict) or rule.get("matcher") != MATCHER] + [owned]
    return merged


def register_hook(api=None, command: str | None = None) -> dict:
    api = api or NativeAPI()
    before = api.request("GET", "/api/hooks")
    if not isinstance(before, dict) or not isinstance(before.get("config"), dict):
        raise AdminError("unexpected_hook_envelope")
    merged = merge_hook_config(before["config"], command or hook_command())
    api.request("PUT", "/api/hooks", {"config": merged})
    api.request("POST", "/api/hooks/reload", {})
    after = api.request("GET", "/api/hooks")
    if after.get("config") != merged:
        raise AdminError("hook_readback_drift")
    return {"registered": True, "event": "chat.message.willSend", "config_verified": True,
            "generation_path_verified": False, "restart_requested": False}


def import_evidence(args) -> dict:
    sys.path.insert(0, str(HERE))
    from community_bindings import WorkingBackend
    from community_facade import Community
    if args.account != "alt":
        raise AdminError("audit_account_mismatch")
    community = Community(WorkingBackend("alt"))
    if community.account.uid != "76644009":
        raise AdminError("configured_audit_identity_mismatch")
    return community.import_evidence(args.evidence, args.floors, args.profile)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local community configuration and evidence maintenance")
    sub = p.add_subparsers(dest="command", required=True)
    cron = sub.add_parser("cron", help="Replace only the existing job's operational payload")
    cron.add_argument("--prompt-file", required=True, type=Path)
    hook = sub.add_parser("hook", help="Register a job-scoped native message handler")
    hook.add_argument("--register", action="store_true", required=True)
    migration = sub.add_parser("import", help="Import attributed local audit evidence idempotently")
    migration.add_argument("--account", choices=("alt", "main"), default="alt")
    migration.add_argument("--evidence", type=Path, required=True)
    migration.add_argument("--floors", type=Path, required=True)
    migration.add_argument("--profile", type=Path, required=True)
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "cron":
            result = update_cron(args.prompt_file.read_text(encoding="utf-8-sig"))
        elif args.command == "hook":
            result = register_hook()
        else:
            result = import_evidence(args)
        out = {"version": 1, "ok": True, "data": result, "errors": []}
        code = 0
    except Exception as error:
        detail = str(error) if isinstance(error, AdminError) else type(error).__name__
        out = {"version": 1, "ok": False, "data": None, "errors": [{"code": detail}]}
        code = 1
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
