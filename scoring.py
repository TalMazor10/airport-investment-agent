"""
Deterministic airport scoring.

Every figure an answer relies on is computed here, in ordinary code, from
data/airports.db. The language model decides which airports to score and
explains the result. It never produces a number.

Terms
-----
growth_gap  Metropolitan population growth minus passenger growth, in
            percentage points per year. Positive when the region is
            outgrowing its airport: the plateau fingerprint of unmet demand.
spillover   Trend in the airport's share of its metropolitan area's
            passengers, in percentage points per year. Losing share scores as
            pressure: demand the airport cannot serve moves to its neighbours.
nas_delay   National Aviation System delay minutes per arriving flight,
            2023 to 2025. BTS assigns this category to airport operations,
            traffic volume and air traffic control, the delay that
            infrastructure can reduce. It is attributed to the arrival
            airport, where BTS records it and where traffic management
            programmes act: when fog cuts San Francisco's arrival rate, flights
            bound for SFO wait at their origins and the delay is recorded on
            arrival into SFO.
scale       Passengers in the latest year. A given improvement at a large
            airport is worth more than the same improvement at a small one.

Method
------
Each term is converted to a percentile against every US airport above
MIN_PASSENGERS, so an airport's score does not depend on which other airports
a question names. The score is the weighted mean of the available
percentiles, from 0 to 100.

A term that cannot be computed is left out, the remaining weights are
rescaled to sum to one, and the result records which term is missing and why.
The growth gap requires the full 2016 to 2024 population series; a partial
series would compare population and traffic over different periods.
An airport alone in its metropolitan area receives the neutral spillover
percentile, 50: there is no neighbouring airport for traffic to move to, so
spillover neither raises nor lowers it.

Peak gate. Falling traffic widens the growth gap and lowers an airport's share
of its metropolitan area, so without a guard an airport that demand has abandoned scores
like one that is full. An airport carrying less than PEAK_GATE of the
passengers of its own busiest year has already handled more traffic than it
carries now, so a plateau or share loss there is not counted as evidence of a
ceiling. For those airports growth gap and spillover take the neutral
percentile. Their raw values are still reported, and delay can still show
strain.

Growth rates are slopes of a trend line fitted through every month of the
window on a log scale, with calendar-month effects removed and each month
weighted by MONTH_WEIGHTS, so pandemic months count for less.

Scores are computed when first requested and held in memory for the life of
the process. Nothing is written back to the database.
"""

import math
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import numpy as np

DB_PATH = Path(__file__).resolve().parent / "data" / "airports.db"

# ----------------------------------------------------------------------------
# Settings. Every analytical judgment in the score is defined here.
# ----------------------------------------------------------------------------

# Scale carries 0.30 because profitability needs volume. The remaining 0.70 is
# pressure. NAS delay carries 0.30 of it because it is the term that separates
# an airport that is full from one that demand has left: a plateau with heavy
# traffic delay indicates a ceiling, a plateau without it does not.
TERM_WEIGHTS = {
    "growth_gap": 0.20,
    "spillover": 0.20,
    "nas_delay": 0.30,
    "scale": 0.30,
}

MIN_PASSENGERS = 100_000        # airports below this in LAST_YEAR are reported, not ranked
FIRST_YEAR, LAST_YEAR = 2016, 2025
LAST_POPULATION_YEAR = 2024     # Census publishes nothing later
CENSUS_REBASE_YEAR = 2020       # first year of estimates based on the 2020 census
NAS_YEARS = (2023, 2025)        # inclusive
PEAK_GATE = 0.95                # share of its own peak-year passengers; below this, no ceiling
MIN_MONTHS_FOR_TREND = 36       # months of data needed for a trend, each counted by its weight
NEUTRAL_PERCENTILE = 50.0
SENSITIVITY_STEP = 0.05
SENSITIVITY_TOP_N = 3

# Weight of each month in a trend. Months not listed count fully.
#
# The shape follows the traffic record rather than the calendar. March 2020
# holds roughly half a normal month before the collapse. April and May 2020 are
# the trough, at under a fifth of normal volume, and carry almost no
# information about underlying demand. Recovery is gradual through 2021 and the
# weights rise with it, reaching full weight in 2022. Each month is listed
# individually so that any one can be changed without touching the others.
MONTH_WEIGHTS = {
    (2020, 3): 0.50,
    (2020, 4): 0.05,
    (2020, 5): 0.05,
    (2020, 6): 0.10,
    (2020, 7): 0.15,
    (2020, 8): 0.15,
    (2020, 9): 0.20,
    (2020, 10): 0.20,
    (2020, 11): 0.20,
    (2020, 12): 0.20,
    (2021, 1): 0.25,
    (2021, 2): 0.25,
    (2021, 3): 0.35,
    (2021, 4): 0.40,
    (2021, 5): 0.45,
    (2021, 6): 0.50,
    (2021, 7): 0.55,
    (2021, 8): 0.55,
    (2021, 9): 0.60,
    (2021, 10): 0.65,
    (2021, 11): 0.70,
    (2021, 12): 0.70,
}


def month_weight(year, month):
    return MONTH_WEIGHTS.get((year, month), 1.0)


# ----------------------------------------------------------------------------
# Arithmetic
# ----------------------------------------------------------------------------

def trend_slope(rows, log):
    """Slope per year of a trend line through monthly observations.

    rows: iterable of (year, month, value).
    The line is fitted by weighted least squares with one intercept per
    calendar month, so the seasonal pattern does not bend it, and each month is
    weighted by month_weight. With log=True the values are fitted on a log
    scale and the result is a growth rate per year (0.03 = 3 percent).
    Otherwise the result is the change in value per year.

    Returns None when the usable months, each counted by its weight, amount to
    fewer than MIN_MONTHS_FOR_TREND. A pandemic month at weight 0.05 counts as
    0.05 of a month.
    """
    rows = [(y, m, v) for y, m, v in rows if v is not None and (v > 0 or not log)]
    if sum(month_weight(y, m) for y, m, _ in rows) < MIN_MONTHS_FOR_TREND:
        return None
    t = np.array([(y - FIRST_YEAR) + (m - 1) / 12 for y, m, _ in rows])
    values = np.array([v for _, _, v in rows], dtype=float)
    target = np.log(values) if log else values
    months = np.array([m for _, m, _ in rows])
    design = np.column_stack([t] + [(months == k).astype(float) for k in range(1, 13)])
    root_w = np.sqrt([month_weight(y, m) for y, m, _ in rows])
    coef, *_ = np.linalg.lstsq(design * root_w[:, None], target * root_w, rcond=None)
    slope = float(coef[0])
    return math.exp(slope) - 1 if log else slope


def annual_growth(points):
    """Growth rate per year of a trend line through yearly population, log scale.

    Census bases its 2010 to 2019 estimates on the 2010 census and its later
    estimates on the 2020 census, and the two series do not meet: New York
    steps up 4 percent between 2019 and 2020 because the 2020 count exceeded
    the projection, not because the population grew. A single line across the
    step would read the correction as growth. The line is therefore allowed one
    step at CENSUS_REBASE_YEAR, and only growth within each series is measured.
    """
    points = [(y, v) for y, v in points if v and v > 0]
    if len(points) < 2:
        return None
    years = np.array([y for y, _ in points], dtype=float)
    columns = [np.ones_like(years), years]
    after = years >= CENSUS_REBASE_YEAR
    if after.any() and not after.all():
        columns.append(after.astype(float))
    coef, *_ = np.linalg.lstsq(np.column_stack(columns), np.log([v for _, v in points]),
                               rcond=None)
    return math.exp(coef[1]) - 1


def percentiles(values):
    """Percentile of each value within its group, from 0 to 100.

    The share of the other values it exceeds, with ties counted as half. The
    lowest value scores 0 and the highest 100.
    """
    ordered = sorted(values.values())
    n = len(ordered)
    if n == 1:
        return {k: NEUTRAL_PERCENTILE for k in values}
    out = {}
    for key, v in values.items():
        below = bisect_left(ordered, v)
        equal = bisect_right(ordered, v) - below
        out[key] = 100.0 * (below + 0.5 * (equal - 1)) / (n - 1)
    return out


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load():
    """Read the rows the score needs from the database, once per process."""
    con = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    data = {
        "passengers": defaultdict(list),   # airport -> [(year, month, passengers)]
        "departures": defaultdict(list),   # airport -> [(year, month, departures)]
        "market": {},                      # airport -> city market id, latest month
        "state": {},                       # airport -> state name, latest month
        "population": defaultdict(list),   # market -> [(year, population)]
        "cbsa": {},                        # market -> Census metropolitan area name
        "nas_arrival": {},                 # airport -> NAS minutes per arriving flight
        "nas_departure": {},               # airport -> NAS minutes per departing flight
        "names": {},                       # airport -> (name, city)
    }
    for y, m, a, pax, deps in con.execute(
            "SELECT year, month, airport, passengers, departures FROM t100_month "
            "WHERE year BETWEEN ? AND ? ORDER BY year, month", (FIRST_YEAR, LAST_YEAR)):
        data["passengers"][a].append((y, m, pax))
        data["departures"][a].append((y, m, deps))
    for a, mkt, state in con.execute(
            "SELECT airport, city_market_id, state FROM airport_month ORDER BY year, month"):
        data["market"][a] = mkt
        data["state"][a] = state
    for mkt, y, pop, cbsa in con.execute(
            "SELECT city_market_id, year, population, cbsa_name FROM metro_population"):
        data["population"][mkt].append((y, pop))
        data["cbsa"][mkt] = cbsa
    first, last = NAS_YEARS
    for key, table in (("nas_arrival", "arrival_delay_month"), ("nas_departure", "airport_month")):
        for a, nas, flights in con.execute(
                f"SELECT airport, SUM(nas_delay), SUM(flights) FROM {table} "
                f"WHERE year BETWEEN ? AND ? GROUP BY airport", (first, last)):
            if flights:
                data[key][a] = nas / flights
    for code, name, city in con.execute("SELECT code, name, city FROM airports"):
        data["names"][code] = (name, city)
    con.close()
    return data


# ----------------------------------------------------------------------------
# The national table
# ----------------------------------------------------------------------------

@lru_cache(maxsize=None)
def national_table(nas_attribution="arrival"):
    """Raw values and percentiles for every airport, computed once per process.

    Returns {airport: record}. Every airport with T-100 data has a record;
    only those with at least MIN_PASSENGERS in LAST_YEAR are ranked and carry
    percentiles. nas_attribution="departure" exists to test the attribution
    choice and is not used when answering questions.
    """
    d = _load()
    annual = defaultdict(lambda: defaultdict(float))
    for a, rows in d["passengers"].items():
        for y, _, p in rows:
            annual[a][y] += p
    latest = {a: annual[a][LAST_YEAR] for a in d["passengers"]}
    peak_year = {a: max(annual[a], key=annual[a].get) for a in d["passengers"]}
    ranked = {a for a, p in latest.items() if p >= MIN_PASSENGERS}

    # Monthly passengers per metropolitan market, for the share calculation.
    market_members = defaultdict(set)
    for a in d["passengers"]:
        if a in d["market"]:
            market_members[d["market"][a]].add(a)
    market_monthly = defaultdict(lambda: defaultdict(float))
    for mkt, members in market_members.items():
        for a in members:
            for y, m, p in d["passengers"][a]:
                market_monthly[mkt][(y, m)] += p

    nas_source = d["nas_arrival"] if nas_attribution == "arrival" else d["nas_departure"]
    table = {}
    for a, pax_rows in d["passengers"].items():
        mkt = d["market"].get(a)
        rec = {
            "airport": a,
            "ranked": a in ranked,
            "raw": {},
            "notes": {},
            "context": {
                "passengers_latest_year": latest[a],
                "peak_year": peak_year[a],
                "share_of_peak": (latest[a] / annual[a][peak_year[a]]
                                  if annual[a][peak_year[a]] else None),
                "passenger_growth": trend_slope(pax_rows, log=True),
                "flight_growth": trend_slope(d["departures"][a], log=True),
                "population_growth": None,
                "metro_area": d["cbsa"].get(mkt),
                "metro_airports": sorted(market_members.get(mkt, {a}) & ranked - {a}),
            },
        }
        ctx = rec["context"]

        # Growth gap
        if mkt is None:
            rec["notes"]["growth_gap"] = "no metropolitan market id for this airport"
        elif mkt not in d["population"]:
            rec["notes"]["growth_gap"] = "no Census population match for its metropolitan area"
        elif ctx["passenger_growth"] is None:
            rec["notes"]["growth_gap"] = "too few months of passenger data for a trend"
        elif _population_years(d["population"][mkt]) != set(range(FIRST_YEAR, LAST_POPULATION_YEAR + 1)):
            years = sorted(_population_years(d["population"][mkt]))
            rec["notes"]["growth_gap"] = (
                f"Census population covers only {years[0]} to {years[-1]} for this metropolitan area, "
                "which Census redefined in 2023; no comparable population trend")
        else:
            ctx["population_growth"] = annual_growth(d["population"][mkt])
            rec["raw"]["growth_gap"] = 100 * (ctx["population_growth"] - ctx["passenger_growth"])

        # Spillover
        others = market_members.get(mkt, set()) & ranked - {a}
        if mkt is None:
            rec["notes"]["spillover"] = "no metropolitan market id for this airport"
        elif not others:
            rec["notes"]["spillover"] = "only ranked airport in its metropolitan area; neutral score"
        else:
            share = [(y, m, 100 * p / market_monthly[mkt][(y, m)])
                     for y, m, p in pax_rows if market_monthly[mkt][(y, m)] > 0]
            slope = trend_slope(share, log=False)
            if slope is None:
                rec["notes"]["spillover"] = "too few months of passenger data for a trend"
            else:
                rec["raw"]["spillover"] = slope

        # NAS delay
        if a in nas_source:
            rec["raw"]["nas_delay"] = nas_source[a]
        else:
            rec["notes"]["nas_delay"] = "no On-Time Performance flights to this airport, 2023 to 2025"

        # Scale
        rec["raw"]["scale"] = latest[a]
        table[a] = rec

    # Percentiles across ranked airports. Spillover is reversed, because losing
    # share is the pressure signal.
    for term in TERM_WEIGHTS:
        sign = -1 if term == "spillover" else 1
        vals = {a: sign * table[a]["raw"][term] for a in ranked if term in table[a]["raw"]}
        for a, p in percentiles(vals).items():
            table[a].setdefault("percentile", {})[term] = p
    for a in ranked:
        pct = table[a].setdefault("percentile", {})
        if "spillover" not in pct and "only ranked airport" in table[a]["notes"].get("spillover", ""):
            pct["spillover"] = NEUTRAL_PERCENTILE

    # Peak gate: below its own peak, an airport's plateau or share loss is not
    # evidence of a ceiling.
    for a in ranked:
        share = table[a]["context"]["share_of_peak"]
        if share is not None and share < PEAK_GATE:
            for term in ("growth_gap", "spillover"):
                if term in table[a]["percentile"]:
                    table[a]["percentile"][term] = NEUTRAL_PERCENTILE
                    table[a]["notes"][term] = (
                        f"carries {share:.0%} of its {peak_year[a]} peak passengers, below the "
                        f"{PEAK_GATE:.0%} threshold, so a plateau or share loss is not counted as "
                        f"evidence of a ceiling; neutral score")
    return table


def _population_years(points):
    return {y for y, p in points if p}


def _weighted_score(rec, weights):
    """Weighted mean of the available percentiles, with weights rescaled."""
    available = {t: w for t, w in weights.items() if t in rec.get("percentile", {})}
    total = sum(available.values())
    if total == 0:
        return None, {}
    effective = {t: w / total for t, w in available.items()}
    return sum(rec["percentile"][t] * w for t, w in effective.items()), effective


# ----------------------------------------------------------------------------
# Public interface
# ----------------------------------------------------------------------------

def score(airports, weights=None, nas_attribution="arrival"):
    """Score a group of airports and rank them within the group.

    airports: IATA codes, e.g. ["BOS", "PVD", "BDL"].
    Returns one record per airport, ranked airports first in score order.
    A ranked record carries its national score and rank, its rank within the
    group, each term's raw value, percentile and effective weight, and notes
    explaining any term that is missing or neutral. Airports below
    MIN_PASSENGERS carry raw figures only.
    """
    weights = weights or TERM_WEIGHTS
    table = national_table(nas_attribution)
    national = {}
    for a, rec in table.items():
        if rec["ranked"]:
            s, _ = _weighted_score(rec, weights)
            if s is not None:
                national[a] = s
    order = sorted(national, key=lambda a: -national[a])
    national_rank = {a: i + 1 for i, a in enumerate(order)}

    d = _load()
    results = []
    for code in dict.fromkeys(c.strip().upper() for c in airports):
        rec = table.get(code)
        name, city = d["names"].get(code, ("", ""))
        if rec is None:
            results.append({"airport": code, "error": "no traffic data for this airport code"})
            continue
        out = {
            "airport": code,
            "name": name,
            "city": city,
            "state": d["state"].get(code, ""),
            "ranked": code in national,
            "context": rec["context"],
            "notes": rec["notes"],
        }
        if code in national:
            _, effective = _weighted_score(rec, weights)
            out["score"] = national[code]
            out["national_rank"] = national_rank[code]
            out["national_pool"] = len(national)
            out["terms"] = {
                t: {"raw": rec["raw"].get(t), "percentile": rec["percentile"].get(t),
                    "weight": effective.get(t, 0.0)}
                for t in TERM_WEIGHTS
            }
        else:
            out["notes"] = {**rec["notes"],
                            "ranking": f"below {MIN_PASSENGERS:,} passengers in {LAST_YEAR}; not ranked"}
            out["terms"] = {t: {"raw": rec["raw"].get(t)} for t in TERM_WEIGHTS}
        results.append(out)

    ranked = sorted((r for r in results if r.get("ranked")), key=lambda r: -r["score"])
    for i, r in enumerate(ranked, 1):
        r["group_rank"] = i
        r["group_size"] = len(ranked)
    return ranked + [r for r in results if not r.get("ranked")]


def sensitivity(airports):
    """Whether the top of a group's ranking survives changes to the weights.

    Each weight is raised and lowered by SENSITIVITY_STEP in turn, with the
    others rescaled so the weights still sum to one. The result lists every
    change that alters the first place or the membership of the top
    SENSITIVITY_TOP_N. Top positions are listed in rank order.
    """
    def top(weights):
        order = [r["airport"] for r in score(airports, weights) if r.get("ranked")]
        return order[0] if order else None, order[:SENSITIVITY_TOP_N]

    base_first, base_top = top(TERM_WEIGHTS)
    changes = []
    for term, w in TERM_WEIGHTS.items():
        for delta in (SENSITIVITY_STEP, -SENSITIVITY_STEP):
            new_w = max(0.0, w + delta)
            rest = sum(v for t, v in TERM_WEIGHTS.items() if t != term)
            trial = {t: (new_w if t == term else v * (1 - new_w) / rest)
                     for t, v in TERM_WEIGHTS.items()}
            first, top_order = top(trial)
            if first != base_first or set(top_order) != set(base_top):
                changes.append({"term": term, "weight": round(new_w, 3),
                                "first": first, "top_in_rank_order": top_order})
    return {
        "first": base_first,
        "top_in_rank_order": base_top,
        "stable": not changes,
        "step": SENSITIVITY_STEP,
        "changes": changes,
    }
