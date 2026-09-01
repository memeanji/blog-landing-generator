r"""원격 작업 큐 — Supabase 판 `JobStore`.

    Streamlit Cloud ──service key──▶ 테이블 직접        (SupabaseStore mode="service")
    Windows Agent   ──publishable key + device_token──▶ RPC (SupabaseStore mode="device")

★계약(`queue_store.JobStore`)만 채운다. UI(`streamlit_app.py`)와 에이전트(`v2.agent`)는
  이 파일이 생겼다는 사실조차 몰라도 된다 — `get_store()` 가 골라 준다.

★Agent 에는 service key 를 넣지 않는다. 아래 device 모드가 쓰는 값은
  ① 공개해도 되는 publishable key ② 자기 device_token 뿐이고,
  모든 접근은 토큰을 검증하는 DB 함수(SECURITY DEFINER)를 지난다.
  그래서 **남의 device 작업은 가져갈 수 없다**(schema.sql 참고).

환경변수
    SUPABASE_URL                  https://<ref>.supabase.co
    SUPABASE_SERVICE_KEY          (Streamlit 서버 전용. Agent 에는 절대 넣지 않는다)
    SUPABASE_PUBLISHABLE_KEY      (Agent 용. 공개돼도 되는 키)
    BLOG_DEVICE_TOKEN             (Agent 가 페어링 후 저장한 자기 토큰)
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .job import Job
from .queue_store import (CANCELED, DONE, FAILED, JobStore, PENDING, RUNNING,
                          host_name, now_iso)

TIMEOUT = 20
LOG_FLUSH_LINES = 25          # 로그를 이만큼 모으면 보낸다(HTTP 왕복 줄이기)
LOG_FLUSH_SEC = 1.5


class SupabaseError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _iso(value) -> str:
    """timestamptz 문자열 → 화면이 쓰는 'YYYY-MM-DDTHH:MM:SS'(로컬시각)."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="seconds")[:19]
    except Exception:                                          # noqa: BLE001
        return str(value)[:19]


class SupabaseStore(JobStore):
    """`LocalStore` 와 **똑같은 레코드 모양**을 돌려준다(화면 코드를 안 고치기 위해)."""

    def __init__(self, mode: str = "", url: str = "", key: str = "",
                 device_token: str = "") -> None:
        import requests                                        # 지연 import

        self._requests = requests
        self.url = (url or _env("SUPABASE_URL")).rstrip("/")
        self.device_token = device_token or _env("BLOG_DEVICE_TOKEN")
        self.mode = mode or ("device" if self.device_token else "service")
        if self.mode == "device":
            self.key = key or _env("SUPABASE_PUBLISHABLE_KEY") or _env("SUPABASE_ANON_KEY")
        else:
            self.key = (key or _env("SUPABASE_SERVICE_KEY")
                        or _env("SUPABASE_SERVICE_ROLE_KEY"))
        if not self.url or not self.key:
            raise SupabaseError(
                "Supabase 설정이 없습니다 — SUPABASE_URL 과 "
                + ("SUPABASE_PUBLISHABLE_KEY" if self.mode == "device"
                   else "SUPABASE_SERVICE_KEY") + " 를 확인하세요.")
        if self.mode == "device" and not self.device_token:
            raise SupabaseError("device 모드에는 BLOG_DEVICE_TOKEN 이 필요합니다.")

        self._buf: dict[str, list[str]] = {}
        self._buf_at: dict[str, float] = {}
        self._lock = threading.Lock()

    # ── 저수준 ───────────────────────────────────────────────────
    def _headers(self, extra: dict | None = None) -> dict:
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}",
             "Content-Type": "application/json"}
        h.update(extra or {})
        return h

    def _rest(self, method: str, path: str, **kw) -> Any:
        r = self._requests.request(method, f"{self.url}/rest/v1/{path}",
                                   headers=self._headers(kw.pop("headers", None)),
                                   timeout=TIMEOUT, **kw)
        if not r.ok:
            raise SupabaseError(f"{method} {path} 실패({r.status_code}): {r.text[:200]}")
        if not r.content:
            return None
        try:
            return r.json()
        except Exception:                                      # noqa: BLE001
            return None

    def _rpc(self, fn: str, payload: dict) -> Any:
        """Agent 전용 — 토큰을 검증하는 DB 함수만 호출한다."""
        body = {"p_token": self.device_token, **payload}
        r = self._requests.post(f"{self.url}/rest/v1/rpc/{fn}",
                                headers=self._headers(), json=body, timeout=TIMEOUT)
        if not r.ok:
            raise SupabaseError(f"rpc {fn} 실패({r.status_code}): {r.text[:200]}")
        try:
            return r.json()
        except Exception:                                      # noqa: BLE001
            return None

    # ── 레코드 모양 맞추기(LocalStore 호환) ──────────────────────
    @staticmethod
    def _rec(row: dict | None) -> dict | None:
        if not row:
            return None
        return {
            "id": row.get("job_id"),
            "kind": row.get("kind") or "run",
            "brand": row.get("brand") or "",
            "title": row.get("title") or "",
            "status": row.get("status") or PENDING,
            "created_at": _iso(row.get("created_at")),
            "created_by": "",
            "target_agent": row.get("device_id") or "",
            "job": row.get("payload") or {},
            "agent": row.get("device_id") or "",
            "pid": None,
            "started_at": _iso(row.get("started_at")),
            "finished_at": _iso(row.get("finished_at")),
            "exit_code": row.get("exit_code"),
            "total": int(row.get("total") or 0),
            "made": int(row.get("made") or 0),
            "published": row.get("published") or [],
            "error": row.get("error") or "",
            "command": row.get("command") or "",
        }

    # ══ UI 쪽 (service 모드) ═════════════════════════════════════
    def submit(self, job: Job, *, kind: str = "run", title: str = "",
               created_by: str = "", target_agent: str = "") -> str:
        if self.mode != "service":
            raise SupabaseError("작업 등록은 UI(service 모드)에서만 할 수 있습니다.")
        if not target_agent:
            raise SupabaseError("실행할 PC(device_id)가 지정되지 않았습니다. "
                                "먼저 Agent 를 연결해 주세요.")
        brand = (getattr(job, "brand", "") or "").strip()
        row = {
            "device_id": target_agent,
            "kind": kind if kind in ("run", "session") else "run",
            "brand": brand,
            "title": title or (f"[{brand}] " if brand else "")
                     + f"{job.flow} · {job.media} / {job.deficiency}",
            "payload": job.to_dict(),
        }
        got = self._rest("POST", "jobs", json=row,
                         headers={"Prefer": "return=representation"})
        return (got or [{}])[0].get("job_id", "")

    def get(self, job_id: str) -> dict | None:
        if not job_id:
            return None
        rows = self._rest("GET", f"jobs?job_id=eq.{job_id}&limit=1")
        return self._rec((rows or [None])[0])

    def list_jobs(self, limit: int = 30) -> list[dict]:
        rows = self._rest("GET", f"jobs?order=created_at.desc&limit={int(limit)}")
        return [self._rec(r) for r in (rows or [])]

    def request_cancel(self, job_id: str) -> None:
        self._rest("PATCH", f"jobs?job_id=eq.{job_id}",
                   json={"cancel_requested": True})

    def read_log(self, job_id: str, offset: int = 0) -> tuple[str, int]:
        rows = self._rest(
            "GET", f"job_logs?job_id=eq.{job_id}&order=id.asc"
                   f"&offset={int(offset)}&limit=2000&select=line") or []
        return "\n".join(r["line"] for r in rows) + ("\n" if rows else ""), \
            int(offset) + len(rows)

    def read_events(self, job_id: str, after: int = 0) -> tuple[list[dict], int]:
        rows = self._rest(
            "GET", f"job_events?job_id=eq.{job_id}&order=id.asc"
                   f"&offset={int(after)}&limit=2000&select=event") or []
        return [r["event"] for r in rows], int(after) + len(rows)

    # ══ Agent 쪽 (device 모드 = RPC 전용) ════════════════════════
    def claim(self, agent_id: str) -> dict | None:
        if self.mode != "device":
            return None                      # UI 는 작업을 집지 않는다
        row = self._rpc("claim_job", {})
        return self._rec(row) if row else None

    def update(self, job_id: str, **fields) -> dict | None:
        if self.mode != "device":
            self._rest("PATCH", f"jobs?job_id=eq.{job_id}", json=fields)
            return None
        keep = {k: v for k, v in fields.items()
                if k in ("total", "made", "published", "error", "command")}
        if keep:
            self._rpc("update_job", {"p_job": job_id, "p_fields": keep})
        return None

    def finish(self, job_id: str, status: str, exit_code: int = 0) -> dict | None:
        self.flush_logs(job_id)
        if self.mode == "device":
            self._rpc("finish_job", {"p_job": job_id, "p_status": status,
                                     "p_exit": int(exit_code)})
        else:
            self._rest("PATCH", f"jobs?job_id=eq.{job_id}",
                       json={"status": status, "exit_code": int(exit_code),
                             "finished_at": datetime.now(timezone.utc).isoformat()})
        return None

    def append_log(self, job_id: str, line: str) -> None:
        """줄마다 왕복하면 느리다 — 모아서 보낸다(끝날 때 flush)."""
        with self._lock:
            buf = self._buf.setdefault(job_id, [])
            buf.append(line)
            first = self._buf_at.setdefault(job_id, time.time())
            ready = len(buf) >= LOG_FLUSH_LINES or (time.time() - first) >= LOG_FLUSH_SEC
        if ready:
            self.flush_logs(job_id)

    def flush_logs(self, job_id: str) -> None:
        with self._lock:
            lines = self._buf.pop(job_id, [])
            self._buf_at.pop(job_id, None)
        if not lines:
            return
        try:
            if self.mode == "device":
                self._rpc("append_log", {"p_job": job_id, "p_lines": lines})
            else:
                self._rest("POST", "job_logs",
                           json=[{"job_id": job_id, "line": ln} for ln in lines])
        except Exception:                                      # noqa: BLE001
            pass                                               # 로그 때문에 실행이 멈추면 안 된다

    def append_event(self, job_id: str, event: dict) -> None:
        try:
            if self.mode == "device":
                self._rpc("append_event", {"p_job": job_id, "p_event": event})
            else:
                self._rest("POST", "job_events",
                           json={"job_id": job_id, "event": event})
        except Exception:                                      # noqa: BLE001
            pass

    def cancel_requested(self, job_id: str) -> bool:
        try:
            if self.mode == "device":
                return bool(self._rpc("cancel_requested", {"p_job": job_id}))
            rows = self._rest("GET", f"jobs?job_id=eq.{job_id}"
                                     f"&select=cancel_requested&limit=1")
            return bool((rows or [{}])[0].get("cancel_requested"))
        except Exception:                                      # noqa: BLE001
            return False

    def heartbeat(self, agent_id: str, **info) -> None:
        if self.mode != "device":
            return
        try:
            self._rpc("agent_heartbeat",
                      {"p_state": info.get("state") or "idle",
                       "p_job": info.get("job") or None,
                       "p_version": str(info.get("version") or "")})
            # 이름이 바뀌었으면 함께 알린다(서버에 함수가 있을 때만 동작)
            label = str(info.get("label") or "")
            if label and label != getattr(self, "_last_label", ""):
                try:
                    self._rpc("rename_device", {"p_label": label})
                    self._last_label = label
                except Exception:                              # noqa: BLE001
                    self._last_label = label
        except Exception:                                      # noqa: BLE001
            pass

    def agents(self, max_age_sec: int = 30) -> list[dict]:
        """UI 가 '내 PC Agent 연결됨' 을 보여줄 때 쓴다(service 모드)."""
        if self.mode != "service":
            return []
        rows = self._rest("GET", "devices?order=last_seen.desc&limit=50") or []
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)
        out = []
        for r in rows:
            try:
                fresh = datetime.fromisoformat(
                    str(r["last_seen"]).replace("Z", "+00:00")) >= cutoff
            except Exception:                                  # noqa: BLE001
                fresh = False
            out.append({"agent": r.get("device_id"), "device_id": r.get("device_id"),
                        "host": r.get("label") or "", "label": r.get("label") or "",
                        "state": r.get("state") or "idle",
                        "job": r.get("current_job") or "",
                        "version": r.get("agent_version") or "",
                        "at": _iso(r.get("last_seen")), "alive": fresh})
        return out

    # ── UI 전용 부가기능(페어링) ─────────────────────────────────
    def create_pairing(self, minutes: int = 10) -> dict:
        """6자리 1회용 코드를 만든다(service 모드)."""
        import random

        if self.mode != "service":
            raise SupabaseError("페어링 코드 발급은 UI 에서만 합니다.")
        expires = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        for _ in range(20):
            code = f"{random.randint(0, 999999):06d}"
            try:
                self._rest("POST", "pairings",
                           json={"code": code, "expires_at": expires})
                return {"code": code, "expires_at": expires}
            except SupabaseError as exc:
                if "duplicate" in str(exc).lower() or "23505" in str(exc):
                    continue
                raise
        raise SupabaseError("페어링 코드를 만들지 못했습니다. 잠시 뒤 다시 시도하세요.")

    def pairing_result(self, code: str) -> dict | None:
        """코드가 사용됐으면 연결된 device 를 돌려준다(UI 폴링용)."""
        rows = self._rest("GET", f"pairings?code=eq.{code}&limit=1") or []
        row = rows[0] if rows else None
        if not row or not row.get("device_id"):
            return None
        dev = (self._rest("GET", f"devices?device_id=eq.{row['device_id']}&limit=1")
               or [None])[0]
        if not dev:
            return None
        return {"device_id": dev["device_id"], "label": dev.get("label") or "",
                "last_seen": _iso(dev.get("last_seen")),
                "version": dev.get("agent_version") or ""}

    def device(self, device_id: str) -> dict | None:
        if not device_id:
            return None
        rows = self._rest("GET", f"devices?device_id=eq.{device_id}&limit=1") or []
        if not rows:
            return None
        d = rows[0]
        alive = False
        try:
            alive = (datetime.fromisoformat(str(d["last_seen"]).replace("Z", "+00:00"))
                     >= datetime.now(timezone.utc) - timedelta(seconds=60))
        except Exception:                                      # noqa: BLE001
            pass
        return {"device_id": d["device_id"], "label": d.get("label") or "",
                "state": d.get("state") or "idle", "alive": alive,
                "version": d.get("agent_version") or "",
                "last_seen": _iso(d.get("last_seen"))}


def pair(url: str, key: str, code: str, label: str = "",
         version: str = "") -> dict:
    """Agent 최초 실행 — 6자리 코드로 device_id·device_token 을 발급받는다.

    ★여기서만 publishable key 를 쓴다. 발급된 토큰은 그 PC 에만 저장한다.
    """
    import requests

    r = requests.post(f"{url.rstrip('/')}/rest/v1/rpc/pair_device",
                      headers={"apikey": key, "Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json={"p_code": code, "p_label": label or socket.gethostname(),
                            "p_version": version},
                      timeout=TIMEOUT)
    if not r.ok:
        raise SupabaseError(f"페어링 실패({r.status_code}): {r.text[:200]}")
    data = r.json()
    if not data or not data.get("device_token"):
        raise SupabaseError("페어링 응답에 토큰이 없습니다.")
    return data
