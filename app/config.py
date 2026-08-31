from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    enable_external_actions: bool
    google_service_account_json: Path | None
    reference_spreadsheet_id: str
    naver_blog_home_url: str
    playwright_headless: bool
    playwright_user_data_dir: Path


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    credential_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    user_data_dir = os.getenv("PLAYWRIGHT_USER_DATA_DIR", "playwright-profile").strip()

    return Settings(
        enable_external_actions=_as_bool(os.getenv("ENABLE_EXTERNAL_ACTIONS"), False),
        google_service_account_json=Path(credential_path) if credential_path else None,
        reference_spreadsheet_id=os.getenv("REFERENCE_SPREADSHEET_ID", "").strip(),
        naver_blog_home_url=os.getenv("NAVER_BLOG_HOME_URL", "https://blog.naver.com/MyBlog.naver").strip(),
        playwright_headless=_as_bool(os.getenv("PLAYWRIGHT_HEADLESS"), False),
        playwright_user_data_dir=(BASE_DIR / user_data_dir).resolve(),
    )

