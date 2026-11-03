r"""시각 표기 한 곳 — **표시는 언제나 KST(UTC+9)**.

왜 필요한가(2026-10-30 실측):
  · PC 로그는 `datetime.now()` = 그 PC 의 로컬 시각인데 **tz 정보가 없다**(naive).
  · Supabase 는 `now()` = **UTC** 로 저장한다(`timestamptz`).
  · 화면의 `astimezone()` 은 **코드가 도는 환경의 시간대**로 바꾼다. Streamlit
    Community Cloud 는 UTC 라서 한국 시각보다 9시간 전으로 보였다.
→ 저장은 UTC(또는 오프셋을 가진 값), **표시는 이 모듈을 거쳐 KST** 로 고정한다.

이 모듈은 다른 v2 모듈을 import 하지 않는다(어디서든 안전하게 쓰기 위해).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    """지금(KST, tz 정보 포함)."""
    return datetime.now(KST)


def to_kst(value) -> datetime | None:
    """문자열·datetime 을 KST aware 로. 못 읽으면 None.

    ★tz 정보가 없는 값은 **KST 로 간주**한다 — 그런 값은 PC(한국)에서 찍힌
      로그뿐이기 때문이다. UTC 로 가정하면 9시간 밀린다.
    """
    if value in (None, ""):
        return None
    dt = value
    if not isinstance(dt, datetime):
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:                                   # 'YYYY-MM-DDTHH:MM:SS…' 앞부분만
                dt = datetime.fromisoformat(text[:19])
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def fmt(value, pattern: str = "%m-%d %H:%M:%S", suffix: str = " KST") -> str:
    """표시용 문자열. 못 읽으면 원문을 그대로 돌려준다(정보를 잃지 않게)."""
    dt = to_kst(value)
    if dt is None:
        return "" if value in (None, "") else str(value)
    return f"{dt.strftime(pattern)}{suffix}"


def ago(value) -> str:
    """'3분 전' 처럼 얼마나 지났는지(표시용). 못 읽으면 빈 문자열."""
    dt = to_kst(value)
    if dt is None:
        return ""
    sec = int((now_kst() - dt).total_seconds())
    if sec < 0:
        return "방금"
    if sec < 60:
        return f"{sec}초 전"
    if sec < 3600:
        return f"{sec // 60}분 전"
    if sec < 86400:
        return f"{sec // 3600}시간 전"
    return f"{sec // 86400}일 전"
