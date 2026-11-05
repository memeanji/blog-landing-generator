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
from .queue_store import (CANCELED, DONE, FAILED, HELD, JobStore, PartialHoldError,
                          PENDING, RUNNING, host_name, matches, now_iso)

TIMEOUT = 20

#  화면(Streamlit) 전용 타임아웃 — Agent 는 위 TIMEOUT(20초)을 그대로 쓴다
#  (2026-11-05). 사람이 화면 앞에서 기다리는 조회는 더 짧게 끊는다.
#  ★`deadline` 을 받은 조회에만 적용된다 → Agent 경로는 영향이 없다.
UI_TIMEOUT = 8

#  deadline 이 임박했을 때도 요청에 주는 최소 시간. 남은 예산이 이보다 적으면
#  요청을 **시작하지 않는다**(예산 확인이 먼저 걸린다). 0.5초 같은 값으로
#  요청을 보내면 멀쩡한 서버도 타임아웃으로 몰아 거짓 차단이 늘어난다.
MIN_REQ_TIMEOUT = 2.0
LOG_FLUSH_LINES = 25          # 로그를 이만큼 모으면 보낸다(HTTP 왕복 줄이기)
LOG_FLUSH_SEC = 1.5


class SupabaseError(RuntimeError):
    """Supabase 호출 실패.

    ★`definite_failure=True` 는 **서버에 아무것도 쓰이지 않았음이 확실**하다는 뜻이다
      (요청을 보내기도 전에 막힌 경우, 또는 서버가 4xx 로 거부한 경우).
      타임아웃·연결 끊김·5xx 는 **도착해서 처리됐을 수도 있으므로** 확실하지 않다
      — 그때 '등록 실패' 로 단정하면 사용자가 다시 넣어 **중복 발행**이 된다.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 definite_failure: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.definite_failure = definite_failure


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
        # ★마지막으로 **성공적으로** 주고받은 시각(화면의 '연결 상태' 표시용).
        #   객체를 만든 것만으로는 채우지 않는다 — 설정만 맞고 키가 틀린 상태를
        #   '연결됨' 으로 보이게 하면 안 된다.
        self._last_ok = ""

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
            # ★4xx = 서버가 **받아서 거부**했다 → 행이 생기지 않았음이 확실하다.
            #   5xx·네트워크 예외는 처리됐을 수도 있어 확실하지 않다(아래 except 로 빠짐).
            raise SupabaseError(
                f"{method} {path} 실패({r.status_code}): {r.text[:200]}",
                status=r.status_code,
                definite_failure=400 <= r.status_code < 500)
        self._last_ok = now_iso()
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
        self._last_ok = now_iso()
        try:
            return r.json()
        except Exception:                                      # noqa: BLE001
            return None

    # ── 연결 확인 (읽기 1회 · 작업을 집거나 바꾸지 않는다) ────────
    def probe(self) -> bool:
        """service 모드에서 테이블을 1행만 읽어 **정말 붙었는지** 확인한다.

        ★device 모드(Agent)는 `strict` 로 부르지 않으므로 건너뛴다 — 토큰 검증
          RPC 는 작업을 집는 것(claim)뿐이라 확인용으로 부를 수 없다.
        """
        if self.mode != "service":
            return True
        self._rest("GET", "jobs?select=job_id&limit=1")
        return True

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
            #  ★중단 '요청' 이 걸려 있는가. 예전에는 이 값을 버려서 화면이
            #    '중단을 눌렀는데 아직 도는 중' 과 '그냥 도는 중' 을 구분하지
            #    못했다. 조회 쿼리는 열을 좁히지 않으므로 그냥 실어 주면 된다.
            "cancel_requested": bool(row.get("cancel_requested")),
        }

    # ══ UI 쪽 (service 모드) ═════════════════════════════════════
    def submit(self, job: Job, *, kind: str = "run", title: str = "",
               created_by: str = "", target_agent: str = "",
               client_key: str = "") -> str:
        # ★아래 두 검사는 **요청을 보내기 전**에 막는다 → 등록 실패가 확실하다.
        if self.mode != "service":
            raise SupabaseError("작업 등록은 UI(service 모드)에서만 할 수 있습니다.",
                                definite_failure=True)
        if not target_agent:
            raise SupabaseError("실행할 PC(device_id)가 지정되지 않았습니다. "
                                "먼저 Agent 를 연결해 주세요.",
                                definite_failure=True)
        brand = (getattr(job, "brand", "") or "").strip()
        payload = job.to_dict()
        if client_key:
            # ★멱등성 키를 payload 에 넣는다(컬럼 추가 없이 조회 가능).
            #   `job_id` 는 서버가 만들기 때문에, 응답이 유실되면 이 키 말고는
            #   방금 넣은 작업을 되찾을 방법이 없다. 옛 Agent 는 `Job.from_dict` 가
            #   모르는 키를 버리므로 영향이 없다.
            payload = {**payload, "client_key": client_key}
        row = {
            "device_id": target_agent,
            "kind": kind if kind in ("run", "session") else "run",
            "brand": brand,
            "title": title or (f"[{brand}] " if brand else "")
                     + f"{job.flow} · {job.media} / {job.deficiency}",
            "payload": payload,
        }
        got = self._rest("POST", "jobs", json=row,
                         headers={"Prefer": "return=representation"})
        return (got or [{}])[0].get("job_id", "")

    def find_by_client_key(self, client_key: str) -> dict | None:
        """멱등성 키로 이미 등록된 작업을 찾는다(재제출 대신 조회)."""
        key = (client_key or "").strip()
        if not key:
            return None
        if self.mode != "service":
            raise NotImplementedError("device 모드에서는 작업을 조회하지 않는다")
        from urllib.parse import quote

        rows = self._rest(
            "GET", f"jobs?payload->>client_key=eq.{quote(key, safe='')}"
                   f"&order=created_at.desc&limit=1") or []
        return self._rec(rows[0]) if rows else None

    # ── 계정별 보류 (Stage 2) ────────────────────────────────────
    #   ★한 번의 **조건부 UPDATE** 로 끝낸다 — "status 가 아직 pending 인 행만"
    #     바꾸므로, 그 순간 Agent 가 집어간 작업(running)은 조건에 걸리지 않고
    #     그대로 끝까지 간다. 경합이 생기지 않는다(원자성은 DB 가 보장한다).
    #   ★`payload->>login_id` 로만 고른다 — blog_id 로 묶지 않는다.
    def _switch_status(self, login_id: str, frm: str, to: str,
                       device: str = "", account_id: str = "") -> list[str]:
        """`frm` 상태인 그 계정의 작업을 `to` 로 바꾼다. 바뀐 job_id 목록.

        ★①바꿀 대상을 **상태로 좁혀 조회**해 `matches`(login_id 정규화)로 고르고,
          ②`job_id` 목록 + `status=eq.<frm>` 조건으로 PATCH 한다.
          조건이 그대로 붙어 있으므로 **원자성은 그대로다** — 그 사이 Agent 가
          집어 간 작업(running)은 조건에 걸리지 않고 끝까지 간다.
        ★예전에는 `payload->>login_id=eq.…` 로 한 번에 PATCH 했다. 서버 필터는
          문자열 정확일치라 `shin894894` / `shin894894@naver.com` 표기가 어긋나면
          **한 건도 못 고르고 '보류 0건' 으로 조용히 끝났다.** 그래서 고른다.
        ★kind=run 만 — 로그인/세션확인/로그아웃 작업은 보류하지 않는다
          (보류하면 사용자가 재로그인으로 복구할 길이 막힌다).
        ★blog_id 로는 절대 고르지 않는다.
        """
        from urllib.parse import quote

        lid, aid = (login_id or "").strip(), (account_id or "").strip()
        if not lid and not aid:
            return []                         # 식별자가 없으면 아무것도 건드리지 않는다
        if self.mode != "service":
            raise SupabaseError("작업 보류/재개는 UI(service 모드)에서만 합니다.",
                                definite_failure=True)

        want = self.list_by_status(frm, lid, aid, kind="run")
        ids = [str(r.get("id")) for r in want if r.get("id")
               and (not device or str(r.get("target_agent") or "") == device)]
        if not ids:
            return []

        done: list[str] = []
        for i in range(0, len(ids), 50):      # URL 이 길어지지 않게 나눠 보낸다
            chunk = ids[i:i + 50]
            lst = ",".join(quote(x, safe="") for x in chunk)
            q = f"jobs?job_id=in.({lst})&status=eq.{quote(frm, safe='')}"
            try:
                rows = self._rest("PATCH", q, json={"status": to},
                                  headers={"Prefer": "return=representation"}) or []
            except Exception as exc:                           # noqa: BLE001
                # ★앞 묶음은 **이미 서버에 반영돼 있다.** 몇 건이 정말 바뀌었는지
                #   다시 조회해 확인하고, 그 사실을 예외에 실어 올린다
                #   (빈손으로 올리면 화면이 '하나도 못 했다' 로 잘못 안내한다).
                verified = None
                try:
                    verified = [str(r.get("id")) for r
                                in self.list_by_status(to, lid, aid, kind="run")
                                if r.get("id")]
                except Exception:                              # noqa: BLE001
                    verified = None            # 재조회 실패 = **결과 확인 불가**
                raise PartialHoldError(
                    f"{frm}→{to} 변경을 일부만 반영한 채 실패했습니다: "
                    f"{type(exc).__name__}: {exc}",
                    done=done, verified=verified, remaining=ids[i:],
                    cause=exc) from exc
            done += [str(r.get("job_id")) for r in rows if r.get("job_id")]
        return done

    def hold_account(self, login_id: str = "", *, device: str = "",
                     account_id: str = "") -> list[str]:
        return self._switch_status(login_id, PENDING, HELD, device,
                                   account_id=account_id)

    def resume_account(self, login_id: str = "", *, device: str = "",
                       account_id: str = "") -> list[str]:
        return self._switch_status(login_id, HELD, PENDING, device,
                                   account_id=account_id)

    def get(self, job_id: str) -> dict | None:
        if not job_id:
            return None
        rows = self._rest("GET", f"jobs?job_id=eq.{job_id}&limit=1")
        return self._rec((rows or [None])[0])

    def list_jobs(self, limit: int = 30) -> list[dict]:
        rows = self._rest("GET", f"jobs?order=created_at.desc&limit={int(limit)}")
        return [self._rec(r) for r in (rows or [])]

    # ══ 상태별 조회 — 서버 필터 + 실제 페이지네이션 (2026-11-04) ════
    #   ★왜 이렇게까지 하나 — 이 조회 결과가 '보류 없음' 이면 화면은 그 계정의
    #     **새 발행을 허용**한다. 그래서 "못 읽었다" 와 "0건" 을 절대 섞으면 안 되고,
    #     조회가 중간에서 잘려도 0건으로 보여선 안 된다.
    #   ① 서버에서 status(+kind)로 좁히고, 식별자도 **서버에서** 거른다.
    #      login_id 는 표기가 두 가지(`a` / `a@naver.com`)이고 대소문자도 섞이므로
    #      `or=(…ilike.a, …ilike.a@naver.com, …)` 로 후보를 모두 넣는다.
    #      ※ LIKE 에서 `_` 는 한 글자 와일드카드다 — `shin894894_2` 같은 id 는
    #        남의 행까지 끌어올 수 있다. 그래서 받아온 뒤 `matches` 로 **다시** 거른다
    #        (서버 필터는 '덜 가져오는' 쪽이 아니라 '더 가져오는' 쪽으로만 틀린다).
    #   ② `offset` 페이지네이션으로 **끝까지** 읽는다. 정렬키는 고정
    #      (`created_at.desc,job_id.desc`) — 안 그러면 페이지가 흔들려 누락이 생긴다.
    #   ③ `count=exact` 로 총건수를 받아 **받은 수와 맞는지 검증**한다. 모자라면
    #      한 번 다시 읽고, 그래도 모자라면 **예외**를 던진다(→ 화면은 fail-safe).
    PAGE = 200                      # 한 번에 받는 행 수
    MAX_PAGES = 100                 # 20,000행까지. 그보다 많으면 예외(조용히 자르지 않는다)

    def _login_candidates(self, login_id: str) -> list[str]:
        """서버 필터에 넣을 login_id 표기 후보(정규화 규칙과 같은 집합)."""
        from .loginid import NAVER_MAIL_DOMAINS, norm_login

        bare = norm_login(login_id)
        if not bare:
            return []
        out = [bare] + [bare + d for d in NAVER_MAIL_DOMAINS]
        return list(dict.fromkeys(out))

    def _status_query(self, status: str, login_id: str = "",
                      account_id: str = "", kind: str = "") -> str:
        from urllib.parse import quote

        q = (f"jobs?status=eq.{quote(status, safe='')}"
             f"&order=created_at.desc,job_id.desc")
        if kind:
            q += f"&kind=eq.{quote(kind, safe='')}"
        lid, aid = (login_id or "").strip(), (account_id or "").strip()
        if lid:
            cands = self._login_candidates(lid)
            if cands:
                ors = ",".join(f"payload->>login_id.ilike.{quote(c, safe='')}"
                               for c in cands)
                q += f"&or=({ors})"
        elif aid:
            q += f"&payload->>account=eq.{quote(aid, safe='')}"
        return q

    def _rest_range(self, query: str, count: bool = False,
                    timeout: float | None = None
                    ) -> tuple[list[dict], int | None]:
        """GET 한 번으로 `(행들, 총건수)` 를 함께 받는다 (2026-11-04).

        ★왜 따로 만들었나 — 예전에는 총건수를 세는 요청(`count=exact`)과 행을 받는
          요청을 **따로** 보내 상태 1종마다 2회, 세 상태에 6회가 나갔다. 운영
          Supabase 는 statement timeout·connection pool 고갈 이력이 있어 요청 수를
          줄이는 것이 안전에 직접 도움이 된다.
          PostgREST 는 `Prefer: count=exact` 를 주면 **같은 응답**의
          `Content-Range: 0-199/512` 헤더로 총건수를 알려준다 → 1회로 합칠 수 있다.
        ★같은 응답에서 온 수이므로 '행과 총건수의 스냅샷이 어긋나는' 문제도 줄어든다.
        ★오류 처리는 `_rest` 와 같다 — 4xx/5xx·네트워크 예외를 그대로 올린다
          (호출한 쪽이 '확인 불가' 로 다뤄 새 발행을 막는다).
        """
        r = self._requests.request(
            "GET", f"{self.url}/rest/v1/{query}",
            headers=self._headers({"Prefer": "count=exact",
                                   "Range-Unit": "items"} if count else None),
            timeout=(TIMEOUT if timeout is None else timeout))
        if not r.ok:
            raise SupabaseError(
                f"GET {query} 실패({r.status_code}): {r.text[:200]}",
                status=r.status_code,
                definite_failure=400 <= r.status_code < 500)
        self._last_ok = now_iso()
        try:
            rows = r.json() or []
        except Exception:                                      # noqa: BLE001
            rows = []
        total = None
        tail = (r.headers.get("Content-Range") or "").split("/")[-1].strip()
        if tail.isdigit():
            total = int(tail)
        return (rows if isinstance(rows, list) else []), total

    def _page_all(self, query: str, cap: int = 0,
                  deadline: float | None = None
                  ) -> tuple[list[dict], int | None, bool]:
        """페이지를 끝까지 읽어 `(레코드, 총건수, 더 남았나)`. job_id 로 중복 제거.

        ★'끝까지 읽었다' 고 말할 수 있는 근거는 **둘뿐**이다
            ① 총건수를 받았고 그만큼 다 모았다
            ② 페이지가 **0행**으로 돌아왔다(그 뒤에 아무것도 없다)
        ★"요청한 limit 보다 적게 왔다" 는 근거가 **아니다** — PostgREST 의
          `db-max-rows` 가 limit 보다 작게 설정돼 있으면 매 페이지가 짧게 오고,
          그걸 '전수' 로 믿으면 오래된 보류를 놓친다. 그래서 짧은 페이지가 와도
          총건수를 모르면 **다음 페이지를 한 번 더** 확인한다.
        ★`deadline`(monotonic 초)을 주면 **요청 사이에서** 남은 시간을 확인하고,
          넘었으면 `SupabaseError` 를 던진다 — 모은 것까지만 돌려주지 **않는다**
          (부분 결과를 성공으로 쓰면 '보류 없음' 오판이 생긴다, 2026-11-05).
          진행 중인 요청은 끊을 수 없으므로 실제 상한은 `deadline + 요청 1회` 다.
        """
        from time import monotonic

        seen: dict[str, dict] = {}
        total: int | None = None
        off = 0
        for page in range(self.MAX_PAGES):
            req_timeout = None
            if deadline is not None:
                left = deadline - monotonic()
                if left <= 0:
                    raise SupabaseError(
                        "조회 시간이 예산을 넘겨 중단했습니다 — 결과를 "
                        "신뢰할 수 없습니다(부분 결과를 쓰지 않습니다).")
                req_timeout = max(MIN_REQ_TIMEOUT, min(UI_TIMEOUT, left))
            rows, got = self._rest_range(
                f"{query}&offset={off}&limit={self.PAGE}", count=(page == 0),
                timeout=req_timeout)
            if page == 0:
                total = got
            for raw in rows:
                rec = self._rec(raw)
                if rec and rec.get("id"):
                    seen[str(rec["id"])] = rec       # ★같은 job_id 는 한 번만
            if not rows:
                return list(seen.values()), total, False       # ② 끝(확실)
            if total is not None and len(seen) >= total:
                return list(seen.values()), total, False       # ① 끝(확실)
            if cap and len(seen) >= cap:
                return list(seen.values())[:cap], total, True
            # ★다음 offset 은 **받은 행 수만큼** 전진시킨다(요청한 크기가 아니다).
            #   PostgREST 의 `db-max-rows` 가 우리 limit 보다 작으면 응답이 잘려
            #   오는데, 요청 크기만큼 뛰면 그 사이 구간을 **건너뛴다**
            #   (실측: cap=100·limit=200·총 250건 → 150건만 받고 조용히 끝났다).
            off += len(rows)
        return list(seen.values()), total, True                # 상한 초과

    def list_by_status(self, status: str, login_id: str = "",
                       account_id: str = "", kind: str = "",
                       limit: int = 0,
                       deadline: float | None = None) -> list[dict]:
        """그 상태의 작업을 **빠짐없이** 돌려준다(식별자를 주면 그 계정만).

        ★못 읽거나 중간에서 잘리면 **예외**를 던진다 — 빈 목록으로 돌려주지 않는다.
          호출한 쪽(화면)은 그것을 '확인 불가' 로 다루고 새 발행을 막는다.
        ★`deadline`(monotonic 초)은 화면이 렌더 1회에 쓸 조회 예산이다. 주면
          요청마다 `UI_TIMEOUT`(8초) 이하로 끊고 요청 사이에서 예산을 확인한다.
          **주지 않으면 지금까지와 완전히 같다**(Agent 경로 = TIMEOUT 20초).
        """
        want = (status or "").strip()
        if not want:
            return []
        query = self._status_query(want, login_id, account_id, kind)

        recs, total, more = self._page_all(query, cap=int(limit or 0),
                                           deadline=deadline)
        if more:
            raise SupabaseError(
                f"{want} 작업이 너무 많아 끝까지 읽지 못했습니다"
                f"({self.MAX_PAGES * self.PAGE}행 초과) — 결과를 신뢰할 수 없습니다.")
        if total is not None and len(recs) < total:
            # 그 사이 상태가 바뀌어 수가 어긋날 수 있다 → **한 번 다시** 읽어 본다.
            recs2, total2, more2 = self._page_all(query, cap=int(limit or 0),
                                                  deadline=deadline)
            if more2:
                raise SupabaseError(
                    f"{want} 작업이 너무 많아 끝까지 읽지 못했습니다 — "
                    f"결과를 신뢰할 수 없습니다.")
            if total2 is not None and len(recs2) < total2:
                raise SupabaseError(
                    f"{want} 작업 조회가 중간에서 잘렸습니다 — 서버 {total2}건 중 "
                    f"{len(recs2)}건만 받았습니다. '보류 없음' 으로 단정하지 않습니다.")
            recs = recs2

        both = not (login_id or "").strip() and not (account_id or "").strip()
        recs.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)
        if both:
            return recs
        #  ★서버 필터(ilike)는 느슨하다 → 정규화 규칙으로 **다시** 거른다.
        return [r for r in recs if matches(r, login_id, account_id)]

    def accounts_list(self) -> list[dict]:
        """블로그 계정 목록(중앙 관리) — **읽기 전용**.

        ★device 토큰을 검증하는 DB 함수만 부른다(`schema_accounts.sql`).
          Agent 에는 service_role key 를 넣지 않는다. 토큰이 틀리면 0행이 온다.
          비활성 계정도 그대로 돌려준다 — 거르는 건 `accounts.load_accounts` 다.
        """
        return list(self._rpc("accounts_list", {}) or [])

    def request_cancel(self, job_id: str) -> None:
        self._rest("PATCH", f"jobs?job_id=eq.{job_id}",
                   json={"cancel_requested": True})

    def cancel_pending(self, job_id: str) -> bool:
        """대기 상태일 때만 그 자리에서 `canceled` 로 바꾼다 → 바뀌었으면 True.

        ★`_switch_status`(보류/재개)와 **같은 방식**이다 — `status=eq.pending` 을
          조건으로 붙여 PATCH 하고 `Prefer: return=representation` 으로 **실제로
          바뀐 행**을 돌려받는다. 그 사이 Agent 가 집어 간 작업(running)은 조건에
          걸리지 않으므로 바뀌지 않고, 돌아온 행이 없으니 False 가 된다.
        ★돌아온 행 수를 **추측하지 않는다.** 0행이면 호출한 쪽이 `request_cancel`
          로 요청을 남기고, 화면은 '중단됨' 이 아니라 '중단 요청함' 이라고 말한다.
        """
        jid = (job_id or "").strip()
        if not jid:
            return False
        if self.mode != "service":
            raise SupabaseError("대기 작업 취소는 UI(service 모드)에서만 합니다.",
                                definite_failure=True)
        from urllib.parse import quote

        q = (f"jobs?job_id=eq.{quote(jid, safe='')}"
             f"&status=eq.{quote(PENDING, safe='')}")
        rows = self._rest("PATCH", q,
                          json={"status": CANCELED, "cancel_requested": True,
                                "exit_code": 130},
                          headers={"Prefer": "return=representation"}) or []
        return bool(rows)

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
        """6자리 1회용 코드를 만든다(service 모드).

        ★난수는 `secrets`(CSPRNG)로 뽑는다 — 예전 `random.randint` 는 메르센 트위스터라
          앞선 값 몇 개를 알면 다음 값을 예측할 수 있다. 이 코드는 그 자체가 인증값은
          아니지만 **맞히면 device_token 을 받아 가는 관문**이라 추측 가능하면 안 된다.
          (무차별 대입은 DB 쪽 `pair_device` 스로틀이 함께 막는다.)
        """
        import secrets

        if self.mode != "service":
            raise SupabaseError("페어링 코드 발급은 UI 에서만 합니다.")
        expires = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        for _ in range(20):
            code = f"{secrets.randbelow(1000000):06d}"
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
