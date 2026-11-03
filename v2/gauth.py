r"""구글 인증을 정하는 **한 곳** — private_key 를 PC 에 두지 않는 길을 포함한다.

    ① GOOGLE_SERVICE_ACCOUNT_JSON          로컬 서비스계정 파일   (개발 PC·기존 환경)
    ② GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT  JSON 내용 직접         (Streamlit Cloud)
    ③ 페어링된 기기 → Edge Function `google-token` 이 발급한 **단기 access token**
    ④ 셋 다 없으면 무엇을 해야 하는지 알려주는 오류

★③ 이 새 노트북용 길이다. Agent 는 자기 `device_token` 만 보내고
  **private_key 는 받지도, 저장하지도 않는다.** 받는 건 1시간짜리 access token 뿐이고
  그 토큰도 디스크에 쓰지 않는다(이 모듈 안 메모리에만 산다).

★Agent 는 scope URL 을 고르지 않는다. `operation`("readonly"/"write") 만 보내고
  실제 scope 는 **서버 allowlist** 가 정한다(사용자 지시 2026-09-10).

로그 규칙: access token · device_token · private_key 원문은 **절대** 찍지 않는다.
           길이·만료시각·client_email(공개값)까지만 남긴다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

# 작업 종류 — Agent 가 서버에 보내는 값은 이 둘뿐이다.
OP_READONLY = "readonly"
OP_WRITE = "write"

# 참고용(실제 발급 scope 는 서버 allowlist 가 정한다). 로컬 파일 인증에만 쓴다.
_LOCAL_SCOPES = {
    OP_READONLY: ["https://www.googleapis.com/auth/spreadsheets.readonly"],
    OP_WRITE: ["https://www.googleapis.com/auth/spreadsheets"],
}

TOKEN_FUNCTION = "google-token"      # Supabase Edge Function 이름
REQUEST_TIMEOUT = 20
EARLY_REFRESH_SEC = 300              # ★만료 5분 전에 미리 새 토큰을 받는다

_lock = threading.Lock()
_remote: dict = {}                   # {operation: RemoteToken} — 프로세스 메모리 전용
_client_email = ""                   # 오류 안내에 쓰는 공개값(비밀 아님)


class GoogleAuthError(RuntimeError):
    pass


def _op(operation: str) -> str:
    if operation not in (OP_READONLY, OP_WRITE):
        raise GoogleAuthError(f"알 수 없는 작업 종류: {operation!r} "
                              f"({OP_READONLY} / {OP_WRITE} 만 가능)")
    return operation


# ── ①② 로컬 서비스계정 ────────────────────────────────────────────────
def local_path() -> Path | None:
    """①② 로 얻어지는 서비스계정 JSON 경로(없으면 None)."""
    from .config import service_account_path

    p = service_account_path()
    return p if (p and Path(p).is_file()) else None


def local_client_email(path: Path | None = None) -> str:
    p = path or local_path()
    if not p:
        return ""
    try:
        return str(json.loads(Path(p).read_text(encoding="utf-8"))
                   .get("client_email") or "")
    except Exception:                                          # noqa: BLE001
        return ""


# ── ③ 서버가 발급하는 단기 토큰 ────────────────────────────────────────
def _server() -> tuple[str, str, str]:
    """(supabase_url, publishable_key, device_token). 하나라도 비면 ('','','')."""
    from . import pairing

    cfg = pairing._config()
    token = (os.getenv("BLOG_DEVICE_TOKEN")
             or pairing.load_device().get("device_token") or "").strip()
    if not (cfg.get("url") and cfg.get("key") and token):
        return "", "", ""
    return cfg["url"].rstrip("/"), cfg["key"], token


def server_available() -> bool:
    """③ 을 쓸 수 있는 상태인가(페어링 완료 + 접속 정보 있음)."""
    return all(_server())


def _fetch_token(operation: str) -> dict:
    """Edge Function 에 토큰을 요청한다. **보내는 건 device_token 과 operation 뿐.**

    반환 {access_token, expires_at(ISO), scope, client_email}
    ⚠️예외 메시지에 device_token / access_token 이 섞이지 않게 직접 만들어 던진다.
    """
    import requests

    url, key, token = _server()
    if not url:
        raise GoogleAuthError("이 PC 는 아직 서버에 연결(페어링)돼 있지 않습니다.")
    endpoint = f"{url}/functions/v1/{TOKEN_FUNCTION}"
    try:
        r = requests.post(endpoint,
                          headers={"apikey": key, "Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"device_token": token, "operation": operation},
                          timeout=REQUEST_TIMEOUT)
    except Exception as exc:                                   # noqa: BLE001
        raise GoogleAuthError(
            f"구글 인증 서버에 연결하지 못했습니다({type(exc).__name__}). "
            "인터넷 연결을 확인해 주세요.") from None
    if r.status_code == 401:
        raise GoogleAuthError(
            "이 기기의 연결이 해제되었습니다(서버가 기기를 알아보지 못함).\n"
            "  → 화면에서 새 6자리 코드를 받아 다시 연결해 주세요.")
    if r.status_code == 429:
        raise GoogleAuthError("인증 요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.")
    if r.status_code != 200:
        raise GoogleAuthError(f"구글 인증 서버 오류(HTTP {r.status_code}).")
    try:
        data = r.json()
    except Exception:                                          # noqa: BLE001
        raise GoogleAuthError("구글 인증 서버가 알 수 없는 응답을 보냈습니다.") from None
    if not data.get("access_token"):
        raise GoogleAuthError("구글 인증 서버가 토큰을 주지 않았습니다.")
    return data


def _expiry_naive_utc(value, fallback_sec: int = 3600) -> dt.datetime:
    """서버가 준 만료시각 → google-auth 가 쓰는 **naive UTC**.
    ★EARLY_REFRESH_SEC 만큼 앞당겨 둔다 = 만료 5분 전에 자동 재발급된다."""
    now = dt.datetime.utcnow()
    exp = None
    if value:
        try:
            exp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            exp = (exp.astimezone(dt.timezone.utc).replace(tzinfo=None)
                   if exp.tzinfo else exp)
        except Exception:                                      # noqa: BLE001
            exp = None
    if exp is None:
        exp = now + dt.timedelta(seconds=fallback_sec)
    return exp - dt.timedelta(seconds=EARLY_REFRESH_SEC)


def _remote_credentials(operation: str):
    """서버 토큰을 쓰는 자격증명(만료되면 스스로 다시 받아온다)."""
    import google.auth.credentials as gcreds

    class RemoteToken(gcreds.Credentials):
        """private_key 없이 **서버가 준 단기 토큰**만 들고 있는 자격증명.

        google-auth 가 `valid` 를 볼 때 만료돼 있으면 `refresh()` 를 부르고,
        여기서 Edge Function 에 다시 요청한다 — 발행이 길어져도 끊기지 않는다.
        """

        def __init__(self, op: str) -> None:
            super().__init__()
            self._op = op
            self.token = None
            self.expiry = None

        def refresh(self, request) -> None:                    # noqa: D401
            global _client_email
            data = _fetch_token(self._op)
            self.token = data["access_token"]
            self.expiry = _expiry_naive_utc(data.get("expires_at"))
            _client_email = str(data.get("client_email") or "") or _client_email

        # 토큰이 실수로 로그·traceback 에 찍히지 않도록 표현을 고정한다.
        def __repr__(self) -> str:
            return f"<RemoteToken op={self._op} valid={self.valid}>"

    with _lock:
        cred = _remote.get(operation)
        if cred is None:
            cred = RemoteToken(operation)
            _remote[operation] = cred          # ★scope(작업)별로 따로 보관
    return cred


# ── 공개 API ──────────────────────────────────────────────────────────
def credentials(operation: str = OP_READONLY):
    """이번 작업에 쓸 구글 자격증명. 우선순위 ①②③, 없으면 ④(오류)."""
    operation = _op(operation)
    p = local_path()
    if p:                                                      # ①②
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(
            str(p), scopes=list(_LOCAL_SCOPES[operation]))
    if server_available():                                     # ③
        return _remote_credentials(operation)
    raise GoogleAuthError(help_text())                         # ④


def client_email() -> str:
    """오류 안내에 쓰는 서비스계정 주소(공개값). 모르면 빈 문자열."""
    return local_client_email() or _client_email


def source() -> str:
    """지금 어느 길로 인증하는지 — 로그·상태창에 찍는 짧은 이름."""
    if local_path():
        return "로컬 서비스계정 파일"
    if server_available():
        return "서버 발급 토큰(페어링)"
    return "없음"


def ensure_available() -> str:
    """실행 전 점검. 쓸 수 있으면 그 길의 이름, 아니면 안내 오류."""
    s = source()
    if s == "없음":
        raise GoogleAuthError(help_text())
    return s


def _configured_but_unusable() -> str:
    """경로를 적어 뒀는데 그게 파일이 아닌 경우 — 무엇이 잘못됐는지 먼저 알려준다."""
    raw = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw or Path(raw).is_file():
        return ""
    what = "폴더입니다" if Path(raw).is_dir() else "그런 파일이 없습니다"
    return (f"※ GOOGLE_SERVICE_ACCOUNT_JSON 에 적힌 경로를 쓸 수 없습니다 — {what}:\n"
            f"   {raw}\n")


def help_text() -> str:
    return (
        _configured_but_unusable() +
        "구글 시트에 접근할 방법이 없습니다.\n"
        "  · 이 PC 를 화면에 연결(페어링)하면 인증 파일 없이 바로 쓸 수 있습니다 —\n"
        "    트레이 아이콘 우클릭 → [연결] 에서 화면의 6자리 코드를 입력하세요.\n"
        "  · 또는 서비스계정 JSON 을 직접 지정: 트레이 메뉴 [구글 시트 인증 파일…]\n"
        "    (터미널로 돌릴 때는 .env 의 GOOGLE_SERVICE_ACCOUNT_JSON)")


def reset_cache() -> None:
    """테스트용 — 메모리에 남은 토큰을 버린다(디스크에는 원래 없다)."""
    with _lock:
        _remote.clear()
