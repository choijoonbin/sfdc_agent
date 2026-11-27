"""Validate one private analysis export; Spring remains its durable storage owner."""

from hashlib import sha256
import math
import os
from pathlib import Path
import re
import stat
import struct
from urllib.parse import urlsplit
from uuid import UUID
import xml.etree.ElementTree as ET

from fastapi import HTTPException
from .strict_json import decode_json as _decode_json

VIEW_BYTES = 8 * 1024 * 1024
MANIFEST_BYTES = 256 * 1024
IMAGE_BYTES = 2 * 1024 * 1024 + 1024
TOTAL_BYTES = 128 * 1024 * 1024
ASSETS = 512
HASH = re.compile(r'^[a-f0-9]{64}$')
NODE = re.compile(r'^[0-9]+:[0-9]+$')
SOURCE_URL = re.compile(r'^/api/nextbss/stories/analyses/([a-f0-9-]{36})/previews/([0-9]+:[0-9]+)\.svg$')
UNAVAILABLE = {'SOURCE_ARCHIVE_UNAVAILABLE', 'SOURCE_SNAPSHOT_UNAVAILABLE', 'SOURCE_NODE_UNAVAILABLE',
               'SOURCE_EXPORT_UNAVAILABLE', 'SOURCE_PREVIEW_TOO_LARGE', 'SOURCE_PREVIEW_BUDGET_EXCEEDED',
               'SOURCE_PREVIEW_UNSUPPORTED'}
PRIVATE = {'accesstoken', 'refreshtoken', 'clientsecret', 'password', 'authorization', 'cookie', 'setcookie',
           'sessionid', 'frontdoorurl', 'otp', 'prompttraces', 'promptbundle', 'environment', 'apikey',
           'credentials', 'credential', 'privatekey', 'rawprompt', 'checkpoint', 'rawcheckpoint'}


def invalid():
    return ValueError('ANALYSIS_OUTPUT_INVALID')


def output_directory(payload, *, root=None):
    configured = root if root is not None else os.environ.get('SFDC_MANAGED_RELEASE_INGRESS', '')
    if not configured:
        raise HTTPException(503, 'Analysis ingress is unavailable.')
    try:
        root = Path(configured).resolve(strict=True)
        if not root.is_dir():
            raise OSError
        parent = root / 'analysis' / str(payload.jobId)
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if parent.resolve(strict=True) != parent:
            raise OSError
        directory = parent / str(payload.attempt)
        directory.mkdir(mode=0o700)
        return directory
    except FileExistsError:
        raise HTTPException(409, 'EXECUTION_OUTPUT_ALREADY_PREPARED') from None
    except OSError:
        raise HTTPException(503, 'Analysis ingress is unavailable.') from None


def _bytes(path, limit):
    if path.resolve(strict=True) != path:
        raise invalid()
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > limit:
        raise invalid()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size != metadata.st_size:
            raise invalid()
        value = stream.read(limit + 1)
    if len(value) != metadata.st_size or len(value) > limit:
        raise invalid()
    return value


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _source_nodes(value, analysis_id, depth=0):
    if depth > 40:
        raise invalid()
    nodes = set()
    if isinstance(value, list):
        if len(value) > 20000:
            raise invalid()
        for member in value:
            nodes.update(_source_nodes(member, analysis_id, depth + 1))
    elif isinstance(value, dict):
        for key, member in value.items():
            if key.replace('_', '').replace('-', '').lower() in PRIVATE:
                raise invalid()
            if key in {'previewUrl', 'sourcePreviewUrl'} and isinstance(member, str):
                match = SOURCE_URL.fullmatch(member)
                if not match or match[1] != analysis_id or len(match[2]) > 80:
                    raise invalid()
                nodes.add(match[2])
            else:
                nodes.update(_source_nodes(member, analysis_id, depth + 1))
    return nodes


def _image(bytes_, entry):
    if entry['mediaType'] == 'image/png':
        if len(bytes_) < 24 or bytes_[:8] != b'\x89PNG\r\n\x1a\n' or bytes_[12:16] != b'IHDR':
            raise invalid()
        dimensions = struct.unpack('>II', bytes_[16:24])
    elif entry['mediaType'] == 'image/svg+xml':
        if re.search(br'<!DOCTYPE|<!ENTITY', bytes_, re.I):
            raise invalid()
        root = ET.fromstring(bytes_)
        if root.tag.rsplit('}', 1)[-1] != 'svg':
            raise invalid()
        box = root.attrib.get('viewBox', '').replace(',', ' ').split()
        dimensions = None
        if len(box) == 4:
            try:
                dimensions = tuple(math.ceil(float(value)) for value in box[2:])
            except (ValueError, OverflowError):
                raise invalid() from None
    else:
        raise invalid()
    if dimensions and any(not 1 <= value <= 20000 for value in dimensions):
        raise invalid()
    for index, key in enumerate(('width', 'height')):
        if key in entry and (not _integer(entry[key], 1, 20000) or dimensions and entry[key] != dimensions[index]):
            raise invalid()


def verify(directory, payload, result, runtime_root):
    try:
        manifest_bytes = _bytes(directory / 'manifest.json', MANIFEST_BYTES)
        if sha256(manifest_bytes).hexdigest() != result.analysisView.manifestSha256:
            raise invalid()
        manifest = _decode_json(manifest_bytes)
        required = {'protocolVersion', 'serializerVersion', 'jobId', 'attempt', 'tenantId', 'workspaceId', 'kind', 'engine',
                    'producerRuntimeSha256', 'analysisId', 'checkpointSha256', 'source', 'view', 'assets', 'unavailable'}
        if not isinstance(manifest, dict) or not required <= manifest.keys() or manifest.keys() - required - {'unavailableRemainder'}:
            raise invalid()
        expected = {'protocolVersion': 1, 'serializerVersion': 1, 'jobId': str(payload.jobId), 'attempt': payload.attempt,
                    'tenantId': str(payload.executionScope.tenantId), 'workspaceId': str(payload.executionScope.workspaceId),
                    'kind': payload.kind, 'engine': payload.engine, 'analysisId': result.checkpoint.get('id'),
                    'checkpointSha256': result.checkpoint.get('digest')}
        if any(manifest.get(key) != value for key, value in expected.items()) or not HASH.fullmatch(manifest['checkpointSha256'] or ''):
            raise invalid()
        if not _integer(manifest['protocolVersion'], 1, 1) or not _integer(manifest['serializerVersion'], 1, 1) or not _integer(manifest['attempt'], 1, 2147483647):
            raise invalid()
        raw = __import__('base64').b64decode(result.checkpoint.get('bytes', ''), validate=True)
        if sha256(raw).hexdigest() != manifest['checkpointSha256']:
            raise invalid()
        analysis_id = manifest['analysisId']
        if str(UUID(analysis_id)) != analysis_id:
            raise invalid()
        record = _decode_json(raw)
        original = record.get('analysis') if payload.kind == 'SCREEN' else record
        if not isinstance(original, dict) or original.get('id') != analysis_id:
            raise invalid()
        if payload.kind=='SCREEN':
            from .screen_analysis_contract import validate_checkpoint
            validate_checkpoint(payload,result.checkpoint)
        runtime = _decode_json(_bytes(runtime_root / 'managed-runtime.json', 2 * 1024 * 1024))
        if not HASH.fullmatch(manifest['producerRuntimeSha256']) or runtime.get('sha256') != manifest['producerRuntimeSha256']:
            raise invalid()
        source = manifest['source']
        selected = urlsplit(payload.sourceReference)
        key = payload.archive.fileKey if payload.archive else selected.path.split('/')[2]
        if not isinstance(source, dict) or source.get('fileKey') != key or source.keys() - {'fileKey', 'archiveSha256', 'version'}:
            raise invalid()
        if payload.archive:
            if source.get('archiveSha256') != payload.archive.sha256:
                raise invalid()
        elif source.get('archiveSha256') is not None:
            raise invalid()
        if 'version' in source and (not isinstance(source['version'], str) or not 1 <= len(source['version']) <= 160 or any(ord(c) < 32 for c in source['version'])):
            raise invalid()
        view_entry = manifest['view']
        if not isinstance(view_entry, dict) or view_entry.get('path') != 'public-view.json' or set(view_entry) != {'path', 'sizeBytes', 'sha256'}:
            raise invalid()
        view_bytes = _bytes(directory / 'public-view.json', VIEW_BYTES)
        if not _integer(view_entry['sizeBytes'], 1, VIEW_BYTES) or view_entry['sizeBytes'] != len(view_bytes) or sha256(view_bytes).hexdigest() != view_entry['sha256']:
            raise invalid()
        view = _decode_json(view_bytes)
        if not isinstance(view, dict) or view.get('id') != manifest['analysisId'] or not isinstance(view.get('source'), dict):
            raise invalid()
        if payload.kind=='SCREEN' and 'contract' in original:
            if any(view.get(field)!=original.get(field) for field in ('contract','allowedGenerationEngines','generationOptions')):
                raise invalid()
        if payload.kind == 'SCREEN' and any(view.get(field) != original.get(field) or (field in view) != (field in original)
                                            for field in ('planningSelection', 'planningSummary')):
            raise invalid()
        if not payload.archive and view['source'].get('fileKey') not in (None, key):
            raise invalid()
        references = _source_nodes(view, manifest['analysisId'])
        assets, unavailable = manifest['assets'], manifest['unavailable']
        if not isinstance(assets, list) or len(assets) > ASSETS or not isinstance(unavailable, list):
            raise invalid()
        known, files = set(), {'manifest.json', 'public-view.json'}
        for entry in assets:
            if not isinstance(entry, dict) or entry.keys() - {'nodeId', 'mediaType', 'path', 'sizeBytes', 'sha256', 'rendererVersion', 'provenance', 'width', 'height'}:
                raise invalid()
            node = entry.get('nodeId')
            if node not in references or node in known or not _integer(entry.get('rendererVersion'), 1, 1):
                raise invalid()
            known.add(node)
            checksum = entry.get('sha256', '')
            if not HASH.fullmatch(checksum) or entry.get('path') != 'blobs/' + checksum or not _integer(entry.get('sizeBytes'), 1, IMAGE_BYTES):
                raise invalid()
            if entry.get('provenance') not in {'LOCAL_RENDER', 'OFFICIAL_FIGMA_EXPORT', 'RECORDED_MCP'}:
                raise invalid()
            data = _bytes(directory / entry['path'], IMAGE_BYTES)
            if len(data) != entry['sizeBytes'] or sha256(data).hexdigest() != checksum:
                raise invalid()
            _image(data, entry)
            files.add(entry['path'])
        for entry in unavailable:
            if not isinstance(entry, dict) or set(entry) != {'nodeId', 'code'} or entry['nodeId'] not in references or entry['nodeId'] in known or entry['code'] not in UNAVAILABLE:
                raise invalid()
            known.add(entry['nodeId'])
        remainder = references - known
        if 'unavailableRemainder' in manifest:
            item = manifest['unavailableRemainder']
            digest = sha256(''.join(node + '\n' for node in sorted(remainder)).encode('ascii')).hexdigest()
            if not isinstance(item, dict) or set(item) != {'code', 'nodeCount', 'nodeSetSha256'} or item['code'] != 'SOURCE_PREVIEW_BUDGET_EXCEEDED' or not _integer(item['nodeCount'], 0, len(references)) or item['nodeCount'] != len(remainder) or item['nodeSetSha256'] != digest:
                raise invalid()
        elif remainder:
            raise invalid()
        total = 0
        observed = set()
        for path in directory.rglob('*'):
            relative = path.relative_to(directory).as_posix()
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode) and relative == 'blobs':
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or relative not in files:
                raise invalid()
            total += metadata.st_size
            observed.add(relative)
        if observed != files or total > TOTAL_BYTES:
            raise invalid()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, ET.ParseError):
        raise invalid() from None
