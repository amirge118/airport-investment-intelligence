"""Fetch/parse logic for the three allowlisted public sources: BTS T-100
airport-year traffic, the OurAirports global registry, and the FAA TAF
forward outlook. Pure functions with no SQLite/JSON knowledge -- callers
(the SQLite auto-refresh in `ingestion/refresh.py`, or the manual
`scripts/refresh_*.py` utilities that still produce `data/snapshot.json`)
decide where the result is published. Relocated here, unchanged, from the
former `scripts/refresh_*.py` so both paths use the same proven adapter;
this build never scrapes the dynamic TranStats/CATS forms.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from hashlib import sha256

USER_AGENT = "AirportIntelligenceResearch/1.0"

# -- BTS T-100 airport-year traffic (Socrata API) ---------------------------

BTS_DATASET = "https://data.bts.gov/resource/r495-tyji.json"
BTS_SOURCE_PAGE = "https://data.bts.gov/d/r495-tyji"
MIN_BTS_AIRPORTS_PER_YEAR = 500


def get_json(url: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        value = json.load(response)
    if not isinstance(value, list):
        raise ValueError(f"Unexpected BTS response for {url.split('?')[0]}")
    return value


def fetch_bts_airport_year(year: int) -> list[dict]:
    params = {
        "$select": ("origin_airport_code,year,max(origin_airport_name) as airport_name,"
                    "max(origin_city_name) as city_name,count(*) as months,"
                    "sum(total_passengers) as passengers,sum(total_seats) as seats,"
                    "sum(domestic_passengers) as domestic_passengers,"
                    "sum(domestic_seats) as domestic_seats,"
                    "sum(domestic_departures) as domestic_departures,"
                    "sum(outbound_international) as international_departures"),
        "$where": f"year='{year}'",
        "$group": "origin_airport_code,year",
        "$limit": "3000",
    }
    rows = get_json(BTS_DATASET + "?" + urllib.parse.urlencode(params))
    if len(rows) < MIN_BTS_AIRPORTS_PER_YEAR:
        raise ValueError(f"Insufficient BTS airport coverage for {year}: {len(rows)}")
    result = []
    for row in rows:
        code = row.get("origin_airport_code", "")
        if not isinstance(code, str) or not (2 <= len(code) <= 4) or not code.isalpha():
            continue
        result.append({
            "code": code.upper(), "year": year,
            "name": str(row.get("airport_name") or code).removesuffix(f" ({code})")[:160],
            "city": str(row.get("city_name") or "").removesuffix(f" ({code})")[:100],
            **{field: int(float(row.get(field) or 0)) for field in (
                "months", "passengers", "seats", "domestic_passengers",
                "domestic_seats", "domestic_departures", "international_departures"
            )},
        })
    return sorted(result, key=lambda item: (item["code"], item["year"]))


# -- OurAirports global registry (public-domain CSV) -------------------------

REGISTRY_SOURCE = "https://davidmegginson.github.io/ourairports-data/airports.csv"
REGISTRY_INFO_PAGE = "https://ourairports.com/data/"
MAX_REGISTRY_DOWNLOAD_BYTES = 20_000_000
MIN_REGISTRY_ROWS = 3000


def fetch_global_registry() -> list[dict]:
    request = urllib.request.Request(REGISTRY_SOURCE, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = response.read(MAX_REGISTRY_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_REGISTRY_DOWNLOAD_BYTES:
        raise ValueError("OurAirports download exceeded size limit")
    rows = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    expected = {"ident", "type", "name", "iso_country", "iso_region", "municipality",
                "iata_code", "icao_code", "scheduled_service"}
    if not expected.issubset(rows.fieldnames or []):
        raise ValueError("Unexpected OurAirports schema")
    registry = []
    seen = set()
    for row in rows:
        code = row["iata_code"].strip().upper()
        icao = row["icao_code"].strip().upper()
        if not (len(code) == 3 and code.isalpha()):
            continue
        if row["type"] not in {"large_airport", "medium_airport", "small_airport"}:
            continue
        if row["scheduled_service"] != "yes" and row["type"] == "small_airport":
            continue
        country = row["iso_country"].strip().upper()
        if len(country) != 2 or (code, country) in seen:
            continue
        seen.add((code, country))
        registry.append({
            "code": code, "icao": icao if len(icao) == 4 and icao.isalpha() else "",
            "name": row["name"].strip()[:160], "city": row["municipality"].strip()[:100],
            "country": country, "region": row["iso_region"].strip().upper()[:12],
            "type": row["type"], "scheduled": row["scheduled_service"] == "yes",
        })
    if len(registry) < MIN_REGISTRY_ROWS:
        raise ValueError(f"Global registry unexpectedly small: {len(registry)}")
    return sorted(registry, key=lambda item: (item["code"], item["country"]))


# -- FAA TAF forward enplanement outlook (official archive) ------------------

TAF_SOURCE_PAGE = "https://www.faa.gov/data_research/aviation/taf"
TAF_SOURCE_URL = "https://taf.faa.gov/Downloads/APO100_TAF_Final_2025.zip"
TAF_GLOSSARY_URL = "https://taf.faa.gov/Downloads/Glossaryfor2025TAF.pdf"
MAX_TAF_DOWNLOAD_BYTES = 40_000_000  # bounded; the official archive is ~15.7MB
ENPLANEMENT_COLUMNS = ("D", "E", "F", "G", "H")  # aac, aat, commuter, us_flag, frgn_flag
TAF_FORECAST_SCENARIO = "1"
TAF_FISCAL_PERIOD = "FY2025-FY2030"
MIN_TAF_REGISTRY_PAIRS = 500  # sanity floor; real data has ~795 (audit expectation, not hardcoded)
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def download_taf_archive() -> bytes:
    request = urllib.request.Request(TAF_SOURCE_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read(MAX_TAF_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_TAF_DOWNLOAD_BYTES:
        raise ValueError(f"FAA TAF download exceeded the {MAX_TAF_DOWNLOAD_BYTES} byte bound")
    return payload


def _iter_enplanement_rows(xlsx_bytes: bytes):
    """Yield one dict per data row (header row skipped) from Enplanements.xlsx.

    The worksheet has no sharedStrings table -- every cell is either an
    inline string (`t="inlineStr"`) or a bare numeric value.
    """
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as workbook:
        with workbook.open("xl/worksheets/sheet1.xml") as sheet:
            header_seen = False
            for event, elem in ET.iterparse(sheet, events=("end",)):
                if elem.tag != _XLSX_NS + "row":
                    continue
                cells: dict[str, str | None] = {}
                for cell in elem.iter(_XLSX_NS + "c"):
                    ref = cell.get("r") or ""
                    col = "".join(ch for ch in ref if ch.isalpha())
                    if cell.get("t") == "inlineStr":
                        inline = cell.find(_XLSX_NS + "is")
                        text_el = inline.find(_XLSX_NS + "t") if inline is not None else None
                        cells[col] = text_el.text if text_el is not None else ""
                    else:
                        value_el = cell.find(_XLSX_NS + "v")
                        cells[col] = value_el.text if value_el is not None else None
                elem.clear()
                if not header_seen:
                    header_seen = True
                    continue
                yield cells


def extract_enplanements(zip_bytes: bytes) -> list[dict]:
    """Rows for `scenario=1`, years 2025 and 2030 only, with locid code padding stripped."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        xlsx_bytes = archive.read("Enplanements.xlsx")
    rows = []
    for cells in _iter_enplanement_rows(xlsx_bytes):
        locid = (cells.get("A") or "").strip()
        scenario = cells.get("B")
        year = cells.get("C")
        if not locid or scenario != TAF_FORECAST_SCENARIO or year is None:
            continue
        try:
            year_int = int(year)
        except ValueError:
            continue
        if year_int not in (2025, 2030):
            continue
        try:
            total = sum(int(float(cells.get(col) or 0)) for col in ENPLANEMENT_COLUMNS)
        except (TypeError, ValueError):
            continue
        rows.append({"code": locid, "year": year_int, "enplanements": total})
    return rows


def build_faa_taf(rows: list[dict], us_codes: set[str]) -> tuple[dict, dict]:
    """Returns (faa_taf dict keyed by US code, coverage counts for the manifest)."""
    by_code: dict[str, dict[int, int]] = {}
    for row in rows:
        by_code.setdefault(row["code"], {})[row["year"]] = row["enplanements"]

    registry_pairs = 0
    positive_pairs = 0
    result: dict[str, dict] = {}
    for code, years in by_code.items():
        if code not in us_codes or 2025 not in years or 2030 not in years:
            continue
        registry_pairs += 1
        e2025, e2030 = years[2025], years[2030]
        if e2025 <= 0 or e2030 <= 0:
            continue
        positive_pairs += 1
        result[code] = {
            "enplanements_2025": e2025, "enplanements_2030": e2030,
            "change_pct": round((e2030 / e2025 - 1) * 100, 2),
        }
    return result, {"registry_pairs": registry_pairs, "positive_pairs": positive_pairs, "distinct_locids": len(by_code)}


def fetch_and_build_faa_taf(us_codes: set[str]) -> tuple[dict, dict, bytes]:
    """Convenience wrapper: download, parse, build. Returns (faa_taf, counts, raw_bytes)."""
    raw = download_taf_archive()
    rows = extract_enplanements(raw)
    faa_taf, counts = build_faa_taf(rows, us_codes)
    if counts["registry_pairs"] < MIN_TAF_REGISTRY_PAIRS:
        raise ValueError(f"Too few US-registry locid pairs with both FY2025 and FY2030: {counts['registry_pairs']}")
    return faa_taf, counts, raw


def taf_source_hash(raw: bytes) -> str:
    return sha256(raw).hexdigest()
