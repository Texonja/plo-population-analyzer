# Methodology

## Objective

The project estimates how the CoinPoker PLO pool behaves at strategically defined decision nodes. Its output is descriptive: it helps an analyst find patterns worth reviewing, quantify their sample size and inspect the source hands.

It does not assign risk scores to accounts and it does not claim to detect multi-accounting, RTA, bots or collusion.

## Processing stages

1. **Parse** — split the export into hands and extract metadata, seats, positions, boards and ordered actions.
2. **Normalize** — convert actions into consistent categories, reconstruct pot-relative sizes and label pot type and board texture.
3. **Identify opportunities** — emit a record only when the exact preconditions for a named decision node are met.
4. **Aggregate** — group records by stake, pot type and optional context dimensions.
5. **Review** — compare a selected metric with a configurable benchmark band and attach sample-size confidence.
6. **Audit** — retain local spot-level records so an analyst can inspect false positives or parsing edge cases.

## Core definitions

- An **opportunity** is a hand state in which the tracked player can take one of the actions defined for a node.
- `bet_pct`, `check_pct`, `fold_pct`, `call_pct` and `raise_pct` use the node's opportunity count as denominator.
- `avg_size_pct` is the mean action size as a percentage of the pot immediately before the action.
- `avg_facing_size_pct` describes the size faced by the responder, not the responder's own action.
- `pot_type` is derived from the number of preflop raises (`limped`, `srp`, `3bet`, `4bet+`).
- Board texture is a deterministic bucket (`dry`, `medium`, `wet`, `high`, `paired`, `monotone`) intended for segmentation rather than exhaustive hand-strength modeling.

### Action semantics

CoinPoker can encode both a call and a raise as `ALLIN X`. The parser compares the actor's new street contribution with the amount already required to call. An all-in is a raise only when it increases that amount; otherwise it is a call. Preflop all-in raises also update the last preflop aggressor, raise count and pot type.

Straddles alter the preflop price and positional incentives. They are parsed and counted, but straddled hands are excluded by default until separate straddle-specific decision-node definitions are validated.

### Flop c-bet response denominator

`FOLD_TO_FLOP_CBET` currently requires all of the following:

1. a final preflop aggressor is known;
2. exactly two players are active on the flop;
3. no defender donk-bet occurs before the aggressor acts;
4. the preflop aggressor makes the first flop bet;
5. the defender's next meaningful action is a fold, call or raise/all-in.

Multiway flops, missing flop actions, check-throughs, donk leads and malformed or missing responses are excluded with explicit reason codes. The opt-in audit export retains the exact flop action sequence, positions, board, c-bet size and classified response for manual review.

The local analyzer's result does not currently match the comparison CoinPoker HUD range. Until denominator equivalence is established, this spot is marked `audit_required` and must not be used for strategy inference.

## Benchmark interpretation

The file `config/benchmarks.default.json` contains practical review bands. A row can be:

- `LOW` or `HIGH` when the observed metric is outside the band and meets `--min-sample`;
- `OK` when it is inside the band;
- `low_sample` when there are too few opportunities to promote the observation;
- `audit_required` when an unresolved definition or external comparison blocks interpretation.

The bands are not solver truth. They should be versioned for a specific research question, stake environment and time window. The generated report copies the exact benchmark file used into the output directory.

The dashboard's exploit score is a prioritization aid based on distance from the band and a coarse sample-size weight. It is not a probability, confidence interval or expected-value estimate. Rows marked `audit_required` receive zero deviation and zero exploit score.

## Four-bet showdown analysis

A four-bet is the third preflop raise. An all-in counts as a raise only when it increases the amount to call. The analyzer reports:

- all qualifying four-bet events after stack and game filters;
- the subset with known/shown hole cards;
- AAxx and broad hand-class composition among known hands;
- the reveal rate, so missing-card selection remains visible.

Unknown hands are excluded from the hand-class denominator. Results are therefore conditional on a hand being revealed and may be distorted by showdown selection.

## Quality controls

- Parse errors are counted and surfaced in `filters_applied.json`.
- Every run records the filters, hand counts and benchmark definition used.
- Synthetic tests cover hand splitting, header parsing, position assignment, board classification, known-card extraction and both report pipelines.
- Targeted fixtures cover IP/OOP c-bet responses, multiway exclusions, donk leads, check-throughs, all-in calls versus raises, preflop all-ins and straddles.
- The c-bet audit supports a deterministic, stratified sample for source-hand validation.
- Real hand histories are intentionally absent from the repository.

## Known limitations

- The parser is format-specific and can require updates if the export wording changes.
- The `FOLD_TO_FLOP_CBET` denominator remains under audit because it differs materially from a comparison CoinPoker HUD result.
- Deterministic texture buckets compress strategically different boards into broad labels.
- Raw frequencies do not control for range composition or opponent selection.
- Small samples and repeated strategic contexts are not independent observations.
- The tool does not calculate uncertainty intervals or adjust for multiple comparisons.
- Population behavior can drift over time; results should be filtered and re-run for the relevant window.
