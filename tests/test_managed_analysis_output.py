import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import shutil
import subprocess
import tempfile
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import zlib

from sfdc_agent.managed_analysis_output import output_directory, verify
from sfdc_agent.managed_execution import ExecutionRequest
from sfdc_agent.config import engine_root


def encoded(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def png():
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\0\0\0')) + chunk(b'IEND', b'')


class ManagedAnalysisOutputTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {'SFDC_MANAGED_RELEASE_INGRESS': str(self.root)}))
        (self.root / 'managed-runtime.json').write_bytes(encoded({'sha256': 'b' * 64}))
        self.payload = ExecutionRequest.model_validate({'jobId': str(uuid4()), 'attempt': 2147483647,
            'executionScope': {'tenantId': str(uuid4()), 'workspaceId': str(uuid4())}, 'timeoutSeconds': 60,
            'deadlineEpochSeconds': int(time()) + 61, 'kind': 'STORY', 'engine': 'NOVA',
            'sourceReference': 'https://www.figma.com/design/ApprovedFile?node-id=1-2',
            'designReleaseId': 'nextbss-1.9-' + 'a' * 64, 'promptReleaseId': 'prompts-v1'})
        self.directory = output_directory(self.payload)
        self.analysis_id = str(uuid4())
        url = f'/api/nextbss/stories/analyses/{self.analysis_id}/previews/'
        self.view = {'id': self.analysis_id, 'source': {'fileKey': 'ApprovedFile', 'version': 'v123'},
                     'board': {'id': '1:2', 'previewUrl': url + '1:2.svg'},
                     'workflows': [{'id': 'flow', 'screens': [{'id': '1:3', 'previewUrl': url + '1:3.svg'}]}]}
        self.raw = encoded(self.view)
        self.result = SimpleNamespace(checkpoint={'id': self.analysis_id, 'digest': digest(self.raw), 'bytes': base64.b64encode(self.raw).decode()},
                                      analysisView=SimpleNamespace(manifestSha256=''))
        self.manifest = {'protocolVersion': 1, 'serializerVersion': 1, 'jobId': str(self.payload.jobId), 'attempt': self.payload.attempt,
            'tenantId': str(self.payload.executionScope.tenantId), 'workspaceId': str(self.payload.executionScope.workspaceId),
            'kind': 'STORY', 'engine': 'NOVA',
            'producerRuntimeSha256': 'b' * 64, 'analysisId': self.analysis_id, 'checkpointSha256': digest(self.raw),
            'source': {'fileKey': 'ApprovedFile', 'version': 'v123'},
            'view': {}, 'assets': [], 'unavailable': [{'nodeId': '1:2', 'code': 'SOURCE_PREVIEW_TOO_LARGE'},
                                                   {'nodeId': '1:3', 'code': 'SOURCE_NODE_UNAVAILABLE'}]}

    def save(self):
        view = encoded(self.view)
        (self.directory / 'public-view.json').write_bytes(view)
        self.manifest['view'] = {'path': 'public-view.json', 'sizeBytes': len(view), 'sha256': digest(view)}
        manifest = encoded(self.manifest)
        (self.directory / 'manifest.json').write_bytes(manifest)
        self.result.analysisView.manifestSha256 = digest(manifest)

    def check(self):
        self.save()
        verify(self.directory, self.payload, self.result, self.root)

    def test_explicit_partial_view_and_exact_ascii_remainder_are_durable(self):
        self.check()
        self.manifest['unavailable'] = []
        self.manifest['unavailableRemainder'] = {'code': 'SOURCE_PREVIEW_BUDGET_EXCEEDED', 'nodeCount': 2,
                                              'nodeSetSha256': digest(b'1:2\n1:3\n')}
        self.check()
        self.manifest['unavailableRemainder']['nodeSetSha256'] = digest(b'1:3\n1:2\n')
        self.save()
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)

    def test_actual_png_and_svg_bytes_are_accepted_without_filename_mime_fabrication(self):
        (self.directory / 'blobs').mkdir()
        values = [('1:2', 'image/png', png(), 'OFFICIAL_FIGMA_EXPORT'),
                  ('1:3', 'image/svg+xml', b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>', 'LOCAL_RENDER')]
        self.manifest['unavailable'] = []
        for node, media, data, provenance in values:
            sha = digest(data)
            (self.directory / 'blobs' / sha).write_bytes(data)
            self.manifest['assets'].append({'nodeId': node, 'mediaType': media, 'path': 'blobs/' + sha,
                'sha256': sha, 'sizeBytes': len(data), 'rendererVersion': 1, 'provenance': provenance, 'width': 1, 'height': 1})
        self.check()
        self.manifest['assets'][0]['mediaType'] = 'image/svg+xml'
        self.save()
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)

    def test_actual_node_exporter_and_python_validator_share_the_wire_contract(self):
        module = engine_root() / 'nextbss/server/managed-analysis-output.mjs'
        if not module.is_file():
            self.skipTest('Packaged managed Node producer is not installed in this Agent-only checkout.')
        document = {'id': self.analysis_id, 'source': {'fileKey': 'ApprovedFile', 'version': 'v123'},
                    'board': {'id': '1:2'}, 'workflows': []}
        raw = encoded(document)
        checkpoint = {'id': self.analysis_id, 'digest': digest(raw), 'bytes': base64.b64encode(raw).decode()}
        script = self.root / 'export.mjs'
        script.write_text("import {readFileSync} from 'node:fs'; import {exportManagedAnalysis} from " + json.dumps(module.as_uri()) + ";\n"
            "const input=JSON.parse(readFileSync(0,'utf8')); input.readPreview=async()=>({bytes:Buffer.from(input.png,'base64'),mediaType:'image/png'});\n"
            "process.stdout.write(JSON.stringify(await exportManagedAnalysis(input)));\n")
        receipt = subprocess.run([shutil.which('node'), str(script)], input=encoded({'request': self.payload.model_dump(mode='json'),
            'directory': str(self.directory), 'document': document, 'checkpoint': checkpoint,
            'producerRuntimeSha256': 'b' * 64, 'png': base64.b64encode(png()).decode()}), capture_output=True,
            check=True, timeout=10, env={'PATH': os.environ.get('PATH', '')})
        result = SimpleNamespace(checkpoint=checkpoint, analysisView=SimpleNamespace(**json.loads(receipt.stdout)))
        verify(self.directory, self.payload, result, self.root)
        manifest = json.loads((self.directory / 'manifest.json').read_bytes())
        self.assertEqual(manifest['assets'][0]['mediaType'], 'image/png')
        self.assertEqual(manifest['assets'][0]['sha256'], digest(png()))

    def test_owner_scope_attempt_raw_analysis_and_runtime_pins_are_exact(self):
        for field, invalid in [('tenantId', str(uuid4())), ('attempt', 1), ('checkpointSha256', 'c' * 64),
                               ('analysisId', str(uuid4())), ('producerRuntimeSha256', 'a' * 64), ('protocolVersion', True)]:
            with self.subTest(field=field):
                original = self.manifest[field]; self.manifest[field] = invalid; self.save()
                with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)
                self.manifest[field] = original

    def test_unknown_missing_duplicate_or_foreign_analysis_node_never_becomes_ready(self):
        original = list(self.manifest['unavailable'])
        for entries in [original[:1], original + [original[0]], [{'nodeId': '9:9', 'code': 'SOURCE_NODE_UNAVAILABLE'}]]:
            self.manifest['unavailable'] = entries; self.save()
            with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)
        self.manifest['unavailable'] = original
        self.view['board']['previewUrl'] = self.view['board']['previewUrl'].replace(self.analysis_id, str(uuid4()))
        self.save()
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)

    def test_no_private_fields_symlinks_hardlinks_unlisted_files_or_changed_bytes(self):
        self.view['source']['access_token'] = 'must-never-be-public'
        self.save()
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)
        del self.view['source']['access_token']; self.save()
        path = self.directory / 'public-view.json'; data = path.read_bytes(); path.unlink()
        target = self.root / 'outside'; target.write_bytes(data); path.symlink_to(target)
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)
        path.unlink(); os.link(target, path)
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)
        path.unlink(); self.save()
        extra = self.directory / 'unlisted'; extra.write_text('forbidden')
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)
        extra.unlink(); path.write_bytes(data + b' ')
        with self.assertRaises(ValueError): verify(self.directory, self.payload, self.result, self.root)

    def test_output_is_created_once_and_archive_scope_must_match_request(self):
        with self.assertRaises(Exception) as exists: output_directory(self.payload)
        self.assertEqual(exists.exception.status_code, 409)
        raw = self.payload.model_dump(mode='json')
        raw['archive'] = {'tenantId': str(uuid4()), 'workspaceId': raw['executionScope']['workspaceId'],
                          'fileKey': 'ApprovedFile', 'sha256': 'c' * 64}
        with self.assertRaises(ValueError): ExecutionRequest.model_validate(raw)


if __name__ == '__main__':
    unittest.main()
