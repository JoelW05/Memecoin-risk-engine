"""Crash-point tests for recoverable single-writer state/log commits."""
import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paperbot as bot
import repair_cash
import state_store as store


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'state.json'
        self.log = self.root / 'events.jsonl'
        self.state = {'cash': 100, 'positions': {}}
        store.atomic_json(self.path, self.state)

    def pending(self):
        with patch.object(store, 'recover', side_effect=OSError('injected interruption')):
            with self.assertRaises(store.PendingCommit):
                with store.StateTransaction(self.path, self.state):
                    self.state['cash'] = 97
                    store.append_text(self.log, '{"event":1}\n')
        return json.loads(store.journal_path(self.path).read_text())

    def test_success_commits_state_and_log(self):
        with store.StateTransaction(self.path, self.state):
            self.state['cash'] = 97
            store.append_text(self.log, 'first\n')
            store.append_text(self.log, 'second\n')
        self.assertEqual(json.loads(self.path.read_text())['cash'], 97)
        self.assertEqual(self.log.read_text(), 'first\nsecond\n')
        self.assertFalse(store.journal_path(self.path).exists())

    def test_exception_rolls_back_memory_and_staged_outputs(self):
        with self.assertRaises(RuntimeError):
            with store.StateTransaction(self.path, self.state):
                self.state['positions']['A'] = 1
                store.append_text(self.log, 'uncommitted\n')
                raise RuntimeError('bad API shape')
        self.assertEqual(self.state, {'cash': 100, 'positions': {}})
        self.assertFalse(self.log.exists())

    def test_keyboard_interrupt_rolls_back(self):
        with self.assertRaises(KeyboardInterrupt):
            with store.StateTransaction(self.path, self.state):
                self.state['cash'] = 0
                raise KeyboardInterrupt
        self.assertEqual(self.state['cash'], 100)

    def test_nonfinite_state_does_not_commit(self):
        with self.assertRaises(ValueError):
            with store.StateTransaction(self.path, self.state):
                store.append_text(self.log, 'bad\n')
                self.state['cash'] = float('nan')
        self.assertEqual(self.state['cash'], 100)
        self.assertFalse(self.log.exists())
        self.assertEqual(json.loads(self.path.read_text())['cash'], 100)

    def test_recovery_finishes_durable_commit_once(self):
        self.pending()
        self.assertEqual(self.state['cash'], 100)
        self.assertTrue(store.recover(self.path))
        self.assertFalse(store.recover(self.path))
        self.assertEqual(self.log.read_text(), '{"event":1}\n')
        self.assertEqual(json.loads(self.path.read_text())['cash'], 97)

    def test_partial_log_append_is_completed_without_duplicate(self):
        record = self.pending()
        payload = base64.b64decode(record['logs'][0]['data'])
        self.log.write_bytes(payload[:5])
        store.recover(self.path)
        self.assertEqual(self.log.read_bytes(), payload)

    def test_complete_append_before_state_failure_is_not_duplicated(self):
        record = self.pending()
        payload = base64.b64decode(record['logs'][0]['data'])
        self.log.write_bytes(payload)
        store.recover(self.path)
        self.assertEqual(self.log.read_bytes(), payload)

    def test_conflicting_external_append_is_preserved_and_refused(self):
        self.pending()
        self.log.write_text('FOREIGN\n')
        with self.assertRaises(store.PendingCommit):
            store.recover(self.path)
        self.assertEqual(self.log.read_text(), 'FOREIGN\n')
        self.assertEqual(json.loads(self.path.read_text())['cash'], 100)
        self.assertTrue(store.journal_path(self.path).exists())

    def test_previous_log_boundary_is_checked(self):
        self.log.write_text('old\n')
        self.pending()
        self.log.write_text('BAD\n')
        with self.assertRaises(store.PendingCommit):
            store.recover(self.path)
        self.assertEqual(self.log.read_text(), 'BAD\n')

    def test_recovery_after_state_replacement_is_idempotent(self):
        self.pending()
        original_unlink = Path.unlink
        def failing_unlink(path, *args, **kwargs):
            if path == store.journal_path(self.path):
                raise OSError('interrupted before journal removal')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', failing_unlink):
            with self.assertRaises(OSError):
                store.recover(self.path)
        self.assertEqual(json.loads(self.path.read_text())['cash'], 97)
        store.recover(self.path)
        self.assertEqual(self.log.read_text(), '{"event":1}\n')

    def test_second_cycle_refuses_pending_commit(self):
        self.pending()
        with self.assertRaises(store.PendingCommit):
            with store.StateTransaction(self.path, self.state):
                pass

    def test_log_cannot_overwrite_state(self):
        with self.assertRaises(ValueError):
            with store.StateTransaction(self.path, self.state):
                store.append_text(self.path, 'bad')

    def test_csv_header_written_once_for_multiple_staged_trades(self):
        log = self.root / 'trades.csv'
        with store.StateTransaction(self.path, self.state):
            bot.log_trade(log, ['first'])
            bot.log_trade(log, ['second'])
        self.assertEqual(log.read_text().count('timestamp,action,'), 1)
        self.assertEqual(len(log.read_text().splitlines()), 3)

    def test_two_log_files_recover_after_first_append(self):
        other = self.root / 'other.jsonl'
        apply = store._apply_log
        calls = 0
        def interrupted(record):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('disk error')
            return apply(record)
        with patch.object(store, '_apply_log', interrupted), self.assertRaises(store.PendingCommit):
            with store.StateTransaction(self.path, self.state):
                self.state['cash'] = 97
                store.append_text(self.log, 'a\n')
                store.append_text(other, 'b\n')
        store.recover(self.path)
        self.assertEqual(self.log.read_text(), 'a\n')
        self.assertEqual(other.read_text(), 'b\n')

    def test_process_death_after_append_recovers_without_finally(self):
        # os._exit bypasses context-manager cleanup: this tests a real lost process,
        # not merely an exception handled by the transaction's rollback method.
        script = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import state_store as store
path, log = sys.argv[2:4]
state = json.load(open(path))
original = store._apply_log
def die_after_append(record):
    original(record)
    os._exit(27)
store._apply_log = die_after_append
with store.StateTransaction(path, state):
    state['cash'] = 97
    store.append_text(log, 'durable event\\n')
"""
        result = subprocess.run([sys.executable, '-c', script, str(Path(store.__file__).parent), str(self.path), str(self.log)],
                                capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 27, result.stderr)
        self.assertTrue(store.journal_path(self.path).exists())
        self.assertEqual(json.loads(self.path.read_text())['cash'], 100)
        store.recover(self.path)
        self.assertEqual(json.loads(self.path.read_text())['cash'], 97)
        self.assertEqual(self.log.read_text(), 'durable event\n')


class ProductionIntegrationTests(unittest.TestCase):
    def test_discovery_failure_does_not_undo_position_exit(self):
        from test_policy import position_state
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, log = root / 'state.json', root / 'trades.csv'
            store.atomic_json(path, position_state())
            argv = ['paperbot.py', '--once', '--state-file', str(path), '--log-file', str(log)]
            try:
                with patch('sys.argv', argv), patch.object(bot, 'get_current_prices', return_value={'TokenA': 0.7}), patch.object(bot, 'run_discovery', side_effect=RuntimeError('discovery API error')), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        bot.main()
            finally:
                bot.release_state_lock()
            saved = json.loads(path.read_text())
            self.assertFalse(saved['positions'])
            self.assertEqual(saved['cash'], 99.02)
            self.assertEqual(len(saved['closed_trades']), 1)
            self.assertEqual(len(log.read_text().splitlines()), 2)

    def test_interrupted_once_cycle_leaves_no_trade_or_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, log = Path(tmp) / 'state.json', Path(tmp) / 'trades.csv'
            def broken_discovery(state, args):
                state['cash'] = 97
                bot.log_trade(log, ['not committed'])
                raise RuntimeError('interrupted discovery')
            try:
                with patch('sys.argv', ['paperbot.py', '--once', '--state-file', str(path)]), patch.object(bot, 'run_discovery', broken_discovery), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        bot.main()
                self.assertFalse(log.exists())
                self.assertEqual(json.loads(path.read_text())['cash'], 100)
            finally:
                bot.release_state_lock()

    def test_repair_refuses_active_state_writer(self):
        with patch('sys.argv', ['repair_cash.py', '--state', 'not-read.json']), patch.object(bot, 'acquire_state_lock', return_value=False):
            with self.assertRaisesRegex(SystemExit, 'State is in use'):
                repair_cash.main()


if __name__ == '__main__':
    unittest.main()
