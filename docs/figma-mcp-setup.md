# Kiro–Figma MCP 연결

Kiro API 키는 모델 실행 인증이며 Figma 파일 접근 승인을 대신하지 않습니다. 각 연결은 BACK이 서명한 테넌트·작업공간·사용자 UUID에 묶입니다. 다른 앱의 로그인이나 운영체제 사용자의 전역 Kiro 설정을 복사하지 않습니다.

## 최초 설정과 승인

Agent 프로젝트의 가상환경을 활성화한 실제 터미널에서 실행합니다. UUID는 연결할 NOVA 사용자의 BACK 식별자를 사용합니다.

```sh
.venv/bin/sfdc-agent figma-mcp configure \
  --tenant-id <tenant-uuid> \
  --workspace-id <workspace-uuid> \
  --user-id <user-uuid>

.venv/bin/sfdc-agent figma-mcp authorize \
  --tenant-id <tenant-uuid> \
  --workspace-id <workspace-uuid> \
  --user-id <user-uuid>
```

`configure`는 연결을 만들며 인증 완료를 의미하지 않습니다. `authorize`는 Kiro ACP를 시작하고 공식 Figma 승인 페이지를 엽니다. 본인 계정으로 승인하면 명령이 연결 상태를 확인하고 종료합니다. 인증 URL이나 client secret을 채팅·로그에 붙여 넣지 않습니다. 승인 중에는 콜백 수신기를 최대 3분 동안 유지하고 모델 요청을 보내지 않습니다.

기본값은 지원되는 Kiro 클라이언트의 등록·OAuth 흐름입니다. 처음부터 별도 OAuth 앱이 필요하다고 가정하지 않습니다. 고정 콜백 기본값은 `http://localhost:7778/oauth/callback`입니다. 포트 사용 중이면 최초 설정에서 `--redirect-uri`를 지정합니다.

2026-11-26에 조회한 공식 [보호 리소스 메타데이터](https://mcp.figma.com/.well-known/oauth-protected-resource)는 `mcp:connect`를 광고했습니다. [인증 서버 메타데이터](https://mcp.figma.com/.well-known/oauth-authorization-server)는 등록 엔드포인트와 refresh token 지원을 광고했습니다. 이 어댑터는 `mcp:connect`를 요청하며 REST용 권한 문자열을 대신 사용하지 않습니다. 조직 정책이나 제공자 오류로 등록이 거부되면 실제 오류를 확인한 후 승인된 연결 방식을 설정합니다. MCP는 REST 제한을 우회하기 위한 인증 수단이 아닙니다.

실제 Figma 동의 화면은 `mcp:connect`에 읽기와 쓰기를 포함한 넓은 권한을 표시했습니다. 이 작업에서는 사용자가 그 범위를 확인하고 **수집기의 읽기 전용 실행 제한을 유지하는 조건으로 명시적으로 승인**했습니다. OAuth 범위 자체가 읽기 전용인 것은 아닙니다. Agent는 승인 토큰의 범위와 별도로 다섯 읽기 도구·정확한 파일·노드 입력을 검사하고 쓰기·셸·파일시스템 도구 실행을 차단합니다.

## 조직에서 자체 OAuth 앱을 요구하는 경우

[Kiro 공식 OAuth 설정](https://kiro.dev/docs/mcp/configuration/)과 Figma의 현재 개발자 설정을 확인합니다. client ID·secret·정확한 콜백을 발급하고 0600 파일에 저장합니다. 값을 소스나 셸 명령 인수에 넣지 않습니다.

```json
{
  "clientId": "<approved-client-id>",
  "clientSecret": "<secret>",
  "redirectUri": "http://localhost:7778/oauth/callback"
}
```

```sh
chmod 600 /absolute/private/figma-oauth.json
.venv/bin/sfdc-agent figma-mcp configure \
  --tenant-id <tenant-uuid> \
  --workspace-id <workspace-uuid> \
  --user-id <user-uuid> \
  --oauth-config /absolute/private/figma-oauth.json
```

고정 콜백은 Figma 앱에 등록한 값과 같아야 합니다. Figma의 승인된 MCP 클라이언트 정책을 따라야 하며, 다른 클라이언트의 ID나 로그인 토큰을 가져오지 않습니다.

## 인증 재사용·만료·철회

```sh
.venv/bin/sfdc-agent figma-mcp status \
  --tenant-id <tenant-uuid> \
  --workspace-id <workspace-uuid> \
  --user-id <user-uuid>
```

`status`는 같은 연결의 Kiro 저장소로 MCP 연결을 확인합니다. 모델 요청과 Figma 화면 조회는 수행하지 않습니다. `CONNECTED`는 현재 연결 인증 상태이며, 원본 확보 완료나 동일 리비전 증명이 아닙니다. 최초 승인 뒤 별도 프로세스로 두 번 확인하여 재사용을 검증합니다.

Kiro가 OAuth 프로토콜과 자동 갱신을 관리하며, Agent는 ACP의 인증 저장 콜백을 사용자·연결 세대별 전용 저장소에 제공합니다. 설치된 CLI 3.6.2의 ACP V3는 이 저장 기능을 제공하지 않으면 새 프로세스에서 인증을 재사용하지 못합니다. 일반 Kiro 터미널의 로그인 파일을 복사하지 않습니다. 갱신 실패·만료·제공자 철회는 재인증 필요로 반환되며 NOVA/REST로 자동 전환되지 않습니다. 다시 `authorize`를 실행합니다. 원격 접근을 해제하려면 Figma 계정의 앱 접근 권한을 철회하고 로컬 연결도 끕니다.

```sh
.venv/bin/sfdc-agent figma-mcp revoke \
  --tenant-id <tenant-uuid> \
  --workspace-id <workspace-uuid> \
  --user-id <user-uuid>
```

`revoke`는 NOVA에서 즉시 연결을 차단하고 Agent 전용 ACP 인증 값을 제거합니다. 진행 중 수집은 인증 범위 검사·폴링에서 종료되고 완료 체크포인트를 만들지 않습니다. 제공자 토큰의 원격 철회까지 완료했다고 보고하지 않습니다. 다시 설정하면 새 인증 세대를 사용하므로 이전 연결의 Kiro HOME를 재사용하지 않습니다.

## 저장·수집 보안과 완료 조건

기본 경로는 `.figma-mcp/`이며 Git에서 제외됩니다. 서비스 계정의 영구 저장소로 바꾸려면 절대 경로 `SFDC_AGENT_FIGMA_MCP_ROOT`를 지정합니다. 폴더는 0700, 파일은 0600입니다. 서버 실행 계정과 승인 명령 실행 계정이 같고 같은 저장 경로를 사용해야 합니다.

분석·생성용 기존 도구 없는 headless 세션과 별도로 ACP V3 수집기를 사용합니다. 승인하는 도구는 Figma 읽기 도구뿐이며, 호스트가 해석한 서버·도구 식별자와 실제 입력의 파일·노드 범위를 매 호출 확인합니다. 파일시스템·터미널·다른 MCP 서버는 허용하지 않습니다. 원본 데이터가 지시문을 포함해도 실행 권한을 추가하지 않습니다.

수집 결과는 원문 도구 입력·출력과 호출 기록으로 저장합니다. 모델이 요약한 답변은 원본으로 채택하지 않습니다. 메타데이터·디자인 컨텍스트·캡처 원문이 없거나 전송이 잘리면 완료 처리하지 않습니다. 원본 리비전·전체 속성·동일 리비전 1× PNG가 증명되지 않으면 `PARTIAL`/`REFERENCE_ONLY`로 남습니다.

Figma MCP도 계정의 시트·플랜별 호출 한도가 있습니다. 실제 검증 중 Enterprise의 View 시트 한도 응답이 확인됐습니다. `FIGMA_MCP_RATE_LIMITED`는 수집을 즉시 중단하며 자동 반복 호출, REST 전환, 다른 클라이언트 인증으로의 전환을 하지 않습니다. 한도가 복구되거나 적절한 시트 접근이 제공되면 사용자가 명시적으로 재시도합니다. 제공자가 초기화 시각을 주지 않으면 임의의 재시도 날짜를 표시하지 않습니다.

완료 체크포인트는 사용자·작업공간·작업 ID·파일·노드에 묶습니다. 같은 작업 재시도는 완료된 수집 결과를 재사용하며, 다른 작업의 변동 가능한 최신 원본을 암묵적으로 재사용하지 않습니다. 취소·실패한 부분 수집은 재개 가능한 완료 인덱스를 만들지 않습니다. 테스트 정리 시 테스트 전용 체크포인트만 제거하며 실제 사용자 OAuth 연결은 보존합니다.
