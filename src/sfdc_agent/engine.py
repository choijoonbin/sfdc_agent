"""Isolated adapter for SFDC_Ui's Node conversion and verification code."""

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from .storage import write_json


class ConversionEngine(Protocol):
    def invoke(self, target: str, **context) -> dict: ...


class NodeEngine:
    def __init__(self, project: Path, ui_root: Path):
        self.runner = project / "bridge" / "runner.mjs"
        self.ui_root = ui_root
        self.source = ui_root / ".nextbss-workspace" / "sources" / "figma" / "AI 테스트용.fig"

    def invoke(self, target: str, **context) -> dict:
        environment = {key: os.environ[key] for key in (
            "PATH", "HOME", "USER", "TMPDIR", "LANG", "PLAYWRIGHT_CHROMIUM_EXECUTABLE",
        ) if key in os.environ}
        with TemporaryDirectory(prefix="sfdc-agent-request-") as directory:
            path = Path(directory) / "request.json"
            write_json(path, {"target": target, "context": {
                "uiRoot": str(self.ui_root), "sourcePath": str(self.source), **context,
            }})
            result = subprocess.run(
                ["node", str(self.runner), str(path)], cwd=self.ui_root,
                env=environment, capture_output=True, text=True, encoding="utf-8", timeout=300,
            )
        if result.returncode:
            raise RuntimeError(f"{target} engine failed: {result.stderr.strip()[:450]}")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise RuntimeError(f"{target} engine returned an invalid report.")
        return data
