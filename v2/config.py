"""환경설정 — 기존 blog_landing_generator/.env 를 그대로 재사용한다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .appdir import ROOT      # 개발 PC=프로젝트 폴더 / 설치본=%APPDATA%\BlogLandingAgent


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    service_account_json: Path
    spreadsheet_id: str
    blog_home_url: str
    headless: bool
    user_data_dir: Path
    out_dir: Path
    # ★계정을 고른 실행에서만 채워진다. 비어 있으면 예전과 똑같이 동작한다
    #   (프로필 = playwright-profile, 세션 파일 저장 안 함).
    account: str = ""

    def check(self) -> None:
        if not self.service_account_json or not self.service_account_json.exists():
            raise RuntimeError(f"서비스 계정 JSON 을 찾을 수 없습니다: {self.service_account_json}")
        if not self.spreadsheet_id:
            raise RuntimeError("REFERENCE_SPREADSHEET_ID 가 .env 에 없습니다.")


def service_account_path() -> Path:
    """서비스 계정 JSON 경로.

    1) `GOOGLE_SERVICE_ACCOUNT_JSON` 이 가리키는 **파일**이 있으면 그대로 쓴다(로컬, 기존 동작).
    2) 없으면 `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`(JSON 문자열)를 임시 파일로 떨어뜨려 쓴다.
       — Streamlit Community Cloud 처럼 파일을 둘 수 없는 곳에서 Secrets 로 넣기 위한 길.
         (Streamlit 은 최상위 secrets 를 환경변수로도 넣어 준다)
    ★로컬 동작은 1) 로 끝나므로 달라지는 것이 없다.
    """
    raw = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if raw and Path(raw).exists():
        return Path(raw)

    content = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT") or "").strip()
    if content:
        import hashlib
        import tempfile

        tag = hashlib.sha1(content.encode("utf-8")).hexdigest()[:8]
        path = Path(tempfile.gettempdir()) / f"blog_landing_sa_{tag}.json"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            try:                                   # 남이 못 읽게(가능한 환경에서만)
                path.chmod(0o600)
            except Exception:                      # noqa: BLE001
                pass
        return path
    return Path(raw) if raw else Path("")


def load_settings(account=None) -> Settings:
    """`.env` 를 읽어 Settings 를 만든다.

    `account` 를 주면(Account 또는 계정 id) 브라우저 프로필을
    **`sessions/<account>/profile`** 로 바꾼다 — 계정끼리 세션이 섞이지 않는다.
    주지 않으면 예전처럼 `.env` 의 `PLAYWRIGHT_USER_DATA_DIR` 하나를 쓴다.
    """
    load_dotenv(ROOT / ".env")
    cred = service_account_path()
    udd = (os.getenv("PLAYWRIGHT_USER_DATA_DIR") or "playwright-profile").strip()
    out = ROOT / "out"
    out.mkdir(parents=True, exist_ok=True)

    from .accounts import account_id                 # 지연 import (순환 방지)

    acc_id = account_id(account)
    if acc_id:
        from . import session_store
        user_data_dir = session_store.ensure(account)
    else:
        user_data_dir = (ROOT / udd).resolve()

    # ★이 값은 실제 조회에 쓰이지 않는다(기준시트는 브랜드 설정으로 정해진다).
    #   .env 가 없는 환경(Cloud)에서 check() 가 헛되이 막지 않도록 브랜드 값을 기본으로 둔다.
    from .brands import default_brand

    sheet_id = ((os.getenv("REFERENCE_SPREADSHEET_ID") or "").strip()
                or default_brand().reference_sheet_id)

    return Settings(
        service_account_json=Path(cred) if cred else Path(""),
        spreadsheet_id=sheet_id,
        blog_home_url=(os.getenv("NAVER_BLOG_HOME_URL")
                       or "https://blog.naver.com/MyBlog.naver").strip(),
        headless=_bool(os.getenv("PLAYWRIGHT_HEADLESS"), False),
        user_data_dir=user_data_dir,
        out_dir=out,
        account=acc_id,
    )


def resolve_headless(args, settings: Settings) -> Settings:
    """`--headless` / `--no-headless` 를 반영한 Settings 를 돌려준다.

    ★2026-08-25 사용자 지시(임시): **검수용은 기본 headless** — 창이 뜨지 않아
      다른 작업을 방해하지 않는다. 실전용은 기존대로 창을 띄운다.
      프로그램(정식 툴)으로 만들 때 이 기본값은 다시 정한다.
    ★`--relogin` 은 사람이 직접 로그인해야 하므로 headless 를 강제로 끈다.
    """
    from dataclasses import replace

    want = getattr(args, "headless", None)
    if want is None:
        kind = getattr(args, "ref_kind", None) or getattr(args, "kind", "검수용")
        want = (kind == "검수용")
    if want and getattr(args, "relogin", False):
        want = False
    return settings if want == settings.headless else replace(settings, headless=want)
