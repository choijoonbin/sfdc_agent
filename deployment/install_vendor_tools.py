"""Install explicitly approved server CLI/browser archives, never a latest URL.

The build contract is public metadata: HTTPS URL, fixed version, archive hash,
entrypoint hash and one allowed host list. No OAuth/API secret is used here.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 768 * 1024 * 1024
MAX_FILES = 10000
DIGEST = re.compile(r"[a-f0-9]{64}")


class ToolError(ValueError):
    pass


def _require(value, message):
    if not value:
        raise ToolError(message)


def _path(value):
    return (isinstance(value, str) and len(value) <= 240
            and re.fullmatch(r"[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*", value) is not None
            and all(part not in {".", ".."} and not part.startswith(".") for part in value.split("/")))


def validate_contract(value):
    _require(isinstance(value, dict) and set(value) == {"kind", "schemaVersion", "approvedDownloadHosts", "tools"}
             and value["kind"] == "sfdc-agent-vendor-tools" and value["schemaVersion"] == 1,
             "Invalid vendor tool contract")
    hosts = value["approvedDownloadHosts"]
    _require(isinstance(hosts, list) and len(hosts) <= 8
             and all(isinstance(host, str) and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                     and host not in {"localhost", "127.0.0.1"} for host in hosts), "Invalid approved download hosts")
    tools, names = value["tools"], set()
    _require(isinstance(tools, list) and len(tools) <= 2, "Invalid vendor tool list")
    for tool in tools:
        _require(isinstance(tool, dict) and set(tool) == {"name", "version", "url", "sha256", "format", "stripPrefix", "entrypoint", "entrypointSha256"}, "Invalid vendor tool fields")
        _require(tool["name"] in {"kiro-cli", "chrome"} and tool["name"] not in names, "Invalid vendor tool name")
        names.add(tool["name"])
        _require(isinstance(tool["version"], str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", tool["version"]), "A fixed tool version is required")
        parsed = urlsplit(tool["url"])
        _require(parsed.scheme == "https" and parsed.hostname in hosts and not parsed.username and not parsed.password
                 and parsed.port in {None, 443} and not parsed.query and not parsed.fragment
                 and "latest" not in parsed.path.lower(), "A pinned approved HTTPS download is required")
        _require(all(isinstance(tool[key], str) and DIGEST.fullmatch(tool[key]) for key in ("sha256", "entrypointSha256")), "Tool hashes are required")
        _require(tool["format"] in {"tar.gz", "tar.xz", "zip"} and _path(tool["entrypoint"])
                 and (tool["stripPrefix"] == "" or _path(tool["stripPrefix"])), "Invalid vendor archive layout")
    return value


class _PinnedRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        _require(parsed.scheme == "https" and parsed.hostname in self.hosts and parsed.port in {None, 443}
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                 "Unapproved vendor download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(tool, hosts):
    request = Request(tool["url"], headers={"User-Agent": "sfdc-agent-artifact-builder/1"})
    with build_opener(_PinnedRedirect(hosts)).open(request, timeout=30) as response:
        data = response.read(MAX_ARCHIVE + 1)
    _require(len(data) <= MAX_ARCHIVE and hashlib.sha256(data).hexdigest() == tool["sha256"], "Vendor archive checksum mismatch")
    return data


def extract(data, tool, root):
    seen, size = set(), 0
    prefix = tool["stripPrefix"]

    def destination(name, content, executable=False):
        nonlocal size
        _require(_path(name), "Unsafe vendor archive path")
        if prefix:
            _require(name.startswith(prefix + "/"), "Unexpected vendor archive prefix")
            name = name[len(prefix) + 1:]
        _require(_path(name) and name not in seen, "Duplicate or unsafe vendor archive path")
        seen.add(name)
        size += len(content)
        _require(len(seen) <= MAX_FILES and size <= MAX_EXPANDED, "Vendor archive expansion limit exceeded")
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        # The whole archive is independently pinned approved code. Preserve
        # executable companions, stripping all setuid/setgid/write permission.
        target.chmod(0o755 if executable else 0o644)

    if tool["format"] == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                _require(_path(member.filename.rstrip("/")), "Unsafe vendor archive path")
                mode = member.external_attr >> 16
                _require(mode & 0o170000 in {0, 0o100000, 0o040000}, "Vendor links and special files are forbidden")
                if member.is_dir():
                    continue
                _require(member.file_size <= MAX_EXPANDED - size, "Vendor archive expansion limit exceeded")
                destination(member.filename, archive.read(member), executable=bool(mode & 0o111))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz" if tool["format"] == "tar.gz" else "r:xz") as archive:
            for member in archive:
                _require(_path(member.name.rstrip("/")) and not member.pax_headers, "Unsafe vendor archive path")
                if member.isdir():
                    continue
                _require(member.isfile() and not member.linkname and member.size <= MAX_EXPANDED - size,
                         "Vendor links, special files and oversized entries are forbidden")
                with archive.extractfile(member) as stream:
                    destination(member.name, stream.read(member.size + 1), executable=bool(member.mode & 0o111))
    binary = root / tool["entrypoint"]
    _require(binary.is_file() and hashlib.sha256(binary.read_bytes()).hexdigest() == tool["entrypointSha256"], "Vendor binary checksum mismatch")
    # Entrypoint execution is explicit even for reviewed regular-file repacks.
    binary.chmod(0o755)
    return binary


def install(value, destination, *, fetch=download):
    value = validate_contract(value)
    destination = Path(destination)
    _require(not destination.exists() and not destination.is_symlink(), "Vendor destination must be new")
    scratch = Path(tempfile.mkdtemp(prefix=".sfdc-vendor-", dir=destination.parent))
    receipts = {}
    try:
        for tool in value["tools"]:
            data = fetch(tool, value["approvedDownloadHosts"])
            _require(len(data) <= MAX_ARCHIVE and hashlib.sha256(data).hexdigest() == tool["sha256"], "Vendor archive checksum mismatch")
            root = scratch / tool["name"]
            root.mkdir(mode=0o755)
            binary = extract(data, tool, root)
            with tempfile.TemporaryDirectory(prefix="sfdc-tool-version-") as isolated_home:
                result = subprocess.run([str(binary), "--version"], env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": isolated_home},
                                        capture_output=True, text=True, timeout=15, check=True)
            _require(len(result.stdout) + len(result.stderr) <= 65536
                     and tool["version"] in result.stdout + result.stderr, "Vendor tool version mismatch")
            receipts[tool["name"]] = {"path": str(destination / tool["name"] / tool["entrypoint"]),
                        "version": tool["version"], "sha256": tool["entrypointSha256"], "archiveSha256": tool["sha256"]}
        (scratch / "vendor-tools.json").write_text(json.dumps({"schemaVersion": 1, "tools": receipts}, indent=2) + "\n")
        for directory, _, _ in os.walk(scratch):
            Path(directory).chmod(0o755)
        scratch.rename(destination)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    receipts = install(json.loads(Path(args.contract).read_text()), args.destination)
    print(json.dumps({"installedTools": receipts, "providerAuthentication": "NOT_CONTACTED"}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile):
        raise SystemExit("Vendor tool installation failed: verify approved URL, fixed version, hashes, archive layout and Linux dependencies.") from None
