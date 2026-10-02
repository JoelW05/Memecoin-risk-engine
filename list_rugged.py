#!/usr/bin/env python3
"""Prints the chain/address/symbol/closed-time of every trade currently
classified as 'rugged/delisted', so they can be spot-checked on a block
explorer against reality. One-off diagnostic, not part of the main pipeline.

Usage: python list_rugged.py [--state paperbot_state.json] [--limit 15]
"""
import argparse
import json

ap = argparse.ArgumentParser()
ap.add_argument("--state", default="paperbot_state.json")
ap.add_argument("--limit", type=int, default=15)
args = ap.parse_args()

with open(args.state, encoding="utf-8") as f:
    state = json.load(f)

rugged = [t for t in state.get("closed_trades", []) if "rugged" in (t.get("reason") or "")]
print(f"{len(rugged)} trades classified as rugged/delisted (showing up to {args.limit}):\n")
for t in rugged[:args.limit]:
    print(f"{t['chain']:<7} {t['symbol']:<14} {t['address']}  closed_at={t['closed_at']}")
