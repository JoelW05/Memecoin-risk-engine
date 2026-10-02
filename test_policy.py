"""Regressions for the v1.5 additions on top of the reviewed tree:
unquotable-position valuation policy, bought-cohort fixed-horizon tracking,
single-writer state lock, request pacing/retry, and Dexscreener's null-pairs shape."""
import contextlib
import math
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import analyzer as a
import paperbot as bot


def args_for(tmp, **over):
    base = dict(outcome_checkpoints_hours="1,6,24", checkpoint_grace_seconds=600,
                outcome_log_file=str(Path(tmp) / "outcomes.jsonl"), missing_price_limit=3,
                sell_fee_pct=1.5, gas_cost_usd=0.05, log_file=str(Path(tmp) / "trades.csv"),
                track_skipped_outcomes=True, track_bought_outcomes=True, unresolved_close_hours=24)
    base.update(over)
    return SimpleNamespace(**base)


def position_state(**pos_over):
    pos = {"chain": "solana", "symbol": "TEST", "entry_price": 1.0, "tokens": 3.0, "invested": 3.0,
           "sl_price": 0.8, "tp_price": 1.5, "score": 5, "opened_at": "2026-09-01T00:00:00+00:00"}
    pos.update(pos_over)
    return {"cash": 97.0, "starting_pot": 100.0, "closed_trades": [], "positions": {"TokenA": pos}}


class UnresolvedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = args_for(self.tmp.name)

    def run_ticks(self, state, price, n=1):
        with patch.object(bot, "get_current_prices", return_value={"TokenA": price}), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(n):
                bot.check_positions(state, self.args)

    def test_fresh_unquotable_position_stays_open_and_is_timestamped(self):
        state = position_state()
        self.run_ticks(state, None, n=5)
        self.assertIn("TokenA", state["positions"])
        self.assertIn("unquotable_since", state["positions"]["TokenA"])
        self.assertFalse(state["closed_trades"])

    def test_unquotable_past_limit_closes_at_zero_with_policy_reason(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
        state = position_state(unquotable_since=since)
        self.run_ticks(state, None)
        self.assertNotIn("TokenA", state["positions"])
        trade = state["closed_trades"][0]
        self.assertTrue(trade["reason"].startswith("unresolved:"))
        self.assertNotIn("rugged", trade["reason"])  # must not be swept up by the legacy cluster filter
        self.assertEqual(trade["exit_price"], 0.0)
        self.assertEqual(trade["pnl"], -3.0)
        self.assertEqual(state["cash"], 97.0)  # zero proceeds, gas floored at 0

    def test_limit_zero_never_closes(self):
        self.args.unresolved_close_hours = 0
        since = (datetime.now(timezone.utc) - timedelta(hours=1000)).isoformat(timespec="seconds")
        state = position_state(unquotable_since=since)
        self.run_ticks(state, None, n=3)
        self.assertIn("TokenA", state["positions"])

    def test_a_price_clears_the_unquotable_clock(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat(timespec="seconds")
        state = position_state(unquotable_since=since)
        self.run_ticks(state, 1.0)
        self.assertNotIn("unquotable_since", state["positions"]["TokenA"])

    def test_fetch_failure_does_not_start_or_advance_the_clock(self):
        state = position_state()
        self.run_ticks(state, a.FETCH_FAILED, n=3)
        self.assertNotIn("unquotable_since", state["positions"]["TokenA"])


class BoughtCohortTests(unittest.TestCase):
    def test_bought_candidates_are_tracked_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = args_for(tmp)
            state = {"pending_outcomes": {}}
            result = {"price_usd": "1", "score": 10, "symbol": "X"}
            bot.track_candidate_for_outcomes(state, "solana", "TokenB", result, "BUY 3.00 @ 1", args, {"candidate_id": "c1", "timestamp": "2026-09-07T00:00:00+00:00"})
            self.assertIn("TokenB", state["pending_outcomes"])
            self.assertTrue(state["pending_outcomes"]["TokenB"]["verdict"].startswith("BUY"))
            args.track_bought_outcomes = False
            state = {"pending_outcomes": {}}
            bot.track_candidate_for_outcomes(state, "solana", "TokenB", result, "BUY 3.00 @ 1", args)
            self.assertNotIn("TokenB", state["pending_outcomes"])


class StateLockTests(unittest.TestCase):
    def test_second_writer_is_refused_until_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "state.json")
            self.assertTrue(bot.acquire_state_lock(state_file))
            first = bot._STATE_LOCK_HANDLE
            try:
                # a second acquisition from a fresh handle must fail while the first is held
                bot._STATE_LOCK_HANDLE = None
                self.assertFalse(bot.acquire_state_lock(state_file))
            finally:
                bot._STATE_LOCK_HANDLE = first
                bot.release_state_lock()
            self.assertTrue(bot.acquire_state_lock(state_file))
            bot.release_state_lock()


class FetchPacingTests(unittest.TestCase):
    def test_429_is_retried_then_succeeds(self):
        calls = []
        payload = io.BytesIO(b'{"ok": true}')
        payload.__enter__ = lambda s: s
        payload.__exit__ = lambda s, *e: None

        def fake_urlopen(req, timeout=12):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
            return payload

        with patch.object(a.urllib.request, "urlopen", side_effect=fake_urlopen), patch.object(a.time, "sleep"):
            self.assertEqual(a.fetch_json("https://api.dexscreener.com/x"), {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_404_is_not_retried(self):
        calls = []

        def fake_urlopen(req, timeout=12):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with patch.object(a.urllib.request, "urlopen", side_effect=fake_urlopen), patch.object(a.time, "sleep"):
            self.assertIsNone(a.fetch_json("https://api.dexscreener.com/x"))
        self.assertEqual(len(calls), 1)

    def test_hosts_are_paced_independently(self):
        slept = []
        with patch.object(a.time, "sleep", side_effect=lambda s: slept.append(s)), \
             patch.object(a.time, "monotonic", side_effect=[100.0, 100.0, 100.0, 100.0]):
            a._LAST_CALL_AT.clear()
            a._pace("api.geckoterminal.com")   # first call: no wait
            a._pace("api.dexscreener.com")     # different host: no wait
        self.assertEqual(slept, [])
        with patch.object(a.time, "sleep", side_effect=lambda s: slept.append(s)), \
             patch.object(a.time, "monotonic", side_effect=[100.5, 100.5]):
            a._pace("api.geckoterminal.com")   # 0.5s after the last gecko call: must wait ~1.6s
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 1.6, places=3)


class DexscreenerShapeTests(unittest.TestCase):
    def test_null_pairs_is_a_complete_no_listing_answer(self):
        # Observed live for an unknown mint: {"schemaVersion": "1.0.0", "pairs": null}
        with patch.object(a, "fetch_json", return_value={"schemaVersion": "1.0.0", "pairs": None}), patch.object(a.time, "sleep"):
            self.assertIsNone(a.get_current_prices("solana", ["TokenA"])["TokenA"])
        with patch.object(a, "fetch_json", return_value=None), patch.object(a.time, "sleep"):
            self.assertIs(a.get_current_prices("solana", ["TokenA"])["TokenA"], a.FETCH_FAILED)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ObservationCadenceTests(unittest.TestCase):
    """1.6.1. Measured live: of 4,475 due checkpoints, 2 were priced and 2,570
    were logged missed, because checkpoints were only examined inside the
    discovery branch and a paced discovery cycle runs far longer than the 600s
    grace window."""

    def test_checkpoints_are_checked_every_tick_not_only_on_discovery(self):
        import inspect
        src = inspect.getsource(bot.main)
        loop = src[src.index("while True:"):]
        check_at = loop.index("commit_operation(check_pending_outcomes)")
        discover_at = loop.index("if discover:")
        self.assertLess(check_at, discover_at,
                        "check_pending_outcomes must run before/outside the discovery branch")
        # and it must not ALSO be gated inside that branch
        self.assertNotIn("commit_operation(check_pending_outcomes)", loop[discover_at:])


class ImplausibleQuoteTests(unittest.TestCase):
    """1.6.2. Seen live: PUMP quoted at ~5,900x entry for days. The trading loop
    correctly refused to act on it, but marked it observed and cleared the
    clock first, so it could never close; and --status valued it, reporting
    +18,227% equity from a single position."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = args_for(self.tmp.name)

    def tick(self, state, price, n=1):
        with patch.object(bot, "get_current_prices", return_value={"TokenA": price}), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(n):
                bot.check_positions(state, self.args)

    def test_implausible_quote_never_trades_and_starts_its_own_clock(self):
        state = position_state()
        self.tick(state, 5000.0, n=3)   # 5000x entry of 1.0
        pos = state["positions"]["TokenA"]
        self.assertEqual(pos["price_status"], "implausible_price")
        self.assertIn("implausible_since", pos)
        self.assertFalse(state["closed_trades"])        # not taken as profit
        self.assertEqual(pos.get("max_price_seen", 1.0), 1.0)  # watermark untouched

    def test_persistent_implausible_quote_resolves_after_the_limit(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
        state = position_state(implausible_since=since)
        self.tick(state, 5000.0)
        trade = state["closed_trades"][0]
        self.assertTrue(trade["reason"].startswith("unresolved: no plausible quote"))
        self.assertEqual(trade["exit_price"], 0.0)

    def test_a_plausible_quote_clears_the_implausible_clock(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat(timespec="seconds")
        state = position_state(implausible_since=since)
        self.tick(state, 1.1)
        self.assertNotIn("implausible_since", state["positions"]["TokenA"])
        self.assertEqual(state["positions"]["TokenA"]["price_status"], "observed")

    def test_status_does_not_value_an_implausible_quote(self):
        state = position_state()
        with patch.object(bot, "get_current_price", return_value=5000.0):
            s = bot.portfolio_summary(state)
        self.assertEqual(s["unpriced"], ["TokenA"])
        self.assertTrue(math.isnan(s["equity"]))          # honest total stays unknown
        self.assertEqual(s["unpriced_cost"], 3.0)
        self.assertAlmostEqual(s["equity_priced"], 97.0)  # cash only; the broken position excluded

    def test_priced_equity_excludes_only_the_broken_position(self):
        state = position_state()
        state["positions"]["TokenB"] = dict(state["positions"]["TokenA"], symbol="OK")
        state["cash"] = 94.0

        def quote(chain, addr):
            return 5000.0 if addr == "TokenA" else 1.5   # A broken, B genuinely up 50%
        with patch.object(bot, "get_current_price", side_effect=quote):
            s = bot.portfolio_summary(state)
        self.assertEqual(s["unpriced"], ["TokenA"])
        self.assertAlmostEqual(s["unrealized_priced"], 1.5)          # 3 tokens x 1.5 - 3.00
        self.assertAlmostEqual(s["equity_priced"], 94.0 + 3.0 + 1.5)  # cash + B's cost + B's gain
