"""co_design の単体テスト。偽 codex と一時ディレクトリのみを使い、実 Codex・AWS・秘密情報は使わない。"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import co_design_fixture as fx  # noqa: E402

co = fx.co
SKILL = Path(__file__).resolve().parents[1]
FORBIDDEN_OPTIONS = {'-m', '--model', '-p', '--profile', '--ignore-user-config', '--ignore-rules', '--approve-for-me',
                     '--add-dir', '--enable', '--disable', 'resume', 'fork', '--worktree', '--oss', '--local-provider',
                     '-i', '--image', '--cyber-access-program'}


def find_task_metrics():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / 'tools/task_metrics/task_metrics.py'
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location('task_metrics_for_co_design', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='co-design-test-')
        self.env = fx.Env(self.temp.name)

    def tearDown(self):
        self.env.close()
        self.temp.cleanup()

    def stopped(self, sid, category):
        session = self.env.session(sid)
        self.assertEqual(session['state'], 'STOPPED')
        self.assertEqual(session['stop']['category'], category, session['stop'])
        return session


class CommandTests(Base):
    def test_command_arguments_are_fixed(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        args = self.env.argv()[0]
        self.assertEqual(args[0], 'exec')
        self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
        for flag in ('--ephemeral', '--json', '--skip-git-repo-check'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('-C') + 1], str(self.env.project))
        self.assertEqual(args[-1], '-')
        self.assertFalse(FORBIDDEN_OPTIONS & set(args))
        self.assertFalse([a for a in args if a.startswith('--dangerously')])
        configs = [args[i + 1] for i, a in enumerate(args) if a == '-c']
        self.assertEqual(configs, list(co.DISABLE_CONFIG))
        for value in configs:
            self.assertRegex(value, r'^features\.[a-z_0-9]+=false$')
        self.assertTrue(args[args.index('--output-schema') + 1].endswith('proposal.schema.json'))

    def test_build_command_for_every_phase(self):
        for phase, schema in co.PHASE_SCHEMA.items():
            with self.subTest(phase=phase):
                command = co.build_command('/x/codex', co.SCHEMAS / (schema + '.schema.json'), '/x/result.json')
                self.assertTrue(Path(command[command.index('--output-schema') + 1]).is_file())
                self.assertFalse(FORBIDDEN_OPTIONS & set(command))


class TransitionTests(Base):
    def test_out_of_order_transitions_are_refused(self):
        sid = self.env.init()
        for argv in (['call', '--session', sid, '--phase', 'proposal'], ['reveal', '--session', sid],
                     ['finalize', '--session', sid]):
            with self.subTest(argv=argv[0]):
                self.assertEqual(self.env.run(*argv)[0]['status'], 'REFUSED')
        brief, scope, index = self.env.inputs()
        self.env.ok('freeze-inputs', '--session', sid, '--brief', brief, '--scope', scope, '--index', index)
        source = self.env.project / 'src/app.py'
        # Q3: Codex 提案が終わる前は Claude 側の根拠登録・凍結・公開・討論を拒否する。
        for argv in (['evidence', 'add', '--session', sid, '--file', source, '--lines', '1-2'],
                     ['freeze', '--session', sid, '--side', 'claude', '--file', brief],
                     ['reveal', '--session', sid], ['call', '--session', sid, '--phase', 'debate'],
                     ['call', '--session', sid, '--phase', 'critique']):
            with self.subTest(argv=argv[:2]):
                self.assertEqual(self.env.run(*argv)[0]['status'], 'REFUSED')
        self.assertEqual(self.env.calls(), [])

    def test_reveal_requires_both_sides_frozen(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        output, code = self.env.run('reveal', '--session', sid)
        self.assertEqual((output['status'], code), ('REFUSED', 2))
        self.assertFalse((self.env.artifacts / sid / 'artifacts/codex/proposal.json').exists())
        proposal = self.env.file('p.json', self.env.claude_proposal(sid))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        revealed = self.env.ok('reveal', '--session', sid)
        self.assertTrue(Path(revealed['codex_proposal']).is_file())
        self.assertEqual(oct(Path(revealed['codex_proposal']).stat().st_mode & 0o777), oct(0o400))

    def test_claude_file_created_before_codex_end_is_refused(self):
        sid = self.env.to_inputs_frozen()
        early = self.env.file('early.json', {'placeholder': True})
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        early.write_text(json.dumps(self.env.claude_proposal(sid)))
        output, _ = self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', early)
        self.assertEqual(output['status'], 'REFUSED')
        self.assertIn('before the Codex call ended', output['error'])
        late = self.env.file('late.json', self.env.claude_proposal(sid))
        self.assertEqual(self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', late)['state'], 'CLAUDE_FROZEN')

    def test_claude_proposal_schema_and_registration(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        good = self.env.claude_proposal(sid)
        for change in (lambda p: p.pop('risks'), lambda p: p.update(side='codex'),
                       lambda p: p['evidence'].append({'id': 'E7', 'kind': 'file', 'ref': 'src/app.py:L1-L1', 'observation': 'x'}),
                       lambda p: p['evidence'].append({'id': 'E8', 'kind': 'brief', 'ref': 'brief:L99', 'observation': 'x'})):
            broken = copy.deepcopy(good)
            change(broken)
            with self.subTest(change=change):
                path = self.env.file('bad.json', broken)
                self.assertEqual(self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', path)[0]['status'], 'REFUSED')

    def test_proposal_accepts_multiple_brief_refs(self):
        # 「;」「,」区切りの複数参照は要素ごとに検証する（2026-10-16 Codex 出力が SCHEMA 停止した形）
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        base = self.env.claude_proposal(sid)
        # 拒否される形を先に確認する（凍結に成功すると状態が進むため、受理は最後に 1 回だけ）
        for ref in ('brief:L1; brief:L99', ';', 'brief:L1; src/app.py:L1'):
            proposal = copy.deepcopy(base)
            proposal['evidence'].append({'id': 'E77', 'kind': 'brief', 'ref': ref, 'observation': 'x'})
            with self.subTest(ref=ref):
                path = self.env.file('multi.json', proposal)
                self.assertEqual(self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', path)[0]['status'], 'REFUSED')
        proposal = copy.deepcopy(base)
        proposal['evidence'].append({'id': 'E77', 'kind': 'brief', 'ref': 'brief:L1-L2; brief:L2, brief:L1', 'observation': 'x'})
        path = self.env.file('multi.json', proposal)
        self.assertEqual(self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', path)['state'], 'CLAUDE_FROZEN')

    def test_frozen_input_change_stops_session(self):
        sid = self.env.to_revealed()
        brief = self.env.artifacts / sid / 'artifacts/inputs/brief.md'
        os.chmod(brief.parent, 0o700)
        os.chmod(brief, 0o600)
        brief.write_text('changed\n')
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        output, code = self.env.run('call', '--session', sid, '--phase', 'debate')
        self.assertEqual((output['status'], code), ('STOPPED', 2))
        self.stopped(sid, 'INPUT_CHANGED')
        self.assertEqual(self.env.calls(), ['proposal'])

    def test_session_lock_refuses_concurrent_command(self):
        sid = self.env.to_inputs_frozen()
        with co.session_lock(sid):
            self.assertEqual(self.env.run('call', '--session', sid, '--phase', 'proposal')[0]['status'], 'REFUSED')
        self.assertEqual(self.env.calls(), [])


class BudgetTests(Base):
    def test_codex_budgets_and_call_caps(self):
        a = self.env.ok('init', '--question-id', 'qa')
        b = self.env.ok('init', '--question-id', 'qb', '--frozen-critique')
        self.assertEqual((a['plan'], a['codex_budget_seconds'], a['max_calls']), ('A', 1800, 3))
        self.assertEqual((b['plan'], b['codex_budget_seconds'], b['max_calls']), ('B', 2400, 4))
        self.assertEqual(a['phase_timeouts'], {'proposal': 900, 'other': 600})

    def test_fourth_call_in_a_and_fifth_in_b_are_refused(self):
        for plan_b, cap in ((False, 3), (True, 4)):
            with self.subTest(plan_b=plan_b):
                sid = self.env.to_inputs_frozen(plan_b)
                dummy = [{'index': i + 1, 'phase': 'proposal', 'started_at': 0, 'ended_at': 1, 'execution': 'failed',
                          'status': 'INCOMPLETE', 'timeout': 1} for i in range(cap)]
                self.env.edit_session(sid, calls=dummy)
                before = len(self.env.calls())
                output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
                self.assertEqual(output['status'], 'STOPPED')
                self.assertIn('call limit', output['stop']['reason'])
                self.assertEqual(len(self.env.calls()), before)

    def test_timeout_comes_from_codex_budget_and_low_remaining_stops(self):
        sid = self.env.to_inputs_frozen()
        self.env.edit_session(sid, codex_budget_seconds=100)
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        timeout = self.env.session(sid)['calls'][0]['timeout']
        self.assertEqual(timeout, 100)
        sid = self.env.to_inputs_frozen()
        self.env.edit_session(sid, codex_budget_seconds=59)
        output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(output['stop']['category'], 'BUDGET')
        self.assertIn('BUDGET_EXHAUSTED', output['stop']['reason'])
        self.assertEqual(self.env.calls(), ['proposal'])

    def test_phase_timeouts_and_claude_time_not_counted(self):
        sid = self.env.to_inputs_frozen()
        # Claude の作業時間（壁時計の経過）は Codex 予算を消費しない。
        self.env.edit_session(sid, created_at=time.time() - 10 * 3600)
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        first = self.env.session(sid)['calls'][0]
        self.assertEqual(first['timeout'], 900)
        self.assertIsNotNone(first['codex_seconds'])
        session = self.env.session(sid)
        self.assertAlmostEqual(co.codex_remaining(session), 1800 - first['codex_seconds'], places=3)
        # 以降の呼び出しは予算に余裕があっても 600 秒が上限。
        session['calls'][0]['codex_seconds'] = 1000
        self.assertEqual(min(co.phase_timeout('debate'), co.codex_remaining(session)), 600)
        session['calls'][0]['codex_seconds'] = 1500
        self.assertEqual(min(co.phase_timeout('debate'), co.codex_remaining(session)), 300)

    def test_timeout_kills_process_group(self):
        sid = self.env.to_inputs_frozen()
        pid_file = self.env.root / 'child.pid'
        self.env.behaviors({'proposal': fx.hang_with_child(pid_file) + 'time.sleep(60)\n'})
        with patch.object(co, 'PROPOSAL_TIMEOUT', 0.6):
            started = time.monotonic()
            output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn('TIMEOUT', output['stop']['reason'])
        self.assertTrue(fx.process_gone(int(pid_file.read_text())))

    def test_failed_call_counts_and_is_not_retried(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': 'sys.exit(1)'})
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['execution'], code), ('failed', 2))
        session = self.stopped(sid, 'EXEC')
        self.assertEqual(len(session['calls']), 1)
        self.assertEqual(self.env.calls(), ['proposal'])
        self.assertEqual(self.env.run('call', '--session', sid, '--phase', 'proposal')[0]['status'], 'REFUSED')
        self.assertEqual(self.env.calls(), ['proposal'])

    def test_oversized_request_is_incomplete_without_call(self):
        sid = self.env.to_inputs_frozen()
        with patch.object(co, 'MAX_REQUEST', 2000):
            output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['status'], code), ('INCOMPLETE', 2))
        self.assertIn('192 KiB', output['problems'][0])
        self.assertEqual(self.env.session(sid)['calls'], [])
        self.assertEqual(self.env.calls(), [])


# 経路形マーカーは実行時に組み立てる（本スキルのファイルを Codex が読んでも出力検査で誤停止しないため）。
CP = '.' + 'claude/' + 'projects'
CTMP = '/private/' + 'tmp/claude-' + '502'
OTHER_SID = '2026' + '0101T000000Z-abcdef'


class PolicyTests(Base):
    CASES = {
        'file_change': ("emit({'type': 'item.started', 'item': {'id': 'f1', 'type': 'file_change', 'status': 'in_progress'}})", 'POLICY'),
        'unknown type': ("emit({'type': 'item.started', 'item': {'id': 'u1', 'type': 'browser_action', 'status': 'in_progress'}})", 'POLICY'),
        # E2E 実測: wait_agent は collab_tool_call として現れる。
        'collab_tool_call': ("emit({'type': 'item.started', 'item': {'id': 'w1', 'type': 'collab_tool_call', 'tool': 'wait_agent', 'status': 'in_progress'}})", 'POLICY'),
        # E2E 実測: cua_repl は無効化できず mcp_tool_call server=cua_repl として現れる。
        'cua_repl': ("mcp('cu1', 'cua_repl', 'js', {'code': '1'})", 'POLICY'),
        'unknown event': ("emit({'type': 'session.configured'})", 'POLICY'),
        'unknown status': ("emit({'type': 'item.completed', 'item': {'id': 'd1', 'type': 'command_execution', 'command': 'ls', 'status': 'declined'}})", 'POLICY'),
        'forbidden server': ("mcp('a1', 'codex_apps', 'sites_deploy_site_version', {})", 'POLICY'),
        'spawn agent tool': ("mcp('a2', 'collaboration', 'spawn_agent', {})", 'POLICY'),
        'scope tool': ("mcp('a3', 'aws-api', 'call_aws', {'cli_command': 'aws sts get-caller-identity'})", 'POLICY'),
        'claude projects': (f"tool('i1', 'ls ~/{CP}/-Users-x/', '')", 'INDEPENDENCE'),
        'claude scratch': (f"tool('i2', 'cat {CTMP}/x/scratchpad/p.md', '')", 'INDEPENDENCE'),
        'other session': (f"tool('i3', 'cat .local/co-design/{OTHER_SID}/artifacts/claude/proposal.json', '')", 'INDEPENDENCE'),
        'session logs': ("tool('i4', 'cat ' + os.environ.get('FAKE_TMP', '') + '/co-design-x/logs', '')", 'INDEPENDENCE'),
        'output path': (f"tool('i5', 'grep -rn needle .', './notes/x.md: see /Users/u/{CP}/-Users-u/abc.jsonl')", 'INDEPENDENCE'),
        'secret': ("tool('s1', 'cat ~/' + '.aws/' + 'credentials', '')", 'SECRET'),
        'hook denial': ("tool('h1', 'rm -rf /x', 'Command blocked by PreToolUse hook: denied', status='failed', exit_code=1)", 'POLICY'),
        'error event': ("emit({'type': 'error', 'message': 'hook denied'})", 'POLICY'),
        'turn failed': ("emit({'type': 'turn.failed', 'error': {'message': 'x'}})", 'POLICY'),
        'tool count': ("[tool('n%d' % i, 'echo %d' % i, 'x') for i in range(61)]", 'BUDGET'),
        'tool output': ("[tool('o%d' % i, 'cat big', 'x' * 1100000) for i in range(4)]", 'BUDGET'),
    }

    def test_each_violation_stops_and_kills_children(self):
        for name, (code, category) in self.CASES.items():
            with self.subTest(case=name):
                sid = self.env.to_inputs_frozen()
                pid_file = self.env.root / f'child-{len(self.env.calls())}.pid'
                self.env.behaviors({'proposal': fx.hang_with_child(pid_file) + code + '\ntime.sleep(30)\n'})
                with patch.dict(os.environ, {'FAKE_TMP': str(self.env.tmp)}):
                    started = time.monotonic()
                    output, exit_code = self.env.run('call', '--session', sid, '--phase', 'proposal')
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual((output['state'], exit_code), ('STOPPED', 2), output)
                self.assertEqual(output['violations'][0]['category'], category, output['violations'])
                self.stopped(sid, category)
                self.assertTrue(fx.process_gone(int(pid_file.read_text())))
                # 停止後の呼び出しはすべて拒否する。
                self.assertEqual(self.env.run('call', '--session', sid, '--phase', 'proposal')[0]['status'], 'REFUSED')

    DENIAL = "2026-10-08T06:07:09.653913Z ERROR codex_core::tools::router: error=Command blocked by PreToolUse hook: deny. Command: rm x"

    def test_stderr_hook_denial_stops_promptly(self):
        # E2E 実測: hook 拒否は events に出ず stderr の 1 行だけに出る。実行中でもすぐ停止する。
        for name, code in (('whole line', f"sys.stderr.write({self.DENIAL!r} + '\\n'); sys.stderr.flush()"),
                           ('split line', f"sys.stderr.write({self.DENIAL[:60]!r}); sys.stderr.flush(); time.sleep(0.4); "
                                          f"sys.stderr.write({self.DENIAL[60:]!r} + '\\n'); sys.stderr.flush()"),
                           ('split marker', f"sys.stderr.write({self.DENIAL[:70]!r}); sys.stderr.flush(); time.sleep(0.4); "
                                            f"sys.stderr.write({self.DENIAL[70:]!r}); sys.stderr.flush()")):
            with self.subTest(case=name):
                sid = self.env.to_inputs_frozen()
                pid_file = self.env.root / f'stderr-{len(self.env.calls())}.pid'
                self.env.behaviors({'proposal': fx.hang_with_child(pid_file) + code + '\ntime.sleep(30)\n'})
                started = time.monotonic()
                output, exit_code = self.env.run('call', '--session', sid, '--phase', 'proposal')
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual((output['state'], exit_code), ('STOPPED', 2))
                self.assertEqual(output['violations'][0], {'category': 'POLICY', 'detail': 'hook denial reported by Codex (stderr)'})
                self.assertTrue(fx.process_gone(int(pid_file.read_text())))

    def test_stderr_hook_denial_without_newline_at_exit(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': f"sys.stderr.write({self.DENIAL!r}); sys.stderr.flush()"})
        output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(output['state'], 'STOPPED')
        self.assertIn('hook denial', output['violations'][0]['detail'])

    def test_unrelated_stderr_does_not_stop(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "sys.stderr.write('WARN some notice\\n' * 5); sys.stderr.flush()"})
        self.assertEqual(self.env.ok('call', '--session', sid, '--phase', 'proposal')['violations'], [])

    def test_violation_after_large_burst_before_exit_is_seen(self):
        # 終了直前に 1 MiB を超えるイベントを書いても EOF まで読んで違反を検出する（m1）。
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "[emit({'type': 'item.completed', 'item': {'id': 'r%d' % i, 'type': 'reasoning', 'text': 'y' * 1000}}) for i in range(2500)]\n"
                                        "emit({'type': 'item.started', 'item': {'id': 'f9', 'type': 'file_change', 'status': 'in_progress'}})"})
        output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(output['state'], 'STOPPED')
        self.assertIn('file_change', output['violations'][0]['detail'])

    def test_independence_discards_result(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': f"tool('i1', 'cat ~/{CP}/p/s.jsonl', '')"})
        output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(output['state'], 'STOPPED')
        call_dir = self.env.tmp / f'co-design-{sid}/logs/calls/1-proposal'
        keep_dir = self.env.artifacts / sid / 'work/calls/1-proposal'
        self.assertFalse((call_dir / 'result.json').exists())
        self.assertFalse((keep_dir / 'result.json').exists())
        # D3: summary は恒久領域に残る。
        self.assertTrue(json.loads((keep_dir / 'summary.json').read_text())['discarded'])

    def test_allowed_reads_do_not_stop(self):
        sid = self.env.to_inputs_frozen()
        inputs = f'.local/co-design/{sid}/artifacts/inputs/brief.md'
        self.env.behaviors({'proposal': f"tool('ok1', 'cat ~/{CP}/p/memory/MEMORY.md', 'm')\n"
                                        f"tool('ok1b', 'ls ~/{CP}/p/memory', 'MEMORY.md')\n"
                                        f"tool('ok2', 'cat {inputs}', 'b')\n"
                                        "mcp('ok3', 'context7', 'resolve-library-id', {'libraryName': 'x'}, result={'r': 1})\n"
                                        "tool('ok4', 'cat docs/plan.md', 'mentions PreToolUse hook text in a document')"})
        output = self.env.ok('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(output['violations'], [])
        self.assertEqual(output['tool_counts'], {'command_execution': 5, 'mcp_tool_call': 1, 'web_search': 0})

    def test_independence_marker_unit(self):
        sid = '20261008T000000Z-abcdef'
        with patch.object(co, 'USER_TMP', Path('/var/folders/aa/bb/T')):
            cases = {f'cat ~/{CP}/x/memory/a.md': [],
                     f'ls ~/{CP}/x/memory': [],
                     f'cat ~/{CP}/x/memory/../s.jsonl': ['.claude/projects'],
                     f'grep -r foo ~/{CP}': ['.claude/projects'],
                     f'cat .local/co-design/{sid}/artifacts/inputs/brief.md': [],
                     f'cat .local/co-design/{sid}/artifacts/claude/proposal.json': ['.local/co-design'],
                     'ls .local/co-design': ['.local/co-design'],
                     'ls .local/co-design-staging': [],
                     CTMP.replace('/private', 'ls '): ['/tmp/claude-'],
                     'cat /private/var/folders/aa/bb/T/co-design-x/logs': ['user-tmp/co-design-']}
            for text, expected in cases.items():
                with self.subTest(text=text):
                    self.assertEqual(co.independence_hits(text, sid), expected)

    def test_evasion_variants_are_detected(self):
        # レビューで指摘された迂回パターン（M2）。
        sid = '20261008T000000Z-abcdef'
        home_claude = '~/.' + 'claude'
        cases = [f"/bin/zsh -lc 'grep -rn X {home_claude}'",
                 "/bin/zsh -lc 'grep -Rn X $HOME'",
                 "/bin/zsh -lc 'rg X ..'",
                 "/bin/zsh -lc 'rg X ../..'",
                 "/bin/zsh -lc 'find /tmp -name \"*.jsonl\" -exec cat {} +'",
                 "/bin/zsh -lc 'ls $TMPDIR'",
                 "/bin/zsh -lc 'ls \"${TMPDIR}\"'",
                 f"/bin/zsh -lc 'tree -L 2 {Path.home()}'",
                 "/bin/zsh -lc 'du -sh /var/folders'",
                 "/bin/zsh -lc 'cd ~ && ls'",
                 "/bin/zsh -lc 'cat ~/.c*/p*/*/*.jsonl'",
                 f"/bin/zsh -lc 'cat {home_claude}//projects/-x/s.jsonl'",
                 f"/bin/zsh -lc 'cat {home_claude}/./projects/-x/s.jsonl'",
                 "/bin/zsh -lc 'cat ~/." + "Claude/Projects/-x/s.jsonl'",
                 "/bin/zsh -lc 'cat ~/.cl\\aude/proj\"ects\"/-x/s.jsonl'",
                 "/bin/zsh -lc 'cat /PRIVATE/" + "TMP/CLAUDE-502/x'"]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(co.independence_hits(text, sid, shell=True), text)

    def test_search_patterns_are_not_paths(self):
        # F1: rg/grep の検索パターンや find の式は経路として扱わない。
        sid = '20261008T000000Z-abcdef'
        commands = ["/bin/zsh -lc 'rg -n \"/tmp\" .'",
                    "/bin/zsh -lc \"grep -rn '\\$HOME' src\"",
                    "/bin/zsh -lc 'rg -n \"\\.\\.\" src'",
                    "/bin/zsh -lc 'rg -n \" / 1000\" .'",
                    "/bin/zsh -lc 'rg \"/var/[a-z]+\" .'",
                    "/bin/zsh -lc 'rg DARWIN_USER_TEMP_DIR .'",
                    "/bin/zsh -lc \"grep -rn -e /tmp -e '\\$HOME' src\"",
                    "/bin/zsh -lc 'grep -rne /tmp src'",
                    "/bin/zsh -lc \"rg -n --glob '!.local' foo .\"",
                    "/bin/zsh -lc 'grep -rn --exclude-dir=.local TODO .'",
                    "/bin/zsh -lc 'find . -path ./.local -prune -o -name \"*.py\" -print'",
                    "/bin/zsh -lc \"awk '{print \\$1}' src/a.txt\"",
                    "/bin/zsh -lc 'sed -n 1,40p src/app.py | rg -n \"/tmp\"'",
                    ['/bin/zsh', '-lc', 'rg -n "/tmp" src']]
        for command in commands:
            with self.subTest(command=command):
                text = command if isinstance(command, str) else ' '.join(command)
                self.assertEqual(co.independence_hits(text, sid, shell=True, command=command), [])

    def test_more_evasions(self):
        # F4 ほか: ブレース展開、find -exec 内、入れ子の sh -c、経路位置の DARWIN_USER_TEMP_DIR。
        sid = '20261008T000000Z-abcdef'
        for command in ("/bin/zsh -lc 'cat ~/.c{laude,}/projects/x/s.jsonl'",
                        "/bin/zsh -lc 'find . -name x -exec ls ~ ;'",
                        "/bin/zsh -lc 'sh -c \"ls /tmp\"'",
                        "/bin/zsh -lc 'getconf DARWIN_USER_TEMP_DIR'",
                        "/bin/zsh -lc 'find src /tmp -name x'"):
            with self.subTest(command=command):
                self.assertTrue(co.independence_hits(command, sid, shell=True))
        # 解析できない命令は保守的な全文検査に戻る。
        self.assertTrue(co.independence_hits("/bin/zsh -lc 'ls /tmp \"unterminated'", sid, shell=True))

    def test_output_path_detection(self):
        sid = '20261008T000000Z-abcdef'
        hits = {f'/Users/u/{CP}/-Users-u/0c9e.jsonl:3: text': ['claude-session-path'],
                f'/Users/u/{CP}/-Users-u/0c9e/tool-results/b.txt': ['claude-session-path'],
                f'{CTMP}/-Users-u/x/scratchpad/p.md': ['claude-tmp-path'],
                '/var/folders/a/b/T/co-design-' + OTHER_SID + '/logs/calls/1-proposal/result.json': ['co-design-work-path'],
                f'./.local/co-design/{OTHER_SID}/artifacts/claude/proposal.json:1:{{': ['co-design-artifact-path'],
                f'./.local/co-design/{sid}/artifacts/claude/proposal.json': ['co-design-artifact-path'],
                f'./.local/co-design/{sid}/artifacts/inputs/brief.md': [],
                f'/Users/u/{CP}/-Users-u/memory/MEMORY.md': [],
                f'x {CP}/-Users-u/0c9e.jsonl': ['claude-session-path'],
                f'{CP}/-Users-u/0c9e:': ['claude-session-path'],
                f'`{CP}/`(`<p>/memory` x)': [],
                'see `.claude/projects/` and `/tmp/claude-` in the docs': []}
        for text, expected in hits.items():
            with self.subTest(text=text):
                self.assertEqual(co.output_hits(text, sid), expected)

    def test_skill_files_do_not_false_positive(self):
        # 本スキルのファイルを読む命令・その出力では停止しない。
        sid = '20261008T000000Z-abcdef'
        for path in sorted(SKILL.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts:
                continue
            relative = '.claude/skills/cross-check/' + path.relative_to(SKILL).as_posix()
            with self.subTest(path=relative):
                self.assertEqual(co.output_hits(path.read_text(encoding='utf-8'), sid), [])
                for command in (f"/bin/zsh -lc 'sed -n 1,200p {relative}'", f"/bin/zsh -lc 'cat /work/project/{relative}'"):
                    self.assertEqual(co.independence_hits(command, sid, shell=True), [])
        for command in ("/bin/zsh -lc 'rg -n debate .claude/skills/cross-check'",
                        "/bin/zsh -lc 'ls .claude/skills/cross-check/scripts/debate/*.md'",
                        "/bin/zsh -lc 'find .claude/skills -name \"*.md\"'",
                        "/bin/zsh -lc 'grep -rn TODO src'", "/bin/zsh -lc 'ls -la'", "/bin/zsh -lc 'rg -n \"[a-z]+\" src'"):
            with self.subTest(command=command):
                self.assertEqual(co.independence_hits(command, sid, shell=True), [])

    def test_codex_raw_names_do_not_collide(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "tool('a/b', 'echo one', 'one')\ntool('a_b', 'echo two', 'two')"})
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        tools = {t['item_id']: t['output'] for t in co.load_tools(sid)}
        self.assertEqual((tools['a/b'], tools['a_b']), ('one', 'two'))


class RunnerTerminationTests(Base):
    def stale(self, sid, command):
        process = fx.subprocess_popen(command)
        self.addCleanup(fx.kill_group, process)
        session = self.env.session(sid)
        session['calls'] = [{'index': 1, 'phase': 'proposal', 'started_at': time.time(), 'ended_at': None,
                             'execution': 'running', 'status': None, 'timeout': 600, 'codex': str(self.env.fake),
                             'pid': process.pid, 'pgid': process.pid}]
        (self.env.artifacts / sid / 'artifacts/session.json').write_text(json.dumps(session))
        return process

    def test_pid_is_recorded(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        call = self.env.session(sid)['calls'][0]
        self.assertEqual(call['pid'], call['pgid'])
        self.assertIsInstance(call['pid'], int)
        self.assertEqual(call['codex'], str(self.env.fake))

    def test_stale_running_call_stops_session_and_kills_codex_group(self):
        sid = self.env.to_inputs_frozen()
        process = self.stale(sid, [sys.executable, str(self.env.fake), 'exec', '-o', '/dev/null', '-'])
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['status'], code), ('REFUSED', 2))
        self.assertIn('runner terminated', output['error'])
        session = self.stopped(sid, 'EXEC')
        self.assertEqual(session['calls'][0]['execution'], 'failed')
        self.assertIsNotNone(process.wait(timeout=5))
        self.assertEqual(self.env.calls(), [])
        # 回収後は報告用の finalize だけが通る。
        self.assertEqual(self.env.ok('finalize', '--session', sid)['state'], 'STOPPED')

    def test_finalize_proceeds_after_reaping(self):
        sid = self.env.to_inputs_frozen()
        process = self.stale(sid, [sys.executable, str(self.env.fake), 'exec', '-o', '/dev/null', '-'])
        final = self.env.ok('finalize', '--session', sid)
        self.assertEqual(final['state'], 'STOPPED')
        self.assertIsNotNone(process.wait(timeout=5))

    def test_reused_pid_with_other_start_time_is_not_killed(self):
        # F3: コマンドが一致しても開始時刻が記録と離れていれば別プロセスとみなす。
        sid = self.env.to_inputs_frozen()
        process = self.stale(sid, [sys.executable, str(self.env.fake), 'exec', '-o', '/dev/null', '-'])
        session = self.env.session(sid)
        session['calls'][0]['started_at'] = time.time() - 3600
        (self.env.artifacts / sid / 'artifacts/session.json').write_text(json.dumps(session))
        self.assertIn('runner terminated', self.env.run('call', '--session', sid, '--phase', 'proposal')[0]['error'])
        self.stopped(sid, 'EXEC')
        self.assertIsNone(process.poll())

    def test_stale_pid_of_other_program_is_not_killed(self):
        sid = self.env.to_inputs_frozen()
        process = self.stale(sid, ['/bin/sleep', '30'])
        output, _ = self.env.run('evidence', 'add', '--session', sid, '--file', self.env.project / 'src/app.py', '--lines', '1-1')
        self.assertIn('runner terminated', output['error'])
        self.stopped(sid, 'EXEC')
        self.assertIsNone(process.poll())

    def test_signal_during_call_stops_child_group(self):
        import signal
        import threading
        sid = self.env.to_inputs_frozen()
        pid_file = self.env.root / 'signal-child.pid'
        self.env.behaviors({'proposal': fx.hang_with_child(pid_file) + 'time.sleep(30)\n'})
        before = signal.getsignal(signal.SIGTERM)
        timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            started = time.monotonic()
            output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((output['status'], code), ('STOPPED', 2))
        self.assertIn('signal', output['stop']['reason'])
        self.stopped(sid, 'EXEC')
        self.assertTrue(fx.wait_for(pid_file))
        self.assertTrue(fx.process_gone(int(pid_file.read_text())))
        self.assertEqual(self.env.session(sid)['calls'][0]['execution'], 'failed')
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_repeated_signals_do_not_abort_cleanup(self):
        # F2: 2 回目以降のシグナルは無視され、後始末（グループ終了）が完了する。
        import signal
        import threading
        sid = self.env.to_inputs_frozen()
        pid_file = self.env.root / 'signal2-child.pid'
        self.env.behaviors({'proposal': fx.hang_with_child(pid_file) + 'time.sleep(30)\n'})
        timers = [threading.Timer(delay, os.kill, (os.getpid(), sig))
                  for delay, sig in ((1.0, signal.SIGINT), (1.02, signal.SIGINT), (1.04, signal.SIGTERM))]
        for timer in timers:
            timer.start()
        try:
            output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        finally:
            for timer in timers:
                timer.cancel()
        self.assertEqual(output['status'], 'STOPPED')
        self.assertIn('signal 2', output['stop']['reason'])
        self.assertTrue(fx.wait_for(pid_file))
        self.assertTrue(fx.process_gone(int(pid_file.read_text())))

    def test_signal_before_launch_stops_without_codex(self):
        import signal
        sid = self.env.to_inputs_frozen()
        original = co.ensure_dir

        def ensure_and_signal(path):
            os.kill(os.getpid(), signal.SIGTERM)
            return original(path)
        with patch.object(co, 'ensure_dir', ensure_and_signal):
            output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(output['status'], 'STOPPED')
        self.assertEqual(self.env.calls(), [])
        self.assertEqual(self.env.session(sid)['calls'][0]['execution'], 'failed')

    def test_runner_error_after_launch_kills_recorded_group(self):
        sid = self.env.to_inputs_frozen()
        pid_file = self.env.root / 'error-child.pid'
        self.env.behaviors({'proposal': fx.hang_with_child(pid_file) + "emit({'type': 'turn.started'})\ntime.sleep(30)\n"})
        original = co.Monitor.feed

        def feed(monitor, raw):
            if pid_file.exists() and pid_file.read_text().strip():
                raise RuntimeError('monitor failure')
            return original(monitor, raw)
        with patch.object(co.Monitor, 'feed', feed):
            output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['status'], code), ('ERROR', 2))
        session = self.stopped(sid, 'EXEC')
        self.assertIsInstance(session['calls'][0]['pgid'], int)
        self.assertTrue(fx.process_gone(int(pid_file.read_text())))

    def test_unexpected_error_is_one_line(self):
        sid = self.env.to_inputs_frozen()
        with patch.object(co, 'build_request', side_effect=KeyError('boom')):
            output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output, code), ({'status': 'ERROR', 'error': 'KeyError: unexpected runner error'}, 2))


class SchemaTests(Base):
    def valid_examples(self):
        proposal = self.env.claude_proposal(self.env.to_revealed())
        debate = {'status': 'COMPLETE', 'critique_of_claude': [{'issue_id': 'C1', 'type': 'fact', 'importance': 'high', 'claim': 'c',
                                                                 'evidence_refs': [], 'alternative': 'a', 'would_change_if': 'w'}],
                  'responses_to_claude_critique': [{'issue_id': 'I1', 'stance': 'accept', 'argument': 'a', 'evidence_refs': [],
                                                    'would_change_if': 'w'}],
                  'useful_from_other': [], 'useful_none_reason': 'none', 'revised_position_summary': 's', 'unverified': []}
        fidelity = {'status': 'COMPLETE', 'issues': [{'issue_id': 'I1', 'verdict': 'faithful', 'detail': 'd', 'evidence_refs': []}],
                    'unverified': []}
        scope = {'question': 'q', 'allowed_paths': ['src'], 'excluded_paths': [], 'allowed_tools': ['shell'],
                 'external_transfer': 'none', 'notes': []}
        return {'proposal': proposal, 'debate_codex': debate, 'fidelity': fidelity, 'scope': scope,
                'ledger': fx.final_ledger(), 'synthesis': fx.synthesis()}

    def test_valid_examples_and_single_field_breaks(self):
        for name, example in self.valid_examples().items():
            with self.subTest(schema=name, variant='valid'):
                co.validate_doc(example, name)
            schema = co.load_schema(name)
            # Codex 用スキーマは strict 規則（全属性 required・additionalProperties false）に従う。
            self.assertEqual(sorted(schema['required']), sorted(schema['properties']))
            self.assertIs(schema['additionalProperties'], False)
            for key in example:
                for label, change in (('missing', lambda d: d.pop(key)), ('wrong type', lambda d: d.__setitem__(key, 12345.5))):
                    broken = copy.deepcopy(example)
                    change(broken)
                    with self.subTest(schema=name, key=key, variant=label), self.assertRaises(co.SchemaError):
                        co.validate_doc(broken, name)
            broken = dict(copy.deepcopy(example), unexpected='x')
            with self.subTest(schema=name, variant='extra'), self.assertRaises(co.SchemaError):
                co.validate_doc(broken, name)
        broken = copy.deepcopy(self.valid_examples()['debate_codex'])
        broken['critique_of_claude'][0]['importance'] = 'critical'
        with self.assertRaises(co.SchemaError):
            co.validate_doc(broken, 'debate_codex')
        with self.assertRaises(co.SchemaError):
            co.validate_doc(dict(self.valid_examples()['fidelity'], unverified=['x' * (co.MAX_STR + 1)]), 'fidelity')

    def test_all_codex_schemas_are_strict_recursively(self):
        def walk(schema, path):
            if schema.get('type') == 'object':
                self.assertEqual(sorted(schema['required']), sorted(schema['properties']), path)
                self.assertIs(schema['additionalProperties'], False, path)
                for key, sub in schema['properties'].items():
                    walk(sub, path + '.' + key)
            if schema.get('type') == 'array':
                walk(schema['items'], path + '[]')
        for name in ('proposal', 'debate_codex', 'fidelity'):
            walk(co.load_schema(name), name)

    def test_invalid_codex_result_stops_as_schema(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "result.pop('risks')"})
        self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.stopped(sid, 'SCHEMA')

    def test_model_incomplete_continues_conditionally(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "result['status'] = 'INCOMPLETE'; result['unverified'] = ['missing contract']"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['status'], output['execution'], output['state'], code), ('INCOMPLETE', 'ok', 'CODEX_PROPOSED', 2))

    def test_scope_validation(self):
        sid = self.env.init()
        brief, _, index = self.env.inputs()
        for scope in ({'question': 'q', 'allowed_paths': ['/etc'], 'excluded_paths': [], 'allowed_tools': [],
                       'external_transfer': 'none', 'notes': []},
                      {'question': 'q', 'allowed_paths': ['../x'], 'excluded_paths': [], 'allowed_tools': [],
                       'external_transfer': 'none', 'notes': []},
                      {'question': 'q', 'allowed_paths': ['src'], 'excluded_paths': [], 'allowed_tools': ['node_repl'],
                       'external_transfer': 'none', 'notes': []},
                      {'question': '  ', 'allowed_paths': [], 'excluded_paths': [], 'allowed_tools': [],
                       'external_transfer': 'none', 'notes': []},
                      {'question': 'q', 'allowed_paths': [' '], 'excluded_paths': [], 'allowed_tools': [],
                       'external_transfer': 'none', 'notes': []}):
            with self.subTest(scope=scope):
                path = self.env.file('scope.json', scope)
                output, _ = self.env.run('freeze-inputs', '--session', sid, '--brief', brief, '--scope', path, '--index', index)
                self.assertEqual(output['status'], 'INVALID')
        self.assertEqual(self.env.session(sid)['state'], 'INIT')

    def test_scope_allows_empty_allowed_paths_for_no_code_questions(self):
        # コード調査なしの質問は allowed_paths を空にできる（偽の経路を入れさせない）。
        sid = self.env.init()
        brief, _, index = self.env.inputs()
        path = self.env.file('scope.json', {'question': 'q', 'allowed_paths': [], 'excluded_paths': ['src'],
                                            'allowed_tools': ['web'], 'external_transfer': 'public-docs-queries-only',
                                            'notes': []})
        output, code = self.env.run('freeze-inputs', '--session', sid, '--brief', brief, '--scope', path, '--index', index)
        self.assertEqual((output['status'], code), ('ok', 0), output)
        self.assertEqual(self.env.session(sid)['state'], 'INPUTS_FROZEN')


class DebatedMixin:
    def debated(self):
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.ok('call', '--session', sid, '--phase', 'debate')
        return sid


class IssueResponseTests(DebatedMixin, Base):
    def test_missing_codex_response_is_incomplete(self):
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1'), fx.issue('I2', importance='medium')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.behaviors({'debate': "result['responses_to_claude_critique'] = result['responses_to_claude_critique'][:1]"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'debate')
        self.assertEqual((output['status'], output['execution'], output['state'], code), ('INCOMPLETE', 'ok', 'DEBATED', 2))
        self.assertIn('I2', ' '.join(output['problems']))

    def test_frozen_critique_phase_rules(self):
        sid = self.env.to_revealed(plan_b=True)
        self.env.behaviors({'critique': "result['responses_to_claude_critique'] = [{'issue_id': 'I1', 'stance': 'accept', "
                                        "'argument': 'a', 'evidence_refs': [], 'would_change_if': 'w'}]"})
        output, _ = self.env.run('call', '--session', sid, '--phase', 'critique')
        self.assertEqual(output['status'], 'INCOMPLETE')
        self.assertIn('must be empty in critique phase', ' '.join(output['problems']))
        # B 案の Claude 批評は Codex 批評呼び出しの終了後に作ったファイルだけを受け付ける。
        self.assertEqual(self.env.run('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')[0]['status'], 'REFUSED')

    def test_pre_debate_rules(self):
        sid = self.env.to_revealed()
        for document, message in ((fx.ledger([fx.issue('I1', claude='pending')]), 'Claude position'),
                                  (fx.ledger([fx.issue('I1', kind='fact', method='user-decision')]), 'not user decision'),
                                  (fx.ledger([fx.issue('I1', result='confirmed', evidence=['E99'])]), 'unknown evidence'),
                                  (fx.ledger([fx.issue('I1', kind='fact', result='confirmed', evidence=['E1'], method='undecidable')]),
                                   'source-text or reproduction'),
                                  (fx.ledger([fx.issue('I1'), fx.issue('I1')]), 'unique')):
            with self.subTest(message=message):
                self.env.write_artifact(sid, 'ledger.json', document)
                output, _ = self.env.run('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
                self.assertEqual(output['status'], 'INVALID')
                self.assertIn(message, output['error'])
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1', kind='fact', result='confirmed', evidence=['E1', 'X1'],
                                                                         method='source-text', status='resolved-by-evidence')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')

    def test_synthesis_refused_without_final_stances_or_answers(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis())
        for document, message in ((fx.final_ledger(i1_codex='pending'), 'final stances'),
                                  (fx.ledger([fx.final_ledger()['issues'][0]]), 'missing from the ledger'),
                                  (fx.ledger([fx.final_ledger()['issues'][0],
                                              dict(fx.final_ledger()['issues'][1], turns=[])]), 'not answered')):
            with self.subTest(message=message):
                self.env.write_artifact(sid, 'ledger.json', document)
                output, _ = self.env.run('synthesis', 'validate', '--session', sid)
                self.assertEqual(output['status'], 'INVALID')
                self.assertIn(message, output['error'])
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', dict(fx.synthesis(), useful_from_codex=[], useful_none_reason=''))
        self.assertIn('useful_none_reason', self.env.run('synthesis', 'validate', '--session', sid)[0]['error'])
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(checked=True))
        self.assertIn('codex_checked', self.env.run('synthesis', 'validate', '--session', sid)[0]['error'])


class ConditionalTests(DebatedMixin, Base):
    def validate(self, sid, ledger, synthesis, *extra):
        self.env.write_artifact(sid, 'ledger.json', ledger)
        self.env.write_artifact(sid, 'synthesis.json', synthesis)
        return self.env.ok('synthesis', 'validate', '--session', sid, *extra)

    def test_none(self):
        sid = self.debated()
        output = self.validate(sid, fx.final_ledger(), fx.synthesis(adopt_i1='codex'))
        self.assertEqual(output['next'], 'none')
        self.assertIn('Codex 미확인', output['note'])
        self.assertEqual(self.env.run('call', '--session', sid, '--phase', 'fidelity')[0]['status'], 'REFUSED')
        self.assertEqual(self.env.run('call', '--session', sid, '--phase', 'round2')[0]['status'], 'REFUSED')

    def test_fidelity_reasons(self):
        sid = self.debated()
        cases = {'adopted claude': (fx.final_ledger(), fx.synthesis()),
                 'omitted high': (fx.final_ledger(), dict(fx.synthesis(adopt_i1='codex'), decisions=fx.synthesis()['decisions'][1:])),
                 'stance differs': (fx.final_ledger(i1_codex='accept'), fx.synthesis(adopt_i1='codex')),
                 'conditions dropped': (fx.ledger([dict(fx.final_ledger()['issues'][0], codex=dict(fx.final_ledger()['issues'][0]['codex'], stance='conditional')),
                                                   fx.final_ledger()['issues'][1]]), fx.synthesis(adopt_i1='codex'))}
        for name, (ledger, synthesis) in cases.items():
            with self.subTest(case=name):
                output = self.validate(sid, ledger, synthesis)
                self.assertEqual(output['next'], 'fidelity', output)
                self.assertIn('I1', output['issue_ids'])

    def round2_ledger(self, sid):
        evidence = self.env.ok('evidence', 'add', '--session', sid, '--file', self.env.project / 'src/app.py', '--lines', '4-4')['id']
        first = fx.issue('I1', codex='reject', method='source-text', status='resolved-by-evidence', result='confirmed',
                         evidence=[evidence], kind='fact')
        return fx.ledger([first, fx.final_ledger()['issues'][1]])

    def test_round2_by_new_evidence_then_both(self):
        sid = self.debated()
        ledger = self.round2_ledger(sid)
        output = self.validate(sid, ledger, fx.synthesis(adopt_i1='codex'))
        self.assertEqual((output['next'], output['issue_ids']), ('round2', ['I1']))
        # round2 と fidelity の両方に該当すれば round2 を先にする。
        output = self.validate(sid, ledger, fx.synthesis())
        self.assertEqual(output['next'], 'round2')
        self.assertTrue(self.env.session(sid)['conditional']['deferred_fidelity'])
        self.env.ok('call', '--session', sid, '--phase', 'round2')
        self.assertEqual(self.env.run('call', '--session', sid, '--phase', 'fidelity')[0]['status'], 'REFUSED')
        # D1: round2 は討論なので codex_checked=true の根拠にならない。
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(checked=True))
        refused = self.env.run('synthesis', 'validate', '--session', sid)[0]
        self.assertEqual(refused['status'], 'INVALID')
        self.assertIn('round2 does not count', refused['error'])
        output = self.validate(sid, ledger, fx.synthesis())
        self.assertEqual((output['next'], output['blocked']), ('none', 'conditional budget already used by round2'))

    def test_user_requested_round2(self):
        sid = self.debated()
        output = self.validate(sid, fx.final_ledger(), fx.synthesis(adopt_i1='codex'), '--user-requested')
        self.assertEqual(output['next'], 'round2')
        self.env.ok('call', '--session', sid, '--phase', 'round2')
        self.assertEqual(self.env.calls(), ['proposal', 'debate', 'round2'])

    def test_budget_exhausted_blocks_conditional(self):
        sid = self.debated()
        used = co.codex_used(self.env.session(sid))
        self.env.edit_session(sid, codex_budget_seconds=used + 30)
        output = self.validate(sid, fx.final_ledger(), fx.synthesis())
        self.assertEqual((output['computed'], output['next']), ('fidelity', 'none'))
        self.assertIn('BUDGET_EXHAUSTED', output['blocked'])
        self.assertEqual(self.env.run('call', '--session', sid, '--phase', 'fidelity')[0]['status'], 'REFUSED')
        self.assertEqual(self.env.ok('finalize', '--session', sid)['state'], 'FINALIZED')

    def test_changed_synthesis_requires_revalidation(self):
        sid = self.debated()
        self.validate(sid, fx.final_ledger(), fx.synthesis())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(conditions=['x']))
        self.assertIn('validate again', self.env.run('call', '--session', sid, '--phase', 'fidelity')[0]['error'])


class LinkTests(Base):
    def test_linked_and_unbacked(self):
        sid = self.env.to_revealed()
        links = json.loads((self.env.artifacts / sid / 'artifacts/codex/proposal_links.json').read_text())
        by_id = {l['id']: l['link'] for l in links['links']}
        self.assertEqual(by_id, {'X1': 'linked', 'X2': 'unbacked', 'X3': 'input'})
        diff = json.loads((self.env.artifacts / sid / 'artifacts/reveal/diff.json').read_text())
        self.assertEqual(diff['unbacked_codex'], [{'id': 'X2', 'ref': 'src/other.py:L1-L1'}])
        self.assertEqual(diff['one_sided']['codex_only'], ['src/other.py'])

    def test_link_ref_kinds(self):
        tools = [{'item_id': 't1', 'text': "sed -n 1,5p remote/src/a.ts", 'output': ''},
                 {'item_id': 'w1', 'text': 'search', 'output': 'see https://example.com/doc'},
                 {'item_id': 'm1', 'text': 'context7/resolve {}', 'output': '', 'server': 'context7'}]
        counts = {'brief': 3, 'scope': 1, 'index': 1}
        cases = {'remote/src/a.ts:L1-L5': 'linked', f'{co.PROJECT}/remote/src/a.ts:L2': 'linked',
                 'other/b.ts:L1': 'unbacked', 'https://example.com/doc': 'linked', 'https://example.com/x': 'unbacked',
                 'brief:L3': 'input', 'brief:L4': 'unbacked', 'context7 resolve': 'linked', 'X1': 'linked', 'X9': 'unbacked'}
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(co.link_ref(ref, tools, counts, {'X1'})[0], expected)

    def citation_context(self):
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis())
        return sid, co.link_context(self.env.session(sid), 'debate', None), co.load_tools(sid)

    def test_prefixed_and_bare_citations(self):
        # E2E-6 実測形式 "X6: path:Lx-Ly" を含む前置 ID 付き引用の解決。
        sid, ctx, tools = self.citation_context()
        cases = {
            'X1: src/app.py:L1-L3': ('linked', None, False),
            'X1 - src/app.py:L2': ('linked', None, False),
            'E1: src/app.py:L2-L3': ('linked', None, False),
            'X3: brief:L1': ('input', None, False),
            'X1': ('linked', 'codex-evidence', False),
            'X1:': ('linked', 'codex-evidence', False),
            'X3': ('input', 'codex-evidence', False),
            'X2': ('unbacked', 'codex-evidence', False),
            'E1': ('linked', 'claude-evidence', False),
            'X99': ('unbacked', None, False),
            'E99': ('unbacked', None, False),
            'X99: free text that no tool ever showed': ('unbacked', None, False),
            'X1: src/other.py:L1': ('unbacked', None, True),
            'X1: brief:L1': ('input', None, True),
            'E1: brief:L2': ('input', None, True),
            'X10: inference from brief:L1': ('inference', None, False),
            'synthesis:L1': ('input', None, False),
            'synthesis:L999': ('unbacked', None, False),
            'ledger:I1': ('input', None, False),
            'ledger:I9': ('unbacked', None, False),
            'earlier proposal: risks[0]': ('input', None, False),
            'earlier critique output: critique_of_claude[C1]': ('unbacked', None, False),
        }
        for ref, (link, via, mismatch) in cases.items():
            with self.subTest(ref=ref):
                result = co.link_citation(ref, tools, ctx)
                self.assertEqual((result['link'], result['via'], result['prefix_mismatch']), (link, via, mismatch))

    def test_debate_links_use_tools_from_earlier_calls_and_keep_count_keys(self):
        # debate 呼び出し自体は道具を使わず、proposal 呼び出しで読んだファイルを "X1: path" 形式で引用する。
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.behaviors({'debate': "result['critique_of_claude'][0]['evidence_refs'] = ['X1: src/app.py:L1-L3', 'X2: src/app.py:L2', 'E1', 'X3']\n"
                                      "result['responses_to_claude_critique'][0]['evidence_refs'] = ['X1', 'X7: missing.py:L1']"})
        output = self.env.ok('call', '--session', sid, '--phase', 'debate')
        self.assertEqual(output['tool_counts'], {'command_execution': 0, 'mcp_tool_call': 0, 'web_search': 0})
        # D5: excerpt・mention を追加し、既存の linked は linked = excerpt + mention として残す。
        self.assertEqual(output['links'], {'linked': 4, 'unbacked': 1, 'input': 1, 'inference': 0, 'excerpt': 4, 'mention': 0,
                                           'prefix_mismatch': 1})
        links = json.loads((self.env.artifacts / sid / 'artifacts/codex/debate_links.json').read_text())
        self.assertEqual({k for k in links['counts']} >= set(co.LINK_KINDS), True)
        self.assertEqual(json.loads(Path(output['summary_path']).read_text())['links'], output['links'])

    def test_hash_mismatch_detected_at_reveal(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        proposal = self.env.file('p.json', self.env.claude_proposal(sid))
        (self.env.project / 'src/app.py').write_text('changed\nline2x\nline3\nline4\n')
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        self.assertGreaterEqual(self.env.ok('reveal', '--session', sid)['diff_counts']['hash_mismatch'], 2)


class SummaryTests(Base):
    def test_call_summaries_import_and_session_summary_rejected(self):
        metrics = find_task_metrics()
        if metrics is None:
            self.skipTest('tools/task_metrics not found above this skill')
        sid, _, final = fx.run_plan_a(self.env)
        self.assertEqual(len(final['call_summaries']), 3)
        for path in final['call_summaries']:
            record = dict(version=1, recorded_at='2026-10-08T00:00:00+00:00', task='t', run='r', provider='codex',
                          workflow='debate', kind='feature', verification='unknown', source='unknown',
                          scope='declared-run', execution_status=None, **dict.fromkeys(metrics.NUMBERS))
            metrics.import_summary(path, record)
            metrics.validate(record)
            self.assertEqual(record['execution_status'], 'COMPLETE')
        record = dict(record, input_tokens=None, cached_input_tokens=None, output_tokens=None, elapsed_seconds=None,
                      source='unknown', scope='declared-run', execution_status=None)
        with self.assertRaises(ValueError):
            metrics.import_summary(final['session_summary'], record)

    def test_session_summary_contents(self):
        sid, _, final = fx.run_plan_a(self.env, fidelity=False)
        summary = json.loads(Path(final['session_summary']).read_text())
        self.assertEqual((summary['codex_calls_total'], summary['violations'], summary['over_budget']), (2, 0, False))
        self.assertNotIn('codex_calls', summary)
        self.assertNotIn('mode', summary)
        self.assertEqual(self.env.run('finalize', '--session', sid)[0]['status'], 'REFUSED')
        text = (self.env.artifacts / sid / 'artifacts/synthesis.md').read_text()
        self.assertIn('Claude 통합 판단(Codex 미확인)', text)

    def test_stopped_session_can_be_finalized_for_reporting(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "emit({'type': 'error', 'message': 'x'})"})
        self.env.run('call', '--session', sid, '--phase', 'proposal')
        final = self.env.ok('finalize', '--session', sid)
        self.assertEqual(final['state'], 'STOPPED')
        summary = json.loads(Path(final['session_summary']).read_text())
        self.assertEqual((summary['final_state'], summary['violations']), ('STOPPED', 1))


class PruneTests(Base):
    def test_list_by_default_and_apply_only_expired_evidence_logs(self):
        old = self.env.tmp / 'co-design-20260901T000000Z-aaaaaa'
        mid = self.env.tmp / 'co-design-20261003T000000Z-bbbbbb'
        new = self.env.tmp / 'co-design-20261008T000000Z-cccccc'
        for base in (old, mid, new):
            for name in ('evidence', 'logs'):
                (base / name).mkdir(parents=True)
                (base / name / 'x.txt').write_text('x')
        (self.env.tmp / 'unrelated').mkdir()
        artifact = self.env.artifacts / '20260901T000000Z-aaaaaa/artifacts'
        artifact.mkdir(parents=True)
        (artifact / 'ledger.json').write_text('{}')
        fixed = datetime_ts('2026-10-12T00:00:00')
        with patch.object(co, 'now', lambda: fixed):
            listed = self.env.ok('prune')
            self.assertFalse(listed['applied'])
            self.assertEqual(sorted(Path(t['path']).relative_to(self.env.tmp).as_posix() for t in listed['targets']),
                             ['co-design-20260901T000000Z-aaaaaa' + '/evidence', 'co-design-20260901T000000Z-aaaaaa' + '/logs',
                              'co-design-20261003T000000Z-bbbbbb' + '/logs'])
            self.assertTrue((old / 'evidence/x.txt').exists())
            self.env.ok('prune', '--apply')
        self.assertFalse((old / 'evidence').exists())
        self.assertFalse((old / 'logs').exists())
        self.assertFalse((mid / 'logs').exists())
        self.assertTrue((mid / 'evidence/x.txt').exists())
        self.assertTrue((new / 'logs/x.txt').exists())
        self.assertTrue((artifact / 'ledger.json').exists())
        self.assertTrue((self.env.tmp / 'unrelated').exists())

    def test_symlinked_work_dir_is_ignored(self):
        target = self.env.root / 'elsewhere'
        (target / 'logs').mkdir(parents=True)
        (self.env.tmp / 'co-design-20260901T000000Z-dddddd').symlink_to(target, target_is_directory=True)
        with patch.object(co, 'now', lambda: datetime_ts('2026-10-12T00:00:00')):
            self.assertEqual(self.env.ok('prune', '--apply')['targets'], [])
        self.assertTrue((target / 'logs').exists())


def datetime_ts(value):
    from datetime import datetime, timezone
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class InputAndVersionTests(Base):
    def test_protected_and_symlink_inputs_refused(self):
        sid = self.env.init()
        brief, scope, index = self.env.inputs()
        protected = self.env.root / '.aws'
        protected.mkdir()
        dummy = protected / 'not-a-secret.md'
        dummy.write_text('dummy')
        linked = self.env.root / 'linked.md'
        linked.symlink_to(brief)
        for bad in (dummy, linked):
            with self.subTest(bad=bad.name):
                output, _ = self.env.run('freeze-inputs', '--session', sid, '--brief', bad, '--scope', scope, '--index', index)
                self.assertEqual(output['status'], 'REFUSED')
        self.env.ok('freeze-inputs', '--session', sid, '--brief', brief, '--scope', scope, '--index', index)
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        for bad in (dummy, linked, self.env.tmp / f'co-design-{sid}/logs/calls/1-proposal/result.json'):
            with self.subTest(evidence=bad.name):
                output, _ = self.env.run('evidence', 'add', '--session', sid, '--file', bad, '--lines', '1-1')
                self.assertEqual(output['status'], 'REFUSED')

    def test_version_gate(self):
        gate = self.env.artifacts / 'verified_codex_version'
        gate.write_text('codex-cli 9.9.9\n')
        output, _ = self.env.run('init', '--question-id', 'q', '--no-auto-verify')
        self.assertIn('version gate', output['error'])
        gate.unlink()
        self.assertIn('missing', self.env.run('init', '--question-id', 'q', '--no-auto-verify')[0]['error'])
        self.assertEqual(list(self.env.artifacts.iterdir()), [])
        gate.write_text(fx.VERSION + '\n')
        sid = self.env.to_inputs_frozen()
        Path(str(self.env.fake) + '.version').write_text('codex-cli 0.0.1-changed')
        output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(output['stop']['category'], 'VERSION')
        self.assertEqual(self.env.calls(), [])


class ReviewFollowUpTests(DebatedMixin, Base):
    def test_scope_transfer_and_tool_enum(self):
        # m5: external_transfer none と web/context7 の併用、playwright は受け付けない。
        sid = self.env.init()
        brief, _, index = self.env.inputs()
        base = {'question': 'q', 'allowed_paths': ['src'], 'excluded_paths': [], 'notes': []}
        for tools, transfer in ((['shell', 'context7'], 'none'), (['web'], 'none'), (['playwright'], 'public-docs-queries-only')):
            with self.subTest(tools=tools, transfer=transfer):
                path = self.env.file('scope.json', dict(base, allowed_tools=tools, external_transfer=transfer))
                output, _ = self.env.run('freeze-inputs', '--session', sid, '--brief', brief, '--scope', path, '--index', index)
                self.assertEqual(output['status'], 'INVALID')
        path = self.env.file('scope.json', dict(base, allowed_tools=['shell', 'aws-api'], external_transfer='none'))
        self.env.ok('freeze-inputs', '--session', sid, '--brief', brief, '--scope', path, '--index', index)

    def test_tampered_proposal_log_stops_at_reveal(self):
        # m7: 呼び出し時のハッシュと異なる結果・連結ファイルは公開しない。
        for name in ('result.json', 'links.json'):
            with self.subTest(file=name):
                sid = self.env.to_inputs_frozen()
                self.env.ok('call', '--session', sid, '--phase', 'proposal')
                proposal = self.env.file('p.json', self.env.claude_proposal(sid))
                self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
                # D3: 結果・連結ファイルは恒久領域（.local/co-design/<sid>/work/）から公開する。
                target = self.env.artifacts / sid / 'work/calls/1-proposal' / name
                target.write_text(target.read_text().replace('"', '"', 1) + ' ')
                output, code = self.env.run('reveal', '--session', sid)
                self.assertEqual((output['status'], code), ('STOPPED', 2))
                self.stopped(sid, 'INPUT_CHANGED')
                self.assertFalse((self.env.artifacts / sid / 'artifacts/codex/proposal.json').exists())

    def test_file_citation_linked_from_tool_output(self):
        # m8: rg などの出力に現れたパスも連結根拠にする。
        tools = [{'item_id': 'r1', 'text': 'rg -n needle', 'output': 'remote/src/b.ts:12: needle'}]
        self.assertEqual(co.link_ref('remote/src/b.ts:L12', tools, {}, set()), ('linked', ['r1']))

    def test_fidelity_for_any_non_codex_adoption(self):
        # m10: Codex 最終 stance が accept でなく、Codex 案以外を採った場合はすべて fidelity 対象。
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        for adopted in ('claude', 'both', 'neither', 'evidence'):
            with self.subTest(adopted=adopted):
                self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1=adopted))
                output = self.env.ok('synthesis', 'validate', '--session', sid)
                self.assertEqual((output['next'], output['issue_ids']), ('fidelity', ['I1']))
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex'))
        self.assertEqual(self.env.ok('synthesis', 'validate', '--session', sid)['next'], 'none')


class CheckedBindingTests(DebatedMixin, Base):
    """D1: Codex 確認表示を fidelity 時点の ledger・synthesis 内容に結び付ける。"""

    def fidelity_done(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis())
        self.assertEqual(self.env.ok('synthesis', 'validate', '--session', sid)['next'], 'fidelity')
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        return sid

    def test_fidelity_records_content_basis(self):
        sid = self.fidelity_done()
        call = co.phase_call(self.env.session(sid), 'fidelity')
        self.assertEqual(call['checked_basis'], dict(co.check_basis(fx.final_ledger(), fx.synthesis()), issue_ids=['I1']))
        # codex_checked だけを変えた synthesis は同じ内容とみなす（確認後に旗を立てられる）。
        self.assertTrue(co.basis_matches(self.env.session(sid), fx.final_ledger(), fx.synthesis(checked=True)))
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(checked=True))
        self.env.ok('synthesis', 'validate', '--session', sid)

    def test_edit_after_fidelity_removes_checked(self):
        sid = self.fidelity_done()
        session = self.env.session(sid)
        self.assertEqual(co.checked_issues(session, fx.final_ledger(), fx.synthesis(checked=True)), {'I1'})
        for name, ledger, synthesis in (
                ('synthesis edited', fx.final_ledger(), fx.synthesis(checked=True, conditions=['new condition'])),
                ('ledger edited', fx.ledger([fx.final_ledger()['issues'][0], dict(fx.final_ledger()['issues'][1], title='changed')]),
                 fx.synthesis(checked=True))):
            with self.subTest(case=name):
                self.assertEqual(co.checked_issues(session, ledger, synthesis), set())
                self.env.write_artifact(sid, 'ledger.json', ledger)
                self.env.write_artifact(sid, 'synthesis.json', synthesis)
                output = self.env.run('synthesis', 'validate', '--session', sid)[0]
                self.assertEqual(output['status'], 'INVALID')
                self.assertIn('codex_checked=true', output['error'])
        # 旗を下ろせば通り、報告は「Codex 미확인」になる。
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(conditions=['new condition']))
        self.env.ok('synthesis', 'validate', '--session', sid)
        final = self.env.ok('finalize', '--session', sid)
        self.assertIn('- 판정 기준 내용과 현재 통합안·원장 일치: 아니오', Path(final['report']).read_text())
        text = (self.env.artifacts / sid / 'artifacts/synthesis.md').read_text()
        self.assertNotIn('Codex 확인됨', text)
        self.assertIn('Claude 통합 판단(Codex 미확인)', text)

    def test_faithful_on_unlisted_issue_is_not_checked(self):
        # M1: 依頼していない争点（C1）への faithful は問題として記録し、確認根拠にしない。
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis())
        self.assertEqual(self.env.ok('synthesis', 'validate', '--session', sid)['issue_ids'], ['I1'])
        self.env.behaviors({'fidelity': "result['issues'].append({'issue_id': 'C1', 'verdict': 'faithful', 'detail': 'ok', "
                                        "'evidence_refs': []})"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'fidelity')
        self.assertEqual((output['execution'], output['status'], code), ('ok', 'INCOMPLETE', 2))
        self.assertIn('fidelity verdict on unlisted issues: C1', output['problems'])
        session = self.env.session(sid)
        self.assertEqual(co.phase_call(session, 'fidelity')['checked_basis']['issue_ids'], ['I1'])
        both = dict(fx.synthesis(checked=True), decisions=[dict(d, codex_checked=True) for d in fx.synthesis()['decisions']])
        self.assertEqual(co.checked_issues(session, fx.final_ledger(), both), {'I1'})
        self.env.write_artifact(sid, 'synthesis.json', both)
        refused = self.env.run('synthesis', 'validate', '--session', sid)[0]
        self.assertEqual(refused['status'], 'INVALID')
        self.assertTrue(refused['error'].endswith(': C1'), refused['error'])
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(checked=True))
        self.env.ok('synthesis', 'validate', '--session', sid)

    def test_compact_faithful_on_omitted_issue_is_not_checked(self):
        # M1: compact で決定を送らなかった争点（EXCERPT FILTER.omitted）への faithful は認めない。
        sid = self.env.to_revealed(compact=True)
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1'), fx.issue('I2', importance='low')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.ok('call', '--session', sid, '--phase', 'debate')
        ledger = fx.final_ledger(extra=[fx.issue('I2', importance='low', codex='reject', claude='proposed')])
        extra = {'issue_id': 'I2', 'decision': 'Keep', 'adopted_from': 'codex', 'reason': 'low', 'rejected_alternatives': [],
                 'conditions': [], 'residual_risk': 'none', 'codex_checked': False}
        self.env.write_artifact(sid, 'ledger.json', ledger)
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(decisions_extra=[extra]))
        self.assertEqual(self.env.ok('synthesis', 'validate', '--session', sid)['issue_ids'], ['I1'])
        self.env.behaviors({'fidelity': "assert 'I2' in json.loads(section('EXCERPT FILTER'))['omitted']\n"
                                        "result['issues'] += [{'issue_id': i, 'verdict': 'faithful', 'detail': 'ok', "
                                        "'evidence_refs': []} for i in ('I2', 'C1')]"})
        output, _ = self.env.run('call', '--session', sid, '--phase', 'fidelity')
        self.assertEqual(output['execution'], 'ok')
        self.assertIn('fidelity verdict on unlisted issues: C1, I2', output['problems'])
        session = self.env.session(sid)
        self.assertEqual(co.phase_call(session, 'fidelity')['checked_basis']['issue_ids'], ['I1'])
        flagged = fx.synthesis(decisions_extra=[dict(extra, codex_checked=True)], checked=True)
        self.assertEqual(co.checked_issues(session, ledger, flagged), {'I1'})
        self.env.write_artifact(sid, 'synthesis.json', flagged)
        self.assertIn('I2', self.env.run('synthesis', 'validate', '--session', sid)[0]['error'])
        self.assertEqual(co.fidelity_coverage(session, ledger, flagged), ['I1'])

    def test_round2_alone_never_counts_as_checked(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex'))
        self.assertEqual(self.env.ok('synthesis', 'validate', '--session', sid, '--user-requested')['next'], 'round2')
        self.env.ok('call', '--session', sid, '--phase', 'round2')
        session = self.env.session(sid)
        self.assertEqual(co.checked_issues(session, fx.final_ledger(), fx.synthesis(adopt_i1='codex', checked=True)), set())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex', checked=True))
        output = self.env.run('synthesis', 'validate', '--session', sid)[0]
        self.assertEqual(output['status'], 'INVALID')
        self.assertIn('round2 does not count', output['error'])

    def test_finalize_refuses_after_change_and_renders_verified_check(self):
        sid = self.fidelity_done()
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(checked=True))
        self.env.ok('synthesis', 'validate', '--session', sid)
        for name in ('synthesis.json', 'ledger.json'):
            with self.subTest(changed=name):
                path = self.env.artifacts / sid / 'artifacts' / name
                original = path.read_text()
                path.write_text(original + '\n')
                output, code = self.env.run('finalize', '--session', sid)
                self.assertEqual((output['status'], code), ('REFUSED', 2))
                self.assertIn('validate again', output['error'])
                path.write_text(original)
        final = self.env.ok('finalize', '--session', sid)
        summary = json.loads(Path(final['session_summary']).read_text())
        self.assertEqual(summary['codex_checked_verified'], ['I1'])
        report = Path(final['report']).read_text()
        self.assertIn('- 판정 대상 쟁점: I1', report)
        self.assertIn('- 판정 기준 내용과 현재 통합안·원장 일치: 예', report)
        for name in ('synthesis.md', 'report.md'):
            text = (self.env.artifacts / sid / 'artifacts' / name).read_text()
            self.assertIn('Codex 확인됨(충실성 검사: 충실)', text)


class PermanentStoreTests(DebatedMixin, Base):
    """D3: 最小根拠の恒久保存と欠落の正直な報告。"""

    def drop_tmp(self, sid):
        shutil.rmtree(self.env.tmp / f'co-design-{sid}')

    def test_reveal_round2_finalize_after_tmp_loss(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        self.drop_tmp(sid)
        proposal = self.env.file('p.json', self.env.claude_proposal(sid))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        self.env.ok('reveal', '--session', sid)
        store = self.env.artifacts / sid / 'work'
        for name in ('evidence/claude/E1.txt', 'evidence/codex_tools.jsonl', 'calls/1-proposal/summary.json',
                     'calls/1-proposal/result.json', 'calls/1-proposal/links.json'):
            self.assertTrue((store / name).is_file(), name)
        self.assertTrue(list((store / 'evidence/codex_raw').iterdir()))
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.ok('call', '--session', sid, '--phase', 'debate')
        evidence = self.env.ok('evidence', 'add', '--session', sid, '--file', self.env.project / 'src/app.py', '--lines', '4-4')['id']
        self.drop_tmp(sid)
        first = fx.issue('I1', codex='reject', method='source-text', status='resolved-by-evidence', result='confirmed',
                         evidence=[evidence], kind='fact')
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([first, fx.final_ledger()['issues'][1]]))
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex'))
        self.assertEqual(self.env.ok('synthesis', 'validate', '--session', sid)['next'], 'round2')
        # round2 の新根拠は恒久領域の抜粋から送る（"(capture unavailable)" にならない）。
        self.env.behaviors({'round2': "assert 'line4' in section('NEW EVIDENCE SINCE DEBATE'), 'evidence text missing'"})
        self.env.ok('call', '--session', sid, '--phase', 'round2')
        self.drop_tmp(sid)
        self.env.ok('synthesis', 'validate', '--session', sid)
        final = self.env.ok('finalize', '--session', sid)
        summary = json.loads(Path(final['session_summary']).read_text())
        self.assertEqual((summary['summaries_missing'], summary['usage_status'], summary['violations']), (0, 'complete', 0))
        self.assertEqual(summary['usage_total'], {'input_tokens': 150, 'cached_input_tokens': 30, 'output_tokens': 15})
        self.assertEqual(len(final['call_summaries']), 3)
        self.assertTrue(all(Path(p).is_file() and str(store) in p for p in final['call_summaries']))
        self.assertEqual([c['summary'] for c in self.env.session(sid)['calls']], final['call_summaries'])

    def test_missing_summaries_are_reported_not_zero(self):
        for removed, expected in ((1, ('partial', 1, None, 'unknown')), (2, ('unknown', 2, None, 'unknown'))):
            with self.subTest(removed=removed):
                sid, _, _ = self.partial_session()
                for index, phase in ((1, 'proposal'), (2, 'debate'))[:removed]:
                    (self.env.artifacts / sid / f'work/calls/{index}-{phase}/summary.json').unlink()
                final = self.env.ok('finalize', '--session', sid)
                summary = json.loads(Path(final['session_summary']).read_text())
                self.assertEqual((summary['usage_status'], summary['summaries_missing'], summary['violations'],
                                  summary['violations_status']), expected)
                self.assertEqual(final['summaries_missing'], removed)
                if removed == 1:
                    self.assertEqual(summary['usage_total'], {'input_tokens': 50, 'cached_input_tokens': 10, 'output_tokens': 5})
                else:
                    self.assertIsNone(summary['usage_total'])
                self.assertEqual([c['summary'] for c in summary['calls']], ['missing'] * removed + ['ok'] * (2 - removed))
                report = (self.env.artifacts / sid / 'artifacts/report.md').read_text()
                self.assertIn(f'요약 누락: {removed}건', report)
                self.assertIn('위반: 알 수 없음(관측 0건)', report)
                self.assertIn('usage 미보고 또는 요약 없음' if removed == 2 else '집계 범위: 일부', report)

    def partial_session(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex'))
        validated = self.env.ok('synthesis', 'validate', '--session', sid)
        return sid, validated, None

    def test_legacy_session_layout_is_still_read(self):
        # 旧セッション: summary・道具記録・根拠抜粋・提案結果が一時領域にあり、call['summary'] は一時パス。
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        store, legacy = self.env.artifacts / sid / 'work', self.env.tmp / f'co-design-{sid}'
        shutil.move(str(store / 'evidence'), str(legacy / 'evidence'))
        for name in ('result.json', 'links.json', 'summary.json'):
            shutil.move(str(store / 'calls/1-proposal' / name), str(legacy / 'logs/calls/1-proposal' / name))
        session = self.env.session(sid)
        session['calls'][0]['summary'] = str(legacy / 'logs/calls/1-proposal/summary.json')
        (self.env.artifacts / sid / 'artifacts/session.json').write_text(json.dumps(session))
        self.assertEqual([t['item_id'] for t in co.load_tools(sid)], ['t1'])
        proposal = self.env.file('p.json', self.env.claude_proposal(sid))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        revealed = self.env.ok('reveal', '--session', sid)
        self.assertEqual(revealed['call_summaries'], [str(legacy / 'logs/calls/1-proposal/summary.json')])
        self.assertEqual(self.env.ok('status', '--session', sid)['state'], 'REVEALED')
        collected = co.collect_calls(self.env.session(sid))
        self.assertEqual((collected['summaries_missing'], collected['usage_status']), (0, 'complete'))
        # 新しく登録した根拠は恒久領域に入る。旧セッションの一時領域の抜粋も読める。
        self.assertEqual(co.claude_evidence_text(sid, 'E1'), 'line2\nline3\n')
        (legacy / 'evidence/claude').mkdir(exist_ok=True)
        (legacy / 'evidence/claude/E9.txt').write_text('legacy capture\n')
        self.assertEqual(co.claude_evidence_text(sid, 'E9'), 'legacy capture\n')

    def test_prune_never_touches_permanent_store(self):
        old_sid = '20260901T000000Z-aaaaaa'
        keep = self.env.artifacts / old_sid / 'work/calls/1-proposal'
        keep.mkdir(parents=True)
        (keep / 'summary.json').write_text('{}')
        logs = self.env.tmp / f'co-design-{old_sid}/logs'
        logs.mkdir(parents=True)
        with patch.object(co, 'now', lambda: datetime_ts('2026-10-12T00:00:00')):
            output = self.env.ok('prune', '--apply')
        self.assertEqual([Path(t['path']).name for t in output['targets']], ['logs'])
        self.assertFalse(logs.exists())
        self.assertTrue((keep / 'summary.json').is_file())


class ReportTests(Base):
    """D4: synthesis.md の補完と report.md。"""

    def test_initial_table_preserves_caveats_and_missing_side(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        proposal = self.env.claude_proposal(sid)
        proposal.update(summary='Option A | B\nwith caveat', status='INCOMPLETE')
        proposal['assumptions'] = ['only if ready']
        original = copy.deepcopy(proposal)
        text = '\n'.join(co._initial_proposal_table(proposal, None))
        self.assertIn('Option A \\| B with caveat', text)
        self.assertIn('미완료', text)
        self.assertIn('only if ready', text)
        self.assertIn('runtime', text)
        self.assertIn('(산출물 없음/유효하지 않음)', text)
        self.assertEqual(proposal, original)

    def test_render_synthesis_details(self):
        document = fx.synthesis(conditions=['only if Z'])
        document['decisions'][0].update(reason='Fits | R1', residual_risk='rollback cost')
        text = co.render_synthesis(document)
        for expected in ('# 통합안', '| 쟁점 | 결정 | 채택 출처 | Codex 확인 |', '| I1 | Use B1 | Claude |',
                         '- 채택 출처: Claude', '- 이유: Fits | R1', '- 기각한 대안: A1', '- 조건: only if Z',
                         '- 잔여 위험: rollback cost', '- Codex 확인: Claude 통합 판단(Codex 미확인)', '## 필수', '## 미해결'):
            self.assertIn(expected, text)
        # 列挙値は韓国語で表示し、引用本文は訳さない。
        for adopted, label in (('both', '양측'), ('neither', '둘 다 기각'), ('evidence', '근거'), ('codex', 'Codex')):
            self.assertIn(f'- 채택 출처: {label}', co.render_synthesis(fx.synthesis(adopt_i1=adopted)))
        debate = co.render_debate(fx.final_ledger())
        for expected in ('# 토론 원장', '## I1 [높음/설계 트레이드오프] Issue I1', '- 제기: Claude / 상태: 조건부 / 판정 방법: 판정 불가',
                         '- Codex (반대): Codex view', '  - P3 Claude 답변: answered'):
            self.assertIn(expected, debate)
        self.assertEqual((co.ko('stance', 'accept'), co.ko('verdict', 'distorted'), co.ko('method', 'source-text'),
                          co.ko('method', 'reproduction'), co.ko('method', 'user-decision')),
                         ('수용', '왜곡', '원문', '재현', '사용자 결정'))

    def test_report_structure_and_content(self):
        sid, _, final = fx.run_plan_a(self.env)
        report = Path(final['report'])
        self.assertEqual(oct(report.stat().st_mode & 0o777), oct(0o400))
        self.assertIn(str(report), final['rendered'])
        text = report.read_text()
        initial = text.split('### 최초 제안 비교(토론 전 동결본)', 1)[1].split('## 2.', 1)[0]
        self.assertIn('| 핵심 제안 | Claude design. | Codex design. |', initial)
        self.assertIn('| 요구사항별 접근 | R1: B1 | R1: A1 |', initial)
        # Final synthesis/ledger positions must not replace the frozen proposals.
        self.assertNotIn('Use B1', initial)
        self.assertNotIn('Claude view', initial)
        self.assertNotIn('Codex view', initial)
        headings = [line for line in text.splitlines() if line.startswith('## ')]
        self.assertEqual([h.split('.')[0] for h in headings], [f'## {n}' for n in range(1, 11)])
        self.assertEqual(headings, ['## 1. 질문·요구사항', '## 2. Claude 제안', '## 3. Codex 제안',
                                    '## 4. Claude→Codex 문제 제기와 응답', '## 5. Codex→Claude 문제 제기와 응답',
                                    '## 6. 쟁점별 판정', '## 7. 토론 결과(결정·채택 출처·이유·조건·잔여 위험)',
                                    '## 8. 충실성 검사', '## 9. 사용자 결정·미해결', '## 10. 비용'])
        for expected in ('# 토론 보고서: q-test', '최종 상태: 완료', '> Question: how should X work?', '- 요약: Claude design.',
                         '- 요약: Codex design.', '- 상태: 완료 / 근거 충분성: 충분', '  - [중간] r → 완화: m',
                         '### I1 [높음/설계 트레이드오프] Issue I1', 'Codex 출력 원문(토론, 반대): because',
                         '- Codex 지적(토론): Claude misses X', '- Codex 대안: Do Y', '- Claude 응답 (반대): Claude view',
                         '  - 답변: answered',
                         '| 쟁점 | 유형/중요도 | 판정 방법 | 검증 결과 | 근거 | 상태 |',
                         '| I1 | 설계 트레이드오프/높음 | 판정 불가 | 미검증 | - | 조건부 |',
                         '| C1 | 설계 트레이드오프/높음 | 판정 불가 | 미검증 | - | 사용자 결정 대기 |',
                         '- 채택 출처: Claude', '- 기각한 대안: A1', '- Codex 확인: Codex 확인됨(충실성 검사: 충실)',
                         '- I1: 충실 — ok', '- Codex 호출: 3회 / 상한 3회', '  - 3. 충실성 검사: 정상 / 완료 /',
                         '요약 있음', '집계 범위: 전체): 입력 150, 캐시 입력 30, 출력 15', '요약 누락: 0건 / 위반: 0',
                         '상한 도달: 아니오', 'Claude 작업 포함, 제한 없음'):
            self.assertIn(expected, text)
        session = self.env.session(sid)
        summary = json.loads(Path(final['session_summary']).read_text())
        self.assertIn('report.md', summary['rendered'])
        # 決定的: 同じ入力から同じ文書になる。
        ledger = json.loads((self.env.artifacts / sid / 'artifacts/ledger.json').read_text())
        synthesis = json.loads((self.env.artifacts / sid / 'artifacts/synthesis.json').read_text())
        checked = co.checked_issues(session, ledger, synthesis)
        self.assertEqual(co.render_report(session, summary, checked), text)

    def test_report_for_stopped_session(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "emit({'type': 'error', 'message': 'x'})"})
        self.env.run('call', '--session', sid, '--phase', 'proposal')
        final = self.env.ok('finalize', '--session', sid)
        text = Path(final['report']).read_text()
        self.assertIn('- 정지: POLICY', text)
        self.assertIn('(산출물 없음)', text)
        self.assertIn('## 10. 비용', text)

    def test_docs_point_to_report(self):
        skill = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        debate = (SKILL / 'references/debate.md').read_text(encoding='utf-8')
        self.assertIn('report.md', skill)
        p5 = next(line for line in debate.splitlines() if line.startswith('| P5'))
        self.assertIn('report.md', p5)


class LinkStrengthTests(Base):
    """D5: 経路の言及と抜粋の区別。"""
    TOOLS = [
        {'status': 'completed', 'item_id': 'sed', 'kind': 'command_execution', 'text': "/bin/zsh -lc 'sed -n 1,3p src/app.py'", 'output': 'a\nb\nc\n'},
        {'status': 'completed', 'item_id': 'cat', 'kind': 'command_execution', 'text': "/bin/zsh -lc 'cat src/b.py'", 'output': 'x\n' * 10},
        {'status': 'completed', 'item_id': 'nl', 'kind': 'command_execution', 'text': "/bin/zsh -lc \"nl -ba src/c.py | sed -n '5,9p'\"",
         'output': ''.join(f'{n}\tx\n' for n in range(5, 10))},
        {'status': 'completed', 'item_id': 'rg', 'kind': 'command_execution', 'text': "/bin/zsh -lc 'rg -n -C1 needle src'",
         'output': 'src/d.py-6-before\nsrc/d.py:7:needle\nsrc/d.py-8-after\n'},
        {'status': 'completed', 'item_id': 'grep', 'kind': 'command_execution', 'text': "/bin/zsh -lc 'grep -n x src/e.py'", 'output': '4:x\n'},
        {'status': 'completed', 'item_id': 'files', 'kind': 'command_execution', 'text': "/bin/zsh -lc 'rg --files src'", 'output': 'src/f.py\n'},
        {'status': 'completed', 'item_id': 'head', 'kind': 'command_execution', 'text': "/bin/zsh -lc 'head -n 3 src/h.py'", 'output': '1\n2\n3\n'},
        {'status': 'completed', 'item_id': 'rgplain', 'kind': 'command_execution', 'text': "/bin/zsh -lc 'rg needle src/i.py'", 'output': 'needle\n'},
        {'status': 'completed', 'item_id': 'mcp', 'kind': 'mcp_tool_call', 'server': 'context7', 'text': 'context7/query {"q": "src/j.py"}', 'output': ''},
        {'status': 'completed', 'item_id': 'web', 'kind': 'web_search', 'text': 'search', 'output': 'https://example.com/doc'},
    ]
    CASES = {'src/app.py:L1-L3': 'excerpt', 'src/app.py:L2': 'excerpt', 'src/app.py:L2-L5': 'mention',
             'src/b.py:L9-L10': 'excerpt', 'src/b.py:L11': 'mention',
             'src/c.py:L6-L8': 'excerpt', 'src/c.py:L4': 'mention',
             'src/d.py:L6-L8': 'excerpt', 'src/d.py:L9': 'mention',
             'src/e.py:L4': 'excerpt', 'src/e.py:L5': 'mention', 'src/f.py:L1': 'mention',
             'src/h.py:L2-L3': 'excerpt', 'src/h.py:L4': 'mention', 'src/i.py:L1': 'mention', 'src/j.py:L1': 'mention',
             'https://example.com/doc': 'mention'}

    def test_excerpt_and_mention_cases(self):
        for ref, strength in self.CASES.items():
            with self.subTest(ref=ref):
                self.assertEqual(co.link_ref_detail(ref, self.TOOLS, {}, set())[0::2], ('linked', strength))
                self.assertEqual(co.link_ref(ref, self.TOOLS, {}, set())[0], 'linked')
        self.assertEqual(co.link_ref_detail('src/missing.py:L1', self.TOOLS, {}, set()), ('unbacked', [], None))
        self.assertEqual(co.link_ref_detail('brief:L1', [], {'brief': 2}), ('input', [], None))

    def test_review_reproductions_are_mention(self):
        # m1: 別パイプラインの範囲・後段の絞り込み・空出力・未完了の道具は excerpt にしない。
        def tool(command, output, status='completed'):
            return [{'item_id': 't', 'kind': 'command_execution', 'status': status, 'text': f"/bin/zsh -lc {command!r}",
                     'output': output}]
        lines = ''.join(f'{n}\n' for n in range(1, 41))
        cases = [("sed -n '1,20p' src/a.py && sed -n '100,140p' src/b.py", lines, 'src/a.py:L110-L120', 'mention'),
                 ("sed -n '1,20p' src/a.py && sed -n '100,140p' src/b.py", lines, 'src/b.py:L110-L120', 'excerpt'),
                 ("sed -n '1,20p' src/a.py && sed -n '100,140p' src/b.py", lines, 'src/a.py:L5-L9', 'excerpt'),
                 ("sed -n '1,200p' src/a.py | head -5", '1\n2\n3\n4\n5\n', 'src/a.py:L150-L160', 'mention'),
                 ("sed -n '1,200p' src/a.py | head -5", '1\n2\n3\n4\n5\n', 'src/a.py:L2-L3', 'mention'),
                 ("cat src/a.py | grep -n x", '3:x\n', 'src/a.py:L1', 'mention'),
                 ("sed -n '1,20p' src/a.py", '', 'src/a.py:L2-L3', 'mention'),
                 ("sed -n '1,20p' src/a.py | nl -ba", lines[:40], 'src/a.py:L2-L3', 'excerpt'),
                 ("sed -n '10,12p' src/a.py", '10\n', 'src/a.py:L10-L12', 'mention'),
                 ("cat src/a.py 2>/dev/null", lines, 'src/a.py:L30-L40', 'excerpt')]
        for command, output, ref, strength in cases:
            with self.subTest(command=command, ref=ref):
                self.assertEqual(co.link_ref_detail(ref, tool(command, output), {}, set())[2], strength)
        self.assertEqual(co.link_ref_detail('src/a.py:L2', tool("sed -n '1,20p' src/a.py", lines, 'failed'), {}, set())[2],
                         'mention')
        self.assertEqual(co.link_ref_detail('src/a.py:L2', tool("sed -n '1,20p' src/a.py", lines, 'in_progress'), {}, set())[2],
                         'mention')

    def test_counts_split_linked_and_claude_evidence(self):
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.behaviors({'debate': "result['critique_of_claude'][0]['evidence_refs'] = ['src/app.py:L1-L3', 'src/app.py:L4', "
                                      "'E1', 'E1: src/app.py:L3', 'E1: src/app.py:L1']"})
        output = self.env.ok('call', '--session', sid, '--phase', 'debate')
        counts = output['links']
        # 応答側の 'X1' も excerpt（X1 の ref を proposal 呼び出しの sed 1,3 が覆う）。
        self.assertEqual((counts['linked'], counts['excerpt'], counts['mention']), (6, 5, 1))
        self.assertEqual(counts['linked'], counts['excerpt'] + counts['mention'])
        links = json.loads((self.env.artifacts / sid / 'artifacts/codex/debate_links.json').read_text())['links']
        self.assertEqual([l['strength'] for l in links if l['id'] == 'C1'], ['excerpt', 'mention', 'excerpt', 'excerpt', 'excerpt'])
        # 道具は経路を挙げただけでも、登録済み Claude 根拠（E1 = L2-L3 のバイト複製）の範囲内なら excerpt。
        ctx = co.link_context(self.env.session(sid), 'debate', None)
        listing = [{'item_id': 'r', 'kind': 'command_execution', 'status': 'completed', 'text': "rg --files src",
                    'output': 'src/app.py\n'}]
        for ref, strength in (('E1: src/app.py:L3', 'excerpt'), ('E1: src/app.py:L2-L3', 'excerpt'),
                              ('E1: src/app.py:L1', 'mention'), ('src/app.py:L3', 'mention'), ('E1', 'excerpt')):
            with self.subTest(ref=ref):
                result = co.link_citation(ref, listing, ctx)
                self.assertEqual((result['link'], result['strength']), ('linked', strength))


class ScaffoldTests(DebatedMixin, Base):
    """L1: ledger・synthesis の初稿。"""
    CLAIM = 'Claude 案は「境界」を見落とす — "quoted" \\ back'
    ARGUMENT = 'Codex の反論: 根拠 X1 を参照。改行\nあり'

    def test_pre_debate_draft_rejected_until_filled_and_never_overwritten(self):
        sid = self.env.to_revealed()
        output = self.env.ok('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate')
        draft_path = Path(output['draft'])
        self.assertEqual(draft_path, self.env.artifacts / sid / 'artifacts/ledger.pre-debate.draft.json')
        draft = json.loads(draft_path.read_text())
        co.validate_doc(draft, 'ledger')
        self.env.write_artifact(sid, 'ledger.json', draft)
        self.assertEqual(self.env.run('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')[0]['status'], 'INVALID')
        draft['issues'][0].update(title='Missing retry', claude={'position': 'Add retry', 'stance': 'proposed', 'evidence_ids': []})
        self.env.write_artifact(sid, 'ledger.json', draft)
        self.assertIn('critique text is empty', self.env.run('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')[0]['error'])
        draft['issues'][0]['turns'][0]['text'] = 'Codex plan lacks retry'
        self.env.write_artifact(sid, 'ledger.json', draft)
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        before = draft_path.read_bytes()
        refused = self.env.run('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate')[0]
        self.assertEqual(refused['status'], 'REFUSED')
        self.assertIn('never overwrite', refused['error'])
        self.assertEqual(draft_path.read_bytes(), before)

    def test_final_draft_copies_codex_text_verbatim(self):
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1'), fx.issue('I2', importance='low')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.behaviors({'debate': f"result['critique_of_claude'][0].update(claim={self.CLAIM!r}, alternative='代替案 Y', "
                                      "would_change_if='Z が示されたら', evidence_refs=['X1: src/app.py:L1-L3', 'brief:L1', 'src/app.py:L1'])\n"
                                      f"result['responses_to_claude_critique'][0].update(argument={self.ARGUMENT!r}, stance='conditional')"})
        self.env.ok('call', '--session', sid, '--phase', 'debate')
        output = self.env.ok('ledger', 'scaffold', '--session', sid, '--stage', 'final')
        raw = Path(output['draft']).read_bytes()
        draft = json.loads(raw)
        by_id = {i['id']: i for i in draft['issues']}
        self.assertEqual(sorted(by_id), ['C1', 'I1', 'I2'])
        self.assertEqual(output['claude_pending'], ['I1', 'I2', 'C1'])
        c1, i1 = by_id['C1'], by_id['I1']
        self.assertEqual((c1['origin'], c1['title'], c1['codex']['position'], c1['codex']['stance']),
                         ('codex', self.CLAIM, '代替案 Y', 'proposed'))
        self.assertEqual(c1['codex']['evidence_ids'], ['X1', 'brief:L1'])
        critique = [t for t in c1['turns'] if t['speaker'] == 'codex'][0]
        self.assertEqual((critique['kind'], critique['text'], critique['would_change_if']), ('critique', self.CLAIM, 'Z が示されたら'))
        answer = [t for t in c1['turns'] if t['speaker'] == 'claude'][0]
        self.assertEqual((answer['kind'], answer['text']), ('answer', ''))
        self.assertEqual((i1['codex']['stance'], i1['codex']['position']), ('conditional', self.ARGUMENT))
        self.assertEqual([t['text'] for t in i1['turns'] if t['speaker'] == 'codex'], [self.ARGUMENT])
        self.assertTrue(all(i['claude']['stance'] == 'pending' for i in draft['issues']))
        # バイト単位: JSON 符号化した Codex 原文がそのまま初稿に含まれる（ensure_ascii なし）。
        for text in (self.CLAIM, self.ARGUMENT, '代替案 Y'):
            self.assertIn(json.dumps(text, ensure_ascii=False).encode('utf-8'), raw)
        self.env.write_artifact(sid, 'ledger.json', draft)
        refused = self.env.run('ledger', 'validate', '--session', sid, '--stage', 'final')[0]
        self.assertEqual(refused['status'], 'INVALID')
        fx.fill_final_ledger(draft)
        self.env.write_artifact(sid, 'ledger.json', draft)
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'final')

    def test_synthesis_draft_rejected_until_filled(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        output = self.env.ok('synthesis', 'scaffold', '--session', sid)
        draft = json.loads(Path(output['draft']).read_text())
        self.assertEqual([d['issue_id'] for d in draft['decisions']], ['I1', 'C1'])
        self.assertTrue(all(d['codex_checked'] is False for d in draft['decisions']))
        self.assertEqual(set(draft['metrics']), {'one_side_confirmed_findings', 'missing_required', 'unbacked_claims'})
        self.env.write_artifact(sid, 'synthesis.json', draft)
        self.assertEqual(self.env.run('synthesis', 'validate', '--session', sid)[0]['status'], 'INVALID')
        for decision in draft['decisions']:
            decision['adopted_from'] = 'codex'
        self.env.write_artifact(sid, 'synthesis.json', draft)
        self.assertIn('decision and reason must be filled', self.env.run('synthesis', 'validate', '--session', sid)[0]['error'])
        fx.fill_synthesis(draft)
        self.env.write_artifact(sid, 'synthesis.json', draft)
        self.env.ok('synthesis', 'validate', '--session', sid)
        self.assertEqual(self.env.run('synthesis', 'scaffold', '--session', sid)[0]['status'], 'REFUSED')

    def test_draft_output_paths_are_guarded(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        existing = self.env.root / 'existing.json'
        existing.write_text('{"keep": true}')
        linked_dir = self.env.root / 'linked-dir'
        linked_dir.symlink_to(self.env.root / 'claude', target_is_directory=True)
        protected = self.env.root / '.aws'
        protected.mkdir()
        for out in (existing, linked_dir / 'd.json', protected / 'd.json', self.env.root / 'settings.json',
                    self.env.root / 'missing-dir/d.json', self.env.root / 'draft.txt'):
            with self.subTest(out=out.name):
                self.assertEqual(self.env.run('synthesis', 'scaffold', '--session', sid, '--out', out)[0]['status'], 'REFUSED')
        self.assertEqual(existing.read_text(), '{"keep": true}')
        custom = self.env.root / 'custom.json'
        self.assertEqual(self.env.ok('synthesis', 'scaffold', '--session', sid, '--out', custom)['draft'], str(custom))

    def test_scaffold_state_rules(self):
        sid = self.env.to_inputs_frozen()
        for argv in (['ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate'],
                     ['ledger', 'scaffold', '--session', sid, '--stage', 'final'], ['synthesis', 'scaffold', '--session', sid]):
            with self.subTest(argv=argv[:3]):
                self.assertEqual(self.env.run(*argv)[0]['status'], 'REFUSED')



class LosslessReuseTests(Base):
    def ready(self):
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        return sid, self.env.session(sid)

    def test_new_default_preserves_content_and_legacy_behavior(self):
        sid, session = self.ready()
        self.assertEqual(session['request_format'], co.LOSSLESS_FORMAT)
        plain = co.build_request(session, 'debate', compact=False)
        packed = co.build_request(session, 'debate')
        self.assertTrue(co.lossless_check(session, 'debate', plain, packed)['ok'])
        self.assertLess(len(packed), len(plain))
        legacy = dict(session)
        legacy.pop('request_format')
        self.assertEqual(co.build_request(legacy, 'debate'), co.build_request(legacy, 'debate', compact=False))
        broken = packed.replace('Codex view', 'different claim')
        self.assertFalse(co.lossless_check(session, 'debate', plain, broken)['ok'])

    def test_reuse_requires_observed_text_and_current_hash(self):
        sid, session = self.ready()
        rows = {e['id']: e for e in co.reuse_evidence(session)}
        self.assertTrue(rows['X1']['reuse'], rows)
        self.assertIn('text', rows['X1'])
        self.assertFalse(rows['X2']['reuse'])
        self.assertTrue(rows['E1']['reuse'])
        self.env.project.joinpath('src/app.py').write_text('changed\n')
        changed = {e['id']: e for e in co.reuse_evidence(session)}
        self.assertFalse(changed['X1']['reuse'])
        self.assertFalse(changed['E1']['reuse'])
        self.assertIn('file changed', changed['X1']['reason'])
        self.assertIn('"reuse":false', co.build_request(session, 'debate'))

    def test_missing_observation_and_ambiguous_command_do_not_reuse(self):
        sid, session = self.ready()
        with patch.object(co, 'load_tools', return_value=[]):
            self.assertFalse(next(e for e in co.reuse_evidence(session) if e['id'] == 'X1')['reuse'])
        tool = {'kind': 'command_execution', 'status': 'completed',
                'text': 'cat src/app.py; cat src/other.py', 'output': 'a\nb\nc\n'}
        self.assertIsNone(co.observed_excerpt(tool, 'src/app.py', 1, 3))

    def test_no_reuse_before_both_proposals_are_frozen(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(co.reuse_evidence(self.env.session(sid)), [])

    def test_default_integrity_rejects_synthesis_loss_and_injected_filter(self):
        sid, _ = CompactTests.session_with_unrelated_issue(self)
        session = self.env.session(sid)
        plain = co.build_request(session, 'fidelity', compact=False)
        packed = co._build_full_request(session, 'fidelity')
        for broken in (packed.replace('Keep', 'Changed'),
                       packed.replace('BEGIN SYNTHESIS', 'BEGIN MISSING SYNTHESIS'),
                       packed + co.block('EXCERPT FILTER', '{}'),
                       packed + co.block('unexpected', 'data')):
            self.assertFalse(co.lossless_check(session, 'fidelity', plain, broken, allow_selective=False)['ok'])
        self.assertTrue(co.lossless_check(session, 'fidelity', plain, packed, allow_selective=False)['ok'])

    def test_conditional_default_keeps_all_issues_and_conditions(self):
        sid, _ = CompactTests.session_with_unrelated_issue(self)
        session = self.env.session(sid)
        text = co.build_request(session, 'fidelity')
        blocks = co.request_blocks(text)
        self.assertNotIn('EXCERPT FILTER', blocks)
        self.assertIn('I2', blocks['LEDGER'])
        self.assertIn('Keep', blocks['SYNTHESIS (cite as synthesis:L<n>)'])
        baseline = co.build_request(session, 'fidelity', compact=False)
        self.assertTrue(co.fidelity_serialization_check(session, baseline, text)['ok'])


class FidelitySerializationTests(Base):
    def ready(self):
        sid, _ = CompactTests.session_with_unrelated_issue(self)
        document = co.read_json_artifact(sid, 'synthesis.json')
        document['decisions'][0]['conditions'] = ['spaces stay  verbatim\n"quote" \\ slash']
        document['decisions'][0]['residual_risk'] = 'risk with  two spaces'
        path = co.art(sid) / 'synthesis.json'
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
        self.env.ok('synthesis', 'validate', '--session', sid)
        return sid, self.env.session(sid)

    def test_serialization_preserves_every_value_and_original_line_map(self):
        sid, session = self.ready()
        baseline = co.build_request(session, 'fidelity', compact=False)
        packet = co.build_request(session, 'fidelity')
        report = co.fidelity_serialization_check(session, baseline, packet)
        self.assertTrue(report['ok'], report)
        self.assertTrue(report['applied'])
        self.assertEqual(report['coverage'], ['I1'])
        self.assertLess(len(packet.encode()), len(co._build_full_request(session, 'fidelity').encode()))
        blocks = co.request_blocks(packet)
        self.assertNotIn('EXCERPT FILTER', blocks)
        self.assertIn('I2', blocks['LEDGER'])
        synthesis = '\n'.join(line.partition(': ')[2] for line in blocks['SYNTHESIS (cite as synthesis:L<n>)'].splitlines())
        self.assertEqual(json.loads(synthesis), co.read_json_artifact(sid, 'synthesis.json'))
        self.assertFalse(co.lossless_check(session, 'fidelity', baseline, packet, allow_selective=False)['ok'])
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        call = co.phase_call(self.env.session(sid), 'fidelity')
        self.assertEqual(call['checked_basis']['issue_ids'], ['I1'])
        self.assertTrue(call['fidelity_serialization']['synthesis_json_equal'])

    def test_gate_rejects_changed_strings_omissions_and_line_numbers(self):
        sid, session = self.ready()
        packet = co.build_request(session, 'fidelity')
        baseline = co.build_request(session, 'fidelity', compact=False)
        for before, after in [('spaces stay  verbatim', 'spaces stay verbatim'), ('risk with  two spaces', 'lost risk'),
                              ('"decision":"Keep"', '"decision":"Changed"'),
                              ('"evidence_refs":["X1"]', '"evidence_refs":[]'), ('1: {', '9: {')]:
            with self.subTest(before=before):
                self.assertIn(before, packet)
                self.assertFalse(co.fidelity_serialization_check(session, baseline, packet.replace(before, after))['ok'])
        broken = packet.replace('BEGIN SYNTHESIS', 'BEGIN MISSING SYNTHESIS')
        self.assertFalse(co.fidelity_serialization_check(session, baseline, broken)['ok'])
        self.assertFalse(co.fidelity_serialization_check(session, baseline, packet + co.block('unexpected', 'x'))['ok'])

    def test_literal_unicode_separators_and_crlf_preserve_values_and_citations(self):
        sid, session = self.ready()
        document = co.read_json_artifact(sid, 'synthesis.json')
        value = 'one\u2028two\u2029three\u0085four'
        document['decisions'][0]['conditions'] = [value]
        document['decisions'][0]['residual_risk'] = value
        source = json.dumps(document, ensure_ascii=False, indent=2).replace('\n', '\r\n') + '\r\n'
        (co.art(sid) / 'synthesis.json').write_bytes(source.encode())
        self.env.ok('synthesis', 'validate', '--session', sid)
        session = self.env.session(sid)
        packet = co.build_request(session, 'fidelity')
        baseline = co.build_request(session, 'fidelity', compact=False)
        report = co.fidelity_serialization_check(session, baseline, packet)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['original_line_count'], source.count('\n'))
        self.assertEqual(report['coverage'], ['I1'])
        self.assertIn(value, packet)
        block = co.request_blocks(packet)['SYNTHESIS (cite as synthesis:L<n>)']
        restored = '\n'.join(line.partition(': ')[2] for line in co.json_source_lines(block))
        self.assertEqual(json.loads(restored), document)
        for separator in ('\u2028', '\u2029', '\u0085'):
            self.assertFalse(co.fidelity_serialization_check(session, baseline, packet.replace(separator, ' ', 1))['ok'])
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        self.assertEqual(co.phase_call(self.env.session(sid), 'fidelity')['checked_basis']['issue_ids'], ['I1'])

    def test_old_sessions_compact_and_other_phases_keep_original_behavior(self):
        sid, session = self.ready()
        legacy = dict(session)
        legacy.pop('fidelity_format')
        self.assertEqual(co.build_request(legacy, 'fidelity'), co._build_full_request(legacy, 'fidelity'))
        for phase in ('proposal', 'debate', 'round2'):
            self.assertEqual(co.build_request(session, phase), co._build_full_request(session, phase))
        self.assertEqual(co.build_request(session, 'fidelity', compact=True),
                         co._build_full_request(session, 'fidelity', compact=True))

    def test_no_savings_uses_full_packet_and_stale_unlisted_issue_still_invalidates_checked(self):
        sid, session = self.ready()
        document = co.read_json_artifact(sid, 'synthesis.json')
        (co.art(sid) / 'synthesis.json').write_text(co.minify(document))
        session = self.env.session(sid)
        packet = co.build_request(session, 'fidelity')
        report = co.fidelity_serialization_check(session, co.build_request(session, 'fidelity', compact=False), packet)
        self.assertTrue(report['ok'])
        self.assertFalse(report['applied'])
        self.assertEqual(report['fallback_reason'], 'full request is no larger')
        self.env.ok('synthesis', 'validate', '--session', sid)
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        self.env.ok('synthesis', 'validate', '--session', sid, '--derive-checked')
        ledger = co.read_json_artifact(sid, 'ledger.json')
        ledger['issues'][-1]['title'] = 'low issue changed after checking'
        self.env.write_artifact(sid, 'ledger.json', ledger)
        self.env.ok('synthesis', 'validate', '--session', sid, '--derive-checked')
        self.assertFalse(any(d['codex_checked'] for d in co.read_json_artifact(sid, 'synthesis.json')['decisions']))


class FormatAssistanceTests(DebatedMixin, Base):
    fidelity_done = CheckedBindingTests.fidelity_done
    def test_derive_checked_from_faithful_missing_or_arbitrary_values(self):
        sid = self.fidelity_done()
        doc = fx.synthesis()
        del doc['decisions'][0]['codex_checked']
        doc['decisions'][1]['codex_checked'] = 'invented'
        self.env.write_artifact(sid, 'synthesis.json', doc)
        original = co.read_artifact(sid, 'synthesis.json')
        self.assertEqual(self.env.run('synthesis', 'validate', '--session', sid)[0]['status'], 'INVALID')
        output = self.env.ok('synthesis', 'validate', '--session', sid, '--derive-checked')
        saved = co.read_json_artifact(sid, 'synthesis.json')
        self.assertEqual([i['codex_checked'] for i in saved['decisions']], [True, False])
        self.assertEqual(output['derived_checked'], ['I1'])
        audit = json.loads(Path(output['format_revision']).read_text())
        self.assertEqual(audit['original_utf8'].encode(), original)
        self.assertEqual(len(audit['changes']), 2)
        # Full content binding still invalidates derived flags after any content edit.
        saved['decisions'][1]['reason'] = 'changed later'
        self.env.write_artifact(sid, 'synthesis.json', saved)
        self.env.ok('synthesis', 'validate', '--session', sid, '--derive-checked')
        self.assertFalse(any(i['codex_checked'] for i in co.read_json_artifact(sid, 'synthesis.json')['decisions']))

    def test_derive_failure_preserves_original_and_semantic_validation(self):
        sid = self.fidelity_done()
        for field, value in [('adopted_from', 'unknown'), ('decision', ''), ('issue_id', 'I999')]:
            doc = fx.synthesis()
            doc['decisions'][0][field] = value
            del doc['decisions'][0]['codex_checked']
            self.env.write_artifact(sid, 'synthesis.json', doc)
            original = co.read_artifact(sid, 'synthesis.json')
            output = self.env.run('synthesis', 'validate', '--session', sid, '--derive-checked')[0]
            self.assertEqual(output['status'], 'INVALID', output)
            self.assertEqual(co.read_artifact(sid, 'synthesis.json'), original)

    def test_round2_does_not_derive_checked(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex'))
        self.env.ok('synthesis', 'validate', '--session', sid, '--user-requested')
        self.env.ok('call', '--session', sid, '--phase', 'round2')
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex', checked=True))
        output = self.env.ok('synthesis', 'validate', '--session', sid, '--derive-checked')
        self.assertEqual(output['derived_checked'], [])
        self.assertFalse(any(d['codex_checked'] for d in co.read_json_artifact(sid, 'synthesis.json')['decisions']))

    def test_distorted_fidelity_does_not_derive_true(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis())
        self.env.ok('synthesis', 'validate', '--session', sid)
        self.env.behaviors({'fidelity': "result['issues'][0]['verdict'] = 'distorted'"})
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(checked=True))
        output = self.env.ok('synthesis', 'validate', '--session', sid, '--derive-checked')
        self.assertEqual(output['derived_checked'], [])
        self.assertFalse(co.read_json_artifact(sid, 'synthesis.json')['decisions'][0]['codex_checked'])

    def test_ref_normalization_is_opt_in_exact_and_audited(self):
        sid = self.debated()
        doc = fx.final_ledger()
        doc['issues'][0]['claude']['evidence_ids'] = ['brief.md:2']
        doc['issues'][0]['requirement_ref'] = 'brief:2'
        doc['issues'][0]['title'] = 'free prose brief.md:2'
        self.env.write_artifact(sid, 'ledger.json', doc)
        original = co.read_artifact(sid, 'ledger.json')
        self.assertEqual(self.env.run('ledger', 'validate', '--session', sid, '--stage', 'final')[0]['status'], 'INVALID')
        output = self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'final', '--normalize-refs')
        saved = co.read_json_artifact(sid, 'ledger.json')
        self.assertEqual(saved['issues'][0]['claude']['evidence_ids'], ['brief:L2'])
        self.assertEqual(saved['issues'][0]['title'], doc['issues'][0]['title'])
        self.assertEqual(json.loads(Path(output['format_revision']).read_text())['original_utf8'].encode(), original)
        for invalid in ('E999', 'brief.md:999'):
            doc['issues'][0]['claude']['evidence_ids'] = [invalid]
            self.env.write_artifact(sid, 'ledger.json', doc)
            original = co.read_artifact(sid, 'ledger.json')
            self.assertEqual(self.env.run('ledger', 'validate', '--session', sid, '--stage', 'final', '--normalize-refs')[0]['status'], 'INVALID')
            self.assertEqual(co.read_artifact(sid, 'ledger.json'), original)

    def test_already_frozen_refusal_does_not_write_normalized_ledger_or_audit(self):
        sid = self.env.to_revealed()
        document = fx.ledger([fx.issue('I1')])
        self.env.write_artifact(sid, 'ledger.json', document)
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        document['issues'][0]['claude']['evidence_ids'] = ['brief.md:2']
        self.env.write_artifact(sid, 'ledger.json', document)
        original = co.read_artifact(sid, 'ledger.json')
        before = {str(p): p.read_bytes() for p in co.art(sid).rglob('*') if p.is_file()}
        output = self.env.run('ledger', 'validate', '--session', sid, '--stage', 'pre-debate', '--normalize-refs')[0]
        self.assertEqual(output['status'], 'REFUSED')
        self.assertEqual(co.read_artifact(sid, 'ledger.json'), original)
        self.assertEqual(before, {str(p): p.read_bytes() for p in co.art(sid).rglob('*') if p.is_file()})

    def test_existing_freeze_target_refuses_before_normalization_audit(self):
        for kind in ('regular', 'dangling-symlink'):
            with self.subTest(target=kind):
                sid = self.env.to_inputs_frozen()
                self.env.ok('call', '--session', sid, '--phase', 'proposal')
                proposal = self.env.claude_proposal(sid)
                proposal['evidence'].append({'id': 'E2', 'kind': 'brief', 'ref': 'brief.md:2', 'observation': 'input'})
                source = self.env.file('new-proposal-' + kind + '.json', proposal)
                original = source.read_bytes()
                target = co.art(sid) / 'claude/proposal.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == 'regular':
                    co.write_frozen(target, b'pre-existing target\n')
                else:
                    target.symlink_to('absent-target.json')
                def snapshot():
                    return {str(p): ('symlink', os.readlink(p)) if p.is_symlink() else ('file', p.read_bytes())
                            for p in co.art(sid).rglob('*') if p.is_symlink() or p.is_file()}
                before = snapshot()
                state = self.env.session(sid)
                output = self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', source,
                                      '--normalize-refs')[0]
                self.assertEqual(output['status'], 'REFUSED', output)
                self.assertEqual(output['error'], 'freeze target already exists')
                self.assertEqual(snapshot(), before)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(self.env.session(sid), state)

    def test_freeze_normalization_preserves_source_birth_checks_and_audit(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        proposal = self.env.claude_proposal(sid)
        proposal['evidence'].append({'id': 'E2', 'kind': 'brief', 'ref': 'brief.md:2', 'observation': 'input'})
        path = self.env.file('new-proposal.json', proposal)
        original = path.read_bytes()
        self.assertEqual(self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', path)[0]['status'], 'REFUSED')
        output = self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', path, '--normalize-refs')
        self.assertEqual(path.read_bytes(), original)
        frozen = co.read_json_artifact(sid, 'claude/proposal.json')
        self.assertIn('brief:L2', [e['ref'] for e in frozen['evidence']])
        self.assertEqual(json.loads(Path(output['format_revision']).read_text())['original_utf8'].encode(), original)

    def test_scaffold_hints_offer_ids_lines_enums_without_filling_semantics(self):
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        output = self.env.ok('synthesis', 'scaffold', '--session', sid)
        self.assertIn('X1', output['hints']['known_evidence_ids'])
        self.assertEqual(output['hints']['known_issue_ids'], ['I1', 'C1'])
        self.assertIn('brief:L1', output['hints']['reference_examples'])
        self.assertIn('adopted_from', ' '.join(output['hints']['enum_choices']))
        doc = json.loads(Path(output['draft']).read_text())
        self.assertEqual(doc['decisions'][0]['adopted_from'], '')
        self.assertEqual(doc['metrics']['missing_required'], -1)

    def test_scaffold_hints_show_item_shapes_for_empty_object_arrays(self):
        # 空配列で渡す user_decisions・unresolved の要素形を示し、文字列で埋めて INVALID になる繰り返しを防ぐ。
        sid = self.debated()
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        output = self.env.ok('synthesis', 'scaffold', '--session', sid)
        shapes = output['hints']['item_shapes']
        self.assertEqual(shapes['user_decisions[]']['required'], ['issue_id', 'question', 'options'])
        self.assertEqual(shapes['user_decisions[]']['fields']['options'], ['string'])
        self.assertEqual(shapes['unresolved[]']['fields']['blocking'], 'boolean')
        # 文字列配列（required 等）と既に要素のある decisions は示さない。
        self.assertNotIn('required[]', shapes)
        self.assertNotIn('decisions[]', shapes)
        # 示した形どおりに埋めれば形式検査を通る。
        doc = json.loads(Path(output['draft']).read_text())
        doc['user_decisions'] = [{'issue_id': 'I1', 'question': 'q', 'options': ['a', 'b']}]
        doc['unresolved'] = [{'issue_id': 'C1', 'impact': 'i', 'needed_evidence': 'e', 'blocking': False}]
        for decision in doc['decisions']:
            decision['adopted_from'] = 'claude'
        co.validate_doc(doc, 'synthesis')


class BatchReuseTests(Base):
    def setUp(self):
        super().setUp()
        self.env.project.joinpath('src/other.py').write_text('other1\n\nother3\n')
        self.hashes = {name: co.sha256((self.env.project / name).read_bytes())
                       for name in ('src/app.py', 'src/other.py')}

    def observed(self, script, output=None):
        if output is None:
            # 正常 reader だけを実行する。拒否ケースの文字列は実行しない。
            result = subprocess.run(['/bin/zsh', '-lc', script], cwd=self.env.project,
                                    capture_output=True, check=True)
            output = result.stdout.decode('utf-8')
        return {'kind': 'command_execution', 'status': 'completed',
                'text': '/bin/zsh -lc ' + co.shlex.quote(script), 'output': output}

    def excerpt(self, tool, path='src/app.py', first=1, last=3, hashes=None):
        return co.observed_excerpt(tool, path, first, last, self.hashes if hashes is None else hashes)

    def test_real_batched_readers_reconstruct_each_file(self):
        scripts = ('nl -ba src/app.py\nnl -ba src/other.py',
                   'cat src/app.py; cat src/other.py',
                   "sed -n '1,3p' src/app.py\nhead -n 3 src/other.py",
                   'head -n 3 src/app.py; nl -ba src/other.py')
        for script in scripts:
            with self.subTest(script=script):
                tool = self.observed(script)
                self.assertEqual(self.excerpt(tool), 'line1\nline2\nline3')
                self.assertEqual(self.excerpt(tool, 'src/other.py'), 'other1\n\nother3')

    def test_single_readers_and_unterminated_last_line(self):
        for script in ('cat src/app.py', 'nl -ba src/app.py', 'head -n 3 src/app.py',
                       "sed -n '2,4p' src/app.py"):
            with self.subTest(script=script):
                self.assertEqual(self.excerpt(self.observed(script), first=2), 'line2\nline3')
        self.env.project.joinpath('src/other.py').write_text('last\n\nno-final-newline')
        self.hashes['src/other.py'] = co.sha256(self.env.project.joinpath('src/other.py').read_bytes())
        tool = self.observed('nl -ba src/app.py\nnl -ba src/other.py')
        self.assertFalse(tool['output'].endswith('\n'))
        self.assertEqual(self.excerpt(tool, 'src/other.py'), 'last\n\nno-final-newline')
        self.assertIsNone(self.excerpt(dict(tool, output=tool['output'] + '\n')))

    def test_unsupported_shell_cannot_supply_reusable_evidence(self):
        output = self.observed('cat src/app.py')['output']
        scripts = ('cat src/app.py | cat', 'cat src/app.py > copied.txt',
                   'cat src/app.py 2>/dev/null', 'cat src/app.py < src/other.py',
                   'cat src/app.py && cat src/other.py', 'cat src/app.py || cat src/other.py',
                   'cat src/app.py & cat src/other.py', '(cat src/app.py)',
                   'echo fake; cat src/app.py', "printf 'header'; cat src/app.py",
                   'cd src; cat app.py', 'LC_ALL=C cat src/app.py',
                   'cat $FILE', 'cat `echo src/app.py`', 'cat src/*.py',
                   'cat src/app.py # ignored', '/tmp/cat src/app.py',
                   "cat ' src/app.py '", "cat 'src\\app.py'", "cat 'src/\tapp.py'",
                   '/tmp/zsh -lc "cat src/app.py"', 'nl -bn src/app.py',
                   'cat src/app.py src/other.py', 'cat src/app.py; printf fake')
        for script in scripts:
            with self.subTest(script=script):
                self.assertIsNone(self.excerpt(self.observed(script, output)))

    def test_entire_output_and_numbering_must_match(self):
        tool = self.observed('nl -ba src/app.py\nnl -ba src/other.py')
        for output in (tool['output'][:-1], tool['output'] + 'extra\n',
                       tool['output'].replace('     1\tother1', '     5\tother1'),
                       tool['output'].replace('     2\t\n', ''),
                       tool['output'].replace('other1', 'line1'),
                       tool['output'].replace('\n', '\r\n')):
            with self.subTest(output=output):
                self.assertIsNone(self.excerpt(dict(tool, output=output)))

    def test_quoted_internal_space_remains_supported(self):
        path = self.env.project / 'src/space file.py'
        path.write_text('spaced\nsecond\n')
        self.hashes['src/space file.py'] = co.sha256(path.read_bytes())
        tool = self.observed("nl -ba 'src/space file.py'; nl -ba src/app.py")
        self.assertEqual(self.excerpt(tool, 'src/space file.py', first=1, last=2), 'spaced\nsecond')

    def test_sibling_hash_missing_changed_or_source_unavailable_rejects_batch(self):
        tool = self.observed('cat src/app.py; cat src/other.py')
        self.assertIsNone(self.excerpt(tool, hashes={'src/app.py': self.hashes['src/app.py']}))
        self.env.project.joinpath('src/other.py').write_text('changed\n')
        self.assertIsNone(self.excerpt(tool))
        with patch.object(co, 'read_source', side_effect=co.Refused('source file not found')):
            self.assertIsNone(self.excerpt(tool))
        with patch.object(co, 'read_source', side_effect=co.Refused('protected source')):
            self.assertIsNone(self.excerpt(tool))

    def test_symlink_out_of_root_and_missing_file_are_not_read(self):
        self.env.project.joinpath('src/alias.py').symlink_to(self.env.project / 'src/other.py')
        hashes = dict(self.hashes, **{'src/alias.py': self.hashes['src/other.py']})
        tool = self.observed('cat src/app.py; cat src/alias.py')
        self.assertIsNone(self.excerpt(tool, hashes=hashes))
        for script in ('cat ../outside.py', 'cat /tmp/outside.py', 'cat src/missing.py'):
            self.assertIsNone(self.excerpt(self.observed(script, '')))

    def test_page_controls_cr_limits_and_invalid_ranges_fall_back(self):
        for source in ('a\r\nb\n', 'a\n\\:\\:\\:\nsecond page\n'):
            self.env.project.joinpath('src/other.py').write_bytes(source.encode())
            self.hashes['src/other.py'] = co.sha256(source.encode())
            self.assertIsNone(self.excerpt(self.observed('nl -ba src/app.py\nnl -ba src/other.py')))
        tool = self.observed('cat src/app.py')
        for first, last in ((0, 1), (3, 2), (1, 5), (5, 5)):
            self.assertIsNone(self.excerpt(tool, first=first, last=last))
        self.assertIsNone(self.excerpt(dict(tool, status='failed')))
        self.assertIsNone(self.excerpt(dict(tool, exit_code=1)))
        self.assertIsNone(self.excerpt(self.observed('\n'.join(['cat src/app.py'] * 17), '')))
        with patch.object(co, 'MAX_REUSE_SOURCE_BYTES', 1):
            self.assertIsNone(self.excerpt(tool))

    def test_batch_reuse_keeps_excerpt_strength_hash_and_disclosure_rules(self):
        sid = self.env.to_revealed()
        session = self.env.session(sid)
        command = self.observed('nl -ba src/app.py\nnl -ba src/other.py')
        command.update(call=1, item_id='batch')
        links = {'links': [{'id': 'X1', 'ref': 'src/app.py:L1-L3', 'strength': 'excerpt', 'tool_items': ['batch']},
                           {'id': 'X2', 'ref': 'src/other.py:L1-L3', 'strength': 'excerpt', 'tool_items': ['batch']}],
                 'cited_files': self.hashes}
        original = co._artifact_or_none
        def artifacts(ident, relative, *args):
            return links if relative == 'codex/proposal_links.json' else original(ident, relative, *args)
        with patch.object(co, 'load_tools', return_value=[command]), patch.object(co, '_artifact_or_none', side_effect=artifacts):
            rows = {e['id']: e for e in co.reuse_evidence(session)}
            self.assertEqual(rows['X1']['text'], 'line1\nline2\nline3')
            self.assertEqual(rows['X2']['text'], 'other1\n\nother3')
            links['links'][0]['strength'] = 'mention'
            self.assertFalse(next(e for e in co.reuse_evidence(session) if e['id'] == 'X1')['reuse'])
            before_reveal = dict(session, frozen={k:v for k,v in session['frozen'].items() if k != 'codex'})
            self.assertEqual(co.reuse_evidence(before_reveal), [])


class CompactTests(Base):
    """L3: 無損失圧縮（既定オフ）と読み取り専用の計測。"""

    def snapshot(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for root in (self.env.artifacts, self.env.tmp)
                for p in sorted(root.rglob('*')) if p.is_file()}

    def session_with_unrelated_issue(self, compact=False):
        sid = self.env.to_revealed(compact=compact)
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1'), fx.issue('I2', importance='low')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.env.ok('call', '--session', sid, '--phase', 'debate')
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger(extra=[fx.issue('I2', importance='low', codex='reject',
                                                                                    claude='proposed')]))
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(decisions_extra=[
            {'issue_id': 'I2', 'decision': 'Keep', 'adopted_from': 'codex', 'reason': 'low', 'rejected_alternatives': [],
             'conditions': [], 'residual_risk': 'none', 'codex_checked': False}]))
        output = self.env.ok('synthesis', 'validate', '--session', sid)
        self.assertEqual(output['next'], 'fidelity')
        return sid, output['issue_ids']

    def test_default_off_and_compact_flow(self):
        sid = self.env.init()
        self.assertFalse(self.env.session(sid)['compact'])
        self.env.behaviors({p: "assert 'Request format: compact' in prompt" for p in ('debate', 'fidelity')})
        sid, _, final = fx.run_plan_a(self.env, compact=True)
        self.assertTrue(self.env.session(sid)['compact'])
        self.assertEqual(self.env.calls(), ['proposal', 'debate', 'fidelity'])
        self.assertTrue(json.loads(Path(final['session_summary']).read_text())['compact'])

    def test_measure_reports_sizes_and_is_read_only(self):
        sid, listed = self.session_with_unrelated_issue()
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        before = self.snapshot()
        results = {phase: self.env.ok('measure-request', '--session', sid, '--phase', phase)
                   for phase in ('proposal', 'debate', 'fidelity')}
        self.assertEqual(self.snapshot(), before)
        for phase, result in results.items():
            with self.subTest(phase=phase):
                self.assertTrue(result['lossless']['ok'], result['lossless'])
                self.assertTrue(result['default_matches_actual'], result)
                self.assertLessEqual(result['compact']['bytes'], result['baseline']['bytes'])
                self.assertIn('system instructions', result['note'])
        self.assertLess(results['debate']['compact']['bytes'], results['debate']['baseline']['bytes'])
        self.assertLess(results['fidelity']['compact']['est_tokens'], results['fidelity']['baseline']['est_tokens'])
        self.assertTrue(results['fidelity']['conditional_inputs_unchanged'])
        # 一時ログが消えても計測できる（実リクエストの大きさは不明として返す）。
        shutil.rmtree(self.env.tmp / f'co-design-{sid}')
        result = self.env.ok('measure-request', '--session', sid, '--phase', 'debate')
        self.assertIsNone(result['actual_request_bytes'])
        self.assertEqual(self.env.run('measure-request', '--session', sid, '--phase', 'round2')[0]['status'], 'REFUSED')

    def test_compact_conditional_request_is_lossless_for_listed_issues(self):
        sid, listed = self.session_with_unrelated_issue()
        # 整形された synthesis（Claude が書く通常の形）で行番号保持の抜粋を確かめる。
        path = self.env.artifacts / sid / 'artifacts/synthesis.json'
        path.write_text(json.dumps(json.loads(path.read_text()), ensure_ascii=False, indent=1))
        self.assertEqual(self.env.ok('synthesis', 'validate', '--session', sid)['next'], 'fidelity')
        session = self.env.session(sid)
        baseline = co.build_request(session, 'fidelity', compact=False)
        compact = co.build_request(session, 'fidelity', compact=True)
        blocks = co.request_blocks(compact)
        flt = json.loads(blocks['EXCERPT FILTER'])
        self.assertEqual((flt['listed'], flt['omitted']), (listed, [i for i in ('I1', 'C1', 'I2') if i not in listed]))
        sent = {i['id']: i for i in json.loads(blocks['LEDGER'])}
        self.assertEqual(sorted(sent), sorted(listed))
        ledger = fx.final_ledger(extra=[fx.issue('I2', importance='low', codex='reject', claude='proposed')])
        for issue in ledger['issues']:
            if issue['id'] in sent:
                self.assertEqual((sent[issue['id']]['claude']['stance'], sent[issue['id']]['codex']['stance']),
                                 (issue['claude']['stance'], issue['codex']['stance']))
        # synthesis は元の行番号のまま関連決定だけを送る。
        original = (self.env.artifacts / sid / 'artifacts/synthesis.json').read_text().splitlines()
        for line in blocks['SYNTHESIS (cite as synthesis:L<n>)'].splitlines():
            if not line.startswith('...'):
                number, _, text = line.partition(': ')
                self.assertEqual(original[int(number) - 1], text)
        self.assertNotIn('"Keep"', blocks['SYNTHESIS (cite as synthesis:L<n>)'])
        self.assertIn('"Keep"', co.request_blocks(baseline)['SYNTHESIS (cite as synthesis:L<n>)'])
        check = co.lossless_check(session, 'fidelity', baseline, compact)
        self.assertTrue(check['ok'], check)
        # 両提案は内容が同一（整形だけ違う）。
        self.assertTrue(check['proposals_equal'])

    def test_lossless_check_detects_loss(self):
        sid, _ = self.session_with_unrelated_issue()
        session = self.env.session(sid)
        baseline = co.build_request(session, 'debate', compact=False)
        compact = co.build_request(session, 'debate', compact=True)
        self.assertTrue(co.lossless_check(session, 'debate', baseline, compact)['ok'])
        broken = compact.replace('"X2"', '"Y2"').replace('"proposed"', '"accept"', 1)
        check = co.lossless_check(session, 'debate', baseline, broken)
        self.assertFalse(check['ok'])
        self.assertIn('X2', check['evidence_ids_missing'])

    def test_related_issue_closure_and_dedupe(self):
        ledger = fx.ledger([dict(fx.issue('I1'), title='Depends on I3 and ledger:I9'), fx.issue('I2'), fx.issue('I3'),
                            dict(fx.issue('I4'), title='see I1')])
        related, unresolved = co.related_issues(ledger, {'decisions': []}, ['I1'])
        self.assertEqual((sorted(related), unresolved), (['I1', 'I3'], ['I9']))
        def dedupe(text):
            return co._dedupe_ledger([dict(fx.issue('C1', origin='codex'), codex={'position': text, 'stance': 'proposed', 'evidence_ids': []},
                                           turns=[dict(fx.ANSWER, speaker='codex', kind='critique', text=text)])],
                                     co._same_as_index({'debate': {'critique_of_claude': [{'issue_id': 'C1', 'claim': text}],
                                                                   'responses_to_claude_critique': []}}))
        # 参照より長い原文だけ置き換える。
        issues = dedupe('x' * 200)
        self.assertTrue(issues[0]['codex']['position'].startswith('@same-as YOUR EARLIER DEBATE OUTPUTS debate.critique_of_claude[C1].claim'))
        self.assertTrue(issues[0]['turns'][0]['text'].startswith('@same-as '))
        # 参照の方が長くなる短い原文（50 文字）は原文のまま残す。
        issues = dedupe('x' * 50)
        self.assertEqual(issues[0]['codex']['position'], 'x' * 50)
        self.assertEqual(issues[0]['turns'][0]['text'], 'x' * 50)

    def test_synthesis_excerpt_fallbacks(self):
        doc = fx.synthesis()
        minified = json.dumps(doc)
        self.assertEqual(co.synthesis_excerpt(minified, {'I1'}), co.numbered(minified))
        self.assertEqual(co.synthesis_excerpt('not json', {'I1'}), co.numbered('not json'))
        pretty = json.dumps(doc, indent=1)
        excerpt = co.synthesis_excerpt(pretty, {'I1'})
        self.assertIn('omitted: decisions on unrelated issues', excerpt)
        self.assertNotIn('"Do Y"', excerpt)
        self.assertIn('"Use B1"', excerpt)


class ModeMappingDocTests(unittest.TestCase):
    """SKILL.md の文書レベルでモード対応を確認する（Q1）。"""

    def test_skill_modes(self):
        text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        retired = '--' + 'independent'
        self.assertNotIn(retired, text)
        hint = next(line for line in text.splitlines() if line.startswith('argument-hint:'))
        for option in ('--review', '--test-plan', '--code-review', '--frozen-critique'):
            self.assertIn(option, hint)
        rows = {line.split('|')[1].strip(): line for line in text.splitlines() if line.startswith('| ') and '---' not in line}
        self.assertIn('debate', rows['없음'])
        self.assertIn('co_design.py', rows['없음'])
        self.assertIn('review', rows['`--review`'])
        self.assertIn('debate', rows['`--frozen-critique`'])
        self.assertIn('폐기', rows['`independent`'])
        self.assertIn('실행하지 않', rows['`independent`'])
        self.assertTrue((SKILL / 'references/debate.md').is_file())
        self.assertIn('references/debate.md', text)

    def test_runner_details_split_out_with_read_triggers(self):
        # 通常進行で読まない詳細を分け、読む契機と主側の規則は debate.md に残す（文書回帰のみ）。
        debate = (SKILL / 'references/debate.md').read_text(encoding='utf-8')
        runner = (SKILL / 'references/debate-runner.md').read_text(encoding='utf-8')
        self.assertIn('(debate-runner.md)', debate)
        for trigger in ('버전 게이트·자동 점검으로 거부', 'STOPPED·INCOMPLETE·BUDGET', '`prune`'):
            self.assertIn(trigger, debate)
        for kept in ('partial_messages.md', '재시도·우회하지 않는다', '--cross-check-summary', '**거부 처리:**'):
            self.assertIn(kept, debate)
        for moved in ('## 실행 한도·정지', '## 버전 게이트·보존', '**E2E 실측 (codex-cli', 'fidelity-json-lines-v1', 'measure-request'):
            self.assertIn(moved, runner)
        for heading in ('## 버전 게이트·보존', '**E2E 실측 (codex-cli', 'fidelity-json-lines-v1'):
            self.assertNotIn(heading, debate)


class SupplyAndResilienceTests(Base):
    """2026-10-26: 調査中断（誤停止・形式厳格・部分結果）とデータ供給（原文添付・参照データ・自動 compact・web）の補強。"""

    def summary(self, sid, index=0):
        return json.loads(Path(self.env.session(sid)['calls'][index]['summary']).read_text())

    def test_output_marker_ignores_paths_inside_file_text(self):
        sid = '20261008T000000Z-abcdef'
        other = self.env.artifacts / OTHER_SID / 'artifacts/claude'
        other.mkdir(parents=True)
        (other / 'critique.json').write_text('secret claude critique\n')
        line = f"            ('rm -f /tmp/t_*.ts {CTMP}/x/a.txt', None),"
        (self.env.project / 'tests').mkdir()
        (self.env.project / 'tests/test_hooks.py').write_text('import x\n' + line + '\n')
        sed = "/bin/zsh -lc 'sed -n 1,5p tests/test_hooks.py'"
        # 作業ルート内ファイルの本文と一致する行は止めない（直接読み・行番号付き・再帰検索の path:行: 形式）
        self.assertEqual(co.output_hits('import x\n' + line + '\n', sid, sed), [])
        self.assertEqual(co.output_hits('     2\t' + line, sid, "/bin/zsh -lc 'nl -ba tests/test_hooks.py'"), [])
        self.assertEqual(co.output_hits('tests/test_hooks.py:2:' + line, sid, "/bin/zsh -lc 'rg -n rm tests'"), [])
        # 本文以外・作業ルート外・.local の内容は従来どおり全文検査する（2026-10-26 リビュー H1 の回帰）
        stop = {
            f"==> {CTMP}/x/notes.md <==\nhello": ("/bin/zsh -lc 'find . -name x | xargs head'", ['claude-tmp-path']),
            f'Binary file {CTMP}/x/a.bin matches': ("/bin/zsh -lc 'grep -r k .'", ['claude-tmp-path']),
            f'grep: {CTMP}/x: Permission denied': ("/bin/zsh -lc 'grep -r k .'", ['claude-tmp-path']),
            f'./.local/co-design/{OTHER_SID}/artifacts/claude/critique.json:1:secret claude critique':
                ("/bin/zsh -lc 'grep -rn secret .'", ['co-design-artifact-path']),
            f'-rw-r--r--  1 u  staff  12 Oct 26 {CTMP}/x/a.txt': ("/bin/zsh -lc 'ls -l x'", ['claude-tmp-path']),
            # 一致しない行（他所のファイル）は本文でも検査する
            'import y\n' + line.replace('None', 'True'): (sed, ['claude-tmp-path']),
        }
        for text, (command, expected) in stop.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(co.output_hits(text, sid, command), expected)
        # 命令がない（MCP 等）ときは全文検査
        self.assertEqual(co.output_hits(line, sid), ['claude-tmp-path'])

    def test_loose_ids_are_normalized_and_revealed(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "result['evidence'][0]['id'] = 'X-01'; result['evidence'][1]['id'] = 'x2'"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['execution'], output['state']), ('ok', 'CODEX_PROPOSED'), output)
        summary = self.summary(sid)
        self.assertEqual(summary['normalized_ids'], ['X-01 -> X1', 'x2 -> X2'])
        keep = Path(self.env.session(sid)['calls'][0]['summary']).parent
        self.assertEqual([e['id'] for e in json.loads((keep / 'result.json').read_text())['evidence']], ['X1', 'X2', 'X3'])
        self.assertIn('X-01', (keep / 'result.raw.json').read_text())
        proposal = self.env.file('proposal.json', self.env.claude_proposal(sid))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        self.env.ok('reveal', '--session', sid)

    def test_ambiguous_ids_still_refused(self):
        sid = self.env.to_inputs_frozen()
        # 正規化すると既存の X1 と衝突する
        self.env.behaviors({'proposal': "result['evidence'][1]['id'] = 'X-1'"})
        self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual(self.env.session(sid)['stop']['category'], 'SCHEMA')

    def test_codex_bad_brief_ref_keeps_result(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "result['evidence'][2]['ref'] = 'brief:L99'"})
        output, _ = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['execution'], output['state']), ('ok', 'CODEX_PROPOSED'), output)
        self.assertTrue(any('X3' in p for p in self.summary(sid)['problems']))

    def test_partial_messages_kept_on_policy_stop(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "emit({'type': 'item.completed', 'item': {'id': 'm0', 'type': 'agent_message', "
                                        "'text': 'interim finding'}}); mcp('z1', 'node_repl', 'js', {})"})
        self.env.run('call', '--session', sid, '--phase', 'proposal')
        summary = self.summary(sid)
        self.assertEqual(summary['execution'], 'stopped')
        self.assertIn('interim finding', Path(summary['partial_messages']).read_text())

    def test_evidence_captures_and_data_reach_codex(self):
        sid = self.env.init()
        brief, scope, index = self.env.inputs()
        data = self.env.root / 'claude' / 'kpi_summary.csv'
        data.write_text('a,b\n1,2\n')
        self.env.ok('freeze-inputs', '--session', sid, '--brief', brief, '--scope', scope, '--index', index, '--data', data)
        self.assertEqual(list(self.env.session(sid)['data']), ['kpi_summary.csv'])
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        outside = self.env.root / 'claude' / 'notes.txt'
        outside.write_text('captured line one\ncaptured line two\n')
        self.env.ok('evidence', 'add', '--session', sid, '--file', outside, '--lines', '1-2')
        proposal = self.env.file('proposal.json', self.env.claude_proposal(sid))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        self.env.ok('reveal', '--session', sid)
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        request = co.build_request(self.env.session(sid), 'debate')
        self.assertIn('captured line two', request)
        self.assertIn('kpi_summary.csv', request)
        # 凍結した参照データの改変は検出する
        target = self.env.artifacts / sid / 'artifacts/inputs/data/kpi_summary.csv'
        os.chmod(target.parent, 0o700)
        os.chmod(target, 0o600)
        target.write_text('tampered\n')
        self.assertIn('attached data changed', co.verify_frozen(self.env.session(sid)))

    def test_data_attachment_guards(self):
        sid = self.env.init()
        brief, scope, index = self.env.inputs()
        bad = self.env.root / 'claude' / 'settings.json'
        bad.write_text('{}')
        output, _ = self.env.run('freeze-inputs', '--session', sid, '--brief', brief, '--scope', scope, '--index', index,
                                 '--data', bad)
        self.assertEqual(output['status'], 'REFUSED')
        self.assertEqual(self.env.session(sid)['state'], 'INIT')

    def test_oversized_request_falls_back_to_compact(self):
        sid = self.env.to_revealed()
        legacy = self.env.session(sid)
        legacy.pop('request_format')
        co.save_session(legacy)
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        plain = len(co.build_request(self.env.session(sid), 'debate').encode('utf-8'))
        with patch.object(co, 'MAX_REQUEST', plain - 1):
            session = self.env.session(sid)
            # 上限内に収まるのは compact 版だけという条件を作る
            self.assertGreater(len(co.build_request(session, 'debate').encode('utf-8')), co.MAX_REQUEST)
            self.assertLessEqual(len(co.build_request(session, 'debate', compact=True).encode('utf-8')), co.MAX_REQUEST)
            output, _ = self.env.run('call', '--session', sid, '--phase', 'debate')
        self.assertEqual(output['execution'], 'ok', output)
        self.assertTrue(self.env.session(sid)['calls'][-1].get('auto_compact'))

    def test_captures_shrink_to_request_room(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        big = self.env.root / 'claude' / 'big.txt'
        big.write_text('evidence line\n' * 3000)
        self.env.ok('evidence', 'add', '--session', sid, '--file', big, '--lines', '1-3000')
        proposal = self.env.file('proposal.json', self.env.claude_proposal(sid))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        self.env.ok('reveal', '--session', sid)
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        session = self.env.session(sid)
        self.assertIn('evidence line', co.build_request(session, 'debate'))
        # 原文を入れると上限を超える場合は ID だけにして、要求自体は上限内に保つ
        with patch.object(co, 'MAX_REQUEST', 30 * 1024):
            request = co.build_request(session, 'debate')
        self.assertNotIn('evidence line', request)
        self.assertIn('capture omitted for size', request)
        self.assertLessEqual(len(request.encode('utf-8')), 30 * 1024)

    def test_version_change_auto_verifies_and_records(self):
        gate = self.env.artifacts / 'verified_codex_version'
        gate.write_text('codex-cli 9.9.9\n')
        output = self.env.ok('init', '--question-id', 'q')
        self.assertEqual(output['codex_verification']['status'], 'PASS', output)
        self.assertEqual(gate.read_text().strip(), fx.VERSION)
        self.assertEqual(json.loads((self.env.artifacts / 'verified_codex_version.json').read_text())['version'], fx.VERSION)
        self.assertEqual(self.env.calls(), ['verify'])
        # 同じ版なら点検しない
        self.assertIsNone(self.env.ok('init', '--question-id', 'q2')['codex_verification'])
        self.assertEqual(self.env.calls(), ['verify'])

    def test_failed_auto_verify_refuses_without_recording(self):
        gate = self.env.artifacts / 'verified_codex_version'
        gate.write_text('codex-cli 9.9.9\n')
        cases = {
            'deny = False': 'hook denial line for the secret-path probe not found',
            "tool('v2', 'echo x', co_secret)": 'guard hook is not active',
            "emit({'type': 'item.completed', 'item': {'id': 'f1', 'type': 'file_change', 'status': 'completed'}})":
                'unexpected item type',
            "emit({'type': 'turn.failed'})": 'turn.failed',
        }
        for behavior, expected in cases.items():
            with self.subTest(behavior=behavior[:30]):
                # 秘密経路の文字列は実行時に組み立てる（本ファイルに経路形を残さない）
                self.env.behaviors({'verify': f'co_secret = {co.VERIFY_SECRET!r}\n' + behavior})
                output, code = self.env.run('init', '--question-id', 'q')
                self.assertEqual((output['status'], code), ('REFUSED', 2))
                self.assertIn(expected, output['error'])
                self.assertEqual(gate.read_text().strip(), 'codex-cli 9.9.9')
        self.assertEqual([p.name for p in self.env.artifacts.iterdir()], ['verified_codex_version'])

    def test_verify_codex_command(self):
        output, code = self.env.run('verify-codex')
        self.assertEqual((output['status'], code), ('PASS', 0), output)
        self.assertEqual(output['warnings'], [])
        self.assertNotIn('attempts', output)
        self.assertEqual(self.env.calls(), ['verify'])

    # プローブ実行後に止まる（11/20 実測の停滞形）。first_only なら 1 回目の点検だけ止まる。
    STALL = ("if {first_only} is False or open(str(HERE) + '.calls').read().split().count('verify') == 1:\n"
             "    time.sleep(30)\n")

    def test_stalled_verify_retries_once_and_records_warning(self):
        gate = self.env.artifacts / 'verified_codex_version'
        gate.write_text('codex-cli 9.9.9\n')
        self.env.behaviors({'verify': self.STALL.format(first_only=True)})
        with patch.object(co, 'VERIFY_TIMEOUT', 2):
            output = self.env.ok('init', '--question-id', 'q')
        verification = output['codex_verification']
        self.assertEqual(verification['status'], 'PASS', verification)
        self.assertEqual(self.env.calls(), ['verify', 'verify'])
        self.assertEqual([a['status'] for a in verification['attempts']], ['FAIL', 'PASS'])
        self.assertIn('timed out after 2s', verification['attempts'][0]['problems'])
        self.assertTrue(any('passed on the single retry' in w for w in verification['warnings']))
        self.assertEqual(gate.read_text().strip(), fx.VERSION)
        recorded = json.loads((self.env.artifacts / 'verified_codex_version.json').read_text())
        self.assertEqual(len(recorded['attempts']), 2)

    def test_repeated_stall_refuses_after_single_retry(self):
        gate = self.env.artifacts / 'verified_codex_version'
        gate.write_text('codex-cli 9.9.9\n')
        self.env.behaviors({'verify': self.STALL.format(first_only=False)})
        with patch.object(co, 'VERIFY_TIMEOUT', 2):
            output, code = self.env.run('init', '--question-id', 'q')
        self.assertEqual((output['status'], code), ('REFUSED', 2))
        self.assertIn('still failing after the single retry', output['error'])
        self.assertEqual(self.env.calls(), ['verify', 'verify'])
        self.assertEqual(gate.read_text().strip(), 'codex-cli 9.9.9')

    def test_non_stall_failures_are_not_retried(self):
        # hook 不在・形の変化・タイムアウト中の想定外 item は再試行しない（隠さない）。
        gate = self.env.artifacts / 'verified_codex_version'
        gate.write_text('codex-cli 9.9.9\n')
        cases = ('deny = False',
                 "tool('v2', 'echo x', co_secret)",
                 "emit({'type': 'turn.failed'})",
                 "emit({'type': 'item.completed', 'item': {'id': 'f1', 'type': 'file_change', 'status': 'completed'}})\n"
                 + self.STALL.format(first_only=False),
                 "tool('v2', 'echo x', co_secret)\n" + self.STALL.format(first_only=False))
        for behavior in cases:
            with self.subTest(behavior=behavior[:30]):
                Path(str(self.env.fake) + '.calls').unlink(missing_ok=True)
                self.env.behaviors({'verify': f'co_secret = {co.VERIFY_SECRET!r}\n' + behavior})
                with patch.object(co, 'VERIFY_TIMEOUT', 2):
                    output, code = self.env.run('init', '--question-id', 'q')
                self.assertEqual((output['status'], code), ('REFUSED', 2))
                self.assertEqual(self.env.calls(), ['verify'])
                self.assertEqual(gate.read_text().strip(), 'codex-cli 9.9.9')

    def test_search_flag_only_when_web_allowed(self):
        command = co.build_command('codex', Path('s.json'), Path('r.json'), web=True)
        self.assertEqual(command[1:3], ['--search', 'exec'])
        self.assertNotIn('--search', co.build_command('codex', Path('s.json'), Path('r.json')))


if __name__ == '__main__':
    unittest.main()
