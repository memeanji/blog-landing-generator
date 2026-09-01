r"""설치파일 빌드 — PyInstaller → 접속정보 payload → Inno Setup.

    .\.venv\Scripts\python.exe installer\build.py            # 전체
    .\.venv\Scripts\python.exe installer\build.py --skip-exe # 설치파일만 다시

★비밀값은 넣지 않는다. payload 에 들어가는 건 공개해도 되는 publishable 키뿐이며,
  service key 나 구글 인증 파일은 절대 포함하지 않는다(검사도 한다).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "installer"
DIST = HERE / "dist" / "BlogLandingAgent"
PAYLOAD = HERE / "payload"
OUT = HERE / "out"
ISCC_CANDIDATES = [
    Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def build_exe() -> None:
    say("① PyInstaller — Agent 실행파일 빌드")
    r = subprocess.run([sys.executable, "-m", "PyInstaller", str(HERE / "agent.spec"),
                        "--noconfirm", "--distpath", str(HERE / "dist"),
                        "--workpath", str(HERE / "build")],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not (DIST / "BlogLandingAgent.exe").exists():
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit("PyInstaller 빌드 실패")
    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    say(f"   완료 — {size / 1024 / 1024:.0f} MB")


def write_payload() -> None:
    """설치 시 사용자 폴더에 놓일 접속 정보(비밀 아님)."""
    say("② 접속 정보 payload 준비")
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    url = (env.get("SUPABASE_URL") or "").strip()
    key = (env.get("SUPABASE_PUBLISHABLE_KEY") or env.get("SUPABASE_ANON_KEY") or "").strip()
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY 가 .env 에 없습니다.")
    if key.startswith("sb_secret_") or "service_role" in key:
        raise SystemExit("★비밀키가 들어갈 뻔했습니다 — publishable 키를 넣으세요.")
    PAYLOAD.mkdir(parents=True, exist_ok=True)
    (PAYLOAD / "agent_config.json").write_text(json.dumps(
        {"supabase_url": url, "supabase_publishable_key": key,
         "_note": "공개돼도 되는 값만 들어갑니다. 구글 인증 파일은 트레이 메뉴에서 지정하세요."},
        ensure_ascii=False, indent=1), encoding="utf-8")
    say(f"   완료 — publishable 키({len(key)}자) · URL 설정")


def guard_secrets() -> None:
    """설치 대상에 비밀값이 섞이지 않았는지 검사."""
    say("③ 비밀값 혼입 검사")
    bad = []
    marks = ("sb_secret_", "service_role", "-----BEGIN PRIVATE KEY-----",
             "NID_AUT", "NID_SES")
    for f in list(DIST.rglob("*.json")) + list(PAYLOAD.rglob("*")):
        if not f.is_file() or f.stat().st_size > 3_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:                                      # noqa: BLE001
            continue
        for m in marks:
            if m in text:
                bad.append(f"{f.relative_to(HERE)} — {m}")
    for name in ("brands.json", "accounts.json", ".env", "device.json",
                 "google-service-account.json"):
        for f in DIST.rglob(name):
            bad.append(f"{f.relative_to(HERE)} — 로컬 설정 파일")
    if bad:
        for b in bad:
            print(f"     ★ {b}")
        raise SystemExit("비밀값/로컬 설정이 설치본에 섞였습니다 — 중단합니다.")
    say("   깨끗함 (service key · 개인키 · 쿠키 · 로컬 설정 없음)")


def build_installer() -> Path:
    say("④ Inno Setup — 설치파일 생성")
    iscc = next((p for p in ISCC_CANDIDATES if p.exists()), None)
    if not iscc:
        raise SystemExit("ISCC.exe 를 찾지 못했습니다(Inno Setup 6 설치 필요).")
    OUT.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(iscc), str(HERE / "BlogLandingAgent.iss")],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    setup = OUT / "BlogLandingAgentSetup.exe"
    if r.returncode != 0 or not setup.exists():
        print(r.stdout[-2000:])
        print(r.stderr[-1500:])
        raise SystemExit("Inno Setup 실패")
    say(f"   완료 — {setup} ({setup.stat().st_size / 1024 / 1024:.0f} MB)")
    return setup


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("블로그 랜딩 Agent 설치파일 빌드")
    if "--skip-exe" not in sys.argv:
        shutil.rmtree(HERE / "dist", ignore_errors=True)
        build_exe()
    write_payload()
    guard_secrets()
    setup = build_installer()
    print()
    print(f"설치파일: {setup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
