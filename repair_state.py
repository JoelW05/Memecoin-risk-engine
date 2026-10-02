#!/usr/bin/env python3
"""One-off repair for the paper bot's state after the pair-cap bug (v1.3.0,
2026-09-03 ~19:30 to whenever v1.3.1 was started).

For about 90 minutes the batched price lookup silently dropped tokens off the
end of Dexscreener's 30-pair response cap, so healthy positions were closed as
"rugged" at $0 and healthy checkpoint rows were logged as rugged_or_delisted.
The request succeeded each time, so no circuit breaker could catch it.

This script puts the data back as far as it honestly can:

  1. Closed trades with reason 'rugged' since --since: check the token on
     Dexscreener NOW. If it has real liquidity it was never rugged -> reopen
     the position (same entry price, tokens, invested; stop/take-profit
     recomputed from a fresh full_check since the originals weren't stored;
     drawdown/run-up watermarks reset to entry - those are lost). Reverse the
     cash credit and remove the trade. Tokens with no liquidity now are left
     as-is (they may have been genuine, or died since - can't tell).
  2. Outcome checkpoint rows marked rugged since --since: removed from
     paperbot_outcomes.jsonl and re-queued in pending_outcomes so the bot
     re-checks them with the fixed lookup. Non-rugged rows are untouched.

Backs up both files first. STOP THE BOT BEFORE RUNNING THIS - it rewrites the
state file the bot holds open. Run with --dry-run first to see what it would do.

Usage:
  python repair_state.py --since 2026-09-03T19:30 --dry-run
  python repair_state.py --since 2026-09-03T19:30
"""

import argparse
import paperbot as bot
from state_store import recover
import json
import shutil
import sys
from datetime import datetime, timezone

import paperbot
from analyzer import ANALYZER_VERSION, full_check
from validate_rugs import current_liquidity


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="UTC time the bad build started, e.g. 2026-09-03T19:30")
    ap.add_argument("--until", default=None, help="UTC time the fixed build started (default: now)")
    ap.add_argument("--state", default=paperbot.STATE_FILE_DEFAULT)
    ap.add_argument("--outcomes", default=paperbot.OUTCOME_LOG_FILE_DEFAULT)
    ap.add_argument("--log-file", default=paperbot.LOG_FILE_DEFAULT)
    ap.add_argument("--min-liquidity", type=float, default=500)
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

    since = args.since if len(args.since) > 10 else args.since + "T00:00"
    until = args.until or "9999"

    def in_window(ts):
        return since <= (ts or "") < until

    with open(args.state, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("pending_outcomes", {})

    # ---------------- 1. closed trades ----------------
    suspects = [t for t in state.get("closed_trades", []) if "rugged" in (t.get("reason") or "") and in_window(t.get("closed_at"))]
    print(f"{len(suspects)} 'rugged' close(s) in the window. Checking them on Dexscreener now...")
    liq = {}
    for chain in sorted({t["chain"] for t in suspects}):
        liq.update(current_liquidity(chain, [t["address"] for t in suspects if t["chain"] == chain]))

    reopened, left, failed = [], [], []
    for t in suspects:
        v = liq.get(t["address"])
        if v is None:
            failed.append(t)
            continue
        if v < args.min_liquidity:
            left.append((t, v))
            continue
        checked = full_check(t["address"])
        if not checked or not checked[2].get("trade_levels"):
            failed.append(t)
            continue
        _, _, result = checked
        levels = result["trade_levels"]
        pos = {
            "chain": t["chain"], "symbol": t["symbol"], "name": t.get("name") or t["symbol"],
            "entry_price": t["entry_price"], "tokens": t["tokens"] if "tokens" in t else round(t["invested"] / t["entry_price"], 8),
            "invested": t["invested"],
            "sl_price": levels["sl_price"], "tp_price": levels["tp_price"],
            "score": t.get("score"), "opened_at": t["opened_at"], "missing_price_streak": 0,
            "entry_confidence": t.get("entry_confidence"), "entry_score_components": t.get("entry_score_components"),
            "analyzer_version": t.get("analyzer_version", ANALYZER_VERSION), "paperbot_version": paperbot.PAPERBOT_VERSION,
            "min_price_seen": t["entry_price"], "max_price_seen": t["entry_price"],
            "repaired_at": paperbot.now_iso(),
            "repair_note": "reopened after v1.3.0 pair-cap false rug; SL/TP recomputed, watermarks reset",
        }
        reopened.append((t, pos, v))

    print(f"\n  {'symbol':<12} {'liquidity now':>14}  action")
    for t, pos, v in reopened:
        print(f"  {t['symbol']:<12} {v:>14,.0f}  REOPEN  (SL {pos['sl_price']:.6g}, TP {pos['tp_price']:.6g})")
    for t, v in left:
        print(f"  {t['symbol']:<12} {v:>14,.0f}  leave closed - no liquidity today (genuine, or died since)")
    for t in failed:
        print(f"  {t['symbol']:<12} {'?':>14}  skipped - lookup failed, re-run later")

    # ---------------- 2. outcome rows ----------------
    rows = list(paperbot_rows(args.outcomes))
    bad = [r for r in rows if r.get("rugged_or_delisted") and in_window(r.get("checked_at"))]
    keep = [r for r in rows if not (r.get("rugged_or_delisted") and in_window(r.get("checked_at")))]
    done_by_addr = {}
    for r in keep:
        done_by_addr.setdefault(r["address"], set()).add(r.get("checkpoint_hours"))
    requeue = {}
    for r in bad:
        a = r["address"]
        if a in state["pending_outcomes"] or a in requeue:
            continue
        requeue[a] = {
            "chain": r["chain"], "symbol": r.get("symbol"), "score": r.get("score_at_discovery"),
            "confidence": r.get("confidence_at_discovery"), "verdict": r.get("verdict_at_discovery"),
            "discovered_at": r["discovered_at"], "discovery_price": r.get("discovery_price"),
            "checkpoints_done": sorted(h for h in done_by_addr.get(a, set()) if h is not None),
        }
    print(f"\n{len(bad)} rugged checkpoint row(s) in the window would be removed from {args.outcomes};")
    print(f"{len(requeue)} token(s) re-queued in pending_outcomes for a fresh check.")

    if args.dry_run:
        print("\nDRY RUN - nothing written. Re-run without --dry-run to apply.")
        return

    # ---------------- apply ----------------
    shutil.copy2(args.state, f"{args.state}.backup-{stamp()}")
    shutil.copy2(args.outcomes, f"{args.outcomes}.backup-{stamp()}")

    reopened_ids = {id(t) for t, _, _ in reopened}
    state["closed_trades"] = [t for t in state["closed_trades"] if id(t) not in reopened_ids]
    for t, pos, _ in reopened:
        state["positions"][t["address"]] = pos
        state["cash"] = round(state["cash"] - (t.get("proceeds") or 0.0), 2)
        paperbot.log_trade(args.log_file, [
            paperbot.now_iso(), "REOPEN", t["chain"], t["symbol"], t["address"], f"{t['entry_price']:.10g}",
            f"{t['invested']:.2f}", f"{pos['tokens']:.4f}", "repair: v1.3.0 pair-cap false rug reversed", t.get("score"), "", f"{state['cash']:.2f}",
        ])
    state["pending_outcomes"].update(requeue)
    paperbot.save_state(args.state, state)

    with open(args.outcomes, "w", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps(r) + "\n")

    print(f"\nApplied: {len(reopened)} position(s) reopened, {len(bad)} outcome row(s) removed, "
          f"{len(requeue)} token(s) re-queued. Backups written next to both files.")
    print("Start the bot again now.")


def paperbot_rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


if __name__ == "__main__":
    main()
