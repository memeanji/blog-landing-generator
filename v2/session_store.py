r"""계정별 네이버 로그인 세션 보관 — `sessions/<account>/`.

    sessions/
      my_account/
        profile/      ← Chromium persistent context (기기 등록·2차 인증 흔적이 남는다)
        state.json    ← storage_state (쿠키). ★NID_SES 는 **세션 쿠키**라 프로필 파일에
                        남지 않는다 — 창을 닫기 전에 여기로 따로 빼 둬야 다음 실행에서 산다.
        meta.json     ← 마지막 로그인 시각 / 확인된 blog_id (표시용)

★기존 `playwright-profile` 은 건드리지 않는다. `--account` 를 주지 않으면 예전 그대로
  `playwright-profile` 을 쓴다(기존 CLI·자동화 동작 보존).
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from .accounts import account_id
from .appdir import ROOT      # 개발 PC=프로젝트 폴더 / 설치본=%APPDATA%\BlogLandingAgent

# ★예전에는 이 파일 위치를 기준으로 삼았다. 설치본에서는 그게 **프로그램 폴더**
#   (…\BlogLandingAgent\_internal) 라서, 로그인 세션이 거기 저장됐다.
#   프로그램을 새로 깔면 지워지고, 화면도 그 폴더를 보지 않아 "로그인 안 됨" 으로
#   보였다. 다른 자료(계정·작업함)와 같은 곳에 두는 것이 맞다.
SESSIONS_ROOT = ROOT / "sessions"
LEGACY_PROFILE = ROOT / "playwright-profile"

# 네이버 로그인에 필요한 쿠키만 남긴다(광고·추적 쿠키까지 다 심으면 add_cookies 가 잘 깨진다).
KEEP_DOMAINS = ("naver.com", ".naver.com", "nid.naver.com", "blog.naver.com")


def session_dir(acc) -> Path:
    ident = account_id(acc)
    if not ident:
        raise RuntimeError("계정 id 가 비어 있습니다.")
    return SESSIONS_ROOT / ident


def profile_dir(acc) -> Path:
    """persistent context 폴더. Account.profile_dir 로 위치를 직접 지정할 수도 있다."""
    override = getattr(acc, "profile_dir", "") or ""
    if override:
        p = Path(override)
        return p if p.is_absolute() else (ROOT / p).resolve()
    return session_dir(acc) / "profile"


def state_path(acc) -> Path:
    return session_dir(acc) / "state.json"


def meta_path(acc) -> Path:
    return session_dir(acc) / "meta.json"


def ensure(acc) -> Path:
    d = profile_dir(acc)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keep(cookie: dict) -> bool:
    dom = str(cookie.get("domain") or "")
    return any(dom == d or dom.endswith(d) for d in KEEP_DOMAINS)


def save_state(acc, cookies: list, blog_id: str = "", origins: list | None = None) -> Path:
    """로그인 창에서 들고 나온 쿠키를 계정 폴더에 저장한다."""
    ident = account_id(acc)
    keep = [c for c in (cookies or []) if _keep(c)]
    path = state_path(acc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"cookies": keep, "origins": origins or [],
         "saved_at": datetime.now().isoformat(timespec="seconds"),
         "account": ident, "blog_id": blog_id},
        ensure_ascii=False), encoding="utf-8")
    write_meta(acc, blog_id=blog_id, cookies=len(keep))
    return path


def load_state(acc) -> dict:
    path = state_path(acc)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def load_cookies(acc) -> list:
    cookies = load_state(acc).get("cookies")
    return [c for c in cookies if isinstance(c, dict)] if isinstance(cookies, list) else []


def write_meta(acc, **fields) -> Path:
    path = meta_path(acc)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            data = {}
    data.update({k: v for k, v in fields.items() if v not in (None, "")})
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_state(acc, profile: bool = False) -> list[str]:
    """저장된 세션을 지운다(`--relogin`). profile=True 면 프로필 폴더까지 통째로."""
    gone = []
    for p in (state_path(acc), meta_path(acc)):
        if p.exists():
            try:
                p.unlink()
                gone.append(p.name)
            except Exception:                                  # noqa: BLE001
                pass
    if profile:
        d = profile_dir(acc)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name + "/")
    return gone


def describe(acc) -> dict:
    """GUI·CLI 에 보여줄 세션 현황(브라우저를 켜지 않는다)."""
    state, meta = load_state(acc), {}
    if meta_path(acc).exists():
        try:
            meta = json.loads(meta_path(acc).read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            meta = {}
    prof = profile_dir(acc)
    return {
        "id": account_id(acc),
        "profile": str(prof),
        "profile_exists": prof.exists(),
        "state": str(state_path(acc)),
        "state_exists": bool(state),
        "cookies": len(state.get("cookies") or []),
        "saved_at": state.get("saved_at") or meta.get("updated_at") or "",
        "blog_id": state.get("blog_id") or meta.get("blog_id") or "",
    }


def adopt_legacy_profile(acc, src: Path | None = None, overwrite: bool = False) -> Path:
    """기존 `playwright-profile` 을 계정 폴더로 복사한다.

    ★쿠키가 아니라 **기기 등록 흔적**을 옮기는 게 목적이다. 이걸 해 두면 이미 쓰던 계정은
      2차 인증(새로운 기기)을 다시 겪지 않는다. 원본은 그대로 둔다.
    """
    source = Path(src) if src else LEGACY_PROFILE
    if not source.exists():
        raise RuntimeError(f"복사할 프로필이 없습니다: {source}")
    dest = profile_dir(acc)
    if dest.exists():
        if not overwrite:
            raise RuntimeError(f"이미 프로필이 있습니다: {dest} (덮어쓰려면 --overwrite)")
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 잠금 파일은 빼고 복사한다(열려 있던 흔적이 남으면 '이미 사용 중' 이 된다).
    shutil.copytree(source, dest,
                    ignore=shutil.ignore_patterns("SingletonLock", "SingletonCookie",
                                                  "SingletonSocket", "*.lock",
                                                  "Crashpad", "ShaderCache", "GrShaderCache"))
    write_meta(acc, adopted_from=str(source))
    return dest
