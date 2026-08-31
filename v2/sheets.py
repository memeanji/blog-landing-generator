"""기준랜딩 탭에서 매체+결핍+검수용/실전용 조건에 정확히 맞는 참고 URL 1건을 찾는다.

시트 실측(2026-08-20)
  · 탭 이름은 계정마다 다르다(기본 `스마일 현미 기준랜딩`). 끝 공백이 있을 수 있어 strip 비교.
  · 헤더 2행 / 데이터 3행~
  · 헤더(2026-08-21 사용자 변경): 매체 · 결핍 · **검수용 블로그랜딩** · **검수용 제품 링크**
    · **실전용 블로그랜딩** (예전 `…참고` 헤더도 그대로 인식한다)
  · 매체는 블록의 첫 행에만 적혀 있고 아래는 빈칸 → 위 행에서 이어받는다.
  · **제품 링크는 검수용/실전용 따로** 읽는다(`검수용 제품 링크` / `실전용 제품 링크`).
    한쪽만 있거나 `제품 링크` 처럼 구분 없는 열 하나만 있어도 동작한다.
    기준글 맨 아래 제품 이미지에는 링크가 안 걸려 있어(빈 앵커) 글에서는 못 뽑는다.
  · `곰도리` 처럼 URL 이 아닌 값 = 아직 준비중
  · `곰도리` 처럼 URL 이 아닌 값 = 아직 준비중
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import brands

# ★기준시트(블로그 랜딩 기준시트)는 **브랜드마다 다르다**(2026-08-31).
#   시트 ID 는 `v2/brands.py` / `brands.json` 한 곳에서만 관리한다. 여기 상수는
#   브랜드를 고르지 않은 실행(=기존 CLI)의 기본값(리퓨어리)일 뿐이다.
#   .env 의 REFERENCE_SPREADSHEET_ID 는 예전처럼 무시한다(고정 시트).
REFERENCE_SHEET_ID = brands.default_brand().reference_sheet_id

# ★기준랜딩 탭 이름. 블로그 계정마다 탭을 따로 두므로 바꿔 끼울 수 있게 해 둔다.
#   (2026-08-24: `참고용 랜딩 ` → `스마일 현미 기준랜딩` 으로 이름이 바뀌었다)
#   우선순위: set_tab() > 환경변수 REFERENCE_TAB > 선택한 브랜드의 reference_tab
SHEET_NAME = brands.default_brand().reference_tab or "스마일 현미 기준랜딩"
_ACTIVE_TAB = ""
_ACTIVE_BRAND: "brands.Brand | None" = None


def set_brand(brand) -> "brands.Brand":
    """이번 실행에서 쓸 브랜드를 지정한다.

    ★브랜드를 바꾸면 **기준시트가 통째로 바뀐다**. 브랜드를 바꿀 때 이전 브랜드의 탭이
      남아 있으면 안 되므로 set_tab() 으로 잡아 둔 탭도 함께 지운다(데이터 혼용 방지).
    """
    global _ACTIVE_BRAND, _ACTIVE_TAB
    b = brands.resolve(brand)
    if _ACTIVE_BRAND is not None and _ACTIVE_BRAND.id != b.id:
        _ACTIVE_TAB = ""
    _ACTIVE_BRAND = b
    return b


def active_brand() -> "brands.Brand":
    """지금 쓰는 브랜드. 지정하지 않았으면 기본 브랜드(리퓨어리) = 기존 동작."""
    return _ACTIVE_BRAND or brands.default_brand()


def active_sheet_id() -> str:
    return active_brand().reference_sheet_id or REFERENCE_SHEET_ID


def set_tab(name: str) -> str:
    """이번 실행에서 쓸 기준랜딩 탭을 지정한다(계정별 탭 전환용)."""
    global _ACTIVE_TAB
    _ACTIVE_TAB = (name or "").strip()
    return _ACTIVE_TAB or active_tab()


def active_tab() -> str:
    import os
    return (_ACTIVE_TAB or (os.getenv("REFERENCE_TAB") or "").strip()
            or active_brand().reference_tab or SHEET_NAME)
KINDS = ("검수용", "실전용")

# 사용자가 뭐라고 적든 시트 표기로 맞춘다.
MEDIA_ALIASES = {
    "gfa": {"gfa", "지에프에이", "네이버gfa", "네이버"},
    "카모": {"카모", "카카오", "카카오모먼트", "kakao", "kakaomoment"},
    "메타": {"메타", "meta", "페북", "페이스북", "facebook"},
    "틱톡": {"틱톡", "tiktok"},
}

_URL_RE = re.compile(r"^https?://", re.I)


def norm(s: str) -> str:
    """공백 정리 — 시트 값에 끝 공백/이중 공백이 섞여 있다."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def canonical_media(value: str) -> str:
    v = norm(value).casefold()
    for canon, names in MEDIA_ALIASES.items():
        if v == canon.casefold() or v in {n.casefold() for n in names}:
            return canon
    return norm(value)


def is_url(v: str) -> bool:
    return bool(_URL_RE.match(norm(v)))


# 제품 링크 컬럼으로 인정하는 헤더 이름(공백 무시 부분일치).
#   ★검수용/실전용 구분이 있으면 앞에 종류를 붙여 찾는다: '검수용제품링크' …
PRODUCT_HEADERS = ("제품링크", "제품상세URL", "제품상세주소", "제품URL", "상품URL", "상품링크")


@dataclass(frozen=True)
class Reference:
    media: str
    deficiency: str
    kind: str
    url: str
    row: int
    product_url: str = ""


def service_account_email(cred: Path) -> str:
    """오류 메시지에 '어느 계정을 공유해야 하는지' 를 같이 보여주기 위해 읽는다."""
    try:
        import json as _json
        return str(_json.loads(Path(cred).read_text(encoding="utf-8"))
                   .get("client_email") or "")
    except Exception:                                          # noqa: BLE001
        return ""


TRANSIENT = ("500", "502", "503", "504", "internal error", "temporarily",
             "temporary error", "timed out", "timeout", "connection")


def is_transient(exc: Exception) -> bool:
    """구글이 잠깐 흔들린 것(5xx/타임아웃)인지. 이때만 한 번 더 시도한다."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if "permission" in text or "403" in text or "404" in text:
        return False
    return any(t in text for t in TRANSIENT)


def open_with_retry(open_fn, tries: int = 2, wait: float = 3.0):
    """시트 열기 — 일시적 오류면 한 번 더. 권한/없음 오류는 즉시 올린다."""
    import time
    for i in range(max(1, tries)):
        try:
            return open_fn()
        except Exception as exc:                               # noqa: BLE001
            if i >= tries - 1 or not is_transient(exc):
                raise
            time.sleep(wait)
    raise RuntimeError("시트를 열지 못했습니다")               # 도달하지 않음


def short(text: str, limit: int = 200) -> str:
    """구글이 HTML 오류 페이지를 통째로 던질 때가 있다 — 한 줄로 줄인다."""
    import re as _re
    t = _re.sub(r"<[^>]+>", " ", text or "")
    t = _re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def access_error(brand, stage: str, title: str, sheet_id: str, cred, exc) -> RuntimeError:
    """시트 열기 실패를 **원인이 드러나는** 한 줄로 바꾼다.

        brand=doctor_nuscent stage=utm_sheet_access status=failed reason=permission_denied
    """
    text = short(f"{type(exc).__name__}: {exc}")
    denied = ("PermissionError" in type(exc).__name__
              or "PERMISSION_DENIED" in text or "403" in text
              or "insufficientPermissions" in text)
    missing = ("SpreadsheetNotFound" in type(exc).__name__ or "404" in text)
    reason = ("permission_denied" if denied else
              "sheet_not_found" if missing else
              "google_temporary_error" if is_transient(exc) else "open_failed")
    who = service_account_email(cred) if cred else ""
    nl = chr(10)
    tip = ""
    if denied:
        tip = (nl + "       → 시트를 서비스 계정에 공유해 주세요"
               + (f" ({who} · 편집자)" if who else "")
               + nl + "       → 권한을 준 뒤에는 코드 수정 없이 그대로 다시 실행하면 됩니다.")
    elif missing:
        tip = nl + "       → brands.json 의 시트 ID 를 확인해 주세요."
    elif reason == "google_temporary_error":
        tip = nl + "       → 구글 시트가 잠깐 응답하지 않았습니다. 잠시 뒤 다시 실행해 주세요."
    return RuntimeError(
        f"[시트] brand={getattr(brand, 'id', '')} stage={stage} status=failed "
        f"reason={reason}\n"
        f"       `{title}` 을(를) 열지 못했습니다 (id={sheet_id})\n"
        f"       {text}{tip}")


def open_book(cred: Path):
    """**선택한 브랜드의** 기준시트(스프레드시트)를 연다. 실패하면 원인이 드러나는 예외."""
    import gspread
    from google.oauth2.service_account import Credentials

    brand = active_brand()
    creds = Credentials.from_service_account_file(
        str(cred), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    # ★어떤 sheet_id 가 들어오든 기준시트는 **선택한 브랜드의 시트**만 본다(혼용 방지).
    target = active_sheet_id()
    try:
        client = gspread.authorize(creds)
        return open_with_retry(lambda: client.open_by_key(target))
    except Exception as exc:                                   # noqa: BLE001
        raise access_error(brand, "reference_sheet_access", brand.reference_title,
                           target, cred, exc) from exc


def list_tabs(cred: Path) -> list[str]:
    """기준시트의 탭 이름 전부(시트 순서 그대로)."""
    return [ws.title for ws in open_book(cred).worksheets()]


def list_reference_tabs(cred: Path) -> list[str]:
    """**기준랜딩 탭만** 골라 돌려준다 — 계정 하나 = 기준랜딩 탭 하나.

    ★목록을 코드에 박지 않는다. 시트에 `<이름> 기준랜딩` 탭을 만들면 그대로 늘어난다.
      브랜드를 바꾸면 그 브랜드 기준시트만 보므로 브랜드끼리 섞이지 않는다.
    """
    brand = active_brand()
    return [t for t in list_tabs(cred) if brand.is_reference_tab(t)]


def _open(cred: Path, sheet_id: str):
    brand = active_brand()
    book = open_book(cred)
    want = active_tab()
    for ws in book.worksheets():
        if ws.title.strip() == want:
            return ws
    raise RuntimeError(
        f"[기준시트] brand={brand.id} stage=reference_tab status=failed "
        f"reason=tab_not_found\n"
        f"       `{brand.reference_title}` 에 `{want}` 탭이 없습니다: "
        f"{[w.title for w in book.worksheets()]}")


def load_rows(cred: Path, sheet_id: str) -> list[dict]:
    """(매체, 결핍, 검수용, 실전용) 로 정리된 데이터 행 목록. 매체는 위 행에서 이어받는다."""
    rows = _open(cred, sheet_id).get_all_values()
    if len(rows) < 3:
        return []
    header = [norm(c) for c in rows[1]]

    def col(*names: str) -> int:
        for i, h in enumerate(header):
            if any(n in h.replace(" ", "") for n in [x.replace(" ", "") for x in names]):
                return i
        raise RuntimeError(f"헤더에서 {names} 컬럼을 찾지 못했습니다. header={header}")

    def col_opt(*names: str) -> int:
        """없어도 되는 컬럼 — 못 찾으면 -1."""
        try:
            return col(*names)
        except RuntimeError:
            return -1

    mi, di = col("매체"), col("결핍")
    #   '검수용 블로그랜딩' / 예전 '검수용 블로그랜딩 참고' 둘 다 이 이름으로 잡힌다.
    ri, pi = col("검수용블로그랜딩"), col("실전용블로그랜딩")

    # 제품 링크 — 종류별 컬럼이 하나라도 있으면 그것만 쓰고,
    #             없을 때만 구분 없는 컬럼 하나를 양쪽에 공용으로 쓴다.
    qi = {k: col_opt(*[k + n for n in PRODUCT_HEADERS]) for k in KINDS}
    if all(v < 0 for v in qi.values()):
        shared = col_opt(*PRODUCT_HEADERS)
        qi = {k: shared for k in KINDS}

    out, current = [], ""
    for n, raw in enumerate(rows[2:], start=3):
        cell = lambda i: norm(raw[i]) if i < len(raw) else ""      # noqa: E731
        if cell(mi):
            current = canonical_media(cell(mi))
        if not cell(di):
            continue
        out.append({"row": n, "media": current, "deficiency": cell(di),
                    "검수용": cell(ri), "실전용": cell(pi),
                    "제품URL": {k: (cell(qi[k]) if qi[k] >= 0 else "") for k in KINDS}})
    return out


def find_reference(cred: Path, sheet_id: str, media: str, deficiency: str,
                   kind: str) -> Reference:
    """조건과 정확히 일치하는 1건. 없거나 URL 이 아니면 이유를 담아 예외."""
    if kind not in KINDS:
        raise RuntimeError(f"kind 는 {KINDS} 중 하나여야 합니다: {kind!r}")
    want_media, want_def = canonical_media(media), norm(deficiency)
    rows = load_rows(cred, sheet_id)

    hits = [r for r in rows if r["media"] == want_media and r["deficiency"] == want_def]
    if not hits:
        same = sorted({r["deficiency"] for r in rows if r["media"] == want_media})
        raise RuntimeError(
            f"매체 {want_media!r} + 결핍 {want_def!r} 조합이 시트에 없습니다.\n"
            f"       해당 매체의 결핍 목록: {same}")
    if len(hits) > 1:
        raise RuntimeError(f"조합이 {len(hits)}개 중복입니다(행 {[h['row'] for h in hits]}). "
                           "시트를 확인하세요.")
    hit = hits[0]
    url = hit[kind]
    if not url:
        raise RuntimeError(f"{want_media} / {want_def} 의 {kind} 칸이 비어 있습니다(랜딩 준비중). "
                           f"시트 {hit['row']}행")
    if not is_url(url):
        raise RuntimeError(f"{want_media} / {want_def} 의 {kind} 칸이 URL 이 아닙니다: {url!r} "
                           f"(랜딩 준비중, 시트 {hit['row']}행)")
    product = (hit.get("제품URL") or {}).get(kind) or ""
    if product and not is_url(product):
        raise RuntimeError(f"{want_media} / {want_def} 의 {kind} 제품 링크가 주소가 아닙니다: "
                           f"{product!r} (시트 {hit['row']}행)")
    return Reference(media=want_media, deficiency=want_def, kind=kind, url=url,
                     row=hit["row"], product_url=product)
