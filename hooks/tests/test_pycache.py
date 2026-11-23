import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_env as env  # noqa: E402

ROOT = env.HOOKS
g = env.load(env.GUARD, 'cache_guard')
if hasattr(g, 'load_proposal'):
    g = g.load_proposal()


class PythonCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cache = self.root / '__pycache__'
        self.cache.mkdir()
        self.pyc = self.cache / 'module.cpython-314.pyc'
        self.pyc.write_bytes(b'cache')
        self.normal = self.root / 'data.txt'
        self.normal.write_text('keep', encoding='utf-8')

    def command(self, text, expected=None, cwd=None):
        # 一時例外を模擬的に外してキャッシュ独自の判定を検証する。
        with patch.object(g, 'is_temp', return_value=False):
            result = g.check_bash(text, cwd=str(cwd or self.root))
        self.assertEqual(result[0] if result else None, expected, text)

    def test_directory_file_empty_and_globs(self):
        self.assertTrue(g.is_python_cache(str(self.cache)))
        self.assertTrue(g.is_python_cache(str(self.pyc)))
        empty = self.root / 'space name' / '__pycache__'
        empty.mkdir(parents=True)
        self.assertTrue(g.is_python_cache(str(empty)))
        for target in (str(self.cache), str(self.pyc), str(empty), '__pycache__', '__pycache__/*.pyc', '__pycache__/*'):
            self.command('rm -rf ' + shlex.quote(target))
        self.command('unlink ' + shlex.quote(str(self.pyc)))
        self.command('rmdir ' + shlex.quote(str(empty)))

    def test_delete_options_do_not_extend_cache_scope(self):
        empty = self.root / 'parent' / '__pycache__'
        empty.mkdir(parents=True)
        for options in ('-p', '--parents', '-pv', '-vp', '--unknown', '-z'):
            self.command('rmdir ' + options + ' ' + shlex.quote(str(empty)), 'ask')
        for options in ('--no-preserve-root', '--unknown', '-z'):
            self.command('rm ' + options + ' __pycache__', 'ask')
        self.command('unlink --unknown ' + shlex.quote(str(self.pyc)), 'ask')
        self.command('rm -fr -- __pycache__')
        self.command('rmdir -v ' + shlex.quote(str(empty)))
        self.command('unlink -- ' + shlex.quote(str(self.pyc)))
        # 親削除は一時例外でも許可しない。
        with patch.object(g, 'is_temp', return_value=True):
            self.assertEqual(g.check_bash('rmdir -p ' + str(empty))[0], 'ask')

    def test_noncache_mixed_and_compound_commands(self):
        for target in (str(self.normal), str(self.root), '$CACHE', '**/__pycache__', './*/__pycache__', '__pycache__/*.txt'):
            self.command('rm -rf ' + shlex.quote(target), 'ask')
        self.command('rm -rf __pycache__ data.txt', 'ask')
        self.command('rm -rf __pycache__ && rm data.txt', 'ask')
        self.command('rm -rf __pycache__; unlink data.txt', 'ask')
        self.command("python3 -c 'import shutil; shutil.rmtree(\"__pycache__\")'", 'ask')

    def test_cache_name_does_not_authorize_data(self):
        (self.cache / 'business.json').write_text('keep', encoding='utf-8')
        self.assertFalse(g.is_python_cache(str(self.cache)))
        self.assertFalse(g.is_python_cache(str(self.pyc)))
        self.command('rm -rf __pycache__', 'ask')
        (self.cache / 'business.json').unlink()
        (self.cache / 'nested').mkdir()
        self.assertFalse(g.is_python_cache(str(self.cache)))
        self.command('rm -rf __pycache__', 'ask')

    @env.requires_symlinks
    def test_symlink_files_directories_parents_and_dotdot(self):
        link = self.cache / 'link.pyc'
        link.symlink_to(self.normal)
        self.assertFalse(g.is_python_cache(str(link)))
        self.assertFalse(g.is_python_cache(str(self.cache)))
        link.unlink()
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(g.is_python_cache(str(alias / '__pycache__')))
        self.assertFalse(g.is_python_cache(str(alias / '..' / self.root.name / '__pycache__')))
        self.command('rm -rf alias/__pycache__', 'ask')
        bad = self.root / 'linked' / '__pycache__'
        bad.parent.mkdir()
        bad.symlink_to(self.cache, target_is_directory=True)
        self.assertFalse(g.is_python_cache(str(bad)))

    def test_relative_cwd_and_cd(self):
        nested = self.root / 'nested'
        nested.mkdir()
        empty = nested / '__pycache__'
        empty.mkdir()
        self.command('cd nested && rm -rf __pycache__')
        self.command('cd nested && rm -rf __pycache__; rm outside.txt', 'ask')
        self.command("sh -c 'rm -rf __pycache__'", cwd=nested)
        self.command('rm -rf __pycache__', cwd=nested)
        for command in ('cd && rm -rf __pycache__', 'cd -P nested && rm -rf __pycache__',
                        'cd -- nested && rm -rf __pycache__', 'pushd nested && rm -rf __pycache__',
                        'cd - && rm -rf __pycache__', 'cd nested/.. && rm -rf __pycache__'):
            self.command(command, 'ask')

    @env.posix_only('Windows はファイル名に改行・CR を使えない')
    def test_normal_n_names_and_control_characters(self):
        name = self.root / 'normal-name' / '__pycache__'
        name.mkdir(parents=True)
        self.assertTrue(g.is_python_cache(str(name)))
        for control in ('\n', '\r'):
            parent = self.root / ('name' + control)
            (parent / '__pycache__').mkdir(parents=True)
            self.assertFalse(g.is_python_cache(str(parent / '__pycache__')))

    def find(self, roots=None, prune=True, extra=''):
        return 'find ' + (roots or shlex.quote(str(self.root))) + ' -type d -name __pycache__ ' + ('-prune ' if prune else '') + '-exec rm -rf {} + ' + extra

    def test_find_common_patterns_roots_and_cd(self):
        self.command(self.find())
        self.command(self.find(prune=False))
        self.command(self.find().replace('-type d -name __pycache__', '-name __pycache__ -type d'))
        other = self.root / 'other'
        other.mkdir()
        (other / '__pycache__').mkdir()
        self.command(self.find(shlex.quote(str(self.root)) + ' ' + shlex.quote(str(other))))
        self.command('cd other && ' + self.find('.'))
        if env.SYMLINKS:
            (self.root / 'escape').symlink_to('/Users', target_is_directory=True)
            self.command(self.find())

    def test_find_rejects_follow_extra_actions_and_data(self):
        for command in (self.find(extra='-exec rm -rf data.txt +'), self.find(extra='-delete'),
                        self.find().replace('find ', 'find -L ', 1), self.find().replace('find ', 'find -H ', 1),
                        self.find().replace('-prune ', '-follow '), self.find(extra='&& rm data.txt'),
                        self.find().replace('-name __pycache__', '-name "*"'),
                        self.find().replace('rm -rf {} +', 'sh -c "rm -rf /Users/fake" {} +'),
                        self.find('$ROOT'), self.find(extra='-o -exec rm -rf {} +')):
            self.command(command, 'ask')
        (self.cache / 'data.txt').write_text('keep', encoding='utf-8')
        self.command(self.find(), 'ask')
        self.command('find /Users/fake -type f -exec stat {} +')

    def test_bounded_scan(self):
        with patch.object(g, 'CACHE_SCAN_LIMIT', 1):
            self.command(self.find(), 'ask')
        with patch.object(g, 'CACHE_SCAN_SECONDS', 0):
            self.assertFalse(g.is_python_cache(str(self.cache)))
            self.command(self.find(), 'ask')

    def test_temp_mixed_and_aws_remain_protected(self):
        command = 'rm -rf /tmp/placeholder ' + str(self.cache)
        # Windows では絶対パス化で cwd のドライブが付く（c:/tmp/placeholder 等）
        with patch.object(g, 'is_temp', side_effect=lambda p: p == '/tmp/placeholder'
                          or (env.WINDOWS and re.fullmatch(r'[a-z]:/tmp/placeholder', p) is not None)):
            self.assertIsNone(g.check_bash(command, cwd=str(self.root)))
        with patch.object(g, 'HOME', str(self.root)):
            aws = self.root / '.aws' / '__pycache__'
            aws.mkdir(parents=True)
            self.assertFalse(g.is_python_cache(str(aws)))
            self.command('rm -rf .aws/__pycache__', 'ask')
        self.command('cat ~/.aws/credentials', 'deny')

    def test_find_and_double_slash_keep_aws_cache_protected(self):
        home = self.root / 'home'
        aws = home / '.aws' / '__pycache__'
        aws.mkdir(parents=True)
        pyc = aws / 'safe-looking.pyc'
        pyc.write_bytes(b'cache')
        with patch.object(g, 'HOME', str(home)):
            for path in (aws, pyc):
                self.assertFalse(g.is_python_cache(str(path)))
                if not env.WINDOWS:  # 先頭 // は POSIX 固有の書き方
                    self.assertFalse(g.is_python_cache('/' + str(path)))
            for root in (home, self.root):
                self.assertFalse(g.find_python_cache(g.tokens(self.find(shlex.quote(str(root)))), str(self.root)))
                self.command(self.find(shlex.quote(str(root))), 'ask')
            if not env.WINDOWS:
                self.command(self.find('/' + str(home)), 'ask')
        if env.WINDOWS:
            return
        # 保護対象以外では先頭 slash 正規化によってリンク確認が失われない。
        self.assertTrue(g.is_python_cache('/' + str(self.cache)))
        if not env.SYMLINKS:
            return
        alias = self.root / 'double-slash-link'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(g.is_python_cache('/' + str(alias / '__pycache__')))

    def invoke_patch(self, operation, target, runtime):
        payload = {'tool_name': 'apply_patch', 'tool_input': {'command': f'*** {operation}: {target}\n'}, 'cwd': str(self.root)}
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'guard.py')] + (['--codex'] if runtime == 'codex' else []),
                                input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True, timeout=5, env=env.ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout else None

    def test_patch_both_runtimes_cache_delete_and_write_protection(self):
        # 保護ディレクトリを模擬し、削除だけの例外を両 runtime で検証。
        protected = self.root / 'ai-harness' / 'hooks' / '__pycache__'
        protected.mkdir(parents=True)
        pyc = protected / 'guard.pyc'
        pyc.write_bytes(b'cache')
        # 子プロセスの PROJECT を fixture に差し替えるため入力ハーネスを使う。
        code = ('import runpy, sys; ns=runpy.run_path(sys.argv[1], run_name="proposal_test"); '
                'ns=ns["load_proposal"]().__dict__ if "load_proposal" in ns else ns; '
                'ns["main"].__globals__["PROJECT"]=sys.argv[2]; '
                'ns["main"].__globals__["PROTECTED_WRITE"]=[sys.argv[2]+"/ai-harness/hooks/"]; '
                'ns["main"].__globals__["is_temp"]=lambda p: False; ns["main"]()')
        for runtime in ('claude', 'codex'):
            for operation, target, expected in (('Delete File', pyc, None), ('Delete File', protected, 'ask'),
                                                  ('Update File', pyc, 'ask'), ('Add File', pyc, 'ask'),
                                                  ('Move to', pyc, 'ask'), ('Delete File', self.normal, 'ask')):
                payload = {'tool_name': 'apply_patch', 'tool_input': {'command': f'*** {operation}: {target}\n'}, 'cwd': str(self.root)}
                result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT / 'guard.py'), str(self.root)] +
                                        (['--codex'] if runtime == 'codex' else []), input=json.dumps(payload), text=True,
                                        encoding='utf-8', capture_output=True, timeout=5, env=env.ENV)
                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] if result.stdout else None
                self.assertEqual(decision, 'deny' if runtime == 'codex' and expected == 'ask' else expected)
            self.assertIsNone(self.invoke_patch('Delete File', self.pyc, runtime))


if __name__ == '__main__':
    unittest.main()
