import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sfdc_agent.engine import NodeEngine


class EngineStorageTests(unittest.TestCase):
    def test_pilot_without_explicit_source_does_not_search_ui_private_storage(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            ui = Path(root) / 'ui'
            with self.assertRaisesRegex(RuntimeError, 'NOVA_PILOT_FIGMA_SOURCE'):
                NodeEngine(Path(root) / 'agent', ui)
            self.assertFalse(ui.exists())

    def test_pilot_passes_pinned_storage_paths_to_node_without_service_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            release = root / 'releases'
            release.mkdir()
            paths = {}
            for name in ('NOVA_PILOT_FIGMA_SOURCE', 'NOVA_PILOT_GUIDE_FIG', 'NOVA_PILOT_LIBRARY_FIG'):
                path = root / name
                path.write_bytes(b'original')
                paths[name] = str(path)
            paths.update(SFDC_MANAGED_RELEASE_ROOT=str(release), SFDC_AGENT_EXECUTION_TOKEN='private-service-value')

            def invoke(arguments, **kwargs):
                request = json.loads(Path(arguments[2]).read_text())
                self.assertEqual(request['context']['sourcePath'], paths['NOVA_PILOT_FIGMA_SOURCE'])
                self.assertEqual(request['context']['releaseRoot'], str(release))
                self.assertEqual(request['context']['sourcePaths']['library'], paths['NOVA_PILOT_LIBRARY_FIG'])
                self.assertNotIn('SFDC_AGENT_EXECUTION_TOKEN', kwargs['env'])
                return subprocess.CompletedProcess(arguments, 0, stdout='{"verified":true}', stderr='')

            with patch.dict(os.environ, paths), patch('sfdc_agent.engine.subprocess.run', side_effect=invoke):
                engine = NodeEngine(root / 'agent', root / 'ui')
                self.assertEqual(engine.invoke('pin'), {'verified': True})
