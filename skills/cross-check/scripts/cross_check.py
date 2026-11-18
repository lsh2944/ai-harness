#!/usr/bin/env python3
"""One bounded Codex call; no model/settings overrides or automatic retries."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
DEFAULT_CODEX = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')


def _runtime_config():
    """install.py がスキル直下に置く harness_config.json（なければ空）。"""
    try:
        data = json.loads((HERE.parent / 'harness_config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


HARNESS_RUNTIME = _runtime_config()


def _resolve_project():
    """作業ルートは実行時の cwd。cwd を確定できない場合（削除済み等）だけ設定の project_root を使う。
    環境変数などでの差し替えは設けない（AI が命令の前に付けて対象範囲を広げられないように）。"""
    try:
        return Path(os.getcwd()).resolve()
    except OSError:
        configured = HARNESS_RUNTIME.get('project_root')
        return Path(configured).resolve() if isinstance(configured, str) and os.path.isabs(configured) else Path('/')


def too_broad_root(path):
    """/・ホームやその上位・一時領域の根のように、作業ルートとして広すぎるか。ホーム配下の隠しディレクトリ内も対象。"""
    path = Path(path).resolve()
    homes = {Path.home().resolve()}
    try:
        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve())
    except (KeyError, OSError):
        pass
    broad = {Path(p).resolve() for p in ('/', '/tmp', '/private', '/private/tmp', '/var', '/private/var',
                                         '/var/folders', '/Users', '/home', '/Volumes')}
    if path in broad or any(home == path or home.is_relative_to(path) for home in homes):
        return True
    # ホーム配下の隠しディレクトリ（~/.aws・~/.codex・~/.claude・~/.agents 等の設定・認証領域）を含む場所も拒否する
    return any(path.is_relative_to(home) and any(part.startswith(".") for part in path.relative_to(home).parts)
               for home in homes)


PROJECT = _resolve_project()
# ハーネス本体（hook・インストーラ・設定断片・認証更新スクリプト・利用者設定）は根拠ソースとして読まない
HARNESS_HOME = Path(HARNESS_RUNTIME.get('harness_home') or HERE.parents[2])
HARNESS_PROTECTED = [HARNESS_HOME / 'hooks', HARNESS_HOME / 'install', HARNESS_HOME / 'settings',
                     HARNESS_HOME / 'tools' / 'aws', HARNESS_HOME / 'config.local.json']
MAX_PACKET = 64 * 1024
MAX_RESULT = 128 * 1024
MAX_LOGS = 8 * 1024 * 1024
# --include で読む元ファイルの上限（抜粋後の packet 全体は MAX_PACKET 以内）。
MAX_SOURCE = 8 * 1024 * 1024
# Q2: guard 範囲外または外部作用のある Codex 既定ツールは「可能なものは無効化、残りは検出して停止」。
# debate（co_design.py）と単一呼び出しで共有する唯一の定義。
# 実測（E2E-4, codex-cli 0.162.0-alpha.2, 2026-10-08）:
# - codex_apps・image_gen・node_repl は下のキーで露出しなかった。
# - cua_repl は同梱 MCP で、下のキーでも呼び出せる（mcp_tool_call server=cua_repl として監視が停止）。
#   `-c mcp_servers.cua_repl.enabled=false` は config 読み込みエラーで起動不能になるため使わない。
#   監視は事後停止なので、TOOL_POLICY_ENV で Codex guard hook に実行前の拒否もさせる（バージョン非依存）。
# - collaboration 系は features.multi_agent/code_mode_host/goals=false でも消えない。spawn_agent は --ephemeral では失敗、
#   list_agents はイベントを出さない（読み取りのみ）、wait_agent などは collab_tool_call として監視が停止する。
DISABLE_CONFIG = (
    'features.apps=false',                  # codex_apps（sites 配備・環境変数など）
    'features.multi_agent=false',           # collaboration.spawn_agent
    'features.multi_agent_v2=false',        # spawn_agent v2
    'features.image_generation=false',      # image_gen
    'features.computer_use=false',          # cua_repl
    'features.browser_use=false',           # node_repl（ブラウザ操作）
    'features.browser_use_external=false',  # node_repl（外部ブラウザ）
    # 2026-10-09 実測: 未使用機能の定義を外すと単一呼び出しの入力が約 4.6K トークン減る（指示の読み込みは不変）。
    'features.plugins=false',
    'features.skill_search=false',
    'features.tool_suggest=false',
    'features.goals=false',
    'features.view_image=false',
    'features.sleep_tool=false',
)
# 単一呼び出しモードは道具を使わないので shell も外す（debate は調査に shell が必要なので共有キーに入れない）。
SINGLE_CALL_DISABLE = DISABLE_CONFIG + ('features.shell_tool=false',)
# Codex guard hook（guard.py --codex）が読む道具方針。hook matcher 対象の道具について
# none=全拒否、read=シェル・Read 以外を拒否（matcher 外の道具は LiveWatch が止める）。
TOOL_POLICY_ENV = 'HARNESS_CODEX_TOOL_POLICY'


class RunnerSignal(BaseException):
    """Codex 呼び出し中にランナーが受けた終了シグナル。"""


GUARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
_PENDING_SIGNAL = {'value': None}


def _flag_signal(signum, frame):
    # ハンドラ内では例外を投げず記録だけする（後始末の途中で中断させない）。2 回目以降は無視する。
    if _PENDING_SIGNAL['value'] is None:
        _PENDING_SIGNAL['value'] = signum
    for sig in GUARDED_SIGNALS:
        try:
            signal.signal(sig, signal.SIG_IGN)
        except ValueError:
            pass


def check_signal():
    """記録済みの終了シグナルがあれば監視ループから RunnerSignal を送出する。"""
    if _PENDING_SIGNAL['value'] is not None:
        raise RunnerSignal(_PENDING_SIGNAL['value'])


@contextmanager
def signal_guard():
    """呼び出し中の SIGTERM/SIGHUP/SIGINT をフラグ化し、監視ループで RunnerSignal にしてプロセスグループを止める。"""
    previous = {}
    _PENDING_SIGNAL['value'] = None
    try:
        for sig in GUARDED_SIGNALS:
            previous[sig] = signal.signal(sig, _flag_signal)
    except ValueError:
        # メインスレッド以外では設定できない。
        pass
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        _PENDING_SIGNAL['value'] = None


def codex_env(policy):
    return {**os.environ, TOOL_POLICY_ENV: policy}

# 実行イベントの上位 type の許可リスト（E2E-3 実測。debate と共有）。
EVENT_TYPES = {'thread.started', 'turn.started', 'turn.completed', 'turn.failed', 'error',
               'item.started', 'item.updated', 'item.completed'}
# 単一呼び出しで許す item（道具呼び出しは一切許さない）。
ALLOWED_ITEMS = {'reasoning', 'agent_message'}
# hook 拒否は events.jsonl に現れず stderr.log の 1 行だけに出る（E2E-1/3 実測。debate と共有）。
HOOK_DENIAL_STDERR = 'Command blocked by PreToolUse hook'
MAX_STDERR_PENDING = 64 * 1024
INCLUDE_SPEC = re.compile(r'(?P<path>.+):(?P<a>\d+)-(?P<b>\d+)')


def check_source(path, label='packet'):
    """開く前に保護対象・認証情報・シンボリックリンク経由の入力を拒否し、解決済みパスを返す。"""
    path = Path(path).absolute()
    resolved = path.resolve(strict=True)
    protected = [Path.home() / '.aws', Path.home() / '.codex',
                 Path.home() / '.claude', *HARNESS_PROTECTED]
    if any(resolved == p.resolve() or p.resolve() in resolved.parents for p in protected):
        raise ValueError(f'protected {label} source')
    if any(p.name in {'.aws', '.codex', 'hooks'} for p in resolved.parents):
        raise ValueError(f'protected {label} source')
    if resolved.name in {'credentials', 'auth.json', 'settings.json', 'settings.local.json',
                         '.claude.json', 'config.toml', 'id_rsa', 'id_ed25519', 'assume_role.sh'} or resolved.name.startswith('.env') or resolved.suffix.lower() in {'.pem', '.key'}:
        raise ValueError(f'credential/settings {label} source')
    # System /tmp and /var aliases are normal on macOS; other linked inputs are not.
    if any(p.is_symlink() for p in [path, *path.parents] if str(p) not in {'/tmp', '/var'}):
        raise ValueError(f'symlink {label} source')
    return resolved


def read_regular(resolved, limit, too_large, label='packet'):
    """単一リンクの通常ファイルだけを上限付きで読み、(bytes, fstat) を返す。切り詰めない。"""
    fd = os.open(resolved, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f'{label} must be a regular, unlinked file')
        if info.st_size > limit:
            raise ValueError(too_large)
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError(too_large)
    return data, info


def read_packet(path):
    """Reject protected sources before opening; never silently truncate."""
    resolved = check_source(path)
    data, _ = read_regular(resolved, MAX_PACKET, 'packet exceeds 64 KiB; narrow the scope, do not truncate')
    if not data.strip():
        raise ValueError('empty or oversized packet')
    return data.decode('utf-8')


def parse_include(spec):
    match = INCLUDE_SPEC.fullmatch(spec)
    if not match:
        raise ValueError('--include must be <path>:<a>-<b>')
    first, last = int(match.group('a')), int(match.group('b'))
    if not 1 <= first <= last:
        raise ValueError('--include range must satisfy 1 <= a <= b')
    return match.group('path'), first, last


def git_provenance(resolved):
    """Git 管理下ならブランチ・コミット・当該ファイルの未コミット変更の有無を返す（読み取りのみ、ロック・fsmonitor 無効）。"""
    git = shutil.which('git')
    if not git:
        return 'unknown (git not found)'
    # パス名をグロブとして解釈させない（--literal-pathspecs）。
    base = [git, '--no-optional-locks', '--literal-pathspecs', '-c', 'core.fsmonitor=false', '-C', str(resolved.parent)]

    # GIT_DIR・GIT_WORK_TREE などが継承されると -C を無視して別のリポジトリを答えるため、GIT_* をすべて外す。
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', LC_ALL='C')

    def query(*args):
        result = subprocess.run(base + list(args), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=10, env=env)
        return result.returncode, result.stdout.strip()
    try:
        code, inside = query('rev-parse', '--is-inside-work-tree')
        if code != 0 or inside != 'true':
            return 'not a git work tree'
        code, commit = query('rev-parse', '--verify', '--quiet', 'HEAD')
        if code == 0 and commit:
            _, branch = query('rev-parse', '--abbrev-ref', 'HEAD')
        else:
            # 最初のコミット前（unborn）は HEAD が解決できないので、ブランチ名だけ symbolic-ref で取る。
            _, branch = query('symbolic-ref', '--short', '-q', 'HEAD')
            commit = None
        status_code, status = query('status', '--porcelain', '--', resolved.name)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown (git query failed)'
    dirty = 'unknown' if status_code != 0 else 'yes' if status else 'no'
    commit_text = commit if commit else 'none (no commits yet)'
    return f'branch {branch or "unknown"}, commit {commit_text}, file dirty: {dirty}'


def source_lines(text):
    """行番号付けは \\n だけで区切る（\\f・\\v・U+2028 などを改行と数えない）。CRLF の \\r は落とす。"""
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return [line[:-1] if line.endswith('\r') else line for line in lines]


def build_excerpts(specs):
    """--include の範囲を「Source excerpts」節にする（原文・行番号・sha256・更新時刻・Git 情報）。"""
    if not specs:
        return ''
    parts = ['## Source excerpts',
             'Original source lines (prefixed with L<line>) with provenance captured when this packet was built.']
    for spec in specs:
        path, first, last = parse_include(spec)
        resolved = check_source(path, 'include')
        data, info = read_regular(resolved, MAX_SOURCE, '--include source exceeds 8 MiB', 'include')
        lines = source_lines(data.decode('utf-8'))
        if last > len(lines):
            raise ValueError(f'--include range outside the file ({len(lines)} lines): {spec}')
        modified = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec='seconds')
        parts += ['', f'### {resolved}:L{first}-L{last}',
                  f'- file sha256: {hashlib.sha256(data).hexdigest()}',
                  f'- modified: {modified}',
                  f'- git: {git_provenance(resolved)}',
                  *[f'L{n}: {lines[n - 1]}' for n in range(first, last + 1)]]
    return '\n'.join(parts) + '\n'


def compose_packet(packet, specs):
    """packet 末尾に抜粋節を付ける。64 KiB を超えたら切り詰めずに拒否する。"""
    excerpts = build_excerpts(specs)
    combined = packet if not excerpts else packet.rstrip('\n') + '\n\n' + excerpts
    if len(combined.encode('utf-8')) > MAX_PACKET:
        raise ValueError('packet with source excerpts exceeds 64 KiB; narrow the ranges, do not truncate')
    return combined


def stop_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except PermissionError:
            # macOS では終了済み・未回収のリーダーだけが残るグループへの killpg が EPERM になる。
            # リーダーを回収してから残った子孫へ再送する。
            if process.poll() is None:
                # 生存中のリーダーにも送れない場合はリーダーだけでも終了させてから例外を伝える。
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            # Allow graceful termination, then kill remaining descendants too.
            time.sleep(0.2)
    process.wait(timeout=5)


def validate_result(result, mode, line_count):
    if not isinstance(result, dict) or set(result) != {'mode', 'status', 'assessment', 'findings', 'unverified'}:
        raise ValueError('invalid result fields')
    if result['mode'] != mode or not isinstance(result['status'], str) or result['status'] not in {'COMPLETE', 'INCOMPLETE'}:
        raise ValueError('invalid result mode/status')
    if not isinstance(result['assessment'], str) or not result['assessment'].strip():
        raise ValueError('empty assessment')
    if not isinstance(result['unverified'], list) or any(not isinstance(x, str) or not x.strip() for x in result['unverified']):
        raise ValueError('invalid unverified list')
    if not isinstance(result['findings'], list):
        raise ValueError('invalid findings')
    for finding in result['findings']:
        if not isinstance(finding, dict) or set(finding) != {'severity', 'claim', 'lines', 'recommendation'}:
            raise ValueError('invalid finding fields')
        if not isinstance(finding['severity'], str) or finding['severity'] not in {'high', 'medium', 'low'} or any(not isinstance(finding[k], str) or not finding[k].strip() for k in ('claim', 'recommendation')):
            raise ValueError('invalid finding content')
        lines = finding['lines']
        if not isinstance(lines, list) or not lines or any(type(n) is not int or not 1 <= n <= line_count for n in lines):
            raise ValueError('invalid packet line reference')
    if result['status'] == 'INCOMPLETE' and not result['unverified']:
        raise ValueError('incomplete result needs an explanation')


def event_problem(raw, allowed=ALLOWED_ITEMS):
    """1 行の実行イベントの違反理由（なければ None）。実行中の監視と終了後の検査で共用する。"""
    if not raw.strip():
        return None
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError):
        return 'malformed execution event'
    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
        return 'malformed execution event'
    kind = event['type']
    if kind not in EVENT_TYPES:
        return 'unexpected event type: ' + kind[:40]
    if kind in {'turn.failed', 'error'}:
        return 'Codex reported an execution error; inspect events.jsonl/stderr.log'
    if kind in {'item.started', 'item.updated', 'item.completed'}:
        item = event.get('item', {})
        if not isinstance(item, dict) or not isinstance(item.get('type'), str):
            return 'malformed execution item'
        if item['type'] not in allowed:
            return 'unexpected tool/action event: ' + item['type'][:40]
    return None


def inspect_events(path, allowed=ALLOWED_ITEMS):
    usage = None
    violations = []
    completed = False
    with path.open(encoding='utf-8', errors='replace') as events:
        for raw in events:
            problem = event_problem(raw, allowed)
            if problem:
                violations.append(problem)
                continue
            if not raw.strip():
                continue
            event = json.loads(raw)
            if event['type'] == 'turn.completed':
                completed = True
                usage = event.get('usage')
    if not completed:
        violations.append('missing turn.completed event')
    return usage, sorted(set(violations))


class LiveWatch:
    """events.jsonl・stderr.log を 0.1 秒ごとに追記分だけ読み、最初の違反理由を返す（読み取り境界で分かれた行は連結）。"""

    def __init__(self, event_path, error_path, allowed=ALLOWED_ITEMS):
        self.event_path, self.error_path = event_path, error_path
        self.allowed = allowed
        self.positions = {'events': 0, 'stderr': 0}
        self.events_pending = b''
        self.stderr_pending = b''

    def _chunk(self, name, path):
        with path.open('rb') as source:
            source.seek(self.positions[name])
            chunk = source.read(1024 * 1024)
        self.positions[name] += len(chunk)
        return chunk

    def poll_events(self, to_eof=False):
        while True:
            chunk = self._chunk('events', self.event_path)
            *lines, self.events_pending = (self.events_pending + chunk).split(b'\n')
            for line in lines:
                problem = event_problem(line.decode('utf-8', 'replace'), self.allowed)
                if problem:
                    return problem
            if not to_eof or not chunk:
                break
        if to_eof and self.events_pending.strip():
            return event_problem(self.events_pending.decode('utf-8', 'replace'), self.allowed)
        return None

    def poll_stderr(self, to_eof=False):
        marker = HOOK_DENIAL_STDERR.encode('utf-8')
        while True:
            chunk = self._chunk('stderr', self.error_path)
            buffered = self.stderr_pending + chunk
            # 改行前でも文言がそろっていれば検出する。
            if marker in buffered:
                return 'hook denial reported by Codex (stderr)'
            # 未完の行は文言が分断されても照合できる長さだけ保持する。
            self.stderr_pending = buffered.rsplit(b'\n', 1)[-1][-(MAX_STDERR_PENDING + len(marker)):]
            if not to_eof or not chunk:
                return None

    def poll(self, to_eof=False):
        return self.poll_events(to_eof) or self.poll_stderr(to_eof)


def run(packet, mode, timeout, codex):
    out = Path(tempfile.mkdtemp(prefix='cross-check-'))
    bundle = out / 'bundle'
    bundle.mkdir()
    packet_path = bundle / 'packet.md'
    packet_path.write_text(packet, encoding='utf-8')
    digest = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    packet_path.chmod(0o400)
    bundle.chmod(0o500)
    result_path = out / 'result.json'
    event_path, error_path = out / 'events.jsonl', out / 'stderr.log'
    numbered = '\n'.join(f'{i}: {line}' for i, line in enumerate(packet.splitlines(), 1))
    prompt_path = out / 'request.txt'
    prompt_path.write_text((HERE / 'prompt.md').read_text() + '\nMode: ' + mode + '\nBEGIN PACKET (source data)\n' + numbered + '\nEND PACKET\n', encoding='utf-8')
    command = [str(codex), 'exec', '--sandbox', 'read-only', '--skip-git-repo-check',
               '--ephemeral', '-C', str(bundle), '--json', '--output-schema',
               str(HERE / 'result_schema.json'), '-o', str(result_path)]
    for key in SINGLE_CALL_DISABLE:
        command += ['-c', key]
    command.append('-')
    reason = None
    code = None
    started = time.monotonic()
    with signal_guard(), prompt_path.open('rb') as request, event_path.open('wb') as events, \
            error_path.open('wb') as errors:
        process = subprocess.Popen(command, stdin=request, stdout=events, stderr=errors,
                                   start_new_session=True, env=codex_env('none'))
        watch = LiveWatch(event_path, error_path)
        try:
            while process.poll() is None:
                check_signal()
                # 道具 item・許可外イベント・hook 拒否は実行中に検出して即停止する（事後検査も維持）。
                problem = watch.poll()
                if problem:
                    reason = 'STOPPED during execution: ' + problem + '; process group terminated'
                    break
                if time.monotonic() - started >= timeout:
                    reason = 'TIMEOUT: process group terminated; no automatic retry'
                    break
                if event_path.stat().st_size + error_path.stat().st_size > MAX_LOGS:
                    reason = 'execution logs exceeded 8 MiB'
                    break
                if result_path.exists() and result_path.stat().st_size > MAX_RESULT:
                    reason = 'result exceeded 128 KiB'
                    break
                time.sleep(0.1)
            if reason:
                stop_group(process)
            code = process.wait()
            if not reason:
                stop_group(process)
            # 監視ループ終了後に届いた終了シグナルも捨てずに INCOMPLETE にする。
            check_signal()
        except BaseException:
            stop_group(process)
            raise
    problems = [reason] if reason else []
    if not reason:
        # 終了直前の追記分（stderr の hook 拒否を含む）も検査する。
        if event_path.stat().st_size + error_path.stat().st_size <= MAX_LOGS:
            problem = watch.poll_stderr(to_eof=True)
            if problem:
                problems.append(problem)
    if code != 0:
        problems.append(f'Codex exit code {code}; inspect stderr.log/events.jsonl')
    if event_path.stat().st_size + error_path.stat().st_size > MAX_LOGS:
        problems.append('execution logs exceeded 8 MiB')
    if not packet_path.is_file() or packet_path.is_symlink() or packet_path.stat().st_size > MAX_PACKET or hashlib.sha256(packet_path.read_bytes()).hexdigest() != digest or set(bundle.iterdir()) != {packet_path}:
        problems.append('input bundle changed')
    if event_path.stat().st_size <= MAX_LOGS:
        usage, event_problems = inspect_events(event_path)
    else:
        usage, event_problems = None, ['execution events too large to inspect']
    problems.extend(event_problems)
    result = None
    try:
        if result_path.stat().st_size > MAX_RESULT:
            raise ValueError('result exceeded 128 KiB')
        result = json.loads(result_path.read_text(encoding='utf-8'))
        validate_result(result, mode, len(packet.splitlines()))
    except (OSError, ValueError, UnicodeError) as error:
        problems.append('invalid/missing result: ' + str(error))
    summary = {'status': 'INCOMPLETE' if problems or result is None else result['status'],
               'mode': mode, 'codex_calls': 1, 'exit_code': code,
               'elapsed_seconds': round(time.monotonic() - started, 2), 'usage': usage,
               'problems': problems, 'output_dir': str(out), 'result_path': str(result_path)}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet', required=True)
    parser.add_argument('--mode', choices=['review', 'test-plan', 'code-review'], default='review')
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--include', action='append', default=[], metavar='PATH:A-B',
                        help='append source lines a-b of PATH with provenance (repeatable)')
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 3600:
            raise ValueError('timeout must be greater than 0 and at most 3600 seconds')
        packet = compose_packet(read_packet(args.packet), args.include)
        codex = DEFAULT_CODEX if DEFAULT_CODEX.is_file() else shutil.which('codex')
        if not codex:
            raise ValueError('Codex CLI not found; no alternative execution attempted')
        summary = run(packet, args.mode, args.timeout, codex)
    except (OSError, ValueError) as error:
        summary = {'status': 'INCOMPLETE', 'problems': [str(error)]}
    except RunnerSignal as stop:
        summary = {'status': 'INCOMPLETE',
                   'problems': [f'runner received signal {stop.args[0]}; Codex process group terminated']}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['status'] == 'COMPLETE' else 2


if __name__ == '__main__':
    sys.exit(main())
