#!/usr/bin/env python3
"""setup.sh と .githooks/post-merge の補助（標準ライブラリのみ、Python 3.8 以上で動く）。

サブコマンド（cwd はリポジトリのどこでもよい。パスはリポジトリ直下からの相対で解決する）:
  system-python                 venv ではない Python 3.11 以上（tomllib あり）を探して表示。なければ終了コード 1
  example-field KEY             config.example.json の最上位キーの値を表示（言語の既定値用）
  write-config ...              config.local.json を新規作成（既存なら上書きせず終了コード 1、権限 0600）
  aws-mode                      config.local.json の aws 節: off / on / placeholder（見本の値のまま）
  python-of                     install.py を動かすインタプリタ（設定の python、なければ PATH の python3）
  apply-command                 利用者が端末で実行する --apply コマンドを表示（実行はしない）
  dry-run                       install.py の dry-run を実行して要約を表示（書き込みなし）
                                終了コード: 0=変更予定あり, 3=変更なし, 1=エラー
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = 'config.local.json'
EXAMPLE = 'config.example.json'
INSTALL = 'install/install.py'
DRY_RUN_TIMEOUT = 120
PROBE_TIMEOUT = 10
# よく使われるシステム Python の場所（PATH の python3 が venv のときの候補）
FIXED_CANDIDATES = ('/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3')
# venv ではなく、install.py が必要とする tomllib（3.11 以上）を持つかを確かめる
PROBE = 'import sys, tomllib; sys.exit(0 if sys.prefix == sys.base_prefix else 1)'
SUMMARY_LINE = re.compile(r'^DRY-RUN: (\d+) file\(s\) would change')


def config_path():
    return ROOT / CONFIG


def load_json(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise SystemExit(f'ERROR: cannot read {path.name}: {type(error).__name__}')
    if not isinstance(data, dict):
        raise SystemExit(f'ERROR: {path.name}: top level must be an object')
    return data


def in_venv(path):
    for candidate in {path, path.resolve()}:
        if (candidate.parent / 'pyvenv.cfg').exists() or (candidate.parent.parent / 'pyvenv.cfg').exists():
            return True
    venv = os.environ.get('VIRTUAL_ENV')
    return bool(venv) and str(path).startswith(venv.rstrip('/') + '/')


def system_python():
    candidates = []
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        if directory:
            candidates.append(os.path.join(directory, 'python3'))
    candidates.extend(FIXED_CANDIDATES)
    seen = set()
    for raw in candidates:
        path = Path(raw)
        if not path.is_absolute() or not os.access(path, os.X_OK) or path.is_dir():
            continue
        resolved = path.resolve()
        # venv 内のリンクは実体が同じでも除外し、同じ実体の別の（venv 外の）パスは候補に残す
        if in_venv(path) or str(resolved).startswith(str(ROOT.resolve()) + '/'):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            result = subprocess.run([str(path), '-I', '-c', PROBE], capture_output=True, timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            # 更新で消えにくいよう、解決前のパス（例: /opt/homebrew/bin/python3）を返す
            return str(path)
    return None


def aws_mode(data):
    section = data.get('aws')
    if not isinstance(section, dict):
        return 'off'
    example = load_json(ROOT / EXAMPLE).get('aws') or {}
    if section.get('roles') == example.get('roles') or section.get('mfa_serial') == example.get('mfa_serial'):
        return 'placeholder'
    return 'on'


def expand(value):
    return str(Path.home()) + value[1:] if value.startswith('~') else value


def python_of(data):
    value = data.get('python')
    if isinstance(value, str) and value.strip():
        return expand(value.strip())
    return shutil.which('python3') or 'python3'


def install_args(data):
    # どの cwd から実行しても同じ意味になるよう絶対パスで渡す
    args = [str(ROOT / INSTALL), '--config', str(config_path())]
    if aws_mode(data) != 'off':
        args.append('--with-aws')
    return args


def cmd_write_config(args):
    path = config_path()
    example = load_json(ROOT / EXAMPLE)
    data = {
        'project_root': args.project_root,
        'memory_dir': example.get('memory_dir'),
        'python': args.python or None,
        'response_language': args.response_language,
        'comment_language': args.comment_language,
        'extra_protected_paths': example.get('extra_protected_paths', []),
    }
    if args.aws:
        section = dict(example.get('aws') or {})
        # 見本の assume_role_cmd は clone 先を ~/ai-harness と仮定しているので、実際の場所に合わせる
        section['assume_role_cmd'] = str(ROOT / 'tools/aws/assume_role.sh')
        data['aws'] = section
    content = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    # 既存ファイル・リンクは上書きしない（O_EXCL）。利用者設定なので 0600 で作る。
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f'ERROR: {CONFIG} already exists; not overwritten', file=sys.stderr)
        return 1
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(content)
    print(f'created {CONFIG}')
    return 0


def cmd_dry_run(_args):
    path = config_path()
    if not path.is_file():
        print(f'ERROR: {CONFIG} not found (run ./setup.sh)', file=sys.stderr)
        return 1
    data = load_json(path)
    command = [python_of(data), *install_args(data)]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=DRY_RUN_TIMEOUT,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f'ERROR: install.py dry-run failed: {type(error).__name__}', file=sys.stderr)
        return 1
    if result.returncode != 0:
        print('ERROR: install.py dry-run failed:', file=sys.stderr)
        for line in (result.stderr.strip() or result.stdout.strip()).splitlines()[-10:]:
            print('  ' + line, file=sys.stderr)
        return 1
    same, pending = 0, None
    print('install.py dry-run (쓰기 없음):')
    for line in result.stdout.splitlines():
        if line.startswith('SAME '):
            same += 1
            continue
        match = SUMMARY_LINE.match(line)
        if match:
            pending = int(match.group(1))
        print('  ' + line)
    if same:
        print(f'  (변경 없음 SAME {same}개 생략)')
    if pending is None:
        print('ERROR: unexpected dry-run output (summary line missing)', file=sys.stderr)
        return 1
    return 0 if pending else 3


def main(argv=None):
    parser = argparse.ArgumentParser(description='ai-harness setup helper', allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('system-python')
    field = sub.add_parser('example-field')
    field.add_argument('key')
    write = sub.add_parser('write-config')
    write.add_argument('--project-root', required=True)
    write.add_argument('--python', default='')
    write.add_argument('--response-language', required=True)
    write.add_argument('--comment-language', required=True)
    write.add_argument('--aws', action='store_true')
    for name in ('aws-mode', 'python-of', 'apply-command', 'dry-run'):
        sub.add_parser(name)
    args = parser.parse_args(argv)

    if args.command == 'system-python':
        found = system_python()
        if not found:
            return 1
        print(found)
        return 0
    if args.command == 'example-field':
        value = load_json(ROOT / EXAMPLE).get(args.key)
        print('' if value is None else value)
        return 0
    if args.command == 'write-config':
        return cmd_write_config(args)
    if args.command == 'dry-run':
        return cmd_dry_run(args)
    data = load_json(config_path())
    if args.command == 'aws-mode':
        print(aws_mode(data))
    elif args.command == 'python-of':
        print(python_of(data))
    else:
        print(' '.join(shlex.quote(a) for a in [python_of(data), *install_args(data), '--apply']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
