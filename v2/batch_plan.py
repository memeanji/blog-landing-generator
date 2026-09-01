r"""배치 계획 — 큰 실행 1건을 **10개씩 순차 배치**로 쪼개 관리한다.

    25건  →  [10, 10, 5]      배치 하나가 끝나야 다음 배치를 시작한다(동시 실행 없음)
    배치와 배치 사이에는 **사람이 직접 네이버 로그인**을 한 번 더 한다
      → 한 브라우저 세션을 몇 시간씩 쓰지 않게 되어 훨씬 안정적이다.

★기존 구조는 그대로다. 이 모듈은 **큐 위에 얹은 얇은 진행표**일 뿐이다.
  · 실행은 예전처럼 `v2.run` / `v2.run_production` 이 큐를 통해 돈다.
  · 로그인도 예전처럼 `v2.session --login` 작업이다.
  · 여기서는 "다음에 무엇을 큐에 넣을지"만 기록한다(계획 파일 `queue/plans/<id>.json`).

상태(batch.status)
    pending    아직 시작 안 함
    login_wait 다음 배치 로그인 대기 — 사용자가 [로그인하고 이어서 실행] 을 눌러야 한다
    logging_in 로그인 작업이 큐에서 도는 중(로그인 창이 떠 있다)
    running    이 배치의 랜딩 작업이 도는 중
    done       이 배치 완료
    failed     이 배치 실패(계획 전체가 멈춘다)
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from .appdir import ROOT      # 개발 PC=프로젝트 폴더 / 설치본=%APPDATA%\BlogLandingAgent
PLANS_DIR = ROOT / "queue" / "plans"

# ★한 배치의 최대 건수. 화면에 노출하지 않는다(사용자는 총 건수만 입력한다).
BATCH_SIZE = 10

PENDING, LOGIN_WAIT, LOGGING_IN, RUNNING, DONE, FAILED, CANCELED = (
    "pending", "login_wait", "logging_in", "running", "done", "failed", "canceled")
# ★공통 오류(로그인 만료 · 브라우저 종료 · 시트 접근 불가)로 배치가 멈춘 상태.
#   행 단위 실패와 달리 **사람이 복구(재로그인)해야** 이어진다.
RECOVER_WAIT = "recover_wait"
OPEN_STATES = (PENDING, LOGIN_WAIT, LOGGING_IN, RUNNING, RECOVER_WAIT)

# `v2.run_production` 종료 코드
EXIT_OK = 0          # 전부 성공
EXIT_PARTIAL = 3     # ★행 단위 실패가 있었지만 나머지는 끝냈다 → 다음 배치로 계속
EXIT_STOPPED = 130   # 사용자가 중단


def split(total: int, size: int = BATCH_SIZE) -> list[int]:
    """25 → [10, 10, 5] · 23 → [10, 10, 3] · 8 → [8]."""
    total, size = max(0, int(total)), max(1, int(size))
    full, rest = divmod(total, size)
    return [size] * full + ([rest] if rest else [])


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_plan_id() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"


def _atomic_write(path: Path, text: str, tries: int = 5) -> None:
    """반쯤 쓰인 JSON 을 읽는 일이 없게 임시 파일에 쓰고 바꿔치기한다.

    ★윈도우에서는 파일 감시(에디터·백신·Streamlit watcher)가 잠깐 물고 있으면
      `os.replace` 가 WinError 5 로 실패한다. 몇 번 다시 시도하고, 그래도 안 되면
      그냥 덮어쓴다(계획 파일은 UI 진행표라 잠깐의 원자성보다 안 끊기는 게 중요하다).
    """
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    for i in range(max(1, tries)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    try:
        path.write_text(text, encoding="utf-8")
    finally:
        try:
            tmp.unlink()
        except Exception:                                      # noqa: BLE001
            pass


def path_of(plan_id: str) -> Path:
    return PLANS_DIR / f"{plan_id}.json"


def create(job_dict: dict, total: int, *, title: str = "", brand: str = "",
           account: str = "", flow: str = "review", mode: str = "convert",
           size: int = BATCH_SIZE, device: str = "") -> dict:
    """총 건수를 배치로 쪼갠 계획을 만든다(아직 아무것도 큐에 넣지 않는다).

    · `start` 는 **convert(기존 글 수정)** 에서만 이어서 센다. create/검수용은
      이미 처리한 행이 조회에서 빠지므로 항상 1 이다(기존 CLI 동작 그대로).
    """
    sizes = split(total, size)
    batches, done = [], 0
    for n, sz in enumerate(sizes, start=1):
        batches.append({
            "no": n, "size": sz,
            "start": (1 + done) if (flow == "production" and mode == "convert") else 1,
            "login_job": "", "run_job": "",
            "status": PENDING if n == 1 else LOGIN_WAIT,
            "published": 0, "error": "",
        })
        done += sz
    plan = {
        "id": new_plan_id(), "created_at": now_iso(), "title": title,
        "brand": brand, "account": account, "flow": flow, "mode": mode,
        "device": device,          # 이 계획을 실행할 PC(Agent)
        "total": int(total), "size": int(size), "sizes": sizes,
        "job": dict(job_dict or {}), "batches": batches, "index": 0,
        "status": RUNNING if batches else DONE,
    }
    save(plan)
    return plan


def save(plan: dict) -> dict:
    plan["updated_at"] = now_iso()
    _atomic_write(path_of(plan["id"]),
                  json.dumps(plan, ensure_ascii=False, indent=1))
    return plan


def get(plan_id: str) -> dict | None:
    p = path_of(plan_id or "")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return None


def list_plans(limit: int = 10) -> list[dict]:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in PLANS_DIR.glob("*.json"):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:                                      # noqa: BLE001
            continue
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:limit]


def current(plan: dict) -> dict | None:
    """지금 처리 중인 배치."""
    i = int(plan.get("index") or 0)
    batches = plan.get("batches") or []
    return batches[i] if 0 <= i < len(batches) else None


def done_count(plan: dict) -> int:
    return sum(int(b.get("size") or 0) for b in (plan.get("batches") or [])
               if b.get("status") == DONE)


def is_open(plan: dict | None) -> bool:
    return bool(plan) and plan.get("status") in OPEN_STATES


def summary(plan: dict) -> str:
    sizes = plan.get("sizes") or []
    i = int(plan.get("index") or 0)
    return (f"{plan.get('total')}건 = " + " + ".join(str(s) for s in sizes)
            + f"  (배치 {min(i + 1, len(sizes))}/{len(sizes)})")


def cancel(plan: dict) -> dict:
    plan["status"] = CANCELED
    for b in plan.get("batches") or []:
        if b.get("status") in OPEN_STATES:
            b["status"] = CANCELED
    return save(plan)


def job_for(plan: dict, batch: dict) -> dict:
    """이 배치에서 큐에 넣을 Job dict — 건수/시작번호만 갈아끼운다."""
    job = dict(plan.get("job") or {})
    job["count"] = int(batch.get("size") or 0)
    if plan.get("flow") == "production":
        job["start"] = int(batch.get("start") or 1)
    return job

# ── 상태 전이 ────────────────────────────────────────────────────
#   큐를 직접 알지 못한다. 두 함수를 받아서 판단만 한다(테스트가 쉬워진다).
#     job_status(job_id) -> (status, record)      큐에서 작업 상태를 읽는다
#     submit_run(batch)  -> job_id                그 배치 실행을 큐에 넣는다
JOB_DONE = "done"
JOB_BAD = ("failed", "canceled")


def advance(plan: dict, *, job_status, submit_run) -> tuple[dict, bool]:
    """계획을 한 칸 진행시킨다. `(plan, 바뀌었나)`.

    · 첫 배치           → 바로 실행([실행 준비]에서 방금 로그인했으므로)
    · 로그인 끝남       → **그 배치 실행을 이어서** 큐에 넣는다
    · 배치 실행 끝남    → 다음 배치는 `login_wait` 로 둔다(자동으로 시작하지 않는다)
    ★언제나 **한 배치만** 큐에 올라간다 — 동시에 도는 일이 없다.
    """
    if not is_open(plan):
        return plan, False
    batch = current(plan)
    if batch is None:
        plan["status"] = DONE
        return plan, True

    if batch["status"] == PENDING:
        batch["run_job"] = submit_run(batch)
        batch["status"] = RUNNING
        return plan, True

    if batch["status"] == LOGGING_IN:
        state, rec = job_status(batch.get("login_job") or "")
        if state == JOB_DONE:
            batch["run_job"] = submit_run(batch)
            batch["status"] = RUNNING
            return plan, True
        if state in JOB_BAD:
            batch["status"] = LOGIN_WAIT          # 다시 누를 수 있게 되돌린다
            batch["error"] = str(rec.get("error")
                                 or "로그인이 끝나지 않았습니다")[:200]
            return plan, True
        return plan, False

    if batch["status"] == RUNNING:
        state, rec = job_status(batch.get("run_job") or "")
        if state not in (JOB_DONE,) + JOB_BAD:
            return plan, False
        code = rec.get("exit_code")
        batch["published"] = len(rec.get("published") or [])

        # ① 전부 성공 · ② 행 단위 실패만 있었다(exit 3) → **배치는 끝난 것**으로 보고
        #    다음 배치로 넘어간다. 행 실패가 배치·계획을 막지 않는다.
        if state == JOB_DONE or code == EXIT_PARTIAL:
            batch["status"] = DONE
            if code == EXIT_PARTIAL:
                batch["error"] = "일부 건 실패(나머지는 완료)"
            plan["index"] = int(plan.get("index") or 0) + 1
            if plan["index"] >= len(plan.get("batches") or []):
                plan["status"] = DONE
            return plan, True

        # ③ 사용자가 중단
        if code == EXIT_STOPPED or state == "canceled":
            batch["status"] = CANCELED
            plan["status"] = CANCELED
            return plan, True

        # ④ 공통 오류 → 계획을 실패로 끝내지 않고 **복구 대기**로 멈춘다.
        batch["status"] = RECOVER_WAIT
        batch["error"] = str(rec.get("error") or "실행이 중간에 멈췄습니다")[:300]
        return plan, True
    return plan, False


def prepare_retry(plan: dict) -> dict:
    """복구 대기 중인 배치를 **이미 발행된 만큼 건너뛰고** 다시 시작할 수 있게 만든다.

    · convert(기존 글 수정) : 발행한 수만큼 `start` 를 밀고 `size` 를 줄인다.
    · 그 외                : 이미 처리된 행은 조회에서 빠지므로 `size` 만 줄인다.
    ★이미 성공한 건은 다시 실행되지 않는다.
    """
    batch = current(plan)
    if batch is None:
        return plan
    done_here = int(batch.get("published") or 0)
    batch["size"] = max(1, int(batch.get("size") or 0) - done_here)
    if plan.get("flow") == "production" and plan.get("mode") == "convert":
        batch["start"] = int(batch.get("start") or 1) + done_here
    batch["published"] = 0
    batch["run_job"] = ""
    batch["status"] = LOGIN_WAIT          # 재로그인 후 이어서 실행
    return save(plan)


def mark_logging_in(plan: dict, login_job_id: str) -> dict:
    """이 배치의 로그인 작업을 걸어 둔다(사용자가 버튼을 눌렀을 때)."""
    batch = current(plan)
    if batch is not None:
        batch["login_job"] = login_job_id
        batch["status"] = LOGGING_IN
        batch["error"] = ""
    return save(plan)

