#!/usr/bin/env python3
"""Explicit local usage records; no AI calls, log discovery or usage estimates."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt
WINDOWS = os.name == 'nt'

# 既定はこのリポジトリの .local/（Git 管理外）。必要なら --store で変更する。
DEFAULT_STORE = Path(__file__).resolve().parents[2] / '.local' / 'task-metrics.jsonl'
MAX_STORE = 8 * 1024 * 1024
MAX_SUMMARY = 128 * 1024
NUMBERS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'elapsed_seconds', 'rework_count')
CHOICES = {
    'provider': ('claude', 'codex'),
    # independent は廃止済みだが既存記録の読み込みのため検証用 CHOICES に残す。
    'workflow': ('single', 'review', 'independent', 'test-plan', 'code-review', 'debate'),
    'kind': ('feature', 'bugfix', 'refactor', 'config'),
    'verification': ('passed', 'failed', 'not-run', 'unknown'),
    'source': ('provider-report', 'user-report', 'unknown'),
    'scope': ('declared-run', 'cross-check-call'),
}
# 新規記録で受け付ける workflow（廃止済み independent は除外）。
RECORD_WORKFLOWS = tuple(w for w in CHOICES['workflow'] if w != 'independent')
# debate の呼び出し別 summary の mode（debate-<phase>）。セッション集計は取り込まない。
DEBATE_PHASES = ('proposal', 'debate', 'critique', 'rebuttal', 'round2', 'fidelity')
FIELDS = {'version', 'recorded_at', 'task', 'run', 'execution_status', *CHOICES, *NUMBERS}
SEMANTICS = 'input_tokens includes cached_input_tokens; never add cached tokens again. Null means unknown. Scopes are aggregated separately; overlapping runs still double-count. No account quota or savings estimate.'


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
        raise ValueError('task/run must be a short ID: 1-64 ASCII letters, digits, dot, underscore or hyphen')
    return value


def number(value, field):
    if value is None:
        return
    if field == 'elapsed_seconds':
        valid = type(value) in (int, float)
    else:
        valid = type(value) is int
    if not valid or value < 0 or value > 2**53 - 1 or not math.isfinite(value):
        raise ValueError('invalid nonnegative finite numeric field: ' + field)


def validate(record):
    if not isinstance(record, dict) or set(record) != FIELDS or type(record['version']) is not int or record['version'] != 1:
        raise ValueError('invalid record fields/version')
    for field in ('task', 'run'):
        identifier(record[field])
    for field, choices in CHOICES.items():
        if not isinstance(record[field], str) or record[field] not in choices:
            raise ValueError('invalid record category: ' + field)
    if not isinstance(record['recorded_at'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00', record['recorded_at']):
        raise ValueError('invalid record timestamp')
    for field in NUMBERS:
        number(record[field], field)
    if record['cached_input_tokens'] is not None and record['input_tokens'] is not None and record['cached_input_tokens'] > record['input_tokens']:
        raise ValueError('cached_input_tokens cannot exceed total input_tokens')
    if record['scope'] == 'cross-check-call' and (record['provider'] != 'codex' or record['source'] != 'provider-report'):
        raise ValueError('invalid cross-check scope provenance')
    status = record['execution_status']
    if status not in (None, 'COMPLETE', 'INCOMPLETE'):
        raise ValueError('invalid execution status')
    if (record['scope'] == 'cross-check-call') != (status is not None):
        raise ValueError('execution status must come from a cross-check summary')


def safe_path(value):
    path = Path(value).expanduser()
    if '..' in path.parts:
        raise ValueError('parent traversal is not allowed')
    path = path.absolute()
    parts = path.parts
    protected_names = {'.aws', '.codex', 'hooks', 'credentials', 'auth.json', 'settings.json',
                       'settings.local.json', '.claude.json', 'config.toml', 'id_rsa', 'id_ed25519', 'assume_role.sh'}
    if any(p in protected_names or p.startswith('.env') for p in parts):
        raise ValueError('protected path')
    if path.suffix.lower() in {'.key', '.pem'} or any(parts[i:i + 2] == ('tools', 'harness') for i in range(len(parts) - 1)):
        raise ValueError('protected path')
    # macOS のシステム別名だけを許可し、その他は dir_fd と O_NOFOLLOW で検査する。
    if len(parts) > 1 and parts[1] in ('tmp', 'var') and Path('/', parts[1]).is_symlink():
        path = Path('/', parts[1]).resolve().joinpath(*parts[2:])
    return path


def _is_link(path):
    """シンボリックリンク・ジャンクションか（Windows）。OneDrive のクラウドファイルの再解析ポイントは対象外。"""
    info = os.lstat(path)
    junction = getattr(os.path, 'isjunction', lambda _p: False)(path)
    return stat.S_ISLNK(info.st_mode) or junction, info


@contextmanager
def _safe_file_windows(path, write):
    """Windows 版の safe_file。dir_fd・O_NOFOLLOW がないため、要素ごとに lstat でリンクでないことを確かめてから開く
    （検査と open の間の差し替えは防げない補助的な検査）。"""
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current = current / part
        if write:
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass
        link, info = _is_link(current)
        if link or not stat.S_ISDIR(info.st_mode):
            raise ValueError('path contains a link or a non-directory')
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_BINARY | getattr(os, 'O_NOINHERIT', 0)
    fd = None
    try:
        if os.path.lexists(path) and _is_link(path)[0]:
            raise ValueError('file must not be a link')
        if write:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(path, flags)
        else:
            fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('file must be regular with a single hard link')
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def safe_file(value, write=False):
    path = safe_path(value)
    if write and path.suffix != '.jsonl':
        raise ValueError('store path must end with .jsonl')
    if WINDOWS:
        with _safe_file_windows(path, write) as fd:
            yield fd
        return
    parent_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        for part in path.parts[1:-1]:
            if write:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        if write:
            # 同時初回記録では排他的作成と既存ファイルのオープンを分ける。
            try:
                fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
        else:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('file must be regular with a single hard link')
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _try_lock(fd, write):
    """非ブロッキングのロック。POSIX は flock（読み取りは共有）、Windows は msvcrt.locking（先頭 1 バイトの排他。
    共有ロックはないため読み取りも排他になる）。取れなければ BlockingIOError。"""
    if fcntl is not None:
        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return
    position = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as error:
        raise BlockingIOError(str(error)) from None
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def _unlock(fd):
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def locked(fd, write):
    deadline = time.monotonic() + 5
    while True:
        try:
            _try_lock(fd, write)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ValueError('store lock timed out')
            time.sleep(0.02)
    try:
        yield
    finally:
        _unlock(fd)


def read_bytes(fd, limit):
    if os.fstat(fd).st_size > limit:
        raise ValueError('file exceeds size limit')
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > limit:
        raise ValueError('file exceeds size limit')
    return bytes(data)


def decode_json(data):
    def reject_constant(value):
        raise ValueError('nonfinite JSON number')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid JSON; stored/source content was not printed') from None


def read_records(fd):
    data = read_bytes(fd, MAX_STORE)
    if data and not data.endswith(b'\n'):
        raise ValueError('store has an incomplete final record')
    records, seen = [], set()
    for raw in data.splitlines():
        record = decode_json(raw)
        validate(record)
        key = (record['provider'], record['run'])
        if key in seen:
            raise ValueError('duplicate provider/run in store')
        seen.add(key)
        records.append(record)
    return records


def import_summary(path, record):
    if record['provider'] != 'codex':
        raise ValueError('cross-check summaries describe Codex calls only')
    if any(record[field] is not None for field in NUMBERS if field != 'rework_count'):
        raise ValueError('cross-check import cannot override explicit token/elapsed values')
    if record['source'] not in ('unknown', 'provider-report'):
        raise ValueError('cross-check import requires provider-report source')
    with safe_file(path) as fd:
        summary = decode_json(read_bytes(fd, MAX_SUMMARY))
    modes = {'review': 'review', 'test-plan': 'test-plan', 'code-review': 'code-review',
             **{'debate-' + phase: 'debate' for phase in DEBATE_PHASES}}
    if not isinstance(summary, dict) or type(summary.get('codex_calls')) is not int or summary['codex_calls'] != 1:
        raise ValueError('not a single-call cross-check summary')
    if not isinstance(summary.get('mode'), str) or modes.get(summary['mode']) != record['workflow']:
        raise ValueError('summary mode does not match workflow')
    if summary.get('status') not in ('COMPLETE', 'INCOMPLETE'):
        raise ValueError('invalid cross-check summary status')
    usage = summary.get('usage')
    if usage is not None and not isinstance(usage, dict):
        raise ValueError('invalid cross-check usage')
    for field in ('input_tokens', 'output_tokens', 'cached_input_tokens'):
        record[field] = usage.get(field) if usage is not None else None
    record['elapsed_seconds'] = summary.get('elapsed_seconds')
    record['source'] = 'provider-report'
    record['scope'] = 'cross-check-call'
    record['execution_status'] = summary['status']
    # COMPLETE はテスト成功を意味しないため verification は入力値を保持する。


def append_record(store, record):
    validate(record)
    encoded = (json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    with safe_file(store, write=True) as fd, locked(fd, write=True):
        records = read_records(fd)
        if any((r['provider'], r['run']) == (record['provider'], record['run']) for r in records):
            raise ValueError('duplicate/conflicting provider/run; existing record kept')
        size = os.lseek(fd, 0, os.SEEK_END)
        if size + len(encoded) > MAX_STORE:
            raise ValueError('store exceeds 8 MiB limit')
        try:
            remaining = encoded
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise OSError('zero-length write')
                remaining = remaining[written:]
            os.fsync(fd)
        except OSError:
            os.ftruncate(fd, size)
            raise


def summarize(records, task=None):
    groups = {}
    for record in records:
        if task is not None and record['task'] != task:
            continue
        key = tuple(record[field] for field in ('provider', 'workflow', 'kind', 'scope'))
        if key not in groups:
            groups[key] = {**dict(zip(('provider', 'workflow', 'kind', 'scope'), key)), 'records': 0,
                           'metrics': {f: {'observed': 0, 'unknown': 0, 'sum': None} for f in NUMBERS},
                           'verification': {v: 0 for v in CHOICES['verification']},
                           'execution_status': {v: 0 for v in ('COMPLETE', 'INCOMPLETE', 'unknown')},
                           'source': {v: 0 for v in CHOICES['source']}}
        group = groups[key]
        group['records'] += 1
        group['verification'][record['verification']] += 1
        group['execution_status'][record['execution_status'] or 'unknown'] += 1
        group['source'][record['source']] += 1
        for field in NUMBERS:
            metric = group['metrics'][field]
            if record[field] is None:
                metric['unknown'] += 1
            else:
                metric['observed'] += 1
                metric['sum'] = (metric['sum'] or 0) + record[field]
    return {'records': sum(g['records'] for g in groups.values()), 'task': task,
            'semantics': SEMANTICS, 'groups': [groups[k] for k in sorted(groups)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=SEMANTICS)
    subparsers = parser.add_subparsers(dest='command', required=True)
    record_parser = subparsers.add_parser('record', help='Append one explicit, nonoverlapping run observation')
    summary_parser = subparsers.add_parser('summary', help='Group observed values; null stays unknown')
    for sub in (record_parser, summary_parser):
        sub.add_argument('--store', default=str(DEFAULT_STORE), help='Local JSONL file (default: %(default)s)')
    for field in ('task', 'run'):
        record_parser.add_argument('--' + field, required=True, help='Nonsecret short stable ID')
    for field in ('provider', 'workflow', 'kind'):
        choices = RECORD_WORKFLOWS if field == 'workflow' else CHOICES[field]
        record_parser.add_argument('--' + field, choices=choices, required=True)
    for field in ('verification', 'source'):
        record_parser.add_argument('--' + field, choices=CHOICES[field], default='unknown')
    for field in NUMBERS:
        record_parser.add_argument('--' + field.replace('_', '-'), type=float if field == 'elapsed_seconds' else int,
                                   help='Explicit observed value; omitted means unknown')
    record_parser.add_argument('--cross-check-summary', help='Import usage/elapsed/execution status from a single Codex cross-check summary.json')
    summary_parser.add_argument('--task', help='Filter by exact task ID')
    args = parser.parse_args(argv)
    try:
        if args.command == 'record':
            record = {field: getattr(args, field) for field in ('task', 'run', 'provider', 'workflow', 'kind', 'verification', 'source', *NUMBERS)}
            record.update(version=1, recorded_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), scope='declared-run', execution_status=None)
            if args.cross_check_summary:
                import_summary(args.cross_check_summary, record)
            append_record(args.store, record)
            output = {'status': 'recorded', 'scope': record['scope'], 'semantics': SEMANTICS}
        else:
            if args.task is not None:
                identifier(args.task)
            with safe_file(args.store) as fd, locked(fd, write=False):
                records = read_records(fd)
            output = summarize(records, args.task)
        print(json.dumps(output, allow_nan=False))
        return 0
    except (ValueError, OSError, OverflowError) as error:
        # パスや入力内容を含む OS エラーの詳細は出力しない。
        message = str(error) if isinstance(error, ValueError) else type(error).__name__ + ': file operation failed'
        print(json.dumps({'status': 'error', 'error': message}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    if WINDOWS:
        # 韓国語・日本語を含む help・出力を既定コードページ（cp932 等）で書くと失敗するため UTF-8 に固定する
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
