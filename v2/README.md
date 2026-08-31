# v2 — 블로그 랜딩 생성 (단순 구조 재작성판)

기존 `app/services/browser.py`(5,184줄)를 건드리지 않고, **필요한 환경설정만 재사용해서** 처음부터 다시 만든 최소 구조입니다.

## 재사용하는 것 / 새로 만든 것

| 재사용 | 새로 작성 |
|---|---|
| `../.env` (서비스계정 경로 · 시트 ID · 프로필 경로) | 시트 조회 · 브라우저 · 복사 · 후처리 · 정렬 · 발행 전부 |
| `../.venv` (playwright / gspread) | |
| `../playwright-profile` (Chromium 프로필) | |
| `../out/` (로그 · 스크린샷) | |

## 파일

```
v2/config.py   .env 로드
v2/logger.py   콘솔 + out/v2_*.log 동시 기록
v2/sheets.py   `참고용 랜딩 ` 시트 → 매체+결핍+검수용/실전용 → 참고 URL 1건
v2/browser.py  Chromium 실행 · 수동 로그인 대기 · 에디터 frame 탐색
v2/source.py   기준글 수정화면 읽기 + 컴포넌트 단위 복사 (원본은 절대 수정/저장 안 함)
v2/writer.py   새 글: 모바일 전환 → 제목 → 컴포넌트별 붙여넣기 → 후처리 → 정렬 → 발행
v2/run.py      CLI · 단계 로그 · 실패 시 중단
```

## 실행

```powershell
cd C:\Users\894플러스\blog_landing_generator

# 시트 현황만 보기
.\.venv\Scripts\python.exe -m v2.run --list

# 테스트 모드 — 1개 생성, 발행 안 함 (기본값)
.\.venv\Scripts\python.exe -m v2.run --media gfa --deficiency "팔자 / 머니" --kind 검수용

# 실제 발행 (되돌릴 수 없음)
.\.venv\Scripts\python.exe -m v2.run --media gfa --deficiency "팔자 / 머니" --count 5 --publish
```

| 옵션 | 뜻 |
|---|---|
| `--media` | gfa / 카모 / 메타 / 틱톡 (별칭 허용: 카카오, meta, tiktok …) |
| `--deficiency` | 결핍. 시트 값과 **정확히 일치**해야 함 (예: `"팔자 / 머니"`) |
| `--kind` | `검수용`(기본) / `실전용` |
| `--count` | 생성 개수. 한 글씩 `생성 → 검증 → 발행 → 다음 글` |
| `--publish` | 붙여야 실제 발행. 발행 직전 **댓글 허용 OFF** 확인 실패 시 발행 중단 |
| `--url` | 시트 무시하고 참고 URL 직접 지정 (`(연습)` 행처럼 매체 칸이 빈 경우) |
| `--blog-id` | 내 블로그 ID 직접 지정 |
| `--hold` | 발행 안 하는 모드에서 결과 확인용으로 창을 열어 둘 초 (기본 300) |
| `--keep-open` | 끝나도 창을 닫지 않음 (Ctrl+C 로 종료) |

## 로그인

자동 로그인하지 않습니다. Chromium 이 네이버 로그인 화면을 띄우면 **사람이 직접 로그인**하고,
프로그램은 `NID_SES` 쿠키가 생기는지 2초마다 확인하다가 완료되면 이어서 진행합니다(최대 10분).

⚠️ **기준글 소유 계정으로 로그인해야 합니다.** 기준글을 `?Redirect=Update` 수정 화면으로 열어
에디터 구조를 그대로 읽기 때문입니다. 새 글도 로그인한 그 블로그에 작성됩니다.
계정이 다르면 `[주의] 기준글 소유 계정 ≠ 로그인 계정` 로그가 뜹니다.

## 동작 방식 (핵심)

1. **본문 전체를 한 번에 Range 로 잡지 않습니다.** `.se-component` 를 하나씩
   `setStartBefore/​setEndAfter` 로 선택 → `execCommand('copy')` → 새 글 탭에서 `Ctrl+V`.
   텍스트/이미지 순서가 그대로 유지됩니다.
2. **컴포넌트마다 그 자리에서 검증**합니다. 글자수 증가·이미지수 증가·"본문 끝에 붙었는지"를
   확인하고 어긋나면 즉시 중단합니다.
3. 컴포넌트 판별은 **index 가 아니라 내용**으로 합니다(글자수/이미지수).
   제목(`.se-documentTitle`) · `추가할 컴포넌트를 선택하세요` 자리표시자 · 빈 컴포넌트는 제외.
4. 문단 삭제는 **`<p>` 자체만** 합니다. 상위 `se-component` 로 올라가면 본문이 통째로 날아갑니다.
5. 링크카드(`oglink`) 안이거나 사람이 읽는 링크 문구가 있는 문단은 **보존**합니다
   (제품 링크·제품 이미지 보호). 보존한 것은 `보존(링크카드)` 로그로 남습니다.
6. 검증에 실패하면 **발행하지 않습니다.**

## 실측으로 확정된 것 (추측 금지)

- 수정 화면은 URL 직접 진입: `blog.naver.com/{id}?Redirect=Update&logNo={no}`.
  화면에 누를 '수정' 버튼이 없어 버튼 탐색은 실패한다.
- 수정 화면의 실제 에디터는 **`about:blank` 중첩 iframe** 안에 있다 →
  frame 을 URL 로 거르면 절대 못 찾는다. `.se-component` 개수로 frame 을 고른다.
- `Ctrl+C` 는 브라우저 창이 OS 포커스를 가져야만 시스템 클립보드에 닿는다 →
  `document.execCommand('copy')` 를 쓴다. `Ctrl+V` 는 정상.
- `Ctrl+A` 금지 — activeElement 가 body 면 페이지 전체(제목·메뉴)가 잡힌다.
- 제목/본문은 `locator.fill()` / `execCommand insertText` 로 안 들어간다(거짓 성공).
  placeholder 를 클릭해 캐럿을 잡고 `keyboard.type` / `Ctrl+V`.
- 정렬은 2단계: 정렬 드롭다운 열기 → `button.se-toolbar-option-align-center-button`.
  텍스트 문단은 paste 시점에 이미 가운데. 이미지 섹션만 하나씩 처리해야 한다.
- '작성 중인 글이 있습니다' 팝업은 **최대 10초 폴링** 후 '취소'. 0초 판정 금지.
- 새 글은 **항상 새 탭**으로 연다. 같은 탭을 재사용하면 앞서 만든 글이 날아간다.

---

## 프로그램(GUI) — 2026-08-27 추가

`블로그랜딩.bat` 더블클릭 (또는 `.\.venv\Scripts\pythonw.exe -m v2.gui`).

GUI 는 Playwright 를 직접 부르지 않는다. 고른 값으로 **기존 CLI 명령을 그대로 만들어**
자식 프로세스로 돌리고 출력만 보여 준다 → 터미널로 돌리던 것과 동작이 같다.

    gui.py ──▶ job.py (Job → argv) ──▶ runner.py (subprocess + 이벤트)
                                          └─▶ v2.run / v2.run_production  (기존 그대로)

| 새 모듈 | 하는 일 |
|---|---|
| `accounts.py` | `accounts.json` 로더. **계정 추가 = JSON 한 덩어리 추가(코드 수정 없음)** |
| `session_store.py` | `sessions/<계정>/profile` + `state.json`(쿠키) + `meta.json` |
| `catalog.py` | 기준랜딩 시트 → 매체·결핍 목록(캐시 `out/catalog_*.json`) |
| `job.py` | 실행 1건 설정 → argv. Streamlit 으로 옮겨도 그대로 쓴다 |
| `runner.py` | 자식 프로세스 실행 · `@@EVENT` 파싱 · 트리 종료(중단) |
| `session.py` | 계정 세션 CLI(`--list/--login/--check/--adopt/--clear`) |
| `gui.py` | Tkinter 창 (표준 라이브러리, 새 의존성 없음) |

### 계정 (`accounts.json`)
```json
{"accounts": [
  {"id": "my_account", "label": "내 계정", "blog_id": "my_blog_id",
   "ref_tab": "스마일 현미 기준랜딩", "media": "카카오모먼트", "enabled": true}
]}
```
* `id` = `sessions/<id>/` 폴더 이름. `ref_tab` = 그 계정이 쓰는 기준랜딩 탭.
* `blog_id` 를 채워 두면 **선택한 계정 ≠ 로그인된 계정이면 작성 전에 멈춘다**(오발행 방지).

### 계정 세션
    .\.venv\Scripts\python.exe -m v2.session --list                # 현황(브라우저 안 켬)
    .\.venv\Scripts\python.exe -m v2.session --adopt <계정id> # 기존 프로필 복사(2차 인증 회피)
    .\.venv\Scripts\python.exe -m v2.session --login my_account # 로그인 창 → 세션 저장
    .\.venv\Scripts\python.exe -m v2.session --check my_account # 저장 세션 살아 있나(headless)

`NID_SES` 는 세션 쿠키라 프로필에 안 남는다 → 창을 닫기 전에 `state.json` 으로 빼 두고,
다음 실행에서 심는다. 계정마다 파일이 달라 서로 덮어쓰지 않는다.

### CLI 에 추가된 옵션 (기존 옵션·동작은 그대로)
| 옵션 | run | run_production | 설명 |
|---|:--:|:--:|---|
| `--account` | ○ | ○ | 계정 선택. 없으면 예전처럼 `playwright-profile` 하나를 쓴다 |
| `--ref-tab` | ○ | (이미 있음) | 기준랜딩 탭. 없으면 계정 설정 → 기본값 |
| `--dry-run` | ○ | (이미 있음) | 시트 매칭만 확인, 브라우저 안 켬 |
| `--events` | ○ | ○ | `@@EVENT {json}` 한 줄 추가 출력(GUI 용). 사람 로그는 그대로 |

이벤트: `run_started` · `plan` · `post_ready` · `published` · `post_failed` ·
`sheet_written` · `run_finished`. `--events` 없이도 `out/<tag>_<시각>.events.jsonl`
에는 항상 쌓인다(나중에 Supabase 적재용).

### 나중에 Streamlit + Supabase
`gui.py` 만 `streamlit_app.py` 로 갈아끼우고, `accounts.py`/`catalog.py` 의 저장소를
JSON → Supabase 테이블로 바꾸면 된다. `job.py`/`runner.py`/`v2` 코어는 그대로.

---

## Streamlit + 로컬 에이전트 — 2026-08-27

    실행_Streamlit.bat        에이전트(최소화 창) + Streamlit UI 를 한 번에
    에이전트만_실행.bat        다른 PC/터미널에서 에이전트만

    Streamlit(UI) ──submit──▶  queue/  ──claim──▶  Local Agent  ──▶ v2.run / v2.run_production
                     ▲                                  │
                     └────────── 로그 · @@EVENT ─────────┘

**UI 는 Playwright 를 직접 실행하지 않는다.** 작업을 큐에 넣기만 하고, 실행은 그 PC 의
에이전트(`v2.agent`)가 한다. 그래서 UI 를 다른 PC 에 두어도 구조가 그대로다.

| 모듈 | 하는 일 |
|---|---|
| `queue_store.py` | `JobStore` 계약 + `LocalStore`(`queue/` 폴더). `get_store()` 로 백엔드 교체 |
| `agent.py` | 큐에서 작업을 집어 이 PC 에서 실행. **'어느 PC 에서 도는가'를 담당하는 유일한 곳** |
| `streamlit_app.py` | 화면. 큐만 본다(Playwright 를 모른다) |

`queue/` 구조 — `pending/` `running/` `finished/` `logs/<id>.log` `logs/<id>.events.jsonl`
`cancel/<id>` `agents/<agent>.json`. 작업 집기(claim)는 `os.rename` 이라 에이전트가 여럿이어도
한 건을 두 번 집지 않는다.

### 다른 PC 로 넓힐 때 (Supabase)
1. `queue_store.py` 에 `SupabaseStore(JobStore)` 를 하나 추가하고(같은 메서드),
   `get_store()` 가 `BLOG_QUEUE_BACKEND=supabase` 일 때 그걸 돌려주게 한다.
2. 각 PC 에서 `python -m v2.agent` 를 띄운다. **UI·에이전트 코드는 안 고친다.**
3. 특정 PC 에서만 돌려야 하면 `submit(..., target_agent="PC이름")` 을 쓴다(이미 지원).

★네이버 로그인 세션(`sessions/<account>/`)은 **절대 큐로 나가지 않는다.** 계정 쿠키는 실행하는
PC 안에만 남는다. 그래서 새 PC 를 붙이면 그 PC 에서 `-m v2.session --login <계정>` 을 한 번 해야 한다
(UI 의 [로그인 창 열기] 버튼은 그 PC 의 에이전트에게 로그인 작업을 시키는 것이다).

### 남는 것들
`v2/gui.py`(Tkinter)는 그대로 두었다 — 더 확장하지 않는다. CLI(`v2.run` / `v2.run_production`)는
아무 것도 바뀌지 않았다.

---

## 브랜드 분리 (기준시트 + UTM 빌더) — 2026-08-31

브랜드가 **최상위 기준**이다. 브랜드를 고르면 **기준시트와 UTM 빌더가 한 세트로** 따라온다.

    brands.json ─▶ v2/brands.py ─▶ sheets.set_brand()        (블로그 랜딩 기준시트)
                                └▶ landing_sheet.set_brand()  (UTM 빌더)

    Streamlit(브랜드 선택) ─▶ Job.brand ─▶ 큐(record.brand) ─▶ agent ─▶ --brand ─▶ CLI

| 브랜드 | 기준시트 | UTM 빌더 |
|---|---|---|
| 리퓨어리 (`repurely`, 기본) | 리퓨어리 블로그 랜딩 기준시트 | 리퓨어리 UTM 빌더 |
| 닥터누센트 (`doctor_nuscent`) | 닥터누센트 블로그 랜딩 기준시트 | 닥터누센트 UTM 빌더 |

- **시트 ID 가 코드에 하드코딩된 곳은 이제 없다.** `brands.json` 한 곳에서만 관리한다
  (`v2/brands.py` 의 `DEFAULTS` 는 파일이 없거나 깨졌을 때의 안전망 = 리퓨어리 기존 값).
- **브랜드가 늘면 `brands.json` 에 한 덩어리만 추가**한다. 브랜드별 실행 로직은 만들지 않는다.
- 매체 탭 이름은 `utm_tab_pattern`(`{media} 블로그 랜딩 UTM 빌더`)으로 만든다. 규칙에서
  벗어나는 매체만 `utm_media_tabs` 에 직접 적는다. 컬럼명이 다르면 `headers` 로 맞춘다
  (`link`/`media`/`date`/`seq`/`product`/`campaign`/`product_def`/`changed`/`production`).
- **UTM 규칙은 새로 만들지 않았다.** UTM 빌더 `링크`(I) 열에 이미 완성된 최종 URL이 들어 있고,
  실전용은 예전처럼 그 값을 행마다 그대로 쓴다.

### 혼용 방지 (중요)
- `sheets._open` / `landing_sheet._open` 은 인자로 어떤 sheet_id 가 와도 **선택한 브랜드의
  시트만** 연다.
- 브랜드를 바꾸면 `sheets.set_brand()` 가 `set_tab()` 으로 잡아 둔 탭도 지운다.
- 계정(`accounts.json`)의 `ref_tab` 은 **그 계정의 `brand` 와 같을 때만** 쓴다
  (`Account.tab_for_brand()`). 비워 두면 리퓨어리로 본다.
- 카탈로그 캐시는 `out/catalog_<브랜드>_<탭>.json` 으로 브랜드마다 분리된다.
- 큐 레코드에 `brand` 가 들어가고 제목에도 `[브랜드]` 가 붙는다 — 큐 파일만 봐도 구분된다.

### CLI
    -m v2.run             --brand doctor_nuscent ...     # 검수용
    -m v2.run_production  --brand doctor_nuscent ... --on-error skip

- `--brand` 를 **주지 않으면 예전과 100% 같다**(리퓨어리). 기존 명령/스케줄은 손대지 않아도 된다.
- `--on-error abort`(기본) = 예전 그대로 배치를 통째로 멈춤 / `skip` = 그 건만 실패 처리하고
  나머지는 계속(Streamlit 은 기본으로 skip 을 보낸다. 체크박스로 끌 수 있다).

### 단계 로그 (@@EVENT)
`stage` 이벤트로 어디서 실패했는지 바로 보인다.

    [단계] brand=doctor_nuscent stage=utm_sheet_access status=failed reason=permission_denied

단계: `brand_config` · `reference_sheet_selected` · `utm_sheet_selected` · `row_match`
· `product_url_lookup` · `product_link_find` · `product_link_remove` ·
`product_link_insert` · `product_link_verify` · `sheet_mark_done`.
`reason` 은 `permission_denied` / `sheet_not_found` / `tab_not_found` /
`google_temporary_error` / `product_url_missing` / `product_url_invalid` 등.

### 회귀 테스트
    .\.venv\Scripts\python.exe scripts\test_streamlit_brands.py

브라우저 없이 브랜드 설정 → 시트 전환 → Job argv → 큐/에이전트 → Streamlit(AppTest) →
안전장치까지 확인한다.

### Streamlit 실사용 화면 (2026-08-31 마무리)
사이드바 ① 브랜드 → ② 계정 → ③ 에이전트, 본문 왼쪽 = 작업 설정, 오른쪽 = 최근 실행 기록,
아래 = 진행 단계/로그/발행 URL.

- **브랜드 `ready:false`(준비 중)** — 목록에 `(준비 중)` 으로 보이고, 설정 화면 대신 안내가
  뜨며 **Dry-run/실전 실행 버튼이 잠긴다**. CLI 쪽에서도 `Brand.require_ready()` 로 한 번 더
  막으므로 큐로 밀어 넣어도 실행되지 않는다. 준비가 끝나면 `brands.json` 에서 true 로만 바꾼다.
- **brands.json 을 못 읽으면** 화면은 `strict=True` 로 읽어 **다른 브랜드로 대체하지 않고 멈춘다**.
  (CLI 는 `--brand` 없이 돌던 기존 명령을 지키려고 예전처럼 내장 기본값으로 산다.)
- 고급 옵션 expander 에 기존 CLI 옵션을 그대로 노출한다 — `--ref-tab` `--batch` `--start`
  `--copy-mode` `--ref-copy-from` `--no-sheet` `--url` `--product-url` `--sheet-product`.
- `실행 전 확인` 에 브랜드·계정·작업 종류·매체·결핍·날짜·캠페인·건수를 항상 보여 주고,
  [시트 조회해서 매칭 확인] 을 누르면 **읽기 전용으로** 매칭 행과 행별 최종 제품 URL 을 표로 띄운다.
- 진행 단계 탭은 `@@EVENT` 의 `stage` 를 사람이 읽는 이름으로 바꿔 ✅/❌ 로 보여 준다.
