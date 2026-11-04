r"""운영 계정 전체 점검 — **읽기 전용**. 아무것도 바꾸지 않는다.

    .\.venv\Scripts\python.exe scripts\audit_accounts_today.py

하는 일
    ① Supabase `accounts_list` RPC 로 **현재** 계정 목록을 받는다(읽기 전용)
    ② 기준시트의 실제 기준랜딩 탭 목록을 받는다(읽기 전용)
    ③ 각 (브랜드, 기준랜딩 탭) → `accounts.resolve_for` 가 **어느 account.id** 를
       고르는지 재현하고 선택 경로(A=login_id / B=brand+ref_tab)를 적는다
    ④ 계정별 세션 폴더(쿠키 수·저장 시각·저장된 blog_id)를 **직접 경로로** 읽는다
    ⑤ 위험 신호를 표시한다
         · 등록 blog_id 가 비었다 / account.id 나 login_id 와 같다(잘못 들어간 값)
         · 등록 blog_id ≠ 세션에 저장된 blog_id
         · 같은 blog_id 를 **다른 login_id** 가 공유한다
         · 시트에 없는 ref_tab(죽은 등록) / 계정이 없는 탭

★안 하는 것: 쿠키·토큰 값 출력, 세션 파일 쓰기, 계정 데이터 변경, 캐시 갱신,
  브라우저 실행, 네이버 접속. 계정 목록은 `path=` 로 임시 파일만 읽게 해
  `accounts_cache.json` 을 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
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
SESSIONS = APPDATA / "sessions"
DEVICE = APPDATA / "device.json"
BRANDS = APPDATA / "brands.json"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass


def line(ch: str = "=", n: int = 112) -> None:
    print(ch * n)


# ── ① 계정 목록 (Supabase RPC · 읽기 전용) ─────────────────────────
def fetch_accounts() -> tuple[list[dict], str]:
    if not DEVICE.exists():
        return [], "device.json 없음"
    try:
        token = json.loads(DEVICE.read_text(encoding="utf-8")).get("device_token")
    except Exception as exc:                                   # noqa: BLE001
        return [], f"device.json 읽기 실패({type(exc).__name__})"
    if not token:
        return [], "device_token 없음"
    cfg = {}
    if (APPDATA / "agent_config.json").exists():
        cfg = json.loads((APPDATA / "agent_config.json").read_text(encoding="utf-8"))
    url = cfg.get("supabase_url") or os.getenv("SUPABASE_URL") or ""
    key = cfg.get("supabase_publishable_key") or os.getenv(
        "SUPABASE_PUBLISHABLE_KEY") or ""
    if not url or not key:
        return [], "접속 정보 없음"
    try:
        from v2.supabase_store import SupabaseStore
        rows = SupabaseStore(mode="device", url=url, key=key,
                             device_token=token).accounts_list()
    except Exception as exc:                                   # noqa: BLE001
        return [], f"RPC 실패({type(exc).__name__}: {exc})"[:120]
    out = []
    for r in rows or []:
        d = dict(r)
        d["enabled"] = bool(d.pop("active", True))
        out.append(d)
    return out, f"Supabase accounts_list RPC ({len(out)}건)"


# ── ④ 세션 폴더 (직접 경로 · 읽기만) ──────────────────────────────
def session_of(ident: str) -> dict:
    d = SESSIONS / ident
    if not d.is_dir():
        return {"folder": False, "cookies": 0, "saved": "", "blog": "",
                "profile": False}
    st = {}
    if (d / "state.json").exists():
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            st = {}
    return {"folder": True, "cookies": len(st.get("cookies") or []),
            "saved": (st.get("saved_at") or "")[:16],
            "blog": st.get("blog_id") or "",
            "profile": (d / "profile").is_dir()}


def main() -> int:
    rows, src = fetch_accounts()
    line()
    print(" 운영 계정 전체 점검 (읽기 전용 · 변경 없음)")
    line()
    print(f"  계정 목록 출처 : {src}")
    print(f"  세션 폴더      : {SESSIONS}")
    if not rows:
        print("  [!] 계정 목록을 받지 못했습니다 — 아래 점검을 할 수 없습니다.")
        return 2

    # ── 기준시트 실제 탭 ──────────────────────────────────────────
    if BRANDS.exists():
        os.environ.setdefault("BLOG_BRANDS_JSON", BRANDS.read_text(encoding="utf-8"))
    from v2 import accounts as A
    from v2 import brands as B
    from v2 import catalog

    sheet: dict[str, list[str]] = {}
    for bid in [b.id for b in B.load_brands(include_disabled=True)]:
        try:
            sheet[bid] = list(catalog.load_tabs(bid)["tabs"])
        except Exception as exc:                               # noqa: BLE001
            sheet[bid] = []
            print(f"  [!] {bid} 기준시트 탭 조회 실패({type(exc).__name__})")
    print(f"  기준시트 탭    : " + " · ".join(
        f"{k} {len(v)}개" for k, v in sheet.items()))

    #   계정 목록을 임시 파일로만 읽게 한다(운영 캐시를 건드리지 않는다)
    tmp = Path(tempfile.mkdtemp(prefix="blog_audit_")) / "accounts.json"
    tmp.write_text(json.dumps({"accounts": rows}, ensure_ascii=False),
                   encoding="utf-8")

    # ══ 표 1 — 계정별 현황 ════════════════════════════════════════
    print("")
    line("-")
    print(f"{'account.id':<17}{'login_id':<16}{'등록 blog_id':<17}{'브랜드':<16}"
          f"{'세션':<16}{'활성':<5}{'위험 신호'}")
    line("-")
    by_blog: dict[str, list[dict]] = {}
    risky: list[tuple[str, str]] = []
    for r in sorted(rows, key=lambda x: str(x.get("id") or "")):
        ident = str(r.get("id") or "")
        lid = (r.get("login_id") or "").strip()
        bid = (r.get("blog_id") or "").strip()
        brand = (r.get("brand") or "") or "(기본)"
        s = session_of(ident)
        sess = (f"쿠키{s['cookies']} {s['saved'][5:]}" if s["cookies"]
                else ("폴더만" if s["folder"] else "없음"))
        warn = []
        if not bid:
            warn.append("blog_id 비었음")
        elif bid == ident:
            warn.append("blog_id=account.id")
        elif lid and bid.casefold() == lid.casefold():
            warn.append("blog_id=login_id ★")
        if s["blog"] and bid and s["blog"].casefold() != bid.casefold():
            warn.append(f"세션 blog_id={s['blog']} 불일치 ★")
        if bid:
            by_blog.setdefault(bid.casefold(), []).append(
                {"id": ident, "lid": lid, "brand": brand})
        if warn:
            risky.append((ident, " · ".join(warn)))
        print(f"{ident:<17}{(lid or '(없음)'):<16}{(bid or '(빈값)'):<17}"
              f"{brand:<16}{sess:<16}{('O' if r.get('enabled') else 'X'):<5}"
              f"{' · '.join(warn)}")

    n_sess = sum(1 for r in rows if session_of(str(r.get("id") or ""))["cookies"])
    n_lid = sum(1 for r in rows if (r.get("login_id") or "").strip())
    print("")
    print(f"  전체 {len(rows)}개 · login_id 있음 {n_lid} / 없음 {len(rows) - n_lid}"
          f" · 저장 세션 있음 {n_sess}개")

    # ══ 표 2 — 탭 → 계정 해석 ═════════════════════════════════════
    print("")
    line("-")
    print(" 기준랜딩 탭 → 실제로 선택되는 계정 (accounts.resolve_for 재현)")
    line("-")
    print(f"{'브랜드':<16}{'기준랜딩 탭':<34}{'선택 계정':<17}{'경로':<13}{'세션'}")
    unresolved: list[str] = []
    for bid_brand, tabs in sheet.items():
        for tab in tabs:
            try:
                acc = A.resolve_for("", tab, bid_brand, create=False, path=tmp)
            except Exception as exc:                           # noqa: BLE001
                print(f"{bid_brand:<16}{tab:<34}{'(오류)':<17}{'-':<13}"
                      f"{type(exc).__name__}")
                unresolved.append(f"{bid_brand}/{tab}")
                continue
            basis = A.last_basis()
            path_kind = ("A(login_id)" if basis.startswith("login_id")
                         else ("B(ref_tab)" if basis else "-"))
            if acc is None:
                print(f"{bid_brand:<16}{tab:<34}{'★계정 없음':<17}{'-':<13}")
                unresolved.append(f"{bid_brand}/{tab}")
                continue
            s = session_of(acc.id)
            sess = (f"쿠키{s['cookies']}" if s["cookies"]
                    else ("폴더만" if s["folder"] else "없음 → 로그인 필요"))
            print(f"{bid_brand:<16}{tab:<34}{acc.id:<17}{path_kind:<13}{sess}")

    # ══ 표 3 — 같은 blog_id 공유 ══════════════════════════════════
    print("")
    line("-")
    print(" 같은 blog_id 를 쓰는 계정 (★같은 로그인 계정이라는 뜻은 아니다)")
    line("-")
    for blog, grp in sorted(by_blog.items()):
        if len(grp) < 2:
            continue
        lids = {g["lid"] for g in grp if g["lid"]}
        print(f"  {blog} — {len(grp)}개 · 등록 login_id {len(lids)}종")
        for g in grp:
            s = session_of(g["id"])
            print(f"      {g['id']:<17} login_id={(g['lid'] or '(없음)'):<16}"
                  f" brand={g['brand']:<16}"
                  f" 세션={'쿠키' + str(s['cookies']) if s['cookies'] else '없음'}")
        print("      → 세션 폴더가 서로 다르므로 쿠키는 분리돼 있습니다"
              "(공유·복사 없음)")

    # ══ 표 4 — 죽은 등록 / 위험 신호 ══════════════════════════════
    print("")
    line("-")
    print(" 시트에 없는 ref_tab(죽은 등록) · 위험 신호")
    line("-")
    all_tabs = {(k, t) for k, v in sheet.items() for t in v}
    for r in sorted(rows, key=lambda x: str(x.get("id") or "")):
        tab = (r.get("ref_tab") or "").strip()
        if not tab:
            continue
        brand = (r.get("brand") or "").strip() or B.DEFAULT_BRAND_ID
        if (brand, tab) not in all_tabs:
            print(f"  {str(r.get('id')):<17} brand={brand:<16} ref_tab={tab!r}"
                  f"  ← 시트에 없음")
    if unresolved:
        print(f"  ★계정이 해석되지 않는 탭: {unresolved}")
    for ident, why in risky:
        print(f"  {ident:<17} {why}")
    if not risky:
        print("  (위험 신호 없음)")

    print("")
    line()
    print(" 끝 — 읽기 전용. 계정 데이터·세션·캐시를 바꾸지 않았습니다.")
    print(" ★세션 파일이 있다 = '쿠키가 저장돼 있다' 일 뿐, 네이버 로그인이 아직")
    print("   살아 있다는 보장은 아닙니다(만료 가능). 실제 확인은 [실행 준비] 입니다.")
    line()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
