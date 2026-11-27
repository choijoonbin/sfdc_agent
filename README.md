# SFDC Agent

`SFDC_Agent`는 Spring이 제출한 Screen·Story 변환을 Node 엔진과 Kiro에서 실행하는 HTTP Worker입니다. Spring이 작업 큐·권한·활성 프롬프트·결과 저장을 관리하고, Agent는 실행별 원본·릴리스·프롬프트 핀과 출력 경계를 검사하며 모델 세션·자식 프로세스·임시 파일을 관리합니다. 독립 CLI 비교 파일럿은 별도의 SQLite와 `.pilot/` 저장소를 사용합니다.

## 로컬 개발

Python 3.11 이상을 사용합니다. 저장소 루트에서 설치합니다.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -e .
```

Main 통합 환경은 형제 `SFDC_BACK`에서 시작합니다. UI는 `https://localhost:4186`, Spring은 `127.0.0.1:8080`, Agent는 `127.0.0.1:8766`입니다.

```sh
cd ../SFDC_BACK
python3 scripts/live-development.py start
python3 scripts/live-development.py status
python3 scripts/live-development.py doctor
```

Agent만 실행할 때는 이 저장소에서 다음 명령을 사용합니다. BACK의 기존 설정 로더와 자격 증명 참조를 사용하며 Docker·Spring·UI를 시작하거나 DB를 초기화하지 않습니다.

```sh
.venv/bin/sfdc-agent serve --check
.venv/bin/sfdc-agent serve
curl -fsS http://127.0.0.1:8766/health
```

통합 실행기가 소유한 Agent는 BACK의 `live-development.py stop-agent`로 먼저 종료합니다. 직접 시작한 Agent는 해당 터미널의 `Ctrl+C`로 종료합니다. 포트가 사용 중이면 다른 포트로 우회하지 않습니다. Python 소스 변경은 자동 재로딩합니다. 자세한 시작·중지·로그·복구 절차는 [BACK 로컬 개발 안내](../SFDC_BACK/docs/local-live-development.md)를 따릅니다.

## 문서

| 목적 | 문서 |
| --- | --- |
| 실행·인증·원본 수집·프롬프트·저장소·공급자 경계 | [변환 근거와 실행 계약](docs/conversion-evidence-contract.md) |
| 독립 파일럿·입력·Azure·프롬프트 평가·검증 | [로컬 개발과 CLI](docs/local-development.md) |
| 사용자별 Figma MCP 최초 승인·갱신·철회 | [MCP 연결](docs/figma-mcp-setup.md) |
| 서버 이미지·고정 런타임·도구·비밀 주입·배포 인수 | [배포 안내](deployment/README.md) |
| CI의 불변 UI 런타임 입력 | [CI 런타임 계약](.github/RUNTIME_CONTRACTS.md) |

공유 Figma REST·Plan은 기존 운영 연결을 조직 연결로 재사용할 수 있습니다. 조직 관리자가 draft → 명시적 workspace bindings → 활성화 순서로 채택하며 기존 작업공간 권한을 보존합니다. 물리 토큰·키 재발급이나 환경 설정 변경은 필요하지 않습니다. 작업공간 관리권한과 연결 사용권한, 개인 OAuth/MCP는 각각 분리합니다. 서명과 실행 기한·철회 경계는 [서비스 인증과 실행 범위](docs/conversion-evidence-contract.md#서비스-인증과-실행-범위)를 따릅니다.

서버 이미지 진입점은 `python -m sfdc_agent.server_runtime --contract /etc/sfdc/agent-server-contract.json`입니다. 서버 MCP는 별도 OAuth 구현과 검증 전까지 `NOT_READY`로 차단됩니다. 로컬 승인이 서버 배포 준비를 의미하지 않습니다.

공통 NOVA Git 소스를 디자인 릴리스 후보로 빌드하려면 운영자가 Agent 환경의 `SFDC_NOVA_PROJECT_ROOT`에 Git 저장소의 실제 절대 경로를 지정합니다. Spring의 후보 입력은 `sources.nova = {"commit":"<40 또는 64자리 소문자 Git SHA>","treeSha256":"<64자리 SHA-256>"}`로 고정하며 경로·URL·브랜치·태그는 받지 않습니다. 이 후보는 Figma `guide/library` 변경과 분리하며 Figma 인증이나 원본 `.fig` 다운로드를 사용하지 않습니다. 기준 불변 릴리스는 계속 검증해 읽습니다. 로컬·객체 저장소 환경 모두 운영자 Git 경로가 없거나 유효하지 않으면 빌드를 차단합니다. 검증·별도 승인·발행·활성화 권한과 절차는 Spring이 관리합니다.

## 코드 진입점

| 책임 | 파일 |
| --- | --- |
| HTTP·모드·서비스 인증 | `api.py`, `service_mode.py`, `managed_http.py` |
| Main 실행·진행·취소·프로세스 수명 | `managed_execution.py`, `managed_execution_progress.py`, `managed_runtime.py` |
| Kiro 세션·격리 실행·이미지 | `kiro_gateway.py`, `kiro_headless.py`, `kiro_processes.py`, `image_input.py`, `kiro_image_transport.py`, `kiro_image_guard.py` |
| 원본·범위·저장 분석·파일 | `managed_figma_scope.py`, `managed_figma_connections.py`, `managed_figma_mcp.py`, `managed_source_reference.py`, `screen_analysis_contract.py`, `managed_analysis_output.py`, `managed_object_storage.py` |
| 모델용 원본 표현·독립 복원 검증 | `kiro_screen_projection.py`, `kiro_screen_projection.mjs`, `screen_prompt_context.py` |
| 프롬프트·공급자·독립 CLI | `managed_prompts.py`, `screen_tasks.py`, `story_tasks.py`, `providers.py`, `azure_provider.py`, `prompt_registry.py`, `cli.py`, `pilot.py` |

Python 구현은 `src/sfdc_agent/`에 있습니다. Main은 Spring → `/internal/v1/executions` → Node runner 순서이며, Node는 필요한 단계에서 Agent의 실행별 Kiro 세션을 호출합니다. 로컬에서는 형제 `SFDC_Ui/nextbss` 엔진을 사용하고 서버는 그 엔진을 고정한 불변 runtime artifact를 사용합니다.

불변 프롬프트·fixture·실행 정책 JSON·lockfile은 실행 및 회귀 계약입니다. `.pilot/`, `.prompt-management/`, `.figma-mcp/`와 비밀 설정은 Git 밖의 업무·인증 자료이므로 문서 정리 대상으로 삭제하지 않습니다. 프롬프트 authoring 계약은 [템플릿 안내](src/sfdc_agent/prompt_templates_v1/README.md), 고정 테스트 바이트는 [fixture 안내](tests/fixtures/immutable-nova-v1/README.md)를 따릅니다.

Node Worker의 비정상 종료 로그에는 고정 허용 목록의 오류 코드(그 외 `UNKNOWN`)·종료 코드·stdout 바이트 수만 기록합니다. stderr 원문·모델 응답·자격 증명은 기록하지 않으며 성공 응답이나 실패 응답 계약은 바꾸지 않습니다.
