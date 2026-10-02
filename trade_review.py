#!/usr/bin/env python3
"""Trading-side review of the paper bot: where does P&L come from, and is capital
stuck? Read-only - it reads paperbot_state.json and changes nothing.

The risk score predicts survival, not drawdown (see HANDOFF section 6), so the
levers for the bot's results are entries, exits and sizing. This prints what is
needed to pick one:

  1. The open book by age - how much capital sits in positions that have hit
     neither stop-loss nor take-profit, and for how long.
  2. Buys per day - whether the bot is still testing its entry rule at all or
     has stopped buying because its cash is tied up.
  3. Closed trades by exit reason, with holding times.
  4. What the extremes say about the exit levels. A position's watermarks stop
     updating when it closes, so a stopped-out trade's best price came BEFORE
     the stop, and a take-profit trade's worst price came before the target.
     That supports one-sided "would a different level have triggered first"
     counts. It cannot say what would have happened next.

  5. (--exits) Which positions got exit levels at the floor. Stop and target are
     1.5x and 2.5x the coin's largest recent move, but never under 20% / 30%.
     A calm coin therefore gets levels far outside its normal movement and can
     sit for weeks. This compares at-floor trades with the rest, shows how long
     winners take (to size any time limit), and lists open positions that have
     no valid quote.

  python trade_review.py
  python trade_review.py --exits
  python trade_review.py --since 2026-09-07T20:47 --state paperbot_state.json
"""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEFAULT_SINCE = "2026-09-07T20:47"
TP_FLOOR_PCT = 30  # analyzer.compute_trade_levels: tp = clamp(volatility * 2.5, 30, 150)
MATCH_WINDOW_S = 600  # a bought candidate row is logged within seconds of its position opening
AGE_BANDS = ((0, 1, "<1 day"), (1, 3, "1-3 days"), (3, 7, "3-7 days"), (7, 10**9, "7+ days"))


def utc(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def days_between(start, end):
    return (end - start).total_seconds() / 86400


def median(values):
    return statistics.median(values) if values else None


def fmt(v, spec="+.2f", none="n/a"):
    return none if v is None else format(v, spec)


def reason_group(reason):
    if reason.startswith("unresolved:"):
        return "unresolved (valued at zero)"
    return reason


def open_book(state, now):
    """(band label, count, invested, median SL %, median TP %) per age band."""
    bands = defaultdict(list)
    for pos in state["positions"].values():
        age = days_between(utc(pos["opened_at"]), now)
        label = next(label for lo, hi, label in AGE_BANDS if lo <= age < hi)
        bands[label].append(pos)
    rows = []
    for _, _, label in AGE_BANDS:
        group = bands.get(label, [])
        sl = [(1 - p["sl_price"] / p["entry_price"]) * 100 for p in group if p.get("entry_price")]
        tp = [(p["tp_price"] / p["entry_price"] - 1) * 100 for p in group if p.get("entry_price")]
        rows.append((label, len(group), sum(p["invested"] for p in group), median(sl), median(tp)))
    return rows


def buys_per_day(state, since):
    opened = [utc(p["opened_at"]) for p in state["positions"].values()]
    opened += [utc(t["opened_at"]) for t in state["closed_trades"]]
    return sorted(Counter(t.date() for t in opened if t >= since).items())


def closed_summary(trades):
    """exit group -> (n, total pnl, avg pnl, median hold hours)."""
    groups = defaultdict(list)
    for t in trades:
        groups[reason_group(t["reason"])].append(t)
    out = {}
    for group, ts in groups.items():
        holds = [days_between(utc(t["opened_at"]), utc(t["closed_at"])) * 24 for t in ts]
        pnl = [t["pnl"] for t in ts]
        out[group] = (len(ts), sum(pnl), sum(pnl) / len(ts), median(holds))
    return out


def profit_factor(trades):
    gains = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    return gains / losses if losses else None


def watermark_counts(trades, levels=(10, 20, 30, 50)):
    """How many stop-loss trades ran up at least +X% before stopping, and how many
    take-profit trades fell at least X% before reaching their target."""
    stops = [t for t in trades if t["reason"] == "stop-loss hit" and t.get("max_runup_pct") is not None]
    targets = [t for t in trades if t["reason"] == "take-profit hit" and t.get("max_drawdown_pct") is not None]
    runup = {x: sum(t["max_runup_pct"] >= x for t in stops) for x in levels}
    drawdown = {x: sum(t["max_drawdown_pct"] <= -x for t in targets) for x in levels}
    return len(stops), runup, len(targets), drawdown


def token_key(chain, address):
    return (chain, (address or "").lower() if chain == "base" else address)


def entry_volatility_index(candidate_rows):
    """(chain, address) -> [(timestamp, volatility_pct)] for candidates the bot bought."""
    index = defaultdict(list)
    for c in candidate_rows:
        if c.get("bought") and c.get("timestamp"):
            index[token_key(c.get("chain"), c.get("address"))].append((utc(c["timestamp"]), c.get("volatility_pct")))
    return index


def entry_tp_pct(trade, index):
    """The take-profit distance this trade was opened with, or None if unknown.
    Closed trades do not store their levels, so they are rebuilt from the
    volatility logged with the same assessment."""
    opened = utc(trade["opened_at"])
    matches = [(abs((ts - opened).total_seconds()), vol)
               for ts, vol in index.get(token_key(trade.get("chain"), trade.get("address")), [])]
    matches = [m for m in matches if m[0] <= MATCH_WINDOW_S and isinstance(m[1], (int, float))]
    if not matches:
        return None
    vol = min(matches)[1]
    return min(150.0, max(30.0, vol * 2.5))


def position_tp_pct(pos):
    return (pos["tp_price"] / pos["entry_price"] - 1) * 100 if pos.get("entry_price") else None


def floor_class(tp_pct):
    if tp_pct is None:
        return "unknown"
    return "target at 30% floor" if tp_pct <= TP_FLOOR_PCT + 1e-6 else "target above floor"


def floor_comparison(closed, positions, index, now):
    """class -> dict of closed/open statistics."""
    groups = defaultdict(lambda: {"closed": [], "open": []})
    for t in closed:
        groups[floor_class(entry_tp_pct(t, index))]["closed"].append(t)
    for pos in positions.values():
        groups[floor_class(position_tp_pct(pos))]["open"].append(pos)
    out = {}
    for label, g in groups.items():
        holds = [days_between(utc(t["opened_at"]), utc(t["closed_at"])) * 24 for t in g["closed"]]
        out[label] = {
            "closed": len(g["closed"]), "realised": sum(t["pnl"] for t in g["closed"]),
            "pf": profit_factor(g["closed"]), "median_hold": median(holds),
            "win_rate": (sum(t["pnl"] > 0 for t in g["closed"]) / len(g["closed"]) * 100) if g["closed"] else None,
            "open": len(g["open"]), "open_invested": sum(p["invested"] for p in g["open"]),
            "open_7d": sum(days_between(utc(p["opened_at"]), now) >= 7 for p in g["open"]),
        }
    return out


def hold_shares(closed, thresholds_days=(1, 3, 7)):
    """exit -> (n, {days: share of closes held longer than that})."""
    out = {}
    for reason in ("take-profit hit", "stop-loss hit"):
        holds = [days_between(utc(t["opened_at"]), utc(t["closed_at"])) for t in closed if t["reason"] == reason]
        if holds:
            out[reason] = (len(holds), {d: sum(h > d for h in holds) / len(holds) * 100 for d in thresholds_days})
    return out


def unquoted_positions(positions):
    return [(p.get("symbol", "?"), p.get("price_status"), p.get("implausible_since") or p.get("unquotable_since"),
             p.get("opened_at")) for p in positions.values() if p.get("price_status") not in (None, "observed")]


def print_exits(state, closed, candidates_path, now):
    from analyze_outcomes import load_jsonl
    try:
        index = entry_volatility_index(load_jsonl(candidates_path))
    except FileNotFoundError:
        index = {}
        print(f"  ({candidates_path} not found - closed trades cannot be matched to their entry levels)")

    print()
    print("EXIT LEVELS: trades whose 30% target was set by the floor, not by the coin's own movement")
    print(f"  {'group':<22}{'closed':>7}{'win%':>6}{'realised':>10}{'PF':>6}{'med hold':>10}"
          f"{'open':>6}{'open 7d+':>9}{'open inv':>9}")
    for label, g in sorted(floor_comparison(closed, state["positions"], index, now).items()):
        print(f"  {label:<22}{g['closed']:>7}{fmt(g['win_rate'], '.0f'):>6}{g['realised']:>+10.2f}"
              f"{fmt(g['pf'], '.2f'):>6}{fmt(g['median_hold'], '.0f') + ('h' if g['median_hold'] is not None else ''):>10}"
              f"{g['open']:>6}{g['open_7d']:>9}{g['open_invested']:>9.2f}")

    print()
    print("HOW LONG EXITS TAKE: share of closes that came after")
    for reason, (n, shares) in hold_shares(closed).items():
        print(f"  {reason:<16} (n={n:>3})  " + "  ".join(f">{d}d: {v:4.0f}%" for d, v in shares.items()))

    stuck = unquoted_positions(state["positions"])
    print()
    print(f"OPEN POSITIONS WITHOUT A VALID QUOTE: {len(stuck)}")
    for symbol, status, since, opened in stuck:
        print(f"  {symbol:<12}{status or '?':<18} since {since or '?'}   opened {opened}")
    unresolved = [t for t in state["closed_trades"] if t["reason"].startswith("unresolved:")]
    print(f"  Closed as unresolved (all time): {len(unresolved)}"
          + (" - " + ", ".join(sorted({t.get('symbol') or '?' for t in unresolved})) if unresolved else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="paperbot_state.json")
    ap.add_argument("--since", default=DEFAULT_SINCE, help=f"Only trades opened at/after this UTC time (default {DEFAULT_SINCE}).")
    ap.add_argument("--exits", action="store_true", help="Exit-level analysis instead of the overview.")
    ap.add_argument("--candidates", default="paperbot_candidates.jsonl", help="Candidate log, for entry volatility (--exits).")
    args = ap.parse_args()
    since = utc(args.since)
    now = datetime.now(timezone.utc)

    with open(args.state, encoding="utf-8") as f:
        state = json.load(f)
    closed = [t for t in state["closed_trades"] if utc(t["opened_at"]) >= since]

    print(f"Trade review - since {args.since} UTC, as of {now:%d %b %H:%M} UTC")
    positions = state["positions"]
    invested = sum(p["invested"] for p in positions.values())
    print(f"  Cash {state['cash']:.2f} | {len(positions)} open positions, {invested:.2f} invested")
    if args.exits:
        return print_exits(state, closed, args.candidates, now)

    print("\nOPEN BOOK by age (hit neither stop nor target yet):")
    print(f"  {'age':<10}{'positions':>10}{'invested':>10}{'median SL':>11}{'median TP':>11}")
    for label, n, inv, sl, tp in open_book(state, now):
        print(f"  {label:<10}{n:>10}{inv:>10.2f}{fmt(sl, '.0f') + ('%' if sl is not None else ''):>11}"
              f"{fmt(tp, '.0f') + ('%' if tp is not None else ''):>11}")

    print("\nBUYS PER DAY (open + closed positions, by open date):")
    days = buys_per_day(state, since)
    print("  " + "  ".join(f"{d:%d %b}: {n}" for d, n in days) if days else "  none")

    print(f"\nCLOSED TRADES: {len(closed)}, realised {sum(t['pnl'] for t in closed):+.2f}, "
          f"profit factor {fmt(profit_factor(closed), '.2f')}")
    print(f"  {'exit':<30}{'n':>5}{'total':>9}{'avg':>8}{'median hold':>13}")
    for group, (n, total, avg, hold) in sorted(closed_summary(closed).items(), key=lambda kv: -kv[1][0]):
        print(f"  {group:<30}{n:>5}{total:>+9.2f}{avg:>+8.2f}{fmt(hold, '>10.1f'):>11}h")

    n_stops, runup, n_targets, drawdown = watermark_counts(closed)
    print("\nWHAT THE EXTREMES SAY (watermark-based; order of events only partly known):")
    print(f"  Of {n_stops} stop-loss trades, how many had first run up by at least:  "
          + "  ".join(f"+{x}%: {c}" for x, c in runup.items()))
    print(f"  Of {n_targets} take-profit trades, how many had first fallen by at least: "
          + "  ".join(f"-{x}%: {c}" for x, c in drawdown.items()))
    print("  A stop-loss trade that ran up first is one a lower target or a trailing stop could have")
    print("  closed as a win. A target trade that fell far first is one a tighter stop would have lost.")


if __name__ == "__main__":
    main()
