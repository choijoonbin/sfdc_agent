"""Server packaging and policy tests; no AWS/provider/DB/service access."""

import asyncio
from copy import deepcopy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
from time import time
import unittest
from unittest.mock import AsyncMock, patch

from sfdc_agent.server_preflight import PreflightError, preflight, read_contract, read_secret, validate_contract, validate_mcp_promotion
from sfdc_agent.server_runtime import ServerCapabilityBoundary

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deployment" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifact = load_module("runtime_install", "install_runtime.py")
vendor = load_module("vendor_install", "install_vendor_tools.py")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def contract():
    return read_contract(ROOT / "deployment/agent-server-contract.example.json")


def environment(value):
    storage, paths, network = (value[key] for key in ("storage", "paths", "network"))
    return {"SFDC_AGENT_SERVER_MODE": "1", "SFDC_AGENT_MANAGED_ONLY": "1", "SFDC_STORAGE_PROVIDER": "s3",
            "AWS_REGION": storage["region"], "SFDC_STORAGE_S3_BUCKET": storage["bucket"], "SFDC_STORAGE_S3_PREFIX": storage["prefix"],
            "SFDC_MANAGED_ENGINE_ROOT": paths["engineRoot"], "SFDC_AGENT_ROOT": paths["stateRoot"], "TMPDIR": paths["scratchRoot"],
            "SFDC_MANAGED_AGENT_ORIGIN": network["selfOrigin"], "SFDC_MANAGED_RUNTIME_SHA256": value["artifact"]["runtimeSha256"],
            "SFDC_AGENT_TRUSTED_HOSTS": ",".join(network["trustedHosts"]),
            "SFDC_MANAGED_FIGMA_SCOPE": value["worker"]["scope"]["tenantId"] + "/" + value["worker"]["scope"]["workspaceId"],
            "SFDC_AGENT_EXECUTION_TOKEN": "synthetic-service-token-never-a-real-secret",
            "SFDC_AGENT_EXECUTION_TOKEN_REF": value["secretReferences"]["executionToken"]}


def runtime_archive(files=None, special=None):
    files = files if files is not None else {"package.json": b'{}', "package-lock.json": b'{}',
                "nextbss/server/managed-execution.mjs": b'export const synthetic = true;\n'}
    entries = {name: digest(data) for name, data in sorted(files.items())}
    managed = {"sha256": digest(json.dumps(entries, separators=(",", ":")).encode()), "entries": entries}
    files = {**files, "managed-runtime.json": (json.dumps(managed, indent=2) + "\n").encode()}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o644
            archive.addfile(member, io.BytesIO(data))
        if special:
            archive.addfile(special)
    compressed = gzip.compress(stream.getvalue(), mtime=0)
    item = {"name": "node-runtime", "file": "sfdc-node-runtime.tar.gz", "sha256": digest(compressed), "bytes": len(compressed),
            "treeSha256": artifact.tree_sha256(files), "fileCount": len(files), "runtimeManifestSha256": digest(files["managed-runtime.json"]), "commit": "a" * 40}
    metadata = {"schemaVersion": 1, "kind": "sfdc-ui-server-artifacts", "source": {"commit": "a" * 40, "treeSha256": "b" * 64},
                "toolchain": {"node": "26.13.0"}, "artifacts": [{"name": "ui"}, item]}
    pins = {"expected_sha256": item["sha256"], "expected_commit": item["commit"], "expected_manifest_sha256": item["runtimeManifestSha256"]}
    return compressed, json.dumps(metadata).encode(), pins, files


class RuntimeArtifactTests(unittest.TestCase):
    def test_installs_a_pinned_complete_runtime_atomically(self):
        archive, metadata, pins, files = runtime_archive()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "sfdc-node-runtime.tar.gz"
            source.write_bytes(archive)
            manifest = root / "artifacts.json"
            manifest.write_bytes(metadata)
            target = root / "runtime"
            receipt = artifact.install(source, manifest, target, **pins)
            self.assertEqual(receipt["uiCommit"], "a" * 40)
            self.assertEqual({str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}, files)
            with self.assertRaises(artifact.ArtifactError):
                artifact.install(source, manifest, target, **pins)
            self.assertFalse(any(root.glob(".sfdc-runtime-*")))

    def test_independent_pins_prevent_replacing_both_archive_and_manifest(self):
        archive, metadata, pins, _ = runtime_archive()
        for key in pins:
            altered = {**pins, key: "c" * (40 if key == "expected_commit" else 64)}
            with self.subTest(key=key), self.assertRaises(artifact.ArtifactError):
                artifact.verify_artifact(archive, metadata, **altered)

    def test_traversal_links_duplicate_and_unexpected_roots_are_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
                           ("nextbss/link", tarfile.SYMTYPE), ("nextbss/hard", tarfile.LNKTYPE),
                           ("nextbss/device", tarfile.CHRTYPE), ("package.json", tarfile.REGTYPE)):
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/outside" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
            data, metadata, pins, _ = runtime_archive(special=member)
            with self.subTest(name=name), self.assertRaises(artifact.ArtifactError):
                artifact.verify_artifact(data, metadata, **pins)
        for name in ("node_modules/private.js", "nextbss/.env", "other/private.js"):
            data, metadata, pins, _ = runtime_archive({"package.json": b'{}', "package-lock.json": b'{}', name: b"secret"})
            with self.subTest(name=name), self.assertRaises(artifact.ArtifactError):
                artifact.verify_artifact(data, metadata, **pins)

    def test_truncated_or_tampered_inventory_fails(self):
        archive, metadata, pins, files = runtime_archive()
        files["nextbss/server/managed-execution.mjs"] = b"changed"
        with self.assertRaises(artifact.ArtifactError):
            artifact.verify_runtime(files)
        with self.assertRaises(artifact.ArtifactError):
            artifact.verify_artifact(archive[:-10], metadata, **pins)


class ServerContractTests(unittest.TestCase):
    def test_core_configuration_is_truthful_and_never_contacts_providers(self):
        value = contract()
        with patch("subprocess.run", side_effect=AssertionError("no binary/provider execution in configuration-only check")):
            startup = preflight(value, environment(value), inspect_runtime=False)
        self.assertTrue(startup.report["startupReady"])
        self.assertFalse(startup.report["deploymentReady"])
        self.assertEqual(startup.report["externalServices"], "NOT_CONTACTED")
        self.assertEqual(startup.report["capabilities"]["figmaMcp"], "NOT_READY")
        self.assertEqual(startup.report["capabilities"]["visualCapture"], "UNAVAILABLE")
        self.assertNotIn("synthetic-service-token", json.dumps(startup.report))
        self.assertNotIn("synthetic-service-token", repr(startup))

    def test_server_cannot_inherit_developer_paths_auth_or_multi_worker_defaults(self):
        value = contract()
        cases = {"SFDC_AGENT_MANAGED_ONLY": "0", "SFDC_STORAGE_PROVIDER": "local", "SFDC_UI_ROOT": "/Users/person/SFDC_Ui",
                 "AWS_PROFILE": "personal", "AWS_SHARED_CREDENTIALS_FILE": "/home/person/.aws/credentials",
                 "AWS_ACCESS_KEY_ID": "synthetic", "SFDC_MANAGED_AGENT_ORIGIN": "http://other-agent:8766",
                 "SFDC_AGENT_TRUSTED_HOSTS": "*", "SFDC_MANAGED_FIGMA_SCOPE": "wrong/scope", "WEB_CONCURRENCY": "2",
                 "SFDC_MANAGED_EXECUTION_RUNNER": "/tmp/arbitrary.mjs", "SFDC_AGENT_EXECUTION_TOKEN_REF": "/wrong/ref"}
        for name, item in cases.items():
            with self.subTest(name=name), self.assertRaises(PreflightError):
                preflight(value, {**environment(value), name: item}, inspect_runtime=False)
        for path in ("/Users/person/runtime", "/home/person/runtime", "/opt/../runtime"):
            changed = deepcopy(value)
            changed["paths"]["engineRoot"] = path
            with self.subTest(path=path), self.assertRaises(PreflightError):
                validate_contract(changed)

    def test_optional_rest_and_kiro_require_explicit_scoped_secret_refs_and_binary_pins(self):
        value = contract()
        value["capabilities"]["figmaRest"] = True
        with self.assertRaises(PreflightError):
            validate_contract(value)
        value["secretReferences"]["figmaRestToken"] = "/sfdc/dev/agent/FIGMA_ACCESS_TOKEN"
        env = environment(value) | {"FIGMA_ACCESS_TOKEN_REF": value["secretReferences"]["figmaRestToken"], "FIGMA_ACCESS_TOKEN": "synthetic-rest-access-token"}
        self.assertEqual(preflight(value, env, inspect_runtime=False).report["capabilities"]["figmaRest"], "CONFIGURED_NOT_AUTHENTICATED")
        value["capabilities"]["kiroHeadless"] = True
        value["secretReferences"]["kiroApiKey"] = "/sfdc/dev/agent/KIRO_API_KEY"
        with self.assertRaises(PreflightError):
            validate_contract(value)
        value["tools"]["kiro-cli"] = {"path": "/opt/sfdc/vendor/kiro-cli/bin/kiro-cli", "version": "1.2.3", "sha256": "b" * 64, "archiveSha256": "c" * 64}
        env |= {"KIRO_API_KEY_REF": value["secretReferences"]["kiroApiKey"], "KIRO_API_KEY": "ksk_synthetic_key_value_only",
                "NOVA_KIRO_CLI": value["tools"]["kiro-cli"]["path"], "SFDC_AGENT_SCREEN_KIRO_MODE": "headless", "SFDC_AGENT_STORY_KIRO_MODE": "headless"}
        self.assertEqual(preflight(value, env, inspect_runtime=False).report["capabilities"]["kiroHeadless"], "CONFIGURED_NOT_AUTHENTICATED")
        with self.assertRaises(PreflightError):
            preflight(value, {**env, "SFDC_AGENT_SCREEN_KIRO_MODE": "interactive"}, inspect_runtime=False)

    def test_secret_files_reject_links_permissive_mode_and_ambiguous_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            private = root / "service-token"
            private.write_text("synthetic-service-token-with-sufficient-length\n")
            private.chmod(0o600)
            env = {"SFDC_AGENT_EXECUTION_TOKEN_FILE": str(private)}
            self.assertTrue(read_secret("SFDC_AGENT_EXECUTION_TOKEN", env).startswith("synthetic"))
            with self.assertRaises(PreflightError):
                read_secret("SFDC_AGENT_EXECUTION_TOKEN", {**env, "SFDC_AGENT_EXECUTION_TOKEN": "also-present"})
            private.chmod(0o644)
            with self.assertRaises(PreflightError):
                read_secret("SFDC_AGENT_EXECUTION_TOKEN", env)
            private.chmod(0o600)
            link = root / "link"
            link.symlink_to(private)
            with self.assertRaises(PreflightError):
                read_secret("SFDC_AGENT_EXECUTION_TOKEN", {"SFDC_AGENT_EXECUTION_TOKEN_FILE": str(link)})

    def test_mcp_contract_is_checkable_but_cannot_be_promoted_by_a_flag(self):
        value = json.loads((ROOT / "deployment/figma-mcp-server-promotion.json").read_text())
        self.assertEqual(validate_mcp_promotion(value)["status"], "NOT_READY")
        changes = [("authentication", "callbackUrl", "http://localhost:7778/oauth/callback"),
                   ("authentication", "principalBinding", ["userId"]), ("credentialStore", "provider", "local-home"),
                   ("lifecycle", "silentRestFallback", True), ("lifecycle", "revokeDisconnect", False),
                   ("topology", "multiNode", False), ("promotion", "passingEvidence", ["pretend-tested"])]
        for section, key, item in changes:
            changed = deepcopy(value)
            changed[section][key] = item
            with self.subTest(key=key), self.assertRaises(PreflightError):
                validate_mcp_promotion(changed)
        changed = contract()
        changed["capabilities"]["figmaMcp"] = True
        with self.assertRaisesRegex(PreflightError, "FIGMA_MCP_SERVER_NOT_IMPLEMENTED"):
            validate_contract(changed)


async def asgi_request(app, path, payload=None, *, token="synthetic-service-token-never-a-real-secret", extra_headers=()):
    body = json.dumps(payload).encode() if payload is not None else b""
    sent, consumed = [], False

    async def receive():
        nonlocal consumed
        if consumed:
            await asyncio.Event().wait()
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"host", b"127.0.0.1:8766"), (b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", ("Bearer " + token).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers + list(extra_headers),
             "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8766)}
    await app(scope, receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    result = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, json.loads(result) if result else None


class ServerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.value, self.observed = contract(), []

        async def existing(scope, receive, send):
            message = await receive()
            self.observed.append(message["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"existingApi":true}'})

        self.existing = existing

    def boundary(self):
        return ServerCapabilityBoundary(self.existing, self.value)

    async def test_local_nova_archive_keeps_body_engine_and_source_unchanged(self):
        payload = {"engine": "NOVA", "kind": "SCREEN", "operation": "ANALYZE", "sourceAcquisition": "LOCAL_FILE",
                   "sourceReference": "figma-local://registered/source?node-id=1-2", "archive": {"sha256": "b" * 64},
                   "executionScope": self.value["worker"]["scope"]}
        status, _ = await asgi_request(self.boundary(), "/internal/v1/executions", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.observed[0]), payload)

    async def test_rest_explicitly_configured_is_forwarded_without_engine_fallback(self):
        self.value["capabilities"].update(figmaRest=True, kiroHeadless=True)
        payload = {"engine": "KIRO", "kind": "SCREEN", "sourceAcquisition": "REST_ONLY",
                   "sourceReference": "https://www.figma.com/design/Registered", "executionScope": self.value["worker"]["scope"]}
        status, _ = await asgi_request(self.boundary(), "/internal/v1/executions", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.observed[0]), payload)

    async def test_mcp_explicit_or_implicit_is_not_silently_replaced_with_rest(self):
        self.value["capabilities"].update(figmaRest=True, kiroHeadless=True)
        base = {"engine": "KIRO", "kind": "SCREEN", "sourceReference": "https://www.figma.com/design/Registered"}
        for payload in (base, {**base, "sourceAcquisition": "MCP_ONLY"}):
            status, result = await asgi_request(self.boundary(), "/internal/v1/executions", payload)
            self.assertEqual((status, result["error"]["code"]), (503, "FIGMA_MCP_SERVER_NOT_READY"))
        status, _ = await asgi_request(self.boundary(), "/internal/v1/figma-mcp/collect", {})
        self.assertEqual(status, 503)
        self.assertEqual(self.observed, [])

    async def test_disabled_binary_capabilities_and_cross_scope_are_rejected(self):
        for path, payload, code in (("/internal/v1/executions", {"engine": "KIRO", "sourceAcquisition": "LOCAL_FILE"}, "KIRO_SERVER_DISABLED"),
                 ("/internal/kiro/sessions", {}, "KIRO_SERVER_DISABLED"),
                 ("/internal/v1/visual-comparisons", {}, "VISUAL_CAPTURE_SERVER_UNAVAILABLE"),
                 ("/internal/v1/source-discovery", {"executionScope": {"tenantId": "different"}}, "SERVER_WORKER_SCOPE_FORBIDDEN")):
            status, result = await asgi_request(self.boundary(), path, payload)
            self.assertEqual(result["error"]["code"], code)
            self.assertIn(status, {403, 503})

    async def test_existing_24_mib_archive_or_projection_envelope_is_preserved(self):
        payload = {"executionScope": self.value["worker"]["scope"], "bytes": "x" * (17 * 1024 * 1024)}
        status, _ = await asgi_request(self.boundary(), "/internal/v1/analysis-views", payload)
        self.assertEqual(status, 200)
        self.assertGreater(len(self.observed[0]), 16 * 1024 * 1024)
        self.assertEqual(json.loads(self.observed[0]), payload)

    async def test_real_api_preserves_service_auth_and_local_archive_discovery(self):
        from sfdc_agent.api import app
        from sfdc_agent import managed_execution
        payload = {"executionScope": self.value["worker"]["scope"], "archive": {**self.value["worker"]["scope"], "fileKey": "Synthetic", "sha256": "b" * 64}}
        service = ServerCapabilityBoundary(app, self.value)
        with patch.dict(os.environ, environment(self.value), clear=False), patch.object(managed_execution, "_discover_source", new=AsyncMock(return_value={"syntheticArchive": True})) as discover:
            status, _ = await asgi_request(service, "/internal/v1/source-discovery", payload, token=None)
            self.assertEqual(status, 401)
            discover.assert_not_awaited()
            status, _ = await asgi_request(service, "/internal/v1/source-discovery", payload, extra_headers=[(b"origin", b"https://not-a-service.invalid")])
            self.assertEqual(status, 403)
            discover.assert_not_awaited()
            status, result = await asgi_request(service, "/internal/v1/source-discovery", payload)
            self.assertEqual((status, result), (200, {"syntheticArchive": True}))
            discover.assert_awaited_once()

    async def test_real_nova_local_execution_preserves_request_validation_and_selected_engine(self):
        from sfdc_agent.api import app
        from sfdc_agent import managed_execution
        payload = {"jobId": "33333333-3333-4333-8333-333333333333", "attempt": 1,
                   "executionScope": self.value["worker"]["scope"], "timeoutSeconds": 10, "deadlineEpochSeconds": int(time()) + 9,
                   "kind": "SCREEN", "engine": "NOVA", "operation": "ANALYZE", "sourceAcquisition": "LOCAL_FILE",
                   "sourceReference": "figma-local://registered/source?node-id=1-2", "designReleaseId": "synthetic-design-release",
                   "promptReleaseId": "prompts-v1", "archive": {**self.value["worker"]["scope"], "fileKey": "Synthetic", "sha256": "b" * 64}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capabilities = root / "nextbss/engine/managed-runtime-capabilities.json"
            capabilities.parent.mkdir(parents=True)
            capabilities.write_text(json.dumps({"schemaVersion": 1, "generationFamilies": [], "sourceAcquisitionVersion": 1, "sourceAcquisitions": ["LOCAL_FILE"]}))
            service = ServerCapabilityBoundary(app, self.value)
            result = managed_execution.ExecutionResult(status="COMPLETED", title="Synthetic local NOVA", artifacts=[])
            with patch.dict(os.environ, {**environment(self.value), "SFDC_MANAGED_ENGINE_ROOT": temporary}, clear=False), \
                 patch.object(managed_execution, "_runner_configuration", return_value=("node", root / "runner.mjs", 900)), \
                 patch.object(managed_execution, "_execute", new=AsyncMock(return_value=result)) as execute:
                status, response = await asgi_request(service, "/internal/v1/executions", payload)
                self.assertEqual((status, response["title"]), (200, "Synthetic local NOVA"))
                selected = execute.await_args.args[0]
                self.assertEqual((selected.engine, selected.sourceAcquisition), ("NOVA", "LOCAL_FILE"))
                self.assertEqual(str(selected.executionScope.tenantId), self.value["worker"]["scope"]["tenantId"])
                invalid = {**payload, "archive": None}
                status, _ = await asgi_request(service, "/internal/v1/executions", invalid)
                self.assertEqual(status, 422)
                execute.assert_awaited_once()


def tool_fixture(*, script=None, special=None):
    script = script or b"#!/bin/sh\nprintf 'Synthetic CLI 1.2.3\\n'\n"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        member = tarfile.TarInfo("vendor/bin/kiro-cli")
        member.size = len(script)
        archive.addfile(member, io.BytesIO(script))
        if special:
            archive.addfile(special)
    data = stream.getvalue()
    value = {"schemaVersion": 1, "kind": "sfdc-agent-vendor-tools", "approvedDownloadHosts": ["downloads.example.invalid"],
             "tools": [{"name": "kiro-cli", "version": "1.2.3", "url": "https://downloads.example.invalid/releases/kiro-1.2.3.tar.gz",
                        "sha256": digest(data), "format": "tar.gz", "stripPrefix": "vendor", "entrypoint": "bin/kiro-cli", "entrypointSha256": digest(script)}]}
    return value, data


class VendorToolTests(unittest.TestCase):
    def test_approved_pinned_binary_installs_and_reports_version_without_credentials(self):
        value, data = tool_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary).resolve() / "vendor"
            receipts = vendor.install(value, destination, fetch=lambda *_: data)
            self.assertEqual(receipts["kiro-cli"]["version"], "1.2.3")
            self.assertTrue(os.access(receipts["kiro-cli"]["path"], os.X_OK))
            self.assertEqual(json.loads((destination / "vendor-tools.json").read_text())["tools"], receipts)

    def test_unsafe_urls_hashes_links_and_version_mismatch_leave_no_installation(self):
        value, data = tool_fixture()
        for key, changed in (("url", "https://downloads.example.invalid/latest/kiro.tar.gz"),
                             ("url", "http://downloads.example.invalid/releases/kiro.tar.gz"),
                             ("url", "https://unapproved.invalid/releases/kiro.tar.gz"), ("version", "latest")):
            bad = deepcopy(value)
            bad["tools"][0][key] = changed
            with self.subTest(key=key, changed=changed), self.assertRaises(vendor.ToolError):
                vendor.validate_contract(bad)
        link = tarfile.TarInfo("vendor/bin/linked")
        link.type, link.linkname = tarfile.SYMTYPE, "/outside"
        cases = [tool_fixture(special=link), tool_fixture(script=b"#!/bin/sh\nprintf 'wrong version\\n'\n")]
        bad = deepcopy(value)
        bad["tools"][0]["sha256"] = "b" * 64
        cases.append((bad, data))
        for changed, content in cases:
            with tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary).resolve() / "vendor"
                with self.assertRaises(vendor.ToolError):
                    vendor.install(changed, destination, fetch=lambda *_: content)
                self.assertFalse(destination.exists())
                self.assertFalse(any(destination.parent.glob(".sfdc-vendor-*")))


if __name__ == "__main__":
    unittest.main()
