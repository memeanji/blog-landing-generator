# -*- mode: python ; coding: utf-8 -*-
"""BlogLandingAgent — PyInstaller 빌드 설정.

    .\.venv\Scripts\python.exe -m PyInstaller installer\agent.spec --noconfirm

★사용자 PC 에는 파이썬도 Playwright 도 없다. 여기서 전부 싸 넣는다.
  크롬(Chromium)만 첫 실행 때 내려받는다(설치파일을 400MB 로 만들지 않기 위해).
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent          # noqa: F821  (PyInstaller 가 넣어 준다)

hidden = (
    collect_submodules("v2")
    + collect_submodules("playwright")
    + ["pystray._win32", "PIL.Image", "PIL.ImageDraw",
       "tkinter", "tkinter.simpledialog", "tkinter.filedialog",
       "tkinter.messagebox", "gspread", "google.oauth2.service_account",
       # ★서버 발급 토큰 방식(v2/gauth.py)이 쓰는 것들 — private_key 없이 인증한다
       "google.auth.credentials", "google.auth.transport.requests",
       "google.oauth2.credentials",
       "requests", "dotenv"]
)

datas = collect_data_files("playwright") + collect_data_files("gspread")

a = Analysis(                                                    # noqa: F821
    [str(ROOT / "agent_tray.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["streamlit", "pandas", "pyarrow", "matplotlib", "numpy.testing",
              "pytest", "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)                                                # noqa: F821

exe = EXE(                                                       # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BlogLandingAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # ★검은 창이 뜨지 않게(트레이 앱)
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(                                                  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BlogLandingAgent",
)
