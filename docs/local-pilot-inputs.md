# 비교 파일럿의 명시적 입력 경로

Main의 Screen·Story 변환은 Spring이 작업별로 지정하는 원본·릴리스를 사용합니다. UI의 과거 숨김 저장소를 삭제해도 이 경로는 유지됩니다. `SFDC_Ui/nextbss`는 현재 변환 엔진 소스이므로 보존합니다.

독립 `pilot` / `resume` CLI는 아래 입력을 명시해야 합니다. UI 저장소에서 Figma 사본이나 활성 릴리스를 추측하지 않으며 필요한 경로가 없으면 실행을 중단합니다.

| 환경 변수 | 입력 |
| --- | --- |
| `NOVA_PILOT_FIGMA_SOURCE` | 비교 대상 `.fig` 원본 |
| `SFDC_MANAGED_RELEASE_ROOT` | 검증할 불변 릴리스 아카이브와 해당 파일럿의 활성 포인터 |
| `NOVA_PILOT_GUIDE_FIG` | 승인된 Guide 원본 |
| `NOVA_PILOT_LIBRARY_FIG` | 승인된 Components Library 원본 |
| `SFDC_UI_ROOT` | 선택적인 UI 엔진 Git 경로 |

입력 원본은 릴리스에 기록된 SHA-256과 대조합니다. 명시한 경로는 자식 Node 요청에 전달하며 키·서버 환경 전체를 전달하지 않습니다. 파일럿의 활성 파일 포인터는 Main DB의 활성 릴리스를 변경하지 않습니다.

과거 README의 UI 내부 `AI 테스트용.fig` 기본 입력 안내는 폐기되었습니다. 필요한 경우 서버가 이미 보관한 원본을 읽기 전용 입력으로 지정할 수 있으며, 입력을 UI 폴더에 다시 복사하지 않습니다.
