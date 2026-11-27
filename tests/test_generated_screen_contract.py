import base64
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import unittest

from pydantic import ValidationError

from sfdc_agent.generated_screen_contract import checked_contract, checked_field_aliases, verify_generated_screen_contract
from sfdc_agent.managed_contracts import ExecutionRequest, ExecutionResult


def contract(generic=True):
    return {
        'schemaVersion': 1, 'bundleName': 'nextBssScreenPublic',
        'generationKind': 'NOVA_SCREEN' if generic else 'KIRO_SCREEN', 'genericModelSupported': generic,
        'host': {'supportedTargets': ['lightning__AppPage'], 'recordContext': 'unsupported', 'flow': 'unsupported', 'quickAction': 'unsupported'},
        'statePolicy': 'local-default-controlled-opt-in' if generic else 'generator-owned', 'businessVerified': False,
        'fieldAliases': {'1:2': 'reference'} if generic else {},
        'fields': [{'key': 'reference', 'sourceNodeId': '1:2', 'type': 'string', 'initialValue': '',
                    'stableBusinessKey': True, 'identityScope': 'explicit-alias', 'alias': 'reference'}] if generic else [],
        'dataBindings': [], 'actions': [],
        'publicApi': {'properties': ['screenModel'] if generic else [],
                      'methods': ['getScreenSnapshot', 'validate', 'reportValidity', 'reset', 'requestAction'] if generic else [],
                      'events': ['screenchange', 'screenaction', 'screenreset'] if generic else [],
                      'eventPropagation': {'bubbles': False, 'composed': False}, 'localEvents': 'legacy-source-events-preserved'},
        'legacyApi': {'properties': [], 'methods': [], 'events': []},
    }


def result(value=None):
    value = value or contract()
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    checksum = sha256(raw).hexdigest()
    name = value['bundleName']
    def artifact(path, body):
        return {'name': path, 'mediaType': 'application/octet-stream', 'base64': base64.b64encode(body).decode()}
    return ExecutionResult.model_validate({
        'status': 'COMPLETED', 'title': 'Generated result',
        'summary': {'designCore': {'existing': 'provenance'}, 'generatedScreenContract': value, 'generatedScreenContractSha256': checksum},
        'artifacts': [artifact(f'contracts/{name}.json', raw),
                      artifact(f'lwc/{name}/{name}.js-meta.xml', b'<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata"><isExposed>true</isExposed><targets><target>lightning__AppPage</target></targets></LightningComponentBundle>'),
                      artifact(f'lwc/{name}/{name}.js', ('const generatedScreenContract = ' + json.dumps(value, separators=(',', ':')) + ';\n').encode()),
                      artifact('managed-result-meta.json', json.dumps({'generatedScreenContract': value, 'generatedScreenContractSha256': checksum}).encode())],
    })


class GeneratedScreenContractTests(unittest.TestCase):
    def test_valid_sidecar_summary_entry_and_existing_provenance_are_preserved(self):
        original = result()
        checked = verify_generated_screen_contract(original, required=True, expected_aliases={'1:2': 'reference'}, kind='SCREEN', engine='NOVA')
        self.assertEqual(checked.model_dump(), original.model_dump())
        self.assertEqual(checked.summary['designCore'], {'existing': 'provenance'})

    def test_tampered_digest_summary_runtime_and_wrong_metadata_context_are_rejected(self):
        for mutate in (
            lambda value: value['summary'].update(generatedScreenContractSha256='f' * 64),
            lambda value: value['summary']['generatedScreenContract']['fields'][0].update(initialValue='tampered'),
            lambda value: value['artifacts'][1].update(base64=base64.b64encode(base64.b64decode(value['artifacts'][1]['base64']).replace(b'lightning__AppPage', b'lightning__RecordPage')).decode()),
            lambda value: value['artifacts'][2].update(base64=base64.b64encode(b'export default class Inert {}').decode()),
            lambda value: value['artifacts'].pop(0),
            lambda value: value['artifacts'][0].update(name='contracts/other.json'),
        ):
            value = result().model_dump()
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                verify_generated_screen_contract(ExecutionResult.model_validate(value), required=True)

    def test_unsupported_kiro_contract_is_honest_and_alias_or_engine_mismatch_is_rejected(self):
        value = result(contract(False))
        self.assertFalse(verify_generated_screen_contract(value, required=True, kind='SCREEN', engine='KIRO').summary['generatedScreenContract']['genericModelSupported'])
        with self.assertRaises(ValueError):
            verify_generated_screen_contract(value, expected_aliases={'1:2': 'reference'})
        with self.assertRaises(ValueError):
            verify_generated_screen_contract(value, kind='SCREEN', engine='NOVA')
        invalid = contract(False)
        invalid['publicApi']['properties'] = ['screenModel']
        with self.assertRaises(ValueError):
            checked_contract(invalid)

    def test_legacy_results_remain_readable_but_new_required_interfaces_cannot_be_omitted(self):
        legacy = ExecutionResult.model_validate({'status': 'COMPLETED', 'title': 'Old result', 'artifacts': []})
        self.assertIs(verify_generated_screen_contract(legacy), legacy)
        with self.assertRaises(ValueError):
            verify_generated_screen_contract(legacy, required=True)

    def test_explicit_alias_validation_rejects_duplicates_unsafe_names_and_missing_fields(self):
        for aliases in ({'1:2': 'same', '1:3': 'same'}, {'1:2': 'constructor'}, {'__proto__': 'field'}, {'1:2': 'has space'}):
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                checked_field_aliases(aliases)
        invalid = contract()
        invalid['fieldAliases'] = {'missing': 'reference'}
        with self.assertRaises(ValueError):
            checked_contract(invalid)

    def test_real_node_generator_contract_passes_agent_verification(self):
        ui = Path(__file__).resolve().parents[2] / 'SFDC_Ui'
        script = """
import {renderBundle} from './nextbss/engine/render.mjs';
import {canonicalScreenContract,generatedScreenContractSha256} from './nextbss/engine/generated-screen-contract.mjs';
let contract;
const name='nextBssScreenPythonContract';
const files=renderBundle({name,novaStandard:true,root:{tag:'main',nodeId:'1:1',children:[{tag:'c-nova-text-field',nodeId:'1:2',attributes:{value:'123'}}]},fieldAliases:{'1:2':'reference'},onContract:c=>{contract=c;}});
const checksum=generatedScreenContractSha256(contract);
const artifacts=[...files].map(([file,source])=>({name:`lwc/${name}/${file}`,mediaType:'application/octet-stream',base64:Buffer.from(source).toString('base64')}));
artifacts.push({name:`contracts/${name}.json`,mediaType:'application/octet-stream',base64:Buffer.from(canonicalScreenContract(contract)).toString('base64')});
process.stdout.write(JSON.stringify({status:'COMPLETED',title:'Real Node result',artifacts,summary:{generatedScreenContract:contract,generatedScreenContractSha256:checksum}}));
"""
        completed = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ui, capture_output=True, text=True, timeout=15, check=True)
        value = ExecutionResult.model_validate_json(completed.stdout)
        self.assertEqual(verify_generated_screen_contract(value, required=True).summary['generatedScreenContract']['fields'][0]['initialValue'], '123')


class GenerationOptionsTests(unittest.TestCase):
    def payload(self):
        return {'jobId': '8b9ed4d2-ffb7-4fd5-9e14-1d4b1ec8b42d', 'attempt': 1,
                'executionScope': {'tenantId': 'aa000000-0000-4000-8000-000000000001', 'workspaceId': 'ad000000-0000-4000-8000-000000000001'},
                'timeoutSeconds': 10, 'deadlineEpochSeconds': 123, 'kind': 'SCREEN', 'engine': 'NOVA',
                'sourceReference': 'https://www.figma.com/design/FrameDocument', 'designReleaseId': 'release', 'promptReleaseId': 'prompts-v1'}

    def test_generation_options_are_preserved_and_optional_context_is_legacy_compatible(self):
        payload = self.payload()
        parsed = ExecutionRequest.model_validate({**payload, 'fieldAliases': {'1:2': 'reference'}, 'targetContext': 'lightning__AppPage'})
        self.assertEqual(parsed.model_dump()['fieldAliases'], {'1:2': 'reference'})
        self.assertIsNone(ExecutionRequest.model_validate({**payload, 'targetContext': None}).targetContext)
        for changes in ({'targetContext': 'lightning__RecordPage'}, {'fieldAliases': {'1:2': 'same', '1:3': 'same'}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ExecutionRequest.model_validate({**payload, **changes})
