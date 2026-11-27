"""Bounded JSON authoring proposals passed from the pinned Node engine to BACK."""

import base64
import binascii
import gzip
from hashlib import sha256
from io import BytesIO
import re

from .design_core import DesignCore
from .strict_json import decode_json, encode_json

MAX_CHANGE_REQUEST_BYTES = 2 * 1024 * 1024
MAX_BUILD_RESPONSE_BYTES = MAX_CHANGE_REQUEST_BYTES + 128 * 1024
_PREFIX = "nextbss/design-system/"
_PATHS = {"tokens.json", "components.json", "library-bindings.json", "canonical-library.json", "guide-inventory.json",
          "button-library.json", "component-catalog.json", "catalog-style-evidence.json", "catalog-release.json",
          "guide-details.json", "nova-library.json", "release-source-guide.json", "release-source-library.json"}


def validate_core_change_request(value):
    """Return the original proposal without decoding executable files or paths."""
    def require(condition):
        if not condition:
            raise ValueError("DESIGN_CORE_CHANGE_REQUEST_INVALID")

    require(isinstance(value, dict) and set(value) == {"schemaVersion", "contract", "parentReleaseId", "parentCore",
                                                      "targetDesignVersion", "sources", "changes"})
    require(len(encode_json(value)) <= MAX_CHANGE_REQUEST_BYTES and type(value["schemaVersion"]) is int
            and value["schemaVersion"] == 1 and value["contract"] == "sfdc-design-system-change-request-v1")
    require(isinstance(value["parentReleaseId"], str) and re.fullmatch(r"nextbss-\d{1,4}(?:\.\d{1,4}){1,2}-[a-f0-9]{64}", value["parentReleaseId"]))
    require(isinstance(value["targetDesignVersion"], str) and re.fullmatch(r"\d{1,4}(?:\.\d{1,4}){1,2}", value["targetDesignVersion"]))
    if value["parentCore"] is not None:
        DesignCore.model_validate(value["parentCore"])
    sources = value["sources"]
    require(isinstance(sources, dict))
    for name in ("guide", "library"):
        source = sources.get(name)
        require(isinstance(source, dict) and isinstance(source.get("fileKey"), str)
                and re.fullmatch(r"[A-Za-z0-9]{1,160}", source["fileKey"])
                and isinstance(source.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", source["sha256"]))
        require(source.get("revision") is None or isinstance(source["revision"], str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", source["revision"]))
    require(isinstance(value["changes"], list) and 1 <= len(value["changes"]) <= len(_PATHS))
    decoded, total = {}, 0
    for file in value["changes"]:
        require(isinstance(file, dict) and set(file) == {"path", "contentEncoding", "content", "bytes", "sha256"})
        path, content = file["path"], file["content"]
        require(isinstance(path, str) and path.startswith(_PREFIX) and path[len(_PREFIX):] in _PATHS and path not in decoded
                and file["contentEncoding"] == "gzip-base64" and isinstance(content, str) and len(content) <= MAX_CHANGE_REQUEST_BYTES
                and type(file["bytes"]) is int and 0 <= file["bytes"] <= 16 * 1024 * 1024
                and isinstance(file["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", file["sha256"]))
        try:
            compressed = base64.b64decode(content, validate=True)
            require(base64.b64encode(compressed).decode() == content)
            with gzip.GzipFile(fileobj=BytesIO(compressed)) as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            total += len(raw)
            require(len(raw) <= 16 * 1024 * 1024 and total <= 24 * 1024 * 1024 and len(raw) == file["bytes"]
                    and sha256(raw).hexdigest() == file["sha256"])
            require(isinstance(decode_json(raw), dict))
        except (OSError, EOFError, binascii.Error, UnicodeError) as error:
            raise ValueError("DESIGN_CORE_CHANGE_REQUEST_INVALID") from error
        decoded[path] = raw
    proposed = lambda name: decode_json(decoded[_PREFIX + name]) if _PREFIX + name in decoded else None
    catalog = proposed("catalog-release.json")
    if catalog is not None:
        require(catalog.get("designVersion") == value["targetDesignVersion"])
        for name in ("guide", "library"):
            require(isinstance(catalog.get(name), dict) and catalog[name].get("fileKey") == sources[name]["fileKey"]
                    and catalog[name].get("revision") == sources[name].get("revision"))
    canonical = proposed("canonical-library.json")
    if canonical is not None:
        export = canonical.get("figExport", {})
        require(isinstance(export, dict) and isinstance(export.get("guideEvidence"), dict)
                and canonical.get("fileKey") == sources["library"]["fileKey"]
                and canonical.get("version") == sources["library"].get("revision")
                and export.get("sha256") == sources["library"]["sha256"]
                and export["guideEvidence"].get("fileKey") == sources["guide"]["fileKey"]
                and export["guideEvidence"].get("sha256") == sources["guide"]["sha256"])
    for name in ("guide", "library"):
        snapshot = decoded.get(_PREFIX + "release-source-" + name + ".json")
        require(snapshot is None or sha256(snapshot).hexdigest() == sources[name]["sha256"])
    return value
