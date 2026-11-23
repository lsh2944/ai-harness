"""Local fixtures only: no provider calls, credentials or production writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('task_metrics.py')
spec = importlib.util.spec_from_file_location('task_metrics', SCRIPT)
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


class TaskMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task-metrics-test-')
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / 'records.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **values):
        record = dict(version=1, recorded_at='2026-10-07T00:00:00+00:00', task='task-1', run='run-1',
                      provider='codex', workflow='code-review', kind='feature', verification='unknown',
                      source='unknown', scope='declared-run', execution_status=None, **dict.fromkeys(metrics.NUMBERS))
        record.update(values)
        return record

    def load(self):
        with metrics.safe_file(self.store) as fd:
            return metrics.read_records(fd)

    def command(self, run='run-1'):
        return [sys.executable, str(SCRIPT), 'record', '--store', str(self.store), '--task', 'task-1',
                '--run', run, '--provider', 'codex', '--workflow', 'code-review', '--kind', 'feature']

    def summary_fixture(self, **values):
        summary = dict(status='COMPLETE', mode='code-review', codex_calls=1, exit_code=0,
                       elapsed_seconds=1.25, usage={'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20},
                       problems=[], output_dir='/not-read', result_path='/not-read/result.json')
        summary.update(values)
        path = self.root / 'summary.json'
        path.write_text(json.dumps(summary))
        return path

    def test_observed_unknown_cache_and_task_filter(self):
        for record in [self.record(input_tokens=100, cached_input_tokens=40, output_tokens=20, rework_count=0),
                       self.record(run='run-2'), self.record(run='run-3', task='other', input_tokens=500)]:
            metrics.append_record(self.store, record)
        result = metrics.summarize(self.load(), 'task-1')
        self.assertEqual(result['records'], 2)
        values = result['groups'][0]['metrics']
        self.assertEqual(values['input_tokens'], {'sum': 100, 'observed': 1, 'unknown': 1})
        self.assertEqual(values['cached_input_tokens']['sum'], 40)
        self.assertEqual(values['elapsed_seconds'], {'sum': None, 'observed': 0, 'unknown': 2})
        self.assertEqual(values['rework_count'], {'sum': 0, 'observed': 1, 'unknown': 1})

    def test_scope_provider_workflow_kind_separate_groups(self):
        records = [self.record(input_tokens=10), self.record(run='r2', scope='cross-check-call', source='provider-report', execution_status='COMPLETE', input_tokens=20),
                   self.record(run='r3', provider='claude'), self.record(run='r4', workflow='test-plan'),
                   self.record(run='r5', kind='bugfix')]
        result = metrics.summarize(records)
        self.assertEqual(len(result['groups']), 5)
        scopes = {g['scope']: g['metrics']['input_tokens']['sum'] for g in result['groups'] if g['metrics']['input_tokens']['observed']}
        self.assertEqual(scopes, {'declared-run': 10, 'cross-check-call': 20})

    def test_duplicate_and_conflicting_run_rejected_unchanged(self):
        metrics.append_record(self.store, self.record())
        original = self.store.read_bytes()
        for record in (self.record(), self.record(task='another', input_tokens=50)):
            with self.assertRaises(ValueError):
                metrics.append_record(self.store, record)
            self.assertEqual(self.store.read_bytes(), original)
        metrics.append_record(self.store, self.record(provider='claude'))
        self.assertEqual(len(self.load()), 2)

    def test_invalid_numbers_ids_and_categories(self):
        changes = [{'input_tokens': x} for x in (-1, True, 1.1, '5', float('nan'), 2**53)]
        changes += [{'elapsed_seconds': x} for x in (-1, True, float('nan'), float('inf'))]
        changes += [dict(input_tokens=5, cached_input_tokens=6), dict(task='private details here'),
                    dict(run='a' * 65), dict(provider='other'), dict(version=True),
                    dict(execution_status=True), dict(execution_status='COMPLETE'),
                    dict(scope='cross-check-call', source='provider-report')]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                metrics.append_record(self.store, self.record(**change))
        self.assertFalse(self.store.exists())

    def test_summary_import_does_not_infer_verification(self):
        record = self.record()
        metrics.import_summary(self.summary_fixture(), record)
        metrics.append_record(self.store, record)
        self.assertEqual(record['input_tokens'], 100)
        self.assertEqual(record['cached_input_tokens'], 40)
        self.assertEqual(record['elapsed_seconds'], 1.25)
        self.assertEqual(record['scope'], 'cross-check-call')
        self.assertEqual(record['source'], 'provider-report')
        self.assertEqual(record['verification'], 'unknown')
        self.assertEqual(self.load()[0]['execution_status'], 'COMPLETE')
        self.assertNotIn('result_path', self.load()[0])

    def test_incomplete_missing_usage_is_unknown(self):
        record = self.record()
        metrics.import_summary(self.summary_fixture(status='INCOMPLETE', usage=None), record)
        metrics.validate(record)
        self.assertIsNone(record['input_tokens'])
        self.assertEqual(record['verification'], 'unknown')

    def test_execution_status_survives_storage_and_aggregation(self):
        cases = [('COMPLETE', {'input_tokens': 100}), ('INCOMPLETE', {'input_tokens': 7}), ('INCOMPLETE', None)]
        for i, (status, usage) in enumerate(cases):
            record = self.record(run='r' + str(i))
            metrics.import_summary(self.summary_fixture(status=status, usage=usage), record)
            metrics.append_record(self.store, record)
        metrics.append_record(self.store, self.record(run='main'))
        groups = {g['scope']: g for g in metrics.summarize(self.load())['groups']}
        calls = groups['cross-check-call']
        self.assertEqual(calls['execution_status'], {'COMPLETE': 1, 'INCOMPLETE': 2, 'unknown': 0})
        self.assertEqual(calls['verification']['unknown'], 3)
        self.assertEqual(calls['metrics']['input_tokens'], {'observed': 2, 'unknown': 1, 'sum': 107})
        self.assertEqual(groups['declared-run']['execution_status']['unknown'], 1)

    def test_import_mismatch_override_and_malformed_rejected(self):
        path = self.summary_fixture()
        for changes in (dict(provider='claude'), dict(workflow='test-plan'), dict(input_tokens=1), dict(source='user-report')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                metrics.import_summary(path, self.record(**changes))
        for changes in (dict(codex_calls=True), dict(usage=[]), dict(mode=[]), dict(status='bad')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                metrics.import_summary(self.summary_fixture(**changes), self.record())
        record = self.record()
        metrics.import_summary(self.summary_fixture(usage={'input_tokens': True}), record)
        with self.assertRaises(ValueError):
            metrics.append_record(self.store, record)

    def test_debate_call_summaries_import_and_session_summary_rejected(self):
        for phase in metrics.DEBATE_PHASES:
            with self.subTest(phase=phase):
                record = self.record(run='debate-' + phase, workflow='debate')
                metrics.import_summary(self.summary_fixture(mode='debate-' + phase, execution='ok', tool_counts={}), record)
                metrics.append_record(self.store, record)
        groups = metrics.summarize(self.load())['groups']
        self.assertEqual([(g['workflow'], g['records']) for g in groups], [('debate', len(metrics.DEBATE_PHASES))])
        # セッション集計・未知 phase・旧 design は呼び出し別記録として取り込まない。
        for changes in (dict(mode='debate-session', codex_calls=3), dict(mode='debate-session'),
                        dict(mode='debate-unknown'), dict(mode='debate')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                metrics.import_summary(self.summary_fixture(**changes), self.record(workflow='debate'))
        with self.assertRaises(ValueError):
            metrics.import_summary(self.summary_fixture(mode='design'), self.record(workflow='independent'))

    def test_legacy_independent_readable_but_not_recordable_by_cli(self):
        legacy = self.record(run='legacy', scope='cross-check-call', source='provider-report',
                             execution_status='COMPLETE', workflow='independent')
        self.store.write_text(json.dumps(legacy) + '\n')
        self.assertEqual(self.load()[0]['workflow'], 'independent')
        result = subprocess.run([sys.executable, str(SCRIPT), 'summary', '--store', str(self.store)], capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['groups'][0]['workflow'], 'independent')
        command = self.command('new-independent')
        command[command.index('code-review')] = 'independent'
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice', result.stderr)
        command[command.index('independent')] = 'debate'
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.load()), 2)

    def test_protected_paths_rejected_before_open(self):
        for path in ('a/.aws/data', '.codex/data', '.claude/settings.json', '.claude/hooks/data',
                     'tools/harness/data', 'a/credentials', 'a/.env.local', 'a/key.pem', 'a/../records.jsonl'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                with metrics.safe_file(self.root / path):
                    self.fail('protected input opened')

    def test_symlink_file_and_parent_rejected(self):
        metrics.append_record(self.store, self.record())
        link = self.root / 'linked.jsonl'
        try:
            link.symlink_to(self.store)
        except OSError:
            self.skipTest('シンボリックリンクを作成できない環境（Windows の権限不足）')
        directory_link = self.root / 'alias'
        directory_link.symlink_to(self.root, target_is_directory=True)
        for path in (link, directory_link / 'records.jsonl'):
            for write in (True, False):
                with self.subTest(path=path, write=write), self.assertRaises((ValueError, OSError)):
                    with metrics.safe_file(path, write=write):
                        self.fail('symlink opened')
        self.assertEqual(len(self.load()), 1)

    def test_hardlink_fifo_directory_rejected(self):
        metrics.append_record(self.store, self.record())
        linked = self.root / 'hardlink.jsonl'
        os.link(self.store, linked)
        paths = [linked]
        if hasattr(os, 'mkfifo'):  # Windows には FIFO がない
            fifo = self.root / 'fifo.jsonl'
            os.mkfifo(fifo)
            paths.append(fifo)
        directory = self.root / 'directory.jsonl'
        directory.mkdir()
        for path in paths + [directory]:
            for write in (False, True):
                with self.subTest(path=path, write=write), self.assertRaises((OSError, ValueError)):
                    with metrics.safe_file(path, write=write):
                        self.fail('special file opened')

    def test_corrupt_store_is_not_ignored_or_changed(self):
        valid = json.dumps(self.record()).encode() + b'\n'
        for data in (b'{bad}\n', valid.rstrip(), valid + b'\n', valid + valid, b'\xff\n', b'{"n":NaN}\n', b'{"n":1,"n":2}\n'):
            self.store.write_bytes(data)
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                metrics.append_record(self.store, self.record(run='other'))
            self.assertEqual(self.store.read_bytes(), data)

    def test_size_limits(self):
        self.store.write_bytes(b'x' * (metrics.MAX_STORE + 1))
        with self.assertRaises(ValueError):
            self.load()
        path = self.root / 'large.json'
        path.write_bytes(b'x' * (metrics.MAX_SUMMARY + 1))
        with self.assertRaises(ValueError):
            metrics.import_summary(path, self.record())
        self.store.write_bytes(b'')
        with patch.object(metrics, 'MAX_STORE', 10), self.assertRaises(ValueError):
            metrics.append_record(self.store, self.record())
        self.assertEqual(self.store.read_bytes(), b'')

    def test_simultaneous_appends_and_duplicate_race(self):
        children = [subprocess.Popen(self.command(run='parallel-' + str(i)), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(8)]
        try:
            results = [child.communicate(timeout=10) for child in children]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
        self.assertEqual([child.returncode for child in children], [0] * 8, results)
        self.assertEqual(len(self.load()), 8)
        children = [subprocess.Popen(self.command(run='same'), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for child in children:
            child.communicate(timeout=10)
        self.assertEqual(sorted(p.returncode for p in children), [0, 2])
        self.assertEqual(len(self.load()), 9)

    def test_cli_help_record_summary_and_errors(self):
        for command in ([], ['record'], ['summary']):
            result = subprocess.run([sys.executable, str(SCRIPT), *command, '--help'], capture_output=True, text=True, encoding='utf-8', timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(self.command(), capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        if os.name != 'nt':  # Windows の権限ビットは読み取り専用属性しか表さない
            self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        result = subprocess.run([sys.executable, str(SCRIPT), 'summary', '--store', str(self.store), '--task', 'task-1'], capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['groups'][0]['metrics']['input_tokens']['sum'], None)
        result = subprocess.run(self.command('invalid') + ['--elapsed-seconds', 'nan'], capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(self.load()), 1)
        self.store.write_bytes(b'private corrupt content\n')
        result = subprocess.run([sys.executable, str(SCRIPT), 'summary', '--store', str(self.store)], capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('private corrupt content', result.stdout + result.stderr)

    def test_missing_import_and_default_parent_creation(self):
        with self.assertRaises(FileNotFoundError):
            metrics.import_summary(self.root / 'absent.json', self.record())
        store = self.root / 'new' / 'records.jsonl'
        metrics.append_record(store, self.record())
        self.assertTrue(store.is_file())


if __name__ == '__main__':
    unittest.main()
