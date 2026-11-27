"""Verify the selected design-system artifact before any conversion provider runs.

The service request pins an immutable release, never a developer source path.
Archived releases without the core marker keep their historical behavior.
"""

from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from .strict_json import decode_json

CORE_PATH = "nextbss/design-system/core-source.json"
CORE_INPUTS_PATH = "nextbss/design-system/core-inputs.json"
LWC_PREFIX = "nextbss/design-system/lwc/"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_RELEASE = re.compile(r"^nextbss-[0-9]+(?:\.[0-9]+){1,2}-[a-f0-9]{64}$")
_FILE = re.compile(r"^[a-z][A-Za-z0-9_]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class CoreCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    engineContract: Literal["nextbss-lwc-v1"]


class ConverterUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Annotated[str, Field(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")]
    runtimeTreeSha256: Digest


class SalesforceUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Annotated[str, Field(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")]
    payloadTreeSha256: Digest
    compatibilityContract: Literal["NOVA_PUBLIC@1.0.0"]
    publicContractSha256: Digest


class ReleaseUnits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    converter: ConverterUnit
    salesforce: SalesforceUnit


class DesignCore(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1, 2]
    contract: Literal["sfdc-design-system-core-v1", "sfdc-design-system-core-v2"]
    coreVersion: Annotated[str, Field(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")]
    sourceRepository: Literal["https://github.com/choijoonbin/sfdc_ui.git"]
    sourcePath: Literal["packages/design-system-core"]
    commit: Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
    inputTreeSha256: Digest
    sourceTreeSha256: Digest
    artifactTreeSha256: Digest
    compatibility: CoreCompatibility
    units: ReleaseUnits | None = None

    @model_validator(mode="before")
    @classmethod
    def exact_version_shape(cls, value):
        if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int or value["schemaVersion"] not in {1, 2}:
            raise ValueError("The core schema version must be integer 1 or 2.")
        version = value["schemaVersion"]
        if value.get("contract") != f"sfdc-design-system-core-v{version}" or ("units" in value) != (version == 2):
            raise ValueError("The core version and strict unit shape differ.")
        return value

    @model_validator(mode="after")
    def relative_source(self):
        if any(part in {".", ".."} for part in self.sourcePath.split("/")):
            raise ValueError("The core source path must be relative.")
        if self.schemaVersion == 2 and (self.units is None or self.units.salesforce.payloadTreeSha256 != self.artifactTreeSha256):
            raise ValueError("The Salesforce unit differs from the final payload.")
        return self

    @model_serializer(mode="wrap")
    def exact_version_dump(self, handler):
        result = handler(self)
        if self.schemaVersion == 1:
            result.pop("units", None)
        return result


def _reject(code, status=409):
    raise HTTPException(status, code)


def _read(path: Path, maximum: int) -> bytes:
    if path.resolve(strict=True) != path:
        raise ValueError("Noncanonical artifact path")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError("Invalid artifact file")
        value = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if len(value) != before.st_size or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError("Artifact changed while reading")
        return value


def verify_selected_core(*, release_id: str, manifest_sha: str | None, release_root: str | Path | None,
                         runtime_root: Path, expected: DesignCore | None = None) -> dict | None:
    """Return trusted provenance from the actual selected archive, or None for legacy.

    New core releases require an exact manifest pin and an explicitly installed
    runtime. They cannot silently use a sibling UI checkout as their runtime.
    """
    if release_root is None:
        if expected is not None:
            _reject("DESIGN_CORE_RELEASE_UNAVAILABLE", 503)
        return None
    try:
        if not _RELEASE.fullmatch(release_id):
            raise ValueError("Invalid release ID")
        root = Path(release_root).resolve(strict=True)
        directory = root / release_id
        manifest_path = directory / "release.json"
        if not manifest_path.exists() and expected is None and manifest_sha is None:
            return None
        raw = _read(manifest_path, 4 * 1024 * 1024)
        manifest = decode_json(raw)
        if not isinstance(manifest, dict) or manifest.get("releaseId") != release_id:
            raise ValueError("Invalid selected release")
        if manifest_sha is not None and sha256(raw).hexdigest() != manifest_sha:
            _reject("DESIGN_CORE_MANIFEST_MISMATCH")
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) > 5000:
            raise ValueError("Invalid manifest inventory")
        markers = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") == CORE_PATH]
        if not markers:
            if expected is not None or (directory / CORE_PATH).exists():
                _reject("DESIGN_CORE_PIN_MISMATCH")
            return None
        if len(markers) != 1 or manifest_sha is None:
            _reject("DESIGN_CORE_MANIFEST_PIN_REQUIRED")
        if not os.environ.get("SFDC_MANAGED_ENGINE_ROOT"):
            _reject("DESIGN_CORE_RUNTIME_UNAVAILABLE", 503)
        marker_bytes = _read(directory / CORE_PATH, 64 * 1024)
        marker_entry = markers[0]
        if marker_entry.get("bytes") != len(marker_bytes) or marker_entry.get("sha256") != sha256(marker_bytes).hexdigest():
            raise ValueError("Core marker differs from manifest")
        core = DesignCore.model_validate(decode_json(marker_bytes))
        pin = core.model_dump(mode="json")
        if expected is not None and pin != expected.model_dump(mode="json"):
            _reject("DESIGN_CORE_PIN_MISMATCH")
        if manifest.get("source") != {"commit": core.commit, "treeSha256": core.artifactTreeSha256}:
            raise ValueError("Core source provenance differs")
        inputs_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") == CORE_INPUTS_PATH]
        if len(inputs_entries) != 1:
            raise ValueError("Core input inventory is required")
        input_raw = _read(directory / CORE_INPUTS_PATH, 4 * 1024 * 1024)
        if inputs_entries[0].get("bytes") != len(input_raw) or inputs_entries[0].get("sha256") != sha256(input_raw).hexdigest():
            raise ValueError("Core input inventory differs from the release")
        inputs = decode_json(input_raw)
        if not isinstance(inputs, dict) or set(inputs) != {"schemaVersion", "files"} or type(inputs.get("schemaVersion")) is not int or inputs["schemaVersion"] != 1:
            raise ValueError("Core input inventory is invalid")
        source_files = inputs["files"]
        if not isinstance(source_files, list) or not 1 <= len(source_files) <= 4096:
            raise ValueError("Core input inventory is invalid")
        input_lines, previous = [], None
        for item in source_files:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                raise ValueError("Core input inventory entry is invalid")
            path = item["path"]
            if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.@/-]+", path) or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
                raise ValueError("Core input path is invalid")
            if previous is not None and previous >= path or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= 32 * 1024 * 1024 or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"] or ""):
                raise ValueError("Core input identity is invalid")
            previous = path
            input_lines.append(f"{path}\0{item['sha256']}\n")
        if sha256("".join(input_lines).encode()).hexdigest() != core.inputTreeSha256:
            raise ValueError("Core committed input identity differs")
        capabilities = decode_json(_read(runtime_root.resolve(strict=True) / "nextbss/engine/managed-runtime-capabilities.json", 8192))
        if not isinstance(capabilities, dict) or core.compatibility.engineContract not in capabilities.get("designSystemCoreContracts", []):
            _reject("RELEASE_RUNTIME_INCOMPATIBLE")
        files = []
        known = set()
        indexed = {}
        total = 0
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("Invalid manifest entry")
            path = entry["path"]
            if path.casefold() in known:
                raise ValueError("Duplicate manifest path")
            known.add(path.casefold())
            indexed[path] = entry
            if not path.startswith(LWC_PREFIX):
                continue
            relative = path[len(LWC_PREFIX):]
            if not _FILE.fullmatch(relative):
                raise ValueError("Invalid LWC artifact path")
            value = _read(directory / path, 32 * 1024 * 1024)
            digest = sha256(value).hexdigest()
            total += len(value)
            if total > 128 * 1024 * 1024 or type(entry.get("bytes")) is not int or entry["bytes"] != len(value) or entry.get("sha256") != digest:
                raise ValueError("LWC artifact differs from manifest")
            files.append((relative, digest))
        if not files:
            raise ValueError("Core LWC inventory is empty")
        source_prefix = "packages/design-system-core/salesforce/main/default/lwc/"
        authored = [(item["path"][len(source_prefix):], item["sha256"]) for item in source_files if item["path"].startswith(source_prefix)]
        if ([path for path, _ in authored] != [path for path, _ in sorted(files)]
                or sha256("".join(f"{path}\0{digest}\n" for path, digest in authored).encode()).hexdigest() != core.sourceTreeSha256):
            raise ValueError("Core raw authored LWC identity differs from committed inputs")
        final_hashes = dict(files)
        for path, digest in authored:
            if path != "novaTokens/novaTokens.css" and final_hashes[path] != digest:
                raise ValueError("Deployable implementation differs from reviewed authored input")
        generated = {"nova-public-contract.json", "nova-module-manifest.json", "nova-source.json",
                     "core-source.json", "core-inputs.json", "design-release-validation.json"}
        def archived_input(path):
            return (path in {"package.json", "package-lock.json"} or path.startswith("nextbss/")) and not (
                path.startswith("nextbss/design-system/") and path[len("nextbss/design-system/"):] in generated)
        input_by_path = {item["path"]: item for item in source_files}
        for item in source_files:
            path = item["path"]
            if archived_input(path):
                source = _read(directory / path, 32 * 1024 * 1024)
                declared = indexed.get(path)
                if (len(source) != item["bytes"] or sha256(source).hexdigest() != item["sha256"] or declared is None
                        or declared.get("bytes") != len(source) or declared.get("sha256") != item["sha256"]):
                    raise ValueError("Archived runtime, package or standard differs from reviewed authored input")
        if any(archived_input(path) and not path.startswith(LWC_PREFIX) and path not in input_by_path for path in indexed):
            raise ValueError("Archived runtime, package or standard is absent from committed inputs")
        tree = sha256("".join(f"{path}\0{digest}\n" for path, digest in sorted(files)).encode()).hexdigest()
        if tree != core.artifactTreeSha256 or _read(manifest_path, 4 * 1024 * 1024) != raw:
            raise ValueError("Core artifact identity differs")
        if core.schemaVersion == 2:
            runtime_files = [(path, entry["sha256"]) for path, entry in indexed.items() if path in {"package.json", "package-lock.json"} or (
                not re.search(r"(?:^|/)(?:tests?|__tests__|fixtures|__fixtures__)(?:/|$)", path)
                and re.fullmatch(r"nextbss/(?:engine|server|shared)/(?:[\w-]+/)*[\w-]+\.(?:mjs|json)", path))]
            runtime_tree = sha256("".join(f"{path}\0{digest}\n" for path, digest in sorted(runtime_files)).encode()).hexdigest()
            public_contract = _read(directory / "nextbss/design-system/nova-public-contract.json", 4 * 1024 * 1024)
            if runtime_tree != core.units.converter.runtimeTreeSha256 or sha256(public_contract).hexdigest() != core.units.salesforce.publicContractSha256:
                raise ValueError("The independent runtime/public contract unit differs")
            # The selected converter identity must describe the code actually executed,
            # not merely an unused archived copy. Keep v1's historical behavior intact.
            execution_root = runtime_root.resolve(strict=True)
            execution_files = []
            for parent in ("nextbss/engine", "nextbss/server", "nextbss/shared"):
                folder = execution_root / parent
                if folder.exists():
                    for candidate in folder.rglob("*"):
                        path = candidate.relative_to(execution_root).as_posix()
                        if (not re.search(r"(?:^|/)(?:tests?|__tests__|fixtures|__fixtures__)(?:/|$)", path)
                                and re.fullmatch(r"nextbss/(?:engine|server|shared)/(?:[\w-]+/)*[\w-]+\.(?:mjs|json)", path)):
                            execution_files.append((path, sha256(_read(candidate, 32 * 1024 * 1024)).hexdigest()))
            for path in ("package.json", "package-lock.json"):
                if (execution_root / path).exists():
                    execution_files.append((path, sha256(_read(execution_root / path, 32 * 1024 * 1024)).hexdigest()))
            if sorted(execution_files) != sorted(runtime_files):
                _reject("DESIGN_CORE_RUNTIME_MISMATCH")
        return pin
    except HTTPException:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        _reject("DESIGN_CORE_ARTIFACT_INVALID")


def preserve_core_summary(result, core):
    if core is None:
        if "designCore" in (result.summary or {}):
            _reject("DESIGN_CORE_RESULT_MISMATCH", 502)
        return result
    summary = dict(result.summary or {})
    if "designCore" in summary and summary["designCore"] != core:
        _reject("DESIGN_CORE_RESULT_MISMATCH", 502)
    summary["designCore"] = core
    return result.model_copy(update={"summary": summary})
