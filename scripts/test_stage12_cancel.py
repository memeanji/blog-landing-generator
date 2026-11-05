r"""Stage 12 검증 — 작업 중단 개선 (2026-11-05)

    .\.venv\Scripts\python.exe scripts\test_stage12_cancel.py

★브라우저·네이버·시트·운영 큐·운영 DB·운영 계정/세션을 건드리지 않는다.
  임시 폴더의 LocalStore 와, 실제 HTTP 를 못 내는 가짜 SupabaseStore 만 쓴다.
  실제 발행·보류·재개·로그아웃·취소 없음.

무엇을 고쳤고 무엇을 검증하나
  ① 진행 중 작업을 **화면 맨 위**에 — `session_state` 가 아니라 큐를 보고 그린다
     (새로고침·재접속·다른 탭에서도 중단 버튼이 보인다)
  ② 권한 — **이 화면에서 고른 PC** 의 작업만 중단할 수 있다
  ③ Agent 가 집은 직후 **프로세스를 띄우기 전에** 취소를 한 번 본다
  ④ 대기 작업은 **조건부 상태 변경**으로 취소한다(경합에서 한쪽만 이긴다)
  ⑤ '중단 요청' 과 '중단 완료' 를 화면에서 구분한다
  ⑥ 기존 보류·재개·멱등성(중복 발행 방지)에 영향이 없다
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage12_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ["BLOG_QUEUE_BACKEND"] = "local"
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from v2 import queue_store                                    # noqa: E402
from v2.job import Job                                        # noqa: E402
from v2.queue_store import (CANCELED, HELD, LocalStore,       # noqa: E402
                            PENDING, RUNNING)
from v2.supabase_store import SupabaseError, SupabaseStore    # noqa: E402

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


# ── 화면 코드에서 필요한 함수만 꺼내 온다(streamlit 없이) ──────────
APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)
NS: dict = {"queue_store": queue_store, "time": __import__("time"), "chr": chr,
            "RUNNING_STATES": (PENDING, RUNNING)}
for _n in TREE.body:
    if isinstance(_n, ast.Assign) and len(_n.targets) == 1:
        _tg = _n.targets[0]
        _names = ([getattr(e, "id", "") for e in _tg.elts]
                  if isinstance(_tg, ast.Tuple) else [getattr(_tg, "id", "")])
        if _names and all(x.isupper() for x in _names if x):
            try:
                _vals = ast.literal_eval(_n.value)
            except Exception:                                  # noqa: BLE001
                continue
            if isinstance(_tg, ast.Tuple):
                _vals = _vals if isinstance(_vals, tuple) else (_vals,)
                for _k, _v in zip(_names, _vals):
                    NS[_k] = _v
            else:
                NS[_names[0]] = _vals


class FakeSt:
    """화면 호출을 삼키는 가짜 — 오류 문구만 모아 둔다."""

    def __init__(self):
        self.session_state = {}
        self.errors: list[str] = []

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def __getattr__(self, _name):
        return lambda *a, **k: None


NS["st"] = FakeSt()
#  ★`BADGE` 는 값에 `queue_store.PENDING` 을 쓰므로 literal_eval 로 못 읽는다.
#    그 대입문만 **실제로 실행**해 넣는다(화면과 같은 표를 쓰기 위해서다).
for _n in TREE.body:
    if (isinstance(_n, ast.Assign) and len(_n.targets) == 1
            and getattr(_n.targets[0], "id", "") == "BADGE"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)
for _n in TREE.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
            "cancel_job", "cancel_guarded", "active_jobs", "active_line"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)
if "BADGE" not in NS:                      # 화면에서 이름이 바뀌면 바로 드러나게
    raise SystemExit("BADGE 를 화면에서 찾지 못했습니다 — 테스트를 맞춰야 합니다")

cancel_job = NS["cancel_job"]
cancel_guarded = NS["cancel_guarded"]
active_jobs = NS["active_jobs"]
active_line = NS["active_line"]
DONE_, ASKED_, FAIL_ = NS["CANCEL_DONE"], NS["CANCEL_ASKED"], NS["CANCEL_FAIL"]


def store() -> LocalStore:
    d = Path(tempfile.mkdtemp(prefix="q_", dir=SANDBOX))
    return LocalStore(d)


def put(st_, *, account="acc_a", dev="PC1", kind="run", title="글 3건") -> str:
    job = Job(brand="repurely", account=account, account_name="민지",
              login_id="", ref_tab="t", flow="production", count=3)
    return st_.submit(job, kind=kind, title=title, target_agent=dev)


def rec(jid, status=RUNNING, dev="PC1", asked=False, total=0, made=0,
        pub=(), title="글 3건", account="acc_a"):
    return {"id": jid, "status": status, "target_agent": dev, "title": title,
            "cancel_requested": asked, "total": total, "made": made,
            "published": list(pub), "created_at": "2026-11-05T16:00:00",
            "job": {"account": account, "account_name": "민지"}}


# ══════════════════════════════════════════════════════════════════
head("1. 대기 작업 취소 — 조건부라 '확정' 이라고 말할 수 있다")

s = store()
j1 = put(s)
check("대기 작업을 취소하면 True(확정)", s.cancel_pending(j1) is True)
g1 = s.get(j1)
check("  상태가 canceled 다", (g1 or {}).get("status") == CANCELED,
      str((g1 or {}).get("status")))
check("  종료코드 130 이 남는다", (g1 or {}).get("exit_code") == 130)
check("  cancel_requested 표시가 남는다", bool((g1 or {}).get("cancel_requested")))
check("  pending 폴더에서 사라졌다",
      not (s.root / "pending" / f"{j1}.json").exists())
check("  두 번째 호출은 False(이미 끝났다)", s.cancel_pending(j1) is False)
check("없는 작업은 False", s.cancel_pending("no-such-job") is False)

#  ★경합 — Agent 가 먼저 집으면 취소는 져야 한다
s2 = store()
j2 = put(s2)
claimed = s2.claim("PC1")
check("Agent 가 집은 뒤에는 취소가 False(졌다)", s2.cancel_pending(j2) is False)
check("  집힌 작업은 running 그대로다",
      (s2.get(j2) or {}).get("status") == RUNNING,
      str((s2.get(j2) or {}).get("status")))
check("  집은 레코드는 정상이다", (claimed or {}).get("id") == j2)

#  ★보류(held)는 취소 대상이 아니다 — 재개로 되살릴 길을 막지 않는다
s3 = store()
j3 = put(s3)
s3.hold_account("", account_id="acc_a")
check("보류 중 작업은 cancel_pending 이 건드리지 않는다",
      s3.cancel_pending(j3) is False)
check("  보류 상태가 그대로다", (s3.get(j3) or {}).get("status") == HELD,
      str((s3.get(j3) or {}).get("status")))

#  ★이미 발행된 글은 지우지 않는다
s4 = store()
j4 = put(s4)
s4.update(j4, published=["https://blog.naver.com/x/1", "https://blog.naver.com/x/2"])
s4.cancel_pending(j4)
check("취소해도 이미 발행된 URL 은 레코드에 남는다",
      len((s4.get(j4) or {}).get("published") or []) == 2)


# ══════════════════════════════════════════════════════════════════
head("2. '요청' 과 '완료' 를 구분한다")

s5 = store()
j5 = put(s5)
s5.claim("PC1")                      # running 으로
s5.request_cancel(j5)
check("요청만 보낸 작업은 아직 running 이다",
      (s5.get(j5) or {}).get("status") == RUNNING)
check("  get() 에 cancel_requested=True 가 실린다",
      bool((s5.get(j5) or {}).get("cancel_requested")))
check("  list_jobs() 에도 실린다",
      any(r.get("id") == j5 and r.get("cancel_requested")
          for r in s5.list_jobs(limit=20)))
check("  cancel_requested() 도 True", s5.cancel_requested(j5) is True)

line = active_line(rec(j5, status=RUNNING, asked=True))
check("배너가 '중단 요청됨' 으로 표시한다", "중단 요청됨" in line, line[:60])
line2 = active_line(rec(j5, status=RUNNING, asked=False))
check("  요청 전에는 그 문구가 없다", "중단 요청됨" not in line2)
check("  계정 이름이 보인다", "민지" in line2)
line3 = active_line(rec(j5, status=RUNNING, total=3, made=2, pub=["u"]))
check("  진행 수치가 보인다(작성/발행)",
      "작성 2/3" in line3 and "발행 1/3" in line3, line3[:80])


# ══════════════════════════════════════════════════════════════════
head("3. 3상태 — 대기는 확정, 실행 중은 요청, 못 보내면 실패")

s6 = store()
j6 = put(s6)
check("대기 작업 → CANCEL_DONE(확정)", cancel_job(s6, j6) == DONE_)

s7 = store()
j7 = put(s7)
s7.claim("PC1")
check("실행 중 작업 → CANCEL_ASKED(요청)", cancel_job(s7, j7) == ASKED_)
check("  요청이 실제로 남았다", s7.cancel_requested(j7) is True)

check("빈 job_id → CANCEL_FAIL", cancel_job(s6, "") == FAIL_)


class Broken:
    """조건부 취소도, 요청도 못 하는 저장소."""

    def cancel_pending(self, _j):
        raise RuntimeError("조건부 취소 불가")

    def request_cancel(self, _j):
        raise RuntimeError("네트워크 끊김")


NS["st"].errors.clear()
check("둘 다 실패하면 CANCEL_FAIL", cancel_job(Broken(), "J") == FAIL_)
check("  화면에 '계속 돌고 있을 수 있습니다' 를 알린다",
      any("계속 돌고" in e for e in NS["st"].errors),
      str(NS["st"].errors)[:70])


class OnlyAsk:
    """조건부 취소는 못 하지만 요청은 되는 저장소(예전 백엔드와 같다)."""

    def __init__(self):
        self.asked: list[str] = []

    def cancel_pending(self, _j):
        raise RuntimeError("지원 안 함")

    def request_cancel(self, j):
        self.asked.append(j)


oa = OnlyAsk()
check("조건부 취소를 못 해도 요청으로 내려간다(예전 동작 보존)",
      cancel_job(oa, "J9") == ASKED_ and oa.asked == ["J9"])

base = queue_store.JobStore
check("기본 구현은 아무것도 하지 않고 False(안전한 기본값)",
      base.cancel_pending(object(), "x") is False)

#  예전 호출부(배치 전체 중단 등)가 그대로 돈다
s8 = store()
j8 = put(s8)
check("cancel_guarded 는 예전처럼 True/False 를 준다",
      cancel_guarded(s8, j8) is True and cancel_guarded(Broken(), "J") is False)


# ══════════════════════════════════════════════════════════════════
head("4. 배너 — 무엇을 보여 주고 무엇을 잠그나")

rows = [
    rec("A", status=RUNNING, dev="PC1"),
    rec("B", status=PENDING, dev="PC1"),
    rec("C", status=RUNNING, dev="PC2"),          # 다른 PC
    rec("D", status=PENDING, dev=""),             # 대상 미지정
    rec("E", status="done", dev="PC1"),
    rec("F", status="failed", dev="PC1"),
    rec("G", status=HELD, dev="PC1"),
]
mine, others = active_jobs(rows, "PC1")
ids = [r["id"] for r in mine]
check("내 PC 의 진행 중·대기만 고른다", ids == ["A", "B"], str(ids))
check("  끝난 작업(done/failed)은 안 나온다",
      not any(r["id"] in ("E", "F") for r in mine + others))
check("  ★보류(held)는 안 나온다 — 중단 대상이 아니다",
      not any(r["id"] == "G" for r in mine + others))
oid = [r["id"] for r in others]
check("다른 PC 작업은 '다른 PC' 로 간다", "C" in oid, str(oid))
check("  ★대상이 비어 있으면 내 것으로 보지 않는다(권한 보수적)", "D" in oid)
check("  실행 중이 대기보다 먼저 온다", ids[0] == "A")

m2, o2 = active_jobs(rows, "")
check("고른 PC 가 없으면 중단 가능 목록이 비어 있다", m2 == [] and len(o2) == 4,
      f"mine={len(m2)} others={len(o2)}")
check("빈 목록이면 둘 다 비어 있다", active_jobs([], "PC1") == ([], []))
check("None 이어도 터지지 않는다", active_jobs(None, "PC1") == ([], []))


# ══════════════════════════════════════════════════════════════════
head("5. Agent — 집은 직후, 띄우기 전에 취소를 본다")

AG = (ROOT / "v2" / "agent.py").read_text(encoding="utf-8")
atree = ast.parse(AG)
fn = next((n for n in ast.walk(atree)
           if isinstance(n, ast.FunctionDef) and n.name == "run_record"), None)
check("run_record 를 찾았다", fn is not None)


def _line_of(pred) -> int:
    for node in ast.walk(fn):
        if pred(node):
            return node.lineno
    return 10 ** 9


cancel_at = _line_of(
    lambda n: isinstance(n, ast.Call)
    and isinstance(n.func, ast.Attribute) and n.func.attr == "cancel_requested")
runner_at = _line_of(
    lambda n: isinstance(n, ast.Call)
    and isinstance(n.func, ast.Name) and n.func.id == "Runner")
start_at = _line_of(
    lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    and n.func.attr in ("start", "start_module"))
check("★취소 확인이 Runner 생성보다 **앞**이다", cancel_at < runner_at,
      f"cancel={cancel_at} Runner={runner_at}")
check("★취소 확인이 프로세스 시작보다 **앞**이다", cancel_at < start_at,
      f"cancel={cancel_at} start={start_at}")
seg = AG[:AG.index("runner = Runner(")]
check("  실행 전 분기에서 CANCELED 로 끝낸다", "CANCELED, 130" in seg)
check("  로그를 남긴다", "실행하지 않습니다" in seg)
check("  실행 중 0.4초 확인도 그대로 있다(이중 안전장치)",
      AG.count("cancel_requested") >= 2, str(AG.count("cancel_requested")))
check("  멈출 때 프로세스 트리를 통째로 정리한다(크로미움 포함)",
      "taskkill" in (ROOT / "v2" / "runner.py").read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════
head("6. Supabase — 조건부 PATCH 여야 한다(경합 방지)")


def sup(rest):
    s_ = SupabaseStore(mode="service", url="http://127.0.0.1:9", key="dummy")
    s_._rest = rest                                            # noqa: SLF001
    return s_


seen: list[tuple] = []


def rest_ok(method, path, **kw):
    seen.append((method, path, kw.get("json"), (kw.get("headers") or {})))
    return [{"job_id": "J1"}]


sp = sup(rest_ok)
check("대기 작업 취소가 True 를 준다", sp.cancel_pending("J1") is True)
m, p, body, hdr = seen[-1]
check("  PATCH 다", m == "PATCH", m)
check("  ★status=eq.pending 조건이 붙는다", "status=eq.pending" in p, p)
check("  그 작업만 고른다(job_id=eq.)", "job_id=eq.J1" in p, p)
check("  canceled 로 바꾼다", (body or {}).get("status") == CANCELED, str(body))
check("  요청 표시도 함께 남긴다", (body or {}).get("cancel_requested") is True)
check("  ★바뀐 행을 돌려받는다(return=representation)",
      "representation" in str(hdr), str(hdr))

sp0 = sup(lambda *a, **k: [])
check("0행이 돌아오면 False — '취소됨' 이라고 말하지 않는다",
      sp0.cancel_pending("J1") is False)
sp_none = sup(lambda *a, **k: None)
check("  응답이 None 이어도 False", sp_none.cancel_pending("J1") is False)
check("빈 job_id 는 요청을 보내지 않는다", sp0.cancel_pending("") is False)

dev = SupabaseStore(mode="device", url="http://127.0.0.1:9", key="dummy",
                    device_token="dummy-token")   # 가짜 — 네트워크를 내지 않는다
raised = None
try:
    dev.cancel_pending("J1")
except SupabaseError as exc:
    raised = exc
check("★Agent(device 모드)는 대기 작업을 취소할 수 없다", raised is not None,
      str(raised)[:50])
check("  그 실패는 '확정 실패' 다(재시도해도 소용없다)",
      bool(getattr(raised, "definite_failure", False)))

#  _rec 이 cancel_requested 를 실어 준다
r = SupabaseStore._rec({"job_id": "J", "status": "running",
                        "cancel_requested": True})
check("_rec 이 cancel_requested 를 실어 준다", r.get("cancel_requested") is True)
r2 = SupabaseStore._rec({"job_id": "J", "status": "running"})
check("  열이 없으면 False(없는 것을 True 로 만들지 않는다)",
      r2.get("cancel_requested") is False)


# ══════════════════════════════════════════════════════════════════
head("7. 기존 기능에 영향이 없다")

SRC_Q = (ROOT / "v2" / "supabase_store.py").read_text(encoding="utf-8")
check("보류/재개는 _switch_status 를 그대로 쓴다",
      "return self._switch_status(login_id, PENDING, HELD" in SRC_Q
      and "return self._switch_status(login_id, HELD, PENDING" in SRC_Q)
check("  보류 조건부 PATCH(status=eq.<frm>)도 그대로다",
      "&status=eq.{quote(frm, safe='')}" in SRC_Q)

#  멱등성(중복 발행 방지) — client_key 가 그대로 동작한다
s9 = store()
job = Job(brand="repurely", account="acc_a", account_name="민지", login_id="",
          ref_tab="t", flow="production", count=1)
k = "same-key-1"
r1 = queue_store.submit_checked(s9, job, kind="run", title="t",
                                target_agent="PC1", client_key=k)
r2 = queue_store.submit_checked(s9, job, kind="run", title="t",
                                target_agent="PC1", client_key=k)
check("client_key 중복 방지가 그대로 돈다",
      r1.ok and r2.ok and len(s9.list_jobs(limit=50)) == 1,
      str(len(s9.list_jobs(limit=50))))

#  취소한 뒤에도 같은 키로 다시 넣을 수 있어야 복구가 된다
s10 = store()
r3 = queue_store.submit_checked(s10, job, kind="run", title="t",
                                target_agent="PC1", client_key="k2")
s10.cancel_pending(r3.job_id)
check("취소된 작업이 finished 로 남는다",
      (s10.get(r3.job_id) or {}).get("status") == CANCELED)

#  조회 한 번으로 배너와 기록이 같이 돈다(요청 수를 늘리지 않았다)
#  ★'최근 실행 기록' 은 최신 20건 한 번만 조회한다(그 안에서 다시 부르지 않는다).
check("★'최근 실행 기록' 목록을 받는 곳이 한 곳뿐이다",
      APP.count("jobs = store.list_jobs(") == 1,
      str(APP.count("jobs = store.list_jobs(")))
_rb = APP[APP.index("with right:"):]
_rb = _rb[:_rb.index("# ══", 10)]
check("  '최근 실행 기록' 은 다시 조회하지 않는다", "list_jobs" not in _rb)
#  ★`render_active_banner(` 의 첫 등장은 **함수 정의**다 — 호출부와 비교해야 한다.
check("  배너 호출보다 조회가 먼저 온다",
      APP.index("active = (store.list_active(")
      < APP.index("render_active_banner(store, active"))


head("8. ★최신 N건 창에 밀려 진행 중 작업을 놓치지 않는다")

#  재현 — 오래 도는 작업 1건 + 그 뒤에 들어온 새 작업 25건
s11 = store()
old_id = put(s11)
s11.claim("PC1")                                  # running (오래 돈다)
for _i in range(25):
    put(s11)
win20, _ = active_jobs(s11.list_jobs(limit=20), "PC1")
act, _ = active_jobs(s11.list_active(limit=100), "PC1")
pend_ids = [r["id"] for r in s11.list_active(limit=100)
            if r.get("status") == PENDING]
check("최신 20건 창으로는 진행 중·대기를 다 못 본다(그래서 바꿨다)",
      len(win20) < 26, f"{len(win20)}/26")
check("★list_active 는 진행 중·대기를 **전부** 본다", len(act) == 26, str(len(act)))
check("  오래 도는 running 이 들어온다",
      old_id in [r["id"] for r in act])
check("  ★다음에 실행될 '가장 오래된 대기' 가 들어온다",
      pend_ids and pend_ids[-1] in [r["id"] for r in act])
check("  끝난 작업은 섞이지 않는다",
      all(r.get("status") in (PENDING, RUNNING) for r in act))
check("  실행 중이 대기보다 먼저 온다",
      (s11.list_active(limit=100)[0] or {}).get("status") == RUNNING)

#  보류는 여전히 빠진다
s12 = store()
j12 = put(s12)
s12.hold_account("", account_id="acc_a")
check("보류(held)는 list_active 에 안 나온다",
      not any(r.get("id") == j12 for r in s12.list_active(limit=100)))

#  기본 구현(이 메서드가 없는 저장소)도 상태로 거른다
class OnlyList:
    def __init__(self, rows): self.rows = rows
    def list_jobs(self, limit=30): return self.rows[:limit]


base_rows = [rec("X", status=RUNNING), rec("Y", status=PENDING),
             rec("Z", status="done"), rec("W", status=HELD)]
got = queue_store.JobStore.list_active(OnlyList(base_rows), 100)
check("기본 구현도 진행 중·대기만 고른다",
      [r["id"] for r in got] == ["X", "Y"], str([r["id"] for r in got]))

#  Supabase — 상태 필터가 서버에서 걸리는지
#  ★`_page_all` 은 `_rest` 가 아니라 `_rest_range` 를 부른다 — 그쪽을 막아야
#    실제 HTTP 가 나가지 않는다(처음에 `_rest` 만 막았다가 연결 시도가 났다).
qseen: list[str] = []

spa = SupabaseStore(mode="service", url="http://127.0.0.1:9", key="dummy")
spa._rest_range = lambda q, count=False, timeout=None: (      # noqa: SLF001
    qseen.append(q) or ([], 0))
spa.list_active(limit=50)
check("★Supabase 가 status=in.(pending,running) 으로 좁힌다",
      any("status=in." in q and "pending" in q and "running" in q
          for q in qseen), str(qseen[:1]))
check("  정렬키를 고정한다(created_at.desc,job_id.desc)",
      any("order=created_at.desc,job_id.desc" in q for q in qseen))
check("  deadline 을 받는다(렌더 예산 안에서 끊긴다)",
      "deadline" in __import__("inspect").signature(
          SupabaseStore.list_active).parameters)
check("  조회 실패를 '진행 중 없음' 으로 바꾸지 않는다",
      "확인하지 못했습니다" in APP)
check("  배너는 세션 상태가 아니라 큐를 본다",
      "render_active_banner(store, active" in APP)

print("")
print("=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
