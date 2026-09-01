#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CoinPoker PLO Four-bet Showdown Analyzer

Finds preflop 4-bets made by non-Hero opponents, filters by starting stack in bb,
and checks known/shown 4-bettor hole cards to estimate how often the 4-bet was AAxx.

Default: PLO4 regular hands only, bombpots excluded, Hero excluded.

Usage after installation:
    plo-fourbet --input cash.txt --stake-filter PL50 --hero Hero --outdir fourbet_report

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

try:
    from . import __version__
    from .common import RANK_VALUE, assign_positions, parse_header, split_hand_history
except ImportError:  # Supports direct execution during local debugging.
    __version__ = "1.1.0"
    from common import RANK_VALUE, assign_positions, parse_header, split_hand_history

SEAT_RE = re.compile(r"Seat (\d+): (.+?) \(₮([0-9.]+) in chips\)")
BUTTON_RE = re.compile(r"Seat #(\d+) is the button")
RAISE_RE = re.compile(r"^(.+?): raises ₮([0-9.]+) to ₮([0-9.]+)")
CALL_RE = re.compile(r"^(.+?): calls ₮([0-9.]+)")
ALLIN_RE = re.compile(r"^(.+?): ALLIN ₮([0-9.]+)")
POST_SB_RE = re.compile(r"^(.+?): posts small blind ₮([0-9.]+)")
POST_BB_RE = re.compile(r"^(.+?): posts big blind ₮([0-9.]+)")
POST_ANTE_RE = re.compile(r"^(.+?): posts ante ₮([0-9.]+)")
STRADDLE_RE = re.compile(r"^(.+?): STRADDLE ₮([0-9.]+)", re.I)
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

    if meta.get("is_straddled") and not getattr(args, "include_straddles", False):
        return False

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
    meta["is_straddled"] = any(STRADDLE_RE.match(line) for line in lines)
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

        m = POST_SB_RE.match(line) or POST_BB_RE.match(line) or POST_ANTE_RE.match(line) or STRADDLE_RE.match(line)
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


def export_detail_row(row: Dict[str, Any], include_identifiers: bool = False) -> Dict[str, Any]:
    """Return a CSV-safe detail row with player identifiers removed by default."""
    exported = dict(row)
    if not include_identifiers:
        exported.pop("fourbettor", None)
        exported.pop("preflop_sequence", None)
    return exported


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
    out = ["<div class='table-wrap'><table>", "<thead><tr>" + "".join(f"<th>{html_escape(c)}</th>" for c in cols) + "</tr></thead>", "<tbody>"]
    for r in rows[:max_rows]:
        out.append("<tr>" + "".join(f"<td>{html_escape(r.get(c, ''))}</td>" for c in cols) + "</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


def make_hand_class_summary(known_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    c = collections.Counter(str(r.get("hand_class", "unknown")) for r in known_rows)
    total = sum(c.values())
    return [
        {"hand_class": cls, "count": n, "pct": pct(n, total)}
        for cls, n in c.most_common()
    ]


def import_headless_pyplot():
    """Load pyplot with a non-interactive backend suitable for CI and servers."""
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    return plt


def generate_fourbet_charts(outdir: Path, summary_rows: List[Dict[str, Any]], known_rows: List[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    try:
        plt = import_headless_pyplot()
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
    chart_html = "\n".join(f"<figure><img src='{html_escape(c)}' alt='{html_escape(Path(c).stem.replace('_',' '))}'><figcaption>{html_escape(Path(c).stem.replace('_',' ').title())}</figcaption></figure>" for c in rel_charts)
    if not chart_html:
        chart_html = "<p class='empty'>Charts were disabled for this run.</p>"
    cols = ["stake_label", "fourbettor_position", "total_4bets_stack_filtered", "known_4bettor_hands", "known_rate_pct", "known_AAxx", "AAxx_pct_of_known", "unknown_4bettor_hands"]
    class_rows = make_hand_class_summary(known_rows)
    html_text = f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>CoinPoker PLO Four-bet Showdown Analyzer</title>
<style>
:root {{ color-scheme:dark; --bg:#07100d; --panel:#0d1915; --panel2:#12221c; --text:#eef5ef; --muted:#9facaa; --border:#22352e; --green:#55d698; }}
* {{ box-sizing:border-box; }} body {{ margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; background:radial-gradient(circle at 85% 0%,rgba(52,145,98,.13),transparent 30rem),var(--bg); color:var(--text); line-height:1.5; }}
main {{ max-width:1220px; margin:0 auto; padding:clamp(20px,4vw,52px); }} .eyebrow {{ color:var(--green); font-size:12px; font-weight:800; letter-spacing:.13em; text-transform:uppercase; }}
h1 {{ margin:12px 0 8px; font-size:clamp(38px,6vw,64px); line-height:1; letter-spacing:-.055em; }} h2 {{ margin:48px 0 18px; font-size:clamp(24px,3vw,32px); letter-spacing:-.035em; }}
.subtitle,.note,.empty {{ color:var(--muted); }} .subtitle {{ max-width:780px; font-size:17px; }}
.cards {{ display:grid; grid-template-columns:repeat(6,1fr); gap:1px; margin:34px 0 12px; overflow:hidden; border:1px solid var(--border); border-radius:16px; background:var(--border); }}
.card {{ background:rgba(13,25,21,.97); padding:20px; }} .card .label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.09em; }} .card .value {{ font-size:24px; font-weight:760; letter-spacing:-.035em; margin-top:7px; }}
.grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; align-items:start; }} figure {{ background:linear-gradient(145deg,var(--panel2),var(--panel)); border:1px solid var(--border); border-radius:16px; padding:12px; margin:0; }} img {{ max-width:100%; display:block; border-radius:10px; background:#fff; }} figcaption {{ color:var(--muted); font-size:12px; margin:9px 4px 3px; }}
.table-wrap {{ width:100%; overflow:auto; border:1px solid var(--border); border-radius:14px; }} table {{ border-collapse:collapse; width:100%; min-width:780px; font-size:12px; background:var(--panel); }} th,td {{ border-bottom:1px solid var(--border); padding:9px 11px; }} th {{ text-align:left; background:#162820; white-space:nowrap; }} tr:last-child td {{ border-bottom:0; }} tr:hover td {{ background:#10221b; }}
.note {{ margin-top:22px; padding:20px; border:1px solid var(--border); border-radius:14px; background:var(--panel); }}
@media(max-width:960px) {{ .cards {{ grid-template-columns:repeat(3,1fr); }} }} @media(max-width:720px) {{ .grid {{ grid-template-columns:1fr; }} .cards {{ grid-template-columns:repeat(2,1fr); }} }} @media(max-width:440px) {{ .cards {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<div class='eyebrow'>Conditional showdown sample</div>
<h1>CoinPoker PLO Four-bet Analyzer</h1>
<p class='subtitle'>Population-level composition of known/shown four-bet hands. Unknown cards stay visible in coverage counts and are excluded from hand-class denominators.</p>
<section class='cards'>{''.join(f"<div class='card'><div class='label'>{html_escape(k)}</div><div class='value'>{html_escape(v)}</div></div>" for k,v in cards)}</section>
<h2>Composition overview</h2><div class='grid'>{chart_html}</div>
<h2>Position summary</h2>{html_table(summary_rows, cols, max_rows=80)}
<h2>Known hand classes</h2>{html_table(class_rows, ['hand_class','count','pct'], max_rows=20)}
<p class='note'>A four-bet is the third preflop raise. ALLIN counts only when it increases the amount to call. Hero is excluded by default. Showdown composition is conditional on cards being revealed and can be affected by selection bias.</p>
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Analyze known/shown villain 4-bets for AAxx frequency")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--input", required=True, help="CoinPoker hand history txt file")
    p.add_argument("--hero", default="Hero", help="Hero screen name, default Hero")
    p.add_argument("--include-hero", action="store_true", help="Include Hero 4-bets too. Default excludes Hero.")
    p.add_argument("--allow-hands-without-hero", action="store_true", help="Analyze hands even if hero name is not present")
    p.add_argument("--outdir", default="fourbet_showdown_report", help="Output directory")
    p.add_argument("--min-stack-bb", type=float, default=60.0, help="Minimum starting stack in bb for the 4-bettor")
    p.add_argument("--game-filter", default="plo4", choices=["plo4", "plo5", "bombpot", "all"], help="Default PLO4 regular only")
    p.add_argument("--include-bombpot", action="store_true")
    p.add_argument("--include-straddles", action="store_true", help="Include straddled hands. Default excludes them")
    p.add_argument("--stake-filter", default="", help="Comma-separated stakes, e.g. PL25,PL50")
    p.add_argument("--date-from", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--date-to", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--no-charts", action="store_true", help="Skip PNG chart generation; the HTML dashboard is still written")
    p.add_argument("--include-identifiers", action="store_true", help="Include player IDs and named preflop sequences in granular CSV files. Off by default.")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    p = build_parser()
    args = p.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        p.error(f"input file not found: {input_path}")
    text = input_path.read_text(encoding="utf-8", errors="replace")
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
    exported_rows = [export_detail_row(row, args.include_identifiers) for row in rows]
    exported_known_rows = [export_detail_row(row, args.include_identifiers) for row in known_rows]
    write_csv(exported_rows, outdir / "fourbet_all.csv")
    write_csv(exported_known_rows, outdir / "fourbet_known_cards.csv")
    write_csv(summary_rows, outdir / "fourbet_summary.csv")
    (outdir / "filters_applied.json").write_text(json.dumps({
        "input": input_path.name,
        "hero": args.hero,
        "include_hero": args.include_hero,
        "include_identifiers": args.include_identifiers,
        "min_stack_bb": args.min_stack_bb,
        "game_filter": args.game_filter,
        "include_bombpot": args.include_bombpot,
        "include_straddles": args.include_straddles,
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
