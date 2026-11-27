"""Validate bounded scope receipts; Node owns graph inference and opaque scene hashing."""

import json
import re

_ROLES = {'base', 'state', 'popup', 'screen', 'candidate'}
_STATUSES = {'source-evidence', 'review-required'}
_PRIVATE = {'accesstoken', 'refreshtoken', 'authorization', 'credentials', 'credential', 'clientsecret',
            'cookie', 'setcookie', 'sessionid', 'password', 'environment', 'privatekey', 'apikey', 'checkpoint',
            'rawdocument', 'figmadocument', 'sourcesnapshot', 'authoredtext', 'hiddenauthoredtext', 'rawprompt', 'prompttraces'}


def _invalid(code='SCREEN_ANALYSIS_CONTRACT_INVALID'):
    return ValueError(code)


def _text(value, limit=200):
    return isinstance(value, str) and 0 < len(value) <= limit


def _keys(value, allowed):
    return isinstance(value, dict) and not value.keys() - set(allowed)


def _node_id(value):
    return _text(value) and re.fullmatch(r'I?[0-9]+:[0-9]+(?:;[0-9]+:[0-9]+)*', value) is not None


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _count(value):
    return type(value) is int and 0 <= value <= 1_000_000


def _evidence(value):
    return isinstance(value, list) and len(value) <= 100 and all(_keys(item, ('kind', 'nodeId'))
        and _text(item.get('kind'), 100) and _node_id(item.get('nodeId')) for item in value)


def _bounded(value, depth=0):
    if depth > 12:
        raise _invalid()
    if isinstance(value, str):
        if len(value) > 2400:
            raise _invalid()
    elif isinstance(value, list):
        if len(value) > 300:
            raise _invalid()
        for item in value:
            _bounded(item, depth + 1)
    elif isinstance(value, dict):
        if {'children', 'id', 'type'} <= value.keys():
            raise _invalid()
        for key, item in value.items():
            if key.replace('_', '').replace('-', '').lower() in _PRIVATE:
                raise _invalid()
            _bounded(item, depth + 1)
    elif value is not None and type(value) not in (bool, int, float):
        raise _invalid()


def validate_logical_scope(analysis, captured):
    scope, contract = analysis.get('logicalScope'), analysis.get('contract', {})
    if 'logicalScope' not in analysis:
        if 'logicalScopeSha256' in contract or 'logicalScopeSha256' in captured:
            raise _invalid()
        return None
    if (not _keys(scope, ('schemaVersion','scopeDigest','source','baseMemberId','members','edges','gaps','collection'))
            or type(scope.get('schemaVersion')) is not int or scope['schemaVersion'] != 1
            or not _hash(scope.get('scopeDigest')) or not _keys(scope.get('source'), ('fileKey','version'))
            or not isinstance(scope['source'].get('fileKey'), str) or re.fullmatch(r'[A-Za-z0-9]{1,160}', scope['source']['fileKey']) is None
            or not isinstance(scope['source'].get('version'), str) or re.fullmatch(r'[A-Za-z0-9_-]{1,160}', scope['source']['version']) is None
            or not _text(scope.get('baseMemberId')) or not isinstance(scope.get('members'), list)
            or not 1 <= len(scope['members']) <= 80 or not isinstance(scope.get('edges'), list) or len(scope['edges']) > 200
            or not isinstance(scope.get('gaps'), list) or len(scope['gaps']) > 300
            or not _keys(scope.get('collection'), ('complete','truncated','outlineMemberCount','capturedMemberCount','omittedMemberCount','maxMembers','maxOutlineMembers'))):
        raise _invalid()
    collection, members = scope['collection'], {}
    if (any(type(collection.get(key)) is not bool for key in ('complete', 'truncated'))
            or any(not _count(collection.get(key)) for key in ('outlineMemberCount', 'capturedMemberCount',
                'omittedMemberCount', 'maxMembers', 'maxOutlineMembers'))
            or collection['maxMembers'] > 24 or collection['maxOutlineMembers'] > 80):
        raise _invalid()
    for member in scope['members']:
        if (not _keys(member, ('id','sourceNodeId','renderNodeId','label','role','status','sourceSha256','outlineSha256','renderNodeType','eligible','captured','evidence'))
                or any(not _node_id(member.get(key)) for key in ('id', 'sourceNodeId', 'renderNodeId'))
                or member['id'] in members or not isinstance(member.get('label'), str) or len(member['label']) > 600
                or not _text(member.get('renderNodeType'), 50) or member.get('role') not in _ROLES
                or member.get('status') not in _STATUSES or type(member.get('captured')) is not bool
                or type(member.get('eligible')) is not bool or not _evidence(member.get('evidence'))
                or member['captured'] and not _hash(member.get('sourceSha256'))
                or not member['captured'] and (member['eligible'] or member.get('sourceSha256') is not None)
                or 'outlineSha256' in member and not _hash(member['outlineSha256'])):
            raise _invalid()
        members[member['id']] = member
    base = members.get(scope['baseMemberId'])
    full = sum(member['captured'] for member in members.values())
    if (not base or base['role'] != 'base' or not base['captured'] or not base['eligible']
            or sum(member['role'] == 'base' for member in members.values()) != 1
            or full > 24 or collection['capturedMemberCount'] != full or collection['maxMembers'] < full
            or collection['maxOutlineMembers'] < len(members) or collection['outlineMemberCount'] < len(members)):
        raise _invalid()
    edge_ids = set()
    for edge in scope['edges']:
        if (not _keys(edge, ('id','fromMemberId','toMemberId','targetNodeId','sourceNodeId','trigger','action','status','executable','reason','evidence'))
                or not _text(edge.get('id')) or edge['id'] in edge_ids or edge.get('fromMemberId') not in members
                or 'toMemberId' not in edge or edge['toMemberId'] is not None and edge['toMemberId'] not in members
                or not _node_id(edge.get('sourceNodeId')) or not _text(edge.get('trigger'), 80)
                or not _text(edge.get('action'), 80) or edge.get('status') not in _STATUSES
                or type(edge.get('executable')) is not bool or not _evidence(edge.get('evidence'))
                or edge.get('targetNodeId') is not None and not _node_id(edge['targetNodeId'])
                or 'reason' in edge and not _text(edge['reason'], 200)
                or edge['executable'] and (edge['status'] != 'source-evidence' or edge['trigger'] != 'CLICK'
                    or edge['action'] not in {'NAVIGATE','OVERLAY','SWAP','CLOSE','BACK','CHANGE_TO'})):
            raise _invalid()
        edge_ids.add(edge['id'])
    for gap in scope['gaps']:
        if (not _keys(gap, ('code','nodeId','message')) or not _text(gap.get('code'), 100)
                or 'nodeId' in gap and not _node_id(gap['nodeId'])
                or 'message' in gap and (not isinstance(gap['message'], str) or len(gap['message']) > 1200)):
            raise _invalid()
    source = analysis.get('source', {})
    if ('nodeId' in source and source['nodeId'] != base['renderNodeId']
            or any(key in source and source[key] != scope['source'][key] for key in ('fileKey', 'version'))
            or contract.get('logicalScopeSha256') != scope['scopeDigest'] or captured.get('logicalScopeSha256') != scope['scopeDigest']):
        raise _invalid()
    _bounded(scope)
    if len(json.dumps(scope, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) > 128 * 1024:
        raise _invalid()
    return scope


def validate_scope_review(scope, review):
    if scope is None:
        if review is not None:
            raise _invalid('SCREEN_SCOPE_REVIEW_INVALID')
        return None
    if (not isinstance(review, dict) or type(review.get('schemaVersion')) is not int or review['schemaVersion'] != 1
            or not isinstance(review.get('decisions'), list)):
        raise _invalid('SCREEN_SCOPE_REVIEW_REQUIRED')
    if review.get('scopeDigest') != scope['scopeDigest']:
        raise _invalid('SCREEN_SCOPE_REVIEW_CHANGED')
    if set(review) != {'schemaVersion', 'scopeDigest', 'decisions'} or len(review['decisions']) != len(scope['members']):
        raise _invalid('SCREEN_SCOPE_REVIEW_INVALID')
    decisions = {}
    for item in review['decisions']:
        if (not isinstance(item, dict) or set(item) != {'memberId', 'included', 'role'} or not _text(item.get('memberId'))
                or item['memberId'] in decisions or type(item.get('included')) is not bool or item.get('role') not in _ROLES):
            raise _invalid('SCREEN_SCOPE_REVIEW_INVALID')
        decisions[item['memberId']] = item
    normalized = []
    for member in scope['members']:
        choice = decisions.pop(member['id'], None)
        if (choice is None or member['id'] == scope['baseMemberId'] and (not choice['included'] or choice['role'] != 'base')
                or member['id'] != scope['baseMemberId'] and choice['role'] == 'base'
                or member['status'] == 'source-evidence' and member['role'] in {'state', 'popup'} and choice['role'] != member['role']
                or choice['included'] and (not member['captured'] or not member['eligible'] or choice['role'] not in {'base', 'state', 'popup'})):
            raise _invalid('SCREEN_SCOPE_REVIEW_INVALID')
        normalized.append({'memberId': member['id'], 'included': choice['included'], 'role': choice['role']})
    if decisions or sum(choice['included'] for choice in normalized) > 24:
        raise _invalid('SCREEN_SCOPE_REVIEW_INVALID')
    return {'schemaVersion': 1, 'scopeDigest': scope['scopeDigest'], 'decisions': normalized}
