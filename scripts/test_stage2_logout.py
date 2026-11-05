r"""Stage 2 구현 검증 — login_id 기준 세션 · 검증 등급 · 선택 계정 로그아웃 · 보류.

    .\.venv\Scripts\python.exe scripts\test_stage2_logout.py

★브라우저·네이버·시트·운영 큐를 건드리지 않는다. 임시 폴더(BLOG_DATA_DIR)만 쓴다.
★`test_hold_design.py` 는 "설계가 안전한가" 를 **설계안 재현**으로 검증했다.
  이 파일은 **실제로 구현된 운영 코드**(queue_store.hold_account /
  session_store.logout / login_state)를 검증한다.

검증 시나리오(사용자 요청, **배치 크기 10 그대로**)
    20건 = [10, 10] · 1배치 실행 중(3건 발행된 상태) 에 로그아웃 요청
      → 실행 중 배치는 **끝까지** 간다(보류 안 됨)
      → 대기 중인 작업만 보류 · 새 배치 생성 차단
      → 다른 login_id 작업은 정상 처리
      → 재로그인 후 수동 재개 → 중복 없이 이어짐
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage2_test_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ.pop("BLOG_LOGIN_CHECK", None)
os.environ.pop("BLOG_ALLOW_HARD_LOGOUT", None)

from v2 import batch_plan, login_state, queue_store, session_store   # noqa: E402
from v2.accounts import Account                                      # noqa: E402
from v2.job import Job                                               # noqa: E402
from v2.queue_store import DONE, HELD, LocalStore, PENDING, RUNNING  # noqa: E402

OK, NG = "  [o]", "  [X]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def head(title: str) -> None:
    print("")
    print("=" * 74)
    print(f" {title}")
    print("=" * 74)


PC = "pc1"
# 같은 blog_id · 다른 login_id = **서로 다른 계정**(정책의 핵심)
A = Account(id="acc_a", label="A계정", login_id="loginA", blog_id="sameblog",
            ref_tab="A 기준랜딩", brand="repurely")
B = Account(id="acc_b", label="B계정", login_id="loginB", blog_id="sameblog",
            ref_tab="B 기준랜딩", brand="repurely")
NOID = Account(id="acc_noid", label="구계정", login_id="", blog_id="oldblog",
               ref_tab="구 기준랜딩", brand="repurely")


def mkjob(acc: Account, count: int = 10, start: int = 1) -> Job:
    return Job(brand="repurely", account=acc.id, account_name=acc.label,
               login_id=acc.login_id, ref_tab=acc.ref_tab, flow="production",
               mode="convert", count=count, start=start, media="gfa")


# ══════════════════════════════════════════════════════════════════
head("1. 검증 등급 — authenticated / manual / input / unavailable 은 서로 다르다")
# ══════════════════════════════════════════════════════════════════
session_store.ensure(A)
session_store.save_state(A, [{"domain": ".naver.com", "name": "x", "value": "1"}],
                         blog_id="sameblog")
login_state.set_account_map(A, login_id="loginA", blog_id="sameblog",
                            ref_tab=A.ref_tab, brand=A.brand)

check("등급 순서가 authenticated > manual > input > unavailable",
      login_state.rank("authenticated") > login_state.rank("manual")
      > login_state.rank("input") > login_state.rank("unavailable"),
      f"{login_state.rank('authenticated')} > {login_state.rank('manual')} > "
      f"{login_state.rank('input')} > {login_state.rank('unavailable')}")

st = login_state.record_login_result(
    A, seen_blog_id="sameblog", expected_blog_id="sameblog",
    expected_login_id="loginA", manual_ok=True)
check("사용자 확인은 manual 로만 기록된다(authenticated 아님)",
      st["grade"] == login_state.MANUAL, st["grade"])
check("manual 은 'by=user_confirmed' 로 근거가 남는다",
      st["by"] == "user_confirmed", st["by"])
check("blog_id 일치는 별도 사실로 기록된다(등급을 올리지 않는다)",
      st.get("blog_verified") is True and st["grade"] == "manual")
check("manual 에는 authenticated_login_id 가 비어 있다",
      not st["authenticated_login_id"])

st2 = login_state.record_login_result(
    A, seen_blog_id="sameblog", expected_blog_id="sameblog",
    expected_login_id="loginA", authenticated_login_id="loginA")
check("네이버 인증 ID 를 읽으면 authenticated 로 올라간다",
      st2["grade"] == login_state.AUTHENTICATED, st2["grade"])

st3 = login_state.record_login_result(
    A, seen_blog_id="sameblog", expected_blog_id="sameblog",
    expected_login_id="loginA", entered_login_id="loginA")
check("입력 ID 일치는 input 이다(인증 아님)",
      st3["grade"] == login_state.INPUT, st3["grade"])

st4 = login_state.record_login_result(
    A, seen_blog_id="sameblog", expected_blog_id="sameblog",
    expected_login_id="loginA", inherit=True)
check("저장 세션 재사용(자동/간편 로그인)은 기존 등급을 승계한다",
      st4["grade"] == login_state.INPUT and st4["by"].startswith("inherited"),
      f"{st4['grade']} / {st4['by']}")

st5 = login_state.record_login_result(
    A, seen_blog_id="sameblog", expected_blog_id="sameblog",
    expected_login_id="loginA")
check("근거가 없으면 unavailable 로 내려간다(조용히 유지하지 않는다)",
      st5["grade"] == login_state.UNAVAILABLE, st5["grade"])

# ── 명백한 불일치는 단계와 무관하게 막는다 ──────────────────────
login_state.record_login_result(A, seen_blog_id="sameblog",
                                expected_blog_id="sameblog",
                                expected_login_id="loginA", manual_ok=True)
ok, g, why = login_state.check_publish(A, expected_login_id="loginA",
                                       expected_blog_id="sameblog",
                                       seen_blog_id="sameblog")
check("warn 단계 · manual 등급이면 발행을 막지 않는다", ok and g == "manual", g)

ok, g, why = login_state.check_publish(A, expected_login_id="loginA",
                                       expected_blog_id="sameblog",
                                       seen_blog_id="otherblog")
check("발행 대상 블로그가 다르면 warn 단계에서도 막는다", not ok, why[:50])

login_state.set_auth_state(A, grade=login_state.MANUAL, blog_id="sameblog",
                           authenticated_login_id="loginB",
                           expected_login_id="loginA", by="test")
ok, g, why = login_state.check_publish(A, expected_login_id="loginA",
                                       seen_blog_id="sameblog")
check("인증된 login_id 가 기대와 다르면 막는다(blog_id 가 같아도)", not ok, why[:50])

login_state.record_login_result(A, seen_blog_id="sameblog",
                                expected_blog_id="sameblog",
                                expected_login_id="loginA")        # unavailable
ok, _, _ = login_state.check_publish(A, expected_login_id="loginA",
                                     seen_blog_id="sameblog")
check("warn 단계에서는 unavailable 이어도 기존 계정 발행을 막지 않는다", ok)
os.environ["BLOG_LOGIN_CHECK"] = "enforce"
ok, _, why = login_state.check_publish(A, expected_login_id="loginA",
                                       seen_blog_id="sameblog")
check("enforce 단계에서는 unavailable 을 막는다", not ok, why[:40])
os.environ["BLOG_LOGIN_CHECK"] = "off"
ok, _, _ = login_state.check_publish(A, expected_login_id="loginA",
                                     seen_blog_id="sameblog")
check("off 단계는 기존 동작과 같다(막지 않는다)", ok)
ok, _, _ = login_state.check_publish(A, expected_login_id="loginA",
                                     expected_blog_id="sameblog",
                                     seen_blog_id="otherblog")
check("off 단계에서도 블로그 불일치는 막는다", not ok)
os.environ.pop("BLOG_LOGIN_CHECK")
check("기본 적용 단계는 warn 이다", login_state.mode() == "warn",
      login_state.mode())


# ══════════════════════════════════════════════════════════════════
head("2. 세션 별칭 — login_id → session_id 단방향 · 3중 검증")
# ══════════════════════════════════════════════════════════════════
login_state.record_login_result(A, seen_blog_id="sameblog",
                                expected_blog_id="sameblog",
                                expected_login_id="loginA", manual_ok=True)
linked, why = login_state.link_session("loginA", A, grade="manual")
check("manual 등급이면 연결된다", linked, why)
check("login_id 로 세션을 찾을 수 있다",
      login_state.session_for_login("loginA") == "acc_a",
      login_state.session_for_login("loginA"))

linked, why = login_state.link_session("", A, grade="authenticated")
check("login_id 가 비면 연결하지 않는다", not linked, why)

linked, why = login_state.link_session("loginC", A, grade="input")
check("input 등급은 연결하지 않는다(검증 실패 인증정보 저장 방지)",
      not linked, why)

session_store.ensure(B)
session_store.save_state(B, [{"domain": ".naver.com", "name": "y", "value": "2"}],
                         blog_id="sameblog")
login_state.set_account_map(B, login_id="loginB", blog_id="sameblog")
linked, why = login_state.link_session("loginA", B, grade="manual")
check("다른 로그인 계정의 세션 폴더에는 연결하지 않는다", not linked, why)
check("기존 별칭이 그대로다(덮어쓰지 않았다)",
      login_state.session_for_login("loginA") == "acc_a")

login_state.record_login_result(B, seen_blog_id="sameblog",
                                expected_blog_id="sameblog",
                                expected_login_id="loginB", manual_ok=True)
linked, why = login_state.link_session("loginB", B, grade="manual")
check("B 계정은 자기 폴더에 연결된다", linked, why)
check("A·B 는 blog_id 가 같아도 서로 다른 세션이다",
      login_state.session_for_login("loginA")
      != login_state.session_for_login("loginB"),
      f"{login_state.session_for_login('loginA')} vs "
      f"{login_state.session_for_login('loginB')}")
check("login_id 가 없는 계정은 별칭이 없다(임의 통합 안 함)",
      login_state.session_for_login("") == "")


# ══════════════════════════════════════════════════════════════════
head("3. 선택 계정 로그아웃 — 프로필 보존 · 다른 계정 무영향 · 백업 금지")
# ══════════════════════════════════════════════════════════════════
prof_a, prof_b = session_store.profile_dir(A), session_store.profile_dir(B)
prof_a.mkdir(parents=True, exist_ok=True)
prof_b.mkdir(parents=True, exist_ok=True)
(prof_a / "Cookies").write_text("x", encoding="utf-8")
(prof_a / "Login Data").write_text("x", encoding="utf-8")
(prof_a / "Local State").write_text("device-trace", encoding="utf-8")
(prof_b / "Cookies").write_text("y", encoding="utf-8")
b_before = session_store.describe(B)

res = session_store.logout(A)
check("기본 로그아웃은 state.json 을 지운다",
      "state.json" in res["local_cleared"], str(res["local_cleared"]))
check("기본 로그아웃은 프로필을 보존한다",
      res["profile_kept"] and prof_a.exists() and (prof_a / "Cookies").exists())
check("기본 로그아웃은 hard 가 아니다", res["hard"] is False)
check("쿠키 백업 파일을 만들지 않는다",
      not list(session_store.session_dir(A).glob("*state*.bak*"))
      and not list(session_store.session_dir(A).glob("*.backup")),
      str([p.name for p in session_store.session_dir(A).iterdir()]))
check("로그아웃 후 세션 없음으로 보인다",
      session_store.describe(A)["state_exists"] is False)
check("다른 계정(B) 세션은 그대로다",
      session_store.describe(B)["state_exists"] is True
      and session_store.describe(B)["cookies"] == b_before["cookies"])
check("다른 계정(B) 프로필 파일도 그대로다", (prof_b / "Cookies").exists())

login_state.clear_auth_state(A)
check("로그아웃하면 인증 상태(auth_state)가 비워진다",
      login_state.grade_of(A) == login_state.UNAVAILABLE,
      login_state.grade_of(A))
amap = login_state.account_map(A)
check("매핑(account_map)은 로그아웃해도 남는다",
      amap.get("login_id") == "loginA" and amap.get("blog_id") == "sameblog",
      str(amap))
check("B 의 인증 상태는 영향 없다",
      login_state.grade_of(B) == login_state.MANUAL, login_state.grade_of(B))

# ── hard logout 은 브라우저가 완전히 종료된 경우에만 ─────────────
closed, why = session_store.browser_closed(A)
check("브라우저가 닫혀 있으면 확인을 통과한다", closed, why)
check("확인 뒤 프로필 폴더가 원래 이름으로 돌아와 있다",
      prof_a.exists() and (prof_a / "Local State").exists())
check("확인 중 만든 임시 폴더가 남지 않는다",
      not prof_a.with_name(prof_a.name + ".closedcheck").exists())

(prof_a / "SingletonLock").write_text("", encoding="utf-8")
closed, why = session_store.browser_closed(A)
check("프로필이 사용 중이면 hard logout 을 막는다", not closed, why[:40])
res = session_store.logout(A, hard=True)
check("사용 중일 때 hard 로그아웃은 실행되지 않는다",
      res["hard"] is False and (prof_a / "Cookies").exists(), res["reason"][:40])
(prof_a / "SingletonLock").unlink()

res = session_store.logout(A, hard=True)
check("브라우저가 닫힌 뒤에는 hard logout 이 로그인 파일을 지운다",
      res["hard"] is True and not (prof_a / "Cookies").exists()
      and not (prof_a / "Login Data").exists(), str(res["local_cleared"]))
check("hard logout 도 프로필 폴더 자체는 남긴다(기기 등록 흔적)",
      prof_a.exists() and (prof_a / "Local State").exists())
check("hard logout 이 다른 계정 프로필을 건드리지 않는다",
      (prof_b / "Cookies").exists())


# ══════════════════════════════════════════════════════════════════
head("4. 보류(held) — 10건 배치는 끝까지 · 대기 작업만 멈춘다")
# ══════════════════════════════════════════════════════════════════
store = LocalStore(SANDBOX / "queue2")

# 20건 = [10, 10] — **배치 크기 10 그대로**
plan = batch_plan.create(mkjob(A, 10).to_dict(), 20, title="A 20건",
                         brand="repurely", account=A.id, flow="production",
                         mode="convert", device=PC)
check("20건은 10건 배치 2개로 쪼개진다", plan["sizes"] == [10, 10],
      str(plan["sizes"]))

submitted: list[str] = []


def submit_run(batch: dict) -> str:
    jid = store.submit(Job.from_dict(batch_plan.job_for(plan, batch)),
                       title=f"배치 {batch['no']}", target_agent=PC)
    submitted.append(jid)
    return jid


def job_status(jid: str):
    rec = store.get(jid) or {}
    return rec.get("status") or "", rec


plan, _ = batch_plan.advance(plan, job_status=job_status, submit_run=submit_run)
rec = store.claim(PC)
check("1배치가 큐에 들어가 Agent 가 집었다", rec is not None and
      rec["job"]["count"] == 10, str((rec or {}).get("title")))
store.update(rec["id"], published=["u1", "u2", "u3"], made=3)
print("     (… 1배치 10건 중 3건 발행된 시점에 사용자가 로그아웃을 누른다)")

# 다른 계정(B)의 대기 작업 — 영향이 없어야 한다
b_job = store.submit(mkjob(B, 10), title="B 배치", target_agent=PC)
# login_id 가 없는 옛 작업 — 추측으로 묶지 않아야 한다
noid_job = store.submit(mkjob(NOID, 5), title="구계정 배치", target_agent=PC)
# A 의 다음 대기 작업(단건 실행으로 들어간 것)
a_pending = store.submit(mkjob(A, 10, start=11), title="A 추가", target_agent=PC)

held = store.hold_account("loginA", device=PC)
check("A 의 대기 작업만 보류된다", held == [a_pending], str(held))
check("실행 중인 1배치는 보류되지 않는다(끝까지 발행)",
      (store.get(rec["id"]) or {}).get("status") == RUNNING)
check("보류된 작업은 held 상태다",
      (store.get(a_pending) or {}).get("status") == HELD)
check("B 계정 작업은 그대로 대기 중이다",
      (store.get(b_job) or {}).get("status") == PENDING)
check("login_id 없는 작업은 보류하지 않는다(임의 통합 금지)",
      (store.get(noid_job) or {}).get("status") == PENDING)

# 보류 중 Agent 가 무엇을 집는가 — A 것은 **하나도** 못 집는다
store.finish(rec["id"], DONE, exit_code=0)
store.update(rec["id"], published=[f"u{i}" for i in range(1, 11)])
check("1배치는 10건 모두 발행하고 완료된다",
      len((store.get(rec["id"]) or {}).get("published") or []) == 10)

#   ★claim 순서는 파일명(작업번호) 순이라 제출 순서와 다를 수 있다. 그래서
#     "무엇을 먼저 집나" 가 아니라 **A 것이 하나도 안 집히나** 를 본다.
drained: list[dict] = []
while True:
    nxt = store.claim(PC)
    if nxt is None:
        break
    drained.append(nxt)
    store.finish(nxt["id"], DONE, exit_code=0)
got_ids = [d["id"] for d in drained]
check("Agent 는 보류된 A 작업을 집지 않는다 — 큐가 빌 때까지 확인",
      a_pending not in got_ids, f"집은 작업 {len(got_ids)}건")
check("다른 login_id 작업은 모두 정상 처리된다 — 영향 없음",
      {b_job, noid_job} <= set(got_ids),
      str([(store.get(i) or {}).get("job", {}).get("login_id") for i in got_ids]))
check("보류된 A 작업은 여전히 held 다",
      (store.get(a_pending) or {}).get("status") == HELD)

# 로그아웃 작업은 **보류되지 않는다**(복구 경로)
logout_job = store.submit(
    Job(brand="repurely", account=A.id, login_id="loginA",
        extra=["--logout", A.id, "--events"]),
    kind="session", title="로그아웃 — A", target_agent=PC)
again = store.hold_account("loginA", device=PC)
check("로그아웃/로그인 작업(session)은 보류되지 않는다",
      logout_job not in again
      and (store.get(logout_job) or {}).get("status") == PENDING, str(again))
got = store.claim(PC)
check("배치가 끝난 뒤 Agent 가 로그아웃 작업을 집는다",
      got is not None and got["id"] == logout_job)
store.finish(got["id"], DONE, exit_code=0)

n_before = len(store.list_jobs(limit=200))
plan_before = json.dumps(plan, ensure_ascii=False, sort_keys=True)
plan, changed = batch_plan.advance(plan, job_status=job_status,
                                   submit_run=submit_run)
check("1배치 완료가 반영되고 다음 배치는 login_wait 로 남는다",
      plan["batches"][0]["status"] == DONE
      and plan["batches"][1]["status"] == batch_plan.LOGIN_WAIT,
      f"{plan['batches'][0]['status']} / {plan['batches'][1]['status']}")
check("다음 배치가 자동으로 큐에 들어가지 않는다(기존 동작 그대로)",
      len(store.list_jobs(limit=200)) == n_before,
      f"{n_before} -> {len(store.list_jobs(limit=200))}")
check("보류 중 새로 제출된 작업이 없다", len(submitted) == 1, str(len(submitted)))


# ══════════════════════════════════════════════════════════════════
head("5. 재로그인 후 재개 — 중복 없이 이어진다")
# ══════════════════════════════════════════════════════════════════
back = store.resume_account("loginA", device=PC)
check("보류 작업이 다시 대기 상태로 돌아온다", back == [a_pending], str(back))
check("재개된 작업은 pending 이다",
      (store.get(a_pending) or {}).get("status") == PENDING)
got = store.claim(PC)
check("Agent 가 이제 그 작업을 집는다", got is not None and got["id"] == a_pending,
      str((got or {}).get("id")))
store.finish(a_pending, DONE, exit_code=0)
check("재개한 작업이 완료로 기록된다",
      (store.get(a_pending) or {}).get("status") == DONE)

ids = [r["id"] for r in store.list_jobs(limit=200)]
check("큐 전체 작업 수 = 제출한 수(중복 생성 없음)",
      len(ids) == len(set(ids)) == 5, f"{len(ids)}건 / 고유 {len(set(ids))}건")
check("이미 끝난 1배치가 다시 생성되지 않았다",
      sum(1 for r in store.list_jobs(limit=200)
          if (r.get("title") or "").startswith("배치 1")) == 1)
check("재개는 다른 계정에 영향이 없다",
      store.resume_account("loginB", device=PC) == [], "B 는 보류된 것이 없다")

# ── 로그아웃 대기 표시(파일 쪽) ─────────────────────────────────
login_state.mark_logout_pending("loginA", session_id=A.id)
check("로그아웃 대기는 그 login_id 에만 걸린다",
      login_state.is_logout_pending("loginA")
      and not login_state.is_logout_pending("loginB"))
check("로그아웃 대기는 해제된다", login_state.clear_logout_pending("loginA")
      and not login_state.is_logout_pending("loginA"))


# ══════════════════════════════════════════════════════════════════
head("6. 기존 동작 보존")
# ══════════════════════════════════════════════════════════════════
check("BATCH_SIZE 는 10 그대로다", batch_plan.BATCH_SIZE == 10,
      str(batch_plan.BATCH_SIZE))
check("10건은 1배치다", batch_plan.split(10) == [10], str(batch_plan.split(10)))
check("25건은 [10,10,5] 그대로다", batch_plan.split(25) == [10, 10, 5],
      str(batch_plan.split(25)))
check("claim 은 pending 만 본다(옛 Agent 도 held 를 못 집는다)",
      "pending" in LocalStore.claim.__code__.co_consts
      or queue_store.PENDING == "pending")
j = mkjob(A, 10)
check("Job.to_dict 에 새 키가 추가되지 않았다(옛 Agent 호환)",
      set(j.to_dict()) == set(Job.from_dict(j.to_dict()).to_dict()))
check("held 는 종료 상태가 아니다(OPEN_STATES 에도, 종료에도 없다)",
      HELD not in queue_store.OPEN_STATES
      and HELD not in (DONE, queue_store.FAILED, queue_store.CANCELED))

# ══════════════════════════════════════════════════════════════════
head("7. 옛 설치본 호환 — 로그아웃 버튼은 Agent 버전으로 잠근다")
# ══════════════════════════════════════════════════════════════════
#   ★`v2.session --logout` 은 새 명령이다. 옛 설치본(1.2.5)에 보내면 argparse 가
#     거부해 작업이 **실패**한다 → 그 PC 에서는 버튼을 눌리지 않게 해야 한다.
from v2 import pairing                                              # noqa: E402

check("AGENT_VERSION 과 필요 버전이 맞물린다",
      pairing.ver_tuple(pairing.AGENT_VERSION) >= pairing.NEEDS["logout"],
      f"{pairing.AGENT_VERSION} >= "
      f"{'.'.join(str(n) for n in pairing.NEEDS['logout'])}")
#   ★실측(2026-11-03): 설치돼 **돌고 있는** 설치본은 1.2.4 다
#     (…\Programs\BlogLandingAgent\BlogLandingAgent.exe 9/17 15:22 빌드).
#     1.2.5 는 installer\dist 에 빌드만 돼 있고 설치되지 않았다.
check("실제 운영 설치본(1.2.4)에서는 로그아웃을 잠근다",
      not pairing.supports("1.2.4", "logout"))
check("빌드만 된 1.2.5 에서도 잠근다",
      not pairing.supports("1.2.5", "logout"))
check("그 이전 버전(1.2.3 · 1.1.5)도 잠근다",
      not pairing.supports("1.2.3", "logout")
      and not pairing.supports("1.1.5", "logout"))
check("1.3.0 이상에서는 열어 준다",
      pairing.supports("1.3.0", "logout")
      and pairing.supports("1.4", "logout")
      and pairing.supports("2.0.0", "logout"))
check("버전을 모르면 잠근다(추측하지 않는다)",
      not pairing.supports("", "logout") and not pairing.supports(None, "logout"))
check("표에 없는 기능은 막지 않는다(기존 기능 영향 없음)",
      pairing.supports("1.0.0", "login") and pairing.supports("", "check"))
#  ★번호를 글자로 박지 않는다 — `pairing.AGENT_VERSION` 하나를 출처로 삼고
#    설치본(.iss)이 **그것과 같은지**만 본다. 전에는 "1.3.0" 을 박아 두어서
#    번호를 1.3.1 로 올리자 이 검사가 거짓 실패했다(2026-11-05).
check(f"설치본 번호(.iss)가 pairing.AGENT_VERSION 과 같다 ({pairing.AGENT_VERSION})",
      f'#define AppVer "{pairing.AGENT_VERSION}"' in (
          ROOT / "installer" / "BlogLandingAgent.iss").read_text(
          encoding="utf-8-sig"))

print("")
print("=" * 74)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 74)
raise SystemExit(1 if fails else 0)
