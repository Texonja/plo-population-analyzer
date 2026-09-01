from argparse import Namespace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from poker_population_analytics import population


FIXTURE = Path(__file__).parents[1] / "examples" / "synthetic_hands.txt"
BENCHMARKS = Path(__file__).parents[1] / "config" / "benchmarks.default.json"


def population_args(**overrides):
    values = {
        "hero": "Hero",
        "include_hero": False,
        "min_sample": 1,
        "game_filter": "plo4",
        "include_bombpot": False,
        "include_straddles": False,
        "stake_filter": "",
        "date_from": "",
        "date_to": "",
        "benchmarks": "",
        "debug_players": False,
        "no_charts": True,
        "compare_stakes": "",
    }
    values.update(overrides)
    return Namespace(**values)


class PopulationTests(unittest.TestCase):
    def test_header_and_position_parsing(self):
        header = population.parse_header(
            "CoinPoker Hand #9000000001: PLO 4 (₮0.25/₮0.50/₮0.05) 2026/01/10 20:00:00 CET"
        )
        self.assertIsNotNone(header)
        self.assertEqual(header["stake_label"], "PL50")
        self.assertEqual(header["plo_cards"], 4)

        positions = population.assign_positions({1: "Button", 2: "SmallBlind", 3: "BigBlind"}, 1)
        self.assertEqual(positions, {"Button": "BTN", "SmallBlind": "SB", "BigBlind": "BB"})

    def test_board_and_starting_hand_classification(self):
        self.assertEqual(population.board_bucket(["As", "7h", "2c"]), "dry")
        self.assertEqual(population.board_bucket(["As", "Ah", "2c"]), "paired")
        self.assertEqual(population.classify_starting_hand(["As", "Ah", "Kd", "Qd"])["hand_class"], "AAxx")

    def test_committed_benchmarks_match_runtime_defaults(self):
        committed = json.loads(BENCHMARKS.read_text(encoding="utf-8"))
        self.assertEqual(committed, population.DEFAULT_BENCHMARKS)

    def test_unresolved_cbet_definition_is_not_ranked_as_an_exploit(self):
        rows = [{
            "spot_id": "FOLD_TO_FLOP_CBET",
            "stake_label": "PL50",
            "pot_type": "srp",
            "spot_name": "Fold/call/raise vs flop c-bet",
            "action": "fold",
        }] * 100
        summary = population.summarize_records(
            rows,
            population.DEFAULT_BENCHMARKS,
            min_sample=30,
            group_cols=["spot_id", "stake_label", "pot_type"],
        )[0]
        self.assertEqual(summary["benchmark_flag"], "audit_required")
        self.assertIn("denominator equivalence", summary["leak_note"])
        self.assertEqual(summary["deviation_pp"], 0.0)
        self.assertEqual(summary["exploit_score"], 0.0)

    def test_population_pipeline_writes_auditable_outputs(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            result = population.analyze_pool(FIXTURE, output, population_args())

            self.assertEqual(result["raw_hands_before_filters"], 4)
            self.assertEqual(result["parse_errors"], 0)
            self.assertGreater(result["pool_spot_records"], 0)
            for filename in (
                "pool_summary.csv",
                "pool_summary_by_board.csv",
                "pool_spots.csv",
                "filters_applied.json",
                "benchmarks_used.json",
                "pool_dashboard.html",
            ):
                self.assertTrue((output / filename).exists(), filename)
            manifest = json.loads((output / "filters_applied.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["input"], FIXTURE.name)

    def test_opt_in_flop_cbet_audit_outputs_are_written(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            result = population.analyze_pool(
                FIXTURE,
                output,
                population_args(audit_flop_cbet=True, audit_sample_size=10),
            )
            self.assertTrue((output / "audit_fold_to_flop_cbet.csv").exists())
            self.assertTrue((output / "audit_fold_to_flop_cbet_included_sample.csv").exists())
            self.assertTrue((output / "audit_fold_to_flop_cbet_exclusion_summary.csv").exists())
            self.assertGreaterEqual(result["flop_cbet_denominator_hands"], 0)

    def test_hero_is_excluded_from_population_by_default(self):
        blocks = population.split_hand_history(FIXTURE.read_text(encoding="utf-8"))
        hand = population.parse_hand(blocks[1], hero="Hero")
        self.assertIsNotNone(hand)
        records = population.extract_required_pool_records(hand, population_args())
        self.assertTrue(all(record.get("actor") != "Hero" for record in records))


if __name__ == "__main__":
    unittest.main()
