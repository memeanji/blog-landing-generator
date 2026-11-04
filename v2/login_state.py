r"""로그인 검증 등급 · 세션 별칭 · 로그아웃 대기 — **login_id 기준**(2026-11-03).

왜 따로 만들었나
    세션 파일(`sessions/<id>/state.json`)은 '쿠키가 있나' 만 안다. 그런데 "그 쿠키가
    **어느 로그인 계정** 것인지, 그걸 **어떻게 확인했는지**" 는 별도 사실이다.
    이 둘을 섞어 두면 `manual`(사용자가 그렇다고 한 것)과 `authenticated`
    (네이버가 인증한 ID 를 실제로 읽은 것)가 같은 등급이 되어 버린다.

확정 정책
  · 계정 식별은 **login_id** 로만 한다. `blog_id` 가 같아도 login_id 가 다르면
    **다른 계정**이고 세션·쿠키·프로필을 절대 공유하지 않는다.
  · `blog_id` 는 **발행 대상 확인용**이다(세션 통합 기준으로 쓰지 않는다).
  · 검증 등급 — authenticated > manual > input > unavailable (**서로 다른 등급**)
      authenticated  네이버가 인증한 ID 를 읽어 기대값과 일치함을 확인
      manual         사용자가 '확인했다'고 **기록**한 것(인증 아님)
      input          로그인 창에 **입력된** ID 가 기대값과 일치(인증 아님)
      unavailable    아무것도 확인하지 못함
  · 적용은 3단계 — off → **warn(기본)** → enforce. 기존 계정이 일괄로 막히지 않게
    기본은 warn 이고, 발행을 멈추는 것은 **명백한 불일치**뿐이다.
  · 별칭(login_id → session_id)은 **단방향 1단**이고, 등급이 manual 이상이고
    대상 세션에 같은 login_id 가 기록돼 있을 때만 연결한다.

★이 파일은 **쿠키·토큰·비밀번호를 저장하거나 기록하지 않는다.** 저장하는 것은
  login_id / blog_id / 등급 / 시각 / 확인 방법뿐이다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .accounts import account_id
from .appdir import ROOT
from .loginid import NAVER_MAIL_DOMAINS, norm_login, same_login   # noqa: F401

SESSIONS_ROOT = ROOT / "sessions"
ALIAS_PATH = SESSIONS_ROOT / "login_aliases.json"       # login_id → session_id
PENDING_PATH = SESSIONS_ROOT / "logout_pending.json"    # 로그아웃 대기 login_id

# ── 검증 등급 ────────────────────────────────────────────────────
AUTHENTICATED, MANUAL, INPUT, UNAVAILABLE = (
    "authenticated", "manual", "input", "unavailable")
RANK = {AUTHENTICATED: 3, MANUAL: 2, INPUT: 1, UNAVAILABLE: 0}
LABEL = {AUTHENTICATED: "네이버 인증 ID 확인", MANUAL: "사용자 확인(인증 아님)",
         INPUT: "입력 ID 일치(인증 아님)", UNAVAILABLE: "확인 불가"}

# ── 적용 단계 ────────────────────────────────────────────────────
OFF, WARN, ENFORCE = "off", "warn", "enforce"


def mode() -> str:
    """`BLOG_LOGIN_CHECK` = off / warn(기본) / enforce."""
    v = (os.getenv("BLOG_LOGIN_CHECK") or WARN).strip().lower()
    return v if v in (OFF, WARN, ENFORCE) else WARN


def rank(grade: str) -> int:
    return RANK.get((grade or "").strip().lower(), 0)


def norm_id(value: str) -> str:
    return (value or "").strip()


def same_id(a: str, b: str) -> bool:
    """**blog_id** 비교 — 대소문자를 구분하지 않는다(네이버가 그렇다).

    ★`login_id` 비교에는 `same_login`(= `v2/loginid.py`)을 쓴다. 로그인 ID 는
      `shin894894` / `shin894894@naver.com` 두 형태로 적히기 때문이다.
      blog_id 에는 도메인 표기가 없으므로 여기서 도메인을 떼면 안 된다.
    """
    a, b = norm_id(a), norm_id(b)
    return bool(a) and bool(b) and a.casefold() == b.casefold()


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    """임시 파일 → os.replace. 반쯤 쓰인 JSON 을 읽는 일이 없게."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.replace(tmp, path)
    except PermissionError:                   # 윈도우 파일감시가 물고 있을 때
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        try:
            tmp.unlink()
        except Exception:                                      # noqa: BLE001
            pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════════
#  meta.json 안의 두 가지를 **분리**해 둔다
#    account_map : 어느 세션 폴더가 어느 계정인지(매핑) — 로그아웃해도 남는다
#    auth_state  : 지금 로그인돼 있는지 / 어떻게 확인했는지 — 로그아웃하면 비운다
# ══════════════════════════════════════════════════════════════════
def _meta_path(acc) -> Path:
    from . import session_store
    return session_store.meta_path(acc)


def read_meta(acc) -> dict:
    return _read(_meta_path(acc))


def account_map(acc) -> dict:
    """이 세션 폴더가 어느 계정인지(login_id·blog_id). 로그아웃해도 유지된다."""
    return dict((read_meta(acc).get("account_map") or {}))


def auth_state(acc) -> dict:
    """지금 인증 상태. 로그아웃하면 비워진다. 없으면 빈 dict."""
    return dict((read_meta(acc).get("auth_state") or {}))


def grade_of(acc) -> str:
    return (auth_state(acc).get("grade") or UNAVAILABLE).strip().lower()


def set_account_map(acc, *, login_id: str = "", blog_id: str = "",
                    ref_tab: str = "", brand: str = "") -> dict:
    """매핑을 적어 둔다 — **비어 있을 때만 채우고, 덮어쓰지 않는다.**

    ★이미 다른 login_id 가 적혀 있으면 조용히 바꾸지 않고 그대로 둔다(계정 뒤섞임 방지).
      바꿔야 하는 상황은 운영자가 직접 확인할 일이다.
    """
    from . import session_store

    meta = read_meta(acc)
    cur = dict(meta.get("account_map") or {})
    lid, bid = norm_id(login_id), norm_id(blog_id)
    if lid and not norm_id(cur.get("login_id")):
        cur["login_id"] = lid
    if bid and not norm_id(cur.get("blog_id")):
        cur["blog_id"] = bid
    if ref_tab and not cur.get("ref_tab"):
        cur["ref_tab"] = ref_tab
    if brand and not cur.get("brand"):
        cur["brand"] = brand
    cur["session_id"] = account_id(acc)
    session_store.write_meta(acc, account_map=cur)
    return cur


def set_auth_state(acc, *, grade: str, blog_id: str = "",
                   authenticated_login_id: str = "", entered_login_id: str = "",
                   expected_login_id: str = "", by: str = "",
                   note: str = "") -> dict:
    """인증 상태를 기록한다.

    ★`manual` 은 **사용자 기록**이다. `authenticated` 로 올리지 않는다.
      나중에 authenticated 를 얻으면 그때 자동으로 올라간다(등급은 매번 새로 쓴다).
    """
    from . import session_store

    g = (grade or UNAVAILABLE).strip().lower()
    if g not in RANK:
        g = UNAVAILABLE
    state = {
        "grade": g, "grade_label": LABEL[g],
        "verified_at": now_iso(),
        "blog_id": norm_id(blog_id),
        "authenticated_login_id": norm_id(authenticated_login_id),
        "entered_login_id": norm_id(entered_login_id),
        "expected_login_id": norm_id(expected_login_id),
        "by": by or g,
    }
    if note:
        state["note"] = note[:200]
    session_store.write_meta(acc, auth_state=state)
    return state


def record_login_result(acc, *, seen_blog_id: str = "", expected_blog_id: str = "",
                        expected_login_id: str = "",
                        authenticated_login_id: str = "",
                        entered_login_id: str = "",
                        manual_ok: bool = False, inherit: bool = False) -> dict:
    """로그인/세션 확인 결과를 **정직하게** 등급으로 바꿔 기록한다.

    등급 판정 (위에서부터, 하나만 고른다)
      authenticated  네이버가 인증한 login_id 를 **읽어서** 기대값과 일치
      input          로그인 창에 **입력된** login_id 가 기대값과 일치
      manual         사용자가 '이 계정 맞다' 고 눌러 **기록**한 경우(인증 아님)
      (승계)         창 없이 저장 세션을 재사용한 경우 — 기존 등급을 **그대로 둔다**
      unavailable    위 어느 것도 아님

    ★`blog_id` 는 등급을 올리지 않는다. 다만 **발행 대상 블로그가 맞는지**는
      별도 사실(`blog_verified`)로 항상 기록하고, 어긋나면 발행을 막는다.
    """
    want_bid = norm_id(expected_blog_id) or norm_id(account_map(acc).get("blog_id"))
    seen = norm_id(seen_blog_id)
    blog_verified = bool(seen and want_bid and same_id(seen, want_bid))

    prev = auth_state(acc)
    auth_lid, ent_lid = norm_id(authenticated_login_id), norm_id(entered_login_id)
    want_lid = norm_id(expected_login_id) or norm_id(account_map(acc).get("login_id"))

    if auth_lid and want_lid and same_login(auth_lid, want_lid):
        grade, by = AUTHENTICATED, "naver_account_api"
    elif ent_lid and want_lid and same_login(ent_lid, want_lid):
        grade, by = INPUT, "login_form_input"
    elif manual_ok:
        grade, by = MANUAL, "user_confirmed"
    elif inherit and prev.get("grade"):
        # ★저장 세션 재사용(자동 로그인·간편 로그인 포함) — 입력 ID 를 알 수 없다.
        #   등급을 올리지도 내리지도 않고 **승계**한다. blog_id 대조는 매번 다시 한다.
        grade, by = str(prev["grade"]), f"inherited({prev.get('by') or '?'})"
    else:
        grade, by = UNAVAILABLE, "no_evidence"

    state = set_auth_state(
        acc, grade=grade, blog_id=seen or want_bid,
        authenticated_login_id=auth_lid, entered_login_id=ent_lid,
        expected_login_id=want_lid, by=by)
    from . import session_store
    state["blog_verified"] = blog_verified
    state["expected_blog_id"] = want_bid
    session_store.write_meta(acc, auth_state=state)
    return state


def clear_auth_state(acc) -> bool:
    """로그아웃 — 인증 상태만 비운다(**매핑은 남긴다**)."""
    from . import session_store

    path = _meta_path(acc)
    meta = _read(path)
    if not meta:
        return False
    meta["auth_state"] = {}
    meta["logged_out_at"] = now_iso()
    # ★옛 키(blog_id·saved_at)는 표시용으로 남겨 둔다 — 매핑 정보이지 인증정보가 아니다.
    _write(path, meta)
    session_store.write_meta(acc, logged_out_at=meta["logged_out_at"])
    return True


# ══════════════════════════════════════════════════════════════════
#  발행 가능 여부 — 명백한 불일치는 **어느 단계에서도** 막는다
# ══════════════════════════════════════════════════════════════════
def check_publish(acc, *, expected_login_id: str = "", expected_blog_id: str = "",
                  seen_blog_id: str = "") -> tuple[bool, str, str]:
    """`(발행해도 되나, 등급, 사유)`.

    막는 경우
      ① 발행 대상 블로그 불일치 — `seen_blog_id` 가 기대 blog_id 와 다르다
      ② 로그인 계정 불일치 — 인증된 login_id 가 기대 login_id 와 다르다
      ③ enforce 단계에서 등급이 unavailable
    ①②는 **단계(off/warn/enforce)와 무관하게** 막는다. 그래서 warn 으로 둬도
    "남의 블로그에 올라가는" 사고는 그대로 걸린다.
    """
    st = auth_state(acc)
    amap = account_map(acc)
    g = (st.get("grade") or UNAVAILABLE).strip().lower()
    want_lid = norm_id(expected_login_id) or norm_id(amap.get("login_id"))
    want_bid = norm_id(expected_blog_id) or norm_id(amap.get("blog_id"))

    # ★발행 대상 블로그 대조 — 지금 본 값이 있으면 그것을, 없으면 마지막 확인값을 쓴다.
    now_bid = norm_id(seen_blog_id) or norm_id(st.get("blog_id"))
    if now_bid and want_bid and not same_id(now_bid, want_bid):
        return False, g, (f"발행 대상 블로그가 다릅니다 — 기대 {want_bid} / "
                          f"확인된 블로그 {now_bid}")

    got_lid = norm_id(st.get("authenticated_login_id"))
    if got_lid and want_lid and not same_login(got_lid, want_lid):
        return False, g, (f"로그인 계정이 다릅니다 — 기대 login_id {want_lid} / "
                          f"인증된 login_id {got_lid}")

    if mode() == ENFORCE and rank(g) <= RANK[UNAVAILABLE]:
        return False, g, ("로그인 계정을 확인하지 못했습니다(enforce 단계) — "
                          "[로그인 상태 다시 확인] 을 눌러 주세요")
    return True, g, ""


def warn_text(acc) -> str:
    """warn 단계에서 화면에 띄울 한 줄(발행은 막지 않는다). 없으면 빈 문자열."""
    st = auth_state(acc)
    g = (st.get("grade") or UNAVAILABLE).strip().lower()
    tail = (f" · 발행 대상 블로그({norm_id(st.get('blog_id'))}) 일치 확인됨"
            if st.get("blog_verified") else "")
    if rank(g) >= RANK[AUTHENTICATED]:
        return ""
    if rank(g) == RANK[UNAVAILABLE]:
        return "로그인 계정(login_id)을 확인하지 못했습니다(발행은 막지 않습니다)" + tail
    return (f"로그인 계정 확인 수준: {LABEL.get(g, g)} — 네이버 인증 확인은 아닙니다"
            + tail)


# ══════════════════════════════════════════════════════════════════
#  세션 별칭 — login_id → session_id (단방향 · 1단 · 검증 후에만)
# ══════════════════════════════════════════════════════════════════
def aliases() -> dict:
    return dict((_read(ALIAS_PATH).get("aliases") or {}))


def session_for_login(login_id: str) -> str:
    """이 login_id 가 쓰는 세션 폴더. 없으면 빈 문자열."""
    lid = norm_id(login_id)
    if not lid:
        return ""
    table = aliases()
    for key, row in table.items():
        if same_login(key, lid):
            return str((row or {}).get("session_id") or "")
    return ""


def link_session(login_id: str, acc, *, grade: str = "", ref_tab: str = "",
                 brand: str = "") -> tuple[bool, str]:
    """`login_id` 를 이 세션 폴더에 연결한다. `(연결됨, 사유)`.

    3중 검증 — 하나라도 어긋나면 **연결하지 않는다**
      ① login_id 가 비어 있지 않다
      ② 대상 세션 폴더가 실제로 있다
      ③ 그 폴더의 매핑(login_id)이 이 키와 같다(또는 아직 비어 있다 → 처음 연결)
    등급이 manual 미만(input/unavailable)이면 **연결하지 않는다**(검증 실패한
    인증정보를 다른 계정의 정상 세션으로 저장하는 사고를 막는다).
    """
    from . import session_store

    lid = norm_id(login_id)
    if not lid:
        return False, "login_id 가 비어 있어 연결하지 않습니다"
    sid = account_id(acc)
    if not sid:
        return False, "세션 폴더 이름이 비어 있습니다"
    g = (grade or grade_of(acc)).strip().lower()
    if rank(g) < RANK[MANUAL]:
        return False, (f"검증 등급이 {LABEL.get(g, g)} 이라 연결하지 않습니다 "
                       f"(manual 이상에서만 연결)")
    if not session_store.session_dir(acc).exists():
        return False, f"세션 폴더가 없습니다({sid})"
    mapped = norm_id(account_map(acc).get("login_id"))
    if mapped and not same_login(mapped, lid):
        return False, (f"그 세션 폴더는 다른 로그인 계정({mapped})의 것입니다 — "
                       f"연결하지 않습니다")
    # ★매핑이 아직 비어 있어도(= '처음 연결'), 계정 등록표가 말하는 login_id 와
    #   어긋나면 연결하지 않는다(2026-11-04). 예전에는 매핑이 비면 **어떤
    #   login_id 든** 그 세션에 붙을 수 있었다 — 운영 경로에서는 같은 값이
    #   들어오므로 동작 변화는 없고, 잘못 부르는 경우만 막힌다.
    own = norm_id(getattr(acc, "login_id", ""))
    if own and not same_login(own, lid):
        return False, (f"그 세션은 {own} 계정의 것입니다 — {lid} 를 "
                       f"연결하지 않습니다")

    table = _read(ALIAS_PATH)
    rows = dict(table.get("aliases") or {})
    for key, row in list(rows.items()):
        if not same_login(key, lid):
            continue
        cur = str((row or {}).get("session_id") or "")
        if cur and cur != sid:
            return False, (f"login_id={lid} 는 이미 세션 {cur} 에 연결돼 있습니다 — "
                           f"덮어쓰지 않습니다")
        rows.pop(key)
    rows[lid] = {"session_id": sid, "grade": g, "linked_at": now_iso(),
                 "ref_tab": ref_tab or "", "brand": brand or ""}
    table["aliases"] = rows
    table["updated_at"] = now_iso()
    _write(ALIAS_PATH, table)
    return True, f"login_id={lid} → 세션 {sid} 연결(등급 {LABEL.get(g, g)})"


def unlink_session(login_id: str) -> bool:
    """별칭만 끊는다(세션 파일은 건드리지 않는다)."""
    lid = norm_id(login_id)
    table = _read(ALIAS_PATH)
    rows = dict(table.get("aliases") or {})
    hit = [k for k in rows if same_login(k, lid)]
    for k in hit:
        rows.pop(k)
    if not hit:
        return False
    table["aliases"], table["updated_at"] = rows, now_iso()
    _write(ALIAS_PATH, table)
    return True


# ══════════════════════════════════════════════════════════════════
#  로그아웃 대기 — **해당 login_id 만**. 다른 계정은 영향 없다.
# ══════════════════════════════════════════════════════════════════
def logout_pending() -> dict:
    return dict((_read(PENDING_PATH).get("pending") or {}))


def is_logout_pending(login_id: str) -> bool:
    lid = norm_id(login_id)
    if not lid:
        return False
    return any(same_login(k, lid) for k in logout_pending())


def mark_logout_pending(login_id: str, *, session_id: str = "",
                        reason: str = "") -> dict:
    """로그아웃 대기로 표시한다 — 이 상태에서는 **그 계정의 새 작업을 만들지 않는다.**

    ★이미 돌고 있는 배치(10건)는 그대로 끝낸다. 끊지 않는다.
    """
    lid = norm_id(login_id)
    if not lid:
        raise RuntimeError("login_id 가 없어 로그아웃 대기를 걸 수 없습니다.")
    table = _read(PENDING_PATH)
    rows = dict(table.get("pending") or {})
    rows[lid] = {"session_id": session_id or "", "since": now_iso(),
                 "reason": reason or "사용자 요청"}
    table["pending"], table["updated_at"] = rows, now_iso()
    _write(PENDING_PATH, table)
    return rows[lid]


def clear_logout_pending(login_id: str) -> bool:
    lid = norm_id(login_id)
    if not lid:                      # ★빈 값으로 전체를 지우는 일이 없게
        return False
    table = _read(PENDING_PATH)
    rows = dict(table.get("pending") or {})
    hit = [k for k in rows if same_login(k, lid)]
    for k in hit:
        rows.pop(k)
    if not hit:
        return False
    table["pending"], table["updated_at"] = rows, now_iso()
    _write(PENDING_PATH, table)
    return True


# ══════════════════════════════════════════════════════════════════
#  세션 공유 영향 범위 (2026-11-03)
#    로그아웃은 **세션 폴더 하나**를 초기화한다. 그 폴더를 다른 계정도 쓰고 있다면
#    그 계정들도 재로그인이 필요해진다 → **먼저 보여 주고**, 로그인 계정이 서로
#    다르면(= 공유가 맞는지 불분명하면) 자동 로그아웃을 **막는다**.
#
#    같은 폴더를 쓰게 되는 경로는 셋뿐이다
#      ① 같은 `id`            — 기본값 sessions/<id> (id 는 유일하므로 자기 자신)
#      ② `profile_dir` 지정   — 다른 계정이 같은 프로필 경로를 가리킬 수 있다
#      ③ login_id 별칭        — login_id → session_id 표가 이 폴더를 가리킨다
#    ★`blog_id` 는 보지 않는다 — 같은 blog_id 라도 로그인 계정이 다를 수 있다.
# ══════════════════════════════════════════════════════════════════
def session_peers(acc, path=None) -> list[dict]:
    """이 계정과 **같은 세션 폴더**를 쓰는 다른 계정들. 없으면 빈 목록.

    각 항목: {"id", "label", "login_id", "blog_id", "brand", "ref_tab", "why"}
    """
    from . import accounts as _accounts
    from . import session_store

    me = account_id(acc)
    try:
        mine = session_store.profile_dir(acc).resolve()
    except Exception:                                          # noqa: BLE001
        mine = None
    try:
        rows = _accounts.load_accounts(path, include_disabled=True)
    except Exception:                                          # noqa: BLE001
        return []                             # 목록을 못 읽으면 '모른다' → 호출한 쪽이 판단

    out: list[dict] = []
    for other in rows:
        if other.id == me:
            continue
        why = ""
        if (other.profile_dir or "").strip() and mine is not None:
            try:
                if session_store.profile_dir(other).resolve() == mine:
                    why = "profile_dir 가 같은 폴더를 가리킵니다"
            except Exception:                                  # noqa: BLE001
                pass
        if not why and norm_id(other.login_id) \
                and session_for_login(other.login_id) == me:
            why = f"login_id={other.login_id} 별칭이 이 세션을 가리킵니다"
        if why:
            out.append({"id": other.id, "label": other.label or other.id,
                        "login_id": norm_id(other.login_id),
                        "blog_id": norm_id(other.blog_id),
                        "brand": other.brand or "", "ref_tab": other.ref_tab or "",
                        "why": why})
    return out


def logout_allowed(acc, path=None) -> tuple[bool, list[dict], str]:
    """`(바로 로그아웃해도 되나, 영향받는 계정들, 사유)`.

    · 공유 계정이 없으면        → 바로 가능
    · 공유 계정의 login_id 가 **이 계정과 같으면** → 같은 로그인 계정이므로 가능
    · 그 밖에(login_id 가 다르거나 비어 있으면) → **금지**(사용자 확인 필요)
    """
    peers = session_peers(acc, path)
    if not peers:
        return True, [], ""
    mine = norm_id(getattr(acc, "login_id", "")) \
        or norm_id(account_map(acc).get("login_id"))
    odd = [p for p in peers if not (mine and same_login(p["login_id"], mine))]
    if not odd:
        return True, peers, (f"같은 로그인 계정({mine})의 다른 등록 {len(peers)}건도 "
                             f"함께 재로그인이 필요해집니다")
    names = " · ".join(f"{p['label']}(id={p['id']}, "
                       f"login_id={p['login_id'] or '없음'})" for p in odd[:4])
    return False, peers, (f"이 세션을 쓰는 다른 계정이 있고 로그인 계정이 같은지 "
                          f"확인되지 않았습니다 — {names}")


def describe(acc, *, expected_login_id: str = "") -> dict:
    """화면·CLI 에 보여줄 한 덩어리(브라우저를 켜지 않는다)."""
    from . import session_store

    info = session_store.describe(acc)
    st = auth_state(acc)
    amap = account_map(acc)
    lid = norm_id(expected_login_id) or norm_id(amap.get("login_id"))
    g = (st.get("grade") or UNAVAILABLE).strip().lower()
    return {
        "session_id": account_id(acc),
        "login_id": lid,
        "blog_id": norm_id(st.get("blog_id")) or norm_id(amap.get("blog_id"))
                   or info.get("blog_id") or "",
        "grade": g, "grade_label": LABEL.get(g, g), "rank": rank(g),
        "mode": mode(),
        "verified_at": st.get("verified_at") or "",
        "logged_in": bool(info.get("state_exists")),
        "cookies": info.get("cookies") or 0,
        "saved_at": info.get("saved_at") or "",
        "blog_verified": bool(st.get("blog_verified")),
        "logout_pending": is_logout_pending(lid),
        "alias_session": session_for_login(lid),
        "profile": info.get("profile") or "",
        "warn": warn_text(acc),
    }
