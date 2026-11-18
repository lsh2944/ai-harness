"""作業ルート・ハーネス保護・ホーム判定の一般化（固定パスを使わない）の検証。Codex は呼ばない。"""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import co_design_fixture as fx  # noqa: E402

co = fx.co
_cc_spec = importlib.util.spec_from_file_location('cross_check_runtime', Path(__file__).resolve().parents[1] / 'scripts/cross_check.py')
cc = importlib.util.module_from_spec(_cc_spec)
_cc_spec.loader.exec_module(cc)
SKILL = Path(__file__).resolve().parents[1]
PRINT_ROOTS = ('import importlib.util,sys;s=importlib.util.spec_from_file_location("m",sys.argv[1]);'
               'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.PROJECT);print(m.HARNESS_HOME)')


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cross-check-runtime-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = {k: v for k, v in os.environ.items() if k != 'HARNESS_PROJECT_ROOT'}

    def roots(self, script, cwd, env=None):
        result = subprocess.run([sys.executable, '-c', PRINT_ROOTS, str(script)], cwd=cwd, env=env or self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split('\n')[:2]

    def installed_copy(self, config):
        # install.py と同じ配置（スキル直下に harness_config.json）
        skill = self.root / 'skills' / 'cross-check'
        shutil.copytree(SKILL / 'scripts', skill / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        if config is not None:
            (skill / 'harness_config.json').write_text(json.dumps(config))
        return skill / 'scripts'

    def test_cwd_wins_and_env_cannot_override(self):
        cwd = self.root / 'cwd'
        configured = self.root / 'configured'
        override = self.root / 'override'
        for path in (cwd, configured, override):
            path.mkdir()
        for name in ('co_design.py', 'cross_check.py'):
            with self.subTest(script=name):
                bare = self.installed_copy(None)
                self.assertEqual(self.roots(bare / name, cwd)[0], str(cwd))
                shutil.rmtree(self.root / 'skills')
                scripts = self.installed_copy({'version': 1, 'project_root': str(configured), 'harness_home': '/opt/harness'})
                # 実行位置（cwd）が設定の project_root より優先、harness_home は設定から読む
                project, harness = self.roots(scripts / name, cwd)
                self.assertEqual((project, harness), (str(cwd), '/opt/harness'))
                # 環境変数を付けても作業ルートは広がらない
                project, _ = self.roots(scripts / name, cwd, {**self.env, 'HARNESS_PROJECT_ROOT': str(override)})
                self.assertEqual(project, str(cwd))
                shutil.rmtree(self.root / 'skills')

    def test_config_root_is_used_only_when_cwd_is_unavailable(self):
        configured = self.root / 'configured'
        configured.mkdir()
        scripts = self.installed_copy({'version': 1, 'project_root': str(configured), 'harness_home': '/opt/harness'})
        gone = self.root / 'gone'
        gone.mkdir()
        code = ('import os,sys;os.chdir(sys.argv[2]);os.rmdir(sys.argv[2]);' + PRINT_ROOTS)
        result = subprocess.run([sys.executable, '-c', code, str(scripts / 'co_design.py'), str(gone)],
                                capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split('\n')[0], str(configured))

    def test_too_broad_root_is_refused(self):
        home = Path.home().resolve()
        hidden = [home / name for name in ('.aws', '.codex/sessions', '.claude/projects/x', '.agents/skills')] + [home / 'project/.local/x']
        for path in ['/', home, home.parent, '/tmp', '/private/var'] + hidden:
            with self.subTest(path=path):
                self.assertTrue(co.too_broad_root(path))
                self.assertTrue(cc.too_broad_root(path))
                with patch.object(co, 'PROJECT', Path(path)):
                    output, code = co.run(['status', '--session', '20261008T000000Z-abcdef'])
                self.assertEqual((output['status'], code), ('REFUSED', 2))
                self.assertIn('too broad', output['error'])
        self.assertFalse(co.too_broad_root(self.root))
        self.assertFalse(co.too_broad_root(home / 'project'))

    def test_broken_sidecar_falls_back_to_cwd(self):
        scripts = self.installed_copy(None)
        (scripts.parent / 'harness_config.json').write_text('{broken')
        self.assertEqual(self.roots(scripts / 'co_design.py', self.root)[0], str(self.root))

    def test_harness_source_is_not_an_evidence_source(self):
        harness = self.root / 'harness'
        (harness / 'hooks').mkdir(parents=True)
        guard = harness / 'hooks' / 'guard.py'
        guard.write_text('x\n')
        other = self.root / 'notes.txt'
        other.write_text('ok\n')
        protected = [harness / 'hooks', harness / 'install', harness / 'settings', harness / 'config.local.json']
        with patch.object(co, 'HARNESS_PROTECTED', protected):
            with self.assertRaises(co.Refused):
                co.read_source(str(guard), 1000)
            self.assertEqual(co.read_source(str(other), 1000)[0], b'ok\n')

    def test_home_roots_include_login_home_when_home_is_overridden(self):
        login = pwd.getpwuid(os.getuid()).pw_dir.rstrip('/').lower()
        with patch.object(co.Path, 'home', return_value=self.root):
            roots = co.broad_roots()
            self.assertIn(str(self.root).lower(), roots)
            self.assertIn(login, roots)
            self.assertIn(login + '/.claude', roots)
            self.assertTrue(co.independence_hits(f"/bin/zsh -lc 'tree -L 2 {login}'", '20261008T000000Z-abcdef', shell=True))


if __name__ == '__main__':
    unittest.main()
