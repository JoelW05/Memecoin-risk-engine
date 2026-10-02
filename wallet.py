#!/usr/bin/env python3
"""Generates and inspects a dedicated Solana wallet for live memecoin trading.

SECURITY - read this before running anything:
  - Run this script YOURSELF, locally, on the machine that will hold the
    funds. The secret key it generates must never be typed, pasted, shown,
    or sent anywhere else - not into a chat, not to an AI assistant, not to
    any website. Whoever has the wallet file has complete, irreversible
    control of everything in it.
  - This wallet should hold ONLY the money you are prepared to lose entirely
    on this bot - never your main wallet, never savings, never anything you
    can't afford to see go to zero.
  - There is no password recovery. If the wallet file is lost, deleted, or
    corrupted with no backup, the funds in it are gone permanently.

Usage:
  python wallet.py generate           # create a new wallet (refuses to overwrite one that exists)
  python wallet.py generate --force   # overwrite an existing wallet file - only if you've backed up its
                                       #   secret elsewhere; otherwise this permanently destroys access to its funds
  python wallet.py address            # print the public deposit address (safe to share - this is what
                                       #   you'd send SOL to; the secret key file itself never should be)
  python wallet.py balance            # check the wallet's live SOL balance via a public Solana RPC

Requires: pip install solders certifi
"""

import argparse
import json
import os
import ssl
import sys
import urllib.request

try:
    from solders.keypair import Keypair
except ImportError:
    print("Missing dependency. Run: pip install solders certifi", file=sys.stderr)
    sys.exit(1)

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    print("Missing dependency. Run: pip install solders certifi", file=sys.stderr)
    sys.exit(1)

WALLET_FILE_DEFAULT = "solana_wallet.json"
RPC_URL = "https://api.mainnet-beta.solana.com"


def generate(args):
    if os.path.exists(args.wallet_file) and not args.force:
        print(f"'{args.wallet_file}' already exists. Refusing to overwrite it - doing so would")
        print("permanently destroy access to any funds in it unless you've saved the secret")
        print("key elsewhere already.")
        print(f"Pass --force if you're certain, or use a different --wallet-file name.")
        sys.exit(1)

    kp = Keypair()
    secret_bytes = list(bytes(kp))
    with open(args.wallet_file, "w", encoding="utf-8") as f:
        json.dump(secret_bytes, f)

    print("New wallet generated.")
    print()
    print("  Address (safe to share - this is your deposit address):")
    print(f"    {kp.pubkey()}")
    print()
    print(f"  Secret key saved to: {os.path.abspath(args.wallet_file)}")
    print()
    print("IMPORTANT:")
    print("  - That file IS the wallet. Anyone who reads it can spend everything in it.")
    print("  - Never paste its contents into a chat, a website, or send it to anyone - including Claude.")
    print("  - Don't sync it to cloud storage (OneDrive/Google Drive/etc) unless the backup itself is encrypted.")
    print("  - Fund this address with ONLY the amount you are prepared to lose entirely.")
    print("  - This is a brand-new, empty wallet - it holds nothing until you send SOL to the address above.")


def load_keypair(wallet_file):
    if not os.path.exists(wallet_file):
        print(f"No wallet file at '{wallet_file}'. Run 'python wallet.py generate' first.", file=sys.stderr)
        sys.exit(1)
    with open(wallet_file, "r", encoding="utf-8") as f:
        secret_bytes = json.load(f)
    return Keypair.from_bytes(bytes(secret_bytes))


def address(args):
    kp = load_keypair(args.wallet_file)
    print(kp.pubkey())


def rpc_call(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=payload, headers={"Content-Type": "application/json"})
    # Explicit certifi CA bundle: some Windows Python installs' default trust
    # store doesn't include the root this particular RPC host's cert chains
    # through, even though it works fine for every other API this project
    # calls - so don't rely on the platform default here.
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def balance(args):
    kp = load_keypair(args.wallet_file)
    pubkey = str(kp.pubkey())
    result = rpc_call("getBalance", [pubkey])
    lamports = (result.get("result") or {}).get("value")
    if lamports is None:
        print(f"Could not fetch balance: {result.get('error')}")
        sys.exit(1)
    sol = lamports / 1_000_000_000
    print(f"Address: {pubkey}")
    print(f"Balance: {sol:.9f} SOL")
    if sol == 0:
        print()
        print("This wallet is empty. Send SOL to the address above to fund it.")


def main():
    # --wallet-file lives on a shared parent parser (not the top-level one) so
    # it works whether typed before or after the subcommand, e.g. both
    # "wallet.py --wallet-file X generate" and "wallet.py generate --wallet-file X".
    wallet_file_parent = argparse.ArgumentParser(add_help=False)
    wallet_file_parent.add_argument("--wallet-file", default=WALLET_FILE_DEFAULT, help=f"Path to the wallet secret-key file. Default: {WALLET_FILE_DEFAULT}")

    parser = argparse.ArgumentParser(description="Dedicated wallet management for live memecoin trading. Run locally - never share the secret key file.", parents=[wallet_file_parent])
    sub = parser.add_subparsers(dest="command", required=True)
    p_gen = sub.add_parser("generate", help="Create a new wallet.", parents=[wallet_file_parent])
    p_gen.add_argument("--force", action="store_true", help="Overwrite an existing wallet file.")
    sub.add_parser("address", help="Print the wallet's public deposit address.", parents=[wallet_file_parent])
    sub.add_parser("balance", help="Check the wallet's live SOL balance.", parents=[wallet_file_parent])
    args = parser.parse_args()

    {"generate": generate, "address": address, "balance": balance}[args.command](args)


if __name__ == "__main__":
    main()
