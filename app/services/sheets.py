from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from app.models import REFERENCE_SHEET_NAME, ReferenceUrl


LogFn = Callable[[str], None]
ReferenceKind = Literal["검수용", "실전용"]


# 시트에 적힌 매체 표기와 프로그램에서 쓰는 이름이 달라서 맞춰준다.
#   실제 시트 값: gfa / 카모 / 메타 / 틱톡
MEDIA_ALIASES = {
    "gfa": {"gfa", "지에프에이", "네이버gfa"},
    "카모": {"카모", "카카오모먼트", "카카오", "kakao", "kakaomoment"},
    "메타": {"메타", "meta", "페이스북", "facebook"},
    "틱톡": {"틱톡", "tiktok"},
}


def canonical_media(value: str) -> str:
    """사용자가 뭐라 적든 시트 표기로 바꾼다. 모르면 원문 그대로."""
    v = (value or "").strip().casefold()
    for canon, names in MEDIA_ALIASES.items():
        if v == canon.casefold() or v in {n.casefold() for n in names}:
            return canon
    return (value or "").strip()


_URL_RE = re.compile(r"^https?://", re.I)


def is_landing_url(value: str) -> bool:
    """실제 랜딩 URL인지. 빈칸이거나 '곰도리'처럼 계정명만 적힌 칸은 아직 준비중으로 본다."""
    return bool(_URL_RE.match((value or "").strip()))


@dataclass(frozen=True)
class ReferenceLookup:
    """매체+결핍 단건 조회 결과."""

    media: str
    deficiency: str
    row_number: int | None = None      # 조합이 시트에 있으면 그 행
    url: str = ""                      # 검수용 칸의 값(URL이 아닐 수도 있음)
    production_url: str = ""           # 실전용 유무 안내용. **사용하지 않는다**

    @property
    def combination_found(self) -> bool:
        return self.row_number is not None

    @property
    def usable(self) -> bool:
        """랜딩이 실제로 들어가 있는 칸만 사용 가능."""
        return is_landing_url(self.url)

    @property
    def blocked_reason(self) -> str:
        if not self.combination_found:
            return "조합 없음"
        if not (self.url or "").strip():
            return "검수용 칸이 비어 있음(랜딩 준비중)"
        if not is_landing_url(self.url):
            return f"검수용 칸에 URL이 아닌 값이 적혀 있음: {self.url!r} (랜딩 준비중)"
        return ""


@dataclass(frozen=True)
class SheetStructure:
    sheet_name: str
    header_row: int
    headers: list[str]
    data_start_row: int | None
    media_column_index: int | None
    deficiency_column_index: int | None
    review_reference_column_index: int | None
    production_reference_column_index: int | None


class SheetsClient:
    def __init__(
        self,
        credential_path: Path | None,
        reference_spreadsheet_id: str,
        enabled: bool,
        log: LogFn,
        readonly: bool = False,
    ) -> None:
        self.credential_path = credential_path
        self.reference_spreadsheet_id = reference_spreadsheet_id
        self.enabled = enabled
        self.log = log
        self.readonly = readonly
        self._client = None

    def _guard_enabled(self) -> None:
        if not self.enabled and not self.readonly:
            raise RuntimeError("외부 작업이 비활성화되어 있습니다. ENABLE_EXTERNAL_ACTIONS=true 설정 후 실행하세요.")
        if not self.credential_path:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON 경로가 설정되지 않았습니다.")
        if not self.credential_path.exists():
            raise RuntimeError(f"서비스 계정 JSON 파일을 찾을 수 없습니다: {self.credential_path}")
        if not self.reference_spreadsheet_id:
            raise RuntimeError("REFERENCE_SPREADSHEET_ID가 설정되지 않았습니다.")

    def _get_client(self):
        self._guard_enabled()
        if self._client is None:
            import gspread
            from google.oauth2.service_account import Credentials

            scope = "https://www.googleapis.com/auth/spreadsheets.readonly" if self.readonly else "https://www.googleapis.com/auth/spreadsheets"
            credentials = Credentials.from_service_account_file(str(self.credential_path), scopes=[scope])
            self._client = gspread.authorize(credentials)
        return self._client

    def analyze_reference_sheet(self) -> SheetStructure:
        worksheet = self._open_reference_sheet()
        rows = worksheet.get_all_values()
        header_row = _infer_header_row(rows)
        headers = rows[header_row - 1] if rows else []
        return SheetStructure(
            sheet_name=REFERENCE_SHEET_NAME,
            header_row=header_row,
            headers=headers,
            data_start_row=_infer_data_start_row(rows, header_row),
            media_column_index=_find_header(headers, ["매체", "media"]),
            deficiency_column_index=_find_header(headers, ["결핍", "결핍명", "deficiency"]),
            review_reference_column_index=_find_header(headers, ["검수용 블로그랜딩 참고", "검수용블로그랜딩참고"]),
            production_reference_column_index=_find_header(headers, ["실전용 블로그랜딩 참고", "실전용블로그랜딩참고"]),
        )

    def find_reference_urls(
        self,
        media: str,
        deficiency: str,
        reference_kind: ReferenceKind = "검수용",
    ) -> list[ReferenceUrl]:
        worksheet = self._open_reference_sheet()
        rows = worksheet.get_all_values()
        if not rows:
            return []

        structure = self.analyze_reference_sheet()
        media_idx = structure.media_column_index
        deficiency_idx = structure.deficiency_column_index
        url_idx = (
            structure.review_reference_column_index
            if reference_kind == "검수용"
            else structure.production_reference_column_index
        )

        if media_idx is None or deficiency_idx is None or url_idx is None:
            raise RuntimeError("참고용 URL 시트에서 매체/결핍/선택한 참고 URL 컬럼을 찾지 못했습니다.")

        matches: list[ReferenceUrl] = []
        current_media = ""
        for row_number, row in enumerate(rows[structure.header_row :], start=structure.header_row + 1):
            row_media = _cell(row, media_idx)
            if row_media:
                current_media = row_media
            if (
                _matches(current_media, media)
                and _matches(_cell(row, deficiency_idx), deficiency)
                and _cell(row, url_idx)
            ):
                matches.append(
                    ReferenceUrl(
                        media=current_media,
                        deficiency=_cell(row, deficiency_idx),
                        url=_cell(row, url_idx),
                        row_number=row_number,
                    )
                )
        return matches

    def lookup_reference(
        self,
        media: str,
        deficiency: str,
        reference_kind: ReferenceKind = "검수용",
    ) -> ReferenceLookup:
        """매체+결핍 1건 조회. URL이 비어 있어도 '조합은 찾았다'를 알 수 있게 돌려준다."""
        rows, structure = self._rows_and_structure()
        mi, di = structure.media_column_index, structure.deficiency_column_index
        ri = structure.review_reference_column_index
        pi = structure.production_reference_column_index
        if mi is None or di is None or ri is None:
            raise RuntimeError("시트에서 매체/결핍/검수용 참고 컬럼을 찾지 못했습니다.")
        want_media = canonical_media(media)
        url_idx = ri if reference_kind == "검수용" else pi

        current = ""
        for row_number, row in enumerate(rows[structure.header_row:], start=structure.header_row + 1):
            row_media = _cell(row, mi)
            if row_media:
                current = row_media
            row_def = _cell(row, di)
            if not row_def:
                continue
            if not _matches(canonical_media(current), want_media):
                continue
            if not _matches(row_def, deficiency):
                continue
            return ReferenceLookup(
                media=current,
                deficiency=row_def,
                row_number=row_number,
                url=_cell(row, url_idx) if url_idx is not None else "",
                production_url=_cell(row, pi) if pi is not None else "",
            )
        return ReferenceLookup(media=want_media, deficiency=deficiency.strip())

    def list_combinations(self) -> list[tuple[str, str, bool, bool]]:
        """(매체, 결핍, 검수용 있음, 실전용 있음) 전체 목록. 선택지 안내용."""
        rows, structure = self._rows_and_structure()
        mi, di = structure.media_column_index, structure.deficiency_column_index
        ri, pi = structure.review_reference_column_index, structure.production_reference_column_index
        out: list[tuple[str, str, bool, bool]] = []
        current = ""
        for row in rows[structure.header_row:]:
            m = _cell(row, mi) if mi is not None else ""
            if m:
                current = m
            d = _cell(row, di) if di is not None else ""
            if not d:
                continue
            out.append((
                current,
                d,
                bool(_cell(row, ri)) if ri is not None else False,
                bool(_cell(row, pi)) if pi is not None else False,
            ))
        return out

    def list_combinations_raw(self) -> list[tuple[str, str, str, str]]:
        """(매체, 결핍, 검수용 칸 원문, 실전용 칸 원문). 빈칸/계정명까지 그대로 보여준다."""
        rows, structure = self._rows_and_structure()
        mi, di = structure.media_column_index, structure.deficiency_column_index
        ri, pi = structure.review_reference_column_index, structure.production_reference_column_index
        out: list[tuple[str, str, str, str]] = []
        current = ""
        for row in rows[structure.header_row:]:
            m = _cell(row, mi) if mi is not None else ""
            if m:
                current = m
            d = _cell(row, di) if di is not None else ""
            if not d:
                continue
            out.append((
                current,
                d,
                _cell(row, ri) if ri is not None else "",
                _cell(row, pi) if pi is not None else "",
            ))
        return out

    def _rows_and_structure(self):
        worksheet = self._open_reference_sheet()
        rows = worksheet.get_all_values()
        return rows, self.analyze_reference_sheet()

    def _open_reference_sheet(self):
        client = self._get_client()
        spreadsheet = client.open_by_key(self.reference_spreadsheet_id)
        normalized_expected = _normalize_header(REFERENCE_SHEET_NAME)
        for worksheet in spreadsheet.worksheets():
            if _normalize_header(worksheet.title) == normalized_expected:
                return worksheet
        raise RuntimeError(f"시트 탭을 찾지 못했습니다: {REFERENCE_SHEET_NAME}")


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return row[index].strip()


def _matches(actual: str, expected: str) -> bool:
    return actual.strip().casefold() == expected.strip().casefold()


def _find_header(headers: list[str], candidates: list[str]) -> int | None:
    normalized = [_normalize_header(header) for header in headers]
    candidate_set = {_normalize_header(candidate) for candidate in candidates}
    for index, header in enumerate(normalized):
        if header in candidate_set:
            return index
    return None


def _infer_header_row(rows: list[list[str]]) -> int:
    for row_number, row in enumerate(rows[:10], start=1):
        normalized = [_normalize_header(cell) for cell in row]
        if "매체" in normalized and "결핍" in normalized:
            return row_number
    return 1


def _infer_data_start_row(rows: list[list[str]], header_row: int) -> int | None:
    for row_number, row in enumerate(rows[header_row:], start=header_row + 1):
        if any(cell.strip() for cell in row):
            return row_number
    return None


def _normalize_header(value: str) -> str:
    return value.strip().replace(" ", "").lower()

# ── 발행 URL 기록('랜딩' 탭) ─────────────────────────────────────────────
LANDING_SHEET_NAME = "랜딩"
BLOG_LINK_HEADER = "블로그 링크"


def _find_header_col(headers: list[str], name: str) -> int | None:
    want = _normalize_header(name)
    for i, h in enumerate(headers):
        if _normalize_header(h) == want:
            return i
    return None


class BlogLinkWriter:
    """'랜딩' 탭 '블로그 링크' 열의 빈 셀에 발행 URL 을 순차 기록한다.

    · 값이 있는 셀은 절대 덮어쓰지 않는다.
    · 쓰기 권한이 필요하므로 readonly=False 로 만든 클라이언트를 쓴다.
    """

    def __init__(self, client: "SheetsClient", log: LogFn) -> None:
        self.client = client
        self.log = log

    def _open(self):
        gc = self.client._get_client()
        sh = gc.open_by_key(self.client.reference_spreadsheet_id)
        target = _normalize_header(LANDING_SHEET_NAME)
        for ws in sh.worksheets():
            if _normalize_header(ws.title) == target:
                return ws
        raise RuntimeError(f"시트 탭을 찾지 못했습니다: {LANDING_SHEET_NAME}")

    def append_blog_links(self, urls: list[str]) -> dict:
        """URL 목록을 빈 '블로그 링크' 셀에 위에서부터 기록. 반환 {기록수, 행번호들}."""
        if not urls:
            return {"written": 0, "rows": []}
        ws = self._open()
        rows = ws.get_all_values()

        header_row = None
        col = None
        for idx in range(min(5, len(rows))):
            c = _find_header_col(rows[idx], BLOG_LINK_HEADER)
            if c is not None:
                header_row, col = idx + 1, c
                break
        if col is None:
            raise RuntimeError(f"'{BLOG_LINK_HEADER}' 열을 찾지 못했습니다")
        self.log(f"   [시트] '{LANDING_SHEET_NAME}' 탭 · 헤더 {header_row}행 · "
                 f"'{BLOG_LINK_HEADER}' = {chr(65 + col)}열")

        # 헤더 다음 행부터 빈 셀 찾기
        targets: list[int] = []
        r = header_row
        while len(targets) < len(urls):
            r += 1
            existing = rows[r - 1][col].strip() if (r - 1) < len(rows) and col < len(rows[r - 1]) else ""
            if existing:
                continue                     # 이미 값이 있으면 건너뛴다(덮어쓰지 않음)
            targets.append(r)
            if r > header_row + 500:
                break

        written = []
        for url, row in zip(urls, targets):
            cell = f"{chr(65 + col)}{row}"
            ws.update(values=[[url]], range_name=cell, value_input_option="RAW")
            self.log(f"   [시트] {cell} ← {url[:60]}")
            written.append(row)
        return {"written": len(written), "rows": written}
