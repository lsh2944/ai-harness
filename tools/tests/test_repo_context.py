import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'repo_context.py'
spec = importlib.util.spec_from_file_location('repo_context', SCRIPT)
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


class RepoContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='repo-context-test-')
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.command('init', '-q', '-b', 'main')
        self.command('config', 'user.name', 'Fixture')
        self.command('config', 'user.email', 'fixture@example.invalid')

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args, check=True):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.repo), *args],
                              capture_output=True, text=True, check=check)

    def commit(self):
        self.command('add', '.')
        self.command('-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')

    def tracked(self):
        p = self.repo / 'main.py'
        p.write_text('version = 1\n')
        self.commit()
        return p

    def test_unborn_and_non_git(self):
        report = context.collect([self.repo, self.base])
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertEqual(report['repositories'][0]['head_state'], 'unborn')
        self.assertIsNone(report['repositories'][0]['head'])
        self.assertFalse(report['repositories'][1]['git'])

    def test_clean_detached_and_linked_worktree(self):
        self.tracked()
        normal = context.collect([self.repo])['repositories'][0]
        self.assertEqual(normal['branch'], 'main')
        self.assertEqual(normal['changes']['total_paths'], 0)
        worktree = self.base / 'second'
        self.command('worktree', 'add', '-q', '--detach', str(worktree), 'HEAD')
        row = context.collect([worktree])['repositories'][0]
        self.assertEqual(row['head_state'], 'detached')
        self.assertEqual(row['head'], normal['head'])
        self.assertEqual(row['worktree'], str(worktree))
        self.assertIsNone(row['branch'])

    def test_staged_unstaged_untracked_and_selected_omitted(self):
        p = self.tracked()
        p.write_text('version = 2\n')
        self.command('add', 'main.py')
        p.write_text('version = 3\n')
        # Windows はファイル名に改行を使えない（空白・改行の保持は POSIX で検証する）
        u = self.repo / ('new file name.py' if os.name == 'nt' else 'new file\nname.py')
        u.write_text('new = True\n')
        report = context.collect([self.repo], [u], limit=0)
        changes = report['repositories'][0]['changes']
        self.assertEqual((changes['staged'], changes['unstaged'], changes['untracked']), (1, 1, 1))
        self.assertEqual(changes['omitted_paths'], 2)
        full = Path(changes['full_changes_path'])
        self.assertEqual(len(json.loads(full.read_text())), 2)
        full.unlink()  # Only this test's temporary report.
        self.assertEqual(report['selected_files'][0]['changes'][0]['path'], u.name)

    def test_dirty_content_change_requires_hash(self):
        p = self.tracked()
        p.write_text('version = 2\n')
        first = context.collect([self.repo], [p], True)
        p.write_text('version = 3\n')
        second = context.collect([self.repo], [p], True)
        self.assertEqual(first['repositories'][0]['changes'], second['repositories'][0]['changes'])
        self.assertNotEqual(first['selected_files'][0]['sha256'], second['selected_files'][0]['sha256'])

    def test_rename_and_delete(self):
        p = self.tracked()
        self.command('mv', 'main.py', 'renamed file.py')
        result = context.collect([self.repo])['repositories'][0]['changes']['paths'][0]
        self.assertEqual(result['original_path'], p.name)
        self.assertEqual(result['path'], 'renamed file.py')
        self.commit()
        moved = self.repo / 'renamed file.py'
        moved.unlink()
        report = context.collect([self.repo], [moved], True)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(report['selected_files'][0]['changes'][0]['xy'], '.D')
        self.assertNotIn('sha256', report['selected_files'][0])

    def test_merge_conflict(self):
        p = self.tracked()
        self.command('checkout', '-qb', 'other')
        p.write_text('version = 2\n')
        self.commit()
        self.command('checkout', '-q', 'main')
        p.write_text('version = 3\n')
        self.commit()
        self.command('merge', 'other', check=False)
        row = context.collect([self.repo])['repositories'][0]
        self.assertEqual(row['changes']['conflicted'], 1)

    def test_no_implicit_hash_and_outside_selection(self):
        p = self.tracked()
        with patch.object(context, 'checked_hash', side_effect=AssertionError('unexpected read')):
            report = context.collect([self.repo], [p])
        self.assertEqual(report['status'], 'COMPLETE')
        outside = self.base / 'elsewhere.py'
        outside.write_text('example = True\n')
        self.assertEqual(context.collect([self.repo], [outside], True)['status'], 'INCOMPLETE')

    def test_protected_symlink_and_hardlink_hash_rejected(self):
        ordinary = self.tracked()
        secret = self.repo / '.aws' / 'dummy.py'
        secret.parent.mkdir()
        secret.write_text('not a real secret')
        named = self.repo / '.env.local'
        named.write_text('dummy')
        linked = self.repo / 'linked.py'
        targets = [secret, named]
        try:
            linked.symlink_to(ordinary)
            targets.append(linked)
        except OSError:
            pass  # シンボリックリンクを作れない環境（Windows の権限不足）ではリンクの検証を省く
        for p in targets:
            with self.subTest(p=p), patch.object(context.os, 'open', side_effect=AssertionError('must reject before opening')):
                with self.assertRaises(ValueError):
                    context.checked_hash(p)
        hardlink = self.repo / 'hardlink.py'
        os.link(ordinary, hardlink)
        with self.assertRaises(ValueError):
            context.checked_hash(hardlink)

    def test_timeout_and_failure_are_not_non_git(self):
        with patch.object(context, 'git', side_effect=subprocess.TimeoutExpired('git', 15)):
            report = context.collect([self.repo])
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertIsNone(report['repositories'][0]['git'])
        failure = subprocess.CompletedProcess([], 128, b'', b'permission denied')
        with patch.object(context, 'git', return_value=failure):
            self.assertEqual(context.collect([self.repo])['status'], 'INCOMPLETE')

    def test_cli_requires_selected_files_for_hash(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--hash'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--hash requires', result.stderr)

    def test_symlink_cycle_is_incomplete(self):
        a, b = self.repo / 'a.py', self.repo / 'b.py'
        try:
            a.symlink_to(b)
            b.symlink_to(a)
        except OSError:
            self.skipTest('シンボリックリンクを作成できない環境（Windows の権限不足）')
        report = context.collect([self.repo], [a], True)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertTrue(report['selected_files'][0]['errors'])

    def test_git_output_and_time_limits_during_capture(self):
        popen = subprocess.Popen
        for script in ("import sys; sys.stdout.write('x'*10000)",
                       "import sys; sys.stderr.write('x'*10000)",
                       'import time; time.sleep(60)'):
            with self.subTest(script=script):
                def fake_git(command, **kwargs):
                    return popen([sys.executable, '-c', script], **kwargs)
                with patch.object(context.subprocess, 'Popen', side_effect=fake_git), \
                        patch.object(context, 'MAX_GIT_BYTES', 1024), \
                        patch.object(context, 'GIT_TIMEOUT', 0.2):
                    with self.assertRaises(ValueError) as error:
                        context.git(self.repo, 'status')
                self.assertIn('captured output:', str(error.exception))


if __name__ == '__main__':
    unittest.main()
