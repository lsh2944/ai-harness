import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_env as env  # noqa: E402

ROOT = env.HOOKS
P = env.PROJECT


def fill(text):
    """テストケースの置換記号を隔離環境の値に置き換える。"""
    return text.replace('<PROJECT>', env.PROJECT).replace('<HOME>', str(env.HOME)).replace('<PROFILE>', env.PROFILE)


def run_guard(payload, runtime='claude', guard=None, environ=None):
    return subprocess.run([sys.executable, str(guard or env.GUARD)] + (['--codex'] if runtime == 'codex' else []),
                          input=fill(payload), capture_output=True, text=True, env=environ or env.ENV)


g = env.load()
_spec = importlib.util.spec_from_file_location('usage_alert', env.SOURCE.with_name('usage_alert.py'))
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)

class GuardTests(unittest.TestCase):
    def test_command_policy(self):
        cases = [
            # 出力捨て・fd 複製は書き込みではない（保護語を含む読み取り検索）。実際の書き込みは従来どおり確認
            ('rg -n "ai-harness/hooks" .claude/skills 2>/dev/null', None),
            ('grep -rn ".claude/settings" docs 2>&1 >/dev/null', None),
            ('echo x 2>/dev/null > ~/.claude/settings.json', 'ask'),
            ('cat a 2>/dev/null | tee ai-harness/hooks/guard.py', 'ask'),
            ('echo x 2>/dev/null>~/.claude/settings.json', 'ask'),
            ('echo x >&2 >~/.claude/settings.json', 'ask'),
            ('aws ec2 describe-instances', None),
            ('aws sts get-caller-identity --profile <PROFILE>', None),
            ('aws s3api delete-bucket --bucket fake', 'deny'),
            ("sh -c 'aws s3api delete-bucket --bucket fake'", 'deny'),
            ("sh -c 'rm <PROJECT>/fake'", 'ask'),
            ('find /tmp/fake <PROJECT>/fake -delete', 'ask'),
            ('cat ~/.aws/credentials # assume_role.sh', 'deny'),
            ('aws configure get aws_secret_access_key', 'deny'),
            ('aws configure get region', None),
            ('aws secretsmanager get-secret-value --secret-id fake', 'deny'),
            ('AWS_SHARED_CREDENTIALS_FILE=/tmp/fake aws ec2 describe-instances', 'deny'),
            ('env -i aws ec2 describe-instances', 'deny'),
            ('env --unset=AWS_PROFILE aws ec2 describe-instances', 'deny'),
            ("AWS_PROFILE=default sh -c 'aws sts get-caller-identity'", 'deny'),
            ('AWS_PROFILE= aws ec2 describe-instances', 'deny'),
            ('AWS_PROFILE=default; aws sts get-caller-identity', 'deny'),
            ('cat ~/.aws/./credentials', 'deny'),
            ('cat <~/.aws/./credentials', 'deny'),
            ('cd ~/.aws && cat credentials', 'deny'),
            ('aws ec2 describe-instances --profile default', 'deny'),
            ('aws s3 sync s3://fake /tmp/fake --delete', 'ask'),
            ('rm /tmp/harness-test-placeholder', None),
            ('rm -f /tmp/t_*.ts /private/tmp/claude-502/x/a.txt', None),
            ('rm /tmp/*/fake', 'ask'),
            ('rm <PROJECT>/*.txt', 'ask'),
            ("find /tmp/fake -name '*.ts' -delete", None),
            ("find -L /tmp/fake -delete", 'ask'),
            ('rm /tmp', 'ask'),
            ('rm <PROJECT>/fake', 'ask'),
            ('<PROJECT>/assume_role.sh --awsEnv DEV', None),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                result = g.check_bash(fill(command))
                self.assertEqual(result[0] if result else None, expected)

    def test_wrapped_and_interpreted_deletes(self):
        cases = [
            ('timeout 10 rm -rf <PROJECT>/fake', 'ask'),
            ('timeout --signal=KILL 5 rm <PROJECT>/fake', 'ask'),
            ('nice rm -rf <PROJECT>/fake', 'ask'),
            ('nice -n 10 rm <PROJECT>/fake', 'ask'),
            ('sudo -u root rm <PROJECT>/fake', 'ask'),
            ('caffeinate -i rm <PROJECT>/fake', 'ask'),
            ('eval rm -rf <PROJECT>/fake', 'ask'),
            ("eval 'rm -rf <PROJECT>/fake'", 'ask'),
            ('perl -e "unlink q(<PROJECT>/fake)"', 'ask'),
            ("find <PROJECT>/fake -execdir rm {} +", 'ask'),
            ("find <PROJECT>/fake -exec timeout 1 rm {} +", 'ask'),
            ('timeout 30 aws s3 rm s3://fake/x', 'deny'),
            ('nice aws lambda invoke --function-name fake /tmp/out', 'deny'),
            ('sudo --user root rm <PROJECT>/fake', 'ask'),
            ('/usr/bin/nice rm -rf <PROJECT>/fake', 'ask'),
            ('/usr/bin/env rm -rf <PROJECT>/fake', 'ask'),
            ('/usr/bin/env AWS_PROFILE=default aws sts get-caller-identity', 'deny'),
            ('stdbuf --output=L rm <PROJECT>/fake', 'ask'),
            ('arch -arm64 rm <PROJECT>/fake', 'ask'),
            ('timeout 10 rm -f /tmp/harness-test-placeholder', None),
            ('eval echo ok', None),
            ('command -v rm', None),
            ('timeout 5 ls <PROJECT>', None),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                result = g.check_bash(fill(command), cwd=P)
                self.assertEqual(result[0] if result else None, expected)

    def test_secret_paths(self):
        cases = [
            ('cat ~/.aws/cli/cache/fake.json', 'deny'),
            ('cat ~/.aws/sso/cache/fake.json', 'deny'),
            ('cat ~/.aws/.credentials-fake.tmp', 'deny'),
            ('cat ~/.aws/unknown-file', 'deny'),
            ('grep -r key ~/.aws', 'deny'),
            ('tar czf /tmp/x.tgz ~/.aws', 'deny'),
            ('cp -r $HOME/.aws /tmp/x', 'deny'),
            ('cat ~/.codex/auth.json', 'deny'),
            ('cat ~/.claude/.credentials.json', 'deny'),
            ("python3 -c \"print(open('<HOME>/.codex/auth.json').read())\"", 'deny'),
            ('aws ec2 describe-instances --cli-input-json file://~/.aws/cli/cache/fake.json', 'deny'),
            ('cd ~/.aws/cli && cat cache/fake.json', 'deny'),
            ('grep -r token ~/.codex', 'deny'),
            ('grep -Rn token <HOME>', 'deny'),
            ('rg token ~', 'deny'),
            ('cp -R ~/.codex /tmp/x', 'deny'),
            ('cat ~/.codex/auth.*', 'deny'),
            ('cat ~/.codex/a?th.json', 'deny'),
            ('cat ~/.co*/auth.json', 'deny'),
            ('cat ~/.codex/auth.{json,bak}', 'deny'),
            ('cd ~/.codex && rg --hidden --no-ignore token', 'deny'),
            ('cd ~ && grep -r token', 'deny'),
            ('grep -d recurse token ~/.codex', 'deny'),
            ('grep -r -f ~/.codex/auth.json src', 'deny'),
            ('tar czf /tmp/x.tgz -C /tmp ~/.codex', 'deny'),
            ('rg --hidden -etoken ~/.codex', 'deny'),
            ('grep -r -etoken ~/.codex', 'deny'),
            ('grep -rnf ~/.codex/auth.json src', 'deny'),
            ('grep -r -f~/.codex/auth.json src', 'deny'),
            ('rg --hidden --regexp=token ~/.codex', 'deny'),
            ('cat ~/.codex/auth.{' + ','.join(f'x{n}' for n in range(64)) + ',json}', 'ask'),
            ('sudo --chdir ~/.codex rg --hidden token', 'ask'),
            ('sudo -D ~/.codex rg token', 'ask'),
            ('grep -rne token src', None),
            ('rsync -a /tmp/pub/ ~/', None),
            ('tar xzf /tmp/x.tgz -C ~', None),
            ('grep -rn <HOME> src', None),
            ('rg -e . README.md', None),
            ('rg token src', None),
            ('cat ~/.codex/*.toml', None),
            ('grep token ~/.codex/config.toml', None),
            ('ls ~/.codex', None),
            ('cd ~ && ls', None),
            ('cat ~/.aws/config', None),
            ('tail ~/.aws/aws-api-mcp/aws-api-mcp-server.log', None),
            ('cat ~/.codex/config.toml', None),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                result = g.check_bash(fill(command), cwd=P)
                self.assertEqual(result[0] if result else None, expected)
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / 'auth'
            link.symlink_to(env.HOME / '.codex/auth.json')
            self.assertEqual(g.check_bash(f'cat {link}')[0], 'deny')
            self.assertEqual(self.invoke(json.dumps({'tool_name':'Read','tool_input':{'file_path':str(link)}})), 'deny')
            # 保護パス自体が外部へのリンクでも字句パスで保護する
            home = Path(td).resolve() / 'home'
            outside = Path(td).resolve() / 'outside'
            (home / '.codex').mkdir(parents=True)
            outside.mkdir()
            (outside / 'auth.json').write_text('x')
            (home / '.codex/auth.json').symlink_to(outside / 'auth.json')
            (home / '.aws').symlink_to(outside, target_is_directory=True)
            with patch.object(g, 'AWS_DIR', str(home / '.aws')), \
                 patch.object(g, 'SECRET_FILES', (str(home / '.codex/auth.json'),)):
                self.assertTrue(g.is_secret(str(home / '.codex/auth.json')))
                self.assertTrue(g.is_secret(str(outside / 'auth.json')))
                self.assertTrue(g.is_secret(str(home / '.aws/credentials')))
                self.assertTrue(g.is_secret(str(outside / 'credentials')))
                self.assertFalse(g.is_secret(str(home / '.aws/config')))
                self.assertEqual(g.check_bash(f'cat {home}/.codex/auth.json')[0], 'deny')
        tools = [
            ('Read', {'file_path':'~/.aws/cli/cache/fake.json'}, 'deny'),
            ('Read', {'file_path':'~/.codex/auth.json'}, 'deny'),
            ('Read', {'file_path':'~/.aws/config'}, None),
            ('Grep', {'pattern':'key', 'path':'~/.aws'}, 'deny'),
            ('Grep', {'pattern':'token', 'path':'~/.codex', 'glob':'auth.json'}, 'deny'),
            ('Grep', {'pattern':'x', 'path':'~'}, 'deny'),
            ('Grep', {'pattern':'x', 'path':'~/.codex/agents'}, None),
            ('Grep', {'pattern':'token', 'path':'~/.codex/auth.json'}, 'deny'),
            ('Grep', {'pattern':'x', 'path':'~', 'glob':'.aws/credentials'}, 'deny'),
            ('Grep', {'pattern':'x', 'path':'<PROJECT>'}, None),
        ]
        for tool, tool_input, expected in tools:
            with self.subTest(tool=tool, tool_input=tool_input):
                result = run_guard(json.dumps({'tool_name':tool,'tool_input':tool_input}))
                decision = json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout else None
                self.assertEqual(decision, expected)

    def test_symlink_temp_is_not_exempt(self):
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / 'escape'
            link.symlink_to('/usr', target_is_directory=True)
            self.assertFalse(g.is_temp(str(link / 'file')))

    def invoke(self, data, runtime='claude', environ=None):
        result = run_guard(data, runtime, environ=environ)
        return json.loads(result.stdout)['hookSpecificOutput']['permissionDecision']

    def decision(self, tool, tool_input, runtime, policy):
        environ = {k: v for k, v in env.ENV.items() if k != g.TOOL_POLICY_ENV}
        if policy is not None:
            environ[g.TOOL_POLICY_ENV] = policy
        result = run_guard(json.dumps({'tool_name': tool, 'tool_input': tool_input}), runtime, environ=environ)
        return json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout.strip() else 'allow'

    def test_codex_tool_policy_blocks_before_execution(self):
        cua = ('mcp__cua_repl__js', {'code': '1+1'})
        shell = ('Bash', {'command': 'ls'})
        read = ('Read', {'file_path': '<PROJECT>/CLAUDE.md'})
        patch_ = ('apply_patch', {'command': '*** Begin Patch\n*** Add File: /tmp/x\n+a\n*** End Patch'})
        cases = [
            ('none', cua, 'deny'), ('none', shell, 'deny'), ('none', read, 'deny'),
            ('read', cua, 'deny'), ('read', patch_, 'deny'), ('read', ('web_search', {}), 'deny'),
            ('read', shell, 'allow'), ('read', read, 'allow'),
            # read でも通常の秘密情報検査は残る
            ('read', ('Bash', {'command': 'cat ~/.aws/credentials'}), 'deny'),
            ('bogus', shell, 'deny'), (None, cua, 'allow'),
        ]
        for policy, (tool, ti), expected in cases:
            self.assertEqual(self.decision(tool, ti, 'codex', policy), expected, (policy, tool))
        # Claude 側は方針変数を無視する
        self.assertEqual(self.decision(*cua, 'claude', 'none'), 'allow')

    def test_guard_timeout_denies(self):
        # 期限切れは Exception の下位で OSError ではない（キャッシュ判定の except OSError に吸収されない）
        self.assertFalse(issubclass(g.GuardTimeout, OSError))
        # 期限を 1 秒に縮め、判定前に 3 秒止めた guard が hook timeout より前に deny を返す
        result = subprocess.run([sys.executable, '-c',
                                 'import sys,signal,time;sys.argv=["guard.py"];'
                                 f'src=open({str(env.GUARD)!r}).read().replace("GUARD_SECONDS = 8","GUARD_SECONDS = 1")'
                                 '.replace("    tool = data.get(\\"tool_name\\", \\"\\")","    time.sleep(3); tool = data.get(\\"tool_name\\", \\"\\")");'
                                 'exec(compile(src,"guard.py","exec"),{"__name__":"__main__","__file__":' + repr(str(env.GUARD)) + '})'],
                                input='{}', capture_output=True, text=True, timeout=10, env=env.ENV)
        output = json.loads(result.stdout)['hookSpecificOutput']
        self.assertEqual(output['permissionDecision'], 'deny')
        # json.load の except ではなく、最上位の期限切れ経路で拒否したこと
        self.assertIn('제한 시간', output['permissionDecisionReason'])

    def test_invalid_input_blocks(self):
        self.assertEqual(self.invoke('bad json'), 'deny')

    def test_codex_ask_never_fails_open(self):
        data = json.dumps({'tool_name':'Bash','tool_input':{'command':'rm <PROJECT>/fake'}})
        self.assertEqual(self.invoke(data, 'codex'), 'deny')
        self.assertEqual(self.invoke(data), 'ask')

    def test_aws_mcp_both_tool_names(self):
        # Codex は aws-api を aws_api に正規化して hook に渡す
        for runtime in ('claude', 'codex'):
            for name in ('mcp__aws-api__call_aws', 'mcp__aws_api__call_aws'):
                for command, expected in [('aws sts get-caller-identity --profile default', 'deny'),
                                          ('aws s3api delete-bucket --bucket fake', 'deny'),
                                          ('echo not-aws', 'deny'),
                                          ('aws sts get-caller-identity', None),
                                          ('aws ec2 describe-instances --profile <PROFILE>', None)]:
                    with self.subTest(runtime=runtime, name=name, command=command):
                        result = run_guard(json.dumps({'tool_name':name,'tool_input':{'cli_command':command}}), runtime)
                        decision = json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout else None
                        self.assertEqual(decision, expected)

    def test_generic_mcp_secret_paths(self):
        cases = [
            ('mcp__context7__resolve-library-id', {'libraryName':'react','query':'hooks'}, None),
            ('mcp__playwright__browser_navigate', {'url':'https://example.com'}, None),
            ('mcp__node_repl__js', {'code':"require('fs').readFileSync(require('os').homedir()+'/.codex/auth.json')"}, 'deny'),
            ('mcp__playwright__browser_navigate', {'url':'file://<HOME>/.aws/credentials'}, 'deny'),
            ('mcp__future_server__read_file', {'path':'~/.aws/cli/cache/fake.json'}, 'deny'),
            ('mcp__future_server__read_file', {'args':{'paths':['/tmp/ok', '$HOME/.aws']}}, 'deny'),
            ('mcp__future_server__read_file', {'path':'~/.aws/config'}, None),
            ('mcp__future_server__read_file', {'path':'<PROJECT>/README.md'}, None),
            ('mcp__future_server__bulk', {'items':[str(n) for n in range(600)]}, 'ask'),
        ]
        for runtime in ('claude', 'codex'):
            for name, tool_input, expected in cases:
                with self.subTest(runtime=runtime, name=name, tool_input=str(tool_input)[:60]):
                    result = run_guard(json.dumps({'tool_name':name,'tool_input':tool_input}), runtime)
                    decision = json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout else None
                    # Codex は ask を deny に変換する
                    self.assertEqual(decision, 'deny' if runtime == 'codex' and expected == 'ask' else expected)

    def test_codex_patch_protection(self):
        data = json.dumps({'tool_name':'apply_patch','tool_input':{'command':'*** Update File: <HOME>/.codex/config.toml\n'}})
        self.assertEqual(self.invoke(data, 'codex'), 'deny')

class GuardConfigTests(unittest.TestCase):
    """設定の欠落・破損は fail-closed、aws.enabled=false はプロファイル固定だけを外す。"""

    def decide(self, guard, tool, tool_input, runtime='claude'):
        result = run_guard(json.dumps({'tool_name': tool, 'tool_input': tool_input}), runtime, guard=guard)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['hookSpecificOutput'] if result.stdout.strip() else None

    def test_missing_or_broken_config_denies_everything(self):
        bad = {
            'missing': None,
            'not json': '{broken',
            'not object': '[]',
            'wrong version': env.config(version=2),
            'relative project': env.config(project_root='relative/path'),
            'missing harness_home': {k: v for k, v in env.config().items() if k != 'harness_home'},
            'aws not object': env.config(aws=True) | {'aws': 'yes'},
            'aws enabled not bool': env.config() | {'aws': {'enabled': 'true', 'profile': 'x'}},
            'aws profile missing': env.config() | {'aws': {'enabled': True}},
            'aws profile invalid': env.config() | {'aws': {'enabled': True, 'profile': 'a b'}},
            'assume envs invalid': env.config() | {'aws': {'enabled': True, 'profile': 'p', 'assume_role_envs': ['$(x)']}},
            'assume cmd relative': env.config() | {'aws': {'enabled': True, 'profile': 'p', 'assume_role_cmd': 'x.sh'}},
            'extra not list': env.config(extra_protected_paths='/x'),
            'extra relative': env.config(extra_protected_paths=['x']),
        }
        harmless = [('Bash', {'command': 'ls'}), ('Read', {'file_path': '/tmp/x'}),
                    ('Write', {'file_path': '/tmp/x'}), ('mcp__context7__resolve-library-id', {'libraryName': 'react'})]
        for label, data in bad.items():
            guard = env.isolated_guard(data)
            for runtime in ('claude', 'codex'):
                for tool, tool_input in harmless:
                    with self.subTest(label=label, runtime=runtime, tool=tool):
                        output = self.decide(guard, tool, tool_input, runtime)
                        self.assertIsNotNone(output)
                        self.assertEqual(output['permissionDecision'], 'deny')
                        self.assertIn('가드 설정', output['permissionDecisionReason'])

    def test_valid_config_allows_harmless_operations(self):
        guard = env.isolated_guard(env.config())
        self.assertIsNone(self.decide(guard, 'Bash', {'command': 'ls'}))

    def test_module_defaults_are_strict_without_config(self):
        # 判定関数を直接呼んでも、設定なしではプロファイル指定をすべて拒否する。
        broken = env.load(env.isolated_guard(None), 'guard_no_config')
        self.assertTrue(broken.CONFIG_ERROR)
        self.assertTrue(broken.AWS_ENABLED)
        self.assertEqual(broken.check_bash('aws sts get-caller-identity --profile anything')[0], 'deny')
        self.assertEqual(broken.check_bash('aws s3api delete-bucket --bucket fake')[0], 'deny')

    def test_protected_paths_follow_config(self):
        self.assertEqual(g.check_edit(env.HARNESS_HOME + '/hooks/guard.py')[0], 'ask')
        self.assertEqual(g.check_edit(env.HARNESS_HOME + '/install/install.py')[0], 'ask')
        self.assertEqual(g.check_edit(env.HARNESS_HOME + '/config.local.json')[0], 'ask')
        self.assertEqual(g.check_edit(env.HARNESS_HOME + '/tools/aws/assume_role.py')[0], 'ask')
        self.assertIsNone(g.check_edit(env.HARNESS_HOME + '/tools/repo_context.py'))
        self.assertEqual(g.check_edit(env.PROJECT + '/.claude/settings.local.json')[0], 'ask')
        self.assertEqual(g.check_edit(env.ASSUME_ROLE_CMD)[0], 'ask')
        self.assertEqual(g.check_edit(str(env.HOME / '.aws/config'))[0], 'deny')
        self.assertIsNone(g.check_edit(env.HARNESS_HOME + '/README.md'))
        self.assertIsNone(g.check_edit(env.PROJECT + '/src/app.py'))
        for command in ('echo x > ai-harness/settings/claude.settings.json', 'echo {} > hooks/harness_config.json',
                        'cp a config.local.json', 'tee assume_role.sh'):
            with self.subTest(command=command):
                self.assertEqual(g.check_bash(command, cwd=P)[0], 'ask')
        extra = env.load(env.isolated_guard(env.config(extra_protected_paths=['/opt/team-secrets'])), 'guard_extra')
        self.assertEqual(extra.check_edit('/opt/team-secrets/a.txt')[0], 'ask')

    def test_assume_role_exception_is_limited_to_configured_command_and_envs(self):
        cases = [(f'{env.ASSUME_ROLE_CMD} --awsEnv DEV', None), (f'{env.ASSUME_ROLE_CMD} --awsEnv DEV2', None),
                 (f'{env.ASSUME_ROLE_CMD} --awsEnv PRD; rm {P}/fake', 'ask'),
                 (f'{P}/other.sh --awsEnv DEV; rm {P}/fake', 'ask')]
        for command, expected in cases:
            with self.subTest(command=command):
                result = g.check_bash(command, cwd=P)
                self.assertEqual(result[0] if result else None, expected)
        # 例外は削除判定を飛ばすため、設定外の環境名は通常判定（rm は承認）に戻る
        self.assertEqual(g.check_bash(f'{env.ASSUME_ROLE_CMD} --awsEnv PRD && rm {P}/fake', cwd=P)[0], 'ask')

    def test_assume_role_requires_plain_invocation(self):
        # 変数代入・env/export・ラッパー・インタプリタ経由・他コマンド併用は、偽の設定や PATH で
        # 書き込み権限の資格情報を読み取り専用プロファイルへ書き込めるため承認対象にする
        cmd = env.ASSUME_ROLE_CMD
        script_py = env.HARNESS_HOME + '/tools/aws/assume_role.py'
        cases = [
            (f'{cmd} --awsEnv DEV', None),
            (f'echo 123456 | {cmd} --awsEnv DEV', None),
            (f"printf '%s\\n' 123456 | {cmd} --awsEnv DEV2", None),
            ('./assume_role.sh --awsEnv DEV', None),  # cwd 基準の相対指定
            (f'HARNESS_CONFIG=/tmp/fake.json {cmd} --awsEnv DEV', 'ask'),
            (f'PATH=/tmp/evil:/usr/bin {cmd} --awsEnv DEV', 'ask'),
            (f'env FOO=1 {cmd} --awsEnv DEV', 'ask'),
            (f'export PATH=/tmp/evil; {cmd} --awsEnv DEV', 'ask'),
            (f'export PYTHONPATH=/tmp/evil && {cmd} --awsEnv DEV', 'ask'),
            (f'timeout 60 {cmd} --awsEnv DEV', 'ask'),
            (f'sh {cmd} --awsEnv DEV', 'ask'),
            (f"bash -c '{cmd} --awsEnv DEV'", 'ask'),
            (f'python3 {script_py} --awsEnv DEV', 'ask'),
            (f'python3 -I {script_py} --awsEnv DEV', 'ask'),
            (f'{env.HARNESS_HOME}/tools/aws/assume_role.sh --awsEnv DEV', 'ask'),
            (f'{cmd} --awsEnv DEV > /tmp/out', 'ask'),
            (f'{cmd} --awsEnv DEV extra', 'ask'),
            (f'echo $(cat /tmp/x) | {cmd} --awsEnv DEV', 'ask'),
            (f'cat /tmp/x | {cmd} --awsEnv DEV', 'ask'),
            # 実行しない参照は対象外
            (f'cat {script_py}', None),
            (f'rg -n assume_role {env.HARNESS_HOME}/tools', None),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                result = g.check_bash(command, cwd=P)
                self.assertEqual(result[0] if result else None, expected)
        self.assertEqual(self.decide(env.GUARD, 'Bash', {'command': f'FOO=1 {cmd} --awsEnv DEV'}, 'codex')['permissionDecision'], 'deny')

    def test_installer_apply_needs_approval(self):
        home = env.HARNESS_HOME
        cases = [
            (f'python3 {home}/install/install.py --config {home}/config.local.json', None),  # dry-run
            (f'python3 {home}/install/install.py --apply', 'ask'),
            (f'python3 {home}/install/install.py --config {home}/x.json --ap', 'ask'),
            (f'python3 {home}/install/../install/install.py --apply', 'ask'),
            ('python3 install/install.py --apply', 'ask'),
            (f'timeout 60 /usr/bin/python3 -B {home}/install/install.py --target codex --apply', 'ask'),
            (f'env HOME=/tmp/h python3 {home}/install/install.py --apply', 'ask'),
            (f"python3 -c \"import runpy,sys; sys.argv=['i','--apply']; runpy.run_path('{home}/install/install.py')\"", 'ask'),
            (f"sh -c 'python3 {home}/install/install.py --apply'", 'ask'),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                result = g.check_bash(command, cwd=home)
                self.assertEqual(result[0] if result else None, expected)
        # 名前を変えたリンク経由も実体パスで判定する
        root = Path(tempfile.mkdtemp(prefix='harness-', dir=env.HOME.parent))
        (root / 'install').mkdir()
        (root / 'install/install.py').write_text('')
        (root / 'runner').symlink_to(root / 'install/install.py')
        linked = env.load(env.isolated_guard(env.config(harness_home=str(root))), 'guard_linked')
        self.assertEqual(linked.check_bash(f'python3 {root}/runner --apply', cwd='/')[0], 'ask')
        self.assertEqual(linked.check_bash(f'python3 {root}/runner --a', cwd='/')[0], 'ask')
        self.assertIsNone(linked.check_bash(f'python3 {root}/runner', cwd='/'))
        # Codex は承認要求を拒否に変換する
        output = self.decide(env.GUARD, 'Bash', {'command': f'python3 {home}/install/install.py --apply'}, 'codex')
        self.assertEqual(output['permissionDecision'], 'deny')

    def test_any_project_claude_settings_need_approval(self):
        for path in ('/work/other/.claude/settings.json', '/work/other/.claude/settings.local.json',
                     str(env.HOME / 'proj/.claude/settings.local.json')):
            with self.subTest(path=path):
                self.assertEqual(g.check_edit(path)[0], 'ask')
        for path in ('/work/other/.claude/agents/x.md', '/work/other/settings.json', '/work/.claude-settings/x'):
            with self.subTest(path=path):
                self.assertIsNone(g.check_edit(path))

    def test_unexpected_config_errors_fail_closed_in_direct_python_run(self):
        # 深い入れ子（RecursionError）など想定外の例外でも、Codex の直接起動で異常終了せず拒否を返す
        guard = env.isolated_guard('[' * 200000)
        for runtime in ('claude', 'codex'):
            output = self.decide(guard, 'Bash', {'command': 'ls'}, runtime)
            self.assertEqual(output['permissionDecision'], 'deny')
            self.assertIn('가드 설정', output['permissionDecisionReason'])

    def test_aws_disabled_keeps_mutation_blocks_but_unpins_profile(self):
        off = env.load(env.isolated_guard(env.config(aws=False)), 'guard_aws_off')
        self.assertFalse(off.AWS_ENABLED)
        cases = [
            # プロファイル固定は外れる
            ('aws sts get-caller-identity --profile default', None),
            ('AWS_PROFILE=other aws ec2 describe-instances', None),
            ('export AWS_PROFILE=other', None),
            ('env -i aws ec2 describe-instances', None),
            # 変更系・デプロイ・認証情報の直接指定・秘密情報は従来どおり拒否
            ('aws s3api delete-bucket --bucket fake', 'deny'),
            ('aws --profile default lambda invoke --function-name fake /tmp/out', 'deny'),
            ('aws s3 cp /tmp/x s3://fake/x', 'deny'),
            ('aws secretsmanager get-secret-value --secret-id fake', 'deny'),
            ('cdk deploy', 'deny'),
            ('AWS_ACCESS_KEY_ID=AKIAEXAMPLE aws sts get-caller-identity', 'deny'),
            ('AWS_SHARED_CREDENTIALS_FILE=/tmp/fake aws ec2 describe-instances', 'deny'),
            ('cat ~/.aws/credentials', 'deny'),
            (f'rm {P}/fake', 'ask'),
            # 認証更新スクリプトの例外も無効（通常判定に戻る）
            (f'{env.ASSUME_ROLE_CMD} --awsEnv DEV && rm {P}/fake', 'ask'),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                result = off.check_bash(command, cwd=P)
                self.assertEqual(result[0] if result else None, expected)
        guard = env.isolated_guard(env.config(aws=False))
        for name in ('mcp__aws-api__call_aws', 'mcp__aws_api__call_aws'):
            for command, expected in [('aws sts get-caller-identity --profile default', None),
                                      ('aws s3api delete-bucket --bucket fake', 'deny'),
                                      ('echo not-aws', 'deny')]:
                with self.subTest(name=name, command=command):
                    output = self.decide(guard, name, {'cli_command': command})
                    self.assertEqual(output['permissionDecision'] if output else None, expected)


class UsageTests(unittest.TestCase):
    def test_warning_distinguishes_local_tokens_from_account_quota(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 's.jsonl'
            path.write_text(json.dumps({'type':'assistant','message':{'id':'one','usage':{'input_tokens':100_001}}})+'\n')
            data = {'transcript_path':str(path),'hook_event_name':'PreToolUse'}
            message = u.monitor(data, 'claude', Path(td)/'state', 100)['systemMessage']
            for text in ('이번 요청 새 입력 100,001 토큰', '알림 기준 100,000',
                         '계정 한도·잔여량·사용률을 뜻하지 않습니다', '경고가 없어도', '모델·추론·필수 검증은 유지'):
                self.assertIn(text, message)

    def test_unavailable_usage_is_not_zero_quota_consumption(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 's.jsonl'
            path.write_text('{}\n')
            data = {'transcript_path':str(path),'hook_event_name':'Stop'}
            message = u.monitor(data, 'codex', Path(td)/'state', 100)['systemMessage']
            self.assertIn('집계할 수 없어', message)
            self.assertIn('계정 한도·잔여량·사용률을 뜻하지 않습니다', message)
            self.assertEqual(u.monitor(data, 'codex', Path(td)/'state', 101), {})

    def test_history_warmup_is_not_new_turn_usage(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'s.jsonl';directory=Path(td)/'state'
            def row(key):
                return json.dumps({'type':'assistant','message':{'id':key,'usage':{'input_tokens':100_001}}})+'\n'
            path.write_text(row('old1')+row('old2'))
            data={'transcript_path':str(path),'session_id':'warm','hook_event_name':'UserPromptSubmit'}
            with patch.object(u,'SCAN_BYTES',1):
                self.assertEqual(u.monitor(data,'claude',directory,100),{})
                data['hook_event_name']='PreToolUse'
                self.assertEqual(u.monitor(data,'claude',directory,131),{})
                with path.open('a') as f:f.write(row('new'))
                self.assertIn('systemMessage',u.monitor(data,'claude',directory,162))

    def test_first_turn_and_no_repeated_alert(self):
        state = {}
        counts = {'input':100_001,'cache':0,'output':0}
        self.assertTrue(u.messages(state, counts, 'PreToolUse'))
        self.assertFalse(u.messages(state, counts, 'PreToolUse'))
        self.assertFalse(u.messages(state, counts, 'Stop'))
        self.assertTrue(u.messages(state, {'input':200_002,'cache':0,'output':0}, 'Stop'))

    def test_incremental_and_duplicate_stream(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'session.jsonl'
            state = {}
            def row(out):
                return json.dumps({'type':'assistant','message':{'id':'same','usage':{'input_tokens':10,'output_tokens':out}}})+'\n'
            path.write_text(row(1))
            self.assertEqual(u.scan(path,state,'claude')['output'],1)
            with path.open('a') as f:f.write(row(5))
            self.assertEqual(u.scan(path,state,'claude'),{'input':10,'cache':0,'output':5})
            self.assertEqual(u.scan(path,state,'claude')['output'],5)

    def test_codex_cumulative_no_double_count(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'session.jsonl'
            usage = {'input_tokens':100,'cached_input_tokens':80,'output_tokens':20,'reasoning_output_tokens':10}
            path.write_text(json.dumps({'type':'token_usage_record','payload':{'thread_token_usage':usage}})+'\n'+
                            json.dumps({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':usage}}})+'\n')
            self.assertEqual(u.scan(path,{},'codex'),{'input':20,'cache':80,'output':20})

    def test_throttle_stop_and_prompt_reset(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'session.jsonl';path.write_text('')
            directory=Path(td)/'state'
            data={'transcript_path':str(path),'session_id':'test','hook_event_name':'UserPromptSubmit'}
            self.assertEqual(u.monitor(data,'claude',directory,100),{})
            row={'type':'assistant','message':{'id':'one','usage':{'input_tokens':100_001}}}
            path.write_text(json.dumps(row)+'\n')
            data['hook_event_name']='PreToolUse'
            self.assertEqual(u.monitor(data,'claude',directory,110),{})
            self.assertIn('systemMessage',u.monitor(data,'claude',directory,131))
            data['hook_event_name']='Stop'
            self.assertEqual(u.monitor(data,'claude',directory,132),{})

    def test_partial_line_and_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'s.jsonl';state={}
            row=json.dumps({'type':'assistant','message':{'id':'x','usage':{'output_tokens':5}}})
            path.write_text(row)
            self.assertEqual(u.scan(path,state,'claude')['output'],0)
            path.write_text(row+'\n')
            self.assertEqual(u.scan(path,state,'claude')['output'],5)
            path.write_text('')
            self.assertEqual(u.scan(path,state,'claude')['output'],0)

if __name__=='__main__': unittest.main()
