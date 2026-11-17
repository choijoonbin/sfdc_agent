# SFDC Agent

`SFDC_Agent`는 NOVA의 Screen·Story Kiro 분석과 LWC 코드 생성을 처리한다. CLI 파일럿은 `SFDC_Ui/nextbss`의 원본 파서, NextBSS 표준, LWC 생성기와 브라우저 검사기를 사용해 변환 결과를 비교한다. 파일럿 작업 상태는 SQLite에 기록하고, 모델 제안·LWC 번들·PNG·비교 보고서는 실행별 폴더에 저장한다.

## NOVA 4175용 에이전트 서버 실행

모든 명령은 `SFDC_Agent` 저장소 루트에서 실행한다. Python 3.11 이상과 로컬 Kiro CLI가 필요하다.

### 최초 설치

이미 `.venv`와 의존성을 설치했다면 서버 기동 단계부터 진행한다.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -e .
```

### 서버 기동

Screen과 Story에서 API 키로 Kiro를 실행하려면 `KIRO_API_KEY` 환경 변수를 설정한다. 환경 변수가 없으면 저장소 루트의 Git 제외 파일 `.env.kiro.local`에서 `KIRO_API_KEY=...` 한 줄을 읽는다. 이 파일은 실행 사용자 소유의 일반 파일이어야 하며 권한은 `0600`으로 유지한다. 서버 환경에서는 시크릿 저장소로 환경 변수를 주입한다.

```sh
SFDC_AGENT_SCREEN_KIRO_MODE=headless \
SFDC_AGENT_STORY_KIRO_MODE=headless \
.venv/bin/sfdc-agent serve
```

Agent는 `127.0.0.1:8765`에서 실행된다. NOVA를 처음 띄우려면 별도 터미널에서 `SFDC_Ui` 저장소의 `npm run dev`를 실행한다. `https://localhost:4175`에서 **Screen to LWC → Kiro 변환** 또는 **Story to LWC → Kiro 분석·LWC 생성**을 실행하는 동안 Agent 터미널을 열어 둔다.

다른 터미널에서 서버 상태를 확인한다.

```sh
curl -fsS http://127.0.0.1:8765/health
```

두 기능을 위 명령으로 기동하면 `status`는 `ready`, `screenKiroMode`와 `storyKiroMode`는 `headless`로 표시된다. 포트가 이미 사용 중이면 먼저 이 명령으로 기존 서버 상태를 확인한다. 실행 모드나 환경 변수를 바꿔야 할 때는 기존 Agent 터미널에서 `Ctrl+C`로 종료하고 다시 기동한다.

### Kiro CLI 로그인 사용

`SFDC_AGENT_SCREEN_KIRO_MODE`와 `SFDC_AGENT_STORY_KIRO_MODE`의 기본값은 각각 `acp`다. 로컬 Kiro CLI 로그인으로 두 기능을 실행하려면 두 값을 `acp`로 지정한다. Screen에서 Figma MCP 연결이 필요하면 브라우저에서 승인한다.

```sh
SFDC_AGENT_SCREEN_KIRO_MODE=acp \
SFDC_AGENT_STORY_KIRO_MODE=acp \
.venv/bin/sfdc-agent serve
```

### Screen·Story 원본 처리

Screen은 Figma REST에서 선택한 노드의 원본을 읽는다. REST를 사용할 수 없고 버전을 별도로 지정하지 않은 경우에는 일치하는 등록 `.fig` 사본을 사용한다. Story 원본은 기존 `.fig` 또는 Figma REST 경로로 수집한다. 헤드리스 모델 작업은 임시 작업공간에서 실행하며, Kiro CLI의 API 키 인증을 확인한 뒤 원본 근거를 표준 입력으로 전달한다. 헤드리스 작업의 Figma 원본은 모델 실행 전에 수집한다.

Screen에서 검토가 필요한 경우, 분석을 확인한 뒤 저장된 분석으로 생성한다. 원본을 확보할 수 없으면 변환 화면에 원인을 표시한다. Story의 실행 모드는 생성 영수증 `generationReceipt.executionMode`와 Agent 로그에서 확인한다. 새 Story 분석으로 검증하면 저장된 LWC 결과를 재사용하지 않는다.

`kiro_gateway.py`가 세션·작업 요청을 관리하고, `screen_tasks.py`와 `story_tasks.py`가 분석·생성 프롬프트를 구성한다. `kiro_acp.mjs`는 로컬 Kiro CLI와 통신한다. 원본 처리, NOVA 생성, LWC 컴파일, 검토·저장과 Salesforce 세션은 `SFDC_Ui`에서 처리한다. UI 연동 코드가 바뀌어 Vite를 다시 시작한 경우 Salesforce 세션을 다시 연결해야 할 수 있다.

## CLI 비교 파일럿 실행

`pilot`은 비교 작업을 한 번 실행하고 결과를 저장한다. 최초 설치를 마친 뒤 실행한다.

```sh
.venv/bin/sfdc-agent pilot --provider kiro
```

비교 파일럿에는 Node, Chrome과 `SFDC_Ui`의 Node 의존성도 필요하다. 기본 입력은 형제 디렉터리 `SFDC_Ui`에 보관된 `AI 테스트용.fig`다. 다른 경로에 있는 UI 프로젝트는 `SFDC_UI_ROOT`로 지정한다. 실행은 `--target screen` 또는 `--target story`로 제한할 수 있다. CLI가 출력하는 작업 ID로 `.venv/bin/sfdc-agent resume <ID>`를 실행하면 완료된 단계를 재사용한다.

Kiro 파일럿은 로컬 CLI 인증과 CLI에서 설정한 모델을 사용한다. OpenAI 연결은 `OPENAI_API_KEY`와 `OPENAI_MODEL`을 설정한 뒤 `--provider openai`로 명시적으로 선택한다. 새 공급자는 `ModelProvider.complete()` 계약으로 연결한다. 공급자 응답은 Python에서 구조를 검사하고, Story의 화면·액션·전이 근거는 기존 Node 검증기로 다시 확인한다.

로컬 API로 비교 작업을 제출할 때는 Agent 서버를 기동한 뒤 다른 터미널에서 `.venv/bin/sfdc-agent worker`를 실행한다. `POST /runs`로 작업을 등록하고 `GET /runs/{id}`와 `GET /runs/{id}/comparison`으로 진행 상태와 결과를 확인한다.

## Azure OpenAI 준비

Azure 공급자는 기존 Screen·Story 파일럿의 분석 인터페이스를 사용한다. Dthub의 AI Hub와 같은 방식으로 Entra ID `client_credentials` 토큰을 받고, Azure OpenAI Chat Completions 요청에 Bearer 토큰과 AI Hub 구독 키를 함께 보낸다. 분석 결과는 기존 스키마로 검증하며 LWC는 NOVA 생성기로 만든다. 4175의 대화형 버튼과 기본 파일럿 공급자는 Kiro를 사용한다.

| 설정 | 키 |
| --- | --- |
| AI Hub 주소·API 버전·채팅 배포 이름 | `SFDC_AGENT_AZURE_BASE_URL`, `SFDC_AGENT_AZURE_API_VERSION`, `SFDC_AGENT_AZURE_CHAT_MODEL` |
| Entra 인증 | `SFDC_AGENT_AZURE_TENANT_ID`, `SFDC_AGENT_AZURE_CLIENT_ID`, `SFDC_AGENT_AZURE_CLIENT_SECRET`, `SFDC_AGENT_AZURE_TOKEN_SCOPE` |
| AI Hub 구독 키 | `SFDC_AGENT_AZURE_SUBSCRIPTION_KEY` |
| 선택 설정 | `SFDC_AGENT_AZURE_TOKEN_TIMEOUT_SECONDS` (기본 30), `SFDC_AGENT_AZURE_REQUEST_TIMEOUT_SECONDS` (기본 120), `SFDC_AGENT_AZURE_MAX_COMPLETION_TOKENS` (기본 16384), `SFDC_AGENT_AZURE_REASONING_EFFORT` |

환경 변수 또는 Git에서 제외되는 저장소 루트의 `.env.azure.local`에 설정한다. 환경 변수가 로컬 파일보다 우선한다. 로컬 파일은 줄마다 `KEY=VALUE`를 쓰고, 파일 권한은 `0600`으로 유지한다. 설정은 Azure를 명시적으로 선택할 때 읽는다. 다음 명령은 필수 값과 형식만 확인하며 인증 또는 모델 API를 호출하지 않는다.

```sh
.venv/bin/sfdc-agent check-azure
```

실제 사용을 결정한 뒤 `pilot --provider azure`로 선택한다. `--model`은 Azure 배포 이름으로 해석된다. 요청에는 Azure 설정과 파일럿 시간 제한 중 더 짧은 값이 적용된다. Entra 토큰은 만료 전에 갱신하고, 응답 처리에서는 완료 상태와 토큰 사용량·지연시간을 기록한다.

## 작업 구조

| 위치 | 책임 |
| --- | --- |
| `src/sfdc_agent/contracts.py` | 요청과 모델 응답 구조 |
| `src/sfdc_agent/providers.py` | 공급자 선택, Kiro ACP 및 선택형 OpenAI Responses API 연결 |
| `src/sfdc_agent/azure_provider.py` | AI Hub 인증·토큰 캐시 및 선택형 Azure Chat Completions 연결 |
| `src/sfdc_agent/pilot.py` | 분석, 기준 변환, 모델 제안, 생성과 검증의 순서 |
| `src/sfdc_agent/storage.py` | 작업 상태와 재개 정보 저장 |
| `bridge/` | 원본·릴리스 검증, Screen·Story 생성, 실제 LWC 캡처 및 동작 검사 |
| `.pilot/runs/<ID>/comparison.json` | 동일 원본에 대한 두 경로의 결과와 산출물 위치 |

## 비교 입력

기준 경로와 에이전트 경로는 한 실행에서 동일한 Figma 사본, 정확한 활성 NextBSS v1.9 릴리스 ID, Story 케이스를 사용한다. Screen은 기존 측정 기반 생성기에 Kiro가 검토할 근거와 추가 확인 항목을 연결한다. Story는 Kiro 실행 명세를 기존 계획 검증기에 통과시킨 뒤 기존 생성기에 전달한다. 검증에서 거부된 명세는 보고서에서 표시한다.

기본 비교 기준은 동일 로컬 `.fig` 재구성이다. 공식 Figma 리비전의 1× PNG가 확보되면 `--reference-manifest <파일>`로 원본을 지정한다. 매니페스트의 `source`에는 `fileKey`, 로컬 사본 `sha256`, 확인한 Figma 리비전 `version`, 노드별 `pngs`가 들어간다. 각 PNG 항목은 `path`, `nodeId`, `fileKey`, `version`, `sha256`, `scale: 1`을 기록한다. 해시와 크기가 맞는 프레임을 공식 비교로 기록한다. 이미지와 검증 자료는 Git에 올리지 않는다.

## 프롬프트 관리

4175 개발 작업공간의 상단 사용자 메뉴에서 **프롬프트 관리**를 열면 Screen 9종, Story 3종의 활성 본문과 사용 가능한 변수를 볼 수 있다. **초안 저장 → 검증 → 활성화**로 적용하고, 이력에서 이전에 활성화한 버전을 복원한다. 검증은 템플릿 변수와 코드에 포함된 합성 입력의 렌더링을 검사한다. 실제 Figma 변환 품질은 모델 시험과 Screen·Story 회귀 검사에서 확인한다.

최초 릴리스 `prompts-v1`은 `src/sfdc_agent/prompt_templates_v1/`에 보존한다. 초안과 확정 버전, 활성 릴리스, 변경 이력은 Agent가 `.prompt-management/prompts.sqlite3`에 저장한다. `SFDC_AGENT_PROMPT_DB`로 별도 경로를 지정할 수 있다. 진행 중인 작업과 저장된 분석은 생성·보정에서 시작할 때의 릴리스를 사용한다. 릴리스 정보가 없는 이전 분석은 최초 버전을 사용한다. 변경 이력을 다른 환경으로 옮길 때는 SQLite 백업을 사용한다.

4175 서버와 Agent에 같은 서버 전용 `SFDC_AGENT_PROMPT_ADMIN_TOKEN`을 설정하고, 4175에 편집과 활성화를 담당할 Salesforce 사용자 ID를 `SFDC_UI_PROMPT_EDITOR_IDS`, `SFDC_UI_PROMPT_PUBLISHER_IDS`로 지정한다. 로컬 파일로 관리할 경우 Agent는 `.prompt-management/admin-token`을 읽고, 4175는 `SFDC_Ui/.prompt-management/access.json`의 `token`, `editorIds`, `publisherIds`를 읽는다. 두 파일은 실행 사용자 소유와 `0600` 권한을 사용한다. 설정한 계정이 Salesforce에 연결된 세션에서 편집하거나 활성화할 수 있다.

같은 릴리스의 텍스트 프롬프트를 다른 모델에서 시험하려면 테스트 근거를 담은 JSON으로 아래 명령을 실행한다. `--release`를 생략하면 활성 릴리스를 사용한다. 결과는 사용한 프롬프트 버전·해시와 모델 응답 JSON을 포함한다. Figma MCP 도구를 직접 호출하는 작업은 기존 Kiro 경로에서 실행한다.

```sh
.venv/bin/sfdc-agent evaluate-prompt story.classification fixture.json --provider azure --release prompts-v1
```
