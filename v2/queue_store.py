r"""작업 큐 저장소 — **UI 와 실행기를 떼어 놓는 층**.

    Streamlit(UI)  ──submit──▶  JobStore  ◀──claim/보고──  Local Agent (PC 마다 1개)
                                                              └─▶ v2.run / v2.run_production

UI 는 Playwright 를 절대 직접 부르지 않는다. **작업을 큐에 넣기만** 하고, 실행은 그 PC 의
에이전트(`v2.agent`)가 한다. 그래서 나중에 UI 를 다른 PC/클라우드에 두어도 구조가 그대로다.

  · 지금(1차)      : `LocalStore` — `queue/` 폴더. UI 와 에이전트가 같은 PC.
  · 나중(Supabase) : `SupabaseStore` 를 하나 더 만들어 `get_store()` 가 그걸 돌려주게만 하면
                     UI·에이전트 코드는 **한 줄도 안 고쳐도** 여러 PC 에서 돌아간다.
                     아래 `JobStore` 의 메서드가 그때 채워야 할 계약(contract)이다.

★네이버 로그인 세션(`sessions/<account>/`)은 큐에 올리지 않는다. 계정 세션은 **실행하는
  PC 안에만** 남는다(쿠키를 네트워크로 보내지 않는다).
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path

from .job import Job

from .appdir import ROOT      # 개발 PC=프로젝트 폴더 / 설치본=%APPDATA%\BlogLandingAgent
QUEUE_ROOT = ROOT / "queue"

PENDING, RUNNING, DONE, FAILED, CANCELED = ("pending", "running", "done",
                                            "failed", "canceled")
OPEN_STATES = (PENDING, RUNNING)
KINDS = ("run", "session")          # run = 랜딩 생성/전환, session = 로그인/세션 확인


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_job_id() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def host_name() -> str:
    try:
        return socket.gethostname()
    except Exception:                                          # noqa: BLE001
        return "local"


class JobStore(ABC):
    """UI ↔ 에이전트 사이의 계약. Supabase 판을 만들 때 이 메서드만 채우면 된다."""

    # ── UI 쪽 ────────────────────────────────────────────────────
    @abstractmethod
    def submit(self, job: Job, *, kind: str = "run", title: str = "",
               created_by: str = "", target_agent: str = "") -> str: ...

    @abstractmethod
    def get(self, job_id: str) -> dict | None: ...

    @abstractmethod
    def list_jobs(self, limit: int = 30) -> list[dict]: ...

    @abstractmethod
    def request_cancel(self, job_id: str) -> None: ...

    @abstractmethod
    def read_log(self, job_id: str, offset: int = 0) -> tuple[str, int]: ...

    @abstractmethod
    def read_events(self, job_id: str, after: int = 0) -> tuple[list[dict], int]: ...

    # ── 에이전트 쪽 ──────────────────────────────────────────────
    @abstractmethod
    def claim(self, agent_id: str) -> dict | None: ...

    @abstractmethod
    def update(self, job_id: str, **fields) -> dict | None: ...

    @abstractmethod
    def finish(self, job_id: str, status: str, exit_code: int = 0) -> dict | None: ...

    @abstractmethod
    def append_log(self, job_id: str, line: str) -> None: ...

    @abstractmethod
    def append_event(self, job_id: str, event: dict) -> None: ...

    @abstractmethod
    def cancel_requested(self, job_id: str) -> bool: ...

    @abstractmethod
    def heartbeat(self, agent_id: str, **info) -> None: ...

    @abstractmethod
    def agents(self, max_age_sec: int = 30) -> list[dict]: ...


def _atomic_write(path: Path, text: str) -> None:
    """반쯤 쓰인 JSON 을 상대가 읽는 일이 없게 임시 파일에 쓰고 바꿔치기한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return None


class LocalStore(JobStore):
    r"""폴더 하나로 만든 큐.

        queue/pending/<id>.json     아직 아무도 안 집은 작업
        queue/running/<id>.json     어느 에이전트가 돌리는 중
        queue/finished/<id>.json    끝난 작업(done/failed/canceled)
        queue/logs/<id>.log         사람이 읽는 로그
        queue/logs/<id>.events.jsonl  기계용 이벤트(그대로 Supabase 에 넣을 수 있다)
        queue/cancel/<id>           중단 요청 표시
        queue/agents/<agent>.json   에이전트 heartbeat

    ★작업을 집는 것(claim)은 `os.rename` 으로 한다 — 같은 파일을 두 에이전트가 동시에
      옮기면 한쪽만 성공한다(윈도우에서도 원자적).
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else QUEUE_ROOT
        for sub in ("pending", "running", "finished", "logs", "cancel", "agents"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ── 경로 ─────────────────────────────────────────────────────
    def _dir(self, state: str) -> Path:
        return self.root / state

    def _find(self, job_id: str) -> tuple[str, Path] | tuple[None, None]:
        for state in ("running", "pending", "finished"):
            p = self._dir(state) / f"{job_id}.json"
            if p.exists():
                return state, p
        return None, None

    def log_path(self, job_id: str) -> Path:
        return self.root / "logs" / f"{job_id}.log"

    def events_path(self, job_id: str) -> Path:
        return self.root / "logs" / f"{job_id}.events.jsonl"

    # ── UI 쪽 ────────────────────────────────────────────────────
    def submit(self, job: Job, *, kind: str = "run", title: str = "",
               created_by: str = "", target_agent: str = "") -> str:
        job_id = new_job_id()
        # ★브랜드는 레코드 맨 위에도 따로 둔다 — 큐 파일만 열어 봐도 어느 브랜드인지 안다.
        brand = (getattr(job, "brand", "") or "").strip()
        record = {
            "id": job_id,
            "kind": kind if kind in KINDS else "run",
            "brand": brand,
            "title": title or (f"[{brand}] " if brand else "")
                     + f"{job.flow} · {job.media} / {job.deficiency}",
            "status": PENDING,
            "created_at": now_iso(),
            "created_by": created_by or host_name(),
            "target_agent": target_agent,          # 비우면 아무 에이전트나 집어간다
            "job": job.to_dict(),
            "agent": "", "pid": None,
            "started_at": "", "finished_at": "", "exit_code": None,
            "total": 0, "made": 0, "published": [], "error": "",
        }
        _atomic_write(self._dir(PENDING) / f"{job_id}.json",
                      json.dumps(record, ensure_ascii=False, indent=1))
        self.log_path(job_id).touch()
        return job_id

    def get(self, job_id: str) -> dict | None:
        _state, path = self._find(job_id)
        return _read_json(path) if path else None

    def list_jobs(self, limit: int = 30) -> list[dict]:
        rows: list[dict] = []
        for state in ("running", "pending", "finished"):
            for p in self._dir(state).glob("*.json"):
                rec = _read_json(p)
                if rec:
                    rows.append(rec)
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[:limit]

    def request_cancel(self, job_id: str) -> None:
        (self.root / "cancel" / job_id).write_text(now_iso(), encoding="utf-8")

    def read_log(self, job_id: str, offset: int = 0) -> tuple[str, int]:
        path = self.log_path(job_id)
        if not path.exists():
            return "", offset
        data = path.read_text(encoding="utf-8", errors="replace")
        return data[offset:], len(data)

    def read_events(self, job_id: str, after: int = 0) -> tuple[list[dict], int]:
        path = self.events_path(job_id)
        if not path.exists():
            return [], after
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        out = []
        for raw in lines[after:]:
            try:
                out.append(json.loads(raw))
            except Exception:                                  # noqa: BLE001
                continue
        return out, len(lines)

    # ── 에이전트 쪽 ──────────────────────────────────────────────
    def claim(self, agent_id: str) -> dict | None:
        for src in sorted(self._dir(PENDING).glob("*.json")):
            rec = _read_json(src)
            if rec is None:
                continue
            want = (rec.get("target_agent") or "").strip()
            if want and want != agent_id:
                continue                                # 다른 PC 전용 작업
            dst = self._dir(RUNNING) / src.name
            try:
                os.rename(src, dst)                     # ★원자적 — 진 쪽은 예외
            except OSError:
                continue
            rec.update({"status": RUNNING, "agent": agent_id,
                        "started_at": now_iso()})
            _atomic_write(dst, json.dumps(rec, ensure_ascii=False, indent=1))
            return rec
        return None

    def update(self, job_id: str, **fields) -> dict | None:
        state, path = self._find(job_id)
        if not path:
            return None
        rec = _read_json(path) or {}
        rec.update(fields)
        _atomic_write(path, json.dumps(rec, ensure_ascii=False, indent=1))
        return rec

    def finish(self, job_id: str, status: str, exit_code: int = 0) -> dict | None:
        state, path = self._find(job_id)
        if not path:
            return None
        rec = _read_json(path) or {}
        rec.update({"status": status, "exit_code": exit_code,
                    "finished_at": now_iso()})
        dest = self._dir("finished") / f"{job_id}.json"
        _atomic_write(dest, json.dumps(rec, ensure_ascii=False, indent=1))
        if path != dest:
            try:
                path.unlink()
            except Exception:                                  # noqa: BLE001
                pass
        marker = self.root / "cancel" / job_id
        if marker.exists():
            try:
                marker.unlink()
            except Exception:                                  # noqa: BLE001
                pass
        return rec

    def append_log(self, job_id: str, line: str) -> None:
        path = self.log_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\r\n") + "\n")

    def append_event(self, job_id: str, event: dict) -> None:
        path = self.events_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def cancel_requested(self, job_id: str) -> bool:
        return (self.root / "cancel" / job_id).exists()

    # ── 페어링(로컬 mock) ────────────────────────────────────────
    #   ★Supabase 를 붙이기 전에도 화면 흐름을 그대로 검증하기 위한 것.
    #     원격 큐(SupabaseStore)에는 같은 이름의 메서드가 이미 있다.
    def create_pairing(self, minutes: int = 10) -> dict:
        import random

        (self.root / "pairings").mkdir(parents=True, exist_ok=True)
        code = f"{random.randint(0, 999999):06d}"
        rec = {"code": code, "created_at": now_iso(),
               "expires_at": (datetime.now() + timedelta(minutes=minutes))
               .isoformat(timespec="seconds"),
               "used_at": "", "device_id": ""}
        _atomic_write(self.root / "pairings" / f"{code}.json",
                      json.dumps(rec, ensure_ascii=False, indent=1))
        return rec

    def pairing_result(self, code: str) -> dict | None:
        """코드가 소비됐으면 연결된 PC 정보(하트비트 전이라도 연결됨으로 본다)."""
        rec = _read_json(self.root / "pairings" / f"{code}.json")
        if not rec or not rec.get("device_id"):
            return None
        return self.device(rec["device_id"]) or {
            "device_id": rec["device_id"], "label": rec.get("label", ""),
            "state": "idle", "alive": False, "version": "",
            "last_seen": rec.get("used_at", "")}

    def consume_pairing(self, code: str, device_id: str, label: str) -> dict:
        """Agent 쪽에서 코드를 소비한다(로컬 mock)."""
        path = self.root / "pairings" / f"{code}.json"
        rec = _read_json(path)
        if not rec:
            raise RuntimeError("페어링 코드가 없습니다")
        if rec.get("used_at"):
            raise RuntimeError("이미 사용된 코드입니다")
        try:
            if datetime.fromisoformat(rec["expires_at"]) < datetime.now():
                raise RuntimeError("만료된 코드입니다")
        except (KeyError, ValueError):
            pass
        rec.update({"used_at": now_iso(), "device_id": device_id, "label": label})
        _atomic_write(path, json.dumps(rec, ensure_ascii=False, indent=1))
        return rec

    def device(self, device_id: str) -> dict | None:
        """agents() 중 이 device 하나(화면의 '내 PC Agent' 표시용)."""
        for a in self.agents(max_age_sec=60):
            if a.get("agent") == device_id:
                return {"device_id": a.get("agent"),
                        "label": a.get("label") or a.get("host") or "",
                        "state": a.get("state") or "idle", "alive": bool(a.get("alive")),
                        "version": a.get("version", ""), "last_seen": a.get("at", "")}
        return None

    def heartbeat(self, agent_id: str, **info) -> None:
        data = {"agent": agent_id, "host": host_name(), "at": now_iso(), **info}
        _atomic_write(self.root / "agents" / f"{agent_id}.json",
                      json.dumps(data, ensure_ascii=False, indent=1))

    def agents(self, max_age_sec: int = 30) -> list[dict]:
        cutoff = datetime.now() - timedelta(seconds=max_age_sec)
        out = []
        for p in (self.root / "agents").glob("*.json"):
            rec = _read_json(p)
            if not rec:
                continue
            try:
                fresh = datetime.fromisoformat(rec.get("at") or "") >= cutoff
            except Exception:                                  # noqa: BLE001
                fresh = False
            rec["alive"] = fresh
            out.append(rec)
        return sorted(out, key=lambda r: r.get("at") or "", reverse=True)


def get_store(backend: str = "") -> JobStore:
    """`BLOG_QUEUE_BACKEND` (local | supabase). 지금은 local 만 있다.

    Supabase 를 붙일 때 여기 분기 하나와 `SupabaseStore` 만 추가하면
    UI(`streamlit_app.py`)와 에이전트(`v2.agent`)는 고치지 않는다.
    """
    name = (backend or os.getenv("BLOG_QUEUE_BACKEND") or "local").strip().lower()
    if name in ("", "local", "file"):
        return LocalStore()
    if name in ("supabase", "remote"):
        # ★원격 큐 — Streamlit Cloud 와 각 PC 의 Agent 가 같은 큐를 본다.
        #   설정이 없거나 연결이 안 되면 **로컬 큐로 조용히 되돌아간다**(기존 동작 보존).
        try:
            from .supabase_store import SupabaseStore
            return SupabaseStore()
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"[큐] Supabase 연결 실패 — 로컬 큐로 진행합니다: {exc}",
                  file=sys.stderr)
            return LocalStore()
    raise RuntimeError(f"아직 없는 큐 백엔드입니다: {name!r} (local | supabase)")
