# Design

## 1. Summary

The Airport Investment Agent helps analysts at a firm that invests in US airport modernisation
find airports where renovation would be most profitable. An LLM interprets the analyst's question
and calls four tools; the tools read a local database built from ten years of US government
aviation and population data; a deterministic scoring module ranks the airports; the LLM explains
the result and its assumptions.

The central choice: **the LLM interprets and explains; code computes.** Every figure comes from
deterministic code, so the same airport always receives the same score and every ranking traces
to a formula with visible inputs. The public data sources are queried when the database is built,
not while the agent answers. The LLM provider is a setting: any provider that offers the
industry-standard chat completions interface works.

### Terms used

| Term | Meaning |
|---|---|
| API | Application programming interface: a service that programs query directly |
| BTS | Bureau of Transportation Statistics, the US Department of Transportation's statistics agency |
| T-100 | The monthly traffic report every airline must file: flights, passengers and cargo on every route |
| On-Time Performance | The BTS record of every domestic flight by major airlines, with delay minutes by cause |
| NAS delay | National Aviation System delay: the BTS category for traffic volume, airport operations, air traffic control and non-extreme weather |
| FAA, TSA | Federal Aviation Administration; Transportation Security Administration |
| Metropolitan area | A city and its region. Census publishes its population; BTS groups its airports into one market |
| LLM | Large language model |
| Chat completions interface | The request format, originally OpenAI's, that most LLM providers accept |
| Tool calling | An LLM requesting that a named function be run with given inputs |
| Load factor | The share of seats sold |
| Percentile | The share of other airports an airport exceeds on a measure, 0 to 100 |

## 2. Architecture and where AI is used

```
question -> app.py -> agent.py  <-- conversation -->  LLM (any provider)
                         |
                         | tool calls chosen by the LLM
                         v
                      tools.py -> scoring.py -> data/airports.db  <- prepare_data.py (offline)
```

| File | Role |
|---|---|
| `prepare_data.py` | Downloads the public sources once and aggregates them into `data/airports.db`, which holds observations only |
| `scoring.py` | Every analytical judgment: weights, percentiles, peak gate, sensitivity |
| `tools.py` | Find airports in a region, score a group, profile one airport, flight distance mix |
| `agent.py` | A short hand-written loop: send the conversation to the LLM, run the tools it requests, return the results, repeat until it answers (at most ten rounds) |
| `app.py`, `voice.py` | Chat page with voice dictation |

**The LLM does three things:** it interprets the question (which states make up New England, that
"LA" means LAX), chooses which tools to call, and explains the result.

**It never produces a number.** It reaches the data only through the tools, its instructions forbid
estimating figures, and the tools return every derived figure, including shares, percentages and
the direction of each score term in words ("Losing 0.29 percentage points of its metropolitan
area's passenger share a year"). Every answer lists the tool calls behind it, so the LLM's
interpretation, such as the states it chose, can be checked.

## 3. How the brief was interpreted

The brief invited questions where its definitions were unclear. These definitions were chosen to
be measurable from public data; they are stated openly, including in the answers, and are open to
correction.

| Term | Definition used | Why |
|---|---|---|
| "Most profitable" | Pressure on the airport, combined with its scale; passenger volume stands in for revenue | Airport financial data is not public, and an improvement is worth more at a larger airport |
| "Renovation" | Terminal and airfield | The goal names flight capacity as well as passenger capacity |
| "Congestion" | Airfield pressure, scored as NAS delay per arriving flight. Terminal pressure, described but not scored: passengers against the airport's own peak year, busiest departure hours | Load factor was rejected (section 7.1) |
| "Unmet demand" | Inferred from fingerprints: traffic flat while the region grows, traffic moving to neighbouring airports, traffic delay, more passengers per flight on flat departures | Turned-away demand is never recorded |
| "Long haul" | A route of 2,700 statute miles or more, about six hours | Flight time is not reported by foreign airlines |
| "New England", "LA" | Expanded by the LLM into states or airport codes, shown to the user | Hardcoding regions would limit the agent to known ones |
| Scope | US airports, 2016 to 2025, every airline. Cargo aircraft count in flight and long-haul figures; the score uses passengers and delay | 2025 is the last year complete in every source |

## 4. Data

US airlines must report their traffic to the government, so the regulator's record is complete,
public and free to use. Commercial flight trackers were rejected: paid, licence-bound, and built
for live positions rather than years of history.

| Source | Provides |
|---|---|
| BTS T-100 Segment (All Carriers) | Every route flown by every airline, with distance and aircraft type |
| BTS T-100 airport summary (open data API) | Passengers, seats and departures per airport per month |
| BTS On-Time Performance | Delay minutes by cause, cancellations, departure hours |
| US Census population estimates | Annual population per metropolitan area |
| OurAirports (open database) | Airport names and locations |

About 3 GB of raw files become a 25 MB database committed to the repository. The window is 2016 to
2025: the sources end in different months of 2026, and a partial year compared with a full one
makes every airport look in decline.

**One source for every flight.** Long-haul shares come from T-100 Segment alone, so each
percentage takes its numerator and denominator from the same flights. Anchorage shows why this
matters: 42% of its flights are long haul, but only 8% of its passenger flights are. The long
flights are cargo planes to and from Asia, and a single percentage would describe neither kind of
traffic.

## 5. Scoring methodology

| Term | Measure | Weight |
|---|---|---|
| Growth gap | The metropolitan area's population growth minus the airport's passenger growth, per year | 0.20 |
| Spillover | Trend in the airport's share of its metropolitan area's passengers; losing share counts as pressure | 0.20 |
| NAS delay | NAS delay minutes per arriving flight, 2023 to 2025 | 0.30 |
| Scale | Passengers in 2025 | 0.30 |

- **Growth** is the slope of a trend line through all 120 months on a log scale, seasonal pattern
  removed, rather than a comparison of two years. Pandemic months count for less (March 2020 half,
  April and May 2020 at 0.05, rising to full weight by 2022), including toward the 36 months a trend
  needs. Census population comes in two series, before and after its 2020 recalibration, and growth
  is measured within each so the recalibration is not read as growth.
- **Metropolitan areas** are BTS markets, not a distance radius: a radius joining Los Angeles and
  Santa Ana would also join Los Angeles and San Diego. Each market is matched to its Census area by
  place name.
- **NAS delay** is mostly the delay infrastructure can reduce, though it includes ordinary weather.
  It is counted at the **arrival** airport: when fog cuts San Francisco's arrival rate, inbound
  flights wait at their origins and the delay is recorded on arrival at San Francisco. Tested
  against the FAA's own judgment: the seven airports where the FAA restricts (JFK, LaGuardia,
  Reagan National) or reviews (O'Hare, LAX, Newark, SFO) airline schedules for capacity average the
  90th percentile of NAS delay when it is counted at arrival, and the 70th at departure.
- **Percentiles are national**, against every US airport above 100,000 passengers (233), so an
  airport's score does not depend on which others a question names. Answers show the national
  score and the rank within the group asked about. The score is the weighted mean, 0 to 100.
- **Missing terms** are left out, the weights rescaled, and the reason stated. An airport alone in
  its metropolitan area gets the neutral spillover percentile, 50.
- **Peak gate.** An airport below 95% of its own busiest year's passengers has already handled more,
  so a plateau or loss of share there is not counted as evidence of a ceiling; its growth terms take
  the neutral percentile, and delay can still show strain. 61 of the 233 fall under this line.
- **Sensitivity.** Every ranking is recomputed with each weight moved by 0.05, and the answer states
  whether first place or the top three change.

## 6. Answers to the four questions

Figures as the tools return them.

- **New England, terminal expansion.** Boston first (72.4, 19th nationally), then Bradley (68.3) and
  Providence (61.7), stable under every weight change. Boston has the region's heaviest airfield
  delay (5.1 minutes per arriving flight, 96th percentile) and its largest traffic.
- **LA and Santa Ana, congestion.** LAX has more airfield delay (2.1 against 1.6 minutes per
  arrival). Santa Ana has the tighter terminal: it runs at 97% of its busiest year, LAX at 85% of its
  2019 peak. LAX is losing 0.9 percentage points of the region's passengers a year to its neighbours,
  but since it is below its own peak, that reads as passengers choosing other airports rather than a
  full LAX.
- **Long haul from Anchorage.** 41.7% of all 2025 departures: 8.2% of passenger flights and 68.3% of
  cargo flights, which are 56% of the total.
- **Unmet demand at SFO, and why.** Inferred from its fingerprints: some of the heaviest airfield
  delay in the country (5.7 minutes per arrival, 99th percentile), and airlines fitting more
  passengers onto fewer flights, with departures down from 218,000 in 2018 to 190,000 in 2025 while
  passengers per departure rose from 129 to 139. That is what airlines do when they cannot add
  flights. SFO scores 78.4, fifth nationally.

## 7. What went wrong and what it taught

### 7.1 How the design evolved

| Question | First idea | What showed it was wrong | What replaced it |
|---|---|---|---|
| Where does the data come from? | Live flight trackers | Investors need years of trend, not today's positions; trackers are paid and licence-bound | The regulator's records, found by asking who is legally required to publish them |
| Who calculates the ranking? | The LLM | A ranking that changes between identical questions cannot back an investment or be audited | Deterministic code |
| How is congestion measured? | Load factor | Across the 25 largest airports it spans only 76% to 83%, and it ranked Nashville, a fast-growing market, last | NAS delay, and passengers against the airport's own peak |
| How is capacity known? | FAA capacity figures | The FAA site blocked automated downloads | A ceiling inferred from traffic: flat while the region grows, or lost to neighbours |
| How is spillover measured? | Neighbours' growth minus the airport's | Wrong whenever a whole region grows or shrinks together | The airport's share of its region's passengers over time |
| Which flights count? | Passenger flights only | Runways serve cargo aircraft too; mixing sources then caused the Anchorage error (7.2) | Every airline, one source, split by aircraft type |
| Does delay belong in the score? | No: it can reflect airline management | BTS splits delay by cause, and NAS delay is the part infrastructure can fix | Included, counted at the arrival airport |
| Against whom is an airport ranked? | The airports in the question | Two airports always score 0 and 100; a weak region still yields a "strong" candidate | Every airport nationally |
| What tools does the LLM get? | One per sample question | Could not answer a fifth question | Four tools, one per kind of request |
| Which LLM provider? | One provider's library | The firm's provider is unknown | The industry-standard interface |

### 7.2 Defects caught by verification

None raised an error; each produced a plausible number.

| Defect | How it was found | Fix | Lesson |
|---|---|---|---|
| International flights counted twice (the source lists both directions), then two thirds of LAX's lost by a page-by-page query without a fixed order | Totals did not match the T-100 airport summary | One source for every route | Reconcile against an independent source |
| Anchorage long haul at 56.5%, mixing cargo and passenger flights | Its main international airlines were Atlas, UPS, FedEx and Kalitta; it had 35,359 international passengers all year | Split by aircraft type | A percentage needs one population |
| **The first score rewarded decline:** Manchester, losing 5.4% of passengers a year, above Boston; Oakland above San Francisco | Reading answers against known airports, with every test passing | The peak gate and a larger NAS delay weight, after simulating four options | Tests prove code matches design, not that the design is right |
| Census population half-missing for renamed areas (45 airports, 42% of passengers), and the 2020 recalibration read as growth, reversing 49 of 231 trends | A live answer said Denver's data ended in 2019; a test failed after that fix | Join on area code; measure growth within each series | Check every record; a failing test after a fix can be the data revealing more |
| The LLM explained correct numbers wrongly: JFK "gaining" share it was losing; SFO's delay, at the 99th percentile nationally, called "moderate"; shares calculated itself | Reading answers against tool output | Directions written in words by code; national percentiles and shares returned by the tools | Give the LLM meaning, not raw values, and nothing to calculate. A rule that fixes one answer can break another, so every change is retested on all four questions |

## 8. Key tradeoffs

| Chosen | Gained | Given up |
|---|---|---|
| Build the data once and commit it | Instant answers; runs with only an LLM key | Freshness: a snapshot to December 2025 |
| Arithmetic in deterministic code | Reproducible, auditable figures | Rescoring under weights an analyst invents mid-conversation |
| National percentiles | Stable scores; meaningful two-airport comparisons | A purely regional view |
| A trend line through every month | One unusual year cannot decide | The simplicity of comparing two years |
| A hand-written agent loop | Every step visible and logged | Conveniences of ready-made helpers |
| The standard chat interface | Any LLM provider | Provider-specific features |
| Browser speech recognition | Nothing to install, no second key | Chrome and Edge only; audio processed by the browser maker |

## 9. Assumptions, uncertainty and scope

- Passenger volume stands in for revenue; no financial data is used.
- Unmet demand is inferred, never observed. Air travel does not scale linearly with population, so
  the growth gap is a relative signal.
- More passengers per flight partly reflects airlines retiring small regional jets everywhere.
- Delay covers domestic flights by major airlines only (69% of LAX's departures, 21% of
  Anchorage's) and includes ordinary weather.
- Census population ends in 2024 and stands in for 2025. 238 of 366 BTS markets match a Census area,
  covering 96.4% of flights; seven areas Census redefined in 2023 are scored without a growth gap.
- The weights are judgments, chosen partly by checking results against known airports, which risks
  tuning to expectations. Every ranking reports whether it survives a 0.05 change in any weight.
- The ranking correlates 0.74 with a ranking by passengers alone: size matters, but 8 of the top 20
  differ.
- Long haul is a convention; a medium band from 1,400 miles is reported alongside it.
- Data ends in December 2025. No forecasts; US airports only.

## 10. Validation

120 automated tests cover the data, the scoring, the tools, the agent loop (with a scripted
stand-in for the LLM) and the chat page. They need no API key and run on every push, on Python
3.10 and 3.14. The database is reconciled against independent sources:

| Check | Tolerance | Result |
|---|---|---|
| Route totals against the T-100 airport summary, nationally, each year | 0.1% | 2016 to 2023 identical; largest difference 0.006% |
| The same per airport and year, 1,000 departures or more | 1% | 4,338 of 4,356 identical; largest difference 0.099% |
| Stored rows against the count BTS publishes | exact | equal |

Twenty questions beyond the brief's four, covering unseen regions, follow-ups, forecasts and
out-of-scope requests, were run and read against the tool output. That review found the last two
defects in 7.2 and cut the median answer from 454 words to about 185. The application was also
tested by hand, including voice.

## 11. With more time

- A gradual peak discount instead of the hard 95% line, which SFO (94.4%) and LAX fall just under.
- Pressure multiplied by scale rather than added, so a large, uncongested airport cannot score on
  size alone.
- The investment side: construction cost, how US airports are financed, projects already funded.
- The FAA's published capacity figures, to test the inferred ceiling, and TSA checkpoint throughput,
  a direct measure of terminal pressure.
- Analyst-chosen scenario weights, and uncertainty in each trend slope, not only in the weights.
- A monthly rebuild, an automatically graded set of test questions, a hosted instance with a
  spending cap, and a local model through Ollama once its tool selection is measured.
