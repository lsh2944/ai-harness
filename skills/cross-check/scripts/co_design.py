#!/usr/bin/env python3
"""Claude-Codex co-design debate runner: bounded tool-enabled Codex calls, frozen proposals and a ledger.

No model/settings overrides except the fixed tool-disable keys, no automatic retries, no cloud writes.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import pwd
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
DEBATE = HERE / 'debate'
SCHEMAS = DEBATE / 'schemas'
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
ARTIFACT_ROOT = PROJECT / '.local' / 'co-design'
# テストでは偽 codex のパスに差し替える。None なら cross_check と同じ探索を行う。
CODEX = None


def _user_tmp():
    try:
        value = os.confstr('CS_DARWIN_USER_TEMP_DIR')
    except (ValueError, OSError, AttributeError):
        value = None
    return Path(value or tempfile.gettempdir())


USER_TMP = _user_tmp()


def _load_cross_check():
    # read_packet と stop_group は同じディレクトリの cross_check.py を再利用する。
    spec = importlib.util.spec_from_file_location('cross_check_shared', HERE / 'cross_check.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CC = _load_cross_check()


def _load_submission(name):
    spec = importlib.util.spec_from_file_location(name, DEBATE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PARTIAL = _load_submission('partial_submission')
HANDOFF = _load_submission('proposal_handoff')

PLANS = {
    # codex_budget は Codex 呼び出しの実行時間の累計上限（Claude の作業時間は含めない）。
    'A': {'codex_budget': 1800, 'max_calls': 3, 'required': ('proposal', 'debate')},
    'B': {'codex_budget': 2400, 'max_calls': 4, 'required': ('proposal', 'critique', 'rebuttal')},
}
CONDITIONAL = ('round2', 'fidelity')
# phase -> (呼び出し前の状態, 成功後の状態)
PHASES = {
    'proposal': ('INPUTS_FROZEN', 'CODEX_PROPOSED'),
    'debate': ('REVEALED', 'DEBATED'),
    'critique': ('REVEALED', 'CODEX_CRITIQUED'),
    'rebuttal': ('CLAUDE_CRITIQUE_FROZEN', 'DEBATED'),
    'round2': ('SYNTHESIZED', 'SYNTHESIZED'),
    'fidelity': ('SYNTHESIZED', 'SYNTHESIZED'),
}
PHASE_SCHEMA = {'proposal': 'proposal', 'debate': 'debate_codex', 'critique': 'debate_codex',
                'rebuttal': 'debate_codex', 'round2': 'debate_codex', 'fidelity': 'fidelity'}
AFTER_CODEX_PROPOSAL = ('CODEX_PROPOSED', 'CLAUDE_FROZEN', 'REVEALED', 'CODEX_CRITIQUED',
                        'CLAUDE_CRITIQUE_FROZEN', 'DEBATED', 'SYNTHESIZED')
PROPOSAL_TIMEOUT = 900
CALL_TIMEOUT = 600
MIN_REMAINING = 60
MAX_INPUT = 64 * 1024
MAX_REQUEST = 192 * 1024
MAX_RESULT = 128 * 1024
MAX_LOGS = 8 * 1024 * 1024
MAX_DOC = 256 * 1024
MAX_SOURCE = 8 * 1024 * 1024
MAX_TOOL_CALLS = 60
MAX_TOOL_OUTPUT = 4 * 1024 * 1024
MAX_PARTIAL = 64 * 1024
# debate 以降の要求に添付する Claude 根拠の凍結原文の上限（超過分は登録情報だけ送る）。
MAX_CAPTURES = 96 * 1024
LOSSLESS_FORMAT = 'lossless-v1'
FIDELITY_FORMAT = 'fidelity-json-lines-v1'
MAX_REUSE = 16 * 1024
# freeze-inputs --data で共有する参照データ（ログ集計・CSV 等）の上限。
# 丸ごと読まれても道具出力 4 MiB・ログ 8 MiB の上限で止まらない大きさにする。
MAX_DATA_FILE = 1024 * 1024
MAX_DATA_TOTAL = 4 * 1024 * 1024
MAX_STR = 20000
MAX_ITEMS = 200
EVIDENCE_DAYS = 14
LOG_DAYS = 7
SID_RE = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{6}')
QID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
# 上位イベント type の許可リストは単一呼び出しモード（cross_check.py）と共有する。
EVENT_TYPES = CC.EVENT_TYPES
ITEM_TYPES = {'agent_message', 'reasoning', 'command_execution', 'mcp_tool_call', 'web_search', 'todo_list'}
TOOL_ITEMS = {'command_execution', 'mcp_tool_call', 'web_search'}
KNOWN_STATUSES = {None, 'in_progress', 'completed', 'failed'}
# Q2: 無効化キーは単一呼び出しモードと共有する唯一の定義（cross_check.py の DISABLE_CONFIG、実測の注記もそこにある）。
DISABLE_CONFIG = CC.DISABLE_CONFIG
FORBIDDEN_SERVERS = {'codex_apps', 'node_repl', 'cua_repl', 'image_gen', 'collaboration'}
FORBIDDEN_TOOL_WORDS = ('spawn_agent', 'imagegen', 'image_gen')
# guard.py の SECRET_WORDS と同じパターン（文字列中の秘密情報パス）。
SECRET_WORDS = re.compile(r"\.aws/(credentials|\.credentials|cli/cache|sso/cache|login/cache)"
                          r"|\.codex/auth\.json|\.claude/\.credentials")
SID_PATTERN = r'\d{8}t\d{6}z-[0-9a-f]{6}'
# 道具出力の検査は経路形のパターンだけを使う（素の単語は文書・本スキル自体に現れるため対象外）。
OUTPUT_MARKERS = (
    ('claude-session-path', re.compile(r'(?:^|[\s/\'"`(])\.claude/projects/[^/\s]+/(?!memory(?![\w-]))[^/\s]+', re.I | re.M)),
    ('claude-tmp-path', re.compile(r'/(?:private/)?tmp/claude-\d+/', re.I)),
    ('co-design-work-path', re.compile(r'co-design-' + SID_PATTERN + r'/(?:logs|evidence)\b', re.I)),
)
CO_DESIGN_ARTIFACT = re.compile(r'\.local/co-design/(' + SID_PATTERN + r')/(\S*)', re.I)
# 広い根を対象にすると相手側の作業領域まで辿れる一覧・探索系ツール（grep は再帰指定時のみ）。
LISTING_TOOLS = {'rg', 'find', 'ls', 'xargs', 'tree', 'du', 'fd', 'ag', 'locate', 'mdfind', 'cd', 'pushd'}
GREP_TOOLS = {'grep', 'egrep', 'fgrep'}
SEARCH_TOOLS = GREP_TOOLS | {'rg', 'ag'}
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
WRAPPERS = {'env', 'command', 'nice', 'nohup', 'time', 'builtin', 'exec', 'sudo'}
GLOB_CHARS = '*?[{'
# 値を取る短い・長いオプション（値はパスとして扱わない）。
SHORT_VALUE = {'grep': 'ABCmdD', 'egrep': 'ABCmdD', 'fgrep': 'ABCmdD', 'rg': 'ABCmgtTMjEr', 'ag': 'ABCmGg'}
LONG_VALUE = {'--after-context', '--before-context', '--context', '--max-count', '--glob', '--iglob', '--type',
              '--type-not', '--type-add', '--max-depth', '--max-columns', '--include', '--exclude', '--exclude-dir',
              '--directories', '--devices', '--encoding', '--threads', '--sort', '--sortr', '--color', '--colors',
              '--max-filesize', '--path-separator', '--replace', '--label', '--pre-glob'}
# 値がファイル・コマンドのパスになる長いオプション（経路として検査する）。
LONG_PATH_VALUE = {'--file', '--ignore-file', '--pre'}
FIND_PATTERN_OPTIONS = {'-name', '-iname', '-path', '-ipath', '-regex', '-iregex', '-wholename', '-iwholename',
                        '-lname', '-ilname'}
# hook 拒否は events.jsonl に現れず stderr.log の 1 行だけに出る（E2E-1/3 実測）。
HOOK_DENIAL_STDERR = CC.HOOK_DENIAL_STDERR
MAX_STDERR_PENDING = CC.MAX_STDERR_PENDING
HOOK_DENIAL = re.compile(r'PreToolUse hook|blocked by .{0,40}hook', re.IGNORECASE)
INPUT_REF = re.compile(r'(brief|scope|index|synthesis):L(\d+)(?:-L?(\d+))?')
# 討論・fidelity の引用は "X6: path:L1-L2" のように根拠 ID を前置することがある（E2E-6 実測）。
PREFIXED_REF = re.compile(r'([EX]\d{1,4})\s*(?::|-|\u2013|\u2014)\s*(.*)', re.S)
LEDGER_REF = re.compile(r'ledger:([A-Z]{1,2}\d{1,4})')
# 自分の過去出力（プロンプトに与えた文書）への参照。例: "earlier debate output: critique_of_claude[C1]"
EARLIER_REF = re.compile(r'(?:(?:your\s+)?earlier\s+|your\s+)?(proposal|debate|critique|rebuttal|round2)(?:\s+output)?\s*:\s*\S.*', re.I | re.S)
LINK_KINDS = ('linked', 'unbacked', 'input', 'inference')
# linked の内訳（D5）: excerpt = 引用行範囲を覆う読み取りが道具記録にある、mention = 経路・語の一致だけ。
STRENGTH_KINDS = ('excerpt', 'mention')
FILE_REF = re.compile(r'(?P<path>[^\s:]+):L(?P<a>\d+)(?:-L?(?P<b>\d+))?')
EVIDENCE_ID = re.compile(r'[EX]\d{1,4}')
ISSUE_ID = re.compile(r'[A-Z]{1,2}\d{1,4}')
# 外部調査（最新動向・一般的な実装・論文）の URL。空白を含まない単一の http(s) URL だけを受け取る。
WEB_URL = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)
RESEARCH_LISTS = ('trends', 'common_practices', 'papers')
RESEARCH_CHECK_STATUS = ('verified', 'mismatch', 'unreachable', 'not-checked')
INPUT_FILES = {'brief': 'brief.md', 'scope': 'scope.json', 'index': 'evidence_index.md'}


class Refused(Exception):
    """状態を変えずに拒否する。"""


class SchemaError(ValueError):
    pass


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
    """呼び出し中の SIGTERM/SIGHUP/SIGINT をフラグ化し、0.1 秒監視ループで RunnerSignal にして既存の後始末を通す。"""
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


def now():
    return time.time()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- JSON・スキーマ

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
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SchemaError('invalid JSON: ' + str(error)[:120]) from None


def load_schema(name):
    return json.loads((SCHEMAS / (name + '.schema.json')).read_text(encoding='utf-8'))


def _type_ok(value, kind):
    return {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
            'integer': type(value) is int, 'number': type(value) in (int, float), 'boolean': type(value) is bool,
            'null': value is None}.get(kind, False)


def check_schema(value, schema, path='$'):
    """標準ライブラリだけの subset 検証（type, enum, required, additionalProperties, properties, items）。"""
    kinds = schema.get('type')
    kinds = kinds if isinstance(kinds, list) else [kinds] if kinds else []
    if kinds and not any(_type_ok(value, k) for k in kinds):
        raise SchemaError(f'{path}: expected {"/".join(kinds)}')
    if 'enum' in schema and value not in schema['enum']:
        raise SchemaError(f'{path}: value not in enum')
    if isinstance(value, dict) and 'properties' in schema:
        props = schema['properties']
        missing = [k for k in schema.get('required', []) if k not in value]
        if missing:
            raise SchemaError(f'{path}: missing {missing[0]}')
        if schema.get('additionalProperties') is False:
            extra = [k for k in value if k not in props]
            if extra:
                raise SchemaError(f'{path}: unexpected {extra[0]}')
        for key, item in value.items():
            if key in props:
                check_schema(item, props[key], f'{path}.{key}')
    if isinstance(value, list) and 'items' in schema:
        for i, item in enumerate(value):
            check_schema(item, schema['items'], f'{path}[{i}]')


def check_lengths(value, path='$'):
    # 長さ制限は strict スキーマに書けないため別途検証する。
    if isinstance(value, str) and len(value) > MAX_STR:
        raise SchemaError(f'{path}: string too long')
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise SchemaError(f'{path}: too many items')
        for i, item in enumerate(value):
            check_lengths(item, f'{path}[{i}]')
    if isinstance(value, dict):
        for key, item in value.items():
            check_lengths(item, f'{path}.{key}')


def validate_doc(value, schema_name):
    check_schema(value, load_schema(schema_name))
    check_lengths(value)


# ---------------------------------------------------------------- パス・ファイル

def sid_check(sid):
    if not isinstance(sid, str) or not SID_RE.fullmatch(sid):
        raise Refused('invalid session id')
    return sid


def session_dir(sid):
    return ARTIFACT_ROOT / sid_check(sid)


def art(sid):
    return session_dir(sid) / 'artifacts'


def work(sid):
    """一時作業領域（request・events・stderr の実行ログだけ。7 日で prune 対象）。"""
    return USER_TMP / ('co-design-' + sid_check(sid))


def store(sid):
    """Codex 禁止領域（.local/co-design/<sid>/）内の恒久作業領域。根拠抜粋・道具記録・呼び出し別 summary/result を置く。

    再起動で一時領域が消えても reveal・round2・finalize が動くようにする（自動削除しない）。
    """
    return session_dir(sid) / 'work'


def evidence_dirs(sid):
    """道具記録・根拠抜粋の探索順（恒久領域、旧セッションの一時領域）。"""
    return [store(sid) / 'evidence', work(sid) / 'evidence']


def call_store(sid, index, phase):
    return store(sid) / 'calls' / f'{index}-{phase}'


def call_logs(sid, index, phase):
    return work(sid) / 'logs' / 'calls' / f'{index}-{phase}'


def claude_evidence_text(sid, ident):
    """登録済み根拠抜粋（E<n>）。恒久領域を優先し、旧セッションの一時領域も読む。なければ None。"""
    for directory in evidence_dirs(sid):
        path = directory / 'claude' / (ident + '.txt')
        try:
            data, _, _ = read_source(path, MAX_DOC, allow_session_files=True)
        except (Refused, OSError):
            continue
        return data.decode('utf-8', 'replace')
    return None


def call_file(sid, call, name):
    """呼び出し別ファイルの実在パス（恒久領域、記録済みパス、旧一時領域の順）。なければ None。"""
    candidates = [call_store(sid, call['index'], call['phase']) / name]
    if name == 'summary.json' and call.get('summary'):
        candidates.append(Path(call['summary']))
    candidates.append(call_logs(sid, call['index'], call['phase']) / name)
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            return path
    return None


def ensure_dir(path):
    """シンボリックリンクを経由しない 0700 ディレクトリを作る。"""
    path = Path(path)
    if path.is_symlink():
        raise Refused('symlinked directory refused')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise Refused('directory check failed')
    return path


def write_frozen(path, data):
    """排他的に作成して 0400 で固定する。既存ファイルは上書きしない。"""
    ensure_dir(Path(path).parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)


def write_private(path, data):
    ensure_dir(Path(path).parent)
    temp = Path(str(path) + '.tmp-' + secrets.token_hex(4))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def dump(value):
    return (json.dumps(value, ensure_ascii=False, indent=1) + '\n').encode('utf-8')


def _within(path, root):
    try:
        root = Path(root).resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def read_source(value, limit, allow_session_files=False):
    """read_packet と同じ保護検査で通常ファイルを読み、(bytes, fstat, 解決パス) を返す。"""
    path = Path(value).absolute()
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise Refused('source file not found') from None
    protected = [Path.home() / '.aws', Path.home() / '.codex', Path.home() / '.claude', *HARNESS_PROTECTED]
    if any(_within(resolved, p) for p in protected) or any(p.name in {'.aws', '.codex', 'hooks'} for p in resolved.parents):
        raise Refused('protected source')
    if resolved.name in {'credentials', 'auth.json', 'settings.json', 'settings.local.json', '.claude.json', 'config.toml',
                         'id_rsa', 'id_ed25519', 'assume_role.sh'} or resolved.name.startswith('.env') \
            or resolved.suffix.lower() in {'.pem', '.key'}:
        raise Refused('credential/settings source')
    if any(p.is_symlink() for p in [path, *path.parents] if str(p) not in {'/tmp', '/var'}):
        raise Refused('symlink source')
    # Codex の実行ログ・他セッションの成果物は Claude 側の根拠として受け付けない。
    if not allow_session_files:
        if _within(resolved, ARTIFACT_ROOT):
            raise Refused('co-design artifacts are not evidence sources')
        if _within(resolved, USER_TMP) and any(p.name.startswith('co-design-') for p in [resolved, *resolved.parents]):
            raise Refused('co-design work directories are not evidence sources')
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused('source must be a regular, unlinked file')
        if info.st_size > limit:
            raise Refused('source exceeds size limit; narrow the scope, do not truncate')
        data = source.read(limit + 1)
    if len(data) > limit:
        raise Refused('source exceeds size limit')
    return data, info, resolved


def read_artifact(sid, relative, limit=MAX_DOC):
    data, _, _ = read_source(art(sid) / relative, limit, allow_session_files=True)
    return data


def read_json_artifact(sid, relative, limit=MAX_DOC):
    return decode_json(read_artifact(sid, relative, limit))


# ---------------------------------------------------------------- セッション

def load_session(sid):
    return decode_json(read_artifact(sid, 'session.json'))


def save_session(session):
    write_private(art(session['sid']) / 'session.json', dump(session))


@contextmanager
def session_lock(sid, create=True):
    directory = session_dir(sid)
    if directory.is_symlink() or not directory.is_dir():
        raise Refused('unknown session')
    # 読み取り専用の確認では既存ロックだけを開き、INIT にもファイルを追加しない。
    flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY | os.O_NONBLOCK
    fd = os.open(directory / '.lock', flags | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused('session lock must be a regular, unlinked file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('another co_design command holds this session lock') from None
        yield
    finally:
        os.close(fd)


START_TOLERANCE = 5


def _ps(field, pid):
    result = subprocess.run(['ps', '-ww', '-o', field + '=', '-p', str(pid)], capture_output=True, text=True,
                            timeout=5, env={**os.environ, 'LC_ALL': 'C'})
    return result.stdout.strip()


def _owned_group(pgid, codex, started_at=None):
    """記録した pgid がまだ当該 Codex のグループと判断できるときだけ True（pid 再利用に備えコマンドと開始時刻を照合）。"""
    try:
        os.kill(pgid, 0)
    except ProcessLookupError:
        # リーダーは終了済み。グループが残っていれば元の子孫だけ（pgid はグループ存続中は再利用されない）。
        return True
    except PermissionError:
        return False
    try:
        command, started = _ps('command', pgid), _ps('lstart', pgid)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not codex or codex not in command or type(started_at) not in (int, float):
        return False
    try:
        launched = time.mktime(time.strptime(started, '%a %b %d %H:%M:%S %Y'))
    except ValueError:
        return False
    # lstart は秒単位なので数秒の許容幅で記録時刻と照合する。
    return abs(launched - started_at) <= START_TOLERANCE


def reap_stale_calls(session):
    """ランナーが呼び出し中に強制終了された痕跡（running のまま）を検出し、残ったグループを止めて STOPPED にする。"""
    stale = [c for c in session['calls'] if c.get('execution') == 'running']
    if not stale:
        return False
    for call in stale:
        pgid = call.get('pgid')
        if type(pgid) is int and pgid > 1 and _owned_group(pgid, call.get('codex'), call.get('spawned_at', call.get('started_at'))):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        call.update(ended_at=now(), execution='failed', status='INCOMPLETE')
    stop(session, 'EXEC', 'runner terminated during call')
    save_session(session)
    return True


@contextmanager
def locked_session(sid, allow_after_reap=False):
    """セッション排他ロックを取り、古い running 呼び出しを回収してからセッションを渡す。

    回収した場合はそのコマンドを拒否する。報告用の finalize だけは続行を許す。
    """
    with session_lock(sid):
        session = load_session(sid)
        if reap_stale_calls(session) and not allow_after_reap:
            raise Refused('runner terminated during a Codex call; session STOPPED (EXEC), report only')
        yield session


def require_state(session, *states):
    if session['state'] == 'STOPPED':
        raise Refused('session is STOPPED (' + session['stop']['category'] + '); report only, no retry')
    if session['state'] not in states:
        raise Refused(f'out-of-order transition: state is {session["state"]}, expected {"/".join(states)}')


def phase_timeout(phase):
    """フェーズ別の 1 回あたり上限（初回設計は長め、以降の討論は CALL_TIMEOUT）。"""
    return PROPOSAL_TIMEOUT if phase == 'proposal' else CALL_TIMEOUT


def call_codex_seconds(call):
    """終了済み呼び出しの Codex 実行時間。実行中（ended_at なし）は 0 として呼び出し側で扱う。"""
    if call.get('codex_seconds') is not None:
        return call['codex_seconds']
    if not call.get('ended_at'):
        return 0.0
    return max(0.0, call['ended_at'] - call.get('spawned_at', call['started_at']))


def codex_budget(session):
    # 旧セッション（deadline_at 方式）は計画の既定値で扱う。
    return session.get('codex_budget_seconds') or PLANS[session['plan']]['codex_budget']


def codex_used(session):
    return sum(call_codex_seconds(c) for c in session['calls'])


def codex_remaining(session):
    return codex_budget(session) - codex_used(session)


def stop(session, category, reason):
    session['state'] = 'STOPPED'
    session['stop'] = {'category': category, 'reason': reason, 'at': iso(now())}


def find_codex():
    if CODEX:
        return Path(CODEX)
    if CC.DEFAULT_CODEX.is_file():
        return CC.DEFAULT_CODEX
    found = shutil.which('codex')
    if not found:
        raise Refused('Codex CLI not found; no alternative execution attempted')
    return Path(found)


def codex_version(codex):
    try:
        result = subprocess.run([str(codex), '--version'], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused('codex --version failed') from None
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines:
        raise Refused('codex --version failed')
    return lines[0].strip()[:200]


def verified_version():
    path = ARTIFACT_ROOT / 'verified_codex_version'
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()[:400]
    return data.decode('utf-8', 'replace').strip() or None


# ---------------------------------------------------------------- 版ゲートの自動点検
# Codex 更新ごとの手動 E2E の代わりに、無害な 1 回の呼び出しで監視の前提を確かめてから版を記録する。
# 1 回あたりの上限。2026-11-20 の実測では成功 23 秒、停滞は 240 秒待っても進まなかった。
# 停滞形の失敗だけ 1 回再試行するので、最悪でも従来の 240 秒と同じ。
VERIFY_TIMEOUT = 120
VERIFY_ATTEMPTS = 2
VERIFY_MARKER = 'cross-check-probe-ok'
# 実際の秘密ファイルは読まない。guard hook が文字列だけで拒否することを確かめる（本ファイルに経路形を残さない）。
VERIFY_SECRET = '~/.aws/' + 'credentials'
VERIFY_PROMPT = (
    'Phase: verify\n'
    'This is an automated harness self-check, not a task. Use only the shell tool, one command per tool call, in this order:\n'
    f'1. echo {VERIFY_MARKER}\n'
    f"2. echo '{VERIFY_SECRET}'   (it only prints a literal string; it is expected to be refused)\n"
    'Do not run anything else and do not retry a refused command. Then reply with one line listing the names of all '
    'tools available to you, comma-separated.\n')
# E2E-3 実測の command_execution の鍵（監視・出力記録が前提にしている形）。
VERIFY_COMMAND_KEYS = {'aggregated_output', 'command', 'exit_code', 'id', 'status', 'type'}
VERIFY_FORBIDDEN_NAMES = ('node_repl', 'cua_repl', 'image_gen', 'imagegen', 'spawn_agent', 'codex_apps')


def _verify_once(codex, version):
    """無害な点検呼び出し 1 回で、hook 拒否・イベント形・道具露出を確かめる。記録はしない。(record, stalled) を返す。
    確かめる内容: 無効化キー付きで起動できる（キーが効いたことまでは保証しない）、上位イベント・item 型が許可リスト内、
    シェル item の形が E2E-3 と同じ、秘密経路を含む命令が stderr の hook 拒否行で止まり実行されない。
    禁止道具（cua_repl 等）の露出は監視が停止させるので警告として記録する。"""
    work = Path(tempfile.mkdtemp(prefix='co-design-verify-', dir=USER_TMP))
    probe = ensure_dir(work / 'probe')
    events_path, stderr_path, last = work / 'events.jsonl', work / 'stderr.log', work / 'last_message.txt'
    command = [str(codex), 'exec', '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral',
               '-C', str(probe), '--json', '-o', str(last)]
    for key in DISABLE_CONFIG:
        command += ['-c', key]
    command.append('-')
    started = time.monotonic()
    timed_out = False
    request_path = work / 'request.txt'
    request_path.write_text(VERIFY_PROMPT, encoding='utf-8')
    # 終了シグナルはフラグ化して監視ループで止める（ランナーだけ死んで Codex グループが残らないように）。
    with signal_guard(), request_path.open('rb') as request, open(events_path, 'wb') as out, open(stderr_path, 'wb') as err:
        process = subprocess.Popen(command, stdin=request, stdout=out, stderr=err, start_new_session=True)
        try:
            while process.poll() is None:
                check_signal()
                if time.monotonic() - started >= VERIFY_TIMEOUT:
                    timed_out = True
                    break
                time.sleep(0.1)
        except RunnerSignal as received:
            CC.stop_group(process)
            raise Refused(f'automatic Codex check interrupted by signal {int(received.args[0])}; version not recorded') from None
        except BaseException:
            CC.stop_group(process)
            raise
        CC.stop_group(process)
    problems, warnings = [], []
    if timed_out:
        problems.append(f'timed out after {VERIFY_TIMEOUT}s')
    elif process.returncode != 0:
        problems.append(f'Codex exit code {process.returncode} (config keys or startup may have changed)')
    completed, usage, probe_ok, secret_ran = False, None, False, False
    for raw in events_path.read_text(encoding='utf-8', errors='replace').splitlines()[:5000]:
        try:
            event = json.loads(raw)
        except ValueError:
            problems.append('malformed execution event')
            continue
        kind = event.get('type') if isinstance(event, dict) else None
        if kind not in EVENT_TYPES:
            problems.append(f'unexpected event type: {str(kind)[:40]}')
            continue
        if kind in ('error', 'turn.failed'):
            problems.append(f'Codex reported {kind} (debate would stop)')
        if kind == 'turn.completed':
            completed, usage = True, event.get('usage')
        item = event.get('item') if kind.startswith('item.') else None
        if not isinstance(item, dict):
            continue
        itype = item.get('type')
        if itype not in ITEM_TYPES:
            problems.append(f'unexpected item type: {str(itype)[:40]}')
        elif itype == 'command_execution' and kind == 'item.completed':
            text = f'{item.get("command")}\n{item.get("aggregated_output")}'
            if VERIFY_MARKER in str(item.get('command')):
                missing = VERIFY_COMMAND_KEYS - set(item)
                if missing:
                    problems.append('command_execution keys changed: missing ' + ', '.join(sorted(missing)))
                elif VERIFY_MARKER in str(item.get('aggregated_output')):
                    probe_ok = True
            if SECRET_WORDS.search(text):
                secret_ran = True
        elif itype == 'mcp_tool_call':
            server = str(item.get('server', '')).lower().replace('-', '_')
            if server in FORBIDDEN_SERVERS:
                warnings.append(f'forbidden tool server exposed: {server[:40]} (the monitor stops it)')
    if not completed:
        problems.append('missing turn.completed event')
    if not probe_ok:
        problems.append('probe shell command did not run with the expected item shape/output')
    if secret_ran:
        problems.append('secret-path command was executed: Codex guard hook is not active (re-trust it in Codex)')
    elif not any(HOOK_DENIAL_STDERR in line and SECRET_WORDS.search(line)
                 for line in stderr_path.read_text(encoding='utf-8', errors='replace').splitlines()):
        # 拒否行が秘密経路の命令に対するものかまで確かめる（別の命令の拒否で PASS にしない）。
        problems.append('hook denial line for the secret-path probe not found in stderr (guard hook inactive or denial format changed)')
    if last.is_file():
        exposed = [n for n in VERIFY_FORBIDDEN_NAMES if n in last.read_text(encoding='utf-8', errors='replace').lower()]
        if exposed:
            warnings.append('Codex reports these tools as available (the monitor stops them): ' + ', '.join(exposed))
    record = {'status': 'FAIL' if problems else 'PASS', 'version': version, 'checked_at': iso(now()),
              'elapsed_seconds': round(time.monotonic() - started, 2), 'usage': usage,
              'problems': sorted(set(problems)), 'warnings': sorted(set(warnings)), 'log_dir': str(work)}
    # 停滞形: 時間切れで、プローブは期待どおり実行され、秘密経路の命令は実行されず、
    # 時間切れに伴う 3 種以外の問題（想定外イベント・item・error・形の変化など）がない場合だけ。
    stall_only = {f'timed out after {VERIFY_TIMEOUT}s', 'missing turn.completed event',
                  'hook denial line for the secret-path probe not found in stderr (guard hook inactive or denial format changed)'}
    stalled = timed_out and probe_ok and not secret_ran and set(problems) <= stall_only
    return record, stalled


def verify_codex(codex, version):
    """点検を行い PASS なら版を記録する。停滞形の失敗に限り 1 回だけ再試行し、その経緯を記録と警告に残す。"""
    attempts = []
    while True:
        record, stalled = _verify_once(codex, version)
        attempts.append(record)
        if record['status'] == 'PASS' or not stalled or len(attempts) >= VERIFY_ATTEMPTS:
            break
    if len(attempts) > 1:
        record = dict(record, attempts=[{key: a[key] for key in ('status', 'elapsed_seconds', 'problems', 'warnings', 'log_dir')}
                                        for a in attempts])
        # 1 回目で出た警告（禁止道具の露出など）も最終結果から落とさない。
        warnings = set().union(*(a['warnings'] for a in attempts))
        if record['status'] == 'PASS':
            # 再試行で通ったことを黙って記録しない（hook の間欠的な停滞を隠さない）。
            warnings.add(f'first automatic check stalled ({attempts[0]["log_dir"]}); passed on the single retry. '
                         'If this repeats, check the Codex guard hook.')
        else:
            record['problems'] = sorted(set(record['problems']) | {'still failing after the single retry for a stall'})
        record['warnings'] = sorted(warnings)
    if record['status'] == 'PASS':
        ensure_dir(ARTIFACT_ROOT)
        write_private(ARTIFACT_ROOT / 'verified_codex_version', (version + '\n').encode('utf-8'))
        write_private(ARTIFACT_ROOT / 'verified_codex_version.json', dump(record))
    return record


def cmd_verify_codex(args):
    codex = find_codex()
    record = verify_codex(codex, codex_version(codex))
    return record, 0 if record['status'] == 'PASS' else 2


def phase_call(session, phase):
    calls = [c for c in session['calls'] if c['phase'] == phase]
    return calls[-1] if calls else None


def input_text(sid, name):
    return read_artifact(sid, 'inputs/' + INPUT_FILES[name], MAX_INPUT).decode('utf-8')


def verify_frozen(session):
    """固定済み入力・提案のハッシュが変わっていないか確認する。"""
    sid = session['sid']
    for name, digest in session['inputs'].items():
        if sha256(read_artifact(sid, 'inputs/' + INPUT_FILES[name], MAX_INPUT)) != digest:
            return 'input changed: ' + name
    for name, digest in session.get('data', {}).items():
        if sha256(read_artifact(sid, 'inputs/data/' + name, MAX_DATA_FILE)) != digest:
            return 'attached data changed: ' + name
    for side, entry in session['frozen'].items():
        if sha256(read_artifact(sid, entry['path'])) != entry['sha256']:
            return 'frozen file changed: ' + side
    return None


# ---------------------------------------------------------------- 実行監視

def _token_at(text, index):
    start, end = index, index
    stops = set(' \t\r\n\'"`;|&<>(),=')
    while start > 0 and text[start - 1] not in stops:
        start -= 1
    while end < len(text) and text[end] not in stops:
        end += 1
    return text[start:end]


def tmp_prefixes():
    base = str(USER_TMP).rstrip('/') + '/'
    prefixes = {base}
    try:
        prefixes.add(str(USER_TMP.resolve()).rstrip('/') + '/')
    except OSError:
        pass
    for prefix in list(prefixes):
        if prefix.startswith('/private/'):
            prefixes.add(prefix[len('/private'):])
    return prefixes


def normalize_command(text):
    """照合前の正規化（シェル解析できない場合の保守的な方式）: 引用符・バックスラッシュ除去、`//`・`/./` の畳み込み、小文字化。"""
    text = re.sub(r'[\\\'"]', '', text)
    return _norm_token(text)


def _norm_token(token):
    """`//`・`/./` を畳み込み小文字化する（APFS は大小無視）。"""
    previous = None
    while previous != token:
        previous = token
        token = re.sub(r'/{2,}', '/', token.replace('/./', '/'))
    return token.lower()


def _path_tokens(text):
    return [t for t in re.split(r'[\s;|&<>()=`,]+', text) if t]


def _home_roots():
    """HOME 環境変数と実際のログインユーザーのホーム（HOME が差し替えられた場合も含む）。"""
    homes = {str(Path.home()).rstrip('/').lower()}
    try:
        homes.add(pwd.getpwuid(os.getuid()).pw_dir.rstrip('/').lower())
    except (KeyError, OSError):
        pass
    return {h for h in homes if h}


def broad_roots():
    homes = _home_roots()
    roots = {'/', '/users', '/private', '/var', '/tmp', '/private/tmp', '/var/folders', '/private/var/folders',
             '~', '$home', '${home}', '$tmpdir', '${tmpdir}'} | homes
    roots |= {r + '/.claude' for r in ('~', '$home', '${home}', *homes)}
    roots |= {p.rstrip('/').lower() for p in tmp_prefixes()}
    return roots


def _escapes_root(path):
    return path == '..' or path.startswith('../')


def _root_hit(token, roots):
    path = posixpath.normpath(_norm_token(token))
    return 'broad-root:' + path[:40] if path in roots or _escapes_root(path) else None


def _glob_hit(token, roots):
    """広い根やホームの .claude 直下へのグロブ・ブレース展開（`~/.c*`、`~/.c{laude,}` など）。"""
    token = _norm_token(token)
    positions = [token.find(c) for c in GLOB_CHARS if c in token]
    if not positions:
        return None
    parent = posixpath.dirname(token[:min(positions)])
    if not parent:
        return None
    path = posixpath.normpath(parent)
    claude_dirs = {r + '/.claude' for r in ('~', '$home', '${home}', *_home_roots())}
    if path in roots or _escapes_root(path) or any(path == d or path.startswith(d + '/') for d in claude_dirs):
        return 'broad-glob:' + path[:40]
    return None


def _marker_hits(text, sid):
    """正規化済みの文字列に含まれる独立性マーカー。"""
    hits = []
    for match in re.finditer(r'\.claude/projects(?![\w-])', text):
        token = _token_at(text, match.start())
        tail = token[token.find('.claude/projects'):]
        if '..' in tail or not re.match(r'\.claude/projects/[^/]+/memory(?:/|$)', tail):
            hits.append('.claude/projects')
    if '/tmp/claude-' in text:
        hits.append('/tmp/claude-')
    allowed = f'.local/co-design/{sid}/artifacts/inputs'
    for match in re.finditer(r'\.local/co-design(?![\w-])', text):
        token = _token_at(text, match.start())
        if allowed not in token or '..' in token:
            hits.append('.local/co-design')
    if any(prefix.lower() + 'co-design-' in text for prefix in tmp_prefixes()):
        hits.append('user-tmp/co-design-')
    if re.search(r'\$\{?tmpdir\b|darwin_user_temp_dir', text):
        hits.append('$TMPDIR')
    return hits


def shell_segments(command, depth=0):
    """シェルコマンドを区切り（; | & && || 括弧・リダイレクト）ごとの語列にする。解析できなければ None。"""
    try:
        words = [str(w) for w in command] if isinstance(command, list) else shlex.split(command)
    except ValueError:
        return None
    if len(words) >= 3 and posixpath.basename(words[0]) in SHELLS and re.fullmatch(r'-[a-z]*c[a-z]*', words[1]):
        script = words[2]
    elif isinstance(command, list):
        script = shlex.join(words)
    else:
        script = command
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments, current = [], []
    for token in tokens:
        if token and set(token) <= set('();<>|&'):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    result = []
    for segment in segments:
        # 入れ子の `sh -c '...'` も展開する。
        if depth < 3 and len(segment) >= 3 and posixpath.basename(segment[0]) in SHELLS and re.fullmatch(r'-[a-z]*c[a-z]*', segment[1]):
            nested = shell_segments(segment[2], depth + 1)
            if nested is None:
                return None
            result += nested
        else:
            result.append(segment)
    return result


def _search_paths(name, args):
    """rg/grep/ag の経路位置の語と再帰指定を返す。検索パターン（最初の位置引数、-e の値）は除外する。"""
    paths, positional, explicit = [], [], False
    recursive = name in ('rg', 'ag')
    i, options = 0, True
    while i < len(args):
        arg = args[i]
        following = args[i + 1] if i + 1 < len(args) else ''
        if options and arg == '--':
            options = False
            i += 1
            continue
        if options and arg.startswith('--'):
            key, eq, value = arg.partition('=')
            if key == '--regexp':
                explicit = True
            elif key in LONG_PATH_VALUE:
                explicit = explicit or key == '--file'
                paths.append(value if eq else following)
            elif name in GREP_TOOLS and (key in ('--recursive', '--dereference-recursive') or arg == '--directories=recurse'):
                recursive = True
            if (key in ('--regexp', *LONG_PATH_VALUE) or key in LONG_VALUE) and not eq:
                if name in GREP_TOOLS and key == '--directories' and following == 'recurse':
                    recursive = True
                i += 2
            else:
                i += 1
            continue
        if options and arg.startswith('-') and arg != '-':
            cluster, consumed = arg[1:], False
            for j, ch in enumerate(cluster):
                if name in GREP_TOOLS and ch in 'rR':
                    recursive = True
                if ch in 'ef' or ch in SHORT_VALUE.get(name, ''):
                    value = cluster[j + 1:]
                    if not value:
                        value, consumed = following, True
                    if ch == 'e':
                        explicit = True
                    elif ch == 'f':
                        explicit = True
                        paths.append(value)
                    elif ch == 'd' and name in GREP_TOOLS and value == 'recurse':
                        recursive = True
                    break
            i += 2 if consumed else 1
            continue
        positional.append(arg)
        i += 1
    if not explicit and positional:
        positional = positional[1:]
    return paths + positional, recursive


def _segment_paths(segment):
    """(マーカー検査する語, 広い根を検査する語, 一覧・探索系か, 入れ子の語列) を返す。"""
    words = list(segment)
    while words:
        head = posixpath.basename(words[0])
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0]) or head in WRAPPERS:
            words = words[1:]
        elif head == 'timeout' and len(words) > 1:
            words = words[2:]
        else:
            break
    if not words:
        return [], [], False, []
    name, args = posixpath.basename(words[0]).lower(), words[1:]
    if name in SEARCH_TOOLS:
        paths, recursive = _search_paths(name, args)
        return [words[0]] + paths, paths, recursive, []
    if name == 'find':
        starts, i = [], 0
        while i < len(args) and args[i] in ('-H', '-L', '-P', '-E', '-X', '-d', '-s', '-x', '-f'):
            if args[i] == '-f' and i + 1 < len(args):
                starts.append(args[i + 1])
                i += 1
            i += 1
        while i < len(args) and not args[i].startswith(('-', '(', '!', ')')):
            starts.append(args[i])
            i += 1
        markers, nested = list(starts), []
        while i < len(args):
            if args[i] in FIND_PATTERN_OPTIONS:
                i += 2
                continue
            if args[i] in ('-exec', '-execdir', '-ok', '-okdir'):
                j = i + 1
                while j < len(args) and args[j] not in (';', '+'):
                    j += 1
                nested.append(args[i + 1:j])
                i = j + 1
                continue
            if not args[i].startswith('-'):
                markers.append(args[i])
            i += 1
        return [words[0]] + markers, starts, True, nested
    if name == 'xargs':
        rest = list(args)
        while rest and (rest[0].startswith('-') or rest[0] == '{}' or rest[0].isdigit()):
            rest = rest[1:]
        return [words[0]], [], True, [rest] if rest else []
    paths = [words[0]]
    for arg in args:
        if arg.startswith('-'):
            key, eq, value = arg.partition('=')
            if eq:
                paths.append(value)
        else:
            paths.append(arg)
    return paths, paths[1:], name in LISTING_TOOLS, []


def _shell_hits(segments, sid, roots, depth=0):
    hits = []
    for segment in segments:
        markers, root_tokens, active, nested = _segment_paths(segment)
        for token in markers:
            normalized = _norm_token(token)
            hits += _marker_hits(normalized, sid)
            glob = _glob_hit(token, roots)
            if glob:
                hits.append(glob)
        if active:
            hits += [h for h in (_root_hit(t, roots) for t in root_tokens) if h]
        if nested and depth < 3:
            hits += _shell_hits(nested, sid, roots, depth + 1)
    return hits


def independence_hits(text, sid, shell=False, command=None):
    """相手側の作業領域を示すマーカー。検出後停止（事前遮断ではない）。

    シェルコマンドは語に分けて経路位置の語だけを検査し、検索パターンは除外する。解析できない場合は保守的な全文検査に戻る。
    """
    sid = sid.lower()
    roots = broad_roots()
    if shell:
        segments = shell_segments(command if command is not None else text)
        if segments is not None:
            return sorted(set(_shell_hits(segments, sid, roots)))
    normalized = normalize_command(text)
    hits = _marker_hits(normalized, sid)
    hits += [h for h in (_glob_hit(t, roots) for t in _path_tokens(normalized)) if h]
    if shell:
        hits += legacy_broad_root_hits(normalized, roots)
    return sorted(set(hits))


def legacy_broad_root_hits(text, roots):
    """シェル解析に失敗した場合の保守的な判定（全語を対象にする）。"""
    tokens = _path_tokens(text)
    active = False
    for i, token in enumerate(tokens):
        name = posixpath.basename(token)
        if name in LISTING_TOOLS or name in ('rg', 'ag'):
            active = True
        elif name in GREP_TOOLS and any(re.fullmatch(r'-[a-z]*r[a-z]*', t) or t.startswith(('--recursive', '--dereference-recursive', '--directories=recurse'))
                                        for t in tokens[i + 1:]):
            active = True
    if not active:
        return []
    return [h for h in (_root_hit(t, roots) for t in tokens if not t.startswith('-')) if h]


# rg/grep -n・nl・cat -n の行頭（`path:12:`・`path-12-`・`  12\t` 等）。本文と分けて経路部分だけを検査する。
LINE_PREFIX = re.compile(r'(?P<prefix>(?:[^\s:]+[:-])?\s*\d+[:\t-] ?)(?P<body>.*)', re.S)
CONTENT_FILE_LIMIT = 2 * 1024 * 1024
CONTENT_FILES = 40


def _project_file_lines(candidate, cache):
    """作業ルート配下の実在ファイル（.local・リンクを除く）の行集合。対象外は None。"""
    if candidate in cache:
        return cache[candidate]
    lines = None
    path = Path(candidate if posixpath.isabs(candidate) else PROJECT / candidate)
    try:
        resolved = path.resolve(strict=True)
        if (len(cache) < CONTENT_FILES and _within(resolved, PROJECT) and not _within(resolved, PROJECT / '.local')
                and not path.is_symlink() and resolved.is_file() and resolved.stat().st_size <= CONTENT_FILE_LIMIT):
            lines = {l.strip() for l in resolved.read_text(encoding='utf-8', errors='replace').splitlines()}
    except (OSError, ValueError):
        lines = None
    cache[candidate] = lines
    return lines


def _content_free_text(text, command):
    """作業ルート内ファイルの本文と一致する行の本文部分を除いた検査対象テキスト。
    ファイル本文に書かれた経路形文字列（テストデータ・文書）での誤停止を防ぎ、それ以外の行（見出し・エラー・一覧・
    作業ルート外や .local の内容）と行頭の経路部分は従来どおり全文を検査する。"""
    cache = {}
    named = []
    if command is not None:
        words = shell_segments(command)
        flat = [w for segment in words for w in segment] if words else _path_tokens(normalize_command(str(command)))
        named = [w for w in flat if isinstance(w, str) and not w.startswith('-') and ('/' in w or '.' in w)]
    known = set()
    for candidate in named:
        lines = _project_file_lines(candidate, cache)
        if lines:
            known |= lines
    kept = []
    for line in text.splitlines():
        match = LINE_PREFIX.fullmatch(line)
        prefix, body = (match.group('prefix'), match.group('body')) if match else ('', line)
        source = prefix.split(':', 1)[0] if ':' in prefix else ''
        pool = known | (_project_file_lines(source, cache) or set()) if source else known
        kept.append(prefix if pool and body.strip() in pool else line)
    return '\n'.join(kept)


def output_hits(text, sid, command=None):
    """道具出力に現れた相手側作業領域の経路形パターン。command（シェル命令）があれば作業ルート内ファイル本文の行は除く。"""
    if command is not None:
        text = _content_free_text(text, command)
    hits = [name for name, pattern in OUTPUT_MARKERS if pattern.search(text)]
    for match in CO_DESIGN_ARTIFACT.finditer(text):
        if match.group(1).lower() != sid.lower() or not match.group(2).lower().startswith('artifacts/inputs'):
            hits.append('co-design-artifact-path')
            break
    return sorted(set(hits))


def tool_kind(item):
    if item['type'] == 'command_execution':
        return 'shell'
    if item['type'] == 'web_search':
        return 'web'
    server = str(item.get('server', '')).lower().replace('_', '-')
    return server


class Monitor:
    """events.jsonl の新規行を逐次検査し、最初の違反で停止させる。"""

    def __init__(self, sid, scope, call_index, evidence_dir):
        self.sid = sid
        self.allowed = set(scope.get('allowed_tools', []))
        self.call_index = call_index
        self.evidence_dir = evidence_dir
        self.violations = []
        self.completed = False
        self.usage = None
        self.seen = set()
        self.recorded = set()
        self.tool_calls = 0
        self.output_bytes = 0
        self.tool_counts = {k: 0 for k in sorted(TOOL_ITEMS)}
        self.tools = []
        self.anonymous = 0
        # 停止時に部分結果として残す Codex の途中メッセージ（独立性・秘密違反時は残さない）。
        self.messages = []
        self.message_bytes = 0

    def violate(self, category, detail):
        self.violations.append({'category': category, 'detail': detail})

    def feed(self, raw):
        if not raw.strip():
            return
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError):
            return self.violate('EXEC', 'malformed execution event')
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            return self.violate('EXEC', 'malformed execution event')
        kind = event['type']
        if kind not in EVENT_TYPES:
            return self.violate('POLICY', 'unexpected event type: ' + kind[:40])
        if kind in ('error', 'turn.failed'):
            # E2E-3 で hook 拒否の形式が確定するまで error/turn.failed は停止扱い。
            return self.violate('POLICY', 'Codex reported ' + kind + ' (hook denial or execution error)')
        if kind == 'turn.completed':
            self.completed = True
            self.usage = event.get('usage') if isinstance(event.get('usage'), dict) else None
        if kind not in ('item.started', 'item.updated', 'item.completed'):
            return
        item = event.get('item')
        if not isinstance(item, dict) or not isinstance(item.get('type'), str):
            return self.violate('EXEC', 'malformed execution item')
        itype = item['type']
        if itype == 'file_change':
            return self.violate('POLICY', 'file_change item')
        if itype not in ITEM_TYPES:
            return self.violate('POLICY', 'unexpected item type: ' + itype[:40])
        if item.get('status') not in KNOWN_STATUSES:
            return self.violate('POLICY', 'unknown item status: ' + str(item.get('status'))[:40])
        if itype == 'agent_message' and kind == 'item.completed' and self.message_bytes < MAX_PARTIAL:
            data = str(item.get('text', '')).encode('utf-8')[:MAX_PARTIAL - self.message_bytes]
            self.messages.append(data.decode('utf-8', 'ignore'))
            self.message_bytes += len(data)
        if itype in TOOL_ITEMS:
            self.tool_item(kind, item)

    def tool_item(self, kind, item):
        item_id = item.get('id')
        if not isinstance(item_id, str) or not item_id:
            self.anonymous += 1
            item_id = f'anon-{self.anonymous}'
        if item_id not in self.seen:
            self.seen.add(item_id)
            self.tool_calls += 1
            self.tool_counts[item['type']] += 1
            if self.tool_calls > MAX_TOOL_CALLS:
                return self.violate('BUDGET', f'tool calls exceeded {MAX_TOOL_CALLS}')
        text = describe(item)
        # 結果を破棄すべき独立性・秘密マーカーを先に判定する。
        shell = item['type'] == 'command_execution'
        command = item.get('command')
        command = command if shell and isinstance(command, (str, list)) else None
        hits = independence_hits(text, self.sid, shell=shell, command=command)
        if hits:
            return self.violate('INDEPENDENCE', 'independence marker: ' + ', '.join(hits))
        if SECRET_WORDS.search(text):
            return self.violate('SECRET', 'secret path marker in tool input')
        server = str(item.get('server', '')).lower().replace('-', '_')
        tool = str(item.get('tool', '')).lower()
        if item['type'] == 'mcp_tool_call' and (server in FORBIDDEN_SERVERS or any(w in tool for w in FORBIDDEN_TOOL_WORDS)):
            return self.violate('POLICY', 'forbidden tool: ' + (server + '/' + tool)[:80])
        if tool_kind(item) not in self.allowed:
            return self.violate('POLICY', 'tool not allowed by scope: ' + tool_kind(item)[:40])
        finished = kind == 'item.completed' or item.get('status') in ('completed', 'failed')
        if finished and item_id not in self.recorded:
            self.recorded.add(item_id)
            output = tool_output(item)
            hits = output_hits(output, self.sid, command)
            if hits:
                return self.violate('INDEPENDENCE', 'independence path in tool output: ' + ', '.join(hits))
            if HOOK_DENIAL.search(denial_text(item)):
                return self.violate('POLICY', 'hook denial reported by a failed tool call')
            self.output_bytes += len(output.encode('utf-8'))
            if self.output_bytes > MAX_TOOL_OUTPUT:
                return self.violate('BUDGET', 'cumulative tool output exceeded 4 MiB')
            self.record_tool(item_id, item, text, output)

    def record_tool(self, item_id, item, text, output):
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', item_id)[:60]
        raw_dir = ensure_dir(self.evidence_dir / 'codex_raw')
        # 置換後の名前が衝突しないよう元 ID の短いハッシュを付ける。
        name = f'{self.call_index}-{safe}-{sha256(item_id.encode("utf-8"))[:8]}'
        output_bytes = output.encode('utf-8')
        write_private(raw_dir / (name + '.out'), output_bytes)
        write_private(raw_dir / (name + '.in'), text.encode('utf-8'))
        entry = {'call': self.call_index, 'item_id': item_id, 'kind': item['type'],
                 'args_sha256': sha256(text.encode('utf-8')), 'output_sha256': sha256(output_bytes),
                 'output_bytes': len(output_bytes), 'status': item.get('status'), 't': round(now(), 3), 'raw': name}
        if item['type'] == 'command_execution':
            entry['command'] = text
        elif item['type'] == 'mcp_tool_call':
            entry['server'], entry['tool'] = str(item.get('server')), str(item.get('tool'))
        else:
            entry['query'] = text
        with open(self.evidence_dir / 'codex_tools.jsonl', 'a', encoding='utf-8') as log:
            log.write(json.dumps(entry, ensure_ascii=False) + '\n')
        self.tools.append({**entry, 'text': text, 'output': output})


def describe(item):
    if item['type'] == 'command_execution':
        command = item.get('command')
        return ' '.join(map(str, command)) if isinstance(command, list) else str(command)
    if item['type'] == 'mcp_tool_call':
        try:
            args = json.dumps(item.get('arguments'), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            args = str(item.get('arguments'))
        return f'{item.get("server")}/{item.get("tool")} {args}'
    query = item.get('query')
    if isinstance(query, str):
        return query
    return json.dumps({k: v for k, v in item.items() if k not in ('id', 'type', 'status')}, ensure_ascii=False, default=str)


def denial_text(item):
    # 正常出力中の文言（文書を読んだ場合など）で誤停止しないよう、失敗した呼び出しだけを見る。
    if item['type'] == 'command_execution':
        failed = item.get('status') == 'failed' or item.get('exit_code') not in (0, None)
        return str(item.get('aggregated_output') or '') if failed else ''
    if item['type'] == 'mcp_tool_call':
        return json.dumps(item.get('error'), ensure_ascii=False, default=str) if item.get('error') else ''
    return ''


def tool_output(item):
    if item['type'] == 'command_execution':
        return str(item.get('aggregated_output') or '')
    if item['type'] == 'mcp_tool_call':
        parts = [item.get('result'), item.get('error')]
        return '\n'.join(json.dumps(p, ensure_ascii=False, default=str) for p in parts if p is not None)
    return json.dumps({k: v for k, v in item.items() if k not in ('id', 'type', 'status')}, ensure_ascii=False, default=str)


def load_tools(sid):
    """セッション内の過去の呼び出しを含む道具出力記録（旧セッションの一時領域 → 恒久領域、重複は除く）。"""
    tools, seen = [], set()
    for evidence in reversed(evidence_dirs(sid)):
        log = evidence / 'codex_tools.jsonl'
        if not log.is_file() or log.is_symlink():
            continue
        for raw in log.read_text(encoding='utf-8').splitlines():
            entry = json.loads(raw)
            key = (entry.get('call'), entry.get('item_id'))
            if key in seen:
                continue
            seen.add(key)
            base = evidence / 'codex_raw' / Path(str(entry.get('raw', ''))).name
            try:
                entry['text'] = Path(str(base) + '.in').read_text(encoding='utf-8')
                entry['output'] = Path(str(base) + '.out').read_text(encoding='utf-8')
            except OSError:
                entry['text'], entry['output'] = entry.get('command', ''), ''
            tools.append(entry)
    return tools


# ---------------------------------------------------------------- 根拠の連結

def normalize_path(path):
    path = path.strip().strip('`').replace('\\', '/')
    project = str(PROJECT).rstrip('/') + '/'
    if path.startswith(project):
        path = path[len(project):]
    while path.startswith('./'):
        path = path[2:]
    return path


def file_ref(ref):
    """`path:Lx-Ly` 形式のリポジトリ引用だけを返す（入力行・URL は除く）。"""
    ref = ref.strip()
    if INPUT_REF.fullmatch(ref) or ref.startswith(('http://', 'https://')):
        return None
    return FILE_REF.fullmatch(ref)


def _command_words(segment):
    """環境変数代入・ラッパー・timeout を外した (コマンド名, 引数)。"""
    words = list(segment)
    while words:
        head = posixpath.basename(words[0])
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0]) or head in WRAPPERS:
            words = words[1:]
        elif head == 'timeout' and len(words) > 1:
            words = words[2:]
        else:
            break
    return (posixpath.basename(words[0]).lower(), words[1:]) if words else ('', [])


def _names_path(word, candidates):
    word = word.strip().strip('`')
    return any(word == c or word.endswith('/' + c) for c in candidates)


def _short_flag(args, letter, long_name):
    return any(a == long_name or (a.startswith('-') and not a.startswith('--') and letter in a[1:]) for a in args)


def _sed_ranges(args):
    """sed -n の印字範囲（'a,bp'・'ap'・';' 区切り・-e 指定）。"""
    ranges = []
    for arg in args:
        for part in arg.split(';'):
            match = re.fullmatch(r'\s*(\d+)(?:,(\d+))?p\s*', part)
            if match:
                ranges.append((int(match.group(1)), int(match.group(2) or match.group(1))))
    return ranges


def _pipelines(text):
    """シェルコマンドを「パイプライン → コマンド語列」に分ける（`;`・`&&`・`||`・`&`・括弧で区切り、`|` でつなぐ）。

    `sh -c '...'` は 1 段だけ展開する。出力リダイレクト先は除き、入力リダイレクト元は引数として残す。解析できなければ None。
    """
    try:
        words = shlex.split(text)
    except ValueError:
        return None
    script = words[2] if len(words) >= 3 and posixpath.basename(words[0]) in SHELLS and \
        re.fullmatch(r'-[a-z]*c[a-z]*', words[1]) else text
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    pipelines, pipeline, command, skip = [], [], [], False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token and set(token) <= set('();<>|&'):
            if token in ('|', '|&'):
                if command:
                    pipeline.append(command)
                command = []
            elif token == '<':
                continue
            elif set(token) <= set('<>&') and '>' in token:
                skip = True
            else:
                if command:
                    pipeline.append(command)
                if pipeline:
                    pipelines.append(pipeline)
                pipeline, command = [], []
        else:
            command.append(token)
    if command:
        pipeline.append(command)
    if pipeline:
        pipelines.append(pipeline)
    return pipelines


def _head_count(args):
    for i, arg in enumerate(args):
        match = re.fullmatch(r'-n?(\d+)', arg)
        if match:
            return int(match.group(1))
        if arg == '-n' and i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
    return 10


def _pipeline_covers(pipeline, candidates, first, last, output_lines):
    """1 本のパイプラインが対象ファイルの first..last 行を読んだか。読み取り後に絞り込み（head・tail・grep など）があれば False。"""
    commands = [_command_words(c) for c in pipeline]
    reader = next((i for i, (name, args) in enumerate(commands)
                   if name in ('sed', 'cat', 'nl', 'bat', 'head') and any(_names_path(a, candidates) for a in args)), None)
    if reader is None:
        return False
    name, args = commands[reader]
    rest = commands[reader + 1:]
    if name == 'sed':
        if not _short_flag(args, 'n', '--quiet'):
            return False
        ranges = _sed_ranges([a for a in args if not a.startswith('-')])
    elif name == 'head':
        ranges = [(1, _head_count(args))]
    elif rest and rest[0][0] == 'sed' and _short_flag(rest[0][1], 'n', '--quiet'):
        # `nl -ba file | sed -n 'a,bp'`: 行番号付けの直後の範囲指定は読み取り範囲そのもの。
        ranges, rest = _sed_ranges([a for a in rest[0][1] if not a.startswith('-')]), rest[1:]
    else:
        ranges = None
    # 後段は行番号付け・素通しだけを許す（head・tail・grep などの絞り込みがあれば mention）。
    if any(later not in ('nl', 'cat') for later, _ in rest):
        return False
    if ranges is None:
        return output_lines >= last
    return any(a <= first and last <= b for a, b in ranges) and output_lines >= last - first + 1


def read_covers(tool, candidates, first, last):
    """道具記録がファイルの first..last 行を読んだと判断できれば True（D5 の excerpt 判定）。

    完了した shell 道具だけを対象にし、次のどれかを満たすとき:
    - 同じパイプラインで対象ファイルを読む `sed -n 'a,bp'`・`head -n N`・`cat`/`nl` の直後の `sed -n` が引用範囲を覆い、
      出力が引用範囲の行数以上ある
    - `cat`/`nl` でファイル全体を読み、出力が last 行以上ある
    - `rg -n`/`grep -n` の出力に引用範囲の全行番号がある（単一ファイル検索は `n:`、複数ファイルは `path:n:`、文脈行は `-`）
    読み取りの後段に絞り込み（head・tail・grep など）があるもの、空出力、別パイプラインの範囲は mention。
    """
    kind = tool.get('kind') or ('mcp_tool_call' if tool.get('server') else 'command_execution')
    if kind != 'command_execution' or tool.get('status') != 'completed':
        return False
    text, output = str(tool.get('text', '')), str(tool.get('output', ''))
    output_lines = len(output.splitlines())
    pipelines = _pipelines(text)
    if not pipelines:
        return False
    if any(_pipeline_covers(p, candidates, first, last, output_lines) for p in pipelines):
        return True
    searched, single = False, False
    for pipeline in pipelines:
        for segment in pipeline:
            name, args = _command_words(segment)
            if name in SEARCH_TOOLS and _short_flag(args, 'n', '--line-number'):
                searched = True
                single = single or any(_names_path(a, candidates) for a in args)
    if not searched:
        return False
    seen = set()
    for line in output.splitlines():
        for candidate in candidates:
            match = re.match(r'(?:\S*/)?' + re.escape(candidate) + r'[:-](\d+)[:-]', line)
            if match:
                seen.add(int(match.group(1)))
        if single:
            match = re.match(r'(\d+)[:-]', line)
            if match:
                seen.add(int(match.group(1)))
    return all(n in seen for n in range(first, last + 1))


def link_ref_detail(ref, tools, line_counts, known_ids=()):
    """引用 ref を道具出力に結び付け、(区分, 道具 item id 一覧, 強さ) を返す。強さは linked のときだけ excerpt/mention。"""
    ref = ref.strip()
    match = INPUT_REF.fullmatch(ref)
    if match:
        first, last = int(match.group(2)), int(match.group(3) or match.group(2))
        ok = 1 <= first <= last <= line_counts.get(match.group(1), 0)
        return ('input' if ok else 'unbacked'), [], None
    if EVIDENCE_ID.fullmatch(ref):
        return ('linked', [], 'mention') if ref in known_ids else ('unbacked', [], None)
    if ref.startswith(('http://', 'https://')):
        url = ref.rstrip('/')
        # 文書内アンカー（#節名）付きの引用は、道具記録にアンカーなしの URL しか残らないことがある（2026-11-24 実測）。
        # 同じページなので、アンカーを除いた URL でも照合する。
        candidates = {url, url.split('#', 1)[0].rstrip('/')} - {''}
        ids = [t['item_id'] for t in tools
               if any(c in t.get('text', '') or c in t.get('output', '') for c in candidates)]
        return ('linked', ids, 'mention') if ids else ('unbacked', [], None)
    match = file_ref(ref)
    if match:
        parts = [p for p in normalize_path(match.group('path')).split('/') if p]
        # 道具の入力または出力にパス末尾（2 要素以上、単一要素ならその名前）が現れれば連結とみなす。
        candidates = {'/'.join(parts[i:]) for i in range(max(len(parts) - 1, 1))} if parts else set()
        hits = [t for t in tools
                if any(c and (c in t.get('text', '') or c in t.get('output', '')) for c in candidates)]
        if not hits:
            return 'unbacked', [], None
        first, last = int(match.group('a')), int(match.group('b') or match.group('a'))
        # 引用行範囲を覆う読み取りがあれば excerpt、経路の一致だけなら mention（D5）。
        strength = 'excerpt' if first <= last and any(read_covers(t, candidates, first, last) for t in hits) else 'mention'
        return 'linked', [t['item_id'] for t in hits], strength
    compact = ' '.join(ref.split())
    if len(compact) >= 4:
        ids = [t['item_id'] for t in tools if compact in ' '.join(t.get('text', '').split())
               or (t.get('server') and t['server'] in compact)]
        if ids:
            return 'linked', ids, 'mention'
    return 'unbacked', [], None


def link_ref(ref, tools, line_counts, known_ids=()):
    """引用 ref を道具出力に結び付け、(区分, 道具 item id 一覧) を返す。"""
    return link_ref_detail(ref, tools, line_counts, known_ids)[:2]


def cited_file_hashes(refs):
    hashes = {}
    for ref in refs:
        match = file_ref(ref)
        if not match:
            continue
        rel = normalize_path(match.group('path'))
        if rel.startswith('/') or '..' in rel.split('/'):
            continue
        try:
            data, _, _ = read_source(PROJECT / rel, MAX_SOURCE)
        except (Refused, OSError):
            continue
        hashes[rel] = sha256(data)
    return hashes


def result_refs(phase, result):
    """結果中の (id, kind, ref) を列挙する。"""
    refs = []
    if phase == 'proposal':
        for entry in result['evidence']:
            refs.append((entry['id'], entry['kind'], entry['ref']))
        return refs
    groups = ('issues',) if phase == 'fidelity' else ('critique_of_claude', 'responses_to_claude_critique')
    for group in groups:
        for entry in result[group]:
            for ref in entry['evidence_refs']:
                refs.append((entry['issue_id'], 'ref', ref))
    return refs


def _ref_target(ref):
    """接頭辞 ID と引用先の一致確認に使う対象（入力文書名またはリポジトリ相対パス）。"""
    match = INPUT_REF.fullmatch(ref.strip())
    if match:
        return match.group(1)
    match = file_ref(ref)
    return normalize_path(match.group('path')) if match else None


def link_context(session, phase, result):
    """引用の解決に使う情報（入力行数、両側の根拠 ID、ledger ID、参照可能な過去出力）。"""
    sid = session['sid']
    counts = {name: len(input_text(sid, name).splitlines()) for name in INPUT_FILES}
    try:
        counts['synthesis'] = len(read_artifact(sid, 'synthesis.json').decode('utf-8').splitlines())
    except (Refused, OSError, UnicodeError):
        pass
    if phase == 'proposal':
        proposal = result
    else:
        try:
            proposal = read_json_artifact(sid, 'codex/proposal.json', MAX_RESULT)
        except (Refused, SchemaError, OSError):
            proposal = {}
    try:
        ledger_ids = {i['id'] for i in read_json_artifact(sid, 'ledger.json')['issues']}
    except (Refused, SchemaError, OSError, KeyError, TypeError):
        ledger_ids = set()
    codex_evidence = {e['id']: e for e in proposal.get('evidence', [])}
    claude_paths = {e['id']: normalize_path(e['path']) for e in session['evidence']}
    claude_lines = {e['id']: tuple(e['lines']) for e in session['evidence']}
    earlier = {c['phase'] for c in session['calls'] if c.get('execution') == 'ok'}
    if phase != 'proposal':
        earlier.add('proposal')
    return {'line_counts': counts, 'codex_evidence': codex_evidence, 'claude_paths': claude_paths,
            'known': set(codex_evidence) | set(claude_paths), 'ledger_ids': ledger_ids, 'earlier': earlier,
            'claude_lines': claude_lines}


def link_citation(ref, tools, ctx, depth=0):
    """前置 ID を外して引用を道具出力・入力・根拠 ID に結び付ける。

    戻り値の link は linked/unbacked/input/inference。道具出力はセッション内の全呼び出し分（過去の呼び出しを含む）。
    """
    ref = ref.strip()
    prefix, rest = None, ref
    match = PREFIXED_REF.fullmatch(ref)
    if match:
        prefix, rest = match.group(1), match.group(2).strip()
    elif EVIDENCE_ID.fullmatch(ref):
        prefix, rest = ref, ''
    entry = {'prefix': prefix, 'via': None, 'prefix_mismatch': False, 'tool_items': [], 'strength': None}
    if not re.search(r'[A-Za-z0-9]', rest):
        # 前置 ID だけの引用は ID が指す根拠で判定する。登録済み Claude 根拠は行範囲のバイト複製なので excerpt。
        if prefix in ctx['claude_paths']:
            return {**entry, 'link': 'linked', 'via': 'claude-evidence', 'strength': 'excerpt'}
        evidence = ctx['codex_evidence'].get(prefix)
        if evidence and depth == 0:
            if evidence['kind'] == 'inference':
                return {**entry, 'link': 'inference', 'via': 'codex-evidence'}
            resolved = link_citation(evidence['ref'], tools, ctx, depth + 1)
            return {**resolved, 'prefix': prefix, 'via': 'codex-evidence', 'prefix_mismatch': False}
        return {**entry, 'link': 'unbacked'}
    if rest.lower().startswith('inference'):
        return {**entry, 'link': 'inference'}
    status, ids, strength = link_ref_detail(rest, tools, ctx['line_counts'], ctx['known'])
    if status == 'unbacked':
        ledger = LEDGER_REF.fullmatch(rest)
        earlier = EARLIER_REF.fullmatch(rest)
        if ledger and ledger.group(1) in ctx['ledger_ids']:
            status = 'input'
        elif earlier and earlier.group(1).lower() in ctx['earlier']:
            status = 'input'
    cited = file_ref(rest)
    if status == 'linked' and strength == 'mention' and cited and prefix in ctx.get('claude_lines', {}):
        # 登録済み Claude 根拠（行範囲のバイト複製）と同じファイルで、その範囲内の引用なら excerpt。
        first, last = int(cited.group('a')), int(cited.group('b') or cited.group('a'))
        low, high = ctx['claude_lines'][prefix]
        if normalize_path(cited.group('path')) == ctx['claude_paths'][prefix] and low <= first <= last <= high:
            strength = 'excerpt'
    if prefix:
        # 前置 ID と引用先の食い違いは失敗にせず引用先で判定し、件数だけ記録する。
        if prefix in ctx['codex_evidence']:
            expected = _ref_target(ctx['codex_evidence'][prefix]['ref'])
        else:
            expected = ctx['claude_paths'].get(prefix)
        actual = _ref_target(rest)
        entry['prefix_mismatch'] = bool(expected and actual and expected != actual)
    return {**entry, 'link': status, 'tool_items': ids[:20], 'strength': strength if status == 'linked' else None}


def link_result(session, phase, result, tools):
    ctx = link_context(session, phase, result)
    links = []
    for ident, kind, ref in result_refs(phase, result):
        if kind == 'inference':
            links.append({'id': ident, 'kind': kind, 'ref': ref, 'link': 'inference', 'tool_items': [],
                          'prefix': None, 'via': None, 'prefix_mismatch': False, 'strength': None})
            continue
        links.append({'id': ident, 'kind': kind, 'ref': ref, **link_citation(ref, tools, ctx)})
    totals = {k: sum(1 for l in links if l['link'] == k) for k in LINK_KINDS}
    # 既存キー linked は互換のため残す（linked = excerpt + mention）。
    totals.update({k: sum(1 for l in links if l['link'] == 'linked' and l.get('strength') == k) for k in STRENGTH_KINDS})
    totals['prefix_mismatch'] = sum(1 for l in links if l['prefix_mismatch'])
    cited = [l['ref'] for l in links] + [PREFIXED_REF.fullmatch(l['ref'].strip()).group(2)
                                          for l in links if PREFIXED_REF.fullmatch(l['ref'].strip())]
    return {'links': links, 'counts': totals, 'cited_files': cited_file_hashes(cited)}


def codex_evidence_ids(session, proposal=None):
    if proposal is None:
        try:
            proposal = read_json_artifact(session['sid'], 'codex/proposal.json')
        except (Refused, SchemaError, OSError):
            return set()
    return {e['id'] for e in proposal.get('evidence', [])}


# ---------------------------------------------------------------- 結果の意味検証

LOOSE_ID = re.compile(r'([A-Za-z]{1,2})[-_ ]?0*(\d{1,4})')


def normalize_ids(result, phase):
    """形式だけ外れた自分側 ID（x1・X-01・C-1 等）を正規形に直し、結果内の同じ文字列値もそろえる。
    正しい ID は変えない。正規化後に重複・不一致が残るものは従来どおり意味検証で拒否する。"""
    if phase == 'proposal':
        prefix = 'X' if result.get('side') == 'codex' else 'E'
        fields = [(e, 'id', re.compile(prefix + r'\d{1,4}')) for e in result.get('evidence', [])]
    elif phase in ('debate', 'critique', 'rebuttal', 'round2'):
        fields = [(c, 'issue_id', ISSUE_ID) for c in result.get('critique_of_claude', [])]
    else:
        return []
    mapping = {}
    for entry, key, valid in fields:
        value = entry.get(key)
        if not isinstance(value, str) or valid.fullmatch(value):
            continue
        match = LOOSE_ID.fullmatch(value.strip())
        if not match:
            continue
        canonical = match.group(1).upper() + str(int(match.group(2)))
        if valid.fullmatch(canonical):
            mapping[value] = canonical
    if not mapping:
        return []
    existing = {entry.get(key) for entry, key, _ in fields}
    if any(new in existing for new in mapping.values()) or len(set(mapping.values())) != len(mapping):
        return []

    def replace(node):
        if isinstance(node, dict):
            return {k: replace(v) for k, v in node.items()}
        if isinstance(node, list):
            return [replace(v) for v in node]
        return mapping.get(node, node) if isinstance(node, str) else node

    result.update(replace(result))
    return [f'{old} -> {new}' for old, new in sorted(mapping.items())]

def proposal_semantics(result, side, session):
    problems = []
    if result['phase'] != 'proposal' or result['side'] != side:
        raise SchemaError('proposal phase/side mismatch')
    prefix = 'X' if side == 'codex' else 'E'
    ids = [e['id'] for e in result['evidence']]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(prefix + r'\d{1,4}', i) for i in ids):
        raise SchemaError(f'evidence ids must be unique {prefix}<n>')
    counts = {name: len(input_text(session['sid'], name).splitlines()) for name in INPUT_FILES}
    registered = {e['id'] for e in session['evidence']}
    for entry in result['evidence']:
        if entry['kind'] == 'brief':
            # 入力行参照を「;」「,」で複数並べた形（2026-10-16 Codex 実測: "brief:L16-L18; brief:L34-L35"）も
            # 要素ごとに検証する。1 つでも入力行として解決できなければ従来どおり拒否する
            parts = [p for p in re.split(r'\s*[;,]\s*', entry['ref'].strip()) if p]
            if not parts or any(link_ref(p, [], counts)[0] != 'input' for p in parts):
                # Claude 側は直して凍結し直せるので拒否する。Codex 側は根拠 1 件の参照不良で提案全体を失わないよう、
                # 該当根拠の不備として残し続行する（連結判定で unbacked になる）。
                if side == 'claude':
                    raise SchemaError('invalid input line reference: ' + entry['ref'][:60])
                problems.append(f'invalid input line reference in {entry["id"]}: ' + entry['ref'][:60])
        elif side == 'claude' and entry['kind'] == 'web' and entry['id'] not in registered:
            # Web 調査の根拠は登録手段がないので、URL 形式だけ検査して未登録のまま受け取る（brief/inference と同じく台帳では引用できない）。
            if not WEB_URL.fullmatch(entry['ref'].strip()):
                raise SchemaError('Claude web evidence ref must be a single http(s) URL: ' + entry['id'])
        elif side == 'claude' and entry['kind'] in ('file', 'command', 'mcp') and entry['id'] not in registered:
            raise SchemaError('Claude evidence must be registered with `evidence add`: ' + entry['id'])
    if not result['summary'].strip():
        raise SchemaError('empty summary')
    if result['status'] == 'INCOMPLETE' and not result['unverified']:
        problems.append('INCOMPLETE proposal without unverified items')
    problems += research_semantics(result['research'], side)
    return problems


def question_date(session):
    """質問時点（セッション開始日、ローカル日付）。Web 調査の基準日として両側へ渡す。"""
    return time.strftime('%Y-%m-%d', time.localtime(session['created_at']))


def research_urls(research):
    return [item['url'] for key in RESEARCH_LISTS for item in research[key]]


def research_semantics(research, side):
    """外部調査欄の形式検査。Claude 側は凍結前に直せない引き継ぎなので拒否、Codex 側は問題として残す。"""
    errors = []
    bad = [url for url in research_urls(research) if not WEB_URL.fullmatch(url.strip())]
    if bad:
        errors.append('research url must be a single http(s) URL: ' + bad[0][:80])
    listed = any(research[key] for key in RESEARCH_LISTS)
    if (not research['web_used'] or not listed) and not research['not_applicable_reason'].strip():
        errors.append('research without web findings needs not_applicable_reason')
    if listed and not research['web_used']:
        errors.append('research lists findings although web_used is false')
    if side == 'claude' and errors:
        raise SchemaError(errors[0])
    # 基準日の食い違いだけで提案全体を INCOMPLETE にしない（報告 11 節に両側の基準日をそのまま出す）。
    return errors


def required_responses(session, phase):
    """Codex が必ず応答すべき Claude 側 high/medium 争点 ID。"""
    sid = session['sid']
    if phase == 'debate':
        ledger = read_json_artifact(sid, 'ledger.pre-debate.json')
        return sorted(i['id'] for i in ledger['issues']
                      if i['origin'] in ('claude', 'reveal-diff') and i['importance'] in ('high', 'medium'))
    if phase == 'rebuttal':
        critique = read_json_artifact(sid, 'claude/critique.json')
        return sorted(i['id'] for i in critique['issues'] if i['importance'] in ('high', 'medium'))
    if phase in CONDITIONAL:
        return sorted(session.get('conditional', {}).get('issue_ids', []))
    return []


def debate_semantics(result, phase, session):
    problems = []
    critique_ids = [c['issue_id'] for c in result['critique_of_claude']]
    if any(not ISSUE_ID.fullmatch(i) for i in critique_ids) or len(critique_ids) != len(set(critique_ids)):
        raise SchemaError('critique issue ids must be unique ids such as C1')
    if phase == 'critique' and result['responses_to_claude_critique']:
        problems.append('responses_to_claude_critique must be empty in critique phase')
    if phase == 'rebuttal' and result['critique_of_claude']:
        problems.append('critique_of_claude must be empty in rebuttal phase')
    if not result['useful_from_other'] and not result['useful_none_reason'].strip():
        problems.append('useful_none_reason is required when useful_from_other is empty')
    answered = {r['issue_id'] for r in result['responses_to_claude_critique']}
    missing = [i for i in required_responses(session, phase) if i not in answered]
    if missing:
        problems.append('missing Codex responses to Claude high/medium issues: ' + ', '.join(missing))
    return problems


def fidelity_semantics(result, session):
    listed = set(session.get('conditional', {}).get('issue_ids', []))
    judged = {i['issue_id'] for i in result['issues']}
    missing, extra = sorted(listed - judged), sorted(judged - listed)
    problems = ['fidelity verdict missing for: ' + ', '.join(missing)] if missing else []
    if extra:
        # 依頼していない争点の判定は確認根拠にしない（checked_issues も対象集合で絞る）。
        problems.append('fidelity verdict on unlisted issues: ' + ', '.join(extra))
    return problems


def fidelity_coverage(session, ledger, synthesis, request=None):
    """fidelity 呼び出しが決定全文を渡して判定を求めた争点 ID（LISTED ISSUES ∩ 送った決定）。

    compact では関連集合（対象＋言及先）だけの決定を全文で送るので、その集合との共通部分にする。
    """
    listed = list((session.get('conditional') or {}).get('issue_ids', []))
    ids = {i['id'] for i in ledger.get('issues', [])}
    if request is not None:
        blocks = request_blocks(request)
        sent_ledger = {i['id'] for i in decode_json(blocks.get('LEDGER', '[]'))}
        # A retained decision must be complete, not merely mentioned in a filter manifest.
        modern = session.get('fidelity_format') == FIDELITY_FORMAT and not session.get('compact')
        split_lines = json_source_lines if modern else str.splitlines
        lines = split_lines(blocks.get('SYNTHESIS (cite as synthesis:L<n>)', ''))
        sent_lines = {int(m.group(1)): m.group(2) for line in lines
                      if (m := re.fullmatch(r'(\d+): (.*)', line))}
        text = read_artifact(session['sid'], 'synthesis.json').decode('utf-8')
        spans = _decision_spans(text)
        if spans is None:
            return []
        original = split_lines(text)
        serialized = compact_json_lines(text)
        complete = set()
        for ident, start, end in spans:
            first, last = text.count('\n', 0, start) + 1, text.count('\n', 0, end - 1) + 1
            if all(sent_lines.get(n) in (original[n - 1], serialized[n - 1]) for n in range(first, last + 1)):
                complete.add(ident)
        return sorted(set(listed) & sent_ledger & complete)
    sent = ids
    if session.get('compact') and listed:
        sent, _ = related_issues(ledger, synthesis, listed)
    return sorted(i for i in listed if i in sent)


# ---------------------------------------------------------------- リクエスト組み立て

def numbered(text):
    return '\n'.join(f'{i}: {line}' for i, line in enumerate(text.splitlines(), 1))


def block(title, body):
    return f'BEGIN {title}\n{body}\nEND {title}\n'


def ledger_excerpt(ledger, issue_ids=None):
    keep = ('id', 'origin', 'type', 'importance', 'title', 'requirement_ref', 'claude', 'codex', 'turns',
            'decision_method', 'verification', 'status')
    issues = [{k: i[k] for k in keep} for i in ledger['issues'] if issue_ids is None or i['id'] in issue_ids]
    return json.dumps(issues, ensure_ascii=False, indent=1)


def codex_result(session, phase):
    call = phase_call(session, phase)
    if not call:
        return None
    try:
        return read_json_artifact(session['sid'], f'codex/{phase}.json', MAX_RESULT)
    except (Refused, SchemaError):
        return None


def minify(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


COMPACT_NOTE = ('Request format: compact. JSON blocks are minified. A string "@same-as <location>" stands for text that is '
                'repeated verbatim at that location in this request. Conditional calls carry only the listed issues and '
                'the issues they mention (see EXCERPT FILTER); omitted issues are named there. Synthesis lines keep their '
                'original numbers.\n')


def related_issues(ledger, synthesis, listed):
    """対象争点と、その本文・決定が言及する争点（推移的）。言及の検出は既存 ledger ID の語単位一致。"""
    ids = {i['id'] for i in ledger['issues']}
    texts = {i['id']: json.dumps(i, ensure_ascii=False) for i in ledger['issues']}
    for group in ('decisions', 'unresolved', 'user_decisions'):
        for entry in (synthesis or {}).get(group, []):
            if entry.get('issue_id') in texts:
                texts[entry['issue_id']] += json.dumps(entry, ensure_ascii=False)
    related, pending = set(listed) & ids, sorted(set(listed) & ids)
    while pending:
        current = pending.pop()
        for match in re.finditer(r'(?<![A-Za-z0-9])([A-Z]{1,2}\d{1,4})(?![0-9])', texts.get(current, '')):
            if match.group(1) in ids and match.group(1) not in related:
                related.add(match.group(1))
                pending.append(match.group(1))
    unresolved = sorted({m for i in related for m in LEDGER_REF.findall(texts[i]) if m not in ids})
    return related, unresolved


def _decision_spans(text):
    """synthesis.json 原文中の decisions 各要素の (issue_id, 開始, 終了) 文字位置。読めなければ None。"""
    decoder, spans = json.JSONDecoder(), []

    def skip(i):
        while i < len(text) and text[i] in ' \t\r\n':
            i += 1
        return i
    try:
        i = skip(0)
        if text[i] != '{':
            return None
        i = skip(i + 1)
        while text[i] != '}':
            key, i = decoder.raw_decode(text, i)
            i = skip(i)
            if text[i] != ':':
                return None
            i = skip(i + 1)
            if key == 'decisions' and text[i] == '[':
                i = skip(i + 1)
                while text[i] != ']':
                    start = i
                    value, i = decoder.raw_decode(text, i)
                    spans.append((value.get('issue_id') if isinstance(value, dict) else None, start, i))
                    i = skip(i)
                    if text[i] == ',':
                        i = skip(i + 1)
                i += 1
            else:
                _, i = decoder.raw_decode(text, i)
            i = skip(i)
            if text[i] == ',':
                i = skip(i + 1)
    except (ValueError, IndexError):
        return None
    return spans


def synthesis_excerpt(text, related):
    """関連する決定だけを元の行番号のまま渡す（synthesis:L<n> 引用を保つ）。行で分けられない決定は残す。"""
    spans = _decision_spans(text)
    if spans is None:
        return numbered(text)
    starts = [0] + [m.end() for m in re.finditer('\n', text)]

    def line_of(offset):
        low, high = 0, len(starts) - 1
        while low < high:
            mid = (low + high + 1) // 2
            low, high = (mid, high) if starts[mid] <= offset else (low, mid - 1)
        return low + 1
    lines = text.splitlines()
    omit, keep = set(), set()
    for ident, start, end in spans:
        first, last = line_of(start), line_of(end - 1)
        whole = not text[starts[first - 1]:start].strip() and \
            text[end:(starts[last] - 1 if last < len(starts) else len(text))].strip() in ('', ',')
        (omit if ident not in related and whole else keep).update(range(first, last + 1))
    omit -= keep
    out, skipped = [], []
    for number, line in enumerate(lines, 1):
        if number in omit:
            skipped.append(number)
            continue
        if skipped:
            out.append(f'... (lines {skipped[0]}-{skipped[-1]} omitted: decisions on unrelated issues)')
            skipped = []
        out.append(f'{number}: {line}')
    if skipped:
        out.append(f'... (lines {skipped[0]}-{skipped[-1]} omitted: decisions on unrelated issues)')
    return '\n'.join(out)


def _same_as_index(earlier):
    """以前の Codex 出力の文字列 → 位置（重複送信を参照に置き換えるため）。"""
    index = {}
    for phase, result in earlier.items():
        for group in ('critique_of_claude', 'responses_to_claude_critique'):
            for item in (result or {}).get(group, []):
                for field in ('claim', 'alternative', 'argument', 'would_change_if'):
                    value = item.get(field)
                    if isinstance(value, str) and len(value) >= 40:
                        index.setdefault(value, f'YOUR EARLIER DEBATE OUTPUTS {phase}.{group}[{item["issue_id"]}].{field}')
    return index


def _same_as(value, index):
    """参照の方が短い（UTF-8 バイト）場合だけ @same-as に置き換え、そうでなければ原文を返す。"""
    if value not in index:
        return value
    reference = '@same-as ' + index[value]
    return reference if len(reference.encode('utf-8')) < len(value.encode('utf-8')) else value


def _dedupe_ledger(issues, index):
    for issue in issues:
        for side in ('codex',):
            issue[side] = dict(issue[side], position=_same_as(issue[side]['position'], index))
        issue['turns'] = [dict(t, text=_same_as(t['text'], index)) if t['speaker'] == 'codex' else t
                          for t in issue['turns']]
    return issues


def _build_full_request(session, phase, compact=None):
    sid = session['sid']
    explicit_format = compact is not None
    compact = bool(session.get('compact')) if compact is None else compact
    # 明示 compact=False は計測用の非圧縮基準。既存セッションは元の動作。
    lossless = session.get('request_format') == LOSSLESS_FORMAT
    packed = compact or (lossless and not explicit_format)
    parts = [(DEBATE / 'prompt_common.md').read_text(encoding='utf-8'),
             (DEBATE / f'prompt_{phase}.md').read_text(encoding='utf-8'),
             f'Session: {sid}\nPhase: {phase}\nWorking root: {PROJECT}\n'
             f'Readable session inputs directory: {art(sid) / "inputs"}\n' + ((COMPACT_NOTE if compact else 'Request format: lossless. JSON is minified; all issues are retained. @same-as references repeat exact text found in the same request.\n') if packed and phase != 'proposal' else '')
             + (f'Question date (research baseline, set research.question_date to this): {question_date(session)}\n' if phase == 'proposal' else ''),
             block('BRIEF (cite as brief:L<n>)', numbered(input_text(sid, 'brief'))),
             block('SCOPE (cite as scope:L<n>)', numbered(input_text(sid, 'scope'))),
             block('EVIDENCE INDEX (cite as index:L<n>)', numbered(input_text(sid, 'index')))]
    if session.get('data'):
        meta = session.get('data_meta', {})
        parts.append(block('ATTACHED DATA FILES (read-only copies; read narrow ranges with rg/awk/head, not whole files)', '\n'.join(
            f'{art(sid) / "inputs" / "data" / name}  bytes={meta.get(name, {}).get("bytes")} '
            f'lines={meta.get(name, {}).get("lines")} sha256={digest}' for name, digest in sorted(session['data'].items()))))
    if phase != 'proposal':
        claude_text = read_artifact(sid, 'claude/proposal.json').decode('utf-8')
        codex_text = read_artifact(sid, 'codex/proposal.json', MAX_RESULT).decode('utf-8')
        diff_text = read_artifact(sid, 'reveal/diff.json').decode('utf-8')
        registry = [{k: e[k] for k in ('id', 'path', 'lines', 'sha256')} for e in session['evidence']]
        if packed:
            codex_proposal = decode_json(codex_text)
            claude_text, codex_text = minify(decode_json(claude_text)), minify(codex_proposal)
            # 公開差分の未連結引用は ref が自分の提案と同じなら ID だけにする（同じ原文の重複送信を除く）。
            refs = {e['id']: e['ref'] for e in codex_proposal.get('evidence', [])}
            diff = decode_json(diff_text)
            diff['unbacked_codex'] = [entry['id'] if refs.get(entry.get('id')) == entry.get('ref') else entry
                                      for entry in diff.get('unbacked_codex', [])]
            diff_text = minify(diff)
            # 作業ルート配下の絶対パスは相対にする（Working root 行から復元できる）。
            registry = minify([dict(e, path=normalize_path(e['path'])) for e in registry])
        else:
            registry = json.dumps(registry, ensure_ascii=False)
        parts.append(block('CLAUDE PROPOSAL (frozen)', claude_text))
        parts.append(block('YOUR PROPOSAL (frozen)', codex_text))
        parts.append(block('CLAUDE EVIDENCE REGISTRY', registry))
        captures_at = len(parts)
        parts.append(block('REVEAL DIFF', diff_text))
    if phase == 'debate':
        ledger = read_json_artifact(sid, 'ledger.pre-debate.json')
        parts.append(block('LEDGER EXCERPT', minify(json.loads(ledger_excerpt(ledger))) if packed else ledger_excerpt(ledger)))
    if phase == 'rebuttal':
        critique = read_artifact(sid, 'claude/critique.json').decode('utf-8')
        parts.append(block('CLAUDE CRITIQUE (frozen)', minify(decode_json(critique)) if packed else critique))
        earlier_critique = codex_result(session, 'critique')
        parts.append(block('YOUR EARLIER CRITIQUE', minify(earlier_critique) if compact else
                           json.dumps(earlier_critique, ensure_ascii=False)))
    if phase in CONDITIONAL:
        conditional = session['conditional']
        parts.append(block('TRIGGER REASONS', json.dumps(conditional['reasons'], ensure_ascii=False)))
        parts.append(block('LISTED ISSUES', ', '.join(conditional['issue_ids']) or '(all high issues)'))
        ledger = read_json_artifact(sid, 'ledger.json')
        synthesis_text = read_artifact(sid, 'synthesis.json').decode('utf-8') if phase == 'fidelity' else None
        earlier = {p: codex_result(session, p) for p in ('debate', 'critique', 'rebuttal') if phase_call(session, p)}
        if compact and conditional['issue_ids']:
            synthesis_doc = decode_json(synthesis_text if synthesis_text is not None else read_artifact(sid, 'synthesis.json'))
            related, unresolved = related_issues(ledger, synthesis_doc, conditional['issue_ids'])
            all_ids = [i['id'] for i in ledger['issues']]
            parts.append(block('EXCERPT FILTER', minify({
                'listed': conditional['issue_ids'],
                'dependencies': sorted(related - set(conditional['issue_ids'])),
                'omitted': [i for i in all_ids if i not in related],
                'unresolved_references': unresolved})))
            earlier = {p: dict(r, **{g: [x for x in r[g] if x['issue_id'] in related]
                                     for g in ('critique_of_claude', 'responses_to_claude_critique')})
                       if r else r for p, r in earlier.items()}
            excerpt = _dedupe_ledger(json.loads(ledger_excerpt(ledger, related)), _same_as_index(earlier))
            parts.append(block('LEDGER', minify(excerpt)))
            parts.append(block('YOUR EARLIER DEBATE OUTPUTS', minify(earlier)))
            if phase == 'fidelity':
                parts.append(block('SYNTHESIS (cite as synthesis:L<n>)', synthesis_excerpt(synthesis_text, related)))
        else:
            excerpt = ledger_excerpt(ledger)
            if packed:
                excerpt = minify(_dedupe_ledger(json.loads(excerpt), _same_as_index(earlier)))
            parts.append(block('LEDGER', excerpt))
            parts.append(block('YOUR EARLIER DEBATE OUTPUTS', minify(earlier) if packed else json.dumps(earlier, ensure_ascii=False)))
            if phase == 'fidelity':
                rendered = numbered_json_source(synthesis_text) if session.get('fidelity_format') == FIDELITY_FORMAT and not compact else numbered(synthesis_text)
                parts.append(block('SYNTHESIS (cite as synthesis:L<n>)', rendered))
        if phase == 'round2':
            debate_end = debate_ended_at(session)
            fresh = [e for e in session['evidence'] if e['added_at'] > debate_end]
            excerpts = []
            for entry in fresh:
                text = claude_evidence_text(sid, entry['id'])
                text = '(capture unavailable)' if text is None else text
                excerpts.append(f'[{entry["id"]}] {entry["path"]}:L{entry["lines"][0]}-L{entry["lines"][1]}\n{text}')
            parts.append(block('NEW EVIDENCE SINCE DEBATE', '\n'.join(excerpts) or '(none)'))
    if phase != 'proposal':
        # 原文添付で要求上限を超えないよう、残り容量（余裕 4 KiB）の範囲で添付し、超える分は ID だけ示す。
        room = MAX_REQUEST - len('\n'.join(parts).encode('utf-8')) - 4096
        parts.insert(captures_at, block('CLAUDE EVIDENCE CAPTURES (frozen text of each E<n>)',
                                        evidence_captures(session, phase, max(0, min(MAX_CAPTURES, room)))))
        if lossless:
            reuse = reuse_evidence(session)
            available = MAX_REQUEST - len('\n'.join(parts).encode('utf-8')) - 4096
            encoded = minify(reuse)
            if len(encoded.encode('utf-8')) <= max(0, min(MAX_REUSE, available)):
                parts.append(block('VERIFIED REUSE EVIDENCE (after reveal; current hash checked)', encoded))
            elif reuse:
                notice = block('REUSE NOT ATTACHED', 'Optional reuse bundle does not fit; investigate from original evidence. No evidence is verified by this notice.')
                if len(notice.encode('utf-8')) < available:
                    parts.append(notice)
    return '\n'.join(parts)


def json_source_lines(text):
    """Only LF/CRLF delimit source lines; Unicode separators inside JSON strings are data."""
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return [line[:-1] if line.endswith('\r') else line for line in lines]


def numbered_json_source(text):
    return '\n'.join(f'{n}: {line}' for n, line in enumerate(json_source_lines(text), 1))


def compact_json_lines(text):
    """Remove JSON syntax whitespace only; keep strings and the original numbered line map."""
    quoted, escaped, lines = False, False, []
    for line in json_source_lines(text):
        out = []
        for char in line:
            if quoted:
                out.append(char)
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
                out.append(char)
            elif char not in ' \t\r':
                out.append(char)
        lines.append(''.join(out))
    return lines


def _serialize_fidelity(session, full):
    text = read_artifact(session['sid'], 'synthesis.json').decode('utf-8')
    lines = compact_json_lines(text)
    info = {'format': FIDELITY_FORMAT, 'applied': False, 'original_line_count': len(json_source_lines(text)),
            'citation_map': 'identity', 'fallback_reason': None}
    try:
        if decode_json('\n'.join(lines)) != decode_json(text):
            info['fallback_reason'] = 'numbered line reconstruction differs from original JSON'
            return full, info
    except SchemaError:
        info['fallback_reason'] = 'numbered lines cannot reconstruct original JSON'
        return full, info
    title = 'SYNTHESIS (cite as synthesis:L<n>)'
    original_block = block(title, request_blocks(full)[title])
    packed = '\n'.join(f'{n}: {line}' for n, line in enumerate(lines, 1))
    packet = full.replace(original_block, block(title, packed), 1)
    if len(packet.encode('utf-8')) >= len(full.encode('utf-8')):
        info['fallback_reason'] = 'full request is no larger'
        return full, info
    info['applied'] = True
    return packet, info


def build_request(session, phase, compact=None):
    if phase == 'fidelity' and compact is None and not session.get('compact') and \
            session.get('fidelity_format') == FIDELITY_FORMAT:
        return _serialize_fidelity(session, _build_full_request(session, phase))[0]
    return _build_full_request(session, phase, compact)


def fidelity_serialization_check(session, baseline, packet):
    """Separate lossless codec gate; never permits omitted issue records or decisions."""
    full = _build_full_request(session, 'fidelity')
    ordinary = lossless_check(session, 'fidelity', baseline, full, allow_selective=False)
    expected, info = _serialize_fidelity(session, full)
    blocks = request_blocks(packet)
    source = read_artifact(session['sid'], 'synthesis.json').decode('utf-8')
    numbered_lines = json_source_lines(blocks.get('SYNTHESIS (cite as synthesis:L<n>)', ''))
    parsed = [re.fullmatch(r'(\d+): (.*)', line) for line in numbered_lines]
    line_map = bool(parsed) and all(m and int(m[1]) == n for n, m in enumerate(parsed, 1)) \
        and len(parsed) == len(json_source_lines(source))
    json_equal = False
    if line_map:
        try:
            json_equal = decode_json('\n'.join(m[2] for m in parsed)) == decode_json(source)
        except SchemaError:
            pass
    # Exact generated packet equality also checks every unaffected block and every original line's tokens.
    exact = packet == expected
    return {**info, 'ok': ordinary['ok'] and exact and line_map and json_equal,
            'packet_matches_source': exact, 'line_map_preserved': line_map, 'synthesis_json_equal': json_equal,
            'full_integrity': ordinary,
            'coverage': fidelity_coverage(session, read_json_artifact(session['sid'], 'ledger.json'),
                                          read_json_artifact(session['sid'], 'synthesis.json'), request=packet)
                        if exact and line_map and json_equal else []}


MAX_REUSE_READERS = 16
MAX_REUSE_COMMAND = 8192
MAX_REUSE_SOURCE_BYTES = MAX_TOOL_OUTPUT


def _reuse_readers(text):
    """実行しない限定文法。単純な reader の改行/セミコロン列だけを受け付ける。"""
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_REUSE_COMMAND:
        return None
    try:
        outer = shlex.split(text)
        if outer and posixpath.basename(outer[0]) in SHELLS:
            shell = posixpath.basename(outer[0])
            if len(outer) != 3 or outer[1] not in ('-c', '-lc') or \
                    outer[0] not in (shell, '/bin/' + shell, '/usr/bin/' + shell):
                return None
            text = outer[2]
        if any(c in text for c in '$`\\\r#'):
            return None
        lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|()<>\n')
        lexer.whitespace, lexer.whitespace_split, lexer.commenters = ' \t', True, ''
        commands, words = [], []
        for token in lexer:
            if token and set(token) <= set(';\n'):
                if token.count(';') > 1 or (';' in token and not words):
                    return None
                if words:
                    commands.append(words)
                words = []
            elif token and set(token) <= set(';&|()<>\n'):
                return None
            else:
                words.append(token)
        if words:
            commands.append(words)
    except ValueError:
        return None
    if not commands or len(commands) > MAX_REUSE_READERS:
        return None
    readers = []
    for words in commands:
        name, args = posixpath.basename(words[0]), words[1:]
        if words[0] not in (name, '/bin/' + name, '/usr/bin/' + name):
            return None
        start, end = 1, None
        if name == 'nl' and len(args) == 2 and args[0] == '-ba':
            pass
        elif name == 'cat' and len(args) == 1:
            pass
        elif name == 'head' and len(args) == 3 and args[0] == '-n' and re.fullmatch(r'[1-9][0-9]{0,8}', args[1]):
            end = int(args[1])
        elif name == 'sed' and len(args) == 3 and args[0] == '-n':
            match = re.fullmatch(r'([1-9][0-9]{0,8})(?:,([1-9][0-9]{0,8}))?p', args[1])
            if not match:
                return None
            start, end = int(match.group(1)), int(match.group(2) or match.group(1))
            if end < start:
                return None
        else:
            return None
        named = args[-1]
        if named != named.strip() or named.startswith('-') or any(ord(c) < 32 or ord(c) == 127 for c in named) or \
                any(c in named for c in '\\;&|()<>{}*?[]~'):
            return None
        path = normalize_path(named)
        if not path or path.startswith('/') or '..' in path.split('/'):
            return None
        readers.append((name, path, start, end))
    return readers


def observed_excerpt(tool, path, first, last, cited_files=None):
    """全 reader の観測ハッシュと全出力（末尾 LF を含む）が一致したときだけ範囲を復元する。"""
    if tool.get('kind') != 'command_execution' or tool.get('status') != 'completed' or \
            tool.get('exit_code') not in (None, 0) or not 1 <= first <= last:
        return None
    readers = _reuse_readers(tool.get('text', ''))
    if readers is None:
        return None
    expected, found, consumed, sources = [], None, 0, {}
    try:
        for name, named, start, end in readers:
            if named not in sources:
                data, _, _ = read_source(PROJECT / named, min(MAX_SOURCE, MAX_REUSE_SOURCE_BYTES - consumed))
                consumed += len(data)
                if not (cited_files or {}).get(named) or sha256(data) != cited_files[named]:
                    return None
                text = data.decode('utf-8')
                # LF 以外の改行と nl の論理ページ制御は曖昧なので再調査へ戻す。
                if any(c in text for c in '\r\v\f\x00\x85\u2028\u2029'):
                    return None
                lines = text.splitlines(keepends=True)
                if name == 'nl' and any(line.rstrip('\n') in ('\\:', '\\:\\:', '\\:\\:\\:') for line in lines):
                    return None
                sources[named] = lines
            lines = sources[named]
            if name == 'nl' and any(line.rstrip('\n') in ('\\:', '\\:\\:', '\\:\\:\\:') for line in lines):
                return None
            stop = min(end, len(lines)) if end is not None else len(lines)
            rendered = lines[start - 1:stop]
            expected.append(''.join(f'{number:6}\t{line}' for number, line in enumerate(lines, 1))
                            if name == 'nl' else ''.join(rendered))
            if named == path and start <= first <= last <= stop:
                found = '\n'.join(line.removesuffix('\n') for line in lines[first - 1:last])
        if ''.join(expected) != tool.get('output', ''):
            return None
    except (Refused, OSError, UnicodeError, ValueError):
        return None
    return found


def reuse_evidence(session):
    """公開後のみ。出力原文と現在ファイルが一致した範囲だけを共有する（原子スナップショットではない）。"""
    sid = session['sid']
    if 'codex' not in session.get('frozen', {}):
        return []
    links = _artifact_or_none(sid, 'codex/proposal_links.json', MAX_DOC * 4) or {}
    tools = [t for t in load_tools(sid) if t.get('call') == (phase_call(session, 'proposal') or {}).get('index')]
    result = []
    for link in links.get('links', []):
        match = file_ref(link.get('ref', ''))
        if not match:
            continue
        path = normalize_path(match.group('path'))
        entry = {'id': link['id'], 'ref': link['ref'], 'reuse': False}
        try:
            if link.get('strength') != 'excerpt' or path.startswith('/') or '..' in path.split('/'):
                raise Refused('no unambiguous observed excerpt')
            data, _, _ = read_source(PROJECT / path, MAX_SOURCE)
            expected = links.get('cited_files', {}).get(path)
            if not expected or sha256(data) != expected:
                raise Refused('file changed or observation hash unavailable')
            first, last = int(match.group('a')), int(match.group('b') or match.group('a'))
            current = '\n'.join(data.decode('utf-8').splitlines()[first - 1:last])
            observed = next((text for t in tools if t.get('item_id') in link.get('tool_items', [])
                             and (text := observed_excerpt(t, path, first, last, links.get('cited_files', {}))) is not None
                             and text == current), None)
            if observed is None:
                raise Refused('observed output unavailable, ambiguous or differs from current source')
            entry.update(reuse=True, text=observed, file_sha256=expected)
        except (Refused, OSError, UnicodeError) as error:
            entry['reason'] = str(error)
        result.append(entry)
    for source in session.get('evidence', []):
        entry = {'id': source['id'], 'reuse': False}
        try:
            data, _, _ = read_source(source['path'], MAX_SOURCE)
            entry['reuse'] = bool(source.get('file_sha256') and sha256(data) == source['file_sha256'])
            if not entry['reuse']:
                entry['reason'] = 'Claude capture is historical; current file changed or hash unavailable'
        except (Refused, OSError):
            entry['reason'] = 'Claude capture cannot be checked against current source'
        result.append(entry)
    return result


def evidence_captures(session, phase, limit=MAX_CAPTURES):
    """Claude 根拠の凍結原文。経路だけでは Codex が読めない（作業領域外・更新後の差異）ため原文を渡す。
    round2 の新規根拠は NEW EVIDENCE 側で送る。上限を超えた分は登録情報だけにし、その旨を明記する。"""
    sid = session['sid']
    cutoff = debate_ended_at(session) if phase == 'round2' else float('inf')
    texts, used, omitted = [], 0, []
    # 省略 ID 行（ID 1 件 7 バイト以内、件数分）を先に見込み、区切りの改行も数える。
    budget = limit - 160 - 7 * len(session['evidence'])
    for entry in session['evidence']:
        if entry['added_at'] > cutoff:
            continue
        text = claude_evidence_text(sid, entry['id'])
        text = '(capture unavailable)' if text is None else text
        piece = f'[{entry["id"]}] {normalize_path(entry["path"])}:L{entry["lines"][0]}-L{entry["lines"][1]}\n{text}'
        size = len(piece.encode('utf-8')) + 1
        if used + size > budget:
            omitted.append(entry['id'])
            continue
        texts.append(piece)
        used += size
    if omitted:
        texts.append('(capture omitted for size; use the registry path only if it is inside the working root: '
                     + ', '.join(omitted) + ')')
    return '\n'.join(texts) or '(none)'


def debate_ended_at(session):
    call = phase_call(session, 'debate') or phase_call(session, 'rebuttal')
    return call['ended_at'] if call and call.get('ended_at') else float('inf')


# ---------------------------------------------------------------- Codex 呼び出し

def build_command(codex, schema_path, result_path, web=False):
    # 範囲明細が web を許すときだけ web_search を有効にする（--search は exec より前に置く。0.164 実測）。
    command = [str(codex)] + (['--search'] if web else []) + ['exec', '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral',
               '-C', str(PROJECT), '--json', '--output-schema', str(schema_path), '-o', str(result_path)]
    for key in DISABLE_CONFIG:
        command += ['-c', key]
    return command + ['-']


def run_call(session, phase, call_index, request, codex, scope, on_start=None):
    sid = session['sid']
    call_dir = ensure_dir(call_logs(sid, call_index, phase))
    keep_dir = ensure_dir(call_store(sid, call_index, phase))
    evidence_dir = ensure_dir(store(sid) / 'evidence')
    request_path, result_path = call_dir / 'request.txt', call_dir / 'result.json'
    event_path, error_path = call_dir / 'events.jsonl', call_dir / 'stderr.log'
    write_private(request_path, request.encode('utf-8'))
    monitor = Monitor(sid, scope, call_index, evidence_dir)
    command = build_command(codex, SCHEMAS / (PHASE_SCHEMA[phase] + '.schema.json'), result_path,
                            web='web' in scope.get('allowed_tools', []))
    reason, code, position, pending = None, None, 0, b''
    error_state = {'position': 0, 'pending': b''}

    def drain_stderr(to_eof=False):
        """stderr.log を逐次読み、hook 拒否の行を検出する（読み取り境界で分かれた行は連結して判定）。"""
        marker = HOOK_DENIAL_STDERR.encode('utf-8')
        while True:
            with error_path.open('rb') as errors_in:
                errors_in.seek(error_state['position'])
                chunk = errors_in.read(1024 * 1024)
            error_state['position'] += len(chunk)
            buffered = error_state['pending'] + chunk
            *lines, rest = buffered.split(b'\n')
            # 改行前でも文言がそろっていれば検出する。
            if not monitor.violations and any(marker in line for line in (*lines, rest)):
                monitor.violate('POLICY', 'hook denial reported by Codex (stderr)')
            # 未完の行は文言が分断されても照合できる長さだけ保持する。
            error_state['pending'] = rest[-(MAX_STDERR_PENDING + len(marker)):]
            if not to_eof or not chunk or monitor.violations:
                return

    def drain(to_eof=False):
        nonlocal position, pending
        while True:
            with event_path.open('rb') as events:
                events.seek(position)
                chunk = events.read(1024 * 1024)
            position += len(chunk)
            pending += chunk
            *lines, pending = pending.split(b'\n')
            for line in lines:
                if monitor.violations:
                    break
                monitor.feed(line.decode('utf-8', 'replace'))
            if not to_eof or not chunk or monitor.violations:
                return

    # Codex 累計予算の残りとフェーズ上限の小さい方。Claude の作業時間は差し引かない。
    timeout = max(0.1, min(phase_timeout(phase), codex_remaining(session)))
    check_signal()
    started = time.monotonic()
    with request_path.open('rb') as stdin, event_path.open('wb') as events, error_path.open('wb') as errors:
        process = subprocess.Popen(command, stdin=stdin, stdout=events, stderr=errors, start_new_session=True)
        try:
            # pid/pgid を最初に記録し、その後でシグナルを確認する。
            if on_start:
                on_start(process.pid)
            while True:
                check_signal()
                exited = process.poll() is not None
                drain(to_eof=exited)
                if not monitor.violations:
                    drain_stderr(to_eof=exited)
                if monitor.violations:
                    reason = monitor.violations[0]
                    break
                if exited:
                    break
                if time.monotonic() - started >= timeout:
                    reason = {'category': 'BUDGET', 'detail': 'TIMEOUT: process group terminated; no automatic retry'}
                    break
                if event_path.stat().st_size + error_path.stat().st_size > MAX_LOGS:
                    reason = {'category': 'BUDGET', 'detail': 'execution logs exceeded 8 MiB'}
                    break
                if result_path.exists() and result_path.stat().st_size > MAX_RESULT:
                    reason = {'category': 'BUDGET', 'detail': 'result exceeded 128 KiB'}
                    break
                time.sleep(0.1)
            # 違反時も正常終了時も残った子孫プロセスをグループごと終了する。
            CC.stop_group(process)
            code = process.returncode
        except BaseException:
            CC.stop_group(process)
            raise
    if reason is None and not monitor.violations:
        drain(to_eof=True)
    if reason is None and not monitor.violations:
        drain_stderr(to_eof=True)
    if pending.strip() and not monitor.violations:
        monitor.feed(pending.decode('utf-8', 'replace'))
    if reason is None and monitor.violations:
        reason = monitor.violations[0]
    if reason and reason not in monitor.violations:
        monitor.violations.append(reason)
    elapsed = round(time.monotonic() - started, 2)
    problems = []
    if reason is None:
        if code != 0:
            reason = {'category': 'EXEC', 'detail': f'Codex exit code {code}'}
        elif not monitor.completed:
            reason = {'category': 'EXEC', 'detail': 'missing turn.completed event'}
        if reason:
            monitor.violations.append(reason)
    changed = verify_frozen(session)
    if changed and reason is None:
        reason = {'category': 'INPUT_CHANGED', 'detail': changed}
        monitor.violations.append(reason)
    result = None
    digests = {}
    normalized = []
    if reason is None:
        try:
            data, _, _ = read_source(result_path, MAX_RESULT, allow_session_files=True)
            digests['result_sha256'] = sha256(data)
            # 検証の成否にかかわらず読めた結果は恒久領域に残す（独立性違反の破棄は別経路）。
            write_private(keep_dir / 'result.json', data)
            result = decode_json(data)
            validate_doc(result, PHASE_SCHEMA[phase])
            normalized = normalize_ids(result, phase)
            if normalized:
                # reveal はここで記録したハッシュの result.json を公開するので、正規化版を結果とし原文は別に残す。
                write_private(keep_dir / 'result.raw.json', data)
                data = dump(result)
                write_private(keep_dir / 'result.json', data)
                digests['result_sha256'] = sha256(data)
            if phase == 'proposal':
                problems += proposal_semantics(result, 'codex', session)
            elif phase == 'fidelity':
                problems += fidelity_semantics(result, session)
            else:
                problems += debate_semantics(result, phase, session)
        except (Refused, SchemaError, OSError, UnicodeError) as error:
            reason = {'category': 'SCHEMA', 'detail': 'invalid/missing result: ' + str(error)[:160]}
            monitor.violations.append(reason)
            result = None
    discarded = False
    if reason and reason['category'] == 'INDEPENDENCE':
        # 独立性マーカー検出時は結果を読まずに破棄する（作業用一時ファイルのみ）。
        if result_path.exists() and not result_path.is_symlink():
            result_path.unlink()
        discarded = True
    partial = None
    if reason and reason['category'] in ('POLICY', 'BUDGET', 'EXEC') and monitor.messages:
        # 時間・道具上限などで止まっても途中の所見を参照できるよう残す（有効な結果としては扱わない）。
        partial = keep_dir / 'partial_messages.md'
        write_private(partial, ('# 정지 전 Codex 중간 메시지 (검증되지 않은 부분 결과)\n\n'
                                + '\n\n---\n\n'.join(monitor.messages)).encode('utf-8'))
    execution = 'ok' if reason is None else 'stopped' if reason['category'] in (
        'POLICY', 'INDEPENDENCE', 'SECRET', 'BUDGET') else 'failed'
    links = None
    if result is not None:
        links = link_result(session, phase, result, load_tools(sid))
        write_private(keep_dir / 'links.json', dump(links))
        digests['links_sha256'] = sha256(dump(links))
    model_status = result['status'] if result else None
    summary = {
        'codex_calls': 1, 'mode': 'debate-' + phase,
        'status': 'COMPLETE' if execution == 'ok' and model_status == 'COMPLETE' and not problems else 'INCOMPLETE',
        'execution': execution, 'model_status': model_status,
        'evidence_sufficiency': result.get('evidence_sufficiency') if result else None,
        'tool_counts': monitor.tool_counts, 'violations': monitor.violations, 'usage': monitor.usage,
        'elapsed_seconds': elapsed, 'exit_code': code, 'timeout_seconds': round(timeout, 2),
        'problems': problems, 'links': links['counts'] if links else None, 'discarded': discarded,
        'normalized_ids': normalized, 'partial_messages': str(partial) if partial else None,
        'session': sid, 'call_index': call_index,
    }
    write_private(keep_dir / 'summary.json', dump(summary))
    return summary, result, links, keep_dir, digests


def cmd_call(args):
    sid, phase = sid_check(args.session), args.phase
    with locked_session(sid) as session:
        plan = PLANS[session['plan']]
        if phase not in plan['required'] and phase not in CONDITIONAL:
            raise Refused(f'phase {phase} is not part of plan {session["plan"]}')
        require_state(session, PHASES[phase][0])
        if phase == 'debate' and not (art(sid) / 'ledger.pre-debate.json').is_file():
            raise Refused('run `ledger validate --stage pre-debate` before the debate call')
        conditional = phase in CONDITIONAL
        basis = None
        if conditional:
            info = session.get('conditional') or {}
            if session.get('conditional_used'):
                raise Refused('conditional call budget already used')
            if info.get('next') != phase:
                raise Refused(f'no computed reason for {phase}; run `synthesis validate` (next={info.get("next")})')
            synthesis_data, ledger_data = read_artifact(sid, 'synthesis.json'), read_artifact(sid, 'ledger.json')
            if info.get('synthesis_sha256') != sha256(synthesis_data) or info.get('ledger_sha256') != sha256(ledger_data):
                raise Refused('synthesis/ledger changed since `synthesis validate`; validate again')
            # D1: 呼び出しに使った ledger・synthesis の内容ハッシュと判定対象の争点を残し、確認表示を内容に結び付ける。
            ledger_doc, synthesis_doc = decode_json(ledger_data), decode_json(synthesis_data)
            basis = dict(check_basis(ledger_doc, synthesis_doc),
                         issue_ids=fidelity_coverage(session, ledger_doc, synthesis_doc))
        remaining = codex_remaining(session)
        over_calls = len(session['calls']) >= plan['max_calls']
        if over_calls or remaining < MIN_REMAINING:
            detail = 'call limit reached' if over_calls else 'less than 60 s of Codex time budget left'
            if conditional:
                session['conditional_skipped'] = 'BUDGET_EXHAUSTED: ' + detail
                save_session(session)
                raise Refused('BUDGET_EXHAUSTED: ' + detail + '; mark codex_checked false')
            stop(session, 'BUDGET', 'BUDGET_EXHAUSTED: ' + detail)
            save_session(session)
            return {'status': 'STOPPED', 'stop': session['stop']}, 2
        codex = find_codex()
        version = codex_version(codex)
        if version != session['codex_version']:
            stop(session, 'VERSION', 'codex version changed during the session')
            save_session(session)
            return {'status': 'STOPPED', 'stop': session['stop']}, 2
        changed = verify_frozen(session)
        if changed:
            stop(session, 'INPUT_CHANGED', changed)
            save_session(session)
            return {'status': 'STOPPED', 'stop': session['stop']}, 2
        request = build_request(session, phase)
        if (session.get('request_format') == LOSSLESS_FORMAT or
                (phase == 'fidelity' and session.get('fidelity_format') == FIDELITY_FORMAT)) and not session.get('compact'):
            baseline = build_request(session, phase, compact=False)
            integrity = (fidelity_serialization_check(session, baseline, request)
                         if phase == 'fidelity' and session.get('fidelity_format') == FIDELITY_FORMAT
                         else lossless_check(session, phase, baseline, request, allow_selective=False))
            if not integrity['ok']:
                return {'status': 'INCOMPLETE', 'problems': ['lossless request integrity check failed'], 'integrity': integrity}, 2
        if phase == 'fidelity' and basis is not None:
            basis['issue_ids'] = fidelity_coverage(session, ledger_doc, synthesis_doc, request=request)
        auto_compact = False
        if len(request.encode('utf-8')) > MAX_REQUEST and not session.get('compact') and phase in ('debate', 'critique', 'rebuttal'):
            # 上限超過で止まる場合に限り、minify・重複 ID 化だけの compact で収まるならその呼び出しだけ compact で送る。
            # 争点を抜粋する条件付き段階（round2・fidelity）には適用しない。
            compacted = build_request(session, phase, compact=True)
            if len(compacted.encode('utf-8')) <= MAX_REQUEST:
                request, auto_compact = compacted, True
        if len(request.encode('utf-8')) > MAX_REQUEST:
            # 切り詰めずに INCOMPLETE とし、呼び出し回数にも数えない。
            return {'status': 'INCOMPLETE', 'problems': ['request exceeds 192 KiB; narrow the scope, do not truncate']}, 2
        scope = decode_json(read_artifact(sid, 'inputs/scope.json', MAX_INPUT))
        index = len(session['calls']) + 1
        # 失敗した呼び出しも数えるため、実行前に記録する。
        record = {'index': index, 'phase': phase, 'started_at': now(), 'ended_at': None, 'execution': 'running',
                  'status': None, 'timeout': round(min(phase_timeout(phase), remaining), 2), 'codex': str(codex)}
        if basis:
            record['checked_basis'] = basis
        if phase == 'fidelity' and session.get('fidelity_format') == FIDELITY_FORMAT and not session.get('compact'):
            record['fidelity_serialization'] = integrity
        if auto_compact:
            record['auto_compact'] = True
        session['calls'].append(record)
        if conditional:
            session['conditional_used'] = phase

        def on_start(pid):
            # ランナーが強制終了されても後続コマンドがグループを止められるよう pid/pgid を残す。
            record.update(pid=pid, pgid=pid, spawned_at=now())
            save_session(session)

        def kill_recorded():
            # 後始末に失敗していても記録した Codex グループだけは最善努力で止める。
            if type(record.get('pgid')) is int and record['pgid'] > 1:
                try:
                    os.killpg(record['pgid'], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        with signal_guard():
            save_session(session)
            try:
                summary, result, links, call_dir, digests = run_call(session, phase, index, request, codex, scope, on_start)
                return finish_call(session, phase, record, summary, result, links, call_dir, digests)
            except RunnerSignal as received:
                kill_recorded()
                record.update(ended_at=now(), execution='failed', status='INCOMPLETE')
                stop(session, 'EXEC', f'runner received signal {int(received.args[0])}; Codex process group terminated')
                save_session(session)
                return {'status': 'STOPPED', 'state': 'STOPPED', 'stop': session['stop']}, 2
            except BaseException:
                kill_recorded()
                record.update(ended_at=now(), execution='failed', status='INCOMPLETE')
                stop(session, 'EXEC', 'runner error during the Codex call')
                save_session(session)
                raise


def finish_call(session, phase, call, summary, result, links, call_dir, digests):
    sid = session['sid']
    call.update(ended_at=now(), execution=summary['execution'], status=summary['status'],
                model_status=summary['model_status'], summary=str(call_dir / 'summary.json'),
                timeout=summary['timeout_seconds'], codex_seconds=summary['elapsed_seconds'], **digests)
    output = {k: summary[k] for k in ('status', 'mode', 'execution', 'model_status', 'tool_counts',
                                      'violations', 'problems', 'links', 'elapsed_seconds', 'usage')}
    if summary['execution'] != 'ok':
        first = summary['violations'][0]
        stop(session, first['category'], first['detail'])
        save_session(session)
        output.update(state='STOPPED', stop=session['stop'])
        return output, 2
    if phase != 'proposal':
        write_frozen(art(sid) / 'codex' / f'{phase}.json', dump(result))
        write_frozen(art(sid) / 'codex' / f'{phase}_links.json', dump(links))
        output['result_path'] = str(art(sid) / 'codex' / f'{phase}.json')
        output['summary_path'] = call['summary']
    session['state'] = PHASES[phase][1]
    save_session(session)
    output['state'] = session['state']
    output['calls_used'] = len(session['calls'])
    # proposal の結果パスは reveal まで出力しない（Claude 側の独立性のため）。
    return output, 0 if summary['status'] == 'COMPLETE' else 2


# ---------------------------------------------------------------- サブコマンド

def prune_targets(apply=False):
    """一時領域だけを対象にする（logs は 7 日、旧セッションの一時 evidence は 14 日）。.local の恒久領域は扱わない。"""
    targets = []
    if not USER_TMP.is_dir():
        return targets
    for entry in sorted(USER_TMP.iterdir()):
        if not entry.name.startswith('co-design-') or entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name.startswith('co-design-verify-'):
            # 版ゲート自動点検のログ（作成時刻は名前にないので更新時刻で logs と同じ 7 日）。
            age_days = (now() - entry.stat().st_mtime) / 86400
            if age_days > LOG_DAYS:
                targets.append({'path': str(entry), 'age_days': round(age_days, 1), 'limit_days': LOG_DAYS})
                if apply:
                    shutil.rmtree(entry)
            continue
        sid = entry.name[len('co-design-'):]
        if not SID_RE.fullmatch(sid):
            continue
        created = datetime.strptime(sid[:16], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp()
        age_days = (now() - created) / 86400
        for name, days in (('evidence', EVIDENCE_DAYS), ('logs', LOG_DAYS)):
            target = entry / name
            if age_days > days and target.is_dir() and not target.is_symlink():
                targets.append({'path': str(target), 'age_days': round(age_days, 1), 'limit_days': days})
                if apply:
                    shutil.rmtree(target)
    return targets


def cmd_init(args):
    if not QID_RE.fullmatch(args.question_id):
        raise Refused('question id must be 1-64 ASCII letters, digits, dot, underscore or hyphen')
    codex = find_codex()
    version = codex_version(codex)
    verified = verified_version()
    verification = None
    if version != verified:
        if args.no_auto_verify:
            if verified is None:
                raise Refused('verified_codex_version is missing; run E2E-1 and record it before using debate')
            raise Refused(f'Codex version gate failed: current {version!r} != verified {verified!r}')
        # 版が変わったら自動点検（Codex 1 回）を通ったときだけ記録を更新して続ける。
        verification = verify_codex(codex, version)
        if verification['status'] != 'PASS':
            # 再試行した場合は全試行のログ経路を示す（1 回目のログを探し回らせない）。
            logs = ', '.join(a['log_dir'] for a in verification.get('attempts', [])) or verification['log_dir']
            raise Refused(f'Codex version gate failed: current {version!r} != verified {verified!r}; automatic check failed: '
                          + '; '.join(verification['problems'])[:500] + f' (logs: {logs})')
    ensure_dir(ARTIFACT_ROOT)
    started = now()
    sid = datetime.fromtimestamp(started, timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)
    plan = 'B' if args.frozen_critique else 'A'
    ensure_dir(art(sid))
    ensure_dir(store(sid) / 'evidence')
    ensure_dir(store(sid) / 'calls')
    ensure_dir(work(sid) / 'logs')
    session = {'version': 1, 'sid': sid, 'question_id': args.question_id, 'plan': plan, 'state': 'INIT',
               'created_at': started, 'codex_budget_seconds': PLANS[plan]['codex_budget'],
               'max_calls': PLANS[plan]['max_calls'],
               'codex_version': version, 'calls': [], 'inputs': {}, 'frozen': {}, 'evidence': [],
               'conditional': None, 'conditional_used': None, 'stop': None, 'compact': bool(args.compact),
               'request_format': LOSSLESS_FORMAT, 'fidelity_format': FIDELITY_FORMAT}
    save_session(session)
    return {'status': 'ok', 'session': sid, 'plan': plan, 'state': 'INIT', 'project_root': str(PROJECT.resolve()),
            'compact': session['compact'], 'request_format': session['request_format'],
            'codex_budget_seconds': session['codex_budget_seconds'],
            'phase_timeouts': {'proposal': PROPOSAL_TIMEOUT, 'other': CALL_TIMEOUT},
            'max_calls': session['max_calls'], 'codex_version': version, 'codex_verification': verification,
            'artifacts': str(art(sid)), 'expired_for_prune': prune_targets()}, 0


def validate_scope(scope):
    validate_doc(scope, 'scope')
    for path in scope['allowed_paths'] + scope['excluded_paths']:
        if not path.strip() or path.startswith('/') or '..' in path.split('/'):
            raise SchemaError('scope paths must be relative to the project root without ..')
    # allowed_paths は助言（ランナーは強制しない）。空配列は「リポジトリのコードを調べない」を表す。
    if not scope['question'].strip():
        raise SchemaError('scope needs a question')
    if scope['external_transfer'] == 'none' and set(scope['allowed_tools']) & {'web', 'context7'}:
        raise SchemaError('external_transfer none conflicts with web/context7 tools')


def cmd_freeze_inputs(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        require_state(session, 'INIT')
        texts = {}
        for name in INPUT_FILES:
            try:
                texts[name] = CC.read_packet(getattr(args, name))
            except (OSError, ValueError) as error:
                raise Refused(f'{name}: {error}') from None
        validate_scope(decode_json(texts['scope']))
        # リポジトリ外の参照データ（ログ集計・CSV 等）は保護検査を通してセッション入力 data/ に凍結コピーする。
        attachments, total = {}, 0
        for value in getattr(args, 'data', None) or []:
            data, _, resolved = read_source(value, MAX_DATA_FILE)
            name = resolved.name
            if name in attachments or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', name):
                raise Refused('data files need unique ASCII names (letters, digits, . _ -): ' + name[:60])
            total += len(data)
            if total > MAX_DATA_TOTAL:
                raise Refused('attached data exceeds 4 MiB in total; narrow it, do not truncate')
            attachments[name] = data
        for name, text in texts.items():
            data = text.encode('utf-8')
            write_frozen(art(sid) / 'inputs' / INPUT_FILES[name], data)
            session['inputs'][name] = sha256(data)
        if attachments:
            session['data'] = {}
            for name, data in attachments.items():
                write_frozen(art(sid) / 'inputs' / 'data' / name, data)
                session['data'][name] = sha256(data)
                session.setdefault('data_meta', {})[name] = {'bytes': len(data), 'lines': data.count(b'\n')}
            (art(sid) / 'inputs' / 'data').chmod(0o500)
        (art(sid) / 'inputs').chmod(0o500)
        session['state'] = 'INPUTS_FROZEN'
        save_session(session)
        return {'status': 'ok', 'state': session['state'], 'inputs': session['inputs']}, 0


def capture_evidence(value, first, last):
    """根拠の読み取りと抜粋だけを行う。登録・ID 割当・保存はしない。"""
    data, _, resolved = read_source(value, MAX_SOURCE)
    return capture_snapshot(data, resolved, first, last)


def capture_snapshot(data, resolved, first, last):
    """同じ bytes から範囲・抜粋・全体 hash を求める。範囲は補正しない。"""
    lines = data.splitlines(keepends=True)
    if not 1 <= first <= last <= len(lines):
        raise Refused('line range outside the file')
    captured = b''.join(lines[first - 1:last])
    if len(captured) > MAX_DOC:
        raise Refused('captured range exceeds 256 KiB; narrow it')
    return captured, {'path': str(resolved), 'lines': [first, last], 'sha256': sha256(captured),
                      'file_sha256': sha256(data), 'bytes': len(captured)}


def cmd_evidence_inspect(args):
    """独立調査用（Codex 初回提案と並行可、ロックなし）。登録・保存・ID 割当は一切しない。"""
    sid = sid_check(args.session)
    with inspection_session(sid):
        path = Path(args.file)
        if not path.is_absolute() or not _within(path.resolve(strict=True), PROJECT):
            raise Refused('inspection requires an absolute source path inside the current project')
        data, _, resolved = read_source(path, MAX_SOURCE)
        lines = data.splitlines(keepends=True)
        if args.whole_file:
            first, last = 1, len(lines)
        else:
            match = re.fullmatch(r'(\d+)-(\d+)', args.lines)
            if not match:
                raise Refused('--lines must be a-b')
            first, last = map(int, match.groups())
        if args.whole_file and not lines:
            # 空ファイルに有効な行範囲を捏造しない。file evidence としては登録できない。
            return {'status': 'ok', 'path': str(resolved), 'line_count': 0, 'lines': None, 'ref': None,
                    'file_sha256': sha256(data), 'sha256': sha256(b''), 'bytes': 0, 'numbered_source': ''}, 0
        _, entry = capture_snapshot(data, resolved, first, last)
        try:
            numbered = ''.join(str(number) + '\t' + line.decode('utf-8')
                               for number, line in enumerate(lines[first - 1:last], first))
        except UnicodeError:
            raise Refused('source excerpt must be UTF-8; do not replace invalid bytes') from None
        suffix = ':L' + str(first) + ('-L' + str(last) if last != first else '')
        return {'status': 'ok', **entry, 'line_count': len(lines),
                'ref': str(resolved.relative_to(PROJECT.resolve())) + suffix, 'numbered_source': numbered}, 0


def evidence_ids_reserved(session, extra=()):
    reserved = {item['id'] for item in session['evidence']} | set(session.get('reserved_evidence_ids', [])) | set(extra)
    proposal = art(session['sid']) / 'claude/proposal.json'
    if proposal.exists() or proposal.is_symlink():
        reserved.update(item['id'] for item in read_json_artifact(session['sid'], 'claude/proposal.json')['evidence'])
    return reserved


def next_evidence_id(reserved):
    for number in range(1, 10000):
        ident = 'E' + str(number)
        if ident not in reserved:
            return ident
    raise Refused('evidence id space exhausted')


def cmd_evidence_add(args):
    sid = sid_check(args.session)
    match = re.fullmatch(r'(\d+)-(\d+)', args.lines)
    if not match:
        raise Refused('--lines must be a-b')
    first, last = int(match.group(1)), int(match.group(2))
    with locked_session(sid) as session:
        # Q3: Codex の提案が終わるまで Claude 側の調査根拠は登録しない。
        require_state(session, *AFTER_CODEX_PROPOSAL)
        captured, entry = capture_evidence(args.file, first, last)
        ident = next_evidence_id(evidence_ids_reserved(session))
        write_frozen(store(sid) / 'evidence' / 'claude' / (ident + '.txt'), captured)
        entry.update(id=ident, added_at=now())
        session['evidence'].append(entry)
        save_session(session)
        return {'status': 'ok', 'id': ident, 'sha256': entry['sha256']}, 0


def cmd_proposal(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        if args.action == 'capture':
            return HANDOFF.capture(SimpleNamespace(**globals()), session, args.agent_output, args.out,
                                   agent_events=args.agent_events, agent_id=args.agent_id,
                                   caller_session_id=args.caller_session_id), 0
        return HANDOFF.prepare(SimpleNamespace(**globals()), session, args.file, args.out), 0


def cmd_freeze(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        if args.side == 'claude':
            require_state(session, 'CODEX_PROPOSED')
            call, target, nxt = phase_call(session, 'proposal'), 'claude/proposal.json', 'CLAUDE_FROZEN'
        else:
            if session['plan'] != 'B':
                raise Refused('claude-critique belongs to --frozen-critique (plan B)')
            require_state(session, 'CODEX_CRITIQUED')
            call, target, nxt = phase_call(session, 'critique'), 'claude/critique.json', 'CLAUDE_CRITIQUE_FROZEN'
        if not call or not call.get('ended_at') or call['execution'] != 'ok':
            raise Refused('the preceding Codex call has not finished successfully')
        data, info, _ = read_source(args.file, MAX_DOC)
        birth = getattr(info, 'st_birthtime', None)
        if birth is None:
            raise Refused('file birthtime is unavailable on this platform')
        if birth <= call['ended_at']:
            raise Refused('file was created before the Codex call ended; write a new file after the call')
        document = decode_json(data)
        if args.side == 'claude':
            HANDOFF.verify_prepared(SimpleNamespace(**globals()), session, document)
        changes = []
        original_data = data
        try:
            if args.side == 'claude':
                validate_doc(document, 'proposal')
                if getattr(args, 'normalize_refs', False) and session.get('proposal_handoff') is None:
                    changes = normalize_proposal_refs(session, document)
                problems = proposal_semantics(document, 'claude', session)
            else:
                validate_doc(document, 'ledger')
                if getattr(args, 'normalize_refs', False):
                    changes = normalize_ledger_refs(session, document)
                problems = validate_ledger(session, document, 'pre-debate', critique=True)
        except SchemaError as error:
            raise Refused('schema: ' + str(error)) from None
        frozen_target = art(sid) / target
        if frozen_target.exists() or frozen_target.is_symlink():
            raise Refused('freeze target already exists')
        revision = None
        if changes:
            revision = str(write_format_audit(sid, target, original_data, changes))
            data = dump(document)
        write_frozen(art(sid) / target, data)
        session['frozen'][args.side] = {'path': target, 'sha256': sha256(data), 'frozen_at': now(), 'birthtime': birth}
        session['state'] = nxt
        save_session(session)
        return {'status': 'ok', 'state': nxt, 'sha256': sha256(data), 'warnings': problems,
                'normalized_refs': changes, 'format_revision': revision}, 0


def build_diff(session, codex_links, version_now):
    sid = session['sid']
    claude = read_json_artifact(sid, 'claude/proposal.json')
    codex = read_json_artifact(sid, 'codex/proposal.json', MAX_RESULT)
    registry = {e['id']: e for e in session['evidence']}
    project = str(PROJECT).rstrip('/') + '/'
    claude_paths = set()
    for entry in claude['evidence']:
        if entry['id'] in registry and registry[entry['id']]['path'].startswith(project):
            claude_paths.add(normalize_path(registry[entry['id']]['path']))
        match = file_ref(entry['ref'])
        if match:
            claude_paths.add(normalize_path(match.group('path')))
    codex_paths = set()
    for entry in codex['evidence']:
        match = file_ref(entry['ref'])
        if match:
            codex_paths.add(normalize_path(match.group('path')))
    mismatch = []
    for entry in session['evidence']:
        try:
            data, _, _ = read_source(entry['path'], MAX_SOURCE)
            lines = data.splitlines(keepends=True)
            current = sha256(b''.join(lines[entry['lines'][0] - 1:entry['lines'][1]]))
        except (Refused, OSError):
            current = None
        if current != entry['sha256']:
            mismatch.append({'side': 'claude', 'evidence_id': entry['id'], 'path': normalize_path(entry['path']),
                             'captured': entry['sha256'], 'current': current})
    claude_files = {normalize_path(e['path']): e['file_sha256'] for e in session['evidence']}
    for path, captured in codex_links['cited_files'].items():
        current = cited_file_hashes([path + ':L1']).get(path)
        if current != captured:
            mismatch.append({'side': 'codex', 'path': path, 'captured': captured, 'current': current})
        if path in claude_files and claude_files[path] != captured:
            mismatch.append({'side': 'both', 'path': path, 'captured_claude': claude_files[path], 'captured_codex': captured})
    # 作業ルート外の絶対パス（推論根拠が参照した標準ライブラリなど）は照合対象にならないので片側一覧に入れない。
    claude_paths = {p for p in claude_paths if not p.startswith('/')}
    codex_paths = {p for p in codex_paths if not p.startswith('/')}
    return {'one_sided': {'claude_only': sorted(claude_paths - codex_paths), 'codex_only': sorted(codex_paths - claude_paths)},
            'hash_mismatch': mismatch,
            'unbacked_codex': [{'id': l['id'], 'ref': l['ref']} for l in codex_links['links'] if l['link'] == 'unbacked'],
            'codex_version': {'session': session['codex_version'], 'now': version_now}}


def cmd_reveal(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        # 両側が固定済みのときだけ公開する。
        require_state(session, 'CLAUDE_FROZEN')
        if 'claude' not in session['frozen']:
            raise Refused('Claude proposal is not frozen')
        call = phase_call(session, 'proposal')
        result_file, links_file = call_file(sid, call, 'result.json'), call_file(sid, call, 'links.json')
        if result_file is None or links_file is None:
            raise Refused('Codex proposal result/links are missing from the permanent store and the temporary logs')
        result, _, _ = read_source(result_file, MAX_RESULT, allow_session_files=True)
        links_data, _, _ = read_source(links_file, MAX_DOC * 4, allow_session_files=True)
        # 呼び出し時に記録したハッシュと照合し、公開前の差し替えを検出する。
        if sha256(result) != call.get('result_sha256') or sha256(links_data) != call.get('links_sha256'):
            stop(session, 'INPUT_CHANGED', 'Codex proposal log changed before reveal')
            save_session(session)
            return {'status': 'STOPPED', 'state': 'STOPPED', 'stop': session['stop']}, 2
        write_frozen(art(sid) / 'codex' / 'proposal.json', result)
        write_frozen(art(sid) / 'codex' / 'proposal_links.json', links_data)
        session['frozen']['codex'] = {'path': 'codex/proposal.json', 'sha256': sha256(result), 'frozen_at': now()}
        try:
            version_now = codex_version(find_codex())
        except Refused:
            version_now = None
        diff = build_diff(session, decode_json(links_data), version_now)
        write_frozen(art(sid) / 'reveal' / 'diff.json', dump(diff))
        session['state'] = 'REVEALED'
        if session.get('request_format') == LOSSLESS_FORMAT:
            bundle = reuse_evidence(session)
            if len(dump(bundle)) <= MAX_DOC:
                write_frozen(art(sid) / 'reveal' / 'reuse_evidence.json', dump(bundle))
        save_session(session)
        return {'status': 'ok', 'state': 'REVEALED', 'codex_proposal': str(art(sid) / 'codex/proposal.json'),
                'diff': str(art(sid) / 'reveal/diff.json'),
                'reusable_evidence': str(art(sid) / 'reveal/reuse_evidence.json') if (art(sid) / 'reveal/reuse_evidence.json').is_file() else None,
                'diff_counts': {'claude_only': len(diff['one_sided']['claude_only']),
                                'codex_only': len(diff['one_sided']['codex_only']),
                                'hash_mismatch': len(diff['hash_mismatch']), 'unbacked_codex': len(diff['unbacked_codex'])},
                'call_summaries': call_summaries(session)}, 0


AFTER_REVEAL = ('REVEALED', 'CODEX_CRITIQUED', 'CLAUDE_CRITIQUE_FROZEN', 'DEBATED', 'SYNTHESIZED')


def proposal_research_urls(sid):
    """公開済みの両提案が挙げた外部調査 URL と web 根拠 URL（リンク確認の対象集合）。

    公開後は両提案が必ずあるので、読めなければ黙って空にせず拒否する（Claude 提案は MAX_DOC まで許される）。
    """
    urls = set()
    for name, limit in (('claude/proposal.json', MAX_DOC), ('codex/proposal.json', MAX_RESULT)):
        try:
            proposal = read_json_artifact(sid, name, limit)
        except (SchemaError, OSError) as error:
            raise Refused(f'cannot read revealed {name}: {str(error)[:80]}') from None
        if not isinstance(proposal, dict):
            raise Refused(f'revealed {name} is not a JSON object')
        research = proposal.get('research')
        if isinstance(research, dict):
            urls.update(item.get('url', '').strip() for key in RESEARCH_LISTS for item in research.get(key, [])
                        if isinstance(item, dict))
        urls.update(e.get('ref', '').strip() for e in proposal.get('evidence', [])
                    if isinstance(e, dict) and e.get('kind') == 'web')
    return {u for u in urls if WEB_URL.fullmatch(u)}


def cmd_research_check(args):
    """公開後にメインが実際に開いて確かめたリンクの結果を検査して保存する（再提出で置き換え）。"""
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        require_state(session, *AFTER_REVEAL)
        data = validation_candidate(sid, 'research_check.json', args.file)
        try:
            document = decode_json(data)
            checks = document.get('checks') if isinstance(document, dict) and set(document) == {'checks'} else None
            if not isinstance(checks, list) or not checks:
                raise SchemaError('research check must be {"checks": [...]} with at least one entry')
            known = proposal_research_urls(sid)
            seen = set()
            for index, entry in enumerate(checks):
                if not isinstance(entry, dict) or set(entry) != {'url', 'status', 'observed_title', 'note'} \
                        or not all(isinstance(entry[k], str) for k in entry):
                    raise SchemaError(f'checks[{index}] must have string url, status, observed_title, note')
                url = entry['url'].strip()
                if url not in known:
                    raise SchemaError(f'checks[{index}].url is not a research or web evidence URL of either proposal')
                if url in seen:
                    raise SchemaError(f'checks[{index}].url is duplicated')
                if entry['status'] not in RESEARCH_CHECK_STATUS:
                    raise SchemaError(f'checks[{index}].status must be one of {", ".join(RESEARCH_CHECK_STATUS)}')
                if entry['status'] in ('verified', 'mismatch') and not entry['observed_title'].strip():
                    raise SchemaError(f'checks[{index}].observed_title is required when the page was opened')
                seen.add(url)
            check_lengths(document)
        except SchemaError as error:
            return {'status': 'INVALID', 'error': str(error)}, 2
        target = art(sid) / 'research_check.json'
        preflight_validation_target(target)
        saved = dump({'checked_at': now(), 'checks': checks})
        write_private(target, saved)
        counts = {status: sum(1 for c in checks if c['status'] == status) for status in RESEARCH_CHECK_STATUS}
        return {'status': 'ok', 'path': str(target), 'sha256': sha256(saved), 'counts': counts,
                'unchecked_urls': sorted(known - seen)}, 0


def known_evidence(session):
    return {e['id'] for e in session['evidence']} | codex_evidence_ids(session)


def codex_last_stances(session):
    stances = {}
    for phase in ('debate', 'critique', 'rebuttal', 'round2'):
        result = codex_result(session, phase)
        if result:
            for response in result['responses_to_claude_critique']:
                stances[response['issue_id']] = response['stance']
    return stances


def validate_ledger(session, ledger, stage, critique=False):
    """ledger の構造と意味規則を検証し、警告一覧を返す。違反は SchemaError。"""
    validate_doc(ledger, 'ledger')
    ids = [i['id'] for i in ledger['issues']]
    if len(ids) != len(set(ids)) or any(not ISSUE_ID.fullmatch(i) for i in ids):
        raise SchemaError('issue ids must be unique ids such as I3 or C1')
    known = known_evidence(session)
    counts = {name: len(input_text(session['sid'], name).splitlines()) for name in INPUT_FILES}
    warnings = []
    for issue in ledger['issues']:
        label = issue['id']
        if critique and issue['origin'] != 'claude':
            raise SchemaError(f'{label}: frozen Claude critique issues must have origin claude')
        if not issue['title'].strip():
            raise SchemaError(f'{label}: empty title')
        if issue['type'] == 'fact' and issue['decision_method'] == 'user-decision':
            raise SchemaError(f'{label}: fact issues are decided by source text or reproduction, not user decision')
        refs = issue['claude']['evidence_ids'] + issue['codex']['evidence_ids'] + issue['verification']['evidence_ids']
        refs += [e for turn in issue['turns'] for e in turn['evidence_ids']]
        for ref in refs:
            if not (ref in known or link_ref(ref, [], counts)[0] == 'input'):
                raise SchemaError(f'{label}: unknown evidence id {ref[:40]}')
        verification = issue['verification']
        if verification['result'] != 'unverified':
            if not verification['evidence_ids'] or not verification['method'].strip():
                raise SchemaError(f'{label}: confirmed/refuted verification needs a method and evidence ids')
            if issue['type'] == 'fact' and issue['decision_method'] not in ('source-text', 'reproduction'):
                raise SchemaError(f'{label}: fact verification must use source-text or reproduction')
        if issue['origin'] == 'claude' and (issue['claude']['stance'] == 'pending' or not issue['claude']['position'].strip()):
            raise SchemaError(f'{label}: Claude-raised issues need a Claude position and stance')
        if any(t['speaker'] == 'claude' and not t['text'].strip() for t in issue['turns']):
            # scaffold の空欄（Claude の批評・回答）が残っている。
            raise SchemaError(f'{label}: Claude {"/".join(sorted({t["kind"] for t in issue["turns"] if t["speaker"] == "claude" and not t["text"].strip()}))} text is empty')
        if stage == 'final' and issue['claude']['stance'] == 'pending':
            raise SchemaError(f'{label}: Claude stance is still pending')
        if stage == 'final' and issue['importance'] in ('high', 'medium'):
            if 'pending' in (issue['claude']['stance'], issue['codex']['stance']):
                raise SchemaError(f'{label}: high/medium issues need final stances from both sides')
            if issue['decision_method'] == 'pending':
                raise SchemaError(f'{label}: decision method is pending')
    if stage == 'final':
        # Claude は Codex の批評の一つ一つに答える。
        by_id = {i['id']: i for i in ledger['issues']}
        for phase in ('debate', 'critique', 'round2'):
            result = codex_result(session, phase)
            for item in (result or {}).get('critique_of_claude', []):
                issue = by_id.get(item['issue_id'])
                if issue is None:
                    raise SchemaError(f'Codex critique {item["issue_id"]} is missing from the ledger')
                if not any(t['speaker'] == 'claude' and t['kind'] == 'answer' for t in issue['turns']):
                    raise SchemaError(f'{item["issue_id"]}: Claude has not answered this Codex critique')
        stances = codex_last_stances(session)
        for issue in ledger['issues']:
            reported = stances.get(issue['id'])
            if reported and issue['codex']['stance'] != reported:
                warnings.append(f'{issue["id"]}: ledger Codex stance differs from Codex output ({reported})')
    if stage == 'pre-debate' and not critique:
        try:
            diff = read_json_artifact(session['sid'], 'reveal/diff.json')
            if diff['hash_mismatch']:
                warnings.append('reveal diff has hash mismatches; record decisive ones as type=fact issues')
        except (Refused, SchemaError):
            pass
    return warnings


def save_format_revision(sid, name, original, document, changes):
    """Only after strict validation: preserve the exact original and the mechanical change list."""
    target = art(sid) / name
    audit = write_format_audit(sid, name, original, changes)
    data = dump(document)
    write_private(target, data)
    return data, str(audit)


def write_format_audit(sid, name, original, changes):
    audit = art(sid) / 'format-revisions' / (name.replace('/', '-') + '-' + secrets.token_hex(6) + '.json')
    write_frozen(audit, dump({'artifact': name, 'original_sha256': sha256(original),
                              'original_utf8': original.decode('utf-8'), 'changes': changes}))
    return audit


def input_ref_normalizer(session, changes):
    counts = {name: len(input_text(session['sid'], name).splitlines()) for name in INPUT_FILES}
    alias = re.compile(r'(brief(?:\.md)?|scope(?:\.json)?|index|evidence_index\.md):(?:L)?(\d+)(?:-L?(\d+))?')

    def ref(value, path):
        match = alias.fullmatch(value) if isinstance(value, str) else None
        if not match:
            return value
        name = {'brief.md': 'brief', 'scope.json': 'scope', 'evidence_index.md': 'index'}.get(match[1], match[1])
        a, b = int(match[2]), int(match[3] or match[2])
        if not 1 <= a <= b <= counts[name]:
            raise SchemaError('input reference outside input lines: ' + value)
        canonical = f'{name}:L{a}' + (f'-L{b}' if match[3] else '')
        if canonical != value:
            changes.append({'field': path, 'before': value, 'after': canonical})
        return canonical
    return ref


def normalize_ledger_refs(session, document):
    """Normalize only whole, explicit reference fields; never rewrite free prose or unknown IDs."""
    changes = []
    ref = input_ref_normalizer(session, changes)
    for index, issue in enumerate(document.get('issues', [])):
        root = f'issues[{index}]'
        if 'requirement_ref' in issue:
            issue['requirement_ref'] = ref(issue['requirement_ref'], root + '.requirement_ref')
        groups = [(side, issue.get(side)) for side in ('claude', 'codex', 'verification')]
        groups += [(f'turns[{n}]', turn) for n, turn in enumerate(issue.get('turns', []))]
        for label, group in groups:
            if isinstance(group, dict) and isinstance(group.get('evidence_ids'), list):
                group['evidence_ids'] = [ref(v, f'{root}.{label}.evidence_ids[{n}]')
                                         for n, v in enumerate(group['evidence_ids'])]
    return changes


def normalize_proposal_refs(session, document):
    changes = []
    ref = input_ref_normalizer(session, changes)
    for n, evidence in enumerate(document['evidence']):
        if evidence['kind'] == 'brief':
            parts = re.split(r'([;,]\s*)', evidence['ref'])
            evidence['ref'] = ''.join(ref(part, f'evidence[{n}].ref') if i % 2 == 0 else part
                                      for i, part in enumerate(parts))
    return changes


def scaffold_hints(session, kind, document):
    schema = decode_json((SCHEMAS / (kind + '.schema.json')).read_bytes())
    enums = {}

    def walk(node, path=''):
        if not isinstance(node, dict):
            return
        if 'enum' in node:
            enums[path] = node['enum']
        for key, value in node.get('properties', {}).items():
            walk(value, path + '.' + key if path else key)
        if 'items' in node:
            walk(node['items'], path + '[]')
    walk(schema)
    issues = document.get('issues') or (_artifact_or_none(session['sid'], 'ledger.json') or {}).get('issues', [])

    def type_text(node):
        if not isinstance(node, dict):
            return 'any'
        if 'enum' in node:
            return 'one of: ' + ' | '.join(str(v) for v in node['enum'])
        if node.get('type') == 'array':
            return [type_text(node.get('items'))]
        return node.get('type', 'any')

    # 草稿で空配列のまま渡す欄のうち、要素がオブジェクトのものは形が見えないので、スキーマから要素の形を示す。
    shapes = {}

    def shape_walk(node, value, path=''):
        if not isinstance(node, dict) or not isinstance(value, dict):
            return
        for key, child in node.get('properties', {}).items():
            here = path + '.' + key if path else key
            if isinstance(value.get(key), list) and not value[key]:
                items = child.get('items') if isinstance(child, dict) else None
                if isinstance(items, dict) and items.get('type') == 'object' and items.get('properties'):
                    shapes[here + '[]'] = {'required': list(items.get('required', [])),
                                           'fields': {k: type_text(v) for k, v in items['properties'].items()}}
            elif isinstance(value.get(key), dict):
                shape_walk(child, value[key], here)
    shape_walk(schema, document)
    counts = {name: len(input_text(session['sid'], name).splitlines()) for name in INPUT_FILES}
    known = known_evidence(session)
    proposal = _artifact_or_none(session['sid'], 'claude/proposal.json') or {}
    proposal_only = [{'id': e['id'], 'kind': e['kind']} for e in proposal.get('evidence', []) if e['id'] not in known]
    suggested = 1
    issue_ids = {i['id'] for i in issues}
    while 'I' + str(suggested) in issue_ids:
        suggested += 1
    return {'known_evidence_ids': sorted(known),
            'known_issue_ids': [i['id'] for i in issues], 'input_line_counts': counts,
            'new_issue_id_example': 'I' + str(suggested),
            'proposal_ids_not_ledger_refs': proposal_only,
            'evidence_ref_rule': 'Ledger refs accept only known_evidence_ids or valid frozen input line references. A proposal brief/inference E ID is not a registered ledger ref. Keep the original proposal/evidence unchanged; do not promote inference, delete evidence, or guess an ID replacement.',
            'reference_examples': [f'{name}:L1' for name, count in counts.items() if count],
            'enum_choices': enums,
            'item_shapes': shapes,
            'semantic_fields': 'Choose stance, importance, agreement, verification, metrics and adoption from evidence; no automatic inference.'}


def validation_candidate(sid, name, source, limit=MAX_DOC):
    """明示した一般初稿だけを受け取り、保存先の指定は受け取らない。"""
    if source is None:
        return read_artifact(sid, name)
    path = Path(source)
    if not path.is_absolute() or not _within(path.resolve(strict=True), PROJECT):
        raise Refused('validation source must be an absolute draft path inside the current project')
    data, _, _ = read_source(path, limit)
    return data


def preflight_validation_target(path):
    """保存・監査を始める前に、runner が計算した通常ファイルの宛先を確認する。"""
    path = Path(path)
    if any(p.is_symlink() for p in [path, *path.parents] if str(p) not in {'/tmp', '/var'}):
        raise Refused('validation target must not be reached through a symlink')
    if not path.parent.is_dir() or not os.access(path.parent, os.W_OK | os.X_OK):
        raise Refused('validation target parent must be an existing writable directory')
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused('validation target must be a regular, unlinked file')


def preflight_validation_commit(sid, name, changes):
    target = art(sid) / name
    preflight_validation_target(target)
    if changes:
        directory = art(sid) / 'format-revisions'
        if directory.is_symlink() or (directory.exists() and
                                     (not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK))):
            raise Refused('format revision directory must be a writable directory without symlinks')
    return target


def cmd_ledger_validate(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        if args.stage == 'pre-debate':
            require_state(session, 'REVEALED')
            if session['plan'] != 'A':
                raise Refused('plan B freezes the Claude critique with `freeze --side claude-critique`')
        else:
            require_state(session, 'DEBATED', 'SYNTHESIZED')
        partial = getattr(args, 'partial_file', None)
        source = getattr(args, 'file', None) or partial
        data = (PARTIAL.candidate(SimpleNamespace(**globals()), session, 'ledger', args.stage, partial) if partial else
                validation_candidate(sid, 'ledger.json', source))
        try:
            document = decode_json(data)
            validate_doc(document, 'ledger')
            changes = normalize_ledger_refs(session, document) if getattr(args, 'normalize_refs', False) else []
            warnings = validate_ledger(session, document, args.stage)
        except SchemaError as error:
            return {'status': 'INVALID', 'error': str(error)}, 2
        output = {'status': 'ok', 'stage': args.stage, 'warnings': warnings}
        if args.stage == 'pre-debate' and ((art(sid) / 'ledger.pre-debate.json').exists() or
                                         (art(sid) / 'ledger.pre-debate.json').is_symlink()):
            raise Refused('pre-debate ledger is already frozen')
        target = art(sid) / 'ledger.json'
        if source is not None:
            target = preflight_validation_commit(sid, 'ledger.json', changes)
            if args.stage == 'pre-debate':
                preflight_validation_target(art(sid) / 'ledger.pre-debate.json')
        if changes:
            data, audit = save_format_revision(sid, 'ledger.json', data, document, changes)
            output.update(normalized_refs=changes, format_revision=audit)
        elif source is not None:
            write_private(target, data)
        if args.stage == 'pre-debate':
            frozen = art(sid) / 'ledger.pre-debate.json'
            write_frozen(frozen, data)
            session['frozen']['ledger-pre-debate'] = {'path': 'ledger.pre-debate.json', 'sha256': sha256(data), 'frozen_at': now()}
            save_session(session)
            output['frozen'] = str(frozen)
        output.update(path=str(target), sha256=sha256(data))
        if partial:
            output['next'] = PARTIAL.next_command(SimpleNamespace(**globals()), session, 'ledger', args.stage)
        return output, 0


def conditional_next(session, ledger, synthesis, user_requested):
    sid = session['sid']
    debate_end = debate_ended_at(session)
    fresh = {e['id'] for e in session['evidence'] if e['added_at'] > debate_end}
    try:
        before = read_json_artifact(sid, 'ledger.pre-debate.json')
    except (Refused, SchemaError):
        try:
            before = read_json_artifact(sid, 'claude/critique.json')
        except (Refused, SchemaError):
            before = {'issues': []}
    before = {i['id']: i for i in before['issues']}
    high = [i for i in ledger['issues'] if i['importance'] == 'high']
    round2, round2_ids = [], []
    if user_requested:
        round2.append('user requested an additional round')
        round2_ids = [i['id'] for i in high]
    for issue in high:
        old = before.get(issue['id'])
        changed = old is None or old['verification']['result'] != issue['verification']['result'] or old['status'] != issue['status']
        if changed and fresh & set(issue['verification']['evidence_ids']):
            round2.append(f'{issue["id"]}: verification/status changed by evidence added after the debate')
            round2_ids.append(issue['id'])
    fidelity, fidelity_ids = [], []
    covered = {d['issue_id'] for d in synthesis['decisions']} | {u['issue_id'] for u in synthesis['unresolved']}
    decisions = {d['issue_id']: d for d in synthesis['decisions']}
    stances = codex_last_stances(session)
    by_id = {i['id']: i for i in ledger['issues']}
    for issue in high:
        if issue['id'] not in covered:
            fidelity.append(f'{issue["id"]}: high issue omitted from the synthesis')
            fidelity_ids.append(issue['id'])
    for issue_id, decision in decisions.items():
        issue = by_id[issue_id]
        if decision['adopted_from'] != 'codex' and issue['codex']['stance'] != 'accept':
            fidelity.append(f'{issue_id}: adopted from {decision["adopted_from"]} while Codex final stance is {issue["codex"]["stance"]}')
            fidelity_ids.append(issue_id)
        elif issue['codex']['stance'] == 'conditional' and not decision['conditions']:
            fidelity.append(f'{issue_id}: Codex conditions are not carried into the decision')
            fidelity_ids.append(issue_id)
    for issue_id, stance in stances.items():
        if issue_id in by_id and by_id[issue_id]['codex']['stance'] != stance:
            fidelity.append(f'{issue_id}: ledger Codex stance differs from Codex output')
            fidelity_ids.append(issue_id)
    if round2:
        return 'round2', round2, sorted(set(round2_ids)), fidelity
    if fidelity:
        return 'fidelity', fidelity, sorted(set(fidelity_ids)), []
    return 'none', [], [], []


def cmd_synthesis_validate(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        require_state(session, 'DEBATED', 'SYNTHESIZED')
        partial = getattr(args, 'partial_file', None)
        source = getattr(args, 'file', None) or partial
        ledger_data = read_artifact(sid, 'ledger.json')
        synthesis_data = (PARTIAL.candidate(SimpleNamespace(**globals()), session, 'synthesis', 'final', partial) if partial else
                          validation_candidate(sid, 'synthesis.json', source))
        ledger = decode_json(ledger_data)
        try:
            warnings = validate_ledger(session, ledger, 'final')
            synthesis = decode_json(synthesis_data)
            derive = bool(partial) or getattr(args, 'derive_checked', False)
            if derive and isinstance(synthesis, dict) and isinstance(synthesis.get('decisions'), list):
                for decision in synthesis['decisions']:
                    if isinstance(decision, dict):
                        decision['codex_checked'] = False
            validate_doc(synthesis, 'synthesis')
            ids = {i['id'] for i in ledger['issues']}
            decided = [d['issue_id'] for d in synthesis['decisions']]
            if len(decided) != len(set(decided)) or any(i not in ids for i in decided):
                raise SchemaError('synthesis decisions must reference unique ledger issues')
            if any(u['issue_id'] not in ids for u in synthesis['unresolved']):
                raise SchemaError('unresolved entries must reference ledger issues')
            empty = [d['issue_id'] for d in synthesis['decisions'] if not d['decision'].strip() or not d['reason'].strip()]
            if empty:
                raise SchemaError('decision and reason must be filled for: ' + ', '.join(empty))
            if not synthesis['useful_from_codex'] and not synthesis['useful_none_reason'].strip():
                raise SchemaError('useful_none_reason is required when nothing useful was taken from Codex')
            metrics = synthesis['metrics']
            if min(metrics['one_side_confirmed_findings'].values()) < 0 or metrics['missing_required'] < 0 or metrics['unbacked_claims'] < 0:
                raise SchemaError('metrics must be nonnegative')
            checked = checked_issues(session, ledger, synthesis)
            if derive:
                for decision in synthesis['decisions']:
                    decision['codex_checked'] = decision['issue_id'] in checked
            wrong = [d['issue_id'] for d in synthesis['decisions'] if d['codex_checked'] and d['issue_id'] not in checked]
            if wrong:
                raise SchemaError('codex_checked=true without a faithful fidelity verdict on the current ledger/synthesis '
                                  '(round2 does not count): ' + ', '.join(wrong))
        except SchemaError as error:
            return {'status': 'INVALID', 'error': str(error)}, 2
        nxt, reasons, issue_ids, deferred = conditional_next(session, ledger, synthesis, args.user_requested)
        revision = None
        changes = []
        if derive and decode_json(synthesis_data) != synthesis:
            original_doc = decode_json(synthesis_data)
            changes = [{'field': f'decisions[{n}].codex_checked', 'before': old.get('codex_checked'),
                        'after': new['codex_checked']}
                       for n, (old, new) in enumerate(zip(original_doc['decisions'], synthesis['decisions']))
                       if old.get('codex_checked') != new['codex_checked']]
        blocked = None
        if nxt != 'none':
            if session.get('conditional_used'):
                blocked = 'conditional budget already used by ' + session['conditional_used']
            elif len(session['calls']) >= session['max_calls']:
                blocked = 'call limit reached'
            elif codex_remaining(session) < MIN_REMAINING:
                blocked = 'BUDGET_EXHAUSTED: less than 60 s of Codex time budget left'
        target = art(sid) / 'synthesis.json'
        if source is not None:
            target = preflight_validation_commit(sid, 'synthesis.json', changes)
        if changes:
            synthesis_data, revision = save_format_revision(sid, 'synthesis.json', synthesis_data, synthesis, changes)
        elif source is not None:
            write_private(target, synthesis_data)
        session['conditional'] = {'next': 'none' if blocked else nxt, 'computed': nxt, 'reasons': reasons,
                                  'issue_ids': issue_ids, 'deferred_fidelity': deferred, 'blocked': blocked,
                                  'synthesis_sha256': sha256(synthesis_data), 'ledger_sha256': sha256(ledger_data),
                                  'user_requested': bool(args.user_requested)}
        session['state'] = 'SYNTHESIZED'
        save_session(session)
        output = {'status': 'ok', 'state': 'SYNTHESIZED', 'next': session['conditional']['next'], 'computed': nxt,
                  'reasons': reasons, 'issue_ids': issue_ids, 'warnings': warnings, 'blocked': blocked,
                  'path': str(target), 'sha256': sha256(synthesis_data)}
        if derive:
            output.update(derived_checked=sorted(checked), format_revision=revision)
        if partial:
            output['partial_next'] = PARTIAL.next_command(SimpleNamespace(**globals()), session, 'synthesis', 'final')
        unchecked = [d['issue_id'] for d in synthesis['decisions'] if not d['codex_checked']]
        if output['next'] == 'none' and unchecked:
            output['note'] = CHECK_UNVERIFIED + ': ' + ', '.join(unchecked)
        return output, 0


def canonical_digest(document, strip_checked=False):
    """整形・キー順に依存しない内容ハッシュ。synthesis は codex_checked を除いて比べる（確認後に旗を立てられるように）。"""
    if strip_checked and isinstance(document, dict) and isinstance(document.get('decisions'), list):
        document = dict(document, decisions=[{k: v for k, v in d.items() if k != 'codex_checked'} if isinstance(d, dict) else d
                                             for d in document['decisions']])
    return sha256(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))


def check_basis(ledger, synthesis):
    return {'ledger': canonical_digest(ledger), 'synthesis': canonical_digest(synthesis, strip_checked=True)}


def basis_matches(session, ledger, synthesis):
    """fidelity 呼び出しの判定基準内容と現在の ledger・synthesis が同じか（基準記録がなければ None）。"""
    call = phase_call(session, 'fidelity')
    basis = (call or {}).get('checked_basis')
    if not basis or ledger is None or synthesis is None:
        return None
    current = check_basis(ledger, synthesis)
    return basis.get('ledger') == current['ledger'] and basis.get('synthesis') == current['synthesis']


def checked_issues(session, ledger=None, synthesis=None):
    """codex_checked=true を認める争点（D1）。

    fidelity が faithful と判定し、その争点が呼び出しで判定を求めた対象（checked_basis.issue_ids）に含まれ、
    呼び出しに使った ledger・synthesis の内容（codex_checked を除く）が現在と同じものだけ。
    round2 は討論であって統合案の確認ではないので根拠にしない。内容ハッシュのない旧セッションの呼び出しも認めない。
    """
    if session.get('conditional_used') != 'fidelity' or ledger is None or synthesis is None:
        return set()
    call = phase_call(session, 'fidelity')
    if not call or call['execution'] != 'ok' or not basis_matches(session, ledger, synthesis):
        return set()
    result = codex_result(session, 'fidelity')
    if not result:
        return set()
    covered = set(call['checked_basis'].get('issue_ids') or [])
    return {i['issue_id'] for i in result['issues'] if i['verdict'] == 'faithful'} & covered


# 表示用の韓国語ラベル（報告書はユーザー向け）。引用する Claude/Codex の本文は訳さない。
KO = {
    'adopted': {'claude': 'Claude', 'codex': 'Codex', 'both': '양측', 'neither': '둘 다 기각', 'evidence': '근거'},
    'stance': {'pending': '미정', 'proposed': '제안', 'accept': '수용', 'reject': '반대', 'conditional': '조건부', 'open': '보류'},
    'verdict': {'faithful': '충실', 'distorted': '왜곡', 'omitted': '누락'},
    'method': {'pending': '미정', 'source-text': '원문', 'reproduction': '재현', 'user-decision': '사용자 결정',
               'undecidable': '판정 불가'},
    'verification': {'confirmed': '확인됨', 'refuted': '반박됨', 'unverified': '미검증'},
    'status': {'agreed': '합의', 'resolved-by-evidence': '근거로 해소', 'conditional': '조건부', 'open-user': '사용자 결정 대기',
               'open-unverified': '미검증 미해결'},
    'type': {'fact': '사실', 'design-tradeoff': '설계 트레이드오프', 'preference': '선호', 'scope': '범위'},
    'importance': {'high': '높음', 'medium': '중간', 'low': '낮음'},
    'origin': {'claude': 'Claude', 'codex': 'Codex', 'reveal-diff': '공개 차이'},
    'speaker': {'claude': 'Claude', 'codex': 'Codex'},
    'kind': {'critique': '비평', 'response': '응답', 'answer': '답변'},
    'phase': {'proposal': '제안', 'debate': '토론', 'critique': '비평', 'rebuttal': '반론', 'round2': '추가 토론',
              'fidelity': '충실성 검사'},
    'execution': {'ok': '정상', 'failed': '실패', 'stopped': '정지', 'running': '실행 중'},
    'call_status': {'COMPLETE': '완료', 'INCOMPLETE': '미완료'},
    'final_state': {'FINALIZED': '완료', 'STOPPED': '정지'},
    'sufficiency': {'sufficient': '충분', 'partial': '부분', 'insufficient': '부족'},
    'summary': {'ok': '있음', 'missing': '누락'},
    'usage_status': {'complete': '전체', 'partial': '일부', 'unknown': '알 수 없음'},
    'usage': {'input_tokens': '입력', 'cached_input_tokens': '캐시 입력', 'output_tokens': '출력'},
    'bool': {True: '예', False: '아니오'},
}


def ko(group, value):
    """列挙値の表示ラベル。未知の値はそのまま出す。"""
    try:
        return KO[group].get(value, str(value))
    except TypeError:
        return str(value)


CHECK_UNVERIFIED = 'Claude 통합 판단(Codex 미확인)'


def check_label(decision, checked):
    if decision['codex_checked'] and decision['issue_id'] in checked:
        return 'Codex 확인됨(충실성 검사: 충실)'
    if decision['codex_checked']:
        return 'codex_checked 주장만 있음(현재 내용에 대한 확인 근거 없음)'
    return CHECK_UNVERIFIED


def render_debate(ledger):
    lines = ['# 토론 원장', '']
    for issue in ledger['issues']:
        lines += [f'## {issue["id"]} [{ko("importance", issue["importance"])}/{ko("type", issue["type"])}] {issue["title"]}',
                  f'- 제기: {ko("origin", issue["origin"])} / 상태: {ko("status", issue["status"])} / '
                  f'판정 방법: {ko("method", issue["decision_method"])}',
                  f'- Claude ({ko("stance", issue["claude"]["stance"])}): {issue["claude"]["position"]}',
                  f'- Codex ({ko("stance", issue["codex"]["stance"])}): {issue["codex"]["position"]}',
                  f'- 검증: {ko("verification", issue["verification"]["result"])} ({issue["verification"]["method"]})']
        for turn in issue['turns']:
            lines.append(f'  - {turn["phase"]} {ko("speaker", turn["speaker"])} {ko("kind", turn["kind"])}: {turn["text"]}')
        lines.append('')
    return '\n'.join(lines) + '\n'


def render_synthesis(synthesis, checked=frozenset()):
    lines = ['# 통합안', '', '| 쟁점 | 결정 | 채택 출처 | Codex 확인 |', '|---|---|---|---|']
    for d in synthesis['decisions']:
        lines.append(f'| {_cell(d["issue_id"])} | {_cell(d["decision"])} | {ko("adopted", d["adopted_from"])} | '
                     f'{check_label(d, checked)} |')
    lines += ['', '## 결정 상세']
    for d in synthesis['decisions']:
        lines += ['', f'### {d["issue_id"]}: {_flat(d["decision"])}', f'- 채택 출처: {ko("adopted", d["adopted_from"])}',
                  f'- 이유: {_flat(d["reason"])}',
                  f'- 기각한 대안: {_joined(d["rejected_alternatives"])}',
                  f'- 조건: {_joined(d["conditions"])}',
                  f'- 잔여 위험: {_flat(d["residual_risk"]) or "(없음)"}',
                  f'- Codex 확인: {check_label(d, checked)}']
    lines += ['', '## 필수', *[f'- {x}' for x in synthesis['required']], '', '## 선택',
              *[f'- {x}' for x in synthesis['optional']], '', '## 사용자 결정',
              *[f'- {u["issue_id"]}: {u["question"]} (선택지: {" / ".join(u["options"])})' for u in synthesis['user_decisions']],
              '', '## 미해결',
              *[f'- {u["issue_id"]}: {u["impact"]} / 필요한 근거: {u["needed_evidence"]} / 차단: {ko("bool", u["blocking"])}'
                for u in synthesis['unresolved']]]
    return '\n'.join(lines) + '\n'


def _flat(text):
    return ' '.join(str(text).split())


def _cell(text):
    return _flat(text).replace('|', '\\|')


def _joined(items, empty='(없음)'):
    return ' / '.join(_flat(x) for x in items) if items else empty


def _artifact_or_none(sid, relative, limit=MAX_DOC):
    try:
        return read_json_artifact(sid, relative, limit)
    except (Refused, SchemaError, OSError):
        return None


BRIEF_REPORT_LINES = 40


def _valid_or_none(document, schema):
    """報告に使う文書はスキーマを満たすものだけ（壊れていれば「산출물 없음」として扱う）。"""
    try:
        validate_doc(document, schema)
    except SchemaError:
        return None
    return document


def _proposal_section(title, proposal):
    lines = ['', title]
    if not isinstance(proposal, dict):
        return lines + ['- (산출물 없음)']
    lines.append(f'- 요약: {_flat(proposal.get("summary", ""))}')
    lines.append(f'- 상태: {ko("call_status", proposal.get("status"))} / 근거 충분성: '
                 f'{ko("sufficiency", proposal.get("evidence_sufficiency"))}')
    lines.append('- 핵심 구성요소:')
    lines += [f'  - {_flat(c)}' for c in proposal.get('design', {}).get('components', [])] or ['  - (없음)']
    lines.append('- 위험:')
    lines += [f'  - [{ko("importance", r.get("severity"))}] {_flat(r.get("risk", ""))} → 완화: {_flat(r.get("mitigation", ""))}'
              for r in proposal.get('risks', [])] or ['  - (없음)']
    return lines


def _report_proposal(proposal):
    """報告用の検証。research 追加前に凍結した提案は、その欄だけ任意として扱う（凍結・呼び出しの検証は現行スキーマのまま）。"""
    if isinstance(proposal, dict) and 'research' not in proposal:
        schema = load_schema('proposal')
        schema['required'] = [k for k in schema['required'] if k != 'research']
        try:
            check_schema(proposal, schema)
            check_lengths(proposal)
        except SchemaError:
            return None
        return proposal
    return _valid_or_none(proposal, 'proposal')


def _initial_proposal_table(claude, codex):
    """Compare frozen proposals, never reconstruct initial positions from the final ledger."""
    def values(proposal):
        proposal = _report_proposal(proposal)
        if proposal is None:
            return ['(산출물 없음/유효하지 않음)'] * 6
        return [
            proposal['summary'],
            _joined([f'{m["requirement"]}: {m["approach"]}' for m in proposal['requirement_mapping']]),
            _joined(proposal['design']['components']),
            _joined(proposal['design']['flows'] + proposal['design']['contracts']),
            _joined([f'[{r["severity"]}] {r["risk"]} → {r["mitigation"]}' for r in proposal['risks']]),
            f'상태: {ko("call_status", proposal["status"])} / 근거: {ko("sufficiency", proposal["evidence_sufficiency"])}'
            f' / 가정: {_joined(proposal["assumptions"])} / 질문: {_joined(proposal["open_questions"])}'
            f' / 미확인: {_joined(proposal["unverified"])}',
        ]

    lines = ['', '### 최초 제안 비교(토론 전 동결본)',
             '- 출처: `claude/proposal.json`, `codex/proposal.json`(artifacts 기준). 토론 후 입장은 4~7절과 구분한다.', '',
             '| 항목 | Claude 최초 제안 | Codex 최초 제안 |', '|---|---|---|']
    labels = ('핵심 제안', '요구사항별 접근', '구성요소', '흐름·계약', '위험·완화', '상태·전제·미확인')
    for label, left, right in zip(labels, values(claude), values(codex)):
        lines.append(f'| {label} | {_cell(left)} | {_cell(right)} |')
    return lines


def _turns(issue, speaker, kinds):
    return [t for t in issue.get('turns', []) if t.get('speaker') == speaker and t.get('kind') in kinds]


def _codex_items(session, group):
    """Codex 討論出力の項目（issue_id → 最後の項目）。"""
    items = {}
    for phase in ('debate', 'critique', 'rebuttal', 'round2'):
        result = codex_result(session, phase) if phase_call(session, phase) else None
        for item in (result or {}).get(group, []):
            items[item['issue_id']] = dict(item, phase=phase)
    return items


def _issue_heading(issue):
    return f'### {issue["id"]} [{ko("importance", issue["importance"])}/{ko("type", issue["type"])}] {_flat(issue["title"])}'


def render_report(session, summary, checked):
    """debate 報告書（D4）。モデル呼び出しなしで成果物から決定的に作る。欠けた成果物は「산출물 없음」と書く。"""
    sid = session['sid']
    ledger = _valid_or_none(_artifact_or_none(sid, 'ledger.json'), 'ledger')
    synthesis = _valid_or_none(_artifact_or_none(sid, 'synthesis.json'), 'synthesis')
    issues = ledger['issues'] if ledger else []
    lines = [f'# 토론 보고서: {session["question_id"]}', '',
             f'- 세션: `{sid}` / 방식: {session["plan"]}안 / 최종 상태: {ko("final_state", summary["final_state"])}']
    if session.get('stop'):
        lines.append(f'- 정지: {session["stop"]["category"]} — {_flat(session["stop"]["reason"])}')
    lines.append('- 이 보고서는 설계 제안의 기록이며 구현 승인이 아니다. 두 AI의 합의는 증거가 아니다.')

    lines += ['', '## 1. 질문·요구사항']
    try:
        brief = input_text(sid, 'brief').splitlines()
        lines += ['', *[f'> {line}' if line.strip() else '>' for line in brief[:BRIEF_REPORT_LINES]]]
        if len(brief) > BRIEF_REPORT_LINES:
            lines.append(f'> …(이하 생략: inputs/brief.md 전체 {len(brief)}행)')
    except (Refused, OSError, KeyError, UnicodeError):
        lines.append('- (산출물 없음)')

    claude_proposal = _artifact_or_none(sid, 'claude/proposal.json')
    codex_proposal = _artifact_or_none(sid, 'codex/proposal.json', MAX_RESULT)
    lines += _initial_proposal_table(claude_proposal, codex_proposal)
    lines += _proposal_section('## 2. Claude 제안', claude_proposal)
    lines += _proposal_section('## 3. Codex 제안', codex_proposal)

    lines += ['', '## 4. Claude→Codex 문제 제기와 응답']
    raised = [i for i in issues if i.get('origin') in ('claude', 'reveal-diff')]
    responses = _codex_items(session, 'responses_to_claude_critique')
    for issue in raised:
        lines += ['', _issue_heading(issue),
                  f'- Claude ({ko("stance", issue["claude"]["stance"])}): {_flat(issue["claude"]["position"])}']
        lines += [f'  - 비평: {_flat(t["text"])}' for t in _turns(issue, 'claude', ('critique',))]
        lines.append(f'- Codex 응답 ({ko("stance", issue["codex"]["stance"])}): {_flat(issue["codex"]["position"]) or "(없음)"}')
        lines += [f'  - {t["phase"]} {ko("kind", t["kind"])}: {_flat(t["text"])}' for t in _turns(issue, 'codex', ('response', 'critique'))]
        raw = responses.get(issue['id'])
        if raw:
            lines.append(f'  - Codex 출력 원문({ko("phase", raw["phase"])}, {ko("stance", raw["stance"])}): {_flat(raw["argument"])}'
                         f' / 판단을 바꿀 조건: {_flat(raw["would_change_if"])}')
    if not raised:
        lines.append('- (없음)')

    lines += ['', '## 5. Codex→Claude 문제 제기와 응답']
    critiques = _codex_items(session, 'critique_of_claude')
    from_codex = [i for i in issues if i.get('origin') == 'codex' or i['id'] in critiques]
    for issue in from_codex:
        raw = critiques.get(issue['id'])
        lines += ['', _issue_heading(issue)]
        if raw:
            lines += [f'- Codex 지적({ko("phase", raw["phase"])}): {_flat(raw["claim"])}', f'- Codex 대안: {_flat(raw["alternative"])}',
                      f'- 판단을 바꿀 조건: {_flat(raw["would_change_if"])}']
        else:
            lines.append(f'- Codex ({ko("stance", issue["codex"]["stance"])}): {_flat(issue["codex"]["position"])}')
        lines.append(f'- Claude 응답 ({ko("stance", issue["claude"]["stance"])}): {_flat(issue["claude"]["position"]) or "(없음)"}')
        lines += [f'  - 답변: {_flat(t["text"])}' for t in _turns(issue, 'claude', ('answer',))] or ['  - 답변: (없음)']
    if not from_codex:
        lines.append('- (없음)')

    lines += ['', '## 6. 쟁점별 판정', '']
    if issues:
        lines += ['| 쟁점 | 유형/중요도 | 판정 방법 | 검증 결과 | 근거 | 상태 |', '|---|---|---|---|---|---|']
        for issue in issues:
            verification = issue['verification']
            method = f' ({_cell(verification["method"])})' if verification['method'].strip() else ''
            lines.append(f'| {issue["id"]} | {ko("type", issue["type"])}/{ko("importance", issue["importance"])} | '
                         f'{ko("method", issue["decision_method"])} | {ko("verification", verification["result"])}{method} | '
                         f'{_cell(", ".join(verification["evidence_ids"])) or "-"} | {ko("status", issue["status"])} |')
    else:
        lines.append('- (산출물 없음)')

    lines += ['', '## 7. 토론 결과(결정·채택 출처·이유·조건·잔여 위험)']
    decisions = synthesis['decisions'] if synthesis else []
    for d in decisions:
        lines += ['', f'### {d["issue_id"]}: {_flat(d["decision"])}', f'- 채택 출처: {ko("adopted", d["adopted_from"])}',
                  f'- 이유: {_flat(d["reason"])}', f'- 기각한 대안: {_joined(d["rejected_alternatives"])}',
                  f'- 조건: {_joined(d["conditions"])}', f'- 잔여 위험: {_flat(d["residual_risk"]) or "(없음)"}',
                  f'- Codex 확인: {check_label(d, checked)}']
    if not decisions:
        lines.append('- (산출물 없음)')

    lines += ['', '## 8. 충실성 검사']
    fidelity = codex_result(session, 'fidelity') if phase_call(session, 'fidelity') else None
    if fidelity:
        lines += [f'- {i["issue_id"]}: {ko("verdict", i["verdict"])} — {_flat(i["detail"])}' for i in fidelity['issues']] \
            or ['- (판정 없음)']
        lines += [f'- 미확인: {_flat(u)}' for u in fidelity.get('unverified', [])]
        covered = (phase_call(session, 'fidelity').get('checked_basis') or {}).get('issue_ids')
        lines.append(f'- 판정 대상 쟁점: {", ".join(covered) if covered else "(기록 없음)"}')
        matches = basis_matches(session, ledger, synthesis)
        lines.append('- 판정 기준 내용과 현재 통합안·원장 일치: '
                     + ('알 수 없음(기준 기록 없음)' if matches is None else ko('bool', matches)))
    else:
        conditional = session.get('conditional') or {}
        used = session.get('conditional_used')
        lines.append('- 충실성 검사 호출 없음'
                     + (f' (조건부 호출: {ko("phase", used)})' if used else '')
                     + (f' / 계산된 다음 단계: {ko("phase", conditional["computed"]) if conditional["computed"] != "none" else "없음"}'
                        if conditional.get('computed') else '')
                     + (f' / 차단: {_flat(conditional["blocked"])}' if conditional.get('blocked') else ''))

    lines += ['', '## 9. 사용자 결정·미해결', '', '### 사용자 결정']
    if synthesis:
        lines += [f'- {u["issue_id"]}: {_flat(u["question"])} (선택지: {" / ".join(_flat(o) for o in u["options"])})'
                  for u in synthesis['user_decisions']] or ['- (없음)']
        lines += ['', '### 미해결']
        lines += [f'- {u["issue_id"]}: {_flat(u["impact"])} / 필요한 근거: {_flat(u["needed_evidence"])} / '
                  f'차단: {ko("bool", u["blocking"])}' for u in synthesis['unresolved']] or ['- (없음)']
    else:
        lines += ['- (산출물 없음)', '', '### 미해결', '- (산출물 없음)']

    lines += ['', '## 10. 비용']
    lines.append(f'- Codex 호출: {summary["codex_calls_total"]}회 / 상한 {summary["max_calls"]}회')
    for call in summary['calls']:
        elapsed = f'{call["elapsed_seconds"]}초' if call.get('elapsed_seconds') is not None else '시간 미상'
        lines.append(f'  - {call["index"]}. {ko("phase", call["phase"])}: {ko("execution", call["execution"])} / '
                     f'{ko("call_status", call["status"])} / {elapsed} / 요약 {ko("summary", call.get("summary", "missing"))}')
    lines.append(f'- Codex 실행 시간 합계: {summary["codex_seconds"]}초 (상한 {summary["codex_budget_seconds"]}초, '
                 f'상한 도달: {ko("bool", summary["over_budget"])})')
    lines.append(f'- 전체 경과 시간(Claude 작업 포함, 제한 없음): {summary["wall_seconds"]}초')
    usage = summary['usage_total']
    usage_text = 'usage 미보고 또는 요약 없음' if usage is None else ', '.join(f'{ko("usage", k)} {v}' for k, v in usage.items())
    lines.append(f'- 토큰(Codex 호출 합계, 집계 범위: {ko("usage_status", summary["usage_status"])}): {usage_text}')
    violations = summary['violations'] if summary['violations'] is not None else \
        f'알 수 없음(관측 {summary["violations_observed"]}건)'
    lines.append(f'- 요약 누락: {summary["summaries_missing"]}건 / 위반: {violations}')
    lines.append('- 토큰은 Codex 호출의 provider 보고값이며 Claude 쪽 사용량은 포함하지 않는다.')
    lines += _research_section(sid, claude_proposal, codex_proposal)
    return '\n'.join(lines) + '\n'


RESEARCH_LABELS = {'trends': '최신 동향', 'common_practices': '일반적인 구현·기술', 'papers': '관련 논문'}
CHECK_LABELS = {'verified': '확인됨', 'mismatch': '내용 불일치', 'unreachable': '열 수 없음', 'not-checked': '미확인'}


def _research_section(sid, claude_proposal, codex_proposal):
    """11 節: 両側の外部調査を URL で統合し、メインのリンク確認結果を並べる（番号 1〜10 は変えない）。"""
    lines = ['', '## 11. 외부 조사(최신 동향·일반 기술·논문)']
    research_schema = load_schema('proposal')['properties']['research']
    researches, evidence = {}, {}
    for name, proposal in (('Claude', claude_proposal), ('Codex', codex_proposal)):
        proposal = proposal if isinstance(proposal, dict) else {}
        research = proposal.get('research')
        try:
            check_schema(research, research_schema)
        except SchemaError:
            research = None
        researches[name] = research
        # 外部調査欄に無い web 根拠 URL も、確認結果が報告から消えないよう別表に出す。
        evidence[name] = [e for e in proposal.get('evidence', []) if isinstance(e, dict) and e.get('kind') == 'web'
                          and isinstance(e.get('ref'), str) and isinstance(e.get('observation'), str)]
        if research is None:
            missing = 'research' not in proposal
            lines.append(f'- {name}: 조사 기록 없음(' + ('이 세션의 제안에 research 필드가 없음' if missing else 'research 형식이 유효하지 않음') + ')')
            continue
        used = '웹 조사함' if research['web_used'] else '웹 조사 안 함'
        reason = research['not_applicable_reason'].strip()
        lines.append(f'- {name}: 기준일 {_flat(research["question_date"])} / {used}'
                     + (f' / 해당 없음 사유: {_flat(reason)}' if reason else ''))
    # 手で壊された確認記録でも finalize を止めない。形式の合う項目だけ使う。
    document = _artifact_or_none(sid, 'research_check.json')
    entries = document.get('checks') if isinstance(document, dict) and isinstance(document.get('checks'), list) else []
    status = {c['url'].strip(): c for c in entries
              if isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ('url', 'status', 'observed_title', 'note'))
              and c['status'] in RESEARCH_CHECK_STATUS}

    def checked(url):
        check = status.get(url)
        if not check:
            return '미확인'
        text = CHECK_LABELS[check['status']]
        if check['observed_title'].strip():
            text += f' ({_flat(check["observed_title"])})'
        if check['note'].strip():
            text += f' — {_flat(check["note"])}'
        return text

    def table(merged):
        rows = ['', '| 내용 | 링크 | 제출 | 링크 확인 |', '| --- | --- | --- | --- |']
        for url, row in merged.items():
            sides = '양측' if len(set(row['sides'])) > 1 else row['sides'][0]
            rows.append(f'| {_cell(" / ".join(row["texts"]))} | {_cell(url)} | {sides} | {_cell(checked(url))} |')
        return rows

    listed = set()
    for key, label in RESEARCH_LABELS.items():
        merged = {}
        for name, research in researches.items():
            for item in research[key] if research else []:
                url = item['url'].strip()
                if key == 'papers':
                    meta = ', '.join(x for x in (item['year'].strip(), item['relevance'].strip()) if x)
                    text = item['title'] + (f' ({meta})' if meta else '')
                else:
                    text = item['summary']
                row = merged.setdefault(url, {'texts': [], 'sides': []})
                row['texts'].append(f'{name}: {_flat(text)}')
                row['sides'].append(name)
        listed.update(merged)
        lines += ['', f'### {label}']
        lines += table(merged) if merged else ['- (없음)']
    extra = {}
    for name, items in evidence.items():
        for item in items:
            url = item['ref'].strip()
            if url not in listed:
                row = extra.setdefault(url, {'texts': [], 'sides': []})
                row['texts'].append(f'{name} {item.get("id", "")}: {_flat(item["observation"])}')
                row['sides'].append(name)
    if extra:
        lines += ['', '### 근거로만 인용된 웹 페이지'] + table(extra)
    lines.append('')
    lines.append('- 링크 확인은 메인이 공개 뒤 실제로 열어 제목·내용 일치를 본 결과다. 내용의 기술적 정확성 검증은 아니다.'
                 if status else '- 링크 확인 기록 없음(`research check` 미실행). 위 링크는 존재 여부를 확인하지 않았다.')
    return lines


USAGE_KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens')


def call_summaries(session):
    """task_metrics に 1 回ずつ取り込む呼び出し別 summary（実在するものだけ）。"""
    paths = []
    for call in session['calls']:
        path = call_file(session['sid'], call, 'summary.json')
        if path is not None:
            paths.append(str(path))
    return paths


def collect_calls(session):
    """呼び出し別 summary を集計する。欠けた summary は missing として数え、合計を 0 と区別する（D3）。"""
    sid = session['sid']
    usage, known, missing, violations, calls = {k: 0 for k in USAGE_KEYS}, 0, 0, 0, []
    for call in session['calls']:
        entry = {k: call.get(k) for k in ('index', 'phase', 'execution', 'status', 'model_status', 'timeout')}
        entry['elapsed_seconds'] = round(call['ended_at'] - call['started_at'], 2) if call.get('ended_at') else None
        path, data = call_file(sid, call, 'summary.json'), None
        if path is not None:
            try:
                data = decode_json(read_source(path, MAX_DOC, allow_session_files=True)[0])
            except (Refused, SchemaError, OSError):
                data = None
        if not isinstance(data, dict):
            missing += 1
            entry['summary'] = 'missing'
            calls.append(entry)
            continue
        entry['summary'] = 'ok'
        entry['links'] = data.get('links')
        violations += len(data.get('violations') or [])
        call_usage = data.get('usage')
        if isinstance(call_usage, dict) and any(type(call_usage.get(k)) is int for k in USAGE_KEYS):
            known += 1
            for key in USAGE_KEYS:
                value = call_usage.get(key)
                usage[key] += value if type(value) is int else 0
        calls.append(entry)
    total = len(session['calls'])
    status = 'complete' if known == total else 'unknown' if known == 0 else 'partial'
    return {'calls': calls, 'usage_total': None if status == 'unknown' else usage, 'usage_status': status,
            'summaries_missing': missing, 'violations': violations if missing == 0 else None,
            'violations_status': 'complete' if missing == 0 else 'unknown', 'violations_observed': violations}


def cmd_finalize(args):
    sid = sid_check(args.session)
    with locked_session(sid, allow_after_reap=True) as session:
        if session.get('finalized_at'):
            raise Refused('session is already finalized')
        if session['state'] not in ('SYNTHESIZED', 'STOPPED'):
            raise Refused(f'out-of-order transition: state is {session["state"]}, expected SYNTHESIZED/STOPPED')
        ledger = _artifact_or_none(sid, 'ledger.json')
        synthesis = _artifact_or_none(sid, 'synthesis.json')
        if session['state'] == 'SYNTHESIZED':
            # D1: 最後の validate 以降に ledger・synthesis が変わっていれば報告しない。
            info = session.get('conditional') or {}
            try:
                current = (sha256(read_artifact(sid, 'synthesis.json')), sha256(read_artifact(sid, 'ledger.json')))
            except (Refused, OSError):
                current = None
            if current != (info.get('synthesis_sha256'), info.get('ledger_sha256')):
                raise Refused('synthesis/ledger changed since the last `synthesis validate`; validate again')
        checked = checked_issues(session, ledger, synthesis)
        finished = now()
        rendered = []
        for document, target, render in ((ledger, 'debate.md', render_debate),
                                         (synthesis, 'synthesis.md', lambda d: render_synthesis(d, checked))):
            if document is None:
                continue
            try:
                text = render(document)
            except (KeyError, TypeError):
                continue
            write_frozen(art(sid) / target, text.encode('utf-8'))
            rendered.append(target)
        collected = collect_calls(session)
        metrics = synthesis.get('metrics') if isinstance(synthesis, dict) else None
        # task_metrics に取り込まれないよう codex_calls/mode は持たない（呼び出し別 summary を使う）。
        summary = {'kind': 'co-design-session', 'session': sid, 'question_id': session['question_id'],
                   'plan': session['plan'], 'final_state': 'STOPPED' if session['state'] == 'STOPPED' else 'FINALIZED',
                   'stop': session['stop'], 'codex_calls_total': len(session['calls']), 'max_calls': session['max_calls'],
                   'codex_budget_seconds': codex_budget(session), 'codex_seconds': round(codex_used(session), 1),
                   'wall_seconds': round(finished - session['created_at'], 1),
                   'over_budget': codex_used(session) >= codex_budget(session), 'codex_version': session['codex_version'],
                   **collected, 'compact': bool(session.get('compact')), 'request_format': session.get('request_format'),
                   'conditional': session.get('conditional'), 'conditional_used': session.get('conditional_used'),
                   'codex_checked_verified': sorted(checked), 'd4_metrics': metrics, 'rendered': rendered}
        write_frozen(art(sid) / 'report.md', render_report(session, summary, checked).encode('utf-8'))
        rendered.append('report.md')
        write_frozen(art(sid) / 'session_summary.json', dump(summary))
        session['over_budget'] = summary['over_budget']
        session['finalized_at'] = finished
        if session['state'] != 'STOPPED':
            session['state'] = 'FINALIZED'
        save_session(session)
        return {'status': 'ok', 'state': session['state'], 'session_summary': str(art(sid) / 'session_summary.json'),
                'report': str(art(sid) / 'report.md'),
                'rendered': [str(art(sid) / r) for r in rendered], 'over_budget': summary['over_budget'],
                'summaries_missing': collected['summaries_missing'], 'usage_status': collected['usage_status'],
                'call_summaries': call_summaries(session)}, 0


# ---------------------------------------------------------------- 初稿（L1）

PROTECTED_NAMES = {'credentials', 'auth.json', 'settings.json', 'settings.local.json', '.claude.json', 'config.toml',
                   'id_rsa', 'id_ed25519', 'assume_role.sh'}


def draft_target(target):
    """初稿出力先を副作用なしで検査する。"""
    target = Path(target).absolute()
    parent = target.parent
    if target.suffix != '.json' or target.name in PROTECTED_NAMES or target.name.startswith('.env'):
        raise Refused('draft output must be a new .json file outside settings/credential names')
    if not parent.is_dir() or any(p.is_symlink() for p in [target, parent, *parent.parents] if str(p) not in {'/tmp', '/var'}):
        raise Refused('draft output directory must exist and must not be reached through a symlink')
    resolved = parent.resolve()
    protected = [Path.home() / '.aws', Path.home() / '.codex', Path.home() / '.claude', *HARNESS_PROTECTED]
    if any(_within(resolved, p) for p in protected) or any(p.name in {'.aws', '.codex', '.claude', 'hooks'} for p in [resolved, *resolved.parents]):
        raise Refused('protected draft output path')
    if target.exists() or target.is_symlink():
        raise Refused('draft output already exists; drafts never overwrite files')
    return target


def submission_draft_target(value):
    if not Path(value).is_absolute():
        raise Refused('submission output must be an absolute draft path inside the current project')
    target = draft_target(value)
    resolved = target.parent.resolve() / target.name
    if (not _within(resolved, PROJECT) or _within(resolved, ARTIFACT_ROOT) or
            (_within(resolved, USER_TMP) and any(p.name.startswith('co-design-') for p in [resolved, *resolved.parents]))):
        raise Refused('submission output must be a separate draft inside the current project, outside session storage')
    if not os.access(target.parent, os.W_OK | os.X_OK):
        raise Refused('submission output parent must be writable')
    return target


def write_draft(target, document):
    """初稿を排他的に作る。既存ファイル・保護領域・シンボリックリンク経由のパスには書かない。"""
    target = draft_target(target)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Refused('draft output already exists; drafts never overwrite files') from None
    try:
        os.write(fd, dump(document))
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


def blank_issue(ident, origin):
    return {'id': ident, 'origin': origin, 'type': 'design-tradeoff', 'importance': 'medium', 'title': '',
            'requirement_ref': '', 'claude': {'position': '', 'stance': 'pending', 'evidence_ids': []},
            'codex': {'position': '', 'stance': 'pending', 'evidence_ids': []}, 'turns': [],
            'decision_method': 'pending', 'verification': {'method': '', 'result': 'unverified', 'evidence_ids': []},
            'status': 'open-unverified'}


def blank_turn(phase, speaker, kind):
    return {'phase': phase, 'speaker': speaker, 'kind': kind, 'text': '', 'evidence_ids': [], 'would_change_if': ''}


def ledger_ids_from_refs(session, refs):
    """Codex の evidence_refs のうち ledger に書ける ID（登録 E・Codex X・入力行）だけを順に返す。前置 ID は ID を取る。"""
    known = known_evidence(session)
    counts = {name: len(input_text(session['sid'], name).splitlines()) for name in INPUT_FILES}
    ids = []
    for ref in refs:
        ref = ref.strip()
        match = PREFIXED_REF.fullmatch(ref)
        candidate = ref if ref in known or link_ref(ref, [], counts)[0] == 'input' else \
            match.group(1) if match and match.group(1) in known else None
        if candidate and candidate not in ids:
            ids.append(candidate)
    return ids


TURN_PHASE = {'debate': 'P3', 'critique': 'P3', 'rebuttal': 'P3', 'round2': 'P4'}


def scaffold_final_ledger(session):
    """討論結果の Codex 批評・応答・stance を原文のまま入れた最終 ledger の初稿。Claude の stance は pending、回答は空欄。"""
    sid = session['sid']
    base = _artifact_or_none(sid, 'ledger.pre-debate.json') or _artifact_or_none(sid, 'claude/critique.json') or {'issues': []}
    issues = []
    for issue in base['issues']:
        issue = json.loads(json.dumps(issue))
        issue['claude']['stance'] = 'pending'
        issues.append(issue)
    by_id = {i['id']: i for i in issues}
    unmatched = []
    for phase in ('debate', 'critique', 'rebuttal', 'round2'):
        result = codex_result(session, phase) if phase_call(session, phase) else None
        if not result:
            continue
        for item in result['responses_to_claude_critique']:
            issue = by_id.get(item['issue_id'])
            if issue is None:
                unmatched.append(item['issue_id'])
                continue
            ids = ledger_ids_from_refs(session, item['evidence_refs'])
            issue['codex'] = {'position': item['argument'], 'stance': item['stance'], 'evidence_ids': ids}
            issue['turns'].append(dict(blank_turn(TURN_PHASE[phase], 'codex', 'response'), text=item['argument'],
                                       evidence_ids=ids, would_change_if=item['would_change_if']))
        for item in result['critique_of_claude']:
            ids = ledger_ids_from_refs(session, item['evidence_refs'])
            issue = by_id.get(item['issue_id'])
            if issue is None:
                # Codex 批評は title=claim、Codex の立場=alternative として原文のまま残す。
                issue = dict(blank_issue(item['issue_id'], 'codex'), type=item['type'], importance=item['importance'],
                             title=item['claim'], codex={'position': item['alternative'], 'stance': 'proposed', 'evidence_ids': ids})
                issues.append(issue)
                by_id[issue['id']] = issue
            issue['turns'].append(dict(blank_turn(TURN_PHASE[phase], 'codex', 'critique'), text=item['claim'],
                                       evidence_ids=ids, would_change_if=item['would_change_if']))
            issue['turns'].append(blank_turn(TURN_PHASE[phase], 'claude', 'answer'))
    states = dict(base.get('states') or {'execution': 'ok', 'evidence_sufficiency': 'partial', 'agreement': 'pending'})
    return {'issues': issues, 'states': states}, sorted(set(unmatched))


def scaffold_storage_next(session, kind, target, stage=None):
    """既定のセッション内初稿を --file に渡す実行不能な案内は返さない。"""
    resolved = target.resolve()
    supported = (_within(resolved, PROJECT) and not _within(resolved, ARTIFACT_ROOT) and
                 not (_within(resolved, USER_TMP) and any(p.name.startswith('co-design-')
                                                         for p in [resolved, *resolved.parents])))
    stage_args = ['--stage', stage] if stage else []
    prefix = ['python3', str(HERE / 'co_design.py'), kind]
    validate = prefix + ['validate', '--session', session['sid'], *stage_args]
    if supported:
        return 'Read the returned draft before editing; fill it without omitting design/evidence/conditions, then submit: ' + shlex.join(
            validate + ['--file', str(target)]) + '. The runner returns the canonical path/hash; do not reconstruct it.'
    example = PROJECT / '.local/cross-check-drafts/<unique>' / (f'ledger.{stage}.json' if stage else 'synthesis.json')
    scaffold = prefix + ['scaffold', '--session', session['sid'], *stage_args, '--out', str(example)]
    return ('This legacy draft cannot be submitted with --file (session artifact drafts are protected). '
            'For runner-managed storage, create a unique draft directory inside the current project and start with: '
            + shlex.join(scaffold) + '; fill its returned draft and submit: '
            + shlex.join(validate + ['--file', str(example)])
            + '. Replace <unique>; use the returned canonical path/hash, never a reconstructed artifact path.')


def cmd_ledger_scaffold(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        if args.stage == 'pre-debate':
            require_state(session, 'REVEALED', 'CODEX_CRITIQUED')
            critique = blank_turn('P3', 'claude', 'critique')
            document = {'issues': [dict(blank_issue('I1', 'claude'), turns=[critique])],
                        'states': {'execution': 'in-progress', 'evidence_sufficiency': 'partial', 'agreement': 'pending'}}
            unmatched = []
        else:
            require_state(session, 'DEBATED', 'SYNTHESIZED')
            document, unmatched = scaffold_final_ledger(session)
        if getattr(args, 'partial', False):
            if not args.out:
                raise Refused('partial scaffolds require --out to a separate absolute draft path inside the current project')
            output = args.out
            return dict(status='ok', **PARTIAL.create(SimpleNamespace(**globals()), session, 'ledger', args.stage,
                                                     document, output)), 0
        target = write_draft(args.out or art(sid) / f'ledger.{args.stage}.draft.json', document)
        pending = [i['id'] for i in document['issues'] if i['claude']['stance'] == 'pending']
        return {'status': 'ok', 'stage': args.stage, 'draft': str(target), 'issues': len(document['issues']),
                'claude_pending': pending, 'unmatched_codex_responses': unmatched,
                'hints': scaffold_hints(session, 'ledger', document),
                'next': scaffold_storage_next(session, 'ledger', target, args.stage)}, 0


def cmd_synthesis_scaffold(args):
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        require_state(session, 'DEBATED', 'SYNTHESIZED')
        ledger = read_json_artifact(sid, 'ledger.json')
        if not isinstance(ledger, dict) or not isinstance(ledger.get('issues'), list):
            raise Refused('ledger.json has no issues list')
        # 空欄・負の metrics は validate が拒否する（埋めるまで通らない）。
        document = {'decisions': [{'issue_id': i['id'], 'decision': '', 'adopted_from': '', 'reason': '',
                                   'rejected_alternatives': [], 'conditions': [], 'residual_risk': '', 'codex_checked': False}
                                  for i in ledger['issues'] if isinstance(i, dict) and 'id' in i],
                    'required': [], 'optional': [], 'user_decisions': [], 'unresolved': [], 'useful_from_codex': [],
                    'useful_none_reason': '',
                    'metrics': {'one_side_confirmed_findings': {'claude': -1, 'codex': -1}, 'missing_required': -1,
                                'unbacked_claims': -1}}
        if getattr(args, 'partial', False):
            if not args.out:
                raise Refused('partial scaffolds require --out to a separate absolute draft path inside the current project')
            output = args.out
            return dict(status='ok', **PARTIAL.create(SimpleNamespace(**globals()), session, 'synthesis', 'final',
                                                     document, output)), 0
        target = write_draft(args.out or art(sid) / 'synthesis.draft.json', document)
        return {'status': 'ok', 'draft': str(target), 'decisions': len(document['decisions']),
                'hints': scaffold_hints(session, 'synthesis', document),
                'next': scaffold_storage_next(session, 'synthesis', target)}, 0


def cmd_ledger(args):
    return cmd_ledger_scaffold(args) if args.action == 'scaffold' else cmd_ledger_validate(args)


def cmd_synthesis(args):
    return cmd_synthesis_scaffold(args) if args.action == 'scaffold' else cmd_synthesis_validate(args)


# ---------------------------------------------------------------- リクエスト計測（L3）

def estimate_tokens(text):
    """オフラインの概算（ASCII 4 文字 ≈ 1 トークン、非 ASCII 1 文字 ≈ 1 トークン）。実トークナイザではない。"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return -(-ascii_chars // 4) + (len(text) - ascii_chars)


def request_blocks(text):
    return {m.group(1): m.group(2) for m in re.finditer(r'^BEGIN (.+)\n(.*?)\nEND \1\n', text, re.M | re.S)}


def lossless_check(session, phase, baseline, compact, allow_selective=True):
    """圧縮本に全争点 ID・X/E 根拠 ID・両提案の内容・送った争点の stance が残っているかを確かめる。"""
    sid = session['sid']
    ledger = _artifact_or_none(sid, 'ledger.json') if phase in CONDITIONAL else _artifact_or_none(sid, 'ledger.pre-debate.json')
    issue_ids = [i['id'] for i in (ledger or {}).get('issues', [])]
    evidence_ids = sorted(known_evidence(session) | {e['id'] for e in (_artifact_or_none(sid, 'claude/proposal.json') or {}).get('evidence', [])})

    def present(text, ident):
        # 指示文（両方に同じ文面で入る）ではなくデータ部（最初の BEGIN 以降）だけを比べる。
        data = text[text.find('\nBEGIN '):]
        return re.search(r'(?<![A-Za-z0-9])' + re.escape(ident) + r'(?![0-9])', data) is not None
    report = {'issue_ids_missing': [i for i in issue_ids if present(baseline, i) and not present(compact, i)],
              'evidence_ids_missing': [i for i in evidence_ids if present(baseline, i) and not present(compact, i)],
              'proposals_equal': None, 'stance_mismatch': []}
    blocks = request_blocks(compact)
    if phase != 'proposal':
        try:
            report['proposals_equal'] = (
                decode_json(blocks['CLAUDE PROPOSAL (frozen)']) == read_json_artifact(sid, 'claude/proposal.json') and
                decode_json(blocks['YOUR PROPOSAL (frozen)']) == read_json_artifact(sid, 'codex/proposal.json', MAX_RESULT))
        except (KeyError, SchemaError, Refused):
            report['proposals_equal'] = False
    sent = blocks.get('LEDGER') or blocks.get('LEDGER EXCERPT')
    if sent and ledger:
        by_id = {i['id']: i for i in ledger['issues']}
        for issue in decode_json(sent):
            source = by_id.get(issue.get('id'))
            if not source or issue['claude']['stance'] != source['claude']['stance'] or \
                    issue['codex']['stance'] != source['codex']['stance']:
                report['stance_mismatch'].append(issue.get('id'))
    original_blocks = request_blocks(baseline)
    report['content_mismatch'] = []
    if not allow_selective and 'EXCERPT FILTER' in blocks:
        report['content_mismatch'].append('unexpected selective filtering')
    earlier = decode_json(blocks['YOUR EARLIER DEBATE OUTPUTS']) if 'YOUR EARLIER DEBATE OUTPUTS' in blocks else {}
    references = {f'@same-as {location}': text for text, location in _same_as_index(earlier).items()}

    def restored(value):
        if isinstance(value, str):
            return references.get(value, value)
        if isinstance(value, list):
            return [restored(x) for x in value]
        if isinstance(value, dict):
            return {k: restored(v) for k, v in value.items()}
        return value

    for title in ('CLAUDE PROPOSAL (frozen)', 'YOUR PROPOSAL (frozen)', 'LEDGER EXCERPT', 'LEDGER',
                  'CLAUDE CRITIQUE (frozen)', 'YOUR EARLIER CRITIQUE', 'YOUR EARLIER DEBATE OUTPUTS'):
        if title not in original_blocks:
            continue
        try:
            before, after = decode_json(original_blocks[title]), restored(decode_json(blocks[title]))
            if title == 'LEDGER' and 'EXCERPT FILTER' in blocks:
                selected = {i['id'] for i in after}
                before = [i for i in before if i['id'] in selected]
            elif title == 'YOUR EARLIER DEBATE OUTPUTS' and 'EXCERPT FILTER' in blocks:
                flt = decode_json(blocks['EXCERPT FILTER'])
                selected = set(flt['listed']) | set(flt['dependencies'])
                before = {p: dict(r, **{g: [i for i in r[g] if i['issue_id'] in selected]
                                      for g in ('critique_of_claude', 'responses_to_claude_critique')}) if r else r
                          for p, r in before.items()}
            if before != after:
                report['content_mismatch'].append(title)
        except (KeyError, SchemaError, TypeError):
            report['content_mismatch'].append(title)
    for title in ('CLAUDE EVIDENCE REGISTRY', 'REVEAL DIFF'):
        if title not in original_blocks:
            continue
        try:
            before, after = decode_json(original_blocks[title]), decode_json(blocks[title])
            if title == 'CLAUDE EVIDENCE REGISTRY':
                before = [dict(e, path=normalize_path(e['path'])) for e in before]
                after = [dict(e, path=normalize_path(e['path'])) for e in after]
            else:
                refs = {e['id']: e['ref'] for e in read_json_artifact(sid, 'codex/proposal.json')['evidence']}
                after['unbacked_codex'] = [{'id': e, 'ref': refs[e]} if isinstance(e, str) else e
                                          for e in after.get('unbacked_codex', [])]
            if before != after:
                report['content_mismatch'].append(title)
        except (KeyError, SchemaError, TypeError):
            report['content_mismatch'].append(title)
    for title in ('CLAUDE EVIDENCE CAPTURES (frozen text of each E<n>)',
                  'VERIFIED REUSE EVIDENCE (after reveal; current hash checked)'):
        if title in original_blocks:
            original = original_blocks[title].split('(capture omitted for size;', 1)[0].rstrip()
            if original and original not in blocks.get(title, ''):
                report['content_mismatch'].append(title)
    for title in ('BRIEF (cite as brief:L<n>)', 'SCOPE (cite as scope:L<n>)',
                  'EVIDENCE INDEX (cite as index:L<n>)', 'NEW EVIDENCE SINCE DEBATE'):
        if original_blocks.get(title) != blocks.get(title):
            report['content_mismatch'].append(title)
    transformed = {'CLAUDE PROPOSAL (frozen)', 'YOUR PROPOSAL (frozen)', 'LEDGER EXCERPT', 'LEDGER',
                   'CLAUDE CRITIQUE (frozen)', 'YOUR EARLIER CRITIQUE', 'YOUR EARLIER DEBATE OUTPUTS',
                   'CLAUDE EVIDENCE REGISTRY', 'REVEAL DIFF',
                   'CLAUDE EVIDENCE CAPTURES (frozen text of each E<n>)',
                   'VERIFIED REUSE EVIDENCE (after reveal; current hash checked)'}
    if allow_selective and 'EXCERPT FILTER' in blocks:
        transformed.add('SYNTHESIS (cite as synthesis:L<n>)')
    for title, original in original_blocks.items():
        if title not in transformed and original != blocks.get(title):
            report['content_mismatch'].append(title)
    if not allow_selective:
        extra = set(blocks) - set(original_blocks) - {'VERIFIED REUSE EVIDENCE (after reveal; current hash checked)',
                                                    'REUSE NOT ATTACHED'}
        report['content_mismatch'].extend(sorted(extra))
    report['ok'] = not (report['content_mismatch'] or report['issue_ids_missing'] or report['evidence_ids_missing'] or report['stance_mismatch']
                        or report['proposals_equal'] is False)
    return report


def cmd_measure_request(args):
    """既存セッションの成果物から圧縮前後のリクエストを組み立てて大きさを比べる（読み取りのみ、モデル呼び出しなし）。"""
    sid = sid_check(args.session)
    # ロック・古い呼び出しの回収・保存をしない（セッションに書き込まない）。
    session = load_session(sid)
    call = phase_call(session, args.phase)
    if call is None:
        raise Refused(f'phase {args.phase} was not called in this session')
    # 呼び出し時点の根拠登録簿で組み立てる（後から登録した根拠を含めない）。
    session = dict(session, evidence=[e for e in session['evidence'] if e['added_at'] <= call['started_at']])
    baseline, compact = build_request(session, args.phase, compact=False), build_request(session, args.phase, compact=True)
    sizes = {name: {'bytes': len(text.encode('utf-8')), 'est_tokens': estimate_tokens(text)}
             for name, text in (('baseline', baseline), ('compact', compact))}
    default_request = build_request(session, args.phase)
    sizes['default'] = {'bytes': len(default_request.encode('utf-8')), 'est_tokens': estimate_tokens(default_request)}
    actual = call_logs(sid, call['index'], args.phase) / 'request.txt'
    actual_bytes = actual.stat().st_size if actual.is_file() and not actual.is_symlink() else None
    current = None
    if args.phase in CONDITIONAL:
        info = session.get('conditional') or {}
        try:
            current = (sha256(read_artifact(sid, 'synthesis.json')) == info.get('synthesis_sha256') and
                       sha256(read_artifact(sid, 'ledger.json')) == info.get('ledger_sha256'))
        except (Refused, OSError):
            current = False
    saved = sizes['baseline']['bytes'] - sizes['compact']['bytes']
    return {'status': 'ok', 'session': sid, 'phase': args.phase, 'session_compact': bool(session.get('compact')),
            **sizes, 'saved_bytes': saved,
            'saved_ratio': round(saved / sizes['baseline']['bytes'], 3) if sizes['baseline']['bytes'] else None,
            'actual_request_bytes': actual_bytes,
            'baseline_matches_actual': None if actual_bytes is None else actual_bytes == sizes['baseline']['bytes'],
            'default_matches_actual': None if actual_bytes is None else actual_bytes == sizes['default']['bytes'],
            'default_saved_bytes': sizes['baseline']['bytes'] - sizes['default']['bytes'],
            'default_lossless': (fidelity_serialization_check(session, baseline, default_request)
                                 if args.phase == 'fidelity' and session.get('fidelity_format') == FIDELITY_FORMAT and not session.get('compact')
                                 else lossless_check(session, args.phase, baseline, default_request)),
            'conditional_inputs_unchanged': current,
            'lossless': lossless_check(session, args.phase, baseline, compact),
            'note': 'request size only; Codex input tokens also include its system instructions and accumulated tool '
                    'turns. est_tokens is an offline heuristic, not a tokenizer.'}, 0


def cmd_status(args):
    session = load_session(sid_check(args.session))
    return {'status': 'ok', 'state': session['state'], 'plan': session['plan'], 'stop': session['stop'],
            'calls_used': len(session['calls']), 'max_calls': session['max_calls'],
            'codex_seconds_used': round(codex_used(session), 1), 'codex_budget_seconds': codex_budget(session),
            'codex_remaining_seconds': round(codex_remaining(session), 1),
            'calls': [{k: c.get(k) for k in ('index', 'phase', 'execution', 'status')} for c in session['calls']]}, 0


def neutral_architect_prompt(output):
    """検証済み中立パスだけを固定文へ渡す。自由文・提案・実装例は受け取らない。"""
    metadata = {'inputs': output['inputs'], 'data': output['data'], 'question_date': output['question_date'],
                'output_contract': str(HERE.parent / 'references/architect-handoff.md'),
                'output_example': str(HERE.parent / 'references/architect-handoff.example.json'),
                'proposal_schema': str(SCHEMAS / 'proposal.schema.json'),
                'evidence_inspect': ['python3', str(HERE / 'co_design.py'), 'evidence', 'inspect',
                                     '--session', output['session']]}
    return (
        '당신은 /cross-check debate P1-b의 독립 architect입니다. 다음은 중립 입력과 출력 계약의 경로·hash 메타데이터뿐입니다. '
        '메인의 사전 조사·제안이나 Codex 출력은 전달하지 않습니다.\n'
        + json.dumps(metadata, ensure_ascii=False, indent=1) + '\n'
        # Codex は監視で Claude 側の作業経路を読めない。こちらも Codex 側の出力・他セッションを読まないよう明示する（強制ではない）。
        f'Codex의 작업·출력과 다른 세션 산출물을 보지 않도록 {ARTIFACT_ROOT}/ 아래에서는 위 inputs·data 경로만 읽고, '
        f'그 밖의 파일·디렉터리(이 세션의 work/·artifacts/codex/ 등 포함)는 읽거나 나열하지 마세요. '
        f'{USER_TMP}/co-design-* 도 읽거나 나열하지 마세요.\n'
        '입력 3종과 선택 data를 직접 읽고 scope 허용 범위에서 현행 코드·호출부·계약 양쪽·근거 원문을 충실히 조사하세요. '
        '요구사항 매핑·설계 전문·대안과 조건·위험·필수 검증·미확인을 빠뜨리지 않고 독립적으로 판단하세요. '
        '파일별 관찰 범위·현재 전체 파일 SHA256·작업트리/확인 날짜/Git 상태를 기록하세요.\n'
        # 外部調査は scope が web を許すときだけ。検索語に社内情報を入れない規則は prompt_common と同じ。
        'scope의 allowed_tools에 web이 있으면 question_date 기준으로 이 주제의 최신 동향, 일반적인 구현 방식·기술, '
        '관련 논문을 WebSearch/WebFetch로 조사해 설계 판단에 반영하고, 실제로 연 페이지의 URL만 proposal.research와 '
        'kind web 근거(ref는 URL 하나)에 기록하세요. 검색어에는 일반 기술 용어만 쓰고 로컬 코드·경로·사내 식별자·비밀을 넣지 마세요. '
        'web이 없거나 조사할 의미가 없으면 web_used false와 빈 목록, not_applicable_reason을 쓰세요. '
        'research.question_date에는 위 question_date를 그대로 쓰세요.\n'
        'file 근거의 줄 수·lines·ref·file_sha256은 evidence_inspect 명령에 --file <절대경로>와 '
        '--lines <a-b> 또는 --whole-file을 주어 얻은 값을 그대로 쓰세요. 이 검사는 읽기 전용이며 등록하지 않습니다. '
        '범위를 추측·자동 보정하지 말고, 빈 파일의 null 범위를 file 근거로 제출하지 마세요.\n'
        '읽기 전용입니다. 구현·테스트 실행·파일 작성·근거 등록·freeze·세션 변경·다른 자식 위임을 하지 마세요. '
        '출력 계약·순수 JSON 예제·schema를 읽고 handoff_version:2의 JSON object 하나만 한국어 메시지로 반환하세요. '
        '최종 응답 전체가 JSON이어야 합니다. 앞뒤 설명·코드 펜스는 금지하며 필요한 설명은 JSON 내부의 기존 필드에 모두 보존하세요. '
        '관찰문은 proposal.evidence[].observation에 한 번만 작성하고 manifest에는 observation을 넣지 마세요. '
        '설명·조건·대안·위험을 축약하지 마세요. 새로운 사용자 판단이 필요하면 결과에 질문으로 남기세요.\n'
        '전송 직전 최종 메시지 자체를 점검하세요: 첫 비공백 문자는 {, 마지막은 }이며 JSON 바깥 조사 완료 안내·요약·목록·코드펜스가 없어야 합니다. '
        '일반 Markdown 계획서 형식이나 문서를 감싸는 펜스를 출력하지 마세요. 필요한 설명은 기존 JSON 필드 안에 모두 보존하고 계약·스키마·문법도 점검하세요. '
        'JSON 문자열 내부의 코드펜스·개행은 제거하지 마세요. 이 점검을 위해 별도 AI 호출·테스트 실행·내용 축약을 하지 마세요.'
    )


def _digest_ok(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _completed_proposal_call(session):
    """完了した proposal 呼び出し記録を検査して返す（非公開成果物の検査は別）。"""
    calls = session.get('calls')
    if not isinstance(calls, list) or not calls or any(not isinstance(c, dict) for c in calls):
        raise Refused('proposal call record is incomplete')
    if any(c.get('execution') == 'running' for c in calls):
        raise Refused('a Codex call is still recorded as running; investigation is not ready')
    call = calls[-1]
    started, ended = call.get('started_at'), call.get('ended_at')
    valid_time = lambda value: type(value) in (int, float) and 0 < value < float('inf')
    if (call.get('phase') != 'proposal' or type(call.get('index')) is not int or call['index'] != len(calls)
            or not valid_time(started) or not valid_time(ended) or ended < started
            or call.get('execution') != 'ok' or call.get('status') not in ('COMPLETE', 'INCOMPLETE')
            or call.get('model_status') not in ('COMPLETE', 'INCOMPLETE')):
        raise Refused('proposal call has not completed successfully or its record is incomplete')
    return call


def _neutral_inputs(sid, session):
    """凍結入力・添付原資料の記録と実体を検査し、中立パス・ハッシュだけを返す。"""
    inputs, data = session.get('inputs'), session.get('data', {})
    data_meta = session.get('data_meta', {})
    if (not isinstance(inputs, dict) or set(inputs) != set(INPUT_FILES)
            or not all(_digest_ok(value) for value in inputs.values()) or not isinstance(data, dict)
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', name) or not _digest_ok(value)
                   for name, value in data.items()) or not isinstance(session.get('frozen'), dict)):
        raise Refused('frozen input record is incomplete')
    # ハッシュ記録だけが消え、原資料を次の調査へ渡し忘れる状態を拒否する。
    data_dir = art(sid) / 'inputs/data'
    if not isinstance(data_meta, dict) or set(data_meta) != set(data):
        raise Refused('attached data records are inconsistent')
    # 一覧取得前に入力本文の read_source で親経路の保護・symlink 検査も済ませる。
    if verify_frozen(session):
        raise Refused('frozen input integrity check failed')
    if data_dir.is_symlink() or (data_dir.exists() and not data_dir.is_dir()):
        raise Refused('attached data directory is invalid')
    actual_data = {entry.name for entry in data_dir.iterdir()} if data_dir.is_dir() else set()
    if actual_data != set(data):
        raise Refused('attached data inventory does not match its frozen record')
    return ({name: {'path': str(art(sid) / 'inputs' / INPUT_FILES[name]), 'sha256': digest}
             for name, digest in inputs.items()},
            {name: {'path': str(art(sid) / 'inputs/data' / name), 'sha256': digest}
             for name, digest in data.items()})


def _private_proposal_intact(sid, call):
    for name, limit in (('result', MAX_RESULT), ('links', MAX_DOC * 4)):
        expected = call.get(name + '_sha256')
        path = call_file(sid, call, name + '.json')
        if not _digest_ok(expected) or path is None:
            raise Refused('private proposal artifacts or their integrity records are missing')
        content, _, _ = read_source(path, limit, allow_session_files=True)
        if sha256(content) != expected:
            raise Refused('private proposal artifact integrity check failed')


@contextmanager
def investigation_session(sid):
    """開始検査と読み取りの間も同じ既存ロックを保持し、状態は変更しない。"""
    sid = sid_check(sid)
    # locked_session は stale call を停止・保存するので、ここでは使わない。
    with session_lock(sid, create=False):
        session = load_session(sid)
        if session.get('sid') != sid or session.get('state') != 'CODEX_PROPOSED' or session.get('stop') is not None:
            raise Refused('investigation requires a completed proposal in CODEX_PROPOSED')
        call = _completed_proposal_call(session)
        inputs, data = _neutral_inputs(sid, session)
        _private_proposal_intact(sid, call)
        # 非公開の結果・要約・問題・ログ・そのパスは出力に含めない。
        output = {'status': 'ok', 'ready': True, 'session': sid,
                  'proposal_call': {'index': call['index'], 'ended_at': call['ended_at'],
                                    'model_status': call['model_status']},
                  'inputs': inputs, 'data': data, 'question_date': question_date(session)}
        if call['model_status'] == 'INCOMPLETE':
            output['caveat'] = 'Codex model reported INCOMPLETE; investigation may proceed conditionally, not as verified completion.'
        yield session, output


# 独立 architect が Codex の初回提案と並行して調査できる状態。凍結入力の後、Claude 提案の凍結前まで。
INSPECT_STATES = ('INPUTS_FROZEN', 'CODEX_PROPOSED')


@contextmanager
def inspection_session(sid):
    """evidence inspect 用の読み取り専用検査。Codex 呼び出し中もロックを取らずに使える。

    session.json は一時ファイル+os.replace で原子的に保存されるので、ロックなしでも書きかけを読まない。
    """
    sid = sid_check(sid)
    directory = session_dir(sid)
    if directory.is_symlink() or not directory.is_dir():
        raise Refused('unknown session')
    session = load_session(sid)
    if session.get('sid') != sid or session.get('stop') is not None or session.get('state') not in INSPECT_STATES:
        raise Refused('inspection requires frozen inputs before the Claude proposal is frozen (INPUTS_FROZEN or CODEX_PROPOSED)')
    if session['state'] == 'CODEX_PROPOSED':
        _private_proposal_intact(sid, _completed_proposal_call(session))
    _neutral_inputs(sid, session)
    yield session


def cmd_architect_prompt(args):
    """入力凍結直後に、Codex 初回提案と並行して起動する独立 architect の固定委任文を返す。"""
    sid = sid_check(args.session)
    with locked_session(sid) as session:
        require_state(session, 'INPUTS_FROZEN')
        inputs, data = _neutral_inputs(sid, session)
        output = {'status': 'ok', 'session': sid, 'inputs': inputs, 'data': data,
                  'question_date': question_date(session)}
        prompt = neutral_architect_prompt(output)
        return dict(output, architect_prompt=prompt, architect_prompt_sha256=sha256(prompt.encode('utf-8'))), 0


def cmd_investigation_ready(args):
    """初回調査の開始条件だけを返す。回収・保存・モデル呼び出し・提案の公開はしない。"""
    with investigation_session(args.session) as (_, output):
        if getattr(args, 'architect_prompt', False):
            prompt = neutral_architect_prompt(output)
            output.update(architect_prompt=prompt, architect_prompt_sha256=sha256(prompt.encode('utf-8')))
        return output, 0


def cmd_prune(args):
    targets = prune_targets(apply=args.apply)
    return {'status': 'ok', 'applied': bool(args.apply), 'targets': targets,
            'note': 'only the temporary area is pruned; .local/co-design (artifacts and work) is never deleted automatically'}, 0


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init')
    p.add_argument('--question-id', required=True)
    p.add_argument('--frozen-critique', action='store_true', help='plan B (large architecture decisions)')
    p.add_argument('--compact', action='store_true', help='lossless request compaction for this session (L3, off by default)')
    p.add_argument('--no-auto-verify', action='store_true',
                   help='refuse on a Codex version change instead of running the automatic check (1 Codex call)')
    sub.add_parser('verify-codex', help='run the automatic Codex check now and record the version on PASS')
    p = sub.add_parser('freeze-inputs')
    for name in ('session', 'brief', 'scope', 'index'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--data', action='append', default=[], metavar='FILE',
                   help='reference data file copied read-only into the session inputs (repeatable, 8 MiB each, 16 MiB total)')
    p = sub.add_parser('call')
    p.add_argument('--session', required=True)
    p.add_argument('--phase', required=True, choices=sorted(PHASES))
    p = sub.add_parser('evidence')
    evidence = p.add_subparsers(dest='action', required=True)
    ev = evidence.add_parser('add')
    ev.add_argument('--session', required=True)
    ev.add_argument('--file', required=True)
    ev.add_argument('--lines', required=True)
    ev = evidence.add_parser('inspect', help='read-only: source line count, canonical ref, hash and numbered excerpt')
    ev.add_argument('--session', required=True)
    ev.add_argument('--file', required=True)
    bounds = ev.add_mutually_exclusive_group(required=True)
    bounds.add_argument('--lines', help='explicit inclusive a-b range (never clamped)')
    bounds.add_argument('--whole-file', action='store_true', help='derive the actual full range; empty file has no range')
    p = sub.add_parser('freeze')
    p.add_argument('--session', required=True)
    p.add_argument('--side', required=True, choices=['claude', 'claude-critique'])
    p.add_argument('--normalize-refs', action='store_true', help='normalize designated input references after source and birthtime checks; preserve original audit')
    p.add_argument('--file', required=True)
    p = sub.add_parser('proposal')
    actions = p.add_subparsers(dest='action', required=True)
    capture = actions.add_parser('capture', help='save the architect final message verbatim from the Agent output_file (no registration)')
    capture.add_argument('--session', required=True)
    capture_source = capture.add_mutually_exclusive_group(required=True)
    capture_source.add_argument('--agent-output', help='absolute output_file returned by the Agent tool')
    capture_source.add_argument('--agent-events', help='existing caller stream-json file inside the current project')
    capture.add_argument('--agent-id', help='exact architect task id returned by the Agent tool (events mode)')
    capture.add_argument('--caller-session-id', help='exact caller Claude session id (events mode)')
    capture.add_argument('--out', required=True, help='new separate message file inside the current project')
    prepare = actions.add_parser('prepare')
    prepare.add_argument('--session', required=True)
    prepare.add_argument('--file', required=True, help='structured architect message saved once by main inside the current project')
    prepare.add_argument('--out', required=True, help='new separate proposal draft inside the current project')
    p = sub.add_parser('reveal')
    p.add_argument('--session', required=True)
    p = sub.add_parser('research')
    actions = p.add_subparsers(dest='action', required=True)
    rc = actions.add_parser('check', help='after reveal: save main-verified link results for the research URLs of both proposals')
    rc.add_argument('--session', required=True)
    rc.add_argument('--file', required=True, help='absolute JSON draft inside the current project: {"checks": [{url, status, observed_title, note}]}')
    p = sub.add_parser('ledger')
    actions = p.add_subparsers(dest='action', required=True)
    lv = actions.add_parser('validate')
    lv.add_argument('--session', required=True)
    lv.add_argument('--normalize-refs', action='store_true', help='normalize whole input-reference fields after strict validation; preserve original and change log')
    lv.add_argument('--stage', required=True, choices=['pre-debate', 'final'])
    ledger_source = lv.add_mutually_exclusive_group()
    ledger_source.add_argument('--file', help='absolute full draft path inside the current project')
    ledger_source.add_argument('--partial-file', help='typed partial submission bound to a runner-created base')
    ls = actions.add_parser('scaffold', help='write a draft (never overwrites; validate rejects it until filled)')
    ls.add_argument('--session', required=True)
    ls.add_argument('--stage', required=True, choices=['pre-debate', 'final'])
    ls.add_argument('--out', help='draft path (default artifacts/ledger.<stage>.draft.json)')
    ls.add_argument('--partial', action='store_true', help='create a trusted full base and a typed partial draft')
    p = sub.add_parser('synthesis')
    actions = p.add_subparsers(dest='action', required=True)
    sv = actions.add_parser('validate')
    sv.add_argument('--session', required=True)
    synthesis_source = sv.add_mutually_exclusive_group()
    synthesis_source.add_argument('--file', help='absolute full draft path inside the current project')
    synthesis_source.add_argument('--partial-file', help='typed partial submission bound to a runner-created base')
    sv.add_argument('--derive-checked', action='store_true', help='derive codex_checked from current faithful fidelity coverage; save only after strict validation')
    sv.add_argument('--user-requested', action='store_true', help='user explicitly asked for round2')
    ss = actions.add_parser('scaffold', help='write a draft (never overwrites; validate rejects it until filled)')
    ss.add_argument('--session', required=True)
    ss.add_argument('--out', help='draft path (default artifacts/synthesis.draft.json)')
    ss.add_argument('--partial', action='store_true', help='create a trusted full base and a typed partial draft')
    p = sub.add_parser('finalize')
    p.add_argument('--session', required=True)
    p = sub.add_parser('status')
    p.add_argument('--session', required=True)
    p = sub.add_parser('investigation-ready', help='read-only: check proposal completion without revealing it or calling a model')
    p.add_argument('--session', required=True)
    p.add_argument('--architect-prompt', action='store_true', help='return a fixed neutral architect prompt only after the existing ready checks')
    p = sub.add_parser('architect-prompt', help='fixed neutral architect prompt right after inputs are frozen (run before the proposal call); '
                       'like other locked commands it stops a stale running call (STOPPED) instead of returning a prompt')
    p.add_argument('--session', required=True)
    p = sub.add_parser('prune')
    p.add_argument('--apply', action='store_true',
                   help='delete expired temporary logs and legacy temporary evidence (default: list only)')
    p = sub.add_parser('measure-request', help='read-only: request bytes before/after compaction (no model call)')
    p.add_argument('--session', required=True)
    p.add_argument('--phase', required=True, choices=sorted(PHASES))
    return root


def cmd_evidence(args):
    return cmd_evidence_inspect(args) if args.action == 'inspect' else cmd_evidence_add(args)


COMMANDS = {'init': cmd_init, 'freeze-inputs': cmd_freeze_inputs, 'call': cmd_call, 'evidence': cmd_evidence,
            'freeze': cmd_freeze, 'proposal': cmd_proposal, 'reveal': cmd_reveal, 'ledger': cmd_ledger, 'synthesis': cmd_synthesis,
            'finalize': cmd_finalize, 'status': cmd_status, 'prune': cmd_prune, 'measure-request': cmd_measure_request,
            'verify-codex': cmd_verify_codex, 'investigation-ready': cmd_investigation_ready,
            'architect-prompt': cmd_architect_prompt, 'research': cmd_research_check}


def run(argv=None):
    args = parser().parse_args(argv)
    if too_broad_root(PROJECT):
        # ホームや / を作業ルートにすると Codex の読み取り範囲が広すぎる。プロジェクトルートで実行し直す。
        return {'status': 'REFUSED', 'error': 'working root is too broad; run from a project directory'}, 2
    try:
        return COMMANDS[args.command](args)
    except Refused as error:
        return {'status': 'REFUSED', 'error': str(error)}, 2
    except SchemaError as error:
        return {'status': 'INVALID', 'error': str(error)}, 2
    except OSError as error:
        # パスや内容を含みうる OS エラーの詳細は出さない。
        return {'status': 'ERROR', 'error': type(error).__name__ + ': file operation failed'}, 2
    except Exception as error:
        # トレースバックの代わりに 1 行で返す（内容は出さない）。
        return {'status': 'ERROR', 'error': type(error).__name__ + ': unexpected runner error'}, 2


def main(argv=None):
    output, code = run(argv)
    print(json.dumps(output, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
