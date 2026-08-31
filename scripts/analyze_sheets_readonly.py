from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
REFERENCE_SHEET_NAME = "참고용 랜딩"


@dataclass(frozen=True)
class ColumnMatch:
    name: str
    index: int

    @property
    def a1(self) -> str:
        return column_number_to_name(self.index + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only analyzer for the blog landing reference sheet")
    parser.add_argument("--media", default="gfa")
    parser.add_argument("--deficiency", default="기미")
    parser.add_argument("--reference-kind", choices=["검수용", "실전용"], default="검수용")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")

    credential_path_value = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    credential_path = Path(credential_path_value) if credential_path_value else None
    reference_spreadsheet_id = os.getenv("REFERENCE_SPREADSHEET_ID", "").strip()

    if credential_path is None:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON is empty in .env", file=sys.stderr)
        return 2
    if not credential_path.exists():
        print(f"ERROR: credential JSON not found: {credential_path}", file=sys.stderr)
        return 2
    if not reference_spreadsheet_id:
        print("ERROR: REFERENCE_SPREADSHEET_ID is empty in .env", file=sys.stderr)
        return 2

    client = authorize_readonly(credential_path)
    spreadsheet = client.open_by_key(reference_spreadsheet_id)
    worksheet = find_worksheet(spreadsheet, REFERENCE_SHEET_NAME)
    if worksheet is None:
        print(f"ERROR: worksheet not found: {REFERENCE_SHEET_NAME}", file=sys.stderr)
        print("Available worksheets:", file=sys.stderr)
        for item in spreadsheet.worksheets():
            print(f"- {item.title}", file=sys.stderr)
        return 2
    rows = worksheet.get_all_values()

    print("READ ONLY Google Sheets analysis")
    print(f"- ENABLE_EXTERNAL_ACTIONS remains: {os.getenv('ENABLE_EXTERNAL_ACTIONS', 'false')}")
    print("- UTM Builder sheets: not read")
    print("- Writes: not executed")
    print()

    print_sheet_analysis(REFERENCE_SHEET_NAME, rows)

    print()
    print(
        "Reference URL lookup test: "
        f"media={args.media}, deficiency={args.deficiency}, reference_kind={args.reference_kind}"
    )
    match = find_reference_url(rows, args.media, args.deficiency, args.reference_kind)
    if match is None:
        print("- Result: not found")
        return 1

    row_number, url_col, url = match
    print("- Result: found")
    print(f"- Source row: {row_number}")
    print(f"- URL column: {column_number_to_name(url_col + 1)} ({url_col + 1})")
    print(f"- URL: {url}")
    return 0


def authorize_readonly(credential_path: Path):
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_file(str(credential_path), scopes=scopes)
    return gspread.authorize(credentials)


def find_worksheet(spreadsheet, expected_title: str):
    normalized_expected = normalize(expected_title)
    for worksheet in spreadsheet.worksheets():
        if normalize(worksheet.title) == normalized_expected:
            return worksheet
    return None


def print_sheet_analysis(sheet_name: str, rows: list[list[str]]) -> None:
    print(f"Sheet: {sheet_name}")
    print("- Exists: yes")
    if not rows:
        print("- Headers: none")
        print("- Data start row: none")
        return

    header_row_number = infer_header_row(rows)
    headers = rows[header_row_number - 1]
    data_start = infer_data_start_row(rows, header_row_number)

    print(f"- Header row: {header_row_number}")
    print(f"- Headers: {', '.join(header for header in headers if header.strip())}")
    print(f"- Data start row: {data_start or 'none'}")
    print("- Required columns:")
    for label, column in infer_columns(headers):
        print(f"  - {label}: {format_column(column)}")


def infer_columns(headers: list[str]) -> list[tuple[str, ColumnMatch | None]]:
    return [
        ("매체", find_header(headers, ["매체", "media"])),
        ("결핍", find_header(headers, ["결핍", "결핍명", "deficiency"])),
        ("검수용 블로그랜딩 참고", find_header(headers, ["검수용 블로그랜딩 참고", "검수용블로그랜딩참고"])),
        ("실전용 블로그랜딩 참고", find_header(headers, ["실전용 블로그랜딩 참고", "실전용블로그랜딩참고"])),
    ]


def infer_header_row(rows: list[list[str]]) -> int:
    for row_number, row in enumerate(rows[:10], start=1):
        normalized = [normalize(cell) for cell in row]
        if "매체" in normalized and "결핍" in normalized:
            return row_number
    return 1


def infer_data_start_row(rows: list[list[str]], header_row_number: int) -> int | None:
    for row_number, row in enumerate(rows[header_row_number:], start=header_row_number + 1):
        if any(cell.strip() for cell in row):
            return row_number
    return None


def find_reference_url(
    rows: list[list[str]],
    media: str,
    deficiency: str,
    reference_kind: str,
) -> tuple[int, int, str] | None:
    if not rows:
        return None

    header_row = infer_header_row(rows)
    headers = rows[header_row - 1]
    media_col = find_header(headers, ["매체", "media"])
    deficiency_col = find_header(headers, ["결핍", "결핍명", "deficiency"])
    url_col = find_header(
        headers,
        ["검수용 블로그랜딩 참고", "검수용블로그랜딩참고"]
        if reference_kind == "검수용"
        else ["실전용 블로그랜딩 참고", "실전용블로그랜딩참고"],
    )
    if media_col is None or deficiency_col is None or url_col is None:
        return None

    current_media = ""
    for row_number, row in enumerate(rows[header_row:], start=header_row + 1):
        row_media = cell(row, media_col.index)
        if row_media:
            current_media = row_media
        if (
            matches(current_media, media)
            and matches(cell(row, deficiency_col.index), deficiency)
            and cell(row, url_col.index)
        ):
            return row_number, url_col.index, cell(row, url_col.index)
    return None


def find_header(headers: list[str], candidates: list[str]) -> ColumnMatch | None:
    normalized_candidates = {normalize(candidate) for candidate in candidates}
    for index, header in enumerate(headers):
        if normalize(header) in normalized_candidates:
            return ColumnMatch(header.strip(), index)
    return None


def format_column(column: ColumnMatch | None) -> str:
    if column is None:
        return "not found"
    return f"{column.name} / {column.a1} ({column.index + 1})"


def cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return row[index].strip()


def matches(actual: str, expected: str) -> bool:
    return actual.strip().casefold() == expected.strip().casefold()


def normalize(value: str) -> str:
    return value.strip().replace(" ", "").lower()


def column_number_to_name(number: int) -> str:
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name


if __name__ == "__main__":
    raise SystemExit(main())
