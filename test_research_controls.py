"""Regression tests for cohort filters and prospective wallet evaluation."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import analyze_outcomes as report
import wallet_research as wr
import paperbot as bot
from test_find_wallets import tx
from test_analyzer import make_pair, make_solana_security
import analyzer


class EvaluationBoundaryTests(unittest.TestCase):
    def test_selected_watchlist_defaults_to_prospective_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wallets.txt'
            path.write_text('# Selected at UTC: 2026-09-08T10:00:00+00:00\nA # lift=inf\n')
            self.assertEqual(wr.evaluation_start(path), datetime(2026, 9, 8, 10, tzinfo=timezone.utc))
            with self.assertRaises(ValueError):
                wr.evaluation_start(path, '2026-09-07')
            self.assertEqual(wr.evaluation_start(path, '2026-09-09'), datetime(2026, 9, 9, tzinfo=timezone.utc))

    def test_unmarked_watchlist_is_exploratory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wallets.txt'
            path.write_text('A\n')
            self.assertIsNone(wr.evaluation_start(path))

    def test_conflicting_selection_metadata_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wallets.txt'
            path.write_text('# Selected at UTC: 2026-09-08\n# Selected at UTC: 2026-09-09\nA\n')
            with self.assertRaises(ValueError):
                wr.evaluation_start(path)

    def test_main_filters_preselection_trades(self):
        start = datetime.now(timezone.utc) - timedelta(days=1)
        before = tx('A', 'M', 10, start.timestamp() - 10, 'old')
        after = tx('A', 'M', 10, start.timestamp() + 10, 'new')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wallets.txt'
            path.write_text(f'# Selected at UTC: {start.isoformat()}\nA # inline metadata\n')
            with patch('sys.argv', ['wallet_research.py', '--wallets', str(path), '--no-cache']), patch.dict(os.environ, {'HELIUS_API_KEY': 'test-key'}), patch.object(wr, 'fetch_wallet_transactions', return_value=[before, after]) as fetch, patch.object(wr, 'fifo_pnl', return_value=([], {}, 0)) as fifo, contextlib.redirect_stdout(io.StringIO()):
                wr.main()
            self.assertEqual([s['sig'] for s in fifo.call_args.args[0]], ['new'])
            self.assertEqual(fetch.call_args.kwargs['since_ts'], start.timestamp())

    def test_partial_cached_week_is_excluded(self):
        wallets = {str(i): {'2025-W01': i, '2025-W02': i} for i in range(4)}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            wr.persistence_report(wallets, available_until=datetime(2025, 1, 8, tzinfo=timezone.utc))
        self.assertIn('Fewer than 2 weeks', output.getvalue())


class ReportCohortTests(unittest.TestCase):
    def test_exact_chain_and_version_filters(self):
        row = {'chain': 'base', 'analyzer_version': '1.2.0', 'paperbot_version': '1.5.0'}
        self.assertTrue(report.cohort_match(row, 'base', '1.2.0', '1.5.0'))
        self.assertFalse(report.cohort_match(row, 'solana'))
        self.assertFalse(report.cohort_match(row, paperbot_version=bot.PAPERBOT_VERSION))

    def test_same_token_multiple_horizons_not_an_outage_cluster(self):
        row = {'chain': 'solana', 'address': 'A', 'checked_at': '2026-09-01T00:00:00Z', 'rugged_or_delisted': True}
        rows = [row | {'checkpoint_hours': h} for h in (1, 6, 24)]
        self.assertEqual(report.filter_outage_clusters(rows)[1], 0)
        self.assertEqual(report.filter_outage_clusters(rows + [row | {'address': 'B'}])[1], 4)

    def test_missing_timestamp_does_not_manufacture_cluster(self):
        rows = [{'chain': 'solana', 'address': x, 'rugged_or_delisted': True} for x in ('A', 'B')]
        self.assertEqual(report.filter_outage_clusters(rows)[1], 0)

    def test_equal_losses_have_nonzero_downside_risk(self):
        trades = [{'invested': 100, 'proceeds': 100 + pnl, 'pnl': pnl, 'reason': 'stop-loss hit', 'score': 10,
                   'entry_price': 1, 'exit_price': 1 + pnl / 100, 'symbol': str(i)} for i, pnl in enumerate((-10, -10, 5))]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report.closed_trade_performance({'closed_trades': trades, 'starting_pot': 100})
        self.assertIn('Sortino-like): -0.61', output.getvalue())

    def test_until_and_chain_applied_to_cli_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for chain in ('base', 'solana'):
                for day in (2, 4):
                    rows.append({'chain': chain, 'address': chain, 'discovered_at': '2026-09-01T00:00:00Z',
                                 'checked_at': f'2026-09-0{day}T00:00:00Z', 'checkpoint_hours': 24,
                                 'observation_status': 'observed', 'price_change_pct': 5, 'score_at_discovery': 10})
            path = root / 'outcomes.jsonl'
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            argv = ['analyze_outcomes.py', '--outcomes', str(path), '--candidates', str(root / 'none'), '--state', str(root / 'none'), '--chain', 'base', '--until', '2026-09-03']
            output = io.StringIO()
            with patch('sys.argv', argv), contextlib.redirect_stdout(output):
                report.main()
            self.assertIn('1 total checkpoint rows logged', output.getvalue())


class BotIntegrationTests(unittest.TestCase):
    def test_readonly_reset_is_refused_before_lock(self):
        with patch('sys.argv', ['paperbot.py', '--status', '--reset']), patch.object(bot, 'acquire_state_lock') as lock, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bot.main()
            lock.assert_not_called()

    def test_output_path_collision_is_refused(self):
        with patch('sys.argv', ['paperbot.py', '--state-file', 'same', '--log-file', './same']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bot.main()

    def test_real_buy_sell_cycle_commits_all_outputs(self):
        result = analyzer.analyze('solana', 'TokenA', [make_pair(price_usd='1')], make_solana_security(), {'risks': []}, None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = ['paperbot.py', '--once', '--state-file', str(root / 'state.json'), '--log-file', str(root / 'trades.csv'),
                    '--candidate-log-file', str(root / 'candidates.jsonl'), '--outcome-log-file', str(root / 'outcomes.jsonl')]
            try:
                with patch('sys.argv', argv), patch.object(bot, 'fetch_trending_candidates', return_value=[{'tokenAddress': 'TokenA'}]), patch.object(bot, 'full_check', return_value=('solana', 'TokenA', result)), patch.object(bot, 'get_current_prices', return_value={'TokenA': 1.6}), contextlib.redirect_stdout(io.StringIO()):
                    bot.main()
            finally:
                bot.release_state_lock()
            state = json.loads((root / 'state.json').read_text())
            bot.validate_trading_cash(state)
            self.assertEqual(len(state['closed_trades']), 1)
            self.assertFalse(state['positions'])
            self.assertEqual(state['closed_trades'][0]['exit_paperbot_version'], bot.PAPERBOT_VERSION)
            candidate = json.loads((root / 'candidates.jsonl').read_text())
            self.assertTrue(candidate['bought'])
            self.assertEqual(candidate['candidate_id'], state['pending_outcomes']['TokenA']['candidate_id'])
            self.assertEqual(candidate['config']['buy_fee_pct'], 1.5)
            self.assertEqual(len((root / 'trades.csv').read_text().splitlines()), 3)
            self.assertFalse((root / 'state.json.txn').exists())


if __name__ == '__main__':
    unittest.main()


class RuleDelistingTests(unittest.TestCase):
    """2a is the per-rule successor to the old rug%: section 2 can only see
    priced rows, so it cannot attribute the bucket gradient to any rule."""

    @staticmethod
    def _comp(rule, fired):
        return {"rule": rule, "severity": "high" if fired else "good", "points": 10 if fired else 0}

    def _dataset(self, fired_gone, clean_gone, n=100, noise_rows=0):
        candidates, raw = [], []
        for side, rate in (("fired", fired_gone), ("clean", clean_gone)):
            for k in range(n):
                cid = f"{side}{k}"
                candidates.append({"chain": "solana", "address": cid, "timestamp": "2026-09-10T00:00:00+00:00",
                                   "candidate_id": cid, "score_components": [self._comp("R", side == "fired")]})
                raw.append({"chain": "solana", "address": cid, "candidate_id": cid, "checkpoint_hours": 24,
                            "score_at_discovery": 50,
                            "observation_status": "no_pairs" if k < round(n * rate) else "observed"})
        for j in range(noise_rows):  # must be ignored entirely
            cid = f"noise{j}"
            candidates.append({"chain": "solana", "address": cid, "timestamp": "2026-09-10T00:00:00+00:00",
                               "candidate_id": cid, "score_components": [self._comp("R", True)]})
            raw.append({"chain": "solana", "address": cid, "candidate_id": cid, "checkpoint_hours": 24,
                        "score_at_discovery": 50, "observation_status": "missed_checkpoint"})
        return raw, report.index_candidates_by_address(candidates)

    def _run(self, raw, indexed, min_n=50):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report.rule_level_delisting(raw, indexed, min_n=min_n)
        return buf.getvalue()

    def test_planted_rates_and_lift_are_recovered(self):
        out = self._run(*self._dataset(fired_gone=0.80, clean_gone=0.10))
        line = next(l for l in out.splitlines() if l.strip().startswith("R "))
        self.assertIn("80.0%", line)
        self.assertIn("10.0%", line)
        self.assertIn("8.00x", line)

    def test_missed_observations_are_excluded_from_both_sides(self):
        out = self._run(*self._dataset(fired_gone=0.80, clean_gone=0.10, noise_rows=500))
        line = next(l for l in out.splitlines() if l.strip().startswith("R "))
        self.assertIn("80.0%", line)          # unchanged by 500 missed rows
        self.assertIn(" 100 ", f" {line} ")   # denominator stays 100, not 600

    def test_inverted_rule_reports_lift_below_one(self):
        out = self._run(*self._dataset(fired_gone=0.05, clean_gone=0.50))
        line = next(l for l in out.splitlines() if l.strip().startswith("R "))
        self.assertIn("0.10x", line)

    def test_thin_rules_are_omitted(self):
        out = self._run(*self._dataset(fired_gone=0.5, clean_gone=0.5, n=10), min_n=50)
        self.assertIn("No rule has 50+ resolved observations", out)
