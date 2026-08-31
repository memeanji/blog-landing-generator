r"""계정 레지스트리 — `accounts.json` 만 고치면 계정이 늘어난다(코드 수정 없음).

    from v2 import accounts
    acc = accounts.resolve("my_account")     # id / label / blog_id 아무거나
    accounts.load_accounts()                    # GUI 콤보박스용 전체 목록

★파일이 없거나 비어 있으면 **빈 목록**을 돌려준다 — `--account` 를 쓰지 않는 기존 CLI 는
  이 모듈을 전혀 타지 않으므로 동작이 달라지지 않는다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # blog_landing_generator/
ACCOUNTS_PATH = ROOT / "accounts.json"

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class Account:
    id: str
    label: str = ""
    blog_id: str = ""
    ref_tab: str = ""            # 계정별 기준랜딩 탭(sheets.set_tab 에 넣는다)
    brand: str = ""              # ★이 ref_tab 이 속한 브랜드. 비우면 기본 브랜드(리퓨어리).
                                 #   다른 브랜드로 실행하면 ref_tab 을 쓰지 않는다(혼용 방지)
    media: str = ""              # GUI 기본 선택 매체
    note: str = ""
    enabled: bool = True
    profile_dir: str = ""        # 비우면 sessions/<id>/profile

    @property
    def title(self) -> str:
        return self.label or self.blog_id or self.id

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "blog_id": self.blog_id,
                "ref_tab": self.ref_tab, "brand": self.brand, "media": self.media,
                "note": self.note, "enabled": self.enabled,
                "profile_dir": self.profile_dir}

    def tab_for_brand(self, brand) -> str:
        """이 계정의 기준랜딩 탭 — **같은 브랜드일 때만** 쓴다.

        계정 탭(`스마일 현미 기준랜딩`)은 리퓨어리 시트의 탭이다. 닥터누센트로 실행하면서
        이 탭을 그대로 쓰면 '탭 없음' 으로 죽거나(운 나쁘면) 엉뚱한 시트를 본다.
        """
        from . import brands as _brands
        want = _brands.brand_id(brand) or _brands.DEFAULT_BRAND_ID
        mine = (self.brand or _brands.DEFAULT_BRAND_ID)
        return self.ref_tab if mine == want else ""


def _clean_id(value: str) -> str:
    """세션 폴더 이름으로 쓸 수 있게 다듬는다(경로 탈출 방지)."""
    v = (value or "").strip()
    if not v or not _ID_RE.match(v):
        v = re.sub(r"[^A-Za-z0-9_\-]", "_", v).strip("_")
    return v


def _from_raw(raw: dict) -> Account | None:
    if not isinstance(raw, dict):
        return None
    ident = _clean_id(str(raw.get("id") or raw.get("blog_id") or ""))
    if not ident:
        return None
    return Account(
        id=ident,
        label=str(raw.get("label") or "").strip(),
        blog_id=str(raw.get("blog_id") or "").strip(),
        ref_tab=str(raw.get("ref_tab") or ""),          # ★끝 공백이 의미 있는 탭이 있다 — strip 금지
        brand=str(raw.get("brand") or "").strip(),
        media=str(raw.get("media") or "").strip(),
        note=str(raw.get("note") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
        profile_dir=str(raw.get("profile_dir") or "").strip(),
    )


def load_accounts(path: Path | str | None = None, include_disabled: bool = False
                  ) -> list[Account]:
    """`accounts.json` 을 읽어 Account 목록으로. 파일이 없으면 빈 목록."""
    p = Path(path) if path else ACCOUNTS_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        raise RuntimeError(f"{p.name} 을 읽지 못했습니다({type(exc).__name__}: {exc}). "
                           f"JSON 문법을 확인하세요.") from exc
    if isinstance(data, dict):
        data = data.get("accounts") or []
    if not isinstance(data, list):
        raise RuntimeError(f"{p.name} 형식이 잘못됐습니다 — 목록이거나 "
                           f'{{"accounts": [...]}} 이어야 합니다.')

    out, seen = [], set()
    for raw in data:
        acc = _from_raw(raw)
        if acc is None or acc.id in seen:
            continue
        seen.add(acc.id)
        if acc.enabled or include_disabled:
            out.append(acc)
    return out


def find_account(key: str, path: Path | str | None = None) -> Account:
    """id · label · blog_id 중 아무거나로 계정 1개를 찾는다."""
    want = (key or "").strip()
    if not want:
        raise RuntimeError("계정을 지정하지 않았습니다.")
    rows = load_accounts(path, include_disabled=True)
    if not rows:
        raise RuntimeError(
            f"{ACCOUNTS_PATH.name} 에 계정이 없습니다. 아래 형태로 추가하세요:\n"
            '       {"accounts": [{"id": "my_account", "label": "내 계정", '
            '"blog_id": "my_account", "ref_tab": "스마일 현미 기준랜딩"}]}')

    low = want.casefold()
    for acc in rows:                                   # 1) 정확히 일치
        if low in {acc.id.casefold(), acc.label.casefold(), acc.blog_id.casefold()} - {""}:
            return acc
    hits = [a for a in rows                            # 2) 부분 일치
            if low in a.id.casefold() or low in a.label.casefold()
            or (a.blog_id and low in a.blog_id.casefold())]
    if len(hits) == 1:
        return hits[0]
    listing = " / ".join(f"{a.id}({a.title})" for a in rows)
    if len(hits) > 1:
        raise RuntimeError(f"계정 {want!r} 이(가) {len(hits)}개와 겹칩니다: "
                           f"{[h.id for h in hits]}")
    raise RuntimeError(f"계정 {want!r} 을(를) {ACCOUNTS_PATH.name} 에서 찾지 못했습니다.\n"
                       f"       쓸 수 있는 계정: {listing}")


def resolve(key: str | Account | None, path: Path | str | None = None) -> Account | None:
    """`--account` 값을 Account 로. 값이 없으면 None(= 기존 동작 그대로)."""
    if key is None or key == "":
        return None
    if isinstance(key, Account):
        return key
    return find_account(str(key), path)


def account_id(value) -> str:
    """Account · 문자열 · None 어느 것이 와도 세션 id 를 뽑는다."""
    if value is None:
        return ""
    if isinstance(value, Account):
        return value.id
    return _clean_id(str(value))


def add_account(acc: Account, path: Path | str | None = None) -> Account:
    """GUI 에서 계정을 추가할 때 쓴다(같은 id 가 있으면 덮어쓴다)."""
    p = Path(path) if path else ACCOUNTS_PATH
    data = {"accounts": []}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {"accounts": loaded}
        except Exception:                                      # noqa: BLE001
            pass
    rows = [r for r in (data.get("accounts") or []) if isinstance(r, dict)]
    acc = replace(acc, id=_clean_id(acc.id))
    rows = [r for r in rows if _clean_id(str(r.get("id") or r.get("blog_id") or ""))
            != acc.id]
    rows.append(acc.to_dict())
    data["accounts"] = rows
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return acc

# ── 기준랜딩 탭 ↔ 계정 ────────────────────────────────────────────
#   ★계정 목록을 코드에도 화면에도 박지 않는다. 기준시트에 `<이름> 기준랜딩` 탭이 생기면
#     그 탭에 대응하는 계정(=세션 폴더)을 여기서 만들어 준다. 사람이 accounts.json 을
#     고칠 필요가 없다. 이미 있는 계정(my_account · seoyeon)은 그대로 재사용된다.
def tab_slug(tab: str, brand=None) -> str:
    """탭 이름 → 세션 폴더로 쓸 수 있는 **안정적인** id.

    한글 탭 이름은 폴더명으로 못 쓰므로 짧은 해시를 붙인다(같은 탭 = 언제나 같은 id).
    """
    import hashlib

    from . import brands as _brands

    bid = _brands.brand_id(brand) or _brands.DEFAULT_BRAND_ID
    key = f"{bid}::{(tab or '').strip()}"
    plain = _clean_id((tab or "").strip())
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return f"{plain}_{digest}" if plain else f"tab_{digest}"


def find_by_tab(tab: str, brand=None, path: Path | str | None = None) -> "Account | None":
    """이 기준랜딩 탭을 쓰는 계정을 찾는다(같은 브랜드일 때만)."""
    want = (tab or "").strip()
    if not want:
        return None
    for acc in load_accounts(path, include_disabled=True):
        if (acc.tab_for_brand(brand) or "").strip() == want:
            return acc
    return None


def ensure_for_tab(tab: str, brand=None, create: bool = True,
                   path: Path | str | None = None) -> "Account | None":
    """기준랜딩 탭에 대응하는 계정을 돌려준다. 없으면(create=True) 만들어 저장한다."""
    from . import brands as _brands

    acc = find_by_tab(tab, brand, path)
    if acc is not None or not create:
        return acc
    b = _brands.resolve(brand)
    label = b.account_name_of(tab) if hasattr(b, "account_name_of") else tab
    return add_account(Account(id=tab_slug(tab, b), label=label, blog_id="",
                               ref_tab=tab, brand=b.id,
                               note=f"{b.title} 기준시트의 `{tab}` 탭에서 자동 등록"),
                       path)
