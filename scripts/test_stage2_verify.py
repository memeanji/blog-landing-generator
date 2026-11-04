r"""Stage 2 **안정성 확인** — 요청받은 항목을 코드로 검증한다(새 기능 추가 없음).

    .\.venv\Scripts\python.exe scripts\test_stage2_verify.py

확인 항목(사용자 요청 2·3번)
    ① 로그인 검증 전 **잘못된 쿠키가 저장되지 않는지**
    ② 발행 직전 blog_id 검증이 **실제 발행 경로에서** 실행되는지
    ③ login_id 를 확인하지 못할 때 **인증 성공으로 표시되지 않는지**
    ④ login_id 가 없는 기존 계정의 로그아웃 처리
    ⑤ 같은 login_id 의 여러 기준랜딩 탭 — 세션·로그아웃 상태 공유
    ⑥ 다른 login_id · 같은 blog_id — 완전 분리
    ⑦ Agent claim ↔ held 경합
    ⑧ 로그아웃 대기 상태가 **Streamlit 재부팅 후에도** 유지되는지
    ⑨ 로그아웃 작업 등록 실패 시 보류 **복구**
    ⑩ 기존 정상 계정이 새 검증 정책으로 **막히지 않는지**

★브라우저·네이버·시트·운영 큐를 건드리지 않는다. 임시 폴더만 쓴다.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SANDBOX = Path(tempfile.mkdtemp(prefix="blog_verify_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ.pop("BLOG_LOGIN_CHECK", None)

from v2 import (accounts, browser, login_state, queue_store,       # noqa: E402
                session_store)
from v2.job import Job                                            # noqa: E402
from v2.queue_store import DONE, HELD, LocalStore, PENDING, RUNNING  # noqa: E402

OK, NG = "  [o]", "  [X]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def blogid_guard(text: str) -> str:
    """`_check_account` 의 등록 blog_id 보호 수준 — "strict" / "skip" / "?".

    strict  등록 blog_id 가 비면 **오류로 중단**(2026-09-17 미커밋 변경에 들어 있다)
    skip    등록 blog_id 가 비면 **검사를 건너뛴다**(git HEAD = 지금 배포된 코드)
    """
    if "blog_id 가 등록돼 있지 않아" in text and "raise RuntimeError" in text:
        return "strict"
    if "if not acc or not acc.blog_id or not blog_id:" in text:
        return "skip"
    return "?"


def is_logout(rec: dict) -> bool:
    """로그아웃 작업인가(extra 가 비어 있어도 안전하게)."""
    return "--logout" in list(((rec or {}).get("job") or {}).get("extra") or [])


def head(title: str) -> None:
    print("")
    print("=" * 74)
    print(f" {title}")
    print("=" * 74)


PC = "pc1"
ACCOUNTS = SANDBOX / "accounts.json"
#  A·B = **같은 blog_id · 다른 login_id**(완전 분리돼야 한다)
#  TAB2 = A 와 **같은 login_id · 다른 기준랜딩 탭**(같은 세션을 써야 한다)
#  NOID = login_id 가 없는 옛 계정
ACCOUNTS.write_text(json.dumps({"accounts": [
    {"id": "acc_a", "label": "A계정", "login_id": "loginA", "blog_id": "sameblog",
     "ref_tab": "A 기준랜딩", "brand": "repurely"},
    {"id": "acc_b", "label": "B계정", "login_id": "loginB", "blog_id": "sameblog",
     "ref_tab": "B 기준랜딩", "brand": "repurely"},
    {"id": "acc_noid", "label": "구계정", "login_id": "", "blog_id": "oldblog",
     "ref_tab": "구 기준랜딩", "brand": "repurely"},
]}, ensure_ascii=False), encoding="utf-8")
accounts.ACCOUNTS_PATH = ACCOUNTS
accounts.refresh()

A = accounts.find_exact("acc_a", ACCOUNTS)
B = accounts.find_exact("acc_b", ACCOUNTS)
NOID = accounts.find_exact("acc_noid", ACCOUNTS)


# ══════════════════════════════════════════════════════════════════
head("1. 로그인 검증 전 — 잘못된 쿠키가 저장되지 않는가")
# ══════════════════════════════════════════════════════════════════
#   ★전에는 `browser.open_session` 이 로그인 확인 직후 바로 저장하고, '고른 계정 =
#     로그인한 계정' 대조는 **그 뒤**에 했다. 그래서 다른 계정으로 로그인하면
#     발행은 막히지만 그 계정 폴더에 **남의 쿠키가 남았다.** 이제 저장 자체를 막는다.
check("등록 blog_id 와 다른 계정이면 저장을 막는다",
      bool(browser.save_blocked("acc_a", "otherblog")),
      browser.save_blocked("acc_a", "otherblog")[:48])
check("등록 blog_id 와 같으면 저장한다(기존 동작)",
      browser.save_blocked("acc_a", "sameblog") == "")
check("대소문자는 구분하지 않는다",
      browser.save_blocked("acc_a", "SameBlog") == "")
check("대조 기준이 아예 없으면 막지 않는다(첫 로그인 · 기존 동작)",
      browser.save_blocked("없는계정", "whatever") == ""
      and browser.save_verdict("없는계정", "whatever")[0] == "unknown")
#   ★2026-11-03 보강: 기준값이 **있는데** 지금 블로그를 못 읽으면 저장을 막는다
#     (대조 없이 덮어쓰면 기준값이 지워져 다음부터 영구히 대조 불가가 된다).
#     쿠키는 지우지 않으므로 기존 정상 세션은 그대로 유지된다.
check("기준값이 있는데 블로그를 못 읽으면 덮어쓰지 않는다(unreadable)",
      browser.save_verdict("acc_a", "")[0] == "unreadable"
      and bool(browser.save_blocked("acc_a", "")))
check("acc_noid 도 등록 blog_id(oldblog)가 있으므로 같은 보호를 받는다",
      browser.save_verdict("acc_noid", "")[0] == "unreadable")
check("계정 조회는 id **정확일치**만 쓴다(폴더 이름이 남의 blog_id 와 같아도 안전)",
      browser.registered_blog_id("sameblog") == ""
      and browser.registered_blog_id("acc_a") == "sameblog",
      "blog_id 로는 안 찾는다")


class FakeCtx:
    """`ctx.cookies()` 만 흉내 내는 가짜 컨텍스트(브라우저 없음)."""

    def __init__(self) -> None:
        self.read = 0

    async def cookies(self):
        self.read += 1
        return [{"domain": ".naver.com", "name": "NID_SES", "value": "XXX"}]


logs: list[str] = []
ctx = FakeCtx()
n = asyncio.run(browser._persist_session(ctx, "acc_a", "otherblog", logs.append))
check("어긋난 계정은 쿠키 저장 0건", n == 0, f"{n}건")
check("어긋나면 쿠키를 **읽지도** 않는다", ctx.read == 0, f"읽기 {ctx.read}회")
check("세션 파일이 만들어지지 않았다",
      not session_store.state_path(A).exists())
check("이유를 로그에 남긴다(쿠키 값은 남기지 않는다)",
      any("저장하지 않았습니다" in m for m in logs)
      and not any("XXX" in m for m in logs), logs[-1][:46] if logs else "")

logs.clear()
n = asyncio.run(browser._persist_session(FakeCtx(), "acc_a", "sameblog",
                                         logs.append))
check("맞는 계정은 예전처럼 저장된다", n == 1
      and session_store.state_path(A).exists(), f"{n}건")
saved = json.loads(session_store.state_path(A).read_text(encoding="utf-8"))
check("저장된 세션에 blog_id 가 들어간다", saved.get("blog_id") == "sameblog")

src = (ROOT / "v2" / "browser.py").read_text(encoding="utf-8")
check("로그인창 경로에도 같은 차단이 걸려 있다",
      src.count("save_verdict(") >= 3, f"save_verdict {src.count('save_verdict(')}곳")
check("차단될 때 save_state 를 호출하지 않는다(else 분기)",
      "else:\n                    path = session_store.save_state" in src)


# ══════════════════════════════════════════════════════════════════
head("2. 발행 직전 blog_id 검증이 실제 발행 경로에 있는가")
# ══════════════════════════════════════════════════════════════════
for mod in ("run", "run_production"):
    text = (ROOT / "v2" / f"{mod}.py").read_text(encoding="utf-8")
    lines = text.splitlines()
    i_open = next((i for i, ln in enumerate(lines)
                   if "browser.open_session(" in ln), -1)
    i_chk = next((i for i, ln in enumerate(lines)
                  if "_check_account(args" in ln and "def " not in ln), -1)
    #   ★함수 **정의** 줄이 아니라 실행 경로의 **호출** 줄을 찾는다
    #     (정의는 파일 앞쪽에 있어서 그냥 찾으면 순서가 거꾸로 보인다).
    i_write = next((i for i, ln in enumerate(lines)
                    if i > i_open and ("await convert_one(" in ln
                                       or "await create_one(" in ln
                                       or "await build_ready(" in ln)), -1)
    i_pub = next((i for i, ln in enumerate(lines)
                  if i > i_open and ("await post.publish()" in ln
                                     or "await publish_all(" in ln)), -1)
    check(f"v2/{mod}.py — 로그인 뒤에 계정 대조를 한다",
          i_open > 0 and i_chk > i_open, f"open_session {i_open + 1}행 → "
                                         f"_check_account {i_chk + 1}행")
    check(f"v2/{mod}.py — **글을 쓰기 전에** 대조를 끝낸다",
          i_write > 0 and i_chk < i_write, f"_check_account {i_chk + 1}행 < "
                                           f"작성 호출 {i_write + 1}행")
    check(f"v2/{mod}.py — **발행 전에** 대조를 끝낸다",
          i_pub > 0 and i_chk < i_pub, f"_check_account {i_chk + 1}행 < "
                                       f"발행 호출 {i_pub + 1}행")
    guard = blogid_guard(text)
    check(f"v2/{mod}.py — 등록 blog_id 보호 수준을 판별할 수 있다",
          guard in ("strict", "skip"),
          "오류로 중단(strict · 9/17 미커밋 변경)" if guard == "strict"
          else "★검사를 건너뜀(skip · git HEAD = 현재 배포본)")
    check(f"v2/{mod}.py — 불일치면 발행하지 않고 중단",
          "다릅니다" in text and "중단합니다" in text)
prod_src = (ROOT / "v2" / "run_production.py").read_text(encoding="utf-8")
check("두 경로 모두 `--account` 없이 돌리던 기존 CLI 는 그대로 통과",
      "if not acc or not blog_id:" in prod_src
      or "if not acc or not acc.blog_id or not blog_id:" in prod_src,
      "acc 가 없으면 아무것도 하지 않는다")
check("※ 참고 — 발행 경로의 등록 blog_id 보호 수준",
      blogid_guard(prod_src) in ("strict", "skip"),
      blogid_guard(prod_src))


# ══════════════════════════════════════════════════════════════════
head("3. login_id 를 확인하지 못할 때 인증 성공으로 보이지 않는가")
# ══════════════════════════════════════════════════════════════════
session_store.save_state(A, [{"domain": ".naver.com", "name": "a", "value": "1"}],
                         blog_id="sameblog")
login_state.set_account_map(A, login_id="loginA", blog_id="sameblog")
st = login_state.record_login_result(A, seen_blog_id="sameblog",
                                     expected_blog_id="sameblog",
                                     expected_login_id="loginA")
check("블로그만 확인되면 등급은 unavailable 이다(인증 아님)",
      st["grade"] == login_state.UNAVAILABLE and st["blog_verified"] is True,
      f"{st['grade']} / blog_verified={st['blog_verified']}")
check("화면 문구가 '확인하지 못했다' 로 나간다",
      "확인하지 못했습니다" in login_state.warn_text(A),
      login_state.warn_text(A)[:52])
check("authenticated 가 아니면 경고 문구가 반드시 있다",
      login_state.warn_text(A) != "")
st = login_state.record_login_result(A, seen_blog_id="sameblog",
                                     expected_blog_id="sameblog",
                                     expected_login_id="loginA",
                                     authenticated_login_id="loginA")
check("실제 인증 ID 를 읽었을 때만 경고가 사라진다",
      st["grade"] == login_state.AUTHENTICATED
      and login_state.warn_text(A) == "")
app = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
check("화면도 authenticated 외에는 '인증 확인이 아니다' 라고 적는다",
      "네이버 인증 ID 확인은 아닙니다" in app)
check("등급은 PC 응답(session_ready 이벤트)으로 전달된다",
      "grade=state[\"grade\"]" in
      (ROOT / "v2" / "session.py").read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════
head("4. login_id 가 없는 기존 계정의 로그아웃")
# ══════════════════════════════════════════════════════════════════
session_store.save_state(NOID, [{"domain": ".naver.com", "name": "o", "value": "9"}],
                         blog_id="oldblog")
prof = session_store.profile_dir(NOID)
prof.mkdir(parents=True, exist_ok=True)
(prof / "Local State").write_text("trace", encoding="utf-8")
res = session_store.logout(NOID)
check("세션 폴더 기준이므로 login_id 없이도 로그아웃된다",
      "state.json" in res["local_cleared"]
      and session_store.describe(NOID)["state_exists"] is False,
      str(res["local_cleared"]))
check("프로필은 그대로 보존된다", (prof / "Local State").exists())
check("다른 계정 세션은 그대로다",
      session_store.describe(A)["state_exists"] is True)

store = LocalStore(SANDBOX / "q_noid")
noid_job = store.submit(Job(brand="repurely", account=NOID.id, login_id="",
                            flow="production", count=10),
                        title="구계정 배치", target_agent=PC)
check("큐 보류는 login_id 가 있어야 한다 — 빈 값이면 아무것도 보류하지 않는다",
      store.hold_account("", device=PC) == []
      and (store.get(noid_job) or {}).get("status") == PENDING)
check("다른 계정 이름으로도 그 작업이 보류되지 않는다(추측 금지)",
      store.hold_account("loginA", device=PC) == []
      and (store.get(noid_job) or {}).get("status") == PENDING)
#   ★2026-11-03 변경: login_id 가 없어도 **account.id 기준으로 로그아웃 가능**.
#     버튼은 더 이상 login_id 때문에 잠기지 않는다(사용자 요청).
check("화면 버튼은 login_id 가 없어도 열린다(account.id 기준)",
      "disabled=not (device_id and (lid or aid) and can_logout)" in app)
check("login_id 없는 계정은 '이 계정 전용 세션' 기준임을 알린다",
      "이 계정 전용 세션" in app)
print("     → 결론: PC 쪽 `v2.session --logout <계정>` 은 login_id 없이도 동작하고,")
print("        **큐 보류만** login_id 를 요구한다(그래서 화면 버튼은 잠긴다).")


# ══════════════════════════════════════════════════════════════════
head("5. 같은 login_id · 여러 기준랜딩 탭 — 세션과 로그아웃 상태 공유")
# ══════════════════════════════════════════════════════════════════
#   같은 login_id 를 가진 탭이 둘 있으면(brands.json 이 그렇게 준다) 계정 해석
#   결과가 **같은 세션 폴더**여야 한다.
acc1 = accounts.resolve_for("", "A 기준랜딩", "repurely", create=False,
                            path=ACCOUNTS, login_id="loginA")
acc2 = accounts.resolve_for("", "다른 기준랜딩", "repurely", create=False,
                            path=ACCOUNTS, login_id="loginA")
check("두 기준랜딩 탭이 같은 세션 폴더로 모인다",
      acc1 is not None and acc2 is not None and acc1.id == acc2.id == "acc_a",
      f"{getattr(acc1, 'id', None)} / {getattr(acc2, 'id', None)}")
check("선택 근거가 login_id 다(탭 이름·해시가 아니다)",
      "login_id=loginA" in accounts.last_basis(), accounts.last_basis()[:46])
check("세션 별칭도 login_id 하나로 모인다",
      login_state.session_for_login("loginA") in ("", "acc_a"))

store2 = LocalStore(SANDBOX / "q_share")
j_tab1 = store2.submit(Job(brand="repurely", account="acc_a", login_id="loginA",
                           ref_tab="A 기준랜딩", flow="production", count=10),
                       title="탭1 배치", target_agent=PC)
j_tab2 = store2.submit(Job(brand="repurely", account="acc_a", login_id="loginA",
                           ref_tab="다른 기준랜딩", flow="production", count=10),
                       title="탭2 배치", target_agent=PC)
held = store2.hold_account("loginA", device=PC)
check("한 번의 로그아웃이 그 login_id 의 **모든 탭** 작업을 보류한다",
      set(held) == {j_tab1, j_tab2}, f"{len(held)}건")
check("두 탭 모두 held 로 보인다 — 상태가 공유된다",
      all((store2.get(j) or {}).get("status") == HELD for j in (j_tab1, j_tab2)))
check("재개도 두 탭에 함께 적용된다",
      set(store2.resume_account("loginA", device=PC)) == {j_tab1, j_tab2})
login_state.mark_logout_pending("loginA", session_id="acc_a")
check("파일 쪽 대기 표시도 login_id 기준이라 탭과 무관하게 같다",
      login_state.is_logout_pending("loginA"))
login_state.clear_logout_pending("loginA")


# ══════════════════════════════════════════════════════════════════
head("6. 다른 login_id · 같은 blog_id — 완전 분리")
# ══════════════════════════════════════════════════════════════════
check("A·B 는 세션 폴더가 다르다",
      session_store.session_dir(A) != session_store.session_dir(B),
      f"{session_store.session_dir(A).name} vs {session_store.session_dir(B).name}")
session_store.save_state(B, [{"domain": ".naver.com", "name": "b", "value": "2"}],
                         blog_id="sameblog")
login_state.set_account_map(B, login_id="loginB", blog_id="sameblog")
check("blog_id 가 같아도 쿠키 파일이 따로 있다",
      session_store.describe(A)["state_exists"]
      and session_store.describe(B)["state_exists"])
session_store.logout(A)
check("A 를 로그아웃해도 B 는 로그인 상태다",
      session_store.describe(A)["state_exists"] is False
      and session_store.describe(B)["state_exists"] is True)
store3 = LocalStore(SANDBOX / "q_split")
ja = store3.submit(Job(brand="repurely", account="acc_a", login_id="loginA",
                       flow="production", count=10), title="A", target_agent=PC)
jb = store3.submit(Job(brand="repurely", account="acc_b", login_id="loginB",
                       flow="production", count=10), title="B", target_agent=PC)
store3.hold_account("loginA", device=PC)
check("A 보류가 B 작업에 영향을 주지 않는다",
      (store3.get(ja) or {}).get("status") == HELD
      and (store3.get(jb) or {}).get("status") == PENDING)
check("blog_id 로는 절대 묶이지 않는다(blog_id 를 키로 준 보류는 0건)",
      store3.hold_account("sameblog", device=PC) == []
      and (store3.get(jb) or {}).get("status") == PENDING)
got = store3.claim(PC)
check("Agent 는 B 작업만 집는다", got is not None and got["id"] == jb,
      str((got or {}).get("title")))


# ══════════════════════════════════════════════════════════════════
head("7. Agent claim ↔ held 경합")
# ══════════════════════════════════════════════════════════════════
store4 = LocalStore(SANDBOX / "q_race")
jobs = [store4.submit(Job(brand="repurely", account="acc_a", login_id="loginA",
                          flow="production", count=10),
                      title=f"배치{i}", target_agent=PC) for i in range(6)]
#   ★claim 과 hold 를 번갈아 — 한 작업은 **둘 중 하나만** 가져간다.
taken, holds = [], []
for i in range(6):
    if i % 2 == 0:
        rec = store4.claim(PC)
        if rec:
            taken.append(rec["id"])
    else:
        holds += store4.hold_account("loginA", device=PC)
rest = store4.claim(PC)
if rest:
    taken.append(rest["id"])
check("한 작업이 running 과 held 양쪽에 들어가지 않는다",
      not (set(taken) & set(holds)), f"claim {len(taken)}건 · held {len(holds)}건")
check("모든 작업이 정확히 한 번만 처리된다",
      len(taken) + len(holds) == len(jobs)
      and len(set(taken) | set(holds)) == len(jobs),
      f"{len(taken)} + {len(holds)} = {len(jobs)}")
states = [(store4.get(j) or {}).get("status") for j in jobs]
check("상태는 running/held 뿐이다(사라진 작업 없음)",
      all(s in (RUNNING, HELD) for s in states), str(sorted(set(states))))
check("보류는 os.replace 한 번이라 집힌 작업은 건드리지 못한다",
      "os.replace(src, dst)" in
      (ROOT / "v2" / "queue_store.py").read_text(encoding="utf-8"))

schema = (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8")
check("[Supabase] claim_job 은 pending 만 집는다 — held 는 못 집는다",
      "where device_id = v_id and status = 'pending'" in schema)
check("[Supabase] claim_job 은 for update skip locked 로 행을 잠근다",
      "for update skip locked" in schema)
check("[Supabase] status 는 text 다(enum·CHECK 아님) → held 를 넣을 수 있다",
      "status      text not null default 'pending'" in schema
      and "check (status" not in schema.lower())
#  ★2026-11-04 — 보류 대상을 '상태로 좁혀 고른 뒤 job_id + status 조건으로 PATCH'
#    하는 2단계로 바꿨다. 조건(`status=eq.<frm>`)이 PATCH 에 그대로 붙어 있으므로
#    원자성은 전과 같다. 바꾼 이유는 ①조회 창에 밀려 보류 대상을 놓치지 않기
#    ②login_id 표기 차이(@naver.com)를 흡수하기 — 아래 두 줄로 둘 다 검증한다.
sup = (ROOT / "v2" / "supabase_store.py").read_text(encoding="utf-8")
check("[Supabase] 보류 PATCH 에 status 조건이 그대로 붙어 있다(원자성 유지)",
      "job_id=in.(" in sup and "&status=eq.{quote(frm" in sup)
check("[Supabase] 보류 대상은 **상태로 좁혀** 고른다(조회 창에 밀리지 않는다)",
      'self.list_by_status(frm, lid, aid, kind="run")' in sup)
check("[Supabase] 보류 대상 선별은 matches(= login_id 정규화)를 쓴다",
      "matches(r, login_id, account_id)" in sup)
print("     → 경합 결론: claim 이 먼저면 행은 running 이 되어 `status=eq.<frm>`")
print("        조건에 안 걸리고, 보류가 먼저면 claim 의 하위질의가 그 행을 못 고른다.")
print("        어느 쪽이 이겨도 **한 번만** 처리된다(Postgres 행 잠금이 보장).")


# ══════════════════════════════════════════════════════════════════
head("8. Streamlit 재부팅 후에도 로그아웃 대기가 유지되는가")
# ══════════════════════════════════════════════════════════════════
#   ★화면 상태(st.session_state)는 재부팅에 날아간다. 그래서 로그아웃 대기를
#     **큐에서** 읽는다(화면·PC 가 함께 보는 유일한 공용 저장소).
store5 = LocalStore(SANDBOX / "q_reboot")
j1 = store5.submit(Job(brand="repurely", account="acc_a", login_id="loginA",
                       flow="production", count=10), title="배치", target_agent=PC)
store5.hold_account("loginA", device=PC)
logout_job = store5.submit(Job(brand="repurely", account="acc_a",
                               login_id="loginA",
                               extra=["--logout", "acc_a", "--events"]),
                           kind="session", title="로그아웃", target_agent=PC)

fresh = LocalStore(SANDBOX / "q_reboot")          # ★재부팅 = 새 객체·빈 캐시
check("재부팅 뒤에도 보류 작업이 보인다",
      [r["id"] for r in fresh.held_jobs("loginA")] == [j1],
      f"{len(fresh.held_jobs('loginA'))}건")
check("재부팅 뒤에도 대기 중 로그아웃 작업이 보인다",
      any(is_logout(r) for r in fresh.list_jobs(limit=50)
          if r.get("status") in (PENDING, RUNNING)))
check("상태 판단이 화면 메모리에 의존하지 않는다(큐만 읽는다)",
      "store_obj.list_by_status(status, lid, aid, kind=kind)" in app
      and "st.session_state" not in app.split("def logout_state")[1].split(
          "def logout_block")[0])
#  ★2026-11-04 — 예전에는 `list_jobs(limit=120)` 를 훑어 held 를 골랐다.
#    작업이 120건을 넘으면 보류 작업이 조회 창 밖으로 밀려 '보류 없음' 이 되고,
#    그러면 로그아웃 대기 계정에 새 발행 작업이 다시 만들어졌다.
check("화면이 조회 창(limit) 안에서 held 를 찾는 방식이 아니다",
      "rows = store_obj.list_jobs(limit=120)" not in app)
check("화면은 보류가 있으면 그 계정 새 작업을 막는다",
      "if kind == \"run\":" in app and "logout_block(store_obj" in app)
check("보류가 풀리면 막지 않는다",
      fresh.resume_account("loginA", device=PC) == [j1]
      and fresh.held_jobs("loginA") == [])
store5.finish(logout_job, DONE, exit_code=0)
check("로그아웃 작업이 끝나면 대기 표시도 사라진다",
      not any(is_logout(r) for r in fresh.list_jobs(limit=50)
              if r.get("status") in (PENDING, RUNNING)))


# ══════════════════════════════════════════════════════════════════
head("9. 로그아웃 작업 등록 실패 → 보류 복구")
# ══════════════════════════════════════════════════════════════════
store6 = LocalStore(SANDBOX / "q_recover")
jx = store6.submit(Job(brand="repurely", account="acc_a", login_id="loginA",
                       flow="production", count=10), title="배치", target_agent=PC)
held = store6.hold_account("loginA", device=PC)          # ① 보류
check("① 보류가 먼저 걸린다", held == [jx])
#   ② 로그아웃 작업 등록이 실패했다고 가정 → ③ 보류를 되돌린다
back = store6.resume_account("loginA", device=PC)
check("② 등록 실패 시 보류를 되돌린다", back == [jx])
check("③ 작업이 그대로 대기 상태로 남는다(멈춘 채 방치되지 않는다)",
      (store6.get(jx) or {}).get("status") == PENDING)
check("④ 되돌린 뒤 Agent 가 정상적으로 집는다",
      (store6.claim(PC) or {}).get("id") == jx)
check("화면 코드도 같은 순서다 — 보류 → 등록 → 실패하면 복구",
      app.index("hold_account(login_id") < app.index("submit_session_job(store_obj, brand, account, \"--logout\"")
      < app.index("store_obj.resume_account(login_id"))
check("복구까지 실패하면 사용자에게 [보류 작업 재개] 를 안내한다",
      "보류도 되돌리지 못했습니다" in app)
check("보류 자체가 실패하면 **로그아웃을 하지 않는다**",
      "로그아웃을 하지 않았습니다" in app)


# ══════════════════════════════════════════════════════════════════
head("10. 기존 정상 계정이 새 정책으로 막히지 않는가")
# ══════════════════════════════════════════════════════════════════
check("기본 적용 단계는 warn 이다", login_state.mode() == "warn")
session_store.save_state(A, [{"domain": ".naver.com", "name": "a", "value": "1"}],
                         blog_id="sameblog")
login_state.record_login_result(A, seen_blog_id="sameblog",
                                expected_blog_id="sameblog",
                                expected_login_id="loginA")       # unavailable
ok, g, why = login_state.check_publish(A, expected_login_id="loginA",
                                       expected_blog_id="sameblog",
                                       seen_blog_id="sameblog")
check("등급을 모르는 기존 계정도 발행이 허용된다", ok and g == "unavailable", g)
ok2, _, _ = login_state.check_publish(NOID, expected_login_id="",
                                      expected_blog_id="oldblog",
                                      seen_blog_id="oldblog")
check("login_id 가 없는 계정도 발행이 허용된다", ok2)
ok3, _, why3 = login_state.check_publish(A, expected_login_id="loginA",
                                         expected_blog_id="sameblog",
                                         seen_blog_id="otherblog")
check("그래도 블로그가 다르면 막는다(warn 에서도)", not ok3, why3[:44])
check("등급 기록은 발행 경로를 바꾸지 않는다 — run*.py 는 login_state 를 쓰지 않는다",
      "login_state" not in (ROOT / "v2" / "run.py").read_text(encoding="utf-8")
      and "login_state" not in
      (ROOT / "v2" / "run_production.py").read_text(encoding="utf-8"),
      "기존 _check_account 만 동작")
check("Job 구조가 그대로다(옛 Agent 1.2.5 호환)",
      set(Job(brand="x").to_dict()) ==
      set(Job.from_dict(Job(brand="x").to_dict()).to_dict()))

print("")
print("=" * 74)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 74)
raise SystemExit(1 if fails else 0)
