# Agent 1.3.0 재빌드 기록 — feature 기준 (2026-11-05)

작성 2026-11-05 15:36:46 · **빌드만 했고 설치하지 않았습니다.**

11-03 빌드본(`agent_build_130`)은 **쓰지 않습니다** — Stage 4·5 · B1 ·
공유 세션 보호 A안 · 설계 ④ 가 들어 있지 않고 `v2/loginid.py` 가 아예 없습니다.
그 산출물은 비교용으로 **그대로 보존**했습니다.

## 1. 빌드 기준선

| 항목 | 값 |
|---|---|
| 작업공간 | `C:\Users\894플러스\agent_build_feature_20261105` (11-03 과 별도) |
| 소스 | `feature/agent-1.3.0-stabilization` **작업 트리** |
| git HEAD | `6224cf0` Streamlit 을 정확 버전으로 고정 — 1.62.0 (화면 로딩 장애 재발 방지) |
| 커밋 안 된 코드 변경 | 없음 (운영·feature 양쪽 모두 확인) |
| 포함 v2 모듈 | 36개 (11-03 은 35개 — `loginid.py` 1개 증가) |
| 9월 안전장치 | 11개 파일 sha256 전부 동일 · 내용 9항목 전부 보존 |
| 혼입 금지 확인 | accounts.json·brands.json·sessions·queue·out·streamlit_app.py·구글 인증·.streamlit·playwright-profile **전부 없음** |
| payload | `agent_config.json` — SUPABASE_URL + **publishable** 키만 |
| 비밀값 검사 | build.py ③단계 통과 — service key·개인키·쿠키·로컬설정 없음 |

## 2. 산출물 해시 (SHA-256)

| 구분 | 크기(byte) | sha256 |
|---|---|---|
| 1.3.0 설치파일 (feature) | 55,467,276 | `b7ee0e7a2036eab30cae1de2244db55c449011222b98995e4f896a9cee3876ec` |
| 1.3.0 Agent exe (feature) | 9,900,335 | `938f51cbd7ce47d9153b8787feada0ff305c0eee952f6a61fd5cf267175304ef` |
| 1.3.0 설치파일 (11-03, 사용 안 함) | 55,448,445 | `305a0a3dbb8488eb0cd0c71e11fb894c5761463ac938c59df50a218c7e2d04e9` |
| 1.2.5 설치파일 (롤백용) | 55,397,527 | `5aaf7133b8319964a2544d669924206727d919df52b431f5fbcd7b83d7730b66` |
| 1.2.5 Agent exe (롤백용) | 9,836,197 | `c6b9d49daf5201ebc1d5e2c36227af55c0efbaceb0ee290615bcc2455de2524e` |
| 현재 설치돼 실행 중 | 9,836,197 | `c6b9d49daf5201ebc1d5e2c36227af55c0efbaceb0ee290615bcc2455de2524e` |

## 3. 검증 결과

### `verify_build_130.py` — 8개 섹션 **전부 통과**

AGENT_VERSION 1.3.0 · `--logout` 지원(1.2.5 에는 없음) · 9월 blog_id 안전장치 유지 ·
세션 경로 호환(`sessions/<account.id>/`) · 속도개선 기본 꺼짐 · 1.2.5 롤백본 보존 ·
AppId 동일(제자리 업그레이드) · `%APPDATA%` 보존 · 관리자 권한 불필요.

### EXE 내부 지문 대조 — 18개 **전부 확인**

새 EXE 에 있고 **11-03 EXE 에는 없음**을 양쪽에서 대조했습니다(17/18 신규 증명,
`same_login` 1개만 옛 EXE 에도 있어 신규 판정 불가).

| 기능 | 지문 |
|---|---|
| login_id 정규화 | `NAVER_MAIL_DOMAINS` · `norm_login` · `same_login` |
| 공유 세션 보호 A안 | `peer_session` · `계정일 수 있습니다(확인되지 않음)` · `이 PC 에 저장된 세션 없음` |
| 선택 계정 로그아웃 | `--logout` · `confirm_shared` · `이 PC 에 세션 없음` |
| 보류·재개(account.id) | `list_by_status` · `PartialHoldError` · `SCAN_LIMIT` |
| Supabase 조회 안정화 | `UI_TIMEOUT` · `MIN_REQ_TIMEOUT` · `_rest_range` · `_page_all` · `_login_candidates` |
| 인증 ID 읽기(기본 꺼짐) | `read_login_id_enabled` · `BLOG_READ_LOGIN_ID` |

★**설계 ④(account.id 단독 조회)는 `streamlit_app.py` 1파일**이라 이 EXE 가 아니라
  **화면 배포**로 반영됩니다. Agent 쪽은 큐의 보류·재개가 account.id 를 받습니다.

## 4. 주의 — 버전 문자열이 11-03 빌드와 같습니다

둘 다 `AGENT_VERSION = 1.3.0` 이라 **하트비트만 보면 어느 빌드가 깔렸는지 구분할 수
없습니다.** 화면의 버전 게이트(`>= 1.3.0`)는 양쪽 다 열립니다. 구분이 필요하면
`v2/pairing.py` 의 `AGENT_VERSION` 과 `installer/BlogLandingAgent.iss` 의 `AppVer` 를
`1.3.1` 로 올려 다시 빌드해야 합니다(1줄씩 · 사용자 판단 필요).

## 5. 빌드를 다시 하려면

`.env` 가 feature 워크트리에 없어서 빌드 직전에 **2줄만**(SUPABASE_URL +
SUPABASE_PUBLISHABLE_KEY) 만들어 쓰고, 끝난 뒤 지웠습니다. 운영 `.env` 의 나머지
(서비스키·구글 인증·시트 ID)는 옮기지 않았습니다. 다시 빌드할 때도 같게 하면 됩니다.

```
set BLOG_BUILD_WS=<새 폴더>
python scripts\make_build_workspace.py
python installer\build.py        (작업공간 안에서)
python scripts\verify_build_130.py
```
