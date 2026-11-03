r"""Agent 1.3.0 설치본 최종 검증 — **읽기 전용**. 설치·실행하지 않는다.

    .\.venv\Scripts\python.exe scripts\verify_build_130.py

빌드된 EXE 안의 동결 바이트코드를 풀어 **실제로 들어간 것**을 확인한다.
  ① AGENT_VERSION = 1.3.0
  ② `--logout` 지원 · 기존 `--login/--check/--clear/--adopt` 유지
  ③ 1.2.5 의 blog_id 검증 유지
  ④ 세션 저장 경로 호환
  ⑤ 속도 개선 기본값(opt-in)
  ⑥ 1.2.5 롤백 파일 보존
  ⑦ 설치 안전성(AppId 동일 · %APPDATA% 보존)
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import zlib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
WS = Path(os.getenv("BLOG_BUILD_WS") or (ROOT.parent / "agent_build_130"))
NEW_EXE = WS / "installer" / "dist" / "BlogLandingAgent" / "BlogLandingAgent.exe"
NEW_SETUP = WS / "installer" / "out" / "BlogLandingAgentSetup.exe"
NEW_ISS = WS / "installer" / "BlogLandingAgent.iss"
RB = ROOT / "installer" / "rollback_125"
OLD_EXE = RB / "BlogLandingAgent-1.2.5-20260917_1810.exe"
OLD_SETUP = RB / "BlogLandingAgentSetup-1.2.5-20260917_1810.exe"
INSTALLED = (Path(os.getenv("LOCALAPPDATA") or "") / "Programs"
             / "BlogLandingAgent" / "BlogLandingAgent.exe")

OK, NG = "  [o]", "  [X]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def head(t: str) -> None:
    print("")
    print("=" * 74)
    print(f" {t}")
    print("=" * 74)


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def scan(p: Path, needles: list[str]) -> dict[str, bool]:
    """EXE 안 동결 모듈을 풀어 문자열이 들어 있는지 본다."""
    data = p.read_bytes()
    found = {n: False for n in needles}
    for m in re.finditer(rb"\x78\x9c", data):
        try:
            out = zlib.decompressobj().decompress(data[m.start():m.start() + 400_000])
        except Exception:                                      # noqa: BLE001
            continue
        for n in needles:
            if not found[n] and n.encode("utf-8") in out:
                found[n] = True
    return found


def version_of(p: Path) -> list[str]:
    """AGENT_VERSION 이 들어 있는 모듈만 풀어 버전 문자열을 뽑는다."""
    data = p.read_bytes()
    hits: set[str] = set()
    for m in re.finditer(rb"\x78\x9c", data):
        try:
            out = zlib.decompressobj().decompress(data[m.start():m.start() + 200_000])
        except Exception:                                      # noqa: BLE001
            continue
        if b"AGENT_VERSION" in out:
            hits |= {v.group().decode() for v in re.finditer(rb"1\.\d\.\d", out)}
    return sorted(hits)


if not NEW_EXE.exists() or not NEW_SETUP.exists():
    print(f"  빌드 산출물이 없습니다: {NEW_EXE}")
    raise SystemExit(2)

head("1. AGENT_VERSION")
v = version_of(NEW_EXE)
check("빌드된 EXE 의 AGENT_VERSION = 1.3.0", v == ["1.3.0"], str(v))
check("설치 프로그램 AppVer = 1.3.0",
      '#define AppVer "1.3.0"' in NEW_ISS.read_text(encoding="utf-8-sig"))
check("롤백본은 1.2.5 그대로", version_of(OLD_EXE) == ["1.2.5"],
      str(version_of(OLD_EXE)))

head("2. 세션 명령 — 신규 추가 + 기존 유지")
cmds = ["--logout", "--confirm", "--confirm-shared", "--hard",
        "--login", "--check", "--clear", "--adopt", "--relogin",
        "--from-profile", "--overwrite", "--ref-tab", "--events"]
f = scan(NEW_EXE, cmds)
for c in ("--logout", "--confirm", "--confirm-shared", "--hard"):
    check(f"신규 `{c}` 지원", f[c])
for c in ("--login", "--check", "--clear", "--adopt", "--relogin",
          "--from-profile", "--overwrite", "--ref-tab", "--events"):
    check(f"기존 `{c}` 유지", f[c])
old_f = scan(OLD_EXE, ["--logout", "--login", "--check", "--clear", "--adopt"])
check("1.2.5 에는 `--logout` 이 없었다(차이 확인)",
      not old_f["--logout"] and old_f["--login"] and old_f["--adopt"])
check("`v2.session` 이 실행 허용 모듈이다(설치본에서 CLI 로 부를 수 있다)",
      "v2.session" in (ROOT / "agent_tray.py").read_text(encoding="utf-8"))

head("3. 1.2.5 의 blog_id 검증·계정 선택 유지")
keep = ["blog_id 가 등록돼 있지 않아", "세션 폴더 이름으로 계정을 추측하지 않습니다",
        "find_by_identity", "find_by_tab", "accounts_list", "accounts_cache.json",
        "set_blog_id", "resolve_for", "claim_job", "finish_job", "agent_heartbeat",
        "BATCH_SIZE"]
f = scan(NEW_EXE, keep)
labels = {
    "blog_id 가 등록돼 있지 않아": "등록 blog_id 없으면 발행 중단(9월 안전장치)",
    "세션 폴더 이름으로 계정을 추측하지 않습니다": "세션 폴더로 계정 추측 금지",
    "find_by_identity": "login_id 기준 계정선택",
    "find_by_tab": "brand+ref_tab 보조선택",
    "accounts_list": "Supabase 중앙 계정관리 RPC",
    "accounts_cache.json": "계정 목록 캐시 폴백",
    "set_blog_id": "첫 확인 blog_id 기록",
    "resolve_for": "계정 해석 공용 경로",
    "claim_job": "큐 claim", "finish_job": "큐 finish",
    "agent_heartbeat": "하트비트", "BATCH_SIZE": "10건 배치",
}
for k, label in labels.items():
    check(label, f[k])

head("4. 세션 저장 경로 호환")
paths = ["sessions", "state.json", "meta.json", "profile", "_prime_session",
         "_persist_session", "load_cookies", "save_state", "clear_state",
         "adopt_legacy_profile", "BlogLandingAgent", "APPDATA", "BLOG_DATA_DIR"]
f = scan(NEW_EXE, paths)
for k in paths:
    check(f"`{k}`", f[k])
src = (ROOT / "v2" / "session_store.py").read_text(encoding="utf-8")
check("세션 폴더 규칙이 그대로다 — sessions/<account.id>/",
      'SESSIONS_ROOT = ROOT / "sessions"' in src
      and "return SESSIONS_ROOT / ident" in src)
check("로그아웃 기본은 프로필 보존(통째 삭제 아님)",
      '"profile_kept": True' in src and "HARD_TARGETS" in src)

head("5. 속도 개선 기본값 — opt-in(기본 꺼짐)")
fw = (ROOT / "v2" / "fastwait.py").read_text(encoding="utf-8")
check("코드가 '설정 없으면 꺼짐' 이다",
      'return (os.getenv("BLOG_FAST_WAIT") or "").strip().lower() in ON' in fw)
check("켜는 값 목록이 1/on/true/yes 다", 'ON = ("1", "on", "true", "yes", "y")' in fw)
sys.path.insert(0, str(ROOT))
os.environ.pop("BLOG_FAST_WAIT", None)
from v2 import fastwait                                        # noqa: E402
check("설정 없음 → 꺼짐", not fastwait.enabled())
os.environ["BLOG_FAST_WAIT"] = "0"
check("=0 → 꺼짐", not fastwait.enabled())
os.environ["BLOG_FAST_WAIT"] = "1"
check("=1 → 켜짐", fastwait.enabled())
os.environ.pop("BLOG_FAST_WAIT", None)
f = scan(NEW_EXE, ["BLOG_FAST_WAIT", "fastwait"])
check("EXE 안에 스위치가 들어 있다", f["BLOG_FAST_WAIT"] and f["fastwait"])
check("조건부 대기는 진입 2곳뿐이다",
      (ROOT / "v2" / "writer.py").read_text(encoding="utf-8").count(
          "fastwait.settle(") == 1
      and (ROOT / "v2" / "edit_post.py").read_text(encoding="utf-8").count(
          "fastwait.settle(") == 1)

head("6. 1.2.5 롤백 파일 보존")
check("롤백 설치파일 있음", OLD_SETUP.exists(),
      f"{OLD_SETUP.stat().st_size:,} byte" if OLD_SETUP.exists() else "")
check("롤백 Agent exe 있음", OLD_EXE.exists())
check("SHA256SUMS 기록 있음", (RB / "SHA256SUMS-1.2.5.txt").exists())
if INSTALLED.exists():
    check("지금 설치돼 실행 중인 것 = 롤백본 1.2.5 (교체하지 않았다)",
          sha(INSTALLED) == sha(OLD_EXE), sha(INSTALLED)[:16] + "…")

head("7. 설치 안전성 (정적 확인)")
new_iss = NEW_ISS.read_text(encoding="utf-8-sig")
old_iss_app = re.search(r"AppId=\{\{([^}]+)\}", new_iss)
check("AppId 가 1.2.5 와 동일 → 제자리 업그레이드",
      "8E2C1B84-3F5A-4C1D-9E77-BLOGLANDING01" in new_iss,
      old_iss_app.group(1) if old_iss_app else "")
check("설치 대상은 프로그램 폴더뿐(DestDir={app})",
      'Source: "..\\installer\\dist\\BlogLandingAgent\\*"; DestDir: "{app}"' in new_iss)
check("%APPDATA% 에는 agent_config.json 만 · onlyifdoesntexist(기존 설정 보존)",
      "DestDir: \"{userappdata}\\BlogLandingAgent\"" in new_iss
      and "onlyifdoesntexist" in new_iss)
check("설치 프로그램이 sessions/ 를 건드리지 않는다",
      "sessions" not in new_iss)
check("설치 프로그램이 .env 를 만들거나 덮지 않는다", ".env" not in new_iss)
check("제거할 때만 Agent 를 종료한다([UninstallRun])",
      "[UninstallRun]" in new_iss and new_iss.count("taskkill") == 1)
check("관리자 권한 불필요(PrivilegesRequired=lowest)",
      "PrivilegesRequired=lowest" in new_iss)

head("8. 산출물 해시 (SHA-256)")
for label, p in (("1.3.0 설치파일", NEW_SETUP), ("1.3.0 Agent exe", NEW_EXE),
                 ("1.2.5 설치파일(롤백)", OLD_SETUP),
                 ("1.2.5 Agent exe(롤백)", OLD_EXE)):
    if p.exists():
        print(f"  {label:<22} {p.stat().st_size:>12,} byte  {sha(p)}")

print("")
print("=" * 74)
print(f" 실패 {len(fails)}건: {fails}" if fails else " 전부 통과")
print("=" * 74)
raise SystemExit(1 if fails else 0)
