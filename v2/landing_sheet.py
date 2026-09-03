r"""발행한 블로그 URL 을 `랜딩` 탭 **K열(블로그 링크)** 에 기록한다.

시트 실측(2026-08-21)
  · `랜딩` 탭 — 헤더 2행 / 데이터 3행~
    B=매체  C=포맷  D=제품_결핍  E=날짜  F=순번  G~I=utm  J=링크  **K=블로그 링크**  L=…
  · 3~7행 = GFA / 821 / 올레놀샷 · 8~12행 = 카카오모먼트 / 821 / 레모니티_흑자

★예전 `BlogLinkWriter` 는 **매체를 보지 않고** K열 빈 셀을 위에서부터 채웠다.
  그러면 카모 글이 GFA 행(3~7)에 들어간다. 여기서는 **매체 + 날짜가 맞는 행에만** 쓴다.
★값이 있는 셀은 절대 덮어쓰지 않는다.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import brands, sheets

SHEET_NAME = "랜딩"          # (구) 테스트 시트 탭 — 실전 기록에는 더 이상 쓰지 않는다

# ── UTM 빌더 시트 ─────────────────────────────────────────────────────
#   ★시트 ID 는 **브랜드마다 다르다**(2026-08-31). `v2/brands.py` / `brands.json` 한 곳에서만
#     관리하고, 아래 상수는 브랜드를 고르지 않은 실행(=기존 CLI)의 기본값(리퓨어리)이다.
#   `리퓨어리 UTM 빌더` — 매체별로 탭이 따로 있다. 컬럼 구성은 (구) `랜딩` 탭과 같고
#   헤더가 1행에 있을 뿐이라, **행 매칭/열 찾기 규칙은 그대로** 두고 시트·탭만 바꾼다.
PRODUCTION_SHEET_ID = brands.default_brand().utm_sheet_id
# ★구글애즈 추가(2026-09-02): UTM 빌더 시트에는 `구글애즈 블로그 랜딩 UTM 빌더` 탭이
#   이미 있었는데 코드에만 매체 등록이 빠져 있어 `매체 '구글애즈'를 알 수 없습니다` 로 멈췄다.
MEDIA = ("GFA", "카카오모먼트", "메타", "틱톡", "구글애즈")
MEDIA_TABS = {m: brands.default_brand().utm_tab(m) for m in MEDIA}

_ACTIVE_BRAND: "brands.Brand | None" = None


def set_brand(brand) -> "brands.Brand":
    """이번 실행에서 쓸 브랜드를 지정한다(UTM 빌더 시트가 통째로 바뀐다)."""
    global _ACTIVE_BRAND
    _ACTIVE_BRAND = brands.resolve(brand)
    return _ACTIVE_BRAND


def active_brand() -> "brands.Brand":
    """지금 쓰는 브랜드. 지정하지 않았으면 기본 브랜드(리퓨어리) = 기존 동작."""
    return _ACTIVE_BRAND or brands.default_brand()


def active_sheet_id() -> str:
    return active_brand().utm_sheet_id or PRODUCTION_SHEET_ID

HEADER_LINK = "블로그 링크"
HEADER_MEDIA = "매체"
HEADER_DATE = "날짜"
HEADER_SEQ = "순번"
HEADER_PRODUCT = "링크"        # I열 — 행마다 UTM 이 다른 실전 제품 링크
HEADER_CAMPAIGN = "utm_campaign"
HEADER_PRODUCT_DEF = "제품_결핍"   # C열 — 같은 날짜에 그룹이 여럿일 때 구분용
HEADER_PRODUCTION = "실전용 블로그 링크"
HEADER_CHANGED = "블로그 최하단 제품 링크 변경"

# ★컬럼명이 다른 브랜드는 `brands.json` 의 headers 로 맞춘다(브랜드별 if 문을 늘리지 않는다).
#   비워 두면 아래 기본값(리퓨어리 실측)을 그대로 쓴다.
DEFAULT_HEADERS = {
    "link": HEADER_LINK, "media": HEADER_MEDIA, "date": HEADER_DATE,
    "seq": HEADER_SEQ, "product": HEADER_PRODUCT, "campaign": HEADER_CAMPAIGN,
    "product_def": HEADER_PRODUCT_DEF, "production": HEADER_PRODUCTION,
    "changed": HEADER_CHANGED,
}


def H(key: str) -> str:
    """지금 브랜드의 컬럼 이름."""
    return active_brand().header(key, DEFAULT_HEADERS[key])

# `랜딩` 탭 매체 표기 ← 사용자가 뭐라고 부르든 맞춰준다.
MEDIA_ALIASES = {
    "카카오모먼트": {"카카오모먼트", "카모", "카카오", "kakao", "kakaomoment"},
    "GFA": {"gfa", "지에프에이", "네이버gfa", "네이버"},
    "메타": {"메타", "meta", "페북", "페이스북", "facebook"},
    "틱톡": {"틱톡", "tiktok"},
    "구글애즈": {"구글애즈", "구글", "google", "googleads", "google ads", "gads", "ga"},
}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).strip()


def canonical_media(value: str) -> str:
    v = norm(value).casefold()
    for canon, names in MEDIA_ALIASES.items():
        if v == norm(canon).casefold() or v in {norm(n).casefold() for n in names}:
            return canon
    return (value or "").strip()


def resolve_target(media: str, log=None) -> dict:
    """매체 → **선택한 브랜드의** UTM 빌더 시트/탭.

    `카모`·`카카오`·`kakao` 는 모두 카카오모먼트로 본다. 탭 이름은 브랜드 설정의
    `utm_media_tabs`(직접 지정) → `utm_tab_pattern` 순으로 정해진다.
    """
    brand = active_brand()
    canon = canonical_media(media)
    if canon not in MEDIA and canon not in (brand.utm_media_tabs or {}):
        raise RuntimeError(f"[UTM 빌더] 매체 {media!r}(→{canon!r})를 알 수 없습니다. "
                           f"쓸 수 있는 매체: {list(MEDIA)}")
    tab = brand.utm_tab(canon)
    if log:
        log(f"[UTM 빌더] brand={brand.id}({brand.title}) · {brand.utm_title}")
        log(f"[실전 시트] media={media} → {canon}")
        log(f"[실전 시트] 대상 탭={tab}")
    return {"sheet_id": active_sheet_id(), "tab": tab, "media": canon,
            "brand": brand.id}


def _open(cred: Path, sheet_id: str, readonly: bool, tab: str = ""):
    import gspread
    from google.oauth2.service_account import Credentials

    scope = ("https://www.googleapis.com/auth/spreadsheets.readonly" if readonly
             else "https://www.googleapis.com/auth/spreadsheets")
    brand = active_brand()
    creds = Credentials.from_service_account_file(str(cred), scopes=[scope])
    # ★어떤 sheet_id 가 들어오든 **선택한 브랜드의 UTM 빌더**만 연다(브랜드 혼용 방지).
    target = active_sheet_id()
    try:
        client = gspread.authorize(creds)
        book = sheets.open_with_retry(lambda: client.open_by_key(target))
    except Exception as exc:                                   # noqa: BLE001
        raise sheets.access_error(brand, "utm_sheet_access", brand.utm_title,
                                  target, cred, exc) from exc
    want = (tab or SHEET_NAME).strip()
    for ws in book.worksheets():
        if ws.title.strip() == want:
            return ws
    raise RuntimeError(
        f"[UTM 빌더] brand={brand.id} stage=utm_tab status=failed reason=tab_not_found\n"
        f"       `{brand.utm_title}` 에 `{want}` 탭이 없습니다: "
        f"{[w.title for w in book.worksheets()]}\n"
        f"       → 탭 이름이 다르면 brands.json 의 utm_tab_pattern / utm_media_tabs 로 맞추세요.")


def _cols(rows: list[list[str]]) -> tuple[int, dict]:
    """헤더 행 번호(1-base)와 {이름: 열 index} 를 찾는다."""
    for i in range(min(5, len(rows))):
        header = [norm(c) for c in rows[i]]
        if norm(H("link")) in header:
            idx = {}
            for name in (H("link"), H("media"), H("date"), H("seq"),
                         H("changed")):
                idx[name] = header.index(norm(name)) if norm(name) in header else -1
            return i + 1, idx
    raise RuntimeError(f"`{H("link")}` 열을 찾지 못했습니다")


def _campaign_col(header_cells: list[str]) -> int:
    """utm_campaign 열 index(없으면 -1)."""
    hdr = [norm(c) for c in header_cells]
    want = norm(H("campaign"))
    return hdr.index(want) if want in hdr else -1


def _product_col(header_cells: list[str]) -> int:
    """제품_결핍(C) 열 index(없으면 -1)."""
    hdr = [norm(c) for c in header_cells]
    want = norm(H("product_def"))
    return hdr.index(want) if want in hdr else -1


def _product_ok(value: str, product: str) -> bool:
    """C열 제품_결핍 대조. 결핍 키워드(흑자)와 시트 표기(레모니티_흑자)가 다르므로
    **양방향 부분일치**로 본다 — '흑자' ⊂ '레모니티_흑자' 면 통과."""
    if not product:
        return True
    v, p = norm(value), norm(product)
    if not v:
        return False
    return p in v or v in p


def _campaign_ok(value: str, campaign: str) -> bool:
    """★같은 날짜에 그룹이 여럿일 때(0826 = 올레놀샷_목주름 30행 + 레모니티_흑자 20행)
    매체+날짜만 보면 앞 그룹의 빈칸에 뒷 그룹 랜딩이 들어간다(2026-08-26).
    campaign 접두사(예: g_i_b_o_l_0831)를 주면 그 그룹 행만 대상으로 삼는다.

    **매칭 규칙 — `_` 단위(순번 자릿수와 무관)**
      접두사 `g_i_b_o_l_0831`
        ✔ g_i_b_o_l_0831        (접두사와 완전히 같은 값)
        ✔ g_i_b_o_l_0831_1 … _9 … _20 … _100   ← 순번이 몇 자리든 전부
        ✘ g_i_b_o_l_08319       (숫자가 이어 붙은 다른 그룹)
        ✘ g_i_b_o_l_0831x_1
      접두사에 순번까지 적으면(`…_0831_1`) `_1` 만 잡고 `_10`·`_19` 는 잡지 않는다.
    ★`camp.startswith(campaign)` 로 하면 `…_1` 접두사가 `…_10` 까지 잡아 버린다.
      그래서 반드시 이 함수 하나만 쓴다(조회·기록 경로 전부 동일 규칙).
    """
    if not campaign:
        return True
    v, c = norm(value), norm(campaign)
    return v == c or v.startswith(c + "_")


_URL_RE = re.compile(r"^https?://", re.I)


def is_url(v: str) -> bool:
    return bool(_URL_RE.match((v or "").strip()))


def check_product_urls(rows: list[dict], log, on_missing: str = "raise") -> list[dict]:
    """행별 제품 링크(UTM 빌더의 `링크` 열)를 검사한다.

    · on_missing="raise" (기본, 기존 동작) — 하나라도 비면 예외
    · on_missing="drop"                    — 비었거나 URL 이 아닌 행만 빼고 진행
                                             (한 건 때문에 나머지가 막히지 않게)
    """
    good, bad = [], []
    for r in rows:
        url = (r.get("product_url") or "").strip()
        if not url:
            bad.append((r, "product_url_missing"))
        elif not is_url(url):
            bad.append((r, "product_url_invalid"))
        else:
            good.append(r)
    if not bad:
        return rows
    if on_missing != "drop":
        raise RuntimeError(f"[시트] {[r['row'] for r, _ in bad]} 행의 "
                           f"`{H('product')}`(제품 링크)가 비었거나 주소가 아닙니다")
    for r, why in bad:
        log(f"[시트] ⚠ {r['row']}행 제외 — {why} "
            f"(`{H('product')}` = {(r.get('product_url') or '')[:60]!r})")
        emit = getattr(log, "event", None)
        if callable(emit):
            emit("post_failed", row=r["row"], stage="product_url_lookup",
                 status="failed", reason=why)
    return good


def find_target_rows(cred: Path, sheet_id: str, media: str, date: str, log,
                     need: int = 0, campaign: str = "", product: str = "") -> list[int]:
    """매체 + 날짜(+캠페인 접두사)가 맞고 블로그 링크 열이 비어 있는 행 번호 목록(위에서부터)."""
    tgt = resolve_target(media, log)
    ws = _open(cred, tgt["sheet_id"], readonly=True, tab=tgt["tab"])
    rows = ws.get_all_values()
    header_row, idx = _cols(rows)
    want_media, want_date = tgt["media"], norm(date)
    j_camp = _campaign_col(rows[header_row - 1])
    j_prod = _product_col(rows[header_row - 1])

    def cell(r: list[str], j: int) -> str:
        return (r[j].strip() if 0 <= j < len(r) else "")

    hits, filled = [], []
    for n, raw in enumerate(rows[header_row:], start=header_row + 1):
        if canonical_media(cell(raw, idx[H("media")])) != want_media:
            continue
        if norm(cell(raw, idx[H("date")])) != want_date:
            continue
        if not _product_ok(cell(raw, j_prod) if j_prod >= 0 else "", product):
            continue
        if not _campaign_ok(cell(raw, j_camp) if j_camp >= 0 else "", campaign):
            continue
        if cell(raw, idx[H("link")]):
            filled.append(n)
            continue
        hits.append(n)

    col_letter = chr(65 + idx[H("link")])
    log(f"[시트] `{tgt['tab']}` 탭 · 헤더 {header_row}행 · {H("link")} = {col_letter}열")
    log(f"[시트] 매체 {want_media} / 날짜 {want_date}"
        f"{' / 제품_결핍 ~' + product if product else ''}"
        f"{' / 캠페인 ' + campaign if campaign else ''} — 빈 행 {hits} "
        f"(이미 채워진 행 {filled or '없음'})")
    # ★자리가 모자라면 **발행 전에 멈춘다**. 경고만 하고 진행하면 기록 못 한 글이
    #   블로그에만 남는다(2026-08-21: 5행짜리 시트에 13건을 돌릴 뻔했다).
    if need and len(hits) < need:
        raise RuntimeError(
            f"[시트] 기록할 자리가 {len(hits)}개뿐인데 {need}건을 발행하려 합니다.\n"
            f"       `{SHEET_NAME}` 탭에 매체={want_media} / 날짜={want_date} 행을 "
            f"{need - len(hits)}개 더 만들거나 --count 를 {len(hits)} 이하로 낮추세요.\n"
            f"       (빈 행 {hits} · 이미 채워진 행 {filled or '없음'})")

    # ★쓰기 권한을 **미리** 확인한다. 5건 발행해 놓고 마지막에 권한 없음으로 실패하면 곤란하다.
    #   확인은 '이미 비어 있는 셀에 빈 값 쓰기' — 시트 내용은 바뀌지 않는다.
    if hits:
        ref = f"{col_letter}{hits[0]}"
        try:
            _open(cred, tgt["sheet_id"], readonly=False, tab=tgt["tab"]).update(
                values=[[""]], range_name=ref, value_input_option="RAW")
            log(f"[시트] 쓰기 권한 확인 OK ({ref} 빈 값 테스트 — 내용 변화 없음)")
        except Exception as exc:                               # noqa: BLE001
            raise RuntimeError(
                f"[시트] 쓰기 권한이 없습니다({type(exc).__name__}: {exc}). "
                f"서비스 계정에 편집 권한을 주세요.") from exc
    return hits


def write_blog_links(cred: Path, sheet_id: str, media: str, date: str,
                     urls: list[str], log, campaign: str = "", product: str = "") -> dict:
    """발행 URL 을 매체+날짜(+캠페인 접두사)가 맞는 블로그 링크 열 빈 셀에 위에서부터 기록한다."""
    if not urls:
        return {"written": 0, "rows": []}
    tgt = resolve_target(media, log)
    ws = _open(cred, tgt["sheet_id"], readonly=False, tab=tgt["tab"])
    rows = ws.get_all_values()
    header_row, idx = _cols(rows)
    col = idx[H("link")]
    col_letter = chr(65 + col)
    want_media, want_date = tgt["media"], norm(date)
    j_camp = _campaign_col(rows[header_row - 1])
    j_prod = _product_col(rows[header_row - 1])

    def cell(r: list[str], j: int) -> str:
        return (r[j].strip() if 0 <= j < len(r) else "")

    targets = []
    for n, raw in enumerate(rows[header_row:], start=header_row + 1):
        if canonical_media(cell(raw, idx[H("media")])) != want_media:
            continue
        if norm(cell(raw, idx[H("date")])) != want_date:
            continue
        if not _product_ok(cell(raw, j_prod) if j_prod >= 0 else "", product):
            continue
        if not _campaign_ok(cell(raw, j_camp) if j_camp >= 0 else "", campaign):
            continue
        if cell(raw, idx[H("link")]):
            continue                       # ★값이 있으면 절대 덮어쓰지 않는다
        targets.append(n)

    if not targets:
        raise RuntimeError(f"[시트] 매체 {want_media} / 날짜 {want_date}"
                           f"{' / 제품_결핍 ~' + product if product else ''}"
                           f"{' / 캠페인 ' + campaign if campaign else ''} 에 "
                           f"비어 있는 {H("link")} 칸이 없습니다")

    written = []
    for url, row in zip(urls, targets):
        ref = f"{col_letter}{row}"
        log(f"[실전 시트] 매칭 행={row}")
        log(f"[실전 시트] 생성 랜딩 URL={url}")
        ws.update(values=[[url]], range_name=ref, value_input_option="RAW")
        log(f"[실전 시트] 기록 완료 ✅ ({tgt['tab']} {ref})")
        written.append(row)

    if len(urls) > len(targets):
        log(f"[시트] ⚠ URL {len(urls)}개 중 {len(targets)}개만 기록했습니다 "
            f"(빈 칸 부족). 남은 URL: {urls[len(targets):]}")
    return {"written": len(written), "rows": written,
            "leftover": urls[len(targets):]}


# ── 실전용 전용 (검수용 흐름은 이 함수를 쓰지 않는다) ──────────────────
def find_published_rows(cred: Path, sheet_id: str, media: str, date: str, log,
                        note: str = "", campaign: str = "",
                        on_missing: str = "raise") -> list[dict]:
    """매체+날짜가 맞고 **K(블로그 링크)에 검수용 URL 이 들어 있는** 행을 위에서부터 돌려준다.

    각 행의 J(링크) = 그 행 전용 제품 링크(UTM 이 행마다 다르다). 절대 섞이면 안 된다.
    """
    tgt = resolve_target(media, log)
    ws = _open(cred, tgt["sheet_id"], readonly=True, tab=tgt["tab"])
    rows = ws.get_all_values()
    header_row, idx = _cols(rows)
    # `링크`(제품 URL) 열은 _cols 가 다루지 않으므로 여기서 직접 찾는다.
    header = [norm(c) for c in rows[header_row - 1]]
    try:
        j = header.index(norm(H("product")))
    except ValueError:
        raise RuntimeError(f"`{H("product")}` 열을 찾지 못했습니다. header={header}")

    want_media, want_date = tgt["media"], norm(date)
    # ★캠페인 필터 — 하루에 그룹(g_i_b_o_l / g_i_b_o_n / g_i_b_l_m …)이 여러 개다.
    #   안 좁히면 엉뚱한 그룹의 글까지 수정한다(2026-08-24).
    try:
        j_camp = header.index(norm(H("campaign")))
    except ValueError:
        j_camp = -1

    def cell(r: list[str], k: int) -> str:
        return (r[k].strip() if 0 <= k < len(r) else "")

    out, skipped = [], 0
    for n, raw in enumerate(rows[header_row:], start=header_row + 1):
        if canonical_media(cell(raw, idx[H("media")])) != want_media:
            continue
        if norm(cell(raw, idx[H("date")])) != want_date:
            continue
        camp = cell(raw, j_camp) if j_camp >= 0 else ""
        if not _campaign_ok(camp, campaign):     # ★`_` 단위 접두사 (자릿수 무관)
            skipped += 1
            continue
        blog = cell(raw, idx[H("link")])
        if not blog:
            continue
        out.append({"row": n, "seq": cell(raw, idx[H("seq")]), "campaign": camp,
                    "blog_url": blog, "product_url": cell(raw, j),
                    "note": cell(raw, idx[H("changed")])})

    log(f"[시트] `{tgt['tab']}` 매체 {want_media} / 날짜 {want_date}"
        f"{' / 캐페인 ' + campaign if campaign else ''} — "
        f"블로그 URL 이 있는 행 {len(out)}개"
        f"{f' (다른 캐페인 {skipped}행 제외)' if skipped else ''}")
    out = note_hint(out, note, log)
    return check_product_urls(out, log, on_missing)


def write_production_links(cred: Path, sheet_id: str, marks: list[dict], log,
                           mark_changed: bool = True) -> dict:
    """실전용 결과를 **행 번호로 정확히 지정해** 기록한다.

    marks = [{"row": 8, "url": "...", "product_url": "..."}, ...]
    · `실전용 블로그 링크` 열에 URL 을 쓴다. **K(블로그 링크)는 절대 건드리지 않는다.**
    · `블로그 최하단 제품 링크 변경 여부` 열이 있으면 'O' 를 남긴다.
    """
    if not marks:
        return {"written": 0, "rows": []}
    ws = _open(cred, sheet_id, readonly=False)
    rows = ws.get_all_values()
    header_row, _ = _cols(rows)
    header = [norm(c) for c in rows[header_row - 1]]

    def find(name: str) -> int:
        want = norm(name)
        for i, h in enumerate(header):
            if want in h:
                return i
        return -1

    col = find(H("production"))
    if col < 0:
        raise RuntimeError(
            f"`{SHEET_NAME}` 탭에 `{H("production")}` 열이 없습니다. "
            f"헤더({header_row}행) 맨 오른쪽에 `{H("production")}` 을 추가해 주세요. "
            f"현재 헤더: {[h for h in header if h]}")
    ch = find(H("changed")) if mark_changed else -1

    written = []
    for m in marks:
        ref = f"{chr(65 + col)}{m['row']}"
        ws.update(values=[[m["url"]]], range_name=ref, value_input_option="RAW")
        log(f"[시트] {ref} ← {m['url']}")
        if ch >= 0:
            ws.update(values=[["O"]], range_name=f"{chr(65 + ch)}{m['row']}",
                      value_input_option="RAW")
        written.append(m["row"])
    return {"written": len(written), "rows": written}


def find_result_columns(cred: Path, sheet_id: str, log, media: str = "") -> dict:
    """결과를 적을 열을 찾는다. `블로그 최하단 제품 링크 변경 여부`(필수) / `실전용 블로그 링크`(선택)."""
    tgt = resolve_target(media, log) if media else {"sheet_id": sheet_id, "tab": SHEET_NAME}
    ws = _open(cred, tgt["sheet_id"], readonly=True, tab=tgt["tab"])
    rows = ws.get_all_values()
    header_row, _ = _cols(rows)
    header = [norm(c) for c in rows[header_row - 1]]

    def find(name: str) -> int:
        want = norm(name)
        for i, h in enumerate(header):
            if want and want in h:
                return i
        return -1

    changed = find(H("changed"))
    production = find(H("production"))
    if changed < 0:
        raise RuntimeError(
            f"`{SHEET_NAME}` 탭에서 `{H("changed")} 여부` 열을 찾지 못했습니다. "
            f"현재 헤더: {[h for h in header if h]}")
    log(f"[시트] 완료표시 열 = {chr(65 + changed)}열 "
        f"({rows[header_row - 1][changed].strip()!r})")
    if production >= 0:
        log(f"[시트] 실전용 URL 열 = {chr(65 + production)}열")
    else:
        log(f"[시트] `{H("production")}` 열이 없습니다 — 완료표시(O)만 기록합니다")
    link = find(H("link"))
    return {"header_row": header_row, "changed": changed, "production": production,
            "link": link, "sheet_id": tgt["sheet_id"], "tab": tgt["tab"]}


def mark_row_done(cred: Path, sheet_id: str, row: int, url: str, cols: dict, log) -> bool:
    """한 행을 '완료'로 표시한다 — 발행 + 제품링크 교체가 모두 성공한 행에만 호출할 것.

    · `블로그 최하단 제품 링크 변경 여부` 에 O. **이미 값이 있으면 그대로 둔다.**
    · `실전용 블로그 링크` 열이 있으면 URL 도 적는다.
    · K(블로그 링크) 를 비롯한 다른 열은 절대 건드리지 않는다.
    """
    ws = _open(cred, cols.get("sheet_id") or sheet_id, readonly=False,
               tab=cols.get("tab") or SHEET_NAME)
    ci, pi = cols["changed"], cols["production"]

    cur = (ws.cell(row, ci + 1).value or "").strip()
    if cur.upper() in {"O", "ㅇ"}:
        log(f"[시트] {chr(65 + ci)}{row} 이미 {cur!r} — 그대로 둡니다")
    else:
        # ★이 열은 결핍 메모(`팔자 / 현미`)로 쓰이기도 한다. 제품 링크를 실제로 갈아끼운
        #   행은 헤더 뜻대로 'O' 로 덮어쓴다(2026-08-24 사용자 지시).
        if cur:
            log(f"[시트] {chr(65 + ci)}{row} 기존 메모 {cur!r} → 'O' 로 덮어씁니다")
        ws.update(values=[["O"]], range_name=f"{chr(65 + ci)}{row}",
                  value_input_option="RAW")
        log(f"[실전 시트] 매칭 행={row}")
        log(f"[실전 시트] 생성 랜딩 URL={url}")
        log(f"[실전 시트] 기록 완료 ✅ ({cols.get('tab')} {chr(65 + ci)}{row} ← O)")

    if pi >= 0 and url:
        ref = f"{chr(65 + pi)}{row}"
        if (ws.cell(row, pi + 1).value or "").strip():
            log(f"[시트] {ref} 이미 값이 있어 건드리지 않습니다")
        else:
            ws.update(values=[[url]], range_name=ref, value_input_option="RAW")
            log(f"[시트] {ref} ← {url}")
    return True




def note_hint(rows: list[dict], note: str, log) -> list[dict]:
    """`블로그 최하단 제품 링크 변경 여부`(K) 열에 적어 둔 **결핍 표시**로 대상을 좁힌다.

    ★이 열은 자유 메모 열이다(`반려남`·`CBO세팅` 처럼 쓰기도 한다).
      그래서 **그 결핍으로 표시된 행이 실제로 있을 때만** 좁히고, 없으면 그냥 넘어간다
      (2026-08-24: 824 g_i_b_o_l 30행에 '팔자 / 현미' 표시).
    """
    if not note:
        return rows
    want = norm(note)
    hit = [r for r in rows if norm(r.get("note", "")) == want]
    if not hit:
        marked = sorted({r.get("note", "") for r in rows if r.get("note")})
        if marked:
            log(f"[시트] (참고) 결핍 표시 {note!r} 인 행은 없습니다. "
                f"이 날짜의 표시값: {marked[:5]}")
        return rows
    log(f"[시트] 결핍 표시 {note!r} 로 좁힘 — {len(rows)}행 → {len(hit)}행")
    return hit

def find_pending_rows(cred: Path, sheet_id: str, media: str, date: str, log,
                      campaign: str = "", note: str = "",
                      on_missing: str = "raise") -> list[dict]:
    """매체+날짜가 맞고 **`블로그 링크`(J)가 비어 있는** 행 = 아직 안 만든 랜딩.

    각 행의 `링크`(I) = 그 행 전용 제품 URL(UTM 이 행마다 다르다).
    `campaign` 을 주면 utm_campaign 이 그 값으로 시작하는 행만 고른다
    (하루에 여러 그룹이 있을 때 엉뚱한 그룹에 쓰는 것을 막는다).
    """
    tgt = resolve_target(media, log)
    ws = _open(cred, tgt["sheet_id"], readonly=True, tab=tgt["tab"])
    rows = ws.get_all_values()
    header_row, idx = _cols(rows)
    header = [norm(c) for c in rows[header_row - 1]]

    def col_of(name: str) -> int:
        want = norm(name)
        for i, h in enumerate(header):
            if want and want in h:
                return i
        raise RuntimeError(f"`{name}` 열을 찾지 못했습니다. header={header}")

    j_link = col_of(H("product"))
    j_camp = col_of(H("campaign"))
    want_media, want_date = tgt["media"], norm(date)

    def cell(r: list[str], k: int) -> str:
        return (r[k].strip() if 0 <= k < len(r) else "")

    out, filled = [], 0
    for n, raw in enumerate(rows[header_row:], start=header_row + 1):
        if canonical_media(cell(raw, idx[H("media")])) != want_media:
            continue
        if norm(cell(raw, idx[H("date")])) != want_date:
            continue
        camp = cell(raw, j_camp)
        if not _campaign_ok(camp, campaign):     # ★`_` 단위 접두사 (자릿수 무관)
            continue
        if cell(raw, idx[H("link")]):
            filled += 1
            continue
        out.append({"row": n, "seq": cell(raw, idx[H("seq")]),
                    "campaign": camp, "product_url": cell(raw, j_link),
                    "note": cell(raw, idx[H("changed")])})

    log(f"[시트] `{tgt['tab']}` 매체 {want_media} / 날짜 {want_date}"
        f"{' / 캠페인 ' + campaign if campaign else ''} — "
        f"만들 행 {len(out)}개 (이미 채워진 행 {filled}개)")
    for r in out[:3]:
        log(f"          {r['row']}행 순번{r['seq']} {r['campaign']}")
    if len(out) > 3:
        log(f"          … 외 {len(out) - 3}개")
    out = note_hint(out, note, log)
    return check_product_urls(out, log, on_missing)


def write_blog_link_row(cred: Path, sheet_id: str, row: int, url: str,
                        cols: dict, log, mark_changed: bool = True) -> bool:
    """지정한 행의 `블로그 링크` 열에 발행 URL 을 적는다(이미 값이 있으면 두고 넘어간다)."""
    ws = _open(cred, cols.get("sheet_id") or sheet_id, readonly=False,
               tab=cols.get("tab") or SHEET_NAME)
    li = cols["link"]
    ref = f"{chr(65 + li)}{row}"
    cur = (ws.cell(row, li + 1).value or "").strip()
    if cur:
        log(f"[실전 시트] {ref} 이미 {cur[:40]!r} — 덮어쓰지 않습니다")
        return False
    log(f"[실전 시트] 매칭 행={row}")
    log(f"[실전 시트] 생성 랜딩 URL={url}")
    ws.update(values=[[url]], range_name=ref, value_input_option="RAW")
    log(f"[실전 시트] 기록 완료 ✅ ({cols.get('tab')} {ref})")

    ci = cols.get("changed", -1)
    if mark_changed and ci >= 0 and not (ws.cell(row, ci + 1).value or "").strip():
        ws.update(values=[["O"]], range_name=f"{chr(65 + ci)}{row}",
                  value_input_option="RAW")
        log(f"[실전 시트] {chr(65 + ci)}{row} ← O (블로그 최하단 제품 링크 변경 여부)")
    return True
