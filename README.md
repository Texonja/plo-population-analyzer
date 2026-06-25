# CoinPoker PLO Population Analyzer

Population-level analytics for anonymized CoinPoker Pot-Limit Omaha hand histories.

## Why this project exists

CoinPoker hand history exports use anonymized player identifiers. That makes classic player-level profiling unsuitable and, in practice, not the goal of this project.

This tool was built to answer a different question:

**What does the overall CoinPoker PLO player pool tend to do in important decision nodes?**

Instead of trying to track individual opponents, the analyzer aggregates population-level tendencies from anonymized CoinPoker hand histories and produces reports that highlight potential strategic patterns, deviations, and review candidates.

## Scope

This project is currently built specifically for the CoinPoker hand history format.

It is not a generic poker hand history parser. The parser, filters and decision-node extraction logic are designed around CoinPoker PLO exports.

## What it does

The tool parses raw CoinPoker PLO hand histories and extracts decision-node statistics such as:

- flop c-bet and fold vs c-bet frequencies
- bet IP vs missed c-bet
- turn probe and fold vs turn probe
- delayed c-bet frequencies, split by IP/OOP
- fold vs delayed c-bet
- fold vs flop and turn check-raise
- turn barrel after flop c-bet/call
- river barrel after flop+turn call
- 4-bet showdown composition, including AAxx frequency among known 4-bettor hands

The output includes CSV summaries, Markdown reports, HTML dashboards and charts.

## Privacy-first approach

The analyzer is designed for population-level aggregation only.

It does not create player profiles, does not attempt to identify opponents, and does not rely on stable player names. This is intentional, because the source hand histories are anonymized and the goal is to analyze the pool, not target individual players.

Raw hand history files and generated reports from real data should not be committed to the repository.

## Example use cases

- Identify whether the CoinPoker PLO pool under-stabs or over-stabs missed c-bet spots
- Compare SRP, 3-bet and 4-bet pot behavior
- Review how often players fold to delayed c-bets
- Check whether 4-bet ranges are heavily weighted toward AAxx
- Compare tendencies across stakes such as PL25, PL50 and PL100
- Generate dashboards for quick review

## Installation

    pip install -r requirements.txt

## Basic usage

Pool tendency analysis:

    python scripts/plo_population_analyzer_pro.py --input cash.txt --outdir reports/PL50_pool --game-filter plo4 --stake-filter PL50 --min-sample 30

4-bet showdown analysis:

    python scripts/plo_fourbet_showdown_analyzer_pro.py --input cash.txt --outdir reports/fourbet_PL50 --game-filter plo4 --stake-filter PL50 --hero Hero --min-stack-bb 60

Stake comparison:

    python scripts/plo_population_analyzer_pro.py --input cash.txt --outdir reports/stake_compare --game-filter plo4 --compare-stakes PL25,PL50,PL100 --min-sample 30

## Output files

Typical pool analyzer output:

- pool_dashboard.html
- pool_report.md
- pool_report.html
- pool_summary.csv
- pool_summary_by_board.csv
- pool_summary_by_position.csv
- pool_summary_by_ip_oop.csv
- pool_summary_by_facing_size.csv
- pool_spots.csv
- charts/

Typical 4-bet analyzer output:

- fourbet_dashboard.html
- fourbet_report.md
- fourbet_summary.csv
- fourbet_known_cards.csv
- fourbet_all.csv
- charts/

## Benchmarks

The default benchmark bands are broad heuristic review bands, not solver-derived GTO targets.

They are used to flag potential review candidates such as unusually low bet frequencies or unusually high fold frequencies. Users can provide their own benchmark configuration through the --benchmarks argument.

Example:

    python scripts/plo_population_analyzer_pro.py --input cash.txt --outdir reports/custom --game-filter plo4 --stake-filter PL50 --benchmarks config/benchmarks.custom.json

Benchmark files affect only interpretation fields such as:

- benchmark_flag
- leak_note
- deviation_pp
- exploit_score
- dashboard flags

They do not change the measured raw frequencies from the hand history.

## AI-assisted development note

This was built as an AI-assisted analytics project. The domain logic, decision-node definitions, validation questions and interpretation were driven through iterative analysis of real anonymized CoinPoker PLO pool tendencies.

The project is intended to demonstrate practical domain-driven analytics: parsing event logs, extracting meaningful decision nodes, validating assumptions, and producing reports that support decision-making.

## Limitations

- This is not a solver.
- Benchmark bands are not GTO truth.
- This is currently CoinPoker-specific, not a universal hand history parser.
- Results depend on hand history format and sample size.
- Unknown showdown hands are excluded from hand-composition percentages.
- Population tendencies should be treated as review candidates, not automatic strategic rules.
- Raw hand histories and generated real-data reports should not be committed to the repository.

## Repository structure

    config/
      benchmarks.default.json

    docs/
      USAGE.md
      CHANGELOG_PRO.md

    examples/
      README_example_outputs.md

    scripts/
      plo_population_analyzer_pro.py
      plo_fourbet_showdown_analyzer_pro.py

    requirements.txt

## License

MIT
