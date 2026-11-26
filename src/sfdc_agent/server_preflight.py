"""Fail-closed configuration checks for the managed server distribution.

These checks do not call AWS, Figma or Kiro. A successful check proves local
packaging/configuration only; provider authentication is a separate acceptance.
"""

from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import UUID

NODE_VERSION = "26.13.0"
DIGEST = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
MAX_MANIFEST = 1024 * 1024
SECRET_NAMES = {"executionToken": "SFDC_AGENT_EXECUTION_TOKEN", "figmaRestToken": "FIGMA_ACCESS_TOKEN",
                "figmaPlanToken": "FIGMA_PLAN_ACCESS_TOKEN", "kiroApiKey": "KIRO_API_KEY"}


class PreflightError(ValueError):
    """Only constant error codes may leave the startup boundary."""


def _require(condition, code):
    if not condition:
        raise PreflightError(code)


def _keys(value, required, optional=()):
    _require(isinstance(value, dict) and set(required).issubset(value)
             and set(value).issubset(set(required) | set(optional)), "SERVER_CONTRACT_FIELDS_INVALID")


def _digest(value):
    return isinstance(value, str) and bool(DIGEST.fullmatch(value))


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _path(value):
    return (isinstance(value, str) and value.startswith("/") and len(value) <= 512
            and not any(part in {".", ".."} for part in value.split("/"))
            and not any(value.startswith(prefix) for prefix in ("/Users/", "/home/", "/Volumes/"))
            and re.fullmatch(r"/[A-Za-z0-9_./-]+", value) is not None)


def _host(value):
    return (isinstance(value, str) and len(value) <= 253 and value == value.lower()
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value) is not None
            and ".." not in value and "*" not in value)


def _validate_contract(value):
    _keys(value, {"kind", "schemaVersion", "environment", "network", "storage", "paths", "worker",
                  "capabilities", "artifact", "secretReferences", "figmaMcp", "tools"})
    _require(value["kind"] == "sfdc-agent-server-contract" and type(value["schemaVersion"]) is int
             and value["schemaVersion"] == 1 and value["environment"] in {"dev", "prd"}, "SERVER_CONTRACT_VERSION_INVALID")
    network, storage, paths = value["network"], value["storage"], value["paths"]
    _keys(network, {"bindHost", "port", "selfOrigin", "trustedHosts"})
    _require(network["bindHost"] == "0.0.0.0" and type(network["port"]) is int
             and 1024 <= network["port"] <= 65535, "SERVER_LISTENER_INVALID")
    _require(network["selfOrigin"] == f'http://127.0.0.1:{network["port"]}', "SERVER_SELF_ORIGIN_INVALID")
    hosts = network["trustedHosts"]
    _require(isinstance(hosts, list) and 1 <= len(hosts) <= 32 and len(set(hosts)) == len(hosts)
             and all(_host(host) for host in hosts) and "127.0.0.1" in hosts, "SERVER_TRUSTED_HOSTS_INVALID")
    _keys(storage, {"provider", "region", "bucket", "prefix"})
    _require(storage["provider"] == "s3" and isinstance(storage["region"], str)
             and re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", storage["region"]) is not None,
             "SERVER_S3_REGION_INVALID")
    _require(isinstance(storage["bucket"], str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", storage["bucket"])
             and ".." not in storage["bucket"], "SERVER_S3_BUCKET_INVALID")
    _require(isinstance(storage["prefix"], str) and 1 <= len(storage["prefix"]) <= 256
             and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", storage["prefix"]) is not None
             and all(part not in {".", ".."} for part in storage["prefix"].split("/")), "SERVER_S3_PREFIX_INVALID")
    _keys(paths, {"engineRoot", "stateRoot", "scratchRoot"})
    _require(all(_path(path) for path in paths.values()) and len(set(paths.values())) == 3,
             "SERVER_PATH_INVALID")
    _require(all(not a.startswith(b.rstrip("/") + "/") for key, a in paths.items()
                 for other, b in paths.items() if key != other), "SERVER_PATHS_OVERLAP")
    worker = value["worker"]
    _keys(worker, {"uvicornWorkers", "routing", "scope"})
    _require(type(worker["uvicornWorkers"]) is int and worker["uvicornWorkers"] == 1
             and worker["routing"] == "scope-dedicated", "SERVER_WORKER_AFFINITY_REQUIRED")
    _keys(worker["scope"], {"tenantId", "workspaceId"})
    _require(all(_uuid(item) for item in worker["scope"].values()), "SERVER_SCOPE_INVALID")
    capabilities = value["capabilities"]
    _keys(capabilities, {"figmaRest", "kiroHeadless", "localArchive", "figmaMcp", "visualCapture"})
    _require(all(type(item) is bool for item in capabilities.values()) and capabilities["localArchive"],
             "SERVER_CAPABILITIES_INVALID")
    _require(capabilities["figmaMcp"] is False, "FIGMA_MCP_SERVER_NOT_IMPLEMENTED")
    _keys(value["figmaMcp"], {"status", "promotionContract"})
    _require(value["figmaMcp"] == {"status": "NOT_READY", "promotionContract": "figma-mcp-server-promotion/v1"},
             "FIGMA_MCP_SERVER_NOT_IMPLEMENTED")
    artifact = value["artifact"]
    _keys(artifact, {"uiCommit", "archiveSha256", "runtimeManifestSha256", "runtimeSha256"})
    _require(isinstance(artifact["uiCommit"], str) and COMMIT.fullmatch(artifact["uiCommit"])
             and all(_digest(artifact[key]) for key in artifact if key != "uiCommit"), "SERVER_ARTIFACT_PIN_INVALID")
    refs = value["secretReferences"]
    required = {"executionToken"} | ({"figmaRestToken"} if capabilities["figmaRest"] else set()) | ({"kiroApiKey"} if capabilities["kiroHeadless"] else set())
    _keys(refs, required, set(SECRET_NAMES) - required)
    _require(all(isinstance(ref, str) and re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", ref)
                 and all(part not in {".", ".."} for part in ref.split("/")) for ref in refs.values()),
             "SERVER_SECRET_REFERENCE_INVALID")
    tools = value["tools"]
    required_tools = ({"kiro-cli"} if capabilities["kiroHeadless"] else set()) | ({"chrome"} if capabilities["visualCapture"] else set())
    _keys(tools, required_tools, {"kiro-cli", "chrome"} - required_tools)
    for name, tool in tools.items():
        _keys(tool, {"path", "version", "sha256", "archiveSha256"})
        _require(_path(tool["path"]) and tool["path"].startswith("/opt/sfdc/vendor/" + name + "/")
                 and isinstance(tool["version"], str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", tool["version"])
                 and _digest(tool["sha256"]) and _digest(tool["archiveSha256"]), "SERVER_VENDOR_TOOL_PIN_INVALID")
    return value


def validate_contract(value):
    try:
        return _validate_contract(value)
    except (TypeError, KeyError, ValueError, RecursionError) as error:
        if isinstance(error, PreflightError):
            raise
        raise PreflightError("SERVER_CONTRACT_FIELDS_INVALID") from None


def read_contract(path):
    try:
        file = Path(path)
        _require(file.stat().st_size <= 64 * 1024 and file.is_file() and not file.is_symlink(), "SERVER_CONTRACT_FILE_INVALID")
        return validate_contract(json.loads(file.read_text()))
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        if isinstance(error, PreflightError):
            raise
        raise PreflightError("SERVER_CONTRACT_FILE_INVALID") from None


def read_secret(name, environment):
    """Load one mounted/platform-injected secret; never consult a developer HOME."""
    direct, filename = environment.get(name), environment.get(name + "_FILE")
    _require(not (direct is not None and filename is not None), "SERVER_SECRET_SOURCE_AMBIGUOUS")
    if filename is not None:
        _require(_path(filename), "SERVER_SECRET_FILE_INVALID")
        try:
            _require(Path(filename).resolve(strict=True) == Path(filename), "SERVER_SECRET_FILE_INVALID")
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                         and info.st_uid in {0, os.getuid()} and not info.st_mode & 0o077,
                         "SERVER_SECRET_FILE_PERMISSIONS_INVALID")
                data = stream.read(8193)
            _require(len(data) <= 8192, "SERVER_SECRET_VALUE_INVALID")
            direct = data.decode("utf-8").removesuffix("\n")
        except (OSError, UnicodeError):
            raise PreflightError("SERVER_SECRET_FILE_INVALID") from None
    _require(isinstance(direct, str) and 16 <= len(direct) <= 8192
             and re.fullmatch(r"[!-~]+", direct) is not None, "SERVER_SECRET_VALUE_INVALID")
    if name == "SFDC_AGENT_EXECUTION_TOKEN":
        _require(len(direct) >= 32, "SERVER_EXECUTION_TOKEN_TOO_SHORT")
    if name == "KIRO_API_KEY":
        _require(re.fullmatch(r"ksk_[A-Za-z0-9_-]{1,500}", direct), "SERVER_KIRO_KEY_FORMAT_INVALID")
    return direct


def _verify_runtime(root, pins):
    try:
        file = root / "managed-runtime.json"
        _require(file.stat().st_size <= MAX_MANIFEST and file.is_file() and not file.is_symlink(), "SERVER_RUNTIME_MANIFEST_INVALID")
        data = file.read_bytes()
        manifest = json.loads(data)
        _require(hashlib.sha256(data).hexdigest() == pins["runtimeManifestSha256"], "SERVER_RUNTIME_MANIFEST_PIN_MISMATCH")
        entries = manifest["entries"]
        _require(isinstance(entries, dict) and 1 <= len(entries) <= 5000 and manifest["sha256"] == pins["runtimeSha256"],
                 "SERVER_RUNTIME_PIN_MISMATCH")
        _require(hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() == pins["runtimeSha256"],
                 "SERVER_RUNTIME_PIN_MISMATCH")
        _require({"package.json", "package-lock.json", "nextbss/server/managed-execution.mjs"}.issubset(entries), "SERVER_RUNTIME_INCOMPLETE")
        for name, digest in entries.items():
            _require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.@/-]+", name) and not name.startswith("/")
                     and all(part and not part.startswith(".") and part not in {".", "..", "node_modules"} for part in name.split("/"))
                     and (name in {"package.json", "package-lock.json"} or name.startswith("nextbss/")) and _digest(digest),
                     "SERVER_RUNTIME_INVENTORY_INVALID")
            path = root / name
            _require(path.resolve().is_relative_to(root) and path.is_file() and not path.is_symlink()
                     and path.stat().st_nlink == 1 and path.stat().st_size <= 128 * 1024 * 1024,
                     "SERVER_RUNTIME_FILE_INVALID")
            with path.open("rb") as stream:
                _require(hashlib.file_digest(stream, "sha256").hexdigest() == digest, "SERVER_RUNTIME_CONTENT_MISMATCH")
        # npm adds node_modules only. No undeclared source or host checkout may augment the release.
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            for item in dirs + files:
                _require(not (base / item).is_symlink(), "SERVER_RUNTIME_LINK_INVALID")
            if base == root:
                dirs[:] = [item for item in dirs if item != "node_modules"]
            for item in files:
                name = str((base / item).relative_to(root))
                _require(name == "managed-runtime.json" or name in entries, "SERVER_RUNTIME_UNDECLARED_SOURCE")
        receipt = json.loads((root.parent / "runtime-provenance.json").read_text())
        _require(all(receipt.get(key) == value for key, value in pins.items()) and receipt.get("node") == NODE_VERSION,
                 "SERVER_RUNTIME_PROVENANCE_MISMATCH")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        if isinstance(error, PreflightError):
            raise
        raise PreflightError("SERVER_RUNTIME_INVALID") from None


@dataclass(frozen=True)
class Startup:
    contract: dict
    report: dict
    secret_environment: dict = field(repr=False)


def preflight(contract, environment=None, *, inspect_runtime=True):
    """Validate server-only configuration without an external network request."""
    contract = validate_contract(contract)
    env = os.environ if environment is None else environment
    _require(env.get("SFDC_AGENT_SERVER_MODE") == "1" and env.get("SFDC_AGENT_MANAGED_ONLY") == "1",
             "SERVER_MANAGED_MODE_REQUIRED")
    storage, paths, network = (contract[key] for key in ("storage", "paths", "network"))
    expected = {"SFDC_STORAGE_PROVIDER": "s3", "AWS_REGION": storage["region"], "SFDC_STORAGE_S3_BUCKET": storage["bucket"],
                "SFDC_STORAGE_S3_PREFIX": storage["prefix"], "SFDC_MANAGED_ENGINE_ROOT": paths["engineRoot"],
                "SFDC_AGENT_ROOT": paths["stateRoot"], "TMPDIR": paths["scratchRoot"],
                "SFDC_MANAGED_AGENT_ORIGIN": network["selfOrigin"], "SFDC_MANAGED_RUNTIME_SHA256": contract["artifact"]["runtimeSha256"],
                "SFDC_MANAGED_FIGMA_SCOPE": contract["worker"]["scope"]["tenantId"] + "/" + contract["worker"]["scope"]["workspaceId"]}
    _require(all(env.get(key) == value for key, value in expected.items()), "SERVER_ENVIRONMENT_CONTRACT_MISMATCH")
    _require(set(env.get("SFDC_AGENT_TRUSTED_HOSTS", "").split(",")) == set(network["trustedHosts"]), "SERVER_HOST_CONTRACT_MISMATCH")
    _require(all(not env.get(key) for key in ("SFDC_UI_ROOT", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE",
                 "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SFDC_AGENT_FIGMA_MCP_ROOT")), "SERVER_PERSONAL_CREDENTIAL_OR_CHECKOUT_FORBIDDEN")
    _require(env.get("WEB_CONCURRENCY", "1") == "1" and not env.get("UVICORN_RELOAD"), "SERVER_WORKER_AFFINITY_REQUIRED")
    secrets = {}
    for key, ref in contract["secretReferences"].items():
        name = SECRET_NAMES[key]
        _require(env.get(name + "_REF") == ref, "SERVER_SECRET_REFERENCE_MISMATCH")
        secrets[name] = read_secret(name, env)
    for key, name in SECRET_NAMES.items():
        _require(key in contract["secretReferences"] or not env.get(name) and not env.get(name + "_FILE"), "SERVER_UNDECLARED_SECRET")
    if contract["capabilities"]["kiroHeadless"]:
        binary = env.get("NOVA_KIRO_CLI", "")
        _require(binary == contract["tools"]["kiro-cli"]["path"], "SERVER_KIRO_BINARY_REQUIRED")
        _require(all(env.get("SFDC_AGENT_" + kind + "_KIRO_MODE") == "headless" for kind in ("SCREEN", "STORY")),
                 "SERVER_KIRO_HEADLESS_REQUIRED")
    if contract["capabilities"]["visualCapture"]:
        _require(env.get("SFDC_AGENT_CHROME_BIN") == contract["tools"]["chrome"]["path"], "SERVER_CHROME_BINARY_REQUIRED")
    _require(env.get("SFDC_MANAGED_EXECUTION_RUNNER", str(Path(paths["engineRoot"]) / "nextbss/server/managed-execution.mjs"))
             == str(Path(paths["engineRoot"]) / "nextbss/server/managed-execution.mjs"), "SERVER_RUNNER_OVERRIDE_FORBIDDEN")
    if inspect_runtime:
        _require(sys.version_info[:2] == (3, 11), "SERVER_PYTHON_VERSION_MISMATCH")
        for module in ("fastapi", "uvicorn", "boto3"):
            _require(importlib.util.find_spec(module) is not None, "SERVER_DEPENDENCY_MISSING")
        for key, value in paths.items():
            path = Path(value)
            _require(path.is_dir() and path.resolve() == path and not path.is_symlink(), "SERVER_DIRECTORY_INVALID")
            if key != "engineRoot":
                _require(os.access(path, os.W_OK | os.X_OK) and path.stat().st_uid == os.getuid()
                         and not path.stat().st_mode & 0o077, "SERVER_DIRECTORY_PERMISSIONS_INVALID")
        node = env.get("SFDC_MANAGED_EXECUTION_NODE", "/usr/local/bin/node")
        _require(_path(node) and Path(node).is_file() and os.access(node, os.X_OK), "SERVER_NODE_BINARY_REQUIRED")
        try:
            result = subprocess.run([node, "--version"], check=True, capture_output=True, text=True, timeout=5)
            _require(result.stdout.strip() == "v" + NODE_VERSION, "SERVER_NODE_VERSION_MISMATCH")
        except (OSError, subprocess.SubprocessError):
            raise PreflightError("SERVER_NODE_BINARY_REQUIRED") from None
        _verify_runtime(Path(paths["engineRoot"]), contract["artifact"])
        _require((Path(paths["engineRoot"]) / "node_modules/@lwc/compiler/package.json").is_file(), "SERVER_NODE_DEPENDENCIES_MISSING")
        if contract["tools"]:
            try:
                installed = json.loads(Path("/opt/sfdc/vendor/vendor-tools.json").read_text())["tools"]
                for name, tool in contract["tools"].items():
                    binary = Path(tool["path"])
                    _require(installed.get(name) == tool and binary.is_file() and not binary.is_symlink()
                             and os.access(binary, os.X_OK), "SERVER_VENDOR_TOOL_UNAVAILABLE")
                    with binary.open("rb") as stream:
                        _require(hashlib.file_digest(stream, "sha256").hexdigest() == tool["sha256"], "SERVER_VENDOR_BINARY_HASH_MISMATCH")
                if contract["capabilities"]["visualCapture"]:
                    _require(Path("/opt/google/chrome/chrome").resolve(strict=True) == Path(contract["tools"]["chrome"]["path"]),
                             "SERVER_CHROME_CHANNEL_UNAVAILABLE")
            except (OSError, ValueError, TypeError, KeyError) as error:
                if isinstance(error, PreflightError):
                    raise
                raise PreflightError("SERVER_VENDOR_TOOL_UNAVAILABLE") from None
    report = {"schemaVersion": 1, "kind": "sfdc-agent-server-readiness", "startupReady": True,
              "inspection": "LOCAL_RUNTIME_AND_CONFIGURATION" if inspect_runtime else "CONFIGURATION_ONLY",
              "externalServices": "NOT_CONTACTED", "deploymentReady": False,
              "workerRouting": "SCOPE_DEDICATED_SINGLE_PROCESS", "runtimeSha256": contract["artifact"]["runtimeSha256"],
              "capabilities": {"localArchive": "CONFIGURED", "figmaRest": "CONFIGURED_NOT_AUTHENTICATED" if contract["capabilities"]["figmaRest"] else "DISABLED",
                               "kiroHeadless": "CONFIGURED_NOT_AUTHENTICATED" if contract["capabilities"]["kiroHeadless"] else "DISABLED",
                               "visualCapture": "BINARY_CONFIGURED_NOT_BROWSER_VERIFIED" if contract["capabilities"]["visualCapture"] else "UNAVAILABLE",
                               "figmaMcp": "NOT_READY"},
              "remainingAcceptance": ["IAM_ROLE_AND_SCOPED_S3", "BACKEND_SERVICE_AUTH", "PROVIDER_AUTHENTICATION_WHEN_ENABLED",
                                      "NODE_REPLACEMENT_AND_RESTART", "CHROME_BROWSER_LAUNCH_WHEN_ENABLED", "FIGMA_MCP_SERVER_PROMOTION"]}
    return Startup(contract, report, secrets)


def _validate_mcp_promotion(value):
    """Check future server OAuth requirements; this does not enable MCP."""
    _keys(value, {"kind", "schemaVersion", "implementationStatus", "currentServerEnabled", "authentication",
                  "credentialStore", "lifecycle", "topology", "promotion"})
    _require(value["kind"] == "figma-mcp-server-promotion/v1" and value["schemaVersion"] == 1
             and value["implementationStatus"] == "CONTRACT_ONLY_NOT_IMPLEMENTED" and value["currentServerEnabled"] is False,
             "FIGMA_MCP_PROMOTION_STATUS_INVALID")
    auth = value["authentication"]
    _keys(auth, {"flow", "callbackUrl", "principalBinding", "pkce", "state", "nonce", "browserConsent", "cliHomeAllowed"})
    callback = urlsplit(auth["callbackUrl"])
    _require(callback.scheme == "https" and callback.hostname and callback.hostname not in {"localhost", "127.0.0.1"}
             and not callback.username and not callback.password and not callback.query and not callback.fragment
             and auth["flow"] == "authorization_code" and auth["principalBinding"] == ["tenantId", "workspaceId", "userId"]
             and all(auth[key] is True for key in ("pkce", "state", "nonce", "browserConsent")) and auth["cliHomeAllowed"] is False,
             "FIGMA_MCP_SERVER_OAUTH_CONTRACT_INVALID")
    _require(value["credentialStore"] == {"provider": "shared-encrypted-store", "kmsKeyReferenceRequired": True,
                  "tokensInLogsAllowed": False, "cacheScope": "tenant/workspace/user", "atRestEncryption": True},
             "FIGMA_MCP_CREDENTIAL_STORE_CONTRACT_INVALID")
    _require(value["lifecycle"] == {"refreshRotation": True, "revokeDisconnect": True, "revokeUserRemoval": True,
                  "keyRotation": True, "expiredCredentialRequiresConsent": True, "silentRestFallback": False},
             "FIGMA_MCP_LIFECYCLE_CONTRACT_INVALID")
    _require(value["topology"] == {"distributedLock": True, "multiNode": True, "restartWithoutLocalHome": True},
             "FIGMA_MCP_TOPOLOGY_CONTRACT_INVALID")
    promotion = value["promotion"]
    _keys(promotion, {"requiredEvidence", "passingEvidence", "enablementRequiresImplementationRelease"})
    required = ["httpsCallbackCsrfPkce", "crossPrincipalDenied", "refreshAndRevocation", "twoNodeConcurrency",
                "restartRecovery", "keyRotation", "noCredentialLogging", "explicitMcpFailureNoFallback"]
    _require(promotion["requiredEvidence"] == required and promotion["passingEvidence"] == []
             and promotion["enablementRequiresImplementationRelease"] is True, "FIGMA_MCP_PROMOTION_EVIDENCE_REQUIRED")
    return {"contractValid": True, "implemented": False, "serverEnabled": False, "status": "NOT_READY"}


def validate_mcp_promotion(value):
    try:
        return _validate_mcp_promotion(value)
    except (TypeError, KeyError, ValueError, RecursionError) as error:
        if isinstance(error, PreflightError):
            raise
        raise PreflightError("FIGMA_MCP_PROMOTION_CONTRACT_INVALID") from None
