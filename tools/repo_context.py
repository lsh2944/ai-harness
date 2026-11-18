#!/usr/bin/env python3
"""Collect local worktree provenance without AI calls or repository writes.

Examples (relative paths are relative to the invoking cwd):
  python3 tools/repo_context.py --repo app --repo service
  python3 tools/repo_context.py --repo app --file app/src/example.ts --hash
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time

MAX_HASH_BYTES = 16 * 1024 * 1024
MAX_GIT_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT = 15


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def git(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    command = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
               '-C', str(path), *args]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    started = time.monotonic()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        try:
            while selector.get_map() or process.poll() is None:
                if time.monotonic() - started >= GIT_TIMEOUT:
                    raise ValueError('Git timed out')
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(chunk)
                    if sum(map(len, buffers.values())) > MAX_GIT_BYTES:
                        raise ValueError('Git stdout/stderr exceeded the capture limit; collection stopped')
            return subprocess.CompletedProcess(command, process.wait(), bytes(buffers['stdout']), bytes(buffers['stderr']))
        except (OSError, ValueError) as error:
            logs = Path(tempfile.mkdtemp(prefix='repo-context-git-error-'))
            for name, data in buffers.items():
                (logs / (name + '.log')).write_bytes(data)
            raise ValueError(f'{error}; captured output: {logs}') from error
        finally:
            # Also stop descendants retaining output pipes, including on interruption.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # macOS は終了済み（未回収）のグループリーダーに EPERM を返す。wait() で回収する。
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


def parse_status(data):
    """Porcelain v2 -z preserves spaces, newlines, and rename source paths."""
    records = iter(data.split(b'\0'))
    headers, changes = {}, []
    for record in records:
        if not record:
            continue
        line = os.fsdecode(record)
        if line.startswith('# '):
            key, value = line[2:].split(' ', 1)
            headers[key] = value
            continue
        kind = line[0]
        if kind in ('?', '!'):
            changes.append({'path': line[2:], 'xy': '??' if kind == '?' else '!!'})
        elif kind in ('1', '2', 'u'):
            parts = line.split(' ', {'1': 8, '2': 9, 'u': 10}[kind])
            if len(parts) != {'1': 9, '2': 10, 'u': 11}[kind]:
                raise ValueError('Malformed Git status record')
            entry = {'path': parts[-1], 'xy': parts[1]}
            if parts[2] != 'N...':
                entry['submodule'] = parts[2]
            if kind == '2':
                entry['original_path'] = os.fsdecode(next(records))
            if kind == 'u':
                entry['conflict'] = True
            changes.append(entry)
        else:
            raise ValueError('Unsupported Git status record')
    return headers, changes


def collect_repo(path, limit):
    path = Path(path).absolute()
    row = {'requested_path': str(path), 'worktree': None, 'git': None, 'errors': []}
    entries = []
    try:
        if not path.is_dir():
            raise ValueError('Repository path is not a directory')
        root = git(path, 'rev-parse', '--show-toplevel')
        if root.returncode:
            if b'not a git repository' in root.stderr:
                row.update(git=False, worktree=str(path.resolve()), changes=None)
                return row, entries
            raise ValueError('Git could not identify the worktree (exit %s)' % root.returncode)
        worktree = Path(os.fsdecode(root.stdout).rstrip('\n')).resolve()
        row.update(git=True, worktree=str(worktree))
        result = git(worktree, 'status', '--porcelain=v2', '--branch', '-z', '--untracked-files=all')
        if result.returncode:
            raise ValueError('Git status failed (exit %s)' % result.returncode)
        headers, entries = parse_status(result.stdout)
        head, branch = headers.get('branch.oid'), headers.get('branch.head')
        if head is None or branch is None:
            raise ValueError('Git did not report branch/HEAD metadata')
        row.update(branch=None if branch == '(detached)' else branch,
                   head=None if head == '(initial)' else head,
                   head_state='unborn' if head == '(initial)' else 'detached' if branch == '(detached)' else 'branch')
        changes = {
            'total_paths': len(entries),
            'staged': sum(e['xy'][0] not in '.?!' for e in entries),
            'unstaged': sum(e['xy'][1] not in '.?!' for e in entries),
            'untracked': sum(e['xy'] == '??' for e in entries),
            'conflicted': sum(bool(e.get('conflict')) for e in entries),
            'paths': entries[:limit], 'omitted_paths': max(0, len(entries) - limit)}
        if changes['omitted_paths']:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='repo-context-changes-',
                                             suffix='.json', delete=False) as full:
                json.dump(entries, full, ensure_ascii=True)
                changes['full_changes_path'] = full.name
        row['changes'] = changes
    except (OSError, ValueError, RuntimeError, StopIteration, subprocess.TimeoutExpired) as error:
        row['errors'].append(str(error) or 'Malformed Git status')
    return row, entries


def checked_hash(path):
    """Hash only explicitly selected regular code files; never emit contents."""
    path = Path(path).absolute()
    # macOS system aliases are allowed; user-controlled symlink sources are not.
    if any(p.is_symlink() for p in [path, *path.parents] if str(p) not in {'/tmp', '/var'}):
        raise ValueError('Hash refused: symlink source')
    resolved = path.resolve()
    protected = {'.aws', '.codex', '.ssh', '.gnupg', '.git', 'hooks'}
    if protected.intersection(resolved.parts) or ('tools', 'harness') in zip(resolved.parts, resolved.parts[1:]):
        raise ValueError('Hash refused: protected path')
    for root in (Path.home() / '.claude', Path.home() / '.codex', Path.home() / '.aws'):
        if resolved == root or root in resolved.parents:
            raise ValueError('Hash refused: assistant/credential path')
    name = resolved.name.lower()
    if name.startswith(('.env', 'secret', 'credential', 'id_rsa', 'id_ed25519')) or name in {
            'auth.json', 'settings.json', 'settings.local.json', '.claude.json', 'config.toml',
            'assume_role.sh', '.npmrc', '.netrc', '.pypirc'}:
        raise ValueError('Hash refused: credential/settings filename')
    if resolved.suffix.lower() not in {'.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.vue',
                                      '.c', '.cc', '.cpp', '.h', '.hpp', '.rs', '.go', '.java',
                                      '.sh', '.json', '.yaml', '.yml', '.toml', '.md', '.sql', '.asl'}:
        raise ValueError('Hash refused: select a supported code/document file')
    fd = os.open(resolved, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('Hash refused: not a regular single-link file')
        if before.st_size > MAX_HASH_BYTES:
            raise ValueError('Hash refused: file exceeds 16 MiB')
        digest, count = hashlib.sha256(), 0
        while chunk := source.read(65536):
            count += len(chunk)
            if count > MAX_HASH_BYTES:
                raise ValueError('Hash refused: file exceeds 16 MiB')
            digest.update(chunk)
        after = os.fstat(source.fileno())
        current = os.lstat(resolved)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError('File changed while hashing; no reliable digest')
    return digest.hexdigest()


def collect(paths, selected=(), hash_files=False, limit=20):
    report = {'schema_version': 1, 'started_at': now(), 'status': 'COMPLETE',
              'repositories': [], 'selected_files': [],
              'notes': ['Read-only observation, not an atomic snapshot or proof of code review.',
                        'Staged/unstaged counts may overlap. Git status excludes ignored files and summarizes submodules.',
                        'Same branch/HEAD/dirty counts do not prove equal working-tree contents.']}
    inventory = {}
    for path in paths:
        row, entries = collect_repo(path, limit)
        report['repositories'].append(row)
        if row['worktree']:
            inventory[Path(row['worktree'])] = (row, entries)
        if row['errors']:
            report['status'] = 'INCOMPLETE'
    for value in selected:
        path = Path(value).absolute()
        item = {'path': str(path), 'errors': []}
        try:
            owners = [p for p in inventory if path.resolve().is_relative_to(p)]
            if not owners:
                raise ValueError('Selected file is outside the requested worktrees')
            owner = max(owners, key=lambda p: len(p.parts))
            row, entries = inventory[owner]
            relative = str(path.resolve().relative_to(owner))
            item.update(worktree=str(owner), relative_path=relative)
            item['changes'] = [e for e in entries if relative in (e['path'], e.get('original_path'))]
            item['exists'] = path.exists()
            item['status_scope'] = 'Not listed does not prove clean/tracked; it can also be ignored or absent.'
            if row['errors']:
                raise ValueError('Selected worktree status could not be collected')
            if hash_files:
                item['sha256'] = checked_hash(path)
        except (OSError, ValueError, RuntimeError) as error:
            item['errors'].append(str(error))
            report['status'] = 'INCOMPLETE'
        report['selected_files'].append(item)
    report['finished_at'] = now()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', action='append', help='Repository/directory; repeat for multiple worktrees (default: cwd)')
    parser.add_argument('--file', action='append', default=[], help='Selected file, relative to cwd or absolute; repeat as needed')
    parser.add_argument('--hash', action='store_true', help='SHA256 only the explicitly selected code files')
    parser.add_argument('--limit', type=int, default=20, help='Changed paths per worktree in stdout, 0..200 (full list retained if omitted)')
    args = parser.parse_args()
    if args.hash and not args.file:
        parser.error('--hash requires at least one --file; automatic whole-tree hashing is not supported')
    if not 0 <= args.limit <= 200:
        parser.error('--limit must be between 0 and 200')
    report = collect(args.repo or [os.getcwd()], args.file, args.hash, args.limit)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['status'] == 'COMPLETE' else 2


if __name__ == '__main__':
    sys.exit(main())
