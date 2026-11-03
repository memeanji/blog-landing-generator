r"""A/B/C 환경별 구글 인증 검증 — 브라우저 없이, 쓰기 없이.

    A. 개발 PC       — 기존 로컬 서비스계정 JSON (회귀 없음)
    B. 표님 PC 재현  — device.json 만 있고 구글 JSON 없음 → 서버 토큰으로 동작
    C. 완전 신규 PC  — 아무것도 없음 → **6자리 페어링부터** 시작해 시트 조회까지

B·C 는 Supabase 홉만 `scripts/fake_supabase.py` 대역으로 바꾼다(계약 동일).
구글 토큰과 시트 접근은 **실제 통신**이다.

    .\.venv\Scripts\python.exe scripts\test_gauth_envs.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PY = sys.executable
PORT = 8799
CODE = "482100"
MEDIA, DEFICIENCY = "구글애즈", "팔자 / 머니"

ok_count, fail_count = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        fail_count += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def clean_env(home: Path, *, keep_google: bool = False) -> dict:
    """새 PC 처럼 보이는 환경변수 묶음.

    ★APPDATA 를 옮기므로 **윈도우 사용자명이 달라도 되는지**까지 함께 검증된다.
      (device.json / agent_config.json 은 APPDATA 기준으로 찾는다)
    """
    env = dict(os.environ)
    for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT",
              "BLOG_DEVICE_TOKEN", "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY",
              "SUPABASE_ANON_KEY", "BLOG_QUEUE_BACKEND", "REFERENCE_SPREADSHEET_ID"):
        env.pop(k, None)
    if not keep_google:
        env["GOOGLE_SERVICE_ACCOUNT_JSON"] = ""
    # ★pairing 은 %APPDATA%\BlogLandingAgent 를 보고, appdir 은 BLOG_DATA_DIR 을 본다
    #   → 둘이 **같은 폴더**를 가리키게 맞춘다(실제 설치본과 동일한 배치).
    env["APPDATA"] = str(home.parent)
    env["BLOG_DATA_DIR"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    # ★화면(Streamlit)은 작업마다 브랜드 설정을 이렇게 실어 보낸다 — 새 PC 에는
    #   brands.json 이 없으므로 실제 운영에서도 이 경로로만 시트 ID 가 전달된다.
    import v2.brands as _brands
    env["BLOG_BRANDS_JSON"] = _brands.config_json()
    return env


def agent_config(home: Path) -> None:
    """설치본이 심어 주는 접속 정보(비밀 아님) — 대역 서버를 가리키게 한다."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "agent_config.json").write_text(json.dumps({
        "supabase_url": f"http://127.0.0.1:{PORT}",
        "supabase_publishable_key": "sb_publishable_fake_for_test",
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def run_dry(env: dict, cwd: Path = ROOT) -> tuple[int, str]:
    r = subprocess.run(
        [PY, "-m", "v2.run", "--media", MEDIA, "--deficiency", DEFICIENCY,
         "--kind", "검수용", "--count", "1", "--dry-run"],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def no_secret_leak(text: str, *needles: str) -> bool:
    return not any(n and n in text for n in needles)


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    import fake_supabase

    fake_supabase.serve(PORT, CODE)
    tmp = Path(tempfile.mkdtemp(prefix="gauth_test_"))
    print(f"임시 환경: {tmp}\n")

    # ── A. 개발 PC ─────────────────────────────────────────────────
    print("A. 개발 PC — 기존 로컬 서비스계정 JSON")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    code, out = run_dry(env)
    check("dry-run 성공", code == 0, f"exit={code}")
    check("기준시트 조회", "매칭 (시트" in out)
    check("참고 랜딩 URL", "[dry-run] 참고 랜딩 = https://" in out)
    check("로컬 파일 경로로 인증", "서버 발급 토큰" not in out)
    if code != 0:
        print(out[-1200:])

    # ── B. 표님 PC 재현 ────────────────────────────────────────────
    print("\nB. 표님 PC 재현 — device.json 있음 / 구글 JSON 없음")
    home_b = tmp / "pyo" / "BlogLandingAgent"
    agent_config(home_b)
    # 이미 페어링된 상태를 만든다(재페어링 없이 기존 토큰 그대로 쓰는 상황)
    import hashlib
    import secrets as _s
    tok = _s.token_hex(32)
    fake_supabase.STATE["devices"][hashlib.sha256(tok.encode()).hexdigest()] = {
        "device_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "label": "표님", "revoked": False}
    (home_b / "device.json").write_text(json.dumps({
        "device_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "device_token": tok,
        "label": "표님", "version": "1.1.5", "mode": "supabase"}), encoding="utf-8")
    before = (home_b / "device.json").read_text(encoding="utf-8")

    env_b = clean_env(home_b)
    code, out = run_dry(env_b)
    check("dry-run 성공", code == 0, f"exit={code}")
    check("기준시트 조회", "매칭 (시트" in out)
    check("구글 JSON 파일 없이 동작", not (home_b / "google-service-account.json").exists())
    check("서비스계정 원문 캐시 안 생김",
          not list(home_b.glob("sa_cache*")) and not list(home_b.glob("*service-account*")))
    check("device.json 변경 없음", (home_b / "device.json").read_text(encoding="utf-8") == before)
    check("로그에 토큰 원문 없음", no_secret_leak(out, tok))
    if code != 0:
        print(out[-1200:])

    # ── C. 완전 신규 PC ────────────────────────────────────────────
    print("\nC. 완전 신규 PC — 6자리 페어링부터")
    home_c = tmp / "newpc" / "BlogLandingAgent"
    agent_config(home_c)
    env_c = clean_env(home_c)
    check(".env 없음", not (home_c / ".env").exists())
    check("구글 JSON 없음", not (home_c / "google-service-account.json").exists())
    check("accounts.json 없음", not (home_c / "accounts.json").exists())
    check("device.json 없음(페어링 전)", not (home_c / "device.json").exists())

    # 인증이 아예 없을 때 ④ 안내가 나오는지
    code, out = run_dry(env_c)
    check("페어링 전에는 안내 오류", code != 0 and "6자리 코드" in out, f"exit={code}")

    # ★실제 페어링 경로 — v2.pairing CLI 를 그대로 돌린다
    r = subprocess.run([PY, "-m", "v2.pairing", "--code", CODE],
                       cwd=str(ROOT), env=env_c, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    pair_out = (r.stdout or "") + (r.stderr or "")
    check("6자리 페어링 성공", r.returncode == 0, pair_out.strip().splitlines()[-1][:80]
          if pair_out.strip() else "")
    check("device.json 생성됨", (home_c / "device.json").exists())
    dev = json.loads((home_c / "device.json").read_text(encoding="utf-8")) \
        if (home_c / "device.json").exists() else {}
    check("페어링 출력에 토큰 원문 없음", no_secret_leak(pair_out, dev.get("device_token", "")))

    code, out = run_dry(env_c)
    check("페어링 후 dry-run 성공", code == 0, f"exit={code}")
    check("기준시트 조회", "매칭 (시트" in out)
    check("참고 랜딩 URL", "[dry-run] 참고 랜딩 = https://" in out)
    check("구글 JSON 파일 여전히 없음", not (home_c / "google-service-account.json").exists())
    check("access token 디스크 저장 없음",
          not any(p.name.endswith(".json") and "token" in p.name.lower()
                  for p in home_c.iterdir()))
    check("로그에 device_token 원문 없음", no_secret_leak(out, dev.get("device_token", "")))
    if code != 0:
        print(out[-1500:])

    # ── 쓰기 작업은 write 토큰을 따로 받는가 ───────────────────────
    print("\nD. 작업 단위 scope 분리 (신규 PC 환경)")
    ops_read = [o for o, _ in fake_supabase.STATE["mint_calls"]]
    check("조회 단계는 readonly 로만 요청", ops_read and set(ops_read) == {"readonly"},
          f"{ops_read}")
    # ★시트에 쓰지는 않는다 — UTM 빌더를 **열기만** 해서 어떤 권한을 요청하는지 본다.
    probe = ROOT / "scripts" / "_probe_write_scope.py"
    probe.write_text(
        "import sys, os\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import gspread\n"
        "import v2.brands as B, v2.landing_sheet as L, v2.gauth as G\n"
        "b = B.default_brand(); L.set_brand(b)\n"
        "# landing_sheet 가 쓰기용으로 고르는 것과 **같은 경로**로 자격증명을 얻는다\n"
        "creds = G.credentials(G.OP_READONLY if False else G.OP_WRITE)\n"
        "book = gspread.authorize(creds).open_by_key(L.active_sheet_id())\n"
        "print('WRITE_OPEN_OK', book.title, len(book.worksheets()))\n", encoding="utf-8")
    r = subprocess.run([PY, str(probe)], cwd=str(ROOT), env=env_c, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    probe.unlink(missing_ok=True)
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    check("쓰기 작업에서 UTM 빌더 열림", "WRITE_OPEN_OK" in (r.stdout or ""),
          tail[-1][:90] if tail else "")
    ops_all = [o for o, _ in fake_supabase.STATE["mint_calls"]]
    check("write 토큰을 따로 요청", "write" in ops_all, f"{ops_all}")
    check("readonly/write 캐시가 따로 보관됨",
          len({k.split("|")[1] for k in fake_supabase.STATE["cache"]}) == 2,
          f"{[k.split('|')[1].rsplit('/', 1)[-1] for k in fake_supabase.STATE['cache']]}")

    # ── 서버 측 동작 확인 ──────────────────────────────────────────
    print("\nE. 서버 측 계약")
    scopes = {s for _, s in fake_supabase.STATE["mint_calls"]}
    check("발급된 scope 는 allowlist 둘뿐", scopes <= {
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/spreadsheets"},
        f"{sorted(x.rsplit('/', 1)[-1] for x in scopes)}")

    # allowlist 밖 scope 는 거부되는가
    import requests
    r = requests.post(f"http://127.0.0.1:{PORT}/functions/v1/google-token",
                      json={"device_token": dev.get("device_token", ""),
                            "operation": "https://www.googleapis.com/auth/drive"},
                      timeout=20)
    check("임의 scope 요청 거부", r.status_code == 400, f"HTTP {r.status_code}")

    # 캐시가 통째로 사라져도(=Edge Function 재시작) 다음 요청이 정상 발급되는가
    before_mints = len(fake_supabase.STATE["mint_calls"])
    fake_supabase.STATE["cache"].clear()
    r = requests.post(f"http://127.0.0.1:{PORT}/functions/v1/google-token",
                      json={"device_token": dev.get("device_token", ""),
                            "operation": "readonly"}, timeout=30)
    check("캐시 소멸(재시작) 후에도 발급됨", r.status_code == 200, f"HTTP {r.status_code}")
    check("구글에서 새로 발급했음",
          len(fake_supabase.STATE["mint_calls"]) == before_mints + 1)
    check("응답에 토큰 원문 외 비밀 없음",
          "private_key" not in r.text and "BEGIN PRIVATE KEY" not in r.text)

    # 페어링 해제 즉시 차단
    for h, d in fake_supabase.STATE["devices"].items():
        d["revoked"] = True
    r = requests.post(f"http://127.0.0.1:{PORT}/functions/v1/google-token",
                      json={"device_token": dev.get("device_token", ""),
                            "operation": "readonly"}, timeout=20)
    check("연결 해제 후 발급 차단", r.status_code == 401, f"HTTP {r.status_code}")

    print(f"\n{'=' * 60}\n통과 {ok_count} · 실패 {fail_count}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass
    raise SystemExit(main())
