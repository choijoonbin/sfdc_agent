# 변환 근거와 실행 계약

Spring은 Main의 작업·권한·불변 릴리스·활성 프롬프트·결과 저장을 소유합니다. Agent는 고정된 실행 요청을 검증해 공통 Node 엔진과 모델 세션을 실행하고 출력 manifest를 반환합니다. 독립 CLI의 SQLite·프롬프트 저장소는 Main의 권한이나 활성 상태를 바꾸지 않습니다. 실행 방법은 [로컬 개발](local-development.md), 배포·서버 인수는 [배포 안내](../deployment/README.md)를 따릅니다.

## 원본과 생성 근거

공통 Node 엔진의 `source-evidence.mjs`는 REST·로컬 archive의 작성된 사실·출처·리비전·미지원 범위를 정규화합니다. `kiro-screen-context.mjs`는 측정 layout, rich-text override/run, 변수 binding과 vector 근거를 제한된 모델 입력에 넣습니다. 불완전한 run table은 unresolved이며 캡처를 보고 추측해 채우지 않습니다.

`kiro-nova-contract.mjs`는 고정된 NOVA 공개 API로 derived mapping과 guidance를 만듭니다. `kiro_screen_projection.mjs`는 저장 checkpoint에 같은 projection을 적용하고 `nova_prompt_evidence.py`가 최종 입력을 검사합니다. authored Figma text는 신뢰하지 않는 데이터이며 실행 지시나 API 권한으로 승격하지 않습니다. 렌더러의 과거 metadata를 별도의 모델 지침으로 사용하지 않습니다.

공통 LWC의 관리 원천은 승인된 NOVA Git 루트의 `packages/nova-ui/main/default/lwc`입니다. 선택한 불변 디자인 릴리스가 해당 Git 소스·공개 API·토큰을 고정하며, UI의 Node runtime은 변환·검증 엔진입니다. NextBSS v1.9는 Figma 디자인 근거의 이름입니다. 임시 모델 작업공간·fixture·runtime artifact는 편집할 공통 LWC 원천이 아닙니다.

Screen은 분석 → 검토 확인 → NOVA 규칙 생성 또는 Kiro 생성 순서입니다. 수집 경로는 요청의 `sourceAcquisition`으로 고정합니다.

| 요청 | 원본 수집 | 실패 시 경계 |
| --- | --- | --- |
| NOVA 규칙 분석 `REST_FIRST` | REST 우선, 허용된 오류에서 버전 없는 같은 파일의 등록 `.fig` 사본 | 접근 거부·노드 없음·리비전 불일치를 사본으로 우회하지 않음 |
| Kiro 분석 `REST_ONLY` | 선택한 Figma API 원본 | MCP·등록 사본·NOVA로 자동 전환하지 않음 |
| Kiro 분석 `MCP_ONLY` | 사용자·작업공간에 결합된 읽기 전용 MCP | 인증·한도·잘림은 명시적 실패, REST로 전환하지 않음 |
| 명시적 로컬 `LOCAL_FILE` | 선택한 `.fig`와 hash·페이지·노드 | 다른 파일로 교체하지 않음 |
| 저장 Screen 계약의 `GENERATE` | 저장한 장면·분석·PNG와 원본·NOVA·프롬프트 핀 | REST/MCP 재수집이나 archive 재열기로 원본을 교체하지 않음 |

Story는 기존 `.fig`·REST 계약을 사용합니다. MCP 원문·PNG·호출 기록은 사용자·작업 ID에 묶어 원자적으로 보존하며 같은 작업 재시도는 완성된 수집만 재사용합니다. 부분 metadata나 Code Connect mapping 지침은 전체 authored scene이 아닙니다. 완전한 원문·속성·리비전 결합이 부족하면 `PARTIAL`/`REFERENCE_ONLY`로 남기며 미확인 게시 키·속성은 검토 대상으로 유지합니다.

## 서비스 인증과 실행 범위

Main Agent는 `SFDC_AGENT_MANAGED_ONLY=1`로 실행합니다. HTTP 경로는 `/health`, 서비스 Bearer 인증을 사용하는 `/internal/v1/`, 실행별 인증을 사용하는 `/internal/kiro/sessions`입니다. 파일럿 `/runs`, SQLite 프롬프트 `/internal/prompts`, 로컬 디자인 후보 API는 404이며 `/health`는 전역 SQLite 활성 프롬프트를 조회하지 않습니다.

- `SFDC_AGENT_EXECUTION_TOKEN`은 Spring–Agent 서비스 Bearer이며 Node 자식에게 전달하지 않습니다.
- `X-SFDC-Managed-Prompt`는 현재 실행에 고정됩니다. Kiro 세션 생성·이벤트 조회·프롬프트 실행·종료에 같은 토큰을 요구하며 실행 종료 시 폐기합니다. 다른 실행의 토큰으로 세션을 조회하거나 종료할 수 없습니다.
- 기본 Host는 `localhost`, `127.0.0.1`입니다. 사설 DNS가 필요하면 `SFDC_AGENT_TRUSTED_HOSTS`에 정확한 DNS 이름 또는 IPv4를 지정합니다.
- 공유 REST·Plan 자격은 `SFDC_MANAGED_FIGMA_SCOPE=<tenant UUID>/<workspace UUID>`로 배정합니다. Spring과 Agent의 정규 UUID 쌍이 같아야 합니다. 누락·형식 오류는 `FIGMA_PROVIDER_SCOPE_UNAVAILABLE`, 다른 범위는 `FIGMA_PROVIDER_SCOPE_FORBIDDEN`입니다. 등록 사본이 있어도 NOVA REST 우선 요청의 원격 권한 검사를 우회하지 않습니다. 명시적 LOCAL은 소유 범위를 따로 확인합니다.
- MCP는 공유 REST 토큰을 사용하지 않고 BACK이 서명한 사용자·테넌트·작업공간의 전용 OAuth 저장소를 사용합니다. 최초 승인·철회는 [MCP 연결](figma-mcp-setup.md)을 따릅니다.

서버는 한 REST 범위의 전용 deployment로 라우팅하며 한 Uvicorn process/worker만 사용합니다. Node가 같은 process의 메모리 실행 context를 loopback으로 호출하기 때문입니다. 서버 배포의 MCP는 `NOT_READY`이며 로컬 credential HOME 복사나 JSON flag로 활성화할 수 없습니다.

`SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS`는 Spring과 Agent의 공통 물리 상한입니다. 기본 540초, 범위 1–900초이며 Spring이 테넌트 정책과 적용한 절대 마감을 각 시도에 고정합니다. 작업 취소는 소유한 자식 프로세스 종료·전송 중지·슬롯 반환을 확인한 뒤 응답합니다. 기한·출력·경로·hash·범위 검증 실패를 성공으로 바꾸지 않습니다. 공개 실패 진단은 `execution_diagnostics_policy.json`의 제한된 계약을 따릅니다.

## 모델 공급자와 이미지

`provider_capabilities.py`는 구현된 입력·출력 기능을 선언하고 `providerReceipt`를 만듭니다. 영수증에는 설정 모델·공급자가 실제 보고한 모델·기능 flag·선택 정책을 구분합니다. Kiro는 실행 CLI의 byte hash와 basename을 기록하며 로컬 절대경로·자격 증명은 넣지 않습니다. CLI나 모델 버전이 보고되지 않으면 unreported로 남기고 대체 모델을 자동 선택하지 않습니다. Main Screen·Story는 Kiro session 경로를 사용하고 파일럿·프롬프트 평가·번역의 텍스트 공급자는 `ModelProvider.complete()`에 연결합니다.

구조화한 텍스트가 기본입니다. Screen 생성·보정만 `SFDC_AGENT_SCREEN_IMAGE_INPUT=bound-png-v1`로 선택할 수 있습니다. `image_input.py`가 저장 분석·원본·노드·리비전 또는 MCP snapshot·크기·SHA를 결합하고, `kiro_image_transport.py`와 `kiro_image_guard.py`가 private PNG 한 파일의 실제 읽기와 전후 권한·경로·hash를 검사합니다. 검증·읽기 실패를 텍스트 성공으로 바꾸지 않으며 실제 읽기가 확인된 결과만 `providerReceipt.imageInput.status=read-verified`입니다.

PNG가 없는 분석은 텍스트 경로와 기준 이미지 미확보 표시를 유지합니다. Story와 OpenAI/Azure 이미지 전송은 이 옵션으로 활성화되지 않습니다. 프로토콜 기능 광고는 전달 증거가 아니며 모델의 내부 resize·해상도가 보고되지 않으면 미확인입니다. 이미지 사용은 운영 기본값이 아니며 별도 품질 비교 후 결정합니다.

영수증과 SHA는 재현 근거이며 결정적 LLM 출력·속도 개선·시각 정확도 보증은 아닙니다. 저장 Screen generator fingerprint는 UI의 production engine/shared/server 모듈과 lockfile을 포함하지만 Agent의 `kiro_screen_projection.mjs`, `prompt_projection.py`, `screen_tasks.py` 구현 byte까지 고정하지는 않습니다. 해당 assembly를 바꿀 때는 checkpoint 호환성을 검토하고 adapter revision 또는 명시적 migration을 사용해야 동일 생성 규칙을 주장할 수 있습니다.

## 프롬프트 릴리스와 번역

Main의 프롬프트 초안·불변 릴리스·활성 버전·권한은 Spring API와 PostgreSQL이 기준입니다. Agent는 실행마다 받은 릴리스·hash를 사용합니다. Kiro steering 파일에 두 번째 변환 규칙을 만들지 않습니다. 템플릿 구조 검증은 실제 변환 품질 검증과 구분합니다.

`prompt_templates_v1/`의 NOVA Prompt v1.0.0과 `prompt_templates_v2/`의 v1.0.1은 불변 패키지입니다. 소스 배포·디렉터리명 변경으로 Main 활성 릴리스가 바뀌지 않습니다. BACK의 `scripts/publish_agent_prompt_bundle.py`가 전체 13개 template·변수 계약·hash를 검사해 새 릴리스로 게시·활성화합니다. 기존 분석·진행 작업은 이전 핀을 유지합니다.

관리형 평가와 CLI `evaluate-prompt`의 Kiro 공급자는 `KIRO_API_KEY`로 격리된 도구 없는 headless 실행을 시작합니다. 평가 입력은 Spring의 본인·작업공간 권한으로 조회하고 민감값 가림·1시간 만료를 적용합니다.

참고 한글 번역은 서비스 인증 전용 `POST /internal/v1/prompts/translations`로 생성합니다. 영문 원문과 릴리스 hash를 바꾸지 않으며 `{{변수}}`, `@@section`, JSON·코드·URL·도구 식별자를 호출 전 보호하고 응답 후 다시 검사합니다. 기본 Kiro이며 `SFDC_AGENT_TRANSLATION_PROVIDER=kiro|openai|azure`, `SFDC_AGENT_TRANSLATION_MODEL`, `SFDC_AGENT_TRANSLATION_TIMEOUT_SECONDS`로 선택합니다. Agent의 `SFDC_AGENT_TRANSLATION_PROFILE_VERSION`·`SFDC_AGENT_TRANSLATION_GLOSSARY_VERSION`은 Spring의 `SFDC_PROMPT_TRANSLATION_PROFILE_VERSION`·`SFDC_PROMPT_TRANSLATION_GLOSSARY_VERSION`과 각각 같아야 합니다. 규칙·용어집 변경은 두 서비스를 함께 배포합니다. Spring이 번역 상태·본문을 보관하고 Agent는 로컬에 원문·번역문을 저장하지 않습니다.

## 업무 파일과 불변 출력

로컬은 `SFDC_STORAGE_PROVIDER=local`과 기존 `SFDC_MANAGED_SOURCE_ROOT`, `SFDC_MANAGED_RELEASE_ROOT`, `SFDC_MANAGED_RELEASE_INGRESS`, `SFDC_MANAGED_DESIGN_SOURCES`를 사용하며 boto3를 필요로 하지 않습니다. S3 서버는 `.[aws]` 의존성과 승인된 환경값을 배포 플랫폼이 주입합니다. Spring이 읽은 Parameter Store 값은 Agent로 자동 전달되지 않습니다.

| 환경값 | 계약 |
| --- | --- |
| `SFDC_STORAGE_PROVIDER` | 서버는 `s3` |
| `SFDC_STORAGE_S3_BUCKET`, `SFDC_STORAGE_S3_PREFIX`, `AWS_REGION` | Spring과 같은 비공개 업무 버킷·환경 prefix·리전 |
| `SFDC_AGENT_EXECUTION_TOKEN`, 원본 scope, Kiro/공급자 키 | 해당 worker에 필요한 값만 private 참조로 주입 |

AWS 인증은 workload 역할이며 정적 AWS 키·개발자 profile을 이미지·저장소·Node 환경으로 전달하지 않습니다. 서버의 secret file 소유자·권한·중복 입력 검사는 배포 preflight를 따릅니다. 원본·릴리스는 작업별 private 임시 공간에 받고 크기·SHA·경로를 확인합니다. 사용자 `.fig`는 100MiB, 릴리스 파일 합은 128MiB, 관리자 Guide/Library는 각각 256MiB입니다. 취소된 전송이 끝나기 전에 작업공간을 지우지 않습니다.

분석 출력은 `release-ingress/analysis/<jobId>/<attempt>/`, 디자인 빌드는 `release-ingress/<buildId>/`입니다. 검증한 파일을 불변 업로드한 뒤 hash가 고정된 `manifest.json` 또는 `output-manifest.json`을 마지막에 게시합니다. 기존 key는 실제 byte를 대조하며 충돌·부분 업로드를 완료로 처리하지 않습니다. 공개 분석 DTO는 8MiB, 원본 asset은 최대 512개·개당 2MiB+1KiB·합계 128MiB이며 raw checkpoint와 공개 DTO hash는 구분합니다. 원본 asset의 실제 MIME·소유 범위·크기·SHA를 확인하고 Spring이 내구성 있는 분석·결과 저장을 소유합니다.

관리자 디자인 원본 index는 `<prefix>/figma-sources/design-sources/index.json`에 둡니다. `objectKey`는 `figma-sources` 안의 상대 key입니다. 실제 측정 크기·SHA를 사용하며 Agent가 작업별 로컬 경로 index로 바꿉니다.

```json
{
  "guide": {"objectKey": "design-sources/guide.fig", "sha256": "<64자리 SHA-256>", "sizeBytes": 12345},
  "library": {"objectKey": "design-sources/library.fig", "sha256": "<64자리 SHA-256>", "sizeBytes": 67890}
}
```

복구는 Spring DB의 scope·작업·불변 릴리스·원본 및 참조한 분석·결과 object를 같은 기준으로 맞춥니다. Agent scratch는 업무 원장이 아니며 Git 문서 정리로 원본·인증·불변 output을 삭제하지 않습니다. 전체 환경 백업·인수는 [BACK DB 보안 운영](../../SFDC_BACK/docs/database-security-runbook.md)과 [서버 배포](../deployment/README.md)를 따릅니다.

## 품질 판정 경계

공식 비교에는 동일 Figma 리비전의 원본과 1× PNG 출처 결합이 필요합니다. archive 재구성·MCP 원문·REST 원본의 동등성이 증명되지 않으면 별개 근거로 남깁니다. screenshot 차이가 낮아도 native handler 실패·필수 control 미지원·원본 누락·미확인 업무 API를 숨기지 않습니다. repair도 같은 검사를 보존하고 regression을 거부합니다.

Guide나 예제 코드는 확정 업무 계약을 대신하지 않습니다. 실제 endpoint/Apex·요청/응답·paging·오류·권한·reset/refresh·navigation 의미가 확인되지 않으면 사용자 검토로 남기며 빈 데이터·로컬 validation을 실제 API 성공으로 보지 않습니다. 원본·공개 API를 넘어 width/font·변형·컴포넌트 속성·사업 규칙을 추측해 품질 gate를 통과시키지 않습니다.

렌더링·기하·동작 판정은 [UI 변환 품질 구조](../../SFDC_Ui/docs/conversion-quality-architecture.md), 저장 분석·실행 시간·upgrade는 [BACK 변환 품질 계약](../../SFDC_BACK/docs/conversion-quality-contract.md)을 함께 따릅니다. 로컬 단위 검사·인증 성공·개별 측정은 실제 provider/Org 인수나 자동 적용 권한을 부여하지 않습니다.
