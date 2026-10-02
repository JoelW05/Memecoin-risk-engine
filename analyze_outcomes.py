#!/usr/bin/env python3
"""Phase 2/3 analysis: does the risk score actually predict outcomes?

Reads paperbot_candidates.jsonl + paperbot_outcomes.jsonl + paperbot_state.json
and answers the questions the whole instrumentation pass existed to enable:

  1. Score bucket vs real price outcome, across the FULL candidate pool
     (bought and skipped alike) - not just what passed the entry filters.
     This is the review's "Cohort A" comparison.
  2. Per-rule trigger vs no-trigger outcome comparison - does a specific
     rule (mint authority, holder concentration, etc.) actually correlate
     with what happened, or is its point weight just intuition?
  3. Confidence vs outcome - does low-confidence data actually produce
     worse/noisier results, justifying the confidence score's existence?
  4. Real closed-trade performance from paperbot_state.json (win rate,
     P&L by exit reason, by score bucket).

Pure standard library. Usage:
  python analyze_outcomes.py
  python analyze_outcomes.py --candidates path\\to\\paperbot_candidates.jsonl --outcomes path\\to\\paperbot_outcomes.jsonl --state path\\to\\paperbot_state.json
  python analyze_outcomes.py --checkpoint-hours 6
"""

import argparse
import json
import math
from datetime import datetime, timezone
import statistics
from collections import defaultdict

BUCKETS = [(0, 20, "0-20   (Lower)"), (21, 45, "21-45  (Moderate)"), (46, 70, "46-70  (High)"), (71, 100, "71-100 (Extreme)")]

# Dexscreener occasionally returns a glitched quote (a near-zero price, or a
# single-tick spike with no real counterparty) rather than an actual tradeable
# price. A genuine memecoin can plausibly 100x; nothing genuinely does 1000x+
# in a day with a real buyer on the other side of that trade. Anything beyond
# this is data corruption, not a result - excluded from stats, but the count
# excluded is always printed so this filter can't quietly hide real outcomes.
SANE_PCT_CHANGE_BOUND = 100_000  # %, i.e. 1000x


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith("\n"):
                    print(f"  WARNING: incomplete final JSONL record in {path}:{line_number}; skipped during live read.")
                    break
                raise


def bucket_for(score):
    if score is None:
        return None
    for lo, hi, label in BUCKETS:
        if lo <= score <= hi:
            return label
    return None


def fmt(v, digits=1):
    return f"{v:+.{digits}f}%" if v is not None else "n/a"


def section(title):
    print(f"\n=== {title} ===")


def check_config_drift(candidates):
    """Every candidate row stamps the exact entry-filter config active when it
    was evaluated. If more than one distinct config shows up in this file,
    every stat below is silently blending runs from before and after a
    settings change - flag it loudly rather than let that happen quietly."""
    seen_configs = {}
    for c in candidates:
        cfg = c.get("config")
        if not cfg:
            continue
        key = json.dumps([cfg, c.get("analyzer_version"), c.get("paperbot_version")], sort_keys=True)
        if key not in seen_configs:
            seen_configs[key] = {"config": cfg, "count": 0, "first_seen": c.get("timestamp")}
        seen_configs[key]["count"] += 1

    section("0. Config drift check")
    if len(seen_configs) <= 1:
        print("  No config/version drift detected among available records; this does not validate labels or cohorts.")
        return
    print(f"  WARNING: {len(seen_configs)} distinct configs found in this candidate log.")
    print("  Every section below MIXES data from different entry-filter settings unless you filter")
    print("  by config yourself. Treat bucket/rule comparisons with real caution until this stops.")
    for key, info in sorted(seen_configs.items(), key=lambda kv: kv[1]["first_seen"] or ""):
        print(f"  - {info['count']:>6} rows, first seen {info['first_seen']}: "
              f"max_score={info['config'].get('max_risk_score')} "
              f"max_positions={info['config'].get('max_positions')} "
              f"max_buys_per_cycle={info['config'].get('max_buys_per_cycle')}")


def filter_outage_clusters(outcomes):
    """Groups rugged_or_delisted=True outcome rows by checked_at timestamp.
    Any group of >1 different tokens marked rugged at the exact same second is
    almost certainly a single Dexscreener outage/rate-limit blip misread as
    each token independently rugging, not independent events - the same root
    cause already found and fixed in closed-trade data (see
    closed_trade_performance). check_pending_outcomes() in paperbot.py now
    has its own circuit breaker to stop new ones going forward; this catches
    whatever was already logged before that fix landed.
    Returns (filtered_outcomes, excluded_count, groups_by_timestamp, reinstated_count)
    where reinstated_count is rugged rows that shared a second with successfully
    priced rows and were therefore kept as genuine."""
    # A rate-limit burst fails EVERY request in that second; a healthy second
    # (the old code did several sequential checks per second) mixes priced
    # rows with the odd genuine rug. So two rugs in one second is only an
    # outage if nothing in that same second was successfully priced.
    by_ts = defaultdict(lambda: {"rugged": [], "priced": 0})
    for o in outcomes:
        slot = by_ts[o.get("checked_at")]
        if o.get("rugged_or_delisted"):
            slot["rugged"].append(o)
        else:
            slot["priced"] += 1
    cluster_ids = set()
    groups = {}
    reinstated = 0
    for ts, slot in by_ts.items():
        identities = {(o.get("chain"), o.get("address")) for o in slot["rugged"] if o.get("address")}
        if ts and len(identities) > 1:
            if slot["priced"] == 0:
                cluster_ids.update(id(o) for o in slot["rugged"])
                groups[ts] = slot["rugged"]
            else:
                reinstated += len(slot["rugged"])
    filtered = [o for o in outcomes if id(o) not in cluster_ids]
    return filtered, len(cluster_ids), groups, reinstated


def observation_status_by_bucket(raw_rows):
    """What happened to every checkpoint that came due, before any censoring:
    priced, no indexed listing, request failed, missed. 'No listing at the
    horizon' is reported as its own category because it is the closest thing
    the data has to 'gone' - but it is a Dexscreener indexing fact, not a
    verified on-chain event, and the table says so."""
    section("1a. Observation status by score bucket (every due checkpoint, nothing censored)")
    statuses = ["observed", "no_pairs", "fetch_failed", "missed_checkpoint", "invalid_price",
                "implausible_price", "capacity_evicted", "not_tracked_at_capacity"]
    grouped = defaultdict(lambda: defaultdict(int))
    legacy = defaultdict(int)
    for o in raw_rows:
        label = bucket_for(o.get("score_at_discovery"))
        if not label:
            continue
        status = o.get("observation_status")
        if status is None:
            legacy[label] += 1  # pre-1.4.0 rows carry a binary label instead of a status
            continue
        grouped[label][status if status in statuses else "other"] += 1
    print(f"  {'Bucket':<20} {'due':>6} {'priced':>7} {'no listing':>11} {'req failed':>11} {'missed':>7} {'other':>6}   {'no-listing %':>12}")
    for _, _, label in BUCKETS:
        g = grouped.get(label)
        if not g:
            continue
        due = sum(g.values())
        other = sum(v for k, v in g.items() if k not in ("observed", "no_pairs", "fetch_failed", "missed_checkpoint"))
        resolved = g["observed"] + g["no_pairs"]
        nl_pct = f"{g['no_pairs'] / resolved * 100:.1f}%" if resolved else "n/a"
        print(f"  {label:<20} {due:>6} {g['observed']:>7} {g['no_pairs']:>11} {g['fetch_failed']:>11} {g['missed_checkpoint']:>7} {other:>6}   {nl_pct:>12}")
    if legacy:
        print(f"  (plus {sum(legacy.values())} legacy rows without an observation status - see the legacy rug% column below)")
    print("  no-listing % = no listing / (priced + no listing). A token Dexscreener no longer indexes at the")
    print("  horizon is the nearest observable proxy for 'gone'; it is not a verified liquidity removal.")
    print("  Request failures and missed checkpoints are missingness, not outcomes - if they are large,")
    print("  the priced statistics below are conditioned on an unrepresentative subset.")


def score_bucket_vs_outcome(outcomes):
    section("1. Score buckets: tracked candidates (bought and skipped when tracking is enabled)")
    grouped = defaultdict(list)
    for o in outcomes:
        label = bucket_for(o.get("score_at_discovery"))
        if label:
            grouped[label].append(o)
    print(f"  {'Bucket':<20} {'priced n':>8} {'label n':>8} {'legacy rug%':>12} {'median':>10} {'mean':>10}")
    for _, _, label in BUCKETS:
        rows = grouped.get(label, [])
        if not rows:
            continue
        vals = [o["price_change_pct"] for o in rows
                if not o.get("rugged_or_delisted") and isinstance(o.get("price_change_pct"), (int, float))
                and math.isfinite(o["price_change_pct"]) and abs(o["price_change_pct"]) <= SANE_PCT_CHANGE_BOUND]
        labeled = [o for o in rows if type(o.get("rugged_or_delisted")) is bool]
        rate = f"{sum(o['rugged_or_delisted'] for o in labeled) / len(labeled) * 100:.1f}%" if labeled else "n/a"
        median = fmt(statistics.median(vals)) if vals else "n/a"
        mean = fmt(statistics.mean(vals)) if vals else "n/a"
        print(f"  {label:<20} {len(vals):>8} {len(labeled):>8} {rate:>12} {median:>10} {mean:>10}")
    print("  Legacy rug rates use old heuristic binary labels only; new observations do not infer rugs.")
    print("  Returns are conditional on usable prices. Missing observations are censored, not zero returns.")


def rule_level_analysis(outcomes, candidates_by_address, min_n=20):
    section("2. Per-rule trigger vs no-trigger outcome (does this specific rule predict anything?)")
    # NOTE: outcome rows never carry score_components themselves (only
    # score_at_discovery) - the per-rule breakdown only exists by joining
    # back to the matching candidate row, same as growth_indicators() does.
    triggered = defaultdict(list)
    not_triggered = defaultdict(list)
    triggered_rug = defaultdict(lambda: [0, 0])
    not_triggered_rug = defaultdict(lambda: [0, 0])

    for o in outcomes:
        cand = candidate_for_outcome(o, candidates_by_address)
        if cand is None:
            continue
        comps = cand.get("score_components") or []
        seen_rules_this_row = set()
        for c in comps:
            rule = c.get("rule")
            if not rule or rule in seen_rules_this_row or c.get("severity") == "unknown":
                continue
            seen_rules_this_row.add(rule)
            fired = rule_fired(cand, rule)
            target = triggered if fired else not_triggered
            rug_target = triggered_rug if fired else not_triggered_rug
            if type(o.get("rugged_or_delisted")) is bool:
                rug_target[rule][1] += 1
            if o.get("rugged_or_delisted"):
                rug_target[rule][0] += 1
                continue
            pct = o.get("price_change_pct")
            if pct is not None and abs(pct) <= SANE_PCT_CHANGE_BOUND:
                target[rule].append(pct)

    all_rules = sorted(set(triggered_rug) | set(not_triggered_rug) | set(triggered) | set(not_triggered))
    print(f"  {'Rule':<24} {'fired n':>8} {'fired med':>10} {'clean n':>8} {'clean med':>10} {'fired rug%':>11} {'clean rug%':>11}")
    for rule in all_rules:
        fired_vals = triggered.get(rule, [])
        clean_vals = not_triggered.get(rule, [])
        f_rug, f_total = triggered_rug.get(rule, [0, 0])
        c_rug, c_total = not_triggered_rug.get(rule, [0, 0])
        if len(fired_vals) + f_rug < min_n or len(clean_vals) + c_rug < min_n:
            continue  # too little data on this rule to say anything
        f_med = f"{statistics.median(fired_vals):+.1f}%" if fired_vals else "n/a"
        c_med = f"{statistics.median(clean_vals):+.1f}%" if clean_vals else "n/a"
        f_rug_pct = f"{f_rug/f_total*100:.1f}%" if f_total else "n/a"
        c_rug_pct = f"{c_rug/c_total*100:.1f}%" if c_total else "n/a"
        print(f"  {rule:<24} {len(fired_vals) + f_rug:>8} {f_med:>10} {len(clean_vals) + c_rug:>8} {c_med:>10} {f_rug_pct:>11} {c_rug_pct:>11}")
    print(f"\n  (rules with fewer than {min_n} observations on either side are omitted - not enough data yet)")
    print("  Rug percentages use only legacy binary labels; median returns use priced observations.")
    print("  A rule that's actually doing work should show a WORSE median/rug% when fired than when clean.")
    print("  A rule where fired and clean look about the same is carrying its point weight on vibes so far.")


def rule_level_delisting(raw_rows, candidates_by_address, min_n=50):
    """Per-rule delisting rate: the properly measured successor to the old rug%.

    Section 2 can only use priced rows, because unpriced observations are
    censored before it sees them - which is why its rug columns read n/a. But
    "no longer indexed at the horizon" is the outcome that actually separates
    the score buckets (section 1a), so the per-rule version of it is the
    attribution that matters: which individual rules drive that gradient?

    Resolved = priced + no listing. Missing, late and failed observations are
    excluded from both numerator and denominator rather than counted either
    way. A token Dexscreener no longer indexes is the nearest observable proxy
    for 'gone'; it is not a verified liquidity removal."""
    section("2a. Per-rule delisting rate (which rules drive the bucket gradient?)")
    fired = defaultdict(lambda: [0, 0])   # rule -> [no_listing, resolved]
    clean = defaultdict(lambda: [0, 0])
    for o in raw_rows:
        status = o.get("observation_status")
        if status not in ("observed", "no_pairs"):
            continue
        cand = candidate_for_outcome(o, candidates_by_address)
        if cand is None:
            continue
        gone = status == "no_pairs"
        seen = set()
        for comp in cand.get("score_components") or []:
            rule = comp.get("rule")
            if not rule or rule in seen or comp.get("severity") == "unknown":
                continue
            seen.add(rule)
            hit = rule_fired(cand, rule)
            if hit is None:
                continue
            bucket = fired if hit else clean
            bucket[rule][1] += 1
            if gone:
                bucket[rule][0] += 1

    rows = []
    for rule in sorted(set(fired) | set(clean)):
        f_gone, f_n = fired.get(rule, [0, 0])
        c_gone, c_n = clean.get(rule, [0, 0])
        if f_n < min_n or c_n < min_n:
            continue
        f_rate, c_rate = f_gone / f_n * 100, c_gone / c_n * 100
        lift = (f_rate / c_rate) if c_rate > 0 else float("inf")
        rows.append((rule, f_n, f_rate, c_n, c_rate, lift))
    if not rows:
        print(f"  No rule has {min_n}+ resolved observations on both sides yet.")
        return
    rows.sort(key=lambda r: r[5], reverse=True)
    print(f"  {'Rule':<24} {'fired n':>8} {'fired gone%':>12} {'clean n':>8} {'clean gone%':>12} {'lift':>8}")
    for rule, f_n, f_rate, c_n, c_rate, lift in rows:
        lift_s = "inf" if lift == float("inf") else f"{lift:.2f}x"
        print(f"  {rule:<24} {f_n:>8} {f_rate:>11.1f}% {c_n:>8} {c_rate:>11.1f}% {lift_s:>8}")
    print(f"\n  (rules with fewer than {min_n} resolved observations on either side are omitted)")
    print("  lift = fired gone% / clean gone%. Above 1 means the rule marks tokens that vanish more")
    print("  often; below 1 means it marks tokens that SURVIVE more often, i.e. it points the wrong")
    print("  way and its point weight is working against the score.")
    print("  These are per-rule associations on an observational sample, not independent effects -")
    print("  rules correlate heavily with each other (thin liquidity, young pool and no socials")
    print("  mostly describe the same coins), so they cannot simply be added up.")


def confidence_vs_outcome(outcomes):
    section("3. Confidence vs outcome (does low-confidence data actually produce worse/noisier results?)")
    buckets = [(0, 50, "0-50 (low)"), (51, 80, "51-80 (medium)"), (81, 100, "81-100 (high)")]
    stats = defaultdict(list)
    for o in outcomes:
        conf = o.get("confidence_at_discovery")
        pct = o.get("price_change_pct")
        if conf is None or o.get("rugged_or_delisted") or pct is None or abs(pct) > SANE_PCT_CHANGE_BOUND:
            continue
        for lo, hi, label in buckets:
            if lo <= conf <= hi:
                stats[label].append(pct)
                break
    for _, _, label in buckets:
        vals = stats.get(label, [])
        if not vals:
            continue
        print(f"  {label:<15} n={len(vals):<6} median={statistics.median(vals):+.1f}%  stdev={statistics.pstdev(vals):.1f}")
    print("\n  Higher stdev at lower confidence would validate the confidence score as a real signal,")
    print("  not just cosmetic.")


def closed_trade_performance(state):
    section("4. Real closed-trade performance (paperbot_state.json)")
    all_closed = state.get("closed_trades", [])

    # Bug found in production and fixed in paperbot.py: a Dexscreener outage
    # could hit every open position at once and get mass-misread as every
    # single coin simultaneously rugging (confirmed: native SOL itself was
    # once in such a cluster, which is definitionally impossible). Any group
    # of "rugged" closes sharing the exact same closed_at timestamp is almost
    # certainly one such outage, not independent rugs - flag and exclude.
    # Same refinement as filter_outage_clusters(): a stop-loss or take-profit
    # closing in the same second proves prices were being fetched fine, so
    # any rugs alongside it are genuine, not an outage.
    by_close_time = defaultdict(lambda: {"rugged": [], "other": 0})
    for t in all_closed:
        slot = by_close_time[t.get("closed_at")]
        if "rugged" in (t.get("reason") or ""):
            slot["rugged"].append(t)
        else:
            slot["other"] += 1
    mass_rug_cluster_ids = set()
    for ts, slot in by_close_time.items():
        if len(slot["rugged"]) > 1 and slot["other"] == 0:
            mass_rug_cluster_ids.update(id(t) for t in slot["rugged"])

    closed = []
    glitched = []
    mass_rug_false_positives = []
    for t in all_closed:
        if id(t) in mass_rug_cluster_ids:
            mass_rug_false_positives.append(t)
            continue
        entry, exit_ = t.get("entry_price"), t.get("exit_price")
        implied_pct = abs((exit_ / entry - 1) * 100) if entry and exit_ else 0
        (glitched if implied_pct > SANE_PCT_CHANGE_BOUND else closed).append(t)

    print(f"  Total closed trades: {len(all_closed)}", end="")
    if glitched:
        print(f"  ({len(glitched)} excluded below - implausible >1000x price move, glitched quote not a real outcome)")
        for t in glitched:
            print(f"    EXCLUDED: {t['symbol']:<12} entry {t['entry_price']:.10g} -> exit {t['exit_price']:.10g} (reason: {t['reason']})")
    else:
        print()
    if mass_rug_false_positives:
        by_ts = defaultdict(list)
        for t in mass_rug_false_positives:
            by_ts[t["closed_at"]].append(t["symbol"])
        print(f"  ({len(mass_rug_false_positives)} more excluded - mass-rug clusters, almost certainly API outages not real rugs)")
        for ts, symbols in by_ts.items():
            print(f"    EXCLUDED: {len(symbols)} coins simultaneously 'rugged' at {ts}: {', '.join(symbols)}")
    if not closed:
        print("  No (non-glitched) closed trades yet.")
        return
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    print(f"  Win rate: {len(wins)}/{len(closed)} ({len(wins)/len(closed)*100:.1f}%)")
    print(f"  Total realized P&L: {sum(t['pnl'] for t in closed):+.2f}")
    print(f"  Avg P&L per trade: {sum(t['pnl'] for t in closed)/len(closed):+.2f}")

    # Win rate alone is a bad primary metric for something this fat-tailed - a
    # 30% win rate with big winners can beat a 70% win rate of small ones.
    # Profit factor and a Sharpe/Sortino-style ratio judge the DISTRIBUTION of
    # outcomes, not just how often a trade happened to be positive.
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    pf_str = f"{profit_factor:.2f}" if profit_factor != float("inf") else "inf (no losses yet)"
    print(f"  Profit factor (gross wins / gross losses): {pf_str}  -- above 1.0 is net profitable, below 1.0 isn't")

    returns_pct = [t["pnl"] / t["invested"] * 100 for t in closed if t.get("invested")]
    if len(returns_pct) >= 2:
        mean_r = statistics.mean(returns_pct)
        stdev_r = statistics.pstdev(returns_pct)
        sharpe_like = mean_r / stdev_r if stdev_r else None
        downside = [r for r in returns_pct if r < 0]
        downside_dev = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns_pct) / len(returns_pct))
        sortino_like = mean_r / downside_dev if downside_dev else None
        print(f"  Mean return per trade: {mean_r:+.1f}%   stdev: {stdev_r:.1f}%")
        print(f"  Reward:variability ratio (Sharpe-like, no risk-free rate): {sharpe_like:.2f}" if sharpe_like is not None else "  Reward:variability ratio: n/a")
        print(f"  Mean / downside RMS below 0 (per-trade Sortino-like): {sortino_like:.2f}" if sortino_like is not None else "  Reward:downside-variability ratio: n/a")
        print("  (Higher is better for both; a ratio near or below 0 means the average trade isn't")
        print("   compensating for how much the outcomes actually swing around.)")

    closed_sorted = sorted((t for t in closed if t.get("closed_at")), key=lambda t: t["closed_at"])
    if closed_sorted:
        pot = state.get("starting_pot") or 1
        cum, peak, max_dd = 0.0, 0.0, 0.0
        for t in closed_sorted:
            cum += t["pnl"]
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        print(f"  Max drawdown of cumulative realized P&L: -{max_dd:.2f} ({-max_dd / pot * 100:.1f}% of starting pot)")
        print("  (this tracks only realized/closed P&L over time, not full portfolio equity - open")
        print("   positions and cash timing aren't reflected, so this is not a bound on full portfolio drawdown)")

    by_reason = defaultdict(list)
    for t in closed:
        by_reason[t["reason"]].append(t["pnl"])
    print("\n  By exit reason:")
    for reason, pnls in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"    {reason:<55} n={len(pnls):<4} total={sum(pnls):+7.2f}  avg={sum(pnls)/len(pnls):+6.2f}")
    if any(r.startswith("unresolved:") for r in by_reason):
        print("    ('unresolved' closes are the bot's stated valuation policy for tokens with no indexed listing")
        print("     for --unresolved-close-hours; they are carried at zero, not verified as rugs)")

    by_bucket = defaultdict(list)
    for t in closed:
        b = bucket_for(t.get("score"))
        if b:
            by_bucket[b].append(t["pnl"])
    print("\n  By entry risk-score bucket:")
    for _, _, label in BUCKETS:
        pnls = by_bucket.get(label, [])
        if not pnls:
            continue
        wins_b = sum(1 for p in pnls if p > 0)
        print(f"    {label:<20} n={len(pnls):<4} win_rate={wins_b/len(pnls)*100:5.1f}%  total_pnl={sum(pnls):+7.2f}")

    drawdowns = [t["max_drawdown_pct"] for t in closed if t.get("max_drawdown_pct") is not None]
    runups = [t["max_runup_pct"] for t in closed if t.get("max_runup_pct") is not None]
    if drawdowns:
        print(f"\n  Median max drawdown while held: {statistics.median(drawdowns):.1f}%")
    if runups:
        print(f"  Median max run-up while held:   {statistics.median(runups):.1f}%")


def utc_time(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def snapshot_key(chain, address, timestamp):
    if not chain or not address or not timestamp:
        return None
    # EVM is case-insensitive; Solana mint identities are case-sensitive.
    return (chain, address.lower() if chain == "base" else address, utc_time(timestamp))


def index_candidates_by_address(candidates):
    """Index exact assessment identities. Ambiguous legacy snapshots stay unmatched."""
    indexed = {}
    for c in candidates:
        keys = [snapshot_key(c.get("chain"), c.get("address"), c.get("timestamp"))]
        if c.get("candidate_id"):
            keys.append(c["candidate_id"])
        for key in keys:
            if key is not None:
                indexed[key] = c if key not in indexed else None
    return indexed


def candidate_for_outcome(outcome, indexed):
    identity = outcome.get("candidate_id")
    if identity:
        return indexed.get(identity)
    key = snapshot_key(outcome.get("chain"), outcome.get("address"), outcome.get("discovered_at"))
    return indexed.get(key)


def rule_fired(candidate, rule_id):
    matches = [c for c in candidate.get("score_components") or []
               if c.get("rule") == rule_id and c.get("severity") != "unknown"]
    if not matches:
        return None
    return any(c.get("severity") != "good" and (c.get("points") or 0) > 0 for c in matches)


def usable_outcomes(outcomes):
    """Censor unknown/late observations; retain explicit historical labels separately."""
    usable = []
    excluded = defaultdict(int)
    for o in outcomes:
        status = o.get("observation_status")
        pct = o.get("price_change_pct")
        if status and status != "observed":
            excluded[status] += 1
        elif o.get("rugged_or_delisted") is True:
            usable.append(o)  # legacy heuristic labels, not independently verified
        elif not isinstance(pct, (int, float)) or not math.isfinite(pct) or abs(pct) > SANE_PCT_CHANGE_BOUND:
            excluded["invalid_or_missing_return"] += 1
        else:
            usable.append(o)
    return usable, dict(excluded)


def growth_indicators(outcomes, candidates_by_address, win_threshold=50, min_n=15):
    section(f"5. What do the real winners (>= +{win_threshold}% by checkpoint) have in common?")
    winners, rest = [], []
    for o in outcomes:
        if o.get("rugged_or_delisted"):
            continue
        pct = o.get("price_change_pct")
        if pct is None or abs(pct) > SANE_PCT_CHANGE_BOUND:
            continue
        cand = candidate_for_outcome(o, candidates_by_address)
        if cand is None:
            continue
        (winners if pct >= win_threshold else rest).append((o, cand))

    print(f"  {len(winners)} winner(s), {len(rest)} non-winner(s) matched against discovery-time features")
    if len(winners) < min_n:
        print(f"  Fewer than {min_n} winners so far - too little data to say anything reliable yet.")
        return

    def num_stats(getter, label, unit=""):
        w_vals = [getter(c) for _, c in winners if getter(c) is not None]
        r_vals = [getter(c) for _, c in rest if getter(c) is not None]
        if not w_vals or not r_vals:
            return
        print(f"  {label:<22} winners median={statistics.median(w_vals):>10.2f}{unit}   "
              f"rest median={statistics.median(r_vals):>10.2f}{unit}")

    print("\n  Discovery-time metrics (winners vs. everyone else):")
    num_stats(lambda c: c.get("liquidity_usd"), "Liquidity")
    num_stats(lambda c: c.get("volume_24h"), "24h volume")
    num_stats(lambda c: c.get("volatility_pct"), "Volatility (5m/1h/6h/24h max)", "%")

    def age_hours(c):
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(c["timestamp"])
            created = datetime.fromtimestamp(c["created_ms"] / 1000, tz=ts.tzinfo)
            return (ts - created).total_seconds() / 3600
        except Exception:
            return None
    num_stats(age_hours, "Pool age at discovery", "h")

    print("\n  Score-component presence (winners vs. everyone else):")
    all_rules = set()
    for _, c in winners + rest:
        for comp in c.get("score_components") or []:
            all_rules.add(comp.get("rule"))
    for rule in sorted(all_rules):
        w_fired = [rule_fired(c, rule) for _, c in winners]
        r_fired = [rule_fired(c, rule) for _, c in rest]
        w_fired = [v for v in w_fired if v is not None]
        r_fired = [v for v in r_fired if v is not None]
        if len(w_fired) < min_n or len(r_fired) < min_n:
            continue
        w_pct = sum(w_fired) / len(w_fired) * 100
        r_pct = sum(r_fired) / len(r_fired) * 100
        if abs(w_pct - r_pct) < 5:
            continue  # not different enough to be worth printing
        print(f"    {rule:<24} fired in {w_pct:5.1f}% of winners vs {r_pct:5.1f}% of the rest")
    print("\n  Read with real caution: this describes what past winners happened to look like, not a")
    print("  formula for finding the next one. Survivorship and small-sample noise both bite hard here.")


def cohort_match(row, chain=None, analyzer_version=None, paperbot_version=None):
    return ((chain is None or row.get("chain") == chain)
            and (analyzer_version is None or row.get("analyzer_version") == analyzer_version)
            and (paperbot_version is None or row.get("paperbot_version") == paperbot_version))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", default="paperbot_candidates.jsonl")
    ap.add_argument("--outcomes", default="paperbot_outcomes.jsonl")
    ap.add_argument("--state", default="paperbot_state.json")
    ap.add_argument("--checkpoint-hours", type=int, default=24, help="Which outcome checkpoint to analyze (1, 6, or 24). Default: 24.")
    ap.add_argument("--since", default=None,
                    help="Only analyse rows stamped at/after this UTC time, e.g. 2026-09-03T19:30. Use it to read "
                         "only data produced after a bot fix, instead of blending it with the period before.")
    ap.add_argument("--show-all-clusters", action="store_true", help="List every excluded outage cluster instead of the 8 largest.")
    ap.add_argument("--win-threshold", type=float, default=50, help="%% gain at the checkpoint that counts as a 'winner' for the growth-indicators section. Default: 50.")
    ap.add_argument("--chain", choices=["solana", "base"], help="Restrict all sections to one chain.")
    ap.add_argument("--analyzer-version", help="Restrict to this assessment engine version.")
    ap.add_argument("--paperbot-version", help="Restrict to this entry/discovery bot version.")
    ap.add_argument("--until", help="Exclusive UTC end; both entry/discovery and observation/close must precede it.")
    args = ap.parse_args()
    try:
        lower = utc_time(args.since) if args.since else None
        upper = utc_time(args.until) if args.until else None
        if lower and upper and upper <= lower:
            raise ValueError("until must be later than since")
    except ValueError as exc:
        ap.error(str(exc))
    def selected(row):
        return cohort_match(row, args.chain, args.analyzer_version, args.paperbot_version)

    since = args.since
    if since and len(since) == 10:
        since += "T00:00"  # a bare date means midnight UTC

    def after_since(ts):
        return (not lower or bool(ts and utc_time(ts) >= lower)) and (not upper or bool(ts and utc_time(ts) < upper))

    if since:
        print(f"Only rows stamped at/after {since} UTC are included (--since).")

    print(f"Cohort: chain={args.chain or 'all'}, analyzer={args.analyzer_version or 'all'}, bot={args.paperbot_version or 'all'}, until={args.until or 'none'}")
    candidates = []
    try:
        candidates = [c for c in load_jsonl(args.candidates) if selected(c) and after_since(c.get("timestamp"))]
        check_config_drift(candidates)
    except FileNotFoundError:
        print(f"(skipping config-drift check - {args.candidates} not found)")

    print(f"\nLoading outcomes from {args.outcomes} (checkpoint = {args.checkpoint_hours}h)...")
    all_outcomes = [o for o in load_jsonl(args.outcomes)
                    if selected(o) and after_since(o.get("checked_at")) and after_since(o.get("discovered_at"))]
    raw_total = len(all_outcomes)
    raw_rows = list(all_outcomes)
    all_outcomes, censored = usable_outcomes(all_outcomes)
    print(f"  Censored observations (not classified as clean or rugged): {censored}")
    print("  Legacy rug labels and cluster exclusions are heuristic, not verified outcomes.")
    all_outcomes, cluster_excluded, cluster_groups, reinstated = filter_outage_clusters(all_outcomes)
    if reinstated:
        print(f"  ({reinstated} rugged row(s) shared a second with successfully priced rows - retained by the heuristic; not independently verified)")
    if cluster_excluded:
        print(f"  WARNING: excluded {cluster_excluded} row(s) across {len(cluster_groups)} timestamp(s) - almost")
        print(f"  certainly Dexscreener outage/rate-limit blips misread as simultaneous rugs, not real outcomes")
        print(f"  (same root cause already found and fixed in closed-trade data; rugged% figures in any earlier")
        print(f"  run of this script were inflated by this).")
        # Hundreds of clusters is normal once rate-limiting is involved; listing
        # every one buries the actual report. Show the biggest few (the SOL/WETH
        # ones that prove the point) and summarise the rest unless asked.
        ordered = sorted(cluster_groups.items(), key=lambda kv: len(kv[1]), reverse=True)
        show = ordered if args.show_all_clusters else ordered[:8]
        for ts, group in show:
            symbols = [o.get("symbol") or "?" for o in group]
            shown = ", ".join(symbols[:15]) + (f", ... +{len(symbols) - 15} more" if len(symbols) > 15 else "")
            print(f"    EXCLUDED: {len(symbols)} coins simultaneously 'rugged' at {ts}: {shown}")
        hidden = ordered[len(show):]
        if hidden:
            print(f"    ... and {len(hidden)} smaller cluster(s) totalling {sum(len(g) for _, g in hidden)} rows "
                  f"(run with --show-all-clusters to list them)")
    outcomes = [o for o in all_outcomes if o.get("checkpoint_hours") == args.checkpoint_hours]
    print(f"  {raw_total} total checkpoint rows logged, {len(outcomes)} at the {args.checkpoint_hours}h horizon "
          f"(after outage-cluster filtering)")
    horizon_raw = [o for o in raw_rows if o.get("checkpoint_hours") == args.checkpoint_hours]
    observation_status_by_bucket(horizon_raw)

    candidates_by_address = index_candidates_by_address(candidates) if candidates else {}

    matches = sum(candidate_for_outcome(o, candidates_by_address) is not None for o in outcomes)
    print(f"  Exact assessment joins: {matches}/{len(outcomes)}; unmatched rows excluded from feature analysis.")
    print("  Return statistics describe priced observations only; unpriced cases are censored.")
    print("  Cohort coverage depends on version and tracking flags; bought fixed horizons were added in v1.5.0.")
    score_bucket_vs_outcome(outcomes)
    if candidates:
        rule_level_analysis(outcomes, candidates_by_address)
        rule_level_delisting(horizon_raw, candidates_by_address)
    else:
        print("\n(skipping per-rule section - no candidates loaded to join against)")
    confidence_vs_outcome(outcomes)

    if candidates:
        growth_indicators(outcomes, candidates_by_address, win_threshold=args.win_threshold)
    else:
        print("\n(skipping growth-indicators section - no candidates loaded)")

    try:
        with open(args.state, encoding="utf-8") as f:
            state = json.load(f)
        state["closed_trades"] = [t for t in state.get("closed_trades", []) if selected(t) and after_since(t.get("closed_at")) and after_since(t.get("opened_at"))]
        closed_trade_performance(state)
    except FileNotFoundError:
        print(f"\n(skipping closed-trade section - {args.state} not found)")

    print()


if __name__ == "__main__":
    main()
