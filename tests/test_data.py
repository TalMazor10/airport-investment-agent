"""
Validation suite for data/airports.db.

Each test asserts one property the database must hold. The tests fall into
five groups: structure, internal consistency, agreement between independent
sources, known answers checked against the published sources, and coverage
figures that are reported without being asserted.

    pytest -v          run every check
    pytest -v -s       also print the coverage figures
"""

import sqlite3
from pathlib import Path

import pytest
import requests

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "airports.db"
T100_URL = "https://data.transportation.gov/resource/r495-tyji.json"

FIRST_YEAR, LAST_YEAR = 2016, 2025
LAST_POPULATION_YEAR = 2024
MONTHS_IN_WINDOW = (LAST_YEAR - FIRST_YEAR + 1) * 12

# Airports named in the brief, plus Boston as a known constrained airport.
NAMED_AIRPORTS = ["LAX", "SNA", "SFO", "ANC", "BOS"]

# Airport-years below this many departures are left out of the per-airport
# reconciliation checks, where small counts make percentages unstable.
MIN_DEPARTURES = 1000

# AIRCRAFT_CONFIG codes as published by BTS.
PASSENGER_AIRCRAFT, FREIGHTER = 1, 2


@pytest.fixture(scope="module")
def db():
    """A read-only connection to the built database, shared by every test."""
    if not DB_PATH.exists():
        pytest.fail(f"database not found at {DB_PATH}; run prepare_data.py first")
    con = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    yield con
    con.close()


def scalar(db, sql, *args):
    """Run a query that returns a single value."""
    return db.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "airport_month", "arrival_delay_month", "peak_hour", "haul_month",
    "t100_month", "metro_population", "airports",
}
# Tables from earlier schema versions. A rebuild must not leave them behind.
RETIRED_TABLES = {"international_month", "month_weight"}


def test_expected_tables_exist_and_retired_tables_are_absent(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert EXPECTED_TABLES <= tables
    assert not tables & RETIRED_TABLES


@pytest.mark.parametrize("table", ["airport_month", "arrival_delay_month", "haul_month", "t100_month"])
def test_monthly_tables_cover_exactly_the_build_window(db, table):
    first, last = db.execute(
        f"SELECT MIN(year * 100 + month), MAX(year * 100 + month) FROM {table}").fetchone()
    assert first == FIRST_YEAR * 100 + 1
    assert last == LAST_YEAR * 100 + 12


def test_peak_hour_covers_exactly_the_build_window(db):
    assert db.execute("SELECT MIN(year), MAX(year) FROM peak_hour").fetchone() == (FIRST_YEAR, LAST_YEAR)


def test_population_covers_the_build_window_up_to_the_last_census_year(db):
    assert db.execute("SELECT MIN(year), MAX(year) FROM metro_population").fetchone() \
        == (FIRST_YEAR, LAST_POPULATION_YEAR)


@pytest.mark.parametrize("table", ["airport_month", "haul_month", "t100_month"])
@pytest.mark.parametrize("airport", NAMED_AIRPORTS)
def test_named_airports_have_every_month(db, airport, table):
    months = scalar(db, f"SELECT COUNT(DISTINCT year * 100 + month) FROM {table} WHERE airport = ?",
                    airport)
    assert months == MONTHS_IN_WINDOW


# ---------------------------------------------------------------------------
# Internal consistency
# ---------------------------------------------------------------------------

def test_haul_bands_sum_to_departures(db):
    mismatched = scalar(db, "SELECT COUNT(*) FROM haul_month "
                            "WHERE long_haul + medium_haul + short_haul != departures")
    assert mismatched == 0


NON_NEGATIVE = [
    ("airport_month", c) for c in (
        "flights", "cancelled", "diverted", "dep_del15", "dep_delay_min", "arr_delay_min",
        "carrier_delay", "weather_delay", "nas_delay", "security_delay", "late_aircraft_delay")
] + [
    ("haul_month", c) for c in ("departures", "long_haul", "medium_haul", "short_haul")
] + [
    ("t100_month", c) for c in ("departures", "passengers", "seats")
]


@pytest.mark.parametrize("table, column", NON_NEGATIVE)
def test_counts_are_never_negative(db, table, column):
    assert scalar(db, f"SELECT COUNT(*) FROM {table} WHERE {column} < 0") == 0


def test_cancellations_never_exceed_flights(db):
    assert scalar(db, "SELECT COUNT(*) FROM airport_month WHERE cancelled > flights") == 0


def test_every_airport_row_has_a_metro_market_and_a_state(db):
    missing = scalar(db, "SELECT COUNT(*) FROM airport_month "
                         "WHERE COALESCE(city_market_id, '') = '' OR COALESCE(state, '') = ''")
    assert missing == 0


# ---------------------------------------------------------------------------
# Agreement between independent sources
# ---------------------------------------------------------------------------

def test_route_totals_match_airport_totals_nationally(db):
    """T-100 Segment route rows, summed, agree with the T-100 airport summary
    within 0.1 percent in every year."""
    rows = db.execute("""
        SELECT h.year, h.deps, t.deps
        FROM (SELECT year, SUM(departures) AS deps FROM haul_month GROUP BY year) h
        JOIN (SELECT year, SUM(departures) AS deps FROM t100_month GROUP BY year) t
          ON h.year = t.year
    """).fetchall()
    assert len(rows) == LAST_YEAR - FIRST_YEAR + 1
    off = [(year, route, summary) for year, route, summary in rows
           if abs(route - summary) > 0.001 * summary]
    assert off == []


def test_route_totals_match_airport_totals_at_every_significant_airport(db):
    """The same agreement, within 1 percent, for every airport-year with at
    least MIN_DEPARTURES departures."""
    rows = db.execute("""
        SELECT t.year, t.airport, COALESCE(h.deps, 0), t.deps
        FROM (SELECT year, airport, SUM(departures) AS deps
              FROM t100_month GROUP BY year, airport) t
        LEFT JOIN (SELECT year, airport, SUM(departures) AS deps
                   FROM haul_month GROUP BY year, airport) h
          ON h.year = t.year AND h.airport = t.airport
        WHERE t.deps >= ?
    """, (MIN_DEPARTURES,)).fetchall()
    off = [(year, airport, route, summary) for year, airport, route, summary in rows
           if abs(route - summary) > 0.01 * summary]
    assert off == []


def test_stored_t100_rows_equal_the_published_count(db):
    """Guards against silent row loss when paging through the BTS API.

    Requires network access and is skipped without it. Also fails if BTS has
    revised the published data since the database was built.
    """
    try:
        r = requests.get(T100_URL, params={
            "$select": "count(*)",
            # year is a text column in this dataset, so it is compared as text
            "$where": f"year between '{FIRST_YEAR}' and '{LAST_YEAR}'",
        }, timeout=30)
        r.raise_for_status()
    except requests.RequestException:
        pytest.skip("BTS open data API unreachable")
    published = int(r.json()[0]["count"])
    assert scalar(db, "SELECT COUNT(*) FROM t100_month") == published


def test_on_time_never_reports_more_departed_flights_than_t100(db):
    """On-Time covers a subset of carriers, so the flights it records as
    departed (scheduled minus cancelled) cannot exceed the T-100 departure
    count for any significant airport-year."""
    rows = db.execute("""
        SELECT o.year, o.airport, o.departed, t.deps
        FROM (SELECT year, airport, SUM(flights - cancelled) AS departed
              FROM airport_month GROUP BY year, airport) o
        JOIN (SELECT year, airport, SUM(departures) AS deps
              FROM t100_month GROUP BY year, airport) t
          ON o.year = t.year AND o.airport = t.airport
        WHERE t.deps >= ?
    """, (MIN_DEPARTURES,)).fetchall()
    over = [r for r in rows if r[2] > r[3]]
    assert over == []


# ---------------------------------------------------------------------------
# Known answers
# ---------------------------------------------------------------------------

# 2025 passengers, verified against the live BTS API on 2026-09-22.
KNOWN_2025_PASSENGERS = {
    "LAX": 36_766_912,
    "SFO": 26_482_764,
    "SNA": 5_532_646,
    "ANC": 2_714_359,
}


@pytest.mark.parametrize("airport, passengers", KNOWN_2025_PASSENGERS.items())
def test_2025_passengers_match_the_published_figures(db, airport, passengers):
    stored = scalar(db, "SELECT SUM(passengers) FROM t100_month WHERE airport = ? AND year = 2025",
                    airport)
    assert stored == passengers


def test_honolulu_matches_its_census_metropolitan_area(db):
    """OurAirports names the city "Honolulu, Oahu"; the qualifier after the
    comma must not prevent the Census match."""
    cbsa = scalar(db, """
        SELECT DISTINCT p.cbsa_name FROM metro_population p
        JOIN airport_month a ON a.city_market_id = p.city_market_id
        WHERE a.airport = 'HNL'
    """)
    assert cbsa == "Urban Honolulu, HI"


def test_2020_shows_the_pandemic_collapse(db):
    """Domestic flights fell 36.8 percent from 2019 to 2020."""
    y2019 = scalar(db, "SELECT SUM(flights) FROM airport_month WHERE year = 2019")
    y2020 = scalar(db, "SELECT SUM(flights) FROM airport_month WHERE year = 2020")
    assert 1 - y2020 / y2019 == pytest.approx(0.368, abs=0.001)


def test_lax_loses_share_of_its_metro_market(db):
    """LAX carried 68.9 percent of Los Angeles metropolitan-area flights in 2016 and 61.6
    percent in 2025: the spillover fingerprint."""
    def lax_share(year):
        lax, metro = db.execute("""
            SELECT SUM(CASE WHEN airport = 'LAX' THEN flights ELSE 0 END), SUM(flights)
            FROM airport_month
            WHERE year = ? AND city_market_id =
                (SELECT city_market_id FROM airport_month WHERE airport = 'LAX' LIMIT 1)
        """, (year,)).fetchone()
        return 100 * lax / metro

    assert lax_share(2016) == pytest.approx(68.9, abs=0.1)
    assert lax_share(2025) == pytest.approx(61.6, abs=0.1)


def test_anchorage_2025_long_haul_split(db):
    """Anchorage is a long-haul cargo hub and a short-haul passenger airport."""
    def long_haul_share(*configs):
        where = f"AND aircraft_config IN ({','.join('?' * len(configs))})" if configs else ""
        long_haul, deps = db.execute(
            f"SELECT SUM(long_haul), SUM(departures) FROM haul_month "
            f"WHERE airport = 'ANC' AND year = 2025 {where}", configs).fetchone()
        return 100 * long_haul / deps

    assert long_haul_share() == pytest.approx(41.7, abs=0.05)
    assert long_haul_share(PASSENGER_AIRCRAFT) == pytest.approx(8.2, abs=0.05)
    assert long_haul_share(FREIGHTER) == pytest.approx(68.3, abs=0.05)


# ---------------------------------------------------------------------------
# Coverage figures, reported and not asserted
# ---------------------------------------------------------------------------

def test_report_census_match_coverage(db):
    matched = scalar(db, "SELECT COUNT(DISTINCT city_market_id) FROM metro_population")
    markets = scalar(db, "SELECT COUNT(DISTINCT city_market_id) FROM airport_month")
    share = scalar(db, """
        SELECT 100.0 * SUM(CASE WHEN city_market_id IN
                   (SELECT city_market_id FROM metro_population) THEN flights ELSE 0 END)
               / SUM(flights)
        FROM airport_month
    """)
    print(f"\nCensus match: {matched} of {markets} airport markets, {share:.1f}% of flights")


def test_report_on_time_coverage_of_t100(db):
    print("\nOn-Time departed flights as a share of T-100 departures, 2025:")
    for airport in ["LAX", "SFO", "SNA", "ANC"]:
        departed = scalar(db, "SELECT SUM(flights - cancelled) FROM airport_month "
                              "WHERE airport = ? AND year = 2025", airport)
        total = scalar(db, "SELECT SUM(departures) FROM t100_month "
                           "WHERE airport = ? AND year = 2025", airport)
        print(f"  {airport}  {100 * departed / total:5.1f}%")


# Metropolitan areas Census created, re-coded or retired in its 2023 definitions.
# Their population series cannot span 2016 to 2024 under one code.
REDEFINED_IN_2023 = {
    "Cleveland, OH", "Bozeman, MT", "Helena, MT", "Minot, ND", "Paducah, KY-IL",
    "Traverse City, MI", "New Bern, NC",
}


def test_every_matched_metro_has_the_full_population_series(db):
    """Guards against the renamed-area defect: joining the two Census files by
    name split each renamed area into two halves. Only areas Census actually
    redefined may lack years."""
    rows = db.execute("SELECT cbsa_name, COUNT(DISTINCT year) FROM metro_population "
                      "GROUP BY city_market_id").fetchall()
    partial = {name for name, years in rows if years != LAST_POPULATION_YEAR - FIRST_YEAR + 1}
    assert partial <= REDEFINED_IN_2023
