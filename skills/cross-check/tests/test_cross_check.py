"""Test the real process boundary without model calls or secret inputs."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/cross_check.py'
spec = importlib.util.spec_from_file_location('cross_check', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CrossCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cross-check-test-')
        self.root = Path(self.temp.name).resolve()
        self.cli = self.root / 'fake-codex'

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, behavior=''):
        self.cli.write_text('''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
args = sys.argv[1:]
assert args[0] == 'exec'
assert args[args.index('--sandbox') + 1] == 'read-only'
assert '--ephemeral' in args and '--json' in args
assert not any(a in args for a in ['--model', '-m', '--ignore-user-config', '--ignore-rules'])
# -c は debate と共有する無効化キーと単一呼び出し専用の shell 無効化だけを、この順に渡す。
assert [args[i + 1] for i, a in enumerate(args) if a == '-c'] == ''' + repr(list(runner.SINGLE_CALL_DISABLE)) + '''
assert args[-1] == '-'
prompt = sys.stdin.read()
assert 'Do not call ANY tools' in prompt
assert '1: Question' in prompt
bundle = pathlib.Path(args[args.index('-C') + 1])
output = pathlib.Path(args[args.index('-o') + 1])
mode = next(line.removeprefix('Mode: ') for line in prompt.splitlines() if line.startswith('Mode: '))
schema = json.loads(pathlib.Path(args[args.index('--output-schema') + 1]).read_text())
assert mode in schema['properties']['mode']['enum']
result = {'mode': mode,
          'status': 'COMPLETE', 'assessment': 'Assessed supplied scope.',
          'findings': [{'severity': 'medium', 'claim': 'A validation is missing.',
                        'lines': [1], 'recommendation': 'Verify the requirement.'}],
          'unverified': ['Runtime behavior was not tested.']}
''' + behavior + '''
output.write_text(json.dumps(result))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message'}}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 3}}))
''')
        self.cli.chmod(0o700)
        return self.cli

    def run_fake(self, behavior='', mode='review', timeout=2):
        return runner.run('Question\nRelevant fact\n', mode, timeout, self.fake(behavior))

    def test_normal_modes_preserve_settings(self):
        for mode in ('review', 'test-plan', 'code-review'):
            with self.subTest(mode=mode):
                summary = self.run_fake(mode=mode)
                self.assertEqual(summary['status'], 'COMPLETE')
                self.assertEqual(summary['mode'], mode)
                self.assertEqual(summary['codex_calls'], 1)
                self.assertEqual(summary['usage']['input_tokens'], 5)

    def test_new_modes_reject_tools_and_invalid_references(self):
        for mode in ('test-plan', 'code-review'):
            for behavior in ("print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution'}}))",
                             "result['findings'][0]['lines'] = [999]"):
                with self.subTest(mode=mode, behavior=behavior):
                    self.assertEqual(self.run_fake(behavior, mode=mode)['status'], 'INCOMPLETE')

    def test_new_modes_cli_arguments(self):
        packet = self.root / 'packet.md'
        packet.write_text('Question\nEvidence\n')
        for mode in ('test-plan', 'code-review'):
            with self.subTest(mode=mode), patch.object(runner, 'DEFAULT_CODEX', self.fake()), \
                    patch('sys.argv', ['cross_check.py', '--packet', str(packet), '--mode', mode]), \
                    patch('builtins.print'):
                self.assertEqual(runner.main(), 0)

    def test_retired_design_mode_rejected(self):
        # 独立設計オプション廃止に伴い design モードは CLI・スキーマ・結果検証のいずれでも受け付けない。
        packet = self.root / 'packet.md'
        packet.write_text('Question\nEvidence\n')
        with patch.object(runner, 'DEFAULT_CODEX', self.fake()), \
                patch('sys.argv', ['cross_check.py', '--packet', str(packet), '--mode', 'design']), \
                patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
            runner.main()
        self.assertEqual(raised.exception.code, 2)
        schema = json.loads((SCRIPT.parent / 'result_schema.json').read_text())
        self.assertNotIn('design', schema['properties']['mode']['enum'])
        self.assertNotIn('mode design', (SCRIPT.parent / 'prompt.md').read_text())

    def test_invalid_results_are_incomplete(self):
        for change in ("result['findings'][0]['lines'] = [999]",
                       "result['findings'][0]['lines'] = [True]",
                       "result['mode'] = 'wrong'", "result['assessment'] = ''",
                       "result['extra'] = 'unrequested'", "result['findings'] = 'wrong'",
                       "result['status'] = []", "result['findings'][0]['severity'] = {}"):
            with self.subTest(change=change):
                self.assertEqual(self.run_fake(change)['status'], 'INCOMPLETE')

    def test_nonzero_missing_or_bad_json(self):
        for behavior in ('sys.exit(1)', "output.write_text('not JSON'); sys.exit(0)"):
            with self.subTest(behavior=behavior):
                self.assertEqual(self.run_fake(behavior)['status'], 'INCOMPLETE')

    def test_mutation_and_tools_are_rejected(self):
        behaviors = ["(bundle / 'packet.md').chmod(0o600); (bundle / 'packet.md').write_text('changed')",
                     "print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution'}}))",
                     "print(json.dumps({'type': 'error', 'message': 'hook denied'}))"]
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                self.assertEqual(self.run_fake(behavior)['status'], 'INCOMPLETE')

    def test_timeout_kills_descendants(self):
        pid_file = self.root / 'child.pid'
        behavior = f'''child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
time.sleep(60)'''
        # 負荷時に子プロセスの pid 書き込み前に打ち切られないよう余裕を持たせる。
        summary = self.run_fake(behavior, timeout=3)
        self.assertEqual(summary['status'], 'INCOMPLETE')
        self.assertTrue(any('TIMEOUT' in p for p in summary['problems']))
        pid = int(pid_file.read_text())
        # A killed orphan can briefly remain as a zombie on Linux.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        import subprocess
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not state or state.startswith('Z'), state)

    def test_stop_group_tolerates_unreaped_leader(self):
        import subprocess
        import time
        # 終了済み・未回収のリーダー（macOS で killpg が EPERM）でも例外にしない。
        process = subprocess.Popen(['/usr/bin/true'], start_new_session=True)
        time.sleep(0.3)
        runner.stop_group(process)
        self.assertEqual(process.returncode, 0)

    def test_stop_group_kills_live_leader_before_reraising_eperm(self):
        import subprocess
        # 生存中のリーダーで killpg が EPERM になった場合もリーダーを終了させてから例外を伝える。
        process = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
        with patch.object(runner.os, 'killpg', side_effect=PermissionError), self.assertRaises(PermissionError):
            runner.stop_group(process)
        self.assertIsNotNone(process.poll())

    def test_packet_inputs(self):
        packet = self.root / 'packet.md'
        packet.write_text('Question\nEvidence\n')
        self.assertEqual(runner.read_packet(packet), packet.read_text())
        linked = self.root / 'linked.md'
        linked.symlink_to(packet)
        with self.assertRaises(ValueError):
            runner.read_packet(linked)
        hardlink = self.root / 'hardlink.md'
        os.link(packet, hardlink)
        with self.assertRaises(ValueError):
            runner.read_packet(hardlink)
        hardlink.unlink()
        packet.write_text('x' * (runner.MAX_PACKET + 1))
        with self.assertRaises(ValueError):
            runner.read_packet(packet)
        protected = self.root / '.aws'
        protected.mkdir()
        source = protected / 'not-a-real-secret.md'
        source.write_text('dummy')
        with self.assertRaises(ValueError):
            runner.read_packet(source)

    def test_incomplete_model_response_is_preserved(self):
        summary = self.run_fake("result['status'] = 'INCOMPLETE'")
        self.assertEqual(summary['status'], 'INCOMPLETE')
        self.assertEqual(summary['problems'], [])

    def test_output_limits(self):
        summary = self.run_fake("output.write_text('x' * 140000); sys.exit(0)")
        self.assertEqual(summary['status'], 'INCOMPLETE')
        summary = self.run_fake("print('x' * 8500000); sys.exit(0)")
        self.assertEqual(summary['status'], 'INCOMPLETE')

    def test_malformed_event_types(self):
        for value in ('[]', 'None', "{'type': []}", "{'type': 'item.completed', 'item': None}"):
            with self.subTest(value=value):
                summary = self.run_fake('print(json.dumps(' + value + '))')
                self.assertEqual(summary['status'], 'INCOMPLETE')

    def test_disable_config_is_shared_with_debate(self):
        # D2: 無効化キーの定義は cross_check.py の 1 か所だけで、co_design.py はそれを使う。
        import importlib.util
        spec = importlib.util.spec_from_file_location('co_design_shared_check', SCRIPT.parent / 'co_design.py')
        co = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(co)
        self.assertIs(co.CC.DISABLE_CONFIG, co.DISABLE_CONFIG)
        self.assertEqual(co.DISABLE_CONFIG, runner.DISABLE_CONFIG)
        # debate は調査に shell を使うので、shell の無効化は単一呼び出し専用に限る。
        self.assertNotIn('features.shell_tool=false', co.DISABLE_CONFIG)
        self.assertEqual(runner.SINGLE_CALL_DISABLE, runner.DISABLE_CONFIG + ('features.shell_tool=false',))
        self.assertEqual(co.EVENT_TYPES, runner.EVENT_TYPES)
        source = (SCRIPT.parent / 'co_design.py').read_text(encoding='utf-8')
        self.assertNotIn("'features.apps=false'", source)

    def live_stop(self, behavior, expected):
        """道具 item・hook 拒否・許可外イベントの直後に 30 秒待つ偽 codex を、実行中に止められるか確かめる。"""
        pid_file = self.root / 'live-child.pid'
        hang = f'''child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
'''
        started = time.monotonic()
        summary = self.run_fake(hang + behavior + '\nsys.stdout.flush(); sys.stderr.flush(); time.sleep(30)', timeout=25)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10, summary)
        self.assertEqual(summary['status'], 'INCOMPLETE')
        self.assertTrue(summary['problems'][0].startswith('STOPPED during execution: ' + expected), summary['problems'])
        self.assertFalse(any('TIMEOUT' in p for p in summary['problems']))
        pid = int(pid_file.read_text())
        state = ''
        try:
            os.kill(pid, 0)
            import subprocess
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        except ProcessLookupError:
            pass
        self.assertTrue(not state or state.startswith('Z'), state)
        return summary

    def test_tool_item_stops_during_execution(self):
        for item in ('command_execution', 'mcp_tool_call', 'web_search', 'file_change'):
            with self.subTest(item=item):
                self.live_stop(f"print(json.dumps({{'type': 'item.started', 'item': {{'id': 't1', 'type': {item!r}}}}}))",
                               'unexpected tool/action event: ' + item)

    def test_stderr_hook_denial_stops_during_execution(self):
        denial = '2026-10-08T06:07:09Z ERROR codex_core::tools::router: error=Command blocked by PreToolUse hook: deny. Command: ls'
        self.live_stop(f'sys.stderr.write({denial[:50]!r}); sys.stderr.flush(); time.sleep(0.3); sys.stderr.write({denial[50:]!r} + "\\n")',
                       'hook denial reported by Codex (stderr)')

    def test_unknown_top_level_event_stops_during_execution(self):
        self.live_stop("print(json.dumps({'type': 'session.configured'}))", 'unexpected event type: session.configured')
        # 終了後の検査でも許可リスト外の上位イベントは INCOMPLETE にする。
        summary = self.run_fake("print(json.dumps({'type': 'session.configured'}))")
        self.assertEqual(summary['status'], 'INCOMPLETE')

    def test_hook_denial_at_exit_is_reported(self):
        summary = self.run_fake("sys.stderr.write('x Command blocked by PreToolUse hook: deny')")
        self.assertEqual(summary['status'], 'INCOMPLETE')
        self.assertIn('hook denial reported by Codex (stderr)', summary['problems'])
        self.assertEqual(self.run_fake("sys.stderr.write('WARN unrelated\\n')")['status'], 'COMPLETE')

    def test_summary_keys_unchanged(self):
        summary = self.run_fake()
        self.assertEqual(set(summary), {'status', 'mode', 'codex_calls', 'exit_code', 'elapsed_seconds', 'usage',
                                        'problems', 'output_dir', 'result_path'})

    # ---------------------------------------------------------------- L2 --include
    def source(self, lines=5):
        path = self.root / 'src' / 'module.py'
        path.parent.mkdir(exist_ok=True)
        path.write_text(''.join(f'value_{n} = {n}  # 行{n}\n' for n in range(1, lines + 1)), encoding='utf-8')
        return path

    def test_include_range_and_provenance(self):
        import hashlib
        path = self.source()
        packet = runner.compose_packet('Question\nEvidence\n', [f'{path}:2-3'])
        self.assertTrue(packet.startswith('Question\nEvidence\n\n## Source excerpts\n'))
        self.assertIn(f'### {path}:L2-L3', packet)
        self.assertIn('L2: value_2 = 2  # 行2\nL3: value_3 = 3  # 行3\n', packet)
        self.assertNotIn('value_1 ', packet)
        self.assertNotIn('value_4', packet)
        self.assertIn('- file sha256: ' + hashlib.sha256(path.read_bytes()).hexdigest(), packet)
        self.assertRegex(packet, r'- modified: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00')
        self.assertIn('- git: not a git work tree', packet)
        # 複数指定は指定順に並ぶ。
        packet = runner.compose_packet('Q\n', [f'{path}:5-5', f'{path}:1-1'])
        self.assertLess(packet.index(':L5-L5'), packet.index(':L1-L1'))

    def test_include_git_provenance(self):
        import shutil
        import subprocess
        if not shutil.which('git'):
            self.skipTest('git not available')
        path = self.source()
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
        for args in (['init', '-q', '-b', 'main'], ['add', 'src/module.py'],
                     ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'init']):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, env=env, capture_output=True)
        commit = subprocess.run(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                                check=True).stdout.strip()
        self.assertIn(f'- git: branch main, commit {commit}, file dirty: no', runner.compose_packet('Q\n', [f'{path}:1-2']))
        path.write_text(path.read_text() + 'extra = 1\n')
        self.assertIn('file dirty: yes', runner.compose_packet('Q\n', [f'{path}:1-2']))
        # n3: グロブ文字を含むファイル名を pathspec として展開しない（x1.py の変更を x[1].py に帰さない）。
        glob_named, plain = self.root / 'src/x[1].py', self.root / 'src/x1.py'
        glob_named.write_text('a\n')
        plain.write_text('b\n')
        for args in (['add', 'src'], ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'more']):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, env=env, capture_output=True)
        plain.write_text('changed\n')
        self.assertIn('file dirty: no', runner.compose_packet('Q\n', [f'{glob_named}:1-1']))
        self.assertIn('file dirty: yes', runner.compose_packet('Q\n', [f'{plain}:1-1']))

    def test_include_git_provenance_ignores_git_env_and_unborn(self):
        import shutil
        import subprocess
        from unittest import mock
        if not shutil.which('git'):
            self.skipTest('git not available')
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}

        def git(repo, *args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, env=env, capture_output=True, text=True).stdout.strip()
        own, other = self.root / 'own', self.root / 'other'
        for repo in (own, other):
            repo.mkdir()
            git(repo, 'init', '-q', '-b', 'main')
            (repo / 'f.py').write_text(f'name = "{repo.name}"\n')
            git(repo, 'add', 'f.py')
            git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', repo.name)
        own_commit, other_commit = git(own, 'rev-parse', 'HEAD'), git(other, 'rev-parse', 'HEAD')
        # GIT_DIR・GIT_WORK_TREE が別リポジトリを指していても、ファイルのあるリポジトリを答える。
        with mock.patch.dict(os.environ, {'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(other)}):
            packet = runner.compose_packet('Q\n', [f'{own / "f.py"}:1-1'])
        self.assertIn(f'commit {own_commit}', packet)
        self.assertNotIn(other_commit, packet)
        # 最初のコミット前はブランチ名と「コミットなし」を明示する。
        unborn = self.root / 'unborn'
        unborn.mkdir()
        git(unborn, 'init', '-q', '-b', 'trunk')
        (unborn / 'g.py').write_text('x = 1\n')
        self.assertIn('- git: branch trunk, commit none (no commits yet), file dirty: yes',
                      runner.compose_packet('Q\n', [f'{unborn / "g.py"}:1-1']))

    def test_include_counts_only_newlines(self):
        # m2: \f・\v・\x1c・\x85・U+2028/2029 は行区切りにしない。CRLF の \r は落とす。
        path = self.root / 'src' / 'odd.txt'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes('a\x0cb\x0bc\r\nd\x1ce\x85f\u2028g\u2029h\nlast\n'.encode('utf-8'))
        packet = runner.compose_packet('Q\n', [f'{path}:2-3'])
        self.assertIn('L2: d\x1ce\x85f\u2028g\u2029h\nL3: last\n', packet)
        self.assertIn('L1: a\x0cb\x0bc\n', runner.compose_packet('Q\n', [f'{path}:1-1']))
        with self.assertRaisesRegex(ValueError, r'outside the file \(3 lines\)'):
            runner.compose_packet('Q\n', [f'{path}:4-4'])
        self.assertEqual(runner.source_lines('x\r\ny'), ['x', 'y'])

    def test_include_rejects_protected_bad_and_oversized(self):
        path = self.source()
        protected = self.root / '.aws'
        protected.mkdir()
        (protected / 'notes.md').write_text('dummy\n')
        linked = self.root / 'linked.py'
        linked.symlink_to(path)
        env_file = self.root / '.env.local'
        env_file.write_text('A=1\n')
        for spec in (f'{protected / "notes.md"}:1-1', f'{linked}:1-1', f'{env_file}:1-1', f'{path}:4-9',
                     f'{path}:3-2', f'{path}:0-1', f'{path}', f'{self.root / "missing.py"}:1-1'):
            with self.subTest(spec=spec), self.assertRaises((ValueError, OSError)):
                runner.compose_packet('Q\n', [spec])
        big = self.root / 'big.txt'
        big.write_text(('x' * 1000 + '\n') * 80)
        with self.assertRaisesRegex(ValueError, 'exceeds 64 KiB'):
            runner.compose_packet('Q\n', [f'{big}:1-80'])
        # 上限内なら切り詰めずに全行を含める。
        self.assertEqual(runner.compose_packet('Q\n', [f'{big}:1-60']).count('x' * 1000), 60)

    def test_include_cli_end_to_end(self):
        path = self.source()
        packet = self.root / 'packet.md'
        packet.write_text('Question\nEvidence\n')
        behavior = "assert 'L2: value_2 = 2' in prompt and 'Source excerpts' in prompt"
        with patch.object(runner, 'DEFAULT_CODEX', self.fake(behavior)), \
                patch('sys.argv', ['cross_check.py', '--packet', str(packet), '--include', f'{path}:2-2']), \
                patch('builtins.print'):
            self.assertEqual(runner.main(), 0)
        printed = []
        with patch.object(runner, 'DEFAULT_CODEX', self.fake()), \
                patch('sys.argv', ['cross_check.py', '--packet', str(packet), '--include', f'{path}:2-99']), \
                patch('builtins.print', side_effect=printed.append):
            self.assertEqual(runner.main(), 2)
        self.assertIn('outside the file', printed[0])

    def test_nan_timeout_rejected_without_model_call(self):
        import subprocess
        result = subprocess.run(['python3', str(SCRIPT), '--packet', '/not-used', '--timeout', 'nan'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'INCOMPLETE')
        self.assertIn('timeout', result.stdout)


if __name__ == '__main__':
    unittest.main()
