"""Tests for trade_review.py on a hand-built state with known answers."""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import trade_review as tr

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def position(days_ago, invested=2.0, entry=1.0, sl=0.7, tp=1.5):
    return {"opened_at": iso(NOW - timedelta(days=days_ago)), "invested": invested,
            "entry_price": entry, "sl_price": sl, "tp_price": tp}


def trade(opened_days_ago, hold_hours, reason, pnl, runup=None, drawdown=None):
    opened = NOW - timedelta(days=opened_days_ago)
    return {"opened_at": iso(opened), "closed_at": iso(opened + timedelta(hours=hold_hours)), "reason": reason,
            "pnl": pnl, "max_runup_pct": runup, "max_drawdown_pct": drawdown}


STATE = {
    "cash": 0.69,
    "positions": {"a": position(0.5), "b": position(2), "c": position(5, sl=0.6, tp=2.0), "d": position(10, invested=3.0)},
    "closed_trades": [
        trade(3, 2, "take-profit hit", 1.5, runup=60, drawdown=-35),
        trade(3, 4, "take-profit hit", 2.0, runup=80, drawdown=-5),
        trade(4, 30, "stop-loss hit", -1.0, runup=25, drawdown=-40),
        trade(5, 50, "stop-loss hit", -1.2, runup=3, drawdown=-45),
        trade(6, 20, "unresolved: no indexed listing for 24h, valued at zero (not a verified rug)", -2.0),
        trade(30, 1, "take-profit hit", 9.9, runup=90, drawdown=0),  # before --since: must be ignored
    ],
}


class TradeReviewTests(unittest.TestCase):
    def test_open_book_bands(self):
        rows = {label: (n, inv, sl, tp) for label, n, inv, sl, tp in tr.open_book(STATE, NOW)}
        self.assertEqual(rows["<1 day"][:2], (1, 2.0))
        self.assertEqual(rows["3-7 days"][0], 1)
        self.assertAlmostEqual(rows["3-7 days"][2], 40.0)   # SL 0.6 on entry 1.0
        self.assertAlmostEqual(rows["3-7 days"][3], 100.0)  # TP 2.0 on entry 1.0
        self.assertEqual(rows["7+ days"][:2], (1, 3.0))

    def test_buys_per_day_counts_open_and_closed(self):
        since = tr.utc("2026-09-07T20:47")
        days = dict(tr.buys_per_day(STATE, since))
        self.assertEqual(sum(days.values()), 4 + 5)  # the 30-day-old trade is before --since
        self.assertEqual(days[(NOW - timedelta(days=3)).date()], 2)

    def test_closed_summary_groups_unresolved_and_reports_hold(self):
        recent = STATE["closed_trades"][:5]
        summary = tr.closed_summary(recent)
        self.assertEqual(summary["take-profit hit"][:2], (2, 3.5))
        self.assertEqual(summary["take-profit hit"][3], 3.0)   # median of 2h and 4h
        self.assertEqual(summary["stop-loss hit"][3], 40.0)
        self.assertEqual(summary["unresolved (valued at zero)"][0], 1)
        self.assertAlmostEqual(tr.profit_factor(recent), 3.5 / 4.2)

    def test_watermark_counts_are_one_sided(self):
        n_stops, runup, n_targets, drawdown = tr.watermark_counts(STATE["closed_trades"][:5])
        self.assertEqual((n_stops, n_targets), (2, 2))
        self.assertEqual(runup, {10: 1, 20: 1, 30: 0, 50: 0})
        self.assertEqual(drawdown, {10: 1, 20: 1, 30: 1, 50: 0})

    def test_trades_without_watermarks_are_skipped_not_counted(self):
        """Pre-instrumentation trades lack max_runup/drawdown; missing is not zero."""
        old = trade(3, 5, "stop-loss hit", -1.0)
        n_stops, runup, _, _ = tr.watermark_counts([old])
        self.assertEqual(n_stops, 0)
        self.assertEqual(runup[10], 0)

    def test_cli_runs_and_respects_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(STATE), encoding="utf-8")
            out = io.StringIO()
            with patch("sys.argv", ["trade_review.py", "--state", str(path)]), contextlib.redirect_stdout(out):
                tr.main()
        text = out.getvalue()
        self.assertIn("CLOSED TRADES: 5, realised -0.70", text)
        self.assertIn("4 open positions, 9.00 invested", text)


class ExitLevelTests(unittest.TestCase):
    OPENED = NOW - timedelta(days=4)

    def candidates(self):
        def row(address, vol, bought=True, seconds=2, chain="solana"):
            return {"timestamp": iso(self.OPENED + timedelta(seconds=seconds)), "chain": chain,
                    "address": address, "bought": bought, "volatility_pct": vol}
        return [row("calm", 5), row("wild", 40), row("huge", 80), row("late", 40, seconds=3600),
                row("skipped", 40, bought=False), row("0xAbC", 4, chain="base")]

    def closed(self, address, pnl=1.0, reason="take-profit hit", hold=10, chain="solana"):
        return {"chain": chain, "address": address, "opened_at": iso(self.OPENED),
                "closed_at": iso(self.OPENED + timedelta(hours=hold)), "reason": reason, "pnl": pnl}

    def test_entry_target_rebuilt_from_logged_volatility(self):
        index = tr.entry_volatility_index(self.candidates())
        self.assertEqual(tr.entry_tp_pct(self.closed("calm"), index), 30.0)    # 5 x 2.5 = 12.5 -> floor
        self.assertEqual(tr.entry_tp_pct(self.closed("wild"), index), 100.0)
        self.assertEqual(tr.entry_tp_pct(self.closed("huge"), index), 150.0)   # cap
        self.assertIsNone(tr.entry_tp_pct(self.closed("late"), index))         # an hour away: not this buy
        self.assertIsNone(tr.entry_tp_pct(self.closed("skipped"), index))      # never bought
        self.assertEqual(tr.entry_tp_pct(self.closed("0xabc", chain="base"), index), 30.0)

    def test_floor_comparison_splits_closed_and_open(self):
        index = tr.entry_volatility_index(self.candidates())
        closed = [self.closed("calm", -1.0, "stop-loss hit", hold=200), self.closed("wild", 2.0), self.closed("nobody")]
        positions = {"x": position(9, sl=0.8, tp=1.3), "y": position(2, sl=0.4, tp=2.0)}
        out = tr.floor_comparison(closed, positions, index, NOW)
        floor = out["target at 30% floor"]
        self.assertEqual((floor["closed"], floor["open"], floor["open_7d"]), (1, 1, 1))
        self.assertEqual(floor["realised"], -1.0)
        self.assertEqual(out["target above floor"]["open"], 1)
        self.assertEqual(out["unknown"]["closed"], 1)

    def test_hold_shares(self):
        closed = [self.closed("a", hold=h) for h in (10, 30, 100, 200)] + [self.closed("b", -1, "stop-loss hit", hold=5)]
        shares = tr.hold_shares(closed)
        self.assertEqual(shares["take-profit hit"][0], 4)
        self.assertEqual(shares["take-profit hit"][1], {1: 75.0, 3: 50.0, 7: 25.0})
        self.assertEqual(shares["stop-loss hit"][1][1], 0.0)

    def test_positions_without_valid_quote_are_listed(self):
        """PUMP sat on a fabricated quote for days; it must show up here, not vanish."""
        positions = {"p": dict(position(20), symbol="PUMP", price_status="implausible_price",
                               implausible_since="2026-09-21T12:00:00+00:00"),
                     "q": dict(position(1), symbol="OK", price_status="observed")}
        self.assertEqual(tr.unquoted_positions(positions),
                         [("PUMP", "implausible_price", "2026-09-21T12:00:00+00:00", positions["p"]["opened_at"])])

    def test_cli_exits_mode_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, cand_path = Path(tmp) / "state.json", Path(tmp) / "c.jsonl"
            state_path.write_text(json.dumps(STATE), encoding="utf-8")
            cand_path.write_text("".join(json.dumps(c) + "\n" for c in self.candidates()), encoding="utf-8")
            out = io.StringIO()
            with patch("sys.argv", ["trade_review.py", "--exits", "--state", str(state_path),
                                    "--candidates", str(cand_path)]), contextlib.redirect_stdout(out):
                tr.main()
        text = out.getvalue()
        self.assertIn("EXIT LEVELS", text)
        self.assertIn("OPEN POSITIONS WITHOUT A VALID QUOTE: 0", text)


if __name__ == "__main__":
    unittest.main()
