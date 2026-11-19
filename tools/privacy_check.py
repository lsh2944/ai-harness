#!/usr/bin/env python3
"""追跡ファイル・コミット予定の内容に個人情報・秘密値の形が含まれていないか検査する（標準ライブラリのみ）。

使い方（cwd はこのリポジトリ内）:
  python3 tools/privacy_check.py --staged   # index の追加・変更ファイル（pre-commit hook が使う）
  python3 tools/privacy_check.py --all      # 追跡ファイル全体（index の内容）

検出時は「ファイル:行: 種類 (一部マスクした値)」だけを出力し、終了コード 1。
エラー（git 失敗・利用者設定の不正）は終了コード 2。
利用者ごとの語（実名・会社ドメイン等）は config.local.json（Git 除外）の任意キー privacy_words（文字列リスト）に置く。
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

MAX_BYTES = 1024 * 1024  # これより大きいファイルは検査せず、飛ばしたことを表示する
GIT_TIMEOUT = 60
CONFIG_NAME = 'config.local.json'

# 許可リストは最小限にする（--all が 0 件になる範囲＋明示的に例示値と決めたもの）。
# AWS アカウント ID: AWS ドキュメントの定番例 123456789012（config.example.json・tools/aws のテスト・CLAUDE.md が使う）と、
# 同じ数字の繰り返し（000000000000 等、明らかな仮値）だけを許可する。
EXAMPLE_ACCOUNT_IDS = {'123456789012'}
# メール: コミット末尾の Co-Authored-By、GitHub の非公開メール、RFC 2606 の例示ドメイン（example.com/.org）。
# example.invalid は RFC 2606 の予約 TLD で、テストの git 作成者・ダミー値（tools/tests・skills/cross-check/tests）が使う。
ALLOWED_EMAILS = {'noreply@anthropic.com'}
ALLOWED_EMAIL_SUFFIXES = ('@users.noreply.github.com',)
ALLOWED_EMAIL_DOMAINS = ('example.com', 'example.org', 'example.invalid')
# ホームパスの仮の名前: u は skills/cross-check/tests が使い、x は CLAUDE.md が仮値の例として挙げている。
ALLOWED_HOME_NAMES = {'u', 'x'}

# 境界: 英数字に挟まれた 12 桁（ハッシュ・長い数字の一部）は対象外にする
ACCOUNT_ID = re.compile(r'(?<![0-9A-Za-z])([0-9]{12})(?![0-9A-Za-z])')
IAM_ARN = re.compile(r'arn:aws[a-z-]*:iam::([0-9]{12})')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
HOME_PATH = re.compile(r'/(?:Users|home)/([A-Za-z0-9._-]+)/')
SECRET_PATTERNS = (
    ('AWS access key ID', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('GitHub token', re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}')),
    ('GitHub fine-grained token', re.compile(r'github_pat_[A-Za-z0-9_]+')),
    ('API key (sk-)', re.compile(r'sk-[A-Za-z0-9]{20,}')),
    ('private key header', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
)


class CheckError(Exception):
    pass


def mask(value):
    """検出値をそのまま表示しないよう一部だけ残す。"""
    if len(value) >= 8:
        return value[:3] + '***' + value[-2:]
    if len(value) >= 4:
        return value[:1] + '***'
    return '***'


def placeholder_account(value):
    return value in EXAMPLE_ACCOUNT_IDS or len(set(value)) == 1


def allowed_email(value):
    lower = value.lower()
    if lower in ALLOWED_EMAILS or lower.endswith(ALLOWED_EMAIL_SUFFIXES):
        return True
    domain = lower.rsplit('@', 1)[1]
    return any(domain == d or domain.endswith('.' + d) for d in ALLOWED_EMAIL_DOMAINS)


def home_dir():
    # 実行中の利用者のホーム。'/' のような短すぎる値は誤検出にしかならないので使わない。
    home = os.path.normpath(os.path.expanduser('~'))
    return home if home.count('/') >= 2 else None


def scan_text(text, words=(), home=None):
    """(行番号, 種類, マスク済み値) のリストを返す。"""
    findings = []
    lowered_words = [(w, w.lower()) for w in words]
    for number, line in enumerate(text.splitlines(), 1):
        def add(kind, value):
            findings.append((number, kind, mask(value)))

        arn_spans = []
        for m in IAM_ARN.finditer(line):
            arn_spans.append(m.span(1))
            if not placeholder_account(m.group(1)):
                add('IAM ARN', m.group(0))
        for m in ACCOUNT_ID.finditer(line):
            # ARN として報告済みの位置は重複させない
            if m.span(1) not in arn_spans and not placeholder_account(m.group(1)):
                add('AWS account ID (12 digits)', m.group(1))
        for m in EMAIL.finditer(line):
            if not allowed_email(m.group(0)):
                add('email', m.group(0))
        for kind, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(line):
                add(kind, m.group(0))
        for m in HOME_PATH.finditer(line):
            if m.group(1) not in ALLOWED_HOME_NAMES:
                add('home path', m.group(0))
        if home and home in line:
            add('current HOME path', home)
        lower = line.lower()
        for word, needle in lowered_words:
            if needle in lower:
                add('privacy_words', word)
    return findings


def git(root, *args, input_bytes=None):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], input=input_bytes, capture_output=True,
                                timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f'git {args[0]} failed: {type(error).__name__}') from None
    if result.returncode != 0:
        message = result.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise CheckError(f'git {args[0]} failed: ' + (message[0] if message else f'exit {result.returncode}'))
    return result.stdout


def repo_root():
    try:
        result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f'git rev-parse failed: {type(error).__name__}') from None
    if result.returncode != 0:
        raise CheckError('not inside a git repository')
    return Path(result.stdout.strip())


def load_words(root):
    """config.local.json の privacy_words を読む。ファイルやキーがなければ空。不正な値はエラーにする。"""
    path = root / CONFIG_NAME
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise CheckError(f'{CONFIG_NAME}: cannot read ({type(error).__name__})') from None
    words = data.get('privacy_words', []) if isinstance(data, dict) else None
    if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
        raise CheckError(f'{CONFIG_NAME}: privacy_words must be a list of non-empty strings')
    return [w.strip() for w in words]


def index_entries(root, staged):
    """検査対象 (パス, blob ID, mode) を index から集める。"""
    if staged:
        names = git(root, 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z').split(b'\0')
        names = [n for n in names if n]
        if not names:
            return []
        listing = git(root, 'ls-files', '-s', '-z', '--', *[os.fsdecode(n) for n in names])
    else:
        listing = git(root, 'ls-files', '-s', '-z')
    entries = []
    for record in listing.split(b'\0'):
        if not record:
            continue
        meta, _, name = record.partition(b'\t')
        mode, blob, _stage = meta.split(b' ')
        entries.append((name.decode('utf-8', 'replace'), blob.decode(), mode.decode()))
    return entries


def read_blobs(root, entries):
    """git cat-file --batch で blob をまとめて読む。"""
    if not entries:
        return {}
    request = b''.join(blob.encode() + b'\n' for _, blob, _ in entries)
    output = git(root, 'cat-file', '--batch', input_bytes=request)
    blobs, offset = {}, 0
    for _, blob, _ in entries:
        end = output.index(b'\n', offset)
        header = output[offset:end].split(b' ')
        if len(header) != 3:
            raise CheckError(f'git cat-file: unexpected object {blob}')
        size = int(header[2])
        blobs[blob] = output[end + 1:end + 1 + size]
        offset = end + 1 + size + 1
    return blobs


def run(staged):
    root = repo_root()
    words = load_words(root)
    home = home_dir()
    entries = [e for e in index_entries(root, staged) if e[2] != '160000']  # サブモジュールは対象外
    blobs = read_blobs(root, entries)
    count = 0
    for name, blob, _mode in entries:
        data = blobs[blob]
        if len(data) > MAX_BYTES:
            print(f'SKIP {name}: larger than {MAX_BYTES} bytes (not checked)')
            continue
        if b'\0' in data[:8192]:
            print(f'SKIP {name}: binary (not checked)')
            continue
        for number, kind, shown in scan_text(data.decode('utf-8', 'replace'), words, home):
            print(f'{name}:{number}: {kind} ({shown})')
            count += 1
    scope = 'staged' if staged else 'tracked'
    print(f'privacy_check: {count} finding(s) in {len(entries)} {scope} file(s)')
    return 1 if count else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check files for personal data / secrets.', allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--staged', action='store_true', help='check added/modified files in the git index')
    mode.add_argument('--all', action='store_true', help='check all tracked files (index content)')
    args = parser.parse_args(argv)
    try:
        return run(args.staged)
    except CheckError as error:
        print(f'privacy_check: ERROR: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
