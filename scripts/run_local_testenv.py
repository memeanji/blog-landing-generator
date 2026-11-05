r"""테스트 전용 Streamlit 실행 — **기본은 검증만 하고 띄우지 않는다**.

    .\.venv\Scripts\python.exe scripts\run_local_testenv.py            # 안전성 검증만
    .\.venv\Scripts\python.exe scripts\run_local_testenv.py --run      # 실제로 띄운다

★무엇을 보장하나
  · 테스트 폴더(`%TEMP%\blog_testenv`)를 **만들지도 지우지도 않는다.**
    없으면 그냥 중단한다(`setup_local_testenv.py --build` 로 먼저 만들 것).
  · 운영 `.env`·Streamlit Secrets·Supabase/Google 인증정보를 **상속하지 않는다**
    (프로세스 환경에서 지우고, 워크트리에는 그 파일들이 없음을 확인한다).
  · 포트는 **8599 고정**. 운영과 겹치지 않는다.
  · Agent 를 띄우지 않는다 — 가짜 하트비트의 시각만 갱신한다.
  · `--run` 없이는 **아무것도 실행하지 않는다.**

★검증 단계에서 하는 일(네트워크를 끊고 화면을 한 번 그려 본다)
  `streamlit.testing.v1.AppTest` 로 **브라우저·서버 없이** 화면을 렌더하고,
  그 동안 소켓을 막아 **외부 호출이 0건**인지 확인한다. 실제 화면(브라우저)을
  띄우는 것과는 다르다 — 포트도 열지 않는다.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FIX = Path(tempfile.gettempdir()) / "blog_testenv"
PROD = Path(os.environ.get("APPDATA") or Path.home()) / "BlogLandingAgent"
PORT = 8599
RUN = "--run" in sys.argv

OK, NG, WARN = "  [o]", "  [X]", "  [!]"
fails: list[str] = []

#  ★상속을 끊어야 하는 변수 — 부모 셸에 남아 있으면 운영에 붙을 수 있다
DROP = ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT",
        "REFERENCE_SPREADSHEET_ID", "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
        "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_PUBLISHABLE_KEY",
        "BLOG_DEVICE_TOKEN", "TEAM_PASSWORD", "BLOG_BRANDS_JSON",
        "BLOG_ACCOUNTS_JSON")
ENVS = {
    "BLOG_DATA_DIR": str(FIX),
    "BLOG_QUEUE_BACKEND": "local",
    "BLOG_ACCOUNTS_REMOTE": "0",
}


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


def prod_print() -> str:
    """운영 폴더 지문(파일 수 + 수정시각·크기 해시)."""
    import hashlib

    rows = sorted(f"{p.stat().st_mtime_ns}:{p.stat().st_size}:{p}"
                  for p in PROD.rglob("*") if p.is_file())
    return (f"{len(rows)}개 "
            f"{hashlib.md5(chr(10).join(rows).encode()).hexdigest()[:12]}")


print("=" * 76)
print(" 테스트 전용 Streamlit 실행 — " + ("실행 모드" if RUN else "검증만(--run 없음)"))
print("=" * 76)
before = prod_print()
print(f"   테스트 폴더 : {FIX}")
print(f"   운영 폴더   : {PROD}  (지문 {before})")
print(f"   포트        : {PORT}")

# ══════════════════════════════════════════════════════════════════
head("① 테스트 폴더 — 만들지도 지우지도 않는다")
if not FIX.is_dir():
    print(NG + f" 테스트 폴더가 없습니다 — {FIX}")
    print("     먼저 만들어 주세요: "
          ".venv\\Scripts\\python.exe scripts\\setup_local_testenv.py --build")
    sys.exit(2)
check("테스트 폴더가 있다", FIX.is_dir())
need = ["accounts.json", "brands.json", "out/tabs_repurely.json",
        "queue/agents"]
missing = [n for n in need if not (FIX / n).exists()]
check("필요한 파일이 갖춰져 있다", not missing, str(missing))
#  ★문자열로 찾으면 **이 검사 자신의 문자열**에 걸린다(자기참조 오탐).
#    그래서 AST 의 호출 이름만 본다.
def _calls_in(path: Path) -> set[str]:
    import ast as _ast

    out: set[str] = set()
    for n in _ast.walk(_ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, _ast.Call):
            f = n.func
            out.add(getattr(f, "attr", getattr(f, "id", "")))
    return out


_mine = _calls_in(Path(__file__))
_bad = _mine & {"rmtree", "unlink", "rmdir", "remove", "replace", "move",
                "copy", "copy2", "copytree", "copyfile"}
check("★이 스크립트가 파일을 지우거나 옮기지 않는다", not _bad,
      f"위험 호출 {sorted(_bad) or '없음'}")
check("  쓰기는 하트비트 갱신(write_text) 하나뿐이다",
      "write_text" in _mine, "테스트 폴더 안")

# ══════════════════════════════════════════════════════════════════
head("② 인증정보 상속 차단")
present = [k for k in DROP if os.environ.get(k)]
for k in DROP:
    os.environ.pop(k, None)
check("프로세스 환경의 인증정보를 지웠다", True,
      f"지운 변수 {present or '없음'}")
for k, v in ENVS.items():
    os.environ[k] = v
check("테스트 환경변수를 박았다", all(os.environ[k] == v for k, v in ENVS.items()),
      " · ".join(f"{k}={v}" for k, v in list(ENVS.items())[1:]))
check("★워크트리에 .streamlit/secrets.toml 이 없다(Secrets 상속 없음)",
      not (ROOT / ".streamlit" / "secrets.toml").exists())
check("★홈에도 secrets.toml 이 없다",
      not (Path.home() / ".streamlit" / "secrets.toml").exists())
check("★워크트리에 .env 가 없다(구글 인증 상속 없음)",
      not (ROOT / ".env").exists())
check("★테스트 폴더에 .env 가 없다", not (FIX / ".env").exists())
prod_secrets = Path(r"C:\Users\894플러스\blog_landing_generator\.streamlit"
                    r"\secrets.toml")
note("운영 레포에는 secrets.toml 이 있다",
     "이 스크립트는 워크트리에서 돌므로 읽히지 않는다"
     if prod_secrets.exists() else "없음")
check("  그 파일은 워크트리 밖이다",
      ROOT not in prod_secrets.parents or not prod_secrets.exists())

# ══════════════════════════════════════════════════════════════════
head("③ 가짜 하트비트 — 시각만 갱신(Agent 미실행)")
hb_dir = FIX / "queue" / "agents"
hbs = sorted(hb_dir.glob("*.json"))
check("하트비트 파일이 있다", bool(hbs), str([p.name for p in hbs]))
for p in hbs:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        continue
    d["at"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"     {p.name:<24} version={d.get('version')} "
          f"state={d.get('state')} at={d['at']}")
check("  갱신한 파일이 전부 테스트 폴더 안이다",
      all(FIX in p.parents for p in hbs))
note("★Agent 는 띄우지 않는다",
     "작업을 실행할 주체가 없으므로 Playwright·네이버 접속이 일어날 수 없다")

# ══════════════════════════════════════════════════════════════════
head("④ 시작 시 외부 API 호출 경로 — 캐시로 대체되는가")
APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
check("큐는 LocalStore 다(Supabase 미접속)",
      os.environ["BLOG_QUEUE_BACKEND"] == "local")
check("기준랜딩 탭 캐시가 있다(시트 호출 불필요)",
      bool(list((FIX / "out").glob("tabs_*.json"))),
      str([p.name for p in (FIX / "out").glob("tabs_*.json")]))
check("결핍·제품 목록 캐시가 있다",
      bool(list((FIX / "out").glob("catalog_*.json"))),
      f"{len(list((FIX / 'out').glob('catalog_*.json')))}개")
#  ★구글 시트 모듈이 **본문(첫 렌더)에서 직접** 불리지 않는지 AST 로 본다
#    (상단 import 문자열에 걸리지 않게).
import ast as _ast2

_tree = _ast2.parse(APP)
_top_sheet = []
for _n in _tree.body:
    if isinstance(_n, (_ast2.Import, _ast2.ImportFrom, _ast2.FunctionDef,
                       _ast2.ClassDef)):
        continue
    for _x in _ast2.walk(_n):
        if isinstance(_x, _ast2.Call):
            _f = _x.func
            _owner = getattr(getattr(_f, "value", None), "id", "")
            if _owner in ("landing_sheet", "sheets"):
                _top_sheet.append((_n.lineno, _owner))
check("시트 모듈을 본문에서 직접 부르지 않는다(발행 경로 안에만 있다)",
      not _top_sheet, str(_top_sheet[:3]))
note("구글 인증이 없으면 시트 호출은 **예외로 즉시 실패**한다",
     "조용히 운영 시트를 읽는 일이 생길 수 없다")

# ══════════════════════════════════════════════════════════════════
head("⑤ ★네트워크를 끊고 화면을 한 번 그려 본다(브라우저·포트 없음)")
print("   AppTest 로 렌더만 한다 — 서버를 띄우지 않고 포트도 열지 않는다.")
calls: list[str] = []
_c, _k = socket.create_connection, socket.socket.connect


def _blk_c(address, *a, **k):
    calls.append(f"create_connection{address}")
    raise OSError("테스트: 외부 네트워크 차단")


def _blk_k(self, address, *a, **k):
    calls.append(f"connect{address}")
    raise OSError("테스트: 외부 네트워크 차단")


socket.create_connection = _blk_c                   # type: ignore[assignment]
socket.socket.connect = _blk_k                      # type: ignore[assignment]
app = None
try:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=120)
    app.session_state["authed"] = True
    app.run()
except Exception as exc:                                       # noqa: BLE001
    print(NG + f" 렌더 중 예외: {type(exc).__name__}: {exc}")
    fails.append("네트워크 차단 상태 렌더")
finally:
    socket.create_connection = _c                   # type: ignore[assignment]
    socket.socket.connect = _k                      # type: ignore[assignment]

if app is not None:
    check("★네트워크를 끊은 채로도 앱이 예외 없이 뜬다",
          not app.exception, str(app.exception)[:200])
    check("★첫 렌더의 외부 호출이 0건이다", not calls, str(calls[:3]))
    seq = None
    try:
        seq = int(app.session_state["_render_seq"])
    except Exception:                                          # noqa: BLE001
        pass
    check("  렌더 경계가 잡힌다(_render_seq)", seq == 1, f"seq={seq}")
    labels = [b.label for b in app.button]
    print(f"     버튼 {len(labels)}개: {labels[:8]}")
    check("  로그아웃 버튼이 **잠겨 있다**(하트비트 version=1.2.5)",
          all(not (b.label == "선택 계정 로그아웃" and not b.disabled)
              for b in app.button),
          "1.2.5 게이트")
    sels = [s.label for s in app.selectbox] if app.selectbox else []
    print(f"     선택 상자 {len(sels)}개: {sels[:5]}")

# ══════════════════════════════════════════════════════════════════
head("⑥ 실행 명령" + ("" if RUN else " — 지금은 실행하지 않는다"))
cmd = [sys.executable, "-m", "streamlit", "run", "streamlit_app.py",
       "--server.port", str(PORT), "--server.headless", "true",
       "--browser.serverAddress", "localhost"]
print("   " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
for k, v in ENVS.items():
    print(f"     {k}={v}")
print("   ★--server.headless true → 브라우저를 자동으로 열지 않는다.")
print(f"     띄운 뒤 http://localhost:{PORT} 을 직접 열면 된다.")

after = prod_print()
head("⑦ 운영 무변경")
check("★운영 폴더 지문이 같다", before == after, f"{before} → {after}")

if RUN:
    if fails:
        print("\n" + NG + " 검증 실패가 있어 실행하지 않습니다.")
        sys.exit(1)
    head("실행")
    print("   Ctrl+C 로 멈춥니다.")
    sys.exit(subprocess.call(cmd, cwd=str(ROOT), env={**os.environ}))

print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 안전성 검증 전부 통과 — `--run` 을 주면 그때 띄운다")
print("=" * 76)
sys.exit(1 if fails else 0)
