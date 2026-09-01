r"""Job 을 **자식 프로세스로** 실행하고 진행 상황을 콜백으로 넘긴다.

GUI 는 이 모듈만 쓴다. Playwright/asyncio 가 GUI 프로세스 안에서 돌지 않으므로
  · 브라우저가 죽어도 GUI 는 살아 있고
  · '중단' 은 프로세스 트리를 죽이면 끝나며
  · 실행되는 명령이 **사람이 터미널에 치던 것과 완전히 같다**(기존 동작 보존).

    r = Runner(on_line=..., on_event=..., on_exit=...)
    r.start(job)      # 백그라운드 스레드
    r.stop()          # 중단
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from .job import Job, module_command
from .logger import EVENT_PREFIX

ROOT = Path(__file__).resolve().parent.parent

CREATE_NO_WINDOW = 0x08000000          # 콘솔 창이 깜빡이지 않게(Windows)


def _popen_kwargs() -> dict:
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


class Runner:
    """한 번에 한 개만 돌린다(프로필이 계정당 하나뿐이라 동시에 돌리면 충돌한다)."""

    def __init__(self, on_line=None, on_event=None, on_exit=None) -> None:
        self.on_line = on_line or (lambda text: None)
        self.on_event = on_event or (lambda data: None)
        self.on_exit = on_exit or (lambda code: None)
        self.proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ── 실행 ─────────────────────────────────────────────────────
    def start(self, job: Job | list[str], label: str = "",
              env_extra: dict | None = None) -> list[str]:
        if self.running:
            raise RuntimeError("이미 실행 중입니다. 끝나거나 중단한 뒤에 다시 눌러 주세요.")
        cmd = job.command() if isinstance(job, Job) else list(job)
        self._stopping = False

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env.update({k: str(v) for k, v in (env_extra or {}).items() if v})

        self.proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            **_popen_kwargs())
        self._thread = threading.Thread(target=self._pump, args=(self.proc,),
                                        name=f"runner-{label or 'job'}", daemon=True)
        self._thread.start()
        return cmd

    def start_module(self, module: str, args: list[str], label: str = "",
                     env_extra: dict | None = None) -> list[str]:
        """`python -m <module> <args…>` 를 그대로 돌린다(계정 로그인·세션 확인용)."""
        return self.start(module_command(module, args), label=label,
                          env_extra=env_extra)

    # ── 중단 ─────────────────────────────────────────────────────
    def stop(self) -> bool:
        """자식 프로세스와 그 아래 크로미움까지 통째로 정리한다."""
        if not self.running or self.proc is None:
            return False
        self._stopping = True
        pid = self.proc.pid
        try:
            if sys.platform == "win32":
                # ★크로미움이 손자 프로세스로 뜨므로 /T(트리)로 죽여야 남지 않는다.
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, **_popen_kwargs())
            else:
                self.proc.terminate()
        except Exception:                                      # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:                                  # noqa: BLE001
                return False
        return True

    # ── 출력 읽기 ────────────────────────────────────────────────
    def _pump(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line = raw.rstrip("\r\n")
                    if line.startswith(EVENT_PREFIX):
                        payload = line[len(EVENT_PREFIX):].strip()
                        try:
                            self.on_event(json.loads(payload))
                        except Exception:                      # noqa: BLE001
                            self.on_line(line)                 # 깨진 이벤트는 그냥 로그로
                        continue
                    if line:
                        self.on_line(line)
        except Exception as exc:                               # noqa: BLE001
            self.on_line(f"[runner] 출력 읽기 중단 ({type(exc).__name__}: {exc})")
        finally:
            code = proc.wait()
            if self._stopping:
                code = 130
            try:
                self.on_exit(code)
            except Exception:                                  # noqa: BLE001
                pass
