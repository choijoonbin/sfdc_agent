"""Command line and worker entry points."""

import argparse
import fcntl
import re
import sys
import time
from pathlib import Path
from .config import project_root, store_for_project, ui_root

from .contracts import PilotRequest
from .engine import NodeEngine
from .pilot import run_pilot
from .providers import create_provider
from .storage import RunStore


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise argparse.ArgumentTypeError("Expected a 32-character run ID.")
    return value


def execute(store: RunStore, row: dict) -> int:
    run_id = row["id"]
    try:
        request = PilotRequest.model_validate_json(row["request"])
        provider = create_provider(request.provider,
                                   timeout=request.model_timeout, model=request.model)
        result = run_pilot(store, run_id, request, NodeEngine(project_root(), ui_root()), provider)
        status = "complete" if result["modelPlansApplied"] else "partial"
        store.update(run_id, "done", status)
        print(store.directory(run_id) / "comparison.json", flush=True)
        return 0 if status == "complete" else 2
    except (Exception, KeyboardInterrupt) as error:
        stage = store.get(run_id)["stage"]
        store.update(run_id, stage, "failed", str(error)[:500] or type(error).__name__)
        print(f"{stage}: {error}", file=sys.stderr, flush=True)
        return 1


def worker(store: RunStore, run_id: str | None = None, *, resume=False, once=False) -> int:
    with (store.root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another local worker is active.", file=sys.stderr)
            return 1
        if resume:
            existing = store.get(run_id)
            if existing is None or existing["status"] not in {"queued", "failed", "running", "partial"}:
                print("Only interrupted, failed, partial or queued runs can be resumed.", file=sys.stderr)
                return 1
            store.update(run_id, existing["stage"], "queued")
        while True:
            row = store.claim(run_id)
            if row:
                status = execute(store, row)
                if once or run_id:
                    return status
            elif once or run_id:
                return 0
            else:
                time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(prog="sfdc-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("pilot")
    run.add_argument("--provider", choices=("kiro", "openai"), default="kiro")
    run.add_argument("--model")
    run.add_argument("--model-timeout", type=int, default=PilotRequest.model_fields["model_timeout"].default)
    run.add_argument("--target", choices=("all", "screen", "story"), default="all")
    run.add_argument("--reference-manifest", type=Path)
    work = sub.add_parser("worker")
    work.add_argument("--once", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("id", type=valid_id)
    sub.add_parser("serve")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        uvicorn.run("sfdc_agent.api:app", host="127.0.0.1", port=8765, timeout_graceful_shutdown=8)
        return 0
    store = store_for_project()
    if args.command == "worker":
        return worker(store, once=args.once)
    if args.command == "resume":
        return worker(store, args.id, resume=True)
    request = PilotRequest(
        provider=args.provider, model=args.model, target=args.target, model_timeout=args.model_timeout,
        reference_manifest=str(args.reference_manifest.resolve(strict=True)) if args.reference_manifest else None,
    )
    run_id = store.enqueue(request)
    print(f"Run {run_id}", flush=True)
    return worker(store, run_id)
