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

head("⑨ 공유 계정 차단 정책 변경 (2026-11-05) — 막지 않고 알린다")

#  브랜드가 달라도 **같은 네이버 ID** 를 쓴다 → 그 자체는 차단 사유가 아니다.
from v2 import login_state as LS                              # noqa: E402
from v2.accounts import Account                               # noqa: E402
import json                                                   # noqa: E402


def _accounts(rows) -> Path:
    p = SANDBOX / f"acc_{len(list(SANDBOX.glob('acc_*.json')))}.json"
    p.write_text(json.dumps({"accounts": rows}, ensure_ascii=False),
                 encoding="utf-8")
    return p


#  ① 같은 blog_id · 양쪽 다 login_id 미등록
rows = [{"id": "x_re", "label": "브랜드A", "login_id": "", "blog_id": "same",
         "brand": "repurely", "enabled": True},
        {"id": "x_dn", "label": "브랜드B", "login_id": "", "blog_id": "same",
         "brand": "doctor_nuscent", "enabled": True}]
P1 = _accounts(rows)
A1 = Account(id="x_re", label="브랜드A", blog_id="same", brand="repurely")
ok1, pr1, why1 = LS.logout_allowed(A1, P1)
check("★같은 blog_id · login_id 미등록 → **허용**", ok1 is True, str(ok1))
check("  후보는 그대로 찾아낸다(안내용)", len(pr1) == 1, f"{len(pr1)}건")
check("  영향 범위를 문구로 알린다", "같은 네이버 계정일 수 있는 등록이" in why1,
      why1[:50])

#  ② login_id 가 서로 다른 경우도 허용
rows2 = [{"id": "y1", "label": "A", "login_id": "me", "blog_id": "sh2",
          "brand": "repurely", "enabled": True},
         {"id": "y2", "label": "B", "login_id": "someoneelse", "blog_id": "sh2",
          "brand": "repurely", "enabled": True}]
P2 = _accounts(rows2)
ok2, _, why2 = LS.logout_allowed(
    Account(id="y1", label="A", login_id="me", blog_id="sh2"), P2)
check("★login_id 가 서로 달라도 **허용**", ok2 is True, str(ok2))
check("  그 등록을 사유에 적는다", "someoneelse" in why2, why2[:60])

#  ③ 후보가 없으면 예전처럼 조용히 허용
P3 = _accounts([{"id": "z1", "label": "혼자", "login_id": "solo",
                 "blog_id": "alone", "brand": "repurely", "enabled": True}])
ok3, pr3, why3 = LS.logout_allowed(
    Account(id="z1", label="혼자", login_id="solo", blog_id="alone"), P3)
check("후보가 없으면 그대로 허용 · 안내 없음",
      ok3 is True and pr3 == [] and why3 == "")

#  ④ 화면 — 체크박스를 **요구하지 않는다**
check("★'공유 세션 확인' 체크박스를 요구하지 않는다", "_shared_" not in APP)
#  ★문구가 줄바꿈으로 쪼개져 있다 — 앞 조각으로 본다.
check("  선택한 등록 외는 건드리지 않는다고 적는다",
      "선택한 등록 외의 쿠키·프로필·계정 " in APP)

#  ★가장 중요한 것 — `confirm_shared` 를 **묻지도 않고 True 로 보내지 않는다.**
#    체크박스를 없애면서 한때 `shared_ok = True` 로 무조건 보냈는데, 그건
#    Agent 의 보호 조건을 조용히 넘기는 것이었다(2026-11-05 지적받아 고침).
check("★confirm_shared 를 무조건 True 로 보내지 않는다",
      "shared_ok = True" not in APP and "confirm_shared=bool(shared_ok)" not in APP)
check("★후보가 있을 때만 confirm_shared 가 간다(= 사용자가 보고 누른 경우)",
      "confirm_shared=bool(_need)" in APP)
check("★후보가 있으면 **한 번 더 눌러야** 실행된다(2단계)",
      "_need and not _ready" in APP
      and "st.session_state[_ckey] = True" in APP)
check("  1단계 버튼은 '확인' 이고 2단계에서 실행된다",
      "영향 범위를 확인했습니다 — 계속" in APP)
check("  확인 상태는 계정마다 따로 기억한다(다른 계정에 번지지 않는다)",
      '_ckey = f"_logout_ok_{aid or lid}"' in APP)
check("  실행하면 확인 상태를 지운다(다음에 또 묻는다)",
      "st.session_state.pop(_ckey, None)" in APP)
check("  영향 범위를 **목록으로** 보여 준다",
      "같은 네이버 계정일 수 있는 등록이" in APP and "login_id=" in APP)
#  ★문구가 여러 줄로 쪼개져 있다 — 이어지는 조각으로 본다.
check("  후보 조회가 실패해도 막지 않고 사실만 알린다",
      "_peers_err" in APP and "같은 세션을 쓰는 등록이 있는지 **확인하지 " in APP
      and "로그아웃을 막지는 " in APP)

#  ★Agent 1.3.3 호환 — 그 설치본에는 **옛 차단 정책**이 들어 있다.
#    화면이 보내는 `--confirm-shared` 로만 통과한다. 그래서 '후보를 못 봐서
#    안 보냈는데 PC 는 막는' 경우가 **영구 차단**이 되면 안 된다.
OLD_ALLOWED = """
def logout_allowed_1_3_3(peers, mine, confirm_shared):
    #  1.3.3 설치본의 판정을 그대로 옮긴 것(차단 분기 포함)
    if not peers:
        return True
    odd = [p for p in peers if not (mine and p.get("login_id") == mine)]
    allowed = not odd
    return allowed or confirm_shared          # cmd_logout 의 통과 조건
"""
_ns: dict = {}
exec(OLD_ALLOWED, _ns)
old_ok = _ns["logout_allowed_1_3_3"]

check("★[1.3.3] 후보 없음 → confirm 없이도 통과",
      old_ok([], "me", False) is True)
check("★[1.3.3] 후보 있고 login_id 같음 → confirm 없이도 통과",
      old_ok([{"login_id": "me"}], "me", False) is True)
check("★[1.3.3] 후보 있고 login_id 미등록 → confirm 없으면 **차단**",
      old_ok([{"login_id": ""}], "me", False) is False)
check("★[1.3.3] 그때 confirm 을 보내면 **통과**(화면 2단계가 이걸 보낸다)",
      old_ok([{"login_id": ""}], "me", True) is True)
check("★[1.3.3] login_id 가 서로 달라도 confirm 이면 통과",
      old_ok([{"login_id": "other"}], "me", True) is True)

check("★차단당한 뒤에는 화면이 **확인 단계를 연다**(영구 차단 방지)",
      "_was_blocked = bool(_ver and _ver[0] == \"blocked\")" in APP
      and "_need = bool(_peers) or _was_blocked" in APP)
check("  차단 안내가 없어진 체크박스를 가리키지 않는다",
      "'공유 세션 확인' 을 읽고 체크한 뒤" not in APP)
check("  후보가 안 보이는데 막힌 경우 건수를 지어내지 않는다",
      "PC 가 같은 세션을 쓰는 등록을 찾아 " in APP)

#  후보 목록은 화면이 직접 본다 — 원격(Supabase) 포함
LA = APP[APP.index("def logout_peers("):APP.index("def logout_verdict(")]
check("★후보 조회는 session_peers 를 그대로 쓴다(판정을 베끼지 않는다)",
      "_ls.session_peers(account)" in LA)
check("  실패해도 빈 목록 + 사유로 돌려준다(터지지 않는다)", "return [], f" in LA)
check("  원격 포함이라고 적어 둔다(로컬 파일만 보는 게 아니다)",
      "원격(Supabase) 우선" in LA)
check("  화면에서는 has_session 숫자를 쓰지 않는다(클라우드엔 세션 파일이 없다)",
      "전부 False 로 나온다" in LA)

#  ⑤ 진짜 위험은 **그대로 막는다**
check("★발행 중이면 여전히 잠근다", "or bool(_busy)" in APP)
check("★큐를 못 읽으면 여전히 잠근다",
      '_unknown = bool(stt.get("read_error"))' in APP and "or _unknown" in APP)
check("  왜 잠그는지 알려 준다(모르는 것과 없는 것을 구분)",
      "확인하지 못했습니다** — 로그아웃을 잠급니다" in APP)

#  ⑥ 보류·재개와 쿠키 취급은 그대로
check("대기 작업 자동 보류가 그대로다", "held = store_obj.hold_account(" in APP)
check("재개가 그대로다", "do_resume(store_obj," in APP)
check("★확인 전 쿠키 초기화로 성공을 오판하지 않는다(수동 경로는 안 지운다)",
      "clear_cookies" not in src and "clear_state" not in src)
check("수동이 기본 · --auto 로 자동 보존", session.parse_args(
    ["--logout", "x"]).auto is False and hasattr(session, "_server_logout"))

print("")
print("=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
