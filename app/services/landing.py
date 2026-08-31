from __future__ import annotations

from typing import Callable

from app.models import PublishedLanding, ReferenceUrl


LogFn = Callable[[str], None]


class LandingPublisher:
    def __init__(self, enabled: bool, log: LogFn) -> None:
        self.enabled = enabled
        self.log = log

    def publish_one_from_reference(self, reference: ReferenceUrl) -> PublishedLanding:
        if not self.enabled:
            self.log(f"외부 작업 비활성화 상태: 발행 대신 dry-run 처리: {reference.url}")
            return PublishedLanding(
                source_url=reference.url,
                published_url="DRY_RUN_NOT_PUBLISHED",
                title="DRY_RUN_TITLE",
            )

        raise NotImplementedError(
            "실제 네이버 블로그 복제/발행 자동화는 수동 로그인 및 단건 테스트 단계에서 구현합니다."
        )

