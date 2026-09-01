# CoinPoker PLO Population Analyzer

CoinPoker-specific, privacy-conscious population analysis for Pot-Limit Omaha hand histories.

[View the case study](docs/index.html) · [Open the population dashboard](docs/demo/pool_dashboard.html) · [Open the four-bet dashboard](docs/demo/fourbet_dashboard.html) · [Read the methodology](docs/METHODOLOGY.md)

## Recruiter preview — no setup required

On Windows, extract the repository and double-click **`START.bat`**. It opens the complete case study and its aggregate dashboards in the default browser. Python, a terminal and access to private hand histories are not required for the portfolio preview.

The installation commands below are only for analysts who want to process their own CoinPoker exports.

This project parses CoinPoker hand histories, reconstructs ordered betting sequences and measures population behavior at precisely defined decision nodes. It is an analytics and decision-support tool—not a player tracker, HUD, solver, scraper, multi-account detector, RTA detector, bot detector or evidence of a rules violation.

## What it demonstrates

- domain modeling of PLO actions, streets, positions, pot types and pot-relative sizing;
- deterministic extraction of missed c-bets, delayed c-bets, probes, barrels, check-raises and their response ranges;
- explicit call-versus-raise classification for `ALLIN X` actions;
- auditable denominator definitions, exclusion reasons and source-action evidence;
- configurable review bands with sample-size labels;
- aggregate-only public reporting and local hand-level validation;
- explicit treatment of unknown cards and selection bias in showdown data.

The important unit is a **decision opportunity**, not an isolated aggressive action. An exploit hypothesis is only considered when both the initiating frequency and the response distribution are measurable at the same node.

## Current validation status

The committed snapshot comes from 38,248 raw hands. Default filters retained 27,701 regular, non-straddled PLO4 hands and produced 19,527 decision-node records with zero parse errors.

Known and tested:

- postflop all-ins are calls when they do not increase the amount to call, and raises when they do;
- preflop all-in raises update the preflop aggressor and pot type;
- straddled hands are detected and excluded by default pending straddle-specific validation;
- flop c-bet audit rows expose the PFA, IP/OOP state, active players, exact action sequence, bet size, response classification and inclusion/exclusion reason;
- targeted tests cover IP and OOP fold/call/raise responses, multiway pots, donk leads, check-throughs, all-in calls/raises and straddles;
- a stratified sample of 100 included flop c-bet opportunities was manually reviewed against the source action sequences.

Still unresolved:

- this analyzer reports PL50 fold-to-flop-c-bet rates of 44.6% in single-raised pots (`n=2,102`) and 43.2% in three-bet pots (`n=837`), while the comparison CoinPoker HUD displays roughly 25–39%;
- straddles, all-in c-bets, position and table size did not explain the difference;
- the spot is therefore marked `audit_required`, assigned zero deviation/exploit score and excluded from strategy claims until denominator equivalence is established.

That unresolved discrepancy is intentionally visible. Hiding it would make the report cleaner and the analysis weaker.

## Case-study observations

These are descriptive observations from one dataset against configurable review bands—not solver truth or automatic strategy prescriptions.

| PL50 node | Pot | Opportunities | Observed response |
| --- | --- | ---: | --- |
| IP action after missed flop c-bet | SRP | 847 | bet 40.7% |
| IP fold vs delayed c-bet | 3-bet | 101 | fold 62.4% |
| Bettor response vs flop check-raise | 3-bet | 90 | fold 28.9% / call 41.1% / raise 30.0% |
| Bettor response vs turn check-raise | 3-bet | 30 | fold 33.3% / call 60.0% / raise 6.7% |

The flop check-raise row is a useful example of why action semantics matter: after correcting all-in calls that had been classified as aggression, the raise share fell from the earlier 48.9% result to 30.0%. The defensible statement is that the sampled bettors continued 71.1%, not that they re-raised nearly half the time.

The secondary four-bet analyzer found 468 qualifying non-straddled events at a 60bb minimum stack. Cards were known/shown in 292; 172 of those were AAxx (58.9%). That percentage is conditional on cards being revealed and is not an estimate for all four-bets without a selection-bias assumption.

## Quick start

Python 3.10+ is required.

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

Run the population report on the included synthetic fixture:

```bash
plo-population --input examples/synthetic_hands.txt --outdir reports/synthetic_pool --game-filter plo4 --min-sample 1
```

Run the four-bet showdown report:

```bash
plo-fourbet --input examples/synthetic_hands.txt --outdir reports/synthetic_fourbet --game-filter plo4 --min-stack-bb 60
```

Generate the local denominator audit when validating a real export:

```bash
plo-population --input path/to/hands.txt --outdir reports/audit --audit-flop-cbet --audit-sample-size 100
```

Real hand histories and granular audit outputs should remain outside version control.

## Main outputs

| File | Purpose |
| --- | --- |
| `pool_dashboard.html` | Aggregate review dashboard |
| `pool_summary.csv` | Decision nodes by stake and pot type |
| `pool_summary_by_board.csv` | Board-texture and IP/OOP splits |
| `pool_summary_by_position.csv` | Position and board-texture splits |
| `pool_summary_by_facing_size.csv` | Response splits by size faced |
| `pool_spots.csv` | Local hand-level audit trail; do not publish from real data |
| `audit_fold_to_flop_cbet*.csv` | Opt-in denominator/exclusion audit; do not publish |
| `filters_applied.json` | Exact run parameters and parse counts |
| `benchmarks_used.json` | Review bands and audit statuses used by the run |

## Analytical guardrails

- Hero actions are excluded from population records by default.
- CoinPoker's anonymized player identifiers are not used to build player profiles.
- `LOW`/`HIGH` are review flags; `audit_required` blocks interpretation for unresolved definitions.
- Benchmark bands are analyst-configured review ranges, not GTO or solver outputs.
- Raw frequencies do not establish causality or expected value.
- Unknown four-bet hands remain in the coverage denominator but outside hand-class percentages.
- Four-bet detail exports omit player identifiers and named sequences unless explicitly enabled.

See [METHODOLOGY.md](docs/METHODOLOGY.md) for definitions and limitations, and [DATA_PRIVACY.md](docs/DATA_PRIVACY.md) before using real exports.

## Repository layout

```text
config/                         configurable review bands and audit statuses
docs/                           case study and aggregate-only demo
examples/synthetic_hands.txt    fabricated deterministic fixture
src/poker_population_analytics analysis pipelines and CLIs
tests/                          parser, classification, audit and smoke tests
.github/workflows/ci.yml        automated validation
```

## Validation

```bash
python -m unittest discover -s tests -v
ruff check --select E9,F63,F7,F82 .
python -m compileall -q src
```

## Authorship and AI assistance

The author defined the poker domain model, analytical questions, inclusion rules, audit criteria and interpretation boundaries, and manually validated source-hand samples. AI-assisted coding and editing were used during implementation and presentation. Analytical responsibility remains with the author.

## Scope

The parser targets CoinPoker text exports and defaults to regular PLO4. PLO5 and bomb-pot filters exist, but their node definitions require format-specific review before results should be interpreted.
