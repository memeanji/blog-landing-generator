r"""계정 레지스트리 — `accounts.json` 만 고치면 계정이 늘어난다(코드 수정 없음).

    from v2 import accounts
    acc = accounts.resolve("my_account")     # id / label / blog_id 아무거나
    accounts.load_accounts()                    # GUI 콤보박스용 전체 목록

★파일이 없거나 비어 있으면 **빈 목록**을 돌려준다 — `--account` 를 쓰지 않는 기존 CLI 는
  이 모듈을 전혀 타지 않으므로 동작이 달라지지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .appdir import ROOT      # 개발 PC=프로젝트 폴더 / 설치본=%APPDATA%\BlogLandingAgent
ACCOUNTS_PATH = ROOT / "accounts.json"

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# 마지막 `resolve_for` 가 **무엇을 근거로** 계정을 골랐는지(로그 표시용).
#   ★검증 로그에 "선택 근거: login_id" 를 찍기 위한 것 — 선택 로직에는 쓰지 않는다.
_LAST_BASIS = ""


def last_basis() -> str:
    """가장 최근 계정 선택의 근거 문자열(로그용). 값이 없으면 빈 문자열."""
    return _LAST_BASIS


@dataclass(frozen=True)
class Account:
    id: str
    label: str = ""
    blog_id: str = ""
    login_id: str = ""           # 네이버 로그인 ID — **화면 표시용**(비밀번호는 저장 안 함)
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
                "login_id": self.login_id,
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
        login_id=str(raw.get("login_id") or "").strip(),
        ref_tab=str(raw.get("ref_tab") or ""),          # ★끝 공백이 의미 있는 탭이 있다 — strip 금지
        brand=str(raw.get("brand") or "").strip(),
        media=str(raw.get("media") or "").strip(),
        note=str(raw.get("note") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
        profile_dir=str(raw.get("profile_dir") or "").strip(),
    )


# ══════════════════════════════════════════════════════════════════
#  계정 목록의 출처 (2026-09-17 — Supabase 중앙관리)
#    ① 메모리 TTL 캐시  ② Supabase  ③ accounts_cache.json  ④ accounts.json
#  ★PC 마다 accounts.json 을 손으로 넣던 것을 없애려고 만들었다. 담당자 PC 에는
#    화면(브라우저)과 Agent 뿐이라 파일을 옮기는 게 현실적이지 않았다.
#  ★계정을 **고르는 규칙은 하나도 바뀌지 않는다** — 목록을 어디서 읽는지만 바뀐다.
#    구버전의 탭이름/해시 자동 생성은 복구하지 않는다.
# ══════════════════════════════════════════════════════════════════
REMOTE_TTL_SEC = 60.0                       # 한 실행에서 load_accounts 가 여러 번 불린다
CACHE_PATH = ROOT / "accounts_cache.json"   # 마지막 정상 목록(장애 시 fallback)

_mem: tuple[float, list, str] | None = None   # (받은 시각, raw rows, 출처 설명)
_SOURCE = ""


def source_label() -> str:
    """가장 최근에 계정 목록을 어디서 읽었는지(로그 표시용)."""
    return _SOURCE


def _remote_rows() -> list | None:
    """Supabase 에서 계정 목록. 설정이 없거나 실패하면 **None**(조용히 폴백)."""
    if (os.getenv("BLOG_ACCOUNTS_REMOTE") or "1").strip().lower() in ("0", "off", "false"):
        return None                          # 테스트·오프라인용 스위치
    if not (os.getenv("BLOG_DEVICE_TOKEN") or "").strip():
        return None                          # 아직 이 PC 가 연결되지 않았다
    try:
        from .supabase_store import SupabaseStore
        rows = SupabaseStore().accounts_list()
    except Exception:                                          # noqa: BLE001
        return None                          # 네트워크·장애 → 캐시로 내려간다
    if not rows:
        return None                          # 0행(토큰 불일치 등)은 '못 받은 것'으로 본다
    # 원격 컬럼(active) → 파일 형식(enabled) 으로만 맞춘다. 나머지는 그대로.
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d.pop("active", True))
        out.append(d)
    return out


def _write_cache(rows: list) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(
            {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
             "accounts": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass                                 # 캐시 못 써도 이번 실행은 계속한다


def _cache_rows() -> tuple[list | None, str]:
    if not CACHE_PATH.exists():
        return None, ""
    try:
        d = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        rows = d.get("accounts") or []
        at = d.get("fetched_at") or "?"
        return (rows, f"캐시(마지막 동기화 {at})") if rows else (None, "")
    except Exception:                                          # noqa: BLE001
        return None, ""


def _file_rows(p: Path) -> list:
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
    return data


def refresh() -> None:
    """메모리 캐시를 버린다(다음 조회 때 원격을 다시 본다)."""
    global _mem
    _mem = None


def _rows(path: Path | str | None) -> list:
    """계정 목록 원본(raw). ★`path` 를 직접 준 호출은 **그 파일만** 읽는다
    (테스트·이관용). 평소 실행은 원격 → 캐시 → 파일 순으로 내려간다."""
    global _mem, _SOURCE
    if path is not None:
        _SOURCE = f"지정 파일({Path(path).name})"
        return _file_rows(Path(path))

    now = time.monotonic()
    if _mem and (now - _mem[0]) < REMOTE_TTL_SEC:
        _SOURCE = _mem[2]
        return _mem[1]

    rows = _remote_rows()
    if rows is not None:
        _write_cache(rows)
        src = f"Supabase({len(rows)}건)"
    else:
        rows, src = _cache_rows()
        if rows is None:
            rows = _file_rows(ACCOUNTS_PATH)
            src = f"로컬 {ACCOUNTS_PATH.name}({len(rows)}건)"
    _mem, _SOURCE = (now, rows, src), src
    return rows


def load_accounts(path: Path | str | None = None, include_disabled: bool = False
                  ) -> list[Account]:
    """계정 목록. 출처는 위 `_rows` 참조(원격 → 캐시 → 파일). 없으면 빈 목록."""
    data = _rows(path)

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


def find_by_identity(login_id: str = "", blog_id: str = "",
                     path: Path | str | None = None) -> "Account | None":
    """★계정 선택의 유일한 근거 — **실제 식별정보(login_id / blog_id)로만** 찾는다.

    세션 폴더 이름(`id`)·라벨·tab_slug 해시는 **보지 않는다.** 계정 이름이 무엇이든
    (smile_hyunmi·hangbokhaseo7·tab_xxxx) 결과가 같아야 하기 때문이다.
    `find_account` 의 느슨한 검색(id/label/blog_id 중 하나만 맞으면 반환)과 달리
    **정확히 일치**만 인정하고, 여러 건이면 조용히 하나를 고르지 않고 예외를 던진다.

    찾는 순서: login_id → blog_id. 못 찾으면 None(오류 문구는 호출한 쪽에서 만든다).
    """
    rows = load_accounts(path, include_disabled=True)
    for field, want in (("login_id", (login_id or "").strip()),
                        ("blog_id", (blog_id or "").strip())):
        if not want:
            continue
        low = want.casefold()
        hits = [a for a in rows
                if (getattr(a, field, "") or "").strip().casefold() == low]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise RuntimeError(
                f"{field}={want!r} 로 등록된 계정이 {len(hits)}개입니다 "
                f"({[h.id for h in hits]}). accounts.json 에서 중복을 정리해 주세요 — "
                f"어느 계정인지 정할 수 없어 중단합니다.")
    return None


def find_exact(key: str, path: Path | str | None = None) -> "Account | None":
    """운영자가 직접 준 값(`--account`)을 **정확히** 일치로만 찾는다.

    ★부분일치·라벨 추측을 하지 않는다(`find_account` 의 2단계 부분일치가
      엉뚱한 계정을 집어오던 원인이다). 순서: login_id → blog_id → id.
    """
    want = (key or "").strip()
    if not want:
        return None
    low = want.casefold()
    rows = load_accounts(path, include_disabled=True)
    for field in ("login_id", "blog_id", "id"):
        hits = [a for a in rows
                if (getattr(a, field, "") or "").strip().casefold() == low]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise RuntimeError(
                f"{field}={want!r} 로 등록된 계정이 {len(hits)}개입니다 "
                f"({[h.id for h in hits]}). accounts.json 에서 중복을 정리해 주세요.")
    return None


def _registered_summary(path: Path | str | None = None) -> str:
    """오류 문구에 붙일 '지금 등록된 계정' 목록(무엇을 적어야 할지 바로 보이게)."""
    try:
        rows = load_accounts(path, include_disabled=True)
    except Exception:                                          # noqa: BLE001
        return "(목록을 읽지 못했습니다)"
    if not rows:
        return "(등록된 계정이 없습니다)"
    return " / ".join(
        f"{a.id}(login_id={a.login_id or '-'}, blog_id={a.blog_id or '-'})"
        for a in rows)


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
def resolve_for(key, ref_tab: str = "", brand=None, create: bool = True,
                path: Path | str | None = None,
                login_id: str = "", blog_id: str = "") -> "Account | None":
    """쓸 계정을 정한다 — **로그인·글쓰기 모든 경로가 이 함수를 쓴다**.

    ★화면이 보낸 값이 그 PC 의 계정 이름과 다를 수 있다(화면에는 계정 목록이
      없어서 탭에서 만든 값을 보낸다). 예전에는 목록에서만 찾다가 그대로
      실패했다. 기준랜딩 탭을 함께 받으면 어긋나지 않는다.

    ★2026-09-17 구조 변경 — **계정은 실제 식별정보로만 고른다.**
      전에는 화면이 보낸 키(= brands.json 의 `session_id`, 없으면 `tab_slug` 해시)를
      `find_account` 로 풀었다. 그 키는 **세션 폴더 이름**일 뿐인데 계정의 정체가 돼서,
      한 계정의 폴더 이름이 다른 계정의 blog_id 와 같으면(hangbokhaseo7) 파일 순서로
      승자가 갈렸다 — 고른 계정과 다른 사람 자리가 열렸다(2026-09-16).
      이제 `session_id`·`Account.id`·해시는 **선택 근거가 아니다.** 계정을 찾은 뒤
      그 계정의 세션 폴더를 찾는 **내부 저장 키**로만 쓴다.

    선택 정책 (2026-09-17 확정. 이름·해시는 어디에도 쓰지 않는다)
      A. **login_id / blog_id 를 얻은 경우** — `find_by_identity` **하나만** 쓴다.
         login_id 를 안 받았으면 **brands.json 의 기준랜딩 탭 → login_id** 로 채운다.
         ★못 찾으면 그 자리에서 **오류.** key·ref_tab·session_id·tab_slug 로
           내려가 **다른 계정을 추측하지 않는다.**
      B. **login_id·blog_id 를 아예 얻을 수 없는 경우만**(brands.json 에 계정이 없는
         옛 기준랜딩 탭, `--account` 직접 실행 등) 아래 호환 경로를 쓴다.
           ① 운영자가 직접 준 key — `find_exact`(정확일치만. 부분일치·라벨추측 금지)
           ② accounts.json 에 **등록된** 기준랜딩 탭(`ref_tab` + 같은 브랜드)
           ③ 그래도 없으면 오류
      어느 경우에도 `tab_slug` 해시로 **새 계정을 만들지 않는다**
      (`create=False` 는 '찾기만' 이라 오류 대신 None 을 준다).
    """
    global _LAST_BASIS
    _LAST_BASIS = ""
    tab = (ref_tab or "").strip()
    lid = (login_id or "").strip()
    bid = (blog_id or "").strip()

    # brands.json 은 화면·PC 양쪽에 같은 값이 가 있다 → 어느 PC 에서든 같은 login_id.
    src = "인자"
    if tab and not lid:
        try:
            from . import brands as _brands
            info = _brands.resolve(brand).account_of(tab)
            lid = str(info.get("login_id") or "").strip()
            src = f"brands.json({tab})"
        except Exception:                                      # noqa: BLE001
            pass

    acc = None
    how = ""
    # ── A. 식별정보(login_id/blog_id)를 얻은 경우 = **이것만** 근거로 쓴다 ──
    #   ★못 찾았을 때 key·ref_tab 으로 넘어가면 안 된다. 그러면 "다른 계정을
    #     조용히 골라 주는" 예전 사고가 되살아난다(2026-09-16 행복하서연 자리).
    #     찾지 못하면 그 자리에서 끝낸다.
    if lid or bid:
        acc = find_by_identity(lid, bid, path)
        if acc is None:
            if not create:
                return None
            raise RuntimeError(
                (f"기준랜딩 {tab!r} 의 " if tab else "")
                + f"login_id={lid!r} 에 해당하는 등록 계정을 찾을 수 없습니다."
                + (f" (blog_id={bid!r})" if bid else "")
                + f"{chr(10)}       accounts.json 에 같은 login_id 또는 blog_id 를 "
                  f"등록해 주세요."
                + f"{chr(10)}       ★login_id 를 얻은 실행이므로 key·ref_tab·세션 폴더 "
                  f"이름으로 **다른 계정을 추측하지 않습니다**(새로 만들지도 않습니다)."
                + f"{chr(10)}       등록된 계정: {_registered_summary(path)}")
        how = (f"login_id={lid}" if lid and (acc.login_id or "").strip().casefold()
               == lid.casefold() else f"blog_id={bid}") + f" · 출처={src}"
    # ── B. 식별정보를 **얻을 수 없는** 레거시/특수 경로만 아래로 내려온다 ──
    #   (brands.json 에 계정이 없는 옛 기준랜딩 탭, `--account` 직접 실행 등)
    if acc is None and key:
        acc = find_exact(str(key), path)
        if acc is not None:
            how = f"운영자가 준 key={key!r} (정확일치 · login_id 없음)"
    if acc is None and tab:
        acc = find_by_tab(tab, brand, path)      # accounts.json 에 등록된 탭
        if acc is not None:
            how = f"accounts.json 에 등록된 ref_tab={tab!r} (login_id 없음)"

    if acc is not None:
        _LAST_BASIS = how
        # ★찾은 계정의 등록 blog_id 와 요청된 blog_id 가 다르면 즉시 중단.
        #   (login_id 는 맞는데 blog_id 가 다른 경우 = 등록정보가 어긋난 상태)
        if bid and acc.blog_id and acc.blog_id.casefold() != bid.casefold():
            raise RuntimeError(
                f"계정 정보가 어긋납니다 — login_id={lid or '?'} 로 찾은 계정 "
                f"{acc.title}(id={acc.id}) 의 등록 blog_id 는 {acc.blog_id!r} 인데 "
                f"요청된 blog_id 는 {bid!r} 입니다. accounts.json 을 확인해 주세요 "
                f"(임의로 고치지 않고 중단합니다).")
        return acc

    if not create:
        return None
    raise RuntimeError(
        (f"기준랜딩 {tab!r} 의 " if tab else "")
        + f"login_id={lid!r} 에 해당하는 등록 계정을 찾을 수 없습니다."
        + (f" (blog_id={bid!r})" if bid else "")
        + f"{chr(10)}       accounts.json 에 같은 login_id 또는 blog_id 를 등록해 주세요."
        + f"{chr(10)}       세션 폴더 이름으로 계정을 추측하지 않습니다(새로 만들지도 않습니다)."
        + f"{chr(10)}       등록된 계정: {_registered_summary(path)}")


def set_blog_id(account_id: str, blog_id: str,
                path: Path | str | None = None) -> bool:
    """계정에 블로그 주소를 적어 둔다(비어 있을 때 한 번).

    ★이게 있어야 "고른 계정과 로그인한 계정이 같은가" 를 확인할 수 있다.
      비어 있으면 검사가 걸리지 않아, 남의 블로그에 글이 올라갈 수 있다.
    """
    import json

    p = Path(path) if path else ACCOUNTS_PATH
    if not p.exists():
        return False
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = data["accounts"] if isinstance(data, dict) else data
    changed = False
    for row in rows:
        if row.get("id") == account_id and not (row.get("blog_id") or "").strip():
            row["blog_id"] = blog_id
            changed = True
    if changed:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return changed


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
