r"""Agent 1.3.0 빌드 출처·해시 기록 생성 → installer/BUILD_1.3.0.md

    .\.venv\Scripts\python.exe scripts\write_build_record.py

★읽기만 한다(빌드 산출물과 작업 트리를 해시로 기록). 아무것도 설치하지 않는다.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
WS = Path(os.getenv("BLOG_BUILD_WS") or (ROOT.parent / "agent_build_130"))
OUT = ROOT / "installer" / "BUILD_1.3.0.md"

NEW_EXE = WS / "installer" / "dist" / "BlogLandingAgent" / "BlogLandingAgent.exe"
NEW_SETUP = WS / "installer" / "out" / "BlogLandingAgentSetup.exe"
OLD_EXE = ROOT / "installer" / "rollback_125" / "BlogLandingAgent-1.2.5-20260917_1810.exe"
OLD_SETUP = (ROOT / "installer" / "rollback_125"
             / "BlogLandingAgentSetup-1.2.5-20260917_1810.exe")
INSTALLED = (Path(os.getenv("LOCALAPPDATA") or "") / "Programs"
             / "BlogLandingAgent" / "BlogLandingAgent.exe")


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def row(label: str, p: Path) -> str:
    if not p.exists():
        return f"| {label} | (없음) | — | — |"
    return (f"| {label} | `{p}` | {p.stat().st_size:,} | `{sha(p)}` |")


def git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return (r.stdout or "").strip()
    except Exception:                                          # noqa: BLE001
        return ""


def main() -> int:
    v2 = sorted((WS / "v2").glob("*.py"))
    lines: list[str] = []
    a = lines.append
    a("# Agent 1.3.0 빌드 기록")
    a("")
    a(f"작성 {datetime.now():%Y-%m-%d %H:%M:%S} · **로컬 빌드만 수행했고 설치하지 "
      "않았습니다.**")
    a("")
    a("## 1. 빌드 기준선")
    a("")
    a("| 항목 | 값 |")
    a("|---|---|")
    a(f"| 빌드 작업공간 | `{WS}` (프로젝트와 분리) |")
    a("| 기준선 | **1.2.5 기준선 + Stage 1~3** = 현재 작업 트리 |")
    a("| ★Git HEAD 로 빌드하지 않은 이유 | 1.2.5 는 2026-09-17 18:10 에 "
      "**작업 트리**에서 빌드됐고, 그 트리에만 있는 9월 안전장치"
      "(등록 blog_id 없으면 발행 중단 등)가 HEAD 에는 없다 |")
    a(f"| 로컬 git HEAD | `{git('rev-parse', '--short', 'HEAD')}` "
      f"{git('log', '-1', '--format=%s')} |")
    a(f"| 포함된 v2 모듈 | {len(v2)}개 (`*.bak-*`·`__pycache__` 제외) |")
    a("| 제외 확인 | accounts.json · brands.json · sessions/ · queue/ · out/ · "
      "streamlit_app.py · 구글 인증 파일 · `.streamlit/` |")
    a("| payload | `agent_config.json` (SUPABASE_URL + **publishable** 키만) |")
    a("| 비밀값 검사 | build.py ③단계 통과 — service key·개인키·쿠키·로컬설정 없음 |")
    a("")
    a("## 2. 산출물 해시 (SHA-256)")
    a("")
    a("| 구분 | 경로 | 크기(byte) | sha256 |")
    a("|---|---|---|---|")
    a(row("**1.3.0 설치파일**", NEW_SETUP))
    a(row("1.3.0 Agent exe", NEW_EXE))
    a(row("1.2.5 설치파일(롤백용)", OLD_SETUP))
    a(row("1.2.5 Agent exe(롤백용)", OLD_EXE))
    a(row("현재 설치돼 실행 중", INSTALLED))
    a("")
    if INSTALLED.exists() and OLD_EXE.exists():
        same = sha(INSTALLED) == sha(OLD_EXE)
        a(f"· 현재 실행 중인 Agent = 롤백용 1.2.5 와 "
          f"**{'동일' if same else '다름'}** "
          f"(빌드 {mtime(INSTALLED)})")
    a("")
    a("## 3. 포함된 v2 모듈")
    a("")
    a("```")
    for i in range(0, len(v2), 4):
        a("  " + "  ".join(f"{p.name:<22}" for p in v2[i:i + 4]).rstrip())
    a("```")
    a("")
    a("## 4. 1.2.5 → 1.3.0 코드 변경(이 빌드에 들어간 것)")
    a("")
    a("| 파일 | 단계 | 내용 |")
    a("|---|---|---|")
    a("| `v2/queue_store.py` | 1·2 | 멱등성(client_key)·strict 백엔드 · "
      "`held` 보류/재개(login_id 또는 account.id) |")
    a("| `v2/supabase_store.py` | 1·2 | `find_by_client_key` · `probe` · "
      "조건부 UPDATE 보류 |")
    a("| `v2/timeutil.py` | 1 | KST 표시(신규) |")
    a("| `v2/login_state.py` | 2 | 검증 등급·세션 별칭·로그아웃 대기·공유 판정(신규) |")
    a("| `v2/session.py` | 2 | `--logout` · `--confirm` · `--confirm-shared` · "
      "등급 기록 |")
    a("| `v2/session_store.py` | 2 | `logout()` · `browser_closed()` |")
    a("| `v2/browser.py` | 2 | `save_verdict` 4상태 · 어긋난 로그인 쿠키 제거 |")
    a("| `v2/writer.py` · `v2/edit_post.py` | 3 | 글 화면 진입 대기 2곳만 조건부 |")
    a("| `v2/fastwait.py` | 3 | 조건부 대기 헬퍼(신규, `BLOG_FAST_WAIT=0` 으로 끔) |")
    a("| `v2/pairing.py` · `installer/BlogLandingAgent.iss` | — | 1.2.5 → **1.3.0** |")
    a("")
    a("## 5. 속도 개선 기본값 — **기본 꺼짐(opt-in)**")
    a("")
    a("| `BLOG_FAST_WAIT` | 동작 |")
    a("|---|---|")
    a("| **설정 없음(기본)** | 기존 대기 방식 = 1.2.5 와 동일 |")
    a("| `0`·`off`·`false`·`no`·빈값·오타 | 기존 대기 방식 |")
    a("| `1`·`on`·`true`·`yes` | 속도 개선 활성 |")
    a("")
    a("· 2026-11-03 변경: 사용자 PC 에서 설정을 **빠뜨리면 안전한 쪽**"
      "(기존 대기 방식)이 되도록 기본값을 뒤집었습니다.")
    a("· 설치 프로그램은 `.env` 를 만들지도 덮지도 않습니다"
      "(payload 는 `agent_config.json` 뿐이고 `onlyifdoesntexist`).")
    a("· **켤 때만** 데이터 폴더의 `.env` 에 `BLOG_FAST_WAIT=1` 을 넣습니다. "
      "지우면 다시 꺼집니다(재설치 불필요).")
    a("")
    a("## 6. 1.2.5 로 되돌리기")
    a("")
    a("1. 트레이 아이콘 → 종료")
    a(f"2. `{OLD_SETUP.name}` 실행 → 덮어 설치(AppId 동일 → 제자리 업그레이드)")
    a("3. `%APPDATA%\\BlogLandingAgent` 는 건드리지 않음 → **세션·계정·설정 그대로**")
    a("4. 화면의 [선택 계정 로그아웃] 버튼은 버전 게이트로 **자동 비활성**")
    a("")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"기록 작성: {OUT}")
    for p in (NEW_SETUP, NEW_EXE):
        if p.exists():
            print(f"  {p.name:<28} {p.stat().st_size:>12,} byte  {sha(p)[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
