"""
The four tools the agent can call.

Each tool is an ordinary function that reads data/airports.db and returns a
JSON-serialisable dict. TOOL_DEFINITIONS describes each tool to the language
model: its name, what it is for, and the inputs it accepts. The model chooses
which tools to call and with what inputs; the code in this file executes the
call and returns the result. No tool asks the model for a number.

    find_airports     which airports are in a region
    score_airports    which of a group is the strongest candidate (scoring.py)
    airport_profile   what is happening at one airport, as facts
    haul_mix          how far an airport's flights go

Tools are sized by kind of request rather than by piece of data: the profile
carries delays, cancellations, busiest hours and metropolitan neighbours
together, because they answer the same kind of question.
"""

import json
import sqlite3
from collections import defaultdict

import scoring
from prepare_data import LONG_HAUL_MILES, MEDIUM_HAUL_MILES

# BTS On-Time Performance identifies states by full name; OurAirports by code.
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
    "GU": "U.S. Pacific Trust Territories and Possessions",
    "MP": "U.S. Pacific Trust Territories and Possessions",
    "AS": "U.S. Pacific Trust Territories and Possessions",
}

# AIRCRAFT_CONFIG codes as published by BTS in T-100.
PASSENGER_AIRCRAFT, FREIGHTER = 1, 2


def _connect():
    return sqlite3.connect(scoring.DB_PATH.as_uri() + "?mode=ro", uri=True)


def _pct(x, digits=1):
    return None if x is None else round(100 * x, digits)


def _round(x, digits=1):
    return None if x is None else round(x, digits)


# ----------------------------------------------------------------------------
# find_airports
# ----------------------------------------------------------------------------

def find_airports(states, include_small=False):
    """Airports in the given states, largest first.

    states: two-letter codes, e.g. ["MA", "RI"]. Airports carrying fewer than
    scoring.MIN_PASSENGERS in the latest year are omitted unless include_small
    is set, and are counted in the result either way.
    """
    codes = [s.strip().upper() for s in states]
    unknown = [c for c in codes if c not in STATE_NAMES]
    names = {STATE_NAMES[c] for c in codes if c in STATE_NAMES}

    con = _connect()
    state_of = {}
    for a, state in con.execute("SELECT airport, state FROM airport_month ORDER BY year, month"):
        state_of[a] = state
    region_of = {code: region.split("-")[0] for code, region in
                 con.execute("SELECT code, region FROM airports")}
    passengers = dict(con.execute(
        "SELECT airport, SUM(passengers) FROM t100_month WHERE year = ? GROUP BY airport "
        "HAVING SUM(passengers) > 0", (scoring.LAST_YEAR,)))
    names_of = {c: (n, city) for c, n, city in con.execute("SELECT code, name, city FROM airports")}
    con.close()

    found, small = [], 0
    for a, pax in passengers.items():
        in_region = state_of.get(a) in names or (a not in state_of and region_of.get(a) in codes)
        if not in_region:
            continue
        if pax < scoring.MIN_PASSENGERS:
            small += 1
            if not include_small:
                continue
        name, city = names_of.get(a, ("", ""))
        found.append({
            "airport": a, "name": name, "city": city,
            "state": state_of.get(a) or region_of.get(a, ""),
            f"passengers_{scoring.LAST_YEAR}": int(pax),
            "ranked": pax >= scoring.MIN_PASSENGERS,
        })
    found.sort(key=lambda r: -r[f"passengers_{scoring.LAST_YEAR}"])
    result = {
        "states_searched": sorted(codes),
        "airports": found,
        "airports_below_ranking_threshold": small,
        "ranking_threshold": f"{scoring.MIN_PASSENGERS:,} passengers in {scoring.LAST_YEAR}",
    }
    if unknown:
        result["unrecognised_state_codes"] = unknown
    return result


# ----------------------------------------------------------------------------
# score_airports
# ----------------------------------------------------------------------------

def _reading(term, raw, context):
    """One sentence stating what a term's value means, direction included.

    Written by code so the model relays the direction rather than inferring it
    from a sign or a percentile. For spillover the percentile runs opposite to
    the raw value: losing share is the pressure signal and scores high.
    """
    if raw is None:
        return None
    if term == "growth_gap":
        pax, pop = context["passenger_growth"], context["population_growth"]
        rates = f"(passengers {pax * 100:+.2f}% a year, metropolitan population {pop * 100:+.2f}% a year)"
        if raw > 0:
            return (f"Population of its metropolitan area grew {raw:.2f} percentage points a year "
                    f"faster than its passengers {rates}.")
        return (f"Passengers grew {-raw:.2f} percentage points a year faster than the population "
                f"of its metropolitan area {rates}.")
    if term == "spillover":
        if raw < 0:
            return f"Losing {-raw:.2f} percentage points of its metropolitan area's passenger share a year."
        if raw > 0:
            return f"Gaining {raw:.2f} percentage points of its metropolitan area's passenger share a year."
        return "Holding a steady share of its metropolitan area's passengers."
    if term == "nas_delay":
        return f"{raw:.2f} minutes of NAS delay per arriving flight, 2023 to 2025."
    return f"{int(raw):,} passengers in {scoring.LAST_YEAR}."


def _sensitivity_summary(s):
    top = ", ".join(s["top_in_rank_order"])
    if s["stable"]:
        return (f"Stable: moving any single weight by {s['step']} leaves {s['first']} first "
                f"and the top positions unchanged ({top}, in rank order).")
    shifts = "; ".join(f"with the {c['term']} weight at {c['weight']}, the order becomes "
                       f"{', '.join(c['top_in_rank_order'])}" for c in s["changes"])
    return f"Not stable. Current order {top}. {shifts}."


TERM_UNITS = {
    "growth_gap": "percentage points per year (metropolitan population growth minus passenger growth)",
    "spillover": "percentage points of metropolitan-area passenger share per year",
    "nas_delay": "NAS delay minutes per arriving flight, 2023 to 2025",
    "scale": f"passengers in {scoring.LAST_YEAR}",
}


def score_airports(airports):
    """National scores for a group of airports, ranked within the group, with
    the sensitivity of the group's top positions to the weights."""
    records = scoring.score(airports)
    out = []
    for r in records:
        if "error" in r:
            out.append(r)
            continue
        c = r["context"]
        item = {
            "airport": r["airport"], "name": r["name"], "city": r["city"], "state": r["state"],
            "ranked": r["ranked"],
            "context": {
                f"passengers_{scoring.LAST_YEAR}": int(c["passengers_latest_year"]),
                "passenger_growth_pct_per_year": _pct(c["passenger_growth"], 2),
                "flight_growth_pct_per_year": _pct(c["flight_growth"], 2),
                "metro_population_growth_pct_per_year": _pct(c["population_growth"], 2),
                "share_of_peak_year_pct": _pct(c["share_of_peak"]),
                "peak_year": c["peak_year"],
                "metro_area": c["metro_area"],
                "other_ranked_airports_in_metro": c["metro_airports"],
            },
            "notes": r["notes"],
        }
        if r["ranked"]:
            item.update({
                "score": _round(r["score"]),
                "rank_in_group": r["group_rank"], "group_size": r["group_size"],
                "national_rank": r["national_rank"], "national_pool": r["national_pool"],
            })
            item["terms"] = {
                t: {"reading": _reading(t, v["raw"], c),
                    "value": _round(v["raw"], 2) if t != "scale" else int(v["raw"]),
                    "unit": TERM_UNITS[t],
                    "percentile": None if v["percentile"] is None else round(v["percentile"]),
                    "effective_weight": round(v["weight"], 3)}
                for t, v in r["terms"].items()
            }
        out.append(item)

    ranked_codes = [r["airport"] for r in out if r.get("ranked")]
    result = {
        "method": {
            "weights": scoring.TERM_WEIGHTS,
            "percentiles_against": f"{out[0].get('national_pool', 'all')} US airports above "
                                   f"{scoring.MIN_PASSENGERS:,} passengers in {scoring.LAST_YEAR}"
                                   if ranked_codes else None,
            "score_range": "0 to 100, weighted mean of term percentiles",
            "percentile_direction": "every percentile is oriented so that higher means a "
                                    "stronger renovation case: metropolitan population growing "
                                    "faster than passengers, more metropolitan-area share lost, more NAS delay, "
                                    "more passengers",
            "reading": "each term's reading states its direction in words; the sign of the "
                       "value is not a guide on its own",
            "peak_gate": f"below {scoring.PEAK_GATE:.0%} of its peak-year passengers, growth gap "
                         "and spillover are neutral (50)",
        },
        "airports": out,
    }
    if len(ranked_codes) >= 2:
        s = scoring.sensitivity(ranked_codes)
        result["sensitivity"] = {**s, "summary": _sensitivity_summary(s)}
    return result


# ----------------------------------------------------------------------------
# airport_profile
# ----------------------------------------------------------------------------

def _nas_delay_comparison(airport):
    """An airport's NAS delay, 2023 to 2025, set against every ranked airport.

    Minutes alone do not show whether delay is high: 5.5 minutes can look modest
    beside an airport's own worse years while ranking near the top nationally.
    The national percentile and median make the comparison explicit.
    """
    table = scoring.national_table("arrival")
    rec = table.get(airport)
    if not rec or "nas_delay" not in rec["raw"]:
        return None
    ranked = sorted(r["raw"]["nas_delay"] for r in table.values()
                    if r["ranked"] and "nas_delay" in r["raw"])
    mid = len(ranked) // 2
    median = ranked[mid] if len(ranked) % 2 else (ranked[mid - 1] + ranked[mid]) / 2
    pct = rec.get("percentile", {}).get("nas_delay") if rec["ranked"] else None
    return {
        "minutes_per_arrival_2023_2025": _round(rec["raw"]["nas_delay"], 2),
        "national_percentile": None if pct is None else round(pct),
        "national_median_minutes": _round(median, 2),
        "reading": (f"More NAS delay than {round(pct)}% of US airports above "
                    f"{scoring.MIN_PASSENGERS:,} passengers." if pct is not None
                    else "Not ranked: below the passenger threshold."),
    }


def airport_profile(airport):
    """Facts about one airport, year by year, with no scoring."""
    a = airport.strip().upper()
    d = scoring._load()
    if a not in d["passengers"]:
        return {"airport": a, "error": "no traffic data for this airport code"}

    annual_pax, annual_deps = defaultdict(float), defaultdict(float)
    for y, _, p in d["passengers"][a]:
        annual_pax[y] += p
    for y, _, n in d["departures"][a]:
        annual_deps[y] += n

    mkt = d["market"].get(a)
    members = [x for x, m in d["market"].items() if m == mkt and x in d["passengers"]] if mkt else [a]
    metro_pax = defaultdict(float)
    for x in members:
        for y, _, p in d["passengers"][x]:
            metro_pax[y] += p

    con = _connect()
    ontime = {y: (f, c) for y, f, c in con.execute(
        "SELECT year, SUM(flights), SUM(cancelled) FROM airport_month WHERE airport = ? "
        "GROUP BY year", (a,))}
    nas = {y: (n, f) for y, n, f in con.execute(
        "SELECT year, SUM(nas_delay), SUM(flights) FROM arrival_delay_month WHERE airport = ? "
        "GROUP BY year", (a,))}
    causes = con.execute(
        "SELECT SUM(flights), SUM(carrier_delay), SUM(weather_delay), SUM(nas_delay), "
        "SUM(security_delay), SUM(late_aircraft_delay) FROM arrival_delay_month "
        "WHERE airport = ? AND year = ?", (a, scoring.LAST_YEAR)).fetchone()
    hours = con.execute(
        "SELECT dep_time_block, flights FROM peak_hour WHERE airport = ? AND year = ? "
        "ORDER BY flights DESC", (a, scoring.LAST_YEAR)).fetchall()
    name_row = con.execute("SELECT name, city FROM airports WHERE code = ?", (a,)).fetchone()
    con.close()

    yearly = []
    for y in range(scoring.FIRST_YEAR, scoring.LAST_YEAR + 1):
        flights, cancelled = ontime.get(y, (0, 0))
        nas_min, arrivals = nas.get(y, (0, 0))
        yearly.append({
            "year": y,
            "passengers": int(annual_pax.get(y, 0)),
            "departures": int(annual_deps.get(y, 0)),
            "passengers_per_departure": _round(annual_pax[y] / annual_deps[y]) if annual_deps.get(y) else None,
            "share_of_metro_passengers_pct": _pct(annual_pax[y] / metro_pax[y]) if metro_pax.get(y) else None,
            "cancellation_rate_pct": _pct(cancelled / flights, 2) if flights else None,
            "nas_delay_min_per_arrival": _round(nas_min / arrivals, 2) if arrivals else None,
        })

    peak_year = max(annual_pax, key=annual_pax.get)
    total_hours = sum(n for _, n in hours)
    arrivals_latest = causes[0] if causes else 0
    latest_pax = annual_pax[scoring.LAST_YEAR]
    latest_deps = annual_deps[scoring.LAST_YEAR]
    ontime_latest = ontime.get(scoring.LAST_YEAR, (0, 0))[0]

    return {
        "airport": a,
        "name": name_row[0] if name_row else "",
        "city": name_row[1] if name_row else "",
        "state": d["state"].get(a, ""),
        "metro_area": d["cbsa"].get(mkt),
        "metro_neighbours": sorted(
            ({"airport": x, f"passengers_{scoring.LAST_YEAR}": int(sum(
                p for y, _, p in d["passengers"][x] if y == scoring.LAST_YEAR))}
             for x in members if x != a),
            key=lambda r: -r[f"passengers_{scoring.LAST_YEAR}"]),
        "peak_year": peak_year,
        "share_of_peak_year_pct": _pct(latest_pax / annual_pax[peak_year]) if annual_pax[peak_year] else None,
        "metro_population": {y: p for y, p in sorted(d["population"].get(mkt, []))} or None,
        "yearly": yearly,
        f"delay_minutes_per_arrival_by_cause_{scoring.LAST_YEAR}": {
            cause: _round(total / arrivals_latest, 2)
            for cause, total in zip(("carrier", "weather", "nas", "security", "late_aircraft"),
                                    causes[1:])
        } if arrivals_latest else None,
        "nas_delay_national_comparison": _nas_delay_comparison(a),
        f"busiest_departure_hours_{scoring.LAST_YEAR}": [
            {"hour": blk, "share_of_daily_departures_pct": _pct(n / total_hours)}
            for blk, n in hours[:3]
        ] if total_hours else None,
        "coverage_notes": {
            "passengers_and_departures": "T-100, every carrier",
            "cancellations_delays_hours": (
                "On-Time Performance, domestic flights by major carriers only: "
                f"{_pct(ontime_latest / latest_deps) if latest_deps else 'n/a'}% of this airport's "
                f"{scoring.LAST_YEAR} departures"),
            "population": "Census metropolitan estimates, 2016 to 2024" if mkt in d["population"]
                          else "no Census population match for this airport's metropolitan area",
        },
    }


# ----------------------------------------------------------------------------
# haul_mix
# ----------------------------------------------------------------------------

def haul_mix(airport, year=scoring.LAST_YEAR):
    """Departures by distance band, for all flights, passenger aircraft and cargo aircraft."""
    a = airport.strip().upper()
    year = int(year)
    con = _connect()
    rows = con.execute(
        "SELECT aircraft_config, SUM(departures), SUM(long_haul), SUM(medium_haul), SUM(short_haul) "
        "FROM haul_month WHERE airport = ? AND year = ? GROUP BY aircraft_config", (a, year)).fetchall()
    con.close()
    if not rows:
        return {"airport": a, "year": year, "error": "no route data for this airport and year"}

    total = sum(r[1] for r in rows)

    def band(selected):
        deps, long_, med, short = (sum(r[i] for r in selected) for i in range(1, 5))
        if not deps:
            return None
        return {"departures": deps,
                "share_of_all_departures_pct": _pct(deps / total),
                "long_haul_pct": _pct(long_ / deps), "medium_haul_pct": _pct(med / deps),
                "short_haul_pct": _pct(short / deps)}

    return {
        "airport": a,
        "year": year,
        "all_flights": band(rows),
        "passenger_aircraft": band([r for r in rows if r[0] == PASSENGER_AIRCRAFT]),
        "cargo_aircraft": band([r for r in rows if r[0] == FREIGHTER]),
        "other_aircraft": band([r for r in rows if r[0] not in (PASSENGER_AIRCRAFT, FREIGHTER)]),
        "definitions": {
            "long_haul": f"route distance of {LONG_HAUL_MILES:,} statute miles or more, "
                         "approximately six hours of flight",
            "medium_haul": f"{MEDIUM_HAUL_MILES:,} to {LONG_HAUL_MILES - 1:,} statute miles",
            "short_haul": f"under {MEDIUM_HAUL_MILES:,} statute miles",
            "other_aircraft": "combined passenger-and-freight aircraft and seaplanes",
            "source": "BTS T-100 Segment, every carrier, domestic and international",
            "why_distance": "flight time is not reported by foreign carriers, so distance is the "
                            "only measure available for every flight",
        },
    }


# ----------------------------------------------------------------------------
# Definitions for the model, and the dispatcher
# ----------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "find_airports",
        "description": (
            "List the airports in one or more US states, largest first, with their passengers in "
            f"{scoring.LAST_YEAR}. Use it to turn a region into airport codes before scoring or "
            "comparing them. Regions such as 'New England' or 'the Pacific Northwest' must be "
            "expanded into two-letter state codes first; the result echoes the states searched so "
            "the expansion is visible. By default only airports large enough to be ranked are "
            f"listed (at least {scoring.MIN_PASSENGERS:,} passengers); set include_small to list "
            "smaller ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "states": {"type": "array", "items": {"type": "string"},
                           "description": "Two-letter US state or territory codes, e.g. [\"MA\", \"RI\"]."},
                "include_small": {"type": "boolean",
                                  "description": "Also list airports below the ranking threshold."},
            },
            "required": ["states"],
        },
    },
    {
        "name": "score_airports",
        "description": (
            "Score and rank a group of airports as renovation candidates. This is the only source "
            "of scores and rankings; never estimate one. Each airport gets a national score from 0 "
            "to 100, its rank within the group and nationally, and four terms with raw values, "
            "percentiles and notes: growth gap (metropolitan population growing faster than passengers), "
            "spillover (losing passenger share to other airports in the same metropolitan area), NAS delay "
            "(traffic and airport-operations delay per arriving flight) and scale (passengers). "
            "Notes explain any term that is missing or neutral. For two or more ranked airports "
            "the result includes a sensitivity check: whether the top positions change when any "
            "weight moves by 0.05."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airports": {"type": "array", "items": {"type": "string"},
                             "description": "IATA airport codes, e.g. [\"BOS\", \"PVD\"]."},
            },
            "required": ["airports"],
        },
    },
    {
        "name": "airport_profile",
        "description": (
            "Facts about one airport, with no scoring: passengers, departures and passengers per "
            f"departure for each year {scoring.FIRST_YEAR} to {scoring.LAST_YEAR}; its share of its "
            "metropolitan area's passengers each year; its peak year and how close it now is to "
            "it; cancellation rate and NAS delay per arrival each year; delay minutes by cause; its "
            "busiest departure hours; the other airports in its metropolitan area; and that area's population. "
            "It also gives the airport's 2023 to 2025 NAS delay with its national percentile and "
            "the national median; judge whether delay is high from the percentile, not the minutes. "
            "Use it to explain why an airport looks congested or constrained, to compare "
            "congestion between airports, or to describe demand. Passengers per departure rising "
            "while departures stay flat indicates airlines using larger aircraft rather than adding "
            "flights, a sign of slot or runway limits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport": {"type": "string", "description": "IATA airport code, e.g. \"SFO\"."},
            },
            "required": ["airport"],
        },
    },
    {
        "name": "haul_mix",
        "description": (
            "Share of an airport's departures that are long, medium and short haul, for all "
            "flights, passenger aircraft only and cargo aircraft only, domestic and international, "
            f"every carrier. Long haul is a route of {LONG_HAUL_MILES:,} statute miles or more, about six hours. "
            "Use it for questions about long-haul flights or the kind of traffic an airport serves. "
            "Report the passenger and cargo aircraft split, not only the total: at cargo hubs they "
            "differ sharply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport": {"type": "string", "description": "IATA airport code, e.g. \"ANC\"."},
                "year": {"type": "integer",
                         "description": f"Year from {scoring.FIRST_YEAR} to {scoring.LAST_YEAR}; "
                                        f"defaults to {scoring.LAST_YEAR}."},
            },
            "required": ["airport"],
        },
    },
]

TOOLS = {
    "find_airports": find_airports,
    "score_airports": score_airports,
    "airport_profile": airport_profile,
    "haul_mix": haul_mix,
}


def run_tool(name, arguments):
    """Execute one tool call from the model and return its result.

    Errors are returned to the model as data rather than raised, so the agent
    can report or correct them.
    """
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        result = fn(**arguments)
        json.dumps(result)
        return result
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
