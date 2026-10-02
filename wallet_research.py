#!/usr/bin/env python3
"""Step 1 research for a copy-trading strategy: BEFORE building a wallet-copying
bot, measure whether "top trader" wallets on Solana/pump.fun actually keep
performing, or whether last week's leaderboard is luck and insiders.

Pulls each wallet's swap history from Helius (Enhanced Transactions API),
reconstructs buys/sells per token, computes FIFO realized P&L in SOL, and
then asks the make-or-break question across wallets:

    does a wallet that was top-half in week N stay top-half in week N+1?

If the answer is "no better than a coin flip", copy-trading those wallets has
no edge to copy, and no amount of bot engineering fixes that.

Pure standard library. Needs a Helius API key (free tier is fine):

    $env:HELIUS_API_KEY = "your-key-here"        (PowerShell, current window)
    python wallet_research.py --wallets wallets.txt

wallets.txt: one Solana wallet address per line (# comments allowed). Get
candidates by reading GMGN / Photon / BullX leaderboards yourself and copying
the addresses - there is no clean public API for those, and scraping them is
a terms-of-service problem. 8+ wallets makes the persistence stats meaningful;
fewer than 5 and the cross-wallet numbers are basically noise.

The key is never printed or written anywhere by this script.
"""

import argparse
import json
import math
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
USER_AGENT = "Mozilla/5.0 (memecheck-cli)"  # see helius_get: the default urllib UA is 403'd

# Some Windows Python installs ship a trust store that is missing intermediates
# for particular hosts - this exact machine could reach Dexscreener fine while
# failing SSL against Solana's RPC (see wallet.py, August). certifi's bundle
# fixes it. It is optional here: without it the standard trust store is used,
# so the stdlib-only path still works where the system store is complete.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    _USING_CERTIFI = True
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()
    _USING_CERTIFI = False
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
PAGE_SIZE = 100
# Free tier is roughly 10 req/s; stay well under it so a long wallet list
# doesn't get us throttled halfway through.
REQUEST_SPACING_SECONDS = 0.25


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def helius_get(url, params, api_key, retries=6):
    params = dict(params, **{"api-key": api_key})
    full = f"{url}?{urllib.parse.urlencode(params)}"
    # A bare urlopen() sends "Python-urllib/3.x", which Helius's edge rejects
    # outright with 403 - the same key and URL succeed from PowerShell or a
    # browser. Found the hard way; analyzer.py never hit it because it has
    # always set a User-Agent. Without this header every request fails.
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_reason = "no attempt completed"  # every path can be exhausted, including all-429
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("Helius rejected the API key (401). Check HELIUS_API_KEY.")
            if e.code == 403:
                sys.exit("Helius returned 403 (forbidden). The key authenticated but this endpoint was refused - "
                         "usually a blocked User-Agent or a plan that excludes the Enhanced Transactions API. "
                         "Check the same URL from PowerShell with Invoke-WebRequest to tell the two apart.")
            if e.code == 429 or e.code >= 500:
                last_reason = f"HTTP {e.code}"
                wait = 2 ** attempt
                print(f"    (Helius {e.code}, backing off {wait}s)", file=sys.stderr)
                time.sleep(wait)
                continue
            # Never include the URL in the error - it carries the key.
            raise RuntimeError(f"Helius HTTP {e.code} for address request") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
            # Print the actual reason, not just the class name. "URLError" alone
            # hides whether this is DNS, a refused connection or an SSL trust
            # failure, which are three different fixes.
            reason = getattr(e, "reason", e)
            last_reason = f"{type(e).__name__}: {reason}".replace(full, "[request]").replace(api_key, "[redacted]").replace(urllib.parse.quote(api_key, safe=""), "[redacted]")
            wait = min(2 ** attempt, 20)  # cap the backoff; six tries is ~50s, not minutes
            print(f"    (network error - {last_reason}; retrying in {wait}s)", file=sys.stderr)
            if isinstance(reason, ssl.SSLError) and not _USING_CERTIFI:
                print("    (SSL trust failure and certifi is not installed - try: pip install certifi)", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Helius request failed after {retries} attempts. Last error: {last_reason}")


def validate_history_page(batch):
    if not isinstance(batch, list) or any(
        not isinstance(t, dict) or not isinstance(t.get("signature"), str) or not t["signature"]
        or type(t.get("timestamp")) not in (int, float) or not math.isfinite(t["timestamp"])
        for t in batch
    ):
        raise RuntimeError("Malformed Helius history page; coverage is unknown.")
    if any(a["timestamp"] < b["timestamp"] for a, b in zip(batch, batch[1:])):
        raise RuntimeError("Helius history page is not newest-first.")
    return batch


def fetch_wallet_transactions(wallet, api_key, days, max_pages, since_ts=None):
    """Require coverage to cutoff/exhaustion; a page budget is not full history."""
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    if since_ts is not None:
        cutoff_ts = max(cutoff_ts, since_ts)
    txs, seen, cursors = [], set(), set()
    before = None
    for page in range(max_pages):
        params = {"limit": PAGE_SIZE}
        if before:
            params["before"] = before
        batch = validate_history_page(helius_get(HELIUS_TX_URL.format(address=wallet), params, api_key))
        for tx in batch:
            if tx["signature"] not in seen:
                seen.add(tx["signature"])
                txs.append(tx)
        if not batch or batch[-1]["timestamp"] < cutoff_ts or len(batch) < PAGE_SIZE:
            return [t for t in txs if t["timestamp"] >= cutoff_ts]
        before = batch[-1]["signature"]
        if before in cursors:
            raise RuntimeError("Helius pagination repeated a cursor; history is incomplete.")
        cursors.add(before)
        time.sleep(REQUEST_SPACING_SECONDS)
    raise RuntimeError("Page budget exhausted before the history cutoff; increase --max-pages.")


def read_wallets(path):
    with open(path, encoding="utf-8-sig") as stream:
        return list(dict.fromkeys(address for line in stream
                                 if (address := line.split("#", 1)[0].strip())))


def parse_utc(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def selection_time(path):
    with open(path, encoding="utf-8-sig") as stream:
        values = [parse_utc(line.split(":", 1)[1].strip()) for line in stream
                  if line.startswith("# Selected at UTC:")]
    if len(set(values)) > 1:
        raise ValueError("Conflicting selection timestamps in watchlist.")
    return values[0] if values else None


def evaluation_start(path, override=None):
    selected = selection_time(path)
    start = parse_utc(override) if override else selected
    if start is not None and selected is not None and start < selected:
        raise ValueError("Evaluation cannot begin before this watchlist was selected.")
    return start


def cache_valid(entry, days, max_pages, max_age_hours, now=None):
    now = time.time() if now is None else now
    return (isinstance(entry, dict) and entry.get("schema") == 1
            and entry.get("days") == days and entry.get("max_pages") == max_pages
            and type(entry.get("fetched_at")) in (int, float)
            and 0 <= now - entry["fetched_at"] < max_age_hours * 3600
            and isinstance(entry.get("transactions"), list))


# ---------------------------------------------------------------------------
# Parsing: derive "this wallet swapped X SOL for Y tokens" from raw transfers.
# Deliberately NOT relying on Helius' events.swap summary - pump.fun bonding
# curve buys are sometimes typed UNKNOWN with no swap event, but the token and
# native transfers are always there. Transfers relative to the wallet are the
# ground truth.
# ---------------------------------------------------------------------------

def parse_swap(tx, wallet):
    """Conservative SOL/token transfer heuristic, not a complete swap decoder."""
    try:
        return _parse_swap(tx, wallet)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None  # malformed transfer data must never become research P&L


def _parse_swap(tx, wallet):
    if tx.get("transactionError") is not None:
        return None
    sol_delta = 0.0
    for nt in tx.get("nativeTransfers") or []:
        amt = (nt.get("amount") or 0) / LAMPORTS
        if nt.get("toUserAccount") == wallet:
            sol_delta += amt
        if nt.get("fromUserAccount") == wallet:
            sol_delta -= amt

    token_deltas = defaultdict(float)
    for tt in tx.get("tokenTransfers") or []:
        mint = tt.get("mint")
        amt = tt.get("tokenAmount") or 0
        if mint == WSOL_MINT:
            # Wrapped SOL moving is still SOL for our purposes (Jupiter routes wrap it).
            if tt.get("toUserAccount") == wallet:
                sol_delta += amt
            if tt.get("fromUserAccount") == wallet:
                sol_delta -= amt
            continue
        if tt.get("toUserAccount") == wallet:
            token_deltas[mint] += amt
        if tt.get("fromUserAccount") == wallet:
            token_deltas[mint] -= amt

    if not math.isfinite(sol_delta) or any(not math.isfinite(d) for d in token_deltas.values()):
        return None
    moved = {m: d for m, d in token_deltas.items() if m and abs(d) > 0}
    if len(moved) != 1:
        return None  # not a simple SOL<->token swap (transfer, token-to-token, airdrop...)
    (mint, tdelta), = moved.items()
    if tdelta > 0 and sol_delta < 0:
        side = "buy"
    elif tdelta < 0 and sol_delta > 0:
        side = "sell"
    else:
        return None
    fee = (tx.get("fee") or 0) / LAMPORTS if tx.get("feePayer") == wallet else 0
    if not math.isfinite(fee) or fee < 0:
        return None
    sol_amount = abs(sol_delta) + fee if side == "buy" else max(0.0, sol_delta - fee)
    return {
        "side": side, "mint": mint, "tokens": abs(tdelta), "sol": sol_amount,
        "ts": tx.get("timestamp") or 0, "source": tx.get("source"), "sig": tx.get("signature"),
    }


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------

def fifo_pnl(swaps):
    """Per-token FIFO realized P&L in SOL. Returns realized events and what's
    still held. Sells of tokens we never saw bought (acquired before the
    window, or airdropped) are counted separately, not as profit."""
    lots = defaultdict(deque)   # mint -> deque of [tokens_remaining, cost_per_token, buy_ts]
    realized = []               # {mint, ts, pnl_sol, proceeds, cost, hold_seconds}
    unknown_cost_sells = 0
    for s in sorted(swaps, key=lambda x: x["ts"]):
        if s["side"] == "buy":
            lots[s["mint"]].append([s["tokens"], s["sol"] / s["tokens"], s["ts"]])
            continue
        remaining = s["tokens"]
        cost = 0.0
        matched = 0.0
        first_buy_ts = None
        q = lots[s["mint"]]
        while remaining > 1e-12 and q:
            lot = q[0]
            take = min(lot[0], remaining)
            cost += take * lot[1]
            matched += take
            if first_buy_ts is None:
                first_buy_ts = lot[2]
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                q.popleft()
        if matched <= 0:
            unknown_cost_sells += 1
            continue
        proceeds = s["sol"] * (matched / s["tokens"])  # only the matched share counts
        realized.append({
            "mint": s["mint"], "ts": s["ts"], "pnl_sol": proceeds - cost,
            "proceeds": proceeds, "cost": cost,
            "hold_seconds": s["ts"] - first_buy_ts if first_buy_ts is not None else None,
        })
    open_positions = {m: sum(l[0] * l[1] for l in q) for m, q in lots.items() if q}
    return realized, open_positions, unknown_cost_sells


def week_key(ts):
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ---------------------------------------------------------------------------
# Cross-wallet persistence
# ---------------------------------------------------------------------------

def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    if len(xs) != len(ys):
        raise ValueError("Rank vectors must have equal lengths.")
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def persistence_report(weekly_by_wallet, available_since=None, available_until=None):
    def monday(week):
        return datetime.strptime(week + "-1", "%G-W%V-%u").replace(tzinfo=timezone.utc)
    now = min(datetime.now(timezone.utc), available_until) if available_until else datetime.now(timezone.utc)
    weeks = sorted(w for w in {w for d in weekly_by_wallet.values() for w in d}
                   if monday(w) + timedelta(days=7) <= now
                   and (available_since is None or monday(w) >= available_since))
    print("  Complete calendar weeks only; wallets need realized events in both weeks.")
    if len(weeks) < 2:
        print("  Fewer than 2 weeks of data - can't measure persistence yet.")
        return
    print(f"  {'week N -> N+1':<20} {'wallets':>8} {'rank corr':>10} {'top-half stayed':>16}")
    corrs, retentions, baselines = [], [], []
    for a, b in zip(weeks, weeks[1:]):
        if (monday(b) - monday(a)).days != 7:
            continue
        wallets = [w for w, d in weekly_by_wallet.items() if a in d and b in d]
        label = f"{a} -> {b}"
        if len(wallets) < 3:
            print(f"  {label:<20} {len(wallets):>8}   (too few wallets active in both weeks)")
            continue
        xs = [weekly_by_wallet[w][a] for w in wallets]
        ys = [weekly_by_wallet[w][b] for w in wallets]
        rho = spearman(xs, ys)
        med_a, med_b = statistics.median(xs), statistics.median(ys)
        top_a = {w for w, x in zip(wallets, xs) if x > med_a}
        top_b = {w for w, y in zip(wallets, ys) if y > med_b}
        retention = len(top_a & top_b) / len(top_a) * 100 if top_a else None
        corrs.append(rho)
        retentions.append(retention)
        if retention is not None:
            baselines.append(len(top_b) / len(wallets) * 100)
        rho_s = f"{rho:+.2f}" if rho is not None else "n/a"
        ret_s = f"{retention:.0f}%" if retention is not None else "n/a"
        print(f"  {label:<20} {len(wallets):>8} {rho_s:>10} {ret_s:>16}")
    valid_c = [c for c in corrs if c is not None]
    valid_r = [r for r in retentions if r is not None]
    if valid_c:
        print(f"\n  Average rank correlation week-to-week: {statistics.mean(valid_c):+.2f}")
    if valid_r:
        print(f"  Average top-half retention:            {statistics.mean(valid_r):.0f}%  (comparison-specific chance baseline below)")
    if baselines:
        print(f"  Mean chance overlap for observed top-group sizes: {statistics.mean(baselines):.1f}%")
    print("  Strict above-median groups may be smaller than half with ties or odd counts.")
    print("  These are descriptive, selected-wallet statistics, not proof of a copyable edge.")
    print("  Evaluate a frozen watchlist on later data, including copier fees and latency.")



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def short(addr):
    return f"{addr[:4]}..{addr[-4:]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallets", default="wallets.txt", help="File with one wallet address per line.")
    ap.add_argument("--days", type=int, default=28, help="How far back to pull. Default 28.")
    ap.add_argument("--max-pages", type=int, default=15, help="Max 100-tx pages per wallet (API budget guard). Default 15 = 1500 txs.")
    ap.add_argument("--cache", default="wallet_tx_cache.json", help="Raw tx cache so re-runs don't re-spend API credits.")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--evaluation-start", help="UTC start of prospective evaluation; defaults to the watchlist selection timestamp, when present.")
    ap.add_argument("--cache-max-age-hours", type=float, default=6, help="Refresh cache after this many hours; default 6.")
    args = ap.parse_args()
    if args.days <= 0 or args.max_pages <= 0 or not math.isfinite(args.cache_max_age_hours) or args.cache_max_age_hours < 0:
        ap.error("days/max-pages must be positive; cache age must be finite and nonnegative.")

    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        sys.exit('Set HELIUS_API_KEY first:  $env:HELIUS_API_KEY = "..."   (never paste the key into this file)')

    try:
        wallets = read_wallets(args.wallets)
        start = evaluation_start(args.wallets, args.evaluation_start)
    except FileNotFoundError:
        sys.exit(f"{args.wallets} not found. Create it with one wallet address per line.")
    except ValueError as exc:
        ap.error(str(exc))
    if not wallets:
        sys.exit("No wallets in the list.")
    if len(wallets) < 5:
        print(f"NOTE: only {len(wallets)} wallet(s) - per-wallet stats are fine, cross-wallet persistence will be noise.\n")

    if start:
        print(f"Prospective evaluation starts {start.isoformat()}; earlier transactions are excluded.")
        if start >= datetime.now(timezone.utc):
            print("No prospective period is available yet. Re-run after new observations accumulate.")
            return
    else:
        print("No selection timestamp: results are exploratory, not a held-out persistence test.")
    since_ts = start.timestamp() if start else None
    cache = {}
    if not args.no_cache and os.path.exists(args.cache):
        with open(args.cache, encoding="utf-8") as f:
            cache = json.load(f)

    if not isinstance(cache, dict):
        cache = {}
    weekly_by_wallet = {}
    coverage_ends = []
    print(f"Pulling up to {args.days} days of history for {len(wallets)} wallet(s)...\n")
    print(f"  {'wallet':<12} {'swaps':>6} {'tokens':>7} {'closed':>7} {'win%':>6} {'realized SOL':>13} {'med hold':>9} {'<5min%':>7} {'open SOL':>9} {'unk sells':>9}")
    for w in wallets:
        if not args.no_cache and cache_valid(cache.get(w), args.days, args.max_pages, args.cache_max_age_hours) and cache[w].get("evaluation_start") == since_ts:
            txs = cache[w]["transactions"]
        else:
            try:
                txs = fetch_wallet_transactions(w, api_key, args.days, args.max_pages, since_ts=since_ts)
            except RuntimeError as exc:
                print(f"  [SKIP] {short(w)}: {exc}")
                continue
            cache[w] = {"schema": 1, "days": args.days, "max_pages": args.max_pages,
                        "fetched_at": time.time(), "transactions": txs, "evaluation_start": since_ts}
            if not args.no_cache:
                tmp = args.cache + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
                os.replace(tmp, args.cache)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()
        if since_ts is not None:
            cutoff = max(cutoff, since_ts)
        coverage_ends.append(datetime.fromtimestamp(cache[w]["fetched_at"], timezone.utc))
        txs = [t for t in txs if t.get("timestamp", 0) >= cutoff]
        swaps = [s for s in (parse_swap(t, w) for t in txs) if s]
        if txs and not swaps:
            # Fetched history but recognised none of it as swaps - most likely
            # the API shape differs from what parse_swap expects. Dump a few
            # raw rows so it can be diagnosed without re-spending API credits.
            with open("wallet_debug_sample.json", "w", encoding="utf-8") as f:
                json.dump(txs[:3], f, indent=2)
            print(f"  [WARN] {short(w)}: fetched {len(txs)} txs but parsed 0 swaps - wrote 3 raw rows to wallet_debug_sample.json")
        realized, open_pos, unknown = fifo_pnl(swaps)
        per_token = defaultdict(float)
        for r in realized:
            per_token[r["mint"]] += r["pnl_sol"]
        closed_tokens = len(per_token)
        wins = sum(1 for v in per_token.values() if v > 0)
        total = sum(r["pnl_sol"] for r in realized)
        holds = [r["hold_seconds"] for r in realized if r["hold_seconds"] is not None]
        med_hold = statistics.median(holds) if holds else None
        fast = sum(1 for h in holds if h < 300) / len(holds) * 100 if holds else None
        weekly = defaultdict(float)
        for r in realized:
            weekly[week_key(r["ts"])] += r["pnl_sol"]
        weekly_by_wallet[w] = dict(weekly)
        if med_hold is None:
            med_hold_s = "n/a"
        elif med_hold < 3600:
            med_hold_s = f"{med_hold/60:.0f}m"
        else:
            med_hold_s = f"{med_hold/3600:.1f}h"
        win_s = f"{wins/closed_tokens*100:.0f}%" if closed_tokens else "n/a"
        fast_s = f"{fast:.0f}%" if fast is not None else "n/a"
        distinct = len({s["mint"] for s in swaps})
        print(f"  {short(w):<12} {len(swaps):>6} {distinct:>7} {closed_tokens:>7} {win_s:>6} {total:>+13.3f} {med_hold_s:>9} {fast_s:>7} {sum(open_pos.values()):>9.3f} {unknown:>9}")

    print("\n  Columns: closed = tokens fully or partly sold; win% = share of closed tokens with realized")
    print("  profit; <5min% = share of sells within 5 minutes of the first buy (very high = sniper/insider")
    print("  pattern - by the time you see their buy the move is over); open SOL = cost basis still held;")
    print("  unk sells = sells of tokens never seen bought in the window (pre-window buys or airdrops).")

    print("\n=== Does performance persist week to week? ===")
    beginning = datetime.now(timezone.utc) - timedelta(days=args.days)
    if start:
        beginning = max(beginning, start)
    persistence_report(weekly_by_wallet, beginning, min(coverage_ends) if coverage_ends else None)

    print("\n=== Not measured here (needs token-level history, expensive on the free tier) ===")
    print("  The copier's real entry price: what the price was 2-5 seconds AFTER each wallet's buy,")
    print("  vs their fill. That's the other half of the edge question and the natural step 2.")
    print()


if __name__ == "__main__":
    main()
