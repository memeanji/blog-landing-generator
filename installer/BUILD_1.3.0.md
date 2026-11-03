# Agent 1.3.0 빌드 기록

작성 2026-11-03 15:21:00 · **로컬 빌드만 수행했고 설치하지 않았습니다.**

## 1. 빌드 기준선

| 항목 | 값 |
|---|---|
| 빌드 작업공간 | `C:\Users\894플러스\agent_build_130` (프로젝트와 분리) |
| 기준선 | **1.2.5 기준선 + Stage 1~3** = 현재 작업 트리 |
| ★Git HEAD 로 빌드하지 않은 이유 | 1.2.5 는 2026-09-17 18:10 에 **작업 트리**에서 빌드됐고, 그 트리에만 있는 9월 안전장치(등록 blog_id 없으면 발행 중단 등)가 HEAD 에는 없다 |
| 로컬 git HEAD | `3835238` Agent 1.3.0 — 연결 안정화 · 계정별 로그아웃 · 발행 진입 대기 개선 |
| 포함된 v2 모듈 | 35개 (`*.bak-*`·`__pycache__` 제외) |
| 제외 확인 | accounts.json · brands.json · sessions/ · queue/ · out/ · streamlit_app.py · 구글 인증 파일 · `.streamlit/` |
| payload | `agent_config.json` (SUPABASE_URL + **publishable** 키만) |
| 비밀값 검사 | build.py ③단계 통과 — service key·개인키·쿠키·로컬설정 없음 |

## 2. 산출물 해시 (SHA-256)

| 구분 | 경로 | 크기(byte) | sha256 |
|---|---|---|---|
| **1.3.0 설치파일** | `C:\Users\894플러스\agent_build_130\installer\out\BlogLandingAgentSetup.exe` | 55,448,445 | `305a0a3dbb8488eb0cd0c71e11fb894c5761463ac938c59df50a218c7e2d04e9` |
| 1.3.0 Agent exe | `C:\Users\894플러스\agent_build_130\installer\dist\BlogLandingAgent\BlogLandingAgent.exe` | 9,884,337 | `e9975d695668072499b9ecca04f6860c3eb89f4f510f53d8d662702d2d6d9b7c` |
| 1.2.5 설치파일(롤백용) | `C:\Users\894플러스\blog_landing_generator\installer\rollback_125\BlogLandingAgentSetup-1.2.5-20260917_1810.exe` | 55,397,527 | `5aaf7133b8319964a2544d669924206727d919df52b431f5fbcd7b83d7730b66` |
| 1.2.5 Agent exe(롤백용) | `C:\Users\894플러스\blog_landing_generator\installer\rollback_125\BlogLandingAgent-1.2.5-20260917_1810.exe` | 9,836,197 | `c6b9d49daf5201ebc1d5e2c36227af55c0efbaceb0ee290615bcc2455de2524e` |
| 현재 설치돼 실행 중 | `C:\Users\894플러스\AppData\Local\Programs\BlogLandingAgent\BlogLandingAgent.exe` | 9,836,197 | `c6b9d49daf5201ebc1d5e2c36227af55c0efbaceb0ee290615bcc2455de2524e` |

· 현재 실행 중인 Agent = 롤백용 1.2.5 와 **동일** (빌드 2026-09-17 18:10:10)

## 3. 포함된 v2 모듈

```
  __init__.py             accounts.py             agent.py                appdir.py
  batch_plan.py           brands.py               browser.py              catalog.py
  config.py               delete_posts.py         diag_copy.py            edit_post.py
  fastwait.py             gauth.py                gui.py                  job.py
  landing_sheet.py        logger.py               login_state.py          manual_start.py
  open_only.py            pairing.py              queue_store.py          run.py
  run_production.py       runner.py               session.py              session_store.py
  shared_cred.py          sheets.py               source.py               source_view.py
  supabase_store.py       timeutil.py             writer.py
```

## 4. 1.2.5 → 1.3.0 코드 변경(이 빌드에 들어간 것)

| 파일 | 단계 | 내용 |
|---|---|---|
| `v2/queue_store.py` | 1·2 | 멱등성(client_key)·strict 백엔드 · `held` 보류/재개(login_id 또는 account.id) |
| `v2/supabase_store.py` | 1·2 | `find_by_client_key` · `probe` · 조건부 UPDATE 보류 |
| `v2/timeutil.py` | 1 | KST 표시(신규) |
| `v2/login_state.py` | 2 | 검증 등급·세션 별칭·로그아웃 대기·공유 판정(신규) |
| `v2/session.py` | 2 | `--logout` · `--confirm` · `--confirm-shared` · 등급 기록 |
| `v2/session_store.py` | 2 | `logout()` · `browser_closed()` |
| `v2/browser.py` | 2 | `save_verdict` 4상태 · 어긋난 로그인 쿠키 제거 |
| `v2/writer.py` · `v2/edit_post.py` | 3 | 글 화면 진입 대기 2곳만 조건부 |
| `v2/fastwait.py` | 3 | 조건부 대기 헬퍼(신규, `BLOG_FAST_WAIT=0` 으로 끔) |
| `v2/pairing.py` · `installer/BlogLandingAgent.iss` | — | 1.2.5 → **1.3.0** |

## 5. 속도 개선 기본값 — **기본 꺼짐(opt-in)**

| `BLOG_FAST_WAIT` | 동작 |
|---|---|
| **설정 없음(기본)** | 기존 대기 방식 = 1.2.5 와 동일 |
| `0`·`off`·`false`·`no`·빈값·오타 | 기존 대기 방식 |
| `1`·`on`·`true`·`yes` | 속도 개선 활성 |

· 2026-11-03 변경: 사용자 PC 에서 설정을 **빠뜨리면 안전한 쪽**(기존 대기 방식)이 되도록 기본값을 뒤집었습니다.
· 설치 프로그램은 `.env` 를 만들지도 덮지도 않습니다(payload 는 `agent_config.json` 뿐이고 `onlyifdoesntexist`).
· **켤 때만** 데이터 폴더의 `.env` 에 `BLOG_FAST_WAIT=1` 을 넣습니다. 지우면 다시 꺼집니다(재설치 불필요).

## 6. 1.2.5 로 되돌리기

1. 트레이 아이콘 → 종료
2. `BlogLandingAgentSetup-1.2.5-20260917_1810.exe` 실행 → 덮어 설치(AppId 동일 → 제자리 업그레이드)
3. `%APPDATA%\BlogLandingAgent` 는 건드리지 않음 → **세션·계정·설정 그대로**
4. 화면의 [선택 계정 로그아웃] 버튼은 버전 게이트로 **자동 비활성**

