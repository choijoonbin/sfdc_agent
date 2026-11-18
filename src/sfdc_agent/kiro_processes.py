"""Bounded, metadata-only ownership of one isolated Kiro CLI process tree."""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess


class ProcessCleanupError(Exception):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    group: int
    uid: int
    state: str
    birth: str
    executable: str

    @property
    def identity(self):
        return self.pid, self.uid, self.birth


def _snapshot():
    # Never inspect argv or environment: both may contain model credentials/data.
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,uid=,stat=,lstart=,comm=", "-ww"],
        capture_output=True, text=True, timeout=1, check=True,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 10)
        if len(fields) != 11:
            raise ProcessCleanupError("KIRO_PROCESS_METADATA_INVALID")
        rows.append(Process(*(int(value) for value in fields[:4]), fields[4], " ".join(fields[5:10]), fields[10]))
    return rows


class OwnedProcesses:
    def __init__(self, process, workspace: Path):
        self.process = process
        self.leader = process.pid
        self.root = workspace.parent.resolve()
        self.prefixes = {str(workspace.parent.absolute()) + "/", str(self.root) + "/"}
        self.known = {}

    def _inside(self, value):
        if value.endswith(" (deleted)"):
            value = value[:-10]
        return any(value.startswith(prefix) for prefix in self.prefixes) and Path(value).resolve().is_relative_to(self.root)

    def _private_executable(self, row):
        if self._inside(row.executable):
            return True
        # Linux comm is a basename, unlike macOS. The executable link is metadata,
        # not command arguments; a detached CLI support node lives in this root.
        try:
            return self._inside(os.readlink(f"/proc/{row.pid}/exe"))
        except OSError:
            return False

    async def observe(self):
        try:
            rows = await asyncio.to_thread(_snapshot)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ProcessCleanupError("KIRO_PROCESS_METADATA_UNAVAILABLE") from error
        rows = [row for row in rows if row.uid == os.getuid() and row.pid != os.getpid()]
        owned = {row.pid for row in rows if self.known.get(row.pid) == row.identity}
        # The direct child PID is held by asyncio; never adopt a replacement PID.
        owned.update(row.pid for row in rows if row.pid == self.leader and self.process.returncode is None
                     and (row.pid not in self.known or self.known[row.pid] == row.identity))
        owned.update(row.pid for row in rows if self._private_executable(row))
        changed = True
        while changed:
            prior = len(owned)
            owned.update(row.pid for row in rows if row.parent in owned)
            changed = len(owned) != prior
        selected = [row for row in rows if row.pid in owned]
        for row in selected:
            if row.pid in self.known and self.known[row.pid] != row.identity:
                raise ProcessCleanupError("KIRO_PROCESS_IDENTITY_CHANGED")
            self.known[row.pid] = row.identity
        return [row for row in selected if not row.state.startswith("Z")]

    async def quiesce(self):
        # The CLI may exit successfully while its detached support node is still
        # flushing session files. Signal only this exact run, on every outcome.
        rows = await self.observe()
        for action, grace in ((signal.SIGTERM, 0.4), (signal.SIGKILL, 1.0)):
            for row in sorted(rows, key=lambda value: value.pid == self.leader):
                try:
                    os.kill(row.pid, action)
                except ProcessLookupError:
                    pass
            deadline = asyncio.get_running_loop().time() + grace
            while rows and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.03)
                rows = await self.observe()
            if not rows:
                # One more observation catches a final support-process spawn.
                await asyncio.sleep(0.03)
                rows = await self.observe()
                if not rows:
                    return
        raise ProcessCleanupError("KIRO_OWNED_PROCESS_STILL_RUNNING")
