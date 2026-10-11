"""Parse bounded, one-time official aviation extracts into normalized rows.

These adapters deliberately stop at typed rows and source statistics. They do
not know about SQLite, chat questions, or LLMs. The command-line importer owns
publication, so a malformed or incomplete extract is rejected before an active
database pointer can move.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import NamedTuple

T100_SOURCE_URL = "https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=Nv4+Pn44vr45&gnoyr_VQ=GDM"
DELAY_SOURCE_URL = "https://www.transtats.bts.gov/OT_Delay/OT_DelayCause1.asp"
NPIAS_SOURCE_URL = "https://www.faa.gov/airports/planning_capacity/npias/current"
NPIAS_DOWNLOAD_URL = "https://www.faa.gov/sites/faa.gov/files/airports/planning_capacity/npias/current/ARP-NPIAS-2025-2029-AppendixA.xlsx"

_SOURCE_YEAR = 2025
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Output measure -> source column. Measures are summed across source rows
# that share one key (RouteKey / AirportMonth).
_T100_MEASURES = {
    "departures": "DEPARTURES_PERFORMED", "seats": "SEATS",
    "passengers": "PASSENGERS", "freight_pounds": "FREIGHT",
}
_DELAY_CAUSES = (
    "carrier_delay_minutes", "weather_delay_minutes", "nas_delay_minutes",
    "security_delay_minutes", "late_aircraft_delay_minutes",
)
_DELAY_MEASURES = {
    "flights": "arr_flights", "delayed": "arr_del15", "cancelled": "arr_cancelled",
    "diverted": "arr_diverted", "arrival_delay_minutes": "arr_delay",
    "carrier_delay_minutes": "carrier_delay", "weather_delay_minutes": "weather_delay",
    "nas_delay_minutes": "nas_delay", "security_delay_minutes": "security_delay",
    "late_aircraft_delay_minutes": "late_aircraft_delay",
}
_T100_REQUIRED = {
    "ORIGIN", "ORIGIN_COUNTRY", "DEST", "DEST_COUNTRY", "YEAR", "CLASS", "DATA_SOURCE", "DISTANCE",
    *_T100_MEASURES.values(),
}
_DELAY_REQUIRED = {"year", "month", "airport", *_DELAY_MEASURES.values()}

_NPIAS_HEADERS = {
    "A": "State", "B": "City", "C": "Airport", "D": "LocID",
    "E": "Owner", "F": "Svc Lvl", "G": "Hub", "H": "Role",
    "I": "Enplaned", "J": "Based Aircraft", "K": "Development Estimate",
}
# Output field -> (source column, max length) for text, (source column, label) for counts.
_NPIAS_TEXT = {
    "state": ("A", 80), "city": ("B", 100), "name": ("C", 160), "ownership": ("E", 20),
    "service_level": ("F", 40), "hub_size": ("G", 40), "role": ("H", 80),
}
_NPIAS_COUNTS = {
    "enplanements": ("I", "NPIAS enplanements"), "based_aircraft": ("J", "NPIAS based aircraft"),
    "development_estimate": ("K", "NPIAS development estimate"),
}


def file_sha256(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(archive: zipfile.ZipFile, *, suffix: str, max_uncompressed: int,
                 preferred_name: str | None = None) -> zipfile.ZipInfo:
    candidates = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith(suffix)]
    if preferred_name:
        preferred = [item for item in candidates if Path(item.filename).name == preferred_name]
        if preferred:
            candidates = preferred
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one {suffix} data member, found {len(candidates)}")
    member = candidates[0]
    if member.file_size > max_uncompressed:
        raise ValueError(f"Archive member exceeds {max_uncompressed} uncompressed bytes")
    if member.filename.startswith(("/", "\\")) or ".." in Path(member.filename).parts:
        raise ValueError("Unsafe archive member path")
    return member


def _decimal(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value).strip() or "0")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid numeric value for {field}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"Invalid nonnegative value for {field}")
    return number


def _whole(value: object, field: str) -> int:
    number = _decimal(value, field)
    if number != number.to_integral_value():
        raise ValueError(f"Expected whole-number value for {field}")
    return int(number)


def _airport_code(value: object, field: str) -> str:
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalnum():
        raise ValueError(f"Invalid {field} airport code: {code!r}")
    return code


def _add_measures(totals: dict[str, Decimal], source_row: dict, measures: dict[str, str]) -> None:
    for name, column in measures.items():
        totals[name] += _decimal(source_row.get(column), column)


def _whole_totals(totals: dict[str, Decimal]) -> dict[str, int]:
    return {name: _whole(value, name) for name, value in totals.items()}


class RouteKey(NamedTuple):
    """The visible T-100 route dimensions; field order is the output sort order.

    TranStats exports retain hidden carrier/aircraft dimensions even when those
    columns are not selected, so multiple rows can share one key. They are
    intentionally summed rather than treated as duplicates.
    """

    origin: str
    destination: str
    year: int
    service_class: str
    data_source: str
    distance_miles: int

    @classmethod
    def from_source(cls, row: dict) -> RouteKey:
        """The only constructor used by the parser; validates every field."""
        key = cls(
            origin=_airport_code(row.get("ORIGIN"), "origin"),
            destination=_airport_code(row.get("DEST"), "destination"),
            year=_whole(row.get("YEAR"), "year"),
            service_class=str(row.get("CLASS", "")).strip().upper(),
            data_source=str(row.get("DATA_SOURCE", "")).strip().upper(),
            distance_miles=_whole(row.get("DISTANCE"), "distance"),
        )
        if key.year != _SOURCE_YEAR:
            raise ValueError(f"T-100 extract contains unexpected year {key.year}")
        if key.service_class not in {"F", "G", "L", "P"} or key.data_source not in {"DU", "IU"}:
            raise ValueError("Unexpected T-100 service class or data source")
        # Zero passes here: the parser drops it as same-airport activity.
        if key.distance_miles > 15_000:
            raise ValueError(f"Invalid T-100 distance: {key.distance_miles}")
        return key


class AirportMonth(NamedTuple):
    """BTS Delay Cause key: carrier rows are summed per airport-month."""

    code: str
    year: int
    month: int

    @classmethod
    def from_source(cls, row: dict) -> AirportMonth:
        key = cls(code=_airport_code(row.get("airport"), "delay"),
                  year=_whole(row.get("year"), "year"), month=_whole(row.get("month"), "month"))
        if key.year != _SOURCE_YEAR or not 1 <= key.month <= 12:
            raise ValueError("Delay extract contains an unexpected period")
        return key


def parse_t100_segments_zip(path: str | Path, *, enforce_coverage: bool = True) -> tuple[list[dict], dict]:
    """Aggregate a selected-field T-100 Segment export to route/class/source rows."""
    totals: dict[RouteKey, dict[str, Decimal]] = defaultdict(lambda: dict.fromkeys(_T100_MEASURES, Decimal(0)))
    raw_rows = 0
    excluded_zero_distance = 0
    with zipfile.ZipFile(path) as archive:
        member = _safe_member(archive, suffix=".csv", max_uncompressed=120_000_000)
        with archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text)
            if not _T100_REQUIRED.issubset(reader.fieldnames or []):
                raise ValueError("Unexpected T-100 Segment export schema")
            for source_row in reader:
                raw_rows += 1
                if str(source_row.get("ORIGIN_COUNTRY", "")).strip().upper() != "US":
                    continue
                key = RouteKey.from_source(source_row)
                # Zero-distance rows are overwhelmingly same-airport local
                # activity (plus an occasional unusable distance record), so
                # they cannot belong to a route-distance denominator.
                if key.distance_miles == 0:
                    excluded_zero_distance += 1
                    continue
                _add_measures(totals[key], source_row, _T100_MEASURES)

    rows = [{**key._asdict(), **_whole_totals(values)} for key, values in sorted(totals.items())]
    origins = {row["origin"] for row in rows}
    if enforce_coverage and (raw_rows < 100_000 or len(origins) < 300 or len(rows) < 1_000):
        raise ValueError("T-100 Segment extract failed the expected national-coverage floor")
    return rows, {
        "raw_rows": raw_rows, "published_rows": len(rows), "covered_origins": len(origins),
        "service_classes": sorted({row["service_class"] for row in rows}),
        "excluded_zero_distance_rows": excluded_zero_distance,
    }


def parse_delay_causes_zip(path: str | Path, *, enforce_coverage: bool = True) -> tuple[list[dict], dict]:
    """Aggregate BTS Airline Delay Cause rows from carrier-airport-month to airport-month."""
    totals: dict[AirportMonth, dict[str, Decimal]] = defaultdict(lambda: dict.fromkeys(_DELAY_MEASURES, Decimal(0)))
    raw_rows = 0
    with zipfile.ZipFile(path) as archive:
        member = _safe_member(
            archive, suffix=".csv", max_uncompressed=30_000_000,
            preferred_name="Airline_Delay_Cause.csv",
        )
        with archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text)
            if not _DELAY_REQUIRED.issubset(reader.fieldnames or []):
                raise ValueError("Unexpected BTS Airline Delay Cause export schema")
            for source_row in reader:
                raw_rows += 1
                _add_measures(totals[AirportMonth.from_source(source_row)], source_row, _DELAY_MEASURES)

    rows = []
    non_reconciling_months = 0
    unattributed_delay_minutes = 0
    for key, values in sorted(totals.items()):
        row = {**key._asdict(), **_whole_totals(values)}
        if row["delayed"] + row["cancelled"] + row["diverted"] > row["flights"]:
            raise ValueError(f"Delay status counts exceed flights for {key.code}/{key.year}-{key.month:02d}")
        difference = row["arrival_delay_minutes"] - sum(row[cause] for cause in _DELAY_CAUSES)
        if abs(difference) > max(5, row["arrival_delay_minutes"] * 0.001):
            # The official summary can contain unattributed minutes for a
            # small number of airport-months. Preserve the observed totals,
            # report the gap in the manifest, and compute cause shares only
            # over the attributed cause-minute denominator.
            non_reconciling_months += 1
        unattributed_delay_minutes += max(0, difference)
        rows.append(row)
    airports = {row["code"] for row in rows}
    months = {row["month"] for row in rows}
    if enforce_coverage and (raw_rows < 10_000 or len(airports) < 200 or months != set(range(1, 13))):
        raise ValueError("BTS Delay Cause extract failed the expected annual national-coverage floor")
    return rows, {
        "raw_rows": raw_rows, "published_rows": len(rows), "covered_airports": len(airports),
        "months": sorted(months), "non_reconciling_airport_months": non_reconciling_months,
        "unattributed_delay_minutes": unattributed_delay_minutes,
    }


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings = []
    with archive.open("xl/sharedStrings.xml") as source:
        for event, elem in ET.iterparse(source, events=("end",)):
            if elem.tag != _XLSX_NS + "si":
                continue
            strings.append("".join(node.text or "" for node in elem.iter(_XLSX_NS + "t")))
            elem.clear()
    return strings


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_XLSX_NS + "t"))
    value = cell.find(_XLSX_NS + "v")
    if value is None or value.text is None:
        return ""
    if cell.get("t") == "s":
        index = int(value.text)
        if not 0 <= index < len(shared):
            raise ValueError("NPIAS workbook contains an invalid shared-string index")
        return shared[index]
    return value.text


def _xlsx_rows(archive: zipfile.ZipFile):
    member = archive.getinfo("xl/worksheets/sheet1.xml")
    if member.file_size > 20_000_000:
        raise ValueError("NPIAS worksheet exceeds the uncompressed size bound")
    shared = _shared_strings(archive)
    with archive.open(member) as source:
        for event, elem in ET.iterparse(source, events=("end",)):
            if elem.tag != _XLSX_NS + "row":
                continue
            row = {}
            for cell in elem.iter(_XLSX_NS + "c"):
                ref = cell.get("r") or ""
                column = "".join(char for char in ref if char.isalpha())
                if column:
                    row[column] = _cell_value(cell, shared).strip()
            elem.clear()
            yield row


def _normalized_header(value: str) -> str:
    return " ".join(value.replace("\n", " ").replace("-", " ").split()).lower()


def parse_npias_xlsx(path: str | Path, *, enforce_coverage: bool = True) -> tuple[list[dict], dict]:
    """Read the official FAA NPIAS Appendix A 'All NPIAS Airports' sheet."""
    rows = []
    seen: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        iterator = _xlsx_rows(archive)
        try:
            header = next(iterator)
        except StopIteration as exc:
            raise ValueError("Empty NPIAS workbook") from exc
        for column, fragment in _NPIAS_HEADERS.items():
            if fragment.lower() not in _normalized_header(header.get(column, "")):
                raise ValueError(f"Unexpected NPIAS header in column {column}")
        for source_row in iterator:
            locid = source_row.get("D", "").strip().upper()
            if not locid or locid.startswith("+"):
                continue
            if not 3 <= len(locid) <= 5 or not locid.isalnum():
                continue
            if locid in seen:
                raise ValueError(f"Duplicate NPIAS LocID: {locid}")
            seen.add(locid)
            rows.append({
                "locid": locid,
                **{name: source_row.get(column, "")[:limit] for name, (column, limit) in _NPIAS_TEXT.items()},
                **{name: _whole(source_row.get(column, 0), label)
                   for name, (column, label) in _NPIAS_COUNTS.items()},
            })
    if enforce_coverage and len(rows) < 2_000:
        raise ValueError("NPIAS extract failed the expected national-coverage floor")
    return sorted(rows, key=lambda row: row["locid"]), {
        "published_rows": len(rows), "covered_airports": len(rows),
        "positive_development_estimates": sum(row["development_estimate"] > 0 for row in rows),
    }
