#!/usr/bin/env python3
"""Bedrock API キーで Bedrock 推論コマンドを実行するラッパー（キー値は出力しない）。

使い方（この形以外はガードが拒否する）:
  python3 <ハーネス>/tools/aws/bedrock_run.py aws bedrock-runtime converse ...
  python3 <ハーネス>/tools/aws/bedrock_run.py python3 script.py [args]
  python3 <ハーネス>/tools/aws/bedrock_run.py uv run [--with PKG ...] python script.py [args]

キーはユーザーが作成する ~/.aws/bedrock_api_key（権限 600）から読み、AWS_BEARER_TOKEN_BEDROCK に渡す。
AWS_PROFILE は変更しないため、キー認証が使われない場合は読み取り専用の資格情報で失敗する
（他の資格情報へ黙って切り替わらない）。ガード（hooks/guard.py の check_bedrock_run）も同じ許可範囲で判定する。
"""
import os
from pathlib import Path
import re
import subprocess
import sys

KEY_FILE = Path.home() / '.aws' / 'bedrock_api_key'
TOKEN_ENV = 'AWS_BEARER_TOKEN_BEDROCK'
INFER_OPS = {'converse', 'converse-stream', 'invoke-model', 'invoke-model-with-response-stream'}
# 実行ファイルはパスなしの名前だけ（/tmp/x/aws 等の偽物にキーを渡さない）
PROGRAMS = {'aws', 'python', 'python3', 'uv'}
PYTHONS = {'python', 'python3'}
# aws の許可オプション（値を取るもの）。--debug（認証ヘッダーをログ出力）・--endpoint-url・
# --no-verify-ssl・--ca-bundle・--profile 等は含めない（許可リスト方式: 未知のオプションは拒否）
AWS_VALUE_OPTS = {
    '--model-id', '--messages', '--system', '--inference-config', '--tool-config', '--guardrail-config',
    '--additional-model-request-fields', '--additional-model-response-field-paths', '--prompt-variables',
    '--request-metadata', '--performance-config', '--service-tier',
    '--body', '--content-type', '--accept', '--trace', '--guardrail-identifier', '--guardrail-version',
    '--performance-config-latency',
    '--region', '--output', '--query', '--cli-binary-format', '--cli-read-timeout', '--cli-connect-timeout',
}
AWS_FLAG_OPTS = {'--no-cli-pager', '--no-paginate'}
NEGATIVE_NUMBER = re.compile(r'-[0-9]+(\.[0-9]+)?')
# invoke-model 系は出力ファイルを位置引数に 1 つ取る
AWS_OUTFILE_OPS = {'invoke-model', 'invoke-model-with-response-stream'}
# uv run の値を取るオプション（--python/-p は任意のインタプリタを実行できるため不可）
UV_VALUE_OPTS = {'--with', '--with-requirements', '--project', '--directory', '--index-url'}
UV_DENY_OPTS = {'--python', '-p'}
# キーを別の宛先へ送れる設定（エンドポイント・プロキシ・証明書・設定ファイル）は子プロセスに渡さない
STRIP_ENV_EXACT = {'AWS_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                   'AWS_CONFIG_FILE', 'AWS_DATA_PATH', 'UV_PYTHON', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'}
STRIP_ENV_PREFIX = ('AWS_ENDPOINT_URL',)


def _check_python(args):
    """python に渡す引数: 先頭が .py スクリプトであること（-c・-m 等のオプションは不可）。"""
    if not args or args[0].startswith('-') or not args[0].endswith('.py'):
        return 'python は .py スクリプトの直接実行のみ（-c・-m 等は不可）'
    return None


def _check_aws(cmd):
    if len(cmd) < 3 or cmd[1] != 'bedrock-runtime' or cmd[2] not in INFER_OPS:
        return 'aws は bedrock-runtime の推論 4 種（' + '・'.join(sorted(INFER_OPS)) + '）のみ'
    positional = 0
    i = 3
    while i < len(cmd):
        token = cmd[i]
        if token.startswith('-'):
            name = token.split('=', 1)[0]
            if name in AWS_FLAG_OPTS and '=' not in token:
                i += 1
                continue
            if name not in AWS_VALUE_OPTS:
                return f'aws のオプション {name} は許可されていない'
            if '=' in token:
                i += 1
                continue
            # aws CLI は位置に関係なく --debug 等を先に解釈するため、値の位置の '-' 始まり（負数以外）も拒否する
            value = cmd[i + 1] if i + 1 < len(cmd) else None
            if value is None or (value.startswith('-') and not NEGATIVE_NUMBER.fullmatch(value)):
                return f'aws のオプション {name} の値がない（または - で始まる）'
            i += 2
            continue
        positional += 1
        if cmd[2] not in AWS_OUTFILE_OPS or positional > 1:
            return f'aws の位置引数 {token} は許可されていない'
        i += 1
    return None


def validate(cmd):
    """実行してよいコマンドか判定する。拒否理由の文字列、許可なら None。"""
    if not cmd:
        return '実行するコマンドがない'
    prog = cmd[0]
    if prog not in PROGRAMS:
        return f'{prog} は実行できない（パスなしの aws・python・python3・uv のみ）'
    if prog == 'aws':
        return _check_aws(cmd)
    if prog in PYTHONS:
        return _check_python(cmd[1:])
    if len(cmd) < 2 or cmd[1] != 'run':
        return 'uv は uv run のみ'
    i = 2
    while i < len(cmd) and cmd[i].startswith('-'):
        name = cmd[i].split('=', 1)[0]
        if name in UV_DENY_OPTS or cmd[i].startswith('-p'):
            return 'uv run の --python/-p は指定できない'
        i += 1 if '=' in cmd[i] or name not in UV_VALUE_OPTS else 2
    if i >= len(cmd) or cmd[i] not in PYTHONS:
        return 'uv run の後は python スクリプトのみ'
    return _check_python(cmd[i + 1:])


def child_env(base, key):
    """子プロセスの環境: キーを追加し、宛先を変えられる設定を除く。"""
    env = {k: v for k, v in base.items()
           if k.upper() not in STRIP_ENV_EXACT and not k.upper().startswith(STRIP_ENV_PREFIX)}
    env[TOKEN_ENV] = key
    return env


def load_key(path=None):
    """キーファイルを読む。失敗時は (None, 理由)。理由にキー値は含めない。"""
    path = Path(path or KEY_FILE)
    try:
        key = path.read_text(encoding='utf-8').strip()
    except (OSError, ValueError):  # UnicodeDecodeError（ValueError）のメッセージにも中身を出さない
        return None, f'キーファイルを読めない（存在・権限・UTF-8 を確認）: {path}'
    if not key or any(c.isspace() for c in key):
        return None, f'キーファイルの形式が不正（空または空白を含む）: {path}'
    return key, None


def main(argv=None):
    cmd = list(sys.argv[1:] if argv is None else argv)
    reason = validate(cmd)
    if reason:
        print(f'bedrock_run: 拒否: {reason}', file=sys.stderr)
        return 2
    key, error = load_key()
    if error:
        print(f'bedrock_run: {error}', file=sys.stderr)
        return 2
    try:
        return subprocess.run(cmd, env=child_env(os.environ, key)).returncode
    except OSError:
        print(f'bedrock_run: コマンドを実行できない: {cmd[0]}', file=sys.stderr)
        return 127


if __name__ == '__main__':
    sys.exit(main())
