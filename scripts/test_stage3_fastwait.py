r"""3단계 검증 — 조건부 대기가 **옛 고정 대기보다 위험해지지 않는가**(브라우저 없음).

    .\.venv\Scripts\python.exe scripts\test_stage3_fastwait.py

증명할 것
    ① 조건이 참이 되는 순간 돌아온다(기다린 시간 ≈ 조건이 선 시각)
    ② 조건이 **서기 전에는 절대** 돌아오지 않는다
    ③ 조건이 끝까지 안 서면 기다린 시간이 옛 고정값과 **정확히 같다**
    ④ `BLOG_FAST_WAIT=0` 이면 조건을 **보지도 않고** 옛 고정값을 그대로 기다린다
    ⑤ 조건 함수가 예외를 던져도 터지지 않고, 총 대기는 고정값을 넘지 않는다
    ⑥ 실제 적용 지점(writer/edit_post)에 고정 대기가 남아 있지 않은지 확인

가짜 page 를 쓴다 — `wait_for_timeout(ms)` 호출을 **가상 시간**으로 누적하므로
테스트가 1초도 실제로 기다리지 않는다(브라우저·네이버와 무관).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from v2 import fastwait                                        # noqa: E402

OK, NG = "  [o]", "  [X]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


class FakePage:
    """`wait_for_timeout` 을 가상 시간으로 누적하는 가짜 페이지."""

    def __init__(self) -> None:
        self.now = 0
        self.calls: list[int] = []

    async def wait_for_timeout(self, ms: int) -> None:
        self.calls.append(int(ms))
        self.now += int(ms)


def run(ms: int, ready_at: int | None, *, step: int = 150, min_ms: int = 0,
        boom: bool = False):
    """조건이 `ready_at` ms 에 참이 되는 상황을 재현한다. (결과, 총대기, 확인횟수)"""
    page = FakePage()
    seen = {"n": 0}

    async def check_fn() -> bool:
        seen["n"] += 1
        if boom:
            raise RuntimeError("조건을 읽지 못했다")
        return ready_at is not None and page.now >= ready_at

    got = asyncio.run(fastwait.settle(page, ms, check_fn, step=step,
                                      min_ms=min_ms))
    return got, page.now, seen["n"]


#  ★1)·2) 절은 **켜진 상태**의 동작을 본다(기본은 꺼짐이므로 여기서 켠다).
os.environ["BLOG_FAST_WAIT"] = "1"

print("=" * 74)
print(" 1) 조건이 서면 바로 돌아온다 (옛 고정값 2500ms 자리) · 스위치 ON")
print("=" * 74)
got, waited, n = run(2500, 0)
check("이미 준비돼 있으면 **전혀 기다리지 않는다**",
      got is True and waited == 0, f"{waited}ms · 확인 {n}회")

got, waited, n = run(2500, 300)
check("0.3초에 준비되면 0.3초만 기다린다(옛 2.5초 → 2.2초 절약)",
      got is True and 300 <= waited < 500, f"{waited}ms")

got, waited, n = run(2500, 1000)
check("1.0초에 준비되면 1.05초만 기다린다", got is True and 1000 <= waited < 1200,
      f"{waited}ms")

got, waited, n = run(900, 100)
check("Ctrl+C 자리(900ms)도 같다 — 0.1초에 복사되면 0.15초",
      got is True and 100 <= waited < 300, f"{waited}ms")

print("")
print("=" * 74)
print(" 2) 조건이 서기 전에는 돌아오지 않는다 / 넘겨 기다리지 않는다")
print("=" * 74)
got, waited, n = run(2500, 2400)
check("조건이 늦게 서면 그때까지 기다린다", got is True and waited >= 2400,
      f"{waited}ms")

got, waited, n = run(2500, None)
check("조건이 끝까지 안 서면 **옛 고정값과 정확히 같다**",
      got is False and waited == 2500, f"{waited}ms (옛 2500ms)")

got, waited, n = run(900, None)
check("900ms 자리도 정확히 900ms", got is False and waited == 900, f"{waited}ms")

got, waited, n = run(600, None)
check("600ms 자리도 정확히 600ms", got is False and waited == 600, f"{waited}ms")

got, waited, n = run(2500, 9999)
check("조건이 제한 안에 못 서면 기다림이 늘지 않는다",
      got is False and waited == 2500, f"{waited}ms")

got, waited, n = run(2500, None, boom=True)
check("조건 함수가 예외를 던져도 터지지 않고 고정값까지만 기다린다",
      got is False and waited == 2500, f"{waited}ms · 확인 {n}회")

print("")
print("=" * 74)
print(" 3) 스위치 — **기본 꺼짐(opt-in)** · BLOG_FAST_WAIT=1 에서만 켜진다")
print("=" * 74)
#   ★2026-11-03 변경: 기본을 꺼짐으로 뒤집었다. 사용자 PC 에서 .env 설정을
#     빠뜨리면 '의도와 반대로 최적화가 켜진' 상태가 되기 때문이다.
os.environ.pop("BLOG_FAST_WAIT", None)
check("설정이 없으면 **꺼져 있다**(1.2.5 와 동일한 고정 대기)",
      not fastwait.enabled())
got, waited, n = run(2500, 0)
check("설정이 없으면 조건을 **보지도 않는다**", n == 0, f"확인 {n}회")
check("설정이 없으면 옛 고정값을 그대로 기다린다",
      got is False and waited == 2500, f"{waited}ms")
for off in ("0", "off", "false", "no", "", "2", "xyz"):
    os.environ["BLOG_FAST_WAIT"] = off
    if fastwait.enabled():
        check(f"BLOG_FAST_WAIT={off!r} 는 꺼짐이어야 한다", False, "켜져 버렸다")
        break
else:
    check("끄는 값·빈값·오타는 모두 꺼짐으로 본다(0/off/false/no/''/2/xyz)", True)
for on in ("1", "on", "true", "yes", "Y", " 1 "):
    os.environ["BLOG_FAST_WAIT"] = on
    if not fastwait.enabled():
        check(f"BLOG_FAST_WAIT={on!r} 는 켜짐이어야 한다", False, "꺼져 버렸다")
        break
else:
    check("켜는 값만 켜짐으로 본다(1/on/true/yes · 공백 허용)", True)
os.environ["BLOG_FAST_WAIT"] = "1"
got, waited, n = run(2500, 300)
check("켜면 조건부 대기가 동작한다", got is True and 300 <= waited < 500,
      f"{waited}ms")
os.environ.pop("BLOG_FAST_WAIT", None)

page = FakePage()
asyncio.run(fastwait.settle(page, 1200, None))
check("조건을 주지 않으면(check=None) 옛 고정 대기와 같다", page.now == 1200,
      f"{page.now}ms")

os.environ["BLOG_FAST_WAIT"] = "1"          # min_ms 는 켜진 상태의 동작이다
got, waited, n = run(2500, 0, min_ms=400)
check("min_ms 를 주면 그만큼은 반드시 기다린다", waited == 400, f"{waited}ms")
os.environ.pop("BLOG_FAST_WAIT", None)

print("")
print("=" * 74)
print(" 4) 적용 범위 — **글 화면 진입 대기 2곳만** (2026-11-03 범위 축소)")
print("=" * 74)
w = (ROOT / "v2" / "writer.py").read_text(encoding="utf-8")
e = (ROOT / "v2" / "edit_post.py").read_text(encoding="utf-8")
prod = (ROOT / "v2" / "run_production.py").read_text(encoding="utf-8")

check("① writer.open_write 의 고정 2.5초 → 조건부",
      "fastwait.settle(page, 2500, lambda: _page_ready(page))" in w)
check("② edit_post.open_for_edit 의 고정 2.5초 → 조건부",
      "fastwait.settle(page, 2500, _ready)" in e)
check("조건부 대기는 이 **2곳뿐**이다",
      w.count("fastwait.settle(") == 1 and e.count("fastwait.settle(") == 1,
      f"writer {w.count('fastwait.settle(')}곳 · "
      f"edit_post {e.count('fastwait.settle(')}곳")

print("")
print("  (원복한 것 — 1건당 1~3초 목표에는 진입 대기만으로 충분)")
check("모바일 전환은 고정 0.9초 그대로",
      "await self.page.wait_for_timeout(900)" in w
      and "fastwait.settle(self.page, 900" not in w)
check("제목 입력 뒤는 고정 0.6초 그대로",
      "await self.page.wait_for_timeout(600)" in w
      and "fastwait.settle(self.page, 600" not in w)
check("Ctrl+C 뒤는 고정 0.9초 그대로",
      "await src.page.wait_for_timeout(900)" in e
      and "fastwait.settle(src.page, 900" not in e)
check("수정화면 진입 확인 간격은 1초 그대로",
      "await page.wait_for_timeout(1000)" in e
      and "250 if fastwait.enabled()" not in e)

print("")
print("  (손대지 않은 안전장치)")
check("에디터 frame 탐색(find_editor_frame)은 그대로",
      "await page.wait_for_timeout(700)" in
      (ROOT / "v2" / "browser.py").read_text(encoding="utf-8"))
check("'작성 중인 글' 팝업 폴링(dismiss_draft_popup)은 그대로",
      "await page.wait_for_timeout(400)" in w and "timeout_ms: int = 10_000" in w)
check("Ctrl+A 뒤 0.8초(에디터 내부 선택)는 그대로",
      "press(\"Control+A\")" in e and "wait_for_timeout(800)" in e)
check("본문 포커스 뒤 0.6초(선택 상태 안정)는 그대로",
      "await src.page.wait_for_timeout(600)" in e)
check("이미지 로딩 확인은 그대로(IMG_WAIT_MS)", "IMG_WAIT_MS" in prod)
check("발행 전 랜덤 대기는 그대로(--delay-min/max)", "args.delay_min" in prod)
check("10건 배치 구조는 그대로", "BATCH_SIZE = 10" in
      (ROOT / "v2" / "batch_plan.py").read_text(encoding="utf-8"))
check("발행 완료 검증·시트 기록은 그대로",
      "await post.publish()" in prod and "mark_row_done" in prod)

print("")
print("  예상 절감 — 진입 대기 2곳: 1건당 **1.5~2.3초**"
      " (10건 15~23초) · 목표 1~3초 충족")

print("")
print("=" * 74)
print(" 5) 설치본 환경에서 BLOG_FAST_WAIT 가 실제로 먹는가")
print("=" * 74)
#   설치본은 ROOT = %APPDATA% 아래 BlogLandingAgent 이고(v2/appdir.py frozen 분기),
#   v2/config._ensure_dotenv() 가 그 폴더의 .env 를 읽어 os.environ 에 넣는다.
#   여기서는 BLOG_DATA_DIR 로 ROOT 를 임시폴더로 바꿔 **같은 경로**를 재현한다.
import json
import pathlib
import subprocess
import tempfile

PROBE = chr(10).join([
    "import os, sys, json",
    "from v2 import config",
    "config.load_settings()",            # 설치본과 같은 순서로 .env 를 읽는다
    "from v2 import fastwait",
    "print(json.dumps({'env': os.getenv('BLOG_FAST_WAIT'),"
    "                  'enabled': fastwait.enabled()}))",
])


def probe(env_line: str) -> dict:
    """임시 ROOT 에 .env 를 두고 **별도 프로세스**로 확인한다."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="blog_envprobe_"))
    if env_line:
        (d / ".env").write_text(env_line + chr(10), encoding="utf-8")
    e = dict(os.environ)
    e["BLOG_DATA_DIR"] = str(d)
    e["PYTHONPATH"] = str(ROOT)
    e["PYTHONIOENCODING"] = "utf-8"
    e.pop("BLOG_FAST_WAIT", None)
    r = subprocess.run([sys.executable, "-c", PROBE], cwd=str(ROOT), env=e,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:                                          # noqa: BLE001
        return {"err": (r.stderr or r.stdout)[-200:]}


got = probe("")
check(".env 에 아무것도 없으면 **꺼짐**(설정을 빠뜨린 PC 가 안전하다)",
      got.get("env") is None and got.get("enabled") is False, str(got))
got = probe("BLOG_FAST_WAIT=0")
check(".env 에 BLOG_FAST_WAIT=0 → 꺼짐",
      got.get("env") == "0" and got.get("enabled") is False, str(got))
got = probe("BLOG_FAST_WAIT=1")
check(".env 에 BLOG_FAST_WAIT=1 → **켜짐**(이때만 켜진다)",
      got.get("env") == "1" and got.get("enabled") is True, str(got))
got = probe(chr(10).join(["BLOG_LOGIN_CHECK=off", "BLOG_FAST_WAIT=1"]))
check("다른 설정과 함께 있어도 읽힌다", got.get("enabled") is True, str(got))

appdir_src = (ROOT / "v2" / "appdir.py").read_text(encoding="utf-8")
check("설치본 ROOT 는 APPDATA 아래 BlogLandingAgent 다(frozen 분기)",
      "APPDATA" in appdir_src and "BlogLandingAgent" in appdir_src
      and "def frozen()" in appdir_src)
check("config 가 ROOT 의 .env 를 읽는다",
      "load_dotenv(ROOT" in (ROOT / "v2" / "config.py").read_text(
          encoding="utf-8"))
print("")
print("=" * 74)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print("=" * 74)
raise SystemExit(1 if fails else 0)
