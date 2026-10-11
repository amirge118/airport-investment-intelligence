"""Focused tests for the FAA TAF forecast parser/math/coverage logic.
No network: the
XLSX structure is built in-memory to match the real worksheet's inline
cell format exactly (verified against the real downloaded archive during
implementation; the real refresh is run manually, deliberately, by
`python3 scripts/refresh_taf.py`).
"""

import io
import sys
import unittest
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import refresh_taf  # noqa: E402


def _row_xml(row_num: int, locid: str, scenario: str, year: str, values: tuple) -> str:
    cells = f'<x:c r="A{row_num}" t="inlineStr"><x:is><x:t>{locid}</x:t></x:is></x:c>'
    cells += f'<x:c r="B{row_num}" t="n"><x:v>{scenario}</x:v></x:c>'
    cells += f'<x:c r="C{row_num}" t="n"><x:v>{year}</x:v></x:c>'
    for col, value in zip("DEFGH", values):
        cells += f'<x:c r="{col}{row_num}" t="n"><x:v>{value}</x:v></x:c>'
    return f'<x:row r="{row_num}">{cells}</x:row>'


def _build_enplanements_xlsx(data_rows: list[str]) -> bytes:
    header = ('<x:row r="1"><x:c r="A1" t="inlineStr"><x:is><x:t>locid</x:t></x:is></x:c>'
              '<x:c r="B1" t="inlineStr"><x:is><x:t>scenario</x:t></x:is></x:c>'
              '<x:c r="C1" t="inlineStr"><x:is><x:t>ayear</x:t></x:is></x:c></x:row>')
    sheet_xml = ('<?xml version="1.0" encoding="utf-8"?>'
                 '<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<x:sheetData>' + header + "".join(data_rows) + '</x:sheetData></x:worksheet>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as workbook_zip:
        workbook_zip.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def _build_archive(xlsx_bytes: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Enplanements.xlsx", xlsx_bytes)
    return buffer.getvalue()


class ExtractEnplanementsTests(unittest.TestCase):
    def test_parses_forecast_rows_and_strips_locid_padding(self):
        rows = [
            _row_xml(2, "ANC ", "1", "2025", (2369014, 10, 309407, 1251, 11955)),
            _row_xml(3, "ANC ", "1", "2030", (2675777, 10, 313161, 1551, 7223)),
        ]
        archive = _build_archive(_build_enplanements_xlsx(rows))
        parsed = refresh_taf.extract_enplanements(archive)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["code"], "ANC")  # trailing space stripped
        self.assertEqual(parsed[0]["enplanements"], 2691637)  # matches the real ANC FY2025 total
        self.assertEqual(parsed[1]["enplanements"], 2997722)  # matches the real ANC FY2030 total

    def test_ignores_non_forecast_scenario(self):
        rows = [_row_xml(2, "XXX", "0", "2025", (100, 0, 0, 0, 0))]
        archive = _build_archive(_build_enplanements_xlsx(rows))
        self.assertEqual(refresh_taf.extract_enplanements(archive), [])

    def test_ignores_years_outside_2025_and_2030(self):
        rows = [_row_xml(2, "XXX", "1", "2027", (100, 0, 0, 0, 0))]
        archive = _build_archive(_build_enplanements_xlsx(rows))
        self.assertEqual(refresh_taf.extract_enplanements(archive), [])

    def test_header_row_is_not_treated_as_data(self):
        rows = [_row_xml(2, "XXX", "1", "2025", (1, 1, 1, 1, 1))]
        archive = _build_archive(_build_enplanements_xlsx(rows))
        parsed = refresh_taf.extract_enplanements(archive)
        self.assertEqual(len(parsed), 1)  # only the one data row, not the header


class BuildFaaTafTests(unittest.TestCase):
    def test_change_pct_formula(self):
        rows = [{"code": "AAA", "year": 2025, "enplanements": 1_000_000},
                {"code": "AAA", "year": 2030, "enplanements": 1_100_000}]
        result, counts = refresh_taf.build_faa_taf(rows, {"AAA"})
        self.assertAlmostEqual(result["AAA"]["change_pct"], 10.0, places=6)
        self.assertEqual(counts["registry_pairs"], 1)
        self.assertEqual(counts["positive_pairs"], 1)

    def test_non_us_code_excluded(self):
        rows = [{"code": "LHR", "year": 2025, "enplanements": 1}, {"code": "LHR", "year": 2030, "enplanements": 1}]
        result, counts = refresh_taf.build_faa_taf(rows, {"AAA"})
        self.assertEqual(result, {})
        self.assertEqual(counts["registry_pairs"], 0)

    def test_missing_year_pair_excluded(self):
        rows = [{"code": "AAA", "year": 2025, "enplanements": 100}]  # no 2030
        result, counts = refresh_taf.build_faa_taf(rows, {"AAA"})
        self.assertEqual(result, {})
        self.assertEqual(counts["registry_pairs"], 0)

    def test_non_positive_pair_counted_but_not_published(self):
        rows = [{"code": "AAA", "year": 2025, "enplanements": 0}, {"code": "AAA", "year": 2030, "enplanements": 500}]
        result, counts = refresh_taf.build_faa_taf(rows, {"AAA"})
        self.assertEqual(result, {})  # never a measured-zero-as-fact figure
        self.assertEqual(counts["registry_pairs"], 1)
        self.assertEqual(counts["positive_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
