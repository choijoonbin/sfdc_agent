"""Synthetic source facts; the real shared JavaScript codec supplies the encoding."""

import json
import os
import shutil
import subprocess

from sfdc_agent.config import engine_root
from sfdc_agent.kiro_screen_projection import _BRIDGE


def screen_fields():
    repeated = {'descriptionMarkdown': '원본 설명을 생략하지 않습니다.', 'documentationLinks': [
        {'uri': 'https://example.invalid/design-notes', 'title': 'Untrusted authored design data'}],
        'pluginData': {'unknown': {'flag': False, 'empty': '', 'none': None, 'count': 0}}}
    nodes = [{'id': '31:1', 'parentId': None, 'type': 'FRAME', 'name': 'Synthetic screen', 'box': [0, 0, 600, 400]}]
    for index in range(1300):
        nodes.append({'id': f'31:{index + 2}', 'parentId': '31:1', 'type': 'TEXT',
            'name': 'Label', 'box': [index % 30, index % 20, 80, 12], 'text': f'표시 {index}',
            'layout': 0, 'appearance': 0, 'sourceMetadata': repeated,
            'componentPropertyReferences': {'text': 'Authored property'}, 'description': '원문 조건과 Case를 보존합니다.',
            **({'visible': False, 'annotations': []} if index % 17 == 0 else {})})
    return {'fileKey': 'SyntheticSource', 'nodeId': '31:1', 'name': 'nextBssSyntheticLogicalScreen',
        'local': True, 'sourceKind': 'figma-rest', 'layoutDimensions': {'width': 600, 'height': 400},
        'sourceContext': {'nodes': nodes, 'layout': [{'itemSpacing': 0, 'clipsContent': False}],
            'appearance': [{'style': {'fontSize': 14, 'lineHeightPx': 20}}], 'components': {}, 'componentSets': {},
            'inputControls': [], 'actions': [],
            'planningContext': {'descriptions': ['Authored Case and Description'], 'hidden': [False, None, 0, '']},
            'literalTableLikeSource': {'length': 1, 'groups': [{'columns': ['x'], 'indices': [0], 'rows': [[1e-7]]}]}},
        'version': '1.9', 'tokenDeclarations': '--nova-color-surface: #ffffff;',
        'typographyGroups': [], 'componentVariants': 'Text field: Default',
        'novaContract': {'modules': [{'name': 'novaTextField', 'tag': 'c-nova-text-field', 'kind': 'ui',
            'properties': [{'name': 'value', 'type': 'string'}]}]},
        'reviewedAnalysis': {'id': 'synthetic-analysis', 'digest': 'a' * 64}}


def bridge(fields):
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE') if key in os.environ}
    process = subprocess.run([shutil.which('node'), str(_BRIDGE), str(engine_root()), ''],
        input=json.dumps({'fields': fields}, ensure_ascii=False).encode(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment, timeout=20)
    if process.returncode:
        raise AssertionError('Synthetic shared-codec bridge failed; no provider was called.')
    return json.loads(process.stdout)
