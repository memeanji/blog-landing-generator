r"""GUI 선택지(매체 · 결핍) — 기준랜딩 시트에서 읽어 캐시한다.

시트가 곧 목록이므로 **결핍/제품이 늘어도 코드를 고칠 일이 없다.** 시트만 채우면 된다.
계정마다 기준랜딩 탭이 다르므로 캐시도 탭 단위로 나눈다(`out/catalog_<탭>.json`).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import brands, landing_sheet, sheets
from .config import load_settings

ROOT = Path(__file__).resolve().parent.parent


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9가-힣]+", "_", (text or "").strip()).strip("_") or "default"


def cache_path(tab: str, brand=None) -> Path:
    """★캐시는 **브랜드마다 따로** 둔다 — 브랜드 목록이 섞이면 안 된다."""
    bid = brands.brand_id(brand) or brands.DEFAULT_BRAND_ID
    return ROOT / "out" / f"catalog_{_slug(bid)}_{_slug(tab)}.json"


def resolve_tab(account=None, ref_tab: str = "", brand=None) -> str:
    """이번 조회에서 쓸 기준랜딩 탭.

    우선순위: 직접 지정(ref_tab) > **같은 브랜드인** 계정의 ref_tab > 브랜드 기본 탭.
    """
    tab = ref_tab or ""
    if not tab and account is not None:
        getter = getattr(account, "tab_for_brand", None)
        tab = getter(brand) if callable(getter) else getattr(account, "ref_tab", "")
    return sheets.set_tab(tab) if tab else sheets.active_tab()


def load(account=None, ref_tab: str = "", refresh: bool = False, brand=None) -> dict:
    """`{"tab", "cached_at", "media": [...], "items": {매체: [ {...}, ... ]}}`.

    `refresh=False` 면 캐시가 있을 때 시트를 읽지 않는다(GUI 가 즉시 뜬다).
    """
    b = sheets.set_brand(brand)                 # ★브랜드를 먼저 고정한다
    landing_sheet.set_brand(b)
    tab = resolve_tab(account, ref_tab, brand=b)
    path = cache_path(tab, b)
    if not refresh and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("items"):
                data["from_cache"] = True
                return data
        except Exception:                                      # noqa: BLE001
            pass

    settings = load_settings()
    settings.check()
    rows = sheets.load_rows(settings.service_account_json, settings.spreadsheet_id)

    items: dict[str, list[dict]] = {}
    for r in rows:
        items.setdefault(r["media"], []).append({
            "row": r["row"],
            "deficiency": r["deficiency"],
            "검수용": sheets.is_url(r["검수용"]),
            "실전용": sheets.is_url(r["실전용"]),
            "제품URL": {k: (r.get("제품URL") or {}).get(k, "") for k in sheets.KINDS},
        })
    data = {"tab": tab, "brand": b.id, "brand_label": b.title,
            "reference_sheet": b.reference_sheet_id,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
            "media": list(items), "items": items, "from_cache": False}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass
    return data


def tabs_cache_path(brand=None) -> Path:
    bid = brands.brand_id(brand) or brands.DEFAULT_BRAND_ID
    return ROOT / "out" / f"tabs_{_slug(bid)}.json"


def load_tabs(brand=None, refresh: bool = False) -> dict:
    """선택한 브랜드 기준시트의 **기준랜딩 탭 목록**.

        {"brand": "repurely", "tabs": ["스마일 현미 기준랜딩", ...], "cached_at": ...}

    ★코드에 계정을 박지 않기 위한 것 — 시트에 `<이름> 기준랜딩` 탭을 만들면 그대로 늘어난다.
      브랜드마다 캐시 파일이 다르므로 목록이 섞이지 않는다.
    """
    b = sheets.set_brand(brand)
    landing_sheet.set_brand(b)
    path = tabs_cache_path(b)
    if not refresh and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("tabs"):
                data["from_cache"] = True
                return data
        except Exception:                                      # noqa: BLE001
            pass

    settings = load_settings()
    settings.check()
    found = sheets.list_reference_tabs(settings.service_account_json)
    data = {"brand": b.id, "brand_label": b.title, "tabs": found,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
            "from_cache": False}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass
    return data


def deficiencies(data: dict, media: str, kind: str = "") -> list[str]:
    """매체별 결핍 목록. kind 를 주면 그 열이 준비된(URL 이 있는) 것만."""
    rows = (data.get("items") or {}).get(media) or []
    if kind:
        rows = [r for r in rows if r.get(kind)]
    return [r["deficiency"] for r in rows]


def label_for(data: dict, media: str, deficiency: str) -> str:
    for r in (data.get("items") or {}).get(media) or []:
        if r["deficiency"] == deficiency:
            marks = " · ".join(k for k in sheets.KINDS if r.get(k)) or "준비중"
            return f"{deficiency}   ({marks})"
    return deficiency


def sheet_media_for(media: str) -> str:
    """기준랜딩 시트 표기(`카모`) → UTM 빌더 시트 표기(`카카오모먼트`)."""
    return landing_sheet.canonical_media(media)
