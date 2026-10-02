"""Offline regressions for the 8 September source review."""
import contextlib
import io
import json
import math
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import find_wallets as fw
import wallet_research as wr
import paperbot as bot
from test_find_wallets import tx, outcome, T
import test_find_wallets as original_fw_tests
from test_policy import position_state, args_for


class CashGuards(unittest.TestCase):
    def test_invalid_fee_refused_before_reset_or_io(self):
        for option in ('nan', '-1', '100'):
            with patch('sys.argv', ['paperbot.py', '--buy-fee-pct', option, '--reset']), patch.object(bot, 'acquire_state_lock') as lock, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    bot.main()
                self.assertEqual(caught.exception.code, 2)
                lock.assert_not_called()

    def test_valid_ledger(self):
        bot.validate_trading_cash(position_state())

    def test_bad_cash_is_blocked_before_entry(self):
        for cash in (float('nan'), float('inf'), -1, 1_724_792_700, 98):
            state = position_state()
            state['cash'] = cash
            before = json.dumps(state)
            with self.subTest(cash=cash), self.assertRaises(ValueError):
                bot.try_open_position(state, 'solana', 'X', {}, None, [0])
            self.assertEqual(json.dumps(state), before)

    def test_closed_trade_math_checked(self):
        state = position_state()
        state['closed_trades'] = [{'invested': 3, 'proceeds': 4, 'pnl': 99}]
        state['cash'] += 99
        with self.assertRaises(ValueError):
            bot.validate_trading_cash(state)

    def test_outage_breaks_missing_listing_interval(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        state = position_state(unquotable_since=since)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            args = args_for(tmp)
            with patch.object(bot, 'get_current_prices', return_value={'TokenA': bot.FETCH_FAILED}):
                bot.check_positions(state, args)
            with patch.object(bot, 'get_current_prices', return_value={'TokenA': None}):
                bot.check_positions(state, args)
        self.assertIn('TokenA', state['positions'])
        self.assertFalse(state['closed_trades'])


class WalletInputAndParsing(unittest.TestCase):
    def test_generated_watchlist_is_read_without_comments_or_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wallets.txt'
            path.write_text('\ufeff# header\n  # comment\nWalletA # winners=2\nWalletA\n WalletB \n', encoding='utf-8')
            self.assertEqual(wr.read_wallets(path), ['WalletA', 'WalletB'])

    def test_airdrop_with_fee_is_not_a_purchase(self):
        transfer = tx('A', 'M', 5, T)
        transfer['nativeTransfers'] = []
        transfer['fee'] = 5000
        self.assertIsNone(wr.parse_swap(transfer, 'A'))
        self.assertIsNone(fw.buyer_of(transfer, 'M'))

    def test_failed_transaction_is_not_a_swap(self):
        transfer = tx('A', 'M', 5, T)
        transfer['transactionError'] = {'error': 'failed'}
        self.assertIsNone(wr.parse_swap(transfer, 'A'))
        self.assertIsNone(fw.buyer_of(transfer, 'M'))

    def test_payment_and_fees_on_buy(self):
        transfer = tx('A', 'M', 5, T)
        transfer['fee'] = 5000
        self.assertAlmostEqual(wr.parse_swap(transfer, 'A')['sol'], 1.000005)

    def test_self_transfer_is_not_a_buy(self):
        transfer = tx('A', 'M', 5, T)
        transfer['tokenTransfers'][0]['fromUserAccount'] = 'A'
        self.assertIsNone(fw.buyer_of(transfer, 'M'))

    def test_nonfinite_and_malformed_amounts_are_rejected(self):
        for amount in (float('nan'), float('inf'), {}, 'bad'):
            transfer = tx('A', 'M', amount, T)
            self.assertIsNone(wr.parse_swap(transfer, 'A'))

    def test_private_key_is_redacted_from_network_errors(self):
        key = 'secret-value'
        error = urllib.error.URLError('failed https://example.test/?api-key=' + key)
        buf = io.StringIO()
        with patch.object(wr.urllib.request, 'urlopen', side_effect=error), patch.object(wr.time, 'sleep'), contextlib.redirect_stderr(buf):
            with self.assertRaises(RuntimeError) as caught:
                wr.helius_get('https://example.test/', {}, key, retries=1)
        self.assertNotIn(key, str(caught.exception) + buf.getvalue())


class CoverageAndSelection(unittest.TestCase):
    def test_later_winner_does_not_replace_earlier_control(self):
        early = outcome('A', -20)
        late = outcome('A', 400) | {'discovered_at': datetime.fromtimestamp(T + 86400, timezone.utc).isoformat()}
        for rows in ([early, late], [late, early]):
            winners, controls = fw.split_winners_and_controls(rows, 24, 50)
            self.assertFalse(winners)
            self.assertEqual(controls[0]['pct'], -20)

    def test_buy_before_window_is_excluded(self):
        batch = [tx('IN', 'M', 1, T - 10, 'a'), tx('OLD', 'M', 1, T - 1000, 'b')]
        with patch.object(fw, 'helius_get', return_value=batch):
            buyers, complete, _ = fw.early_buyers('M', T, 'key', 1, 3600, 0)
        self.assertEqual(buyers, {'IN'})
        self.assertTrue(complete)

    def test_error_envelope_is_not_empty_history(self):
        with patch.object(fw, 'helius_get', return_value={'error': 'failure'}):
            with self.assertRaises(RuntimeError):
                fw.early_buyers('M', T, 'key', 1, 3600, 0)

    def test_unordered_history_is_rejected(self):
        with self.assertRaises(RuntimeError):
            wr.validate_history_page([tx('A', 'M', 1, T, 'a'), tx('A', 'M', 1, T + 1, 'b')])

    def test_wallet_budget_exhaustion_is_explicit(self):
        now = time.time()
        batch = [tx('A', 'M', 1, now, 'a'), tx('A', 'M', 1, now - 1, 'b')]
        with patch.object(wr, 'PAGE_SIZE', 2), patch.object(wr, 'helius_get', return_value=batch), patch.object(wr.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                wr.fetch_wallet_transactions('A', 'key', 28, 1)

    def test_wallet_pages_are_deduplicated(self):
        now = time.time()
        first = [tx('A', 'M', 1, now, 'a'), tx('A', 'M', 1, now - 1, 'b')]
        second = [first[-1], tx('A', 'M', 1, now - 40 * 86400, 'c')]
        with patch.object(wr, 'PAGE_SIZE', 2), patch.object(wr, 'helius_get', side_effect=[first, second]), patch.object(wr.time, 'sleep'):
            rows = wr.fetch_wallet_transactions('A', 'key', 28, 2)
        self.assertEqual([r['signature'] for r in rows], ['a', 'b'])

    def test_cache_reuse_is_bounded_and_parameter_specific(self):
        entry = {'schema': 1, 'days': 28, 'max_pages': 15, 'fetched_at': 100, 'transactions': []}
        self.assertTrue(wr.cache_valid(entry, 28, 15, 6, now=101))
        self.assertFalse(wr.cache_valid(entry, 7, 15, 6, now=101))
        self.assertFalse(wr.cache_valid(entry, 28, 20, 6, now=101))
        self.assertFalse(wr.cache_valid(entry, 28, 15, 6, now=100000))
        self.assertFalse(wr.cache_valid([], 28, 15, 6, now=101))

    def test_gap_between_weeks_not_presented_as_next_week(self):
        wallets = {str(i): {'2025-W01': i, '2025-W03': i} for i in range(4)}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wr.persistence_report(wallets)
        self.assertNotIn('Average rank', buf.getvalue())

    def test_tied_groups_have_correct_chance_baseline(self):
        wallets = {str(i): {'2025-W01': int(i == 3), '2025-W02': int(i == 3)} for i in range(4)}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wr.persistence_report(wallets)
        self.assertIn('25.0%', buf.getvalue())
        self.assertNotIn('50% = pure chance', buf.getvalue())


# Extend the existing end-to-end fixture rather than invoking live Helius.
class WatchlistFailures(unittest.TestCase):
    setUp = original_fw_tests.EndToEndTests.setUp
    history = original_fw_tests.EndToEndTests.history
    run_main = original_fw_tests.EndToEndTests.run_main
    def test_no_control_coverage_does_not_overwrite_watchlist(self):
        Path('wallets.txt').write_text('OLD\n')
        with patch.object(fw, 'scan', side_effect=[({'A': {'WIN1', 'WIN2'}}, 2, []), ({}, 0, [])]):
            self.run_main()
        self.assertEqual(Path('wallets.txt').read_text(), 'OLD\n')

    def test_completed_empty_scan_clears_old_watchlist(self):
        Path('wallets.txt').write_text('OLD\n')
        self.run_main('--min-winners', '5')
        self.assertEqual(wr.read_wallets('wallets.txt'), [])


if __name__ == '__main__':
    unittest.main()
