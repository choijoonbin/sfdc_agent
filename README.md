# SFDC Agent

`SFDC_Agent`는 Figma → LWC 파일럿의 입력 고정, 모델 분석, 기존 NOVA 생성기 호출, 화면·동작 비교를 처리한다. `SFDC_Ui/nextbss`에서 원본 파서, NextBSS 표준, LWC 생성기와 브라우저 검사기를 가져온다. 작업 상태는 SQLite에 기록하고, 모델 제안·LWC 번들·PNG·비교 보고서는 실행별 폴더에 저장한다.

## 시작

저장소 루트에서 실행한다.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -e .
.venv/bin/sfdc-agent pilot --provider kiro
```

Python 3.11 이상, Node, Chrome과 `SFDC_Ui`의 Node 의존성이 필요하다. 기본 입력은 형제 디렉터리 `SFDC_Ui`에 보관된 `AI 테스트용.fig`다. 다른 경로에 있는 UI 프로젝트는 `SFDC_UI_ROOT`로 지정한다. 실행은 `--target screen` 또는 `--target story`로 제한할 수 있다. CLI가 출력하는 작업 ID로 `sfdc-agent resume <ID>`를 실행하면 완료된 단계를 재사용한다.

Kiro는 현재 로컬 CLI 인증과 CLI에서 설정한 모델을 사용한다. OpenAI 연결은 `OPENAI_API_KEY`와 `OPENAI_MODEL`을 설정한 뒤 `--provider openai`로 명시적으로 선택한다. 새 공급자는 `ModelProvider.complete()` 계약으로 연결한다. 공급자 응답은 Python에서 구조를 검사하고, Story의 화면·액션·전이 근거는 기존 Node 검증기로 다시 확인한다.

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

별도 로컬 API가 필요한 경우 `sfdc-agent serve`로 8765에 실행하고 다른 터미널에서 `sfdc-agent worker`를 실행한다. `POST /runs`로 대기열에 넣고 `GET /runs/{id}`와 `GET /runs/{id}/comparison`으로 확인한다. API는 로컬 단일 사용자 파일럿용이다. 4175는 계속 SFDC_Ui에서 제공한다.

## 비교 입력

기준 경로와 에이전트 경로는 한 실행에서 동일한 Figma 사본, 정확한 활성 NextBSS v1.9 릴리스 ID, Story 케이스를 사용한다. Screen은 기존 측정 기반 생성기에 Kiro가 검토할 근거와 추가 확인 항목을 연결한다. Story는 Kiro 실행 명세를 기존 계획 검증기에 통과시킨 뒤 기존 생성기에 전달한다. 검증에서 거부된 명세는 보고서에서 표시한다.

기본 비교 기준은 동일 로컬 `.fig` 재구성이다. 공식 Figma 리비전의 1× PNG가 확보되면 `--reference-manifest <파일>`로 원본을 지정한다. 매니페스트의 `source`에는 `fileKey`, 로컬 사본 `sha256`, 확인한 Figma 리비전 `version`, 노드별 `pngs`가 들어간다. 각 PNG 항목은 `path`, `nodeId`, `fileKey`, `version`, `sha256`, `scale: 1`을 기록한다. 해시와 크기가 맞는 프레임을 공식 비교로 기록한다. 이미지와 검증 자료는 Git에 올리지 않는다.

## 4175에서 Kiro 실행

```sh
SFDC_AGENT_SCREEN_KIRO_MODE=headless SFDC_AGENT_STORY_KIRO_MODE=headless .venv/bin/sfdc-agent serve
```

`SFDC_Ui`는 기존 HTTPS 4175를 사용한다. Screen의 **Kiro 변환**, Story의 **Kiro 분석**과 **Kiro로 LWC 생성**은 기존 화면에서 실행한다. 4175의 Node 서버가 `127.0.0.1:8765`의 Python 서버로 모델 작업을 보낸다. 헤드리스 모드는 아래의 `KIRO_API_KEY` 설정을 사용한다. `acp` 모드에서는 Kiro CLI 로그인 상태가 필요하며, Screen에서 Figma MCP를 연결할 경우 브라우저에서 승인한다.

`kiro_gateway.py`가 세션·작업 요청을 관리하고, `screen_tasks.py`와 `story_tasks.py`가 근거에 맞는 분석·생성 프롬프트를 만든다. `kiro_acp.mjs`는 로컬 Kiro CLI와 통신한다. `SFDC_Ui`의 NextBSS 원본 처리, NOVA 생성, LWC 컴파일, 검토·저장, Salesforce 세션 경로를 계속 사용한다. 위 CLI 파일럿의 OpenAI 어댑터는 별도로 실행할 수 있다. 현재 4175의 대화형 작업은 Kiro를 사용한다.

상태는 `curl http://127.0.0.1:8765/health`로 확인한다. 기존 4175 프로세스가 변경 전 버전을 실행 중이면 같은 포트에서 Vite를 다시 실행한다. Vite가 다시 시작되면 메모리의 Salesforce 세션을 다시 연결해야 할 수 있다.

### Story Kiro API 키 실행

`SFDC_AGENT_STORY_KIRO_MODE=headless`를 Python 서버 실행 환경에 설정하면 Story의 Kiro 업무 분류·명세 분석·LWC 코드 생성에만 적용된다. 설정이 없거나 `acp`이면 현재 CLI 로그인 경로를 사용한다. Screen, Salesforce 인증, NOVA 생성과 CLI 파일럿에는 이 설정이 적용되지 않는다.

```sh
SFDC_AGENT_STORY_KIRO_MODE=headless .venv/bin/sfdc-agent serve
```

서버는 `KIRO_API_KEY` 환경 변수를 우선하고, 없으면 저장소 루트의 Git 제외 파일 `.env.kiro.local`에서 `KIRO_API_KEY=...` 한 줄을 읽는다. 파일은 실행 사용자 소유의 일반 파일이어야 하며 권한은 `0600`으로 유지한다. 서버에서 운영할 때는 시크릿 저장소로 환경 변수를 주입한다.

Story 원본은 기존 `.fig` 또는 Figma REST 경로로 수집한다. 헤드리스 모델 실행은 임시 작업공간을 쓰며, Kiro CLI가 API 키 인증을 확인한 경우에만 원본 근거를 표준 입력으로 받는다. 모델 작업에는 Figma MCP를 연결하지 않는다. 저장된 Story의 생성 영수증 `generationReceipt.executionMode`와 Agent 로그로 실제 실행 방식을 확인한다. 새 Story 분석으로 검증하면 저장된 LWC 결과를 재사용하지 않는다. 기본 모드로 돌아가려면 Python 서버를 `SFDC_AGENT_STORY_KIRO_MODE=acp`로 시작한다.

### Screen Kiro API 키 실행

Screen에도 같은 키를 사용하려면 두 기능의 모드를 각각 지정한다.

```sh
SFDC_AGENT_SCREEN_KIRO_MODE=headless SFDC_AGENT_STORY_KIRO_MODE=headless .venv/bin/sfdc-agent serve
```

Screen은 Figma REST에서 선택한 노드의 원본을 읽는다. REST를 사용할 수 없고 버전을 별도로 지정하지 않은 경우에는 일치하는 등록 `.fig` 사본을 사용한다. 저장된 원본 근거를 헤드리스 분석과 LWC 생성에 전달한다. 검토가 필요한 분석은 기존 화면에서 검토한 뒤 같은 분석으로 생성한다. 원본을 확보할 수 없으면 변환 화면에 원인을 표시한다. `/health`의 `screenKiroMode`와 `storyKiroMode`에서 각 기능의 설정을 확인한다.
