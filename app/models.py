from __future__ import annotations

from dataclasses import dataclass


MEDIA_OPTIONS = ["GFA", "카카오모먼트", "메타", "틱톡"]
REFERENCE_SHEET_NAME = "참고용 랜딩"

LANDING_RECORD_HEADERS = [
    "생성일",
    "매체",
    "결핍",
    "참고 URL",
    "생성된 랜딩 URL",
    "상태",
]


@dataclass(frozen=True)
class ReferenceUrl:
    media: str
    deficiency: str
    url: str
    row_number: int


@dataclass(frozen=True)
class PublishedLanding:
    source_url: str
    published_url: str
    title: str
    status: str = "발행완료"
