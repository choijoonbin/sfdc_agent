#!/usr/bin/env python3
"""Fail-closed Agent unit gate with a matched, explicitly supplied Node runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

REQUIRED = (
    'package.json', 'package-lock.json',
    'nextbss/engine/component-library.mjs', 'nextbss/engine/managed-runtime-capabilities.json',
    'nextbss/server/managed-execution.mjs', 'nextbss/server/managed-analysis-output.mjs',
    'nextbss/server/managed-original-analysis.mjs', 'nextbss/server/managed-kiro-discovery.mjs',
    'nextbss/server/kiro-nova-contract.mjs', 'nextbss/shared/preview-validation-protocol.mjs',
    'nextbss/design-system/nova-library.json', 'nextbss/design-system/nova-public-contract.json',
    'nextbss/design-system/nova-module-manifest.json', 'nextbss/design-system/lwc/novaTokens/novaTokens.css',
)
NODE_VERSION = 'v26.13.0'
NODE_PREFLIGHT = r"""
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const root=process.argv[1];
globalThis.fetch=()=>{throw Error('Provider calls are forbidden in unit preflight');};
const fromRoot=path=>import(pathToFileURL(resolve(root,path)).href);
const {readComponentLibrary,assertManagedLibraryCompatible}=await fromRoot('nextbss/engine/component-library.mjs');
const library=await readComponentLibrary(path=>readFile(resolve(root,'nextbss/design-system',path),'utf8'));
await assertManagedLibraryCompatible(library.tuple);
for(const path of ['nextbss/server/managed-execution.mjs','nextbss/server/managed-analysis-output.mjs',
  'nextbss/server/managed-original-analysis.mjs','nextbss/server/managed-kiro-discovery.mjs',
  'nextbss/shared/preview-validation-protocol.mjs']) await fromRoot(path);
process.stdout.write(JSON.stringify({modules:library.modules.size,componentLibrary:library.tuple}));
"""


class PreflightError(RuntimeError):
    pass


def checked_run(arguments, *, cwd=None, timeout=45):
    try:
        result = subprocess.run(arguments, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        raise PreflightError('The declared runtime command failed; install the exact Node/dependencies and inspect the matched checkout.') from None
    return result.stdout.strip()


def preflight(root, *, expected_ref=None, agent_root=None, run=checked_run):
    root = Path(root).resolve(strict=True)
    if agent_root is not None and root != Path(agent_root).resolve().parent / 'SFDC_Ui':
        raise PreflightError('Unit contracts require the explicit sibling SFDC_Ui checkout; another directory cannot satisfy sibling-dependent tests.')
    if expected_ref is not None and not re.fullmatch(r'[a-f0-9]{40}', expected_ref):
        raise PreflightError('The UI runtime ref must be an immutable 40-character Git commit SHA.')
    for name in REQUIRED:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise PreflightError('Required matched runtime source is absent: ' + name)
    node = shutil.which('node')
    if not node or run([node, '--version']) != NODE_VERSION:
        raise PreflightError('The unit gate requires Node ' + NODE_VERSION + '.')
    revision = run(['git', '-C', str(root), 'rev-parse', 'HEAD'])
    if expected_ref is not None and revision != expected_ref:
        raise PreflightError('The checked-out UI commit differs from the declared immutable runtime ref.')
    try:
        actual = json.loads(run([node, '--input-type=module', '-e', NODE_PREFLIGHT, str(root)]))
        if actual['modules'] != 72 or actual['componentLibrary']['family'] != 'NOVA_PUBLIC':
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise PreflightError('The selected Node source does not provide the required verified NOVA runtime contract.') from None
    return {'node': NODE_VERSION, 'uiCommit': revision, 'componentLibrary': actual['componentLibrary'],
            'requiredSourceSha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in REQUIRED}}


def source_hashes(root, folders, files=()):
    root = Path(root)
    inventory = {}
    paths = [root / name for name in files]
    for folder in folders:
        paths.extend(path for path in (root / folder).rglob('*') if path.is_file() and '__pycache__' not in path.parts
                     and not path.name.endswith(('.pyc', '.DS_Store')))
    for path in sorted(paths):
        if path.is_symlink():
            raise PreflightError('Unit source inventories must not contain symlinks.')
        inventory[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return inventory


def load_suite(repo):
    # Existing tests are intentionally not a Python package (__init__.py absent).
    # A few fixtures use the namespace form tests.test_*, so retain its parent too.
    sys.path.insert(0, str(Path(repo)))
    return unittest.TestLoader().discover(str(Path(repo) / 'tests'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-root', required=True)
    parser.add_argument('--expected-ui-ref')
    parser.add_argument('--report', default='unit-contract-proof.json')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        proof = preflight(args.ui_root, expected_ref=args.expected_ui_ref, agent_root=repo)
        before = source_hashes(args.ui_root, ['nextbss/design-system', 'nextbss/engine', 'nextbss/server', 'nextbss/shared'], ['package.json', 'package-lock.json'])
        agent = source_hashes(repo, ['src', 'tests', 'scripts'], ['requirements.lock', 'pyproject.toml', '.github/workflows/verify.yml'])
    except (OSError, PreflightError) as error:
        parser.exit(2, 'Runtime preflight failed: ' + str(error) + '\n')
    os.environ.update(SFDC_UI_ROOT=str(Path(args.ui_root).resolve()), SFDC_MANAGED_ENGINE_ROOT=str(Path(args.ui_root).resolve()))
    # Unit proofs never inherit an operator's provider credentials.
    for key in list(os.environ):
        if key.startswith(('FIGMA_', 'KIRO_', 'AZURE_', 'OPENAI_', 'SALESFORCE_')):
            os.environ.pop(key, None)
    sys.path.insert(0, str(repo / 'src'))
    result = unittest.TextTestRunner(verbosity=2).run(load_suite(repo))
    proof['tests'] = {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped)}
    proof['providerOrModelEvidence'] = False
    proof['runtimeSourceSha256'] = before
    proof['agentSourceSha256'] = agent
    proof['sourcesUnchanged'] = (before == source_hashes(args.ui_root, ['nextbss/design-system', 'nextbss/engine', 'nextbss/server', 'nextbss/shared'], ['package.json', 'package-lock.json'])
                                 and agent == source_hashes(repo, ['src', 'tests', 'scripts'], ['requirements.lock', 'pyproject.toml', '.github/workflows/verify.yml']))
    Path(args.report).write_text(json.dumps(proof, indent=2) + '\n')
    if result.skipped:
        print('Required Agent unit gate rejected skipped tests; supply the matched runtime instead of accepting a partial run.', file=sys.stderr)
    if not proof['sourcesUnchanged']:
        print('Matched sources changed during the unit gate; this proof cannot certify another source state.', file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped and proof['sourcesUnchanged'] else 1


if __name__ == '__main__':
    sys.exit(main())
