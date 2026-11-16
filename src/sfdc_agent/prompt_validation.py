"""Render saved prompt drafts against small, synthetic operation inputs."""

from .screen_tasks import build_screen_task
from .story_tasks import build_story_task


_FIGMA = {"fileKey": "PromptFixture", "nodeId": "1:2"}
_SCREEN = {**_FIGMA, "name": "nextBssScreenFixture", "version": "1.9", "tokenDeclarations": "--nb-spacing-8: 8px;",
           "typographyGroups": [], "componentVariants": "Button: Primary / Enabled",
           "layoutDimensions": {"width": 640, "height": 480}}
_LOCAL = {**_SCREEN, "local": True, "sourceContext": {"nodes": [{"id": "1:2", "name": "Sample screen"}]},
          "reviewedAnalysis": {"id": "fixture", "mappings": [], "issues": []}}
_REST = {**_LOCAL, "sourceKind": "figma-rest", "historyTables": [{"nodeId": "1:3", "columns": [], "empty": "No records"}]}
_CAPTURED = {**_SCREEN, "reviewedAnalysis": _LOCAL["reviewedAnalysis"],
             "capturedContext": [{"nodeId": "1:2", "text": "Sample captured measurement"}]}
_SPLIT = {**_SCREEN, "coverage": {"split": True, "nodesRead": ["1:2"], "unresolved": ["1:3"]},
          "sourceSections": [{"nodeId": "1:2", "text": "Sample section"}], "splitLayout": [{"nodeId": "1:2"}]}
_PLAN = {"source": _FIGMA, "sourceDigest": "a" * 64, "referenceContext": "Sample Figma structure"}
_STORY = '{"specDigest":"' + "a" * 64 + '","bundleName":"nextBssStoryFixture","screens":[]}'
_DIAGNOSTIC = "Sample compiler diagnostic"
_OLD = '{"title":"Sample prior draft"}'

_CASES = {
    "screen.review-plan": {"REST": _REST, "로컬 .fig": _LOCAL, "MCP 캡처": _CAPTURED, "MCP 분할": _SPLIT},
    "screen.generate": {"REST": _REST, "로컬 .fig": _LOCAL, "MCP 캡처": _CAPTURED, "MCP 분할": _SPLIT, "MCP": _SCREEN},
    "screen.repair": {"저장 근거": {"local": True, "diagnostic": _DIAGNOSTIC, "evidence": _REST, "previousDraft": _OLD},
                      "ACP 로컬": {"local": True, "diagnostic": _DIAGNOSTIC},
                      "ACP": {"local": False, "diagnostic": _DIAGNOSTIC}},
    "screen.analyze": {"노드": {"fileKey": _FIGMA["fileKey"], "nodeIds": ["1:2", "1:3"], "includeScreenshot": True}},
    "screen.metadata": {"노드": {"fileKey": _FIGMA["fileKey"], "nodeIds": ["1:2"]}},
    "screen.capture": {"캡처·레이아웃": {**_FIGMA, "maxDimension": 640, "readLayout": True},
                       "캡처": {**_FIGMA, "maxDimension": 640}},
    "screen.discovery": {"프레임": _FIGMA, "페이지": {"fileKey": _FIGMA["fileKey"]}},
    "screen.plan": {"원본 근거": _PLAN, "캡처": {**_PLAN, "referenceContext": None}},
    "screen.plan.repair": {"저장 근거": {"evidence": _PLAN, "previousDraft": _OLD, "diagnostic": _DIAGNOSTIC},
                           "ACP": {"diagnostic": _DIAGNOSTIC}},
    "story.classification": {"업무 분류": {"evidence": '{"headings":[],"screens":[],"links":[]}'}},
    "story.analysis": {"업무 명세": {"evidence": '{"screens":[],"cases":[],"links":[]}', "categories": "{}", "maxFacts": 4}},
    "story.generate": {"코드 생성": {"input": _STORY},
                       "코드 보정": {"input": _STORY, "diagnostic": '{"code":"FIXTURE","message":"Sample error"}'}},
}

# A body can also be included in a different operation's repair request.
_DEPENDENTS = {"screen.generate": ("screen.repair",), "screen.repair": ("screen.plan.repair",),
               "screen.plan": ("screen.plan.repair",)}


def preview_operation(operation, templates):
    rendered = {}
    for target in (operation, *_DEPENDENTS.get(operation, ())):
        builder = build_screen_task if target.startswith("screen.") else build_story_task
        for label, fields in _CASES[target].items():
            text = builder(target, fields, templates=templates)
            if not text.strip() or len(text.encode()) > 8 * 1024 * 1024:
                raise ValueError("조립한 프롬프트가 비어 있거나 허용 크기를 초과합니다.")
            rendered[label if target == operation else f"{target} · {label}"] = text
    return next(iter(rendered.values())), list(rendered)
