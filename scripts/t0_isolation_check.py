r"""T0 — 테스트 Agent 격리 실측. **정적 검증 → 통과하면 작업 없는 상태로 1회 실행.**

    .\.venv\Scripts\python.exe scripts\t0_isolation_check.py          # 정적 검증만
    .\.venv\Scripts\python.exe scripts\t0_isolation_check.py --run    # 정적 + 실제 1회 실행

★무엇을 확인하나
  ① 테스트 전용 APPDATA · BLOG_DATA_DIR · BLOG_QUEUE_BACKEND=local ·
     BLOG_ACCOUNTS_REMOTE=0 가 적용되는가
  ② 운영 `device.json`·`agent_config.json`·세션 파일에 **접근하지 못하는가**
  ③ 운영 Supabase URL·토큰이 Agent 프로세스에 **주입되지 않는가**
  ④ agent_id 가 TESTPC 로 고정되는가
  ⑤ 테스트 큐가 비어 있는가
  ⑥ 실행 전후 운영 Agent 의 파일·프로세스·큐가 그대로인가

★토큰·비밀번호는 **값을 찍지 않는다** — 있는지(bool)와 길이만 본다.
★`--run` 은 `v2.agent --once` 다. 큐가 비어 있으면 **집을 것이 없어 바로 끝난다**
  → Playwright·네이버 접속·발행이 일어나지 않는다.
★운영 Agent 프로세스를 건드리지 않는다(죽이지도, 재시작하지도 않는다).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FIX = Path(tempfile.gettempdir()) / "blog_testenv"
TEST_APPDATA = FIX / "_appdata"
PROD_APPDATA = Path(os.environ.get("APPDATA") or Path.home())
PROD = PROD_APPDATA / "BlogLandingAgent"
AGENT_ID = "TESTPC"
RUN = "--run" in sys.argv

OK, NG, WARN = "  [o]", "  [X]", "  [!]"
fails: list[str] = []

#  Agent 자식 프로세스에 넘길 환경 — 운영 값을 **상속하지 않는다**
DROP = ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT",
        "REFERENCE_SPREADSHEET_ID", "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
        "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_PUBLISHABLE_KEY",
        "BLOG_DEVICE_TOKEN", "TEAM_PASSWORD", "BLOG_BRANDS_JSON",
        "BLOG_ACCOUNTS_JSON")


def child_env() -> dict:
    e = {k: v for k, v in os.environ.items() if k not in DROP}
    e["APPDATA"] = str(TEST_APPDATA)          # ★pairing 격리(이중 안전장치)
    e["BLOG_DATA_DIR"] = str(FIX)
    e["BLOG_QUEUE_BACKEND"] = "local"
    e["BLOG_ACCOUNTS_REMOTE"] = "0"
    e["PYTHONIOENCODING"] = "utf-8"
    #  ★Playwright 실행 자체를 막는다(2026-11-05 · T0 승인 조건).
    #    브라우저 경로를 **빈 폴더**로 돌려 두면, 혹시 작업이 집히더라도
    #    브라우저를 띄우지 못하고 즉시 실패한다 → 네이버에 접속할 수 없다.
    e["PLAYWRIGHT_BROWSERS_PATH"] = str(FIX / "_no_browsers")
    return e


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def note(name: str, extra: str = "") -> None:
    print(WARN + f" {name}" + (f" - {extra}" if extra else ""))


def head(t: str) -> None:
    print("\n" + "=" * 78)
    print(f" {t}")
    print("=" * 78)


def fingerprint(root: Path) -> str:
    rows = sorted(f"{p.stat().st_mtime_ns}:{p.stat().st_size}:{p}"
                  for p in root.rglob("*") if p.is_file())
    return (f"{len(rows)}개 "
            f"{hashlib.md5(chr(10).join(rows).encode()).hexdigest()[:12]}")


print("=" * 78)
print(" T0 — 테스트 Agent 격리 실측 " + ("(정적 + 실행)" if RUN else "(정적 검증만)"))
print("=" * 78)
if not FIX.is_dir():
    print(NG + f" 테스트 폴더가 없습니다 — {FIX}")
    sys.exit(2)
prod_before = fingerprint(PROD)
print(f"   테스트 폴더   : {FIX}")
print(f"   테스트 APPDATA: {TEST_APPDATA}")
print(f"   운영 폴더     : {PROD}")
print(f"   운영 지문(전) : {prod_before}")

# ══════════════════════════════════════════════════════════════════
head("① 테스트 전용 환경변수")
env = child_env()
for k in ("APPDATA", "BLOG_DATA_DIR", "BLOG_QUEUE_BACKEND",
          "BLOG_ACCOUNTS_REMOTE"):
    print(f"     {k:<22}= {env[k]}")
check("APPDATA 가 테스트 폴더 안이다",
      FIX.resolve() in Path(env["APPDATA"]).resolve().parents
      or Path(env["APPDATA"]).resolve() == (FIX / "_appdata").resolve())
check("BLOG_DATA_DIR 가 테스트 폴더다",
      Path(env["BLOG_DATA_DIR"]).resolve() == FIX.resolve())
check("★BLOG_QUEUE_BACKEND=local", env["BLOG_QUEUE_BACKEND"] == "local")
check("BLOG_ACCOUNTS_REMOTE=0", env["BLOG_ACCOUNTS_REMOTE"] == "0")
check("운영 인증정보 변수를 상속하지 않는다",
      not [k for k in DROP if k in env], str([k for k in DROP if k in env]))

head("② 운영 파일 접근 차단 — 테스트 APPDATA 쪽에는 그 파일이 없다")
if not TEST_APPDATA.exists():
    note("테스트 APPDATA 폴더가 아직 없다", "실행 시 pairing 이 만든다(빈 폴더)")
for name in ("device.json", "agent_config.json"):
    tp = TEST_APPDATA / "BlogLandingAgent" / name
    pp = PROD / name
    print(f"     {name:<20} 테스트={tp.exists()}  운영={pp.exists()}")
    check(f"  테스트 쪽에 {name} 이 없다(토큰 상속 없음)", not tp.exists())
check("운영 device.json 은 그대로 있다(읽지도 쓰지도 않는다)",
      (PROD / "device.json").exists())
check("운영 세션 폴더가 테스트 경로와 다르다",
      (PROD / "sessions").resolve() != (FIX / "sessions").resolve())

head("③ 큐 백엔드·토큰 주입 — Agent 와 **같은 순서**로 흉내 내 본다")
probe = r'''
import json, os, sys
sys.path.insert(0, r"%ROOT%")
out = {}
from v2 import pairing
out["pairing_device_file"] = str(pairing.DEVICE_FILE)
out["pairing_device_exists"] = pairing.DEVICE_FILE.exists()
out["pairing_config_file"] = str(pairing.CONFIG_FILE)
out["pairing_config_exists"] = pairing.CONFIG_FILE.exists()
dev = pairing.apply_env()                      # ★Agent 가 시작할 때 부르는 그것
out["device_keys"] = sorted(dev.keys())
tok = os.environ.get("BLOG_DEVICE_TOKEN") or ""
out["token_injected"] = bool(tok)
out["token_len"] = len(tok)                    # 값은 절대 담지 않는다
url = os.environ.get("SUPABASE_URL") or ""
out["supabase_url_injected"] = bool(url)
out["backend_env"] = os.environ.get("BLOG_QUEUE_BACKEND")
from v2 import queue_store
from v2.appdir import ROOT as DATA_ROOT
out["appdir_root"] = str(DATA_ROOT)
out["backend_resolved"] = queue_store.configured_backend()
out["store_class"] = type(queue_store.get_store()).__name__
out["agent_id_default"] = (dev.get("device_id") or queue_store.host_name())
out["agent_version"] = pairing.AGENT_VERSION
print("@@PROBE@@" + json.dumps(out, ensure_ascii=False))
'''.replace("%ROOT%", str(ROOT))

r = subprocess.run([sys.executable, "-c", probe], env=child_env(),
                   cwd=str(ROOT), capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=180)
data = {}
for line in (r.stdout or "").splitlines():
    if line.startswith("@@PROBE@@"):
        data = json.loads(line[len("@@PROBE@@"):])
if not data:
    print(NG + " 프로브 실패")
    print((r.stdout or "")[-800:])
    print((r.stderr or "")[-800:])
    fails.append("프로브 실행")
else:
    for k in ("pairing_device_file", "pairing_config_file", "appdir_root"):
        print(f"     {k:<22}= {data[k]}")
    print(f"     {'backend_resolved':<22}= {data['backend_resolved']}")
    print(f"     {'store_class':<22}= {data['store_class']}")
    print(f"     {'agent_version':<22}= {data['agent_version']}")
    print(f"     {'token_injected':<22}= {data['token_injected']} "
          f"(길이 {data['token_len']} · 값은 출력하지 않음)")
    check("★pairing 이 **테스트 APPDATA** 를 본다(운영 device.json 아님)",
          str(FIX) in data["pairing_device_file"],
          data["pairing_device_file"])
    check("  그 경로에 device.json 이 없다", not data["pairing_device_exists"])
    check("  그 경로에 agent_config.json 이 없다",
          not data["pairing_config_exists"])
    check("★운영 device_token 이 프로세스에 주입되지 않았다",
          data["token_injected"] is False, f"token_len={data['token_len']}")
    check("★운영 SUPABASE_URL 이 주입되지 않았다",
          data["supabase_url_injected"] is False)
    check("★큐 백엔드가 local 로 확정된다",
          data["backend_resolved"] == "local", data["backend_resolved"])
    check("★저장소가 LocalStore 다(Supabase 아님)",
          data["store_class"] == "LocalStore", data["store_class"])
    check("appdir.ROOT 가 테스트 폴더다",
          Path(data["appdir_root"]).resolve() == FIX.resolve(),
          data["appdir_root"])
    check("device 정보가 비어 있다(운영 계정 상속 없음)",
          not data["device_keys"], str(data["device_keys"]))
    note("기본 agent_id(플래그 없을 때)", data["agent_id_default"])

head("④ agent_id 고정")
check(f"--agent-id {AGENT_ID} 로 넘긴다", True, AGENT_ID)
check("  Agent 가 --agent-id 를 우선한다(소스 확인)",
      "agent_id = args.agent_id or dev.get(\"device_id\") or host_name()"
      in (ROOT / "v2" / "agent.py").read_text(encoding="utf-8"))

head("⑤ 테스트 큐가 비어 있는가")
counts = {}
for s in ("pending", "running", "held", "finished"):
    counts[s] = len(list((FIX / "queue" / s).glob("*.json")))
    print(f"     {s:<10}{counts[s]}건")
check("★pending·running 이 0건이다(집을 작업이 없다)",
      counts["pending"] == 0 and counts["running"] == 0, str(counts))
print("   → 작업이 없으므로 `--once` 는 **아무것도 실행하지 않고** 끝난다"
      "(Playwright·네이버 접속 없음).")

# ══════════════════════════════════════════════════════════════════
if not RUN:
    head("실행은 하지 않았다 — `--run` 을 주면 그때 1회 실행한다")
else:
    if fails:
        print("\n" + NG + " 정적 검증 실패가 있어 실행하지 않습니다.")
    else:
        head("⑥ 테스트 Agent 1회 실행 (`--once` · 작업 없음)")
        #  ★실행 **직전에 한 번 더** 센다 — 검사와 실행 사이에 화면에서 작업이
        #    들어올 수 있다. 하나라도 있으면 실행하지 않는다.
        again = {s: len(list((FIX / "queue" / s).glob("*.json")))
                 for s in ("pending", "running")}
        check("실행 직전 재확인 — pending·running 0건",
              again["pending"] == 0 and again["running"] == 0, str(again))
        if again["pending"] or again["running"]:
            print(NG + " 실행 직전에 작업이 생겼습니다 — **중단합니다.**")
            sys.exit(1)
        print(f"     PLAYWRIGHT_BROWSERS_PATH={FIX / '_no_browsers'} "
              f"(브라우저 실행 차단)")
        cmd = [sys.executable, "-m", "v2.agent", "--agent-id", AGENT_ID,
               "--once"]
        print("     " + " ".join(cmd))
        rr = subprocess.run(cmd, env=child_env(), cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
        print(f"     exit={rr.returncode}")
        for line in (rr.stdout or "").splitlines()[:20]:
            print("     | " + line)
        for line in (rr.stderr or "").splitlines()[:10]:
            print("     ! " + line)
        check("Agent 가 정상 종료했다(작업 없음)", rr.returncode == 0,
              f"exit={rr.returncode}")
        check("  '처리할 작업이 없습니다' 로 끝났다",
              "처리할 작업이 없습니다" in (rr.stdout or ""))
        check("  큐=LocalStore 로 떴다",
              "LocalStore" in (rr.stdout or ""), "시작 줄 확인")
        check(f"  agent_id 가 {AGENT_ID} 다",
              AGENT_ID in (rr.stdout or ""), "시작 줄 확인")
        hb = FIX / "queue" / "agents" / f"{AGENT_ID}.json"
        check("  하트비트를 테스트 폴더에 남겼다", hb.exists(), str(hb))
        if hb.exists():
            d = json.loads(hb.read_text(encoding="utf-8"))
            print(f"     하트비트: agent={d.get('agent')} "
                  f"version={d.get('version')} state={d.get('state')}")
            #  ★기대값을 박지 않는다 — `v2/pairing.py` 가 유일한 출처다.
            #    (부모는 v2 를 import 하지 않는다 — 격리를 깨지 않으려고 글자로 읽는다)
            import re as _re

            _m = _re.search(r'AGENT_VERSION\s*=\s*"([0-9.]+)"',
                            (ROOT / "v2" / "pairing.py").read_text(encoding="utf-8"))
            _want = _m.group(1) if _m else ""
            check(f"  하트비트 version 이 {_want}(소스 실행)이다",
                  d.get("version") == _want, str(d.get("version")))
        blob = (rr.stdout or "") + (rr.stderr or "")
        check("★출력에 토큰·비밀번호가 없다",
              not any(k in blob for k in ("device_token", "eyJ", "password",
                                          "PUBLISHABLE", "SERVICE_KEY")))
        check("★출력에 운영 Supabase 주소가 없다", "supabase.co" not in blob)

head("⑦ 실행 전후 운영 상태")
prod_after = fingerprint(PROD)
print(f"   운영 지문(전) : {prod_before}")
print(f"   운영 지문(후) : {prod_after}")
check("★운영 폴더가 그대로다(파일·큐·세션·토큰 무변경)",
      prod_before == prod_after)
try:
    ps = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BlogLandingAgent.exe"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=60).stdout or ""
    live = [ln for ln in ps.splitlines() if "BlogLandingAgent" in ln]
    print(f"   운영 Agent 프로세스: {live[0].split() if live else '없음'}")
    check("★운영 Agent 프로세스가 살아 있다(건드리지 않았다)", bool(live))
except Exception as exc:                                       # noqa: BLE001
    note("프로세스 확인 불가", str(exc)[:60])

print("\n" + "=" * 78)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" T0 " + ("정적 + 실행 검증 전부 통과" if RUN else "정적 검증 전부 통과"))
print("=" * 78)
sys.exit(1 if fails else 0)
