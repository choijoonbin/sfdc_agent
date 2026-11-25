"""Declared transport capabilities and bounded reproducibility receipts."""

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}$")
_PROVIDERS = frozenset({"kiro", "openai", "azure"})


def model_identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def provider_capabilities(provider: str) -> dict:
    """Describe implemented transports, never infer a model's image support.

    Image submission requires a separately implemented and tested adapter. A
    provider having vision-capable models does not enable images on this server.
    """
    if provider not in _PROVIDERS:
        raise ValueError("Unknown model provider.")
    return {"structuredText": True, "images": False, "jsonObject": True,
            "strictSchema": provider == "openai",
            "imageInputStatus": "unsupported" if provider == "kiro" else "not-enabled"}


def provider_receipt(provider: str, configured_model=None, reported_model=None, *, cli=None) -> dict:
    receipt = {"schemaVersion": 1, "provider": provider,
               "configuredModel": model_identifier(configured_model),
               "reportedModel": model_identifier(reported_model),
               "modelSelection": "provider-managed" if provider == "kiro" else "configured",
               "capabilities": provider_capabilities(provider)}
    if isinstance(cli, dict):
        receipt["cli"] = cli
    return receipt


@lru_cache(maxsize=8)
def _executable_digest(path, size, modified):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cli_identity(binary: str) -> dict:
    """Identify the launched bytes without an extra CLI process or local paths."""
    path = Path(binary)
    identity = {"name": path.name, "version": None, "versionStatus": "unreported"}
    try:
        info = path.stat()
        identity["executableSha256"] = _executable_digest(str(path), info.st_size, info.st_mtime_ns)
    except OSError:
        identity["identityStatus"] = "unavailable"
    return identity


def reported_stream_model(body: bytes):
    """Retain only an explicitly reported model ID, never model output text."""
    for line in reversed(body.splitlines()):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict) and event.get("type") == "runFinished":
            return model_identifier(data.get("modelId") or data.get("model"))
    return None
