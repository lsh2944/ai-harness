"""assume_role の設定読み込みと credentials 書き換えの検証（AWS は呼ばない）。"""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('assume_role', Path(__file__).with_name('assume_role.py'))
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)

ROLE = 'arn:aws:iam::123456789012:role/example-read-only'
MFA = 'arn:aws:iam::123456789012:mfa/example-device'


class ConfigTests(unittest.TestCase):
    def write(self, data):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(data if isinstance(data, str) else json.dumps(data))
        handle.close()
        return handle.name

    def test_valid_config(self):
        path = self.write({'aws': {'profile': 'ai-readonly', 'roles': {'DEV': ROLE}, 'mfa_serial': MFA}})
        self.assertEqual(ar.load_config(path), ({'DEV': ROLE}, MFA, 'ai-readonly'))

    def test_invalid_configs_are_refused(self):
        base = {'profile': 'ai-readonly', 'roles': {'DEV': ROLE}, 'mfa_serial': MFA}
        cases = {
            'broken json': '{',
            'no aws': {},
            'empty roles': {'aws': {**base, 'roles': {}}},
            'bad role arn': {'aws': {**base, 'roles': {'DEV': 'arn:aws:iam::1:role/x'}}},
            'bad env name': {'aws': {**base, 'roles': {'DEV;rm': ROLE}}},
            'bad mfa': {'aws': {**base, 'mfa_serial': 'lee-phone'}},
            'default profile': {'aws': {**base, 'profile': 'default'}},
            'bad profile': {'aws': {**base, 'profile': 'a b'}},
        }
        for label, data in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                ar.load_config(self.write(data))

    def test_main_reports_config_error_without_calling_aws(self):
        stderr = io.StringIO()
        with patch.object(ar, 'DEFAULT_CONFIG', Path('/nonexistent/config.json')), \
                patch.object(ar, 'assume_role', side_effect=AssertionError('must not call AWS')), \
                patch('sys.stderr', stderr):
            self.assertEqual(ar.main(), 1)
        self.assertIn('Cannot read harness config', stderr.getvalue())

    def test_config_cannot_be_redirected_by_environment(self):
        # 偽の roles で書き込み権限の資格情報を読み取り専用プロファイルへ書けないよう、環境変数では差し替えない
        fake = self.write({'aws': {'profile': 'ai-readonly', 'roles': {'DEV': ROLE}, 'mfa_serial': MFA}})
        stderr = io.StringIO()
        with patch.dict(os.environ, {'HARNESS_CONFIG': fake}), \
                patch.object(ar, 'DEFAULT_CONFIG', Path('/nonexistent/config.json')), \
                patch.object(ar, 'assume_role', side_effect=AssertionError('must not call AWS')), \
                patch('sys.stderr', stderr):
            self.assertEqual(ar.main(), 1)
        self.assertIn(str(Path('/nonexistent/config.json')), stderr.getvalue())  # Windows は \ 区切りで表示される
        self.assertEqual(ar.DEFAULT_CONFIG, Path(ar.__file__).resolve().parents[2] / 'config.local.json')

    def test_update_profile_keeps_other_sections(self):
        creds = {'aws_access_key_id': 'A' * 20, 'aws_secret_access_key': 'b' * 40, 'aws_session_token': 'c' * 10}
        with patch.object(ar, 'PROFILE', 'ai-readonly'):
            raw = '[default]\naws_access_key_id = keep\n\n[ai-readonly]\naws_access_key_id = old\nregion = x\n'
            updated = ar.update_profile(raw, creds)
        self.assertIn('[default]\naws_access_key_id = keep\n', updated)
        self.assertIn('aws_access_key_id = ' + 'A' * 20, updated)
        self.assertIn('region = x', updated)
        self.assertNotIn('= old', updated)


if __name__ == '__main__':
    unittest.main()
