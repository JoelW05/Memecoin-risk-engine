#!/usr/bin/env python3
"""Re-weighting experiment: do fixed rule weights beat the current scorer on data
they never saw?

Section 2a of analyze_outcomes.py found rules whose weights point the wrong way
(mint/freeze authority mark tokens that SURVIVE). Refitting on all the data and
reporting the fit would be circular, so there are three modes:

SPLIT (default) - chronological holdout.
  1. Joins 24h outcome rows to their exact candidate assessment. Label:
     "no_pairs" = no listing at the horizon (the delisting proxy), "observed" =
     still listed. Every other status (missed, failed, evicted, ...) is
     missingness and is excluded, never counted either way.
  2. Keeps one row per token (its earliest resolved assessment), so a token
     that was assessed repeatedly cannot appear on both sides of the split or
     be counted as several independent observations.
  3. Sorts by assessment time: the first 60% is the derivation set, the last
     40% the holdout. Every choice - which rules are inverted, every weight,
     every scale factor - is made on the derivation set alone.
  4. Re-scores each candidate from its stored score_components under schemes
     A-D and compares them on the holdout only: AUC (Mann-Whitney), the 4-bucket
     delisting gradient, entry eligibility, and a per-chain breakdown.
     A paired bootstrap gives confidence intervals on each AUC difference.

FREEZE (--freeze FILE) - pre-registration. Fits schemes D and E on every
  resolved row available now, writes them with a timestamp and a hash, and
  refuses to overwrite an existing file.

FORWARD (--forward FILE) - the prospective test. Scores A, D and E only on
  tokens first assessed after the freeze, withholds all results until the
  pre-registered sample size is reached, then reports the pre-registered verdict.

"No listing" means Dexscreener no longer indexes the token. It is the nearest
observable proxy for "gone", not a verified on-chain liquidity removal, and it
measures survival, not return. Tokens that cannot be sold (honeypots, frozen
balances) can stay listed, so this label cannot see the risks some rules exist
for; scheme E and the secondary "delisted or down 50%+" label address that.

Pure standard library. Analysis only: it reads the logs and changes nothing.
  python reweight_experiment.py
  python reweight_experiment.py --freeze frozen_weights.json
  python reweight_experiment.py --forward frozen_weights.json
"""

import argparse
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone

from analyze_outcomes import (BUCKETS, SANE_PCT_CHANGE_BOUND, candidate_for_outcome, index_candidates_by_address,
                              load_jsonl, rule_fired, utc_time)

EXPERIMENT_VERSION = "1.1.0"
DEFAULT_SINCE = "2026-09-07T20:47"  # first run with a working affordability constraint; earlier labels are corrupt
LABELS = {"no_pairs": 1, "observed": 0}
NOT_FIRED = ("good", "unknown")
CRASH_PCT = -50  # secondary label: delisted, or still listed but down at least this much at 24h
FORWARD_MIN_ROWS = 8000  # pre-registered: no forward result is shown below this many resolved tokens
# paperbot.py's defaults, used only if a candidate row predates the config stamp.
DEFAULT_ENTRY = {"max_risk_score": 30, "min_liquidity_usd": 15000, "min_pool_age_hours": 6, "require_socials": True}
# Mutually exclusive bands of one measurement (analyzer.analyze fires at most one per group).
BAND_GROUPS = (("no_liquidity", "liquidity_critical", "liquidity_high", "liquidity_medium"),
               ("pool_age_high", "pool_age_medium"))
BAND_OF = {rule: group for group in BAND_GROUPS for rule in group}
# Rules that guard against risks the delisting label cannot observe: a token that
# cannot be sold, frozen balances, inflatable supply, hidden contract powers, or a
# safety check that could not run. Such tokens can stay listed and even look
# healthy, so in scheme E the label may raise these weights but never lower them.
SAFETY_RULES = frozenset((
    "mint_authority", "freeze_authority", "rugcheck_high", "rugcheck_warn", "rugcheck_unavailable",
    "goplus_unavailable", "honeypot", "honeypot_sim", "honeypot_sim_sell_tax", "honeypot_sim_unavailable",
    "cannot_sell_all", "hidden_owner", "ownership_reclaimable", "selfdestruct", "mintable",
    "buy_tax", "sell_tax", "not_open_source", "ownership_not_renounced",
))
SCHEME_NOTES = {
    "A": "current weights (baseline)",
    "B": "zero the rules that are significantly inverted on the derivation set",
    "C": "points proportional to derivation-set log-lift, negatives set to zero",
    "D": "points from a ridge logistic regression on the fitting data, negatives set to zero",
    "E": "D, except safety rules never drop below their current points",
}
PRIMARY = ("y", "no listing at 24h")
SECONDARY = ("crash", f"no listing, or listed but down {-CRASH_PCT}%+ at 24h")


# ---------------------------------------------------------------- data

def token_key(chain, address):
    # EVM addresses are case-insensitive; Solana mints are case-sensitive.
    return (chain, (address or "").lower() if chain == "base" else address)


def fired_points(candidate):
    """rule -> total stored points of its firing components ({} if nothing fired).

    "Fired" matches analyze_outcomes.rule_fired: a component whose severity is
    neither good nor unknown and which carries points. A rule can fire more than
    once (one rugcheck_high per RugCheck finding), so points are summed."""
    fired = defaultdict(int)
    for comp in candidate.get("score_components") or []:
        if comp.get("rule") and comp.get("severity") not in NOT_FIRED and (comp.get("points") or 0) > 0:
            fired[comp["rule"]] += comp["points"]
    return dict(fired)


def evaluated_rules(candidate):
    """rule -> True (fired) / False (clean), for every rule with a known result.
    Rules whose only components are 'unknown' are absent: missing data is
    neither a firing nor a clean check."""
    out = {}
    for comp in candidate.get("score_components") or []:
        rule = comp.get("rule")
        if rule and rule not in out:
            hit = rule_fired(candidate, rule)
            if hit is not None:
                out[rule] = hit
    return out


def crash_label(delisted, pct):
    """Secondary label. None when a still-listed token has no believable return:
    a missing or implausible quote is missing data, not a crash and not a survivor."""
    if delisted:
        return 1
    if not isinstance(pct, (int, float)) or not math.isfinite(pct) or abs(pct) > SANE_PCT_CHANGE_BOUND:
        return None
    return 1 if pct <= CRASH_PCT else 0


def load_dataset(candidates_path, outcomes_path, since=DEFAULT_SINCE, until=None, checkpoint_hours=24,
                 chain=None, one_per_token=True):
    """Return (records, audit). Each record is one resolved, joined observation."""
    lower = utc_time(since) if since else None
    upper = utc_time(until) if until else None

    def in_window(ts):
        if not ts:
            return False
        t = utc_time(ts)
        return (lower is None or t >= lower) and (upper is None or t < upper)

    candidates = [c for c in load_jsonl(candidates_path)
                  if in_window(c.get("timestamp")) and (chain is None or c.get("chain") == chain)]
    indexed = index_candidates_by_address(candidates)

    audit = Counter()
    seen_candidates = set()
    records = []
    for o in load_jsonl(outcomes_path):
        if o.get("checkpoint_hours") != checkpoint_hours:
            continue
        if chain is not None and o.get("chain") != chain:
            continue
        if not (in_window(o.get("discovered_at")) and in_window(o.get("checked_at"))):
            continue
        audit["horizon_rows"] += 1
        status = o.get("observation_status")
        if status not in LABELS:
            audit[f"excluded:{status}"] += 1
            continue
        cand = candidate_for_outcome(o, indexed)
        if cand is None:
            audit["unmatched"] += 1
            continue
        ident = cand.get("candidate_id") or id(cand)
        if ident in seen_candidates:
            audit["duplicate_outcome_rows"] += 1
            continue
        seen_candidates.add(ident)
        fired = fired_points(cand)
        stored = cand.get("score")
        records.append({
            "ts": utc_time(cand["timestamp"]),
            "chain": cand.get("chain"),
            "token": token_key(cand.get("chain"), cand.get("address")),
            "y": LABELS[status],
            "crash": crash_label(LABELS[status], o.get("price_change_pct")),
            "stored_score": stored,
            "fired": fired,
            "evaluated": evaluated_rules(cand),
            "rebuilt_ok": isinstance(stored, (int, float)) and min(100, sum(fired.values())) == stored,
            "analyzer_version": cand.get("analyzer_version"),
            "candidate": cand,
        })
    audit["resolved_joined"] = len(records)
    records.sort(key=lambda r: r["ts"])
    if one_per_token:
        kept, tokens = [], set()
        for r in records:
            if r["token"] in tokens:
                audit["repeat_assessments_dropped"] += 1
                continue
            tokens.add(r["token"])
            kept.append(r)
        records = kept
    audit["rows_used"] = len(records)
    return records, audit


def chronological_split(records, frac=0.6):
    """First `frac` of the time-ordered records derive; the rest are held out.
    Any holdout token also present in the derivation set is purged from the
    holdout (only possible when repeat assessments are kept)."""
    records = sorted(records, key=lambda r: r["ts"])
    cut = int(round(len(records) * frac))
    derivation, holdout = records[:cut], records[cut:]
    seen = {r["token"] for r in derivation}
    purged = [r for r in holdout if r["token"] not in seen]
    return derivation, purged, len(holdout) - len(purged)


# ---------------------------------------------------------------- scoring

def score(record, weights):
    """Re-score from stored components. A rule named in `weights` contributes
    that many points once if it fired; any other fired rule keeps its stored
    points. So weights={} rebuilds the stored score exactly."""
    total = 0
    for rule, pts in record["fired"].items():
        total += weights[rule] if rule in weights else pts
    return max(0, min(100, int(round(total))))


def bucket_index(s):
    for i, (lo, hi, _) in enumerate(BUCKETS):
        if lo <= s <= hi:
            return i
    return None


def typical_points(records):
    """rule -> the most common stored points when it fires (its current weight)."""
    seen = defaultdict(Counter)
    for r in records:
        for rule, pts in r["fired"].items():
            seen[rule][pts] += 1
    return {rule: c.most_common(1)[0][0] for rule, c in seen.items()}


# ---------------------------------------------------------------- statistics

def auc_from_counts(pos, neg):
    """AUC = P(score of a positive > score of a negative), ties count half.
    `pos`/`neg` map score -> count (or weight). Equals Mann-Whitney U / (P*N)."""
    total_pos, total_neg = sum(pos.values()), sum(neg.values())
    if not total_pos or not total_neg:
        return None
    below = 0.0
    acc = 0.0
    for s in sorted(set(pos) | set(neg)):
        p, n = pos.get(s, 0), neg.get(s, 0)
        acc += p * (below + 0.5 * n)
        below += n
    return acc / (total_pos * total_neg)


def auc(scores, labels):
    pos, neg = Counter(), Counter()
    for s, y in zip(scores, labels):
        (pos if y else neg)[s] += 1
    return auc_from_counts(pos, neg)


def paired_bootstrap(score_table, labels, baseline, others, reps=1000, seed=0, alphas=(0.05,)):
    """Resample holdout rows with replacement (same rows for every scheme) and
    return {scheme: {alpha: (lo, hi)}} percentile intervals on AUC(scheme) - AUC(baseline).

    Rows with identical (scores, label) are grouped first; drawing a group with
    probability count/n is the same as drawing a row uniformly, so this is the
    ordinary nonparametric bootstrap, just faster."""
    names = [baseline] + list(others)
    cells = Counter(tuple(score_table[k][i] for k in names) + (labels[i],) for i in range(len(labels)))
    keys = list(cells)
    cum, running = [], 0
    for k in keys:
        running += cells[k]
        cum.append(running)
    n = running
    rng = random.Random(seed)
    diffs = {k: [] for k in others}
    idx_range = range(len(keys))
    for _ in range(reps):
        draw = Counter(rng.choices(idx_range, cum_weights=cum, k=n))
        per = [(Counter(), Counter()) for _ in names]
        for ci, count in draw.items():
            cell = keys[ci]
            y = cell[-1]
            for j in range(len(names)):
                per[j][0 if y else 1][cell[j]] += count
        base = auc_from_counts(*per[0])
        for j, k in enumerate(others, start=1):
            a = auc_from_counts(*per[j])
            if base is not None and a is not None:
                diffs[k].append(a - base)
    out = {}
    for k, d in diffs.items():
        d.sort()
        out[k] = {}
        for alpha in alphas:
            if not d:
                out[k][alpha] = (None, None)
                continue
            lo = d[max(0, int(math.floor(alpha / 2 * len(d))))]
            hi = d[min(len(d) - 1, int(math.ceil((1 - alpha / 2) * len(d))) - 1)]
            out[k][alpha] = (lo, hi)
    return out


def rule_counts(records):
    """rule -> [fired_delisted, fired_n, clean_delisted, clean_n].

    Like section 2a, except for banded rules: a band's reference group is rows
    where NO band of its group fired. Under 2a's definition the "clean" side of
    liquidity_medium includes the critical and high bands, so a genuinely risky
    band looks protective - the planted-signal test caught that zeroing a
    correctly signed weight."""
    stats = defaultdict(lambda: [0, 0, 0, 0])
    for r in records:
        for rule, hit in r["evaluated"].items():
            group = BAND_OF.get(rule)
            if not hit and group and any(other in r["fired"] for other in group):
                continue  # a worse or better band fired: not a clean reference row
            s = stats[rule]
            if hit:
                s[0] += r["y"]
                s[1] += 1
            else:
                s[2] += r["y"]
                s[3] += 1
    return dict(stats)


def log_risk_ratio(fd, fn, cd, cn):
    """ln(delisted% fired / delisted% clean) with a 0.5 continuity correction, and its SE."""
    a, n1, c, n2 = fd + 0.5, fn + 0.5, cd + 0.5, cn + 0.5
    lrr = math.log((a / n1) / (c / n2))
    se = math.sqrt(max(0.0, 1 / a - 1 / n1 + 1 / c - 1 / n2))
    return lrr, se


def _solve(matrix, vector):
    """Gaussian elimination with partial pivoting (the system is small and SPD)."""
    n = len(vector)
    m = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError("singular system in logistic fit")
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def fit_logistic(records, features, ridge=1.0, iterations=50, tol=1e-8):
    """Ridge logistic regression of delisted on fired-rule indicators (Newton/IRLS).
    Rows are grouped by their fired-feature pattern, which makes this fast.
    Returns (intercept, {feature: coefficient})."""
    fidx = {f: i + 1 for i, f in enumerate(features)}
    groups = defaultdict(lambda: [0, 0])  # pattern -> [n, delisted]
    for r in records:
        pattern = tuple(sorted(fidx[f] for f in r["fired"] if f in fidx))
        groups[pattern][0] += 1
        groups[pattern][1] += r["y"]
    p = len(features) + 1
    beta = [0.0] * p
    for _ in range(iterations):
        grad = [0.0] * p
        hess = [[0.0] * p for _ in range(p)]
        for pattern, (n, d) in groups.items():
            cols = (0,) + pattern
            eta = sum(beta[c] for c in cols)
            prob = 1 / (1 + math.exp(-eta)) if eta >= 0 else math.exp(eta) / (1 + math.exp(eta))
            w = n * prob * (1 - prob)
            g = d - n * prob
            for a in cols:
                grad[a] += g
                row = hess[a]
                for b in cols:
                    row[b] += w
        for j in range(1, p):  # intercept is not penalised
            grad[j] -= ridge * beta[j]
            hess[j][j] += ridge
        hess[0][0] += 1e-9
        step = _solve(hess, grad)
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < tol:
            break
    return beta[0], {f: beta[i] for f, i in fidx.items()}


def calibrate_scale(records, raw_weights, target_fraction, threshold):
    """Pick the multiplier k so that the share of derivation rows scoring <= threshold
    under round(k * raw weight) points matches the baseline's share. That holds the
    number of buyable candidates roughly fixed, so eligibility is compared like for
    like. AUC does not depend on k except through rounding and the 100 cap."""
    def fraction(k):
        pts = {rule: round(k * w) for rule, w in raw_weights.items()}
        return sum(score(r, pts) <= threshold for r in records) / len(records)

    lo, hi = 0.01, 1000.0
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        if fraction(mid) > target_fraction:
            lo = mid
        else:
            hi = mid
    k = min((lo, hi), key=lambda v: abs(fraction(v) - target_fraction))
    return k, {rule: round(k * w) for rule, w in raw_weights.items()}


# ---------------------------------------------------------------- schemes

def safety_floor(weights_d, current):
    """Scheme E: D's points, except a safety rule whose D points would fall to or
    below its current points is left out, so it keeps exactly today's scoring
    (including multi-finding rules such as rugcheck_high)."""
    return {rule: pts for rule, pts in weights_d.items()
            if rule not in SAFETY_RULES or pts > current.get(rule, 0)}


def derive_schemes(derivation, min_n=50, ridge=1.0, z=1.96):
    """Fix schemes B-E from the given rows only. Rules without min_n rows on both
    sides keep their current points everywhere."""
    counts = rule_counts(derivation)
    testable = sorted(r for r, (fd, fn, cd, cn) in counts.items() if fn >= min_n and cn >= min_n)
    lrr = {r: log_risk_ratio(*counts[r]) for r in testable}
    inverted = sorted(r for r in testable if lrr[r][0] + z * lrr[r][1] < 0)

    threshold = DEFAULT_ENTRY["max_risk_score"]
    base_frac = sum(score(r, {}) <= threshold for r in derivation) / len(derivation)
    k_c, weights_c = calibrate_scale(derivation, {r: max(0.0, lrr[r][0]) for r in testable}, base_frac, threshold)
    intercept, coefs = fit_logistic(derivation, testable, ridge=ridge)
    k_d, weights_d = calibrate_scale(derivation, {r: max(0.0, coefs[r]) for r in testable}, base_frac, threshold)
    current = typical_points(derivation)
    return {
        "schemes": {"A": {}, "B": {r: 0 for r in inverted}, "C": weights_c, "D": weights_d,
                    "E": safety_floor(weights_d, current)},
        "counts": counts, "testable": testable, "log_rr": lrr, "inverted": inverted, "current": current,
        "logistic": (intercept, coefs), "scale": {"C": k_c, "D": k_d}, "baseline_eligible_fraction": base_frac,
    }


# ---------------------------------------------------------------- evaluation

def passes_static_entry_gates(record):
    """The bot's non-score entry gates that are decidable from the candidate row
    alone (liquidity, pool age at assessment, socials). Portfolio gates - open
    positions, cash, per-cycle budget - are not modelled."""
    cand = record["candidate"]
    cfg = {**DEFAULT_ENTRY, **{k: v for k, v in (cand.get("config") or {}).items() if v is not None}}
    liq = cand.get("liquidity_usd")
    if liq is None or liq < cfg["min_liquidity_usd"]:
        return False
    created = cand.get("created_ms")
    if created is None:
        return False
    age_h = (record["ts"].timestamp() * 1000 - created) / 3_600_000
    if age_h < cfg["min_pool_age_hours"]:
        return False
    if cfg["require_socials"] and "no_socials" in record["fired"]:
        return False
    return True


def max_score_for(record):
    cfg = record["candidate"].get("config") or {}
    return cfg.get("max_risk_score", DEFAULT_ENTRY["max_risk_score"])


def rate(ys):
    return (sum(ys) / len(ys) * 100) if ys else None


def evaluate(holdout, schemes, reps=1000, seed=0, n_tests=None, label="y"):
    """Compare schemes on `holdout` for one label; the first scheme is the baseline.
    Rows whose label is unknown (None) are left out of this label's analysis."""
    holdout = [r for r in holdout if r[label] is not None]
    names = list(schemes)
    labels = [r[label] for r in holdout]
    table = {k: [score(r, w) for r in holdout] for k, w in schemes.items()}
    n_tests = n_tests or max(1, len(names) - 1)
    alphas = (0.05, 0.05 / n_tests)
    result = {"n": len(holdout), "delisted_pct": rate(labels), "alphas": alphas,
              "auc": {k: auc(table[k], labels) for k in names}}
    result["ci"] = paired_bootstrap(table, labels, names[0], names[1:], reps=reps, seed=seed, alphas=alphas)

    result["buckets"] = {}
    for k in names:
        rows = [[] for _ in BUCKETS]
        for s, y in zip(table[k], labels):
            rows[bucket_index(s)].append(y)
        result["buckets"][k] = [(len(b), rate(b)) for b in rows]

    static_ok = [passes_static_entry_gates(r) for r in holdout]
    limits = [max_score_for(r) for r in holdout]
    result["eligibility"] = {}
    for gated in (False, True):
        elig = {k: [s <= lim and (ok or not gated) for s, lim, ok in zip(table[k], limits, static_ok)] for k in names}
        base = elig[names[0]]
        per = {}
        for k in names:
            e = elig[k]
            per[k] = {
                "eligible": (sum(e), rate([y for y, x in zip(labels, e) if x])),
                "gained": (sum(x and not b for x, b in zip(e, base)),
                           rate([y for y, x, b in zip(labels, e, base) if x and not b])),
                "lost": (sum(b and not x for x, b in zip(e, base)),
                         rate([y for y, x, b in zip(labels, e, base) if b and not x])),
            }
        result["eligibility"]["score+static gates" if gated else "score gate only"] = per

    result["by_chain"] = {}
    for chain in sorted({r["chain"] for r in holdout}):
        idx = [i for i, r in enumerate(holdout) if r["chain"] == chain]
        sub_labels = [labels[i] for i in idx]
        if not any(sub_labels) or all(sub_labels):
            continue
        sub = {k: [table[k][i] for i in idx] for k in names}
        result["by_chain"][chain] = {
            "n": len(idx), "delisted_pct": rate(sub_labels),
            "auc": {k: auc(sub[k], sub_labels) for k in names},
            "ci": paired_bootstrap(sub, sub_labels, names[0], names[1:], reps=reps, seed=seed + 1, alphas=alphas),
        }
    return result


def verdict(result, names, alpha):
    """A scheme qualifies only if its adjusted CI on the AUC gain excludes zero overall
    and no chain gets significantly worse at the unadjusted level."""
    winners = []
    for k in names[1:]:
        lo, _ = result["ci"][k][alpha]
        if lo is None or lo <= 0:
            continue
        hurt = [c for c, ch in result["by_chain"].items()
                if (ch["ci"][k][0.05][1] is not None and ch["ci"][k][0.05][1] < 0)]
        if not hurt:
            winners.append(k)
    return winners


def forward_verdict(primary, secondary, names):
    """Pre-registered rule: a scheme passes if it passes verdict() on the primary
    label AND is not significantly worse than the baseline on the secondary label."""
    passed = []
    for k in verdict(primary, names, primary["alphas"][1]):
        hi = secondary["ci"][k][0.05][1]
        if hi is None or hi >= 0:
            passed.append(k)
    return passed


# ---------------------------------------------------------------- freeze / forward

def schemes_hash(schemes):
    return hashlib.sha256(json.dumps(schemes, sort_keys=True).encode("utf-8")).hexdigest()


def build_frozen(records, derived, since, frozen_at, min_n=50, ridge=1.0):
    """Package D and E as fitted by derive_schemes on every row resolved before the freeze."""
    schemes = {k: derived["schemes"][k] for k in ("A", "D", "E")}
    versions = Counter(r["analyzer_version"] for r in records)
    return {
        "experiment_version": EXPERIMENT_VERSION,
        "frozen_at": frozen_at,
        "fitted_on": {"since": since, "first_assessment": records[0]["ts"].isoformat(timespec="seconds"),
                      "last_assessment": records[-1]["ts"].isoformat(timespec="seconds"), "rows": len(records),
                      "delisted_pct": round(rate([r["y"] for r in records]), 2)},
        "analyzer_version": versions.most_common(1)[0][0],
        "primary": f"AUC for '{PRIMARY[1]}', E vs A; D reported alongside",
        "secondary": f"AUC for '{SECONDARY[1]}'; a passing scheme must not be significantly worse here",
        "rule": "passes if the Bonferroni-adjusted 95% CI (2 comparisons) on the primary AUC gain is above zero, "
                "no chain is significantly worse, and the secondary 95% CI is not entirely below zero",
        "min_rows": FORWARD_MIN_ROWS,
        "safety_rules": sorted(SAFETY_RULES),
        "current_points": derived["current"],
        "logistic": {"intercept": derived["logistic"][0], "coefficients": derived["logistic"][1],
                     "scale": derived["scale"]["D"], "ridge": ridge, "min_n": min_n},
        "schemes": schemes,
        "sha256": schemes_hash(schemes),
    }


def write_frozen(path, frozen):
    """Exclusive create: a pre-registration must never be silently redone."""
    with open(path, "x", encoding="utf-8") as f:
        json.dump(frozen, f, indent=2, sort_keys=True)
        f.write("\n")


def load_frozen(path):
    with open(path, encoding="utf-8") as f:
        frozen = json.load(f)
    if schemes_hash(frozen["schemes"]) != frozen.get("sha256"):
        raise ValueError(f"{path}: weights do not match their recorded hash - the file was edited after freezing.")
    return frozen


def forward_start(frozen, since=None):
    """The forward window opens at the freeze, and may not be moved earlier."""
    start = utc_time(frozen["frozen_at"])
    if since and utc_time(since) < start:
        raise ValueError(f"--since {since} is before the freeze ({frozen['frozen_at']}); "
                         "data from before the freeze is not a forward test.")
    return utc_time(since) if since else start


def forward_records(records, frozen, start):
    """Tokens whose EARLIEST resolved assessment is at/after the start, scored by the
    frozen analyzer version. `records` must be deduplicated over the whole period
    since fitting began, so a token already seen before the freeze never counts."""
    rows = [r for r in records if r["ts"] >= start]
    same = [r for r in rows if r["analyzer_version"] == frozen["analyzer_version"]]
    return same, len(rows) - len(same)


# ---------------------------------------------------------------- report

def pct(v):
    return "  n/a" if v is None else f"{v:5.1f}%"


def signed(v):
    return "  n/a " if v is None else f"{v:+.3f}"


def print_data_audit(records, audit):
    excluded = {k.split(':', 1)[1]: v for k, v in audit.items() if k.startswith("excluded:")}
    print(f"  {audit['horizon_rows']} rows at horizon; excluded as missingness (not outcomes): {excluded or 'none'}")
    print(f"  {audit['resolved_joined']} resolved + joined ({audit['unmatched']} unmatched, "
          f"{audit['duplicate_outcome_rows']} duplicate rows); "
          f"{audit['repeat_assessments_dropped']} repeat assessments of a token dropped -> {audit['rows_used']} rows")
    rebuilt = sum(r["rebuilt_ok"] for r in records)
    flag = "" if rebuilt == len(records) else "   <-- WARNING: stored scores not reproducible from components"
    print(f"  Baseline rebuilt from stored components: {rebuilt}/{len(records)} exact{flag}")


def print_auc_table(result, names, title):
    alpha, adj = result["alphas"]
    print(f"\n{title} (n={result['n']}, positive {pct(result['delisted_pct'])})")
    print(f"  {'Scheme':<7}{'AUC':>7}{'dAUC vs A':>11}{'95% CI':>20}{f'{1 - adj:.1%} CI':>20}")
    for k in names:
        line = f"  {k:<7}{result['auc'][k]:>7.3f}"
        if k != names[0]:
            d = result["auc"][k] - result["auc"][names[0]]
            ci, ci_adj = result["ci"][k][alpha], result["ci"][k][adj]
            line += f"{signed(d):>11}{f'[{signed(ci[0])}, {signed(ci[1])}]':>20}{f'[{signed(ci_adj[0])}, {signed(ci_adj[1])}]':>20}"
        print(line)


def print_buckets(result, names, what):
    print(f"\n  {what} by score bucket:")
    print(f"  {'Scheme':<7}" + "".join(f"{label.split('(')[1].rstrip(')'):>16}" for _, _, label in BUCKETS))
    for k in names:
        print(f"  {k:<7}" + "".join(f"{pct(r)} ({n:>5})".rjust(16) for n, r in result["buckets"][k]))


def print_eligibility(result, names, what, gates=None):
    for gate, per in result["eligibility"].items():
        if gates and gate not in gates:
            continue
        print(f"\n  Entry eligibility, {gate} ({what}):")
        print(f"  {'Scheme':<7}{'eligible':>9}{'rate':>9}  |{'newly buyable':>14}{'rate':>9}  |{'newly blocked':>14}{'rate':>9}")
        for k in names:
            e, g, l = per[k]["eligible"], per[k]["gained"], per[k]["lost"]
            print(f"  {k:<7}{e[0]:>9}{pct(e[1]):>9}  |{g[0]:>14}{pct(g[1]):>9}  |{l[0]:>14}{pct(l[1]):>9}")


def print_chains(result, names):
    alpha = result["alphas"][0]
    print("\n  By chain: AUC, and dAUC vs A with 95% CI")
    for chain, ch in result["by_chain"].items():
        parts = [f"A {ch['auc'][names[0]]:.3f}"]
        for k in names[1:]:
            lo, hi = ch["ci"][k][alpha]
            parts.append(f"{k} {signed(ch['auc'][k] - ch['auc'][names[0]])} [{signed(lo)},{signed(hi)}]")
        print(f"  {chain:<7} n={ch['n']:>6} positive {pct(ch['delisted_pct'])}  " + "  ".join(parts))


def print_weights(derived, records, names):
    counts, coefs = derived["counts"], derived["logistic"][1]
    print(f"\n  {'Rule':<24}{'fired n':>8}{'lift':>7}{'logit':>7} |" + "".join(f"{k:>4}" for k in names))
    for rule in sorted(derived["testable"], key=lambda r: -derived["log_rr"][r][0]):
        a_pts = derived["current"].get(rule, 0)
        pts = [derived["schemes"][k].get(rule, a_pts) for k in names]
        print(f"  {rule:<24}{counts[rule][1]:>8}{math.exp(derived['log_rr'][rule][0]):>6.2f}x{coefs[rule]:>+7.2f} |"
              + "".join(f"{p:>4}" for p in pts))
    others = sorted({rule for r in records for rule in r["fired"]} - set(derived["testable"]))
    if others:
        print(f"  Too rare to re-weight (keep current points): {', '.join(others)}")


def print_report(records, audit, derivation, holdout, purged, derived, result, args):
    names = list(result["auc"])
    print(f"Re-weighting experiment v{EXPERIMENT_VERSION} - chronological holdout, analysis only")
    print(f"Window: {args.since or 'all'} -> {args.until or 'now'} UTC | {args.checkpoint_hours}h checkpoint | chain: {args.chain or 'all'}")
    print_data_audit(records, audit)
    print(f"Split {args.split:.0%}/{1 - args.split:.0%} by assessment time:")
    for label, part in (("derivation", derivation), ("holdout", holdout)):
        if part:
            print(f"  {label:<10} n={len(part):>6}  {part[0]['ts']:%d %b %H:%M} -> {part[-1]['ts']:%d %b %H:%M}"
                  f"  delisted {pct(rate([r['y'] for r in part]))}")
    if purged:
        print(f"  ({purged} holdout rows purged: token also in derivation set)")

    print("\nSchemes, fixed on the derivation set only:")
    for k in names:
        print(f"  {k}  {SCHEME_NOTES[k]}")
    print(f"  B zeroes: {', '.join(derived['inverted']) or '(none - no rule significantly inverted)'}")
    print(f"  C, D scaled so {derived['baseline_eligible_fraction']:.1%} of derivation rows score <= "
          f"{DEFAULT_ENTRY['max_risk_score']}, as under A (k_C={derived['scale']['C']:.1f}, k_D={derived['scale']['D']:.1f})")
    print_weights(derived, derivation, names)

    print_auc_table(result, names, f"HOLDOUT, {PRIMARY[1]} - paired bootstrap x{args.bootstrap}; "
                                   f"adjusted CI covers {len(names) - 1} comparisons")
    print_buckets(result, names, "Delisted at 24h (holdout)")
    print_eligibility(result, names, "holdout, rate = delisted")
    print_chains(result, names)

    winners = verdict(result, names, result["alphas"][1])
    print("\nVERDICT")
    if winners:
        print(f"  Beats the baseline on the holdout (adjusted CI above zero, no chain significantly worse): {', '.join(winners)}")
        print("  This supports a deploy decision; it does not make it. Deploying changes analyzer.py, so bump")
        print("  ANALYZER_VERSION and judge the new weights again on data collected after the change.")
    else:
        print("  No scheme beats the baseline on the holdout with an adjusted CI excluding zero. Keep current weights.")
    print("  Measures survival (still indexed by Dexscreener at 24h), not return. Rules are correlated;")
    print("  a weight here is a predictive contribution, not the causal effect of the property.")


def run_freeze(args):
    records, audit = load_dataset(args.candidates, args.outcomes, since=args.since, checkpoint_hours=args.checkpoint_hours,
                                  chain=args.chain)
    if len(records) < 1000:
        raise SystemExit(f"Only {len(records)} resolved rows - too few to freeze weights on.")
    derived = derive_schemes(records, min_n=args.min_n, ridge=args.ridge)
    frozen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    frozen = build_frozen(records, derived, args.since, frozen_at, min_n=args.min_n, ridge=args.ridge)
    try:
        write_frozen(args.freeze, frozen)
    except FileExistsError:
        raise SystemExit(f"{args.freeze} already exists. A pre-registration is never redone silently; "
                         "if you really mean to replace it, delete the file yourself first.")
    print(f"Re-weighting experiment v{EXPERIMENT_VERSION} - FREEZE (pre-registration)")
    print_data_audit(records, audit)
    fit = frozen["fitted_on"]
    print(f"  Fitted on {fit['rows']} tokens assessed {fit['first_assessment']} -> {fit['last_assessment']}, "
          f"delisted {fit['delisted_pct']}%, analyzer {frozen['analyzer_version']}")
    print_weights(derived, records, ["A", "D", "E"])
    print(f"\nFROZEN at {frozen_at} UTC -> {args.freeze}")
    print(f"  sha256 {frozen['sha256']}")
    print(f"  Forward test: tokens first assessed after the freeze, results withheld until {FORWARD_MIN_ROWS} are resolved.")
    print("  Keep analyzer.py unchanged until then - rows from another analyzer version are excluded from the test.")


def run_forward(args):
    frozen = load_frozen(args.forward)
    start = forward_start(frozen, args.since)
    # Deduplicate over the whole period since fitting began, so a token first seen
    # before the freeze can never reappear as a "new" forward observation.
    records, audit = load_dataset(args.candidates, args.outcomes, since=frozen["fitted_on"]["since"],
                                  checkpoint_hours=args.checkpoint_hours, chain=args.chain)
    rows, other_version = forward_records(records, frozen, start)
    names = list(frozen["schemes"])
    print(f"Re-weighting experiment v{EXPERIMENT_VERSION} - FORWARD TEST of {args.forward}")
    print(f"  Frozen {frozen['frozen_at']} UTC, sha256 {frozen['sha256'][:16]}... verified")
    print(f"  Forward window opens {start.isoformat(timespec='seconds')}; "
          f"{len(rows)} new tokens resolved at {args.checkpoint_hours}h"
          + (f" ({other_version} excluded: scored by a different analyzer version)" if other_version else ""))
    rebuilt = sum(r["rebuilt_ok"] for r in rows)
    print(f"  Baseline rebuilt from stored components: {rebuilt}/{len(rows)} exact")
    need = frozen["min_rows"]
    if len(rows) < need:
        if rows:
            days = max((rows[-1]["ts"] - start).total_seconds() / 86400, 1e-6)
            per_day = len(rows) / days
            print(f"  Progress: {len(rows)}/{need} (~{per_day:.0f} per day -> about {(need - len(rows)) / per_day:.1f} more days"
                  f" of assessments, plus the 24h wait for their outcomes)")
        print("  Results are withheld until the pre-registered sample size is reached, so the test cannot be")
        print("  stopped early at a moment when the numbers happen to look good.")
        return
    primary = evaluate(rows, frozen["schemes"], reps=args.bootstrap, seed=args.seed, label=PRIMARY[0])
    secondary = evaluate(rows, frozen["schemes"], reps=args.bootstrap, seed=args.seed + 7, label=SECONDARY[0])
    for k in names:
        print(f"  {k}  {SCHEME_NOTES[k]}")
    print_auc_table(primary, names, f"PRIMARY: {PRIMARY[1]}")
    print_buckets(primary, names, "Delisted at 24h")
    print_chains(primary, names)
    print_auc_table(secondary, names, f"SECONDARY: {SECONDARY[1]}")
    print_eligibility(primary, names, "rate = delisted", gates=("score+static gates",))
    print_eligibility(secondary, names, f"rate = delisted or down {-CRASH_PCT}%+", gates=("score+static gates",))
    passed = forward_verdict(primary, secondary, names)
    print("\nPRE-REGISTERED VERDICT")
    print(f"  Rule: {frozen['rule']}.")
    for k in names[1:]:
        print(f"  {k}: {'PASSES' if k in passed else 'does not pass'}")
    print("  Survival and 24h return only; the risks some rules guard against (cannot sell) are not observable here.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", default="paperbot_candidates.jsonl")
    ap.add_argument("--outcomes", default="paperbot_outcomes.jsonl")
    ap.add_argument("--since", default=None, help=f"UTC start (default {DEFAULT_SINCE}; earlier labels are corrupt). "
                                                  "In --forward mode the default is the freeze time, and earlier is refused.")
    ap.add_argument("--until", default=None, help="Exclusive UTC end (split mode only).")
    ap.add_argument("--checkpoint-hours", type=int, default=24)
    ap.add_argument("--chain", choices=["solana", "base"])
    ap.add_argument("--split", type=float, default=0.6, help="Derivation share, by time (default 0.6).")
    ap.add_argument("--min-n", type=int, default=50, help="Min rows on each side for a rule to be re-weighted.")
    ap.add_argument("--ridge", type=float, default=1.0, help="L2 penalty for the logistic fit.")
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260921)
    ap.add_argument("--all-assessments", action="store_true",
                    help="Keep repeat assessments of the same token (holdout still purged of derivation tokens).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--freeze", metavar="FILE", help="Fit D and E on all data now and pre-register them in FILE.")
    mode.add_argument("--forward", metavar="FILE", help="Test the schemes frozen in FILE on data after the freeze.")
    args = ap.parse_args()
    if not 0 < args.split < 1:
        ap.error("--split must be between 0 and 1")

    if args.forward:
        try:
            return run_forward(args)
        except ValueError as exc:
            raise SystemExit(str(exc))
    args.since = args.since or DEFAULT_SINCE
    if args.freeze:
        return run_freeze(args)

    records, audit = load_dataset(args.candidates, args.outcomes, since=args.since, until=args.until,
                                  checkpoint_hours=args.checkpoint_hours, chain=args.chain,
                                  one_per_token=not args.all_assessments)
    derivation, holdout, purged = chronological_split(records, args.split)
    if len(derivation) < 100 or len(holdout) < 100:
        raise SystemExit(f"Not enough resolved rows to run the experiment ({len(derivation)} derivation, {len(holdout)} holdout).")
    derived = derive_schemes(derivation, min_n=args.min_n, ridge=args.ridge)
    schemes = {k: derived["schemes"][k] for k in "ABCD"}  # E was designed after this holdout was read
    result = evaluate(holdout, schemes, reps=args.bootstrap, seed=args.seed)
    print_report(records, audit, derivation, holdout, purged, derived, result, args)


if __name__ == "__main__":
    main()
