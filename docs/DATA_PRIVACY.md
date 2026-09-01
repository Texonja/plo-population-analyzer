# Data privacy and handling

Hand histories can contain pseudonymous player identifiers, timestamps, table identifiers, exact cards and financial information. Treat them as sensitive operational data even when screen names are randomized.

## Safe workflow

1. Store raw exports outside the repository.
2. Run analysis locally in an ignored `reports/` directory.
3. Review granular CSV files before sharing them.
4. Publish only aggregate summaries or deliberately sanitized fixtures.
5. Keep access controls and retention periods aligned with the data owner's policy.

The repository ignores common hand-history paths and generated report directories. The committed `examples/synthetic_hands.txt` file is fabricated, and the portfolio dashboard contains aggregate statistics only.

## Output sensitivity

- `pool_summary*.csv`, the Markdown report and dashboard are aggregate outputs.
- `pool_spots.csv` contains hand identifiers and timestamps for local auditability.
- `audit_fold_to_flop_cbet*.csv` can contain hand identifiers, player labels and exact action lines and is local-only.
- `fourbet_all.csv` and `fourbet_known_cards.csv` contain exact known cards; player IDs and named action sequences are omitted unless `--include-identifiers` is explicitly enabled.
- `filters_applied.json` records only the input filename, not its full local path.

Do not commit granular outputs from real data. If they must be shared for a legitimate review, use an approved secure channel and apply the organization's pseudonymization policy first.
