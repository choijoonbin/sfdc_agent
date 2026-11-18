"""Paths shared by the API and conversion worker."""

import os
from pathlib import Path

from .storage import RunStore


def project_root() -> Path:
    return Path(os.environ.get("SFDC_AGENT_ROOT", Path(__file__).resolve().parents[2])).resolve()


def ui_root() -> Path:
    return Path(os.environ.get("SFDC_UI_ROOT", project_root().parent / "SFDC_Ui")).resolve()


def engine_root() -> Path:
    return Path(os.environ.get("SFDC_MANAGED_ENGINE_ROOT", ui_root())).resolve()


def store_for_project() -> RunStore:
    return RunStore(project_root() / ".pilot")
