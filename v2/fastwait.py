r"""고정 대기를 **조건부 대기**로 바꾸는 작은 헬퍼 (2026-11-03, 3단계).

왜 필요한가
    발행 로그 측정 결과(`scripts/analyze_publish_timing.py`) 1건 평균 55.8초 중
    **고정 `wait_for_timeout(…)` 이 차지하는 몫**이 적지 않았다. 그중 상당수는
    "바로 다음 줄에서 확인할 조건"을 그냥 시간으로 기다리는 것이었다.
    (예: 글쓰기 화면을 열고 2.5초 고정 대기 → 그 뒤 팝업/에디터를 확인한다)

안전 규칙 — 이 헬퍼는 **절대 더 오래, 더 위험하게 기다리지 않는다**
    ① 총 대기는 옛 고정값 `ms` 를 **넘지 않는다.** 조건이 끝까지 참이 아니면
       기다린 시간이 옛 코드와 **정확히 같다**(동작이 달라지지 않는다).
    ② 조건은 **바로 다음 줄이 검사하는 그것**만 쓴다(새 판정을 만들지 않는다).
    ③ 조건 함수가 예외를 던지면 '아직 아님' 으로 보고 계속 기다린다.
    ④ **기본은 꺼짐**이다. `BLOG_FAST_WAIT=1` 을 넣은 PC 에서만 켜진다.
       설정이 없는 PC(= 설정을 빠뜨린 PC)는 1.2.5 와 **완전히 같은 대기**로 돈다.
"""
from __future__ import annotations

import os

#  켜는 값 / 끄는 값. 그 밖의 값(빈 값·오타·미설정)은 **끈 것으로 본다**.
ON = ("1", "on", "true", "yes", "y")
OFF = ("0", "off", "false", "no", "n", "")


def enabled() -> bool:
    """조건부 대기를 쓸 것인가 — **기본 꺼짐(opt-in)**.

        설정 없음        → 꺼짐(= 1.2.5 와 동일한 고정 대기)
        BLOG_FAST_WAIT=0 → 꺼짐
        BLOG_FAST_WAIT=1 → 켜짐

    ★기본을 꺼짐으로 둔 이유(2026-11-03): 사용자 PC 마다 `.env` 를 손으로 넣어야
      하는데 그걸 빠뜨리면 **의도와 반대로 최적화가 켜진 채** 돌아간다.
      안전한 쪽(기존 동작)이 기본이어야 한다.
    """
    return (os.getenv("BLOG_FAST_WAIT") or "").strip().lower() in ON


async def settle(page, ms: int, check=None, *, step: int = 150,
                 min_ms: int = 0) -> bool:
    """옛 `await page.wait_for_timeout(ms)` 자리에 그대로 넣는다.

    `check()` 가 참이 되면 **그 즉시** 돌아온다(참이었으면 True).
    `check` 가 없거나 스위치가 꺼져 있으면 옛 고정 대기와 완전히 같다.

    · `min_ms` — 조건을 보기 **전에** 반드시 기다릴 시간(화면이 바뀌는 중에
      옛 상태를 '이미 됐다' 로 잘못 읽는 것을 막아야 할 때만 쓴다).
    """
    ms = max(0, int(ms))
    if check is None or not enabled():
        await page.wait_for_timeout(ms)
        return False

    waited = 0
    if min_ms > 0:
        waited = min(min_ms, ms)
        await page.wait_for_timeout(waited)

    while True:
        try:
            if await check():
                return True
        except Exception:                                      # noqa: BLE001
            pass                              # 아직 준비가 안 된 것으로 본다
        if waited >= ms:
            return False
        slice_ms = min(max(1, int(step)), ms - waited)
        await page.wait_for_timeout(slice_ms)
        waited += slice_ms
