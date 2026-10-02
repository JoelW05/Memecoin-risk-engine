"""Regressions for the cash-reconciliation repair (repair_cash.py) and the
corrupt-state warning, using the real TOSHI numbers that caused the problem."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paperbot as bot
import repair_cash as rc


def trade(symbol, entry, exit_, invested, proceeds, reason="take-profit hit"):
    return {"chain": "solana", "symbol": symbol, "address": symbol, "entry_price": entry,
            "exit_price": exit_, "invested": invested, "proceeds": proceeds,
            "pnl": round(proceeds - invested, 2), "reason": reason,
            "opened_at": "2026-08-23T00:00:00+00:00", "closed_at": "2026-08-23T12:00:00+00:00", "score": 10}


# The actual trade from the state file: $3 at 0.0001324 buys ~21,941 tokens,
# and a fabricated 79,806.68 quote turns that into ~$1.72bn of "proceeds".
TOSHI = trade("TOSHI", 0.0001324, 79806.68, 3.00, 1724714000.00)
GOOD_WIN = trade("WIN", 1.0, 1.5, 3.00, 4.40)
GOOD_LOSS = trade("LOSS", 1.0, 0.8, 3.00, 2.30, "stop-loss hit")


def state_with(closed, positions=1, cash=1724792700.07, pot=100.0):
    pos = {f"Open{i}": {"chain": "solana", "symbol": f"O{i}", "entry_price": 1.0, "tokens": 3.0,
                        "invested": 3.00, "sl_price": 0.8, "tp_price": 1.5, "score": 5,
                        "opened_at": "2026-09-01T00:00:00+00:00"} for i in range(positions)}
    return {"starting_pot": pot, "cash": cash, "positions": pos, "closed_trades": list(closed),
            "seen_addresses": [], "pending_outcomes": {}}


class ArtefactDetectionTests(unittest.TestCase):
    def test_toshi_is_detected_by_price_ratio(self):
        self.assertIn("fabricated quote", rc.artefact_reason(TOSHI))

    def test_impossible_proceeds_detected_even_if_prices_look_sane(self):
        # a trade whose prices were never recorded, but whose cash credit is absurd
        t = trade("X", None, None, 3.00, 900000.0)
        self.assertIn("impossible", rc.artefact_reason(t))

    def test_ordinary_trades_are_never_voided(self):
        for t in (GOOD_WIN, GOOD_LOSS):
            self.assertIsNone(rc.artefact_reason(t))

    def test_a_genuine_hundredfold_win_is_kept(self):
        # 100x is possible in this market; the bound is 1000x for a reason
        self.assertIsNone(rc.artefact_reason(trade("MOON", 0.001, 0.1, 3.00, 295.0)))


class RebuildTests(unittest.TestCase):
    def test_cash_is_rebuilt_from_pot_positions_and_real_pnl(self):
        state = state_with([TOSHI, GOOD_WIN, GOOD_LOSS], positions=2)
        cash, kept, voided = rc.rebuild(state)
        # 100 pot - 6 invested in 2 open + (1.40 - 0.70) realised
        self.assertEqual(cash, 94.70)
        self.assertEqual([t["symbol"] for t in kept], ["WIN", "LOSS"])
        self.assertEqual([t["symbol"] for t, _ in voided], ["TOSHI"])

    def test_rebuild_does_not_mutate_state(self):
        state = state_with([TOSHI, GOOD_WIN])
        before = json.dumps(state, sort_keys=True)
        rc.rebuild(state)
        self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_clean_state_reconciles_exactly(self):
        state = state_with([GOOD_WIN], positions=1, cash=98.40)
        cash, _, voided = rc.rebuild(state)
        self.assertEqual(cash, 98.40)
        self.assertFalse(voided)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "paperbot_state.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(state_with([TOSHI, GOOD_WIN, GOOD_LOSS], positions=2), f)

    def run_repair(self, *extra):
        with patch.object(sys, "argv", ["repair_cash.py", "--state", self.path, *extra]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc.main()
        return buf.getvalue(), json.load(open(self.path, encoding="utf-8"))

    def test_dry_run_writes_nothing(self):
        out, after = self.run_repair("--dry-run")
        self.assertIn("DRY RUN", out)
        self.assertEqual(after["cash"], 1724792700.07)
        self.assertEqual(len(after["closed_trades"]), 3)

    def test_apply_fixes_cash_and_moves_artefacts_to_audit_list(self):
        out, after = self.run_repair()
        self.assertEqual(after["cash"], 94.70)
        self.assertEqual([t["symbol"] for t in after["closed_trades"]], ["WIN", "LOSS"])
        self.assertEqual([t["symbol"] for t in after["voided_trades"]], ["TOSHI"])
        self.assertIn("voided_reason", after["voided_trades"][0])
        self.assertTrue(any(p.name.startswith("paperbot_state.json.backup-") for p in Path(self.tmp.name).iterdir()))
        # open positions and their money are untouched
        self.assertEqual(len(after["positions"]), 2)

    def test_second_run_is_a_no_op(self):
        self.run_repair()
        out, after = self.run_repair()
        self.assertIn("already reconciles", out)
        self.assertEqual(after["cash"], 94.70)


class ResetTradingTests(unittest.TestCase):
    """The state cannot be reconciled when the affordability constraint was
    broken: 60 positions on a 100 pot is not a state a valid run could reach.
    --reset-trading archives that history and restarts trading only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path.cwd()
        self.addCleanup(lambda: __import__("os").chdir(self.cwd))
        __import__("os").chdir(self.tmp.name)
        self.path = "paperbot_state.json"
        state = state_with([TOSHI, GOOD_WIN, GOOD_LOSS], positions=40)
        state["pending_outcomes"] = {"P1": {"chain": "solana", "discovered_at": "2026-09-07T00:00:00+00:00"}}
        state["seen_addresses"] = ["A", "B", "C"]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def run_repair(self, *extra):
        with patch.object(sys, "argv", ["repair_cash.py", "--state", self.path, *extra]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc.main()
        return buf.getvalue(), json.load(open(self.path, encoding="utf-8"))

    def test_over_committed_state_rebuilds_to_impossible_negative_cash(self):
        # 100 pot - 120 invested + 0.70 realised: proof the constraint was broken
        out, _ = self.run_repair("--dry-run")
        self.assertIn("-19.30", out)

    def test_dry_run_reset_writes_nothing(self):
        out, after = self.run_repair("--reset-trading", "--dry-run")
        self.assertIn("RESET", out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(len(after["positions"]), 40)
        self.assertEqual(len(after["closed_trades"]), 3)

    def test_reset_clears_trading_preserves_research_and_archives(self):
        out, after = self.run_repair("--reset-trading")
        self.assertEqual(after["cash"], 100.0)
        self.assertEqual(after["positions"], {})
        self.assertEqual(after["closed_trades"], [])
        self.assertIn("trading_restarted_at", after)
        # research data survives untouched
        self.assertEqual(list(after["pending_outcomes"]), ["P1"])
        self.assertEqual(after["seen_addresses"], ["A", "B", "C"])
        archives = list(Path(".").glob("paperbot_trading_archive-*.json"))
        self.assertEqual(len(archives), 1)
        archived = json.loads(archives[0].read_text(encoding="utf-8"))
        self.assertEqual(len(archived["positions"]), 40)
        self.assertEqual(len(archived["closed_trades"]), 3)
        self.assertEqual([t["symbol"] for t in archived["voided_trades"]], ["TOSHI"])
        self.assertTrue(any(p.name.startswith("paperbot_state.json.backup-") for p in Path(".").iterdir()))


class StartupWarningTests(unittest.TestCase):
    def test_warning_fires_only_for_impossible_cash(self):
        for cash, expected in ((1724792700.07, True), (95.0, False), (100.0 * bot.IMPLAUSIBLE_CASH_MULTIPLE + 1, True)):
            # no open positions, so --status needs no price lookups
            state = state_with([], positions=0, cash=cash)
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "s.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state, f)
                buf = io.StringIO()
                argv = ["paperbot.py", "--status", "--state-file", path]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
                    with contextlib.suppress(SystemExit):
                        bot.main()
                self.assertEqual("[WARN] cash" in buf.getvalue(), expected, f"cash={cash}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
