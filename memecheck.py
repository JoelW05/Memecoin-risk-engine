#!/usr/bin/env python3
"""Paste a memecoin link or contract address, get an automated on-chain risk report.

Supports Solana and Base. Pure standard library — no pip install needed.
Scoring logic lives in analyzer.py (shared with paperbot.py).
"""

import argparse
import json
import sys
import urllib.parse

# Token symbols/names routinely contain emoji or other characters outside the
# Windows console's default cp1252 codepage — without this, printing an unlucky
# coin's report would crash instead of just showing it.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from analyzer import (
    fmt_usd, fmt_age, fmt_price, social_label, risk_label,
    resolve, goplus_security, rugcheck_report, honeypot_check, analyze,
)

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def risk_color(score):
    if score >= 46:
        return RED
    if score >= 21:
        return YELLOW
    return GREEN


def c(text, color_code, enabled):
    return f"{color_code}{text}{RESET}" if enabled else text


def print_report(chain, token_address, result, use_color=True):
    label, col = risk_label(result["score"]), risk_color(result["score"])
    title = f"{result.get('name') or '?'} ({result.get('symbol') or '?'})"

    print()
    print(c(title, BOLD + CYAN, use_color))
    print(c(f"{chain.upper()}  |  {token_address}", DIM, use_color))
    print()
    print(f"Risk score: {c(str(result['score']) + '/100', col, use_color)}   {c(label, BOLD + col, use_color)}")
    print()
    print(c("Market snapshot", BOLD, use_color))
    print(f"  Price:        {('$' + str(result['price_usd'])) if result['price_usd'] else 'n/a'}")
    print(f"  Liquidity:    {fmt_usd(result['liquidity_usd']) if result['liquidity_usd'] is not None else 'n/a'}")
    print(f"  24h volume:   {fmt_usd(result['volume_24h']) if result['volume_24h'] is not None else 'n/a'}")
    print(f"  FDV/MCap:     {fmt_usd(result['fdv']) if result['fdv'] is not None else 'n/a'}")
    pchange = result["price_change_24h"]
    if pchange is not None:
        pcol = GREEN if pchange >= 0 else RED
        print(f"  24h change:   {c(f'{pchange:+.1f}%', pcol, use_color)}")
    if result["created_ms"]:
        print(f"  Pool age:     {fmt_age(result['created_ms'])}")
    txns = result["txns_24h"]
    if txns:
        print(f"  24h txns:     {txns.get('buys', '?')} buys / {txns.get('sells', '?')} sells")
    print()

    print(c("Suggested trade parameters (heuristic)", BOLD, use_color))
    levels = result["trade_levels"]
    pos = result["position_size"]
    if levels:
        print(f"  Stop-loss:    {c(fmt_price(levels['sl_price']), RED, use_color)}  (-{levels['sl_pct']:.0f}%)")
        print(f"  Take-profit:  {c(fmt_price(levels['tp_price']), GREEN, use_color)}  (+{levels['tp_pct']:.0f}%)")
        print(f"  Reward:risk:  {levels['rr']:.1f}:1")
        print(f"  Suggested size: {pos['range']}  ({pos['note']})")
        print(c(
            "  Derived only from this pair's own recent price-swing volatility\n"
            "  (5m/1h/6h/24h change) — bands are wide on purpose since memecoins\n"
            "  swing far more than typical assets, and past volatility does not\n"
            "  predict future behavior. Size is scaled only by the risk score\n"
            "  above, as a share of a dedicated speculative allocation — not\n"
            "  your net worth or savings. Not financial advice.",
            DIM, use_color,
        ))
    else:
        print(f"  Not enough price-history data to estimate stop-loss/take-profit.")
        print(f"  Suggested size: {pos['range']}  ({pos['note']})")
    print()

    print(c("Socials & further research", BOLD, use_color))
    links = list(result["websites"]) + list(result["socials"])
    if links:
        for link in links:
            label = social_label(link.get("type") or link.get("label"))
            print(f"  {label}: {link.get('url')}")
    else:
        print("  No official links found on Dexscreener for this pair.")
    query = urllib.parse.quote_plus(f"{result.get('name') or ''} {result.get('symbol') or ''}".strip())
    print(c("  Can't pull live mention counts here (X's API is paid-only, Reddit blocks", DIM, use_color))
    print(c("  scripted requests) — check manually instead:", DIM, use_color))
    print(f"  Search X:      https://x.com/search?q={query}&f=live")
    print(f"  Search Reddit: https://www.reddit.com/search/?q={query}")
    print(f"  Search Google: https://www.google.com/search?q={query}+scam")
    print()

    groups = [
        ("critical", "Critical red flags", RED, "[X]"),
        ("high", "High-risk flags", RED, "[X]"),
        ("medium", "Warnings", YELLOW, "[!]"),
        ("good", "Positive signals", GREEN, "[OK]"),
    ]
    any_bad = False
    for severity, heading, color_code, marker in groups:
        items = [msg for sev, msg in result["flags"] if sev == severity]
        if not items:
            continue
        if severity in ("critical", "high", "medium"):
            any_bad = True
        print(c(heading, BOLD + color_code, use_color))
        for msg in items:
            print(f"  {c(marker, color_code, use_color)} {msg}")
        print()

    if not any_bad:
        print(c("No major red flags detected by automated checks.", GREEN, use_color))
        print()

    print(c(
        "Not financial advice. This is an automated heuristic check of on-chain\n"
        "signals (liquidity, mint/freeze authority, honeypot simulation, holder\n"
        "concentration) — it cannot detect every scam, and a clean report does not\n"
        "mean a token is a good investment. Memecoins are extremely high-risk;\n"
        "only ever risk money you can afford to lose entirely.",
        DIM, use_color,
    ))
    print()


def main():
    parser = argparse.ArgumentParser(description="Analyze a Solana or Base memecoin from a link or contract address.")
    parser.add_argument("input", help="Token/contract address, or a link (Dexscreener, pump.fun, Solscan, Birdeye, Basescan, etc.)")
    parser.add_argument("--json", action="store_true", help="Print raw combined JSON instead of the formatted report.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    args = parser.parse_args()

    resolved = resolve(args.input)
    if not resolved:
        print("Could not find a Solana address or Base (0x...) address/link in that input.", file=sys.stderr)
        sys.exit(1)
    chain, token_address, pairs = resolved
    if not pairs:
        print(f"Warning: no Dexscreener pairs found for {token_address} on {chain}. It may be too new, unlisted, or the address may be wrong.", file=sys.stderr)

    security = goplus_security(chain, token_address)
    rugcheck = rugcheck_report(token_address) if chain == "solana" else None
    honeypot = honeypot_check(token_address) if chain == "base" else None

    result = analyze(chain, token_address, pairs, security, rugcheck, honeypot)

    if args.json:
        print(json.dumps({
            "chain": chain,
            "token_address": token_address,
            "dexscreener_pairs": pairs,
            "goplus_security": security,
            "rugcheck": rugcheck,
            "honeypot_is": honeypot,
            "analysis": {"score": result["score"], "flags": result["flags"]},
        }, indent=2))
        return

    print_report(chain, token_address, result, use_color=not args.no_color)


if __name__ == "__main__":
    main()
