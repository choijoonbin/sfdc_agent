"""Azure OpenAI Chat Completions through the AI Hub gateway."""

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from http.client import HTTPException
from threading import Lock
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener

from .config import project_root
from .providers import (
    ProviderError, _evidence_text, _max_response_bytes, _NoRedirect, _parse_object, _prompt,
)


logger = logging.getLogger(__name__)
_PREFIX = "SFDC_AGENT_AZURE_"
_REQUIRED = (
    "BASE_URL", "API_VERSION", "TOKEN_SCOPE", "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
    "SUBSCRIPTION_KEY", "CHAT_MODEL",
)
_OPTIONAL = {
    "TOKEN_TIMEOUT_SECONDS": "30", "REQUEST_TIMEOUT_SECONDS": "120",
    "MAX_COMPLETION_TOKENS": "16384", "REASONING_EFFORT": "",
}
_SETTINGS = (*_REQUIRED, *_OPTIONAL)
_ENV_NAMES = {_PREFIX + name for name in _SETTINGS}
_DEPLOYMENT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
_TENANT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}")
_EFFORT = {"", "none", "minimal", "low", "medium", "high", "xhigh"}
_FINISH = {"stop", "length", "content_filter", "tool_calls", "function_call"}


def _read_local_settings() -> dict[str, str]:
    path = project_root() / ".env.azure.local"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise ProviderError("AZURE_CONFIG_INVALID", "Could not read .env.azure.local.") from None
    values = {}
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name not in _ENV_NAMES:
            continue
        try:
            if not sep or name in values:
                raise ValueError
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'"):
                if len(value) < 2 or not value.endswith("'"):
                    raise ValueError
                value = value[1:-1]
            if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
                raise ValueError
        except ValueError:
            raise ProviderError("AZURE_CONFIG_INVALID", f"Invalid Azure configuration at line {number}.") from None
        values[name] = value
    return values


@dataclass(frozen=True)
class AzureSettings:
    base_url: str = field(repr=False)
    api_version: str
    token_scope: str = field(repr=False)
    tenant_id: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    subscription_key: str = field(repr=False)
    chat_model: str
    token_timeout_seconds: float
    request_timeout_seconds: float
    max_completion_tokens: int
    reasoning_effort: str

    @classmethod
    def from_environment(cls):
        local = _read_local_settings()
        values = {name: os.environ.get(_PREFIX + name, local.get(_PREFIX + name, _OPTIONAL.get(name, "")))
                  for name in _SETTINGS}
        missing = [_PREFIX + name for name in _REQUIRED
                   if values[name].strip() in {"", "CHANGE_ME", "FROM_PARAM_STORE", "REPLACE_ME"}]
        if missing:
            raise ProviderError("AZURE_CONFIG_MISSING", "Missing Azure configuration: " + ", ".join(missing))
        try:
            return cls._validate(values)
        except (ValueError, UnicodeError, OverflowError):
            raise ProviderError("AZURE_CONFIG_INVALID", "Invalid Azure endpoint, identity, deployment or limits.") from None

    @classmethod
    def _validate(cls, values):
        base = values["BASE_URL"].rstrip("/")
        url = urlsplit(base)
        version = values["API_VERSION"]
        tenant = values["TENANT_ID"]
        model = values["CHAT_MODEL"]
        token_timeout = float(values["TOKEN_TIMEOUT_SECONDS"])
        request_timeout = float(values["REQUEST_TIMEOUT_SECONDS"])
        token_limit = int(values["MAX_COMPLETION_TOKENS"])
        effort = values["REASONING_EFFORT"]
        if (
            any(char.isspace() or char in "\\?#" for char in base)
            or url.scheme != "https" or not url.hostname
            or url.username is not None or url.password is not None
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-preview)?", version)
            or not _TENANT.fullmatch(tenant) or not _DEPLOYMENT.fullmatch(model) or effort not in _EFFORT
            or not 1 <= token_timeout <= 600 or not 1 <= request_timeout <= 600
            or not 1 <= token_limit <= 128_000
        ):
            raise ValueError
        # Validate the URL port and subscription header before attempting authentication.
        _ = url.port
        if any(not 33 <= ord(char) <= 126 for char in values["SUBSCRIPTION_KEY"]):
            raise ValueError
        return cls(base, version, values["TOKEN_SCOPE"], tenant, values["CLIENT_ID"], values["CLIENT_SECRET"],
                   values["SUBSCRIPTION_KEY"], model, token_timeout, request_timeout, token_limit, effort)


def _post(request: Request, timeout: float, stage: str) -> dict:
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return _parse_object(response.read(_max_response_bytes + 1).decode("utf-8"))
    except HTTPError as error:
        status = error.code
        error.close()
        raise ProviderError(f"AZURE_{stage}_HTTP_{status}", f"Azure {stage.lower()} request failed (HTTP {status}).") from None
    except TimeoutError:
        raise ProviderError(f"AZURE_{stage}_TIMEOUT", f"Azure {stage.lower()} request timed out.") from None
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ProviderError(f"AZURE_{stage}_TIMEOUT", f"Azure {stage.lower()} request timed out.") from None
        raise ProviderError(f"AZURE_{stage}_UNAVAILABLE", f"Azure {stage.lower()} request could not connect.") from None
    except (OSError, HTTPException):
        raise ProviderError(f"AZURE_{stage}_UNAVAILABLE", f"Azure {stage.lower()} request could not connect.") from None
    except UnicodeError:
        raise ProviderError("MODEL_INVALID_RESPONSE", "The Azure response was not valid UTF-8.") from None


class _EntraToken:
    def __init__(self, settings: AzureSettings):
        self.settings = settings
        self._lock = Lock()
        self._token = ""
        self._refresh_at = 0.0

    def get(self) -> str:
        with self._lock:
            if monotonic() < self._refresh_at:
                return self._token
            settings = self.settings
            request = Request(
                f"https://login.microsoftonline.com/{settings.tenant_id}/oauth2/v2.0/token",
                data=urlencode({"client_id": settings.client_id, "client_secret": settings.client_secret,
                                "scope": settings.token_scope, "grant_type": "client_credentials"}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
            )
            started = monotonic()
            response = _post(request, settings.token_timeout_seconds, "TOKEN")
            token = response.get("access_token")
            try:
                lifetime = float(response.get("expires_in"))
            except (ValueError, TypeError, OverflowError):
                lifetime = 0.0
            if (
                not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token)
                or not math.isfinite(lifetime) or lifetime <= monotonic() - started
            ):
                raise ProviderError("AZURE_TOKEN_INVALID", "Azure authentication returned no usable access token.")
            self._token = token
            self._refresh_at = started + lifetime - min(120, lifetime / 2)
            return token


def _usage_value(data, key):
    value = data.get(key) if isinstance(data, dict) else None
    return value if type(value) is int and value >= 0 else None


class AzureOpenAIProvider:
    """Optional Azure provider for the existing Screen and Story pilot contract."""

    name = "azure"

    def __init__(self, settings: AzureSettings, timeout: float, model: str | None = None):
        self.model = settings.chat_model if model is None else model
        if not _DEPLOYMENT.fullmatch(self.model):
            raise ProviderError("AZURE_MODEL_INVALID", "Expected an Azure deployment name.")
        self.settings = settings
        self.timeout = min(timeout, settings.request_timeout_seconds)
        self._token = _EntraToken(settings)

    def complete(self, task: str, evidence: dict | str, schema: dict) -> dict:
        settings = self.settings
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _prompt(task, schema)},
                {"role": "user", "content": _evidence_text(evidence)},
            ],
            "max_completion_tokens": settings.max_completion_tokens,
        }
        if settings.reasoning_effort:
            body["reasoning_effort"] = settings.reasoning_effort
        version = urlencode({"api-version": settings.api_version})
        path = f"/openai/deployments/{quote(self.model, safe='')}/chat/completions?{version}"
        request = Request(settings.base_url + path, data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
                          headers={"Authorization": f"Bearer {self._token.get()}", "api-key": settings.subscription_key,
                                   "Content-Type": "application/json"}, method="POST")
        started = monotonic()
        try:
            response = _post(request, self.timeout, "CHAT")
            choices = response.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            reason = choice.get("finish_reason")
            reason = reason if isinstance(reason, str) and reason in _FINISH else "unknown"
            usage = response.get("usage")
            details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
            logger.info("azure_chat elapsed_ms=%d finish=%s input_tokens=%s output_tokens=%s total_tokens=%s cached_tokens=%s",
                        round((monotonic() - started) * 1000), reason, _usage_value(usage, "prompt_tokens"),
                        _usage_value(usage, "completion_tokens"), _usage_value(usage, "total_tokens"),
                        _usage_value(details, "cached_tokens"))
            message = choice.get("message")
            if reason == "content_filter" or (isinstance(message, dict) and message.get("refusal")):
                raise ProviderError("AZURE_CONTENT_FILTERED", "Azure did not return a usable completion.")
            if reason != "stop" or not isinstance(message, dict):
                raise ProviderError("AZURE_INCOMPLETE", "Azure did not complete the response.")
            return _parse_object(message.get("content"))
        except ProviderError as error:
            logger.warning("azure_chat_failed elapsed_ms=%d code=%s", round((monotonic() - started) * 1000), error.code)
            raise
