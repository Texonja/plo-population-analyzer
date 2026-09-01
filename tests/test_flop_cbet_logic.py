from argparse import Namespace
import unittest

from poker_population_analytics import population


def pool_args():
    return Namespace(hero="Hero", include_hero=True, debug_players=False)


def heads_up_hand(
    hand_id: int,
    *,
    pfa_ip: bool,
    response: str = "fold",
    pfa_checks: bool = False,
    defender_leads: bool = False,
) -> str:
    if pfa_ip:
        seats = """Seat 1: Aggressor (₮50.00 in chips)
Seat 2: Hero (₮50.00 in chips)"""
        posts = """Aggressor: posts small blind ₮0.25
Hero: posts big blind ₮0.50"""
        preflop = """Aggressor: raises ₮1.25 to ₮1.50
Hero: calls ₮1.00"""
        if defender_leads:
            flop = """Hero: bets ₮1.50
Aggressor: folds"""
        elif pfa_checks:
            flop = """Hero: checks
Aggressor: checks"""
        else:
            bet_amount = "3.00" if response == "allin_call" else "1.50"
            response_line = {
                "fold": "Hero: folds",
                "call": "Hero: calls ₮1.50",
                "raise": "Hero: raises ₮3.00 to ₮4.50",
                "allin_call": "Hero: ALLIN ₮2.00",
                "allin_raise": "Hero: ALLIN ₮5.00",
            }[response]
            flop = f"Hero: checks\nAggressor: bets ₮{bet_amount}\n{response_line}"
    else:
        seats = """Seat 1: Hero (₮50.00 in chips)
Seat 2: Aggressor (₮50.00 in chips)"""
        posts = """Hero: posts small blind ₮0.25
Aggressor: posts big blind ₮0.50"""
        preflop = """Hero: calls ₮0.25
Aggressor: raises ₮1.00 to ₮1.50
Hero: calls ₮1.00"""
        if pfa_checks:
            flop = """Aggressor: checks
Hero: checks"""
        else:
            bet_amount = "3.00" if response == "allin_call" else "1.50"
            response_line = {
                "fold": "Hero: folds",
                "call": "Hero: calls ₮1.50",
                "raise": "Hero: raises ₮3.00 to ₮4.50",
                "allin_call": "Hero: ALLIN ₮2.00",
                "allin_raise": "Hero: ALLIN ₮5.00",
            }[response]
            flop = f"Aggressor: bets ₮{bet_amount}\n{response_line}"

    return f"""CoinPoker Hand #{hand_id}: PLO 4 (₮0.25/₮0.50/₮0.05) 2026/02/01 20:00:00 CET
Table 'SYNTH-CBET' 2-max Seat #1 is the button
{seats}
Aggressor: posts ante ₮0.05
Hero: posts ante ₮0.05
{posts}
*** HOLE CARDS ***
Dealt to Aggressor
Dealt to Hero [As Kd Qh Jc]
{preflop}
*** FLOP *** [Ks 7h 2c]
{flop}
"""


def multiway_hand() -> str:
    return """CoinPoker Hand #9100000100: PLO 4 (₮0.25/₮0.50/₮0.05) 2026/02/01 20:10:00 CET
Table 'SYNTH-CBET' 3-max Seat #1 is the button
Seat 1: Aggressor (₮50.00 in chips)
Seat 2: Hero (₮50.00 in chips)
Seat 3: Caller (₮50.00 in chips)
Aggressor: posts ante ₮0.05
Hero: posts ante ₮0.05
Caller: posts ante ₮0.05
Hero: posts small blind ₮0.25
Caller: posts big blind ₮0.50
*** HOLE CARDS ***
Dealt to Aggressor
Dealt to Hero [As Kd Qh Jc]
Dealt to Caller
Aggressor: raises ₮1.25 to ₮1.50
Hero: calls ₮1.25
Caller: calls ₮1.00
*** FLOP *** [Ks 7h 2c]
Hero: checks
Caller: checks
Aggressor: bets ₮2.00
Hero: folds
Caller: folds
"""


class FlopCbetLogicTests(unittest.TestCase):
    def parse(self, text: str) -> population.Hand:
        hand = population.parse_hand(text, hero="Hero")
        self.assertIsNotNone(hand)
        return hand

    def test_ip_pfa_fold_call_raise_responses(self):
        for offset, expected in enumerate(("fold", "call", "raise_or_allin")):
            with self.subTest(expected=expected):
                response = ("fold", "call", "raise")[offset]
                evaluation = population.evaluate_flop_cbet(
                    self.parse(heads_up_hand(9100000000 + offset, pfa_ip=True, response=response))
                )
                self.assertTrue(evaluation.included)
                self.assertIs(evaluation.pfa_is_ip, True)
                self.assertEqual(population.response_category(evaluation.response), expected)

    def test_oop_pfa_fold_call_raise_responses(self):
        for offset, expected in enumerate(("fold", "call", "raise_or_allin")):
            with self.subTest(expected=expected):
                response = ("fold", "call", "raise")[offset]
                evaluation = population.evaluate_flop_cbet(
                    self.parse(heads_up_hand(9100000010 + offset, pfa_ip=False, response=response))
                )
                self.assertTrue(evaluation.included)
                self.assertIs(evaluation.pfa_is_ip, False)
                self.assertEqual(population.response_category(evaluation.response), expected)

    def test_multiway_donk_and_checkthrough_are_excluded(self):
        multiway = population.evaluate_flop_cbet(self.parse(multiway_hand()))
        donk = population.evaluate_flop_cbet(
            self.parse(heads_up_hand(9100000020, pfa_ip=True, defender_leads=True))
        )
        checkthrough = population.evaluate_flop_cbet(
            self.parse(heads_up_hand(9100000021, pfa_ip=True, pfa_checks=True))
        )

        self.assertEqual(multiway.reason, "flop_not_heads_up_3way")
        self.assertEqual(donk.reason, "defender_led_into_pfa")
        self.assertEqual(checkthrough.reason, "pfa_checked_back_no_cbet")
        self.assertFalse(multiway.included or donk.included or checkthrough.included)

    def test_postflop_allin_call_and_raise_are_distinguished(self):
        call_hand = self.parse(heads_up_hand(9100000030, pfa_ip=True, response="allin_call"))
        raise_hand = self.parse(heads_up_hand(9100000031, pfa_ip=True, response="allin_raise"))

        call_eval = population.evaluate_flop_cbet(call_hand)
        raise_eval = population.evaluate_flop_cbet(raise_hand)
        self.assertEqual(call_eval.response.raw_action, "allin")
        self.assertEqual(call_eval.response.action, "call")
        self.assertEqual(population.response_category(call_eval.response), "call")
        self.assertEqual(raise_eval.response.raw_action, "allin")
        self.assertEqual(raise_eval.response.action, "raise")
        self.assertEqual(population.response_category(raise_eval.response), "raise_or_allin")

    def test_preflop_allin_raise_updates_pfa_and_pot_type(self):
        hand = self.parse(
            """CoinPoker Hand #9100000032: PLO 4 (₮0.25/₮0.50/₮0.05) 2026/02/01 20:00:00 CET
Table 'SYNTH-CBET' 2-max Seat #1 is the button
Seat 1: Aggressor (₮50.00 in chips)
Seat 2: Hero (₮50.00 in chips)
Aggressor: posts ante ₮0.05
Hero: posts ante ₮0.05
Aggressor: posts small blind ₮0.25
Hero: posts big blind ₮0.50
*** HOLE CARDS ***
Dealt to Aggressor
Dealt to Hero [As Kd Qh Jc]
Aggressor: ALLIN ₮10.00
Hero: calls ₮9.75
*** FLOP *** [Ks 7h 2c]
Hero: checks
Aggressor: checks
"""
        )
        preflop_allin = next(event for event in hand.events["PREFLOP"] if event.raw_action == "allin")
        self.assertEqual(preflop_allin.action, "raise")
        self.assertEqual(hand.preflop_raises, 1)
        self.assertEqual(hand.last_preflop_raiser, "Aggressor")
        self.assertEqual(hand.pot_type, "srp")

    def test_straddled_hands_are_detected_and_excluded_by_default(self):
        text = heads_up_hand(9100000033, pfa_ip=True, response="fold").replace(
            "Hero: posts big blind ₮0.50",
            "Hero: posts big blind ₮0.50\nAggressor: STRADDLE ₮1.00",
        )
        hand = self.parse(text)
        base = {
            "game_filter": "plo4",
            "include_bombpot": False,
            "stake_filter": "",
            "date_from": "",
            "date_to": "",
        }
        self.assertIs(hand.meta["is_straddled"], True)
        self.assertEqual(hand.meta["straddle_count"], 1)
        self.assertFalse(population.pass_filters(hand, Namespace(**base, include_straddles=False)))
        self.assertTrue(population.pass_filters(hand, Namespace(**base, include_straddles=True)))

    def test_extractor_uses_the_same_validated_denominator(self):
        included = self.parse(heads_up_hand(9100000040, pfa_ip=True, response="fold"))
        excluded = self.parse(heads_up_hand(9100000041, pfa_ip=True, defender_leads=True))
        included_rows = [
            row
            for row in population.extract_required_pool_records(included, pool_args())
            if row["spot_id"] == "FOLD_TO_FLOP_CBET"
        ]
        excluded_rows = [
            row
            for row in population.extract_required_pool_records(excluded, pool_args())
            if row["spot_id"] == "FOLD_TO_FLOP_CBET"
        ]
        self.assertEqual(len(included_rows), 1)
        self.assertEqual(included_rows[0]["action"], "fold")
        self.assertEqual(excluded_rows, [])

    def test_audit_row_contains_manual_review_evidence(self):
        hand = self.parse(heads_up_hand(9100000050, pfa_ip=False, response="raise"))
        row = population.make_flop_cbet_audit_row(hand)
        self.assertIs(row["denominator_eligible"], True)
        self.assertEqual(row["reason_included"], "included_raise_or_allin")
        self.assertEqual(row["detected_cbet_bettor"], "Aggressor")
        self.assertEqual(row["response_classified_action"], "raise_or_allin")
        self.assertIn("Aggressor: bets", row["full_flop_action_sequence"])
        self.assertIn("Hero: raises", row["full_flop_action_sequence"])


if __name__ == "__main__":
    unittest.main()
