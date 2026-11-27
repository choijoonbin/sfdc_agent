import json
import math
import unittest

from sfdc_agent.strict_json import StrictJSONDecoder, decode_json, encode_json, loads


class StrictJsonTests(unittest.TestCase):
    def test_compact_utf8_wire_bytes_and_scalar_values_are_preserved(self):
        value = {'문구': '한글', 'values': [None, True, False, 3, -1.25, {'value': 'NaN'}]}
        expected = '{"문구":"한글","values":[null,true,false,3,-1.25,{"value":"NaN"}]}'.encode()
        self.assertEqual(encode_json(value), expected)
        self.assertEqual(decode_json(expected), value)
        self.assertEqual(decode_json(bytearray(expected)), value)
        for document, expected_value in [('null', None), ('false', False), ('"text"', 'text'), ('3', 3), ('[]', [])]:
            with self.subTest(document=document):
                self.assertEqual(loads(document), expected_value)

    def test_duplicate_keys_are_rejected_at_every_level_after_unescaping(self):
        for document in ['{"id":1,"id":2}', '[{"id":1,"id":2}]', '{"a":{"id":1,"id":2}}',
                         '{"id":1,"i\\u0064":2}']:
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, r'^Repeated JSON key\.$'):
                loads(document)
        self.assertEqual(loads('{"first":{"id":1},"second":{"id":2}}'),
                         {'first': {'id': 1}, 'second': {'id': 2}})

    def test_named_nonfinite_constants_and_nonfinite_output_keep_their_error_policies(self):
        for token in ['NaN', 'Infinity', '-Infinity']:
            for document in [token, '[1,' + token + ']', '{"value":' + token + '}']:
                with self.subTest(document=document), self.assertRaisesRegex(ValueError, r'^Non-finite JSON number\.$'):
                    loads(document)
        for value in [math.nan, math.inf, -math.inf]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                encode_json({'value': value})
        # Preserve the existing stdlib number parsing rather than silently adding
        # a new overflow policy to callers that validate their own numeric fields.
        self.assertTrue(math.isinf(loads('1e400')))
        self.assertEqual(loads('{"value":"NaN"}'), {'value': 'NaN'})

    def test_json_bytes_detection_stays_separate_from_managed_utf8_decoding(self):
        document = '{"문구":"한글"}'
        expected = {'문구': '한글'}
        for encoding in ['utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be', 'utf-32', 'utf-32-le', 'utf-32-be']:
            for container in [bytes, bytearray]:
                encoded = container(document.encode(encoding))
                with self.subTest(encoding=encoding, container=container):
                    self.assertEqual(loads(encoded), expected)
                    if encoding == 'utf-8':
                        self.assertEqual(decode_json(encoded), expected)
                    else:
                        with self.assertRaises(ValueError):
                            decode_json(encoded)

    def test_raw_decoder_preserves_span_offsets_and_uses_the_same_hooks(self):
        document = 'prefix{"한글":[1,2]} suffix'
        decoder = StrictJSONDecoder()
        value, end = decoder.raw_decode(document, len('prefix'))
        self.assertEqual(value, {'한글': [1, 2]})
        self.assertEqual(document[len('prefix'):end], '{"한글":[1,2]}')
        for invalid in ['{"id":1,"id":2}', '{"value":Infinity}']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                decoder.raw_decode(invalid + ' suffix')

    def test_caller_error_factory_does_not_replace_ordinary_json_syntax_errors(self):
        for code in ['MANAGED_OBJECT_STORAGE_INVALID', 'SCREEN_ANALYSIS_CONTRACT_INVALID']:
            def invalid():
                return ValueError(code)
            for document in ['{"id":1,"id":2}', '{"value":NaN}', 'Infinity']:
                with self.subTest(code=code, document=document), self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    loads(document, error_factory=invalid)
            with self.assertRaisesRegex(ValueError, '^' + code + '$'):
                StrictJSONDecoder(error_factory=invalid).raw_decode('{"id":1,"id":2}')
            self.assertEqual(loads('{"id":1}', error_factory=invalid), {'id': 1})
            with self.assertRaises(json.JSONDecodeError):
                loads('{', error_factory=invalid)
        with self.assertRaises(UnicodeDecodeError):
            decode_json(b'"\xff"')

    def test_existing_managed_execution_json_facade_keeps_the_wire_contract(self):
        from sfdc_agent.managed_execution import _decode_json, _encode_json
        self.assertIs(_decode_json, decode_json)
        self.assertIs(_encode_json, encode_json)
        self.assertEqual(_decode_json(_encode_json({'문구': '한글'})), {'문구': '한글'})
        with self.assertRaisesRegex(ValueError, r'^Repeated JSON key\.$'):
            _decode_json(b'{"id":1,"id":2}')
