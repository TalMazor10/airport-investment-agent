# Design

## Data

### Sources

All data comes from the US Bureau of Transportation Statistics (BTS), the US Census Bureau and
OurAirports. US carriers are required by law to report traffic to the Department of
Transportation, so the record is complete, public and free of licence restrictions. Commercial
flight trackers were considered and rejected: they are paid, licence-bound, and built for live
aircraft positions rather than multi-year history.

| Source | Provides | Access |
|---|---|---|
| BTS On-Time Performance | One record per domestic flight by carriers above the BTS revenue threshold: delay minutes by cause, cancellations, departure hour, metropolitan market | Monthly archive files |
| BTS T-100 Segment (All Carriers) | Every route flown by every carrier, domestic and international, passenger and freighter, with distance and aircraft configuration | TranStats download form |
| BTS T-100 Segment Summary by Origin Airport | Passengers, seats and departures per airport per month | Open data API, `r495-tyji` |
| US Census Population Estimates | Annual population per metropolitan area | Flat files |
| OurAirports | Airport names, cities and coordinates | CSV |

`prepare_data.py` downloads these sources once, aggregates them to rows per airport per month,
and writes `data/airports.db`. The database is committed to the repository, so the application
runs with no credentials and no download step.

### Coverage window: calendar years 2016 to 2025

The sources end in different months:

| Source | Last month published |
|---|---|
| On-Time Performance | July 2026 |
| T-100 Segment (All Carriers) | June 2026 |
| T-100 airport summary | April 2026 |
| Census population | 2024 |

A partial year compared against a full one shows every airport in decline, so the window ends at
2025, the last calendar year complete in every flight source. Six months of 2026 carry little
weight in a decision on infrastructure with a ten-year horizon.

The pandemic years are handled differently. 2020 and 2021 are complete data that is
unrepresentative of underlying demand, so discounting them is an analytical judgment. They are
stored in full and down-weighted month by month in the scoring module, where the weights are
visible and adjustable. 2026 is removed at build time because it is incomplete, which is a
coverage fact rather than a judgment. The cutoff remains a build parameter, `--years`.

Census population ends in 2024. Metropolitan areas grow at roughly one percent a year, so 2024
population stands in for 2025. This is a stated assumption.

### Flight scope: passenger and freighter

The scope widened during design and changed the data layer twice.

1. **Passenger flights only.** The first reading of the brief centred on terminal expansion, and
   freighters were excluded as irrelevant to passenger terminals.
2. **Airside included.** The brief's goal is "increased flight and passenger capacity". Flight
   capacity is airside: towers, taxiways and runways are renovations too, as are ground
   transport and cargo handling. All flights are therefore in scope.
3. **A mixed-population error.** The first implementation took domestic flights from On-Time
   Performance, which excludes freighters, and international flights from a BTS international
   dataset, which includes them. Anchorage came out at 56.5% long haul. Its largest international
   operators were Atlas, UPS, FedEx and Kalitta, and it carried only 35,359 international
   passengers in 2025. The figure mixed two populations and described neither.
4. **One source for every flight.** BTS T-100 Segment (All Carriers) records every route by every
   carrier with its aircraft configuration. It replaced both inputs. Every long-haul figure now
   draws its numerator and denominator from the same population, and passenger and freighter
   traffic are reported separately.

| Anchorage, 2025 | Long-haul share |
|---|---|
| All flights | 41.7% |
| Passenger aircraft | 8.2% |
| Freighters | 68.3% |

Anchorage is a long-haul freighter hub and a short-haul passenger airport.

On-Time Performance is retained for what only it provides: delay causes, cancellations and
departure hour. These cover domestic flights by major carriers only. The coverage is consistent
across airports, so comparisons between airports hold, but it is not any airport's complete
picture.

### Long haul: 2,700 statute miles

Long haul conventionally means six hours of flight or more. It is measured here as a route
distance of 2,700 statute miles or more, which approximates six hours of block time at typical
jet speeds. A medium band from 1,400 miles is reported alongside, because the threshold is a
convention and a single percentage hides how much traffic sits just below it.

Measuring six hours directly was tested and rejected. T-100 publishes ramp-to-ramp minutes, but
only US carriers report them. Foreign carriers record zero.

| Airport, 2025 | By distance, 2,700 miles | By recorded time, 6 hours |
|---|---|---|
| ANC | 41.7% | 22.5% |
| JFK | 24.6% | 17.9% |
| SFO | 16.1% | 5.5% |
| LAX | 13.7% | 2.8% |
| BOS | 10.4% | 10.0% |

Of the 37,592 LAX departures on routes of 2,700 miles or more, 29,746 (79%) carry no recorded
flight time, and every one of them is operated by a foreign carrier. A time threshold would have
classified most international long-haul flights as short haul without raising any error.
Distance is published for every segment regardless of carrier.

### Validation

Four defects surfaced during development, and none of them raised an error. International
departures were counted twice because the source reports both directions of each route. Paged API
queries without a sort order silently dropped two thirds of the international traffic at large
airports. The Anchorage long-haul figure mixed freighter and passenger populations. Flight time
is missing for foreign carriers. Each was found by reconciling one figure against an independent
source, and the test suite makes that reconciliation permanent.

`tests/test_data.py` runs 57 checks against the built database (`pytest -v`).

| Group | Checks |
|---|---|
| Structure | expected tables present and retired tables absent; every table covers exactly 2016 to 2025, population to 2024; no missing months at the airports named in the brief |
| Consistency | distance bands sum to departures; no negative counts; cancellations never exceed flights; every airport row carries a metropolitan market and a state |
| Independent sources | route-level totals against the T-100 airport summary, nationally and per airport; stored T-100 row count against the count BTS publishes; On-Time flights never exceed T-100 departures |
| Known answers | 2025 passengers at LAX, SFO, SNA and ANC; the 2020 collapse, 36.8% below 2019; LAX share of Los Angeles metro flights, 68.9% in 2016 and 61.6% in 2025; the Anchorage long-haul split |
| Reported | Census match coverage; On-Time coverage of T-100 |

Results of the reconciliation checks on the 2016 to 2025 build:

| Check | Tolerance | Result |
|---|---|---|
| Route totals against airport totals, national, each year | 0.1% | 2016 to 2023 identical; largest difference 0.006%, in 2025 |
| The same, per airport-year with 1,000 or more departures | 1% | 4,338 of 4,356 identical; largest difference 0.099% |
| Stored T-100 rows against the published count | exact | equal |
| On-Time departed flights against T-100 departures | never greater | highest ratio 1.00, at airports served by one carrier that reports every flight |

Airport-years below 1,000 departures, 8,767 of them carrying 1.7% of all departures, are left out
of the per-airport reconciliation, where small counts make percentages unstable. They remain in
the database. The 1% tolerance leaves room for routine BTS revisions without masking a real
defect; the largest observed difference is a tenth of it.

**Coverage.** Census population matches 238 of 366 airport markets, covering 96.4% of flights. In
2025, On-Time Performance covers 69.1% of departures at LAX, 75.4% at SFO, 86.9% at SNA and
21.2% at ANC. The Anchorage figure is low because most of its traffic is freighters and small
Alaskan carriers, so delay figures there describe a minority of its flights.

### Known data limitations and future improvements

The following were found during validation. None changes the answer to the questions the system
is built for, so each is recorded rather than resolved; each is a candidate for the next iteration
once real usage shows which matters most.

- **Census matching is by place name, and partial.** 38 airports above 100,000 passengers, about
  3% of passengers, have no population reference. Most are in Puerto Rico, Guam, the Virgin
  Islands, Hawaii's neighbour islands or resort towns, which Census does not classify as
  metropolitan areas; local population is a weak reference for visitor-driven airports in any
  case. Four are matching misses: West Palm Beach, Palm Springs, Fort Walton Beach and Redmond.
  Improvement: resolve the remaining names with a language model at build time, review the
  result, and write it to the database once, so the query path stays deterministic.
- **West Palm Beach (PBI) is absent from the OurAirports IATA list**, so it has no name or city
  in the `airports` table.
- **Two airports above the ranking threshold, New Haven and Wilmington DE, have no metropolitan
  market id**, because no On-Time reporting carrier serves them. T-100 Segment publishes a city
  market id for every airport and could supply it.
- **Delay data covers domestic flights by major carriers only.** At Anchorage this is 21% of
  departures.
- **T-100 Segment is obtained through a web form**, which can change without notice. Every
  download is cached, so a rebuild from cache does not depend on it.

## Scoring

### What the score measures

The goal is to find airports where renovation would be most profitable through increased flight
and passenger capacity. The score combines evidence of **pressure**, meaning demand the airport
struggles to serve, with **scale**, meaning how much traffic an improvement would affect.
Passenger volume stands in for revenue; no financial data is used.

| Term | Measure | Weight |
|---|---|---|
| Growth gap | Metropolitan population growth minus passenger growth, per year, 2016 to 2025 | 0.20 |
| Spillover | Trend in the airport's share of its metropolitan area's passengers | 0.20 |
| NAS delay | National Aviation System delay minutes per arriving flight, 2023 to 2025 | 0.30 |
| Scale | Passengers in 2025 | 0.30 |

All arithmetic is in `scoring.py`, in ordinary code. The language model selects which airports to
score and explains the result, but never produces a number, so the same question always returns
the same figures.

**Load factor was considered and rejected.** Across the 25 largest US airports it spans only 76% to
83%, because airlines manage it to a revenue target, and it ranks Nashville, one of the fastest
growing markets in the country, last.

### How each term is computed

- **Growth rates** are the slope of a trend line through the 120 monthly passenger totals, on a
  log scale so the slope reads as percent per year, with the seasonal pattern removed. Pandemic
  months count for less: March 2020 at half weight, April and May 2020 at 0.05, rising through
  2021 to full weight in 2022. A trend needs 36 months of data, counted by weight.
- **Growth gap** compares that rate with the metropolitan area's population trend. Census
  population ends in 2024. Air travel does not scale linearly with population, so the gap is a
  relative signal between airports, not an absolute measure of unmet demand.
- **Spillover** tracks the airport's share of all passengers in its metropolitan area, using the
  BTS city market grouping rather than a distance radius. A radius large enough to join Los
  Angeles and Santa Ana would also join Los Angeles and San Diego.
- **NAS delay** is the delay category BTS assigns to airport operations, traffic volume and air
  traffic control, the delay that infrastructure can reduce. It is attributed to the **arrival**
  airport. BTS records delay causes on arrival, and traffic management acts at the constrained
  end: when fog cuts San Francisco's arrival rate, flights bound for SFO wait at their origins
  and the delay is recorded on arrival into SFO. Counting it at the origin would charge SFO's
  congestion to dozens of other airports. **Tested:** the seven airports where the FAA limits
  scheduled traffic for capacity (JFK, LaGuardia, Reagan National, O'Hare, LAX, Newark, SFO)
  average the 90th percentile under arrival attribution and the 70th under departure attribution.

### From measures to a score

- **Percentiles against the whole country.** Each term becomes a percentile against every US
  airport above 100,000 passengers in 2025, 233 airports. An airport's score is therefore the
  same whichever airports a question names. Ranking within the requested group alone would give
  one of two airports 100 and the other 0 on every term, and would call the best of a weak region
  "strong". Answers show both: the national score and the rank within the group.
- **The score** is the weighted mean of the term percentiles, from 0 to 100.
- **Missing terms** are left out and the remaining weights rescaled, and the answer states which
  term is missing and why. The main case is an airport with no Census population match.
- **An airport alone in its metropolitan area** receives the neutral spillover percentile, 50.
  There is no neighbouring airport for traffic to move to, so spillover neither raises nor lowers
  it, and the other terms decide.
- **Peak gate.** An airport carrying less than 95% of the passengers of its own busiest year has
  already handled more traffic than it carries now. A plateau or a share loss there indicates lost
  demand, not a ceiling, so growth gap and spillover take the neutral percentile. Raw values are
  still reported.
- **Computed live.** Scores are computed from the database when first requested and held in memory.
  Changing a weight takes effect on the next question, with no rebuild.

### The first model rewarded decline

The first version of the score passed every test and still produced wrong answers. Checking its
output against airports with known histories exposed the problem:

| | First model | Current model |
|---|---|---|
| New England, top three | Providence, Bradley, **Manchester** | **Boston**, Bradley, Providence |
| Bay Area, first | **Oakland** | **SFO** |
| Akron-Canton, national rank | **10th** | 82nd |

Manchester's passengers fall 5.4% a year, Oakland's 3.3%, Akron-Canton's 5.7%. The growth gap
widens whenever passengers fall, so an airport that demand has abandoned scored like one that is
full. Spillover had the same weakness. Two changes corrected it: the peak gate, and a larger NAS
delay weight, raised from 0.15 to 0.30, because delay is what separates a full airport from an
abandoned one.

It also changed one interpretation. LAX carries 85% of its 2019 passengers, so its loss of share to
Santa Ana, Burbank and Long Beach is not evidence that LAX is full: passengers chose other airports
while LAX still had room. The share loss is reported; it no longer raises the score.

### Sensitivity

The weights are judgments. Every ranking is recomputed with each weight moved up and down by 0.05,
the others rescaled, and the answer reports whether the first place or the top three change. A
ranking that survives is stated as stable; one that does not is reported with the change that
moves it. The New England ranking is stable under every such change.

`tests/test_scoring.py` runs 24 checks on the arithmetic, the scoring rules and the design
decisions above, including the FAA attribution test and the corrected answers for New England
and the Bay Area.
