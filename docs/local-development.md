# 로컬 개발과 CLI

Main은 [BACK 통합 실행기](../../SFDC_BACK/docs/local-live-development.md)로 기동하며 Agent 단독 실행도 같은 BACK 설정을 읽습니다. 최초 설치와 `serve`는 [README](../README.md)를 따릅니다. 모든 아래 명령은 Agent 저장소 루트에서 실행합니다. 서버 이미지 설정은 [배포 안내](../deployment/README.md)를 사용합니다.

## 독립 비교 파일럿

`pilot`은 동일한 입력으로 기준 변환과 모델 제안을 비교합니다. Main의 작업·활성 프롬프트·권한을 바꾸지 않으며, 로컬 SQLite와 `.pilot/runs/<ID>/`에 분석·LWC·PNG·비교 보고서를 저장합니다. Node·Chrome과 형제 UI의 잠긴 npm 의존성이 필요합니다.

| 환경 변수 | 명시적 입력 |
| --- | --- |
| `NOVA_PILOT_FIGMA_SOURCE` | 비교 대상 `.fig` 원본 |
| `SFDC_MANAGED_RELEASE_ROOT` | 검증할 불변 릴리스 아카이브와 파일럿의 활성 포인터 |
| `NOVA_PILOT_GUIDE_FIG` | 승인된 Guide 원본 |
| `NOVA_PILOT_LIBRARY_FIG` | 승인된 Components Library 원본 |
| `SFDC_UI_ROOT` | 선택적인 UI 엔진 Git 경로 |

입력 원본은 릴리스의 SHA-256과 대조합니다. 경로가 없으면 중단하며 UI 내부의 숨김 파일이나 Main의 활성 릴리스를 추측하지 않습니다. 이미 보관한 원본을 읽기 전용 입력으로 지정할 수 있으며 UI 폴더에 다시 복사할 필요가 없습니다. 입력 경로만 자식 Node 요청에 전달하고 키·서버 환경 전체는 전달하지 않습니다.

```sh
.venv/bin/sfdc-agent pilot --provider kiro --target screen
.venv/bin/sfdc-agent resume <32자리-run-ID>
```

`--target`은 `all`, `screen`, `story`입니다. `resume`은 같은 작업의 완료 단계를 재사용합니다. `worker --once`는 독립 파일럿 큐를 처리합니다. 관리형 서버는 파일럿 `/runs` API를 제공하지 않으며 제품 작업은 Spring에서 제출·조회합니다.

기본 Kiro 파일럿은 `KIRO_API_KEY`와 CLI headless 실행을 사용합니다. OpenAI는 `OPENAI_API_KEY`, `OPENAI_MODEL`을 설정하고 `--provider openai`로 선택합니다. 새 텍스트 공급자는 `ModelProvider.complete()` 계약에 연결합니다. Python이 응답 구조를 확인하고 Story의 화면·액션·전이 근거는 기존 Node 검증기로 다시 확인합니다.

한 비교는 같은 Figma 사본·정확한 불변 릴리스·Story 케이스를 사용합니다. 기본 기준은 로컬 `.fig` 재구성입니다. 공식 동일 리비전 1× PNG가 확보되면 `pilot --reference-manifest <파일>`로 지정합니다. 매니페스트의 `source`에는 `fileKey`, 로컬 사본 `sha256`, 확인한 리비전 `version`, 노드별 `pngs`를 기록합니다. PNG마다 `path`, `nodeId`, `fileKey`, `version`, `sha256`, `scale: 1`이 필요합니다. 해시·크기·리비전이 일치하는 근거만 공식 비교로 다루며 이미지·모델 응답·비밀 검증 자료는 Git에 올리지 않습니다.

## Azure OpenAI

Azure는 Entra ID `client_credentials` 토큰과 AI Hub 구독 키로 Chat Completions를 호출하는 명시적 파일럿·텍스트 평가 공급자입니다. 분석 스키마와 NOVA 생성기는 그대로 사용합니다.

| 설정 | 키 |
| --- | --- |
| AI Hub 주소·API 버전·채팅 배포 | `SFDC_AGENT_AZURE_BASE_URL`, `SFDC_AGENT_AZURE_API_VERSION`, `SFDC_AGENT_AZURE_CHAT_MODEL` |
| Entra 인증 | `SFDC_AGENT_AZURE_TENANT_ID`, `SFDC_AGENT_AZURE_CLIENT_ID`, `SFDC_AGENT_AZURE_CLIENT_SECRET`, `SFDC_AGENT_AZURE_TOKEN_SCOPE` |
| 구독 키 | `SFDC_AGENT_AZURE_SUBSCRIPTION_KEY` |
| 선택 제한 | `SFDC_AGENT_AZURE_TOKEN_TIMEOUT_SECONDS` (기본 30), `SFDC_AGENT_AZURE_REQUEST_TIMEOUT_SECONDS` (기본 120), `SFDC_AGENT_AZURE_MAX_COMPLETION_TOKENS` (기본 16384), `SFDC_AGENT_AZURE_REASONING_EFFORT` |

환경 변수 또는 Git 제외 파일 `.env.azure.local`에 `KEY=VALUE`로 설정합니다. 환경 변수가 우선하며 파일은 0600으로 유지합니다. Azure를 선택할 때만 읽습니다. 다음 명령은 필수 값·형식만 검사하고 인증·모델 API는 호출하지 않습니다.

```sh
.venv/bin/sfdc-agent check-azure
```

실제 사용은 `pilot --provider azure`로 선택합니다. `--model`은 Azure 배포 이름입니다. Azure 설정과 파일럿 제한 중 짧은 시간을 적용하며 Entra 토큰은 만료 전에 갱신합니다. 응답의 완료 상태·토큰 사용량·지연을 기록합니다.

## 독립 프롬프트 평가와 복구

CLI 프롬프트 저장소는 `.prompt-management/prompts.sqlite3`이며 `SFDC_AGENT_PROMPT_DB`로 바꿀 수 있습니다. Main DB의 프롬프트·권한과 별개입니다. 최초 릴리스 `prompts-v1`과 패키지 불변 템플릿을 보존하며, 진행 중 작업·저장 분석은 시작할 때의 핀을 유지합니다. 릴리스 정보가 없는 이전 CLI 분석은 최초 버전을 사용합니다.

```sh
.venv/bin/sfdc-agent evaluate-prompt story.classification fixture.json --provider azure --release prompts-v1
```

`--release`를 생략하면 CLI 저장소의 활성 릴리스를 사용합니다. 결과에는 프롬프트 버전·해시와 모델 응답이 포함됩니다. 원본은 모델 실행 전에 허가된 REST·Plan 토큰 또는 등록한 로컬 원본으로 확보해야 합니다.

CLI 작업을 다른 환경으로 옮기거나 복구할 때는 실행을 중지하고 SQLite 백업 API로 작업 DB·프롬프트 DB의 일관된 사본을 만듭니다. 참조된 `.pilot/runs/` 산출물·원본·불변 릴리스도 함께 보존합니다. 사용자별 MCP 인증 자료는 [MCP 저장·철회 절차](figma-mcp-setup.md)를 따르며 다른 사용자의 인증 HOME로 대체하지 않습니다.

## 변경 검증

전체 단위 게이트는 실제 형제 UI runtime과 고정 fixture를 사용합니다. 준비할 불변 UI commit·CI read token은 [CI 런타임 계약](../.github/RUNTIME_CONTRACTS.md)을 따릅니다.

```sh
.venv/bin/python scripts/verify-unit-contracts.py --ui-root ../SFDC_Ui --report /absolute/private/agent-proof.json
```

검증기는 provider 자격을 제거하고 Node 모듈·fixture hash와 누락/skip을 확인합니다. 이 결과는 실제 provider 인증·Figma 수집·Salesforce 적용 인수를 대신하지 않습니다. 문서만 변경한 경우 링크·실행 파일 참조·diff 검증으로 범위를 제한합니다.
