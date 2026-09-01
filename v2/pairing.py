r"""PC ↔ 브라우저 연결(페어링) — Agent 쪽.

    화면(Streamlit)에서 6자리 코드 발급  →  이 PC 에서 코드 입력  →  device 등록
      · 발급받은 `device_token` 은 **이 PC 에만** 저장한다(%APPDATA%\BlogLandingAgent).
      · Agent 는 그 토큰으로 **자기 device 의 작업만** 가져간다(DB 함수가 검증).
      · service key 는 여기에 없다. Agent 가 아는 건 publishable key 와 자기 토큰뿐이다.

    .\.venv\Scripts\python.exe -m v2.pairing --code 482100      # 연결
    .\.venv\Scripts\python.exe -m v2.pairing --status           # 연결 상태 보기
    .\.venv\Scripts\python.exe -m v2.pairing --reset            # 연결 해제(토큰 삭제)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

AGENT_VERSION = "1.0.0"

# 토큰 저장 위치 — 저장소나 프로젝트 폴더가 아니라 사용자 프로필 아래.
DEVICE_DIR = Path(os.getenv("APPDATA") or Path.home()) / "BlogLandingAgent"
DEVICE_FILE = DEVICE_DIR / "device.json"

# 접속 정보(비밀 아님) — 설치 시 함께 놓이는 설정 파일 또는 환경변수.
CONFIG_FILE = DEVICE_DIR / "agent_config.json"


def _config() -> dict:
    """SUPABASE_URL · publishable key. 파일 > 환경변수 > 프로젝트 .env 순."""
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            cfg = {}
    url = (cfg.get("supabase_url") or os.getenv("SUPABASE_URL") or "").strip()
    key = (cfg.get("supabase_publishable_key")
           or os.getenv("SUPABASE_PUBLISHABLE_KEY")
           or os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if not url or not key:                                     # 개발 PC 용 편의
        try:
            from dotenv import dotenv_values

            root = Path(__file__).resolve().parent.parent
            env = dotenv_values(root / ".env")
            url = url or (env.get("SUPABASE_URL") or "").strip()
            key = key or (env.get("SUPABASE_PUBLISHABLE_KEY") or "").strip()
        except Exception:                                      # noqa: BLE001
            pass
    return {"url": url, "key": key}


def save_config(url: str, key: str) -> Path:
    """설치 프로그램이 접속 정보를 심을 때 쓴다(비밀값 아님)."""
    DEVICE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(
        {"supabase_url": url, "supabase_publishable_key": key},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return CONFIG_FILE


def load_device() -> dict:
    """이 PC 의 device_id · device_token(없으면 빈 dict)."""
    if not DEVICE_FILE.exists():
        return {}
    try:
        return json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return {}


def save_device(data: dict) -> Path:
    DEVICE_DIR.mkdir(parents=True, exist_ok=True)
    DEVICE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    try:                                                       # 다른 사용자 못 읽게
        os.chmod(DEVICE_FILE, 0o600)
    except Exception:                                          # noqa: BLE001
        pass
    return DEVICE_FILE


def clear_device() -> bool:
    if DEVICE_FILE.exists():
        DEVICE_FILE.unlink()
        return True
    return False


def is_paired() -> bool:
    """연결됐나. 원격(supabase) 모드는 토큰까지, 로컬 모드는 device_id 만 있으면 된다."""
    d = load_device()
    if not d.get("device_id"):
        return False
    return d.get("mode") != "supabase" or bool(d.get("device_token"))


def apply_env() -> dict:
    """저장된 값을 환경변수로 올린다 — `get_store()` 가 device 모드로 뜨게."""
    cfg, dev = _config(), load_device()
    if cfg["url"]:
        os.environ.setdefault("SUPABASE_URL", cfg["url"])
    if cfg["key"]:
        os.environ.setdefault("SUPABASE_PUBLISHABLE_KEY", cfg["key"])
    if dev.get("device_token"):
        os.environ["BLOG_DEVICE_TOKEN"] = dev["device_token"]
        os.environ.setdefault("BLOG_QUEUE_BACKEND", "supabase")
    return dev


def pair_with_code(code: str, label: str = "") -> dict:
    """6자리 코드로 이 PC 를 등록하고 토큰을 저장한다.

    · 접속 정보(원격 큐)가 있으면 Supabase 로 등록한다.
    · 없으면 **로컬 큐(queue/pairings)** 로 같은 흐름을 그대로 수행한다 —
      Supabase 를 붙이기 전에도 화면·Agent 연결을 검증할 수 있게 하기 위해서다.
      나중에 접속 정보만 넣으면 코드 변경 없이 원격으로 붙는다.
    """
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        raise RuntimeError("페어링 코드는 6자리 숫자입니다.")
    label = label or socket.gethostname()
    cfg = _config()

    if cfg["url"] and cfg["key"]:
        from .supabase_store import pair

        got = pair(cfg["url"], cfg["key"], code, label=label, version=AGENT_VERSION)
        data = {"device_id": got["device_id"], "device_token": got["device_token"],
                "label": label, "version": AGENT_VERSION, "mode": "supabase"}
    else:
        from .queue_store import LocalStore

        store = LocalStore()
        device_id = label                      # 로컬 모드는 PC 이름이 곧 device
        store.consume_pairing(code, device_id, label)
        data = {"device_id": device_id, "device_token": "", "label": label,
                "version": AGENT_VERSION, "mode": "local"}
    save_device(data)
    return data


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="이 PC 를 화면(Streamlit)과 연결한다")
    p.add_argument("--code", help="화면에 뜬 6자리 페어링 코드")
    p.add_argument("--label", default="", help="PC 이름(기본: 컴퓨터 이름)")
    p.add_argument("--status", action="store_true", help="연결 상태 보기")
    p.add_argument("--reset", action="store_true", help="연결 해제(토큰 삭제)")
    args = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    if args.reset:
        print("연결 해제:", "완료" if clear_device() else "연결된 적이 없습니다")
        return 0
    if args.status or not args.code:
        cfg, dev = _config(), load_device()
        print(f"접속 설정 : {'있음' if cfg['url'] and cfg['key'] else '없음'} "
              f"({CONFIG_FILE})")
        if dev:
            print(f"연결 상태 : 연결됨 · PC {dev.get('label')} "
                  f"· device {str(dev.get('device_id'))[:8]}…")
            print(f"토큰 위치 : {DEVICE_FILE} (이 PC 밖으로 나가지 않습니다)")
        else:
            print("연결 상태 : 연결 안 됨 — 화면의 6자리 코드로 "
                  "`-m v2.pairing --code <코드>` 를 실행하세요")
        return 0

    data = pair_with_code(args.code, args.label)
    print(f"연결 완료 — PC {data['label']} · device {data['device_id'][:8]}…")
    print(f"토큰 저장: {DEVICE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
