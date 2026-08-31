"""단계 로그 — 콘솔과 out/v2_*.log 에 동시에 남긴다.

★기계용 이벤트(`log.event(...)`)를 함께 낸다. GUI 는 로그 문구를 정규식으로 긁지 않고
  이 JSON 한 줄만 보면 된다(문구가 바뀌어도 GUI 가 조용히 깨지지 않는다).
  · `out/<tag>_<stamp>.events.jsonl` 에는 **항상** 남긴다(나중에 Supabase 적재용).
  · stdout 에는 `--events` 를 준 실행에서만 `@@EVENT {...}` 로 찍는다(기존 출력 그대로).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

EVENT_PREFIX = "@@EVENT "


class Log:
    def __init__(self, out_dir: Path, tag: str = "v2", events: bool = False) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = Path(out_dir) / f"{tag}_{stamp}.log"
        self.events_path = Path(out_dir) / f"{tag}_{stamp}.events.jsonl"
        self._fh = self.path.open("w", encoding="utf-8")
        self._ev = None
        self._emit_stdout = bool(events)
        try:                                   # 한글이 cp949 콘솔에서 깨지지 않게
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                      # noqa: BLE001
            pass

    def __call__(self, msg: str = "") -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        try:
            print(line, flush=True)
        except Exception:                      # noqa: BLE001
            print(line.encode("ascii", "replace").decode(), flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def event(self, name: str, **fields) -> None:
        """기계용 이벤트 한 줄. 어떤 이유로도 실행을 멈추지 않는다."""
        try:
            payload = {"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": name, **fields}
            line = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:                      # noqa: BLE001
            return
        try:
            if self._ev is None:
                self._ev = self.events_path.open("w", encoding="utf-8")
            self._ev.write(line + "\n")
            self._ev.flush()
        except Exception:                      # noqa: BLE001
            pass
        if self._emit_stdout:
            try:
                print(EVENT_PREFIX + line, flush=True)
            except Exception:                  # noqa: BLE001
                pass

    def close(self) -> None:
        for fh in (self._fh, self._ev):
            try:
                if fh is not None:
                    fh.close()
            except Exception:                  # noqa: BLE001
                pass
