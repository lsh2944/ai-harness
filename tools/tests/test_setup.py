"""setup.sh・.githooks・tools/harness_setup.py のテスト。

このリポジトリの作業ツリー（Git 除外ファイルを除く）を一時 origin に複製し、そこからローカル clone して、
一時 HOME・pty（端末入力の代わり）で実行する。実際の HOME・このリポジトリの git 設定は使わない。
install.py の --apply は実行しない（適用段階はスタブの install.py で引数だけ確かめる）。
"""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
BASE_PYTHON = getattr(sys, '_base_executable', None) or sys.executable
HAS_TOMLLIB = sys.version_info >= (3, 11)
WINDOWS = os.name == 'nt'
SETUP_ENTRY = 'setup.cmd' if WINDOWS else './setup.sh'
APPLY_ENTRY = 'apply.cmd' if WINDOWS else './apply.sh'


def find_sh():
    """hook を直接実行する sh。Windows は PATH にないことが多いため Git for Windows 同梱の sh.exe を使う。"""
    found = shutil.which('sh')
    if found or not WINDOWS:
        return found or 'sh'
    git = shutil.which('git')
    for candidate in ([Path(git).resolve().parents[1] / 'bin' / 'sh.exe'] if git else []):
        if candidate.is_file():
            return str(candidate)
    return 'sh'


SH = find_sh()
# setup.sh・apply.sh は sh と pty で端末入力を再現する（Windows は setup.cmd・apply.cmd を WindowsSetupTests で検証）
POSIX_SCRIPTS = unittest.skipIf(WINDOWS, 'setup.sh・apply.sh（sh・pty）は Mac/Linux 用')
# 擬似端末のない環境で「端末から答えを入力した」状態を再現する起動スクリプト（stdin だけを端末扱いにする）
TTY_SHIM = ('import io, runpy, sys\n'
            'class Tty(io.StringIO):\n'
            '    def isatty(self):\n'
            '        return True\n'
            'sys.stdin = Tty(sys.stdin.read())\n'
            'sys.argv = sys.argv[1:]\n'
            'runpy.run_path(sys.argv[0], run_name="__main__")\n')
FAKE_ACCOUNT = ''.join(['3141', '5926', '5358'])
STUB_INSTALL = '''import json, os, sys
if '--apply' in sys.argv:
    with open(os.environ['STUB_MARKER'], 'w') as out:
        json.dump({'argv': sys.argv[1:], 'tty': sys.stdin.isatty(), 'exe': sys.executable}, out)
elif os.environ.get('STUB_NO_CHANGE'):
    print('SAME   /stub/target')
    print('DRY-RUN: 0 file(s) would change; nothing was written. Re-run with --apply to install.')
else:
    print('CREATE /stub/target (+1 lines)')
    print('DRY-RUN: 1 file(s) would change; nothing was written. Re-run with --apply to install.')
'''


def run_pty(argv, answers, cwd, env, timeout=90):
    """pty を stdin/stdout にして実行し、答えを端末入力として渡す。(終了コード, 出力) を返す。"""
    master, slave = os.openpty()
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, close_fds=True)
    finally:
        os.close(slave)
    os.write(master, ''.join(a + '\n' for a in answers).encode())
    chunks, deadline = [], time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise AssertionError('timeout: ' + b''.join(chunks).decode('utf-8', 'replace'))
            ready, _, _ = select.select([master], [], [], min(remaining, 0.5))
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b''
                if not data:
                    break
                chunks.append(data)
            elif proc.poll() is not None:
                break
        proc.wait(timeout=10)
    finally:
        os.close(master)
    return proc.returncode, b''.join(chunks).decode('utf-8', 'replace').replace('\r\n', '\n')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='setup-test-')
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'home'
        self.bin = self.base / 'bin'
        self.home.mkdir()
        self.bin.mkdir()
        if WINDOWS:
            # git・Python・Git Bash の sh を使うため PATH はそのまま。Path.home() は USERPROFILE を読む
            self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                        'GIT_CONFIG_NOSYSTEM': '1', 'XDG_CONFIG_HOME': str(self.home / '.config'), 'PYTHONUTF8': '1'}
        else:
            (self.bin / 'python3').symlink_to(BASE_PYTHON)
            self.env = {
                'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
                'GIT_CONFIG_NOSYSTEM': '1', 'XDG_CONFIG_HOME': str(self.home / '.config'), 'TERM': 'dumb',
            }
        self.origin = self.base / 'origin'
        self.make_origin()
        self.clone = self.base / 'clone'
        self.git(self.base, 'clone', '-q', str(self.origin), str(self.clone))

    def tearDown(self):
        self.temp.cleanup()

    def git(self, cwd, *args, check=True):
        return subprocess.run(['git', *args], cwd=cwd, env=self.env, capture_output=True, text=True, encoding='utf-8',
                              check=check, timeout=60)

    def make_origin(self):
        listing = subprocess.run(['git', '-C', str(REPO), 'ls-files', '-z', '--cached', '--others',
                                  '--exclude-standard'], capture_output=True, check=True).stdout
        for raw in listing.split(b'\0'):
            if not raw:
                continue
            name = os.fsdecode(raw)
            source = REPO / name
            if not source.is_file() or source.is_symlink():
                continue
            target = self.origin / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.git(self.origin, 'init', '-q', '-b', 'main')
        self.git(self.origin, 'config', 'user.name', 'Origin')
        self.git(self.origin, 'config', 'user.email', 'origin@example.invalid')
        self.git(self.origin, 'add', '-A')
        self.git(self.origin, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'base')

    def setup(self, answers, *args, shell=None, cwd=None):
        argv = ([shell, str(self.clone / 'setup.sh')] if shell else [str(self.clone / 'setup.sh')]) + list(args)
        return run_pty(argv, answers, cwd or self.base, self.env)

    def local(self, key):
        result = self.git(self.clone, 'config', '--local', '--get', key, check=False)
        return result.stdout.strip()

    def config(self):
        return json.loads((self.clone / 'config.local.json').read_text(encoding='utf-8'))

    def write_config(self, **extra):
        data = {'project_root': str(self.home / 'project'), 'python': BASE_PYTHON,
                'response_language': 'Korean', 'comment_language': 'Japanese', **extra}
        path = self.clone / 'config.local.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        if not WINDOWS:
            path.chmod(0o600)

    def use_stub_install(self):
        (self.clone / 'install/install.py').write_text(STUB_INSTALL, encoding='utf-8')
        self.marker = self.base / 'apply-marker.json'
        self.env['STUB_MARKER'] = str(self.marker)


@POSIX_SCRIPTS
@unittest.skipUnless(HAS_TOMLLIB, 'install.py dry-run needs Python 3.11+')
class SetupScriptTests(Fixture):
    ANSWERS = ['Test Person', 'person@example.com', '', '', '', '', 'n']

    def test_first_run_configures_repo_and_stops_before_apply(self):
        # PATH の先頭に venv の python3 を置き、検出から外れることも確かめる
        venv = self.base / 'venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text('home = /nowhere\n')
        (venv / 'bin/python3').symlink_to(BASE_PYTHON)
        self.env['PATH'] = f'{venv / "bin"}:{self.env["PATH"]}'
        code, out = self.setup(self.ANSWERS, '--no-apply')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.local('user.name'), 'Test Person')
        self.assertEqual(self.local('user.email'), 'person@example.com')
        self.assertEqual(self.local('core.hooksPath'), '.githooks')
        self.assertFalse((self.home / '.gitconfig').exists(), 'global git config must not be written')
        config_file = self.clone / 'config.local.json'
        self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
        config = self.config()
        self.assertEqual(config['project_root'], str(self.home / 'project'))
        self.assertEqual(config['python'], str(self.bin / 'python3'))
        self.assertEqual((config['response_language'], config['comment_language']), ('Korean', 'Japanese'))
        self.assertNotIn('aws', config)
        root = self.clone.resolve()
        self.assertEqual(config['extra_protected_paths'], [str(root / '.githooks'), str(root / 'tools/privacy_check.py'),
                                                           str(root / 'tools/harness_setup.py'), str(root / 'setup.sh'),
                                                           str(root / 'apply.sh'), str(root / 'setup.cmd'),
                                                           str(root / 'apply.cmd')])
        self.assertIn('AWS 모듈(--with-aws): off', out)
        self.assertIn('DRY-RUN:', out)
        self.assertIn('--no-apply', out)
        self.assertNotIn('SAME ', out)
        # dry-run は一時 HOME にも何も書かない
        self.assertFalse((self.home / '.claude').exists())
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.home / '.ai-harness').exists())

    def test_rerun_skips_completed_steps_without_prompts(self):
        code, out = self.setup(self.ANSWERS, '--no-apply')
        self.assertEqual(code, 0, out)
        before = (self.clone / 'config.local.json').read_bytes()
        for shell in ['sh'] + (['dash'] if shutil.which('dash') else []):
            with self.subTest(shell=shell):
                code, out = self.setup([], '--no-apply', shell=shell)
                self.assertEqual(code, 0, out)
                for step in ('[1/5]', '[2/5]', '[3/5]'):
                    self.assertIn(step, out)
                self.assertEqual(out.count('건너뜁니다'), 3, out)
                self.assertNotIn('user.name:', out)
                self.assertIn('DRY-RUN:', out)
        self.assertEqual((self.clone / 'config.local.json').read_bytes(), before)

    def test_missing_hook_exec_bit_is_reported(self):
        # 実行権限のない hook は git が黙って飛ばすため、post-commit も含めて警告する
        hooks = ('pre-commit', 'commit-msg', 'post-merge', 'post-commit')
        for name in hooks:
            with self.subTest(hook=name):
                hook = self.clone / '.githooks' / name
                hook.chmod(0o644)
                try:
                    code, out = self.setup(self.ANSWERS, '--no-apply')
                    self.assertEqual(code, 0, out)
                    self.assertIn(f'.githooks/{name} 에 실행 권한이 없어', out)
                    others = set(hooks) - {name}
                    self.assertFalse(any(f'.githooks/{o} 에 실행 권한' in out for o in others), out)
                finally:
                    hook.chmod(0o755)

    def test_invalid_answers_are_asked_again(self):
        answers = ['', 'Name', 'not-an-email', 'n@example.com', 'relative/path', '/abs/project',
                   'relative-python', '-', '', '', 'n']
        code, out = self.setup(answers, '--no-apply')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.local('user.email'), 'n@example.com')
        config = self.config()
        self.assertEqual(config['project_root'], '/abs/project')
        self.assertIsNone(config['python'])

    def test_aws_answer_copies_example_section_and_stops(self):
        code, out = self.setup(self.ANSWERS[:-1] + ['y'], '--no-apply')
        self.assertEqual(code, 0, out)
        aws = self.config()['aws']
        example = json.loads((self.clone / 'config.example.json').read_text())['aws']
        self.assertEqual(aws['roles'], example['roles'])
        self.assertEqual(aws['assume_role_cmd'], str(self.clone.resolve() / 'tools/aws/assume_role.sh'))
        self.assertIn('다시 실행', out)
        self.assertNotIn('DRY-RUN', out)
        code, out = self.setup([], '--no-apply')
        self.assertEqual(code, 0, out)
        self.assertIn('견본 값', out)
        self.assertNotIn('DRY-RUN', out)

    def test_existing_hooks_path_is_kept_unless_confirmed(self):
        self.git(self.clone, 'config', '--local', 'core.hooksPath', 'custom-hooks')
        code, out = self.setup(['A', 'a@example.com', 'n', '', '', '', '', 'n'], '--no-apply')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.local('core.hooksPath'), 'custom-hooks')
        self.assertIn('바꾸지 않았습니다', out)

    def test_non_tty_is_refused_without_changes(self):
        result = subprocess.run([str(self.clone / 'setup.sh')], cwd=self.base, env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn('터미널', result.stderr)
        self.assertEqual(self.local('core.hooksPath'), '')
        self.assertFalse((self.clone / 'config.local.json').exists())

    def test_windows_is_refused(self):
        fake = self.base / 'fakebin'
        fake.mkdir()
        (fake / 'uname').write_text('#!/bin/sh\necho MINGW64_NT-10.0\n')
        (fake / 'uname').chmod(0o755)
        self.env['PATH'] = f'{fake}:{self.env["PATH"]}'
        code, out = self.setup([])
        self.assertEqual(code, 1)
        self.assertIn('Windows', out)
        self.assertEqual(self.local('core.hooksPath'), '')

    def test_unknown_option_is_rejected(self):
        code, out = self.setup([], '--yes')
        self.assertEqual(code, 2)
        self.assertIn('usage', out)


@POSIX_SCRIPTS
class ApplyStepTests(Fixture):
    """5) の適用段階。スタブ install.py で、続行を選んだときだけ --apply が端末から呼ばれることを確かめる。"""

    def setUp(self):
        super().setUp()
        self.git(self.clone, 'config', '--local', 'user.name', 'A')
        self.git(self.clone, 'config', '--local', 'user.email', 'a@example.com')
        self.use_stub_install()

    def test_declining_does_not_apply(self):
        self.write_config()
        code, out = self.setup(['n'])
        self.assertEqual(code, 0, out)
        self.assertFalse(self.marker.exists())
        self.assertIn('적용하지 않았습니다', out)
        self.assertIn('--apply', out)

    def test_confirming_runs_install_with_apply_on_terminal(self):
        self.write_config()
        code, out = self.setup(['y'])
        self.assertEqual(code, 0, out)
        record = json.loads(self.marker.read_text())
        self.assertTrue(record['tty'])
        self.assertEqual(record['argv'], ['--config', str(self.clone.resolve() / 'config.local.json'), '--apply'])

    def test_aws_config_adds_with_aws(self):
        self.write_config(aws={'profile': 'p', 'roles': {'DEV': 'arn:aws:iam::' + FAKE_ACCOUNT + ':role/r'},
                               'mfa_serial': 'arn:aws:iam::' + FAKE_ACCOUNT + ':mfa/d'})
        code, out = self.setup(['y'])
        self.assertEqual(code, 0, out)
        self.assertIn('--with-aws', json.loads(self.marker.read_text())['argv'])


class GitHookTests(Fixture):
    def setUp(self):
        super().setUp()
        for repo in (self.clone,):
            self.git(repo, 'config', '--local', 'user.name', 'A')
            self.git(repo, 'config', '--local', 'user.email', 'a@example.com')
            self.git(repo, 'config', '--local', 'core.hooksPath', '.githooks')

    def commit(self, cwd, message='change'):
        return self.git(cwd, '-c', 'commit.gpgsign=false', 'commit', '-qm', message, check=False)

    def test_pre_commit_blocks_fake_account_id_and_allows_clean_commit(self):
        (self.clone / 'notes.md').write_text(f'account {FAKE_ACCOUNT}\n')
        self.git(self.clone, 'add', 'notes.md')
        result = self.commit(self.clone)
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn('notes.md:1: AWS account ID', output)
        self.assertNotIn(FAKE_ACCOUNT, output)
        self.assertIn('--no-verify', output)
        self.assertEqual(self.git(self.clone, 'rev-list', '--count', 'HEAD').stdout.strip(), '1')
        (self.clone / 'notes.md').write_text('account <id>\n')
        self.git(self.clone, 'add', 'notes.md')
        result = self.commit(self.clone)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def origin_commit(self, name, content):
        path = self.origin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git(self.origin, 'add', name)
        self.git(self.origin, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-qm', name)

    def pull(self):
        result = self.git(self.clone, 'pull', '-q', '--ff-only')
        return result.stdout + result.stderr

    def test_pre_commit_special_contents_and_names(self):
        cases = {
            'wide.txt': ('key ' + FAKE_ACCOUNT + '\n').encode('utf-16'),  # BOM 付き UTF-16
            'big.txt': (('x' * 200 + '\n') * 8000 + 'id ' + FAKE_ACCOUNT + '\n').encode(),  # 1MiB 超
            # Windows はファイル名に * を使えない
            ('we[ir]d name.txt' if WINDOWS else 'we[ir]d *name.txt'): ('id ' + FAKE_ACCOUNT + '\n').encode(),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                (self.clone / name).write_bytes(data)
                self.git(self.clone, 'add', '--', name)
                result = self.commit(self.clone)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'{name}:', result.stdout + result.stderr)
                self.git(self.clone, 'rm', '-q', '--cached', '--', name)
                (self.clone / name).unlink()
        # 通常ファイル → 中身に個人情報を含むシンボリックリンクへの型変更
        (self.clone / 'link').write_text('plain\n')
        self.git(self.clone, 'add', 'link')
        self.assertEqual(self.commit(self.clone).returncode, 0)
        (self.clone / 'link').unlink()
        try:
            os.symlink('/Users/' + 'alice' + '/secret', self.clone / 'link')
        except OSError:
            return  # シンボリックリンクを作れない環境（Windows の権限不足）ではリンクの型変更を省く
        self.git(self.clone, 'add', 'link')
        result = self.commit(self.clone)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('link:1: home path', result.stdout + result.stderr)

    def test_pre_commit_blocks_binary(self):
        (self.clone / 'blob.bin').write_bytes(b'\x00\x01\x02')
        self.git(self.clone, 'add', 'blob.bin')
        result = self.commit(self.clone)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SKIP blob.bin: binary', result.stdout + result.stderr)

    def test_commit_msg_blocks_personal_data_but_allows_coauthor(self):
        (self.clone / 'a.txt').write_text('ok\n')
        self.git(self.clone, 'add', 'a.txt')
        result = self.commit(self.clone, 'feat: x\n\nmail ' + 'bob' + '@' + 'corp-sample.co.jp\n')
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn('<commit message>:3: email', output)
        self.assertIn('[commit-msg]', output)
        result = self.commit(self.clone, 'feat: x\n\nCo-Authored-By: Claude <noreply' + '@anthropic.com>\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def poison_python(self):
        """PATH の python3 を、呼ばれたら印を残すだけの偽物にする（post-merge が Python を呼ばないことの確認用）。"""
        fake = self.base / 'poison'
        fake.mkdir()
        self.python_called = self.base / 'python-called'
        script = f'#!/bin/sh\ntouch "{self.python_called}"\nexit 1\n'
        for name in ('python3', 'python'):
            (fake / name).write_text(script)
            (fake / name).chmod(0o755)
        self.env['PATH'] = f'{fake}{os.pathsep}{self.env["PATH"]}'

    def test_post_merge_without_config_suggests_setup(self):
        self.poison_python()
        self.origin_commit('skills/new-skill/SKILL.md', 'x\n')
        out = self.pull()
        self.assertIn('skills/new-skill/SKILL.md', out)
        self.assertIn(SETUP_ENTRY, out)
        self.assertFalse(self.python_called.exists())

    def test_post_merge_with_config_only_prints_commands(self):
        self.write_config()
        self.poison_python()
        self.origin_commit('instructions/core.md', 'changed\n')
        out = self.pull()
        self.assertFalse(self.python_called.exists(), 'post-merge must not run Python')
        self.assertIn('instructions/core.md', out)
        root = self.clone.resolve().as_posix()  # git は Windows でも / 区切りでルートを返す
        self.assertIn(APPLY_ENTRY, out)
        self.assertIn(f"'{root}/{Path(APPLY_ENTRY).name}'", out)
        self.assertNotIn('install.py', out)
        self.assertNotIn('--apply', out)
        self.assertNotIn('DRY-RUN:', out)
        self.assertFalse((self.home / '.claude').exists(), 'post-merge must not write HOME')

    @unittest.skipUnless(shutil.which('dash'), 'dash not installed')
    @unittest.skipIf(WINDOWS, 'Windows では \\ がパスの区切りでディレクトリ名に使えない')
    def test_post_merge_keeps_backslash_in_paths_under_dash(self):
        odd = self.base / 'with\\tback'
        self.git(self.base, 'clone', '-q', str(self.origin), str(odd))
        (odd / 'config.local.json').write_text('{}')
        self.origin_commit('agents/y.md', 'y\n')
        self.git(odd, '-c', 'core.hooksPath=/dev/null', 'pull', '-q', '--ff-only')
        result = subprocess.run(['dash', str(odd / '.githooks/post-merge')], cwd=odd, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertIn(f"'{odd.resolve()}/apply.sh'", result.stdout)

    def test_post_merge_with_placeholder_aws_asks_for_edit(self):
        example = json.loads((self.clone / 'config.example.json').read_text())
        self.write_config(aws=example['aws'])
        self.origin_commit('agents/x.md', 'x\n')
        out = self.pull()
        self.assertIn('견본 값', out)
        self.assertNotIn('--apply', out)

    def test_post_merge_ignores_unrelated_changes(self):
        self.origin_commit('README.md', 'docs only\n')
        out = self.pull()
        self.assertNotIn('ai-harness', out)

    NOTICE = '[ai-harness] 설치본 반영 필요: ' + ('apply.cmd' if WINDOWS else './apply.sh')

    def test_post_commit_notifies_only_for_install_paths(self):
        cases = [('hooks/new_hook.py', True), ('tools/aws/new.sh', True), ('skills/s/SKILL.md', True),
                 ('README.md', False), ('tools/privacy_note.md', False), ('setup_notes.txt', False)]
        for name, expected in cases:
            with self.subTest(name=name):
                path = self.clone / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'{name}\n')
                self.git(self.clone, 'add', '--', name)
                result = self.commit(self.clone, 'change ' + name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                output = result.stdout + result.stderr
                if expected:
                    self.assertIn(self.NOTICE, output)
                else:
                    self.assertNotIn('ai-harness', output)
        self.assertFalse((self.home / '.claude').exists(), 'post-commit must not write HOME')

    def run_post_commit(self, cwd):
        return subprocess.run([SH, str(self.clone / '.githooks/post-commit')], cwd=cwd, env=self.env,
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_post_commit_handles_root_and_merge_commits_without_python(self):
        self.poison_python()
        fresh = self.base / 'fresh'
        fresh.mkdir()
        self.git(fresh, 'init', '-q', '-b', 'main')
        self.git(fresh, 'config', 'user.name', 'A')
        self.git(fresh, 'config', 'user.email', 'a@example.com')
        plain = ['-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false']
        # 最初のコミット（親なし）でも検出する
        (fresh / 'agents').mkdir()
        (fresh / 'agents/a.md').write_text('a\n')
        self.git(fresh, 'add', '-A')
        self.git(fresh, *plain, 'commit', '-qm', 'root')
        result = self.run_post_commit(fresh)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, self.NOTICE))
        # マージコミットは第 1 親との差分で判定する（相手側ブランチの設置関連変更を検出）
        self.git(fresh, 'checkout', '-q', '-b', 'side')
        (fresh / 'install').mkdir()
        (fresh / 'install/x.py').write_text('x\n')
        self.git(fresh, 'add', '-A')
        self.git(fresh, *plain, 'commit', '-qm', 'side')
        self.git(fresh, 'checkout', '-q', 'main')
        (fresh / 'README.md').write_text('r\n')
        self.git(fresh, 'add', '-A')
        self.git(fresh, *plain, 'commit', '-qm', 'docs')
        self.git(fresh, *plain, 'merge', '-q', '--no-ff', '--no-edit', 'side')
        result = self.run_post_commit(fresh)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, self.NOTICE))
        # 設置関連の変更がない通常コミット
        (fresh / 'README.md').write_text('r2\n')
        self.git(fresh, 'add', '-A')
        self.git(fresh, *plain, 'commit', '-qm', 'docs2')
        result = self.run_post_commit(fresh)
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        # Git 管理外でも失敗しない
        outside = self.base / 'outside'
        outside.mkdir()
        result = self.run_post_commit(outside)
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        self.assertFalse(self.python_called.exists(), 'post-commit must not run Python')


@POSIX_SCRIPTS
class ApplyScriptTests(Fixture):
    """apply.sh。スタブ install.py で、dry-run 要約の後に --apply が端末から呼ばれることを確かめる。"""

    def setUp(self):
        super().setUp()
        self.use_stub_install()

    def apply(self, answers=(), *args):
        return run_pty([str(self.clone / 'apply.sh'), *args], list(answers), self.base, self.env)

    def record(self):
        return json.loads(self.marker.read_text())

    def assert_home_untouched(self):
        for name in ('.claude', '.claude.json', '.codex', '.agents', '.ai-harness'):
            self.assertFalse((self.home / name).exists(), name)

    def test_missing_config_suggests_setup(self):
        code, out = self.apply()
        self.assertEqual(code, 1, out)
        self.assertIn('./setup.sh', out)
        self.assertFalse(self.marker.exists())

    def test_non_tty_is_refused(self):
        self.write_config()
        result = subprocess.run([str(self.clone / 'apply.sh')], cwd=self.base, env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn('터미널', result.stderr)
        self.assertFalse(self.marker.exists())

    def test_windows_is_refused(self):
        self.write_config()
        fake = self.base / 'fakebin'
        fake.mkdir()
        (fake / 'uname').write_text('#!/bin/sh\necho MSYS_NT-10.0\n')
        (fake / 'uname').chmod(0o755)
        self.env['PATH'] = f'{fake}:{self.env["PATH"]}'
        code, out = self.apply()
        self.assertEqual(code, 1, out)
        self.assertIn('Windows', out)
        self.assertFalse(self.marker.exists())

    def test_unknown_option_is_rejected(self):
        self.write_config()
        code, out = self.apply((), '--yes')
        self.assertEqual(code, 2, out)
        self.assertIn('usage', out)
        self.assertFalse(self.marker.exists())

    def test_placeholder_aws_asks_for_edit(self):
        example = json.loads((self.clone / 'config.example.json').read_text())
        self.write_config(aws=example['aws'])
        code, out = self.apply()
        self.assertEqual(code, 1, out)
        self.assertIn('견본 값', out)
        self.assertNotIn('DRY-RUN', out)
        self.assertFalse(self.marker.exists())

    def test_no_change_reports_up_to_date(self):
        self.write_config()
        self.env['STUB_NO_CHANGE'] = '1'
        code, out = self.apply()
        self.assertEqual(code, 0, out)
        self.assertIn('이미 최신', out)
        self.assertNotIn('/stub/target', out)
        self.assertFalse(self.marker.exists())

    def test_runs_install_with_apply_on_terminal_after_dry_run(self):
        self.write_config()
        code, out = self.apply()
        self.assertEqual(code, 0, out)
        self.assertIn('AWS 모듈(--with-aws): off', out)
        self.assertIn('CREATE /stub/target', out)
        self.assertLess(out.index('DRY-RUN: 1 file(s)'), out.index('Claude Code·Codex'))
        record = self.record()
        self.assertTrue(record['tty'])
        self.assertEqual(record['argv'], ['--config', str(self.clone.resolve() / 'config.local.json'), '--apply'])
        self.assertEqual(record['exe'], BASE_PYTHON)
        self.assert_home_untouched()

    def test_runs_from_other_cwd_with_aws(self):
        self.write_config(aws={'profile': 'p', 'roles': {'DEV': 'arn:aws:iam::' + FAKE_ACCOUNT + ':role/r'},
                               'mfa_serial': 'arn:aws:iam::' + FAKE_ACCOUNT + ':mfa/d'})
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        code, out = run_pty([str(self.clone / 'apply.sh')], [], elsewhere, self.env)
        self.assertEqual(code, 0, out)
        self.assertIn('AWS 모듈(--with-aws): on', out)
        self.assertEqual(self.record()['argv'], ['--config', str(self.clone.resolve() / 'config.local.json'),
                                                 '--with-aws', '--apply'])
        self.assert_home_untouched()

    @unittest.skipUnless(HAS_TOMLLIB, 'system Python detection needs Python 3.11+')
    def test_null_python_uses_system_python_outside_venv(self):
        # PATH の先頭に venv の python3 を置いても、install.py は venv ではない python3 で動かす
        venv = self.base / 'venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text('home = /nowhere\n')
        (venv / 'bin/python3').symlink_to(BASE_PYTHON)
        self.env['PATH'] = f'{venv / "bin"}:{self.env["PATH"]}'
        self.write_config(python=None)
        code, out = self.apply()
        self.assertEqual(code, 0, out)
        self.assertEqual(Path(self.record()['exe']).parent, self.bin)


class HelperTests(Fixture):
    def helper(self, *args):
        return subprocess.run([sys.executable, str(self.clone / 'tools/harness_setup.py'), *args], cwd=self.clone,
                              env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=60)

    def test_write_config_does_not_overwrite(self):
        args = ['write-config', '--project-root', '~/p', '--response-language', 'Korean', '--comment-language', 'Japanese']
        self.assertEqual(self.helper(*args).returncode, 0)
        result = self.helper(*args[:2], '/other', *args[3:])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.config()['project_root'], '~/p')

    def test_aws_mode_and_python_of(self):
        self.write_config(python='~/py/bin/python3')
        self.assertEqual(self.helper('aws-mode').stdout.strip(), 'off')
        self.assertEqual(self.helper('python-of').stdout.strip(), str(self.home / 'py/bin/python3'))
        (self.clone / 'config.local.json').unlink()
        self.write_config(aws={'roles': {'DEV': 'r'}, 'mfa_serial': 'm'})
        self.assertEqual(self.helper('aws-mode').stdout.strip(), 'on')
        self.assertIn('--with-aws', self.helper('apply-command').stdout)

    def test_aws_mode_errors_and_placeholder_apply_command(self):
        for extra in ({'aws': None}, {'aws': 'yes'}, {'AWS': {'roles': {}}}):
            with self.subTest(extra=extra):
                self.write_config(**extra)
                result = self.helper('aws-mode')
                self.assertEqual(result.returncode, 1)
                self.assertIn('ERROR', result.stderr)
        example = json.loads((self.clone / 'config.example.json').read_text())
        self.write_config(aws=example['aws'])
        self.assertEqual(self.helper('aws-mode').stdout.strip(), 'placeholder')
        result = self.helper('apply-command')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('견본 값', result.stderr)

    def test_dry_run_without_config_fails(self):
        result = self.helper('dry-run')
        self.assertEqual(result.returncode, 1)
        self.assertIn('setup.cmd' if WINDOWS else 'setup.sh', result.stderr)

    def test_dry_run_reports_no_change_with_exit_3(self):
        self.write_config()
        stub = 'print("SAME /a")\nprint("DRY-RUN: 0 file(s) would change; nothing was written.")\n'
        (self.clone / 'install/install.py').write_text(stub)
        result = self.helper('dry-run')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('SAME 1', result.stdout)

    def test_apply_command_quoting(self):
        self.write_config(python=str(self.home / 'Python 312' / 'python.exe'))
        command = self.helper('apply-command').stdout.strip()
        if WINDOWS:
            # PowerShell に貼り付けて動く形（& 'パス' ... --apply）
            self.assertTrue(command.startswith("& '"), command)
            self.assertIn("python.exe' '", command)
        else:
            self.assertIn("'" + str(self.home / 'Python 312' / 'python.exe') + "'", command)
        self.assertTrue(command.endswith('--apply'), command)


@unittest.skipUnless(HAS_TOMLLIB, 'install.py dry-run needs Python 3.11+')
class WindowsSetupTests(Fixture):
    """setup.cmd・apply.cmd が呼ぶ setup-windows・apply-windows。端末入力は stdin を端末扱いにする起動スクリプトで再現する
    （Python だけで動くため全 OS で実行する）。"""

    def run_helper(self, *args, answers=None):
        script = [str(self.clone / 'tools/harness_setup.py'), *args]
        command = [BASE_PYTHON, *script] if answers is None else [BASE_PYTHON, '-c', TTY_SHIM, *script]
        return subprocess.run(command, cwd=self.base, env=self.env, input=None if answers is None else
                              ''.join(a + '\n' for a in answers), stdin=subprocess.DEVNULL if answers is None else None,
                              capture_output=True, text=True, encoding='utf-8', timeout=120)

    def test_non_terminal_is_refused_without_changes(self):
        for args in (['setup-windows'], ['apply-windows']):
            with self.subTest(args=args):
                result = self.run_helper(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn('터미널', result.stderr)
        self.assertEqual(self.local('core.hooksPath'), '')
        self.assertFalse((self.clone / 'config.local.json').exists())

    def test_first_run_configures_repo_and_stops_before_apply(self):
        self.use_stub_install()
        project = str(self.home / 'work')
        answers = ['Tester', 'tester@example.com', project, BASE_PYTHON, '', '']
        result = self.run_helper('setup-windows', '--no-apply', answers=answers)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.local('user.name'), self.local('user.email'), self.local('core.hooksPath')),
                         ('Tester', 'tester@example.com', '.githooks'))
        config = self.config()
        self.assertEqual((config['project_root'], config['python']), (project, BASE_PYTHON))
        self.assertNotIn('aws', config)
        root = self.clone.resolve()
        self.assertIn(str(root / 'setup.cmd'), config['extra_protected_paths'])
        self.assertIn('--no-apply', result.stdout)
        self.assertFalse(self.marker.exists())
        # 再実行は済んだ段階を飛ばし、質問しない
        again = self.run_helper('setup-windows', '--no-apply', answers=[])
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn('건너뜁니다', again.stdout)

    def test_apply_runs_install_after_confirmation(self):
        self.git(self.clone, 'config', '--local', 'user.name', 'A')
        self.git(self.clone, 'config', '--local', 'user.email', 'a@example.com')
        self.git(self.clone, 'config', '--local', 'core.hooksPath', '.githooks')
        self.use_stub_install()
        self.write_config()
        declined = self.run_helper('setup-windows', answers=['n'])
        self.assertEqual(declined.returncode, 0, declined.stdout + declined.stderr)
        self.assertFalse(self.marker.exists())
        result = self.run_helper('apply-windows', answers=[])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(self.marker.read_text(encoding='utf-8'))
        self.assertEqual(record['argv'], ['--config', str(self.clone.resolve() / 'config.local.json'), '--apply'])

    def test_aws_section_is_refused_on_this_path(self):
        self.write_config(aws={'roles': {'DEV': 'r'}, 'mfa_serial': 'm'})
        result = self.run_helper('apply-windows', answers=[])
        self.assertEqual(result.returncode, 1)
        self.assertIn('AWS', result.stderr)


if __name__ == '__main__':
    unittest.main()
