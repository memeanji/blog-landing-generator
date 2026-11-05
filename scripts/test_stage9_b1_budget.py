r"""Stage 9 검증 — B1 로그인 작업 자동 새로고침 예산 (B안 5등급)
    (2026-11-05 · 브랜치 feature/agent-1.3.0-stabilization)

    .\.venv\Scripts\python.exe scripts\test_stage9_b1_budget.py

★브라우저·네이버·시트·운영 큐·운영 DB·운영 세션을 건드리지 않는다. 가상 시계로
  규칙만 돌린다(실제 대기 없음 → 15분 시나리오도 즉시 끝난다).

검증 항목(사용자 요청 4단계 ①~④)
  ① 예산표 — 승인된 5등급 값 그대로인가
  ② 등급 판정 — 5등급 전체 조합 · 무응답 우선 · '다른 작업 중' 조건
  ③ ticks · 무변화 경과 · 하드 상한의 **독립** 판정
  ④ Agent 가 다른 작업을 15분 이상 수행하는 경우
  ⑤ alive → 무응답 전환 / 무응답 → alive 전환
  ⑥ pending → running 전환
  ⑦ 진행 신호로 예산 초기화 · 시간만 흘러도 초기화되지 않음
  ⑧ 로그인 작업을 **취소하지 않는다**(큐·상태 미변경)
  ⑨ [상태 다시 확인] — 예산·하드 상한 새로 시작
  ⑩ R1~R4 — 발행 작업의 자동 새로고침은 **제한되지 않는다**
  ⑪ 추가 DB 조회 0회(이미 받아 온 `alive` 목록만 쓴다)
"""
from __future__ import annotations

import ast
import io
import math
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("BLOG_DATA_DIR", str(tempfile.mkdtemp(prefix="blog_stage9_")))
os.environ.setdefault("BLOG_ACCOUNTS_REMOTE", "0")
os.environ.setdefault("BLOG_QUEUE_BACKEND", "local")
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from v2 import queue_store                                     # noqa: E402
from v2.queue_store import PENDING, RUNNING                    # noqa: E402

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


# ── 가상 시계 — 실제로 기다리지 않는다 ─────────────────────────────
class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def tick(self, dt):
        self.t += dt

    def sleep(self, dt):                      # 운영 코드가 부르면 시간만 흐른다
        self.t += dt


CLOCK = Clock()


class FakeSt:
    """`st` 자리 — `session_state` 와 위젯 흉내."""

    def __init__(self):
        self.session_state = {}
        self.buttons = []
        self.press = set()
        self.warnings = []
        self.infos = []
        self.reran = 0

    def button(self, label, **kw):
        self.buttons.append(label)
        return label in self.press

    def warning(self, msg, **kw):
        self.warnings.append(str(msg))

    def info(self, msg, **kw):
        self.infos.append(str(msg))

    def rerun(self):
        self.reran += 1


# ── 운영 코드에서 B1 함수와 상수를 그대로 꺼낸다 ────────────────────
APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)
NS: dict = {"queue_store": queue_store, "time": CLOCK, "chr": chr}
for _n in TREE.body:
    if isinstance(_n, ast.Assign) and len(_n.targets) == 1:
        _tg = _n.targets[0]
        _names = ([getattr(e, "id", "") for e in _tg.elts]
                  if isinstance(_tg, ast.Tuple) else [getattr(_tg, "id", "")])
        if _names and all(x.isupper() for x in _names if x):
            try:
                _vals = ast.literal_eval(_n.value)
            except Exception:                                  # noqa: BLE001
                continue
            if isinstance(_tg, ast.Tuple):
                _vals = _vals if isinstance(_vals, tuple) else (_vals,)
                for _k, _v in zip(_names, _vals):
                    NS[_k] = _v
            else:
                NS[_names[0]] = _vals
for _n in TREE.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
            "begin_render", "render_scope", "render_seq", "agent_of",
            "watch_grade", "watch_signal", "watch_login", "watch_login_reset",
            "watch_login_wanted"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)

ST = FakeSt()
NS["st"] = ST
begin_render = NS["begin_render"]
agent_of, watch_grade = NS["agent_of"], NS["watch_grade"]
watch_signal, watch_login = NS["watch_signal"], NS["watch_login"]
watch_login_reset, watch_login_wanted = (NS["watch_login_reset"],
                                         NS["watch_login_wanted"])
BUDGET, LABEL = NS["WATCH_BUDGET"], NS["WATCH_LABEL"]
HARD = NS["WATCH_HARD_SEC"]
WANT_KEY, BUDGET_KEY = NS["WATCH_WANT_KEY"], NS["WATCH_BUDGET_KEY"]

MIN_PERIOD = 2.0            # 운영 코드의 time.sleep(2.0)


def func_calls(name: str) -> set[str]:
    """운영 코드에서 그 함수가 **실제로 부르는 이름**만 뽑는다.

    ★문자열·주석·docstring 을 보면 오탐이 난다(2026-11-05: `box.update(` 가
      'update(' 에, docstring 의 `live_agents()` 가 'agents(' 에 걸렸다).
      그래서 AST 의 Call 노드만 본다. `a.b()` 는 'a.b' 와 'b' 둘 다 담는다.
    """
    for n in TREE.body:
        if not (isinstance(n, ast.FunctionDef) and n.name == name):
            continue
        out: set[str] = set()
        for x in ast.walk(n):
            if not isinstance(x, ast.Call):
                continue
            f = x.func
            if isinstance(f, ast.Attribute):
                owner = getattr(f.value, "id", getattr(
                    getattr(f.value, "func", None), "id", ""))
                out.add(f.attr)
                if owner:
                    out.add(f"{owner}.{f.attr}")
            elif isinstance(f, ast.Name):
                out.add(f.id)
        return out
    return set()


def func_src(name: str) -> str:
    """그 함수의 소스(본문만 — docstring 제외)."""
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            body = n.body[1:] if (n.body and isinstance(n.body[0], ast.Expr)
                                  and isinstance(n.body[0].value, ast.Constant)
                                  and isinstance(n.body[0].value.value, str)
                                  ) else n.body
            return chr(10).join(ast.unparse(b) for b in body)
    return ""


#  ★로그인 감시 UI 블록 — `elif job_state in RUNNING_STATES:` 가 **두 곳**이라
#    (도움 함수 하나 + 본문 하나) 마지막 것을 써야 새 블록을 본다.
PANEL = APP.rsplit("elif job_state in RUNNING_STATES:", 1)[1][:2200]


def job(jid="J1", status=PENDING, made=0, total=0, published=0,
        started_at="", error=""):
    return {"id": jid, "status": status, "made": made, "total": total,
            "published": list(range(published)), "started_at": started_at,
            "error": error}


def agent(alive=True, cur="", state="idle", dev="PC1"):
    return {"device_id": dev, "agent": dev, "alive": alive, "job": cur,
            "state": state}


def drive(rec_fn, agent_fn, period=MIN_PERIOD, limit=5000):
    """렌더를 반복해 `(멈춘 경과초, 등급, 사유, 렌더수)`.

    rec_fn/agent_fn 은 `경과초 -> 레코드/Agent` 다(상태 변화를 흉내 낸다).
    """
    ST.session_state.clear()
    t0 = CLOCK.monotonic()
    for i in range(limit):
        el = CLOCK.monotonic() - t0
        keep, grade, why = watch_login(rec_fn(el), agent_fn(el))
        if not keep:
            return el, grade, why, i
        CLOCK.tick(period)
    return CLOCK.monotonic() - t0, "", "미중단", limit


# ══════════════════════════════════════════════════════════════════
head("① 예산표 — 승인된 B안 5등급")
WANT = {
    "pending_idle":   (90,  120.0),
    "pending_busy":   (675, 900.0),
    "pending_dead":   (23,  30.0),
    "running_mine":   (540, 720.0),
    "running_orphan": (45,  60.0),
}
check("등급이 5개다", len(BUDGET) == 5, str(sorted(BUDGET)))
for g, (tk, st_sec) in WANT.items():
    got = BUDGET.get(g)
    check(f"{g} = ticks {tk} · 무변화 {st_sec:.0f}초", got == (tk, st_sec),
          str(got))
check("하드 상한 = 900초(15분)", HARD == 900.0, str(HARD))
check("다섯 등급 모두 안내 문구가 있다",
      all(g in LABEL and LABEL[g] for g in BUDGET), str(sorted(LABEL)))
loose = all(BUDGET[g][0] >= math.ceil(BUDGET[g][1] / MIN_PERIOD)
            for g in BUDGET)
check("★ticks 는 시간 예산보다 느슨하다(2초 주기에서 시간이 먼저 걸린다)", loose,
      " · ".join(f"{g}:{BUDGET[g][0]}>{BUDGET[g][1] / MIN_PERIOD:.0f}"
                 for g in BUDGET))

head("② 등급 판정 — 5등급 전체 조합")
CASES = [
    ("pending · alive · 유휴", job(), agent(cur=""), "pending_idle"),
    ("pending · alive · 같은 작업 보유", job(), agent(cur="J1"), "pending_idle"),
    ("pending · alive · 다른 작업 중", job(), agent(cur="OTHER"), "pending_busy"),
    ("pending · 무응답", job(), agent(alive=False), "pending_dead"),
    ("pending · 무응답 + 다른 작업", job(), agent(alive=False, cur="OTHER"),
     "pending_dead"),
    ("running · alive · 이 작업 보유", job(status=RUNNING), agent(cur="J1"),
     "running_mine"),
    ("running · alive · 다른 작업", job(status=RUNNING), agent(cur="OTHER"),
     "running_orphan"),
    ("running · alive · 작업 없음", job(status=RUNNING), agent(cur=""),
     "running_orphan"),
    ("running · 무응답", job(status=RUNNING), agent(alive=False, cur="J1"),
     "running_orphan"),
]
for name, rec, ag, want in CASES:
    got = watch_grade(rec, ag)
    check(f"{name} → {want}", got == want, got)
check("★무응답이면 '다른 작업 중' 보다 무응답이 먼저다",
      watch_grade(job(), agent(alive=False, cur="OTHER")) == "pending_dead")
check("★'다른 작업 중' 은 작업 번호가 **있고 다를** 때만",
      watch_grade(job(), agent(cur="")) == "pending_idle"
      and watch_grade(job(), agent(cur="J1")) == "pending_idle")
check("작업 번호가 없으면 감시하지 않는다",
      watch_login({"id": "", "status": PENDING}, agent()) == (False, "", ""))

head("③ ticks · 무변화 경과 · 하드 상한의 독립 판정")
print("%-24s%10s%12s%10s  %s" % ("등급", "주기", "멈춤", "사유", "판정"))
print("-" * 76)
for g in WANT:
    status = RUNNING if g.startswith("running") else PENDING
    cur = "J1" if g in ("running_mine",) else (
        "OTHER" if g == "pending_busy" else "")
    alive = g not in ("pending_dead",)
    if g == "running_orphan":
        alive, cur = False, ""
    for period in (MIN_PERIOD, 3.0, 10.0):
        el, grade, why, _ = drive(
            lambda _t, s=status: job(status=s),
            lambda _t, a=alive, c=cur: agent(alive=a, cur=c), period=period)
        want_sec = min(WANT[g][1], HARD)
        ok = (grade == g) and why.find("진행 변화가 없었습니다") >= 0 \
            and el <= want_sec + period
        print("%-24s%10.1f초%12.0f초%10s  %s"
              % (g, period, el, "시간" if "진행 변화" in why else why[:8],
                 "OK" if ok else "★확인"))
        check(f"  {g} · 주기 {period:.0f}초 → 시간 예산에서 멈춤", ok,
              f"{el:.0f}초 · {why[:40]}")

print("\n   ticks 그물(주기가 비정상적으로 짧을 때)")
for period in (0.5, 0.1):
    el, grade, why, n = drive(lambda _t: job(), lambda _t: agent(),
                              period=period)
    check(f"  주기 {period}초 → ticks 로 멈춘다", "번 했는데" in why,
          f"{el:.1f}초 · 렌더 {n}회 · {why[:34]}")

head("④ Agent 가 다른 작업을 15분 이상 수행 — 하드 상한에서 멈춘다")
el, grade, why, n = drive(lambda _t: job(), lambda _t: agent(cur="OTHER"))
check("★'다른 작업 중' 은 2분이 아니라 하드 상한까지 간다",
      grade == "pending_busy" and 880.0 <= el <= 902.0,
      f"{el:.0f}초 · {grade} · {why[:40]}")
check("  2분(120초)에 멈추지 않는다", el > 300.0, f"{el:.0f}초")
print(f"   → 10건 배치(5~20분) 중 로그인을 눌러도 {el / 60:.0f}분까지 자동 확인이 "
      f"유지된다")

head("⑤ Agent alive ↔ 무응답 전환")
el, grade, why, _ = drive(
    lambda _t: job(),
    lambda t: agent(alive=(t < 20.0), cur=""))
check("★alive → 20초에 무응답 = 무응답 예산(30초)에 바로 걸린다",
      grade == "pending_dead" and 20.0 <= el <= 34.0,
      f"{el:.0f}초 · {grade}")
check("  등급이 빡빡해져도 since 는 리셋되지 않는다(예산이 되살아나지 않는다)",
      el < 60.0, f"{el:.0f}초")
el2, grade2, _, _ = drive(
    lambda _t: job(),
    lambda t: agent(alive=(t >= 10.0), cur=""))
check("무응답 → 10초에 alive = 느슨한 예산이 적용되나 since 는 그대로",
      grade2 == "pending_idle" and 110.0 <= el2 <= 124.0,
      f"{el2:.0f}초 · {grade2}")

head("⑥ 로그인 작업이 pending → running 으로 바뀐다")
#   상태 변화는 **진행 신호**다 → ticks·무변화 경과가 리셋되고 등급도 바뀐다
el, grade, why, _ = drive(
    lambda t: job(status=(RUNNING if t >= 40.0 else PENDING),
                  started_at=("2026-11-05T10:00:00" if t >= 40.0 else "")),
    lambda _t: agent(cur="J1"))
check("★pending(2분) → 40초에 running(12분) 으로 바뀌면 예산이 리셋된다",
      grade == "running_mine" and 750.0 <= el <= 764.0,
      f"{el:.0f}초 · {grade} (40초 + 720초 = 760초)")
check("  전환이 진행 신호로 인정된다(status·started_at 변화)",
      watch_signal(job()) != watch_signal(job(status=RUNNING)))

head("⑦ 진행 신호로만 예산이 초기화된다")
el, grade, why, _ = drive(
    lambda t: job(made=int(t // 30)),          # 30초마다 made 증가
    lambda _t: agent(cur=""))
check("★진행이 잦으면(30초마다) 하드 상한까지 간다",
      880.0 <= el <= 902.0 and "분 동안 계속했습니다" in why,
      f"{el:.0f}초 · {why[:40]}")
el, grade, why, _ = drive(
    lambda t: job(made=int(t // 130)),         # 130초마다 = 예산보다 드물다
    lambda _t: agent(cur=""))
check("  진행이 드물면(130초마다) 시간 예산에서 멈춘다",
      110.0 <= el <= 124.0, f"{el:.0f}초 · {why[:40]}")
el, grade, why, _ = drive(lambda _t: job(), lambda _t: agent(cur=""))
check("★시간만 흘러도 예산은 초기화되지 않는다(진행 없음 → 2분)",
      110.0 <= el <= 124.0, f"{el:.0f}초")
#   연결 상태(하트비트)는 진행 신호가 아니다
el, grade, why, _ = drive(
    lambda _t: job(),
    lambda t: agent(cur="", state=("busy" if int(t) % 4 else "idle")))
check("★하트비트·state 변화는 진행 신호가 아니다(예산이 늘지 않는다)",
      110.0 <= el <= 124.0, f"{el:.0f}초")
sig_body = func_src("watch_signal")
check("  진행 신호에 연결 상태(alive·last_seen·state)가 들어 있지 않다",
      not any(k in sig_body for k in ("alive", "last_seen", "'state'",
                                      chr(34) + "state" + chr(34))),
      sig_body.replace(chr(10), " ")[:80])
check("  진행 신호는 작업 레코드의 6개 항목이다",
      all(k in sig_body for k in ("status", "started_at", "made", "total",
                                  "published", "error")))

head("⑧ 로그인 작업을 취소하지 않는다")
src = APP.split("def watch_login(")[1].split("def watch_login_reset")[0]
check("★watch_login 이 cancel_requested 를 쓰지 않는다",
      "cancel" not in src.lower(), "")
wl_calls = func_calls("watch_login")
BAD = {"request_cancel", "hold_account", "resume_account", "finish",
       "submit", "claim", "list_by_status"}
check("  큐를 건드리는 호출이 없다(submit·claim·finish·cancel·hold/resume)",
      not (wl_calls & BAD), str(sorted(wl_calls & BAD)))
check("  store 를 통한 호출이 전혀 없다",
      not [c for c in wl_calls if c.startswith(("store.", "store_obj."))],
      str(sorted(wl_calls)))
reset_src = APP.split("def watch_login_reset")[1].split("def watch_login_wanted")[0]
check("  [상태 다시 확인] 도 작업을 건드리지 않는다",
      "cancel" not in reset_src.lower()
      and "request_cancel" not in reset_src)
panel = PANEL
check("★중단 안내에 '취소되지 않았습니다' 를 적는다",
      "취소되지 않았습니다" in panel)
check("  큐·상태를 바꾸지 않았다고 적는다",
      "작업 상태를 바꾸지 않았습니다" in panel)
check("  지금 상태(등급)를 보여 준다", "WATCH_LABEL" in panel)

head("⑨ [상태 다시 확인] — 예산·하드 상한 새로 시작")
ST.session_state.clear()
t0 = CLOCK.monotonic()
while True:
    keep, grade, why = watch_login(job(), agent(cur=""))
    if not keep:
        break
    CLOCK.tick(MIN_PERIOD)
first = CLOCK.monotonic() - t0
check("1차: 2분에 멈춘다", 110.0 <= first <= 124.0, f"{first:.0f}초")
box = ST.session_state.get(BUDGET_KEY)
check("  멈춘 사유가 남는다", bool(box and box.get("stopped")))
watch_login_reset()
check("★버튼을 누르면 예산 상태가 지워진다",
      BUDGET_KEY not in ST.session_state)
check("  갱신 요청도 0 으로 내린다", ST.session_state.get(WANT_KEY) == 0)
keep2, _, _ = watch_login(job(), agent(cur=""))
check("★재개 첫 주기에 갱신이 다시 켜진다", keep2 is True)
t1 = CLOCK.monotonic()
while True:
    keep, _, _ = watch_login(job(), agent(cur=""))
    if not keep:
        break
    CLOCK.tick(MIN_PERIOD)
second = CLOCK.monotonic() - t1
check("  재개 뒤 다시 2분을 간다(하드 상한도 새로 시작)",
      110.0 <= second <= 124.0, f"{second:.0f}초")
check("UI 에 [상태 다시 확인] 버튼이 있다", '"상태 다시 확인"' in APP)
check("  그 버튼이 watch_login_reset() 을 부른다",
      "watch_login_reset()" in panel)

head("⑩ R1~R4 — 발행 작업의 자동 새로고침은 제한되지 않는다")
tail = APP.split("if st.session_state.get(\"_auto_refresh\")")[1][:200]
check("R3 발행 경로는 `_auto_refresh` 로 **무조건** 갱신한다",
      "or watch_login_wanted()" in tail, tail.split(chr(10))[0][:60])
set_lines = [ln.strip() for ln in APP.split(chr(10))
             if '_auto_refresh"] = True' in ln]
check("★`_auto_refresh = True` 를 쓰는 곳은 발행 감시 **한 곳**뿐",
      len(set_lines) == 1, str(set_lines))
pub = APP.split("watch_run = store.get(running_id)")[1][:400]
check("  발행 감시 블록에 예산이 없다(watch_login 미사용)",
      "watch_login" not in pub and "_auto_refresh" in pub)
login_blk = APP.split("elif job_state in RUNNING_STATES:")[1][:1200]
check("  로그인 경로는 `_auto_refresh` 를 건드리지 않는다",
      '_auto_refresh"] = True' not in login_blk)

#   런타임 조합 — 렌더 번호를 함께 보므로 지난 렌더 요청이 새지 않는다
def wanted(auto, want_seq):
    ST.session_state.clear()
    ST.session_state["_auto_refresh"] = auto
    begin_render()                             # 렌더 1
    seq = NS["render_seq"]()
    ST.session_state[WANT_KEY] = (seq if want_seq == "this" else
                                  (seq - 1 if want_seq == "prev" else 0))
    return bool(ST.session_state.get("_auto_refresh") or watch_login_wanted())


print("%-18s%-18s%s" % ("발행 요청", "로그인 요청", "결과"))
print("-" * 76)
R = [("없음", False, 0, False), ("있음", True, 0, True),
     ("없음(이번 렌더)", False, "this", True),
     ("없음(지난 렌더)", False, "prev", False),
     ("있음(예산 소진)", True, 0, True)]
for label, auto, want, expect in R:
    got = wanted(auto, want)
    print("%-18s%-18s%s" % (label, str(want), "갱신" if got else "중단"))
    check(f"  {label} → {'갱신' if expect else '중단'}", got == expect, str(got))
check("★R1 로그인 예산이 소진돼도 발행이 돌면 갱신이 유지된다",
      wanted(True, 0) is True)
check("★R2 발행이 끝나면 로그인 예산이 적용된다",
      wanted(False, 0) is False)
check("★지난 렌더의 로그인 요청은 새지 않는다(렌더 번호 대조)",
      wanted(False, "prev") is False)

head("⑪ 추가 DB 조회 0회 — 이미 받아 온 목록만 쓴다")
ao_calls = func_calls("agent_of")
check("agent_of 가 조회를 하지 않는다(store·agents·device 호출 없음)",
      not (ao_calls & {"agents", "device", "list_by_status", "list_jobs",
                       "get_store", "live_agents"}),
      str(sorted(ao_calls)))
check("  live_agents() 결과를 나눠 쓴다(화면이 매 렌더 이미 호출한다)",
      "agent_of(alive," in APP and "alive = live_agents(store)" in APP)
check("watch_login 이 조회를 하지 않는다",
      not (wl_calls & {"agents", "device", "list_by_status", "list_jobs",
                       "get_store", "live_agents", "session_job"}),
      str(sorted(wl_calls)))
ag = agent_of([agent(dev="PC1", cur="X"), agent(dev="PC2")], "PC2")
check("device_id 로 정확히 고른다", ag.get("device_id") == "PC2", str(ag))
check("목록에 없으면 빈 dict(=무응답 취급)",
      agent_of([agent(dev="PC1")], "PC9") == {}
      and watch_grade(job(), {}) == "pending_dead")
check("device_id 가 비면 빈 dict", agent_of([agent()], "") == {})


print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print("=" * 76)
sys.exit(1 if fails else 0)
