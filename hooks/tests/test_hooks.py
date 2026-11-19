import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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


class RelativeWriteTests(unittest.TestCase):
    """Bash の書き込み対象を cwd（命令内の cd を含む）基準で解釈して保護パスと比較する。"""

    @classmethod
    def setUpClass(cls):
        # 実在するハーネス複製（リンク判定・main() の chdir 用）。extra_protected_paths は setup.sh の生成内容に合わせる
        cls.home = Path(tempfile.mkdtemp(prefix='harness-rel-', dir=env.HOME.parent)).resolve()
        for rel in ('hooks/guard.py', 'install/install.py', 'settings/claude.json', 'tools/aws/assume_role.sh',
                    'tools/privacy_check.py', 'tools/harness_setup.py', '.githooks/pre-commit', 'setup.sh',
                    'docs/x.md', 'README.md'):
            (cls.home / rel).parent.mkdir(parents=True, exist_ok=True)
            (cls.home / rel).write_text('')
        h = str(cls.home)
        cls.config = env.config(harness_home=h, extra_protected_paths=[
            h + '/.githooks', h + '/tools/privacy_check.py', h + '/tools/harness_setup.py', h + '/setup.sh'])
        cls.guard_path = env.isolated_guard(cls.config)
        cls.g = env.load(cls.guard_path, 'guard_relative')
        # ハーネス外の作業ディレクトリ
        cls.other = Path(tempfile.mkdtemp(prefix='project-', dir=env.HOME.parent)).resolve()

    def check(self, cases, cwd, guard=None):
        guard = guard or self.g
        for command, expected in cases:
            with self.subTest(command=command, cwd=cwd):
                result = guard.check_bash(command, cwd=cwd)
                self.assertEqual(result[0] if result else None, expected, result)

    def test_relative_writes_to_protected_files_need_approval(self):
        self.check([
            ("sed -i '' 's/a/b/' hooks/guard.py", 'ask'),
            ("sed -i '' 's/a/b/' tools/privacy_check.py", 'ask'),
            ("sed -E -i '' 's/a/b/' hooks/guard.py", 'ask'),
            ("sed --in-place 's/a/b/' ./hooks/../hooks/guard.py", 'ask'),
            ('cp /tmp/x hooks/guard.py', 'ask'),
            ('mv /tmp/x install/install.py', 'ask'),
            ('cp /tmp/x settings/', 'ask'),
            ('mv hooks hooks.bak', 'ask'),
            ('cp /tmp/x tools/aws/assume_role.sh', 'ask'),
            ('cp /tmp/x tools/harness_setup.py', 'ask'),
            ('echo x > setup.sh', 'ask'),
            ('echo x >> hooks/guard.py', 'ask'),
            ('echo x>hooks/guard.py', 'ask'),
            ('echo x >| hooks/guard.py', 'ask'),
            ('echo x >&hooks/guard.py', 'ask'),
            ('cat /tmp/x | tee -a .githooks/pre-commit', 'ask'),
            ('chmod +x .githooks/pre-commit', 'ask'),
            ("python3 -c \"open('hooks/guard.py','w').write('x')\"", 'ask'),
            ("python3 -c \"import pathlib; pathlib.Path('tools/privacy_check.py').write_text('')\"", 'ask'),
            ('sed -i "" s/a/b/ hooks/*.py', 'ask'),
            ('sed -i "" s/a/b/ */guard.py', 'ask'),
        ], str(self.home))

    def test_cd_is_applied_to_following_segments(self):
        h = str(self.home)
        self.check([
            ("cd hooks; sed -i '' 's/a/b/' guard.py", 'ask'),
            ('cd hooks && echo x > guard.py', 'ask'),
            ('cd tools; cp /tmp/x privacy_check.py', 'ask'),
            ('(cd hooks && echo x > guard.py)', 'ask'),
            ('{ cd hooks; echo x > guard.py; }', 'ask'),
            ('if cd hooks; then echo x > guard.py; fi', 'ask'),
            ('pushd hooks; echo x > guard.py', 'ask'),
            ("sh -c 'cd hooks; echo x > guard.py'", 'ask'),
            ('cd docs; cd ..; echo x > hooks/guard.py', 'ask'),
            (f'cd /tmp && cd {h}/hooks && echo x > guard.py', 'ask'),
            # 移動先を確定できない cd の後の相対パス書き込みは承認
            ('cd "$X"; sed -i "" s/a/b/ guard.py', 'ask'),
            ('cd $(git rev-parse --show-toplevel) && echo x > notes.md', 'ask'),
            ('cd foo`echo x` && echo x > notes.md', 'ask'),
            ('cd -; echo x > notes.md', 'ask'),
            ('cd; echo x > notes.md', 'ask'),
            ('popd; echo x > notes.md', 'ask'),
            ('eval cd hooks; echo x > guard.py', 'ask'),
            ('eval "cd hooks && echo x > guard.py"', 'ask'),
            ('echo "$(cd hooks; echo x > guard.py)"', 'ask'),
            ("bash -c 'cd /tmp; true'; echo x > notes.md", None),
            ('eval cd docs; echo x > notes.md', None),
            # 書き込みの承認より deny 判定を優先する
            ('echo x > hooks/guard.py; aws s3api delete-bucket --bucket fake', 'deny'),
            # 確定できなくても、書き込みがない・絶対パスだけなら従来どおり
            ('cd "$X"; ls', None),
            ('cd "$X"; cp /tmp/a /tmp/b', None),
            ('cd docs && echo x > a.md', None),
        ], h)
        # cd .. は字句・実体の両方で解釈する
        self.check([("cd ..; sed -i '' 's/a/b/' hooks/guard.py", 'ask'),
                    ("cd ..; sed -i '' 's/a/b/' docs/x.md", None)], h + '/docs')

    def test_unrelated_relative_writes_are_allowed(self):
        self.check([
            ("sed -i '' 's/a/b/' docs/x.md", None),
            ("sed -i '' 's/hooks/x/' README.md", None),
            ('echo x > notes.txt', None),
            ('echo x > hooks.md', None),
            ('cp README.md /tmp/x', None),
            ('cat hooks/guard.py > /tmp/out', None),
            ('grep -n "->" hooks/guard.py', None),
            ('rg "a > b" hooks/', None),
            ('python3 -m unittest discover -s hooks/tests -t hooks/tests', None),
            ('python3 tools/privacy_check.py --all', None),
            ('python3 -c "import json; print(json.dumps(1))" < hooks/guard.py', None),
            ('git diff -- hooks/guard.py > /tmp/d.txt 2>&1', None),
            # 区切りをクォートした heredoc の本文はデータ（展開・実行されない）
            ("cat > /tmp/msg.txt <<'EOF'\nfix: hooks/guard.py 수정 (cp/mv 대상 > hooks/guard.py)\nEOF", None),
            ("git commit -F - <<\"EOF\"\ncp x hooks/guard.py\nEOF", None),
            ("git commit -m \"$(cat <<'EOF'\nfix: echo x > hooks/guard.py\nEOF\n)\"", None),
        ], str(self.home))

    def test_heredoc_exemption_is_limited(self):
        self.check([
            # heredoc の書き込み先自体・本文の後のコマンドは判定する
            ("cat > hooks/guard.py <<'EOF'\nx\nEOF", 'ask'),
            ("cat > /tmp/x <<'EOF'\nx\nEOF\necho x > hooks/guard.py", 'ask'),
            ("cat > /tmp/x <<-'EOF'\n\tx\n\tEOF\necho x > hooks/guard.py", 'ask'),
            # 区切りが未クォートの本文は置換が実行されるため除外しない
            ('cat > /tmp/x <<EOF\n$(cd hooks; echo x > guard.py)\nEOF', 'ask'),
            # クォート内の << は heredoc ではない
            ("echo \"<<'X'\"\nsed -i '' s/a/b/ hooks/guard.py", 'ask'),
            ("echo '<<X'\necho x > hooks/guard.py\nX", 'ask'),
        ], str(self.home))

    def test_symlinks_are_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td).resolve()
            (td / 'alias').symlink_to(self.home / 'hooks', target_is_directory=True)
            (td / 'root').symlink_to(self.home, target_is_directory=True)
            self.check([('echo x > alias/guard.py', 'ask'), ('cp /tmp/x alias', 'ask'),
                        ('cd root/hooks; echo x > guard.py', 'ask'), ('echo x > other.txt', None)], str(td))
            # 実行ディレクトリ自体がリンク
            self.check([("sed -i '' s/a/b/ hooks/guard.py", 'ask')], str(td / 'root'))
            # 設定の harness_home がリンク（保護パスの実体でも判定する）。Edit 系も同じ
            linked = env.load(env.isolated_guard(env.config(harness_home=str(td / 'root'))), 'guard_linked_home')
            self.check([("sed -i '' s/a/b/ hooks/guard.py", 'ask'), ('echo x > docs/x.md', None)],
                       str(self.home), linked)
            self.assertEqual(linked.check_edit(str(self.home / 'hooks/guard.py'))[0], 'ask')
            self.assertIsNone(linked.check_edit(str(self.home / 'docs/x.md')))

    def test_copy_and_link_check_destination_only(self):
        self.check([
            # cp・ln・install・rsync は宛先だけ、mv は移動元と宛先の両方
            ('cp hooks/guard.py /tmp/x', None),
            ('cp -r hooks /tmp/backup', None),
            ('cp /tmp/x hooks/guard.py', 'ask'),
            ('cp -t hooks /tmp/x', 'ask'),
            ('cp --target-directory=hooks /tmp/x', 'ask'),
            ('ln -s hooks/guard.py /tmp/link', None),
            ('ln -sf /tmp/x hooks/guard.py', 'ask'),
            ('cd hooks && ln -s /tmp/x/guard.py', 'ask'),
            ('mv hooks/guard.py /tmp/x', 'ask'),
            ('install -m 644 /tmp/x hooks/guard.py', 'ask'),
            ('install -m 644 hooks/guard.py /tmp/x', None),
            ('install -d hooks/new', 'ask'),
            ('rsync -a /tmp/x/ hooks/', 'ask'),
            ('rsync -a hooks/ /tmp/b/', None),
            ('rsync -a hooks/ host:/b/', None),
            # find -exec cp など書き込みコマンドが引数側にあるものは従来どおり全語を対象
            ('find /tmp -name x -exec cp {} hooks/ +', 'ask'),
        ], str(self.home))

    def test_added_write_commands(self):
        h = str(self.home)
        self.check([
            ('dd if=/tmp/x of=hooks/guard.py', 'ask'),
            ('dd if=hooks/guard.py of=/tmp/x', None),
            ('curl -sSLo hooks/guard.py https://example.com/x', 'ask'),
            ('curl --output=tools/privacy_check.py https://example.com/x', 'ask'),
            ('curl -s https://example.com/hooks/guard.py -o /tmp/x', None),
            ('wget -O hooks/guard.py https://example.com/x', 'ask'),
            ('wget -O /tmp/x https://example.com/hooks/guard.py', None),
            ('patch -p1 < /tmp/x.patch', 'ask'),
            ('git apply /tmp/x.patch', 'ask'),
            ('git checkout -- hooks/guard.py', 'ask'),
            ('git restore .', 'ask'),
            ('git stash', 'ask'),
            ('git apply --check /tmp/x.patch', None),
            ('git stash list', None),
            ('git status', None),
            ('git -C /tmp/other-repo apply /tmp/x.patch', None),
            ('tar -xzf /tmp/a.tgz -C hooks', 'ask'),
            ('tar xzf /tmp/a.tgz -C tools/aws', 'ask'),
            ('unzip /tmp/a.zip -d hooks', 'ask'),
            ('tar -czf /tmp/a.tgz hooks', None),
            ('tar -xzf /tmp/a.tgz', None),
            ('unzip -o /tmp/a.zip', None),
        ], h)
        self.check([('tar -xzf /tmp/a.tgz', 'ask'), ('unzip /tmp/a.zip', 'ask'), ('unzip -l /tmp/a.zip', None)],
                   h + '/hooks')
        # ハーネス外の作業ディレクトリでは展開・パッチ・git は従来どおり
        other = str(self.other)
        self.check([('patch -p1 < /tmp/x.patch', None), ('git stash', None), ('tar -xzf /tmp/a.tgz', None),
                    (f'git -C {h} stash', 'ask'), (f'patch -d {h}/docs -p1 < /tmp/x.patch', 'ask')], other)

    def test_moving_or_changing_protected_ancestors(self):
        h = str(self.home)
        parent, name = os.path.split(h)
        self.check([
            (f'mv {h} /tmp/x', 'ask'),
            (f'chmod -R u+w {h}', 'ask'),
            (f'ln -sfn /tmp/x {h}', 'ask'),
            (f'mv {h}/docs /tmp/d', None),
            (f'chmod 755 {h}/docs', None),
            (f'chmod -R go-w {h}/docs', None),
        ], str(self.other))
        self.check([(f'mv {name} x', 'ask'), (f'chmod -R 700 {name}', 'ask')], parent)
        self.check([('chmod -R go-w .', 'ask'), ('chmod -w hooks/guard.py', 'ask'), ('chmod 644 docs/x.md', None)], h)

    def test_unexpanded_variables_in_harness(self):
        h = str(self.home)
        self.check([
            ('echo x > $D/guard.py', 'ask'),
            ("sed -i '' 's/a/b/' $F", 'ask'),
            ('cp /tmp/x "$T"', 'ask'),
            ('tar -xzf /tmp/a.tgz -C $D', 'ask'),
            # スクリプト中の $ はパスではない
            ("sed -i '' 's/a$/b/' docs/x.md", None),
            ('echo "$X" > /tmp/x', None),
        ], h)
        self.check([('echo x > $D/guard.py', None), ('cp /tmp/x "$T"', None),
                    (f'cd {h}/docs; echo x > $D/a', 'ask')], str(self.other))

    def test_heredoc_body_exemption_depends_on_consumer(self):
        self.check([
            # 本文を実行し得るコマンド・パイプで渡す場合は本文も判定する
            ("source /dev/stdin <<'X'\necho x > hooks/guard.py\nX", 'ask'),
            (". /dev/stdin <<'X'\ncp /tmp/x hooks/guard.py\nX", 'ask'),
            ("cat <<'X' | tee /tmp/y\necho x > hooks/guard.py\nX", 'ask'),
            ("bash -c \"$(cat <<'X'\necho x > hooks/guard.py\nX\n)\"", 'ask'),
            # 注釈中の << は heredoc ではない
            ("cat /tmp/a # <<'X'\necho x > hooks/guard.py\nX", 'ask'),
            # データとしてだけ使う heredoc（$'…' を含む行も正しく読む）
            ("git tag -a v1 -F - <<'EOF'\ncp x hooks/guard.py\nEOF", None),
            ("git commit --file=- <<'EOF'\necho x > hooks/guard.py\nEOF", None),
            ("tee /tmp/y <<'EOF'\ncp x hooks/guard.py\nEOF", None),
            ("cat $'a\\'b' > /tmp/o <<'X'\ncp x hooks/guard.py\nX", None),
        ], str(self.home))

    def test_long_commands_are_bounded(self):
        # 上限超過は判定前に承認（Codex は拒否）
        long = 'echo ' + 'a' * (self.g.BASH_MAX_CHARS + 1)
        self.assertEqual(self.g.check_bash(long, cwd=str(self.home))[0], 'ask')
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': long}, 'cwd': str(self.home)})
        for runtime, expected in (('claude', 'ask'), ('codex', 'deny')):
            result = run_guard(payload, runtime, guard=self.guard_path)
            self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'], expected)
        # 上限以下でも二乗時間の後戻りにならない入力（正規表現は C 実装で期限切れの割り込みが効かない）
        limit = self.g.BASH_MAX_CHARS
        for unit in ('find ', 'cd ', 'sed ', 'perl ', 'python ', 'unset ', 'xargs ', 'rsync ', 'greengrass-cli ',
                     '1', ','):
            text = (unit * (limit // len(unit)))[:limit - 10]
            for name, function in (('ordered', lambda t: [self.g.ordered_search(p, t) for p in
                                                          self.g.INLINE_DELETE + self.g.PROFILE_BYPASS
                                                          + self.g.DEPLOY_PATTERNS + list(self.g.WRITE_ORDERED)]),
                                   ('devnull', lambda t: self.g.DEVNULL_RE.sub(' ', t)),
                                   ('write', self.g.is_write_text),
                                   ('brace', lambda t: self.g.brace_expand('{' + t))):
                with self.subTest(unit=unit, check=name):
                    started = time.monotonic()
                    function(text)
                    self.assertLess(time.monotonic() - started, 1.0)

    def test_without_cwd_uses_protected_names(self):
        cases = [
            ("sed -i '' 's/a/b/' hooks/guard.py", 'ask'),
            ('cp /tmp/x tools/privacy_check.py', 'ask'),
            ('cp /tmp/x tools/aws/assume_role.sh', 'ask'),
            ('echo x > setup.sh', 'ask'),
            ('cp a .githooks/pre-commit', 'ask'),
            ('mv /tmp/x install', 'ask'),
            ("sed -i '' 's/a/b/' docs/x.md", None),
            ('echo x > myhooks/x', None),
            ('echo x > notes.txt', None),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                result = self.g.check_bash(command)
                self.assertEqual(result[0] if result else None, expected, result)

    def test_hook_input_cwd_end_to_end(self):
        h = str(self.home)
        cases = [
            ('Bash', {'command': "sed -i '' 's/a/b/' hooks/guard.py"}, h, 'ask'),
            ('Bash', {'command': 'cd tools && echo x > privacy_check.py'}, h, 'ask'),
            ('Bash', {'command': "sed -i '' 's/a/b/' docs/x.md"}, h, None),
            ('Bash', {'command': "sed -i '' 's/a/b/' guard.py"}, h + '/hooks', 'ask'),
            # Edit 系・apply_patch の相対パスも hook 入力の cwd 基準
            ('Write', {'file_path': 'hooks/guard.py'}, h, 'ask'),
            ('Edit', {'file_path': 'docs/x.md'}, h, None),
            ('apply_patch', {'command': '*** Update File: tools/privacy_check.py\n'}, h, 'ask'),
            ('apply_patch', {'command': '*** Update File: docs/x.md\n'}, h, None),
        ]
        for runtime in ('claude', 'codex'):
            for tool, tool_input, cwd, expected in cases:
                with self.subTest(runtime=runtime, tool=tool, tool_input=tool_input):
                    payload = json.dumps({'tool_name': tool, 'tool_input': tool_input, 'cwd': cwd})
                    result = run_guard(payload, runtime, guard=self.guard_path)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    decision = json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout else None
                    self.assertEqual(decision, 'deny' if runtime == 'codex' and expected == 'ask' else expected)


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
