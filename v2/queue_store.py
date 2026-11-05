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
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path

from .job import Job
from .loginid import same_login

from .appdir import ROOT      # 개발 PC=프로젝트 폴더 / 설치본=%APPDATA%\BlogLandingAgent
QUEUE_ROOT = ROOT / "queue"

PENDING, RUNNING, DONE, FAILED, CANCELED = ("pending", "running", "done",
                                            "failed", "canceled")
# ★HELD — 사용자가 그 계정을 로그아웃하려 해서 **대기 작업만** 멈춰 둔 상태.
#   · 종료 상태가 아니다. 사용자가 재로그인하고 직접 재개하면 pending 으로 돌아간다.
#   · Agent 는 pending 만 집으므로(claim) 보류 작업은 집히지 않는다 — 옛 Agent 도 같다.
#   · **실행 중(running)인 배치는 보류하지 않는다.** 10건 배치는 끝까지 간다.
HELD = "held"
OPEN_STATES = (PENDING, RUNNING)
KINDS = ("run", "session")          # run = 랜딩 생성/전환, session = 로그인/세션 확인


# ══════════════════════════════════════════════════════════════════
#  큐 백엔드 상태 (2026-11-03, 1단계)
#   ★예전에는 Supabase 연결이 실패하면 **stderr 한 줄만 남기고** 로컬 큐로
#     조용히 내려갔다. 화면(Streamlit Cloud)에서 그렇게 되면 작업이 그 서버
#     컨테이너 안 `queue/` 로 들어가 **PC 에이전트가 영원히 못 본다.**
#     `@st.cache_resource` 가 그 객체를 프로세스 내내 들고 있어서 새로고침으로도
#     안 풀리고 관리자 재부팅만이 해결책이었다.
#   → ① 폴백 사실을 **여기에 기록**해 화면이 읽을 수 있게 하고
#     ② `strict=True` 로 부르면 **폴백하지 않고 예외**를 던진다.
#     ③ 기본값(strict=False)은 예전과 똑같이 동작한다 — Agent·CLI 영향 없음.
# ══════════════════════════════════════════════════════════════════
class PartialHoldError(RuntimeError):
    """보류/재개를 **일부만** 반영한 채 실패했다 (2026-11-04).

    왜 따로 만들었나
        보류는 50건씩 나눠 보낸다. 두 번째 묶음이 실패하면 **첫 묶음은 이미
        서버에 반영된 상태**다. 그런데 평범한 예외로 올려 보내면 반환값(바뀐
        job_id 목록)이 사라져, 화면은 "하나도 보류하지 못했다" 고 안내한다.
        실제로는 N 건이 보류돼 있으므로 사용자가 [보류 작업 재개] 를 눌러야 한다.

    들고 있는 것
        done     : 성공을 **확인한** job_id 목록(서버가 돌려준 것)
        verified : 실패 직후 **다시 조회한** 실제 그 상태의 job_id 목록.
                   재조회까지 실패하면 `None` — 그때는 '0건' 이 아니라
                   **'결과 확인 불가'** 로 안내해야 한다.
        remaining: 보내지 못한(또는 결과를 모르는) job_id 목록
    """

    def __init__(self, message: str, *, done=None, verified=None,
                 remaining=None, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.done = list(done or [])
        self.verified = None if verified is None else list(verified)
        self.remaining = list(remaining or [])
        self.cause = cause

    @property
    def confirmed(self) -> int:
        """실제로 바뀐 것으로 **확인된** 건수. 확인 불가면 -1."""
        if self.verified is not None:
            return len(self.verified)
        return len(self.done) if self.done else -1


class QueueBackendError(RuntimeError):
    """원격 큐(Supabase)에 붙지 못했다 — 로컬 큐로 숨기지 않고 알린다."""


_BACKEND: dict = {"requested": "", "kind": "", "mode": "", "fallback": False,
                  "reason": "", "at": ""}


def backend_info() -> dict:
    """마지막 `get_store()` 가 무엇에 붙었는지(화면 표시용). 사본을 돌려준다."""
    return dict(_BACKEND)


def _note_backend(requested: str, kind: str, mode: str = "",
                  fallback: bool = False, reason: str = "") -> None:
    _BACKEND.update({"requested": requested, "kind": kind, "mode": mode,
                     "fallback": bool(fallback), "reason": reason,
                     "at": now_iso()})


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

    # ★네트워크 없이 이 PC 안에서만 움직이는 저장소인가.
    #   True 면 "제출이 실패했으면 작업도 없다" 가 보장된다(중간에 떠 있는 요청이 없다).
    #   원격 저장소는 False — 타임아웃이 나도 서버에서는 처리됐을 수 있다.
    is_local = False

    # ── UI 쪽 ────────────────────────────────────────────────────
    @abstractmethod
    def submit(self, job: Job, *, kind: str = "run", title: str = "",
               created_by: str = "", target_agent: str = "",
               client_key: str = "") -> str: ...

    @abstractmethod
    def get(self, job_id: str) -> dict | None: ...

    @abstractmethod
    def list_jobs(self, limit: int = 30) -> list[dict]: ...

    @abstractmethod
    def request_cancel(self, job_id: str) -> None: ...

    def cancel_pending(self, job_id: str) -> bool:
        """**아직 집히지 않은** 작업만 그 자리에서 `canceled` 로 바꾼다.

        돌려주는 값
          `True`  — 이 호출이 **대기 상태였던 작업을 실제로 취소했다**(확정).
          `False` — 대기 상태가 아니었다(이미 Agent 가 집어 갔거나 끝났다).
                    그때는 호출한 쪽이 `request_cancel()` 로 **요청**을 남긴다.

        ★왜 따로 두나 — `request_cancel` 은 '요청' 일 뿐이라 Agent 가 그 작업을
          집어 **프로세스를 띄운 뒤에야** 반응한다. 대기 작업은 띄울 필요가 없다.
        ★경합 — 바로 이 순간 Agent 가 `claim()` 할 수 있다. 그래서 구현은 반드시
          **조건부**(대기 상태일 때만 바뀜)여야 하고, 바뀌었는지를 되돌려 줘야 한다.
          추측으로 `True` 를 돌려주면 화면이 '취소됨' 이라고 말하는데 PC 에서는
          글이 올라가는, 가장 나쁜 불일치가 생긴다.
        ★기본 구현은 **아무것도 하지 않고 False** — 조건부 변경을 보장할 수 없는
          저장소는 이 길을 쓰지 않는다(= 지금까지와 똑같이 동작한다).
        """
        return False

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

    def find_by_client_key(self, client_key: str) -> dict | None:
        """멱등성 키로 **이미 등록된** 작업을 찾는다(재제출 금지용).

        ★응답만 유실되고 서버에는 작업이 생긴 경우를 가려내기 위한 것이다.
          구현하지 않은 저장소는 `NotImplementedError` 를 던지고, 호출한 쪽은
          그것을 '확인 불가' 로 다뤄야 한다(없는 것으로 단정하면 중복 제출이 된다).
        """
        raise NotImplementedError

    # ── 계정별 보류 (2026-11-03, Stage 2) ────────────────────────
    #   로그아웃 대기 중인 **그 login_id 의 대기 작업만** 멈춘다.
    #   ★실행 중인 작업은 건드리지 않는다(10건 배치를 중간에 끊지 않는다).
    #   ★login_id 가 비어 있는 작업은 **보류하지 않는다** — 누구 것인지 모르는 작업을
    #     추측으로 묶으면 다른 계정의 발행이 멈춘다.
    #   식별자는 **login_id 1순위 · 없으면 account.id(세션 폴더) 정확일치**다.
    #   blog_id 는 쓰지 않는다(같은 blog_id 에 로그인 계정이 둘 이상일 수 있다).
    def hold_account(self, login_id: str = "", *, device: str = "",
                     account_id: str = "") -> list[str]:
        """그 계정의 pending 작업을 held 로. 보류된 job_id 목록."""
        raise NotImplementedError

    def resume_account(self, login_id: str = "", *, device: str = "",
                       account_id: str = "") -> list[str]:
        """held → pending. 재개된 job_id 목록."""
        raise NotImplementedError

    # ── 상태로 좁혀 보는 조회 (2026-11-04) ───────────────────────
    #   ★왜 따로 만들었나 — 예전에는 `list_jobs(limit=200)`(화면은 120)을 훑어
    #     그 안에서 held 를 골랐다. 작업이 많아 **보류 작업이 조회 창 밖으로
    #     밀려나면 "보류 없음" 으로 보이고**, 그러면 로그아웃 대기 중인 계정에
    #     새 발행 작업이 다시 만들어진다(= 신규 발행 차단 누락).
    #     상태로 먼저 좁히면 held·running 은 몇 건뿐이라 창에 밀려나지 않는다.
    #   ★식별 비교는 `matches`(login_id 정규화 포함)로 **클라이언트 쪽에서** 한다.
    #     서버 필터는 문자열 정확일치라 `@naver.com` 표기 차이를 흡수하지 못한다.
    SCAN_LIMIT = 500

    def list_by_status(self, status: str, login_id: str = "",
                       account_id: str = "", kind: str = "",
                       limit: int = 0) -> list[dict]:
        """그 상태의 작업만 돌려준다(식별자를 주면 그 계정만).

        기본 구현은 `list_jobs` 를 훑는다. 상태별 조회를 서버에서 할 수 있는
        저장소는 이 메서드를 덮어써서 **조회 창 문제를 아예 없앤다.**
        """
        want = (status or "").strip()
        both = not (login_id or "").strip() and not (account_id or "").strip()
        out: list[dict] = []
        for rec in self.list_jobs(limit=int(limit or self.SCAN_LIMIT)):
            if (rec.get("status") or "") != want:
                continue
            if kind and (rec.get("kind") or "run") != kind:
                continue
            if not both and not matches(rec, login_id, account_id):
                continue
            out.append(rec)
        return out

    def held_jobs(self, login_id: str = "", account_id: str = "") -> list[dict]:
        """보류 중인 작업 목록(식별자를 주면 그 계정만)."""
        return self.list_by_status(HELD, login_id, account_id)

    def running_for_login(self, login_id: str = "",
                          account_id: str = "") -> list[dict]:
        """그 계정의 **실행 중** 작업(= 아직 끝나지 않은 배치). 로그아웃 시점 판단용.

        ★식별자가 없으면 **아무것도 돌려주지 않는다**(남의 배치를 내 것으로 세지 않게).
        """
        if not (login_id or "").strip() and not (account_id or "").strip():
            return []
        return self.list_by_status(RUNNING, login_id, account_id)

    # ── 상태 표시용(추상 아님 — 기존 구현체를 고치지 않아도 된다) ──
    def last_contact(self) -> str:
        """이 저장소와 **마지막으로 성공적으로 주고받은 시각**(없으면 빈 문자열)."""
        return getattr(self, "_last_ok", "") or ""

    def describe_backend(self) -> dict:
        """화면에 보여 줄 한 줄 요약."""
        return {"kind": type(self).__name__,
                "mode": getattr(self, "mode", "") or "",
                "last_contact": self.last_contact()}

    def probe(self) -> bool:
        """정말 주고받을 수 있는지 **가볍게** 확인한다(기본: 할 일 없음).

        ★설정값만 맞고 키가 틀린 경우는 객체를 만들 때 드러나지 않는다.
          화면은 `strict` 로 부를 때 이걸 한 번 거쳐 '붙은 줄 알았는데 아닌' 상태를
          만들지 않는다. 읽기 1회뿐이고 작업을 집거나 바꾸지 않는다.
        """
        return True


def login_of(rec: dict | None) -> str:
    """작업 레코드에 적힌 로그인 ID. 없으면 빈 문자열.

    ★`blog_id` 로 대신 찾지 않는다 — blog_id 가 같아도 로그인 계정이 다를 수 있다.
    """
    job = (rec or {}).get("job") or {}
    return str(job.get("login_id") or "").strip()


def account_of(rec: dict | None) -> str:
    """작업 레코드의 계정(세션 폴더) 이름. `login_id` 가 없는 계정의 유일한 식별자다.

    ★`blog_id` 는 쓰지 않는다 — 같은 blog_id 에 로그인 계정이 둘 이상 있을 수 있다.
    """
    job = (rec or {}).get("job") or {}
    return str(job.get("account") or "").strip()


def _same_login(a: str, b: str) -> bool:
    """로그인 ID 비교 — `shin894894` 와 `shin894894@naver.com` 을 같게 본다.

    ★운영 등록표에 두 형태가 섞여 있다. 문자열 그대로 비교하면 작업 payload 에
      적힌 형태와 등록 형태가 다를 때 **보류·재개가 그 계정을 못 찾는다.**
      정규화는 `v2/loginid.py` 한 곳에만 있다(login_state 와 같은 규칙).
    """
    return same_login(a, b)


def _same_account(a: str, b: str) -> bool:
    """세션 폴더 이름은 **대소문자까지 그대로** 같아야 한다(폴더 이름이므로)."""
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a) and bool(b) and a == b


def matches(rec: dict | None, login_id: str = "", account_id: str = "") -> bool:
    """이 작업이 그 계정의 것인가.

    · `login_id` 를 주면 **그것만** 본다(계정 식별 1순위).
    · `login_id` 가 없을 때만 `account_id`(세션 폴더 이름) **정확일치**로 본다.
    · 둘 다 없으면 **아무것도 고르지 않는다**(추측 금지).
    """
    if (login_id or "").strip():
        return _same_login(login_of(rec), login_id)
    if (account_id or "").strip():
        return _same_account(account_of(rec), account_id)
    return False


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

    is_local = True                        # 네트워크가 없다 → 실패 판정이 정확하다

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else QUEUE_ROOT
        for sub in ("pending", "running", "held", "finished", "logs", "cancel",
                    "agents"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._last_ok = now_iso()          # 폴더를 만들 수 있었다 = 교신 성공

    # ── 경로 ─────────────────────────────────────────────────────
    def _dir(self, state: str) -> Path:
        return self.root / state

    def _find(self, job_id: str) -> tuple[str, Path] | tuple[None, None]:
        for state in ("running", "pending", "held", "finished"):
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
               created_by: str = "", target_agent: str = "",
               client_key: str = "") -> str:
        job_id = new_job_id()
        # ★브랜드는 레코드 맨 위에도 따로 둔다 — 큐 파일만 열어 봐도 어느 브랜드인지 안다.
        brand = (getattr(job, "brand", "") or "").strip()
        record = {
            "id": job_id,
            # ★멱등성 키 — 응답이 유실돼도 이 값으로 **이미 넣은 작업**을 찾는다.
            "client_key": client_key,
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
        rec = _read_json(path) if path else None
        if rec is not None:
            #  ★중단 '요청' 표시를 레코드에 실어 준다 — 화면이 '요청됨' 과
            #    '중단됨' 을 구분할 수 있어야 한다(원격 큐도 같은 이름의 열을 준다).
            rec.setdefault("cancel_requested",
                           (self.root / "cancel" / str(job_id)).exists())
        return rec

    def find_by_client_key(self, client_key: str) -> dict | None:
        key = (client_key or "").strip()
        if not key:
            return None
        for state in ("pending", "running", "held", "finished"):
            for p in self._dir(state).glob("*.json"):
                rec = _read_json(p)
                if rec and (rec.get("client_key") or "") == key:
                    self._last_ok = now_iso()
                    return rec
        self._last_ok = now_iso()
        return None

    def list_jobs(self, limit: int = 30) -> list[dict]:
        rows: list[dict] = []
        self._last_ok = now_iso()
        for state in ("running", "pending", "held", "finished"):
            for p in self._dir(state).glob("*.json"):
                rec = _read_json(p)
                if rec:
                    #  중단 '요청' 표시를 함께 싣는다(화면이 구분해 보여 준다).
                    rec.setdefault(
                        "cancel_requested",
                        (self.root / "cancel" / str(rec.get("id") or "")).exists())
                    rows.append(rec)
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[:limit]

    def list_by_status(self, status: str, login_id: str = "",
                       account_id: str = "", kind: str = "",
                       limit: int = 0) -> list[dict]:
        """상태 폴더 **하나만** 읽는다 → 조회 창에 밀려 놓치는 일이 없다."""
        want = (status or "").strip()
        state = want if want in ("pending", "running", "held") else "finished"
        both = not (login_id or "").strip() and not (account_id or "").strip()
        rows: list[dict] = []
        for p in self._dir(state).glob("*.json"):
            rec = _read_json(p)
            if not rec or (rec.get("status") or "") != want:
                continue
            if kind and (rec.get("kind") or "run") != kind:
                continue
            if not both and not matches(rec, login_id, account_id):
                continue
            rows.append(rec)
        self._last_ok = now_iso()
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[: int(limit)] if limit else rows

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

    # ── 계정별 보류 ──────────────────────────────────────────────
    def _move_account(self, src_state: str, dst_state: str, login_id: str,
                      device: str, status: str, account_id: str = "") -> list[str]:
        """`src_state` → `dst_state` 로 **그 계정 작업만** 옮긴다(os.replace = 원자적).

        ★`pending` 폴더에서만 꺼내므로 **실행 중 작업은 절대 영향받지 않는다.**
          옮기기가 실패하면(= 그 순간 Agent 가 집어갔다) 조용히 건너뛴다 — 그 작업은
          running 이 되었고, 배치는 끝까지 가는 것이 정책이다.
        ★식별자: login_id 1순위, 없으면 account.id 정확일치. 둘 다 없으면 0건.
        """
        lid, aid = (login_id or "").strip(), (account_id or "").strip()
        if not lid and not aid:
            return []
        moved: list[str] = []
        for src in sorted(self._dir(src_state).glob("*.json")):
            rec = _read_json(src)
            if rec is None or not matches(rec, lid, aid):
                continue
            # ★랜딩 작업(run)만 보류한다. 로그인·세션확인·로그아웃(session)은
            #   사용자가 방금 누른 **복구 수단**이라 보류하면 재로그인을 못 한다.
            if (rec.get("kind") or "run") != "run":
                continue
            want = (rec.get("target_agent") or "").strip()
            if device and want and want != device:
                continue
            dst = self._dir(dst_state) / src.name
            try:
                os.replace(src, dst)
            except OSError:
                continue                   # 방금 Agent 가 집어갔다 → 그대로 둔다
            rec["status"] = status
            rec["held_at"] = now_iso() if status == HELD else ""
            _atomic_write(dst, json.dumps(rec, ensure_ascii=False, indent=1))
            moved.append(str(rec.get("id") or src.stem))
        return moved

    def hold_account(self, login_id: str = "", *, device: str = "",
                     account_id: str = "") -> list[str]:
        return self._move_account(PENDING, HELD, login_id, device, HELD,
                                  account_id=account_id)

    def resume_account(self, login_id: str = "", *, device: str = "",
                       account_id: str = "") -> list[str]:
        return self._move_account(HELD, PENDING, login_id, device, PENDING,
                                  account_id=account_id)

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

    def cancel_pending(self, job_id: str) -> bool:
        """대기 폴더에 **아직 있을 때만** 옮겨서 취소한다(원자적).

        ★`os.rename` 이 경합을 가른다 — `claim()` 이 같은 파일을 running 으로
          옮기는 것과 똑같은 방식이다. 둘이 동시에 걸면 한쪽만 성공하고
          진 쪽은 `OSError` 를 받아 `False` 가 된다.
        """
        src = self._dir(PENDING) / f"{job_id}.json"
        dst = self._dir("finished") / f"{job_id}.json"
        if not src.exists():
            return False
        try:
            os.rename(src, dst)                     # ★원자적 — 진 쪽은 예외
        except OSError:
            return False                            # 그 사이 Agent 가 집어 갔다
        rec = _read_json(dst) or {}
        rec.update({"status": CANCELED, "exit_code": 130,
                    "cancel_requested": True, "finished_at": now_iso()})
        _atomic_write(dst, json.dumps(rec, ensure_ascii=False, indent=1))
        #  요청 표시는 남겨 두지 않는다 — 이미 끝난 작업이다.
        marker = self.root / "cancel" / job_id
        if marker.exists():
            try:
                marker.unlink()
            except Exception:                                  # noqa: BLE001
                pass
        return True

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


# ── 제출 결과 3상태 (2026-11-03 보완) ─────────────────────────────
#   ★'확인 불가' 를 '실패' 로 단정하면 안 된다. 서버에는 작업이 생겼는데 응답만
#     유실된 경우가 있고, 그때 다시 넣으면 **같은 발행이 두 번** 돌아간다.
#     그래서 ①성공 확인 ②실패 확정 ③확인 불가 를 나누고, ③에서는 자동 재제출을
#     하지 않고 **멱등성 키로 조회**해 되찾는다.
SUBMIT_OK, SUBMIT_FAILED, SUBMIT_UNKNOWN = "ok", "failed", "unknown"


class SubmitResult:
    """제출 결과. `ok` 가 아니면 `message` 를 사용자에게 그대로 보여 준다."""

    __slots__ = ("status", "job_id", "client_key", "message", "cause")

    def __init__(self, status: str, job_id: str = "", client_key: str = "",
                 message: str = "", cause: str = "") -> None:
        self.status, self.job_id = status, job_id
        self.client_key, self.message, self.cause = client_key, message, cause

    @property
    def ok(self) -> bool:
        return self.status == SUBMIT_OK

    @property
    def unknown(self) -> bool:
        return self.status == SUBMIT_UNKNOWN

    def __repr__(self) -> str:                                 # 테스트 가독성
        return (f"SubmitResult({self.status}, job_id={self.job_id!r}, "
                f"key={self.client_key!r})")


def new_client_key() -> str:
    """제출 1회를 가리키는 멱등성 키."""
    return uuid.uuid4().hex


def find_job_by_client_key(store: JobStore, client_key: str,
                           tries: int = 1, wait_sec: float = 1.5
                           ) -> tuple[dict | None, str]:
    """(찾은 레코드 | None, 조회 실패 사유).

    ★돌려주는 값이 `(None, "")` 이면 **조회는 됐고 없었다**는 뜻이고,
      `(None, 사유)` 면 **확인하지 못했다**는 뜻이다. 둘을 섞으면 안 된다.
    ★`tries>1` — 늦게 도착한 요청이 서버에서 처리되는 중일 수 있어 잠깐 기다려
      다시 본다(네트워크 지연으로 방금 넣은 작업이 아직 안 보이는 경우).
    """
    if not client_key:
        return None, "멱등성 키가 없습니다"
    why = ""
    for i in range(max(1, tries)):
        if i:
            time.sleep(wait_sec)
        try:
            rec = store.find_by_client_key(client_key)
        except NotImplementedError:
            return None, "이 저장소는 작업 조회를 지원하지 않습니다"
        except Exception as exc:                               # noqa: BLE001
            why = f"{type(exc).__name__}: {exc}"
            continue
        if rec:
            return rec, ""
        why = ""
    return None, why


def _definite_failure(store: JobStore, exc: BaseException) -> bool:
    """이 예외가 '서버에 아무것도 안 들어갔음' 을 보장하는가.

    ★보수적으로 판단한다 — 확실하지 않으면 **False**(= 확인 불가로 다룬다).
      타임아웃·연결 끊김·5xx 는 요청이 도착해 처리됐을 수 있다.
    """
    if getattr(exc, "definite_failure", False):
        return True
    if isinstance(exc, (TypeError, ValueError, KeyError)):
        return True                        # 우리 쪽 인자 오류 = 보내기 전에 막혔다
    # 로컬 파일 큐는 네트워크가 없다 → 실패했으면 파일도 없다(조회가 정확하다)
    return bool(getattr(store, "is_local", False))


def submit_checked(store: JobStore, job: Job, *, kind: str = "run",
                   title: str = "", target_agent: str = "",
                   label: str = "작업",
                   client_key: str = "") -> SubmitResult:
    """큐에 넣고 **정말 들어갔는지 확인**한다 → `SubmitResult`(3상태).

    ★조용히 실패하는 길을 막기 위한 것이다. 예전에는 `submit` 이 예외를 던지면
      화면이 깨졌고, 로컬 큐로 내려간 상태에서는 성공처럼 보였다(2026-10-30).
    ★제출이 실패로 보여도 **먼저 멱등성 키로 조회**한다. 조회로 '없음' 을 확인하면
      실패 확정, 조회 자체를 못 하면 '확인 불가' 다. 어느 경우에도 **자동으로
      다시 넣지 않는다** — 재시도는 사람이 결정한다.
    """
    retry = bool(client_key)               # 같은 키로 다시 부른 것 = 재시도
    key = client_key or new_client_key()

    def confirm(cause: str, had_id: str = "",
                definite: bool = False) -> SubmitResult:
        """조회로 실제 등록 여부를 가린다.

        ★`definite=False`(기본)면 조회에서 안 보여도 **실패로 단정하지 않는다.**
          늦게 도착한 요청이 나중에 처리될 수 있어, 그때 다시 넣으면 중복 발행이 된다.
        """
        rec, why = find_job_by_client_key(store, key, tries=1 if definite else 2)
        if rec:
            return SubmitResult(SUBMIT_OK, rec.get("id") or had_id, key,
                                f"{label}은 등록되어 있습니다(응답만 유실됐습니다).",
                                cause)
        if why:
            return SubmitResult(
                SUBMIT_UNKNOWN, had_id, key,
                f"{label}의 등록 여부를 확인하지 못했습니다 — 등록됐을 수도 "
                f"있으니 다시 넣지 마세요.\n원인: {cause}\n조회 실패: {why}",
                cause)
        if not definite:
            return SubmitResult(
                SUBMIT_UNKNOWN, had_id, key,
                f"{label}이 아직 큐에 보이지 않습니다 — 요청이 늦게 처리될 수도 "
                f"있어 **등록 실패로 단정하지 않습니다.** 다시 넣지 마세요.\n"
                f"원인: {cause}", cause)
        return SubmitResult(SUBMIT_FAILED, "", key,
                            f"{label}을 큐에 등록하지 못했습니다 — "
                            f"실행되지 않았습니다.\n원인: {cause}", cause)

    # ★같은 키로 다시 불렸으면 **먼저 조회**한다 — 이미 들어가 있으면 또 넣지 않는다.
    if retry:
        rec, why = find_job_by_client_key(store, key)
        if rec:
            return SubmitResult(SUBMIT_OK, rec.get("id") or "", key,
                                f"{label}은 이미 등록되어 있습니다(다시 넣지 "
                                f"않았습니다).")
        if why:
            return SubmitResult(SUBMIT_UNKNOWN, "", key,
                                f"{label}의 등록 여부를 확인하지 못해 다시 넣지 "
                                f"않았습니다.\n조회 실패: {why}")

    try:
        jid = store.submit(job, kind=kind, title=title, target_agent=target_agent,
                           client_key=key)
    except TypeError as exc:
        # 옛 저장소(멱등성 키를 모르는 구현)와의 호환 — 키 없이 한 번만 시도한다.
        if "client_key" not in str(exc):
            return confirm(f"{type(exc).__name__}: {exc}",
                           definite=_definite_failure(store, exc))
        try:
            jid = store.submit(job, kind=kind, title=title,
                               target_agent=target_agent)
        except Exception as exc2:                              # noqa: BLE001
            return SubmitResult(SUBMIT_UNKNOWN, "", "",
                                f"{label}의 등록 여부를 확인하지 못했습니다 — "
                                f"다시 넣지 말고 작업 목록에서 확인해 주세요.\n"
                                f"원인: {type(exc2).__name__}: {exc2}")
        key = ""                                               # 조회 수단이 없다
    except Exception as exc:                                   # noqa: BLE001
        return confirm(f"{type(exc).__name__}: {exc}",
                       definite=_definite_failure(store, exc))

    if not jid:
        # 번호를 못 받았을 뿐 서버에는 들어갔을 수 있다 → 단정하지 않는다.
        return confirm("작업 번호를 받지 못했습니다")

    try:
        rec = store.get(jid)
    except Exception as exc:                                   # noqa: BLE001
        return SubmitResult(SUBMIT_UNKNOWN, jid, key,
                            f"{label}을 넣었지만 상태를 확인하지 못했습니다 — "
                            f"다시 넣지 마세요(작업 번호 {jid}).\n"
                            f"원인: {type(exc).__name__}: {exc}",
                            f"{type(exc).__name__}: {exc}")
    if not rec:
        # ★로컬 큐는 방금 쓴 파일이 반드시 보인다 → 안 보이면 실패가 확실하다.
        #   원격은 복제 지연 등이 있을 수 있어 단정하지 않는다.
        return confirm(f"작업 번호 {jid} 를 큐에서 다시 찾지 못했습니다", had_id=jid,
                       definite=bool(getattr(store, "is_local", False)))
    return SubmitResult(SUBMIT_OK, jid, key)


def configured_backend(backend: str = "") -> str:
    """이번 실행이 **쓰려고 하는** 백엔드 이름(local | supabase)."""
    name = (backend or os.getenv("BLOG_QUEUE_BACKEND") or "local").strip().lower()
    if name in ("", "file"):
        return "local"
    return name


def wants_remote(backend: str = "") -> bool:
    """원격 큐를 쓰려는 설정인가(= 폴백을 숨기면 안 되는 상황인가)."""
    return configured_backend(backend) in ("supabase", "remote")


def get_store(backend: str = "", strict: bool = False) -> JobStore:
    """`BLOG_QUEUE_BACKEND` (local | supabase).

    Supabase 를 붙일 때 여기 분기 하나와 `SupabaseStore` 만 추가하면
    UI(`streamlit_app.py`)와 에이전트(`v2.agent`)는 고치지 않는다.

    ★`strict=True` (화면이 쓴다) — 원격 큐를 쓰려는 설정인데 붙지 못하면
      **로컬 큐로 내려가지 않고 `QueueBackendError`** 를 던진다. 조용한 폴백은
      "작업을 넣었는데 PC 가 못 본다" 는 상태를 만들고, 그 상태가 화면 프로세스에
      캐시되면 재부팅 말고는 풀 방법이 없었다(2026-10-30).
    ★`strict=False` (기본 · Agent·CLI) — 예전과 똑같이 로컬 큐로 되돌아간다.
      어느 쪽이든 결과는 `backend_info()` 에 남아 화면에서 확인할 수 있다.
    """
    name = configured_backend(backend)
    if name == "local":
        store = LocalStore()
        _note_backend(name, type(store).__name__)
        return store
    if name in ("supabase", "remote"):
        # ★원격 큐 — Streamlit Cloud 와 각 PC 의 Agent 가 같은 큐를 본다.
        try:
            from .supabase_store import SupabaseStore
            store = SupabaseStore()
            if strict:
                store.probe()              # 키가 틀린 경우까지 여기서 걸러낸다
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            _note_backend(name, "LocalStore" if not strict else "",
                          fallback=True, reason=reason)
            if strict:
                raise QueueBackendError(
                    "원격 큐(Supabase)에 연결하지 못했습니다 — "
                    "작업을 넣어도 각자 PC 의 Agent 가 보지 못합니다.\n"
                    f"원인: {reason}") from exc
            import sys
            print(f"[큐] Supabase 연결 실패 — 로컬 큐로 진행합니다: {exc}",
                  file=sys.stderr)
            return LocalStore()
        _note_backend(name, type(store).__name__,
                      mode=getattr(store, "mode", "") or "")
        return store
    raise RuntimeError(f"아직 없는 큐 백엔드입니다: {name!r} (local | supabase)")
