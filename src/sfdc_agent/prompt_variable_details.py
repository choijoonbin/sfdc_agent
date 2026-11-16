"""Synthetic reference data for the prompt management UI, separate from execution."""

import json
from copy import deepcopy


def _detail(description, value_type, example, input_field=None):
    detail = {"description": description, "valueType": value_type, "example": example}
    if input_field:
        detail["inputField"] = input_field
    return detail


def _text(description, example, input_field=None):
    return _detail(description, "문자열", example, input_field)


def _fragment(description, example, input_field=None):
    return _detail(description, "자동 조립 문구", example, input_field)


def _object(description, example, input_field=None):
    return _detail(description, "JSON 객체", example, input_field)


def _serialized(description, example, input_field):
    return {**_detail(description, "JSON으로 직렬화된 문자열", example, input_field),
            "exampleLabel": "직렬화 전 JSON 예시"}


_REFERENCE = {"fileKey": "ExampleFixture", "nodeId": "1:2"}
_DIMENSIONS = {"width": 640, "height": 480}
_COVERAGE = {"split": True, "nodesRead": ["1:3"], "unresolved": ["1:4"]}
_SECTIONS = [{"nodeId": "1:2", "text": "Frame 640 × 480; title: Sample screen"}]
_SOURCE_CONTEXT = {
    "nodes": [{"id": "1:2", "parentId": None, "type": "FRAME", "name": "Sample screen",
               "box": [0, 0, 640, 480]}],
    "layout": [{}], "appearance": [{}], "components": {}, "componentSets": {},
    "inputControls": [], "actions": [],
}
_SOURCE_DIGEST = "a" * 64
_BUNDLE_NAME = "nextBssExampleScreen"
_SCREEN_OUTPUT = {
    **_REFERENCE, "title": "Sample screen", **_DIMENSIONS, "html": "<template>...</template>",
    "css": "@import 'c/nextBssTokens';\n...",
    "javascript": "import { LightningElement } from 'lwc';\nexport default class Screen extends LightningElement {}",
    "notes": [],
}
_STORY_BUNDLE = "nextBssExampleStory"
_STORY_SCREEN = {"id": "1:2", "title": "예제 화면"}
_REVIEWED_ANALYSIS = {
    "id": "00000000-0000-4000-8000-000000000001",
    "source": {**_REFERENCE, "mode": "figma-mcp", "revisionVerified": False},
    "standard": {"version": "1.9"}, "dimensions": _DIMENSIONS,
    "mappings": [{"nodeId": "1:3", "label": "조회", "component": "Button"}], "issues": [],
    "sourceDigest": _SOURCE_DIGEST, "reviewMode": "manual", "createdAt": "2026-01-01T00:00:00.000Z", "digest": "b" * 64,
}

_COMMON = {
    "version": _text("화면 변환에 적용할 NextBSS 컴포넌트 카탈로그의 버전입니다. 프롬프트 릴리스나 Figma 리비전과 별개입니다.",
                     "1.9", "version"),
    "component_variants": _text("등록된 NextBSS 컴포넌트와 각 컴포넌트의 변형 이름을 한 줄씩 나열한 카탈로그 요약입니다.",
                                "Button: Filled, T-Blue Ghost\nText Field / Text Area: Default", "componentVariants"),
    "file_key": _text("Figma 링크의 파일 식별자입니다.", _REFERENCE["fileKey"], "fileKey"),
    "node_id": _text("Figma 화면의 노드 ID입니다. 수집한 하위 근거를 인용할 때는 해당 하위 노드 ID를 사용합니다.",
                     "1:2", "nodeId / capturedContext[].nodeId / sourceSections[].nodeId"),
    "node_url": _text("Figma 링크의 node-id 쿼리 값을 만들기 위해 노드 ID의 콜론을 하이픈으로 바꿉니다.", "1-2", "nodeId"),
    "source_context": _object("REST 또는 로컬 .fig에서 읽은 레이어, 위치·크기, 표시 속성, 컴포넌트 및 동작 근거입니다.",
                              _SOURCE_CONTEXT, "sourceContext"),
    "dimensions_line": _fragment("원본의 화면 폭과 높이가 확인됐을 때 조립하는 크기 고정 지시입니다.",
                                 "Use the verified 640 × 480 px screen dimensions.", "layoutDimensions"),
    "reading": _fragment("원본 출처, 읽은 노드와 누락된 노드, 측정 위치에 따라 조립하는 원본 읽기 지시입니다.",
                         "Use these verified child get_design_context responses: 1:3.", "coverage / splitLayout"),
    "rules": _fragment("활성 NextBSS 버전에서 토큰, 용도, 배치와 상태를 일관되게 해석하는 공통 생성 규칙입니다.",
                       "Prefer a matching registered NextBSS component and its existing variants.", "version"),
    "action_evidence": _fragment("확인된 액션이 있을 때만 출처에 맞춰 조립하는 동작 지시입니다. 추정한 동작은 추가하지 않습니다.",
                                 "NOVA-verified action evidence from the selected source node.", "actions"),
    "additional_rules": _fragment("원본의 레이어와 상태를 실제 요소로 구현할 때 항상 포함하는 추가 규칙입니다.",
                                   "Preserve the authored controls and do not invent business behavior."),
    "table_rules": _fragment("이력 테이블 근거가 있을 때만 넣는 열 순서, 행, 빈 상태 구현 규칙과 근거입니다.",
                              "Use the verified history table column order and preserve the empty state.",
                              "historyTables / sourceContext.historyTables"),
    "accordion": _fragment("아코디언 변형, 펼침 상태와 부모 기준 배치를 확인하게 하는 공통 규칙과 출처별 지시입니다.",
                           "Inspect each Accordion instance and its enclosing layout before generating code."),
    "component_contracts": _fragment("공통 NextBSS LWC 태그의 지원 속성과 사용 조건을 알려주는 내부 규칙입니다.",
                                     "Use the documented NextBSS LWC attributes and supported variants."),
    "token_declarations": _text("활성 NextBSS에서 추출한 색상·간격·모서리·글자 스타일의 CSS 변수 선언입니다.",
                                "--nb-spacing-8: 8px;\n--nb-radius-6: 6px;", "tokenDeclarations"),
    "typography_groups": _detail("활성 NextBSS 글자 스타일 토큰의 그룹 이름입니다. 프롬프트에는 쉼표로 연결해 넣습니다.",
                                 "문자열 배열", ["--nb-typography-body-medium-14"], "typographyGroups"),
    "output_format": _fragment("묶음 이름에 맞춰 모델이 반환할 LWC HTML·CSS·JavaScript의 형식을 안내합니다.",
                               "Return a single JSON object containing html, css, and javascript.", "name"),
    "example": _object("모델이 반환해야 할 Screen 생성 결과의 형식 예시입니다. Agent가 조립하며 입력값은 아닙니다.", _SCREEN_OUTPUT),
    "payload": _fragment("원본 출처와 실제 읽은 내용에 따라 화면 근거를 묶어 넣는 프롬프트 부분입니다.",
                         "Figma MCP captured context for node 1:2:\nFrame 640 × 480; title: Sample screen",
                         "sourceContext / capturedContext / sourceSections"),
    "source_label": _fragment("원본이 Figma REST인지 로컬 .fig 사본인지 알려주는 표기입니다.", "Local Figma source", "sourceKind / local"),
    "source_identity": _fragment("원본 출처를 유지하고 다른 파일이나 노드를 사용하지 않도록 조립하는 지시입니다.",
                                 "Use only the supplied Figma source for the selected file and node."),
    "source_instruction": _fragment("Figma MCP, 저장된 분석, REST, 로컬 .fig 중 제공한 원본 근거를 사용하는 방법을 알려줍니다.",
                                    "Use the verified Figma source evidence supplied below."),
    "nodes_read": _detail("분할 조회에서 확인한 하위 Figma 노드 ID입니다. 프롬프트에서는 쉼표로 연결합니다.",
                          "문자열 배열", _COVERAGE["nodesRead"], "coverage.nodesRead"),
    "unresolved_nodes": _detail("분할 조회에서 근거를 확인할 수 없었던 하위 노드 ID입니다. 프롬프트에서는 쉼표로 연결합니다.",
                                "문자열 배열", _COVERAGE["unresolved"], "coverage.unresolved"),
    "split_layout": _detail("분할 조회에서 확인한 자식 노드의 부모 기준 좌표와 크기 근거입니다.", "JSON 객체 배열",
                            [{"id": "1:3", "parentId": "1:2", "x": 24, "y": 32, "width": 360, "height": 40}], "splitLayout"),
    "context_text": _text("읽어 온 Figma MCP 응답에서 수집한 원본 컨텍스트 텍스트입니다.", _SECTIONS[0]["text"],
                          "capturedContext[].text / sourceSections[].text"),
    "actions": _detail("원본에서 확인한 액션과 적용 범위, 연결된 입력 필드의 근거입니다.", "JSON 객체 배열",
                       [{"kind": "reset", "nodeId": "1:3", "label": "초기화", "scopeId": "1:2",
                         "fields": [{"nodeId": "1:4", "label": "검색어", "resetValue": ""}]}], "actions"),
    "layout_evidence": _fragment("화면 크기를 어느 원본에서 측정했는지 밝히는 지시문입니다.",
                                 "The verified Figma frame dimensions are measured", "sourceKind / local"),
    "width": _detail("측정한 Figma 화면 너비입니다. 단위는 px입니다.", "숫자", 640, "layoutDimensions.width"),
    "height": _detail("측정한 Figma 화면 높이입니다. 단위는 px입니다.", "숫자", 480, "layoutDimensions.height"),
    "history_tables": _detail("원본에서 검증된 이력 테이블의 노드 ID, 열 너비·순서와 빈 상태 근거입니다.",
                              "JSON 객체 배열", [{"nodeId": "1:3", "headerNodeId": "1:4", "box": [24, 80, 160, 96],
                                                 "columns": [{"nodeId": "1:5", "label": "이름", "width": 160,
                                                              "box": [24, 80, 160, 48]}],
                                                 "empty": {"nodeId": "1:6", "label": "조회 결과가 없습니다.",
                                                           "box": [24, 128, 160, 48]},
                                                 "emptyLabel": "조회 결과가 없습니다.", "rowCount": 0}], "historyTables"),
    "reviewed_analysis": _object("생성에 재사용하는 저장된 Screen 분석입니다. 원본과 NextBSS 기준, 치수, 컴포넌트 매핑, 검토 항목을 포함합니다.",
                                 _REVIEWED_ANALYSIS, "reviewedAnalysis"),
    "name": _text("생성할 NextBSS LWC 묶음의 API 이름입니다.", _BUNDLE_NAME, "name"),
    "diagnostic": _text("앞선 분석 또는 생성 결과가 검증을 통과하지 못한 이유입니다. 보정 요청에 사용합니다.",
                        "The LWC template is missing a required closing tag.", "diagnostic"),
    "prompt": _fragment("같은 원본으로 다시 보정할 수 있도록 기존 생성 근거에서 다시 조립한 전체 프롬프트입니다.",
                        "Generate the first LWC source draft for the selected Figma screen.", "evidence"),
    "previous_draft": _text("검증을 통과하지 못한 직전 모델 응답 원문입니다. JSON 형식이 깨진 응답도 보정 근거로 그대로 전달합니다.",
                            "```json\n" + json.dumps(_SCREEN_OUTPUT, ensure_ascii=False, indent=2) + "\n```", "previousDraft"),
    "source_digest": _text("파일럿에서 고정한 원본 패키지를 식별하는 SHA-256 값입니다. 16진수 64자로 전달합니다.",
                           _SOURCE_DIGEST, "sourceDigest"),
    "context": _text("추가로 저장된 Figma 구조나 속성이 있을 때 모델에 제공하는 원본 설명입니다.",
                     "Frame 640 × 480; heading at x=24, y=32.", "referenceContext"),
}


_BY_OPERATION = {
    "screen.review-plan": {
        "source": _object("Agent가 분석용으로 조립한 원본 근거입니다. 파일과 노드, 화면 크기, 조회 범위와 액션을 묶습니다. "
                          "출처에 따라 figmaContext, figmaRestSnapshot 또는 localSource 중 하나가 포함됩니다.",
                          {**_REFERENCE, "local": False, "dimensions": _DIMENSIONS, "coverage": None,
                           "splitLayout": [], "actions": [], "figmaContext": _SECTIONS}),
    },
    "screen.generate": {
        "evidence": _fragment("아코디언을 확인할 때 원본 출처별로 어떤 근거를 우선할지 알려주는 추가 지시입니다.",
                              "Use the verified Accordion state and parent-relative layout in the supplied source."),
        "reviewed": _fragment("저장한 Screen 분석이 있을 때만 그 내용을 원본 근거와 함께 반영하도록 조립하는 지시입니다.",
                              "Use the reviewed analysis as implementation evidence.", "reviewedAnalysis"),
    },
    "screen.analyze": {
        "arguments": _object("Agent가 각 노드에 대해 조립하는 Figma MCP 디자인 조회 인자입니다. 여러 노드는 한 줄에 JSON 객체 하나씩 "
                             "전달하며, excludeScreenshot은 includeScreenshot의 반대값입니다.",
                             {**_REFERENCE, "clientFrameworks": "LWC", "clientLanguages": "html,css,javascript",
                              "skillNames": "figma-design-to-code", "excludeScreenshot": False},
                             "fileKey / nodeIds / includeScreenshot"),
    },
    "screen.metadata": {
        "arguments": _object("Agent가 각 노드의 계층·레이어를 읽기 위해 조립하는 Figma MCP 메타데이터 조회 인자입니다. "
                             "여러 노드는 한 줄에 JSON 객체 하나씩 전달합니다.", _REFERENCE, "fileKey / nodeIds"),
    },
    "screen.capture": {
        "screenshot": _object("Figma 화면 캡처를 요청할 인자입니다. maxDimension으로 최대 크기를 제한하고, 노드 내용만 Base64로 받습니다.",
                              {**_REFERENCE, "maxDimension": 640, "enableBase64Response": True, "contentsOnly": True},
                              "fileKey / nodeId / maxDimension"),
        "reference": _object("배치도 함께 읽을 때 메타데이터 조회에서 사용할 파일과 노드입니다.", _REFERENCE, "fileKey / nodeId"),
        "metadata": _fragment("readLayout이 참일 때만 Figma 메타데이터를 캡처와 함께 읽도록 조립하는 지시입니다.",
                               'Also call get_metadata with {"fileKey":"ExampleFixture","nodeId":"1:2"}.', "readLayout"),
    },
    "screen.discovery": {
        "reference": _object("페이지·프레임을 탐색할 Figma 파일입니다. nodeId를 생략하면 파일의 최상위 페이지를 조회합니다.",
                             _REFERENCE, "fileKey / nodeId"),
        "kind": _fragment("nodeId가 있으면 페이지 내부 계층, 없으면 문서 최상위 페이지를 조회하도록 정하는 문구입니다.",
                          "page hierarchy", "nodeId"),
    },
    "screen.plan": {
        "source": _object("파일럿에서 수집한 Figma 화면의 파일·노드·리비전, 화면 크기와 레이어 목록입니다. "
                          "리비전을 확인하지 못한 MCP 캡처에는 version이 없을 수 있습니다.",
                          {**_REFERENCE, "version": "1234567890", **_DIMENSIONS,
                           "nodes": [{"id": "1:2", "parentId": None, "name": "Sample screen", "type": "FRAME",
                                      "x": 0, "y": 0, **_DIMENSIONS}]}, "source"),
    },
    "screen.plan.repair": {
        "diagnostic": _text("파일럿 분석 계획이 원본 근거 또는 출력 형식 검증을 통과하지 못한 이유입니다.",
                            "The layout plan refers to an unknown source node.", "diagnostic"),
    },
    "story.classification": {
        "evidence": _serialized("Story 보드의 업무 헤더, 화면 후보와 화면 간 연결입니다. 업무 그룹과 시작 화면을 분류하는 데 사용합니다.",
                                {"headings": [{"anchorId": "1:1", "title": "예제 업무", "sourceStart": "1:2"}],
                                 "screens": [{**_STORY_SCREEN, "sourceHeading": "1:1"}], "links": []}, "evidence"),
    },
    "story.analysis": {
        "evidence": _serialized("한 Story 업무의 시작점, 화면과 컴포넌트, 시나리오, 연결, 시각 근거를 묶은 실행 명세 분석 입력입니다.",
                                {"workflowId": "workflow-example", "title": "예제 업무", "startScreenId": "1:2",
                                 "screens": [{**_STORY_SCREEN, "frame": _DIMENSIONS, "blocks": []}],
                                 "cases": [], "links": []}, "evidence"),
        "categories": _serialized("Story 분석에서 살필 항목의 ID와 항목별 검토 지시입니다.",
                                  {"layout-geometry": "Capture screen-relative bounding boxes and parent coordinate space."}, "categories"),
        "max_facts": _detail("한 번의 Story 분석에서 모델이 반환할 수 있는 근거 항목의 상한입니다. 허용 범위는 1~100입니다.",
                             "정수", 48, "maxFacts"),
    },
    "story.generate": {
        "reviewed": _serialized("저장된 Story 실행 명세와 일치 검증용 해시, 묶음 이름, 배치와 NextBSS 실행 계약입니다.",
                                {"specDigest": _SOURCE_DIGEST, "bundleName": _STORY_BUNDLE,
                                 "spec": {"plan": {"startScreenId": "1:2", "screens": [_STORY_SCREEN]}},
                                 "geometry": {}, "nextBss": {"tokenModule": "c/nextBssTokens"}, "runtimeContract": {}}, "input"),
        "diagnostic": _serialized("직전 Story 코드 검증이 실패했을 때만 다시 요청하면서 보내는 오류 코드와 메시지입니다.",
                                   {"code": "STORY_KIRO_PREVIEW_COMPILE", "message": "Sample compilation failure"}, "diagnostic"),
        "output_contract": _object("Agent가 조립하는 기대 응답 형식입니다. 명세 해시와 생성 파일, 확인하지 못한 항목을 포함합니다.",
                                   {"specDigest": _SOURCE_DIGEST, "files": {
                                       f"{_STORY_BUNDLE}.html": "LWC template", f"{_STORY_BUNDLE}.css": "scoped CSS",
                                       f"{_STORY_BUNDLE}.js": "LWC JavaScript"},
                                    "unresolved": [{"kind": "missing", "screenId": "1:2", "scenarioId": "example-scenario",
                                                    "reason": "Source behavior is not specified.", "evidenceIds": [],
                                                    "nextAction": "Confirm the expected outcome."}]}),
    },
}


def prompt_variable_details(operation, variables):
    """Return operation-specific documentation for exactly the declared placeholders."""
    details = _COMMON | _BY_OPERATION.get(operation, {})
    return deepcopy({name: details[name] for name in variables})
