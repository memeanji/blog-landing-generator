r"""Agent 1.3.0 **별도 빌드 작업공간** 만들기 — 필요한 파일만 옮기고 기준선을 검증한다.

    .\.venv\Scripts\python.exe scripts\make_build_workspace.py

왜 별도 공간인가
    `installer/agent.spec` 은 `pathex=[ROOT]` + `collect_submodules("v2")` 로
    **그 폴더의 코드를 그대로** 싸 넣는다. 프로젝트 폴더에서 바로 빌드하면
    `*.bak-*`·`out/`·`sessions/`·`queue/` 같은 것이 같은 트리에 있어 혼입 여지가 있고,
    기존 1.2.5 산출물(`installer/dist`·`installer/out`)을 덮어쓴다.

기준선 (★중요)
    1.2.5 는 2026-09-17 18:10 에 **그 시점 작업 트리**에서 빌드됐다.
    그 이후 바뀐 코드 파일은 **2026-11-03 작업(Stage 1~3)뿐**임을 확인했다.
    따라서  1.3.0 기준선 = 1.2.5 기준선 + Stage 1~3  =  현재 작업 트리  다.
    → Git HEAD 로 빌드하면 9월 미커밋 안전장치가 빠진다. 그래서 **작업 트리**를 쓴다.

비밀값
    `.env` 는 `write_payload()` 가 **SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY** 두 개만
    읽는다. 그 두 줄만 옮긴다(서비스키·구글 인증 경로·시트 ID 는 옮기지 않는다).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
WS = Path(os.getenv("BLOG_BUILD_WS") or (ROOT.parent / "agent_build_130"))

#  빌드에 **실제로 필요한 것만**
COPY_FILES = ["agent_tray.py",
              "installer/agent.spec", "installer/build.py",
              "installer/BlogLandingAgent.iss"]
#  9월 미커밋 안전장치가 들어 있는 파일(기준선 보존 확인용)
SEPT_FILES = ["agent_tray.py", "v2/config.py", "v2/run.py", "v2/run_production.py",
              "v2/sheets.py", "v2/source_view.py", "v2/landing_sheet.py",
              "v2/gauth.py", "v2/shared_cred.py", "v2/writer.py", "v2/edit_post.py"]
#  11/03 Stage 1~3 에서 바뀐/추가된 파일
STAGE_FILES = ["v2/queue_store.py", "v2/supabase_store.py", "v2/session.py",
               "v2/session_store.py", "v2/browser.py", "v2/pairing.py",
               "v2/login_state.py", "v2/fastwait.py", "v2/timeutil.py",
               "v2/writer.py", "v2/edit_post.py", "installer/BlogLandingAgent.iss"]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    print("=" * 78)
    print(" Agent 1.3.0 별도 빌드 작업공간 준비")
    print("=" * 78)
    if WS.exists():
        shutil.rmtree(WS)
    (WS / "v2").mkdir(parents=True)
    (WS / "installer").mkdir(parents=True)

    # ── v2 패키지: .py 만(백업·캐시 제외) ──────────────────────────
    n = 0
    for src in sorted((ROOT / "v2").glob("*.py")):
        shutil.copy2(src, WS / "v2" / src.name)
        n += 1
    print(f"  v2/*.py            {n}개 복사 (★.bak-* · __pycache__ 는 제외)")

    for rel in COPY_FILES:
        dst = WS / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    print(f"  빌드 파일           {len(COPY_FILES)}개 복사")

    # ── .env: 필요한 두 줄만 ──────────────────────────────────────
    from dotenv import dotenv_values
    env = dotenv_values(ROOT / ".env")
    url = (env.get("SUPABASE_URL") or "").strip()
    key = (env.get("SUPABASE_PUBLISHABLE_KEY")
           or env.get("SUPABASE_ANON_KEY") or "").strip()
    if not url or not key:
        print("  ❌ .env 에 SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY 가 없습니다.")
        return 2
    if key.startswith("sb_secret_") or "service_role" in key:
        print("  ❌ 비밀키가 들어올 뻔했습니다 — 중단합니다.")
        return 2
    (WS / ".env").write_text(
        f"SUPABASE_URL={url}\nSUPABASE_PUBLISHABLE_KEY={key}\n", encoding="utf-8")
    print(f"  .env               2줄만 복사 (publishable {len(key)}자 · 서비스키 없음)")

    # ── 혼입 금지 목록 확인 ───────────────────────────────────────
    print("")
    print("  혼입 금지 항목 확인")
    for bad in ("accounts.json", "brands.json", "sessions", "queue", "out",
                "streamlit_app.py", "google-service-account.json", "scripts",
                ".streamlit", "playwright-profile"):
        print(f"    {bad:<34} {'★있음!' if (WS / bad).exists() else '없음'}")
    leftovers = [p.name for p in WS.rglob("*.bak*")]
    print(f"    *.bak* 파일                        "
          f"{'★' + str(len(leftovers)) + '개' if leftovers else '없음'}")

    # ── 기준선 검증: 복사본이 작업 트리와 바이트 동일한가 ──────────
    print("")
    print("  기준선 검증 — 작업 트리와 동일한가(sha256)")
    bad = 0
    for rel in SEPT_FILES + STAGE_FILES:
        a, b = ROOT / rel, WS / rel
        if not b.exists():
            print(f"    ❌ {rel} — 작업공간에 없음")
            bad += 1
            continue
        if sha(a) != sha(b):
            print(f"    ❌ {rel} — 내용 다름")
            bad += 1
    print(f"    9월 안전장치 파일 {len(SEPT_FILES)}개 + Stage 파일 "
          f"{len(STAGE_FILES)}개 — {'전부 동일 ✅' if bad == 0 else str(bad) + '개 불일치'}")

    # ── 9월 안전장치가 살아 있는지 내용으로 확인 ──────────────────
    print("")
    print("  9월 운영 필수 코드 보존 확인(내용)")
    checks = [
        ("v2/run.py", "blog_id 가 등록돼 있지 않아", "등록 blog_id 없으면 발행 중단"),
        ("v2/run_production.py", "blog_id 가 등록돼 있지 않아", "등록 blog_id 없으면 발행 중단"),
        ("v2/accounts.py", "accounts_list", "Supabase 중앙 계정관리"),
        ("v2/accounts.py", "find_by_identity", "login_id 기준 계정선택"),
        ("v2/accounts.py", "세션 폴더 이름으로 계정을 추측하지 않습니다",
         "세션 폴더로 계정 추측 금지"),
        ("v2/accounts.py", "accounts_cache.json", "계정 목록 캐시 폴백"),
        ("v2/config.py", "gauth", "서버 발급 토큰 인증 경로"),
        ("v2/supabase_store.py", "accounts_list", "accounts_list RPC"),
        ("v2/batch_plan.py", "BATCH_SIZE = 10", "10건 배치"),
    ]
    for rel, needle, label in checks:
        t = (WS / rel).read_text(encoding="utf-8", errors="replace")
        print(f"    {label:<32} {'있음' if needle in t else '★없음'}")

    print("")
    print(f"  작업공간: {WS}")
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
