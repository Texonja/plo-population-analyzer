from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from poker_population_analytics import fourbet


FIXTURE = Path(__file__).parents[1] / "examples" / "synthetic_hands.txt"


def fourbet_args(**overrides):
    values = {
        "hero": "Hero",
        "include_hero": False,
        "allow_hands_without_hero": False,
        "min_stack_bb": 60.0,
        "game_filter": "plo4",
        "include_bombpot": False,
        "include_straddles": False,
        "stake_filter": "",
        "date_from": "",
        "date_to": "",
        "no_charts": True,
        "include_identifiers": False,
    }
    values.update(overrides)
    return Namespace(**values)


class FourbetTests(unittest.TestCase):
    def test_known_hand_classification(self):
        result = fourbet.classify_known_hand(["As", "Ah", "Kd", "Qd"])
        self.assertIs(result["is_aaxx"], True)
        self.assertEqual(result["hand_class"], "AAxx")
        self.assertIs(result["double_suited"], False)
        self.assertIs(fourbet.classify_known_hand(["As", "Ah", "Ks", "Kh"])["double_suited"], True)

    def test_fourbet_sequence_and_showdown_cards(self):
        first_hand = fourbet.split_hand_history(FIXTURE.read_text(encoding="utf-8"))[0]
        error, rows = fourbet.analyze_hand(first_hand, fourbet_args())

        self.assertIsNone(error)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["fourbettor"], "PlayerGamma")
        self.assertEqual(row["fourbet_action"], "4bet")
        self.assertIs(row["known_cards"], True)
        self.assertIs(row["is_aaxx"], True)
        self.assertIn("3bet", row["preflop_sequence"])

    def test_straddled_fourbet_hand_is_opt_in(self):
        first_hand = fourbet.split_hand_history(FIXTURE.read_text(encoding="utf-8"))[0]
        straddled = first_hand.replace(
            "PlayerBeta: posts big blind ₮0.50",
            "PlayerBeta: posts big blind ₮0.50\nPlayerAlpha: STRADDLE ₮1.00",
        )
        _, excluded = fourbet.analyze_hand(straddled, fourbet_args(include_straddles=False))
        _, included = fourbet.analyze_hand(straddled, fourbet_args(include_straddles=True))
        self.assertEqual(excluded, [])
        self.assertEqual(len(included), 1)

    def test_unknown_cards_are_not_classified_as_non_aaxx(self):
        summary = fourbet.summarize(
            [
                {"stake_label": "PL50", "fourbettor_position": "CO", "known_cards": True, "is_aaxx": True},
                {"stake_label": "PL50", "fourbettor_position": "CO", "known_cards": False, "is_aaxx": ""},
            ]
        )
        aggregate = next(row for row in summary if row["fourbettor_position"] == "ALL")
        self.assertEqual(aggregate["known_rate_pct"], "50.0%")
        self.assertEqual(aggregate["AAxx_pct_of_known"], "100.0%")

    def test_detail_export_redacts_player_identifiers_by_default(self):
        row = {"hand_id": "1", "fourbettor": "PlayerGamma", "preflop_sequence": "PlayerGamma:4bet"}
        redacted = fourbet.export_detail_row(row)
        self.assertNotIn("fourbettor", redacted)
        self.assertNotIn("preflop_sequence", redacted)
        self.assertEqual(fourbet.export_detail_row(row, include_identifiers=True), row)

    def test_no_charts_mode_still_writes_dashboard_and_redacted_csv(self):
        with TemporaryDirectory() as directory:
            fourbet.main(
                [
                    "--input",
                    str(FIXTURE),
                    "--outdir",
                    directory,
                    "--min-stack-bb",
                    "60",
                    "--no-charts",
                ]
            )
            output = Path(directory)
            self.assertTrue((output / "fourbet_dashboard.html").exists())
            header = (output / "fourbet_all.csv").read_text(encoding="utf-8").splitlines()[0]
            self.assertNotIn("fourbettor,", header)
            self.assertNotIn("preflop_sequence", header)


if __name__ == "__main__":
    unittest.main()
