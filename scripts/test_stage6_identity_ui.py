r"""Stage 6 검증 — 계정 식별 정확성 · fail-safe 범위 · UI 이동 회귀 (2026-11-04)

    .\.venv\Scripts\python.exe scripts\test_stage6_identity_ui.py

★브라우저·네이버·구글·운영 Supabase·운영 큐를 건드리지 않는다(임시폴더만).
  Supabase 는 `_rest`/`_count` 를 가로채므로 네트워크로 나가지 않는다.

검증 항목(사용자 요청 1·3·4·5·6·7)
  ① 계정 식별·조회 필터 — `test01` / `test010`, 밑줄(`_`) 포함 id, account.id 정확일치
     ★서버 `ilike` 는 **더 가져올 수는 있어도 덜 가져오지는 않는다**(안전 방향).
       받아온 뒤 `matches`(정규화 정확일치)로 다시 거르므로 남의 작업은 끼지 않는다.
  ③ fail-safe 범위 — 그 계정만 차단 · 요청 수 상한 · 0건으로 오표시되지 않음
  ④ 로그아웃 참조 일치 — 상태확인과 로그아웃이 같은 계정을 가리키는가
  ⑤ 부분 실패·복구 · 새로고침 후 상태 유지
  ⑥ UI 이동 회귀 — 상단 패널 1곳 · 중복 UI 제거 · 같은 함수 재사용
  ⑦ Agent 1.2.5 호환 · 발행 경로 무변경
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage6_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
for _k in ("BLOG_LOGIN_CHECK", "BLOG_READ_LOGIN_ID", "BLOG_QUEUE_BACKEND"):
    os.environ.pop(_k, None)

import time                                                    # noqa: E402

from v2 import queue_store                                     # noqa: E402
from v2.job import Job                                         # noqa: E402
from v2.loginid import norm_login, same_login                  # noqa: E402
from v2.queue_store import (HELD, LocalStore, PENDING,         # noqa: E402
                            RUNNING, matches)
from v2.supabase_store import SupabaseError, SupabaseStore     # noqa: E402

OK, NG = "  [o]", "  [X]"
fails: list[str] = []
notes: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def note(name: str, extra: str = "") -> None:
    print(f"  [!] {name}" + (f" - {extra}" if extra else ""))
    notes.append(name)


def head(t: str) -> None:
    print("\n" + "=" * 76)
    print(f" {t}")
    print("=" * 76)


APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)
NS: dict = {"queue_store": queue_store, "time": time, "chr": chr,
            "RUNNING_STATES": (PENDING, RUNNING),
            "QUEUE_READ_TRIES": 2, "QUEUE_READ_WAIT": 0.01}
for _n in TREE.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
            "is_logout_job", "logout_state", "logout_block"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)
logout_state, logout_block = NS["logout_state"], NS["logout_block"]


def panel_src() -> str:
    """`render_account_tools` 본문 소스(표시 구조 단정에 쓴다)."""
    return APP.split("def render_account_tools")[1].split("def do_logout")[0]


def sup(rest=None, count=None) -> SupabaseStore:
    s = SupabaseStore(mode="service", url="http://127.0.0.1:9", key="dummy")
    if rest is not None:
        s._rest = rest                                         # noqa: SLF001
    s._count = count if count is not None else (lambda q: None)  # noqa: SLF001
    return s


def row(jid, login="", acct="", status="held", kind="run", dev="PC1",
        at="2026-11-04T10:00:00"):
    return {"job_id": jid, "status": status, "kind": kind, "device_id": dev,
            "created_at": at,
            "payload": {"login_id": login, "account": acct}}


def serve(rows):
    """offset/limit 을 지키고, 서버 필터는 **모사하지 않는** 가짜 서버.

    ★일부러 필터를 무시한다 — 서버가 과하게 돌려줬을 때도 클라이언트가
      남의 작업을 고르지 않는지(= 최종 안전장치가 있는지) 보기 위해서다.
    """
    def rest(method, path, **kw):
        if method != "GET":
            ids = path.split("in.(")[1].split(")")[0].split(",")
            return [{"job_id": i} for i in ids]
        want = ("held" if "status=eq.held" in path else
                "pending" if "status=eq.pending" in path else "running")
        hit = [r for r in rows if r["status"] == want]
        if "kind=eq.run" in path:
            hit = [r for r in hit if r["kind"] == "run"]
        off = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        lim = int(path.split("limit=")[1].split("&")[0]) if "limit=" in path else 200
        return hit[off:off + lim]
    return rest


# ══════════════════════════════════════════════════════════════════
head("① 계정 식별 — 비슷한 이름이 섞이지 않는가")

s = sup()
q01 = s._status_query("held", "test01", "")                     # noqa: SLF001
check("서버 질의는 끝에 와일드카드를 붙이지 않는다(접두사 일치가 아니다)",
      "ilike.test01," in q01 or "ilike.test01&" in q01 or q01.endswith("ilike.test01)"),
      q01[q01.find("or=("):][:70])
check("  `*`·`%` 를 넣지 않는다", "*" not in q01 and "%25" not in q01)

#  test01 / test010 / TEST01 / test01@naver.com 이 섞인 큐
MIX = [row("J_01", login="test01"), row("J_010", login="test010"),
       row("J_UP", login="TEST01"), row("J_DOM", login="test01@naver.com"),
       row("J_OTH", login="someoneelse")]
got01 = sup(serve(MIX)).held_jobs("test01", "")
ids01 = sorted(r["id"] for r in got01)
check("★test01 조회에 test010 이 섞이지 않는다", "J_010" not in ids01, str(ids01))
check("  대소문자만 다른 같은 계정은 포함된다(TEST01)", "J_UP" in ids01)
check("  도메인 표기만 다른 같은 계정도 포함된다", "J_DOM" in ids01)
check("  남의 계정은 포함되지 않는다", "J_OTH" not in ids01)
got010 = sorted(r["id"] for r in sup(serve(MIX)).held_jobs("test010", ""))
check("★반대 방향도 안전하다 — test010 조회에 test01 이 안 섞인다",
      got010 == ["J_010"], str(got010))

#  밑줄이 들어간 id — LIKE 에서 `_` 는 한 글자 와일드카드다
UND = [row("U_REAL", login="shin894894_2"),
       row("U_WILD", login="shin894894x2"),       # `_` 가 와일드카드면 서버가 함께 준다
       row("U_LONG", login="shin894894_22")]
q_und = s._status_query("held", "shin894894_2", "")             # noqa: SLF001
check("밑줄 id 도 질의에 그대로 들어간다", "shin894894_2" in q_und.replace("%5F", "_"))
und = sorted(r["id"] for r in sup(serve(UND)).held_jobs("shin894894_2", ""))
check("★`_` 때문에 서버가 더 줘도 **클라이언트가 걸러낸다**",
      und == ["U_REAL"], str(und))
note("서버 `ilike` 는 `_`·`%` 를 와일드카드로 본다 — '더 가져옴' 쪽으로만 틀린다",
     "덜 가져오는(누락) 방향으로는 틀리지 않으므로 보류 누락은 생기지 않는다")
#  ★`matches` 는 **변환된 레코드**(job 키)를 받는다. 서버 원시 행(payload 키)과
#    헷갈리면 안 된다 — 아래 rec() 로 변환해 넘긴다.
def rec(login="", acct=""):
    return {"job": {"login_id": login, "account": acct}}


check("  최종 선별은 matches(정규화 정확일치)가 한다",
      matches(rec(login="shin894894_2"), "shin894894_2", "")
      and not matches(rec(login="shin894894x2"), "shin894894_2", ""))

#  account.id (login_id 가 없는 계정)
qa = s._status_query("held", "", "tab_abc")                     # noqa: SLF001
check("account 는 정확일치(eq)로 거른다",
      "payload->>account=eq.tab_abc" in qa.replace("%3E%3E", ">>"), qa[:70])
ACC = [row("A1", acct="tab_abc"), row("A2", acct="tab_abcd"),
       row("A3", acct="TAB_ABC"), row("A4", acct="tab_ab")]
acc_ids = sorted(r["id"] for r in sup(serve(ACC)).held_jobs("", "tab_abc"))
check("★tab_abc 가 tab_abcd·tab_ab 와 섞이지 않는다", acc_ids == ["A1"], str(acc_ids))
check("  세션 폴더 이름은 대소문자까지 구분한다(TAB_ABC 는 다른 계정)",
      "A3" not in acc_ids)

#  식별자 fallback
check("login_id 가 있으면 login_id 만 본다(account 는 보지 않는다)",
      matches(rec(login="aa", acct="zz"), "aa", "")
      and not matches(rec(login="bb", acct="zz"), "aa", "zz"))
check("login_id 가 없으면 account 로 본다",
      matches(rec(login="", acct="zz"), "", "zz"))
check("★둘 다 없으면 아무것도 고르지 않는다(임의 계정 선택 금지)",
      not matches(rec(login="aa", acct="zz"), "", "")
      and sup(serve(MIX)).hold_account("", account_id="") == [])
check("blog_id 로는 절대 고르지 않는다",
      not matches({"job": {"blog_id": "b1"}}, "b1", ""))

#  보류·재개가 다른 계정/다른 PC 를 건드리지 않는가
PICK = [row("P_MINE", login="test01", status="pending", dev="PC1"),
        row("P_SIM", login="test010", status="pending", dev="PC1"),
        row("P_OTHERPC", login="test01", status="pending", dev="PC2"),
        row("P_SESS", login="test01", status="pending", kind="session", dev="PC1"),
        row("P_RUN", login="test01", status="running", dev="PC1")]
patched: list[str] = []


def rest_pick(method, path, **kw):
    if method == "GET":
        return serve(PICK)(method, path, **kw)
    ids = path.split("in.(")[1].split(")")[0].split(",")
    patched.extend(ids)
    return [{"job_id": i} for i in ids]


held_ids = sup(rest_pick).hold_account("test01", device="PC1")
check("★보류는 내 계정·내 PC·kind=run 인 대기 작업만 고른다",
      held_ids == ["P_MINE"], str(held_ids))
check("  비슷한 이름(test010)·다른 PC·session·running 은 건드리지 않는다",
      not [i for i in patched if i != "P_MINE"], str(patched))


# ══════════════════════════════════════════════════════════════════
head("③ fail-safe 적용 범위 · 요청 수")

calls = {"n": 0}


class Flaky:
    """항상 5xx — 몇 번 호출되는지 센다."""
    def list_by_status(self, *_a, **_k):
        calls["n"] += 1
        raise SupabaseError("GET jobs 실패(503)", status=503)

    def list_jobs(self, limit=30):
        raise SupabaseError("GET jobs 실패(503)", status=503)


calls["n"] = 0
why = logout_block(Flaky(), "test01", "")
check("★조회 실패면 그 계정의 신규 발행을 막는다",
      "확인하지 못해 새 작업을 만들지 않았습니다" in why)
check("  문구에 계정 이름이 들어간다(어느 계정이 막혔는지)", "test01" in why)
check("  다른 계정은 영향 없다고 적는다", "다른 계정의 발행은 영향받지 않습니다" in why)
check("★조회 호출 수가 상한 안이다(상태 3종 × 재시도 2회 = 6회 이하)",
      calls["n"] <= 6, f"{calls['n']}회")

good = LocalStore(SANDBOX / "q_other")
check("같은 화면에서 다른 정상 계정은 막히지 않는다",
      logout_block(good, "otheraccount", "") == "")
mine = good.submit(Job(brand="b", account="acc_m", login_id="test01",
                       flow="production", count=10), title="t",
                   target_agent="PC1")
good.hold_account("test01", device="PC1")
check("보류된 계정만 막힌다",
      "로그아웃 대기 상태입니다" in logout_block(good, "test01", "")
      and logout_block(good, "test010", "") == "")

stt_err = logout_state(Flaky(), "test01", "")
check("★API 오류를 '보류 0건' 으로 표시하지 않는다",
      stt_err["held"] == [] and bool(stt_err["read_error"])
      and stt_err["pending"] is False)
check("  화면은 숫자 대신 '확인하지 못했습니다' 를 띄운다",
      'if stt.get("read_error"):' in APP
      and "보류 상태를 확인하지 못했습니다" in APP)
check("  그 경우 건수 metric 을 아예 그리지 않는다(0 으로 오해 방지)",
      "    else:\n        m1, m2, m3 = st.columns(3)" in panel_src()
      and 'm1.metric("대기"' in panel_src())

#  응답 지연 — 요청 1건당 타임아웃(TIMEOUT)과 최악 조합을 기록한다
from v2.supabase_store import TIMEOUT                           # noqa: E402

worst = 3 * NS["QUEUE_READ_TRIES"] * 2 * 2 * TIMEOUT
note(f"★최악 응답 지연 — 상태 3종 × 재시도 {NS['QUEUE_READ_TRIES']}회 × "
     f"(count+페이지) × 총건수 재확인 × {TIMEOUT}초 = 최대 {worst}초",
     "서버가 '응답 없이 매달리는' 상태에서만 발생한다(오류는 즉시 돌아온다)")


# ══════════════════════════════════════════════════════════════════
head("④ 로그인 상태 확인 ↔ 로그아웃의 계정 참조 일치")

panel = APP.split("def render_account_tools")[1].split("def do_logout")[0]
check("식별자를 한 번만 정한다(lid=login_id, aid=없을 때만 account.id)",
      'lid = (getattr(account, "login_id", "") or "").strip()' in panel
      and 'aid = "" if lid else (getattr(account, "id", "") or "").strip()' in panel)
check("상태확인은 그 account 객체를 그대로 넘긴다",
      'submit_session_job(store_obj, brand, account, "--check"' in panel)
check("로그아웃도 같은 lid·aid 를 넘긴다",
      "do_logout(store_obj, brand, account, lid, device_id," in panel
      and "account_id=aid" in panel)
check("재개도 같은 lid·aid 를 넘긴다",
      "do_resume(store_obj, lid, device_id, account_id=aid)" in panel)
check("상태 조회도 같은 lid·aid 로 한다", "logout_state(store_obj, lid, aid)" in panel)
check("식별 불가면 버튼이 잠긴다",
      "disabled=not (device_id and (lid or aid) and can_logout)" in panel)
check("식별 불가 사유를 화면에 적는다", "계정을 특정할 수 없습니다" in panel)
check("버전 게이트가 그대로다(1.3.0 미만 잠금)",
      "can_logout = agent_can_logout(device)" in panel)
check("실행 중 배치는 끝까지 간다고 안내한다",
      "끝까지 발행" in panel)


# ══════════════════════════════════════════════════════════════════
head("⑤ 부분 실패·복구 · 새로고침 후 상태 유지")

ROWS = [row(f"H{i}", login="test01", status="pending") for i in range(120)]
cnt = {"patch": 0}


def partial(method, path, **kw):
    if method == "GET":
        return serve(ROWS)(method, path, **kw)
    cnt["patch"] += 1
    ids = path.split("in.(")[1].split(")")[0].split(",")
    if cnt["patch"] == 2:
        raise SupabaseError("PATCH 실패(503)", status=503)
    for i in ids:
        for r in ROWS:
            if r["job_id"] == i:
                r["status"] = "held"
    return [{"job_id": i} for i in ids]


err = None
try:
    sup(partial).hold_account("test01", device="PC1")
except Exception as exc:                                       # noqa: BLE001
    err = exc
check("부분 실패 시 실제 건수를 재조회해 알려 준다",
      getattr(err, "verified", None) is not None and err.confirmed == 50,
      f"confirmed={getattr(err, 'confirmed', None)}")
check("화면이 성공/실패 건수를 나눠 보여 준다",
      "일부만 보류했습니다" in APP and "실패/미확인" in APP)
check("재조회 실패는 0건이 아니라 '확인 불가' 로 안내한다",
      "확인하지 못했습니다" in APP and "0건이 아닙니다" in APP)

#  중복 재개 — 두 번 눌러도 이미 돌아간 것은 건드리지 않는다
res_rows = [row("R1", login="test01", status="held"),
            row("R2", login="test01", status="pending")]
touched: list[str] = []


def rest_res(method, path, **kw):
    if method == "GET":
        return serve(res_rows)(method, path, **kw)
    ids = path.split("in.(")[1].split(")")[0].split(",")
    touched.extend(ids)
    for i in ids:
        for r in res_rows:
            if r["job_id"] == i:
                r["status"] = "pending"
    return [{"job_id": i} for i in ids]


st1 = sup(rest_res)
first = st1.resume_account("test01", device="PC1")
second = st1.resume_account("test01", device="PC1")
check("★재개는 held 인 것만 되돌린다", first == ["R1"] and touched == ["R1"])
check("★두 번째 재개는 아무것도 바꾸지 않는다(중복 처리 없음)", second == [])
check("화면은 '이미 재개된 상태' 로 알린다", "이미 재개된 상태입니다" in APP)

#  새로고침(= 새 저장소 객체·빈 캐시) 후에도 보류가 보인다
fresh_dir = SANDBOX / "q_refresh"
s1 = LocalStore(fresh_dir)
j1 = s1.submit(Job(brand="b", account="acc_r", login_id="test01",
                   flow="production", count=10), title="t", target_agent="PC1")
s1.hold_account("test01", device="PC1")
s2 = LocalStore(fresh_dir)                      # 새로고침
check("★새로고침 뒤에도 보류 상태가 유지된다",
      [r["id"] for r in s2.held_jobs("test01")] == [j1])
check("  상태 판단이 session_state 에 의존하지 않는다",
      "st.session_state" not in APP.split("def logout_state")[1].split(
          "def logout_block")[0])


# ══════════════════════════════════════════════════════════════════
head("⑥ UI 이동 회귀 — 자리만 바뀌고 기능은 그대로인가")

check("★상단 두 칸을 st.columns 로 만든다",
      '_top_left, _top_right = st.columns([2, 1], gap="large")' in APP)
check("★오른쪽 칸을 자리(st.empty)로 잡아 둔다",
      "ACCOUNT_BOX = _top_right.empty()" in APP)
check("계정이 정해진 뒤 그 자리를 채운다(순서 변경 없음)",
      "with ACCOUNT_BOX.container(border=True):" in APP
      and APP.index("ready_info = session_ready")
      < APP.rindex("with ACCOUNT_BOX.container"))
check("★패널 호출은 **한 곳**뿐이다(중복 UI 없음)",
      APP.count("render_account_tools(store, brand, account") == 1,
      f"{APP.count('render_account_tools(store, brand, account')}곳")
check("접힌 expander 를 쓰지 않는다(바로 보인다)",
      'st.expander("계정 세션 관리' not in APP and "계정 세션 관리 (보조)" not in APP)
check("제목이 '네이버 계정 관리' 다", "네이버 계정 관리" in APP)
check("계정 아이디를 보여 준다", '"아이디 `" + lid + "`"' in panel)
check("로그인 상태를 보여 준다",
      "● 로그인됨" in panel and "○ 로그인 안 됨" in panel
      and "▶ 로그인 진행 중" in panel)
check("대기·실행중·보류 건수를 보여 준다",
      'm1.metric("대기"' in panel and 'm2.metric("실행 중"' in panel
      and 'm3.metric("보류"' in panel)
check("대기 건수는 이미 받아 온 목록을 나눠 쓴다(조회 추가 없음)",
      'out["waiting"] = [r for r in pending_all' in APP)
check("[로그인 상태 다시 확인]·[선택 계정 로그아웃]·[보류 작업 재개] 가 모두 있다",
      "로그인 상태 다시 확인" in panel and "선택 계정 로그아웃" in panel
      and "보류 작업 재개" in panel)
check("공유 세션 확인 체크박스가 유지된다", "공유 세션 확인" in panel)
check("기존 함수를 그대로 재사용한다(새 로그아웃 로직 없음)",
      panel.count("do_logout(") == 1 and panel.count("do_resume(") == 1
      and "hold_account" not in panel and "resume_account" not in panel)
check("이 계정에만 적용된다는 안내가 남아 있다",
      "이 계정에만** 적용됩니다" in panel)
check("실행 준비 영역의 로그인/준비 로직은 그대로다",
      "logged_in = bool(info[\"state_exists\"])" in APP
      and "ready = bool(agent_ready) and logged_in" in APP)
check("로그인 상태는 **표시용**으로만 넘긴다(판단은 기존 코드가 한다)",
      "logged_in=logged_in, job_state=job_state" in APP
      and "logged_in: bool | None = None" in panel)


# ══════════════════════════════════════════════════════════════════
head("⑦ Agent 1.2.5 호환 · 발행 경로 무변경")

import subprocess                                              # noqa: E402

diff = (subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only",
                        "da602b9", "HEAD"], stdout=subprocess.PIPE,
                       encoding="utf-8", errors="replace").stdout or "").split()
locked = ["v2/run.py", "v2/run_production.py", "v2/writer.py",
          "v2/edit_post.py", "v2/sheets.py", "v2/landing_sheet.py",
          "v2/job.py", "v2/batch_plan.py", "v2/agent.py", "agent_tray.py",
          "v2/accounts.py", "v2/session_store.py", "supabase/schema.sql"]
check("발행·시트·Job·배치·Agent·스키마 파일 무변경",
      not [f for f in locked if f in diff],
      f"변경={[f for f in locked if f in diff] or '없음'}")
ag = (ROOT / "v2" / "agent.py").read_text(encoding="utf-8")
check("Agent 는 새 API·예외를 쓰지 않는다",
      "list_by_status" not in ag and "PartialHoldError" not in ag
      and "loginid" not in ag)
check("claim 은 pending 만 집는다 → held 는 1.2.5 대상이 아니다",
      "where device_id = v_id and status = 'pending'" in
      (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8"))
loc = LocalStore(SANDBOX / "q_claim")
jc = loc.submit(Job(brand="b", account="acc_c", login_id="test01",
                    flow="production", count=10), title="t", target_agent="PC1")
loc.hold_account("test01", device="PC1")
check("보류 작업은 claim 되지 않는다", loc.claim("PC1") is None)
loc.resume_account("test01", device="PC1")
check("재개하면 claim 된다", (loc.claim("PC1") or {}).get("id") == jc)
key = queue_store.new_client_key()
idem = LocalStore(SANDBOX / "q_idem6")
r1 = queue_store.submit_checked(idem, Job(brand="b", account="a",
                                          login_id="test01"), kind="run",
                                title="t", target_agent="PC1", client_key=key)
r2 = queue_store.submit_checked(idem, Job(brand="b", account="a",
                                          login_id="test01"), kind="run",
                                title="t", target_agent="PC1", client_key=key)
check("client_key 중복 방지 유지", r1.ok and r2.ok
      and len(idem.list_jobs(limit=50)) == 1)
check("배포 버전 표기는 한 곳에서만 온다(pairing.AGENT_VERSION)",
      'AGENT_VERSION = "1.3.0"' in
      (ROOT / "v2" / "pairing.py").read_text(encoding="utf-8"))
note("★AGENT_VERSION 이 아직 1.3.0 이다 — 11/03 빌드본(Stage 4·5 미포함)과 "
     "내용이 달라진다", "재빌드 전에 1.3.1 로 올려야 한다(승인 대기)")


# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
if notes:
    print(f" 확인된 현재 동작·위험 {len(notes)}건:")
    for n in notes:
        print(f"   · {n}")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
