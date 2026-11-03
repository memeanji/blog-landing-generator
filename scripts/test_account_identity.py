# -*- coding: utf-8 -*-
"""계정 선택이 **실제 식별정보(login_id/blog_id)로만** 되는지 검증.

브라우저·시트를 건드리지 않는다(순수 해석 로직). 실행:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/test_account_identity.py
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from v2 import accounts, brands, session_store           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACC = accounts.ACCOUNTS_PATH

_ok = _fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✅ {name}" + (f"  — {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  ❌ {name}" + (f"  — {detail}" if detail else ""))


def digest(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.exists() else "(없음)"


def temp_accounts(rows: list[dict]) -> Path:
    """임시 accounts.json — 다른 PC 를 흉내내거나 오류 케이스를 만들 때 쓴다."""
    f = Path(tempfile.mkdtemp(prefix="acctest_")) / "accounts.json"
    f.write_text(json.dumps({"accounts": rows}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return f


print("=" * 78)
print("계정 선택 구조 검증 (login_id/blog_id 기준 · 이름·해시 비의존)")
print("=" * 78)
print(f"accounts.json : {ACC}")
before = digest(ACC)
print(f"파일 해시(전) : {before}\n")

REP = brands.resolve("repurely")
DN = brands.resolve("doctor_nuscent")

# ── 1·2. 탭 → 계정 자동 선택 ────────────────────────────────────────
print("[1·2] 기준랜딩 탭 → 계정 자동 선택")
for brand, tab, want_login, want_blog in (
        (REP, "스마일 현미 기준랜딩", "scwscw0615", "myloveeee207"),
        (REP, "행복하서연 기준랜딩", "rhksrhf6996", "hangbokhaseo7"),
        (REP, "행복하서연 대표님 기준랜딩", "shin894894@naver.com", "seoyoungene_"),
        (DN, "스마일 현미 기준랜딩", "scwscw0615", "myloveeee207"),
        (DN, "행복하서연 기준랜딩", "rhksrhf6996", "hangbokhaseo7"),
):
    acc = accounts.resolve_for("", tab, brand)
    check(f"{brand.title} · {tab}",
          acc is not None and acc.login_id == want_login and acc.blog_id == want_blog,
          f"id={getattr(acc, 'id', None)} login_id={getattr(acc, 'login_id', None)} "
          f"blog_id={getattr(acc, 'blog_id', None)}")

# ── 3. URL 에서 얻은 blog_id 대조(일치) ─────────────────────────────
print("\n[3] URL blog_id 가 등록값과 같을 때 → 통과")
acc = accounts.resolve_for("", "스마일 현미 기준랜딩", REP, blog_id="myloveeee207")
check("blog_id=myloveeee207 대조", acc is not None and acc.blog_id == "myloveeee207",
      f"id={getattr(acc, 'id', None)}")

# ── 4. URL 이 비어 blog_id 를 못 얻는 행 → login_id 로만 선택 ───────
print("\n[4] URL 이 비어 blog_id 를 못 얻을 때 → login_id 로 선택")
acc = accounts.resolve_for("", "행복하서연 기준랜딩", REP, blog_id="")
check("blog_id 없이 login_id 로 선택", acc is not None and acc.login_id == "rhksrhf6996",
      f"id={getattr(acc, 'id', None)}")

# ── 5. login_id 불일치 → 생성/rename 없이 오류 ──────────────────────
print("\n[5] login_id 가 등록돼 있지 않을 때 → 오류(자동 생성·rename 금지)")
tmp = temp_accounts([
    {"id": "someone", "label": "다른사람", "blog_id": "otherblog",
     "login_id": "other_login", "ref_tab": "다른 기준랜딩"},
])
n_before = len(json.loads(tmp.read_text(encoding="utf-8"))["accounts"])
try:
    accounts.resolve_for("", "스마일 현미 기준랜딩", REP, path=tmp)
    check("오류 발생", False, "오류 없이 통과해버렸다")
except RuntimeError as exc:
    msg = str(exc)
    check("오류 발생", True, msg.splitlines()[0])
    check("문구에 login_id 포함", "scwscw0615" in msg)
    check("문구에 '찾을 수 없습니다' 포함", "찾을 수 없습니다" in msg)
n_after = len(json.loads(tmp.read_text(encoding="utf-8"))["accounts"])
check("계정을 새로 만들지 않았다", n_before == n_after, f"{n_before}건 → {n_after}건")

# ── 6. login_id 는 맞지만 blog_id 가 다를 때 → 즉시 오류 ────────────
print("\n[6] login_id 는 맞고 blog_id 가 다를 때 → 즉시 오류")
try:
    accounts.resolve_for("", "스마일 현미 기준랜딩", REP, blog_id="hangbokhaseo7")
    check("오류 발생", False, "통과해버렸다")
except RuntimeError as exc:
    check("오류 발생", True, str(exc).splitlines()[0][:110])

# ── 7. 기존 세션 재사용 ─────────────────────────────────────────────
print("\n[7] 찾은 계정의 기존 로그인 세션을 그대로 쓴다")
for tab in ("스마일 현미 기준랜딩", "행복하서연 기준랜딩"):
    acc = accounts.resolve_for("", tab, REP)
    d = session_store.session_dir(acc)
    meta = d / "meta.json"
    m = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    same = (not m.get("blog_id")) or m["blog_id"].casefold() == acc.blog_id.casefold()
    check(f"{tab} 세션 경로·주인 일치", same,
          f"{d.name}/ · state={'O' if session_store.state_path(acc).exists() else 'X'} "
          f"· meta blog_id={m.get('blog_id') or '(없음)'}")

# ── 8. 다른 PC: id/세션폴더 이름이 전혀 달라도 같은 계정을 고른다 ───
print("\n[8] 다른 PC 흉내 — id·session 폴더 이름을 바꿔도 같은 계정 선택")
rows = json.loads(ACC.read_text(encoding="utf-8"))["accounts"]
mangled = []
for i, a in enumerate(rows):
    b = dict(a)
    b["id"] = f"pc2_random_{i}"          # ★이름을 전부 엉뚱하게 바꾼다
    mangled.append(b)
tmp2 = temp_accounts(mangled)
for tab, want_login in (("스마일 현미 기준랜딩", "scwscw0615"),
                        ("행복하서연 기준랜딩", "rhksrhf6996")):
    acc = accounts.resolve_for("", tab, REP, path=tmp2)
    check(f"{tab} → login_id 로 찾음", acc is not None and acc.login_id == want_login,
          f"id={getattr(acc, 'id', None)} (이름이 달라도 동일 계정)")

# ── 9. 세션 폴더 이름을 선택 근거로 쓰지 않는다 ─────────────────────
print("\n[9] session_id/Account.id 를 선택 근거로 쓰지 않는다")
#  한 계정의 id 를 다른 계정의 blog_id 와 같게 만든 '충돌' 상황.
#  옛 find_account 는 파일 순서로 승자가 갈렸다 → 이제는 login_id 로만 고른다.
collide = [
    {"id": "hangbokhaseo7", "label": "스마일 현미", "blog_id": "myloveeee207",
     "login_id": "scwscw0615", "ref_tab": "스마일 현미 기준랜딩"},
    {"id": "seoyeon", "label": "행복하서연", "blog_id": "hangbokhaseo7",
     "login_id": "rhksrhf6996", "ref_tab": "행복하서연 기준랜딩"},
]
tmp3 = temp_accounts(collide)
acc = accounts.resolve_for("hangbokhaseo7", "행복하서연 기준랜딩", REP, path=tmp3)
check("이름이 겹쳐도 행복하서연이 선택된다",
      acc is not None and acc.login_id == "rhksrhf6996",
      f"id={getattr(acc, 'id', None)} login_id={getattr(acc, 'login_id', None)}")
acc = accounts.resolve_for("hangbokhaseo7", "스마일 현미 기준랜딩", REP, path=tmp3)
check("같은 키를 줘도 탭의 계정(스마일 현미)이 선택된다",
      acc is not None and acc.login_id == "scwscw0615",
      f"id={getattr(acc, 'id', None)} login_id={getattr(acc, 'login_id', None)}")

# ── 10. 중복 login_id → 조용히 고르지 않고 오류 ─────────────────────
print("\n[10] 같은 login_id 가 두 계정에 등록됐을 때 → 모호 오류")
dup = [
    {"id": "a1", "label": "A", "blog_id": "blogA", "login_id": "same_login"},
    {"id": "a2", "label": "B", "blog_id": "blogB", "login_id": "same_login"},
]
tmp4 = temp_accounts(dup)
try:
    accounts.find_by_identity("same_login", "", tmp4)
    check("모호 오류 발생", False, "하나를 골라버렸다")
except RuntimeError as exc:
    check("모호 오류 발생", True, str(exc)[:100])

# ── 11. ★login_id 가 있는데 매칭 실패 → key/ref_tab 으로 절대 안 넘어간다 ──
print(f"{chr(10)}[11] login_id 있음 + 등록 계정 없음 → fallback 없이 즉시 오류")
#  ref_tab 과 key 로는 '찾을 수 있는' 상태를 일부러 만든다.
#  (login_id 만 다르다 → 정책 A 라면 오류여야 하고, fallback 이 살아 있으면 통과해버린다)
trap = [
    {"id": "smile_hyunmi", "label": "스마일 현미", "blog_id": "myloveeee207",
     "login_id": "WRONG_LOGIN", "ref_tab": "스마일 현미 기준랜딩", "brand": ""},
]
tmp5 = temp_accounts(trap)
#  ref_tab 만으로는 잡힌다는 것을 먼저 확인(= 덫이 제대로 놓였다)
bait = accounts.find_by_tab("스마일 현미 기준랜딩", REP, tmp5)
check("덫 확인 — ref_tab 으로는 찾을 수 있는 상태", bait is not None,
      f"ref_tab 매칭={getattr(bait, 'id', None)}")
check("덫 확인 — key 로도 찾을 수 있는 상태",
      accounts.find_exact("smile_hyunmi", tmp5) is not None)
try:
    got = accounts.resolve_for("smile_hyunmi", "스마일 현미 기준랜딩", REP, path=tmp5)
    check("fallback 없이 오류", False,
          f"❗fallback 이 일어나 {getattr(got, 'id', None)} 를 선택해버렸다")
except RuntimeError as exc:
    msg = str(exc)
    check("fallback 없이 오류", True, msg.splitlines()[0])
    check("문구에 '추측하지 않습니다' 포함", "추측하지 않습니다" in msg)

# ── 12. login_id 없음(brands.json 에 계정 없는 탭) → 호환 경로로 정상 선택 ──
print(f"{chr(10)}[12] login_id 없음 + 등록 ref_tab → 호환 경로로 정상 선택")
legacy = [
    {"id": "tab_legacy", "label": "민지 테스트", "blog_id": "iammeanji",
     "login_id": "", "ref_tab": "민지  테스트 계정 기준랜딩", "brand": "repurely"},
]
tmp6 = temp_accounts(legacy)
acc = accounts.resolve_for("", "민지  테스트 계정 기준랜딩", REP, path=tmp6)
check("ref_tab 호환 경로로 선택", acc is not None and acc.id == "tab_legacy",
      f"id={getattr(acc, 'id', None)} 근거={accounts.last_basis()}")
check("근거에 'login_id 없음' 표시", "login_id 없음" in accounts.last_basis())

# ── 13. create=False 는 오류 대신 None (찾기만) ─────────────────────
print(f"{chr(10)}[13] create=False 는 예외 없이 None")
got = accounts.resolve_for("", "스마일 현미 기준랜딩", REP, create=False, path=tmp5)
check("None 반환", got is None, f"결과={got}")

# ── 마무리: 실제 파일이 변하지 않았는지 ─────────────────────────────
after = digest(ACC)
print(f"\n파일 해시(후) : {after}")
check("accounts.json 을 건드리지 않았다", before == after)

print("\n" + "=" * 78)
print(f"결과: 성공 {_ok} · 실패 {_fail}")
print("=" * 78)
sys.exit(1 if _fail else 0)
