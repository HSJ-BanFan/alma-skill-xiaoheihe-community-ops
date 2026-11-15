from __future__ import annotations

import time
import uuid
from pathlib import Path

from corpus_model import CorpusManifest
from corpus import CorpusRunner
from corpus_store import CorpusLocked, CorpusPaused, CorpusStore
from xhh_sdk.accounts import AccountStore
from xhh_sdk.exceptions import XhhConfigError


def register(commands):
    parser = commands.add_parser(
        "corpus", help="plan and inspect bounded multi-query corpus runs")
    actions = parser.add_subparsers(dest="corpus_action", required=True)
    plan = actions.add_parser("plan", help="validate and persist one manifest")
    plan.add_argument("manifest", type=Path)
    status = actions.add_parser(
        "status", help="inspect a run without loading credentials")
    status.add_argument("run_id")
    run = actions.add_parser("run", help="execute a planned corpus run")
    run.add_argument("run_id")
    run.add_argument("--max-requests", type=int)
    run.add_argument("--max-seconds", type=float)
    run.add_argument("--max-new-records", type=int)
    resume = actions.add_parser("resume", help="resume a stopped corpus run")
    resume.add_argument("run_id")
    resume.add_argument("--acknowledge-block", action="store_true")
    resume.add_argument("--max-requests", type=int)
    resume.add_argument("--max-seconds", type=float)
    resume.add_argument("--max-new-records", type=int)


def _load_bound_client(args, manifest, client_factory):
    accounts = AccountStore(args.data_dir)
    account = accounts.get(manifest.account_alias)
    if str(account.identity) != manifest.account_uid:
        raise XhhConfigError(
            "managed account identity does not match corpus manifest",
            code="account_identity_not_verified",
        )
    config = accounts.get_config(
        manifest.account_alias, protocol=manifest.reader.protocol)
    return client_factory(config)


def _emit(emitter, data, coverage, error=None):
    ok = coverage.get("status") not in {"blocked_human", "failed"}
    emitter(ok, "corpus", data, coverage=coverage, error=error)
    return 0 if ok else 1


def _event_envelope(store, event):
    if event.get("event") == "status":
        status = event["status"]
        return {
            "action": "run",
            **status,
        }, {
            "status": status["run"]["state"],
            "manifest_sha256": status["run"]["manifest_sha256"],
            "continuation": None,
        }, event.get("error")
    run_id = event.get("run_id") or _run_id_from_delivery(
        store, event["delivery_id"])
    replay = store.pending_delivery(run_id)
    status = store.status(run_id)
    data = {
        "action": "run",
        "delivery_id": event["delivery_id"],
        "revision": event["revision"],
        "run": status["run"],
        "queries": status["queries"],
        "pages": replay["pages"] if replay else [],
        "new_items": replay["new_items"] if replay else [],
        "updated_items": replay["updated_items"] if replay else [],
        "remainders": replay["remainders"] if replay else [],
        "counts": event["counts"],
    }
    return data, {
        "status": event["state"]["run_state"],
        "manifest_sha256": status["run"]["manifest_sha256"],
        "continuation": None,
        "delivery_id": event["delivery_id"],
    }, event["stop"]


def _run_id_from_delivery(store, delivery_id):
    return delivery_id.split(":", 1)[0]


def _emit_pending(store, run_id, emitter, lease):
    pending = store.pending_delivery(run_id)
    if pending is None:
        return None
    status = store.status(run_id)
    data = {
        "action": "status",
        **status,
        "delivery_id": pending["delivery_id"],
        "pages": pending["pages"],
        "new_items": pending["new_items"],
        "updated_items": pending["updated_items"],
        "remainders": pending["remainders"],
        "counts": pending["counts"],
    }
    coverage = {
        "status": pending["state"]["run_state"],
        "manifest_sha256": status["run"]["manifest_sha256"],
        "continuation": None,
        "replayed_delivery": True,
        "delivery_id": pending["delivery_id"],
    }
    code = _emit(emitter, data, coverage, pending["stop"])
    store.acknowledge_delivery(run_id, pending["revision"], lease=lease)
    return code, status


def dispatch(args, *, client_factory=None, parser=None, emitter=None):
    store = CorpusStore(args.data_dir)
    if args.corpus_action == "plan":
        manifest = CorpusManifest.load(args.manifest)
        planned = store.plan(manifest)
        data = {"action": "plan", **planned}
        coverage = {
            "status": planned["run"]["state"],
            "manifest_sha256": planned["run"]["manifest_sha256"],
        }
        return _emit(emitter, data, coverage)
    if args.corpus_action == "status":
        state = store.status(args.run_id)
        data = {"action": "status", **state}
        coverage = {
            "status": state["run"]["state"],
            "manifest_sha256": state["run"]["manifest_sha256"],
            "continuation": None,
        }
        return _emit(emitter, data, coverage)
    current = store.status(args.run_id)
    if (current["run"]["state"] == "blocked_human"
            and not getattr(args, "acknowledge_block", False)):
        data = {"action": "status", **current}
        return _emit(emitter, data, {
            "status": "blocked_human",
            "manifest_sha256": current["run"]["manifest_sha256"],
            "continuation": None,
        }, {
            "code": "corpus_block_acknowledgement_required",
            "message": "blocked corpus runs require --acknowledge-block",
            "retry_allowed": False,
        })
    manifest = store.manifest(args.run_id)
    if manifest.sha256() != current["run"]["manifest_sha256"]:
        raise XhhConfigError("corpus manifest hash changed")
    try:
        lease = store.acquire_lease(
            args.run_id,
            owner=f"corpus-{uuid.uuid4().hex}",
            lease_seconds=min(
                3600, max(60, manifest.limits.seconds + 30)),
            max_requests=getattr(args, "max_requests", None),
            max_seconds=getattr(args, "max_seconds", None),
            max_new_records=getattr(args, "max_new_records", None),
            acknowledge_block=getattr(args, "acknowledge_block", False),
        )
    except CorpusPaused:
        paused = store.status(args.run_id)
        data = {"action": "status", **paused}
        return _emit(emitter, data, {
            "status": paused["run"]["state"],
            "manifest_sha256": paused["run"]["manifest_sha256"],
            "continuation": None,
        })
    except CorpusLocked as error:
        locked = store.status(args.run_id)
        data = {"action": "status", **locked}
        return _emit(emitter, data, {
            "status": locked["run"]["state"],
            "manifest_sha256": locked["run"]["manifest_sha256"],
            "continuation": None,
        }, {
            "code": "corpus_run_locked",
            "message": str(error),
            "retry_allowed": True,
        })
    try:
        pending_result = _emit_pending(
            store, args.run_id, emitter, lease=lease)
        if pending_result is not None:
            return pending_result[0]
        from browser import Browser
        runner = CorpusRunner(
            store,
            manifest,
            lease,
            parser=parser,
            client_loader=lambda: _load_bound_client(
                args, manifest, client_factory),
            browser_factory=Browser,
        )
        events = runner.run()
        final = None
        while True:
            try:
                event = next(events)
            except StopIteration as stopped:
                final = stopped.value
                break
            if event.get("event") == "delivery":
                event["run_id"] = args.run_id
            data, coverage, error = _event_envelope(store, event)
            code = _emit(emitter, data, coverage, error)
            if event.get("event") == "delivery":
                store.acknowledge_delivery(
                    args.run_id, event["revision"], lease=lease)
            if code:
                return code
        if final is None:
            final = store.status(args.run_id)
        data = {"action": "resume" if args.corpus_action == "resume"
                else "run", **final}
        return _emit(emitter, data, {
            "status": final["run"]["state"],
            "manifest_sha256": final["run"]["manifest_sha256"],
            "continuation": None,
        })
    finally:
        store.release_lease(lease)
