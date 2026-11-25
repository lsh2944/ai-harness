#!/usr/bin/env python3
"""ai-harness の hook・設定・指示・スキル・エージェントを配置する。--apply なしでは何も書き込まない。

使い方:
  python3 install/install.py --config config.local.json [--target claude|codex|both] [--with-aws] [--apply]
"""
import argparse
import datetime
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time
import tomllib

HOME = Path.home()
HARNESS = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
WINDOWS = os.name == 'nt'

# Codex config.toml の所有 hook ブロック（旧版との互換のため同じマーカーを使う）
BEGIN = '# BEGIN shared harness hooks'
END = '# END shared harness hooks'
# CLAUDE.md / AGENTS.md に挿入する指示ブロック
MD_BEGIN = '<!-- BEGIN ai-harness instructions (install.py が管理。手で編集しない) -->'
MD_END = '<!-- END ai-harness instructions -->'
OWNED_CODEX_HOOKS = ('/.codex/hooks/guard.py', '/.codex/hooks/usage_alert.py')
GUARD_WRAPPER = 'guard_hook.sh'
RUNTIME_CONFIG = 'harness_config.json'
# hook コマンドの起動失敗・異常終了は未判定のまま実行される（fail-open）。guard は sh 経由にし、失敗時も拒否を返す。
DENY_ON_FAILURE = json.dumps({'hookSpecificOutput': {
    'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
    'permissionDecisionReason': '가드 hook을 실행하지 못했습니다(인터프리터·guard.py 확인 필요). 작업을 실행하지 않았습니다.'}},
    ensure_ascii=False)

SKILLS = ('handoff', 'cross-check', 'efficient-investigation', 'task-metrics', 'prune-memory')
AWS_SKILLS = ('assume-dev',)
# 設定 sidecar を必要とする（Python からプロジェクトルート等を読む）スキル
SKILLS_WITH_RUNTIME = ('cross-check', 'prune-memory')
AGENTS = ('code-reviewer', 'architect', 'troubleshooter', 'script-feature')
INSTRUCTIONS = ('language.md', 'core.md', 'conduct.md')
SKIP_NAMES = {'tests', '__pycache__', '.DS_Store'}
RENDER_SUFFIXES = {'.md', '.yaml', '.yml'}
VAR = re.compile(r'\{\{([A-Z_]+)\}\}')
AWS_BLOCK = re.compile(r'<!-- BEGIN aws -->\n?(.*?)<!-- END aws -->\n?', re.S)
# github_owners が設定されたときだけ残す指示ブロック（ルート README の英語化規則）
GITHUB_BLOCK = re.compile(r'<!-- BEGIN github_readme -->\n?(.*?)<!-- END github_readme -->\n?', re.S)
GITHUB_OWNER_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$')
PROFILE_RE = re.compile(r'^[A-Za-z0-9_.+-]{1,128}$')
ENV_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
AWS_MCP_TOOLS = ', mcp__aws-api__call_aws, mcp__aws-api__suggest_aws_commands'
# 既存値より優先して上書きする保護用キー（断片に値があるときだけ）。それ以外のスカラーは値がないときだけ追加する。
FORCED_CLAUDE_KEYS = (('env', 'AWS_PROFILE'),)


# ---------------- 利用者設定 ----------------
def _path(value, name, required=True):
    if value in (None, '') and not required:
        return None
    if not isinstance(value, str) or any(c in value for c in '\n\r\0'):
        raise ValueError(f'config: {name} must be a path string')
    path = value.replace('~', str(HOME), 1) if value.startswith('~') else value
    if not os.path.isabs(path):
        raise ValueError(f'config: {name} must be an absolute path (or start with ~)')
    return os.path.normpath(path)


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\n\r\0{}'):
        raise ValueError(f'config: {name} must be a single-line string')
    return value.strip()


def default_memory_dir(project_root):
    # Claude Code はプロジェクトパスの英数字以外を - に置き換えたディレクトリにメモリを置く
    return str(HOME / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', project_root) / 'memory')


def template_path(path):
    """指示・スキル文中に埋め込むパス。Windows は / 区切りにする（PowerShell・Git Bash・Python のどれでも使え、
    Markdown 中の \\ のエスケープ問題を避ける）。POSIX はそのまま。"""
    return path.replace('\\', '/') if WINDOWS and path else path


def permission_rule_path(path):
    """Claude の権限規則（Edit(...)）の絶対パス表記。POSIX は //Users/x/...（従来の "/" + パス）、
    Windows は //c/Users/x/...（ドライブを先頭要素にした / 区切り）。"""
    if WINDOWS:
        drive, rest = os.path.splitdrive(path)
        return '//' + drive.rstrip(':').lower() + rest.replace('\\', '/')
    return '/' + path


def python_command(python):
    """スキル文中で .py を実行するコマンドの先頭。POSIX は従来どおり python3、
    Windows は PowerShell 道具から登録済みインタプリタを呼ぶ形（& "C:/.../python.exe"）。"""
    return f'& "{template_path(python)}"' if WINDOWS else 'python3'


def load_settings(path, with_aws):
    """config.local.json を検証し、テンプレート変数と実行時設定を返す。"""
    # 任意の場所の設定で guard の保護（aws.enabled・project_root・追加保護パス）を弱められないよう、
    # 設定はこのリポジトリ内の通常ファイルに限る（config.local.json はガードの保護対象）。
    candidate = Path(os.path.abspath(path))
    if candidate.is_symlink() or not candidate.is_file() \
            or not candidate.resolve().is_relative_to(HARNESS.resolve()):
        raise ValueError(f'config: must be a regular file inside {HARNESS} (got {path})')
    try:
        data = json.loads(candidate.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'config: cannot read {path}: {type(error).__name__}') from None
    if not isinstance(data, dict):
        raise ValueError('config: top level must be an object')
    project = _path(data.get('project_root'), 'project_root')
    python = _path(data.get('python'), 'python', False) or PYTHON
    variables = {
        'PROJECT_ROOT': template_path(project),
        'HARNESS_HOME': template_path(str(HARNESS)),
        'HARNESS_HOME_RULE': permission_rule_path(str(HARNESS)),
        'MEMORY_DIR': template_path(_path(data.get('memory_dir'), 'memory_dir', False) or default_memory_dir(project)),
        'PYTHON': template_path(python),
        'PY': python_command(python),
        'RESPONSE_LANGUAGE': _text(data.get('response_language'), 'response_language'),
        'COMMENT_LANGUAGE': _text(data.get('comment_language'), 'comment_language'),
        'AWS_MCP_TOOLS': '',
    }
    # 任意。未設定・空ならルート README 英語化の規則ブロックを入れない（既存 PC の設定でも導入が失敗しない）。
    owners = data.get('github_owners', [])
    if not isinstance(owners, list) or not all(isinstance(o, str) and GITHUB_OWNER_RE.fullmatch(o) for o in owners):
        raise ValueError('config: github_owners must be a list of GitHub account names')
    variables['GITHUB_OWNERS'] = ', '.join(f'`{o}`' for o in dict.fromkeys(owners))
    extra = data.get('extra_protected_paths', [])
    if not isinstance(extra, list):
        raise ValueError('config: extra_protected_paths must be a list')
    extra = [_path(p, 'extra_protected_paths') for p in extra]
    aws = {'enabled': False}
    if with_aws:
        section = data.get('aws')
        if not isinstance(section, dict):
            raise ValueError('config: --with-aws requires an "aws" object')
        profile = section.get('profile')
        if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
            raise ValueError('config: aws.profile is invalid')
        command = _path(section.get('assume_role_cmd'), 'aws.assume_role_cmd')
        envs = section.get('assume_role_envs', [])
        if not isinstance(envs, list) or not envs or not all(isinstance(e, str) and ENV_NAME_RE.fullmatch(e) for e in envs):
            raise ValueError('config: aws.assume_role_envs must be a non-empty list of names')
        aws = {'enabled': True, 'profile': profile, 'assume_role_cmd': command, 'assume_role_envs': envs}
        variables.update(AWS_PROFILE=profile, ASSUME_ROLE_CMD=template_path(command),
                         ASSUME_ROLE_CMD_RULE=permission_rule_path(command), AWS_ENVS=', '.join(envs),
                         AWS_MCP_TOOLS=AWS_MCP_TOOLS)
    runtime = {'version': 1, 'project_root': project, 'harness_home': str(HARNESS), 'aws': aws,
               'extra_protected_paths': extra}
    return {'variables': variables, 'runtime': runtime, 'with_aws': with_aws, 'python': python}


# ---------------- テンプレート ----------------
def render(text, variables, with_aws, label):
    """{{VAR}} を置換し、<!-- BEGIN aws --> ブロックは --with-aws のときだけ、
    <!-- BEGIN github_readme --> ブロックは GITHUB_OWNERS が空でないときだけ残す。未解決の変数はエラー。"""
    text = AWS_BLOCK.sub(lambda m: m.group(1) if with_aws else '', text)
    text = GITHUB_BLOCK.sub(lambda m: m.group(1) if variables.get('GITHUB_OWNERS') else '', text)
    missing = set()

    def substitute(match):
        if match.group(1) in variables:
            return variables[match.group(1)]
        missing.add(match.group(1))
        return match.group(0)
    text = VAR.sub(substitute, text)
    if missing:
        raise ValueError(f'{label}: unresolved template variables {sorted(missing)}')
    return text


def render_data(text, variables, with_aws, label):
    """JSON/TOML 断片用。値は JSON 文字列としてエスケープする。"""
    escaped = {k: json.dumps(v, ensure_ascii=False)[1:-1] for k, v in variables.items()}
    return render(text, escaped, with_aws, label)


def instructions_block(settings):
    names = INSTRUCTIONS + (('aws-readonly.md',) if settings['with_aws'] else ())
    parts = [render((HARNESS / 'instructions' / n).read_text(encoding='utf-8'), settings['variables'],
                    settings['with_aws'], f'instructions/{n}').strip() for n in names]
    return MD_BEGIN + '\n\n' + '\n\n'.join(parts) + '\n\n' + MD_END + '\n'


def insert_block(text, block, warnings, label):
    """マーカー間だけを置き換える。マーカーがなければ末尾に追加する。"""
    text = text or ''
    if text.count(MD_BEGIN) > 1 or text.count(MD_END) > 1 or (MD_BEGIN in text) != (MD_END in text):
        raise RuntimeError(f'{label}: broken ai-harness marker block; fix it manually')
    if MD_BEGIN in text:
        start, end = text.index(MD_BEGIN), text.index(MD_END) + len(MD_END)
        if start > end:
            raise RuntimeError(f'{label}: broken ai-harness marker block; fix it manually')
        outside = text[:start] + text[end:]
        result = text[:start] + block.rstrip('\n') + text[end:]
    else:
        outside = text
        result = (text.rstrip() + '\n\n' if text.strip() else '') + block
    # 同じ見出しが既にブロック外にあれば重複の恐れがあるため警告する（自動では消さない）。
    for heading in re.findall(r'^##+ .+$', block, re.M):
        if re.search(r'^' + re.escape(heading.split(' (')[0]), outside, re.M):
            warnings.append(f'{label}: heading "{heading}" also exists outside the managed block (possible duplicate)')
    return result


# ---------------- Claude ----------------
def merge(dst, src, label='settings'):
    """dict は再帰、list は未登録の要素だけ追加、スカラー・未登録キーは値がないときだけ追加する。
    利用者が設定済みの値（defaultMode 等）は変えない。型が食い違う場合は書き換えずに中止する。"""
    for key, value in src.items():
        if key not in dst:
            dst[key] = json.loads(json.dumps(value))
        elif isinstance(value, dict):
            if not isinstance(dst[key], dict):
                raise ValueError(f'{label}: "{key}" must be an object')
            merge(dst[key], value, f'{label}.{key}')
        elif isinstance(value, list):
            if not isinstance(dst[key], list):
                raise ValueError(f'{label}: "{key}" must be a list')
            dst[key].extend(v for v in value if v not in dst[key])
    return dst


CLAUDE_GUARD_MATCHER = 'Bash|Write|Edit|NotebookEdit|Read|Grep|mcp__.*'
# Windows の Claude はシェル道具が PowerShell になり得る。MultiEdit も guard の判定対象（check_edit）
CLAUDE_GUARD_MATCHER_WINDOWS = 'Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|Read|Grep|mcp__.*'


def hook(path, codex=False):
    # Codex は hook 定義ごとに trusted_hash を持ち、コマンドが変わると再承認まで黙って実行しない。
    # 承認なしの差し替えはガード停止になるため、Codex は python 直接起動を維持する。
    if WINDOWS:
        # /bin/sh がない。python を直接起動し、異常終了は guard.py 自身（excepthook）が拒否を返す。
        # / 区切りは Git Bash・cmd のどちらで解釈されても同じパスになる
        command = f'"{template_path(PYTHON)}" "{template_path(str(path))}"'
        return {'type': 'command', 'command': command + (' --codex' if codex else ''), 'timeout': 10}
    if path.name == 'guard.py' and not codex:
        return {'type': 'command', 'command': f'"/bin/sh" "{path.parent / GUARD_WRAPPER}"', 'timeout': 10}
    return {'type': 'command', 'command': f'"{PYTHON}" "{path}"' + (' --codex' if codex else ''), 'timeout': 10}


def _slashed(command):
    return str(command or '').replace('\\', '/')


def stdin_is_terminal():
    """stdin が利用者の端末か。Windows の isatty() は NUL（AI 道具の stdin）でも True になるため、
    コンソールのハンドルかを GetConsoleMode で確かめる。"""
    if not sys.stdin.isatty():
        return False
    if not WINDOWS:
        return True
    try:
        fileno = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return True  # ファイル記述子のない置き換え（テストの擬似端末）は isatty() に従う
    import ctypes
    import msvcrt
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(fileno), ctypes.byref(mode)))


def confirm_apply(count):
    """適用は利用者が端末で確認したときだけ行う。AI の非対話実行（stdin が TTY でない）は拒否する。
    環境変数などの迂回スイッチは設けない。"""
    if not stdin_is_terminal():
        raise RuntimeError('--apply must be run by the user in an interactive terminal (stdin is not a TTY); nothing was written')
    answer = input(f'{count} file(s) will be written. Type "apply" to continue: ')
    if answer.strip() != 'apply':
        raise RuntimeError('Aborted: confirmation did not match; nothing was written')


def _get(data, path):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def claude_settings(text, settings, warnings=None):
    claude = json.loads(text) if text and text.strip() else {}
    if not isinstance(claude, dict):
        raise ValueError('~/.claude/settings.json must be a JSON object')
    fragment = load_fragment(settings, 'claude.settings.json', 'claude.settings.aws.json')
    merge(claude, fragment, '~/.claude/settings.json')
    # 保護用の強制キーは既存値より優先する（例: --with-aws の env.AWS_PROFILE は読み取り専用プロファイルに固定）。
    for path in FORCED_CLAUDE_KEYS:
        wanted = _get(fragment, path)
        if wanted is None:
            continue
        current = _get(claude, path)
        if current != wanted:
            if warnings is not None:
                warnings.append(f'~/.claude/settings.json: {".".join(path)} will be overwritten '
                                f'(existing value differs from the enforced value)')
            _get(claude, path[:-1])[path[-1]] = wanted
    # 未確認の他の設定や既存 hook は保持し、所有する hook だけ置換する。
    hooks = claude.setdefault('hooks', {})
    if not isinstance(hooks, dict) or not all(
            isinstance(groups, list) and all(isinstance(g, dict) and isinstance(g.get('hooks', []), list)
                                             and all(isinstance(h, dict) for h in g.get('hooks', [])) for g in groups)
            for groups in hooks.values()):
        raise ValueError('~/.claude/settings.json: "hooks" has an unexpected structure; fix it manually')
    for groups in hooks.values():
        for group in groups:
            # Windows の既存登録は \ 区切りの場合もあるため / に揃えて所有 hook を判定する
            group['hooks'] = [h for h in group.get('hooks', []) if not any(
                n in _slashed(h.get('command', '')) for n in ['/hooks/guard.py', '/hooks/usage_alert.py', '/hooks/' + GUARD_WRAPPER])]
        groups[:] = [g for g in groups if g.get('hooks')]
    claude['hooks'].setdefault('PreToolUse', []).append(
        {'matcher': CLAUDE_GUARD_MATCHER_WINDOWS if WINDOWS else CLAUDE_GUARD_MATCHER,
         'hooks': [hook(HOME / '.claude/hooks/guard.py')]})
    for event in ['PreToolUse', 'UserPromptSubmit', 'Stop', 'SubagentStop']:
        group = {'hooks': [hook(HOME / '.claude/hooks/usage_alert.py')]}
        if event == 'PreToolUse':
            group['matcher'] = '.*'
        claude['hooks'].setdefault(event, []).append(group)
    return json.dumps(claude, ensure_ascii=False, indent=2) + '\n'


def load_fragment(settings, name, aws_name):
    """settings/ の断片を読み、--with-aws なら AWS 用断片を重ねる。"""
    data = {}
    for filename in [name] + ([aws_name] if settings['with_aws'] else []):
        text = render_data((HARNESS / 'settings' / filename).read_text(encoding='utf-8'),
                           settings['variables'], settings['with_aws'], f'settings/{filename}')
        merge(data, tomllib.loads(text) if filename.endswith('.toml') else json.loads(text))
    return data


def claude_mcp(text, mcp, warnings):
    """~/.claude.json は既存 MCP サーバーの版固定と env だけを変更する（サーバーは作成しない）。"""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
        raise ValueError('~/.claude.json: top level and "mcpServers" must be JSON objects')
    servers = data.get('mcpServers', {})
    pins = mcp.get('pins', {})
    for server in servers.values():
        if isinstance(server, dict) and server.get('command') == 'npx' and isinstance(server.get('args'), list):
            server['args'] = [pins.get(a, a) if isinstance(a, str) else a for a in server['args']]
    for name, values in mcp.get('servers', {}).items():
        server = servers.get(name)
        if isinstance(server, dict):
            if not isinstance(server.setdefault('env', {}), dict):
                raise ValueError(f'~/.claude.json: mcpServers.{name}.env must be an object')
            # 読み取り専用の強制値（プロファイル・READ_OPERATIONS_ONLY）は既存値より優先する
            server['env'].update(values.get('env', {}))
        else:
            warnings.append(f'~/.claude.json: MCP server "{name}" is not registered; its env was not set')
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


# ---------------- Codex ----------------
def replace_block(text, header, body):
    # 次のテーブル見出しか hook ブロックのマーカーまでを置換し、マーカーを消さない。
    pattern = r'(?ms)^' + re.escape(header) + r'\n.*?(?=^\[|^' + re.escape(BEGIN) + r'|^' + re.escape(END) + r'|\Z)'
    if not re.search(pattern, text):
        return text.rstrip() + '\n\n' + header + '\n' + body.rstrip() + '\n'
    return re.sub(pattern, lambda _: header + '\n' + body.rstrip() + '\n\n', text, count=1)


def add_values(text, header, values):
    parsed = tomllib.loads(text)
    node = parsed
    for part in header.strip('[]').split('.'):
        node = node.get(part, {})
    merged = dict(node)
    merged.update(values)
    body = '\n'.join(f'{k} = {json.dumps(v, ensure_ascii=False)}' for k, v in merged.items())
    return replace_block(text, header, body)


def owned_command(command):
    """生成した hook（python <~/.codex/hooks/guard.py|usage_alert.py> ...）の実行かどうか。コメントや他引数の言及は対象外"""
    try:
        # Windows のパス（\ 区切り）は shlex が \ を消すため、先に / へ揃える
        argv = shlex.split(_slashed(command)) if isinstance(command, str) else []
    except ValueError:
        return False
    if len(argv) >= 2 and os.path.basename(argv[0]) == 'sh':
        return argv[1].endswith('/.codex/hooks/' + GUARD_WRAPPER)
    return (len(argv) >= 2 and os.path.basename(argv[0]).lower().startswith('python')
            and argv[1].endswith(OWNED_CODEX_HOOKS))


def drop_owned(data):
    """パース済み設定から所有 hook を除き、空になったグループ・イベントを畳む（意味比較用）"""
    hooks = data.get('hooks')
    if isinstance(hooks, dict):
        for event in [e for e, v in hooks.items() if isinstance(v, list)]:
            groups = []
            for group in hooks[event]:
                children = group.get('hooks') if isinstance(group, dict) else None
                if isinstance(children, list) and children:
                    children = [h for h in children if not (isinstance(h, dict) and owned_command(h.get('command')))]
                    if not children:
                        continue
                    group = {**group, 'hooks': children}
                groups.append(group)
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
        if not hooks:
            del data['hooks']
    return data


def owned_chunk(chunk):
    try:
        return owned_command(tomllib.loads(''.join(chunk[1:])).get('command'))
    except tomllib.TOMLDecodeError:
        return False


def strip_owned_hooks(text):
    """所有する hook 定義とマーカーだけを除去する。
    マーカー位置に依存せず、Codex がブロック内に追記した [hooks.state] 等の他テーブルや利用者の hook は保持する。
    行単位の分割が文字列内の見出し風の行で誤る場合に備え、除去結果を意味比較し不一致なら中止する。"""
    chunks, current = [], []
    for line in text.splitlines(keepends=True):
        if line.strip() in (BEGIN, END):
            continue
        if line.lstrip().startswith('[') and current:
            chunks.append(current)
            current = []
        current.append(line)
    if current:
        chunks.append(current)
    out, k = [], 0
    while k < len(chunks):
        m = re.fullmatch(r'\[\[hooks\.(\w+)\]\]', chunks[k][0].strip())
        if m:
            j = k + 1
            while j < len(chunks) and chunks[j][0].strip() == f'[[hooks.{m.group(1)}.hooks]]':
                j += 1
            inner = chunks[k + 1:j]
            kept = [c for c in inner if not owned_chunk(c)]
            # 所有 hook だけを除き、利用者の hook が残るグループは matcher ごと保持する。
            if len(kept) < len(inner):
                if kept:
                    out += [chunks[k]] + kept
                k = j
                continue
        out.append(chunks[k])
        k += 1
    result = ''.join(''.join(c) for c in out)
    if drop_owned(tomllib.loads(result)) != drop_owned(tomllib.loads(text)):
        raise RuntimeError('Codex hook removal changed unrelated settings; edit config.toml manually')
    return result


def codex_config(text, fragment, mcp=None, warnings=None):
    """settings/codex.config*.toml・mcp*.json の断片を既存設定に適用し、所有 hook ブロックを末尾に置き直す。"""
    mcp = mcp or {}
    warnings = [] if warnings is None else warnings
    text = strip_owned_hooks(text)
    for key, value in fragment.get('defaults', {}).items():
        # 未設定の最上位キーだけ先頭に追加する（利用者の値は変えない）
        if key not in tomllib.loads(text):
            text = f'{key} = {json.dumps(value)}\n' + text
    for package, pinned in mcp.get('pins', {}).items():
        text = text.replace(json.dumps(package), json.dumps(pinned))
    for name, values in mcp.get('servers', {}).items():
        # 未定義のサーバーに env テーブルだけ作ると起動コマンドのない定義になるため追加しない
        if isinstance(tomllib.loads(text).get('mcp_servers', {}).get(name), dict):
            text = add_values(text, f'[mcp_servers.{name}.env]', values.get('env', {}))
        else:
            warnings.append(f'~/.codex/config.toml: [mcp_servers.{name}] is not defined; its env was not set')
    for name, values in fragment.get('set', {}).items():
        text = add_values(text, f'[{name}]', values)
    policy = tomllib.loads(text).get('shell_environment_policy', {})
    excluded = list(dict.fromkeys(policy.get('exclude', []) + fragment.get('shell_environment', {}).get('exclude', [])))
    # 親テーブルと set 子テーブルを混在させず追加する。
    scalar = {k: v for k, v in policy.items() if not isinstance(v, dict)}
    scalar['exclude'] = excluded
    text = replace_block(text, '[shell_environment_policy]', '\n'.join(f'{k} = {json.dumps(v)}' for k, v in scalar.items()))
    block = [BEGIN]
    # 追加される MCP も含め全 MCP を guard に通す（Codex は aws-api を aws_api に正規化する）。
    for event, filename, matcher in [('PreToolUse', 'guard.py', 'Bash|apply_patch|Read|mcp__.*'),
                                     ('PreToolUse', 'usage_alert.py', '.*'), ('UserPromptSubmit', 'usage_alert.py', None),
                                     ('Stop', 'usage_alert.py', None), ('SubagentStop', 'usage_alert.py', None)]:
        block.append(f'[[hooks.{event}]]')
        if matcher:
            block.append('matcher = ' + json.dumps(matcher))
        block.append(f'[[hooks.{event}.hooks]]')
        for k, v in hook(HOME / '.codex/hooks' / filename, True).items():
            block.append(f'{k} = {json.dumps(v)}')
    block.append(END)
    text = text.rstrip() + '\n\n' + '\n'.join(block) + '\n'
    tomllib.loads(text)
    return text


# ---------------- 配置計画 ----------------
def guard_wrapper(directory):
    return ('#!/bin/sh\n'
            '# install.py が生成。guard.py を起動できない・異常終了した場合も拒否を返す（fail-closed）。\n'
            f'{shlex.quote(PYTHON)} {shlex.quote(str(directory / "guard.py"))} "$@" && exit 0\n'
            f'printf \'%s\\n\' {shlex.quote(DENY_ON_FAILURE)}\n')


def tree_files(source):
    """配置対象のファイル（テスト・キャッシュ・リンクを除く）を相対パスで列挙する。"""
    files = []
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source)
        if any(part in SKIP_NAMES for part in rel.parts) or path.is_symlink():
            continue
        if path.is_file():
            files.append(rel)
    return files


def skill_edits(name, destination, settings):
    source = HARNESS / 'skills' / name
    variables = dict(settings['variables'], SKILL_DIR=str(destination))
    edits = {}
    for rel in tree_files(source):
        text = (source / rel).read_text(encoding='utf-8')
        if rel.suffix in RENDER_SUFFIXES:
            text = render(text, variables, settings['with_aws'], f'skills/{name}/{rel}')
        edits[destination / rel] = text
    if name in SKILLS_WITH_RUNTIME:
        edits[destination / RUNTIME_CONFIG] = runtime_json(settings)
    return edits


def runtime_json(settings):
    return json.dumps(settings['runtime'], ensure_ascii=False, indent=2) + '\n'


def read(path):
    if path.is_symlink():
        raise RuntimeError(f'Refusing symlink: {path}')
    return path.read_text(encoding='utf-8') if path.exists() else None


def build(settings, target):
    """(書き込み予定 {Path: 内容}, 警告) を返す。何も書き込まない。"""
    edits, warnings = {}, []
    with_aws = settings['with_aws']
    mcp = load_fragment(settings, 'mcp.json', 'mcp.aws.json')
    runtimes = [r for r in ('claude', 'codex') if target in (r, 'both')]
    for runtime in runtimes:
        base = HOME / ('.claude' if runtime == 'claude' else '.codex') / 'hooks'
        edits[base / RUNTIME_CONFIG] = runtime_json(settings)
        for name in ('guard.py', 'usage_alert.py'):
            edits[base / name] = (HARNESS / 'hooks' / name).read_text(encoding='utf-8')
        if not WINDOWS:  # Windows は sh ラッパーを使わない（hook() 参照）
            edits[base / GUARD_WRAPPER] = guard_wrapper(base)
        instructions = HOME / ('.claude/CLAUDE.md' if runtime == 'claude' else '.codex/AGENTS.md')
        edits[instructions] = insert_block(read(instructions), instructions_block(settings), warnings, str(instructions))
        skills_root = HOME / ('.claude/skills' if runtime == 'claude' else '.agents/skills')
        for name in SKILLS + (AWS_SKILLS if with_aws else ()):
            edits.update(skill_edits(name, skills_root / name, settings))
    if 'claude' in runtimes:
        for name in AGENTS:
            edits[HOME / '.claude/agents' / f'{name}.md'] = render(
                (HARNESS / 'agents' / f'{name}.md').read_text(encoding='utf-8'), settings['variables'], with_aws,
                f'agents/{name}.md')
        path = HOME / '.claude/settings.json'
        edits[path] = claude_settings(read(path), settings, warnings)
        path = HOME / '.claude.json'
        current = read(path)
        if current is not None:
            edits[path] = claude_mcp(current, mcp, warnings)
        elif mcp.get('servers'):
            warnings.append('~/.claude.json does not exist; MCP env was not set')
    if 'codex' in runtimes:
        path = HOME / '.codex/config.toml'
        fragment = load_fragment(settings, 'codex.config.toml', 'codex.config.aws.toml')
        edits[path] = codex_config(read(path) or '', fragment, mcp, warnings)
    return edits, warnings


def priority(path):
    # 登録前に hook と設定 sidecar を配置する。稼働中のクライアントが未配置・設定なしの hook を参照しない。
    if path.parent.name == 'hooks':
        return 0 if path.name == RUNTIME_CONFIG else 1
    return 3 if path in registrations() else 2


def registrations():
    return {HOME / '.claude/settings.json', HOME / '.claude.json', HOME / '.codex/config.toml'}


def change_summary(old, new):
    if old is None:
        return 'CREATE', f'+{len(new.splitlines())} lines'
    if old == new:
        return 'SAME', ''
    added = removed = 0
    a, b = old.splitlines(), new.splitlines()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            removed += i2 - i1
        if tag in ('replace', 'insert'):
            added += j2 - j1
    return 'UPDATE', f'+{added} -{removed} lines'


def atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.harness-')
    try:
        # newline='' : Windows でも LF のまま書く（CRLF に変換すると比較・hook スクリプトが壊れる）
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as out:
            if hasattr(os, 'fchmod'):  # Windows（3.12 まで）にはない。権限ビットも読み取り専用属性しか表さない
                os.fchmod(out.fileno(), mode)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        replace_with_retry(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def replace_with_retry(source, target, attempts=5):
    """os.replace。Windows では OneDrive・起動中のアプリが一時的に開いていると PermissionError になるため短く再試行する。"""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def check_python(python):
    # guard_hook.sh はこのインタプリタを固定する。消えうる venv・ハーネス配下だと全道具が拒否で固まる。
    path = Path(python)
    if not path.is_absolute() or not os.access(path, os.X_OK) or (WINDOWS and not path.is_file()):
        raise RuntimeError(f'Python interpreter is not executable: {python}')
    if WINDOWS and 'windowsapps' in str(path).lower():
        # Microsoft Store の python.exe は実行するとストアを開くだけの別名（hook では動かない）
        raise RuntimeError(f'Use a real Python installation, not the Microsoft Store alias: {python}')
    resolved = path.resolve()
    in_venv = any((p.parent / 'pyvenv.cfg').exists() for p in {path.parent, resolved.parent})
    if python == sys.executable and sys.prefix != sys.base_prefix:
        in_venv = True
    if in_venv or resolved.is_relative_to(HARNESS.resolve()):
        raise RuntimeError(f'Use a system interpreter for hooks, not {python} (set "python" in the config '
                           'or run install.py with a system python3)')


def parse_args(argv):
    # 省略形（--ap 等）で適用されないよう、オプションは完全一致のみ受け付ける
    parser = argparse.ArgumentParser(description='Install ai-harness (dry-run unless --apply).', allow_abbrev=False)
    parser.add_argument('--config', default=str(HARNESS / 'config.local.json'),
                        help='user config JSON inside this repository (default: config.local.json)')
    parser.add_argument('--target', choices=('claude', 'codex', 'both'), default='both')
    parser.add_argument('--with-aws', action='store_true', help='install the optional AWS read-only module')
    parser.add_argument('--apply', action='store_true', help='write files (default is dry-run)')
    return parser.parse_args(argv)


def main(argv=None):
    global PYTHON
    args = parse_args(argv)
    settings = load_settings(args.config, args.with_aws)
    PYTHON = settings['python']
    check_python(PYTHON)
    os.umask(0o077)
    watched = [HOME / p for p in ('.claude/settings.json', '.claude.json', '.codex/config.toml',
                                  '.claude/CLAUDE.md', '.codex/AGENTS.md')]
    snapshots = {p: read(p) for p in watched}
    edits, warnings = build(settings, args.target)
    if any(read(p) != content for p, content in snapshots.items()):
        raise RuntimeError('Configuration changed during preparation; retry without overwriting it')
    edits = dict(sorted(edits.items(), key=lambda item: (priority(item[0]), str(item[0]))))
    originals = {p: read(p) for p in edits}
    changed = {p: c for p, c in edits.items() if originals[p] != c}
    for p, content in edits.items():
        status, detail = change_summary(originals[p], content)
        print(f'{status:6} {p}' + (f' ({detail})' if detail else ''))
    for warning in warnings:
        print('WARN   ' + warning)
    if not args.apply:
        print(f'DRY-RUN: {len(changed)} file(s) would change; nothing was written. Re-run with --apply to install.')
        return 0
    confirm_apply(len(changed))
    if args.with_aws:
        aws = HOME / '.aws'
        if aws.is_symlink() or (aws / 'credentials').is_symlink():
            raise RuntimeError('Refusing symlink AWS path')
    backup = HOME / '.ai-harness/backups' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True, mode=0o700)
    manifest, previous = {}, {}
    # 変更するファイルだけを退避する（機密資格情報は読み込まない）。
    for i, (p, content) in enumerate(changed.items()):
        old = originals[p]
        if p in snapshots and old != snapshots[p]:
            raise RuntimeError(f'Concurrent edit detected before backup: {p}')
        if old is not None:
            dest = backup / f'{i:03d}-{p.name}'
            atomic(dest, old)
            manifest[str(p)] = str(dest)
            previous[p] = (old, p.stat().st_mode & 0o777)
        else:
            previous[p] = None
    atomic(backup / 'manifest.json', json.dumps(manifest, indent=2))
    completed = []
    try:
        for p, content in changed.items():
            # 読み取り以降に他のアプリが変更したファイルを上書きしない。
            if read(p) != originals[p]:
                raise RuntimeError(f'Concurrent edit detected: {p}')
            atomic(p, content)
            completed.append(p)
        if args.with_aws:
            aws = HOME / '.aws'
            if aws.exists():
                aws.chmod(0o700)
            if (aws / 'credentials').exists():
                (aws / 'credentials').chmod(0o600)
    except Exception:
        for p in reversed(completed):
            # 適用後の外部変更は保持する。
            if read(p) != changed[p]:
                continue
            if previous[p]:
                atomic(p, *previous[p])
            else:
                p.unlink()
        raise
    print('BACKUP ' + str(backup))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
