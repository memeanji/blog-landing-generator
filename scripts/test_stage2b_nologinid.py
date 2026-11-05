r"""Stage 2 보완 검증 — **login_id 가 없는 계정의 로그아웃** + 세션 오염 방지.

    .\.venv\Scripts\python.exe scripts\test_stage2b_nologinid.py

검증 항목(사용자 요청 1·2·5번)
    ① login_id 있는 계정 → login_id 기준
    ② login_id 없는 계정 → **account.id 정확일치** 기준(세션 폴더)
    ③ blog_id 는 어떤 경우에도 식별 기준이 아니다
    ④ 세션을 공유하는 계정이 있으면 **영향 범위를 보여 주고 자동 로그아웃 금지**
    ⑤ 선택 계정 외의 인증정보는 변경·삭제되지 않는다
    ⑥ 등록 blog_id 가 없어도 **저장돼 있던 blog_id** 와 대조해 세션 오염을 막는다
    ⑦ 어긋난 로그인은 영구 프로필에도 흔적을 남기지 않는다(쿠키 제거)
    ⑧ 검증됨 / 어긋남 / 확인 불가 3상태 구분
    ⑨ 기존 정상 계정은 불필요하게 재로그인되지 않는다

★실제 운영 데이터(accounts_cache.json 2026-09-18)의 모양을 그대로 재현한다.
  브라우저·네이버·시트·운영 큐는 건드리지 않는다.
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

SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage2b_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ.pop("BLOG_LOGIN_CHECK", None)

from v2 import (accounts, browser, login_state, queue_store,       # noqa: E402
                session_store)
from v2.job import Job                                            # noqa: E402
from v2.queue_store import HELD, LocalStore, PENDING, RUNNING      # noqa: E402

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


def head(title: str) -> None:
    print("")
    print("=" * 74)
    print(f" {title}")
    print("=" * 74)


PC = "pc1"
#  ★운영 데이터(2026-09-18 snapshot)의 모양 그대로:
#    seoyeon(login_id 있음) 과 tab_caa3dca8(login_id 없음) 이 **같은 blog_id**,
#    shin894894(있음) 과 tab_7605d481(없음) 이 또 같은 blog_id.
ROWS = [
    {"id": "seoyeon", "label": "행복하서연", "login_id": "rhksrhf6996",
     "blog_id": "hangbokhaseo7", "brand": "", "ref_tab": "행복하서연 기준랜딩"},
    {"id": "tab_caa3dca8", "label": "행복하서연", "login_id": "",
     "blog_id": "hangbokhaseo7", "brand": "doctor_nuscent",
     "ref_tab": "행복하서연 기준랜딩"},
    {"id": "shin894894", "label": "행복하서연 대표님",
     "login_id": "shin894894@naver.com", "blog_id": "seoyoungene_",
     "brand": "repurely", "ref_tab": "행복하서연 대표님 기준랜딩"},
    {"id": "tab_7605d481", "label": "행복하서연대표님", "login_id": "",
     "blog_id": "seoyoungene_", "brand": "doctor_nuscent",
     "ref_tab": "행복하서연대표님 기준랜딩"},
    {"id": "noblogid_acc", "label": "행복하서연 noblogid_acc", "login_id": "noblogid_acc",
     "blog_id": "", "brand": "doctor_nuscent",
     "ref_tab": "블로그ID 없는 계정 기준랜딩"},
]
ACCOUNTS = SANDBOX / "accounts.json"
ACCOUNTS.write_text(json.dumps({"accounts": ROWS}, ensure_ascii=False),
                    encoding="utf-8")
accounts.ACCOUNTS_PATH = ACCOUNTS
accounts.refresh()
A = accounts.find_exact("seoyeon", ACCOUNTS)            # login_id 있음
NOID = accounts.find_exact("tab_caa3dca8", ACCOUNTS)    # login_id 없음 · 같은 blog_id
B = accounts.find_exact("shin894894", ACCOUNTS)
NOID2 = accounts.find_exact("tab_7605d481", ACCOUNTS)
TAX = accounts.find_exact("noblogid_acc", ACCOUNTS)          # login_id 있음 · blog_id 없음


def mkjob(acc, count: int = 10) -> Job:
    return Job(brand=acc.brand or "repurely", account=acc.id,
               account_name=acc.label, login_id=acc.login_id,
               ref_tab=acc.ref_tab, flow="production", mode="convert",
               count=count, media="gfa")


# ══════════════════════════════════════════════════════════════════
head("1. 식별자 우선순위 — login_id → account.id · blog_id 는 절대 아님")
# ══════════════════════════════════════════════════════════════════
rec_a = {"job": {"account": "seoyeon", "login_id": "rhksrhf6996",
                 "blog_id": "hangbokhaseo7"}}
rec_n = {"job": {"account": "tab_caa3dca8", "login_id": "",
                 "blog_id": "hangbokhaseo7"}}
check("login_id 를 주면 그것만 본다",
      queue_store.matches(rec_a, "rhksrhf6996", "")
      and not queue_store.matches(rec_n, "rhksrhf6996", ""))
check("login_id 를 주면 account.id 는 보지 않는다(섞이지 않는다)",
      not queue_store.matches(rec_n, "rhksrhf6996", "tab_caa3dca8"),
      "login_id 우선")
check("login_id 가 없을 때만 account.id 정확일치로 본다",
      queue_store.matches(rec_n, "", "tab_caa3dca8")
      and not queue_store.matches(rec_a, "", "tab_caa3dca8"))
check("account.id 는 대소문자까지 정확히 같아야 한다(폴더 이름)",
      not queue_store.matches(rec_n, "", "TAB_CAA3DCA8"))
check("부분일치로는 안 걸린다",
      not queue_store.matches(rec_n, "", "tab_caa3"))
check("blog_id 를 식별자로 주면 **아무것도** 걸리지 않는다",
      not queue_store.matches(rec_a, "", "hangbokhaseo7")
      and not queue_store.matches(rec_n, "", "hangbokhaseo7"),
      "blog_id 는 account.id 가 아니다")
check("둘 다 비면 아무것도 고르지 않는다",
      not queue_store.matches(rec_a, "", "") and not queue_store.matches(rec_n, "", ""))


# ══════════════════════════════════════════════════════════════════
head("2. login_id 없는 계정의 보류/재개 — account.id 기준")
# ══════════════════════════════════════════════════════════════════
store = LocalStore(SANDBOX / "q1")
j_a = store.submit(mkjob(A), title="A(login_id 있음)", target_agent=PC)
j_n = store.submit(mkjob(NOID), title="구계정(login_id 없음)", target_agent=PC)
j_b = store.submit(mkjob(B), title="B", target_agent=PC)
j_n2 = store.submit(mkjob(NOID2), title="구계정2", target_agent=PC)

held = store.hold_account("", device=PC, account_id="tab_caa3dca8")
check("login_id 없는 계정도 **보류된다**", held == [j_n], str(held))
check("보류된 작업은 held 다", (store.get(j_n) or {}).get("status") == HELD)
check("같은 blog_id 인 다른 계정(A)은 영향 없음",
      (store.get(j_a) or {}).get("status") == PENDING)
check("다른 계정(B·구계정2)도 영향 없음",
      (store.get(j_b) or {}).get("status") == PENDING
      and (store.get(j_n2) or {}).get("status") == PENDING)
check("blog_id 로 보류를 시도해도 0건",
      store.hold_account("", device=PC, account_id="hangbokhaseo7") == []
      and (store.get(j_a) or {}).get("status") == PENDING)
drained = []
while True:
    got = store.claim(PC)
    if got is None:
        break
    drained.append(got["id"])
    store.finish(got["id"], queue_store.DONE, 0)
check("Agent 는 보류된 작업을 집지 않는다", j_n not in drained,
      f"집은 작업 {len(drained)}건")
check("나머지 3건은 정상 처리된다", set(drained) == {j_a, j_b, j_n2})
back = store.resume_account("", device=PC, account_id="tab_caa3dca8")
check("재개도 account.id 로 된다", back == [j_n])
check("재개 후 Agent 가 집는다", (store.claim(PC) or {}).get("id") == j_n)

check("held_jobs 도 account.id 로 조회된다",
      store.held_jobs("", "tab_caa3dca8") == [])
check("running_for_login 도 account.id 를 받는다",
      len(store.running_for_login("", "tab_caa3dca8")) == 1,
      f"{len(store.running_for_login('', 'tab_caa3dca8'))}건")


# ══════════════════════════════════════════════════════════════════
head("3. 세션 공유 영향 범위 — 불분명하면 자동 로그아웃 금지")
# ══════════════════════════════════════════════════════════════════
for acc in (A, NOID, B, NOID2, TAX):
    session_store.ensure(acc)
    session_store.save_state(acc, [{"domain": ".naver.com", "name": "x",
                                    "value": "v"}],
                             blog_id=acc.blog_id or "")

#  ★정책 변경 (2026-11-05 · 4-3 A안) — 예전에는 `blog_id` 를 보지 않아
#    "같은 blog_id 라도 세션을 공유하지 않으면 바로 로그아웃 가능" 이었다.
#    그런데 `_server_logout` 은 네이버 **서버** 세션을 끊고, 그건 같은 네이버
#    계정 전체에 걸린다 — 쿠키 폴더가 분리돼 있어도 막지 못한다.
#    그래서 같은 blog_id 를 **위험 후보**로 올리고, 로그인 계정이 같은지
#    확인되지 않으면 막는다(모르면 막는다). 상세 검증은 stage10 에 있다.
ok, peers, why = login_state.logout_allowed(NOID, path=ACCOUNTS)
check("★같은 blog_id 인데 login_id 가 미등록이면 **막는다**(정책 변경)",
      (not ok) and len(peers) == 1 and peers[0]["id"] == "seoyeon",
      f"후보 {[q['id'] for q in peers]} · {why[:40]}")
check("  사유에 blog_id 가 같다고 적힌다",
      peers and "blog_id" in peers[0]["why"], peers[0]["why"][:46])
check("  같은 계정이라고 단정하지 않는다(확인되지 않음)",
      peers and "확인되지 않음" in peers[0]["why"])
ok_a, peers_a, why_a = login_state.logout_allowed(A, path=ACCOUNTS)
check("★login_id 가 있어도 상대가 미등록이면 막는다(한쪽만 알면 확인 불가)",
      (not ok_a) and len(peers_a) == 1
      and peers_a[0]["id"] == "tab_caa3dca8",
      f"후보 {[q['id'] for q in peers_a]}")
check("  blog_id 가 없는 계정은 후보로 묶지 않는다(빈 값끼리 묶이면 안 된다)",
      login_state.logout_allowed(TAX, path=ACCOUNTS)[1] == [],
      str(login_state.logout_allowed(TAX, path=ACCOUNTS)[1]))

# ── profile_dir 로 같은 폴더를 가리키는 경우(공유) ─────────────────
shared_rows = list(ROWS) + [{
    "id": "shared_other", "label": "같은 프로필을 쓰는 다른 계정", "login_id": "",
    "blog_id": "hangbokhaseo7", "brand": "repurely", "ref_tab": "공유 탭",
    "profile_dir": str(session_store.profile_dir(NOID))}]
SHARED = SANDBOX / "accounts_shared.json"
SHARED.write_text(json.dumps({"accounts": shared_rows}, ensure_ascii=False),
                  encoding="utf-8")
accounts.refresh()
peers = login_state.session_peers(NOID, path=SHARED)
_by = {q["id"]: q for q in peers}
#  ★이제 후보가 둘이다 — `shared_other`(profile_dir 공유) + `seoyeon`(같은 blog_id).
#    ①번 경로(profile_dir)가 그대로 동작하는지가 여기서 볼 것이다.
check("같은 프로필 폴더를 쓰는 계정을 찾아낸다", "shared_other" in _by,
      str(sorted(_by)))
check("왜 공유로 보는지 사유가 붙는다",
      "profile_dir" in _by.get("shared_other", {}).get("why", ""),
      _by.get("shared_other", {}).get("why", "")[:40])
check("  blog_id 경로가 profile_dir 사유를 덮어쓰지 않는다",
      "blog_id" not in _by.get("shared_other", {}).get("why", ""))
check("  같은 blog_id 인 계정도 함께 후보가 된다(2026-11-05)",
      "seoyeon" in _by, str(sorted(_by)))
ok, peers, why = login_state.logout_allowed(NOID, path=SHARED)
check("로그인 계정이 같은지 확인되지 않으면 **금지**", not ok, why[:56])
check("금지 사유에 영향 계정이 나열된다", "shared_other" in why)
check("영향 범위 목록을 함께 돌려준다", len(peers) == 2,
      f"{[q['id'] for q in peers]}")

sess = (ROOT / "v2" / "session.py").read_text(encoding="utf-8")
check("CLI 는 공유가 불분명하면 중단한다(--confirm-shared 없이는)",
      "logout_blocked" in sess and "shared_session_unverified" in sess)
check("CLI 는 영향 범위를 로그에 먼저 적는다",
      "영향받을 수 있는 등록" in sess)
check("  각 줄에 세션(쿠키) 보유 여부를 적는다(2026-11-05)",
      "이 PC 에 세션 없음" in sess and "개 저장됨" in sess)
check("사용자가 확인하면 진행한다",
      "confirm_shared" in sess and "영향 범위를 확인했습니다" in sess)
app = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
check("화면에 '공유 세션 확인' 체크가 있다", "공유 세션 확인" in app)
check("화면은 login_id 가 없어도 버튼을 연다",
      "disabled=not (device_id and (lid or aid) and can_logout)" in app)
check("화면이 '이 계정 전용 세션' 기준임을 안내한다",
      "이 계정 전용 세션" in app)
check("내부 세션 폴더 이름은 화면에 노출하지 않는다(2026-09-17 결정)",
      "세션 폴더 `{aid}`" not in app)
check("★화면이 blog_id 의 쓰임을 정확히 적는다(2026-11-05)",
      "같은 네이버 계정일 수 있어" in app
      and "단정하지는 않습니다" in app)
accounts.refresh()


# ══════════════════════════════════════════════════════════════════
head("4. 선택 계정 외 인증정보는 건드리지 않는다")
# ══════════════════════════════════════════════════════════════════
before = {a.id: session_store.describe(a)["cookies"] for a in (A, B, NOID2, TAX)}
prof_keep = session_store.profile_dir(A)
(prof_keep / "Local State").write_text("keep", encoding="utf-8")
res = session_store.logout(NOID)
check("고른 계정만 초기화된다",
      session_store.describe(NOID)["state_exists"] is False, str(res["local_cleared"]))
after = {a.id: session_store.describe(a)["cookies"] for a in (A, B, NOID2, TAX)}
check("다른 계정 쿠키 수가 그대로다", before == after, f"{before} == {after}")
check("다른 계정 프로필 파일도 그대로다", (prof_keep / "Local State").exists())
check("같은 blog_id 인 A 는 여전히 로그인 상태다",
      session_store.describe(A)["state_exists"] is True)


# ══════════════════════════════════════════════════════════════════
head("5. 세션 오염 방지 — 등록 blog_id 가 없어도 막는다")
# ══════════════════════════════════════════════════════════════════
#   noblogid_acc 은 **등록 blog_id 가 비어 있다**(운영에 그런 계정이 있다).
check("noblogid_acc 은 등록 blog_id 가 없다", browser.registered_blog_id("noblogid_acc") == "")
session_store.save_state(TAX, [{"domain": ".naver.com", "name": "t", "value": "1"}],
                         blog_id="realblog")
check("그 세션에 저장돼 있던 blog_id 를 읽는다",
      browser.saved_blog_id("noblogid_acc") == "realblog")
v, why = browser.save_verdict("noblogid_acc", "realblog")
check("저장값과 같으면 verified", v == "verified", f"{v} / {why[:30]}")
v, why = browser.save_verdict("noblogid_acc", "otherblog")
check("저장값과 다르면 mismatch — 등록값이 없어도 막는다", v == "mismatch",
      f"{v} / {why[:46]}")
check("save_blocked 도 막는다", bool(browser.save_blocked("noblogid_acc", "otherblog")))

v, why = browser.save_verdict("no_such_account", "anyblog")
check("기준값이 전혀 없으면 unknown(첫 로그인) — 예전처럼 저장한다",
      v == "unknown" and browser.save_blocked("no_such_account", "anyblog") == "",
      f"{v}")
v, why = browser.save_verdict("seoyeon", "hangbokhaseo7")
check("등록 blog_id 가 있으면 그것을 1순위로 쓴다", v == "verified",
      f"{v} / {why[:30]}")
v, why = browser.save_verdict("seoyeon", "")
check("기준값이 있는데 블로그를 못 읽으면 unreadable — 기존 세션을 보존한다",
      v == "unreadable" and bool(browser.save_blocked("seoyeon", "")),
      f"{v} / {why[:50]}")
v, why = browser.save_verdict("no_such_account", "")
check("기준값도 없고 못 읽었으면 unknown(예전 동작)",
      v == "unknown" and browser.save_blocked("no_such_account", "") == "", v)


class FakeCtx:
    """`cookies()` / `clear_cookies()` 만 흉내 내는 가짜 컨텍스트."""

    def __init__(self) -> None:
        self.read = 0
        self.cleared = 0

    async def cookies(self):
        self.read += 1
        return [{"domain": ".naver.com", "name": "NID_SES", "value": "SECRET"}]

    async def clear_cookies(self):
        self.cleared += 1


logs: list[str] = []
ctx = FakeCtx()
n = asyncio.run(browser._persist_session(ctx, "noblogid_acc", "otherblog", logs.append))
check("어긋나면 저장 0건", n == 0)
check("어긋나면 **프로필 쿠키까지 지운다**(자동 로그인 재발 방지)",
      ctx.cleared == 1, f"clear_cookies {ctx.cleared}회")
check("어긋나면 쿠키를 읽지 않는다", ctx.read == 0)
check("쿠키 값은 로그에 남지 않는다", not any("SECRET" in m for m in logs))
check("기존 세션(realblog)이 덮어써지지 않았다",
      browser.saved_blog_id("noblogid_acc") == "realblog")

logs.clear()
ctx2 = FakeCtx()
n = asyncio.run(browser._persist_session(ctx2, "noblogid_acc", "realblog", logs.append))
check("맞으면 예전처럼 저장된다(쿠키 제거 없음)",
      n == 1 and ctx2.cleared == 0, f"{n}건 · clear {ctx2.cleared}회")

logs.clear()
ctx3 = FakeCtx()
n = asyncio.run(browser._persist_session(ctx3, "no_such_account", "anyblog",
                                         logs.append))
check("unknown 은 저장하되 '검증됨' 으로 적지 않는다",
      n == 1 and any("검증됨' 으로 기록하지 않습니다" in m for m in logs),
      logs[0][:48] if logs else "")
check("unknown 에서는 쿠키를 지우지 않는다(기존 계정 재로그인 유발 금지)",
      ctx3.cleared == 0)

br = (ROOT / "v2" / "browser.py").read_text(encoding="utf-8")
check("로그인창 경로도 어긋나면 쿠키를 headless 로 넘기지 않는다",
      "cookies = []" in br and "_wipe_context_cookies(ctx, log)" in br)
check("프로필 폴더 자체는 지우지 않는다(기기 등록 흔적 보존)",
      "프로필 폴더는 보존" in br)


# ══════════════════════════════════════════════════════════════════
head("5-2. unknown / unreadable 의 경계 — 세션 오염이 가능한가")
# ══════════════════════════════════════════════════════════════════
#   ① unreadable: 기준값이 있는데 지금 블로그를 못 읽음 → **저장 금지 · 쿠키 유지**
session_store.save_state(TAX, [{"domain": ".naver.com", "name": "t", "value": "1"}],
                         blog_id="realblog")
before_saved = browser.saved_blog_id("noblogid_acc")
logs2: list[str] = []
ctx4 = FakeCtx()
n = asyncio.run(browser._persist_session(ctx4, "noblogid_acc", "", logs2.append))
check("unreadable 이면 저장하지 않는다", n == 0)
check("unreadable 이면 쿠키를 지우지 않는다(정상 세션 유지)", ctx4.cleared == 0,
      f"clear {ctx4.cleared}회")
check("기존 기준값(realblog)이 그대로 남는다 — 대조 능력을 잃지 않는다",
      browser.saved_blog_id("noblogid_acc") == before_saved == "realblog")
check("이유를 로그에 남긴다", any("저장하지 않았습니다" in m for m in logs2))

#   ② unknown 은 **기준값이 아예 없을 때만** 일어난다 → 한 번 저장되면 자기치유
fresh_acc = accounts.Account(id="brand_new", label="새 계정", login_id="",
                             blog_id="", ref_tab="새 탭", brand="repurely")
v, _ = browser.save_verdict("brand_new", "firstblog")
check("기준값이 없는 첫 로그인만 unknown 이다", v == "unknown", v)
session_store.save_state(fresh_acc, [{"domain": ".naver.com", "name": "n",
                                      "value": "1"}], blog_id="firstblog")
v2_, _ = browser.save_verdict("brand_new", "firstblog")
v3_, _ = browser.save_verdict("brand_new", "otherblog")
check("한 번 저장되면 그 뒤로는 대조된다(unknown 이 반복되지 않는다)",
      v2_ == "verified" and v3_ == "mismatch", f"{v2_} / {v3_}")

#   ③ unknown 저장이 **다른 계정**을 건드릴 수 있나 → 구조적으로 불가
check("저장 경로는 그 계정 폴더 하나뿐이다",
      "state_path(acc)" in (ROOT / "v2" / "session_store.py").read_text(
          encoding="utf-8"))
check("다른 계정(A·B) 세션은 unknown 저장 뒤에도 그대로다",
      session_store.describe(A)["state_exists"] is True
      and session_store.describe(B)["state_exists"] is True)

#   ④ unknown 상태에서 **세션 별칭이 생기지 않는다**
login_state.set_account_map(fresh_acc, login_id="", blog_id="firstblog")
st_new = login_state.record_login_result(
    fresh_acc, seen_blog_id="firstblog", expected_blog_id="",
    expected_login_id="")
check("unknown 저장은 등급을 unavailable 로 둔다",
      st_new["grade"] == login_state.UNAVAILABLE, st_new["grade"])
linked, why_l = login_state.link_session("someone", fresh_acc,
                                         grade=st_new["grade"])
check("unavailable 등급에서는 별칭이 생성되지 않는다", not linked, why_l[:46])
check("별칭 표에 그 세션이 올라가지 않았다",
      login_state.session_for_login("someone") == "")

#   ⑤ 등록 blog_id 가 없는 계정은 **발행 자체가 막힌다**(unknown 이 발행으로
#      이어질 수 없다) — 기존 _check_account 규칙
guards = {}
for mod in ("run", "run_production"):
    t = (ROOT / "v2" / f"{mod}.py").read_text(encoding="utf-8")
    guards[mod] = blogid_guard(t)
    check(f"v2/{mod}.py — 등록 blog_id 보호 수준 판별",
          guards[mod] in ("strict", "skip"), guards[mod])
if all(g == "strict" for g in guards.values()):
    print("     → 이 트리에서는 등록 blog_id 가 없는 계정은 **발행 자체가 막힌다**.")
    print("        따라서 unknown 저장이 잘못된 발행으로 이어질 수 없다.")
else:
    print("     → ★이 트리(git HEAD)에서는 등록 blog_id 가 비면 계정 대조를")
    print("        **건너뛴다**. 그래서 unknown 상태의 방어는 browser.save_verdict")
    print("        (저장 차단) 하나에 의존한다 — 9/17 미커밋 변경을 함께 올리면")
    print("        발행 전 차단까지 복원된다.")

# ══════════════════════════════════════════════════════════════════
head("6. 기존 정상 계정 영향 없음")
# ══════════════════════════════════════════════════════════════════
check("등록 blog_id 와 맞는 계정은 그대로 저장·진행",
      browser.save_blocked("seoyeon", "hangbokhaseo7") == ""
      and browser.save_blocked("shin894894", "seoyoungene_") == "")
check("같은 blog_id·다른 login_id 계정도 각자 정상 저장",
      browser.save_blocked("tab_7605d481", "seoyoungene_") == "")
check("발행 경로(run*.py)는 여전히 login_state 를 쓰지 않는다",
      "login_state" not in (ROOT / "v2" / "run.py").read_text(encoding="utf-8")
      and "login_state" not in
      (ROOT / "v2" / "run_production.py").read_text(encoding="utf-8"))
check("Job 구조 변화 없음(옛 Agent 호환)",
      set(Job(brand="x").to_dict()) ==
      set(Job.from_dict(Job(brand="x").to_dict()).to_dict()))
check("보류는 kind=run 만(로그인·로그아웃 작업은 보류되지 않는다)",
      '(rec.get("kind") or "run") != "run"' in
      (ROOT / "v2" / "queue_store.py").read_text(encoding="utf-8"))

#   ★로그인 유지(저장 세션 재사용)가 그대로 동작하는지 — 쿠키 복원 경로 확인
br2 = (ROOT / "v2" / "browser.py").read_text(encoding="utf-8")
check("저장 세션 복원(_prime_session)은 손대지 않았다",
      "async def _prime_session" in br2 and "load_cookies(account)" in br2)
check("headless 로 저장 세션을 먼저 확인하는 경로가 그대로다",
      "저장된 세션 확인" in br2 and "창 없이 진행합니다" in br2)
check("검증 통과한 계정은 예전처럼 쿠키가 저장된다(로그인 유지)",
      browser.save_blocked("seoyeon", "hangbokhaseo7") == "")
check("쿠키를 지우는 것은 mismatch 뿐이다(다른 판정은 세션을 끊지 않는다)",
      br2.count("await _wipe_context_cookies(ctx, log)") == 2
      and 'if verdict == "mismatch"' in br2,
      f"호출 {br2.count('await _wipe_context_cookies(ctx, log)')}곳(= mismatch 경로 2곳)")

print("")
print("=" * 74)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 74)
raise SystemExit(1 if fails else 0)
