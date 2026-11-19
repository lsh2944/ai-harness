#!/usr/bin/env python3
"""追跡ファイル・コミット予定の内容・コミットメッセージに個人情報・秘密値の形が含まれていないか検査する（標準ライブラリのみ）。

使い方（cwd はこのリポジトリ内）:
  python3 tools/privacy_check.py --staged          # index の削除以外の変更ファイル（pre-commit hook が使う）
  python3 tools/privacy_check.py --all             # 追跡ファイル全体（index の内容）
  python3 tools/privacy_check.py --message <file>  # コミットメッセージ（コメント行を除く。commit-msg hook が使う）

検出時は「ファイル:行: 種類 (一部マスクした値)」だけを出力し、終了コード 1。
検査できないファイル（NUL を含むバイナリ）があっても終了コード 1（中身を確かめずに通さない）。
エラー（git 失敗・利用者設定の不正）は終了コード 2。
利用者ごとの語（実名・会社ドメイン等）は config.local.json（Git 除外）の任意キー privacy_words（文字列リスト）に置く。
"""
import argparse
import codecs
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CHUNK = 1024 * 1024  # blob はこの大きさずつ読む（大きいファイルも行単位で全体を検査する）
SNIFF = 8000  # 先頭のこのバイト数に NUL があればバイナリとみなす（git と同じ目安）
GIT_TIMEOUT = 60
CONFIG_NAME = 'config.local.json'
# BOM 付きの UTF-16/32 は NUL を含むが文字列として検査する。UTF-32 LE の BOM は UTF-16 LE の BOM を含むので先に判定する。
BOMS = (
    (codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'), (codecs.BOM_UTF8, 'utf-8-sig'),
    (codecs.BOM_UTF16_LE, 'utf-16'), (codecs.BOM_UTF16_BE, 'utf-16'),
)
WORDS_NOTE = 'privacy_check: note: 고유명사(실명·회사 도메인) 검사는 config.local.json의 privacy_words를 설정했을 때만 동작합니다.'

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
# fake は hooks/tests/test_pycache.py の /Users/fake（末尾 / なし。名前単独も検出するようにしたため必要になった）。
ALLOWED_HOME_NAMES = {'u', 'x', 'fake'}

# 境界: 英数字に挟まれた 12 桁（ハッシュ・長い数字の一部）は対象外にする
ACCOUNT_ID = re.compile(r'(?<![0-9A-Za-z])([0-9]{12})(?![0-9A-Za-z])')
IAM_ARN = re.compile(r'arn:aws[a-z-]*:iam::([0-9]{12})')
# 先頭の否定後読みで連続文字列の途中からの再試行を止める（長い行で計算量が二乗にならないようにする）
EMAIL = re.compile(r'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
# 名前の後ろが / だけでなく空白・引用符・括弧・カンマ・コロン・セミコロン・行末でも検出する（/Users/<名前> 単独も対象）
HOME_PATH = re.compile(r'/(?:Users|home)/([A-Za-z0-9._-]+)(?=[/\s\'"`()\[\]{}<>,:;]|$)')
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


def scan_line(line, number, words=(), home=None):
    """1 行を検査して (行番号, 種類, マスク済み値) のリストを返す。words は (元の語, 小文字) の組。"""
    findings = []

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
        # 文末の句点（/Users/x. 等）は名前に含めずに判定する
        if m.group(1).rstrip('.') not in ALLOWED_HOME_NAMES:
            add('home path', m.group(0))
    if home and home in line:
        add('current HOME path', home)
    lower = line.lower()
    for word, needle in words:
        if needle in lower:
            add('privacy_words', word)
    return findings


def lowered(words):
    return [(w, w.lower()) for w in words]


def scan_text(text, words=(), home=None):
    """文字列全体を検査する（行番号は 1 始まり）。"""
    pairs = lowered(words)
    findings = []
    for number, line in enumerate(text.split('\n'), 1):
        findings.extend(scan_line(line.rstrip('\r'), number, pairs, home))
    return findings


def detect_codec(head):
    for bom, codec in BOMS:
        if head.startswith(bom):
            return codec
    return None


def scan_stream(chunks, words=(), home=None):
    """bytes の塊の列を検査する。(findings, None) か、バイナリなら (None, 'binary') を返す。
    塊は必ず最後まで消費する（git cat-file の出力を読み残さないため）。
    行は塊の境界をまたいで繋げてから検査する（境界で値が分断されて見逃さない）。"""
    iterator = iter(chunks)
    head = b''
    for data in iterator:
        head += data
        if len(head) >= SNIFF:
            break
    codec = detect_codec(head)
    if codec is None and b'\0' in head[:SNIFF]:
        for _ in iterator:
            pass
        return None, 'binary'
    decoder = codecs.getincrementaldecoder(codec or 'utf-8')(errors='replace')
    pairs = lowered(words)
    findings, state = [], {'pending': '', 'number': 0}

    def feed(text, final):
        lines = (state['pending'] + text).split('\n')
        state['pending'] = '' if final else lines.pop()
        for line in lines:
            state['number'] += 1
            findings.extend(scan_line(line.rstrip('\r'), state['number'], pairs, home))

    feed(decoder.decode(head), False)
    for data in iterator:
        feed(decoder.decode(data), False)
    feed(decoder.decode(b'', final=True), True)
    return findings, None


def git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=GIT_TIMEOUT)
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
    """検査対象 (パス, blob ID, mode) を集める。パスを pathspec として渡さず blob ID を直接使う
    （[ や * を含むファイル名の誤解釈を避ける）。"""
    entries = []
    if staged:
        # 削除以外すべて（追加・変更・型変更等）。名前変更は追加として出す。
        fields = git(root, 'diff', '--cached', '--raw', '-z', '--no-renames', '--no-abbrev',
                     '--diff-filter=d').split(b'\0')
        fields = [f for f in fields if f]
        if len(fields) % 2:
            raise CheckError('git diff: unexpected raw output')
        for meta, name in zip(fields[0::2], fields[1::2]):
            parts = meta.lstrip(b':').split(b' ')
            if len(parts) != 5:
                raise CheckError('git diff: unexpected raw output')
            entries.append((os.fsdecode(name), parts[3].decode(), parts[1].decode()))
        return entries
    for record in git(root, 'ls-files', '-s', '-z').split(b'\0'):
        if not record:
            continue
        meta, _, name = record.partition(b'\t')
        mode, blob, _stage = meta.split(b' ')
        entries.append((os.fsdecode(name), blob.decode(), mode.decode()))
    return entries


class BlobReader:
    """git cat-file --batch を 1 つ起動し、blob を CHUNK ずつ読む。"""

    def __init__(self, root):
        try:
            self.proc = subprocess.Popen(['git', '-C', str(root), 'cat-file', '--batch'],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as error:
            raise CheckError(f'git cat-file failed: {type(error).__name__}') from None

    def chunks(self, blob):
        self.proc.stdin.write(blob.encode() + b'\n')
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split(b' ')
        if len(header) != 3:
            raise CheckError(f'git cat-file: unexpected object {blob}')
        remaining = int(header[2])
        while remaining:
            data = self.proc.stdout.read(min(CHUNK, remaining))
            if not data:
                raise CheckError('git cat-file: truncated output')
            remaining -= len(data)
            yield data
        self.proc.stdout.read(1)  # 末尾の改行

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=GIT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


def report(name, findings):
    for number, kind, shown in findings:
        print(f'{name}:{number}: {kind} ({shown})')
    return len(findings)


def run_files(staged):
    root = repo_root()
    words = load_words(root)
    home = home_dir()
    entries = [e for e in index_entries(root, staged) if e[2] != '160000']  # サブモジュールは対象外
    count, skipped = 0, 0
    reader = BlobReader(root) if entries else None
    try:
        for name, blob, _mode in entries:
            findings, reason = scan_stream(reader.chunks(blob), words, home)
            if reason:
                print(f'SKIP {name}: {reason} (not checked)')
                skipped += 1
            else:
                count += report(name, findings)
    finally:
        if reader:
            reader.close()
    scope = 'staged' if staged else 'tracked'
    print(f'privacy_check: {count} finding(s) in {len(entries)} {scope} file(s)')
    if staged and not words:
        print(WORDS_NOTE)
    if skipped:
        print(f'privacy_check: {skipped} file(s) could not be checked (binary). 내용을 확인하지 못해 차단합니다. '
              '필요하면 사용자가 --no-verify 사용 여부를 결정합니다.')
    return 1 if count or skipped else 0


def comment_char(root):
    try:
        result = subprocess.run(['git', '-C', str(root), 'config', '--get', 'core.commentChar'],
                                capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return '#'
    value = result.stdout.strip()
    # auto（自動選択）や未設定は既定の # とみなす
    return value if result.returncode == 0 and len(value) == 1 else '#'


def run_message(path):
    root = repo_root()
    words = lowered(load_words(root))
    home = home_dir()
    try:
        text = Path(path).read_bytes().decode('utf-8', 'replace')
    except OSError as error:
        raise CheckError(f'cannot read commit message: {type(error).__name__}') from None
    char = comment_char(root)
    scissors = f'{char} ------------------------ >8 ------------------------'
    count = 0
    for number, line in enumerate(text.split('\n'), 1):
        line = line.rstrip('\r')
        if line == scissors:
            break  # これ以降（commit -v の差分）は git がメッセージから取り除く
        if line.startswith(char):
            continue
        count += report('<commit message>', scan_line(line, number, words, home))
    print(f'privacy_check: {count} finding(s) in commit message')
    if not words:
        print(WORDS_NOTE)
    return 1 if count else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check files for personal data / secrets.', allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--staged', action='store_true', help='check added/modified files in the git index')
    mode.add_argument('--all', action='store_true', help='check all tracked files (index content)')
    mode.add_argument('--message', metavar='FILE', help='check a commit message file (comment lines excluded)')
    args = parser.parse_args(argv)
    try:
        if args.message is not None:
            return run_message(args.message)
        return run_files(args.staged)
    except CheckError as error:
        print(f'privacy_check: ERROR: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
