# Agent Review Engine — 한국어 안내

GitHub Enterprise Server 3.19의 PR을 에이전트 중심 개발 흐름으로 검토하고, 사내 OpenAI-compatible LLM이 승인 판단을 내리면 GitHub App이 `APPROVE` 및 필수 Check를 게시하는 독립 서비스입니다. Jev는 **선택적인 PR 라벨러**이며, 현재 로컬 API가 불완전해도 전체 흐름이 멈추지 않도록 기본 비활성화되어 있습니다.

## 사용 흐름

1. 팀원 또는 에이전트가 Draft PR 생성 → 파일 경로 기반 기본 분류(필요하면 Jev 추가 사용).
2. Ready for Review로 전환 또는 처음부터 Ready로 PR 생성 → Light CI가 성공할 때까지 대기.
3. 중앙 Policy Engine이 위험도를 결정하고 사내 LLM이 Diff와 테스트 결과를 검토.
4. 저위험 변경은 자동 `APPROVE`, 고위험·Fork는 별도 사람 승인 필요. GitHub App이 `agent-review-gate`를 게시.
5. `auto_enqueue=true`면 GitHub API로 Merge Queue 진입 시도(기본 비활성화). 머지 정책은 GitHub에서 최종 집행.
6. Merge Queue 합성 SHA에서 Heavy CI와 통합 변경 LLM 리뷰 통과 시 해당 SHA의 Check를 성공 처리하고 GitHub가 머지.

## Jev 동작

| 설정 | 분류 | LLM 리뷰 |
| --- | --- | --- |
| `JEV_ENABLED=false` | 서버의 경로 기반 분류 | 정상 수행 |
| `JEV_ENABLED=true`, 정상 응답 | `jev:risk-*` / `area:*` 라벨 | 정상 수행 |
| Enabled, API 불완전 또는 오류 | 기본 분류와 `review:jev-degraded` | 정상 수행 |

관리자가 `PATCH /admin/repos/{owner}/{repo}`에 `{"jev_enabled":true}` 또는 `false`를 보내면 Repository 단위로 켜고 끌 수 있습니다. Jev 라벨은 **승인 권한이 아니라 참고 메타데이터**입니다.

## 필수 설정

`GITHUB_API_URL`, `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_BOT_LOGIN`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LIGHT_CI_CHECKS`, `HEAVY_CI_CHECKS`, `CHECK_NAME`. Key는 Repository의 PR에 넣지 않고 중앙 서비스에만 저장합니다. GitHub App 사용 및 정확한 required check source 지정이 중요합니다. `approval_mode=approve` 활성화 시 `CI_EXPECTED_APP_ID`가 반드시 있어야 합니다.

## 처음에는 Shadow Mode

`APPROVAL_MODE=report_only`, `JEV_ENABLED=false`, `AUTO_ENQUEUE=false`로 시작하고, 임의의 PR을 자동 승인하지 않습니다. 이때 `agent-review-gate`를 GitHub Branch Protection의 required에 넣으면 PR이 막히므로 실증 검증을 마친 뒤에 Required로 전환해야 합니다.

사용자·운영자 흐름 및 예외 처리: [FLOW_AND_REQUIREMENTS.md](docs/FLOW_AND_REQUIREMENTS.md). 실서비스 설정: [DEPLOYMENT.md](docs/DEPLOYMENT.md). 현재 미구현 범위: [KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## 고도화 브랜치에서 추가한 기능

- **Repo별 LLM 프로필:** `LLM_PROFILES_JSON`에 서버에서 허용하는 endpoint/model/secret 환경변수명을 사전 등록한 후, `PATCH /admin/repos/{owner}/{repo}`에 `{"llm_profile":"fast"}`처럼 프로필 이름만 지정합니다. PR 작성자가 URL이나 Key를 변경할 수 없습니다.
- **모델 접속 제한:** `LLM_ALLOWED_HOSTS`로 접근 가능한 사내 모델 호스트 목록을 제한합니다. API Key를 JSON 설정이나 Repository에 직접 기록하면 안 됩니다.
- **감사 기록:** 작업 완료 시 SHA·판정·위험도·CI 증거 해시 및 토큰 사용량을 저장합니다. PR Diff, 원본 프롬프트, Key는 감사 테이블에 저장하지 않습니다.
- **운영 API:** `GET /admin/audit`, `GET /admin/metrics?days=30`에서 변경 이력 및 일/주/월 단위 작업·토큰 지표를 확인할 수 있습니다. 두 API 모두 관리자 토큰이 필요합니다.
- **DB 마이그레이션:** MVP에서 업데이트할 경우 서버/Worker 배포 전에 `alembic upgrade head`를 실행해야 합니다.

현재도 관리자 인증은 단일 토큰 기반이므로 조직 전체 도입 전에는 SSO/RBAC, 운영 모니터링, 모델 프로필 Secret Manager 연동, GHES 실서버 통합시험이 추가로 필요합니다.
