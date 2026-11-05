r"""PC ↔ 브라우저 연결(페어링) — Agent 쪽.

    화면(Streamlit)에서 6자리 코드 발급  →  이 PC 에서 코드 입력  →  device 등록

    ★`device_token` 이 실제로 어디에 있는가(2026-09-10 정리 — 오해가 없도록 못 박아 둔다)
        · Supabase `devices` 테이블 : **SHA-256(token) 만** 저장 (원문 없음)
        · 이 PC 의 `device.json`     : **원문 저장** ← Agent 가 bearer 로 쓰므로 정상이다
        · job payload / 큐 레코드    : 저장 안 함
        · 서버(Edge Function) 로그   : 저장 안 함 (해시 앞 12자만 찍는다)
        · Agent 로그                 : 저장 안 함
      즉 "원문은 어디에도 없다" 가 아니라 **"서버에는 없고, 페어링된 그 PC 에만 있다"** 이다.
      유출 시 대응은 `devices.revoked_at` 이다(그 기기만 즉시 차단).

    ★파일 보호에 대한 정확한 사실(2026-09-10)
      `save_device()` 가 `os.chmod(0o600)` 을 부르지만, **윈도우에서 이것은 유닉스의
      600 과 같은 보안 경계가 아니다** — 파이썬은 윈도우에서 읽기전용 속성만 건드리고
      ACL 은 바꾸지 않는다. 실제 보호는 **`%APPDATA%` 가 사용자 프로필 ACL 로 보호되는
      것**에 의존한다(그 사용자 + Administrators + SYSTEM 만 접근).
      → 같은 PC 의 관리자 권한 사용자는 읽을 수 있다는 뜻이다. 그래서 토큰 유출을
        '불가능' 으로 보지 않고, **revoke 로 되돌릴 수 있게** 설계해 두었다.
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

# ★1.3.0 — `v2.session --logout / --confirm`(선택 계정 로그아웃)이 들어갔다.
#   화면은 이 번호를 보고 로그아웃 버튼을 열어 준다(옛 설치본에서는 잠긴다).
# ★1.3.1 (2026-11-05) — **기능 변경 없음. 빌드를 구분하기 위한 번호다.**
#   11-03 에 1.3.0 으로 빌드한 설치본에는 Stage 4·5 · 공유 세션 보호 A안 ·
#   login_id 정규화(`v2/loginid.py`)가 들어 있지 않다. 둘 다 1.3.0 이라고
#   말하면 하트비트만 보고는 어느 빌드가 깔렸는지 알 수 없어서 올렸다.
#   ※`NEEDS` 는 **최소 버전**이라 그대로 (1,3,0) 이다 — 1.3.1 은 그 이상이므로
#     로그아웃 게이트가 정상히 열린다. 올리면 1.3.0 빌드가 잠겨 버린다.
# ★1.3.2 (2026-11-05) — **기능 축소·확장 없음.** 작업 중단에서 한 가지만
#   더했다: 작업을 집은 **직후·프로세스를 띄우기 전에** 취소를 한 번 본다.
#   1.3.1 은 실행을 시작한 뒤 0.4초 루프에서만 봐서, 취소해 둔 대기 작업도
#   크로미움이 잠깐 떴다가 죽었다. 번호를 올리는 이유는 그 차이를
#   하트비트로 구분하기 위해서다(게이트 조건은 그대로다).
# ★1.3.3 (2026-11-05) — 작업 중단(집은 직후 확인) + **Chromium 수동 로그아웃**.
#   로그아웃은 이제 기본이 수동이다 — 그 계정의 기존 프로필로 창을 띄워
#   사용자가 네이버에서 직접 로그아웃한다(`--auto` 로 옛 자동 방식 보존).
# ★1.3.4 (2026-11-05) — 수동 로그아웃 흐름 정리. 기능 확장 없음.
#   창을 띄운 뒤 **건드리지 않고** 닫히기만 기다린다(폴링 제거).
#   닫히면 한 번 재확인하고, **확인된 경우에만** 로컬 쿠키를 지운다.
#   공유 후보만으로는 로그아웃을 막지 않는다(안내만).
AGENT_VERSION = "1.3.4"

# ★기능별 **최소 Agent 버전**. 화면은 이 표를 보고 버튼을 열거나 잠근다.
#   옛 설치본에 없는 명령을 보내면 argparse 가 거부해 작업이 실패하기 때문이다.
NEEDS = {"logout": (1, 3, 0)}


def ver_tuple(text: str) -> tuple:
    """버전 글자를 숫자 튜플로 — 'X.Y.Z' 꼴을 (X, Y, Z) 로 본다.

    숫자가 아닌 부분은 0 으로 본다(빈 값은 `(0,)`).
    ★여기에 세 자리 숫자 버전을 **예시로 적지 않는다** — docstring 은 주석과 달리
      `.pyc` 에 **남는다.** 전에는 옛 버전 번호를 예시로 적어 뒀는데, 설치본 EXE
      에서 버전 문자열을 뽑는 검증이 `AGENT_VERSION` 과 그 예시를 **둘 다** 집어
      '버전이 두 개' 로 보였다(2026-11-05). 설명이 필요하면 숫자 없이 쓴다.
    """
    out = []
    for part in str(text or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out[:3]) or (0,)


def supports(version: str, feature: str) -> bool:
    """그 Agent 버전이 이 기능을 쓸 수 있는가(버전을 모르면 False = 잠근다)."""
    need = NEEDS.get(feature)
    if need is None:
        return True
    got = ver_tuple(version)
    return bool(str(version or "").strip()) and got >= need

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
    # ⚠️윈도우에서는 이 호출이 **읽기전용 속성만** 바꾼다(ACL 은 안 건드린다).
    #   즉 유닉스의 600 같은 보안 경계가 아니다 — 실제 보호는 `%APPDATA%` 의
    #   사용자 프로필 ACL 이 담당한다. 리눅스/맥에서 돌 때만 의미가 있다.
    try:
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


def rename(label: str) -> tuple[bool, str]:
    """화면에 보일 이 PC 이름을 바꾼다.

    · 로컬 모드  — device.json 만 고치면 끝난다.
    · 원격 모드  — 하트비트가 이름을 함께 보내므로 다음 하트비트(몇 초)에 반영된다.
                   (서버에 `rename_device` 함수가 있으면 즉시 반영한다)
    """
    label = (label or "").strip()
    if not label:
        return False, "이름이 비어 있습니다."
    dev = load_device()
    if not dev:
        return False, "아직 연결되지 않았습니다."
    dev["label"] = label
    save_device(dev)
    if dev.get("mode") != "supabase":
        return True, "화면을 새로고침하면 바뀐 이름이 보입니다."

    try:                                   # 서버에 즉시 반영(함수가 있을 때만)
        import requests

        cfg = _config()
        r = requests.post(f"{cfg['url'].rstrip('/')}/rest/v1/rpc/rename_device",
                          headers={"apikey": cfg["key"],
                                   "Authorization": f"Bearer {cfg['key']}",
                                   "Content-Type": "application/json"},
                          json={"p_token": dev.get("device_token"), "p_label": label},
                          timeout=15)
        if r.ok:
            return True, "화면에 바로 반영됩니다."
    except Exception:                                          # noqa: BLE001
        pass
    return True, "다음 연결 신호(몇 초 뒤)에 화면 이름이 바뀝니다."


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="이 PC 를 화면(Streamlit)과 연결한다")
    p.add_argument("--code", help="화면에 뜬 6자리 페어링 코드")
    p.add_argument("--label", default="", help="PC 이름(기본: 컴퓨터 이름)")
    p.add_argument("--status", action="store_true", help="연결 상태 보기")
    p.add_argument("--reset", action="store_true", help="연결 해제(토큰 삭제)")
    p.add_argument("--rename", metavar="이름", help="화면에 보일 이 PC 이름 바꾸기")
    args = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    if args.reset:
        print("연결 해제:", "완료" if clear_device() else "연결된 적이 없습니다")
        return 0
    if args.rename:
        okay, msg = rename(args.rename)
        print(("이름 변경 완료 — " if okay else "실패 — ") + msg)
        return 0 if okay else 1
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
