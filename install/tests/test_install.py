"""install.py の検証。すべて一時 HOME で行い、実際の HOME・設定には触れない。"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pty
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HARNESS = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('install', HARNESS / 'install/install.py')
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)
# venv の python は check_python が拒否するため、子プロセスは基底インタプリタで起動する
BASE_PYTHON = getattr(sys, '_base_executable', None) or sys.executable


def write_config(directory, with_aws=True, **extra):
    """一時ハーネス複製（directory/harness）内に設定を書く（install.py は複製内の設定しか読まない）。"""
    data = json.loads((HARNESS / 'config.example.json').read_text())
    data.update(project_root=str(directory / 'project'), python=BASE_PYTHON, **extra)
    if not with_aws:
        data.pop('aws')
    path = directory / 'harness' / 'config.test.json'
    path.write_text(json.dumps(data))
    return path


def copy_harness(directory):
    """テスト用のハーネス複製。設定の配置先と生成物のパスを実リポジトリから切り離す。"""
    target = directory / 'harness'
    shutil.copytree(HARNESS, target, ignore=shutil.ignore_patterns('.git', '__pycache__', '.local', 'tests',
                                                                   'config.local.json'))
    return target


def seed_home(home):
    """既存利用者の設定を模した仮 HOME（秘密値はダミー）。"""
    files = {
        '.claude/settings.json': json.dumps({'model': 'keep-me', 'permissions': {'ask': ['Bash(user rule)']},
                                             'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': '/usr/local/bin/user-hook'}]}]}}),
        '.claude.json': json.dumps({'oauthAccount': {'placeholder': 'dummy'}, 'mcpServers': {
            'context7': {'command': 'npx', 'args': ['-y', '@upstash/context7-mcp']},
            'aws-api': {'command': 'uvx', 'args': ['awslabs.aws-api-mcp-server'], 'env': {'AWS_REGION': 'us-east-1'}}}}),
        '.codex/config.toml': ('model = "m"\n\n[mcp_servers.aws-api]\ncommand = "uvx"\n\n'
                               '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n'),
        '.claude/CLAUDE.md': '# My rules\n\n- keep this line\n',
        '.codex/AGENTS.md': '# Agents\n',
    }
    for rel, text in files.items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def run_install(home, *args, tty_answer=None):
    """一時 HOME で install.py を子プロセス実行する。tty_answer があれば擬似端末を stdin にして確認文字列を送る。"""
    env = {**os.environ, 'HOME': str(home)}
    command = [BASE_PYTHON, str(home.parent / 'harness/install/install.py'), *args]
    if tty_answer is None:
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    master, slave = pty.openpty()
    try:
        os.write(master, (tty_answer + '\n').encode())
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, stdin=slave)
    finally:
        os.close(slave)
        os.close(master)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='install-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.harness = copy_harness(self.root)
        harness_patch = patch.object(i, 'HARNESS', self.harness)
        harness_patch.start()
        self.addCleanup(harness_patch.stop)
        # 子プロセスの HOME が実際の HOME と異なることを前提にする（誤って実環境へ書かない）
        self.assertNotEqual(self.home, Path.home())

    def settings(self, with_aws=True):
        with patch.object(i, 'HOME', self.home):
            return i.load_settings(write_config(self.root, with_aws), with_aws)

    def test_mid_install_failure_rolls_back_and_removes_created(self):
        home = self.home
        initial = {home/'.claude/settings.json': '{}', home/'.claude.json': '{}', home/'.codex/config.toml': '',
                   home/'.claude/CLAUDE.md': 'before', home/'.codex/AGENTS.md': 'before'}
        for p, s in initial.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(s)
        created = home/'.codex/hooks/new.py'
        edits = {created: 'new', home/'.claude/CLAUDE.md': 'changed', home/'.claude/settings.json': 'new settings'}
        real = i.atomic
        config = write_config(self.root)

        def fail(path, content, mode=0o600):
            if path == home/'.claude/settings.json' and content == 'new settings':
                raise OSError('test failure')
            return real(path, content, mode)
        with patch.object(i, 'HOME', home), patch.object(i, 'check_python'), patch.object(i, 'confirm_apply'), \
                patch.object(i, 'build', return_value=(edits, [])), patch.object(i, 'atomic', side_effect=fail), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                i.main(['--apply', '--config', str(config)])
        self.assertFalse(created.exists())
        for p, s in initial.items():
            self.assertEqual(p.read_text(), s)

    def test_guard_wrapper_denies_when_interpreter_fails(self):
        # 起動失敗・異常終了は deny JSON、正常判定（無出力・exit 0）はそのまま通す
        hooks = self.root / 'hooks'
        hooks.mkdir()
        (hooks/'guard.py').write_text('import sys\nsys.exit(0 if "--ok" in sys.argv else 1)\n')
        for python, args, expected in [('/nonexistent/python3', [], 'deny'), (sys.executable, [], 'deny'),
                                       (sys.executable, ['--ok'], None)]:
            with patch.object(i, 'PYTHON', python):
                (hooks/i.GUARD_WRAPPER).write_text(i.guard_wrapper(hooks))
            result = subprocess.run(['/bin/sh', str(hooks/i.GUARD_WRAPPER), *args], input='{}', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            decision = json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout.strip() else None
            self.assertEqual(decision, expected, (python, args))

    def test_guard_hook_commands_use_wrapper_and_stay_owned(self):
        claude = i.hook(i.HOME/'.claude/hooks/guard.py')['command']
        codex = i.hook(i.HOME/'.codex/hooks/guard.py', True)['command']
        self.assertEqual(claude, f'"/bin/sh" "{i.HOME}/.claude/hooks/{i.GUARD_WRAPPER}"')
        # Codex は trusted_hash 再承認が必要になるため従来形式のまま
        self.assertTrue(codex.endswith('/.codex/hooks/guard.py" --codex'))
        self.assertTrue(i.owned_command(codex))
        self.assertTrue(i.owned_command(f'"/bin/sh" "{i.HOME}/.codex/hooks/{i.GUARD_WRAPPER}" --codex'))
        # 旧形式（python 直接起動）も所有 hook として置換対象に残す
        self.assertTrue(i.owned_command(f'"/usr/bin/python3" "{i.HOME}/.codex/hooks/guard.py" --codex'))
        self.assertFalse(i.owned_command(f'"/bin/sh" "{i.HOME}/.codex/hooks/other.sh"'))

    def fragments(self, with_aws=True):
        settings = self.settings(with_aws)
        return (i.load_fragment(settings, 'codex.config.toml', 'codex.config.aws.toml'),
                i.load_fragment(settings, 'mcp.json', 'mcp.aws.json'))

    def test_codex_config_keeps_tables_inside_marker_block_and_is_idempotent(self):
        # Codex が hook ブロック内に [hooks.state]・[tui] を追記し、マーカーが直前セクションに隣接した配置
        fragment, mcp = self.fragments()
        owned = lambda name: f'"/usr/bin/python3" "{i.HOME}/.codex/hooks/{name}" --codex'
        source = (f'model = "m"\n\n[mcp_servers.aws-api]\ncommand = "uvx"\n\n[shell_environment_policy]\n\n{i.BEGIN}\n'
                  f'[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch|Read|mcp__aws-api__call_aws"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = {json.dumps(owned("guard.py"))}\ntimeout = 10\n'
                  f'[[hooks.PreToolUse]]\nmatcher = ".*"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = {json.dumps(owned("usage_alert.py"))}\ntimeout = 10\n'
                  f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = {json.dumps(owned("usage_alert.py"))}\ntimeout = 10\n\n'
                  '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "/usr/local/bin/my-own-hook"\n\n'
                  '[hooks.state]\n\n[hooks.state."cfg:pre_tool_use:0:0"]\ntrusted_hash = "sha256:x"\n\n'
                  f'[tui]\nstatus_line = ["a"]\n\n{i.END}\n')
        once = i.codex_config(source, fragment, mcp)
        parsed = i.tomllib.loads(once)
        # guard の matcher は正規化後の MCP 名（aws_api）を含む全 MCP に一致する
        guard_matcher = next(g['matcher'] for g in parsed['hooks']['PreToolUse'] if any('guard.py' in h['command'] for h in g['hooks']))
        for name in ['Bash', 'apply_patch', 'Read', 'mcp__aws-api__call_aws', 'mcp__aws_api__call_aws',
                     'mcp__context7__resolve_library_id', 'mcp__node_repl__js']:
            self.assertTrue(re.fullmatch(guard_matcher, name), name)
        self.assertIsNone(re.fullmatch(guard_matcher, 'web__run'))
        self.assertEqual(parsed['hooks']['state'], {'cfg:pre_tool_use:0:0': {'trusted_hash': 'sha256:x'}})
        self.assertEqual(parsed['tui'], {'status_line': ['a']})
        self.assertEqual([len(parsed['hooks'][e]) for e in ['PreToolUse', 'UserPromptSubmit', 'Stop', 'SubagentStop']], [2, 1, 2, 1])
        self.assertIn('/usr/local/bin/my-own-hook', [h['command'] for g in parsed['hooks']['Stop'] for h in g['hooks']])
        self.assertEqual((once.count(i.BEGIN), once.count(i.END)), (1, 1))
        self.assertEqual(i.codex_config(once, fragment, mcp), once)
        # 断片の値（読み取り専用プロファイル・既定値・上書き値・除外変数）
        self.assertEqual(parsed['mcp_servers']['aws-api']['env'], {'AWS_API_MCP_PROFILE_NAME': 'claude-assumed', 'READ_OPERATIONS_ONLY': 'true'})
        self.assertEqual(parsed['shell_environment_policy']['set'], {'AWS_PROFILE': 'claude-assumed'})
        self.assertIn('AWS_SECRET_ACCESS_KEY', parsed['shell_environment_policy']['exclude'])
        self.assertEqual((parsed['agents'], parsed['sandbox_workspace_write']), ({'max_threads': 2, 'max_depth': 1}, {'network_access': False}))
        self.assertEqual((parsed['tool_output_token_limit'], parsed['sandbox_mode']), (6000, 'workspace-write'))

    def test_codex_config_without_aws_and_missing_server(self):
        fragment, mcp = self.fragments(with_aws=False)
        parsed = i.tomllib.loads(i.codex_config('sandbox_mode = "read-only"\n', fragment, mcp))
        self.assertNotIn('set', parsed.get('shell_environment_policy', {}))
        self.assertNotIn('mcp_servers', parsed)
        self.assertEqual(parsed['sandbox_mode'], 'read-only')  # 利用者の値は変えない
        fragment, mcp = self.fragments(with_aws=True)
        warnings = []
        parsed = i.tomllib.loads(i.codex_config('', fragment, mcp, warnings))
        # サーバー未定義なら env テーブルだけを作らない
        self.assertNotIn('mcp_servers', parsed)
        self.assertTrue(any('aws-api' in w for w in warnings))

    def test_codex_config_ownership_mixed_groups_and_unsafe_split(self):
        fragment, mcp = self.fragments()
        owned = json.dumps(f'"/usr/bin/python3" "{i.HOME}/.codex/hooks/guard.py" --codex')
        source = ('[[hooks.PreToolUse]]\nmatcher = "Bash"\n'
                  f'[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = {owned}\n'
                  '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "/usr/local/bin/mixed-user-hook"\n\n'
                  '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n# /.codex/hooks/guard.py を参考にした独自 hook\ntype = "command"\ncommand = "/usr/local/bin/commented-hook"\n\n'
                  '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
                  f'command = {json.dumps("/usr/local/bin/audit " + str(i.HOME) + "/.codex/hooks/guard.py")}\n')
        once = i.codex_config(source, fragment, mcp)
        parsed = i.tomllib.loads(once)
        commands = lambda e: [h['command'] for g in parsed['hooks'][e] for h in g['hooks']]
        # 混在グループは利用者 hook と matcher を保持し、所有 hook は末尾の 1 件だけ
        self.assertIn({'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/usr/local/bin/mixed-user-hook'}]}, parsed['hooks']['PreToolUse'])
        self.assertEqual(sum(i.owned_command(c) for c in commands('PreToolUse')), 2)
        self.assertIn('/usr/local/bin/commented-hook', commands('Stop'))
        self.assertTrue(any(c.startswith('/usr/local/bin/audit ') for c in commands('Stop')))
        self.assertEqual(i.codex_config(once, fragment, mcp), once)
        # 文字列内の見出し風の行で分割を誤る場合は書き換えずに中止する
        tricky = (f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = """sh -c "cat <<E\n'
                  f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = {owned}\n[keep]\nE"\n"""\n')
        i.tomllib.loads(tricky)
        with self.assertRaises(RuntimeError):
            i.codex_config(tricky, fragment, mcp)

    def test_render_blocks_and_unresolved_variables(self):
        text = 'a {{PROJECT_ROOT}}\n<!-- BEGIN aws -->\nprofile {{AWS_PROFILE}}\n<!-- END aws -->\nz\n'
        self.assertEqual(i.render(text, {'PROJECT_ROOT': '/p', 'AWS_PROFILE': 'ro'}, True, 't'), 'a /p\nprofile ro\nz\n')
        self.assertEqual(i.render(text, {'PROJECT_ROOT': '/p'}, False, 't'), 'a /p\nz\n')
        with self.assertRaises(ValueError):
            i.render('x {{AWS_PROFILE}}', {}, False, 't')

    def test_config_validation(self):
        bad = [{'project_root': 'relative'}, {'response_language': ''}, {'comment_language': 'a\nb'},
               {'extra_protected_paths': 'x'}, {'python': 'python3'}]
        for extra in bad:
            with self.subTest(extra=extra), patch.object(i, 'HOME', self.home), self.assertRaises(ValueError):
                path = write_config(self.root)
                data = json.loads(path.read_text())
                data.update(extra)
                path.write_text(json.dumps(data))
                i.load_settings(path, False)
        path = write_config(self.root, with_aws=False)
        with patch.object(i, 'HOME', self.home):
            with self.assertRaises(ValueError):
                i.load_settings(path, True)  # --with-aws なのに aws 設定がない
            settings = i.load_settings(path, False)
        self.assertEqual(settings['runtime']['aws'], {'enabled': False})
        self.assertEqual(settings['variables']['MEMORY_DIR'],
                         str(self.home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(self.root / 'project')) / 'memory'))

    def test_dry_run_writes_nothing_and_lists_changes(self):
        seed_home(self.home)
        before = snapshot(self.home)
        config = write_config(self.root)
        for args in ([], ['--with-aws'], ['--target', 'claude'], ['--target', 'codex', '--with-aws']):
            with self.subTest(args=args):
                result = run_install(self.home, '--config', str(config), *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(snapshot(self.home), before)
                self.assertIn('DRY-RUN', result.stdout)
                self.assertNotIn('dummy', result.stdout)  # 既存設定の値を出力しない
        result = run_install(self.home, '--config', str(config), '--with-aws')
        entries = [re.match(r'^(\w+)\s+(\S+)', l).groups() for l in result.stdout.splitlines()
                   if l.split(' ', 1)[0] in ('CREATE', 'UPDATE', 'SAME')]
        status = dict((path, state) for state, path in entries)
        for rel in ('.claude/hooks/harness_config.json', '.claude/hooks/guard.py', '.codex/hooks/guard_hook.sh',
                    '.claude/skills/cross-check/scripts/co_design.py', '.agents/skills/assume-dev/SKILL.md',
                    '.claude/agents/architect.md'):
            self.assertEqual(status.get(str(self.home / rel)), 'CREATE', rel)
        for rel in ('.claude/settings.json', '.claude.json', '.codex/config.toml', '.claude/CLAUDE.md'):
            self.assertEqual(status.get(str(self.home / rel)), 'UPDATE', rel)
        self.assertFalse(any('/tests/' in path for _, path in entries))
        # hook と設定 sidecar は登録より前
        order = [path for _, path in entries]
        self.assertLess(order.index(str(self.home / '.claude/hooks/harness_config.json')), order.index(str(self.home / '.claude/hooks/guard.py')))
        self.assertLess(order.index(str(self.home / '.claude/hooks/guard.py')), order.index(str(self.home / '.claude/settings.json')))

    def test_apply_in_temp_home_is_idempotent_and_keeps_user_settings(self):
        seed_home(self.home)
        config = write_config(self.root)
        first = run_install(self.home, '--config', str(config), '--with-aws', '--apply', tty_answer='apply')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(list((self.home / '.ai-harness/backups').iterdir()))
        # 2 回目は変更なし
        second = run_install(self.home, '--config', str(config), '--with-aws')
        self.assertIn('DRY-RUN: 0 file(s) would change', second.stdout)
        claude = json.loads((self.home / '.claude/settings.json').read_text())
        self.assertEqual(claude['model'], 'keep-me')
        self.assertIn('Bash(user rule)', claude['permissions']['ask'])
        self.assertIn(f'Edit(/{self.harness}/hooks/**)', claude['permissions']['ask'])
        self.assertEqual(claude['env']['AWS_PROFILE'], 'claude-assumed')
        self.assertIn('/usr/local/bin/user-hook', [h['command'] for g in claude['hooks']['Stop'] for h in g['hooks']])
        mcp = json.loads((self.home / '.claude.json').read_text())
        self.assertEqual(mcp['oauthAccount'], {'placeholder': 'dummy'})
        self.assertEqual(mcp['mcpServers']['context7']['args'], ['-y', '@upstash/context7-mcp@4.1.1'])
        self.assertEqual(mcp['mcpServers']['aws-api']['env']['READ_OPERATIONS_ONLY'], 'true')
        self.assertEqual(mcp['mcpServers']['aws-api']['env']['AWS_REGION'], 'us-east-1')
        text = (self.home / '.claude/CLAUDE.md').read_text()
        self.assertTrue(text.startswith('# My rules\n\n- keep this line\n'))
        self.assertEqual(text.count(i.MD_BEGIN), 1)
        self.assertIn('**Korean**', text)
        self.assertIn('claude-assumed', text)
        self.assertIn('**Korean**', (self.home / '.codex/AGENTS.md').read_text())
        # 生成物に未解決の変数が残らない、guard の設定が読める
        for path in self.home.rglob('*'):
            if path.is_file() and path.suffix in ('.md', '.yaml', '.json'):
                self.assertNotIn('{{', path.read_text(), path)
        for runtime in ('.claude', '.codex'):
            loader = importlib.util.spec_from_file_location('g' + runtime[1:], self.home / runtime / 'hooks/guard.py')
            guard = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(guard)
            self.assertIsNone(guard.CONFIG_ERROR)
            self.assertEqual(guard.ALLOWED_PROFILE, 'claude-assumed')
            self.assertEqual(guard.PROJECT, str(self.root / 'project'))
        # 生成した hook・設定 sidecar は所有者のみ読み書き可能
        for path in (self.home / '.claude/hooks').iterdir():
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o077, 0, path)
        # guard ラッパー経由で実際に判定できる（設定 sidecar が有効）
        result = subprocess.run(['/bin/sh', str(self.home / '.claude/hooks/guard_hook.sh')],
                                input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'aws s3 rb s3://x'}}),
                                capture_output=True, text=True, env={**os.environ, 'HOME': str(self.home)}, timeout=30)
        self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        result = subprocess.run(['/bin/sh', str(self.home / '.claude/hooks/guard_hook.sh')],
                                input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}),
                                capture_output=True, text=True, env={**os.environ, 'HOME': str(self.home)}, timeout=30)
        self.assertEqual(result.stdout.strip(), '')

    def test_without_aws_omits_aws_module(self):
        seed_home(self.home)
        config = write_config(self.root, with_aws=False)
        result = run_install(self.home, '--config', str(config), '--apply', tty_answer='apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / '.claude/skills/assume-dev').exists())
        agent = (self.home / '.claude/agents/code-reviewer.md').read_text()
        self.assertNotIn('aws-api', agent)
        self.assertNotIn('<!--', agent)
        runtime = json.loads((self.home / '.claude/hooks/harness_config.json').read_text())
        self.assertEqual(runtime['aws'], {'enabled': False})
        self.assertNotIn('AWS_PROFILE', json.loads((self.home / '.claude/settings.json').read_text())['env'])

    def test_config_must_live_inside_harness(self):
        # リポジトリ外の設定・外を指すリンク・省略形オプションでは何も書かない
        seed_home(self.home)
        before = snapshot(self.home)
        inside = write_config(self.root)
        outside = self.root / 'outside.json'
        outside.write_text(inside.read_text())
        link = self.harness / 'link.json'
        link.symlink_to(outside)
        for config in (outside, link, self.root / 'harness/missing.json'):
            with self.subTest(config=config):
                result = run_install(self.home, '--config', str(config), '--apply')
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('inside', result.stderr)
                self.assertEqual(snapshot(self.home), before)
        result = run_install(self.home, '--config', str(inside), '--ap')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(run_install(self.home, '--config', str(inside)).returncode, 0)

    def test_existing_settings_values_are_kept(self):
        existing = {'env': {'BASH_MAX_OUTPUT_LENGTH': '30000', 'AWS_PROFILE': 'user-choice'},
                    'bashOutputMaxChars': 30000, 'workflowKeywordTriggerEnabled': True,
                    'permissions': {'defaultMode': 'default', 'disableBypassPermissionsMode': 'enable',
                                    'deny': ['Read(./secret)', 'Edit(~/.aws/**)'], 'ask': []},
                    'skillOverrides': {'anthropic-skills:morning': 'on'},
                    'autoMode': {'hard_deny': ['my rule']}}
        warnings = []
        merged = json.loads(i.claude_settings(json.dumps(existing), self.settings(), warnings))
        # 保護用の強制キー（--with-aws の AWS_PROFILE）だけは上書きし、それ以外の既存スカラーは保持する
        self.assertEqual(merged['env'], {'BASH_MAX_OUTPUT_LENGTH': '30000', 'AWS_PROFILE': 'claude-assumed'})
        self.assertEqual(len(warnings), 1)
        self.assertIn('env.AWS_PROFILE will be overwritten', warnings[0])
        self.assertNotIn('user-choice', warnings[0])  # 既存値は出力しない
        # 一致していれば警告しない・AWS モジュールなしでは強制しない
        same = []
        i.claude_settings(json.dumps({'env': {'AWS_PROFILE': 'claude-assumed'}}), self.settings(), same)
        self.assertEqual(same, [])
        kept = json.loads(i.claude_settings(json.dumps(existing), self.settings(with_aws=False), same))
        self.assertEqual(kept['env']['AWS_PROFILE'], 'user-choice')
        self.assertEqual(same, [])
        self.assertEqual((merged['bashOutputMaxChars'], merged['workflowKeywordTriggerEnabled']), (30000, True))
        self.assertEqual((merged['permissions']['defaultMode'], merged['permissions']['disableBypassPermissionsMode']),
                         ('default', 'enable'))
        # リストは重複なく追加、辞書は未設定のキーだけ追加
        self.assertEqual(merged['permissions']['deny'][:2], ['Read(./secret)', 'Edit(~/.aws/**)'])
        self.assertEqual(merged['permissions']['deny'].count('Edit(~/.aws/**)'), 1)
        self.assertIn('Bash(cdk deploy *)', merged['permissions']['deny'])
        self.assertEqual(merged['skillOverrides']['anthropic-skills:morning'], 'on')
        self.assertEqual(merged['skillOverrides']['anthropic-skills:docs'], 'off')
        self.assertEqual(merged['autoMode']['hard_deny'][0], 'my rule')
        self.assertIn('$defaults', merged['autoMode']['hard_deny'])
        # 値がなければ断片の値が入る
        fresh = json.loads(i.claude_settings('{}', self.settings()))
        self.assertEqual((fresh['permissions']['defaultMode'], fresh['env']['AWS_PROFILE']), ('auto', 'claude-assumed'))

    def test_unexpected_structures_stop_cleanly(self):
        settings = self.settings()
        for text in ('[]', '{"permissions": []}', '{"env": "x"}', '{"permissions": {"deny": "x"}}',
                     '{"hooks": []}', '{"hooks": {"Stop": {}}}', '{"hooks": {"Stop": [1]}}',
                     '{"hooks": {"Stop": [{"hooks": "x"}]}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                i.claude_settings(text, settings)
        for text in ('[]', '{"mcpServers": []}', '{"mcpServers": {"aws-api": {"env": []}}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                i.claude_mcp(text, {'servers': {'aws-api': {'env': {'READ_OPERATIONS_ONLY': 'true'}}}}, [])
        # CLI では traceback ではなく ERROR 1 行で終わる
        (self.home / '.claude').mkdir()
        (self.home / '.claude/settings.json').write_text('{"permissions": []}')
        result = run_install(self.home, '--config', str(write_config(self.root)), '--target', 'claude')
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith('ERROR:'), result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_apply_requires_interactive_confirmation(self):
        # AI の非対話実行（stdin が端末でない）・確認文字列の不一致では何も書かない
        seed_home(self.home)
        before = snapshot(self.home)
        config = write_config(self.root)
        result = run_install(self.home, '--config', str(config), '--with-aws', '--apply')
        self.assertEqual(result.returncode, 1)
        self.assertIn('interactive terminal', result.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse((self.home / '.ai-harness').exists())
        for answer in ('', 'yes', 'APPLY'):
            with self.subTest(answer=answer):
                result = run_install(self.home, '--config', str(config), '--apply', tty_answer=answer)
                self.assertEqual(result.returncode, 1)
                self.assertIn('confirmation did not match', result.stderr)
                self.assertEqual(snapshot(self.home), before)
        # 迂回用の環境変数などは存在しない（非 TTY では常に拒否）
        env = {**os.environ, 'HOME': str(self.home), 'AI_HARNESS_APPLY': '1', 'CI': '1'}
        result = subprocess.run([BASE_PYTHON, str(self.harness / 'install/install.py'), '--config', str(config), '--apply'],
                                env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(snapshot(self.home), before)
        # in-process でも確認関数が非 TTY を拒否する
        with patch('sys.stdin', io.StringIO('apply\n')), self.assertRaises(RuntimeError):
            i.confirm_apply(1)

    def test_dry_run_warns_when_enforced_key_differs(self):
        seed_home(self.home)
        settings = self.home / '.claude/settings.json'
        settings.write_text(json.dumps({'env': {'AWS_PROFILE': 'user-choice'}}))
        result = run_install(self.home, '--config', str(write_config(self.root)), '--with-aws', '--target', 'claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WARN   ~/.claude/settings.json: env.AWS_PROFILE will be overwritten', result.stdout)
        self.assertNotIn('user-choice', result.stdout)
        self.assertEqual(json.loads(settings.read_text())['env']['AWS_PROFILE'], 'user-choice')

    def test_venv_interpreter_is_refused(self):
        venv = self.root / 'venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
        python = venv / 'bin/python3'
        python.symlink_to(BASE_PYTHON)
        with self.assertRaises(RuntimeError):
            i.check_python(str(python))
        with self.assertRaises(RuntimeError):
            i.check_python('python3')


if __name__ == '__main__':
    unittest.main()
