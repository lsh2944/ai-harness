"""bedrock_run の許可判定とキーの受け渡しの検証（AWS・外部コマンドは呼ばない）。"""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bedrock_run', Path(__file__).with_name('bedrock_run.py'))
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

FAKE_KEY = 'fake-bedrock-key-for-test'
CONVERSE = ['aws', 'bedrock-runtime', 'converse', '--model-id', 'm', '--messages', 'x']

# ガード（hooks/guard.py の check_bedrock_run）の動値テストでも同じ表を使う
ALLOWED = [
    CONVERSE,
    CONVERSE + ['--region', 'ap-northeast-1', '--no-cli-pager', '--inference-config=x'],
    CONVERSE + ['--cli-read-timeout', '-1'],
    ['aws', 'bedrock-runtime', 'converse-stream', '--model-id', 'm', '--messages', 'x'],
    ['aws', 'bedrock-runtime', 'invoke-model', '--model-id', 'm', '--body', 'b', '/tmp/o.json'],
    ['aws', 'bedrock-runtime', 'invoke-model-with-response-stream', '--model-id', 'm', '--body', 'b', '/tmp/o.json'],
    ['python3', 'bench.py', '3'],
    ['python', '/tmp/bench.py'],
    ['uv', 'run', '--with', 'boto3', 'python', 'bench.py', '3'],
    ['uv', 'run', '--with=boto3', 'python3', 'bench.py'],
]
DENIED = [
    [],
    ['aws', 'bedrock-runtime', 'start-async-invoke'],
    ['aws', 'bedrock-runtime', 'apply-guardrail'],
    ['aws', 'bedrock', 'create-provisioned-model-throughput'],
    ['aws', 's3', 'ls'],
    CONVERSE + ['--profile', 'other'],
    CONVERSE + ['--profile=other'],
    CONVERSE + ['--debug'],
    CONVERSE + ['--endpoint-url', 'http://127.0.0.1:9'],
    CONVERSE + ['--endpoint-url=http://127.0.0.1:9'],
    CONVERSE + ['--no-verify-ssl'],
    CONVERSE + ['--ca-bundle', '/tmp/ca.pem'],
    # 値の位置に隠したグローバルオプション（aws CLI は位置に関係なく先に解釈する）
    CONVERSE + ['--additional-model-response-field-paths', '--debug'],
    CONVERSE + ['--system', '--endpoint-url=http://127.0.0.1:9'],
    CONVERSE + ['--system'],
    CONVERSE + ['/tmp/o.json'],
    ['aws', 'bedrock-runtime', 'invoke-model', '--model-id', 'm', '/tmp/a.json', '/tmp/b.json'],
    ['/tmp/x/aws', 'bedrock-runtime', 'converse', '--model-id', 'm'],
    ['/usr/bin/python3', 'bench.py'],
    ['python3.12', 'bench.py'],
    ['python3', '-c', 'print(1)'],
    ['python3', '-m', 'http.server'],
    ['python3', '-u', 'bench.py'],
    ['python3'],
    ['uv', 'pip', 'install', 'x'],
    ['uv', 'run', '--python', '/tmp/x', 'python', 'bench.py'],
    ['uv', 'run', '-p', '3.12', 'python', 'bench.py'],
    ['uv', 'run', '--python=/tmp/x', 'python', 'bench.py'],
    ['uv', 'run', '--with', 'boto3', 'sh', 'x.py'],
    ['uv', 'run', 'python', '-c', 'print(1)'],
    ['printenv'],
    ['env'],
    ['sh', '-c', 'echo x'],
]


class ValidateTests(unittest.TestCase):
    def test_allowed(self):
        for cmd in ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertIsNone(br.validate(cmd))

    def test_denied(self):
        for cmd in DENIED:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(br.validate(cmd))

    def test_child_env_strips_redirect_settings(self):
        base = {'AWS_PROFILE': 'p', 'PATH': '/usr/bin', 'AWS_ENDPOINT_URL': 'x', 'AWS_ENDPOINT_URL_BEDROCK_RUNTIME': 'x',
                'HTTPS_PROXY': 'x', 'https_proxy': 'x', 'ALL_PROXY': 'x', 'AWS_CA_BUNDLE': 'x',
                'REQUESTS_CA_BUNDLE': 'x', 'SSL_CERT_FILE': 'x', 'AWS_CONFIG_FILE': 'x',
                'AWS_DATA_PATH': 'x', 'UV_PYTHON': 'x'}
        env = br.child_env(base, FAKE_KEY)
        self.assertEqual(env, {'AWS_PROFILE': 'p', 'PATH': '/usr/bin', br.TOKEN_ENV: FAKE_KEY})


class MainTests(unittest.TestCase):
    def key_file(self, data):
        handle = tempfile.NamedTemporaryFile('wb', delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(data if isinstance(data, bytes) else data.encode())
        handle.close()
        return handle.name

    def run_main(self, cmd, key_path, side_effect=None):
        err = io.StringIO()
        out = io.StringIO()
        with patch.object(br, 'KEY_FILE', Path(key_path)), \
             patch('sys.stderr', err), patch('sys.stdout', out), \
             patch.object(br.subprocess, 'run', side_effect=side_effect) as run:
            run.return_value.returncode = 0
            code = br.main(cmd)
        return code, run, out.getvalue() + err.getvalue()

    def test_passes_key_in_env_without_printing(self):
        path = self.key_file(FAKE_KEY + '\n')
        with patch.dict(os.environ, {'AWS_PROFILE': 'test-readonly', 'HTTPS_PROXY': 'http://127.0.0.1:9'}):
            code, run, printed = self.run_main(['python3', 'bench.py'], path)
        self.assertEqual(code, 0)
        env = run.call_args.kwargs['env']
        self.assertEqual(env[br.TOKEN_ENV], FAKE_KEY)
        # プロファイルは変更しない（キー認証が効かなければ読み取り専用で失敗させる）
        self.assertEqual(env['AWS_PROFILE'], 'test-readonly')
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertNotIn(FAKE_KEY, printed)

    def test_rejected_command_is_not_run(self):
        path = self.key_file(FAKE_KEY)
        code, run, printed = self.run_main(CONVERSE + ['--debug'], path)
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertNotIn(FAKE_KEY, printed)

    def test_missing_or_invalid_key(self):
        for path in ('/nonexistent-bedrock-key', self.key_file(''), self.key_file('a b'),
                     self.key_file(b'\xff' + FAKE_KEY.encode())):
            with self.subTest(path=path):
                code, run, printed = self.run_main(['python3', 'bench.py'], path)
                self.assertEqual(code, 2)
                run.assert_not_called()
                self.assertNotIn('a b', printed)
                self.assertNotIn(FAKE_KEY, printed)

    def test_command_not_found(self):
        path = self.key_file(FAKE_KEY)
        code, _, printed = self.run_main(['python3', 'bench.py'], path, side_effect=FileNotFoundError(FAKE_KEY))
        self.assertEqual(code, 127)
        self.assertNotIn(FAKE_KEY, printed)


if __name__ == '__main__':
    unittest.main()
