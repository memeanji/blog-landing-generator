r"""Stage 7 검증 — UI 조회 안정화: UI_TIMEOUT · 렌더 예산(deadline) · 렌더 캐시
    (2026-11-05 · 브랜치 feature/agent-1.3.0-stabilization)

    .\.venv\Scripts\python.exe scripts\test_stage7_ui_budget.py

★브라우저·네이버·시트·운영 큐·운영 DB·운영 세션을 건드리지 않는다. 임시 폴더만
  쓴다. 가짜 저장소가 `time.sleep` 으로 지연만 흉내낸다(네트워크 없음).

검증 항목(사용자 요청 3단계 ①~③)
  ① 정책 상수 — UI_TIMEOUT=8 · QUEUE_READ_TRIES=1 · 렌더 예산 18초 ·
     Agent TIMEOUT=20 유지
  ② deadline 전달 경로 — list_by_status → _page_all → HTTP 요청
  ③ deadline 만료 시 **부분 결과를 성공으로 쓰지 않는다**(명확한 조회 실패)
  ④ 동일 렌더에서 deadline 을 **공유**하고 호출마다 초기화하지 않는다
  ⑤ 렌더 캐시 — 중복 조회 제거 · 계정 사이 공유 없음
  ⑥ 제출 직전 `fresh=True` — 캐시를 무시하고 재조회해 보류를 본다
  ⑦ 조회 실패·타임아웃·부분 결과 → 그 계정 신규 발행 차단 유지
  ⑧ 실제 최악 대기시간(벽시계) — 조회 경로 상한 = 예산 + 요청 1회
"""
from __future__ import annotations

import ast
import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage7_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ["BLOG_QUEUE_BACKEND"] = "local"
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from v2 import queue_store                                     # noqa: E402
from v2.queue_store import HELD, PENDING, RUNNING              # noqa: E402
from v2.supabase_store import (MIN_REQ_TIMEOUT, SupabaseError,  # noqa: E402
                               SupabaseStore, TIMEOUT, UI_TIMEOUT)

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


# ── 운영 화면 함수를 그대로 꺼낸다 ─────────────────────────────────
APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)
NS: dict = {"queue_store": queue_store, "time": time, "chr": chr,
            "RUNNING_STATES": (PENDING, RUNNING)}
for _n in TREE.body:                        # 대문자 모듈 상수는 전부 그대로
    if isinstance(_n, ast.Assign) and len(_n.targets) == 1:
        _tg = _n.targets[0]
        _names = ([getattr(e, "id", "") for e in _tg.elts]
                  if isinstance(_tg, ast.Tuple) else [getattr(_tg, "id", "")])
        if _names and all(x.isupper() for x in _names if x):
            try:
                _vals = ast.literal_eval(_n.value)
            except Exception:                                  # noqa: BLE001
                continue
            #  ★`A = (1, 2)` 처럼 **값이 튜플**인 상수는 풀어헤치면 안 된다
            #    (2026-11-05: QUEUE_PERMANENT_STATUS 가 400 하나로 들어갔다).
            #    여러 이름에 나눠 담는 것은 `A, B = 1, 2` 꼴뿐이다.
            if isinstance(_tg, ast.Tuple):
                _vals = _vals if isinstance(_vals, tuple) else (_vals,)
                for _k, _v in zip(_names, _vals):
                    NS[_k] = _v
            else:
                NS[_names[0]] = _vals
for _n in TREE.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
            "is_logout_job", "render_scope", "query_deadline",
            "store_takes_deadline", "logout_state", "_logout_state_read",
            "logout_block"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)
logout_state, logout_block = NS["logout_state"], NS["logout_block"]

#  ★설계 ④(2026-11-05) — 운영 코드는 발행 작업 범위를 **account.id** 로 정한다.
#    테스트가 식별자 하나만 주면 그것을 account.id 로 본다(옛 호출부를 그대로 둔다).
_raw_lb = logout_block


def logout_block(store_obj, login_id="", account_id="", **kw):
    return _raw_lb(store_obj, "", (account_id or login_id), **kw)

render_scope, query_deadline = NS["render_scope"], NS["query_deadline"]

BUDGET = NS["RENDER_QUERY_BUDGET"]
TRIES = NS["QUEUE_READ_TRIES"]
FAST_SEC, FAST_EXTRA = NS["QUEUE_FAST_RETRY_SEC"], NS["QUEUE_FAST_EXTRA"]
PERMANENT = NS["QUEUE_PERMANENT_STATUS"]
NS["QUEUE_READ_WAIT"] = 0.01                # 테스트는 실제로 기다리지 않는다


def new_render() -> None:
    """렌더 1회를 새로 시작한다(운영은 렌더마다 모듈을 다시 실행한다)."""
    NS.setdefault("_render_box", {}).clear()


class Fake:
    """`list_by_status` 1회가 `pages` 번 요청하고 요청마다 `delay` 초 걸린다.

    ★`deadline` 을 **받는다** — 실제 SupabaseStore 와 같은 자리에서(요청 사이)
      남은 시간을 확인하고, 넘으면 예외를 던진다(부분 결과를 쓰지 않는다).
    """

    is_local = False

    def __init__(self, delay=0.01, pages=1, boom=None, held=0):
        self.delay, self.pages, self.boom = delay, pages, dict(boom or {})
        self.held = held
        self.requests = 0
        self.status_calls = 0
        self.deadlines: list[float | None] = []

    def list_by_status(self, status, login_id="", account_id="", kind="",
                       limit=0, deadline=None):
        self.status_calls += 1
        self.deadlines.append(deadline)
        exc = self.boom.get(status)
        if exc:
            raise exc
        for _ in range(self.pages):
            if deadline is not None and time.monotonic() >= deadline:
                raise SupabaseError(
                    "조회 시간이 예산을 넘겨 중단했습니다 — 결과를 신뢰할 수 "
                    "없습니다(부분 결과를 쓰지 않습니다).")
            self.requests += 1
            time.sleep(self.delay)
        if status == HELD and self.held:
            return [{"id": f"H{i}", "status": HELD, "kind": "run",
                     "job": {"login_id": login_id, "account": account_id}}
                    for i in range(self.held)]
        return []

    def list_jobs(self, limit=30):
        return []


# ══════════════════════════════════════════════════════════════════
head("① 정책 상수 — 승인된 값대로인가")
check("UI_TIMEOUT = 8초", UI_TIMEOUT == 8, f"{UI_TIMEOUT}")
check("QUEUE_READ_TRIES = 1 (느린 실패는 1회)", TRIES == 1, f"{TRIES}")
check("렌더 조회 예산 = 18초", BUDGET == 18.0, f"{BUDGET}")
check("★Agent 기존 TIMEOUT = 20초 유지", TIMEOUT == 20, f"{TIMEOUT}")
check("화면 타임아웃이 Agent 보다 짧다", UI_TIMEOUT < TIMEOUT)
check("예산 임박 시 요청 최소시간이 있다(거짓 차단 방지)",
      MIN_REQ_TIMEOUT > 0, f"MIN_REQ_TIMEOUT={MIN_REQ_TIMEOUT}초")
check("빠른 실패는 한 번 더 시도한다(일시 오류 복구 유지)",
      FAST_EXTRA >= 1, f"extra={FAST_EXTRA} · 기준 {FAST_SEC}초")

head("② deadline 전달 경로 — list_by_status → _page_all → HTTP")
check("list_by_status 가 deadline 을 받는다",
      "deadline" in SupabaseStore.list_by_status.__code__.co_varnames)
check("_page_all 이 deadline 을 받는다",
      "deadline" in SupabaseStore._page_all.__code__.co_varnames)
check("_rest_range 가 timeout 을 받는다(요청 단위로 끊는다)",
      "timeout" in SupabaseStore._rest_range.__code__.co_varnames)
src = (ROOT / "v2" / "supabase_store.py").read_text(encoding="utf-8")
check("  _page_all 이 요청 사이에서 남은 시간을 확인한다",
      "left = deadline - monotonic()" in src)
check("  요청 타임아웃을 UI_TIMEOUT 과 남은 시간 중 작은 값으로 끊는다",
      "min(UI_TIMEOUT, left)" in src)
check("★Agent 경로는 deadline 없이 TIMEOUT 을 쓴다(무영향)",
      "timeout=(TIMEOUT if timeout is None else timeout)" in src)
check("화면이 예산을 저장소까지 넘긴다",
      "deadline=query_deadline()" in APP)

head("③ deadline 만료 — 부분 결과를 성공으로 쓰지 않는다")
#   5페이지 × 3초 = 15초가 필요한데 예산을 2초만 준다.
new_render()
st = Fake(delay=3.0, pages=5)
box = render_scope()
box["deadline"] = time.monotonic() + 2.0            # 예산을 일부러 줄인다
t0 = time.monotonic()
stt = logout_state(st, "loginW", "")
el = time.monotonic() - t0
check("★예산을 넘기면 조회 실패로 처리된다(부분 결과 아님)",
      bool(stt["read_error"]) and stt["held"] == [],
      stt["read_error"][:52])
check("  '보류 0건' 으로 단정하지 않는다", stt["pending"] is False
      and bool(stt["read_error"]))
check("  모은 페이지를 돌려주지 않는다(요청은 했으나 결과를 버린다)",
      st.requests >= 1 and stt["held"] == [], f"요청 {st.requests}회")
why = logout_block(st, "loginW", "")
check("★그 계정의 신규 발행이 차단된다",
      "확인하지 못해 새 작업을 만들지 않았습니다" in why, why[:46])
check("  다른 계정은 영향받지 않는다고 알린다",
      "다른 계정의 발행은 영향받지 않습니다" in why)

#   실제 SupabaseStore 로도 같은지 — 네트워크 없이 _rest_range 만 가로챈다
real = SupabaseStore(mode="service", url="http://127.0.0.1:9", key="dummy")
real_calls = {"n": 0}


def slow_range(query, count=False, timeout=None):
    real_calls["n"] += 1
    time.sleep(0.4)
    return [{"job_id": f"R{real_calls['n']}", "status": "held", "kind": "run",
             "device_id": "PC1",
             "payload": {"login_id": "loginW", "account": "acc_w"}}], 999


real._rest_range = slow_range                                  # noqa: SLF001
raised = None
try:
    real.list_by_status(HELD, "loginW", "", deadline=time.monotonic() + 1.0)
except Exception as exc:                                       # noqa: BLE001
    raised = exc
check("★실제 구현도 예산 초과를 예외로 올린다",
      isinstance(raised, SupabaseError) and "예산" in str(raised),
      str(raised)[:52])
check("  총건수(999)에 못 미치는 부분 결과를 성공으로 돌려주지 않는다",
      raised is not None)

head("④ 동일 렌더에서 deadline 공유 — 호출마다 초기화하지 않는다")
new_render()
d1 = query_deadline()
time.sleep(0.3)
d2 = query_deadline()
check("★같은 렌더에서는 같은 deadline 을 쓴다", d1 == d2, f"차이 {d2 - d1:.3f}초")
st2 = Fake(delay=0.01)
logout_state(st2, "loginW", "")
logout_state(st2, "other", "")               # 다른 계정도 같은 예산을 쓴다
uniq = {d for d in st2.deadlines if d is not None}
check("  계정이 달라도 렌더 예산은 하나다", len(uniq) == 1,
      f"deadline {len(uniq)}종 · 전달 {len(st2.deadlines)}회")
d_before = query_deadline()
new_render()
check("렌더가 바뀌면 예산이 새로 생긴다", query_deadline() != d_before)

head("⑤ 렌더 캐시 — 중복 제거 · 계정 사이 공유 없음")
new_render()
st3 = Fake(delay=0.01)
logout_state(st3, "loginW", "")              # 렌더(표시)
logout_state(st3, "loginW", "")              # 같은 렌더에서 또
check("★같은 렌더·같은 계정은 한 번만 조회한다", st3.status_calls == 3,
      f"상태조회 {st3.status_calls}회 (3상태 × 1회)")

new_render()
st4 = Fake(delay=0.01, held=2)
a = logout_state(st4, "accA", "")
b = logout_state(st4, "accB", "")
check("★계정이 다르면 캐시를 공유하지 않는다", st4.status_calls == 6,
      f"상태조회 {st4.status_calls}회 (2계정 × 3상태)")
check("  각 계정의 결과에 자기 식별자가 들어 있다",
      a["login_id"] == "accA" and b["login_id"] == "accB")

new_render()
st5 = Fake(delay=0.01)
logout_state(st5, "", "tab_abc")             # login_id 없이 account.id 로
logout_state(st5, "", "tab_abc")
check("login_id 없는 계정도 캐시 키가 account.id 로 잡힌다",
      st5.status_calls == 3, f"{st5.status_calls}회")

new_render()
st6 = Fake(delay=0.01)
logout_state(st6, "loginW", "")
logout_state(st6, "", "loginW")               # 같은 글자라도 자리가 다르면 별개
check("  login_id 와 account.id 를 섞지 않는다", st6.status_calls == 6,
      f"{st6.status_calls}회")

head("⑥ 제출 직전 fresh=True — 오래된 캐시로 판정하지 않는다")
new_render()
st7 = Fake(delay=0.01, held=0)
first = logout_state(st7, "loginW", "")       # 렌더 시점: 보류 0건
check("렌더 시점에는 보류가 없다", first["held"] == [] and not first["pending"])
st7.held = 1                                  # 그 사이 로그아웃이 보류를 걸었다
stale = logout_state(st7, "loginW", "")       # 캐시를 쓰면
check("★캐시는 옛 값(보류 0건)을 그대로 준다 — 위험 재현",
      stale["held"] == [] and stale is first)
fresh = logout_state(st7, "loginW", "", fresh=True)
check("★fresh=True 는 다시 읽어 보류를 본다", len(fresh["held"]) == 1,
      f"held {len(fresh['held'])}건")
check("  그래서 그 계정의 신규 발행이 차단된다",
      "로그아웃 대기 상태입니다" in logout_block(st7, "loginW", ""))
#  ★정책 변경(2026-11-05 · 설계 ④) — 발행 작업의 조회·보류·재개 범위는
#    **account.id 하나**다. 같은 네이버 ID 를 여러 등록(`…_re`/`…_dn`)으로
#    나눠 쓰는 운영 구조에서 login_id 로 조회하면 다른 등록의 작업까지
#    끌어오고(과도), 등록 전 작업은 놓친다(누락). 상세 검증은 stage11.
check("★제출 판정(logout_block)이 fresh=True 를 쓴다",
      'logout_state(store_obj, "", aid, fresh=True)' in APP)
check("  표시 경로(render_account_tools)는 캐시를 쓴다(fresh 아님)",
      'stt = logout_state(store_obj, "", aid)' in APP)
check("  두 경로가 **같은 식별자(account.id)** 를 쓴다",
      'logout_state(store_obj, "", aid' in APP)

head("⑦ 실패와 '보류 0건' 구분 — 기존 동작 유지")
new_render()
good = logout_state(Fake(delay=0.01, held=0), "loginW", "")
check("보류 0건 — read_error 없음", good["held"] == []
      and not good["read_error"] and good["pending"] is False)
check("  그 계정의 발행은 막지 않는다",
      logout_block(Fake(delay=0.01, held=0), "loginW", "") == "")
for label, exc in (("503", SupabaseError("GET jobs 실패(503)")),
                   ("DNS", SupabaseError("NameResolutionError")),
                   ("SSL", SupabaseError("SSLEOFError: UNEXPECTED_EOF"))):
    new_render()
    bad = logout_state(Fake(boom={HELD: exc}), "loginW", "")
    check(f"[{label}] 조회 실패 — read_error 기록 + 0건 단정 안 함",
          bool(bad["read_error"]) and bad["held"] == []
          and bad["pending"] is False)
    new_render()
    check(f"[{label}] 신규 발행 차단",
          "확인하지 못해 새 작업을 만들지 않았습니다"
          in logout_block(Fake(boom={HELD: exc}), "loginW", ""))

head("⑧ 실제 최악 대기시간(벽시계) — 조회 경로 상한")
#   ★예산은 요청 **사이에서만** 확인된다. 진행 중인 동기 요청은 끊을 수 없으므로
#     상한은 `예산 + 요청 1회` 다. 렌더 전체의 절대 상한은 아니다.
new_render()
st8 = Fake(delay=1.0, pages=40)               # 한 상태가 40초 필요
box = render_scope()
box["deadline"] = time.monotonic() + 3.0      # 예산 3초로 줄여 빠르게 측정
t0 = time.monotonic()
r8 = logout_state(st8, "loginW", "")
el8 = time.monotonic() - t0
print(f"   예산 3초 · 요청 1초 × 40페이지 → 실측 {el8:.2f}초 "
      f"(요청 {st8.requests}회)")
check("★예산이 지켜진다(예산 + 요청 1회 안)", el8 <= 3.0 + 1.0 + 0.5,
      f"{el8:.2f}초 ≤ 4.5초")
check("  예산 초과는 조회 실패로 간다", bool(r8["read_error"]))

new_render()
st9 = Fake(delay=0.5, pages=100)
box = render_scope()
box["deadline"] = time.monotonic() + 2.0
t0 = time.monotonic()
logout_state(st9, "loginW", "")
el9 = time.monotonic() - t0
print(f"   예산 2초 · 요청 0.5초 × 100페이지 → 실측 {el9:.2f}초")
check("  MAX_PAGES 까지 가지 않고 예산에서 끊는다",
      el9 <= 2.0 + 0.5 + 0.5 and st9.requests < SupabaseStore.MAX_PAGES,
      f"{el9:.2f}초 · 요청 {st9.requests}회")

print(f"\n   운영 정책값 기준 조회 경로 상한 = 예산 {BUDGET:.0f}초 + "
      f"요청 1회 {UI_TIMEOUT}초 = {BUDGET + UI_TIMEOUT:.0f}초")
print("   ★렌더 전체의 절대 상한은 아니다 — 구글 시트 호출·그림 그리기는 별도다.")

head("⑨ 기존 발행·보류·재개 로직 무변경")
check("submit / hold_account / resume_account 시그니처 유지",
      all(hasattr(SupabaseStore, m) for m in
          ("submit", "hold_account", "resume_account", "held_jobs",
           "running_for_login", "claim", "update", "finish")))
check("Agent 가 쓰는 claim 경로에 deadline 이 없다",
      "deadline" not in SupabaseStore.claim.__code__.co_varnames)
check("보류/재개는 상태 조건부 PATCH 그대로",
      "_switch_status" in src and "status=eq." in src)
#  ★소스에서는 f-string 이 두 줄로 나뉘어 있다 — 조각으로 확인한다
#    (렌더된 문구 자체는 ③·⑦ 에서 이미 검증했다).
check("fail-safe 문구 그대로(기존 안내 유지)",
      "보류 작업을 확인하지 못해 새 작업을 만들지 " in APP
      and "안았습니다" not in APP and "않았습니다**" in APP)


# ══════════════════════════════════════════════════════════════════
head("⑩ 재시도 대상 구분 — 빠른 일시 오류만 다시 시도한다")


class Counting:
    """몇 번 불렸는지 세는 큐. `exc` 를 그대로 던진다."""

    is_local = False

    def __init__(self, exc, sleep=0.0):
        self.exc, self.sleep = exc, sleep
        self.calls = 0

    def list_by_status(self, status, login_id="", account_id="", kind="",
                       limit=0, deadline=None):
        self.calls += 1
        if self.sleep:
            time.sleep(self.sleep)
        raise self.exc

    def list_jobs(self, limit=30):
        raise self.exc


check("영구 오류 목록에 401·403 이 있다",
      401 in PERMANENT and 403 in PERMANENT, str(PERMANENT))
check("  429(요청 과다)·409(충돌)는 영구가 아니다(일시적일 수 있다)",
      429 not in PERMANENT and 409 not in PERMANENT)
check("  5xx 는 영구가 아니다",
      not [s for s in PERMANENT if 500 <= s < 600])

for code in (401, 403, 400, 404, 422):
    new_render()
    q = Counting(SupabaseError(f"거부({code})", status=code))
    why = logout_block(q, "loginW", "")
    check(f"★[{code}] 영구 오류는 다시 시도하지 않는다(1회)", q.calls == 1,
          f"{q.calls}회")
    check(f"  [{code}] 그래도 신규 발행은 막는다(fail-safe)",
          "확인하지 못해 새 작업을 만들지 않았습니다" in why)

for code in (500, 502, 503, 429):
    new_render()
    q = Counting(SupabaseError(f"일시 오류({code})", status=code))
    logout_block(q, "loginW", "")
    check(f"[{code}] 일시 오류는 {TRIES + FAST_EXTRA}회 시도한다",
          q.calls == TRIES + FAST_EXTRA, f"{q.calls}회")

new_render()
q = Counting(SupabaseError("NameResolutionError"))       # 상태코드 없음
logout_block(q, "loginW", "")
check("상태코드가 없는 네트워크 오류도 다시 시도한다(DNS·SSL)",
      q.calls == TRIES + FAST_EXTRA, f"{q.calls}회")

new_render()
q = Counting(SupabaseError("Read timed out"), sleep=FAST_SEC + 0.2)
t0 = time.monotonic()
logout_block(q, "loginW", "")
check("★느린 실패는 상태코드와 무관하게 1회만 시도한다", q.calls == 1,
      f"{q.calls}회 · {time.monotonic() - t0:.2f}초")

#  ★재시도도 예산 안에서만 — 예산이 없으면 더 시도하지 않는다
new_render()
q = Counting(SupabaseError("GET jobs 실패(503)", status=503))
box = render_scope()
box["deadline"] = time.monotonic() - 0.01                 # 예산을 이미 소진
logout_state(q, "loginW", "")
check("★예산이 소진되면 빠른 실패도 재시도하지 않는다", q.calls == 1,
      f"{q.calls}회")


print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
