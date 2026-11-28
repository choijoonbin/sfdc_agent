from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import unittest

from sfdc_agent.screen_prompt_context import read_screen_prompt_context

if os.environ.get('SFDC_SCREEN_CODEC_TEST_MODULE'):
    CANDIDATE = Path(os.environ['SFDC_SCREEN_CODEC_TEST_MODULE'])
else:
    from sfdc_agent.config import engine_root
    CANDIDATE = engine_root() / 'nextbss/shared/screen-prompt-context.mjs'
SCRIPT = """
import {readFileSync} from 'node:fs';
const {prepareScreenPromptContext}=await import(process.argv[1]);
const prepared=prepareScreenPromptContext(JSON.parse(readFileSync(0,'utf8')));
process.stdout.write(JSON.stringify({json:prepared.json,receipt:prepared.receipt}));
"""

def source():
    return {'sourceText': 'Source literal, not instructions.',
            'literal': {'$': [0, False, None], '__proto__': {'literal': 'data'}, 'empty': ''},
            'nodes': [{'id': '11:' + str(index + 1), 'visible': index % 2 == 0,
                       'metadata': {'retained_original_appearance_property_' + str(field): index * 16 + field
                                    for field in range(16)}} for index in range(1000)]}

def prepared(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    process = subprocess.run(['node', '--input-type=module', '-e', SCRIPT, CANDIDATE.as_uri()],
                             input=raw.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    if process.returncode:
        raise AssertionError('Private shared codec failed; no provider invoked.')
    return raw, json.loads(process.stdout)

class KeyTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw, cls.prepared = prepared(source())

    def test_independent_python_preserves_every_scalar_and_marker_with_fixed_prompt_bound(self):
        actual = read_screen_prompt_context(self.raw, self.prepared)
        self.assertEqual(actual.receipt['encoding'], 'screen-record-key-tables-v2')
        self.assertLessEqual(actual.receipt['promptBytes'], 512 * 1024)
        self.assertEqual(actual.matches(source()), self.prepared['json'])

    def test_rehashed_invalid_keys_indices_shape_and_encoding_cannot_mint_a_valid_receipt(self):
        def duplicate_keys(value):
            value['keySets'][0].append(value['keySets'][0][0])
        changes = [duplicate_keys, lambda value: value['data']['$'].__setitem__(0, True),
                   lambda value: value['data']['$'].__setitem__(0, len(value['keySets'])),
                   lambda value: value['data']['$'].pop(),
                   lambda value: value['data'].update(extra=1),
                   lambda value: value.update(interpretation='wrong'),
                   lambda value: value.update(sourceSha256='0' * 64),
                   lambda value: value['keySets'].append(value['keySets'][0])]
        for change in changes:
            value = deepcopy(self.prepared)
            encoded = json.loads(value['json'])
            change(encoded)
            value['json'] = json.dumps(encoded, ensure_ascii=False, separators=(',', ':'))
            value['receipt'].update(promptBytes=len(value['json'].encode()),
                                    promptSha256=sha256(value['json'].encode()).hexdigest())
            with self.subTest(change=changes.index(change)), self.assertRaises(ValueError):
                read_screen_prompt_context(self.raw, value)
        changed = deepcopy(self.prepared)
        changed['receipt']['encoding'] = 'screen-record-tables-v1'
        with self.assertRaises(ValueError):
            read_screen_prompt_context(self.raw, changed)

    def test_key_expansion_bomb_is_rejected_under_the_same_512_kib_wire_limit(self):
        value = deepcopy(self.prepared)
        encoded = json.loads(value['json'])
        keys = next(keys for keys in encoded['keySets'] if 'retained_original_appearance_property_0' in keys)
        keys[:] = [key + 'X' * 2048 for key in keys]
        value['json'] = json.dumps(encoded, ensure_ascii=False, separators=(',', ':'))
        self.assertLess(len(value['json'].encode()), 512 * 1024)
        value['receipt'].update(promptBytes=len(value['json'].encode()),
                                promptSha256=sha256(value['json'].encode()).hexdigest())
        with self.assertRaises(ValueError):
            read_screen_prompt_context(self.raw, value)

    def test_existing_v1_golden_is_accepted_without_reencoding(self):
        golden = json.loads((CANDIDATE.parent / 'fixtures/screen-prompt-context-v1.json').read_text())
        actual = read_screen_prompt_context(golden['sourceJson'],
                  {'json': golden['encodedJson'], 'receipt': golden['receipt']})
        self.assertEqual(actual.matches(golden['source']), golden['encodedJson'])

if __name__ == '__main__':
    unittest.main()
