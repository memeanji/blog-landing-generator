r"""Stage 4 안정화 검증 — 2026-11-04 변경분 (브랜치 feature/agent-1.3.0-stabilization)

    .\.venv\Scripts\python.exe scripts\test_stage4_stabilization.py

★브라우저·네이버·시트·운영 큐·운영 DB 를 건드리지 않는다. 임시 폴더만 쓴다.
  네트워크로 나가지 않는다(Supabase 는 `_rest` 를 가로채 호출만 기록한다).

검증 대상(이번에 고친 것)
  A. login_id 정규화 — `shin894894` ↔ `shin894894@naver.com` 을 같은 계정으로
  B. 그 정규화가 login_state 의 등급·별칭·보류·공유판정에 실제로 먹는가
  C. held 조회 창 문제 — 작업이 많아도 보류 작업을 놓치지 않는가
     (= 로그아웃 대기 계정에 새 발행 작업이 다시 만들어지지 않는가)
  D. 보류/재개의 원자성 — 상태 조건이 PATCH 에 그대로 붙어 있는가
  E. 네이버 인증 ID 읽기 — 기본 꺼짐 · 실패해도 예외 없음 · 등급 상승 경로
  F. 회귀 — 발행 경로·Job 구조·claim·멱등성은 그대로인가
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage4_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ.pop("BLOG_LOGIN_CHECK", None)
os.environ.pop("BLOG_READ_LOGIN_ID", None)
os.environ.pop("BLOG_ALLOW_HARD_LOGOUT", None)

from v2 import browser, login_state, loginid, queue_store, session_store  # noqa: E402
from v2.accounts import Account                                          # noqa: E402
from v2.job import Job                                                   # noqa: E402
from v2.queue_store import (HELD, LocalStore, PENDING, RUNNING,          # noqa: E402
                            matches)

OK, NG = "  [o]", "  [X]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def head(title: str) -> None:
    print("\n" + "=" * 74)
    print(f" {title}")
    print("=" * 74)


def acct(ident: str, **kw) -> Account:
    acc = Account(id=ident, **kw)
    session_store.ensure(acc)
    return acc


# ══════════════════════════════════════════════════════════════════
head("A. login_id 정규화 (v2/loginid.py)")

check("None·빈 값은 빈 문자열", loginid.norm_login(None) == ""
      and loginid.norm_login("   ") == "")
check("앞뒤 공백 제거", loginid.norm_login("  shin894894 ") == "shin894894")
check("@naver.com 을 떼어 본다",
      loginid.norm_login("shin894894@naver.com") == "shin894894")
check("도메인 대소문자도 뗀다",
      loginid.norm_login("Shin894894@NAVER.COM") == "Shin894894")
check("@mail.naver.com·@naver.net 도 뗀다",
      loginid.norm_login("a1@mail.naver.com") == "a1"
      and loginid.norm_login("a1@naver.net") == "a1")
check("남의 도메인은 **그대로 둔다**(다른 계정일 수 있다)",
      loginid.norm_login("someone@gmail.com") == "someone@gmail.com")
check("도메인만 있는 값은 떼지 않는다(빈 ID 가 되지 않게)",
      loginid.norm_login("@naver.com") == "@naver.com")

check("표기만 다른 같은 계정 = 같다",
      loginid.same_login("shin894894", "shin894894@naver.com")
      and loginid.same_login("shin894894@naver.com", "SHIN894894"))
check("빈 값은 언제나 다르다(추측 금지)",
      not loginid.same_login("", "") and not loginid.same_login("a", "")
      and not loginid.same_login("", "a"))
check("정말 다른 계정은 다르다",
      not loginid.same_login("shin894894", "other894894"))
check("★blog_id 비교(same_id)에는 도메인 제거가 적용되지 않는다",
      not login_state.same_id("blog@naver.com", "blog")
      and login_state.same_id("Blog7", "blog7"))
check("login_state 가 같은 정규화를 재수출한다",
      login_state.same_login is loginid.same_login)


# ══════════════════════════════════════════════════════════════════
head("B. 정규화가 login_state 에 실제로 먹는가")

#   운영 등록표(11/03)에 실제로 있는 조합: 등록은 도메인 포함, 네이버가 주는
#   인증 ID 는 보통 도메인 없는 형태다. 예전 코드는 이 둘을 **다른 계정**으로 봤다.
a1 = acct("acc_dom", blog_id="seoyoungene_", login_id="shin894894@naver.com",
          label="등록은 도메인 포함")
st1 = login_state.record_login_result(
    a1, seen_blog_id="seoyoungene_", expected_blog_id="seoyoungene_",
    expected_login_id=a1.login_id, authenticated_login_id="shin894894")
check("등록 도메인 포함 + 인증 ID 도메인 없음 → authenticated",
      st1["grade"] == login_state.AUTHENTICATED, st1["grade"])

a2 = acct("acc_bare", blog_id="hangbokhaseo7", login_id="rhksrhf6996")
st2 = login_state.record_login_result(
    a2, seen_blog_id="hangbokhaseo7", expected_blog_id="hangbokhaseo7",
    expected_login_id=a2.login_id,
    authenticated_login_id="rhksrhf6996@naver.com")
check("등록 도메인 없음 + 인증 ID 도메인 포함 → authenticated",
      st2["grade"] == login_state.AUTHENTICATED, st2["grade"])

a3 = acct("acc_other", blog_id="esthersmemories", login_id="ggomdoriii")
st3 = login_state.record_login_result(
    a3, seen_blog_id="esthersmemories", expected_blog_id="esthersmemories",
    expected_login_id=a3.login_id, authenticated_login_id="someoneelse")
check("정말 다른 계정은 authenticated 를 주지 않는다",
      st3["grade"] != login_state.AUTHENTICATED, st3["grade"])
check("인증 ID 를 못 읽으면(빈 값) 등급을 올리지 않는다",
      login_state.record_login_result(
          acct("acc_noid", blog_id="b9", login_id="who9"),
          seen_blog_id="b9", expected_blog_id="b9", expected_login_id="who9",
          authenticated_login_id="")["grade"] != login_state.AUTHENTICATED)

ok1, g1, why1 = login_state.check_publish(a1, expected_login_id="shin894894",
                                          expected_blog_id="seoyoungene_",
                                          seen_blog_id="seoyoungene_")
check("★check_publish — 표기만 다른 같은 계정을 막지 않는다(회귀 방지)",
      ok1 is True, why1 or "허용")
ok3, g3, why3 = login_state.check_publish(a3, expected_login_id="ggomdoriii",
                                          expected_blog_id="esthersmemories",
                                          seen_blog_id="esthersmemories")
check("check_publish — 인증된 login_id 가 정말 다르면 막는다",
      ok3 is False and "로그인 계정이 다릅니다" in why3, why3)
okb, _gb, whyb = login_state.check_publish(a1, expected_blog_id="seoyoungene_",
                                           seen_blog_id="otherblog")
check("check_publish — 발행 대상 블로그 불일치는 그대로 막는다",
      okb is False and "블로그가 다릅니다" in whyb, whyb)

linked, lwhy = login_state.link_session("shin894894", a1,
                                        grade=login_state.AUTHENTICATED)
check("별칭 — 등록 표기와 달라도 같은 세션으로 연결된다", linked, lwhy)
check("별칭 조회 — 도메인 포함으로 물어도 같은 세션을 찾는다",
      login_state.session_for_login("shin894894@naver.com") == "acc_dom",
      login_state.session_for_login("shin894894@naver.com"))
linked_bad, bad_why = login_state.link_session("someoneelse", a1,
                                               grade=login_state.AUTHENTICATED)
check("★별칭 — 정말 다른 계정은 그 세션에 연결되지 않는다",
      linked_bad is False, bad_why)
#   매핑이 아직 비어 있는 세션에도 등록표의 login_id 와 어긋나면 붙지 않는다
fresh_acc = acct("acc_fresh", blog_id="b_fresh", login_id="ownerA")
check("매핑이 비어 있어도 등록 login_id 와 어긋나면 연결 거부",
      login_state.link_session("intruderB", fresh_acc,
                               grade=login_state.MANUAL)[0] is False)
check("등록 login_id 와 같으면(표기만 달라도) 연결된다",
      login_state.link_session("ownerA@naver.com", fresh_acc,
                               grade=login_state.MANUAL)[0] is True)

login_state.mark_logout_pending("shin894894@naver.com", session_id="acc_dom")
check("로그아웃 대기 — 도메인 없는 표기로 물어도 대기 중으로 보인다",
      login_state.is_logout_pending("shin894894"))
check("다른 계정은 대기 중이 아니다",
      not login_state.is_logout_pending("rhksrhf6996"))
check("대기 해제 — 표기가 달라도 해제된다",
      login_state.clear_logout_pending("shin894894")
      and not login_state.is_logout_pending("shin894894@naver.com"))
check("빈 login_id 로는 대기표를 건드리지 않는다",
      login_state.clear_logout_pending("") is False)

#   세션 공유 판정 — 같은 로그인 계정(표기만 다름)이면 로그아웃을 막지 않는다
share = SANDBOX / "shared_profile"
share.mkdir(parents=True, exist_ok=True)
s_a = Account(id="share_a", login_id="shin894894@naver.com",
              blog_id="seoyoungene_", profile_dir=str(share), label="A")
s_b = Account(id="share_b", login_id="shin894894", blog_id="seoyoungene_",
              profile_dir=str(share), label="B")
s_c = Account(id="share_c", login_id="otherperson", blog_id="seoyoungene_",
              profile_dir=str(share), label="C")
for a in (s_a, s_b, s_c):
    session_store.ensure(a)
acc_file = SANDBOX / "share_accounts.json"
acc_file.write_text(json.dumps(
    {"accounts": [s_a.to_dict(), s_b.to_dict()]}, ensure_ascii=False),
    encoding="utf-8")
allowed, peers, why = login_state.logout_allowed(s_a, acc_file)
check("공유 세션 — 같은 로그인 계정(표기 다름)이면 로그아웃 허용",
      allowed is True and len(peers) == 1, why)
acc_file.write_text(json.dumps(
    {"accounts": [s_a.to_dict(), s_c.to_dict()]}, ensure_ascii=False),
    encoding="utf-8")
allowed2, peers2, why2 = login_state.logout_allowed(s_a, acc_file)
check("공유 세션 — 로그인 계정이 다르면 자동 로그아웃 금지(그대로)",
      allowed2 is False and len(peers2) == 1, why2)


# ══════════════════════════════════════════════════════════════════
head("C. held 조회 창 — 작업이 많아도 보류를 놓치지 않는가")

store = LocalStore(SANDBOX / "q_window")
#   ★가장 **먼저** 들어온 작업을 보류시키고, 그 뒤로 다른 계정 작업 200건을 넣는다.
#     예전 방식(list_jobs(limit=120) 훑기)이라면 보류 작업이 창 밖으로 밀린다.
mine = store.submit(Job(brand="repurely", account="acc_w", login_id="loginW",
                        flow="production", count=10),
                    title="내 배치", target_agent="PC1")
held_ids = store.hold_account("loginW", device="PC1")
check("보류 1건 걸림", held_ids == [mine], str(held_ids))

for i in range(200):
    store.submit(Job(brand="repurely", account=f"acc_{i}",
                     login_id=f"other{i}", flow="production", count=1),
                 title=f"남의 작업 {i}", target_agent="PC1")

window = [r for r in store.list_jobs(limit=120)
          if r.get("status") == HELD and matches(r, "loginW", "")]
check("★예전 방식(최근 120건 훑기)으로는 보류를 못 찾는다(문제 재현)",
      window == [], f"{len(window)}건")
found = store.held_jobs("loginW")
check("★고친 방식(상태로 좁혀 조회)은 보류를 찾는다",
      [r["id"] for r in found] == [mine], f"{len(found)}건")

check("list_by_status — kind 필터가 먹는다",
      store.list_by_status(PENDING, kind="session") == []
      and len(store.list_by_status(PENDING, kind="run")) == 200)
check("list_by_status — 식별자를 주면 그 계정만",
      [r["id"] for r in store.list_by_status(HELD, "loginW", "")] == [mine])
check("list_by_status — 식별자 표기가 달라도 찾는다(정규화)",
      [r["id"] for r in store.list_by_status(HELD, "loginW@naver.com", "")]
      == [mine])
check("running_for_login — 식별자가 없으면 아무것도 돌려주지 않는다",
      store.running_for_login("", "") == [])
check("held_jobs — 식별자가 없으면 전체 보류를 돌려준다(기존 동작 유지)",
      [r["id"] for r in store.held_jobs()] == [mine])

#   login_id 가 없는 계정은 account.id 정확일치로 — 이번 변경에도 그대로여야 한다
store2 = LocalStore(SANDBOX / "q_noid")
nid = store2.submit(Job(brand="repurely", account="tab_abc", login_id="",
                        flow="production", count=10), title="x",
                    target_agent="PC1")
store2.hold_account("", account_id="tab_abc")
check("login_id 없는 계정 — account.id 로 보류/조회된다",
      [r["id"] for r in store2.held_jobs("", "tab_abc")] == [nid])
check("login_id 없는 계정 — 비슷한 이름(tab_ab)으로는 안 잡힌다",
      store2.held_jobs("", "tab_ab") == [])


# ══════════════════════════════════════════════════════════════════
head("C-2. 화면(logout_state) 이 그 보류를 보고 새 작업을 막는가")

#   ★`streamlit_app.py` 는 모듈로 import 하면 페이지 전체가 실행된다(bare 모드
#     에서 실패한다). 그래서 **운영 코드 본문 그대로** 세 함수만 꺼내 실행한다.
src = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
tree = ast.parse(src)
want = {"is_logout_job", "render_scope", "query_deadline",
        "store_takes_deadline", "logout_state", "_logout_state_read",
        "logout_block"}
#  ★운영 코드가 쓰는 모듈 수준 이름을 그대로 넣어 준다(새로 생기면 여기에 추가).
#    `QUEUE_READ_TRIES/WAIT` = 큐 조회 재시도(2026-11-04).
ns: dict = {"queue_store": queue_store, "time": time,
            "RUNNING_STATES": (queue_store.PENDING, queue_store.RUNNING),
            "QUEUE_READ_TRIES": 2, "QUEUE_READ_WAIT": 0.01, "chr": chr}
for _node in tree.body:                       # 상수는 소스에서 직접 읽어 맞춘다
    if not isinstance(_node, ast.Assign) or len(_node.targets) != 1:
        continue
    _t = _node.targets[0]
    _names = ([getattr(e, "id", "") for e in _t.elts]
              if isinstance(_t, ast.Tuple) else [getattr(_t, "id", "")])
    #  ★대문자 모듈 상수는 전부 집어온다(2026-11-05) — 이름을 하나씩 적으면
    #    상수가 늘 때 테스트가 옛 값을 쓴다.
    if not (_names and all(x.isupper() for x in _names if x)):
        continue
    try:
        _vals = ast.literal_eval(_node.value)
    except Exception:                                          # noqa: BLE001
        continue
    #  ★값이 튜플인 상수는 풀어헤치지 않는다(2026-11-05).
    if isinstance(_t, ast.Tuple):
        _vals = _vals if isinstance(_vals, tuple) else (_vals,)
        for _n, _v in zip(_names, _vals):
            ns[_n] = _v
    else:
        ns[_names[0]] = _vals
ns["QUEUE_READ_WAIT"] = 0.01                  # 테스트는 기다리지 않는다
#  ★3단계(2026-11-05) — 느린 실패는 1회, 빠른 실패만 한 번 더. 상세 검증은
#    scripts/test_stage7_ui_budget.py 에 있다. 여기서는 값이 읽혔는지만 본다.
check("재시도 정책을 운영 코드에서 그대로 읽어 왔다",
      ns.get("QUEUE_READ_TRIES", 0) >= 1
      and ns.get("QUEUE_FAST_EXTRA", 0) >= 1,
      f"tries={ns.get('QUEUE_READ_TRIES')} · "
      f"fast_extra={ns.get('QUEUE_FAST_EXTRA')}")
picked = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in want:
        exec(compile(ast.Module([node], []), "streamlit_app.py", "exec"), ns)
        picked.append(node.name)
check("화면 함수를 운영 코드에서 그대로 꺼냈다", sorted(picked) == sorted(want),
      str(sorted(picked)))


#  ★테스트에서는 호출 1회 = 렌더 1회로 본다(렌더 스코프를 비운다).
#    운영은 렌더마다 모듈을 다시 실행해 캐시·예산이 새로 생긴다.
def _render_reset() -> None:
    ns.setdefault("_render_box", {}).clear()


_rls, _rlb = ns["logout_state"], ns["logout_block"]
ns["logout_state"] = lambda s, l, a="", **k: (_render_reset(), _rls(s, l, a, **k))[1]
ns["logout_block"] = lambda s, l, a="", **k: (_render_reset(), _rlb(s, l, a, **k))[1]

stt = ns["logout_state"](store, "loginW", "")
check("★화면이 보류를 본다(작업 201건 뒤에 있어도)",
      len(stt["held"]) == 1 and stt["pending"] is True,
      f"held={len(stt['held'])} pending={stt['pending']}")
check("화면 상태에 읽기 오류 표시가 없다", stt.get("read_error") == "")
block = ns["logout_block"](store, "loginW", "")
check("★새 발행 작업을 막는다(차단 누락 해소)",
      "로그아웃 대기 상태입니다" in block and "보류된 작업 1건" in block)
check("다른 계정은 막지 않는다", ns["logout_block"](store, "other1", "") == "")

logout_job = store.submit(Job(brand="repurely", account="acc_w",
                              login_id="loginW",
                              extra=["--logout", "acc_w", "--events"]),
                          kind="session", title="로그아웃", target_agent="PC1")
stt2 = ns["logout_state"](store, "loginW", "")
check("대기 중 로그아웃 작업도 상태로 좁혀 찾는다",
      (stt2["logout_job"] or {}).get("id") == logout_job)

back = store.resume_account("loginW", device="PC1")
check("재개하면 보류가 풀린다", back == [mine] and store.held_jobs("loginW") == [])


class OldStore:
    """상태별 조회가 **없는** 옛 저장소 — 화면이 예전 방식으로 내려가야 한다."""

    is_local = True

    def __init__(self, rows):
        self.rows = rows

    def list_jobs(self, limit: int = 30):
        return self.rows[:limit]


old_rows = [{"id": "h1", "status": HELD, "kind": "run",
             "job": {"login_id": "loginW", "account": "acc_w"}}]
stt3 = ns["logout_state"](OldStore(old_rows), "loginW", "")
check("옛 저장소(list_by_status 없음)에서도 보류를 찾는다(폴백)",
      len(stt3["held"]) == 1 and stt3["pending"] is True)


class BrokenStore:
    def list_by_status(self, *_a, **_k):
        raise RuntimeError("queue down")

    def list_jobs(self, limit: int = 30):
        raise RuntimeError("queue down")


stt4 = ns["logout_state"](BrokenStore(), "loginW", "")
check("큐를 못 읽으면 '보류 없음' 이 아니라 **읽기 실패**로 남는다",
      stt4["pending"] is False and "queue down" in stt4["read_error"],
      stt4["read_error"])
check("화면이 그 사실을 사용자에게 알리는 코드가 있다",
      'stt.get("read_error")' in src and "보류 상태를 확인하지 못했습니다" in src)


# ══════════════════════════════════════════════════════════════════
head("D. 보류/재개의 원자성 — Supabase 쪽 (네트워크 없음)")

from v2.supabase_store import SupabaseStore                    # noqa: E402

sup = SupabaseStore(mode="service", url="http://127.0.0.1:9", key="dummy-key")
calls: list[tuple[str, str]] = []


def _page(path: str, rows: list) -> list:
    """가짜 서버가 offset/limit 을 **정직하게** 지키게 한다(2026-11-05).

    ★병합한 `_page_all` 은 '0행 응답' 또는 '총건수 도달' 만 끝으로 인정하고
      offset 을 받은 행 수만큼 전진시킨다. 같은 묶음을 매번 돌려주는 가짜 서버는
      끝을 표현할 수 없어 상한 초과 예외가 난다(실제 PostgREST 는 끝에서 0행).
    """
    off = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
    lim = int(path.split("limit=")[1].split("&")[0]) if "limit=" in path else 200
    return rows[off:off + lim]


def _wire_range(s, counter=None):
    """`_rest_range`(행+총건수를 한 응답에서) 를 `_rest` 픽스처로 흉내낸다.

    ★가로채지 않으면 테스트가 **실제 HTTP 를 시도**한다(가짜 주소 → ConnectionError).
    """
    def _range(query, count=False, timeout=None):
        #  ★timeout 은 실제 구현이 넘기는 인자다(UI 예산). 가짜는 받기만 한다.
        rows = s._rest("GET", query) or []                     # noqa: SLF001
        total = counter(query) if (count and counter is not None) else None
        return (rows if isinstance(rows, list) else []), total
    s._rest_range = _range                                     # noqa: SLF001
    return s
ROWS = [
    {"job_id": "J1", "status": "pending", "kind": "run", "device_id": "PC1",
     "payload": {"login_id": "loginW@naver.com", "account": "acc_w"}},
    {"job_id": "J2", "status": "pending", "kind": "run", "device_id": "PC1",
     "payload": {"login_id": "someoneelse", "account": "acc_x"}},
    {"job_id": "J3", "status": "pending", "kind": "session", "device_id": "PC1",
     "payload": {"login_id": "loginW", "account": "acc_w"}},
    {"job_id": "J4", "status": "pending", "kind": "run", "device_id": "PC2",
     "payload": {"login_id": "loginW", "account": "acc_w"}},
]


def fake_rest(method, path, **kw):
    calls.append((method, path))
    if method == "GET":
        want = "pending" if "status=eq.pending" in path else "none"
        kind_run = "kind=eq.run" in path
        return _page(path, [r for r in ROWS if r["status"] == want
                            and (not kind_run or r["kind"] == "run")])
    if method == "PATCH":
        ids = path.split("in.(")[1].split(")")[0].split(",")
        return [{"job_id": i} for i in ids]
    return None


sup._rest = fake_rest                                          # noqa: SLF001
_wire_range(sup)
done = sup.hold_account("loginW", device="PC1")
check("★표기가 다른 작업(loginW@naver.com)을 고른다 — 예전엔 0건이었다",
      done == ["J1"], str(done))
gets = [p for m, p in calls if m == "GET"]
patches = [p for m, p in calls if m == "PATCH"]
check("①상태로 좁혀 조회한다", any("status=eq.pending" in p for p in gets)
      and any("kind=eq.run" in p for p in gets), str(gets))
check("★②PATCH 에 status 조건이 그대로 붙어 있다(원자성)",
      all("status=eq.pending" in p for p in patches), str(patches))
check("②PATCH 는 job_id 목록으로 지정한다",
      all("job_id=in.(" in p for p in patches))
check("다른 계정(J2)·session 작업(J3)·다른 PC(J4)는 고르지 않는다",
      "J2" not in str(patches) and "J3" not in str(patches)
      and "J4" not in str(patches), str(patches))

calls.clear()
check("식별자가 없으면 **아무 요청도 보내지 않는다**",
      sup.hold_account("", account_id="") == [] and calls == [])

calls.clear()
ROWS[:] = [{"job_id": f"K{i}", "status": "held", "kind": "run",
            "device_id": "PC1",
            "payload": {"login_id": "loginW", "account": "acc_w"}}
           for i in range(120)]


def fake_rest2(method, path, **kw):
    calls.append((method, path))
    if method == "GET":
        return _page(path, [r for r in ROWS if "status=eq.held" in path])
    if method == "PATCH":
        ids = path.split("in.(")[1].split(")")[0].split(",")
        return [{"job_id": i} for i in ids]
    return None


sup._rest = fake_rest2                                         # noqa: SLF001
_wire_range(sup)
back2 = sup.resume_account("loginW", device="PC1")
check("재개도 같은 경로로 동작한다(120건)", len(back2) == 120, f"{len(back2)}건")
check("50건씩 나눠 보낸다(URL 길이 안전)",
      len([p for m, p in calls if m == "PATCH"]) == 3,
      f"PATCH {len([p for m, p in calls if m == 'PATCH'])}회")
check("재개 PATCH 에도 status 조건이 붙어 있다(held 만)",
      all("status=eq.held" in p for m, p in calls if m == "PATCH"))

check("list_by_status 는 서버에서 상태로 좁힌다(소스 확인)",
      'q = (f"jobs?status=eq.{quote(status' in
      (ROOT / "v2" / "supabase_store.py").read_text(encoding="utf-8"))
check("claim 경로(claim_job RPC)는 손대지 않았다",
      "update public.jobs j set status = 'running'" in
      (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8")
      and "where device_id = v_id and status = 'pending'" in
      (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════
head("E. 네이버 인증 ID 읽기 — 기본 꺼짐 · 실패해도 예외 없음")

import asyncio                                                 # noqa: E402


class FakePage:
    def __init__(self, cands=None, boom=False):
        self.cands, self.boom, self.goto_count = cands or [], boom, 0

    async def goto(self, url, **kw):
        self.goto_count += 1
        if self.boom:
            raise RuntimeError("net down")

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, js):
        return {"href": "https://nid.naver.com/", "cands": self.cands}


class FakeCtx:
    def __init__(self, page):
        self.pages = [page]


def read(cands=None, boom=False, env_on=False):
    page = FakePage(cands, boom)
    if env_on:
        os.environ["BLOG_READ_LOGIN_ID"] = "1"
    else:
        os.environ.pop("BLOG_READ_LOGIN_ID", None)
    got, why = asyncio.run(browser.read_login_id(FakeCtx(page), lambda *_a: None))
    os.environ.pop("BLOG_READ_LOGIN_ID", None)
    return got, why, page.goto_count


got, why, hits = read(["shin894894"], env_on=False)
check("★기본은 꺼짐 — 페이지를 열지도 않는다",
      got == "" and hits == 0 and "꺼짐" in why, why)
check("환경변수 판정 — 1/on/true/yes 만 켠다",
      browser.read_login_id_enabled({"BLOG_READ_LOGIN_ID": "1"})
      and browser.read_login_id_enabled({"BLOG_READ_LOGIN_ID": "on"})
      and not browser.read_login_id_enabled({"BLOG_READ_LOGIN_ID": "0"})
      and not browser.read_login_id_enabled({"BLOG_READ_LOGIN_ID": ""})
      and not browser.read_login_id_enabled({}))

got, why, hits = read(["shin894894"], env_on=True)
check("켜면 읽는다", got == "shin894894" and hits == 1, f"{got} / {why}")
got, why, _ = read(["shin894894@naver.com"], env_on=True)
check("도메인 포함도 **원문 그대로** 돌려준다(비교는 정규화가 맡는다)",
      got == "shin894894@naver.com", got)
got, why, _ = read(["아이디", "로그인", "naver", "", "  "], env_on=True)
check("화면 단어·빈 값은 ID 로 잡지 않는다", got == "" and "읽지 못했습니다" in why,
      f"{got!r} / {why}")
got, why, _ = read(["아이디 : shin894894"], env_on=True)
check("라벨이 섞여 있어도 ID 만 뽑는다", got == "shin894894", got)
got, why, _ = read(["a"], env_on=True)
check("너무 짧은 값은 거른다", got == "", got)
got, why, hits = read(["shin894894"], boom=True, env_on=True)
check("★모든 출처가 실패해도 예외를 던지지 않는다(빈 값 + 사유)",
      got == "" and "읽지 못했습니다" in why, why)
check("두 출처를 모두 시도했다는 사유가 남는다",
      "내정보" in why and "네이버홈" in why, why)


class DeadCtx:
    pages: list = []

    async def new_page(self):
        raise RuntimeError("ctx closed")


os.environ["BLOG_READ_LOGIN_ID"] = "1"
got, why = asyncio.run(browser.read_login_id(DeadCtx(), lambda *_a: None))
os.environ.pop("BLOG_READ_LOGIN_ID", None)
check("브라우저 컨텍스트가 죽어도 예외를 던지지 않는다",
      got == "" and "페이지를 열지 못했습니다" in why, why)

sess_src = (ROOT / "v2" / "session.py").read_text(encoding="utf-8")
check("session.py 가 읽은 ID 를 record_login_result 로 넘긴다",
      "authenticated_login_id=auth_lid" in sess_src)
check("읽기 실패가 로그인 흐름을 멈추지 않는다(try/except 로 감쌌다)",
      "auth_lid, why_lid = await browser.read_login_id(ctx, log)" in sess_src
      and "네이버 인증 ID 읽기 실패" in sess_src)
check("발행 경로(run.py·run_production.py)는 인증 ID 를 읽지 않는다",
      all("read_login_id" not in (ROOT / "v2" / f).read_text(encoding="utf-8")
          for f in ("run.py", "run_production.py", "writer.py", "edit_post.py")))


# ══════════════════════════════════════════════════════════════════
head("F. 회귀 — 기존 발행 로직·호환성은 그대로인가")

run_src = (ROOT / "v2" / "run.py").read_text(encoding="utf-8")
prod_src = (ROOT / "v2" / "run_production.py").read_text(encoding="utf-8")
check("발행 경로는 여전히 login_state 를 쓰지 않는다",
      "login_state" not in run_src and "login_state" not in prod_src)
check("발행 경로는 loginid 정규화도 쓰지 않는다(건드리지 않았다)",
      "loginid" not in run_src and "loginid" not in prod_src)
check("기존 blog_id 안전장치(_check_account)가 그대로 있다",
      "_check_account" in run_src or "_check_account" in prod_src)

FIELDS = {"flow", "brand", "brand_config", "account", "media", "deficiency",
          "kind", "count", "ref_tab", "account_name", "login_id", "product_url",
          "url", "sheet_media", "sheet_date", "sheet_campaign", "sheet_product",
          "batch", "date", "campaign", "rows", "mode", "content_from", "start",
          "on_error", "extra"}
import dataclasses                                             # noqa: E402

now_fields = {f.name for f in dataclasses.fields(Job)}
check("Job 구조가 그대로다(옛 Agent 1.2.5 호환)",
      FIELDS <= now_fields, f"누락={sorted(FIELDS - now_fields) or '없음'}")
j = Job(brand="repurely", account="a", login_id="x")
check("Job.from_dict 가 모르는 키를 버린다(client_key 무해)",
      Job.from_dict({**j.to_dict(), "client_key": "zzz"}).to_dict()
      == j.to_dict())

reg = LocalStore(SANDBOX / "q_regress")
r1 = reg.submit(Job(brand="repurely", account="acc_r", login_id="loginR",
                    flow="production", count=10), title="t",
                target_agent="PC1")
reg.hold_account("loginR", device="PC1")
check("Agent 는 보류 작업을 집지 못한다(claim)", reg.claim("PC1") is None)
reg.resume_account("loginR", device="PC1")
got_job = reg.claim("PC1")
check("재개하면 집는다", (got_job or {}).get("id") == r1)

key = queue_store.new_client_key()
res = queue_store.submit_checked(
    LocalStore(SANDBOX / "q_idem"),
    Job(brand="repurely", account="acc_i", login_id="loginI"),
    kind="run", title="멱등", target_agent="PC1", client_key=key)
check("멱등성 — 첫 제출은 성공", res.ok and res.client_key == key, repr(res))
store_i = LocalStore(SANDBOX / "q_idem")
res2 = queue_store.submit_checked(
    store_i, Job(brand="repurely", account="acc_i", login_id="loginI"),
    kind="run", title="멱등", target_agent="PC1", client_key=key)
check("★같은 client_key 로 다시 불러도 두 번 넣지 않는다",
      res2.ok and len(store_i.list_jobs(limit=50)) == 1,
      f"{len(store_i.list_jobs(limit=50))}건")
check("find_by_client_key 가 그 작업을 되찾는다",
      (store_i.find_by_client_key(key) or {}).get("client_key") == key)

check("hold/resume/held_jobs/running_for_login 시그니처 유지",
      all(hasattr(LocalStore, m) for m in
          ("hold_account", "resume_account", "held_jobs", "running_for_login",
           "list_by_status", "find_by_client_key")))
check("matches 규칙 유지 — blog_id 로는 절대 고르지 않는다",
      not matches({"job": {"blog_id": "b"}}, "b", "")
      and not matches({"job": {}}, "", ""))


# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 74)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 74)
sys.exit(1 if fails else 0)
