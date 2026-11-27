import asyncio
from contextlib import asynccontextmanager, contextmanager
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from threading import Event
import tempfile
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from sfdc_agent.managed_object_storage import ManagedObjectStorage, storage_io


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def digest(value):
    return sha256(value).hexdigest()


class PreconditionFailed(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.fail_key = None

    def get_object(self, *, Bucket, Key):
        value = self.objects[Key]
        return {"Body": io.BytesIO(value), "ContentLength": len(value), "Metadata": {"sha256": digest(value)}}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, Metadata):
        if IfNoneMatch != "*" or Metadata != {"sha256": digest(Body)}:
            raise AssertionError("Missing immutable write contract")
        if Key == self.fail_key:
            raise OSError("synthetic transport failure")
        if Key in self.objects:
            raise PreconditionFailed()
        self.objects[Key] = bytes(Body)
        self.writes.append(Key)


class ManagedObjectStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.client = FakeS3()
        self.storage = ManagedObjectStorage(self.client, "fixture-business-files", "dev/nova/")
        self.release = "nextbss-1.9-" + "a" * 64
        self.release_file = b'{"synthetic":"immutable-release"}'
        self.release_manifest = encoded({"releaseId": self.release, "files": [
            {"path": "nextbss/design-system/tokens.json", "bytes": len(self.release_file), "sha256": digest(self.release_file)}]})
        self.client.objects[self.storage.object_key("releases", self.release + "/release.json")] = self.release_manifest
        self.client.objects[self.storage.object_key("releases", self.release + "/nextbss/design-system/tokens.json")] = self.release_file
        self.archive_bytes = b"synthetic-figma-archive"
        self.archive = SimpleNamespace(tenantId=uuid4(), workspaceId=uuid4(), fileKey="ApprovedFile", sha256=digest(self.archive_bytes))
        self.archive_key = f"{self.archive.tenantId}/{self.archive.workspaceId}/{self.archive.fileKey}/{self.archive.sha256}.fig"
        self.client.objects[self.storage.object_key("figma-sources", self.archive_key)] = self.archive_bytes
        self.enterContext(patch.dict(os.environ, {"SFDC_STORAGE_PROVIDER": "local"}))

    def prepare(self, **changes):
        return self.storage.prepare(self.root, archive=self.archive, release_id=self.release,
            manifest_sha=digest(self.release_manifest), deadline=time() + 60, **changes)

    def design_index(self):
        index = {}
        for kind in ("guide", "library"):
            value = (kind + "-figma-original").encode()
            key = "design-sources/" + kind + ".fig"
            self.client.objects[self.storage.object_key("figma-sources", key)] = value
            index[kind] = {"objectKey": key, "sha256": digest(value), "sizeBytes": len(value)}
        self.client.objects[self.storage.object_key("figma-sources", "design-sources/index.json")] = encoded(index)
        return index

    def output(self):
        output = self.root / "output"
        output.mkdir()
        value = b'{"safe":"public-view"}'
        (output / "public-view.json").write_bytes(value)
        manifest = encoded({"view": {"path": "public-view.json", "sizeBytes": len(value), "sha256": digest(value)}, "assets": []})
        (output / "manifest.json").write_bytes(manifest)
        return output, manifest

    def test_materializes_only_exact_pinned_release_and_scoped_source_in_private_workspace(self):
        environment = self.prepare()
        self.assertEqual((Path(environment["SFDC_MANAGED_SOURCE_ROOT"]) / self.archive_key).read_bytes(), self.archive_bytes)
        release = Path(environment["SFDC_MANAGED_RELEASE_ROOT"]) / self.release
        self.assertEqual((release / "nextbss/design-system/tokens.json").read_bytes(), self.release_file)
        self.assertEqual((release / "release.json").read_bytes(), self.release_manifest)
        self.assertEqual((release / "release.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(key.startswith("AWS") or "TOKEN" in key for key in environment))

    def test_larger_core_inventories_preserve_nested_runtime_files_without_losing_bounds(self):
        body = b'export default "offline-fixture";\n'
        entries = []
        for index in range(1030):
            path = f"nextbss/server/conversion-quality/runtime-{index}.mjs"
            entries.append({"path": path, "bytes": len(body), "sha256": digest(body)})
            self.client.objects[self.storage.object_key("releases", self.release + "/" + path)] = body
        raw = encoded({"releaseId": self.release, "files": entries})
        self.client.objects[self.storage.object_key("releases", self.release + "/release.json")] = raw
        environment = self.storage.prepare(self.root, release_id=self.release, manifest_sha=digest(raw))
        target = Path(environment["SFDC_MANAGED_RELEASE_ROOT"]) / self.release / entries[-1]["path"]
        self.assertEqual(target.read_bytes(), body)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        oversized = encoded({"releaseId": self.release, "files": [entries[0]] * 5001})
        self.client.objects[self.storage.object_key("releases", self.release + "/release.json")] = oversized
        with self.assertRaises(ValueError):
            self.storage.prepare(self.root, release_id=self.release, manifest_sha=digest(oversized))

    def test_rejects_hash_mismatch_path_traversal_duplicate_entries_and_unknown_release_pin(self):
        with self.assertRaises(ValueError):
            self.storage.prepare(self.root, release_id=self.release, manifest_sha="0" * 64)
        for entries in ([{"path": "../outside", "bytes": 1, "sha256": "a" * 64}],
                        [{"path": "same", "bytes": 0, "sha256": digest(b"")}]*2,
                        [{"path": "large", "bytes": 33*1024*1024, "sha256": "a"*64}]):
            manifest = encoded({"releaseId": self.release, "files": entries})
            self.client.objects[self.storage.object_key("releases", self.release + "/release.json")] = manifest
            with self.assertRaises(ValueError):
                self.storage.prepare(self.root, release_id=self.release, manifest_sha=digest(manifest))
        self.assertFalse((self.root.parent / "outside").exists())

    def test_source_size_and_digest_are_checked_before_node_runs(self):
        self.client.objects[self.storage.object_key("figma-sources", self.archive_key)] = b"changed"
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.root / "sources" / self.archive_key).exists())
        self.assertFalse(list(self.root.rglob("*.part")))

    def test_design_source_object_index_materializes_into_existing_node_path_contract(self):
        index = self.design_index()
        environment = self.prepare(design_sources=True)
        local = json.loads(Path(environment["SFDC_MANAGED_DESIGN_SOURCES"]).read_text())
        self.assertEqual(set(local), {"guide", "library"})
        for kind, path in local.items():
            self.assertTrue(Path(path).is_absolute())
            self.assertEqual(digest(Path(path).read_bytes()), index[kind]["sha256"])

    def test_admin_design_originals_use_separate_256_mib_limit_from_user_archives(self):
        index = self.design_index()
        index['library']['sizeBytes'] = 150_462_817
        self.client.objects[self.storage.object_key('figma-sources', 'design-sources/index.json')] = encoded(index)
        limits = []
        def download(area, key, path, maximum, expected_sha, expected_size=None, deadline=None):
            self.assertLessEqual(expected_size, maximum)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic')
            limits.append(maximum)
        with patch.object(self.storage, 'download', download):
            self.storage.prepare(self.root, design_sources=True)
        self.assertEqual(limits, [256*1024*1024, 256*1024*1024])

    def test_uploads_verified_files_then_manifest_and_is_idempotent_by_actual_bytes(self):
        output, manifest = self.output()
        prefix = "analysis/" + str(uuid4()) + "/1"
        self.storage.publish_output(output, prefix, "manifest.json", digest(manifest), analysis=True)
        self.assertEqual(self.client.writes[-1], self.storage.object_key("release-ingress", prefix + "/manifest.json"))
        self.storage.publish_output(output, prefix, "manifest.json", digest(manifest), analysis=True)
        self.assertEqual(len(self.client.writes), 2)
        self.client.objects[self.storage.object_key("release-ingress", prefix + "/public-view.json")] = b"changed"
        with self.assertRaises(ValueError):
            self.storage.publish_output(output, prefix, "manifest.json", digest(manifest), analysis=True)

    def test_failed_or_unverified_upload_never_publishes_completion_marker(self):
        output, manifest = self.output()
        prefix = "analysis/" + str(uuid4()) + "/1"
        marker = self.storage.object_key("release-ingress", prefix + "/manifest.json")
        self.client.fail_key = self.storage.object_key("release-ingress", prefix + "/public-view.json")
        with self.assertRaises(ValueError):
            self.storage.publish_output(output, prefix, "manifest.json", digest(manifest), analysis=True)
        self.assertNotIn(marker, self.client.objects)
        (output / "unexpected-private-file").write_text("synthetic")
        with self.assertRaises(ValueError):
            self.storage.publish_output(output, prefix, "manifest.json", digest(manifest), analysis=True)
        self.assertNotIn(marker, self.client.objects)

    def test_local_mode_is_lazy_and_deadlines_stop_transfers(self):
        self.assertIsNone(ManagedObjectStorage.configured())
        with patch.dict(os.environ, {"SFDC_STORAGE_PROVIDER": "invalid"}):
            with self.assertRaises(ValueError):
                ManagedObjectStorage.configured()
        with self.assertRaises(TimeoutError):
            self.storage.prepare(self.root, archive=self.archive, deadline=time() - 1)
        self.assertFalse(list(self.root.rglob("*.fig")))

    async def test_discovery_uses_private_s3_source_without_forwarding_aws_credentials(self):
        from sfdc_agent.managed_execution import SourceDiscoveryRequest, _discover_source
        payload = SourceDiscoveryRequest.model_validate({"archive": vars(self.archive),
            "executionScope": {"tenantId": self.archive.tenantId, "workspaceId": self.archive.workspaceId}})
        runner = self.root / "nextbss/server/managed-source-discovery.mjs"
        runner.parent.mkdir(parents=True)
        runner.write_text("// synthetic runner")
        observed = []
        async def process(binary, runner, scratch, timeout, encoded, environment):
            source = Path(environment["SFDC_MANAGED_SOURCE_ROOT"]) / self.archive_key
            self.assertEqual(source.read_bytes(), self.archive_bytes)
            self.assertTrue(str(source).startswith(str(Path(scratch).resolve())))
            self.assertFalse(any(key.startswith("AWS") for key in environment))
            observed.append(source)
            return b'{"result":{"pages":[]}}'
        with patch("sfdc_agent.managed_execution.ManagedObjectStorage.configured", return_value=self.storage), \
                patch("sfdc_agent.managed_execution.engine_root", return_value=self.root), \
                patch("sfdc_agent.managed_execution._runner_configuration", return_value=("node", runner, 90)), \
                patch("sfdc_agent.managed_execution._run_process", process):
            self.assertEqual(await _discover_source(payload), {"pages": []})
        self.assertTrue(observed)
        self.assertFalse(observed[0].exists())

    async def test_design_build_uses_s3_inputs_and_publishes_output_manifest_before_workspace_cleanup(self):
        from sfdc_agent.managed_design import DesignBuild, run_build
        self.design_index()
        runner = self.root / "nextbss/server/managed-design-release.mjs"
        runner.parent.mkdir(parents=True)
        runner.write_text("// synthetic runner")
        payload = DesignBuild.model_validate({"buildId": uuid4(), "baseReleaseId": self.release,
            "baseManifestSha256": digest(self.release_manifest), "designVersion": "1.9", "sources": {}})
        observed = []
        async def process(binary, runner, scratch, timeout, request, environment):
            self.assertTrue(Path(environment["SFDC_MANAGED_DESIGN_SOURCES"]).is_file())
            self.assertTrue((Path(environment["SFDC_MANAGED_RELEASE_ROOT"]) / self.release / "release.json").is_file())
            directory = Path(environment["SFDC_DESIGN_BUILD_OUTPUT"])
            value = b'{"synthetic":"verified-build-file"}'
            (directory / "catalog.json").write_bytes(value)
            manifest = encoded({"buildId": str(payload.buildId), "files": [
                {"path": "catalog.json", "sizeBytes": len(value), "sha256": digest(value)}]})
            (directory / "output-manifest.json").write_bytes(manifest)
            observed.append(directory)
            return encoded({"status": "VALIDATED", "buildId": str(payload.buildId), "releaseId": self.release,
                "manifestSha256": digest(self.release_manifest), "outputManifestSha256": digest(manifest),
                "checks": [{"label": "synthetic validation", "status": "passed"}], "compatible": True, "reproduced": True})
        with patch("sfdc_agent.managed_design.ManagedObjectStorage.configured", return_value=self.storage), \
                patch("sfdc_agent.managed_design.engine_root", return_value=self.root), \
                patch("sfdc_agent.managed_design._runner_configuration", return_value=("node", runner, 900)), \
                patch("sfdc_agent.managed_design._run_process", process):
            self.assertEqual((await run_build(payload)).status, "VALIDATED")
        self.assertEqual(self.client.writes[-1], self.storage.object_key("release-ingress", str(payload.buildId) + "/output-manifest.json"))
        self.assertFalse(observed[0].exists())

    async def test_analysis_publishes_only_after_existing_verifier_accepts_output(self):
        from sfdc_agent.managed_execution import ExecutionRequest, _execute
        payload = ExecutionRequest.model_validate({"jobId": uuid4(), "attempt": 1,
            "executionScope": {"tenantId": self.archive.tenantId, "workspaceId": self.archive.workspaceId},
            "timeoutSeconds": 60, "deadlineEpochSeconds": int(time()) + 61, "kind": "SCREEN", "engine": "NOVA",
            "sourceReference": "https://www.figma.com/design/ApprovedFile?node-id=1-2", "designReleaseId": self.release,
            "designManifestSha256": digest(self.release_manifest), "promptReleaseId": "prompts-v1", "archive": vars(self.archive)})
        runner = self.root / "runner.mjs"
        runner.write_text("// synthetic runner")
        verified, observed = [], []
        @contextmanager
        def prompts(bundle):
            yield []
        @asynccontextmanager
        async def progress(*args):
            yield SimpleNamespace(auth_error=None)
        async def process(binary, runner, scratch, timeout, request, environment):
            directory = Path(environment["SFDC_ANALYSIS_OUTPUT"])
            view = b'{}'
            (directory / "public-view.json").write_bytes(view)
            manifest = encoded({"view": {"path": "public-view.json", "sizeBytes": len(view), "sha256": digest(view)}, "assets": []})
            (directory / "manifest.json").write_bytes(manifest)
            observed.append(directory)
            return encoded({"status": "REVIEW_REQUIRED", "title": "synthetic", "artifacts": [], "summary": {"title": "synthetic"},
                "checkpoint": {"id": "synthetic"}, "analysisView": {"protocolVersion": 1, "manifestSha256": digest(manifest)}})
        def verify(*args):
            self.assertFalse(self.client.writes)
            verified.append(True)
        with patch("sfdc_agent.managed_execution.ManagedObjectStorage.configured", return_value=self.storage), \
                patch("sfdc_agent.managed_execution.engine_root", return_value=self.root), \
                patch("sfdc_agent.managed_execution._runner_configuration", return_value=("node", runner, 90)), \
                patch("sfdc_agent.managed_execution._run_process", process), \
                patch("sfdc_agent.managed_execution.execution_prompts", prompts), \
                patch("sfdc_agent.managed_execution.execution_progress", progress), \
                patch("sfdc_agent.managed_execution.verify_analysis_output", verify):
            self.assertEqual((await _execute(payload)).status, "REVIEW_REQUIRED")
        self.assertEqual(verified, [True])
        self.assertEqual(self.client.writes[-1], self.storage.object_key("release-ingress", f"analysis/{payload.jobId}/1/manifest.json"))
        self.assertFalse(observed[0].exists())

    async def test_source_revision_materializes_its_pinned_release_and_archive(self):
        import base64
        from sfdc_agent.managed_source_revisions import RevisionRequest, run_revision
        runner = self.root / 'nextbss/server/managed-source-revision.mjs'
        runner.parent.mkdir(parents=True)
        runner.write_text('// synthetic runner')
        library = {'schemaVersion': 1, 'family': 'NOVA_PUBLIC', 'libraryVersion': '1.0.0',
            **{key: 'a'*64 for key in ('descriptorSha256', 'contractSha256', 'moduleManifestSha256', 'tokenCssSha256')}}
        payload = RevisionRequest.model_validate({'revisionId': uuid4(), 'parentResultId': uuid4(), 'parentDigest': 'a'*64,
            'adjustmentKind': 'SURFACE', 'producerKind': 'DETERMINISTIC_SOURCE_ADJUSTMENT', 'modelCalls': 0,
            'executionScope': {'tenantId': self.archive.tenantId, 'workspaceId': self.archive.workspaceId},
            'kind': 'SCREEN', 'engine': 'NOVA', 'sourceReference': 'https://www.figma.com/design/ApprovedFile?node-id=1-2',
            'pins': {'design': {'sourceId': self.release, 'manifestSha256': digest(self.release_manifest)},
                     'prompt': {'sourceId': 'prompts-v1', 'manifestSha256': 'a'*64},
                     'maxConcurrentJobs': 1, 'timeoutSeconds': 60}, 'view': {}, 'metadata': {'componentLibrary': library},
            'analysisViewId': uuid4(), 'analysisPayloadSha256': 'b'*64, 'analysisView': {},
            'analysisCheckpoint': {'id': uuid4(), 'digest': digest(b'{}'), 'bytes': base64.b64encode(b'{}').decode()},
            'archive': vars(self.archive), 'artifacts': [{'name': 'lwc/fixture/fixture.css', 'mediaType': 'application/octet-stream',
                'sha256': digest(b':host{}'), 'base64': base64.b64encode(b':host{}').decode()}]})
        observed = []
        async def process(binary, runner, scratch, timeout, request, environment, **kwargs):
            source = Path(environment['SFDC_MANAGED_SOURCE_ROOT']) / self.archive_key
            self.assertEqual(source.read_bytes(), self.archive_bytes)
            self.assertEqual((Path(environment['SFDC_MANAGED_RELEASE_ROOT']) / self.release / 'release.json').read_bytes(), self.release_manifest)
            self.assertLessEqual(timeout, 60)
            observed.append(source)
            return encoded({'status': 'UNCHANGED', 'revisionId': str(payload.revisionId), 'parentResultId': str(payload.parentResultId),
                'parentDigest': payload.parentDigest, 'producerKind': payload.producerKind, 'modelCalls': 0,
                'ruleDigest': 'a'*64, 'artifacts': []})
        with patch('sfdc_agent.managed_source_revisions.ManagedObjectStorage.configured', return_value=self.storage), \
                patch('sfdc_agent.managed_source_revisions.engine_root', return_value=self.root), \
                patch('sfdc_agent.managed_source_revisions._runner_configuration', return_value=('node', runner, 90)), \
                patch('sfdc_agent.managed_source_revisions._run_process', process):
            self.assertEqual((await run_revision(payload)).status, 'UNCHANGED')
        self.assertFalse(observed[0].exists())

    async def test_cancellation_waits_for_owned_transfer_before_workspace_can_be_removed(self):
        started, release = Event(), Event()
        def transfer():
            started.set()
            release.wait(timeout=5)
        task = asyncio.create_task(storage_io(transfer))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
