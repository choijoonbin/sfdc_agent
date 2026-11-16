"""Versioned instructions for interactive Figma conversion."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

from .config import project_root
from .prompt_templates import load_baseline_templates, template_placeholders, validate_template
from .prompt_variable_details import prompt_variable_details
from .storage import timestamp


BASELINE_RELEASE = "prompts-v1"
BASELINE_VERSION = "v1"
PROMPTS = {
    "screen.review-plan": ("화면 분석", "분석", False),
    "screen.generate": ("화면 LWC 생성", "생성", False),
    "screen.repair": ("화면 LWC 보정", "생성", False),
    "screen.analyze": ("Figma 디자인 구조 읽기", "Figma MCP", True),
    "screen.metadata": ("Figma 메타데이터 읽기", "Figma MCP", True),
    "screen.capture": ("Figma 화면 캡처", "Figma MCP", True),
    "screen.discovery": ("Figma 페이지·프레임 탐색", "Figma MCP", True),
    "screen.plan": ("파일럿 변환 계획", "파일럿", False),
    "screen.plan.repair": ("파일럿 계획 보정", "파일럿", False),
    "story.classification": ("Story 업무 분류", "분석", False),
    "story.analysis": ("Story 실행 명세 분석", "분석", False),
    "story.generate": ("Story LWC 생성·보정", "생성", False),
}


class RegistryError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class PromptRelease:
    id: str
    versions: Mapping[str, str]
    templates: Mapping[str, str]


def _release(release_id, versions, templates):
    return PromptRelease(release_id, MappingProxyType(versions), MappingProxyType(templates))


def _baseline():
    templates = load_baseline_templates()
    return _release(BASELINE_RELEASE, dict.fromkeys(templates, BASELINE_VERSION), templates)


def registry_path():
    return Path(os.environ.get("SFDC_AGENT_PROMPT_DB") or project_root() / ".prompt-management" / "prompts.sqlite3")


def runtime_release(release_id=None):
    if release_id == BASELINE_RELEASE:
        return _baseline()
    path = registry_path()
    if not path.exists():
        if release_id is None:
            return _baseline()
        raise RegistryError("PROMPT_RELEASE_NOT_FOUND", "저장된 작업의 프롬프트 릴리스를 찾을 수 없습니다.", 404)
    return PromptRegistry(path).release(release_id)


def _meta(operation):
    try:
        label, category, mcp = PROMPTS[operation]
    except KeyError:
        raise RegistryError("PROMPT_NOT_FOUND", "관리할 프롬프트를 찾을 수 없습니다.", 404) from None
    return {"id": operation, "label": label, "group": "Screen" if operation.startswith("screen.") else "Story",
            "category": category, "requiresFigmaMcp": mcp}


class PromptRegistry:
    def __init__(self, path=None):
        self.path = path if path is not None else registry_path()

    @contextmanager
    def _connect(self, *, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        # Opening by URI keeps read-only operations from creating an empty store.
        address = str(self.path) if write else self.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(address, uri=not write, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                self._initialize(db)
            else:
                db.execute("BEGIN")
            yield db

    def _initialize(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS versions (
            operation TEXT NOT NULL, number INTEGER NOT NULL, base_version TEXT, body TEXT NOT NULL,
            created_at TEXT NOT NULL, author TEXT NOT NULL, reason TEXT NOT NULL,
            validated INTEGER NOT NULL, published INTEGER NOT NULL, PRIMARY KEY (operation, number))""")
        db.execute("""CREATE TABLE IF NOT EXISTS drafts (
            operation TEXT PRIMARY KEY, number INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS releases (
            id TEXT PRIMARY KEY, versions TEXT NOT NULL, created_at TEXT NOT NULL,
            actor TEXT NOT NULL, operation TEXT, action TEXT NOT NULL)""")
        db.execute("CREATE TABLE IF NOT EXISTS active_release (singleton INTEGER PRIMARY KEY CHECK (singleton=1), id TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM active_release WHERE singleton=1").fetchone():
            return
        baseline = _baseline()
        db.executemany("INSERT INTO versions VALUES (?, 1, NULL, ?, '', 'NOVA', '기존 변환 기준 버전', 1, 1)", baseline.templates.items())
        db.execute("INSERT INTO releases VALUES (?, ?, ?, 'NOVA', NULL, 'baseline')",
                   (baseline.id, json.dumps(dict(baseline.versions)), timestamp()))
        db.execute("INSERT INTO active_release VALUES (1, ?)", (baseline.id,))

    @staticmethod
    def _active(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_release'").fetchone():
            return None
        row = db.execute("SELECT id FROM active_release WHERE singleton=1").fetchone()
        if row is None:
            raise RegistryError("PROMPT_RELEASE_INVALID", "활성 프롬프트 릴리스를 확인할 수 없습니다.", 503)
        return row['id']

    def _read_release(self, db, release_id=None):
        release_id = release_id or self._active(db) or BASELINE_RELEASE
        if release_id == BASELINE_RELEASE:
            return _baseline()
        if self._active(db) is None:
            raise RegistryError("PROMPT_RELEASE_NOT_FOUND", "저장된 작업의 프롬프트 릴리스를 찾을 수 없습니다.", 404)
        row = db.execute("SELECT versions FROM releases WHERE id=?", (release_id,)).fetchone()
        if row is None:
            raise RegistryError("PROMPT_RELEASE_NOT_FOUND", "저장된 작업의 프롬프트 릴리스를 찾을 수 없습니다.", 404)
        versions = json.loads(row['versions'])
        baselines = load_baseline_templates()
        if set(versions) != set(baselines):
            raise RegistryError("PROMPT_RELEASE_INVALID", "프롬프트 릴리스의 구성 정보가 올바르지 않습니다.", 503)
        templates = {operation: self._version(db, operation, version)['body'] for operation, version in versions.items()}
        return _release(release_id, versions, templates)

    def release(self, release_id=None):
        with self._connect() as db:
            return self._read_release(db, release_id)

    @staticmethod
    def _version(db, operation, version):
        row = db.execute("SELECT * FROM versions WHERE operation=? AND number=?", (operation, int(version[1:]))).fetchone()
        if row is None:
            raise RegistryError("PROMPT_VERSION_NOT_FOUND", "해당 프롬프트 버전을 찾을 수 없습니다.", 404)
        return row

    @staticmethod
    def _draft(db, operation):
        return db.execute("SELECT versions.* FROM drafts JOIN versions USING (operation, number) WHERE operation=?", (operation,)).fetchone()

    @staticmethod
    def _entry(row):
        return {"version": f"v{row['number']}", "baseVersion": row['base_version'],
                "templates": {row['operation']: row['body']}, "createdAt": row['created_at'] or None,
                "author": row['author'], "reason": row['reason'], "validated": bool(row['validated'])}

    @staticmethod
    def _initial(operation, release):
        return {"version": BASELINE_VERSION, "templates": {operation: release.templates[operation]},
                "createdAt": None, "author": "NOVA", "reason": "기존 변환 기준 버전", "validated": True}

    def catalog(self):
        if not self.path.exists():
            release = _baseline()
            return self._catalog(release, {})
        with self._connect() as db:
            release = self._read_release(db)
            drafts = dict(db.execute("SELECT operation, number FROM drafts")) if self._active(db) else {}
            return self._catalog(release, drafts)

    @staticmethod
    def _catalog(release, drafts):
        return {"activeReleaseId": release.id, "operations": [
            {**_meta(operation), "activeVersion": release.versions[operation],
             "draftVersion": f"v{drafts[operation]}" if operation in drafts else None} for operation in PROMPTS]}

    def detail(self, operation):
        _meta(operation)
        if self.path.exists():
            with self._connect() as db:
                if self._active(db):
                    return self._detail_from_db(db, operation)
        release = _baseline()
        baseline = self._initial(operation, release)
        return self._detail(operation, release, baseline, None, [baseline])

    @staticmethod
    def _detail(operation, release, active, draft, history):
        variables = template_placeholders(operation)
        return {"activeReleaseId": release.id,
                "operation": {**_meta(operation), "variables": variables, "editableKeys": [operation],
                              "variableDetails": prompt_variable_details(operation, variables)},
                "active": active, "draft": draft, "history": history}

    def _detail_from_db(self, db, operation):
        release = self._read_release(db)
        draft = self._draft(db, operation)
        history = db.execute("SELECT * FROM versions WHERE operation=? AND published=1 ORDER BY number DESC", (operation,))
        active = self._version(db, operation, release.versions[operation])
        return self._detail(operation, release, self._entry(active), self._entry(draft) if draft else None,
                            [self._entry(row) for row in history])

    @staticmethod
    def _check_release(active, expected):
        if active != expected:
            raise RegistryError("PROMPT_RELEASE_CHANGED", "활성 릴리스가 바뀌었습니다. 최신 버전을 불러온 뒤 다시 시도해 주세요.")

    def save_draft(self, operation, templates, expected_version, expected_release, reason, actor):
        _meta(operation)
        if not reason.strip():
            raise RegistryError("PROMPT_REASON_REQUIRED", "변경 사유를 입력해 주세요.", 422)
        if set(templates) != {operation}:
            raise RegistryError("PROMPT_TEMPLATE_INVALID", "이 작업의 프롬프트 본문을 입력해 주세요.", 422)
        body = templates[operation]
        self._validate(operation, body)
        with self._connect(write=True) as db:
            release = self._read_release(db)
            self._check_release(release.id, expected_release)
            previous = self._draft(db, operation)
            if (f"v{previous['number']}" if previous else None) != expected_version:
                raise RegistryError("PROMPT_DRAFT_CHANGED", "초안이 바뀌었습니다. 최신 버전을 불러온 뒤 다시 시도해 주세요.")
            if body == release.templates[operation]:
                raise RegistryError("PROMPT_NO_CHANGE", "현재 활성 프롬프트와 다른 내용을 입력해 주세요.", 422)
            next_number = db.execute("SELECT MAX(number)+1 FROM versions WHERE operation=?", (operation,)).fetchone()[0]
            db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
                       (operation, next_number, release.versions[operation], body, timestamp(), actor, reason))
            db.execute("INSERT INTO drafts VALUES (?, ?) ON CONFLICT(operation) DO UPDATE SET number=excluded.number",
                       (operation, next_number))
            return self._detail_from_db(db, operation)

    @staticmethod
    def _validate(operation, text):
        try:
            validate_template(operation, text)
        except (ValueError, KeyError) as error:
            raise RegistryError("PROMPT_TEMPLATE_INVALID", str(error), 422) from None

    def validate(self, operation, version):
        from .prompt_validation import preview_operation

        _meta(operation)
        with self._connect(write=True) as db:
            draft = self._draft(db, operation)
            if draft is None or f"v{draft['number']}" != version:
                raise RegistryError("PROMPT_DRAFT_CHANGED", "현재 초안을 불러와 검증해 주세요.")
            release = self._read_release(db)
            if draft['base_version'] != release.versions[operation]:
                raise RegistryError("PROMPT_DRAFT_CHANGED", "기준 버전이 바뀌었습니다. 최신 버전에서 초안을 다시 저장해 주세요.")
            self._validate(operation, draft['body'])
            try:
                rendered, cases = preview_operation(operation, {**release.templates, operation: draft['body']})
            except (ValueError, KeyError) as error:
                raise RegistryError("PROMPT_RENDER_FAILED", str(error), 422) from None
            db.execute("UPDATE versions SET validated=1 WHERE operation=? AND number=?", (operation, draft['number']))
            return {"valid": True, "preview": {operation: rendered}, "cases": cases}

    def promote(self, operation, version, expected_release, actor, *, restore=False):
        _meta(operation)
        from .prompt_validation import preview_operation

        with self._connect(write=True) as db:
            release = self._read_release(db)
            self._check_release(release.id, expected_release)
            if release.versions[operation] == version:
                return self._detail_from_db(db, operation)
            candidate = self._version(db, operation, version)
            if restore:
                if not candidate['published']:
                    raise RegistryError("PROMPT_RESTORE_UNAVAILABLE", "이전에 활성화한 버전만 복원할 수 있습니다.")
            else:
                draft = self._draft(db, operation)
                if draft is None or draft['number'] != candidate['number'] or candidate['base_version'] != release.versions[operation]:
                    raise RegistryError("PROMPT_DRAFT_CHANGED", "현재 활성 버전을 기준으로 만든 최신 초안을 선택해 주세요.")
                if not candidate['validated']:
                    raise RegistryError("PROMPT_VALIDATION_REQUIRED", "초안을 검증한 뒤 활성화해 주세요.")
            try:
                preview_operation(operation, {**release.templates, operation: candidate['body']})
            except (ValueError, KeyError) as error:
                raise RegistryError("PROMPT_RENDER_FAILED", str(error), 422) from None
            versions = {**release.versions, operation: version}
            release_id = f"prompts-{uuid4().hex}"
            db.execute("INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?)",
                       (release_id, json.dumps(versions), timestamp(), actor, operation, "restore" if restore else "activate"))
            db.execute("UPDATE active_release SET id=? WHERE singleton=1", (release_id,))
            if not restore:
                db.execute("UPDATE versions SET published=1 WHERE operation=? AND number=?", (operation, candidate['number']))
                db.execute("DELETE FROM drafts WHERE operation=?", (operation,))
            return self._detail_from_db(db, operation)
