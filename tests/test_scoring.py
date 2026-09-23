"""
Tests for scoring.py.

Mechanical checks on the arithmetic and the scoring rules, followed by two
checks on the design itself: that attributing NAS delay to the arrival
airport ranks the FAA's own capacity-constrained airports higher than
attributing it to the departure airport, and a report of how adding NAS delay
to the score moves airports.

    pytest -v tests/test_scoring.py
    pytest -v -s tests/test_scoring.py     # also print the reports
"""

import math

import pytest

import scoring

NEW_ENGLAND = ["BOS", "PVD", "MHT", "BDL", "PWM", "BTV", "ORH", "BGR"]

# Airports where the FAA limits scheduled traffic because of capacity,
# per faa.gov Slot Administration, confirmed 2026-09-23.
FAA_SLOT_CONTROLLED = ["JFK", "LGA", "DCA"]
FAA_SCHEDULE_FACILITATED = ["ORD", "LAX", "EWR", "SFO"]


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------

def test_trend_slope_recovers_a_known_growth_rate():
    rows = [(y, m, 1000 * 1.05 ** ((y - 2016) + (m - 1) / 12))
            for y in range(2016, 2026) for m in range(1, 13)
            if (y, m) not in scoring.MONTH_WEIGHTS]
    assert scoring.trend_slope(rows, log=True) == pytest.approx(0.05, abs=1e-9)


def test_trend_slope_ignores_a_regular_seasonal_pattern():
    season = {1: 0.8, 7: 1.3}
    rows = [(y, m, 1000 * 1.03 ** (y - 2016 + (m - 1) / 12) * season.get(m, 1.0))
            for y in range(2016, 2026) for m in range(1, 13)]
    assert scoring.trend_slope(rows, log=True) == pytest.approx(0.03, abs=1e-9)


def test_trend_slope_needs_enough_months():
    rows = [(2025, m, 100.0) for m in range(1, 13)]
    assert scoring.trend_slope(rows, log=True) is None


def test_pandemic_months_count_by_their_weight_toward_the_minimum():
    """36 calendar months spanning 2020 to 2022 weigh less than 36 full months."""
    rows = [(y, m, 100.0 + m) for y in (2020, 2021, 2022) for m in range(1, 13)]
    assert len(rows) == scoring.MIN_MONTHS_FOR_TREND
    assert scoring.trend_slope(rows, log=True) is None


def test_percentiles_run_from_0_to_100_with_ties_counted_half():
    p = scoring.percentiles({"a": 1, "b": 2, "c": 2, "d": 3})
    assert p["a"] == 0 and p["d"] == 100
    assert p["b"] == p["c"] == pytest.approx(50)


# ---------------------------------------------------------------------------
# Scoring rules
# ---------------------------------------------------------------------------

def test_term_weights_sum_to_one():
    assert sum(scoring.TERM_WEIGHTS.values()) == pytest.approx(1.0)


def test_ranked_pool_is_every_airport_above_the_threshold():
    table = scoring.national_table("arrival")
    ranked = [a for a, r in table.items() if r["ranked"]]
    assert all(table[a]["raw"]["scale"] >= scoring.MIN_PASSENGERS for a in ranked)
    assert all(r["raw"]["scale"] < scoring.MIN_PASSENGERS for r in table.values() if not r["ranked"])


def test_every_ranked_score_lies_between_0_and_100():
    everyone = [a for a, r in scoring.national_table("arrival").items() if r["ranked"]]
    for r in scoring.score(everyone):
        assert 0 <= r["score"] <= 100


def test_a_score_does_not_depend_on_the_group_it_is_asked_with():
    alone = scoring.score(["BOS"])[0]["score"]
    in_region = next(r for r in scoring.score(NEW_ENGLAND) if r["airport"] == "BOS")["score"]
    with_lax = next(r for r in scoring.score(["LAX", "BOS"]) if r["airport"] == "BOS")["score"]
    assert alone == in_region == with_lax


def test_scores_are_reproducible():
    assert scoring.score(NEW_ENGLAND) == scoring.score(NEW_ENGLAND)


def test_group_ranks_follow_scores():
    ranked = [r for r in scoring.score(NEW_ENGLAND) if r.get("ranked")]
    assert [r["group_rank"] for r in ranked] == list(range(1, len(ranked) + 1))
    assert [r["score"] for r in ranked] == sorted((r["score"] for r in ranked), reverse=True)


def test_effective_weights_always_sum_to_one():
    everyone = [a for a, r in scoring.national_table("arrival").items() if r["ranked"]]
    for r in scoring.score(everyone):
        assert sum(t["weight"] for t in r["terms"].values()) == pytest.approx(1.0)


def test_an_airport_alone_in_its_metro_gets_the_neutral_spillover_score():
    slc = scoring.score(["SLC"])[0]
    assert slc["terms"]["spillover"]["percentile"] == scoring.NEUTRAL_PERCENTILE
    assert "neutral" in slc["notes"]["spillover"]


def test_an_airport_below_its_peak_gets_neutral_growth_terms():
    """Manchester carries well under 95% of its peak-year passengers: its falling
    traffic is lost demand, not a ceiling. Raw values are still reported."""
    mht = scoring.score(["MHT"])[0]
    assert mht["context"]["share_of_peak"] < scoring.PEAK_GATE
    for term in ("growth_gap", "spillover"):
        assert mht["terms"][term]["percentile"] == scoring.NEUTRAL_PERCENTILE
        assert "peak" in mht["notes"][term]
        assert mht["terms"][term]["raw"] is not None


def test_a_missing_term_is_left_out_and_explained():
    sju = scoring.score(["SJU"])[0]
    assert sju["terms"]["growth_gap"]["weight"] == 0
    assert "Census" in sju["notes"]["growth_gap"]


def test_a_small_airport_is_reported_but_not_ranked():
    table = scoring.national_table("arrival")
    small = next(a for a, r in table.items() if not r["ranked"] and r["raw"]["scale"] > 0)
    r = scoring.score([small])[0]
    assert r["ranked"] is False and "not ranked" in r["notes"]["ranking"]


def test_an_unknown_code_is_reported_not_raised():
    assert "error" in scoring.score(["ZZZ"])[0]


def test_sensitivity_reports_a_result_for_a_group():
    s = scoring.sensitivity(NEW_ENGLAND)
    assert s["first"] in NEW_ENGLAND and isinstance(s["stable"], bool)


# ---------------------------------------------------------------------------
# Design checks
# ---------------------------------------------------------------------------

def test_lax_loses_share_of_its_metro_passengers():
    """The spillover fingerprint behind the LA and Santa Ana comparison."""
    assert scoring.score(["LAX"])[0]["terms"]["spillover"]["raw"] < 0


def test_boston_ranks_first_in_new_england():
    """The largest airport in the region, at record traffic and with the most
    NAS delay. The first scoring model ranked it fourth, below Manchester,
    whose traffic was falling; the peak gate and the NAS weight corrected it."""
    assert scoring.score(NEW_ENGLAND)[0]["airport"] == "BOS"


def test_sfo_ranks_above_oakland_and_san_jose():
    """Oakland and San Jose carry about two thirds of their peak traffic and
    have little delay. The first scoring model ranked Oakland above SFO."""
    assert scoring.score(["SFO", "OAK", "SJC"])[0]["airport"] == "SFO"


def test_arrival_attribution_ranks_faa_constrained_airports_higher():
    """NAS delay counted at the arrival airport should place the airports the
    FAA itself limits for capacity higher than counting it at departure."""
    faa = FAA_SLOT_CONTROLLED + FAA_SCHEDULE_FACILITATED

    def mean_nas_percentile(attribution):
        table = scoring.national_table(attribution)
        return sum(table[a]["percentile"]["nas_delay"] for a in faa) / len(faa)

    arrival, departure = mean_nas_percentile("arrival"), mean_nas_percentile("departure")
    print(f"\nFAA-constrained airports, mean NAS percentile: arrival {arrival:.1f}, "
          f"departure {departure:.1f}")
    assert arrival > departure


def test_report_effect_of_adding_nas_delay():
    """Reported, not asserted. The NAS term stays if it raises airports whose
    traffic is flat and delayed and lowers those that are flat and not delayed."""
    everyone = [a for a, r in scoring.national_table("arrival").items() if r["ranked"]]
    without = {t: w for t, w in scoring.TERM_WEIGHTS.items() if t != "nas_delay"}
    total = sum(without.values())
    without = {t: w / total for t, w in without.items()}
    before = {r["airport"]: r["score"] for r in scoring.score(everyone, without)}
    after = {r["airport"]: r for r in scoring.score(everyone)}
    moves = sorted(((after[a]["score"] - before[a], a) for a in before), reverse=True)

    def line(delta, a):
        t = after[a]["terms"]
        gap = t["growth_gap"]["percentile"]
        return (f"  {a}  {delta:+5.1f}  growth gap pct {'n/a' if gap is None else f'{gap:5.1f}'}"
                f"  NAS pct {t['nas_delay']['percentile']:5.1f}  "
                f"NAS min/flight {t['nas_delay']['raw']:.1f}")

    print("\nLargest rises when NAS delay is added:")
    for delta, a in moves[:8]:
        print(line(delta, a))
    print("Largest falls:")
    for delta, a in moves[-8:]:
        print(line(delta, a))


def test_report_national_top_15():
    everyone = [a for a, r in scoring.national_table("arrival").items() if r["ranked"]]
    print("\nNational top 15:")
    for r in scoring.score(everyone)[:15]:
        t = r["terms"]
        fmt = lambda v: "  n/a" if v is None else f"{v:5.1f}"
        print(f"  {r['national_rank']:>3} {r['airport']}  score {r['score']:5.1f}  "
              f"gap {fmt(t['growth_gap']['percentile'])}  spill {fmt(t['spillover']['percentile'])}  "
              f"nas {fmt(t['nas_delay']['percentile'])}  scale {fmt(t['scale']['percentile'])}")
    assert not any(math.isnan(r["score"]) for r in scoring.score(everyone))


def test_sensitivity_lists_the_top_positions_in_rank_order():
    ranked = [r["airport"] for r in scoring.score(NEW_ENGLAND) if r.get("ranked")]
    assert scoring.sensitivity(NEW_ENGLAND)["top_in_rank_order"] == ranked[:3]


def test_population_growth_ignores_the_2020_census_rebase():
    """A series growing 1% a year that steps up 4% at 2020, as New York's does
    when Census switches to 2020-based estimates, still reads as 1% a year."""
    points = [(y, 1_000_000 * 1.01 ** (y - 2016) * (1.04 if y >= 2020 else 1.0))
              for y in range(2016, 2025)]
    assert scoring.annual_growth(points) == pytest.approx(0.01, abs=1e-9)
