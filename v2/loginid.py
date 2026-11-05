r"""login_id 정규화 — **비교할 때만** 쓰는 아주 작은 모듈 (2026-11-04).

왜 따로 만들었나
    네이버 로그인 ID 는 같은 계정이 두 형태로 적힌다 — `shin894894` 와
    `shin894894@naver.com`. 운영 등록표(11/03 15:38 기준 18건)에 실제로 둘이
    섞여 있다(`shin894894` 등록만 도메인 포함). 문자열 그대로 비교하면
    **같은 계정이 서로 다른 계정으로 보인다.** 그 상태에서 인증 ID 대조를 켜면
    `login_state.check_publish` 가 정상 계정의 발행을 '계정 불일치' 로 막는다.

★이 모듈은 **저장값을 고치지 않는다.** 비교할 때만 도메인을 떼어 본다.
  운영 계정 데이터·세션 파일·작업 payload 는 적힌 그대로 남는다.

★의존성이 없다(표준 라이브러리도 안 쓴다). `login_state` 와 `queue_store`
  양쪽에서 쓰는데, 서로를 import 하면 순환이 생기기 때문이다.
"""
from __future__ import annotations

# 네이버가 로그인 ID 로 받아 주는 메일 도메인. 이 꼬리만 떼어 본다.
#   ★`@gmail.com` 같은 남의 도메인은 떼지 않는다 — 그건 다른 계정일 수 있다.
NAVER_MAIL_DOMAINS = ("@naver.com", "@mail.naver.com", "@naver.net")


def norm_login(value) -> str:
    """비교용 login_id. `None`·공백·대소문자·네이버 메일 도메인을 흡수한다.

        norm_login(None)                      == ""
        norm_login("  shin894894 ")           == "shin894894"
        norm_login("shin894894@naver.com")    == "shin894894"
        norm_login("Shin894894@NAVER.COM")    == "Shin894894"   # 대소문자는 보존
        norm_login("someone@gmail.com")       == "someone@gmail.com"
    """
    v = str(value or "").strip()
    low = v.casefold()
    for dom in NAVER_MAIL_DOMAINS:
        if low.endswith(dom) and len(v) > len(dom):
            return v[: -len(dom)]
    return v


def same_login(a, b) -> bool:
    """두 login_id 가 같은 계정인가. **빈 값은 언제나 False**(추측 금지)."""
    x, y = norm_login(a), norm_login(b)
    return bool(x) and bool(y) and x.casefold() == y.casefold()
