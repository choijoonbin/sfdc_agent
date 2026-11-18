import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('unit_contract_preflight', Path(__file__).resolve().parents[1] / 'scripts/verify-unit-contracts.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class RuntimePreflightTests(unittest.TestCase):
    def test_discovery_supports_the_existing_non_package_test_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory) / 'tests'
            tests.mkdir()
            (tests / 'test_standalone_fixture.py').write_text('import unittest\nclass Fixture(unittest.TestCase):\n def test_ok(self): pass\n')
            self.assertEqual(module.load_suite(directory).countTestCases(), 1)

    def fixture(self, directory):
        root = Path(directory) / 'SFDC_Ui'
        root.mkdir()
        for name in module.REQUIRED:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')
        return root

    def test_missing_shared_runtime_fails_before_test_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'nextbss/shared/preview-validation-protocol.mjs').unlink()
            with self.assertRaisesRegex(module.PreflightError, 'shared/preview-validation-protocol'):
                module.preflight(root, run=lambda *args, **kwargs: self.fail('No command before source inventory'))

    def test_immutable_ref_and_actual_node_contract_are_required(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(module.shutil, 'which', return_value='/node'):
            root = self.fixture(directory)
            def run(arguments):
                if '--version' in arguments: return module.NODE_VERSION
                if 'rev-parse' in arguments: return 'a' * 40
                return json.dumps({'modules': 72, 'componentLibrary': {'family': 'NOVA_PUBLIC'}})
            proof = module.preflight(root, expected_ref='a' * 40, run=run)
            self.assertEqual(proof['uiCommit'], 'a' * 40)
            self.assertEqual(len(proof['requiredSourceSha256']), len(module.REQUIRED))
            with self.assertRaisesRegex(module.PreflightError, 'differs'):
                module.preflight(root, expected_ref='b' * 40, run=run)
            with self.assertRaisesRegex(module.PreflightError, 'immutable'):
                module.preflight(root, expected_ref='dev2', run=run)

    def test_wrong_node_and_non_sibling_layout_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(module.shutil, 'which', return_value='/node'):
            root = self.fixture(directory)
            with self.assertRaisesRegex(module.PreflightError, 'Node'):
                module.preflight(root, run=lambda arguments: 'v22.0.0')
            with self.assertRaisesRegex(module.PreflightError, 'sibling'):
                module.preflight(root, agent_root=Path(directory) / 'elsewhere/Agent')
