"""Regressions for find_wallets.py: winner/control selection, buyer attribution,
page budgeting, and the lift score that separates skill from a sniper bot."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import find_wallets as fw

T = 1_757_000_000  # arbitrary fixed epoch for the fixtures
ISO = datetime.fromtimestamp(T, tz=timezone.utc).isoformat()


def outcome(address, pct, chain="solana", hours=24, status="observed", symbol=None):
    return {"address": address, "chain": chain, "symbol": symbol or address, "checkpoint_hours": hours,
            "observation_status": status, "price_change_pct": pct, "discovered_at": ISO}


def tx(payer, mint, amount, ts, sig="s"):
    """A buy: the fee payer nets a positive amount of the mint."""
    return {"feePayer": payer, "timestamp": ts, "signature": sig,
            # A buy requires consideration; a token receipt alone can be an airdrop.
            "nativeTransfers": [{"fromUserAccount": payer, "toUserAccount": "POOL", "amount": 1_000_000_000}],
            "tokenTransfers": [{"mint": mint, "tokenAmount": amount,
                                "fromUserAccount": "POOL", "toUserAccount": payer}]}


class SelectionTests(unittest.TestCase):
    def test_winners_controls_split_and_filtering(self):
        rows = [
            outcome("WIN1", 120.0), outcome("WIN2", 50.0),          # winners (>= 50)
            outcome("CTL1", 49.9), outcome("CTL2", -80.0),          # controls
            outcome("BASE1", 900.0, chain="base"),                  # wrong chain
            outcome("WRONGHR", 900.0, hours=6),                     # wrong horizon
            outcome("CENSORED", 900.0, status="no_pairs"),          # not an observation
            {"address": "NOPCT", "chain": "solana", "checkpoint_hours": 24,
             "observation_status": "observed", "price_change_pct": None, "discovered_at": ISO},
        ]
        winners, controls = fw.split_winners_and_controls(rows, 24, 50)
        self.assertEqual(sorted(t["address"] for t in winners), ["WIN1", "WIN2"])
        self.assertEqual(sorted(t["address"] for t in controls), ["CTL1", "CTL2"])

    def test_earliest_observation_per_token_is_used_without_peeking(self):
        winners, controls = fw.split_winners_and_controls(
            [outcome("A", 10.0), outcome("A", 300.0), outcome("A", -5.0)], 24, 50)
        self.assertFalse(winners)
        self.assertEqual([t["pct"] for t in controls], [10.0])

    def test_legacy_rows_without_status_are_accepted(self):
        row = outcome("LEGACY", 80.0)
        del row["observation_status"]
        winners, _ = fw.split_winners_and_controls([row], 24, 50)
        self.assertEqual([t["address"] for t in winners], ["LEGACY"])


class ResilienceTests(unittest.TestCase):
    """Three failures seen on the first live run against real Helius data."""

    def test_fabricated_percentages_are_not_treated_as_winners(self):
        # Seen live: a token reported +1,406,818% and another +485,184%. Those
        # are bad quotes, and their "early buyers" are noise, not signal.
        rows = [outcome("REAL", 200.0), outcome("GLITCH", 1_406_818.0), outcome("INF", float("inf"))]
        winners, controls = fw.split_winners_and_controls(rows, 24, 50)
        self.assertEqual([t["address"] for t in winners], ["REAL"])
        self.assertNotIn("GLITCH", [t["address"] for t in controls])

    def test_winners_are_scanned_newest_first(self):
        # Paging walks backwards from now, so older tokens cost more pages.
        old = dict(outcome("OLD", 100.0), discovered_at=datetime.fromtimestamp(T - 86400 * 5, tz=timezone.utc).isoformat())
        new_ = dict(outcome("NEW", 100.0), discovered_at=datetime.fromtimestamp(T, tz=timezone.utc).isoformat())
        winners, _ = fw.split_winners_and_controls([old, new_], 24, 50)
        self.assertEqual([t["address"] for t in winners], ["NEW", "OLD"])

    def test_one_token_failing_does_not_abort_the_whole_scan(self):
        # Seen live: seven tokens scanned cleanly, then a DNS blip killed the run.
        tokens = [{"address": "A", "symbol": "A", "discovered_ts": T, "pct": 100.0},
                  {"address": "B", "symbol": "B", "discovered_ts": T, "pct": 100.0},
                  {"address": "C", "symbol": "C", "discovered_ts": T, "pct": 100.0}]
        calls = []

        def flaky(address, *a, **k):
            calls.append(address)
            if address == "B":
                raise RuntimeError("Helius request failed after 6 attempts. Last error: URLError: getaddrinfo failed")
            return {"WALLET"}, True, 1

        args = SimpleArgs(max_pages=20, window_minutes=60, spacing=0)
        with patch.object(fw, "early_buyers", side_effect=flaky), contextlib.redirect_stdout(io.StringIO()) as buf:
            hits, reached, skipped = fw.scan(tokens, "win", "KEY", args)
        self.assertEqual(calls, ["A", "B", "C"])       # it kept going
        self.assertEqual(reached, 2)                    # B counted as neither reached nor skipped
        self.assertEqual(skipped, [])
        self.assertEqual(hits["WALLET"], {"A", "C"})
        self.assertIn("NETWORK FAILURE", buf.getvalue())


class SimpleArgs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class BuyerAttributionTests(unittest.TestCase):
    def test_fee_payer_receiving_the_mint_is_the_buyer(self):
        self.assertEqual(fw.buyer_of(tx("ALICE", "M", 100, T), "M"), "ALICE")

    def test_seller_is_not_a_buyer(self):
        sell = {"feePayer": "ALICE", "timestamp": T,
                "tokenTransfers": [{"mint": "M", "tokenAmount": 100,
                                    "fromUserAccount": "ALICE", "toUserAccount": "POOL"}]}
        self.assertIsNone(fw.buyer_of(sell, "M"))

    def test_pool_and_router_accounts_are_excluded(self):
        # tokens move through an account that did not pay the fee
        routed = {"feePayer": "ALICE", "timestamp": T,
                  "tokenTransfers": [{"mint": "M", "tokenAmount": 100,
                                      "fromUserAccount": "POOL", "toUserAccount": "ROUTER"}]}
        self.assertIsNone(fw.buyer_of(routed, "M"))

    def test_other_mints_are_ignored(self):
        self.assertIsNone(fw.buyer_of(tx("ALICE", "OTHER", 100, T), "M"))

    def test_net_zero_round_trip_is_not_a_buy(self):
        wash = {"feePayer": "ALICE", "timestamp": T, "tokenTransfers": [
            {"mint": "M", "tokenAmount": 100, "fromUserAccount": "POOL", "toUserAccount": "ALICE"},
            {"mint": "M", "tokenAmount": 100, "fromUserAccount": "ALICE", "toUserAccount": "POOL"}]}
        self.assertIsNone(fw.buyer_of(wash, "M"))


class PagingTests(unittest.TestCase):
    def test_stops_once_launch_is_reached_and_respects_window(self):
        page = [tx("LATE", "M", 5, T + 7200), tx("EARLY", "M", 5, T - 60)]
        with patch.object(fw, "helius_get", return_value=page), patch.object(fw.time, "sleep"):
            buyers, reached, pages = fw.early_buyers("M", T, "k", max_pages=8, window_seconds=3600, spacing=0)
        self.assertEqual(buyers, {"EARLY"})   # LATE bought 2h after discovery
        self.assertTrue(reached)
        self.assertEqual(pages, 1)

    def test_budget_exhaustion_reports_not_reached(self):
        # every page is full and always newer than the launch, so we never get back
        full = [tx(f"W{i}", "M", 1, T + 100_000) for i in range(fw.PAGE_SIZE)]
        with patch.object(fw, "helius_get", return_value=full), patch.object(fw.time, "sleep"):
            buyers, reached, pages = fw.early_buyers("M", T, "k", max_pages=3, window_seconds=3600, spacing=0)
        self.assertFalse(reached)
        self.assertEqual(pages, 2)  # repeated cursor terminates incomplete paging early
        self.assertFalse(buyers)

    def test_empty_history_counts_as_reached(self):
        with patch.object(fw, "helius_get", return_value=[]), patch.object(fw.time, "sleep"):
            _, reached, pages = fw.early_buyers("M", T, "k", max_pages=8, window_seconds=3600, spacing=0)
        self.assertTrue(reached)
        self.assertEqual(pages, 1)


class EndToEndTests(unittest.TestCase):
    """SKILL is early to both winners and neither control. SNIPER is early to
    everything. The lift score must separate them; winner count alone does not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path.cwd()
        self.addCleanup(lambda: os.chdir(self.cwd))
        os.chdir(self.tmp.name)
        rows = [outcome("WIN1", 200.0), outcome("WIN2", 150.0), outcome("CTL1", 5.0), outcome("CTL2", -60.0)]
        with open("outcomes.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def history(self, url, params, api_key, retries=4):
        mint = url.split("/addresses/")[1].split("/")[0]
        early = T - 60
        txs = [tx("SNIPER", mint, 1, early, "a")]
        if mint.startswith("WIN"):
            txs.append(tx("SKILL", mint, 1, early, "b"))
        return txs

    def run_main(self, *extra):
        argv = ["find_wallets.py", "--outcomes", "outcomes.jsonl", "--sample", "2", "--min-winners", "2", *extra]
        with patch.dict(os.environ, {"HELIUS_API_KEY": "test-key"}), \
             patch.object(fw, "helius_get", side_effect=self.history), \
             patch.object(fw.time, "sleep"), patch.object(sys, "argv", argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with contextlib.suppress(SystemExit):
                    fw.main()
            return buf.getvalue()

    def test_sniper_is_ranked_but_excluded_by_lift(self):
        out = self.run_main()
        self.assertIn("buys everything", out)          # SNIPER is described, not silently dropped
        written = Path("wallets.txt").read_text(encoding="utf-8")
        self.assertIn("SKILL", written)
        self.assertNotIn("SNIPER", written)
        self.assertIn("winners=2 controls=0 lift=inf", written)
        self.assertIn("HYPOTHESES, NOT VETTED TRADERS", written)

    def test_lowering_min_lift_lets_the_sniper_through(self):
        self.run_main("--min-lift", "0.5")
        self.assertIn("SNIPER", Path("wallets.txt").read_text(encoding="utf-8"))

    def test_no_qualifying_wallet_reports_that_as_a_result(self):
        out = self.run_main("--min-winners", "5")
        self.assertIn("no wallet-level signal", out)
        self.assertEqual(Path("wallets.txt").read_text().strip(), "# Completed scan: no wallets met the configured thresholds.")

    def test_missing_key_is_refused(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["find_wallets.py", "--outcomes", "outcomes.jsonl"]):
            with self.assertRaises(SystemExit) as cm:
                fw.main()
        self.assertIn("HELIUS_API_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
