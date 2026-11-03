r"""계정 식별·세션 분리 검증 — **운영 폴더를 전혀 건드리지 않는다.**

    .\.venv\Scripts\python.exe scripts\test_session_identity.py

확인하는 정책(2026-09-17 확정 · 2026-11-03 재확인)
  · `login_id` 가 다르면 `blog_id` 가 같아도 **다른 계정 · 다른 세션**
  · `login_id` 가 같으면 기준랜딩 탭이 여러 개여도 **같은 세션 재사용**
  · `blog_id` 는 계정 통합·세션 식별에 쓰지 않는다(로그인 후 대조용)
  · `login_id` 가 빈 계정은 blog_id 로 추측하거나 자동 통합하지 않는다

★임시 폴더를 `BLOG_DATA_DIR` 로 잡아 **그 안에서만** 세션 파일을 만든다.
  `%APPDATA%\BlogLandingAgent` 와 저장소의 sessions/ 는 읽지도 쓰지도 않는다.
  쿠키는 가짜 문자열이고, 브라우저·네이버·시트는 전혀 쓰지 않는다.
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

# ── ★v2 를 import 하기 **전에** 데이터 폴더를 임시폴더로 바꿔 끼운다 ──
SANDBOX = Path(tempfile.mkdtemp(prefix="blog_ident_test_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"       # Supabase 조회 금지(오프라인)
os.environ.pop("BLOG_DEVICE_TOKEN", None)

from v2 import accounts, session_store                         # noqa: E402
from v2.appdir import ROOT as DATA_ROOT                        # noqa: E402

OK, NG = "  ✅", "  ❌"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        fails.append(name)


print(f"샌드박스 = {DATA_ROOT}")
check("샌드박스가 운영 폴더가 아니다",
      "BlogLandingAgent" not in str(DATA_ROOT)
      and str(ROOT) not in str(DATA_ROOT), str(DATA_ROOT))

# ══════════════════════════════════════════════════════════════════
#  가짜 계정 목록 — **같은 blog_id · 다른 login_id** 두 계정이 핵심
# ══════════════════════════════════════════════════════════════════
SAME_BLOG = "same_blog"
ACC_PATH = SANDBOX / "accounts.json"
ACC_PATH.write_text(json.dumps({"accounts": [
    {"id": "session_A", "label": "계정 A", "login_id": "account_A",
     "blog_id": SAME_BLOG, "brand": "repurely", "ref_tab": "A 기준랜딩"},
    {"id": "session_B", "label": "계정 B", "login_id": "account_B",
     "blog_id": SAME_BLOG, "brand": "repurely", "ref_tab": "B 기준랜딩"},
    # 같은 login_id 를 두 탭에서 쓰는 계정(한 레코드) — 세션 재사용 확인용
    {"id": "session_C", "label": "계정 C", "login_id": "account_C",
     "blog_id": "blog_c", "brand": "repurely", "ref_tab": "C 기준랜딩"},
    # login_id 가 빈 계정 — 추측·통합 금지 확인용
    {"id": "session_D", "label": "계정 D", "login_id": "",
     "blog_id": SAME_BLOG, "brand": "repurely", "ref_tab": "D 기준랜딩"},
]}, ensure_ascii=False), encoding="utf-8")


def acc(key: str):
    got = accounts.find_exact(key, path=ACC_PATH)
    assert got is not None, key
    return got


A, B, C, D = acc("session_A"), acc("session_B"), acc("session_C"), acc("session_D")

# ══════════════════════════════════════════════════════════════════
print()
print("1) 같은 blog_id · 다른 login_id → 다른 계정으로 고른다")
by_a = accounts.find_by_identity("account_A", "", path=ACC_PATH)
by_b = accounts.find_by_identity("account_B", "", path=ACC_PATH)
check("login_id=account_A → session_A", by_a is not None and by_a.id == "session_A",
      getattr(by_a, "id", None))
check("login_id=account_B → session_B", by_b is not None and by_b.id == "session_B",
      getattr(by_b, "id", None))
check("같은 blog_id 인데도 서로 다른 계정", by_a.id != by_b.id)
check("A 와 B 의 등록 blog_id 는 같다(= 통합 조건이 아님)",
      by_a.blog_id == by_b.blog_id == SAME_BLOG)

# blog_id 로는 고를 수 없어야 한다 — 중복이면 '모를 수 없다' 가 아니라 **오류**
try:
    accounts.find_by_identity("", SAME_BLOG, path=ACC_PATH)
    check("blog_id 만으로는 계정을 정하지 않는다(중복 → 오류)", False, "조용히 골랐다")
except RuntimeError as exc:
    check("blog_id 만으로는 계정을 정하지 않는다(중복 → 오류)", True,
          str(exc).split(".")[0][:70])

# ══════════════════════════════════════════════════════════════════
print()
print("2) 세션 폴더 분리 — 쿠키가 서로 넘어가지 않는다")
dir_a, dir_b = session_store.session_dir(A), session_store.session_dir(B)
check("A·B 세션 폴더가 다르다", dir_a != dir_b, f"{dir_a.name} vs {dir_b.name}")
check("폴더 이름이 blog_id 가 아니다(= account.id 기준)",
      dir_a.name == "session_A" and dir_b.name == "session_B",
      f"{dir_a.name} / {dir_b.name}")
check("프로필 폴더도 분리된다",
      session_store.profile_dir(A) != session_store.profile_dir(B))

session_store.save_state(A, [{"name": "NID_SES", "value": "A-only",
                              "domain": ".naver.com"}], blog_id=SAME_BLOG)
session_store.save_state(B, [{"name": "NID_SES", "value": "B-only",
                              "domain": ".naver.com"},
                             {"name": "NID_AUT", "value": "B-only",
                              "domain": ".naver.com"}], blog_id=SAME_BLOG)
ck_a = session_store.load_cookies(A)
ck_b = session_store.load_cookies(B)
check("A 쿠키 1개 · B 쿠키 2개로 각자 보관", len(ck_a) == 1 and len(ck_b) == 2,
      f"{len(ck_a)} / {len(ck_b)}")
check("A 세션에 B 쿠키가 섞이지 않았다",
      all(c["value"] == "A-only" for c in ck_a))
check("B 세션에 A 쿠키가 섞이지 않았다",
      all(c["value"] == "B-only" for c in ck_b))
info_a, info_b = session_store.describe(A), session_store.describe(B)
check("세션 기록에 account id 가 들어간다",
      info_a["id"] == "session_A" and info_b["id"] == "session_B")
check("같은 blog_id 를 각자 적어 둔다(대조용)",
      info_a["blog_id"] == info_b["blog_id"] == SAME_BLOG)

# ══════════════════════════════════════════════════════════════════
print()
print("3) 한쪽만 로그아웃(세션 초기화) — 다른 쪽은 그대로")
gone = session_store.clear_state(A)                # 프로필은 남긴다(기본값)
check("A 의 state/meta 를 지웠다", len(gone) == 2, str(gone))
check("A 는 '세션 없음' 이 된다",
      session_store.describe(A)["state_exists"] is False)
check("B 세션은 그대로 살아 있다",
      session_store.describe(B)["state_exists"] is True
      and len(session_store.load_cookies(B)) == 2)
check("B 쿠키 값도 그대로", all(c["value"] == "B-only"
                            for c in session_store.load_cookies(B)))

session_store.save_state(A, [{"name": "NID_SES", "value": "A-again",
                              "domain": ".naver.com"}], blog_id=SAME_BLOG)
gone_b = session_store.clear_state(B)
check("이번엔 B 만 지운다", len(gone_b) == 2, str(gone_b))
check("A 는 다시 로그인한 상태로 유지된다",
      session_store.describe(A)["state_exists"] is True
      and session_store.load_cookies(A)[0]["value"] == "A-again")
check("같은 blog_id 라는 이유로 함께 지워지지 않는다",
      session_store.describe(B)["state_exists"] is False)

# ══════════════════════════════════════════════════════════════════
print()
print("4) 같은 login_id · 여러 기준랜딩 탭 → 한 세션 재사용")


class FakeBrand:                       # brands.json 의 탭→계정 표만 흉내낸다
    id = "repurely"

    def __init__(self, table: dict) -> None:
        self.table = table

    def account_of(self, tab: str) -> dict:
        return self.table.get(tab, {})


def use_brand_table(table: dict):
    """`brands.resolve()` 를 가짜 표로 바꿔 끼운다(실제 brands.json 을 안 읽게)."""
    from v2 import brands as _brands
    old = _brands.resolve
    _brands.resolve = lambda *_a, **_k: FakeBrand(table)       # type: ignore[assignment]
    return _brands, old


# 같은 login_id(account_C)를 **두 탭**에 적어 둔 상태(= 운영에서 하려는 일).
#   ★브랜드는 실제 호출부처럼 **문자열**("repurely")로 넘긴다 — 그래야
#     `tab_for_brand` 비교가 실제와 같게 동작한다.
_b, _old_resolve = use_brand_table({
    "C 기준랜딩":  {"login_id": "account_C", "name": "계정 C"},
    "C2 기준랜딩": {"login_id": "account_C", "name": "계정 C(다른 탭)"},
})
try:
    got1 = accounts.resolve_for("", "C 기준랜딩", "repurely", create=False,
                                path=ACC_PATH)
    got2 = accounts.resolve_for("", "C2 기준랜딩", "repurely", create=False,
                                path=ACC_PATH)
finally:
    _b.resolve = _old_resolve                                  # type: ignore[assignment]
check("두 탭이 같은 계정으로 해석된다",
      got1 is not None and got2 is not None and got1.id == got2.id == "session_C",
      f"{getattr(got1, 'id', None)} / {getattr(got2, 'id', None)}")
check("따라서 세션 폴더도 같다(재로그인 없음)",
      session_store.session_dir(got1) == session_store.session_dir(got2))
session_store.save_state(C, [{"name": "NID_SES", "value": "C",
                              "domain": ".naver.com"}], blog_id="blog_c")
check("한 탭에서 로그인하면 다른 탭에서도 세션 있음",
      session_store.describe(got2)["state_exists"] is True)
check("선택 근거가 login_id(브랜드 설정의 탭→login_id)로 남는다",
      "login_id=account_C" in accounts.last_basis()
      and "brands.json" in accounts.last_basis(), accounts.last_basis())

# ══════════════════════════════════════════════════════════════════
print()
print("5) login_id 가 빈 계정 — 추측·자동 통합 금지")
_b, _old_resolve = use_brand_table({"D 기준랜딩": {"name": "계정 D"}})   # login_id 없음
try:
    got_d = accounts.resolve_for("", "D 기준랜딩", "repurely", create=False,
                                 path=ACC_PATH)
    basis_d = accounts.last_basis()
    unknown = accounts.resolve_for("", "없는 기준랜딩", "repurely", create=False,
                                   path=ACC_PATH)
finally:
    _b.resolve = _old_resolve                                  # type: ignore[assignment]
check("등록된 ref_tab 으로만 찾는다(B 경로)",
      got_d is not None and got_d.id == "session_D", getattr(got_d, "id", None))
check("선택 근거에 'login_id 없음' 이 남는다", "login_id 없음" in basis_d, basis_d)
check("blog_id 가 같은 A·B 로 통합되지 않는다",
      got_d is not None and got_d.id not in ("session_A", "session_B"))
check("D 는 자기 세션 폴더를 쓴다",
      got_d is not None and session_store.session_dir(got_d).name == "session_D")
check("모르는 탭은 계정을 만들지 않는다(None)", unknown is None)

# ══════════════════════════════════════════════════════════════════
print()
print("6) 알려진 함정 재현 — 보고서용(정책 위반 가능 지점)")
#  ⚠️`find_exact` 는 login_id → **blog_id** → id 순으로 찾는다.
#    화면이 보내는 `--account` 는 **세션 폴더 이름(id)** 인데, 그 값이 다른 계정의
#    blog_id 와 같으면 **그 계정이 먼저 잡힌다.**
TRAP = SANDBOX / "accounts_trap.json"
TRAP.write_text(json.dumps({"accounts": [
    {"id": "same_blog", "label": "폴더 이름이 blog_id 와 같은 계정",
     "login_id": "account_X", "blog_id": "blog_x"},
    {"id": "other", "label": "다른 계정", "login_id": "account_Y",
     "blog_id": "same_blog"},
]}, ensure_ascii=False), encoding="utf-8")
hit = accounts.find_exact("same_blog", path=TRAP)
check("⚠ --account=<id> 가 남의 blog_id 와 같으면 **다른 계정**이 잡힌다(현재 코드)",
      hit is not None and hit.id == "other",
      f"기대(현재 동작)=other · 실제={getattr(hit, 'id', None)}")

#  ⚠️`_from_raw` 는 id 가 비면 **blog_id 를 세션 폴더 이름으로** 쓴다.
raw = accounts._from_raw({"login_id": "account_Z", "blog_id": "blog_z"})
check("⚠ id 가 비면 blog_id 가 세션 폴더 이름이 된다(현재 코드)",
      raw is not None and raw.id == "blog_z", getattr(raw, "id", None))

# ══════════════════════════════════════════════════════════════════
print()
print(f"(샌드박스는 그대로 남겨 둡니다 — 확인용: {SANDBOX})")
if fails:
    print(f"❌ 실패 {len(fails)}건: " + " / ".join(fails))
    sys.exit(1)
print("✅ 전부 통과")
