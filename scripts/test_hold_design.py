r"""Stage 2 설계 검증 — 로그아웃 시 **대기 작업 보류(held)** 가 안전한가.

    .\.venv\Scripts\python.exe scripts\test_hold_design.py

★운영 코드를 아직 고치지 않았으므로, 설계대로의 동작을 **이 테스트 안에서**
  재현해 검증한다(보류 = `pending/` → `held/` 로 원자적 이동).
  검증 대상은 **기존 운영 코드의 반응**이다:
    · `LocalStore.claim()` 이 held 를 집지 않는가        (= Agent 가 못 가져간다)
    · `batch_plan.advance()` 가 held 를 보고 무엇을 하는가 (= 재제출/완료처리 하는가)
  임시 폴더만 쓰고 네이버·시트·운영 큐는 건드리지 않는다.

시나리오(사용자 요청): 10건을 1건씩 10배치로 돌리다 **3건 발행 후 로그아웃** →
  3건 완료 · 7건 보류 · 로그아웃 · 재로그인 후 **수동 재개** · 중복 발행 없음.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SANDBOX = Path(tempfile.mkdtemp(prefix="blog_hold_test_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"

from v2 import batch_plan, queue_store                         # noqa: E402
from v2.job import Job                                         # noqa: E402
from v2.queue_store import DONE, LocalStore, PENDING           # noqa: E402

OK, NG = "  ✅", "  ❌"
fails: list[str] = []
HELD = "held"                       # ★설계안의 새 상태


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        fails.append(name)


# ══════════════════════════════════════════════════════════════════
#  보류/재개 — 설계대로의 구현(로컬 큐 판). 원자적 이동 1회뿐이다.
# ══════════════════════════════════════════════════════════════════
def hold_account(store: LocalStore, *, device: str, login_id: str) -> dict:
    """그 login_id 의 **대기(pending) 작업만** held 로 옮긴다.

    돌려주는 값: {"held": [...], "running": [...], "skipped": [...]}
      · running 은 **보류하지 않는다**(진행 중인 발행은 안전하게 끝낸다).
      · login_id 가 빈 작업은 login_id 로 묶지 않는다(다른 계정과 섞이지 않게).
    """
    out = {"held": [], "running": [], "skipped": []}
    (store.root / HELD).mkdir(parents=True, exist_ok=True)
    for p in sorted((store.root / PENDING).glob("*.json")):
        rec = queue_store._read_json(p) or {}
        job = rec.get("job") or {}
        who = (job.get("login_id") or "").strip()
        if rec.get("target_agent") not in ("", device):
            out["skipped"].append(rec.get("id"));  continue
        if not who or who != login_id:
            out["skipped"].append(rec.get("id"));  continue
        try:                                   # ★원자적 — 방금 claim 됐으면 실패한다
            os.replace(p, store.root / HELD / p.name)
        except OSError:
            out["running"].append(rec.get("id"));  continue
        # ★실제 구현(queue_store.LocalStore.hold_account)과 같게 상태도 기록한다.
        rec["status"] = HELD
        (store.root / HELD / p.name).write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        out["held"].append(rec.get("id"))
    for p in (store.root / "running").glob("*.json"):
        rec = queue_store._read_json(p) or {}
        if (rec.get("job") or {}).get("login_id") == login_id:
            out["running"].append(rec.get("id"))
    return out


def resume_account(store: LocalStore, ids: list[str]) -> list[str]:
    back = []
    for jid in ids:
        src = store.root / HELD / f"{jid}.json"
        if src.exists():
            os.replace(src, store.root / PENDING / f"{jid}.json")
            back.append(jid)
    return back


def held_ids(store: LocalStore) -> list[str]:
    d = store.root / HELD
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


# ══════════════════════════════════════════════════════════════════
print("1) 준비 — 10건을 1건씩 10배치로 (로그아웃이 가능한 유일한 구성)")
store = LocalStore(root=SANDBOX / "queue")
ME, OTHER = "account_A", "account_B"
PC = "pc1"
job_a = Job(brand="repurely", media="GFA", deficiency="테스트",
            account="session_A", login_id=ME, count=1)
plan = batch_plan.create(job_a.to_dict(), 10, title="실전 10건", brand="repurely",
                         account="session_A", flow="production", mode="convert",
                         size=1, device=PC)
check("10배치로 쪼갰다", len(plan["batches"]) == 10, str(len(plan["batches"])))
check("배치 크기 1", plan["size"] == 1)

submitted: list[str] = []
blocked: list[str] = []
hold_on = {"login_id": ""}          # ★로그아웃 대기 상태(계정별)


def submit_run(batch: dict) -> str:
    """advance 가 다음 배치를 넣으려 할 때 불린다."""
    if hold_on["login_id"] == ME:
        # ★보류 중인 계정은 **새 배치를 만들지 않는다**
        blocked.append(batch["no"])
        raise RuntimeError("보류 중인 계정이라 새 배치를 넣지 않습니다")
    res = queue_store.submit_checked(
        store, Job.from_dict(batch_plan.job_for(plan, batch)),
        title=f"배치 {batch['no']}/10", target_agent=PC, label="배치")
    if not res.ok:
        raise RuntimeError(res.message)
    submitted.append(res.job_id)
    return res.job_id


def status_of(jid: str):
    rec = store.get(jid) if jid else None
    if rec is None:                                    # held 폴더에 있나?
        p = store.root / HELD / f"{jid}.json"
        if p.exists():
            return HELD, (queue_store._read_json(p) or {})
    return ((rec or {}).get("status") or ""), (rec or {})


def step():
    global plan
    plan, changed = batch_plan.advance(plan, job_status=status_of,
                                       submit_run=submit_run)
    if changed:
        batch_plan.save(plan)
    return changed


def finish_current(published: int = 1):
    """지금 돌고 있는 배치를 Agent 가 집어 **완료**시킨다."""
    rec = store.claim(PC)
    assert rec is not None, "집을 작업이 없다"
    store.update(rec["id"], published=[f"url{i}" for i in range(published)])
    store.finish(rec["id"], DONE, 0)
    return rec["id"]


# ══════════════════════════════════════════════════════════════════
print()
print("2) 3건 발행 — 한 배치씩 완료하고 다음은 로그인 대기")
done_ids = []
for n in range(3):
    step()                                  # 배치 제출(1회차는 PENDING→RUNNING)
    done_ids.append(finish_current())
    step()                                  # 완료 확인 → 다음 배치는 LOGIN_WAIT
    if n < 2:                               # 다음 배치를 위해 로그인했다고 표시
        cur = batch_plan.current(plan)
        batch_plan.mark_logging_in(plan, "login-ok")
        plan["batches"][cur["no"] - 1]["login_job"] = ""
        plan["batches"][cur["no"] - 1]["status"] = batch_plan.PENDING
        batch_plan.save(plan)
check("3건 발행 완료", len(done_ids) == 3, str(done_ids))
check("완료된 배치 3개", sum(1 for b in plan["batches"]
                        if b["status"] == batch_plan.DONE) == 3)
check("남은 배치 7개", sum(1 for b in plan["batches"]
                      if b["status"] in batch_plan.OPEN_STATES) == 7)

# 남은 7배치 중 하나를 큐에 올려 '대기(pending)' 상태로 만든다
cur = batch_plan.current(plan)
plan["batches"][cur["no"] - 1]["status"] = batch_plan.PENDING
batch_plan.save(plan)
step()
check("4번째 배치가 큐에서 대기 중", len(submitted) == 4
      and (store.get(submitted[-1]) or {}).get("status") == PENDING)

# 다른 계정의 작업도 하나 넣어 둔다(영향 없어야 한다)
other_res = queue_store.submit_checked(
    store, Job(brand="repurely", media="GFA", deficiency="테스트",
               account="session_B", login_id=OTHER),
    title="다른 계정 작업", target_agent=PC, label="작업")
check("다른 계정 작업도 대기 중", other_res.ok)

# ══════════════════════════════════════════════════════════════════
print()
print("3) 로그아웃 요청 — 그 계정의 대기 작업만 보류")
hold_on["login_id"] = ME                    # ★먼저 '로그아웃 대기' 를 기록
got = hold_account(store, device=PC, login_id=ME)
check("내 계정의 대기 작업이 보류됐다", got["held"] == [submitted[-1]], str(got))
check("보류 폴더에 1건", held_ids(store) == [submitted[-1]], str(held_ids(store)))
check("다른 계정 작업은 건드리지 않았다",
      other_res.job_id in (got["skipped"] or []), str(got["skipped"]))
check("실행 중이던 작업은 보류 목록에 없다", got["running"] == [], str(got["running"]))

print()
print("4) Agent 는 보류된 작업을 가져가지 못한다")
first_claim = store.claim(PC)
check("claim 은 보류 작업을 건너뛴다",
      first_claim is not None and first_claim["id"] != submitted[-1],
      str(first_claim and first_claim["id"]))
check("대신 **다른 계정** 작업을 집었다(영향 없음)",
      first_claim["id"] == other_res.job_id)
store.finish(first_claim["id"], DONE, 0)
check("그 뒤로는 집을 것이 없다(보류분은 안 보인다)", store.claim(PC) is None)

print()
print("5) 보류 중에는 새 배치도 만들지 않는다")
n_before = len(submitted)
for _ in range(3):
    try:
        step()
    except Exception:                                          # noqa: BLE001
        pass                                   # submit_run 이 막은 것
check("보류 중 새 작업 제출 0건", len(submitted) == n_before,
      f"{n_before} → {len(submitted)}")
check("계획이 완료로 바뀌지 않았다", batch_plan.is_open(plan))
check("완료된 3건은 그대로", sum(1 for b in plan["batches"]
                           if b["status"] == batch_plan.DONE) == 3)
state, _ = status_of(submitted[-1])
check("보류 작업 상태는 held", state == HELD, state)

print()
print("6) held 를 본 batch_plan 은 재제출도 완료처리도 하지 않는다")
n2, idx_before = len(submitted), plan["index"]
changed = step()
check("advance 가 아무것도 바꾸지 않는다", changed is False)
check("같은 배치를 다시 넣지 않았다", len(submitted) == n2)
check("배치 번호(진행 위치)가 움직이지 않았다", plan["index"] == idx_before)

print()
print("7) 재로그인 후 수동 재개 — 중복 없이 이어진다")
hold_on["login_id"] = ""                    # 재로그인·검증 성공
back = resume_account(store, held_ids(store))
check("보류 작업이 되돌아왔다", back == [submitted[-1]], str(back))
check("보류 폴더가 비었다", held_ids(store) == [])
again = store.claim(PC)
check("Agent 가 이제 그 작업을 집는다", again is not None
      and again["id"] == submitted[-1], str(again and again["id"]))
store.update(again["id"], published=["url_resumed"])
store.finish(again["id"], DONE, 0)
step()
check("4번째 배치가 완료로 기록됐다",
      plan["batches"][3]["status"] == batch_plan.DONE,
      plan["batches"][3]["status"])
all_ids = [p.stem for p in (store.root / "finished").glob("*.json")] \
    + [p.stem for p in (store.root / PENDING).glob("*.json")] \
    + [p.stem for p in (store.root / "running").glob("*.json")]
check("큐 전체 작업 수 = 제출한 수(중복 생성 없음)",
      len(all_ids) == len(submitted) + 1,          # +1 = 다른 계정 작업
      f"{len(all_ids)} vs {len(submitted) + 1}")
check("같은 작업 번호가 두 번 생기지 않았다", len(all_ids) == len(set(all_ids)))
check("이미 끝난 3건이 다시 생성되지 않았다",
      sum(1 for b in plan["batches"] if b["status"] == batch_plan.DONE) == 4)

print()
print(f"(샌드박스: {SANDBOX})")
if fails:
    print(f"❌ 실패 {len(fails)}건: " + " / ".join(fails))
    sys.exit(1)
print("✅ 전부 통과")
