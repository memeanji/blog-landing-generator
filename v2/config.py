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


CRED_HELP = (
    "구글 시트를 읽으려면 **서비스계정 JSON** 이 이 PC 에 있어야 합니다.\n"
    "  · 설치본(트레이 앱): 트레이 아이콘 우클릭 → [구글 시트 인증 파일…] 에서 JSON 을 고르세요.\n"
    "  · 직접 실행(리포)  : .env 의 GOOGLE_SERVICE_ACCOUNT_JSON 에 JSON **파일 경로**를 적으세요.\n"
    "  · 파일을 둘 수 없는 환경: GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT 에 JSON 내용을 넣으세요."
)


@dataclass(frozen=True)
class Settings:
    # ★None = '아직 지정 안 됨'. 예전엔 Path("") 를 썼는데 pathlib 이 그것을 **'.'(현재 폴더)**
    #   으로 바꿔 버려서, 아래 check() 의 exists() 가 통과하고 한참 뒤 시트를 열 때
    #   `PermissionError: [Errno 13] Permission denied: '.'` 라는 엉뚱한 오류가 났다.
    service_account_json: Path | None
    spreadsheet_id: str
    blog_home_url: str
    headless: bool
    user_data_dir: Path
    out_dir: Path
    # ★계정을 고른 실행에서만 채워진다. 비어 있으면 예전과 똑같이 동작한다
    #   (프로필 = playwright-profile, 세션 파일 저장 안 함).
    account: str = ""

    def check(self) -> None:
        # ★인증 가능 여부는 gauth 가 판단한다 — 로컬 JSON(①②)이 없어도
        #   **페어링된 기기(③)** 면 서버가 단기 토큰을 준다(새 노트북용 길).
        from . import gauth

        gauth.ensure_available()
        if not self.spreadsheet_id:
            # ★설치본에는 .env 가 없는 게 정상이다 — 시트 ID 는 **브랜드 설정**에서 온다
            #   (화면이 작업마다 BLOG_BRANDS_JSON 으로 보낸다). 그래서 원인을 그렇게 적는다.
            raise RuntimeError(
                "기준시트 ID 를 알 수 없습니다 — 브랜드 설정이 비어 있습니다.\n"
                "  · 화면에서 실행하면 브랜드 설정이 함께 전달됩니다(BLOG_BRANDS_JSON).\n"
                "  · 터미널로 직접 돌릴 때는 brands.json 또는 .env 의 "
                "REFERENCE_SPREADSHEET_ID 가 필요합니다.")


_dotenv_done = False


def _ensure_dotenv() -> None:
    """`.env` 를 한 번만 읽어 둔다(이미 있는 환경변수는 덮지 않는다 — dotenv 기본값)."""
    global _dotenv_done
    if _dotenv_done:
        return
    _dotenv_done = True
    try:
        load_dotenv(ROOT / ".env")
    except Exception:                                          # noqa: BLE001
        pass


def require_credential(cred) -> Path:
    """서비스계정 JSON 경로를 **쓰기 직전에** 검증한다. 통과하면 그 경로를 돌려준다.

    ★여기서 막지 않으면 `open()` 이 대신 실패하는데, 경로가 폴더('.')면 메시지가
      `Permission denied: '.'` 로 나와서 원인이 전혀 안 보인다(2026-09-10 실제 발생).
      그래서 ①미지정 ②없는 파일 ③폴더 를 각각 다른 문구로 구분해 막는다.
    """
    if cred is None or str(cred).strip() in ("", "."):
        raise RuntimeError("구글 서비스계정 JSON 이 지정돼 있지 않습니다.\n" + CRED_HELP)
    p = Path(cred)
    if p.is_dir():
        raise RuntimeError(
            f"구글 서비스계정 JSON 자리에 **폴더 경로**가 들어왔습니다: {p}\n"
            "JSON 파일 자체를 가리켜야 합니다.\n" + CRED_HELP)
    if not p.is_file():
        raise RuntimeError(f"구글 서비스계정 JSON 을 찾을 수 없습니다: {p}\n" + CRED_HELP)
    return p


def service_account_path() -> Path | None:
    """서비스 계정 JSON 경로.

    1) `GOOGLE_SERVICE_ACCOUNT_JSON` 이 가리키는 **파일**이 있으면 그대로 쓴다(로컬, 기존 동작).
    2) 없으면 `GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`(JSON 문자열)를 임시 파일로 떨어뜨려 쓴다.
       — Streamlit Community Cloud 처럼 파일을 둘 수 없는 곳에서 Secrets 로 넣기 위한 길.
         (Streamlit 은 최상위 secrets 를 환경변수로도 넣어 준다)
    ★로컬 동작은 1) 로 끝나므로 달라지는 것이 없다.
    """
    # ★.env 를 아직 안 읽은 경로에서 불려도 ①이 밀리지 않게 여기서 한 번 보장한다.
    #   (안 하면 진입점에 따라 로컬 파일을 두고도 서버 토큰으로 넘어간다.)
    _ensure_dotenv()
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
            # ⚠️윈도우에서는 읽기전용 속성만 바뀐다(ACL 아님) — 유닉스 600 과 다르다.
            #   이 파일은 임시폴더에 놓이므로 같은 PC 사용자는 읽을 수 있다고 봐야 한다.
            #   (이 경로는 파일을 못 두는 클라우드용이고, 새 PC 는 서버 토큰 방식을 쓴다.)
            try:
                path.chmod(0o600)
            except Exception:                      # noqa: BLE001
                pass
        return path
    # ★raw 가 비어 있으면 **None** 을 돌려준다 — 예전처럼 Path("") 를 돌려주면
    #   pathlib 이 '.'(현재 폴더)로 바꿔 놓아서 '지정 안 됨' 이 '폴더를 열어라' 가 된다.
    return Path(raw) if raw else None


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
        service_account_json=Path(cred) if cred else None,
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
