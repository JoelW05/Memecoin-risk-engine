#!/usr/bin/env python3
"""Settle the 'was it really a rug?' question empirically instead of by
timestamp heuristics.

analyze_outcomes.py has two ways of guessing which "rugged_or_delisted" rows
were really Dexscreener rate-limit failures: a strict rule (any 2+ rugs in the
same second) and a refined rule (only if nothing else priced in that second).
They disagree by ~2000 rows. This script asks the market instead: a genuinely
rugged token does not come back, so if a token labelled rugged days ago has
real liquidity RIGHT NOW, that label was false, full stop.

It samples addresses from three rugged groups plus a control, and checks each
on Dexscreener today:

  pure_cluster    2+ rugs in one second, nothing priced      (both rules exclude)
  mixed_second    2+ rugs in one second, something priced    (refined rule KEEPS)
  lone_rug        the only rug in its second                 (neither rule touches)
  control_priced  rows that priced FINE at their checkpoint  (the baseline)

Memecoins die fast, so "alive today" only means something relative to the
control: if 30% of correctly-priced tokens survive to today and 22% of a
rugged group does, that group was almost entirely false positives. The
"implied false-positive" column is alive(group) / alive(control), capped at
100%. It is an estimate, not a measurement - but it's an estimate anchored
on what actually happened to the tokens, not on timestamps.

Pure standard library. ~20 Dexscreener requests at default sample size.
Retries failed requests - the paper bot may be sharing this machine's rate
limit while this runs.

Usage: python validate_rugs.py [--outcomes paperbot_outcomes.jsonl] [--sample 120] [--min-liquidity 500]
"""

import argparse
import json
import random
import time
from collections import defaultdict

from analyzer import fetch_json

BATCH = 10
PAIR_CAP = 30  # max PAIRS per multi-address response, not tokens
RETRIES = 4


def load_rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def classify(rows):
    """Mirror analyze_outcomes' grouping exactly, then split rugged rows into
    the three groups described in the module docstring, plus the control."""
    by_ts = defaultdict(lambda: {"rugged": [], "priced": 0})
    control = []
    for o in rows:
        slot = by_ts[o.get("checked_at")]
        if o.get("rugged_or_delisted"):
            slot["rugged"].append(o)
        else:
            slot["priced"] += 1
            control.append(o)
    groups = {"pure_cluster": [], "mixed_second": [], "lone_rug": [], "control_priced": control}
    for slot in by_ts.values():
        n = len(slot["rugged"])
        if n == 0:
            continue
        if n == 1:
            groups["lone_rug"].extend(slot["rugged"])
        elif slot["priced"] == 0:
            groups["pure_cluster"].extend(slot["rugged"])
        else:
            groups["mixed_second"].extend(slot["rugged"])
    return groups


def _liquidity_from_pairs(chain, pairs, wanted):
    by_lower = {a.lower(): a for a in wanted}
    liq = {}
    for p in pairs:
        if p.get("chainId") != chain:
            continue
        key = by_lower.get(((p.get("baseToken") or {}).get("address") or "").lower())
        if key is None:
            continue
        liq[key] = max(liq.get(key, 0.0), (p.get("liquidity") or {}).get("usd") or 0.0)
    return liq


def _fetch_with_retries(url):
    for attempt in range(RETRIES):
        data = fetch_json(url)
        if isinstance(data, dict):
            return data
        time.sleep(2.0 * (attempt + 1))
    return None


def current_liquidity(chain, addresses):
    """address -> max liquidity USD across its pairs on this chain today,
    0.0 if Dexscreener definitely has no pairs, None if the request failed
    even after retries (never treat that as 'dead').

    Dexscreener's multi-address endpoint returns at most 30 PAIRS total, so a
    token can silently fall off the end of a batch and look dead. Small
    batches, and any token missing from a capped response is re-asked alone."""
    out = {}
    addrs = list(dict.fromkeys(addresses))
    needs_individual = []
    for i in range(0, len(addrs), BATCH):
        chunk = addrs[i:i + BATCH]
        if i:
            time.sleep(0.5)
        data = _fetch_with_retries("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk))
        if data is None:
            for a in chunk:
                out[a] = None
            continue
        pairs = data.get("pairs") or []
        liq = _liquidity_from_pairs(chain, pairs, chunk)
        truncated = len(pairs) >= PAIR_CAP
        for a in chunk:
            if a in liq:
                out[a] = liq[a]
            elif truncated:
                needs_individual.append(a)
            else:
                out[a] = 0.0
    for a in needs_individual:
        time.sleep(0.5)
        data = _fetch_with_retries("https://api.dexscreener.com/latest/dex/tokens/" + a)
        if data is None:
            out[a] = None
            continue
        out[a] = _liquidity_from_pairs(chain, data.get("pairs") or [], [a]).get(a, 0.0)
    return out

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outcomes", default="paperbot_outcomes.jsonl")
    ap.add_argument("--sample", type=int, default=120, help="Unique addresses to check per group. Default 120.")
    ap.add_argument("--min-liquidity", type=float, default=500, help="USD liquidity today that counts as 'alive'. Default 500.")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rows = list(load_rows(args.outcomes))
    groups = classify(rows)
    rng = random.Random(args.seed)

    print(f"{len(rows)} outcome rows; rows by group: " + ", ".join(f"{g}={len(v)}" for g, v in groups.items()))
    print(f"Checking up to {args.sample} unique addresses per group on Dexscreener now "
          f"(alive = any pair with >= ${args.min_liquidity:,.0f} liquidity)...\n")

    stats = {}
    examples = {}
    for name, group_rows in groups.items():
        # one entry per (chain, address); sample addresses, not rows, so a
        # token checked at 1h/6h/24h doesn't get counted three times
        uniq = {}
        for o in group_rows:
            uniq[(o.get("chain"), o.get("address"))] = o.get("symbol")
        keys = list(uniq)
        rng.shuffle(keys)
        keys = keys[:args.sample]
        checked = alive = failed = 0
        ex = []
        for chain in sorted({c for c, _ in keys}):
            addrs = [a for c, a in keys if c == chain]
            liq = current_liquidity(chain, addrs)
            for a in addrs:
                v = liq.get(a)
                if v is None:
                    failed += 1
                    continue
                checked += 1
                if v >= args.min_liquidity:
                    alive += 1
                    if len(ex) < 5:
                        ex.append(f"{uniq[(chain, a)] or '?'} (${v:,.0f})")
        stats[name] = (checked, alive, failed)
        examples[name] = ex

    c_checked, c_alive, _ = stats["control_priced"]
    control_rate = c_alive / c_checked if c_checked else None

    print(f"  {'group':<15} {'checked':>8} {'alive':>6} {'% alive':>8} {'implied false-pos':>18}   reading")
    readings = {
        "control_priced": "baseline survival of tokens that were genuinely alive at checkpoint",
        "pure_cluster": "both filters exclude these - expect near 100% false-positive",
        "mixed_second": "refined filter KEEPS these - high => strict was right, low => refined was right",
        "lone_rug": "no timestamp rule can touch these - this is the irreducible label error",
    }
    for name in ("control_priced", "pure_cluster", "mixed_second", "lone_rug"):
        checked, alive, failed = stats[name]
        pct = alive / checked * 100 if checked else None
        pct_s = f"{pct:.0f}%" if pct is not None else "n/a"
        if name == "control_priced" or pct is None or not control_rate:
            fp_s = "-"
        else:
            fp_s = f"~{min(100.0, (alive / checked) / control_rate * 100):.0f}%"
        fail_note = f"  ({failed} lookups failed)" if failed else ""
        print(f"  {name:<15} {checked:>8} {alive:>6} {pct_s:>8} {fp_s:>18}   {readings[name]}{fail_note}")

    print()
    for name, ex in examples.items():
        if ex and name != "control_priced":
            print(f"  {name} still trading today: {', '.join(ex)}")
    print("\n  Implied false-positive = alive(group) / alive(control). Alive today proves the label")
    print("  was wrong; dead today is ambiguous (may have died later), so this is an estimate")
    print("  anchored on real token fates, not a measurement. Tokens are sampled per address,")
    print("  and the control spans the same date range as the rugged rows.")


if __name__ == "__main__":
    main()
