#!/usr/bin/env python3
"""Automated tests for analyzer.py's scoring engine.

This is the single shared dependency of the dashboard, CLI, and paper bot -
a silent regression here changes every component's behavior at once, which
is exactly why it's the one piece of this project that most needed a real
test suite rather than manual spot-checks.

Two kinds of test:
  - Fixture tests: deterministic scenarios (honeypot, no liquidity, missing
    API data, etc.) with a known expected outcome.
  - Property tests: "increasing any risk factor should never lower the
    score" - these catch logic errors fixtures alone tend to miss, since
    they check a relationship rather than a specific number.

Run: python test_analyzer.py
"""

import time
import unittest

from analyzer import analyze, compute_confidence, extract_candidate_address


# ---------------------------------------------------------------------------
# Fixture builders - minimal, realistic shapes matching what the real APIs
# return, so these tests exercise the actual field paths analyze() reads.
# ---------------------------------------------------------------------------

def make_pair(chain_id="solana", liquidity_usd=50000, volume_h24=10000, price_usd="0.001",
              age_hours=100, name="Test Coin", symbol="TEST", websites=None, socials=None,
              price_change_h24=1.0):
    return {
        "chainId": chain_id,
        "liquidity": {"usd": liquidity_usd},
        "volume": {"h24": volume_h24},
        "priceUsd": price_usd,
        "fdv": 1_000_000,
        "pairCreatedAt": (time.time() - age_hours * 3600) * 1000,
        "priceChange": {"m5": 1.0, "h1": 2.0, "h6": 5.0, "h24": price_change_h24},
        "txns": {"h24": {"buys": 100, "sells": 90}},
        "baseToken": {"name": name, "symbol": symbol},
        "info": {
            "websites": websites if websites is not None else [{"url": "https://example.com", "label": "Website"}],
            "socials": socials if socials is not None else [{"url": "https://twitter.com/example", "type": "twitter"}],
        },
    }


def make_solana_security(mint_renounced=True, freeze_renounced=True, top10_pct=20):
    return {
        "mintable": {"status": "0" if mint_renounced else "1"},
        "freezable": {"status": "0" if freeze_renounced else "1"},
        "top10_holder_percent": str(top10_pct),
    }


def make_base_security(honeypot=False, open_source=True, mintable=False, buy_tax=0, sell_tax=0,
                        owner_renounced=True, holder_count=500):
    return {
        "is_honeypot": "1" if honeypot else "0",
        "is_open_source": "1" if open_source else "0",
        "is_mintable": "1" if mintable else "0",
        "cannot_sell_all": "0",
        "hidden_owner": "0",
        "can_take_back_ownership": "0",
        "selfdestruct": "0",
        "buy_tax": str(buy_tax),
        "sell_tax": str(sell_tax),
        "owner_address": "0x0000000000000000000000000000000000000000" if owner_renounced else "0xdead000000000000000000000000000000dead",
        "holder_count": str(holder_count),
    }


def clean_rugcheck():
    return {"risks": []}


class PerfectTokenTests(unittest.TestCase):
    def test_perfect_solana_token_scores_zero(self):
        r = analyze("solana", "addr", [make_pair()], make_solana_security(), clean_rugcheck(), None)
        self.assertEqual(r["score"], 0, r["flags"])
        self.assertEqual(r["confidence"], 100)

    def test_perfect_base_token_scores_zero(self):
        r = analyze("base", "addr", [make_pair(chain_id="base")], make_base_security(), None, {"honeypotResult": {"isHoneypot": False}, "simulationResult": {"sellTax": 0}})
        self.assertEqual(r["score"], 0, r["flags"])
        self.assertEqual(r["confidence"], 100)


class KnownScamPatternTests(unittest.TestCase):
    def test_honeypot_is_critical_and_dominant(self):
        r = analyze("base", "addr", [make_pair(chain_id="base")], make_base_security(honeypot=True), None, None)
        self.assertGreaterEqual(r["score"], 40)
        self.assertTrue(any(c["rule"] == "honeypot" and c["severity"] == "critical" for c in r["score_components"]))

    def test_no_liquidity_is_critical(self):
        r = analyze("solana", "addr", [], None, None, None)
        self.assertGreaterEqual(r["score"], 30)
        self.assertTrue(any(c["rule"] == "no_liquidity" for c in r["score_components"]))

    def test_low_liquidity_scores_higher_than_healthy_liquidity(self):
        low = analyze("solana", "addr", [make_pair(liquidity_usd=1000)], make_solana_security(), clean_rugcheck(), None)
        healthy = analyze("solana", "addr", [make_pair(liquidity_usd=200000)], make_solana_security(), clean_rugcheck(), None)
        self.assertGreater(low["score"], healthy["score"])

    def test_mint_authority_not_renounced_is_critical(self):
        r = analyze("solana", "addr", [make_pair()], make_solana_security(mint_renounced=False), clean_rugcheck(), None)
        self.assertGreaterEqual(r["score"], 25)
        self.assertTrue(any(c["rule"] == "mint_authority" and c["severity"] == "critical" for c in r["score_components"]))

    def test_freeze_authority_not_renounced_is_critical(self):
        r = analyze("solana", "addr", [make_pair()], make_solana_security(freeze_renounced=False), clean_rugcheck(), None)
        self.assertGreaterEqual(r["score"], 25)

    def test_high_holder_concentration_is_critical(self):
        r = analyze("solana", "addr", [make_pair()], make_solana_security(top10_pct=85), clean_rugcheck(), None)
        self.assertGreaterEqual(r["score"], 20)

    def test_high_tax_flagged_critical(self):
        r = analyze("base", "addr", [make_pair(chain_id="base")], make_base_security(buy_tax=25), None, None)
        self.assertGreaterEqual(r["score"], 20)


class MissingDataTests(unittest.TestCase):
    """Missing security data must reduce confidence and be recorded as
    UNKNOWN, not silently treated as "clean" (a token with unrenounced mint
    authority and a token where GoPlus is simply down should not look the
    same to a caller relying only on the numeric score)."""

    def test_missing_goplus_data_drops_confidence(self):
        r = analyze("solana", "addr", [make_pair()], None, clean_rugcheck(), None)
        self.assertLess(r["confidence"], 100)
        self.assertFalse(r["data_sources"]["goplus"])
        unknown_rules = [c["rule"] for c in r["score_components"] if c["severity"] == "unknown"]
        self.assertIn("mint_authority", unknown_rules)
        self.assertIn("freeze_authority", unknown_rules)

    def test_missing_rugcheck_drops_confidence(self):
        r = analyze("solana", "addr", [make_pair()], make_solana_security(), None, None)
        self.assertLess(r["confidence"], 100)
        self.assertFalse(r["data_sources"]["rugcheck"])

    def test_no_pair_data_gives_low_confidence(self):
        r = analyze("solana", "addr", [], None, None, None)
        self.assertEqual(r["confidence"], 20)

    def test_missing_data_never_looks_identical_to_clean_result(self):
        """The specific bug this whole section exists to prevent: a token
        analyzer.py couldn't check anything for must not score/read the same
        as a token that was actually checked and found clean."""
        clean = analyze("solana", "addr", [make_pair()], make_solana_security(), clean_rugcheck(), None)
        unchecked = analyze("solana", "addr", [make_pair()], None, None, None)
        self.assertNotEqual(clean["confidence"], unchecked["confidence"])


class MonotonicityTests(unittest.TestCase):
    """Increasing any single risk factor should never DECREASE the score,
    holding everything else constant. These catch sign errors and inverted
    conditions that a fixed-scenario fixture test can miss entirely."""

    def test_liquidity_monotonic(self):
        scores = [
            analyze("solana", "a", [make_pair(liquidity_usd=usd)], make_solana_security(), clean_rugcheck(), None)["score"]
            for usd in (1000, 10000, 30000, 100000)
        ]
        self.assertEqual(scores, sorted(scores, reverse=True), scores)

    def test_pool_age_monotonic(self):
        scores = [
            analyze("solana", "a", [make_pair(age_hours=h)], make_solana_security(), clean_rugcheck(), None)["score"]
            for h in (1, 24, 72, 500)
        ]
        self.assertEqual(scores, sorted(scores, reverse=True), scores)

    def test_holder_concentration_monotonic(self):
        # ascending risk (10% -> 90% concentration), so scores should ascend too -
        # unlike liquidity/age above where the loop runs from high-risk to low-risk.
        scores = [
            analyze("solana", "a", [make_pair()], make_solana_security(top10_pct=p), clean_rugcheck(), None)["score"]
            for p in (10, 30, 50, 90)
        ]
        self.assertEqual(scores, sorted(scores), scores)

    def test_tax_monotonic(self):
        # ascending risk (0% -> 30% tax), so scores should ascend too.
        scores = [
            analyze("base", "a", [make_pair(chain_id="base")], make_base_security(buy_tax=p), None, None)["score"]
            for p in (0, 5, 10, 30)
        ]
        self.assertEqual(scores, sorted(scores), scores)

    def test_honeypot_always_scores_at_least_as_high_as_clean(self):
        clean = analyze("base", "a", [make_pair(chain_id="base")], make_base_security(honeypot=False), None, None)
        hp = analyze("base", "a", [make_pair(chain_id="base")], make_base_security(honeypot=True), None, None)
        self.assertGreaterEqual(hp["score"], clean["score"])

    def test_renounced_authority_never_scores_higher_than_unrenounced(self):
        renounced = analyze("solana", "a", [make_pair()], make_solana_security(mint_renounced=True), clean_rugcheck(), None)
        not_renounced = analyze("solana", "a", [make_pair()], make_solana_security(mint_renounced=False), clean_rugcheck(), None)
        self.assertLessEqual(renounced["score"], not_renounced["score"])


class ScoreBoundsTests(unittest.TestCase):
    def test_score_never_exceeds_100(self):
        worst_pair = make_pair(liquidity_usd=100, age_hours=0.1, websites=[], socials=[])
        r = analyze("solana", "a", [worst_pair], make_solana_security(mint_renounced=False, freeze_renounced=False, top10_pct=99),
                    {"risks": [{"level": "danger", "description": f"bad thing {i}"} for i in range(10)]}, None)
        self.assertLessEqual(r["score"], 100)

    def test_score_never_negative(self):
        r = analyze("solana", "a", [make_pair(liquidity_usd=10_000_000)], make_solana_security(), clean_rugcheck(), None)
        self.assertGreaterEqual(r["score"], 0)


class AddressExtractionTests(unittest.TestCase):
    def test_evm_address_from_url(self):
        addr, chain = extract_candidate_address("https://basescan.org/token/0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed")
        self.assertEqual(chain, "base")
        self.assertEqual(addr, "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed")

    def test_solana_address_from_lowercased_url(self):
        # Regression test: Dexscreener lowercases pair addresses in URLs, which
        # can turn a valid base58 char into one base58 excludes (e.g. 'L' -> 'l').
        addr, chain = extract_candidate_address("https://dexscreener.com/solana/3uwfrdltpajxtrni1boc5huwe6hzc4hge5yldvep2noc")
        self.assertEqual(chain, "solana")
        self.assertEqual(len(addr), 44)

    def test_no_address_found(self):
        addr, chain = extract_candidate_address("this is not an address")
        self.assertIsNone(addr)
        self.assertIsNone(chain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
