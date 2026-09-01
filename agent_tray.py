r"""블로그 랜딩 Agent — 작업표시줄 트레이에서 조용히 도는 실행기.

    사용자는 설치만 하면 된다. 그 뒤로는 컴퓨터를 켤 때 자동으로 떠서
    화면(Streamlit)이 보낸 **자기 PC 작업만** 가져와 실행한다.

    트레이 메뉴
      · 상태 보기            연결·큐·세션 현황
      · 이 PC 연결(6자리)    화면에서 받은 코드를 넣어 연결
      · 구글 시트 인증 파일   시트를 읽기 위한 서비스계정 JSON 지정(1회)
      · 로그 폴더 열기
      · 윈도우 시작 시 자동 실행(체크)
      · 종료

    ★이 파일은 설치본의 진입점이기도 하다.
      `BlogLandingAgent.exe --module v2.run …` 처럼 불리면 그 모듈을 대신 실행한다
      (설치본에는 python.exe 가 없어서 자식 프로세스를 이렇게 띄운다).
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_NAME = "블로그 랜딩 Agent"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "BlogLandingAgent"


# ══════════════════════════════════════════════════════════════════
# 진입점
#   · 인자 없이 실행  →  트레이 Agent (평소 사용 방식)
#   · `--module <모듈>` →  그 모듈을 대신 실행 (설치본에는 python.exe 가 없어서
#                          자식 프로세스를 이렇게 띄운다)
#   · `--selftest`     →  필요한 부품이 다 들어갔는지 점검(문제 생겼을 때 확인용)
#
# ★`--module` 로 부를 수 있는 것은 **실행 진입점(main)이 있는 모듈만**이다.
#   `v2.brands` 같은 설정/도우미 모듈은 import 전용이라 여기서 실행하지 않는다.
# ══════════════════════════════════════════════════════════════════
RUNNABLE = {
    "v2.agent": "원격 큐를 보는 실행기",
    "v2.run": "검수용 랜딩 생성",
    "v2.run_production": "실전용 랜딩 전환",
    "v2.session": "네이버 로그인 창 / 세션 확인",
    "v2.pairing": "이 PC 연결(6자리 코드)",
    "v2.delete_posts": "글 삭제 도구",
}


def _run_module_and_exit() -> None:
    if len(sys.argv) < 3 or sys.argv[1] != "--module":
        return
    import importlib

    module, args = sys.argv[2], sys.argv[3:]
    if module not in RUNNABLE:
        nl = chr(10)
        usable = nl.join(f"         {k:20} {v}" for k, v in RUNNABLE.items())
        print(f"[오류] `{module}` 은 실행할 수 있는 모듈이 아닙니다.{nl}"
              f"       실행 가능한 모듈:{nl}{usable}", file=sys.stderr)
        sys.exit(2)
    mod = importlib.import_module(module)
    entry = getattr(mod, "main", None)
    if not callable(entry):
        print(f"[오류] `{module}` 에 실행 진입점(main)이 없습니다.", file=sys.stderr)
        sys.exit(2)
    sys.argv = [module, *args]
    sys.exit(entry())


def _selftest_and_exit() -> None:
    """설치본이 제대로 묶였는지 점검한다(트레이가 안 뜰 때 원인 찾기용)."""
    if "--selftest" not in sys.argv:
        return
    okay = True
    for name in ("v2.agent", "v2.run", "v2.run_production", "v2.session",
                 "v2.pairing", "v2.brands", "v2.queue_store", "v2.supabase_store",
                 "gspread", "requests", "playwright.sync_api", "pystray",
                 "PIL.Image", "tkinter", "dotenv"):
        try:
            __import__(name)
            print(f"  OK   {name}")
        except Exception as exc:                               # noqa: BLE001
            okay = False
            print(f"  실패 {name} — {type(exc).__name__}: {exc}")
    print("전부 정상" if okay else "★빠진 부품이 있습니다")
    sys.exit(0 if okay else 1)


_run_module_and_exit()
_selftest_and_exit()

from v2 import appdir, pairing                                  # noqa: E402
from v2.agent import serve                                      # noqa: E402
from v2.queue_store import get_store, host_name                 # noqa: E402

LOG_DIR = appdir.sub("out")
LOG_FILE = LOG_DIR / "agent_tray.log"
_events: "queue.Queue[str]" = queue.Queue()


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:                                          # noqa: BLE001
        pass
    print(line, flush=True)


# ══════════════════════════════════════════════════════════════════
# 윈도우 시작 시 자동 실행
# ══════════════════════════════════════════════════════════════════
def autostart_target() -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f'"{exe}"'
    return f'"{exe}" "{Path(__file__).resolve()}"'


def autostart_on() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(val)
    except Exception:                                          # noqa: BLE001
        return False


def set_autostart(on: bool) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_target())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        log(f"[자동실행] {'등록' if on else '해제'}")
        return True
    except Exception as exc:                                   # noqa: BLE001
        log(f"[자동실행] 실패: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════
# 첫 실행 — 크롬(Chromium) 자동 설치
# ══════════════════════════════════════════════════════════════════
def browsers_dir() -> Path:
    return Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH")
                or (appdir.ROOT / "browsers"))


def chromium_ready() -> bool:
    """설치 여부만 본다 — 드라이버를 띄우지 않는다(느리고 경고가 남는다)."""
    for base in (browsers_dir(),
                 Path(os.getenv("LOCALAPPDATA") or "") / "ms-playwright"):
        try:
            for d in base.glob("chromium-*"):
                if any(d.rglob("chrome.exe")):
                    return True
        except Exception:                                      # noqa: BLE001
            continue
    return False


def install_chromium(notify=None) -> bool:
    """없으면 내려받는다(수백 MB, 몇 분). 사용자는 아무것도 안 해도 된다."""
    if chromium_ready():
        return True
    if notify:
        notify("크롬을 내려받는 중입니다 — 몇 분 걸릴 수 있습니다.")
    log("[크롬] 설치 시작")
    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(appdir.sub("browsers")))
    try:
        if getattr(sys, "frozen", False):
            from playwright.__main__ import main as pw_main

            argv = sys.argv
            sys.argv = ["playwright", "install", "chromium"]
            try:
                pw_main()
            except SystemExit:
                pass
            finally:
                sys.argv = argv
        else:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           env=env, check=False)
    except Exception as exc:                                   # noqa: BLE001
        log(f"[크롬] 설치 실패: {exc}")
        if notify:
            notify(f"크롬 설치에 실패했습니다: {exc}")
        return False
    okay = chromium_ready()
    log(f"[크롬] 설치 {'완료' if okay else '실패'}")
    if notify:
        notify("크롬 준비 완료 — 이제 실행할 수 있습니다." if okay
               else "크롬 설치에 실패했습니다. 인터넷 연결을 확인해 주세요.")
    return okay


# ══════════════════════════════════════════════════════════════════
# 대화상자 (tkinter — 파이썬 기본 포함)
# ══════════════════════════════════════════════════════════════════
def ask_code() -> str:
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    code = simpledialog.askstring(
        APP_NAME, "화면에 뜬 6자리 연결 코드를 입력하세요.", parent=root)
    root.destroy()
    return (code or "").strip()


def ask_name(default: str = "") -> str:
    """이 PC 를 화면에서 어떻게 부를지. 비워 두면 컴퓨터 이름을 쓴다."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    name = simpledialog.askstring(
        APP_NAME,
        "이 컴퓨터를 화면에서 어떻게 표시할까요?\n"
        "(예: 민지 사무실 PC · 집 노트북)\n"
        "비워 두면 컴퓨터 이름을 씁니다.",
        initialvalue=default, parent=root)
    root.destroy()
    return (name or "").strip()


def ask_file(title: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(title=title, parent=root,
                                      filetypes=[("JSON 파일", "*.json")])
    root.destroy()
    return path or ""


def show(title: str, body: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showinfo(title, body, parent=root)
    root.destroy()


# ══════════════════════════════════════════════════════════════════
# 트레이
# ══════════════════════════════════════════════════════════════════
def make_icon(connected: bool):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = (34, 139, 76) if connected else (150, 150, 150)
    d.rounded_rectangle((6, 10, 58, 54), radius=10, fill=body)
    d.rectangle((16, 22, 48, 27), fill=(255, 255, 255))
    d.rectangle((16, 32, 40, 37), fill=(255, 255, 255))
    d.rectangle((16, 42, 44, 47), fill=(255, 255, 255))
    return img


class TrayAgent:
    def __init__(self) -> None:
        self.icon = None
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None

    # ── 상태 ────────────────────────────────────────────────────
    def connected(self) -> bool:
        return pairing.is_paired()

    def status_text(self) -> str:
        dev = pairing.load_device()
        cfg_ok = bool(pairing._config()["url"] and pairing._config()["key"])
        sa = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or ""
        lines = [
            f"연결 상태 : {'연결됨' if dev else '연결 안 됨'}",
            f"PC 이름   : {dev.get('label') or host_name()}",
            f"접속 설정 : {'있음' if cfg_ok else '없음'}",
            f"구글 인증 : {'있음' if sa and Path(sa).exists() else '없음(시트를 읽으려면 필요)'}",
            f"크롬      : {'준비됨' if chromium_ready() else '미설치'}",
            f"데이터 폴더: {appdir.ROOT}",
        ]
        return "\n".join(lines)

    # ── 메뉴 동작 ───────────────────────────────────────────────
    def on_status(self, *_):
        show(APP_NAME, self.status_text())

    def on_pair(self, *_):
        code = ask_code()
        if not code:
            return
        import socket

        name = ask_name(pairing.load_device().get("label") or socket.gethostname())
        try:
            data = pairing.pair_with_code(code, label=name)
            show(APP_NAME, f"연결됐습니다.\n\nPC: {data['label']}\n"
                           f"이제 화면에서 실행하면 이 컴퓨터에서 돕니다.")
            log(f"[연결] {data['label']} · {data['device_id'][:8]}")
            self.restart_worker()
        except Exception as exc:                               # noqa: BLE001
            show(APP_NAME, f"연결하지 못했습니다.\n\n{exc}")

    def on_rename(self, *_):
        """연결한 뒤에도 화면에 보일 이름을 바꾼다."""
        import socket

        dev = pairing.load_device()
        if not dev:
            show(APP_NAME, "아직 연결되지 않았습니다.\n"
                           "먼저 '이 PC 연결(6자리 코드)' 을 해 주세요.")
            return
        name = ask_name(dev.get("label") or socket.gethostname())
        if not name:
            return
        okay, msg = pairing.rename(name)
        show(APP_NAME, f"이름을 '{name}' 으로 바꿨습니다.\n\n{msg}")
        log(f"[이름] {name} — {msg}")

    def on_google(self, *_):
        path = ask_file("구글 서비스계정 JSON 파일 선택")
        if not path:
            return
        dest = appdir.ROOT / "google-service-account.json"
        try:
            shutil.copyfile(path, dest)
            cfg = appdir.ROOT / "agent_config.json"
            import json

            data = {}
            if cfg.exists():
                data = json.loads(cfg.read_text(encoding="utf-8"))
            data["google_service_account_path"] = str(dest)
            cfg.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = str(dest)
            show(APP_NAME, f"구글 인증 파일을 등록했습니다.\n\n{dest}\n"
                           f"(이 파일은 이 컴퓨터 안에만 있습니다)")
            log("[구글] 인증 파일 등록")
        except Exception as exc:                               # noqa: BLE001
            show(APP_NAME, f"등록하지 못했습니다.\n\n{exc}")

    def on_logs(self, *_):
        os.startfile(str(LOG_DIR))                             # noqa: S606

    def on_autostart(self, *_):
        set_autostart(not autostart_on())
        if self.icon:
            self.icon.update_menu()

    def on_quit(self, *_):
        log("[트레이] 종료 요청")
        self.stop.set()
        if self.icon:
            self.icon.stop()

    # ── 백그라운드 큐 감시 ──────────────────────────────────────
    def restart_worker(self) -> None:
        self.stop.set()
        time.sleep(0.5)
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._serve, daemon=True,
                                       name="agent-serve")
        self.worker.start()

    def _serve(self) -> None:
        dev = pairing.apply_env()
        agent_id = dev.get("device_id") or host_name()
        store = get_store()
        log(f"[에이전트] 시작 — {agent_id} · 큐={type(store).__name__}")
        while not self.stop.is_set():
            try:
                serve(store, agent_id, once=True, poll=2.0, quiet=True)
            except Exception as exc:                           # noqa: BLE001
                log(f"[에이전트] 오류: {exc}")
                time.sleep(5)
            time.sleep(1.5)

    # ── 실행 ────────────────────────────────────────────────────
    def run(self) -> int:
        import pystray

        # 설정 파일에 적힌 구글 인증 경로를 환경으로
        cfgp = appdir.ROOT / "agent_config.json"
        if cfgp.exists():
            try:
                import json

                sa = json.loads(cfgp.read_text(encoding="utf-8")).get(
                    "google_service_account_path")
                if sa and Path(sa).exists():
                    os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", sa)
            except Exception:                                  # noqa: BLE001
                pass
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(appdir.sub("browsers")))

        if not autostart_on():
            set_autostart(True)                                # 설치 후 첫 실행

        def menu_items():
            return pystray.Menu(
                pystray.MenuItem("상태 보기", self.on_status, default=True),
                pystray.MenuItem("이 PC 연결(6자리 코드)", self.on_pair),
                pystray.MenuItem("PC 이름 바꾸기…", self.on_rename),
                pystray.MenuItem("구글 시트 인증 파일…", self.on_google),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("로그 폴더 열기", self.on_logs),
                pystray.MenuItem("윈도우 시작 시 자동 실행", self.on_autostart,
                                 checked=lambda _i: autostart_on()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("종료", self.on_quit),
            )

        self.icon = pystray.Icon("blog_landing_agent", make_icon(self.connected()),
                                 f"{APP_NAME} 실행 중", menu_items())

        def setup(icon):
            icon.visible = True
            threading.Thread(target=install_chromium, daemon=True,
                             args=(lambda m: log(f"[크롬] {m}"),)).start()
            self.restart_worker()
            if not self.connected():
                threading.Thread(target=self._first_run_hint, daemon=True).start()

        log(f"[트레이] 시작 · 데이터 폴더 {appdir.ROOT}")
        self.icon.run(setup=setup)
        return 0

    def _first_run_hint(self) -> None:
        time.sleep(2)
        show(APP_NAME,
             "설치가 끝났습니다.\n\n"
             "화면(웹)에서 [연결 코드 받기] 를 누르면 6자리 숫자가 나옵니다.\n"
             "작업표시줄 오른쪽 아이콘을 눌러 '이 PC 연결(6자리 코드)' 에 입력해 주세요.")


def main() -> int:
    return TrayAgent().run()


if __name__ == "__main__":
    sys.exit(main())
