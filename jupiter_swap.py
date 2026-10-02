#!/usr/bin/env python3
"""Live swap execution via Jupiter's aggregator API. Solana only.

SAFETY - read this before running anything:
  - "quote" NEVER touches the wallet's secret key and NEVER sends anything.
    It only asks Jupiter "what would this trade look like right now" and
    prints the answer.
  - "swap" without --execute is ALSO just a dry run - same as quote, no
    signing, no broadcast, wallet key not even loaded for signing.
  - "swap" only actually sends a transaction if you pass BOTH --execute AND
    --i-understand-this-sends-real-money, AND THEN type the literal word YES
    at an interactive prompt that shows you the exact amounts first. There is
    no flag combination that skips that prompt.
  - Once broadcast, a swap is irreversible. There is no cancel, no undo.

This is not currently wired into paperbot.py's automatic loop - it only runs
when you invoke it yourself, on purpose, one trade at a time.

Usage:
  python jupiter_swap.py quote --buy <token_mint> --sol 0.02
  python jupiter_swap.py quote --sell <token_mint>
  python jupiter_swap.py swap  --buy <token_mint> --sol 0.02
  python jupiter_swap.py swap  --buy <token_mint> --sol 0.02 --execute --i-understand-this-sends-real-money
  python jupiter_swap.py swap  --sell <token_mint> --execute --i-understand-this-sends-real-money

Requires: pip install solders certifi
"""

import argparse
import base64
import json
import ssl
import sys
import urllib.request

try:
    from solders.transaction import VersionedTransaction
except ImportError:
    print("Missing dependency. Run: pip install solders certifi", file=sys.stderr)
    sys.exit(1)

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    print("Missing dependency. Run: pip install solders certifi", file=sys.stderr)
    sys.exit(1)

from wallet import WALLET_FILE_DEFAULT, load_keypair, rpc_call

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
LAMPORTS_PER_SOL = 1_000_000_000


def http_post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def http_get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def get_quote(input_mint, output_mint, amount, slippage_bps):
    url = f"{JUPITER_QUOTE_URL}?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}"
    return http_get_json(url)


def get_mint_decimals(mint):
    result = rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    value = (result.get("result") or {}).get("value")
    if not value:
        raise RuntimeError(f"Could not look up mint {mint} - is the address correct?")
    return value["data"]["parsed"]["info"]["decimals"]


def get_token_balance(owner_pubkey, mint):
    """Raw base-unit balance + decimals of one SPL token in the wallet, or None if the wallet holds no account for that mint."""
    result = rpc_call("getTokenAccountsByOwner", [owner_pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
    accounts = (result.get("result") or {}).get("value") or []
    if not accounts:
        return None
    info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
    return int(info["amount"]), int(info["decimals"])


def build_swap_transaction(quote, user_pubkey):
    return http_post_json(JUPITER_SWAP_URL, {"quoteResponse": quote, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True})


def sign_transaction(swap_tx_b64, keypair):
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
    sig = keypair.sign_message(bytes(raw_tx.message))
    signed_tx = VersionedTransaction.populate(raw_tx.message, [sig])
    return base64.b64encode(bytes(signed_tx)).decode()


def broadcast_transaction(signed_tx_b64):
    result = rpc_call("sendTransaction", [signed_tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    if "error" in result:
        raise RuntimeError(f"Broadcast failed: {result['error']}")
    return result["result"]


def describe_quote(quote, input_label, output_label, input_decimals, output_decimals):
    in_amt = int(quote["inAmount"]) / (10 ** input_decimals)
    out_amt = int(quote["outAmount"]) / (10 ** output_decimals)
    min_out = int(quote["otherAmountThreshold"]) / (10 ** output_decimals)
    impact = float(quote.get("priceImpactPct") or 0) * 100
    print(f"  {in_amt:.6f} {input_label} -> ~{out_amt:.6f} {output_label}")
    print(f"  Minimum received (after {quote['slippageBps'] / 100:.1f}% slippage): {min_out:.6f} {output_label}")
    print(f"  Price impact: {impact:.2f}%")


def resolve_trade(args, user_pubkey):
    """Shared setup for both quote and swap: figures out mints/amounts/decimals
    for either a --buy (spend SOL) or --sell (spend full token balance) trade."""
    if args.buy:
        amount = int(args.sol * LAMPORTS_PER_SOL)
        if amount <= 0:
            print("--sol must be greater than 0.")
            sys.exit(1)
        output_decimals = get_mint_decimals(args.buy)
        return SOL_MINT, args.buy, amount, "SOL", args.buy, 9, output_decimals
    else:
        balance = get_token_balance(user_pubkey, args.sell)
        if not balance or balance[0] == 0:
            print(f"No balance of {args.sell} in this wallet - nothing to sell.")
            sys.exit(0)
        amount, decimals = balance
        return args.sell, SOL_MINT, amount, args.sell, "SOL", decimals, 9


def cmd_quote(args):
    kp = load_keypair(args.wallet_file)
    input_mint, output_mint, amount, input_label, output_label, in_dec, out_dec = resolve_trade(args, str(kp.pubkey()))
    quote = get_quote(input_mint, output_mint, amount, args.slippage_bps)
    print("=== Quote (dry run - nothing sent, wallet key not used for signing) ===")
    describe_quote(quote, input_label, output_label, in_dec, out_dec)


def cmd_swap(args):
    kp = load_keypair(args.wallet_file)
    user_pubkey = str(kp.pubkey())
    input_mint, output_mint, amount, input_label, output_label, in_dec, out_dec = resolve_trade(args, user_pubkey)

    quote = get_quote(input_mint, output_mint, amount, args.slippage_bps)
    print("=== Quote ===")
    describe_quote(quote, input_label, output_label, in_dec, out_dec)

    if not args.execute:
        print()
        print("Dry run only (no --execute passed) - nothing signed, nothing broadcast.")
        return

    if not args.i_understand_this_sends_real_money:
        print()
        print("Refusing to execute: --execute alone isn't enough. Also pass")
        print("--i-understand-this-sends-real-money if you actually mean it.")
        sys.exit(1)

    print()
    print("!!! THIS WILL SEND A REAL TRANSACTION ON SOLANA MAINNET - IRREVERSIBLE !!!")
    confirm = input(f"Type YES (all caps) to confirm swapping {input_label} for {output_label}: ")
    if confirm.strip() != "YES":
        print("Not confirmed - aborting. Nothing was sent.")
        return

    print("Building and signing transaction...")
    swap_resp = build_swap_transaction(quote, user_pubkey)
    signed_tx_b64 = sign_transaction(swap_resp["swapTransaction"], kp)
    print("Broadcasting...")
    sig = broadcast_transaction(signed_tx_b64)
    print(f"Sent. Signature: {sig}")
    print(f"Track it: https://solscan.io/tx/{sig}")


def add_trade_args(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--buy", metavar="MINT", help="Token mint address to buy, spending SOL.")
    group.add_argument("--sell", metavar="MINT", help="Token mint address to sell in full, receiving SOL.")
    parser.add_argument("--sol", type=float, help="SOL amount to spend (required with --buy).")
    parser.add_argument("--slippage-bps", type=int, default=100, help="Slippage tolerance in basis points (100 = 1%%). Default: 100.")
    parser.add_argument("--wallet-file", default=WALLET_FILE_DEFAULT, help=f"Path to the wallet secret-key file. Default: {WALLET_FILE_DEFAULT}")


def main():
    parser = argparse.ArgumentParser(description="Jupiter swap quoting and (gated) live execution for the memecoin bot's wallet.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_quote = sub.add_parser("quote", help="Show what a trade would look like right now. Never sends anything.")
    add_trade_args(p_quote)

    p_swap = sub.add_parser("swap", help="Show a quote, and (only with --execute + confirmation) actually send it.")
    add_trade_args(p_swap)
    p_swap.add_argument("--execute", action="store_true", help="Actually attempt the trade (still requires --i-understand-this-sends-real-money and a typed YES).")
    p_swap.add_argument("--i-understand-this-sends-real-money", action="store_true", help="Required alongside --execute to acknowledge this is real mainnet money.")

    args = parser.parse_args()
    if args.buy and args.sol is None:
        parser.error("--buy requires --sol <amount>")

    {"quote": cmd_quote, "swap": cmd_swap}[args.command](args)


if __name__ == "__main__":
    main()
