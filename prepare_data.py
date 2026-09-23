"""
Build the airport analytics database from public US aviation data.

This is an offline build step, not a runtime dependency. It produces
data/airports.db, which is committed to the repository; the application reads
that database directly and performs no downloads while serving queries.

Sources, all public domain US government or Unlicense:
    BTS On-Time Performance          one record per domestic flight by carriers
                                     above the BTS revenue reporting threshold:
                                     delays, cancellations, departure hour
    BTS T-100 Segment (All Carriers) every route flown by every carrier,
                                     domestic and international, passenger and
                                     cargo aircraft, with distance and aircraft
                                     configuration
    BTS T-100 Segment Summary        passengers, seats and departures per
      by Origin Airport              airport per month
    OurAirports                      airport names, cities, coordinates
    US Census Population Estimates   annual population per metropolitan area

Pipeline:
    1. Download one On-Time Performance archive per month.
    2. Stream each archive and aggregate it to per-airport-per-month rows. The
       raw CSV is 286 MB and roughly 631,000 rows per month; 20 of its 110
       columns are read and at most one row is held in memory at a time.
    3. Download one T-100 Segment file per year and aggregate it to departures
       per airport, month, aircraft configuration and distance band.
    4. Fetch T-100 airport totals from the BTS open data API.
    5. Fetch airport metadata from OurAirports.
    6. Match airport markets to Census metropolitan areas and fetch population.
    7. Write the result to SQLite.

Usage:
    python prepare_data.py                      # 2016 to 2025
    python prepare_data.py --years 2024 2025    # selected years only
    python prepare_data.py --skip-download      # rebuild from cache/

Coverage window
---------------
The default window is the calendar years 2016 to 2025. The sources end in
different months: On-Time Performance runs further than T-100, and Census
population estimates end in 2024. A partial year compared against a full one
shows every airport in decline, so the window ends at the last calendar year
that is complete in every flight source. The missing 2025 population year is
carried downstream as a stated assumption.

Pandemic months
---------------
US air traffic collapsed in spring 2020 and recovered unevenly through 2021.
Every month is stored as reported. How much each pandemic month counts in a
trend is an analytical judgment, so the month weights are defined in the
scoring module alongside the other scoring parameters, where they can be
changed without rebuilding this database.
"""

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import time
import zipfile
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
DB_PATH = os.path.join(HERE, "data", "airports.db")

DEFAULT_YEARS = list(range(2016, 2026))

ONTIME_URL = (
    "https://transtats.bts.gov/PREZIP/"
    "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
)
SEGMENT_FORM_URL = (
    "https://transtats.bts.gov/DL_SelectFields.aspx"
    "?gnoyr_VQ=FMG&QO_fu146_anzr=Nv4%20Pn44vr45"
)
T100_URL = "https://data.transportation.gov/resource/r495-tyji.json"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# Census metropolitan population. The API requires a key; these flat files do
# not, and they carry the same estimates. Two files are needed because Census
# rebases the series after each decennial census.
CENSUS_URLS = [
    ("https://www2.census.gov/programs-surveys/popest/datasets/"
     "2010-2019/metro/totals/cbsa-est2019-alldata.csv", range(2010, 2020)),
    ("https://www2.census.gov/programs-surveys/popest/datasets/"
     "2020-2024/metro/totals/cbsa-est2024-alldata.csv", range(2020, 2025)),
]

# Columns requested from the T-100 Segment download form. The form returns only
# the columns selected.
SEGMENT_FIELDS = [
    "YEAR", "MONTH", "ORIGIN", "ORIGIN_COUNTRY",
    "DISTANCE", "AIRCRAFT_CONFIG", "DEPARTURES_PERFORMED",
]

# Haul length is classified by the route distance T-100 publishes for each
# segment, in statute miles.
#
# Distance is used rather than the flight time T-100 also carries, because
# flight time is reported by US carriers only. Foreign carriers record zero
# minutes: in 2025, 79 percent of LAX departures on routes of 2,700 miles or
# more carry no flight time. A time threshold would classify most
# international long-haul flights as short haul.
#
# 2,700 statute miles approximates six hours of block time at typical jet cruise
# speed including taxi and climb. Six hours is the common industry threshold.
#
# Three bands rather than two: the threshold is a convention, not a physical
# boundary, and a single long-haul percentage hides how much traffic sits just
# below the line. The medium band makes that sensitivity visible.
LONG_HAUL_MILES = 2700
MEDIUM_HAUL_MILES = 1400

# The five BTS delay-cause columns. NASDelay is the one that matters most:
# BTS defines it as delay from airport operations, traffic volume, air traffic
# control and non-extreme weather, i.e. the delay that infrastructure can fix.
DELAY_CAUSES = [
    "CarrierDelay",
    "WeatherDelay",
    "NASDelay",
    "SecurityDelay",
    "LateAircraftDelay",
]


# ----------------------------------------------------------------------------
# 1. Download
# ----------------------------------------------------------------------------

def month_list(years):
    """Expand a list of years into (year, month) pairs, oldest first."""
    out = []
    for y in years:
        for m in range(1, 13):
            out.append((y, m))
    return out


def download_month(year, month):
    """Fetch one monthly On-Time zip into cache/. Returns the path, or None.

    Files are skipped if already on disk, so the whole run is resumable.
    """
    name = f"ontime_{year}_{month:02d}.zip"
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and os.path.getsize(path) > 100_000:
        return path

    url = ONTIME_URL.format(year=year, month=month)
    try:
        r = requests.get(url, stream=True, timeout=180)
    except requests.RequestException as e:
        print(f"  {year}-{month:02d}  network error: {e}")
        return None

    if r.status_code != 200:
        # Recent months may not be published yet. Not an error.
        print(f"  {year}-{month:02d}  not available (HTTP {r.status_code})")
        return None

    tmp = path + ".part"
    total = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
            total += len(chunk)
    os.replace(tmp, path)
    print(f"  {year}-{month:02d}  downloaded {total/1e6:5.1f} MB")
    return path


def download_segments(year):
    """Fetch one year of T-100 Segment (All Carriers) into cache/.

    This table is not published on the BTS open data API. TranStats serves it
    through an ASP.NET download form, so the request replays that form: a GET
    collects the hidden state fields the form requires, and a POST with the
    year and column selection returns a zip. A form can change without notice,
    which a static file URL does not, so every download is cached and a
    rebuild from cache never contacts the form.

    Returns the path, or None if the year is not available.
    """
    path = os.path.join(CACHE, f"t100_segment_{year}.zip")
    if os.path.exists(path) and os.path.getsize(path) > 100_000:
        return path

    session = requests.Session()
    try:
        page = session.get(SEGMENT_FORM_URL, timeout=120)
        page.raise_for_status()
        hidden = dict(re.findall(
            r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"', page.text))
        form = {
            **hidden,
            "cboGeography": "All",
            "cboYear": str(year),
            "cboPeriod": "All",
            "chkDownloadZip": "on",
            "btnDownload": "Download",
        }
        form.update({field: "on" for field in SEGMENT_FIELDS})
        r = session.post(SEGMENT_FORM_URL, data=form, timeout=600)
    except requests.RequestException as e:
        print(f"  segments {year}  network error: {e}")
        return None

    if r.status_code != 200 or r.content[:2] != b"PK":
        print(f"  segments {year}  not available (HTTP {r.status_code}, "
              f"{r.headers.get('Content-Type')})")
        return None

    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, path)
    print(f"  segments {year}  downloaded {len(r.content)/1e6:5.1f} MB")
    return path


# ----------------------------------------------------------------------------
# 2. Aggregate
# ----------------------------------------------------------------------------

def new_bucket():
    """One accumulator per airport per month."""
    b = {
        "flights": 0,
        "cancelled": 0,
        "diverted": 0,
        "dep_del15": 0,
        "dep_delay_min": 0.0,
        "arr_delay_min": 0.0,
        "city_market_id": "",
        "state": "",
    }
    for c in DELAY_CAUSES:
        b[c] = 0.0
    return b


def fnum(row, key):
    """Read a numeric CSV field that may be blank."""
    v = row.get(key) or ""
    if v == "":
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def aggregate_month(zip_path):
    """Stream one monthly On-Time zip and return three aggregates.

    by_origin : per departing airport: flights, cancellations, diversions,
                delay minutes by cause, metropolitan market and state.
    by_dest   : per arriving airport, delay causes only. The BTS delay-cause
                columns describe ARRIVAL delay, so grouping them by origin
                attributes congestion at the destination to the wrong airport.
                Both attributions are produced so the scoring model can be
                validated against each.
    by_hour   : per airport per departure-hour block, flight counts only.
                Terminal congestion is a peak-hour effect and is not visible
                in an annual average.

    Flight distance is not taken from this source. On-Time Performance covers
    domestic passenger flights only; distance comes from T-100 Segment, which
    covers every flight.
    """
    by_origin = defaultdict(new_bucket)
    by_dest = defaultdict(lambda: {c: 0.0 for c in DELAY_CAUSES} | {"flights": 0})
    by_hour = defaultdict(int)

    with zipfile.ZipFile(zip_path) as z:
        inner = z.namelist()[0]
        with z.open(inner) as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="ignore"))
            for row in reader:
                year = row.get("Year") or ""
                month = row.get("Month") or ""
                origin = row.get("Origin") or ""
                dest = row.get("Dest") or ""
                if not origin or not year:
                    continue

                key = (int(year), int(month), origin)
                b = by_origin[key]
                b["flights"] += 1
                b["cancelled"] += int(fnum(row, "Cancelled"))
                b["diverted"] += int(fnum(row, "Diverted"))
                b["dep_del15"] += int(fnum(row, "DepDel15"))
                b["dep_delay_min"] += fnum(row, "DepDelayMinutes")
                b["arr_delay_min"] += fnum(row, "ArrDelayMinutes")
                for c in DELAY_CAUSES:
                    b[c] += fnum(row, c)

                if not b["city_market_id"]:
                    b["city_market_id"] = row.get("OriginCityMarketID") or ""
                    b["state"] = row.get("OriginStateName") or ""

                if dest:
                    d = by_dest[(int(year), int(month), dest)]
                    d["flights"] += 1
                    for c in DELAY_CAUSES:
                        d[c] += fnum(row, c)

                blk = row.get("DepTimeBlk") or ""
                if blk:
                    by_hour[(int(year), origin, blk)] += 1

    return by_origin, by_dest, by_hour


def aggregate_segments(zip_path):
    """Aggregate one year of T-100 Segment to departures by distance band.

    Key: (year, month, origin airport, aircraft configuration code).

    AIRCRAFT_CONFIG is stored as the code BTS publishes:
        1  passenger
        2  cargo aircraft (freight configuration)
        3  combined passenger and freight on the main deck
        4  seaplane
    Grouping codes into passenger and cargo service is left to the code
    that reads the database, so the stored figures remain exactly as reported.

    Only departures from US airports are kept. The source also lists segments
    flown from foreign airports into the United States.
    """
    out = defaultdict(lambda: {"departures": 0.0, "long": 0.0, "medium": 0.0, "short": 0.0})
    with zipfile.ZipFile(zip_path) as z:
        inner = next(n for n in z.namelist() if n.upper().startswith("T_T100"))
        with z.open(inner) as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="ignore"))
            for row in reader:
                if row.get("ORIGIN_COUNTRY") != "US":
                    continue
                deps = fnum(row, "DEPARTURES_PERFORMED")
                if deps == 0:
                    continue
                key = (int(row["YEAR"]), int(row["MONTH"]), row["ORIGIN"],
                       int(fnum(row, "AIRCRAFT_CONFIG")))
                a = out[key]
                a["departures"] += deps
                miles = fnum(row, "DISTANCE")
                if miles >= LONG_HAUL_MILES:
                    a["long"] += deps
                elif miles >= MEDIUM_HAUL_MILES:
                    a["medium"] += deps
                else:
                    a["short"] += deps
    return out


# ----------------------------------------------------------------------------
# 3. Reference data
# ----------------------------------------------------------------------------

def fetch_t100():
    """T-100 passengers, seats and departures, per airport per month.

    The passenger-volume source, and the independent reference that the
    route-level T-100 Segment totals are reconciled against. On-Time Performance
    counts flights rather than people, and covers only carriers above the BTS
    reporting threshold, so the two datasets have different coverage. See
    DESIGN.md.
    """
    rows = []
    offset = 0
    while True:
        params = {
            "$select": "origin_airport_code,year,reporting_month,total_departures,"
                       "total_passengers,total_seats,domestic_passengers,"
                       "outbound_international_1",
            "$limit": 50000,
            "$offset": offset,
        }
        r = requests.get(T100_URL, params=params, timeout=120)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        print(f"  t100: {len(rows):,} rows")
        if len(batch) < 50000:
            break
    return rows


def fetch_airports():
    """Airport names, cities, coordinates and country.

    Every airport holding an IATA code is kept, worldwide. OurAirports lists US
    territories such as Puerto Rico and Guam under their own country codes, so
    a filter on the United States would drop airports that BTS reports as
    domestic.
    """
    r = requests.get(OURAIRPORTS_URL, timeout=180)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    out = []
    for row in reader:
        code = (row.get("iata_code") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": row.get("name") or "",
            "city": row.get("municipality") or "",
            "country": row.get("iso_country") or "",
            "region": (row.get("iso_region") or "").replace("US-", ""),
            "lat": row.get("latitude_deg") or "",
            "lon": row.get("longitude_deg") or "",
            "type": row.get("type") or "",
        })
    return out


def _normalise_place(text):
    """Reduce a place name to a comparable form."""
    text = text.lower().replace(".", "").replace("saint ", "st ")
    return re.sub(r"[^a-z0-9 ]", " ", text)


def fetch_metro_population(market_cities, years):
    """Annual population for the metropolitan area around each airport market.

    This is the reference for the growth-gap measure: an airport whose traffic
    is flat while its region grows is the pattern the ranking is looking for.

    Census identifies metropolitan areas by CBSA code and BTS identifies them by
    city market id, and no published crosswalk links the two. They are matched
    on place name and state instead: an airport's city is compared against the
    component names of each CBSA in the same state, taking the shortest match so
    that "Ontario, CA" resolves to Ontario rather than to a larger area that
    merely contains the word.

    Coverage is partial by construction. Roughly two thirds of airport markets
    match, accounting for about 95 percent of flight volume. The remainder are
    mostly Hawaii, Puerto Rico and resort destinations that Census either names
    differently or does not classify as metropolitan areas. Unmatched markets
    have no population record in the database.

    The two Census files are joined on the CBSA code, not the name. Census
    renamed many metropolitan areas under its 2023 definitions (for example
    Denver-Aurora-Lakewood became Denver-Aurora-Centennial) while keeping their
    codes. Joined by name, each renamed area splits into two entries holding
    half the series each. Every name an area has carried is used for matching,
    and the most recent name is stored.

    market_cities: {city_market_id: (city, state_code)}
    years:         years to keep; Census publishes nothing after 2024
    returns: {city_market_id: {"cbsa": name, "pop": {year: population}}}
    """
    wanted = set(years)
    metros = {}   # CBSA code -> {"names": [oldest first], "pop": {year: population}}
    for url, census_years in CENSUS_URLS:
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.text)):
            if row.get("LSAD") != "Metropolitan Statistical Area":
                continue
            area = metros.setdefault(row["CBSA"], {"names": [], "pop": {}})
            if row["NAME"] not in area["names"]:
                area["names"].append(row["NAME"])
            area["pop"].update({y: int(row[f"POPESTIMATE{y}"])
                                for y in census_years
                                if y in wanted and (row.get(f"POPESTIMATE{y}") or "").isdigit()})

    index = []
    for code, area in metros.items():
        for name in area["names"]:
            head, states = name.rsplit(",", 1)
            index.append((code, _normalise_place(head), set(states.strip().split("-"))))

    out = {}
    for market, (city, state) in market_cities.items():
        # OurAirports cities may carry a qualifier after a comma, e.g. "Honolulu, Oahu".
        tokens = [_normalise_place(t).strip() for t in re.split(r"[-/,]", city) if t.strip()]
        best = None
        for code, head, states in index:
            if state not in states:
                continue
            if any(t and t in head for t in tokens):
                if best is None or len(head) < len(best[1]):
                    best = (code, head)
        if best:
            area = metros[best[0]]
            out[market] = {"cbsa": area["names"][-1], "pop": area["pop"]}
    return out


# ----------------------------------------------------------------------------
# 4. Write the database
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE airport_month (
    year INTEGER, month INTEGER, airport TEXT,
    flights INTEGER, cancelled INTEGER, diverted INTEGER,
    dep_del15 INTEGER, dep_delay_min REAL, arr_delay_min REAL,
    carrier_delay REAL, weather_delay REAL, nas_delay REAL,
    security_delay REAL, late_aircraft_delay REAL,
    city_market_id TEXT, state TEXT,
    PRIMARY KEY (year, month, airport)
);

CREATE TABLE arrival_delay_month (
    year INTEGER, month INTEGER, airport TEXT, flights INTEGER,
    carrier_delay REAL, weather_delay REAL, nas_delay REAL,
    security_delay REAL, late_aircraft_delay REAL,
    PRIMARY KEY (year, month, airport)
);

CREATE TABLE peak_hour (
    year INTEGER, airport TEXT, dep_time_block TEXT, flights INTEGER,
    PRIMARY KEY (year, airport, dep_time_block)
);

CREATE TABLE haul_month (
    year INTEGER, month INTEGER, airport TEXT, aircraft_config INTEGER,
    departures INTEGER, long_haul INTEGER, medium_haul INTEGER, short_haul INTEGER,
    PRIMARY KEY (year, month, airport, aircraft_config)
);

CREATE TABLE t100_month (
    year INTEGER, month INTEGER, airport TEXT,
    departures REAL, passengers REAL, seats REAL,
    domestic_passengers REAL, international_passengers REAL,
    PRIMARY KEY (year, month, airport)
);

CREATE TABLE metro_population (
    city_market_id TEXT, year INTEGER, population INTEGER, cbsa_name TEXT,
    PRIMARY KEY (city_market_id, year)
);

CREATE TABLE airports (
    code TEXT PRIMARY KEY, name TEXT, city TEXT, country TEXT, region TEXT,
    lat REAL, lon REAL, type TEXT
);

CREATE INDEX idx_am_airport ON airport_month(airport);
CREATE INDEX idx_am_year ON airport_month(year);
CREATE INDEX idx_am_market ON airport_month(city_market_id);
CREATE INDEX idx_haul_airport ON haul_month(airport);
CREATE INDEX idx_t100_airport ON t100_month(airport);
"""


def write_db(by_origin, by_dest, by_hour, segments, t100, airports, metros, years):
    """Write every table to a new database file, then swap it into place.

    Building a fresh file guarantees that tables removed from the schema do not
    survive from an earlier build, and a build that fails part-way leaves the
    previous database untouched.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp_path = DB_PATH + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    con = sqlite3.connect(tmp_path)
    con.executescript(SCHEMA)
    year_set = set(years)

    con.executemany(
        "INSERT INTO airport_month VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (y, m, a, b["flights"], b["cancelled"], b["diverted"], b["dep_del15"],
             b["dep_delay_min"], b["arr_delay_min"],
             b["CarrierDelay"], b["WeatherDelay"], b["NASDelay"],
             b["SecurityDelay"], b["LateAircraftDelay"],
             b["city_market_id"], b["state"])
            for (y, m, a), b in by_origin.items()
        ],
    )

    con.executemany(
        "INSERT INTO arrival_delay_month VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (y, m, a, d["flights"], d["CarrierDelay"], d["WeatherDelay"],
             d["NASDelay"], d["SecurityDelay"], d["LateAircraftDelay"])
            for (y, m, a), d in by_dest.items()
        ],
    )

    con.executemany(
        "INSERT INTO peak_hour VALUES (?,?,?,?)",
        [(y, a, blk, n) for (y, a, blk), n in by_hour.items()],
    )

    con.executemany(
        "INSERT INTO haul_month VALUES (?,?,?,?,?,?,?,?)",
        [
            (y, m, a, cfg, round(v["departures"]), round(v["long"]),
             round(v["medium"]), round(v["short"]))
            for (y, m, a, cfg), v in segments.items()
        ],
    )

    con.executemany(
        "INSERT OR REPLACE INTO t100_month VALUES (?,?,?,?,?,?,?,?)",
        [
            (int(r["year"]), int(r["reporting_month"][5:7]), r["origin_airport_code"],
             float(r.get("total_departures") or 0), float(r.get("total_passengers") or 0),
             float(r.get("total_seats") or 0), float(r.get("domestic_passengers") or 0),
             float(r.get("outbound_international_1") or 0))
            for r in t100
            if r.get("origin_airport_code") and r.get("year") and r.get("reporting_month")
            and int(r["year"]) in year_set
        ],
    )

    con.executemany(
        "INSERT OR REPLACE INTO airports VALUES (?,?,?,?,?,?,?,?)",
        [
            (a["code"], a["name"], a["city"], a["country"], a["region"],
             float(a["lat"] or 0), float(a["lon"] or 0), a["type"])
            for a in airports
        ],
    )

    con.executemany(
        "INSERT INTO metro_population VALUES (?,?,?,?)",
        [
            (market, year, pop, info["cbsa"])
            for market, info in metros.items()
            for year, pop in info["pop"].items()
        ],
    )

    con.commit()
    for t in ("airport_month", "arrival_delay_month", "peak_hour", "haul_month",
              "t100_month", "metro_population", "airports"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<22} {n:>9,} rows")
    con.execute("VACUUM")
    con.close()
    os.replace(tmp_path, DB_PATH)
    print(f"\ndatabase written: {DB_PATH}  ({os.path.getsize(DB_PATH)/1e6:.1f} MB)")


# ----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", nargs="+", type=int, default=DEFAULT_YEARS,
                   help="calendar years to include (default 2016 to 2025, the last "
                        "year complete in every flight source)")
    p.add_argument("--skip-download", action="store_true",
                   help="rebuild the database from files already in cache/")
    args = p.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    t_start = time.time()

    print(f"Building from {min(args.years)} to {max(args.years)}\n")

    paths = []
    print("STEP 1: download On-Time Performance")
    for year, month in month_list(args.years):
        if args.skip_download:
            pth = os.path.join(CACHE, f"ontime_{year}_{month:02d}.zip")
            if os.path.exists(pth):
                paths.append(pth)
        else:
            pth = download_month(year, month)
            if pth:
                paths.append(pth)
    print(f"  {len(paths)} monthly files ready\n")

    print("STEP 2: aggregate On-Time Performance")
    by_origin, by_dest, by_hour = defaultdict(new_bucket), defaultdict(
        lambda: {c: 0.0 for c in DELAY_CAUSES} | {"flights": 0}), defaultdict(int)
    for i, pth in enumerate(paths, 1):
        o, d, h = aggregate_month(pth)
        for k, v in o.items():
            tgt = by_origin[k]
            for f, val in v.items():
                tgt[f] = val if isinstance(val, str) else tgt[f] + val
        for k, v in d.items():
            tgt = by_dest[k]
            for f, val in v.items():
                tgt[f] += val
        for k, v in h.items():
            by_hour[k] += v
        print(f"  [{i:>3}/{len(paths)}] {os.path.basename(pth)}  "
              f"{len(by_origin):,} airport-months so far")
    print()

    print("STEP 3: T-100 Segment, route distances for every carrier")
    segments = defaultdict(lambda: {"departures": 0.0, "long": 0.0, "medium": 0.0, "short": 0.0})
    for year in args.years:
        if args.skip_download:
            pth = os.path.join(CACHE, f"t100_segment_{year}.zip")
            pth = pth if os.path.exists(pth) else None
        else:
            pth = download_segments(year)
        if not pth:
            continue
        for k, v in aggregate_segments(pth).items():
            tgt = segments[k]
            for f, val in v.items():
                tgt[f] += val
    print(f"  {len(segments):,} airport-month-configuration rows\n")

    print("STEP 4: T-100 airport totals")
    t100 = fetch_t100()
    print()

    print("STEP 5: airport metadata")
    airports = fetch_airports()
    us = sum(1 for a in airports if a["country"] == "US")
    print(f"  {len(airports):,} airports with an IATA code ({us:,} in the US)\n")

    print("STEP 6: metropolitan population")
    city_of = {a["code"]: (a["city"], a["region"]) for a in airports}
    biggest = {}
    for (y, m, apt), b in by_origin.items():
        mkt = b["city_market_id"]
        if not mkt:
            continue
        cur = biggest.get(mkt)
        if cur is None or b["flights"] > cur[1]:
            biggest[mkt] = (apt, b["flights"])
    market_cities = {
        mkt: city_of[apt] for mkt, (apt, _) in biggest.items() if apt in city_of
    }
    metros = fetch_metro_population(market_cities, args.years)
    matched = len(metros)
    print(f"  {matched:,} of {len(market_cities):,} airport markets matched to a "
          f"Census metropolitan area ({matched/max(len(market_cities),1)*100:.0f}%)")
    print("  unmatched markets have no population record\n")

    print("STEP 7: write database")
    write_db(by_origin, by_dest, by_hour, segments, t100, airports, metros, args.years)
    print(f"\ntotal time: {(time.time()-t_start)/60:.1f} minutes")


if __name__ == "__main__":
    sys.exit(main())
