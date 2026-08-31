# 블로그 랜딩 자동 생성 프로그램

로컬 GUI에서 매체, 결핍명, 생성 개수를 선택하고 블로그 랜딩 생성 흐름을 실행하기 위한 MVP입니다.

현재 기본 설정은 `ENABLE_EXTERNAL_ACTIONS=false`입니다. 이 상태에서는 Google Sheets나 네이버 블로그에 접근하지 않고 진행 로그만 출력합니다.

## 실행 준비

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
```

`.env`에 서비스 계정 JSON 경로와 참고용 스프레드시트 ID를 입력합니다. JSON 파일은 `.gitignore`에 포함되어 GitHub에 올라가지 않도록 처리되어 있습니다.

## 실행

```powershell
.\.venv\Scripts\python.exe main.py
```

## Google Sheets 읽기 전용 구조 분석

`ENABLE_EXTERNAL_ACTIONS=false` 상태에서도 아래 스크립트는 읽기 전용 scope만 사용해 `블로그 랜딩 참고용 URL` 시트 구조를 확인합니다. UTM Builder 시트는 읽지 않고, 시트 값도 수정하지 않습니다.

```powershell
.\.venv\Scripts\python.exe scripts\analyze_sheets_readonly.py --media GFA
.\.venv\Scripts\python.exe scripts\analyze_sheets_readonly.py --media GFA --deficiency "결핍명"
```

## MVP 범위

- Tkinter GUI
- 매체, 결핍명, 생성 개수 입력
- 진행 로그
- 외부 작업 차단 기본값
- Google Sheets 구조 분석/조회/참고용 URL 시트 기록 코드 분리
- Playwright 수동 로그인 대기 코드 분리

## 다음 단계

사용자 확인 후 아래 순서로 외부 작업을 하나씩 활성화합니다.

1. Google Sheets 연결 확인
2. `블로그 랜딩 참고용 URL` 시트 구조 분석
3. GFA + 결핍 기준 참고 URL 조회
4. Playwright 브라우저 실행 및 수동 로그인 대기
5. GFA 랜딩 1개 테스트
6. 발행 URL 추출
7. `블로그 랜딩 참고용 URL` 시트에 새 행 기록 테스트

---

## 배포 구조 (Streamlit Community Cloud + 로컬 에이전트)

```
Streamlit Cloud (UI · 큐)  ──submit──▶  큐  ──claim──▶  로컬 PC 에이전트  ──▶  Playwright
                                                         (네이버 로그인 세션은 이 PC 안에만)
```

* **화면은 브라우저 자동화를 하지 않는다.** 고른 값으로 Job 을 만들어 큐에 넣기만 한다.
  실제 네이버 작업은 로컬 PC 에서 도는 `python -m v2.agent` 가 한다.
* 그래서 Streamlit Cloud 에 올려도 **클라우드에서 크로미움이 뜨지 않는다**(애초에 못 뜬다).

### ⚠ 지금 큐는 `queue/` 폴더다 — 클라우드와 내 PC 가 공유되지 않는다
`queue_store.LocalStore` 는 같은 PC 의 폴더를 쓴다. 클라우드 UI 가 넣은 작업은
**클라우드 컨테이너 안의 폴더**에 쌓이고, 집/사무실 PC 의 에이전트는 그것을 보지 못한다.
클라우드에서 실제로 실행하려면 큐를 공유 저장소로 바꿔야 한다 —
`queue_store.py` 에 `SupabaseStore(JobStore)` 를 하나 추가하고
`BLOG_QUEUE_BACKEND=supabase` 로 두면 UI·에이전트 코드는 고치지 않아도 된다(설계된 확장점).

그때까지 클라우드 배포본의 쓰임새는 **화면 확인 · 시트 조회(읽기 전용) · 설정 점검**이고,
**실제 발행은 로컬에서** `실행_Streamlit.bat` 으로 돌리는 것이 맞다.

### Streamlit Cloud 설정
| 항목 | 값 |
|---|---|
| Repository | (내 GitHub 저장소) |
| Branch | `main` |
| Main file path | `streamlit_app.py` |
| Python deps | `requirements.txt` (UI 전용 — Playwright 없음) |

**Secrets** (앱 설정 → Secrets). 최상위 문자열 값은 환경변수로도 들어간다.

```toml
# 구글 서비스계정 키 "내용 자체"(파일 경로가 아니다)
GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT = '''
{"type":"service_account","project_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\n...","client_email":"...@....iam.gserviceaccount.com"}
'''

# 브랜드 설정(brands.json 과 같은 내용). 저장소에는 brands.example.json 만 있다.
BLOG_BRANDS_JSON = '''
{"brands":[{"id":"repurely","label":"리퓨어리","reference_sheet_id":"...","utm_sheet_id":"..."}]}
'''
```

### 로컬 PC 에서 해야 하는 것
```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-agent.txt
.\.venv\Scripts\python.exe -m playwright install chromium

copy .env.example .env            # 서비스계정 JSON 경로를 채운다
copy brands.example.json brands.json   # 실제 시트 ID 를 채운다

.\실행_Streamlit.bat              # 에이전트 + UI 를 한 번에
```
`accounts.json` 은 만들지 않아도 된다 — 기준시트의 `<이름> 기준랜딩` 탭을 고르고
[실행 준비] 를 누르면 그 탭 전용 계정(세션 폴더)이 자동으로 만들어진다.
