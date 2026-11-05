r"""운영과 완전히 분리된 로컬 테스트 환경을 **만든다**(실행은 하지 않는다).

    .\.venv\Scripts\python.exe scripts\setup_local_testenv.py            # 안전성 검사만
    .\.venv\Scripts\python.exe scripts\setup_local_testenv.py --build    # 실제 생성

★무엇을 보장하나
  · `BLOG_DATA_DIR` 를 **임시 폴더**로 두어 운영 `%APPDATA%\BlogLandingAgent` 를
    읽지도 쓰지도 않는다(아래 ⑤ 예외 1건은 **읽기 전용**이고 그대로 남겨 둔다).
  · `BLOG_QUEUE_BACKEND=local` → Supabase 에 **접속하지 않는다**.
  · `BLOG_ACCOUNTS_REMOTE=0` → 계정 목록 RPC 를 부르지 않는다.
  · 테스트 폴더에 `.env` 를 두지 않는다 → 구글 인증정보가 없어 **시트 호출이
    아예 불가능**하다. 기준랜딩 탭은 캐시 파일로 넣는다.
  · 운영 계정·세션·쿠키·인증정보를 **복사하지 않는다**(합성 값만 만든다).
  · ★**Agent 를 실행하지 않는다.** 화면이 필요한 것은 `queue/agents/*.json`
    하트비트 파일뿐이므로 그것을 **손으로 써 넣는다**. 그래서 Playwright·네이버
    접속·실제 발행이 **구조적으로 일어날 수 없다.**

★검증할 수 없는 것(별도 Supabase 프로젝트가 있어야 한다)
  · Supabase 조회 제한시간·오류 처리 · PostgREST 서버 필터 동작
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROD = Path(os.environ.get("APPDATA") or Path.home()) / "BlogLandingAgent"
BUILD = "--build" in sys.argv

OK, NG, WARN = "  [o]", "  [X]", "  [!]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def note(name: str, extra: str = "") -> None:
    print(WARN + f" {name}" + (f" - {extra}" if extra else ""))


def head(t: str) -> None:
    print("\n" + "=" * 76)
    print(f" {t}")
    print("=" * 76)


# ══════════════════════════════════════════════════════════════════
head("① 외부 네트워크 호출 가능성 — 어떤 경로가 열려 있나")

SRC = {p.name: p.read_text(encoding="utf-8")
       for p in (ROOT / "v2").glob("*.py")}
APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")


def imports_of(name: str) -> set[str]:
    """그 모듈이 **직접 import 하는** v2 모듈 이름."""
    out = set()
    try:
        tree = ast.parse(SRC.get(name, ""))
    except Exception:                                          # noqa: BLE001
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("."):
            out.add((n.module or "").lstrip("."))
        if isinstance(n, ast.ImportFrom) and n.level and not n.module:
            out |= {a.name for a in n.names}
    return out


net = {m for m, s in SRC.items()
       if any(k in s for k in ("import requests", "import gspread",
                              "from playwright", "import playwright"))}
print(f"   네트워크를 쓸 수 있는 v2 모듈: {sorted(m[:-3] for m in net)}")
agent_imports = imports_of("agent.py")
print(f"   v2/agent.py 가 직접 import 하는 것: {sorted(agent_imports)}")
check("v2/agent.py 는 gauth(구글 토큰 교환)를 import 하지 않는다",
      "gauth" not in agent_imports)
check("v2/agent.py 는 shared_cred(인증정보 복사)를 import 하지 않는다",
      "shared_cred" not in agent_imports)
check("streamlit_app.py 는 gauth 를 쓰지 않는다", "gauth" not in APP)
check("pairing 의 쓰기 함수는 화면·Agent 루프에서 불리지 않는다",
      "save_config(" not in APP and "pairing.pair(" not in APP
      and "save_config(" not in SRC.get("agent.py", ""))
note("★Playwright·네이버 접속은 **작업을 실행할 때만** 일어난다",
     "그래서 이 환경은 Agent 를 띄우지 않는다(하트비트 파일로 대체)")
note("★agent_tray.py(트레이 GUI)는 gauth·pairing 쓰기를 한다",
     "테스트에서 **실행하지 않는다**")

head("② 운영 경로 참조 — BLOG_DATA_DIR 가 덮지 못하는 곳")
hard = []
for name, s in SRC.items():
    for ln_no, ln in enumerate(s.split("\n"), 1):
        if "getenv(\"APPDATA\")" in ln and "appdir" not in name:
            hard.append(f"{name}:{ln_no}")
print(f"   APPDATA 를 직접 쓰는 곳: {hard or '없음'}")
check("appdir.ROOT 는 BLOG_DATA_DIR 를 존중한다",
      'override = (os.getenv("BLOG_DATA_DIR") or "").strip()'
      in SRC.get("appdir.py", ""))
pair_src = SRC.get("pairing.py", "")
pair_hard = 'DEVICE_DIR = Path(os.getenv("APPDATA") or Path.home())' in pair_src
note("★`v2/pairing.py` 의 DEVICE_DIR 는 **BLOG_DATA_DIR 를 따르지 않는다**",
     "항상 운영 %APPDATA%\\BlogLandingAgent 를 본다") if pair_hard else None
check("그 경로를 **읽는** 호출만 남아 있다(device.json 의 label·token)",
      "load_device()" in SRC.get("agent.py", ""))
#  ★개수로 세지 않는다 — 쓰는 **이름**이 파일을 건드리는지로 본다.
SAFE_PAIRING = {"supports", "NEEDS", "ver_tuple", "AGENT_VERSION"}
used = set(re.findall(r"pairing\.([A-Za-z_]+)", APP))
check("화면이 쓰는 pairing 이름은 모두 '파일을 건드리지 않는' 것이다",
      used <= SAFE_PAIRING, f"사용: {sorted(used)}")
check("  device.json·agent_config.json 을 읽는 이름은 쓰지 않는다",
      not (used & {"load_device", "_config", "save_config", "pair",
                   "pair_with_code", "load_config"}))
print("""
   → 결론: 테스트가 운영 폴더를 **쓰는 경로는 없다.** 읽기는 두 곳뿐이고
     (`device.json` 의 label / `agent_config.json`), 그마저도 Agent 를 띄우지
     않으면 일어나지 않는다. 화면은 `pairing.supports()` 만 쓴다(파일 접근 없음).""")

head("③ 큐·계정·시트 격리 설정")
check("BLOG_QUEUE_BACKEND=local 이면 LocalStore 를 쓴다",
      'name = (backend or os.getenv("BLOG_QUEUE_BACKEND") or "local")'
      in SRC.get("queue_store.py", ""))
check("BLOG_ACCOUNTS_REMOTE=0 이면 계정 RPC 를 부르지 않는다",
      "BLOG_ACCOUNTS_REMOTE" in SRC.get("accounts.py", ""))
check("구글 설정은 ROOT/.env 에서 읽는다(테스트 폴더에 두지 않는다)",
      'load_dotenv(ROOT / ".env")' in SRC.get("config.py", ""))
check("기준랜딩 탭은 캐시 파일로 대신할 수 있다(시트 호출 없이)",
      'return ROOT / "out" / f"tabs_{_slug(bid)}.json"'
      in SRC.get("catalog.py", ""))

head("④ Agent 없이 화면을 검증할 수 있나")
check("LocalStore.agents() 는 queue/agents/*.json 을 읽는다",
      'for p in (self.root / "agents").glob("*.json")'
      in SRC.get("queue_store.py", ""))
check("  alive 는 `at`(시각)으로만 판정된다 → 파일로 흉내 낼 수 있다",
      'rec["alive"] = fresh' in SRC.get("queue_store.py", ""))
check("화면의 로그아웃 게이트는 하트비트의 version 을 본다",
      'pairing.supports((device or {}).get("version")' in APP)
print("""
   → 그래서 `queue/agents/<id>.json` 에 version·state·job 을 써 넣으면
     Agent 1.2.5 / 1.3.0 게이트를 **실행 없이** 재현할 수 있다.""")

# ══════════════════════════════════════════════════════════════════
head("⑤ 테스트 폴더 생성" + ("" if BUILD else " — 계획만(--build 로 실제 생성)"))

FIX = Path(tempfile.gettempdir()) / "blog_testenv"
print(f"   위치            : {FIX}")
print(f"   운영 폴더       : {PROD}  ← 건드리지 않는다")
PLAN = [
    ("brands.json / accounts.json / out/tabs_*.json",
     "합성 fixture(scripts/make_test_fixture.py) — 가짜 시트 ID"),
    ("queue/{pending,running,held,finished,agents,logs}",
     "LocalStore 폴더"),
    ("queue/agents/TESTPC.json",
     "가짜 하트비트 — version 을 1.2.5 / 1.3.0 으로 바꿔 게이트 시험"),
    ("accounts.json 의 테스트 계정",
     "같은 login_id 를 쓰는 `tst_re` / `tst_dn` + 다른 ID `tst_other`"),
    (".env", "★만들지 않는다 — 구글 인증이 없어야 시트 호출이 불가능하다"),
    ("sessions/", "★비워 둔다 — 운영 쿠키를 복사하지 않는다"),
]
for a, b in PLAN:
    print(f"     · {a:<44} {b}")

ENVS = {
    "BLOG_DATA_DIR": str(FIX),
    "BLOG_QUEUE_BACKEND": "local",
    "BLOG_ACCOUNTS_REMOTE": "0",
}
print("\n   실행에 쓸 환경변수")
for k, v in ENVS.items():
    print(f"     set {k}={v}")
print("""
   실행 방법(승인 후 사용자가 직접)
     set BLOG_DATA_DIR=%TEMP%\\blog_testenv
     set BLOG_QUEUE_BACKEND=local
     set BLOG_ACCOUNTS_REMOTE=0
     .venv\\Scripts\\python.exe -m streamlit run streamlit_app.py --server.port 8599
   ★Agent 는 띄우지 않는다. 포트도 운영과 다르게 둔다.""")

if BUILD:
    import make_test_fixture

    if FIX.exists():
        shutil.rmtree(FIX)
    FIX.mkdir(parents=True)
    make_test_fixture.build(FIX)
    for sub in ("pending", "running", "held", "finished", "agents", "logs",
                "cancel", "plans"):
        (FIX / "queue" / sub).mkdir(parents=True, exist_ok=True)
    (FIX / "sessions").mkdir(exist_ok=True)

    #  같은 login_id 를 쓰는 두 등록 + 다른 ID 하나(합성)
    accs = json.loads((FIX / "accounts.json").read_text(encoding="utf-8"))
    rows = accs.get("accounts") or []
    tabs = json.loads((FIX / "out" / "tabs_repurely.json").read_text(
        encoding="utf-8")).get("tabs") or [""]
    rows += [
        {"id": "tst_re", "label": "테스트 계정(리퓨어리)", "login_id": "tstlogin",
         "blog_id": "tstblog", "ref_tab": tabs[0], "brand": "repurely",
         "media": "", "enabled": True, "note": "테스트 전용"},
        {"id": "tst_dn", "label": "테스트 계정(프리즈젯)", "login_id": "tstlogin",
         "blog_id": "tstblog", "ref_tab": tabs[0], "brand": "repurely",
         "media": "", "enabled": True, "note": "테스트 전용 — 같은 login_id"},
        {"id": "tst_other", "label": "다른 테스트 계정", "login_id": "otherlogin",
         "blog_id": "otherblog", "ref_tab": tabs[0], "brand": "repurely",
         "media": "", "enabled": True, "note": "테스트 전용"},
    ]
    (FIX / "accounts.json").write_text(
        json.dumps({"accounts": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    #  가짜 하트비트 — Agent 를 띄우지 않는다
    (FIX / "queue" / "agents" / "TESTPC.json").write_text(json.dumps({
        "agent": "TESTPC", "host": "TESTPC", "state": "idle", "job": "",
        "version": "1.2.5",          # ← 1.3.0 으로 바꾸면 로그아웃 게이트가 열린다
        "pid": 0, "at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n   생성 완료 — {FIX}")
    print("   ★하트비트의 `at` 은 30초면 만료된다. 화면 시험 직전에")
    print("     scripts/setup_local_testenv.py --build 를 다시 돌려 갱신하면 된다.")
    #  운영 폴더를 건드리지 않았는지 마지막 확인
    check("운영 폴더에 테스트 흔적이 없다",
          not (PROD / "accounts.json").exists()
          or json.loads((PROD / "accounts.json").read_text(
              encoding="utf-8")).get("accounts", [{}])[0].get("id")
          != "tst_re")

print("\n" + "=" * 76)
if fails:
    print(f" 안전성 검사 실패 {len(fails)}건: {fails}")
else:
    print(" 안전성 검사 전부 통과")
print(" ★이 스크립트는 화면·Agent 를 **실행하지 않는다.**")
print("=" * 76)
sys.exit(1 if fails else 0)
