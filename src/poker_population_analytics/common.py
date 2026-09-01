"""Shared parsing primitives for CoinPoker PLO hand histories."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


RANK_ORDER = "23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANK_ORDER, start=2)}
HEADER_RE = re.compile(r"CoinPoker Hand #(\d+): (.*?) \(([^)]+)\) ([0-9/]+ [0-9:]+) (\w+)")


def split_hand_history(text: str) -> List[str]:
    """Split a text export into complete hand-history blocks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return re.split(r"\n(?=CoinPoker Hand #)", normalized)


def parse_header(line: str) -> Optional[Dict[str, Any]]:
    """Parse stable game metadata from a CoinPoker hand header."""
    match = HEADER_RE.match(line)
    if not match:
        return None
    hand_id, description, stakes, date_time, timezone = match.groups()
    amounts = [float(value) for value in re.findall(r"([0-9]+(?:\.[0-9]+)?)", stakes)]
    small_blind = amounts[0] if len(amounts) > 0 else None
    big_blind = amounts[1] if len(amounts) > 1 else None
    third_amount = amounts[2] if len(amounts) > 2 else None
    plo_match = re.search(r"PLO\s+(\d+)", description)
    plo_cards = int(plo_match.group(1)) if plo_match else None
    is_bombpot = "BombPot" in description
    return {
        "hand_id": hand_id,
        "desc": description,
        "stakes_str": stakes,
        "dt": date_time,
        "tz": timezone,
        "sb": small_blind,
        "bb": big_blind,
        "ante": None if is_bombpot else third_amount,
        "bomb_amount": third_amount if is_bombpot else None,
        "plo_cards": plo_cards,
        "is_bombpot": is_bombpot,
        "stake_label": f"PL{int(round(big_blind * 100))}" if big_blind else "UNKNOWN",
    }


def assign_positions(seats: Dict[int, str], button_seat: Optional[int]) -> Dict[str, str]:
    """Map player names to conventional positions for two to nine seats."""
    if not seats or button_seat not in seats:
        return {}
    seat_numbers = sorted(seats)
    button_index = seat_numbers.index(button_seat)
    order = [seat_numbers[(button_index + offset) % len(seat_numbers)] for offset in range(len(seat_numbers))]
    labels_by_count = {
        2: ["BTN/SB", "BB"],
        3: ["BTN", "SB", "BB"],
        4: ["BTN", "SB", "BB", "CO"],
        5: ["BTN", "SB", "BB", "UTG", "CO"],
        6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
        7: ["BTN", "SB", "BB", "UTG", "UTG1", "HJ", "CO"],
        8: ["BTN", "SB", "BB", "UTG", "UTG1", "MP", "HJ", "CO"],
        9: ["BTN", "SB", "BB", "UTG", "UTG1", "MP1", "MP2", "HJ", "CO"],
    }
    labels = labels_by_count.get(len(order), ["BTN"] + [f"P{index}" for index in range(1, len(order))])
    return {seats[seat]: labels[index] for index, seat in enumerate(order)}

