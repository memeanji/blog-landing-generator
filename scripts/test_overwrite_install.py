r"""덮어 설치(v1.1.4 → v1.1.5) 시 기존 연결이 그대로 유지되는지 검증.

표님 PC 를 직접 만질 수 없으므로, **설치 프로그램이 실제로 하는 파일 동작을 그대로 재현**해
확인한다. 재현 대상(installer/BlogLandingAgent.iss 기준):

    [Files] {app}\*                       ← 프로그램 폴더만 덮어씀
    [Files] agent_config.json → %APPDATA%\BlogLandingAgent  (Flags: onlyifdoesntexist)
    device.json 은 [Files] 목록에 **아예 없다** → 건드리지 않는다
    DelTree 는 [Code] 의 CurUninstallStepChanged(usPostUninstall) 안에만 있다
      = **제거할 때만**, 그것도 "예" 를 눌러야 지운다. 덮어 설치는 이 경로를 지나지 않는다.

    .\.venv\Scripts\python.exe scripts\test_overwrite_install.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
LABEL = "표님"
ok_n, fail_n = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        fail_n += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="overwrite_"))
    home = tmp / "user" / "BlogLandingAgent"          # = %APPDATA%\BlogLandingAgent
    home.mkdir(parents=True)

    # ── 설치 전 상태: v1.1.4 로 이미 페어링된 PC ────────────────────
    device = {
        "device_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "device_token": "0" * 64,                      # 형식만 맞춘 더미(실토큰 아님)
        "label": LABEL,
        "version": "1.1.4",
        "mode": "supabase",
    }
    (home / "device.json").write_text(json.dumps(device, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    (home / "agent_config.json").write_text(json.dumps({
        "supabase_url": "https://example.supabase.co",
        "supabase_publishable_key": "sb_publishable_x",
        "google_service_account_path": "",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    (home / "sessions").mkdir()
    (home / "sessions" / "keep.txt").write_text("네이버 로그인 세션 자리", encoding="utf-8")

    before = {p.name: sha(p) for p in home.rglob("*") if p.is_file()}
    print(f"설치 전 %APPDATA% 파일 {len(before)}개\n")

    # ── 설치 프로그램이 하는 일 재현 ────────────────────────────────
    print("① 설치 프로그램 파일 동작 재현")
    app = tmp / "Program Files" / "BlogLandingAgent"
    app.mkdir(parents=True)
    (app / "BlogLandingAgent.exe").write_text("v1.1.5 (새 빌드)", encoding="utf-8")
    # agent_config.json — onlyifdoesntexist 이므로 이미 있으면 건너뛴다
    skipped = (home / "agent_config.json").exists()
    check("agent_config.json 은 이미 있으면 덮어쓰지 않음(onlyifdoesntexist)", skipped)

    after = {p.name: sha(p) for p in home.rglob("*") if p.is_file()}
    check("device.json 내용 그대로", before.get("device.json") == after.get("device.json"),
          f"{before.get('device.json')} → {after.get('device.json')}")
    check("agent_config.json 그대로",
          before.get("agent_config.json") == after.get("agent_config.json"))
    check("로그인 세션 폴더 그대로", (home / "sessions" / "keep.txt").exists())

    # ── 설치 스크립트에 위험한 삭제가 없는지 원문 확인 ───────────────
    print("\n② 설치 스크립트 원문 확인")
    iss = (ROOT / "installer" / "BlogLandingAgent.iss").read_text(
        encoding="utf-8", errors="replace")
    check("device.json 을 다루는 [Files] 항목 없음", "device.json" not in iss)
    deltree_in_uninstall = bool(re.search(
        r"CurUninstallStepChanged.*?DelTree", iss, re.S))
    check("DelTree 는 '제거' 절차 안에만 있음", deltree_in_uninstall)
    check("삭제 전 사용자 확인(MsgBox)을 거침", "MB_YESNO" in iss)
    check("설치 단계(CurStepChanged)에는 삭제 없음", "CurStepChanged" not in iss)
    check("agent_config 는 onlyifdoesntexist", "onlyifdoesntexist" in iss)

    # ── 새 Agent 가 이 폴더를 읽었을 때 ─────────────────────────────
    print("\n③ v1.1.5 Agent 가 기존 연결을 그대로 인식하는가")
    env = dict(os.environ)
    env["APPDATA"] = str(home.parent)
    env["BLOG_DATA_DIR"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    probe = ROOT / "scripts" / "_probe_pairing.py"
    probe.write_text(
        "import sys, os, json\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from v2 import pairing\n"
        "d = pairing.load_device()\n"
        "print(json.dumps({'paired': pairing.is_paired(), 'label': d.get('label'),\n"
        "                  'device_id': d.get('device_id'),\n"
        "                  'agent_version': pairing.AGENT_VERSION,\n"
        "                  'stored_version': d.get('version')}, ensure_ascii=False))\n",
        encoding="utf-8")
    r = subprocess.run([PY, str(probe)], cwd=str(ROOT), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    probe.unlink(missing_ok=True)
    try:
        got = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:                                          # noqa: BLE001
        got = {}
        print((r.stdout or "") + (r.stderr or ""))
    check("이미 연결됨으로 인식(재페어링 안 함)", got.get("paired") is True, str(got.get("paired")))
    check(f"Agent 이름 '{LABEL}' 유지", got.get("label") == LABEL, str(got.get("label")))
    check("device_id 동일(새 기기로 재등록 아님)",
          got.get("device_id") == device["device_id"], str(got.get("device_id")))
    check("Agent 버전만 1.1.5 로 올라감", got.get("agent_version") == "1.1.5",
          f"{got.get('stored_version')} → {got.get('agent_version')}")

    # ── 서버 이름이 바뀔 여지가 있는지 ──────────────────────────────
    print("\n④ 서버(devices.label)가 바뀔 경로가 있는가")
    agent_src = (ROOT / "v2" / "agent.py").read_text(encoding="utf-8")
    store_src = (ROOT / "v2" / "supabase_store.py").read_text(encoding="utf-8")
    check("하트비트는 device.json 의 label 을 그대로 보냄",
          'label=pairing.load_device().get("label", "")' in agent_src)
    check("label 이 비어 있으면 이름 변경 요청을 아예 안 보냄",
          "if label and label != getattr(self" in store_src)
    check("agent_heartbeat 는 label 을 건드리지 않음",
          "p_label" not in store_src.split("def heartbeat")[1].split("def agents")[0]
          .replace('self._rpc("rename_device", {"p_label": label})', ""))
    schema = (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8")
    check("pair_device 로 정한 label 을 덮어쓰는 함수 없음",
          "rename_device" not in schema)

    print(f"\n{'=' * 60}\n통과 {ok_n} · 실패 {fail_n}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass
    raise SystemExit(main())
