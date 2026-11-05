r"""Stage 13 검증 — Chromium 수동 로그아웃 (2026-11-05)

    .\.venv\Scripts\python.exe scripts\test_stage13_manual_logout.py

★브라우저·네이버·시트·운영 큐·운영 DB·운영 계정/세션을 건드리지 않는다.
  임시 폴더의 LocalStore 와 가짜 컨텍스트만 쓴다. 실제 로그인·로그아웃 없음.

운영 방식(2026-11-05 사용자 설명) — **순차**다. 한 계정의 발행을 모두 끝내고
로그아웃한 뒤 다음 네이버 계정으로 로그인한다. 여러 PC 동시 발행이 아니다.
그래서 DB 잠금 같은 동시성 장치를 더하지 않는다.

검증 항목
  ① 기본이 **수동**이고 `--auto` 로 예전 자동 경로가 그대로 남아 있다
  ② 기존 Chromium 프로필을 쓰고 **새 프로필을 만들지 않는다**
  ③ 쿠키를 **지우지 않는다**(복원만 한다) — 로컬 초기화는 뒤 단계가 한다
  ④ 창을 닫거나 시간이 지나면 `unknown` — '닫았으니 됐겠지' 가 없다
  ⑤ 화면이 **확인됨 / 아직 로그인됨 / 확인 불가** 를 구분한다
  ⑥ 공유 세션 보호·조회 실패 차단은 그대로다
  ⑦ 선택 계정이 **실행 중**이면 로그아웃을 막는다(순차 운영)
  ⑧ 발행·보류·재개는 바뀌지 않았다
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage13_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ["BLOG_QUEUE_BACKEND"] = "local"
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from v2 import browser, queue_store, session                  # noqa: E402
from v2.queue_store import PENDING, RUNNING                   # noqa: E402

OK, NG = "  [o]", "  [X]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def head(t: str) -> None:
    print("\n" + "=" * 76)
    print(f" {t}")
    print("=" * 76)


SES = (ROOT / "v2" / "session.py").read_text(encoding="utf-8")
BRW = (ROOT / "v2" / "browser.py").read_text(encoding="utf-8")
APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)

NS: dict = {"queue_store": queue_store, "chr": chr,
            "RUNNING_STATES": (PENDING, RUNNING)}
for _n in TREE.body:
    if (isinstance(_n, ast.Assign) and len(_n.targets) == 1
            and getattr(_n.targets[0], "id", "") == "LOGOUT_VERDICT"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)
for _n in TREE.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
            "is_logout_job", "logout_verdict"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)
logout_verdict = NS["logout_verdict"]


# ══════════════════════════════════════════════════════════════════
head("① 기본은 수동 · 자동은 지우지 않았다")

a = session.parse_args(["--logout", "x"])
check("기본이 수동이다(--auto 없음)", a.auto is False)
check("--auto 를 주면 자동이다", session.parse_args(
    ["--logout", "x", "--auto"]).auto is True)
import inspect                                                # noqa: E402

check("cmd_logout 이 auto 를 받는다",
      "auto" in inspect.signature(session.cmd_logout).parameters)
check("★옛 자동 경로(_server_logout)가 남아 있다",
      hasattr(session, "_server_logout"))
check("★수동 경로(_manual_logout)가 생겼다", hasattr(session, "_manual_logout"))
check("auto 일 때만 _server_logout 을 부른다",
      "if auto:" in SES and "_server_logout(acc, log)" in SES
      and "else:" in SES and "_manual_logout(acc, log)" in SES)


# ══════════════════════════════════════════════════════════════════
head("② 기존 프로필을 쓰고 새로 만들지 않는다")

src = SES[SES.index("async def _manual_logout"):SES.index("async def _auto_login_recheck")]
check("계정으로 설정을 만든다(load_settings(account=acc))",
      "load_settings(account=acc)" in src)
check("★창을 띄운다(headless=False)", "headless=False" in src)
check("프로필 경로를 직접 만들지 않는다",
      "mkdtemp" not in src and "user_data_dir=" not in src
      and "profile_dir" not in src)
check("세션 폴더 규칙이 그대로다(sessions/<account.id>/)",
      'SESSIONS_ROOT / ident' in (ROOT / "v2" / "session_store.py")
      .read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════
head("③ 쿠키를 지우지 않는다 — 복원만 한다")

check("★clear_cookies 를 부르지 않는다", "clear_cookies" not in src)
check("★clear_state 를 부르지 않는다", "clear_state" not in src)
check("저장 쿠키를 **복원**한다", "_restore_cookies" in src)
check("쿠키가 없어도 프로필 세션으로 연다", "프로필에 남은 세션으로 엽니다" in src)
check("로컬 초기화는 뒤 단계(session_store.logout)가 한다",
      "session_store.logout(acc, hard=hard)" in SES)


# ══════════════════════════════════════════════════════════════════
head("④ 기다리기 — '닫았으니 됐겠지' 가 없다")

wsrc = BRW[BRW.index("async def wait_manual_logout"):
           BRW.index("async def wait_manual_login")]
check("★failed 를 돌려주지 않는다(단정하지 않는다)", '"failed"' not in wsrc)
check("로그아웃을 확인하면 ok", 'return "ok"' in wsrc)
check("시간 초과면 unknown", wsrc.count('return "unknown"') >= 1
      and "확인되지 않았습니다" in wsrc)
check("창이 닫히면 unknown(성공으로 보지 않는다)", "창이 닫혔습니다" in wsrc)
check("아이디·비밀번호를 다루지 않는다",
      "password" not in wsrc.lower() and "fill(" not in wsrc)


class FakeCtx:
    """`_confirm_logged_in` 이 보는 최소한의 가짜 — 네트워크를 내지 않는다."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.pages = [self]

    async def wait_for_timeout(self, _ms):
        return None

    async def new_page(self):
        return self


def run_wait(seq, timeout=6):
    calls = {"n": 0}

    async def fake_confirm(_ctx, _home, _log):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    old = browser._confirm_logged_in
    browser._confirm_logged_in = fake_confirm
    try:
        return asyncio.run(browser.wait_manual_logout(
            FakeCtx(seq), lambda *a, **k: None, timeout_sec=timeout))
    finally:
        browser._confirm_logged_in = old


check("로그인 → 로그아웃으로 바뀌면 ok", run_wait(["myblog", ""]) == "ok")
check("계속 로그인 상태면 unknown(시간 초과)",
      run_wait(["myblog"], timeout=4) == "unknown")
check("처음부터 로그아웃 상태면 ok", run_wait([""]) == "ok")


# ══════════════════════════════════════════════════════════════════
head("⑤ 화면이 3상태를 구분한다")


class EvStore:
    def __init__(self, events): self.events = events
    def read_events(self, _jid, after=0): return (self.events, 0)


def lo_rec(status="done", jid="J1"):
    return {"id": jid, "status": status, "kind": "session",
            "job": {"extra": ["--logout", "acc_a", "--events"]}}


for got, key in (("clean", "clean"),
                 ("auto_login_detected", "auto_login_detected"),
                 ("unknown", "unknown"),
                 ("", "unknown"),                 # 값이 비면 확인 불가
                 ("뜻밖의값", "unknown")):        # 모르는 값도 확인 불가
    v = logout_verdict(EvStore([{"event": "logout_done", "auto_login": got}]),
                       lo_rec())
    check(f"auto_login={got!r:<22} → {key}", v and v[0] == key,
          str(v and v[0]))

check("★이벤트를 못 읽으면 확인 불가(성공으로 만들지 않는다)",
      (logout_verdict(EvStore([]), lo_rec()) or ("", ))[0] == "unknown")


class BrokenStore:
    def read_events(self, _jid, after=0): raise RuntimeError("큐 읽기 실패")


check("  조회가 터져도 확인 불가로 떨어진다",
      (logout_verdict(BrokenStore(), lo_rec()) or ("", ))[0] == "unknown")
check("차단된 로그아웃은 따로 알린다",
      (logout_verdict(EvStore([{"event": "logout_blocked"}]), lo_rec())
       or ("", ))[0] == "blocked")
check("진행 중이면 판정하지 않는다",
      logout_verdict(EvStore([]), lo_rec(status=RUNNING)) is None)
check("로그아웃 작업이 아니면 판정하지 않는다",
      logout_verdict(EvStore([]), {"id": "J", "status": "done",
                                   "job": {"extra": ["--login", "a"]}}) is None)
check("레코드가 없어도 터지지 않는다", logout_verdict(EvStore([]), None) is None)
check("★'작업 완료' 와 '로그아웃됨' 을 섞지 않는다",
      "작업이 '완료' 로 끝났다는" in APP)


# ══════════════════════════════════════════════════════════════════
head("⑥⑦ 차단 — 공유 세션은 그대로, 실행 중이면 막는다")

LS = (ROOT / "v2" / "login_state.py").read_text(encoding="utf-8")
check("logout_allowed 판정이 그대로다", "def logout_allowed(acc, path=None)" in LS)
check("session_peers(공유 후보 찾기)가 그대로다", "def session_peers(" in LS)
check("blog_id 위험 후보(A안)가 그대로다",
      "계정일 수 있습니다(확인되지 않음)" in LS)
check("cmd_logout 이 여전히 먼저 판정하고 막는다",
      "allowed, peers, why = login_state.logout_allowed(acc)" in SES
      and "if not allowed and not confirm_shared:" in SES)
check("조회 실패 시 차단(fail-safe)이 그대로다", "read_error" in APP)

check("★실행 중이면 로그아웃 버튼을 잠근다",
      "_busy = list(stt.get(\"running\") or [])" in APP
      and "or bool(_busy)" in APP)
check("  왜 막는지 알려 준다", "발행이 아직 돌고 있습니다" in APP)
check("  중단으로 풀 수 있다고 안내한다", "중단한 뒤 다시 눌러" in APP)
check("★대기 작업은 기존대로 **보류**로 옮긴다(막지 않는다)",
      "held = store_obj.hold_account(" in APP)


# ══════════════════════════════════════════════════════════════════
head("⑧ 기존 기능 무변경 · 동시성 장치를 더하지 않았다")

check("발행 경로(run/writer/edit_post)를 건드리지 않았다",
      all(not (ROOT / "v2" / f).read_text(encoding="utf-8").count("_manual_logout")
          for f in ("run.py", "run_production.py", "writer.py", "edit_post.py")))
check("보류·재개(_switch_status)가 그대로다",
      "def _switch_status(" in (ROOT / "v2" / "supabase_store.py")
      .read_text(encoding="utf-8"))
check("자동 로그인(--login) 경로가 그대로다",
      "def wait_manual_login(" in BRW and "--relogin" in SES)
check("★DB 잠금·동시성 테이블을 더하지 않았다",
      "account_holds" not in SES and "account_holds" not in APP
      and "FOR UPDATE" not in SES)
check("하드 로그아웃 승인 게이트가 그대로다",
      "BLOG_ALLOW_HARD_LOGOUT" in SES)
check("세션 폴더·토큰 경로 로직 무변경",
      "SESSIONS_ROOT = ROOT / \"sessions\"" in
      (ROOT / "v2" / "session_store.py").read_text(encoding="utf-8"))

print("")
print("=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
