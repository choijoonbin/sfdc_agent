# SFDC Agent

`SFDC_Agent`는 NOVA의 Screen·Story Kiro 분석과 LWC 코드 생성을 처리한다. CLI 파일럿은 `SFDC_Ui/nextbss`의 원본 파서, NextBSS 표준, LWC 생성기와 브라우저 검사기를 사용해 변환 결과를 비교한다. 파일럿 작업 상태는 SQLite에 기록하고, 모델 제안·LWC 번들·PNG·비교 보고서는 실행별 폴더에 저장한다.

## Spring 통합 실시간 개발 (`dev`)

프론트·백엔드·Agent는 모두 `dev` 브랜치에서 개발합니다. Main 진입점은 `https://localhost:4186`이며, Spring은 `127.0.0.1:8080`, Agent는 `127.0.0.1:8766`을 사용합니다. `SFDC_BACK`에서 `python3 scripts/live-development.py start`로 실행하면 각 PRIMARY 소스의 UI 변경, Java 변경, Python 변경을 반영합니다. 기존 PostgreSQL·Keycloak과 사용자·권한·업무 데이터·연동 설정을 그대로 사용합니다.

실행·종료·검증·로그 위치는 [통합 개발 안내](../SFDC_BACK/docs/local-live-development.md)를 확인하세요. 백엔드 없이 동작하던 프론트·Agent와 해당 웹 실행 안내는 [`dev_fe` 브랜치](https://github.com/choijoonbin/sfdc_agent/blob/dev_fe/README.md)에 보존합니다.

## 개발 준비와 실행

모든 명령은 `SFDC_Agent` 저장소 루트에서 실행한다. Python 3.11 이상과 로컬 Kiro CLI가 필요하다.

### 최초 설치

이미 `.venv`와 의존성을 설치했다면 서버 기동 단계부터 진행한다.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -e .
```

### Main 통합 환경 기동

`SFDC_BACK`에서 통합 시작 명령을 실행한다. Main Agent의 실행 모드와 자격 증명 참조는 통합 환경 설정으로 관리한다.

```sh
cd ../SFDC_BACK
python3 scripts/live-development.py start
```

Agent 상태는 다음 주소에서 확인한다.

```sh
curl -fsS http://127.0.0.1:8766/health
```

### Agent만 단독 기동

프론트는 `npm run dev`, Spring은 IntelliJ에서 실행하고, Agent는 이 저장소에서 아래 명령으로 실행한다. Agent 서버는 `127.0.0.1:8766` 하나를 사용한다.

`serve`는 형제 `SFDC_BACK`의 기존 로컬 설정 로더를 통해 관리형 인증, 서비스 토큰, Figma 작업공간 범위, 원본·릴리스·Node 엔진 경로와 자격 증명 파일 참조를 통합 실행과 동일하게 읽는다. 설정을 Agent 쪽에 복제하지 않는다. Docker·Spring·UI를 시작하거나 DB를 초기화하지 않으며, Python 소스 변경은 자동 재로딩한다. 최초 통합 환경 준비가 완료된 로컬 설치에서 사용한다.

```sh
.venv/bin/sfdc-agent serve
```

다른 터미널에서 서버 상태를 확인한다.

```sh
curl -fsS http://127.0.0.1:8766/health
```

기동하면 `status: ready`, `mode: managed`, `screenKiroMode: headless`, `storyKiroMode: headless`가 표시된다. 서버를 시작하지 않고 설정만 확인하려면 `.venv/bin/sfdc-agent serve --check`를 실행한다. 통합 실행기로 시작한 Agent가 이미 있다면 `python3 ../SFDC_BACK/scripts/live-development.py stop-agent`로 그 Agent만 종료한 뒤 단독 실행한다. 직접 실행한 Agent는 해당 터미널의 `Ctrl+C`로 종료한다. 포트 사용 중에는 다른 포트로 우회하지 않고 기동을 거부한다.

서버 배포는 기존 서비스 환경 변수와 `uvicorn sfdc_agent.api:app --host 127.0.0.1 --port 8766`을 사용한다. 서비스 토큰과 관리형 모드 등 필수 설정은 배포 환경에서 주입한다. 로컬 `serve` 명령은 기존 로컬 설치 전용이다.

### Screen·Story 원본 처리

Screen은 Figma REST에서 선택한 노드의 원본을 읽는다. REST를 사용할 수 없고 버전을 별도로 지정하지 않은 경우에는 일치하는 등록 `.fig` 사본을 사용한다. Story 원본은 기존 `.fig` 또는 Figma REST 경로로 수집한다. 헤드리스 모델 작업은 임시 작업공간에서 실행하며, Kiro CLI의 API 키 인증을 확인한 뒤 원본 근거를 표준 입력으로 전달한다. 헤드리스 작업의 Figma 원본은 모델 실행 전에 수집한다.

Screen에서 검토가 필요한 경우, 분석을 확인한 뒤 저장된 분석으로 생성한다. 원본을 확보할 수 없으면 변환 화면에 원인을 표시한다. Story의 실행 모드는 생성 영수증 `generationReceipt.executionMode`와 Agent 로그에서 확인한다. 새 Story 분석으로 검증하면 저장된 LWC 결과를 재사용하지 않는다.

`kiro_gateway.py`가 모델 세션·작업 요청을 관리하고, `kiro_headless.py`가 Key를 격리된 Kiro CLI headless 프로세스에 전달한다. `screen_tasks.py`와 `story_tasks.py`가 분석·생성 프롬프트를 구성한다. Agent는 `SFDC_Ui/nextbss` 엔진을 사용해 원본 처리·NOVA 생성·LWC 컴파일을 수행한다. Main의 작업 상태·결과 저장·권한·Salesforce 세션은 Spring이 관리하고, 프론트는 화면과 API 연결을 담당한다.

## CLI 비교 파일럿 실행

`pilot`은 비교 작업을 한 번 실행하고 결과를 저장한다. 최초 설치를 마친 뒤 실행한다.

```sh
.venv/bin/sfdc-agent pilot --provider kiro
```

비교 파일럿에는 Node, Chrome과 `SFDC_Ui`의 Node 의존성도 필요하다. 원본·Guide·Library 파일과 불변 릴리스 경로를 [파일럿 입력 안내](docs/local-pilot-inputs.md)에 따라 먼저 지정한다. UI의 과거 숨김 저장소를 기본 입력으로 사용하지 않는다. 다른 경로에 있는 UI 프로젝트는 `SFDC_UI_ROOT`로 지정한다. 실행은 `--target screen` 또는 `--target story`로 제한할 수 있다. CLI가 출력하는 작업 ID로 `.venv/bin/sfdc-agent resume <ID>`를 실행하면 완료된 단계를 재사용한다.

Kiro 파일럿은 `KIRO_API_KEY`와 CLI headless 모드를 사용한다. OpenAI 연결은 `OPENAI_API_KEY`와 `OPENAI_MODEL`을 설정한 뒤 `--provider openai`로 명시적으로 선택한다. 새 공급자는 `ModelProvider.complete()` 계약으로 연결한다. 공급자 응답은 Python에서 구조를 검사하고, Story의 화면·액션·전이 근거는 기존 Node 검증기로 다시 확인한다.

비교 파일럿은 `pilot`과 `resume` CLI로 실행한다. 현재 관리형 서버는 파일럿 `/runs` API를 제공하지 않으며, 제품 변환 작업은 Spring에서 제출하고 조회한다.

## Azure OpenAI 준비

Azure 공급자는 기존 Screen·Story 파일럿의 분석 인터페이스를 사용한다. Dthub의 AI Hub와 같은 방식으로 Entra ID `client_credentials` 토큰을 받고, Azure OpenAI Chat Completions 요청에 Bearer 토큰과 AI Hub 구독 키를 함께 보낸다. 분석 결과는 기존 스키마로 검증하며 LWC는 NOVA 생성기로 만든다. 기본 파일럿 공급자는 Kiro이며 Azure는 명시적으로 선택한다.

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
| `src/sfdc_agent/providers.py` | 공급자 선택, Kiro Key headless 및 선택형 OpenAI Responses API 연결 |
| `src/sfdc_agent/azure_provider.py` | AI Hub 인증·토큰 캐시 및 선택형 Azure Chat Completions 연결 |
| `src/sfdc_agent/provider_capabilities.py` | 공급자별 실제 전송 기능과 모델·CLI 실행 영수증 |
| `src/sfdc_agent/prompt_projection.py` | 원본을 유지한 단계별 근거·공개 API 설명 투영과 릴리스별 제한 캐시 |
| `src/sfdc_agent/provider_observation.py` | 응답 본문을 저장하지 않는 클라이언트 지연·명시적 토큰 사용량 계측 |
| `src/sfdc_agent/execution_diagnostics.py` | Worker 실패에서 공개 가능한 코드·단계·재시도 시각 정보만 전달 |
| `src/sfdc_agent/nova_prompt_evidence.py` | 고정 NOVA 공개 계약과 모델 입력의 최종 경계 검증 |
| `src/sfdc_agent/pilot.py` | 분석, 기준 변환, 모델 제안, 생성과 검증의 순서 |
| `src/sfdc_agent/storage.py` | 작업 상태와 재개 정보 저장 |
| `bridge/` | 원본·릴리스 검증, Screen·Story 생성, 실제 LWC 캡처 및 동작 검사 |
| `.pilot/runs/<ID>/comparison.json` | 동일 원본에 대한 두 경로의 결과와 산출물 위치 |

원본·공통 지침·모델 어댑터의 경계와 영수증 필드는 [변환 근거 계약](docs/conversion-evidence-contract.md), 공통 브라우저 검증 구조는 [SFDC_UI 변환 품질 구조](../SFDC_Ui/docs/conversion-quality-architecture.md)를 참고합니다.

## 비교 입력

기준 경로와 에이전트 경로는 한 실행에서 동일한 Figma 사본, 정확한 활성 NextBSS v1.9 릴리스 ID, Story 케이스를 사용한다. Screen은 기존 측정 기반 생성기에 Kiro가 검토할 근거와 추가 확인 항목을 연결한다. Story는 Kiro 실행 명세를 기존 계획 검증기에 통과시킨 뒤 기존 생성기에 전달한다. 검증에서 거부된 명세는 보고서에서 표시한다.

기본 비교 기준은 동일 로컬 `.fig` 재구성이다. 공식 Figma 리비전의 1× PNG가 확보되면 `--reference-manifest <파일>`로 원본을 지정한다. 매니페스트의 `source`에는 `fileKey`, 로컬 사본 `sha256`, 확인한 Figma 리비전 `version`, 노드별 `pngs`가 들어간다. 각 PNG 항목은 `path`, `nodeId`, `fileKey`, `version`, `sha256`, `scale: 1`을 기록한다. 해시와 크기가 맞는 프레임을 공식 비교로 기록한다. 이미지와 검증 자료는 Git에 올리지 않는다.

## 프롬프트 관리

Main의 **프롬프트 관리**는 Spring API와 PostgreSQL을 기준으로 초안·릴리스·활성 버전·변경 이력과 편집 권한을 관리한다. Agent는 Spring이 실행별로 고정해 전달한 프롬프트 릴리스와 해시를 사용한다. 템플릿 검증은 변수와 입력 구조를 검사하고, 실제 Figma 변환 품질은 모델 시험과 Screen·Story 회귀 검사에서 확인한다.

독립 CLI 파일럿은 별도의 SQLite 프롬프트 저장소 `.prompt-management/prompts.sqlite3`를 사용하며 `SFDC_AGENT_PROMPT_DB`로 경로를 지정할 수 있다. 이 저장소는 Main의 활성 프롬프트와 권한을 변경하지 않는다. 최초 릴리스 `prompts-v1`은 `src/sfdc_agent/prompt_templates_v1/`에 보존한다. CLI에서 진행 중인 작업과 저장된 분석은 시작할 때의 릴리스를 사용하며, 릴리스 정보가 없는 이전 분석은 최초 버전을 사용한다. 독립 CLI 이력을 다른 환경으로 옮길 때는 SQLite 백업을 사용한다.

독립 CLI에서 같은 릴리스의 텍스트 프롬프트를 다른 모델로 시험하려면 테스트 근거를 담은 JSON으로 아래 명령을 실행한다. `--release`를 생략하면 CLI 저장소의 활성 릴리스를 사용한다. 결과는 사용한 프롬프트 버전·해시와 모델 응답 JSON을 포함한다. Figma 원본은 모델 실행 전에 REST·Plan Access Token 또는 등록된 로컬 원본으로 확보한다.

```sh
.venv/bin/sfdc-agent evaluate-prompt story.classification fixture.json --provider azure --release prompts-v1
```

## Spring 관리형 Agent

Spring에서 작업·권한·프롬프트 릴리스를 관리하는 서버는 `SFDC_AGENT_MANAGED_ONLY=1`로 실행한다. 이 모드의 HTTP 경로는 `/health`, 서비스 Bearer 인증을 사용하는 `/internal/v1/`, 실행별 인증을 사용하는 `/internal/kiro/sessions`다. 로컬 파일럿 `/runs`, SQLite 프롬프트 관리 `/internal/prompts`, 로컬 디자인 후보 경로는 404를 반환한다. `/health`는 전역 SQLite 활성 프롬프트를 조회하지 않는다.

원격 Figma REST·Plan·MCP 자격 증명을 사용하는 프로세스는 운영자가 `SFDC_MANAGED_FIGMA_SCOPE=<tenant UUID>/<workspace UUID>`로 배정한다. Spring과 Agent에 동일한 정규 UUID 쌍을 설정한다. 원격 호출은 요청 범위와 현재 배정을 확인하며 배정 누락·형식 오류는 `FIGMA_PROVIDER_SCOPE_UNAVAILABLE`, 다른 범위는 `FIGMA_PROVIDER_SCOPE_FORBIDDEN`으로 반환한다. 관리자 원본 등록은 해당 작업공간의 데이터 사용 권한을 관리한다. 소유 관계가 확인된 명시적 아카이브와 LOCAL 입력은 배정과 독립적으로 사용할 수 있다.

`SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS`는 Spring과 Agent의 공통 물리 실행 상한이며 기본값은 540초, 허용 범위는 1–900초다. Spring은 테넌트 정책과 물리 상한을 적용한 절대 마감을 각 시도에 고정한다. 작업별 취소는 자식 프로세스 종료와 슬롯 반환을 확인한 뒤 응답한다.

Kiro 세션은 현재 실행의 `X-SFDC-Managed-Prompt` 토큰과 같은 실행에 고정된다. 생성, 이벤트 조회, 구조화된 프롬프트 실행과 종료에 같은 토큰이 필요하다. 실행이 끝나면 토큰이 폐기되며 다른 실행의 토큰으로 세션을 조회하거나 종료할 수 없다. `SFDC_AGENT_EXECUTION_TOKEN` 서비스 Bearer는 Spring과 Agent 사이에서 사용하며 Node 작업 프로세스에 전달하지 않는다.

관리형 프롬프트 평가와 명령행 `evaluate-prompt`의 Kiro 공급자는 `KIRO_API_KEY` 설정으로 격리된 도구 없는 headless 실행을 시작한다. 실행 입력값은 Spring의 본인·작업공간 권한으로 조회하며 민감값 가림과 1시간 만료를 적용한다.

활성 프롬프트의 한글 참고 번역은 서비스 인증 전용 `POST /internal/v1/prompts/translations`에서 생성한다. 영문 원문과 릴리스 해시는 변경하지 않으며 `{{변수}}`, `@@section`, JSON·코드·URL·도구 식별자는 모델 호출 전에 보호하고 응답 후 무결성을 다시 검사한다. 기본 공급자는 로컬 Kiro이고 `SFDC_AGENT_TRANSLATION_PROVIDER=kiro|openai|azure`, `SFDC_AGENT_TRANSLATION_MODEL`, `SFDC_AGENT_TRANSLATION_TIMEOUT_SECONDS`로 별도 지정할 수 있다. 번역 캐시를 식별하는 `SFDC_AGENT_TRANSLATION_PROFILE_VERSION`과 `SFDC_AGENT_TRANSLATION_GLOSSARY_VERSION`은 Spring의 `SFDC_PROMPT_TRANSLATION_PROFILE_VERSION`, `SFDC_PROMPT_TRANSLATION_GLOSSARY_VERSION`과 각각 같은 값을 사용하고, 번역 규칙이나 용어집 변경 시 두 서비스를 한 배포에서 함께 올린다. Spring이 번역 상태와 본문을 보관하며 Agent는 원문이나 번역문을 로컬에 저장하지 않는다.

기본 허용 Host는 `localhost`, `127.0.0.1`이다. 사설 네트워크의 이름이 필요하면 시작 전에 `SFDC_AGENT_TRUSTED_HOSTS=agent.internal,agent.service.local`처럼 정확한 호스트 이름을 추가한다. 각 설정값은 정확한 DNS 호스트 이름 또는 IPv4 주소를 사용한다. Main 통합 환경과 로컬 `serve`는 모두 `SFDC_AGENT_MANAGED_ONLY=1`을 사용한다. 파일럿 CLI는 서버 없이 실행한다.

## 관리형 로컬/S3 저장소

로컬은 기본 `SFDC_STORAGE_PROVIDER=local`을 유지한다. 기존 `SFDC_MANAGED_SOURCE_ROOT`, `SFDC_MANAGED_RELEASE_ROOT`, `SFDC_MANAGED_RELEASE_INGRESS`, `SFDC_MANAGED_DESIGN_SOURCES`와 업무 파일을 계속 사용하며 boto3는 설치하거나 불러올 필요가 없다.

개발·운영 서버는 이미지 빌드 때 `pip install '.[aws]'`로 선택 의존성을 설치하고, 배포 플랫폼이 Parameter Store의 승인된 값을 Agent 환경에 주입한다. Spring이 읽은 Parameter Store 값은 Agent 프로세스에 자동 전달되지 않는다.

| 환경값 | 서버 설정 |
| --- | --- |
| `SFDC_STORAGE_PROVIDER` | `s3` |
| `SFDC_STORAGE_S3_BUCKET` | Spring과 동일한 비공개 업무 버킷 |
| `SFDC_STORAGE_S3_PREFIX` | Spring과 동일한 환경별 prefix |
| `AWS_REGION` | S3와 Parameter Store의 승인된 리전 |
| `SFDC_AGENT_EXECUTION_TOKEN`, source scope, Kiro/공급자 키 | 해당 Agent 역할에 필요한 값만 Parameter Store에서 주입 |

AWS 인증은 실행 역할을 사용한다. 정적 AWS 키를 이미지·저장소에 넣거나 Node 자식 프로세스로 전달하지 않는다. Agent는 요청에서 고정한 `.fig` 원본과 디자인 릴리스를 S3에서 작업별 비공개 임시 공간으로 받아 크기·SHA·경로를 검사한다. 사용자 업로드 원본은 100MiB, 릴리스 파일 합계는 128MiB이며 관리자 디자인 원본은 별도 상한 256MiB/개를 적용한다. 작업 종료·취소 시 임시 공간을 정리하고, 취소된 전송이 종료되기 전에 작업 공간을 삭제하지 않는다.

분석 출력은 `release-ingress/analysis/<jobId>/<attempt>/`, 디자인 빌드는 `release-ingress/<buildId>/`에 기록한다. 기존 출력 검증 후 파일을 불변 업로드하고, 해시가 고정된 `manifest.json` 또는 `output-manifest.json`을 마지막에 업로드한다. 같은 키가 이미 있으면 실제 내용을 확인하며, 파일 충돌이나 일부 전송 실패를 완료로 처리하지 않는다. 업무 상태·작업 큐·활성 릴리스는 PostgreSQL의 Spring 관리 모델을 유지한다.

관리자 디자인 원본 인덱스는 `<prefix>/figma-sources/design-sources/index.json`에 다음 구조로 보관한다. `objectKey`는 `figma-sources` 영역 안의 상대키이며 각 원본의 크기와 SHA를 정확히 지정한다. Agent가 이를 작업별 로컬 경로 인덱스로 변환하므로 서버 간 절대경로 공유가 필요하지 않다.

```json
{
  "guide": {"objectKey": "design-sources/guide.fig", "sha256": "<64자리 SHA-256>", "sizeBytes": 13346218},
  "library": {"objectKey": "design-sources/library.fig", "sha256": "<64자리 SHA-256>", "sizeBytes": 150462817}
}
```

위 크기는 구조 설명용이다. 실제 파일의 측정값과 SHA를 사용해야 한다. 이 구성은 AWS 리소스를 만들거나 기존 로컬 원본을 자동 업로드하지 않는다. 버킷·파라미터·권한은 배포 환경에서 준비한다. [Boto3 인증 체인](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html), [S3 조건부 업로드](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)를 따른다.
