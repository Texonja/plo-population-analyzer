#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CoinPoker PLO 4-bet Showdown Analyzer

Finds preflop 4-bets made by non-Hero opponents, filters by starting stack in bb,
and checks known/shown 4-bettor hole cards to estimate how often the 4-bet was AAxx.

Default: PLO4 regular hands only, bombpots excluded, Hero excluded.

Usage:
    python coinpoker_plo_4bet_showdown_analyzer.py --input cash.txt --stake-filter PL50 --hero Hero --outdir fourbet_report

Notes:
- A 4-bet is defined as the third preflop raise in a hand:
    open raise = raise #1
    3-bet      = raise #2
    4-bet      = raise #3
- ALLIN actions are treated as raises only if they increase the current amount to call.
- "Known" means the 4-bettor's 4-card hand appears in the HH via shows/showed/mucks/mucked.
- This is population-only; anonymized IDs are not profiled individually.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import html
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RANK_ORDER = "23456789TJQKA"
RANK_VALUE = {r: i for i, r in enumerate(RANK_ORDER, start=2)}

HEADER_RE = re.compile(r"CoinPoker Hand #(\d+): (.*?) \(([^)]+)\) ([0-9/]+ [0-9:]+) (\w+)")
SEAT_RE = re.compile(r"Seat (\d+): (.+?) \(₮([0-9.]+) in chips\)")
BUTTON_RE = re.compile(r"Seat #(\d+) is the button")
RAISE_RE = re.compile(r"^(.+?): raises ₮([0-9.]+) to ₮([0-9.]+)")
CALL_RE = re.compile(r"^(.+?): calls ₮([0-9.]+)")
ALLIN_RE = re.compile(r"^(.+?): ALLIN ₮([0-9.]+)")
POST_SB_RE = re.compile(r"^(.+?): posts small blind ₮([0-9.]+)")
POST_BB_RE = re.compile(r"^(.+?): posts big blind ₮([0-9.]+)")
POST_ANTE_RE = re.compile(r"^(.+?): posts ante ₮([0-9.]+)")
FOLD_RE = re.compile(r"^(.+?): folds")
STREET_RE = re.compile(r"^\*\*\* (?:FIRST |SECOND |THIRD )?(FLOP|TURN|RIVER) \*\*")

# Robust known-card patterns. CoinPoker exports can vary slightly between full action and summary lines.
KNOWN_CARD_PATTERNS = [
    re.compile(r"^(.+?):\s+shows\s+\[([^\]]+)\]", re.I),
    re.compile(r"^(.+?):\s+showed\s+\[([^\]]+)\]", re.I),
    re.compile(r"^(.+?):\s+mucks\s+\[([^\]]+)\]", re.I),
    re.compile(r"^(.+?):\s+mucked\s+\[([^\]]+)\]", re.I),
    re.compile(r"^Seat \d+:\s+(.+?)\s+showed\s+\[([^\]]+)\]", re.I),
    re.compile(r"^Seat \d+:\s+(.+?)\s+mucked\s+\[([^\]]+)\]", re.I),
]


def split_hand_history(text: str) -> List[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    return re.split(r"\n(?=CoinPoker Hand #)", text)


def parse_header(line: str) -> Optional[Dict[str, Any]]:
    m = HEADER_RE.match(line)
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
    labels = labels_by_n.get(len(order), ["BTN"] + [f"P{i}" for i in range(1, len(order))])
    return {seats[seat]: labels[i] for i, seat in enumerate(order)}


def parse_cards(card_text: str) -> List[str]:
    return [c.strip() for c in card_text.split() if re.match(r"^[2-9TJQKA][cdhsCDHS]$", c.strip())]


def classify_known_hand(cards: List[str]) -> Dict[str, Any]:
    ranks = [c[0].upper() for c in cards]
    suits = [c[1].lower() for c in cards]
    rank_counts = collections.Counter(ranks)
    suit_counts = collections.Counter(suits)
    vals = sorted([RANK_VALUE.get(r, 0) for r in ranks], reverse=True)
    uniq = sorted(set(vals))
    pairs = sum(1 for v in rank_counts.values() if v == 2)
    high = sum(1 for v in vals if v >= 11)
    broadway = sum(1 for v in vals if v >= 10)
    ace = "A" in ranks
    best_window = 0
    for low in range(2, 11):
        best_window = max(best_window, sum(1 for v in uniq if low <= v <= low + 4))
    double_suited = sum(1 for v in suit_counts.values() if v >= 2) >= 2
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
    elif ace and max(suit_counts.values() or [0]) >= 2:
        hand_class = "suited_ace"
    else:
        hand_class = "other"
    return {
        "cards": " ".join(cards),
        "card_count": len(cards),
        "is_aaxx": rank_counts.get("A", 0) >= 2 and len(cards) == 4,
        "hand_class": hand_class,
        "double_suited": double_suited,
        "pairs": pairs,
        "broadways": broadway,
        "high_cards": high,
        "connected_window": best_window,
    }


def extract_known_cards(lines: List[str], seat_names: set[str]) -> Dict[str, List[str]]:
    known: Dict[str, List[str]] = {}
    for line in lines:
        for pat in KNOWN_CARD_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            player = m.group(1).strip()
            cards = parse_cards(m.group(2))
            if player in seat_names and len(cards) >= 4:
                known[player] = cards[:4]
            break
    return known


def pass_filters(meta: Dict[str, Any], args: argparse.Namespace) -> bool:
    gf = args.game_filter.lower()
    if gf == "plo4":
        if meta.get("plo_cards") != 4:
            return False
        if meta.get("is_bombpot") and not args.include_bombpot:
            return False
    elif gf == "plo5":
        if meta.get("plo_cards") != 5:
            return False
        if meta.get("is_bombpot") and not args.include_bombpot:
            return False
    elif gf == "bombpot":
        if not meta.get("is_bombpot"):
            return False
    elif gf == "all":
        if meta.get("is_bombpot") and not args.include_bombpot:
            return False
    else:
        raise ValueError(f"Unknown game filter: {args.game_filter}")

    if args.stake_filter:
        allowed = {x.strip().upper() for x in args.stake_filter.split(",") if x.strip()}
        if meta.get("stake_label", "").upper() not in allowed:
            return False

    date_part = str(meta.get("dt", "")).split()[0].replace("/", "-")
    if args.date_from and date_part < args.date_from:
        return False
    if args.date_to and date_part > args.date_to:
        return False
    return True


def analyze_hand(block: str, args: argparse.Namespace) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    lines = block.splitlines()
    if not lines:
        return "empty", []
    meta = parse_header(lines[0])
    if not meta:
        return "header", []
    if not pass_filters(meta, args):
        return None, []

    seats: Dict[int, str] = {}
    stacks: Dict[str, float] = {}
    for line in lines:
        m = SEAT_RE.match(line)
        if m:
            seat = int(m.group(1))
            player = m.group(2)
            seats[seat] = player
            stacks[player] = float(m.group(3))
    if not seats:
        return "seats", []
    if args.hero and args.hero not in seats.values() and not args.allow_hands_without_hero:
        return None, []

    bb = float(meta.get("bb") or 0.0)
    if bb <= 0:
        return "bb", []
    button_match = BUTTON_RE.search(block)
    button_seat = int(button_match.group(1)) if button_match else None
    positions = assign_positions(seats, button_seat)
    known_cards = extract_known_cards(lines, set(seats.values()))

    street = "PREFLOP"
    street_contrib: Dict[str, float] = collections.defaultdict(float)
    current_to_call = 0.0
    raise_count = 0
    preflop_sequence: List[str] = []
    fourbet_rows: List[Dict[str, Any]] = []

    for line in lines[1:]:
        if STREET_RE.match(line):
            street = "POSTFLOP"
            break
        if street != "PREFLOP":
            continue

        m = POST_SB_RE.match(line) or POST_BB_RE.match(line) or POST_ANTE_RE.match(line)
        if m:
            player = m.group(1)
            amount = float(m.group(2))
            if POST_ANTE_RE.match(line):
                pass
            else:
                street_contrib[player] += amount
                current_to_call = max(current_to_call, street_contrib[player])
            continue

        m = CALL_RE.match(line)
        if m:
            player = m.group(1)
            amount = float(m.group(2))
            street_contrib[player] += amount
            preflop_sequence.append(f"{player}:call_to_{current_to_call:.4g}")
            continue

        m = FOLD_RE.match(line)
        if m:
            player = m.group(1)
            preflop_sequence.append(f"{player}:fold")
            continue

        m = RAISE_RE.match(line)
        if m:
            player = m.group(1)
            total_to = float(m.group(3))
            # Raise only if it increases current bet.
            if total_to > current_to_call + 1e-9:
                raise_count += 1
                action_name = {1: "open", 2: "3bet", 3: "4bet"}.get(raise_count, f"{raise_count+1}bet")
                preflop_sequence.append(f"{player}:{action_name}_to_{total_to:.4g}")
                if raise_count == 3:
                    row = build_fourbet_row(meta, player, stacks, positions, bb, known_cards, action_name, total_to, args, "raise")
                    if row:
                        row["preflop_sequence"] = " | ".join(preflop_sequence)
                        fourbet_rows.append(row)
                street_contrib[player] = total_to
                current_to_call = total_to
            continue

        m = ALLIN_RE.match(line)
        if m:
            player = m.group(1)
            amount = float(m.group(2))
            new_total = street_contrib[player] + amount
            if new_total > current_to_call + 1e-9:
                raise_count += 1
                action_name = {1: "open_allin", 2: "3bet_allin", 3: "4bet_allin"}.get(raise_count, f"{raise_count+1}bet_allin")
                preflop_sequence.append(f"{player}:{action_name}_to_{new_total:.4g}")
                if raise_count == 3:
                    row = build_fourbet_row(meta, player, stacks, positions, bb, known_cards, action_name, new_total, args, "allin_raise")
                    if row:
                        row["preflop_sequence"] = " | ".join(preflop_sequence)
                        fourbet_rows.append(row)
                street_contrib[player] = new_total
                current_to_call = max(current_to_call, new_total)
            else:
                street_contrib[player] = new_total
                preflop_sequence.append(f"{player}:allin_call_to_{new_total:.4g}")
            continue

    return None, fourbet_rows


def build_fourbet_row(
    meta: Dict[str, Any],
    player: str,
    stacks: Dict[str, float],
    positions: Dict[str, str],
    bb: float,
    known_cards: Dict[str, List[str]],
    action_name: str,
    total_to: float,
    args: argparse.Namespace,
    action_type: str,
) -> Optional[Dict[str, Any]]:
    if args.hero and not args.include_hero and player == args.hero:
        return None
    stack = float(stacks.get(player, 0.0))
    stack_bb = stack / bb if bb else 0.0
    if stack_bb < args.min_stack_bb:
        return None
    cards = known_cards.get(player, [])
    hand_info = classify_known_hand(cards) if cards else {
        "cards": "",
        "card_count": 0,
        "is_aaxx": "",
        "hand_class": "unknown",
        "double_suited": "",
        "pairs": "",
        "broadways": "",
        "high_cards": "",
        "connected_window": "",
    }
    return {
        "hand_id": meta.get("hand_id", ""),
        "dt": meta.get("dt", ""),
        "stake_label": meta.get("stake_label", ""),
        "desc": meta.get("desc", ""),
        "bb": bb,
        "fourbettor": player,
        "fourbettor_position": positions.get(player, ""),
        "fourbettor_start_stack": round(stack, 4),
        "fourbettor_start_stack_bb": round(stack_bb, 1),
        "fourbet_action": action_name,
        "fourbet_action_type": action_type,
        "fourbet_total_to": round(total_to, 4),
        "fourbet_total_to_bb": round(total_to / bb, 2) if bb else "",
        "known_cards": bool(cards),
        **hand_info,
    }


def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: List[str] = []
    for row in rows:
        for k in row.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def pct_float_text(value: Any) -> float:
    try:
        if isinstance(value, str) and value.endswith("%"):
            value = value[:-1]
        return float(value)
    except Exception:
        return math.nan


def html_escape(x: Any) -> str:
    return html.escape(str(x))


def html_table(rows: List[Dict[str, Any]], cols: List[str], max_rows: int = 80) -> str:
    if not rows:
        return "<p><em>No rows.</em></p>"
    out = ["<table>", "<thead><tr>" + "".join(f"<th>{html_escape(c)}</th>" for c in cols) + "</tr></thead>", "<tbody>"]
    for r in rows[:max_rows]:
        out.append("<tr>" + "".join(f"<td>{html_escape(r.get(c, ''))}</td>" for c in cols) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def make_hand_class_summary(known_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    c = collections.Counter(str(r.get("hand_class", "unknown")) for r in known_rows)
    total = sum(c.values())
    return [
        {"hand_class": cls, "count": n, "pct": pct(n, total)}
        for cls, n in c.most_common()
    ]


def generate_fourbet_charts(outdir: Path, summary_rows: List[Dict[str, Any]], known_rows: List[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    chart_dir = outdir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    pos_rows = [r for r in summary_rows if r.get("fourbettor_position") != "ALL" and int(r.get("known_4bettor_hands") or 0) > 0]
    if pos_rows:
        pos_rows = sorted(pos_rows, key=lambda r: pct_float_text(r.get("AAxx_pct_of_known")), reverse=True)
        labels = [str(r.get("fourbettor_position")) + f"\nn={r.get('known_4bettor_hands')}" for r in pos_rows]
        vals = [pct_float_text(r.get("AAxx_pct_of_known")) for r in pos_rows]
        fig, ax = plt.subplots(figsize=(9, 5))
        ax.bar(labels, vals)
        ax.set_ylim(0, max(100, max(vals) + 10 if vals else 100))
        ax.set_ylabel("AAxx among known 4-bets (%)")
        ax.set_title("AAxx share by 4-bettor position")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        path = chart_dir / "aaxx_by_position.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        paths.append(str(path))

    classes = make_hand_class_summary(known_rows)
    if classes:
        labels = [str(r.get("hand_class")) for r in classes]
        vals = [int(r.get("count") or 0) for r in classes]
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.bar(labels, vals)
        ax.set_ylabel("Known 4-bettor hands")
        ax.set_title("Known 4-bet hand-class composition")
        ax.tick_params(axis='x', rotation=35)
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        path = chart_dir / "hand_class_composition.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        paths.append(str(path))

    return paths


def generate_fourbet_dashboard(outdir: Path, summary_rows: List[Dict[str, Any]], rows: List[Dict[str, Any]], known_rows: List[Dict[str, Any]], chart_paths: List[str], args: argparse.Namespace) -> None:
    known_aaxx = sum(1 for r in known_rows if r.get("is_aaxx") is True)
    overall_pct = pct(known_aaxx, len(known_rows)) or "n/a"
    cards = [
        ("Stack-filtered 4-bets", f"{len(rows):,}"),
        ("Known hands", f"{len(known_rows):,}"),
        ("AAxx known", f"{known_aaxx:,}"),
        ("AAxx % known", overall_pct),
        ("Min stack", f"{args.min_stack_bb:g}bb"),
        ("Stake filter", args.stake_filter or "all"),
    ]
    rel_charts = []
    for p in chart_paths:
        try:
            rel_charts.append(Path(p).relative_to(outdir).as_posix())
        except Exception:
            rel_charts.append(Path(p).as_posix())
    chart_html = "\n".join(f"<figure><img src='{html_escape(c)}' alt='{html_escape(c)}'><figcaption>{html_escape(Path(c).stem.replace('_',' ').title())}</figcaption></figure>" for c in rel_charts)
    cols = ["stake_label", "fourbettor_position", "total_4bets_stack_filtered", "known_4bettor_hands", "known_rate_pct", "known_AAxx", "AAxx_pct_of_known", "unknown_4bettor_hands"]
    class_rows = make_hand_class_summary(known_rows)
    html_text = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>PLO 4-bet Showdown Dashboard</title>
<style>
:root {{ --bg:#111318; --panel:#1b1f2a; --text:#edf1f7; --muted:#aeb6c2; --border:#303746; }}
body {{ margin:0; font-family:Inter,Segoe UI,Arial,sans-serif; background:var(--bg); color:var(--text); line-height:1.45; }}
main {{ max-width:1180px; margin:0 auto; padding:28px; }}
h1 {{ margin:0 0 4px; font-size:32px; }} h2 {{ margin-top:34px; border-bottom:1px solid var(--border); padding-bottom:8px; }}
.subtitle,.note {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:22px 0; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }}
.card .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }} .card .value {{ font-size:22px; font-weight:700; margin-top:6px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:18px; align-items:start; }}
figure {{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:12px; margin:0; }} img {{ max-width:100%; display:block; border-radius:8px; background:#fff; }} figcaption {{ color:var(--muted); font-size:12px; margin-top:8px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; background:var(--panel); border-radius:12px; overflow:hidden; }} th,td {{ border-bottom:1px solid var(--border); padding:7px 9px; }} th {{ text-align:left; background:#242a37; }}
</style></head><body><main>
<h1>PLO 4-bet Showdown Dashboard</h1>
<p class='subtitle'>Population-only inference of known/shown 4-bettor hand composition. Unknown hands are excluded from AAxx denominator.</p>
<section class='cards'>{''.join(f"<div class='card'><div class='label'>{html_escape(k)}</div><div class='value'>{html_escape(v)}</div></div>" for k,v in cards)}</section>
<h2>Charts</h2><div class='grid'>{chart_html}</div>
<h2>Position summary</h2>{html_table(summary_rows, cols, max_rows=80)}
<h2>Known hand classes</h2>{html_table(class_rows, ['hand_class','count','pct'], max_rows=20)}
<p class='note'>ALLIN is counted as a 4-bet only when it increases the current preflop bet. Hero is excluded by default.</p>
</main></body></html>"""
    (outdir / "fourbet_dashboard.html").write_text(html_text, encoding="utf-8")
    (outdir / "fourbet_report.html").write_text(html_text, encoding="utf-8")


def pct(num: int, den: int) -> str:
    return "" if den == 0 else f"{100.0 * num / den:.1f}%"


def summarize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        groups[(str(r.get("stake_label", "")), str(r.get("fourbettor_position", "")))].append(r)
    out: List[Dict[str, Any]] = []
    for (stake, pos), gr in sorted(groups.items()):
        known = [r for r in gr if r.get("known_cards")]
        aaxx = [r for r in known if r.get("is_aaxx") is True]
        out.append({
            "stake_label": stake,
            "fourbettor_position": pos,
            "total_4bets_stack_filtered": len(gr),
            "known_4bettor_hands": len(known),
            "known_rate_pct": pct(len(known), len(gr)),
            "known_AAxx": len(aaxx),
            "AAxx_pct_of_known": pct(len(aaxx), len(known)),
            "unknown_4bettor_hands": len(gr) - len(known),
        })
    # Add all-positions aggregate by stake.
    by_stake: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        by_stake[str(r.get("stake_label", ""))].append(r)
    for stake, gr in sorted(by_stake.items()):
        known = [r for r in gr if r.get("known_cards")]
        aaxx = [r for r in known if r.get("is_aaxx") is True]
        out.insert(0, {
            "stake_label": stake,
            "fourbettor_position": "ALL",
            "total_4bets_stack_filtered": len(gr),
            "known_4bettor_hands": len(known),
            "known_rate_pct": pct(len(known), len(gr)),
            "known_AAxx": len(aaxx),
            "AAxx_pct_of_known": pct(len(aaxx), len(known)),
            "unknown_4bettor_hands": len(gr) - len(known),
        })
    return out


def make_md(summary_rows: List[Dict[str, Any]], rows: List[Dict[str, Any]], args: argparse.Namespace) -> str:
    md: List[str] = []
    known = [r for r in rows if r.get("known_cards")]
    aaxx = [r for r in known if r.get("is_aaxx") is True]
    md.append("# CoinPoker PLO 4-bet Showdown Analyzer")
    md.append("")
    md.append(f"Input file: `{Path(args.input).name}`")
    md.append(f"Game filter: **{args.game_filter}**, stake filter: **{args.stake_filter or 'all'}**, min stack: **{args.min_stack_bb:g}bb**, Hero included: **{args.include_hero}**")
    md.append("")
    md.append("## Headline")
    md.append("")
    md.append(f"- Stack-filtered non-Hero 4-bets found: **{len(rows):,}**")
    md.append(f"- Known/shown 4-bettor hands: **{len(known):,}**")
    md.append(f"- AAxx among known 4-bettor hands: **{len(aaxx):,} / {len(known):,} ({pct(len(aaxx), len(known)) or 'n/a'})**")
    md.append("")
    md.append("## Summary")
    md.append("")
    cols = ["stake_label", "fourbettor_position", "total_4bets_stack_filtered", "known_4bettor_hands", "known_rate_pct", "known_AAxx", "AAxx_pct_of_known", "unknown_4bettor_hands"]
    md.append("| " + " | ".join(cols) + " |")
    md.append("| " + " | ".join(["---"] * len(cols)) + " |")
    for r in summary_rows:
        md.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    md.append("")
    md.append("## Important notes")
    md.append("")
    md.append("- This counts only known/shown 4-bettor cards for the AAxx percentage. Unknown 4-bettor hands are excluded from the AAxx denominator.")
    md.append("- ALLIN is counted as a 4-bet only when it increases the current preflop bet.")
    md.append("- Because CoinPoker uses anonymized IDs, this is population-only and not a player profile.")
    return "\n".join(md)


def main() -> None:
    p = argparse.ArgumentParser(description="Analyze known/shown villain 4-bets for AAxx frequency")
    p.add_argument("--input", required=True, help="CoinPoker hand history txt file")
    p.add_argument("--hero", default="Hero", help="Hero screen name, default Hero")
    p.add_argument("--include-hero", action="store_true", help="Include Hero 4-bets too. Default excludes Hero.")
    p.add_argument("--allow-hands-without-hero", action="store_true", help="Analyze hands even if hero name is not present")
    p.add_argument("--outdir", default="fourbet_showdown_report", help="Output directory")
    p.add_argument("--min-stack-bb", type=float, default=60.0, help="Minimum starting stack in bb for the 4-bettor")
    p.add_argument("--game-filter", default="plo4", choices=["plo4", "plo5", "bombpot", "all"], help="Default PLO4 regular only")
    p.add_argument("--include-bombpot", action="store_true")
    p.add_argument("--stake-filter", default="", help="Comma-separated stakes, e.g. PL25,PL50")
    p.add_argument("--date-from", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--date-to", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--no-charts", action="store_true", help="Skip PNG charts and HTML dashboard images")
    args = p.parse_args()

    text = Path(args.input).read_text(encoding="utf-8", errors="replace")
    blocks = split_hand_history(text)
    rows: List[Dict[str, Any]] = []
    parse_errors = collections.Counter()
    for block in blocks:
        err, hand_rows = analyze_hand(block, args)
        if err:
            parse_errors[err] += 1
        rows.extend(hand_rows)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    known_rows = [r for r in rows if r.get("known_cards")]
    summary_rows = summarize(rows)
    write_csv(rows, outdir / "fourbet_all.csv")
    write_csv(known_rows, outdir / "fourbet_known_cards.csv")
    write_csv(summary_rows, outdir / "fourbet_summary.csv")
    (outdir / "filters_applied.json").write_text(json.dumps({
        "input": args.input,
        "hero": args.hero,
        "include_hero": args.include_hero,
        "min_stack_bb": args.min_stack_bb,
        "game_filter": args.game_filter,
        "include_bombpot": args.include_bombpot,
        "stake_filter": args.stake_filter or "all",
        "date_from": args.date_from or "none",
        "date_to": args.date_to or "none",
        "raw_hands": len(blocks),
        "stack_filtered_4bets": len(rows),
        "known_4bettor_hands": len(known_rows),
        "parse_errors": dict(parse_errors),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    md = make_md(summary_rows, rows, args)
    (outdir / "fourbet_report.md").write_text(md, encoding="utf-8")
    chart_paths: List[str] = []
    if not args.no_charts:
        chart_paths = generate_fourbet_charts(outdir, summary_rows, known_rows)
        generate_fourbet_dashboard(outdir, summary_rows, rows, known_rows, chart_paths, args)

    known_aaxx = sum(1 for r in known_rows if r.get("is_aaxx") is True)
    print("Done.")
    print(f"Raw hands: {len(blocks):,}")
    print(f"Stack-filtered 4-bets: {len(rows):,}")
    print(f"Known/shown 4-bettor hands: {len(known_rows):,}")
    print(f"AAxx among known: {known_aaxx:,}/{len(known_rows):,} ({pct(known_aaxx, len(known_rows)) or 'n/a'})")
    print(f"Output: {outdir}")
    print("Open fourbet_dashboard.html, fourbet_report.md and fourbet_known_cards.csv.")


if __name__ == "__main__":
    main()
