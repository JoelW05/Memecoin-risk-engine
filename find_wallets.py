#!/usr/bin/env python3
"""Derive a candidate "elite trader" watchlist from our OWN outcome data,
instead of copying a public leaderboard.

The idea: paperbot_outcomes.jsonl already records what happened to thousands of
coins this bot's discovery feed surfaced. Some of them ran. Ask Helius who was
buying those coins in their first minutes, and rank wallets by how many
separate winners they were early to.

Why this rather than GMGN/Photon leaderboards:
  - No scraping and no terms-of-service problem; one documented API.
  - The wallets it finds are relevant to what THIS bot can actually trade,
    because the winners come from this bot's own discovery feed.
  - A published leaderboard attracts copiers, and some wallets are farmed
    specifically to be copied. A privately derived list avoids that particular leaderboard mechanism, but can still select manipulated activity.

THE CONTROL IS THE POINT. A sniper bot buys every new token, so it appears in
every winner - and in every loser too. So an equal-sized sample of coins that
did NOT run is measured the same way, and each wallet gets a lift score:

    lift = (share of winners it was early to) / (share of controls it was early to)

Lift near 1.0 means the wallet buys everything and its presence in winners is
arithmetic, not skill. Lift well above 1 is the only interesting signal here,
and even then it is a hypothesis, not a finding - feed the output to
wallet_research.py, which asks the question that actually matters: does the
wallet's performance PERSIST from one week to the next?

Known biases, stated plainly:
  - Only covers the discovery window for tokens whose history fits inside --max-pages. Very
    active winners get skipped and are reported as such, so this leans toward
    smaller winners.
  - Solana only (Helius).
  - Cannot see pre-graduation bonding-curve activity that never reached
    Dexscreener, so these are graduated-token traders.
  - Being early to winners is not skill. Snipers and insiders score highly here
    by design; that is what wallet_research.py's <5min hold column is for.

Pure standard library. Needs a Helius API key (free tier is fine):

    $env:HELIUS_API_KEY = "your-key-here"        (PowerShell, current window)
    python find_wallets.py

Writes wallets.txt for wallet_research.py. The key is never printed or stored.
"""

import argparse
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from wallet_research import HELIUS_TX_URL, PAGE_SIZE, helius_get, short, parse_swap, validate_history_page

# The observable window starts ten minutes before discovery. This is not
# proof of reaching token launch; old tokens may have much earlier history.
LAUNCH_SLACK_SECONDS = 600


def load_rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # partially written final record on a live file


def utc_ts(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


SANE_PCT_CHANGE_BOUND = 100_000  # %, i.e. 1000x - beyond this it is a bad quote


def split_winners_and_controls(rows, checkpoint_hours, win_threshold):
    """One entry per Solana token: (address, symbol, discovered_at timestamp,
    earliest eligible discovery % change at the chosen horizon). Only rows with a real
    observed price count - censored/unknown rows tell us nothing either way."""
    best = {}
    for o in rows:
        if o.get("chain") != "solana" or o.get("checkpoint_hours") != checkpoint_hours:
            continue
        status = o.get("observation_status")
        if status is not None and status != "observed":
            continue
        pct = o.get("price_change_pct")
        addr, disc = o.get("address"), o.get("discovered_at")
        if not addr or not disc or not isinstance(pct, (int, float)):
            continue
        if not math.isfinite(pct) or abs(pct) > SANE_PCT_CHANGE_BOUND:
            continue  # fabricated quote, not an outcome - see analyze_outcomes.py
        try:
            ts = utc_ts(disc)
        except (ValueError, TypeError):
            continue
        if addr not in best or ts < best[addr]["discovered_ts"]:
            best[addr] = {"address": addr, "symbol": o.get("symbol"), "discovered_ts": ts, "pct": pct}
    winners = sorted((t for t in best.values() if t["pct"] >= win_threshold),
                     key=lambda t: t["discovered_ts"], reverse=True)
    controls = sorted((t for t in best.values() if t["pct"] < win_threshold),
                      key=lambda t: t["discovered_ts"], reverse=True)
    return winners, controls


def buyer_of(tx, mint):
    """The wallet that BOUGHT this mint in this transaction, or None.

    Requires the fee payer to be the net receiver of the token: that excludes
    pool and program accounts, which move the token without initiating."""
    payer = tx.get("feePayer")
    if not payer:
        return None
    swap = parse_swap(tx, payer)
    return payer if swap and swap["side"] == "buy" and swap["mint"] == mint else None


def early_buyers(mint, discovered_ts, api_key, max_pages, window_seconds, spacing):
    """Wallets that bought this mint at or before discovery + window.

    Helius returns newest-first; coverage means paging back to ten minutes
    before discovery, not proof of launch. Returns (buyers, reached_launch, pages_used); reached_launch is
    False when the page budget ran out first, and those tokens are excluded
    from the rates rather than counted as "nobody bought early"."""
    cutoff_early = discovered_ts - LAUNCH_SLACK_SECONDS
    cutoff_late = discovered_ts + window_seconds
    buyers = set()
    before = None
    cursors = set()
    for page in range(max_pages):
        params = {"limit": PAGE_SIZE}
        if before:
            params["before"] = before
        batch = validate_history_page(helius_get(HELIUS_TX_URL.format(address=mint), params, api_key))
        if not isinstance(batch, list) or not batch:
            return buyers, True, page + 1  # no more history: we have seen it all
        for tx in batch:
            if cutoff_early <= tx["timestamp"] <= cutoff_late:
                wallet = buyer_of(tx, mint)
                if wallet:
                    buyers.add(wallet)
        oldest = batch[-1]
        before = oldest.get("signature")
        if (oldest.get("timestamp") or 0) <= cutoff_early or len(batch) < PAGE_SIZE:
            return buyers, True, page + 1
        if before in cursors:
            return buyers, False, page + 1
        cursors.add(before)
        time.sleep(spacing)
    return buyers, False, max_pages


def scan(tokens, label, api_key, args):
    """Returns {wallet: set(token addresses)} plus how many tokens we reached."""
    hits = defaultdict(set)
    reached_count = 0
    skipped = []
    failed = 0
    for i, token in enumerate(tokens, 1):
        try:
            buyers, reached, pages = early_buyers(
                token["address"], token["discovered_ts"], api_key,
                args.max_pages, args.window_minutes * 60, args.spacing)
        except RuntimeError as exc:
            failed += 1
            print(f"  [{label} {i}/{len(tokens)}] {(token['symbol'] or '?')[:12]:<12} "
                  f"{token['pct']:>+8.1f}%  NETWORK FAILURE, skipping this token: {exc}")
            time.sleep(args.spacing)
            continue
        status = f"{len(buyers):>4} early buyer(s), {pages} page(s)"
        if reached:
            reached_count += 1
            for wallet in buyers:
                hits[wallet].add(token["address"])
        else:
            skipped.append(token)
            status += "  SKIPPED - too active to cover window within page budget"
        print(f"  [{label} {i}/{len(tokens)}] {(token['symbol'] or '?')[:12]:<12} {token['pct']:>+8.1f}%  {status}")
        time.sleep(args.spacing)
    if failed:
        print(f"  ({failed} token(s) abandoned on network errors - they count as neither reached nor skipped)")
    return hits, reached_count, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outcomes", default="paperbot_outcomes.jsonl")
    ap.add_argument("--out", default="wallets.txt", help="Where to write the ranked watchlist. Default: wallets.txt")
    ap.add_argument("--checkpoint-hours", type=int, default=24, help="Which horizon defines a winner. Default: 24.")
    ap.add_argument("--win-threshold", type=float, default=50, help="%% gain at that horizon that counts as a winner. Default: 50.")
    ap.add_argument("--sample", type=int, default=25, help="Winners to scan, and an equal number of controls. Default: 25.")
    ap.add_argument("--max-pages", type=int, default=20, help="Page budget per token (100 txs each). Tokens whose discovery window is further back than this are skipped, not guessed at. Default: 20.")
    ap.add_argument("--window-minutes", type=float, default=60, help="A buy this long after discovery still counts as early. Default: 60.")
    ap.add_argument("--min-winners", type=int, default=2, help="Wallets must be early to at least this many winners to make the list. Default: 2.")
    ap.add_argument("--min-lift", type=float, default=1.5, help="Minimum winners-rate/controls-rate. 1.0 means the wallet buys everything. Default: 1.5.")
    ap.add_argument("--top", type=int, default=25, help="Maximum wallets to write. Default: 25.")
    ap.add_argument("--spacing", type=float, default=0.25, help="Seconds between Helius requests. Default: 0.25.")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if min(args.sample, args.max_pages, args.top, args.min_winners, args.checkpoint_hours) <= 0:
        ap.error("Sample, pages, top, min-winners and horizon must be positive.")
    if any(not math.isfinite(v) or v < 0 for v in (args.spacing, args.window_minutes, args.min_lift)) or not math.isfinite(args.win_threshold):
        ap.error("Invalid nonfinite or negative numeric option.")

    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        sys.exit('Set HELIUS_API_KEY first:  $env:HELIUS_API_KEY = "..."   (never paste the key into this file)')

    try:
        rows = list(load_rows(args.outcomes))
    except FileNotFoundError:
        sys.exit(f"{args.outcomes} not found - run this from the folder holding the bot's logs.")

    winners, controls = split_winners_and_controls(rows, args.checkpoint_hours, args.win_threshold)
    print(f"{len(rows)} outcome rows -> {len(winners)} Solana winner(s) (>= +{args.win_threshold:g}% at "
          f"{args.checkpoint_hours}h) and {len(controls)} control(s).")
    if not winners:
        sys.exit("No winners at that threshold. Lower --win-threshold or wait for more data.")

    rng = random.Random(args.seed)
    rng.shuffle(winners)
    rng.shuffle(controls)
    winners = winners[:args.sample]
    controls = controls[:args.sample]
    budget = (len(winners) + len(controls)) * args.max_pages
    print(f"Scanning {len(winners)} winner(s) and {len(controls)} control(s) - up to {budget} Helius requests, "
          f"roughly {budget * args.spacing / 60:.0f} minute(s).\n")

    win_hits, win_reached, win_skipped = scan(winners, "win ", api_key, args)
    print()
    ctl_hits, ctl_reached, ctl_skipped = scan(controls, "ctrl", api_key, args)

    print(f"\nCovered the discovery window for {win_reached}/{len(winners)} winner(s) and {ctl_reached}/{len(controls)} control(s).")
    if win_skipped or ctl_skipped:
        print(f"  ({len(win_skipped) + len(ctl_skipped)} token(s) skipped - too much later activity to page back to "
              f"the discovery window within --max-pages; raise it to include them, at proportional API cost)")
    if not win_reached or not ctl_reached:
        sys.exit("No complete winner/control comparison: both cohorts need observed history. Watchlist left unchanged.")

    wallets = sorted(set(win_hits) | set(ctl_hits))
    scored = []
    for w in wallets:
        w_hit, c_hit = len(win_hits.get(w, ())), len(ctl_hits.get(w, ()))
        w_rate = w_hit / win_reached
        c_rate = (c_hit / ctl_reached) if ctl_reached else 0.0
        lift = (w_rate / c_rate) if c_rate > 0 else (float("inf") if w_hit else 0.0)
        scored.append({"wallet": w, "winners": w_hit, "controls": c_hit, "lift": lift})
    scored.sort(key=lambda s: (-s["winners"], -(s["lift"] if s["lift"] != float("inf") else 1e9)))

    print(f"\n{len(scored)} distinct wallet(s) were early to at least one scanned token.")
    print(f"  {'wallet':<14} {'winners':>8} {'controls':>9} {'lift':>8}   reading")
    shown = [s for s in scored if s["winners"] >= args.min_winners][:args.top * 2]
    for s in shown[:20]:
        lift_s = "inf" if s["lift"] == float("inf") else f"{s['lift']:.1f}"
        if s["lift"] == float("inf"):
            note = "no control hits observed; infinite ratio is not certainty"
        elif s["lift"] < 1.2:
            note = "buys everything; presence in winners is arithmetic, not skill"
        elif s["lift"] < 2:
            note = "mild preference for winners"
        else:
            note = "strong preference for winners - worth testing for persistence"
        print(f"  {short(s['wallet']):<14} {s['winners']:>8} {s['controls']:>9} {lift_s:>8}   {note}")

    picked = [s for s in scored if s["winners"] >= args.min_winners and s["lift"] >= args.min_lift][:args.top]
    if not picked:
        print(f"\nNo wallet cleared --min-winners {args.min_winners} and --min-lift {args.min_lift}. That is itself a "
              f"result for these thresholds, not evidence that skill is absent. Either raise "
              f"--sample for more statistical power; no wallet-level signal has been established by this run.")
        with open(args.out + ".tmp", "w", encoding="utf-8") as f:
            f.write("# Completed scan: no wallets met the configured thresholds.\n")
        os.replace(args.out + ".tmp", args.out)
        return

    with open(args.out + ".tmp", "w", encoding="utf-8") as f:
        f.write(f"# Derived by find_wallets.py on {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
        f.write(f"# Selected at UTC: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"# From {win_reached} winner(s) (>= +{args.win_threshold:g}% at {args.checkpoint_hours}h) and "
                f"{ctl_reached} control(s) in {args.outcomes}.\n")
        f.write("# Columns in the comment after each address: winners hit, controls hit, lift.\n")
        f.write("# THESE ARE HYPOTHESES, NOT VETTED TRADERS. Being early to winners is not skill - snipers and\n")
        f.write("# insiders rank highly here by design. Run wallet_research.py against this file to find out\n")
        f.write("# whether any of them actually persist week to week before trusting a single one.\n")
        for s in picked:
            lift_s = "inf" if s["lift"] == float("inf") else f"{s['lift']:.1f}"
            f.write(f"{s['wallet']}  # winners={s['winners']} controls={s['controls']} lift={lift_s}\n")

    os.replace(args.out + ".tmp", args.out)
    print(f"\nWrote {len(picked)} wallet(s) to {args.out}.")
    print("Freeze this watchlist. wallet_research.py will evaluate only transactions after selection; allow new data to accumulate:")
    print(f"  python wallet_research.py --wallets {args.out} --days 28")


if __name__ == "__main__":
    main()
