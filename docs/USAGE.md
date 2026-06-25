# Usage Quickstart

Pool report for one stake:

```bash
python scripts/plo_population_analyzer_pro.py --input cash.txt --outdir reports/PL50_pool --game-filter plo4 --stake-filter PL50 --min-sample 30
```

Compare stakes:

```bash
python scripts/plo_population_analyzer_pro.py --input cash.txt --outdir reports/stake_compare --game-filter plo4 --compare-stakes PL25,PL50,PL100 --min-sample 30
```

4-bet showdown composition:

```bash
python scripts/plo_fourbet_showdown_analyzer_pro.py --input cash.txt --outdir reports/fourbet_PL50 --game-filter plo4 --stake-filter PL50 --hero Hero --min-stack-bb 60
```

Main HTML outputs:

- `pool_dashboard.html`
- `pool_report.md`
- `stake_comparison.md`
- `fourbet_dashboard.html`
- `fourbet_report.md`

No per-player tendency file is produced. The tool is designed for population-only aggregation on anonymized hand histories.
