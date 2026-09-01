r"""로컬 에이전트 — 큐에 올라온 작업을 **이 PC 에서** 실행한다.

    .\.venv\Scripts\python.exe -m v2.agent            # 계속 돌면서 큐를 본다
    .\.venv\Scripts\python.exe -m v2.agent --once     # 한 건만 처리하고 끝
    .\.venv\Scripts\python.exe -m v2.agent --status   # 지금 붙어 있는 에이전트 보기

이 파일이 **'어느 PC 에서 실행되는가'를 담당하는 유일한 곳**이다.
  · UI(Streamlit)는 큐에 넣기만 한다 → 다른 PC 에 둬도 된다.
  · 네이버 로그인 세션(`sessions/<account>/`)은 **에이전트가 도는 PC 안에만** 있다.
  · 나중에 큐를 Supabase 로 바꾸면(`queue_store.get_store()`), 이 파일을 각 PC 에서
    그대로 띄우는 것만으로 여러 PC 분산 실행이 된다. 아래 코드는 안 고쳐도 된다.

실행 자체는 기존 CLI 그대로다 — `v2.run` / `v2.run_production` / `v2.session` 을
자식 프로세스로 돌리고(`v2.runner`), `@@EVENT` 를 큐에 옮겨 적는다.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback

from . import pairing
from .job import Job
from .queue_store import (CANCELED, DONE, FAILED, JobStore, get_store, host_name,
                          now_iso)
from .runner import Runner

POLL_SEC = 1.5
HEARTBEAT_SEC = 5.0


def _say(msg: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass
    print(f"{now_iso()[11:]} {msg}", flush=True)


def run_record(store: JobStore, rec: dict, agent_id: str) -> int:
    """작업 1건을 실행하고 진행 상황을 큐에 계속 적는다."""
    job_id = rec["id"]
    kind = rec.get("kind") or "run"
    job = Job.from_dict(rec.get("job") or {})
    agg = {"total": int(rec.get("total") or 0), "made": 0,
           "published": list(rec.get("published") or []), "error": ""}
    done = threading.Event()
    result = {"code": None}

    def on_line(text: str) -> None:
        store.append_log(job_id, text)

    def on_event(ev: dict) -> None:
        store.append_event(job_id, ev)
        name = ev.get("event")
        if name == "plan":
            agg["total"] = int(ev.get("total") or 0)
        elif name == "post_ready":
            agg["made"] = int(ev.get("no") or agg["made"] + 1)
        elif name == "published":
            url = str(ev.get("url") or "")
            if url:
                agg["published"].append(url)
        elif name == "post_failed":
            agg["error"] = str(ev.get("error") or "")
        elif name == "run_finished" and not ev.get("ok"):
            agg["error"] = str(ev.get("error") or "")
        store.update(job_id, total=agg["total"], made=agg["made"],
                     published=agg["published"], error=agg["error"])

    def on_exit(code: int) -> None:
        result["code"] = int(code)
        done.set()

    runner = Runner(on_line=on_line, on_event=on_event, on_exit=on_exit)
    # ★설치본 PC 에는 brands.json 이 없다 — 화면이 실어 보낸 브랜드 설정을
    #   자식 프로세스의 환경변수로 넘긴다(파일이 있으면 파일이 우선).
    env_extra = {"BLOG_BRANDS_JSON": job.brand_config} if job.brand_config else None
    try:
        if kind == "session":
            # 세션 작업은 `v2.session` 을 그대로 돌린다(로그인 창은 이 PC 에 뜬다).
            cmd = runner.start_module("v2.session", list(job.extra), label="session",
                                      env_extra=env_extra)
        else:
            cmd = runner.start(job, env_extra=env_extra)
    except Exception as exc:                                   # noqa: BLE001
        store.append_log(job_id, f"[agent] 실행하지 못했습니다: {exc}")
        store.update(job_id, error=str(exc))
        store.finish(job_id, FAILED, 1)
        return 1

    store.update(job_id, pid=getattr(runner.proc, "pid", None),
                 command=" ".join(cmd))
    store.append_log(job_id, f"[agent] {agent_id} 실행 시작")
    store.append_log(job_id, f"[agent] {' '.join(cmd)}")
    _say(f"실행 중 — {job_id} ({rec.get('title')})")

    stopped = False
    last_beat = 0.0
    while not done.wait(0.4):
        if not stopped and store.cancel_requested(job_id):
            store.append_log(job_id, "[agent] 중단 요청 — 프로세스를 정리합니다")
            runner.stop()
            stopped = True
        if time.time() - last_beat > HEARTBEAT_SEC:
            store.heartbeat(agent_id, pid=os.getpid(), state="busy", job=job_id)
            last_beat = time.time()

    code = int(result["code"] or 0)
    status = DONE if code == 0 else (CANCELED if code == 130 else FAILED)
    store.append_log(job_id, f"[agent] 종료 — {status} (코드 {code})")
    store.finish(job_id, status, code)
    _say(f"완료 — {job_id} · {status} (코드 {code})")
    return code


def serve(store: JobStore, agent_id: str, once: bool = False,
          poll: float = POLL_SEC, quiet: bool = False) -> int:
    """큐를 보고 작업을 실행한다.

    `quiet` — 트레이에서 짧은 주기로 반복 호출할 때 시작/대기 문구를 생략한다
              (작업을 실제로 집었을 때의 로그는 그대로 남는다).
    """
    if not quiet:
        _say(f"에이전트 시작 — id={agent_id} · pid={os.getpid()} "
             f"· 큐={type(store).__name__}")
        _say("큐를 봅니다. Ctrl+C 로 종료합니다.")
    last_beat = 0.0
    try:
        while True:
            if time.time() - last_beat > HEARTBEAT_SEC:
                store.heartbeat(agent_id, pid=os.getpid(), state="idle",
                                version=pairing.AGENT_VERSION)
                last_beat = time.time()
            rec = store.claim(agent_id)
            if rec is None:
                if once:
                    if not quiet:
                        _say("처리할 작업이 없습니다(--once).")
                    return 0
                time.sleep(poll)
                continue
            try:
                run_record(store, rec, agent_id)
            except Exception as exc:                           # noqa: BLE001
                store.append_log(rec["id"], f"[agent] 예외: {exc}")
                store.append_log(rec["id"], traceback.format_exc())
                store.finish(rec["id"], FAILED, 1)
            if once:
                return 0
    except KeyboardInterrupt:
        _say("중단했습니다.")
        return 130
    finally:
        store.heartbeat(agent_id, pid=os.getpid(), state="stopped")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="블로그 랜딩 로컬 에이전트")
    p.add_argument("--agent-id", default="", help=f"에이전트 이름(기본: {host_name()})")
    p.add_argument("--once", action="store_true", help="한 건만 처리하고 끝낸다")
    p.add_argument("--poll", type=float, default=POLL_SEC, help="큐 확인 간격(초)")
    p.add_argument("--status", action="store_true", help="에이전트 현황만 보고 끝")
    p.add_argument("--force", action="store_true",
                   help="이미 같은 이름의 에이전트가 살아 있어도 띄운다")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # ★이 PC 가 화면과 연결(페어링)돼 있으면 그 토큰으로 **원격 큐**에 붙는다.
    #   연결 전이거나 설정이 없으면 예전처럼 로컬 큐로 돈다(기존 동작 보존).
    dev = pairing.apply_env()
    store = get_store()
    agent_id = args.agent_id or dev.get("device_id") or host_name()
    if dev.get("device_id"):
        _say(f"연결된 PC — {dev.get('label')} · device {dev['device_id'][:8]}…")
    elif type(store).__name__ != "LocalStore":
        _say("[안내] 아직 이 PC 가 화면과 연결되지 않았습니다 "
             "(`-m v2.pairing --code <6자리>`).")

    if args.status:
        rows = store.agents(max_age_sec=30)
        if not rows:
            _say("붙어 있는 에이전트가 없습니다.")
        for r in rows:
            _say(f"{'●' if r.get('alive') else '○'} {r.get('agent')} "
                 f"({r.get('host')}) · {r.get('state')} · pid={r.get('pid')} "
                 f"· {r.get('at')}")
        return 0

    if not args.force and type(store).__name__ == "LocalStore":
        for r in store.agents(max_age_sec=20):
            if r.get("agent") == agent_id and r.get("alive") \
                    and r.get("pid") != os.getpid() and r.get("state") != "stopped":
                _say(f"[중단] 같은 이름의 에이전트가 이미 돌고 있습니다 "
                     f"(pid={r.get('pid')}). 그대로 쓰거나 --force 를 주세요.")
                return 2
    return serve(store, agent_id, once=args.once, poll=args.poll)


if __name__ == "__main__":
    sys.exit(main())
