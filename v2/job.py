r"""실행 1건의 설정(Job) → CLI argv.

GUI 는 Playwright 를 직접 부르지 않는다. **여기서 만든 argv 로 기존 CLI 를 그대로 실행**한다.
  · 검수용 랜딩 생성  → `python -m v2.run ...`
  · 실전용 랜딩 전환  → `python -m v2.run_production ...`

★`kind`(참고 랜딩 컬럼: 검수용/실전용)와 `flow`(어느 플로우를 돌릴지)는 **다른 축**이다.
  - flow=review     : 새 글을 만든다  → `--kind`
  - flow=production : 이미 있는 글을 실전용으로 바꾼다 → `--ref-kind`
  둘 다 "참고 랜딩 시트의 어느 열을 읽을지"만 뜻한다. 섞지 않는다.

나중에 Streamlit 으로 옮길 때도 이 모듈은 그대로 쓴다(화면만 바뀐다).
"""
from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .appdir import SRC_ROOT as ROOT   # venv 탐색용(소스 폴더)

FLOWS = {
    "review": {"module": "v2.run", "label": "검수용 랜딩 생성 (새 글)"},
    "production": {"module": "v2.run_production", "label": "실전용 랜딩 전환 (기존 글 수정)"},
}
KINDS = ("검수용", "실전용")
PROD_MODES = {"convert": "기존 검수용 글을 수정", "create": "새 글로 만들기"}


def module_command(module: str, args: list[str],
                   python: str | None = None) -> list[str]:
    """`python -m <module> …` 을 만든다.

    ★설치본(PyInstaller)에는 python.exe 가 없다. 그때는 **자기 자신**을
      `BlogLandingAgent.exe --module v2.run …` 형태로 다시 부른다
      (`agent_tray.py` 가 맨 앞에서 이 인자를 받아 그 모듈을 실행한다).
    """
    import sys as _sys

    if getattr(_sys, "frozen", False) and not python:
        return [_sys.executable, "--module", module, *args]
    return [python or python_exe(), "-m", module, *args]


def python_exe() -> str:
    """프로젝트 venv 의 python. 없으면 지금 돌고 있는 python."""
    for cand in (ROOT / ".venv" / "Scripts" / "python.exe",
                 ROOT / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


@dataclass
class Job:
    flow: str = "review"                 # review / production
    # ★브랜드 = 기준시트 + UTM 빌더 한 세트. 비우면 기본 브랜드(리퓨어리) = 기존 동작.
    #   큐에 들어간 Job 만 보고도 어느 브랜드 작업인지 알 수 있어야 한다.
    brand: str = ""
    # ★설치본 PC 에는 brands.json 이 없다. 화면이 고른 브랜드 설정을 그대로 실어 보내
    #   에이전트가 환경변수(BLOG_BRANDS_JSON)로 넘겨 준다. CLI 인자로는 나가지 않는다.
    brand_config: str = ""
    account: str = ""
    media: str = ""
    deficiency: str = ""
    kind: str = "검수용"                  # 참고 랜딩 컬럼(플로우와 별개)
    count: int = 1
    publish: bool = False
    dry_run: bool = False
    headless: bool | None = None         # None = 각 CLI 기본값 그대로
    events: bool = True
    ref_tab: str = ""
    # ★계정은 화면에서 이미 확정해 보낸다(로컬 파일에 기대지 않는다).
    #   실행 기록에도 "계정=행복하서연 (rhksrhf6996)" 처럼 남는다.
    account_name: str = ""
    login_id: str = ""
    # ── 검수용(v2.run) 전용 ────────────────────────────────────────
    product_url: str = ""
    url: str = ""
    sheet_media: str = ""
    sheet_date: str = ""
    sheet_campaign: str = ""
    sheet_product: str | None = None     # None = --deficiency 앞 단어 자동
    batch: int = 0
    # ── 실전용(v2.run_production) 전용 ─────────────────────────────
    date: str = ""
    campaign: str = ""                   # 실전용 --campaign (utm_campaign 접두사)
    rows: str = ""                       # 실전용 --rows (실패한 시트 행만 다시 실행)
    mode: str = "convert"                # convert / create
    content_from: str = "ref"            # ref / review
    start: int = 1
    on_error: str = ""                   # ""=CLI 기본(abort) / "skip"=실패 건만 건너뛴다
    extra: list[str] = field(default_factory=list)   # 필요할 때 직접 덧붙이는 인자

    # ── 검증 ──────────────────────────────────────────────────────
    def validate(self) -> list[str]:
        bad = []
        if self.brand:
            try:
                from . import brands
                brands.find_brand(self.brand)
            except Exception as exc:                           # noqa: BLE001
                bad.append(str(exc).splitlines()[0])
        if self.flow not in FLOWS:
            bad.append(f"알 수 없는 플로우: {self.flow}")
        if self.kind not in KINDS:
            bad.append(f"참고 랜딩 종류는 {KINDS} 중 하나여야 합니다")
        if self.flow == "review":
            if not self.url and not (self.media and self.deficiency):
                bad.append("매체와 결핍을 고르세요(또는 참고 URL 직접 지정).")
            if self.count < 1:
                bad.append("생성 개수는 1 이상이어야 합니다.")
        else:
            if not (self.media and self.deficiency):
                bad.append("매체와 결핍을 고르세요.")
            if self.mode not in PROD_MODES:
                bad.append(f"실전용 방식은 {tuple(PROD_MODES)} 중 하나여야 합니다")
        return bad

    # ── argv ─────────────────────────────────────────────────────
    def to_argv(self) -> list[str]:
        a: list[str] = []
        add = a.extend

        def flag(name: str, value) -> None:
            if value not in (None, "", False):
                add([name, str(value)])

        flag("--brand", self.brand)
        flag("--account", self.account)
        flag("--ref-tab", self.ref_tab)
        flag("--media", self.media)
        flag("--deficiency", self.deficiency)

        if self.flow == "review":
            flag("--kind", self.kind)
            flag("--url", self.url)
            flag("--product-url", self.product_url)
            add(["--count", str(max(1, int(self.count)))])
            flag("--sheet-media", self.sheet_media)
            flag("--sheet-date", self.sheet_date)
            flag("--sheet-campaign", self.sheet_campaign)
            if self.sheet_product is not None:       # 빈 문자열 = 대조 끄기(의미 있음)
                add(["--sheet-product", self.sheet_product])
            if self.batch:
                add(["--batch", str(self.batch)])
        else:
            flag("--ref-kind", self.kind)
            flag("--date", self.date)
            flag("--campaign", self.campaign)
            flag("--rows", self.rows)
            flag("--mode", self.mode)
            flag("--content-from", self.content_from)
            if int(self.count) > 0:
                add(["--count", str(int(self.count))])
            if int(self.start) > 1:
                add(["--start", str(int(self.start))])
            flag("--on-error", self.on_error)

        if self.publish:
            add(["--publish"])
        if self.dry_run:
            add(["--dry-run"])
        if self.events:
            add(["--events"])
        if self.headless is True:
            add(["--headless"])
        elif self.headless is False:
            add(["--no-headless"])
        add(list(self.extra))
        return a

    def module(self) -> str:
        return FLOWS[self.flow]["module"]

    def command(self, python: str | None = None) -> list[str]:
        return module_command(self.module(), self.to_argv(), python)

    def command_line(self, python: str | None = None) -> str:
        """사람이 터미널에 그대로 붙여넣을 수 있는 한 줄(로그·재현용)."""
        def q(t: str) -> str:
            return f'"{t}"' if (not t or " " in t) else t

        return " ".join(q(x) for x in self.command(python))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {f for f in cls.__dataclass_fields__}          # noqa: SLF001
        return cls(**{k: v for k, v in (data or {}).items() if k in known})
