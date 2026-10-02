"""Offline regressions for pricing, observations, research joins and file serving."""
import contextlib
import http.client
import http.server
import io
import json
import math
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import analyzer as a
import analyze_outcomes as research
import paperbot as bot
import server
from test_analyzer import make_pair, make_base_security, make_solana_security


def pair(address="TokenA", price="2", chain="solana", liquidity=100):
    return {"chainId": chain, "baseToken": {"address": address},
            "priceUsd": price, "liquidity": {"usd": liquidity}}


class PricingTests(unittest.TestCase):
    def fetch(self, responses, addresses=None):
        with patch.object(a, "fetch_json", side_effect=responses), patch.object(a.time, "sleep"):
            return a.get_current_prices("solana", addresses or ["TokenA"])

    def test_bad_envelopes_are_unknown(self):
        # {"pairs": null} is deliberately absent: Dexscreener returns exactly that for an
        # unknown token (observed live), so it is a complete "no listing" answer - see test_policy.
        for response in (None, {}, {"error": "rate limit"},
                         {"pairs": {}}, {"pairs": [None]}, {"pairs": [{"baseToken": "bad"}]}):
            with self.subTest(response=response):
                self.assertIs(self.fetch([response])["TokenA"], a.FETCH_FAILED)

    def test_nonfinite_zero_and_missing_quotes_are_unknown(self):
        for value in (None, "NaN", "Infinity", "-1", "0", "nonsense"):
            with self.subTest(price=value):
                self.assertIs(self.fetch([{"pairs": [pair(price=value)]}])["TokenA"], a.FETCH_FAILED)

    def test_partial_batch_rechecks_every_missing_token(self):
        result = self.fetch([{"pairs": [pair()]}, {"pairs": [pair("TokenB", "3")]}], ["TokenA", "TokenB"])
        self.assertEqual(result, {"TokenA": 2.0, "TokenB": 3.0})

    def test_failed_individual_fallback_stays_unknown(self):
        result = self.fetch([{"pairs": [pair()]}, None], ["TokenA", "TokenB"])
        self.assertIs(result["TokenB"], a.FETCH_FAILED)

    def test_empty_listing_is_not_a_transport_error(self):
        self.assertIsNone(self.fetch([{"pairs": []}])["TokenA"])

    def test_solana_identity_is_case_sensitive(self):
        self.assertEqual(a._best_prices_from_pairs("solana", [pair("AbC"), pair("abc", "7")], ["AbC", "abc"]),
                         {"AbC": 2.0, "abc": 7.0})

    def test_base_identity_is_case_insensitive(self):
        self.assertEqual(a._best_prices_from_pairs("base", [pair("0xABC", chain="base")], ["0xabc"]), {"0xabc": 2.0})

    def test_price_and_levels_reject_nonfinite_inputs(self):
        self.assertIsNone(a.compute_trade_levels("nan", 10))
        self.assertIsNone(a.compute_trade_levels("1", float("inf")))
        self.assertEqual(a.fmt_price(float("nan")), "n/a")


class SecurityTests(unittest.TestCase):
    def test_empty_security_reports_not_clean(self):
        result = a.analyze("base", "x", [make_pair("base")], {"holder_count": "50"}, None, {})
        for rule in ("honeypot", "mintable", "honeypot_sim"):
            self.assertIsNone(research.rule_fired(result | {"score_components": result["score_components"]}, rule))
        self.assertFalse(result["data_sources"]["honeypot_is"])

    def test_empty_rugcheck_lowers_confidence(self):
        self.assertEqual(a.compute_confidence("solana", True, make_solana_security(), {}, None), 85)

    def test_liquidity_bands_have_negative_observations(self):
        result = a.analyze("solana", "x", [make_pair(liquidity_usd=1000)], make_solana_security(), {"risks": []}, None)
        self.assertTrue(research.rule_fired(result, "liquidity_critical"))
        self.assertFalse(research.rule_fired(result, "liquidity_high"))
        self.assertFalse(research.rule_fired(result, "liquidity_medium"))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = SimpleNamespace(outcome_checkpoints_hours="1,6,24", checkpoint_grace_seconds=600,
                                    outcome_log_file=str(Path(self.tmp.name) / "outcomes.jsonl"),
                                    missing_price_limit=3, sell_fee_pct=1.5, gas_cost_usd=0.05,
                                    log_file=str(Path(self.tmp.name) / "trades.csv"))

    def pending(self, hours):
        return {"pending_outcomes": {"TokenA": {
            "chain": "solana", "discovered_at": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),
            "discovery_price": 1.0, "checkpoints_done": [], "candidate_id": "snapshot-1",
            "analyzer_version": "old-engine", "paperbot_version": "old-bot"}}}

    def rows(self):
        return [json.loads(line) for line in Path(self.args.outcome_log_file).read_text().splitlines()]

    def test_restart_does_not_backfill_old_horizons(self):
        state = self.pending(24.01)
        with patch.object(bot, "get_current_prices", return_value={"TokenA": 2.0}):
            bot.check_pending_outcomes(state, self.args)
        rows = self.rows()
        self.assertEqual([r["observation_status"] for r in rows], ["missed_checkpoint", "missed_checkpoint", "observed"])
        self.assertEqual([r["price_change_pct"] for r in rows], [None, None, 100.0])
        self.assertEqual(rows[-1]["analyzer_version"], "old-engine")
        self.assertEqual(rows[-1]["candidate_id"], "snapshot-1")
        self.assertFalse(state["pending_outcomes"])

    def test_missing_price_never_becomes_rug_label(self):
        with patch.object(bot, "get_current_prices", return_value={"TokenA": None}):
            bot.check_pending_outcomes(self.pending(1.01), self.args)
        self.assertEqual(self.rows()[0]["observation_status"], "no_pairs")
        self.assertIsNone(self.rows()[0]["rugged_or_delisted"])

    def test_spike_is_censored_and_fetch_failure_is_retried_within_grace(self):
        with patch.object(bot, "get_current_prices", return_value={"TokenA": 1000.0}):
            bot.check_pending_outcomes(self.pending(1.01), self.args)
        self.assertEqual(self.rows()[-1]["observation_status"], "implausible_price")
        # A failed request inside the grace window is not an observation at all:
        # nothing is logged, the horizon stays pending, and the attempt is counted.
        state = self.pending(1.01)
        with patch.object(bot, "get_current_prices", return_value={"TokenA": a.FETCH_FAILED}):
            bot.check_pending_outcomes(state, self.args)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(state["pending_outcomes"]["TokenA"]["checkpoints_done"], [])
        self.assertEqual(state["pending_outcomes"]["TokenA"]["fetch_failures"], 1)

    def test_at_capacity_the_newcomer_is_refused_and_in_flight_work_survives(self):
        """Changed in 1.6.1. Evicting the OLDEST pending entry threw away the
        observation closest to completing its 24h horizon - the most expensive
        row in the file - to make room for one that had produced nothing yet.
        Refusing the newcomer keeps what is nearly finished; the refusal is
        logged per horizon so the analysis can see how much was skipped."""
        state = self.pending(0.5)
        in_flight = dict(state["pending_outcomes"]["TokenA"])
        self.args.track_skipped_outcomes = True
        snapshot = {"candidate_id": "new-assessment", "timestamp": "2026-09-07T00:00:00+00:00"}
        result = {"price_usd": "1", "score": 10, "analyzer_version": "test-version"}
        with patch.object(bot, "MAX_PENDING_OUTCOMES", 1):
            bot.track_candidate_for_outcomes(state, "solana", "TokenB", result, "skip", self.args, snapshot)
        rows = self.rows()
        self.assertEqual([r["observation_status"] for r in rows], ["not_tracked_at_capacity"] * 3)
        self.assertEqual({r["address"] for r in rows}, {"TokenB"})   # the newcomer, not the incumbent
        self.assertEqual(rows[0]["score_at_discovery"], 10)
        self.assertNotIn("TokenB", state["pending_outcomes"])
        self.assertEqual(state["pending_outcomes"]["TokenA"], in_flight)  # untouched

    def position_state(self):
        return {"cash": 97.0, "starting_pot": 100.0, "closed_trades": [], "positions": {"TokenA": {
            "chain": "solana", "symbol": "TEST", "entry_price": 1.0, "tokens": 3.0, "invested": 3.0,
            "sl_price": 0.8, "tp_price": 1.5, "score": 5, "opened_at": "2026-09-01T00:00:00+00:00"}}}

    def test_missing_prices_do_not_liquidate_positions(self):
        state = self.position_state()
        with patch.object(bot, "get_current_prices", return_value={"TokenA": None}), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(5):
                bot.check_positions(state, self.args)
        self.assertIn("TokenA", state["positions"])
        self.assertEqual(state["cash"], 97)
        self.assertFalse(state["closed_trades"])

    def test_valid_stop_exit_still_settles(self):
        state = self.position_state()
        with patch.object(bot, "get_current_prices", return_value={"TokenA": 0.7}), contextlib.redirect_stdout(io.StringIO()):
            bot.check_positions(state, self.args)
        self.assertEqual(state["closed_trades"][0]["reason"], "stop-loss hit")
        self.assertEqual(state["cash"], 99.02)

    def test_unpriced_portfolio_not_valued_at_cost(self):
        with patch.object(bot, "get_current_price", return_value=None):
            result = bot.portfolio_summary(self.position_state())
        self.assertTrue(math.isnan(result["equity"]))
        self.assertEqual(result["unpriced"], ["TokenA"])


class ResearchTests(unittest.TestCase):
    def test_analysis_main_with_new_and_legacy_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            timestamp = "2026-09-07T00:00:00+00:00"
            cand = {"candidate_id": "one", "chain": "solana", "address": "X", "timestamp": timestamp,
                    "score_components": [], "config": {"max_risk_score": 30}}
            outcome = {"candidate_id": "one", "chain": "solana", "address": "X", "discovered_at": timestamp,
                       "checked_at": "2026-09-08T00:00:00+00:00", "checkpoint_hours": 24,
                       "score_at_discovery": 10, "price_change_pct": 20, "observation_status": "observed",
                       "rugged_or_delisted": None}
            (root / "c.jsonl").write_text(json.dumps(cand) + "\n")
            rows = [outcome, outcome | {"candidate_id": None, "observation_status": "no_pairs", "price_change_pct": None},
                    outcome | {"candidate_id": None, "observation_status": None, "rugged_or_delisted": False}]
            (root / "o.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            (root / "s.json").write_text('{"closed_trades": []}')
            argv = ["analyze_outcomes.py", "--candidates", str(root / "c.jsonl"), "--outcomes", str(root / "o.jsonl"),
                    "--state", str(root / "s.json"), "--since", "2026-09-07T01:00+01:00"]
            output = io.StringIO()
            with patch("sys.argv", argv), contextlib.redirect_stdout(output):
                research.main()
            self.assertIn("Exact assessment joins: 2/2", output.getvalue())
            self.assertIn("'no_pairs': 1", output.getvalue())

    def test_partial_tail_is_skipped_but_complete_corruption_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "live.jsonl"
            path.write_text('{"ok": 1}\n{"partial":')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(list(research.load_jsonl(path)), [{"ok": 1}])
            path.write_text('{"broken":\n')
            with self.assertRaises(json.JSONDecodeError):
                list(research.load_jsonl(path))

    def test_rule_analysis_excludes_unknown_from_control(self):
        candidates, outcomes = [], []
        for ident, severity, points in (("a", "high", 8), ("b", "unknown", 0), ("c", "good", 0)):
            candidates.append({"candidate_id": ident, "score_components": [{"rule": "test_rule", "severity": severity, "points": points}]})
            outcomes.append({"candidate_id": ident, "price_change_pct": -20, "rugged_or_delisted": False})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            research.rule_level_analysis(outcomes, research.index_candidates_by_address(candidates), min_n=1)
        row = next(line.split() for line in output.getvalue().splitlines() if line.strip().startswith("test_rule"))
        self.assertEqual((row[1], row[3]), ("1", "1"))

    def test_new_observations_do_not_dilute_historical_rug_rate(self):
        rows = [{"score_at_discovery": 5, "rugged_or_delisted": True},
                {"score_at_discovery": 5, "rugged_or_delisted": False, "price_change_pct": 10},
                {"score_at_discovery": 5, "rugged_or_delisted": None, "price_change_pct": 20}]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            research.score_bucket_vs_outcome(rows)
        self.assertIn("50.0%", output.getvalue())

    def test_join_selects_correct_assessment_not_first_address(self):
        early = {"chain": "solana", "address": "X", "timestamp": "2026-09-01T00:00:00Z", "candidate_id": "old", "score": 1}
        later = early | {"timestamp": "2026-09-02T00:00:00Z", "candidate_id": "new", "score": 90}
        indexed = research.index_candidates_by_address([early, later])
        self.assertEqual(research.candidate_for_outcome({"candidate_id": "new"}, indexed)["score"], 90)
        self.assertEqual(research.candidate_for_outcome({"chain": "solana", "address": "X", "discovered_at": later["timestamp"]}, indexed)["score"], 90)
        self.assertIsNone(research.candidate_for_outcome({"chain": "base", "address": "X", "discovered_at": later["timestamp"]}, indexed))

    def test_ambiguous_legacy_snapshot_is_not_guessed(self):
        row = {"chain": "solana", "address": "X", "timestamp": "2026-09-01T00:00:00Z"}
        indexed = research.index_candidates_by_address([row, row | {"score": 90}])
        self.assertIsNone(research.candidate_for_outcome(row | {"discovered_at": row["timestamp"]}, indexed))

    def test_unknown_is_not_negative_and_repeated_flags_aggregate(self):
        self.assertIsNone(research.rule_fired({"score_components": [{"rule": "r", "severity": "unknown", "points": 0}]}, "r"))
        self.assertTrue(research.rule_fired({"score_components": [{"rule": "r", "severity": "good", "points": 0},
                                                                 {"rule": "r", "severity": "high", "points": 8}]}, "r"))

    def test_censoring_does_not_count_unknown_as_clean(self):
        usable, excluded = research.usable_outcomes([
            {"observation_status": "no_pairs"}, {"observation_status": "missed_checkpoint"},
            {"observation_status": "observed", "price_change_pct": float("nan")},
            {"observation_status": "observed", "price_change_pct": -20}])
        self.assertEqual(len(usable), 1)
        self.assertEqual(sum(excluded.values()), 3)

    def test_timestamp_comparison_normalizes_offsets(self):
        self.assertEqual(research.utc_time("2026-09-03T22:03+01:00"), research.utc_time("2026-09-03T21:03Z"))


class ServerTests(unittest.TestCase):
    def test_public_assets_only_for_get_and_head(self):
        service = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.NoCacheHandler)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", service.server_port)
            for method in ("GET", "HEAD"):
                for path, expected in (("/", 200), ("/app.js", 200), ("/wallet.py", 404),
                                       ("/paperbot_state.json", 404), ("/../wallet.py", 404), ("/%2e%2e/wallet.py", 404)):
                    connection.request(method, path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected, (method, path))
                    response.read()
            connection.close()
        finally:
            service.shutdown()
            service.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
