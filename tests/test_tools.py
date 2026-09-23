"""
Tests for tools.py: every tool returns data the model can read, reports
problems as data rather than raising, and gives the expected facts for the
airports in the brief.

    pytest -v tests/test_tools.py
"""

import json

import pytest

import tools

NEW_ENGLAND = ["ME", "NH", "VT", "MA", "RI", "CT"]


def test_every_definition_has_a_matching_function():
    assert {d["name"] for d in tools.TOOL_DEFINITIONS} == set(tools.TOOLS)


@pytest.mark.parametrize("name, arguments", [
    ("find_airports", {"states": NEW_ENGLAND}),
    ("score_airports", {"airports": ["BOS", "PVD", "BDL"]}),
    ("airport_profile", {"airport": "SFO"}),
    ("haul_mix", {"airport": "ANC"}),
])
def test_every_tool_returns_json_the_model_can_read(name, arguments):
    result = tools.run_tool(name, arguments)
    assert "error" not in result
    json.dumps(result)


def test_an_unknown_tool_is_reported_not_raised():
    assert "error" in tools.run_tool("book_flight", {})


def test_bad_arguments_are_reported_not_raised():
    assert "error" in tools.run_tool("haul_mix", {"code": "ANC"})


def test_find_airports_echoes_the_states_it_searched():
    result = tools.find_airports(NEW_ENGLAND)
    assert result["states_searched"] == sorted(NEW_ENGLAND)
    codes = [a["airport"] for a in result["airports"]]
    assert codes[0] == "BOS"
    assert {"PVD", "BDL", "MHT", "PWM", "BTV"} <= set(codes)


def test_find_airports_omits_small_airports_by_default_but_counts_them():
    default = tools.find_airports(["MA"])
    everything = tools.find_airports(["MA"], include_small=True)
    assert all(a["ranked"] for a in default["airports"])
    assert len(everything["airports"]) == len(default["airports"]) + default["airports_below_ranking_threshold"]


def test_find_airports_includes_airports_missing_from_ourairports():
    """West Palm Beach has no OurAirports entry but must still be found in Florida."""
    assert "PBI" in [a["airport"] for a in tools.find_airports(["FL"])["airports"]]


def test_find_airports_flags_unrecognised_codes():
    assert tools.find_airports(["ZZ"])["unrecognised_state_codes"] == ["ZZ"]


def test_score_airports_includes_sensitivity_for_a_group():
    result = tools.score_airports(["BOS", "PVD", "BDL"])
    assert "sensitivity" in result
    assert result["airports"][0]["rank_in_group"] == 1


def test_airport_profile_covers_every_year():
    years = [y["year"] for y in tools.airport_profile("SFO")["yearly"]]
    assert years == list(range(2016, 2026))


def test_airport_profile_lists_metro_neighbours():
    neighbours = {n["airport"] for n in tools.airport_profile("LAX")["metro_neighbours"]}
    assert {"SNA", "BUR", "LGB", "ONT"} <= neighbours


def test_haul_mix_gives_the_anchorage_split():
    result = tools.haul_mix("ANC")
    assert result["all_flights"]["long_haul_pct"] == pytest.approx(41.7, abs=0.05)
    assert result["passenger_aircraft"]["long_haul_pct"] == pytest.approx(8.2, abs=0.05)
    assert result["cargo_aircraft"]["long_haul_pct"] == pytest.approx(68.3, abs=0.05)


def test_each_term_states_its_direction_in_words():
    """JFK loses share of its metropolitan area, and its passengers grow faster
    than the area's population. The model misread both from signed values in the first live
    test; the reading sentence states the direction for it."""
    jfk = tools.score_airports(["JFK"])["airports"][0]["terms"]
    assert jfk["spillover"]["reading"].startswith("Losing")
    assert jfk["growth_gap"]["reading"].startswith("Passengers grew")
    dfw = tools.score_airports(["DFW"])["airports"][0]["terms"]
    assert dfw["growth_gap"]["reading"].startswith("Passengers grew")


def test_sensitivity_lists_the_top_positions_in_rank_order_with_a_summary():
    result = tools.score_airports(["JFK", "LGA", "EWR"])
    order = [a["airport"] for a in result["airports"] if a.get("ranked")]
    assert result["sensitivity"]["top_in_rank_order"] == order[:3]
    assert result["sensitivity"]["summary"].startswith(("Stable", "Not stable"))


def test_haul_mix_returns_each_aircraft_type_as_a_share_of_departures():
    """Miami cargo aircraft are 13.5% of departures. The model computed this itself
    in the first live test; the tool now returns it."""
    result = tools.haul_mix("MIA")
    assert result["cargo_aircraft"]["share_of_all_departures_pct"] == pytest.approx(13.5, abs=0.05)
    assert result["all_flights"]["share_of_all_departures_pct"] == 100.0


def test_the_profile_sets_delay_against_every_ranked_airport():
    """SFO's delay is near the top nationally. Without this comparison the LLM
    judged 5.5 minutes 'moderate' against SFO's own worse pre-pandemic years."""
    comparison = tools.airport_profile("SFO")["nas_delay_national_comparison"]
    assert comparison["national_percentile"] >= 95
    assert comparison["minutes_per_arrival_2023_2025"] > comparison["national_median_minutes"]


def test_percentiles_are_whole_numbers():
    terms = tools.score_airports(["BOS", "BDL"])["airports"][0]["terms"]
    assert all(isinstance(t["percentile"], int) for t in terms.values())
