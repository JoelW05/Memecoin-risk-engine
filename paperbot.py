#!/usr/bin/env python3
"""Paper-trading bot: discovers new memecoins, scores them with analyzer.py,
and — with a SIMULATED balance only, no real wallet, no real money — "buys"
the ones that clear a risk-score bar at the tool's suggested position size,
then watches them against the tool's suggested stop-loss/take-profit.

This exists to answer one question before any real money is involved: would
blindly following this scorer's advice have made or lost money? It is not
a trading strategy recommendation, and a good backtest here still wouldn't
make it one — see the disclaimer printed on every report.

Discovery defaults to GeckoTerminal's free "trending pools" feed (ranked by
real 5m/1h trading activity, not paid promotion) for Solana and Base. The
older Dexscreener "latest boosted tokens" feed is still available via
--discovery-source boosts, but boosts are PAID promotion slots — plenty of
low-quality projects buy boosts specifically to get in front of bots like
this one, which is why it tended to buy too often and too indiscriminately.

Entry is gated by the scorer's risk score AND a handful of separate bot-level
filters (min liquidity, min pool age, requires at least one official social
link, and a per-cycle buy cap) — see --help. The scorer flags risk; these
filters control how trigger-happy the bot itself is on top of that.

Every simulated trade also has round-trip DEX fee + slippage + gas costs
deducted (see --buy-fee-pct/--sell-fee-pct/--gas-cost-usd) so the P&L in
--report reflects what a real trade would actually net, not a fantasy
frictionless fill — fixed gas cost in particular can dominate on the
smallest position sizes this bot opens.

Usage:
  python paperbot.py                 # run continuously (Ctrl+C to stop)
  python paperbot.py --once          # single discovery+check cycle, then exit
  python paperbot.py --status        # print current paper portfolio, exit
  python paperbot.py --report        # print a full performance breakdown
  python paperbot.py --reset --pot 100   # wipe state, start fresh with a 100-unit pot

Pot units: Dexscreener prices are USD. For simplicity this simulator treats
your pot as USD-equivalent (e.g. "£100" is tracked as "100 units") — it does
not apply a GBP/USD exchange rate, so real GBP P&L would also carry FX risk
this simulation doesn't model.

Research data: every candidate the scorer evaluates - bought or not - is
appended to paperbot_candidates.jsonl (one JSON object per line) with its
full structured score breakdown, data-source availability, and the exact
config that produced the verdict. This is the raw dataset for any rule-level
or cohort analysis; --report only summarizes actual trades, not the full
candidate pool the bot chose from.

Skipped AND bought candidates get their price followed at fixed checkpoints
(1h, 6h, 24h by default - see --outcome-checkpoints-hours) with zero simulated
money spent, logged to paperbot_outcomes.jsonl; the verdict field says which
cohort a row is from. A position whose token has no indexed listing anywhere
is carried unresolved and, after --unresolved-close-hours (default 24), closed
at zero under a reason that says so - an accounting policy, not a rug claim. Without this, only bought
coins would ever have a labeled outcome, silently conditioning every result
on the bot's own entry filters instead of on what discovery actually saw.
"""

import argparse
import csv
import io
import json
import math
import uuid
from pathlib import Path
import os
import sys
import time
from datetime import datetime, timezone

# One writer per state file (see acquire_state_lock). Windows and POSIX expose
# different primitives; either is fine, and a crash releases both automatically.
try:
    import msvcrt
except ImportError:  # pragma: no cover - not Windows
    msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from state_store import StateTransaction, PendingCommit, append_text, log_is_empty, recover, journal_path, atomic_json

from analyzer import ANALYZER_VERSION, FETCH_FAILED, fetch_json, fmt_price, full_check, get_current_price, get_current_prices, risk_label

# Bump alongside meaningful changes to entry-filter logic (try_open_position,
# run_discovery) for the same reason ANALYZER_VERSION exists: every logged
# candidate/trade stamps both versions, so a rule change never silently gets
# pooled with data from before the change as if it were one experiment.
PAPERBOT_VERSION = "1.6.2"

# Token symbols/names routinely contain emoji or other characters outside the
# Windows console's default cp1252 codepage — without this, a single unlucky
# coin name would crash a long-running bot mid-cycle.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

STATE_FILE_DEFAULT = "paperbot_state.json"
LOG_FILE_DEFAULT = "paperbot_trades.csv"
CANDIDATE_LOG_FILE_DEFAULT = "paperbot_candidates.jsonl"
OUTCOME_LOG_FILE_DEFAULT = "paperbot_outcomes.jsonl"
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
GECKOTERMINAL_TRENDING_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/trending_pools?duration={duration}&page={page}"
GECKOTERMINAL_NEW_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/new_pools?page={page}"
# Dexscreener occasionally returns a glitched quote for one tick rather than a
# real tradeable price (seen in production: a position closed "take-profit hit"
# against an exit price ~600,000,000x its entry - obviously no real counterparty
# ever traded at that price). Since max configured take-profit is 150% (2.5x
# entry), any single-tick price implying far more than that is bad data, not a
# result - reject and re-check next tick rather than closing a position on it.
MAX_PLAUSIBLE_PRICE_MULTIPLE = 100

# Wrapped SOL's mint - the most liquid token on Solana, used purely as an
# always-should-be-fetchable benchmark to distinguish "this coin's liquidity
# is actually gone" from "Dexscreener itself is down/rate-limited right now."
SOL_BENCHMARK_MINT = "So11111111111111111111111111111111111111112"

# Hard cap on how many skipped coins can be mid-tracking at once - without this,
# a bot running for weeks against a firehose discovery feed could accumulate an
# unbounded dict and an unbounded number of price-check API calls per cycle.
MAX_PENDING_OUTCOMES = 12000

# Cash above this multiple of the starting pot cannot come from simulated
# trading and indicates a corrupted state file - see repair_cash.py.
IMPLAUSIBLE_CASH_MULTIPLE = 50


_STATE_LOCK_HANDLE = None


def acquire_state_lock(state_file):
    """Refuse to run two bots against one state file. Seen in production: a
    manual run overlapping the scheduled task, both saving the same JSON. The
    lock is an OS-level lock on <state_file>.lock held for the life of the
    process, so a crash releases it without cleanup. Returns True if acquired."""
    global _STATE_LOCK_HANDLE
    if _STATE_LOCK_HANDLE is not None:
        return False
    state_file = str(Path(state_file).resolve())
    handle = open(state_file + ".lock", "a+", encoding="utf-8")
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _STATE_LOCK_HANDLE = handle
    return True


def release_state_lock():
    global _STATE_LOCK_HANDLE
    handle, _STATE_LOCK_HANDLE = _STATE_LOCK_HANDLE, None
    if handle is None:
        return
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path, starting_pot):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("pending_outcomes", {})  # older state files predate outcome tracking
        return state
    return {
        "starting_pot": starting_pot,
        "cash": starting_pot,
        "positions": {},       # token_address -> {...}
        "closed_trades": [],   # list of {...}
        "seen_addresses": [],  # discovery de-dupe, most-recent last
        "pending_outcomes": {},  # token_address -> {...} - skipped candidates awaiting checkpoint price checks
        "started_at": now_iso(),
    }


def save_state(path, state):
    atomic_json(path, state)


def log_trade(path, row):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    if log_is_empty(path):
        writer.writerow(["timestamp", "action", "chain", "symbol", "address", "price", "amount", "tokens", "reason", "score", "pnl", "cash_after"])
    writer.writerow(row)
    append_text(path, buffer.getvalue())


def log_candidate(path, chain, token_address, result, verdict, args):
    """Every candidate the bot EVALUATES, bought or not, one JSON object per
    line. This is the cohort-bias fix: without it, only knowing what got
    bought means every downstream analysis is silently conditioned on the
    bot's own filters, not on what discovery actually surfaced. Append-only,
    never rewritten - safe to tail while the bot is running."""
    row = {
        "timestamp": now_iso(),
        "candidate_id": uuid.uuid4().hex,
        "chain": chain,
        "address": token_address,
        "symbol": result.get("symbol"),
        "name": result.get("name"),
        "score": result.get("score"),
        "confidence": result.get("confidence"),
        "score_components": result.get("score_components"),
        "data_sources": result.get("data_sources"),
        "liquidity_usd": result.get("liquidity_usd"),
        "volume_24h": result.get("volume_24h"),
        "price_usd": result.get("price_usd"),
        "created_ms": result.get("created_ms"),
        "volatility_pct": result.get("volatility_pct"),
        "verdict": verdict,
        "bought": verdict.startswith("BUY"),
        "analyzer_version": result.get("analyzer_version", ANALYZER_VERSION),
        "paperbot_version": PAPERBOT_VERSION,
        "config": {
            "max_risk_score": args.max_risk_score, "min_liquidity_usd": args.min_liquidity_usd,
            "min_pool_age_hours": args.min_pool_age_hours, "require_socials": args.require_socials,
            "max_buys_per_cycle": args.max_buys_per_cycle, "max_positions": args.max_positions,
            "sizing": args.sizing, "discovery_source": args.discovery_source,
            **{key: getattr(args, key, None) for key in (
                "buy_fee_pct", "sell_fee_pct", "gas_cost_usd", "unresolved_close_hours",
                "outcome_checkpoints_hours", "checkpoint_grace_seconds", "track_bought_outcomes",
                "track_skipped_outcomes", "discovery_interval", "tick_interval", "trending_pages",
                "trending_durations", "include_new_pools")},
        },
    }
    append_text(path, json.dumps(row, allow_nan=False) + "\n")
    return row


def log_outcome_checkpoint(path, row):
    append_text(path, json.dumps(row, allow_nan=False) + "\n")


def track_candidate_for_outcomes(state, chain, token_address, result, verdict, args, candidate=None):
    """Follow a candidate's price at fixed horizons with zero simulated money
    spent. Skipped coins would otherwise vanish with no record of what
    happened to them; bought coins get a trade outcome but not one on the
    same fixed horizons, so both are tracked (see --track-bought-outcomes)
    and the verdict field says which cohort a row belongs to. Without this, any analysis of "does this
    score/rule actually predict outcomes" is silently conditioned on the
    bot's own entry filters, since only coins that passed them would ever
    have a labeled result."""
    is_buy = verdict.startswith("BUY")
    if is_buy and not getattr(args, "track_bought_outcomes", True):
        return
    if not is_buy and not args.track_skipped_outcomes:
        return
    if token_address in state["pending_outcomes"]:
        return
    try:
        price = float(result["price_usd"])
    except (TypeError, ValueError):
        return
    if not math.isfinite(price) or price <= 0:
        return
    if len(state["pending_outcomes"]) >= MAX_PENDING_OUTCOMES:
        for cp in sorted({int(h) for h in args.outcome_checkpoints_hours.split(",")}):
            log_outcome_checkpoint(args.outcome_log_file, {
                "address": token_address, "chain": chain, "candidate_id": (candidate or {}).get("candidate_id"),
                "discovered_at": (candidate or {}).get("timestamp", now_iso()), "checked_at": now_iso(),
                "checkpoint_hours": cp, "observation_status": "not_tracked_at_capacity",
                "score_at_discovery": result.get("score"), "price_change_pct": None,
                "rugged_or_delisted": None, "observer_version": PAPERBOT_VERSION,
            })
        return
    state["pending_outcomes"][token_address] = {
        "chain": chain, "symbol": result.get("symbol"), "score": result.get("score"),
        "confidence": result.get("confidence"), "verdict": verdict,
        "discovered_at": (candidate or {}).get("timestamp", now_iso()), "discovery_price": price, "checkpoints_done": [],
        "candidate_id": (candidate or {}).get("candidate_id"),
        "score_components": result.get("score_components"),
        "analyzer_version": result.get("analyzer_version", ANALYZER_VERSION),
        "paperbot_version": PAPERBOT_VERSION,
    }


def check_pending_outcomes(state, args):
    """Record observations at their actual time; missing data is not a rug.

    A late check cannot reconstruct an earlier price. Expired checkpoints are
    explicitly censored and never included as returns at the nominal horizon.
    """
    checkpoints = sorted({int(h) for h in args.outcome_checkpoints_hours.split(",")})
    grace = getattr(args, "checkpoint_grace_seconds", 600)
    now = datetime.now(timezone.utc)
    due = []
    requested = {}
    for addr, entry in state["pending_outcomes"].items():
        elapsed = (now - datetime.fromisoformat(entry["discovered_at"])).total_seconds()
        for cp in checkpoints:
            if cp not in entry.get("checkpoints_done", []) and elapsed >= cp * 3600:
                due.append((addr, entry, cp))
                if elapsed - cp * 3600 <= grace:
                    requested.setdefault(entry["chain"], []).append(addr)
    prices = {chain: get_current_prices(chain, addresses) for chain, addresses in requested.items()}
    for addr, entry, cp in due:
        checked = datetime.now(timezone.utc)
        elapsed = (checked - datetime.fromisoformat(entry["discovered_at"])).total_seconds()
        late = elapsed - cp * 3600
        price = prices.get(entry["chain"], {}).get(addr, FETCH_FAILED)
        status = "observed"
        if late > grace:
            status = "missed_checkpoint"
        elif price is FETCH_FAILED:
            status = "fetch_failed"
        elif price is None:
            status = "no_pairs"
        elif not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            status = "invalid_price"
        elif price > entry["discovery_price"] * MAX_PLAUSIBLE_PRICE_MULTIPLE:
            status = "implausible_price"
        if status == "fetch_failed":
            # Still inside the grace window (otherwise it would be a missed
            # checkpoint already): try again next cycle instead of censoring
            # a horizon on one transient failure. Count the attempts so the
            # row that is eventually written says how hard it was to get.
            entry["fetch_failures"] = entry.get("fetch_failures", 0) + 1
            continue
        valid = status == "observed"
        log_outcome_checkpoint(args.outcome_log_file, {
            "fetch_failures_before_observation": entry.get("fetch_failures", 0),
            "address": addr, "chain": entry["chain"], "symbol": entry.get("symbol"),
            "candidate_id": entry.get("candidate_id"),
            "score_components": entry.get("score_components"),
            "discovered_at": entry["discovered_at"], "discovery_price": entry["discovery_price"],
            "score_at_discovery": entry.get("score"), "confidence_at_discovery": entry.get("confidence"),
            "verdict_at_discovery": entry.get("verdict"),
            "checkpoint_hours": cp, "checked_at": checked.isoformat(timespec="seconds"),
            "actual_elapsed_hours": elapsed / 3600, "lateness_seconds": late,
            "observation_status": status,
            "price_at_checkpoint": price if valid else None,
            "price_change_pct": round((price / entry["discovery_price"] - 1) * 100, 2) if valid else None,
            "rugged_or_delisted": None,
            "analyzer_version": entry.get("analyzer_version", "legacy_unknown"),
            "paperbot_version": entry.get("paperbot_version", "legacy_unknown"),
            "observer_version": PAPERBOT_VERSION,
        })
        entry["checkpoints_done"] = sorted(set(entry.get("checkpoints_done", [])) | {cp})
    for addr in list(state["pending_outcomes"]):
        if set(state["pending_outcomes"][addr].get("checkpoints_done", [])) >= set(checkpoints):
            del state["pending_outcomes"][addr]


def fetch_boost_candidates(chains):
    data = fetch_json(BOOSTS_URL)
    if not isinstance(data, list):
        return []
    return [
        item for item in data
        if item.get("chainId") in chains and item.get("tokenAddress")
    ]


def _extract_token_address(pool):
    base_token_id = ((pool.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
    # ids look like "solana_<address>" / "base_<address>"
    return base_token_id.split("_", 1)[1] if "_" in base_token_id else None


def fetch_trending_candidates(chains, durations, pages, include_new_pools):
    """GeckoTerminal's trending-pools feed, ranked by real recent trading
    activity rather than paid promotion, plus (optionally) its new_pools feed
    for organic fresh listings trending can't surface yet. Network ids
    ('solana', 'base') line up 1:1 with our chain ids.

    Trending alone tends to keep surfacing the same handful of coins cycle
    after cycle (that's what "trending" means), which is why this pulls
    multiple pages and unions in new_pools too — a genuinely wider net rather
    than just the same top-20 every time."""
    out = []
    seen_this_fetch = set()

    def add_pools(pools, chain):
        for pool in pools:
            token_address = _extract_token_address(pool)
            if not token_address or token_address in seen_this_fetch:
                continue
            seen_this_fetch.add(token_address)
            out.append({"chainId": chain, "tokenAddress": token_address})

    for chain in chains:
        for duration in durations:
            for page in range(1, pages + 1):
                url = GECKOTERMINAL_TRENDING_URL.format(network=chain, duration=duration, page=page)
                data = fetch_json(url)
                add_pools((data or {}).get("data") or [], chain)
        if include_new_pools:
            for page in range(1, pages + 1):
                url = GECKOTERMINAL_NEW_POOLS_URL.format(network=chain, page=page)
                data = fetch_json(url)
                add_pools((data or {}).get("data") or [], chain)
    return out


def portfolio_summary(state, refresh_prices=True):
    cash = state["cash"]
    invested = 0.0
    unrealized = 0.0
    lines = []
    unpriced = []
    unpriced_cost = 0.0
    unrealized_priced = 0.0
    for addr, pos in state["positions"].items():
        invested += pos["invested"]
        price = get_current_price(pos["chain"], addr) if refresh_prices else pos["entry_price"]
        if price is not None and price > pos["entry_price"] * MAX_PLAUSIBLE_PRICE_MULTIPLE:
            # Same rule the trading loop uses: an implausible quote is no quote.
            # Valuing at it once reported +18,227% equity from one position.
            price = None
        if price is None:
            unpriced.append(addr)
            unpriced_cost += pos["invested"]
            price = float("nan")  # unknown valuation must not silently become entry cost
        value = pos["tokens"] * price
        unrealized += value - pos["invested"]
        if not math.isnan(value):
            unrealized_priced += value - pos["invested"]
        lines.append((pos["symbol"], pos["chain"], pos["invested"], value, value - pos["invested"], pos["sl_price"], pos["tp_price"], price))
    realized = sum(t["pnl"] for t in state["closed_trades"])
    equity = cash + invested + unrealized
    wins = sum(1 for t in state["closed_trades"] if t["pnl"] > 0)
    total_closed = len(state["closed_trades"])
    return {
        "cash": cash, "invested": invested, "unrealized": unrealized,
        "realized": realized, "equity": equity, "open_lines": lines,
        "wins": wins, "total_closed": total_closed,
        "unpriced": unpriced,
        # Equity over the positions that DO have a trustworthy price, with the
        # unpriced ones' cost stated separately. "equity" above stays NaN when
        # anything is unpriced - the honest total - but one broken quote should
        # not hide the whole portfolio from view.
        "unpriced_cost": unpriced_cost,
        "equity_priced": cash + (invested - unpriced_cost) + unrealized_priced,
        "unrealized_priced": unrealized_priced,
    }


def print_summary(state):
    s = portfolio_summary(state)
    pot = state["starting_pot"]
    print()
    print(f"=== Paper portfolio (started at {pot:.2f}) ===")
    print(f"  Cash:              {s['cash']:.2f}")
    print(f"  Invested (open):   {s['invested']:.2f}")
    if s["unpriced"]:
        print(f"  Unrealized P&L:    {s['unrealized_priced']:+.2f} on priced positions; "
              f"{len(s['unpriced'])} unpriced ({s['unpriced_cost']:.2f} at cost) excluded")
    else:
        print(f"  Unrealized P&L:    {s['unrealized']:+.2f}")
    print(f"  Realized P&L:      {s['realized']:+.2f}  ({s['wins']}/{s['total_closed']} winners)")
    if s["unpriced"]:
        eq = s["equity_priced"]
        print(f"  Total equity:      unknown - {eq:.2f} ({(eq / pot - 1) * 100:+.1f}%) excluding "
              f"{len(s['unpriced'])} unpriced position(s) whose value cannot be observed; "
              f"between {eq:.2f} and {eq + s['unpriced_cost']:.2f} if they are worth zero to cost")
    else:
        print(f"  Total equity:      {s['equity']:.2f}  ({(s['equity'] / pot - 1) * 100:+.1f}% since start)")
    if s["open_lines"]:
        print(f"  Open positions ({len(s['open_lines'])}):")
        for symbol, chain, invested, value, pnl, sl_price, tp_price, price in s["open_lines"]:
            print(f"    {symbol:<12} {chain:<7} invested {invested:>7.2f}  now {value:>7.2f}  ({pnl:+.2f})   "
                  f"price {fmt_price(price)}  SL {fmt_price(sl_price)}  TP {fmt_price(tp_price)}")
    print()


def print_report(state):
    s = portfolio_summary(state)
    pot = state["starting_pot"]
    closed = state["closed_trades"]

    print()
    print("=== Paper-trading performance report ===")
    started = state.get("started_at")
    if started:
        try:
            started_dt = datetime.fromisoformat(started)
            running_for = datetime.now(timezone.utc) - started_dt
            hours = running_for.total_seconds() / 3600
            print(f"Running since: {started}  ({hours:.1f}h)")
        except ValueError:
            pass
    print(f"Starting pot:  {pot:.2f}")
    print(f"Current equity: {s['equity']:.2f}  ({(s['equity'] / pot - 1) * 100:+.1f}% since start)" if not s["unpriced"] else f"Current equity: unknown ({len(s['unpriced'])} unpriced positions; nan means unknown)")
    print(f"  Cash: {s['cash']:.2f}   Invested (open): {s['invested']:.2f}   Unrealized P&L: {s['unrealized']:+.2f}")
    print()

    total_closed = len(closed)
    if total_closed == 0:
        print("No closed trades yet — everything's still open or nothing's fired. Nothing to break down yet.")
        print()
        return

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    win_rate = len(wins) / total_closed * 100
    avg_pnl = sum(t["pnl"] for t in closed) / total_closed
    best = max(closed, key=lambda t: t["pnl"])
    worst = min(closed, key=lambda t: t["pnl"])

    print(f"Closed trades: {total_closed}  ({len(wins)} wins / {len(losses)} losses, win rate {win_rate:.0f}%)")
    print(f"  Realized P&L:          {s['realized']:+.2f}")
    print(f"  Avg P&L per trade:     {avg_pnl:+.2f}")
    print(f"  Best trade:  {best['symbol']:<12} {best['pnl']:+.2f}  ({best['reason']})")
    print(f"  Worst trade: {worst['symbol']:<12} {worst['pnl']:+.2f}  ({worst['reason']})")
    print()

    print("By exit reason:")
    reasons = {}
    for t in closed:
        reasons.setdefault(t["reason"], []).append(t["pnl"])
    for reason, pnls in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"  {reason:<45} {len(pnls):>3} trades   total {sum(pnls):+8.2f}   avg {sum(pnls) / len(pnls):+6.2f}")
    print()

    print("By entry risk-score bucket (open + closed):")
    buckets = [(0, 20, "0-20   (Lower)"), (21, 45, "21-45  (Moderate)"), (46, 70, "46-70  (High)"), (71, 100, "71-100 (Extreme)")]
    all_trades = closed + [
        {"score": p["score"], "pnl": None}
        for p in state["positions"].values()
    ]
    for lo, hi, label in buckets:
        bucket_trades = [t for t in all_trades if lo <= t["score"] <= hi]
        if not bucket_trades:
            continue
        bucket_closed = [t for t in bucket_trades if t["pnl"] is not None]
        open_count = len(bucket_trades) - len(bucket_closed)
        if bucket_closed:
            bucket_win_rate = sum(1 for t in bucket_closed if t["pnl"] > 0) / len(bucket_closed) * 100
            bucket_pnl = sum(t["pnl"] for t in bucket_closed)
            print(f"  {label:<20} {len(bucket_closed)} closed ({bucket_win_rate:.0f}% win rate, {bucket_pnl:+.2f} total), {open_count} still open")
        else:
            print(f"  {label:<20} 0 closed, {open_count} still open")
    print()

    print("This is a simulation of one specific heuristic scorer against real live prices —")
    print("a good run here is evidence, not proof, and a bad run on a small sample doesn't")
    print("prove the opposite either. Longer runs and more closed trades matter more than")
    print("any single day's number. Not financial advice.")
    print()


def pick_sizing_pct(position_size, mode):
    if mode == "max":
        return position_size["max_pct"]
    if mode == "mid":
        return (position_size["min_pct"] + position_size["max_pct"]) / 2
    return position_size["min_pct"]  # "min" — the conservative default


def validate_trading_cash(state):
    """Refuse inconsistent/nonfinite accounting; never repair state implicitly."""
    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Accounting contains a missing or nonfinite number.")
        return value
    pot, cash = number(state.get("starting_pot")), number(state.get("cash"))
    if pot <= 0 or cash < 0 or cash > pot * IMPLAUSIBLE_CASH_MULTIPLE:
        raise ValueError("Cash/pot outside permitted bounds; inspect repair_cash.py --dry-run.")
    invested = [number(p.get("invested")) for p in state.get("positions", {}).values()]
    if any(v < 0 for v in invested):
        raise ValueError("Negative open investment.")
    pnls = []
    for trade in state.get("closed_trades", []):
        cost, proceeds, pnl = (number(trade.get(k)) for k in ("invested", "proceeds", "pnl"))
        if cost < 0 or proceeds < 0 or abs(round(proceeds - cost, 2) - pnl) > 0.011:
            raise ValueError("Closed-trade accounting does not reconcile.")
        pnls.append(pnl)
    expected = round(pot - sum(invested) + sum(pnls), 2)
    if abs(expected - cash) > 0.011:
        raise ValueError(f"Cash does not reconcile: recorded {cash:.2f}, expected {expected:.2f}.")


def try_open_position(state, chain, token_address, result, args, buys_this_cycle):
    validate_trading_cash(state)
    pos_size = result["position_size"]
    levels = result["trade_levels"]
    score = result["score"]

    if score > args.max_risk_score:
        return f"skip (score {score} > max {args.max_risk_score})"
    if pos_size["max_pct"] <= 0:
        return "skip (sizing says sit this one out)"
    if not levels:
        return "skip (not enough volatility data for SL/TP)"
    if len(state["positions"]) >= args.max_positions:
        return f"skip (already at max {args.max_positions} open positions)"
    liquidity = result.get("liquidity_usd")
    if liquidity is None or liquidity < args.min_liquidity_usd:
        return f"skip (liquidity {liquidity if liquidity is not None else 'n/a'} < min {args.min_liquidity_usd:.0f})"
    created_ms = result.get("created_ms")
    if created_ms is None:
        return "skip (no pool-age data)"
    age_hours = (time.time() * 1000 - created_ms) / 3_600_000
    if age_hours < args.min_pool_age_hours:
        # retryable: "too young" is purely a matter of time, unlike the other
        # gates here - the coin deserves another look once it actually ages
        # past the bar instead of being blacklisted forever on first sight.
        return f"__RETRY__skip (pool age {age_hours:.1f}h < min {args.min_pool_age_hours}h)"
    if args.require_socials and not (result.get("websites") or result.get("socials")):
        return "skip (no official website/social links)"
    try:
        price = float(result["price_usd"])
    except (TypeError, ValueError):
        return "skip (no usable price)"
    if not math.isfinite(price) or price <= 0:
        return "skip (no usable price)"
    # this gate deliberately doesn't get remembered in seen_addresses — a coin
    # skipped only because THIS cycle's buy budget ran out is worth another
    # look next cycle, unlike one skipped for a real quality reason above.
    if buys_this_cycle[0] >= args.max_buys_per_cycle:
        return f"__RETRY__skip (cycle buy limit {args.max_buys_per_cycle} reached)"

    sizing_pct = pick_sizing_pct(pos_size, args.sizing)
    gross_amount = round(state["starting_pot"] * sizing_pct / 100, 2)
    if gross_amount <= 0:
        return "skip (sizing rounds to 0)"
    if gross_amount > state["cash"]:
        return f"skip (needs {gross_amount:.2f}, only {state['cash']:.2f} cash left)"

    # fees/slippage + fixed gas are taken out of the fixed cash outlay, so a
    # smaller net amount actually converts into tokens — this is what makes
    # the smallest position sizes brutal in practice, not just in theory.
    net_amount = gross_amount * (1 - args.buy_fee_pct / 100) - args.gas_cost_usd
    if net_amount <= 0:
        return f"skip (£{gross_amount:.2f} buy doesn't cover fees+gas)"

    tokens = net_amount / price
    state["cash"] = round(state["cash"] - gross_amount, 2)
    state["positions"][token_address] = {
        "chain": chain, "symbol": result.get("symbol") or "?", "name": result.get("name") or "?",
        "entry_price": price, "tokens": tokens, "invested": gross_amount,
        "sl_price": levels["sl_price"], "tp_price": levels["tp_price"],
        "score": score, "opened_at": now_iso(), "missing_price_streak": 0,
        # snapshot at entry - needed to later ask "did this specific rule/confidence
        # level actually predict the outcome", not just "what's the current score"
        "entry_confidence": result.get("confidence"),
        "entry_score_components": result.get("score_components"),
        "analyzer_version": result.get("analyzer_version", ANALYZER_VERSION),
        "paperbot_version": PAPERBOT_VERSION,
        # running high/low watermark in price terms, updated every tick this
        # stays open - gives max drawdown / max run-up on close, not just the
        # final exit price, which is what a real risk review needs.
        "min_price_seen": price, "max_price_seen": price,
    }
    buys_this_cycle[0] += 1
    log_trade(args.log_file, [
        now_iso(), "BUY", chain, result.get("symbol"), token_address, f"{price:.10g}",
        f"{gross_amount:.2f}", f"{tokens:.4f}", f"score {score} ({risk_label(score)}), sizing={args.sizing}", score, "", f"{state['cash']:.2f}",
    ])
    return f"BUY {gross_amount:.2f} @ {price:.10g} (score {score}, SL {levels['sl_price']:.10g}, TP {levels['tp_price']:.10g})"


def check_positions(state, args):
    to_close = []
    # Missing listings are unresolved observations, never executable zero-price fills.
    prices = {}
    for chain in sorted({p["chain"] for p in state["positions"].values()}):
        prices.update(get_current_prices(chain, [a for a, p in state["positions"].items() if p["chain"] == chain]))
    for addr, pos in state["positions"].items():
        price = prices.get(addr)
        if price is FETCH_FAILED:
            pos.pop("unquotable_since", None)
            pos["missing_price_streak"] = 0
            pos["price_status"] = "fetch_failed"
            print(f"  [WARN] {pos['symbol']:<10} price request failed outright - "
                  f"treating as an API outage, not this coin rugging. Not counted against it.")
            continue
        if price is None:
            pos["missing_price_streak"] = pos.get("missing_price_streak", 0) + 1
            pos["price_status"] = "no_pairs"
            pos.setdefault("unquotable_since", now_iso())
            hours_unquotable = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["unquotable_since"])).total_seconds() / 3600
            limit_h = getattr(args, "unresolved_close_hours", 24) or 0
            if limit_h and hours_unquotable >= limit_h:
                # Valuation policy, stated plainly in the reason: a token with no
                # indexed listing anywhere for this long is carried at zero. That
                # is an accounting decision, not a verified on-chain rug, and the
                # reason string is kept distinct from the legacy "rugged" closes
                # so the analysis reports it as its own category.
                to_close.append((addr, 0.0, f"unresolved: no indexed listing for {limit_h:g}h, valued at zero (not a verified rug)"))
            elif pos["missing_price_streak"] >= args.missing_price_limit:
                horizon = f"valued at zero after {limit_h:g}h" if limit_h else "left open indefinitely (--unresolved-close-hours 0)"
                print(f"  [WARN] {pos['symbol']:<10} no indexed listing for {hours_unquotable:.1f}h - unresolved, {horizon}.")
            continue
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            pos.pop("unquotable_since", None)
            pos["missing_price_streak"] = 0
            pos["price_status"] = "invalid_price"
            continue
        if price > pos["entry_price"] * MAX_PLAUSIBLE_PRICE_MULTIPLE:
            # An implausible quote is not an observation. Never act on it, never
            # let it touch the watermarks, and - the part that was missing -
            # never let it clear the clock. A one-tick spike clears itself when
            # the next plausible quote arrives; a quote that stays broken (seen
            # live: PUMP at ~5,900x entry for days) is carried unresolved and
            # valued at zero after the same limit as an unlisted token, under a
            # reason that says exactly why.
            pos["price_status"] = "implausible_price"
            pos.setdefault("implausible_since", now_iso())
            hours_bad = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["implausible_since"])).total_seconds() / 3600
            limit_h = getattr(args, "unresolved_close_hours", 24) or 0
            if limit_h and hours_bad >= limit_h:
                to_close.append((addr, 0.0, f"unresolved: no plausible quote for {limit_h:g}h, valued at zero (not a verified rug)"))
            else:
                print(f"  [WARN] {pos['symbol']:<10} ignoring implausible price {fmt_price(price)} "
                      f"(>{MAX_PLAUSIBLE_PRICE_MULTIPLE}x entry {fmt_price(pos['entry_price'])}) for {hours_bad:.1f}h - "
                      f"not an observation" + (f"; valued at zero after {limit_h:g}h" if limit_h else ""))
            continue
        pos["price_status"] = "observed"
        pos["missing_price_streak"] = 0
        pos.pop("unquotable_since", None)
        pos.pop("implausible_since", None)
        pos["min_price_seen"] = min(pos.get("min_price_seen", price), price)
        pos["max_price_seen"] = max(pos.get("max_price_seen", price), price)
        if price <= pos["sl_price"]:
            to_close.append((addr, price, "stop-loss hit"))
        elif price >= pos["tp_price"]:
            to_close.append((addr, price, "take-profit hit"))

    for addr, price, reason in to_close:
        pos = state["positions"].pop(addr)
        gross_proceeds = pos["tokens"] * price
        # rugged/delisted closes (price 0) still "pay" gas trying to exit — a
        # real wallet doesn't get gas back on a failed/worthless sell either.
        proceeds = round(max(0.0, gross_proceeds * (1 - args.sell_fee_pct / 100) - args.gas_cost_usd), 2)
        pnl = round(proceeds - pos["invested"], 2)
        state["cash"] = round(state["cash"] + proceeds, 2)
        entry_price = pos["entry_price"]
        min_seen = pos.get("min_price_seen", entry_price)
        max_seen = pos.get("max_price_seen", entry_price)
        trade = {
            "chain": pos["chain"], "symbol": pos["symbol"], "address": addr,
            "entry_price": entry_price, "exit_price": price,
            "invested": pos["invested"], "proceeds": proceeds, "pnl": pnl,
            "reason": reason, "opened_at": pos["opened_at"], "closed_at": now_iso(),
            # NOTE: "score" was missing from this dict prior to this instrumentation
            # pass despite print_report()'s risk-bucket breakdown reading t["score"]
            # on every closed trade - untriggered only because no real trade had
            # closed yet in any test run. Real bug, found while adding the fields below.
            "score": pos["score"],
            "max_drawdown_pct": round((min_seen / entry_price - 1) * 100, 2) if entry_price else None,
            "max_runup_pct": round((max_seen / entry_price - 1) * 100, 2) if entry_price else None,
            "entry_confidence": pos.get("entry_confidence"),
            "entry_score_components": pos.get("entry_score_components"),
            "analyzer_version": pos.get("analyzer_version", ANALYZER_VERSION),
            "paperbot_version": pos.get("paperbot_version", PAPERBOT_VERSION),
            "exit_paperbot_version": PAPERBOT_VERSION,
            "exit_sell_fee_pct": args.sell_fee_pct, "exit_gas_cost_usd": args.gas_cost_usd,
            "valuation_policy": "unresolved_zero" if reason.startswith("unresolved:") else "market_quote",
        }
        state["closed_trades"].append(trade)
        log_trade(args.log_file, [
            now_iso(), "SELL", pos["chain"], pos["symbol"], addr, f"{price:.10g}",
            f"{proceeds:.2f}", f"{pos['tokens']:.4f}", reason, pos["score"], f"{pnl:.2f}", f"{state['cash']:.2f}",
        ])
        print(f"  [SELL] {pos['symbol']:<10} {reason} — pnl {pnl:+.2f} (cash now {state['cash']:.2f})")


def run_discovery(state, args):
    chains = set(args.chains.split(","))
    if args.discovery_source == "boosts":
        candidates = fetch_boost_candidates(chains)
    else:
        candidates = fetch_trending_candidates(chains, args.trending_durations.split(","), args.trending_pages, args.include_new_pools)
    seen = set(state["seen_addresses"])
    new_count = 0
    buys_this_cycle = [0]  # mutable int-in-a-list so try_open_position can increment it
    for item in candidates:
        addr = item["tokenAddress"]
        if addr in seen or addr in state["positions"]:
            continue

        checked = full_check(addr)
        if not checked:
            seen.add(addr)
            state["seen_addresses"].append(addr)
            new_count += 1
            continue
        chain, token_address, result = checked
        if chain not in chains:
            continue
        label = risk_label(result["score"])
        verdict = try_open_position(state, chain, token_address, result, args, buys_this_cycle)
        retryable = verdict.startswith("__RETRY__")
        if retryable:
            verdict = verdict[len("__RETRY__"):]
        else:
            seen.add(addr)
            state["seen_addresses"].append(addr)
            new_count += 1
        # Logged regardless of verdict - this is what makes rule-level and
        # cohort analysis possible later: every coin the scorer actually saw,
        # not just the ones that got bought.
        candidate = log_candidate(args.candidate_log_file, chain, token_address, result, verdict, args)
        track_candidate_for_outcomes(state, chain, token_address, result, verdict, args, candidate)
        print(f"  [DISCOVER] {result.get('symbol') or '?':<10} {chain:<7} score {result['score']:>3} ({label:<13}) -> {verdict}")

    # cap unbounded growth of the de-dupe list
    state["seen_addresses"] = state["seen_addresses"][-3000:]
    if new_count == 0:
        print(f"  [DISCOVER] no new candidates from {args.discovery_source} since last check")


def main():
    parser = argparse.ArgumentParser(description="Paper-trading bot for the memecoin risk scorer. No real funds, no wallet — simulation only.")
    parser.add_argument("--pot", type=float, default=100.0, help="Starting simulated pot (only used the first time / with --reset). Default: 100.")
    parser.add_argument("--max-risk-score", type=int, default=30, help="Skip coins scoring above this (0-100). Default: 30 (lower-risk-leaning only; was 45).")
    parser.add_argument("--sizing", choices=["min", "mid", "max"], default="min", help="Which end of the tool's suggested %% band to use for position size. Default: min (conservative).")
    parser.add_argument("--max-positions", type=int, default=20, help="Cap on concurrent open paper positions. Default: 20.")
    parser.add_argument("--max-buys-per-cycle", type=int, default=3, help="Cap on new positions opened in a single discovery cycle, regardless of how many candidates pass. Default: 3 — this is the main knob for 'buying too often'.")
    parser.add_argument("--min-liquidity-usd", type=float, default=15000, help="Skip coins with pool liquidity below this, on top of whatever the score already penalizes. Default: 15000.")
    parser.add_argument("--min-pool-age-hours", type=float, default=6, help="Skip pools younger than this — avoids sniping brand-new pools outright. Default: 6.")
    parser.add_argument("--require-socials", dest="require_socials", action="store_true", default=True, help="Skip coins with no official website/Twitter/Telegram/Discord link (default: on).")
    parser.add_argument("--allow-no-socials", dest="require_socials", action="store_false", help="Disable the require-socials filter.")
    parser.add_argument("--buy-fee-pct", type=float, default=1.5, help="Simulated DEX fee + slippage on entry, as %% of trade size. Default: 1.5.")
    parser.add_argument("--sell-fee-pct", type=float, default=1.5, help="Simulated DEX fee + slippage on exit, as %% of trade size. Default: 1.5.")
    parser.add_argument("--gas-cost-usd", type=float, default=0.05, help="Simulated fixed network/gas cost per trade (buy and sell each pay this once). Default: 0.05.")
    parser.add_argument("--missing-price-limit", type=int, default=3, help="Consecutive missing listings before warning that a position is unresolved. Default: 3.")
    parser.add_argument("--unresolved-close-hours", type=float, default=24, help="A position with no indexed listing anywhere for this many consecutive hours is closed and carried at zero, with a reason that says exactly that (an accounting policy, not a verified rug). 0 = never close, leave such positions open. Default: 24.")
    parser.add_argument("--track-bought-outcomes", dest="track_bought_outcomes", action="store_true", default=True, help="Also observe BOUGHT coins at the fixed 1/6/24h horizons, so bought and skipped cohorts are comparable (default: on).")
    parser.add_argument("--no-track-bought-outcomes", dest="track_bought_outcomes", action="store_false", help="Disable fixed-horizon observation of bought coins.")
    parser.add_argument("--chains", default="solana,base", help="Comma-separated chain ids to scan. Default: solana,base.")
    parser.add_argument("--discovery-source", choices=["trending", "boosts"], default="trending", help="'trending' = GeckoTerminal trending pools (real activity, default). 'boosts' = Dexscreener paid-promotion feed (old default, noisier).")
    parser.add_argument("--trending-durations", default="5m,1h", help="Comma-separated GeckoTerminal trending windows to union (5m, 1h, 6h, 24h). Default: 5m,1h.")
    parser.add_argument("--trending-pages", type=int, default=2, help="Pages (20 pools each) to pull per chain/duration/feed. Default: 2. Raise this if discovery feels too repetitive - trending naturally keeps surfacing the same top coins, so more pages plus new_pools is the fix, not a shorter discovery-interval.")
    parser.add_argument("--include-new-pools", dest="include_new_pools", action="store_true", default=True, help="Also pull GeckoTerminal's new_pools feed (organic fresh listings, not paid) alongside trending (default: on).")
    parser.add_argument("--no-new-pools", dest="include_new_pools", action="store_false", help="Disable the new_pools feed, trending only.")
    parser.add_argument("--discovery-interval", type=int, default=300, help="Seconds between discovery scans. Default: 300 (5 min).")
    parser.add_argument("--tick-interval", type=int, default=30, help="Seconds between open-position price checks. Default: 30.")
    parser.add_argument("--state-file", default=STATE_FILE_DEFAULT)
    parser.add_argument("--log-file", default=LOG_FILE_DEFAULT)
    parser.add_argument("--candidate-log-file", default=CANDIDATE_LOG_FILE_DEFAULT, help="Append-only JSONL log of every candidate the scorer evaluates, bought or not - the raw dataset for rule-level and cohort analysis. Default: " + CANDIDATE_LOG_FILE_DEFAULT)
    parser.add_argument("--track-skipped-outcomes", dest="track_skipped_outcomes", action="store_true", default=True, help="Follow skipped (not bought) candidates' prices at fixed checkpoints, no simulated money spent, so outcome data isn't limited to only what passed the entry filters (default: on).")
    parser.add_argument("--no-track-skipped-outcomes", dest="track_skipped_outcomes", action="store_false", help="Disable outcome tracking for skipped candidates.")
    parser.add_argument("--outcome-checkpoints-hours", default="1,6,24", help="Comma-separated hours-after-discovery to check a skipped candidate's price. Default: 1,6,24.")
    parser.add_argument("--checkpoint-grace-seconds", type=int, default=600, help="Maximum checkpoint lateness before recording a missed observation. Default: 600.")
    parser.add_argument("--outcome-log-file", default=OUTCOME_LOG_FILE_DEFAULT, help="Append-only JSONL log of skipped-candidate price checkpoints. Default: " + OUTCOME_LOG_FILE_DEFAULT)
    parser.add_argument("--once", action="store_true", help="Run a single discovery + position-check cycle, then exit.")
    parser.add_argument("--status", action="store_true", help="Print the current paper portfolio and exit (no discovery, no trading).")
    parser.add_argument("--report", action="store_true", help="Print a full performance breakdown (win rate, P&L by exit reason and by risk-score bucket) and exit.")
    parser.add_argument("--reset", action="store_true", help="Wipe existing state and start a fresh pot.")
    args = parser.parse_args()
    if args.reset and (args.status or args.report):
        parser.error("--reset cannot be combined with read-only --status or --report.")
    output_paths = [Path(p).resolve() for p in (args.state_file, args.log_file, args.candidate_log_file, args.outcome_log_file)]
    if len(set(output_paths)) != len(output_paths):
        parser.error("State, trade, candidate and outcome paths must be distinct.")
    monetary_options = (args.pot, args.buy_fee_pct, args.sell_fee_pct, args.gas_cost_usd,
                        args.unresolved_close_hours, args.min_liquidity_usd, args.min_pool_age_hours)
    if any(not math.isfinite(v) or v < 0 for v in monetary_options) or args.pot <= 0:
        parser.error("Pot must be positive; fees, limits and valuation hours must be finite and nonnegative.")
    if args.buy_fee_pct >= 100 or args.sell_fee_pct >= 100:
        parser.error("Simulated percentage fees must be below 100.")
    if args.checkpoint_grace_seconds < 0 or args.tick_interval <= 0 or args.discovery_interval <= 0:
        parser.error("Intervals must be positive and checkpoint grace nonnegative.")
    try:
        horizons = [int(h) for h in args.outcome_checkpoints_hours.split(",")]
        if not horizons or any(h <= 0 for h in horizons):
            raise ValueError
    except ValueError:
        parser.error("Outcome checkpoints must be comma-separated positive integer hours.")

    if not (args.status or args.report) and not acquire_state_lock(args.state_file):
        sys.exit(f"Another paperbot process already holds {args.state_file}.lock - refusing to start a second writer "
                 f"against the same state. Stop it first (Stop-ScheduledTask / Stop-Process), then retry.")

    if args.status or args.report:
        if journal_path(args.state_file).exists():
            sys.exit("A cycle is awaiting recovery. Start the bot normally to finish the commit before reading reports.")
    else:
        recover(args.state_file)

    if args.reset and os.path.exists(args.state_file):
        os.remove(args.state_file)

    state = load_state(args.state_file, args.pot)

    # A glitched exit quote once credited ~$1.7bn of phantom proceeds to cash
    # (TOSHI, 2026-08-23) and sat there unnoticed for two weeks, quietly voiding
    # the "can I afford this position" constraint. MAX_PLAUSIBLE_PRICE_MULTIPLE
    # stops new ones; this says so out loud if a state file is already corrupt.
    pot = state.get("starting_pot") or 0.0
    if pot > 0 and (state.get("cash") or 0.0) > pot * IMPLAUSIBLE_CASH_MULTIPLE:
        print(f"  [WARN] cash {state['cash']:,.2f} is more than {IMPLAUSIBLE_CASH_MULTIPLE}x the starting pot "
              f"({pot:,.2f}). Almost certainly phantom proceeds from a fabricated exit quote - the affordability "
              f"check is meaningless until this is fixed. Stop the bot and run: python repair_cash.py --dry-run")

    if args.status:
        print_summary(state)
        return

    if args.report:
        print_report(state)
        return

    print(f"Paper-trading bot — pot {state['starting_pot']:.2f}, cash {state['cash']:.2f}, "
          f"{len(state['positions'])} open position(s). Chains: {args.chains}. "
          f"Max risk score to buy: {args.max_risk_score}. Sizing: {args.sizing}.")
    print("Simulation only — no wallet, no real funds, no live orders are ever placed.")

    validate_trading_cash(state)

    def commit_operation(operation):
        with StateTransaction(args.state_file, state):
            validate_trading_cash(state)
            operation(state, args)
            validate_trading_cash(state)

    if args.once:
        # Settle existing positions before discovery can fail or block on an API.
        commit_operation(check_positions)
        commit_operation(check_pending_outcomes)
        commit_operation(run_discovery)
        commit_operation(check_positions)
        print_summary(state)
        return

    last_discovery = 0.0
    try:
        while True:
            print(f"\n--- tick @ {now_iso()} ---")
            try:
                discover = time.time() - last_discovery >= args.discovery_interval
                commit_operation(check_positions)
                commit_operation(check_pending_outcomes)
                if discover:
                    commit_operation(run_discovery)
                    last_discovery = time.time()
            except PendingCommit as exc:
                sys.exit(f"[STOP] {exc}")  # never start a new cycle over a pending journal
            except Exception as exc:
                print(f"  [ERROR] failing operation rolled back; earlier completed operations remain committed: {exc!r}")
            time.sleep(args.tick_interval)
    except KeyboardInterrupt:
        print("\nStopping; completed operations are saved. Any interrupted operation was rolled back.")
        # Do not save here: this used to persist half-finished mutations.




if __name__ == "__main__":
    main()
