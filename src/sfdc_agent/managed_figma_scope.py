"""Allocate the process's Figma provider credentials to one trusted server scope."""

import os
from uuid import UUID

from fastapi import HTTPException
from starlette.responses import JSONResponse


class FigmaProviderScopeError(HTTPException):
    def __init__(self, status, code):
        self.code = code
        super().__init__(status_code=status, detail=code)


def require_figma_scope(scope):
    configured = os.environ.get('SFDC_MANAGED_FIGMA_SCOPE', '')
    try:
        allowed = tuple(configured.split('/'))
        if len(allowed) != 2 or any(str(UUID(value)) != value for value in allowed):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise FigmaProviderScopeError(503, 'FIGMA_PROVIDER_SCOPE_UNAVAILABLE') from None
    try:
        selected = tuple(str(value) for value in scope) if isinstance(scope, tuple) else (str(scope.tenantId), str(scope.workspaceId))
    except (TypeError, AttributeError):
        selected = ()
    if selected != allowed:
        raise FigmaProviderScopeError(403, 'FIGMA_PROVIDER_SCOPE_FORBIDDEN')


def figma_scope_response(_, error):
    return JSONResponse({'code': error.detail}, status_code=error.status_code)


def remote_figma_environment(scope):
    require_figma_scope(scope)
    environment = {'FIGMA_ACCESS_TOKEN': os.environ.get('FIGMA_ACCESS_TOKEN', '')}
    if os.environ.get('FIGMA_PLAN_ACCESS_TOKEN', '').strip():
        environment['FIGMA_PLAN_ACCESS_TOKEN'] = os.environ['FIGMA_PLAN_ACCESS_TOKEN']
    return environment
