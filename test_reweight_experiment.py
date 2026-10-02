"""Planted-signal tests for reweight_experiment.py.

Synthetic candidates are produced by the real analyzer.analyze(), so their
score_components have exactly the production structure. Delisting is then drawn
from a KNOWN model, and each test checks the experiment recovers that answer -
or, for the null and drift cases, that it refuses to claim one.
"""
import argparse
import contextlib
import io
import json
import math
import random
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path

import reweight_experiment as rx
from analyzer import ANALYZER_VERSION, analyze
from test_analyzer import clean_rugcheck, make_base_security, make_pair, make_solana_security

START = datetime(2026, 9, 8, tzinfo=timezone.utc)
CLEAN_HONEYPOT = {"honeypotResult": {"isHoneypot": False}, "simulationResult": {"sellTax": 0}}
CONFIG = {"max_risk_score": 30, "min_liquidity_usd": 15000, "min_pool_age_hours": 6, "require_socials": True}


def iso(dt):
    return dt.isoformat(timespec="seconds")


def synthetic_candidate(rng, chain):
    liquidity = rng.choice([1000, 8000, 30000, 100000, 100000])
    age = rng.choice([3, 20, 100, 100])
    pair = make_pair(chain_id=chain, liquidity_usd=liquidity, age_hours=age,
                     volume_h24=liquidity * (0.01 if rng.random() < 0.2 else 0.5),
                     websites=[] if rng.random() < 0.15 else None, socials=[])
    if chain == "solana":
        security = make_solana_security(mint_renounced=rng.random() > 0.12, freeze_renounced=rng.random() > 0.12,
                                        top10_pct=rng.choice([20, 20, 50, 80]))
        result = analyze("solana", "x", [pair], security, clean_rugcheck(), None)
    else:
        security = make_base_security(open_source=rng.random() > 0.2, mintable=rng.random() < 0.15,
                                      owner_renounced=rng.random() > 0.3)
        result = analyze("base", "x", [pair], security, None, CLEAN_HONEYPOT)
    return result, age


def write_dataset(tmp, n, logit, seed=1, drift_at=None, pre_since=200, base_share=0.3):
    """Write candidates/outcomes files. `logit(fired_rules, late)` gives the true
    log-odds of delisting; `late` is True for rows after `drift_at` (a fraction)."""
    rng = random.Random(seed)
    # Returns come from a separate stream so adding them never changes the labels.
    # Planted label-blind risk: authority-holding tokens survive, but 60% are diluted
    # or frozen and marked -80% while staying listed - invisible to the delisting label.
    ret_rng = random.Random(seed + 1000)
    cand_path, out_path = Path(tmp) / "c.jsonl", Path(tmp) / "o.jsonl"
    cands, outs = [], []
    # Rows before the evaluation window carry corrupt labels: every one "delisted".
    for i in range(pre_since):
        ts = START - timedelta(days=1, minutes=i)
        cid = uuid.uuid4().hex
        result, _ = synthetic_candidate(rng, "solana")
        cands.append({"timestamp": iso(ts), "candidate_id": cid, "chain": "solana", "address": f"OLD{i}",
                      "score": result["score"], "score_components": result["score_components"], "config": CONFIG})
        outs.append({"candidate_id": cid, "chain": "solana", "address": f"OLD{i}", "discovered_at": iso(ts),
                     "checked_at": iso(ts + timedelta(hours=24)), "checkpoint_hours": 24, "observation_status": "no_pairs"})
    for i in range(n):
        ts = START + timedelta(minutes=5 * i)
        chain = "base" if rng.random() < base_share else "solana"
        address = f"0xABC{i:037d}" if chain == "base" else f"Sol{i}"
        result, age = synthetic_candidate(rng, chain)
        fired = rx.fired_points(result)
        late = drift_at is not None and i >= n * drift_at
        p = 1 / (1 + math.exp(-logit(fired, late)))
        y = rng.random() < p
        cid = uuid.uuid4().hex
        cands.append({"timestamp": iso(ts), "candidate_id": cid, "chain": chain, "address": address,
                      "score": result["score"], "score_components": result["score_components"],
                      "liquidity_usd": result["liquidity_usd"],
                      "created_ms": ts.timestamp() * 1000 - age * 3_600_000, "config": CONFIG,
                      "analyzer_version": ANALYZER_VERSION})
        common = {"candidate_id": cid, "chain": chain, "address": address, "discovered_at": iso(ts)}
        # 1h rows are noise and must be ignored at the 24h horizon.
        outs.append({**common, "checked_at": iso(ts + timedelta(hours=1)), "checkpoint_hours": 1,
                     "observation_status": rng.choice(["no_pairs", "observed"])})
        # Missingness, deliberately concentrated in survivors: counting it as
        # "delisted" (the project's recurring defect) would visibly distort results.
        status = "no_pairs" if y else "observed"
        if not y and rng.random() < 0.10:
            status = rng.choice(["missed_checkpoint", "fetch_failed"])
        dumped = ("mint_authority" in fired or "freeze_authority" in fired) and ret_rng.random() < 0.6
        change = -80.0 if dumped else ret_rng.gauss(0, 25)
        outs.append({**common, "checked_at": iso(ts + timedelta(hours=24)), "checkpoint_hours": 24,
                     "observation_status": status, "price_change_pct": change if status == "observed" else None})
    cand_path.write_text("".join(json.dumps(c) + "\n" for c in cands), encoding="utf-8")
    out_path.write_text("".join(json.dumps(o) + "\n" for o in outs), encoding="utf-8")
    return cand_path, out_path


INVERTED_TRUTH = {"liquidity_critical": 1.6, "liquidity_high": 0.9, "liquidity_medium": 0.4, "pool_age_high": 1.2,
                  "pool_age_medium": 0.5, "not_open_source": 1.0, "no_socials": 0.3,
                  "mint_authority": -2.5, "freeze_authority": -2.0}


def inverted_logit(fired, late):
    return -1.5 + sum(INVERTED_TRUTH.get(rule, 0.0) for rule in fired)


def run(cand_path, out_path, reps=300, **kw):
    records, audit = rx.load_dataset(cand_path, out_path, since=rx.DEFAULT_SINCE, **kw)
    derivation, holdout, purged = rx.chronological_split(records, 0.6)
    derived = rx.derive_schemes(derivation, min_n=50)
    result = rx.evaluate(holdout, derived["schemes"], reps=reps, seed=7)
    return records, audit, derived, result


class AucTests(unittest.TestCase):
    def test_auc_matches_pairwise_definition_with_ties(self):
        rng = random.Random(3)
        scores = [rng.randint(0, 10) for _ in range(300)]
        labels = [rng.random() < 0.3 + s / 30 for s in scores]
        pos = [s for s, y in zip(scores, labels) if y]
        neg = [s for s, y in zip(scores, labels) if not y]
        brute = sum((p > n) + 0.5 * (p == n) for p, n in product(pos, neg)) / (len(pos) * len(neg))
        self.assertAlmostEqual(rx.auc(scores, labels), brute, places=12)

    def test_identical_schemes_give_zero_width_interval(self):
        labels = [i % 3 == 0 for i in range(200)]
        table = {"A": [i % 7 for i in range(200)], "B": [i % 7 for i in range(200)]}
        ci = rx.paired_bootstrap(table, labels, "A", ["B"], reps=100, seed=1)
        self.assertEqual(ci["B"][0.05], (0.0, 0.0))


class PlantedSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.paths = write_dataset(cls.tmp.name, 6000, inverted_logit, seed=11)
        cls.records, cls.audit, cls.derived, cls.result = run(*cls.paths)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_baseline_rebuilds_every_stored_score(self):
        self.assertTrue(all(r["rebuilt_ok"] for r in self.records))
        self.assertEqual(rx.score(self.records[0], {}), self.records[0]["stored_score"])

    def test_missingness_and_other_horizons_are_excluded_not_labelled(self):
        """Missed and failed checkpoints were once counted as deaths; they must be
        dropped from numerator and denominator alike."""
        excluded = self.audit["excluded:missed_checkpoint"] + self.audit["excluded:fetch_failed"]
        self.assertGreater(excluded, 200)
        self.assertEqual(self.audit["horizon_rows"], 6000)  # 1h rows and pre-window rows never counted
        self.assertEqual(self.audit["rows_used"], 6000 - excluded)

    def test_rows_before_since_are_ignored(self):
        self.assertFalse(any(str(r["token"][1]).startswith("OLD") for r in self.records))

    def test_inverted_rules_found_and_only_them(self):
        """First run flagged liquidity_medium (planted +0.4) as inverted: its reference
        group contained the worse liquidity bands. Bands now compare to 'no band fired'."""
        self.assertEqual(self.derived["inverted"], ["freeze_authority", "mint_authority"])
        self.assertEqual(self.derived["schemes"]["B"], {"freeze_authority": 0, "mint_authority": 0})

    def test_log_lift_and_logistic_zero_the_protective_rules(self):
        for scheme in ("C", "D"):
            self.assertEqual(self.derived["schemes"][scheme]["mint_authority"], 0)
            self.assertEqual(self.derived["schemes"][scheme]["freeze_authority"], 0)
        coefs = self.derived["logistic"][1]
        self.assertLess(coefs["mint_authority"], -1.5)
        self.assertGreater(coefs["liquidity_critical"], 1.0)
        # Liquidity bands must come out monotonic, as planted.
        d = self.derived["schemes"]["D"]
        self.assertGreater(d["liquidity_critical"], d["liquidity_high"])
        self.assertGreater(d["liquidity_high"], d["liquidity_medium"])

    def test_fixed_schemes_beat_baseline_on_holdout(self):
        adj = self.result["alphas"][1]
        for scheme in ("B", "C", "D"):
            self.assertGreater(self.result["auc"][scheme], self.result["auc"]["A"])
            self.assertGreater(self.result["ci"][scheme][adj][0], 0, scheme)
        self.assertIn("B", rx.verdict(self.result, ["A", "B", "C", "D"], adj))

    def test_newly_buyable_tokens_are_the_survivors(self):
        per = self.result["eligibility"]["score gate only"]["B"]
        n_gained, gained_rate = per["gained"]
        self.assertGreater(n_gained, 50)
        self.assertEqual(per["lost"][0], 0)  # zeroing weights can only lower scores
        self.assertLess(gained_rate, self.result["delisted_pct"])

    def test_holdout_labels_never_influence_the_schemes(self):
        """Flip every holdout label on disk: every derived weight must be unchanged."""
        records, _ = rx.load_dataset(*self.paths, since=rx.DEFAULT_SINCE)
        _, holdout, _ = rx.chronological_split(records, 0.6)
        holdout_ids = {r["candidate"]["candidate_id"] for r in holdout}
        with tempfile.TemporaryDirectory() as tmp:
            flipped = Path(tmp) / "o.jsonl"
            with open(self.paths[1], encoding="utf-8") as src, open(flipped, "w", encoding="utf-8") as dst:
                for line in src:
                    o = json.loads(line)
                    if o["candidate_id"] in holdout_ids and o["checkpoint_hours"] == 24:
                        o["observation_status"] = {"no_pairs": "observed", "observed": "no_pairs"}[o["observation_status"]]
                    dst.write(json.dumps(o) + "\n")
            _, _, derived2, result2 = run(self.paths[0], flipped, reps=50)
        self.assertEqual(derived2["schemes"], self.derived["schemes"])
        self.assertLess(result2["auc"]["A"], 0.5)  # and the flip really did reach the holdout


class NoFalseDiscoveryTests(unittest.TestCase):
    def test_null_truth_equal_to_current_weights_yields_no_winner(self):
        """If the current points ARE the true model, nothing may be declared better."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_dataset(tmp, 6000, lambda fired, late: -2.2 + 0.05 * sum(fired.values()), seed=5)
            _, _, derived, result = run(*paths)
        self.assertEqual(derived["inverted"], [])
        self.assertEqual(rx.verdict(result, ["A", "B", "C", "D"], result["alphas"][1]), [])

    def test_signal_that_reverses_after_the_split_is_not_deployed(self):
        """A pattern that holds only in the derivation period must fail on the holdout -
        the point of a chronological split rather than a refit on all data."""
        def drifting(fired, late):
            effect = INVERTED_TRUTH | {"mint_authority": 2.5 if late else -2.5, "freeze_authority": 0.0}
            return -1.5 + sum(effect.get(rule, 0.0) for rule in fired)
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_dataset(tmp, 6000, drifting, seed=9, drift_at=0.6)
            _, _, derived, result = run(*paths)
        self.assertIn("mint_authority", derived["inverted"])
        self.assertLess(result["auc"]["B"], result["auc"]["A"])
        self.assertNotIn("B", rx.verdict(result, ["A", "B", "C", "D"], result["alphas"][1]))


class DatasetHandlingTests(unittest.TestCase):
    def test_one_row_per_token_keeps_the_earliest_and_folds_evm_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            cands, outs = [], []
            for i, (addr, status) in enumerate([("0xAbC", "observed"), ("0xabc", "no_pairs"), ("SolA", "no_pairs"),
                                               ("sola", "observed")]):
                chain = "base" if addr.startswith("0x") else "solana"
                ts = iso(START + timedelta(hours=i))
                cands.append({"timestamp": ts, "candidate_id": str(i), "chain": chain, "address": addr,
                              "score": 0, "score_components": []})
                outs.append({"candidate_id": str(i), "chain": chain, "address": addr, "discovered_at": ts,
                             "checked_at": iso(START + timedelta(hours=i + 24)), "checkpoint_hours": 24,
                             "observation_status": status})
            (Path(tmp) / "c.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cands), encoding="utf-8")
            (Path(tmp) / "o.jsonl").write_text("".join(json.dumps(o) + "\n" for o in outs), encoding="utf-8")
            records, audit = rx.load_dataset(Path(tmp) / "c.jsonl", Path(tmp) / "o.jsonl")
            every, _ = rx.load_dataset(Path(tmp) / "c.jsonl", Path(tmp) / "o.jsonl", one_per_token=False)
        # Base 0xAbC/0xabc are one token (earliest, survived); Solana SolA/sola are two.
        self.assertEqual([(r["token"], r["y"]) for r in records],
                         [(("base", "0xabc"), 0), (("solana", "SolA"), 1), (("solana", "sola"), 0)])
        self.assertEqual(audit["repeat_assessments_dropped"], 1)
        self.assertEqual(len(every), 4)

    def test_split_purges_holdout_tokens_seen_in_derivation(self):
        recs = [{"ts": START + timedelta(hours=i), "token": ("solana", t)} for i, t in enumerate("ABCAD")]
        derivation, holdout, purged = rx.chronological_split(recs, 0.6)
        self.assertEqual([r["token"][1] for r in holdout], ["D"])
        self.assertEqual(purged, 1)

    def test_static_entry_gates(self):
        ts = START + timedelta(days=1)

        def rec(**over):
            cand = {"liquidity_usd": 20000, "created_ms": ts.timestamp() * 1000 - 10 * 3_600_000, "config": CONFIG}
            cand.update(over)
            return {"ts": ts, "candidate": cand, "fired": {}}
        self.assertTrue(rx.passes_static_entry_gates(rec()))
        self.assertFalse(rx.passes_static_entry_gates(rec(liquidity_usd=10000)))
        self.assertFalse(rx.passes_static_entry_gates(rec(created_ms=ts.timestamp() * 1000 - 2 * 3_600_000)))
        self.assertFalse(rx.passes_static_entry_gates(rec(created_ms=None)))
        no_socials = rec()
        no_socials["fired"] = {"no_socials": 5}
        self.assertFalse(rx.passes_static_entry_gates(no_socials))


class LabelTests(unittest.TestCase):
    def test_secondary_label_treats_bad_quotes_as_missing(self):
        self.assertEqual(rx.crash_label(1, None), 1)
        self.assertEqual(rx.crash_label(0, -60.0), 1)
        self.assertEqual(rx.crash_label(0, -50.0), 1)
        self.assertEqual(rx.crash_label(0, 12.0), 0)
        self.assertIsNone(rx.crash_label(0, None))
        self.assertIsNone(rx.crash_label(0, float("nan")))
        self.assertIsNone(rx.crash_label(0, 5_000_000.0))  # fabricated quote, as with TOSHI/PUMP


class SafetyFloorTests(unittest.TestCase):
    def test_safety_rules_never_drop_below_current_points(self):
        d = {"mint_authority": 0, "not_open_source": 20, "honeypot_sim": 40, "liquidity_critical": 33, "pool_age_high": 0}
        current = {"mint_authority": 25, "not_open_source": 12, "honeypot_sim": 40, "liquidity_critical": 30, "pool_age_high": 15}
        e = rx.safety_floor(d, current)
        # mint_authority and honeypot_sim are left out, so they keep today's stored points exactly.
        self.assertEqual(e, {"not_open_source": 20, "liquidity_critical": 33, "pool_age_high": 0})

    def test_multi_finding_safety_rule_keeps_stored_total(self):
        record = {"fired": {"rugcheck_high": 24, "liquidity_high": 18}}
        e = rx.safety_floor({"rugcheck_high": 0, "liquidity_high": 20}, {"rugcheck_high": 8, "liquidity_high": 18})
        self.assertEqual(rx.score(record, e), 44)  # three RugCheck findings still count 3 x 8


class FreezeForwardTests(unittest.TestCase):
    """Freeze on the first 60% of a planted dataset, forward-test on the rest."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.paths = write_dataset(cls.tmp.name, 25000, inverted_logit, seed=31)
        cls.records, _ = rx.load_dataset(*cls.paths)
        cls.freeze_at = cls.records[int(len(cls.records) * 0.6)]["ts"]
        before = [r for r in cls.records if r["ts"] < cls.freeze_at]
        derived = rx.derive_schemes(before)
        cls.frozen = rx.build_frozen(before, derived, rx.DEFAULT_SINCE, cls.freeze_at.isoformat(timespec="seconds"))
        cls.frozen_path = Path(cls.tmp.name) / "frozen.json"
        rx.write_frozen(cls.frozen_path, cls.frozen)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def forward(self, **over):
        args = argparse.Namespace(forward=str(self.frozen_path), candidates=str(self.paths[0]),
                                  outcomes=str(self.paths[1]), since=None, checkpoint_hours=24, chain=None,
                                  bootstrap=200, seed=3)
        vars(args).update(over)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rx.run_forward(args)
        return out.getvalue()

    def test_freeze_refuses_to_overwrite(self):
        with self.assertRaises(FileExistsError):
            rx.write_frozen(self.frozen_path, self.frozen)

    def test_edited_weights_are_detected(self):
        tampered = Path(self.tmp.name) / "tampered.json"
        data = json.loads(self.frozen_path.read_text(encoding="utf-8"))
        data["schemes"]["E"]["liquidity_critical"] = 99
        tampered.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            rx.load_frozen(tampered)

    def test_forward_window_cannot_start_before_the_freeze(self):
        with self.assertRaises(ValueError):
            rx.forward_start(self.frozen, (self.freeze_at - timedelta(hours=1)).isoformat())
        self.assertEqual(rx.forward_start(self.frozen), self.freeze_at)

    def test_forward_rows_are_new_tokens_only(self):
        """A token first seen before the freeze must not count as a forward observation."""
        rows, other = rx.forward_records(self.records, self.frozen, self.freeze_at)
        before = {r["token"] for r in self.records if r["ts"] < self.freeze_at}
        self.assertTrue(rows)
        self.assertFalse(before & {r["token"] for r in rows})
        self.assertTrue(all(r["ts"] >= self.freeze_at for r in rows))
        self.assertEqual(other, 0)

    def test_other_analyzer_versions_are_excluded(self):
        frozen = dict(self.frozen, analyzer_version="9.9.9")
        rows, other = rx.forward_records(self.records, frozen, self.freeze_at)
        self.assertEqual(rows, [])
        self.assertGreater(other, 0)

    def test_results_withheld_below_preregistered_size(self):
        text = self.forward()  # ~9,000 forward rows, but min_rows is raised below
        self.assertIn("PRE-REGISTERED VERDICT", text)
        data = json.loads(self.frozen_path.read_text(encoding="utf-8"))
        big = Path(self.tmp.name) / "big.json"
        data["min_rows"] = 10**6
        big.write_text(json.dumps(data), encoding="utf-8")
        text = self.forward(forward=str(big))
        self.assertIn("withheld", text)
        self.assertNotIn("AUC", text)
        self.assertNotIn("VERDICT", text)

    def test_e_keeps_the_label_blind_protection_that_d_drops(self):
        """D zeroes the authority rules because those tokens survive; the planted -80%
        dumps on them are invisible to the delisting label but not to the secondary one.
        (A first version planted dumps on mint only, while freeze stayed genuinely
        protective: there E's floor rightly cost it accuracy - the floor is a trade-off.)"""
        for rule in ("mint_authority", "freeze_authority"):
            self.assertEqual(self.frozen["schemes"]["D"][rule], 0)
            self.assertNotIn(rule, self.frozen["schemes"]["E"])
        rows, _ = rx.forward_records(self.records, self.frozen, self.freeze_at)
        primary = rx.evaluate(rows, self.frozen["schemes"], reps=200, seed=1)
        secondary = rx.evaluate(rows, self.frozen["schemes"], reps=200, seed=1, label="crash")
        self.assertGreater(primary["auc"]["D"], primary["auc"]["E"])      # D wins on the blind label...
        self.assertGreater(secondary["auc"]["E"], secondary["auc"]["D"])  # ...and loses where the risk shows
        self.assertLess(secondary["auc"]["D"], secondary["auc"]["A"])
        self.assertIn("E", rx.forward_verdict(primary, secondary, ["A", "D", "E"]))


if __name__ == "__main__":
    unittest.main()
