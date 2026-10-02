#!/usr/bin/env python3
"""Repair the simulated cash balance after glitched-quote trades corrupted it.

On 2026-08-23 a Dexscreener quote of 79,806.68 for TOSHI (entry 0.0001324, a
6e8 multiple) closed a position at "take-profit" and credited its proceeds to
cash: about $1.72 billion on a 100-unit pot. analyze_outcomes.py has always
excluded such trades from its statistics, so the reports stayed correct - but
nothing ever corrected the state file, so the bot's cash constraint has been
meaningless ever since (it could always afford another position). paperbot's
MAX_PLAUSIBLE_PRICE_MULTIPLE guard, added later, prevents new occurrences; it
cannot undo old ones.

Rather than patch the number, this recomputes it from first principles:

    cash = starting_pot
         - sum(invested) over open positions
         + sum(pnl) over closed trades that are not data artefacts

Trades whose exit price or proceeds are impossible are VOIDED: moved to a
"voided_trades" list with the reason recorded, contributing nothing to cash.
That is deliberately not the same as booking a loss. The exit price was
fabricated, so the real outcome is unknown; voiding neither credits a fake
win nor invents a loss, and it makes the state agree with what the analysis
has been reporting all along.

Any drift from other causes is also corrected, since the balance is rebuilt
rather than adjusted. STOP THE BOT FIRST - this rewrites the state file.

Usage:
  python repair_cash.py --dry-run
  python repair_cash.py
"""

import argparse
import paperbot as bot
from state_store import recover
import json
import os
import shutil
from datetime import datetime, timezone

# A memecoin can plausibly 1000x. Nothing legitimately does 1000x with a real
# buyer on the other side of the trade, which is what an exit price claims.
# Same bound analyze_outcomes.py uses (SANE_PCT_CHANGE_BOUND = 100_000%).
IMPLAUSIBLE_MULTIPLE = 1000


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def artefact_reason(trade):
    """Why this closed trade cannot be a real outcome, or None if it looks real."""
    entry = trade.get("entry_price")
    exit_ = trade.get("exit_price")
    invested = trade.get("invested")
    proceeds = trade.get("proceeds")
    if entry and exit_ and entry > 0 and exit_ / entry > IMPLAUSIBLE_MULTIPLE:
        return f"exit {exit_:.10g} is {exit_ / entry:,.0f}x entry {entry:.10g} - fabricated quote"
    if invested and proceeds and invested > 0 and proceeds / invested > IMPLAUSIBLE_MULTIPLE:
        return f"proceeds {proceeds:,.2f} on {invested:,.2f} invested - {proceeds / invested:,.0f}x, impossible"
    return None


def rebuild(state):
    """Returns (new_cash, kept, voided) without mutating anything."""
    kept, voided = [], []
    for t in state.get("closed_trades", []):
        reason = artefact_reason(t)
        (voided if reason else kept).append((t, reason))
    invested_open = sum(p.get("invested") or 0.0 for p in state.get("positions", {}).values())
    realised = sum(t.get("pnl") or 0.0 for t, _ in kept)
    new_cash = round((state.get("starting_pot") or 0.0) - invested_open + realised, 2)
    return new_cash, [t for t, _ in kept], voided


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="paperbot_state.json")
    ap.add_argument("--reset-trading", action="store_true",
                    help="Archive positions and closed trades to a separate file and restart the TRADING simulation "
                         "from a full pot. Research data (pending_outcomes, seen_addresses) is preserved, as are the "
                         "candidate and outcome logs, which never depended on cash. Use this when the balance cannot "
                         "be reconciled because the affordability constraint was broken while cash was corrupt.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not bot.acquire_state_lock(args.state):
        raise SystemExit("State is in use by the bot or another repair. Stop that writer before repairing.")
    try:
        # Recovery precedes inspection so a repair never reads a half-committed cycle.
        if args.dry_run and bot.journal_path(args.state).exists():
            raise SystemExit("Pending cycle journal: restart the bot to recover it before a dry run.")
        if not args.dry_run:
            recover(args.state)
        return run_repair(args)
    finally:
        bot.release_state_lock()


def run_repair(args):

    with open(args.state, encoding="utf-8") as f:
        state = json.load(f)

    old_cash = state.get("cash")
    new_cash, kept, voided = rebuild(state)

    pot = state.get("starting_pot") or 0.0
    invested_open = sum(p.get("invested") or 0.0 for p in state.get("positions", {}).values())
    realised = sum(t.get("pnl") or 0.0 for t in kept)

    print(f"  Starting pot:            {pot:>18,.2f}")
    print(f"  Open positions:          {len(state.get('positions', {})):>18}  ({invested_open:,.2f} invested)")
    print(f"  Closed trades kept:      {len(kept):>18}  ({realised:+,.2f} realised)")
    print(f"  Closed trades voided:    {len(voided):>18}")
    for t, reason in voided:
        print(f"    VOID {t.get('symbol', '?'):<12} pnl {t.get('pnl', 0):>+18,.2f}  {reason}")
    print()
    print(f"  Cash recorded in state:  {old_cash:>18,.2f}")
    print(f"  Cash rebuilt from trades:{new_cash:>18,.2f}")
    drift = (old_cash or 0.0) - new_cash
    print(f"  Correction:              {-drift:>+18,.2f}")

    if args.reset_trading:
        archive_path = f"paperbot_trading_archive-{stamp()}.json"
        print(f"\n  RESET: {len(state.get('positions', {}))} open position(s) and {len(state.get('closed_trades', []))} "
              f"closed trade(s) will be archived to {archive_path} and cleared.")
        print(f"  Cash returns to the full pot ({pot:,.2f}). Preserved: {len(state.get('pending_outcomes', {}))} pending "
              f"observation(s), {len(state.get('seen_addresses', []))} seen address(es), and both JSONL logs.")
        if args.dry_run:
            print("\n  DRY RUN - nothing written. Re-run without --dry-run to apply.")
            return
        backup = f"{args.state}.backup-{stamp()}"
        shutil.copy2(args.state, backup)
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump({
                "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": "Cash was corrupted by fabricated exit quotes from 2026-08-23, so the affordability "
                          "constraint never applied and the bot opened positions a full pot could not fund. "
                          "Per-trade entries and exits are real; the portfolio-level sequence is not a valid "
                          "simulation of this pot and is retained for reference only.",
                "starting_pot": pot, "cash_recorded": old_cash, "cash_rebuilt": new_cash,
                "positions": state.get("positions", {}), "closed_trades": state.get("closed_trades", []),
                "voided_trades": [dict(t, voided_reason=reason) for t, reason in voided],
            }, f, indent=2)
        state["positions"] = {}
        state["closed_trades"] = []
        state["cash"] = pot
        state["trading_restarted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, args.state)
        print(f"\n  Applied. Cash {pot:,.2f}, no open positions, no closed trades.")
        print(f"  Archive: {archive_path}")
        print(f"  Backup:  {backup}")
        print("  Start the bot again now; this is the start of the valid evaluation period.")
        return

    if abs(drift) < 0.01:
        print("\n  Cash already reconciles. Nothing to do.")
        return

    if args.dry_run:
        print("\n  DRY RUN - nothing written. Re-run without --dry-run to apply.")
        return

    backup = f"{args.state}.backup-{stamp()}"
    shutil.copy2(args.state, backup)

    state["closed_trades"] = kept
    state.setdefault("voided_trades", []).extend(
        dict(t, voided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), voided_reason=reason)
        for t, reason in voided
    )
    state["cash"] = new_cash
    tmp = args.state + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, args.state)

    print(f"\n  Applied. Cash is now {new_cash:,.2f}. {len(voided)} trade(s) moved to voided_trades "
          f"(kept in the file for audit, excluded from all accounting).")
    print(f"  Backup: {backup}")
    print("  Start the bot again now.")


if __name__ == "__main__":
    main()
