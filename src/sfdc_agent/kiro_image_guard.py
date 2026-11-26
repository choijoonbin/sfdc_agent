"""One-use Kiro read_file guard; also executed as a standalone command hook.

Only standard-library imports: the isolated CLI does not inherit PYTHONPATH.
No hook output includes image bytes, model output, credentials or file paths.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def _read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Invalid image authority file.")
        value = stream.read(8193)
    if len(value) > 8192:
        raise ValueError("Image authority file exceeds its limit.")
    return json.loads(value)


def _write_once(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)


def verify_image_file(authority):
    path = Path(authority["imagePath"])
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Image reference path changed.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o222
                or info.st_size != authority["bytes"]):
            raise ValueError("Image reference is not immutable.")
        digest = hashlib.sha256(stream.read(authority["bytes"] + 1)).hexdigest()
    if digest != authority["sha256"]:
        raise ValueError("Image reference digest changed.")


def guard_image_read(manifest: Path, event: dict):
    authority = _read_private(manifest)
    phase = event.get("hook_event_name")
    inputs = event.get("tool_input")
    if phase not in {"PreToolUse", "PostToolUse"} or event.get("tool_name") != "read_file":
        raise ValueError("Unexpected image transport tool.")
    if not isinstance(inputs, dict) or inputs.get("path") != authority["imagePath"] or event.get("cwd") != authority["workspace"]:
        raise ValueError("Image transport tool scope does not match.")
    verify_image_file(authority)
    if phase == "PostToolUse" and not manifest.with_name("image-read-PreToolUse.json").is_file():
        raise ValueError("Image read has no corresponding authorization.")
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        raise ValueError("Image read has no session identity.")
    _write_once(manifest.with_name(f"image-read-{phase}.json"), {
        "phase": phase, "tool": "read_file", "sha256": authority["sha256"],
        "sessionSha256": hashlib.sha256(session.encode()).hexdigest(),
    })


def read_image_authority_receipts(manifest):
    return tuple(_read_private(manifest.with_name(f"image-read-{phase}.json"))
                 for phase in ("PreToolUse", "PostToolUse"))


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError
        body = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(body) > 64 * 1024:
            raise ValueError
        guard_image_read(Path(sys.argv[1]), json.loads(body))
    except BaseException:
        sys.stderr.write("The immutable image transport denied this tool invocation.\n")
        raise SystemExit(2)
