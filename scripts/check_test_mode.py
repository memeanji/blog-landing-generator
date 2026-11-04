r"""로컬 Streamlit 을 띄우기 **전에** 격리 상태를 확인한다 (2026-11-04).

    .\.venv\Scripts\python.exe scripts\check_test_mode.py

하나라도 어긋나면 **0 이 아닌 코드로 끝난다** → 실행 스크립트가 거기서 멈춘다.

확인하는 것
    ① BLOG_DATA_DIR 이 **운영 폴더가 아닌** 테스트 fixture 를 가리킨다
    ② 그 fixture 가 합성 데이터다([테스트] 브랜드·계정만 있다)
    ③ 큐 백엔드가 **로컬**이다(운영 Supabase 로 작업이 들어가지 않는다)
    ④ 구글 서비스계정·시트 쓰기 수단이 **없다**
    ⑤ device 토큰이 없다(운영 Agent 로 작업을 보낼 수 없다)
    ⑥ 네이버 자동 실행 스위치(ENABLE_EXTERNAL_ACTIONS)가 꺼져 있다
    ⑦ 운영 데이터 폴더(%APPDATA%\BlogLandingAgent)를 쓰지 않는다
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

PROD_DATA = Path(os.getenv("APPDATA") or "") / "BlogLandingAgent"
PROD_REPO = Path(r"C:\Users\894플러스\blog_landing_generator")

bad: list[str] = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("  [o] " if cond else "  [X] ") + name + (f" - {extra}" if extra else ""))
    if not cond:
        bad.append(name)


print("=" * 74)
print(" 테스트 모드 사전 점검")
print("=" * 74)

data_dir = (os.getenv("BLOG_DATA_DIR") or "").strip()
ok("① BLOG_DATA_DIR 이 지정돼 있다", bool(data_dir), data_dir or "(없음)")
p = Path(data_dir).resolve() if data_dir else None
if p is not None:
    ok("① 운영 데이터 폴더가 아니다", p != PROD_DATA.resolve(), str(p))
    ok("① 운영 저장소 폴더가 아니다", p != PROD_REPO.resolve())
    ok("① 폴더가 실제로 있다", p.exists())

if p is not None and (p / "brands.json").exists():
    try:
        rows = json.loads((p / "brands.json").read_text(encoding="utf-8"))
        brands = rows.get("brands") or []
    except Exception as exc:                                   # noqa: BLE001
        brands = []
        ok("② brands.json 을 읽을 수 있다", False, str(exc)[:60])
    ok("② 브랜드가 테스트 전용이다([테스트] 표시)",
       bool(brands) and all("[테스트]" in (b.get("label") or "") for b in brands),
       ", ".join((b.get("label") or "?") for b in brands)[:70])
    ok("② 기준시트 ID 가 가짜다(실수로 통신하면 즉시 실패)",
       all("NO_SUCH_SHEET" in (b.get("reference_sheet_id") or "")
           for b in brands))
else:
    ok("② fixture 에 brands.json 이 있다", False)

if p is not None and (p / "accounts.json").exists():
    accs = (json.loads((p / "accounts.json").read_text(encoding="utf-8"))
            .get("accounts") or [])
    ok("② 계정이 테스트 전용이다([테스트] 표시)",
       bool(accs) and all("[테스트]" in (a.get("label") or "") for a in accs),
       f"{len(accs)}개")
    ok("② 운영 계정 id 가 섞이지 않았다",
       not [a for a in accs if (a.get("id") or "").startswith(
           ("shin", "rhks", "tax", "ggom", "scw", "smile", "tab_", "seoyeon",
            "iammeanji"))])
else:
    ok("② fixture 에 accounts.json 이 있다", False)

backend = (os.getenv("BLOG_QUEUE_BACKEND") or "local").strip().lower()
ok("③ 큐 백엔드가 로컬이다(운영 Supabase 아님)",
   backend in ("", "local", "file"), backend or "local")
sup_url = (os.getenv("SUPABASE_URL") or "").strip()
ok("③ 운영 Supabase 주소가 환경에 없다(또는 루프백이다)",
   not sup_url or "127.0.0.1" in sup_url or "localhost" in sup_url,
   sup_url or "(없음)")

gjson = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
ok("④ 구글 서비스계정 경로가 환경에 없다", not gjson, gjson or "(없음)")
if p is not None:
    ok("④ fixture 에 구글 인증 파일이 없다",
       not list(p.glob("*service*account*.json")))

ok("⑤ device 토큰이 없다", not (os.getenv("BLOG_DEVICE_TOKEN") or "").strip())
if p is not None:
    ok("⑤ fixture 에 device.json 이 없다", not (p / "device.json").exists())

ext = (os.getenv("ENABLE_EXTERNAL_ACTIONS") or "").strip().lower()
ok("⑥ 외부 실행 스위치가 켜져 있지 않다", ext not in ("1", "true", "on", "yes"),
   ext or "(없음)")

if p is not None:
    ok("⑦ 운영 데이터 폴더 밖이다",
       PROD_DATA.resolve() not in [p, *p.parents])

print("=" * 74)
if bad:
    print(f" ❌ 테스트 모드가 아닙니다 — {len(bad)}건: {bad}")
    print("    Streamlit 을 띄우지 않습니다.")
    sys.exit(1)
print(" ✅ 테스트 모드 확인 — 운영과 격리된 상태입니다.")
print("=" * 74)
