r"""데이터 폴더 한 곳 — 개발 PC 와 설치본(EXE)이 서로 다른 곳을 쓰게 한다.

    개발 PC   : 프로젝트 폴더 (지금까지와 100% 동일)
    설치본     : %APPDATA%\BlogLandingAgent

★설치본에서는 프로그램 폴더가 읽기 전용(또는 임시폴더)이라 그 안에 로그·세션을
  쓸 수 없다. 그래서 **쓰는 것은 전부 이 폴더 아래**로 모은다.
      sessions/   네이버 로그인 세션(쿠키·프로필) ← 이 PC 밖으로 절대 안 나간다
      out/        실행 로그·스크린샷
      queue/      로컬 큐(원격 큐를 못 쓸 때의 대비)
      accounts.json · brands.json · device.json · agent_config.json
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 프로젝트 폴더(개발 PC 기준)
SRC_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    """PyInstaller 로 묶인 실행 파일인가."""
    return bool(getattr(sys, "frozen", False))


def data_root() -> Path:
    """읽고 쓰는 파일이 모이는 곳."""
    override = (os.getenv("BLOG_DATA_DIR") or "").strip()
    if override:
        p = Path(override)
    elif frozen():
        p = Path(os.getenv("APPDATA") or Path.home()) / "BlogLandingAgent"
    else:
        p = SRC_ROOT
    p.mkdir(parents=True, exist_ok=True)
    return p


ROOT = data_root()


def sub(name: str) -> Path:
    p = ROOT / name
    p.mkdir(parents=True, exist_ok=True)
    return p
