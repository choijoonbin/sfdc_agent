"""Install a pinned UI runtime artifact without executing archive contents.

This module deliberately uses only the Python standard library. It runs before
any npm command, accepts only the UI artifact protocol, and never merges trees.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 5000
NODE_VERSION = "26.13.0"
DIGEST = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")


class ArtifactError(ValueError):
    pass


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("Duplicate JSON key")
        result[key] = value
    return result


def read_json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ArtifactError("Invalid artifact JSON") from None


def valid_path(name):
    return (isinstance(name, str) and len(name) <= 240
            and re.fullmatch(r"[A-Za-z0-9_.@/-]+", name) is not None
            and all(part and part not in {".", "..", "node_modules"}
                    and not part.startswith(".") for part in name.split("/")))


def read_archive(compressed):
    if len(compressed) > MAX_BYTES:
        raise ArtifactError("Archive size limit exceeded")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ArtifactError("Expanded archive size limit exceeded")
    except (OSError, EOFError):
        raise ArtifactError("Invalid gzip archive") from None
    files, cursor = {}, 0

    def field(header, start, length):
        try:
            return header[start:start + length].split(b"\0", 1)[0].decode("ascii")
        except UnicodeError:
            raise ArtifactError("Invalid archive header") from None

    while cursor + 512 <= len(data):
        header = data[cursor:cursor + 512]
        cursor += 512
        if not any(header):
            if len(data) - cursor < 512 or any(data[cursor:]):
                raise ArtifactError("Invalid tar terminator")
            return files
        checksum = field(header, 148, 8).strip()
        actual = sum(header[:148]) + 8 * 32 + sum(header[156:])
        if not re.fullmatch(r"[0-7]+", checksum) or int(checksum, 8) != actual:
            raise ArtifactError("Invalid tar checksum")
        prefix, name = field(header, 345, 155), field(header, 0, 100)
        name = prefix + "/" + name if prefix else name
        if not valid_path(name) or name in files or header[257:263] != b"ustar\0":
            raise ArtifactError("Invalid or duplicate archive path")
        if header[156] not in {0, 48} or field(header, 157, 100):
            raise ArtifactError("Links and special files are forbidden")
        size_text = field(header, 124, 12).strip()
        if not re.fullmatch(r"[0-7]+", size_text):
            raise ArtifactError("Invalid tar size")
        size = int(size_text, 8)
        if cursor + size > len(data):
            raise ArtifactError("Truncated archive")
        files[name] = data[cursor:cursor + size]
        cursor += ((size + 511) // 512) * 512
        if len(files) > MAX_FILES:
            raise ArtifactError("Archive file count limit exceeded")
    raise ArtifactError("Missing tar terminator")


def tree_sha256(files):
    return sha256("".join(name + "\0" + sha256(files[name]) + "\n"
                          for name in sorted(files)).encode())


def verify_runtime(files):
    required = {"package.json", "package-lock.json", "managed-runtime.json"}
    if not required.issubset(files) or any(name not in required and not name.startswith("nextbss/")
                                         for name in files):
        raise ArtifactError("Invalid runtime inventory")
    manifest = read_json(files["managed-runtime.json"])
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, dict) or set(entries) != set(files) - {"managed-runtime.json"}:
        raise ArtifactError("Runtime manifest inventory mismatch")
    for name, digest in entries.items():
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest) or sha256(files[name]) != digest:
            raise ArtifactError("Runtime content hash mismatch")
    # The existing Node packager hashes JSON.stringify(entries), preserving key order.
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    if manifest.get("sha256") != sha256(encoded):
        raise ArtifactError("Runtime manifest hash mismatch")
    return manifest


def verify_artifact(archive, artifacts, *, expected_sha256, expected_commit, expected_manifest_sha256):
    if (not DIGEST.fullmatch(expected_sha256) or not COMMIT.fullmatch(expected_commit)
            or not DIGEST.fullmatch(expected_manifest_sha256)):
        raise ArtifactError("Independent artifact pins are required")
    metadata = read_json(artifacts)
    if (not isinstance(metadata, dict) or metadata.get("schemaVersion") != 1
            or metadata.get("kind") != "sfdc-ui-server-artifacts"
            or not isinstance(metadata.get("source"), dict) or not isinstance(metadata.get("toolchain"), dict)
            or metadata.get("source", {}).get("commit") != expected_commit
            or metadata.get("toolchain", {}).get("node") != NODE_VERSION):
        raise ArtifactError("Artifact identity or toolchain mismatch")
    items = metadata.get("artifacts")
    if not isinstance(items, list) or len(items) != 2 or {item.get("name") for item in items if isinstance(item, dict)} != {"ui", "node-runtime"}:
        raise ArtifactError("Incomplete artifact manifest")
    item = next(item for item in items if item.get("name") == "node-runtime")
    if (item.get("file") != "sfdc-node-runtime.tar.gz" or item.get("commit") != expected_commit
            or item.get("sha256") != expected_sha256 or sha256(archive) != expected_sha256
            or item.get("bytes") != len(archive)
            or item.get("runtimeManifestSha256") != expected_manifest_sha256):
        raise ArtifactError("Archive pin or size mismatch")
    files = read_archive(archive)
    if (len(files) != item.get("fileCount") or tree_sha256(files) != item.get("treeSha256")
            or sha256(files.get("managed-runtime.json", b"")) != expected_manifest_sha256):
        raise ArtifactError("Artifact tree or manifest identity mismatch")
    runtime = verify_runtime(files)
    return files, {"schemaVersion": 1, "kind": "sfdc-agent-runtime-installation", "uiCommit": expected_commit,
                   "archiveSha256": expected_sha256, "runtimeManifestSha256": expected_manifest_sha256,
                   "runtimeSha256": runtime["sha256"], "node": NODE_VERSION, "fileCount": len(files)}


def install(archive_path, artifacts_path, destination, **pins):
    archive_path, artifacts_path, destination = Path(archive_path), Path(artifacts_path), Path(destination)
    if archive_path.name != "sfdc-node-runtime.tar.gz" or archive_path.stat().st_size > MAX_BYTES:
        raise ArtifactError("Invalid runtime archive")
    if artifacts_path.stat().st_size > 256 * 1024:
        raise ArtifactError("Artifact manifest is too large")
    files, receipt = verify_artifact(archive_path.read_bytes(), artifacts_path.read_bytes(), **pins)
    if destination.exists() or destination.is_symlink() or destination.parent.resolve() != destination.parent:
        raise ArtifactError("Installation requires a new path with a real parent")
    scratch = Path(tempfile.mkdtemp(prefix=".sfdc-runtime-", dir=destination.parent))
    try:
        for name, data in files.items():
            path = scratch / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(0o644)
        for directory, _, _ in os.walk(scratch):
            Path(directory).chmod(0o755)
        # Parent is owned by the image build user; no concurrent untrusted writer.
        scratch.rename(destination)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "artifacts", "destination", "expected-sha256", "expected-commit", "expected-manifest-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    receipt = install(args.archive, args.artifacts, args.destination, expected_sha256=args.expected_sha256,
                      expected_commit=args.expected_commit, expected_manifest_sha256=args.expected_manifest_sha256)
    Path(args.receipt).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        main()
    except (ArtifactError, OSError):
        raise SystemExit("Runtime installation failed: verify artifact pins, inventory, paths and hashes.") from None
