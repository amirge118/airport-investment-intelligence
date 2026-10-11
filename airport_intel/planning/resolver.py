"""US-only airport resolution: codes, names, cities, states, and regions.

The state/region crosswalk below is a versioned lookup table, not a
prompt-specific exception list. City -> airport selection follows a fixed
policy (commercial scheduled service, then observed 2025 passenger
throughput) and always returns alternatives for disambiguation instead of
silently picking one airport. Foreign-place detection is data-driven from
the bundled global OurAirports registry (code and name/city lookups
against real non-US rows), not a hand-maintained name list: several city
names (Paris, London, Rome, Athens, Vienna, Cairo, Dublin, Berlin ...) are
also real, if minor, US towns, so a hardcoded denylist would wrongly
refuse a legitimate small-airport match.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..facts import FactBundle

STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}
VALID_STATE_ABBRS = set(STATE_NAME_TO_ABBR.values())

# A small set of commonly referenced multi-state US regions. Versioned here;
# extend deliberately rather than special-casing a single prompt.
REGIONS: dict[str, set[str]] = {
    "new england": {"CT", "ME", "MA", "NH", "RI", "VT"},
    "mid-atlantic": {"NY", "NJ", "PA", "DE", "MD", "DC"},
    "pacific northwest": {"WA", "OR"},
    "southwest": {"AZ", "NM", "TX", "OK"},
    "midwest": {"OH", "IN", "IL", "MI", "WI", "MN", "IA", "MO", "ND", "SD", "NE", "KS"},
    "southeast": {"VA", "WV", "NC", "SC", "GA", "FL", "KY", "TN", "AL", "MS", "AR", "LA"},
    "mountain west": {"MT", "ID", "WY", "CO", "UT", "NV"},
}

# Cosmetic only (message wording), never used to decide resolution. Falls
# back to the raw ISO-3166 alpha-2 code for anything not listed here.
COUNTRY_NAMES = {
    "GB": "United Kingdom", "FR": "France", "JP": "Japan", "CA": "Canada", "DE": "Germany",
    "AE": "UAE", "CN": "China", "AU": "Australia", "SG": "Singapore", "NL": "Netherlands",
    "ES": "Spain", "IT": "Italy", "MX": "Mexico", "IE": "Ireland", "CH": "Switzerland",
    "KR": "South Korea", "IN": "India", "BR": "Brazil",
}


def _country_label(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


_AIRPORT_SUFFIX_RE = re.compile(r"\s+(international\s+)?(airport|airfield)$", re.I)


class Registry:
    """US-filtered airport identity/geography plus annual BTS aggregates.

    Also keeps a small global (non-US-filtered) code/name index purely to
    recognize an explicitly foreign identifier from real data, never to
    answer anything about non-US airports.
    """

    def __init__(self, catalogue: FactBundle):
        self.catalogue = catalogue
        self._by_code: dict[str, dict] = {}
        self._by_icao: dict[str, dict] = {}
        self._global_by_code: dict[str, dict] = {}
        self._global_by_icao: dict[str, dict] = {}
        self._global_rows: list[dict] = []

        for row in catalogue.get("airport_registry", []):
            country = row.get("country", "")
            code = row.get("code", "")
            icao = row.get("icao", "")
            global_row = {"code": code, "icao": icao, "name": row.get("name", code),
                          "city": row.get("city", ""), "country": country}
            self._global_rows.append(global_row)
            if code:
                self._global_by_code[code] = global_row
            if icao:
                self._global_by_icao[icao] = global_row

            if country != "US":
                continue
            region = row.get("region", "")
            state = region.split("-")[1] if region.startswith("US-") and "-" in region else ""
            us_row = {
                "code": code, "icao": icao, "name": row.get("name", code),
                "city": row.get("city", ""), "state": state,
                "scheduled": bool(row.get("scheduled")), "country": "US",
            }
            if code:
                self._by_code[code] = us_row
            if icao:
                self._by_icao[icao] = us_row

        self._passengers_2025: dict[str, int] = {}
        for row in catalogue.get("airport_years", []):
            if row["year"] == 2025:
                self._passengers_2025[row["code"]] = row.get("passengers", 0)
                # BTS-reported airports not already in the geography registry
                # (rare schema gaps) still resolve by exact code.
                self._by_code.setdefault(row["code"], {
                    "code": row["code"], "icao": "", "name": row.get("name", row["code"]),
                    "city": row.get("city", ""), "state": "", "scheduled": True, "country": "US",
                })

    def us_codes(self) -> set[str]:
        return set(self._by_code)

    def get(self, code: str) -> dict | None:
        return self._by_code.get(code.upper())

    def by_state(self, abbr: str) -> list[dict]:
        return sorted(
            (row for row in self._by_code.values() if row["state"] == abbr),
            key=lambda r: -self._passengers_2025.get(r["code"], 0),
        )

    def by_region(self, region_key: str) -> list[dict]:
        states = REGIONS.get(region_key, set())
        return sorted(
            (row for row in self._by_code.values() if row["state"] in states),
            key=lambda r: -self._passengers_2025.get(r["code"], 0),
        )

    def by_city(self, city: str) -> list[dict]:
        """Candidates for a city name, ranked by policy (scheduled service, then 2025 throughput)."""
        needle = city.strip().lower()
        matches = [row for row in self._by_code.values() if row["city"].strip().lower() == needle]
        return sorted(
            matches,
            key=lambda r: (not r["scheduled"], -self._passengers_2025.get(r["code"], 0)),
        )

    def _foreign_matches(self, text: str) -> list[dict]:
        """Real non-US registry rows whose city or name matches `text`."""
        needle = text.strip().lower()
        if not needle:
            return []
        word = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
        return [
            row for row in self._global_rows
            if row["country"] and row["country"] != "US"
            and (row["city"].strip().lower() == needle or word.search(row["name"].lower()))
        ]

    def resolve_token(self, token: str) -> dict:
        """Resolve a token; "<place> airport" falls back to "<place>".

        Planners often pass the user's wording ("Anchorage airport"). The generic
        suffix is dropped only when the whole phrase matched nothing, or matched
        just a foreign airport while the place is a US city with scheduled
        service. An exact US airport-name match still wins; no candidate is
        ever invented.
        """
        decision = self._resolve_token(token)
        stripped = _AIRPORT_SUFFIX_RE.sub("", token.strip())
        if not stripped or stripped == token.strip():
            return decision
        fallback = self._resolve_token(stripped)
        if decision["kind"] == "ambiguous" and not decision.get("candidates"):
            if fallback["kind"] != "ambiguous" or fallback.get("candidates"):
                return fallback
        # "Santa Ana airport" also names a foreign airport; as for bare city
        # names, a US place with scheduled service wins over a foreign homonym.
        # If several US airports match, the fallback is a clarification, never a pick.
        if (decision["kind"] == "out_of_scope" and fallback["kind"] in {"airport", "ambiguous"}
                and any(row.get("scheduled") for row in fallback.get("candidates") or [])):
            return fallback
        return decision

    def _resolve_token(self, token: str) -> dict:
        """Resolve a single free-text token to a resolver decision.

        Returns one of:
          {"kind": "airport", "candidates": [...]}       exactly one or more US matches
          {"kind": "state", "abbr": "TX"}                 state-wide scope
          {"kind": "region", "key": "new england"}        region-wide scope
          {"kind": "ambiguous", "query": token, ...}       no confident match, or a
                                                            low-confidence US match that
                                                            shares a name with a real
                                                            non-US place (never guessed)
          {"kind": "out_of_scope", "place": token, "country": "..."}  explicitly non-US,
                                                            identified from real registry data
        """
        raw = token.strip()
        upper = raw.upper()
        lower = raw.lower()

        if "," in raw:
            city_part, _, state_part = raw.partition(",")
            state_part = state_part.strip().lower()
            abbr = STATE_NAME_TO_ABBR.get(state_part) or (state_part.upper() if state_part.upper() in VALID_STATE_ABBRS else None)
            if abbr:
                city_matches = [row for row in self.by_city(city_part.strip()) if row["state"] == abbr]
                if city_matches:
                    return {"kind": "airport", "candidates": city_matches}
                return {"kind": "ambiguous", "query": raw}

        if len(upper) == 4 and upper.isalpha() and upper in self._by_icao:
            return {"kind": "airport", "candidates": [self._by_icao[upper]]}

        if len(upper) == 3 and upper.isalpha() and upper in self._by_code:
            return {"kind": "airport", "candidates": [self._by_code[upper]]}

        if lower in STATE_NAME_TO_ABBR:
            return {"kind": "state", "abbr": STATE_NAME_TO_ABBR[lower]}
        if upper in VALID_STATE_ABBRS:
            return {"kind": "state", "abbr": upper}

        if lower in REGIONS:
            return {"kind": "region", "key": lower}

        city_matches = self.by_city(raw)
        if city_matches:
            states = {row["state"] for row in city_matches if row["state"]}
            if len(states) > 1:
                # Same city name in different states (Portland, OR vs Portland, ME):
                # a genuine ambiguity, never silently resolved to one state.
                return {"kind": "ambiguous", "query": raw, "candidates": city_matches}
            has_scheduled_service = any(row["scheduled"] for row in city_matches)
            foreign = [] if has_scheduled_service else self._foreign_matches(raw)
            if foreign:
                # A real, confidently-scheduled US match wins outright; but a
                # match with no scheduled service (e.g. Paris, TX) that shares
                # its name with a real foreign place is not confident enough
                # to silently resolve -- ask, rather than guess either way.
                return {"kind": "ambiguous", "query": raw, "candidates": city_matches,
                        "reason": "low_confidence_foreign_homonym",
                        "foreign_country": _country_label(foreign[0]["country"])}
            return {"kind": "airport", "candidates": city_matches}

        # Whole-word match only: a bare substring would resolve "Rank" to
        # "Frank Wiley Field" and silently answer the wrong geography.
        word = re.compile(rf"(?<!\w){re.escape(lower)}(?!\w)")
        name_matches = [row for row in self._by_code.values() if word.search(row["name"].lower())]
        if len(name_matches) == 1:
            return {"kind": "airport", "candidates": name_matches}
        if name_matches:
            return {"kind": "ambiguous", "query": raw, "candidates": name_matches[:5]}

        if (len(upper) in (3, 4)) and upper.isalpha():
            global_row = self._global_by_code.get(upper) or self._global_by_icao.get(upper)
            if global_row and global_row["country"] and global_row["country"] != "US":
                return {"kind": "out_of_scope", "place": raw, "country": _country_label(global_row["country"])}

        foreign = self._foreign_matches(raw)
        if foreign:
            return {"kind": "out_of_scope", "place": raw, "country": _country_label(foreign[0]["country"])}

        return {"kind": "ambiguous", "query": raw}

    def classify_entities(self, entities: list[str]) -> dict:
        """Resolve every plan entity and group the decisions by kind.

        Returns ``airports`` (first candidate of each confident match),
        ``scopes`` (state/region decisions), ``ambiguous`` and
        ``out_of_scope`` decisions; the caller decides how to combine them.
        """
        airports: list[dict] = []
        scopes: list[dict] = []
        ambiguous: list[dict] = []
        out_of_scope: list[dict] = []
        for entity in entities:
            decision = self.resolve_token(entity)
            if decision["kind"] == "airport":
                airports.append(decision["candidates"][0])
            elif decision["kind"] in ("state", "region"):
                scopes.append(decision)
            elif decision["kind"] == "out_of_scope":
                out_of_scope.append(decision)
            else:
                ambiguous.append(decision)
        return {"airports": airports, "scopes": scopes, "ambiguous": ambiguous, "out_of_scope": out_of_scope}


def load_snapshot(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
