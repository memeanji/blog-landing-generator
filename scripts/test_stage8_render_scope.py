r"""Stage 8 검증 — 렌더 경계(렌더 스코프)가 정말 렌더마다 새로 시작되는가
    (2026-11-05 · 브랜치 feature/agent-1.3.0-stabilization)

    .\.venv\Scripts\python.exe scripts\test_stage8_render_scope.py
    .\.venv\Scripts\python.exe scripts\test_stage8_render_scope.py --no-app
                                            (실제 Streamlit 런타임 부분 생략)

★왜 이 테스트가 필요한가 — 3단계의 조회 예산(`deadline`)과 조회 캐시는 "렌더 1회"
  동안만 살아야 한다. 처음 구현은 "Streamlit 이 렌더마다 모듈을 다시 실행하므로
  모듈 변수가 저절로 비워진다" 는 **전제**에 기댔다. 그 전제가 틀리면 `deadline`
  이 살아남아 **예산이 영구히 소진**되고, 모든 계정의 조회가 '확인 불가' 가 되어
  **발행이 전부 차단**된다. 그래서 전제를 없애고 `begin_render()` 가 렌더 시작점
  에서 직접 비우도록 바꿨다. 여기서 그것을 확인한다.

★브라우저·네이버·시트·운영 큐·운영 DB·운영 세션을 건드리지 않는다.
  합성 fixture 폴더(임시)만 쓰고, 큐는 LocalStore 다.

검증 항목
  ① `begin_render()` 의미 — 렌더 번호 증가 · 캐시/예산 새로 시작
  ② 같은 렌더 — deadline 공유 · 중복 조회 제거
  ③ 새 렌더 — 캐시·deadline 이 **넘어오지 않는다**(소진된 예산도)
  ④ 계정 사이 캐시 공유 없음
  ⑤ 제출 직전 `fresh=True` 유지
  ⑥ `st.session_state` 를 못 쓰는 환경 폴백
  ⑦ 순서 불변식 — `begin_render()` 앞에 조회를 유발하는 본문이 없다(AST)
  ⑧ ★실제 Streamlit 런타임(AppTest) — 자동 rerun · 버튼 클릭 · 계정 변경에서
     렌더 번호가 실제로 올라가는가
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

#  ★합성 fixture — 운영 brands.json·accounts.json·세션·큐를 읽지도 쓰지도 않는다.
if not (os.getenv("BLOG_DATA_DIR") or "").strip():
    import make_test_fixture                                   # noqa: E402

    _FIX = Path(tempfile.mkdtemp(prefix="blog_stage8_fixture_"))
    make_test_fixture.build(_FIX)
    os.environ["BLOG_DATA_DIR"] = str(_FIX)
    os.environ.setdefault("BLOG_ACCOUNTS_REMOTE", "0")
    print(f"(테스트 fixture: {_FIX})")
os.environ.setdefault("BLOG_QUEUE_BACKEND", "local")

from v2 import queue_store                                     # noqa: E402
from v2.queue_store import HELD, PENDING, RUNNING              # noqa: E402
from v2.supabase_store import SupabaseError                    # noqa: E402

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


class FakeSessionState(dict):
    """`st.session_state` 를 흉내 낸다(get/설정만 쓴다)."""


NS: dict = {"queue_store": queue_store, "time": time, "chr": chr,
            "RUNNING_STATES": (PENDING, RUNNING)}
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
NS["QUEUE_READ_WAIT"] = 0.01
for _n in TREE.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
            "is_logout_job", "begin_render", "render_scope", "render_seq",
            "query_deadline", "store_takes_deadline", "logout_state",
            "_logout_state_read", "logout_block"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)


class FakeSt:
    """`st` 자리에 끼우는 최소 대역 — `session_state` 만 쓴다."""

    def __init__(self, broken: bool = False):
        self.broken = broken
        self.session_state = FakeSessionState()

    def __getattr__(self, name):
        if name == "session_state" and self.broken:
            raise RuntimeError("session_state 를 쓸 수 없는 환경")
        raise AttributeError(name)


NS["st"] = FakeSt()
begin_render = NS["begin_render"]
render_scope, render_seq = NS["render_scope"], NS["render_seq"]
query_deadline = NS["query_deadline"]
logout_state, logout_block = NS["logout_state"], NS["logout_block"]

#  ★설계 ④(2026-11-05) — 운영 코드는 발행 작업 범위를 **account.id** 로 정한다.
#    테스트가 식별자 하나만 주면 그것을 account.id 로 본다(옛 호출부를 그대로 둔다).
_raw_lb = logout_block


def logout_block(store_obj, login_id="", account_id="", **kw):
    return _raw_lb(store_obj, "", (account_id or login_id), **kw)

BUDGET = NS["RENDER_QUERY_BUDGET"]
SEQ_KEY = NS["RENDER_SEQ_KEY"]


class Fake:
    """상태별 조회를 세는 가짜 큐."""

    is_local = False

    def __init__(self, held=0, boom=None):
        self.held, self.boom = held, dict(boom or {})
        self.status_calls = 0

    def list_by_status(self, status, login_id="", account_id="", kind="",
                       limit=0, deadline=None):
        self.status_calls += 1
        if self.boom.get(status):
            raise self.boom[status]
        if deadline is not None and time.monotonic() >= deadline:
            raise SupabaseError("조회 시간이 예산을 넘겨 중단했습니다")
        if status == HELD and self.held:
            return [{"id": "H1", "status": HELD, "kind": "run",
                     "job": {"login_id": login_id, "account": account_id}}]
        return []

    def list_jobs(self, limit=30):
        return []


# ══════════════════════════════════════════════════════════════════
head("① begin_render() — 렌더 번호와 새 저장소")
n1 = begin_render()
check("첫 렌더는 1번", n1 == 1, f"seq={n1}")
check("저장소에 렌더 번호가 남는다", render_seq() == 1)
check("session_state 에도 남는다(경계가 눈에 보인다)",
      NS["st"].session_state.get(SEQ_KEY) == 1)
render_scope()["아무거나"] = "값"
n2 = begin_render()
check("렌더가 바뀌면 번호가 올라간다", n2 == 2, f"seq={n2}")
check("★이전 렌더의 내용은 넘어오지 않는다",
      "아무거나" not in render_scope(), str(sorted(render_scope()))[:60])

head("② 같은 렌더 — deadline 공유 · 중복 조회 제거")
begin_render()
d1 = query_deadline()
time.sleep(0.2)
d2 = query_deadline()
check("★같은 렌더에서는 같은 deadline", d1 == d2, f"차이 {d2 - d1:.4f}초")
q = Fake()
logout_state(q, "loginW", "")
logout_state(q, "loginW", "")
check("같은 렌더·같은 계정은 한 번만 조회한다", q.status_calls == 3,
      f"{q.status_calls}회")

head("③ 새 렌더 — 캐시·예산이 넘어오지 않는다 (★위험 1번)")
begin_render()
before = query_deadline()
time.sleep(0.05)                 # 시계 해상도에 기대지 않는다
begin_render()
after = query_deadline()
check("★새 렌더는 deadline 을 새로 만든다", after > before,
      f"{after - before:.3f}초 늘었다")

#  ★핵심 — **소진된 예산**이 다음 렌더로 넘어오면 모든 발행이 영구 차단된다.
begin_render()
render_scope()["deadline"] = time.monotonic() - 100.0      # 예산을 소진시킨다
q_dead = Fake()
stt_dead = logout_state(q_dead, "loginW", "")
check("소진된 예산에서는 조회가 실패로 처리된다", bool(stt_dead["read_error"]),
      stt_dead["read_error"][:46])

begin_render()                                              # 다음 렌더
q_ok = Fake()
stt_ok = logout_state(q_ok, "loginW", "")
check("★★다음 렌더에서는 정상 조회된다(예산이 되살아난다)",
      not stt_ok["read_error"] and q_ok.status_calls == 3,
      f"read_error={stt_ok['read_error'][:30]!r} · 조회 {q_ok.status_calls}회")
check("  그래서 발행 차단이 풀린다",
      logout_block(Fake(), "loginW", "") == "")

#  캐시도 넘어오지 않는다
begin_render()
q_c = Fake(held=0)
logout_state(q_c, "loginW", "")
begin_render()
q_c2 = Fake(held=1)
again = logout_state(q_c2, "loginW", "")
check("★이전 렌더의 조회 결과를 재사용하지 않는다",
      len(again["held"]) == 1 and q_c2.status_calls == 3,
      f"held {len(again['held'])}건 · 조회 {q_c2.status_calls}회")

head("④ 계정 사이 캐시 공유 없음")
begin_render()
q_two = Fake()
a = logout_state(q_two, "accA", "")
b = logout_state(q_two, "accB", "")
check("계정이 다르면 각각 조회한다", q_two.status_calls == 6,
      f"{q_two.status_calls}회")
check("  결과에 자기 식별자가 들어 있다",
      a["login_id"] == "accA" and b["login_id"] == "accB")
begin_render()
q_ab = Fake()
logout_state(q_ab, "", "tab_x")
logout_state(q_ab, "tab_x", "")
check("login_id 와 account.id 를 섞지 않는다", q_ab.status_calls == 6,
      f"{q_ab.status_calls}회")

head("⑤ 제출 직전 fresh=True 유지")
begin_render()
q_f = Fake(held=0)
logout_state(q_f, "loginW", "")                 # 표시(캐시 채움)
q_f.held = 1                                    # 그 사이 보류가 걸렸다
check("캐시는 옛 값을 준다", logout_state(q_f, "loginW", "")["held"] == [])
check("★fresh=True 는 다시 읽어 보류를 본다",
      len(logout_state(q_f, "loginW", "", fresh=True)["held"]) == 1)
check("  제출 판정이 fresh=True 를 쓴다",
      'logout_state(store_obj, "", aid, fresh=True)' in APP)

head("⑥ session_state 를 못 쓰는 환경 폴백")
saved = NS["st"]
NS["st"] = FakeSt(broken=True)
try:
    f1 = begin_render()
    render_scope()["x"] = 1
    f2 = begin_render()
    check("session_state 가 없어도 터지지 않는다", isinstance(f1, int))
    check("★그 환경에서도 렌더 번호가 올라간다", f2 == f1 + 1, f"{f1} → {f2}")
    check("★그 환경에서도 저장소를 비운다", "x" not in render_scope())
finally:
    NS["st"] = saved

head("⑦ 순서 불변식 — begin_render() 앞에 조회를 유발하는 본문이 없다")
HOT = {"get_store", "live_agents", "logout_state", "logout_block",
       "render_account_tools", "query_deadline", "render_scope", "session_job",
       "list_by_status", "held_jobs"}
begin_line = None
for _n in TREE.body:
    if isinstance(_n, ast.Expr) and isinstance(_n.value, ast.Call) \
            and getattr(_n.value.func, "id", "") == "begin_render":
        begin_line = _n.lineno
check("본문에 begin_render() 호출이 있다", begin_line is not None,
      f"{begin_line}줄")
earlier = []
for _n in TREE.body:
    if begin_line is None or _n.lineno >= begin_line:
        continue
    if isinstance(_n, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                       ast.ClassDef, ast.Assign, ast.AnnAssign)):
        continue
    for _x in ast.walk(_n):
        if isinstance(_x, ast.Call):
            nm = getattr(_x.func, "attr", getattr(_x.func, "id", ""))
            if nm in HOT:
                earlier.append((_n.lineno, nm))
check("★그보다 먼저 조회를 유발하는 본문 실행문이 없다", not earlier,
      str(earlier))
calls = [_n for _n in TREE.body
         if isinstance(_n, ast.Expr) and isinstance(_n.value, ast.Call)
         and getattr(_n.value.func, "id", "") == "begin_render"]
check("  begin_render() 는 본문에서 **한 번만** 부른다", len(calls) == 1,
      f"{len(calls)}회")
check("render_scope() 는 저장소를 스스로 만들지 않는다(받아 쓴다)",
      'globals().setdefault("_render_box"' not in APP)
check("  그래도 먼저 불리면 터지지 않는다(빈 칸을 만든다)",
      "if not isinstance(box, dict):" in APP)

# ══════════════════════════════════════════════════════════════════
if "--no-app" in sys.argv:
    head("⑧ 실제 Streamlit 런타임(AppTest) — 생략(--no-app)")
else:
    head("⑧ ★실제 Streamlit 런타임(AppTest) — 렌더마다 경계가 생기는가")
    import streamlit as real_st
    from streamlit.testing.v1 import AppTest

    def seq_of(a) -> int | None:
        """AppTest 의 `session_state` 는 `.get()` 이 없다 — 키로 직접 읽는다."""
        try:
            return int(a.session_state[SEQ_KEY])
        except Exception:                                      # noqa: BLE001
            return None

    real_st.cache_resource.clear()
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=120)
    app.session_state["authed"] = True          # 비밀번호 게이트 건너뛰기
    app.run()
    check("앱이 예외 없이 뜬다", not app.exception, str(app.exception)[:160])
    seq1 = seq_of(app)
    check("★첫 렌더에서 렌더 번호가 1", seq1 == 1, f"seq={seq1}")

    app.run()                                   # 자동 rerun 과 같은 경로
    seq2 = seq_of(app)
    check("★다시 그리면 렌더 번호가 올라간다(2)", seq2 == 2, f"seq={seq2}")
    check("  다시 그려도 예외 없다", not app.exception, str(app.exception)[:160])

    labels = [b.label for b in app.button]
    hit = [b for b in app.button if b.label == "연결 다시 맺기"]
    if hit:
        hit[0].click().run()
        seq3 = seq_of(app)
        #  ★클릭 1회에 렌더가 **2회** 돌 수 있다 — 버튼 처리 안에서 `st.rerun()`
        #    을 부르면 (클릭 렌더 + 다시 그리기) 두 번이다. 그래도 매 렌더마다
        #    경계가 생기는 것이 핵심이므로 '올라갔는가' 로 본다.
        check("★버튼 클릭(rerun)에서도 올라간다", seq3 is not None
              and seq3 > seq2, f"{seq2} → {seq3}"
              + ("  (핸들러가 st.rerun() 을 불러 2회 렌더)"
                 if seq3 and seq3 - seq2 > 1 else ""))
        check("  클릭 뒤에도 예외 없다", not app.exception, str(app.exception)[:160])
    else:
        check("버튼을 찾을 수 없어 클릭 검증을 건너뛴다", True,
              str(labels)[:120])

    #  계정(기준랜딩 탭) 변경 → rerun
    before_seq = seq_of(app)
    if app.selectbox:
        sb = app.selectbox[0]
        opts = list(getattr(sb, "options", []) or [])
        if len(opts) >= 2:
            cur = sb.value
            nxt = [o for o in opts if o != cur]
            sb.select(nxt[0]).run()
            after_seq = seq_of(app)
            check("★선택 변경(계정·브랜드)에서도 올라간다",
                  after_seq == before_seq + 1, f"{before_seq} → {after_seq}")
            check("  변경 뒤에도 예외 없다", not app.exception,
                  str(app.exception)[:160])
        else:
            check("선택 항목이 1개뿐 — 변경 검증 건너뜀", True, str(opts)[:80])
    else:
        check("selectbox 가 없어 변경 검증 건너뜀", True)

    #  ★여러 번 돌려도 번호가 계속 올라간다(= 매 렌더 새 저장소)
    base = seq_of(app)
    for _ in range(3):
        app.run()
    end = seq_of(app)
    check("★연속 3회 재실행에 번호가 3 올라간다", end == base + 3,
          f"{base} → {end}")
    check("  연속 실행 뒤에도 예외 없다", not app.exception,
          str(app.exception)[:160])
    print(f"   ※ 렌더 번호가 매번 오른다 = `begin_render()` 가 렌더마다 실행됐다 "
          f"= 캐시·예산이 매 렌더 새로 만들어졌다 (총 {end}회 렌더)")


print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print("=" * 76)
sys.exit(1 if fails else 0)
