r"""운영 계정 현황 보고 — **읽기 전용**. 아무것도 바꾸지 않는다.

    .\.venv\Scripts\python.exe scripts\report_accounts_readonly.py

읽는 것(쓰기·수정 없음)
    ① %APPDATA%\BlogLandingAgent\accounts_cache.json  ← Supabase 마지막 동기화 사본
    ② %APPDATA%\BlogLandingAgent\brands.json          ← 탭 → 계정 매핑(화면·PC 공용)
    ③ %APPDATA%\BlogLandingAgent\sessions\            ← 세션 폴더 현황(쿠키 수·시각만)
    ④ supabase\schema_accounts.sql / add_account_*.sql  ← 적재 기록

하는 일
    · 계정별 비교표(브랜드·표시명·id·login_id·blog_id·ref_tab·세션·활성)
    · 같은 blog_id 를 쓰는 계정 묶음
    · **실제 계정 선택 경로 재현** — 각 (브랜드, 기준랜딩 탭) 으로 `accounts.resolve_for`
      를 돌려 어떤 계정이 선택되는지, A경로(login_id)인지 B경로(brand+ref_tab)인지
    · login_id 가 비어 있어 화면 로그아웃이 잠기던 계정(2026-11-03 보완 전 기준)

★추측으로 login_id 를 채우지 않는다. blog_id 가 같다는 이유로 같은 로그인 계정이라고
  판단하지 않는다. 확인 못 한 항목은 '미확인' 으로 적는다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

APPDATA = Path(os.getenv("APPDATA") or "") / "BlogLandingAgent"
CACHE = APPDATA / "accounts_cache.json"
BRANDS = APPDATA / "brands.json"
SESSIONS = APPDATA / "sessions"


def line(ch: str = "=", n: int = 100) -> None:
    print(ch * n)


def load_cache() -> tuple[list[dict], str]:
    if not CACHE.exists():
        return [], ""
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return [], ""
    return list(d.get("accounts") or []), str(d.get("fetched_at") or "")


def session_info(ident: str) -> dict:
    d = SESSIONS / ident
    if not d.is_dir():
        return {"folder": False, "cookies": 0, "saved": "", "profile": False}
    st = {}
    if (d / "state.json").exists():
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            st = {}
    return {"folder": True, "cookies": len(st.get("cookies") or []),
            "saved": (st.get("saved_at") or "")[:16],
            "blog_id": st.get("blog_id") or "",
            "profile": (d / "profile").is_dir()}


def main() -> int:
    rows, fetched = load_cache()
    line()
    print(" 운영 계정 현황 (읽기 전용)")
    line()
    print(f"  · 계정 목록 출처 : {CACHE}")
    print(f"    마지막 동기화  : {fetched or '(없음)'}   ← 이 시점 이후 변경은 **미확인**")
    print(f"  · 세션 폴더      : {SESSIONS}")
    if not rows:
        print("  [!] accounts_cache.json 을 읽지 못했습니다 — 보고할 수 없습니다.")
        return 2

    # ── 캐시 이후 추가된 계정: 적재 SQL 로만 확인된다 ─────────────────
    #   `supabase/add_account_<id>.sql` 이 있으면 그 내용을 읽어 함께 보여 준다.
    #   ★파일이 없으면 그냥 건너뛴다(저장소에는 운영 계정 데이터를 두지 않는다).
    tax_sql = next(iter(sorted(
        (ROOT / "supabase").glob("add_account_*.sql"))), None)
    tax_row = None
    if tax_sql is not None:
        t = tax_sql.read_text(encoding="utf-8")
        m = re.search(r"values\s*\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*"
                      r"'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*(\w+)\)", t)
        if m:
            tax_row = {"id": m[1], "label": m[2], "login_id": m[3],
                       "blog_id": m[4], "brand": m[5], "ref_tab": m[6],
                       "active": m[7] == "true", "_src": tax_sql.name}

    print("")
    line("-")
    print(f"{'브랜드':<16}{'표시명':<22}{'account.id':<17}{'login_id':<24}"
          f"{'blog_id':<16}{'활성':<6}")
    line("-")
    allrows = list(rows) + ([tax_row] if tax_row else [])
    for r in allrows:
        brand = r.get("brand") or "(기본=리퓨어리)"
        act = r.get("active", r.get("enabled", True))
        print(f"{brand:<16}{(r.get('label') or ''):<22}{(r.get('id') or ''):<17}"
              f"{(r.get('login_id') or '(빈 문자열)'):<24}"
              f"{(r.get('blog_id') or '(빈값)'):<16}"
              f"{('O' if act else 'X'):<6}"
              + ("   ★캐시 이후 추가 — 운영 DB 반영 미확인"
                 if r.get("_src") else ""))

    n_all = len(allrows)
    n_has = sum(1 for r in allrows if (r.get("login_id") or "").strip())
    print("")
    print(f"  전체 {n_all}건 · login_id 등록 {n_has}건 · "
          f"login_id 빈 문자열 {n_all - n_has}건")
    print("  ※ `blog_accounts.login_id` 는 `not null default ''` 이므로 "
          "**NULL 은 존재할 수 없다**(빈 문자열만 있다). — schema_accounts.sql")

    # ── 세션 폴더 ───────────────────────────────────────────────────
    print("")
    line("-")
    print(f"{'account.id':<17}{'ref_tab':<34}{'세션폴더':<10}{'쿠키':<7}"
          f"{'마지막 저장':<18}{'프로필'}")
    line("-")
    for r in allrows:
        ident = r.get("id") or ""
        s = session_info(ident)
        print(f"{ident:<17}{(r.get('ref_tab') or '(없음)'):<34}"
              f"{('있음' if s['folder'] else '없음'):<10}"
              f"{(str(s['cookies']) + '개' if s['folder'] else '-'):<7}"
              f"{(s['saved'] or '-'):<18}"
              f"{('있음' if s['profile'] else '없음')}")
    extra = sorted(d.name for d in (SESSIONS.iterdir() if SESSIONS.is_dir() else [])
                   if d.is_dir() and d.name
                   not in {r.get("id") for r in allrows})
    if extra:
        print("")
        print(f"  ★계정 목록에 없는 세션 폴더: {extra}  ← 고아 폴더(쓰이지 않음)")

    # ── 같은 blog_id 묶음 ──────────────────────────────────────────
    print("")
    line("-")
    print(" 같은 blog_id 를 쓰는 계정 (★같은 로그인 계정이라는 뜻은 아니다)")
    line("-")
    groups: dict[str, list[dict]] = {}
    for r in allrows:
        b = (r.get("blog_id") or "").strip().lower()
        if b:
            groups.setdefault(b, []).append(r)
    for b, g in sorted(groups.items()):
        if len(g) < 2:
            continue
        print(f"  {b} — {len(g)}건")
        for r in g:
            print(f"      {r.get('id'):<17} login_id={(r.get('login_id') or '(빈값)'):<24}"
                  f" brand={(r.get('brand') or '기본')}")
        lids = {(r.get("login_id") or "").strip() for r in g} - {""}
        print(f"      → 등록된 login_id {len(lids)}종{(' ' + str(sorted(lids))) if lids else ''}"
              f" · 나머지는 빈 문자열 → **같은 로그인 계정인지 확인 불가**")

    # ── 계정 선택 경로 재현 ────────────────────────────────────────
    print("")
    line("-")
    print(" 계정 선택 경로 재현 — (브랜드, 기준랜딩 탭) → 실제로 선택되는 계정")
    line("-")
    sand = Path(tempfile.mkdtemp(prefix="blog_report_"))
    os.environ["BLOG_DATA_DIR"] = str(sand)
    os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
    acc_path = sand / "accounts.json"
    acc_path.write_text(json.dumps(
        {"accounts": [{k: v for k, v in r.items() if not k.startswith("_")}
                      for r in allrows]}, ensure_ascii=False), encoding="utf-8")
    if BRANDS.exists():
        (sand / "brands.json").write_text(BRANDS.read_text(encoding="utf-8"),
                                          encoding="utf-8")

    from v2 import accounts as A                               # noqa: N811
    from v2 import brands as B                                 # noqa: N811
    A.ACCOUNTS_PATH = acc_path
    A.refresh()

    want = sorted({((r.get("brand") or ""), (r.get("ref_tab") or ""))
                   for r in allrows if (r.get("ref_tab") or "").strip()})
    print(f"{'브랜드':<16}{'기준랜딩 탭':<34}{'선택된 계정':<17}{'경로':<8}근거")
    for brand, tab in want:
        try:
            acc = A.resolve_for("", tab, brand or None, create=False,
                                path=acc_path)
        except Exception as exc:                               # noqa: BLE001
            print(f"{(brand or '기본'):<16}{tab:<34}{'(오류)':<17}{'-':<8}"
                  f"{type(exc).__name__}: {str(exc).splitlines()[0][:40]}")
            continue
        basis = A.last_basis()
        path_kind = "A(login_id)" if basis.startswith("login_id") else (
            "B(ref_tab)" if basis else "-")
        print(f"{(brand or '기본'):<16}{tab:<34}"
              f"{(getattr(acc, 'id', None) or '(없음)'):<17}{path_kind:<8}"
              f"{basis[:46]}")

    # ── 화면 로그아웃이 잠기던 계정 ────────────────────────────────
    print("")
    line("-")
    print(" login_id 가 비어 있던 계정 (2026-11-03 보완 전: 화면 로그아웃 잠김)")
    line("-")
    for r in allrows:
        if (r.get("login_id") or "").strip():
            continue
        s = session_info(r.get("id") or "")
        act = r.get("active", r.get("enabled", True))
        print(f"  {r.get('id'):<17} {(r.get('label') or ''):<22} "
              f"활성={'O' if act else 'X'} · 세션="
              f"{'쿠키 ' + str(s['cookies']) + '개(' + (s['saved'] or '?') + ')'
                 if s['folder'] and s['cookies'] else '없음'}")
    print("")
    print("  → 2026-11-03 보완 후: 위 계정도 **account.id 기준**으로 화면에서 "
          "로그아웃할 수 있다.")
    print("")
    line()
    print(" 끝 — 읽기 전용. 계정 데이터·세션 매핑을 바꾸지 않았습니다.")
    line()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
