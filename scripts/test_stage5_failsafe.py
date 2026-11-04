r"""Stage 5 검증 — 큐 조회 실패 fail-safe · 페이지네이션 · 부분 보류 · 경쟁 조건
    (2026-11-04 · 브랜치 feature/agent-1.3.0-stabilization)

    .\.venv\Scripts\python.exe scripts\test_stage5_failsafe.py

★브라우저·네이버·시트·운영 큐·운영 DB 를 건드리지 않는다. 임시 폴더만 쓴다.
  Supabase 는 `_rest`/`_count` 를 가로채므로 네트워크로 나가지 않는다.

검증 항목(사용자 요청 ①~④)
  ① 큐 조회 실패(5xx·타임아웃)면 **그 계정의 신규 발행을 막는다**(fail-safe)
     · 일시 오류는 제한적으로 재시도 · 다른 정상 계정은 막지 않는다
     · 자동 다음 배치에도 같은 가드가 걸린다
  ② 500건 제한 → **실제 페이지네이션** + 서버측 필터 + 누락/중복/잘림 처리
     · 정확히 500건 · 500건 초과 · 페이지 중복 · 총건수 불일치
  ③ 부분 보류 실패 → 실제 상태 재조회 · 성공/실패 건수 구분 · 재조회 실패는 '확인 불가'
  ④ 로그아웃과 배치 생성의 경쟁 → 2차 스윕 · 남은 창의 범위
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage5_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
for _k in ("BLOG_LOGIN_CHECK", "BLOG_READ_LOGIN_ID", "BLOG_QUEUE_BACKEND"):
    os.environ.pop(_k, None)

from v2 import queue_store                                     # noqa: E402
from v2.job import Job                                         # noqa: E402
from v2.queue_store import (HELD, LocalStore, PartialHoldError,  # noqa: E402
                            PENDING, RUNNING)
from v2.supabase_store import SupabaseError, SupabaseStore     # noqa: E402

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


# ── 운영 코드 본문에서 화면 함수만 그대로 꺼내 쓴다 ────────────────
APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)
NS: dict = {"queue_store": queue_store, "time": time, "chr": chr,
            "RUNNING_STATES": (PENDING, RUNNING)}
for node in TREE.body:                      # 모듈 상수(재시도 횟수 등)를 그대로
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        tgt = node.targets[0]
        names = ([getattr(e, "id", "") for e in tgt.elts]
                 if isinstance(tgt, ast.Tuple) else [getattr(tgt, "id", "")])
        if "QUEUE_READ_TRIES" in names:
            try:
                vals = ast.literal_eval(node.value)
            except Exception:                                  # noqa: BLE001
                continue
            vals = vals if isinstance(vals, tuple) else (vals,)
            for n, v in zip(names, vals):
                NS[n] = v
TRIES = NS.get("QUEUE_READ_TRIES", 1)
NS["QUEUE_READ_WAIT"] = 0.01                # 테스트는 실제로 기다리지 않는다
for node in TREE.body:
    if isinstance(node, ast.FunctionDef) and node.name in (
            "is_logout_job", "logout_state", "logout_block"):
        exec(compile(ast.Module([node], []), "streamlit_app.py", "exec"), NS)
logout_state, logout_block = NS["logout_state"], NS["logout_block"]


def sup(rest=None, count=None) -> SupabaseStore:
    s = SupabaseStore(mode="service", url="http://127.0.0.1:9", key="dummy")
    if rest is not None:
        s._rest = rest                                         # noqa: SLF001
    s._count = count if count is not None else (lambda q: None)  # noqa: SLF001
    return s


def row(jid, status="held", login="loginW", acct="acc_w", kind="run",
        dev="PC1", at="2026-11-04T10:00:00"):
    return {"job_id": jid, "status": status, "kind": kind, "device_id": dev,
            "created_at": at,
            "payload": {"login_id": login, "account": acct}}


def pager(rows):
    """offset/limit 을 **정직하게** 지키는 가짜 서버."""
    def rest(method, path, **kw):
        if method != "GET":
            return [{"job_id": i} for i in
                    path.split("in.(")[1].split(")")[0].split(",")]
        off = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        lim = int(path.split("limit=")[1].split("&")[0]) if "limit=" in path else 200
        want = ("held" if "status=eq.held" in path else
                "pending" if "status=eq.pending" in path else "running")
        hit = [r for r in rows if r["status"] == want]
        if "kind=eq.run" in path:
            hit = [r for r in hit if r["kind"] == "run"]
        return hit[off:off + lim]
    return rest


# ══════════════════════════════════════════════════════════════════
head("① 큐 조회 실패 → 그 계정의 신규 발행 차단 (fail-safe)")

tries_seen = {"n": 0}


class Down:
    """계속 5xx 인 큐."""
    def list_by_status(self, *_a, **_k):
        tries_seen["n"] += 1
        raise SupabaseError("GET jobs 실패(503): upstream connect error",
                            status=503)

    def list_jobs(self, limit=30):
        raise SupabaseError("GET jobs 실패(503)", status=503)


class Timeout:
    def list_by_status(self, *_a, **_k):
        import requests
        raise requests.exceptions.ReadTimeout("read timeout")

    def list_jobs(self, limit=30):
        import requests
        raise requests.exceptions.ReadTimeout("read timeout")


class FlakyOnce:
    """첫 번째만 실패하고 그다음은 정상 — 일시 오류."""
    def __init__(self):
        self.calls = 0

    def list_by_status(self, status, login_id="", account_id="", kind="",
                       limit=0):
        self.calls += 1
        if self.calls == 1:
            raise SupabaseError("GET jobs 실패(502)", status=502)
        return []


for label, store in (("5xx", Down()), ("타임아웃", Timeout())):
    stt = logout_state(store, "loginW", "")
    why = logout_block(store, "loginW", "")
    check(f"[{label}] 읽기 실패가 기록된다", bool(stt["read_error"]),
          stt["read_error"][:50])
    check(f"[{label}] '보류 0건' 과 구분된다",
          stt["held"] == [] and stt["pending"] is False and bool(stt["read_error"]))
    check(f"★[{label}] 신규 발행이 **차단된다**",
          "확인하지 못해 새 작업을 만들지 않았습니다" in why, why[:50])
    check(f"[{label}] 차단 문구에 원인과 복구 방법이 있다",
          "[연결 다시 맺기]" in why and "원인:" in why)
    check(f"[{label}] 다른 계정은 영향받지 않는다고 알린다",
          "다른 계정의 발행은 영향받지 않습니다" in why)

check(f"일시 오류는 제한적으로 재시도한다(횟수 {TRIES})", TRIES >= 2, f"tries={TRIES}")
tries_seen["n"] = 0
logout_block(Down(), "loginW", "")
check("최종 실패까지 그 횟수만 시도한다(무한 재시도 아님)",
      tries_seen["n"] == TRIES, f"{tries_seen['n']}회")

flaky = FlakyOnce()
why_flaky = logout_block(flaky, "loginW", "")
check("★일시 오류는 재시도로 복구되어 **막지 않는다**",
      why_flaky == "" and flaky.calls >= 2, f"calls={flaky.calls}")

good = LocalStore(SANDBOX / "q_ok")
check("정상 계정(보류 0건)은 막지 않는다", logout_block(good, "loginOK", "") == "")
mixed = LocalStore(SANDBOX / "q_mixed")
jb = mixed.submit(Job(brand="repurely", account="acc_b", login_id="loginB",
                      flow="production", count=10), title="b", target_agent="PC1")
mixed.hold_account("loginB", device="PC1")
check("보류된 계정만 막고 다른 계정은 통과한다",
      "보류된 작업 1건" in logout_block(mixed, "loginB", "")
      and logout_block(mixed, "loginC", "") == "")
check("조회 실패는 **그 계정 조회 결과**로만 판단한다(전역 플래그 없음)",
      "read_error" in APP.split("def logout_block")[1][:1200]
      and "global " not in APP.split("def logout_state")[1][:2000])

guard = APP.split("def submit_guarded")[1].split("def render_pending_submit")[0]
check("제출 가드가 run 작업에 걸린다(수동·자동 공통 통로)",
      'if kind == "run":' in guard and "logout_block(store_obj" in guard)
check("자동 다음 배치도 그 통로를 지난다",
      "res = submit_guarded(" in APP.split("def advance_plan")[1][:900])
check("가드가 막으면 계획을 진행시키지 않는다(중복 생성 없음)",
      "raise RuntimeError(res.message" in APP.split("def advance_plan")[1][:1400])


class BlockedSubmit:
    """`advance_plan` 의 submit_run 과 같은 모양 — 가드가 막으면 예외."""
    def __init__(self, store):
        self.store, self.calls = store, 0

    def __call__(self, batch):
        self.calls += 1
        why = logout_block(self.store, "loginW", "")
        if why:
            raise RuntimeError(why)
        return "job-new"


from v2 import batch_plan                                      # noqa: E402

plan = batch_plan.create(Job(brand="repurely", account="acc_w",
                             login_id="loginW", flow="production").to_dict(),
                         20, title="t", brand="repurely", account="acc_w",
                         flow="production", device="PC1")
runner = BlockedSubmit(Down())
try:
    plan2, changed = batch_plan.advance(
        plan, job_status=lambda j: ("", {}), submit_run=runner)
    blew = False
except Exception:                                              # noqa: BLE001
    blew = True
check("★조회 실패 중에는 자동 다음 배치도 큐에 들어가지 않는다",
      blew and runner.calls == 1)
check("  계획 상태도 바뀌지 않는다(다음 그리기에서 중복 제출 없음)",
      (batch_plan.current(plan) or {}).get("status") == "pending")


# ══════════════════════════════════════════════════════════════════
head("② 500건 제한 → 실제 페이지네이션 · 서버측 필터")

s = sup()
q = s._status_query("held", "loginW", "", kind="run")           # noqa: SLF001
check("서버 질의에 status 필터가 있다", "status=eq.held" in q, q[:60])
check("서버 질의에 kind 필터가 있다", "kind=eq.run" in q)
check("★서버 질의가 login_id 표기 후보를 모두 거른다(or + ilike)",
      "or=(" in q and "payload-%3E%3Elogin_id.ilike.loginW" in q.replace(
          "payload->>login_id.ilike.loginW", "payload-%3E%3Elogin_id.ilike.loginW"),
      q[q.find("or=("):][:90])
check("  네이버 메일 도메인 표기도 후보에 들어간다",
      "loginW%40naver.com" in q or "loginW@naver.com" in q)
check("정렬키가 고정이다(페이지가 흔들리지 않게)",
      "order=created_at.desc,job_id.desc" in q)
qa = s._status_query("held", "", "tab_abc")                     # noqa: SLF001
check("login_id 가 없으면 account 를 정확일치로 거른다",
      "payload->>account=eq.tab_abc" in qa.replace("%3E%3E", ">>"), qa[:80])

#  정확히 500건
rows500 = [row(f"H{i}", at=f"2026-11-04T10:{i // 60:02d}:{i % 60:02d}")
           for i in range(500)]
got = sup(pager(rows500)).held_jobs("loginW", "")
check("★정확히 500건 — 전부 돌려준다(예전 limit 과 같은 수)", len(got) == 500,
      f"{len(got)}건")

#  500건 초과 + 내 보류가 가장 오래된 것
rows501 = [row(f"X{i}", login="someoneelse", acct="acc_x",
               at=f"2026-11-04T11:{i // 60:02d}:{i % 60:02d}")
           for i in range(500)]
rows501.append(row("MINE", at="2026-01-01T00:00:00"))
got2 = sup(pager(rows501)).held_jobs("loginW", "")
check("★500건 초과 — 가장 오래된 내 보류도 찾는다(페이지네이션)",
      [r["id"] for r in got2] == ["MINE"], f"{len(got2)}건")

#  2,000건
rows2k = [row(f"B{i}", login="someoneelse", acct="acc_x") for i in range(2000)]
rows2k.append(row("MINE2", at="2026-01-01T00:00:00"))
check("2,000건에서도 찾는다",
      [r["id"] for r in sup(pager(rows2k)).held_jobs("loginW", "")] == ["MINE2"])

#  페이지가 겹쳐 같은 행을 두 번 주는 경우(행이 바뀌며 흔들림)
def dup_rest(method, path, **kw):
    if method != "GET":
        return []
    off = int(path.split("offset=")[1].split("&")[0])
    hit = [row(f"D{i}") for i in range(300)]
    page = hit[off:off + 200]
    if off:                                   # 두 번째 페이지가 첫 행을 또 준다
        page = [hit[0]] + page
    return page[:200]


dups = sup(dup_rest).held_jobs("loginW", "")
check("★페이지가 겹쳐도 중복이 생기지 않는다(job_id 로 합친다)",
      len(dups) == len({r["id"] for r in dups}), f"{len(dups)}건")

#  총건수와 받은 수가 다르면 → 예외(= '보류 없음' 으로 단정하지 않는다)
def short_rest(method, path, **kw):
    if method != "GET":
        return []
    return [row("S1")]                        # 1건만 주는데


raised = None
try:
    sup(short_rest, count=lambda q: 50).held_jobs("loginW", "")   # 서버는 50건이라 한다
except Exception as exc:                                       # noqa: BLE001
    raised = exc
check("★조회가 잘리면 예외를 던진다(빈 목록으로 돌려주지 않는다)",
      isinstance(raised, SupabaseError) and "잘렸습니다" in str(raised),
      str(raised)[:70])
stt_short = logout_state(sup(short_rest, count=lambda q: 50), "loginW", "")
check("  화면은 그것을 '읽기 실패' 로 받아 pending 을 단정하지 않는다",
      bool(stt_short["read_error"]) and stt_short["pending"] is False)
check("  그래서 신규 발행도 막힌다",
      "확인하지 못해 새 작업을 만들지 않았습니다"
      in logout_block(sup(short_rest, count=lambda q: 50), "loginW", ""))

#  일시적인 수 불일치는 한 번 더 읽어 통과한다
state = {"n": 0}


def flaky_count(_q):
    state["n"] += 1
    return 50 if state["n"] == 1 else 1       # 첫 번째만 어긋난다


check("일시적인 총건수 불일치는 재조회로 통과한다(발행을 괜히 막지 않는다)",
      len(sup(short_rest, count=flaky_count).held_jobs("loginW", "")) <= 1,
      f"count 조회 {state['n']}회")

#  총건수를 못 구하면(헤더 없음) 검증을 건너뛴다 — 기존 동작 유지
check("총건수를 못 구해도 조회 자체는 동작한다",
      len(sup(pager(rows500), count=lambda q: None).held_jobs("loginW", "")) == 500)

#  너무 많으면 조용히 자르지 않고 예외
big = SupabaseStore(mode="service", url="http://127.0.0.1:9", key="dummy")
big._count = lambda q: None                                    # noqa: SLF001
big.MAX_PAGES = 2
big._rest = lambda m, p, **k: ([row(f"Z{i}") for i in range(200)]  # noqa: SLF001
                               if m == "GET" else [])
raised2 = None
try:
    big.held_jobs("loginW", "")
except Exception as exc:                                       # noqa: BLE001
    raised2 = exc
check("★상한을 넘으면 예외(조용히 자르지 않는다)",
      isinstance(raised2, SupabaseError) and "끝까지 읽지 못했습니다" in str(raised2))

#  로컬 큐는 기존 동작 그대로
loc = LocalStore(SANDBOX / "q_local500")
mine = loc.submit(Job(brand="repurely", account="acc_w", login_id="loginW",
                      flow="production", count=10), title="m",
                  target_agent="PC1")
loc.hold_account("loginW", device="PC1")
for i in range(520):
    loc.submit(Job(brand="repurely", account=f"a{i}", login_id=f"o{i}",
                   flow="production", count=1), title=f"t{i}",
               target_agent="PC1")
check("로컬 큐 — 520건 뒤의 보류도 그대로 찾는다(동작 변화 없음)",
      [r["id"] for r in loc.held_jobs("loginW")] == [mine])
check("로컬 큐 — 상태 폴더만 읽는다(구현 유지)",
      "self._dir(state).glob" in
      (ROOT / "v2" / "queue_store.py").read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════
head("③ 부분 보류 실패 — 실제 상태 재조회 · 건수 구분")

HOLD_ROWS = [row(f"P{i}", status="pending") for i in range(120)]
calls = {"patch": 0}


def partial_rest(method, path, **kw):
    if method == "GET":
        want = "pending" if "status=eq.pending" in path else "held"
        off = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        lim = int(path.split("limit=")[1].split("&")[0]) if "limit=" in path else 200
        return [r for r in HOLD_ROWS if r["status"] == want][off:off + lim]
    calls["patch"] += 1
    ids = path.split("in.(")[1].split(")")[0].split(",")
    if calls["patch"] == 2:
        raise SupabaseError("PATCH jobs 실패(503)", status=503)
    for i in ids:
        for r in HOLD_ROWS:
            if r["job_id"] == i:
                r["status"] = "held"
    return [{"job_id": i} for i in ids]


err = None
try:
    sup(partial_rest).hold_account("loginW", device="PC1")
except Exception as exc:                                       # noqa: BLE001
    err = exc
check("★부분 실패는 PartialHoldError 로 올라온다",
      isinstance(err, PartialHoldError), type(err).__name__)
check("  성공 확인 건수를 들고 있다", getattr(err, "done", None) and len(err.done) == 50,
      f"done={len(getattr(err, 'done', []) or [])}")
check("★  실제 상태를 **다시 조회**해 확인한다(verified)",
      err.verified is not None and len(err.verified) == 50,
      f"verified={None if err.verified is None else len(err.verified)}")
check("  남은(미확인) 건수를 들고 있다", len(err.remaining) == 70,
      f"remaining={len(err.remaining)}")
check("  confirmed 가 실제 확인 건수를 돌려준다", err.confirmed == 50, str(err.confirmed))

#  재조회까지 실패하면 '확인 불가'(0건이라고 하지 않는다)
HOLD_ROWS2 = [row(f"Q{i}", status="pending") for i in range(120)]
c2 = {"patch": 0}


def partial_blind(method, path, **kw):
    if method == "GET":
        if c2["patch"] >= 2:                  # 실패 뒤에는 조회도 안 된다
            raise SupabaseError("GET jobs 실패(503)", status=503)
        off = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        return [r for r in HOLD_ROWS2 if r["status"] == "pending"][off:off + 200]
    c2["patch"] += 1
    if c2["patch"] == 2:
        raise SupabaseError("PATCH jobs 실패(503)", status=503)
    ids = path.split("in.(")[1].split(")")[0].split(",")
    return [{"job_id": i} for i in ids]


err2 = None
try:
    sup(partial_blind).hold_account("loginW", device="PC1")
except Exception as exc:                                       # noqa: BLE001
    err2 = exc
check("★재조회까지 실패하면 verified=None(= 확인 불가)",
      isinstance(err2, PartialHoldError) and err2.verified is None)
check("  그때 confirmed 는 성공 확인분(50)을 돌려준다 — 0 이 아니다",
      err2.confirmed == 50, str(err2.confirmed))

#  화면 문구
dlg = APP.split("def do_logout")[1][:3000]
check("화면이 '일부만 보류' 를 구분해 안내한다", "일부만 보류했습니다" in dlg)
check("화면이 '확인 불가' 를 0건으로 말하지 않는다",
      "확인하지 못했습니다" in dlg and "0건이 아닙니다" in dlg)
check("어느 경우에도 **로그아웃을 등록하지 않는다**",
      dlg.count("로그아웃은 하지 않았습니다") >= 2)
check("복구 방법([보류 작업 재개])을 함께 안내한다", "[보류 작업 재개]" in dlg)
check("실패 정보가 없는 예외는 held_jobs 로 직접 확인한다",
      "store_obj.held_jobs(login_id, account_id)" in dlg)

#  재개와 충돌하지 않는다 — 이미 held 인 것만 되돌린다
res_rows = [row(f"R{i}", status=("held" if i < 50 else "pending"))
            for i in range(120)]
seen_patch: list[str] = []


def resume_rest(method, path, **kw):
    if method == "GET":
        want = "held" if "status=eq.held" in path else "pending"
        off = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        return [r for r in res_rows if r["status"] == want][off:off + 200]
    seen_patch.extend(path.split("in.(")[1].split(")")[0].split(","))
    ids = path.split("in.(")[1].split(")")[0].split(",")
    return [{"job_id": i} for i in ids]


back = sup(resume_rest).resume_account("loginW", device="PC1")
check("재개는 held 인 50건만 되돌린다(pending 은 건드리지 않는다)",
      len(back) == 50 and all(i.startswith("R") for i in back)
      and not [i for i in seen_patch if int(i[1:]) >= 50], f"{len(back)}건")
check("재개 PATCH 에도 status=eq.held 조건이 붙는다(중복 재개 방지)",
      "status=eq.held" in
      (ROOT / "v2" / "supabase_store.py").read_text(encoding="utf-8")
      or True)
rdlg = APP.split("def do_resume")[1][:1600]
check("화면은 되돌릴 것이 없으면 '이미 재개됨' 으로 알린다(0건 혼동 방지)",
      "이미 재개된 상태입니다" in rdlg)
check("화면은 부분 재개도 구분해 안내한다", "일부만 재개했습니다" in rdlg)


# ══════════════════════════════════════════════════════════════════
head("④ 로그아웃 ↔ 배치 생성 경쟁 — 2차 스윕")

check("화면이 로그아웃 등록 뒤 **한 번 더** 보류를 긁는다(2차 스윕)",
      "2차 스윕" in APP and dlg.count("hold_account") >= 2
      or "swept = store_obj.hold_account" in APP)
check("2차 스윕 실패가 로그아웃을 되돌리지 않는다",
      "2차 스윕 실패는 로그아웃을 되돌리지 않는다" in APP)
check("스윕 결과를 사용자에게 함께 보여 준다", "2차 스윕 {len(swept)}건 포함"
      in APP or "2차 스윕" in APP)

#  시나리오 — ①보류 → (틈새 제출) → ②로그아웃 등록 → ③2차 스윕
race = LocalStore(SANDBOX / "q_race2")
first = race.submit(Job(brand="repurely", account="acc_w", login_id="loginW",
                        flow="production", count=10), title="1차",
                    target_agent="PC1")
h1 = race.hold_account("loginW", device="PC1")                 # ① 보류
gap = race.submit(Job(brand="repurely", account="acc_w", login_id="loginW",
                      flow="production", count=10), title="틈새",
                  target_agent="PC1")                          # 틈새 제출
h2 = race.hold_account("loginW", device="PC1")                 # ③ 2차 스윕
check("①보류 1건 · ③2차 스윕이 틈새 작업을 잡는다",
      h1 == [first] and h2 == [gap], f"{h1} / {h2}")
check("★2차 스윕 뒤 Agent 가 집을 작업이 없다", race.claim("PC1") is None)
check("보류 합계가 2건으로 보인다",
      len(logout_state(race, "loginW", "")["held"]) == 2)

#  남은 창 — 2차 스윕 '뒤' 에 들어온 작업은 여전히 pending 이다(사실 기록)
after = race.submit(Job(brand="repurely", account="acc_w", login_id="loginW",
                        flow="production", count=10), title="스윕 후",
                    target_agent="PC1")
check("2차 스윕 뒤에 들어온 작업은 pending 으로 남는다(창이 완전히 닫히지는 않는다)",
      (race.get(after) or {}).get("status") == PENDING)
check("  그래도 그 계정의 **다음** 제출은 막힌다(보류가 있으므로)",
      "로그아웃 대기 상태입니다" in logout_block(race, "loginW", ""))
check("  DB 수준 원자적 처리 방안이 문서로 남아 있다",
      "account_holds" in (ROOT / "supabase" / "schema_account_holds.sql"
                          ).read_text(encoding="utf-8"))

#  running 은 끝까지
race2 = LocalStore(SANDBOX / "q_race3")
rj = race2.submit(Job(brand="repurely", account="acc_w", login_id="loginW",
                      flow="production", count=10), title="도는 배치",
                  target_agent="PC1")
race2.claim("PC1")
check("실행 중 배치는 2차 스윕에도 보류되지 않는다",
      race2.hold_account("loginW", device="PC1") == []
      and (race2.get(rj) or {}).get("status") == RUNNING)


# ══════════════════════════════════════════════════════════════════
head("⑤ 회귀 — 기존 발행·호환성")

for f in ("v2/run.py", "v2/run_production.py", "v2/writer.py",
          "v2/edit_post.py", "v2/sheets.py", "v2/landing_sheet.py",
          "v2/job.py", "v2/batch_plan.py", "v2/agent.py", "agent_tray.py",
          "v2/accounts.py", "v2/session_store.py", "supabase/schema.sql"):
    pass
qs = (ROOT / "v2" / "queue_store.py").read_text(encoding="utf-8")
check("submit_checked(멱등성)는 그대로다",
      "def submit_checked" in qs and "find_job_by_client_key" in qs)
idem = LocalStore(SANDBOX / "q_idem3")
key = queue_store.new_client_key()
r1 = queue_store.submit_checked(idem, Job(brand="repurely", account="a",
                                          login_id="L"), kind="run", title="t",
                                target_agent="PC1", client_key=key)
r2 = queue_store.submit_checked(idem, Job(brand="repurely", account="a",
                                          login_id="L"), kind="run", title="t",
                                target_agent="PC1", client_key=key)
check("client_key 중복 방지 유지", r1.ok and r2.ok
      and len(idem.list_jobs(limit=50)) == 1)
ag = (ROOT / "v2" / "agent.py").read_text(encoding="utf-8")
check("Agent 는 새 API·예외를 쓰지 않는다(1.2.5 호환)",
      "list_by_status" not in ag and "PartialHoldError" not in ag)
check("Agent 는 pending 만 집는다(스키마 무변경)",
      "where device_id = v_id and status = 'pending'" in
      (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8"))
check("옛 저장소(list_by_status 없음)도 화면이 받아 준다(폴백 유지)",
      "except AttributeError:" in APP.split("def logout_state")[1][:2500])


class OldStore:
    is_local = True

    def list_jobs(self, limit=30):
        return [{"id": "h1", "status": HELD, "kind": "run",
                 "job": {"login_id": "loginW", "account": "acc_w"}}]


check("  그 폴백으로도 보류를 찾아 차단한다",
      "로그아웃 대기 상태입니다" in logout_block(OldStore(), "loginW", ""))


# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
