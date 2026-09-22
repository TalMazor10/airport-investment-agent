"""
Build the airport analytics database from public US aviation data.

This is an offline build step, not a runtime dependency. It produces
data/airports.db, which is committed to the repository; the application reads
that database directly and performs no downloads while serving queries.

Sources, all public domain US government or Unlicense:
    BTS On-Time Performance  one record per domestic flight
    BTS T-100 Segment        passengers and seats per airport per month
    OurAirports              airport names, cities, states, coordinates

Pipeline:
    1. Download one archive per month from the BTS On-Time Performance set.
    2. Stream each archive and aggregate to per-airport-per-month rows. The
       raw CSV is 286 MB and roughly 631,000 rows per month; 20 of its 110
       columns are retained and at most one row is held in memory at a time.
    3. Fetch T-100 passenger and seat counts from the BTS open data API.
    4. Fetch airport metadata from OurAirports.
    5. Write the result to SQLite.

Usage:
    python prepare_data.py                      # 2016 to present
    python prepare_data.py --years 2024 2025    # selected years only
    python prepare_data.py --skip-download      # rebuild from cache/

COVID handling: 2020 and 2021 are downloaded and stored. They are excluded at
query time rather than at ingest, which keeps the exclusion visible in the
output, reversible, and controllable by the caller.
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import zipfile
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
DB_PATH = os.path.join(HERE, "data", "airports.db")

ONTIME_URL = (
    "https://transtats.bts.gov/PREZIP/"
    "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
)
T100_URL = "https://data.transportation.gov/resource/r495-tyji.json"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# A flight is "long haul" at 6 hours or more. CRSElapsedTime is scheduled
# minutes, so this is measured rather than estimated from distance.
LONG_HAUL_MINUTES = 360
MEDIUM_HAUL_MINUTES = 180

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
    """Fetch one monthly zip into cache/. Returns the path, or None if absent.

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
        "distance_sum": 0.0,
        "elapsed_sum": 0.0,
        "long_haul": 0,
        "medium_haul": 0,
        "short_haul": 0,
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
    """Stream one monthly zip and return three aggregates.

    by_origin : per departing airport, all retained measures
    by_dest   : per arriving airport, delay causes only. The BTS delay-cause
                columns describe ARRIVAL delay, so grouping them by origin
                attributes congestion at the destination to the wrong airport.
                Both attributions are produced so the scoring model can be
                validated against each.
    by_hour   : per airport per departure-hour block, flight counts only.
                Terminal congestion is a peak-hour effect and is not visible
                in an annual average.
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
                b["distance_sum"] += fnum(row, "Distance")
                for c in DELAY_CAUSES:
                    b[c] += fnum(row, c)

                # Scheduled duration drives the long-haul split.
                elapsed = fnum(row, "CRSElapsedTime")
                b["elapsed_sum"] += elapsed
                if elapsed >= LONG_HAUL_MINUTES:
                    b["long_haul"] += 1
                elif elapsed >= MEDIUM_HAUL_MINUTES:
                    b["medium_haul"] += 1
                else:
                    b["short_haul"] += 1

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


# ----------------------------------------------------------------------------
# 3. Reference data from public APIs
# ----------------------------------------------------------------------------

def fetch_t100():
    """T-100 passengers, seats and departures, per airport per month.

    The passenger-volume source. On-Time Performance counts flights rather than
    people, and covers only carriers above the BTS reporting threshold, so the
    two datasets have different coverage. See DESIGN.md.
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
    """Airport names, cities, states and coordinates. Public domain (Unlicense)."""
    r = requests.get(OURAIRPORTS_URL, timeout=120)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    out = []
    for row in reader:
        if row.get("iso_country") != "US":
            continue
        code = (row.get("iata_code") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": row.get("name") or "",
            "city": row.get("municipality") or "",
            "region": (row.get("iso_region") or "").replace("US-", ""),
            "lat": row.get("latitude_deg") or "",
            "lon": row.get("longitude_deg") or "",
            "type": row.get("type") or "",
        })
    return out


# ----------------------------------------------------------------------------
# 4. Write the database
# ----------------------------------------------------------------------------

SCHEMA = """
DROP TABLE IF EXISTS airport_month;
CREATE TABLE airport_month (
    year INTEGER, month INTEGER, airport TEXT,
    flights INTEGER, cancelled INTEGER, diverted INTEGER,
    dep_del15 INTEGER, dep_delay_min REAL, arr_delay_min REAL,
    carrier_delay REAL, weather_delay REAL, nas_delay REAL,
    security_delay REAL, late_aircraft_delay REAL,
    distance_sum REAL, elapsed_sum REAL,
    long_haul INTEGER, medium_haul INTEGER, short_haul INTEGER,
    city_market_id TEXT, state TEXT,
    PRIMARY KEY (year, month, airport)
);

DROP TABLE IF EXISTS arrival_delay_month;
CREATE TABLE arrival_delay_month (
    year INTEGER, month INTEGER, airport TEXT, flights INTEGER,
    carrier_delay REAL, weather_delay REAL, nas_delay REAL,
    security_delay REAL, late_aircraft_delay REAL,
    PRIMARY KEY (year, month, airport)
);

DROP TABLE IF EXISTS peak_hour;
CREATE TABLE peak_hour (
    year INTEGER, airport TEXT, dep_time_block TEXT, flights INTEGER,
    PRIMARY KEY (year, airport, dep_time_block)
);

DROP TABLE IF EXISTS t100_month;
CREATE TABLE t100_month (
    year INTEGER, month INTEGER, airport TEXT,
    departures REAL, passengers REAL, seats REAL,
    domestic_passengers REAL, international_passengers REAL,
    PRIMARY KEY (year, month, airport)
);

DROP TABLE IF EXISTS airports;
CREATE TABLE airports (
    code TEXT PRIMARY KEY, name TEXT, city TEXT, region TEXT,
    lat REAL, lon REAL, type TEXT
);

CREATE INDEX idx_am_airport ON airport_month(airport);
CREATE INDEX idx_am_year ON airport_month(year);
CREATE INDEX idx_t100_airport ON t100_month(airport);
CREATE INDEX idx_am_market ON airport_month(city_market_id);
"""


def write_db(by_origin, by_dest, by_hour, t100, airports):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    con.executemany(
        "INSERT OR REPLACE INTO airport_month VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (y, m, a, b["flights"], b["cancelled"], b["diverted"], b["dep_del15"],
             b["dep_delay_min"], b["arr_delay_min"],
             b["CarrierDelay"], b["WeatherDelay"], b["NASDelay"],
             b["SecurityDelay"], b["LateAircraftDelay"],
             b["distance_sum"], b["elapsed_sum"],
             b["long_haul"], b["medium_haul"], b["short_haul"],
             b["city_market_id"], b["state"])
            for (y, m, a), b in by_origin.items()
        ],
    )

    con.executemany(
        "INSERT OR REPLACE INTO arrival_delay_month VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (y, m, a, d["flights"], d["CarrierDelay"], d["WeatherDelay"],
             d["NASDelay"], d["SecurityDelay"], d["LateAircraftDelay"])
            for (y, m, a), d in by_dest.items()
        ],
    )

    con.executemany(
        "INSERT OR REPLACE INTO peak_hour VALUES (?,?,?,?)",
        [(y, a, blk, n) for (y, a, blk), n in by_hour.items()],
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
        ],
    )

    con.executemany(
        "INSERT OR REPLACE INTO airports VALUES (?,?,?,?,?,?,?)",
        [
            (a["code"], a["name"], a["city"], a["region"],
             float(a["lat"] or 0), float(a["lon"] or 0), a["type"])
            for a in airports
        ],
    )

    con.commit()
    for t in ("airport_month", "arrival_delay_month", "peak_hour", "t100_month", "airports"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<22} {n:>9,} rows")
    con.execute("VACUUM")
    con.close()
    print(f"\ndatabase written: {DB_PATH}  ({os.path.getsize(DB_PATH)/1e6:.1f} MB)")


# ----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", nargs="+", type=int,
                   default=list(range(2016, 2027)),
                   help="years to include (2020 and 2021 are kept, and excluded at query time)")
    p.add_argument("--skip-download", action="store_true",
                   help="rebuild the database from files already in cache/")
    args = p.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    t_start = time.time()

    print(f"Building from {min(args.years)} to {max(args.years)}\n")

    paths = []
    print("STEP 1: download")
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

    print("STEP 2: aggregate")
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

    print("STEP 3: T-100 passenger data")
    t100 = fetch_t100()
    print()

    print("STEP 4: airport metadata")
    airports = fetch_airports()
    print(f"  {len(airports):,} US airports\n")

    print("STEP 5: write database")
    write_db(by_origin, by_dest, by_hour, t100, airports)
    print(f"\ntotal time: {(time.time()-t_start)/60:.1f} minutes")


if __name__ == "__main__":
    sys.exit(main())
