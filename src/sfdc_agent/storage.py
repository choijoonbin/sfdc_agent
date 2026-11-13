"""Persist runs and stage receipts outside the UI server."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import PilotRequest


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class RunStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "runs.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error TEXT
                )
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def enqueue(self, request: PilotRequest) -> str:
        run_id = uuid4().hex
        now = timestamp()
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, 'queued', 'queued', ?, ?, NULL)",
                       (run_id, request.model_dump_json(), now, now))
        return run_id

    def get(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def claim(self, run_id: str | None = None) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if run_id:
                row = db.execute("SELECT * FROM runs WHERE status = 'queued' AND id = ?", (run_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE runs SET status = 'running', updated_at = ? WHERE id = ?",
                       (timestamp(), row["id"]))
        return dict(row)

    def update(self, run_id: str, stage: str, status: str = "running", error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET stage = ?, status = ?, error = ?, updated_at = ? WHERE id = ?",
                       (stage, status, error, timestamp(), run_id))

    def directory(self, run_id: str) -> Path:
        return self.root / "runs" / run_id
