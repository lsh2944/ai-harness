"""tools/privacy_check.py のテスト。一時 git リポジトリ・一時 HOME で実行する。

検出させたい仮の値はこのファイル自体が --all で検出されないよう、実行時に文字列を連結して作る。
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'privacy_check.py'
spec = importlib.util.spec_from_file_location('privacy_check', SCRIPT)
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)

FAKE_ACCOUNT = ''.join(['2109', '8765', '4321'])
FAKE_ARN = 'arn:aws:iam::' + FAKE_ACCOUNT + ':role/' + 'reader'
FAKE_EMAIL = 'alice' + '@' + 'corp-sample.co.jp'
FAKE_HOME_PATH = '/Users/' + 'alice' + '/work'
FAKE_LINUX_HOME = '/home/' + 'bob' + '/src'
FAKE_AKIA = 'AKIA' + 'Z' * 16
FAKE_GHP = 'ghp' + '_' + 'a1' * 15
FAKE_PAT = 'github' + '_pat_' + 'B' * 30
FAKE_SK = 'sk' + '-' + 'c3' * 12
FAKE_KEY_HEADER = '-----BEGIN ' + 'RSA PRIVATE KEY' + '-----'


def kinds(text, **kwargs):
    return [kind for _, kind, _ in privacy.scan_text(text, **kwargs)]


class ScanTextTests(unittest.TestCase):
    def test_detects_each_generic_kind(self):
        cases = {
            'AWS account ID (12 digits)': f'account = "{FAKE_ACCOUNT}"',
            'IAM ARN': f'role: {FAKE_ARN}',
            'email': f'contact {FAKE_EMAIL}',
            'home path': f'cd {FAKE_HOME_PATH}',
            'AWS access key ID': f'key={FAKE_AKIA}',
            'GitHub token': f'token {FAKE_GHP}',
            'GitHub fine-grained token': f'token {FAKE_PAT}',
            'API key (sk-)': f'OPENAI={FAKE_SK}',
            'private key header': FAKE_KEY_HEADER,
        }
        for kind, line in cases.items():
            with self.subTest(kind=kind):
                self.assertIn(kind, kinds(line))
        self.assertEqual(kinds(f'x {FAKE_LINUX_HOME}'), ['home path'])

    def test_arn_is_reported_once(self):
        self.assertEqual(kinds(FAKE_ARN), ['IAM ARN'])

    def test_allowed_placeholders_are_not_reported(self):
        example_id = '1234' + '5678' + '9012'
        lines = [
            'arn:aws:iam::' + example_id + ':role/example-read-only',
            'id ' + '0' * 12 + ' and ' + '7' * 12,
            'Co-Authored-By: Claude <noreply' + '@anthropic.com>',
            'someone' + '@users.noreply.github.com',
            'a' + '@example.com b' + '@sub.example.org c' + '@example.invalid',
            '/Users/u/project and /home/x/repo',
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(kinds(line), [])

    def test_twelve_digits_inside_longer_tokens_are_ignored(self):
        for line in ('sha ab' + FAKE_ACCOUNT + 'cd', 'n=' + FAKE_ACCOUNT + '9', 'x' + FAKE_ACCOUNT):
            with self.subTest(line=line):
                self.assertEqual(kinds(line), [])

    def test_non_example_email_domain_is_reported(self):
        # example.net・.test は許可していない
        self.assertEqual(kinds('a' + '@example.net'), ['email'])
        self.assertEqual(kinds('a' + '@corp.test'), ['email'])

    def test_privacy_words_case_insensitive_and_line_numbers(self):
        text = 'first\nThe ACME team\nsecond\n'
        self.assertEqual(privacy.scan_text(text, words=['acme']), [(2, 'privacy_words', 'a***')])

    def test_current_home_string(self):
        home = '/opt/people/' + 'carol'
        self.assertEqual(kinds(f'path={home}/x', home=home), ['current HOME path'])
        self.assertEqual(kinds('path=/opt/people/dave', home=home), [])

    def test_mask_hides_most_of_value(self):
        shown = privacy.mask(FAKE_AKIA)
        self.assertNotIn(FAKE_AKIA, shown)
        self.assertTrue(shown.startswith('AKI') and '***' in shown)
        self.assertEqual(privacy.mask('ab'), '***')


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='privacy-check-test-')
        base = Path(self.temp.name).resolve()
        self.repo = base / 'repo'
        self.home = base / 'home'
        self.repo.mkdir()
        self.home.mkdir()
        self.env = {**os.environ, 'HOME': str(self.home), 'GIT_CONFIG_NOSYSTEM': '1',
                    'XDG_CONFIG_HOME': str(self.home / '.config')}
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *args],
                              cwd=self.repo, env=self.env, capture_output=True, text=True, check=True)

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding='utf-8')
        return path

    def check(self, mode, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), mode], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_staged_reports_location_kind_and_masks_value(self):
        self.write('ok.txt', 'hello\n')
        self.write('docs/bad.md', f'line1\nowner {FAKE_ACCOUNT}\n')
        self.git('add', '.')
        result = self.check('--staged')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('docs/bad.md:2: AWS account ID (12 digits)', result.stdout)
        self.assertNotIn(FAKE_ACCOUNT, result.stdout)
        self.assertNotIn('ok.txt:', result.stdout)

    def test_staged_ignores_unstaged_untracked_and_deleted(self):
        self.write('tracked.txt', 'clean\n')
        self.write('gone.txt', f'{FAKE_EMAIL}\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.git('rm', '-q', 'gone.txt')
        self.write('tracked.txt', f'{FAKE_EMAIL}\n')  # 未ステージの変更
        self.write('untracked.txt', f'{FAKE_EMAIL}\n')
        result = self.check('--staged')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('0 finding(s) in 0 staged file(s)', result.stdout)

    def test_staged_checks_index_content_not_worktree(self):
        self.write('a.txt', f'{FAKE_EMAIL}\n')
        self.git('add', 'a.txt')
        self.write('a.txt', 'cleaned but not staged\n')
        result = self.check('--staged')
        self.assertEqual(result.returncode, 1)
        self.assertIn('a.txt:1: email', result.stdout)

    def test_all_checks_tracked_files_only(self):
        self.write('one.txt', 'fine\n')
        self.write('two.txt', f'x\ny\n{FAKE_HOME_PATH}\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.write('untracked.txt', f'{FAKE_AKIA}\n')
        result = self.check('--all')
        self.assertEqual(result.returncode, 1)
        self.assertIn('two.txt:3: home path', result.stdout)
        self.assertNotIn('untracked.txt', result.stdout)
        self.assertIn('1 finding(s) in 2 tracked file(s)', result.stdout)

    def test_clean_repo_exits_zero(self):
        self.write('readme.md', 'nothing here\n')
        self.git('add', '.')
        self.assertEqual(self.check('--all').returncode, 0)

    def test_file_names_with_spaces_and_non_ascii(self):
        self.write('dir with space/ファイル.txt', f'{FAKE_GHP}\n')
        self.git('add', '.')
        result = self.check('--staged')
        self.assertEqual(result.returncode, 1)
        self.assertIn('dir with space/ファイル.txt:1: GitHub token', result.stdout)

    def test_binary_and_large_files_are_skipped_with_notice(self):
        self.write('image.bin', b'\x00\x01' + FAKE_AKIA.encode())
        self.write('big.txt', 'a' * (privacy.MAX_BYTES + 1))
        self.git('add', '.')
        result = self.check('--staged')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('SKIP image.bin: binary', result.stdout)
        self.assertIn('SKIP big.txt: larger than', result.stdout)

    def test_privacy_words_from_local_config(self):
        self.write('.gitignore', 'config.local.json\n')
        self.write('config.local.json', json.dumps({'project_root': '~/p', 'privacy_words': ['Contoso']}))
        self.write('note.md', 'visit contoso.example soon\n')
        self.git('add', '.gitignore', 'note.md')
        result = self.check('--staged')
        self.assertEqual(result.returncode, 1)
        self.assertIn('note.md:1: privacy_words (C***)', result.stdout)
        # 設定ファイル自体は Git 除外なので検査対象にならない
        self.assertNotIn('config.local.json:', result.stdout)

    def test_missing_key_uses_generic_patterns_only(self):
        self.write('config.local.json', json.dumps({'project_root': '~/p'}))
        self.write('note.md', 'contoso\n')
        self.git('add', 'note.md')
        self.assertEqual(self.check('--staged').returncode, 0)

    def test_invalid_privacy_words_is_an_error(self):
        for value in ('Contoso', [''], [1]):
            with self.subTest(value=value):
                self.write('config.local.json', json.dumps({'privacy_words': value}))
                result = self.check('--all')
                self.assertEqual(result.returncode, 2)
                self.assertIn('privacy_words must be a list', result.stderr)
        self.write('config.local.json', '{broken')
        self.assertEqual(self.check('--all').returncode, 2)

    def test_runs_from_subdirectory(self):
        self.write('sub/deep/file.txt', f'{FAKE_SK}\n')
        self.git('add', '.')
        result = self.check('--staged', cwd=self.repo / 'sub')
        self.assertEqual(result.returncode, 1)
        self.assertIn('sub/deep/file.txt:1: API key (sk-)', result.stdout)

    def test_current_home_path_detected(self):
        self.write('log.txt', f'saved to {self.home}/notes\n')
        self.git('add', '.')
        result = self.check('--staged')
        self.assertEqual(result.returncode, 1)
        self.assertIn('log.txt:1: current HOME path', result.stdout)

    def test_outside_git_repository_is_an_error(self):
        outside = Path(self.temp.name) / 'plain'
        outside.mkdir()
        result = self.check('--all', cwd=outside)
        self.assertEqual(result.returncode, 2)
        self.assertIn('not inside a git repository', result.stderr)

    def test_mode_is_required(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
