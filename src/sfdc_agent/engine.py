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
        def configured_path(name: str, *, directory=False) -> Path:
            value = os.environ.get(name)
            if not value or not value.strip():
                raise RuntimeError(f"Set {name} explicitly for the offline comparison pilot.")
            path = Path(value).resolve(strict=True)
            if not (path.is_dir() if directory else path.is_file()):
                raise RuntimeError(f"{name} must identify an existing {'directory' if directory else 'file'}.")
            return path

        self.source = configured_path("NOVA_PILOT_FIGMA_SOURCE")
        self.release_root = configured_path("SFDC_MANAGED_RELEASE_ROOT", directory=True)
        self.source_paths = {
            "guide": str(configured_path("NOVA_PILOT_GUIDE_FIG")),
            "library": str(configured_path("NOVA_PILOT_LIBRARY_FIG")),
            "buttonPlanningEvidence": str(self.source),
        }

    def invoke(self, target: str, **context) -> dict:
        environment = {key: os.environ[key] for key in (
            "PATH", "HOME", "USER", "TMPDIR", "LANG", "PLAYWRIGHT_CHROMIUM_EXECUTABLE",
        ) if key in os.environ}
        with TemporaryDirectory(prefix="sfdc-agent-request-") as directory:
            path = Path(directory) / "request.json"
            write_json(path, {"target": target, "context": {
                "uiRoot": str(self.ui_root), "sourcePath": str(self.source),
                "releaseRoot": str(self.release_root), "sourcePaths": self.source_paths, **context,
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
