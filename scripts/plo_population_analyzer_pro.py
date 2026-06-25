#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLO Population Tendencies Analyzer PRO

This is NOT a solver and NOT an ML coach. It measures poker lines/sequences from
CoinPoker hand histories, e.g.:
- Hero c-bets flop OOP, checks turn, villain stabs, Hero folds/calls/raises.
- Hero c-bets flop IP, gets called, villain checks turn, Hero barrels/checks.
- Hero checks back flop IP, villain probes turn, Hero response.
- Hero defends blind, check-calls flop, faces turn barrel, Hero response.

Default: PLO 4 regular hands only. PLO5 and BombPot are excluded unless requested.

Usage:
    python coinpoker_plo_pool_analyzer_v2.py --input cash.txt --hero Hero --outdir pool_report

Filters:
    --game-filter plo4 | plo5 | bombpot | all       default: plo4
    --include-bombpot                              include bombpots with plo4/plo5 filter
    --stake-filter PL25,PL50
    --date-from YYYY-MM-DD --date-to YYYY-MM-DD
    --include-multiway                             default is HU flop only for most line stats
"""

from __future__ import annotations

import argparse
import collections
import csv
import html
import json
import math
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RANK_ORDER = "23456789TJQKA"
RANK_VALUE = {r: i for i, r in enumerate(RANK_ORDER, start=2)}
MONEY_RE = re.compile(r"₮([0-9]+(?:\.[0-9]+)?)")

ACTION_PATTERNS = [
    ("posts_ante", re.compile(r"^(.+?): posts ante ₮([0-9.]+)")),
    ("posts_sb", re.compile(r"^(.+?): posts small blind ₮([0-9.]+)")),
    ("posts_bb", re.compile(r"^(.+?): posts big blind ₮([0-9.]+)")),
    ("bets", re.compile(r"^(.+?): bets ₮([0-9.]+)")),
    ("calls", re.compile(r"^(.+?): calls ₮([0-9.]+)")),
    ("raises", re.compile(r"^(.+?): raises ₮([0-9.]+) to ₮([0-9.]+)")),
    ("allin", re.compile(r"^(.+?): ALLIN ₮([0-9.]+)")),
    ("return", re.compile(r"^(.+?): RETURN ₮([0-9.]+)")),
    ("collected", re.compile(r"^(.+?) collected ₮([0-9.]+) from pot")),
    ("folds", re.compile(r"^(.+?): folds")),
    ("checks", re.compile(r"^(.+?): checks")),
]

ACTION_MAP = {
    "bets": "bet",
    "calls": "call",
    "raises": "raise",
    "allin": "allin",
    "folds": "fold",
    "checks": "check",
}
AGGRO_ACTIONS = {"bet", "raise", "allin"}
CONTINUE_ACTIONS = {"call", "raise", "allin"}


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return default if not b else a / b


def split_hand_history(text: str) -> List[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    return re.split(r"\n(?=CoinPoker Hand #)", text)


def parse_all_cards_from_brackets(text: str) -> List[str]:
    cards: List[str] = []
    for group in re.findall(r"\[([^\]]*)\]", text):
        cards.extend(group.split())
    return cards


def parse_header(line: str) -> Optional[Dict[str, Any]]:
    m = re.match(r"CoinPoker Hand #(\d+): (.*?) \(([^)]+)\) ([0-9/]+ [0-9:]+) (\w+)", line)
    if not m:
        return None
    hand_id, desc, stakes_str, dt_str, tz = m.groups()
    nums = [float(x) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)", stakes_str)]
    sb = nums[0] if len(nums) > 0 else None
    bb = nums[1] if len(nums) > 1 else None
    third = nums[2] if len(nums) > 2 else None
    plo_match = re.search(r"PLO\s+(\d+)", desc)
    plo_cards = int(plo_match.group(1)) if plo_match else None
    is_bombpot = "BombPot" in desc
    return {
        "hand_id": hand_id,
        "desc": desc,
        "stakes_str": stakes_str,
        "dt": dt_str,
        "tz": tz,
        "sb": sb,
        "bb": bb,
        "ante": None if is_bombpot else third,
        "bomb_amount": third if is_bombpot else None,
        "plo_cards": plo_cards,
        "is_bombpot": is_bombpot,
        "stake_label": f"PL{int(round(bb * 100))}" if bb else "UNKNOWN",
    }


def assign_positions(seats: Dict[int, str], button_seat: Optional[int]) -> Dict[str, str]:
    if not seats or button_seat not in seats:
        return {}
    seat_nums = sorted(seats.keys())
    bidx = seat_nums.index(button_seat)
    order = [seat_nums[(bidx + i) % len(seat_nums)] for i in range(len(seat_nums))]
    n = len(order)
    labels_by_n = {
        2: ["BTN/SB", "BB"],
        3: ["BTN", "SB", "BB"],
        4: ["BTN", "SB", "BB", "CO"],
        5: ["BTN", "SB", "BB", "UTG", "CO"],
        6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
        7: ["BTN", "SB", "BB", "UTG", "UTG1", "HJ", "CO"],
        8: ["BTN", "SB", "BB", "UTG", "UTG1", "MP", "HJ", "CO"],
        9: ["BTN", "SB", "BB", "UTG", "UTG1", "MP1", "MP2", "HJ", "CO"],
    }
    labels = labels_by_n.get(n, ["BTN"] + [f"P{i}" for i in range(1, n)])
    return {seats[seat]: labels[i] for i, seat in enumerate(order)}


def postflop_order_players(seats: Dict[int, str], button_seat: Optional[int]) -> List[str]:
    if not seats or button_seat not in seats:
        return list(seats.values())
    seat_nums = sorted(seats.keys())
    bidx = seat_nums.index(button_seat)
    # Postflop action starts left of the button; button acts last.
    order = [seat_nums[(bidx + i) % len(seat_nums)] for i in range(1, len(seat_nums))] + [button_seat]
    return [seats[s] for s in order]


def classify_starting_hand(cards: List[str]) -> Dict[str, Any]:
    if not cards:
        return {"hero_cards": "", "hand_class": "unknown", "double_suited": False, "connected_window": 0, "high_cards": 0, "broadways": 0, "pairs": 0, "ace": False, "hero_card_count": 0}
    ranks = [c[0] for c in cards]
    suits = [c[1] for c in cards]
    rank_counts = collections.Counter(ranks)
    suit_counts = collections.Counter(suits)
    vals = sorted([RANK_VALUE.get(r, 0) for r in ranks], reverse=True)
    uniq = sorted(set(vals))
    pairs = sum(1 for v in rank_counts.values() if v == 2)
    high = sum(1 for v in vals if v >= 11)
    broadway = sum(1 for v in vals if v >= 10)
    ace = "A" in ranks
    double_suited = sum(1 for v in suit_counts.values() if v >= 2) >= 2
    best_window = 0
    for low in range(2, 11):
        best_window = max(best_window, sum(1 for v in uniq if low <= v <= low + 4))
    if rank_counts.get("A", 0) >= 2:
        hand_class = "AAxx"
    elif rank_counts.get("K", 0) >= 2:
        hand_class = "KKxx"
    elif pairs >= 1:
        hand_class = "pair"
    elif best_window >= 4:
        hand_class = "rundown"
    elif broadway >= 3:
        hand_class = "broadway"
    elif ace and max(suit_counts.values()) >= 2:
        hand_class = "suited_ace"
    else:
        hand_class = "other"
    return {
        "hero_cards": " ".join(cards),
        "hero_card_count": len(cards),
        "hand_class": hand_class,
        "double_suited": double_suited,
        "connected_window": best_window,
        "high_cards": high,
        "broadways": broadway,
        "pairs": pairs,
        "ace": ace,
    }


def board_bucket(cards: List[str]) -> str:
    if not cards:
        return "no_board"
    ranks = [c[0] for c in cards]
    suits = [c[1] for c in cards]
    vals = [RANK_VALUE.get(r, 0) for r in ranks]
    rank_counts = collections.Counter(ranks)
    suit_counts = collections.Counter(suits)
    paired = any(v >= 2 for v in rank_counts.values())
    monotone = len(cards) >= 3 and len(set(suits[:3])) == 1
    flush_possible = max(suit_counts.values()) >= 3 if suit_counts else False
    broadways = sum(1 for v in vals if v >= 10)
    uniq = sorted(set(vals))
    best_window = 0
    for low in range(2, 11):
        best_window = max(best_window, sum(1 for v in uniq if low <= v <= low + 4))
    wet_score = 0
    if monotone or flush_possible:
        wet_score += 2
    elif len(set(suits[:3])) == 2:
        wet_score += 1
    if best_window >= 4:
        wet_score += 2
    elif best_window == 3:
        wet_score += 1
    if paired:
        return "paired"
    if monotone:
        return "monotone"
    if wet_score >= 3:
        return "wet"
    if broadways >= 2:
        return "high"
    if wet_score <= 1:
        return "dry"
    return "medium"


@dataclass
class Event:
    street: str
    player: str
    action: str
    raw_action: str
    amount: float = 0.0
    pot_before: float = 0.0
    size_pot_pct: float = math.nan
    idx: int = 0


@dataclass
class Hand:
    meta: Dict[str, Any]
    seats: Dict[int, str]
    positions: Dict[str, str]
    postflop_order: List[str]
    hero_cards: List[str]
    boards: Dict[str, List[str]]
    events: Dict[str, List[Event]]
    active_at_street_start: Dict[str, List[str]]
    hero_net: float
    hero_net_bb: float
    hero_contributed: float
    total_contributed: float
    preflop_raises: int
    last_preflop_raiser: str
    hero_vpip: bool
    hero_pfr: bool
    hero_3bet: bool
    hero_open: bool
    hero_fold_street: str
    pot_type: str
    preflop_role: str

    @property
    def hand_id(self) -> str:
        return str(self.meta.get("hand_id", ""))

    @property
    def bb(self) -> float:
        return float(self.meta.get("bb") or 0.0)

    @property
    def stake_label(self) -> str:
        return str(self.meta.get("stake_label", ""))

    @property
    def desc(self) -> str:
        return str(self.meta.get("desc", ""))

    @property
    def hero_position(self) -> str:
        return str(self.meta.get("hero_position", ""))

    def active_players(self, street: str) -> List[str]:
        active = set(self.active_at_street_start.get(street, []))
        return [p for p in self.postflop_order if p in active]

    def hero_is_ip(self, street: str = "FLOP", hero: str = "Hero") -> Optional[bool]:
        active_order = self.active_players(street)
        if hero not in active_order or len(active_order) < 2:
            return None
        return active_order[-1] == hero

    def is_hu(self, street: str = "FLOP") -> bool:
        return len(self.active_players(street)) == 2


def parse_hand(hand_text: str, hero: str = "Hero") -> Optional[Hand]:
    lines = hand_text.splitlines()
    if not lines:
        return None
    meta = parse_header(lines[0])
    if not meta:
        return None

    button_match = re.search(r"Seat #(\d+) is the button", hand_text)
    button_seat = int(button_match.group(1)) if button_match else None
    seats: Dict[int, str] = {}
    stacks: Dict[str, float] = {}
    for line in lines:
        m = re.match(r"Seat (\d+): (.+?) \(₮([0-9.]+) in chips\)", line)
        if m:
            seat = int(m.group(1))
            name = m.group(2)
            seats[seat] = name
            stacks[name] = float(m.group(3))
    if hero not in seats.values():
        return None

    positions = assign_positions(seats, button_seat)
    pf_order = postflop_order_players(seats, button_seat)
    meta["hero_position"] = positions.get(hero, "")
    meta["n_players_dealt"] = len(seats)

    hero_cards: List[str] = []
    for line in lines:
        if line.startswith(f"Dealt to {hero} "):
            hero_cards = parse_all_cards_from_brackets(line)
            break
    meta.update(classify_starting_hand(hero_cards))

    street = "PREFLOP"
    street_contrib: Dict[str, float] = collections.defaultdict(float)
    total_contrib: Dict[str, float] = collections.defaultdict(float)
    active = set(seats.values())
    active_at_street_start: Dict[str, List[str]] = {"PREFLOP": list(active)}
    boards: Dict[str, List[str]] = {}
    events: Dict[str, List[Event]] = {"PREFLOP": [], "FLOP": [], "TURN": [], "RIVER": []}

    pot = 0.0
    preflop_raises = 0
    last_preflop_raiser = ""
    hero_vpip = False
    hero_pfr = False
    hero_3bet = False
    hero_open = False
    hero_fold_street = ""
    hero_collected = 0.0
    event_idx = 0

    for line in lines[1:]:
        sm = re.match(r"\*\*\* (?:(FIRST|SECOND|THIRD) )?(FLOP|TURN|RIVER) \*\*\* (.*)", line)
        if sm:
            marker, new_street, rest = sm.groups()
            street = new_street
            if marker in (None, "FIRST"):
                boards[new_street] = parse_all_cards_from_brackets(rest)
                active_at_street_start[new_street] = list(active)
            street_contrib = collections.defaultdict(float)
            continue

        if line.startswith("Board [") and "RIVER" not in boards:
            cards = parse_all_cards_from_brackets(line)
            if len(cards) >= 3:
                boards.setdefault("FLOP", cards[:3])
            if len(cards) >= 4:
                boards.setdefault("TURN", cards[:4])
            if len(cards) >= 5:
                boards.setdefault("RIVER", cards[:5])

        matched = False
        for raw_action, regex in ACTION_PATTERNS:
            m = regex.match(line)
            if not m:
                continue
            matched = True
            player = m.group(1)
            amount = 0.0
            total_to: Optional[float] = None
            if raw_action in ("posts_ante", "posts_sb", "posts_bb", "bets", "calls", "allin", "return", "collected"):
                amount = float(m.group(2))
            elif raw_action == "raises":
                total_to = float(m.group(3))
                amount = max(0.0, total_to - street_contrib[player])

            pot_before = pot
            if raw_action == "posts_ante":
                total_contrib[player] += amount
                pot += amount
            elif raw_action in ("posts_sb", "posts_bb"):
                total_contrib[player] += amount
                street_contrib[player] += amount
                pot += amount
            elif raw_action in ("bets", "calls"):
                total_contrib[player] += amount
                street_contrib[player] += amount
                pot += amount
            elif raw_action == "raises":
                total_contrib[player] += amount
                street_contrib[player] = total_to or street_contrib[player]
                pot += amount
            elif raw_action == "allin":
                total_contrib[player] += amount
                street_contrib[player] += amount
                pot += amount
            elif raw_action == "return":
                total_contrib[player] -= amount
                street_contrib[player] = max(0.0, street_contrib[player] - amount)
                pot -= amount
            elif raw_action == "collected":
                if player == hero:
                    hero_collected += amount
            elif raw_action == "folds":
                active.discard(player)
                if player == hero and not hero_fold_street:
                    hero_fold_street = street

            if street == "PREFLOP":
                if raw_action == "raises":
                    if player == hero:
                        hero_vpip = True
                        hero_pfr = True
                        if preflop_raises == 0:
                            hero_open = True
                        if preflop_raises >= 1:
                            hero_3bet = True
                    preflop_raises += 1
                    last_preflop_raiser = player
                elif raw_action in ("calls", "allin") and player == hero:
                    hero_vpip = True

            action = ACTION_MAP.get(raw_action)
            if action:
                size_pct = safe_div(amount, pot_before, default=math.nan) if action in ("bet", "call", "raise", "allin") else math.nan
                event_idx += 1
                events.setdefault(street, []).append(Event(street=street, player=player, action=action, raw_action=raw_action, amount=amount, pot_before=pot_before, size_pot_pct=size_pct, idx=event_idx))
            break
        if matched:
            continue

    hero_contributed = max(0.0, total_contrib.get(hero, 0.0))
    total_contributed = sum(max(0.0, v) for v in total_contrib.values())
    hero_net = hero_collected - hero_contributed
    bb = float(meta.get("bb") or 0.0)
    hero_net_bb = safe_div(hero_net, bb, default=0.0)

    if meta.get("is_bombpot"):
        pot_type = "bombpot"
    elif preflop_raises == 0:
        pot_type = "limped"
    elif preflop_raises == 1:
        pot_type = "srp"
    elif preflop_raises == 2:
        pot_type = "3bet"
    else:
        pot_type = "4bet+"

    if hero_fold_street == "PREFLOP" and hero_vpip:
        preflop_role = "vpip_folded_pre"
    elif hero_fold_street == "PREFLOP":
        preflop_role = "folded_pre"
    elif hero_3bet:
        preflop_role = "3bettor"
    elif hero_open:
        preflop_role = "opener"
    elif hero_vpip and meta.get("hero_position") in ("SB", "BB", "BTN/SB"):
        preflop_role = "blind_defender_or_caller"
    elif hero_vpip:
        preflop_role = "caller"
    elif meta.get("hero_position") == "BB" and preflop_raises == 0 and hero_fold_street != "PREFLOP":
        preflop_role = "bb_check"
    else:
        preflop_role = "other"

    return Hand(
        meta=meta,
        seats=seats,
        positions=positions,
        postflop_order=pf_order,
        hero_cards=hero_cards,
        boards=boards,
        events=events,
        active_at_street_start=active_at_street_start,
        hero_net=hero_net,
        hero_net_bb=hero_net_bb,
        hero_contributed=hero_contributed,
        total_contributed=total_contributed,
        preflop_raises=preflop_raises,
        last_preflop_raiser=last_preflop_raiser,
        hero_vpip=hero_vpip,
        hero_pfr=hero_pfr,
        hero_3bet=hero_3bet,
        hero_open=hero_open,
        hero_fold_street=hero_fold_street,
        pot_type=pot_type,
        preflop_role=preflop_role,
    )


def first_event(events: List[Event], player: Optional[str] = None, actions: Optional[set] = None, after_idx: int = -1) -> Optional[Event]:
    for ev in events:
        if ev.idx <= after_idx:
            continue
        if player is not None and ev.player != player:
            continue
        if actions is not None and ev.action not in actions:
            continue
        return ev
    return None


def next_event_by_player(events: List[Event], player: str, after_event: Event) -> Optional[Event]:
    return first_event(events, player=player, after_idx=after_event.idx)


def first_nonhero_after(events: List[Event], hero: str, after_event: Event) -> Optional[Event]:
    for ev in events:
        if ev.idx > after_event.idx and ev.player != hero:
            return ev
    return None


def any_nonhero_aggro_before(events: List[Event], hero_event: Event, hero: str) -> bool:
    return any(ev.idx < hero_event.idx and ev.player != hero and ev.action in AGGRO_ACTIONS for ev in events)


def all_nonhero_checked_before(events: List[Event], hero_event: Event, hero: str) -> bool:
    before = [ev for ev in events if ev.idx < hero_event.idx and ev.player != hero]
    return bool(before) and all(ev.action == "check" for ev in before)


def hero_response_category(ev: Optional[Event]) -> str:
    if ev is None:
        return "no_response"
    if ev.action == "fold":
        return "fold"
    if ev.action == "call":
        return "call"
    if ev.action in ("raise", "allin"):
        return "raise_or_allin"
    if ev.action == "check":
        return "check"
    if ev.action == "bet":
        return "bet"
    return ev.action


def villain_response_category(ev: Optional[Event]) -> str:
    if ev is None:
        return "no_response"
    if ev.action == "fold":
        return "fold"
    if ev.action == "call":
        return "call"
    if ev.action in ("raise", "allin"):
        return "raise_or_allin"
    if ev.action == "check":
        return "check"
    if ev.action == "bet":
        return "bet"
    return ev.action



def action_category(ev: Optional[Event]) -> str:
    if ev is None:
        return "no_response"
    if ev.action in ("bet", "allin") and ev.raw_action in ("bets", "allin"):
        return "bet"
    if ev.action in ("raise", "allin") and ev.raw_action in ("raises", "allin"):
        return "raise_or_allin"
    if ev.action == "fold":
        return "fold"
    if ev.action == "call":
        return "call"
    if ev.action == "check":
        return "check"
    if ev.action == "bet":
        return "bet"
    return ev.action


def response_category(ev: Optional[Event]) -> str:
    if ev is None:
        return "no_response"
    if ev.action == "fold":
        return "fold"
    if ev.action == "call":
        return "call"
    if ev.action in ("raise", "allin"):
        return "raise_or_allin"
    if ev.action == "check":
        return "check"
    if ev.action == "bet":
        return "bet"
    return ev.action


def street_bucket(h: Hand, street: str) -> str:
    if street == "FLOP":
        return board_bucket(h.boards.get("FLOP", []))
    if street == "TURN":
        return board_bucket(h.boards.get("TURN", []))
    if street == "RIVER":
        return board_bucket(h.boards.get("RIVER", []))
    return "no_board"


def size_bucket(size_pct: float) -> str:
    if size_pct is None or math.isnan(size_pct):
        return ""
    pctv = size_pct * 100.0
    if pctv <= 35:
        return "<=35%"
    if pctv <= 55:
        return "36-55%"
    if pctv <= 80:
        return "56-80%"
    if pctv <= 110:
        return "81-110%"
    return ">110%"


def player_pos(h: Hand, player: str) -> str:
    return h.positions.get(player, "")


def relative_position(h: Hand, street: str, player: str) -> str:
    order = h.active_players(street)
    if player not in order:
        return ""
    if len(order) == 2:
        return "IP" if order[-1] == player else "OOP"
    if order[-1] == player:
        return "IP_multiway"
    if order[0] == player:
        return "first_multiway"
    return "middle_multiway"


def include_player(player: str, args: argparse.Namespace) -> bool:
    if args.include_hero:
        return True
    return player != args.hero


def make_pool_record(
    h: Hand,
    spot_id: str,
    spot_name: str,
    street: str,
    actor: str,
    action: str,
    size_pct: float = math.nan,
    facing_action: str = "",
    aggressor: str = "",
    context: str = "",
    facing_size_pct: float = math.nan,
) -> Dict[str, Any]:
    rec = {
        "spot_id": spot_id,
        "spot_name": spot_name,
        "hand_id": h.hand_id,
        "dt": h.meta.get("dt", ""),
        "stake_label": h.stake_label,
        "desc": h.desc,
        "pot_type": h.pot_type,
        "street": street,
        "actor_position": player_pos(h, actor),
        "actor_ip_oop": relative_position(h, street, actor),
        "board_bucket": street_bucket(h, street),
        "action": action,
        "facing_action": facing_action,
        "aggressor_position": player_pos(h, aggressor) if aggressor else "",
        "aggressor_ip_oop": relative_position(h, street, aggressor) if aggressor else "",
        "size_pot_pct": round(size_pct, 4) if size_pct is not None and not math.isnan(size_pct) else "",
        "size_bucket": size_bucket(size_pct),
        "facing_size_pot_pct": round(facing_size_pct, 4) if facing_size_pct is not None and not math.isnan(facing_size_pct) else "",
        "facing_size_bucket": size_bucket(facing_size_pct),
        "context": context,
    }
    if getattr(h, "debug_players", False):
        rec["actor"] = actor
        rec["aggressor"] = aggressor
    return rec


def first_action_by_any(events: List[Event], players: List[str], after_idx: int = -1) -> Optional[Event]:
    allowed = set(players)
    for ev in events:
        if ev.idx <= after_idx:
            continue
        if ev.player in allowed:
            return ev
    return None


def next_action_by_player(events: List[Event], player: str, after_idx: int) -> Optional[Event]:
    for ev in events:
        if ev.idx > after_idx and ev.player == player:
            return ev
    return None


def first_aggressive_after(events: List[Event], player: str, after_idx: int) -> Optional[Event]:
    for ev in events:
        if ev.idx > after_idx and ev.player == player and ev.action in AGGRO_ACTIONS:
            return ev
    return None


def extract_required_pool_records(h: Hand, args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Extract population-level opportunities/responses.

    This intentionally does not aggregate by player. By default Hero's own actions are
    excluded from the actor side, but opponent responses to Hero actions may still be
    counted because they are opponent decisions. Use --include-hero to include Hero as
    a normal population actor.

    v2 nodes added:
    - Fold/call/raise vs flop c-bet, split later by actor IP/OOP.
    - Fold/call/raise vs turn barrel after calling flop c-bet.
    - OOP response vs IP flop stab after PFA OOP missed flop c-bet.
    - OOP response vs IP turn stab after PFA OOP missed turn c-bet.
    - PFA turn barrel/give-up after flop c-bet called.
    - PFA fold/call/raise after betting flop, checking turn, and facing stab.
    - Fold/call/raise vs river barrel after flop-call + turn-call.
    - Delayed c-bet frequency and fold/call/raise vs delayed c-bet, split OOP/IP.
    """
    recs: List[Dict[str, Any]] = []
    for obj in [h]:
        setattr(obj, "debug_players", bool(args.debug_players))

    pfa = h.last_preflop_raiser
    has_pfa = bool(pfa) and h.pot_type in {"srp", "3bet", "4bet+"}

    flop_events = h.events.get("FLOP", [])
    turn_events = h.events.get("TURN", [])
    river_events = h.events.get("RIVER", [])
    if not flop_events:
        return recs

    flop_order = h.active_players("FLOP")
    turn_order = h.active_players("TURN")
    river_order = h.active_players("RIVER")

    def other_player(order: List[str], player: str) -> str:
        for x in order:
            if x != player:
                return x
        return ""

    def pfa_is_ip(order: List[str]) -> Optional[bool]:
        if not has_pfa or len(order) != 2 or pfa not in order:
            return None
        return order[-1] == pfa

    def pfa_flop_cbet_and_defender_call() -> Tuple[Optional[str], Optional[Event], Optional[Event]]:
        """Return defender, PFA flop c-bet, defender call for clean HU flop c-bet/call nodes."""
        if not has_pfa or len(flop_order) != 2 or pfa not in flop_order:
            return None, None, None
        defender = other_player(flop_order, pfa)
        if not defender:
            return None, None, None

        if flop_order[0] == pfa:  # PFA OOP acts first.
            pfa_bet = first_action_by_any(flop_events, [pfa])
            if not pfa_bet or pfa_bet.action not in AGGRO_ACTIONS:
                return defender, pfa_bet, None
            def_resp = next_action_by_player(flop_events, defender, pfa_bet.idx)
            if def_resp and def_resp.action == "call":
                return defender, pfa_bet, def_resp
            return defender, pfa_bet, None

        # PFA IP: OOP defender must check, then PFA c-bets.
        def_first = first_action_by_any(flop_events, [defender])
        if not def_first or def_first.action != "check":
            return defender, None, None
        pfa_bet = next_action_by_player(flop_events, pfa, def_first.idx)
        if not pfa_bet or pfa_bet.action not in AGGRO_ACTIONS:
            return defender, pfa_bet, None
        def_resp = next_action_by_player(flop_events, defender, pfa_bet.idx)
        if def_resp and def_resp.action == "call":
            return defender, pfa_bet, def_resp
        return defender, pfa_bet, None

    def pfa_turn_action_after_flop_call(defender: str) -> Tuple[Optional[Event], bool]:
        """Return PFA turn action and whether PFA had a clean c-bet/barrel opportunity."""
        if not defender or len(turn_order) != 2 or pfa not in turn_order or defender not in turn_order:
            return None, False
        if turn_order[0] == pfa:  # PFA OOP acts first.
            ev = first_action_by_any(turn_events, [pfa])
            return ev, ev is not None
        # PFA IP: defender must check first.
        def_first = first_action_by_any(turn_events, [defender])
        if not def_first or def_first.action != "check":
            return None, False
        ev = next_action_by_player(turn_events, pfa, def_first.idx)
        return ev, ev is not None

    def pfa_river_action_after_turn_call(defender: str) -> Tuple[Optional[Event], bool]:
        """Return PFA river action and whether PFA had a clean river barrel opportunity."""
        if not defender or len(river_order) != 2 or pfa not in river_order or defender not in river_order:
            return None, False
        if river_order[0] == pfa:  # PFA OOP acts first.
            ev = first_action_by_any(river_events, [pfa])
            return ev, ev is not None
        # PFA IP: defender must check first.
        def_first = first_action_by_any(river_events, [defender])
        if not def_first or def_first.action != "check":
            return None, False
        ev = next_action_by_player(river_events, pfa, def_first.idx)
        return ev, ev is not None

    # A) Fold/call/raise vs flop c-bet, clean HU flop.
    if has_pfa and len(flop_order) == 2 and pfa in flop_order:
        defender = other_player(flop_order, pfa)
        pfa_ip = pfa_is_ip(flop_order)
        pfa_bet: Optional[Event] = None
        def_resp: Optional[Event] = None
        if pfa_ip is False:  # PFA OOP.
            pfa_bet = first_action_by_any(flop_events, [pfa])
            if pfa_bet and pfa_bet.action in AGGRO_ACTIONS:
                def_resp = next_action_by_player(flop_events, defender, pfa_bet.idx)
        elif pfa_ip is True:  # PFA IP after OOP check.
            def_first = first_action_by_any(flop_events, [defender])
            if def_first and def_first.action == "check":
                pfa_bet = next_action_by_player(flop_events, pfa, def_first.idx)
                if pfa_bet and pfa_bet.action in AGGRO_ACTIONS:
                    def_resp = next_action_by_player(flop_events, defender, pfa_bet.idx)
        if pfa_bet and pfa_bet.action in AGGRO_ACTIONS and def_resp and include_player(defender, args):
            recs.append(make_pool_record(
                h,
                "FOLD_TO_FLOP_CBET",
                "Fold/call/raise vs flop c-bet",
                "FLOP",
                defender,
                response_category(def_resp),
                def_resp.size_pot_pct,
                facing_action="flop_cbet",
                aggressor=pfa,
                context="PFA c-bets flop HU; defender responds",
                facing_size_pct=pfa_bet.size_pot_pct,
            ))

    # B) Existing: Bet IP vs missed flop c-bet + NEW OOP response vs that stab.
    # PFA is OOP on flop, checks first, IP player decides bet/check. If IP bets, OOP responds fold/call/xr.
    if has_pfa and len(flop_order) == 2 and pfa in flop_order and flop_order[-1] != pfa:
        pfa_flop_first = first_action_by_any(flop_events, [pfa])
        ip_player = flop_order[-1]
        if pfa_flop_first and pfa_flop_first.action == "check":
            ip_action = next_action_by_player(flop_events, ip_player, pfa_flop_first.idx)
            if ip_action and include_player(ip_player, args):
                recs.append(make_pool_record(
                    h,
                    "BET_IP_VS_MISSED_FLOP_CB",
                    "Bet IP vs missed flop c-bet",
                    "FLOP",
                    ip_player,
                    action_category(ip_action),
                    ip_action.size_pot_pct,
                    facing_action="missed_flop_cbet",
                    aggressor=pfa,
                    context="PFA OOP checks flop; IP player acts",
                ))
            if ip_action and ip_action.action in AGGRO_ACTIONS:
                oop_resp = next_action_by_player(flop_events, pfa, ip_action.idx)
                if oop_resp and include_player(pfa, args):
                    recs.append(make_pool_record(
                        h,
                        "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB",
                        "OOP fold/call/check-raise vs IP flop stab after missed c-bet",
                        "FLOP",
                        pfa,
                        response_category(oop_resp),
                        oop_resp.size_pot_pct,
                        facing_action="ip_flop_stab_after_missed_cbet",
                        aggressor=ip_player,
                        context="PFA OOP checks flop, IP stabs; OOP responds",
                        facing_size_pct=ip_action.size_pot_pct,
                    ))

    # C) Existing: Bet IP vs missed turn c-bet + NEW OOP response vs turn stab.
    # PFA OOP c-bets flop, gets called, then checks turn to IP.
    if has_pfa and len(flop_order) == 2 and len(turn_order) == 2 and pfa in flop_order and pfa in turn_order and flop_order[-1] != pfa and turn_order[-1] != pfa:
        ip_player = flop_order[-1]
        pfa_flop_first = first_action_by_any(flop_events, [pfa])
        if pfa_flop_first and pfa_flop_first.action in AGGRO_ACTIONS:
            ip_flop_resp = next_action_by_player(flop_events, ip_player, pfa_flop_first.idx)
            if ip_flop_resp and ip_flop_resp.action == "call":
                pfa_turn_first = first_action_by_any(turn_events, [pfa])
                if pfa_turn_first and pfa_turn_first.action == "check":
                    ip_turn_action = next_action_by_player(turn_events, ip_player, pfa_turn_first.idx)
                    if ip_turn_action and include_player(ip_player, args):
                        recs.append(make_pool_record(
                            h,
                            "BET_IP_VS_MISSED_TURN_CB",
                            "Bet IP vs missed turn c-bet after flop c-bet/call",
                            "TURN",
                            ip_player,
                            action_category(ip_turn_action),
                            ip_turn_action.size_pot_pct,
                            facing_action="missed_turn_cbet",
                            aggressor=pfa,
                            context="PFA OOP c-bets flop, IP calls, PFA checks turn; IP player acts",
                        ))
                    if ip_turn_action and ip_turn_action.action in AGGRO_ACTIONS:
                        oop_turn_resp = next_action_by_player(turn_events, pfa, ip_turn_action.idx)
                        if oop_turn_resp and include_player(pfa, args):
                            recs.append(make_pool_record(
                                h,
                                "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB",
                                "OOP fold/call/check-raise vs IP turn stab after missed turn c-bet",
                                "TURN",
                                pfa,
                                response_category(oop_turn_resp),
                                oop_turn_resp.size_pot_pct,
                                facing_action="ip_turn_stab_after_missed_cbet",
                                aggressor=ip_player,
                                context="PFA OOP c-bets flop, gets called, checks turn, IP stabs; OOP responds",
                                facing_size_pct=ip_turn_action.size_pot_pct,
                            ))

    # D) Turn probe after flop checkback + fold/call/raise vs turn probe.
    # PFA IP checks back flop after OOP check. OOP gets turn probe opportunity. If OOP bets, PFA response is fold/call/raise.
    if has_pfa and len(flop_order) == 2 and len(turn_order) == 2 and pfa in flop_order and pfa in turn_order and flop_order[-1] == pfa and turn_order[-1] == pfa:
        oop_player = flop_order[0]
        oop_flop_first = first_action_by_any(flop_events, [oop_player])
        if oop_flop_first and oop_flop_first.action == "check":
            pfa_flop_action = next_action_by_player(flop_events, pfa, oop_flop_first.idx)
            if pfa_flop_action and pfa_flop_action.action == "check":
                oop_turn_first = first_action_by_any(turn_events, [oop_player])
                if oop_turn_first and include_player(oop_player, args):
                    recs.append(make_pool_record(
                        h,
                        "TURN_PROBE_AFTER_FLOP_CHECKBACK",
                        "Turn probe after PFA checks back flop",
                        "TURN",
                        oop_player,
                        action_category(oop_turn_first),
                        oop_turn_first.size_pot_pct,
                        facing_action="flop_checkback",
                        aggressor=pfa,
                        context="OOP checks flop, PFA IP checks back, OOP acts first on turn",
                    ))
                if oop_turn_first and oop_turn_first.action in AGGRO_ACTIONS:
                    pfa_response = next_action_by_player(turn_events, pfa, oop_turn_first.idx)
                    if pfa_response and include_player(pfa, args):
                        recs.append(make_pool_record(
                            h,
                            "FOLD_TO_TURN_PROBE",
                            "Fold/call/raise vs turn probe",
                            "TURN",
                            pfa,
                            response_category(pfa_response),
                            pfa_response.size_pot_pct,
                            facing_action="turn_probe",
                            aggressor=oop_player,
                            context="PFA IP checked back flop and now faces OOP turn probe",
                            facing_size_pct=oop_turn_first.size_pot_pct,
                        ))

    # E) PFA turn barrel/give-up after flop c-bet called; and defender fold/call/raise vs turn barrel.
    defender, flop_cbet, flop_call = pfa_flop_cbet_and_defender_call()
    if has_pfa and defender and flop_cbet and flop_call:
        pfa_turn_ev, pfa_turn_opportunity = pfa_turn_action_after_flop_call(defender)
        if pfa_turn_opportunity and pfa_turn_ev and include_player(pfa, args):
            recs.append(make_pool_record(
                h,
                "PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED",
                "PFA turn barrel/give-up after flop c-bet called",
                "TURN",
                pfa,
                action_category(pfa_turn_ev),
                pfa_turn_ev.size_pot_pct,
                facing_action="flop_cbet_called",
                aggressor=defender,
                context="PFA c-bets flop HU, defender calls, PFA has clean turn barrel/check opportunity",
            ))
        if pfa_turn_ev and pfa_turn_ev.action in AGGRO_ACTIONS:
            def_turn_resp = next_action_by_player(turn_events, defender, pfa_turn_ev.idx)
            if def_turn_resp and include_player(defender, args):
                recs.append(make_pool_record(
                    h,
                    "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL",
                    "Fold/call/raise vs turn barrel after calling flop c-bet",
                    "TURN",
                    defender,
                    response_category(def_turn_resp),
                    def_turn_resp.size_pot_pct,
                    facing_action="turn_barrel_after_flop_call",
                    aggressor=pfa,
                    context="Defender called flop c-bet and now faces PFA turn barrel",
                    facing_size_pct=pfa_turn_ev.size_pot_pct,
                ))
            if def_turn_resp and def_turn_resp.action == "call":
                pfa_river_ev, pfa_river_opportunity = pfa_river_action_after_turn_call(defender)
                if pfa_river_opportunity and pfa_river_ev and pfa_river_ev.action in AGGRO_ACTIONS:
                    def_river_resp = next_action_by_player(river_events, defender, pfa_river_ev.idx)
                    if def_river_resp and include_player(defender, args):
                        recs.append(make_pool_record(
                            h,
                            "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL",
                            "Fold/call/raise vs river barrel after flop-call + turn-call",
                            "RIVER",
                            defender,
                            response_category(def_river_resp),
                            def_river_resp.size_pot_pct,
                            facing_action="river_barrel_after_flop_turn_call",
                            aggressor=pfa,
                            context="Defender called flop c-bet and turn barrel, then faces PFA river barrel",
                            facing_size_pct=pfa_river_ev.size_pot_pct,
                        ))
        elif pfa_turn_ev and pfa_turn_ev.action == "check":
            # PFA bet flop, got called, checks turn; if defender bets, count PFA response.
            def_turn_bet = next_action_by_player(turn_events, defender, pfa_turn_ev.idx) if turn_order and turn_order[0] == pfa else None
            # If PFA is IP, his check comes after defender checked, so no defender stab can happen after PFA check on same street.
            if def_turn_bet and def_turn_bet.action in AGGRO_ACTIONS:
                pfa_resp = next_action_by_player(turn_events, pfa, def_turn_bet.idx)
                if pfa_resp and include_player(pfa, args):
                    recs.append(make_pool_record(
                        h,
                        "PFA_RESPONSE_AFTER_TURN_GIVEUP",
                        "PFA fold/call/raise after flop c-bet, turn check, faced stab",
                        "TURN",
                        pfa,
                        response_category(pfa_resp),
                        pfa_resp.size_pot_pct,
                        facing_action="turn_stab_after_pfa_giveup",
                        aggressor=defender,
                        context="PFA c-bets flop, gets called, checks turn, defender bets; PFA responds",
                        facing_size_pct=def_turn_bet.size_pot_pct,
                    ))

    # F) Delayed c-bet frequency + fold/call/raise vs delayed c-bet, split by responder IP/OOP.
    # Delayed c-bet = PFA declines flop c-bet, flop checks through, then PFA bets turn when he has first clean betting opportunity.
    if has_pfa and len(flop_order) == 2 and len(turn_order) == 2 and pfa in flop_order and pfa in turn_order:
        defender = other_player(flop_order, pfa)
        if defender and defender in turn_order:
            pfa_flop_action: Optional[Event] = None
            flop_checked_through = False
            if flop_order[0] == pfa:  # PFA OOP checks, IP checks back.
                pfa_first = first_action_by_any(flop_events, [pfa])
                if pfa_first and pfa_first.action == "check":
                    def_flop_action = next_action_by_player(flop_events, defender, pfa_first.idx)
                    if def_flop_action and def_flop_action.action == "check":
                        pfa_flop_action = pfa_first
                        flop_checked_through = True
            else:  # PFA IP checks back after OOP check.
                def_first = first_action_by_any(flop_events, [defender])
                if def_first and def_first.action == "check":
                    pfa_action = next_action_by_player(flop_events, pfa, def_first.idx)
                    if pfa_action and pfa_action.action == "check":
                        pfa_flop_action = pfa_action
                        flop_checked_through = True

            if flop_checked_through and pfa_flop_action:
                pfa_turn_ev: Optional[Event] = None
                clean_delayed_opportunity = False
                if turn_order[0] == pfa:  # PFA OOP acts first on turn.
                    pfa_turn_ev = first_action_by_any(turn_events, [pfa])
                    clean_delayed_opportunity = pfa_turn_ev is not None
                else:  # PFA IP only has delayed c-bet opportunity if OOP checks turn.
                    def_turn_first = first_action_by_any(turn_events, [defender])
                    if def_turn_first and def_turn_first.action == "check":
                        pfa_turn_ev = next_action_by_player(turn_events, pfa, def_turn_first.idx)
                        clean_delayed_opportunity = pfa_turn_ev is not None

                if clean_delayed_opportunity and pfa_turn_ev:
                    pfa_role = "IP" if turn_order[-1] == pfa else "OOP"
                    responder = defender
                    responder_role = "IP" if turn_order[-1] == responder else "OOP"
                    if include_player(pfa, args):
                        recs.append(make_pool_record(
                            h,
                            f"DELAYED_CBET_{pfa_role}",
                            f"Delayed c-bet frequency by PFA {pfa_role}",
                            "TURN",
                            pfa,
                            action_category(pfa_turn_ev),
                            pfa_turn_ev.size_pot_pct,
                            facing_action="flop_checked_through",
                            aggressor=defender,
                            context=f"PFA {pfa_role} checks back/through flop, then has delayed c-bet opportunity on turn",
                        ))
                    if pfa_turn_ev.action in AGGRO_ACTIONS:
                        def_resp = next_action_by_player(turn_events, responder, pfa_turn_ev.idx)
                        if def_resp and include_player(responder, args):
                            recs.append(make_pool_record(
                                h,
                                f"FOLD_TO_DELAYED_CBET_{responder_role}",
                                f"Fold/call/raise vs delayed c-bet, responder {responder_role}",
                                "TURN",
                                responder,
                                response_category(def_resp),
                                def_resp.size_pot_pct,
                                facing_action="delayed_cbet",
                                aggressor=pfa,
                                context=f"PFA delayed c-bets turn after flop checked through; responder is {responder_role}",
                                facing_size_pct=pfa_turn_ev.size_pot_pct,
                            ))

    # G) Fold to flop check-raise:
    # First player checks, later raises over the bettor. Count bettor response.
    if len(flop_order) == 2:
        first_player, second_player = flop_order[0], flop_order[1]
        first_ev = first_action_by_any(flop_events, [first_player])
        if first_ev and first_ev.action == "check":
            bettor_ev = next_action_by_player(flop_events, second_player, first_ev.idx)
            if bettor_ev and bettor_ev.action in AGGRO_ACTIONS:
                xr_ev = next_action_by_player(flop_events, first_player, bettor_ev.idx)
                if xr_ev and xr_ev.action in {"raise", "allin"}:
                    bettor_resp = next_action_by_player(flop_events, second_player, xr_ev.idx)
                    if bettor_resp and include_player(second_player, args):
                        recs.append(make_pool_record(
                            h,
                            "FOLD_TO_FLOP_CHECK_RAISE",
                            "Fold/call/3bet vs flop check-raise",
                            "FLOP",
                            second_player,
                            response_category(bettor_resp),
                            bettor_resp.size_pot_pct,
                            facing_action="flop_check_raise",
                            aggressor=first_player,
                            context="OOP checks, IP bets, OOP check-raises; bettor responds",
                            facing_size_pct=xr_ev.size_pot_pct,
                        ))

    # H) Fold to turn check-raise:
    # Same pattern on turn, regardless of who was PFA, as long as the street starts HU.
    if len(turn_order) == 2:
        first_player, second_player = turn_order[0], turn_order[1]
        first_ev = first_action_by_any(turn_events, [first_player])
        if first_ev and first_ev.action == "check":
            bettor_ev = next_action_by_player(turn_events, second_player, first_ev.idx)
            if bettor_ev and bettor_ev.action in AGGRO_ACTIONS:
                xr_ev = next_action_by_player(turn_events, first_player, bettor_ev.idx)
                if xr_ev and xr_ev.action in {"raise", "allin"}:
                    bettor_resp = next_action_by_player(turn_events, second_player, xr_ev.idx)
                    if bettor_resp and include_player(second_player, args):
                        recs.append(make_pool_record(
                            h,
                            "FOLD_TO_TURN_CHECK_RAISE",
                            "Fold/call/3bet vs turn check-raise",
                            "TURN",
                            second_player,
                            response_category(bettor_resp),
                            bettor_resp.size_pot_pct,
                            facing_action="turn_check_raise",
                            aggressor=first_player,
                            context="OOP checks, IP bets, OOP check-raises turn; bettor responds",
                            facing_size_pct=xr_ev.size_pot_pct,
                        ))

    return recs


DEFAULT_BENCHMARKS: Dict[str, Dict[str, Any]] = {
    "FOLD_TO_FLOP_CBET": {
        "metric": "fold_pct", "low": 25.0, "high": 45.0,
        "low_note": "defenders are very sticky vs flop c-bet; c-bet more value/equity-heavy and plan turns",
        "high_note": "defenders overfold vs flop c-bet; more bluff/protection c-bets may be available",
    },
    "BET_IP_VS_MISSED_FLOP_CB": {
        "metric": "bet_pct", "low": 45.0, "high": 70.0,
        "low_note": "pool under-stabs vs missed flop c-bet; check response stats before pure-bluffing more",
        "high_note": "pool auto-stabs too much vs missed flop c-bet; OOP can check-raise/call more strategically",
    },
    "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB": {
        "metric": "fold_pct", "low": 25.0, "high": 50.0,
        "low_note": "OOP is sticky vs IP flop stab after missed c-bet; IP stabs should be value/equity-heavy",
        "high_note": "OOP overfolds vs IP flop stab after missed c-bet; IP can attack more often",
    },
    "BET_IP_VS_MISSED_TURN_CB": {
        "metric": "bet_pct", "low": 45.0, "high": 70.0,
        "low_note": "pool under-stabs vs missed turn c-bet after calling flop",
        "high_note": "pool attacks missed turn c-bet very aggressively; OOP turn checks need protection/traps",
    },
    "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB": {
        "metric": "fold_pct", "low": 35.0, "high": 60.0,
        "low_note": "OOP is sticky vs IP turn stab after missed turn c-bet; IP turn stabs need value/equity",
        "high_note": "OOP overfolds vs IP turn stab after missed turn c-bet; IP can attack turn checks more",
    },
    "PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED": {
        "metric": "bet_pct", "low": 40.0, "high": 70.0,
        "low_note": "PFA under-barrels after flop c-bet gets called; possible turn give-up leak",
        "high_note": "PFA barrels turn very aggressively after flop c-bet gets called; defender can trap/check-raise more",
    },
    "PFA_RESPONSE_AFTER_TURN_GIVEUP": {
        "metric": "fold_pct", "low": 30.0, "high": 60.0,
        "low_note": "PFA does not fold much after flop c-bet / turn check / facing stab; stabs need equity/value",
        "high_note": "PFA overfolds after flop c-bet / turn check / facing stab; IP can stab turn aggressively",
    },
    "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL": {
        "metric": "fold_pct", "low": 35.0, "high": 55.0,
        "low_note": "defenders are sticky vs turn barrel after calling flop; barrel value/equity-heavy",
        "high_note": "defenders overfold vs turn barrel after calling flop; turn barrels gain fold equity",
    },
    "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL": {
        "metric": "fold_pct", "low": 40.0, "high": 65.0,
        "low_note": "defenders call down too much vs river barrel; river value-bet thinner and bluff less",
        "high_note": "defenders overfold vs river barrel after calling flop+turn; triple barrels may print",
    },
    "TURN_PROBE_AFTER_FLOP_CHECKBACK": {
        "metric": "bet_pct", "low": 35.0, "high": 60.0,
        "low_note": "OOP under-probes after flop checks through",
        "high_note": "OOP probes very aggressively after flop checks through; IP checkback range needs protection",
    },
    "FOLD_TO_TURN_PROBE": {
        "metric": "fold_pct", "low": 30.0, "high": 50.0,
        "low_note": "IP is sticky vs turn probe; probe more value/equity-heavy",
        "high_note": "IP overfolds vs turn probe; OOP can probe more often",
    },
    "DELAYED_CBET_IP": {
        "metric": "bet_pct", "low": 35.0, "high": 65.0,
        "low_note": "PFA IP under-delays after checking back flop",
        "high_note": "PFA IP delayed c-bets very aggressively; OOP can protect turn checking range",
    },
    "DELAYED_CBET_OOP": {
        "metric": "bet_pct", "low": 35.0, "high": 65.0,
        "low_note": "PFA OOP under-delays after flop checks through",
        "high_note": "PFA OOP delayed c-bets very aggressively; IP can call/raise more selectively",
    },
    "FOLD_TO_DELAYED_CBET_OOP": {
        "metric": "fold_pct", "low": 35.0, "high": 60.0,
        "low_note": "OOP is sticky vs delayed c-bet; delayed c-bets should be value/equity-heavy",
        "high_note": "OOP overfolds vs delayed c-bet; IP delayed c-bets can bluff more",
    },
    "FOLD_TO_DELAYED_CBET_IP": {
        "metric": "fold_pct", "low": 35.0, "high": 60.0,
        "low_note": "IP is sticky vs delayed c-bet; OOP delayed c-bets should be value/equity-heavy",
        "high_note": "IP overfolds vs delayed c-bet; OOP delayed c-bets can bluff more",
    },
    "FOLD_TO_FLOP_CHECK_RAISE": {
        "metric": "fold_pct", "low": 30.0, "high": 55.0,
        "low_note": "bettors do not fold enough vs flop check-raise; x/r should be value/equity-heavy",
        "high_note": "bettors overfold vs flop check-raise; check-raise bluffs gain value",
    },
    "FOLD_TO_TURN_CHECK_RAISE": {
        "metric": "fold_pct", "low": 40.0, "high": 65.0,
        "low_note": "bettors continue too much vs turn check-raise; turn x/r should be strong",
        "high_note": "bettors overfold vs turn check-raise; high-leverage turn x/r bluffs may print",
    },
}

def load_benchmarks(path: str) -> Dict[str, Dict[str, Any]]:
    if not path:
        return DEFAULT_BENCHMARKS
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Benchmarks file not found: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_BENCHMARKS)
    merged.update(data)
    return merged


def benchmark_flag(row: Dict[str, Any], benchmarks: Dict[str, Dict[str, Any]], min_sample: int) -> Tuple[str, str]:
    spot_id = row.get("spot_id", "")
    b = benchmarks.get(spot_id)
    if not b:
        return "", ""
    n = int(row.get("opportunities") or 0)
    if n < min_sample:
        return "low_sample", "sample below min-sample"
    metric = b.get("metric", "")
    val = row.get(metric, "")
    if val == "" or val is None:
        return "", ""
    try:
        v = float(val)
    except Exception:
        return "", ""
    low = float(b.get("low", 0.0))
    high = float(b.get("high", 100.0))
    if v < low:
        return "LOW", str(b.get("low_note", "below benchmark band"))
    if v > high:
        return "HIGH", str(b.get("high_note", "above benchmark band"))
    return "OK", "inside benchmark band"


def confidence_label(n: int) -> str:
    if n >= 300:
        return "HIGH"
    if n >= 100:
        return "MEDIUM"
    if n >= 30:
        return "LOW"
    return "IGNORE"


def confidence_weight(n: int) -> float:
    if n >= 300:
        return 1.0
    if n >= 100:
        return 0.7
    if n >= 30:
        return 0.4
    return 0.1


def pct_float(value: Any) -> float:
    try:
        if isinstance(value, str) and value.endswith("%"):
            value = value[:-1]
        return float(value)
    except Exception:
        return math.nan


def row_metric_value(row: Dict[str, Any], benchmarks: Dict[str, Dict[str, Any]]) -> Tuple[str, float]:
    b = benchmarks.get(str(row.get("spot_id", "")), {})
    metric = str(b.get("metric", ""))
    return metric, pct_float(row.get(metric, "")) if metric else ("", math.nan)[1]


def html_escape(x: Any) -> str:
    return html.escape(str(x))


def html_table(rows: List[Dict[str, Any]], cols: List[str], max_rows: int = 50) -> str:
    if not rows:
        return "<p><em>No rows.</em></p>"
    out = ["<table>", "<thead><tr>" + "".join(f"<th>{html_escape(c)}</th>" for c in cols) + "</tr></thead>", "<tbody>"]
    for r in rows[:max_rows]:
        cls = str(r.get("benchmark_flag", "")).lower()
        out.append(f"<tr class='{html_escape(cls)}'>" + "".join(f"<td>{html_escape(r.get(c, ''))}</td>" for c in cols) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def write_default_benchmarks(path: Path) -> None:
    path.write_text(json.dumps(DEFAULT_BENCHMARKS, indent=2, ensure_ascii=False), encoding="utf-8")


def filter_nonempty_bucket(rows: List[Dict[str, Any]], bucket_col: str) -> List[Dict[str, Any]]:
    return [r for r in rows if str(r.get(bucket_col, "")).strip()]


def summarize_records_filtered(records: List[Dict[str, Any]], benchmarks: Dict[str, Dict[str, Any]], min_sample: int, group_cols: List[str], required_col: str) -> List[Dict[str, Any]]:
    return summarize_records(filter_nonempty_bucket(records, required_col), benchmarks, min_sample, group_cols)


def summarize_records(records: List[Dict[str, Any]], benchmarks: Dict[str, Dict[str, Any]], min_sample: int, group_cols: List[str]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in records:
        key = tuple(r.get(c, "") for c in group_cols)
        groups[key].append(r)

    out: List[Dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        n = len(rows)
        actions = collections.Counter(str(r.get("action", "")) for r in rows)
        bet = actions.get("bet", 0)
        check = actions.get("check", 0)
        fold = actions.get("fold", 0)
        call = actions.get("call", 0)
        raise_ = actions.get("raise_or_allin", 0)
        no_resp = actions.get("no_response", 0)
        size_vals = []
        facing_size_vals = []
        for r in rows:
            v = r.get("size_pot_pct", "")
            if v != "":
                try:
                    size_vals.append(float(v) * 100.0)
                except Exception:
                    pass
            fv = r.get("facing_size_pot_pct", "")
            if fv != "":
                try:
                    facing_size_vals.append(float(fv) * 100.0)
                except Exception:
                    pass
        base = {c: key[i] for i, c in enumerate(group_cols)}
        base.update({
            "spot_name": rows[0].get("spot_name", ""),
            "opportunities": n,
            "bet_pct": round(100.0 * safe_div(bet, n), 1),
            "check_pct": round(100.0 * safe_div(check, n), 1),
            "fold_pct": round(100.0 * safe_div(fold, n), 1),
            "call_pct": round(100.0 * safe_div(call, n), 1),
            "raise_pct": round(100.0 * safe_div(raise_, n), 1),
            "no_response_pct": round(100.0 * safe_div(no_resp, n), 1),
            "avg_size_pct": round(safe_div(sum(size_vals), len(size_vals)), 1) if size_vals else "",
            "avg_facing_size_pct": round(safe_div(sum(facing_size_vals), len(facing_size_vals)), 1) if facing_size_vals else "",
            "raw_action_counts": json.dumps(dict(actions), ensure_ascii=False),
        })
        flag, note = benchmark_flag(base, benchmarks, min_sample)
        base["benchmark_flag"] = flag
        base["leak_note"] = note

        b = benchmarks.get(str(base.get("spot_id", "")), {})
        metric = b.get("metric", "")
        base["benchmark_metric"] = metric
        base["benchmark_low"] = b.get("low", "")
        base["benchmark_high"] = b.get("high", "")
        try:
            metric_val = float(base.get(metric, "")) if metric else math.nan
            low = float(b.get("low", math.nan))
            high = float(b.get("high", math.nan))
            if metric and not math.isnan(metric_val) and not math.isnan(low) and not math.isnan(high):
                if metric_val < low:
                    deviation = metric_val - low
                elif metric_val > high:
                    deviation = metric_val - high
                else:
                    deviation = 0.0
            else:
                deviation = 0.0
        except Exception:
            deviation = 0.0
        base["deviation_pp"] = round(deviation, 1)
        base["confidence"] = confidence_label(n)
        base["exploit_score"] = round(abs(deviation) * confidence_weight(n), 2)
        out.append(base)
    return out


def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def markdown_table(rows: List[Dict[str, Any]], cols: List[str], max_rows: int = 80) -> str:
    if not rows:
        return "_No rows._"
    rows = rows[:max_rows]
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [header, sep]
    for r in rows:
        vals = []
        for c in cols:
            vals.append(str(r.get(c, "")).replace("|", "/"))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def plot_key_leaks(summary: List[Dict[str, Any]], benchmarks: Dict[str, Dict[str, Any]], outdir: Path, min_sample: int, max_rows: int = 12) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    leaks = [r for r in summary if int(r.get("opportunities") or 0) >= min_sample and r.get("benchmark_flag") in {"LOW", "HIGH"}]
    leaks = sorted(leaks, key=lambda r: float(r.get("exploit_score") or 0.0), reverse=True)[:max_rows]
    if not leaks:
        return paths
    labels = [f"{r.get('spot_id')}\n{r.get('pot_type')} n={r.get('opportunities')}" for r in leaks]
    values = []
    lows = []
    highs = []
    for r in leaks:
        b = benchmarks.get(str(r.get("spot_id", "")), {})
        metric = str(b.get("metric", ""))
        values.append(pct_float(r.get(metric, 0.0)))
        lows.append(float(b.get("low", 0.0)))
        highs.append(float(b.get("high", 100.0)))
    fig, ax = plt.subplots(figsize=(12, max(5, 0.65 * len(labels))))
    y = list(range(len(labels)))
    ax.barh(y, values)
    for i, (lo, hi) in enumerate(zip(lows, highs)):
        ax.plot([lo, hi], [i, i], linewidth=5, alpha=0.35)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Observed %; thick segment = benchmark band")
    ax.set_title("Top benchmark deviations")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    path = outdir / "charts" / "key_leaks.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    paths.append(str(path))
    return paths


def plot_spot_pottype_heatmap(summary: List[Dict[str, Any]], outdir: Path, min_sample: int) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception:
        return paths
    rows = [r for r in summary if int(r.get("opportunities") or 0) >= min_sample and r.get("benchmark_flag") not in {"", "low_sample"}]
    if not rows:
        return paths
    # Keep the most useful spots by total sample.
    sample_by_spot: Dict[str, int] = collections.Counter()
    for r in rows:
        sample_by_spot[str(r.get("spot_id", ""))] += int(r.get("opportunities") or 0)
    spots = [s for s, _ in sample_by_spot.most_common(14)]
    pot_types = [p for p in ["srp", "3bet", "4bet+", "limped"] if any(r.get("pot_type") == p for r in rows)]
    if not spots or not pot_types:
        return paths
    mat = np.full((len(spots), len(pot_types)), np.nan)
    for i, spot in enumerate(spots):
        for j, pt in enumerate(pot_types):
            match = [r for r in rows if r.get("spot_id") == spot and r.get("pot_type") == pt]
            if match:
                mat[i, j] = float(match[0].get("deviation_pp") or 0.0)
    fig, ax = plt.subplots(figsize=(9, max(5, 0.45 * len(spots))))
    im = ax.imshow(mat, aspect="auto")
    ax.set_xticks(range(len(pot_types)))
    ax.set_xticklabels(pot_types)
    ax.set_yticks(range(len(spots)))
    ax.set_yticklabels(spots, fontsize=8)
    ax.set_title("Deviation from benchmark by spot and pot type (pp)")
    for i in range(len(spots)):
        for j in range(len(pot_types)):
            if not math.isnan(float(mat[i, j])):
                ax.text(j, i, f"{mat[i, j]:.1f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax, label="percentage-point deviation")
    fig.tight_layout()
    path = outdir / "charts" / "spot_pottype_heatmap.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    paths.append(str(path))
    return paths


def plot_action_mix(summary: List[Dict[str, Any]], outdir: Path, spot_ids: Optional[List[str]] = None) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    wanted = spot_ids or [
        "FOLD_TO_FLOP_CBET",
        "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB",
        "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL",
        "FOLD_TO_DELAYED_CBET_IP",
        "FOLD_TO_DELAYED_CBET_OOP",
        "FOLD_TO_FLOP_CHECK_RAISE",
    ]
    rows = [r for r in summary if r.get("spot_id") in wanted and int(r.get("opportunities") or 0) >= 30]
    rows = sorted(rows, key=lambda r: (str(r.get("spot_id")), str(r.get("pot_type"))))[:18]
    if not rows:
        return paths
    labels = [f"{r.get('spot_id')}\n{r.get('pot_type')} n={r.get('opportunities')}" for r in rows]
    fold = [float(r.get("fold_pct") or 0) for r in rows]
    call = [float(r.get("call_pct") or 0) for r in rows]
    raise_ = [float(r.get("raise_pct") or 0) for r in rows]
    y = list(range(len(rows)))
    fig, ax = plt.subplots(figsize=(12, max(5, 0.65 * len(rows))))
    ax.barh(y, fold, label="fold")
    ax.barh(y, call, left=fold, label="call")
    left2 = [a + b for a, b in zip(fold, call)]
    ax.barh(y, raise_, left=left2, label="raise/all-in")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Response mix (%)")
    ax.set_title("Fold / call / raise mix in key response nodes")
    ax.legend(loc="lower right")
    fig.tight_layout()
    path = outdir / "charts" / "response_mix.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    paths.append(str(path))
    return paths


def plot_board_texture_heatmap(board_summary: List[Dict[str, Any]], benchmarks: Dict[str, Dict[str, Any]], outdir: Path, min_sample: int, spot_id: str = "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB") -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception:
        return paths
    b = benchmarks.get(spot_id, {})
    metric = str(b.get("metric", "fold_pct"))
    rows = [r for r in board_summary if r.get("spot_id") == spot_id and int(r.get("opportunities") or 0) >= min_sample]
    if not rows:
        return paths
    boards = sorted({str(r.get("board_bucket", "")) for r in rows})
    pots = [p for p in ["srp", "3bet", "4bet+"] if any(r.get("pot_type") == p for r in rows)]
    if not boards or not pots:
        return paths
    mat = np.full((len(boards), len(pots)), np.nan)
    for i, bd in enumerate(boards):
        for j, pt in enumerate(pots):
            match = [r for r in rows if r.get("board_bucket") == bd and r.get("pot_type") == pt]
            if match:
                mat[i, j] = pct_float(match[0].get(metric, ""))
    fig, ax = plt.subplots(figsize=(8, max(4, 0.5 * len(boards))))
    im = ax.imshow(mat, aspect="auto")
    ax.set_xticks(range(len(pots)))
    ax.set_xticklabels(pots)
    ax.set_yticks(range(len(boards)))
    ax.set_yticklabels(boards)
    ax.set_title(f"{spot_id}: {metric} by board texture")
    for i in range(len(boards)):
        for j in range(len(pots)):
            if not math.isnan(float(mat[i, j])):
                ax.text(j, i, f"{mat[i, j]:.1f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax, label="observed %")
    fig.tight_layout()
    path = outdir / "charts" / f"board_texture_{spot_id.lower()}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    paths.append(str(path))
    return paths


def generate_pool_charts(outdir: Path, summary: List[Dict[str, Any]], board_summary: List[Dict[str, Any]], benchmarks: Dict[str, Dict[str, Any]], min_sample: int) -> List[str]:
    paths: List[str] = []
    paths += plot_key_leaks(summary, benchmarks, outdir, min_sample)
    paths += plot_spot_pottype_heatmap(summary, outdir, min_sample)
    paths += plot_action_mix(summary, outdir)
    paths += plot_board_texture_heatmap(board_summary, benchmarks, outdir, min_sample)
    return paths


def generate_dashboard_html(outdir: Path, filters: Dict[str, Any], summary: List[Dict[str, Any]], leak_rows: List[Dict[str, Any]], chart_paths: List[str]) -> None:
    top_cols = ["spot_id", "pot_type", "opportunities", "benchmark_metric", "deviation_pp", "exploit_score", "confidence", "benchmark_flag", "leak_note"]
    summary_cols = ["spot_id", "pot_type", "opportunities", "bet_pct", "check_pct", "fold_pct", "call_pct", "raise_pct", "avg_size_pct", "benchmark_flag", "confidence", "exploit_score"]
    rel_charts = []
    for p in chart_paths:
        try:
            rel_charts.append(Path(p).relative_to(outdir).as_posix())
        except Exception:
            rel_charts.append(Path(p).as_posix())
    cards = [
        ("Hands analyzed", filters.get("hands_analyzed_after_filters", "")),
        ("Pool spot records", filters.get("pool_spot_records", "")),
        ("Stake filter", filters.get("stake_filter", "")),
        ("Parse errors", filters.get("parse_errors", "")),
        ("Hero as actor", filters.get("include_hero_as_population_actor", "")),
        ("Game filter", filters.get("game_filter", "")),
    ]
    chart_html = "\n".join(f"<figure><img src='{html_escape(c)}' alt='{html_escape(c)}'><figcaption>{html_escape(Path(c).stem.replace('_',' ').title())}</figcaption></figure>" for c in rel_charts)
    html_text = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>PLO Population Tendencies Dashboard</title>
<style>
:root {{ --bg:#111318; --panel:#1b1f2a; --text:#edf1f7; --muted:#aeb6c2; --border:#303746; }}
body {{ margin:0; font-family:Inter,Segoe UI,Arial,sans-serif; background:var(--bg); color:var(--text); line-height:1.45; }}
main {{ max-width:1280px; margin:0 auto; padding:28px; }}
h1 {{ margin:0 0 4px; font-size:32px; }}
h2 {{ margin-top:34px; border-bottom:1px solid var(--border); padding-bottom:8px; }}
.subtitle {{ color:var(--muted); margin:0 0 22px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:22px 0; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }}
.card .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
.card .value {{ font-size:22px; font-weight:700; margin-top:6px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(380px,1fr)); gap:18px; align-items:start; }}
figure {{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:12px; margin:0; }}
figcaption {{ color:var(--muted); font-size:12px; margin-top:8px; }}
img {{ max-width:100%; height:auto; display:block; border-radius:8px; background:#fff; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; background:var(--panel); border-radius:12px; overflow:hidden; }}
th,td {{ border-bottom:1px solid var(--border); padding:7px 9px; vertical-align:top; }}
th {{ text-align:left; color:#dbe4f0; background:#242a37; position:sticky; top:0; }}
tr.low td, tr.high td {{ font-weight:600; }}
.note {{ color:var(--muted); }}
.code {{ font-family:ui-monospace,Consolas,monospace; }}
a {{ color:#a9c7ff; }}
</style></head><body><main>
<h1>PLO Population Tendencies Dashboard</h1>
<p class='subtitle'>Privacy-preserving aggregate analysis of anonymized PLO hand histories. No per-player profiling.</p>
<section class='cards'>
{''.join(f"<div class='card'><div class='label'>{html_escape(k)}</div><div class='value'>{html_escape(v)}</div></div>" for k,v in cards)}
</section>
<h2>Executive summary: top exploit candidates</h2>
{html_table(leak_rows, top_cols, max_rows=12)}
<h2>Charts</h2>
<div class='grid'>{chart_html}</div>
<h2>Overall spot summary</h2>
{html_table(summary, summary_cols, max_rows=160)}
<h2>Methodology note</h2>
<p class='note'>Benchmark bands are practical review bands, not solver truth. Flags are exploit candidates, not automatic strategy changes. Inspect CSV rows and hand IDs before making large adjustments.</p>
<p class='note'>Generated files: <span class='code'>pool_summary.csv</span>, <span class='code'>pool_summary_by_board.csv</span>, <span class='code'>pool_summary_by_position.csv</span>, <span class='code'>pool_summary_by_ip_oop.csv</span>, <span class='code'>pool_summary_by_facing_size.csv</span>, <span class='code'>pool_spots.csv</span>.</p>
</main></body></html>"""
    (outdir / "pool_dashboard.html").write_text(html_text, encoding="utf-8")


def write_stake_comparison(comparison_rows: List[Dict[str, Any]], outdir: Path, stakes: List[str]) -> None:
    write_csv(comparison_rows, outdir / "stake_comparison.csv")
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return
    # Pick most stable rows with values for at least 2 stakes.
    candidates = []
    for r in comparison_rows:
        present = sum(1 for st in stakes if str(r.get(f"{st}_value", "")).strip())
        if present >= 2:
            total_n = sum(int(r.get(f"{st}_n", 0) or 0) for st in stakes)
            candidates.append((total_n, r))
    candidates = sorted(candidates, reverse=True, key=lambda x: x[0])[:12]
    if not candidates:
        return
    labels = [f"{r.get('spot_id')}\n{r.get('pot_type')}" for _, r in candidates]
    x = list(range(len(labels)))
    fig, ax = plt.subplots(figsize=(13, 6))
    width = 0.8 / max(1, len(stakes))
    for idx, st in enumerate(stakes):
        vals = [pct_float(r.get(f"{st}_value", math.nan)) for _, r in candidates]
        xpos = [v + idx * width for v in x]
        ax.bar(xpos, vals, width=width, label=st)
    ax.set_xticks([v + width * (len(stakes)-1)/2 for v in x])
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Observed metric %")
    ax.set_title("Stake comparison: key population nodes")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    chart_dir = outdir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_dir / "stake_comparison.png", dpi=160)
    plt.close(fig)


def pass_filters(h: Hand, args: argparse.Namespace) -> bool:
    gf = args.game_filter.lower()
    if gf == "plo4":
        if h.meta.get("plo_cards") != 4:
            return False
        if h.meta.get("is_bombpot") and not args.include_bombpot:
            return False
    elif gf == "plo5":
        if h.meta.get("plo_cards") != 5:
            return False
        if h.meta.get("is_bombpot") and not args.include_bombpot:
            return False
    elif gf == "bombpot":
        if not h.meta.get("is_bombpot"):
            return False
    elif gf == "all":
        if h.meta.get("is_bombpot") and not args.include_bombpot:
            return False
    else:
        raise ValueError(f"Unknown game filter: {args.game_filter}")

    if args.stake_filter:
        allowed = {x.strip().upper() for x in args.stake_filter.split(",") if x.strip()}
        if h.stake_label.upper() not in allowed:
            return False

    date_part = str(h.meta.get("dt", "")).split()[0].replace("/", "-")
    if args.date_from and date_part < args.date_from:
        return False
    if args.date_to and date_part > args.date_to:
        return False
    return True


def analyze_pool(input_path: Path, outdir: Path, args: argparse.Namespace) -> Dict[str, Any]:
    text = input_path.read_text(encoding="utf-8", errors="replace")
    raw_blocks = split_hand_history(text)
    hands: List[Hand] = []
    parse_errors = 0
    for block in raw_blocks:
        h = parse_hand(block, hero=args.hero)
        if h is None:
            parse_errors += 1
            continue
        if pass_filters(h, args):
            hands.append(h)

    records: List[Dict[str, Any]] = []
    for h in hands:
        records.extend(extract_required_pool_records(h, args))

    benchmarks = load_benchmarks(args.benchmarks)
    outdir.mkdir(parents=True, exist_ok=True)

    # Overall summary by spot/stake/pot type and then more granular board summary.
    overall_cols = ["spot_id", "stake_label", "pot_type"]
    board_cols = ["spot_id", "stake_label", "pot_type", "board_bucket", "actor_ip_oop"]
    pos_cols = ["spot_id", "stake_label", "pot_type", "actor_position", "board_bucket"]
    ip_oop_cols = ["spot_id", "stake_label", "pot_type", "actor_ip_oop"]

    size_cols = ["spot_id", "stake_label", "pot_type", "facing_size_bucket"]

    summary = summarize_records(records, benchmarks, args.min_sample, overall_cols)
    board_summary = summarize_records(records, benchmarks, args.min_sample, board_cols)
    pos_summary = summarize_records(records, benchmarks, args.min_sample, pos_cols)
    ip_oop_summary = summarize_records(records, benchmarks, args.min_sample, ip_oop_cols)
    facing_size_summary = summarize_records_filtered(records, benchmarks, args.min_sample, size_cols, "facing_size_bucket")

    write_csv(records, outdir / "pool_spots.csv")
    write_csv(summary, outdir / "pool_summary.csv")
    write_csv(board_summary, outdir / "pool_summary_by_board.csv")
    write_csv(pos_summary, outdir / "pool_summary_by_position.csv")
    write_csv(ip_oop_summary, outdir / "pool_summary_by_ip_oop.csv")
    write_csv(facing_size_summary, outdir / "pool_summary_by_facing_size.csv")
    (outdir / "benchmarks_used.json").write_text(json.dumps(benchmarks, indent=2, ensure_ascii=False), encoding="utf-8")
    write_default_benchmarks(outdir / "benchmarks.default.json")

    filters = {
        "input": str(input_path),
        "hero_screen_name": args.hero,
        "include_hero_as_population_actor": args.include_hero,
        "game_filter": args.game_filter,
        "include_bombpot": args.include_bombpot,
        "stake_filter": args.stake_filter or "all",
        "date_from": args.date_from or "none",
        "date_to": args.date_to or "none",
        "raw_hands_before_filters": len(raw_blocks),
        "hands_analyzed_after_filters": len(hands),
        "pool_spot_records": len(records),
        "parse_errors": parse_errors,
        "note": "Population-only aggregation. No per-player tendency file is produced. Hero actions are excluded by default unless --include-hero is used.",
    }
    (outdir / "filters_applied.json").write_text(json.dumps(filters, indent=2, ensure_ascii=False), encoding="utf-8")

    # Leak notes: strongest deviations with enough sample.
    leak_rows = [r for r in summary if int(r.get("opportunities") or 0) >= args.min_sample and r.get("benchmark_flag") in {"LOW", "HIGH"}]
    # Sort by sample first, then flag severity roughly by absolute metric distance.
    def severity(r: Dict[str, Any]) -> Tuple[int, float]:
        b = benchmarks.get(r.get("spot_id", ""), {})
        metric = b.get("metric", "")
        v = float(r.get(metric) or 0.0)
        low = float(b.get("low", 0.0))
        high = float(b.get("high", 100.0))
        dist = low - v if v < low else v - high if v > high else 0.0
        return (int(r.get("opportunities") or 0), dist)
    leak_rows = sorted(leak_rows, key=severity, reverse=True)

    chart_paths: List[str] = []
    if not getattr(args, "no_charts", False):
        chart_paths = generate_pool_charts(outdir, summary, board_summary, benchmarks, args.min_sample)

    md: List[str] = []
    md.append("# CoinPoker PLO Pool Tendency Analyzer")
    md.append("")
    md.append(f"Input file: `{input_path.name}`")
    md.append("")
    md.append("## Active filters")
    md.append(f"- Game filter: **{args.game_filter}**")
    md.append(f"- Include bombpot: **{args.include_bombpot}**")
    md.append(f"- Stake filter: **{args.stake_filter or 'all'}**")
    md.append(f"- Date from: **{args.date_from or 'none'}**")
    md.append(f"- Date to: **{args.date_to or 'none'}**")
    md.append(f"- Raw hands before filters: **{len(raw_blocks):,}**")
    md.append(f"- Hands analyzed after filters: **{len(hands):,}**")
    md.append(f"- Pool spot records: **{len(records):,}**")
    md.append(f"- Parse errors: **{parse_errors:,}**")
    md.append(f"- Hero included as population actor: **{args.include_hero}**")
    md.append("")
    md.append("## Important privacy/modeling note")
    md.append("")
    md.append("CoinPoker export uses anonymized player IDs, so this report intentionally does **population-only aggregation**. It does not create per-player profiles and it does not try to track individual opponents across sessions.")
    md.append("")
    md.append("Benchmark bands are practical review bands, not solver truth. Use the flags as exploit candidates, then inspect `pool_spots.csv` and the hand IDs before changing strategy aggressively.")
    md.append("")
    md.append("## v2 added nodes")
    md.append("")
    md.append("This version adds response stats vs IP flop/turn stabs after missed c-bets, fold vs flop c-bet, turn barrel after flop call, river barrel after flop+turn call, PFA turn give-up response, and delayed c-bet / fold-to-delayed-c-bet split by IP/OOP.")
    md.append("")
    md.append("## Main pool leaks / deviations")
    md.append("")
    if leak_rows:
        for i, r in enumerate(leak_rows[:20], start=1):
            b = benchmarks.get(r.get("spot_id", ""), {})
            metric = b.get("metric", "")
            band = f"{b.get('low')}–{b.get('high')}%"
            md.append(f"{i}. **{r.get('spot_id')}** / {r.get('stake_label')} / {r.get('pot_type')}: {metric} = **{r.get(metric)}%**, benchmark band **{band}**, sample **{r.get('opportunities')}**. {r.get('leak_note')}")
            md.append("")
    else:
        md.append(f"No spot reached min sample {args.min_sample} with a benchmark deviation. Lower `--min-sample`, widen filters, or use a larger HH file.")
        md.append("")

    md.append("## Overall summary")
    md.append("")
    cols = ["spot_id", "stake_label", "pot_type", "opportunities", "bet_pct", "check_pct", "fold_pct", "call_pct", "raise_pct", "avg_size_pct", "benchmark_flag", "leak_note"]
    md.append(markdown_table(summary, cols, max_rows=120))
    md.append("")
    md.append("## Output files")
    md.append("")
    md.append("- `pool_summary.csv` — compact aggregate stats by spot/stake/pot type")
    md.append("- `pool_summary_by_board.csv` — same stats split by board texture")
    md.append("- `pool_summary_by_position.csv` — same stats split by position and board texture")
    md.append("- `pool_summary_by_ip_oop.csv` — compact split by actor IP/OOP, useful for delayed c-bet and c-bet response nodes")
    md.append("- `pool_spots.csv` — individual anonymized spot records, with hand IDs for review")
    md.append("- `benchmarks_used.json` — benchmark bands used for leak flags")
    md.append("- `filters_applied.json` — exact filters and parse counts")

    report_md = "\n".join(md)
    (outdir / "pool_report.md").write_text(report_md, encoding="utf-8")
    legacy_html = "<html><head><meta charset='utf-8'><title>PLO Population Analyzer Markdown Report</title><style>body{font-family:Arial,sans-serif;max-width:1250px;margin:30px auto;line-height:1.45}table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ddd;padding:4px 7px}th{background:#f3f3f3}code{background:#f6f6f6;padding:2px 4px;}</style></head><body><pre>" + html.escape(report_md) + "</pre></body></html>"
    (outdir / "pool_report.html").write_text(legacy_html, encoding="utf-8")
    # pool_report_legacy.html was intentionally removed: it duplicated pool_report.html
    # and made the output folder look noisier than necessary.
    generate_dashboard_html(outdir, filters, summary, leak_rows, chart_paths)

    return {
        **filters,
        "outdir": str(outdir),
        "summary": summary,
        "board_summary": board_summary,
        "facing_size_summary": facing_size_summary,
    }


def build_stake_comparison(results: List[Dict[str, Any]], stakes: List[str], benchmarks: Dict[str, Dict[str, Any]], outdir: Path) -> None:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for stake, result in zip(stakes, results):
        for r in result.get("summary", []):
            spot = str(r.get("spot_id", ""))
            pot = str(r.get("pot_type", ""))
            b = benchmarks.get(spot, {})
            metric = str(b.get("metric", ""))
            key = (spot, pot)
            row = index.setdefault(key, {
                "spot_id": spot,
                "pot_type": pot,
                "metric": metric,
                "benchmark_low": b.get("low", ""),
                "benchmark_high": b.get("high", ""),
            })
            row[f"{stake}_value"] = r.get(metric, "") if metric else ""
            row[f"{stake}_n"] = r.get("opportunities", "")
            row[f"{stake}_flag"] = r.get("benchmark_flag", "")
            row[f"{stake}_confidence"] = r.get("confidence", "")
    comparison_rows = sorted(index.values(), key=lambda r: (str(r.get("spot_id")), str(r.get("pot_type"))))
    write_stake_comparison(comparison_rows, outdir, stakes)

    # Lightweight markdown summary.
    cols = ["spot_id", "pot_type", "metric"]
    for st in stakes:
        cols += [f"{st}_value", f"{st}_n", f"{st}_flag"]
    md = ["# Stake Comparison", "", f"Stakes: **{', '.join(stakes)}**", "", markdown_table(comparison_rows, cols, max_rows=200)]
    (outdir / "stake_comparison.md").write_text("\n".join(md), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description="PLO population tendency analyzer with dashboard/charts")
    p.add_argument("--input", required=True, help="CoinPoker hand history txt file")
    p.add_argument("--hero", default="Hero", help="Hero screen name in HH, usually Hero")
    p.add_argument("--include-hero", action="store_true", help="Include Hero's own decisions as population records. Default excludes Hero as actor.")
    p.add_argument("--outdir", default="cp_pool_report", help="Output directory")
    p.add_argument("--min-sample", type=int, default=30, help="Minimum sample for benchmark leak flags")
    p.add_argument("--game-filter", default="plo4", choices=["plo4", "plo5", "bombpot", "all"], help="Default is plo4 regular only")
    p.add_argument("--include-bombpot", action="store_true", help="Include BombPot hands with plo4/plo5/all filters")
    p.add_argument("--stake-filter", default="", help="Comma-separated stakes, e.g. PL25,PL50")
    p.add_argument("--date-from", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--date-to", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--benchmarks", default="", help="Optional JSON benchmark override file")
    p.add_argument("--debug-players", action="store_true", help="Write anonymized actor/aggressor IDs to pool_spots.csv for debugging. Off by default.")
    p.add_argument("--no-charts", action="store_true", help="Skip PNG chart generation and dashboard images")
    p.add_argument("--compare-stakes", default="", help="Run separate reports and a comparison for comma-separated stakes, e.g. PL25,PL50,PL100")
    args = p.parse_args()

    if args.compare_stakes:
        stakes = [x.strip().upper() for x in args.compare_stakes.split(",") if x.strip()]
        if not stakes:
            raise SystemExit("--compare-stakes was provided but no stakes were parsed")
        root = Path(args.outdir)
        root.mkdir(parents=True, exist_ok=True)
        results: List[Dict[str, Any]] = []
        for st in stakes:
            sub_args = argparse.Namespace(**vars(args))
            sub_args.stake_filter = st
            sub_args.outdir = str(root / st)
            print(f"Running stake {st}...")
            results.append(analyze_pool(Path(args.input), Path(sub_args.outdir), sub_args))
        benchmarks = load_benchmarks(args.benchmarks)
        build_stake_comparison(results, stakes, benchmarks, root)
        print("Done.")
        print(f"Stake comparison output: {root}")
        print("Open stake_comparison.md and charts/stake_comparison.png.")
        return

    result = analyze_pool(Path(args.input), Path(args.outdir), args)
    print("Done.")
    print(f"Raw hands before filters: {result['raw_hands_before_filters']:,}")
    print(f"Hands analyzed after filters: {result['hands_analyzed_after_filters']:,}")
    print(f"Pool spot records: {result['pool_spot_records']:,}")
    print(f"Parse errors: {result['parse_errors']:,}")
    print(f"Output: {result['outdir']}")
    print("Open pool_dashboard.html or pool_report.md.")


if __name__ == "__main__":
    main()
