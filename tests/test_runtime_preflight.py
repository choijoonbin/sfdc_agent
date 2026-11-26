import importlib.util
import json
from pathlib import Path
import shutil
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
                fixture, identity = module.library_fixture()
                return json.dumps({'modules': 72, 'componentLibrary': fixture['componentLibrary'],
                                   'libraryFixtureArchiveSha256': identity['archiveSha256']})
            proof = module.preflight(root, expected_ref='a' * 40, run=run)
            self.assertEqual(proof['uiCommit'], 'a' * 40)
            self.assertEqual(len(proof['requiredSourceSha256']), len(module.REQUIRED))
            self.assertEqual(proof['libraryFixture']['moduleCount'], 72)
            self.assertFalse((root / 'nextbss/design-system/lwc').exists())
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

    def test_fixed_library_fixture_has_no_nova_checkout_or_ui_source_dependency(self):
        fixture, identity = module.library_fixture()
        self.assertEqual((identity['fileCount'], identity['expandedBytes'], identity['moduleCount']), (242, 728075, 72))
        self.assertEqual(fixture['componentLibrary']['family'], 'NOVA_PUBLIC')
        self.assertIn('lwc/novaTokens/novaTokens.css', fixture['entries'])
        self.assertNotIn('nextbss/design-system/lwc/novaTokens/novaTokens.css', module.REQUIRED)

    def test_missing_changed_or_linked_fixture_is_rejected_before_node(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'fixture'
            target.mkdir()
            with self.assertRaisesRegex(module.PreflightError, 'fixture'):
                module.library_fixture(target)
            for name in module.FIXTURE_PINS:
                shutil.copyfile(module.FIXTURE_ROOT / name, target / name)
            archive = target / 'library-files.zip'
            data = archive.read_bytes()
            archive.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            with self.assertRaisesRegex(module.PreflightError, 'pin changed'):
                module.library_fixture(target)
            archive.unlink()
            archive.symlink_to(module.FIXTURE_ROOT / 'library-files.zip')
            with self.assertRaisesRegex(module.PreflightError, 'fixture'):
                module.library_fixture(target)

    def test_node_reader_must_report_the_complete_selected_fixture_identity(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(module.shutil, 'which', return_value='/node'):
            root = self.fixture(directory)
            def run(arguments):
                if '--version' in arguments: return module.NODE_VERSION
                if 'rev-parse' in arguments: return 'a' * 40
                return json.dumps({'modules': 72, 'componentLibrary': {'family': 'NOVA_PUBLIC'},
                                   'libraryFixtureArchiveSha256': '0' * 64})
            with self.assertRaisesRegex(module.PreflightError, 'runtime contract'):
                module.preflight(root, expected_ref='a' * 40, run=run)
