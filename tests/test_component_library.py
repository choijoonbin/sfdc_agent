import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
import json
import tempfile
from time import time
from starlette.requests import Request
from pydantic import ValidationError
from sfdc_agent.component_library import ComponentLibrary
from sfdc_agent.kiro_screen_projection import project_screen_fields
from sfdc_agent.managed_execution import execute


class ComponentLibraryTests(unittest.TestCase):
    def value(self):
        return dict(schemaVersion=1, family='NOVA_PUBLIC', libraryVersion='1.0.0',
                    descriptorSha256='a' * 64, contractSha256='b' * 64,
                    moduleManifestSha256='c' * 64, tokenCssSha256='d' * 64)

    def test_complete_nova_pin_preserves_exact_digests(self):
        self.assertEqual(ComponentLibrary.model_validate(self.value()).model_dump(), self.value())

    def test_missing_or_unknown_nova_contract_is_rejected(self):
        for patch in [dict(contractSha256=None), dict(moduleManifestSha256=None),
                      dict(libraryVersion='2.0.0'), dict(schemaVersion=True), dict(untrustedPath='/tmp/source')]:
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                ComponentLibrary.model_validate({**self.value(), **patch})

    def test_legacy_does_not_fabricate_nova_hashes(self):
        old = {**self.value(), 'family': 'NEXTBSS_LEGACY', 'libraryVersion': None,
               'contractSha256': None, 'moduleManifestSha256': None}
        self.assertEqual(ComponentLibrary.model_validate(old).model_dump(), old)
        with self.assertRaises(ValidationError):
            ComponentLibrary.model_validate({**old, 'contractSha256': 'b' * 64})


class PinnedProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_projection_passes_only_runtime_paths_and_locale_without_parent_authority(self):
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (b'{"projected":true}', b'')
        fields = {'nodeId': '10:1', 'designReleaseId': 'nextbss-1.9-' + 'e' * 64,
                  'sourceContext': {'components': [{}]}, 'componentLibrary': ComponentLibraryTests().value()}
        original = json.loads(json.dumps(fields))
        environment = {'PATH': '/selected/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C', 'LC_CTYPE': 'UTF-8',
                       'SFDC_MANAGED_ENGINE_ROOT': '/selected/engine', 'SFDC_MANAGED_RELEASE_ROOT': '/selected/releases',
                       'SFDC_MANAGED_EXECUTION_NODE': '/selected/node', 'SFDC_AGENT_EXECUTION_TOKEN': 'fixture-service-token',
                       'SFDC_MANAGED_PROMPT_CONTEXT': 'fixture-prompt-token', 'KIRO_API_KEY': 'fixture-provider-key',
                       'OPENAI_API_KEY': 'fixture-openai-key', 'FIGMA_ACCESS_TOKEN': 'fixture-source-token',
                       'SFDC_AGENT_AZURE_CLIENT_SECRET': 'fixture-azure-secret', 'AWS_ACCESS_KEY_ID': 'fixture-aws-key',
                       'AWS_SECRET_ACCESS_KEY': 'fixture-aws-secret', 'AWS_SESSION_TOKEN': 'fixture-aws-session',
                       'AWS_PROFILE': 'foreign-profile', 'AWS_REGION': 'foreign-region',
                       'SFDC_UI_ROOT': '/foreign/ui', 'SFDC_AGENT_ROOT': '/foreign/agent',
                       'SFDC_MANAGED_SOURCE_ROOT': '/foreign/source', 'SFDC_MANAGED_FIGMA_SCOPE': 'foreign/scope',
                       'NOVA_LOCAL_WORKSPACE': '/foreign/workspace', 'SFDC_NOVA_PROJECT_ROOT': '/foreign/nova',
                       'HOME': '/foreign/home', 'XDG_CONFIG_HOME': '/foreign/config', 'TMPDIR': '/foreign/scratch',
                       'NODE_OPTIONS': '--require /foreign/preload.js', 'NODE_PATH': '/foreign/modules',
                       'HTTPS_PROXY': 'http://foreign-proxy', 'NODE_EXTRA_CA_CERTS': '/foreign/cert.pem'}
        with patch.dict('os.environ', environment, clear=True), patch(
                'sfdc_agent.kiro_screen_projection.engine_root', return_value=Path('/selected/engine')), patch(
                'sfdc_agent.kiro_screen_projection.asyncio.create_subprocess_exec', return_value=process) as spawn:
            projected = await project_screen_fields('screen.generate', fields)
        self.assertTrue(projected['sourceContext']['projected'])
        self.assertEqual(fields, original)
        self.assertEqual(spawn.await_args.args[0], '/selected/node')
        self.assertEqual(spawn.await_args.args[2:], ('/selected/engine', '/selected/releases'))
        self.assertEqual(spawn.await_args.kwargs.get('env'),
                         {'PATH': '/selected/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C', 'LC_CTYPE': 'UTF-8'})
        self.assertEqual(json.loads(process.communicate.await_args.args[0]), {'fields': original})

    async def test_nonmanaged_projection_retains_only_its_explicit_release_store_fallback(self):
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (b'{"projected":true}', b'')
        fields = {'sourceContext': {'components': [{}]}}
        with patch.dict('os.environ', {'PATH': '/selected/bin', 'SFDC_MANAGED_RELEASE_ROOT': '/selected/releases',
                                     'SFDC_UI_ROOT': '/foreign/ui', 'NOVA_LOCAL_WORKSPACE': '/foreign/workspace',
                                     'KIRO_API_KEY': 'fixture-provider-key'}, clear=True), patch(
                'sfdc_agent.kiro_screen_projection.engine_root', return_value=Path('/selected/engine')), patch(
                'sfdc_agent.kiro_screen_projection.asyncio.create_subprocess_exec', return_value=process) as spawn:
            projected = await project_screen_fields('screen.review-plan', fields)
        self.assertTrue(projected['sourceContext']['projected'])
        self.assertEqual(spawn.await_args.args[2:], ('/selected/engine', ''))
        self.assertEqual(spawn.await_args.kwargs.get('env'),
                         {'PATH': '/selected/bin', 'SFDC_MANAGED_RELEASE_ROOT': '/selected/releases'})

    async def test_repair_projection_keeps_its_nested_protocol_with_no_unrequested_environment(self):
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (b'{"projected":true}', b'')
        evidence = {'sourceContext': {'inputControls': [{}]}}
        fields = {'evidence': evidence, 'previousDraft': 'fixture draft', 'diagnostic': 'fixture diagnostic'}
        original = json.loads(json.dumps(fields))
        with patch.dict('os.environ', {}, clear=True), patch(
                'sfdc_agent.kiro_screen_projection.engine_root', return_value=Path('/selected/engine')), patch(
                'sfdc_agent.kiro_screen_projection.asyncio.create_subprocess_exec', return_value=process) as spawn:
            projected = await project_screen_fields('screen.repair', fields)
        self.assertEqual(spawn.await_args.args[0], 'node')
        self.assertEqual(spawn.await_args.kwargs.get('env'), {})
        self.assertEqual(json.loads(process.communicate.await_args.args[0]), {'fields': evidence})
        self.assertEqual(projected, {**original, 'evidence': {'sourceContext': {'inputControls': [{}], 'projected': True}}})
        self.assertEqual(fields, original)

    async def test_analysis_projection_uses_its_explicit_release_not_the_active_pointer(self):
        release_id = 'nextbss-1.9-' + 'e' * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = root / 'nextbss/engine'
            server = root / 'nextbss/server'
            engine.mkdir(parents=True)
            server.mkdir(parents=True)
            (engine / 'generate.mjs').write_text('export async function readNextBssStandard(options) {'
                'if (options.releaseId !== ' + json.dumps(release_id) + ' || !options.releaseRoot) throw Error("active pointer forbidden");'
                'return { standard:{version:"1.9"}, componentLibrary:{tuple:{family:"NOVA_PUBLIC"},contract:{selected:true}} };}')
            (engine / 'kiro-source-snapshot.mjs').write_text('export const rebuildKiroSourceSnapshot = () => ({});')
            (engine / 'kiro-button-evidence.mjs').write_text('export const extractKiroButtonEvidence = () => [];')
            (engine / 'kiro-form-projection.mjs').write_text('export const projectKiroFormEvidence = () => ({});')
            (server / 'kiro-nova-contract.mjs').write_text('export const projectKiroNovaContext = (value, contract) => ({...value, selected:contract.selected});')
            fields = {'nodeId': '10:1', 'version': '1.9', 'designReleaseId': release_id,
                      'sourceContext': {'nodes': [{'id': '10:1'}], 'components': [{}]},
                      'componentLibrary': ComponentLibraryTests().value()}
            with patch.dict('os.environ', {'SFDC_MANAGED_ENGINE_ROOT': directory, 'SFDC_MANAGED_RELEASE_ROOT': str(root / 'releases')}):
                projected = await project_screen_fields('screen.review-plan', fields)
                self.assertTrue(projected['sourceContext']['selected'])
                with self.assertRaisesRegex(ValueError, 'pinned component-library'):
                    await project_screen_fields('screen.review-plan', {key: value for key, value in fields.items() if key != 'designReleaseId'})

    async def test_managed_projection_failure_cannot_silently_downgrade(self):
        process = AsyncMock()
        process.returncode = 1
        process.communicate.return_value = (b'', b'')
        fields = {'sourceContext': {'components': [{}]}, 'componentLibrary': ComponentLibraryTests().value()}
        with patch('sfdc_agent.kiro_screen_projection.asyncio.create_subprocess_exec', return_value=process):
            with self.assertRaisesRegex(ValueError, 'pinned component-library'):
                await project_screen_fields('screen.generate', fields)

    async def test_unversioned_legacy_projection_keeps_its_explicit_compatibility(self):
        process = AsyncMock()
        process.returncode = 1
        process.communicate.return_value = (b'', b'')
        fields = {'sourceContext': {'components': [{}]}}
        with patch('sfdc_agent.kiro_screen_projection.asyncio.create_subprocess_exec', return_value=process):
            self.assertIs(await project_screen_fields('screen.generate', fields), fields)


class RuntimeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_family_is_explicit_before_any_execution(self):
        pin = ComponentLibraryTests().value()
        pin.update(family='NEXTBSS_LEGACY', libraryVersion=None, contractSha256=None, moduleManifestSha256=None)
        response = await self.request(pin, {'schemaVersion': 1, 'generationFamilies': [{'family': 'NOVA_PUBLIC', 'libraryVersion': '1.0.0'}]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body), {'code': 'RELEASE_RUNTIME_INCOMPATIBLE'})

    async def test_missing_capability_fails_explicitly_without_a_child(self):
        response = await self.request(ComponentLibraryTests().value(), None)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body), {'code': 'RUNTIME_CAPABILITIES_UNAVAILABLE'})

    async def request(self, pin, capability):
        payload = dict(jobId='841548c8-deef-4854-a90b-79ff582e20ac', attempt=1,
                       executionScope={'tenantId': 'aa000000-0000-4000-8000-000000000001', 'workspaceId': 'ad000000-0000-4000-8000-000000000001'},
                       timeoutSeconds=60, deadlineEpochSeconds=int(time()) + 60, kind='SCREEN', engine='NOVA',
                       sourceReference='figma-local://local' + 'a' * 32 + '/source?node-id=10-1&page-id=10-0',
                       designReleaseId='nextbss-1.9-' + 'a' * 64, promptReleaseId='prompts-v1', componentLibrary=pin)
        async def receive():
            return {'type': 'http.request', 'body': json.dumps(payload).encode(), 'more_body': False}
        request = Request({'type': 'http', 'method': 'POST', 'path': '/internal/v1/executions',
                           'headers': [(b'authorization', b'Bearer fixture')], 'server': ('127.0.0.1', 9999)}, receive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if capability is not None:
                path = root / 'nextbss/engine/managed-runtime-capabilities.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(capability))
            execution = AsyncMock(side_effect=AssertionError('No execution before compatibility'))
            with patch.dict('os.environ', {'SFDC_AGENT_EXECUTION_TOKEN': 'fixture'}), \
                    patch('sfdc_agent.managed_execution.engine_root', return_value=root), \
                    patch('sfdc_agent.managed_execution._runner_configuration', return_value=('node', root / 'runner.mjs', 540)), \
                    patch('sfdc_agent.managed_execution._execute', execution):
                response = await execute(request)
                execution.assert_not_called()
                return response
