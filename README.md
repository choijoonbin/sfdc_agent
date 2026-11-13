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

## 작업 구조

| 위치 | 책임 |
| --- | --- |
| `src/sfdc_agent/contracts.py` | 요청과 모델 응답 구조 |
| `src/sfdc_agent/providers.py` | Kiro ACP 및 선택형 OpenAI Responses API 연결 |
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
.venv/bin/sfdc-agent serve
```

`SFDC_Ui`는 기존 HTTPS 4175를 사용한다. Screen의 **Kiro 변환**, Story의 **Kiro 분석**과 **Kiro로 LWC 생성**은 기존 화면에서 실행한다. 4175의 Node 서버가 `127.0.0.1:8765`의 Python 서버로 모델 작업을 보내므로 브라우저에 새 주소를 입력할 필요가 없다. 이 경로에는 파일럿 대기열을 처리하는 `worker`가 필요 없다. 모델 호출을 위해 Kiro CLI 로그인 상태가 필요하며, Figma 링크의 MCP 연결은 기존 화면에서 승인한다.

`kiro_gateway.py`가 세션·작업 요청을 관리하고, `screen_tasks.py`와 `story_tasks.py`가 근거에 맞는 분석·생성 프롬프트를 만든다. `kiro_acp.mjs`는 로컬 Kiro CLI와 통신한다. `SFDC_Ui`의 NextBSS 원본 처리, NOVA 생성, LWC 컴파일, 검토·저장, Salesforce 세션 경로를 계속 사용한다. 위 CLI 파일럿의 OpenAI 어댑터는 별도로 실행할 수 있다. 현재 4175의 대화형 작업은 Kiro를 사용한다.

상태는 `curl http://127.0.0.1:8765/health`로 확인한다. 기존 4175 프로세스가 변경 전 버전을 실행 중이면 같은 포트에서 Vite를 다시 실행한다. Vite가 다시 시작되면 메모리의 Salesforce 세션을 다시 연결해야 할 수 있다.
