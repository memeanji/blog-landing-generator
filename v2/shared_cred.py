r"""사내 공유폴더의 구글 인증 JSON을 이 PC로 가져오는 길 — **경로를 한 곳에서만 정한다.**

왜 있는가(2026-09-14 요청)
  PC 마다 사람이 파일 탐색기를 열어 JSON 을 찾아 고르는 대신, 사내 공유폴더에 둔 파일을
  트레이 버튼 하나로 가져오게 한다. 실패하면 기존 수동 선택으로 돌아간다(fallback).

어디를 뒤지나 — 먼저 찾은 것을 쓴다
  ① 환경변수 `BLOG_SHARED_CRED`            파일 또는 폴더 경로
  ② `agent_config.json` 의 `shared_credential_path`   파일 또는 폴더 경로
  ③ `DEFAULT_DIRS` 의 폴더들 (아래 목록)
  폴더를 받으면 그 안에서 `CRED_NAME` 파일을 찾는다(하위 폴더 1단계까지).

★경로를 바꿀 때 **프로그램을 다시 빌드할 필요가 없다** — `agent_config.json` 에
  `"shared_credential_path": "\\\\서버\\공유\\폴더"` 한 줄을 넣으면 그걸 먼저 본다.
  (트레이 [사내 인증파일 가져오기] 가 못 찾으면 그 자리에서 경로를 물어보고 여기에 저장한다.)

⚠️원본 파일은 **읽기만** 한다. 내용을 고치거나 원본을 지우지 않는다.
⚠️이 파일에는 **경로 규칙만** 있고 인증값은 없다 — 설치본·깃허브에 비밀이 섞이지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .appdir import ROOT

# 사내에서 쓰기로 한 파일 이름(이름이 바뀌면 여기만 고친다)
CRED_NAME = "블로그랜딩_구글인증_0914.json"

# 이 PC 안의 최종 저장 위치(내부 고정명 — 기존 구조 그대로)
LOCAL_DEST = ROOT / "google-service-account.json"

# 설정 파일에서 공유 경로를 적어 두는 키
CONFIG_KEY = "shared_credential_path"

# 후보 폴더(있을 법한 곳부터). 없는 경로는 조용히 건너뛴다.
DEFAULT_DIRS: list[str] = [
    r"%USERPROFILE%\SynologyDrive\00_공용\인증",
    r"%USERPROFILE%\SynologyDrive\인증",
    r"Z:\인증",
    r"\\8949plus\공유\인증",
]


def _config_path() -> Path:
    return ROOT / "agent_config.json"


def load_config() -> dict:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return {}


def save_config(**kv) -> None:
    """agent_config.json 에 키를 덧쓴다(다른 값은 보존)."""
    data = load_config()
    data.update(kv)
    _config_path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")


def candidates() -> list[Path]:
    """뒤져 볼 위치를 순서대로. (파일 경로일 수도, 폴더일 수도 있다)"""
    out: list[Path] = []
    env = (os.getenv("BLOG_SHARED_CRED") or "").strip().strip('"')
    if env:
        out.append(Path(os.path.expandvars(env)))
    cfg = str(load_config().get(CONFIG_KEY) or "").strip().strip('"')
    if cfg:
        out.append(Path(os.path.expandvars(cfg)))
    out += [Path(os.path.expandvars(d)) for d in DEFAULT_DIRS]
    return out


def _pick(base: Path) -> Path | None:
    """경로가 파일이면 그대로, 폴더면 그 안(+하위 1단계)에서 CRED_NAME 을 찾는다."""
    try:
        if base.is_file():
            return base
        if not base.is_dir():
            return None
        direct = base / CRED_NAME
        if direct.is_file():
            return direct
        for child in base.iterdir():                           # 하위 1단계까지만
            if child.is_dir():
                p = child / CRED_NAME
                if p.is_file():
                    return p
    except Exception:                                          # noqa: BLE001 — 권한/네트워크 오류는 '없음'으로
        return None
    return None


def find() -> tuple[Path | None, list[str]]:
    """공유폴더에서 인증 JSON 찾기. 반환 (찾은 파일 or None, 뒤져 본 곳 목록)."""
    looked: list[str] = []
    for base in candidates():
        looked.append(str(base))
        hit = _pick(base)
        if hit:
            return hit, looked
    return None, looked


def copy_to_local(src: Path) -> Path:
    """공유폴더 → 이 PC 로 복사하고 설정에 경로를 적는다. 원본은 건드리지 않는다."""
    src = Path(src)
    LOCAL_DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, LOCAL_DEST)                           # 내용 그대로
    save_config(google_service_account_path=str(LOCAL_DEST))
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = str(LOCAL_DEST)
    return LOCAL_DEST


def remember_dir(path) -> None:
    """사람이 직접 고른 위치를 다음부터 먼저 보도록 저장."""
    p = Path(path)
    save_config(**{CONFIG_KEY: str(p if p.is_dir() else p.parent)})
