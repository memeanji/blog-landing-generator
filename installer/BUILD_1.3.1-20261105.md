# Agent 1.3.1 최종 빌드 기록 (2026-11-05)

작성 2026-11-05 15:54:28 · **빌드만 했고 설치하지 않았습니다.**

## 1. 왜 1.3.1 인가 — 기능 변경이 아니라 **빌드 구분**

11-03 에 1.3.0 으로 빌드한 설치본에는 Stage 4·5 · B1 · 공유 세션 보호 A안 ·
`v2/loginid.py` 가 들어 있지 않습니다. 둘 다 1.3.0 이라고 말하면 하트비트만 보고는
어느 빌드가 깔렸는지 알 수 없어 번호를 올렸습니다. **기능 로직·세션·로그아웃
코드는 한 줄도 바꾸지 않았습니다.**

| 바꾼 곳 | 전 | 후 |
|---|---|---|
| `v2/pairing.py` `AGENT_VERSION` | 1.3.0 | **1.3.1** |
| `installer/BlogLandingAgent.iss` `AppVer` | 1.3.0 | **1.3.1** |
| `v2/pairing.py` `NEEDS[logout]` | (1,3,0) | **(1,3,0) 그대로** |

`NEEDS` 는 '현재 버전' 이 아니라 **기능별 최소 버전**입니다. 올리면 1.3.0 으로
깔린 PC 의 로그아웃이 잠겨 버립니다. 실측: 1.3.1→열림 · 1.3.0→열림 ·
1.2.5→잠김 · 빈값→잠김.

제품 코드에서 번호가 쓰이는 곳은 전수 조사로 **이 2곳뿐**임을 확인했습니다.

## 2. 산출물 해시 (SHA-256)

| 구분 | 크기(byte) | sha256 |
|---|---|---|
| **1.3.1 설치파일 (배포 대상)** | 55,464,466 | `61e340b74faca33ff6d21277b2a8114363a5c177e2fcfa8eaee95032e9e173ab` |
| 1.3.1 Agent exe | 9,900,586 | `efbb076b6d4eb345c4262c2d58ca92451f6fa9ea28f6d2b3c4dd9071ceb541db` |
| 1.2.5 설치파일 (롤백용) | 55,397,527 | `5aaf7133b8319964a2544d669924206727d919df52b431f5fbcd7b83d7730b66` |
| 1.2.5 Agent exe (롤백용) | 9,836,197 | `c6b9d49daf5201ebc1d5e2c36227af55c0efbaceb0ee290615bcc2455de2524e` |
| 현재 설치돼 실행 중 (= 1.2.5) | 9,836,197 | `c6b9d49daf5201ebc1d5e2c36227af55c0efbaceb0ee290615bcc2455de2524e` |

· 설치파일 경로 `C:\Users\894플러스\agent_build_131b_20261105\installer\out\BlogLandingAgentSetup.exe`

## 3. 소스

| 항목 | 값 |
|---|---|
| 브랜치 | `feature/agent-1.3.0-stabilization` |
| git HEAD | `5237293` ver_tuple docstring 에서 버전 예시 제거 — EXE 버전 검증이 둘로 잡히던 문제 |
| 빌드 기준 | 그 브랜치의 **작업 트리**(미커밋 변경 0건) |
| 포함 v2 모듈 | 36개 — feature 와 바이트 단위 전부 일치 |
| 9월 안전장치 | 11개 파일 sha256 동일 · 내용 9항목 보존 |
| 혼입 금지 | accounts.json·brands.json·sessions·queue·out·streamlit_app.py·구글 인증·.streamlit·playwright-profile **전부 없음** |
| 비밀값 | build.py ③단계 통과 — service key·개인키·쿠키 없음. payload 는 URL + publishable 키만 |

## 4. 검증 — 전부 통과

### `verify_build_130.py` 8개 섹션

버전 문자열이 **하나뿐**(1.3.1) · `.iss` 가 `pairing.py` 와 일치 · 게이트가 1.3.1 에서
열리고 1.2.5 에서 잠김 · `--logout` 등 신규 4개 + 기존 9개 명령 유지 · 9월 blog_id
안전장치 유지 · 세션 경로 호환(`sessions/<account.id>/`) · 속도개선 기본 꺼짐 ·
1.2.5 롤백본 보존 · AppId 동일(제자리 업그레이드) · `%APPDATA%` 보존 ·
`sessions/` 미접촉 · 관리자 권한 불필요.

### EXE 내부 지문 18개

새 EXE 에 있고 **11-03 EXE 에는 없음**을 양쪽에서 대조(17/18 신규 증명,
`same_login` 1개만 옛 EXE 에도 있어 신규 판정 불가).

| 기능 | 지문 |
|---|---|
| login_id 정규화 | `NAVER_MAIL_DOMAINS` · `norm_login` · `same_login` |
| **공유 세션 보호 A안** | `peer_session` · `계정일 수 있습니다(확인되지 않음)` · `이 PC 에 저장된 세션 없음` |
| 선택 계정 로그아웃 | `--logout` · `confirm_shared` · `이 PC 에 세션 없음` |
| 보류·재개(account.id) | `list_by_status` · `PartialHoldError` · `SCAN_LIMIT` |
| Supabase 조회 안정화 | `UI_TIMEOUT` · `MIN_REQ_TIMEOUT` · `_rest_range` · `_page_all` · `_login_candidates` |
| 인증 ID 읽기(기본 꺼짐) | `read_login_id_enabled` · `BLOG_READ_LOGIN_ID` |

★**설계 ④(account.id 단독 조회)는 `streamlit_app.py` 1파일**이라 이 EXE 가 아니라
  **화면 배포**로 반영됩니다.

### 회귀 테스트

`scripts/test_stage*.py` 13개 — **826건 통과 · 실패 0건**.

## 5. 빌드 과정에서 걸린 것 (기록)

1. **EXE 에서 버전이 두 개로 잡혔다** — `['1.3.0','1.3.1']`. 코드가 두 벌 들어간
   것이 아니라 `ver_tuple` 의 **docstring** 에 옛 번호를 예시로 적어 둔 탓이었다.
   주석은 `.pyc` 에서 사라지지만 **docstring 은 남는다.** 11-03 빌드에서는 버전이
   1.3.0 이라 예시와 우연히 같아 드러나지 않았다 — 번호를 올릴 때만 보이는 문제.
   → docstring 에서 숫자를 뺐고, 검증이 '버전 문자열이 하나뿐인가' 를 따로 본다.
2. 검증 도구 4곳이 번호를 **글자로 박아** 두어 올리는 순간 거짓 실패했다.
   → `v2/pairing.py` 를 유일한 출처로 읽게 바꿨다(이제 올려도 따라온다).

## 6. 보존된 다른 빌드 (지우지 않았습니다)

| 폴더 | 내용 |
|---|---|
| `installer/rollback_125/` (운영 워크트리) | **1.2.5 롤백본** — 반드시 보존 |
| `agent_build_130/` | 11-03 구형 1.3.0 — 비교용. 배포하지 않음 |
| `agent_build_feature_20261105/` | 중간 빌드(1.3.0 번호) — 버려도 됨 |
| `agent_build_131_20261105/` | 중간 빌드(docstring 문제) — 버려도 됨 |
| `agent_build_131b_20261105/` | **최종 1.3.1 — 이것을 배포** |
