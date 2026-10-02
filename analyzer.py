#!/usr/bin/env python3
"""Shared scoring engine used by memecheck.py (CLI) and paperbot.py (paper-trading bot).

Supports Solana and Base. Pure standard library — no pip install needed.

Data sources (all free, no API key required):
  - Dexscreener     -> liquidity, volume, price, pool age, socials
  - GoPlus Security -> mint/freeze authority, honeypot/tax flags, holder concentration
  - RugCheck        -> Solana-specific risk report (best-effort, skipped on failure)
  - Honeypot.is     -> Base/EVM sell-simulation honeypot check
"""

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

USER_AGENT = "Mozilla/5.0 (memecheck-cli)"

# Bump this whenever a scoring rule, weight, or threshold below changes
# meaningfully. Every candidate/trade logged downstream (paperbot.py) stamps
# this value, so runs before and after a rule change are never silently
# pooled together as if they were one continuous experiment.
ANALYZER_VERSION = "1.2.0"

# Central, named thresholds - the single source of truth for both the scoring
# rules below and paperbot.py's entry filters. Kept as plain data (not
# scattered through code) so a future experiment can swap this dict out
# wholesale and log which version of it produced which result.
SCORING_CONFIG = {
    "liquidity_critical_usd": 3000,
    "liquidity_high_usd": 15000,
    "liquidity_medium_usd": 50000,
    "pool_age_high_hours": 6,
    "pool_age_medium_hours": 48,
    "volume_to_liquidity_min_ratio": 0.05,
    "holder_concentration_critical_pct": 70,
    "holder_concentration_high_pct": 40,
    "tax_critical_pct": 20,
    "tax_medium_pct": 8,
    "min_holder_count": 50,
}

EVM_ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
# Intentionally permissive (not strict base58): Dexscreener lowercases addresses in its
# URL slugs, which can turn a valid uppercase char (e.g. 'L') into one base58 excludes
# (lowercase 'l'). Dexscreener's own API matches case-insensitively, so we just need a
# plausible-length alphanumeric candidate here, not a strictly valid base58 string.
SOL_ADDR_RE = re.compile(r"[A-Za-z0-9]{32,44}")


# Shared request budget. Every free API here rate-limits by IP, and the bot,
# the analysis scripts and the dashboard all share one IP. Minimum spacing
# per host keeps a burst of calls under each provider's published or observed
# limit; a 429 or 5xx is retried with backoff instead of being swallowed as
# "no data". Final failure still returns None so callers' contracts hold.
HOST_MIN_INTERVAL_SECONDS = {
    "api.dexscreener.com": 0.21,      # ~300/min documented
    "api.geckoterminal.com": 2.1,     # 30/min documented
    "api.gopluslabs.io": 0.5,
    "api.rugcheck.xyz": 0.5,
    "api.honeypot.is": 0.5,
}
DEFAULT_MIN_INTERVAL_SECONDS = 0.25
FETCH_RETRIES = 2
_LAST_CALL_AT = {}


def _pace(host):
    interval = HOST_MIN_INTERVAL_SECONDS.get(host, DEFAULT_MIN_INTERVAL_SECONDS)
    wait = _LAST_CALL_AT.get(host, 0.0) + interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL_AT[host] = time.monotonic()


def fetch_json(url, timeout=12):
    host = urllib.parse.urlsplit(url).hostname or ""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(FETCH_RETRIES + 1):
        _pace(host)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                if attempt < FETCH_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            return None
        except Exception:
            if attempt < FETCH_RETRIES:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None


def extract_candidate_address(text):
    text = text.strip()
    evm = EVM_ADDR_RE.search(text)
    if evm:
        return evm.group(0), "base"
    candidates = SOL_ADDR_RE.findall(text)
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0], "solana"
    return None, None


def dexscreener_tokens(chain_id, address):
    data = fetch_json(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    pairs = (data or {}).get("pairs") or []
    return [p for p in pairs if p.get("chainId") == chain_id]


def dexscreener_pair(chain_id, pair_address):
    data = fetch_json(f"https://api.dexscreener.com/latest/dex/pairs/{chain_id}/{pair_address}")
    return (data or {}).get("pairs") or []


def resolve(input_str):
    addr, chain = extract_candidate_address(input_str)
    if not addr:
        return None
    chain_id = "base" if chain == "base" else "solana"
    pairs = dexscreener_tokens(chain_id, addr)
    token_address = addr
    if not pairs:
        # input may have been a pair/pool address rather than the token itself
        pair_pairs = dexscreener_pair(chain_id, addr)
        if pair_pairs:
            token_address = pair_pairs[0]["baseToken"]["address"]
            pairs = dexscreener_tokens(chain_id, token_address) or pair_pairs
    return chain_id, token_address, pairs


def goplus_security(chain, address):
    if chain == "base":
        url = f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={address}"
    else:
        url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}"
    data = fetch_json(url)
    result = (data or {}).get("result")
    if not result:
        return None
    if chain == "base":
        return result.get(address.lower())
    return result.get(address) or next(iter(result.values()), None)


def rugcheck_report(mint):
    return fetch_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")


def honeypot_check(address):
    return fetch_json(f"https://api.honeypot.is/v2/IsHoneypot?address={address}&chainID=8453")


def fmt_usd(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(n):
        return "n/a"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.2f}"


def fmt_age(created_ms):
    if not created_ms:
        return "unknown"
    created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def pct(value):
    """Normalize a GoPlus percent field (fraction or already-percent) to 0-100."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return v * 100 if v <= 1 else v


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def estimate_volatility_pct(best_pair):
    """Rough 'how wild does this thing move' proxy from Dexscreener's reported
    price-change windows. Not a real ATR (no OHLC history in the free API) —
    just the largest swing seen across the windows we do have."""
    pc = (best_pair or {}).get("priceChange") or {}
    windows = []
    for key in ("m5", "h1", "h6", "h24"):
        try:
            value = abs(float(pc[key]))
            if math.isfinite(value):
                windows.append(value)
        except (KeyError, TypeError, ValueError):
            pass
    return max(windows) if windows else None


def compute_trade_levels(price_usd, volatility_pct):
    """Stop-loss/take-profit distances derived purely from this pair's own
    recent volatility. Bands are deliberately wide — memecoins swing far more
    than traditional assets, and a tight stop just gets wicked out by noise."""
    try:
        price = float(price_usd)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0 or volatility_pct is None or not math.isfinite(volatility_pct):
        return None
    sl_pct = clamp(volatility_pct * 1.5, 20, 65)
    tp_pct = clamp(volatility_pct * 2.5, 30, 150)
    return {
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "sl_price": price * (1 - sl_pct / 100),
        "tp_price": price * (1 + tp_pct / 100),
        "rr": tp_pct / sl_pct,
    }


def suggest_position_size(score):
    """Generic risk-score-scaled sizing band, as a share of a dedicated
    speculative/high-risk allocation — not net worth, savings, or any actual
    portfolio, which this tool knows nothing about. min_pct/max_pct are exposed
    numerically (alongside the display string) so callers like the paper-trading
    bot can size a position without parsing text."""
    if score >= 71:
        return {"range": "Sit this one out", "min_pct": 0, "max_pct": 0, "note": "Automated checks put this in the extreme-risk band — position size 0%."}
    if score >= 46:
        return {"range": "≤ 1%", "min_pct": 0, "max_pct": 1, "note": "High risk — keep any position token-sized, not portfolio-sized."}
    if score >= 21:
        return {"range": "1-3%", "min_pct": 1, "max_pct": 3, "note": "Moderate risk — small speculative slice only."}
    return {"range": "3-5%", "min_pct": 3, "max_pct": 5, "note": "Lower (relative) risk for a memecoin — still cap it hard."}


def is_safe_url(url):
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


def social_label(social_type):
    labels = {"twitter": "Twitter/X", "x": "Twitter/X", "telegram": "Telegram", "discord": "Discord", "website": "Website", "Website": "Website"}
    if social_type in labels:
        return labels[social_type]
    return social_type.capitalize() if social_type else "Link"


def fmt_price(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(n):
        return "n/a"
    if n == 0:
        return "$0"
    abs_n = abs(n)
    if abs_n >= 1:
        return f"${n:.4f}"
    decimals = int(clamp(math.ceil(-math.log10(abs_n)) + 3, 4, 12))
    return f"${n:.{decimals}f}"


def compute_confidence(chain, has_pair_data, security, rugcheck, honeypot):
    """How much of the scoring picture we actually have, 0-100. A score computed
    from complete data and a score computed with half the checks unavailable
    are not the same claim, even if the number comes out identical - this is
    what lets a caller tell them apart instead of treating a missing API as a
    silent "clean" result."""
    if not has_pair_data:
        return 20  # almost nothing else could be meaningfully evaluated
    confidence = 100
    if not security:
        confidence -= 40
    if chain == "solana" and not valid_rugcheck(rugcheck):
        confidence -= 15
    if chain == "base" and not valid_honeypot(honeypot):
        confidence -= 15
    return max(0, confidence)


def valid_rugcheck(data):
    return isinstance(data, dict) and isinstance(data.get("risks"), list) and all(isinstance(r, dict) for r in data["risks"])


def valid_honeypot(data):
    return (isinstance(data, dict) and isinstance(data.get("honeypotResult"), dict)
            and type(data["honeypotResult"].get("isHoneypot")) is bool)


def analyze(chain, token_address, pairs, security, rugcheck, honeypot):
    flags = []  # (severity, message) - kept for backward compatibility with existing renderers
    components = []  # structured: [{rule, severity, points, message}] - one entry per rule EVALUATED,
    # including zero-point "good"/negative findings, so downstream analysis can compute a true
    # trigger-rate (N times fired / N times evaluated) per rule, not just a list of what fired.
    score = 0

    def flag(rule_id, severity, points, message):
        flags.append((severity, message))
        components.append({"rule": rule_id, "severity": severity, "points": points, "message": message})
        return points

    cfg = SCORING_CONFIG
    best_pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0) if pairs else None
    liquidity_usd = ((best_pair or {}).get("liquidity") or {}).get("usd")
    volume_24h = ((best_pair or {}).get("volume") or {}).get("h24")
    price_usd = (best_pair or {}).get("priceUsd")
    fdv = (best_pair or {}).get("fdv") or (best_pair or {}).get("marketCap")
    created_ms = (best_pair or {}).get("pairCreatedAt")
    price_change_24h = ((best_pair or {}).get("priceChange") or {}).get("h24")
    txns_24h = ((best_pair or {}).get("txns") or {}).get("h24") or {}
    name = ((best_pair or {}).get("baseToken") or {}).get("name")
    symbol = ((best_pair or {}).get("baseToken") or {}).get("symbol")
    info = (best_pair or {}).get("info") or {}
    websites = [w for w in (info.get("websites") or []) if is_safe_url(w.get("url"))]
    socials = [s for s in (info.get("socials") or []) if is_safe_url(s.get("url"))]

    if best_pair and not websites and not socials:
        score += flag("no_socials", "medium", 5, "No official website or social links (Twitter/Telegram/Discord) found on Dexscreener — most legitimate projects list at least one. Could just be a very fresh listing, so verify manually.")
    elif best_pair:
        components.append({"rule": "no_socials", "severity": "good", "points": 0, "message": "Official website/social link present."})

    if liquidity_usd is None:
        score += flag("no_liquidity", "critical", 30, "No liquidity pool found on Dexscreener — token may be untradeable, unlisted, or the address is wrong.")
    elif liquidity_usd < cfg["liquidity_critical_usd"]:
        score += flag("liquidity_critical", "critical", 30, f"Extremely low liquidity ({fmt_usd(liquidity_usd)}) — trivial to manipulate price.")
    elif liquidity_usd < cfg["liquidity_high_usd"]:
        score += flag("liquidity_high", "high", 18, f"Low liquidity ({fmt_usd(liquidity_usd)}) — expect heavy slippage.")
    elif liquidity_usd < cfg["liquidity_medium_usd"]:
        score += flag("liquidity_medium", "medium", 6, f"Moderate liquidity ({fmt_usd(liquidity_usd)}).")
    else:
        components.append({"rule": "liquidity_medium", "severity": "good", "points": 0, "message": f"Liquidity healthy ({fmt_usd(liquidity_usd)})."})

    if created_ms:
        age_hours = (time.time() * 1000 - created_ms) / 3_600_000
        if age_hours < cfg["pool_age_high_hours"]:
            score += flag("pool_age_high", "high", 15, f"Pool created {fmt_age(created_ms)} — extremely new, classic rug window.")
        elif age_hours < cfg["pool_age_medium_hours"]:
            score += flag("pool_age_medium", "medium", 8, f"Pool created {fmt_age(created_ms)} — still very new.")
        else:
            components.append({"rule": "pool_age_high", "severity": "good", "points": 0, "message": f"Pool age healthy ({fmt_age(created_ms)})."})
    else:
        components.append({"rule": "pool_age_high", "severity": "unknown", "points": 0, "message": "Pool creation timestamp unavailable."})

    if volume_24h is not None and liquidity_usd:
        if volume_24h < liquidity_usd * cfg["volume_to_liquidity_min_ratio"]:
            score += flag("thin_volume", "medium", 5, "24h volume is very low relative to liquidity — thin/inactive trading.")
        else:
            components.append({"rule": "thin_volume", "severity": "good", "points": 0, "message": "24h volume healthy relative to liquidity."})

    if chain == "solana":
        if security:
            mint_status = (security.get("mintable") or {}).get("status")
            freeze_status = (security.get("freezable") or {}).get("status")
            if mint_status == "1":
                score += flag("mint_authority", "critical", 25, "Mint authority is NOT renounced — the dev can mint unlimited new tokens at will.")
            elif mint_status == "0":
                components.append({"rule": "mint_authority", "severity": "good", "points": 0, "message": "Mint authority renounced."})
            else:
                components.append({"rule": "mint_authority", "severity": "unknown", "points": 0, "message": "Mint authority status unavailable."})
            if freeze_status == "1":
                score += flag("freeze_authority", "critical", 25, "Freeze authority is NOT renounced — the dev can freeze any wallet's tokens.")
            elif freeze_status == "0":
                components.append({"rule": "freeze_authority", "severity": "good", "points": 0, "message": "Freeze authority renounced."})
            else:
                components.append({"rule": "freeze_authority", "severity": "unknown", "points": 0, "message": "Freeze authority status unavailable."})
            top10 = pct(security.get("top10_holder_percent"))
            if top10 is not None:
                if top10 > cfg["holder_concentration_critical_pct"]:
                    score += flag("holder_concentration", "critical", 20, f"Top 10 holders control {top10:.0f}% of supply — extreme concentration.")
                elif top10 > cfg["holder_concentration_high_pct"]:
                    score += flag("holder_concentration", "high", 10, f"Top 10 holders control {top10:.0f}% of supply.")
                else:
                    components.append({"rule": "holder_concentration", "severity": "good", "points": 0, "message": f"Top 10 holder concentration healthy ({top10:.0f}%)."})
            else:
                components.append({"rule": "holder_concentration", "severity": "unknown", "points": 0, "message": "Holder concentration data unavailable."})
        else:
            for rule_id in ("mint_authority", "freeze_authority", "holder_concentration"):
                components.append({"rule": rule_id, "severity": "unknown", "points": 0, "message": "GoPlus security data unavailable for this check."})
            score += flag("goplus_unavailable", "medium", 8, "GoPlus security data unavailable (API error/timeout) — mint/freeze authority and holder concentration could not be checked. Treat this score as less reliable than usual.")
        if valid_rugcheck(rugcheck):
            seen_risks = set()
            for r in rugcheck.get("risks") or []:
                level = (r.get("level") or "").lower()
                desc = r.get("description") or r.get("name")
                if not desc or desc in seen_risks:
                    continue
                seen_risks.add(desc)
                if level in ("danger", "high"):
                    score += flag("rugcheck_high", "high", 8, f"RugCheck: {desc}")
                elif level == "warn":
                    score += flag("rugcheck_warn", "medium", 3, f"RugCheck: {desc}")
            if not (rugcheck.get("risks") or []):
                components.append({"rule": "rugcheck_high", "severity": "good", "points": 0, "message": "RugCheck reported no risk findings."})
        else:
            components.append({"rule": "rugcheck_high", "severity": "unknown", "points": 0, "message": "RugCheck data unavailable."})
            score += flag("rugcheck_unavailable", "medium", 5, "RugCheck data unavailable (API error/timeout) — this Solana-specific risk report could not be checked.")
    else:  # base / EVM
        if security:
            def is_flagged(key):
                return str(security.get(key)) == "1"

            if is_flagged("is_honeypot"):
                score += flag("honeypot", "critical", 40, "GoPlus flags this as a HONEYPOT — you may not be able to sell.")
            elif str(security.get("is_honeypot")) == "0":
                components.append({"rule": "honeypot", "severity": "good", "points": 0, "message": "GoPlus reports this is not a honeypot."})
            else:
                components.append({"rule": "honeypot", "severity": "unknown", "points": 0, "message": "Honeypot status unavailable."})
            if security.get("is_open_source") == "0":
                score += flag("not_open_source", "high", 12, "Contract source code is NOT verified/open source.")
            elif security.get("is_open_source") == "1":
                components.append({"rule": "not_open_source", "severity": "good", "points": 0, "message": "Contract source is verified/open source."})
            else:
                components.append({"rule": "not_open_source", "severity": "unknown", "points": 0, "message": "Contract verification status unavailable."})
            if is_flagged("cannot_sell_all"):
                score += flag("cannot_sell_all", "critical", 20, "Cannot sell full balance — sell restriction detected.")
            if is_flagged("hidden_owner"):
                score += flag("hidden_owner", "critical", 20, "Hidden owner detected — ownership may not actually be renounced.")
            if is_flagged("can_take_back_ownership"):
                score += flag("ownership_reclaimable", "critical", 20, "Ownership can be reclaimed by the deployer even if it looks renounced.")
            if is_flagged("selfdestruct"):
                score += flag("selfdestruct", "critical", 20, "Contract has a self-destruct function.")
            if is_flagged("is_mintable"):
                score += flag("mintable", "high", 15, "Token is mintable — supply can be inflated.")
            elif str(security.get("is_mintable")) == "0":
                components.append({"rule": "mintable", "severity": "good", "points": 0, "message": "Token is not mintable."})
            else:
                components.append({"rule": "mintable", "severity": "unknown", "points": 0, "message": "Mintability unavailable."})
            for field, rule in (("cannot_sell_all", "cannot_sell_all"), ("hidden_owner", "hidden_owner"),
                                ("can_take_back_ownership", "ownership_reclaimable"), ("selfdestruct", "selfdestruct")):
                if not is_flagged(field):
                    known = str(security.get(field)) == "0"
                    components.append({"rule": rule, "severity": "good" if known else "unknown", "points": 0,
                                       "message": "Check negative." if known else "Check unavailable."})
            for tax_key, rule_id, label in (("buy_tax", "buy_tax", "Buy"), ("sell_tax", "sell_tax", "Sell")):
                taxp = pct(security.get(tax_key))
                if taxp is not None:
                    if taxp >= cfg["tax_critical_pct"]:
                        score += flag(rule_id, "critical", 20, f"{label} tax is {taxp:.0f}% — likely a scam/near-unsellable token.")
                    elif taxp >= cfg["tax_medium_pct"]:
                        score += flag(rule_id, "medium", 6, f"{label} tax is {taxp:.0f}%.")
                    else:
                        components.append({"rule": rule_id, "severity": "good", "points": 0, "message": f"{label} tax low ({taxp:.0f}%)."})
                else:
                    components.append({"rule": rule_id, "severity": "unknown", "points": 0, "message": f"{label} tax unavailable."})
            owner = security.get("owner_address")
            if owner and owner != "0x0000000000000000000000000000000000000000" and not is_flagged("hidden_owner"):
                score += flag("ownership_not_renounced", "medium", 5, "Contract ownership is not renounced.")
            elif owner == "0x0000000000000000000000000000000000000000":
                components.append({"rule": "ownership_not_renounced", "severity": "good", "points": 0, "message": "Contract ownership renounced."})
            holder_count = security.get("holder_count")
            try:
                if holder_count is not None and int(holder_count) < cfg["min_holder_count"]:
                    score += flag("low_holder_count", "medium", 5, f"Only {holder_count} holders — very small/early holder base.")
                elif holder_count is not None:
                    components.append({"rule": "low_holder_count", "severity": "good", "points": 0, "message": f"Holder count healthy ({holder_count})."})
            except ValueError:
                pass
        else:
            for rule_id in ("honeypot", "not_open_source", "cannot_sell_all", "hidden_owner", "ownership_reclaimable", "selfdestruct", "mintable", "buy_tax", "sell_tax", "ownership_not_renounced", "low_holder_count"):
                components.append({"rule": rule_id, "severity": "unknown", "points": 0, "message": "GoPlus security data unavailable for this check."})
            score += flag("goplus_unavailable", "medium", 8, "GoPlus security data unavailable (API error/timeout) — honeypot, tax, ownership, and holder checks could not be run. Treat this score as less reliable than usual.")
        if valid_honeypot(honeypot):
            hr = honeypot.get("honeypotResult") or {}
            already_flagged = any(f[0] == "critical" and "HONEYPOT" in f[1] for f in flags)
            if hr.get("isHoneypot") and not already_flagged:
                reason = hr.get("honeypotReason") or "reason unspecified"
                score += flag("honeypot_sim", "critical", 40, f"Honeypot.is confirms HONEYPOT: {reason}")
            elif not hr.get("isHoneypot"):
                components.append({"rule": "honeypot_sim", "severity": "good", "points": 0, "message": "Honeypot.is simulation found no sell restriction."})
            sim = honeypot.get("simulationResult") or {}
            sell_tax = sim.get("sellTax")
            if sell_tax is not None and sell_tax >= 20 and not any("Sell tax" in f[1] for f in flags):
                score += flag("honeypot_sim_sell_tax", "high", 10, f"Honeypot.is simulation: sell tax {sell_tax}%.")
        else:
            components.append({"rule": "honeypot_sim", "severity": "unknown", "points": 0, "message": "Honeypot.is data unavailable."})
            score += flag("honeypot_sim_unavailable", "medium", 8, "Honeypot.is data unavailable (API error/timeout) — live sell-simulation could not be run.")

    # Record the negative side of mutually exclusive bands as well as positives.
    # Absence of a component previously biased per-rule comparison cohorts.
    for rule, known in [(r, liquidity_usd is not None) for r in
                        ("no_liquidity", "liquidity_critical", "liquidity_high", "liquidity_medium")] + [
                        ("pool_age_high", bool(created_ms)), ("pool_age_medium", bool(created_ms))]:
        if not any(c["rule"] == rule for c in components):
            components.append({"rule": rule, "severity": "good" if known else "unknown", "points": 0,
                               "message": "Threshold not triggered." if known else "Input unavailable."})
    score = min(score, 100)
    volatility_pct = estimate_volatility_pct(best_pair)
    trade_levels = compute_trade_levels(price_usd, volatility_pct)
    position_size = suggest_position_size(score)
    confidence = compute_confidence(chain, best_pair is not None, security, rugcheck, honeypot)
    return {
        "name": name, "symbol": symbol, "price_usd": price_usd, "liquidity_usd": liquidity_usd,
        "volume_24h": volume_24h, "fdv": fdv, "price_change_24h": price_change_24h,
        "txns_24h": txns_24h, "created_ms": created_ms, "flags": flags, "score": score,
        "volatility_pct": volatility_pct, "trade_levels": trade_levels, "position_size": position_size,
        "websites": websites, "socials": socials,
        "score_components": components, "confidence": confidence, "analyzer_version": ANALYZER_VERSION,
        "data_sources": {
            "dexscreener": best_pair is not None,
            "goplus": security is not None,
            "rugcheck": valid_rugcheck(rugcheck) if chain == "solana" else None,
            "honeypot_is": valid_honeypot(honeypot) if chain == "base" else None,
        },
    }


def risk_label(score):
    if score >= 71:
        return "EXTREME RISK"
    if score >= 46:
        return "HIGH RISK"
    if score >= 21:
        return "MODERATE RISK"
    return "LOWER RISK"


def get_current_price(chain, token_address):
    """Compatibility helper: positive finite USD price, otherwise None.

    Use get_current_prices to distinguish a failed lookup from no listing.
    Neither is proof of a rug or an executable zero-price exit.
    """
    value = get_current_prices(chain, [token_address]).get(token_address)
    return None if value is FETCH_FAILED else value


# Dexscreener's /latest/dex/tokens endpoint accepts up to 30 comma-separated
# addresses per call - but returns AT MOST 30 PAIRS in total, not 30 tokens.
# Found the hard way: with 30 memecoins per request, tokens at the tail of the
# list fell off the end of the response and looked exactly like "no pairs any
# more, so it rugged" - the request succeeded, so no circuit breaker caught
# it, and the same tokens fell off every tick, so they hit the streak limit
# together and closed as a cluster. So: small batches, and whenever a response
# is at the cap, any token missing from it is re-asked individually before
# anyone concludes it has no pairs.
DEXSCREENER_BATCH_SIZE = 10
DEXSCREENER_PAIR_CAP = 30
DEXSCREENER_BATCH_SPACING_SECONDS = 0.25
FETCH_FAILED = object()  # sentinel: the REQUEST failed - says nothing about the token


def _best_prices_from_pairs(chain, pairs, wanted):
    """{address: price} for each wanted address that has a pair on this chain,
    using its most liquid pair. Addresses with no pair are simply absent."""
    normalize = str.lower if chain == "base" else lambda a: a
    by_lower = {normalize(a): a for a in wanted}
    best = {}
    for pair in pairs:
        if pair.get("chainId") != chain:
            continue
        base = normalize((pair.get("baseToken") or {}).get("address") or "")
        key = by_lower.get(base)
        if key is None:
            continue
        try:
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            if not math.isfinite(liq) or liq < 0:
                liq = 0
        except (TypeError, ValueError):
            liq = 0
        if key not in best or liq > best[key][0]:
            best[key] = (liq, pair.get("priceUsd"))
    out = {}
    for a, (_, price) in best.items():
        try:
            value = float(price)
            out[a] = value if math.isfinite(value) and value > 0 else FETCH_FAILED
        except (TypeError, ValueError):
            out[a] = FETCH_FAILED
    return out


def _valid_pair_response(data):
    # Missing keys, error objects and malformed nested data are transport/schema
    # failures. An explicit empty list is an observation of no indexed pairs.
    if isinstance(data, dict) and "pairs" in data and data["pairs"] is None:
        return True  # Dexscreener's shape for "no such token": an explicit null, not an error
    return (isinstance(data, dict) and isinstance(data.get("pairs"), list)
            and all(isinstance(p, dict) and isinstance(p.get("chainId"), str)
                    and isinstance(p.get("baseToken"), dict)
                    and isinstance(p["baseToken"].get("address"), str)
                    and (p.get("liquidity") is None or isinstance(p["liquidity"], dict))
                    for p in data["pairs"]))


def get_current_prices(chain, token_addresses):
    """Map token addresses to positive finite USD prices, None for an explicit
    empty individual listing, or FETCH_FAILED for network/schema/quote errors.
    Recheck all omissions from multi-token batches individually. No result here
    proves liquidity removal, rugging, or the ability to execute at this price.
    """
    results = {}
    addrs = list(dict.fromkeys(a for a in token_addresses if a))
    needs_individual = []
    first = True
    for i in range(0, len(addrs), DEXSCREENER_BATCH_SIZE):
        chunk = addrs[i:i + DEXSCREENER_BATCH_SIZE]
        if not first:
            time.sleep(DEXSCREENER_BATCH_SPACING_SECONDS)
        first = False
        data = fetch_json("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk))
        if not _valid_pair_response(data):
            for a in chunk:
                results[a] = FETCH_FAILED
            continue
        pairs = data.get("pairs") or []
        found = _best_prices_from_pairs(chain, pairs, chunk)
        truncated = len(pairs) >= DEXSCREENER_PAIR_CAP
        for a in chunk:
            if a in found:
                results[a] = found[a]
            elif len(chunk) > 1 or truncated:
                needs_individual.append(a)  # may have fallen off the end - not evidence of anything
            else:
                results[a] = None  # response was complete and the token wasn't in it
    for a in needs_individual:
        time.sleep(DEXSCREENER_BATCH_SPACING_SECONDS)
        data = fetch_json("https://api.dexscreener.com/latest/dex/tokens/" + a)
        if not _valid_pair_response(data):
            results[a] = FETCH_FAILED
            continue
        results[a] = _best_prices_from_pairs(chain, data.get("pairs") or [], [a]).get(a)
    return results

def full_check(input_str):
    """Resolve + fetch + score a token/pair address or link in one call. Returns
    None if no candidate address could be extracted from the input."""
    resolved = resolve(input_str)
    if not resolved:
        return None
    chain, token_address, pairs = resolved
    security = goplus_security(chain, token_address)
    rugcheck = rugcheck_report(token_address) if chain == "solana" else None
    honeypot = honeypot_check(token_address) if chain == "base" else None
    result = analyze(chain, token_address, pairs, security, rugcheck, honeypot)
    return chain, token_address, result
