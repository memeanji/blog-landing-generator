r"""배치 계획 시뮬레이션 — 25건이 10+10+5 로 **순차** 처리되는지 확인한다.

    .\.venv\Scripts\python.exe scripts\test_batch_plan.py

★실제 큐/에이전트/브라우저를 쓰지 않는다. 큐 상태를 흉내 내어 상태 전이만 검증한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from v2 import batch_plan as bp                                # noqa: E402
from v2.job import Job                                         # noqa: E402

OK, NG = "  ✅", "  ❌"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        fails.append(name)


class FakeQueue:
    """큐 흉내 — submit 하면 pending, finish() 로 상태를 바꾼다."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.n = 0

    def submit(self, job_dict: dict, title: str = "") -> str:
        self.n += 1
        jid = f"job{self.n}"
        self.jobs[jid] = {"id": jid, "status": "pending", "title": title,
                          "job": job_dict, "published": []}
        self.order.append(jid)
        return jid

    def status(self, jid: str):
        rec = self.jobs.get(jid) or {}
        return rec.get("status", ""), rec

    def finish(self, jid: str, status: str = "done", published: int = 0) -> None:
        rec = self.jobs[jid]
        rec["status"] = status
        rec["published"] = [f"url{i}" for i in range(published)]

    @property
    def open_jobs(self) -> list[str]:
        return [j for j, r in self.jobs.items() if r["status"] == "pending"]


def run_plan(total: int, flow: str = "production", mode: str = "convert"):
    q = FakeQueue()
    base = Job(flow=flow, brand="repurely", account="test_account", media="카모",
               deficiency="목 / 지연", kind="실전용", date="831",
               mode=mode, publish=True, events=True).to_dict()
    plan = bp.create(base, total, title="테스트", brand="repurely",
                     account="test_account", flow=flow, mode=mode)
    return q, plan


print("1) 분할")
for total, want in ((25, [10, 10, 5]), (23, [10, 10, 3]), (8, [8]),
                    (10, [10]), (20, [10, 10]), (1, [1])):
    check(f"{total}건 → {want}", bp.split(total) == want, str(bp.split(total)))

print()
print("2) 25건 순차 진행 (production · convert)")
q, plan = run_plan(25)
check("배치 3개 · 10+10+5", [b["size"] for b in plan["batches"]] == [10, 10, 5])
check("첫 배치만 대기 없이 시작(pending) · 나머지는 로그인 대기",
      [b["status"] for b in plan["batches"]]
      == [bp.PENDING, bp.LOGIN_WAIT, bp.LOGIN_WAIT])
check("convert 는 start 가 이어진다", [b["start"] for b in plan["batches"]] == [1, 11, 21])


def advance(plan):
    plan, changed = bp.advance(
        plan, job_status=q.status,
        submit_run=lambda b: q.submit(bp.job_for(plan, b), f"배치 {b['no']}"))
    return plan


# ── 배치 1 : 로그인 없이 바로 실행 ────────────────────────────────
plan = advance(plan)
b1 = plan["batches"][0]
check("배치 1 이 큐에 올라간다", b1["status"] == bp.RUNNING and b1["run_job"])
check("배치 1 건수 = 10", q.jobs[b1["run_job"]]["job"]["count"] == 10)
check("배치 1 start = 1", q.jobs[b1["run_job"]]["job"]["start"] == 1)
check("큐에 올라간 작업은 하나뿐(동시 실행 없음)", len(q.open_jobs) == 1,
      str(q.open_jobs))

plan = advance(plan)          # 아직 안 끝났으면 아무 일도 없어야 한다
check("끝나기 전에는 다음 배치를 시작하지 않는다",
      len(q.open_jobs) == 1 and plan["index"] == 0)

# ── 배치 1 완료 → 배치 2 는 '로그인 대기' 에서 멈춘다 ─────────────
q.finish(b1["run_job"], "done", published=10)
plan = advance(plan)
check("배치 1 완료 처리", plan["batches"][0]["status"] == bp.DONE
      and plan["batches"][0]["published"] == 10)
check("index 가 배치 2 로", plan["index"] == 1)
cur = bp.current(plan)
check("배치 2 는 로그인 대기 — 자동으로 시작하지 않는다",
      cur["status"] == bp.LOGIN_WAIT)
plan = advance(plan)
check("로그인 전에는 큐에 아무것도 안 넣는다", len(q.jobs) == 1, str(list(q.jobs)))
check("완료 건수 집계", bp.done_count(plan) == 10)

# ── 사용자가 로그인 → 배치 2 이어서 실행 ─────────────────────────
login2 = q.submit({"kind": "session"}, "로그인")
bp.mark_logging_in(plan, login2)
check("로그인 대기 → 로그인 중", bp.current(plan)["status"] == bp.LOGGING_IN)
plan = advance(plan)
check("로그인이 끝나기 전에는 실행하지 않는다", len(q.jobs) == 2)
q.finish(login2, "done")
plan = advance(plan)
cur = bp.current(plan)
check("로그인 완료 → 배치 2 자동 실행", cur["status"] == bp.RUNNING and cur["run_job"])
check("배치 2 건수 = 10 · start = 11",
      q.jobs[cur["run_job"]]["job"]["count"] == 10
      and q.jobs[cur["run_job"]]["job"]["start"] == 11)

# ── 배치 3 ───────────────────────────────────────────────────────
q.finish(cur["run_job"], "done", published=10)
plan = advance(plan)
check("배치 3 도 로그인 대기", bp.current(plan)["status"] == bp.LOGIN_WAIT)
login3 = q.submit({"kind": "session"}, "로그인")
bp.mark_logging_in(plan, login3)
q.finish(login3, "done")
plan = advance(plan)
cur = bp.current(plan)
check("배치 3 건수 = 5 · start = 21",
      q.jobs[cur["run_job"]]["job"]["count"] == 5
      and q.jobs[cur["run_job"]]["job"]["start"] == 21)
q.finish(cur["run_job"], "done", published=5)
plan = advance(plan)
check("계획 완료", plan["status"] == bp.DONE and bp.done_count(plan) == 25)
check("총 큐 작업 = 실행 3 + 로그인 2", len(q.jobs) == 5, str(len(q.jobs)))

print()
print("3) 실패·중단")
q, plan = run_plan(25)
plan = advance(plan)
q.finish(bp.current(plan)["run_job"], "failed")
plan = advance(plan)
# ★공통 오류로 보고 '복구 대기' 로 멈춘다(계획을 통째로 실패시키지 않는다).
#   행 단위 실패만 있었던 경우는 exit 3 으로 구분한다(아래 5번 참고).
check("배치가 멈추면 복구 대기", bp.current(plan)["status"] == bp.RECOVER_WAIT)
plan = advance(plan)
check("멈춘 뒤에는 아무것도 큐에 넣지 않는다", len(q.jobs) == 1)

q, plan = run_plan(25)
plan = advance(plan)
login = q.submit({"kind": "session"}, "로그인")
bp.current(plan)["status"] = bp.LOGGING_IN
bp.current(plan)["login_job"] = login
q.finish(login, "canceled")
plan = advance(plan)
check("로그인이 취소되면 다시 로그인 대기로",
      bp.current(plan)["status"] == bp.LOGIN_WAIT)

q, plan = run_plan(8)
check("8건은 배치 하나", [b["size"] for b in plan["batches"]] == [8])
bp.cancel(plan)
check("중단하면 열린 배치도 닫힌다", plan["status"] == bp.CANCELED)

print()
print("4) 검수용(create/review)은 start 를 올리지 않는다")
_q, rplan = run_plan(25, flow="review", mode="convert")
check("review start 는 전부 1", [b["start"] for b in rplan["batches"]] == [1, 1, 1])
_q, cplan = run_plan(25, flow="production", mode="create")
check("create start 도 전부 1", [b["start"] for b in cplan["batches"]] == [1, 1, 1])


print()
print("5) 행 단위 실패 vs 공통 오류")
from v2.run_production import is_common_error                   # noqa: E402

row_errs = [RuntimeError("[정리] 참고글 제품 카드/이미지를 지우지 못했습니다"),
            RuntimeError("[검증] 이미지 4/5"),
            TimeoutError("Timeout 30000ms exceeded")]
common_errs = [RuntimeError("Target page, context or browser has been closed"),
               RuntimeError("[시트] stage=utm_sheet_access reason=permission_denied"),
               RuntimeError("선택한 계정(스마일 현미)과 실제 로그인된 계정이 다릅니다"),
               RuntimeError("로그인 확인 실패 — 2단계 인증")]
check("행 단위 오류는 공통으로 보지 않는다",
      not any(is_common_error(e) for e in row_errs))
check("로그인·브라우저·시트 오류는 공통으로 본다",
      all(is_common_error(e) for e in common_errs))

# exit 3(일부 실패) → 배치는 끝난 것으로 보고 다음 배치로 간다
q, plan = run_plan(25)
plan = advance(plan)
cur = bp.current(plan)
q.finish(cur["run_job"], "failed", published=9)
q.jobs[cur["run_job"]]["exit_code"] = bp.EXIT_PARTIAL
plan = advance(plan)
check("행 단위 실패(exit 3)여도 배치는 완료로 넘어간다",
      plan["batches"][0]["status"] == bp.DONE and plan["index"] == 1)
check("다음 배치는 로그인 대기(계획은 계속)",
      bp.current(plan)["status"] == bp.LOGIN_WAIT
      and plan["status"] == bp.RUNNING)

# 공통 오류(exit 1) → 복구 대기
q, plan = run_plan(25)
plan = advance(plan)
cur = bp.current(plan)
q.finish(cur["run_job"], "failed", published=4)
q.jobs[cur["run_job"]]["exit_code"] = 1
q.jobs[cur["run_job"]]["error"] = "RuntimeError: Target page ... closed"
plan = advance(plan)
cur = bp.current(plan)
check("공통 오류는 복구 대기로 멈춘다", cur["status"] == bp.RECOVER_WAIT)
check("계획은 실패로 끝내지 않고 열려 있다", bp.is_open(plan))
plan = advance(plan)
check("복구 대기에서는 큐에 아무것도 안 넣는다", len(q.jobs) == 1)

bp.prepare_retry(plan)
cur = bp.current(plan)
check("재시도는 이미 발행된 4건을 건너뛴다",
      cur["size"] == 6 and cur["start"] == 5, f"size={cur['size']} start={cur['start']}")
check("재시도 전 로그인 대기로 돌아간다", cur["status"] == bp.LOGIN_WAIT)

# 사용자 중단(130)
q, plan = run_plan(25)
plan = advance(plan)
cur = bp.current(plan)
q.finish(cur["run_job"], "canceled")
q.jobs[cur["run_job"]]["exit_code"] = bp.EXIT_STOPPED
plan = advance(plan)
check("사용자 중단은 계획도 중단", plan["status"] == bp.CANCELED)

print()
print("6) 실패 건만 재실행")
from v2.job import Job as _Job                                   # noqa: E402

j = _Job(flow="production", brand="repurely", media="카모", deficiency="목 / 지연",
         kind="실전용", date="831", rows="812,815,820").to_argv()
check("--rows 가 전달된다",
      "--rows" in j and j[j.index("--rows") + 1] == "812,815,820", str(j))

# plan_results — 같은 행이 재실행돼 성공하면 실패 목록에서 빠진다
src = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
block = src[src.index("def plan_results"):src.index("def advance_plan")]
ns: dict = {}
exec(compile(block, "plan_results", "exec"), ns)


class FakeStore:
    def __init__(self, per_job):
        self.per_job = per_job

    def read_events(self, jid):
        return self.per_job.get(jid, []), 0


ev1 = [{"event": "published", "no": 1, "row": 811, "seq": "1", "campaign": "c_1",
        "url": "https://blog/1"},
       {"event": "post_failed", "no": 2, "row": 812, "seq": "2", "campaign": "c_2",
        "stage": "product_link_verify", "reason": "verify_failed", "scope": "row",
        "error": "RuntimeError: 검증 실패"}]
ev2 = [{"event": "published", "no": 1, "row": 812, "seq": "2", "campaign": "c_2",
        "url": "https://blog/2"}]
fake_plan = {"batches": [{"no": 1, "run_job": "j1"}, {"no": 2, "run_job": "j2"}],
             "total": 2}
res = ns["plan_results"](FakeStore({"j1": ev1, "j2": ev2}), fake_plan)
check("성공 1 + 실패 1 집계(재실행 전)",
      ns["plan_results"](FakeStore({"j1": ev1}), {"batches": [{"no": 1,
                                                              "run_job": "j1"}]})
      ["fail"] == 1)
check("재실행으로 성공한 행은 실패 목록에서 빠진다",
      res["fail"] == 0 and res["ok"] == 2, str(res["fails"]))
res2 = ns["plan_results"](FakeStore({"j1": ev1}), {"batches": [{"no": 1,
                                                               "run_job": "j1"}]})
check("실패 목록에 순번·행·단계·사유가 담긴다",
      res2["fails"][0]["순번"] == "2" and res2["fails"][0]["행"] == 812
      and res2["fails"][0]["단계"] == "product_link_verify"
      and res2["fails"][0]["사유"] == "verify_failed", str(res2["fails"][0]))
check("재실행 대상은 실패한 행 번호뿐", res2["failed_rows"] == ["812"],
      str(res2["failed_rows"]))

# 테스트가 만든 계획 파일 정리
for f in bp.PLANS_DIR.glob("*.json"):
    try:
        f.unlink()
    except Exception:                                          # noqa: BLE001
        pass

print()
if fails:
    print(f"실패 {len(fails)}건: {fails}")
    sys.exit(1)
print("전부 통과 ✅")
