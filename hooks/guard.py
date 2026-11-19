#!/usr/bin/env python3
"""Claude/Codex PreToolUse ガード。補助防御であり、シェルの完全な解析器ではない。

Claude 経由の操作だけを制限する（ユーザー自身のターミナル操作には影響しない）。
  1. ファイル削除 → ユーザー承認（一時領域・検証済み Python キャッシュは除外）
  2. AWS → 設定した read-only プロファイル以外の認証情報を禁止（aws.enabled=false ならプロファイル固定のみ無効）
  3. AWS → 参照系以外の操作（作成・変更・削除・publish・deploy）を禁止（aws.enabled に関係なく常に有効）
  4. ガード設定・認証情報ファイル自体の改変 → ユーザー承認、認証情報の読み取り → 禁止

設定は同じディレクトリの harness_config.json（install.py が生成）から読む。
欠落・破損・不正値の場合は全操作を拒否する（fail-closed）。
"""
import glob
import json
import os
import re
import shlex
import signal
import sys
import stat
import time

HOME = os.path.expanduser("~")
RUNTIME = "codex" if "--codex" in sys.argv else "claude"
# Codex 起動側（cross_check.py の TOOL_POLICY_ENV と同名）が渡す道具方針。hook matcher 対象の道具について
# none=全拒否、read=Bash・Read のみ検査付きで許可（matcher 外の道具は起動側の実行監視が止める）
TOOL_POLICY_ENV = "HARNESS_CODEX_TOOL_POLICY"
# hook の timeout（10 秒）超過は未判定のまま実行されるため、その前に拒否する
GUARD_SECONDS = 8
CONFIG_NAME = "harness_config.json"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,128}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ConfigError(ValueError):
    """ガード設定の欠落・破損。保護を弱めないよう全操作を拒否する。"""


def _abs_path(value, name):
    if not isinstance(value, str) or not os.path.isabs(value) or any(c in value for c in "\n\r\0"):
        raise ConfigError(f"{name} は絶対パスの文字列が必要")
    return os.path.normpath(value)


def load_config(path=CONFIG_PATH):
    """設定を検証して返す。不正なら ConfigError（main() で全操作拒否にする）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as error:
        # 深い入れ子の RecursionError 等も含め、読めない設定はすべて設定エラーにする
        raise ConfigError(f"{CONFIG_NAME} を読めない（{type(error).__name__}）") from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConfigError("version は 1 が必要")
    config = {"project_root": _abs_path(data.get("project_root"), "project_root"),
              "harness_home": _abs_path(data.get("harness_home"), "harness_home"),
              "profile": None, "assume_role_cmd": None, "assume_role_envs": ()}
    cloud = data.get("aws")
    if not isinstance(cloud, dict) or not isinstance(cloud.get("enabled"), bool):
        raise ConfigError("aws.enabled は真偽値が必要")
    config["aws_enabled"] = cloud["enabled"]
    if cloud["enabled"]:
        profile = cloud.get("profile")
        if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
            raise ConfigError("aws.profile が不正")
        command = cloud.get("assume_role_cmd") or None
        envs = cloud.get("assume_role_envs", [])
        if not isinstance(envs, list) or not all(isinstance(e, str) and ENV_NAME_RE.fullmatch(e) for e in envs):
            raise ConfigError("aws.assume_role_envs は英数字の文字列リストが必要")
        config.update(profile=profile, assume_role_envs=tuple(envs),
                      assume_role_cmd=_abs_path(command, "aws.assume_role_cmd") if command else None)
    extra = data.get("extra_protected_paths", [])
    if not isinstance(extra, list):
        raise ConfigError("extra_protected_paths はリストが必要")
    config["extra_protected_paths"] = [_abs_path(p, "extra_protected_paths") for p in extra]
    return config


try:
    CONFIG, CONFIG_ERROR = load_config(), None
except Exception as _error:
    # 判定関数は最も厳しい既定値で動くが、main() は設定エラーなら全操作を拒否する。
    # 読み込み時の例外で異常終了すると Codex の直接起動は未判定で実行されるため、例外はすべてここで止める。
    CONFIG, CONFIG_ERROR = None, (str(_error) if isinstance(_error, ConfigError) else type(_error).__name__)

PROJECT = CONFIG["project_root"] if CONFIG else None
HARNESS_HOME = CONFIG["harness_home"] if CONFIG else None
AWS_ENABLED = CONFIG["aws_enabled"] if CONFIG else True
# 設定エラー時は実在し得ないプロファイル名にして、プロファイル指定はすべて拒否側に倒す
ALLOWED_PROFILE = (CONFIG["profile"] if CONFIG else "\0invalid-config") if AWS_ENABLED else None
ASSUME_ROLE_CMD = CONFIG["assume_role_cmd"] if CONFIG else None
ASSUME_ROLE_ENVS = CONFIG["assume_role_envs"] if CONFIG else ()

# 承認なしで削除してよい一時ディレクトリ
TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
                 "$TMPDIR", "${TMPDIR")


def _protected_write():
    """改変時に承認を求める保護パスと、コマンド文字列中の保護語。"""
    paths = [os.path.join(HOME, rel) for rel in (
        ".claude/settings.json", ".claude/hooks/", ".claude.json", ".aws/", ".codex/config.toml", ".codex/hooks/")]
    words = [".claude/settings", ".claude/hooks", ".claude.json", ".aws/", ".codex/config", ".codex/hooks", CONFIG_NAME]
    if HARNESS_HOME:
        name = os.path.basename(HARNESS_HOME)
        # ハーネス本体（hook・インストーラ・設定断片・default 認証で動く認証更新スクリプト）と利用者設定
        for sub in ("hooks", "install", "settings", "tools/aws"):
            paths.append(os.path.join(HARNESS_HOME, sub) + "/")
            words.append(f"{name}/{sub}")
        paths.append(os.path.join(HARNESS_HOME, "config.local.json"))
        words.append("config.local.json")
    if PROJECT:
        paths.append(os.path.join(PROJECT, ".claude/settings"))
    if ASSUME_ROLE_CMD:
        paths.append(ASSUME_ROLE_CMD)
        words.append(os.path.basename(ASSUME_ROLE_CMD))
    for extra in (CONFIG or {}).get("extra_protected_paths", []):
        paths.append(extra)
        words.append(extra)
    return paths, words


PROTECTED_WRITE, PROTECTED_WRITE_WORDS = _protected_write()


_PROTECTED_ENTRIES_CACHE = {}


def _protected_entries():
    """保護パスを (基準パス, ディレクトリか) で字句・実体の両方について列挙する（保護パス自体がリンクの場合も含む）。
    PROTECTED_WRITE の内容ごとにキャッシュする（実体パス解決の繰り返しを避ける）。"""
    key = tuple(PROTECTED_WRITE)
    if key not in _PROTECTED_ENTRIES_CACHE:
        entries = []
        for prot in key:
            base = os.path.normpath(prot)
            for b in dict.fromkeys((base, os.path.realpath(base))):
                entries.append((b, prot.endswith("/")))
        _PROTECTED_ENTRIES_CACHE[key] = tuple(entries)
    return _PROTECTED_ENTRIES_CACHE[key]


def is_protected_write(path):
    """絶対パスが改変承認の対象（保護パス配下・任意プロジェクトの .claude/settings*）なら True。"""
    for base, is_dir in _protected_entries():
        # ディレクトリはそれ自体（mv・cp の宛先）と配下、それ以外は従来どおり前方一致
        if (path == base or path.startswith(base + "/")) if is_dir else path.startswith(base):
            return True
    return bool(re.search(r"/\.claude/settings[^/]*$", path))

# 読み取り禁止の秘密情報。~/.aws は許可リスト方式（config と MCP ログ以外は不可）。
AWS_DIR = os.path.join(HOME, ".aws")
AWS_READABLE_FILES = (os.path.join(AWS_DIR, "config"),)
AWS_READABLE_DIRS = (os.path.join(AWS_DIR, "aws-api-mcp"),)
SECRET_FILES = (os.path.join(HOME, ".codex/auth.json"), os.path.join(HOME, ".claude/.credentials.json"))
# 文字列中（python -c 等）に現れる秘密情報パスの補助検出
SECRET_WORDS = re.compile(r"\.aws/(credentials|\.credentials|cli/cache|sso/cache|login/cache)"
                          r"|\.codex/auth\.json|\.claude/\.credentials")

# 後続コマンドを実行するラッパー: (値を取るオプション, オプション後に読み飛ばす位置引数の数)
WRAPPERS = {
    "sudo": ({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user", "--group",
              "--chdir", "--close-from", "--host", "--prompt", "--role", "--type", "--other-user",
              "--command-timeout"}, 0),
    "doas": ({"-u", "-C"}, 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1),
    "caffeinate": ({"-t", "-w"}, 0),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "arch": ({"-arch"}, 0),
    "exec": ({"-a"}, 0),
    "time": (set(), 0), "nohup": (set(), 0), "command": (set(), 0), "builtin": (set(), 0),
}

# 再帰読み取り: 値を取るオプション（パターン・出力先などパス以外の値を読み飛ばす）
SEARCH_VALUE_OPTS = {
    "grep": {"-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "-B", "-C", "--context",
             "--after-context", "--before-context", "-d", "--directories", "-D", "--devices",
             "--include", "--exclude", "--exclude-dir", "--label"},
    "rg": {"-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "-t", "--type", "-T",
           "--type-not", "--type-add", "-m", "--max-count", "-A", "-B", "-C", "--context",
           "--after-context", "--before-context", "-j", "--threads", "-d", "--max-depth", "-E",
           "--encoding", "-M", "--max-columns", "-r", "--replace", "--pre", "--pre-glob",
           "--ignore-file", "--sort", "--sortr", "--max-filesize", "--engine", "--colors",
           "--color", "--path-separator", "--context-separator", "--field-match-separator",
           "--field-context-separator"},
    "ag": {"-G", "--file-search-regex", "--ignore", "--ignore-dir", "-A", "-B", "-C",
           "--context", "-m", "--max-count", "--depth", "-p", "--path-to-ignore", "-g"},
}
SEARCH_VALUE_OPTS["egrep"] = SEARCH_VALUE_OPTS["fgrep"] = SEARCH_VALUE_OPTS["grep"]
COPY_VALUE_OPTS = {
    "tar": {"-f", "--file", "-C", "--directory", "-T", "--files-from", "-X", "--exclude-from",
            "--exclude", "-b", "--block-size"},
    "rsync": {"-e", "--rsh", "--exclude", "--include", "--filter", "--exclude-from",
              "--include-from", "--files-from", "-T", "--temp-dir", "--backup-dir", "--link-dest",
              "--compare-dest", "--copy-dest", "--log-file", "--password-file", "--chmod"},
    "cp": set(), "zip": set(), "ditto": set(),
}
GLOB_LIMIT = 2000
GLOB_SECONDS = 1.0
CHDIR = "__CHDIR__"  # strip_prefix が実行ディレクトリ変更ラッパーを検出した印

# AWS 参照系オペレーションの判定
READ_OP_PREFIXES = ("describe-", "get-", "list-", "batch-get-", "head-", "lookup-",
                    "search-", "filter-", "scan", "query", "tail", "wait", "help",
                    "validate-", "estimate-", "preview-", "check-", "test-dns-answer")
READ_OP_DENY = {"get-login-password", "get-authorization-token", "get-secret-value",
               "batch-get-secret-value", "get-session-token", "get-federation-token",
               "get-role-credentials", "get-credentials", "get-password-data"}
S3_READ = {"ls", "presign"}
LOGS_READ = {"start-query", "stop-query"}
AWS_GLOBAL_FLAGS = {"--debug", "--no-verify-ssl", "--no-paginate", "--no-sign-request",
                    "--no-cli-pager", "--no-cli-auto-prompt", "--cli-auto-prompt"}

DEPLOY_PATTERNS = [
    r"\bcdk\s+(deploy|destroy|bootstrap|import|watch)\b",
    r"\bsam\s+(deploy|delete|sync)\b",
    r"\bterraform\s+(apply|destroy|import)\b",
    r"\bserverless\s+(deploy|remove)\b",
    r"\b(npm|yarn|pnpm)\s+(run\s+)?[\w:.-]*(deploy|destroy)\b",
    r"\bgreengrass-cli\b.*\bdeployment\s+create\b",
]
# 認証情報の差し替え・回避
CRED_BYPASS = [
    r"\bAWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)\s*=",
    r"\bAWS_(SHARED_CREDENTIALS_FILE|CONFIG_FILE|WEB_IDENTITY_TOKEN_FILE|ROLE_ARN)\s*=",
]
# プロファイル固定（aws.enabled のときだけ判定）
PROFILE_BYPASS = [
    r"\bunset\s+[^;&|]*\bAWS_(PROFILE|DEFAULT_PROFILE)\b",
    r"\bexport\s+AWS_(PROFILE|DEFAULT_PROFILE)\s*=",
]

INLINE_DELETE = [
    r"\bos\.(remove|unlink|rmdir|removedirs)\s*\(",
    r"\bshutil\.rmtree\s*\(",
    r"\.unlink\s*\(",
    r"\bfs\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\s*\(",
    r"\bfind\b[^;&|]*\s-delete\b",
    r"\bfind\b[^;&|]*-(exec|execdir|ok|okdir)\b[^;&|]*\b(rm|unlink|rmdir|shred)\b",
    r"\bperl\b.*\b(unlink|rmtree|remove_tree)\b",
    r"\bxargs\b[^;&|]*\b(rm|unlink)\b",
    r"\bgit\s+(clean|rm)\b",
    r"\brsync\b[^;&|]*--delete",
]


def out(decision, reason):
    # 出力途中の期限切れで JSON が二重にならないよう、判定確定時点でタイマーを止める。
    signal.alarm(0)
    # Codex は ask をサポートしない。失敗扱いで実行されることを防ぐ。
    if RUNTIME == "codex" and decision == "ask":
        decision = "deny"
        reason += "; Codex hook의 승인 요청은 미지원입니다. 필요한 작업은 사용자가 직접 실행하세요."
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


def split_segments(cmd):
    # ; && || | 改行でコマンドを分割（クォート内は簡易的に無視しない＝安全側）
    return [s.strip() for s in re.split(r"&&|\|\||[;|\n]|\$\(|`", cmd) if s.strip()]


def tokens(seg):
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def strip_prefix(toks):
    """先頭の VAR=val / env / WRAPPERS（timeout・nice・sudo 等）を除去し、代入を返す"""
    assigns = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        name = os.path.basename(t)  # /usr/bin/env・/usr/bin/nice 等の絶対パス指定も同一視する
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            k, v = t.split("=", 1)
            assigns[k] = v
        elif name == "env":
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                if re.match(r"^(-C|--chdir)", toks[i]):
                    assigns[CHDIR] = True
                if toks[i] in ("-i", "--ignore-environment"):
                    assigns["AWS_PROFILE"] = None
                if toks[i].startswith("--unset="):
                    assigns[toks[i].split("=", 1)[1]] = None
                if toks[i] in ("-u", "--unset") and i + 1 < len(toks):
                    assigns[toks[i + 1]] = None  # unset
                    i += 1
                i += 1
            continue
        elif name in WRAPPERS:
            values, positional = WRAPPERS[name]
            j = i + 1
            while j < len(toks) and toks[j].startswith("-") and toks[j] != "-":
                if toks[j] == "--":
                    j += 1
                    break
                # command -v/-V は検索のみで実行しない
                if name == "command" and toks[j] in ("-v", "-V"):
                    return assigns, toks[i:]
                # sudo -D/--chdir は後続コマンドの実行ディレクトリを変える
                if name == "sudo" and re.match(r"^(-D|--chdir)", toks[j]):
                    assigns[CHDIR] = True
                j += 2 if toks[j] in values else 1
            i = j + positional
            continue
        else:
            break
        i += 1
    return assigns, toks[i:]


def is_temp(path):
    p = path.strip("'\"")
    if ".." in p:
        return False
    p = os.path.expandvars(os.path.expanduser(p))
    if "$" in p or not os.path.isabs(p):
        return False
    glob_at = next((i for i, c in enumerate(p) if c in "*?[]"), None)
    if glob_at is not None:
        # ワイルドカードは最終要素のみ許可（途中のリンク経由で一時領域外へ出ないよう親を検査）
        if "/" in p[glob_at:]:
            return False
        parent = os.path.realpath(p[:p.rfind("/", 0, glob_at)] or "/")
        p = parent + "/x"
    else:
        p = os.path.realpath(p)
    roots = ["/private/tmp", "/tmp", os.environ.get("TMPDIR", "")]
    return any(root and p.startswith(os.path.realpath(root).rstrip("/") + "/")
               for root in roots)


def _secret_roots():
    """字句パスと実体パスの両方で保護対象を判定する（保護パス自体がリンクの場合も含む）"""
    real_aws = os.path.realpath(AWS_DIR)
    roots = [(AWS_DIR, SECRET_FILES)]
    real_files = tuple(os.path.realpath(f) for f in SECRET_FILES)
    if real_aws != AWS_DIR or real_files != SECRET_FILES:
        roots.append((real_aws, real_files))
    return roots


def _is_secret_in(path, aws_dir, files):
    if path == aws_dir or path in files:
        return True
    if path.startswith(aws_dir + "/"):
        rel = path[len(aws_dir) + 1:]
        return not (rel in ("config", "aws-api-mcp") or rel.startswith("aws-api-mcp/"))
    return False


def is_secret(path):
    """パスが秘密情報、または ~/.aws 自体（再帰読み取りの起点）なら True"""
    return any(_is_secret_in(path, aws, files) for aws, files in _secret_roots())


def contains_secret(path):
    """秘密情報そのもの、または ~/.aws・既存の秘密ファイルを配下に含むディレクトリなら True"""
    if is_secret(path):
        return True
    prefix = path.rstrip("/") + "/"
    return any(t.startswith(prefix) for aws, files in _secret_roots()
               for t in [aws] + [f for f in files if os.path.lexists(f)])


def brace_expand(s, limit=64):
    """入れ子なしの {a,b} を展開する（シェル展開の近似）。上限超過は切り捨てず None"""
    m = re.search(r"\{([^{}]*,[^{}]*)\}", s)
    if not m:
        return [s]
    out = []
    for part in m.group(1).split(","):
        sub = brace_expand(s[:m.start()] + part + s[m.end():], limit)
        if sub is None:
            return None
        out += sub
        if len(out) > limit:
            return None
    return out


def path_variants(value, cwd):
    """引数から字句パスと実体パスを列挙する（ブレース・ワイルドカード展開を含む）。展開数・時間超過は None"""
    variants = brace_expand(value)
    if variants is None:
        return None
    deadline = time.monotonic() + GLOB_SECONDS
    paths = []
    for s in variants:
        lexical = os.path.normpath(os.path.join(cwd, os.path.expandvars(os.path.expanduser(s))))
        candidates = [lexical]
        if any(c in lexical for c in "*?["):
            for n, match in enumerate(glob.iglob(lexical)):
                if n >= GLOB_LIMIT or time.monotonic() > deadline:
                    return None
                candidates.append(match)
        for c in candidates:
            paths += [os.path.normpath(c), os.path.realpath(c)]
    return paths


def secret_access(value, cwd, check):
    paths = path_variants(value, cwd)
    if paths is None:
        return ("ask", "ワイルドカードの展開対象が多すぎるため確認が必要")
    if any(check(p) for p in paths):
        return ("deny", "認証情報ファイルへのアクセスは禁止")
    return None


def is_recursive_reader(prog, argv):
    if prog in ("rg", "ag", "tar", "zip", "rsync", "ditto"):
        return True
    if prog in ("grep", "egrep", "fgrep") and ("--directories=recurse" in argv or any(
            argv[k] in ("-d", "--directories") and argv[k + 1] == "recurse" for k in range(1, len(argv) - 1))):
        return True
    return prog in ("grep", "egrep", "fgrep", "cp") and any(
        re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*|--recursive|--dereference-recursive", a) for a in argv[1:])


def recursive_sources(prog, argv):
    """再帰読み取りの (読み取り元, パターンファイル)。
    パターン・出力先・オプション値を除き、読み取り元の省略時は cwd（"."）"""
    search = prog in SEARCH_VALUE_OPTS
    values = SEARCH_VALUE_OPTS.get(prog) or COPY_VALUE_OPTS.get(prog, set())
    positional, pattern_files, explicit_pattern, k = [], [], False, 1
    while k < len(argv):
        a = argv[k]
        k += 1
        if a == "--":
            positional += argv[k:]
            break
        if a.startswith("--"):
            opt, attached = (a.split("=", 1) + [None])[:2]
            value = attached
            if attached is None and opt in values:
                value = argv[k] if k < len(argv) else ""
                k += 1
            if search and opt in ("--regexp", "--file"):
                explicit_pattern = True
                if opt == "--file":
                    pattern_files.append(value or "")
            continue
        if a.startswith("-") and a != "-":
            # 短縮オプションのまとめ書き（-rnf FILE・-etoken・-f/path）
            for n, c in enumerate(a[1:], 2):
                if "-" + c not in values:
                    continue
                value = a[n:]
                if not value:
                    value = argv[k] if k < len(argv) else ""
                    k += 1
                if search and c in "ef":
                    explicit_pattern = True
                    if c == "f":
                        pattern_files.append(value)
                break
            continue
        positional.append(a)
    if search:
        # 明示パターン（-e/-f）がなければ最初の位置引数はパターン
        if not explicit_pattern:
            positional = positional[1:]
        return positional or ["."], pattern_files
    if prog in ("cp", "rsync", "ditto"):
        return positional[:-1], pattern_files
    return positional, pattern_files


def find_in_temp(cmd):
    """find を含む全セグメントについて、起点がすべて一時領域配下なら True"""
    found = False
    for seg in split_segments(cmd):
        _, argv = strip_prefix(tokens(seg))
        if not argv or os.path.basename(argv[0]) != "find":
            continue
        found = True
        opts = argv[1:]
        if any(o in ("-L", "-H", "-follow") for o in opts):
            return False
        roots = []
        for o in opts:
            if o.startswith("-") or o in ("(", "!", ")"):
                break
            roots.append(o)
        if not roots or not all(is_temp(r.rstrip("/") + "/x") for r in roots):
            return False
    return found



# Python キャッシュ以外を含むディレクトリやリンクは例外にしない。
CACHE_SCAN_LIMIT = 20000
CACHE_SCAN_SECONDS = 1.0


def _cache_path(path, cwd):
    if not isinstance(path, str) or not path or '..' in path.split(os.path.sep) or any(c in path for c in '$*?[]`\n\r'):
        return None
    p = os.path.sep + os.path.abspath(os.path.join(cwd or os.getcwd(), os.path.expanduser(path))).lstrip(os.path.sep)
    # macOS の標準一時パス別名だけを正規化する。
    for alias, target in (('/tmp', '/private/tmp'), ('/var', '/private/var')):
        if (p == alias or p.startswith(alias + '/')) and os.path.islink(alias) and os.path.realpath(alias) == target:
            p = target + p[len(alias):]
    current = os.path.sep
    try:
        for part in p.split(os.path.sep)[1:]:
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return None
    except OSError:
        return None
    aws = os.path.join(HOME, '.aws')
    if p == aws or p.startswith(aws + os.path.sep):
        return None
    return p


def _cache_tick(budget):
    budget[0] += 1
    if budget[0] > CACHE_SCAN_LIMIT or time.monotonic() > budget[1]:
        raise OSError('cache scan limit')


def _cache_directory(path, budget):
    if os.path.basename(path) != '__pycache__' or not stat.S_ISDIR(os.lstat(path).st_mode):
        return False
    with os.scandir(path) as entries:
        for entry in entries:
            _cache_tick(budget)
            if not entry.name.endswith('.pyc') or not entry.is_file(follow_symlinks=False):
                return False
    return True


def is_python_cache(path, cwd=None):
    """実在する安全なキャッシュディレクトリまたは直下の pyc だけを許可。"""
    p = _cache_path(path, cwd)
    if p is None:
        return False
    budget = [0, time.monotonic() + CACHE_SCAN_SECONDS]
    try:
        mode = os.lstat(p).st_mode
        if stat.S_ISDIR(mode):
            return _cache_directory(p, budget)
        return (stat.S_ISREG(mode) and p.endswith('.pyc')
                and _cache_directory(os.path.dirname(p), budget))
    except OSError:
        return False


def _cache_delete_target(path, cwd):
    if is_python_cache(path, cwd):
        return True
    # ワイルドカードは検証済みキャッシュの直下に限定する。
    parent, leaf = os.path.split(path)
    return leaf in ('*.pyc', '*') and is_python_cache(parent, cwd)


def _safe_delete_target(path, cwd, cache_allowed=True):
    if '..' in path.split(os.path.sep):
        return False
    if '$' in path:
        # 一時パスの既存変数対応は維持する。
        return is_temp(path)
    absolute = os.path.abspath(os.path.join(cwd, os.path.expanduser(path)))
    return is_temp(absolute) or (cache_allowed and _cache_delete_target(path, cwd))


def find_python_cache(argv, cwd):
    """find の削除式全体と全一致キャッシュを制限時間内で検査。"""
    if not argv or os.path.basename(argv[0]) != 'find':
        return False
    args = argv[1:]
    roots = []
    while args and not args[0].startswith('-'):
        roots.append(args[0])
        args = args[1:]
    if args[:4] not in (['-type', 'd', '-name', '__pycache__'],
                        ['-name', '__pycache__', '-type', 'd']):
        return False
    tail = args[4:]
    if tail[:1] == ['-prune']:
        tail = tail[1:]
    if tail not in (['-exec', 'rm', '-rf', '{}', '+'],
                    ['-exec', 'rm', '-r', '-f', '{}', '+']):
        return False
    if not roots:
        return False
    budget = [0, time.monotonic() + CACHE_SCAN_SECONDS]
    try:
        stack = []
        for root in roots:
            p = _cache_path(root, cwd)
            if p is None or not stat.S_ISDIR(os.lstat(p).st_mode):
                return False
            stack.append(p)
        while stack:
            p = stack.pop()
            _cache_tick(budget)
            if os.path.basename(p) == '__pycache__':
                if _cache_path(p, cwd) is None or not _cache_directory(p, budget):
                    return False
                continue
            with os.scandir(p) as entries:
                for entry in entries:
                    _cache_tick(budget)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        return True
    except OSError:
        return False


# ---------------- AWS ----------------
def check_aws_argv(argv, assigns, source):
    """argv[0] == 'aws'。問題があれば (decision, reason) を返す"""
    profile = None
    pos = []
    i = 1
    while i < len(argv):
        t = argv[i]
        if t.startswith("--"):
            if "=" in t:
                k, v = t.split("=", 1)
                if k == "--profile":
                    profile = v
            elif t in AWS_GLOBAL_FLAGS:
                pass
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                if t == "--profile":
                    profile = argv[i + 1]
                if len(pos) < 2 or t == "--profile":
                    i += 1  # 値をスキップ（サービス/操作特定前のみ厳密に）
                else:
                    i += 1
        elif t.startswith("-") and len(pos) >= 2:
            pass
        else:
            pos.append(t)
        i += 1

    env_profile = assigns.get("AWS_PROFILE", assigns.get("AWS_DEFAULT_PROFILE", ""))
    # プロファイル固定は aws.enabled のときだけ。参照系以外の禁止は常に下で判定する。
    if AWS_ENABLED:
        if "AWS_PROFILE" in assigns and env_profile is None:
            return ("deny", f"[{source}] AWS_PROFILE の解除は禁止（{ALLOWED_PROFILE} 固定）")
        if profile and profile != ALLOWED_PROFILE:
            return ("deny", f"[{source}] --profile {profile} は禁止。AI は {ALLOWED_PROFILE}（read-only ロール）のみ使用可")
        if env_profile and env_profile != ALLOWED_PROFILE:
            return ("deny", f"[{source}] AWS_PROFILE={env_profile} は禁止。{ALLOWED_PROFILE} のみ使用可")

    if not pos or pos[0] in ("help", "--version"):
        return None
    svc = pos[0]
    op = pos[1] if len(pos) > 1 else ""
    if svc == "configure":
        if op == "get" and any(p in {"region", "output"} for p in pos[2:]):
            return None
        if op in ("list", "list-profiles"):
            return None
        return ("deny", f"[{source}] aws configure による認証設定の変更は禁止")
    if svc == "s3":
        if op in S3_READ:
            return None
        if op in ("cp", "sync"):
            args = [p for p in pos[2:]]
            # S3 → ローカルのダウンロードのみ許可（宛先が s3:// でない）
            if len(args) >= 2 and args[0].startswith("s3://") and not args[-1].startswith("s3://"):
                if "--delete" in argv:
                    return ("ask", "S3 sync --delete はローカルファイルを削除する")
                protected = check_edit(args[-1])
                if protected:
                    return protected
                return None
        return ("deny", f"[{source}] aws s3 {op} は AWS 側の変更になるため禁止。必要ならユーザーに実行を依頼すること")
    if svc == "logs" and op in LOGS_READ:
        return None
    if svc == "sts" and op == "get-caller-identity":
        return None
    if op in READ_OP_DENY:
        return ("deny", f"[{source}] aws {svc} {op} は認証情報を出力するため禁止")
    if op.startswith(READ_OP_PREFIXES):
        return None
    return ("deny", f"[{source}] aws {svc} {op} は参照系以外の操作のため禁止（Claude は read-only 運用）。"
                    "変更が必要ならコマンドを提示してユーザーに実行を依頼すること")


INTERPRETERS = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|sh|bash|zsh|dash|ksh|source|\.)$")


def _real(value, cwd):
    value = re.sub(r"^\d*[<>]+", "", value)
    return os.path.realpath(os.path.join(cwd, os.path.expandvars(os.path.expanduser(value))))


def _executed(argv):
    """セグメントで実行されるスクリプトのパスと、インライン実行コード（-c・-m の引数）を返す。"""
    if not argv:
        return None, None
    prog = os.path.basename(argv[0])
    if not INTERPRETERS.match(prog):
        return argv[0], None
    k = 1
    while k < len(argv) and argv[k].startswith("-") and argv[k] != "-":
        if argv[k] == "-m" or (prog not in ("source", ".") and re.fullmatch(r"-[A-Za-z]*c", argv[k])):
            return None, " ".join(argv[k + 1:])
        k += 1
    return (argv[k] if k < len(argv) else None), None


def _assume_role_targets():
    paths = [ASSUME_ROLE_CMD] if ASSUME_ROLE_CMD else []
    if HARNESS_HOME:
        paths += [os.path.join(HARNESS_HOME, "tools/aws", n) for n in ("assume_role.sh", "assume_role.py")]
    return {os.path.realpath(p) for p in paths}


def _assume_role_exact(cmd, cwd):
    """`<設定のスクリプト> --awsEnv <登録環境>` の単独実行（MFA を echo/printf から渡すパイプのみ可）か。
    変数代入・env/export・ラッパー・置換・リダイレクト・他コマンドとの連結は認めない。"""
    if not (AWS_ENABLED and ASSUME_ROLE_CMD) or any(c in cmd for c in ";&`\n\r$()<>"):
        return False
    parts = cmd.split("|")
    if len(parts) > 2:
        return False
    if len(parts) == 2:
        feed = tokens(parts[0])
        if not (len(feed) >= 2 and re.fullmatch(r"[0-9]{6}", feed[-1])
                and feed[:-1] in (["echo"], ["printf", "%s\\n"])):
            return False
    argv = tokens(parts[-1])
    return (len(argv) == 3 and _real(argv[0], cwd) == os.path.realpath(ASSUME_ROLE_CMD)
            and argv[1] == "--awsEnv" and argv[2] in ASSUME_ROLE_ENVS)


def check_harness_exec(cmd, cwd):
    """インストーラ適用と認証更新スクリプトの実行判定。"allow"（認証更新の正規形）・(decision, reason)・None。"""
    segments = [strip_prefix(tokens(seg))[1] for seg in split_segments(cmd)]
    installer = os.path.realpath(os.path.join(HARNESS_HOME, "install/install.py")) if HARNESS_HOME else None
    # 適用はガード設定（hook sidecar）を書き換えるため、経路・ラッパー・インライン実行に関係なく承認を求める。
    # argparse の省略形（--ap 等）も含め、"apply" を含む・--a で始まる引数があれば適用とみなす。
    runs_installer = "install.py" in cmd or any(
        installer and t and not t.startswith("-") and _real(t, cwd) == installer for argv in segments for t in argv)
    if runs_installer and ("apply" in cmd.lower() or any(t.startswith("--a") for argv in segments for t in argv)):
        return ("ask", "ハーネスのインストーラ適用（--apply）はガード設定を変更するため確認が必要")
    targets = _assume_role_targets()
    names = {os.path.basename(p) for p in targets} | {"assume_role.py"}
    hit = False
    for argv in segments:
        script, inline = _executed(argv)
        if inline is not None:
            hit = hit or any(n in inline for n in names)
        elif script:
            hit = hit or _real(script, cwd) in targets or os.path.basename(script) in names
    if not hit:
        return None
    if _assume_role_exact(cmd, cwd):
        return "allow"
    return ("ask", "認証更新スクリプトは登録済み環境名での単独実行のみ許可（変数代入・ラッパー・他コマンド併用は確認が必要）")


# Bash の書き込み判定。リダイレクトと、対象ファイルを書き換えるコマンド。
REDIRECT_RE = r">\s*[^&\s]|>&\s*[^\d\s&-]"
WRITE_CMD_RE = re.compile(r"\btee\b|\bsed\b[^;&|]*\s(-[A-Za-z]*i|--in-place)|\bcp\b|\bmv\b|\bchmod\b|\bln\b|"
                          r"\bperl\b[^;&|]*\s-[A-Za-z]*i|\bpython3?\b.*(open|write|dump)|\btruncate\b")
WRITE_RE = re.compile(REDIRECT_RE + "|" + WRITE_CMD_RE.pattern)
# hook 入力に cwd がないとき、相対パスの書き込み対象がこの名前（パス要素単位）を含めば承認対象にする
FALLBACK_PROTECTED_NAMES = ("hooks/", "install/", "settings/", "tools/aws/", "config.local.json", ".githooks",
                            "privacy_check.py", "harness_setup.py", "setup.sh")
# cd 先として字句的に確定できる引数（変数・置換・クォート・ワイルドカードを含まない）
CD_TARGET_RE = re.compile(r"[\w./~+@%,:=-]+")
# サブシェル・グループ・制御構文の先頭語（直後の cd を検出するため読み飛ばす）
SHELL_LEADERS = {"(", "{", "!", "if", "then", "else", "elif", "do", "while", "until"}
WRITE_CWD_LIMIT = 32


class WriteState:
    """Bash 書き込み判定用の実行ディレクトリ候補。
    サブシェル・pushd/popd を正確に追わず、cd 先を候補に加え続ける（どの候補で解釈しても保護対象なら承認）。
    nobase: hook 入力に cwd がない（相対パスは保護名で判定）。unknown: cd 先が確定できない（相対パスの書き込みは承認）。"""

    def __init__(self, dirs=(), nobase=False):
        self.dirs = list(dict.fromkeys(os.path.normpath(d) for d in dirs))
        self.nobase = nobase or not self.dirs
        self.unknown = False
        self.pushed = False
        self.pending_redirect = False
        self.cache = {}  # (語, 基準ディレクトリ) → 判定。複製間で共有する

    def copy(self):
        other = WriteState()
        other.__dict__.update(self.__dict__, dirs=list(self.dirs))
        return other


def _shell_words(seg):
    """リダイレクト演算子を独立した語にして分割する（クォート内の > は語の一部のまま）。失敗時は None。"""
    try:
        lexer = shlex.shlex(seg, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


def _update_write_cwd(state, seg, dynamic_cd):
    """cd・pushd・popd を実行ディレクトリ候補に反映する（eval 内の cd は scan_writes が同じ候補で走査する）。"""
    try:
        toks = shlex.split(seg)
    except ValueError:
        # クォートがセグメントをまたぐ断片（sh -c 'cd x; …' の先頭など）はクォート記号を外して読む
        toks = [t.strip("'\"") for t in seg.split()]
    while toks and (toks[0] in SHELL_LEADERS or toks[0].startswith("(")):
        if toks[0] != "(" and toks[0].startswith("("):
            toks[0] = toks[0].lstrip("(")
        else:
            toks.pop(0)
    _, argv = strip_prefix(toks)
    if not argv:
        return
    prog = os.path.basename(argv[0])
    if prog == "popd":
        # このコマンド内の pushd 以前の位置はすでに候補にある。それ以外はディレクトリスタック次第
        if not state.pushed:
            state.unknown = True
        return
    if prog not in ("cd", "pushd"):
        return
    args = argv[1:]
    while prog == "cd" and args and args[0] in ("-P", "-L", "-e", "-@"):
        args = args[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    # 引数なし（HOME・ディレクトリスタック）・cd -・置換を含む cd は移動先を確定できない
    if dynamic_cd or len(args) != 1 or args[0].startswith("-") or not CD_TARGET_RE.fullmatch(args[0]):
        state.unknown = True
        return
    state.pushed = state.pushed or prog == "pushd"
    target = os.path.expanduser(args[0])
    if target.startswith("~"):
        state.unknown = True
        return
    # 追加中のリストを走査しないよう複製を使う
    bases = [""] if os.path.isabs(target) else list(state.dirs)
    for base in bases:
        joined = os.path.join(base, target)
        for d in (os.path.normpath(joined), os.path.realpath(joined)):
            if d not in state.dirs:
                state.dirs.append(d)
    if len(state.dirs) > WRITE_CWD_LIMIT:
        state.unknown = True


def _write_candidates(seg, state):
    """書き込み系セグメントの対象候補文字列と、先頭のコマンド名を返す。書き込み系でなければ None。"""
    clean = re.sub(r"\d*>>?\s*/dev/null\b|&>>?\s*/dev/null\b|\d*>&\d+", " ", seg)
    pending = state.pending_redirect
    # `>|` はセグメント分割で切れるため、次のセグメントの先頭語をリダイレクト先として扱う
    state.pending_redirect = clean.rstrip().endswith(">")
    command_write = WRITE_CMD_RE.search(clean)
    if not (pending or command_write or re.search(REDIRECT_RE, clean)):
        return None
    words = _shell_words(clean)
    _, argv = strip_prefix(tokens(clean))
    # `>|` の続きのセグメントは先頭語がリダイレクト先なのでコマンド名として除外しない
    name = argv[0] if argv and not pending else None
    if words is None:
        # 字句解析できない場合は演算子・クォート記号で区切ったすべての語を対象にする（安全側）
        parts = re.split(r"[<>&|;()'\"\s]+", clean)
        return [p for p in parts if p], name
    found = [words[0]] if pending and words else []
    skip = False
    for n, w in enumerate(words):
        if skip:
            skip = False
            continue
        operator = bool(w) and all(c in "();<>|&" for c in w)
        if operator:
            # 入力リダイレクト・heredoc の語は読み取り元なので除き、出力リダイレクトの語は対象にする
            if "<" in w and ">" not in w:
                skip = True
            elif ">" in w and n + 1 < len(words):
                found.append(words[n + 1])
                skip = True
            continue
        if command_write:
            found.append(w)
            # python -c・perl -e 等のコード中の文字列リテラルもパスとして扱う
            found += [a or b for a, b in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", w)]
    out = []
    for w in found:
        for v in (w, w.split("=", 1)[-1]):
            if v and not v.startswith("-") and v not in out:
                out.append(v)
    return out, name


def check_protected_write(seg, state):
    """セグメントの書き込み対象を実行ディレクトリ候補で絶対パス・実体パスにし、保護パスと比較する。"""
    result = _write_candidates(seg, state)
    if not result:
        return None
    candidates, name = result
    for value in candidates:
        expanded = os.path.expandvars(os.path.expanduser(value))
        if os.path.isabs(expanded):
            bases = ["/"]
        else:
            if value == name:
                continue
            if state.unknown:
                return ("ask", f"実行ディレクトリが確定できないため相対パスの書き込みは確認が必要: {seg[:200]}")
            if state.nobase:
                padded = "/" + os.path.normpath(expanded) + "/"
                if any("/" + n.rstrip("/") + "/" in padded for n in FALLBACK_PROTECTED_NAMES):
                    return ("ask", f"ガード設定/認証関連ファイルの変更を含む可能性: {value[:200]}")
            bases = state.dirs
        for base in bases:
            key = (value, base)
            if key not in state.cache:
                # heredoc 等で同じ語が繰り返されるため、実体パス解決の結果をコマンド内で再利用する
                paths = path_variants(value, base)
                state.cache[key] = ("ask", "ワイルドカードの展開対象が多すぎるため確認が必要") if paths is None else next(
                    (("ask", f"ガード設定/認証関連ファイルの変更: {p}") for p in paths if is_protected_write(p)), None)
            if state.cache[key]:
                return state.cache[key]
    return None


def _logical_segments(cmd):
    """split_segments の断片を、クォートが閉じるまで連結した論理セグメントにする。
    (連結後の文字列, 構成断片) を返す。クォート内の ; | && で切れた sh -c・python -c を一体で読むため。"""
    out, buf, quote = [], [], None
    for piece in split_segments(cmd):
        buf.append(piece)
        # クォート状態を断片ごとに追跡する（連結文字列の再解析を繰り返さず線形時間に保つ）
        k = 0
        while k < len(piece):
            c = piece[k]
            if quote == "'":
                quote = None if c == "'" else quote
            elif c == "\\":
                k += 1
            elif quote == '"':
                quote = None if c == '"' else quote
            elif c in "'\"":
                quote = c
            k += 1
        if quote is None:
            out.append((" ; ".join(buf), buf))
            buf = []
    if buf:
        out.append((" ; ".join(buf), buf))
    return out


HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([A-Za-z0-9_.-]+)'|\"([A-Za-z0-9_.-]+)\"|([A-Za-z0-9_.-]+))")


def strip_literal_heredocs(cmd):
    """区切りをクォートした heredoc（<<'EOF'）の本文を除く。本文は展開・実行されないデータのため。
    クォート外の << だけを対象にし（$(…) 内はクォート文脈を戻す）、区切りが未クォートの本文は残す。"""
    lines = cmd.split("\n")
    out, i, quote, stack = [], 0, None, []
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        found, k = [], 0
        while k < len(line):
            c = line[k]
            if quote == "'":
                quote = None if c == "'" else quote
            elif c == "\\":
                k += 1
            elif line.startswith("$(", k):
                stack.append(quote)
                quote = None
                k += 1
            elif quote == '"':
                quote = None if c == '"' else quote
            elif c in "'\"":
                quote = c
            elif c == "(":
                stack.append(None)
            elif c == ")" and stack:
                quote = stack.pop()
            elif line.startswith("<<", k) and not line.startswith("<<<", k):
                m = HEREDOC_RE.match(line, k)
                if m:
                    found.append((m.group(1) == "-", m.group(2) or m.group(3), m.group(4)))
                    k = m.end()
                    continue
            k += 1
        for tabs, literal, plain in found:
            delim = literal or plain
            start = i
            while i < len(lines) and (lines[i].lstrip("\t") if tabs else lines[i]) != delim:
                i += 1
            if not literal:
                out.extend(lines[start:i])
            if i < len(lines):
                out.append(lines[i])
                i += 1
    return "\n".join(out)


def scan_writes(cmd, state, depth=0):
    """コマンド全体の書き込み対象を cd・sh -c・eval を反映した実行ディレクトリ候補で判定する。最初の該当を返す。"""
    if depth > 4:
        return ("ask", "シェルの入れ子が深すぎるため確認が必要")
    cmd = strip_literal_heredocs(cmd)
    # 置換（$(…)・`…`・変数）を含む cd はセグメント分割で引数が欠けるため、コマンド内の cd 全体を確定不可にする
    dynamic_cd = bool(re.search(r"(^|[\s;&|({!])(cd|pushd)\s[^;&|\n]*[$`]", cmd))
    for joined, pieces in _logical_segments(cmd):
        # クォートをまたぐ連結は、断片単位（$(…) 内の cd・書き込み）と連結全体の両方で判定する
        for seg in (pieces + [joined] if len(pieces) > 1 else [joined]):
            r = check_protected_write(seg, state)
            if r:
                return r
            _update_write_cwd(state, seg, dynamic_cd)
        try:
            toks = shlex.split(joined)
        except ValueError:
            continue
        while toks and toks[0] in SHELL_LEADERS:
            toks.pop(0)
        _, argv = strip_prefix(toks)
        prog = os.path.basename(argv[0]) if argv else ""
        if prog in ("sh", "bash", "zsh", "dash", "ksh"):
            ci = next((i for i, a in enumerate(argv[1:], 1)
                       if a.startswith("-") and "c" in a[1:] and not a.startswith("--")), None)
            if ci is not None and ci + 1 < len(argv):
                # 子シェルの cd は呼び出し側に影響しないため複製で走査する
                r = scan_writes(argv[ci + 1], state.copy(), depth + 1)
                if r:
                    return r
        elif prog == "eval":
            # eval は同じシェルで再解釈されるため、cd も含めて同じ候補で走査する
            r = scan_writes(" ".join(argv[1:]), state, depth + 1)
            if r:
                return r
    return None


def check_bash(cmd, depth=0, cwd=None, write_state=None):
    decisions = []
    seen_find = False
    safe_finds = True
    cwd_known = True
    # 書き込み判定の実行ディレクトリ候補。cwd が渡されなければ（hook 入力に cwd なし）保護名で判定する。
    # 入れ子（sh -c・eval）は最上位の scan_writes が cd を反映して走査済み。結果は他の判定（deny を含む）の後に返す
    write_hit = None
    if depth == 0:
        write_hit = scan_writes(cmd, write_state or WriteState([cwd] if cwd else []))
    cwd = cwd or os.getcwd()
    if depth > 4:
        return ("ask", "シェルの入れ子が深すぎるため確認が必要")

    # 認証情報ファイルの読み取り禁止
    if SECRET_WORDS.search(cmd):
        return ("deny", "認証情報ファイルへのアクセスは禁止")
    if re.search(r"\baws\b.*--with-decryption\b", cmd):
        return ("deny", "復号した秘密情報の取得は禁止")

    for pat in CRED_BYPASS + (PROFILE_BYPASS if AWS_ENABLED else []):
        if re.search(pat, cmd):
            return ("deny", "AWS 認証情報/プロファイルの差し替えは禁止（AI は設定したプロファイル固定）")
    for pat in DEPLOY_PATTERNS:
        if re.search(pat, cmd):
            return ("deny", "AWS へのデプロイ/削除系コマンドは禁止。デプロイはユーザーが実行する")
    r = check_harness_exec(cmd, cwd)
    if r == "allow":
        return None
    if r:
        return r

    for seg in split_segments(cmd):
        assigns, argv = strip_prefix(tokens(seg))
        if AWS_ENABLED and any(k in assigns and assigns[k] != ALLOWED_PROFILE
                               for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")):
            return ("deny", f"AWS_PROFILE の解除・差し替えは禁止（{ALLOWED_PROFILE} 固定）")
        # sudo -D・env -C で実行ディレクトリが変わると相対パス・暗黙 cwd を正しく判定できない
        if assigns.pop(CHDIR, False) and argv:
            return ("ask", "実行ディレクトリを変更するラッパーのため確認が必要")
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        # 相対パス・./・シンボリックリンク経由の秘密ファイル読み取りも遮断する。
        recursive = is_recursive_reader(prog, argv)
        for arg in argv[1:]:
            arg = re.sub(r"^\d*[<>]+", "", arg)
            # --opt=path・file://path 形式の値も検査する
            for value in {arg, arg.split("=", 1)[-1]}:
                value = re.sub(r"^(file|fileb)://", "", value)
                if not value or value.startswith("-"):
                    continue
                expanded = os.path.expandvars(os.path.expanduser(value))
                if ".aws/" in expanded and any(c in expanded for c in "*?[]$"):
                    return ("deny", "曖昧な AWS 認証ファイルアクセスは禁止")
                # 再帰読み取りのパターン（例: grep -r /Users/x src）はパス扱いしない
                if recursive and prog in SEARCH_VALUE_OPTS:
                    continue
                r = secret_access(value, cwd, is_secret)
                if r:
                    return r
        # 再帰読み取り（grep -r・rg・tar 等）は秘密情報を含む上位ディレクトリ・暗黙の cwd も対象にする。
        if recursive:
            sources, pattern_files = recursive_sources(prog, argv)
            # パターンファイル（-f/--file）も読み取り対象
            for value, check in [(v, contains_secret) for v in sources] + [(v, is_secret) for v in pattern_files]:
                r = secret_access(value, cwd, check)
                if r:
                    return r
        if prog == "cd":
            if len(argv) == 2 and not argv[1].startswith("-") and not any(c in argv[1] for c in "$*?[]`\n\r") and ".." not in argv[1].split(os.path.sep):
                cwd = os.path.abspath(os.path.join(cwd, os.path.expanduser(argv[1])))
            else:
                cwd_known = False
        elif prog in ("pushd", "popd"):
            cwd_known = False

        # 認証更新スクリプトの例外は check_harness_exec（単独実行の正規形のみ）で判定済み
        if prog == "aws":
            r = check_aws_argv(argv, assigns, "Bash")
            if r:
                return r
        elif prog in ("sh", "bash", "zsh", "dash", "ksh"):
            ci = next((i for i, a in enumerate(argv[1:], 1)
                       if a.startswith("-") and "c" in a[1:] and not a.startswith("--")), None)
            if ci is not None and ci + 1 < len(argv):
                if not cwd_known:
                    return ("ask", "実行ディレクトリが不明なシェル操作")
                r = check_bash(argv[ci + 1], depth + 1, cwd)
                if r:
                    return r
            else:
                return ("ask", "シェルスクリプトの実行には内容の確認が必要")
        elif prog == "eval":
            # eval の引数はシェルで再解釈されるため入れ子として検査する
            if not cwd_known:
                return ("ask", "実行ディレクトリが不明なシェル操作")
            r = check_bash(" ".join(argv[1:]), depth + 1, cwd)
            if r:
                return r
        elif AWS_ENABLED and any(k in assigns and assigns[k] not in (None, ALLOWED_PROFILE)
                                 for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")):
            return ("deny", f"AWS_PROFILE の差し替えは禁止（{ALLOWED_PROFILE} 固定）")

        # 削除は対象ごとに判定し、混在した一般ファイルは承認対象にする。
        if prog in ("rm", "rmdir", "unlink", "shred", "trash", "srm"):
            targets = []
            options = True
            cache_options = prog in ("rm", "rmdir", "unlink")
            parent_delete = False
            for arg in argv[1:]:
                if options and arg == "--":
                    options = False
                elif options and arg.startswith("-"):
                    if prog == "rm":
                        safe_option = arg in ("--force", "--recursive", "--dir", "--verbose", "--interactive") or re.fullmatch(r"-[frRiIdv]+", arg)
                    elif prog == "rmdir":
                        safe_option = arg in ("-v", "--verbose", "--ignore-fail-on-non-empty")
                        parent_delete = parent_delete or arg == "--parents" or (not arg.startswith("--") and "p" in arg[1:])
                    else:
                        safe_option = False
                    cache_options = cache_options and bool(safe_option)
                    continue
                else:
                    targets.append(arg)
            if parent_delete or not targets or not all((cwd_known or os.path.isabs(t)) and _safe_delete_target(t, cwd, cache_options) for t in targets):
                decisions.append(("ask", f"ファイル削除: {seg[:200]}"))
        if prog == "find":
            seen_find = True
            if not (find_in_temp(seg) or (cwd_known and find_python_cache(argv, cwd))):
                safe_finds = False
                if "__pycache__" in argv and any(a in ("-exec", "-execdir", "-ok", "-okdir", "-delete") for a in argv[1:]):
                    decisions.append(("ask", f"find 操作の確認: {seg[:200]}"))

    if decisions:
        return decisions[0]

    for pat in INLINE_DELETE:
        m = re.search(pat, cmd)
        if m:
            # find は全起点が一時領域配下かつリンク追跡なしの場合のみ承認不要。
            if pat.startswith(r"\bfind") and (find_in_temp(cmd) or (seen_find and safe_finds)):
                continue
            return ("ask", f"削除を伴う可能性のある操作: {m.group(0)}")

    # 保護ファイルへの書き込み（リダイレクト・sed -i・tee・cp・mv など）をセグメント単位で判定
    for seg in split_segments(cmd):
        # /dev/null への出力捨て・fd 複製（2>/dev/null・>&2 等）は書き込みではない
        seg = re.sub(r"\d*>>?\s*/dev/null\b|&>>?\s*/dev/null\b|\d*>&\d+", " ", seg)
        if any(w in seg for w in PROTECTED_WRITE_WORDS) and WRITE_RE.search(seg):
            return ("ask", "ガード設定/認証関連ファイルの変更を含むコマンド")
    # 相対パス・cd 後の書き込み対象を実行ディレクトリ基準で解釈した判定
    return write_hit


def check_edit(path):
    # 相対パスはプロセスの cwd（main() で hook 入力の cwd に移動済み）基準で解釈する
    p = os.path.realpath(os.path.expanduser(path or ""))
    lexical = os.path.abspath(os.path.expanduser(path or ""))
    if p.startswith(os.path.join(HOME, ".aws/")):
        return ("deny", "~/.aws 配下の編集は禁止")
    # どのプロジェクトでも .claude/settings.json・settings.local.json（権限・hook 設定）は承認対象
    if any(re.search(r"/\.claude/settings[^/]*$", v) for v in (p, lexical)):
        return ("ask", f"権限設定ファイルの変更: {p}")
    # 字句パス・実体パスの両方で、保護パス（字句・実体）と比較する
    if any(is_protected_write(v) for v in (p, lexical)):
        return ("ask", f"ガード設定/認証関連ファイルの変更: {p}")
    return None


MCP_SCAN_VALUES = 500


def check_mcp_input(tool_input):
    """MCP 引数の文字列（コード・URL・パスを含む）に秘密情報パスがあれば拒否する"""
    if SECRET_WORDS.search(json.dumps(tool_input, ensure_ascii=False)):
        return ("deny", "認証情報ファイルへのアクセスは禁止")
    stack, seen = [tool_input], 0
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack += list(value.values())
        elif isinstance(value, list):
            stack += value
        elif isinstance(value, str):
            seen += 1
            if seen > MCP_SCAN_VALUES:
                return ("ask", "MCP 引数が多すぎるため確認が必要")
            # パス形式の値（file:// を含む）は実体パスでも判定する
            path = re.sub(r"^file://", "", value)
            if path.startswith(("/", "~", "$HOME")) and len(path) < 4096 and not any(c.isspace() for c in path):
                r = secret_access(path, os.getcwd(), is_secret)
                if r:
                    return r
    return None


def main():
    # 設定の欠落・破損は保護範囲が不明になるため、入力に関係なく拒否する（fail-closed）。
    if CONFIG_ERROR:
        out("deny", f"가드 설정({CONFIG_PATH})이 없거나 잘못되었습니다: {CONFIG_ERROR}. "
                    "install.py로 다시 설치하거나 사용자가 설정을 확인하세요. 작업을 실행하지 않았습니다.")
    try:
        data = json.load(sys.stdin)
    except Exception:
        out("deny", "가드 입력을 해석할 수 없습니다. 작업을 실행하지 않았습니다.")
    if not isinstance(data, dict) or not isinstance(data.get("tool_input", {}), dict):
        out("deny", "가드 입력 형식이 잘못되었습니다.")
    write_state = WriteState(nobase=True)
    if data.get("cwd"):
        os.chdir(data["cwd"])
        # Bash の相対パス書き込みは hook 入力の cwd（字句パス）と移動後の実体パスの両方で解釈する
        write_state = WriteState([os.getcwd()] + ([data["cwd"]] if os.path.isabs(data["cwd"]) else []))
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    # cross-check・prune-memory が起動した Codex は道具方針を渡す。監視の事後停止より前に実行自体を拒否する。
    policy = os.environ.get(TOOL_POLICY_ENV) if RUNTIME == "codex" else None
    if policy is not None:
        if policy not in ("none", "read"):
            out("deny", f"알 수 없는 도구 방침입니다({TOOL_POLICY_ENV}={policy[:20]}).")
        if policy == "none" or tool not in ("Bash", "Read"):
            out("deny", f"이 Codex 실행에서는 {tool[:60]} 도구를 사용할 수 없습니다({TOOL_POLICY_ENV}={policy}).")
    r = None
    if tool == "Bash":
        r = check_bash(ti.get("command", ""), write_state=write_state)
    elif tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        r = check_edit(ti.get("file_path") or ti.get("notebook_path"))
    elif tool == "Read":
        r = secret_access(ti.get("file_path", ""), os.getcwd(), is_secret)
    elif tool == "Grep":
        r = secret_access(ti.get("path") or os.getcwd(), os.getcwd(), contains_secret)
        if not r and SECRET_WORDS.search(f'{ti.get("path", "")} {ti.get("glob", "")}'):
            r = ("deny", "認証情報ファイルの検索は禁止")
    elif tool == "apply_patch":
        patch = ti.get("command", "")
        for operation, path in re.findall(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$", patch, re.M):
            # キャッシュ削除だけを例外とし、通常の追加・更新・移動は保護する。
            if operation == "Delete File" and is_python_cache(path, os.getcwd()) and os.path.isfile(path):
                continue
            r = check_edit(path)
            if r:
                break
            if operation == "Delete File" and not is_temp(os.path.abspath(path)):
                r = ("ask", "一時ディレクトリ外のファイル削除")
                break
    # Codex は MCP サーバー名の英数字・_ 以外を _ に正規化する（aws-api → aws_api）
    elif tool in ("mcp__aws-api__call_aws", "mcp__aws_api__call_aws"):
        argv = tokens(ti.get("cli_command", ""))
        if argv and argv[0] == "aws":
            r = check_aws_argv(argv, {}, "MCP")
        else:
            r = ("deny", "aws で始まらないコマンドは不可")
    elif tool.startswith("mcp__"):
        # 追加される MCP も既定は許可し、引数中の秘密情報パスだけを共通で拒否する
        r = check_mcp_input(ti)
    if r:
        out(*r)
    signal.alarm(0)
    sys.exit(0)


class GuardTimeout(Exception):
    """hook のタイムアウト（未判定で実行される）より前に自分で拒否するための期限切れ。"""


def _expire(signum, frame):
    raise GuardTimeout()


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, _expire)
    signal.alarm(GUARD_SECONDS)
    try:
        main()
    except GuardTimeout:
        out("deny", "가드 검사가 제한 시간을 넘었습니다. 작업을 실행하지 않았습니다.")
    except Exception:
        out("deny", "가드 검사에 실패했습니다. 보호 규칙을 확인하세요.")
