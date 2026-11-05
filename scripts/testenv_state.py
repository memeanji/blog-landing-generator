r"""테스트 폴더의 **큐·하트비트 상태만** 바꾼다 — 화면 점검용 도구.

    .\.venv\Scripts\python.exe scripts\testenv_state.py show
    .\.venv\Scripts\python.exe scripts\testenv_state.py touch
    .\.venv\Scripts\python.exe scripts\testenv_state.py agent --version 1.3.0
    .\.venv\Scripts\python.exe scripts\testenv_state.py agent --busy
    .\.venv\Scripts\python.exe scripts\testenv_state.py session --status pending
    .\.venv\Scripts\python.exe scripts\testenv_state.py jobs --account tst_dn --count 3
    .\.venv\Scripts\python.exe scripts\testenv_state.py hold --account tst_dn
    .\.venv\Scripts\python.exe scripts\testenv_state.py clear

★안전 조건
  · 쓰기는 **`%TEMP%\blog_testenv` 안으로만** 한다. 경로가 다르면 즉시 중단한다.
  · 운영 폴더·운영 DB·네이버·구글에 **접속하지 않는다**(파일만 만진다).
  · Agent 를 띄우지 않는다 — 하트비트 파일로 흉내 낸다.
  · `clear` 는 **큐의 작업 파일만** 지운다(계정·브랜드·캐시·하트비트는 남긴다).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

FIX = Path(tempfile.gettempdir()) / "blog_testenv"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

STATES = ("pending", "running", "held", "finished")


def guard(p: Path) -> Path:
    """테스트 폴더 밖이면 쓰지 않는다."""
    q = p.resolve()
    if FIX.resolve() not in q.parents and q != FIX.resolve():
        print(f"  [X] 테스트 폴더 밖입니다 — {q}")
        sys.exit(2)
    return q


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def jwrite(p: Path, data: dict) -> None:
    guard(p).parent.mkdir(parents=True, exist_ok=True)
    guard(p).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def jread(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return {}


def accounts() -> list[dict]:
    return jread(FIX / "accounts.json").get("accounts") or []


def qdir(state: str) -> Path:
    return FIX / "queue" / state


def new_id() -> str:
    import uuid

    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def put_job(account: str, *, state: str = "pending", kind: str = "run",
            login_id: str | None = None, title: str = "",
            dev: str = "TESTPC", extra: list | None = None) -> str:
    """작업 레코드 하나를 그 상태 폴더에 넣는다(LocalStore 형식)."""
    acc = next((a for a in accounts() if a.get("id") == account), None)
    lid = login_id if login_id is not None else (acc or {}).get("login_id", "")
    jid = new_id()
    rec = {
        "id": jid, "kind": kind, "brand": (acc or {}).get("brand", "repurely"),
        "title": title or f"[테스트] {account} {kind}",
        "status": state, "created_at": now(), "created_by": "testenv",
        "target_agent": dev, "agent": dev if state == "running" else "",
        "pid": None, "started_at": now() if state == "running" else "",
        "finished_at": "", "exit_code": None, "total": 0, "made": 0,
        "published": [], "error": "", "command": "",
        "job": {"account": account, "account_name": (acc or {}).get("label", ""),
                "login_id": lid, "brand": (acc or {}).get("brand", "repurely"),
                "ref_tab": (acc or {}).get("ref_tab", ""),
                "flow": "production", "count": 1, "extra": list(extra or [])},
    }
    jwrite(qdir(state) / f"{jid}.json", rec)
    return jid


# ══════════════════════════════════════════════════════════════════
def cmd_show(_a) -> int:
    print("=" * 76)
    print(f" 테스트 폴더 상태 — {FIX}")
    print("=" * 76)
    print("\n 하트비트(가짜 Agent)")
    for p in sorted((FIX / "queue" / "agents").glob("*.json")):
        d = jread(p)
        age = "?"
        try:
            age = f"{(datetime.now() - datetime.fromisoformat(d['at'])).total_seconds():.0f}초 전"
        except Exception:                                      # noqa: BLE001
            pass
        fresh = "싱싱(alive)" if age != "?" and float(
            age.split("초")[0]) < 30 else "★만료(무응답으로 보임)"
        print(f"   {p.stem:<18} version={d.get('version'):<8} "
              f"state={d.get('state'):<6} job={d.get('job') or '-':<22} "
              f"{age} {fresh}")
    print("\n 큐")
    for s in STATES:
        rows = sorted(qdir(s).glob("*.json"))
        print(f"   {s:<9} {len(rows)}건")
        for p in rows[:6]:
            r = jread(p)
            j = r.get("job") or {}
            print(f"       {r.get('id','?')[:22]:<24}"
                  f"account={j.get('account',''):<12}"
                  f"login_id={j.get('login_id') or '(없음)':<12}"
                  f"kind={r.get('kind','')}")
    print("\n 계정")
    for a in accounts():
        print(f"   {a.get('id'):<12} login_id={a.get('login_id') or '(없음)':<12}"
              f" blog={a.get('blog_id',''):<10} brand={a.get('brand','')}")
    print("=" * 76)
    return 0


def cmd_touch(_a) -> int:
    n = 0
    for p in sorted((FIX / "queue" / "agents").glob("*.json")):
        d = jread(p)
        d["at"] = now()
        jwrite(p, d)
        n += 1
        print(f"  [o] {p.stem} at={d['at']} (version={d.get('version')})")
    print(f"  하트비트 {n}개 갱신 — 화면을 다시 그리면 '연결됨' 으로 보입니다.")
    return 0


def cmd_agent(a) -> int:
    p = FIX / "queue" / "agents" / f"{a.name}.json"
    d = jread(p) or {"agent": a.name, "host": a.name, "pid": 0}
    d["agent"] = a.name
    if a.version:
        d["version"] = a.version
    if a.busy:
        d["state"], d["job"] = "running", a.job or "OTHER-JOB"
    if a.idle:
        d["state"], d["job"] = "idle", ""
    d["at"] = now()
    jwrite(p, d)
    print(f"  [o] {a.name} → version={d.get('version')} "
          f"state={d.get('state')} job={d.get('job') or '-'}")
    return 0


def cmd_session(a) -> int:
    jid = put_job(a.account, state=a.status, kind="session",
                  title=f"[테스트] 로그인 — {a.account}",
                  extra=["--login", a.account, "--events"])
    print(f"  [o] 세션 작업 {jid} → {a.status} (account={a.account})")
    print("      화면에서 '▶ 로그인 창이 열렸습니다' 가 보이고 B1 예산이 돌기 시작합니다.")
    return 0


def cmd_jobs(a) -> int:
    ids = [put_job(a.account, state=("held" if a.held else "pending"))
           for _ in range(a.count)]
    print(f"  [o] 발행 작업 {len(ids)}건 → {'held' if a.held else 'pending'} "
          f"(account={a.account})")
    for i in ids:
        print(f"      {i}")
    return 0


def cmd_hold(a) -> int:
    moved = []
    for p in sorted(qdir("pending").glob("*.json")):
        r = jread(p)
        if (r.get("job") or {}).get("account") != a.account:
            continue
        r["status"], r["held_at"] = "held", now()
        jwrite(qdir("held") / p.name, r)
        guard(p).unlink()
        moved.append(r.get("id"))
    print(f"  [o] {a.account} 의 대기 작업 {len(moved)}건을 held 로 옮겼습니다")
    print("      ★이건 화면의 '보류' 와 같은 결과만 흉내 낸 것입니다"
          "(큐 파일만 옮김).")
    return 0


def cmd_clear(_a) -> int:
    n = 0
    for s in STATES:
        for p in sorted(qdir(s).glob("*.json")):
            guard(p).unlink()
            n += 1
    print(f"  [o] 큐의 작업 파일 {n}건 삭제 — 계정·브랜드·캐시·하트비트는 그대로")
    return 0


def main() -> int:
    if not FIX.is_dir():
        print(f"  [X] 테스트 폴더가 없습니다 — {FIX}")
        print("      먼저: scripts\\setup_local_testenv.py --build")
        return 2
    ap = argparse.ArgumentParser(description="테스트 폴더 상태 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="현재 상태 보기")
    sub.add_parser("touch", help="하트비트 시각 갱신(연결됨으로 보이게)")
    g = sub.add_parser("agent", help="가짜 Agent 바꾸기")
    g.add_argument("--name", default="TESTPC")
    g.add_argument("--version", default="")
    g.add_argument("--busy", action="store_true", help="다른 작업 중으로")
    g.add_argument("--idle", action="store_true", help="유휴로")
    g.add_argument("--job", default="")
    s = sub.add_parser("session", help="로그인(세션) 작업 넣기 — B1 시험")
    s.add_argument("--account", default="tst_re")
    s.add_argument("--status", default="pending", choices=("pending", "running"))
    j = sub.add_parser("jobs", help="발행 작업 넣기")
    j.add_argument("--account", default="tst_re")
    j.add_argument("--count", type=int, default=1)
    j.add_argument("--held", action="store_true")
    h = sub.add_parser("hold", help="그 계정의 대기 작업을 held 로")
    h.add_argument("--account", default="tst_re")
    sub.add_parser("clear", help="큐의 작업 파일만 지우기")
    a = ap.parse_args()
    return {"show": cmd_show, "touch": cmd_touch, "agent": cmd_agent,
            "session": cmd_session, "jobs": cmd_jobs, "hold": cmd_hold,
            "clear": cmd_clear}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
