#!/usr/bin/env python3
"""setup.sh・apply.sh の補助（標準ライブラリのみ、Python 3.8 以上で動く）。git hook からは使わない。

サブコマンド（cwd はリポジトリのどこでもよい。パスはリポジトリ直下からの相対で解決する）:
  system-python                 venv ではない Python 3.11 以上（tomllib あり）を探して表示。なければ終了コード 1
  example-field KEY             config.example.json の最上位キーの値を表示（言語の既定値用）
  write-config ...              config.local.json を新規作成（既存なら上書きせず終了コード 1、権限 0600）。
                                extra_protected_paths に .githooks 等（PROTECTED_DEFAULTS）の絶対パスを入れる
  aws-mode                      config.local.json の aws 節: off / on / placeholder（見本の値のまま）。
                                aws が object でない・大文字小文字違いのキーはエラー
  python-of                     install.py を動かすインタプリタ（設定の python、なければ venv ではない Python 3.11 以上、
                                それもなければ PATH の python3）
  apply-command                 利用者が端末で実行する --apply コマンドを表示（実行はしない。placeholder なら終了コード 1）
  dry-run                       install.py の dry-run を実行して要約を表示（書き込みなし）
                                終了コード: 0=変更予定あり, 3=変更なし, 1=エラー
  setup-windows [--no-apply]    Windows の初期設定（setup.cmd が呼ぶ）。setup.sh と同じ段階を対話で行う
  apply-windows                 Windows の設置本反映（apply.cmd が呼ぶ）。apply.sh と同じ段階
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
WINDOWS = os.name == 'nt'
# 利用者が実行する初期設定・反映の入口（Windows は sh がないため .cmd）
SETUP_ENTRY = 'setup.cmd' if WINDOWS else './setup.sh'
APPLY_ENTRY = 'apply.cmd' if WINDOWS else './apply.sh'
# よく使われるシステム Python の場所（PATH の python3 が venv のときの候補）
FIXED_CANDIDATES = ('/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3')
# venv ではなく、install.py が必要とする tomllib（3.11 以上）を持つかを確かめる
PROBE = 'import sys, tomllib; sys.exit(0 if sys.prefix == sys.base_prefix else 1)'
# 新規作成する config.local.json の extra_protected_paths に足す（clone 内の相対パス）
PROTECTED_DEFAULTS = ('.githooks', 'tools/privacy_check.py', 'tools/harness_setup.py', 'setup.sh', 'apply.sh',
                      'setup.cmd', 'apply.cmd')
PLACEHOLDER_HELP = ('config.local.json 의 aws.roles 또는 aws.mfa_serial 이 아직 견본 값입니다. '
                    f'실제 값으로 편집한 뒤 {SETUP_ENTRY} 를 다시 실행하세요.')
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


def inside(path, base):
    """path が base の配下か（区切りは OS に従う。Windows は大文字小文字を区別しない）。"""
    return os.path.normcase(str(path)).startswith(os.path.normcase(str(base).rstrip('/\\')) + os.sep)


def in_venv(path):
    for candidate in {path, path.resolve()}:
        if (candidate.parent / 'pyvenv.cfg').exists() or (candidate.parent.parent / 'pyvenv.cfg').exists():
            return True
    venv = os.environ.get('VIRTUAL_ENV')
    return bool(venv) and inside(path, venv)


def windows_candidates():
    """Windows の Python 候補。py ランチャーが知っているインストール・既定のインストール先・PATH の python.exe。
    Microsoft Store の別名（WindowsApps）は実行してもストアを開くだけなので除く。"""
    found = []
    try:
        listing = subprocess.run(['py', '-0p'], capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=PROBE_TIMEOUT).stdout
        found += re.findall(r'([A-Za-z]:\\[^\r\n]*?python\.exe)\s*$', listing, re.M)
    except (OSError, subprocess.TimeoutExpired):
        pass
    local = os.environ.get('LOCALAPPDATA')
    if local:
        found += sorted(map(str, Path(local, 'Programs', 'Python').glob('Python3*/python.exe')), reverse=True)
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        if directory:
            found.append(os.path.join(directory, 'python.exe'))
    return [p for p in found if 'windowsapps' not in p.lower()]


def system_python():
    candidates = []
    if WINDOWS:
        candidates = windows_candidates()
    else:
        for directory in os.environ.get('PATH', '').split(os.pathsep):
            if directory:
                candidates.append(os.path.join(directory, 'python3'))
        candidates.extend(FIXED_CANDIDATES)
    seen = set()
    for raw in candidates:
        path = Path(raw)
        if not path.is_absolute() or not os.access(path, os.X_OK) or path.is_dir() or (WINDOWS and not path.is_file()):
            continue
        resolved = path.resolve()
        # venv 内のリンクは実体が同じでも除外し、同じ実体の別の（venv 外の）パスは候補に残す
        if in_venv(path) or inside(resolved, ROOT.resolve()):
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
    """aws 節の状態。キーがなければ off。キーはあるが object でない（null 等）・大文字小文字違いのキーはエラー。"""
    variants = [k for k in data if isinstance(k, str) and k.strip().lower() == 'aws' and k != 'aws']
    if variants:
        raise SystemExit(f'ERROR: {CONFIG}: unexpected key {variants[0]!r} (did you mean "aws"?)')
    if 'aws' not in data:
        return 'off'
    section = data['aws']
    if not isinstance(section, dict):
        raise SystemExit(f'ERROR: {CONFIG}: "aws" must be an object (remove the key to disable the AWS module)')
    example = load_json(ROOT / EXAMPLE).get('aws') or {}
    if section.get('roles') == example.get('roles') or section.get('mfa_serial') == example.get('mfa_serial'):
        return 'placeholder'
    return 'on'


def expand(value):
    value = str(Path.home()) + value[1:] if value.startswith('~') else value
    return os.path.normpath(value) if WINDOWS else value


def python_of(data):
    value = data.get('python')
    if isinstance(value, str) and value.strip():
        return expand(value.strip())
    # 未設定なら venv ではない Python 3.11 以上（install.py は venv の python を拒否する）。見つからなければ PATH の python3
    return system_python() or shutil.which('python3') or 'python3'


def install_args(data):
    # どの cwd から実行しても同じ意味になるよう絶対パスで渡す
    args = [str(ROOT / INSTALL), '--config', str(config_path())]
    if aws_mode(data) != 'off':
        args.append('--with-aws')
    return args


def protected_defaults():
    # 個人情報検査・git hook・初期設定の仕組みを AI が承認なしに弱められないよう、ガードの追加保護パスに入れる（clone の絶対パス）
    return [str(ROOT / name) for name in PROTECTED_DEFAULTS]


def cmd_write_config(args):
    path = config_path()
    example = load_json(ROOT / EXAMPLE)
    data = {
        'project_root': args.project_root,
        'memory_dir': example.get('memory_dir'),
        'python': args.python or None,
        'response_language': args.response_language,
        'comment_language': args.comment_language,
        'extra_protected_paths': list(example.get('extra_protected_paths', [])) + protected_defaults(),
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
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as out:
        out.write(content)
    if WINDOWS:
        restrict_to_owner(path)
    print(f'created {CONFIG}')
    return 0


def restrict_to_owner(path):
    """Windows の権限ビット（0600）は読み取り専用属性しか表さないため、ACL で本人だけが読み書きできるようにする。"""
    user = os.environ.get('USERNAME')
    if not user:
        return
    try:
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'{user}:F'],
                       capture_output=True, timeout=PROBE_TIMEOUT, check=True)
    except (OSError, subprocess.SubprocessError):
        print(f'      주의: {CONFIG} 의 접근 권한을 본인으로 제한하지 못했습니다(icacls 실패).', file=sys.stderr)


def cmd_dry_run(_args):
    path = config_path()
    if not path.is_file():
        print(f'ERROR: {CONFIG} not found (run {SETUP_ENTRY})', file=sys.stderr)
        return 1
    data = load_json(path)
    command = [python_of(data), *install_args(data)]
    try:
        # install.py の出力（パス・警告）を UTF-8 で受け渡す（Windows の既定コードページでは韓国語のパスが壊れる）
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=DRY_RUN_TIMEOUT, stdin=subprocess.DEVNULL,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
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
    for name in ('aws-mode', 'python-of', 'apply-command', 'dry-run', 'apply-windows'):
        sub.add_parser(name)
    sub.add_parser('setup-windows').add_argument('--no-apply', action='store_true')
    args = parser.parse_args(argv)

    if args.command == 'setup-windows':
        return setup_windows(args.no_apply)
    if args.command == 'apply-windows':
        return apply_windows()
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
        if aws_mode(data) == 'placeholder':
            # 見本の値のまま適用させない（setup.sh と同じく編集を案内する）
            print(PLACEHOLDER_HELP, file=sys.stderr)
            return 1
        print(apply_command(data))
    return 0


def apply_command(data):
    """利用者が端末で実行する --apply コマンドの表示。Windows は PowerShell で貼り付けて動く形にする。"""
    argv = [python_of(data), *install_args(data), '--apply']
    if WINDOWS:
        quote = lambda a: a if re.fullmatch(r'--[\w-]+', a) else "'" + a.replace("'", "''") + "'"
        return '& ' + ' '.join(quote(a) for a in argv)
    return ' '.join(shlex.quote(a) for a in argv)


# ---------------- Windows（setup.cmd・apply.cmd） ----------------
def stdin_is_terminal():
    """stdin が利用者の端末か。Windows の isatty() は NUL（AI 道具の stdin）でも True になるため、
    コンソールのハンドルかを GetConsoleMode で確かめる（install.py と同じ判定）。"""
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


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        print()
        raise SystemExit('입력이 끝나 중단합니다.')


def confirm(prompt):
    return ask(prompt + ' [y/N]: ').strip() in ('y', 'Y', 'yes', 'YES', 'Yes')


def git_config(key, value=None):
    args = ['git', '-C', str(ROOT), 'config', '--local'] + ([key, value] if value is not None else ['--get', key])
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=PROBE_TIMEOUT)
    if value is not None and result.returncode != 0:
        raise SystemExit(f'ERROR: git config {key} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def require_terminal(entry):
    if not stdin_is_terminal():
        print(f'{entry}는 사용자가 터미널에서 직접 실행해야 합니다(stdin이 터미널이 아님). 아무것도 바꾸지 않았습니다.',
              file=sys.stderr)
        raise SystemExit(1)


def run_apply(data):
    """install.py --apply を端末を引き継いで実行する（install.py 自身の apply 入力確認を経る）。"""
    return subprocess.run([python_of(data), *install_args(data), '--apply'], cwd=ROOT).returncode


def setup_windows(no_apply):
    require_terminal(SETUP_ENTRY)
    top = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                         encoding='utf-8', timeout=PROBE_TIMEOUT).stdout.strip()
    if not top or os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(ROOT)):
        print(f'{SETUP_ENTRY}는 ai-harness 저장소(git clone한 폴더) 바로 아래에서만 동작합니다.', file=sys.stderr)
        return 1

    # --- 1) コミット作成者（このリポジトリだけ） ---
    name, email = git_config('user.name'), git_config('user.email')
    if name and email:
        print('[1/5] 이 저장소의 커밋 작성자 설정이 있습니다. 건너뜁니다.')
    else:
        print('[1/5] 이 저장소에만 쓰는 커밋 작성자(개인 GitHub 계정용)를 설정합니다. 전역 git 설정은 바꾸지 않습니다.')
        print('      메일은 GitHub에 등록한 개인 메일 또는 GitHub noreply 메일(<ID>+<사용자명>@users.noreply.github.com)을 권장합니다.')
        while not name:
            name = ask('      user.name: ').strip()
        git_config('user.name', name)
        while not re.fullmatch(r'.+@.+', email or ''):
            email = ask('      user.email: ').strip()
        git_config('user.email', email)

    # --- 2) git hook の場所 ---
    hooks = git_config('core.hooksPath')
    if hooks == '.githooks':
        print('[2/5] core.hooksPath=.githooks 가 설정되어 있습니다. 건너뜁니다.')
    elif not hooks or confirm(f"[2/5] core.hooksPath가 이미 '{hooks}'로 설정되어 있습니다. .githooks로 바꿀까요?"):
        git_config('core.hooksPath', '.githooks')
        print('[2/5] core.hooksPath=.githooks 를 설정했습니다(커밋 전 개인정보 검사·pull 후 반영 알림).')
    else:
        print('[2/5] core.hooksPath를 바꾸지 않았습니다. 이 저장소의 hook은 동작하지 않습니다.')

    # --- 3) 利用者設定 ---
    path = config_path()
    if path.exists() or path.is_symlink():
        print(f'[3/5] {CONFIG} 이 있습니다. 건너뜁니다.')
    else:
        print(f'[3/5] {CONFIG} 을 만듭니다(Git 제외 파일, 이 컴퓨터 전용).')
        default_project = str(Path.home() / 'project')
        while True:
            project = ask(f'      project_root(주 작업 프로젝트 절대 경로) [{default_project}]: ').strip() or default_project
            if os.path.isabs(project) or project == '~' or project.startswith('~/') or project.startswith('~\\'):
                break
            print('      절대 경로(또는 ~로 시작)로 입력하세요.')
        detected = system_python()
        if not detected:
            print('      venv가 아닌 Python 3.11 이상을 찾지 못했습니다. 경로를 직접 입력하세요.')
        while True:
            prompt = f'      hook용 python(venv 밖 Python 3.11+) [{detected}]: ' if detected else '      hook용 python: '
            python = ask(prompt).strip().strip('"') or (detected or '')
            if python and os.path.isabs(python) and os.path.isfile(python) and 'windowsapps' not in python.lower():
                break
            print('      실행 가능한 python.exe 의 절대 경로를 입력하세요(Microsoft Store 별칭은 사용할 수 없음).')
        example = load_json(ROOT / EXAMPLE)
        lang = ''
        while not lang:
            lang = ask(f"      응답 언어 [{example.get('response_language')}]: ").strip() or example.get('response_language', '')
        comment = ''
        while not comment:
            comment = ask(f"      코드 주석 언어 [{example.get('comment_language')}]: ").strip() or example.get('comment_language', '')
        print('      AWS 읽기 전용 모듈(--with-aws)은 Windows에서 아직 지원하지 않아 끈 상태로 만듭니다.')
        args = argparse.Namespace(project_root=project, python=python, response_language=lang,
                                  comment_language=comment, aws=False)
        if cmd_write_config(args):
            return 1

    data = load_json(path)
    aws = aws_mode(data)
    print(f'      AWS 모듈(--with-aws): {aws}')
    if aws != 'off':
        print(f'      Windows에서는 AWS 모듈을 아직 지원하지 않습니다. {CONFIG} 에서 aws 절을 지운 뒤 다시 실행하세요.',
              file=sys.stderr)
        return 1

    # --- 4) dry-run ---
    print('[4/5] 설치 예정 내용을 확인합니다(쓰기 없음).')
    status = cmd_dry_run(None)
    if status == 3:
        print('[5/5] 설치본이 이미 최신입니다. 할 일이 없습니다.')
        return 0
    if status != 0:
        print('dry-run이 실패했습니다. 위 오류를 확인한 뒤 다시 실행하세요.', file=sys.stderr)
        return 1
    command = apply_command(data)
    if no_apply:
        print(f'[5/5] --no-apply: 적용하지 않았습니다. 적용 명령: {command}')
        return 0

    # --- 5) 適用（install.py 自身の端末確認を経る） ---
    print('      주의: Claude Code·Codex가 실행 중이면 ~/.claude.json 등을 동시에 써서 적용이 중단될 수 있습니다. 모두 종료한 뒤 적용하세요.')
    if not confirm('[5/5] 위 내용으로 설치를 적용할까요? (다음 확인에서 apply를 입력해야 실제로 씁니다)'):
        print(f'적용하지 않았습니다. 나중에 적용하려면: {APPLY_ENTRY}')
        return 0
    return run_apply(data)


def apply_windows():
    require_terminal(APPLY_ENTRY)
    path = config_path()
    if not path.is_file():
        print(f'{CONFIG} 이 없습니다. 이 컴퓨터에서 처음이면 터미널에서 {SETUP_ENTRY} 를 먼저 실행하세요.', file=sys.stderr)
        return 1
    data = load_json(path)
    aws = aws_mode(data)
    print(f'AWS 모듈(--with-aws): {aws}')
    if aws != 'off':
        print(f'Windows에서는 AWS 모듈을 아직 지원하지 않습니다. {CONFIG} 에서 aws 절을 지운 뒤 다시 실행하세요.',
              file=sys.stderr)
        return 1
    print('설치 예정 내용을 확인합니다(쓰기 없음).')
    status = cmd_dry_run(None)
    if status == 3:
        print('설치본이 이미 최신입니다. 할 일이 없습니다.')
        return 0
    if status != 0:
        print('dry-run이 실패했습니다. 위 오류를 확인한 뒤 다시 실행하세요.', file=sys.stderr)
        return 1
    print()
    print('주의: Claude Code·Codex가 실행 중이면 ~/.claude.json 등을 동시에 써서 적용이 중단될 수 있습니다. 모두 종료한 뒤 적용하세요.')
    print('적용하려면 다음 확인에서 apply 를 입력하세요(그 밖의 입력은 취소).')
    return run_apply(data)


if __name__ == '__main__':
    if WINDOWS:
        # パイプ・リダイレクト時も韓国語の案内を出せるようにする（コンソールへの出力はもともと Unicode）
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())
