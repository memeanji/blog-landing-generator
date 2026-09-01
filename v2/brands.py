r"""브랜드 레지스트리 — **시트 ID 가 있는 유일한 설정 계층**.

    from v2 import brands
    b = brands.resolve("닥터누센트")     # id / label 아무거나
    b.reference_sheet_id                # 블로그 랜딩 기준시트
    b.utm_tab("카카오모먼트")            # UTM 빌더의 매체 탭

브랜드가 늘면 **`brands.json` 에 한 덩어리만 추가**하면 된다(코드 수정 없음).
`accounts.json` 과 같은 방식이다.

★`DEFAULTS` 는 **뼈대만** 가지고 있다(시트 ID 는 비어 있다). 실제 시트 ID 는
  `brands.json`(로컬) 또는 환경변수 `BLOG_BRANDS_JSON`(배포)에서 채운다 —
  공개 저장소에 시트 ID 를 남기지 않기 위해서다. 둘 다 없으면 `Brand.check()` 가
  "기준시트 ID 가 없습니다" 로 분명하게 멈춘다.

★브랜드별 데이터가 절대 섞이면 안 된다. 기준시트/UTM 빌더는 언제나 한 브랜드에서만
  가져온다 — `sheets.set_brand()` / `landing_sheet.set_brand()` 가 그 창구다.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # blog_landing_generator/
BRANDS_PATH = ROOT / "brands.json"
NL = chr(10)

# UTM 빌더 탭 이름 규칙 — 리퓨어리 실측(`카카오모먼트 블로그 랜딩 UTM 빌더`).
DEFAULT_UTM_TAB_PATTERN = "{media} 블로그 랜딩 UTM 빌더"

# 기준시트에서 '기준랜딩 탭' 을 알아보는 표시. `스마일 현미 기준랜딩` 처럼 이름에 들어간다.
#   ★탭을 추가하는 것만으로 계정이 늘어나게 하는 장치다(코드 수정 없음).
DEFAULT_REFERENCE_TAB_MARK = "기준랜딩"

# ★기본 브랜드 = 리퓨어리. `--brand` 를 주지 않은 기존 CLI 는 전부 여기로 온다.
DEFAULT_BRAND_ID = "repurely"

DEFAULTS: list[dict] = [
    {
        "id": "repurely",
        "label": "리퓨어리",
        "reference_sheet_id": "",      # ← brands.json / BLOG_BRANDS_JSON 에서 채운다
        "reference_sheet_label": "리퓨어리 블로그 랜딩 기준시트",
        "reference_tab": "스마일 현미 기준랜딩",
        "utm_sheet_id": "",            # ← brands.json / BLOG_BRANDS_JSON 에서 채운다
        "utm_sheet_label": "리퓨어리 UTM 빌더",
        "utm_tab_pattern": DEFAULT_UTM_TAB_PATTERN,
        "utm_media_tabs": {},
        "headers": {},
        "enabled": True,
        "ready": True,
        "status_note": "",
        "note": "기존에 쓰던 시트 그대로(2026-08-31 이전 하드코딩 값)",
    },
    {
        "id": "doctor_nuscent",
        "label": "닥터누센트",
        "reference_sheet_id": "",
        "reference_sheet_label": "닥터누센트 블로그 랜딩 기준시트",
        "reference_tab": "시트1",
        "utm_sheet_id": "",
        "utm_sheet_label": "닥터누센트 UTM 빌더",
        "utm_tab_pattern": DEFAULT_UTM_TAB_PATTERN,
        "utm_media_tabs": {},
        "headers": {},
        "enabled": True,
        "ready": False,
        "status_note": "기준시트 미완성 · UTM 빌더 서비스 계정 권한 대기",
        "note": "2026-08-31 추가. 준비가 끝나면 brands.json 의 ready 를 true 로 바꾸면 된다",
    },
]

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class Brand:
    id: str
    label: str = ""
    reference_sheet_id: str = ""
    reference_sheet_label: str = ""
    reference_tab: str = ""
    # 기준랜딩 탭을 알아보는 표시(이름에 이 말이 들어간 탭 = 기준랜딩 탭)
    reference_tab_mark: str = DEFAULT_REFERENCE_TAB_MARK
    utm_sheet_id: str = ""
    utm_sheet_label: str = ""
    utm_tab_pattern: str = DEFAULT_UTM_TAB_PATTERN
    utm_media_tabs: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    enabled: bool = True
    # ★ready=False = "준비 중" — 화면에 표시하고 **실행은 막는다**.
    #   준비가 끝나면 brands.json 에서 true 로만 바꾸면 된다(코드 수정 없음).
    ready: bool = True
    status_note: str = ""        # 준비 중인 이유(화면에 그대로 보여 준다)
    note: str = ""

    # ── 표시 ─────────────────────────────────────────────────────
    @property
    def title(self) -> str:
        return self.label or self.id

    @property
    def reference_title(self) -> str:
        return self.reference_sheet_label or f"{self.title} 블로그 랜딩 기준시트"

    @property
    def utm_title(self) -> str:
        return self.utm_sheet_label or f"{self.title} UTM 빌더"

    def sheet_url(self, sheet_id: str) -> str:
        return (f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
                if sheet_id else "")

    # ── 매핑 ─────────────────────────────────────────────────────
    def utm_tab(self, canonical_media: str) -> str:
        """매체(정규화된 시트 표기) → UTM 빌더 탭 이름.

        `utm_media_tabs` 에 명시된 값이 최우선. 없으면 `utm_tab_pattern` 으로 만든다
        (브랜드마다 if 문을 늘리지 않기 위한 매핑 계층).
        """
        explicit = (self.utm_media_tabs or {}).get(canonical_media)
        if explicit:
            return str(explicit)
        pattern = self.utm_tab_pattern or DEFAULT_UTM_TAB_PATTERN
        return pattern.format(media=canonical_media)

    def is_reference_tab(self, title: str) -> bool:
        """기준시트의 이 탭이 '기준랜딩 탭' 인가(= 계정 하나에 해당하는가)."""
        mark = (self.reference_tab_mark or DEFAULT_REFERENCE_TAB_MARK).replace(" ", "")
        return bool(mark) and mark in (title or "").replace(" ", "")

    def account_name_of(self, title: str) -> str:
        """`스마일 현미 기준랜딩` → `스마일 현미` (화면 보조 표기용)."""
        mark = (self.reference_tab_mark or DEFAULT_REFERENCE_TAB_MARK)
        return (title or "").replace(mark, "").strip() or (title or "").strip()

    def header(self, key: str, default: str) -> str:
        """UTM 빌더 컬럼명 오버라이드. 브랜드가 다른 헤더를 쓰면 `headers` 로 맞춘다."""
        value = (self.headers or {}).get(key)
        return str(value) if value else default

    def require_ready(self) -> None:
        """준비되지 않은 브랜드는 여기서 멈춘다(오발행 방지)."""
        if self.ready:
            return
        raise RuntimeError(
            f"[브랜드] {self.title} 은(는) 아직 준비 중이라 실행할 수 없습니다"
            + (f" — {self.status_note}" if self.status_note else "")
            + f"{chr(10)}       준비가 끝나면 brands.json 의 "
              f"`{self.id}` 항목에서 ready 를 true 로 바꿔 주세요.")

    def check(self) -> None:
        if not self.reference_sheet_id:
            raise RuntimeError(f"[브랜드] {self.title}: 기준시트 ID 가 없습니다 "
                               f"(brands.json 의 reference_sheet_id)")
        if not self.utm_sheet_id:
            raise RuntimeError(f"[브랜드] {self.title}: UTM 빌더 ID 가 없습니다 "
                               f"(brands.json 의 utm_sheet_id)")

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label,
                "reference_sheet_id": self.reference_sheet_id,
                "reference_sheet_label": self.reference_sheet_label,
                "reference_tab": self.reference_tab,
                "reference_tab_mark": self.reference_tab_mark,
                "utm_sheet_id": self.utm_sheet_id,
                "utm_sheet_label": self.utm_sheet_label,
                "utm_tab_pattern": self.utm_tab_pattern,
                "utm_media_tabs": dict(self.utm_media_tabs or {}),
                "headers": dict(self.headers or {}),
                "enabled": self.enabled, "ready": self.ready,
                "status_note": self.status_note, "note": self.note}

    def summary(self) -> dict:
        """UI/로그용 요약 — 인증정보는 담지 않는다."""
        return {"brand": self.id, "brand_label": self.title, "ready": self.ready,
                "reference_sheet": self.reference_sheet_id,
                "reference_title": self.reference_title,
                "reference_tab": self.reference_tab,
                "utm_sheet": self.utm_sheet_id,
                "utm_title": self.utm_title}


def _clean_id(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if not _ID_RE.match(v):
        v = re.sub(r"[^A-Za-z0-9_\-]", "_", v).strip("_")
    return v


def _from_raw(raw: dict) -> Brand | None:
    if not isinstance(raw, dict):
        return None
    ident = _clean_id(str(raw.get("id") or ""))
    if not ident:
        return None
    tabs = raw.get("utm_media_tabs") or {}
    heads = raw.get("headers") or {}
    return Brand(
        id=ident,
        label=str(raw.get("label") or "").strip(),
        reference_sheet_id=str(raw.get("reference_sheet_id") or "").strip(),
        reference_sheet_label=str(raw.get("reference_sheet_label") or "").strip(),
        # ★탭 이름은 끝 공백이 의미 있을 수 있다 — strip 금지(기존 `참고용 랜딩 ` 사례).
        reference_tab=str(raw.get("reference_tab") or ""),
        reference_tab_mark=str(raw.get("reference_tab_mark")
                               or DEFAULT_REFERENCE_TAB_MARK),
        utm_sheet_id=str(raw.get("utm_sheet_id") or "").strip(),
        utm_sheet_label=str(raw.get("utm_sheet_label") or "").strip(),
        utm_tab_pattern=str(raw.get("utm_tab_pattern") or DEFAULT_UTM_TAB_PATTERN),
        utm_media_tabs=dict(tabs) if isinstance(tabs, dict) else {},
        headers=dict(heads) if isinstance(heads, dict) else {},
        enabled=bool(raw.get("enabled", True)),
        ready=bool(raw.get("ready", True)),
        status_note=str(raw.get("status_note") or "").strip(),
        note=str(raw.get("note") or "").strip(),
    )


class BrandConfigError(RuntimeError):
    """`brands.json` 자체를 읽지 못했다 — 다른 브랜드로 넘어가면 안 되는 상황."""


def load_brands(path: Path | str | None = None,
                include_disabled: bool = False,
                strict: bool = False) -> list[Brand]:
    """`brands.json` → Brand 목록.

    · strict=False (기본, CLI) — 파일이 없거나 깨졌으면 **내장 기본값**(리퓨어리)으로 산다.
      `--brand` 없이 돌리던 기존 명령이 설정 파일 하나 때문에 죽지 않게 하기 위함이다.
    · strict=True  (UI)        — 읽지 못하면 `BrandConfigError`. **다른 브랜드로 자동
      fallback 하지 않는다.** 엉뚱한 브랜드 시트로 실행되는 것이 훨씬 위험하다.

    파일에 같은 id 가 있으면 파일 쪽이 이긴다(기본값을 덮어쓴다).
    """
    p = Path(path) if path else BRANDS_PATH
    raw_rows: list = []

    # ★파일 대신 환경변수/Secrets 로 넣을 수도 있다(공개 저장소에 시트 ID 를 두기 싫을 때).
    #   `BLOG_BRANDS_JSON` = brands.json 과 같은 내용(JSON 문자열).
    inline = (os.getenv("BLOG_BRANDS_JSON") or "").strip() if path is None else ""
    if inline:
        try:
            data = json.loads(inline)
            rows = data.get("brands") if isinstance(data, dict) else data
            if isinstance(rows, list) and rows:
                merged: dict[str, Brand] = {}
                for raw in DEFAULTS:
                    b = _from_raw(raw)
                    if b:
                        merged[b.id] = b
                for raw in rows:
                    b = _from_raw(raw)
                    if b:
                        merged[b.id] = b
                return [b for b in merged.values() if b.enabled or include_disabled]
        except Exception as exc:                               # noqa: BLE001
            if strict:
                raise BrandConfigError(
                    f"BLOG_BRANDS_JSON 을 읽지 못했습니다: "
                    f"{type(exc).__name__}: {exc}") from exc

    if not p.exists():
        if strict:
            raise BrandConfigError(
                f"브랜드 설정을 찾지 못했습니다.{NL}"
                f"       · 파일 : {p} (없음){NL}"
                f"       · Secrets/환경변수 : BLOG_BRANDS_JSON (비어 있음){NL}{NL}"
                f"       브랜드마다 기준시트/UTM 빌더가 달라 "
                f"임의의 기본값으로 대체하지 않습니다.{NL}"
                f"       · 로컬            : brands.example.json 을 "
                f"brands.json 으로 복사해 시트 ID 를 채우세요.{NL}"
                f"       · Streamlit Cloud : 앱 Settings → Secrets 에 "
                f"BLOG_BRANDS_JSON 을 넣으세요{NL}"
                f"                           (brands.json 은 시트 ID 때문에 "
                f"저장소에 올리지 않습니다).")
    else:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("brands") or []
            if not isinstance(data, list):
                raise ValueError("brands 가 목록이 아닙니다")
            raw_rows = data
        except BrandConfigError:
            raise
        except Exception as exc:                               # noqa: BLE001
            if strict:
                raise BrandConfigError(
                    f"브랜드 설정을 읽지 못했습니다: {p}{chr(10)}"
                    f"       {type(exc).__name__}: {exc}{chr(10)}"
                    f"       JSON 문법을 고치기 전에는 실행할 수 없습니다"
                    f"(다른 브랜드로 대체하지 않습니다).") from exc
            raw_rows = []                                      # CLI 는 기본값으로 산다

    merged: dict[str, Brand] = {}
    for raw in DEFAULTS:
        b = _from_raw(raw)
        if b:
            merged[b.id] = b
    for raw in raw_rows:
        b = _from_raw(raw)
        if b:
            merged[b.id] = b
    return [b for b in merged.values() if b.enabled or include_disabled]


def find_brand(key: str, path: Path | str | None = None) -> Brand:
    """id · label 중 아무거나로 브랜드 1개를 찾는다."""
    want = (key or "").strip()
    if not want:
        raise RuntimeError("브랜드를 지정하지 않았습니다.")
    rows = load_brands(path, include_disabled=True)
    low = want.casefold()
    for b in rows:
        if low in {b.id.casefold(), b.label.casefold()} - {""}:
            return b
    hits = [b for b in rows
            if low in b.id.casefold() or (b.label and low in b.label.casefold())]
    if len(hits) == 1:
        return hits[0]
    listing = " / ".join(f"{b.id}({b.title})" for b in rows)
    if len(hits) > 1:
        raise RuntimeError(f"브랜드 {want!r} 이(가) 여러 개와 겹칩니다: {[h.id for h in hits]}")
    raise RuntimeError(f"[브랜드] 설정에 없는 브랜드입니다: {want!r}\n"
                       f"       쓸 수 있는 브랜드: {listing}\n"
                       f"       (brands.json 에 한 덩어리 추가하면 바로 쓸 수 있습니다)")


def default_brand(path: Path | str | None = None) -> Brand:
    """`--brand` 를 주지 않았을 때의 브랜드 = 리퓨어리(기존 동작)."""
    try:
        return find_brand(DEFAULT_BRAND_ID, path)
    except RuntimeError:
        rows = load_brands(path, include_disabled=True)
        if rows:
            return rows[0]
        b = _from_raw(DEFAULTS[0])
        assert b is not None
        return b


def resolve(key: str | Brand | None, path: Path | str | None = None) -> Brand:
    """`--brand` 값을 Brand 로. 비어 있으면 기본 브랜드(리퓨어리)."""
    if key is None or key == "":
        return default_brand(path)
    if isinstance(key, Brand):
        return key
    return find_brand(str(key), path)


def brand_id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Brand):
        return value.id
    return _clean_id(str(value))
